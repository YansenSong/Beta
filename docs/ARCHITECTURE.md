# Architecture

This project intentionally keeps the core runtime small and pushes policy to explicit boundaries.

## Runtime flow

```text
user / queued messages
        ↓
transform_context (view only)
        ↓
model adapter stream
        ↓
assistant message
        ↓
0..N tool calls
        ↓
prepare (lookup → normalize → validate → before hook)
        ↓
execute (parallel when allowed)
        ↓
after hook → tool result messages
        ↓
turn_end
        ↓
steering checkpoint
        ↓
next turn or follow-up checkpoint
```

The event stream exposes `agent_*`, `turn_*`, `message_*`, and `tool_execution_*` lifecycles. `message_update` carries the latest whole partial message rather than only a text delta.

## Boundaries

- **ModelAdapter** owns provider protocol conversion and streaming.
- **Agent** owns turn semantics, queue checkpoints, and history mutation.
- **ToolRuntime** owns tool lookup, argument preparation/validation, hooks, execution, progress, and result normalization.
- **SessionTree** owns durable branchable history; the Agent still consumes a linear active branch.
- **Compaction** is an append-only session entry. It shortens reconstructed context without deleting historical entries.
- **SkillCatalog** discovers skill metadata. Skill bodies stay on disk and should enter context only through ordinary tools such as `read_text_file`.

## Parallel tool invariant

Preflight is source-ordered. Allowed tool executions run concurrently. Completion events are emitted in real completion order, while tool-result messages are committed to history in the assistant's original tool-call order. This keeps observability truthful and history deterministic.

## Extension points already present

The skeleton deliberately includes the extension seams needed by the tutorial through chapter 09: context transformation, `prepare_next_turn`, graceful stop, steering, follow-up, before/after tool hooks, sequential-vs-parallel tools, session branching, compaction, and skill discovery.
