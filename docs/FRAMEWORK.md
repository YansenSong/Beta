# Beta Agent 当前框架说明

本文档用于介绍 `Beta` 仓库当前已经搭建完成的智能体框架。它不是 Pi 的逐行 Python 翻译，而是参考 Pi Agent 的核心设计，以及 `learn-pi-agent` 教程第 00～09 章所形成的能力边界，用 Python 重新组织出的一个最小、完整、可扩展骨架。

当前目标不是做一个功能堆满的 Coding Agent，而是先把 Agent 最重要的运行机制稳定下来。后续无论加入 MCP、Extension、沙箱、更多模型 Provider、复杂记忆或 Coding Agent UI，都尽量建立在这些边界之上，而不是反过来改坏 Core。

---

## 1. 当前框架解决什么问题

从最小视角看，一个 Tool-driven Agent 的核心循环只有：

```text
User Message
    ↓
LLM
    ↓
Assistant Message
    ↓
是否包含 Tool Call？
    ├── 否 → 当前任务结束
    └── 是
          ↓
       执行 Tool
          ↓
       Tool Result
          ↓
       再次调用 LLM
```

真正做成框架以后，还需要解决：

- 如何把运行过程实时暴露给 UI / 日志 / Tracing；
- Tool 参数如何校验，失败后怎样反馈给模型；
- 多个 Tool Call 是否可以并行执行；
- 用户在 Agent 工作途中补充要求时，什么时候进入下一轮；
- Runtime history 和某一次 LLM 实际看到的 Context 如何解耦；
- 长会话如何保存、分支和恢复；
- Context 太长时如何压缩但不破坏原始历史；
- 如何让 Agent 按需加载领域工作流，而不是把所有说明都塞进 system prompt。

Beta 当前骨架正是围绕这些问题组织的。

---

## 2. 目录结构

```text
Beta/
├── docs/
│   ├── ARCHITECTURE.md          # 精简版架构边界说明
│   └── FRAMEWORK.md             # 当前这份完整框架说明
├── examples/
│   └── basic.py                 # 最小运行示例
├── skills/
│   └── example/
│       └── SKILL.md             # Skill 示例
├── src/beta_agent/
│   ├── __init__.py              # 公共 API 导出
│   ├── agent.py                 # Agent Loop / Steering / Follow-up
│   ├── types.py                 # Message、Event、ToolCall 等基础数据结构
│   ├── events.py                # 异步 EventStream
│   ├── model.py                 # Provider-neutral ModelAdapter
│   ├── tools.py                 # Tool 与 ToolRuntime
│   ├── builtin_tools.py         # 内置基础 Tool
│   ├── session.py               # Append-only Session Tree
│   ├── compaction.py            # Context Compaction
│   ├── skills.py                # Skill 发现与 metadata catalog
│   └── adapters/
│       └── openai_compatible.py # OpenAI-compatible 流式模型适配器
└── tests/
    ├── test_agent_loop.py
    ├── test_parallel_tools.py
    ├── test_session.py
    └── test_skills.py
```

可以把整个项目粗略分成四层：

```text
Harness / Application
    │
    ├── Session / Compaction / Skills
    │
Agent Runtime
    ├── Agent Loop
    ├── Event Stream
    └── Tool Runtime
    │
Provider Boundary
    └── ModelAdapter
    │
External Model API
```

最重要的原则是：**不同层只理解自己真正需要理解的东西。**

---

## 3. 核心数据模型：`types.py`

`types.py` 是整个框架共享的数据协议层，主要包括：

- `Message`：用户、助手、Tool Result、System 等消息；
- `ToolCall`：模型要求执行的某一次工具调用；
- `ToolResult`：工具执行后的标准化结果；
- `AgentEvent`：运行时事件；
- `AgentContext`：当前 system prompt、messages 和 tools；
- `TurnResult`：一轮完整执行结束后的快照；
- `ToolBatchResult`：一批工具执行后的消息和终止状态。

框架内部尽量围绕这些统一对象工作，而不是让 Agent Loop 直接依赖某个模型厂商的 JSON 格式。

这意味着：

```text
Agent Message / ToolCall
        ↓
   Agent Runtime
        ↓
ModelAdapter 边界
        ↓
OpenAI / 其他 Provider 协议
```

Provider 的特殊格式应该尽量停留在 Adapter 层。

---

## 4. 模型边界：`model.py` 与 `adapters/`

### 4.1 `ModelAdapter`

Agent Loop 不直接调用 OpenAI SDK，而是依赖 `ModelAdapter` 协议。

它关心的是：

```text
system_prompt
messages
available tools
        ↓
stream()
        ↓
ModelEvent
```

