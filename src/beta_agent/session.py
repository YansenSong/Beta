from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .types import Message, ToolCall, utc_now_iso


@dataclass(slots=True)
class SessionEntry:
    id: str
    parent_id: str | None
    timestamp: str
    type: str
    payload: dict[str, Any]


class SessionTree:
    """Append-only branchable session history."""

    def __init__(self, entries: Iterable[SessionEntry] = (), leaf_id: str | None = None) -> None:
        self.entries = list(entries)
        self.by_id = {entry.id: entry for entry in self.entries}
        self.leaf_id = leaf_id if leaf_id is not None else (self.entries[-1].id if self.entries else None)

    def _append(self, type_: str, payload: dict[str, Any]) -> SessionEntry:
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
        return entry

    def append_message(self, message: Message) -> SessionEntry:
        return self._append("message", _message_to_dict(message))

    def append_compaction(self, *, summary: str, first_kept_entry_id: str, tokens_before: int) -> SessionEntry:
        return self._append(
            "compaction",
            {
                "summary": summary,
                "first_kept_entry_id": first_kept_entry_id,
                "tokens_before": tokens_before,
            },
        )

    def branch(self, entry_id: str | None) -> None:
        if entry_id is not None and entry_id not in self.by_id:
            raise KeyError(f"Unknown session entry: {entry_id}")
        self.leaf_id = entry_id

    def get_branch(self, from_id: str | None = None) -> list[SessionEntry]:
        current_id = self.leaf_id if from_id is None else from_id
        path: list[SessionEntry] = []
        while current_id:
            current = self.by_id[current_id]
            path.append(current)
            current_id = current.parent_id
        path.reverse()
        return path

    def reconstruct_messages(self, from_id: str | None = None) -> list[Message]:
        branch = self.get_branch(from_id)
        compactions = [entry for entry in branch if entry.type == "compaction"]
        if not compactions:
            return [_message_from_dict(e.payload) for e in branch if e.type == "message"]

        latest = compactions[-1]
        first_kept_id = latest.payload["first_kept_entry_id"]
        start = next((i for i, entry in enumerate(branch) if entry.id == first_kept_id), None)
        if start is None:
            return [_message_from_dict(e.payload) for e in branch if e.type == "message"]

        messages = [
            Message.system(
                f"Conversation summary:\n{latest.payload['summary']}",
                compaction_entry_id=latest.id,
            )
        ]
        messages.extend(_message_from_dict(e.payload) for e in branch[start:] if e.type == "message")
        return messages

    def save_jsonl(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for entry in self.entries:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            handle.write(json.dumps({"_meta": {"leaf_id": self.leaf_id}}, ensure_ascii=False) + "\n")

    @classmethod
    def load_jsonl(cls, path: str | Path) -> "SessionTree":
        entries: list[SessionEntry] = []
        leaf_id: str | None = None
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                data = json.loads(line)
                if "_meta" in data:
                    leaf_id = data["_meta"].get("leaf_id")
                else:
                    entries.append(SessionEntry(**data))
        return cls(entries, leaf_id)


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "stop_reason": message.stop_reason,
        "is_error": message.is_error,
        "metadata": message.metadata,
        "timestamp": message.timestamp,
    }


def _message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        role=data["role"],
        content=data.get("content", ""),
        tool_calls=[ToolCall(**call) for call in data.get("tool_calls", [])],
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        stop_reason=data.get("stop_reason"),
        is_error=data.get("is_error", False),
        metadata=data.get("metadata", {}),
        timestamp=data.get("timestamp", utc_now_iso()),
    )
