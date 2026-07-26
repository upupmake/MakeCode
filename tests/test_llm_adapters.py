import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from system.models import ModelConfig
from utils.llm_client import (
    AnthropicMessagesClient,
    AsyncBaseLLMClient,
    _client_request_active,
    _create_async_chat_client,
    build_anthropic_request_messages,
    format_anthropic_tools,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeAnthropicStream:
    def __init__(self, events, final_message):
        self._events = iter(events)
        self._final_message = final_message
        self.final_message_requested = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration

    async def get_final_message(self):
        self.final_message_requested = True
        return self._final_message


class FakeAnthropicStreamManager:
    def __init__(self, stream):
        self.stream = stream
        self.exited = False

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class FakeAnthropicClient:
    def __init__(self, manager):
        self.messages = SimpleNamespace(stream=Mock(return_value=manager))


def test_anthropic_message_conversion_extracts_system_and_groups_parallel_tool_results():
    messages = [
        {"role": "system", "content": "system one"},
        {"role": "system", "content": [{"type": "text", "text": "system two"}]},
        {"role": "user", "content": "run both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path":"a.py"}'},
                },
                {
                    "id": "call_2",
                    "name": "Search",
                    "arguments": {"query": "needle"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "Read", "content": "file"},
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "name": "Search",
            "content": "failed",
            "is_error": True,
        },
        {"role": "user", "content": "continue"},
    ]

    system, converted = build_anthropic_request_messages(messages, "claude-test")

    assert system == "system one\n\nsystem two"
    assert converted == [
        {"role": "user", "content": [{"type": "text", "text": "run both"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Read",
                    "input": {"path": "a.py"},
                },
                {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "Search",
                    "input": {"query": "needle"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "file"},
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "content": "failed",
                    "is_error": True,
                },
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ]


def test_anthropic_message_conversion_replays_native_blocks_only_for_same_model():
    native_blocks = [
        {"type": "thinking", "thinking": "summary", "signature": "sig"},
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "compaction", "content": "compact state"},
        {"type": "text", "text": "answer"},
    ]
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "summary",
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-source",
            "native_blocks": native_blocks,
        },
    }

    _, same_model = build_anthropic_request_messages([message], "claude-source")
    _, other_model = build_anthropic_request_messages([message], "claude-other")

    assert same_model[0]["content"] == native_blocks
    assert same_model[0]["content"] is not native_blocks
    assert other_model == [{
        "role": "assistant",
        "content": [{"type": "text", "text": "answer"}],
    }]


def test_anthropic_tool_conversion_uses_input_schema_and_flattens_namespaces():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a file",
                "parameters": {
                    "$defs": {"Path": {"type": "string"}},
                    "type": "object",
                    "properties": {"path": {"$ref": "#/$defs/Path"}},
                    "required": ["path"],
                },
                "strict": True,
            },
        },
        {
            "type": "namespace",
            "tools": [{
                "name": "Search",
                "description": "Search files",
                "inputSchema": {"type": "object", "properties": {}},
            }],
        },
    ]

    assert format_anthropic_tools(tools) == [
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            "strict": True,
        },
        {
            "name": "Search",
            "description": "Search files",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "max_tokens", "pause_turn", "refusal"])
async def test_anthropic_stream_builds_lossless_unified_result(stop_reason):
    events = [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="thinking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="summary"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="answer"),
        ),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use"),
        ),
    ]
    native_blocks = [
        {"type": "thinking", "thinking": "summary", "signature": "sig"},
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"path": "a.py"}},
        {"type": "compaction", "content": "compact state"},
    ]
    final_message = SimpleNamespace(
        content=native_blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=12, output_tokens=34),
    )
    stream = FakeAnthropicStream(events, final_message)
    manager = FakeAnthropicStreamManager(stream)
    raw_client = FakeAnthropicClient(manager)
    client = AnthropicMessagesClient(raw_client, "claude-test", "xhigh")
    tools = [{"name": "Read", "description": "Read", "input_schema": {"type": "object"}}]

    generated = [event async for event in client.generate_stream(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}],
        tools,
        tool_choice="Read",
    )]

    assert [event["type"] for event in generated] == ["reasoning", "text", "tool_calls", "done"]
    result = generated[-1]["result"]
    assert result.text == "answer"
    assert result.reasoning == "summary"
    assert result.stop_reason == stop_reason
    assert result.usage == {"input_tokens": 12, "output_tokens": 34}
    assert result.tool_calls == [{
        "id": "tool_1",
        "name": "Read",
        "arguments": '{"path": "a.py"}',
        "raw": {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"path": "a.py"}},
    }]
    assert result.assistant_message["message_metadata"] == {
        "source_format": "anthropic",
        "source_model": "claude-test",
        "native_blocks": native_blocks,
    }
    assert result.assistant_message["content_blocks"][-1] == {
        "type": "native",
        "native_type": "compaction",
        "block": {"type": "compaction", "content": "compact state"},
    }
    assert raw_client.messages.stream.call_args.kwargs == {
        "model": "claude-test",
        "max_tokens": 64_000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "xhigh"},
        "cache_control": {"type": "ephemeral"},
        "system": [{
            "type": "text",
            "text": "system",
            "cache_control": {"type": "ephemeral"},
        }],
        "tools": tools,
        "tool_choice": {"type": "tool", "name": "Read"},
    }
    assert stream.final_message_requested is True
    assert manager.exited is True


