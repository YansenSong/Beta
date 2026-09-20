# Beta Agent 运行流程

本文从 `examples/beta_agent_cli.py` 中的一次用户输入出发，梳理用户输入、模型请求、事件转换和终端流式输出的完整链路。

- Steering：用户在任务运行过程中追加的修正；
- Follow-up：当前任务本来准备结束时追加的新要求。

```text
用户输入
将其构造成AgentMessage(role="user")
增加到context.messages中
读取启动前已经排队的 Steering → pending
while True：
	has_more_toolcalls = True
	while has_more_toolcalls or pending: 进入循环
		if previous_turn(TurnResult) is not None：（非首轮）
			仅当确认下一次 assistant request 会发生时，调用 prepare_next_turn
			Hook 可返回旧 AgentContext 或 NextTurnUpdate(context/messages/model)
			启动新的turn
		if pending:
			将pending中的消息增加到context.messages中
			清空pending
		启动新 run 时，先把当前 runtime tools 的 delta 放在本次 prompts 之前，再写入 transcript
		其他 turn 在模型请求前 replay transcript 并记录运行期间产生的 Tool 变化
		Hook：transform_context 调用模型之前，修改“本轮模型能看到的消息”
		将context.messages转换为模型适配层使用的 ProviderMessage
		从 transcript collapse 得到 system_prompt；使用 ProviderMessage 与当前 executable tools 请求模型API
		模型开始流式回复：
			收到start：
				向 context.messages 添加 assistant 占位消息
				发出 message_start
			收到 update：
				更新 context.messages[-1]
				发出 message_update
				CLI 实时打印新增文本
			收到 done：
				用最终完整的 assistant 消息更新 context.messages[-1]
				发出 message_end
		has_more_toolcalls = False
		if assistant.toolcalls:
			配置项：tool_execution：决定工具执行的模式，顺序或并行
			检查工具是否存在，参数预处理，并进行参数校验
			Hook：before_tool_call：在工具校验完成后、真正执行前进行权限或策略判断
			工具执行
			Hook：after_tool_call：在工具执行结果发出前修改结果、错误状态或终止标记
			将工具执行结果ToolTesult转为AgentMessage
			确认批次是否终止（多个工具是否全部执行）
			按模型原始调用顺序，将工具执行结果增加到context.messages中
			has_more_toolcalls = True：必须继续循环，让模型看到工具调用的结果
		将以上本轮结果保存为 previous_turn (TurnResult)
		Hook：should_stop_after_turn 如果要求停止:结束 run，状态为 completed
		pending = 当前turn完全结束时获取用户在turn过程中追加的消息
	follow_up = 模型要结束本次run时用户追加的消息
	if follow_up：
		pending = follow_up
		continue
	break
```

两条队列默认按 `one-at-a-time` 每个合法检查点消费一条；配置为 `all` 时才一次取出队列中全部消息。Agent 每次 emit lifecycle event 前会先 await `Agent.subscribe()` listeners，再把事件写入调用方消费的 EventStream。


```

## 1. 整体调用链

```text
用户输入
  ↓
beta_agent_cli.py: agent.stream(user_input)
  ↓
Agent.stream()
  ↓
EventStream 后台执行 Agent._run()
  ↓
Agent._run_impl() 驱动 Agent Loop
  ↓
Agent._stream_assistant() 准备上下文并消费模型流
  ↓
Agent._model_stream()
  ↓
OpenAICompatibleAdapter.stream() 发送 HTTP 请求
  ↓
ModelEvent(start/update/done/error)
  ↓
AgentEvent(message_start/message_update/message_end)
  ↓
CLI 增量打印 assistant 文本
```

如果模型产生 Tool Call，则形成循环：

```text
模型生成 Tool Call
  ↓
ToolRuntime 执行工具
  ↓
Tool Result 写入 self.context.messages
  ↓
再次调用模型
  ↓
模型读取工具结果并继续回答
```

## 2. CLI 接收用户输入

文件：`examples/beta_agent_cli.py`

CLI 主要完成三件事：

1. 获取用户输入 `user_input`；
2. 调用 `agent.stream(user_input)` 启动一次 Agent run；
3. 消费事件流，将 assistant 的新增文本打印到终端。

```python
stream = agent.stream(user_input)

async for event in stream:
    if (
        event.type == "message_update"
        and event.message is not None
        and event.message.role == "assistant"
    ):
        text = event.message.text
        print(text[printed_length:], end="", flush=True)
        printed_length = len(text)
