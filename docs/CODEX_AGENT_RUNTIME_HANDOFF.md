# Beta Agent Runtime 重构交接文档（给 Codex）

> 目标仓库：`YansenSong/Beta`  
> 目标：在**不访问 Pi 源码**的前提下，将 Beta 当前 Agent Runtime 按本文描述的语义继续演进。  
> 参考语义快照：Pi `main`，截至 2026-09-20。  
> 重要：本文已经把需要借鉴的上游设计翻译成 Beta 可执行的工程规格；**不要要求访问 Pi，也不要自行猜测 Pi 实现细节**。

---

## 0. 给 Codex 的执行指令

你正在修改当前 Beta 仓库。请把本文当作设计规格，而不是讨论稿。

执行时遵守以下原则：

1. **先阅读当前源码和测试，再改代码。**
   - 重点检查：
     - `src/beta_agent/agent.py`
     - `src/beta_agent/messages.py`
     - `src/beta_agent/types.py`
     - `src/beta_agent/tools.py`
     - `src/beta_agent/provider_messages.py`
     - `src/beta_agent/model.py`
     - `src/beta_agent/adapters/openai_compatible.py`
     - `src/beta_agent/events.py`
     - `src/beta_agent/session.py`
     - `src/beta_agent/extensions/bridge.py`
     - `src/beta_agent/extensions/runner.py`
     - `src/coding_agent/assembly.py`
     - `src/beta_agent/compaction.py`
     - `tests/`
2. 先运行当前测试，建立 baseline。
3. 分阶段实施，不要一次性重写整个 runtime。
4. 每完成一个阶段，先补测试并确保旧测试继续通过。
5. 除本文明确要求外，不要顺手重写 Coding Agent 的业务工具。
6. Beta 当前“防御式 failure normalization”是**有意保留的设计差异**，不要为了模仿别的项目而删除。
7. 兼容性优先：
   - 尽量保留 `Agent(...)` 当前构造方式；
   - 尽量保留公开 import；
   - 老 Session 文件必须仍能加载；
   - 现有 Coding Agent CLI 不应因为此次 runtime 重构而失效。
8. 完成后给出：
   - 修改摘要；
   - 关键设计说明；
   - 新增/修改测试；
   - 测试结果；
   - 仍保留的已知差异或后续工作。

---

# 1. 当前 Beta Runtime 的判断

Beta 已经实现了 Agent Runtime 的大部分核心控制流，不需要推倒重来。

当前值得保留的设计包括：

- Agent run / turn 双层循环；
- tool-driven continuation；
- steering 与 follow-up 的不同检查点；
- parallel / sequential tool execution；
- per-tool `execution_mode`；
- `prepare_arguments`；
- `before_tool_call` / `after_tool_call`；
- tool batch `terminate`；
- cancellation；
- length-truncated tool call 不执行；
- transform-context 与 provider conversion 分层；
- append-only SessionTree；
- branch-local compaction；
- extension bridge；
- model failure normalization；
- Runtime event stream。

这次重构的核心不是“重新写 Agent Loop”，而是继续完善几个**状态语义与生命周期协议**。

---

# 2. 本次重构的优先级

建议按下面顺序实施。

| Priority | 工作 | 是否本轮必须 |
|---|---|---|
| P0 | 建 baseline / 回归测试 | 是 |
| P1 | Transcript-native system prompt / tool state | **是，最高优先级** |
| P2 | Steering / Follow-up QueueMode | 是 |
| P3 | `prepare_next_turn` richer update | 是 |
| P4 | Tool progress 生命周期 + rich ToolResult | 是 |
| P5 | Awaited Agent event subscribers / ExtensionHost 收敛 | 是 |
| P6 | Provider request options 抽象 | 可选，时间允许再做 |
| P7 | Durable task recovery / replay policy | **不在本轮范围** |

---

# 3. P1：把 system prompt 和 tool state 变成 transcript-native state

这是本次最重要的架构改造。

## 3.1 当前问题

现在 Beta 的 `AgentContext` 大致是三套并列状态：

```text
AgentContext
├── system_prompt
├── messages
└── tools
```

模型调用时再把三者组合：

```text
system_prompt + messages + tools -> provider
```

问题是：

- Session 只保存 messages，不能完整重放某个历史点的 system/tool 状态；
- branch 后不能仅靠 transcript 判断当时有哪些工具；
- extension 动态修改 `context.tools` 时，历史看不到这次变化；
- system/tool state 和 conversation history 是两套 source of truth；
- resume 时容易依赖当前进程配置，而不是历史事实。

目标模型应改成：

```text
AgentContext
├── messages   <- system instruction 和 tool declaration 的历史也在这里
└── tools      <- 当前真正可执行的 Tool object 集合
```

其中：

