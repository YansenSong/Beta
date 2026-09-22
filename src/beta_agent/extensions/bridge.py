from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..agent import Agent
from ..runtime.cancellation import CancellationToken, call_with_optional_cancellation
from ..runtime.events import EventStream
from ..harness.tool import BeforeToolCallContext, BeforeToolCallDecision, Tool, adapt_tool_hook
from ..types import AgentEvent, AgentMessage
from .runner import ExtensionRunner
from .types import MessageEndEvent, ToolCallEvent, TurnEndEvent


@dataclass(slots=True)
class ExtensionHost:
    """把 ExtensionRunner 绑定到单个 Agent 的 harness layer。"""

    agent: Agent
    runner: ExtensionRunner
    persist_messages: bool
    _previous_before: Any
    _previous_transform: Any
    _previous_tools: list[Tool]
    _unsubscribe: Callable[[], None]
    _closed: bool = False

    def stream(self, prompt) -> EventStream[list[AgentMessage]]:
        return self.agent.stream(prompt)

    async def run(self, prompt) -> list[AgentMessage]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[AgentMessage]]:
        return self.agent.continue_stream()

    def abort(self) -> None:
        self.agent.abort()

    async def wait_for_idle(self) -> None:
        await self.agent.wait_for_idle()

    @property
    def is_running(self) -> bool:
        return self.agent.is_running

    async def run_command(self, input_: str) -> None:
        await self.runner.run_command(input_)

    def close(self) -> None:
        if self._closed:
            return
        if self.is_running:
            self.agent.abort()
        self._unsubscribe()
        self.agent.config.before_tool_call = self._previous_before
        self.agent.config.transform_context = self._previous_transform
        self.agent.context.tools = list(self._previous_tools)
        self._closed = True


def bind_extensions(agent: Agent, runner: ExtensionRunner, *, persist_messages: bool = True) -> ExtensionHost:
    """把 Extension registration 接到 Core seam，并返回用于运行/观察 Agent 的 harness。"""

    original_tools = list(agent.context.tools)
    extension_tools = runner.get_registered_tools()
    universe: dict[str, Tool] = {tool.name: tool for tool in [*original_tools, *extension_tools]}

    def resolve(names: list[str]) -> list[Tool]:
        return [universe[name] for name in names if name in universe]

    def apply(tools: list[Tool]) -> None:
        agent.context.tools = list(tools)

    runner.bind_tool_runtime(resolve=resolve, apply=apply)
    active = list(runner.config.active_tools) or list(universe)
    runner.set_active_tools(active)

    previous_before = agent.config.before_tool_call
    previous_transform = agent.config.transform_context
    previous_before_adapter = adapt_tool_hook(previous_before, kind="before")

    async def before_tool_call(hook_context: BeforeToolCallContext, cancellation: CancellationToken | None = None):
        if previous_before_adapter:
            decision = await call_with_optional_cancellation(
                previous_before_adapter,
                hook_context,
                cancellation=cancellation,
            )
            if decision and decision.block:
                return decision
        verdict = await runner.emit_tool_call(
            ToolCallEvent(
                tool_call=hook_context.tool_call,
                args=hook_context.args,
                agent_context=hook_context.context,
                assistant_message=hook_context.assistant_message,
            ),
            cancellation=cancellation,
        )
        if verdict and verdict.block:
            return BeforeToolCallDecision(block=True, reason=verdict.reason, terminate=verdict.terminate)
        return None

    async def transform_context(messages, cancellation: CancellationToken | None = None):
        current = list(messages)
        if previous_transform:
            current = await call_with_optional_cancellation(
                previous_transform,
                current,
                cancellation=cancellation,
            )
        return await runner.emit_context(list(current), cancellation=cancellation)

    agent.config.before_tool_call = before_tool_call
    agent.config.transform_context = transform_context

    async def on_agent_event(event: AgentEvent, cancellation: CancellationToken) -> None:
        if event.type == "message_end" and event.message is not None:
            if persist_messages:
                runner.session.append_message(event.message)
            await runner.emit("message_end", MessageEndEvent(event.message))
        elif event.type == "turn_end" and event.message is not None:
            await runner.emit("turn_end", TurnEndEvent(event.message, list(event.tool_results or [])))

    unsubscribe = agent.subscribe(on_agent_event)
    return ExtensionHost(
        agent=agent,
        runner=runner,
        persist_messages=persist_messages,
        _previous_before=previous_before,
        _previous_transform=previous_transform,
        _previous_tools=original_tools,
        _unsubscribe=unsubscribe,
    )
