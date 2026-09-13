from pydantic import BaseModel
from beta_agent import Agent, Message, ScriptedModelAdapter, SessionTree, Tool, ToolCall, ToolResult
from beta_agent.extensions import ExtensionRunner, ExtensionTool, RuntimeConfig, ToolCallDecision, ToolCallEvent, bind_extensions

class NoArgs(BaseModel): pass
class ValueArgs(BaseModel): value:str
async def noop(args,ctx): return ToolResult(content="ok")

def make_runner(tmp_path): return ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(active_tools=["base"]),session=SessionTree())

async def test_factory_load_is_atomic_and_handler_errors_are_isolated(tmp_path):
    runner=make_runner(tmp_path); seen=[]
    def broken(pi):
        pi.register_command("leak",description="should not commit",handler=lambda a,c:None)
        raise RuntimeError("boom")
    def good(pi):
        async def bad(event,ctx): raise RuntimeError("handler boom")
        async def later(event,ctx): seen.append(event.message.content)
        pi.on("message_end",bad); pi.on("message_end",later)
    await runner.load([broken,good])
    assert runner.get_commands()==[]
    await runner.emit("message_end",type("E",(),{"message":Message.user("x")})())
    assert seen==["x"] and len(runner.errors)==2

async def test_context_handlers_chain_and_tool_call_short_circuits(tmp_path):
    runner=make_runner(tmp_path); order=[]
    def ext(pi):
        async def first(event,ctx): order.append("ctx1"); return [*event.messages,Message.user("A")]
        async def second(event,ctx): order.append(event.messages[-1].content); return [*event.messages,Message.user("B")]
        async def guard1(event,ctx): order.append("guard1"); return ToolCallDecision(block=True,reason="no")
        async def guard2(event,ctx): order.append("guard2")
        pi.on("context",first); pi.on("context",second); pi.on("tool_call",guard1); pi.on("tool_call",guard2)
    await runner.load([ext]); transformed=await runner.emit_context([Message.user("start")]); assert [m.content for m in transformed]==["start","A","B"] and order[:2]==["ctx1","A"]
    call=ToolCall(id="1",name="base",arguments={}); decision=await runner.emit_tool_call(ToolCallEvent(call,NoArgs(),type("C",(),{})()))
    assert decision and decision.block and order[-1]=="guard1" and "guard2" not in order

async def test_registered_tool_uses_core_runtime_and_active_tools_apply_immediately(tmp_path):
    class ExtArgs(BaseModel): value:str
    model=ScriptedModelAdapter([Message.assistant(tool_calls=[ToolCall(id="x",name="echo_ext",arguments={"value":"hi"})],stop_reason="tool_calls"),Message.assistant("done")])
    base=Tool(name="base",description="base",args_model=NoArgs,handler=noop); agent=Agent(model=model,tools=[base])
    runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(active_tools=["base", "echo_ext"]),session=SessionTree())
    def ext(pi):
        async def execute(args,ctx,tool_ctx):
            return ToolResult(content=args.value)
        pi.register_tool(ExtensionTool(name="echo_ext",description="echo",args_model=ExtArgs,handler=execute))
    await runner.load([ext]); host=bind_extensions(agent,runner)
    result=await host.run("go")
    tool_message=next(m for m in result if m.role=="tool")
    assert tool_message.content=="hi" and not tool_message.is_error and "echo_ext" in [tool.name for tool in agent.context.tools]
    host.close()

async def test_agent_events_forward_to_extensions_and_session(tmp_path):
    model=ScriptedModelAdapter([Message.assistant("hello")]); agent=Agent(model=model)
    runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(),session=SessionTree()); seen=[]
    def ext(pi):
        async def on_message(event,ctx): seen.append(event.message.content)
        pi.on("message_end",on_message)
    await runner.load([ext]); host=bind_extensions(agent,runner); await host.run("hi")
    assert seen==["hi","hello"] and [m.content for m in runner.session.reconstruct_messages()]==["hi","hello"]

async def test_extension_tool_can_enable_deferred_tool_and_report_added_names(tmp_path):
    class EnableArgs(BaseModel): pass
    model=ScriptedModelAdapter([Message.assistant(tool_calls=[ToolCall(id="e",name="enable",arguments={})],stop_reason="tool_calls"),Message.assistant("done")])
    agent=Agent(model=model)
    runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(active_tools=["enable"]),session=SessionTree())
    def ext(pi):
        async def enable(args,ctx,tool_ctx):
            ctx.set_active_tools(["enable", "hidden"])
            return ToolResult(content="enabled")
        async def hidden(args,ctx,tool_ctx): return ToolResult(content="hidden")
        pi.register_tool(ExtensionTool(name="enable",description="enable hidden",args_model=EnableArgs,handler=enable))
        pi.register_tool(ExtensionTool(name="hidden",description="deferred",args_model=EnableArgs,handler=hidden))
    await runner.load([ext]); host=bind_extensions(agent,runner); await host.run("go")
    result=next(m for m in agent.messages if m.role=="tool")
    assert result.metadata["added_tool_names"] == ["hidden"]
    assert [tool.name for tool in agent.context.tools]==["enable", "hidden"]

async def test_directory_loader_isolated_import_failures(tmp_path):
    from beta_agent.extensions import load_extensions_from_dir
    from beta_agent.extensions.types import ExtensionError
    (tmp_path / "good.py").write_text("def extension(pi):\n    pi.register_command('hello', description='hello', handler=lambda args, ctx: None)\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("raise RuntimeError('import boom')\n", encoding="utf-8")
    errors: list[ExtensionError] = []
    factories = load_extensions_from_dir(tmp_path, errors=errors)
    runner = make_runner(tmp_path)
    await runner.load(factories)
    assert [c.name for c in runner.get_commands()] == ["hello"]
    assert len(errors) == 1 and errors[0].stage == "import"
