from __future__ import annotations

import pytest

from beta_agent import Agent, AgentConfig, AgentMessage, ScriptedModelAdapter


def _user_texts(call):
    return [message.text for message in call if message.role == "user"]


async def test_steering_defaults_to_one_at_a_time_and_preserves_fifo():
    model = ScriptedModelAdapter([AgentMessage.assistant("r1"), AgentMessage.assistant("r2"), AgentMessage.assistant("r3")])
    agent = Agent(model=model)
    agent.steer("s1")
    agent.steer("s2")
    agent.steer("s3")

    await agent.run("start")

    assert agent.config.steering_mode == "one-at-a-time"
    assert [_user_texts(call) for call in model.calls] == [
        ["start", "s1"],
        ["start", "s1", "s2"],
        ["start", "s1", "s2", "s3"],
    ]
    assert not agent.has_queued_messages()


async def test_all_mode_delivers_steering_batch_together():
    model = ScriptedModelAdapter([AgentMessage.assistant("done")])
    agent = Agent(model=model, config=AgentConfig(steering_mode="all"))
    for text in ("s1", "s2", "s3"):
        agent.steer(text)

    await agent.run("start")

    assert len(model.calls) == 1
    assert _user_texts(model.calls[0]) == ["start", "s1", "s2", "s3"]


async def test_follow_up_defaults_to_one_at_a_time_and_all_mode_is_supported():
    one_model = ScriptedModelAdapter(
        [
            AgentMessage.assistant("r1"),
            AgentMessage.assistant("r2"),
            AgentMessage.assistant("r3"),
            AgentMessage.assistant("r4"),
        ]
    )
    one_agent = Agent(model=one_model)
    for text in ("f1", "f2", "f3"):
        one_agent.follow_up(text)

    await one_agent.run("start")

    assert one_agent.config.follow_up_mode == "one-at-a-time"
    assert [_user_texts(call) for call in one_model.calls] == [
        ["start"],
        ["start", "f1"],
        ["start", "f1", "f2"],
        ["start", "f1", "f2", "f3"],
    ]

    all_model = ScriptedModelAdapter([AgentMessage.assistant("r1"), AgentMessage.assistant("r2")])
    all_agent = Agent(model=all_model, config=AgentConfig(follow_up_mode="all"))
    all_agent.follow_up("f1")
    all_agent.follow_up("f2")
    await all_agent.run("start")

    assert [_user_texts(call) for call in all_model.calls] == [
        ["start"],
        ["start", "f1", "f2"],
    ]


async def test_continue_from_assistant_consumes_steering_before_follow_up():
    model = ScriptedModelAdapter(
        [AgentMessage.assistant("first"), AgentMessage.assistant("continued"), AgentMessage.assistant("followed")]
    )
    agent = Agent(model=model)
    await agent.run("start")

    with pytest.raises(ValueError, match="Cannot continue from an assistant"):
        agent.continue_stream()

    agent.follow_up("follow-up")
    agent.steer("steering")
    stream = agent.continue_stream()
    await stream.result()

    assert _user_texts(model.calls[1]) == ["start", "steering"]
    assert _user_texts(model.calls[2]) == ["start", "steering", "follow-up"]
    assert not agent.has_queued_messages()


async def test_continue_from_assistant_can_consume_follow_up_without_steering():
    model = ScriptedModelAdapter([AgentMessage.assistant("first"), AgentMessage.assistant("continued")])
    agent = Agent(model=model)
    await agent.run("start")
    agent.follow_up("resume with this")

    stream = agent.continue_stream()
    await stream.result()

    assert _user_texts(model.calls[1]) == ["start", "resume with this"]


def test_queue_clear_helpers_and_has_queued_messages():
    agent = Agent(model=ScriptedModelAdapter([]))
    agent.steer("s")
    agent.follow_up("f")
    assert agent.has_queued_messages()

    agent.clear_steering_queue()
    assert agent.has_queued_messages()
    agent.clear_all_queues()
    assert not agent.has_queued_messages()
