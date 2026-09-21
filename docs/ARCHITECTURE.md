# Beta Agent 架构说明

Beta 的目标不是把所有 Agent 功能都塞进一个 Runtime，而是把稳定控制流和容易变化的策略分开。

截至 Chapter 12，项目已经形成四个主要层次，并在 Application / Harness 层提供 Coding Agent 产品组装：

```text
Application / Harness
├── coding_agent（Coding Agent 产品包）
├── beta_agent.harness.durable.runtime（durable drive / recovery / checkpoint）
├── CLI / UI
├── Session / Compaction / Skills
└── ExtensionHost / ExtensionRunner
        ↓
Agent Core（beta_agent）
├── Agent Loop
├── EventStream + awaited event subscribers
└── ToolRuntime
        ↓
Provider Boundary
└── ModelAdapter
        ↓
External Model API

Durable Storage（被 Harness 使用）
├── harness/durable/types.py
├── harness/durable/memory.py
└── harness/durable/sqlite.py
```

包级依赖保持单向：`coding_agent -> beta_agent`。Core 不反向依赖 Coding Agent 产品层。

核心原则是：**产品行为优先通过已有 seam 组合，而不是不断给 `Agent._run()` 增加分支。**

---

## 1. 主运行路径

不启用 Extension 时，一次典型 run 是：

```text
User / queued Message
        ↓
transform_context
        ↓
transcript replay + provider projection
        ↓
ModelAdapter.stream(system_prompt, messages, tools)
        ↓
Assistant Message
        ↓
0..N Tool Call
        ↓
ToolRuntime preflight
lookup → prepare → validate → before hook
        ↓
execute
        ↓
after hook
        ↓
Tool Result Message
        ↓
turn_end
        ↓
Steering checkpoint
        ↓
下一轮 / Follow-up checkpoint
        ↓
agent_end
```

整个过程通过统一 EventStream 暴露：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

`message_update` 携带当前完整 partial message，而不是单独字符 delta，因此 UI、日志和 Tracing 不必各自重新拼接状态。

---

## 2. 启用 Extension 后的路径

Extension Runtime 不替换 Core，而是接到 Core 已经存在的 seam：

```text
Extension factory
      ↓ load once
ExtensionRunner registrations
      ↓
ExtensionHost / bridge
      ├── context      → AgentConfig.transform_context
      ├── tool_call    → AgentConfig.before_tool_call
      ├── message_end  ← awaited Agent subscriber
      ├── turn_end     ← awaited Agent subscriber
      └── active tools → running Agent.context.tools
```

`ExtensionHost` 是 facade，不再消费并重新发射一条内层 EventStream。Agent 在把事件交给外部 EventStream 前，按注册顺序 await subscribers；Coding Agent 的消息持久化因此属于 run settlement。

Extension Tool 也不会进入第二套执行器：

```text
ExtensionTool
    ↓ wrapper
普通 Tool
    ↓
Core ToolRuntime
```

因此参数验证、并行执行、错误归一化、Tool Result 写回等语义只有一套。

---

## 3. 模块边界

### `types.py`

共享协议层：

```text
Message
ToolCall
ToolResult
AgentEvent
AgentContext
TurnResult
ToolBatchResult
```

Agent Core 尽量只理解这些内部对象，而不是 Provider-specific JSON。

### `transcript.py`

提供 system prompt / tool declaration 的纯 replay、diff 与 provider projection。Transcript 是模型状态历史的 source of truth；运行时的 `AgentContext.tools` 则保留可执行的 Python Tool objects。

### `model.py` / `adapters/`

`ModelAdapter` 负责 Provider 协议转换和模型流式调用。

```text
internal Message / Tool
       ↓
ModelAdapter boundary
       ↓
OpenAI-compatible / other provider
```

Provider 差异不应进入 Agent Loop。

### `agent.py`

负责：

- run / turn 控制流；
- Runtime history；
- Steering / Follow-up checkpoint；
- `transform_context`；
- `prepare_next_turn`；
- Tool batch 的触发；
- 生命周期事件。

`AgentContext.messages` 是 canonical transcript：system instruction 增量和模型可见 Tool declaration 通过 system message 顺序重放。每次模型请求前，Agent 会比较 transcript declaration 与当前 executable tools，并将差异作为新 message 持久化。

Run 的初始 prompt 会先与当前 executable tool set 对齐，再一起加入 transcript；因此空闲期间发生的 Tool 切换记录在新 user message 之前，branch 到该请求时也能重放正确工具状态。

它刻意不知道：

```text
permission mode
plan mode
subagent workflow
extension module loading
session persistence backend
```

### `tools.py`

负责 Tool Call 的完整生命周期：

```text
lookup
→ prepare_arguments
→ validate
→ before_tool_call
→ execute
→ after_tool_call
→ commit Tool Result
```