这样做的好处是，未来添加 Anthropic、Gemini、本地模型或自定义网关时，不需要重新设计 Agent Loop。

### 4.2 `ScriptedModelAdapter`

测试中使用可脚本化的假模型 Adapter。

它的意义很重要：Agent Runtime 的大部分行为都应该能在**不访问真实模型 API**的情况下进行确定性测试。

### 4.3 `OpenAICompatibleAdapter`

当前提供一个最小的 OpenAI-compatible Chat Completions 流式实现，主要负责：

- 把内部 `Message` 转换为 Provider message；
- 把 `Tool` 的 Pydantic schema 转成 function tool schema；
- 累积流式文本；
- 累积流式 tool call arguments；
- 最终重新产生统一的 `ModelEvent` / `Message`。

因此 Provider protocol 与 Agent Runtime 仍然隔着明确边界。

---

## 5. EventStream：运行过程也是框架输出

如果 Agent 只有：

```python
result = await agent.run(...)
```

那么外部只能等任务彻底结束。

但真实 Agent 可能经历：

```text
开始 run
↓
模型开始输出
↓
模型持续生成文本
↓
Tool 开始执行
↓
Tool 上报进度
↓
Tool 完成
↓
第二轮模型调用
↓
最终结束
```

所以 Beta 把过程暴露为统一事件流。

当前事件包括：

```text
agent_start
agent_end

turn_start
turn_end

message_start
message_update
message_end

tool_execution_start
tool_execution_update
tool_execution_end
```

UI、日志系统、Tracing 系统都可以订阅同一条流，不需要 Agent Loop 分别知道这些消费者是谁。

### 为什么 `message_update` 使用完整 partial message

更新事件不是只发送：

```text
"Hel"
"lo"
```

而是发送当前完整状态：

```text
"Hel"
"Hello"
```

这样消费者不必自己重新拼接 Assistant Message，也更容易同时处理流式 Tool Call。

---

## 6. Agent Loop：`agent.py`

`Agent` 是当前 Runtime 的控制中心，但它刻意不承担 Tool 参数校验、Session Tree、Skill 发现等职责。

一次 run 的主要流程是：

```text
agent_start
    ↓
turn_start
    ↓
注入本轮 user / queued message
    ↓
transform_context（可选）
    ↓
ModelAdapter.stream()
    ↓
Assistant Message
    ↓
Tool Calls?
    ├── 有 → ToolRuntime.execute_batch()
    │          ↓
    │       Tool Results
    │          ↓
    │       turn_end
    │          ↓
    │       下一轮
    │
    └── 无 → turn_end
              ↓
          Steering?
              ↓
          Follow-up?
              ↓
          agent_end
```

### 6.1 为什么 Runtime history 和 LLM input 要分开

`self.context.messages` 保存 Agent 已经发生过的完整历史。

而 `transform_context()` 返回的是：

```text
“这一轮模型应该看到的 messages”
```

例如 Runtime 可以保存 100 条消息，但本轮只发送其中经过裁剪的 20 条。

因此：

```text
Runtime history ≠ Current LLM input
```

这条边界以后可以承载：

- sliding window；
- RAG 临时上下文；
- 内部消息过滤；
- Provider-specific context policy。

### 6.2 `prepare_next_turn`

与 `transform_context()` 不同，`prepare_next_turn()` 可以真正替换下一轮 Runtime context。

可以把它理解成：

```text
transform_context
→ 临时修改“本轮模型看到什么”

prepare_next_turn
→ 修改“下一轮 Runtime 从什么状态继续”
```

---

## 7. Steering 与 Follow-up

这两个概念看起来都像“用户又发了一条消息”，但时序不同。

### 7.1 Steering

假设 Agent 正在执行：

```text
查 A、B、C
```

模型已经发出了三个 Tool Call，这时用户说：

```text
B 不用了，改查 D
```

Beta 不会让这条新消息强行插进当前 Tool batch。

当前 turn 会先完整结束：

```text
assistant tool calls
↓
tool results
↓
turn_end
↓
读取 steering
↓
把新 user message 放入下一轮
```

这样当前历史始终是自洽的。

### 7.2 Follow-up

Follow-up 的读取点更晚。

只有当前 Agent **本来已经准备结束 run** 时，才读取 Follow-up。

例如：

```text
assistant: 任务已经完成
↓
没有 Tool Call
↓
没有 Steering
↓
本来准备 agent_end
↓
发现 Follow-up: “再总结成三点”
↓
继续下一轮
```

因此 Agent Loop 使用双层循环，而不是把两种消息合成一个模糊的 pending queue。

---

## 8. Tool Runtime：`tools.py`

Tool Runtime 的目标是让 Agent Loop 不需要知道每一种 Tool 执行细节。

完整 Tool Call 生命周期是：

