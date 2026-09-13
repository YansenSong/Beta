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
    # 这些 Hook 负责“策略”，Agent Loop 只负责稳定的运行时控制流。
    # 这样后续增加上下文裁剪、权限判断等能力时，不需要不断改写主循环。
    tool_execution: Literal["parallel", "sequential"] = "parallel"
    transform_context: TransformContext | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    should_stop_after_turn: ShouldStopAfterTurn | None = None
    get_steering_messages: MessageProvider | None = None
    get_follow_up_messages: MessageProvider | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None


class _MessageQueue:
    """Agent 内部使用的轻量消息队列。

    Steering 和 Follow-up 故意使用两条队列，因为它们进入 Loop 的检查点不同。
    """

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
        # 同一个 Agent 的 context 是可变状态；禁止两个 run 同时修改它，避免 history 交叉写入。
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
        """统一兼容同步 Hook 和异步 Hook，减少调用方接入成本。"""
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
            # Steering 即使在 run 启动前已经排队，也应该参与下一次模型调用。
            pending = await self._drain_steering()

            # 双层循环是一个关键设计：
            # - 内层循环处理“当前任务仍需继续”的 Tool Call / Steering；
            # - 外层循环只在任务本来要结束时，再检查更晚到达的 Follow-up。
            while True:
                has_more_tool_calls = True
                while has_more_tool_calls or pending:
                    if last_turn is not None:
                        # prepare_next_turn 可以真正替换下一轮 Runtime context，
                        # 它与只改变模型输入视图的 transform_context 不同。
                        if self.config.prepare_next_turn:
                            next_context = await self._call(self.config.prepare_next_turn, last_turn)
                            if next_context is not None:
                                self.context = next_context
                        if not pending:
                            pending = await self._drain_steering()
                        await emit(AgentEvent(type="turn_start"))

                    if pending:
                        # Steering 不抢占正在执行的 turn；只在完整 turn 结束后作为普通 user message 注入。
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
                        # 输出被 token limit 截断时，参数即使“碰巧能解析”也可能不完整，因此禁止执行。
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

                    # 当前 turn（包括所有 tool result）完全结束后，Steering 才能影响下一轮。
                    pending = await self._drain_steering()

                # Follow-up 的检查点更晚：只有 Agent 本来准备结束当前 run 时才读取。
                follow_up = await self._drain_follow_up()
                if follow_up:
                    pending = follow_up
                    continue
                break

            await emit(AgentEvent(type="agent_end", messages=list(new_messages)))
            return new_messages

    async def _stream_assistant(self, emit) -> Message:
        # transform_context 只决定“本轮模型看到什么”，默认不替换 Runtime 保存的完整 history。
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
                # update 携带的是“当前完整 partial message”，而不是单独字符 delta。
                # UI/Tracing 即使漏掉某个更新，也可以直接使用下一次完整状态继续渲染。
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
