# Extension Runtime 使用与设计指南

本文档说明 Beta 在 Chapter 10～11 中加入的 Extension Runtime：如何加载 Extension、如何注册 Tool / Command / Event Handler，以及 Permission Gate、Plan Mode、Subagent 为什么都能在不修改 Agent Core 的情况下实现。

如果只是想按教程理解演进过程，请先看 [`tutorials/10-extension-runtime.md`](tutorials/10-extension-runtime.md) 和 [`tutorials/11-extension-composition.md`](tutorials/11-extension-composition.md)。本文更偏向当前代码的使用说明和约束参考。

## 1. Extension 的角色

Extension 不是第二套 Agent Runtime。

它只做两件事：

```text
加载阶段
Extension factory
    ↓
注册 Tool / Command / Handler
    ↓
ExtensionRunner 保存 registration

运行阶段
已有 Agent seam
    ↓
ExtensionRunner 分发已注册行为
```

因此 Extension 不直接拿 `Agent`、`ToolRuntime` 或内部 registry，也不应该修改它们的私有状态。

当前公开入口位于：

```python
from beta_agent.extensions import (
    ExtensionAPI,
    ExtensionRunner,
    ExtensionTool,
    RuntimeConfig,
    ToolCallDecision,
    bind_extensions,
    load_extensions_from_dir,
)
```

## 2. 最小 Extension

一个 Python Extension 本质上是接收 `ExtensionAPI` 的 factory：

```python
from beta_agent.extensions import ExtensionAPI


def extension(pi: ExtensionAPI) -> None:
    async def on_message_end(event, ctx):
        print(event.message.role, event.message.content)

    pi.on("message_end", on_message_end)
```

factory 在加载时只执行一次。真正长期存在的是 `on_message_end` 这条 registration。

## 3. Runner 与 Host

运行时组装通常包含三部分：

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

这里：

- `Agent` 仍然负责原来的 Agent Loop；
- `ExtensionRunner` 保存注册信息和组合规则；
- `ExtensionHost` 把 Runner 接到现有 `AgentConfig` Hook 与 EventStream；
- `SessionTree` 为 Extension 提供可持久化的当前会话上下文。

`bind_extensions()` 不会创建第二套 Agent Loop。

## 4. 原子加载

每个 Extension factory 都先写入临时 registration：

```text
factory
  ↓
PendingRegistrations
  ↓
成功？
├── 是 → commit
└── 否 → discard
```

例如：

```python
def broken(pi):
    pi.register_tool(...)
    raise RuntimeError("boom")
```

`broken` 加载失败后，它前面注册的 Tool 也不会留在 Runner 中。

错误会记录到：

```python
runner.errors
```

单个 Extension 加载失败不会阻止其他 Extension 继续加载。

## 5. Event Handler 的三种组合语义

当前支持四类事件：

```text
tool_call
context
message_end
turn_end
```

它们并不是完全相同的 EventEmitter 语义。

### 5.1 Observe：`message_end` / `turn_end`

这类事件按注册顺序依次执行：

```text
A handler
   ↓
B handler
   ↓
C handler
```

单个 handler 抛异常时，Runner 记录错误并继续执行后面的 handler。

适合：

- 日志；
- 状态统计；
- Session 派生状态；
- Extension 自己的轻量观测逻辑。

### 5.2 Intercept：`tool_call`

`tool_call` 可以返回 `ToolCallDecision`：

```python
from beta_agent.extensions import ToolCallDecision

async def guard(event, ctx):
    if event.tool_call.name == "delete_file":
        return ToolCallDecision(
            block=True,
            reason="当前环境禁止删除文件",
        )
```

一旦某个 handler 返回 `block=True`，后面的 `tool_call` handler 不再执行。

最终 block 会回到原来的 `before_tool_call` seam，并由 `ToolRuntime` 规范化成模型可见的 error Tool Result。

### 5.3 Inject：`context`

`context` 是链式变换：

```text
messages0
   ↓ Extension A
messages1
   ↓ Extension B
messages2
   ↓ ModelAdapter
```

后一个 handler 必须看到前一个 handler 的结果。

例如：

```python
from beta_agent import Message

async def inject_mode(event, ctx):
    return [
        *event.messages,
        Message.user("[READ ONLY MODE] 只分析，不修改文件。"),
    ]
```

这只改变当前模型调用看到的 Context，不要求改写 Agent 的完整 Runtime history。

## 6. 注册 Extension Tool

Extension Tool 比普通 Tool 多一个 `ExtensionContext`：

```python
from pydantic import BaseModel
from beta_agent import ToolResult
from beta_agent.extensions import ExtensionAPI, ExtensionTool


class EchoArgs(BaseModel):
    text: str


async def echo(args, ctx, tool_ctx):
    return ToolResult(content=f"{ctx.cwd}: {args.text}")


def extension(pi: ExtensionAPI) -> None:
    pi.register_tool(
        ExtensionTool(
            name="echo",
            description="回显文本",
            args_model=EchoArgs,
            handler=echo,
        )
    )
```

