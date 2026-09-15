from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from .messages import AgentMessage, ImageContent, TextContent, ToolCall

ProviderRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ProviderTextContent:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(slots=True)
class ProviderImageContent:
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None


ProviderContent = ProviderTextContent | ProviderImageContent


@dataclass(slots=True)
class ProviderMessage:
    role: ProviderRole
    content: list[ProviderContent] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, ProviderTextContent))


def _convert_content(message: AgentMessage) -> list[ProviderContent]:
    converted: list[ProviderContent] = []
    for block in message.content:
        if isinstance(block, TextContent):
            converted.append(ProviderTextContent(text=block.text))
        elif isinstance(block, ImageContent):
            converted.append(
                ProviderImageContent(
                    url=block.url,
                    data=block.data,
                    media_type=block.media_type,
                )
            )
    return converted


def default_convert_to_llm(messages: Sequence[AgentMessage]) -> list[ProviderMessage]:
    """移除仅供 runtime 使用的字段，并将 AgentMessage 转换为 provider DTO。"""

    converted: list[ProviderMessage] = []
    for message in messages:
        if message.role not in {"system", "user", "assistant", "tool"}:
            # 默认情况下，runtime custom message 不会暴露给 provider。
            # 如果产品确实需要，可以显式提供 converter。
            continue
        converted.append(
            ProviderMessage(
                role=message.role,
                content=_convert_content(message),
                tool_calls=list(message.tool_calls),
                tool_call_id=message.tool_call_id,
                name=message.name,
            )
        )
    return converted


__all__ = [
    "ProviderContent",
    "ProviderImageContent",
    "ProviderMessage",
    "ProviderRole",
    "ProviderTextContent",
    "default_convert_to_llm",
]

