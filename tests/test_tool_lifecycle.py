from __future__ import annotations

import asyncio

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
from beta_agent.tools import AfterToolCallPatch


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