- `messages` 是**模型状态历史的 source of truth**；
- `tools` 是 runtime 能实际执行的 Python Tool object 集合；
- 模型“认为哪些工具存在”必须可以通过 replay transcript 得到；
- runtime 实际能执行哪些工具由 `context.tools` 决定；
- 两者不一致时，在下一次模型请求前生成一个 system-message tool delta，把变化写入 transcript。

---

## 3.2 System message 要支持 tool delta

建议在 `messages.py` 中增加显式的数据结构，不要只塞进松散 metadata。

示意：

```python
@dataclass(slots=True, frozen=True)
class ToolDeclaration:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolReference:
    name: str
```

然后让 `AgentMessage` 的 system message 可以携带：

```python
tools_added: list[ToolDeclaration]
tools_removed: list[ToolReference]
```

建议直接作为 `AgentMessage` 的 first-class fields，而不是：

```python
metadata["tools_added"]
```

原因：

- Session 序列化需要稳定 schema；
- replay helper 需要类型明确；
- provider compatibility layer 需要识别；
- tool state 不是“展示 metadata”，而是 conversation protocol 的一部分。

### System message 的语义

按 transcript 顺序 replay：

1. system message 的非空文本是系统指令增量；
2. 多个非空 system 文本按顺序组合，建议用 `"\n\n"` 连接；
3. `tools_removed` 先移除同名工具；
4. `tools_added` 再按名称增加/替换 declaration；
5. 最终得到当前有效 system prompt 与当前模型可见 tool declarations。

本轮**不要求实现命名 system sections**。未来如果需要可替换的 system prompt section，再单独扩展；不要为了这次重构过度设计。

---

## 3.3 新增 transcript utility 模块

建议新建：

```text
src/beta_agent/transcript.py
```

至少提供下面这些纯函数。

### `to_tool_declaration(tool)`

从 Python `Tool` 提取可持久化、可比较、无 executable handler 的 declaration：

```text
name
description
parameters/schema
```

不能把：

- handler；
- coroutine；
- Python class；
- cancellation object；
- runtime context

写进 transcript。

---

### `get_current_tool_declarations(messages)`

从所有 system messages replay 当前模型可见工具。

规则：

```text
for system message in transcript order:
    remove tools_removed
    add/replace tools_added
```

最终按稳定顺序返回 declaration。

同名 declaration 后来重新加入时，以后来定义为准。

---

### `get_current_system_prompt(messages)`

从 system messages replay 当前 prompt。

本轮规则：

```text
所有非空 system message text，按时间顺序，用 "\n\n" 拼接。
```

---

### `get_tool_state_changes(previous, current)`

输入：

- previous：从 transcript replay 出来的 declaration；
- current：`AgentContext.tools` 的真实 runtime tools。

输出：

```python
@dataclass(...)
class ToolStateChanges:
    tools_added: list[ToolDeclaration]
    tools_removed: list[ToolReference]
```

规则：

- 新工具 -> added；
- 删除工具 -> removed；
- 同名但 description/schema 改变 -> **removed + added**；
- declaration 完全一致 -> no-op。

比较时必须基于稳定的 declaration，而不是 Tool object identity。

---

### `create_initial_system_message(system_prompt, tools)`

如果 prompt 和 tools 都为空，可以返回 `None`。

否则创建一条 system message：

```text
content = system_prompt
tools_added = 当前初始工具 declaration
```

---

## 3.4 Agent 构造器兼容策略

尽量保留目前外部 API：

```python
Agent(
    model=model,
    system_prompt="...",
    tools=[...],
    messages=[...],
)
```

但是内部初始化时：

### 新 conversation

如果 `messages` 为空或不含既有 system baseline：

```text
create_initial_system_message(system_prompt, tools)
↓
作为 transcript 第一条消息
```

### 已恢复 conversation

如果 `messages` 已经有可重放的 system/tool state：

- 不要再次机械 prepend 同一份 system prompt；
- runtime `tools` 仍使用构造器传入的当前可执行 tools；
- 如果历史 declaration 和 runtime tools 有差异，由下一轮的 tool delta 机制记录。

### 兼容性

可以临时保留：

```python
agent.system_prompt
```

或类似 property，但必须是**从 transcript replay 得到的只读视图**，不能继续作为独立 mutable source of truth。

目标是最终让：

```python
AgentContext
```

不再需要独立的：

```python
system_prompt: str
```

字段。

---

# 4. 在 turn 边界声明 tool state changes

仅仅把初始 tools 放进 system message 还不够。

ExtensionRuntime 现在允许：

```python
agent.context.tools = ...
```

所以 runtime 工具集合可能在 conversation 中途变化。

必须在下一次 provider request 前，把 runtime tool set 与 transcript tool state 做 diff。

建议实现一个内部 helper：

```python
declare_tool_changes(
    context: AgentContext,
    pending_messages: list[AgentMessage],
) -> list[AgentMessage]
```

