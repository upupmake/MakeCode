import asyncio
from types import SimpleNamespace

import pytest

import system.agent_runtime as runtime_module
from system.agent_runtime import AgentRuntime
from system.tool_registry import ToolRegistry
from utils.conversations import ConversationStore


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def format_tools(self, tools):
        return tools

    def append_assistant_message(self, messages, raw_message):
        messages.append(raw_message)

    def format_tool_result(self, tool_id, tool_name, output):
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": output,
        }

    async def generate_stream(self, messages, tools):
        for event in self._responses.pop(0):
            yield event


class BlockingClient(FakeClient):
    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def generate_stream(self, messages, tools):
        self.started.set()
        await asyncio.Event().wait()
        yield


def _done(text):
    return {
        "type": "done",
        "result": SimpleNamespace(
            text=text,
            tool_calls=[],
            assistant_message={"role": "assistant", "content": text},
        ),
    }


def _fake_client(*responses):
    return FakeClient(responses)


@pytest.fixture(autouse=True)
def isolate_runtime_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "get_memory_pre_recall", lambda: False)
    monkeypatch.setattr(
        runtime_module.tasks_module,
        "activate_conversation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        runtime_module.teams_module,
        "activate_conversation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        runtime_module.SKILL_LOADER,
        "refresh_workspace",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runtime_module.paths, "workdir", lambda: tmp_path)


def test_run_turn_emits_text_and_persists_session(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    events = []
    runtime = AgentRuntime(
        store=store,
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: _fake_client(
            [
                {"type": "text", "content": "hello "},
                {"type": "text", "content": "world"},
                _done("hello world"),
            ]
        ),
        event_handler=events.append,
    )

    result = asyncio.run(runtime.run_turn("hi"))

    assert result.is_error is False
    assert result.text == "hello world"
    assert result.session_id == store.active_id
    assert [event["type"] for event in events] == [
        "text_delta",
        "text_delta",
        "assistant_done",
    ]
    snapshot = store.load(store.active_path)
    assert snapshot.messages[-1] == {"role": "assistant", "content": "hello world"}
    assert snapshot.runtime is not None


def test_run_turn_reports_missing_model(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    runtime = AgentRuntime(
        store=store,
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: None,
    )

    result = asyncio.run(runtime.run_turn("hi"))

    assert result.is_error is True
    assert result.error == "No model configured"
    assert result.session_id == store.active_id


def test_cancel_current_response_cancels_running_turn(tmp_path, monkeypatch):
    import system.stream_cancel as stream_cancel

    monkeypatch.setattr(stream_cancel, "post_tui", lambda *args, **kwargs: None)
    store = ConversationStore(tmp_path / "conversations")

    async def scenario():
        client = BlockingClient()
        runtime = AgentRuntime(
            store=store,
            registry=ToolRegistry(include_ask_user=False),
            llm_client_factory=lambda **kwargs: client,
        )
        stream_cancel.start_cancel_listener()
        try:
            task = asyncio.create_task(runtime.run_turn("hi"))
            await client.started.wait()
            assert stream_cancel.cancel_current_response() is True
            return await asyncio.wait_for(task, timeout=2)
        finally:
            stream_cancel.stop_cancel_listener()

    result = asyncio.run(scenario())

    assert result.cancelled is True
    assert result.is_error is False
    assert result.text == ""


def test_restore_loads_snapshot_messages(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    path = store.save_messages(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "previous"},
        ]
    )
    snapshot = store.load(path)
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
    )

    runtime.restore(snapshot)

    assert runtime.messages == snapshot.messages
    assert runtime.store.active_id == snapshot.conversation_id


def test_system_prompt_includes_headless_note_and_append_prompt(tmp_path):
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        append_system_prompt="APPENDED MARKER",
        headless=True,
    )

    prompt = runtime._system_prompt()

    assert "APPENDED MARKER" in prompt
    assert "# Headless Mode" in prompt


