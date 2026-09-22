from __future__ import annotations

import asyncio
from pathlib import Path

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
from coding_agent import CodingAgentOptions, create_coding_agent
from coding_agent.tools import create_bash_tool


class NoArgs(BaseModel):
    pass


class NeverModel:
    def __init__(self, *, partial: bool = False):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.never = asyncio.Event()
        self.partial = partial

    async def stream(self, *, system_prompt, messages, tools, cancellation):
        try:
            if self.partial:
                yield ModelEvent("start", AgentMessage.assistant("partial"))
                yield ModelEvent("update", AgentMessage.assistant("partial text"))
            self.started.set()
            await self.never.wait()
        finally:
            self.closed.set()


async def _events_and_result(stream):
    result = await stream.result()
    events = [event async for event in stream]
    return events, result


@pytest.mark.asyncio
async def test_abort_before_provider_completion_has_normal_aborted_lifecycle():
    model = NeverModel()
    agent = Agent(model=model)
    stream = agent.stream("hello")
    await model.started.wait()

    agent.abort()
    events, messages = await _events_and_result(stream)
    await agent.wait_for_idle()

    assert model.closed.is_set()
    assert messages[-1].role == "assistant"
    assert messages[-1].stop_reason == "aborted"
    assert events[-1].type == "agent_end"
    assert events[-1].status == "aborted"
    assert sum(event.type == "agent_end" for event in events) == 1
    assert not agent.is_running


@pytest.mark.asyncio
async def test_abort_during_model_stream_closes_partial_message_once():
    model = NeverModel(partial=True)
    agent = Agent(model=model)
    stream = agent.stream("hello")
    await model.started.wait()

    agent.abort()
    events, messages = await _events_and_result(stream)

    assistants = [message for message in messages if message.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].text == "partial text"
    assert assistants[0].stop_reason == "aborted"
    assert sum(event.type == "message_start" and event.message.role == "assistant" for event in events) == 1
    assert sum(event.type == "message_end" and event.message.role == "assistant" for event in events) == 1
    assert events[-1].status == "aborted"


@pytest.mark.asyncio
async def test_abort_during_one_tool_returns_aborted_tool_result():
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    never = asyncio.Event()

    async def execute(args, ctx):
        tool_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        return ToolResult(content="unexpected")

    class Model:
        async def stream(self, *, system_prompt, messages, tools, cancellation):
            call = ToolCall("one", "block", {})
            yield ModelEvent("start", AgentMessage.assistant("", stop_reason="tool_calls"))
            yield ModelEvent("done", AgentMessage.assistant("", tool_calls=[call], stop_reason="tool_calls"))

    agent = Agent(model=Model(), tools=[Tool("block", "block", NoArgs, execute)])
    stream = agent.stream("go")
    await tool_started.wait()
    agent.abort()
    events, messages = await _events_and_result(stream)

    tool_result = next(message for message in messages if message.role == "tool")
    assert tool_cancelled.is_set()
    assert tool_result.is_error
    assert tool_result.metadata["aborted"] is True
    assert events[-1].status == "aborted"


@pytest.mark.asyncio
async def test_abort_during_parallel_tools_preserves_source_order_and_pairing():
    started: dict[str, asyncio.Event] = {name: asyncio.Event() for name in "abc"}
    release = asyncio.Event()

    async def execute(args, ctx):
        started[ctx.tool_name].set()
        if ctx.tool_name != "a":
            await release.wait()
        return ToolResult(content=ctx.tool_name)

    class Model:
        async def stream(self, *, system_prompt, messages, tools, cancellation):
            calls = [ToolCall(name, name, {}) for name in "abc"]
            yield ModelEvent("start", AgentMessage.assistant("", stop_reason="tool_calls"))
            yield ModelEvent("done", AgentMessage.assistant("", tool_calls=calls, stop_reason="tool_calls"))

    agent = Agent(model=Model(), tools=[Tool(name, name, NoArgs, execute) for name in "abc"])
    stream = agent.stream("go")
    await asyncio.gather(*(event.wait() for event in started.values()))
    agent.abort()
    events, messages = await _events_and_result(stream)
    release.set()

    results = [message for message in messages if message.role == "tool"]
    assert [message.tool_call_id for message in results] == ["a", "b", "c"]
    assert results[0].text == "a" and not results[0].is_error
    assert results[1].metadata["aborted"] is True
    assert results[2].metadata["aborted"] is True
    assert len([event for event in events if event.type == "tool_execution_end"]) == 3
    assert events[-1].status == "aborted"


@pytest.mark.asyncio
async def test_abort_during_sequential_batch_fills_unstarted_calls():
    started = asyncio.Event()
    never = asyncio.Event()
    calls: list[str] = []

    async def execute(args, ctx):
        calls.append(ctx.tool_name)
        started.set()
        await never.wait()

    class Model:
        async def stream(self, *, system_prompt, messages, tools, cancellation):
            yield ModelEvent("start", AgentMessage.assistant("", stop_reason="tool_calls"))
            yield ModelEvent(
                "done",
                AgentMessage.assistant(
                    "",
                    tool_calls=[ToolCall(name, name, {}) for name in "abc"],
                    stop_reason="tool_calls",
                ),
            )

    agent = Agent(
        model=Model(),
        tools=[Tool(name, name, NoArgs, execute) for name in "abc"],
        config=AgentConfig(tool_execution="sequential"),
    )
    stream = agent.stream("go")
    await started.wait()
    agent.abort()
    events, messages = await _events_and_result(stream)

    results = [message for message in messages if message.role == "tool"]
    assert calls == ["a"]
    assert [message.tool_call_id for message in results] == ["a", "b", "c"]
    assert all(message.metadata["aborted"] is True for message in results)
    assert len([event for event in events if event.type == "tool_execution_start"]) == 3
    assert len([event for event in events if event.type == "tool_execution_end"]) == 3


