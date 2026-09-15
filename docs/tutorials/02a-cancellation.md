# 02A：用户点击“停止”之后发生了什么

第 02 章介绍了 EventStream：调用方可以一边等待 Agent 工作，一边看到模型输出和 Tool 进度。

但真实产品很快会遇到另一个问题：

- 用户发现模型理解错了，想马上改一句话；
- Agent 正在执行一个很慢的搜索、测试或网络请求；
- 模型准备调用一个不合适的 Tool，用户想在它继续之前刹车；
- 用户已经等够了，不想继续消耗时间和模型额度；
- Coding Agent 看起来要修改错误的文件，用户必须立刻停止。

这时 UI 不能只停止渲染事件。因为 Agent 可能仍然在后台调用模型、执行 Tool，甚至继续修改文件。

所以调用方需要一个真正的停止入口：

~~~python
agent.abort()
~~~

本章沿着这个真实动作，解释 Beta 的 cancellation 机制。

## 本章目标

读完本章，你应该能看懂下面这条链路：

~~~text
用户点击“停止”
    ↓
agent.abort()
    ↓
当前 run 收到取消请求
    ↓
模型 / Tool 停止继续工作
    ↓
Agent 补齐必要的结束事件
    ↓
agent_end(status="aborted")
~~~

这里先记住一句话：

> abort 是用户发起的动作；CancellationToken 是共享的通知；CancelledError 是 Runtime 用来结束控制流的信号。

## 1. 为什么“停止读取事件”不等于“停止 Agent”

假设 UI 这样运行 Agent：

~~~python
stream = agent.stream("请检查项目并修复测试")

async for event in stream:
    render(event)
~~~

如果用户关闭了窗口，或者 UI 只是从循环里 break：

~~~python
async for event in stream:
    render(event)
    if user_closed_panel:
        break
~~~

这只代表 UI 不再读取事件，不代表 EventStream 背后的 Agent 任务已经结束。

后台可能仍在做：

~~~text
调用模型
    ↓
模型返回 Tool Call
    ↓
执行 Tool
    ↓
再次调用模型
~~~

因此真实 UI 通常把“停止按钮”和 Agent 绑定起来：

~~~python
def on_stop_button_clicked(agent):
    agent.abort()
~~~

如果使用 run() 而不是直接消费 stream，也可以让 run 在后台运行，然后从另一个 UI 任务里调用 abort：

~~~python
run_task = asyncio.create_task(
    agent.run("请检查项目并修复测试")
)

# 用户稍后点击停止按钮
agent.abort()

result = await run_task
~~~

这个例子说明了 abort() 的真实作用：它不是“告诉 UI 不要显示了”，而是“请求这一次 Agent 工作停止”。

## 2. agent.abort() 做了什么

Beta 在 [../../src/beta_agent/agent.py](../../src/beta_agent/agent.py) 中把每一次运行记录为一个 active run：

~~~python
token = CancellationToken()
stream = EventStream(
    lambda emit: self._run(prompts, emit, token),
    on_cancel=lambda _: token.cancel(),
)
self._active_run = _ActiveRun(token=token, stream=stream)
~~~

因此 abort() 可以找到当前这一次运行：

~~~python
def abort(self) -> None:
    active = self._active_run
    if active is None:
        return
    active.token.cancel()
    active.stream.cancel()
~~~

逐行翻译就是：

~~~text
没有正在运行的 Agent？
    什么也不做。

有正在运行的 Agent？
    先告诉所有参与者“不要继续了”；
    再请求承载这次 run 的 asyncio Task 停止。
~~~

它们看起来像重复做了两次取消，但其实是两条不同的路径：

~~~text
active.token.cancel()
    → 业务代码可以检查到取消

active.stream.cancel()
    → EventStream 可以打断自己的 runner task
    → EventStream 的 on_cancel 也会再次通知 token
~~~

重复调用是安全的，因为 CancellationToken.cancel() 使用的 asyncio.Event.set() 是幂等的。

## 3. Token 就像“这次任务的红灯”

先看 [../../src/beta_agent/cancellation.py](../../src/beta_agent/cancellation.py)：

~~~python
class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()
~~~

可以把一次运行想成一辆正在行驶的车：

~~~text
token.cancelled = False
    车可以继续走

调用 token.cancel()
    红灯亮了

