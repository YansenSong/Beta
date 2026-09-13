from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from .types import AgentContext, AgentEvent, Message, ToolBatchResult, ToolCall, ToolResult

ArgsT = TypeVar("ArgsT", bound=BaseModel)
Emit = Callable[[AgentEvent], Awaitable[None]]
ToolHandler = Callable[[ArgsT, "ToolExecutionContext"], Awaitable[ToolResult | str] | ToolResult | str]
BeforeToolCall = Callable[[ToolCall, BaseModel, AgentContext], Awaitable["BeforeToolCallDecision | None"] | "BeforeToolCallDecision | None"]
AfterToolCall = Callable[[ToolCall, BaseModel, ToolResult, bool, AgentContext], Awaitable["AfterToolCallPatch | None"] | "AfterToolCallPatch | None"]


@dataclass(slots=True)
class BeforeToolCallDecision:
    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(slots=True)
class AfterToolCallPatch:
    content: str | None = None
    details: Any = None
    replace_details: bool = False
    is_error: bool | None = None
    terminate: bool | None = None


@dataclass(slots=True)
class ToolExecutionContext:
    tool_call_id: str
    tool_name: str
    _emit: Emit

    async def progress(self, partial_result: Any) -> None:
        await self._emit(
            AgentEvent(
                type="tool_execution_update",
                tool_call_id=self.tool_call_id,
                tool_name=self.tool_name,
                result=partial_result,
            )
        )


@dataclass(slots=True)
class Tool(Generic[ArgsT]):
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    execution_mode: Literal["parallel", "sequential"] = "parallel"
    prepare_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None

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


@dataclass(slots=True)
class _Finalized:
    call: ToolCall
    result: ToolResult
    is_error: bool


class ToolRuntime:
    def __init__(
        self,
        *,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        execution_mode: Literal["parallel", "sequential"] = "parallel",
    ) -> None:
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.execution_mode = execution_mode

    async def execute_batch(
        self,
        *,
        context: AgentContext,
        calls: list[ToolCall],
        emit: Emit,
    ) -> ToolBatchResult:
        force_sequential = self.execution_mode == "sequential" or any(
            next((t for t in context.tools if t.name == call.name), None)
            and next(t for t in context.tools if t.name == call.name).execution_mode == "sequential"
            for call in calls
        )
        if force_sequential:
            finalized = []
            for call in calls:
                await self._emit_start(call, emit)
                prepared = await self._prepare(context, call)
                if isinstance(prepared, _Finalized):
                    item = prepared
                else:
                    item = await self._execute_prepared(context, prepared, emit)
                await self._emit_end(item, emit)
                finalized.append(item)
            return await self._commit(finalized, emit)

        # Pi-style parallel path: preflight stays source-ordered; execute is concurrent.
        entries: list[_Finalized | _Prepared] = []
        for call in calls:
            await self._emit_start(call, emit)
            prepared = await self._prepare(context, call)
            if isinstance(prepared, _Finalized):
                await self._emit_end(prepared, emit)
            entries.append(prepared)

        async def run(entry: _Finalized | _Prepared) -> _Finalized:
            if isinstance(entry, _Finalized):
                return entry
            item = await self._execute_prepared(context, entry, emit)
            await self._emit_end(item, emit)
            return item

        finalized = await asyncio.gather(*(run(entry) for entry in entries))
        return await self._commit(finalized, emit)

    async def _prepare(self, context: AgentContext, call: ToolCall) -> _Prepared | _Finalized:
        tool = next((item for item in context.tools if item.name == call.name), None)
        if tool is None:
            return _Finalized(call, ToolResult(content=f"Tool {call.name!r} not found"), True)

        try:
            raw_args = call.arguments
            if tool.prepare_arguments:
                raw_args = tool.prepare_arguments(raw_args)
            args = tool.args_model.model_validate(raw_args)
        except (ValidationError, ValueError, TypeError) as exc:
            return _Finalized(call, ToolResult(content=f"Invalid arguments: {exc}"), True)

        if self.before_tool_call:
            decision = self.before_tool_call(call, args, context)
            if inspect.isawaitable(decision):
                decision = await decision
            if decision and decision.block:
                return _Finalized(
                    call,
                    ToolResult(
                        content=decision.reason or "Tool execution was blocked",
                        terminate=decision.terminate,
                    ),
                    True,
                )
        return _Prepared(call, tool, args)

    async def _execute_prepared(self, context: AgentContext, prepared: _Prepared, emit: Emit) -> _Finalized:
        try:
            result = await prepared.tool.execute(
                prepared.args,
                ToolExecutionContext(prepared.call.id, prepared.call.name, emit),
            )
            is_error = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # tool failures become model-visible results
            result = ToolResult(content=f"Tool execution failed: {exc}")
            is_error = True

        if self.after_tool_call:
            patch = self.after_tool_call(prepared.call, prepared.args, result, is_error, context)
            if inspect.isawaitable(patch):
                patch = await patch
            if patch:
                if patch.content is not None:
                    result.content = patch.content
                if patch.replace_details:
                    result.details = patch.details
                if patch.terminate is not None:
                    result.terminate = patch.terminate
                if patch.is_error is not None:
                    is_error = patch.is_error

        return _Finalized(prepared.call, result, is_error)

    async def _emit_start(self, call: ToolCall, emit: Emit) -> None:
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
                error=item.result.content if item.is_error else None,
            )
        )

    async def _commit(self, finalized: list[_Finalized], emit: Emit) -> ToolBatchResult:
        messages: list[Message] = []
        for item in finalized:
            message = Message.tool_result(
                tool_call_id=item.call.id,
                name=item.call.name,
                content=item.result.content,
                is_error=item.is_error,
                details=item.result.details,
                added_tool_names=item.result.added_tool_names,
                terminate=item.result.terminate,
            )
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))
            messages.append(message)

        terminate = bool(finalized) and all(item.result.terminate for item in finalized)
        return ToolBatchResult(messages=messages, terminate=terminate)
