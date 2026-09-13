# 架构说明

Beta Agent 有意保持 Core Runtime 足够小，并把容易变化的策略放到明确的扩展边界上。

目标不是让 Agent Loop 理解所有能力，而是让它只负责稳定的运行时控制流；Provider 协议、Tool 执行细节、Session 历史、Context 压缩和 Skill 发现分别由独立模块负责。

## 运行流程

```text
用户消息 / 队列消息
        ↓
transform_context（只改变本轮模型视图）
        ↓
ModelAdapter 流式调用
        ↓
Assistant Message
        ↓
0..N 个 Tool Call
        ↓
prepare
（lookup → 参数预处理 → validate → before hook）
        ↓
execute
（允许时并行执行）
        ↓
after hook → Tool Result Message
        ↓
turn_end
        ↓
Steering 检查点
        ↓
下一轮 / Follow-up 检查点
```

整个运行过程通过统一 Event Stream 暴露，主要生命周期包括：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

其中 `message_update` 携带的是当前最新的完整 partial message，而不是单独的文本 delta。这样 UI、日志和 Tracing 不需要各自重新维护一份消息拼接状态。

## 模块边界

- **ModelAdapter**：负责 Provider 协议转换与模型流式调用。Agent Runtime 不直接依赖某一家模型厂商的请求/响应格式；
- **Agent**：负责 turn 语义、Steering / Follow-up 检查点、消息历史更新以及主控制流；
- **ToolRuntime**：负责 Tool lookup、参数预处理与校验、before/after hook、执行、进度事件以及结果标准化；
- **SessionTree**：负责可持久化、可分支的历史结构。即使 Session 内部是一棵树，Agent 实际消费的仍然是当前 active branch 对应的一条线性消息序列；
- **Compaction**：以 append-only Session Entry 的形式记录压缩结果。它只改变 Context 重建方式，不删除旧历史；
- **SkillCatalog**：负责发现 Skill metadata。Skill 正文继续保存在文件系统中，需要时通过 `read_text_file` 一类普通 Tool 进入 Context。

## Context 的两个层次

框架区分两类 Context 修改：

```text
transform_context
→ 只影响“这一轮模型看到什么”
→ 不应该默认改写 Runtime history

prepare_next_turn
→ 可以真正替换下一轮 Runtime 使用的 AgentContext
```

这两个入口分别对应“模型输入视图”和“Runtime 状态”两个层次，避免 Context 策略直接侵入 Agent Loop。

## 并行 Tool 的关键不变量

并行 Tool 执行遵守两种不同的顺序：

```text
Tool Execution Event
→ 按真实完成顺序产生

Tool Result 写入 History
→ 按 Assistant 原始 Tool Call 顺序写入
```

例如模型按以下顺序请求：

```text
A → B → C
```

如果实际执行时间导致完成顺序是：

```text
B → C → A
```

那么 `tool_execution_end` 会按：

```text
B → C → A
```

实时发生；但最终 Tool Result Message 仍按：

```text
A → B → C
```

写回消息历史。

这样既保证 Observability 反映真实执行过程，也保证 Session / Context 的历史顺序稳定、可复现。

另外，并行只发生在真正的 `execute` 阶段。Tool 的 lookup、参数准备、schema validate、`before_tool_call` 等 preflight 阶段仍按原始 Tool Call 顺序执行，避免权限确认等交互同时发生。

## Steering 与 Follow-up 的边界

Steering 和 Follow-up 最终都会作为普通 user message 进入历史，但它们的检查时间不同。

**Steering** 在当前 turn 完整结束后读取：

```text
Assistant
↓
Tool Batch
↓
Tool Results
↓
turn_end
↓
读取 Steering
↓
下一轮
```

它不会取消或抢占已经开始的 Tool Call。

**Follow-up** 则只在 Agent 本来准备结束整个 run 时读取：

```text
当前任务已结束
↓
没有 Tool Call
↓
没有 Steering
↓
检查 Follow-up
├── 有 → 继续下一轮
└── 无 → agent_end
```

因此两者需要保留不同的 checkpoint，而不是合并成一个含义模糊的 pending-message 入口。

## Session 与 Compaction

Session 使用 append-only Tree 保存历史。

每个 Entry 只记录自己的 `parent_id`，Session 通过 `leaf_id` 表示当前 active branch 的末端。调用 `branch(entry_id)` 时，只移动 leaf，不会删除旧分支。

Compaction 也遵守同样的 append-only 原则：

```text
原始历史 Entry
        ↓
追加 CompactionEntry
        ↓
未来 reconstruct_messages()
使用 summary + retained tail
```

原始历史仍然完整存在，因此 Branch、恢复和持久化不会因为 Context 压缩而丢失信息。

## Skill 的边界

Skill 不属于 Agent Loop，也不属于 Session 的特殊 Entry 类型。

启动时只发现：

```text
name
description
location
```

并把这些 metadata 放入 system prompt。

模型真正需要某个 Skill 时，再通过普通文件读取 Tool 获取 `SKILL.md` 正文。此时正文作为正常 Tool Result 进入消息历史。

因此：

```text
Skill Catalog
→ 来自当前项目环境

实际读取过的 Skill 内容
→ 属于本次 Agent 执行历史
```

这让新能力尽量沿用已经存在的 Message / Tool / Context 边界，而不是不断给 Core 增加特殊分支。

## 当前已经预留的扩展点

当前骨架已经包含教程第 00～09 章所需要的主要扩展缝隙：

- Context transformation；
- `prepare_next_turn`；
- graceful stop；
- Steering；
- Follow-up；
- Tool before / after hook；
- Sequential / Parallel Tool；
- Session branching；
- Context Compaction；
- Skill discovery。

后续增加更多 Provider、MCP、Extension Runtime、Sandbox、Telemetry、Coding Agent UI 或更复杂持久化时，应优先沿这些边界扩展，而不是把新策略继续塞进 Agent Loop。
