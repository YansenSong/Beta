from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .cancellation import CancellationToken, call_with_optional_cancellation
from .types import AgentContent, AgentContext, AgentEvent, AgentMessage, ToolBatchResult, ToolCall, ToolResult
from .messages import normalize_content_blocks

ArgsT = TypeVar("ArgsT", bound=BaseModel)
Emit = Callable[[AgentEvent], Awaitable[None]]
ToolHandler = Callable[[ArgsT, "ToolExecutionContext"], Awaitable[ToolResult | str] | ToolResult | str]
BeforeToolCall = Callable[..., Awaitable["BeforeToolCallDecision | None"] | "BeforeToolCallDecision | None"]
AfterToolCall = Callable[..., Awaitable["AfterToolCallPatch | None"] | "AfterToolCallPatch | None"]


@dataclass(slots=True)
class BeforeToolCallDecision:
    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(slots=True)
class AfterToolCallPatch:
    content: str | AgentContent | Sequence[AgentContent] | None = None
    details: Any = None
    replace_details: bool = False
    is_error: bool | None = None
    terminate: bool | None = None
    usage: dict[str, Any] | None = None


@dataclass(slots=True, init=False)
class ToolExecutionContext:
    tool_call_id: str
    tool_name: str
    cancellation: CancellationToken
    _emit: Emit
    _accepting_updates: bool
    _update_lock: asyncio.Lock
    operation_id: str | None
    run_id: str | None
    batch_id: str | None
    attempt: int
    is_recovery: bool
    _memo_get: Callable[[str], Awaitable[Any]] | None
    _memo_set: Callable[[str, Any], Awaitable[Any]] | None

    def __init__(
        self,
        tool_call_id: str,
        tool_name: str,
        cancellation_or_emit: CancellationToken | Emit | None = None,
        emit: Emit | None = None,
        *,
        cancellation: CancellationToken | None = None,
        _emit: Emit | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        batch_id: str | None = None,
        attempt: int = 1,
        is_recovery: bool = False,
        memo_get: Callable[[str], Awaitable[Any]] | None = None,
        memo_set: Callable[[str, Any], Awaitable[Any]] | None = None,
    ) -> None:
        """同时兼容 P0 的 ``(id, name, token, emit)`` 和旧 shape。"""

        if _emit is not None:
            emit_value = _emit
        elif isinstance(cancellation_or_emit, CancellationToken):
            emit_value = emit
        else:
            emit_value = cancellation_or_emit if callable(cancellation_or_emit) else emit
        if emit_value is None:
            raise TypeError("ToolExecutionContext requires an event emitter")

        if cancellation is not None:
            token = cancellation
        elif isinstance(cancellation_or_emit, CancellationToken):
            token = cancellation_or_emit
        elif isinstance(emit, CancellationToken):
            token = emit
        else:
            token = CancellationToken()

        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.cancellation = token
        self._emit = emit_value
        self._accepting_updates = True
        self._update_lock = asyncio.Lock()
        self.operation_id = operation_id
        self.run_id = run_id
        self.batch_id = batch_id
        self.attempt = attempt
        self.is_recovery = is_recovery
        self._memo_get = memo_get
        self._memo_set = memo_set

    async def get_memo(self, name: str) -> Any:
        if self._memo_get is None:
            raise RuntimeError("Durable memo is unavailable outside durable execution")
        return await self._memo_get(name)

    async def memo(self, name: str, candidate: Any) -> Any:
        if self._memo_set is None:
            raise RuntimeError("Durable memo is unavailable outside durable execution")
        return await self._memo_set(name, candidate)

    async def progress(self, partial_result: Any) -> None:
        # 长耗时 Tool 可以主动上报中间状态；这些 update 只用于 UI/Tracing，不写入消息历史。
        async with self._update_lock:
            if not self._accepting_updates:
                return
            self.cancellation.throw_if_cancelled()
            await self._emit(
                AgentEvent(
                    type="tool_execution_update",
                    tool_call_id=self.tool_call_id,
                    tool_name=self.tool_name,
                    result=partial_result,
                )
            )

    async def close_updates(self) -> None:
        async with self._update_lock:
            self._accepting_updates = False


@dataclass(slots=True)
class Tool(Generic[ArgsT]):
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    # 单个 Tool 可以要求顺序执行；同一批里只要出现一个 sequential Tool，整批就退化为串行。
    execution_mode: Literal["parallel", "sequential"] = "parallel"
    prepare_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    replay_policy: Literal["safe", "unsafe"] = "unsafe"

    def schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()

    async def execute(self, args: ArgsT, ctx: ToolExecutionContext) -> ToolResult:
        value = self.handler(args, ctx)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, ToolResult):
            return value
        return ToolResult(content=str(value))


