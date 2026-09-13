from pydantic import BaseModel, Field
from beta_agent import Agent, SessionTree, ToolResult
from beta_agent.extensions import ExtensionAPI, ExtensionTool
class SubagentArgs(BaseModel):
    task:str=Field(description="委托给子 agent 的独立任务")
def extension(pi:ExtensionAPI)->None:
    async def execute(args,ctx,tool_ctx):
        factory=ctx.config.services.get("child_model_factory")
        if not callable(factory):raise RuntimeError("RuntimeConfig.services 缺少 child_model_factory")
        child_session=SessionTree(); child=Agent(model=factory(),system_prompt="你是子 agent，独立完成委托任务。")
        messages=await child.run(args.task)
        for message in messages:
            child_session.append_message(message)
        replies=[m for m in messages if m.role=="assistant"]
        if not replies:raise RuntimeError("子 agent 没有返回 assistant 消息")
        return ToolResult(content=replies[-1].content,details={"child_messages":len(child_session.get_branch())})
    pi.register_tool(ExtensionTool(name="subagent",description="把任务交给独立子 agent 执行",args_model=SubagentArgs,handler=execute))
