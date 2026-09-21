from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence, TypeAlias, Union


class DurableRuntimeError(Exception):
    pass


class DurableStateConflict(DurableRuntimeError):
    pass


class UnsupportedDurableVersion(DurableRuntimeError):
    pass


JsonValue: TypeAlias = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not JSON-safe: {exc}") from exc


def validate_json(value: Any) -> JsonValue:
    return json.loads(canonical_json(value))


def arguments_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

ReplayPolicy = Literal["safe", "unsafe"]
TaskStatus = Literal["pending", "running", "terminal"]
TaskOutcomeStatus = Literal["completed", "failed", "aborted", "orphaned", "faulted"]
ToolOperationStatus = Literal["planned", "effect_pending", "outcome_ready", "completed"]
OutboxStatus = Literal["pending", "published"]
OutboxOwnerKind = Literal["input", "prepared_input", "assistant", "tool", "recovery"]


@dataclass(frozen=True, slots=True)
class StoredError:
    message: str
    detail: JsonValue | None = None


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    status: TaskOutcomeStatus
    result: JsonValue | None = None
    error: StoredError | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    session_id: str
    kind: str
    version: int
    status: TaskStatus
    input: JsonValue
    checkpoint: JsonValue | None
    outcome: TaskOutcome | None
    abort_requested: bool
    background: bool
    created_at: str
    updated_at: str
    revision: int = 0


@dataclass(frozen=True, slots=True, init=False)
class ToolOperationRecord:
    id: str
    task_id: str
    run_id: str
    batch_id: str
    source_index: int
    tool_call_id: str
    tool_name: str
    source_arguments: JsonValue
    arguments: JsonValue | None
    arguments_hash: str | None
    replay_policy: ReplayPolicy | None
    result_message_id: str
    status: ToolOperationStatus
    attempt: int
    result: JsonValue | None
    is_error: bool | None
    terminate: bool | None
    created_at: str
    updated_at: str

    def __init__(self, id: str, task_id: str, run_id: str, batch_id: str, source_index: int,
                 tool_call_id: str, tool_name: str, *args, **kwargs) -> None:
        # v1 positional compatibility: arguments, hash, policy, status, attempt,
        # result, is_error, terminate, created_at, updated_at.
        if args and len(args) == 10 and not kwargs:
            arguments, arg_hash, policy, status, attempt, result, is_error, terminate, created_at, updated_at = args
            values = (arguments, arguments, arg_hash, policy, id, status, attempt, result,
                      is_error, terminate, created_at, updated_at)
        else:
            names = ("source_arguments", "arguments", "arguments_hash", "replay_policy",
                     "result_message_id", "status", "attempt", "result", "is_error",
                     "terminate", "created_at", "updated_at")
            supplied = dict(zip(names, args)); supplied.update(kwargs)
            values = tuple(supplied[name] for name in names)
        for name, value in zip(("id", "task_id", "run_id", "batch_id", "source_index",
                                "tool_call_id", "tool_name"),
                               (id, task_id, run_id, batch_id, source_index, tool_call_id, tool_name)):
            object.__setattr__(self, name, value)
        for name, value in zip(("source_arguments", "arguments", "arguments_hash", "replay_policy",
                                "result_message_id", "status", "attempt", "result", "is_error",
                                "terminate", "created_at", "updated_at"), values):
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True, init=False)
class OutboxRecord:
    id: str
    task_id: str
    owner_kind: OutboxOwnerKind
    owner_id: str
    group_id: str
    turn_index: int
    source_index: int
    durable_message_id: str
    message: JsonValue
    status: OutboxStatus
    created_at: str
    published_at: str | None

    def __init__(self, id: str, *args, **kwargs) -> None:
        # v1 positional compatibility: operation_id, batch_id, source_index,
        # durable_message_id, message, status, created_at, published_at.
        if len(args) == 8 and not kwargs:
            operation_id, batch_id, source_index, message_id, message, status, created_at, published_at = args
            values = ("", "tool", operation_id, batch_id, 0, source_index, message_id,
                      message, status, created_at, published_at)
        else:
            names = ("task_id", "owner_kind", "owner_id", "group_id", "turn_index",
                     "source_index", "durable_message_id", "message", "status", "created_at",
                     "published_at")
            supplied = dict(zip(names, args)); supplied.update(kwargs)
            values = tuple(supplied[name] for name in names)
        object.__setattr__(self, "id", id)
        for name, value in zip(("task_id", "owner_kind", "owner_id", "group_id", "turn_index",
                                "source_index", "durable_message_id", "message", "status",
                                "created_at", "published_at"), values):
            object.__setattr__(self, name, value)

    @property
    def operation_id(self) -> str:
        return self.owner_id

    @property
    def batch_id(self) -> str:
        return self.group_id

