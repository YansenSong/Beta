# Beta Agent 当前框架说明

本文档介绍 `Beta` 当前 `main` 分支已经实现的 Python Agent Framework，以及 Chapter 12 的 Coding Agent 产品层。

它参考 Pi Agent 的架构思想和 `learn-pi-agent` Chapter 00～12 的递进过程，但不是 TypeScript 源码的逐行翻译。Beta 更关注这些稳定边界：

```text
Model Boundary
Agent Loop
Event Stream
Tool Runtime
Steering / Follow-up
Context Transform
Session Tree
Compaction
Skills
Extension Runtime
Extension Composition
Coding Agent Assembly
```

Chapter 12 已把 read / write / edit / grep / bash、Workspace 与前面这些 Runtime primitive 组装成 `beta_agent.coding` 产品层。

---

## 1. 框架现在解决什么问题

最小 Tool-driven Agent 的循环很简单：

```text
User
 ↓
LLM
 ↓
Assistant
 ↓
Tool Call?
├── no  → finish
└── yes → Tool → Tool Result → LLM
```

真正做成可维护框架以后，还需要回答：

- Provider 协议差异放在哪里；
- 流式过程如何暴露给 CLI / UI / tracing；
- Tool 参数怎样验证、失败怎样回给模型；
- 多个 Tool Call 怎样并行但保持稳定历史；
- 用户中途 Steering / Follow-up 何时进入下一轮；
- Runtime history 与单次 LLM Context 怎样解耦；
- 长会话如何分支、持久化、压缩；
- Skill 如何按需加载；
- Permission / Plan Mode / Subagent 这类行为是否需要修改 Agent Core；
- 外部 Python 模块怎样安全地注册 Tool、Command、Handler。

Beta 当前 00～12 的代码就是围绕这些问题形成的。

---

## 2. 当前目录结构

```text
Beta/
├── docs/
│   ├── README.md
│   ├── ARCHITECTURE.md
│   ├── FRAMEWORK.md
│   ├── EXTENSIONS.md
│   └── tutorials/
│       ├── 00-model-boundary.md
│       ├── ...
│       ├── 10-extension-runtime.md
│       ├── 11-extension-composition.md
│       └── 12-coding-agent.md
├── examples/
│   ├── deepseek_cli.py
│   ├── coding_agent_cli.py
│   └── extensions/
│       ├── permission_gate.py
│       ├── plan_mode.py
│       └── subagent.py
├── skills/
│   └── example/SKILL.md
├── src/beta_agent/
│   ├── __init__.py
│   ├── agent.py
│   ├── types.py
│   ├── events.py
│   ├── model.py
│   ├── tools.py
│   ├── builtin_tools.py
│   ├── session.py
│   ├── compaction.py
│   ├── skills.py
│   ├── adapters/
│   │   └── openai_compatible.py
│   ├── extensions/
│   │   ├── __init__.py
│   │   ├── types.py
│   │   ├── runner.py
│   │   ├── wrapper.py
│   │   ├── loader.py
│   │   └── bridge.py
│   └── coding/
│       ├── assembly.py
│       ├── prompt.py
│       ├── extensions/
│       └── tools/
└── tests/
    ├── test_agent_loop.py
    ├── test_parallel_tools.py
    ├── test_session.py
    ├── test_skills.py
    ├── test_extension_runtime.py
    └── test_extension_composition.py
```

整个系统可以粗略看成：

```text
Application / Harness
├── CLI
├── Session / Compaction / Skills
└── ExtensionHost / ExtensionRunner
        ↓
Agent Core
├── Agent Loop
├── EventStream
└── ToolRuntime
        ↓
Provider Boundary
└── ModelAdapter
        ↓
External Model API
```

---

## 3. 核心协议：`types.py`

`types.py` 是框架内部共享的数据协议层。

主要对象包括：

```text
Message
ToolCall
ToolResult
ModelEvent
AgentEvent
AgentContext
ToolBatchResult
TurnResult
```

核心思想是：Agent Runtime 围绕自己的内部对象工作，而不是直接操作某个 Provider 的 JSON。

例如：

```text
Message / ToolCall
      ↓
Agent Runtime
      ↓
ModelAdapter
      ↓
Provider request / stream response
```

这条边界让未来增加其他 Provider 时，不需要重写 Agent Loop。

---

## 4. 模型边界：`model.py` 与 `adapters/`

### `ModelAdapter`

Agent 只依赖一个 Provider-neutral 接口：

```text
system_prompt
messages
tools
   ↓
stream()
   ↓
ModelEvent
```

### `ScriptedModelAdapter`

测试里使用脚本化模型响应，不依赖真实 API。

这很重要，因为 Tool Loop、Steering、Context、Extension 等 Runtime 行为应该可以做确定性测试。

