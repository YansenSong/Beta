from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .providers.messages import ProviderMessage
from .providers.model import accepts_request_options
from .providers.policy import ProviderRequestOptions, merge_provider_request_options
from .runtime.cancellation import CancellationToken, accepts_cancellation
from .runtime.errors import AgentErrorInfo, ErrorStage, RunStatus
from .runtime.transcript import collapse_transcript, declare_tool_changes, get_current_system_prompt
from .harness.tool import ToolRuntime
from .types import AgentEvent, AgentMessage, ModelEvent, NextTurnUpdate, ToolBatchResult, TurnResult


@dataclass(slots=True)
class _RunState:
    new_messages: list[AgentMessage]
    agent_started: bool = False
    turn_started: bool = False
    turn_end_emitted: bool = False
    agent_end_emitted: bool = False
    current_assistant: AgentMessage | None = None
    failed_subscribers: set[int] = dataclass_field(default_factory=set)
    finalizing: bool = False


@dataclass(slots=True)
class _AssistantResult:
    message: AgentMessage
    error_info: AgentErrorInfo | None = None


class _StageFailure(Exception):
    def __init__(self, info: AgentErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


class _AgentLoopMixin:
    """Low-level agent turn loop, model streaming, tool dispatch, and finalization."""

    async def _run(
        self,
        prompts: list[AgentMessage],
        emit,
        cancellation: CancellationToken,
    ) -> list[AgentMessage]:
        state = _RunState(new_messages=list(prompts))

        async def publish(event: AgentEvent) -> None:
            for listener in list(self._subscribers):
                if id(listener) in state.failed_subscribers:
                    continue
                try:
                    await self._call(listener, event, cancellation=cancellation)
                except asyncio.CancelledError:
                    if cancellation.cancelled:
                        state.failed_subscribers.add(id(listener))
                        continue
                    raise
                except Exception as exc:
                    state.failed_subscribers.add(id(listener))
                    if not state.finalizing:
                        raise _StageFailure(_error_info("event_listener", exc)) from exc
            await emit(event)

        try:
            return await self._run_impl(prompts, publish, cancellation, state)
        except asyncio.CancelledError:
            cancellation.cancel()
            if self._external_failure is not None:
                info = self._external_failure
                self._external_failure = None
                return await self._finalize_error(state, publish, info)
            try:
                return await self._finalize_aborted(state, publish)
            except _StageFailure as failure:
                return await self._finalize_error(state, publish, failure.info)
        except _StageFailure as failure:
            return await self._finalize_error(state, publish, failure.info)
        except Exception as exc:
            return await self._finalize_error(state, publish, _error_info("runtime", exc))

    async def _run_impl(self, prompts, emit, cancellation, state) -> list[AgentMessage]:
        state.agent_started = True
        await emit(AgentEvent(type="agent_start"))
        cancellation.throw_if_cancelled()

        # Reconcile against prompts before making them part of canonical history:
        # a prompt received now is interpreted under the current executable tools.
        initial_messages = declare_tool_changes(self.context, prompts)
        state.new_messages = list(initial_messages)
        self.context.messages.extend(initial_messages)
        state.turn_started = True

        # 一次turn是只包含一轮模型的调用（看到消息-工具调用-工具返回结果）
        # 工具返回结果-模型回复为新的turn。一个run中可能包含多个turn
        await emit(AgentEvent(type="turn_start"))
        for message in initial_messages:
            cancellation.throw_if_cancelled()
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))

        previous_turn: TurnResult | None = None
        # Steering 即使在 run 启动前已经排队，也应该参与下一次模型调用。
        pending = await self._drain_steering(cancellation)
        if (
            not pending
            and not prompts
            and self.context.messages
            and self.context.messages[-1].role == "assistant"
        ):
            # continue_stream() may resume a completed assistant turn when a
            # queued follow-up is the only message waiting to be delivered.
            pending = await self._drain_follow_up(cancellation)

        # 双层循环是一个关键设计：
        # - 内层循环处理“当前任务仍需继续”的 Tool Call / Steering；
        # - 外层循环只在任务本来要结束时，再检查更晚到达的 Follow-up。
        while True:
            has_more_tool_calls = True
            while has_more_tool_calls or pending:
                prepared_messages: list[AgentMessage] = []

                # 如果上一轮有结果，且配置了 prepare_next_turn Hook，则在下一轮开始前调用它。
                if previous_turn is not None:
                    if self.config.prepare_next_turn:
                        cancellation.throw_if_cancelled()
                        try:
                            next_update = await self._call(
                                self.config.prepare_next_turn,
                                previous_turn,
                                cancellation=cancellation,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            raise _StageFailure(_error_info("prepare_next_turn", exc)) from exc
                        cancellation.throw_if_cancelled()
                        if isinstance(next_update, NextTurnUpdate):
                            if next_update.context is not None:
                                self.context = next_update.context
                            if next_update.model is not None:
                                self.model = next_update.model
                            if next_update.request_options is not None:
                                self._provider_request_options = merge_provider_request_options(
                                    self._provider_request_options, next_update.request_options
                                )
                            prepared_messages = list(next_update.messages)
                        elif next_update is not None:
                            # Keep the historical AgentContext return contract.
                            self.context = next_update
                    if not pending:
                        pending = await self._drain_steering(cancellation)
                    pending = [*prepared_messages, *pending]
                    state.turn_started = True
                    state.turn_end_emitted = False
                    await emit(AgentEvent(type="turn_start"))

                if pending:
                    cancellation.throw_if_cancelled()
                pending = declare_tool_changes(self.context, pending)
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
                            coordinator=self.config.tool_coordinator,
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

        # Apply the same compatibility projection to the transformed view so
        # temporary system instructions affect this request without mutating the
        # durable transcript. Runtime-executable tools still come only from context.tools.
        system_prompt, runtime_messages = collapse_transcript(runtime_messages)

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
            model_stream = self._model_stream(provider_messages, cancellation, system_prompt=system_prompt)
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

    def _model_stream(
        self,
        messages: Sequence[ProviderMessage],
        cancellation: CancellationToken,
        *,
        system_prompt: str | None = None,
    ):
        stream = self.model.stream
        kwargs: dict[str, Any] = {
            "system_prompt": (
                get_current_system_prompt(self.context.messages) if system_prompt is None else system_prompt
            ),
            "messages": messages,
            "tools": self.context.tools,
        }
        if accepts_cancellation(stream):
            kwargs["cancellation"] = cancellation
        if accepts_request_options(self.model):
            kwargs["request_options"] = ProviderRequestOptions(
                session_id=self._provider_request_options.session_id,
                timeout_seconds=self._provider_request_options.timeout_seconds,
                retry=self._provider_request_options.retry,
                headers=dict(self._provider_request_options.headers),
                metadata=dict(self._provider_request_options.metadata),
                transport=self._provider_request_options.transport,
                reasoning=self._provider_request_options.reasoning,
                thinking_budget_tokens=self._provider_request_options.thinking_budget_tokens,
            )
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
            await emit(
                AgentEvent(
                    type="agent_end",
                    messages=list(state.new_messages),
                    status=status,
                    error_info=error_info,
                    error=error_info.message if error_info else None,
                )
            )
            state.agent_end_emitted = True
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
        state.finalizing = True
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
