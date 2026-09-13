from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .tools import Tool, ToolExecutionContext
from .types import ToolResult


class ReadTextFileArgs(BaseModel):
    path: str = Field(description="Path to a UTF-8 text file under the configured root")


def make_read_text_file_tool(root: str | Path) -> Tool[ReadTextFileArgs]:
    root_path = Path(root).resolve()

    async def read_file(args: ReadTextFileArgs, ctx: ToolExecutionContext) -> ToolResult:
        candidate = Path(args.path)
        if not candidate.is_absolute():
            candidate = root_path / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError as exc:
            raise PermissionError("Path escapes the configured read root") from exc
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return ToolResult(content=candidate.read_text(encoding="utf-8"))

    return Tool(
        name="read_text_file",
        description="Read a UTF-8 text file from the configured project root.",
        args_model=ReadTextFileArgs,
        handler=read_file,
        execution_mode="parallel",
    )