代码调用 throw_if_cancelled()
    发现红灯 → 抛出 CancelledError → 把控制权交回 Runtime
~~~

重要的是，token.cancel() 只是亮红灯，并不会凭空跳进正在执行的 Tool 函数里。

真正检查红灯的是：

~~~python
cancellation.throw_if_cancelled()
~~~

如果还没有取消，这行代码什么也不做；如果已经取消，它抛出 asyncio.CancelledError。

所以调用方不需要自己调用 throw_if_cancelled()。调用方只需要调用：

~~~python
agent.abort()
~~~

Agent、Model Adapter 和 Tool 会在自己的安全检查点检查同一个 Token。

## 4. 为什么还要调用 stream.cancel()

Token 适合通知业务代码，但 Agent 可能此刻正停在一个异步等待里，例如：

~~~text
等待模型网络响应
等待 Tool 的子任务
等待文件或进程操作
~~~

如果只把 Token 标记为取消，当前 await 可能还要等很久才返回，Agent 才能执行下一次检查。

EventStream 自己持有一个 asyncio Task。它的 runner 大致是：

~~~python
async def _drive(self, runner):
    self._started = True
    async def emit(event):
        await self._queue.put(event)
        await asyncio.sleep(0)

    try:
        return await runner(emit)
    finally:
        self._close_queue()
~~~

这里的 runner 就是前面传入的：

~~~python
lambda emit: self._run(prompts, emit, token)
~~~

所以 EventStream 实际上在一个任务里运行整个 Agent。

当调用 stream.cancel() 时，它会：

~~~text
1. 标记 stream 已经收到取消请求；
2. 调用 on_cancel，让 token 变成 cancelled；
3. 如果 runner 已经启动，对底层 asyncio Task 调用 task.cancel()；
   如果 runner 还没启动，则让它启动后通过 token 走 aborted 收尾。
~~~

task.cancel() 的含义是“请求当前协程在可中断的位置抛出 CancelledError”，不是强制杀掉整个 Python 线程。

因此两种取消配合起来：

~~~text
Token
    让 Model / Tool 知道不要开始下一步

Task.cancel()
    尝试打断当前正在等待的 await
~~~

如果某个 Tool 一直做同步阻塞工作，既没有 await，也没有检查 Token，那么任何协作式取消机制都不能让它瞬间停下。这个 Tool 自己需要把工作拆成可检查的步骤，或者使用支持取消的子进程 / 异步 API。

## 5. 以“用户中止一次代码修复”为例

假设用户输入：

~~~text
请运行测试，找到失败原因并直接修改代码。
~~~

Agent 可能经历：

~~~text
模型开始回答
    ↓
模型请求执行测试命令
    ↓
Tool 开始运行测试
    ↓
用户看到命令很慢，或者发现命令方向不对
    ↓
用户点击“停止”
~~~

此时：

~~~python
agent.abort()
~~~

典型的内部过程是：

~~~text
1. 当前 run 的 token 被标记为 cancelled；
2. EventStream 请求已经启动的 runner task 取消；如果它还没启动，
   则由 token 让 Agent 启动后直接走 aborted 收尾；
3. 正在等待的模型 / Tool 协程收到 CancelledError，
   或者稍后在 throw_if_cancelled() 处发现取消；
4. Tool Runtime 不再启动后续 Tool；
5. 已经开始但未完成的工作被标记为 aborted；
6. Agent 补齐 turn 和 agent 的结束生命周期；
7. 调用方收到 agent_end(status="aborted")。
~~~

用户看到的重点不是 Python 异常，而是一个明确的产品状态：

~~~text
这次任务被用户停止了
~~~

如果取消发生在模型正在生成 Assistant Message，Runtime 还要处理已经发出的 partial message。否则外部可能已经收到 message_start，却永远等不到对应的 message_end。

因此 Agent._stream_assistant() 会把未完成的 Assistant Message 收束为 aborted，再把取消信号继续交给上层。

## 6. 取消信号怎样回到 Agent

Agent 的最外层运行函数在 [../../src/beta_agent/agent.py](../../src/beta_agent/agent.py) 中单独处理 CancelledError：

~~~python
async def _run(...):
    state = _RunState(new_messages=list(prompts))
    try:
        return await self._run_impl(...)
    except asyncio.CancelledError:
        cancellation.cancel()
        return await self._finalize_aborted(state, emit)
    except _StageFailure as failure:
        return await self._finalize_error(state, emit, failure.info)
    except Exception as exc:
        return await self._finalize_error(state, emit, _error_info("runtime", exc))
