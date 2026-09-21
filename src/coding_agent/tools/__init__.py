from pathlib import Path
from typing import Any

from beta_agent.harness.tool import Tool

from .bash import BASH_MAX_BYTES, BashArgs, create_bash_tool
from .edit import EditArgs, EditItem, create_edit_tool, normalize_edit_arguments, prepare_edit_arguments
from .grep import GrepArgs, SKIP_DIRECTORIES, create_grep_tool
from .path_utils import resolve_tool_path
from .read_file import READ_MAX_BYTES, READ_MAX_LINES, ReadFileArgs, create_read_file_tool
from .write_file import WriteFileArgs, create_write_file_tool


def create_coding_tools(cwd: str | Path) -> list[Tool[Any]]:
    """为 workspace 创建稳定的 Chapter 12 Coding Tool set。"""

    return [
        create_read_file_tool(cwd),
        create_write_file_tool(cwd),
        create_edit_tool(cwd),
        create_grep_tool(cwd),
        create_bash_tool(cwd),
    ]


__all__ = [
    "BASH_MAX_BYTES",
    "BashArgs",
    "EditArgs",
    "EditItem",
    "GrepArgs",
    "READ_MAX_BYTES",
    "READ_MAX_LINES",
    "ReadFileArgs",
    "SKIP_DIRECTORIES",
    "WriteFileArgs",
    "create_bash_tool",
    "create_coding_tools",
    "create_edit_tool",
    "create_grep_tool",
    "create_read_file_tool",
    "create_write_file_tool",
    "normalize_edit_arguments",
    "prepare_edit_arguments",
    "resolve_tool_path",
]
