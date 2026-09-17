# 04：并行 Tool——真实完成顺序和历史顺序不是一回事

> 参考：[`learn-pi-agent` Chapter 04](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/04-parallel-tools)。

## 本章目标

理解并行 Tool Runtime 最关键的两个问题：**哪些阶段可以并发**，以及**并发以后应该保留哪一种顺序**。

## 1. 不是把整个 executeToolCall 扔进 gather

一个 Tool Call 在 Chapter 03 已经包含：

```text
lookup → prepare → validate → before hook → execute
```

如果从 lookup 开始全部并发，多个权限确认、参数准备和外部交互也会一起发生，时序会变得不可控。

Pi 的思路，也是 Beta 当前保留的思路，是：

```text
preflight 按 source order
    ↓
真正 execute 才并发
```

也就是：

```text
prepare A
prepare B
prepare C

然后：
execute A ─────────
execute B ──
execute C ─────
```

Beta 对应 [`../../src/beta_agent/tools.py`](../../src/beta_agent/tools.py) 中 `ToolRuntime.execute_batch()` 的 parallel path。

## 2. prepare 失败也必须保留位置

假设模型产生：

```text
A, B, C
```

B 在 lookup 或 validate 阶段已经失败。

并行阶段真正执行的可能只有 A 和 C，但 B 不能从 batch 中消失。

最终仍然应该得到三个位置：

```text
A result
B error result
C result
```

因为模型原始 Assistant Message 确实发出了三个 Tool Call。

## 3. 并发后自然出现两种顺序

假设模型顺序是：

```text
A → B → C
```

真实耗时让它们按下面顺序完成：

```text
B → C → A
```

此时两种顺序都正确，只是用途不同。

### 观察事件：completion order

`tool_execution_end` 应该在 Tool 真正完成时立即发：

```text
end B
end C
end A
```

UI、日志和 Tracing 需要看到真实发生时间。

### History：source order

Tool Result 写回消息历史时仍然保持：

```text
A
B
C
```

这样同一个 Assistant Message 在不同机器、不同网络条件下，不会仅因为完成速度不同而产生不同 history。

一句话记忆：

> 事件忠于现实时间，历史忠于模型原始结构。

## 4. `asyncio.gather` 为什么刚好适合提交顺序

Beta 并发执行后通过 `asyncio.gather()` 收集结果。

每个执行结束时可以立即发 end event；而 gather 返回值仍按输入 awaitable 的位置排列，因此最后 commit history 时可以恢复 source order。

## 5. 什么时候必须退回 sequential

Beta 支持：

- 全局 `tool_execution="sequential"`；
- 单个 Tool 声明 `execution_mode="sequential"`。

如果 batch 中存在必须串行的 Tool，就不要为了追求并发而破坏它的执行约束。

并行是性能策略，不应该改变 Tool 的正确性语义。

## 6. terminate 不等于取消同批任务

某个较快 Tool 先返回 `terminate=True`，不会立即取消正在跑的其他 Tool。

当前 batch 仍然完整 settle，之后再决定下一轮是否继续。

“停止后续 Loop”和“中止当前并发任务”是两种不同机制。后者更接近 cancellation / abort 语义。

### 并行分支的索引为什么不能丢

源码先创建与调用数等长的槽位：

```python
entries: list[_Finalized | _Prepared | None] = [None] * len(calls)
```

preflight 按 `enumerate(calls)` 写回对应槽位；只有 `_Prepared` 会被转成 task，并以原索引作为 key：

```python
tasks[index] = asyncio.create_task(run(index, entry))
results = await asyncio.gather(*tasks.values())
for index, item in zip(tasks, results):
    entries[index] = item
```

`run()` 内部一完成就发 `tool_execution_end`，所以事件是 completion order；`gather()` 返回值与传入 awaitable 同序，再借 `index` 写回 `entries`，所以 `_commit()` 得到 source order。准备失败的 `_Finalized` 从未离开原槽位，也就不会因“没有 task”而消失。

## 7. 掌握标准

你应该能回答：

- 为什么 preflight 不一定适合并发？
- prepare 失败的 Tool 为什么还要保留原始位置？
- completion order 和 source order 分别服务谁？
- 为什么并发 Tool Result 不应该按完成速度写 history？
- `terminate` 与 cancellation 有什么区别？

下一章讨论的不是性能，而是时间：Agent 正在工作时，新的用户消息什么时候才能进入。
