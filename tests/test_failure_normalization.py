from __future__ import annotations

import pytest
from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentMessage,
    ModelEvent,
    ScriptedModelAdapter,
    Tool,
    ToolCall,
    ToolResult,
)


class NoArgs(BaseModel):
    pass


class BlockingModel:
    def __init__(self, *, partial: bool = False):
        self.partial = partial
        self.calls = 0

    async def stream(self, *, system_prompt, messages, tools, cancellation):
        self.calls += 1
        if self.partial:
            yield ModelEvent("start", AgentMessage.assistant("partial"))
        raise RuntimeError("provider boom")
        yield  # pragma: no cover


async def _collect(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events, await stream.result()


@pytest.mark.asyncio
async def test_model_failure_before_first_event_is_normalized():
    agent = Agent(model=BlockingModel())

    events, messages = await _collect(agent.stream("hello"))

    assistant = next(message for message in messages if message.role == "assistant")
    assert assistant.stop_reason == "error"
    assert agent.last_error is not None
    assert agent.last_error.stage == "model"
    assert [event.type for event in events].count("message_start") == 2
    assert [event.type for event in events].count("message_end") == 2
    assert [event.type for event in events].count("agent_error") == 1
    assert events[-1].type == "agent_end"
    assert events[-1].status == "error"


@pytest.mark.asyncio
async def test_model_failure_after_partial_has_one_completed_assistant_message():
    agent = Agent(model=BlockingModel(partial=True))

    events, messages = await _collect(agent.stream("hello"))

    assistants = [message for message in messages if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].text == "partial"
    assert assistants[0].stop_reason == "error"
    assert [event.type for event in events].count("message_start") == 2
    assert [event.type for event in events].count("message_end") == 2
    assert [event for event in events if event.type == "message_end"][-1].message.stop_reason == "error"


@pytest.mark.asyncio
async def test_before_tool_failure_fails_closed_and_agent_continues():
    executed: list[str] = []

    async def execute(args, ctx):
        executed.append("called")
        return ToolResult(content="unexpected")

    async def before(call, args, context):
        raise ValueError("policy unavailable")

    model = ScriptedModelAdapter(
        [
            AgentMessage.assistant(
                tool_calls=[ToolCall("1", "danger", {})],
                stop_reason="tool_calls",
            ),
            AgentMessage.assistant("recovered"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool("danger", "danger", NoArgs, execute)],
        config=AgentConfig(before_tool_call=before),
    )

    messages = await agent.run("go")

    result = next(message for message in messages if message.role == "tool")
    assert result.is_error
    assert "before_tool_call" in result.metadata["details"]["stage"]
    assert "blocked" in result.text
    assert executed == []
    assert messages[-1].text == "recovered"
    assert agent.last_error is None


@pytest.mark.asyncio
async def test_tool_execute_failure_is_model_visible_and_non_fatal():
    async def execute(args, ctx):
        raise RuntimeError("tool boom")

    model = ScriptedModelAdapter(
        [
            AgentMessage.assistant(
                tool_calls=[ToolCall("1", "broken", {})],
                stop_reason="tool_calls",
            ),
            AgentMessage.assistant("self repaired"),
        ]
    )
    agent = Agent(model=model, tools=[Tool("broken", "broken", NoArgs, execute)])

    messages = await agent.run("go")

    result = next(message for message in messages if message.role == "tool")
    assert result.is_error
    assert result.metadata["details"]["stage"] == "tool_execute"
    assert messages[-1].text == "self repaired"


@pytest.mark.asyncio
async def test_after_tool_failure_does_not_repeat_tool_or_fatal_run():
    executed = 0

    async def execute(args, ctx):
        nonlocal executed
        executed += 1
        return ToolResult(content="real result", details={"ok": True})

    async def after(call, args, result, is_error, context):
        raise RuntimeError("observer boom")

    model = ScriptedModelAdapter(
        [
            AgentMessage.assistant(
                tool_calls=[ToolCall("1", "once", {})],
                stop_reason="tool_calls",
            ),
            AgentMessage.assistant("continued"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool("once", "once", NoArgs, execute)],
        config=AgentConfig(after_tool_call=after),
    )

    messages = await agent.run("go")

    result = next(message for message in messages if message.role == "tool")
    assert executed == 1
    assert result.is_error
    assert result.metadata["details"]["stage"] == "after_tool_call"
    assert result.metadata["details"]["tool_executed"] is True
    assert messages[-1].text == "continued"
    assert agent.last_error is None


@pytest.mark.asyncio
async def test_transform_and_converter_failures_are_fatal_without_provider_call():
    model = BlockingModel()

    def transform(messages, cancellation):
        raise ValueError("transform boom")

    agent = Agent(model=model, config=AgentConfig(transform_context=transform))
    events, _ = await _collect(agent.stream("hello"))
    assert model.calls == 0
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "transform_context"

    def converter(messages, cancellation):
        raise ValueError("converter boom")

    model = BlockingModel()
    agent = Agent(model=model, config=AgentConfig(convert_to_llm=converter))
    events, _ = await _collect(agent.stream("hello"))
    assert model.calls == 0
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "convert_to_llm"


@pytest.mark.asyncio
async def test_prepare_and_should_stop_failures_are_fatal_at_their_stage():
    async def execute(args, ctx):
        return ToolResult(content="ok")

    model = ScriptedModelAdapter(
        [AgentMessage.assistant(tool_calls=[ToolCall("1", "ok", {})], stop_reason="tool_calls")]
    )

    def prepare(turn, cancellation):
        raise RuntimeError("prepare boom")

    agent = Agent(
        model=model,
        tools=[Tool("ok", "ok", NoArgs, execute)],
        config=AgentConfig(prepare_next_turn=prepare),
    )
    events, messages = await _collect(agent.stream("hello"))
    assert any(message.role == "tool" for message in messages)
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "prepare_next_turn"

    model = ScriptedModelAdapter([AgentMessage.assistant("done")])

    def should_stop(turn, cancellation):
        raise RuntimeError("stop decision boom")

    agent = Agent(model=model, config=AgentConfig(should_stop_after_turn=should_stop))
    events, _ = await _collect(agent.stream("hello"))
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "should_stop_after_turn"


@pytest.mark.asyncio
async def test_steering_and_follow_up_provider_failures_are_not_silently_dropped():
    def steering(cancellation):
        raise RuntimeError("steering queue boom")

    agent = Agent(model=ScriptedModelAdapter([]), config=AgentConfig(get_steering_messages=steering))
    events, _ = await _collect(agent.stream("hello"))
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "steering_provider"

    def follow_up(cancellation):
        raise RuntimeError("follow-up queue boom")

    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant("done")]),
        config=AgentConfig(get_follow_up_messages=follow_up),
    )
    events, _ = await _collect(agent.stream("hello"))
    assert events[-1].status == "error"
    assert agent.last_error is not None and agent.last_error.stage == "follow_up_provider"


