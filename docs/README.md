# Beta Agent 文档索引

`docs/` 用来说明 Beta 当前已经实现的框架边界、扩展机制和学习路径。

如果你第一次阅读这个项目，推荐按下面顺序进入：

1. [`ARCHITECTURE.md`](ARCHITECTURE.md)：先看系统边界、数据流和几个必须保持稳定的不变量；
2. [`FRAMEWORK.md`](FRAMEWORK.md)：再看每个模块为什么存在、彼此如何协作；
3. [`EXTENSIONS.md`](EXTENSIONS.md)：理解 Chapter 10～11 新加入的 Extension Runtime，以及如何写 Extension；
4. [`tutorials/`](tutorials/)：按 Chapter 00～11 的顺序重新走一遍框架演进过程。

## 当前文档覆盖范围

当前代码和文档已经覆盖：

```text
00  Model Boundary
01  Tool-driven Loop
02  Agent Runtime / Events
03  Tool Runtime
04  Parallel Tools
05  Steering / Follow-up
06  Context Transform
07  Session Tree
08  Context Compaction
09  Skills
10  Extension Runtime
11  Extension Composition
```

下一阶段才进入 Chapter 12：Coding Agent Assembly。

## 文档与代码的关系

| 文档 | 解决的问题 | 主要对应源码 |
| --- | --- | --- |
| `ARCHITECTURE.md` | 系统边界和稳定 invariant 是什么 | `src/beta_agent/` 全局 |
| `FRAMEWORK.md` | 当前框架各模块如何协作 | `agent.py`、`tools.py`、`session.py`、`extensions/` |
| `EXTENSIONS.md` | 如何编写、加载和组合 Extension | `src/beta_agent/extensions/`、`examples/extensions/` |
| `tutorials/00～11` | 为什么框架一步步长成现在这样 | 每章对应的源码和测试 |

## 当前架构主线

Beta 现在可以粗略看成四层：

```text
Application / Harness
├── CLI / UI
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

最重要的设计原则仍然是：**新能力优先复用已有 seam，而不是继续往 Agent Loop 里增加产品级分支。**

例如：

```text
Permission Gate
→ tool_call interception

Plan Mode
→ active tools + context interception + tool_call interception

Subagent
→ 普通 Extension Tool + Child Agent
```

因此 `permission_mode`、`plan_mode`、`subagent_branch` 都没有进入 Agent Core。

## 运行与验证

安装开发依赖后：

```bash
pip install -e ".[dev]"
pytest
```

当前真实模型 CLI 示例：

```bash
python examples/deepseek_cli.py
```

Extension 组合示例位于：

```text
examples/extensions/
```

当文档与代码出现不一致时，以测试和当前 `main` 源码为准，并同步修正文档。