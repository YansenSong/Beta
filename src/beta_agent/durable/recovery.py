from __future__ import annotations
from dataclasses import dataclass, field, replace
from typing import Any, Sequence
from ..cancellation import CancellationToken
from ..cancellation import call_with_optional_cancellation
from ..messages import utc_now_iso
from ..session import SessionTree, agent_message_from_dict
from ..tools import Tool, ToolExecutionContext
from ..types import AgentContext, ToolCall, ToolResult
from .coordinator import DurableToolCoordinator, OperationHandle
from .storage import DurableStorage

@dataclass(slots=True)
class RecoveryReport:
    replayed_operations: list[str] = field(default_factory=list)
    interrupted_operations: list[str] = field(default_factory=list)
    published_messages: list[str] = field(default_factory=list)
    pending_run_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

async def recover_durable_runtime(storage: DurableStorage, session: SessionTree, session_file, tools: Sequence[Tool[Any]], *, after_tool_call=None) -> RecoveryReport:
    report, tool_map = RecoveryReport(), {tool.name: tool for tool in tools}
    for operation in await storage.scan_effect_pending_operations():
        tool, reason, args = tool_map.get(operation.tool_name), None, None
        if tool is None: reason = "missing_tool"
        elif operation.replay_policy != "safe" or tool.replay_policy != "safe": reason = "unsafe_effect_interrupted"
        else:
            try: args = tool.args_model.model_validate(operation.arguments)
            except Exception: reason = "schema_drift"
        coordinator = DurableToolCoordinator(storage, task_id=operation.task_id, run_id=operation.run_id)
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
                cancellation=CancellationToken(), **coordinator.execution_context_kwargs(handle))
            try:
                result, is_error = await tool.execute(args, ctx), False
            except Exception as exc:
                result, is_error = ToolResult(content=f"Tool execution failed: {exc}",
                    details={"stage": "tool_execute", "exception_type": type(exc).__name__}), True
            finally: await ctx.close_updates()
            if after_tool_call is not None:
                patch = await call_with_optional_cancellation(
                    after_tool_call,
                    ToolCall(operation.tool_call_id, operation.tool_name, operation.arguments),
                    args, result, is_error, AgentContext(messages=session.reconstruct_messages(), tools=tools),
                    cancellation=ctx.cancellation,
                )
                if patch:
                    from ..messages import normalize_content_blocks
                    if patch.content is not None: result.content = normalize_content_blocks(patch.content)
                    if patch.replace_details: result.details = patch.details
                    if patch.terminate is not None: result.terminate = patch.terminate
                    if patch.is_error is not None: is_error = patch.is_error
                    if patch.usage is not None: result.usage = dict(patch.usage)
            await coordinator.settle_operation(handle, result, is_error)
            report.replayed_operations.append(operation.id)
    for outbox in await storage.scan_pending_outbox():
        session.append_message(agent_message_from_dict(outbox.message))
        session.save_jsonl(session_file)
        await storage.mark_outbox_published(outbox.operation_id, utc_now_iso())
        report.published_messages.append(outbox.durable_message_id)
    for task in await storage.scan_tasks():
        if task.status == "running":
            await storage.replace_task(replace(task, status="pending", updated_at=utc_now_iso()))
            report.pending_run_ids.append(task.id)
        elif task.status == "pending" and task.kind == "agent_run":
            report.pending_run_ids.append(task.id)
    return report
