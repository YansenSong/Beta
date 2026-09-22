# Beta 第一轮对齐 pi-agent-core：Codex 执行交接（当前架构版）

## 0. 基线与执行原则

本文基于 Beta 默认分支提交：

```text
547acd9c378dd063177f7360b125dc22359b1892
feat: Implement session management, skill discovery, and tool execution framework
```

基线测试：`python3 -m pytest -q`，结果为 `102 passed`。

Codex 执行时先核对当前 HEAD。如果源码晚于上述基线，以最新源码为准，但须保持本文描述的层次边界。若某项能力已经实现，验证语义和测试即可，不得创建平行 API。

特别注意：当前架构已经删除旧版顶层 `tools/`、`session/`、`skills/`、`compaction/`、`durable/` 包。不要恢复这些目录。

本轮目标：

> 对相同的模型流、工具调用、队列消息、hook 决策和异常输入，Beta 产生确定且协议完整的 provider 请求序列、Agent 事件序列、transcript 和最终状态。

## 1. 当前真实架构

依赖方向：

```text
coding_agent（产品层）
    ↓
beta_agent.harness（组合能力与持久化运行时）
    ↓
beta_agent core / providers / runtime
```

必须保持 `coding_agent -> beta_agent` 单向依赖。

### Core

```text
src/beta_agent/
├── __init__.py
├── agent.py
├── agent_loop.py
├── messages.py
├── types.py
├── runtime/
│   ├── cancellation.py
│   ├── errors.py
│   ├── events.py
│   └── transcript.py
├── providers/
│   ├── messages.py
│   ├── model.py
│   └── policy.py
└── adapters/openai_compatible.py
```

- `agent.py`：Agent facade、配置、消息队列、订阅和 run 入口。
- `agent_loop.py`：turn 调度、模型流、工具批次和终止收尾。
- `types.py` / `messages.py`：共享协议与消息模型。
- `runtime/*`：取消、错误、EventStream、transcript replay。
- `providers/*`：provider-neutral DTO、adapter protocol 和请求策略。

### Harness

```text
src/beta_agent/harness/
├── __init__.py
├── compation.py
├── session.py
├── skill.py
├── tool.py
└── durable/
    ├── types.py
    ├── memory.py
    ├── sqlite.py
    └── runtime/
        ├── checkpoint.py
        ├── drive.py
        ├── errors.py
        ├── failpoints.py
        ├── outbox.py
        ├── recovery.py
        └── tools.py
```

当前规范位置：

- `Tool`、`ToolRuntime`、tool hook、`ToolExecutionContext`：`harness/tool.py`
- Session：`harness/session.py`
- Skill：`harness/skill.py`
- Compaction：`harness/compation.py`
- Durable coordinator/recovery：`harness/durable/runtime/`

当前文件名确实是 `compation.py`。本轮不做拼写重命名，以免扩大兼容范围。

### Extension 与产品层

```text
src/beta_agent/extensions/
├── bridge.py
├── loader.py
├── runner.py
├── types.py
└── wrapper.py

src/coding_agent/
├── assembly.py
├── prompt.py
├── tools/
└── extensions/
    ├── permission_gate.py
    ├── plan_mode.py
    └── subagent.py
```

`extensions/bridge.py` 会组合 `AgentConfig.before_tool_call`；`coding_agent/assembly.py` 会把 `agent.config.after_tool_call` 传给 durable recovery。修改 hook 契约时必须同步适配这些调用链。

## 2. 本轮范围

按顺序完成：

1. `finish_turn` 三态调度。
2. `prepare_request` 每请求 hook。
3. 统一错误与中断生命周期。
4. 细粒度 streaming/event 协议。
5. 结构化 tool hook context 和完整 tool events。
6. `AgentState`、queue peek、active cancellation 和 reset。

明确不做：

- 不重构 `harness/session.py` 为完整 SessionRepo/Storage。
- 不扩展完整 compaction/branch-summary。
- 不迁移 pico3、scheduler 或 pi subagent runtime。
- 不重构 durable runtime 为 reducer/lane/drive 全套。
- 不实现 provider catalog 或 proxy stream。
- 不移动现有模块，不修正 `compation.py` 文件名。

