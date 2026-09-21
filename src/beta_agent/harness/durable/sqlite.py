from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .types import (
    DurableStateConflict,
    UnsupportedDurableVersion,
    JsonValue,
    OutboxRecord,
    StoredError,
    TaskOutcome,
    TaskRecord,
    ToolOperationRecord,
    _check_cas,
    _validate_operation,
    _validate_task,
    canonical_json,
    validate_json,
)


T = TypeVar("T")
SCHEMA_VERSION = 2


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path); self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock(); self._poisoned = False

    async def open(self) -> None:
        async with self._lock:
            if self._connection is not None: return
            if str(self.path) != ":memory:": self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = self._open_sync()

    def _open_sync(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False)
        for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000", "synchronous=FULL"): c.execute(f"PRAGMA {pragma}")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2): c.close(); raise UnsupportedDurableVersion(str(version))
        if version == 1: self._migrate_v1(c)
        self._create_v2(c); c.commit(); return c

    @staticmethod
    def _create_v2(c: sqlite3.Connection) -> None:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,status TEXT NOT NULL,kind TEXT NOT NULL,session_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS tasks_status_kind ON tasks(status,kind);
        CREATE INDEX IF NOT EXISTS tasks_session_status ON tasks(session_id,status);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_agent_run ON tasks(session_id) WHERE status!='terminal' AND kind='agent_run';
        CREATE TABLE IF NOT EXISTS tool_operations (id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),status TEXT NOT NULL,batch_id TEXT NOT NULL,source_index INTEGER NOT NULL,result_message_id TEXT NOT NULL UNIQUE,data TEXT NOT NULL,UNIQUE(batch_id,source_index));
        CREATE INDEX IF NOT EXISTS operations_task ON tool_operations(task_id);
        CREATE INDEX IF NOT EXISTS operations_status ON tool_operations(status);
        CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),owner_kind TEXT NOT NULL,owner_id TEXT NOT NULL,group_id TEXT NOT NULL,turn_index INTEGER NOT NULL,source_index INTEGER NOT NULL,durable_message_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS outbox_status_task ON outbox(status,task_id);
        CREATE INDEX IF NOT EXISTS outbox_group_order ON outbox(task_id,group_id,source_index);
        CREATE TABLE IF NOT EXISTS task_memos (operation_id TEXT NOT NULL REFERENCES tool_operations(id),name TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(operation_id,name));
        PRAGMA user_version=2;
        """)

    def _migrate_v1(self, c: sqlite3.Connection) -> None:
        tasks = c.execute("SELECT data FROM tasks").fetchall()
        operations = c.execute("SELECT data FROM tool_operations").fetchall()
        boxes = c.execute("SELECT data FROM outbox").fetchall()
        memos = c.execute("SELECT operation_id,name,value FROM task_memos").fetchall()
        c.executescript("DROP TABLE task_memos; DROP TABLE outbox; DROP TABLE tool_operations; DROP TABLE tasks;")
        self._create_v2(c)
        now_tasks: dict[str, TaskRecord] = {}
        for row in tasks:
            task = _task(_load(row[0]))
            if task.kind == "agent_run" and task.status != "terminal":
                task = replace(task, status="terminal", checkpoint=None,
                    outcome=TaskOutcome("orphaned", reason="legacy_v1_checkpoint_not_resumable"))
            now_tasks[task.id] = task
            c.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (task.id,task.status,task.kind,task.session_id,_dump(task)))
        old_boxes = {_load(row[0])["operation_id"]: _load(row[0]) for row in boxes}
        for row in operations:
            data = _load(row[0]); message_id = old_boxes.get(data["id"], {}).get("durable_message_id", uuid.uuid4().hex)
            op = ToolOperationRecord(data["id"],data["task_id"],data["run_id"],data["batch_id"],data["source_index"],data["tool_call_id"],data["tool_name"],
                source_arguments=data["arguments"],arguments=data["arguments"],arguments_hash=data["arguments_hash"],replay_policy=data["replay_policy"],result_message_id=message_id,
                status=data["status"],attempt=data["attempt"],result=data.get("result"),is_error=data.get("is_error"),terminate=data.get("terminate"),created_at=data["created_at"],updated_at=data["updated_at"])
            c.execute("INSERT INTO tool_operations VALUES(?,?,?,?,?,?,?)", (op.id,op.task_id,op.status,op.batch_id,op.source_index,op.result_message_id,_dump(op)))
        for row in boxes:
            data = _load(row[0]); op_id=data["operation_id"]
            op_data=next((_load(x[0]) for x in operations if _load(x[0])["id"]==op_id), None)
            if op_data is None: continue
            box=OutboxRecord(data["id"],op_data["task_id"],"tool",op_id,data["batch_id"],0,data["source_index"],data["durable_message_id"],data["message"],data["status"],data["created_at"],data.get("published_at"))
            self._insert_box(c,box)
        for operation_id,name,value in memos:
            c.execute("INSERT OR IGNORE INTO task_memos VALUES(?,?,?)",(operation_id,name,value))

    def _check(self, mutation: bool = False) -> sqlite3.Connection:
        if self._connection is None: raise RuntimeError("Durable storage is closed")
        if mutation and self._poisoned: raise RuntimeError("Durable storage is poisoned; close and reopen it")
        return self._connection
    async def close(self) -> None:
        async with self._lock:
            c,self._connection=self._connection,None
            if c is not None: c.close()
    async def _read(self, fn: Callable[[sqlite3.Connection],T]) -> T:
        async with self._lock: return fn(self._check())
    async def _write(self, fn: Callable[[sqlite3.Connection],T]) -> T:
        async with self._lock:
            c=self._check(True); c.execute("BEGIN IMMEDIATE")
            try: result=fn(c); c.commit(); return result
            except BaseException:
                try: c.rollback()
                except sqlite3.Error: self._poisoned=True
                raise

    async def accept_task(self, task: TaskRecord, outbox: Sequence[OutboxRecord]) -> None:
        _validate_task(task)
        def op(c):
            c.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",(task.id,task.status,task.kind,task.session_id,_dump(task)))
            for box in outbox: self._insert_box(c,box)
        try: await self._write(op)
        except sqlite3.IntegrityError as exc: raise DurableStateConflict(str(exc)) from exc
    async def create_task(self, record: TaskRecord) -> None: await self.accept_task(record,())
    async def get_task(self, task_id: str) -> TaskRecord | None:
        row=await self._read(lambda c:c.execute("SELECT data FROM tasks WHERE id=?",(task_id,)).fetchone()); return _task(_load(row[0])) if row else None
    async def get_open_task(self, session_id: str) -> TaskRecord | None:
        row=await self._read(lambda c:c.execute("SELECT data FROM tasks WHERE session_id=? AND kind='agent_run' AND status!='terminal'",(session_id,)).fetchone()); return _task(_load(row[0])) if row else None
    async def scan_tasks(self,*,status:str|None=None,kind:str|None=None)->list[TaskRecord]:
        clauses=[]; values=[]
        if status is not None: clauses.append("status=?"); values.append(status)
        if kind is not None: clauses.append("kind=?"); values.append(kind)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        rows=await self._read(lambda c:c.execute("SELECT data FROM tasks"+where+" ORDER BY id",values).fetchall()); return [_task(_load(r[0])) for r in rows]
    async def replace_task_cas(self,record:TaskRecord,*,expected_revision:int)->None: await self.replace_task_with_outbox_cas(record,(),expected_revision=expected_revision)
    async def replace_task_with_outbox_cas(self,record:TaskRecord,outbox:Sequence[OutboxRecord],*,expected_revision:int)->None:
        _validate_task(record)
        def op(c):
            row=c.execute("SELECT data FROM tasks WHERE id=?",(record.id,)).fetchone()
            if not row: raise KeyError(record.id)
            _check_cas(_task(_load(row[0])),record,expected_revision)
            c.execute("UPDATE tasks SET status=?,kind=?,session_id=?,data=? WHERE id=?",(record.status,record.kind,record.session_id,_dump(record),record.id))
            for box in outbox:self._insert_box(c,box)
        await self._write(op)
    async def replace_task(self,record:TaskRecord)->None:
        current=await self.get_task(record.id)
        if current is None: raise KeyError(record.id)
        if record.revision==current.revision: record=replace(record,revision=current.revision+1)
        await self.replace_task_cas(record,expected_revision=current.revision)
    async def settle_assistant_with_tool_plan(self,task:TaskRecord,operations:Sequence[ToolOperationRecord],outbox:Sequence[OutboxRecord],*,expected_revision:int)->None:
        def op(c):
            row=c.execute("SELECT data FROM tasks WHERE id=?",(task.id,)).fetchone()
            if not row: raise KeyError(task.id)
            _check_cas(_task(_load(row[0])),task,expected_revision)
            c.execute("UPDATE tasks SET status=?,data=? WHERE id=?",(task.status,_dump(task),task.id))
            for item in operations:self._insert_operation(c,item)
            for box in outbox:self._insert_box(c,box)
        await self._write(op)
    async def create_tool_batch_intents(self,records:Sequence[ToolOperationRecord])->None: await self._write(lambda c:[self._insert_operation(c,r) for r in records])
    async def create_or_replace_tool_effect_intent(self,operation:ToolOperationRecord,*,expected_status:str="planned")->None:
        _validate_operation(operation)
        def op(c):
            if c.execute("UPDATE tool_operations SET status=?,data=? WHERE id=? AND status=?",(operation.status,_dump(operation),operation.id,expected_status)).rowcount!=1: raise DurableStateConflict("Invalid tool intent transition")
        await self._write(op)
    async def get_operation(self,operation_id:str)->ToolOperationRecord|None:
        row=await self._read(lambda c:c.execute("SELECT data FROM tool_operations WHERE id=?",(operation_id,)).fetchone()); return _operation(_load(row[0])) if row else None
    async def get_operations(self,ids:Sequence[str])->list[ToolOperationRecord]:
        result=[]
        for item in ids:
            op=await self.get_operation(item)
            if op is None: raise KeyError(item)
            result.append(op)
        return result
    async def settle_tool_operation(self,operation:ToolOperationRecord,outbox:OutboxRecord,*,expected_status:str)->None:
        def op(c):
            if c.execute("UPDATE tool_operations SET status=?,data=? WHERE id=? AND status=?",(operation.status,_dump(operation),operation.id,expected_status)).rowcount!=1: raise DurableStateConflict("Invalid tool settlement transition")
            self._insert_box(c,outbox)
        await self._write(op)
    async def settle_operation(self,operation:ToolOperationRecord,outbox:OutboxRecord)->None:
        current=await self.get_operation(operation.id)
        if current is None: raise KeyError(operation.id)
        if not outbox.task_id: object.__setattr__(outbox,"task_id",operation.task_id)
        await self.settle_tool_operation(operation,outbox,expected_status=current.status)
    async def scan_effect_pending_operations(self)->list[ToolOperationRecord]:
        rows=await self._read(lambda c:c.execute("SELECT data FROM tool_operations WHERE status='effect_pending' ORDER BY batch_id,source_index").fetchall()); return [_operation(_load(r[0])) for r in rows]
    async def scan_pending_outbox(self,*,task_id:str|None=None,group_id:str|None=None)->list[OutboxRecord]:
        clauses=["status='pending'"]; vals=[]
        if task_id is not None:clauses.append("task_id=?");vals.append(task_id)
        if group_id is not None:clauses.append("group_id=?");vals.append(group_id)
        rows=await self._read(lambda c:c.execute("SELECT data FROM outbox WHERE "+" AND ".join(clauses)+" ORDER BY task_id,group_id,source_index,id",vals).fetchall()); return [_outbox(_load(r[0])) for r in rows]
    async def mark_outbox_published(self,outbox_id:str,published_at:str)->None:
        def op(c):
            row=c.execute("SELECT id,data FROM outbox WHERE id=?",(outbox_id,)).fetchone() or c.execute("SELECT id,data FROM outbox WHERE owner_kind='tool' AND owner_id=?",(outbox_id,)).fetchone()
            if not row: raise KeyError(outbox_id)
            box=replace(_outbox(_load(row[1])),status="published",published_at=published_at)
            c.execute("UPDATE outbox SET status='published',data=? WHERE id=?",(_dump(box),row[0]))
            if box.owner_kind=="tool":
                op_data=_operation(_load(c.execute("SELECT data FROM tool_operations WHERE id=?",(box.owner_id,)).fetchone()[0])); updated=replace(op_data,status="completed",updated_at=published_at)
                c.execute("UPDATE tool_operations SET status='completed',data=? WHERE id=?",(_dump(updated),updated.id)); c.execute("DELETE FROM task_memos WHERE operation_id=?",(updated.id,))
        await self._write(op)
    async def get_memo(self,operation_id:str,name:str)->JsonValue|None:
        row=await self._read(lambda c:c.execute("SELECT value FROM task_memos WHERE operation_id=? AND name=?",(operation_id,name)).fetchone()); return _load(row[0]) if row else None
    async def memo(self,operation_id:str,name:str,candidate:JsonValue)->JsonValue:
        encoded=canonical_json(candidate)
        def op(c):c.execute("INSERT OR IGNORE INTO task_memos VALUES(?,?,?)",(operation_id,name,encoded));return _load(c.execute("SELECT value FROM task_memos WHERE operation_id=? AND name=?",(operation_id,name)).fetchone()[0])
        return await self._write(op)
    async def request_abort(self,task_id:str)->TaskRecord:
        task=await self.get_task(task_id)
        if task is None:raise KeyError(task_id)
        if task.status=="terminal" or task.abort_requested:return task
        updated=replace(task,abort_requested=True,revision=task.revision+1);await self.replace_task_cas(updated,expected_revision=task.revision);return updated
    async def terminalize_task(self,task_id:str,outcome:TaskOutcome,updated_at:str)->None:
        task=await self.get_task(task_id)
        if task is None:raise KeyError(task_id)
        terminal=replace(task,status="terminal",checkpoint=None,outcome=outcome,updated_at=updated_at,revision=task.revision+1)
        await self.replace_task_cas(terminal,expected_revision=task.revision)
        await self._write(lambda c:c.execute("DELETE FROM task_memos WHERE operation_id IN (SELECT id FROM tool_operations WHERE task_id=?)",(task_id,)))
    @staticmethod
    def _insert_operation(c,op):
        _validate_operation(op);c.execute("INSERT INTO tool_operations VALUES(?,?,?,?,?,?,?)",(op.id,op.task_id,op.status,op.batch_id,op.source_index,op.result_message_id,_dump(op)))
    @staticmethod
    def _insert_box(c,box):
        validate_json(box.message);c.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)",(box.id,box.task_id,box.owner_kind,box.owner_id,box.group_id,box.turn_index,box.source_index,box.durable_message_id,box.status,_dump(box)))


def _dump(value:Any)->str:return canonical_json(asdict(value))
def _load(value:str)->Any:return json.loads(value)
def _task(data:dict[str,Any])->TaskRecord:
    outcome=data.get("outcome")
    if outcome:
        error=outcome.get("error");outcome=TaskOutcome(**{**outcome,"error":StoredError(**error) if error else None})
    return TaskRecord(**{**data,"outcome":outcome,"revision":data.get("revision",0)})
def _operation(data:dict[str,Any])->ToolOperationRecord:return ToolOperationRecord(**data)
def _outbox(data:dict[str,Any])->OutboxRecord:return OutboxRecord(**data)