class DurableStorage(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def create_task(self, record: TaskRecord) -> None: ...
    async def accept_task(self, task: TaskRecord, outbox: Sequence[OutboxRecord]) -> None: ...
    async def get_task(self, task_id: str) -> TaskRecord | None: ...
    async def get_open_task(self, session_id: str) -> TaskRecord | None: ...
    async def replace_task(self, record: TaskRecord) -> None: ...
    async def replace_task_cas(self, record: TaskRecord, *, expected_revision: int) -> None: ...
    async def replace_task_with_outbox_cas(self, record: TaskRecord, outbox: Sequence[OutboxRecord], *, expected_revision: int) -> None: ...
    async def settle_assistant_with_tool_plan(self, task: TaskRecord, operations: Sequence[ToolOperationRecord], outbox: Sequence[OutboxRecord], *, expected_revision: int) -> None: ...
    async def scan_tasks(self, *, status: str | None = None, kind: str | None = None) -> list[TaskRecord]: ...
    async def create_tool_batch_intents(self, records: Sequence[ToolOperationRecord]) -> None: ...
    async def get_operation(self, operation_id: str) -> ToolOperationRecord | None: ...
    async def get_operations(self, ids: Sequence[str]) -> list[ToolOperationRecord]: ...
    async def create_or_replace_tool_effect_intent(self, operation: ToolOperationRecord, *, expected_status: str = "planned") -> None: ...
    async def settle_tool_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord, *, expected_status: str) -> None: ...
    async def settle_operation(self, operation: ToolOperationRecord, outbox: OutboxRecord) -> None: ...
    async def scan_effect_pending_operations(self) -> list[ToolOperationRecord]: ...
    async def scan_pending_outbox(self, *, task_id: str | None = None, group_id: str | None = None) -> list[OutboxRecord]: ...
    async def mark_outbox_published(self, outbox_id: str, published_at: str) -> None: ...
    async def get_memo(self, operation_id: str, name: str) -> JsonValue | None: ...
    async def memo(self, operation_id: str, name: str, candidate: JsonValue) -> JsonValue: ...
    async def request_abort(self, task_id: str) -> TaskRecord: ...
    async def terminalize_task(self, task_id: str, outcome: TaskOutcome, updated_at: str) -> None: ...

def _check_cas(current: TaskRecord, replacement: TaskRecord, expected: int) -> None:
    if current.revision != expected or replacement.revision != expected + 1: raise DurableStateConflict("Task revision conflict")
    if current.abort_requested and not replacement.abort_requested: raise DurableStateConflict("abort_requested is monotonic")


def _validate_task(record: TaskRecord) -> None:
    validate_json(record.input); validate_json(record.checkpoint)
    if record.revision < 0: raise ValueError("revision must be non-negative")
    if record.status == "terminal":
        if record.outcome is None or record.checkpoint is not None: raise ValueError("Terminal task requires outcome and no checkpoint")
    elif record.checkpoint is None: raise ValueError("Live task requires checkpoint")


def _validate_operation(record: ToolOperationRecord) -> None:
    validate_json(record.source_arguments); validate_json(record.arguments); validate_json(record.result)
    if record.status == "planned" and any(v is not None for v in (record.arguments, record.arguments_hash, record.replay_policy, record.result)): raise ValueError("Planned operation cannot contain effect data")
    if record.status == "effect_pending" and (record.arguments is None or record.arguments_hash is None or record.replay_policy is None or record.result is not None): raise ValueError("Invalid effect_pending operation")
    if record.status in ("outcome_ready", "completed") and (record.result is None or record.is_error is None or record.terminate is None): raise ValueError("Settled operation requires a result")