### `OpenAICompatibleAdapter`

当前真实模型适配器采用 OpenAI-compatible Chat Completions 风格，负责：

- 内部 Message → Provider message；
- Pydantic Tool schema → function tool schema；
- 流式文本累积；
- tool call argument 累积；
- finish reason 映射；
- Provider stream → `ModelEvent`。

`examples/deepseek_cli.py` 使用这一 Adapter 连接 DeepSeek API。

---

## 5. EventStream：结果之外还要暴露过程

Agent 不只是最终返回一段文本。

一次真实 run 可能经历：

```text
agent_start
turn_start
message_start
message_update × N
tool_execution_start
tool_execution_update × N
tool_execution_end
message_end
turn_end
...
agent_end
```

因此 `EventStream` 同时提供：

```python
async for event in stream:
    ...

messages = await stream.result()
```

前者用于 UI / CLI / tracing，后者用于拿最终结果。

### 完整 partial message

`message_update` 不是只发字符 delta，而是发当前完整 partial message：

```text
H
He
Hel
Hell
Hello
```

这样消费者即使错过某次 update，也能直接使用后续完整状态继续渲染。

---

## 6. Agent Loop：`agent.py`

`Agent` 负责稳定控制流，而不负责具体 Tool、Session 或 Extension 策略。

一次 run 的主要路径：

```text
agent_start
   ↓
turn_start
   ↓
user / queued messages
   ↓
transform_context
   ↓
ModelAdapter.stream()
   ↓
Assistant Message
   ↓
Tool Calls?
├── yes → ToolRuntime.execute_batch()
│          ↓
│       Tool Results
│          ↓
│       turn_end
│          ↓
│       next turn
└── no  → turn_end
           ↓
        Steering?
           ↓
        Follow-up?
           ↓
        agent_end
```

Agent 本身不知道：

```text
permission gate
plan mode
subagent
extension directory
```

这些都在更外层组合。

---

## 7. Context 的两个层次

### `transform_context`

只改变：

```text
“这一轮模型看到什么”
```

默认不修改完整 Runtime history。

所以：

```text
Agent.context.messages
≠
本次 ModelAdapter 输入 messages
```

这条 seam 可用于：

- sliding window；
- RAG 临时注入；
- message filtering；
- Plan Mode prompt；
- Provider-specific policy。

### `prepare_next_turn`

它更强，可以真正替换下一轮 Runtime 的 `AgentContext`。

可以理解为：

```text
transform_context
→ 临时模型视图

prepare_next_turn
→ 下一轮 Runtime 状态
```

---

## 8. Steering 与 Follow-up

两者最终都会成为普通 user message，但时机不同。

### Steering

在当前 turn 完整结束后读取：

```text
Assistant
↓
Tool batch
↓
Tool Results
↓
turn_end
↓
Steering
↓
next turn
```

不会强行打断正在执行的 Tool batch。

### Follow-up

只有 Agent 本来准备结束整个 run 时才读取：

```text
没有 Tool Call
↓
没有 Steering
↓
准备 agent_end
↓
Follow-up?
├── yes → next turn
└── no  → agent_end
```

这就是 Agent Loop 使用不同 checkpoint 的原因。

---

## 9. Tool 与 ToolRuntime：`tools.py`

每个 Tool 声明：

```text
name
description
args_model
handler
execution_mode
prepare_arguments(optional)
```

完整 Tool lifecycle：

```text
ToolCall
↓
lookup
↓
prepare_arguments
↓
Pydantic validate
↓
before_tool_call
↓
execute
↓
after_tool_call
↓
ToolResult
↓
Tool Result Message
```

Tool 错误不会默认把整个 Agent run 直接打崩，而是尽量被转换成模型可见结果，让模型有机会自我修正。

### truncated tool call

如果模型因为 token limit 截断了 Tool Call，Runtime 不会因为参数“碰巧能解析”就执行，而是返回明确失败结果。

---

## 10. Parallel Tool 的两个顺序

假设模型请求：

```text
A → B → C
```

真实完成顺序：

```text
B → C → A
```

Beta 保留：

```text
execution events
→ B → C → A

history commit
→ A → B → C
```

所以 Observability 反映真实执行过程，而 Message history 保持确定性。

并发只发生在真正的 `execute` 阶段，preflight：

```text
lookup
prepare
validate
before_tool_call
```

仍然保持 source order。

---

## 11. Session Tree：`session.py`

长期历史不是一条会被覆盖的数组，而是一棵 append-only tree。

每个 `SessionEntry` 有：

```text
id
parent_id
timestamp
type
payload
```

如果原历史：

```text
U1 → A1 → U2 → A2
```

把 leaf 移回 A1 后继续：

