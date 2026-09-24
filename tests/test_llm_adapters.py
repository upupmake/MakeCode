import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import APIError, AsyncOpenAI
from openai.types.responses.response_input_param import ResponseInputItemParam
from pydantic import TypeAdapter

from system.models import ModelConfig
from utils.llm_client import (
    AnthropicMessagesClient,
    AsyncBaseLLMClient,
    AsyncChatAPIClient,
    OpenAIResponsesClient,
    _TrackedAsyncAnthropic,
    _TrackedAsyncOpenAI,
    _client_request_active,
    _create_async_chat_client,
    _LLM_TIMEOUT,
    build_anthropic_request_messages,
    build_llm_result,
    build_openai_prompt_cache_key,
    build_openai_responses_request,
    format_anthropic_tools,
    format_openai_responses_tools,
    sanitize_openai_messages,
    strip_native_message_payloads,
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


def _anthropic_sse_response(request, text="ok"):
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(
        f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
        for event_name, data in events
    )
    return httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/event-stream"},
        content=body.encode(),
    )


class _FailingAnthropicByteStream(httpx.AsyncByteStream):
    def __init__(self, packets):
        self.packets = packets

    async def __aiter__(self):
        for packet in self.packets:
            yield packet
        raise httpx.ReadError("stream disconnected")

    async def aclose(self):
        pass


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

    system, converted = build_anthropic_request_messages(messages)

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


def test_anthropic_message_conversion_replays_native_blocks_across_models():
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

    _, replayed = build_anthropic_request_messages([message])

    assert replayed[0]["content"] == native_blocks
    assert replayed[0]["content"] is not native_blocks


def test_anthropic_message_conversion_never_replays_openai_sourced_native_blocks():
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "chain of thought",
        "message_metadata": {
            "source_format": "openai_chat",
            "source_model": "gpt-test",
            "native_blocks": [
                {"type": "thinking", "thinking": "chain of thought", "signature": "forged"},
                {"type": "text", "text": "answer"},
            ],
        },
    }

    _, rebuilt = build_anthropic_request_messages([message])

    assert rebuilt == [{
        "role": "assistant",
        "content": [{"type": "text", "text": "answer"}],
    }]


def test_openai_responses_request_extracts_instructions_and_tool_outputs():
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

    instructions, converted = build_openai_responses_request(messages)

    assert instructions == "system one\n\nsystem two"
    assert converted == [
        {"role": "user", "content": "run both"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "Read",
            "arguments": '{"path":"a.py"}',
        },
        {
            "type": "function_call",
            "call_id": "call_2",
            "name": "Search",
            "arguments": '{"query": "needle"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "file",
        },
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": "failed",
        },
        {"role": "user", "content": "continue"},
    ]


def test_openai_responses_replays_native_blocks_in_original_order():
    native_blocks = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "enc",
            "summary": [{"type": "summary_text", "text": "summary"}],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "Read",
            "arguments": '{"path":"a.py"}',
        },
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "looking"}],
        },
    ]
    messages = [
        {"role": "system", "content": "stable system"},
        {
            "role": "assistant",
            "content": "looking",
            "reasoning_content": "summary",
            "tool_calls": [{
                "id": "call_1",
                "name": "Read",
                "arguments": '{"path":"a.py"}',
            }],
            "message_metadata": {
                "source_format": "openai_responses",
                "source_model": "gpt-source",
                "native_blocks": native_blocks,
            },
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "Read", "content": "file"},
        {"role": "user", "content": "continue"},
    ]

    instructions, replayed = build_openai_responses_request(messages)

    assert instructions == "stable system"
    assert replayed[:3] == native_blocks
    assert replayed[:3] is not native_blocks
    assert replayed[3:] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "file",
        },
        {"role": "user", "content": "continue"},
    ]


def test_openai_responses_never_replays_foreign_native_blocks():
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "chain of thought",
        "tool_calls": [{
            "id": "call_1",
            "name": "Read",
            "arguments": '{"path":"a.py"}',
        }],
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-test",
            "native_blocks": [
                {"type": "thinking", "thinking": "chain of thought", "signature": "forged"},
                {"type": "text", "text": "answer"},
                {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"path": "a.py"}},
            ],
        },
    }

    _, rebuilt = build_openai_responses_request([message])

    assert rebuilt == [
        {
            "type": "message",
            "role": "assistant",
            "content": "answer",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "Read",
            "arguments": '{"path":"a.py"}',
        },
    ]
    assert "encrypted_content" not in json.dumps(rebuilt)


def test_openai_responses_tool_conversion_flattens_namespaces_and_preserves_strict():
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
                    "additionalProperties": False,
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

    assert format_openai_responses_tools(tools) == [
        {
            "type": "function",
            "name": "Read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "Search",
            "description": "Search files",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        },
    ]


@pytest.mark.parametrize("strict", [None, False, True], ids=["unspecified", "false", "true"])
@pytest.mark.parametrize("shape", ["function", "mcp"])
@pytest.mark.parametrize("namespaced", [False, True], ids=["flat", "namespace"])
def test_responses_tool_strict_preserves_source_without_mutation(strict, shape, namespaced):
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    definition = {"name": "Search", "description": "Search files",
                  "parameters" if shape == "function" else "inputSchema": schema}
    if strict is not None:
        definition["strict"] = strict
    tool = {"type": "function", "function": definition} if shape == "function" else definition
    tools = [{"type": "namespace", "tools": [tool]}] if namespaced else [tool]
    original = copy.deepcopy(tools)

    formatted = format_openai_responses_tools(tools)

    assert formatted == [{"type": "function", "name": "Search", "description": "Search files",
                          "parameters": schema, "strict": strict if strict is not None else False}]
    assert tools == original
    formatted[0]["parameters"]["properties"]["query"]["type"] = "integer"
    assert tools == original


def test_responses_preserves_strict_for_all_builtin_tools():
    import main as main_module
    from tools.todo import TODO_TOOLS
    from utils import memory

    tools = (main_module.COMMON_TOOLS + main_module.MEMORY_RECALL_TOOLS
             + main_module.MEMORY_SELF_MANAGEMENT_TOOLS + main_module.SKILL_TOOLS
             + main_module.TASK_MANAGER_TOOLS + main_module.TEAM_TOOLS + main_module.ASK_USER_TOOLS
             + main_module.UNDERSTAND_IMAGE_TOOLS + main_module.TITLE_GENERATION_TOOLS
             + memory.LONG_TERM_MEMORY_TOOLS + memory.MEMORY_RECALL_SELECTION_TOOLS + TODO_TOOLS)

    definitions = [candidate for tool in tools
                   for candidate in (tool["tools"] if tool.get("type") == "namespace" else [tool])]
    assert all(tool["function"]["strict"] is True for tool in definitions)
    formatted = format_openai_responses_tools(tools)
    assert len(formatted) == len(definitions)
    assert all(tool["strict"] is True for tool in formatted)
    assert all(tool["parameters"]["additionalProperties"] is False for tool in formatted)


def test_responses_strict_setting_affects_cache_identity_but_not_tool_order():
    source = [
        {"name": "Zulu", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "Alpha", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
         "strict": True},
    ]
    before = format_openai_responses_tools(source)
    assert before == format_openai_responses_tools(list(reversed(source)))
    source[0]["strict"] = True
    after = format_openai_responses_tools(source)
    assert [tool["name"] for tool in before] == [tool["name"] for tool in after] == ["Alpha", "Zulu"]
    common = {"api_key": "key", "base_url": "https://gateway.example/v1", "model": "test",
              "reasoning_effort": "medium", "messages": [], "instructions": "stable system",
              "message_format": "openai_responses"}
    assert build_openai_prompt_cache_key(tools=before, **common) != build_openai_prompt_cache_key(tools=after, **common)


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
    assert [tool["name"] for tool in format_openai_responses_tools(tools)] == ["Alpha", "Middle", "Zulu"]
    assert all(tool["strict"] is False for tool in format_openai_responses_tools(tools))


