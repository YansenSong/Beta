# Beta Agent Runtime P0 完善实现计划

> 目标读者：后续直接执行改造的 Codex  
> 适用仓库：`YansenSong/Beta` 当前 `main`  
> 重要约束：**本文档必须能够在完全看不到 pi 源码的情况下独立执行。**  
> 本轮只处理三个最高优先级问题：
>
> 1. 端到端 Cancellation / Abort
> 2. Agent Runtime Message 与 Provider Message 分层
> 3. Runtime / Model / Hook / Tool Failure 归一化
>
> 不在本轮实现：自动 compaction、ResourceLoader、更多 provider、find/ls、新 TUI/RPC、完整 subagent 重构、模型注册中心。

---

## 0. 执行原则

这次改造是 **Runtime 地基升级**，不是重写 Agent Loop。

当前 Beta 已经具备并且必须保留以下语义：

- 双层 loop：
  - 内层处理 tool call 与 steering；
  - 外层只在 Agent 原本准备结束时处理 follow-up。
- steering 只在完整 turn 结束后进入下一轮。
- parallel tool batch：
  - lookup / argument preparation / validation / before hook 按模型 source order；
  - 真正的 tool execute 可并行；
  - `tool_execution_end` 可按 completion order；
  - 最终 Tool Result message 必须按 assistant source order 写回 history。
- 遇到 `stop_reason == "length"` 的 tool calls 不执行，而是返回错误结果。
- Tool failure 应转成模型可见 Tool Result，而不是直接炸毁 Agent loop。
- `transform_context` 只决定当前 provider call 看见的上下文，不应默认覆盖完整 runtime history。
- SessionTree 继续采用 append-only + parent/leaf 的思路。
- ExtensionRunner 目前对 extension handler 的错误隔离行为应继续保留。

**禁止为了实现本计划而把 `agent.py`、`tools.py` 整体推倒重写。**  
优先做小步、可测试、可回滚的结构性修改。

---

# 1. 当前问题与最终目标

完成本计划后，Beta Runtime 至少满足以下三个核心不变量。

## 1.1 Cancellation 不变量

调用：

```python
agent.abort()
```

必须能够结束当前 active run，并最终满足：

```text
Agent.abort()
    ↓
active run 被标记为 cancelled
    ↓
正在进行的 provider stream / hook / tool task 收到取消
    ↓
并行 tool 子任务全部停止
    ↓
bash 等外部子进程被终止
    ↓
消息历史保持协议可继续使用
    ↓
发出唯一一次 agent_end(status="aborted")
    ↓
wait_for_idle() 返回
    ↓
同一个 Agent 可以开始下一次 run
```

不能出现：

- Agent UI 已经认为取消，但底层 HTTP 仍在跑；
- bash 子进程继续存活；
- EventStream 永远不结束；
- tool-call assistant message 已经写入 history，但缺失对应 Tool Result，导致后续 continue 请求协议非法；
- abort 后 Agent 永远处于 busy；
- abort 导致裸 `asyncio.CancelledError` 从正常 Agent API 冒给调用者。

---

## 1.2 Message Boundary 不变量

Agent 内部消息和真正发给 provider 的消息必须是两套明确的数据模型。

最终数据流：

```text
Session / Extensions / Agent Runtime
             │
             ▼
       AgentMessage[]
             │
      transform_context
             │
             ▼
       AgentMessage[]
             │
       convert_to_llm
             │
             ▼
      ProviderMessage[]
             │
             ▼
        ModelAdapter
             │
             ▼
     provider-specific payload
```

必须满足：

- Session 只持久化 `AgentMessage`。
- Extension 只处理 `AgentMessage`。
- provider adapter 不直接依赖 Agent runtime-only metadata。
- `transform_context` 的输入输出都是 AgentMessage。
- `convert_to_llm` 是 AgentMessage → ProviderMessage 的唯一标准边界。
- runtime-only 信息，例如：
  - delivery / steering / follow-up 标记；
  - session metadata；
  - compaction metadata；
  - extension metadata；
  - `is_error`；
  - timestamp；
  - UI-only/custom message；

  不应在没有显式转换规则时泄漏到 provider payload。
- 老 session JSONL 必须能够继续加载。

---

## 1.3 Failure Normalization 不变量

“业务运行失败”不得等价于“Python task 意外炸掉”。

下面这些失败必须有明确、稳定的归属：

```text
Model/provider failure
    → assistant stop_reason="error"
    → turn_end
    → agent_error
    → agent_end(status="error")

Agent abort
    → stop_reason="aborted" 或 aborted tool result
    → turn_end（若当前 turn 已开始）
    → agent_end(status="aborted")

Tool execute failure
    → is_error=True 的 Tool Result
    → Agent 可以继续下一轮让模型自修复

before_tool_call failure
    → fail closed
    → 当前 tool call 变成 error Tool Result
    → 不执行目标 tool

after_tool_call failure
    → 当前 tool call 变成 error Tool Result
    → 不炸掉整个 Agent

transform_context / convert_to_llm / prepare_next_turn /
should_stop_after_turn / queue provider failure
    → agent_error
    → agent_end(status="error")
    → EventStream 正常结束
```

只有真正的程序错误或不可恢复的系统级异常才允许继续向上抛。

**不要 `except BaseException`。**  
`KeyboardInterrupt`、`SystemExit` 等不能被 Runtime 吞掉。  
`asyncio.CancelledError` 必须单独处理。

---

# 2. 建议新增 / 修改的文件

建议最终结构：

```text
src/beta_agent/
├── __init__.py
├── agent.py
├── cancellation.py          # NEW
├── errors.py                # NEW
├── events.py
├── messages.py              # NEW：AgentMessage + content blocks
├── provider_messages.py     # NEW：ProviderMessage + default converter
├── model.py
├── tools.py
├── types.py
├── session.py
├── compaction.py
├── adapters/
│   └── openai_compatible.py
└── extensions/
    ├── bridge.py
    ├── runner.py
    └── types.py

tests/
├── test_agent_loop.py
├── test_cancellation.py            # NEW
├── test_failure_normalization.py   # NEW
├── test_message_conversion.py      # NEW
├── test_session.py
├── test_parallel_tools.py
├── test_extension_runtime.py
├── test_extension_composition.py
├── test_coding_assembly.py
└── test_coding_e2e.py
```

不强制文件名完全一致，但职责边界必须保持。

---

# 3. Workstream A：端到端 Cancellation / Abort

---

## 3.1 新增 `CancellationToken`

新增：

```text
src/beta_agent/cancellation.py
```

建议接口：

```python
from __future__ import annotations

import asyncio


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()

    async def wait(self) -> None:
        await self._event.wait()
```

