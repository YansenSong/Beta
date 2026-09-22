from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from beta_agent import Agent, AgentConfig, AgentMessage, ScriptedModelAdapter, Tool, ToolResult


class Empty(BaseModel):
    pass


async def _execute(args, ctx):
    return ToolResult("ok")


@pytest.mark.asyncio
async def test_state_is_streaming_until_agent_end_subscribers_settle():
    entered = asyncio.Event()
    release = asyncio.Event()
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("done")]))

    async def listener(event, cancellation=None):
        if event.type == "agent_end":
            assert agent.state.is_streaming
            entered.set()
            await release.wait()

    agent.subscribe(listener)
    stream = agent.stream("hello")
    await entered.wait()
    assert agent.active_cancellation is not None
    assert agent.state.pending_tool_calls == frozenset()
    release.set()
    await stream.result()

    snapshot = agent.state
    assert not snapshot.is_streaming
    assert snapshot.streaming_message is None
    assert snapshot.error_message is None


def test_queue_peek_is_non_consuming_and_prefers_steering():
    agent = Agent(model=ScriptedModelAdapter([]), config=AgentConfig(steering_mode="one-at-a-time"))
    agent.steer("first")
    agent.steer("second")
    agent.follow_up("later")

    assert [message.text for message in agent.peek_queued_messages()] == ["first"]
    assert [message.text for message in agent.peek_queued_messages()] == ["first"]
    agent.clear_steering_queue()
    assert [message.text for message in agent.peek_queued_messages()] == ["later"]


@pytest.mark.asyncio
async def test_reset_preserves_system_tool_baseline_and_clears_runtime_queues():
    tool = Tool("work", "work", Empty, _execute)
    config = AgentConfig()
    model = ScriptedModelAdapter([AgentMessage.assistant("done")])
    agent = Agent(model=model, system_prompt="base", tools=[tool], config=config)

    await agent.run("conversation")
    agent.steer("steering")
    agent.follow_up("follow-up")
    agent.reset()

    assert [(message.role, message.text) for message in agent.messages] == [("system", "base")]
    assert agent.context.tools == [tool]
    assert agent.peek_queued_messages() == []
    assert agent.state.messages[0].text == "base"
    assert agent.state.error_message is None
    assert agent.model is model
    assert agent.config is config


@pytest.mark.asyncio
async def test_reset_is_rejected_while_model_is_active():
    started = asyncio.Event()
    never = asyncio.Event()

    class Never:
        async def stream(self, *, system_prompt, messages, tools, cancellation):
            started.set()
            await never.wait()
            yield  # pragma: no cover

    agent = Agent(model=Never())
    stream = agent.stream("hello")
    await started.wait()
    with pytest.raises(RuntimeError, match="already processing"):
        agent.reset()
    agent.abort()
    await stream.result()
