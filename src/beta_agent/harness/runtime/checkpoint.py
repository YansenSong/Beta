from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union, cast

from ...durable.types import JsonValue, validate_json


class UnsupportedOperationStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OperationMeta:
    operation_id: str
    session_id: str
    kind: Literal["agent_run"]
    mode: Literal["prompt", "continue"]
    accepted_at: str
    prompt_message_ids: tuple[str, ...]
    initial_session_leaf_id: str | None


@dataclass(frozen=True, slots=True)
class StartingState:
    kind: Literal["starting"] = "starting"
    turn_index: int = 0


@dataclass(frozen=True, slots=True)
class CheckpointState:
    turn_index: int
    continuation: Literal["need_assistant", "may_finish"]
    previous_assistant_message_id: str | None
    previous_tool_message_ids: tuple[str, ...]
    include_final_assistant: bool = True
    kind: Literal["checkpoint"] = "checkpoint"


@dataclass(frozen=True, slots=True)
class AssistantEffectPendingState:
    turn_index: int
    response_message_id: str
    request_id: str
    model_id: str | None
    started_at: str
    kind: Literal["assistant_effect_pending"] = "assistant_effect_pending"


@dataclass(frozen=True, slots=True)
class ToolsState:
    turn_index: int
    batch_id: str
    assistant_message_id: str
    tool_operation_ids: tuple[str, ...]
    kind: Literal["tools"] = "tools"


OperationState = Union[StartingState, CheckpointState, AssistantEffectPendingState, ToolsState]


def _nonnegative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def operation_meta_to_json(meta: OperationMeta) -> JsonValue:
    value: JsonValue = {
        "operation_id": meta.operation_id, "session_id": meta.session_id, "kind": meta.kind,
        "mode": meta.mode, "accepted_at": meta.accepted_at,
        "prompt_message_ids": list(meta.prompt_message_ids),
        "initial_session_leaf_id": meta.initial_session_leaf_id,
    }
    return validate_json(value)


def operation_meta_from_json(value: JsonValue) -> OperationMeta:
    if not isinstance(value, dict) or value.get("kind") != "agent_run":
        raise ValueError("Invalid durable operation metadata")
    mode = value.get("mode")
    if mode not in ("prompt", "continue"):
        raise ValueError("Invalid operation mode")
    ids = value.get("prompt_message_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ValueError("prompt_message_ids must be a list of strings")
    return OperationMeta(
        operation_id=_string(value, "operation_id"), session_id=_string(value, "session_id"),
        kind="agent_run", mode=cast(Literal["prompt", "continue"], mode),
        accepted_at=_string(value, "accepted_at"), prompt_message_ids=tuple(ids),
        initial_session_leaf_id=_optional_string(value, "initial_session_leaf_id"),
    )


def operation_state_to_json(state: OperationState) -> JsonValue:
    if isinstance(state, StartingState):
        value: JsonValue = {"kind": "starting", "turn_index": state.turn_index}
    elif isinstance(state, CheckpointState):
        value = {"kind": "checkpoint", "turn_index": state.turn_index,
                 "continuation": state.continuation,
                 "previous_assistant_message_id": state.previous_assistant_message_id,
                 "previous_tool_message_ids": list(state.previous_tool_message_ids),
                 "include_final_assistant": state.include_final_assistant}
    elif isinstance(state, AssistantEffectPendingState):
        value = {"kind": "assistant_effect_pending", "turn_index": state.turn_index,
                 "response_message_id": state.response_message_id, "request_id": state.request_id,
                 "model_id": state.model_id, "started_at": state.started_at}
    elif isinstance(state, ToolsState):
        value = {"kind": "tools", "turn_index": state.turn_index, "batch_id": state.batch_id,
                 "assistant_message_id": state.assistant_message_id,
                 "tool_operation_ids": list(state.tool_operation_ids)}
    else:
        raise UnsupportedOperationStateError(f"Unsupported state type: {type(state).__name__}")
    return validate_json(value)


def operation_state_from_json(value: JsonValue) -> OperationState:
    if not isinstance(value, dict):
        raise ValueError("Operation state must be an object")
    kind = value.get("kind")
    turn = _nonnegative(value.get("turn_index"), "turn_index")
    if kind == "starting":
        return StartingState(turn_index=turn)
    if kind == "checkpoint":
        continuation = value.get("continuation")
        if continuation not in ("need_assistant", "may_finish"):
            raise ValueError("Invalid checkpoint continuation")
        ids = value.get("previous_tool_message_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("previous_tool_message_ids must be a list of strings")
        include = value.get("include_final_assistant", True)
        if not isinstance(include, bool):
            raise ValueError("include_final_assistant must be boolean")
        return CheckpointState(turn, cast(Literal["need_assistant", "may_finish"], continuation),
                               _optional_string(value, "previous_assistant_message_id"), tuple(ids), include)
    if kind == "assistant_effect_pending":
        return AssistantEffectPendingState(turn, _string(value, "response_message_id"),
                                           _string(value, "request_id"),
                                           _optional_string(value, "model_id"), _string(value, "started_at"))
    if kind == "tools":
        ids = value.get("tool_operation_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("tool_operation_ids must be a list of strings")
        return ToolsState(turn, _string(value, "batch_id"), _string(value, "assistant_message_id"), tuple(ids))
    raise UnsupportedOperationStateError(f"Unsupported operation state: {kind!r}")


def _string(value: dict[str, JsonValue], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{name} must be a non-empty string")
    return item


def _optional_string(value: dict[str, JsonValue], name: str) -> str | None:
    item = value.get(name)
    if item is not None and not isinstance(item, str):
        raise ValueError(f"{name} must be a string or null")
    return item