```text
Tool Call
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
Tool Result
```

### 8.1 参数验证

每个 Tool 都声明一个 Pydantic `args_model`。

模型参数不是直接传给 Tool handler，而是先经过：

```python
args = tool.args_model.model_validate(raw_args)
```

如果失败，错误会转成模型可见的 Tool Result，而不是让整个 Agent Runtime 崩溃。

### 8.2 before / after hook

`before_tool_call` 适合：

- 权限检查；
- 人工确认；
- 调用次数限制；
- 环境策略。

`after_tool_call` 适合：

- 标准化返回值；
- 加 metadata；
- 修改错误状态；
- 给出 graceful terminate 信号。

这些策略都不需要写进具体 Tool，也不需要塞进 Agent Loop。

---

## 9. 并行 Tool 的两个顺序

这是当前框架里最值得注意的一个设计。

假设模型一次请求：

```text
A → B → C
```

执行时间分别是：

```text
A = 300ms
B = 50ms
C = 150ms
```

真实完成顺序会是：

```text
B → C → A
```

因此观测事件应该反映真实情况：

```text
tool_execution_end(B)
tool_execution_end(C)
tool_execution_end(A)
```

但是 history 如果也按完成速度写入，就可能因为机器负载不同而变成不稳定顺序。

所以 Tool Result 仍然按模型原始顺序写入：

```text
ToolResult(A)
ToolResult(B)
ToolResult(C)
```

最终框架同时保留：

```text
Execution events → completion order
Message history   → source order
```

另外，并行只发生在真正 `execute` 阶段。

前面的：

```text
lookup
prepare
validate
before hook
```

仍按 source order 完成。

---

## 10. Session Tree：`session.py`

Agent Loop 只需要一条线性的 `messages[]`。

但长期会话管理需要支持：

- 保存历史；
- 回到旧节点；
- 从旧节点重新分支；
- 切回其他分支；
- 持久化。

因此 Session 不直接保存“一份可被截断的 messages 数组”，而是保存 Entry Tree。

每个 Entry 有：

```text
id
parent_id
timestamp
type
payload
```

普通历史可能是：

```text
U1 → A1 → U2 → A2
```

如果把 leaf 移回 A1，然后继续追加：

```text
U1
 ↓
A1
 ├── U2 → A2
 └── U2' → A2'
```

旧节点没有任何一个被删除。

### `branch()` 实际做什么

它只是：

```text
移动 leaf_id
```

下一条 Entry 的 `parent_id` 会指向新的 leaf。

### Agent 如何使用 Session Tree

模型不需要知道整棵树。

`get_branch()` 会从当前 leaf 沿 `parent_id` 回溯，再转换成一条线性路径。

因此边界是：

```text
SessionTree
→ 保存完整树

Agent
→ 只得到当前 active branch 的 Message[]
```

---

## 11. Context Compaction：`compaction.py`

长 Session 不应该因为模型 Context Window 有上限，就删除旧历史。

Beta 的做法是把一次压缩本身也保存成 Session Entry。

```text
旧历史
↓
生成 summary
↓
追加 CompactionEntry
```

Compaction Entry 主要记录：

```text
summary
first_kept_entry_id
tokens_before
```

它表达的是：

> 当前分支从这里重建 Context 时，更早的一段消息可以用 summary 替代。

旧 Entry 仍然完整保存在 Session Tree 中。

### 为什么 `first_kept_entry_id` 很重要

 retained tail 不能从任意消息开始。

例如：

```text
assistant(tool_call)
tool_result
```

如果只保留 `tool_result`，Provider 上下文就会失去对应的 Tool Call。

当前实现会尽量把 retained tail 回退到 user message 边界，以避免切坏 Tool 协议关系。

---

## 12. Skills：`skills.py`

Skill 不是 Tool。

Tool 表示：

```text
Agent 可以执行什么操作
```

Skill 表示：

```text
遇到某类任务时，Agent 应该遵循什么工作方法
```

例如：

```text
skills/database-debugging/SKILL.md
```

启动时，Beta 只读取：

```text
name
description
location
```

然后生成一个轻量 catalog 放入 system prompt。

Skill 正文不会全部提前进入上下文。

模型判断 Skill 与当前问题相关后，再通过普通文件读取 Tool 获取正文：

```text
Skill metadata in prompt
        ↓
模型判断相关
        ↓
read(SKILL.md)
        ↓
Tool Result
        ↓
Skill 正文进入当前 run
```

这样可以做到“能力很多，但每次 Context 只加载真正相关的说明”。

---

## 13. 与 learn-pi-agent 00～09 章的对应关系