def test_system_prompt_is_built_once_for_cache_stability(tmp_path, monkeypatch):
    calls = []

    def fake_prompt(workdir, label, plan_mode=False):
        calls.append(workdir)
        return f"prompt-{len(calls)}"

    monkeypatch.setattr(runtime_module, "get_orchestrator_system_prompt", fake_prompt)
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        headless=True,
    )

    first = runtime._system_prompt()
    second = runtime._system_prompt()

    assert first == second
    assert len(calls) == 1


def test_run_turn_aggregates_provider_usage(tmp_path):
    class UsageClient(FakeClient):
        async def generate_stream(self, messages, tools):
            yield {
                "type": "done",
                "result": SimpleNamespace(
                    text="ok",
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": "ok"},
                    usage={
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 60},
                        "output_tokens": 5,
                    },
                ),
            }

    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: UsageClient([]),
    )

    result = asyncio.run(runtime.run_turn("hi"))

    assert result.usage["input_tokens"] == 100
    assert result.usage["cached_tokens"] == 60
    assert result.usage["uncached_input_tokens"] == 40
    assert result.usage["output_tokens"] == 5
    assert result.usage["context_tokens"] > 0


def test_anthropic_usage_is_normalized_to_total_input(tmp_path):
    class UsageClient(FakeClient):
        async def generate_stream(self, messages, tools):
            yield {
                "type": "done",
                "result": SimpleNamespace(
                    text="ok",
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": "ok"},
                    usage={
                        "input_tokens": 41,
                        "cache_read_input_tokens": 17280,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 3,
                    },
                ),
            }

    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: UsageClient([]),
    )

    result = asyncio.run(runtime.run_turn("hi"))

    assert result.usage["input_tokens"] == 17321
    assert result.usage["cached_tokens"] == 17280
    assert result.usage["uncached_input_tokens"] == 41
    assert result.usage["output_tokens"] == 3


def test_headless_prompt_reports_ask_user_available(tmp_path):
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=True),
        headless=True,
        ask_user_available=True,
    )

    prompt = runtime._system_prompt()

    assert "AskUser is available" in prompt
    assert "AskUser and interactive HITL prompts are unavailable" not in prompt


def test_headless_prompt_reports_ask_user_unavailable(tmp_path):
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        headless=True,
    )

    prompt = runtime._system_prompt()

    assert "AskUser and interactive HITL prompts are unavailable" in prompt


def _compact_runtime(tmp_path):
    events = []
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        event_handler=events.append,
    )
    runtime.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {"role": "tool", "content": "old output"},
    ]
    return runtime, events


def test_compact_emits_tool_output_start_and_end_events(tmp_path, monkeypatch):
    runtime, events = _compact_runtime(tmp_path)
    token_values = iter([75, 40])
    monkeypatch.setattr(runtime_module, "get_context_token_limit", lambda: 100)
    monkeypatch.setattr(runtime_module, "get_compaction_thresholds", lambda: (70, 90))
    monkeypatch.setattr(
        runtime_module,
        "estimate_tokens",
        lambda *args, **kwargs: next(token_values),
    )
    monkeypatch.setattr(runtime_module, "compact_tool_outputs", lambda messages: True)

    asyncio.run(runtime._compact([]))

    assert events == [
        {
            "type": "compact",
            "phase": "start",
            "scope": "tool_output",
            "tokens_before": 75,
            "threshold_percent": 70,
        },
        {
            "type": "compact",
            "phase": "end",
            "scope": "tool_output",
            "status": "success",
            "changed": True,
            "tokens_before": 75,
            "tokens_after": 40,
        },
    ]
    assert runtime._result("done").compactions == events


