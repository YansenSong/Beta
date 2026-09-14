# Beta Agent

一个参考 Pi Agent 架构思想，并结合 `learn-pi-agent` 教程第 00～12 章设计实现的轻量级 Python 智能体框架。

这个仓库的目标不是把 Pi 的 TypeScript 源码逐行翻译成 Python，而是保留其中最重要的架构边界，并用更符合 Python 使用习惯的方式重新组织：异步事件驱动的 Agent Loop、模型适配层、Tool Runtime、Steering / Follow-up 队列、Context Hook、可分支 Session、追加式 Compaction，以及按需加载的 Skill 机制。

## 当前已经实现

- 异步 **Agent Loop**，统一暴露 `agent / turn / message / tool` 生命周期事件；
- `message_update` 始终携带当前完整 partial message，便于 UI、日志与 Tracing 使用；
- 与 Provider 解耦的 `ModelAdapter` 协议，以及一个 **OpenAI-compatible 流式适配器**；
- 基于 **Pydantic** Schema 的类型化 Tool；
- 完整 Tool 生命周期：lookup → prepare arguments → validate → before hook → execute → after hook → Tool Result；
- 多 Tool 并行执行，同时保持确定性的 history 写入顺序；
- 不同检查点的 **Steering** 与 **Follow-up** 队列；
- `transform_context`、`prepare_next_turn` 和 graceful stop 等扩展 Hook；
- Append-only 的 **SessionTree**，支持分支和 JSONL 持久化；
- Branch-local 的 **CompactionEntry**，压缩 Context 但不删除原始历史；
- **SkillCatalog**，只向 system prompt 注入 Skill metadata，正文按需通过普通 Tool 读取。
- 独立的 **Coding Agent 产品包** `coding_agent`，组装 workspace、`read_file` / `write_file` / `edit` / `grep` / `bash`、Skills、Extensions、Session 和可选 Compaction。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
pytest
```

## 最小使用示例

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
        tools=[Tool("add", "Add two numbers", AddArgs, add)],
    )

    stream = agent.stream("12 + 30 等于多少？")
    async for event in stream:
        if event.type == "message_update" and event.message:
            print(event.message.content)
    await stream.result()

asyncio.run(main())
```

## 文档

- [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md)：完整中文框架说明，包括运行流程、模块职责、关键设计边界，以及教程 00～12 章和当前代码的对应关系；
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：更精简的架构边界与运行流程说明。
- [`docs/tutorials/12-coding-agent.md`](docs/tutorials/12-coding-agent.md)：Chapter 12 Coding Agent 产品层组装说明。

## Steering 与 Follow-up

```python
agent.steer("B 不用查了，改查 D")
agent.follow_up("最后再总结成三点")
```

Steering 不会抢占当前正在执行的 turn。当前 assistant message 以及它触发的 Tool batch 会先完整执行，等 `turn_end` 以后，Steering 才会作为普通 user message 注入下一轮。

Follow-up 的检查点更晚：只有当 Agent 本来已经准备结束当前 run 时，才会检查是否还有后续消息。如果存在 Follow-up，则继续开启下一轮，而不是立即 `agent_end`。

这样可以保证每个 turn 的历史始终保持完整和自洽。

## Session

```python
from beta_agent import SessionTree

session = SessionTree()
entry = session.append_message(...)
session.branch(entry.id)
session.save_jsonl("session.jsonl")
```

Session 内部保存的是一棵 append-only 的历史树，但 Agent 每次实际运行仍然只接收当前 active branch 对应的线性 `Message[]`。

调用 `branch(entry.id)` 时不会删除旧历史，只是把当前 `leaf_id` 移动到指定节点。之后产生的新消息会从这个节点继续形成新的分支。

Compaction 同样不会重写或删除旧 Session Entry，而是追加一个新的 Compaction Entry，并在后续重建 Context 时用摘要替换更早的一段历史。

## Skills

一个 Skill 使用 `SKILL.md` 表示，并在文件顶部声明 metadata：

```markdown
---
name: database-debugging
description: 排查数据库连接、慢查询与锁等待问题
---

完整的领域说明写在这里……
```

`SkillCatalog` 只会把 `name`、`description` 和 `location` 放进 system prompt，不会在启动时把全部 Skill 正文塞进 Context。

当模型判断某个 Skill 与当前任务相关时，可以通过普通的文件读取 Tool 去读取对应 `SKILL.md`。这样既能保持 system prompt 精简，也能复用已有 Tool Runtime，而不需要给 Agent Loop 增加特殊的 `loadSkill()` 或 `executeSkill()` 逻辑。

## 包边界

`beta_agent` 是通用 Agent Framework：Agent Loop、Model Adapter、Tool Runtime、Session、Compaction、Skills 与 Extension Runtime 都在这里。

`coding_agent` 是建立在 `beta_agent` 之上的产品层：Coding Tools、Coding Prompt、Coding Agent assembly 和产品级 extensions 都在独立包中。依赖方向保持为：

```text
coding_agent -> beta_agent
```

Core 不依赖 Coding Agent 产品层。

## 当前范围

当前 Core 与产品层覆盖教程第 00～12 章；Coding Agent 位于独立的 `coding_agent` 包，不改变 Core 的 Agent Loop 和 Tool Runtime。

Coding Agent 的 `cwd` 只是路径解析基点，不是 sandbox；示例 Permission Gate 只是策略演示，不是完整命令安全系统。复杂 Provider 特性、Telemetry、Sandbox、MCP、更完整的持久化后端等仍不属于 Core 的职责。

这些都适合作为后续扩展层，但不应该成为一个清晰、可理解的基础 Agent Runtime 的前置条件。