def test_openai_prompt_cache_key_is_stable_for_growing_history_and_dict_order():
    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "first question"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path"}},
            },
        },
    }]
    common = {
        "api_key": "user-secret-key",
        "base_url": "https://gateway.example/v1",
        "model": "test-model",
        "reasoning_effort": "high",
    }

    first = build_openai_prompt_cache_key(messages=messages, tools=tools, **common)
    continued = build_openai_prompt_cache_key(
        messages=messages + [
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow up"},
        ],
        tools=[{
            "function": {
                "parameters": {
                    "properties": {"path": {"description": "Path", "type": "string"}},
                    "type": "object",
                },
                "description": "Read",
                "name": "Read",
            },
            "type": "function",
        }],
        **common,
    )

    assert first == continued
    assert first.startswith("mc-pc2-")
    assert "user-secret-key" not in first
    assert "gateway.example" not in first

    responses_first = build_openai_prompt_cache_key(
        messages=messages,
        tools=format_openai_responses_tools(tools),
        message_format="openai_responses",
        instructions="stable system",
        **common,
    )
    responses_continued = build_openai_prompt_cache_key(
        messages=messages + [
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow up"},
        ],
        tools=format_openai_responses_tools(tools),
        message_format="openai_responses",
        instructions="stable system",
        **common,
    )
    assert responses_first == responses_continued
    assert responses_first != first


def test_openai_prompt_cache_key_separates_request_identity():
    common = {
        "api_key": "key-a",
        "base_url": "https://gateway.example/v1",
        "model": "model-a",
        "reasoning_effort": "high",
        "messages": [{"role": "system", "content": "system-a"}],
        "tools": [{"type": "function", "function": {"name": "Read"}}],
    }
    variants = [
        common,
        {**common, "api_key": "key-b"},
        {**common, "base_url": "https://other.example/v1"},
        {**common, "model": "model-b"},
        {**common, "reasoning_effort": "max"},
        {**common, "messages": [{"role": "system", "content": "system-b"}]},
        {**common, "tools": [{"type": "function", "function": {"name": "Search"}}]},
    ]

    keys = {build_openai_prompt_cache_key(**variant) for variant in variants}

    assert len(keys) == len(variants)
    responses_key = build_openai_prompt_cache_key(
        **{**common, "message_format": "openai_responses", "instructions": "system-a"}
    )
    assert responses_key not in keys


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

    _, anthropic_messages = build_anthropic_request_messages([message])
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
    _, responses_items = build_openai_responses_request([message])
    assert responses_items == [
        {
            "type": "message",
            "role": "assistant",
            "content": "answer",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "Read",
            "arguments": '{"path": "a.py"}',
        },
    ]


@pytest.mark.anyio
async def test_openai_responses_stream_builds_lossless_unified_result():
    native_blocks = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "enc",
            "summary": [{"type": "summary_text", "text": "summary"}],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "Read",
            "arguments": '{"path":"a.py"}',
        },
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "answer"}],
        },
    ]
    events = [
        SimpleNamespace(type="response.reasoning_summary_text.delta", delta="summary"),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(type="function_call"),
        ),
        SimpleNamespace(type="response.output_text.delta", delta="answer"),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                output=native_blocks,
                status="completed",
                usage=SimpleNamespace(input_tokens=12, output_tokens=34),
            ),
        ),
    ]

    class FakeResponsesStream:
        def __init__(self):
            self._events = iter(events)
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration

        def close(self):
            self.closed = True

    stream = FakeResponsesStream()
    create = AsyncMock(return_value=stream)
    raw_client = SimpleNamespace(responses=SimpleNamespace(create=create))
    client = OpenAIResponsesClient(
        raw_client,
        "gpt-test",
        "high",
        base_url="https://gateway.example/v1",
        api_key="key",
    )
    tools = format_openai_responses_tools([{
        "type": "function",
        "function": {"name": "Read", "description": "Read", "parameters": {"type": "object"}},
    }])

    generated = [event async for event in client.generate_stream(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}],
        tools,
    )]

    assert [event["type"] for event in generated] == ["reasoning", "tool_calls", "text", "done"]
    result = generated[-1]["result"]
    assert result.text == "answer"
    assert result.reasoning == "summary"
    assert result.stop_reason == "completed"
    assert result.usage == {"input_tokens": 12, "output_tokens": 34}
    assert result.tool_calls == [{
        "id": "call_1",
        "name": "Read",
        "arguments": '{"path":"a.py"}',
        "raw": native_blocks[1],
    }]
    assert result.assistant_message["message_metadata"] == {
        "source_format": "openai_responses",
        "source_model": "gpt-test",
        "native_blocks": native_blocks,
    }
    kwargs = create.call_args.kwargs
    assert kwargs["store"] is False
    assert kwargs["instructions"] == "system"
    assert kwargs["input"] == [{"role": "user", "content": "hello"}]
    assert kwargs["reasoning"] == {"effort": "high", "summary": "auto"}
    assert kwargs["tools"] == tools
    assert kwargs["prompt_cache_key"].startswith("mc-pc2-")
    assert stream.closed is True


def test_llm_timeout_is_provider_neutral():
    assert isinstance(_LLM_TIMEOUT, tuple)
    assert _LLM_TIMEOUT == (10.0, 120.0, 120.0, 120.0)


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
        request = client.client._client.build_request(
            "GET",
            "https://api.anthropic.com/test",
            timeout=client.client.timeout,
        )
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 120.0,
            "write": 120.0,
            "pool": 120.0,
        }
    finally:
        await client.client.close()


@pytest.mark.anyio
async def test_async_client_factory_passes_openai_cache_identity_to_adapter():
    model = ModelConfig(
        "https://gateway.example/",
        "user-api-key",
        "openai-test",
        reasoning_effort="high",
        message_format="openai_chat",
    )

    client = _create_async_chat_client(model)
    try:
        assert isinstance(client, AsyncChatAPIClient)
        assert client.base_url == "https://gateway.example/v1"
        assert client.api_key == "user-api-key"
        assert str(client.client.base_url) == "https://gateway.example/v1/"
    finally:
        await client.client.close()


