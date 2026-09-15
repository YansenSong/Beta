from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..cancellation import CancellationToken, call_with_optional_cancellation
from ..tools import Tool
from ..types import AgentMessage
from .types import (
    ContextEvent,
    ExtensionAPI,
    ExtensionContext,
    ExtensionError,
    ExtensionFactory,
    ExtensionHandler,
    ExtensionTool,
    RegisteredCommand,
    RuntimeConfig,
    ToolCallDecision,
    ToolCallEvent,
)
from .wrapper import wrap_registered_tool

@dataclass(slots=True)
class _PendingRegistrations:
    handlers: dict[str, list[ExtensionHandler]] = field(default_factory=lambda: defaultdict(list))
    tools: list[ExtensionTool[Any]] = field(default_factory=list)
    commands: list[RegisteredCommand] = field(default_factory=list)

class ExtensionRunner:
    """保存 extension registration，并在现有 Core seam 上完成组合。"""

    def __init__(self, *, cwd: str | Path, config: RuntimeConfig, session) -> None:
        self.cwd = Path(cwd).resolve()
        self.config = config
        self.session = session
        self._handlers: dict[str, list[ExtensionHandler]] = defaultdict(list)
        self._tools: list[ExtensionTool[Any]] = []
        self._commands: list[RegisteredCommand] = []
        self._resolve_tools: Callable[[list[str]], list[Tool]] | None = None
        self._apply_tools: Callable[[list[Tool]], None] | None = None
        self.errors: list[ExtensionError] = []

    async def load(self, factories: list[ExtensionFactory]) -> None:
        for factory in factories:
            pending = _PendingRegistrations()
            api = self._create_api(pending)
            try:
                result = factory(api)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.errors.append(ExtensionError(stage="load", extension=getattr(factory, "__name__", None), message=str(exc)))
                continue
            for event, handlers in pending.handlers.items():
                self._handlers[event].extend(handlers)
            self._tools.extend(pending.tools)
            self._commands.extend(pending.commands)

    def bind_tool_runtime(self, *, resolve: Callable[[list[str]], list[Tool]], apply: Callable[[list[Tool]], None]) -> None:
        self._resolve_tools = resolve
        self._apply_tools = apply

    def create_context(self) -> ExtensionContext:
        return ExtensionContext(
            cwd=self.cwd,
            mode="cli",
            config=self.config,
            session=self.session,
            _get_active_tools=self.get_active_tools,
            _set_active_tools=self.set_active_tools,
        )

    def get_active_tools(self) -> list[str]:
        return list(self.config.active_tools)

    def set_active_tools(self, names: list[str]) -> None:
        if self._resolve_tools is None:
            known = {tool.name for tool in self.get_registered_tools()}
            resolved_names = [name for name in names if name in known]
            self.config.active_tools[:] = resolved_names
            return
        tools = self._resolve_tools(list(names))
        self.config.active_tools[:] = [tool.name for tool in tools]
        if self._apply_tools:
            self._apply_tools(tools)

    async def emit(
        self,
        event: str,
        payload: Any,
        *,
        cancellation: CancellationToken | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        ctx = self.create_context()
        for handler in list(self._handlers.get(event, [])):
            try:
                if cancellation is not None:
                    cancellation.throw_if_cancelled()
                value = await call_with_optional_cancellation(handler, payload, ctx, cancellation=cancellation)
                results.append(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append(ExtensionError(stage="handler", event=event, message=str(exc)))
        return results

    async def emit_tool_call(
        self,
        event: ToolCallEvent,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolCallDecision | None:
        ctx = self.create_context()
        for handler in list(self._handlers.get("tool_call", [])):
            try:
                if cancellation is not None:
                    cancellation.throw_if_cancelled()
                value = await call_with_optional_cancellation(handler, event, ctx, cancellation=cancellation)
                if isinstance(value, ToolCallDecision) and value.block:
                    return value
                if isinstance(value, dict) and value.get("block"):
                    return ToolCallDecision(block=True, reason=str(value.get("reason", "")), terminate=bool(value.get("terminate", False)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append(ExtensionError(stage="handler", event="tool_call", message=str(exc)))
        return None

    async def emit_context(
        self,
        messages: list[AgentMessage],
        *,
        cancellation: CancellationToken | None = None,
    ) -> list[AgentMessage]:
        current = list(messages)
        ctx = self.create_context()
        for handler in list(self._handlers.get("context", [])):
            try:
                if cancellation is not None:
                    cancellation.throw_if_cancelled()
                value = await call_with_optional_cancellation(
                    handler,
                    ContextEvent(messages=current),
                    ctx,
                    cancellation=cancellation,
                )
                if value is not None:
                    current = list(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append(ExtensionError(stage="handler", event="context", message=str(exc)))
        return current

    def get_registered_tools(self) -> list[Tool]:
        return [wrap_registered_tool(tool, self) for tool in self._tools]

    def get_commands(self) -> list[RegisteredCommand]:
        return list(self._commands)

    async def run_command(self, input_: str, *, cancellation: CancellationToken | None = None) -> None:
        raw = input_.strip().lstrip("/")
        name, _, args = raw.partition(" ")
        command = next((item for item in self._commands if item.name == name), None)
        if command is None:
            raise KeyError(f"Command not found: {name}")
        if cancellation is not None:
            cancellation.throw_if_cancelled()
        await call_with_optional_cancellation(
            command.handler,
            args.strip(),
            self.create_context(),
            cancellation=cancellation,
        )

    def _create_api(self, pending: _PendingRegistrations) -> ExtensionAPI:
        return ExtensionAPI(
            lambda event, handler: pending.handlers[event].append(handler),
            pending.tools.append,
            pending.commands.append,
        )
