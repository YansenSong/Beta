from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..agent import Agent
from ..cancellation import CancellationToken, call_with_optional_cancellation
from ..events import EventStream
from ..tools import BeforeToolCallDecision, Tool
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
    _active_outer: EventStream[list[AgentMessage]] | None = None

    def stream(self, prompt) -> EventStream[list[AgentMessage]]:
        # 同步创建 inner stream，让调用方在 outer host task 被调度之前，
        # 就能看到 Agent 的 active-run guard。
        self._ensure_outer_idle()
        inner = self.agent.stream(prompt)

        async def drive(emit):
            try:
                async for event in inner:
                    await self._handle_event(event)
                    await emit(event)
                return await inner.result()
            except asyncio.CancelledError:
                # 只取消 host wrapper 时，绝不能让底层 Agent 的 provider/tool task 继续运行。
                self.agent.abort()
                await inner.wait()
                raise

        outer = EventStream(drive, on_cancel=lambda _: self.agent.abort())
        self._active_outer = outer

        def cancel_inner_if_needed(completed: EventStream[list[AgentMessage]]) -> None:
            if completed.cancel_requested and not inner.done:
                self.agent.abort()
            if self._active_outer is completed:
                self._active_outer = None

        outer.add_done_callback(cancel_inner_if_needed)
        return outer

    async def run(self, prompt) -> list[AgentMessage]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[AgentMessage]]:
        self._ensure_outer_idle()
        inner = self.agent.continue_stream()

        async def drive(emit):
            try:
                async for event in inner:
                    await self._handle_event(event)
                    await emit(event)
                return await inner.result()
            except asyncio.CancelledError:
                self.agent.abort()
                await inner.wait()
                raise

        outer = EventStream(drive, on_cancel=lambda _: self.agent.abort())
        self._active_outer = outer
        outer.add_done_callback(
            lambda completed: self._on_outer_done(completed, inner)
        )
        return outer

    def _on_outer_done(self, completed: EventStream[list[AgentMessage]], inner: EventStream[list[AgentMessage]]) -> None:
        if completed.cancel_requested and not inner.done:
            self.agent.abort()
        if self._active_outer is completed:
            self._active_outer = None

    def _ensure_outer_idle(self) -> None:
        if self._active_outer is not None and not self._active_outer.done:
            raise RuntimeError("Agent is already processing. Use steer()/follow_up() or wait_for_idle().")

    def abort(self) -> None:
        self.agent.abort()

    async def wait_for_idle(self) -> None:
        await self.agent.wait_for_idle()
        outer = self._active_outer
        if outer is not None:
            await outer.wait()

    @property
    def is_running(self) -> bool:
        return self.agent.is_running or (self._active_outer is not None and not self._active_outer.done)

    async def run_command(self, input_: str) -> None:
        await self.runner.run_command(input_)

    async def _handle_event(self, event: AgentEvent) -> None:
        if event.type == "message_end" and event.message is not None:
            try:
                if self.persist_messages:
                    self.runner.session.append_message(event.message)
                await self.runner.emit("message_end", MessageEndEvent(event.message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Persistence/lifecycle wiring 属于 infrastructure，
                # 不同于 ExtensionRunner 会隔离处理的普通 extension handler。
                self.agent._fail_from_bridge(exc)
        elif event.type == "turn_end" and event.message is not None:
            try:
                await self.runner.emit("turn_end", TurnEndEvent(event.message, list(event.tool_results or [])))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.agent._fail_from_bridge(exc)

    def close(self) -> None:
        if self.is_running:
            self.agent.abort()
        self.agent.config.before_tool_call = self._previous_before
        self.agent.config.transform_context = self._previous_transform
        self.agent.context.tools = list(self._previous_tools)


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

    async def before_tool_call(call, args, context, cancellation: CancellationToken | None = None):
        if previous_before:
            decision = await call_with_optional_cancellation(
                previous_before,
                call,
                args,
                context,
                cancellation=cancellation,
            )
            if decision and decision.block:
                return decision
        verdict = await runner.emit_tool_call(
            ToolCallEvent(tool_call=call, args=args, agent_context=context),
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
    return ExtensionHost(
        agent=agent,
        runner=runner,
        persist_messages=persist_messages,
        _previous_before=previous_before,
        _previous_transform=previous_transform,
        _previous_tools=original_tools,
    )