## 3. 工作项一：`finish_turn`

主要文件：

```text
src/beta_agent/types.py
src/beta_agent/agent.py
src/beta_agent/agent_loop.py
src/beta_agent/__init__.py
tests/test_finish_turn.py（新增）
tests/test_agent_loop.py
tests/test_next_turn_update.py
tests/test_failure_normalization.py
```

在 `types.py` 增加：

```python
TurnAction = Literal["continue", "end"]

@dataclass(slots=True, frozen=True)
class TurnDecision:
    action: TurnAction
```

在 `AgentConfig` 增加 `finish_turn: FinishTurn | None`。Hook 接收现有 `TurnResult` 和可选 cancellation，同步或异步返回 `TurnDecision | None`。

正常时序必须是：

```text
assistant 写入 context
→ 全部 tool result 写入 context
→ 构造 TurnResult
→ 调用 finish_turn
→ 发出 turn_end
→ 应用调度决定
```

规则：

- `None`：自然调度。
- `end`：本轮 `turn_end` 后立即结束，不再拉取 steering/follow-up。
- `continue`：保证还有一次 provider request。
- 若 tool/steering/prepared message 已自然触发下一请求，不额外重复请求。
- 若原本将退出，发起一次 context-only request，不注入伪造消息。
- error/aborted 是硬退出，不能被 continue 恢复。
- hook 异常 stage 为 `finish_turn`，进入统一失败流程。

建议在 loop 局部维护 `explicit_continuation`。

保留现有 `should_stop_after_turn`：`True` 映射为 `end`。两者同时配置时，优先在初始化阶段抛出清晰 `ValueError`；若项目兼容要求不允许，则规定 `finish_turn` 优先并测试。

验收测试：continue 无 tool 时恰好多一次请求；有 tool/steering 时不重复；end 不读取 follow-up；None 保持自然行为；error/abort 忽略 continue；hook 在 `turn_end` 前；hook 异常只有一个 terminal lifecycle。

## 4. 工作项二：`prepare_request`

主要文件：

```text
src/beta_agent/types.py
src/beta_agent/agent.py
src/beta_agent/agent_loop.py
src/beta_agent/providers/policy.py
src/beta_agent/__init__.py
tests/test_prepare_request.py（新增）
```

在 `types.py` 增加（可用 `TYPE_CHECKING` 避免循环导入）：

```python
@dataclass(slots=True)
class PrepareRequestContext:
    context: AgentContext
    model: ModelAdapter
    request_options: ProviderRequestOptions

@dataclass(slots=True)
class RequestUpdate:
    context: AgentContext | None = None
    model: ModelAdapter | None = None
    request_options: ProviderRequestOptionsPatch | None = None
```

`AgentConfig` 增加 `prepare_request`。它不能返回 messages；正式 transcript message 仍由现有 `prepare_next_turn` 追加。

每次即将调用 `ModelAdapter.stream()` 前恰好调用一次，包括：首个 prompt、tool result 后、steering/follow-up 后、continue API 和 `finish_turn=continue` 的 context-only 请求。

调用前 queued/prepared messages 已加入 canonical context；调用后再执行 `transform_context`、transcript collapse、`convert_to_llm` 和 adapter stream。不要把它放进 adapter；adapter 内部 retry 不得重复触发。

Update 规则：

- context 更新 `Agent.context`。
- model 更新 `Agent.model`。
- options 使用现有 `merge_provider_request_options()`，合并 headers/metadata。
- hook 前后检查 cancellation。
- 异常 stage 为 `prepare_request`。

测试精确断言每个逻辑请求调用一次、首轮也调用、retry 不重复、可替换 context/model/options、headers/metadata 正确合并。

## 5. 工作项三：统一失败与中断生命周期

主要文件：

