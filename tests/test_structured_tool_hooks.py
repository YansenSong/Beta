from __future__ import annotations

import pytest
from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentMessage,
    AfterToolCallContext,
    AfterToolCallPatch,
    BeforeToolCallContext,
    ScriptedModelAdapter,
    Tool,
    ToolCall,
    ToolResult,
)


class Empty(BaseModel):
    pass


@pytest.mark.asyncio
async def test_structured_hooks_receive_complete_assistant_and_patch_fields():
    before_seen: list[BeforeToolCallContext] = []
    after_seen: list[AfterToolCallContext] = []

    async def execute(args, ctx):
        return ToolResult("original", details={"old": True}, usage={"before": 1})

    async def before(value: BeforeToolCallContext, cancellation=None):
        before_seen.append(value)
        return None

    async def after(value: AfterToolCallContext, cancellation=None):
        after_seen.append(value)
        return AfterToolCallPatch(
            content="patched",
            details={"new": True},
            usage={"after": 2},
            terminate=False,
            is_error=False,
        )

    call = ToolCall("c", "work", {})
    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant(tool_calls=[call], stop_reason="tool_calls"), AgentMessage.assistant("done")]),
        tools=[Tool("work", "work", Empty, execute)],
        config=AgentConfig(before_tool_call=before, after_tool_call=after),
    )

    await agent.run("go")

    assert len(before_seen) == len(after_seen) == 1
    assert before_seen[0].assistant_message.tool_calls == [call]
    assert before_seen[0].tool_call == call
    assert after_seen[0].assistant_message is before_seen[0].assistant_message
    result = next(message for message in agent.messages if message.role == "tool")
    assert result.text == "patched"
    assert result.metadata["details"] == {"new": True}
    assert result.metadata["usage"] == {"after": 2}
    assert not result.is_error


@pytest.mark.asyncio
async def test_structured_before_type_error_is_not_retried_and_fails_closed():
    calls = 0
    executed = 0

    async def before(value: BeforeToolCallContext, cancellation=None):
        nonlocal calls
        calls += 1
        raise TypeError("hook body failed")

    async def execute(args, ctx):
        nonlocal executed
        executed += 1
        return ToolResult("unexpected")

    agent = Agent(
        model=ScriptedModelAdapter(
            [
                AgentMessage.assistant(
                    tool_calls=[ToolCall("c", "work", {})], stop_reason="tool_calls"
                ),
                AgentMessage.assistant("recovered"),
            ]
        ),
        tools=[Tool("work", "work", Empty, execute)],
        config=AgentConfig(before_tool_call=before),
    )

    await agent.run("go")

    assert calls == 1
    assert executed == 0
    assert next(message for message in agent.messages if message.role == "tool").is_error
