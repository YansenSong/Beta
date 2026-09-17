# Chapter 10：Extension Runtime

前 00～09 章已经把 Agent Loop、Tool Runtime、Context Hook、Session、Compaction 和 Skill 分开。到了这一章，我们不再把新能力硬编码进 Core，而是允许外部 Python 模块在受控 API 上注册行为。

## 核心问题不是 import，而是边界

Extension 不应该拿到 `Agent`、`ToolRuntime` 或内部 registry。它只拿 `ExtensionAPI`：

```python
def extension(pi: ExtensionAPI) -> None:
    pi.register_tool(...)
    pi.register_command("todos", ...)
    pi.on("tool_call", ...)
    pi.on("context", ...)
```

factory 只在加载时执行一次。真正长期存在的是注册结果，由 `ExtensionRunner` 保存。

## 原子加载

每个 factory 先写入临时 registration。只有 factory 成功结束，才统一 commit；如果抛异常则整体 discard。这样不会留下“Tool 已注册但 Extension 加载失败”的半状态。

## 三类组合语义

- `message_end / turn_end`：按注册顺序通知，单个 handler 报错不会阻断后续 handler；
- `tool_call`：按顺序执行，一旦返回 `block=True` 立即短路；
- `context`：链式变换，后一个 handler 必须看到前一个 handler 的输出。

因此 Extension Runtime 不是一个简单 EventEmitter。

## 接回 Core

`bind_extensions()` 只把注册行为接入现有 seam：

```text
register_tool  -> Agent active tools -> ToolRuntime
context        -> transform_context -> ModelAdapter
tool_call      -> before_tool_call -> ToolRuntime
message_end    -> ExtensionHost event forwarding -> ExtensionRunner
```

Agent Loop 没有第二套“Extension Loop”。

## Active Tools

Extension 通过 `ctx.get_active_tools()` / `ctx.set_active_tools()` 修改下一次模型可见的 Tool。Runner 只知道 Tool 名字，真正的名字解析和运行中 Agent Tool 集合更新由 bridge 绑定。

这使 Extension 不需要依赖 Harness 内部怎样组织 Tool registry。

## Extension Tool wrapper

Extension Tool 在作者视角多拿一个 `ExtensionContext`，但注册后会被包装成普通 `Tool`，继续走已有的参数校验、并行执行、错误归一化和 Tool Result 提交流程。

如果 Tool 执行期间通过 `set_active_tools()` 新启用了 Tool，wrapper 会把新增名字写入 `ToolResult.added_tool_names`。

## 对应源码

- `src/beta_agent/extensions/types.py`
- `src/beta_agent/extensions/runner.py`
- `src/beta_agent/extensions/wrapper.py`
- `src/beta_agent/extensions/loader.py`
- `src/beta_agent/extensions/bridge.py`
- `tests/test_extension_runtime.py`

## 沿注册、绑定、运行读一遍

Extension factory 并不直接改 Agent。`ExtensionRunner.load()` 为每个 factory 创建独立的 `_PendingRegistrations`；只有 factory 完整执行成功，才把 handlers、tools、commands 合并到 Runner。抛异常时 pending 内容全部丢弃，这就是“原子加载”的具体实现。

绑定发生在 [`bind_extensions()`](../../src/beta_agent/extensions/bridge.py)。它先把 Core Tool 与 Extension Tool 建成 `universe`，再把已有 hook 与 Extension hook 串起来：

```python
async def before_tool_call(call, args, context, cancellation=None):
    if previous_before:
        decision = await call_with_optional_cancellation(...)
        if decision and decision.block:
            return decision
    verdict = await runner.emit_tool_call(...)
    if verdict and verdict.block:
        return BeforeToolCallDecision(...)
```

Context handler 则是管道语义：`ExtensionRunner.emit_context()` 把前一个 handler 的返回值作为后一个 handler 的输入；普通 lifecycle `emit()` 会依次调用全部 handler；`emit_tool_call()` 遇到第一个 block 立即返回。三类组合语义由三个不同方法直接表达，而不是藏在通用 EventEmitter 中。

Extension Tool 经 [`wrap_registered_tool()`](../../src/beta_agent/extensions/wrapper.py) 变成普通 `Tool`。wrapper 在调用前后比较 active tool names，并把新增项写进 `ToolResult.added_tool_names`；之后仍由 `ToolRuntime` 负责校验、执行和提交。
