# Beta 生产级 Durable Runtime v1：Codex 实施交接

> 本文是一份可直接交给 Codex 执行的工程规格。执行者只需要访问 Beta 仓库，不需要访问 Pi 仓库，也不要在实现过程中自行猜测 Pi 的行为。

## 0. 基线、事实与上游状态

本文基于以下源码快照完成核对：

- Beta：`0ce490655b8ec052cb80909bc5dbcc9cea60487d`（2026-09-20）
- Pi：`19451accdeec671c1f4da9eafac8fc270f510ef4`（2026-09-20）

Beta 当前已经完成上一轮 runtime 重构，包括：

- transcript-native system/tool state；
- `QueueMode`；
- richer `NextTurnUpdate`；
- rich `ToolResult`；
- late tool-progress guard；
- awaited event subscribers；
- Session v3 与旧格式迁移；
- 独立的 `coding_agent` 产品包。

这些能力不是本轮重做对象。

Pi 当前有两套可参考依据，必须区分：

1. `packages/agent/src/harness/pico3` 是已经存在的 durable harness 实现，包含 task phase、effect sandwich、tool replay、abort、恢复、durable progress 等运行语义。
2. `packages/durable` / Pico5 是新的目标架构。当前真正落地的主要是 durable records、Storage contract 和 `MemoryStorage`；SQLite、JSONL、完整 scheduler、tool task、generation task 等仍处于规范/实施清单阶段。

因此，本轮要吸收的是两者已经一致的核心思想，不要声称“一比一移植了完整 Pico5”，也不要照搬 Pico3 的所有内部 facade。

上游源码仅作为本文的来源记录：