```text
src/beta_agent/agent_loop.py
src/beta_agent/runtime/errors.py
src/beta_agent/runtime/events.py
src/beta_agent/messages.py（如需正式错误字段）
tests/test_failure_normalization.py
tests/test_cancellation.py
tests/test_event_subscribers.py
```

必须满足：

1. 已开始的 run 恰好一个 `agent_end`。
2. 已开始的 turn 恰好一个 `turn_end`。
3. 每个 `message_start` 恰好一个匹配 `message_end`。
4. 每个 `tool_execution_start` 恰好一个匹配 `tool_execution_end`。
5. run failure/abort 形成 assistant failure message，进入 `Agent.context.messages` 和 `new_messages`。
6. failure assistant 不保留无结果 tool calls。
7. finalization 中 subscriber 再失败不会递归或阻止 `agent_end`。

标准失败序列：

```text
message_start(error/aborted assistant)
message_end(error/aborted assistant)
turn_end
agent_error            # error 时保留 Beta 扩展事件
agent_end
```

已有 partial assistant 时，规范化该对象并补齐生命周期，不创建第二条 assistant。最终消息至少有统一 `stop_reason`、`error_message`、`error_type`、`stage`，并清空 tool calls。

覆盖：steering/follow-up provider、prepare-next-turn、prepare-request、finish-turn、transform、converter、stream 创建/中途/无 final、extension bridge、subscriber、root cancellation、tool preparation/execution cancellation。普通 tool exception 仍转成 tool-result error。

保留 `_RunState.failed_subscribers` 防递归设计，并测试失败 subscriber 不参与错误收尾、其他 subscriber 的明确行为、terminal events 不重复。

## 6. 工作项四：细粒度 streaming/events

主要文件：

```text
src/beta_agent/types.py
src/beta_agent/providers/model.py
src/beta_agent/adapters/openai_compatible.py
src/beta_agent/agent_loop.py
src/beta_agent/runtime/events.py
src/beta_agent/__init__.py
tests/test_streaming_events.py（新增）
tests/test_agent_loop.py
tests/test_cancellation.py
```

扩展 `ModelEvent`：

```python
ModelEventType = Literal[
    "start",
    "text_start", "text_delta", "text_end",
    "thinking_start", "thinking_delta", "thinking_end",
    "toolcall_start", "toolcall_delta", "toolcall_end",
    "done", "error",
    "update",  # legacy adapter 兼容
]
```

事件应能表达当前完整 partial assistant、content index、delta、tool call ID/name、完成的 tool call 和错误信息。可以使用单 dataclass 可选字段或 discriminated union，以当前改动最小且类型清晰为准。

`AgentEvent(type="message_update")` 必须同时带当前 partial message 快照和原始 `ModelEvent`。消费者不能靠 diff 两条完整消息判断本次是 text/thinking/toolcall delta。

将 `AgentEvent.type: str` 收紧为事件名 `Literal`，但本轮不强制拆成多个事件类。

在 `OpenAICompatibleAdapter` 实现 start、text start/delta/end、toolcall start/delta/end、done/error。没有 thinking 数据时不伪造；`ScriptedModelAdapter` 应能测试 thinking。

大量现有测试构造 `ModelEvent("update", ...)`，保留 legacy update 过渡支持，不能一次破坏。

测试：delta 顺序、partial 快照、thinking/text 分离、toolcall 分片、message-update 保存原始事件、done/error 只结束一次、legacy update 仍工作、abort 无未闭合 message。

## 7. 工作项五：在 `harness/tool.py` 迁移 tool hook

主要文件：

```text
src/beta_agent/harness/tool.py
src/beta_agent/agent_loop.py
src/beta_agent/types.py
src/beta_agent/extensions/bridge.py
src/beta_agent/extensions/types.py
src/beta_agent/extensions/wrapper.py
src/beta_agent/harness/durable/runtime/recovery.py
src/beta_agent/harness/durable/runtime/tools.py
src/coding_agent/assembly.py
src/coding_agent/extensions/permission_gate.py
tests/test_tool_lifecycle.py
tests/test_parallel_tools.py
tests/test_extension_composition.py
tests/test_extension_runtime.py
tests/test_durable_runtime.py
tests/test_durable_harness_v2.py
tests/test_coding_assembly.py
```

