from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, Field

from beta_agent import Agent, SkillCatalog, Tool, ToolExecutionContext, ToolResult, make_read_text_file_tool
from beta_agent.adapters import OpenAICompatibleAdapter


class AddArgs(BaseModel):
    a: float = Field(description="First number")
    b: float = Field(description="Second number")


async def add(args: AddArgs, ctx: ToolExecutionContext) -> ToolResult:
    return ToolResult(content=str(args.a + args.b))


async def main() -> None:
    root = os.path.dirname(os.path.dirname(__file__))
    catalog = SkillCatalog.discover(os.path.join(root, "skills"))
    system_prompt = "You are a concise assistant. Use tools when useful."
    if catalog.skills:
        system_prompt += "\n\n" + catalog.prompt_fragment()

    model = OpenAICompatibleAdapter(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            Tool(name="add", description="Add two numbers.", args_model=AddArgs, handler=add),
            make_read_text_file_tool(root),
        ],
    )

    stream = agent.stream("What is 12 + 30? If a relevant skill exists, inspect it first.")
    async for event in stream:
        if event.type == "message_update" and event.message and event.message.role == "assistant":
            print("\r" + event.message.content, end="", flush=True)
        elif event.type == "tool_execution_start":
            print(f"\n[tool] {event.tool_name} {event.args}")
    await stream.result()
    print()


if __name__ == "__main__":
    asyncio.run(main())