@pytest.mark.asyncio
async def test_abort_terminates_bash_process_before_side_effect(tmp_path):
    marker = tmp_path / "marker"

    class Model:
        async def stream(self, *, system_prompt, messages, tools, cancellation):
            call = ToolCall("bash-call", "bash", {"command": f"sleep 5; touch {marker}"})
            yield ModelEvent("start", AgentMessage.assistant("", stop_reason="tool_calls"))
            yield ModelEvent("done", AgentMessage.assistant("", tool_calls=[call], stop_reason="tool_calls"))

    agent = Agent(model=Model(), tools=[create_bash_tool(tmp_path)])
    stream = agent.stream("run command")
    started = asyncio.Event()

    async def consume():
        async for event in stream:
            if event.type == "tool_execution_start":
                started.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 3)
    agent.abort()
    result = await asyncio.wait_for(stream.result(), 5)
    await asyncio.wait_for(consumer, 5)
    await agent.wait_for_idle()

    assert not marker.exists()
    assert not agent.is_running
    assert next(message for message in result if message.role == "tool").metadata["aborted"] is True


@pytest.mark.asyncio
async def test_double_abort_is_idempotent_and_run_can_be_reused():
    first_started = asyncio.Event()
    never = asyncio.Event()

    class ReusableModel:
        def __init__(self):
            self.calls = 0

        async def stream(self, *, system_prompt, messages, tools, cancellation):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                await never.wait()
                return
            yield ModelEvent("start", AgentMessage.assistant("second"))
            yield ModelEvent("done", AgentMessage.assistant("second"))

    model = ReusableModel()
    agent = Agent(model=model)
    stream = agent.stream("first")
    await first_started.wait()
    agent.abort()
    agent.abort()
    events, _ = await _events_and_result(stream)
    assert sum(event.type == "agent_end" for event in events) == 1
    assert events[-1].status == "aborted"

    messages = await agent.run("second")
    assert messages[-1].text == "second"
    assert messages[-1].stop_reason == "stop"
    assert not agent.is_running


@pytest.mark.asyncio
async def test_second_run_while_active_is_rejected_instead_of_queued():
    model = NeverModel()
    agent = Agent(model=model)
    stream = agent.stream("first")
    await model.started.wait()

    with pytest.raises(RuntimeError, match="already processing"):
        agent.stream("second")
    with pytest.raises(RuntimeError, match="already processing"):
        await agent.run("third")

    agent.abort()
    await stream.result()


@pytest.mark.asyncio
async def test_coding_runtime_abort_propagates_through_extension_host(tmp_path: Path):
    model = NeverModel()
    runtime = await create_coding_agent(CodingAgentOptions(cwd=tmp_path, model=model))
    stream = runtime.stream("hello")
    await model.started.wait()

    runtime.abort()
    await runtime.wait_for_idle()
    events, messages = await _events_and_result(stream)

    assert messages[-1].stop_reason == "aborted"
    assert events[-1].status == "aborted"
    assert not runtime.is_running
    runtime.close()


@pytest.mark.asyncio
async def test_abort_during_durable_coordinator_preparation_balances_tool_lifecycle():
    entered = asyncio.Event()
    never = asyncio.Event()

    class BlockingCoordinator:
        def __init__(self):
            self.settled = []

        def begin_batch(self):
            pass

        async def prepare_operation(self, prepared, source_index):
            if source_index == 0:
                return "handle-a"
            entered.set()
            await never.wait()

        def execution_context_kwargs(self, handle):
            return {}

        async def settle_operation(self, handle, result, is_error):
            self.settled.append(handle)
            return {}

        async def acknowledge_published(self, operation_id):
            pass

    async def execute(args, ctx):
        return ToolResult("unexpected")

    calls = [ToolCall(name, name, {}) for name in ("a", "b")]
    model = ScriptedModelAdapter([
        AgentMessage.assistant(tool_calls=calls, stop_reason="tool_calls"),
    ])
    coordinator = BlockingCoordinator()
    agent = Agent(
        model=model,
        tools=[Tool(name, name, NoArgs, execute) for name in ("a", "b")],
        config=AgentConfig(tool_coordinator=coordinator),
    )
    stream = agent.stream("go")
    await entered.wait()
    agent.abort()
    events, messages = await _events_and_result(stream)

    for call in calls:
        starts = [
            event for event in events
            if event.type == "tool_execution_start" and event.tool_call_id == call.id
        ]
        ends = [
            event for event in events
            if event.type == "tool_execution_end" and event.tool_call_id == call.id
        ]
        assert len(starts) == len(ends) == 1
        assert ends[0].is_error is True
        assert ends[0].result.content.text == "Operation aborted"

    results = [message for message in messages if message.role == "tool"]
    assert [message.tool_call_id for message in results] == ["a", "b"]
    assert all(message.metadata["aborted"] is True for message in results)
    assert coordinator.settled == ["handle-a"]
    assert agent.state.pending_tool_calls == frozenset()
    assert events[-1].status == "aborted"
