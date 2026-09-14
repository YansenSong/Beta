from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ErrorStage = Literal[
    "runtime",
    "model",
    "transform_context",
    "convert_to_llm",
    "prepare_next_turn",
    "should_stop_after_turn",
    "steering_provider",
    "follow_up_provider",
    "before_tool_call",
    "tool_execute",
    "after_tool_call",
    "extension_bridge",
]
RunStatus = Literal["completed", "error", "aborted"]


@dataclass(slots=True)
class AgentErrorInfo:
    stage: ErrorStage
    message: str
    exception_type: str
    retryable: bool = False