要求：

- `cancel()` 幂等。
- token 只是“合作式状态”，**不能用 token 替代真正的 task cancellation**。
- `Agent.abort()` 应同时：
  1. `token.cancel()`
  2. cancel active root task / EventStream task

原因：只设置 Event，无法强制结束一个没有主动检查 token 的 provider/hook/tool。

---

## 3.2 EventStream 增加明确的生命周期控制

当前 `EventStream` 内部已经持有 `_task`，但外部没有正式控制能力。

建议新增：

```python
class EventStream(Generic[T]):
    ...

    @property
    def done(self) -> bool:
        return self._task.done()

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()

    async def wait(self) -> None:
        try:
            await self._task
        except asyncio.CancelledError:
            pass
```

可以额外支持：

```python
def add_done_callback(...)
```

或构造参数：

```python
EventStream(runner, on_done=..., on_cancel=...)
```

用于 Agent / ExtensionHost 清理 active run。

### 关键要求

`EventStream` 自己不负责理解 Agent domain error。

也就是说：

```text
EventStream
```

仍然只是通用 async event stream。

**正常的 Agent runtime/provider/tool/hook failure 应由 Agent 层归一化，不能依赖 EventStream 猜测错误语义。**

---

## 3.3 Agent 增加 Active Run 状态

当前 `_run_lock` 会让第二个 run 等待第一个 run，而不是明确拒绝。

本次应改成：

> 同一个 Agent 同时只能存在一个 active run。  
> 第二次 `run/stream/continue_stream` 必须立即报错，而不是悄悄排队。

理由：

- `abort()` 必须有唯一目标；
- 两个 run 排队会让用户以为取消的是全部任务；
- steering/follow-up 本来就是 active run 期间与 Agent 交互的正规方式。

建议 Agent 新增：

```python
@dataclass(slots=True)
class _ActiveRun:
    token: CancellationToken
    stream: EventStream[list[AgentMessage]]
```

Agent 字段：

```python
self._active_run: _ActiveRun | None = None
self._last_error: AgentErrorInfo | None = None
```

公共 API：

```python
@property
def is_running(self) -> bool: ...

@property
def last_error(self) -> AgentErrorInfo | None: ...

def abort(self) -> None: ...

async def wait_for_idle(self) -> None: ...
```

### `stream()` 行为

伪代码：

```python
def stream(self, prompt):
    if self.is_running:
        raise RuntimeError(
            "Agent is already processing. Use steer()/follow_up() "
            "or wait_for_idle()."
        )

    token = CancellationToken()

    stream = EventStream(
        lambda emit: self._run(prompts, emit, token),
        on_done=lambda: self._clear_active_run(stream),
    )

    self._active_run = _ActiveRun(token=token, stream=stream)
    return stream
```

### `abort()`

```python
def abort(self) -> None:
    active = self._active_run
    if active is None:
        return

    active.token.cancel()
    active.stream.cancel()
```

### `wait_for_idle()`

没有 active run 时立即返回。

有 active run 时等待当前 stream 完整 settlement，包括最终事件写入。

---

## 3.4 Cancellation 必须贯穿 Model / Hook / Tool

### ModelAdapter

升级协议：

```python
class ModelAdapter(Protocol):
    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ProviderMessage],
        tools: Sequence[object],
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        ...
```

`ScriptedModelAdapter` 与 `OpenAICompatibleAdapter` 同步更新。

如果担心外部已有自定义 adapter，可暂时允许：

```python
cancellation: CancellationToken | None = None
```

但 Agent 第一方调用必须传入。

---

### ToolExecutionContext

增加：

```python
@dataclass(slots=True)
class ToolExecutionContext:
    tool_call_id: str
    tool_name: str
    cancellation: CancellationToken
    _emit: Emit
```

长耗时 tool 可以：

```python
ctx.cancellation.throw_if_cancelled()
```

---

### Hooks

建议所有 AgentConfig hook 支持 cancellation。

目标签名示意：

```python
transform_context(messages, cancellation)
prepare_next_turn(turn_result, cancellation)
should_stop_after_turn(turn_result, cancellation)
get_steering_messages(cancellation)
get_follow_up_messages(cancellation)
before_tool_call(call, args, context, cancellation)
after_tool_call(call, args, result, is_error, context, cancellation)
```

### 兼容策略

Beta 目前是 `0.1.0`，可以直接升级第一方 hook。

但为了不让已有用户扩展瞬间全部坏掉，建议 `_call()` 扩展成一个兼容调用器：

```text
如果 callable 明确接受 cancellation：
    传 cancellation keyword
否则：
    使用旧签名调用
```

不要简单用 `try TypeError -> retry old signature`，因为 hook 内部真正抛出的 TypeError 会被误判成签名不兼容。

应使用 `inspect.signature()` 判断：

- 是否存在名为 `cancellation` 的参数；
- 是否接受 `**kwargs`。

把判断逻辑做成内部 helper，并加单测。

---

## 3.5 Agent 在各 cancellation point 主动检查

至少在以下位置执行：

```python
cancellation.throw_if_cancelled()
```

- 第一次 provider request 前；
- `transform_context` 前后；
- `convert_to_llm` 前后；
- `prepare_next_turn` 前后；
- steering/follow-up provider 前后；
- tool batch preflight 前；
- 每个 tool call preflight 前；
- tool execute 前；
- should_stop hook 前。

task cancellation 负责“强制打断”，token check 负责：

- 在任务刚开始时检测预先取消；
- 防止取消后继续启动新工作；
- 为自定义同步/异步组件提供合作式 API。

---

## 3.6 Tool batch 的取消必须保持消息协议完整

这是本计划里最重要的取消边界之一。

### 问题

假设 assistant 一次返回：

```text
tool A
tool B
tool C
```

这条 assistant message 已经写入 history。

如果 parallel tool batch 执行到一半时 abort：

```text
A finished
B cancelled
C cancelled
```

如果 history 只写入 A 的 Tool Result：

```text
assistant(tool A/B/C)
tool_result(A)
```

后续继续 session 时，很多 provider 会认为 B/C 没有对应 tool result，协议可能非法。

### 目标语义

**只要一条 assistant tool-call message 已经正式提交到 runtime history，则该 message 中的每一个 tool call 最终都必须对应一个 Tool Result message。**

所以 abort 时：

```text
A → 真实结果
B → aborted error result
C → aborted error result
```

最终按 source order 写入：

```text
tool_result(A)
tool_result(B aborted)
tool_result(C aborted)
```

---

## 3.7 ToolBatchResult 增加 aborted 状态

修改：