@pytest.mark.anyio
async def test_async_client_factory_selects_openai_responses_adapter():
    model = ModelConfig(
        "https://gateway.example/",
        "user-api-key",
        "openai-test",
        reasoning_effort="high",
        message_format="openai_responses",
    )

    client = _create_async_chat_client(model)
    try:
        assert isinstance(client, OpenAIResponsesClient)
        assert client.base_url == "https://gateway.example/v1"
        assert client.api_key == "user-api-key"
        assert str(client.client.base_url) == "https://gateway.example/v1/"
        assert client.format_tools([{
            "type": "function",
            "function": {"name": "Read", "description": "Read", "parameters": {"type": "object"}},
        }]) == [{
            "type": "function",
            "name": "Read",
            "description": "Read",
            "parameters": {"type": "object"},
            "strict": False,
        }]
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
async def test_anthropic_stream_retries_connection_failure_before_response_starts():
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary connect failure", request=request)
        return _anthropic_sse_response(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raw_client = _TrackedAsyncAnthropic(
        base_url="https://gateway.example",
        api_key="key",
        http_client=http_client,
        max_retries=1,
    )
    client = AnthropicMessagesClient(raw_client, "claude-test")
    try:
        with patch.object(AsyncAnthropic, "_sleep_for_retry", new=AsyncMock()):
            events = [
                event
                async for event in client.generate_stream([{"role": "user", "content": "hello"}])
            ]
    finally:
        await raw_client.close()

    assert attempts == 2
    assert [event["type"] for event in events] == ["text", "done"]
    assert events[-1]["result"].text == "ok"


@pytest.mark.anyio
async def test_anthropic_stream_does_not_replay_after_partial_output():
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        message_start = (
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"id":"msg_1","type":"message","role":"assistant","model":"claude-test",'
            '"content":[],"stop_reason":null,"stop_sequence":null,'
            '"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        ).encode()
        content_start = (
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n\n'
        ).encode()
        text_delta = (
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"partial"}}\n\n'
        ).encode()
        if attempts == 1:
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                stream=_FailingAnthropicByteStream([message_start, content_start, text_delta]),
            )
        return _anthropic_sse_response(request, text="answer")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raw_client = _TrackedAsyncAnthropic(
        base_url="https://gateway.example",
        api_key="key",
        http_client=http_client,
        max_retries=2,
    )
    client = AnthropicMessagesClient(raw_client, "claude-test")
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            async for event in client.generate_stream([{"role": "user", "content": "hello"}]):
                emitted.append(event)
        assert sleep.await_count == 1
    finally:
        await raw_client.close()

    assert attempts == 2
    assert [event["type"] for event in emitted] == ["text", "text", "done"]
    assert emitted[0] == {"type": "text", "content": "partial"}
    assert emitted[1] == {"type": "text", "content": "answer"}
    assert emitted[-1]["result"].text == "answer"


@pytest.mark.anyio
async def test_client_request_active_cleanup_survives_cross_task_stream_close():
    async def stream():
        with _client_request_active():
            yield "chunk"

    generator = stream()
    assert await asyncio.create_task(generator.__anext__()) == "chunk"
    await asyncio.create_task(generator.aclose())


@pytest.fixture
def responses_native_items():
    return [
        {
            "id": "rs_1", "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "summary"}],
            "encrypted_content": "encrypted-test-state",
        },
        {
            "id": "msg_1", "type": "message", "role": "assistant",
            "status": "completed", "phase": "commentary",
            "content": [{"type": "output_text", "text": "reading", "annotations": []}],
        },
        {
            "id": "fc_1", "type": "function_call", "call_id": "call_1",
            "name": "Read", "arguments": '{"path":"a.py"}', "status": "completed",
        },
        {
            "id": "msg_2", "type": "message", "role": "assistant",
            "status": "completed", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "answer", "annotations": []}],
        },
    ]


class ResponsesByteStream(httpx.AsyncByteStream):
    def __init__(self, events, fail=False, error=None):
        self.events = events
        self.fail = fail
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for sequence_number, event in enumerate(self.events):
            data = {"sequence_number": sequence_number, **event}
            yield f"event: {event['type']}\ndata: {json.dumps(data)}\n\n".encode()
        if self.fail:
            raise httpx.ReadError("stream disconnected")
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def responses_terminal_event(items, status="completed", **extra):
    return {
        "type": f"response.{status}",
        "response": {
            "id": "resp_1", "object": "response", "created_at": 1,
            "model": "test", "status": status, "output": items,
            "error": None, "incomplete_details": None,
            "usage": {"input_tokens": 1200, "output_tokens": 20, "total_tokens": 1220,
                      "input_tokens_details": {"cached_tokens": 1024},
                      "output_tokens_details": {"reasoning_tokens": 10}},
            **extra,
        },
    }


def responses_sdk_client(events, *, fail=False, requests=None):
    byte_stream = ResponsesByteStream(events, fail=fail)

    async def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        TypeAdapter(list[ResponseInputItemParam]).validate_python(body["input"])
        if requests is not None:
            requests.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=byte_stream)

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1", api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0,
    )
    return OpenAIResponsesClient(raw, "test", base_url="https://gateway.example/v1", api_key="test-key"), byte_stream


@pytest.fixture
def isolated_responses_runtime(monkeypatch):
    import system.tui_app as tui
    monkeypatch.setattr(tui, "set_client_request_active", Mock())
    monkeypatch.setattr(tui, "set_client_request_retry", Mock())
    monkeypatch.setattr(tui, "post_tui", Mock())
    monkeypatch.setattr("utils.llm_client._is_response_cancelled", lambda: False)


@pytest.mark.parametrize("source", ["openai_chat", "anthropic"])
def test_responses_foreign_assistant_matches_sdk_input_schema(source):
    message = {
        "role": "assistant", "content": "answer",
        "message_metadata": {"source_format": source},
        "tool_calls": [{"id": "call_1", "name": "Read", "arguments": "{}"}],
    }
    _, items = build_openai_responses_request([message])
    TypeAdapter(list[ResponseInputItemParam]).validate_python(items)
    assert items[0]["content"] == "answer"
    assert items[1]["call_id"] == "call_1"


def test_responses_inline_image_matches_sdk_input_schema():
    _, items = build_openai_responses_request([{
        "role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "data": b"png", "media_type": "image/png"},
        ],
    }])
    TypeAdapter(list[ResponseInputItemParam]).validate_python(items)


@pytest.mark.anyio
@pytest.mark.parametrize("failure, message, expected_retries", [
    ("failed", "server_error", 2), ("incomplete", "max_output_tokens", 0),
    ("error", "bad request", 0),
])
async def test_responses_terminal_failures_are_not_silent(failure, message, expected_retries, isolated_responses_runtime):
    if failure == "error":
        events = [{"type": "error", "code": "invalid_request", "message": "bad request", "param": None}]
    else:
        events = [responses_terminal_event([], failure,
            error={"code": "server_error", "message": "upstream failed"} if failure == "failed" else None,
            incomplete_details={"reason": "max_output_tokens"} if failure == "incomplete" else None)]
    client, byte_stream = responses_sdk_client(events)
    client.client.max_retries = 2
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(Exception, match=message):
            async for event in client.generate_stream([{"role": "user", "content": "hi"}]):
                emitted.append(event)
        assert sleep.await_count == expected_retries
        assert not any(event["type"] == "done" for event in emitted)
        assert byte_stream.closed
    finally:
        await client.client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_responses_early_exit_closes_sdk_stream_immediately(stop, monkeypatch, isolated_responses_runtime):
    client, byte_stream = responses_sdk_client([
        {"type": "response.output_text.delta", "delta": "partial", "output_index": 0,
         "content_index": 0, "item_id": "msg_1", "logprobs": []},
        responses_terminal_event([]),
    ])
    stream = client.generate_stream([{"role": "user", "content": "hi"}])
    try:
        assert await anext(stream) == {"type": "text", "content": "partial"}
        if stop == "cancel":
            monkeypatch.setattr("utils.llm_client._is_response_cancelled", lambda: True)
            assert [event async for event in stream] == []
        else:
            await stream.aclose()
        assert byte_stream.closed
    finally:
        await stream.aclose()
        await client.client.close()


@pytest.mark.anyio
async def test_responses_refusal_is_visible_and_replayable(isolated_responses_runtime):
    items = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "refusal", "refusal": "Cannot help."}]}]
    client, _ = responses_sdk_client([
        {"type": "response.refusal.delta", "delta": "Cannot help.", "item_id": "msg_1",
         "content_index": 0, "output_index": 0},
        responses_terminal_event(items),
    ])
    try:
        events = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await client.client.close()
    assert events[0] == {"type": "text", "content": "Cannot help."}
    result = events[-1]["result"]
    assert result.text == "Cannot help."
    assert result.assistant_message["content_blocks"][0]["text"] == "Cannot help."
    assert build_openai_responses_request([result.assistant_message])[1] == items
    assert sanitize_openai_messages([result.assistant_message])[0]["content"] == "Cannot help."


