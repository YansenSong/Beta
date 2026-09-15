from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .cancellation import CancellationToken, accepts_cancellation, call_with_optional_cancellation
from .errors import AgentErrorInfo, ErrorStage, RunStatus
from .events import EventStream
from .model import ModelAdapter
from .provider_messages import ProviderMessage, default_convert_to_llm
from .tools import AfterToolCall, BeforeToolCall, ToolRuntime
from .types import AgentContext, AgentEvent, AgentMessage, ModelEvent, ToolBatchResult, TurnResult

TransformContext = Callable[..., Awaitable[list[AgentMessage]] | list[AgentMessage]]
PrepareNextTurn = Callable[..., Awaitable[AgentContext | None] | AgentContext | None]
ShouldStopAfterTurn = Callable[..., Awaitable[bool] | bool]
MessageProvider = Callable[..., Awaitable[list[AgentMessage]] | list[AgentMessage]]
ConvertToLlm = Callable[
    [Sequence[AgentMessage]],
    Awaitable[list[ProviderMessage]] | list[ProviderMessage],
]


@dataclass(slots=True)
class AgentConfig:
    # 这些 Hook 负责“策略”，Agent Loop 只负责稳定的运行时控制流。
    # 这样后续增加上下文裁剪、权限判断等能力时，不需要不断改写主循环。
    tool_execution: Literal["parallel", "sequential"] = "parallel"
    transform_context: TransformContext | None = None
    convert_to_llm: ConvertToLlm = default_convert_to_llm
    prepare_next_turn: PrepareNextTurn | None = None
    should_stop_after_turn: ShouldStopAfterTurn | None = None
    get_steering_messages: MessageProvider | None = None
    get_follow_up_messages: MessageProvider | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None


@dataclass(slots=True)
class _ActiveRun:
    token: CancellationToken
    stream: EventStream[list[AgentMessage]]


@dataclass(slots=True)
class _RunState:
    new_messages: list[AgentMessage]
    agent_started: bool = False
    turn_started: bool = False
    turn_end_emitted: bool = False
    agent_end_emitted: bool = False
    current_assistant: AgentMessage | None = None


@dataclass(slots=True)
class _AssistantResult:
    message: AgentMessage
    error_info: AgentErrorInfo | None = None