```python
@dataclass(slots=True)
class ToolBatchResult:
    messages: list[AgentMessage]
    terminate: bool = False
    aborted: bool = False
```

Agent 收到：

```python
if batch.aborted:
    # turn_end
    # agent_end(status="aborted")
    # 不再发起下一轮 provider call
```

### aborted Tool Result

建议：

```python
AgentMessage.tool_result(
    ...,
    content="Operation aborted",
    is_error=True,
    aborted=True,
)
```

其中 `aborted=True` 放在 runtime metadata 中。

---

## 3.8 parallel tool cancellation 实现约束

当前使用 `asyncio.gather()`。

改造后：

- 保存每个 source-order call 对应的 task；
- root cancellation 时：
  - cancel 所有尚未完成的 tasks；
  - `await asyncio.gather(..., return_exceptions=True)` 回收全部 task；
  - 已完成调用保留真实结果；
  - 被取消调用生成 aborted result；
  - 未开始或还在 preflight 的调用也生成 aborted result；
- 每个 `tool_execution_start` 最多一次；
- 每个已 start 的调用必须对应一个 `tool_execution_end`；
- 每个 assistant tool call 最终都必须对应一个 Tool Result message；
- Tool Result message 仍然按 source order commit。

不要因为取消破坏当前 parallel ordering contract。

---

## 3.9 sequential tool cancellation

若：

```text
A finished
B executing → abort
C not started
```

最终也应得到：

```text
A real result
B aborted result
C aborted result
```

C 可以发：

```text
tool_execution_start
tool_execution_end(error="Operation aborted")
```

这样事件协议和 tool-result pairing 都完整。

---

## 3.10 bash cancellation

现有 `bash.py` 已经在 `CancelledError` 时：

- terminate process；
- POSIX 下尝试杀 process group；
- 最终重新 raise CancelledError。

保留这个基础。

新增：

```python
ctx.cancellation.throw_if_cancelled()
```

至少在 spawn 前检查。

验收时必须验证：

- abort 以后 shell process 被结束；
- `wait_for_idle()` 不挂；
- tool result 被标记 aborted；
- 可以立即运行下一次 Agent request。

不要求本轮重做 bash streaming/output accumulator。

---

## 3.11 ExtensionHost 的取消传播

当前 ExtensionHost 会再包一层 EventStream。

要避免：

```text
outer stream 被 cancel
但 inner agent stream 继续跑
```

建议调整 `ExtensionHost.stream()`：

1. **同步创建** `inner = self.agent.stream(prompt)`；
2. 再创建 outer EventStream；
3. outer cancel 时显式 `inner.cancel()` 或 `self.agent.abort()`；
4. `continue_stream()` 同样处理。

另外在 `CodingAgentRuntime` 增加 passthrough：

```python
def abort(self) -> None:
    self.agent.abort()

async def wait_for_idle(self) -> None:
    await self.agent.wait_for_idle()

@property
def is_running(self) -> bool:
    return self.agent.is_running
```

---

## 3.12 Cancellation 测试清单

新增 `tests/test_cancellation.py`，至少覆盖：

### C1. abort before provider completion

Fake model：

```python
started.set()
await never_finishes.wait()
```

启动 Agent 后 abort。

断言：

- stream iterator 结束；
- `stream.result()` 不裸抛 CancelledError；
- 最后一个事件是 `agent_end`；
- `agent_end.status == "aborted"`；
- `agent.is_running is False`；
- `wait_for_idle()` 返回。

### C2. abort during model streaming

模型先 emit partial text，再阻塞。

abort 后：

- partial assistant message 被规范化为 `stop_reason="aborted"`；
- `message_end` 恰好一次；
- history 没有残留“永远 streaming”的 partial。

### C3. abort during one tool

阻塞 tool。

abort 后：

- tool 收到 CancelledError；
- Tool Result `is_error=True`；
- metadata 标记 aborted；
- run status aborted。

### C4. abort during parallel tools

3 个 tool：

- A 快速完成；
- B/C 阻塞。

断言：

- 3 个 Tool Result 都存在；
- A 是真实结果；
- B/C 是 aborted；
- message order 是 A/B/C 的 source order；
- 所有 task 都结束。

### C5. abort during sequential batch

验证未开始调用也生成 aborted Tool Result。

### C6. double abort

连续两次 `agent.abort()`：

- 不抛；
- 只产生一次 `agent_end`。

### C7. run after abort

第一轮 abort 后，第二轮正常运行成功。

### C8. second run while active

active run 中再次 `agent.stream()` / `agent.run()`：

- 立即 RuntimeError；
- 不排队。

### C9. ExtensionHost propagation

从 `CodingAgentRuntime.stream()` 或 `ExtensionHost.stream()` 启动后：

```python
runtime.abort()
```

inner Agent 必须停止。

---

# 4. Workstream B：AgentMessage / ProviderMessage 分层

---

## 4.1 新增 Agent-side Message 模型

新增：

```text
src/beta_agent/messages.py
```

建议最小内容模型：

```python
@dataclass(slots=True)
class TextContent:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(slots=True)
class ImageContent:
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None


AgentContent = TextContent | ImageContent
```

`ImageContent` 可以先只建立数据模型，不要求本轮提供完整用户上传产品能力。

### AgentMessage

建议：

```python
Role = Literal["system", "user", "assistant", "tool", "custom"]

@dataclass(slots=True)
class AgentMessage:
    role: Role
    content: list[AgentContent] = field(default_factory=list)

    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    stop_reason: StopReason | None = None
    is_error: bool = False

    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)
```

### 便利 API

必须提供：

```python
@property
def text(self) -> str:
    ...
```

规则：

- 只拼接 `TextContent.text`；
- image 等非文本 block 不进入 `.text`。

现有 classmethod 继续保留：

```python
AgentMessage.user("hello")
AgentMessage.system("...")
AgentMessage.assistant("42")
AgentMessage.tool_result(...)
```

这些 string 参数内部自动转换成：

```python
[TextContent(text=...)]
```

因此旧调用方式尽量不变。

---

## 4.2 `Message` 保留为兼容 alias

在 `beta_agent.__init__` 仍导出：

```python
Message = AgentMessage
```

或在 `types.py`：

```python
Message = AgentMessage
```

并写注释：

```text
Backward compatibility alias.
New code should use AgentMessage.
```

暂时不要删除 `Message`，避免 Coding Agent 与已有 tests 一次性全部断掉。

但是新代码、类型注解与新增 tests 应优先使用 `AgentMessage`。

---

## 4.3 新增 ProviderMessage

新增：

```text
src/beta_agent/provider_messages.py
```

ProviderMessage **不能包含 Agent runtime-only state**。

