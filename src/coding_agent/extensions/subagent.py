from __future__ import annotations

from pydantic import BaseModel, Field

from beta_agent import Agent, SessionTree, ToolResult
from beta_agent.extensions import ExtensionAPI, ExtensionTool

CHILD_MODEL_FACTORY_SERVICE = "child_model_factory"


class SubagentArgs(BaseModel):
    task: str = Field(description="委托给子 agent 的独立任务")


def subagent_extension(api: ExtensionAPI) -> None:
    """Register an opt-in subagent Tool for the Coding Agent product layer.

    The child model is provided through ``RuntimeConfig.services`` under
    ``child_model_factory``. The parent only receives the final child answer as
    a normal ToolResult; the child's internal history stays isolated.
    """

    async def execute(args: SubagentArgs, ctx, tool_ctx) -> ToolResult:
        del tool_ctx
        factory = ctx.config.services.get(CHILD_MODEL_FACTORY_SERVICE)
        if not callable(factory):
            raise RuntimeError(
                "CodingAgentOptions.child_model_factory is required when subagent_extension is enabled"
            )

        child_session = SessionTree()
        child = Agent(
            model=factory(),
            system_prompt="你是子 agent，独立完成委托任务。",
        )
        messages = await child.run(args.task)
        for message in messages:
            child_session.append_message(message)

        replies = [message for message in messages if message.role == "assistant"]
        if not replies:
            raise RuntimeError("子 agent 没有返回 assistant 消息")

        return ToolResult(
            content=replies[-1].text,
            details={"child_messages": len(child_session.get_branch())},
        )

    api.register_tool(
        ExtensionTool(
            name="subagent",
            description="把独立任务交给隔离的子 agent 执行",
            args_model=SubagentArgs,
            handler=execute,
        )
    )


# Conventional module-level factory name for directory-based extension loading.
extension = subagent_extension

__all__ = [
    "CHILD_MODEL_FACTORY_SERVICE",
    "SubagentArgs",
    "extension",
    "subagent_extension",
]
