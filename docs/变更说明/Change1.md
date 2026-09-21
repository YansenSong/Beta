| 能力 | 原本 | 变更后 | 实际意义 |
|---|---|---|---|
| **System Prompt / Tool 状态进入 Transcript** | `system_prompt` 和 runtime tools 更多属于 Agent 当前内存状态 | 新增 `transcript.py`，System instruction、Tool declaration 的增加/删除都变成可重放的 transcript state | Session 恢复后不只是恢复聊天内容，也能知道“当时模型看到什么 system prompt、有哪些工具” |
| **Tool declaration 与可执行 Tool 分离** | 工具主要作为 Python runtime object 存在 | transcript 保存纯 `ToolDeclaration`，实际 Python Tool 仍放在 registry/context 中 | 持久化层不需要序列化 Python 函数，对重放和跨进程恢复更友好 |
| **Session v3** | Session 主要保存 message history / branch / compaction | 增加 system/tool baseline、tool state delta、rich Tool Result、legacy session migration | Session 更接近完整的 Agent 状态日志，而不只是聊天记录 |
| **Compaction 更可靠** | 摘要旧消息后重点恢复 conversation tail | 压缩后会重建当时有效的 system/tool baseline，再拼 summary + retained tail | 长会话压缩后不会突然“忘记原来的工具和系统指令” |
| **Steering / Follow-up QueueMode** | 队列处理策略相对固定 | 增加 `one-at-a-time` / `all`，默认逐条消费 | 可以控制用户中途追加消息是逐条进入 Agent，还是一次性 drain |
| **`prepare_next_turn` 大幅增强** | 主要调整下一轮 Context | 新增 `NextTurnUpdate`，一次可以修改 `context`、追加 `messages`、切换 `model`、修改 provider request options | Hook 开始真正具备“控制下一次模型请求”的能力 |
| **Tool Result 更丰富** | 主要是字符串结果 | `ToolResult` 支持 text/image content blocks、`details`、`usage`、`terminate` 等 | Tool 可以返回图片、多模态结果、usage 数据，不再只是字符串 |
| **Tool Progress** | Tool 通常只有开始/结束 | `ToolExecutionContext.progress()` 可以发送 `tool_execution_update` | 长时间运行的 bash/search/tool 可以实时报告进度，适合 UI/Tracing |
| **Awaited Event Subscribers** | Extension / persistence 更依赖外层 EventStream 消费 | Agent 增加 `subscribe()`，subscriber 会被真正 `await` | `message_end` 持久化等操作可以保证在 run settlement 前完成，不容易因为异步调度丢状态 |
| **Provider Request Policy** | Provider 配置较简单 | 新增 timeout、retry、headers、metadata、transport、reasoning、thinking budget 等配置 | Runtime 与不同 Provider 的调用策略解耦得更干净 |
| **Durable Storage** | Agent 一旦进程崩掉，中途 Tool 状态基本只能靠外部自己处理 | 新增 Memory / SQLite durable backend、Task、ToolOperation、Outbox 等记录 | 开始具备“崩溃后知道执行到哪里”的基础设施 |
| **Tool Intent → Effect → Outcome** | Tool 直接执行然后写结果 | 执行外部副作用前先持久化 intent，执行后把 outcome + outbox 同事务写入 | 降低“工具执行了，但结果没写进 Session”这种不一致问题 |
| **Recovery** | 没有系统性的 Tool recovery | `safe` Tool 可以恢复时重放，`unsafe` Tool 不自动重放，而是写明确 interrupted result | 避免崩溃后偷偷重复执行 `bash/edit` 这类有风险副作用 |
| **原子 `write_file`** | 普通文件写入 | 临时文件 → flush/fsync → `os.replace()`，并标为 `safe` replay policy | 崩溃恢复时重复写相同完整内容更容易收敛，不容易留下半文件 |