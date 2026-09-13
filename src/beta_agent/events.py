from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Generic, TypeVar

from .types import AgentEvent

T = TypeVar("T")
_SENTINEL = object()


class EventStream(Generic[T]):
    """Async event iterator with an awaitable final result."""

    def __init__(self, runner: Callable[[Callable[[AgentEvent], Awaitable[None]]], Awaitable[T]]):
        self._queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue()
        self._task = asyncio.create_task(self._drive(runner))

    async def _drive(
        self,
        runner: Callable[[Callable[[AgentEvent], Awaitable[None]]], Awaitable[T]],
    ) -> T:
        async def emit(event: AgentEvent) -> None:
            await self._queue.put(event)

        try:
            return await runner(emit)
        finally:
            await self._queue.put(_SENTINEL)

    def __aiter__(self) -> AsyncIterator[AgentEvent]:
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._queue.get()
        if item is _SENTINEL:
            raise StopAsyncIteration
        assert isinstance(item, AgentEvent)
        return item

    async def result(self) -> T:
        return await self._task
