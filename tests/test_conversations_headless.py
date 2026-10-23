import json
import os
import time

import pytest

from utils.conversations import (
    CONVERSATION_FILE,
    RUNTIME_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SUB_AGENT_HISTORY_FILE,
    TASK_PLAN_FILE,
    ConversationLock,
    ConversationLockError,
    ConversationStore,
)


def test_create_normalizes_session_ids(tmp_path):
    store = ConversationStore(tmp_path / "conversations")

    path = store.create("0123456789abcdef0123456789abcdef")

    assert store.active_id == "conv_0123456789abcdef0123456789abcdef"
    assert path == (
        tmp_path
        / "conversations"
        / "conv_0123456789abcdef0123456789abcdef"
        / CONVERSATION_FILE
    )


def test_create_accepts_uuid_with_hyphens(tmp_path):
    store = ConversationStore(tmp_path / "conversations")

    store.create("01234567-89ab-cdef-0123-456789abcdef")

    assert store.active_id == "conv_0123456789abcdef0123456789abcdef"


def test_create_rejects_existing_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    store.create("conv_0123456789abcdef0123456789abcdef")
    store.save_messages([{"role": "user", "content": "first"}])

    with pytest.raises(FileExistsError):
        store.create("conv_0123456789abcdef0123456789abcdef")


def test_find_latest_returns_newest_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    first = store.save_messages([{"role": "user", "content": "first"}])
    time.sleep(0.01)
    second = store.save_messages([{"role": "user", "content": "second"}])
    old = time.time() - 60
    os.utime(first, (old, old))

    latest = store.find_latest()

    assert latest is not None
    assert latest.path == second
    assert latest.messages[-1]["content"] == "second"


def test_runtime_metadata_round_trips_with_schema_v2(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    runtime = {"model": "test-model", "permission_mode": "default"}

    path = store.save_messages(
        [{"role": "user", "content": "hello"}],
        runtime=runtime,
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    snapshot = store.load(path)

    assert manifest["schema_version"] == RUNTIME_SCHEMA_VERSION
    assert manifest["runtime"] == runtime
    assert snapshot.runtime == runtime
    assert snapshot.messages[0]["content"] == "hello"


def test_persist_false_does_not_write_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations", persist=False)

    path = store.save_messages([{"role": "user", "content": "hello"}])

    assert store.active_id is not None
    assert path.exists() is False
    assert (tmp_path / "conversations").exists() is False


def test_fork_copies_messages_and_rewrites_conversation_ids(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    path = store.save_messages(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        runtime={"model": "test-model"},
    )
    original_id = store.active_id
    task_plan = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": original_id,
        "epic_id": "epic1234",
        "next_id": 1,
        "tasks": {},
    }
    (path.parent / TASK_PLAN_FILE).write_text(json.dumps(task_plan), encoding="utf-8")
    history_path = path.parent / SUB_AGENT_HISTORY_FILE
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": original_id,
                "records": [],
            }
        ),
        encoding="utf-8",
    )

    snapshot = store.load(path)
    forked = store.fork(snapshot, "fedcba9876543210fedcba9876543210")

    forked_root = forked.root
    forked_manifest = json.loads(
        (forked_root / CONVERSATION_FILE).read_text(encoding="utf-8")
    )
    forked_task_plan = json.loads(
        (forked_root / TASK_PLAN_FILE).read_text(encoding="utf-8")
    )
    forked_history = json.loads(
        (forked_root / SUB_AGENT_HISTORY_FILE).read_text(encoding="utf-8")
    )

    assert forked.conversation_id == "conv_fedcba9876543210fedcba9876543210"
    assert forked.messages == snapshot.messages
    assert forked.runtime == {"model": "test-model"}
    assert forked_manifest["conversation_id"] == forked.conversation_id
    assert forked_task_plan["conversation_id"] == forked.conversation_id
    assert forked_history["conversation_id"] == forked.conversation_id
    assert snapshot.root != forked.root


def test_conversation_lock_blocks_a_second_holder(tmp_path):
    root = tmp_path / "conversation"
    first = ConversationLock(root)
    first.acquire()
    try:
        with pytest.raises(ConversationLockError):
            ConversationLock(root).acquire()
    finally:
        first.release()

    with ConversationLock(root):
        pass