语义：

1. 取当前 committed transcript：
   ```python
   context.messages
   ```
2. 加上本轮准备注入的 pending/prepared system message，计算“pending 自己已经表达了什么”；
3. replay 当前模型可见 tool declarations；
4. 与 `context.tools` 做 diff；
5. 如果没有变化，pending 原样返回；
6. 如果 pending 中已经存在 system message：
   - 可以把该 system message 的 `tools_added/tools_removed` 更新为正确 delta；
7. 如果 pending 中没有 system message：
   - 生成一条空文本 system message，只携带 tool delta；
   - 插在第一个 non-system pending message 之前；
8. 这些 system delta message 必须：
   - 发 `message_start`；
   - 发 `message_end`；
   - 写入 `context.messages`；
   - 写入本次 `new_messages`；
   - 因而能够被 Session 持久化。

重要：

> `context.tools` 表示“现在能执行什么”；transcript 中的 tool delta 表示“模型在这个历史点被告知了什么”。

两者不能混为一个对象。

---

# 5. Provider compatibility：当前 OpenAI-compatible adapter 不必原生支持历史 tool delta

Beta 当前主要是 OpenAI-compatible Chat Completions adapter。

它的 API 通常还是：

```text
messages + top-level tools
```

因此本轮不要求 provider 真正理解每一条历史 tool delta。

## 5.1 Provider boundary 需要做“collapse”

在 provider 请求前：

1. 从 transcript replay 当前有效 system prompt；
2. 从 transcript/runtime 得到当前有效工具；
3. 生成 provider 能理解的 context：
   - 一个 leading system message；
   - 去除 conversation 中其余 system-state message，避免重复；
   - 普通 user / assistant / tool messages 保持顺序；
   - 当前工具通过 top-level `tools` 发送。

也就是说：

```text
Durable/runtime transcript
    ↓
provider compatibility projection
    ↓
OpenAI Chat Completions payload
```

不要因为 OpenAI provider 暂时无法表达全部历史 tool delta，就放弃 transcript-native state。

未来如果某个 provider 支持 mid-conversation system/tool additions，可以在 adapter 层实现更高保真的 projection。

---

## 5.2 建议的 ModelAdapter 边界

本轮可以选择“低风险兼容改造”：

### 方案 A：保留当前 stream signature

暂时继续：

```python
stream(
    system_prompt=...,
    messages=...,
    tools=...,
    cancellation=...,
)
```

但这些参数必须由 transcript projection 计算出来，而不是读取独立 `AgentContext.system_prompt`。

这是更稳妥的本轮选择。

### 方案 B：未来再改

以后可以把 model boundary 进一步改为：

```python
stream(
    context=ProviderContext(...),
    cancellation=...,
)
```

本轮不强求。

---

# 6. SessionTree 与 transcript-native state

`SessionTree` 目前 append-only + branch 的方向是对的，继续保留。

但从本轮开始，Session 必须能够持久化：

- system text；
- `tools_added`；
- `tools_removed`；
- rich tool-result content；
- 新增 metadata。

建议升级：

```python
CURRENT_SESSION_FORMAT_VERSION = 3
```

## 6.1 新 Session

新建 Coding Agent Session 时，应确保初始 system/tool baseline 最终进入 Session 历史。

不要只存在 `Agent` 内存里。

可以在 assembly 层创建 initial system message，然后：

```text
session.append_message(initial_system_message)
agent messages 从 session 重建结果开始
```

或者设计等价但同样 durable 的流程。

关键验收：

> 新 session 保存 -> 进程退出 -> 重新 load，仅靠 session transcript + 当前 executable tool registry，就能恢复 system/tool state。

---

## 6.2 老 v1/v2 Session 兼容

老 Session 里没有 transcript-native 初始 system/tool baseline。

不要拒绝加载。

建议策略：

1. 老文件继续正常 `load_jsonl`；
2. 在当前 leaf 恢复时，如果 transcript 内没有 system state：
   - 使用当前 Coding Agent 构建出的 system prompt/tool registry；
   - 在**当前恢复点**追加一条 system snapshot/state-transition message；
3. 从这个点以后，新历史变成 self-contained；
4. 不要为了迁移而重写旧 entry id 或删除旧历史。

这意味着：

- 旧 session 的更早历史仍属于 legacy；
- migration point 之后拥有完整 transcript state；
- 这是可以接受的向前迁移方式。

---

## 6.3 Compaction 注意事项

目前 compaction reconstruction 会生成：

```text
system: "Conversation summary: ..."
```

在新设计下，所有 system message 都会参与 system prompt replay。

所以 Codex 必须明确决定并测试 compaction summary 的语义。

本轮建议：

- 保留 summary 为 system message；
- 但 reconstruction 时：
  - 先保留当前有效 system/tool baseline；
  - 再加入 summary system message；
  - 再加入 retained tail；
