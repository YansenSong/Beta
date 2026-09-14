from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import Tool

from .messages import (
    AgentContent,
    AgentMessage,
    ContentBlocks,
    ImageContent,
    Message,
    Role,
    StopReason,
    TextContent,
    ToolCall,
    utc_now_iso,
)
from .errors import AgentErrorInfo, RunStatus


@dataclass(slots=True)
class ModelEvent:
    type: Literal["start", "update", "done", "error"]
    partial: AgentMessage


@dataclass(slots=True)
class AgentEvent:
    type: str
    message: AgentMessage | None = None
    messages: list[AgentMessage] | None = None
    tool_results: list[AgentMessage] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None
    error: str | None = None
    status: RunStatus | None = None
    error_info: AgentErrorInfo | None = None


@dataclass(slots=True)
class AgentContext:
    system_prompt: str
    messages: list[AgentMessage] = field(default_factory=list)
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
    messages: list[AgentMessage]
    terminate: bool = False
    aborted: bool = False


@dataclass(slots=True)
class TurnResult:
    message: AgentMessage
    tool_results: list[AgentMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


__all__ = [
    "AgentContent",
    "AgentContext",
    "AgentErrorInfo",
    "AgentEvent",
    "AgentMessage",
    "ContentBlocks",
    "ImageContent",
    "Message",
    "ModelEvent",
    "Role",
    "RunStatus",
    "StopReason",
    "TextContent",
    "ToolBatchResult",
    "ToolCall",
    "ToolResult",
    "TurnResult",
    "utc_now_iso",
]