@pytest.mark.asyncio
async def test_stream_subscriber_failure_closes_partial_assistant_once():
    async def listener(event, cancellation=None):
        if event.type == "message_update":
            raise RuntimeError("subscriber boom")

    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("done")]))
    agent.subscribe(listener)
    events, messages = await _collect(agent.stream("hello"))

    assistants = [message for message in messages if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].stop_reason == "error"
    assert agent.last_error is not None and agent.last_error.stage == "event_listener"
    assert sum(event.type == "agent_end" for event in events) == 1
    assert sum(event.type == "turn_end" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["steering", "follow_up", "prepare_next_turn"])
async def test_scheduling_failure_gets_a_complete_failure_turn(failure_kind):
    config_kwargs = {}
    tools = []
    responses = [AgentMessage.assistant("done")]
    if failure_kind == "steering":
        reads = 0

        async def steering(cancellation=None):
            nonlocal reads
            reads += 1
            if reads == 1:
                return []
            raise RuntimeError("steering boom")

        config_kwargs["get_steering_messages"] = steering
    elif failure_kind == "follow_up":
        async def follow_up(cancellation=None):
            raise RuntimeError("follow-up boom")

        config_kwargs["get_follow_up_messages"] = follow_up
    else:
        async def execute(args, ctx):
            return ToolResult("ok")

        call = ToolCall("call", "work", {})
        responses = [AgentMessage.assistant(tool_calls=[call], stop_reason="tool_calls")]
        tools = [Tool("work", "work", NoArgs, execute)]

        async def prepare_next(turn, cancellation=None):
            raise RuntimeError("prepare_next_turn boom")

        config_kwargs["prepare_next_turn"] = prepare_next

    agent = Agent(
        model=ScriptedModelAdapter(responses),
        tools=tools,
        config=AgentConfig(**config_kwargs),
    )
    events, messages = await _collect(agent.stream("hello"))

    assert [event.type for event in events][-7:] == [
        "turn_end",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "agent_error",
        "agent_end",
    ]
    assert sum(event.type == "turn_start" for event in events) == 2
    assert sum(event.type == "turn_end" for event in events) == 2
    assert sum(event.type == "agent_end" for event in events) == 1
    failures = [
        message
        for message in messages
        if message.role == "assistant" and message.stop_reason == "error"
    ]
    assert len(failures) == 1
    assert agent.last_error is not None
    assert agent.last_error.stage in {"steering_provider", "follow_up_provider", "prepare_next_turn"}
