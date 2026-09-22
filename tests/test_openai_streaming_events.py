from __future__ import annotations

import json

import httpx
import pytest

from beta_agent.adapters.openai_compatible import OpenAICompatibleAdapter
from beta_agent.providers.messages import ProviderMessage


class _Response:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c", "function": {"name": "work", "arguments": "{"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": None}]},
        ]
        for chunk in chunks:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

    async def aclose(self):
        return None


class _Client:
    def __init__(self, **kwargs):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def build_request(self, method, url, *, json, headers):
        self.request = httpx.Request(method, url, json=json, headers=headers)
        return self.request

    async def send(self, request, *, stream):
        return _Response()


@pytest.mark.asyncio
async def test_openai_compatible_adapter_emits_fine_grained_events(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    adapter = OpenAICompatibleAdapter(model="test", api_key="secret")

    events = [
        event
        async for event in adapter.stream(
            system_prompt="system",
            messages=[ProviderMessage(role="user")],
            tools=[],
        )
    ]

    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "text_end",
        "toolcall_end",
        "done",
    ]
    assert events[2].delta == "hi"
    assert events[-2].tool_call is not None
    assert events[-1].partial.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_openai_toolcall_partials_are_monotonic(monkeypatch):
    class MultiResponse(_Response):
        async def aiter_lines(self):
            chunks = [
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "id": "a", "function": {"name": "first", "arguments": '{"value":'}}
                    ]}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": "1}"}}
                    ]}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 1, "id": "b", "function": {"name": "second", "arguments": '{"value":2}'}}
                    ]}, "finish_reason": "tool_calls"}],
                },
            ]
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            yield "data: [DONE]"

    class MultiClient(_Client):
        async def send(self, request, *, stream):
            return MultiResponse()

    monkeypatch.setattr(httpx, "AsyncClient", MultiClient)
    adapter = OpenAICompatibleAdapter(model="test", api_key="secret")
    events = [
        event
        async for event in adapter.stream(
            system_prompt="",
            messages=[ProviderMessage(role="user")],
            tools=[],
        )
    ]

    tool_events = [event for event in events if event.type.startswith("toolcall_")]
    snapshots = [
        (event.type, [call.id for call in event.partial.tool_calls], [call.arguments for call in event.partial.tool_calls])
        for event in tool_events
    ]
    assert snapshots == [
        ("toolcall_start", ["a"], [{}]),
        ("toolcall_delta", ["a"], [{}]),
        ("toolcall_delta", ["a"], [{"value": 1}]),
        ("toolcall_start", ["a", "b"], [{"value": 1}, {}]),
        ("toolcall_delta", ["a", "b"], [{"value": 1}, {"value": 2}]),
        ("toolcall_end", ["a", "b"], [{"value": 1}, {"value": 2}]),
        ("toolcall_end", ["a", "b"], [{"value": 1}, {"value": 2}]),
    ]


@pytest.mark.asyncio
async def test_openai_interleaved_tool_calls_end_only_after_stream_completion(monkeypatch):
    class InterleavedResponse(_Response):
        async def aiter_lines(self):
            chunks = [
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "id": "a", "function": {"name": "first", "arguments": '{"a":'}},
                    ]}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 1, "id": "b", "function": {"name": "second", "arguments": '{"b":'}},
                    ]}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": "1}"}},
                    ]}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 1, "function": {"arguments": "2}"}},
                    ]}, "finish_reason": "tool_calls"}],
                },
            ]
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            yield "data: [DONE]"

    class InterleavedClient(_Client):
        async def send(self, request, *, stream):
            return InterleavedResponse()

    monkeypatch.setattr(httpx, "AsyncClient", InterleavedClient)
    adapter = OpenAICompatibleAdapter(model="test", api_key="secret")
    events = [
        event
        async for event in adapter.stream(
            system_prompt="",
            messages=[ProviderMessage(role="user")],
            tools=[],
        )
    ]

    end_zero = next(index for index, event in enumerate(events) if event.type == "toolcall_end" and event.content_index == 0)
    last_zero_delta = max(
        index
        for index, event in enumerate(events)
        if event.type == "toolcall_delta" and event.content_index == 0
    )
    assert end_zero > last_zero_delta
    assert sum(event.type == "toolcall_start" for event in events) == 2
    assert sum(event.type == "toolcall_end" for event in events) == 2

    ends = [event for event in events if event.type == "toolcall_end"]
    assert [event.completed_tool_call.arguments for event in ends] == [{"a": 1}, {"b": 2}]
    assert all(
        next(index for index, event in enumerate(events) if event is end)
        < next(index for index, event in enumerate(events) if event.type == "done")
        for end in ends
    )
    assert events[-1].partial.tool_calls[0].arguments == {"a": 1}
    assert events[-1].partial.tool_calls[1].arguments == {"b": 2}


@pytest.mark.asyncio
async def test_openai_partial_snapshots_keep_accumulated_tool_calls(monkeypatch):
    class PartialResponse(_Response):
        async def aiter_lines(self):
            chunks = [
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "id": "a", "function": {"name": "first", "arguments": '{"a":'}},
                    ]}, "finish_reason": None}],
                },
                {"choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "answer"}, "finish_reason": None}]},
                {
                    "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": "1}"}},
                    ]}, "finish_reason": "tool_calls"}],
                },
            ]
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            yield "data: [DONE]"

    class PartialClient(_Client):
        async def send(self, request, *, stream):
            return PartialResponse()

    monkeypatch.setattr(httpx, "AsyncClient", PartialClient)
    adapter = OpenAICompatibleAdapter(model="test", api_key="secret")
    events = [
        event
        async for event in adapter.stream(
            system_prompt="",
            messages=[ProviderMessage(role="user")],
            tools=[],
        )
    ]

    reasoning = next(event for event in events if event.type == "thinking_delta")
    text = next(event for event in events if event.type == "text_delta")
    assert [call.id for call in reasoning.partial.tool_calls] == ["a"]
    assert [call.id for call in text.partial.tool_calls] == ["a"]
    assert reasoning.partial.thinking == "thinking"
    assert text.partial.text == "answer"
    assert events[-1].partial.tool_calls[0].arguments == {"a": 1}