@pytest.mark.anyio
async def test_responses_sdk_multi_turn_keeps_exact_request_prefix(responses_native_items, isolated_responses_runtime):
    requests = []
    client, _ = responses_sdk_client([responses_terminal_event(responses_native_items)], requests=requests)
    tools = client.format_tools([{"name": "Read", "inputSchema": {"type": "object"}}])
    history = [{"role": "system", "content": "stable system"}, {"role": "user", "content": "read file"},
               {"role": "assistant", "content": "historical reply", "message_metadata": {"source_format": "anthropic"}}]
    try:
        first = [event async for event in client.generate_stream(history, tools)]
        result = first[-1]["result"]
        history += [result.assistant_message, client.format_tool_result("call_1", "Read", "file contents")]
        awaitable = client.generate_stream(history, tools)
        _ = [event async for event in awaitable]
    finally:
        await client.client.close()
    assert requests[1]["input"] == requests[0]["input"] + responses_native_items + [
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"}]
    for field in ("tools", "instructions", "reasoning", "prompt_cache_key"):
        assert requests[0][field] == requests[1][field]
    assert requests[0]["store"] is False
    assert "previous_response_id" not in requests[0]
    assert "reasoning.encrypted_content" in requests[0]["include"]
    assert result.usage["input_tokens_details"]["cached_tokens"] == 1024


@pytest.mark.anyio
async def test_responses_missing_completed_retries_before_output(isolated_responses_runtime):
    import system.tui_app as tui

    items = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "answer", "annotations": []}]}]
    byte_streams = [
        ResponsesByteStream([]),
        ResponsesByteStream([]),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event(items),
        ]),
    ]
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=byte_streams[len(requests) - 1])

    async def backoff(delay):
        assert byte_streams[len(requests) - 1].closed

    raw = _TrackedAsyncOpenAI(base_url="https://gateway.example/v1", api_key="test-key",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=2)
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock(side_effect=backoff)) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    finally:
        await raw.close()

    assert len(requests) == 3
    assert requests[0] == requests[1] == requests[2]
    assert all(stream.closed for stream in byte_streams)
    assert [event["type"] for event in emitted] == ["text", "done"]
    assert emitted[-1]["result"].text == "answer"
    assert [call.args[1:] for call in tui.set_client_request_retry.call_args_list] == [(1, 2), (2, 2)]
    assert [call.args[0] for call in tui.set_client_request_active.call_args_list] == [True, False]


@pytest.mark.parametrize("target", ["openai_chat", "openai_responses", "anthropic"])
def test_mixed_protocol_history_projection_is_prefix_stable(target, responses_native_items):
    calls = [{"id": "call_1", "name": "Read", "arguments": '{"path":"a.py"}'}]
    responses = build_llm_result(text="readinganswer", reasoning="summary", tool_calls=calls,
        source_format="openai_responses", source_model="gpt-test", native_blocks=responses_native_items).assistant_message
    anthropic_blocks = [
        {"type": "thinking", "thinking": "thinking", "signature": "test-signature"},
        {"type": "text", "text": "claude answer"},
    ]
    anthropic = build_llm_result(text="claude answer", reasoning="thinking", source_format="anthropic",
        source_model="claude-test", native_blocks=anthropic_blocks).assistant_message
    history = [{"role": "system", "content": "stable"}, {"role": "user", "content": "read"}, responses,
               {"role": "tool", "tool_call_id": "call_1", "name": "Read", "content": "file"},
               {"role": "user", "content": "continue"}, anthropic]
    original = copy.deepcopy(history)

    def project(messages):
        if target == "openai_chat":
            return "", sanitize_openai_messages(messages)
        if target == "anthropic":
            return build_anthropic_request_messages(messages)
        return build_openai_responses_request(messages)

    system, first = project(history)
    next_system, continued = project(history + [{"role": "user", "content": "next"}])
    assert system == next_system
    assert continued[:len(first)] == first
    wire = json.dumps(first)
    assert "readinganswer" in wire or ("reading" in wire and "answer" in wire)
    assert "claude answer" in wire and "call_1" in wire and "file" in wire
    assert ("encrypted-test-state" in wire) == (target == "openai_responses")
    assert ("test-signature" in wire) == (target == "anthropic")
    assert history == original
    assert build_openai_responses_request(history)[1][1:5] == responses_native_items
    assert "encrypted-test-state" not in json.dumps(strip_native_message_payloads(history))


@pytest.mark.anyio
async def test_responses_upstream_break_retries_before_output(isolated_responses_runtime):
    import system.tui_app as tui

    failure = responses_terminal_event([], "failed", error={
        "code": "upstream_stream_break",
        "message": "Upstream stream ended prematurely; safe to retry",
    })
    items = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "answer", "annotations": []}]}]
    byte_streams = [
        ResponsesByteStream([failure]),
        ResponsesByteStream([failure]),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event(items),
        ]),
    ]
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=byte_streams[len(requests) - 1])

    async def backoff(delay):
        assert byte_streams[len(requests) - 1].closed

    raw = _TrackedAsyncOpenAI(base_url="https://gateway.example/v1", api_key="test-key",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=2)
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock(side_effect=backoff)) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    finally:
        await raw.close()

    assert len(requests) == 3
    assert requests[0] == requests[1] == requests[2]
    assert all(stream.closed for stream in byte_streams)
    assert [event["type"] for event in emitted] == ["text", "done"]
    assert emitted[-1]["result"].text == "answer"
    assert [call.args[1:] for call in tui.set_client_request_retry.call_args_list] == [(1, 2), (2, 2)]
    assert [call.args[0] for call in tui.set_client_request_active.call_args_list] == [True, False]


@pytest.mark.anyio
async def test_responses_api_overload_retries_before_output(isolated_responses_runtime):
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    overload = APIError(
        "Our servers are currently overloaded. Please try again later.",
        request,
        body={"code": "server_error", "message": "Our servers are currently overloaded. Please try again later."},
    )
    streams = [
        ResponsesByteStream([], error=overload),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]
    requests = []

    async def handler(http_request):
        body = json.loads(http_request.content)
        TypeAdapter(list[ResponseInputItemParam]).validate_python(body["input"])
        requests.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert all(stream.closed for stream in streams)
    assert sleep.await_args_list[0].args == (0.5,)
    assert [event["type"] for event in emitted] == ["text", "done"]


@pytest.mark.anyio
async def test_responses_api_token_generation_error_retries_before_output(isolated_responses_runtime):
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    generation_error = APIError(
        "Internal error during token generation",
        request,
        body={"code": "server_error", "message": "Internal error during token generation"},
    )
    streams = [
        ResponsesByteStream([], error=generation_error),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]
    requests = []

    async def handler(http_request):
        body = json.loads(http_request.content)
        TypeAdapter(list[ResponseInputItemParam]).validate_python(body["input"])
        requests.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert all(stream.closed for stream in streams)
    assert sleep.await_args_list[0].args == (0.5,)
    assert [event["type"] for event in emitted] == ["text", "done"]