def test_compact_emits_partial_summary_memory_and_end_events(tmp_path, monkeypatch):
    runtime, events = _compact_runtime(tmp_path)
    token_values = iter([95, 30])

    async def fake_partial_compact(
        messages,
        context_token_limit,
        current_context_tokens,
        reason,
        *,
        on_summary_delta=None,
        on_summary=None,
        on_memory_start=None,
        on_memory_end=None,
    ):
        on_summary_delta("sum")
        on_summary_delta("mary")
        on_summary("summary")
        on_memory_start()
        on_memory_end(None)
        return True

    monkeypatch.setattr(runtime_module, "get_context_token_limit", lambda: 100)
    monkeypatch.setattr(runtime_module, "get_compaction_thresholds", lambda: (70, 90))
    monkeypatch.setattr(
        runtime_module,
        "estimate_tokens",
        lambda *args, **kwargs: next(token_values),
    )
    monkeypatch.setattr(runtime_module, "partial_compact", fake_partial_compact)
    monkeypatch.setattr(
        runtime_module,
        "compact_tool_outputs",
        lambda messages: pytest.fail("layer 1 must not run after partial success"),
    )

    asyncio.run(runtime._compact([]))

    assert [event["phase"] for event in events] == [
        "start",
        "summary_delta",
        "summary_delta",
        "summary",
        "memory_start",
        "memory_end",
        "end",
    ]
    assert events[0]["scope"] == "partial"
    assert events[1]["text"] == "sum"
    assert events[2]["text"] == "mary"
    assert events[3]["summary"] == "summary"
    assert events[4]["scope"] == "partial"
    assert events[6]["status"] == "success"
    assert events[6]["committed"] is True
    assert events[6]["tokens_before"] == 95
    assert events[6]["tokens_after"] == 30
    stored = runtime._result("done").compactions
    assert [event["phase"] for event in stored] == [
        "start",
        "summary",
        "memory_start",
        "memory_end",
        "end",
    ]


def test_compact_falls_back_to_tool_output_after_partial_failure(tmp_path, monkeypatch):
    runtime, events = _compact_runtime(tmp_path)
    token_values = iter([95, 40])

    async def failing_partial_compact(*args, **kwargs):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(runtime_module, "get_context_token_limit", lambda: 100)
    monkeypatch.setattr(runtime_module, "get_compaction_thresholds", lambda: (70, 90))
    monkeypatch.setattr(
        runtime_module,
        "estimate_tokens",
        lambda *args, **kwargs: next(token_values),
    )
    monkeypatch.setattr(runtime_module, "partial_compact", failing_partial_compact)
    monkeypatch.setattr(runtime_module, "compact_tool_outputs", lambda messages: True)

    asyncio.run(runtime._compact([]))

    assert [(event["phase"], event["scope"]) for event in events] == [
        ("start", "partial"),
        ("end", "partial"),
        ("start", "tool_output"),
        ("end", "tool_output"),
    ]
    assert events[1]["status"] == "failed"
    assert events[1]["committed"] is False
    assert events[1]["error"] == "summary failed"
    assert events[1]["tokens_after"] == 95
    assert events[3]["tokens_before"] == 95
    assert events[3]["tokens_after"] == 40


def test_compact_reports_skipped_partial_before_tool_output_fallback(tmp_path, monkeypatch):
    runtime, events = _compact_runtime(tmp_path)
    token_values = iter([95, 40])

    async def no_range(*args, **kwargs):
        return False

    monkeypatch.setattr(runtime_module, "get_context_token_limit", lambda: 100)
    monkeypatch.setattr(runtime_module, "get_compaction_thresholds", lambda: (70, 90))
    monkeypatch.setattr(
        runtime_module,
        "estimate_tokens",
        lambda *args, **kwargs: next(token_values),
    )
    monkeypatch.setattr(runtime_module, "partial_compact", no_range)
    monkeypatch.setattr(runtime_module, "compact_tool_outputs", lambda messages: False)

    asyncio.run(runtime._compact([]))

    assert [(event["phase"], event["scope"]) for event in events] == [
        ("start", "partial"),
        ("end", "partial"),
        ("start", "tool_output"),
        ("end", "tool_output"),
    ]
    assert events[1]["status"] == "skipped"
    assert events[1]["error"] is None
    assert events[3]["status"] == "noop"
    assert events[3]["tokens_after"] == 95
