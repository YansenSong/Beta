from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar
from pydantic import BaseModel

from ..session import SessionTree
from ..tools import ToolExecutionContext
from ..types import AgentContext, AgentMessage, ToolCall, ToolResult

ArgsT = TypeVar("ArgsT", bound=BaseModel)
ExtensionEventName = Literal["tool_call", "context", "message_end", "turn_end"]

@dataclass(slots=True)
class RuntimeConfig:
    model: str = ""
    active_tools: list[str] = field(default_factory=list)
    services: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class ExtensionContext:
    cwd: Path
    mode: Literal["cli"]
    config: RuntimeConfig
    session: SessionTree
    _get_active_tools: Callable[[], list[str]]
    _set_active_tools: Callable[[list[str]], None]

    def get_active_tools(self) -> list[str]:
        return self._get_active_tools()

    def set_active_tools(self, names: list[str]) -> None:
        self._set_active_tools(names)

    def append_entry(self, kind: str, payload: dict[str, Any]) -> None:
        self.session._append("custom", {"kind": kind, "data": payload})

@dataclass(slots=True)
class ToolCallEvent:
    tool_call: ToolCall
    args: BaseModel
    agent_context: AgentContext

@dataclass(slots=True)
class ContextEvent:
    messages: list[AgentMessage]

@dataclass(slots=True)
class MessageEndEvent:
    message: AgentMessage

@dataclass(slots=True)
class TurnEndEvent:
    message: AgentMessage
    tool_results: list[AgentMessage]

@dataclass(slots=True)
class ToolCallDecision:
    block: bool = False
    reason: str = ""
    terminate: bool = False

ExtensionHandler = Callable[[Any, ExtensionContext], Awaitable[Any] | Any]
ExtensionFactory = Callable[["ExtensionAPI"], Awaitable[None] | None]
ExtensionToolHandler = Callable[[ArgsT, ExtensionContext, ToolExecutionContext], Awaitable[ToolResult | str] | ToolResult | str]

@dataclass(slots=True)
class ExtensionTool(Generic[ArgsT]):
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ExtensionToolHandler[ArgsT]
    execution_mode: Literal["parallel", "sequential"] = "parallel"
    replay_policy: Literal["safe", "unsafe"] = "unsafe"

@dataclass(slots=True)
class RegisteredCommand:
    name: str
    description: str
    handler: Callable[[str, ExtensionContext], Awaitable[None] | None]

@dataclass(slots=True)
class ExtensionError:
    stage: str
    message: str
    extension: str | None = None
    event: str | None = None

class ExtensionAPI:
    def __init__(self, on_handler, register_tool, register_command):
        self._on_handler = on_handler
        self._register_tool = register_tool
        self._register_command = register_command

    def on(self, event: ExtensionEventName, handler: ExtensionHandler) -> None:
        self._on_handler(event, handler)

    def register_tool(self, tool: ExtensionTool[Any]) -> None:
        self._register_tool(tool)

    def register_command(self, name: str, *, description: str, handler) -> None:
        self._register_command(RegisteredCommand(name=name, description=description, handler=handler))