| 教程章节 | Beta 中的主要落点 | 当前能力 |
| --- | --- | --- |
| 00 Minimal LLM Call | `model.py`, `adapters/` | 模型抽象、流式输出、Provider 边界 |
| 01 Tool-driven Agent | `agent.py`, `tools.py` | LLM → Tool → Tool Result → LLM 循环 |
| 02 Agent Runtime | `events.py`, `agent.py` | Agent / Turn / Message 生命周期事件 |
| 03 Tool Runtime | `tools.py` | lookup、prepare、validate、hook、execute |
| 04 Parallel Tools | `tools.py` | 顺序 preflight、并发 execute、稳定 history |
| 05 Steering & Follow-up | `agent.py` | 两种消息队列与不同检查点 |
| 06 Context Transform | `agent.py` | `transform_context`, `prepare_next_turn` |
| 07 Session Tree | `session.py` | parent/leaf、branch、JSONL 持久化 |
| 08 Context Compaction | `compaction.py` | append-only CompactionEntry、重建短 Context |
| 09 Skills | `skills.py`, `builtin_tools.py` | Skill metadata catalog、正文 lazy load |

因此目前这版可以理解为：

> 把教程第 00～09 章中已经形成的稳定 Core 能力合并成一个 Python 包，而不是继续保持“每章一份 demo”。

---

## 14. 一个最小运行示例

```python
import asyncio
from pydantic import BaseModel

from beta_agent import Agent, Tool, ToolExecutionContext, ToolResult
from beta_agent.adapters import OpenAICompatibleAdapter


class AddArgs(BaseModel):
    a: float
    b: float


async def add(args: AddArgs, ctx: ToolExecutionContext) -> ToolResult:
    return ToolResult(content=str(args.a + args.b))


async def main():
    model = OpenAICompatibleAdapter(
        model="gpt-4.1-mini",
        api_key="...",
    )

    agent = Agent(
        model=model,
        tools=[
            Tool(
                name="add",
                description="Add two numbers",
                args_model=AddArgs,
                handler=add,
            )
        ],
    )

    stream = agent.stream("12 + 30 等于多少？")

    async for event in stream:
        print(event.type)

    messages = await stream.result()
    print(messages[-1].content)


asyncio.run(main())
```

更完整的可运行示例见：

```text
examples/basic.py
```

---

## 15. 新功能应该放在哪里

后面扩展时，可以先用下面的判断方式决定代码位置。

### 新 Provider

放在：

```text
src/beta_agent/adapters/
```

不要让 Agent Loop 认识 Provider-specific JSON。

### 新 Tool

实现：

```text
Tool + Pydantic args model + handler
```

尽量不要修改 `ToolRuntime`。

### 权限 / 审批 / 调用策略

优先使用：

```text
before_tool_call
after_tool_call
```

### 临时裁剪模型 Context

使用：

```text
transform_context
```

不要直接删除 Runtime history。

### 下一轮要真正切换 Runtime 状态

使用：

```text
prepare_next_turn
```

### 会话持久化 / 分支历史

属于：

```text
Session 层
```

不要塞进 Agent Loop。

### 长历史摘要

属于：

```text
Compaction + Session reconstruction
```

### 任务方法论 / 领域流程

优先考虑：

```text
Skill
```

而不是不断膨胀全局 system prompt。

---

## 16. 当前有意没有加入的内容

当前骨架停在教程 Chapter 09 左右，下面这些能力还没有进入 Core：

- Extension Runtime；
- Extension Composition；
- Coding Agent 专用工具和 UI；
- MCP；
- sandbox / command approval；
- 多 Provider 完整能力矩阵；
- token accounting 与成本统计；
- telemetry / tracing backend；
- 更完整的 Session persistence backend；
- 自动 compaction 策略；
- 多 Agent orchestration。

这些并不是“遗漏”，而是当前阶段主动控制范围。

一个健康的演进顺序应该是：

```text
先稳定 Core 语义
    ↓
保证测试覆盖
    ↓
再逐层增加扩展能力
```

而不是一开始就让 Agent Loop 同时承担所有功能。

---

## 17. 推荐的阅读顺序

如果准备真正理解并继续维护这个框架，建议按下面的顺序读源码：

```text
1. types.py
   ↓
2. model.py
   ↓
3. events.py
   ↓
4. tools.py
   ↓
5. agent.py
   ↓
6. session.py
   ↓
7. compaction.py
   ↓
8. skills.py
   ↓
9. adapters/openai_compatible.py
   ↓
10. tests/
```

其中最值得反复理解的三个文件是：

```text
agent.py
→ Agent 的控制流与时间边界

tools.py
→ Tool Call 的生命周期和并发语义

session.py
→ Runtime history 之外的长期会话结构
```

理解这三块之后，后续第 10 章之后的扩展会容易很多。
