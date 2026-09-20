from __future__ import annotations

import json

import pytest

from beta_agent import (
    Agent,
    AgentConfig,
    AgentMessage,
    ImageContent,
    Message,
    ProviderMessage,
    ScriptedModelAdapter,
    SessionTree,
    TextContent,
    ToolCall,
    default_convert_to_llm,
)
from beta_agent.adapters.openai_compatible import OpenAICompatibleAdapter


def test_default_converter_strips_runtime_metadata_and_custom_messages():
    messages = [
        AgentMessage.user("hello", delivery="steering", secret_runtime_metadata="x"),
        AgentMessage(role="custom", content=[TextContent("internal")], metadata={"extension": "x"}),
    ]

    converted = default_convert_to_llm(messages)

    assert len(converted) == 1
    assert isinstance(converted[0], ProviderMessage)
    assert converted[0].role == "user"
    assert converted[0].text == "hello"
    assert not hasattr(converted[0], "metadata")
    assert not hasattr(converted[0], "delivery")


def test_converter_preserves_assistant_tool_calls_and_tool_text_without_error_metadata():
    call = ToolCall(id="call-1", name="lookup", arguments={"q": "x"})
    messages = [
        AgentMessage.assistant("checking", tool_calls=[call], stop_reason="tool_calls"),
        AgentMessage.tool_result(
            tool_call_id="call-1",
            name="lookup",
            content="not found",
            is_error=True,
            details={"stage": "tool_execute"},
        ),
    ]

    converted = default_convert_to_llm(messages)

    assert converted[0].text == "checking"
    assert converted[0].tool_calls == [call]
    assert converted[1].role == "tool"
    assert converted[1].tool_call_id == "call-1"
    assert converted[1].name == "lookup"
    assert converted[1].text == "not found"
    assert not hasattr(converted[1], "is_error")
    assert not hasattr(converted[1], "metadata")


@pytest.mark.asyncio
async def test_transform_runs_before_converter_and_model_receives_provider_messages():
    order: list[str] = []
    model = ScriptedModelAdapter([AgentMessage.assistant("done")])

    async def transform(messages, cancellation):
        order.append("transform_context")
        assert all(isinstance(message, AgentMessage) for message in messages)
        return messages

    async def convert(messages, cancellation):
        order.append("convert_to_llm")
        converted = default_convert_to_llm(messages)
        assert all(isinstance(message, ProviderMessage) for message in converted)
        return converted

    class RecordingModel(ScriptedModelAdapter):
        async def stream(self, *, system_prompt, messages, tools, cancellation=None):
            order.append("model.stream")
            assert all(isinstance(message, ProviderMessage) for message in messages)
            async for event in super().stream(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                cancellation=cancellation,
            ):
                yield event

    model = RecordingModel([AgentMessage.assistant("done")])
    agent = Agent(
        model=model,
        config=AgentConfig(transform_context=transform, convert_to_llm=convert),
    )

    await agent.run("hello")

    assert order == ["transform_context", "convert_to_llm", "model.stream"]


def test_v1_session_string_content_loads_and_new_format_writes_blocks(tmp_path):
    old_entry = {
        "id": "old-1",
        "parent_id": None,
        "timestamp": "2024-01-01T00:00:00+00:00",
        "type": "message",
        "payload": {"role": "user", "content": "old message"},
    }
    old_path = tmp_path / "old.jsonl"
    old_path.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")

    old_session = SessionTree.load_jsonl(old_path)
    assert old_session.reconstruct_messages()[0].text == "old message"

    session = SessionTree()
    session.append_message(
        AgentMessage(
            role="user",
            content=[TextContent("hello"), ImageContent(url="https://example.test/a.png")],
        )
    )
    new_path = tmp_path / "new.jsonl"
    session.save_jsonl(new_path)
    lines = [json.loads(line) for line in new_path.read_text(encoding="utf-8").splitlines()]
    assert isinstance(lines[0]["payload"]["content"], list)
    assert lines[-1]["_meta"]["format_version"] == 3
    restored = SessionTree.load_jsonl(new_path).reconstruct_messages()[0]
    assert restored.text == "hello"
    assert isinstance(restored.content[1], ImageContent)


def test_newer_session_format_is_rejected_and_old_constructor_alias_remains():
    assert Message is AgentMessage
    assert Message.user("x").text == "x"
    assert Message.assistant("y", stop_reason="stop").text == "y"


def test_newer_session_format_is_rejected(tmp_path):
    future_path = tmp_path / "future.jsonl"
    future_path.write_text(
        json.dumps({"_meta": {"leaf_id": None, "format_version": 999}}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported session format version"):
        SessionTree.load_jsonl(future_path)


def test_openai_adapter_only_converts_provider_message_dtos():
    adapter = OpenAICompatibleAdapter(model="test", api_key="key")
    payload = adapter._messages(
        "system",
        [
            default_convert_to_llm([AgentMessage.user("hello", delivery="steering")])[0],
        ],
    )
    assert payload == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    with pytest.raises(TypeError, match="ProviderMessage"):
        adapter._messages("system", [AgentMessage.user("runtime message")])