- 不能因为 compaction 丢失 tool declaration state。

验收：

```text
compact 前 current tools == compact/reconstruct 后 current tools
compact 前 system prompt 基础指令仍然存在
```

不要让 summary 替换掉原始 Coding Agent system prompt。

---

# 7. P2：Steering / Follow-up QueueMode

当前 `_MessageQueue.drain()` 总是一次全部清空。

需要支持：

```python
QueueMode = Literal["all", "one-at-a-time"]
```

建议默认：

```text
one-at-a-time
```

分别配置：

```python
steering_mode
follow_up_mode
```

两条队列互不影响。

## 7.1 `one-at-a-time`

每个 drain point：

- 只取最旧的一条；
- 后续消息继续留在 queue；
- 等下一个合法 drain point 再交付。

## 7.2 `all`

保持当前行为：

- 一次取完所有消息。

## 7.3 保持已有语义

仍然必须保证：

### Steering

只有当前 assistant turn 的 tool calls 全部完成后才能影响下一轮。

不能因为收到 steering 就跳过已经由模型发出的 tool calls。

### Follow-up

只有 Agent 原本准备结束时才读取。

---

## 7.4 Queue API

建议补充：

```python
clear_steering_queue()
clear_follow_up_queue()
clear_all_queues()
has_queued_messages()
```

如果公开 `Agent` 层已有简单 queue API，可保持兼容并向上扩展。

---

## 7.5 continue 行为

当前如果最后一条是 assistant，Beta 会直接拒绝 continue。

建议改进：

如果最后一条是 assistant：

1. 有 queued steering -> 消费合法的一批并继续；
2. 否则有 queued follow-up -> 消费合法的一批并继续；
3. 两者都没有 -> 再报不能从 assistant continue。

---

# 8. P3：`prepare_next_turn` 不再只返回 AgentContext

当前：

```python
prepare_next_turn(turn) -> AgentContext | None
```

太窄。

建议新增：

```python
@dataclass(slots=True)
class NextTurnUpdate:
    context: AgentContext | None = None
    messages: list[AgentMessage] = field(default_factory=list)
    model: ModelAdapter | None = None
```

本轮不强行增加 thinking-level；Beta 目前没有统一的 provider-independent thinking state，可留到 P6。

## 8.1 语义

`prepare_next_turn` 只能在**已经确定还会有下一轮 assistant request**时执行。

顺序必须保持：

```text
turn_end
↓
should_stop_after_turn
↓
检查是否真的要继续
↓
prepare_next_turn
↓
注入 prepared messages + steering
↓
turn_start
↓
下一次 model request
```

不要在最终 turn 结束后无意义执行 compaction/preparation。

---

## 8.2 `NextTurnUpdate.messages`

它们是正式 transcript messages，不是临时 provider-only prompt。

所以必须：

- 经过 tool-change declaration；
- 发 message lifecycle event；
- 写入 context；
- 写入 new_messages；
- 能被 Session 持久化。

---

## 8.3 `NextTurnUpdate.model`

允许在 turn boundary 换模型。

要求：

- 只影响下一轮及后续；
- 不改写已经完成的历史；
- cancellation 继续使用当前 run token；
- model switch 本身不需要产生 synthetic assistant message。

---

# 9. P4：Tool Runtime 生命周期修正

## 9.1 禁止 late progress

当前 `ToolExecutionContext.progress()` 在 Tool 已经 return 后仍可能被后台 task 调用。

这会导致：

```text
tool_execution_end
↓
晚到的 tool_execution_update
```

生命周期倒流。

必须修复。

### 推荐实现

`ToolExecutionContext` 增加：

```python
_accepting_updates: bool
_update_lock: asyncio.Lock
```

`progress()`：

```text
拿 lock
再次检查 accepting
如果已经 closed -> 静默忽略
否则 emit update
释放 lock
```

增加：

```python
async close_updates()
```

语义：

```text
拿同一把 lock
_accepting_updates = False
返回
```

在 tool execute settle 后、发 `tool_execution_end` 前：

```python
await ctx.close_updates()
```

这样可保证：

> `close_updates()` 返回以后绝不会再出现该 tool 的 update event。

晚到 progress 应当**忽略**，不要抛异常污染后台 task。

---

## 9.2 Rich ToolResult

当前：

```python
ToolResult.content: str
```

需要扩大。

建议：

```python
ToolResult.content:
    str
    | AgentContent
    | Sequence[AgentContent]
```

在 `__post_init__` 中统一 normalize 成：

```python
ContentBlocks
```

这样 tool 可以返回：

- text；
- image；
- text + image。

### `AfterToolCallPatch.content`

同步扩大为同样的 rich content 类型。

---

## 9.3 Tool usage

给 `ToolResult` 增加可选：

