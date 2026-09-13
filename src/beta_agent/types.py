from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import Tool

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["stop", "tool_calls", "length", "error", "aborted"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    stop_reason: StopReason | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)

    @classmethod
    def user(cls, text: str, **metadata: Any) -> "Message":
        return cls(role="user", content=text, metadata=metadata)

    @classmethod
    def system(cls, text: str, **metadata: Any) -> "Message":
        return cls(role="system", content=text, metadata=metadata)

    @classmethod
    def assistant(
        cls,
        text: str = "",
        *,
        tool_calls: list[ToolCall] | None = None,
        stop_reason: StopReason = "stop",
        **metadata: Any,
    ) -> "Message":
        return cls(
            role="assistant",
            content=text,
            tool_calls=list(tool_calls or []),
            stop_reason=stop_reason,
            metadata=metadata,
        )

    @classmethod
    def tool_result(
        cls,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        is_error: bool = False,
        **metadata: Any,
    ) -> "Message":
        return cls(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            is_error=is_error,
            metadata=metadata,
        )

    def copy(self, **changes: Any) -> "Message":
        return replace(self, **changes)


@dataclass(slots=True)
class ModelEvent:
    type: Literal["start", "update", "done", "error"]
    partial: Message


@dataclass(slots=True)
class AgentEvent:
    type: str
    message: Message | None = None
    messages: list[Message] | None = None
    tool_results: list[Message] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None
    error: str | None = None


@dataclass(slots=True)
class AgentContext:
    system_prompt: str
    messages: list[Message] = field(default_factory=list)
    tools: list["Tool[Any]"] = field(default_factory=list)

    def clone(self) -> "AgentContext":
        return AgentContext(
            system_prompt=self.system_prompt,
            messages=list(self.messages),
            tools=list(self.tools),
        )


@dataclass(slots=True)
class ToolResult:
    content: str
    details: Any = None
    terminate: bool = False
    added_tool_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolBatchResult:
    messages: list[Message]
    terminate: bool = False


@dataclass(slots=True)
class TurnResult:
    message: Message
    tool_results: list[Message]
    context: AgentContext
    new_messages: list[Message]
