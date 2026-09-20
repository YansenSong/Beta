from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .serialization import JsonValue

ReplayPolicy = Literal["safe", "unsafe"]
TaskStatus = Literal["pending", "running", "terminal"]
TaskOutcomeStatus = Literal["completed", "failed", "aborted", "orphaned", "faulted"]
ToolOperationStatus = Literal["effect_pending", "outcome_ready", "completed"]
OutboxStatus = Literal["pending", "published"]


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


@dataclass(frozen=True, slots=True)
class ToolOperationRecord:
    id: str
    task_id: str
    run_id: str
    batch_id: str
    source_index: int
    tool_call_id: str
    tool_name: str
    arguments: JsonValue
    arguments_hash: str
    replay_policy: ReplayPolicy
    status: ToolOperationStatus
    attempt: int
    result: JsonValue | None
    is_error: bool | None
    terminate: bool | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: str
    operation_id: str
    batch_id: str
    source_index: int
    durable_message_id: str
    message: JsonValue
    status: OutboxStatus
    created_at: str
    published_at: str | None

