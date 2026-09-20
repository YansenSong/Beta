from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from beta_agent import Message, ScriptedModelAdapter, Tool, ToolCall, ToolResult
from beta_agent.transcript import get_current_tool_declarations
from beta_agent.extensions import ExtensionTool
from coding_agent import (
    CodingAgentOptions,
    CodingCompactionOptions,
    create_coding_agent,
)
from coding_agent.extensions import plan_mode_extension, subagent_extension


class NoArgs(BaseModel):
    pass


async def noop(args, ctx):
    return ToolResult(content="ok")


@pytest.mark.asyncio
async def test_assembly_tools_prompt_skills_and_extension_factory(tmp_path: Path):
    skill = tmp_path / "skills/verify/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: verify\ndescription: Run the project verification\n---\nSECRET SKILL BODY\n",
        encoding="utf-8",
    )
    command_seen: list[str] = []

    def extension(api):
        async def execute(args, ctx, tool_ctx):
            return ToolResult(content="extension")

        api.register_tool(
            ExtensionTool(name="extension_tool", description="Extension test tool", args_model=NoArgs, handler=execute)
        )
        api.register_command(
            "remember", description="test command", handler=lambda value, ctx: command_seen.append(value)
        )

    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([Message.assistant("done")]),
            extensions=[extension],
            system_prompt_prefix="CUSTOM PREFIX",
            append_system_prompt="CUSTOM APPEND",
        )
    )
    assert [tool.name for tool in runtime.tools] == ["read_file", "write_file", "edit", "grep", "bash"]
    assert [tool.name for tool in runtime.agent.context.tools] == [tool.name for tool in runtime.tools]
    assert "CUSTOM PREFIX" in runtime.agent.context.system_prompt
    assert "CUSTOM APPEND" in runtime.agent.context.system_prompt
    assert "read_file:" in runtime.agent.context.system_prompt
    assert "verify" in runtime.agent.context.system_prompt
    assert "Run the project verification" in runtime.agent.context.system_prompt
    assert "SECRET SKILL BODY" not in runtime.agent.context.system_prompt
    assert f"Current working directory: {tmp_path.resolve()}" in runtime.agent.context.system_prompt
    assert [tool.name for tool in runtime.runner.get_registered_tools()] == ["extension_tool"]
    await runtime.run_command("/remember value")
    assert command_seen == ["value"]
    runtime.close()


@pytest.mark.asyncio
async def test_plan_mode_can_activate_product_subagent_with_configured_child_factory(tmp_path: Path):
    parent_model = ScriptedModelAdapter(
        [
            Message.assistant(
                tool_calls=[ToolCall("sub", "subagent", {"task": "inspect independently"})],
                stop_reason="tool_calls",
            ),
            Message.assistant("parent done"),
        ]
    )
    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=parent_model,
            extensions=[plan_mode_extension, subagent_extension],
            child_model_factory=lambda: ScriptedModelAdapter([Message.assistant("child analysis")]),
        )
    )

    await runtime.run_command("/plan")
    assert [tool.name for tool in runtime.agent.context.tools] == ["read_file", "grep", "bash", "subagent"]

    await runtime.run("delegate analysis")
    tool_result = next(message for message in runtime.agent.messages if message.role == "tool")
    assert tool_result.content == "child analysis"
    assert tool_result.metadata["details"]["child_messages"] == 2
    runtime.close()


@pytest.mark.asyncio
async def test_duplicate_extra_tool_name_fails_fast(tmp_path: Path):
    duplicate = Tool(name="read_file", description="duplicate", args_model=NoArgs, handler=noop)
    with pytest.raises(ValueError, match="Duplicate tool name.*read_file"):
        await create_coding_agent(
            CodingAgentOptions(cwd=tmp_path, model=ScriptedModelAdapter([]), extra_tools=[duplicate])
        )


@pytest.mark.asyncio
async def test_host_persists_once_save_reload_and_resume(tmp_path: Path):
    session_path = tmp_path / "state/session.jsonl"
    first = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([Message.assistant("first answer")]),
            session_file=session_path,
        )
    )
    await first.run("first question")
    assert len(first.session.entries) == 3  # durable system/tool baseline + two run messages
    await first.save_session()
    assert len(first.session.entries) == 3, "save_session must not append agent.messages a second time"
    first.close()

    seen: list[str] = []

    def observe(api):
        async def on_message(event, ctx):
            seen.append(event.message.content)

        api.on("message_end", on_message)

    resumed = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([Message.assistant("continued answer")]),
            session_file=session_path,
            extensions=[observe],
        )
    )
    assert [message.role for message in resumed.agent.messages] == ["system", "user", "assistant"]
    assert [message.text for message in resumed.agent.messages[-2:]] == ["first question", "first answer"]
    assert seen == []
    await resumed.run("continue question")
    assert seen == ["continue question", "continued answer"]
    assert [message.text for message in resumed.session.reconstruct_messages()][-4:] == [
        "first question",
        "first answer",
        "continue question",
        "continued answer",
    ]
    resumed.close()


