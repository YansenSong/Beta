from __future__ import annotations

import difflib
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from beta_agent.tools import Tool, ToolExecutionContext
from beta_agent.types import ToolResult
from .path_utils import resolve_tool_path


class EditItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    old_text: str = Field(alias="oldText", description="Exact text to replace")
    new_text: str = Field(alias="newText", description="Replacement text")


class EditArgs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(description="Path to edit, relative to the workspace when not absolute")
    edits: list[EditItem] = Field(description="Exact, unique replacements to apply atomically")


def prepare_edit_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    """标准化 Chapter 12/tutorial-era model 产生的参数 shape。

    这个函数只负责调整 JSON shape；matching 与 file validation 仍保留在 Tool handler 中，
    从而确保它们基于同一个原始 file snapshot 执行。
    """

    normalized = dict(raw)
    if "edits" in normalized:
        edits = normalized["edits"]
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in edits: {exc}") from exc
        if isinstance(edits, dict):
            edits = [edits]
        normalized["edits"] = edits
        return normalized

    old_key = "oldText" if "oldText" in normalized else "old_text"
    new_key = "newText" if "newText" in normalized else "new_text"
    if old_key in normalized or new_key in normalized:
        normalized["edits"] = [
            {
                "oldText": normalized.pop(old_key, None),
                "newText": normalized.pop(new_key, None),
            }
        ]
    return normalized


# 描述性 alias，方便调用方直接测试 normalization seam；
# Tool 自身仍使用规范名称 ``prepare_edit_arguments``。
normalize_edit_arguments = prepare_edit_arguments


def _find_all(haystack: bytes, needle: bytes) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position < 0:
            return positions
        positions.append(position)
        # 每次只前进一个 byte，这样重叠 match（例如 ``aaa`` 中的 ``aa``）
        # 也会被视为 duplicate。
        start = position + 1


def _first_changed_line(original: bytes, first_offset: int) -> int:
    return original[:first_offset].count(b"\n") + 1


def _unified_diff(path: Path, original: str, updated: str) -> str:
    lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
        lineterm="\n",
    )
    return "".join(lines)


def create_edit_tool(cwd: str | Path) -> Tool[EditArgs]:
    workspace = Path(cwd).expanduser().resolve()

    async def edit_file(args: EditArgs, ctx: ToolExecutionContext) -> ToolResult:
        del ctx
        candidate = resolve_tool_path(workspace, args.path)
        if not candidate.is_file():
            raise FileNotFoundError(f"File not found or is not a regular file: {args.path!r}")
        try:
            original_bytes = candidate.read_bytes()
            original_text = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnicodeError(f"File is not valid UTF-8 text: {args.path!r}") from exc
        except OSError as exc:
            raise OSError(f"Unable to read file {args.path!r}: {exc}") from exc

        replacements: list[tuple[int, int, EditItem]] = []
        for index, item in enumerate(args.edits, start=1):
            if item.old_text == "":
                raise ValueError(f"Edit {index} has an empty oldText; refusing an ambiguous insertion")
            needle = item.old_text.encode("utf-8")
            positions = _find_all(original_bytes, needle)
            if not positions:
                raise ValueError(f"Edit {index} oldText was not found in {args.path!r}")
            if len(positions) > 1:
                raise ValueError(
                    f"Edit {index} oldText must match exactly once in {args.path!r}; found {len(positions)} matches"
                )
            start = positions[0]
            replacements.append((start, start + len(needle), item))

        for left, right in pairwise(sorted(replacements, key=lambda item: (item[0], item[1]))):
            if right[0] < left[1]:
                raise ValueError(f"Edit ranges overlap in {args.path!r}")

        updated_bytes = original_bytes
        # 从末尾开始应用修改，可以让每个 range 都继续锚定在原始 snapshot 上，
        # 并从 Tool 的视角保证 write 是 atomic 的。
        for start, end, item in sorted(replacements, key=lambda entry: entry[0], reverse=True):
            updated_bytes = updated_bytes[:start] + item.new_text.encode("utf-8") + updated_bytes[end:]

        try:
            candidate.write_bytes(updated_bytes)
        except OSError as exc:
            raise OSError(f"Unable to write file {args.path!r}: {exc}") from exc

        updated_text = updated_bytes.decode("utf-8")
        first_offset = min((start for start, _, _ in replacements), default=0)
        diff = _unified_diff(candidate, original_text, updated_text)
        details = {
            "diff": diff,
            "patch": diff,
            "first_changed_line": _first_changed_line(original_bytes, first_offset),
            "replacements": len(replacements),
            "path": str(candidate),
        }
        return ToolResult(
            content=f"Successfully applied {len(replacements)} edit(s) to {args.path}",
            details=details,
        )

    return Tool(
        name="edit",
        description="Apply exact unique text replacements atomically to a UTF-8 text file.",
        args_model=EditArgs,
        handler=edit_file,
        execution_mode="sequential",
        prepare_arguments=prepare_edit_arguments,
        replay_policy="unsafe",
    )
