from __future__ import annotations

import asyncio

from beta_agent import Agent, AgentMessage, ScriptedModelAdapter, SessionTree
from beta_agent.extensions import ExtensionRunner, RuntimeConfig, bind_extensions


async def test_agent_end_subscribers_settle_before_wait_for_idle():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen_tokens = []
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("done")]))

    async def listener(event, cancellation):
        seen_tokens.append(cancellation)
        if event.type == "agent_end":
            entered.set()
            await release.wait()

    unsubscribe = agent.subscribe(listener)
    stream = agent.stream("hello")
    idle_wait = asyncio.create_task(agent.wait_for_idle())
    await asyncio.wait_for(entered.wait(), timeout=2)

    assert agent.is_running
    assert not idle_wait.done()
    assert seen_tokens
    assert all(token is seen_tokens[0] for token in seen_tokens)
    assert agent._active_run is not None and seen_tokens[0] is agent._active_run.token

    release.set()
    await asyncio.wait_for(idle_wait, timeout=2)
    await stream.result()
    assert not agent.is_running
    unsubscribe()


async def test_sync_subscriber_order_and_idempotent_unsubscribe():
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("one"), AgentMessage.assistant("two")]))
    seen = []
    first = agent.subscribe(lambda event: seen.append(("first", event.type)))
    agent.subscribe(lambda event: seen.append(("second", event.type)))

    await agent.run("one")
    assert all(seen[index][0] == "first" and seen[index + 1][0] == "second" for index in range(0, len(seen), 2))
    first()
    first()
    seen.clear()
    await agent.run("two")

    assert seen
    assert all(name == "second" for name, _ in seen)


async def test_subscriber_failure_enters_normalized_error_lifecycle():
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("unused")]))

    async def broken_listener(event, cancellation):
        if event.type == "message_end" and event.message is not None and event.message.role == "user":
            raise OSError("persistence unavailable")

    agent.subscribe(broken_listener)
    stream = agent.stream("hello")
    events = [event async for event in stream]
    await stream.result()

    assert agent.last_error is not None
    assert agent.last_error.stage == "event_listener"
    assert [event.type for event in events][-2:] == ["agent_error", "agent_end"]
    assert events[-1].status == "error"


async def test_agent_end_subscriber_failure_changes_terminal_status_to_error():
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("done")]))

    async def broken_end(event, cancellation):
        if event.type == "agent_end" and event.status == "completed":
            raise RuntimeError("settlement failed")

    agent.subscribe(broken_end)
    stream = agent.stream("hello")
    events = [event async for event in stream]
    await stream.result()

    assert agent.last_error is not None and agent.last_error.stage == "event_listener"
    assert events[-1].type == "agent_end"
    assert events[-1].status == "error"


async def test_extension_persistence_failure_uses_agent_error_lifecycle(tmp_path):
    agent = Agent(model=ScriptedModelAdapter([AgentMessage.assistant("unused")]))
    runner = ExtensionRunner(cwd=tmp_path, config=RuntimeConfig(), session=SessionTree())
    host = bind_extensions(agent, runner)

    def fail_append(message):
        raise OSError("disk unavailable")

    runner.session.append_message = fail_append
    stream = host.stream("hello")
    events = [event async for event in stream]
    await stream.result()

    assert agent.last_error is not None and agent.last_error.stage == "event_listener"
    assert events[-1].status == "error"
    host.close()
