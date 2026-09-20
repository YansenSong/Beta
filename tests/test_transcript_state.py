from __future__ import annotations

import json

from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentMessage,
    ScriptedModelAdapter,
    SessionTree,
    Tool,
    ToolCall,
    ToolDeclaration,
    ToolReference,
    ToolResult,
    collapse_transcript,
    compact_session,
    create_initial_system_message,
    get_current_system_prompt,
    get_current_tool_declarations,
    get_tool_state_changes,
    to_tool_declaration,
)


class Args(BaseModel):
    value: str


async def _handler(args, ctx):
    return "ok"


def _tool(name: str, description: str = "tool", schema: dict | None = None) -> Tool:
    class ToolArgs(BaseModel):
        value: str

    if schema is None:
        args_model = ToolArgs
    else:
        # Keep the test schema explicit so same-name redefine behavior is clear.
        class ExplicitArgs(BaseModel):
            value: int

        args_model = ExplicitArgs
    return Tool(name, description, args_model, _handler)


def test_initial_state_replay_and_tool_state_diffs():
    tool_a = _tool("a")
    tool_b = _tool("b")
    initial = create_initial_system_message("system instructions", [tool_a, tool_b])

    assert initial is not None
    assert get_current_system_prompt([initial]) == "system instructions"
    assert [item.name for item in get_current_tool_declarations([initial])] == ["a", "b"]
    assert not hasattr(initial.tools_added[0], "handler")

    added = get_tool_state_changes([to_tool_declaration(tool_a)], [to_tool_declaration(tool_a), to_tool_declaration(tool_b)])
    assert [item.name for item in added.tools_added] == ["b"]
    assert added.tools_removed == []

    removed = get_tool_state_changes([to_tool_declaration(tool_a), to_tool_declaration(tool_b)], [to_tool_declaration(tool_b)])
    assert [item.name for item in removed.tools_removed] == ["a"]
    assert removed.tools_added == []


def test_agent_constructor_stores_system_and_tool_baseline_in_transcript():
    agent = Agent(model=ScriptedModelAdapter([]), system_prompt="S", tools=[_tool("a")])

    assert agent.context.messages[0].role == "system"
    assert agent.context.system_prompt == "S"
    assert agent.system_prompt == "S"
    assert [item.name for item in get_current_tool_declarations(agent.messages)] == ["a"]


async def test_provider_projection_collapses_system_messages_and_tool_deltas():
    tool_a = _tool("a")
    tool_b = _tool("b")

    class RecordingModel(ScriptedModelAdapter):
        def __init__(self):
            super().__init__([AgentMessage.assistant("done")])
            self.requests = []

        async def stream(self, *, system_prompt, messages, tools, cancellation=None):
            self.requests.append((system_prompt, list(messages), list(tools)))
            async for event in super().stream(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                cancellation=cancellation,
            ):
                yield event

    model = RecordingModel()
    agent = Agent(model=model, system_prompt="base", tools=[tool_a, tool_b])
    extra = AgentMessage.system(
        "extra instruction",
        tools_removed=[ToolReference("a")],
        tools_added=[to_tool_declaration(tool_b)],
    )
    agent.context.messages.append(extra)

    await agent.run("hello")

    prompt, messages, tools = model.requests[0]
    assert prompt == "base\n\nextra instruction"
    assert [message.role for message in messages] == ["user"]
    assert [tool.name for tool in tools] == ["a", "b"]


async def test_runtime_tool_change_is_recorded_before_next_provider_request():
    tool_a = _tool("a")
    tool_b = _tool("b")

    class RecordingModel(ScriptedModelAdapter):
        def __init__(self):
            super().__init__(
                [
                    AgentMessage.assistant(
                        tool_calls=[ToolCall("call", "a", {"value": "x"})],
                        stop_reason="tool_calls",
                    ),
                    AgentMessage.assistant("done"),
                ]
            )
            self.requests = []

        async def stream(self, *, system_prompt, messages, tools, cancellation=None):
            self.requests.append((system_prompt, list(messages), list(tools)))
            async for event in super().stream(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                cancellation=cancellation,
            ):
                yield event

    async def after_tool(call, args, result, is_error, context):
        context.tools = [tool_a, tool_b]

    model = RecordingModel()
    agent = Agent(
        model=model,
        system_prompt="base",
        tools=[tool_a],
        config=AgentConfig(after_tool_call=after_tool),
    )
    stream = agent.stream("go")
    events = [event async for event in stream]
    await stream.result()

    delta = next(
        message
        for message in agent.messages
        if message.role == "system" and any(item.name == "b" for item in message.tools_added)
    )
    assert [item.name for item in delta.tools_added] == ["b"]
    assert len(model.requests) == 2
    assert [tool.name for tool in model.requests[1][2]] == ["a", "b"]
    assert [message.role for message in model.requests[1][1]] == ["user", "assistant", "tool"]
    delta_lifecycle = [
        event.type
        for event in events
        if event.message is delta and event.type in {"message_start", "message_end"}
    ]
    assert delta_lifecycle == ["message_start", "message_end"]


