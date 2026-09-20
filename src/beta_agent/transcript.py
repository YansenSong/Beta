from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .messages import AgentMessage, ToolDeclaration, ToolReference


@dataclass(slots=True)
class ToolStateChanges:
    tools_added: list[ToolDeclaration]
    tools_removed: list[ToolReference]


def to_tool_declaration(tool: object) -> ToolDeclaration:
    """Copy only the model-facing schema out of an executable runtime tool."""

    schema = getattr(tool, "schema", None)
    if callable(schema):
        parameters = schema()
    elif schema is not None:
        parameters = schema
    else:
        parameters = getattr(tool, "parameters", {})
    if not isinstance(parameters, dict):
        raise TypeError(f"Tool {getattr(tool, 'name', '<unknown>')!r} schema must be a dict")
    return ToolDeclaration(
        name=str(getattr(tool, "name")),
        description=str(getattr(tool, "description", "")),
        parameters=deepcopy(parameters),
    )


def declarations_equal(left: ToolDeclaration, right: ToolDeclaration) -> bool:
    return (
        left.name == right.name
        and left.description == right.description
        and left.parameters == right.parameters
    )


def _by_name(declarations: Sequence[ToolDeclaration]) -> dict[str, ToolDeclaration]:
    return {item.name: item for item in declarations}


def _same_tool_state(left: Sequence[ToolDeclaration], right: Sequence[ToolDeclaration]) -> bool:
    left_by_name = _by_name(left)
    right_by_name = _by_name(right)
    return left_by_name.keys() == right_by_name.keys() and all(
        declarations_equal(left_by_name[name], right_by_name[name]) for name in left_by_name
    )


def get_current_tool_declarations(messages: Sequence[AgentMessage]) -> list[ToolDeclaration]:
    declarations: dict[str, ToolDeclaration] = {}
    for message in messages:
        if message.role != "system":
            continue
        for reference in message.tools_removed:
            declarations.pop(reference.name, None)
        for declaration in message.tools_added:
            # Replacing an existing declaration keeps its stable position. A remove
            # followed by add naturally moves that name to the end of the state.
            declarations[declaration.name] = declaration
    return list(declarations.values())


def get_current_system_prompt(messages: Sequence[AgentMessage]) -> str:
    parts = [message.text for message in messages if message.role == "system" and message.text]
    return "\n\n".join(parts)


def has_replayable_system_state(messages: Sequence[AgentMessage]) -> bool:
    return any(
        message.role == "system" and (message.text or message.tools_added or message.tools_removed)
        for message in messages
    )


def get_tool_state_changes(
    previous: Sequence[ToolDeclaration],
    current: Sequence[ToolDeclaration],
) -> ToolStateChanges:
    previous_by_name = _by_name(previous)
    current_by_name = _by_name(current)
    removed = [
        ToolReference(name=old.name)
        for old in previous
        if old.name not in current_by_name or not declarations_equal(old, current_by_name[old.name])
    ]
    added = [
        new
        for new in current
        if new.name not in previous_by_name or not declarations_equal(previous_by_name[new.name], new)
    ]
    return ToolStateChanges(tools_added=added, tools_removed=removed)


def create_initial_system_message(
    system_prompt: str,
    tools: Sequence[object],
) -> AgentMessage | None:
    declarations = [to_tool_declaration(tool) for tool in tools]
    if not system_prompt and not declarations:
        return None
    return AgentMessage.system(system_prompt, tools_added=declarations)


def declare_tool_changes(
    context: Any,
    pending_messages: Sequence[AgentMessage],
) -> list[AgentMessage]:
    """Make the transcript's effective declarations match executable runtime tools.

    Pending system state is folded into its last system message when possible. If
    there is no pending system message, a new delta is inserted before the first
    non-system pending message so transcript order remains explicit.
    """

    pending = list(pending_messages)
    committed = list(context.messages)
    current_runtime = [to_tool_declaration(tool) for tool in context.tools]
    effective = get_current_tool_declarations([*committed, *pending])
    if _same_tool_state(effective, current_runtime):
        return pending

    system_indexes = [index for index, message in enumerate(pending) if message.role == "system"]
    if system_indexes:
        target_index = system_indexes[-1]
        state_before_target = get_current_tool_declarations([*committed, *pending[:target_index]])
        changes = get_tool_state_changes(state_before_target, current_runtime)
        target = pending[target_index]
        pending[target_index] = target.copy(
            tools_added=changes.tools_added,
            tools_removed=changes.tools_removed,
        )
        return pending

    changes = get_tool_state_changes(effective, current_runtime)
    delta = AgentMessage.system("", tools_added=changes.tools_added, tools_removed=changes.tools_removed)
    insertion = next((index for index, message in enumerate(pending) if message.role != "system"), len(pending))
    pending.insert(insertion, delta)
    return pending


def collapse_transcript(messages: Sequence[AgentMessage]) -> tuple[str, list[AgentMessage]]:
    """Project transcript state into the legacy system_prompt + messages boundary."""

    return get_current_system_prompt(messages), [message for message in messages if message.role != "system"]


__all__ = [
    "ToolStateChanges",
    "collapse_transcript",
    "create_initial_system_message",
    "declare_tool_changes",
    "declarations_equal",
    "get_current_system_prompt",
    "get_current_tool_declarations",
    "get_tool_state_changes",
    "has_replayable_system_state",
    "to_tool_declaration",
]
