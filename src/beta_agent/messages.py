from __future__ import annotations

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
    """List of content blocks with a narrow string-compatibility surface.

    New code should use ``AgentMessage.text`` for text.  The compatibility
    methods let the pre-P0 examples continue to perform simple string checks
    while ``content`` is now genuinely a list of blocks.
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


def _content_blocks(value: str | AgentContent | list[AgentContent] | tuple[AgentContent, ...] | None) -> ContentBlocks:
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

    def __post_init__(self) -> None:
        self.content = _content_blocks(self.content)
        self.tool_calls = list(self.tool_calls)
        self.metadata = dict(self.metadata)

    @property
    def text(self) -> str:
        return self.content.text

    @classmethod
    def user(cls, text: str, **metadata: Any) -> "AgentMessage":
        return cls(role="user", content=[TextContent(text)], metadata=metadata)

    @classmethod
    def system(cls, text: str, **metadata: Any) -> "AgentMessage":
        return cls(role="system", content=[TextContent(text)], metadata=metadata)

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
        content: str | list[AgentContent],
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


# Backward compatibility alias. New runtime code should use AgentMessage.
Message = AgentMessage
