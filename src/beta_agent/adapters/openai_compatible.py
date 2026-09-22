from __future__ import annotations

import asyncio
import json
import inspect
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from ..runtime.cancellation import CancellationToken
from ..providers.messages import ProviderImageContent, ProviderMessage, ProviderTextContent
from ..providers.policy import ProviderRequestOptions
from ..types import AgentMessage, ModelEvent, ToolCall


@dataclass(slots=True)
class _ToolCallAccumulator:
    """Provider fragments plus the stable identity of one streamed call."""

    provider_id: str = ""
    provider_name: str = ""
    arguments: str = ""
    event_id: str = ""
    event_name: str = ""
    partial_arguments: dict[str, Any] = field(default_factory=dict)
    started: bool = False
    ended: bool = False

    def update(self, index: int, item: dict[str, Any]) -> None:
        function = item.get("function") or {}
        provider_id = item.get("id")
        if not self.event_id:
            self.event_id = str(provider_id or f"call_{index}")
        if provider_id:
            self.provider_id = str(provider_id)

        name = function.get("name")
        if name:
            name = str(name)
            if not self.event_name and not self.started:
                self.event_name = name
            self.provider_name += name

        arguments = function.get("arguments")
        if arguments:
            self.arguments += str(arguments)


class OpenAICompatibleAdapter:
    """用于 OpenAI-compatible Chat Completions API 的最小 streaming adapter。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | Any,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        session_header: str | None = None,
        before_payload: Any = None,
        on_response: Any = None,
        allow_authorization_override: bool = False,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_body = extra_body or {}
        self.headers = dict(headers or {})
        self.session_header = session_header
        self.before_payload = before_payload
        self.on_response = on_response
        self.allow_authorization_override = allow_authorization_override

    async def _api_key(self) -> str:
        value = self.api_key() if callable(self.api_key) else self.api_key
        if inspect.isawaitable(value):
            value = await value
        return str(value)

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ProviderMessage],
        tools: Sequence[object],
        request_options: ProviderRequestOptions | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[ModelEvent]:
        text = ""
        thinking = ""
        text_started = False
        thinking_started = False
        tool_parts: dict[int, _ToolCallAccumulator] = {}
        finish_reason: str | None = None
        try:
            options = request_options or ProviderRequestOptions()
            if options.transport == "websocket":
                raise ValueError("OpenAICompatibleAdapter does not support websocket transport")
            if cancellation is not None:
                cancellation.throw_if_cancelled()
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": self._messages(system_prompt, messages),
                "tools": [self._tool_schema(tool) for tool in tools],
                "stream": True,
                **self.extra_body,
            }
            if not payload["tools"]:
                payload.pop("tools")

            if self.before_payload is not None:
                replacement = self.before_payload(dict(payload))
                if inspect.isawaitable(replacement):
                    replacement = await replacement
                if replacement is not None:
                    payload = dict(replacement)

            headers = {"Content-Type": "application/json", **self.headers, **dict(options.headers)}
            if self.session_header and options.session_id:
                headers[self.session_header] = options.session_id
            timeout = self.timeout if options.timeout_seconds is None else options.timeout_seconds
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = None
                for attempt in range(options.retry.max_retries + 1):
                    attempt_headers = dict(headers)
                    if not self.allow_authorization_override or "Authorization" not in attempt_headers:
                        attempt_headers["Authorization"] = f"Bearer {await self._api_key()}"
                    try:
                        request = client.build_request("POST", f"{self.base_url}/chat/completions", json=payload, headers=attempt_headers)
                        response = await client.send(request, stream=True)
                        response.raise_for_status()
                        break
                    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                        if response is not None:
                            retryable = _retryable_response(response)
                            delay = _retry_delay(response)
                            await response.aclose()
                        else:
                            retryable, delay = True, None
                        if (not options.retry.enabled or not retryable or attempt >= options.retry.max_retries):
                            raise
                        if delay is None:
                            delay = min(options.retry.max_delay_seconds, options.retry.base_delay_seconds * (2 ** attempt) * random.uniform(0.8, 1.2))
                        if delay > options.retry.max_delay_seconds:
                            raise
                        await _cancelable_sleep(delay, cancellation)
                assert response is not None
                try:
                    if self.on_response is not None:
                        value = self.on_response(response.status_code, dict(response.headers))
                        if inspect.isawaitable(value):
                            await value
                    yield ModelEvent(
                        type="start",
                        partial=self._snapshot(
                            text, thinking, tool_parts, finish_reason=finish_reason
                        ),
                    )
                    async for line in response.aiter_lines():
                        if cancellation is not None:
                            cancellation.throw_if_cancelled()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        chunk = json.loads(data)
                        choice = chunk.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        reasoning_delta = next(
                            (
                                delta.get(name)
                                for name in ("thinking", "reasoning", "reasoning_content")
                                if delta.get(name)
                            ),
                            None,
                        )
                        if reasoning_delta:
                            if not thinking_started:
                                thinking_started = True
                                yield ModelEvent(
                                    type="thinking_start",
                                    partial=self._snapshot(
                                        text, thinking, tool_parts, finish_reason=finish_reason
                                    ),
                                    content_index=0,
                                )
                            thinking += str(reasoning_delta)
                            yield ModelEvent(
                                type="thinking_delta",
                                partial=self._snapshot(
                                    text, thinking, tool_parts, finish_reason=finish_reason
                                ),
                                content_index=0,
                                delta=str(reasoning_delta),
                            )
                        content_delta = delta.get("content")
                        if content_delta is not None and content_delta != "":
                            if not text_started:
                                text_started = True
                                yield ModelEvent(
                                    type="text_start",
                                    partial=self._snapshot(
                                        text, thinking, tool_parts, finish_reason=finish_reason
                                    ),
                                    content_index=0,
                                )
                            text += str(content_delta)
                            yield ModelEvent(
                                type="text_delta",
                                partial=self._snapshot(
                                    text, thinking, tool_parts, finish_reason=finish_reason
                                ),
                                content_index=0,
                                delta=str(content_delta),
                            )
                        for item in delta.get("tool_calls") or []:
                            index = int(item.get("index", 0))
                            acc = tool_parts.setdefault(index, _ToolCallAccumulator())
                            acc.update(index, item)
                            function = item.get("function") or {}
                            if not acc.started:
                                acc.started = True
                                yield ModelEvent(
                                    type="toolcall_start",
                                    partial=self._snapshot(
                                        text,
                                        thinking,
                                        tool_parts,
                                        finish_reason=finish_reason,
                                        placeholder_index=index,
                                    ),
                                    content_index=index,
                                    tool_call_id=acc.event_id,
                                    tool_name=acc.event_name or None,
                                )
                            tool_delta = function.get("arguments") or function.get("name")
                            if tool_delta:
                                yield ModelEvent(
                                    type="toolcall_delta",
                                    partial=self._snapshot(
                                        text, thinking, tool_parts, finish_reason=finish_reason
                                    ),
                                    content_index=index,
                                    tool_call_id=acc.event_id,
                                    tool_name=acc.event_name or None,
                                    delta=str(tool_delta),
                                )
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        partial = self._snapshot(
                            text,
                            thinking,
                            tool_parts,
                            finish_reason=finish_reason,
                        )
                finally:
                    await response.aclose()

            partial = self._snapshot(
                text,
                thinking,
                tool_parts,
                finish_reason=finish_reason,
                final=True,
            )
            if text_started:
                yield ModelEvent(type="text_end", partial=partial, content_index=0)
            if thinking_started:
                yield ModelEvent(type="thinking_end", partial=partial, content_index=0)
            for index in sorted(tool_parts):
                part = tool_parts[index]
                if part.ended:
                    continue
                calls = self._tool_calls({index: part})
                part.ended = True
                yield ModelEvent(
                    type="toolcall_end",
                    partial=partial,
                    content_index=index,
                    tool_call_id=part.event_id or (calls[0].id if calls else f"call_{index}"),
                    tool_name=part.event_name or None,
                    tool_call=calls[0] if calls else None,
                    completed_tool_call=calls[0] if calls else None,
                )
            yield ModelEvent(type="done", partial=partial)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider/network/decoding failure 属于 model stream contract 的一部分；
            # Agent 仍会为不符合 contract 的 third-party adapter 保留一层防御性 catch。
            error = AgentMessage.assistant(
                text,
                thinking=thinking,
                tool_calls=self._tool_calls(tool_parts),
                stop_reason="error",
                error_message=str(exc),
                error_type=type(exc).__name__,
            )
            yield ModelEvent(
                type="error",
                partial=error,
                error=str(exc),
                error_message=str(exc),
                error_type=type(exc).__name__,
            )

    def _messages(self, system_prompt: str, messages: Sequence[ProviderMessage]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        for message in messages:
            if not isinstance(message, ProviderMessage):
                raise TypeError("OpenAICompatibleAdapter requires ProviderMessage values")
            if message.role in {"system", "user"}:
                result.append({"role": message.role, "content": self._content(message.content)})
            elif message.role == "assistant":
                item: dict[str, Any] = {
                    "role": "assistant",
                    "content": self._content(message.content) or None,
                }
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                        }
                        for call in message.tool_calls
                    ]
                result.append(item)
            elif message.role == "tool":
                if any(isinstance(block, ProviderImageContent) for block in message.content):
                    raise ValueError(
                        "Chat Completions tool messages support text content only; "
                        "this tool result contains an image"
                    )
                item: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": self._content(message.content),
                }
                if message.name:
                    item["name"] = message.name
                result.append(item)
        return result

    def _content(
        self,
        content: Sequence[ProviderTextContent | ProviderImageContent],
    ) -> str | list[dict[str, Any]]:
        if len(content) == 1 and isinstance(content[0], ProviderTextContent):
            return content[0].text
        result: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, ProviderTextContent):
                result.append({"type": "text", "text": block.text})
            elif isinstance(block, ProviderImageContent):
                image_url = block.url
                if image_url is None and block.data is not None:
                    image_url = (
                        block.data
                        if block.data.startswith("data:")
                        else f"data:{block.media_type or 'application/octet-stream'};base64,{block.data}"
                    )
                result.append({"type": "image_url", "image_url": {"url": image_url}})
        return result

    def _tool_schema(self, tool: object) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": getattr(tool, "name"),
                "description": getattr(tool, "description"),
                "parameters": tool.schema(),
            },
        }

    def _snapshot(
        self,
        text: str,
        thinking: str,
        parts: dict[int, _ToolCallAccumulator],
        *,
        finish_reason: str | None,
        final: bool = False,
        placeholder_index: int | None = None,
    ) -> AgentMessage:
        if final:
            reason = finish_reason or ("tool_calls" if parts else "stop")
            tool_calls = self._tool_calls(parts)
        else:
            reason = self._map_finish_reason(finish_reason) if finish_reason else None
            tool_calls = self._partial_tool_calls(parts, placeholder_index=placeholder_index)
        return AgentMessage.assistant(
            text,
            thinking=thinking,
            tool_calls=tool_calls,
            stop_reason=reason,
        )

    def _tool_calls(self, parts: dict[int, _ToolCallAccumulator]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(parts):
            part = parts[index]
            raw = part.arguments or "{}"
            try:
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
            except json.JSONDecodeError:
                arguments = {"__raw__": raw}
            calls.append(
                ToolCall(
                    id=part.event_id or part.provider_id or f"call_{index}",
                    name=part.provider_name,
                    arguments=arguments,
                )
            )
        return calls

    def _partial_tool_calls(
        self,
        parts: dict[int, _ToolCallAccumulator],
        *,
        placeholder_index: int | None = None,
    ) -> list[ToolCall]:
        """Build a monotonic snapshot without inventing parsed arguments."""

        calls: list[ToolCall] = []
        for index in sorted(parts):
            part = parts[index]
            raw = "" if index == placeholder_index else part.arguments
            try:
                arguments = json.loads(raw) if raw else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                arguments = dict(part.partial_arguments)
            else:
                part.partial_arguments = dict(arguments)
            calls.append(
                ToolCall(
                    id=part.event_id or part.provider_id or f"call_{index}",
                    name=part.event_name,
                    arguments=arguments,
                )
            )
        return calls

    def _map_finish_reason(self, reason: str) -> str:
        if reason == "tool_calls":
            return "tool_calls"
        if reason == "length":
            return "length"
        return "stop"


def _retryable_response(response: httpx.Response) -> bool:
    directive = response.headers.get("x-should-retry", "").lower()
    if directive == "false": return False
    if directive == "true": return True
    return response.status_code in {408, 409, 429} or response.status_code >= 500


def _retry_delay(response: httpx.Response) -> float | None:
    milliseconds = response.headers.get("retry-after-ms")
    if milliseconds is not None:
        try: return max(0.0, float(milliseconds) / 1000)
        except ValueError: return None
    value = response.headers.get("retry-after")
    if value is None: return None
    try: return max(0.0, float(value))
    except ValueError:
        try: return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError): return None


async def _cancelable_sleep(delay: float, cancellation: CancellationToken | None) -> None:
    if delay <= 0: return
    if cancellation is None:
        await asyncio.sleep(delay); return
    sleeper = asyncio.create_task(asyncio.sleep(delay))
    cancelled = asyncio.create_task(cancellation.wait())
    done, pending = await asyncio.wait({sleeper, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending: task.cancel()
    if cancelled in done:
        raise asyncio.CancelledError()
