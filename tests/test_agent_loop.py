from pydantic import BaseModel

from beta_agent import Agent, Message, ScriptedModelAdapter, Tool, ToolCall, ToolExecutionContext, ToolResult


class AddArgs(BaseModel):
    a: int
    b: int


async def add(args: AddArgs, ctx: ToolExecutionContext) -> ToolResult:
    return ToolResult(content=str(args.a + args.b))


async def test_tool_driven_loop_and_events():
    model = ScriptedModelAdapter(
        [
            Message.assistant(
                tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 12, "b": 30})],
                stop_reason="tool_calls",
            ),
            Message.assistant("42", stop_reason="stop"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool(name="add", description="add", args_model=AddArgs, handler=add)],
    )

    stream = agent.stream("calculate")
    event_types = []
    async for event in stream:
        event_types.append(event.type)
    result = await stream.result()

    assert result[-1].content == "42"
    assert [m.role for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    assert "tool_execution_start" in event_types
    assert "tool_execution_end" in event_types
    assert event_types[0] == "agent_start"
    assert event_types[-1] == "agent_end"