```

`message_update` 携带当前完整的 partial message，而不是单个 token。因此 CLI 用 `printed_length` 只打印新增部分。

## 3. `Agent.stream()` 创建运行任务

文件：`src/beta_agent/agent.py`

```python
def stream(self, prompt):
    self._ensure_idle()
    prompts = self._normalize_prompts(prompt)
    token = CancellationToken()

    stream = EventStream(
        lambda emit: self._run(prompts, emit, token),
        on_cancel=lambda _: token.cancel(),
    )

    self._active_run = _ActiveRun(token=token, stream=stream)
    stream.add_done_callback(lambda completed: self._clear_active_run(completed))
    return stream
```

主要步骤：

1. `_ensure_idle()`：保证同一个 Agent 没有另一个正在执行的 run；
2. `_normalize_prompts()`：将字符串、单条消息或消息序列统一转换为 `list[AgentMessage]`；
3. 为本次运行创建 `CancellationToken`；
4. 创建 `EventStream`，将 `self._run(...)` 注册为后台执行入口；
5. 保存 `_active_run`，供状态检查和 `abort()` 使用；
6. 返回事件流，让调用方实时消费事件。

这里并不是直接同步执行 `_run()`。真正的执行发生在 `EventStream` 创建的异步任务中。

## 4. `EventStream` 启动后台任务

文件：`src/beta_agent/events.py`

`EventStream` 初始化时创建后台任务：

```python
self._task = asyncio.create_task(self._drive(runner))
```

其中 `runner` 是：

```python
lambda emit: self._run(prompts, emit, token)
```

`_drive()` 中执行：

```python
return await runner(emit)
```

等价于：

```python
return await self._run(prompts, emit, token)
```

`emit(event)` 会把 `AgentEvent` 放入内部异步队列。CLI 的 `async for event in stream` 正是在消费这条队列。

在调用 `emit` 前，Agent 会按注册顺序等待订阅者；Extension message dispatch 和 Session persistence 因此先于对应事件对外可见。`agent_end` 的 subscribers 全部完成后 run 才 settle，不再依靠 `sleep(0)` 让 ExtensionHost 抢到执行时机。

## 5. `Agent._run()` 统一处理退出路径

文件：`src/beta_agent/agent.py`

```python
async def _run(self, prompts, emit, cancellation):
    state = _RunState(new_messages=list(prompts))
    try:
        return await self._run_impl(prompts, emit, cancellation, state)
    except asyncio.CancelledError:
        ...
    except _StageFailure as failure:
        ...
    except Exception as exc:
        ...
```

`_run()` 是主循环外层的安全边界：

- 创建 `_RunState`，记录本次 run 的生命周期状态；
- 调用 `_run_impl()` 执行真正的 Agent Loop；
- 将取消、阶段错误和其他异常转换为统一的结束事件。

需要区分：

- `state.new_messages`：本次 run 新增的消息；
- `self.context.messages`：Agent 当前会话的完整历史。

## 6. `Agent._run_impl()` 驱动 Agent Loop

文件：`src/beta_agent/agent.py`

### 6.1 启动生命周期并写入用户消息

```python
state.agent_started = True
await emit(AgentEvent(type="agent_start"))
cancellation.throw_if_cancelled()

self.context.messages.extend(prompts)
state.turn_started = True
await emit(AgentEvent(type="turn_start"))
```

随后为完整的用户消息发出：

```text
message_start(user)
message_end(user)
```

### 6.2 双层循环

外层循环负责 Follow-up：只有 Agent 本来准备结束时，才检查是否还有后续请求。

内层循环负责当前任务的连续执行：

```python
has_more_tool_calls = True

while has_more_tool_calls or pending:
    ...
```

它处理第一次模型调用、Tool Call 后的下一次模型调用，以及 turn 结束后到达的 Steering 消息。

### 6.3 准备后续 Turn

```python
if previous_turn is not None:
    ...