不得新建第二个 ToolRuntime。

在 `harness/tool.py` 定义：

```python
@dataclass(slots=True)
class BeforeToolCallContext:
    assistant_message: AgentMessage
    tool_call: ToolCall
    args: BaseModel
    context: AgentContext

@dataclass(slots=True)
class AfterToolCallContext:
    assistant_message: AgentMessage
    tool_call: ToolCall
    args: BaseModel
    result: ToolResult
    is_error: bool
    context: AgentContext
```

从根 `beta_agent.__init__` 导出。`ToolRuntime.execute_batch()` 接收完整 assistant message，并以其 tool calls 为单一来源；如保留 calls 参数，必须检查一致性。

新 hook 规范：

```text
before_tool_call(context, cancellation=None)
after_tool_call(context, cancellation=None)
```

当前 bridge、测试和 durable recovery 依赖旧位置参数。使用 `inspect.signature` 在配置/注册阶段判断并集中包装。禁止捕获 `TypeError` 后换签名重试，因为 hook 内部真实 TypeError 会导致重复执行。

维持语义：lookup → prepare → Pydantic validate → before → execute → after；before 异常 fail closed；after 异常不导致 durable recovery 重放效果；patch 对 content/details/usage/terminate/is_error 逐字段替换，不深合并。

标准 tool events：

```text
tool_execution_start:  tool_call_id, tool_name, args
tool_execution_update: tool_call_id, tool_name, args, partial_result
tool_execution_end:    tool_call_id, tool_name, result, is_error
```

在 `AgentEvent` 增加明确的 `partial_result` 和 `is_error`。保留旧 result/error 时写明映射并测试。

验收：hook 看见完整 assistant 和同批 calls；before fail closed；after patch 全字段；progress 字段完整；late progress 忽略；parallel end 按完成顺序、result message 按 source order；extension/durable/coding assembly 不退化。

## 8. 工作项六：`AgentState`

主要文件：

```text
src/beta_agent/types.py
src/beta_agent/agent.py
src/beta_agent/agent_loop.py
src/beta_agent/runtime/transcript.py
src/beta_agent/__init__.py
tests/test_agent_state.py（新增）
tests/test_event_subscribers.py
tests/test_queue_modes.py
tests/test_transcript_state.py
```

这只是 Agent 运行状态，不要与 `harness/session.py` 的持久化 SessionTree 混合。

至少公开：

```python
@dataclass(slots=True)
class AgentState:
    model: ModelAdapter
    messages: list[AgentMessage]
    tools: list[Tool[Any]]
    is_streaming: bool
    streaming_message: AgentMessage | None
    pending_tool_calls: frozenset[str]
    error_message: str | None
```

可实现为 snapshot、只读 view 或内部 mutable state，但外部不得取得可绕过 Agent 的内部 list/set 引用。

归约规则：run 开始置 streaming 并清旧 error；assistant start/update 更新 streaming message；assistant end 清空；tool start/end 维护 pending IDs；error/aborted 设置 error；所有 awaited `agent_end` subscriber settle 后才进入 idle。

增加：

```python
agent.state
agent.active_cancellation       # 或按项目命名 cancellation_token
agent.peek_queued_messages()
agent.reset()
```

Queue peek 不消费队列，优先返回按当前 mode 选中的 steering，否则 follow-up。

Reset：运行中拒绝；通过 `runtime/transcript.py` helper 保留当前 system/tool baseline；清普通 conversation、两个队列、streaming/pending/error；保留 model/config/executable tools/subscribers；不得修改已经持久化的 `harness.session.SessionTree`。

测试 state 在 delta、parallel tools、error、idle 和 reset 下正确；`agent_end` subscriber 未 settle 时仍 running；reset 保留 baseline 且运行中拒绝。

## 9. 不得退化的当前行为

