from __future__ import annotations

from pydantic import BaseModel

from beta_agent import Agent, Message, ScriptedModelAdapter, SessionTree, Tool, ToolCall, ToolResult
from beta_agent.extensions import ExtensionRunner, RuntimeConfig, ToolCallEvent, bind_extensions
from coding_agent.extensions import permission_gate_extension, plan_mode_extension, subagent_extension


class BashArgs(BaseModel):
    command: str


class EmptyArgs(BaseModel):
    pass


async def ok(args, ctx):
    return ToolResult(content="ok")


async def test_permission_gate_blocks_dangerous_bash_through_core_tool_runtime(tmp_path):
    executed = []

    async def bash(args, ctx):
        executed.append(args.command)
        return ToolResult(content="executed")

    model = ScriptedModelAdapter(
        [
            Message.assistant(
                tool_calls=[ToolCall("1", "bash", {"command": "rm -rf /tmp/demo"})],
                stop_reason="tool_calls",
            ),
            Message.assistant("blocked acknowledged"),
        ]
    )
    agent = Agent(model=model, tools=[Tool("bash", "bash", BashArgs, bash)])
    runner = ExtensionRunner(
        cwd=tmp_path,
        config=RuntimeConfig(active_tools=["bash"]),
        session=SessionTree(),
    )
    await runner.load([permission_gate_extension])
    host = bind_extensions(agent, runner)

    await host.run("dangerous")
    result = next(message for message in agent.messages if message.role == "tool")
    assert result.is_error
    assert "permission-gate" in result.content
    assert executed == []


async def test_plan_mode_reconfigures_tools_guards_bash_and_injects_context(tmp_path):
    tools = [
        Tool("bash", "bash", BashArgs, ok),
        Tool("write", "write", EmptyArgs, ok),
        Tool("write_file", "write file", EmptyArgs, ok),
        Tool("search", "search", EmptyArgs, ok),
    ]
    agent = Agent(model=ScriptedModelAdapter([Message.assistant("planned")]), tools=tools)
    runner = ExtensionRunner(
        cwd=tmp_path,
        config=RuntimeConfig(active_tools=[tool.name for tool in tools]),
        session=SessionTree(),
    )
    await runner.load([plan_mode_extension])
    host = bind_extensions(agent, runner)

    await host.run_command("/plan")
    assert [tool.name for tool in agent.context.tools] == ["bash", "search"]

    unsafe = await runner.emit_tool_call(
        ToolCallEvent(
            ToolCall("1", "bash", {"command": "rm x"}),
            BashArgs(command="rm x"),
            agent.context,
        )
    )
    assert unsafe and unsafe.block

    transformed = await runner.emit_context([Message.user("inspect")])
    assert "PLAN MODE ACTIVE" in transformed[-1].content

    await host.run_command("/plan")
    assert [tool.name for tool in agent.context.tools] == ["bash", "write", "write_file", "search"]


async def test_subagent_is_a_normal_tool_and_child_history_stays_out_of_parent(tmp_path):
    parent_model = ScriptedModelAdapter(
        [
            Message.assistant(
                tool_calls=[ToolCall("sub", "subagent", {"task": "child task"})],
                stop_reason="tool_calls",
            ),
            Message.assistant("parent done"),
        ]
    )
    config = RuntimeConfig(
        active_tools=[],
        services={
            "child_model_factory": lambda: ScriptedModelAdapter([Message.assistant("child answer")]),
        },
    )
    runner = ExtensionRunner(cwd=tmp_path, config=config, session=SessionTree())
    await runner.load([subagent_extension])
    parent = Agent(model=parent_model)
    host = bind_extensions(parent, runner)

    await host.run("delegate")
    assistant_contents = [message.content for message in parent.messages if message.role == "assistant"]
    assert assistant_contents in (["", "parent done"], ["parent done"])

    tool_result = next(message for message in parent.messages if message.role == "tool")
    assert tool_result.content == "child answer"
    assert tool_result.metadata["details"]["child_messages"] == 2
    assert all(
        message.content != "child task" and message.content != "child answer"
        for message in parent.messages
        if message.role != "tool"
    )
