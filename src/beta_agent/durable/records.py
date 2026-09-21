from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .serialization import JsonValue

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
