from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Sequence

from .records import OutboxRecord, TaskOutcome, TaskRecord, ToolOperationRecord
from .serialization import JsonValue, validate_json


class MemoryStorage:
    def __init__(self) -> None:
        self._open = False
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._operations: dict[str, ToolOperationRecord] = {}
        self._outbox: dict[str, OutboxRecord] = {}
        self._memos: dict[tuple[str, str], JsonValue] = {}

    async def open(self) -> None:
        self._open = True

    def _check(self) -> None:
        if not self._open:
            raise RuntimeError("Durable storage is closed")

    async def close(self) -> None:
        async with self._lock:
            self._open = False

    async def create_task(self, record: TaskRecord) -> None:
        async with self._lock:
            self._check(); self._validate_task(record)
            if record.id in self._tasks: raise ValueError(f"Duplicate task id: {record.id}")
            self._tasks[record.id] = copy.deepcopy(record)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        self._check(); return copy.deepcopy(self._tasks.get(task_id))

    async def replace_task(self, record: TaskRecord) -> None:
        async with self._lock:
            self._check(); self._validate_task(record)
            if record.id not in self._tasks: raise KeyError(record.id)
            self._tasks[record.id] = copy.deepcopy(record)

    async def scan_tasks(self, *, status: str | None = None, kind: str | None = None) -> list[TaskRecord]:
        self._check()
        return copy.deepcopy(sorted((r for r in self._tasks.values() if (status is None or r.status == status) and (kind is None or r.kind == kind)), key=lambda r: (r.created_at, r.id)))

    async def create_tool_batch_intents(self, records: Sequence[ToolOperationRecord]) -> None:
        async with self._lock:
            self._check()
            ids = [r.id for r in records]
            if len(ids) != len(set(ids)) or any(i in self._operations for i in ids): raise ValueError("Duplicate operation id")
            for r in records:
                validate_json(r.arguments)
                if r.status != "effect_pending": raise ValueError("New operation must be effect_pending")
            self._operations.update({r.id: copy.deepcopy(r) for r in records})

    async def get_operation(self, operation_id: str) -> ToolOperationRecord | None:
        self._check(); return copy.deepcopy(self._operations.get(operation_id))

    async def settle_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord) -> None:
        async with self._lock:
            self._check(); validate_json(operation.result); validate_json(outbox.message)
            current = self._operations.get(operation.id)
            if current is None: raise KeyError(operation.id)
            if operation.status != "outcome_ready" or outbox.status != "pending": raise ValueError("Invalid settlement state")
            if any(o.operation_id == operation.id for o in self._outbox.values()): raise ValueError("Operation already settled")
            if any(o.durable_message_id == outbox.durable_message_id for o in self._outbox.values()): raise ValueError("Duplicate durable message id")
            self._operations[operation.id] = copy.deepcopy(operation); self._outbox[outbox.id] = copy.deepcopy(outbox)

    async def scan_effect_pending_operations(self) -> list[ToolOperationRecord]:
        self._check(); return copy.deepcopy(sorted((r for r in self._operations.values() if r.status == "effect_pending"), key=lambda r: (r.batch_id, r.source_index)))

    async def scan_pending_outbox(self) -> list[OutboxRecord]:
        self._check(); return copy.deepcopy(sorted((r for r in self._outbox.values() if r.status == "pending"), key=lambda r: (r.batch_id, r.source_index)))

    async def mark_outbox_published(self, operation_id: str, published_at: str) -> None:
        async with self._lock:
            self._check()
            op = self._operations[operation_id]
            box = next((v for v in self._outbox.values() if v.operation_id == operation_id), None)
            if box is None: raise KeyError(operation_id)
            self._operations[operation_id] = replace(op, status="completed", updated_at=published_at)
            self._outbox[box.id] = replace(box, status="published", published_at=published_at)
            self._memos = {k: v for k, v in self._memos.items() if k[0] != operation_id}

    async def get_memo(self, operation_id: str, name: str) -> JsonValue | None:
        self._check(); return copy.deepcopy(self._memos.get((operation_id, name)))

    async def memo(self, operation_id: str, name: str, candidate: JsonValue) -> JsonValue:
        async with self._lock:
            self._check(); value = validate_json(candidate)
            if operation_id not in self._operations: raise KeyError(operation_id)
            return copy.deepcopy(self._memos.setdefault((operation_id, name), value))

    async def request_abort(self, task_id: str) -> None:
        async with self._lock:
            self._check(); self._tasks[task_id] = replace(self._tasks[task_id], abort_requested=True)

    async def terminalize_task(self, task_id: str, outcome: TaskOutcome, updated_at: str) -> None:
        async with self._lock:
            self._check(); task = self._tasks[task_id]
            self._tasks[task_id] = replace(task, status="terminal", checkpoint=None, outcome=copy.deepcopy(outcome), updated_at=updated_at)
            operation_ids = {o.id for o in self._operations.values() if o.task_id == task_id}
            self._memos = {k: v for k, v in self._memos.items() if k[0] not in operation_ids}

    @staticmethod
    def _validate_task(record: TaskRecord) -> None:
        validate_json(record.input); validate_json(record.checkpoint)
        if record.status == "terminal" and (record.outcome is None or record.checkpoint is not None): raise ValueError("Terminal task requires outcome and no checkpoint")
        if record.status != "terminal" and record.checkpoint is None: raise ValueError("Live task requires checkpoint")

