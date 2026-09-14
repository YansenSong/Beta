# 00：先建立模型边界，而不是先写 Agent

> 参考：[`learn-pi-agent` Chapter 00](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/00-minimal-llm-call)。本文以 Beta 当前实现为落点，不是原文翻译。

## 本章目标

理解为什么一个 Agent Framework 不应该把 OpenAI、Anthropic 等 Provider 的消息格式直接当成自己的 Runtime 数据结构。

## 1. 普通聊天时，问题还不明显

最简单的聊天程序只有：

```text
messages
   ↓
Provider API
   ↓
assistant response
```

如果只有 user / assistant 文本，直接保存 Provider SDK 的 Message 看起来非常自然。

真正的架构压力通常从 Tool Calling 开始。

不同 Provider 对“模型请求工具”和“工具结果”的协议表达可能完全不同。此时如果 Runtime 直接保存 Provider Message，那么 Tool Runtime、Session、Context 处理等后续模块都会被迫理解 Provider 细节。

于是一个原本应该局限在模型接入层的差异，会扩散到整个框架。

## 2. Beta 的选择：Runtime 使用自己的 AgentMessage

Beta 在 [`../../src/beta_agent/messages.py`](../../src/beta_agent/messages.py) 中定义了自己的 Runtime 消息：

- `AgentMessage`
- `ToolCall`
- `ToolResult`

`Message` 仍然是 `AgentMessage` 的兼容 alias。消息内容现在以 text/image content blocks 表达，文本读取使用 `message.text`。

Agent Loop 关心的是：

```text
assistant 请求了哪个 tool
参数是什么
tool 最后返回了什么
是否失败
```

而不是 Provider 把这些信息叫做哪个字段。

因此框架内部可以一直使用统一语义：

```text
Agent Message
     ↓
`convert_to_llm`
     ↓
ProviderMessage boundary
     ↓
Provider protocol
```

## 3. Adapter 是翻译层

[`../../src/beta_agent/model.py`](../../src/beta_agent/model.py) 定义 `ModelAdapter` 协议，具体 Provider 实现在 [`../../src/beta_agent/adapters/`](../../src/beta_agent/adapters/) 中。

[`../../src/beta_agent/provider_messages.py`](../../src/beta_agent/provider_messages.py) 的 `default_convert_to_llm()` 负责把 Runtime 消息转换成不含 runtime-only metadata 的 `ProviderMessage`。

目前的 OpenAI-compatible Adapter 负责：

- 接收 `ProviderMessage`，再转换成 Provider payload；
- 把 Beta Tool schema 转成 Provider Tool schema；
- 把流式 Provider 响应重新组装成内部 `AgentMessage` / `ToolCall`；
- 把 Provider 的 stop reason 映射回 Runtime 能理解的状态。

核心原则可以记成一句话：

> Provider-specific protocol 到模型调用边界为止。

## 4. 为什么不是一开始就抽象所有东西

好的框架抽象通常不是“为了未来可能支持十家模型”而提前设计，而是当 Provider 差异开始污染 Runtime 时，再建立稳定边界。

Tool Calling 正是这个分界点。

## 5. 对照 Beta 阅读

建议依次看：

1. `messages.py`：内部 AgentMessage 和 content blocks 长什么样；
2. `provider_messages.py`：Runtime 到 Provider 的显式转换边界；
3. `model.py`：Agent Loop 依赖什么接口；
4. `adapters/openai_compatible.py`：Provider 差异在哪里结束。

阅读时注意：`Agent` 本身不应该出现大量 Provider 字段名。

## 6. 掌握标准

你应该能回答：

- 为什么直接把 OpenAI Message 当 Session Message 会增加耦合？
- `AgentMessage` 和 Provider Message 为什么不是同一个概念？
- 新增 Anthropic Adapter 时，哪些模块原则上不应该修改？
- Provider 转换为什么适合放在 LLM call boundary？

下一章开始加入 Tool Result。此时一次 LLM 调用会自然变成一个循环。
