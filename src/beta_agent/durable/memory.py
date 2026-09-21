from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Sequence

from .types import (
    DurableStateConflict,
    JsonValue,
    OutboxRecord,
    TaskOutcome,
    TaskRecord,
    ToolOperationRecord,
    _check_cas,
    _validate_operation,
    _validate_task,
    validate_json,
)



class MemoryStorage:
    def __init__(self) -> None:
        self._open = False
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._operations: dict[str, ToolOperationRecord] = {}
        self._outbox: dict[str, OutboxRecord] = {}
        self._memos: dict[tuple[str, str], JsonValue] = {}

    async def open(self) -> None: self._open = True
    def _check(self) -> None:
        if not self._open: raise RuntimeError("Durable storage is closed")
    async def close(self) -> None:
        async with self._lock: self._open = False

    async def accept_task(self, task: TaskRecord, outbox: Sequence[OutboxRecord]) -> None:
        async with self._lock:
            self._check(); _validate_task(task)
            if task.id in self._tasks: raise ValueError(f"Duplicate task id: {task.id}")
            if any(t.session_id == task.session_id and t.kind == "agent_run" and t.status != "terminal" for t in self._tasks.values()):
                raise DurableStateConflict(f"Session already has an open agent_run: {task.session_id}")
            self._validate_new_outbox(outbox)
            self._tasks[task.id] = copy.deepcopy(task)
            self._outbox.update({box.id: copy.deepcopy(box) for box in outbox})

    async def create_task(self, record: TaskRecord) -> None: await self.accept_task(record, ())
    async def get_task(self, task_id: str) -> TaskRecord | None:
        self._check(); return copy.deepcopy(self._tasks.get(task_id))
    async def get_open_task(self, session_id: str) -> TaskRecord | None:
        self._check()
        found = [t for t in self._tasks.values() if t.session_id == session_id and t.kind == "agent_run" and t.status != "terminal"]
        if len(found) > 1: raise DurableStateConflict("Multiple open tasks")
        return copy.deepcopy(found[0]) if found else None
    async def scan_tasks(self, *, status: str | None = None, kind: str | None = None) -> list[TaskRecord]:
        self._check(); return copy.deepcopy(sorted((r for r in self._tasks.values() if (status is None or r.status == status) and (kind is None or r.kind == kind)), key=lambda r: (r.created_at, r.id)))

    async def replace_task_cas(self, record: TaskRecord, *, expected_revision: int) -> None:
        await self.replace_task_with_outbox_cas(record, (), expected_revision=expected_revision)
    async def replace_task_with_outbox_cas(self, record: TaskRecord, outbox: Sequence[OutboxRecord], *, expected_revision: int) -> None:
        async with self._lock:
            self._check(); _validate_task(record)
            current = self._tasks.get(record.id)
            if current is None: raise KeyError(record.id)
            _check_cas(current, record, expected_revision); self._validate_new_outbox(outbox)
            self._tasks[record.id] = copy.deepcopy(record)
            self._outbox.update({box.id: copy.deepcopy(box) for box in outbox})
    async def replace_task(self, record: TaskRecord) -> None:
        current = await self.get_task(record.id)
        if current is None: raise KeyError(record.id)
        if record.revision == current.revision: record = replace(record, revision=current.revision + 1)
        await self.replace_task_cas(record, expected_revision=current.revision)

    async def settle_assistant_with_tool_plan(self, task: TaskRecord, operations: Sequence[ToolOperationRecord], outbox: Sequence[OutboxRecord], *, expected_revision: int) -> None:
        async with self._lock:
            self._check(); _validate_task(task)
            current = self._tasks.get(task.id)
            if current is None: raise KeyError(task.id)
            _check_cas(current, task, expected_revision); self._validate_new_operations(operations, planned=True); self._validate_new_outbox(outbox)
            self._tasks[task.id] = copy.deepcopy(task)
            self._operations.update({op.id: copy.deepcopy(op) for op in operations})
            self._outbox.update({box.id: copy.deepcopy(box) for box in outbox})

    async def create_tool_batch_intents(self, records: Sequence[ToolOperationRecord]) -> None:
        async with self._lock:
            self._check(); self._validate_new_operations(records, planned=False)
            self._operations.update({r.id: copy.deepcopy(r) for r in records})
    async def create_or_replace_tool_effect_intent(self, operation: ToolOperationRecord, *, expected_status: str = "planned") -> None:
        async with self._lock:
            self._check(); current = self._operations.get(operation.id)
            if current is None: raise KeyError(operation.id)
            if current.status != expected_status or operation.status != "effect_pending": raise DurableStateConflict("Invalid tool intent transition")
            _validate_operation(operation); self._operations[operation.id] = copy.deepcopy(operation)
    async def get_operation(self, operation_id: str) -> ToolOperationRecord | None:
        self._check(); return copy.deepcopy(self._operations.get(operation_id))
    async def get_operations(self, ids: Sequence[str]) -> list[ToolOperationRecord]:
        self._check()
        try: return copy.deepcopy([self._operations[item] for item in ids])
        except KeyError as exc: raise KeyError(f"Missing tool operation: {exc.args[0]}") from exc
    async def settle_tool_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord, *, expected_status: str) -> None:
        async with self._lock:
            self._check(); current = self._operations.get(operation.id)
            if current is None: raise KeyError(operation.id)
            if current.status != expected_status or operation.status != "outcome_ready": raise DurableStateConflict("Invalid tool settlement transition")
            _validate_operation(operation); self._validate_new_outbox((outbox,))
            self._operations[operation.id] = copy.deepcopy(operation); self._outbox[outbox.id] = copy.deepcopy(outbox)
    async def settle_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord) -> None:
        current = await self.get_operation(operation.id)
        if current is None: raise KeyError(operation.id)
        if not outbox.task_id: object.__setattr__(outbox, "task_id", operation.task_id)
        await self.settle_tool_operation(operation, outbox, expected_status=current.status)
    async def scan_effect_pending_operations(self) -> list[ToolOperationRecord]:
        self._check(); return copy.deepcopy(sorted((r for r in self._operations.values() if r.status == "effect_pending"), key=lambda r: (r.batch_id, r.source_index)))

    async def scan_pending_outbox(self, *, task_id: str | None = None, group_id: str | None = None) -> list[OutboxRecord]:
        self._check(); return copy.deepcopy(sorted((r for r in self._outbox.values() if r.status == "pending" and (task_id is None or r.task_id == task_id) and (group_id is None or r.group_id == group_id)), key=lambda r: (r.task_id, r.group_id, r.source_index, r.created_at, r.id)))
    async def mark_outbox_published(self, outbox_id: str, published_at: str) -> None:
        async with self._lock:
            self._check(); box = self._outbox.get(outbox_id)
            if box is None: box = next((b for b in self._outbox.values() if b.owner_kind == "tool" and b.owner_id == outbox_id), None)
            if box is None: raise KeyError(outbox_id)
            self._outbox[box.id] = replace(box, status="published", published_at=published_at)
            if box.owner_kind == "tool":
                op = self._operations[box.owner_id]
                self._operations[op.id] = replace(op, status="completed", updated_at=published_at)
                self._memos = {k: v for k, v in self._memos.items() if k[0] != op.id}

    async def get_memo(self, operation_id: str, name: str) -> JsonValue | None:
        self._check(); return copy.deepcopy(self._memos.get((operation_id, name)))
    async def memo(self, operation_id: str, name: str, candidate: JsonValue) -> JsonValue:
        async with self._lock:
            self._check(); value = validate_json(candidate)
            if operation_id not in self._operations: raise KeyError(operation_id)
            return copy.deepcopy(self._memos.setdefault((operation_id, name), value))
    async def request_abort(self, task_id: str) -> TaskRecord:
        async with self._lock:
            self._check(); task = self._tasks[task_id]
            if task.status == "terminal" or task.abort_requested: return copy.deepcopy(task)
            updated = replace(task, abort_requested=True, revision=task.revision + 1)
            self._tasks[task_id] = updated; return copy.deepcopy(updated)
    async def terminalize_task(self, task_id: str, outcome: TaskOutcome, updated_at: str) -> None:
        async with self._lock:
            self._check(); task = self._tasks[task_id]
            self._tasks[task_id] = replace(task, status="terminal", checkpoint=None, outcome=copy.deepcopy(outcome), updated_at=updated_at, revision=task.revision + 1)
            ids = {o.id for o in self._operations.values() if o.task_id == task_id}
            self._memos = {k: v for k, v in self._memos.items() if k[0] not in ids}

    def _validate_new_operations(self, records: Sequence[ToolOperationRecord], *, planned: bool) -> None:
        ids = [r.id for r in records]
        if len(ids) != len(set(ids)) or any(i in self._operations for i in ids): raise ValueError("Duplicate operation id")
        if len({(r.batch_id, r.source_index) for r in records}) != len(records): raise ValueError("Duplicate batch source index")
        for record in records:
            if record.status != ("planned" if planned else "effect_pending"): raise ValueError("Invalid new operation status")
            _validate_operation(record)
    def _validate_new_outbox(self, records: Sequence[OutboxRecord]) -> None:
        ids = [r.id for r in records]; message_ids = [r.durable_message_id for r in records]
        if len(ids) != len(set(ids)) or any(i in self._outbox for i in ids): raise ValueError("Duplicate outbox id")
        if len(message_ids) != len(set(message_ids)) or any(r.durable_message_id in message_ids for r in self._outbox.values()): raise ValueError("Duplicate durable message id")
        for record in records:
            validate_json(record.message)
            if record.status != "pending": raise ValueError("New outbox must be pending")


