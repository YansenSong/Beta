# 08：Context Compaction——缩短工作 Context，但不要删除历史

> 参考：[`learn-pi-agent` Chapter 08](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/08-context-compaction)。

## 本章目标

理解长会话压缩为什么应该成为 Session 历史的一部分，以及为什么 Compaction 的目标是“改变重建方式”而不是“删掉旧消息”。

## 1. Session 可以无限保存，模型 Context 不可以

有了 Session Tree 以后，完整历史可以一直追加：

```text
U1
A1
Tool1
...
U80
A80
```

但模型的 Context Window 是有限的。

如果只是临时在内存中做：

```text
messages = compact(messages)
```

那么程序重启或切换 branch 后，这次压缩就失去了稳定语义。

所以压缩结果需要和 Session path 建立关系。

## 2. Beta 把 Compaction 也记录成 Entry

[`../../src/beta_agent/harness/compation.py`](../../src/beta_agent/harness/compation.py) 会生成 summary，并通过 `SessionTree.append_compaction()` 追加一条新的 Entry。

它记录：

```text
summary
first_kept_entry_id
tokens_before
```

最重要的是：旧 Entry 一个都不删除。

```text
旧历史
   ↓
追加 CompactionEntry
```

因此 Session 甚至会多一条记录，而不是变短。

## 3. 真正变短的是 reconstruction

假设完整 branch 是：

```text
U1 → A1 → U2 → A2 → U3 → A3 → C → U4 → A4
```

Compaction `C` 表示前半段已经被 summary 覆盖，同时 `first_kept_entry_id` 指向 `U3`。

那么 `reconstruct_messages()` 可以得到：

```text
system: 原始 system/tool baseline（若有）
system: Conversation Summary
U3
A3
U4
A4
```

原始 `U1 ~ A2` 仍然存在于 Session Tree，只是不再逐条进入 canonical working context。

Compaction summarizer 不会把 system-state messages 当作旧对话内容送去摘要。重建时 Session 先从被压缩的 transcript replay 当前 system prompt 与 Tool declaration baseline，再加 summary system message 和 retained tail；因此摘要不会取代 Coding Agent 原始指令，也不会丢失有效工具状态。

所以 Compaction 的本质是：

> 保存一条“以后怎样重建这条 branch”的历史指令。

## 4. 为什么 cut point 必须安全

不能只按 token 数随便找切点。

如果历史中有：

```text
assistant(tool call)
tool result
```

却从 `tool result` 开始保留，就会把调用和结果拆开。

于是 Context 虽然变短了，却可能变成 Provider 无法正确理解的协议序列。

Beta 当前采取一个简单但保守的策略：尽量向前寻找 user message 作为 retained tail 起点。

这是教学骨架，不是最终生产级算法。以后可以进一步识别完整 Tool Call / Tool Result group，再根据 token budget 找安全边界。

## 5. Compaction 天然是 branch-local

因为 Compaction 本身就是树上的 Entry，所以它只影响经过自己的路径。

```text
A → B → C → Compaction → D
        \
         X → Y
```

`D` 的 active path 经过 Compaction，因此 reconstruction 使用 summary。

`Y` 的路径没有经过它，因此不会应用这次 summary。

不需要额外维护一个全局的：

```text
session.is_compacted = true
```

## 6. 多次 Compaction 怎么办

长会话可能有：

```text
...
C1
...
C2
...
```

重建当前 active branch 时使用最近一次有效 Compaction 即可。

旧 Compaction Entry 继续留在历史里，因为它仍然可能对旧 branch 有意义。

## 7. Compaction 与 `transform_context` 不同

两者都可能让模型输入变短，但所在层次不同：

```text
Session branch
    ↓
apply Compaction
    ↓
canonical Agent messages
    ↓
transform_context
    ↓
current LLM input
```

Compaction 改变 Session 到 canonical messages 的长期重建语义。

`transform_context` 只改变某一次模型调用的临时 view。

## 8. 对照 Beta 阅读

重点看：

- `compact_session()`：选择 retained tail、调用 summarizer；
- `SessionTree.append_compaction()`：Compaction 怎样进入树；
- `SessionTree.reconstruct_messages()`：summary 怎样替代旧前缀。

### cut point 与 reconstruction 的具体实现

[`compact_session()`](../../src/beta_agent/harness/compation.py) 先收集当前 branch 中的 message Entry，从尾部倒推希望保留的数量，再继续向前寻找 user message：

```python
target_pos = max(0, len(message_entries) - keep_last_messages)
while target_pos > 0:
    candidate = message_entries[target_pos][1]
    if candidate.payload.get("role") == "user":
        break
    target_pos -= 1
```

它只把切点之前的非 system `prefix_messages` 交给 summarizer，并把摘要、`first_kept_entry_id`、`tokens_before` 追加为 compaction Entry。原 message Entry 一条都不删。

重建时 `SessionTree.reconstruct_messages()` 只看 active branch 上最后一条 compaction，先从压缩前缀恢复 system prompt 与 Tool declaration baseline，再注入带 `compaction_entry_id` metadata 的 system summary，最后从 `first_kept_entry_id` 起恢复原消息。如果该 id 在 branch 中找不到，代码会回退到完整消息，而不是用损坏的摘要默默丢历史。

## 9. 掌握标准

你应该能回答：

- 为什么 Compaction 不应该删除旧 Session Entry？
- `first_kept_entry_id` 指向什么？
- 为什么 cut point 不能落在孤立 Tool Result 上？
- 为什么 Compaction 天然是 branch-local？
- Compaction 与 `transform_context` 的层次区别是什么？

下一章讨论另一类 Context 压力：领域说明很多，但并不是每次任务都需要全部读取。
