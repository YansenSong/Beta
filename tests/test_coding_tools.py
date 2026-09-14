from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from beta_agent import AgentContext, Message, ToolCall, ToolExecutionContext, ToolRuntime
from coding_agent.tools import (
    BASH_MAX_BYTES,
    READ_MAX_BYTES,
    READ_MAX_LINES,
    BashArgs,
    EditArgs,
    GrepArgs,
    ReadFileArgs,
    WriteFileArgs,
    create_bash_tool,
    create_edit_tool,
    create_grep_tool,
    create_read_file_tool,
    create_write_file_tool,
    prepare_edit_arguments,
    resolve_tool_path,
)


async def _noop_emit(_event) -> None:
    return None


async def _execute(tool, arguments: dict):
    runtime = ToolRuntime()
    batch = await runtime.execute_batch(
        context=AgentContext(system_prompt="", tools=[tool]),
        calls=[ToolCall(id="test-call", name=tool.name, arguments=arguments)],
        emit=_noop_emit,
    )
    return batch.messages[0]


async def _execute_direct(tool, args):
    return await tool.execute(args, ToolExecutionContext("direct", tool.name, _noop_emit))


def test_resolve_tool_path_uses_cwd_without_sandboxing(tmp_path: Path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    assert resolve_tool_path(cwd, "src/../README.md") == (cwd / "README.md").resolve()
    assert resolve_tool_path(cwd, str(outside)) == outside.resolve()
    assert resolve_tool_path(cwd, "../outside.txt") == outside.resolve()


@pytest.mark.asyncio
async def test_read_file_full_offset_limit_and_trailing_newline(tmp_path: Path):
    path = tmp_path / "lines.txt"
    path.write_bytes("one\r\ntwo\r\nthree\r\n".encode("utf-8"))
    tool = create_read_file_tool(tmp_path)

    full = await _execute_direct(tool, ReadFileArgs(path="lines.txt"))
    assert full.content == "one\r\ntwo\r\nthree\r\n"

    partial = await _execute_direct(tool, ReadFileArgs(path="lines.txt", offset=2, limit=1))
    assert partial.content.startswith("two\r\n")
    assert partial.details["truncation"]["truncated"] is True
    assert "offset=3" in partial.content


@pytest.mark.asyncio
async def test_read_file_truncates_lines_and_utf8_bytes(tmp_path: Path):
    many = tmp_path / "many.txt"
    many.write_text("".join(f"line-{i}\n" for i in range(1, READ_MAX_LINES + 20)), encoding="utf-8")
    tool = create_read_file_tool(tmp_path)
    result = await _execute_direct(tool, ReadFileArgs(path="many.txt"))
    assert result.details["truncation"]["truncated"] is True
    assert result.details["truncation"]["total_lines"] == READ_MAX_LINES + 19
    assert "offset=501" in result.content

    large = tmp_path / "large.txt"
    large.write_text("你好" * (READ_MAX_BYTES // 3), encoding="utf-8")
    result = await _execute_direct(tool, ReadFileArgs(path="large.txt"))
    assert result.details["truncation"]["truncated"] is True
    assert result.content.encode("utf-8")
    assert "offset=1" in result.content


@pytest.mark.asyncio
async def test_read_file_errors_are_model_visible(tmp_path: Path):
    tool = create_read_file_tool(tmp_path)
    missing = await _execute(tool, {"path": "missing.txt"})
    assert missing.is_error and "missing.txt" in missing.content
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert (await _execute_direct(tool, ReadFileArgs(path="empty.txt"))).content == ""
    assert (await _execute(tool, {"path": "empty.txt", "offset": 1})).is_error
    path = tmp_path / "one.txt"
    path.write_text("one\n", encoding="utf-8")
    beyond = await _execute(tool, {"path": "one.txt", "offset": 2})
    assert beyond.is_error and "offset" in beyond.content.lower()


@pytest.mark.asyncio
async def test_write_file_creates_parent_and_reports_utf8_bytes(tmp_path: Path):
    tool = create_write_file_tool(tmp_path)
    result = await _execute_direct(tool, WriteFileArgs(path="nested/file.txt", content="你好"))
    target = tmp_path / "nested/file.txt"
    assert target.read_bytes() == "你好".encode("utf-8")
    assert "6 bytes" in result.content
    assert result.details["bytes_written"] == 6
    assert tool.execution_mode == "sequential"

    replaced = await _execute_direct(tool, WriteFileArgs(path="nested/file.txt", content="new"))
    assert replaced.details["bytes_written"] == 3
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_edit_supports_normalized_shapes_and_atomic_original_matching(tmp_path: Path):
    path = tmp_path / "source.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = create_edit_tool(tmp_path)

    assert prepare_edit_arguments({"path": "source.txt", "edits": {"oldText": "alpha", "newText": "A"}})[
        "edits"
    ] == [{"oldText": "alpha", "newText": "A"}]
    assert prepare_edit_arguments({"path": "source.txt", "edits": '[{"oldText":"alpha","newText":"A"}]'})[
        "edits"
    ][0]["oldText"] == "alpha"
    assert prepare_edit_arguments({"path": "source.txt", "oldText": "alpha", "newText": "A"})[
        "edits"
    ][0]["newText"] == "A"

    result = await _execute_direct(
        tool,
        EditArgs(
            path="source.txt",
            edits=[
                {"oldText": "alpha", "newText": "alpha beta"},
                {"oldText": "beta", "newText": "BETA"},
            ],
        ),
    )
    assert path.read_text(encoding="utf-8") == "alpha beta\nBETA\n"
    assert result.details["first_changed_line"] == 1
    assert "---" in result.details["diff"] and "+++" in result.details["diff"]
    assert tool.execution_mode == "sequential"


@pytest.mark.asyncio
async def test_edit_rejects_missing_duplicate_empty_and_overlap_without_partial_write(tmp_path: Path):
    path = tmp_path / "source.txt"
    original = "abc abc\nabcdef\n"
    path.write_text(original, encoding="utf-8")
    tool = create_edit_tool(tmp_path)

    cases = [
        [{"oldText": "missing", "newText": "x"}],
        [{"oldText": "abc", "newText": "x"}],
        [{"oldText": "", "newText": "x"}],
        [
            {"oldText": "abcdef", "newText": "x"},
            {"oldText": "def", "newText": "y"},
        ],
    ]
    for edits in cases:
        result = await _execute(tool, {"path": "source.txt", "edits": edits})
        assert result.is_error
        assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_edit_preserves_crlf_and_utf8_bom(tmp_path: Path):
    path = tmp_path / "crlf.txt"
    original = b"\xef\xbb\xbffirst\r\nsecond\r\n"
    path.write_bytes(original)
    tool = create_edit_tool(tmp_path)
    await _execute_direct(
        tool,
        EditArgs(path="crlf.txt", edits=[{"oldText": "second", "newText": "changed"}]),
    )
    assert path.read_bytes() == b"\xef\xbb\xbffirst\r\nchanged\r\n"


@pytest.mark.asyncio
async def test_grep_regex_literal_case_glob_recursive_skip_context_and_limit(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("before\nNeedle\nafter\n", encoding="utf-8")
    (tmp_path / "src/b.txt").write_text("needle in text\n", encoding="utf-8")
    (tmp_path / "src/.git").mkdir()
    (tmp_path / "src/.git/hidden.py").write_text("Needle\n", encoding="utf-8")
    (tmp_path / "src/__pycache__").mkdir()
    (tmp_path / "src/__pycache__/hidden.py").write_text("Needle\n", encoding="utf-8")
    tool = create_grep_tool(tmp_path)

    result = await _execute_direct(
        tool,
        GrepArgs(path="src", pattern="needle", ignoreCase=True, glob="*.py", context=1),
    )
    assert "src/a.py:2: Needle" in result.content
    assert "src/a.py-1- before" in result.content
    assert "src/a.py-3- after" in result.content
    assert "hidden.py" not in result.content

    literal = await _execute_direct(tool, GrepArgs(path="src", pattern=".", literal=True))
    assert literal.content == "No matches found"
    limited = await _execute_direct(tool, GrepArgs(path="src", pattern="needle", ignoreCase=True, limit=1))
    assert limited.details["truncation"]["truncated"] is True
    assert "truncated" in limited.content.lower()
    assert tool.execution_mode == "parallel"


@pytest.mark.asyncio
async def test_grep_no_match_invalid_regex_and_missing_path(tmp_path: Path):
    tool = create_grep_tool(tmp_path)
    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")
    assert (await _execute(tool, {"path": "one.txt", "pattern": "missing"})).content == "No matches found"
    invalid = await _execute(tool, {"path": "one.txt", "pattern": "["})
    assert invalid.is_error and "invalid regex" in invalid.content.lower()
    missing = await _execute(tool, {"path": "missing", "pattern": "x"})
    assert missing.is_error and "missing" in missing.content


@pytest.mark.skipif(os.name == "nt", reason="bash tool targets POSIX-like shells")
@pytest.mark.asyncio
async def test_bash_cwd_combines_output_and_reports_nonzero(tmp_path: Path):
    tool = create_bash_tool(tmp_path)
    cwd_result = await _execute_direct(tool, BashArgs(command="pwd"))
    assert cwd_result.content.strip() == str(tmp_path.resolve())

    output = await _execute_direct(tool, BashArgs(command="printf out; printf err >&2"))
    assert output.content == "outerr"
    failed = await _execute(tool, {"command": "printf failure; exit 7"})
    assert failed.is_error and "failure" in failed.content and "7" in failed.content
    assert tool.execution_mode == "sequential"


@pytest.mark.skipif(os.name == "nt", reason="bash tool targets POSIX-like shells")
@pytest.mark.asyncio
async def test_bash_timeout_cancellation_and_tail_truncation(tmp_path: Path):
    tool = create_bash_tool(tmp_path)
    timed_out = await _execute(tool, {"command": "printf before; sleep 5", "timeout": 0.05})
    assert timed_out.is_error and "timed out" in timed_out.content.lower() and "before" in timed_out.content

    task = asyncio.create_task(
        tool.execute(BashArgs(command="sleep 5"), ToolExecutionContext("cancel", "bash", _noop_emit))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    command = "python3 -c 'print(\"head\"); print(\"x\" * 70000); print(\"tail\")'"
    large = await _execute_direct(tool, BashArgs(command=command))
    assert large.details["truncated"] is True
    assert "tail" in large.content
    assert len(large.content.encode("utf-8")) <= BASH_MAX_BYTES


def test_bash_timeout_must_be_positive_and_finite():
    with pytest.raises(ValidationError):
        BashArgs(command="true", timeout=0)
    with pytest.raises(ValidationError):
        BashArgs(command="true", timeout=-1)
    with pytest.raises(ValidationError):
        ReadFileArgs(path="x", limit=0)
    with pytest.raises(ValidationError):
        GrepArgs(pattern="x", context=-1)
