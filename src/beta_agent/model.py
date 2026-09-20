from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from .cancellation import CancellationToken
from .provider_messages import ProviderMessage
from .provider_policy import ProviderRequestOptions
from .types import AgentMessage, Message, ModelEvent


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

    def __init__(self, responses: Sequence[AgentMessage]):
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
        final = self._responses.popleft()
        partial = AgentMessage.assistant("", stop_reason=final.stop_reason or "stop")
        yield ModelEvent(type="start", partial=partial)

        if cancellation is not None:
            cancellation.throw_if_cancelled()
        if final.text:
            partial = final.copy()
            yield ModelEvent(type="update", partial=partial)

        if cancellation is not None:
            cancellation.throw_if_cancelled()
        if final.tool_calls:
            partial = final.copy()
            yield ModelEvent(type="update", partial=partial)

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