Tool progress 只在执行生命周期内可发出；Tool settle 后到达的 update 会被忽略。Tool Result 支持 text/image content blocks 和可选 usage，并随 Tool Message 一起提交。

### `session.py`

负责 append-only、可分支历史树。

Agent 实际消费当前 branch 的线性 Message，但 Session 保留完整树结构。

### `compaction.py`

负责对当前 branch 生成 append-only CompactionEntry。

它改变未来 Context 的重建方式，不删除旧 Session Entry。

### `skills.py`

负责发现 Skill metadata：

```text
name
description
location
```

正文仍留在文件系统，通过普通读取 Tool 按需进入 Context。

### `extensions/`

负责把外部行为接到已有 seam：

```text
types.py    Extension API / Context / Event types
runner.py   registration + dispatch semantics
wrapper.py  ExtensionTool → Core Tool
loader.py   Python module loading
bridge.py   ExtensionRunner ↔ Agent Core wiring
```

### `src/coding_agent/`

这是独立产品包，不属于 `beta_agent` Core。它负责：

```text
Coding Agent assembly
Coding prompt
read_file / write_file / edit / grep / bash
产品级 extension（如 permission gate）
```

它可以依赖 `beta_agent` 的 Agent、ToolRuntime、Session、Skills、Compaction 和 Extension Runtime；反向依赖不允许出现。

---

## 4. Runtime History 与 LLM Context

Beta 明确区分：

```text
Runtime history
≠
当前一次模型调用看到的 Context
```

`Agent.context.messages` 表示已经发生的完整 Runtime history。

这份 history 同时记录系统指令与模型可见工具的状态变化。对仍使用 `system_prompt + messages + tools` 签名的 OpenAI-compatible adapter，Runtime 在边界处把 system message replay 成单个 system prompt、从普通 message list 移除 system-state messages，并把当前可执行工具作为顶层 tools 传入。

`transform_context()` 只决定某一次 ModelAdapter 调用看到什么。

因此 Context seam 可以承载：

- sliding window；
- 临时 RAG；
- 内部消息过滤；
- Plan Mode 提示注入；
- Provider-specific input policy。

而 `prepare_next_turn()` 更强，它可以真正替换下一轮 Runtime 使用的 `AgentContext`。

它也可以返回 `NextTurnUpdate`，一次提交新的 context、要进入 transcript 的 messages 和后续 model。只有确定还有下一次 assistant request 时才会执行此 Hook；prepared messages 会经过正常 message lifecycle 和持久化流程。

---

## 5. Parallel Tool 的两个顺序

并行执行必须同时保留两个不同顺序。

假设模型按：

```text
A → B → C
```

发出 Tool Call，而真实完成顺序是：

```text
B → C → A
```

那么：

```text
Tool execution events
→ B → C → A

Tool Result history
→ A → B → C
```

原因：

- Observability 应反映真实完成时间；
- Message history 应保持确定、可复现的 source order。

另外，并行只发生在 `execute` 阶段。

```text
lookup / prepare / validate / before hook
```

仍按 source order 执行，避免权限判断、参数准备等 preflight 同时发生。

---

## 6. Steering 与 Follow-up

二者最终都会以普通 user message 进入 history，但读取 checkpoint 不同。

### Steering

```text
Assistant
↓
Tool batch
↓
Tool Results
↓
turn_end
↓
读取 Steering
↓
下一轮
```

它不会抢占当前正在执行的 Tool batch。

### Follow-up

```text
当前任务本来准备结束
↓
没有 Tool Call
↓
没有 Steering
↓
检查 Follow-up
├── 有 → 继续下一轮
└── 无 → agent_end
```

所以两种消息不能简单合并成同一个 pending queue。

两条队列各自支持 `one-at-a-time` 与 `all`，默认都是 `one-at-a-time`。前者每个合法检查点只交付最早的一条，后者保留一次 drain 全部的行为；continue 从 assistant 结尾恢复的前提是内部确有排队消息，空的外部 provider 不会被当作可继续的凭据。

---

## 7. Session 与 Compaction

Session 使用 append-only tree：

```text
U1 → A1 → U2 → A2
       └── U2' → A2'
```

`branch(entry_id)` 只移动 `leaf_id`，不会删除旧分支。

Compaction 同样 append-only：

```text
旧历史仍保留
      ↓
追加 CompactionEntry
      ↓
reconstruct_messages()
恢复 system/tool baseline + summary + retained tail
```

因此 Context Window 的限制不会迫使持久化层删除真实历史。

Session format v3 会保存 system/tool deltas、rich Tool Result content 和新增 metadata。Coding Agent 对缺少 transcript baseline 的旧 v1/v2 session，在当前 leaf 追加当前 prompt/tool snapshot，不重写旧 entry。Compaction 重建时会先恢复被压缩前有效的 system/tool baseline，再追加 summary 和 retained tail。

---