def test_anthropic_and_openai_tool_conversion_is_deterministic_by_name():
    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("Zulu", "Alpha", "Middle")
    ]

    openai_tools = _create_test_openai_client().format_tools(tools)
    anthropic_tools = format_anthropic_tools(tools)

    assert [tool["function"]["name"] for tool in openai_tools] == ["Alpha", "Middle", "Zulu"]
    assert [tool["name"] for tool in anthropic_tools] == ["Alpha", "Middle", "Zulu"]


def _create_test_openai_client():
    from utils.llm_client import AsyncChatAPIClient

    return AsyncChatAPIClient(None, "test")


@pytest.mark.anyio
async def test_summary_stream_resumes_pause_turn_and_emits_one_done_event():
    class FakeSummaryClient(AsyncBaseLLMClient):
        def __init__(self):
            super().__init__(None, "test")
            self.calls = 0
            self.requests = []

        async def generate_stream(self, messages, tools=None):
            self.requests.append(list(messages))
            self.calls += 1
            if self.calls == 1:
                result = SimpleNamespace(
                    text="partial ",
                    stop_reason="pause_turn",
                    assistant_message={
                        "role": "assistant",
                        "content": "partial ",
                        "stop_reason": "pause_turn",
                    },
                )
                yield {"type": "text", "content": "partial "}
            else:
                result = SimpleNamespace(
                    text="summary",
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "summary"},
                )
                yield {"type": "text", "content": "summary"}
            yield {"type": "done", "result": result}

    client = FakeSummaryClient()
    events = [
        event
        async for event in client.get_summary_stream_events("conversation", "compact")
    ]

    assert [event["type"] for event in events] == ["text", "text", "done"]
    assert events[-1]["result"].text == "partial summary"
    assert client.calls == 2
    assert client.requests[1][-1]["stop_reason"] == "pause_turn"


def test_cross_model_rebuild_uses_normalized_content_blocks_when_legacy_fields_are_missing():
    message = {
        "role": "assistant",
        "content_blocks": [
            {"type": "text", "text": "answer"},
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "Read",
                "arguments": {"path": "a.py"},
            },
        ],
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-source",
        },
        "reasoning_content": "reasoning summary",
    }

    _, anthropic_messages = build_anthropic_request_messages([message], "claude-other")
    from utils.llm_client import sanitize_openai_messages

    assert anthropic_messages == [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "answer"},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "Read",
                "input": {"path": "a.py"},
            },
        ],
    }]
    assert sanitize_openai_messages([message]) == [{
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "reasoning summary",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Read", "arguments": '{"path": "a.py"}'},
        }],
    }]


@pytest.mark.anyio
async def test_async_client_factory_selects_official_anthropic_sdk_from_message_format():
    model = ModelConfig(
        "https://api.anthropic.com",
        "key",
        "claude-test",
        reasoning_effort="max",
        message_format="anthropic",
    )

    client = _create_async_chat_client(model)
    try:
        assert isinstance(client, AnthropicMessagesClient)
        assert client.model == "claude-test"
        assert client.reasoning_effort == "max"
        assert client.client.__class__.__mro__[1].__name__ == "AsyncAnthropic"
        assert str(client.client.base_url) == "https://api.anthropic.com"
    finally:
        await client.client.close()


@pytest.mark.anyio
async def test_anthropic_client_factory_strips_v1_suffix_from_gateway_base_url():
    model = ModelConfig(
        "https://gateway.example/v1/",
        "key",
        "claude-test",
        message_format="anthropic",
    )

    client = _create_async_chat_client(model)
    try:
        assert str(client.client.base_url) == "https://gateway.example"
    finally:
        await client.client.close()


@pytest.mark.anyio
async def test_client_request_active_cleanup_survives_cross_task_stream_close():
    async def stream():
        with _client_request_active():
            yield "chunk"

    generator = stream()
    assert await asyncio.create_task(generator.__anext__()) == "chunk"
    await asyncio.create_task(generator.aclose())
