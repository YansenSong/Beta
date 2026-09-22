from __future__ import annotations

import pytest

from beta_agent import Agent, AgentMessage, ModelEvent, ScriptedModelAdapter, ToolCall


@pytest.mark.asyncio
async def test_streaming_events_preserve_delta_kind_and_partial_snapshot():
    script = [
        ModelEvent("start", AgentMessage.assistant("")),
        ModelEvent("thinking_start", AgentMessage.assistant("", thinking="plan")),
        ModelEvent("thinking_delta", AgentMessage.assistant("", thinking="plan"), delta="plan"),
        ModelEvent("thinking_end", AgentMessage.assistant("", thinking="plan")),
        ModelEvent("text_start", AgentMessage.assistant("", thinking="plan")),
        ModelEvent("text_delta", AgentMessage.assistant("hello", thinking="plan"), delta="hello"),
        ModelEvent("text_end", AgentMessage.assistant("hello", thinking="plan")),
        ModelEvent("done", AgentMessage.assistant("hello", thinking="plan")),
    ]
    agent = Agent(model=ScriptedModelAdapter([script]))

    events = [event async for event in agent.stream("go")]
    updates = [event for event in events if event.type == "message_update"]

    assert [event.model_event.type for event in updates] == [
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_start",
        "text_delta",
        "text_end",
    ]
    assert all(event.partial is event.message for event in updates)
    assert updates[1].model_event.delta == "plan"
    assert updates[4].message.text == "hello"
    assert updates[4].message.thinking == "plan"
    assert agent.messages[-1].text == "hello"
    assert agent.messages[-1].thinking == "plan"


@pytest.mark.asyncio
async def test_legacy_update_still_emits_message_update_with_raw_event():
    raw = ModelEvent("update", AgentMessage.assistant("partial"))
    script = [raw, ModelEvent("done", AgentMessage.assistant("done"))]
    agent = Agent(model=ScriptedModelAdapter([script]))

    events = [event async for event in agent.stream("go")]

    update = next(event for event in events if event.type == "message_update")
    assert update.model_event is raw
    assert update.message.text == "partial"


@pytest.mark.asyncio
async def test_toolcall_streaming_and_tool_events_have_explicit_fields():
    from pydantic import BaseModel
    from beta_agent import Tool, ToolResult

    class Empty(BaseModel):
        pass

    async def execute(args, ctx):
        return ToolResult("ok")

    call = ToolCall("c", "work", {})
    first = [
        ModelEvent("start", AgentMessage.assistant("", stop_reason="tool_calls")),
        ModelEvent(
            "toolcall_start",
            AgentMessage.assistant("", stop_reason="tool_calls"),
            tool_call_id="c",
            tool_name="work",
        ),
        ModelEvent(
            "toolcall_delta",
            AgentMessage.assistant("", tool_calls=[call], stop_reason="tool_calls"),
            tool_call_id="c",
            tool_name="work",
            delta="{}",
        ),
        ModelEvent(
            "toolcall_end",
            AgentMessage.assistant("", tool_calls=[call], stop_reason="tool_calls"),
            tool_call_id="c",
            tool_name="work",
            tool_call=call,
            completed_tool_call=call,
        ),
        ModelEvent("done", AgentMessage.assistant("", tool_calls=[call], stop_reason="tool_calls")),
    ]
    second = [
        ModelEvent("start", AgentMessage.assistant("done")),
        ModelEvent("done", AgentMessage.assistant("done")),
    ]
    agent = Agent(model=ScriptedModelAdapter([first, second]), tools=[Tool("work", "work", Empty, execute)])

    events = [event async for event in agent.stream("go")]

    model_updates = [event for event in events if event.type == "message_update"]
    assert [event.model_event.type for event in model_updates] == [
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
    ]
    assert model_updates[-1].model_event.completed_tool_call == call
    tool_end = next(event for event in events if event.type == "tool_execution_end")
    assert tool_end.partial_result is None
    assert tool_end.is_error is False
    assert tool_end.result.content.text == "ok"
