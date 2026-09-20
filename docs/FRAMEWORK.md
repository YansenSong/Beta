# Beta Agent 当前框架说明

本文档介绍 `Beta` 当前 `main` 分支的 Python Agent Framework，以及建立在它之上的 Coding Agent 产品层。

Beta 参考 Pi Agent 的架构思想与 `learn-pi-agent` Chapter 00～12 的递进过程，但不做 TypeScript 源码逐行翻译。项目更关注稳定边界：

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

Chapter 12 已把 read / write / edit / grep / bash、Workspace 与前面的 Runtime primitive 组装成独立的 `coding_agent` 产品包；通用 Runtime 仍留在 `beta_agent`。

---

## 1. 当前目录与包边界

```text
Beta/
├── docs/
│   ├── README.md
│   ├── ARCHITECTURE.md
│   ├── FRAMEWORK.md
│   ├── EXTENSIONS.md
│   └── tutorials/
├── examples/
│   ├── deepseek_cli.py
│   └── coding_agent_cli.py
├── skills/
│   └── example/SKILL.md
├── src/
│   ├── beta_agent/
│   │   ├── agent.py
│   │   ├── types.py
│   │   ├── events.py
│   │   ├── model.py
│   │   ├── tools.py
│   │   ├── session.py
│   │   ├── compaction.py
│   │   ├── transcript.py
│   │   ├── skills.py
│   │   ├── adapters/
│   │   └── extensions/
│   └── coding_agent/
│       ├── assembly.py
│       ├── prompt.py
│       ├── tools/
│       │   ├── read_file.py
│       │   ├── write_file.py
│       │   ├── edit.py
│       │   ├── grep.py
│       │   └── bash.py
│       └── extensions/
│           ├── permission_gate.py
│           ├── plan_mode.py
│           └── subagent.py
└── tests/
```

包级依赖必须保持单向：

```text
coding_agent -> beta_agent
```

`beta_agent` 不反向依赖 `coding_agent`。这条边界意味着：通用 Runtime primitive 才进入 Core；文件系统、Shell、Coding Prompt、Plan Mode、Subagent 等产品行为留在 Coding Agent。

---

## 2. 总体运行路径

最小 Tool-driven Agent：

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

Beta 把它扩展成稳定的生命周期：

```text
agent_start
   ↓
turn_start
   ↓
queued/user messages
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
│       Steering checkpoint
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

每次请求前，Runtime 从 transcript 重放系统指令和模型可见 Tool 声明，并在 Provider 边界投影成 Adapter 所需的 `system_prompt`、普通消息和当前顶层 tools。

Agent Core 不认识 permission gate、plan mode、subagent、workspace workflow 或 Extension 目录；这些都在更外层组合。

---

## 3. Model Boundary：`model.py` / `adapters/`

Agent 只依赖 Provider-neutral 的 `ModelAdapter`：

```text
transcript replay + current executable tools
        ↓
system_prompt + provider messages + top-level tools
   ↓
ModelAdapter.stream()
   ↓
ModelEvent
```

`AgentContext.messages` 同时承载 system instruction 与模型可见 Tool declaration 的历史；`transcript.py` replay 后在兼容边界 collapse 成当前 Adapter 接口需要的参数。Provider JSON、流式协议、finish reason、Tool schema 转换都留在 Adapter。当前真实实现是 `OpenAICompatibleAdapter`，测试使用确定性的 `ScriptedModelAdapter`。

---

## 4. EventStream：结果之外还要暴露过程

运行过程统一暴露：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

消费者既可以：

```python
async for event in stream:
    ...
