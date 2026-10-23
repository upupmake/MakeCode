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
