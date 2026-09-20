from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool", "custom"]
StopReason = Literal["stop", "tool_calls", "length", "error", "aborted"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolDeclaration:
    """可放入 transcript/session 的模型可见工具定义，不含可执行 runtime state。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", deepcopy(self.parameters))


@dataclass(slots=True, frozen=True)
class ToolReference:
    name: str


@dataclass(slots=True)
class TextContent:
    text: str = ""
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ImageContent:
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None


AgentContent = TextContent | ImageContent


class ContentBlocks(list[AgentContent]):
    """带有限 string compatibility surface 的 content block list。

    新代码应通过 ``AgentMessage.text`` 读取文本。这里保留这些 compatibility
    method，是为了让 P0 之前的示例在 ``content`` 已真正变成 block list 后，
    仍能继续进行简单的 string check。
    """

    @property
    def text(self) -> str:
        return "".join(block.text for block in self if isinstance(block, TextContent))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.text == other
        return list.__eq__(self, other)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return item in self.text
        return list.__contains__(self, item)

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text) or list.__len__(self) > 0

    def lower(self) -> str:
        return self.text.lower()

    def upper(self) -> str:
        return self.text.upper()

    def strip(self, chars: str | None = None) -> str:
        return self.text.strip(chars)

    def startswith(self, prefix: str | tuple[str, ...], *args: Any) -> bool:
        return self.text.startswith(prefix, *args)

    def endswith(self, suffix: str | tuple[str, ...], *args: Any) -> bool:
        return self.text.endswith(suffix, *args)

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        return self.text.encode(encoding, errors)

    def splitlines(self, *args: Any, **kwargs: Any) -> list[str]:
        return self.text.splitlines(*args, **kwargs)


def normalize_content_blocks(
    value: str | AgentContent | Sequence[AgentContent] | None,
) -> ContentBlocks:
    if value is None:
        return ContentBlocks()
    if isinstance(value, str):
        return ContentBlocks([TextContent(value)])
    if isinstance(value, (TextContent, ImageContent)):
        return ContentBlocks([value])
    blocks: list[AgentContent] = []
    for block in value:
        if isinstance(block, str) and not isinstance(block, TextContent):
            blocks.append(TextContent(block))
        elif isinstance(block, (TextContent, ImageContent)):
            blocks.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "text":
                blocks.append(TextContent(str(block.get("text", ""))))
            elif block_type == "image":
                blocks.append(ImageContent(**{key: block[key] for key in ("url", "data", "media_type") if key in block}))
            else:
                raise ValueError(f"Unsupported content block type: {block_type!r}")
        else:
            raise TypeError(f"Unsupported content block: {block!r}")
    return ContentBlocks(blocks)


@dataclass(slots=True)
class AgentMessage:
    role: Role
    content: ContentBlocks = field(default_factory=ContentBlocks)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    stop_reason: StopReason | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)
    tools_added: list[ToolDeclaration] = field(default_factory=list)
    tools_removed: list[ToolReference] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.content = normalize_content_blocks(self.content)
        self.tool_calls = list(self.tool_calls)
        self.metadata = dict(self.metadata)
        self.tools_added = list(self.tools_added)
        self.tools_removed = list(self.tools_removed)

    @property
    def text(self) -> str:
        return self.content.text

    @classmethod
    def user(cls, text: str, **metadata: Any) -> "AgentMessage":
        return cls(role="user", content=[TextContent(text)], metadata=metadata)

    @classmethod
    def system(
        cls,
        text: str,
        *,
        tools_added: Sequence[ToolDeclaration] = (),
        tools_removed: Sequence[ToolReference] = (),
        **metadata: Any,
    ) -> "AgentMessage":
        return cls(
            role="system",
            content=[TextContent(text)],
            metadata=metadata,
            tools_added=list(tools_added),
            tools_removed=list(tools_removed),
        )

    @classmethod
    def assistant(
        cls,
        text: str = "",
        *,
        content: list[AgentContent] | None = None,
        tool_calls: list[ToolCall] | None = None,
        stop_reason: StopReason = "stop",
        **metadata: Any,
    ) -> "AgentMessage":
        return cls(
            role="assistant",
            content=content if content is not None else [TextContent(text)],
            tool_calls=list(tool_calls or []),
            stop_reason=stop_reason,
            metadata=metadata,
        )

    @classmethod
    def tool_result(
        cls,
        *,
        tool_call_id: str,
        name: str,
        content: str | AgentContent | Sequence[AgentContent],
        is_error: bool = False,
        **metadata: Any,
    ) -> "AgentMessage":
        return cls(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            is_error=is_error,
            metadata=metadata,
        )

    def copy(self, **changes: Any) -> "AgentMessage":
        return replace(self, **changes)


# 向后兼容 alias；新的 Runtime code 应使用 AgentMessage。
Message = AgentMessage