class _StageFailure(Exception):
    def __init__(self, info: AgentErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


class _MessageQueue:
    """Agent 内部使用的轻量消息队列。

    Steering 和 Follow-up 故意使用两条队列，因为它们进入 Loop 的检查点不同。
    """

    def __init__(self) -> None:
        self._items: deque[AgentMessage] = deque()

    def push(self, message: AgentMessage) -> None:
        self._items.append(message)

    def drain(self) -> list[AgentMessage]:
        items = list(self._items)
        self._items.clear()
        return items


class Agent:
    """带 cancellation 与标准化 lifecycle error 的有状态 Agent Runtime。"""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        system_prompt: str = "You are a helpful assistant.",
        tools: Sequence[object] = (),
        messages: Sequence[AgentMessage] = (),
        config: AgentConfig | None = None,
    ) -> None:
        self.model = model
        self.context = AgentContext(system_prompt=system_prompt, messages=list(messages), tools=list(tools))
        self.config = config or AgentConfig()
        self._steering = _MessageQueue()
        self._follow_up = _MessageQueue()
        self._active_run: _ActiveRun | None = None
        self._last_error: AgentErrorInfo | None = None
        self._external_failure: AgentErrorInfo | None = None

    @property
    def messages(self) -> list[AgentMessage]:
        return list(self.context.messages)

    @property
    def is_running(self) -> bool:
        active = self._active_run
        return active is not None and not active.stream.done

    @property
    def last_error(self) -> AgentErrorInfo | None:
        return self._last_error

    def replace_messages(self, messages: Sequence[AgentMessage]) -> None:
        self.context.messages = list(messages)

    def steer(self, text: str) -> None:
        self._steering.push(AgentMessage.user(text, delivery="steering"))

    def follow_up(self, text: str) -> None:
        self._follow_up.push(AgentMessage.user(text, delivery="follow_up"))

    def stream(self, prompt: str | AgentMessage | Sequence[AgentMessage]) -> EventStream[list[AgentMessage]]:
        self._ensure_idle()
        prompts = self._normalize_prompts(prompt)
        token = CancellationToken()
        self._last_error = None
        self._external_failure = None
        stream = EventStream(
            lambda emit: self._run(prompts, emit, token),
            on_cancel=lambda _: token.cancel(),
        )
        self._active_run = _ActiveRun(token=token, stream=stream)
        stream.add_done_callback(lambda completed: self._clear_active_run(completed))
        return stream

    async def run(self, prompt: str | AgentMessage | Sequence[AgentMessage]) -> list[AgentMessage]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[AgentMessage]]:
        self._ensure_idle()
        if not self.context.messages:
            raise ValueError("Cannot continue: no messages in context")
        if self.context.messages[-1].role == "assistant":
            raise ValueError("Cannot continue from an assistant message")
        return self.stream([])

    def abort(self) -> None:
        active = self._active_run
        if active is None:
            return
        active.token.cancel()
        active.stream.cancel()

    def _fail_from_bridge(self, exc: BaseException) -> None:
        """让 ExtensionHost 的结构性 failure 复用 Agent 的 error lifecycle。"""

        info = _error_info("extension_bridge", exc)
        self._external_failure = info
        active = self._active_run
        if active is not None:
            active.token.cancel()
            active.stream.cancel()
        else:
            self._last_error = info

    async def wait_for_idle(self) -> None:
        active = self._active_run
        if active is None:
            return
        await active.stream.wait()
        if self._active_run is active:
            self._active_run = None

    def _ensure_idle(self) -> None:
        active = self._active_run
        if active is None:
            return
        if active.stream.done:
            self._clear_active_run(active.stream)
            return
        raise RuntimeError("Agent is already processing. Use steer()/follow_up() or wait_for_idle().")

    def _clear_active_run(self, stream: EventStream[list[AgentMessage]]) -> None:
        if self._active_run is not None and self._active_run.stream is stream:
            self._active_run = None

    def _normalize_prompts(
        self,
        prompt: str | AgentMessage | Sequence[AgentMessage],
    ) -> list[AgentMessage]:
        if isinstance(prompt, str):
            return [AgentMessage.user(prompt)]
        if isinstance(prompt, AgentMessage):
            return [prompt]
        return list(prompt)

    async def _call(self, fn: Callable[..., Any], *args: Any, cancellation: CancellationToken | None = None) -> Any:
        return await call_with_optional_cancellation(fn, *args, cancellation=cancellation)

    async def _drain_steering(self, cancellation: CancellationToken) -> list[AgentMessage]:
        cancellation.throw_if_cancelled()
        if self.config.get_steering_messages:
            try:
                messages = await self._call(self.config.get_steering_messages, cancellation=cancellation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _StageFailure(_error_info("steering_provider", exc)) from exc
            cancellation.throw_if_cancelled()
            return list(messages)
        return self._steering.drain()

    async def _drain_follow_up(self, cancellation: CancellationToken) -> list[AgentMessage]:
        cancellation.throw_if_cancelled()
        if self.config.get_follow_up_messages:
            try:
                messages = await self._call(self.config.get_follow_up_messages, cancellation=cancellation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _StageFailure(_error_info("follow_up_provider", exc)) from exc
            cancellation.throw_if_cancelled()
            return list(messages)
        return self._follow_up.drain()

    async def _run(
        self,
        prompts: list[AgentMessage],
        emit,
        cancellation: CancellationToken,
    ) -> list[AgentMessage]:
        state = _RunState(new_messages=list(prompts))
        try:
            return await self._run_impl(prompts, emit, cancellation, state)
        except asyncio.CancelledError:
            cancellation.cancel()
            if self._external_failure is not None:
                info = self._external_failure
                self._external_failure = None
                return await self._finalize_error(state, emit, info)
            return await self._finalize_aborted(state, emit)
        except _StageFailure as failure:
            return await self._finalize_error(state, emit, failure.info)
        except Exception as exc:
            return await self._finalize_error(state, emit, _error_info("runtime", exc))

    async def _run_impl(self, prompts, emit, cancellation, state) -> list[AgentMessage]:
        state.agent_started = True
        await emit(AgentEvent(type="agent_start"))
        cancellation.throw_if_cancelled()

        self.context.messages.extend(prompts)
        state.turn_started = True
        await emit(AgentEvent(type="turn_start"))
        for message in prompts:
            cancellation.throw_if_cancelled()
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))

        previous_turn: TurnResult | None = None
        # Steering 即使在 run 启动前已经排队，也应该参与下一次模型调用。
        pending = await self._drain_steering(cancellation)

        # 双层循环是一个关键设计：
        # - 内层循环处理“当前任务仍需继续”的 Tool Call / Steering；
        # - 外层循环只在任务本来要结束时，再检查更晚到达的 Follow-up。
        while True:
            has_more_tool_calls = True
            while has_more_tool_calls or pending:
                if previous_turn is not None:
                    if self.config.prepare_next_turn:
                        cancellation.throw_if_cancelled()
                        try:
                            next_context = await self._call(
                                self.config.prepare_next_turn,
                                previous_turn,
                                cancellation=cancellation,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            raise _StageFailure(_error_info("prepare_next_turn", exc)) from exc
                        cancellation.throw_if_cancelled()
                        if next_context is not None:
                            self.context = next_context
                    if not pending:
                        pending = await self._drain_steering(cancellation)
                    state.turn_started = True
                    state.turn_end_emitted = False
                    await emit(AgentEvent(type="turn_start"))

                if pending:
                    cancellation.throw_if_cancelled()
                    for message in pending:
                        self.context.messages.append(message)
                        state.new_messages.append(message)
                        await emit(AgentEvent(type="message_start", message=message))
                        await emit(AgentEvent(type="message_end", message=message))
                    pending = []

                assistant_result = await self._stream_assistant(emit, cancellation, state)
                assistant = assistant_result.message
                state.current_assistant = assistant
                if not any(message is assistant for message in state.new_messages):
                    state.new_messages.append(assistant)

                if assistant_result.error_info is not None or assistant.stop_reason == "error":
                    await self._emit_turn_end(state, emit, assistant, [])
                    info = assistant_result.error_info or _error_info(
                        "model",
                        RuntimeError(assistant.metadata.get("error_message", "Model request failed.")),
                    )
                    raise _StageFailure(info)

                if assistant.stop_reason == "aborted":
                    await self._emit_turn_end(state, emit, assistant, [])
                    return await self._finalize_aborted(state, emit)

                tool_results: list[AgentMessage] = []
                batch: ToolBatchResult | None = None
                has_more_tool_calls = False
                if assistant.tool_calls:
                    cancellation.throw_if_cancelled()
                    if assistant.stop_reason == "length":
                        batch = await self._fail_truncated_tool_calls(assistant, emit, cancellation)
                    else:
                        tool_runtime = ToolRuntime(
                            before_tool_call=self.config.before_tool_call,
                            after_tool_call=self.config.after_tool_call,
                            execution_mode=self.config.tool_execution,
                        )
                        batch = await tool_runtime.execute_batch(
                            context=self.context,
                            calls=assistant.tool_calls,
                            emit=emit,
                            cancellation=cancellation,
                        )
                    tool_results = batch.messages
                    self.context.messages.extend(tool_results)
                    state.new_messages.extend(tool_results)
                    has_more_tool_calls = not batch.terminate and not batch.aborted

                await self._emit_turn_end(state, emit, assistant, tool_results)
                previous_turn = TurnResult(
                    message=assistant,
                    tool_results=list(tool_results),
                    context=self.context,
                    new_messages=state.new_messages,
                )

                if batch is not None and batch.aborted:
                    return await self._finalize_aborted(state, emit)

                if self.config.should_stop_after_turn:
                    cancellation.throw_if_cancelled()
                    try:
                        should_stop = await self._call(
                            self.config.should_stop_after_turn,
                            previous_turn,
                            cancellation=cancellation,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _StageFailure(_error_info("should_stop_after_turn", exc)) from exc
                    cancellation.throw_if_cancelled()
                    if should_stop:
                        return await self._emit_agent_end(state, emit, status="completed")

                # 当前 turn（包括所有 tool result）完全结束后，Steering 才能影响下一轮。
                pending = await self._drain_steering(cancellation)

            # Follow-up 的检查点更晚：只有 Agent 本来准备结束当前 run 时才读取。
            follow_up = await self._drain_follow_up(cancellation)
            if follow_up:
                pending = follow_up
                continue
            break

        return await self._emit_agent_end(state, emit, status="completed")

    async def _stream_assistant(self, emit, cancellation: CancellationToken, state: _RunState) -> _AssistantResult:
        # transform_context 只决定“本轮模型看到什么”，默认不替换 Runtime 保存的完整 history。
        cancellation.throw_if_cancelled()
        runtime_messages: Sequence[AgentMessage] = list(self.context.messages)
        if self.config.transform_context:
            try:
                runtime_messages = await self._call(
                    self.config.transform_context,
                    list(runtime_messages),
                    cancellation=cancellation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _StageFailure(_error_info("transform_context", exc)) from exc
        cancellation.throw_if_cancelled()

        try:
            converted_messages = await self._call(
                self.config.convert_to_llm,
                list(runtime_messages),
                cancellation=cancellation,
            )
            provider_messages = list(converted_messages)
            if not all(isinstance(message, ProviderMessage) for message in provider_messages):
                raise TypeError("convert_to_llm must return ProviderMessage values")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _StageFailure(_error_info("convert_to_llm", exc)) from exc
        cancellation.throw_if_cancelled()

        partial: AgentMessage | None = None
        added_partial = False
        message_end_emitted = False
        model_started = True
        state.current_assistant = None

        try:
            model_stream = self._model_stream(provider_messages, cancellation)
            if inspect.isawaitable(model_stream):
                model_stream = await model_stream
            async for event in model_stream:
                cancellation.throw_if_cancelled()
                if not isinstance(event, ModelEvent):
                    raise TypeError("ModelAdapter yielded a non-ModelEvent value")
                partial = event.partial
                state.current_assistant = partial
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
                        await emit(AgentEvent(type="message_start", message=partial.copy()))
                    await emit(AgentEvent(type="message_update", message=partial.copy()))
                elif event.type in {"done", "error"}:
                    if event.type == "error" or partial.stop_reason == "error":
                        error_message = str(partial.metadata.get("error_message", "Model request failed."))
                        partial = _model_error_message(partial, error_message, partial.metadata.get("error_type"))
                    elif partial.stop_reason == "aborted":
                        partial = _aborted_assistant(partial)
                    if added_partial:
                        self.context.messages[-1] = partial
                    else:
                        self.context.messages.append(partial)
                        added_partial = True
                        await emit(AgentEvent(type="message_start", message=partial.copy()))
                    await emit(AgentEvent(type="message_end", message=partial))
                    message_end_emitted = True
                    state.current_assistant = partial
                    error_info = (
                        _error_info(
                            "model",
                            RuntimeError(str(partial.metadata.get("error_message", "Model request failed."))),
                        )
                        if partial.stop_reason == "error"
                        else None
                    )
                    return _AssistantResult(partial, error_info)

            # 合规的 adapter 应始终正常结束；如果 stream 不完整，
            # 则将其作为普通 model failure 处理，并补全 message lifecycle。
            error = RuntimeError("Model stream ended without a final message")
            final = _model_error_message(partial or AgentMessage.assistant(""), str(error), type(error).__name__)
            if added_partial:
                self.context.messages[-1] = final
            else:
                self.context.messages.append(final)
                await emit(AgentEvent(type="message_start", message=final.copy()))
            await emit(AgentEvent(type="message_end", message=final))
            state.current_assistant = final
            return _AssistantResult(final, _error_info("model", error))
        except asyncio.CancelledError:
            cancellation.cancel()
            if model_started and not message_end_emitted:
                aborted = _aborted_assistant(partial)
                if added_partial:
                    self.context.messages[-1] = aborted
                else:
                    self.context.messages.append(aborted)
                    await emit(AgentEvent(type="message_start", message=aborted.copy()))
                await emit(AgentEvent(type="message_end", message=aborted))
                state.current_assistant = aborted
            raise
        except Exception as exc:
            # 针对 third-party adapter 的防御性边界：有些 adapter 会直接抛异常，
            # 而不是 yield ModelEvent(type="error")。
            final = _model_error_message(partial or AgentMessage.assistant(""), str(exc), type(exc).__name__)
            if added_partial:
                self.context.messages[-1] = final
            else:
                self.context.messages.append(final)
                await emit(AgentEvent(type="message_start", message=final.copy()))
            if not message_end_emitted:
                await emit(AgentEvent(type="message_end", message=final))
            state.current_assistant = final
            return _AssistantResult(final, _error_info("model", exc))

    def _model_stream(self, messages: Sequence[ProviderMessage], cancellation: CancellationToken):
        stream = self.model.stream
        kwargs: dict[str, Any] = {
            "system_prompt": self.context.system_prompt,
            "messages": messages,
            "tools": self.context.tools,
        }
        if accepts_cancellation(stream):
            kwargs["cancellation"] = cancellation
        return stream(**kwargs)

    async def _fail_truncated_tool_calls(
        self,
        assistant: AgentMessage,
        emit,
        cancellation: CancellationToken,
    ) -> ToolBatchResult:
        results: list[AgentMessage] = []
        for call in assistant.tool_calls:
            cancellation.throw_if_cancelled()
            await emit(
                AgentEvent(
                    type="tool_execution_start",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                )
            )
            message = AgentMessage.tool_result(
                tool_call_id=call.id,
                name=call.name,
                content="Tool call was not executed because the model output hit its token limit; arguments may be truncated.",
                is_error=True,
                details={"stage": "tool_execute"},
            )
            await emit(
                AgentEvent(
                    type="tool_execution_end",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    error=message.text,
                )
            )
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))
            results.append(message)
        return ToolBatchResult(messages=results, terminate=False)

    async def _emit_turn_end(self, state: _RunState, emit, assistant, tool_results) -> None:
        if state.turn_started and not state.turn_end_emitted:
            state.turn_end_emitted = True
            state.turn_started = False
            await emit(AgentEvent(type="turn_end", message=assistant, tool_results=list(tool_results)))

    async def _emit_agent_end(
        self,
        state: _RunState,
        emit,
        *,
        status: RunStatus,
        error_info: AgentErrorInfo | None = None,
    ) -> list[AgentMessage]:
        if not state.agent_end_emitted:
            state.agent_end_emitted = True
            await emit(
                AgentEvent(
                    type="agent_end",
                    messages=list(state.new_messages),
                    status=status,
                    error_info=error_info,
                    error=error_info.message if error_info else None,
                )
            )
        return list(state.new_messages)

    async def _finalize_aborted(self, state: _RunState, emit) -> list[AgentMessage]:
        self._last_error = None
        if not state.agent_started:
            state.agent_started = True
            await emit(AgentEvent(type="agent_start"))
        if state.current_assistant is not None and not any(
            message is state.current_assistant for message in state.new_messages
        ):
            state.new_messages.append(state.current_assistant)
        await self._emit_turn_end(state, emit, state.current_assistant, [])
        return await self._emit_agent_end(state, emit, status="aborted")

    async def _finalize_error(self, state: _RunState, emit, info: AgentErrorInfo) -> list[AgentMessage]:
        self._last_error = info
        if not state.agent_started:
            state.agent_started = True
            await emit(AgentEvent(type="agent_start"))
        await emit(AgentEvent(type="agent_error", error=info.message, error_info=info))
        return await self._emit_agent_end(state, emit, status="error", error_info=info)


def _error_info(stage: ErrorStage, exc: BaseException) -> AgentErrorInfo:
    return AgentErrorInfo(stage=stage, message=str(exc) or type(exc).__name__, exception_type=type(exc).__name__)


def _model_error_message(message: AgentMessage, error_message: str, error_type: Any) -> AgentMessage:
    metadata = dict(message.metadata)
    metadata.update({"error_message": error_message, "error_type": str(error_type or "RuntimeError")})
    # 不完整/错误的 assistant stream 不能留下 Provider 可见、却没有对应 Tool Result 的 tool call。
    return message.copy(stop_reason="error", tool_calls=[], metadata=metadata)


def _aborted_assistant(partial: AgentMessage | None) -> AgentMessage:
    message = partial or AgentMessage.assistant("")
    metadata = dict(message.metadata)
    metadata["aborted"] = True
    return message.copy(stop_reason="aborted", tool_calls=[], metadata=metadata)
