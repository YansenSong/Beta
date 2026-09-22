from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .providers.model import ModelAdapter
    from .harness.tool import Tool
    from .providers.policy import ProviderRequestOptions, ProviderRequestOptionsPatch

from .providers.policy import ProviderRequestOptions

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
from .runtime.errors import AgentErrorInfo, RunStatus

QueueMode = Literal["all", "one-at-a-time"]
TurnAction = Literal["continue", "end"]

ModelEventType = Literal[
    "start",
    "text_start",
    "text_delta",
    "text_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "toolcall_start",
    "toolcall_delta",
    "toolcall_end",
    "done",
    "error",
    # Kept for adapters and callers written against the first Beta protocol.
    "update",
]

AgentEventType = Literal[
    "agent_start",
    "turn_start",
    "message_start",
    "message_update",
    "message_end",
    "prepare_request",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "finish_turn",
    "turn_end",
    "agent_error",
    "agent_end",
]


@dataclass(slots=True)
class ModelEvent:
    type: ModelEventType
    partial: AgentMessage
    content_index: int | None = None
    delta: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_call: ToolCall | None = None
    completed_tool_call: ToolCall | None = None
    error: str | None = None
    error_message: str | None = None
    error_type: str | None = None


@dataclass(slots=True, frozen=True)
class TurnDecision:
    action: TurnAction


@dataclass(slots=True)
class PrepareRequestContext:
    context: AgentContext
    model: "ModelAdapter"
    request_options: "ProviderRequestOptions"


@dataclass(slots=True)
class RequestUpdate:
    context: AgentContext | None = None
    model: "ModelAdapter | None" = None
    request_options: "ProviderRequestOptionsPatch | None" = None


@dataclass(slots=True)
class AgentState:
    model: "ModelAdapter"
    messages: list[AgentMessage]
    tools: list["Tool[Any]"]
    is_streaming: bool
    streaming_message: AgentMessage | None
    pending_tool_calls: frozenset[str]
    error_message: str | None


@dataclass(slots=True)
class AgentEvent:
    type: AgentEventType
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
    # ``partial`` is an explicit alias for the complete current assistant
    # snapshot on message_update events. ``message`` remains the historical
    # field used by existing consumers.  New fields are appended to preserve
    # the original positional constructor order.
    partial: AgentMessage | None = None
    model_event: ModelEvent | None = None
    partial_result: Any = None
    is_error: bool | None = None
    turn_result: "TurnResult | None" = None
    turn_decision: TurnDecision | None = None


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
        from .runtime.transcript import create_initial_system_message, has_replayable_system_state

        if system_prompt is not None and not has_replayable_system_state(self.messages):
            baseline = create_initial_system_message(system_prompt, self.tools)
            if baseline is not None:
                self.messages.insert(0, baseline)

    @property
    def system_prompt(self) -> str:
        """Read-only compatibility view replayed from transcript system messages."""

        from .runtime.transcript import get_current_system_prompt

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
    request_options: "ProviderRequestOptionsPatch | None" = None


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
    "AgentEventType",
    "AgentState",
    "ContentBlocks",
    "ImageContent",
    "Message",
    "ModelEvent",
    "ModelEventType",
    "NextTurnUpdate",
    "PrepareRequestContext",
    "QueueMode",
    "RequestUpdate",
    "Role",
    "RunStatus",
    "StopReason",
    "TextContent",
    "ToolBatchResult",
    "ToolCall",
    "ToolResult",
    "TurnAction",
    "TurnDecision",
    "TurnResult",
    "utc_now_iso",
]