建议：

```python
ProviderRole = Literal["system", "user", "assistant", "tool"]

@dataclass(slots=True)
class ProviderTextContent:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(slots=True)
class ProviderImageContent:
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None


ProviderContent = ProviderTextContent | ProviderImageContent


@dataclass(slots=True)
class ProviderMessage:
    role: ProviderRole
    content: list[ProviderContent] = field(default_factory=list)

    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
```

**不要复制以下字段：**

- `timestamp`
- `metadata`
- `stop_reason`
- `is_error`
- steering/follow-up delivery metadata
- session metadata
- extension metadata

Provider-specific request fields也不要放进 AgentMessage。

---

## 4.4 Default `convert_to_llm`

建议函数：

```python
def default_convert_to_llm(
    messages: Sequence[AgentMessage],
) -> list[ProviderMessage]:
    ...
```

默认规则：

### system / user

转成同角色 ProviderMessage。

### assistant

转：

- text/image content；
- tool_calls。

不传：

- stop_reason；
- timestamp；
- metadata。

### tool

转：

- `tool_call_id`
- `name`
- content

不传：

- `is_error`
- details metadata

错误信息如果需要 provider 看见，应已经存在于 tool result 的文本 content 中。

### custom

默认：

```text
filter out
```

不要把 custom runtime message 意外发给模型。

未来可以通过自定义 `convert_to_llm` 显式转换 custom message。

---

## 4.5 AgentConfig 增加 converter seam

修改 `AgentConfig`：

```python
ConvertToLlm = Callable[
    [Sequence[AgentMessage]],
    Awaitable[list[ProviderMessage]] | list[ProviderMessage],
]

@dataclass(slots=True)
class AgentConfig:
    ...
    convert_to_llm: ConvertToLlm = default_convert_to_llm
```

实际 provider call 顺序必须固定为：

```python
runtime_messages = self.context.messages

if transform_context:
    runtime_messages = await transform_context(...)

provider_messages = await convert_to_llm(runtime_messages)

await model.stream(
    ...,
    messages=provider_messages,
)
```

必须保证：

```text
transform_context
```

永远看见 AgentMessage，而不是 ProviderMessage。

---

## 4.6 ModelAdapter 输入改为 ProviderMessage

修改：

```text
src/beta_agent/model.py
```

目标：

```python
class ModelAdapter(Protocol):
    async def stream(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ProviderMessage],
        tools: Sequence[object],
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        ...
```

ModelAdapter 的输出仍然使用 Beta 统一的 assistant `AgentMessage` / `ModelEvent`。

即：

```text
Agent → ProviderMessage → Adapter → provider API
provider stream → Adapter normalization → AgentMessage/ModelEvent → Agent
```

这样 provider-specific response 不会污染 Agent core。

---

## 4.7 OpenAICompatibleAdapter 只接受 ProviderMessage

修改：

```text
src/beta_agent/adapters/openai_compatible.py
```

当前 `_messages()` 直接根据 runtime Message：

- role
- content
- tool_calls

拼 OpenAI payload。

改造后：

```text
Agent runtime metadata
```

已经在 converter 阶段被剥离。

adapter 只负责：

```text
ProviderMessage
    ↓
OpenAI Chat Completions JSON
```

### Content conversion

文本：

```json
{"role": "user", "content": "text"}
```

如果未来/当前存在 image content，则可输出 Chat Completions 支持的 content array。

本轮核心验收只强制 text path 完整。

---

## 4.8 ScriptedModelAdapter 更新

它的：

```python
self.calls
```

以后保存：

```python
list[list[ProviderMessage]]
```

测试可以借此验证：

- runtime metadata 不会进入 provider message；
- custom message 被默认过滤；
- transform_context 先运行；
- convert_to_llm 后运行。

---

## 4.9 AgentContext 与 Extensions 继续使用 AgentMessage

修改所有类型注解：

```text
AgentContext.messages
TurnResult.message
TurnResult.tool_results
AgentEvent.message/messages/tool_results
SessionTree
Extension ContextEvent
MessageEndEvent
TurnEndEvent
```

统一为 AgentMessage。

**Extension API 不应该看见 ProviderMessage。**

Extension 若要控制 provider 输入，应通过：

```text
context transform / 后续专门 provider hook
```

而不是直接修改 provider request DTO。

本轮不要新增 provider request extension hook。

---

## 4.10 Session JSONL 向后兼容

当前老 session 的 message payload：

```json
{
  "content": "hello"
}
```

新 session 建议写：

```json
{
  "content": [
    {
      "type": "text",
      "text": "hello"
    }
  ]
}
```

### loader 规则

`_message_from_dict()`：

```text
如果 content 是 str：
    视为旧格式
    → [TextContent(text=content)]

如果 content 是 list：
    按 block type 解析
```

### writer

以后只写新格式。

### format version

建议 `_meta` 扩展：

```json
{
  "_meta": {
    "leaf_id": "...",
    "format_version": 2
  }
}
```

旧文件没有 `format_version` 时当成 v1。

如果未来读取到：

```text
format_version > CURRENT_SESSION_FORMAT_VERSION
```

应报清晰错误，不要偷偷错误解析。

---

## 4.11 迁移所有 `.content` 字符串假设

全仓搜索：

```text
.message.content
message.content
result[-1].content
len(message.content)
```

需要逐一判断。

常见迁移：

```python
message.content
```

若语义是“拿纯文本”：

```python
message.text
```

例如：

- compaction 字符估算；
- subagent 最终回复；
- tests 中 `result[-1].content == "42"`；
- prompt/debug 输出。

若语义是“完整消息内容”，则使用 content blocks，不能强行 `.text`。

---

## 4.12 Message Boundary 测试清单

新增 `tests/test_message_conversion.py`。

### M1. default converter strips metadata

AgentMessage：

```python
AgentMessage.user(
    "hello",
    delivery="steering",
    secret_runtime_metadata="x",
)
```

转换后 ProviderMessage 不包含这些字段。

### M2. custom filtered

custom message 不进入默认 provider messages。

### M3. assistant tool call preserved

assistant text + tool_calls 都正确转换。

### M4. tool result preserved

`tool_call_id`、name、文本正确。

`is_error` 不作为 provider DTO 字段发送。

### M5. transform before convert

记录两个 hook 的执行顺序：

```text
transform_context
convert_to_llm
model.stream
```

必须严格一致。

### M6. Session v1 load

手写旧 JSONL：

```json
"content": "old message"
```

加载后：

```python
message.text == "old message"
```

### M7. new session roundtrip

content blocks 写入再读取完全等价。

### M8. old constructor compatibility

