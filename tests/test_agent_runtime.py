import asyncio
import copy
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


class RecordingClient(FakeClient):
    def __init__(self, responses):
        normalized = [
            response if isinstance(response, list) else [response]
            for response in responses
        ]
        super().__init__(normalized)
        self.requests = []

    async def generate_stream(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        async for event in super().generate_stream(messages, tools):
            yield event


def _call(tool_id, name, arguments):
    return {"id": tool_id, "name": name, "arguments": arguments}


def _tool_done(*tool_calls):
    return {
        "type": "done",
        "result": SimpleNamespace(
            text="",
            tool_calls=list(tool_calls),
            assistant_message={"role": "assistant", "content": None},
        ),
    }


def _notification(*items, notification_id="ntf_test"):
    return {
        "id": notification_id,
        "version": 1,
        "count": len(items),
        "decisions": sum(1 for item in items if item["intent"] == "decision"),
        "items": list(items),
    }


def _decision_item(delivery_id="dlv_decision", text="完整的决策答复正文。"):
    return {
        "delivery_id": delivery_id,
        "message_id": "msg_1",
        "intent": "decision",
        "from": "user",
        "created_at": "2026-10-27T12:34:56Z",
        "text": text,
    }


def _todo_item(
    delivery_id="dlv_todo",
    preview="待办消息的短摘要。",
    text="这条全文不应该出现在通知里。",
):
    return {
        "delivery_id": delivery_id,
        "message_id": "msg_2",
        "intent": "todo",
        "from": "agent:agt_789",
        "created_at": "2026-10-27T12:35:00Z",
        "preview": preview,
        "text": text,
    }


def _runtime_with_tool(tmp_path, client, handler, monkeypatch):
    registry = ToolRegistry(include_ask_user=False)
    original_handlers = registry.handlers

    def handlers(mcp_handlers):
        handlers_map = original_handlers(mcp_handlers)
        handlers_map["EchoInjection"] = handler
        return handlers_map

    monkeypatch.setattr(registry, "handlers", handlers)
    return AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=registry,
        llm_client_factory=lambda **kwargs: client,
    )


def _injected_messages(messages):
    return [message for message in messages if message.get("notification") is True]