- system prompt/tool declaration transcript replay 与 diff。
- 空闲期工具变化在下次 prompt 前形成 system-state message。
- Runtime history 与单次 provider context view 分离。
- steering/follow-up 检查点及两种 queue mode。
- tool preflight 按 source order。
- parallel execute 并发，execution-end 按完成顺序，result message 按 source order。
- token-limit 截断 tool call 不执行。
- before hook fail closed，tool exception 转模型可见 result。
- progress settle 后拒绝 late update。
- awaited subscribers 属于 run settlement。
- ExtensionTool 仍走 `harness.tool.ToolRuntime`。
- durable operation/outbox/recovery/storage 测试通过。
- `coding_agent -> beta_agent` 单向依赖。

## 10. 标准事件序列

普通回复：

```text
agent_start
turn_start
message_start(user)
message_end(user)
prepare_request
message_start(assistant)
message_update(assistant, model_event)*
message_end(assistant)
finish_turn
turn_end
agent_end
```

Tool-driven 两轮：

```text
agent_start → turn_start → user lifecycle → prepare_request
→ assistant tool-call lifecycle
→ tool start/update/end
→ tool-result message lifecycle
→ finish_turn → turn_end
→ turn_start → prepare_request
→ final assistant lifecycle
→ finish_turn → turn_end → agent_end
```

Run failure：

```text
agent_start → turn_start → 已完成的合法事件
→ error assistant start/end → turn_end → agent_error → agent_end
```

测试应断言完整顺序和次数，terminal events 均只能出现一次。

## 11. 最低测试矩阵

1. 普通文本。
2. 单工具。
3. 并行工具乱序完成、顺序提交。
4. sequential 工具整批串行。
5. 未知工具和参数失败。
6. before block/异常。
7. after patch/异常。
8. 截断 tool call。
9. steering/follow-up 的两种 mode。
10. finish continue/end/None。
11. prepare request 替换 context/model/options。
12. transform/converter 异常。
13. stream 创建失败、中途失败、无 final。
14. streaming/tool execution 中取消。
15. subscriber failure。
16. tool loadout replay。
17. continue 合法性。
18. AgentState、queue peek、reset。
19. extension bridge 新 hook 适配。
20. durable recovery 新 after-hook 适配。

并发测试用 `asyncio.Event`、barrier 或 Future 控制，不依赖长时间 sleep。

每个场景尽量同时断言 model call count、provider input、Agent event order/fields、final context、returned messages、AgentState 和 error/status。

## 12. 质量要求与提交建议

- 基线 102 个测试及新增测试全部通过。
- 新公共类型加入正确 `__all__`。
- 不吞 `asyncio.CancelledError`。
- 不用捕获 TypeError 后重试探测签名。
- 公共集合返回防御性副本或只读 view。
- 错误 stage 稳定并进入 `AgentErrorInfo`。
- 不做无关格式化、模块移动或命名清理。
- 必要文档更新限于 `docs/ARCHITECTURE.md` 和 `docs/FRAMEWORK.md` 相关段落。

建议提交：

```text
1. agent: add finish-turn scheduling
2. agent: add typed prepare-request hook
3. runtime: normalize failure and abort lifecycle
4. providers: preserve typed assistant deltas
5. harness: migrate tool hooks to structured contexts
6. agent: expose runtime state, queue peek, and reset
7. tests: add core protocol conformance scenarios
```

## 13. 完成定义与最终交付

完成条件：六项全部实现或证明已等价；既有和新增测试全部通过；事件顺序、调用次数、失败 terminal lifecycle、tool hook context、AgentState 均有直接测试；extension、durable、coding_agent 不退化；没有重新引入旧目录或第二套 Tool/Session/Skill。

Codex 最终报告必须包含：

1. 实际 HEAD 和修改文件。
2. 六项逐项实现摘要。
3. 旧 API 兼容/弃用策略。
4. extension、durable、coding_agent 的适配点。
5. 新增测试场景。
6. 完整测试命令和结果。
7. 留到第二轮的事项。
8. 与本文不同的设计选择及理由。

不要只报告“已完成”或测试数量；结果必须足以复核协议、事件顺序和架构边界。
