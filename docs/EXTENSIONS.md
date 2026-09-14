# Extension Runtime 使用与设计指南

本文档说明 Beta 的 Extension Runtime：如何注册 Tool / Command / Event Handler，以及 Coding Agent 中 Permission Gate、Plan Mode、Subagent 为什么都能在不修改 Agent Core 的情况下实现。

如果只是按教程理解演进过程，请先看 [`tutorials/10-extension-runtime.md`](tutorials/10-extension-runtime.md) 和 [`tutorials/11-extension-composition.md`](tutorials/11-extension-composition.md)。

## 1. Extension 的角色

Extension 不是第二套 Agent Runtime。它只负责把外部行为注册到已有 seam：

```text
Extension factory
    ↓
register Tool / Command / Handler
    ↓
ExtensionRunner
    ↓
AgentConfig hooks + EventStream + active tools
```

Agent Loop、ToolRuntime、Message protocol 都仍然只有一套。

通用 Extension Runtime 位于：

```text
src/beta_agent/extensions/
├── types.py
├── runner.py
├── wrapper.py
├── loader.py
└── bridge.py
```

Coding Agent 的正式产品级 Extension 位于：

```text
src/coding_agent/extensions/
├── permission_gate.py
├── plan_mode.py
└── subagent.py
```

`examples/` 不再维护一套重复的 Extension 源码。

## 2. 最小 Extension

一个 Extension 本质上是接收 `ExtensionAPI` 的 factory：

```python
from beta_agent.extensions import ExtensionAPI


def extension(pi: ExtensionAPI) -> None:
    async def on_message_end(event, ctx):
        print(event.message.role, event.message.content)

    pi.on("message_end", on_message_end)
```

factory 在加载时执行一次，真正长期存在的是注册结果。

## 3. Runner 与 Host

```python
from beta_agent import Agent, SessionTree
from beta_agent.extensions import ExtensionRunner, RuntimeConfig, bind_extensions

session = SessionTree()
runner = ExtensionRunner(
    cwd=".",
    config=RuntimeConfig(model="deepseek-chat"),
    session=session,
)
await runner.load([extension_a, extension_b])

agent = Agent(model=model, tools=base_tools)
host = bind_extensions(agent, runner)
messages = await host.run("你好")
```

职责边界：

- `Agent`：稳定 Agent Loop；
- `ExtensionRunner`：registration、dispatch、错误隔离；
- `ExtensionHost`：把 Runner 接到现有 Core seam；
- `SessionTree`：为 Extension 提供当前会话上下文。

## 4. 原子加载

每个 factory 先写入临时 registration：

```text
factory
  ↓
PendingRegistrations
  ↓
成功？
├── yes → commit all
└── no  → discard all
```

因此 factory 中途抛错不会留下“半个 Extension”。错误记录在 `runner.errors`，单个 Extension 失败也不会阻止其他 Extension 加载。

## 5. 三种事件组合语义

当前主要事件：

```text
tool_call
context
message_end
turn_end
```

### Observe：`message_end` / `turn_end`

按注册顺序执行。单个 handler 失败会被记录，但不会阻断后续 handler。

### Intercept：`tool_call`

handler 可以返回 `ToolCallDecision(block=True, ...)`。第一个 block 会立即短路，最终仍由 Core ToolRuntime 生成模型可见 error ToolResult。

### Transform：`context`

顺序 pipeline：

```text
messages0
↓ A
messages1
↓ B
messages2
↓ Model
```

后一个 handler 总是看到前一个 handler 的输出。

## 6. Extension Tool

Extension Tool 比普通 Tool 多一个 `ExtensionContext`：

```python
from pydantic import BaseModel
from beta_agent import ToolResult
from beta_agent.extensions import ExtensionAPI, ExtensionTool


class EchoArgs(BaseModel):
    text: str


def extension(pi: ExtensionAPI) -> None:
    async def echo(args, ctx, tool_ctx):
        return ToolResult(content=f"{ctx.cwd}: {args.text}")

    pi.register_tool(
        ExtensionTool(
            name="echo",
            description="回显文本",
            args_model=EchoArgs,
            handler=echo,
        )
    )
```

Runner 会把它包装成普通 Core `Tool`，因此继续复用：

```text
lookup
→ prepare_arguments
→ validate
→ before_tool_call
→ execute
→ after_tool_call
→ Tool Result
```

不会出现第二套 Tool protocol。

## 7. Active Tools

Extension 不直接改 `agent.context.tools`，而是调用：

```python
ctx.get_active_tools()
ctx.set_active_tools(names)
```

bridge 负责：

