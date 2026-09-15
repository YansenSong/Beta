from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from beta_agent.tools import Tool, ToolExecutionContext
from beta_agent.types import ToolResult
from .path_utils import resolve_tool_path

READ_MAX_LINES = 500
READ_MAX_BYTES = 64 * 1024


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path to a UTF-8 text file, relative to the workspace when not absolute")
    offset: int | None = Field(default=None, ge=1, description="1-based line number to start reading from")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to return")


def _truncate_utf8_prefix(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def create_read_file_tool(cwd: str | Path) -> Tool[ReadFileArgs]:
    workspace = Path(cwd).expanduser().resolve()

    async def read_file(args: ReadFileArgs, ctx: ToolExecutionContext) -> ToolResult:
        del ctx
        candidate = resolve_tool_path(workspace, args.path)
        if not candidate.is_file():
            raise FileNotFoundError(f"File not found or is not a regular file: {args.path!r}")

        try:
            text = candidate.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnicodeError(f"File is not valid UTF-8 text: {args.path!r}") from exc
        except OSError as exc:
            raise OSError(f"Unable to read file {args.path!r}: {exc}") from exc

        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        if total_lines == 0 and args.offset is None:
            return ToolResult(
                content="",
                details={
                    "truncation": {
                        "truncated": False,
                        "output_lines": 0,
                        "total_lines": 0,
                        "output_bytes": 0,
                    }
                },
            )
        offset = args.offset or 1
        if offset > total_lines:
            raise ValueError(
                f"Offset {offset} is beyond the end of file {args.path!r} ({total_lines} lines)"
            )

        requested_limit = args.limit if args.limit is not None else READ_MAX_LINES
        line_limit = min(requested_limit, READ_MAX_LINES)
        start_index = offset - 1
        selected_lines = lines[start_index : start_index + line_limit]
        selected_end = offset + len(selected_lines) - 1
        selected_text = "".join(selected_lines)

        line_truncated = offset + line_limit - 1 < total_lines
        byte_truncated = len(selected_text.encode("utf-8")) > READ_MAX_BYTES
        truncated = line_truncated or byte_truncated

        display_text = _truncate_utf8_prefix(selected_text, READ_MAX_BYTES)
        if byte_truncated:
            # byte limit 可能会从超长 line 的中间截断；这种情况下继续使用相同 offset，
            # 是最不容易让人意外的 continuation instruction。
            continuation_offset = offset
            shown_line_count = len(display_text.splitlines())
            if shown_line_count:
                selected_end = min(total_lines, offset + shown_line_count - 1)
        else:
            continuation_offset = selected_end + 1

        if truncated:
            # continuation hint 也必须放在 byte budget 内；这个短 loop 还能处理一种边界情况：
            # selected text 本身略低于限制，但加上 hint 后会超限。
            for _ in range(3):
                separator = "" if not display_text or display_text.endswith(("\n", "\r")) else "\n"
                hint = (
                    f"[显示第 {offset}-{selected_end} 行，共 {total_lines} 行。"
                    f"用 offset={continuation_offset} 继续。]"
                )
                if len((display_text + separator + hint).encode("utf-8")) <= READ_MAX_BYTES:
                    output_text = display_text + separator + hint
                    break
                overhead = len((separator + hint).encode("utf-8"))
                display_text = _truncate_utf8_prefix(selected_text, max(0, READ_MAX_BYTES - overhead))
                shown_line_count = len(display_text.splitlines())
                if shown_line_count:
                    selected_end = min(total_lines, offset + shown_line_count - 1)
                continuation_offset = offset if byte_truncated else selected_end + 1
                byte_truncated = True
            else:
                output_text = display_text
        else:
            output_text = display_text

        details = {
            "truncation": {
                "truncated": truncated,
                "output_lines": len(display_text.splitlines()),
                "total_lines": total_lines,
                "output_bytes": len(output_text.encode("utf-8")),
            }
        }

        return ToolResult(content=output_text, details=details)

    return Tool(
        name="read_file",
        description="Read a UTF-8 text file by line range from the workspace path.",
        args_model=ReadFileArgs,
        handler=read_file,
        execution_mode="parallel",
    )