```text
U1
 ↓
A1
 ├── U2  → A2
 └── U2' → A2'
```

旧分支仍然存在。

Agent 最终消费的仍然是 `get_branch()` 还原出来的当前线性 Message 序列。

Session 还支持 JSONL 保存与加载。

---

## 12. Context Compaction：`compaction.py`

Context Window 有上限，不代表历史必须删除。

Beta 会追加：

```text
CompactionEntry
```

记录：

```text
summary
first_kept_entry_id
tokens_before
```

未来重建当前 branch 时使用：

```text
summary + retained tail
```

原始旧 Entry 仍然保留。

retained tail 会尽量从 user message 边界开始，避免留下孤立的 Tool Result。

---

## 13. Skills：`skills.py`

Skill 与 Tool 职责不同。

```text
Tool
→ Agent 可以执行什么操作

Skill
→ 遇到某类任务时应采用什么方法
```

启动时只读取 Skill metadata：

```text
name
description
location
```

正文仍留在 `SKILL.md` 文件里。

模型真正判断 Skill 相关时，再通过普通读取 Tool 加载正文。

所以大量 Skill 不会一次性污染 system prompt。

---

## 14. Extension Runtime：`extensions/`

Chapter 10 加入 Extension Runtime，但没有修改 Agent Loop 的基本形状。

核心目录：

```text
src/beta_agent/extensions/
├── types.py
├── runner.py
├── wrapper.py
├── loader.py
└── bridge.py
```

### `ExtensionAPI`

Extension factory 只能注册受控能力：

```python
pi.on(...)
pi.register_tool(...)
pi.register_command(...)
```

它不会直接得到完整 `Agent`。

### `ExtensionRunner`

保存：

```text
handlers
tools
commands
runtime config
session
errors
```

并定义事件组合语义。

### `ExtensionHost`

由 `bind_extensions()` 创建，负责把 Runner 接回已有 Core seam：

```text
context     → transform_context
tool_call   → before_tool_call
message_end ← EventStream
turn_end    ← EventStream
active tools → running Agent tools
```

因此 Extension Runtime 属于 Harness 层。

---

## 15. Extension factory 的原子加载

假设：

```python
def extension(pi):
    pi.register_tool(tool_a)
    raise RuntimeError("boom")
```

不能留下半个 Extension。

所以 Runner 使用：

```text
temporary registrations
        ↓
run factory
        ↓
success?
├── yes → commit all
└── no  → discard all
```

一个 Extension 失败不会阻止其他 Extension 继续加载。

错误被记录在：

```python
runner.errors
```

---

## 16. Extension Event Composition

当前四类事件：

```text
tool_call
context
message_end
turn_end
```

不是统一的 EventEmitter 规则。

### Observe

`message_end / turn_end`：按注册顺序执行，单个 handler 失败被隔离。

### Intercept

`tool_call`：第一个返回 `block=True` 的 handler 立即短路。

### Transform

`context`：顺序 pipeline：

```text
messages0
↓ A
messages1
↓ B
messages2
↓ Model
```

详细使用方式见 [`EXTENSIONS.md`](EXTENSIONS.md)。

---

## 17. Extension Tool

Extension 作者可以定义 `ExtensionTool`，handler 会额外拿到 `ExtensionContext`。

但注册后会被 wrapper 转回普通 Core `Tool`：

```text
ExtensionTool
↓
wrapper
↓
Tool
↓
ToolRuntime
```

因此 Extension Tool 自动复用：

- Pydantic validation；
- before / after hook；
- sequential / parallel execution；
- progress event；
- error normalization；
- source-order history commit。

不会出现第二套 Extension Tool protocol。

---

## 18. Active Tools

Extension 通过：

```python
ctx.get_active_tools()
ctx.set_active_tools(names)
```

改变下一次 ModelAdapter 调用可见的 Tool。

它不直接修改：

```python
agent.context.tools
```

bridge 负责：

```text
Tool names
↓ resolve
Tool objects
↓ apply
running Agent
```

这也是 Plan Mode 能动态关闭 mutation Tool 的基础。

---

## 19. Chapter 11：Extension Composition

Chapter 11 用三个能力验证 Extension primitive 是否足够强。

### Permission Gate

```text
tool_call
→ inspect bash command
→ allow / block
```

被 block 后仍然经过 Core ToolRuntime，最终成为模型可见的 error Tool Result。

示例：

```text
examples/extensions/permission_gate.py
```

### Plan Mode

Extension 内部保存：

```text
enabled
tools_before_plan_mode
```

同一状态控制：

```text
set_active_tools
context injection
tool_call bash policy
```

退出时恢复进入 Plan Mode 前真实的 Tool 集合。

示例：

