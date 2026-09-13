from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from .types import Message, ModelEvent


class ModelAdapter(Protocol):
    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Message],
        tools: Sequence[object],
    ) -> AsyncIterator[ModelEvent]: ...


class ScriptedModelAdapter:
    """Deterministic adapter for tests and examples."""

    def __init__(self, responses: Sequence[Message]):
        self._responses = deque(responses)
        self.calls: list[list[Message]] = []

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Message],
        tools: Sequence[object],
    ) -> AsyncIterator[ModelEvent]:
        if not self._responses:
            raise RuntimeError("ScriptedModelAdapter has no responses left")
        self.calls.append(list(messages))
        final = self._responses.popleft()
        partial = Message.assistant("", stop_reason=final.stop_reason or "stop")
        yield ModelEvent(type="start", partial=partial)

        if final.content:
            partial = final.copy(content=final.content)
            yield ModelEvent(type="update", partial=partial)

        if final.tool_calls:
            partial = final.copy()
            yield ModelEvent(type="update", partial=partial)

        yield ModelEvent(type="done", partial=final)
