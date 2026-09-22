from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentContext,
    AgentMessage,
    ImageContent,
    ScriptedModelAdapter,
    SessionTree,
    TextContent,
    Tool,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
    ToolRuntime,
)
from beta_agent.harness.tool import AfterToolCallPatch
from beta_agent.harness.durable import MemoryStorage
from beta_agent.harness.durable.runtime import DurableToolCoordinator, recover_durable_runtime
from beta_agent.runtime.cancellation import CancellationToken


class EmptyArgs(BaseModel):
    pass


async def _noop_emit(event):
    return None


async def test_late_progress_is_ignored_after_tool_execution_end():
    release = asyncio.Event()
    background: list[asyncio.Task] = []
    events = []

    async def execute(args, ctx):
        async def report_late():
            await release.wait()
            await ctx.progress("late")

        background.append(asyncio.create_task(report_late()))
        return ToolResult("finished")

    tool = Tool("work", "work", EmptyArgs, execute)
    runtime = ToolRuntime()
    await runtime.execute_batch(
        context=AgentContext(system_prompt="", tools=[tool]),
        calls=[ToolCall("call", "work", {})],
        emit=lambda event: _record(events, event),
    )
    end_index = next(index for index, event in enumerate(events) if event.type == "tool_execution_end")
    release.set()
    await asyncio.gather(*background)

    assert not any(event.type == "tool_execution_update" for event in events)
    assert events[end_index].type == "tool_execution_end"


async def _record(events, event):
    events.append(event)


async def test_rich_tool_result_usage_patch_and_session_roundtrip(tmp_path):
    async def execute(args, ctx):
        return ToolResult(
            content=[TextContent("original"), ImageContent(url="https://example.test/image.png")],
            details={"source": "tool"},
            usage={"input_tokens": 3},
        )

    async def after(call, args, result, is_error, context):
        return AfterToolCallPatch(
            content=[TextContent("replacement"), ImageContent(data="YWJj", media_type="image/png")],
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    model = ScriptedModelAdapter(
        [
            AgentMessage.assistant(tool_calls=[ToolCall("call", "rich", {})], stop_reason="tool_calls"),
            AgentMessage.assistant("done"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool("rich", "rich result", EmptyArgs, execute)],
        config=AgentConfig(after_tool_call=after),
    )
    await agent.run("show image")

    result = next(message for message in agent.messages if message.role == "tool")
    assert result.text == "replacement"
    assert isinstance(result.content[1], ImageContent)
    assert result.content[1].data == "YWJj"
    assert result.metadata["details"] == {"source": "tool"}
    assert result.metadata["usage"] == {"input_tokens": 4, "output_tokens": 2}

    session = SessionTree()
    session.append_message(result)
    path = tmp_path / "rich.jsonl"
    session.save_jsonl(path)
    restored = SessionTree.load_jsonl(path).reconstruct_messages()[0]
    assert restored.text == "replacement"
    assert isinstance(restored.content[1], ImageContent)
    assert restored.content[1].data == "YWJj"
    assert restored.metadata["usage"] == {"input_tokens": 4, "output_tokens": 2}


async def test_text_tool_result_is_normalized_to_content_blocks():
    result = ToolResult("plain text")
    assert result.content.text == "plain text"


@pytest.mark.asyncio
async def test_cancellation_during_after_hook_settles_completed_tool_effect(tmp_path):
    storage = MemoryStorage()
    await storage.open()
    effects = 0

    async def execute(args, ctx):
        nonlocal effects
        effects += 1
        return ToolResult("effect result", details={"effect": effects})

    async def after(value, cancellation=None):
        cancellation.cancel()
        raise asyncio.CancelledError()

    tool = Tool("work", "work", EmptyArgs, execute, replay_policy="safe")
    coordinator = DurableToolCoordinator(storage, task_id="task", run_id="run")
    runtime = ToolRuntime(after_tool_call=after, coordinator=coordinator)
    batch = await runtime.execute_batch(
        context=AgentContext(system_prompt="", tools=[tool]),
        calls=[ToolCall("call", "work", {})],
        emit=_noop_emit,
        cancellation=CancellationToken(),
    )

    message = batch.messages[0]
    assert effects == 1
    assert batch.aborted is True
    assert message.text == "effect result"
    assert message.metadata["aborted"] is False
    assert message.metadata["details"]["stage"] == "after_tool_call"
    assert message.metadata["details"]["after_hook_cancelled"] is True

    pending = await storage.scan_effect_pending_operations()
    assert pending == []
    operation = await storage.get_operation(message.metadata["durable_operation_id"])
    assert operation is not None
    assert operation.status == "outcome_ready"

    session = SessionTree()
    report = await recover_durable_runtime(storage, session, tmp_path / "session.jsonl", [tool])
    assert report.replayed_operations == []
    assert effects == 1
    assert len(session.entries) == 1
    assert (await storage.get_operation(operation.id)).status == "completed"
    await storage.close()


@pytest.mark.asyncio
async def test_batch_cancellation_does_not_rewrite_completed_effect_as_aborted():
    storage = MemoryStorage()
    await storage.open()
    entered_after = asyncio.Event()
    effects = 0

    async def execute(args, ctx):
        nonlocal effects
        effects += 1
        return ToolResult("effect result")

    async def after(value, cancellation=None):
        entered_after.set()
        await asyncio.Event().wait()

    tool = Tool("work", "work", EmptyArgs, execute)
    coordinator = DurableToolCoordinator(storage, task_id="task", run_id="run")
    runtime = ToolRuntime(after_tool_call=after, coordinator=coordinator)
    task = asyncio.create_task(
        runtime.execute_batch(
            context=AgentContext(system_prompt="", tools=[tool]),
            calls=[ToolCall("call", "work", {})],
            emit=_noop_emit,
        )
    )
    await entered_after.wait()
    task.cancel()
    batch = await task

    message = batch.messages[0]
    assert effects == 1
    assert batch.aborted is True
    assert message.metadata["aborted"] is False
    assert message.metadata["details"]["after_hook_cancelled"] is True
    operation = await storage.get_operation(message.metadata["durable_operation_id"])
    assert operation is not None
    assert operation.status == "outcome_ready"
    assert await storage.scan_effect_pending_operations() == []
    await storage.close()
