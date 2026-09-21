from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal

from .agent_loop import _AgentLoopMixin, _StageFailure, _error_info
from .providers.messages import ProviderMessage, default_convert_to_llm
from .providers.model import ModelAdapter
from .providers.policy import ProviderRequestOptions
from .runtime.cancellation import CancellationToken, call_with_optional_cancellation
from .runtime.errors import AgentErrorInfo
from .runtime.events import EventStream
from .runtime.transcript import get_current_system_prompt
from .harness.tool import AfterToolCall, BeforeToolCall
from .types import AgentContext, AgentMessage, NextTurnUpdate, QueueMode, TurnResult

TransformContext = Callable[..., Awaitable[list[AgentMessage]] | list[AgentMessage]]
PrepareNextTurn = Callable[
    ...,
    Awaitable[NextTurnUpdate | AgentContext | None] | NextTurnUpdate | AgentContext | None,
]
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
    # 工具批次的默认执行方式：并行执行，或按照模型给出的顺序依次执行。
    tool_execution: Literal["parallel", "sequential"] = "parallel"

    # 决定加载 Steering / Follow-up 消息时的行为：
    # - "one-at-a-time"：每次只取一条消息，让模型在 turn 内只看到一条 Steering / Follow-up 消息。
    # - "all"：一次性取出所有排队的消息，让模型在 turn 内看到所有排队的 Steering / Follow-up 消息。
    steering_mode: QueueMode = "one-at-a-time"
    follow_up_mode: QueueMode = "one-at-a-time"

    # 可选 Hook，用来在调用模型之前，修改“本轮模型能看到的消息”。
    transform_context: TransformContext | None = None

    # 将 Runtime 使用的 AgentMessage 转换为模型适配层使用的 ProviderMessage。
    convert_to_llm: ConvertToLlm = default_convert_to_llm

    # 可选 Hook，在上一轮结束、下一轮开始前，根据 TurnResult 调整 AgentContext。
    prepare_next_turn: PrepareNextTurn | None = None

    # 可选 Hook，在每个 turn 完整结束后，判断是否立即结束当前 Agent run。
    should_stop_after_turn: ShouldStopAfterTurn | None = None

    # 可选消息提供器，在 turn 之间读取 Steering 消息，使其参与下一次模型调用。
    get_steering_messages: MessageProvider | None = None

    # 可选消息提供器，仅在 Agent 原本准备结束时读取 Follow-up 消息并继续运行。
    get_follow_up_messages: MessageProvider | None = None

    # 可选 Hook，在工具参数准备和校验完成后、真正执行前进行权限或策略判断。
    before_tool_call: BeforeToolCall | None = None

    # 可选 Hook，在工具执行完成后、结果事件发出前修改结果、错误状态或终止标记。
    after_tool_call: AfterToolCall | None = None

    provider_request_options: ProviderRequestOptions = dataclass_field(default_factory=ProviderRequestOptions)

    tool_coordinator: Any = None


@dataclass(slots=True)
class _ActiveRun:
    token: CancellationToken
    stream: EventStream[list[AgentMessage]]


class _MessageQueue:
    """Agent 内部使用的轻量消息队列。

    Steering 和 Follow-up 故意使用两条队列，因为它们进入 Loop 的检查点不同。
    """

    def __init__(self) -> None:
        self._items: deque[AgentMessage] = deque()

    def push(self, message: AgentMessage) -> None:
        self._items.append(message)

    def extend(self, messages: Sequence[AgentMessage]) -> None:
        self._items.extend(messages)

    def drain(self, mode: QueueMode = "all") -> list[AgentMessage]:
        if not self._items:
            return []
        if mode == "one-at-a-time":
            return [self._items.popleft()]
        items = list(self._items)
        self._items.clear()
        return items

    def clear(self) -> None:
        self._items.clear()

    def __bool__(self) -> bool:
        return bool(self._items)


class Agent(_AgentLoopMixin):
    """Stateful Agent facade; low-level turn execution lives in ``agent_loop``."""

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
        self._subscribers: list[Callable[..., Any]] = []
        self._provider_request_options = self.config.provider_request_options

    @property
    def messages(self) -> list[AgentMessage]:
        return list(self.context.messages)

    @property
    def system_prompt(self) -> str:
        """Read-only compatibility view derived from transcript system messages."""

        return get_current_system_prompt(self.context.messages)

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

    def clear_steering_queue(self) -> None:
        self._steering.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return bool(self._steering or self._follow_up)

    def subscribe(self, listener: Callable[..., Any]) -> Callable[[], None]:
        """Register an awaited event listener and return an idempotent unsubscribe."""

        self._subscribers.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if not subscribed:
                return
            subscribed = False
            try:
                self._subscribers.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    # 启动一次Agent运行，返回一个 EventStream，stream 里会 yield AgentEvent。
    # 这个 stream 里会 yield AgentEvent，直到 run 完成或被取消
    def stream(self, prompt: str | AgentMessage | Sequence[AgentMessage]) -> EventStream[list[AgentMessage]]:
        # 确认 Agent 当前没有正在执行另一个run，如果已有未完成的 run，会抛出异常，
        # 防止同一个有状态 Agent 被两个主流程同时修改。
        self._ensure_idle() 

        # 把三种输入格式统一转换为：list[AgentMessage]
        # 例如："你好"会变成类似[AgentMessage.user("你好")]
        prompts = self._normalize_prompts(prompt)

        # 为这一次运行创建独立的取消令牌。后续模型请求、工具执行和各类 Hook 都会共享它。
        # 调用 agent.abort() 或取消事件流时，这个令牌会进入 cancelled 状态。
        token = CancellationToken()

        # 清除上一次运行遗留的错误状态。
        self._last_error = None
        self._external_failure = None

        # EventStream 启动后台异步任务后，会把自己的 emit 函数传进来。
        # 相当于执行：await self._run(prompts, emit, token)
        stream = EventStream(
            # prompts 是规范化后的用户消息;emit 用于向事件队列发送 AgentEvent；token 用于传播取消信号
            # _run() 中执行类似下面的操作：await emit(AgentEvent(type="agent_start"));await emit(AgentEvent(type="message_update", ...))
            lambda emit: self._run(prompts, emit, token),
            # 当外部取消 EventStream 时，同时设置 Agent 的取消令牌，让模型、工具和Hook 都能感知取消，而不只是取消最外层事件消费者。
            on_cancel=lambda _: token.cancel(),
        )

        # 记录当前正在运行的任务。这个状态主要用于：is_running 查询；阻止重入；agent.abort()等
        self._active_run = _ActiveRun(token=token, stream=stream)

        # 当 stream 完成时，清理 _active_run 状态。
        stream.add_done_callback(lambda completed: self._clear_active_run(completed))

        return stream

    async def run(self, prompt: str | AgentMessage | Sequence[AgentMessage]) -> list[AgentMessage]:
        return await self.stream(prompt).result()

    def continue_stream(self) -> EventStream[list[AgentMessage]]:
        self._ensure_idle()
        if not self.context.messages:
            raise ValueError("Cannot continue: no messages in context")
        if self.context.messages[-1].role == "assistant" and not self.has_queued_messages():
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

    # 检查被调用函数是否接受 cancellation 参数，接受才传入。
    # 同时兼容同步函数和异步函数，并统一用 await 返回结果。
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
            self._steering.extend(list(messages))
        return self._steering.drain(self.config.steering_mode)

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
            self._follow_up.extend(list(messages))
        return self._follow_up.drain(self.config.follow_up_mode)