```

也可以：

```python
messages = await stream.result()
```

`message_update` 携带当前完整 partial message，而不是要求 UI 自己累计字符 delta。

此外，`Agent.subscribe(listener)` 提供按注册顺序 await 的事件订阅。Agent 先等待 subscribers，再把事件交给调用方的 EventStream；因此 `agent_end` listener 完成前 run 仍未 settle，Session persistence 不需要依赖 `asyncio.sleep(0)` 让外层消费者抢到调度。

---

## 5. Context 的两个层次

Beta 明确区分：

```text
Runtime history
!=
某一次模型调用看到的 Context
```

`transform_context()` 只修改本轮模型视图，适合 sliding window、RAG、过滤、Plan Mode 提示等。

`prepare_next_turn()` 更强，可以真正替换下一轮 Runtime 使用的 `AgentContext`。

它也可以返回 `NextTurnUpdate(context=..., messages=..., model=...)`。prepared messages 会成为正式 transcript 并经过 message event / persistence；model 变更只作用于下一轮及之后。Hook 只会在确定要发起下一次 assistant request 时运行。

---

## 6. Tool 与 ToolRuntime：`tools.py`

每个 Tool 声明：

```text
name
description
args_model
handler
execution_mode
prepare_arguments(optional)
```

完整生命周期：

```text
lookup
→ prepare_arguments
→ Pydantic validate
→ before_tool_call
→ execute
→ after_tool_call
→ Tool Result
```

Tool 失败通常被归一化成模型可见 error Tool Result，让模型有机会自我修正，而不是直接终止整个 Agent run。

`ToolResult.content` 可包含文本、图片或混合 content blocks，并可携带 usage metadata。`ToolExecutionContext.progress()` 在 execute settle 后关闭更新入口，避免 late progress 出现在 `tool_execution_end` 之后。

### Parallel Tool 的两个顺序

模型 source order 假设为：

```text
A → B → C
```

真实完成顺序可能是：

```text
B → C → A
```

Beta 保留：

```text
execution events → B → C → A
history commit   → A → B → C
```

Observability 反映真实执行时间；Message history 保持确定性。并发只发生在 execute 阶段，lookup / prepare / validate / before hook 仍按 source order 执行。

---

## 7. Steering 与 Follow-up

两者都会成为普通 user message，但检查点不同。

Steering 在当前 turn 和 Tool batch 完整结束后读取，不抢占执行中的 Tool。

Follow-up 只有在当前 run 原本准备结束时才读取：

```text
没有 Tool Call
↓
没有 Steering
↓
Follow-up?
├── yes → next turn
└── no  → agent_end
```

Steering 与 Follow-up 各自支持 `one-at-a-time` / `all`，默认 `one-at-a-time`；队列模式不会改变两者不同的消费检查点。

---

## 8. Session Tree 与 Compaction

`SessionTree` 是 append-only tree：

```text
U1 → A1 → U2 → A2
       └── U2' → A2'
```

`branch(entry_id)` 只移动 active leaf，不删除旧分支。

Compaction 也保持 append-only：

```text
旧历史仍保留
      ↓
追加 CompactionEntry
      ↓
reconstruct_messages()
恢复 system/tool baseline + summary + retained tail
```

Context Window 的限制不会迫使持久化层删除真实历史。

Session format v3 持久化 system instruction/tool declaration delta、rich Tool Result content 与新增 metadata。Coding Agent 会为新 session 写入初始 baseline；legacy v1/v2 session 在恢复 leaf 追加当前 snapshot，而不改写旧 entry。Compaction reconstruction 先保留有效 baseline 和工具状态，再接 summary 与 retained tail。

---

## 9. Skills：metadata + lazy read

`SkillCatalog` 启动时只发现：

```text
name
description
location
```

Skill 正文仍留在 `SKILL.md`。模型判断相关后，由具体产品提供的普通读取 Tool 按需加载正文。

Core 因此不需要内置文件系统 Tool，也不会因为 Skill 数量增加而把全部正文塞进 system prompt。

---

## 10. Extension Runtime：`src/beta_agent/extensions/`

Extension Runtime 不替换 Agent Core，而是接到已有 seam：

```text
Extension factory
      ↓
ExtensionRunner registrations
      ↓
ExtensionHost / bridge
      ├── context      → AgentConfig.transform_context
      ├── tool_call    → AgentConfig.before_tool_call
      ├── message_end  ← awaited Agent subscriber
      ├── turn_end     ← awaited Agent subscriber
      └── active tools → Agent.context.tools
```

核心文件：

```text
types.py    Extension API / Context / Event types
runner.py   registration + dispatch semantics
wrapper.py  ExtensionTool → Core Tool
loader.py   Python module loading
bridge.py   ExtensionRunner ↔ Agent wiring
```

### 原子加载

factory 先写入临时 registration，成功后一次提交，失败则全部丢弃。不能出现“Extension 加载失败，但一半 Tool 已经进入 Runtime”的状态。

### Event Composition

```text
message_end / turn_end → observe，错误隔离后继续

