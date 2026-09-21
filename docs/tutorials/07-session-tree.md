# 07：Session Tree——历史为什么要从数组升级成树

> 参考：[`learn-pi-agent` Chapter 07](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/07-session-tree)。

## 本章目标

理解为什么长期会话、回退和分支不适合只用一条 `messages[]` 表示，以及为什么 Session 应该在 Agent Loop 外部。

## 1. 线性 history 的极限

普通对话看起来是：

```text
U1 → A1 → U2 → A2 → U3 → A3
```

如果用户想回到 `A1`，从那里重新提问：

```text
U1 → A1 → U2'
```

最简单的做法是截断旧数组，但这样旧路径：

```text
U2 → A2 → U3 → A3
```

会被删除。

复制数组也能保留旧历史，但你又需要额外记录：

- 两条 history 从哪里分叉；
- 当前正在使用哪条；
- 怎样切回旧路径。

于是问题已经变成“历史结构”，而不是“当前消息列表”。

## 2. Beta 的 `SessionEntry`

Beta 在 [`../../src/beta_agent/harness/session.py`](../../src/beta_agent/harness/session.py) 中用 append-only Entry 表示历史。

每条 Entry 具有：

```text
id
parent_id
timestamp
type
payload
```

新增 Entry 时，它的 `parent_id` 指向当前 `leaf_id`，然后自己成为新的 leaf。

如果一直顺序追加，看起来仍然是一条链。

## 3. Branch 的本质只是移动 leaf

假设已有：

```text
U1 → A1 → U2 → A2
```

把 leaf 移到 `A1`，再写入 `U2' / A2'`：

```text
U1 → A1
      ├── U2  → A2
      └── U2' → A2'
```

旧 Entry 不需要删除，也不需要修改。

Beta 的 `branch(entry_id)` 做的核心动作就是改变：

```text
下一条 Entry 应该接到谁后面
```

这种 append-only 语义非常适合持久化、审计和恢复。

## 4. Agent 仍然只消费线性 messages

Session 内部是一棵树，不代表 Agent Loop 也要理解树。

当前 leaf 在 `A2'` 时，Session 通过 `get_branch()` 沿 `parent_id` 回溯，再反转得到：

```text
U1 → A1 → U2' → A2'
```

然后 `reconstruct_messages()` 把当前 active branch 投影成普通 `Message[]`。

所以职责边界是：

```text
SessionTree
→ 决定当前历史路径

Agent
→ 处理线性的当前 Context
```

## 5. 为什么 Session 不应该塞进 Agent Loop

如果 Agent Loop 同时负责：

- leaf；
- branch；
- 回退；
- 持久化；
- 历史恢复；

那么一次 run 的执行语义会和长期历史管理绑死。

更干净的流程是：

```text
load Session
    ↓
reconstruct active branch
    ↓
Agent.replace_messages(...)
    ↓
run Agent
    ↓
把新增消息追加回 Session
```

Beta 当前也保持这层分离：`SessionTree` 是独立组件，没有硬塞进 `Agent._run()`。

## 6. JSONL 为什么合适

Append-only Entry 很适合 JSONL：

```text
entry
entry
entry
...
```

每条记录保存自己的 `id` 和 `parent_id`，不需要把 children 嵌套进父对象。

Beta 的 `save_jsonl()` / `load_jsonl()` 就是一个最小持久化实现。

当前 Session format v3 除普通对话外，也保存 system message 的 `tools_added` / `tools_removed` delta、rich Tool Result content 和新增 metadata。初始 system/tool baseline 与后续变化因此可以沿 branch replay，而不是从当前进程配置猜测历史状态。

旧 v1/v2 文件仍可加载。Coding Agent 恢复到没有 transcript state 的 legacy leaf 时，会在当前 leaf 追加由当前 system prompt/tool registry 构成的 migration baseline；旧 entries 与 ids 不会被改写。若旧 branch 已有 compaction，重建的工作 transcript 会把 baseline 排在 synthetic summary 之前，同时 Session 仍保持 append-only。迁移点之后的新历史即可自包含。

## 7. Session 与 Context Transformation 的先后

两层不要混淆：

```text
Session Tree
    ↓
选择 active branch
    ↓
canonical Message[]
    ↓
transform_context
    ↓
这一轮 LLM input
```

Session 回答“我们当前在哪条历史路径上”。

Context Transformation 回答“这条路径里本轮模型看哪些内容”。

### 从 Entry 指针重建 active branch

`SessionTree._append()` 从不寻找或修改 children，只把当前 `leaf_id` 写成新节点的 `parent_id`，然后移动 leaf：

```python
entry = SessionEntry(
    id=uuid.uuid4().hex,
    parent_id=self.leaf_id,
    timestamp=utc_now_iso(),
    type=type_,
    payload=payload,
)
self.entries.append(entry)
self.by_id[entry.id] = entry
self.leaf_id = entry.id
```

`branch(entry_id)` 也只校验 id 后移动 `leaf_id`。`get_branch()` 再从 leaf 沿 `parent_id` 向上查 `by_id`，最后 `reverse()` 得到模型需要的时间正序。于是 `entries` 是完整 append-only 存储，`leaf_id` 是当前视角，`get_branch()` 的返回值才是线性 active path。

持久化时每个 Entry 占一行 JSON，最后另写一条 `_meta` 保存 `leaf_id` 和格式版本。加载 v1/v2 消息时 `_content_from_json()` 还兼容旧的纯字符串 content，这说明 Session 的磁盘 schema 也是需要维护的边界。

## 8. 掌握标准

你应该能回答：

- 为什么 branch 不应该通过删除未来消息来实现？
- `leaf_id` 的意义是什么？
- 为什么 Session 是树，但 Agent 仍然只需要线性 Message？
- Session 与 Agent Loop 为什么应该解耦？
- Session branch 与 `transform_context` 分别解决什么问题？

下一章继续解决 active branch 自身越来越长的问题。