```text
Tool names
↓ resolve
Tool objects
↓ apply
running Agent
```

所以产品模式可以动态改变下一次模型调用可见的 Tool，而不重建 Agent。

## 8. Command

Command 属于用户 / Harness 层，不是模型 Tool Call：

```python
def extension(pi: ExtensionAPI) -> None:
    async def status(args, ctx):
        print(ctx.get_active_tools())

    pi.register_command("status", description="显示工具状态", handler=status)
```

调用：

```python
await host.run_command("/status")
```

## 9. Session 与 Extension 状态

`ExtensionContext.session` 指向当前 `SessionTree`。Extension 可以维护运行期闭包状态，也可以通过 `ctx.append_entry(...)` 追加自定义 Session Entry。

不要为了一个 Extension：

- 修改 Agent message protocol；
- monkey-patch `Agent._run()`；
- 自建第二套隐藏 Tool Runtime；
- 让产品模式反向进入 Core。

## 10. 动态加载目录

通用 Loader 仍支持：

```python
factories = load_extensions_from_dir("some/extensions")
await runner.load(factories)
```

目录中的 Python 文件只需要暴露：

```python
def extension(pi):
    ...
```

当前没有实现 pip entry-point discovery、hot reload、Extension API version negotiation 或第三方包权限模型。

## 11. Coding Agent 的三个正式 Extension

Chapter 11 的组合案例现在已经产品化到 `coding_agent.extensions`，不再放在 `examples/extensions/`。

### Permission Gate

```python
from coding_agent.extensions import permission_gate_extension
```

实现位置：

```text
src/coding_agent/extensions/permission_gate.py
```

机制：

```text
tool_call
→ inspect bash command
→ block / allow
```

它只是策略示范，不等同于 OS sandbox 或完整 trust model。

### Plan Mode

```python
from coding_agent.extensions import plan_mode_extension
```

实现位置：

```text
src/coding_agent/extensions/plan_mode.py
```

默认关闭，通过 `/plan` 显式切换。一份闭包状态同时驱动：

```text
active tools
context injection
bash tool_call policy
```

进入时保存实际 active tools，移除 `write_file` / `edit` 等 mutation Tool，并在可用时加入 `subagent`；退出时恢复进入前的真实快照。

### Subagent

```python
from coding_agent.extensions import subagent_extension
```

实现位置：

```text
src/coding_agent/extensions/subagent.py
```

Parent 只看到一个普通 Tool：

```text
Parent Agent
→ subagent Tool
→ Child Agent + Child Session
→ ToolResult
→ Parent continues
```

Child history 不直接进入 Parent history。

通过 `CodingAgentRuntime` 使用时，需要提供子模型 factory：

```python
runtime = await create_coding_agent(
    CodingAgentOptions(
        cwd=".",
        model=parent_model,
        extensions=[plan_mode_extension, subagent_extension],
        child_model_factory=lambda: make_child_model(),
    )
)
```

`subagent_extension` 不会因为文件存在就自动启用；必须显式放进 `extensions`。当前 Plan Mode 会在运行期把已注册的 `subagent` 加入 active tools。

## 12. 编写 Extension 时的边界清单

优先使用：

```text
增加模型能力      → register_tool
用户命令          → register_command
观察生命周期      → message_end / turn_end
阻止 Tool Call    → tool_call
临时 Context 修改 → context
动态 Tool 切换    → set_active_tools
长期状态          → Session / custom entry
```

避免：

```text
Extension 直接持有并修改 Agent 私有状态
Extension 直接修改 ToolRuntime
Extension monkey-patch Agent._run
Extension 自建第二套 Tool execution protocol
Extension 把产品模式硬编码进 Core
```

## 13. 对应源码和测试

Core Extension Runtime：

```text
src/beta_agent/extensions/types.py
src/beta_agent/extensions/runner.py
src/beta_agent/extensions/wrapper.py
src/beta_agent/extensions/loader.py
src/beta_agent/extensions/bridge.py
```

Coding Agent 产品 Extension：

```text
src/coding_agent/extensions/permission_gate.py
src/coding_agent/extensions/plan_mode.py
src/coding_agent/extensions/subagent.py
```

测试：

```text
tests/test_extension_runtime.py
tests/test_extension_composition.py
tests/test_coding_assembly.py
```

应持续保护这些 invariant：

```text
factory load 原子
handler failure 隔离
tool_call block 短路
context transform 顺序组成 pipeline
Extension Tool 继续走 Core ToolRuntime
active tools 对下一次模型调用立即生效
Parent / Child Agent history 隔离
Coding Agent product extension 不反向污染 beta_agent Core
```
