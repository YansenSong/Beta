from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from .events import EventStream
from .model import ModelAdapter
from .tools import AfterToolCall, BeforeToolCall, ToolRuntime
from .types import AgentContext, AgentEvent, Message, TurnResult

TransformContext = Callable[[Sequence[Message]], Awaitable[list[Message]] | list[Message]]
PrepareNextTurn = Callable[[TurnResult], Awaitable[AgentContext | None] | AgentContext | None]
ShouldStopAfterTurn = Callable[[TurnResult], Awaitable[bool] | bool]
MessageProvider = Callable[[], Awaitable[list[Message]] | list[Message]]


@dataclass(slots=True)
class AgentConfig:
    tool_execution: Literal["parallel", "sequential"] = "parallel"
    transform_context: TransformContext | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    should_stop_after_turn: ShouldStopAfterTurn | None = None
    get_steering_messages: MessageProvider | None = None
    get_follow_up_messages: MessageProvider | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None


class _MessageQueue:
    def __init__(self) -> None:
        self._items: deque[Message] = deque()

    def push(self, message: Message) -> None:
        self._items.append(message)

    def drain(self) -> list[Message]:
        items = list(self._items)
        self._items.clear()
        return items


class Agent:
    """Stateful agent runtime with Pi-inspired turn and tool semantics."""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        system_prompt: str = "You are a helpful assistant.",
        tools: Sequence[object] = (),
        messages: Sequence[Message] = (),
        config: AgentConfig | None = None,
    ) -> None:
        self.model = model
        self.context = AgentContext(system_prompt=system_prompt, messages=list(messages), tools=list(tools))
        self.config = config or AgentConfig()
        self._steering = _MessageQueue()
        self._follow_up = _MessageQueue()
        self._run_lock = asyncio.Lock()

    @property
    def messages(self) -> list[Message]:
        return list(self.context.messages)

    def replace_messages(self, messages: Sequence[Message]) -> None:
        self.context.messages = list(messages)

    def steer(self, text: str) -> None:
        self._steering.push(Message.user(text, delivery="steering"))

    def follow_up(self, text: str) -> None:
        self._follow_up.push(Message.user(text, delivery="follow_up"))

    def stream(self, prompt: str | Message | Sequence[Message]) -> EventStream[list[Message]]:
        prompts = self._normalize_prompts(prompt)
        return EventStream(lambda emit: self._run(prompts, emit))

    async def run(self, prompt: str | Message | Sequence[Message]) -> list[Message]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[Message]]:
        if not self.context.messages:
            raise ValueError("Cannot continue: no messages in context")
        if self.context.messages[-1].role == "assistant":
            raise ValueError("Cannot continue from an assistant message")
        return EventStream(lambda emit: self._run([], emit))

    def _normalize_prompts(self, prompt: str | Message | Sequence[Message]) -> list[Message]:
        if isinstance(prompt, str):
            return [Message.user(prompt)]
        if isinstance(prompt, Message):
            return [prompt]
        return list(prompt)

    async def _call(self, fn, *args):
        value = fn(*args)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _drain_steering(self) -> list[Message]:
        if self.config.get_steering_messages:
            return list(await self._call(self.config.get_steering_messages))
        return self._steering.drain()

    async def _drain_follow_up(self) -> list[Message]:
        if self.config.get_follow_up_messages:
            return list(await self._call(self.config.get_follow_up_messages))
        return self._follow_up.drain()

    async def _run(self, prompts: list[Message], emit) -> list[Message]:
        async with self._run_lock:
            new_messages = list(prompts)
            self.context.messages.extend(prompts)
            tool_runtime = ToolRuntime(
                before_tool_call=self.config.before_tool_call,
                after_tool_call=self.config.after_tool_call,
                execution_mode=self.config.tool_execution,
            )

            await emit(AgentEvent(type="agent_start"))
            await emit(AgentEvent(type="turn_start"))
            for message in prompts:
                await emit(AgentEvent(type="message_start", message=message))
                await emit(AgentEvent(type="message_end", message=message))

            last_turn: TurnResult | None = None
            pending = await self._drain_steering()

            while True:
                has_more_tool_calls = True
                while has_more_tool_calls or pending:
                    if last_turn is not None:
                        if self.config.prepare_next_turn:
                            next_context = await self._call(self.config.prepare_next_turn, last_turn)
                            if next_context is not None:
                                self.context = next_context
                        if not pending:
                            pending = await self._drain_steering()
                        await emit(AgentEvent(type="turn_start"))

                    if pending:
                        for message in pending:
                            self.context.messages.append(message)
                            new_messages.append(message)
                            await emit(AgentEvent(type="message_start", message=message))
                            await emit(AgentEvent(type="message_end", message=message))
                        pending = []

                    assistant = await self._stream_assistant(emit)
                    new_messages.append(assistant)

                    if assistant.stop_reason in {"error", "aborted"}:
                        await emit(AgentEvent(type="turn_end", message=assistant, tool_results=[]))
                        await emit(AgentEvent(type="agent_end", messages=list(new_messages)))
                        return new_messages

                    tool_results: list[Message] = []
                    has_more_tool_calls = False
                    if assistant.tool_calls:
                        if assistant.stop_reason == "length":
                            batch = await self._fail_truncated_tool_calls(assistant, emit)
                        else:
                            batch = await tool_runtime.execute_batch(
                                context=self.context,
                                calls=assistant.tool_calls,
                                emit=emit,
                            )
                        tool_results = batch.messages
                        self.context.messages.extend(tool_results)
                        new_messages.extend(tool_results)
                        has_more_tool_calls = not batch.terminate

                    await emit(AgentEvent(type="turn_end", message=assistant, tool_results=list(tool_results)))
                    last_turn = TurnResult(
                        message=assistant,
                        tool_results=list(tool_results),
                        context=self.context,
                        new_messages=new_messages,
                    )

                    if self.config.should_stop_after_turn and await self._call(
                        self.config.should_stop_after_turn, last_turn
                    ):
                        await emit(AgentEvent(type="agent_end", messages=list(new_messages)))
                        return new_messages

                    pending = await self._drain_steering()

                follow_up = await self._drain_follow_up()
                if follow_up:
                    pending = follow_up
                    continue
                break

            await emit(AgentEvent(type="agent_end", messages=list(new_messages)))
            return new_messages

    async def _stream_assistant(self, emit) -> Message:
        llm_messages: Sequence[Message] = self.context.messages
        if self.config.transform_context:
            llm_messages = await self._call(self.config.transform_context, list(llm_messages))

        added_partial = False
        final: Message | None = None
        async for event in self.model.stream(
            system_prompt=self.context.system_prompt,
            messages=llm_messages,
            tools=self.context.tools,
        ):
            partial = event.partial
            if event.type == "start":
                self.context.messages.append(partial)
                added_partial = True
                await emit(AgentEvent(type="message_start", message=partial.copy()))
            elif event.type == "update":
                if added_partial:
                    self.context.messages[-1] = partial
                else:
                    self.context.messages.append(partial)
                    added_partial = True
                await emit(AgentEvent(type="message_update", message=partial.copy()))
            elif event.type in {"done", "error"}:
                final = partial
                if added_partial:
                    self.context.messages[-1] = final
                else:
                    self.context.messages.append(final)
                    await emit(AgentEvent(type="message_start", message=final.copy()))
                await emit(AgentEvent(type="message_end", message=final))
                break

        if final is None:
            final = Message.assistant("Model stream ended without a final message", stop_reason="error")
            if added_partial:
                self.context.messages[-1] = final
            else:
                self.context.messages.append(final)
                await emit(AgentEvent(type="message_start", message=final.copy()))
            await emit(AgentEvent(type="message_end", message=final))
        return final

    async def _fail_truncated_tool_calls(self, assistant: Message, emit):
        from .types import ToolBatchResult

        results: list[Message] = []
        for call in assistant.tool_calls:
            await emit(
                AgentEvent(
                    type="tool_execution_start",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                )
            )
            message = Message.tool_result(
                tool_call_id=call.id,
                name=call.name,
                content="Tool call was not executed because the model output hit its token limit; arguments may be truncated.",
                is_error=True,
            )
            await emit(
                AgentEvent(
                    type="tool_execution_end",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    error=message.content,
                )
            )
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))
            results.append(message)
        return ToolBatchResult(messages=results, terminate=False)