```text
examples/extensions/plan_mode.py
```

### Subagent

Parent Agent 不增加特殊 child-agent branch。

```text
Parent
↓
ToolCall: subagent
↓
Core ToolRuntime
↓
Child Agent + Child Session
↓
ToolResult
↓
Parent continues
```

示例：

```text
examples/extensions/subagent.py
```

Child history 不直接进入 Parent history。

---

## 20. 教程 00～12 与源码对应

| Chapter | 主题 | Beta 主要落点 |
| --- | --- | --- |
| 00 | Model Boundary | `types.py`、`model.py`、`adapters/` |
| 01 | Tool-driven Loop | `agent.py`、`tools.py` |
| 02 | Agent Runtime / Events | `events.py`、`agent.py` |
| 03 | Tool Runtime | `tools.py` |
| 04 | Parallel Tools | `tools.py` |
| 05 | Steering / Follow-up | `agent.py` |
| 06 | Context Transform | `agent.py` |
| 07 | Session Tree | `session.py` |
| 08 | Context Compaction | `compaction.py`、`session.py` |
| 09 | Skills | `skills.py`、`builtin_tools.py` |
| 10 | Extension Runtime | `extensions/` |
| 11 | Extension Composition | `examples/extensions/`、extension tests |
| 12 | Coding Agent Assembly | `coding/`、`examples/coding_agent_cli.py`、coding tests |

Beta 已经不再保持“每章一套独立 demo Runtime”，而是把这些能力合并进同一个 Python package。

---

## 21. 当前运行入口

### 安装

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
pytest
```

### DeepSeek CLI

项目当前真实模型多轮 CLI：

```bash
python examples/deepseek_cli.py
```

需要在项目根目录 `.env` 配置：

```text
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=...
```

### Extension 示例

```text
examples/extensions/permission_gate.py
examples/extensions/plan_mode.py
examples/extensions/subagent.py
```

它们主要用于展示 Chapter 11 的组合方式，不是一个完整的终端产品入口。

### Coding Agent CLI

```bash
python examples/coding_agent_cli.py --cwd . --session .beta/session.jsonl
```

CLI 只负责读取配置、显示事件和调用 `CodingAgentRuntime`；workspace、五个 Coding Tool、Skill metadata、Session 与 Extension 的装配都位于 `beta_agent.coding`。

---

## 22. 新能力应该放在哪里

### 新 Provider

```text
src/beta_agent/adapters/
```

不要让 Agent Loop 认识 Provider JSON。

### 新普通 Tool

```text
Tool + Pydantic args_model + handler
```

### 外部可插拔 Tool

```text
ExtensionTool + pi.register_tool()
```

### 权限 / Policy

优先：

```text
tool_call interception
或现有 before_tool_call hook
```

### 临时 Context 修改

```text
transform_context
或 Extension context handler
```

### 产品模式切换

优先组合：

```text
Extension state
+ active tools
+ context
+ tool_call
```

而不是给 `Agent` 增加 `mode` 分支。

### 持久化会话

```text
SessionTree
```

### 长历史压缩

```text
Compaction + Session reconstruction
```

### 领域工作方法

```text
Skill
```

### 用户命令

```text
pi.register_command()
```

---

## 23. 当前有意没有实现的内容

Chapter 12 已实现基础 Coding Agent，但仍然主动不做：

- read / write / edit / grep / bash 的完整生产级增强；
- OS sandbox、完整 trust model 与命令确认 UI；
- MCP；
- sandbox / 容器隔离；
- permission popup / TUI；
- pip Extension package discovery；
- hot reload / Extension API version negotiation；
- 完整多 Provider capability matrix；
- 精确 token accounting / cost tracking；
- telemetry backend；
- 更完整 Session storage backend；
- 自动 Compaction 策略；
- 进程级 Subagent isolation。

这些应该在已有边界之上逐层加入，而不是提前把 Core 变成产品实现集合。

---

## 24. 推荐阅读顺序

如果要继续维护 Beta，推荐按下面顺序读：

```text
1. types.py
2. model.py
3. events.py
4. tools.py
5. agent.py
6. session.py
7. compaction.py
8. skills.py
9. extensions/types.py
10. extensions/runner.py
11. extensions/wrapper.py
12. extensions/bridge.py
13. examples/extensions/
14. tests/
```

其中最值得反复理解的是：

```text
agent.py
→ 时间与控制流边界

tools.py
→ Tool lifecycle / parallel invariant

session.py
→ 长期历史结构

extensions/runner.py + bridge.py
→ 产品行为如何进入已有 seam 而不污染 Core
```

读完这四组以后，可以继续阅读 `tutorials/12-coding-agent.md`，理解如何把已有 primitive 组装成 Coding Agent。
