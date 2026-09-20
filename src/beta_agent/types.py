from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .model import ModelAdapter
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

QueueMode = Literal["all", "one-at-a-time"]


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


@dataclass(slots=True, init=False)
class AgentContext:
    messages: list[AgentMessage] = field(default_factory=list)
    tools: list["Tool[Any]"] = field(default_factory=list)

    def __init__(
        self,
        messages: Sequence[AgentMessage] | None = None,
        tools: Sequence["Tool[Any]"] | None = None,
        *,
        system_prompt: str | None = None,
    ) -> None:
        # Compatibility for callers that historically constructed AgentContext
        # directly. Runtime state is immediately represented as a transcript message.
        self.messages = list(messages or [])
        self.tools = list(tools or [])
        from .transcript import create_initial_system_message, has_replayable_system_state

        if system_prompt is not None and not has_replayable_system_state(self.messages):
            baseline = create_initial_system_message(system_prompt, self.tools)
            if baseline is not None:
                self.messages.insert(0, baseline)

    @property
    def system_prompt(self) -> str:
        """Read-only compatibility view replayed from transcript system messages."""

        from .transcript import get_current_system_prompt

        return get_current_system_prompt(self.messages)

    def clone(self) -> "AgentContext":
        return AgentContext(
            messages=list(self.messages),
            tools=list(self.tools),
        )


@dataclass(slots=True)
class ToolResult:
    content: str | AgentContent | Sequence[AgentContent]
    details: Any = None
    terminate: bool = False
    added_tool_names: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        from .messages import normalize_content_blocks

        self.content = normalize_content_blocks(self.content)
        self.added_tool_names = list(self.added_tool_names)
        self.usage = dict(self.usage) if self.usage is not None else None


@dataclass(slots=True)
class NextTurnUpdate:
    context: AgentContext | None = None
    messages: list[AgentMessage] = field(default_factory=list)
    model: "ModelAdapter | None" = None


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
    # # 本次 Agent run 开始后，截至当前 turn 累计新增的消息
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
    "NextTurnUpdate",
    "QueueMode",
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