以下仍可运行：

```python
Message.user("x")
Message.assistant("y")
Message.tool_result(...)
```

### M9. OpenAI adapter receives ProviderMessage

禁止 adapter 测试依赖 AgentMessage runtime metadata。

---

# 5. Workstream C：Failure Normalization

---

## 5.1 新增标准错误结构

新增：

```text
src/beta_agent/errors.py
```

建议：

```python
ErrorStage = Literal[
    "model",
    "transform_context",
    "convert_to_llm",
    "prepare_next_turn",
    "should_stop_after_turn",
    "steering_provider",
    "follow_up_provider",
    "before_tool_call",
    "tool_execute",
    "after_tool_call",
    "extension_bridge",
]

@dataclass(slots=True)
class AgentErrorInfo:
    stage: ErrorStage
    message: str
    exception_type: str
    retryable: bool = False
```

不要默认存 traceback 到消息/session。

traceback 可以：

- logging；
- debug mode；
- test 内部；

但不要塞进 provider-visible message。

---

## 5.2 AgentEvent 增加 run-level 状态

建议：

```python
RunStatus = Literal["completed", "error", "aborted"]
```

`AgentEvent` 增加：

```python
status: RunStatus | None = None
error_info: AgentErrorInfo | None = None
```

保留现有：

```python
error: str | None
```

因为 tool execution event 现在已经使用它。

### agent_end

所有正常 Agent run 最终都必须有：

```python
AgentEvent(
    type="agent_end",
    messages=...,
    status="completed" | "error" | "aborted",
    error_info=...,
)
```

### agent_error

fatal runtime error 时，先：

```python
AgentEvent(
    type="agent_error",
    error_info=...
)
```

再：

```text
agent_end(status="error")
```

---

## 5.3 Agent.last_error

Agent 加：

```python
@property
def last_error(self) -> AgentErrorInfo | None:
    ...
```

规则：

- 每次 run 开始先清空；
- completed → None；
- aborted → None 或可使用独立 aborted status，不当作 error；
- error → 保存 fatal error。

这样只使用：

```python
await agent.run(...)
```

而没迭代事件的调用者，至少还能通过 `agent.last_error` 判断。

本轮不要求把 `run()` 返回值改成 `AgentRunResult`，避免 API 改动过大。

---

## 5.4 Fatal runtime failure 统一出口

建议在 `_run()` 外层做统一防线：

```python
try:
    return await self._run_impl(...)
except asyncio.CancelledError:
    return await self._finalize_aborted_run(...)
except Exception as exc:
    return await self._finalize_failed_run(
        stage="runtime",
        exc=exc,
    )
```

但是：

**不能只靠最外层 try/except。**

因为不同 stage 有不同语义：

- tool hook failure 应转 Tool Result，而不是 fatal；
- model failure 应生成 assistant error message；
- transform failure 是 run-level fatal；
- cancellation 是 aborted，不是 error。

所以应在每个语义边界就地归一化，外层只做最后防线。

---

## 5.5 Model failure 归一化

`_stream_assistant()` 必须保证：

> 一个符合 ModelAdapter contract 的普通 provider/runtime failure 不会从该函数以 Exception 形式逃出去。

### 情况 A：adapter 自己 emit error event

最终得到：

```python
AgentMessage.assistant(
    ...,
    stop_reason="error",
    error_message=...
)
```

并正常：

```text
message_end
turn_end
agent_error
agent_end(status="error")
```

### 情况 B：adapter 直接抛 Exception

Agent 必须 defensive normalize：

```text
Exception
  ↓
assistant error message
  ↓
message_end
  ↓
turn_end
  ↓
agent_error
  ↓
agent_end(error)
```

### partial stream 已经开始

如果已经 append 了 partial assistant：

- 不允许再 append 第二条独立 assistant error message；
- 应把最后的 partial **替换**成最终 error assistant；
- `message_start` 只一次；
- `message_end` 只一次。

### error message 内容

建议：

```python
content="Model request failed."
metadata={
    "error_message": str(exc),
    "error_type": type(exc).__name__,
}
```

如果已经有 partial text：

- 可以保留 partial text；
- error 信息放 metadata；
- 不应伪装成模型生成的长错误解释。

---

## 5.6 OpenAICompatibleAdapter 自身也要守约

adapter 里：

```python
response.raise_for_status()
json.loads(...)
httpx streaming
```

都可能失败。

实现：

```python
try:
    ...
except asyncio.CancelledError:
    raise
except Exception as exc:
    yield ModelEvent(
        type="error",
        partial=AgentMessage.assistant(
            "",
            stop_reason="error",
            error_message=str(exc),
            error_type=type(exc).__name__,
        ),
    )
    return
```

Agent 层仍保留 defensive catch，以防第三方 adapter 不守协议。

双层保护不是重复：

```text
Adapter
    → 第一责任：归一化 provider error

Agent
    → 最后责任：不让错误 Adapter 破坏 Runtime lifecycle
```

---

## 5.7 before_tool_call failure：Fail Closed

当前 before hook 可以用于 permission gate。

所以若 hook 本身报错，**不能继续执行目标 tool**。

规则：

```text
before_tool_call throws
    ↓
当前 tool 不执行
    ↓
生成 error Tool Result
```

文本建议：

```text
Tool execution blocked because before_tool_call hook failed: <message>
```

标记：

```python
is_error=True
details={
    "stage": "before_tool_call",
    "exception_type": ...
}
```

不要让整个 Agent fatal。

这样模型有机会在下一轮看到错误，同时安全边界是 fail closed。

---

## 5.8 tool.execute failure

保留现有方向：

```text
tool throws Exception
    ↓
ToolResult(is_error=True)
```

但是补两个要求：

1. `asyncio.CancelledError` 不得被转成普通 tool error；
2. tool error details 应至少可标记 stage/type。

例如：

```python
ToolResult(
    content=f"Tool execution failed: {exc}",
    details={
        "stage": "tool_execute",
        "exception_type": type(exc).__name__,
    },
)
```

如果 tool 原本有结构化 details，不要在普通成功路径强行改格式。

---

## 5.9 after_tool_call failure

tool 已经真实执行过。

若 after hook 抛错：

- 不允许重新执行 tool；
- 不允许炸掉 Agent；
- 当前 call 最终返回 error Tool Result。

建议：

```text
Tool executed, but after_tool_call hook failed: <message>
```

details 可包含：

```python
{
    "stage": "after_tool_call",
    "exception_type": ...,
    "tool_executed": True,
    "original_result_details": original_result.details,
}
```

不要把 original result content 拼接进错误文本，避免产生语义混乱。

---

## 5.10 transform_context failure