注册以后，`wrapper.py` 会把它包装成普通 `Tool`。

之后继续走已有路径：

```text
lookup
→ prepare / validate
→ before_tool_call
→ execute
→ after_tool_call
→ Tool Result
```

因此 Extension Tool 自动继承原有 ToolRuntime 的参数校验、并行/串行语义、进度事件和失败归一化。

## 7. Active Tools

Extension 不直接修改 `agent.context.tools`。

它使用：

```python
ctx.get_active_tools()
ctx.set_active_tools([...])
```

`ExtensionRunner` 保存 Tool 名字，bridge 负责把这些名字解析成实际 `Tool`，并把结果应用到运行中的 Agent。

因此：

```text
Extension
   ↓ set_active_tools(names)
ExtensionRunner
   ↓ resolve
Tool objects
   ↓ apply
running Agent.context.tools
```

下一次 ModelAdapter 调用立即看到新的 Tool 集合。

如果某个 Extension Tool 在执行过程中新增了 active Tool，wrapper 会把新增 Tool 名写到：

```python
ToolResult.added_tool_names
```

## 8. 注册 Command

Command 属于 Harness / 用户交互层，而不是模型 Tool Call。

```python
def extension(pi: ExtensionAPI) -> None:
    async def status(args, ctx):
        print(ctx.get_active_tools())

    pi.register_command(
        "status",
        description="显示当前工具状态",
        handler=status,
    )
```

执行：

```python
await host.run_command("/status")
```

Runner 会自动去掉开头的 `/`，并把剩余文本作为参数交给 handler。

## 9. Session 与 Extension 状态

`ExtensionContext.session` 指向当前 `SessionTree`。

Extension 可以把持久状态设计成：

```text
运行期 module state
        +
Session append-only log
```

也可以通过：

```python
ctx.append_entry("my-extension-state", {"enabled": True})
```

追加 custom entry。

重要原则仍然是：不要为了某个 Extension 修改 Agent message protocol，也不要让 Extension 自己维护另一套隐藏 Session。

## 10. 动态加载目录

当前 Loader 有意保持简单：

```python
factories = load_extensions_from_dir("extensions")
await runner.load(factories)
```

目录中的 Python 文件只要暴露：

```python
def extension(pi):
    ...
```

就可以成为 Extension 入口。

当前阶段没有实现：

- pip entry point discovery；
- package dependency isolation；
- hot reload；
- Extension API version negotiation；
- 第三方包权限模型。

这些属于未来更外层的插件生态问题，不应该反向复杂化当前 Runner。

## 11. Chapter 11 的三个组合案例

### Permission Gate

位置：

```text
examples/extensions/permission_gate.py
```

机制：

```text
tool_call
→ 判断 bash 是否危险
→ block / allow
```

Agent Core 不认识 permission mode。

### Plan Mode

位置：

```text
examples/extensions/plan_mode.py
```

一份 `enabled` 状态同时驱动：

```text
active tools
context injection
bash tool_call policy
```

退出 Plan Mode 时恢复进入前真实的 active tools，而不是恢复某份写死的默认列表。

### Subagent

位置：

```text
examples/extensions/subagent.py
```

Parent 只看到一次普通 Tool Call：

```text
Parent Agent
→ subagent Tool
→ Child Agent + Child Session
→ ToolResult
→ Parent continues
```

Child 内部 message history 不会直接混进 Parent history。

## 12. 编写 Extension 时的边界清单

优先遵守下面这些规则：

```text
需要增加模型能力
→ register_tool

需要用户命令
→ register_command

需要观察生命周期
→ message_end / turn_end

需要阻止某次 Tool Call
→ tool_call

需要临时改变模型看到的 Context
→ context

需要切换模型可见 Tool
→ set_active_tools

需要长期状态
→ Session / custom entry
```

尽量避免：

```text
Extension 直接持有 Agent
Extension 直接修改 ToolRuntime
Extension monkey-patch Agent._run
Extension 自建第二套 Tool execution protocol
Extension 把产品模式硬编码进 Core
```

## 13. 对应源码和测试

核心实现：

```text
src/beta_agent/extensions/types.py
src/beta_agent/extensions/runner.py
src/beta_agent/extensions/wrapper.py
src/beta_agent/extensions/loader.py
src/beta_agent/extensions/bridge.py
```

测试：

```text
tests/test_extension_runtime.py
tests/test_extension_composition.py
```

当修改 Extension Runtime 时，应优先保证这些 invariant 继续成立：

```text
factory load 是原子的
handler failure 被隔离
tool_call block 会短路
context transform 是顺序 pipeline
Extension Tool 继续走 Core ToolRuntime
active tools 对下一次模型调用立即生效
Parent / Child Agent history 保持隔离
```
