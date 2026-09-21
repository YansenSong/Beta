from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from ..session import SessionEntry, SessionTree
from ..types import AgentMessage, Message

Summarizer = Callable[[list[AgentMessage]], Awaitable[str] | str]


async def compact_session(
    session: SessionTree,
    *,
    summarize: Summarizer,
    keep_last_messages: int = 8,
    estimate_tokens: Callable[[list[AgentMessage]], int] | None = None,
) -> SessionEntry | None:
    """追加一条 branch-local compaction Entry，而不删除旧 history。

    retained tail 会尽量从 user message 开始，从而避免在对应的
    assistant/tool-call pair 已被摘要后，单独保留一个 tool result。
    """

    branch = session.get_branch()
    message_entries = [(i, e) for i, e in enumerate(branch) if e.type == "message"]
    if len(message_entries) <= keep_last_messages:
        return None

    # 先按“希望保留最近 N 条消息”计算候选切点。
    target_pos = max(0, len(message_entries) - keep_last_messages)

    # 切点不能只看数量，还要尽量落在安全的协议边界。
    # 从 user message 开始 retained tail，可以避免只保留 tool result、却把对应 tool call 摘要掉。
    while target_pos > 0:
        candidate = message_entries[target_pos][1]
        if candidate.payload.get("role") == "user":
            break
        target_pos -= 1

    first_kept_branch_index, first_kept = message_entries[target_pos]
    prefix_entries = [e for e in branch[:first_kept_branch_index] if e.type == "message"]
    if not prefix_entries:
        return None

    # 只摘要切点以前的旧消息。原 Session Entry 不会删除，摘要只是新的 append-only 记录。
    prefix_messages = [session_message(e) for e in prefix_entries]
    summary_messages = [message for message in prefix_messages if message.role != "system"]
    if not summary_messages:
        return None
    summary = summarize(summary_messages)
    if inspect.isawaitable(summary):
        summary = await summary

    # tokens_before 是压缩时的观测信息，不参与 Session Tree 的结构关系。
    # 未提供精确 tokenizer 时，用字符数做一个足够简单的近似估算。
    tokens_before = (
        estimate_tokens([session_message(e) for _, e in message_entries])
        if estimate_tokens
        else sum(max(1, len(session_message(e).text) // 4) for _, e in message_entries)
    )
    return session.append_compaction(
        summary=str(summary),
        first_kept_entry_id=first_kept.id,
        tokens_before=tokens_before,
    )


def session_message(entry: SessionEntry) -> AgentMessage:
    from ..session import _message_from_dict

    return _message_from_dict(entry.payload)
