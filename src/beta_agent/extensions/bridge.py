from __future__ import annotations
import inspect
from dataclasses import dataclass
from typing import Any
from ..agent import Agent
from ..events import EventStream
from ..tools import BeforeToolCallDecision, Tool
from ..types import AgentEvent, Message
from .runner import ExtensionRunner
from .types import MessageEndEvent, ToolCallEvent, TurnEndEvent

@dataclass(slots=True)
class ExtensionHost:
    """Harness layer that binds ExtensionRunner to one Agent without changing Agent Core."""
    agent: Agent
    runner: ExtensionRunner
    persist_messages: bool
    _previous_before: Any
    _previous_transform: Any
    _previous_tools: list[Tool]

    def stream(self, prompt) -> EventStream[list[Message]]:
        async def drive(emit):
            inner = self.agent.stream(prompt)
            async for event in inner:
                await self._handle_event(event)
                await emit(event)
            return await inner.result()
        return EventStream(drive)

    async def run(self, prompt) -> list[Message]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[Message]]:
        async def drive(emit):
            inner = self.agent.continue_stream()
            async for event in inner:
                await self._handle_event(event)
                await emit(event)
            return await inner.result()
        return EventStream(drive)

    async def run_command(self, input_: str) -> None:
        await self.runner.run_command(input_)

    async def _handle_event(self, event: AgentEvent) -> None:
        if event.type == "message_end" and event.message is not None:
            if self.persist_messages:
                self.runner.session.append_message(event.message)
            await self.runner.emit("message_end", MessageEndEvent(event.message))
        elif event.type == "turn_end" and event.message is not None:
            await self.runner.emit("turn_end", TurnEndEvent(event.message, list(event.tool_results or [])))

    def close(self) -> None:
        self.agent.config.before_tool_call = self._previous_before
        self.agent.config.transform_context = self._previous_transform
        self.agent.context.tools = list(self._previous_tools)


def bind_extensions(agent: Agent, runner: ExtensionRunner, *, persist_messages: bool = True) -> ExtensionHost:
    """Connect Extension registrations to Core seams and return the harness used to run/observe the Agent."""
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

    async def before_tool_call(call, args, context):
        if previous_before:
            decision = previous_before(call, args, context)
            if inspect.isawaitable(decision):
                decision = await decision
            if decision and decision.block:
                return decision
        verdict = await runner.emit_tool_call(ToolCallEvent(tool_call=call, args=args, agent_context=context))
        if verdict and verdict.block:
            return BeforeToolCallDecision(block=True, reason=verdict.reason, terminate=verdict.terminate)
        return None

    async def transform_context(messages):
        current = list(messages)
        if previous_transform:
            value = previous_transform(current)
            current = await value if inspect.isawaitable(value) else value
        return await runner.emit_context(list(current))

    agent.config.before_tool_call = before_tool_call
    agent.config.transform_context = transform_context
    return ExtensionHost(
        agent=agent,
        runner=runner,
        persist_messages=persist_messages,
        _previous_before=previous_before,
        _previous_transform=previous_transform,
        _previous_tools=original_tools,
    )