tool_call → intercept，第一个 block 立即短路

context → 顺序 pipeline，后一个 handler 看前一个输出
```

`ExtensionHost` 直接委托 Agent 的 stream / continue / abort / wait API。Agent subscriber 在事件交给 EventStream 前负责消息持久化和 Extension dispatch；Extension 普通 handler 错误由 Runner 隔离，持久化等 infrastructure failure 则进入 Agent 的 `event_listener` error lifecycle。

### Extension Tool

`ExtensionTool` 最终会被 wrapper 转成普通 Core `Tool`，所以参数验证、执行模式、before / after hook、error normalization 和 history commit 仍然只有一套。

### Active Tools

Extension 通过：

```python
ctx.get_active_tools()
ctx.set_active_tools(names)
```

动态改变下一次模型调用可见的 Tool，不直接修改 Agent 私有状态。

---

## 11. Chapter 11：产品级 Extension Composition

Chapter 11 用 Permission Gate、Plan Mode、Subagent 验证 Chapter 10 的 seam 是否足够表达真实产品行为。现在三者已经作为 Coding Agent 的正式产品模块收口到：

```text
src/coding_agent/extensions/
├── permission_gate.py
├── plan_mode.py
└── subagent.py
```

`examples/extensions/` 已删除，避免维护两套逐渐漂移的实现。

### Permission Gate

```text
tool_call
→ inspect bash
→ allow / block
```

被 block 后仍然由 Core ToolRuntime 生成 error Tool Result。

### Plan Mode

Plan Mode 默认关闭，通过 `/plan` 显式切换：

```text
enabled state
├── snapshot active tools
├── remove write_file / edit 等 mutation Tool
├── add available bash / subagent helpers
├── tool_call policy for bash
└── context instruction
```

退出时恢复进入前真实的 active-tool 快照，而不是硬编码默认列表。

### Subagent

Subagent 仍然只是普通 Extension Tool：

```text
Parent Agent
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

Child history 不直接进入 Parent history。

通过 Coding Agent facade 使用时，子模型由：

```python
CodingAgentOptions.child_model_factory
```

提供。`subagent_extension` 仍需显式加载；当前 Plan Mode 会在已注册时将 `subagent` 加入 active tools。

---

## 12. Coding Agent 产品包：`src/coding_agent/`

Coding Agent 负责产品组装，而不是新增另一套 Runtime：

```text
ModelAdapter
+ Agent / ToolRuntime
+ Session / Compaction
+ Skills
+ ExtensionRunner / ExtensionHost
+ Coding Tools
+ Coding Prompt / Workspace
= CodingAgentRuntime
```

### 五个 Coding Tool

```text
read_file   parallel    UTF-8 按行读取、offset / limit、截断提示
grep        parallel    regex / literal / glob / context 递归搜索
write_file  sequential  创建或完整覆写 UTF-8 文件
edit        sequential  exact + unique + non-overlap 原子替换
bash        sequential  workspace 下执行 shell、超时/取消/输出截断
```

它们仍然都是普通 `beta_agent.Tool`。

### Session persistence

Agent subscriber 已在 `message_end` 时向 Session 追加消息，因此 `CodingAgentRuntime.save_session()` 不能再次遍历 `agent.messages` 重复 append。新建及 legacy 迁移 baseline 在首次模型请求前进入 Session。

### cwd 不是 sandbox

Coding Agent 的 `cwd` 只是路径解析基点。绝对路径和 `../` 可以被解析；生产级 trust、sandbox、确认 UI 必须由更外层安全机制实现。

---

## 13. 当前 CLI

### DeepSeek CLI

```bash
python examples/deepseek_cli.py
```

### Coding Agent CLI

```bash
python examples/coding_agent_cli.py --cwd . --session .beta/session.jsonl
```

Coding Agent CLI 默认加载 Plan Mode 与 Subagent，并为 child agent 提供新的模型 Adapter factory；Permission Gate 默认加载，可通过 `--no-permission-gate` 关闭。

输入：

```text
/plan
```

会调用 `CodingAgentRuntime.run_command()`，切换只读 Plan Mode。Plan Mode 默认仍是关闭状态。