这是当前 provider call 的 runtime preparation failure。

规则：

```text
transform_context throws
    ↓
不调用 provider
    ↓
agent_error(stage="transform_context")
    ↓
agent_end(status="error")
```

不要静默 fallback 到原 messages。

原因：

- transform 可能承担安全裁剪、context 管理、extension 注入；
- 静默忽略可能比失败更危险。

---

## 5.11 convert_to_llm failure

同 transform：

```text
convert_to_llm throws
    ↓
不调用 provider
    ↓
agent_error
    ↓
agent_end(error)
```

禁止把未转换 AgentMessage 直接发给 adapter 作为 fallback。

---

## 5.12 prepare_next_turn failure

上一 turn 已经完成。

若 hook 失败：

- 保留已经完成的 history；
- 不开启新 turn/provider request；
- `agent_error(stage="prepare_next_turn")`
- `agent_end(error)`

不要回滚前一个 turn。

---

## 5.13 should_stop_after_turn failure

当前 turn 已经完整结束。

若 hook 失败：

- 不继续执行更多模型调用；
- fatal error；
- history 保留；
- agent_end(error)。

理由：

```text
hook 已经无法可靠决定是否继续
```

继续运行风险大于停止。

---

## 5.14 Steering / Follow-up provider failure

`get_steering_messages` / `get_follow_up_messages` 如果来自外部消息队列。

若失败：

```text
agent_error
agent_end(error)
```

不要默认为 `[]`。

因为静默丢掉用户消息是不可接受的。

---

## 5.15 ExtensionRunner 现有错误隔离继续保留

当前 extension handler：

```text
runner.emit(...)
runner.emit_tool_call(...)
runner.emit_context(...)
```

已经 catch 普通 Exception 并记录 `runner.errors`。

这个行为不需要全部改成 Agent fatal。

本轮规则：

```text
普通 extension handler error
    → runner.errors
    → 继续既有 extension composition
```

但是：

- 如果 ExtensionHost 与 Agent Core 之间的 bridge 本身出错；
- 或 persist message / lifecycle bridge 出现 runtime structural failure；

则应归入 `extension_bridge` fatal error。

不要让一个普通 extension handler failure 与 bridge infrastructure failure 混为一谈。

---

# 6. Agent Loop 推荐重构形态

不要一次写成一个超大的 try/except。

建议：

```python
async def _run(...):
    # 只负责 lifecycle shell
    try:
        return await self._run_impl(...)
    except asyncio.CancelledError:
        return await self._handle_abort(...)
    except Exception as exc:
        return await self._handle_unexpected_runtime_error(...)


async def _run_impl(...):
    # 保留现有双层 loop 结构
    ...
```

再提供语义 helper：

```python
async def _safe_transform_context(...)
async def _safe_convert_to_llm(...)
async def _safe_prepare_next_turn(...)
async def _safe_should_stop(...)
async def _safe_get_steering(...)
async def _safe_get_follow_up(...)

async def _emit_agent_error(...)
async def _emit_agent_end(...)
```

ToolRuntime 内部则：

```python
async def _safe_before_tool_call(...)
async def _safe_execute_tool(...)
async def _safe_after_tool_call(...)
```

目标是：

> 看到函数名就知道该边界的错误语义。

---

# 7. Event Lifecycle 明确定义

完成后至少遵守以下事件序列。

---

## 7.1 正常无 tool

```text
agent_start
turn_start
message_start(user)
message_end(user)
message_start(assistant)
message_update*
message_end(assistant)
turn_end
agent_end(status=completed)
```

---

## 7.2 Tool 成功

```text
agent_start
turn_start
user start/end
assistant start/update/end
tool_execution_start
tool_execution_update*
tool_execution_end
tool_result message_start/end
turn_end
turn_start
assistant ...
turn_end
agent_end(completed)
```

---

## 7.3 Model error

```text
agent_start
turn_start
user start/end
assistant message_start      # 如果 stream 已开始
assistant message_update*
assistant message_end(error)
turn_end
agent_error
agent_end(error)
```

如果 model 在任何 start event 之前就失败，也必须生成：

```text
message_start(error assistant)
message_end(error assistant)
```

保持消息 lifecycle 完整。

---

## 7.4 Abort during model

```text
agent_start
turn_start
...
assistant message_start
assistant message_update*
assistant message_end(aborted)
turn_end
agent_end(aborted)
```

不要求额外 `agent_error`。

abort 是用户控制流，不是 runtime error。

---

## 7.5 Tool error

```text
tool_execution_start
tool_execution_end(error)
tool_result message_start
tool_result message_end
turn_end
```

Agent 可以继续下一轮。

---

## 7.6 Abort during tool batch

每一个 assistant tool call：

```text
tool_execution_start
tool_execution_end
tool_result message_start
tool_result message_end
```

最终：

```text
turn_end
agent_end(aborted)
```

---

## 7.7 Fatal hook error

以 transform 为例：

```text
agent_start
turn_start
user start/end
agent_error(stage=transform_context)
agent_end(error)
```

不伪造 assistant message。

---

# 8. Public API 兼容要求

本轮应尽量保持：

```python
from beta_agent import (
    Agent,
    AgentConfig,
    Message,
    Tool,
    ToolResult,
    ...
)
```

仍可 import。

新增建议导出：

```python
AgentMessage
TextContent
ImageContent
ProviderMessage
CancellationToken
AgentErrorInfo
RunStatus
```

### 必须保持的旧调用

```python
Message.user("hello")
Message.assistant("42", stop_reason="stop")
Message.tool_result(...)
await agent.run("...")
agent.stream("...")
agent.steer("...")
agent.follow_up("...")
```

### 允许变化

旧代码若直接：

```python
message.content == "42"
```

新架构后应迁移到：

```python
message.text == "42"
```

Beta 目前版本较早，可以接受这类明确、有测试覆盖的 API 进化。

---

# 9. Session / Compaction 联动修改

由于 AgentMessage content 改成 blocks：

```text
src/beta_agent/session.py
src/beta_agent/compaction.py
```

都需要同步。

---

## 9.1 Session serialization

新增 helper：

```python
_content_to_json(...)
_content_from_json(...)
```

不要把 block serialization 散落在 `_message_to_dict()` 里。

---

## 9.2 Compaction

当前：

```python
len(session_message(e).content)
```

等字符串假设改成：

```python
len(session_message(e).text)
```

如果未来 image 参与 token estimate，本轮先不实现精确 image token，只把 text path 做正确。

---

## 9.3 Coding subagent

当前类似：

```python
replies[-1].content
```

改成：

```python
replies[-1].text
```