~~~

这里的意思是：

~~~text
取消异常到达 _run()
    ↓
确认 token 仍然是 cancelled
    ↓
执行 _finalize_aborted()
    ↓
发送 agent_end(status="aborted")
~~~

CancelledError 是内部控制流，不应该直接变成用户看到的“普通错误”。

## 7. Model 和 Tool 在什么地方检查取消

取消不是后台线程自动替大家检查的。Runtime 在重要边界主动检查。

当前 Agent 会在这些时机检查：

~~~text
开始一次模型调用前
模型流式输出每个事件时
开始 Tool batch 前
准备每个 Tool Call 时
开始执行 Tool 时
进入下一轮 Loop 前
~~~

例如模型流式处理：

~~~python
async for event in model_stream:
    cancellation.throw_if_cancelled()
    # 处理 start / update / done
~~~

Tool 收到的是同一个 Token：

~~~python
ToolExecutionContext(
    tool_call_id,
    tool_name,
    emit,
    cancellation=cancellation,
)
~~~

因此一个长时间 Tool 可以这样写：

~~~python
async def process_many(args, ctx):
    for path in args.paths:
        ctx.cancellation.throw_if_cancelled()
        await process_one(path)
    return "done"
~~~

假设它已经处理了前 3 个文件，用户点击停止。下一次循环开始前的检查就会抛出取消异常，Tool 不会继续处理剩下的文件。

Tool 还可以通过 ctx.progress(...) 上报进度；这个方法内部也会先检查取消。

如果 Tool 需要清理自己的资源，应该清理后继续抛出取消异常：

~~~python
try:
    await do_work()
except asyncio.CancelledError:
    await cleanup()
    raise
~~~

不要把取消吞掉并伪装成普通成功：

~~~python
try:
    await do_work()
except asyncio.CancelledError:
    return "done"
~~~

这样上层就不知道用户已经点击了停止，可能继续进入下一轮 Agent Loop。

## 8. “停止”“失败”和“正常结束”有什么区别

用真实使用场景区分这三个状态：

| 场景 | 例子 | 结果 |
| --- | --- | --- |
| 用户停止 | 用户点击停止按钮 | agent_end(status="aborted") |
| 执行失败 | 模型接口报错、Tool 抛出异常 | agent_end(status="error")，或得到错误 Tool Result |
| 正常结束 | 模型给出最终答案，或 Tool 返回 terminate=True | agent_end(status="completed") |

terminate=True 是 Tool 或策略告诉 Loop：“这次正常工作到这里结束”。它不等于取消，也不等于立刻杀掉同一批仍在执行的 Tool。

而 aborted 明确表示：这次工作没有正常完成，是因为有人请求停止。

## 9. 对照 Beta 阅读

建议按这个顺序打开源码：

- [../../src/beta_agent/agent.py](../../src/beta_agent/agent.py)：stream() 创建一次 run，abort() 发起停止，_run() 负责统一收尾；
- [../../src/beta_agent/events.py](../../src/beta_agent/events.py)：EventStream 如何持有 runner task，以及 cancel() 怎样触发 on_cancel 和 task.cancel()；
- [../../src/beta_agent/cancellation.py](../../src/beta_agent/cancellation.py)：Token 如何保存 cancelled 状态；
- [../../src/beta_agent/model.py](../../src/beta_agent/model.py)：Model Adapter 如何在多个阶段检查 Token；
- [../../src/beta_agent/tools.py](../../src/beta_agent/tools.py)：Tool batch 如何取消 child task，并为未完成调用补出 aborted result。

## 10. 掌握标准

你应该能解释：

- 为什么 UI 停止读取 EventStream，不等于 Agent 已经停止？
- 用户在什么真实情况下会调用 agent.abort()？
- token.cancel() 和 stream.cancel() 各自解决什么问题？
- 为什么 Tool 需要使用 Agent 传进来的 Token？
- 为什么取消最后是 aborted，而不是普通 error？
- 一个同步阻塞、完全不检查 Token 的 Tool，为什么不能及时响应停止？

下一章回到正常执行路径：Tool Runtime 如何把 lookup、校验、hook、execute 和结果规范化拆开。