def test_redefinition_is_remove_plus_add_and_identical_declaration_is_noop():
    original = ToolDeclaration("lookup", "old", {"type": "object", "properties": {"q": {"type": "string"}}})
    replacement = ToolDeclaration("lookup", "new", {"type": "object", "properties": {"q": {"type": "integer"}}})
    changes = get_tool_state_changes([original], [replacement])

    assert changes.tools_removed == [ToolReference("lookup")]
    assert changes.tools_added == [replacement]
    assert get_tool_state_changes([original], [ToolDeclaration("lookup", "old", dict(original.parameters))]) == type(changes)([], [])


def test_branch_replay_and_provider_collapse():
    tool_a = ToolDeclaration("a", "A", {"type": "object"})
    tool_b = ToolDeclaration("b", "B", {"type": "object"})
    session = SessionTree()
    baseline = session.append_message(AgentMessage.system("base", tools_added=[tool_a]))
    session.append_message(AgentMessage.user("question"))
    session.append_message(AgentMessage.system("extra", tools_removed=[ToolReference("a")], tools_added=[tool_b]))
    session.append_message(AgentMessage.assistant("answer"))

    assert [item.name for item in get_current_tool_declarations(session.reconstruct_messages())] == ["b"]
    session.branch(baseline.id)
    branch = session.reconstruct_messages()
    assert [item.name for item in get_current_tool_declarations(branch)] == ["a"]

    prompt, messages = collapse_transcript(
        [
            AgentMessage.system("base", tools_added=[tool_a]),
            AgentMessage.user("question"),
            AgentMessage.system("extra", tools_removed=[ToolReference("a")], tools_added=[tool_b]),
            AgentMessage.assistant("answer", tool_calls=[ToolCall("c", "b", {})]),
        ]
    )
    assert prompt == "base\n\nextra"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].tool_calls[0].name == "b"


async def test_session_v3_roundtrip_persists_tool_deltas_and_compaction_keeps_state(tmp_path):
    tool_a = ToolDeclaration("a", "A", {"type": "object", "properties": {"x": {"type": "string"}}})
    tool_b = ToolDeclaration("b", "B", {"type": "object"})
    session = SessionTree()
    session.append_message(AgentMessage.system("base prompt", tools_added=[tool_a]))
    session.append_message(AgentMessage.user("u1"))
    session.append_message(AgentMessage.assistant("a1"))
    session.append_message(AgentMessage.system("", tools_removed=[ToolReference("a")], tools_added=[tool_b]))
    session.append_message(AgentMessage.user("u2"))
    session.append_message(AgentMessage.assistant("a2"))

    path = tmp_path / "session.jsonl"
    session.save_jsonl(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["payload"]["tools_added"][0]["name"] == "a"
    assert rows[3]["payload"]["tools_removed"] == [{"name": "a"}]
    assert rows[-1]["_meta"]["format_version"] == 3

    restored = SessionTree.load_jsonl(path)
    assert get_current_system_prompt(restored.reconstruct_messages()) == "base prompt"
    assert [item.name for item in get_current_tool_declarations(restored.reconstruct_messages())] == ["b"]

    await compact_session(
        restored,
        keep_last_messages=2,
        summarize=lambda messages: ",".join(message.text for message in messages),
    )
    compacted = restored.reconstruct_messages()
    assert "base prompt" in get_current_system_prompt(compacted)
    assert "Conversation summary:\nu1,a1" in get_current_system_prompt(compacted)
    assert [item.name for item in get_current_tool_declarations(compacted)] == ["b"]