本轮不重构 subagent 本身。

---

# 10. OpenAI Adapter 详细要求

完成后 OpenAI adapter 必须只有三个责任：

```text
1. ProviderMessage → OpenAI request payload
2. OpenAI SSE → Beta ModelEvent / AgentMessage
3. Provider-specific error → normalized model error event
```

它不负责：

- session；
- steering/follow-up；
- runtime metadata；
- extension metadata；
- AgentMessage filtering；
- compaction。

### HTTP error

包括：

```text
401 / 429 / 500
timeout
connection error
invalid JSON
stream interruption
```

都必须成为：

```text
ModelEvent(type="error")
```

或最终由 Agent defensive catch 归一。

### CancelledError

必须：

```python
except asyncio.CancelledError:
    raise
```

不能转成普通 provider error。

---

# 11. Failure 测试矩阵

新增：

```text
tests/test_failure_normalization.py
```

至少覆盖以下用例。

---

## F1. Model adapter throws before first event

断言：

- `agent.run()` 不裸抛；
- history 有 error assistant；
- stop_reason error；
- agent_error；
- agent_end(error)；
- `agent.last_error.stage == "model"`。

---

## F2. Model throws after partial update

断言：

- 只有一条 assistant message；
- `message_start` 一次；
- `message_end` 一次；
- partial 被替换为 final error assistant。

---

## F3. before_tool_call throws

断言：

- tool handler 没有被调用；
- 返回 Tool Result error；
- Agent loop 没炸；
- model 可以收到该 Tool Result 并进行下一轮。

---

## F4. tool.execute throws

断言现有行为保持。

---

## F5. after_tool_call throws

断言：

- tool handler 只执行一次；
- Agent 不 fatal；
- 当前 Tool Result is_error；
- details 标记 after hook failure。

---

## F6. transform_context throws

断言：

- model 没被调用；
- agent_error；
- agent_end(error)；
- stream 正常结束。

---

## F7. convert_to_llm throws

同上。

---

## F8. prepare_next_turn throws

先完成一轮 tool result，再在 prepare 中抛。

断言：

- 已完成 history 保留；
- 不发第二次 provider request；
- agent_end(error)。

---

## F9. should_stop_after_turn throws

断言 run fatal end，且不继续。

---

## F10. steering provider throws

断言不静默忽略。

---

## F11. follow-up provider throws

断言不静默忽略。

---

## F12. extension handler throws

断言保持现有行为：

```text
runner.errors 记录
Agent 不 fatal
```

---

# 12. Integration Tests

除了新增 unit tests，还必须更新现有 tests。

---

## 12.1 `tests/test_agent_loop.py`

保留原有关键断言：

```text
user → assistant → tool → assistant
```

并增加：

```python
assert final_agent_end.status == "completed"
```

文本断言迁移到 `.text`。

---

## 12.2 `tests/test_parallel_tools.py`

必须确认本轮没有破坏：

- parallel execute；
- source-order Tool Result commit；
- completion-order end event。

再增加 cancellation case，可放入 `test_cancellation.py`。

---

## 12.3 `tests/test_session.py`

增加：

- old string content load；
- new blocks roundtrip；
- meta format version。

---

## 12.4 `tests/test_extension_runtime.py`

确认：

- extension handler failure 隔离仍然有效；
- cancellation 参数升级没有破坏旧 extension callback；
- context handler 仍拿 AgentMessage。

---

## 12.5 `tests/test_extension_composition.py`

确认：

- previous before hook + extension tool_call hook composition 仍正确；
- previous transform + extension context transform 顺序仍正确；
- hook error policy符合本计划。

---

## 12.6 `tests/test_coding_assembly.py`

确认：

- `CodingAgentRuntime.abort()`；
- `wait_for_idle()`；
- session persistence 使用 AgentMessage blocks；
- assembly 仍能 load old session。

---

## 12.7 `tests/test_coding_e2e.py`

现有 happy-path 必须全绿。

不要为了新架构删掉旧 e2e 断言。

---

# 13. 推荐实施顺序

不要一次把三个 Workstream 混成一个巨大 patch。

---

## Phase 0：建立 baseline

执行：

```bash
python -m pytest -q
```

记录现有测试结果。

如果 baseline 本来就失败：

- 先确认失败是否与本计划无关；
- 不要把已有无关 bug 混在此次改造中。

---

## Phase 1：错误 / 状态基础类型

新增：

```text
errors.py
cancellation.py
RunStatus
AgentErrorInfo
```

扩展 AgentEvent，但暂时不改核心行为。

先让旧测试继续通过。

---

## Phase 2：Active Run + Abort

实现：

```text
Agent.is_running
Agent.abort()
Agent.wait_for_idle()
EventStream.cancel()
```

并先让 provider-only cancellation test 通过。

然后处理 ExtensionHost 的 cancel propagation。

---

## Phase 3：Tool cancellation consistency

实现：

```text
ToolExecutionContext.cancellation
ToolBatchResult.aborted
parallel/sequential aborted result completion
```

先把 cancellation tests 全做绿。

**这一阶段完成前不要开始 Message 大迁移。**

---

## Phase 4：Failure Normalization

按 stage 一个个加：

```text
model
before tool
tool execute
after tool
transform
convert
prepare
should_stop
queues
```

每加入一个 stage，就先补对应 test。

不要最后一次性写所有 catch。

---

## Phase 5：ProviderMessage boundary

先只拆类型和 converter：

```text
AgentMessage → ProviderMessage
```

先保持 AgentMessage 仍可通过旧构造 API 生成。

改 model adapter 输入。

改 OpenAI adapter。

把转换顺序测试做绿。

---

## Phase 6：Structured content + session migration

把：

```python
content: str
```

升级为 content blocks。

全仓迁移 `.content` 字符串假设。

增加老 session load tests。

---

## Phase 7：Integration / cleanup

执行：

```bash
python -m pytest -q
python -m compileall src
```

检查：

- 无未 await task warning；
- 无 pending task destroyed warning；
- 无 CancelledError 泄漏；
- 无老 session 读取回归。

---

# 14. 每个 Phase 的提交边界建议

如果 Codex 有 commit 权限，建议按以下粒度提交：

```text
1. feat(runtime): add cancellation and error primitives
2. feat(runtime): add active-run abort lifecycle
3. feat(tools): make tool batches cancellation-safe
4. feat(runtime): normalize model and hook failures
5. refactor(messages): separate agent and provider messages
6. refactor(messages): add structured content and session compatibility
7. test(runtime): complete cancellation/error/message integration coverage
```

不要把所有东西压成一个 commit。

---

# 15. Definition of Done

三个 P0 差距只有同时满足下面条件才算完成。

