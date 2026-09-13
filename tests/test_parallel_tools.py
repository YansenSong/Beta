import asyncio

from pydantic import BaseModel

from beta_agent import Agent, Message, ScriptedModelAdapter, Tool, ToolCall, ToolExecutionContext, ToolResult


class DelayArgs(BaseModel):
    label: str
    delay: float


async def delayed(args: DelayArgs, ctx: ToolExecutionContext) -> ToolResult:
    await asyncio.sleep(args.delay)
    return ToolResult(content=args.label)


async def test_parallel_completion_events_but_source_order_history():
    calls = [
        ToolCall(id="slow", name="delay", arguments={"label": "slow", "delay": 0.04}),
        ToolCall(id="fast", name="delay", arguments={"label": "fast", "delay": 0.005}),
    ]
    model = ScriptedModelAdapter(
        [Message.assistant(tool_calls=calls, stop_reason="tool_calls"), Message.assistant("done")]
    )
    agent = Agent(
        model=model,
        tools=[Tool(name="delay", description="delay", args_model=DelayArgs, handler=delayed)],
    )

    ends = []
    stream = agent.stream("go")
    async for event in stream:
        if event.type == "tool_execution_end":
            ends.append(event.tool_call_id)
    await stream.result()

    assert ends == ["fast", "slow"]
    tool_history = [m.tool_call_id for m in agent.messages if m.role == "tool"]
    assert tool_history == ["slow", "fast"]
