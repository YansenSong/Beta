from __future__ import annotations

from pydantic import BaseModel

from beta_agent import (
    Agent,
    AgentConfig,
    AgentContext,
    AgentMessage,
    NextTurnUpdate,
    ScriptedModelAdapter,
    Tool,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
)


class EmptyArgs(BaseModel):
    pass


async def _execute(args: EmptyArgs, ctx: ToolExecutionContext) -> ToolResult:
    return ToolResult("ok")


async def test_next_turn_update_replaces_context_adds_messages_and_switches_model():
    old_model = ScriptedModelAdapter(
        [AgentMessage.assistant(tool_calls=[ToolCall("call", "work", {})], stop_reason="tool_calls")]
    )
    next_model = ScriptedModelAdapter([AgentMessage.assistant("next model"), AgentMessage.assistant("later run")])
    replacement_contexts: list[AgentContext] = []

    async def prepare(turn, cancellation):
        replacement = turn.context.clone()
        replacement_contexts.append(replacement)
        return NextTurnUpdate(
            context=replacement,
            messages=[AgentMessage.user("prepared context")],
            model=next_model,
        )

    agent = Agent(
        model=old_model,
        tools=[Tool("work", "work", EmptyArgs, _execute)],
        config=AgentConfig(prepare_next_turn=prepare),
    )
    stream = agent.stream("start")
    events = [event async for event in stream]
    result = await stream.result()

    assert agent.context is replacement_contexts[0]
    assert len(old_model.calls) == 1
    assert len(next_model.calls) == 1
    assert "prepared context" in [message.text for message in next_model.calls[0]]
    assert result[-1].text == "next model"
    prepared = next(message for message in agent.messages if message.role == "user" and message.text == "prepared context")
    assert [event.type for event in events if event.message is prepared] == ["message_start", "message_end"]

    await agent.run("again")
    assert len(next_model.calls) == 2
    assert agent.messages[-1].text == "later run"


async def test_steering_queued_during_prepare_is_delivered_with_prepared_messages():
    model = ScriptedModelAdapter(
        [
            AgentMessage.assistant(tool_calls=[ToolCall("call", "work", {})], stop_reason="tool_calls"),
            AgentMessage.assistant("continued"),
        ]
    )
    agent = Agent(model=model, tools=[Tool("work", "work", EmptyArgs, _execute)])

    async def prepare(turn):
        agent.steer("from prepare")
        return NextTurnUpdate(messages=[AgentMessage.user("prepared")])

    agent.config.prepare_next_turn = prepare
    await agent.run("start")

    assert [message.text for message in model.calls[1] if message.role == "user"] == [
        "start",
        "prepared",
        "from prepare",
    ]


async def test_prepare_next_turn_is_skipped_when_the_run_will_stop():
    prepared: list[bool] = []

    def prepare(turn):
        prepared.append(True)
        return None

    agent = Agent(
        model=ScriptedModelAdapter([AgentMessage.assistant("final")]),
        config=AgentConfig(
            prepare_next_turn=prepare,
            should_stop_after_turn=lambda turn: True,
        ),
    )
    await agent.run("start")

    assert prepared == []


async def test_legacy_prepare_context_return_remains_supported():
    replacement: list[AgentContext] = []

    def prepare(turn):
        context = turn.context.clone()
        replacement.append(context)
        return context

    agent = Agent(
        model=ScriptedModelAdapter(
            [
                AgentMessage.assistant(tool_calls=[ToolCall("call", "work", {})], stop_reason="tool_calls"),
                AgentMessage.assistant("done"),
            ]
        ),
        tools=[Tool("work", "work", EmptyArgs, _execute)],
        config=AgentConfig(prepare_next_turn=prepare),
    )
    await agent.run("start")

    assert agent.context is replacement[0]
    assert agent.messages[-1].text == "done"

