# Beta Agent 教学文档

这组文档参考 [`learn-pi-agent`](https://github.com/yiz-hhh/learn-pi-agent) Chapter 00～09 的教学递进，并结合 Beta 当前 Python 实现重新组织。

它们**不是原教程的翻译**，也不会再创建一套配套 demo 代码。目标是让你直接围绕当前仓库理解：一个最小 Agent Framework 为什么会逐步长出 Message、Agent Loop、Event Stream、Tool Runtime、并行执行、Steering / Follow-up、Context、Session、Compaction 和 Skills。

## 建议学习顺序

| 章节 | 主题 | 主要对应源码 |
| --- | --- | --- |
| [00](00-model-boundary.md) | 模型边界与内部 Message | `types.py`、`model.py`、`adapters/` |
| [01](01-tool-driven-loop.md) | Tool-driven Agent Loop | `agent.py`、`tools.py` |
| [02](02-agent-runtime-events.md) | Agent Runtime 与事件流 | `events.py`、`agent.py` |
| [03](03-tool-runtime.md) | Tool Runtime 生命周期 | `tools.py` |
| [04](04-parallel-tools.md) | 并行 Tool 与两种顺序 | `tools.py` |
| [05](05-steering-followup.md) | Steering / Follow-up | `agent.py` |
| [06](06-context-transform.md) | Runtime History 与 LLM Context | `agent.py` |
| [07](07-session-tree.md) | 可分支 Session Tree | `session.py` |
| [08](08-context-compaction.md) | Append-only Compaction | `compaction.py`、`session.py` |
| [09](09-skills.md) | Skill Catalog 与按需加载 | `skills.py`、`builtin_tools.py` |

## 怎么读

推荐每章做三件事：

1. 先只看“为什么需要这一层”，不要急着研究实现细节；
2. 再打开文档列出的 Beta 源码，对照职责边界；
3. 最后回答章节末尾的检查题，确认自己理解的是设计而不是函数名。

整套课程里最重要的一条主线是：

```text
先保持 Core 简单
    ↓
当复杂度真正出现
    ↓
找到稳定边界
    ↓
把变化隔离到边界外
```

例如 Provider 差异留在 Adapter，Tool 复杂度留在 ToolRuntime，历史结构留在 Session，领域流程留在 Skill。Agent Loop 只保留必须稳定的控制流。

## 与其他文档的关系

- [`../FRAMEWORK.md`](../FRAMEWORK.md)：完整介绍当前 Beta 框架有哪些模块；
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md)：精简版架构边界；
- 本目录：按“问题逐步出现”的顺序解释为什么会形成这些模块。

## 范围

目前教程只覆盖 Chapter 00～09，因为这正好对应 Beta 当前 Core 的主要能力。原教程 Chapter 10～12 涉及 Extension Runtime、Extension Composition 和 Coding Agent，可以等 Beta 真正加入相应实现后，再继续扩展这里的课程。