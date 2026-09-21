# 01：Tool Result 如何把一次调用变成 Agent Loop

> 参考：[`learn-pi-agent` Chapter 01](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/01-tool-driven-agent)。

## 本章目标

理解 Tool-driven Agent 最核心的反馈回路，以及为什么 Tool 失败也应该成为模型可见的信息。

## 1. Tool Call 不是任务结束

模型第一次输出可能不是答案，而是：

```text
assistant
└── toolCall(calculator)
```

程序执行工具以后得到结果，但模型并不知道程序侧发生了什么。

所以 Tool Result 必须进入历史，再次调用模型：

```text
user
 ↓
assistant(tool call)
 ↓
tool result
 ↓
LLM
 ↓
assistant(final answer or another tool call)
```

只要下一轮仍然产生 Tool Call，这个过程就继续。

这就是 Agent Loop 最小的骨架。

## 2. History 是反馈通道

一次 Tool Call 至少要留下两类事实：

```text
assistant: 我请求执行 X

tool: X 的实际结果是 Y
```

下一次模型调用必须同时看到它们，才能知道哪个结果对应哪个请求。

Beta 的 [`../../src/beta_agent/agent.py`](../../src/beta_agent/agent.py) 会把 Assistant Message 和后续 Tool Result 都写入 `context.messages`。

因此 Agent 不需要预先知道任务会执行几步。

每一轮只做：

```text
调用模型
→ 观察 Tool Call
→ 执行 Tool
→ 写回 Tool Result
→ 再决定是否继续
```

## 3. Tool 失败为什么不能只抛异常

如果 Tool 不存在、参数有问题、网络超时，而 Runtime 直接整体崩溃，那么模型只会看到自己之前发出的 Tool Call。

它不知道失败原因，也失去了自我修正机会。

更有用的做法是把可恢复的 Tool failure 规范化为：

```text
Tool Result
├── content: 失败原因
└── is_error: true
```

这样模型下一轮可以：

- 修改参数；
- 换一个 Tool；
- 调整计划；
- 或把真实失败原因告诉用户。

Beta 的 [`../../src/beta_agent/harness/tool.py`](../../src/beta_agent/harness/tool.py) 正是沿这个方向设计。

## 4. Agent Loop 不应该变成 Workflow Engine

Tool-driven Agent 与固定 Workflow 的一个区别是：下一步由模型基于最新历史决定。

```text
固定 Workflow：A → B → C

Agent Loop：
LLM → Tool A → LLM → Tool C → LLM → final
```

Loop 只提供“反馈继续发生”的机制，不负责预先编排所有步骤。

## 5. 对照 Beta 阅读

重点看：

- `Agent._run()`：什么时候继续下一 turn；
- `assistant.tool_calls`：模型提出动作的位置；
- `ToolRuntime.execute_batch()`：Tool Result 从哪里回来；
- `self.context.messages.extend(tool_results)`：结果怎样进入下一次模型输入的历史来源。

### 把循环落实到 `_run_impl()`

主循环收到 Assistant 后，先执行整批 Tool，再把结果同时写入长期 context 和本次返回值：

```python
batch = await tool_runtime.execute_batch(
    context=self.context,
    calls=assistant.tool_calls,
    emit=emit,
    cancellation=cancellation,
)
tool_results = batch.messages
self.context.messages.extend(tool_results)
state.new_messages.extend(tool_results)
has_more_tool_calls = not batch.terminate and not batch.aborted
```

这里的 `has_more_tool_calls` 决定内层循环是否再次调用模型。注意 Assistant 本身更早已由 `_stream_assistant()` 放进 `context.messages`，所以第二次模型调用看到的顺序是：

```text
... → assistant(tool_calls=[...]) → tool result(s) → 下一次 LLM
```

这也解释了为何 Tool 异常不能直接穿透循环：`ToolRuntime._execute_prepared()` 会先把异常整理成带错误标记的 `_Finalized`，随后 `_commit()` 生成 `AgentMessage.tool_result(..., is_error=True)`。协议链仍然完整，模型才能读到失败并修正下一步。

## 6. 掌握标准

你应该能解释：

- 为什么 Tool Call 后必须再次调用模型？
- Assistant Tool Call 和 Tool Result 为什么都要进入 history？
- 哪些 Tool failure 应该转成模型可见结果？
- 为什么 Agent Loop 不需要提前知道一共会执行多少步？

下一章的问题是：Loop 能工作了，但调用方仍然看不到它正在做什么。
