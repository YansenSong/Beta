# Chapter 11：Extension Composition

Chapter 10 建立 Extension Runtime 后，这一章用三种更“像产品功能”的能力验证一个问题：它们是否仍然可以只由 primitive 组合出来，而无需修改 Agent Loop？

答案是可以。Chapter 12 之后，这三个能力已经作为 Coding Agent 的正式产品级 Extension 收口到 `src/coding_agent/extensions/`，不再保留一份 `examples/extensions/` 的重复实现。

## Permission Gate

Permission Gate 只监听 `tool_call`。例如 bash 命中危险模式时返回 block：

```text
assistant tool_call
      ↓
permission extension
      ↓ block
before_tool_call
      ↓
ToolRuntime
      ↓
error ToolResult
      ↓
model
```

bash Tool 本身不需要知道权限策略。

实现：`src/coding_agent/extensions/permission_gate.py`。

## Plan Mode

Plan Mode 是一个持续状态，但仍然只组合三条 seam：

```text
enabled
 ├─ set_active_tools   禁用 write/edit/delete
 ├─ tool_call          bash 只允许只读命令
 └─ context            注入 Plan Mode 提示
```

进入模式时保存“当时真实的 active tools”，退出时恢复这份快照，而不是恢复一套写死的默认 Tool。这样不会覆盖其他 Extension 已经做出的配置变化。

Coding Agent 中通过 `/plan` 显式切换，默认关闭。实现：`src/coding_agent/extensions/plan_mode.py`。

## Subagent

Parent Agent 不增加 child-agent 分支。Subagent 只是一个普通 Extension Tool：

```text
Parent
  ↓ tool_call: subagent
ToolRuntime
  ↓
Child Agent + Child Session
  ↓
ToolResult(final child answer)
  ↓
Parent continues
```

Child 的消息、Tool Call 和 Session 都留在 Child 内部；Parent history 只看到一次 Tool Call 和一次 Tool Result。

因为它仍然是 Tool，如果同一轮产生多个 subagent call，现有并行 Tool Runtime 就能并发执行，不需要额外 Subagent Scheduler。

Coding Agent 通过 `CodingAgentOptions.child_model_factory` 提供子模型 factory，并且只有显式加载 `subagent_extension` 后才会注册这个 Tool。实现：`src/coding_agent/extensions/subagent.py`。

## 这一章真正验证了什么

Core 仍然没有：

```text
permission_mode
plan_mode
subagent_branch
```

这些能力分别落回：

```text
Permission -> Tool interception
Plan Mode  -> active tools + context + Tool interception
Subagent   -> Tool registration + existing Tool Runtime
```

这说明 00～10 章留下的 seam 已经能够表达更大的 Agent 行为，同时保持 Agent Loop 的形状稳定。

## 对应测试

`tests/test_extension_composition.py` 会验证：危险命令经 Core ToolRuntime 变成 error ToolResult、Plan Mode 的 Tool 切换/Context/恢复语义，以及 Subagent 的父子上下文隔离。

`tests/test_coding_assembly.py` 进一步验证 Plan Mode 能在正式 Coding Agent facade 中激活 `subagent`，且 `child_model_factory` 能通过产品组装层传递给 Subagent Extension。

## 三个 Extension 的关键源码

Permission Gate 只订阅 `tool_call`。它先限定 `event.tool_call.name == "bash"`，再从已校验的 `event.args.command` 匹配危险模式，返回 `ToolCallDecision(block=True, reason=...)`。这个 decision 会被 bridge 翻译成 Core 的 `BeforeToolCallDecision`，最终成为 error Tool Result。

Plan Mode 在一个 closure 中保存 `enabled` 和 `tools_before`。`/plan` 开启时过滤 `write_file`、`edit` 等 mutating tools，并请求启用 `bash`、`subagent`；关闭时恢复原 snapshot。它还同时注册两个 handler：

```python
api.on("tool_call", guard)       # 拦截非只读 bash
api.on("context", inject_context) # 临时追加 PLAN_MODE_PROMPT
```

所以工具集合是 Runtime 状态，planning prompt 只是每次模型调用前的 view，不会污染 Session。

Subagent 则注册一个标准 `ExtensionTool`。handler 从 `ctx.config.services["child_model_factory"]` 取模型，创建全新的 `SessionTree` 和 `Agent`，执行 `child.run(args.task)`，最后只把末条 Assistant 文本放进父级 `ToolResult`。父级从未拿到 child 的 context 对象，这就是隔离并非文档约定、而是对象所有权上的事实。
