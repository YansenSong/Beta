import importlib.util
from pathlib import Path
from pydantic import BaseModel
from beta_agent import Agent, Message, ScriptedModelAdapter, SessionTree, Tool, ToolCall, ToolResult
from beta_agent.extensions import ExtensionRunner, RuntimeConfig, ToolCallEvent, bind_extensions
ROOT=Path(__file__).resolve().parents[1]
def load_example(name):
    path=ROOT/"examples"/"extensions"/f"{name}.py"; spec=importlib.util.spec_from_file_location(f"example_{name}",path); mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(mod); return mod
class BashArgs(BaseModel): command:str
class EmptyArgs(BaseModel): pass
async def ok(args,ctx):return ToolResult(content="ok")

async def test_permission_gate_blocks_dangerous_bash_through_core_tool_runtime(tmp_path):
    mod=load_example("permission_gate"); executed=[]
    async def bash(args,ctx): executed.append(args.command); return ToolResult(content="executed")
    model=ScriptedModelAdapter([Message.assistant(tool_calls=[ToolCall("1","bash",{"command":"rm -rf /tmp/demo"})],stop_reason="tool_calls"),Message.assistant("blocked acknowledged")])
    agent=Agent(model=model,tools=[Tool("bash","bash",BashArgs,bash)])
    runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(active_tools=["bash"]),session=SessionTree()); await runner.load([mod.extension]); bind_extensions(agent,runner)
    await agent.run("dangerous")
    result=next(m for m in agent.messages if m.role=="tool")
    assert result.is_error and "permission-gate" in result.content and executed==[]

async def test_plan_mode_reconfigures_tools_guards_bash_and_injects_context(tmp_path):
    mod=load_example("plan_mode"); tools=[Tool("bash","bash",BashArgs,ok),Tool("write","write",EmptyArgs,ok),Tool("write_file","write file",EmptyArgs,ok),Tool("search","search",EmptyArgs,ok)]
    agent=Agent(model=ScriptedModelAdapter([Message.assistant("planned")]),tools=tools); runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(active_tools=[t.name for t in tools]),session=SessionTree()); await runner.load([mod.extension]); host=bind_extensions(agent,runner)
    await host.run_command("/plan"); assert [tool.name for tool in agent.context.tools]==["bash","search"]
    unsafe=await runner.emit_tool_call(ToolCallEvent(ToolCall("1","bash",{"command":"rm x"}),BashArgs(command="rm x"),agent.context)); assert unsafe and unsafe.block
    transformed=await runner.emit_context([Message.user("inspect")]); assert "PLAN MODE ACTIVE" in transformed[-1].content
    await host.run_command("/plan"); assert [tool.name for tool in agent.context.tools]==["bash","write","write_file","search"]

async def test_subagent_is_a_normal_tool_and_child_history_stays_out_of_parent(tmp_path):
    mod=load_example("subagent")
    parent_model=ScriptedModelAdapter([Message.assistant(tool_calls=[ToolCall("sub","subagent",{"task":"child task"})],stop_reason="tool_calls"),Message.assistant("parent done")])
    config=RuntimeConfig(active_tools=[],services={"child_model_factory":lambda:ScriptedModelAdapter([Message.assistant("child answer")])})
    runner=ExtensionRunner(cwd=tmp_path,config=config,session=SessionTree()); await runner.load([mod.extension]); parent=Agent(model=parent_model); host=bind_extensions(parent,runner)
    await host.run("delegate")
    assert [m.content for m in parent.messages if m.role=="assistant"]==["","parent done"] or [m.content for m in parent.messages if m.role=="assistant"]==["parent done"]
    tool_result=next(m for m in parent.messages if m.role=="tool"); assert tool_result.content=="child answer" and tool_result.metadata["details"]["child_messages"]==2
    assert all(m.content!="child task" and m.content!="child answer" for m in parent.messages if m.role!="tool")
