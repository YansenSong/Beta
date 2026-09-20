from __future__ import annotations
import uuid
from dataclasses import dataclass, replace
from typing import Any
from ..messages import AgentMessage, utc_now_iso
from ..session import agent_message_to_dict
from ..types import ToolResult
from .failpoints import Failpoint, NoopFailpoint
from .records import OutboxRecord, ToolOperationRecord
from .serialization import arguments_hash
from .storage import DurableStorage

@dataclass(frozen=True, slots=True)
class OperationHandle:
    operation: ToolOperationRecord
    is_recovery: bool = False

class DurableToolCoordinator:
    def __init__(self, storage: DurableStorage, *, task_id: str, run_id: str, failpoint: Failpoint | None = None) -> None:
        self.storage, self.task_id, self.run_id = storage, task_id, run_id
        self.batch_id = uuid.uuid4().hex
        self.failpoint = failpoint or NoopFailpoint()

    async def prepare_operation(self, prepared, source_index: int) -> OperationHandle:
        now = utc_now_iso()
        arguments = prepared.args.model_dump(mode="json", by_alias=True)
        operation = ToolOperationRecord(
            uuid.uuid4().hex, self.task_id, self.run_id, self.batch_id, source_index,
            prepared.call.id, prepared.call.name, arguments, arguments_hash(arguments),
            prepared.tool.replay_policy, "effect_pending", 1, None, None, None, now, now,
        )
        await self.storage.create_tool_batch_intents([operation])
        await self.failpoint.hit("after_effect_intent_committed")
        return OperationHandle(operation)

    def execution_context_kwargs(self, handle: OperationHandle) -> dict[str, Any]:
        operation = handle.operation
        return {
            "operation_id": operation.id, "run_id": operation.run_id, "batch_id": operation.batch_id,
            "attempt": operation.attempt, "is_recovery": handle.is_recovery,
            "memo_get": lambda name: self.storage.get_memo(operation.id, name),
            "memo_set": lambda name, value: self.storage.memo(operation.id, name, value),
        }

    async def settle_operation(self, handle: OperationHandle, result: ToolResult, is_error: bool) -> dict[str, Any]:
        await self.failpoint.hit("after_effect_returned_before_outcome_commit")
        operation, message_id = handle.operation, uuid.uuid4().hex
        metadata = {"durable_message_id": message_id, "durable_operation_id": operation.id,
                    "durable_batch_id": operation.batch_id, "durable_source_index": operation.source_index,
                    "durable_recovery": handle.is_recovery}
        message = AgentMessage.tool_result(
            tool_call_id=operation.tool_call_id, name=operation.tool_name, content=result.content,
            is_error=is_error, details=result.details, added_tool_names=result.added_tool_names,
            terminate=result.terminate, aborted=False, usage=result.usage, **metadata)
        now = utc_now_iso()
        settled = replace(operation, status="outcome_ready", result=agent_message_to_dict(message),
                          is_error=is_error, terminate=result.terminate, updated_at=now)
        outbox = OutboxRecord(uuid.uuid4().hex, operation.id, operation.batch_id, operation.source_index,
                              message_id, agent_message_to_dict(message), "pending", now, None)
        await self.storage.settle_operation(settled, outbox)
        await self.failpoint.hit("after_outcome_and_outbox_committed")
        return metadata

    async def acknowledge_published(self, operation_id: str) -> None:
        await self.storage.mark_outbox_published(operation_id, utc_now_iso())