def test_notification_is_injected_before_next_model_request(tmp_path, monkeypatch):
    notification = _notification(
        _decision_item(
            text="完整的决策答复正文。\n第二行 <keep> & value."
        ),
        _todo_item(),
    )
    holder = {}

    def handler(**kwargs):
        holder["runtime"].enqueue_notification(notification)
        return "tool ok"

    client = RecordingClient(
        [_tool_done(_call("t1", "EchoInjection", {})), _done("finished")]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    result = asyncio.run(runtime.run_turn("start"))

    assert result.is_error is False
    assert len(client.requests) == 2
    assert _injected_messages(client.requests[0]) == []
    injected = _injected_messages(client.requests[1])
    assert len(injected) == 1
    message = injected[0]
    assert message["role"] == "user"
    assert message["notification_id"] == "ntf_test"
    assert message["delivery_ids"] == ["dlv_decision", "dlv_todo"]
    content = message["content"]
    assert content.startswith('<notification version="1" id="ntf_test"')
    assert 'count="2" decisions="1"' in content
    assert "其中 1 条需要决策" in content
    assert "完整的决策答复正文。\n第二行 &lt;keep&gt; &amp; value." in content
    assert "<text>完整的决策答复正文。" in content
    assert "<preview>待办消息的短摘要。</preview>" in content
    assert "这条全文不应该出现在通知里。" not in content
    assert content.count("</notification>") == 1


def test_notification_delivery_ids_are_injected_once_per_turn(tmp_path, monkeypatch):
    notification = _notification(_decision_item())
    holder = {}

    def handler(**kwargs):
        holder["runtime"].enqueue_notification(notification)
        return "ok"

    client = RecordingClient(
        [
            _tool_done(_call("t1", "EchoInjection", {})),
            _tool_done(_call("t2", "EchoInjection", {})),
            _done("finished"),
        ]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    result = asyncio.run(runtime.run_turn("start"))

    assert len(client.requests) == 3
    for request in client.requests[1:]:
        injected = _injected_messages(request)
        assert len(injected) == 1
        assert injected[0]["content"].count('delivery_id="dlv_decision"') == 1
    assert len(_injected_messages(result.messages)) == 1


def test_multiple_notifications_merge_into_one_model_message(tmp_path, monkeypatch):
    holder = {}

    def handler(**kwargs):
        runtime = holder["runtime"]
        runtime.enqueue_notification(
            _notification(_todo_item(), notification_id="ntf_a")
        )
        runtime.enqueue_notification(
            _notification(_decision_item(), notification_id="ntf_b")
        )
        return "ok"

    client = RecordingClient(
        [_tool_done(_call("t1", "EchoInjection", {})), _done("finished")]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    asyncio.run(runtime.run_turn("start"))

    injected = _injected_messages(client.requests[1])
    assert len(injected) == 1
    message = injected[0]
    assert message["notification_id"] == "ntf_a,ntf_b"
    assert message["delivery_ids"] == ["dlv_todo", "dlv_decision"]
    assert message["content"].count("<notification ") == 2


def test_notification_preserves_decision_text_and_escapes_xml(tmp_path, monkeypatch):
    text = '第一行\n第二行 <tag attr="x"> & 结束 </notification>'
    notification = _notification(_decision_item(text=text))
    holder = {}

    def handler(**kwargs):
        holder["runtime"].enqueue_notification(notification)
        return "ok"

    client = RecordingClient(
        [_tool_done(_call("t1", "EchoInjection", {})), _done("finished")]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    asyncio.run(runtime.run_turn("start"))

    content = _injected_messages(client.requests[1])[0]["content"]
    assert "第一行\n第二行 &lt;tag attr=\"x\"&gt; &amp; 结束" in content
    assert "&lt;/notification&gt;" in content
    assert content.count("</notification>") == 1


def test_duplicate_invalid_and_empty_notifications_are_ignored(tmp_path, monkeypatch):
    duplicate = _notification(_decision_item(delivery_id="dlv_dup"))["items"][0]
    holder = {}

    def handler(**kwargs):
        runtime = holder["runtime"]
        runtime.enqueue_notification(None)
        runtime.enqueue_notification({})
        runtime.enqueue_notification({"id": "ntf_bad", "version": 99, "items": []})
        runtime.enqueue_notification({"id": "ntf_bad", "items": "nope"})
        runtime.enqueue_notification(
            _notification({"delivery_id": "", "intent": "todo", "preview": "x"})
        )
        runtime.enqueue_notification(
            _notification(
                {"delivery_id": "d1", "intent": "unknown", "preview": "x"}
            )
        )
        runtime.enqueue_notification(
            _notification({"delivery_id": "d2", "intent": "decision"})
        )
        runtime.enqueue_notification(
            _notification({"delivery_id": "d3", "intent": "todo"})
        )
        runtime.enqueue_notification(_notification())
        runtime.enqueue_notification(
            _notification(duplicate, dict(duplicate), notification_id="ntf_dup")
        )
        return "ok"

    client = RecordingClient(
        [_tool_done(_call("t1", "EchoInjection", {})), _done("finished")]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    result = asyncio.run(runtime.run_turn("start"))

    assert result.is_error is False
    injected = _injected_messages(client.requests[1])
    assert len(injected) == 1
    assert injected[0]["content"].count('delivery_id="dlv_dup"') == 1
    assert injected[0]["delivery_ids"] == ["dlv_dup"]


def test_generic_injection_uses_caller_defined_content(tmp_path, monkeypatch):
    holder = {}

    def handler(**kwargs):
        runtime = holder["runtime"]
        runtime.enqueue_injection(
            "CALLER DEFINED CONTEXT",
            metadata={"source": "custom"},
            dedupe_ids=["custom-1"],
        )
        runtime.enqueue_injection(
            "CALLER DEFINED CONTEXT",
            metadata={"source": "custom"},
            dedupe_ids=["custom-1"],
        )
        return "ok"

    client = RecordingClient(
        [_tool_done(_call("t1", "EchoInjection", {})), _done("finished")]
    )
    runtime = _runtime_with_tool(tmp_path, client, handler, monkeypatch)
    holder["runtime"] = runtime

    asyncio.run(runtime.run_turn("start"))

    injected = [
        message
        for message in client.requests[1]
        if message.get("source") == "custom"
    ]
    assert len(injected) == 1
    assert injected[0]["role"] == "user"
    assert injected[0]["content"] == "CALLER DEFINED CONTEXT"
    assert injected[0]["delivery_ids"] == ["custom-1"]


def test_injection_queue_drops_oldest_when_full(tmp_path, caplog):
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
    )
    runtime._pending_injections = asyncio.Queue(maxsize=2)
    for index in range(3):
        runtime.enqueue_notification(
            _notification(_todo_item(), notification_id=f"ntf_{index}")
        )

    records = runtime._drain_injections()
    assert len(records) == 2
    contents = [record.render(set())[0] for record in records]
    assert all('id="ntf_0"' not in content for content in contents)
    assert any("消息注入队列已满" in record.message for record in caplog.records)


def test_notification_queued_after_turn_is_injected_next_turn(tmp_path):
    client = RecordingClient([_done("first"), _done("second")])
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: client,
    )

    asyncio.run(runtime.run_turn("one"))
    runtime.enqueue_notification(_notification(_decision_item()))
    asyncio.run(runtime.run_turn("two"))

    assert _injected_messages(client.requests[0]) == []
    injected = _injected_messages(client.requests[1])
    assert len(injected) == 1
    assert "完整的决策答复正文。" in injected[0]["content"]


def test_run_turn_injects_project_instructions_once(tmp_path):
    (tmp_path / "AGENTS.md").write_text("RUNTIME_RULE", encoding="utf-8")
    responses = [[_done("one")], [_done("two")], [_done("three")]]
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        llm_client_factory=lambda **kwargs: _fake_client(*responses.pop(0)),
    )

    first = asyncio.run(runtime.run_turn("first"))
    assert "RUNTIME_RULE" not in first.text
    users = [message for message in first.messages if message.get("role") == "user"]
    assert "RUNTIME_RULE" in users[-1]["content"]
    state = users[-1]["message_metadata"]["project_instructions"]
    assert state["kind"] == "initial"
    assert users[-1]["message_metadata"]["display_content"] == "first"

    second = asyncio.run(runtime.run_turn("second"))
    users = [message for message in second.messages if message.get("role") == "user"]
    assert users[-1] == {"role": "user", "content": "second"}
    assert "RUNTIME_RULE" in users[0]["content"]

    (tmp_path / "AGENTS.md").write_text("RUNTIME_RULE_V2", encoding="utf-8")
    third = asyncio.run(runtime.run_turn("third"))
    users = [message for message in third.messages if message.get("role") == "user"]
    assert "RUNTIME_RULE_V2" in users[-1]["content"]
    assert "replace all previously provided project instructions" in users[-1]["content"]
    state = users[-1]["message_metadata"]["project_instructions"]
    assert state["kind"] == "replacement"


def test_system_prompt_does_not_embed_project_instructions(tmp_path):
    (tmp_path / "AGENTS.md").write_text("SYSTEM_PROMPT_SECRET", encoding="utf-8")
    runtime = AgentRuntime(
        store=ConversationStore(tmp_path / "conversations"),
        registry=ToolRegistry(include_ask_user=False),
        headless=True,
    )

    assert "SYSTEM_PROMPT_SECRET" not in runtime._system_prompt()