@dataclass(slots=True)
class _Prepared:
    call: ToolCall
    tool: Tool[Any]
    args: BaseModel
    durable_handle: Any = None


@dataclass(slots=True)
class _Finalized:
    call: ToolCall
    result: ToolResult
    is_error: bool
    aborted: bool = False
    durable_handle: Any = None
    durable_metadata: dict[str, Any] | None = None


class ToolCoordinator(Protocol):
    async def prepare_operation(self, prepared: _Prepared, source_index: int) -> Any: ...
    async def settle_operation(self, handle: Any, result: ToolResult, is_error: bool) -> dict[str, Any]: ...
    async def acknowledge_published(self, operation_id: str) -> None: ...
    def execution_context_kwargs(self, handle: Any) -> dict[str, Any]: ...


class ToolRuntime:
    """负责 Tool Call 从模型输出到 Tool Result 的完整生命周期。"""

    def __init__(
        self,
        *,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        execution_mode: Literal["parallel", "sequential"] = "parallel",
        coordinator: ToolCoordinator | None = None,
    ) -> None:
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.execution_mode = execution_mode
        self.coordinator = coordinator

    async def execute_batch(
        self,
        *,
        context: AgentContext,
        calls: list[ToolCall],
        emit: Emit,
        cancellation: CancellationToken | None = None,
    ) -> ToolBatchResult:
        token = cancellation or CancellationToken()
        if not calls:
            return ToolBatchResult(messages=[])

        begin_batch = getattr(self.coordinator, "begin_batch", None)
        if begin_batch is not None:
            begin_batch()

        force_sequential = self.execution_mode == "sequential" or any(
            next((t for t in context.tools if t.name == call.name), None)
            and next(t for t in context.tools if t.name == call.name).execution_mode == "sequential"
            for call in calls
        )
        try:
            if force_sequential:
                finalized, aborted = await self._execute_sequential(context, calls, emit, token)
            else:
                finalized, aborted = await self._execute_parallel(context, calls, emit, token)
            messages = await self._commit(finalized, emit)
            terminate = bool(finalized) and all(item.result.terminate for item in finalized)
            return ToolBatchResult(messages=messages, terminate=terminate, aborted=aborted)
        except asyncio.CancelledError:
            # Root Agent cancellation 可能在 preflight 或 child task 运行时到达。
            # 在把控制权交还给 Agent 前，先把整批调用转换成协议完整的 result set。
            token.cancel()
            finalized = [_aborted(call) for call in calls]
            messages = await self._commit(finalized, emit)
            return ToolBatchResult(messages=messages, aborted=True)

    async def _execute_sequential(
        self,
        context: AgentContext,
        calls: list[ToolCall],
        emit: Emit,
        cancellation: CancellationToken,
    ) -> tuple[list[_Finalized], bool]:
        finalized: list[_Finalized] = []
        for index, call in enumerate(calls):
            if cancellation.cancelled:
                return await self._abort_remaining(calls, index, finalized, emit), True

            started = False
            ended = False
            try:
                await self._emit_start(call, emit)
                started = True
                prepared = await self._prepare(context, call, cancellation)
                if isinstance(prepared, _Prepared) and self.coordinator is not None:
                    prepared.durable_handle = await self.coordinator.prepare_operation(prepared, index)
                item = prepared if isinstance(prepared, _Finalized) else await self._execute_prepared(
                    context, prepared, emit, cancellation
                )
                await self._emit_end(item, emit)
                ended = True
                finalized.append(item)
            except asyncio.CancelledError:
                cancellation.cancel()
                if started and not ended:
                    item = _aborted(call)
                    await self._emit_end(item, emit)
                    finalized.append(item)
                return await self._abort_remaining(calls, index + 1, finalized, emit), True
        return finalized, False

    async def _execute_parallel(
        self,
        context: AgentContext,
        calls: list[ToolCall],
        emit: Emit,
        cancellation: CancellationToken,
    ) -> tuple[list[_Finalized], bool]:
        # Lookup / argument preparation / validation / before hook 仍按 source order 执行；
        # 只有准备完成后的 execute phase 会并发运行。
        entries: list[_Finalized | _Prepared | None] = [None] * len(calls)
        for index, call in enumerate(calls):
            if cancellation.cancelled:
                finalized = await self._abort_from_entries(calls, entries, index, emit)
                return finalized, True
            try:
                await self._emit_start(call, emit)
                prepared = await self._prepare(context, call, cancellation)
                entries[index] = prepared
                if isinstance(prepared, _Finalized):
                    await self._emit_end(prepared, emit)
            except asyncio.CancelledError:
                cancellation.cancel()
                entries[index] = _aborted(call)
                await self._emit_end(entries[index], emit)
                finalized = await self._abort_from_entries(calls, entries, index + 1, emit)
                return finalized, True

        tasks: dict[int, asyncio.Task[_Finalized]] = {}

        if self.coordinator is not None:
            for index, entry in enumerate(entries):
                if isinstance(entry, _Prepared):
                    entry.durable_handle = await self.coordinator.prepare_operation(entry, index)

        async def run(index: int, entry: _Prepared) -> _Finalized:
            item = await self._execute_prepared(context, entry, emit, cancellation)
            # execution_end 按完成顺序产生；下面最终 message commit
            # 仍会按 source order 消费 gather 后的值。
            await self._emit_end(item, emit)
            return item

        for index, entry in enumerate(entries):
            if isinstance(entry, _Prepared):
                tasks[index] = asyncio.create_task(run(index, entry))

        try:
            results = await asyncio.gather(*tasks.values())
            for index, item in zip(tasks, results):
                entries[index] = item
            return [entry for entry in entries if isinstance(entry, _Finalized)], False
        except asyncio.CancelledError:
            cancellation.cancel()
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            task_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for index, task_result in zip(tasks, task_results):
                if isinstance(task_result, _Finalized):
                    entries[index] = task_result
                else:
                    entries[index] = _aborted(calls[index])
                    await self._emit_end(entries[index], emit)
            finalized = [entry for entry in entries if isinstance(entry, _Finalized)]
            return finalized, True

    async def _abort_remaining(
        self,
        calls: list[ToolCall],
        start: int,
        finalized: list[_Finalized],
        emit: Emit,
    ) -> list[_Finalized]:
        for call in calls[start:]:
            await self._emit_start(call, emit)
            item = _aborted(call)
            await self._emit_end(item, emit)
            finalized.append(item)
        return finalized

    async def _abort_from_entries(
        self,
        calls: list[ToolCall],
        entries: list[_Finalized | _Prepared | None],
        start: int,
        emit: Emit,
    ) -> list[_Finalized]:
        del start
        for index in range(len(calls)):
            entry = entries[index]
            if isinstance(entry, _Finalized):
                continue
            # 已 prepared 的 entry 在按 source order 的 preflight 中已经发出 tool_execution_start，
            # 因此只有尚未 prepared 的 entry 需要补发 start。
            if entry is None:
                await self._emit_start(calls[index], emit)
            item = _aborted(calls[index])
            entries[index] = item
            await self._emit_end(item, emit)
        return [entry if isinstance(entry, _Finalized) else _aborted(calls[index]) for index, entry in enumerate(entries)]

    async def _prepare(
        self,
        context: AgentContext,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> _Prepared | _Finalized:
        # preflight 顺序为：lookup -> prepare arguments -> schema validate -> before hook。
        # 任一步失败都会被规范化成 Tool Result，让模型下一轮能够“看到失败原因”并自我修正。
        cancellation.throw_if_cancelled()
        tool = next((item for item in context.tools if item.name == call.name), None)
        if tool is None:
            return _Finalized(
                call,
                ToolResult(content=f"Tool {call.name!r} not found", details={"stage": "tool_execute"}),
                True,
            )

        try:
            raw_args = call.arguments
            if tool.prepare_arguments:
                raw_args = tool.prepare_arguments(raw_args)
            args = tool.args_model.model_validate(raw_args)
        except asyncio.CancelledError:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            return _Finalized(
                call,
                ToolResult(
                    content=f"Invalid arguments: {exc}",
                    details={"stage": "prepare_arguments", "exception_type": type(exc).__name__},
                ),
                True,
            )
        except Exception as exc:
            return _Finalized(
                call,
                ToolResult(
                    content=f"Tool arguments could not be prepared: {exc}",
                    details={"stage": "prepare_arguments", "exception_type": type(exc).__name__},
                ),
                True,
            )

        if self.before_tool_call:
            try:
                decision = await call_with_optional_cancellation(
                    self.before_tool_call,
                    call,
                    args,
                    context,
                    cancellation=cancellation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Permission 和 policy hook 采用 fail closed；当 before hook 无法作出决定时，
                # 绝不会调用目标 Tool。
                return _Finalized(
                    call,
                    ToolResult(
                        content=f"Tool execution blocked because before_tool_call hook failed: {exc}",
                        details={"stage": "before_tool_call", "exception_type": type(exc).__name__},
                    ),
                    True,
                )
            cancellation.throw_if_cancelled()
            if decision and decision.block:
                return _Finalized(
                    call,
                    ToolResult(
                        content=decision.reason or "Tool execution was blocked",
                        terminate=decision.terminate,
                        details={"stage": "before_tool_call"},
                    ),
                    True,
                )
        return _Prepared(call, tool, args)

    async def _execute_prepared(
        self,
        context: AgentContext,
        prepared: _Prepared,
        emit: Emit,
        cancellation: CancellationToken,
    ) -> _Finalized:
        cancellation.throw_if_cancelled()
        execution_context = ToolExecutionContext(
            prepared.call.id,
            prepared.call.name,
            emit,
            cancellation=cancellation,
            **(self.coordinator.execution_context_kwargs(prepared.durable_handle) if self.coordinator and prepared.durable_handle else {}),
        )
        try:
            result = await prepared.tool.execute(prepared.args, execution_context)
            is_error = False
        except asyncio.CancelledError:
            # batch boundary 会把这里转换成 aborted Tool Result；
            # 直接调用 Tool.execute 的调用方仍会收到真实的 task cancellation。
            raise
        except Exception as exc:  # Tool failure 会被转换为 model-visible result。
            result = ToolResult(
                content=f"Tool execution failed: {exc}",
                details={"stage": "tool_execute", "exception_type": type(exc).__name__},
            )
            is_error = True
        finally:
            await execution_context.close_updates()

        # after hook 位于 Tool 真正执行之后、tool_execution_end 事件之前，
        # 可以统一补充 metadata、改写展示内容或设置 terminate，而不用侵入具体 Tool。
        if self.after_tool_call:
            patch = None
            try:
                patch = await call_with_optional_cancellation(
                    self.after_tool_call,
                    prepared.call,
                    prepared.args,
                    result,
                    is_error,
                    context,
                    cancellation=cancellation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The external effect has already returned.  Normalize the hook
                # failure, then continue through durable settlement so recovery
                # can never execute the effect a second time.
                result = ToolResult(
                    content=f"Tool executed, but after_tool_call hook failed: {exc}",
                    details={
                        "stage": "after_tool_call",
                        "exception_type": type(exc).__name__,
                        "tool_executed": True,
                        "original_result_details": result.details,
                    },
                )
                is_error = True
            if patch:
                if patch.content is not None:
                    result.content = normalize_content_blocks(patch.content)
                if patch.replace_details:
                    result.details = patch.details
                if patch.terminate is not None:
                    result.terminate = patch.terminate
                if patch.is_error is not None:
                    is_error = patch.is_error
                if patch.usage is not None:
                    result.usage = dict(patch.usage)

        metadata = None
        if self.coordinator is not None and prepared.durable_handle is not None:
            metadata = await self.coordinator.settle_operation(prepared.durable_handle, result, is_error)
        return _Finalized(prepared.call, result, is_error, durable_handle=prepared.durable_handle, durable_metadata=metadata)

    async def _emit_start(self, call: ToolCall, emit: Emit) -> None:
        # start 表示 Runtime 开始处理这次调用；即使 Tool 不存在或参数非法，也会有完整生命周期事件。
        await emit(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
            )
        )

    async def _emit_end(self, item: _Finalized, emit: Emit) -> None:
        await emit(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=item.call.id,
                tool_name=item.call.name,
                args=item.call.arguments,
                result=item.result,
                error=item.result.content.text if item.is_error else None,
            )
        )

    async def _commit(self, finalized: list[_Finalized], emit: Emit) -> list[AgentMessage]:
        # 只有最终 Tool Result 会进入对话历史；progress/update 事件不会污染模型上下文。
        messages: list[AgentMessage] = []
        for item in finalized:
            message = AgentMessage.tool_result(
                tool_call_id=item.call.id,
                name=item.call.name,
                content=item.result.content,
                is_error=item.is_error,
                details=item.result.details,
                added_tool_names=item.result.added_tool_names,
                terminate=item.result.terminate,
                aborted=item.aborted,
                usage=item.result.usage,
                **(item.durable_metadata or {}),
            )
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))
            if self.coordinator is not None and item.durable_metadata:
                await self.coordinator.acknowledge_published(item.durable_metadata["durable_operation_id"])
            messages.append(message)

        return messages


def _aborted(call: ToolCall) -> _Finalized:
    return _Finalized(
        call,
        ToolResult(
            content="Operation aborted",
            details={"stage": "tool_execute", "exception_type": "CancelledError"},
        ),
        True,
        aborted=True,
    )
