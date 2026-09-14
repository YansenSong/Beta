from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from beta_agent.tools import Tool, ToolExecutionContext
from beta_agent.types import ToolResult

from .path_utils import resolve_tool_path


class WriteFileArgs(BaseModel):
    path: str = Field(description="Path to write, relative to the workspace when not absolute")
    content: str = Field(description="Complete UTF-8 text content to write")


def create_write_file_tool(cwd: str | Path) -> Tool[WriteFileArgs]:
    workspace = Path(cwd).expanduser().resolve()

    async def write_file(args: WriteFileArgs, ctx: ToolExecutionContext) -> ToolResult:
        del ctx
        candidate = resolve_tool_path(workspace, args.path)
        encoded = args.content.encode("utf-8")
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(encoded)
        except OSError as exc:
            raise OSError(f"Unable to write file {args.path!r}: {exc}") from exc
        return ToolResult(
            content=f"Successfully wrote {len(encoded)} bytes to {args.path}",
            details={"path": str(candidate), "bytes_written": len(encoded)},
        )

    return Tool(
        name="write_file",
        description="Create or overwrite a UTF-8 text file, creating parent directories as needed.",
        args_model=WriteFileArgs,
        handler=write_file,
        execution_mode="sequential",
    )
