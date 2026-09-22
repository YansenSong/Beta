from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
import json
from typing import Protocol

from ..runtime.cancellation import CancellationToken
from .messages import ProviderMessage
from .policy import ProviderRequestOptions
from ..types import AgentMessage, Message, ModelEvent, ToolCall


class ModelAdapter(Protocol):
    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ProviderMessage],
        tools: Sequence[object],
        request_options: ProviderRequestOptions,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]: ...


class ScriptedModelAdapter:
    """供测试和示例使用的 deterministic adapter。"""

    def __init__(self, responses: Sequence[AgentMessage | ModelEvent | Sequence[ModelEvent]]):
        self._responses = deque(responses)
        self.calls: list[list[ProviderMessage]] = []

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ProviderMessage],
        tools: Sequence[object],
        request_options: ProviderRequestOptions | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[ModelEvent]:
        if cancellation is not None:
            cancellation.throw_if_cancelled()
        if not self._responses:
            raise RuntimeError("ScriptedModelAdapter has no responses left")
        self.calls.append(list(messages))
        scripted = self._responses.popleft()
        if isinstance(scripted, ModelEvent):
            yield scripted
            return
        if isinstance(scripted, Sequence) and not isinstance(scripted, AgentMessage):
            for event in scripted:
                if not isinstance(event, ModelEvent):
                    raise TypeError("ScriptedModelAdapter event scripts must contain ModelEvent values")
                yield event
            return
        final = scripted
        if not isinstance(final, AgentMessage):
            raise TypeError("ScriptedModelAdapter responses must be AgentMessage or ModelEvent values")
        partial = AgentMessage.assistant("", stop_reason=final.stop_reason or "stop")
        yield ModelEvent(type="start", partial=partial)

        if cancellation is not None:
            cancellation.throw_if_cancelled()
        thinking = final.thinking or str(final.metadata.get("thinking", ""))
        if thinking:
            yield ModelEvent(
                type="thinking_start",
                partial=partial.copy(thinking=thinking),
                content_index=0,
            )
            yield ModelEvent(
                type="thinking_delta",
                partial=final.copy(thinking=thinking),
                content_index=0,
                delta=thinking,
            )
            yield ModelEvent(
                type="thinking_end",
                partial=final.copy(thinking=thinking),
                content_index=0,
            )
        if final.text:
            yield ModelEvent(
                type="text_start",
                partial=final.copy(thinking=thinking),
                content_index=0,
            )
            yield ModelEvent(
                type="text_delta",
                partial=final.copy(thinking=thinking),
                content_index=0,
                delta=final.text,
            )
            yield ModelEvent(type="text_end", partial=final.copy(thinking=thinking), content_index=0)

        if cancellation is not None:
            cancellation.throw_if_cancelled()
        if final.tool_calls:
            accumulated_calls: list[ToolCall] = []
            for content_index, call in enumerate(final.tool_calls):
                placeholder = ToolCall(call.id, call.name, {})
                yield ModelEvent(
                    type="toolcall_start",
                    partial=final.copy(tool_calls=[*accumulated_calls, placeholder]),
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content_index=content_index,
                )
                raw_arguments = json.dumps(call.arguments, sort_keys=True)
                yield ModelEvent(
                    type="toolcall_delta",
                    partial=final.copy(
                        tool_calls=[
                            *accumulated_calls,
                            ToolCall(call.id, call.name, dict(call.arguments)),
                        ]
                    ),
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content_index=content_index,
                    delta=raw_arguments,
                )
                yield ModelEvent(
                    type="toolcall_end",
                    partial=final.copy(tool_calls=[*accumulated_calls, call]),
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content_index=content_index,
                    tool_call=call,
                    completed_tool_call=call,
                )
                accumulated_calls.append(call)

        if cancellation is not None:
            cancellation.throw_if_cancelled()
        yield ModelEvent(type="done", partial=final)


def accepts_request_options(adapter: ModelAdapter) -> bool:
    import inspect

    signature = inspect.signature(adapter.stream)
    return "request_options" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