@pytest.mark.asyncio
async def test_legacy_v2_session_loads_and_appends_durable_migration_baseline(tmp_path: Path):
    session_path = tmp_path / "legacy.jsonl"
    entries = [
        {
            "id": "legacy-user",
            "parent_id": None,
            "timestamp": "2024-01-01T00:00:00+00:00",
            "type": "message",
            "payload": {"role": "user", "content": "old question"},
        },
        {
            "id": "legacy-assistant",
            "parent_id": "legacy-user",
            "timestamp": "2024-01-01T00:00:01+00:00",
            "type": "message",
            "payload": {"role": "assistant", "content": "old answer"},
        },
    ]
    rows = [
        *entries,
        {"_meta": {"leaf_id": "legacy-assistant", "format_version": 2}},
    ]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([Message.assistant("resumed answer")]),
            session_file=session_path,
        )
    )
    assert [message.text for message in runtime.agent.messages[:2]] == ["old question", "old answer"]
    assert runtime.agent.messages[-1].role == "system"
    assert "Current working directory" in runtime.agent.system_prompt
    assert {item.name for item in get_current_tool_declarations(runtime.agent.messages)} == {
        "read_file",
        "write_file",
        "edit",
        "grep",
        "bash",
    }

    await runtime.run("new question")
    assert [message.text for message in runtime.session.reconstruct_messages()][-2:] == [
        "new question",
        "resumed answer",
    ]
    await runtime.save_session()
    saved = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
    assert saved[-1]["_meta"]["format_version"] == 3
    runtime.close()


@pytest.mark.asyncio
async def test_legacy_v2_compacted_session_orders_migration_baseline_before_summary(tmp_path: Path):
    session_path = tmp_path / "legacy-compacted.jsonl"
    entries = [
        {
            "id": "legacy-user-1",
            "parent_id": None,
            "timestamp": "2024-01-01T00:00:00+00:00",
            "type": "message",
            "payload": {"role": "user", "content": "old question"},
        },
        {
            "id": "legacy-assistant-1",
            "parent_id": "legacy-user-1",
            "timestamp": "2024-01-01T00:00:01+00:00",
            "type": "message",
            "payload": {"role": "assistant", "content": "old answer"},
        },
        {
            "id": "legacy-user-2",
            "parent_id": "legacy-assistant-1",
            "timestamp": "2024-01-01T00:00:02+00:00",
            "type": "message",
            "payload": {"role": "user", "content": "recent question"},
        },
        {
            "id": "legacy-assistant-2",
            "parent_id": "legacy-user-2",
            "timestamp": "2024-01-01T00:00:03+00:00",
            "type": "message",
            "payload": {"role": "assistant", "content": "recent answer"},
        },
        {
            "id": "legacy-compaction",
            "parent_id": "legacy-assistant-2",
            "timestamp": "2024-01-01T00:00:04+00:00",
            "type": "compaction",
            "payload": {
                "summary": "legacy summary",
                "first_kept_entry_id": "legacy-user-2",
                "tokens_before": 40,
            },
        },
    ]
    rows = [*entries, {"_meta": {"leaf_id": "legacy-compaction", "format_version": 2}}]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([]),
            session_file=session_path,
        )
    )
    messages = runtime.agent.messages
    assert [message.role for message in messages[:2]] == ["system", "system"]
    assert "Current working directory" in messages[0].text
    assert "Conversation summary:\nlegacy summary" == messages[1].text
    assert {item.name for item in get_current_tool_declarations(messages)} == {
        "read_file",
        "write_file",
        "edit",
        "grep",
        "bash",
    }
    assert runtime.agent.system_prompt.index("Current working directory") < runtime.agent.system_prompt.index(
        "Conversation summary"
    )
    assert [message.text for message in messages[-2:]] == ["recent question", "recent answer"]

    await runtime.save_session()
    runtime.close()

    restored = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([]),
            session_file=session_path,
        )
    )
    restored_messages = restored.agent.messages
    assert [message.role for message in restored_messages[:2]] == ["system", "system"]
    assert "Current working directory" in restored_messages[0].text
    assert "Conversation summary:\nlegacy summary" == restored_messages[1].text
    restored.close()


@pytest.mark.asyncio
async def test_optional_compaction_replaces_agent_context_and_survives_reload(tmp_path: Path):
    session_path = tmp_path / "compact.jsonl"
    model = ScriptedModelAdapter([Message.assistant("a1"), Message.assistant("a2")])
    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=model,
            session_file=session_path,
            compaction=CodingCompactionOptions(
                keep_last_messages=2,
                summarize=lambda messages: "summary: " + ",".join(message.text for message in messages),
            ),
        )
    )
    await runtime.run("u1")
    await runtime.run("u2")
    assert len(runtime.session.entries) == 5
    await runtime.save_session()
    assert len(runtime.session.entries) == 6
    assert runtime.session.entries[-1].type == "compaction"
    assert [message.role for message in runtime.agent.messages] == ["system", "system", "user", "assistant"]
    assert "summary: u1,a1" in runtime.agent.messages[1].text
    assert [message.content for message in runtime.session.reconstruct_messages()][-2:] == ["u2", "a2"]
    runtime.close()

    restored = await create_coding_agent(
        CodingAgentOptions(
            cwd=tmp_path,
            model=ScriptedModelAdapter([]),
            session_file=session_path,
        )
    )
    assert [message.role for message in restored.agent.messages[:2]] == ["system", "system"]
    assert "summary: u1,a1" in restored.agent.messages[1].text
    assert [message.content for message in restored.agent.messages[-2:]] == ["u2", "a2"]
    restored.close()