```python
usage: dict[str, Any] | None = None
```

先用通用 dict 即可，不必本轮设计复杂 token accounting class。

`after_tool_call` patch 也允许覆盖 `usage`。

最终 tool-result AgentMessage metadata/session 要保留 usage。

---

## 9.4 现有语义必须保持

不要破坏：

- preflight 顺序；
- per-tool sequential override；
- parallel execute；
- completion-order `tool_execution_end`；
- source-order tool-result message commit；
- `terminate` 必须是**整批 finalized calls 全部 terminate=True 才提前终止**；
- argument prepare/validation failure 要变成 model-visible tool result；
- before hook failure/block 要变成 model-visible result；
- after hook failure要保持 Beta 当前防御式 normalization。

---

# 10. P5：Agent event subscriber 成为 run settlement 的组成部分

当前 ExtensionHost 通过：

```text
消费 inner EventStream
-> handle extension/persistence
-> re-emit outer EventStream
```

并且 `EventStream.emit()` 里还有：

```python
await asyncio.sleep(0)
```

来给 wrapper 抢调度机会。

这可以工作，但 runtime boundary 不够干净。

目标改成：

```text
Agent emit event
↓
core awaited subscribers
↓
producer 才能继续下一个 lifecycle event
```

特别是：

```text
agent_end emitted
↓
agent_end subscribers 全部 settle
↓
run result settle
↓
wait_for_idle 返回
```

---

## 10.1 Agent subscribe API

建议增加：

```python
unsubscribe = agent.subscribe(listener)
```

listener 形态建议：

```python
async def listener(event: AgentEvent, cancellation: CancellationToken):
    ...
```

也可允许 sync listener。

要求：

- listener 按注册顺序 await；
- unsubscribe 幂等；
- run active 时 listener 能看到同一个 cancellation；
- `agent_end` listener 完成前 Agent 仍不算 idle。

---

## 10.2 listener failure

Beta 的设计倾向是“结构性 failure 要进入统一 error lifecycle”。

继续保持。

建议：

- 普通 extension user handler 仍由 `ExtensionRunner` 自己隔离记录错误；
- persistence / bridge / core subscriber 抛异常时视为 infrastructure failure；
- 进入 Beta 现有 `_StageFailure` / `_finalize_error` 路径；
- 不要让 subscriber exception 静默丢失。

新增明确 stage，例如：

```text
event_listener
```

或者继续映射到：

```text
extension_bridge
```

但要统一。

---

## 10.3 简化 ExtensionHost

完成 subscribe 后，建议把 ExtensionHost 的 message persistence 和 turn/message extension dispatch 改成 Agent subscriber。

目标是最终不再需要：

```python
await asyncio.sleep(0)
```

作为 bridge correctness 的关键机制。

理想结构：

```text
bind_extensions()
    -> 注册 before_tool_call hook
    -> 注册 transform_context hook
    -> 注册 agent event subscriber
    -> 返回 host facade

host.stream()
    -> 直接委托 agent.stream()
```

Host 仍可保留 facade API：

```python
run
stream
continue_stream
abort
wait_for_idle
is_running
run_command
close
```

`close()` 要 unsubscribe，并恢复被替换的 hooks/tools。

---

# 11. Beta 的 failure philosophy 必须保留

这里不要“为了 parity”做反向重构。

当前 Beta 有意做到：

- provider adapter 直接抛异常 -> Agent 也能 normalize；
- transform hook 失败 -> stage error；
- queue provider 失败 -> stage error；
- tool hook 失败 -> model-visible 或 runtime-visible structured failure；
- cancellation -> protocol-complete aborted lifecycle；
- `agent_error` + `agent_end(status=error)`。

保留这种 Python-friendly defensive boundary。

也就是说：

```text
协议优先 + defensive normalization
```

是 Beta 的合理设计，不需要删除。

---

# 12. P6（可选）：Provider request options

只有前面全部稳定后再做。

当前 adapter 构造器里混有：

```text
model
api_key
base_url
timeout
extra_body
```

未来 Agent runtime 可能还需要：

- session/cache id；
- transport；
- retry delay cap；
- thinking budget；
- request metadata；
- payload/response callback。

如果本轮实现，建议加：

