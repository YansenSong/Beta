from __future__ import annotations
import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Sequence
from ....runtime.cancellation import CancellationToken
from ....runtime.cancellation import call_with_optional_cancellation
from ....messages import utc_now_iso
from ...session import SessionTree, agent_message_from_dict
from ...tool import (
    AfterToolCallContext,
    Tool,
    ToolExecutionContext,
    _PATCH_UNSET,
    _after_hook_cancelled_result,
    adapt_tool_hook,
)
from ....types import AgentContext, AgentMessage, ToolCall, ToolResult
from .tools import DurableToolCoordinator, OperationHandle
from ..types import DurableStorage
from .outbox import SessionOutboxPublisher

@dataclass(slots=True)
class RecoveryReport:
    replayed_operations: list[str] = field(default_factory=list)
    interrupted_operations: list[str] = field(default_factory=list)
    published_messages: list[str] = field(default_factory=list)
    pending_run_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

async def recover_durable_runtime(storage: DurableStorage, session: SessionTree, session_file, tools: Sequence[Tool[Any]], *, after_tool_call=None) -> RecoveryReport:
    report, tool_map = RecoveryReport(), {tool.name: tool for tool in tools}
    after_hook = adapt_tool_hook(after_tool_call, kind="after")
    for operation in await storage.scan_effect_pending_operations():
        tool, reason, args = tool_map.get(operation.tool_name), None, None
        if tool is None: reason = "missing_tool"
        elif operation.replay_policy != "safe" or tool.replay_policy != "safe": reason = "unsafe_effect_interrupted"
        else:
            try: args = tool.args_model.model_validate(operation.arguments)
            except Exception: reason = "schema_drift"
        coordinator = DurableToolCoordinator(storage, task_id=operation.task_id, run_id=operation.run_id, batch_id=operation.batch_id)
        handle = OperationHandle(replace(operation, attempt=operation.attempt + 1, updated_at=utc_now_iso()), True)
        if reason:
            result = ToolResult(content="Tool execution was interrupted after its durable intent was recorded. The runtime did not replay this unsafe operation because its external side effect may already have occurred. Inspect the environment before retrying.",
                details={"stage": "durable_recovery", "code": reason, "operation_id": operation.id,
                         "tool_name": operation.tool_name, "effect_may_have_occurred": True})
            await coordinator.settle_operation(handle, result, True)
            report.interrupted_operations.append(operation.id)
        else:
            async def emit(_event): return None
            ctx = ToolExecutionContext(operation.tool_call_id, operation.tool_name, emit,
                args=operation.source_arguments,
                cancellation=CancellationToken(), **coordinator.execution_context_kwargs(handle))
            try:
                result, is_error = await tool.execute(args, ctx), False
            except Exception as exc:
                result, is_error = ToolResult(content=f"Tool execution failed: {exc}",
                    details={"stage": "tool_execute", "exception_type": type(exc).__name__}), True
            finally: await ctx.close_updates()
            if after_tool_call is not None:
                try:
                    context = AgentContext(messages=session.reconstruct_messages(), tools=tools)
                    call = ToolCall(operation.tool_call_id, operation.tool_name, operation.arguments)
                    assistant = next(
                        (
                            message
                            for message in reversed(context.messages)
                            if message.role == "assistant"
                            and any(item.id == operation.tool_call_id for item in message.tool_calls)
                        ),
                        AgentMessage.assistant(tool_calls=[call], stop_reason="tool_calls"),
                    )
                    patch = await call_with_optional_cancellation(
                        after_hook,
                        AfterToolCallContext(
                            assistant_message=assistant,
                            tool_call=call,
                            args=args,
                            result=result,
                            is_error=is_error,
                            context=context,
                        ),
                        cancellation=ctx.cancellation,
                    )
                except asyncio.CancelledError:
                    result = _after_hook_cancelled_result(result)
                    is_error = True
                    patch = None
                except Exception as exc:
                    result = ToolResult(content=f"Tool executed, but after_tool_call hook failed: {exc}",
                        details={"stage": "after_tool_call", "exception_type": type(exc).__name__,
                                 "tool_executed": True, "original_result_details": result.details})
                    is_error = True
                    patch = None
                if patch:
                    from ...messages import normalize_content_blocks
                    if patch.content is not None: result.content = normalize_content_blocks(patch.content)
                    if patch.replace_details:
                        result.details = None if getattr(patch, "details", _PATCH_UNSET) is _PATCH_UNSET else patch.details
                    elif getattr(patch, "details", _PATCH_UNSET) is not _PATCH_UNSET:
                        result.details = patch.details
                    if patch.terminate is not None: result.terminate = patch.terminate
                    if patch.is_error is not None: is_error = patch.is_error
                    if patch.usage is not None: result.usage = dict(patch.usage)
            await coordinator.settle_operation(handle, result, is_error)
            report.replayed_operations.append(operation.id)
    publisher = SessionOutboxPublisher(storage, session, session_file)
    for outbox in await storage.scan_pending_outbox():
        await publisher.publish_record(outbox)
        report.published_messages.append(outbox.durable_message_id)
    for task in await storage.scan_tasks():
        if task.status == "running":
            await storage.replace_task(replace(task, status="pending", updated_at=utc_now_iso()))
            report.pending_run_ids.append(task.id)
        elif task.status == "pending" and task.kind == "agent_run":
            report.pending_run_ids.append(task.id)
    return report
