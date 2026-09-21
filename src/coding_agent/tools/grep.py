from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from beta_agent.harness.tool import Tool, ToolExecutionContext
from beta_agent.types import ToolResult
from .path_utils import resolve_tool_path

SKIP_DIRECTORIES = frozenset({".git", "node_modules", "dist", "build", "__pycache__", ".venv"})


class GrepArgs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    pattern: str = Field(description="Regular expression, or literal text when literal is true")
    path: str = Field(default=".", description="File or directory to search")
    glob: str | None = Field(default=None, description="Optional filename glob filter")
    ignore_case: bool = Field(default=False, alias="ignoreCase")
    literal: bool = False
    context: int = Field(default=0, ge=0, description="Number of context lines before and after each match")
    limit: int = Field(default=100, ge=1, description="Maximum number of matching lines to return")


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files: list[Path] = []
    for root, directories, filenames in os.walk(path, topdown=True, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES)
        for name in sorted(filenames):
            files.append(Path(root) / name)
    return files


def _matches_glob(file_path: Path, base: Path, pattern: str | None) -> bool:
    if pattern is None:
        return True
    relative = file_path.relative_to(base).as_posix() if file_path.is_relative_to(base) else file_path.name
    return any(
        (
            fnmatch.fnmatch(file_path.name, pattern),
            fnmatch.fnmatch(relative, pattern),
            pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:]),
            Path(relative).match(pattern),
        )
    )


def _display_path(file_path: Path, cwd: Path, user_path: str) -> str:
    if not Path(user_path).expanduser().is_absolute():
        return Path(os.path.relpath(file_path, cwd)).as_posix()
    return str(file_path)


def create_grep_tool(cwd: str | Path) -> Tool[GrepArgs]:
    workspace = Path(cwd).expanduser().resolve()

    async def grep(args: GrepArgs, ctx: ToolExecutionContext) -> ToolResult:
        del ctx
        candidate = resolve_tool_path(workspace, args.path)
        if not candidate.exists():
            raise FileNotFoundError(f"Search path does not exist: {args.path!r}")
        if not candidate.is_file() and not candidate.is_dir():
            raise ValueError(f"Search path is not a file or directory: {args.path!r}")

        flags = re.IGNORECASE if args.ignore_case else 0
        expression = re.escape(args.pattern) if args.literal else args.pattern
        try:
            matcher = re.compile(expression, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern {args.pattern!r}: {exc}") from exc

        base_for_glob = candidate if candidate.is_dir() else candidate.parent
        matches: list[tuple[Path, int, str, list[str]]] = []
        for file_path in _iter_files(candidate):
            if not _matches_glob(file_path, base_for_glob, args.glob):
                continue
            try:
                text = file_path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                # Grep 是 source-search Tool；binary/non-UTF-8 文件直接静默跳过，
                # 避免把损坏的 model context 返回出去。
                continue
            except OSError as exc:
                raise OSError(f"Unable to read search file {str(file_path)!r}: {exc}") from exc
            lines = text.splitlines()
            for line_number, line in enumerate(lines, start=1):
                if matcher.search(line):
                    matches.append((file_path, line_number, line, lines))

        selected = matches[: args.limit]
        if not selected:
            return ToolResult(content="No matches found", details={"match_count": 0})

        selected_lines_by_file: dict[Path, set[int]] = {}
        for file_path, line_number, _, _ in selected:
            selected_lines_by_file.setdefault(file_path, set()).add(line_number)

        output: list[str] = []
        emitted: set[tuple[Path, int]] = set()
        for file_path, line_number, _, lines in selected:
            start = max(1, line_number - args.context)
            end = min(len(lines), line_number + args.context)
            display = _display_path(file_path, workspace, args.path)
            for current in range(start, end + 1):
                key = (file_path, current)
                if key in emitted:
                    continue
                emitted.add(key)
                line = lines[current - 1]
                if current in selected_lines_by_file.get(file_path, set()):
                    output.append(f"{display}:{current}: {line}")
                else:
                    output.append(f"{display}-{current}- {line}")

        truncated = len(matches) > args.limit
        if truncated:
            output.append(f"[Results truncated at {args.limit} matching lines; increase limit to continue.]")
        details = {
            "match_count": len(matches),
            "output_matches": len(selected),
            "truncation": {"truncated": truncated, "limit": args.limit, "total_matches": len(matches)},
        }
        return ToolResult(content="\n".join(output), details=details)

    return Tool(
        name="grep",
        description="Search UTF-8 source files recursively with regex, literals, globs, and context lines.",
        args_model=GrepArgs,
        handler=grep,
        execution_mode="parallel",
        replay_policy="safe",
    )
