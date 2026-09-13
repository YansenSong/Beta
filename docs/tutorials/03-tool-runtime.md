# 03：Tool Runtime——把执行复杂度从 Agent Loop 中拿出去

> 参考：[`learn-pi-agent` Chapter 03](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/03-tool-runtime)。

## 本章目标

理解为什么 Tool execution 不应该只是 `tool.execute(args)`，以及 Tool Runtime 应该负责哪些事情。

## 1. Tool Call 在真正执行前还有很多步骤

一个成熟一点的 Tool Call 通常会经历：

```text
lookup
  ↓
prepare arguments
  ↓
validate
  ↓
before hook
  ↓
execute
  ↓
after hook
  ↓
normalize Tool Result
```

如果这些逻辑全部堆进 Agent Loop，那么每增加一种校验、权限或结果处理策略，主循环都会继续膨胀。

所以 Beta 用 [`../../src/beta_agent/tools.py`](../../src/beta_agent/tools.py) 把它们集中在 `ToolRuntime`。

## 2. lookup：模型只给名字，Runtime 找实现

模型输出的是 Tool name 和 arguments。

如果当前工具集中不存在这个名字，应该得到一个失败 Tool Result，而不是让 Agent Loop 因 `KeyError` 一类异常崩掉。

这说明：

```text
Tool Call 被 Runtime 接受处理
```

和：

```text
Tool handler 真正开始执行
```

不是同一个时刻。

## 3. prepare + validate：让 handler 只接收可信参数

Beta 的 `Tool` 允许 `prepare_arguments`，随后使用 Pydantic `args_model` 校验。

这样 handler 可以假设：

- 必需字段已经存在；
- 类型已经符合 schema；
- 模型格式和 Tool 内部格式之间的转换已经完成。

参数错误会被标准化为模型可见的失败，而不是散落在每个 handler 里重复处理。

## 4. before / after hook 是策略边界

`before_tool_call` 可以承载：

- 权限检查；
- 用户确认；
- 调用限制；
- 风险拦截。

`after_tool_call` 可以承载：

- 结果修饰；
- metadata 补充；
- error 状态调整；
- `terminate` 等控制信息。

重要的是：这些策略不需要写死进 Tool handler，也不需要污染 Agent Loop。

## 5. Tool progress 是 Runtime event，不是 history

长时间 Tool 可以通过 `ToolExecutionContext.progress()` 发：

```text
tool_execution_update
```

这些 update 是观察信息，不是模型对话历史。

真正写回 history 的只有最终 Tool Result。

这个区分很重要：

```text
observability state ≠ conversation state
```

## 6. 错误统一从 Tool Result 出口返回

Tool not found、参数校验失败、before hook block、handler exception，来源不同，但对于 Agent Loop 都可以统一成：

```text
ToolBatchResult
└── Message(role="tool", is_error=...)
```

Agent Loop 因而只需要关心：

```text
有 Tool Call
→ 交给 ToolRuntime
→ 收回 Tool Result
```

## 7. `terminate` 为什么是 batch 级判断

单个 Tool 可以返回 `terminate=True`，但一个 Assistant Message 可能同时产生多个调用。

Beta 只有在这一批 finalized result 都要求 terminate 时，才把整个 batch 视为终止。

这样单个 Tool 不会越权决定其他同批调用是否应该消失。

## 8. 掌握标准

你应该能解释：

- Tool Runtime 与 Agent Loop 的职责边界是什么？
- 参数校验为什么应该发生在 handler 之前？
- progress event 为什么不进入 message history？
- before / after hook 为什么比把策略写进每个 Tool 更可扩展？
- 一个 Tool 的 `terminate=True` 为什么不等于立刻终止整批？

下一章只改变一件事：多个准备好的 Tool 如何安全并发。