---

## 14. 教程 00～12（含 02A 补充章）与源码对应

| Chapter | 主题 | 主要落点 |
| --- | --- | --- |
| 00 | Model Boundary | `src/beta_agent/types.py`、`model.py`、`adapters/` |
| 01 | Tool-driven Loop | `src/beta_agent/agent.py`、`tools.py` |
| 02 | Agent Runtime / Events | `src/beta_agent/events.py`、`agent.py` |
| 02A | Cancellation / Abort | `src/beta_agent/cancellation.py`、`events.py`、`agent.py`、`model.py`、`tools.py` |
| 03 | Tool Runtime | `src/beta_agent/tools.py` |
| 04 | Parallel Tools | `src/beta_agent/tools.py` |
| 05 | Steering / Follow-up | `src/beta_agent/agent.py` |
| 06 | Context Transform | `src/beta_agent/agent.py` |
| 07 | Session Tree | `src/beta_agent/session.py` |
| 08 | Context Compaction | `src/beta_agent/compaction.py`、`session.py` |
| 09 | Skills | `src/beta_agent/skills.py` + 产品层读取 Tool |
| 10 | Extension Runtime | `src/beta_agent/extensions/` |
| 11 | Extension Composition | `src/coding_agent/extensions/`、extension tests |
| 12 | Coding Agent Assembly | `src/coding_agent/`、`examples/coding_agent_cli.py`、coding tests |

---

## 15. 新能力应该放在哪里

```text
新 Provider
→ src/beta_agent/adapters/

新的通用 Runtime primitive
→ beta_agent

文件系统 / Shell / Coding Prompt / Coding workflow
→ coding_agent

外部可插拔 Tool
→ ExtensionTool + register_tool

权限 / Policy
→ tool_call interception

临时 Context 修改
→ transform_context / Extension context handler

产品模式切换
→ Extension state + active tools + context + tool_call

长期历史
→ SessionTree

历史压缩
→ Compaction

领域工作方法
→ Skill
```

核心判断标准：如果一个产品能力需要给 `Agent._run()` 增加专属分支，应先检查现有 seam 是否真的不足，而不是直接污染 Core。

---

## 16. 稳定 invariant

修改项目时优先保护：

```text
Provider-specific protocol 不进入 Agent Loop
Tool preflight 保持 source order
并行 execute 可以按 completion order 发事件
Tool Result history 保持 source order
Steering 不打断当前 turn
Follow-up 只在 run 原本要结束时检查
Runtime history 与单次 LLM Context 解耦
Session / Compaction append-only
Extension factory 原子加载
Extension handler failure 隔离
tool_call block 短路
context handler 顺序组成 pipeline
Extension Tool 继续走 Core ToolRuntime
active tools 对下一次模型调用立即生效
Parent / Child Agent history 隔离
coding_agent -> beta_agent 单向依赖
```

测试是这些 invariant 的最终约束。

---

## 17. 当前有意没有实现

当前仍主动不做：

- OS sandbox、完整 trust model 与命令确认 UI；
- MCP；
- pip Extension package discovery；
- hot reload / Extension API version negotiation；
- 完整多 Provider capability matrix；
- 精确 token / cost accounting；
- telemetry backend；
- 更完整 Session storage backend；
- 自动 Compaction 策略；
- 进程级 Subagent isolation。

这些应该继续叠加在已有边界之上，而不是反向复杂化 Agent Core。

---

## 18. 推荐阅读顺序

```text
1. beta_agent/types.py
2. beta_agent/model.py
3. beta_agent/events.py
4. beta_agent/tools.py
5. beta_agent/agent.py
6. beta_agent/session.py
7. beta_agent/compaction.py
8. beta_agent/skills.py
9. beta_agent/extensions/types.py
10. beta_agent/extensions/runner.py
11. beta_agent/extensions/wrapper.py
12. beta_agent/extensions/bridge.py
13. coding_agent/assembly.py
14. coding_agent/tools/
15. coding_agent/extensions/
16. tests/
```

其中最值得反复理解的是：Agent 的控制流边界、Tool lifecycle / parallel invariant、Session 的长期历史结构，以及 Extension Runner / Bridge 如何让产品能力进入已有 seam 而不污染 Core。
