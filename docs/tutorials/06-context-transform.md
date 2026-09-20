# 06：Context Transformation——发生过什么，不等于这一轮都要发给模型

> 参考：[`learn-pi-agent` Chapter 06](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/06-context-transform)。

## 本章目标

理解为什么要区分：

```text
Runtime history
```

和：

```text
current LLM input
```

这两个概念。

## 1. History 会越来越长

一次长期运行的 Agent 可能积累：

```text
U1
A1
Tool1
U2
A2
Tool2
U3
...
```

这些消息都已经真实发生过，未来可能用于：

- Session 恢复；
- 调试和审计；
- 分支历史；
- 后续 Compaction。

但下一次调用模型时，并不一定需要全部发送。

如果为了缩短 Context 直接删掉 `context.messages`，就把“工作视图变短”和“历史被破坏”混成了一件事。

## 2. Beta 的 `transform_context`

Beta 的 `AgentConfig.transform_context` 允许在模型调用前生成一份临时 LLM input。

执行关系是：

```text
self.context.messages
        ↓
transform_context
        ↓
llm_messages
        ↓
ModelAdapter.stream()
```

关键在于：返回的新列表只服务当前模型调用。

默认情况下，它不会替换 Runtime 中的 canonical history。

系统消息也属于这个临时视图：Agent 会在转换 Provider message 前，从 transformed messages 重放 system text，因此临时追加、修改或移除 system message 会影响本轮请求但不会改写 transcript。顶层可执行 tools 仍取自 `AgentContext.tools`，transform hook 不能靠声明伪造 Runtime 不具备的 handler。

对应源码：[`../../src/beta_agent/agent.py`](../../src/beta_agent/agent.py) 的 `_stream_assistant()`。

## 3. 这层可以做什么

常见策略包括：

- 只保留最近若干轮；
- 过滤内部消息；
- 临时插入检索结果；
- 追加当前项目背景；
- 针对不同模型准备不同 Context view。

这些策略变化很快，所以不应该写死进 Agent Loop。

Agent Loop 只需要知道：

```text
调用模型前，可以先准备一次 Context view
```

具体怎么准备，交给 Hook。

## 4. Context 裁剪不能破坏 Tool 协议关系

“最后 N 条消息”是个危险的简化。

如果裁剪结果从一条 Tool Result 开始：

```text
tool result
user
assistant
```

但对应的 Assistant Tool Call 已经被裁掉，那么 Provider 侧可能得到非法或语义残缺的上下文。

因此真实 Context policy 必须理解消息关系，而不仅仅是数组长度。

至少要避免把：

```text
assistant(tool call)
→ tool result
```

切成只剩后一半。

## 5. `prepare_next_turn` 是另一层

`transform_context` 只改变：

```text
这一轮模型看什么
```

而 `prepare_next_turn` 可以真正决定：

```text
下一轮 Runtime 从什么 AgentContext 继续
```

这两个 Hook 不应该混淆。

可以这样记：

```text
transform_context = 临时视图
prepare_next_turn = 下一轮状态
```

Beta 在已经确定还会继续发起下一次 assistant request 后，才在 turn 边界调用 `prepare_next_turn`。旧 Hook 仍可返回新的 `AgentContext`；也可返回 `NextTurnUpdate`：

```python
NextTurnUpdate(
    context=replacement_context,  # 可选
    messages=[AgentMessage.system("下一阶段说明")],  # 可选，正式 transcript message
    model=next_model,  # 可选，影响本轮之后的请求
)
```

prepared messages 会经过 tool-state reconciliation、`message_start` / `message_end` 和 Session persistence，不是仅作用于当前请求的临时 prompt。最终 turn、被 Stop Hook 结束的 run、或其他没有下一次请求的路径都不会运行 prepare hook。

## 6. 为什么这一层非常重要

后面的 Session、Compaction、RAG、内部消息过滤，本质上都依赖“历史”和“模型输入”可以不是同一份数组。

如果一开始把两者绑死，后续每一种 Context 策略都会开始直接修改历史结构。

### `_stream_assistant()` 中的三份数据

源码明确保留了三层表示：

```python
runtime_messages = list(self.context.messages)
if self.config.transform_context:
    runtime_messages = await self._call(
        self.config.transform_context,
        list(runtime_messages),
        cancellation=cancellation,
    )
converted_messages = await self._call(
    self.config.convert_to_llm,
    list(runtime_messages),
    cancellation=cancellation,
)
provider_messages = list(converted_messages)
```

- `self.context.messages` 是 canonical Runtime history；
- `runtime_messages` 是本轮经过裁剪、补充或过滤的 `AgentMessage` view；
- `provider_messages` 是去掉 Runtime-only 字段后的 Provider DTO。

Hook 收到的是 `list(self.context.messages)` 的浅拷贝，因此简单的增删列表项不会直接改写 canonical list。相反，`prepare_next_turn(previous_turn)` 可以返回新的 `AgentContext`，或通过 `NextTurnUpdate` 替换 context、追加 transcript messages、切换 model；这正是“临时视图”和“改变后续状态”的源码差异。

## 7. 掌握标准

你应该能回答：

- 为什么 Runtime history 不能等同于当前 LLM input？
- `transform_context` 为什么更像 view，而不是 storage？
- 为什么简单 `messages[-N:]` 可能破坏 Tool Call / Tool Result 关系？
- `transform_context` 与 `prepare_next_turn` 的差异是什么？
- 哪些未来能力适合建立在 Context transformation 上？

下一章把问题从“这一轮模型看什么”扩展到“完整历史到底怎样保存和分支”。
