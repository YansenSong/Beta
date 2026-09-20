# 05：Steering 与 Follow-up——新消息什么时候进入 Agent

> 参考：[`learn-pi-agent` Chapter 05](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/05-steering-followup)。

## 本章目标

理解“用户在 Agent 工作期间又发了一条消息”并不是一个简单的消息队列问题，而是一个**检查点时序**问题。

## 1. 为什么不直接抢占当前 turn

假设当前 Assistant 已经发出：

```text
tool A
tool B
tool C
```

Tool batch 正在运行时，用户补充：

```text
B 不查了，改查 D
```

如果立即把这条消息塞进当前 turn，就必须同时回答：

- 已启动的 B 要不要取消？
- 当前 Assistant Message 是否仍然成立？
- 已经完成的 Tool Result 是否要撤回？
- history 在什么位置改写？

Beta 选择和 Pi 类似的更稳定语义：**当前 turn 先完整结束，新输入只影响下一轮。**

## 2. Steering：turn 结束后的下一轮输入

Steering 的时间点是：

```text
assistant
  ↓
tool batch
  ↓
tool results
  ↓
turn_end
  ↓
check steering
  ↓
next turn
```

因此 Steering 不会取消当前 Tool，也不会撤销已发生历史。

它最终进入 context 时仍然只是普通 user message。

所谓 Steering，描述的是：

```text
这条 user message 在 run 的哪个 checkpoint 被读取
```

而不是一种新的 message role。

Beta 对应：

- `Agent.steer()`
- `_drain_steering()`
- `Agent._run()` 内层循环

源码：[`../../src/beta_agent/agent.py`](../../src/beta_agent/agent.py)。

## 3. Follow-up：本来准备结束时再检查

另一种输入更晚。

Agent 已经完成最终回答，没有 Tool Call，也没有 Steering，本来应该发出 `agent_end`。

这时如果有：

```text
再帮我总结成三点
```

这更适合 Follow-up。

检查点是：

```text
inner loop would stop
        ↓
check follow-up
        ↓
有消息 → 再开一轮
无消息 → agent_end
```

Follow-up 进入 history 后同样是普通 user message。

## 4. 为什么需要双层循环

如果 Steering 和 Follow-up 都塞进一个 `pending_messages` 获取函数：

- 每轮都读取，会让 Follow-up 太早进入；
- 只在结束时读取，又让 Steering 太晚影响下一轮。

所以两种时间语义自然形成：

```text
外层 while true
    └── 内层：Tool / Steering 驱动当前任务继续

内层准备停止
    └── Follow-up 决定是否重新进入内层
```

这就是 Beta `Agent._run()` 双层循环最重要的原因。

## 5. error / aborted 是另一种边界

Follow-up 接住的是一次**正常准备结束**的 run。

如果 Assistant 以 `error` / `aborted` 结束，当前 run 会直接结束，不应该偷偷借 Follow-up 把失败运行重新启动。

恢复失败任务应该由更高层显式决定。

## 6. 这章真正教的是“时间语义”

Steering 与 Follow-up 的 payload 可以完全一样，区别不在数据结构，而在 checkpoint。

这是一类很常见的框架设计：

```text
相同数据
+ 不同进入时间
= 不同运行语义
```

### 两条队列在源码中的消费点

`steer()` 和 `follow_up()` 都构造 user message，但 metadata 会留下投递方式：

```python
def steer(self, text: str) -> None:
    self._steering.push(AgentMessage.user(text, delivery="steering"))

def follow_up(self, text: str) -> None:
    self._follow_up.push(AgentMessage.user(text, delivery="follow_up"))
```

内层循环在一个 turn 结束、下一 turn 开始前调用 `_drain_steering()`；取到的消息进入 `pending`，随后追加到 `context.messages`。只有当内层循环准备退出时，外层循环才执行：

```python
follow_up = await self._drain_follow_up(cancellation)
if follow_up:
    pending = follow_up
    continue
break
```

`AgentConfig.steering_mode` 与 `follow_up_mode` 分别控制队列消费方式，默认都是 `"one-at-a-time"`：每个合法 checkpoint 只取最旧的一条，其余消息留到后续同类 checkpoint。设为 `"all"` 则保持一次 drain 全部消息的行为。外部 `get_steering_messages` / `get_follow_up_messages` provider 返回的消息也先进入对应队列，再按相同 mode 消费。

若 history 最后一条是 Assistant，`continue_stream()` 仍默认拒绝继续；但只要有 queued Steering / Follow-up，或配置了对应的外部 message provider，就可以恢复运行。优先级不变：可用 Steering 先进入下一轮，Follow-up 只在 Agent 本来准备结束时读取。

## 7. 掌握标准

你应该能解释：

- Steering 为什么不抢占当前 Tool batch？
- Steering 与 Follow-up 最终为什么都只是 user message？
- 两者为什么不能简单合成一个队列检查点？
- Beta 的双层循环分别负责什么？
- 为什么 aborted run 不继续读取 Follow-up？

下一章开始区分“Runtime 已经发生的历史”和“这一轮模型实际需要看到的 Context”。
