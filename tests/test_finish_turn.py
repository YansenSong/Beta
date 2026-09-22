from __future__ import annotations

import pytest

from beta_agent import Agent, AgentConfig, AgentMessage, ScriptedModelAdapter, TurnDecision


@pytest.mark.asyncio
async def test_finish_continue_makes_exactly_one_context_only_request():
    seen: list[int] = []

    async def finish(turn, cancellation=None):
        seen.append(len(turn.context.messages))
        return TurnDecision("continue") if len(seen) == 1 else None

    model = ScriptedModelAdapter([AgentMessage.assistant("one"), AgentMessage.assistant("two")])
    agent = Agent(model=model, config=AgentConfig(finish_turn=finish))

    events = [event async for event in agent.stream("hello")]

    assert len(model.calls) == 2
    assert [message.role for message in model.calls[1]] == ["user", "assistant"]
    assert not any(message.role == "user" and message.text == "continue" for message in agent.messages)
    assert [event.type for event in events].count("turn_end") == 2, [
        (event.type, event.status, event.error, event.message.stop_reason if event.message else None)
        for event in events
    ]
    assert events[-1].type == "agent_end"


@pytest.mark.asyncio
async def test_finish_continue_does_not_duplicate_natural_tool_request():
    calls = []

    async def finish(turn, cancellation=None):
        calls.append(turn.message.text)
        return TurnDecision("continue") if len(calls) == 1 else None

    from pydantic import BaseModel
    from beta_agent import Tool, ToolCall, ToolResult

    class Empty(BaseModel):
        pass

    async def execute(args, ctx):
        return ToolResult("ok")

    model = ScriptedModelAdapter(
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
        config=AgentConfig(finish_turn=finish),
    )

    await agent.run("go")

    assert len(model.calls) == 2
    assert [message.text for message in agent.messages if message.role == "assistant"] == ["", "done"], [
        (message.text, message.stop_reason, message.metadata) for message in agent.messages
    ]


@pytest.mark.asyncio
async def test_finish_end_skips_follow_up_and_hook_runs_before_turn_end():
    follow_up_reads = 0

    async def get_follow_up(cancellation=None):
        nonlocal follow_up_reads
        follow_up_reads += 1
        return [AgentMessage.user("must not run")]

    async def finish(turn, cancellation=None):
        return TurnDecision("end")

    model = ScriptedModelAdapter([AgentMessage.assistant("done")])
    agent = Agent(
        model=model,
        config=AgentConfig(finish_turn=finish, get_follow_up_messages=get_follow_up),
    )
    stream = agent.stream("hello")
    events = [event async for event in stream]
    await stream.result()

    assert follow_up_reads == 0
    assert len(model.calls) == 1
    assert [event.type for event in events][-4:] == ["message_end", "finish_turn", "turn_end", "agent_end"]


@pytest.mark.asyncio
async def test_finish_error_is_one_terminal_lifecycle_and_has_stage():
    async def finish(turn, cancellation=None):
        raise RuntimeError("finish boom")

    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant("done")]),
        config=AgentConfig(finish_turn=finish),
    )
    events = [event async for event in agent.stream("hello")]

    assert agent.last_error is not None and agent.last_error.stage == "finish_turn"
    assert sum(event.type == "agent_error" for event in events) == 1
    assert sum(event.type == "agent_end" for event in events) == 1
    assert sum(event.type == "turn_end" for event in events) == 1
    failure = [message for message in agent.messages if message.role == "assistant"][-1]
    assert failure.stop_reason == "error"
    assert failure.tool_calls == []
    assert failure.metadata["stage"] == "finish_turn"


def test_finish_and_legacy_should_stop_are_rejected_together():
    with pytest.raises(ValueError, match="finish_turn"):
        AgentConfig(finish_turn=lambda turn: TurnDecision("end"), should_stop_after_turn=lambda turn: False)