## 8. Skill 与 Extension 的区别

两者都不是 Agent Loop 的特殊分支，但职责不同。

### Skill

描述：

```text
“遇到某类任务时应该怎样做”
```

主要是知识 / 工作流文本，通过文件按需加载。

### Extension

描述：

```text
“运行中的 Agent 行为怎样被扩展或重新配置”
```

可以注册：

```text
Tool
Command
Event Handler
Context interception
Tool Call interception
```

所以：

```text
Skill     → 行为指导
Extension → 运行时能力与策略
```

---

## 9. Extension 的组合规则

不同事件不能使用同一个通用并发 EventEmitter 语义。

### Observe

`message_end` / `turn_end`：

```text
A → B → C
```

按注册顺序 await。A 报错时记录错误，但继续 B、C。

### Tool interception

`tool_call`：

```text
A
↓
B returns block
↓
stop
```

第一个 `block=True` 立即短路。

### Context pipeline

`context`：

```text
messages0
↓ A
messages1
↓ B
messages2
↓ Model
```

后一个 handler 必须看到前一个 handler 的输出。

---

## 10. Extension 加载必须原子化

Extension factory 是外部代码，可能在注册了一部分能力后抛异常。

因此 Runner 使用临时 registration：

```text
factory(api)
   ↓
PendingRegistrations
   ↓
成功？
├── yes → commit all
└── no  → discard all
```

不能出现：

```text
Extension 加载失败
但一半 Tool 已经留在 Runtime
```

单 Extension 失败也不应该影响其他 Extension 加载。

---

## 11. Active Tools

Extension 不直接拿 Agent 实例修改：

```python
agent.context.tools
```

而是调用：

```python
ctx.get_active_tools()
ctx.set_active_tools(names)
```

Runner 只保存 Tool 名字，bridge 完成：

```text
names
↓ resolve
Tool objects
↓ apply
running Agent.context.tools
```

所以 Plan Mode 可以在不重建 Agent 的情况下实时改变下一次模型调用可见的 Tool。

---

## 12. Chapter 11 为什么重要

Chapter 11 的价值不是新增三个功能，而是验证 Chapter 10 的 seam 是否足够表达真实 Agent 行为。

### Permission Gate

```text
tool_call
→ allow / block
```

### Plan Mode

```text
state
├── set_active_tools
├── tool_call policy
└── context injection
```

### Subagent

```text
register Tool("subagent")
→ Core ToolRuntime
→ Child Agent
→ ToolResult
```

Agent Core 仍然没有：

```text
permission_mode
plan_mode
subagent_branch
```

这正是当前架构成功与否的核心判断标准。

---

## 13. 当前稳定 invariant

修改框架时，应优先保护这些不变量：

```text
Provider-specific protocol 不进入 Agent Loop

Tool preflight 保持 source order
并行 execute 可以按 completion order 发事件
Tool Result history 保持 source order

Steering 不打断当前 turn
Follow-up 只在 run 原本要结束时检查

Runtime history 与单次 LLM Context 解耦
Session / Compaction 保持 append-only

Extension factory 原子加载
Extension handler failure 被隔离
tool_call block 立即短路
context handler 顺序组成 pipeline
Extension Tool 仍走 Core ToolRuntime
active tools 对下一次 Model call 立即生效
Parent / Child Agent history 保持隔离

coding_agent 只依赖 beta_agent，Core 不反向依赖产品包
```

测试是这些 invariant 的最终约束。

Extension 的普通观察 handler 仍由 `ExtensionRunner` 按注册顺序逐个隔离错误；承载持久化和 Extension dispatch 的 Agent subscriber 则被 await。结构性 subscriber/persistence failure 会进入 Agent 的 `event_listener` error lifecycle，而不是依赖调度让步或外层事件转发。

---

## 14. Chapter 12：Coding Agent 产品层

当前 00～11 已经把 Runtime primitive 和 Extension composition 打通，Chapter 12 在独立的 `coding_agent` 包中解决产品组装问题：

```text
read
write
edit
grep
bash
Workspace
Session
Compaction
Skills
Extensions
```

真正组合成 Coding Agent。

产品层通过 `CodingAgentRuntime` facade 统一走 `ExtensionHost`，所以 message persistence 和 Extension observe 不会因调用方直接拿到 `Agent` 而丢失。`save_session()` 不会再次 append `agent.messages`。

新建或从旧 session 迁移时，Coding Agent 会在首个模型请求前把 prompt/tool baseline 写入 Session；之后 ExtensionHost 通过 Agent subscriber 在 `message_end` 时持久化消息。

Coding Tool 仍是普通 `Tool`：`read_file` / `grep` 可并行，`write_file` / `edit` / `bash` 串行。`cwd` 只负责路径解析，不提供 sandbox；示例 Permission Gate 只是 `before_tool_call` 策略，不是完整安全系统。
