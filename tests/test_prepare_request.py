from __future__ import annotations

import pytest
from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentMessage,
    ProviderRequestOptions,
    ProviderRequestOptionsPatch,
    RequestUpdate,
    ScriptedModelAdapter,
    Tool,
    ToolCall,
    ToolResult,
)


class Empty(BaseModel):
    pass


class RecordingModel(ScriptedModelAdapter):
    def __init__(self, responses):
        super().__init__(responses)
        self.options = []

    async def stream(self, *, system_prompt, messages, tools, request_options=None, cancellation=None):
        self.options.append(request_options)
        async for event in super().stream(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            request_options=request_options,
            cancellation=cancellation,
        ):
            yield event


@pytest.mark.asyncio
async def test_prepare_request_runs_once_per_logical_request_and_merges_options():
    seen = []

    async def execute(args, ctx):
        return ToolResult("ok")

    async def prepare(request, cancellation=None):
        seen.append(request)
        return RequestUpdate(
            request_options=ProviderRequestOptionsPatch(
                headers={"x-new": "yes"}, metadata={"request": len(seen)}
            )
        )

    model = RecordingModel(
        [
            AgentMessage.assistant(
                tool_calls=[ToolCall("c", "work", {})], stop_reason="tool_calls"
            ),
            AgentMessage.assistant("done"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[Tool("work", "work", Empty, execute)],
        config=AgentConfig(
            provider_request_options=ProviderRequestOptions(
                headers={"base": "ok"}, metadata={"base": True}
            ),
            prepare_request=prepare,
        ),
    )

    await agent.run("go")

    assert len(seen) == 2
    assert len(model.calls) == 2
    assert [options.headers for options in model.options] == [
        {"base": "ok", "x-new": "yes"},
        {"base": "ok", "x-new": "yes"},
    ]
    assert [options.metadata for options in model.options] == [
        {"base": True, "request": 1},
        {"base": True, "request": 2},
    ]
    assert seen[0].context is agent.context


@pytest.mark.asyncio
async def test_prepare_request_can_replace_context_and_model():
    replacement = RecordingModel([AgentMessage.assistant("replacement")])
    original = RecordingModel([AgentMessage.assistant("unused")])

    from beta_agent import AgentContext

    replacement_context = AgentContext(messages=[AgentMessage.user("from hook")], tools=[])

    async def prepare(request, cancellation=None):
        return RequestUpdate(context=replacement_context, model=replacement)

    agent = Agent(
        model=original,
        messages=[AgentMessage.user("original")],
        config=AgentConfig(prepare_request=prepare),
    )

    result = await agent.run("ignored")

    assert result[-1].text == "replacement"
    assert original.calls == []
    assert len(replacement.calls) == 1
    assert [message.text for message in agent.context.messages if message.role == "user"] == ["from hook"]
