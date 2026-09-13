from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from ..types import Message, ModelEvent, ToolCall


class OpenAICompatibleAdapter:
    """Minimal streaming adapter for OpenAI-compatible Chat Completions APIs."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_body = extra_body or {}

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Message],
        tools: Sequence[object],
    ) -> AsyncIterator[ModelEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system_prompt, messages),
            "tools": [self._tool_schema(tool) for tool in tools],
            "stream": True,
            **self.extra_body,
        }
        if not payload["tools"]:
            payload.pop("tools")

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        partial = Message.assistant("", stop_reason="stop")
        yield ModelEvent(type="start", partial=partial)

        text = ""
        tool_parts: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    chunk = json.loads(data)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        text += delta["content"]
                    for item in delta.get("tool_calls") or []:
                        index = int(item.get("index", 0))
                        acc = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if item.get("id"):
                            acc["id"] = item["id"]
                        function = item.get("function") or {}
                        if function.get("name"):
                            acc["name"] += function["name"]
                        if function.get("arguments"):
                            acc["arguments"] += function["arguments"]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    partial = Message.assistant(
                        text,
                        tool_calls=self._tool_calls(tool_parts),
                        stop_reason=self._map_finish_reason(finish_reason),
                    )
                    yield ModelEvent(type="update", partial=partial)

        final = Message.assistant(
            text,
            tool_calls=self._tool_calls(tool_parts),
            stop_reason=self._map_finish_reason(finish_reason),
        )
        yield ModelEvent(type="done", partial=final)

    def _messages(self, system_prompt: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        for message in messages:
            if message.role in {"system", "user"}:
                result.append({"role": message.role, "content": message.content})
            elif message.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.content or None}
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
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
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

    def _tool_calls(self, parts: dict[int, dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(parts):
            part = parts[index]
            raw = part["arguments"] or "{}"
            try:
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
            except json.JSONDecodeError:
                arguments = {"__raw__": raw}
            calls.append(ToolCall(id=part["id"] or f"call_{index}", name=part["name"], arguments=arguments))
        return calls

    def _map_finish_reason(self, reason: str) -> str:
        if reason == "tool_calls":
            return "tool_calls"
        if reason == "length":
            return "length"
        return "stop"
