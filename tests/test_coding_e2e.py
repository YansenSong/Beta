from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beta_agent import Message, ScriptedModelAdapter, ToolCall
from coding_agent import CodingAgentOptions, create_coding_agent
from coding_agent.extensions import permission_gate_extension

FIXTURE = Path(__file__).parent / "fixtures" / "coding_project"


@pytest.mark.asyncio
async def test_scripted_coding_agent_bug_fix_chain_and_resume(tmp_path: Path):
    workspace = tmp_path / "coding_project"
    shutil.copytree(FIXTURE, workspace)
    session_file = tmp_path / "coding-session.jsonl"
    model = ScriptedModelAdapter(
        [
            Message.assistant(
                tool_calls=[ToolCall("danger", "bash", {"command": "rm -rf keep"})],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[ToolCall("skill", "read_file", {"path": "skills/verify/SKILL.md"})],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[ToolCall("grep", "grep", {"pattern": "def add", "path": "src"})],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[ToolCall("source", "read_file", {"path": "src/calculator.py"})],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[ToolCall("test-fail", "bash", {"command": "python3 -m pytest -q"})],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[
                    ToolCall(
                        "edit",
                        "edit",
                        {
                            "path": "src/calculator.py",
                            "edits": [{"oldText": "return a - b", "newText": "return a + b"}],
                        },
                    )
                ],
                stop_reason="tool_calls",
            ),
            Message.assistant(
                tool_calls=[ToolCall("test-pass", "bash", {"command": "python3 -m pytest -q"})],
                stop_reason="tool_calls",
            ),
            Message.assistant("已修复并验证通过"),
        ]
    )
    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=workspace,
            model=model,
            extensions=[permission_gate_extension],
            session_file=session_file,
        )
    )

    result = await runtime.run("修复 calculator 的 bug，并验证测试")
    assert result[-1].content == "已修复并验证通过"
    assert (workspace / "keep/keep.txt").exists()
    assert "return a + b" in (workspace / "src/calculator.py").read_text(encoding="utf-8")

    tool_messages = [message for message in runtime.agent.messages if message.role == "tool"]
    assert len(tool_messages) == 7
    assert tool_messages[0].is_error and "permission-gate" in tool_messages[0].content
    assert "修改代码后运行测试验证结果" in tool_messages[1].content
    assert "src/calculator.py:1: def add" in tool_messages[2].content
    assert "return a - b" in tool_messages[3].content
    assert tool_messages[4].is_error and "failed" in tool_messages[4].content.lower()
    assert not tool_messages[5].is_error
    assert not tool_messages[6].is_error
    assert any(
        call.name == "bash"
        for message in runtime.agent.messages
        if message.role == "assistant"
        for call in message.tool_calls
    )

    before_save = len(runtime.session.entries)
    await runtime.save_session()
    assert len(runtime.session.entries) == before_save
    runtime.close()

    resumed = await create_coding_agent(
        CodingAgentOptions(
            cwd=workspace,
            model=ScriptedModelAdapter([Message.assistant("继续工作完成")]),
            session_file=session_file,
        )
    )
    old_message_count = len(resumed.agent.messages)
    await resumed.run("继续")
    assert len(resumed.agent.messages) == old_message_count + 2
    assert [message.content for message in resumed.session.reconstruct_messages()][-2:] == [
        "继续",
        "继续工作完成",
    ]
    await resumed.save_session()
    assert len(resumed.session.entries) == before_save + 2
    resumed.close()
