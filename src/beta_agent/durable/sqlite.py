from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .records import OutboxRecord, StoredError, TaskOutcome, TaskRecord, ToolOperationRecord
from .serialization import JsonValue, canonical_json, validate_json

T = TypeVar("T")
SCHEMA_VERSION = 1


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._poisoned = False

    async def open(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = self._open_sync()

    def _open_sync(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000", "synchronous=FULL"):
            connection.execute(f"PRAGMA {pragma}")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            connection.close()
            raise ValueError(f"Unsupported durable schema version: {version}")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS tasks_status_kind ON tasks(status, kind);
        CREATE TABLE IF NOT EXISTS tool_operations (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL, batch_id TEXT NOT NULL, source_index INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS operations_status_order ON tool_operations(status, batch_id, source_index);
        CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE REFERENCES tool_operations(id), durable_message_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, batch_id TEXT NOT NULL, source_index INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS outbox_status_order ON outbox(status, batch_id, source_index);
        CREATE TABLE IF NOT EXISTS task_memos (operation_id TEXT NOT NULL REFERENCES tool_operations(id), name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(operation_id, name));
        PRAGMA user_version=1;
        """)
        connection.commit()
        return connection

    def _check(self, mutation: bool = False) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Durable storage is closed")
        if mutation and self._poisoned:
            raise RuntimeError("Durable storage is poisoned; close and reopen it")
        return self._connection

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    async def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            return fn(self._check())

    async def _write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            connection = self._check(True)
            def transaction() -> T:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(connection)
                    connection.commit()
                    return result
                except BaseException:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        self._poisoned = True
                    raise
            try:
                return transaction()
            except sqlite3.OperationalError as exc:
                if "disk" in str(exc).lower() or "i/o" in str(exc).lower():
                    self._poisoned = True
                raise

    async def create_task(self, record: TaskRecord) -> None:
        _validate_task(record)
        await self._write(lambda c: c.execute("INSERT INTO tasks VALUES(?,?,?,?)", (record.id, record.status, record.kind, _dump(record))))

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = await self._read(lambda c: c.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone())
        return _task(_load(row[0])) if row else None

    async def replace_task(self, record: TaskRecord) -> None:
        _validate_task(record)
        def op(c):
            if c.execute("UPDATE tasks SET status=?,kind=?,data=? WHERE id=?", (record.status, record.kind, _dump(record), record.id)).rowcount != 1:
                raise KeyError(record.id)
        await self._write(op)

    async def scan_tasks(self, *, status: str | None = None, kind: str | None = None) -> list[TaskRecord]:
        clauses, values = [], []
        if status is not None: clauses.append("status=?"); values.append(status)
        if kind is not None: clauses.append("kind=?"); values.append(kind)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._read(lambda c: c.execute("SELECT data FROM tasks" + where + " ORDER BY id", values).fetchall())
        return [_task(_load(row[0])) for row in rows]

    async def create_tool_batch_intents(self, records: Sequence[ToolOperationRecord]) -> None:
        for record in records:
            validate_json(record.arguments)
            if record.status != "effect_pending": raise ValueError("New operation must be effect_pending")
        await self._write(lambda c: c.executemany("INSERT INTO tool_operations VALUES(?,?,?,?,?,?)", [(r.id, r.task_id, r.status, r.batch_id, r.source_index, _dump(r)) for r in records]))

    async def get_operation(self, operation_id: str) -> ToolOperationRecord | None:
        row = await self._read(lambda c: c.execute("SELECT data FROM tool_operations WHERE id=?", (operation_id,)).fetchone())
        return ToolOperationRecord(**_load(row[0])) if row else None

    async def settle_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord) -> None:
        validate_json(operation.result); validate_json(outbox.message)
        if operation.status != "outcome_ready" or outbox.status != "pending": raise ValueError("Invalid settlement state")
        def op(c):
            if c.execute("UPDATE tool_operations SET status=?,data=? WHERE id=? AND status='effect_pending'", (operation.status, _dump(operation), operation.id)).rowcount != 1:
                raise ValueError("Operation is not effect_pending")
            c.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?)", (outbox.id, outbox.operation_id, outbox.durable_message_id, outbox.status, outbox.batch_id, outbox.source_index, _dump(outbox)))
        await self._write(op)

    async def scan_effect_pending_operations(self) -> list[ToolOperationRecord]:
        rows = await self._read(lambda c: c.execute("SELECT data FROM tool_operations WHERE status='effect_pending' ORDER BY batch_id,source_index").fetchall())
        return [ToolOperationRecord(**_load(row[0])) for row in rows]

    async def scan_pending_outbox(self) -> list[OutboxRecord]:
        rows = await self._read(lambda c: c.execute("SELECT data FROM outbox WHERE status='pending' ORDER BY batch_id,source_index").fetchall())
        return [OutboxRecord(**_load(row[0])) for row in rows]

    async def mark_outbox_published(self, operation_id: str, published_at: str) -> None:
        def op(c):
            row = c.execute("SELECT data FROM tool_operations WHERE id=?", (operation_id,)).fetchone()
            boxrow = c.execute("SELECT id,data FROM outbox WHERE operation_id=?", (operation_id,)).fetchone()
            if not row or not boxrow: raise KeyError(operation_id)
            operation = replace(ToolOperationRecord(**_load(row[0])), status="completed", updated_at=published_at)
            box = replace(OutboxRecord(**_load(boxrow[1])), status="published", published_at=published_at)
            c.execute("UPDATE tool_operations SET status='completed',data=? WHERE id=?", (_dump(operation), operation_id))
            c.execute("UPDATE outbox SET status='published',data=? WHERE id=?", (_dump(box), boxrow[0]))
            c.execute("DELETE FROM task_memos WHERE operation_id=?", (operation_id,))
        await self._write(op)

    async def get_memo(self, operation_id: str, name: str) -> JsonValue | None:
        row = await self._read(lambda c: c.execute("SELECT value FROM task_memos WHERE operation_id=? AND name=?", (operation_id, name)).fetchone())
        return _load(row[0]) if row else None

    async def memo(self, operation_id: str, name: str, candidate: JsonValue) -> JsonValue:
        encoded = canonical_json(candidate)
        def op(c):
            c.execute("INSERT OR IGNORE INTO task_memos VALUES(?,?,?)", (operation_id, name, encoded))
            return _load(c.execute("SELECT value FROM task_memos WHERE operation_id=? AND name=?", (operation_id, name)).fetchone()[0])
        return await self._write(op)

    async def request_abort(self, task_id: str) -> None:
        task = await self.get_task(task_id)
        if task is None: raise KeyError(task_id)
        await self.replace_task(replace(task, abort_requested=True))

    async def terminalize_task(self, task_id: str, outcome: TaskOutcome, updated_at: str) -> None:
        task = await self.get_task(task_id)
        if task is None: raise KeyError(task_id)
        terminal = replace(task, status="terminal", checkpoint=None, outcome=outcome, updated_at=updated_at)
        def op(c):
            c.execute("UPDATE tasks SET status='terminal',data=? WHERE id=?", (_dump(terminal), task_id))
            c.execute("DELETE FROM task_memos WHERE operation_id IN (SELECT id FROM tool_operations WHERE task_id=?)", (task_id,))
        await self._write(op)


def _dump(value: Any) -> str: return canonical_json(asdict(value))
def _load(value: str) -> Any: return json.loads(value)
def _task(data: dict[str, Any]) -> TaskRecord:
    outcome = data.get("outcome")
    if outcome:
        error = outcome.get("error")
        outcome = TaskOutcome(**{**outcome, "error": StoredError(**error) if error else None})
    return TaskRecord(**{**data, "outcome": outcome})
def _validate_task(record: TaskRecord) -> None:
    validate_json(record.input); validate_json(record.checkpoint)
    if record.status == "terminal" and (record.outcome is None or record.checkpoint is not None): raise ValueError("Terminal task requires outcome and no checkpoint")
    if record.status != "terminal" and record.checkpoint is None: raise ValueError("Live task requires checkpoint")