@pytest.mark.anyio
async def test_responses_error_event_token_generation_error_retries_before_output(isolated_responses_runtime):
    streams = [
        ResponsesByteStream([{
            "type": "error",
            "code": "server_error",
            "message": "Internal error during token generation",
            "param": None,
        }]),
        ResponsesByteStream([responses_terminal_event([])]),
    ]
    requests = []

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert sleep.await_args_list[0].args == (0.5,)
    assert emitted[-1]["type"] == "done"


@pytest.mark.anyio
async def test_responses_error_event_overload_retries_before_output(isolated_responses_runtime):
    streams = [
        ResponsesByteStream([{
            "type": "error",
            "code": "server_error",
            "message": "Our servers are currently overloaded. Please try again later.",
            "param": None,
        }]),
        ResponsesByteStream([responses_terminal_event([])]),
    ]
    requests = []

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert sleep.await_args_list[0].args == (0.5,)
    assert emitted[-1]["type"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [None, "text", "tool_calls"])
@pytest.mark.parametrize("error_style", ["event", "sdk", "sdk_no_code"])
async def test_responses_sdk_processing_error_retries_incomplete_stream(partial, error_style, isolated_responses_runtime):
    import system.tui_app as tui

    message = (
        "An error occurred while processing your request. You can retry your request, "
        "or contact us through our help center at help.openai.com if the error persists. "
        "Please include the request ID 062df7a9-fe1a-4b21-97c1-5ec0d1bc4357 in your message."
    )
    partial_events = {
        None: [],
        "text": [{"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
                  "output_index": 0, "content_index": 0, "logprobs": []}],
        "tool_calls": [{"type": "response.output_item.added", "output_index": 0,
                        "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1",
                                 "name": "Read", "arguments": "", "status": "in_progress"}}],
    }
    items = [{"id": "msg_2", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "answer", "annotations": []}]}]
    error_event = (
        {"type": "error", "code": "server_error", "message": message, "param": None}
        if error_style == "event" else {
            "type": "error", "error": {
                **({"code": "server_error"} if error_style == "sdk" else {}), "message": message,
            },
        }
    )
    streams = [
        ResponsesByteStream([*partial_events[partial], error_event]),
        ResponsesByteStream([{"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_2",
                              "output_index": 0, "content_index": 0, "logprobs": []},
                             responses_terminal_event(items)]),
    ]
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        TypeAdapter(list[ResponseInputItemParam]).validate_python(body["input"])
        requests.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1", api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert all(stream.closed for stream in streams)
    assert sleep.await_args_list[0].args == (0.5,)
    assert tui.set_client_request_retry.call_args_list[0].args[1:] == (1, 1)
    assert [event["type"] for event in emitted] == ([partial] if partial else []) + ["text", "done"]
    assert emitted[-1]["result"].text == "answer"
    assert emitted[-1]["result"].tool_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("max_retries", [0, 2])
async def test_responses_sdk_processing_error_respects_retry_limit(max_retries, isolated_responses_runtime):
    message = "An error occurred while processing your request. You can retry your request."
    requests = []
    client, byte_stream = responses_sdk_client([
        {"type": "error", "error": {"code": "server_error", "message": message}}
    ], requests=requests)
    client.client.max_retries = max_retries
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(APIError, match="An error occurred while processing your request"):
            async for event in client.generate_stream([{"role": "user", "content": "hi"}]):
                emitted.append(event)
        assert sleep.await_count == max_retries
    finally:
        await client.client.close()

    assert len(requests) == max_retries + 1
    assert all(body == requests[0] for body in requests)
    assert emitted == []
    assert byte_stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("code, message", [
    ("invalid_request", "Invalid input. You can retry your request."),
    ("bad_request", "An error occurred while processing your request. You can retry your request."),
    ("authentication_error", "An error occurred while processing your request. You can retry your request."),
    ("insufficient_quota", "An error occurred while processing your request. You can retry your request."),
])
async def test_responses_sdk_other_retry_suggestion_is_not_retryable(code, message, isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([
        {"type": "error", "error": {"code": code, "message": message}}
    ], requests=requests)
    client.client.max_retries = 2
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(APIError, match="You can retry your request"):
            async for _ in client.generate_stream([{"role": "user", "content": "hi"}]):
                pass
        sleep.assert_not_awaited()
    finally:
        await client.client.close()

    assert len(requests) == 1
    assert byte_stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [400, 401, 403])
async def test_responses_http_client_errors_do_not_retry_even_with_server_message(
    status_code, isolated_responses_runtime,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status_code, json={"error": {
            "code": "server_error",
            "message": "An error occurred while processing your request. You can retry your request.",
        }})

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1", api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(APIError, match="An error occurred while processing your request"):
            async for _ in client.generate_stream([{"role": "user", "content": "hi"}]):
                pass
        sleep.assert_not_awaited()
    finally:
        await raw.close()

    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [429, 500])
async def test_responses_http_transient_errors_use_sdk_retry_only(status_code, isolated_responses_runtime):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status_code, json={"error": {
            "code": "server_error",
            "message": "An error occurred while processing your request. You can retry your request.",
        }})

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1", api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch.object(AsyncOpenAI, "_sleep_for_retry", new=AsyncMock()) as sdk_sleep, \
                patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as app_sleep, \
                pytest.raises(APIError, match="An error occurred while processing your request"):
            async for _ in client.generate_stream([{"role": "user", "content": "hi"}]):
                pass
        assert sdk_sleep.await_count == 1
        app_sleep.assert_not_awaited()
    finally:
        await raw.close()

    assert len(requests) == 2


@pytest.mark.anyio
async def test_responses_api_overload_after_output_does_not_retry(isolated_responses_runtime):
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    overload = APIError(
        "Our servers are currently overloaded. Please try again later.",
        request,
        body={"code": "server_error", "message": "Our servers are currently overloaded. Please try again later."},
    )
    requests = []
    streams = [
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
        ], error=overload),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert sleep.await_count == 1
    assert [event["type"] for event in emitted] == ["text", "text", "done"]
    assert emitted[0] == {"type": "text", "content": "partial"}
    assert emitted[1] == {"type": "text", "content": "answer"}
    assert all(stream.closed for stream in streams)


@pytest.mark.anyio
async def test_responses_api_token_generation_error_after_output_does_not_retry(isolated_responses_runtime):
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    generation_error = APIError(
        "Internal error during token generation",
        request,
        body={"code": "server_error", "message": "Internal error during token generation"},
    )
    requests = []
    streams = [
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
        ], error=generation_error),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()

    assert len(requests) == 2
    assert sleep.await_count == 1
    assert [event["type"] for event in emitted] == ["text", "text", "done"]
    assert emitted[-1]["type"] == "done"
    assert all(stream.closed for stream in streams)