```python
@dataclass(slots=True)
class ModelRequestOptions:
    session_id: str | None = None
    transport: str | None = None
    max_retry_delay_ms: int | None = None
    thinking_budget: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

由 Agent/ModelAdapter 传递。

但不要：

- 把 OpenAI 专有字段塞进 Agent core；
- 本轮强制所有 adapter 实现完整功能。

如果风险较大，明确留 TODO 即可。

---

# 13. 本轮明确不做的事情

以下内容不要顺手扩展：

## 13.1 不做完整 Durable Harness

不要求本轮实现：

- durable operation/task records；
- crash recovery scheduler；
- atomic operation journal；
- global facts；
- shared commit sequence；
- torn-tail repair；
- open-operation recovery；
- background task ownership tree。

这些属于下一阶段 durable agent runtime。

---

## 13.2 不做 tool replay policy

暂不实现类似：

```python
replay = "never" | "safe"
```

的 crash recovery 语义。

---

## 13.3 不重写 SessionTree 为数据库

现有 append-only tree 可以继续用。

本轮只要求它能正确持久化新 transcript schema。

---

## 13.4 不删除 Beta 的错误归一化

见第 11 节。

---

# 14. 推荐的代码改动地图

## `src/beta_agent/messages.py`

需要：

- `ToolDeclaration`
- `ToolReference`
- `AgentMessage.tools_added`
- `AgentMessage.tools_removed`
- rich tool-result content compatibility
- copy / normalize 行为保持正确

注意 dataclass mutable default。

---

## `src/beta_agent/transcript.py`（新增）

建议包含：

```text
to_tool_declaration
declarations_equal
create_initial_system_message
get_current_system_prompt
get_current_tool_declarations
get_tool_state_changes
declare_tool_changes / helper
collapse transcript for legacy provider
```

尽量写纯函数，并重点单测。

---

## `src/beta_agent/types.py`

需要：

- `AgentContext` 逐步去掉独立 `system_prompt`
- `NextTurnUpdate`
- `QueueMode`
- `ToolResult` rich content / usage
- 必要的 event/listener type

---

## `src/beta_agent/agent.py`

需要：

- 构造时建立 transcript-native baseline；
- queue mode；
- richer `prepare_next_turn`；
- prepared messages 注入；
- 每次下一 provider turn 前声明 tool delta；
- current system prompt 从 transcript replay；
- subscriber lifecycle；
- continue + queued messages 行为；
- 保持 should-stop / steering / follow-up 时序。

不要重写整个 loop；在现有双层循环上演进。

---

## `src/beta_agent/tools.py`

需要：

- late progress guard；
- rich ToolResult；
- usage；
- after patch usage；
- lifecycle 顺序回归测试。

---

## `src/beta_agent/provider_messages.py`

需要：

- system/tool-state message projection；
- provider DTO 不应携带 runtime-only executable object；
- custom message 过滤规则保持。

---

## `src/beta_agent/adapters/openai_compatible.py`

需要：

- 使用 collapse 后的当前 system prompt；
- 使用当前 effective tools；
- 不重复 system prompt；
- rich content 继续工作；
- 不要求支持历史 tool delta 的 provider-native encoding。

---

## `src/beta_agent/session.py`

需要：

- format v3；
- tools_added/tools_removed 序列化；
- rich content roundtrip；
- legacy v1/v2 load；
- system/tool state replay 相关 roundtrip test。

---

## `src/beta_agent/compaction.py`

需要：

- compaction 后不丢 system/tool state；
- retained tail protocol 安全性保持。

---

## `src/beta_agent/extensions/bridge.py`

需要：

- 优先改为 core subscriber；
- 去掉对 `asyncio.sleep(0)` correctness hack 的依赖；
- close 时 unsubscribe；
- tool registry 改变后让下一个 turn 自动产生 tool delta。

---

## `src/beta_agent/extensions/runner.py`

原则上少改。

只在需要配合 active tools / subscriber 时调整。

---

## `src/coding_agent/assembly.py`

需要：

- 新 session 初始 system/tool baseline durable；
- legacy session migration point；
- Agent 初始化不要双写 system prompt；
- Coding Agent 对外 API 保持。

---

# 15. 测试计划

不要只改实现，下面测试至少要覆盖。

建议新增：

```text
tests/test_transcript_state.py
tests/test_queue_modes.py
tests/test_event_subscribers.py
```

也可以合并进已有 test 文件，但场景必须覆盖。

---

## 15.1 Transcript state

### Initial system state

```text
Agent(system_prompt="S", tools=[A, B])
```

断言 transcript replay 得到：

```text
system prompt = S
tools = A, B
```

---

### Tool add

初始 `[A]`，运行中改成 `[A, B]`。

下一次 model request 前 transcript 必须出现：

```text
system message
tools_added = [B]
```

---

### Tool remove

`[A, B] -> [B]`

必须出现：

```text
tools_removed = [A]
```

---

### Tool redefine

工具 A 同名，但 schema/description 改变。

必须表示为：

```text
remove A
add new A
```

---

### No-op tool state

定义完全相同不能产生多余 system message。

---

### Branch replay

在：

```text
A available
-> branch
-> B added
```

从旧 branch replay 时不能看到 B。

---

### Session roundtrip

system/tool delta 保存 JSONL，再 load 后 replay 结果完全一致。

---

## 15.2 Provider collapse

构造包含多个 system/tool delta 的 transcript。

OpenAI-compatible 最终 payload 必须：

- system prompt 不重复；
- 当前 tool list 正确；
- user/assistant/tool 消息顺序正确。

---

## 15.3 Queue mode

### one-at-a-time steering

排队三条 steering。

每个合法 drain point 只能交付一条。

### all steering

一次交付三条。

### follow-up 同理。

### 默认值

默认应验证为：

```text
one-at-a-time
```

---

## 15.4 prepare_next_turn

测试：

- context replacement；
- prepared messages；
- model replacement；
- `should_stop_after_turn=True` 时 prepare 不执行；
- final turn 没有下一轮时 prepare 不执行；
- prepare 过程中加入 steering 时不能错误丢失。

---

## 15.5 Late progress

构造一个 tool：

1. 启动后台 task；
2. tool 本体先 return；
3. 后台 task 再尝试 `ctx.progress()`。

事件序列中必须：

```text
tool_execution_end
```

之后没有该 tool 的：

```text
tool_execution_update
```

而且 background progress 不应因为 closed context 抛未处理异常。

---

## 15.6 Rich ToolResult

至少：

- text；
- image；
- text + image；
- details；
- usage；
- after hook 替换 content；
- after hook 替换 usage；
- Session roundtrip。

---

## 15.7 Event subscriber settlement

用 `asyncio.Event` 做一个 listener：

```text
收到 agent_end 后阻塞
```

此时断言：

```text
agent.wait_for_idle()
```

还没有返回。

释放 listener 后才返回。

---

## 15.8 Extension persistence

确保改用 subscriber 后：

- message 仍只 append 一次；
- 不重复持久化；
- turn_end extension 仍收到；
- cancellation 仍能结束；
- persistence failure 仍进入 error lifecycle。

---

## 15.9 Existing regression

至少运行：

```bash
pytest -q
```

并重点确认已有：

```text
test_agent_loop.py
test_cancellation.py
test_parallel_tools.py
test_failure_normalization.py
test_message_conversion.py
test_session.py
test_extension_runtime.py
test_extension_composition.py
test_coding_assembly.py
test_coding_e2e.py
test_coding_tools.py
```

继续通过。

---

# 16. 事件顺序必须保持的协议

正常 tool turn：

```text
agent_start
turn_start

