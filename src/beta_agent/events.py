from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Generic, TypeVar

from .types import AgentEvent

T = TypeVar("T")
_SENTINEL = object()


class EventStream(Generic[T]):
    """支持 await 最终结果的 async event iterator。"""

    def __init__(
        self,
        runner: Callable[[Callable[[AgentEvent], Awaitable[None]]], Awaitable[T]],
        *,
        on_done: Callable[["EventStream[T]"], None] | None = None,
        on_cancel: Callable[["EventStream[T]"], None] | None = None,
    ):
        self._queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue()
        self._closed = False
        self._started = False
        self._cancel_requested = False
        self._on_done = on_done
        self._on_cancel = on_cancel
        self._task = asyncio.create_task(self._drive(runner))
        self._task.add_done_callback(self._task_done)

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def add_done_callback(self, callback: Callable[["EventStream[T]"], None]) -> None:
        if self.done:
            callback(self)
            return
        previous = self._on_done

        def chained(stream: "EventStream[T]") -> None:
            if previous is not None:
                previous(stream)
            callback(stream)

        self._on_done = chained

    def add_cancel_callback(self, callback: Callable[["EventStream[T]"], None]) -> None:
        previous = self._on_cancel

        def chained(stream: "EventStream[T]") -> None:
            if previous is not None:
                previous(stream)
            callback(stream)

        self._on_cancel = chained

    def cancel(self) -> None:
        if self._cancel_requested:
            return
        self._cancel_requested = True
        if self._on_cancel is not None:
            self._on_cancel(self)
        # 如果 task 在第一次获得调度前就被取消，它不会进入 coroutine body。
        # 这个边界情况下先让它启动，使 Agent 的 token 能产出 domain-level aborted lifecycle，
        # 而不是留下一个一直挂起的 stream。
        # 对没有 on_cancel coordinator 的通用 EventStream，可以立即安全取消；
        # Agent/Host stream 则通过 coordinator，让 domain layer 先完成 aborted lifecycle 的收尾。
        if not self._task.done() and (self._started or self._on_cancel is None):
            self._task.cancel()

    def _close_queue(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_SENTINEL)

    def _task_done(self, _task: asyncio.Task[T]) -> None:
        self._close_queue()
        if self._on_done is not None:
            self._on_done(self)

    async def _drive(
        self,
        runner: Callable[[Callable[[AgentEvent], Awaitable[None]]], Awaitable[T]],
    ) -> T:
        self._started = True

        async def emit(event: AgentEvent) -> None:
            await self._queue.put(event)
            # 给 wrapper layer（例如 ExtensionHost）一个机会观察每个 event，
            # 并在 producer 继续执行同一个 event-loop turn 中剩余 lifecycle 之前取消它。
            await asyncio.sleep(0)

        try:
            return await runner(emit)
        finally:
            self._close_queue()

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

    async def wait(self) -> None:
        try:
            await self._task
        except asyncio.CancelledError:
            # EventStream 是通用组件，不会自行推断 Agent domain status。
            # Agent 本身会捕获 cancellation，并且通常返回一个结果。
            pass