@pytest.mark.anyio
@pytest.mark.parametrize("max_retries", [0, 2])
async def test_responses_upstream_break_respects_retry_limit(max_retries, isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([
        responses_terminal_event([], "failed", error={"code": "upstream_stream_break", "message": "safe to retry"}),
    ], requests=requests)
    client.client.max_retries = max_retries
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(RuntimeError, match="response.failed.*upstream_stream_break"):
            async for event in client.generate_stream([{"role": "user", "content": "hi"}]):
                emitted.append(event)
        assert sleep.await_count == max_retries
    finally:
        await client.client.close()

    assert len(requests) == max_retries + 1
    assert emitted == []
    assert byte_stream.closed


@pytest.mark.anyio
async def test_responses_upstream_break_cancellation_stops_retry(monkeypatch, isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([
        responses_terminal_event([], "failed", error={"code": "upstream_stream_break", "message": "safe to retry"}),
    ], requests=requests)
    client.client.max_retries = 2

    async def cancel_during_backoff(delay):
        assert byte_stream.closed
        monkeypatch.setattr("utils.llm_client._is_response_cancelled", lambda: True)

    try:
        with patch("utils.llm_client.asyncio.sleep", new=cancel_during_backoff):
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await client.client.close()
    assert len(requests) == 1
    assert emitted == []


@pytest.mark.anyio
@pytest.mark.parametrize("output", ["text", "reasoning", "refusal", "tool_calls"])
async def test_responses_upstream_break_after_output_never_retries(output, isolated_responses_runtime):
    events = {
        "text": {"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
                 "output_index": 0, "content_index": 0, "logprobs": []},
        "reasoning": {"type": "response.reasoning_summary_text.delta", "delta": "partial",
                      "item_id": "rs_1", "output_index": 0, "summary_index": 0},
        "refusal": {"type": "response.refusal.delta", "delta": "partial", "item_id": "msg_1",
                    "output_index": 0, "content_index": 0},
        "tool_calls": {"type": "response.output_item.added", "output_index": 0,
                       "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1",
                                "name": "Read", "arguments": "", "status": "in_progress"}},
    }
    requests = []
    failure = responses_terminal_event([], "failed", error={"code": "upstream_stream_break", "message": "safe to retry"})
    streams = [
        ResponsesByteStream([events[output], failure]),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
        assert sleep.await_count == 1
    finally:
        await raw.close()

    assert len(requests) == 2
    assert emitted[0]["type"] == ("text" if output == "refusal" else output)
    assert [event["type"] for event in emitted[-2:]] == ["text", "done"]
    assert all(stream.closed for stream in streams)


@pytest.mark.anyio
async def test_responses_disconnect_after_output_retries_incomplete_stream(isolated_responses_runtime):
    requests = []
    items = [{"id": "msg_2", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "answer", "annotations": []}]}]
    streams = [
        ResponsesByteStream([{"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
                              "content_index": 0, "output_index": 0, "logprobs": []}], fail=True),
        ResponsesByteStream([{"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_2",
                              "content_index": 0, "output_index": 0, "logprobs": []},
                             responses_terminal_event(items)]),
    ]

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1", api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert sleep.await_args_list[0].args == (0.5,)
    assert [event["type"] for event in emitted] == ["text", "text", "done"]
    assert emitted[-1]["result"].text == "answer"
    assert all(stream.closed for stream in streams)