---

## Cancellation DoD

- [ ] Agent 有 `abort()`。
- [ ] Agent 有 `wait_for_idle()`。
- [ ] Agent 有 `is_running`。
- [ ] 同一个 Agent 不会把第二个 run 静默排队。
- [ ] provider stream 可被取消。
- [ ] before/after/transform 等 hook 可观察 cancellation。
- [ ] tool execute 可被取消。
- [ ] parallel tool tasks 全部正确回收。
- [ ] bash process tree 不会在 abort 后残留。
- [ ] abort 后 tool-call/result pairing 完整。
- [ ] abort 最终只有一次 `agent_end(status="aborted")`。
- [ ] abort 后下一次 run 可正常工作。

---

## Message Boundary DoD

- [ ] AgentMessage 与 ProviderMessage 是不同类型。
- [ ] AgentContext / Session / Extension 使用 AgentMessage。
- [ ] ModelAdapter 输入 ProviderMessage。
- [ ] `transform_context` 在 converter 之前执行。
- [ ] `convert_to_llm` 是显式配置点。
- [ ] runtime metadata 默认不会发给 provider。
- [ ] custom AgentMessage 默认不发给 provider。
- [ ] `Message` 兼容 alias 仍可用。
- [ ] text classmethod 旧调用仍可用。
- [ ] 新 session 写 content blocks。
- [ ] 旧 string-content session 能继续加载。

---

## Failure Normalization DoD

- [ ] provider/network 普通失败不会裸抛出 Agent API。
- [ ] model partial failure 不留下未结束 partial message。
- [ ] before hook failure fail closed。
- [ ] tool execute failure 是 error Tool Result。
- [ ] after hook failure 不炸 Agent。
- [ ] transform failure 是 fatal run error。
- [ ] converter failure 是 fatal run error。
- [ ] prepare_next_turn failure 是 fatal run error。
- [ ] should_stop hook failure 是 fatal run error。
- [ ] steering/follow-up provider failure 不静默吞。
- [ ] extension handler 现有隔离行为仍保留。
- [ ] 所有 fatal runtime failure 最终都有 `agent_end(status="error")`。
- [ ] `agent.last_error` 可读取最近 fatal error。
- [ ] `asyncio.CancelledError` 与普通 error 明确区分。

---

# 16. 明确的非目标

Codex 在执行本文档时 **不要顺手实现**：

- auto compaction；
- overflow recovery；
- model registry；
- model switching；
- thinking level；
- AGENTS.md / CLAUDE.md ResourceLoader；
- find / ls / PowerShell；
- bash output accumulator；
- TUI；
- RPC；
- provider 数量扩展；
- subagent workspace/tool 继承；
- session branch summary / label / model-change entry。

如果改造过程中发现这些能力需要接口预留：

```text
可以预留字段或抽象
但不要实现完整功能
```

避免 P0 改造失控。

---

# 17. Codex 执行时的检查清单

开始改代码前：

```text
1. 阅读：
   src/beta_agent/agent.py
   src/beta_agent/events.py
   src/beta_agent/types.py
   src/beta_agent/model.py
   src/beta_agent/tools.py
   src/beta_agent/session.py
   src/beta_agent/compaction.py
   src/beta_agent/adapters/openai_compatible.py
   src/beta_agent/extensions/bridge.py
   src/beta_agent/extensions/runner.py
   src/beta_agent/extensions/types.py
   src/coding_agent/assembly.py
   src/coding_agent/tools/bash.py

2. 阅读现有 tests，先理解当前行为。

3. 运行 baseline pytest。

4. 严格按 Phase 1 → 7 实施。

5. 每一阶段优先写/更新测试，再进入下一阶段。
```

---

# 18. 最终架构预期

完成后 Beta Runtime 的主路径应接近：

```text
                         ┌──────────────────────┐
                         │       Agent          │
                         │ active run / abort   │
                         └──────────┬───────────┘
                                    │
                              AgentMessage[]
                                    │
                         transform_context
                                    │
                              AgentMessage[]
                                    │
                           convert_to_llm
                                    │
                            ProviderMessage[]
                                    │
                         ┌──────────▼──────────┐
                         │    ModelAdapter     │
                         │ normalized stream  │
                         └──────────┬──────────┘
                                    │
                            AgentMessage
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
                no tool calls                 tool calls
                     │                             │
                     │                    ToolRuntime
                     │                  cancellation-safe
                     │                  failure-normalized
                     │                             │
                     │                     Tool Results
                     └──────────────┬──────────────┘
                                    │
                                 turn_end
                                    │
                     steering / follow-up / stop
                                    │
                                  ...
                                    │
                     agent_end(completed/error/aborted)
```

贯穿整条链路：

```text
CancellationToken
+
asyncio task cancellation
+
AgentErrorInfo
+
严格的 AgentMessage / ProviderMessage boundary
```

---

# 19. 最重要的实现判断

如果实现过程中出现取舍，按下面优先级决策：

```text
协议完整性
    >
取消后资源真正停止
    >
消息历史可继续
    >
错误可观察
    >
向后兼容
    >
代码最少
```

例如：

- abort 时宁可补 aborted Tool Result，也不要留下半套 tool protocol；
- before permission hook 出错时宁可 block，也不要继续执行；
- transform 出错时宁可结束 run，也不要静默绕过；
- provider message 宁可显式转换，也不要继续让 adapter 猜 runtime message；
- 老 session 宁可增加兼容解析，也不要让用户已有历史全部失效。

---

# 20. 验收命令

最终至少执行：

```bash
python -m pytest -q
python -m compileall src
```

并确认测试输出中没有：

```text
Task was destroyed but it is pending
coroutine was never awaited
Unhandled exception in task
CancelledError
```

作为正常 Agent abort 的泄漏。

如果测试中需要验证没有 task leak，应使用 asyncio event / task 状态做确定性同步，避免依赖长时间 `sleep()`。

---

## 最终交付要求

Codex 完成本计划后，应给出：

1. 修改文件清单；
2. 新增公共 API；
3. 兼容性变化，尤其是 `message.content → message.text/content blocks`；
4. cancellation lifecycle 的实现说明；
5. failure normalization matrix 的实际落地说明；
6. 旧 session 格式兼容说明；
7. 全部测试结果；
8. 若存在尚未完成的 DoD 项，必须明确列出，不能用“基本完成”代替。

**不要以“代码能跑”为验收标准。**  
本次真正的完成标准是：

> Agent 被取消、provider 报错、hook 报错、tool 报错、消息经过扩展与持久化之后，Runtime 仍然能够保持可解释、可恢复、协议完整的状态。