```

这表示当前不是第一次模型调用，而是在准备下一个 turn。里面主要完成：

1. 调用 `prepare_next_turn`，允许返回旧式 `AgentContext` 或 `NextTurnUpdate`；
2. `NextTurnUpdate` 可替换 context、追加正式 transcript messages、切换后续 model；
3. 当前没有待处理消息时，读取新的 Steering；
4. 对 pending message 声明 tool-state delta，随后发出正常 message lifecycle；
5. 重置 turn 状态并发出新的 `turn_start`。

如果当前 turn 后没有下一次模型请求（例如 run 将结束、Stop Hook 已要求结束），`prepare_next_turn` 不会运行。

### 6.4 请求模型

```python
assistant_result = await self._stream_assistant(
    emit,
    cancellation,
    state,
)
```

执行到这里时，`self.context.messages` 已包含完整对话历史和本次用户问题。

如果 assistant 包含 Tool Call，`_run_impl()` 会通过 `ToolRuntime.execute_batch()` 执行工具，把 Tool Result 写入上下文，然后进入下一个 turn 再次请求模型。

## 7. `Agent._stream_assistant()` 准备并消费模型流

文件：`src/beta_agent/agent.py`

### 7.1 准备本轮上下文

```python
runtime_messages = list(self.context.messages)
```

如果配置了 `transform_context`：

```python
runtime_messages = await self._call(
    self.config.transform_context,
    list(runtime_messages),
    cancellation=cancellation,
)
```

这个可选 Hook 用于修改“本轮模型能看到的消息”，例如裁剪历史或注入摘要。默认不会覆盖 `self.context.messages` 保存的完整历史。

System instruction 与模型可见 Tool 声明则从 canonical transcript 顺序 replay。Provider 兼容投影把所有 system text 合并到单一 `system_prompt`，从普通 Provider message list 移除 system-state messages，并用当前 `context.tools` 作为顶层 tools；这避免把历史 delta 重复发送给 OpenAI-compatible API。

### 7.2 转换为 Provider 消息

```python
converted_messages = await self._call(
    self.config.convert_to_llm,
    list(runtime_messages),
    cancellation=cancellation,
)
provider_messages = list(converted_messages)
```

这一步将 Runtime 使用的 `AgentMessage` 转换为模型适配层使用的 `ProviderMessage`，并移除仅供 Runtime 使用的字段。

### 7.3 消费模型事件流

```python
model_stream = self._model_stream(provider_messages, cancellation)

async for event in model_stream:
    ...
```

模型适配器通常依次产生：

```text
start → update → update → ... → done
```

发生错误时可能是：

```text
start → update → error
```

`_stream_assistant()` 的转换关系如下：

| ModelEvent | Agent 处理 |
| --- | --- |
| `start` | 加入 assistant 占位消息，发出 `message_start` |
| `update` | 更新最后一条 assistant partial，发出 `message_update` |
| `done` | 保存最终消息，发出 `message_end` |
| `error` | 标准化错误消息，发出 `message_end` 并返回错误信息 |

`event.partial` 是当前累计得到的完整 assistant 消息，而不是单个 token。

## 8. `Agent._model_stream()` 调用模型适配器

文件：`src/beta_agent/agent.py`

```python
stream = self.model.stream
kwargs = {
    # 由 transcript replay/collapse 得到的只读兼容投影
    "system_prompt": system_prompt,
    "messages": messages,
    "tools": self.context.tools,
}

if accepts_cancellation(stream):
    kwargs["cancellation"] = cancellation

return stream(**kwargs)
```

CLI 创建 Agent 时传入的是 `OpenAICompatibleAdapter`，所以这里实际调用：

```python
OpenAICompatibleAdapter.stream(...)
```

`ModelAdapter(Protocol)` 只规定适配器应提供的接口。Python 使用结构化类型，因此 `OpenAICompatibleAdapter` 无需显式继承 `ModelAdapter`；只要方法签名和行为满足协议，就可以作为模型适配器传给 Agent。

## 9. `OpenAICompatibleAdapter.stream()` 发送请求

文件：`src/beta_agent/adapters/openai_compatible.py`

适配器负责：

1. 将 system prompt、Provider 消息和工具转换为 OpenAI-compatible 请求体；
2. 使用 `httpx.AsyncClient.stream()` 请求 `/chat/completions`；
3. 逐行读取 SSE 数据；
4. 累积文本和 Tool Call 参数；
5. 持续生成统一的 `ModelEvent`。

真正的 HTTP 调用是：

```python
async with client.stream(
    "POST",
    f"{self.base_url}/chat/completions",
    json=payload,
    headers=headers,
) as response:
    ...
```

`OpenAICompatibleAdapter.stream()` 是异步生成器。调用 `_model_stream()` 时通常只创建生成器对象；直到上层执行 `async for event in model_stream`，才真正开始请求并读取响应。

## 10. 模型输出返回 CLI

适配器产生 `ModelEvent(update)` 后：

1. `_stream_assistant()` 更新 `self.context.messages` 中最后一条 assistant 消息；
2. `_stream_assistant()` 发出 `AgentEvent(type="message_update")`；
3. Agent 先 await 已注册的 subscribers，再把事件放入 EventStream 异步队列；
4. CLI 的 `async for event in stream` 取出事件；
5. CLI 计算新增文本并打印到终端。

一次没有工具调用的典型事件顺序为：

```text
agent_start
turn_start
message_start(user)
message_end(user)
message_start(assistant)
message_update(assistant)
message_update(assistant)
...
message_end(assistant)
turn_end
agent_end(status="completed")
```

至此，一次从用户输入到模型输出展示的完整流程结束。
