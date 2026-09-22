from __future__ import annotations

import pytest
from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentContext,
    AgentConfig,
    AgentMessage,
    ProviderRequestOptions,
    ProviderRequestOptionsPatch,
    RequestUpdate,
    ScriptedModelAdapter,
    Tool,
    ToolCall,
    ToolResult,
    get_current_tool_declarations,
    to_tool_declaration,
)


class Empty(BaseModel):
    pass


class RecordingModel(ScriptedModelAdapter):
    def __init__(self, responses):
        super().__init__(responses)
        self.options = []
        self.tools = []

    async def stream(self, *, system_prompt, messages, tools, request_options=None, cancellation=None):
        self.options.append(request_options)
        self.tools.append(list(tools))
        async for event in super().stream(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            request_options=request_options,
            cancellation=cancellation,
        ):
            yield event


@pytest.mark.asyncio
async def test_prepare_request_runs_once_per_logical_request_and_merges_options():
    seen = []

    async def execute(args, ctx):
        return ToolResult("ok")

    async def prepare(request, cancellation=None):
        seen.append(request)
        return RequestUpdate(
            request_options=ProviderRequestOptionsPatch(
                headers={"x-new": "yes"}, metadata={"request": len(seen)}
            )
        )

    model = RecordingModel(
        [
            AgentMessage.assistant(
                tool_calls=[ToolCall("c", "work", {})], stop_reason="tool_calls"
            ),
            AgentMessage.assistant("done"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool("work", "work", Empty, execute)],
        config=AgentConfig(
            provider_request_options=ProviderRequestOptions(
                headers={"base": "ok"}, metadata={"base": True}
            ),
            prepare_request=prepare,
        ),
    )

    await agent.run("go")

    assert len(seen) == 2
    assert len(model.calls) == 2
    assert [options.headers for options in model.options] == [
        {"base": "ok", "x-new": "yes"},
        {"base": "ok", "x-new": "yes"},
    ]
    assert [options.metadata for options in model.options] == [
        {"base": True, "request": 1},
        {"base": True, "request": 2},
    ]
    assert seen[0].context is agent.context


@pytest.mark.asyncio
async def test_prepare_request_can_replace_context_and_model():
    replacement = RecordingModel([AgentMessage.assistant("replacement")])
    original = RecordingModel([AgentMessage.assistant("unused")])

    replacement_context = AgentContext(messages=[AgentMessage.user("from hook")], tools=[])

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement_context, model=replacement)

    agent = Agent(
        model=original,
        messages=[AgentMessage.user("original")],
        config=AgentConfig(prepare_request=prepare),
    )

    result = await agent.run("ignored")

    assert result[-1].text == "replacement"
    assert original.calls == []
    assert len(replacement.calls) == 1
    assert [message.text for message in agent.context.messages if message.role == "user"] == ["from hook"]


@pytest.mark.asyncio
async def test_prepare_context_replacement_reconciles_tools_into_transcript_and_reset():
    async def execute(args, ctx):
        return ToolResult("ok")

    tool = Tool("work", "work", Empty, execute)
    replacement = AgentContext(messages=[AgentMessage.user("replacement")], tools=[tool])

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement)

    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant("done")]),
        config=AgentConfig(prepare_request=prepare),
    )
    stream = agent.stream("original")
    events = [event async for event in stream]
    generated = await stream.result()

    declarations = get_current_tool_declarations(agent.messages)
    assert [item.name for item in declarations] == ["work"]
    delta = next(
        message
        for message in generated
        if message.role == "system" and message.tools_added
    )
    assert [item.name for item in delta.tools_added] == ["work"]
    assert sum(
        event.type == "message_start" and event.message is delta
        for event in events
    ) == 1
    assert sum(
        event.type == "message_end" and event.message is delta
        for event in events
    ) == 1

    agent.reset()
    assert [item.name for item in get_current_tool_declarations(agent.messages)] == ["work"]


@pytest.mark.asyncio
async def test_prepare_context_replacement_removes_declared_tools_without_duplicate_delta():
    async def execute(args, ctx):
        return ToolResult("ok")

    tool = Tool("work", "work", Empty, execute)
    declaration = to_tool_declaration(tool)
    replacement = AgentContext(
        messages=[
            AgentMessage.system("", tools_added=[declaration]),
            AgentMessage.user("replacement"),
        ],
        tools=[],
    )

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement)

    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant("done")]),
        config=AgentConfig(prepare_request=prepare),
    )
    generated = await agent.run("original")

    assert get_current_tool_declarations(agent.messages) == []
    removals = [
        message
        for message in generated
        if message.role == "system" and message.tools_removed
    ]
    assert len(removals) == 1
    assert [item.name for item in removals[0].tools_removed] == ["work"]


@pytest.mark.asyncio
async def test_prepare_context_reconciliation_is_appended_after_historical_tool_deltas():
    async def execute(args, ctx):
        return ToolResult("ok")

    current_tool = Tool("current", "current", Empty, execute)
    historical_tool = Tool("historical", "historical", Empty, execute)
    historical_user = AgentMessage.user("historical context")
    historical_delta = AgentMessage.system(
        "",
        tools_added=[to_tool_declaration(historical_tool)],
    )
    replacement = AgentContext(
        messages=[historical_user, historical_delta],
        tools=[current_tool],
    )

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement)

    model = RecordingModel([AgentMessage.assistant("done")])
    agent = Agent(model=model, config=AgentConfig(prepare_request=prepare))
    stream = agent.stream("ignored")
    events = [event async for event in stream]
    generated = await stream.result()

    reconciliation = next(
        message
        for message in generated
        if message.role == "system" and message.tools_added
    )
    transcript = agent.messages
    assert transcript.index(reconciliation) > transcript.index(historical_delta)
    assert transcript.index(reconciliation) > transcript.index(historical_user)
    assert get_current_tool_declarations(transcript) == [to_tool_declaration(current_tool)]
    assert sum(
        event.type == "message_start" and event.message is reconciliation
        for event in events
    ) == 1
    assert sum(
        event.type == "message_end" and event.message is reconciliation
        for event in events
    ) == 1

    agent.reset()
    assert get_current_tool_declarations(agent.messages) == [to_tool_declaration(current_tool)]


@pytest.mark.asyncio
async def test_prepare_context_replacement_with_matching_tools_emits_no_reconciliation_delta():
    async def execute(args, ctx):
        return ToolResult("ok")

    tool = Tool("work", "work", Empty, execute)
    declaration = to_tool_declaration(tool)
    historical_delta = AgentMessage.system("", tools_added=[declaration])
    replacement = AgentContext(
        messages=[AgentMessage.user("historical context"), historical_delta],
        tools=[tool],
    )

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement)

    model = RecordingModel([AgentMessage.assistant("done")])
    agent = Agent(model=model, config=AgentConfig(prepare_request=prepare))
    stream = agent.stream("ignored")
    events = [event async for event in stream]
    generated = await stream.result()

    assert [message for message in generated if message.role == "system"] == []
    assert not any(
        event.type in {"message_start", "message_end"}
        and event.message is not None
        and event.message.role == "system"
        for event in events
    )
    assert model.tools == [[tool]]
    assert get_current_tool_declarations(agent.messages) == [declaration]

    agent.reset()
    assert get_current_tool_declarations(agent.messages) == [declaration]
