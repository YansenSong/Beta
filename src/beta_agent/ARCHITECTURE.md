# beta_agent runtime layout

The package follows the same separation principle as Pi Agent's core runtime while
remaining idiomatic Python: the stateful Agent facade is small, and the turn loop
is isolated from persistence, provider DTOs, tools, and product integrations.

```text
beta_agent/
├── agent.py              # stateful facade: queues, lifecycle, public run APIs
├── agent_loop.py         # low-level turn/model/tool execution
├── messages.py           # transcript message domain model
├── types.py              # shared core runtime types
├── runtime/              # cancellation, events, errors, transcript state
├── providers/            # provider protocol, DTOs, request policy
├── tools/                # tool definitions and execution runtime
├── session/              # append-only session tree and codecs
├── compaction/           # session compaction policy
├── skills/               # skill discovery/catalog
├── adapters/             # concrete provider adapters
├── durable/              # optional durable runtime / recovery
└── extensions/           # extension bridge and runner
```

## Dependency direction

- `agent.py` owns mutable Agent state and delegates execution to `agent_loop.py`.
- `agent_loop.py` coordinates model streaming and tools, but does not own product
  assembly, persistence backends, or extension registration.
- `runtime/` contains small provider-agnostic primitives used by the core.
- `providers/` contains the model boundary and provider request DTO/policy.
- Feature areas with multiple responsibilities are packages instead of large root
  modules (`tools/`, `session/`, `compaction/`, `skills/`).
- `durable/` and `extensions/` stay outside the minimal core loop and plug in
  through existing seams.

## Compatibility

Existing imports such as `beta_agent.model`, `beta_agent.events`,
`beta_agent.provider_policy`, `beta_agent.transcript`, `beta_agent.tools`,
`beta_agent.session`, `beta_agent.compaction`, and `beta_agent.skills`
continue to resolve. Root modules that moved under `runtime/` or `providers/`
are forwarding facades; package conversions re-export their previous public
symbols from `__init__.py`.

This lets internal code adopt the clearer layout without forcing downstream users
to migrate imports in the same release.