@pytest.mark.anyio
async def test_responses_disconnect_respects_retry_limit(isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([
        {"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
         "content_index": 0, "output_index": 0, "logprobs": []},
    ], fail=True, requests=requests)
    client.client.max_retries = 2
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(httpx.ReadError, match="disconnected"):
            async for event in client.generate_stream([{"role": "user", "content": "hi"}]):
                emitted.append(event)
        assert sleep.await_count == 2
    finally:
        await client.client.close()
    assert len(requests) == 3
    assert emitted == [{"type": "text", "content": "partial"}] * 3
    assert byte_stream.closed


@pytest.mark.anyio
async def test_responses_missing_completed_retries_after_output(isolated_responses_runtime):
    requests = []
    streams = [
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "partial", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
        ]),
        ResponsesByteStream([
            {"type": "response.output_text.delta", "delta": "answer", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "logprobs": []},
            responses_terminal_event([]),
        ]),
    ]

    async def handler(http_request):
        requests.append(json.loads(http_request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    raw = _TrackedAsyncOpenAI(
        base_url="https://gateway.example/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
        assert sleep.await_count == 1
    finally:
        await raw.close()

    assert len(requests) == 2
    assert [event["type"] for event in emitted] == ["text", "text", "done"]
    assert emitted[0] == {"type": "text", "content": "partial"}
    assert emitted[1] == {"type": "text", "content": "answer"}
    assert all(stream.closed for stream in streams)


@pytest.mark.anyio
@pytest.mark.parametrize("max_retries", [0, 2])
async def test_responses_missing_completed_respects_retry_limit(max_retries, isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([], requests=requests)
    client.client.max_retries = max_retries
    emitted = []
    try:
        with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep, \
                pytest.raises(RuntimeError, match="without a terminal response.completed"):
            async for event in client.generate_stream([{"role": "user", "content": "hi"}]):
                emitted.append(event)
        assert sleep.await_count == max_retries
    finally:
        await client.client.close()

    assert len(requests) == max_retries + 1
    assert emitted == []
    assert byte_stream.closed


@pytest.mark.anyio
async def test_responses_missing_completed_cancellation_stops_retry(monkeypatch, isolated_responses_runtime):
    requests = []
    client, byte_stream = responses_sdk_client([], requests=requests)
    client.client.max_retries = 2

    async def cancel_during_backoff(delay):
        assert byte_stream.closed
        monkeypatch.setattr("utils.llm_client._is_response_cancelled", lambda: True)

    try:
        with patch("utils.llm_client.asyncio.sleep", new=cancel_during_backoff):
            emitted = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await client.client.close()
    assert len(requests) == 1
    assert emitted == []


@pytest.mark.anyio
async def test_responses_sdk_retries_connection_before_output(isolated_responses_runtime):
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary failure", request=request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=ResponsesByteStream([responses_terminal_event([])]))

    raw = _TrackedAsyncOpenAI(base_url="https://gateway.example/v1", api_key="test-key",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=1)
    client = OpenAIResponsesClient(raw, "test")
    try:
        with patch.object(AsyncOpenAI, "_sleep_for_retry", new=AsyncMock()):
            events = [event async for event in client.generate_stream([{"role": "user", "content": "hi"}])]
    finally:
        await raw.close()
    assert attempts == 2
    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_responses_persistence_and_compaction_preserve_phase(
    responses_native_items, isolated_responses_runtime, tmp_path,
):
    from utils.conversations import ConversationStore
    from utils import memory

    client, _ = responses_sdk_client([responses_terminal_event(responses_native_items)])
    history = [{"role": "system", "content": "stable"}, {"role": "user", "content": "old"}]
    try:
        events = [event async for event in client.generate_stream(history)]
        history += [events[-1]["result"].assistant_message,
                    client.format_tool_result("call_1", "Read", "甲" * 3000),
                    {"role": "user", "content": "latest"},
                    {"role": "assistant", "content": "latest answer"}]
    finally:
        await client.client.close()

    store = ConversationStore(tmp_path / "conversations")
    loaded = store.load(store.save_messages(history)).messages
    assert build_openai_responses_request(loaded) == build_openai_responses_request(history)
    assert memory.compact_tool_outputs(loaded)
    assert "native_blocks" not in loaded[2]["message_metadata"]
    _, items = build_openai_responses_request(loaded)
    TypeAdapter(list[ResponseInputItemParam]).validate_python(items)
    assert [(item["content"], item.get("phase")) for item in items if item.get("role") == "assistant"] == [
        ("reading", "commentary"), ("answer", "final_answer"), ("latest answer", None),
    ]
    assert items[2]["type"] == "function_call"
    assert items[4]["type"] == "function_call_output"
    assert items[4]["call_id"] == items[2]["call_id"]
    assert "encrypted-test-state" not in json.dumps(items)
    assert not memory.compact_tool_outputs(loaded)


def auxiliary_sse_response(request, message_format, reply):
    if reply.get("error"):
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "auxiliary failure"}})
    text = reply.get("text", "")
    calls = reply.get("calls", [])
    model = json.loads(request.content)["model"]
    if message_format == "openai_responses":
        items = [{"type": "reasoning", "id": "rs_aux", "summary": [], "encrypted_content": "aux-encrypted"}]
        if text:
            items.append({"type": "message", "id": "msg_aux", "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": text, "annotations": []}]})
        items.extend({"type": "function_call", "id": f"fc_{index}", "call_id": call["id"],
                      "name": call["name"], "arguments": json.dumps(call["arguments"])}
                     for index, call in enumerate(calls))
        events = []
        if text:
            events.append({"type": "response.output_text.delta", "delta": text, "item_id": "msg_aux",
                           "output_index": 1, "content_index": 0, "logprobs": []})
        events.append(responses_terminal_event(items, model=model))
    elif message_format == "anthropic":
        events = [{"type": "message_start", "message": {
            "id": "msg_aux", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }}]
        blocks = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]}
            for call in calls
        ]
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                start = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            events.extend([
                {"type": "content_block_start", "index": index, "content_block": start},
                {"type": "content_block_delta", "index": index, "delta": delta},
                {"type": "content_block_stop", "index": index},
            ])
        events.extend([
            {"type": "message_delta", "delta": {"stop_reason": "tool_use" if calls else "end_turn",
             "stop_sequence": None}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ])
    else:
        delta = {"role": "assistant", "content": text}
        if calls:
            delta["tool_calls"] = [
                {"index": index, "id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                for index, call in enumerate(calls)
            ]
        chunk = {"id": "chat_aux", "object": "chat.completion.chunk", "created": 1, "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ResponsesByteStream(events))


@pytest.fixture(params=["openai_chat", "openai_responses", "anthropic"])
async def auxiliary_gateway(request, monkeypatch, isolated_responses_runtime, tmp_path):
    import main as main_module
    import system.stream_render as stream_render
    from utils import llm_client, memory, teams
    from tools import understand_image

    formats = ["openai_chat", "openai_responses", "anthropic"]
    offset = formats.index(request.param)
    models = {name: ModelConfig("https://gateway.example/v1", "test-key", name,
                               message_format=formats[(offset + index) % 3])
              for index, name in enumerate(("main", "recall", "vision"))}
    manager = Mock()
    manager.get_current_model.return_value = models["main"]
    manager.get_memory_recall_model.return_value = models["recall"]
    monkeypatch.setattr(llm_client, "get_current_model_config", lambda: models["main"])
    monkeypatch.setattr(llm_client, "get_model_manager", lambda: manager)
    monkeypatch.setattr(llm_client, "get_understand_image_model", lambda: models["vision"])
    replies, requests, clients = [], [], []
    pending = {}

    async def handler(http_request):
        body = json.loads(http_request.content)
        model = models[body["model"]]
        paths = {"openai_chat": "/v1/chat/completions", "openai_responses": "/v1/responses", "anthropic": "/v1/messages"}
        assert http_request.url.path == paths[model.message_format]
        assert body["stream"] is True
        if model.message_format == "openai_responses":
            TypeAdapter(list[ResponseInputItemParam]).validate_python(body["input"])
            assert body["store"] is False
            assert all(tool["strict"] is True for tool in body.get("tools", []))
        items = body.get("input", body.get("messages", []))
        for item in items:
            if item.get("type") == "function_call_output":
                pending[model.model_id].discard(item["call_id"])
            elif item.get("role") == "tool":
                pending[model.model_id].discard(item["tool_call_id"])
            elif item.get("role") == "user" and isinstance(item.get("content"), list):
                for block in item["content"]:
                    if block.get("type") == "tool_result":
                        pending[model.model_id].discard(block["tool_use_id"])
        assert not pending.get(model.model_id), "Previous tool calls must have matching outputs"
        requests.append(body)
        assert replies, "Unexpected additional model request"
        reply = replies.pop(0)
        pending[model.model_id] = {call["id"] for call in reply.get("calls", [])}
        return auxiliary_sse_response(http_request, model.message_format, reply)

    def sdk_factory(sdk_class, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        kwargs["max_retries"] = 0
        client = sdk_class(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(llm_client, "_TrackedAsyncOpenAI", lambda **kwargs: sdk_factory(_TrackedAsyncOpenAI, **kwargs))
    monkeypatch.setattr(llm_client, "_TrackedAsyncAnthropic", lambda **kwargs: sdk_factory(_TrackedAsyncAnthropic, **kwargs))
    for module in (main_module, memory, teams, understand_image, stream_render):
        monkeypatch.setattr(module, "post_tui", Mock())
        if hasattr(module, "log_error_traceback"):
            monkeypatch.setattr(module, "log_error_traceback", Mock())
    monkeypatch.setattr(stream_render, "is_cancelled", lambda: False)
    monkeypatch.setattr(memory, "_compact_console", Mock())
    monkeypatch.setattr(memory, "_render_agent_response_message", Mock())
    monkeypatch.setattr(memory, "_MEMORY_RECALL_WINDOWS", {})
    monkeypatch.setattr(memory, "list_long_term_memories", lambda: [{
        "id": "mem_test", "category": "workflow", "insight": "durable test rule", "evidence": "test",
        "reuse_condition": "test trigger", "status": "active", "updated_at": "2026-01-01 00:00:00",
    }])
    monkeypatch.setattr(teams, "get_sub_agent_console", lambda: False)
    monkeypatch.setattr(teams.GLOBAL_MCP_MANAGER, "get_registry_snapshot", lambda: ([], {}))
    monkeypatch.setattr(teams, "_workdir", lambda: tmp_path)
    agent_context = teams.current_agent_role.set("test")
    try:
        yield SimpleNamespace(models=models, manager=manager, replies=replies, requests=requests, clients=clients)
    finally:
        teams.current_agent_role.reset(agent_context)
        for client in clients:
            await client.close()


def assert_auxiliary_tool_continuation(first, second):
    history_field = "input" if "input" in first else "messages"
    assert second[history_field][:len(first[history_field])] == first[history_field]
    for field in ("tools", "system", "instructions", "prompt_cache_key"):
        assert first.get(field) == second.get(field)
    assert "call_aux" in json.dumps(second)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["title", "recall", "memory"])
async def test_auxiliary_tool_loops_use_real_protocol_adapters(operation, auxiliary_gateway, monkeypatch):
    import main as main_module
    from utils import memory

    gateway = auxiliary_gateway
    names = {"title": "GenerateConversationTitle", "recall": "SelectRelevantMemories", "memory": "AppendLongTermMemory"}
    valid = {"title": {"title": "Test title"}, "recall": {"memory_ids": ["mem_test"]},
             "memory": {"category": "workflow", "insight": "rule", "evidence": "test", "reuse_condition": "trigger"}}
    for arguments in ({**valid[operation], "unexpected": True}, valid[operation]):
        gateway.replies.append({"calls": [{"id": "call_aux", "name": names[operation], "arguments": arguments}]})
    handler = Mock(return_value={"id": "mem_saved"})
    monkeypatch.setattr(memory, "LONG_TERM_MEMORY_TOOL_HANDLERS", {"AppendLongTermMemory": handler})
    if operation == "title":
        source = main_module._collect_user_message_content([
            {"role": "user", "content": [{"type": "text", "text": "title input"},
                                          {"type": "image", "data": b"private-image"}]},
        ])
        assert await main_module.generate_title(source) == "Test title"
    elif operation == "recall":
        assert await memory.select_relevant_memory_ids("recall input") == ["mem_test"]
    else:
        gateway.replies.append({"text": "memory done"})
        result = await memory.manual_memory_update("save rule", [{"role": "user", "content": "memory input"}])
        assert len(result) == 2
        handler.assert_called_once_with(**valid[operation])
        assert result[-1]["output"] == {"id": "mem_saved"}
    assert all(client.is_closed() for client in gateway.clients)
    assert not gateway.replies
    assert_auxiliary_tool_continuation(*gateway.requests[:2])
    second = json.dumps(gateway.requests[1])
    assert "extra_forbidden" in second
    assert "private-image" not in second
    assert {body["model"] for body in gateway.requests} == ({"recall"} if operation == "recall" else {"main"})


@pytest.fixture
def auxiliary_history():
    return [
        {"role": "system", "content": "private-system-marker"},
        {"role": "user", "content": [{"type": "text", "text": "old question"},
                                      {"type": "image", "media_type": "image/png", "data": b"private-image"}]},
        {"role": "assistant", "content": "old answer", "reasoning_content": "private-reasoning-marker",
         "tool_calls": [{"id": "call_old", "name": "Read", "arguments": '{"path":"file.py"}'}],
         "message_metadata": {"source_format": "openai_responses",
                              "native_blocks": [{"type": "reasoning", "encrypted_content": "private-native-marker"}]}},
        {"role": "tool", "tool_call_id": "call_old", "name": "Read", "content": "file output"},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": "latest answer"},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["full", "partial"])
async def test_compaction_and_memory_extraction_use_real_adapters(mode, auxiliary_gateway, auxiliary_history, monkeypatch):
    from utils import memory
    gateway = auxiliary_gateway
    gateway.replies.extend([{"text": "test summary"}, {"calls": [{"id": "call_aux", "name": "AppendLongTermMemory",
        "arguments": {"category": "workflow", "insight": "rule", "evidence": "test", "reuse_condition": "trigger"}}]},
        {"text": "memory done"}])
    handler = Mock(return_value={"id": "mem_saved"})
    monkeypatch.setattr(memory, "LONG_TERM_MEMORY_TOOL_HANDLERS", {"AppendLongTermMemory": handler})
    if mode == "partial":
        monkeypatch.setattr(memory, "_select_partial_compaction_range", lambda *args: (1, 4))
        assert await memory.partial_compact(auxiliary_history, 100, 100, "test")
    else:
        await memory.auto_compact(auxiliary_history, "test")
    assert "test summary" in auxiliary_history[1]["content"]
    handler.assert_called_once()
    assert len(gateway.clients) == 2 and gateway.clients[0] is not gateway.clients[1]
    assert all(client.is_closed() for client in gateway.clients)
    assert not gateway.replies
    summary_wire, memory_wire = (json.dumps(body) for body in gateway.requests[:2])
    assert ("latest question" in summary_wire) == (mode == "full")
    assert "latest question" in memory_wire and "old question" in memory_wire
    assert "file output" in summary_wire and "file output" in memory_wire
    for marker in ("private-image", "private-system-marker", "private-reasoning-marker", "private-native-marker"):
        assert marker not in summary_wire and marker not in memory_wire
    assert_auxiliary_tool_continuation(*gateway.requests[1:3])


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [False, True])
async def test_understand_image_uses_real_dedicated_protocol(failure, auxiliary_gateway, monkeypatch):
    from tools import understand_image
    gateway = auxiliary_gateway
    gateway.replies.append({"error": True} if failure else {"text": "small image"})
    monkeypatch.setattr(understand_image, "is_understand_image_enabled", lambda: True)
    monkeypatch.setattr(understand_image, "load_image_for_understanding", AsyncMock(return_value=(b"png", "image/png")))
    result = await understand_image.understand_image("describe", "fixture.png")
    assert ("auxiliary failure" in result and result.startswith("Error:")) if failure else result == "small image"
    assert all(client.is_closed() for client in gateway.clients)
    assert gateway.requests[0]["model"] == "vision"
    body = gateway.requests[0]
    user = next(item for item in body.get("input", body.get("messages")) if item.get("role") == "user")
    image = user["content"][0]
    image_type = {"openai_chat": "image_url", "openai_responses": "input_image", "anthropic": "image"}
    assert image["type"] == image_type[gateway.models["vision"].message_format]
    assert "cG5n" in json.dumps(image)
    assert not body.get("tools")


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["title", "recall", "memory", "summary", "partial_memory"])
async def test_auxiliary_request_failures_close_clients_without_committing_history(
    operation, auxiliary_gateway, auxiliary_history, monkeypatch,
):
    import main as main_module
    from utils import memory
    gateway = auxiliary_gateway
    original = copy.deepcopy(auxiliary_history)
    handler = Mock()
    monkeypatch.setattr(memory, "LONG_TERM_MEMORY_TOOL_HANDLERS", {"AppendLongTermMemory": handler})
    gateway.replies.append({"error": True})
    if operation == "title":
        assert await main_module.generate_title("test") is None
    elif operation == "recall":
        result = await memory.recall_long_term_memories("test")
        assert result["ids"] == [] and "auxiliary failure" in result["error"]
    elif operation == "memory":
        assert await memory.manual_memory_update("test", auxiliary_history) == []
    elif operation == "summary":
        gateway.replies.append({"error": True})  # Streaming summary's independent fallback request.
        with pytest.raises(Exception, match="auxiliary failure"):
            await memory.auto_compact(auxiliary_history, "test")
    else:
        gateway.replies.insert(0, {"text": "summary before memory failure"})
        monkeypatch.setattr(memory, "_select_partial_compaction_range", lambda *args: (1, 4))
        with pytest.raises(RuntimeError, match="auxiliary failure"):
            await memory.partial_compact(auxiliary_history, 100, 100, "test")
    assert auxiliary_history == original
    assert not gateway.replies
    assert all(client.is_closed() for client in gateway.clients)
    handler.assert_not_called()


@pytest.mark.anyio
async def test_sub_agent_and_completion_report_use_real_adapters(auxiliary_gateway, tmp_path):
    from utils import llm_client, teams
    gateway = auxiliary_gateway
    gateway.replies.extend([
        {"calls": [{"id": "call_aux", "name": "TodoUpdate", "arguments": {"todos": []}}]},
        {"text": "work done"}, {"text": "COMPLETION_STATUS: completed"},
    ])
    client = llm_client.create_current_async_llm_client()
    manager = teams.TeammateManager(tmp_path / "team")
    try:
        result = await manager._sub_agent_loop("1", "tester", "test task", tmp_path / "trace.jsonl", client)
        assert result["report"] == "COMPLETION_STATUS: completed"
        assert not client.client.is_closed()  # The delegation caller owns the client.
    finally:
        await llm_client.close_async_llm_client(client)
    assert not gateway.replies
    assert_auxiliary_tool_continuation(*gateway.requests[:2])
    report_wire = json.dumps(gateway.requests[2])
    assert "work done" in report_wire and "TodoUpdate" in report_wire
    assert "aux-encrypted" not in report_wire
    assert all(client.is_closed() for client in gateway.clients)


@pytest.mark.anyio
async def test_auxiliary_model_factories_fall_back_to_current_protocol(auxiliary_gateway, monkeypatch):
    from utils import llm_client
    gateway = auxiliary_gateway
    gateway.manager.get_memory_recall_model.return_value = None
    monkeypatch.setattr(llm_client, "get_understand_image_model", lambda: None)
    gateway.replies.extend([{"text": "recall fallback"}, {"text": "vision fallback"}])
    clients = [llm_client.create_memory_recall_llm_client(), llm_client.create_image_understanding_llm_client()]
    try:
        assert clients[0] is not clients[1]
        for client in clients:
            assert client.model == "main"
            assert [event async for event in client.generate_stream([{"role": "user", "content": "test"}])][-1]["type"] == "done"
    finally:
        for client in clients:
            await llm_client.close_async_llm_client(client)
    assert not gateway.replies
    assert all(client.is_closed() for client in gateway.clients)
