# 02：Agent Runtime 为什么需要统一事件流

> 参考：[`learn-pi-agent` Chapter 02](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/02-agent-runtime)。

## 本章目标

理解 Agent 的“最终结果”和“运行过程”为什么应该同时成为框架输出。

## 1. 只返回最终 messages 不够

一个 Tool-driven run 可能经历：

```text
LLM 开始
→ Assistant 流式生成
→ Tool 开始执行
→ Tool 完成
→ 下一轮 LLM
→ 最终结束
```

如果 API 只有：

```text
await agent.run(...)
```

那么 UI、日志、Tracing 在整个过程中都只能等待。

最容易想到的是增加很多 callback，但随着生命周期增多，Agent Loop 会开始直接认识 UI、日志和监控需求。

更稳定的做法是把 Runtime 状态变化统一成事件。

## 2. Beta 的事件层级

Beta 通过 `AgentEvent` 与 `EventStream` 暴露：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / update / end
```

其中前三组让外部可以观察一次 run、一次 turn 和一条 message 的生命周期。

对应源码：

- [`../../src/beta_agent/events.py`](../../src/beta_agent/events.py)
- [`../../src/beta_agent/types.py`](../../src/beta_agent/types.py)
- [`../../src/beta_agent/agent.py`](../../src/beta_agent/agent.py)

## 3. `message_update` 为什么发完整 partial

流式模型底层通常给的是 delta。

但 Runtime 对外如果也只暴露 delta，消费者就必须自己维护一份正在生成的 Assistant Message。只处理文本还好，Tool Call arguments 也流式生成时就会变复杂。

Beta 的选择是：

```text
第一次 update: "Hel"
第二次 update: "Hello"
第三次 update: "Hello world"
```

即每次事件携带当前完整 partial message，而不是只携带最新字符片段。

好处是：

- UI 丢掉一次中间事件也能从下次恢复；
- 日志、Tracing 不必重复实现拼接状态机；
- 文本与 Tool Call 可以统一看作 Assistant Message 的当前快照。

## 4. 流式 Assistant 在 history 中怎样存在

`Agent._stream_assistant()` 会在开始时把 partial message 放进 context，后续 update 用最新 partial 替换它，直到 final message 到达。

因此 Runtime history 在流式生成期间也能代表“当前最新状态”。

这比在旁边再维护一份完全独立的 streaming state 更简单。

## 5. stop reason 也是执行语义

Assistant Message 不是只看有没有 Tool Call。

如果 `stop_reason` 是 `error` / `aborted`，run 应该结束；如果是 `length`，已经出现的 Tool Call 可能只是被截断的半成品，不能直接执行。

Beta 会把这种截断调用变成失败的 Tool Result，而不是冒险运行不完整参数。

### Agent subscriber、EventStream 和 Assistant partial 如何衔接

[`EventStream`](../../src/beta_agent/events.py) 内部是一条 `asyncio.Queue` 加一个 runner task。Agent 对外发布每个事件时，会先按注册顺序 await `Agent.subscribe(listener)` callbacks，再调用 EventStream 的 `emit()`：

```python
async def publish(event: AgentEvent) -> None:
    for listener in listeners:
        await listener(event, cancellation)
    await event_stream_emit(event)
```

订阅者适合承担需要先于下一条 Runtime event 完成的工作，例如 Extension `message_end` / `turn_end` dispatch 和 Session persistence。`agent_end` subscribers 完成后 run 才 settle，`wait_for_idle()` 也会等到这个边界。EventStream 则继续为 CLI/UI 提供异步迭代接口；它不需要通过主动让出调度来保证 bridge 顺序。

在 [`Agent._stream_assistant()`](../../src/beta_agent/agent.py) 中，`ModelEvent.partial` 每次都替换当前 Assistant，并发出副本：

```python
state.current_assistant = partial
if added_partial:
    self.context.messages[-1] = partial
await emit(AgentEvent(type="message_update", message=partial.copy()))
```

因此 queue 负责“把变化送出去”，`context.messages[-1]` 负责“Runtime 当前认为什么是真的”。最终 `done` partial 会成为 history 中的完整 Assistant，而消费者无需自行拼 token delta。

## 6. 掌握标准

你应该能回答：

- 为什么 Event Stream 比大量 callback 更适合框架层？
- turn 和 message 生命周期分别代表什么？
- 为什么 `message_update` 用 whole partial 而不是 raw delta？
- 为什么 `stop_reason="length"` 时 Tool Call 不应该直接执行？

下一章会把 Tool 执行本身也拆出明确生命周期。