- [Pi pinned commit](https://github.com/earendil-works/pi/tree/19451accdeec671c1f4da9eafac8fc270f510ef4)
- [Beta pinned commit](https://github.com/YansenSong/Beta/tree/0ce490655b8ec052cb80909bc5dbcc9cea60487d)

## 1. 本轮目标

实现一个默认关闭、可选启用的 **Beta Durable Agent Runtime v1**，覆盖：

1. provider request policy；
2. durable run/task identity；
3. tool operation journal；
4. effect intent / outcome 两阶段持久化；
5. `safe` / `unsafe` tool replay policy；
6. 进程崩溃后的恢复与 uncertain/interrupted 处理；
7. transactional outbox 驱动的 transcript 补写与去重；
8. Coding Agent 内置工具的 replay 策略声明；
9. 完整的故障注入测试。

核心边界：

```text
beta_agent
├── core agent loop
├── provider request policy
└── durable runtime
    ├── records / storage
    ├── task & operation journal
    ├── outbox
    └── recovery

coding_agent
└── 只提供工具声明、workspace 组装和产品策略
```

机制属于 `beta_agent`；某个工具是否可安全 replay 的知识属于工具定义本身。

## 2. 明确不做的事情

本轮不要扩大为完整 Pico5 重写。以下均为非目标：

- 不删除或替换现有 `SessionTree`；
- 不把 `coding_agent` 逻辑反向塞进 `beta_agent`；
- 不实现 Chord/document family/document watch；
- 不实现 CRDT、多进程并发写入或分布式 scheduler；
- 不保证任意外部副作用 exactly-once；
- 不自动 replay `bash`、`edit` 或未知第三方工具；
- 不把 `unsafe` 工具擅自升级成 `safe`；
- 不在恢复时重新执行 `before_tool_call`；
- 不自动恢复旧进程中未完成的流式 token；
- 不实现 background task、task dependency DAG 或 owned conversation；
- 不实现 reconciliation hook；v1 对不安全且结果未知的 effect 生成明确的 interrupted result；
- 不发送 Pi 专属字段给 OpenAI-compatible API；provider 不支持的 request option 必须忽略或明确拒绝，不能偷偷拼进 payload。

## 3. 必须保持的兼容性

1. 未配置 durable 选项时，现有 `Agent`、`ToolRuntime`、`CodingAgentRuntime` 行为不变。
2. 现有 `ModelAdapter` 实现如果没有新的 `request_options` 参数，仍应能工作。
3. Session v1/v2/v3 继续可加载。
4. 现有公开 import 尽量不破坏；新类型从 `beta_agent.__init__` 导出。
5. Python 最低版本仍为 3.10；不要使用仅 3.11+ 才有的 `StrEnum`。
6. 继续使用 dataclass、Protocol 和 stdlib；本轮不要增加 ORM 或异步 SQLite 依赖。

## 4. 关键设计原则

### 4.1 Effect sandwich

任何可能产生外部副作用的工具必须遵守：

```text
durably commit effect intent
             ↓
perform external effect outside storage transaction
             ↓
durably commit outcome + outbox message
             ↓
publish/persist transcript message
             ↓
acknowledge delivery
```

不得在数据库事务内运行模型、工具、shell、网络请求、用户回调或 extension hook。

### 4.2 默认不安全

工具不声明 replay policy 时，默认：

```python
replay_policy = "unsafe"
```

恢复时只有“持久化 intent 中保存的 policy”和“当前注册工具的 policy”都为 `safe`，才允许自动 replay：

```text
stored safe + current safe   -> replay
stored safe + current unsafe -> interrupted
stored unsafe + current safe -> interrupted
stored unsafe + current unsafe -> interrupted
```

当前声明只能收紧，不能把过去保存的 unsafe intent 升级。

### 4.3 恢复使用最终调用，不重新做准入决策

正常执行时，顺序仍是：

```text
lookup
→ prepare_arguments
→ validate
→ before_tool_call
→ validate final args
→ persist final call + replay policy
→ execute
→ after_tool_call
→ persist outcome
```

恢复 replay 时：

- 使用 intent 中保存的最终 tool name 与 args；
- 不重新执行 `prepare_arguments`；
- 不重新执行 `before_tool_call`；
- 必须用当前 schema 再验证一次；
- replayed execution 完成后可以执行 `after_tool_call`；
- stable operation id、tool call id、batch id 和 source index 不变；
- `attempt` 加一，并向 execution context 暴露 `is_recovery=True`。

### 4.4 Durable progress 才能称为 progress

现有 `tool_execution_update` 仍可以是 UI 事件，但 durable mode 下恢复所依赖的状态必须先写入 journal。未提交的最后一个 progress 窗口在崩溃时允许丢失，不能据此判断 effect 已完成。

### 4.5 Conversation persistence 与 task persistence 概念分离，但交付要可恢复

`SessionTree` 回答“说过什么”；durable journal 回答“正在做什么、effect 是否可能已经发生”。v1 暂不把二者合成一个存储引擎，因此必须用 transactional outbox 修复跨存储 dual-write：

- outcome 与待投递 tool-result message 在同一个 SQLite transaction 中提交；
- Session 写入完成后才 ack outbox；
- 崩溃后 pending outbox 重新投递；
- transcript 通过 durable message id 去重；
- outbox 是未完成投递的 source of truth。

## 5. 新包与文件布局

新增：

```text
src/beta_agent/
├── provider_policy.py
└── durable/
    ├── __init__.py
    ├── records.py
    ├── serialization.py
    ├── storage.py
    ├── memory.py
    ├── sqlite.py
    ├── coordinator.py
    ├── recovery.py
    └── failpoints.py
```

修改：

```text
src/beta_agent/model.py
src/beta_agent/agent.py
src/beta_agent/tools.py
src/beta_agent/types.py
src/beta_agent/messages.py
src/beta_agent/session.py
src/beta_agent/extensions/bridge.py
src/beta_agent/extensions/types.py
src/beta_agent/extensions/wrapper.py
src/beta_agent/adapters/openai_compatible.py
src/beta_agent/__init__.py

src/coding_agent/assembly.py
src/coding_agent/tools/read_file.py
src/coding_agent/tools/grep.py
src/coding_agent/tools/write_file.py
src/coding_agent/tools/edit.py
src/coding_agent/tools/bash.py
```

新增测试：

```text
tests/test_provider_policy.py
tests/test_durable_memory_storage.py
tests/test_durable_sqlite_storage.py
tests/test_durable_tool_runtime.py
tests/test_durable_recovery.py
tests/test_durable_outbox.py
tests/test_coding_replay_policies.py
```

## 6. Provider request policy

### 6.1 数据模型

在 `provider_policy.py` 新增不可变数据类。字段名可以按项目风格微调，但语义必须保持：

```python
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping

Transport = Literal["auto", "http", "sse", "websocket"]

@dataclass(frozen=True, slots=True)
class RetryPolicy:
    enabled: bool = True
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

@dataclass(frozen=True, slots=True)
class ProviderRequestOptions:
    session_id: str | None = None
    timeout_seconds: float | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    headers: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    transport: Transport = "auto"
    reasoning: str | None = None
    thinking_budget_tokens: int | None = None
```

要求：

- 构造时校验 retry 次数、延迟、timeout、thinking budget 都是有限且非负；
- headers/metadata 要 defensive copy；
- 提供明确的 merge 函数，headers/metadata 做 key merge，其余非 `None` 字段覆盖；
- policy 是“每个 provider request 的快照”，开始请求后外部修改不得影响当前 attempt；
- callbacks 不放进 durable JSON，也不要放进这个 frozen data object。

### 6.2 Agent 与 adapter 边界

给 `AgentConfig` 增加默认 `provider_request_options`。给 `NextTurnUpdate` 增加可选 request-options patch，使下一轮可以调整 timeout/retry/metadata，但不要把它混进 transcript。

扩展 `ModelAdapter.stream()`：

```python
async def stream(
    *,
    system_prompt: str,
    messages: Sequence[ProviderMessage],
    tools: Sequence[object],
    request_options: ProviderRequestOptions,
    cancellation: CancellationToken,
) -> AsyncIterator[ModelEvent]: ...
```

为了兼容旧 adapter，新增一个统一调用 helper，通过签名检测仅在 adapter 接受 `request_options` 时传入。不要在 Agent 主循环散落 `try TypeError -> retry without arg`，因为 adapter 内部真实的 `TypeError` 不能被误判为签名不兼容。

### 6.3 OpenAI-compatible adapter

修改 `OpenAICompatibleAdapter`：

- `api_key` 支持字符串或 sync/async resolver，并在每个 attempt 前重新解析；
- request options 的 timeout、headers 与 adapter 默认值合并；
- auth header 最后写入，除非明确允许，否则用户 headers 不能覆盖 Authorization；
- `before_payload` hook 接收 payload copy，可返回替换 payload；
- `on_response` hook 只接收 status 与普通 response headers，不能收到 Authorization；
- `session_id` 默认不拼进 OpenAI payload；允许构造 adapter 时显式指定 `session_header` 后映射到 header；
- `metadata`、reasoning、thinking budget 只有 adapter 明确支持时才映射；否则保留给其他 adapter，不能发送未知字段；
- `transport="auto" | "http" | "sse"` 可映射到现有 SSE-over-HTTP 实现；`websocket` 必须明确报 unsupported，不能静默假装支持。

### 6.4 Retry 语义

实现取消可中断的退避：

- 默认最多 3 次 retry，首次调用不计入 retry；
- retryable：连接错误、timeout、HTTP 408/409/429/5xx、`x-should-retry: true`；
- non-retryable：普通 4xx、认证错误、schema 错误、明确 quota/billing exhaustion、`x-should-retry: false`；
- 优先尊重 `retry-after-ms` / `retry-after`；超过 `max_delay_seconds` 时立即失败；
- 无服务端 delay 时使用指数退避并加小幅 jitter；
- cancellation 在请求中与 sleep 中都必须立即生效；
- 只有在还没向 Agent 发出任何 assistant `start/update` 时允许 adapter 内 retry；
- 流已经开始后中断，返回一个 `stop_reason="error"` 的 assistant message，本轮不自动从头重放，避免重复 partial/tool call；
- 不同时叠加 httpx 自带 retries 和本层 retries。

需要新的 provider lifecycle 事件时，优先通过现有 `AgentEvent` 扩展 `provider_retry_scheduled` / `provider_retry_started` / `provider_retry_finished`，不要伪装成 tool event。

## 7. Durable records

### 7.1 通用约束

所有 durable payload 必须为 JSON-safe value。写入前做一次明确的序列化验证；不要允许任意 Python 对象、Exception、Path、Pydantic model 直接进入存储。

ID 使用不可猜测的字符串（推荐 `uuid.uuid4().hex`）。tool call id 不是 durable operation id，也不能作为主键。

时间统一使用 UTC ISO string，沿用 `utc_now_iso()`。

### 7.2 TaskRecord

在 v1 中只内置 `agent_run` 与 `tool_call` 两种 task kind，但 record 设计保持通用：

```python
TaskStatus = Literal["pending", "running", "terminal"]
TaskOutcomeStatus = Literal[
    "completed", "failed", "aborted", "orphaned", "faulted"
]

@dataclass(frozen=True, slots=True)
class StoredError:
    message: str
    detail: JsonValue | None = None

@dataclass(frozen=True, slots=True)
class TaskOutcome:
    status: TaskOutcomeStatus
    result: JsonValue | None = None
    error: StoredError | None = None
    reason: str | None = None

@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    session_id: str
    kind: str
    version: int
    status: TaskStatus
    input: JsonValue
    checkpoint: JsonValue | None
    outcome: TaskOutcome | None
    abort_requested: bool
    background: bool
    created_at: str
    updated_at: str
```

约束：

- `pending/running` 必须有完整 checkpoint，不能依赖进程内对象恢复；
- `terminal` 必须有 outcome，并清除 checkpoint 与 live memos；
- task record 每次是完整替换，不持久化不透明 patch；
- reopen 时遗留 `running` 先恢复为 `pending`，保留 checkpoint 与 abort mark；
- 未知或不支持迁移的 live kind 变成 `orphaned`，不要删除；
- task definition version 为持久化协议，未来迁移必须显式处理。

### 7.3 ToolOperationRecord

```python
ReplayPolicy = Literal["safe", "unsafe"]
ToolOperationStatus = Literal[
    "effect_pending", "outcome_ready", "completed"
]

@dataclass(frozen=True, slots=True)
class ToolOperationRecord:
    id: str
    task_id: str
    run_id: str
    batch_id: str
    source_index: int
    tool_call_id: str
    tool_name: str
    arguments: JsonValue
    arguments_hash: str
    replay_policy: ReplayPolicy
    status: ToolOperationStatus
    attempt: int
    result: JsonValue | None
    is_error: bool | None
    terminate: bool | None
    created_at: str
    updated_at: str
```

`arguments_hash` 使用 canonical JSON（sorted keys、UTF-8、稳定 separators）计算 SHA-256，仅用于诊断/完整性检查，不能代替保存 arguments。

`result` 保存完整的 rich `ToolResult`：text/image blocks、details、usage、terminate、added tool names。必须复用公开 serializer，不要依赖私有 `_message_to_dict`。

### 7.4 OutboxRecord

```python
OutboxStatus = Literal["pending", "published"]

@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: str
    operation_id: str
    batch_id: str
    source_index: int
    durable_message_id: str
    message: JsonValue
    status: OutboxStatus
    created_at: str
    published_at: str | None
```

数据库对 `durable_message_id` 和 `operation_id` 建唯一约束。一个 operation 只能产生一个最终 transcript result。

## 8. Storage contract 与实现

### 8.1 Protocol

`storage.py` 定义 async `DurableStorage` Protocol。至少包含：

- open/close；
- create/get/replace/scan tasks；
- atomic create tool batch intents；
- atomic settle operation + insert outbox；
- scan effect-pending operations；
- scan pending outbox，按 `batch_id, source_index` 稳定排序；
- mark outbox published + operation completed；
- first-writer-wins memo get-or-set；
- request abort / terminalize task；
- transaction helper仅供 coordinator 内部使用。

不要让业务层拼 SQL，也不要把 sqlite connection 暴露出去。

### 8.2 MemoryStorage

内存实现是语义参考，不只是测试假对象：

- 所有写入和读取都 deep-copy；
- 一个 atomic 操作失败时不得部分更新；
- ID 不跨 record type 冲突；
- scan 顺序稳定；
- close 后所有操作拒绝；
- 用 `asyncio.Lock` 串行化 mutation。

### 8.3 SQLiteStorage

使用 stdlib `sqlite3`，建议：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=FULL;
```

要求：

- `PRAGMA user_version` 管理 schema version；
- 每个业务 commit 使用 `BEGIN IMMEDIATE` / commit / rollback；
- 所有 JSON 存 canonical UTF-8 text；
- connection 不跨并发操作裸用；可用 `asyncio.Lock` 加 `asyncio.to_thread()` 包住整个 transaction；
- 绝不能把一个 transaction 拆到多个 `to_thread()` 调用；
- unique/foreign-key/check constraint 覆盖 record 不变量；
- SQLite 抛出“commit 结果不确定”的 I/O 错误后，storage 进入 poisoned 状态，本实例后续 mutation 必须拒绝并要求 reopen；
- close 等待已经 admitted 的 transaction 完成；
- 测试中必须真实 close/reopen 文件验证恢复，不能只测同一个 connection。

建议表：

```text
durable_meta
tasks
task_memos
tool_operations
outbox
```

schema 细节由实现者决定，但必须支持上述 contract 和索引：

- tasks `(status, kind)`；
- operations `(status, batch_id, source_index)`；
- outbox `(status, batch_id, source_index)`；
- unique `(operation_id)` 与 `durable_message_id`。

## 9. Session 与 outbox 投递

### 9.1 公开序列化

把 `session.py` 中 message JSON 转换整理为公开、可测试的函数，例如：

```python
agent_message_to_dict(message) -> dict[str, JsonValue]
agent_message_from_dict(data) -> AgentMessage
```

outbox、Session 和测试共用同一实现，避免出现两个不兼容的 rich-content schema。

### 9.2 Transcript 去重

每个 durable tool result 的 metadata 必须包含：

```python
{
    "durable_message_id": "...",
    "durable_operation_id": "...",
    "durable_batch_id": "...",
    "durable_source_index": 0,
    "durable_recovery": False,
}
```

给 `SessionTree` 增加按 `durable_message_id` 的索引和 idempotent append：

- 已存在相同 id 且 payload 相同：返回已有 entry；
- 已存在相同 id 但 payload 不同：视为持久化冲突并报错；
- 普通非 durable message 保持原行为；
- load 时重建该索引并检测冲突。

### 9.3 Session 文件写入

当前 `save_jsonl()` 直接覆盖目标文件，不能作为 durable ack 的依据。改为原子替换：

1. 在同目录创建临时文件；
2. 写完整内容；
3. flush + `os.fsync(file)`；
4. `os.replace(temp, target)`；
5. POSIX 下尽力 fsync 父目录；
6. 失败时清理临时文件，旧文件保持有效。

不要把目标文件删掉后再 rename，也不要使用跨目录临时文件。

### 9.4 投递与 ack

正常路径：

1. coordinator 在 operation outcome transaction 中写 pending outbox；
2. `ToolRuntime._commit()` 生成带 durable ids 的 message；
3. awaited `message_end` subscriber append Session；
4. durable mode 下 subscriber 立即 atomic-save Session；
5. emit 返回后 coordinator 将 outbox 标记 published、operation 标记 completed。

恢复路径：

1. 读取 pending outbox；
2. 按 batch/source order 生成 message；
3. idempotent append 到 Session；
4. atomic-save；
5. ack；
6. 如果 crash 发生在第 4 与第 5 步之间，下一次恢复通过 durable_message_id 发现已存在，只做 ack，不重复追加。

如果没有 `session_file`，不得把 outbox ack 成 published。durable Coding Agent 构造时应要求明确的 session 文件和 durable DB 路径。

## 10. Tool API 与 Coordinator

### 10.1 Tool 定义

扩展 `Tool`：

```python
@dataclass(slots=True)
class Tool(Generic[ArgsT]):
    ...
    replay_policy: ReplayPolicy = "unsafe"
```

扩展 extension tool 定义与 wrapper，使 extension 注册的工具也能声明 policy；默认同样是 unsafe。

### 10.2 ToolExecutionContext

新增只读字段/方法：

```python
operation_id: str | None
run_id: str | None
batch_id: str | None
attempt: int
is_recovery: bool

async def get_memo(self, name: str) -> JsonValue | None: ...
async def memo(self, name: str, candidate: JsonValue) -> JsonValue: ...
```

memo 是 operation-scoped、first-writer-wins、小型 JSON value。非 durable mode 下：

- `operation_id/run_id/batch_id` 为 `None`；
- `attempt=1`、`is_recovery=False`；
- 调用 durable memo API 应明确报 `RuntimeError`，不能假装已经持久化。

### 10.3 Coordinator seam

不要把 SQL 逻辑直接写进 `ToolRuntime`。定义内部 coordinator Protocol，至少包含：

```python
prepare_batch_intents(...prepared final calls...) -> handles
settle_operation(handle, final_result, is_error) -> durable message metadata
acknowledge_published(operation_id)
```

`ToolRuntime` 没有 coordinator 时完全走当前路径。

有 coordinator 时：

- 所有 preflight 仍按 source order；
- 一批真正准备执行的 calls 的 intent 尽量在一个 transaction 中写入；
- intent commit 完成前一个 effect 都不能启动；
- parallel execute 仍可并行；
- 每个 outcome 单独可靠提交；
- transcript message 仍按模型原始 call order产生；
- immediate failure（工具不存在、参数不合法、before hook block）没有跨过 effect boundary，可以直接产生普通 result；若为了批次恢复一致性选择 journal，也必须标成 outcome-ready，绝不能写 effect-pending；
- `tool_execution_start/end` 的现有语义保持；
- late progress guard 保持；
- terminate 计算保持当前行为，不因 durability 改写。

### 10.4 Coding Agent 策略

内置工具必须显式声明：

| Tool | v1 policy | 原因 |
|---|---|---|
| `read_file` | `safe` | 只读；恢复时内容可能变化，但重复执行不产生副作用 |
| `grep` | `safe` | 只读搜索 |
| `write_file` | `safe`，但必须先改成原子文件替换 | 相同完整内容重复写入可安全收敛 |
| `edit` | `unsafe` | 崩溃后不知道 patch 是否已应用；重放可能失败或改变语义 |
| `bash` | `unsafe` | 任意命令可能产生不可逆或重复副作用 |

`write_file` 的原子替换要求：同目录 temp file、写入、flush/fsync、`os.replace`，失败时不留下半文件。不要把“Python 的一次 `write_bytes` 调用”称为文件系统原子操作。

## 11. Run/task 生命周期

### 11.1 run identity

durable Coding Agent 每次 `stream/run` 创建一个 `agent_run` task：

```text
pending -> running -> terminal
```

checkpoint 至少包含：

```json
{
  "phase": "agent_loop",
  "session_leaf_id": "...",
  "started_message_count": 12
}
```

`agent_start` 前 task 必须已经 durable；`agent_end` awaited subscribers 全部完成后再写 terminal outcome。

映射：

- 正常结束 -> `completed`；
- 用户 abort -> `aborted`；
- runtime/provider 无法恢复的错误 -> `failed` 或 `faulted`；
- 未知 task version/kind -> `orphaned`。

不要把 provider 的普通 model-visible error 与 storage contract fault 混为一种错误。

### 11.2 reopen

打开 durable runtime 时：

1. 将遗留 running task 重置为 pending；
2. 先恢复/中断 effect-pending tool operations；
3. 投递所有 outcome-ready outbox；
4. 根据 transcript tail 判断 incomplete run 是否需要继续；
5. 返回 `RecoveryReport`，不要在 `create_coding_agent()` 内悄悄启动一个无人持有的后台 run。

`RecoveryReport` 至少包含：

```python
replayed_operations: list[str]
interrupted_operations: list[str]
published_messages: list[str]
pending_run_ids: list[str]
warnings: list[str]
```

v1 的自动行为仅到“修复 operation/outbox/transcript 并报告 pending run”为止。由调用方显式调用类似 `resume_recovered_run(run_id)` 继续模型循环。这样避免在构造函数返回前隐式消耗 provider token。

### 11.3 abort

run task 需要 durable `abort_requested`：

```text
commit abort_requested
→ signal current in-memory run
→ join run
→ write terminal aborted outcome
```

v1 不需要完整 generic abort scheduler，但至少保证：

- storage 中看到 abort mark 后，新的 tool effect 不得入场；
- 已经 effect-pending 的操作按正常 recovery 规则处理；
- close 本身不应伪造 abort mark；
- close 等待已进入 storage 的 commits 完成，然后关闭；
- closed runtime 的新 mutation 拒绝。

## 12. Crash recovery 规则

逐条实现，不能用“重新跑整个 agent”替代。

### 12.1 `effect_pending`

恢复器查找当前注册工具：

1. 缺失工具 -> interrupted result；
2. stored/current 任一 unsafe -> interrupted result；
3. 当前 schema 不再接受 stored args -> interrupted result；
4. 两边 safe -> 使用 stored final args replay；
5. replay success/failure 都写 outcome + outbox；
6. replay 过程中再次 crash，仍保持 effect_pending，下一次可再次安全 replay。

interrupted result 必须 model-visible、结构化、不可含糊：

```python
ToolResult(
    content=(
        "Tool execution was interrupted after its durable intent was recorded. "
        "The runtime did not replay this unsafe operation because its external "
        "side effect may already have occurred. Inspect the environment before retrying."
    ),
    details={
        "stage": "durable_recovery",
        "code": "unsafe_effect_interrupted",
        "operation_id": operation.id,
        "tool_name": operation.tool_name,
        "effect_may_have_occurred": True,
    },
)
```

不要写成“tool failed”而丢失 uncertain 语义，也不要自动再次调用 unsafe tool。

### 12.2 `outcome_ready`

effect 已经有 durable outcome，不再执行 tool。只投递/补写 transcript，然后 ack。

### 12.3 `completed`

不执行、不投递；若 transcript 反而缺失该 durable message，应报告 storage/session inconsistency，而不是静默生成第二个结果。

### 12.4 批次顺序

parallel tool 的 effect completion 可以乱序，但 transcript tool-result message 必须按 `source_index`。恢复时若前几条已发布，只补缺失项，仍保持剩余项相对顺序。

## 13. Failpoints 与测试方法

`failpoints.py` 提供仅测试使用的注入 seam，不能依赖环境变量或真实 `os._exit()`：

```python
class Failpoint(Protocol):
    async def hit(self, name: str) -> None: ...
```

生产默认 no-op。至少覆盖这些点：

```text
after_run_task_created
after_assistant_tool_call_persisted
after_effect_intent_committed
after_effect_returned_before_outcome_commit
after_outcome_and_outbox_committed
after_session_saved_before_outbox_ack
```

测试通过抛出专用 `InjectedCrash` 模拟进程消失；随后 close/reopen 新 storage/runtime 实例，不得复用旧内存对象。

## 14. 分批实施顺序

### Batch 1 — Provider policy

- 新增 policy types、validation、merge；
- 扩展 adapter 调用 seam，兼容旧 adapter；
- OpenAI-compatible 动态 auth、headers、timeout、retry、hooks；
- 完成 provider tests。

验收：现有 model/agent tests 不回归；retry cancellation 与 retry-after cap 有测试。

### Batch 2 — Records 与 storage

- 新增 records/serialization/MemoryStorage；
- 新增 SQLite schema、transaction、reopen、poisoning；
- 完成 storage conformance tests。

验收：同一套行为测试对 memory/sqlite 两个 backend 运行。

### Batch 3 — Session durability 与 outbox

- 公开 message serializer；
- durable message 去重；
- atomic `save_jsonl()`；
- outbox publish/ack；
- 完成两个 crash window 测试。

验收：outcome→session 与 session→ack 两个窗口都不丢、不重复。

### Batch 4 — Durable tool coordinator

- `Tool.replay_policy`；
- context stable IDs/memos；
- batch intent；
- outcome/outbox；
- 正常执行与 parallel order；
- extension tool policy 透传。

验收：未启用 durable 时旧路径完全不变；启用时 effect 前必有 intent。

### Batch 5 — Recovery 与 tasks

- run task lifecycle；
- reopen reconciliation；
- safe replay / unsafe interruption；
- recovery report；
- abort/close gates；
- 全部 failpoint tests。

验收：每个 crash point 都用新 runtime 实例恢复。

### Batch 6 — Coding Agent 集成

- `CodingAgentOptions` 增加清晰的 durable 配置对象，而不是散落多个 bool/path；
- 强制 durable DB 与 session file 成对配置；
- 标注五个内置工具的 policy；
- `write_file` 原子替换；
- README/architecture 文档；
- 导出公开 API。

建议配置：

```python
@dataclass(slots=True)
class DurableRuntimeOptions:
    database_path: str | Path
    session_file: str | Path
    session_id: str | None = None
```

不要让默认 `create_coding_agent()` 自动创建隐藏的 durable 文件。

## 15. 测试矩阵

### 15.1 Storage

- atomic batch rollback；
- detached read/write；
- task full replacement；
- terminal record 不含 checkpoint/memos；
- first-writer-wins memo；
- SQLite close/reopen；
- corrupted JSON/schema version 明确失败；
- poisoned storage 拒绝后续 mutation；
- scan/filter/order 稳定。

### 15.2 Provider policy

- old adapter compatibility；
- per-request options snapshot；
- dynamic API key 每 attempt 重新解析；
- header merge 且 Authorization 不泄露/不被覆盖；
- retryable/non-retryable status；
- Retry-After 秒、HTTP date 与 retry-after-ms；
- max delay cap；
- cancel during request；
- cancel during backoff；
- mid-stream failure 不自动 retry；
- unsupported websocket 明确失败。

### 15.3 Tool durability

- intent 一定早于 handler 调用；
- handler 未运行时不会伪造 effect outcome；
- stored/current safe 双重门；
- missing tool/schema drift -> interrupted；
- recovery 不重跑 before hook；
- recovery 会跑 after hook；
- operation ID 与 tool call ID 稳定；
- attempt 增加；
- parallel effect 完成乱序、transcript 顺序稳定；
- late progress 仍被忽略；
- unsafe interrupted result 含 `effect_may_have_occurred=True`；
- `bash` 永不自动 replay。

### 15.4 Outbox/session

- outcome commit 后 crash、恢复补写一次；
- session save 后 ack 前 crash、恢复不重复；
- 同 durable id 相同 payload 幂等；
- 同 durable id 不同 payload 报冲突；
- atomic save 失败保留旧文件；
- rich text/image/details/usage round trip；
- old Session v1/v2/v3 仍加载。

### 15.5 Coding tools

- replay policy 与本规格表一致；
- `write_file` 使用同目录 temp + replace；
- write crash 不留下部分目标；
- repeated write 收敛到同一 bytes；
- `edit` / `bash` 为 unsafe。

## 16. 运行与质量要求

实施前先安装开发依赖，然后每个 Batch 后运行：

```bash
python -m pip install -e ".[dev]"
pytest -q
```

再执行：

```bash
python -m compileall -q src tests
```

要求：

- 不删除现有测试来获得通过；
- 不用 `sleep()` 制造时序测试，使用 events/failpoints；
- 不访问真实 provider；HTTP retry 使用 `httpx.MockTransport` 或等价 fake；
- 不执行真实危险 shell；
- 不依赖测试执行顺序；
- 临时 SQLite/Session 文件使用 pytest `tmp_path`；
- 所有 recovery test 必须 close/reopen；
- README 明确说明 durable mode 的保证与不保证。

## 17. 最终验收标准

实现只有在全部满足时才算完成：

- [ ] 默认非 durable 行为与现有测试兼容；
- [ ] provider request options 可按 request 快照传递；
- [ ] retry 可取消、有上限、不会在已开始 stream 后盲目重放；
- [ ] run/task 有 durable identity 与 terminal receipt；
- [ ] 每个真实 tool effect 前已经提交最终 call intent；
- [ ] replay 默认 unsafe；
- [ ] 仅 stored/current 都 safe 时自动 replay；
- [ ] unsafe uncertain effect 生成明确 interrupted result；
- [ ] recovery 不重跑 before hook；
- [ ] outcome 与 outbox 同 transaction；
- [ ] Session 写入与 outbox ack 之间的两个 crash window 都可恢复；
- [ ] parallel tool result transcript 顺序不变；
- [ ] `read_file/grep/write_file/edit/bash` policy 正确；
- [ ] `write_file` 改为原子替换；
- [ ] Memory/SQLite 通过同一组 conformance tests；
- [ ] 所有新旧 tests 通过；
- [ ] 文档明确 v1 不是 exactly-once，也不是完整 Pico5。

## 18. Codex 最终交付格式

完成代码后，Codex 的最终回复必须包含：

1. 实际修改的模块；
2. durable 保证与明确不保证；
3. replay policy 表；
4. migration/backward compatibility 说明；
5. 运行过的测试命令和结果；
6. 如果未完成任何 Batch，明确列出未完成项和阻塞原因，不能用“基础已经搭好”代替。

不要要求调用方阅读 Pi 源码来理解或验收实现；本文已经包含本轮所需的全部行为语义。