message_start(user)
message_end(user)

message_start(assistant)
message_update*
message_end(assistant)

tool_execution_start(call-1)
tool_execution_start(call-2)

tool_execution_update*

tool_execution_end(...)
tool_execution_end(...)

message_start(tool-result-1)
message_end(tool-result-1)

message_start(tool-result-2)
message_end(tool-result-2)

turn_end
...
agent_end
```

Parallel 模式特别注意：

```text
tool_execution_end
```

可以按实际完成顺序。

但：

```text
tool-result messages
```

必须按 assistant 原始 tool-call source order 写入 transcript。

---

# 17. Runtime loop 的目标时序

最终 loop 的概念顺序应接近：

```text
start run
│
├─ commit prompt messages
│
├─ poll initial steering
│
└─ while run:
    │
    ├─ if previous completed turn and next turn confirmed:
    │    └─ prepare_next_turn
    │
    ├─ prepared messages + queued steering
    │
    ├─ declare runtime tool changes into transcript
    │
    ├─ turn_start
    │
    ├─ transform_context
    │
    ├─ provider projection
    │
    ├─ stream assistant
    │
    ├─ execute/finalize tool batch
    │
    ├─ commit tool results
    │
    ├─ turn_end
    │
    ├─ should_stop_after_turn?
    │
    ├─ steering drain
    │
    └─ if otherwise stopping:
         └─ follow-up drain

agent_end
│
└─ awaited subscribers settle

