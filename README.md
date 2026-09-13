# Beta Agent

A compact Python agent framework inspired by the architectural ideas in Pi and the `learn-pi-agent` tutorial through Chapter 09.

The goal of this repository is not to port Pi line-by-line. It keeps the same useful boundaries while giving them a Python-native shape: an async event-driven agent loop, provider adapters, a tool runtime, steering/follow-up queues, context hooks, branchable sessions, append-only compaction, and lazy skill discovery.

## What is implemented

- Async **Agent Loop** with `agent / turn / message / tool` events.
- Whole-partial `message_update` events for streaming UIs and tracing.
- Provider-neutral `ModelAdapter` protocol plus an **OpenAI-compatible streaming adapter**.
- Typed tools using **Pydantic** schemas.
- Tool lifecycle: lookup → prepare arguments → validate → before hook → execute → after hook → normalized Tool Result.
- Parallel tool execution with deterministic history ordering.
- **Steering** and **follow-up** queues at different checkpoints.
- `transform_context`, `prepare_next_turn`, and graceful-stop hooks.
- Append-only **SessionTree** with branching and JSONL persistence.
- Branch-local **CompactionEntry** semantics without deleting old history.
- **SkillCatalog** that injects only skill metadata; bodies are read lazily through ordinary tools.

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
pytest
```

## Minimal usage

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

See [`examples/basic.py`](examples/basic.py) for a runnable example.

Documentation:

- [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md): 中文框架说明、运行流程、模块职责以及教程 00～09 章对应关系。
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): concise design-boundary reference.

## Steering vs follow-up

```python
agent.steer("B 不用查了，改查 D")
agent.follow_up("最后再总结成三点")
```

Steering is injected only after the current turn (including its tool batch) finishes. Follow-up is checked later, when the run would otherwise end. This keeps the current turn internally consistent.

## Sessions

```python
from beta_agent import SessionTree

session = SessionTree()
entry = session.append_message(...)
session.branch(entry.id)
session.save_jsonl("session.jsonl")
```

The session stores a tree, but `reconstruct_messages()` gives the Agent a normal linear active branch. Compaction is represented by another append-only entry and only changes reconstruction.

## Skills

A skill is a `SKILL.md` with metadata frontmatter:

```markdown
---
name: database-debugging
description: Diagnose connection, slow query, and lock problems
---

Full instructions live here...
```

`SkillCatalog` places only `name`, `description`, and `location` into the system prompt. The model can read the full file through a normal read tool when it decides the skill is relevant.

## Scope

This first skeleton intentionally stops around the capabilities covered through tutorial Chapter 09. Extension runtimes, coding-agent-specific UX, advanced provider features, telemetry, sandboxing, MCP, and richer persistence backends are good next layers, but they should not be prerequisites for a clean core.
