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
from .types import (
    AgentEvent,
    AgentMessage,
    ModelEvent,
    NextTurnUpdate,
    PrepareRequestContext,
    RequestUpdate,
    ToolBatchResult,
    TurnDecision,
    TurnResult,
)


@dataclass(slots=True)
class _RunState:
    new_messages: list[AgentMessage]
    agent_started: bool = False
    turn_started: bool = False
    turn_end_emitted: bool = False
    agent_end_emitted: bool = False
    current_assistant: AgentMessage | None = None
    assistant_message_started: bool = False
    assistant_message_end_emitted: bool = False
    agent_error_emitted: bool = False
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
            self._reduce_event(event)
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
            if event.type == "agent_end":
                self._reduce_event(event, final=True)

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

        # A turn contains one provider request plus its tool batch.  The next
        # request can be required by tools, steering, follow-up, or an explicit
        # finish_turn=continue decision.
        await emit(AgentEvent(type="turn_start"))
        for message in initial_messages:
            cancellation.throw_if_cancelled()
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))

        previous_turn: TurnResult | None = None
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

        request_required = True
        explicit_continuation = False
        while True:
            while request_required or pending:
                prepared_messages: list[AgentMessage] = []

                # If there is a previous turn, this iteration starts a new
                # turn, even for a context-only continuation request.
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
                    state.current_assistant = None
                    state.assistant_message_started = False
                    state.assistant_message_end_emitted = False
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
                            assistant_message=assistant,
                            emit=emit,
                            cancellation=cancellation,
                        )
                    tool_results = batch.messages
                    self.context.messages.extend(tool_results)
                    state.new_messages.extend(tool_results)
                    has_more_tool_calls = not batch.terminate and not batch.aborted

                turn_result = TurnResult(
                    message=assistant,
                    tool_results=list(tool_results),
                    context=self.context,
                    new_messages=state.new_messages,
                )

                # Cancellation during tool preparation/execution is a hard
                # abort.  Do not let finish_turn revive that run.
                if batch is not None and batch.aborted:
                    return await self._finalize_aborted(state, emit)

                # should_stop_after_turn is the historical spelling of an end
                # decision.  AgentConfig rejects configuring both hooks.
                decision: TurnDecision | None = None
                finish_requested_continue = False
                if self.config.should_stop_after_turn:
                    cancellation.throw_if_cancelled()
                    try:
                        should_stop = await self._call(
                            self.config.should_stop_after_turn,
                            turn_result,
                            cancellation=cancellation,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _StageFailure(_error_info("should_stop_after_turn", exc)) from exc
                    cancellation.throw_if_cancelled()
                    if should_stop:
                        decision = TurnDecision("end")
                elif self.config.finish_turn:
                    cancellation.throw_if_cancelled()
                    try:
                        decision_value = await self._call(
                            self.config.finish_turn,
                            turn_result,
                            cancellation=cancellation,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _StageFailure(_error_info("finish_turn", exc)) from exc
                    cancellation.throw_if_cancelled()
                    if decision_value is not None and not isinstance(decision_value, TurnDecision):
                        raise _StageFailure(
                            _error_info("finish_turn", TypeError("finish_turn must return TurnDecision | None"))
                        )
                    if decision_value is not None:
                        decision = decision_value
                    if decision is not None and decision.action not in {"continue", "end"}:
                        raise _StageFailure(
                            _error_info("finish_turn", ValueError(f"Unknown turn action: {decision.action!r}"))
                        )
                    finish_requested_continue = decision_value is not None and decision.action == "continue"

                # finish_turn is intentionally before turn_end so the emitted
                # lifecycle records the completed turn before scheduling.
                await emit(
                    AgentEvent(
                        type="finish_turn",
                        message=assistant,
                        tool_results=list(tool_results),
                        turn_result=turn_result,
                        turn_decision=decision,
                    )
                )
                await self._emit_turn_end(state, emit, assistant, tool_results)
                previous_turn = turn_result

                if decision is not None and decision.action == "end":
                    return await self._emit_agent_end(state, emit, status="completed")

                # Current-turn steering is checked only after all tool results
                # have been committed.  A continue decision is consumed by the
                # first naturally required request, if one exists.
                pending = await self._drain_steering(cancellation)
                request_required = has_more_tool_calls or bool(pending)
                explicit_continuation = finish_requested_continue and not request_required

            # Follow-up is intentionally later than steering.  It satisfies an
            # explicit continuation without producing an extra empty request.
            follow_up = await self._drain_follow_up(cancellation)
            if follow_up:
                pending = follow_up
                request_required = True
                explicit_continuation = False
                continue
            if explicit_continuation:
                pending = []
                request_required = True
                explicit_continuation = False
                continue
            break

        return await self._emit_agent_end(state, emit, status="completed")

    async def _stream_assistant(self, emit, cancellation: CancellationToken, state: _RunState) -> _AssistantResult:
        # prepare_request is deliberately outside the adapter.  It runs once
        # per logical request, before context transformation/conversion, so
        # adapter retries cannot trigger it a second time.
        cancellation.throw_if_cancelled()
        await emit(AgentEvent(type="prepare_request"))
        context_replaced = False
        if self.config.prepare_request:
            request_context = PrepareRequestContext(
                context=self.context,
                model=self.model,
                request_options=self._provider_request_options,
            )
            try:
                request_update = await self._call(
                    self.config.prepare_request,
                    request_context,
                    cancellation=cancellation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _StageFailure(_error_info("prepare_request", exc)) from exc
            cancellation.throw_if_cancelled()
            if request_update is not None and not isinstance(request_update, RequestUpdate):
                raise _StageFailure(
                    _error_info("prepare_request", TypeError("prepare_request must return RequestUpdate | None"))
                )
            if request_update is not None:
                if request_update.context is not None:
                    self.context = request_update.context
                    context_replaced = True
                if request_update.model is not None:
                    self.model = request_update.model
                if request_update.request_options is not None:
                    self._provider_request_options = merge_provider_request_options(
                        self._provider_request_options, request_update.request_options
                    )

        if context_replaced:
            await self._reconcile_request_tools(state, emit, cancellation)

        # transform_context 只决定“本轮模型看到什么”，默认不替换 Runtime 保存的完整 history。
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
            except _StageFailure:
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
        except _StageFailure:
            raise
        except Exception as exc:
            raise _StageFailure(_error_info("convert_to_llm", exc)) from exc
        cancellation.throw_if_cancelled()

        partial: AgentMessage | None = None
        added_partial = False
        model_started = True
        state.current_assistant = None
        state.assistant_message_started = False
        state.assistant_message_end_emitted = False

        streaming_types = {
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
            "update",
        }

        def ensure_started(snapshot: AgentMessage) -> None:
            nonlocal added_partial
            if not added_partial:
                self.context.messages.append(snapshot)
                added_partial = True

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
                    ensure_started(partial)
                    if not state.assistant_message_started:
                        await emit(AgentEvent(type="message_start", message=partial.copy()))
                        state.assistant_message_started = True
                elif event.type in streaming_types:
                    ensure_started(partial)
                    self.context.messages[-1] = partial
                    if not state.assistant_message_started:
                        await emit(AgentEvent(type="message_start", message=partial.copy()))
                        state.assistant_message_started = True
                    snapshot = partial.copy()
                    await emit(
                        AgentEvent(
                            type="message_update",
                            message=snapshot,
                            partial=snapshot,
                            model_event=event,
                        )
                    )
                elif event.type in {"done", "error"}:
                    if event.type == "error" or partial.stop_reason == "error":
                        error_message = event.error or event.error_message or str(
                            partial.metadata.get("error_message", "Model request failed.")
                        )
                        partial = _model_error_message(
                            partial,
                            error_message,
                            event.error_type or partial.metadata.get("error_type"),
                            stage="model",
                        )
                    elif partial.stop_reason == "aborted":
                        partial = _aborted_assistant(partial, stage="model")
                    await self._finalize_assistant_message(state, emit, partial)
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
            final = _model_error_message(
                partial or AgentMessage.assistant(""),
                str(error),
                type(error).__name__,
                stage="model",
            )
            await self._finalize_assistant_message(state, emit, final)
            return _AssistantResult(final, _error_info("model", error))
        except asyncio.CancelledError:
            cancellation.cancel()
            if model_started and not state.assistant_message_end_emitted:
                aborted = _aborted_assistant(partial, stage="model")
                await self._finalize_assistant_message(state, emit, aborted)
            raise
        except _StageFailure:
            raise
        except Exception as exc:
            # 针对 third-party adapter 的防御性边界：有些 adapter 会直接抛异常，
            # 而不是 yield ModelEvent(type="error").
            final = _model_error_message(
                partial or AgentMessage.assistant(""),
                str(exc),
                type(exc).__name__,
                stage="model",
            )
            await self._finalize_assistant_message(state, emit, final)
            return _AssistantResult(final, _error_info("model", exc))

    async def _reconcile_request_tools(self, state: _RunState, emit, cancellation: CancellationToken) -> None:
        """Persist executable-tool changes before projecting a provider request."""

        cancellation.throw_if_cancelled()
        deltas = declare_tool_changes(self.context, [])
        for message in deltas:
            # This is a request-time projection of the replacement context.  It
            # must replay after every historical message, including older system
            # tool deltas, so the final transcript state matches context.tools.
            self.context.messages.append(message)
            if not any(existing is message for existing in state.new_messages):
                state.new_messages.append(message)
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))
        cancellation.throw_if_cancelled()

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
                    result=message,
                    is_error=True,
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
        self._state_error_message = None
        if not state.agent_started:
            state.agent_started = True
            await emit(AgentEvent(type="agent_start"))
        if not self._has_closed_failure_assistant(state):
            await self._begin_failure_turn(state, emit)
        assistant = await self._ensure_failure_assistant(state, emit, aborted=True)
        await self._emit_turn_end(state, emit, assistant, [])
        return await self._emit_agent_end(state, emit, status="aborted")

    async def _finalize_error(self, state: _RunState, emit, info: AgentErrorInfo) -> list[AgentMessage]:
        state.finalizing = True
        self._last_error = info
        if not state.agent_started:
            state.agent_started = True
            await emit(AgentEvent(type="agent_start"))
        if not self._has_closed_failure_assistant(state):
            await self._begin_failure_turn(state, emit)
        assistant = await self._ensure_failure_assistant(state, emit, info=info)
        await self._emit_turn_end(state, emit, assistant, [])
        if not state.agent_error_emitted:
            state.agent_error_emitted = True
            await emit(AgentEvent(type="agent_error", error=info.message, error_info=info))
        return await self._emit_agent_end(state, emit, status="error", error_info=info)

    @staticmethod
    def _has_closed_failure_assistant(state: _RunState) -> bool:
        return (
            not state.turn_started
            and state.assistant_message_end_emitted
            and state.current_assistant is not None
            and state.current_assistant.stop_reason in {"error", "aborted"}
        )

    async def _begin_failure_turn(self, state: _RunState, emit) -> None:
        if state.turn_started:
            return
        state.turn_started = True
        state.turn_end_emitted = False
        state.current_assistant = None
        state.assistant_message_started = False
        state.assistant_message_end_emitted = False
        await emit(AgentEvent(type="turn_start"))

    async def _finalize_assistant_message(self, state: _RunState, emit, message: AgentMessage) -> AgentMessage:
        """Replace/append one canonical assistant and close its message lifecycle."""

        current = state.current_assistant
        replaced = False
        if current is not None:
            for index, existing in enumerate(self.context.messages):
                if existing is current:
                    self.context.messages[index] = message
                    replaced = True
                    break
        if not replaced and state.assistant_message_started:
            for index in range(len(self.context.messages) - 1, -1, -1):
                if self.context.messages[index].role == "assistant":
                    self.context.messages[index] = message
                    replaced = True
                    break
        if not replaced:
            self.context.messages.append(message)
        state.current_assistant = message
        if not state.assistant_message_started:
            await emit(AgentEvent(type="message_start", message=message.copy()))
            state.assistant_message_started = True
        if not state.assistant_message_end_emitted:
            await emit(AgentEvent(type="message_end", message=message))
            state.assistant_message_end_emitted = True
        return message

    async def _ensure_failure_assistant(
        self,
        state: _RunState,
        emit,
        *,
        info: AgentErrorInfo | None = None,
        aborted: bool = False,
    ) -> AgentMessage:
        """Normalize one active/partial assistant and close its lifecycle."""

        current = state.current_assistant if (
            state.turn_started
            and (
                not state.assistant_message_end_emitted
                or state.current_assistant is not None
                and state.current_assistant.stop_reason in {"error", "aborted"}
            )
        ) else (
            state.current_assistant
            if state.current_assistant is not None
            and state.current_assistant.stop_reason in {"error", "aborted"}
            else None
        )
        if current is None:
            state.assistant_message_started = False
            state.assistant_message_end_emitted = False
        if aborted:
            normalized = _aborted_assistant(current, stage=info.stage if info else "runtime")
        else:
            assert info is not None
            normalized = _model_error_message(
                current or AgentMessage.assistant(""),
                info.message,
                info.exception_type,
                stage=info.stage,
            )

        state.current_assistant = current
        await self._finalize_assistant_message(state, emit, normalized)
        if not any(message is normalized for message in state.new_messages):
            # If this is a replacement of an existing partial, remove that
            # exact object from the per-run result before adding the final one.
            state.new_messages[:] = [message for message in state.new_messages if message is not current]
            state.new_messages.append(normalized)

        return normalized


def _error_info(stage: ErrorStage, exc: BaseException) -> AgentErrorInfo:
    return AgentErrorInfo(stage=stage, message=str(exc) or type(exc).__name__, exception_type=type(exc).__name__)


def _model_error_message(
    message: AgentMessage,
    error_message: str,
    error_type: Any,
    *,
    stage: str = "model",
) -> AgentMessage:
    metadata = dict(message.metadata)
    metadata.update(
        {
            "error_message": error_message,
            "error_type": str(error_type or "RuntimeError"),
            "stage": stage,
        }
    )
    # 不完整/错误的 assistant stream 不能留下 Provider 可见、却没有对应 Tool Result 的 tool call。
    return message.copy(stop_reason="error", is_error=True, tool_calls=[], metadata=metadata)


def _aborted_assistant(partial: AgentMessage | None, *, stage: str = "runtime") -> AgentMessage:
    message = partial or AgentMessage.assistant("")
    metadata = dict(message.metadata)
    metadata.update(
        {
            "aborted": True,
            "error_message": "Operation aborted",
            "error_type": "CancelledError",
            "stage": stage,
        }
    )
    return message.copy(stop_reason="aborted", is_error=True, tool_calls=[], metadata=metadata)