run becomes idle
```

具体 `turn_start` 放置要与当前 Beta event tests 保持一致；不要为了图示机械搬位置，核心是 lifecycle 语义。

---

# 18. 关键不变量

实现过程中如果拿不准，优先守住这些 invariants。

### Invariant A：Transcript 是模型状态历史

system prompt/tool declaration 变化不能只存在 Python mutable state。

### Invariant B：Executable Tool object 不进入持久化 transcript

只持久化 declaration。

### Invariant C：Session append-only

新状态通过新 message/entry 表达，不原地改旧历史。

### Invariant D：transform_context 默认不改完整 runtime history

它只决定“这一轮模型看到什么”。

### Invariant E：Steering 不跳过当前 tool calls

它只影响下一 assistant turn。

### Invariant F：Follow-up 只在原本准备停止时读取

### Invariant G：Tool progress 不能晚于 tool_execution_end

### Invariant H：Parallel completion order 与 transcript commit order 分离

### Invariant I：`agent_end` listener 完成才算 run settled

### Invariant J：Beta defensive error normalization 保留

---

# 19. 推荐实施批次

为了降低一次性破坏风险，建议按 5 个逻辑批次提交。

## Batch 1：Transcript foundation

修改：

```text
messages.py
types.py
transcript.py
session.py
```

新增 transcript unit tests。

此时先不大改 ExtensionHost。

---

## Batch 2：Agent loop integration

修改：

```text
agent.py
provider_messages.py
openai_compatible.py
model.py（如必要）
coding_agent/assembly.py
compaction.py
```

完成 system/tool state 真正进入主循环。

---

## Batch 3：Queue + prepare-next-turn

完成：

- QueueMode；
- continue queue behavior；
- NextTurnUpdate；
- 对应测试。

---

## Batch 4：Tool lifecycle

完成：

- late progress guard；
- rich ToolResult；
- usage；
- lifecycle tests。

---

## Batch 5：Awaited subscribers + ExtensionHost

完成：

- `Agent.subscribe()`；
- event settlement；
- extension persistence 迁移；
- 移除 `sleep(0)` correctness dependency；
- 全量回归。

---

# 20. 文档同步

代码完成后至少检查并更新：

```text
README.md
docs/ARCHITECTURE.md
docs/FRAMEWORK.md
docs/PIPELINE.md
docs/tutorials/00-model-boundary.md
docs/tutorials/02-agent-runtime-events.md
docs/tutorials/03-tool-runtime.md
docs/tutorials/04-parallel-tools.md
docs/tutorials/05-steering-followup.md
docs/tutorials/06-context-transform.md
docs/tutorials/07-session-tree.md
docs/tutorials/08-context-compaction.md
docs/tutorials/10-extension-runtime.md
docs/tutorials/11-extension-composition.md
docs/tutorials/12-coding-agent.md
```

不要求每个文件都大改，但不能让文档继续声称：

```text
system_prompt 是独立 runtime source of truth
```

或：

```text
所有 steering 默认一次 drain
```

如果代码已改变。

---

# 21. 验收标准

本轮可以认为完成，必须同时满足：

- [ ] `AgentContext` 的长期设计不再依赖独立 mutable `system_prompt`
- [ ] 初始 system/tool state 能进入 transcript
- [ ] 中途 tool set 改变会形成可持久化 delta
- [ ] branch/session replay 能恢复正确 tool state
- [ ] OpenAI-compatible adapter 在新 transcript 模型下仍正常
- [ ] legacy session 可加载
- [ ] steering/follow-up 支持 `one-at-a-time` / `all`
- [ ] 默认 queue mode 是 `one-at-a-time`
- [ ] `prepare_next_turn` 支持 context/messages/model update
- [ ] 没有下一 turn 时 prepare 不运行
- [ ] tool settle 后 late progress 被忽略
- [ ] ToolResult 支持 image/rich content
- [ ] ToolResult 支持 usage
- [ ] parallel tool completion/commit 顺序不回退
- [ ] Agent 支持 awaited event subscriber
- [ ] `agent_end` subscriber 完成前 `wait_for_idle()` 不返回
- [ ] ExtensionHost 不再依赖 `asyncio.sleep(0)` 保证 persistence 顺序
- [ ] Beta 当前 failure-normalization 测试仍通过
- [ ] Coding Agent e2e 仍通过
- [ ] `pytest -q` 全绿

---

# 22. 本轮完成后仍允许存在的差异

完成本文后，Beta 仍不需要与任何外部实现逐行一致。

允许保留：

1. **Python dataclass / Pydantic 风格的数据模型**
2. **Beta 更强的 defensive exception normalization**
3. **EventStream 作为用户消费事件的 async iterator**
4. **现有 SessionTree 实现**
5. **OpenAI-compatible provider 的 collapse compatibility**
6. **Coding Agent 的现有扩展体系**

重点是设计语义一致，而不是语言层面的机械翻译。

---

# 23. 给 Codex 的最终提醒

这次最容易犯的三个错误：

### 错误 1：只把 system prompt 放进第一条 message，但 runtime 后续仍偷偷用独立 `context.system_prompt`

这样只是复制一份状态，没有解决双 source of truth。

### 错误 2：`context.tools` 一变就直接修改旧 system message

Session 是 append-only。工具变化必须以新的 transcript delta 表达。

### 错误 3：为了 transcript-native，把 executable Tool object 直接序列化

绝对不要。

Transcript 只保存：

```text
tool declaration
```

Runtime registry 才保存：

```text
Tool(handler=...)
```

---

完成修改后，请在最终报告中按下面格式返回：

```markdown
## Summary
...

## Architecture changes
...

## Compatibility
...

## Tests added/updated
...

## Test result
...

## Remaining follow-ups
...
```

如果实现过程中发现本文与当前 Beta 最新源码有局部命名差异，以**本文的语义要求优先，当前仓库结构其次**，做最小必要适配，不要因此扩大成无关重构。
