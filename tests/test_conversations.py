import json

import pytest

from utils.conversations import (
    CONVERSATION_FILE,
    SCHEMA_VERSION,
    SUB_AGENT_HISTORY_FILE,
    TASK_PLAN_FILE,
    ConversationStore,
)
from utils.llm_client import build_anthropic_request_messages, sanitize_openai_messages


def test_conversation_store_saves_and_loads_bound_history(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]

    conversation_path = store.save_messages(messages)
    root = conversation_path.parent
    task_plan = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "epic_id": "epic1234",
        "next_id": 2,
        "tasks": {"1": {"id": "1", "subject": "Work", "description": "", "status": "pending", "depend_on": []}},
    }
    (root / TASK_PLAN_FILE).write_text(json.dumps(task_plan), encoding="utf-8")
    history_path = root / SUB_AGENT_HISTORY_FILE
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "records": [{"conversation_id": store.active_id, "plan_task_id": "1", "status": "completed"}],
    }), encoding="utf-8")

    snapshot = store.load(conversation_path)

    assert conversation_path.name == CONVERSATION_FILE
    assert conversation_path.parent.name == store.active_id
    assert snapshot.messages == messages
    assert snapshot.task_plan == task_plan
    assert snapshot.sub_agent_history == [
        {"conversation_id": store.active_id, "plan_task_id": "1", "status": "completed"}
    ]


def test_sidecars_and_traces_never_enter_provider_messages(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ])
    task_marker = "TASK_PLAN_ONLY_MARKER"
    history_marker = "SUB_AGENT_HISTORY_ONLY_MARKER"
    trace_marker = "SUB_AGENT_TRACE_ONLY_MARKER"
    (conversation.parent / TASK_PLAN_FILE).write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "epic_id": "epic1234",
        "next_id": 2,
        "tasks": {
            "1": {
                "id": "1",
                "subject": task_marker,
                "description": "",
                "status": "pending",
                "depend_on": [],
            }
        },
    }), encoding="utf-8")
    history_path = conversation.parent / SUB_AGENT_HISTORY_FILE
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "records": [{
            "conversation_id": store.active_id,
            "plan_task_id": "1",
            "report": history_marker,
        }],
    }), encoding="utf-8")
    trace_path = conversation.parent / "sub_agents" / "runs" / "run_test" / "task_1_trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(trace_marker, encoding="utf-8")

    snapshot = store.load(conversation)
    openai_request = sanitize_openai_messages(snapshot.messages)
    system, anthropic_request = build_anthropic_request_messages(
        snapshot.messages,
        "claude-test",
    )
    provider_payload = json.dumps(
        {"openai": openai_request, "system": system, "anthropic": anthropic_request},
        ensure_ascii=False,
    )

    assert task_marker not in provider_payload
    assert history_marker not in provider_payload
    assert trace_marker not in provider_payload


def test_conversation_store_updates_title_without_renaming_directory(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    path = store.save_messages([{"role": "system", "content": "system"}])
    original_root = path.parent

    store.update_title("新标题")

    assert store.active_title == "新标题"
    assert store.active_path == path
    assert path.parent == original_root
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "新标题"


def test_conversation_store_rejects_legacy_checkpoint_arrays(tmp_path):
    path = tmp_path / "conversations" / "conv_00000000000000000000000000000000" / CONVERSATION_FILE
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid 6.0 conversation data"):
        ConversationStore(tmp_path / "conversations").load(path)


def test_conversation_store_rejects_mismatched_sidecar_id(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    path = store.save_messages([{"role": "system", "content": "system"}])
    (path.parent / TASK_PLAN_FILE).write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": "conv_other",
        "epic_id": "epic1234",
        "next_id": 1,
        "tasks": {},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="sidecar ID mismatch"):
        store.load(path)


def test_conversation_store_lists_only_new_format_conversations(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    new_path = store.save_messages([{"role": "system", "content": "system"}])
    legacy_dir = tmp_path / "checkpoint"
    legacy_dir.mkdir()
    (legacy_dir / "ckpt_old.json").write_text("[]", encoding="utf-8")
    invalid_path = (
        tmp_path
        / "conversations"
        / "conv_00000000000000000000000000000000"
        / CONVERSATION_FILE
    )
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("[]", encoding="utf-8")

    assert store.list_conversations() == [new_path]


def test_conversation_store_rejects_manifest_outside_its_root(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    outside_store = ConversationStore(tmp_path / "other")
    outside_path = outside_store.save_messages([{"role": "system", "content": "system"}])

    with pytest.raises(ValueError, match="Invalid 6.0 conversation path"):
        store.load(outside_path)


def test_task_manager_rejects_invalid_loaded_plan(tmp_path):
    from utils.tasks import TaskManager

    data = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": "conv_test",
        "epic_id": "epic1234",
        "next_id": 1,
        "tasks": {
            "1": {
                "id": "1",
                "subject": "Broken",
                "description": "",
                "status": "pending",
                "depend_on": [],
            }
        },
    }

    with pytest.raises(ValueError, match="next_id"):
        TaskManager(tmp_path / "conv_test", "conv_test", data)


def test_task_manager_persists_conversation_binding(tmp_path):
    from utils.tasks import TaskManager

    root = tmp_path / "conv_test"
    manager = TaskManager(root, "conv_test")
    manager.create_tasks([{
        "subject": "Bound task",
        "description": "",
        "depend_on": [],
        "status": "pending",
    }])

    saved = json.loads((root / TASK_PLAN_FILE).read_text(encoding="utf-8"))
    assert saved["conversation_id"] == "conv_test"
    assert saved["tasks"]["1"]["subject"] == "Bound task"


@pytest.mark.anyio
async def test_teammate_manager_persists_conversation_binding(tmp_path):
    import asyncio

    from utils.teams import TeammateManager

    root = tmp_path / "conv_test"
    manager = TeammateManager(
        root,
        "conv_test",
        [{"conversation_id": "conv_test", "plan_task_id": "1"}],
    )
    await manager._save_history(asyncio.Lock())

    saved = json.loads((root / SUB_AGENT_HISTORY_FILE).read_text(encoding="utf-8"))
    assert saved == {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": "conv_test",
        "records": [{"conversation_id": "conv_test", "plan_task_id": "1"}],
    }


def test_teammate_manager_rejects_mismatched_history_binding(tmp_path):
    from utils.teams import TeammateManager

    with pytest.raises(ValueError, match="history conversation ID mismatch"):
        TeammateManager(
            tmp_path / "conv_test",
            "conv_test",
            [{"conversation_id": "conv_other", "plan_task_id": "1"}],
        )
