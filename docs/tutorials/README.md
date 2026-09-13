# Beta Agent 教学文档

这组文档参考 `learn-pi-agent` Chapter 00～11 的教学递进，并结合 Beta 当前 Python 实现重新组织。

它们不是原教程的逐句翻译，而是围绕当前仓库解释：一个最小 Agent Framework 为什么会逐步长出 Message、Agent Loop、Event Stream、Tool Runtime、并行执行、Steering / Follow-up、Context、Session、Compaction、Skills，以及最终的 Extension Runtime 与 Extension Composition。

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
| [10](10-extension-runtime.md) | Extension API、Runner、Loader、Bridge | `extensions/` |
| [11](11-extension-composition.md) | Permission、Plan Mode、Subagent 组合 | `examples/extensions/` |

## 怎么读

推荐每章做三件事：

1. 先看“为什么需要这一层”，不要急着记函数名；
2. 再打开文档列出的 Beta 源码，对照职责边界；
3. 最后看对应测试，确认关键 invariant 是怎样被锁住的。

整套课程最重要的一条主线是：

```text
先保持 Core 简单
    ↓
当复杂度真正出现
    ↓
找到稳定 seam
    ↓
把变化隔离到 seam 外
```

Provider 差异留在 Adapter，Tool 复杂度留在 ToolRuntime，历史结构留在 Session，领域知识留在 Skill，而 Permission / Plan Mode / Subagent 这类产品行为则留在 Extension。Agent Loop 只保留必须稳定的控制流。

## 与其他文档的关系

- `../FRAMEWORK.md`：完整介绍 Beta 框架模块；
- `../ARCHITECTURE.md`：精简版架构边界；
- 本目录：按“问题逐步出现”的顺序解释为什么形成这些模块。

## 范围

目前覆盖 Chapter 00～11。Chapter 12 会进入 Coding Agent Assembly：read / write / edit / grep / bash、Workspace 与前面所有 Runtime 能力真正组装到一起。在进入第 12 章之前，Chapter 10～11 的目标是先证明 Extension 能在不污染 Agent Core 的前提下改变完整 Agent 行为。
