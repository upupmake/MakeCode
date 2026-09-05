import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from openai import APIError
from rich.panel import Panel
from rich.text import Text

from system.models import MESSAGE_FORMATS, ModelConfig, ModelManager, REASONING_EFFORTS
from system import console_render, ts_validator, updater, window_attention
from system.commands import CommandAction, CommandHandler, CommandResult
from system.tool_history import TOOL_EXECUTION_HISTORY
from system.tui_modals import AddMemoryModal, AddModelModal, ChoiceModal, InfoPanelModal, McpSwitchModal, McpToolsModal, McpViewModal, MemoryConfigModal, MemoryPanelModal, RecallModelPickerModal, LayoutModal, ModelManagerModal, EditModelModal, TaskPanelModal
from utils import llm_client as llm_client_module, memory
from utils.conversations import ConversationStore
from utils.llm_client import (
    AsyncChatAPIClient,
    build_llm_result,
    create_memory_recall_llm_client,
    sanitize_openai_messages,
    strip_native_message_payloads,
)
import main as main_module


def test_render_current_workdir_preserves_windows_drive_root():
    with patch("system.console_render.paths.workdir", return_value="D:\\"), \
            patch("system.console_render.post_tui") as post_tui:
        console_render.render_current_workdir()

    payload = post_tui.call_args.args[1]
    assert Text.from_markup(payload).plain == "📂 当前工作目录: D:\\"


MEMORY_RECORDS = [
    {
        "id": "mem_a",
        "category": "workflow",
        "updated_at": "2026-01-01 00:00:00",
        "insight": "a",
        "reuse_condition": "when a",
        "status": "active",
    }
]


class FakeRecallClient:
    def __init__(self, selected_ids=None):
        self.selected_ids = selected_ids or ["mem_a"]
        self.generate_calls = 0
        self.messages = []

    def format_tools(self, tools):
        return tools

    async def generate_stream(self, messages, tools):
        self.generate_calls += 1
        self.messages.append(list(messages))
        tool_calls = [{
            "id": "call_1",
            "name": "SelectRelevantMemories",
            "arguments": json.dumps({"memory_ids": self.selected_ids}),
            "raw": {},
        }]
        yield {
            "type": "done",
            "result": SimpleNamespace(
                text="",
                tool_calls=tool_calls,
                assistant_message={"role": "assistant", "content": ""},
            ),
        }


def test_model_manager_persists_and_updates_model_alias(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model(
        "https://example.com",
        "key",
        ["main", "recall"],
        aliases=["日常模型", "记忆模型"],
    )

    assert [model.alias for model in models] == ["日常模型", "记忆模型"]
    assert models[0].get_display_text() == "日常模型 · main (example.com)"

    assert manager.update_model_by_key(
        models[0].key,
        "https://example.com",
        "key",
        "main",
        "openai_chat",
        "  新   名称  ",
    )
    saved = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert saved["models"][0]["alias"] == "新 名称"

    reloaded = ModelManager(tmp_path)
    assert reloaded.models[0].alias == "新 名称"

    assert manager.get_current_model().alias == "新 名称"

    assert reloaded.update_model_by_key(
        reloaded.models[0].key,
        "https://example.com",
        "key",
        "main",
        "openai_chat",
        "",
    )
    assert ModelManager(tmp_path).models[0].alias == ""


def test_model_manager_updates_model_config_and_references(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com/", "key", ["main", "other"])
    manager.toggle_favorite_by_index(0)
    manager.set_reasoning_effort(models[0].key, "high")
    manager.set_memory_recall_model_by_key(models[0].key)

    updated = manager.update_model_by_key(
        models[0].key,
        "https://updated.example.com/v1/",
        "new-key",
        "updated-model",
        "anthropic",
        "  新   名称  ",
    )

    assert updated is not None
    assert updated.base_url == "https://updated.example.com/v1"
    assert updated.api_key == "new-key"
    assert updated.model_id == "updated-model"
    assert updated.message_format == "anthropic"
    assert updated.alias == "新 名称"
    assert updated.is_favorite is True
    assert updated.reasoning_effort == "high"
    assert manager.current_model_key == updated.key
    assert manager.last_selected_key == updated.key
    assert manager.memory_recall_model_key == updated.key

    saved = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert saved["last_selected"]["model_id"] == "updated-model"
    assert saved["memory_recall_model"]["model_id"] == "updated-model"


def test_model_manager_rejects_edit_that_duplicates_existing_model(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main", "other"])
    original = manager.models[0].to_dict()

    updated = manager.update_model_by_key(
        manager.models[0].key,
        "https://example.com",
        "key",
        "other",
        "openai_chat",
        "",
    )

    assert updated is None
    assert manager.models[0].to_dict() == original


def test_model_manager_add_model_alias_list_stays_positional(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model(
        "https://example.com",
        "key",
        ["main", "recall", "deep"],
        aliases=["日常模型", "", "深度模型"],
    )

    assert [model.alias for model in models] == ["日常模型", "", "深度模型"]


def test_model_manager_persists_and_clears_memory_recall_model(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["main", "recall"])
    recall_key = models[1].key

    assert manager.set_memory_recall_model_by_key(recall_key)
    reloaded = ModelManager(tmp_path)

    assert reloaded.get_memory_recall_model().key == recall_key
    assert reloaded.get_memory_recall_model_display_text() == models[1].get_display_text()

    assert reloaded.delete_model_by_key(recall_key)
    assert reloaded.get_memory_recall_model() is None
    assert reloaded.get_memory_recall_model_display_text() == "同主模型"


def test_memory_recall_model_stays_cleared_after_reload(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["main", "recall"])
    recall_key = models[1].key

    assert manager.set_memory_recall_model_by_key(recall_key)

    manager2 = ModelManager(tmp_path)
    assert manager2.delete_model_by_key(recall_key)

    manager3 = ModelManager(tmp_path)
    assert manager3.get_memory_recall_model() is None
    assert manager3.get_memory_recall_model_display_text() == "同主模型"

    saved = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert saved["memory_recall_model"] is None


def test_model_manager_preserves_unknown_top_level_fields_and_removes_legacy_max_context(tmp_path):
    config_file = tmp_path / "model_config.json"
    config_file.write_text(json.dumps({
        "version": 2,
        "extra_field": {"keep": True},
        "models": [
            {
                "base_url": "https://example.com",
                "api_key": "key",
                "model_id": "main",
                "is_favorite": False,
                "max_context": 128,
            }
        ],
    }), encoding="utf-8")

    manager = ModelManager(tmp_path)
    assert manager.toggle_favorite_by_index(0)

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["extra_field"] == {"keep": True}
    assert saved["models"][0]["is_favorite"] is True
    assert "max_context" not in saved["models"][0]


def test_model_manager_does_not_overwrite_unreadable_config(tmp_path):
    config_file = tmp_path / "model_config.json"
    original_content = '{"models": '
    config_file.write_text(original_content, encoding="utf-8")

    manager = ModelManager(tmp_path)

    assert manager.load_error is not None
    assert manager.add_model("https://example.com", "key", ["main"]) == []
    assert config_file.read_text(encoding="utf-8") == original_content


def test_model_manager_accepts_null_selected_model_fields(tmp_path):
    config_file = tmp_path / "model_config.json"
    config_file.write_text(json.dumps({
        "version": 2,
        "last_selected": None,
        "memory_recall_model": None,
        "models": [
            {
                "base_url": "https://example.com",
                "api_key": "key",
                "model_id": "main",
                "is_favorite": False,
                "max_context": 128,
            }
        ],
    }), encoding="utf-8")

    manager = ModelManager(tmp_path)

    assert manager.load_error is None
    assert manager.get_current_model().model_id == "main"
    assert manager.get_memory_recall_model() is None


def test_model_manager_does_not_overwrite_null_top_level_config(tmp_path):
    config_file = tmp_path / "model_config.json"
    original_content = "null"
    config_file.write_text(original_content, encoding="utf-8")

    manager = ModelManager(tmp_path)

    assert manager.load_error is not None
    assert manager.add_model("https://example.com", "key", ["main"]) == []
    assert config_file.read_text(encoding="utf-8") == original_content


def test_create_current_async_llm_client_returns_fresh_clients():
    model = ModelConfig("https://example.com", "key", "model")
    created_clients = [Mock(), Mock()]

    with patch("utils.llm_client.get_current_model_config", return_value=model), \
            patch("utils.llm_client._create_async_chat_client", side_effect=created_clients) as create_client:
        first_client = llm_client_module.create_current_async_llm_client()
        second_client = llm_client_module.create_current_async_llm_client()

    assert first_client is created_clients[0]
    assert second_client is created_clients[1]
    assert first_client is not second_client
    assert create_client.call_count == 2


def test_create_memory_recall_llm_client_uses_configured_model_and_falls_back():
    recall_model = ModelConfig("https://example.com", "key", "recall-model")
    current_model = ModelConfig("https://example.com", "key", "current-model")

    mock_manager = Mock()
    mock_manager.get_memory_recall_model.return_value = recall_model
    with patch("utils.llm_client.get_model_manager", return_value=mock_manager):
        client = create_memory_recall_llm_client()
    assert client.model == "recall-model"

    mock_manager2 = Mock()
    mock_manager2.get_memory_recall_model.return_value = None
    mock_manager2.get_current_model.return_value = current_model
    with patch("utils.llm_client.get_model_manager", return_value=mock_manager2):
        fallback_client = create_memory_recall_llm_client()
    assert fallback_client.model == "current-model"

    mock_manager3 = Mock()
    mock_manager3.get_memory_recall_model.return_value = None
    mock_manager3.get_current_model.return_value = None
    with patch("utils.llm_client.get_model_manager", return_value=mock_manager3):
        assert create_memory_recall_llm_client() is None


@pytest.mark.anyio
async def test_select_relevant_memory_ids_uses_dedicated_recall_client():
    recall_client = FakeRecallClient()

    with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
            patch.object(memory, "create_memory_recall_llm_client", return_value=recall_client):
        selected = await memory.select_relevant_memory_ids("query")

    assert selected == ["mem_a"]
    assert recall_client.generate_calls == 1
    memory._MEMORY_RECALL_WINDOWS = {}


def test_append_long_term_memory_evicts_least_recently_updated(tmp_path, monkeypatch):
    memory_file = tmp_path / "long_term_memory.jsonl"
    records = [
        {
            "id": "mem_created_first",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-03 00:00:00",
            "category": "workflow",
            "insight": "recently updated",
            "evidence": "test",
            "reuse_condition": "test",
            "status": "active",
        },
        {
            "id": "mem_updated_first",
            "created_at": "2026-01-02 00:00:00",
            "updated_at": "2026-01-02 00:00:00",
            "category": "workflow",
            "insight": "least recently updated",
            "evidence": "test",
            "reuse_condition": "test",
            "status": "active",
        },
    ]
    memory_file.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", memory_file)
    monkeypatch.setattr(memory, "get_memory_size", lambda: 2)

    result = memory.append_long_term_memory("workflow", "new", "test", "test")

    saved_records = [
        json.loads(line)
        for line in memory_file.read_text(encoding="utf-8").splitlines()
    ]
    saved_by_id = {record["id"]: record for record in saved_records}
    assert saved_by_id["mem_created_first"]["status"] == "active"
    assert saved_by_id["mem_updated_first"]["status"] == "deleted"
    assert result["deleted_overflow_ids"] == ["mem_updated_first"]


def test_memory_config_reads_latest_disk_values_and_preserves_existing_fields(tmp_path, monkeypatch):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"memory_size": 9}), encoding="utf-8")

    assert memory.get_memory_recall_window_size() == 3
    assert memory.get_context_length() == 200
    assert memory.get_context_token_limit() == 200 * 1024
    assert memory.get_compaction_thresholds() == (70, 90)
    assert memory.get_tool_output_compact_tokens() == 2000
    assert memory.get_partial_compact_percentages() == (30, 50)
    assert memory.set_context_length(300) == 300
    assert memory.set_compaction_thresholds(65, 85) == (65, 85)
    assert memory.set_tool_output_compact_tokens(2400) == 2400
    assert memory.set_partial_compact_percentages(25, 45) == (25, 45)

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["memory_size"] == 9
    assert saved["memory_recall_window_size"] == 3
    assert saved["context_length"] == 300
    assert saved["tool_output_compact_threshold"] == 65
    assert saved["partial_compact_threshold"] == 85
    assert saved["tool_output_compact_tokens"] == 2400
    assert saved["partial_compact_min_percent"] == 25
    assert saved["partial_compact_max_percent"] == 45

    config_file.write_text(json.dumps({
        "memory_size": 11,
        "memory_recall_window_size": 4,
        "context_length": 256,
        "tool_output_compact_threshold": 60,
        "partial_compact_threshold": 80,
        "tool_output_compact_tokens": 3200,
        "partial_compact_min_percent": 20,
        "partial_compact_max_percent": 40,
    }), encoding="utf-8")

    assert memory.get_memory_size() == 11
    assert memory.get_memory_recall_window_size() == 4
    assert memory.get_context_length() == 256
    assert memory.get_context_token_limit() == 256 * 1024
    assert memory.get_compaction_thresholds() == (60, 80)
    assert memory.get_tool_output_compact_tokens() == 3200
    assert memory.get_partial_compact_percentages() == (20, 40)
    assert memory.set_memory_recall_window_size(5) == 5

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["memory_size"] == 11
    assert saved["context_length"] == 256
    assert saved["memory_recall_window_size"] == 5
    assert saved["tool_output_compact_threshold"] == 60
    assert saved["partial_compact_threshold"] == 80
    assert saved["tool_output_compact_tokens"] == 3200
    assert saved["partial_compact_min_percent"] == 20
    assert saved["partial_compact_max_percent"] == 40


def test_memory_config_cache_reuses_same_file_signature_and_invalidates_on_change(tmp_path, monkeypatch):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"context_length": 210}), encoding="utf-8")
    memory._reset_memory_config_cache()

    with patch("builtins.open", wraps=open) as open_mock:
        assert memory.get_context_length() == 210
        assert memory.get_context_length() == 210

    assert open_mock.call_count == 1

    config_file.write_text(
        json.dumps({"context_length": 220, "external": "changed"}),
        encoding="utf-8",
    )
    assert memory.get_context_length() == 220


def test_memory_config_setter_merges_external_latest_values_with_cached_data(tmp_path, monkeypatch):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"context_length": 210, "external": "before"}), encoding="utf-8")
    assert memory.get_context_length() == 210

    config_file.write_text(
        json.dumps({"context_length": 220, "external": "after-change"}),
        encoding="utf-8",
    )
    assert memory.set_memory_size(12) == 12

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["context_length"] == 220
    assert saved["external"] == "after-change"
    assert saved["memory_size"] == 12


@pytest.mark.parametrize("first,second", [(0, 90), (70, 70), (90, 70), (70, 100), (70.0, 90)])
def test_memory_compaction_thresholds_require_ordered_integer_percentages(tmp_path, monkeypatch, first, second):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"memory_size": 9}), encoding="utf-8")

    with pytest.raises(ValueError, match="compaction thresholds"):
        memory.set_compaction_thresholds(first, second)

    assert json.loads(config_file.read_text(encoding="utf-8")) == {"memory_size": 9}


@pytest.mark.parametrize("tokens", [0, -2, 1999, 2000.0, True])
def test_tool_output_compact_tokens_require_positive_even_integer(tmp_path, monkeypatch, tokens):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"memory_size": 9}), encoding="utf-8")

    with pytest.raises(ValueError, match="positive even integer"):
        memory.set_tool_output_compact_tokens(tokens)

    assert json.loads(config_file.read_text(encoding="utf-8")) == {"memory_size": 9}


@pytest.mark.parametrize("minimum,maximum", [(0, 50), (30, 30), (50, 30), (30, 100), (30.0, 50)])
def test_partial_compact_percentages_require_ordered_integer_percentages(
        tmp_path, monkeypatch, minimum, maximum,
):
    config_file = tmp_path / "memory_config.json"
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({"memory_size": 9}), encoding="utf-8")

    with pytest.raises(ValueError, match="partial compaction percentages"):
        memory.set_partial_compact_percentages(minimum, maximum)

    assert json.loads(config_file.read_text(encoding="utf-8")) == {"memory_size": 9}


def test_memory_config_modal_includes_compaction_threshold_fields():
    fields = MemoryConfigModal._FIELDS

    assert fields["context_length"]["input_id"] == "memory-config-context-length"
    assert "memory_size" in fields
    assert fields["tool_output_compact_threshold"]["input_id"] == "memory-config-tool-output-compact-threshold"
    assert fields["partial_compact_threshold"]["input_id"] == "memory-config-partial-compact-threshold"
    assert fields["tool_output_compact_tokens"]["input_id"] == "memory-config-tool-output-compact-tokens"
    assert fields["partial_compact_min_percent"]["input_id"] == "memory-config-partial-compact-min-percent"
    assert fields["partial_compact_max_percent"]["input_id"] == "memory-config-partial-compact-max-percent"
    assert "keep_recent_tool_call" not in fields
    assert fields["memory_recall_window_size"]["input_id"] == "memory-config-memory-recall-window-size"


def test_memory_config_modal_requires_second_compaction_threshold_to_be_greater():
    values = {
        "context_length": 200,
        "memory_size": 30,
        "tool_output_compact_threshold": 90,
        "partial_compact_threshold": 70,
        "tool_output_compact_tokens": 2000,
        "partial_compact_min_percent": 30,
        "partial_compact_max_percent": 50,
        "memory_recall_window_size": 3,
    }
    modal = MemoryConfigModal(values)
    inputs = {
        f"#{meta['input_id']}": SimpleNamespace(value=str(values[field]))
        for field, meta in modal._FIELDS.items()
    }

    with patch.object(modal, "query_one", side_effect=lambda selector, *args: inputs[selector]), \
            patch.object(modal, "_show_error") as show_error:
        assert modal._collect_values() is None

    show_error.assert_called_once_with("压缩阈值必须满足 0 < 第一层阈值 < 第二层阈值 < 100。")


def _memory_config_modal_values(**overrides):
    values = {
        "context_length": 200,
        "memory_size": 30,
        "tool_output_compact_threshold": 70,
        "partial_compact_threshold": 90,
        "tool_output_compact_tokens": 2000,
        "partial_compact_min_percent": 30,
        "partial_compact_max_percent": 50,
        "memory_recall_window_size": 3,
    }
    values.update(overrides)
    return values


def _collect_memory_config_values(values):
    modal = MemoryConfigModal(values)
    inputs = {
        f"#{meta['input_id']}": SimpleNamespace(value=str(values[field]))
        for field, meta in modal._FIELDS.items()
    }
    return modal, inputs


def test_memory_config_modal_requires_even_tool_output_compact_tokens():
    modal, inputs = _collect_memory_config_values(
        _memory_config_modal_values(tool_output_compact_tokens=1999)
    )

    with patch.object(modal, "query_one", side_effect=lambda selector, *args: inputs[selector]), \
            patch.object(modal, "_show_error") as show_error:
        assert modal._collect_values() is None

    show_error.assert_called_once_with("第一层压缩后保留总 tokens（偶数） 必须是正偶数。")


def test_memory_config_modal_requires_ordered_partial_compact_range():
    modal, inputs = _collect_memory_config_values(
        _memory_config_modal_values(
            partial_compact_min_percent=50,
            partial_compact_max_percent=30,
        )
    )

    with patch.object(modal, "query_one", side_effect=lambda selector, *args: inputs[selector]), \
            patch.object(modal, "_show_error") as show_error:
        assert modal._collect_values() is None

    show_error.assert_called_once_with("第二层可压缩落点必须满足 0 < 下限 < 上限 < 100。")


def test_window_attention_is_noop_on_non_windows():
    with patch.object(window_attention.sys, "platform", "linux"):
        window_attention.request_window_attention()


def test_launch_updater_rejects_unsupported_platform(tmp_path):
    with patch.object(updater, "AUTO_UPDATE_SUPPORTED", False):
        with pytest.raises(RuntimeError, match="不支持应用内自动更新"):
            updater.launch_updater(tmp_path / "MakeCode")


def test_signal_legacy_updater_ready(monkeypatch, tmp_path):
    ready_file = tmp_path / "legacy.ready"
    monkeypatch.setenv("MAKECODE_UPDATE_READY_FILE", str(ready_file))

    main_module._signal_legacy_updater_ready()

    assert ready_file.is_file()
    assert "MAKECODE_UPDATE_READY_FILE" not in main_module.os.environ


def test_ts_validator_platform_key_detection(monkeypatch):
    monkeypatch.setattr(ts_validator.sys, "platform", "win32")
    monkeypatch.setattr(ts_validator.platform, "machine", lambda: "AMD64")
    assert ts_validator._current_platform_key() == "windows-x86_64"

    monkeypatch.setattr(ts_validator.sys, "platform", "linux")
    monkeypatch.setattr(ts_validator.platform, "machine", lambda: "aarch64")
    assert ts_validator._current_platform_key() == "linux-aarch64"

    monkeypatch.setattr(ts_validator.sys, "platform", "darwin")
    monkeypatch.setattr(ts_validator.platform, "machine", lambda: "arm64")
    assert ts_validator._current_platform_key() == "macos-arm64"


def test_ts_validator_archive_marker_changes_with_archive_content(tmp_path):
    archive = tmp_path / "parsers-macos-arm64.tar.zst"
    archive.write_bytes(b"old parser archive")
    old_marker = ts_validator._archive_marker(tmp_path, archive)

    archive.write_bytes(b"v1.14.3 parser archive")
    new_marker = ts_validator._archive_marker(tmp_path, archive)

    assert old_marker != new_marker
    assert old_marker.parent == tmp_path
    assert new_marker.name.startswith(f".extracted_{archive.name}.")


def test_ts_validator_configure_cache_dir_uses_pack_config(monkeypatch, tmp_path):
    configure_mock = Mock()
    monkeypatch.setattr(ts_validator, "configure", configure_mock)

    assert ts_validator._configure_cache_dir(tmp_path)
    config = configure_mock.call_args.args[0]
    assert isinstance(config, ts_validator.PackConfig)
    assert config.cache_dir == str(tmp_path)


def test_ts_validator_configure_cache_dir_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(ts_validator, "_TS_VALIDATOR_AVAILABLE", True)
    monkeypatch.setattr(ts_validator, "configure", Mock(side_effect=RuntimeError("cache unavailable")))

    assert not ts_validator._configure_cache_dir(tmp_path)
    assert not ts_validator._TS_VALIDATOR_AVAILABLE


def test_ts_validator_does_not_load_uncached_language(monkeypatch):
    get_parser_mock = Mock()
    monkeypatch.setattr(ts_validator, "_TS_VALIDATOR_AVAILABLE", True)
    monkeypatch.setattr(ts_validator, "_TS_CACHED_LANGUAGES", frozenset({"python"}))
    monkeypatch.setattr(ts_validator, "detect_language_from_path", Mock(return_value="rust"))
    monkeypatch.setattr(ts_validator, "get_parser", get_parser_mock)

    assert ts_validator.validate_code("main.rs", "fn main() {}") == (True, "")
    get_parser_mock.assert_not_called()


def test_tui_modals_use_q_not_escape_for_cancel():
    for modal in [ChoiceModal, MemoryConfigModal, RecallModelPickerModal, AddModelModal, EditModelModal, LayoutModal]:
        keys = {binding.key for binding in modal.BINDINGS}
        assert "q" in keys
        assert "escape" not in keys


# ---------- ChoiceModal 渲染与交互测试 ----------

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, ListView, Select, TextArea


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ChoiceModalHost(App):
    """Test host app for mounting ChoiceModal."""
    def __init__(self, modal: ChoiceModal, on_dismiss=None):
        super().__init__()
        self._modal = modal
        self._on_dismiss = on_dismiss
        self.status_refreshes = 0

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._on_dismiss)

    def refresh_status(self) -> None:
        self.status_refreshes += 1


@pytest.mark.anyio
async def test_memory_config_modal_keeps_all_fields_reachable_on_short_terminal():
    modal = MemoryConfigModal(_memory_config_modal_values())
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        dialog = modal.query_one("#memory-config-dialog", VerticalScroll)

        assert dialog.max_scroll_y > 0
        assert modal.query_one("#memory-config-partial-compact-max-percent", Input)
        assert modal.query_one("#memory-config-apply", Button)
        child_ids = [child.id for child in dialog.children]
        assert child_ids.index("memory-config-choose-recall-model") < child_ids.index(
            "memory-config-tool-output-compact-tokens"
        )
        assert child_ids.index("memory-config-choose-recall-model") < child_ids.index(
            "memory-config-partial-compact-min-percent"
        )
        assert child_ids.index("memory-config-choose-recall-model") < child_ids.index(
            "memory-config-partial-compact-max-percent"
        )


def assert_list_selection(list_view: ListView, index: int) -> None:
    assert list_view.index == index
    assert list_view.has_focus
    assert list_view.children[index].has_class("-highlight")


@pytest.mark.anyio
async def test_choice_modal_options_only_renders_list_no_custom():
    """ChoiceModal 有选项且 allow_custom=False 时，不渲染自定义输入和提示。"""
    modal = ChoiceModal("测试标题", ["选项A", "选项B"], allow_custom=False)
    app = ChoiceModalHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(modal.query_one("#choice-title", Label).render()) == "测试标题"
        assert modal.query("#choice-list").__len__() == 1
        assert modal.query("#custom-input").__len__() == 0
        assert modal.query("#custom-hint").__len__() == 0


@pytest.mark.anyio
async def test_choice_modal_close_button_is_top_right_and_cancels():
    modal = ChoiceModal("测试标题", ["选项A"], allow_custom=False)
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    app = ChoiceModalHost(modal, on_dismiss)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        close_button = modal.query_one("#modal-close", Button)
        title = modal.query_one("#choice-title", Label)
        assert str(close_button.label) == "×"
        assert close_button.region.y == title.region.y
        assert close_button.region.x >= title.region.right

        await pilot.click("#modal-close")
        await pilot.pause()

    assert result == "<cancelled>"


@pytest.mark.anyio
async def test_choice_modal_renders_dynamic_markup_tokens_as_plain_text():
    payload = "&mt=doc&dt=doc','https://ku.baidu-int.com/knowledge/example[/link]"
    modal = ChoiceModal(payload, [payload], allow_custom=False)
    app = ChoiceModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(modal.query_one("#choice-title", Label).render()) == payload
        option_label = modal.query_one("#choice-list").children[0].query_one(Label)
        assert str(option_label.render()) == payload


@pytest.mark.anyio
async def test_choice_modal_with_custom_input_renders_hint_and_input():
    """ChoiceModal 有选项且 allow_custom=True 时，渲染提示标签和输入框。"""
    modal = ChoiceModal("测试标题", ["选项A"], allow_custom=True)
    app = ChoiceModalHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        hint = modal.query_one("#custom-hint", Label)
        assert "Enter 提交" in str(hint.render())
        assert "q 取消" in str(hint.render())

        inp = modal.query_one("#custom-input", Input)
        assert inp.placeholder == "输入自定义选项"


@pytest.mark.anyio
async def test_choice_modal_custom_only_no_options_renders_input():
    """HITL 拒绝原因场景：空选项 + allow_custom=True，只有标题、提示和输入框。"""
    modal = ChoiceModal("请输入拒绝原因", [], allow_custom=True)
    app = ChoiceModalHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(modal.query_one("#choice-title", Label).render()) == "请输入拒绝原因"
        assert modal.query("#choice-list").__len__() == 0
        assert modal.query_one("#custom-hint", Label) is not None
        assert modal.query_one("#custom-input", Input) is not None


@pytest.mark.anyio
async def test_choice_modal_css_contains_list_item_auto_height():
    """ChoiceModal CSS 包含 ListItem height:auto 和 margin-bottom 规则。"""
    css = ChoiceModal.CSS
    assert "#choice-list > ListItem" in css
    assert "height: auto" in css
    assert "margin-bottom: 1" in css


@pytest.mark.anyio
async def test_choice_modal_css_contains_custom_hint_style():
    """ChoiceModal CSS 包含 #custom-hint 的边框和颜色样式。"""
    css = ChoiceModal.CSS
    assert "#custom-hint" in css
    assert "border-top: solid" in css
    assert "color: #aaaaaa" in css


def test_memory_and_mcp_panels_use_default_text_and_card_backgrounds():
    css = ChoiceModal.CSS

    def rule(selector):
        return css.split(f"{selector} {{", 1)[1].split("}", 1)[0]

    for selector in (
        "#memory-title",
        "#memory-summary",
        "#memory-help",
        ".memory-add-label",
        "#memory-add-hint",
        "#mcp-title",
        "#mcp-summary",
        "#mcp-help",
        ".mcp-add-label",
        "#mcp-add-advanced-title",
        "#mcp-add-hint",
    ):
        assert "color:" not in rule(selector)

    for selector in (
        "#memory-list > ListItem",
        "#memory-list > ListItem.-highlight",
        "#mcp-list > ListItem",
        "#mcp-list > ListItem.-highlight",
    ):
        assert "background:" not in rule(selector)


@pytest.mark.anyio
async def test_choice_modal_long_option_text_stored():
    """长选项文本正确存储，不因 height:auto 而丢失。"""
    long_text = "这是一个非常非常长的选项文本，用于测试 ListItem 的 height:auto 是否能正确换行显示"
    modal = ChoiceModal("测试", [long_text], allow_custom=False)
    app = ChoiceModalHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import ListView
        lv = modal.query_one("#choice-list", ListView)
        item_label = lv.children[0].query_one(Label)
        assert str(item_label.render()) == long_text


@pytest.mark.anyio
async def test_choice_modal_long_option_text_wraps():
    """长选项文本在 ListItem 内自动换行，不被截断为单行。"""
    long_text = "ThisIsAVeryLongOptionText" * 5  # 125 chars
    modal = ChoiceModal("测试", [long_text], allow_custom=False)
    app = ChoiceModalHost(modal)
    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        from textual.widgets import ListView
        lv = modal.query_one("#choice-list", ListView)
        item_label = lv.children[0].query_one(Label)
        # Label should be constrained to container width and wrap to multiple rows
        assert item_label.size.height > 1, f"Expected height > 1 for wrapping, got {item_label.size.height}"


@pytest.mark.anyio
async def test_choice_modal_long_title_keeps_options_reachable_by_scrolling():
    title = "这是一个非常长的标题，用于验证标题内容超出弹窗高度时仍然可以滚动查看选项。\n" * 20
    modal = ChoiceModal(title, ["选项A", "选项B"], allow_custom=False)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        dialog = modal.query_one("#choice-dialog", VerticalScroll)
        choice_list = modal.query_one("#choice-list")

        assert dialog.max_scroll_y > 0
        dialog.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert dialog.scroll_y == dialog.max_scroll_y
        viewport_top = dialog.scroll_y
        viewport_bottom = viewport_top + dialog.scrollable_content_region.height
        assert choice_list.virtual_region.y < viewport_bottom
        assert choice_list.virtual_region.bottom > viewport_top


@pytest.mark.anyio
async def test_choice_modal_q_cancels_when_not_in_input():
    """按 q 键在非 Input 焦点时取消弹窗。"""
    modal = ChoiceModal("测试", ["选项A"], allow_custom=False)
    result = None

    def on_dismiss(val):
        nonlocal result
        result = val

    app = ChoiceModalHost(modal, on_dismiss)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert result == "<cancelled>"


@pytest.mark.anyio
async def test_mcp_tools_modal_applies_original_name_switches_immediately():
    manager = Mock()
    manager.apply_tool_switches.return_value = {
        "saved": True,
        "server": "filesystem",
        "disabled_tools": ["read.file", "delete.file"],
        "message": "saved",
    }
    modal = McpToolsModal(
        {
            "server": "filesystem",
            "loaded": True,
            "server_disabled": False,
            "tools": [
                {
                    "name": "filesystem_read_file",
                    "original_name": "read.file",
                    "description": "Read a file",
                    "disabled": False,
                    "enabled": True,
                },
                {
                    "name": "filesystem_delete_file",
                    "original_name": "delete.file",
                    "description": "Delete a file",
                    "disabled": True,
                    "enabled": False,
                },
            ],
        },
        manager,
    )
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    app = ChoiceModalHost(modal, on_dismiss)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        tool_table = modal.query_one("#mcp-tools-table", DataTable)
        assert tool_table.cursor_row == 0
        assert not tool_table.zebra_stripes
        assert not any(segment.style.underline for segment in tool_table.render_line(1))
        separator_line = tool_table.render_line(2)
        assert sum(segment.text.count("─") for segment in separator_line) > 0
        assert [str(column.label) for column in tool_table.ordered_columns] == [
            "草稿状态",
            "工具名称",
            "MCP 原始名称",
            "描述",
        ]
        assert "启用" in str(tool_table.get_row_at(0)[0])
        assert str(tool_table.get_row_at(0)[1]) == "filesystem_read_file"
        assert str(tool_table.get_row_at(0)[2]) == "read.file"
        assert str(tool_table.get_row_at(0)[3]) == "Read a file"
        assert "共 2 个工具 · 草稿启用 1 个" in str(
            modal.query_one("#mcp-tools-summary", Label).render()
        )

        with patch.object(tool_table, "clear", side_effect=AssertionError("toggle must update in place")):
            await pilot.press("space")
            await pilot.pause()
        assert "禁用" in str(tool_table.get_row_at(0)[0])
        assert "草稿启用 0 个" in str(modal.query_one("#mcp-tools-summary", Label).render())
        await pilot.click("#mcp-tools-apply")
        await pilot.pause()

    manager.apply_tool_switches.assert_called_once_with(
        "filesystem", ["read.file", "delete.file"]
    )
    assert result["server"] == "filesystem"
    assert result["result"]["saved"] is True


@pytest.mark.anyio
async def test_mcp_tools_modal_explains_unavailable_service_and_cannot_apply():
    manager = Mock()
    modal = McpToolsModal(
        {
            "server": "offline-api",
            "loaded": False,
            "server_disabled": True,
            "tools": [],
        },
        manager,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        summary = str(modal.query_one("#mcp-tools-summary", Label).render())
        assert "服务当前未连接" in summary
        assert "请先启用并连接服务" in summary
        assert modal.query_one("#mcp-tools-apply", Button).disabled
        assert modal.focused.id == "mcp-tools-close"
        await pilot.press("space")
        await pilot.pause()

    manager.apply_tool_switches.assert_not_called()


@pytest.mark.anyio
async def test_mcp_tools_modal_filters_table_by_draft_status():
    manager = Mock()
    modal = McpToolsModal(
        {
            "server": "filesystem",
            "loaded": True,
            "server_disabled": False,
            "tools": [
                {
                    "name": "filesystem_read_file",
                    "original_name": "read.file",
                    "description": "Read a file",
                    "disabled": False,
                },
                {
                    "name": "filesystem_delete_file",
                    "original_name": "delete.file",
                    "description": "Delete a file",
                    "disabled": True,
                },
            ],
        },
        manager,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        status_filter = modal.query_one("#mcp-tools-status-filter", Select)
        tool_table = modal.query_one("#mcp-tools-table", DataTable)

        status_filter.value = "disabled"
        await pilot.pause()
        assert tool_table.row_count == 1
        assert str(tool_table.get_row_at(0)[2]) == "delete.file"
        assert "当前显示 1 个" in str(modal.query_one("#mcp-tools-summary", Label).render())

        tool_table.focus()
        await pilot.press("space")
        await pilot.pause()
        assert tool_table.row_count == 0
        assert "当前显示 0 个" in str(modal.query_one("#mcp-tools-summary", Label).render())

        status_filter.value = "enabled"
        await pilot.pause()
        assert tool_table.row_count == 2
        assert {str(tool_table.get_row_at(index)[2]) for index in range(2)} == {
            "read.file",
            "delete.file",
        }

    manager.apply_tool_switches.assert_not_called()


@pytest.mark.anyio
async def test_mcp_view_modal_filters_tools_read_only_by_saved_status():
    modal = McpViewModal(
        Text("MCP 状态总览"),
        [
            {
                "provider": "filesystem",
                "name": "filesystem_read_file",
                "description": "Read a file",
                "disabled": False,
            },
            {
                "provider": "filesystem",
                "name": "filesystem_delete_file",
                "description": "Delete a file",
                "disabled": True,
            },
        ],
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        tool_table = modal.query_one("#mcp-view-tools-table", DataTable)
        status_filter = modal.query_one("#mcp-view-status-filter", Select)
        assert len(modal.query("#mcp-view-close")) == 0
        assert len(modal.query("#mcp-view-actions")) == 0
        assert "q" in {binding.key for binding in modal.BINDINGS}
        assert not tool_table.zebra_stripes
        assert not any(segment.style.underline for segment in tool_table.render_line(1))
        separator_line = tool_table.render_line(2)
        assert sum(segment.text.count("─") for segment in separator_line) > 0
        assert tool_table.row_count == 2
        assert {str(tool_table.get_row_at(index)[0]) for index in range(2)} == {
            "● 启用",
            "○ 禁用",
        }

        status_filter.value = "disabled"
        await pilot.pause()
        assert tool_table.row_count == 1
        assert str(tool_table.get_row_at(0)[2]) == "filesystem_delete_file"
        assert "当前显示 1 / 2 个工具" in str(
            modal.query_one("#mcp-view-filter-summary", Label).render()
        )
        assert modal.query("#mcp-view-apply").__len__() == 0


@pytest.mark.anyio
async def test_mcp_tools_modal_mouse_click_selects_before_toggling_table_row():
    manager = Mock()
    modal = McpToolsModal(
        {
            "server": "filesystem",
            "loaded": True,
            "server_disabled": False,
            "tools": [
                {
                    "name": "filesystem_read_file",
                    "original_name": "read.file",
                    "description": "Read a file",
                    "disabled": False,
                },
                {
                    "name": "filesystem_write_file",
                    "original_name": "write.file",
                    "description": "Write a file",
                    "disabled": False,
                },
            ],
        },
        manager,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        tool_table = modal.query_one("#mcp-tools-table", DataTable)
        assert tool_table.cursor_row == 0

        await pilot.click(tool_table, offset=(2, 4))
        await pilot.pause()
        assert tool_table.cursor_row == 1
        assert "启用" in str(tool_table.get_row_at(1)[0])
        assert "草稿启用 2 个" in str(modal.query_one("#mcp-tools-summary", Label).render())

        await pilot.click(tool_table, offset=(2, 4))
        await pilot.pause()
        assert tool_table.cursor_row == 1
        assert "草稿启用 1 个" in str(modal.query_one("#mcp-tools-summary", Label).render())

    manager.apply_tool_switches.assert_not_called()


@pytest.mark.anyio
async def test_mcp_switch_modal_opens_tool_management_and_restores_service_selection():
    manager = Mock()
    manager.list_tool_switches.return_value = {
        "server": "filesystem",
        "loaded": True,
        "server_disabled": False,
        "tools": [{
            "name": "filesystem_read_file",
            "original_name": "read.file",
            "description": "Read a file",
            "disabled": False,
            "enabled": True,
        }],
    }
    manager.apply_tool_switches.return_value = {
        "saved": True,
        "server": "filesystem",
        "disabled_tools": ["read.file"],
        "message": "saved",
    }
    modal = McpSwitchModal(
        [{
            "name": "filesystem",
            "disabled": False,
            "loaded": True,
            "transport": "stdio",
            "target": "npx",
            "tool_count": 1,
        }],
        manager,
    )
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    app = ChoiceModalHost(modal, on_dismiss)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, McpToolsModal)
        manager.list_tool_switches.assert_called_once_with("filesystem")

        await pilot.press("space")
        await pilot.click("#mcp-tools-apply")
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal
        assert_list_selection(modal.query_one("#mcp-list", ListView), 0)
        assert "工具开关已保存" in str(modal.query_one("#mcp-title", Label).render())
        await pilot.press("q")
        await pilot.pause()

    assert result["action"] == "cancel"
    assert result["tool_results"][0]["server"] == "filesystem"


@pytest.mark.anyio
async def test_mcp_switch_modal_separates_services_from_actions_and_shows_details():
    modal = McpSwitchModal(
        [
            {
                "name": "filesystem",
                "disabled": False,
                "loaded": True,
                "transport": "stdio",
                "target": "npx",
                "tool_count": 4,
            },
            {
                "name": "remote-api",
                "disabled": True,
                "loaded": False,
                "transport": "streamable-http",
                "target": "https://example.com/mcp",
                "tool_count": 0,
            },
        ],
        Mock(),
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        service_list = modal.query_one("#mcp-list", ListView)

        assert len(service_list.children) == 2
        assert "共 2 个服务 · 草稿启用 1 个" in str(modal.query_one("#mcp-summary", Label).render())
        assert "确认应用" not in str(service_list.children[-1].query_one(Label).render())
        assert modal.query_one("#mcp-apply", Button).region.height > 0
        assert modal.query_one("#mcp-cancel", Button).region.height > 0
        first_label = str(service_list.children[0].query_one(Label).render())
        assert "filesystem" in first_label
        assert "草稿：启用 · 运行：已加载 · 协议：stdio · 工具：4" in first_label
        assert "目标：npx" in first_label


@pytest.mark.anyio
async def test_mcp_switch_modal_click_selects_before_toggling_service():
    modal = McpSwitchModal(
        [
            {
                "name": "filesystem",
                "disabled": False,
                "loaded": True,
                "transport": "stdio",
                "target": "npx",
                "tool_count": 4,
            },
            {
                "name": "remote-api",
                "disabled": True,
                "loaded": False,
                "transport": "streamable-http",
                "target": "https://example.com/mcp",
                "tool_count": 0,
            },
        ],
        Mock(),
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        service_list = modal.query_one("#mcp-list", ListView)
        assert_list_selection(service_list, 0)

        await pilot.click("#mcp-server-1")
        await pilot.pause()

        assert_list_selection(service_list, 1)
        assert "草稿启用 1 个" in str(modal.query_one("#mcp-summary", Label).render())
        assert "草稿：禁用" in str(service_list.children[1].query_one(Label).render())

        await pilot.click("#mcp-server-1")
        await pilot.pause()

        assert_list_selection(service_list, 1)
        assert "草稿启用 2 个" in str(modal.query_one("#mcp-summary", Label).render())
        assert "草稿：启用" in str(service_list.children[1].query_one(Label).render())


@pytest.mark.anyio
async def test_mcp_switch_modal_updates_summary_and_applies_with_fixed_button():
    modal = McpSwitchModal(
        [{"name": "api", "disabled": True, "loaded": False, "transport": "sse", "target": "https://example.com/sse", "tool_count": 0}],
        Mock(),
    )
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    app = ChoiceModalHost(modal, on_dismiss)
    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "草稿启用 1 个" in str(modal.query_one("#mcp-summary", Label).render())
        assert "草稿：启用" in str(modal.query_one("#mcp-list", ListView).children[0].query_one(Label).render())

        await pilot.press("tab")
        await pilot.pause()
        assert modal.focused.id == "mcp-apply"
        await pilot.press("enter")
        await pilot.pause()

    assert result["action"] == "confirm"
    assert result["disabled_updates"] == {"api": False}


@pytest.mark.anyio
async def test_mcp_switch_modal_preserves_delete_confirmation_and_selection():
    manager = Mock()
    manager.delete_server_config.return_value = {"saved": True}
    modal = McpSwitchModal(
        [
            {"name": "first", "disabled": False, "loaded": True},
            {"name": "second", "disabled": True, "loaded": False},
        ],
        manager,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert "确认删除 MCP 服务配置" in str(modal.query_one("#mcp-title", Label).render())
        manager.delete_server_config.assert_not_called()

        await pilot.press("n")
        await pilot.press("d")
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        service_list = modal.query_one("#mcp-list", ListView)
        manager.delete_server_config.assert_called_once_with("first")
        assert len(service_list.children) == 1
        assert service_list.index == 0
        assert "second" in str(service_list.children[0].query_one(Label).render())
        assert "共 1 个服务" in str(modal.query_one("#mcp-summary", Label).render())


@pytest.mark.anyio
async def test_mcp_switch_modal_selects_next_then_previous_service_after_delete():
    manager = Mock()
    manager.delete_server_config.return_value = {"saved": True}
    modal = McpSwitchModal(
        [
            {"name": "first", "disabled": False, "loaded": True},
            {"name": "second", "disabled": True, "loaded": False},
            {"name": "third", "disabled": True, "loaded": False},
        ],
        manager,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        service_list = modal.query_one("#mcp-list", ListView)
        service_list.index = 1
        await pilot.pause()

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [item["name"] for item in modal._server_switches] == ["first", "third"]
        assert_list_selection(service_list, 1)
        assert "third" in str(service_list.children[1].query_one(Label).render())

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [item["name"] for item in modal._server_switches] == ["first"]
        assert_list_selection(service_list, 0)
        assert "first" in str(service_list.children[0].query_one(Label).render())


@pytest.mark.anyio
async def test_mcp_switch_modal_ignores_stale_row_reload_callbacks():
    modal = McpSwitchModal(
        [
            {"name": "first", "disabled": False, "loaded": False},
            {"name": "second", "disabled": True, "loaded": False},
        ],
        Mock(),
    )
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.pause()
        modal._reload_rows(0)
        modal._reload_rows(1)
        await pilot.pause()
        await pilot.pause()

        service_list = modal.query_one("#mcp-list", ListView)
        assert [item.id for item in service_list.children] == ["mcp-server-0", "mcp-server-1"]
        assert service_list.index == 1


@pytest.mark.anyio
async def test_mcp_switch_modal_scrolls_many_wrapped_service_cards_while_actions_stay_visible():
    servers = [
        {
            "name": f"服务-{index}-" + "很长的名称" * 5,
            "disabled": bool(index % 2),
            "loaded": index % 3 == 0,
            "transport": "streamable-http",
            "target": "https://example.com/" + "very-long-path/" * 5,
            "tool_count": index,
        }
        for index in range(12)
    ]
    modal = McpSwitchModal(servers, Mock())
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        service_list = modal.query_one("#mcp-list", ListView)
        first_label = service_list.children[0].query_one(Label)

        assert service_list.max_scroll_y > 0
        assert first_label.size.height > 3
        assert modal.query_one("#mcp-apply", Button).region.height > 0
        assert modal.query_one("#mcp-cancel", Button).region.height > 0

        await pilot.press(*(["down"] * (len(service_list.children) - 1)))
        await pilot.pause()
        assert service_list.index == len(service_list.children) - 1
        assert service_list.scroll_y > 0


@pytest.mark.anyio
async def test_mcp_switch_modal_adds_disabled_remote_service_from_manual_form():
    manager = Mock()
    manager.add_server_config.return_value = {"saved": True}
    manager.list_server_switches.return_value = [{
        "name": "remote-api",
        "disabled": True,
        "loaded": False,
        "transport": "sse",
        "target": "https://example.com/sse",
        "tool_count": 0,
    }]
    modal = McpSwitchModal([], manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#mcp-add")
        await pilot.pause()
        add_modal = app.screen

        add_modal.query_one("#mcp-add-name", Input).value = "remote-api"
        add_modal.query_one("#mcp-add-transport", Select).value = "sse"
        await pilot.pause()
        assert add_modal.query_one("#mcp-add-remote-core").display
        assert not add_modal.query_one("#mcp-add-stdio-core").display

        add_modal.query_one("#mcp-add-url", Input).value = "https://example.com/sse"
        add_modal.query_one("#mcp-add-headers", TextArea).text = "Authorization=Bearer secret\nX-Region=cn"
        add_modal.query_one("#mcp-add-auth", Input).value = "oauth"
        add_modal.query_one("#mcp-add-timeout", Input).value = "5000"
        add_modal.query_one("#mcp-add-sse-read-timeout", Input).value = "30.5"
        add_modal.action_submit()
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal
        service_list = modal.query_one("#mcp-list", ListView)
        assert len(service_list.children) == 1
        assert "remote-api" in str(service_list.children[0].query_one(Label).render())
        assert "草稿：禁用" in str(service_list.children[0].query_one(Label).render())

    manager.add_server_config.assert_called_once_with("remote-api", {
        "url": "https://example.com/sse",
        "transport": "sse",
        "headers": {"Authorization": "Bearer secret", "X-Region": "cn"},
        "auth": "oauth",
        "timeout": 5000,
        "sse_read_timeout": 30.5,
        "disabled": True,
    })


@pytest.mark.anyio
async def test_mcp_switch_modal_adds_disabled_stdio_service_from_manual_form():
    manager = Mock()
    manager.add_server_config.return_value = {"saved": True}
    manager.list_server_switches.return_value = [{
        "name": "filesystem",
        "disabled": True,
        "loaded": False,
        "transport": "stdio",
        "target": "npx",
        "tool_count": 0,
    }]
    modal = McpSwitchModal([], manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#mcp-add")
        await pilot.pause()
        add_modal = app.screen

        name_input = add_modal.query_one("#mcp-add-name", Input)
        name_input.value = "filesyste"
        name_input.focus()
        await pilot.press("q")
        await pilot.pause()
        assert app.screen is add_modal
        assert name_input.value == "filesysteq"

        name_input.value = "filesystem"
        add_modal.query_one("#mcp-add-command", Input).value = "npx"
        add_modal.query_one("#mcp-add-args", Input).value = '-y "@scope/server" "/repo with spaces"'
        add_modal.query_one("#mcp-add-env", TextArea).text = "TOKEN=secret\nMODE=dev"
        add_modal.query_one("#mcp-add-cwd", Input).value = "/repo with spaces"
        add_modal.query_one("#mcp-add-keep-alive", Select).value = "true"
        add_modal.query_one("#mcp-add-env", TextArea).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is add_modal
        add_modal.query_one("#mcp-add-cwd", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal

    manager.add_server_config.assert_called_once_with("filesystem", {
        "command": "npx",
        "args": ["-y", "@scope/server", "/repo with spaces"],
        "transport": "stdio",
        "env": {"TOKEN": "secret", "MODE": "dev"},
        "cwd": "/repo with spaces",
        "keep_alive": True,
        "disabled": True,
    })


@pytest.mark.anyio
async def test_mcp_manual_add_form_keeps_invalid_remote_config_open():
    manager = Mock()
    modal = McpSwitchModal([], manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#mcp-add")
        await pilot.pause()
        add_modal = app.screen

        add_modal.query_one("#mcp-add-name", Input).value = "remote-api"
        add_modal.query_one("#mcp-add-transport", Select).value = "streamable-http"
        await pilot.pause()
        add_modal.action_submit()
        await pilot.pause()

        assert app.screen is add_modal
        assert "远程服务必须填写 URL" in str(add_modal.query_one("#mcp-add-error", Label).render())
        manager.add_server_config.assert_not_called()

        add_modal.query_one("#mcp-add-url", Input).value = "https://example.com/mcp"
        add_modal.query_one("#mcp-add-headers", TextArea).text = "Authorization"
        add_modal.action_submit()
        await pilot.pause()

        assert app.screen is add_modal
        assert "KEY=VALUE" in str(add_modal.query_one("#mcp-add-error", Label).render())
        manager.add_server_config.assert_not_called()


@pytest.mark.anyio
async def test_choice_modal_deletes_only_after_confirmation():
    deleted = []
    modal = ChoiceModal(
        "测试",
        ["选项A", "选项B"],
        delete_handler=deleted.append,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert deleted == []
        assert "确认删除" in str(modal.query_one("#choice-title", Label).render())

        await pilot.press("n")
        await pilot.press("d")
        await pilot.press("y")
        await pilot.pause()

        assert deleted == ["选项A"]
        assert modal._options == ["选项B"]
        assert "d 删除选中项" in str(modal.query_one("#choice-title", Label).render())


@pytest.mark.anyio
async def test_choice_modal_selects_next_then_previous_option_after_delete():
    deleted = []
    modal = ChoiceModal(
        "测试",
        ["选项A", "选项B", "选项C"],
        delete_handler=deleted.append,
    )
    app = ChoiceModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        choice_list = modal.query_one("#choice-list", ListView)
        choice_list.index = 1
        await pilot.pause()

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert modal._options == ["选项A", "选项C"]
        assert_list_selection(choice_list, 1)
        assert str(choice_list.children[1].query_one(Label).render()) == "选项C"

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert modal._options == ["选项A"]
        assert_list_selection(choice_list, 0)
        assert str(choice_list.children[0].query_one(Label).render()) == "选项A"

    assert deleted == ["选项B", "选项C"]


@pytest.mark.anyio
async def test_choice_modal_v_previews_selected_option_and_returns_to_same_selection():
    previewed = []

    def preview_handler(option):
        previewed.append(option)
        return "预览标题", Text("预览内容")

    modal = ChoiceModal("测试", ["选项A", "选项B"], preview_handler=preview_handler)
    app = ChoiceModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        choice_list = modal.query_one("#choice-list")
        choice_list.index = 1

        await pilot.press("v")
        await pilot.pause()

        assert previewed == ["选项B"]
        assert isinstance(app.screen, InfoPanelModal)
        assert "预览标题" in str(app.screen.query_one("#choice-title", Label).render())

        await pilot.press("q")
        await pilot.pause()

        assert app.screen is modal
        assert choice_list.index == 1


@pytest.mark.anyio
async def test_task_panel_is_read_only():
    manager = Mock()
    manager.get_task_table.return_value = {
        "rows": [
            {
                "id": "7",
                "subject": "Read only",
                "status": "pending",
                "is_runnable": True,
            }
        ]
    }
    modal = TaskPanelModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "只读" in str(modal.query_one("#task-title", Label).render())
        await pilot.press("d", "y")
        await pilot.pause()
        assert modal.query_one("#task-table", DataTable).row_count == 1

    manager.delete_task.assert_not_called()


@pytest.mark.anyio
async def test_tracked_async_openai_reports_actual_retry_number():
    client = llm_client_module._TrackedAsyncOpenAI(
        base_url="https://example.com",
        api_key="key",
    )
    try:
        with patch("system.tui_app.set_client_request_active"), \
                patch("system.tui_app.set_client_request_retry") as set_retry, \
                patch("system.tui_app.post_tui") as post_tui, \
                patch.object(llm_client_module.AsyncOpenAI, "_sleep_for_retry", new=AsyncMock()):
            with llm_client_module._client_request_active():
                await client._sleep_for_retry(
                    retries_taken=0,
                    max_retries=2,
                    options=Mock(),
                    response=None,
                )
                await client._sleep_for_retry(
                    retries_taken=1,
                    max_retries=2,
                    options=Mock(),
                    response=None,
                )

        assert [call.args[1:] for call in set_retry.call_args_list] == [(1, 2), (2, 2)]
        assert [call.args[1] for call in post_tui.call_args_list] == [
            "[#aaaaaa]🌐 LLM 请求失败（连接或超时错误），正在重试 1/2。[/#aaaaaa]",
            "[#aaaaaa]🌐 LLM 请求失败（连接或超时错误），正在重试 2/2。[/#aaaaaa]",
        ]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_tracked_anthropic_retries_transient_gateway_not_found_response():
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                404,
                request=request,
                json={"type": "error", "error": {"type": "not_found_error", "message": "temporary route miss"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = llm_client_module._TrackedAsyncAnthropic(
        base_url="https://gateway.example",
        api_key="key",
        http_client=http_client,
        max_retries=1,
    )
    try:
        with patch("system.tui_app.set_client_request_active"), \
                patch("system.tui_app.set_client_request_retry") as set_retry, \
                patch.object(llm_client_module.AsyncAnthropic, "_sleep_for_retry", new=AsyncMock()):
            with llm_client_module._client_request_active():
                response = await client.messages.create(
                    model="claude-test",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "hello"}],
                )

        assert response.content[0].text == "ok"
        assert attempts == 2
        assert [call.args[1:] for call in set_retry.call_args_list] == [(1, 1)]
    finally:
        await client.close()


def test_model_reasoning_effort_defaults_only_when_missing_or_invalid(tmp_path):
    config_file = tmp_path / "model_config.json"
    config_file.write_text(json.dumps({
        "models": [
            {"base_url": "https://example.com", "api_key": "key", "model_id": "missing"},
            {"base_url": "https://example.com", "api_key": "key", "model_id": "invalid", "reasoning_effort": "ultra"},
            {"base_url": "https://example.com", "api_key": "key", "model_id": "saved", "reasoning_effort": "high"},
        ]
    }), encoding="utf-8")

    manager = ModelManager(tmp_path)
    efforts = {model.model_id: model.reasoning_effort for model in manager.models}

    assert efforts == {"invalid": "medium", "missing": "medium", "saved": "high"}


def test_model_message_format_defaults_only_when_missing_or_invalid(tmp_path):
    config_file = tmp_path / "model_config.json"
    config_file.write_text(json.dumps({
        "models": [
            {"base_url": "https://example.com", "api_key": "key", "model_id": "missing"},
            {"base_url": "https://example.com", "api_key": "key", "model_id": "invalid", "message_format": "claude"},
            {"base_url": "https://example.com", "api_key": "key", "model_id": "saved", "message_format": "anthropic"},
        ]
    }), encoding="utf-8")

    manager = ModelManager(tmp_path)
    formats = {model.model_id: model.message_format for model in manager.models}

    assert formats == {"invalid": "openai_chat", "missing": "openai_chat", "saved": "anthropic"}


def test_message_format_is_identity_while_reasoning_effort_is_runtime_only(tmp_path):
    manager = ModelManager(tmp_path)
    openai_model = manager.add_model(
        "https://example.com", "key", ["same"], message_format="openai_chat"
    )[0]
    anthropic_model = manager.add_model(
        "https://example.com", "key", ["same"], message_format="anthropic"
    )[0]
    high_model = ModelConfig(
        "https://example.com",
        "key",
        "same",
        reasoning_effort="high",
        message_format="openai_chat",
    )

    assert openai_model.key != anthropic_model.key
    assert openai_model.key == high_model.key
    assert openai_model.runtime_key != high_model.runtime_key

    reloaded = ModelManager(tmp_path)
    assert {model.message_format for model in reloaded.models} == set(MESSAGE_FORMATS)


@pytest.mark.parametrize("reasoning_effort", REASONING_EFFORTS)
def test_model_manager_persists_each_reasoning_effort(tmp_path, reasoning_effort):
    manager = ModelManager(tmp_path)
    model = manager.add_model("https://example.com", "key", ["main"])[0]

    assert manager.select_model(model.key, reasoning_effort)
    reloaded = ModelManager(tmp_path)

    assert reloaded.get_current_model().reasoning_effort == reasoning_effort
    saved = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert saved["models"][0]["reasoning_effort"] == reasoning_effort


def test_model_manager_updates_effort_without_selecting_another_model(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["current", "other"])
    current_key = manager.get_current_model().key
    other_key = next(model.key for model in models if model.key != current_key)

    assert manager.set_reasoning_effort(other_key, "max")
    reloaded = ModelManager(tmp_path)

    assert reloaded.get_current_model().key == current_key
    assert next(model for model in reloaded.models if model.key == other_key).reasoning_effort == "max"


def test_running_model_effort_is_process_local_until_explicitly_changed(tmp_path):
    setup_manager = ModelManager(tmp_path)
    model_key = setup_manager.add_model("https://example.com", "key", ["main"])[0].key
    first_process = ModelManager(tmp_path)
    second_process = ModelManager(tmp_path)

    assert second_process.set_reasoning_effort(model_key, "high")
    assert second_process.get_current_model().reasoning_effort == "high"
    assert first_process.get_current_model().reasoning_effort == "medium"

    assert first_process._reload_from_disk()
    assert next(model for model in first_process.models if model.key == model_key).reasoning_effort == "high"
    assert first_process.get_current_model().reasoning_effort == "medium"


def test_model_config_save_is_atomic(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"])

    saved = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))

    assert saved["models"][0]["model_id"] == "main"
    assert list(tmp_path.glob(".model_config.json.*.tmp")) == []


@pytest.mark.anyio
async def test_model_manager_modal_shows_efforts_and_changes_current_model_with_arrow_keys(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"])
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        title = str(modal.query_one("#model-manager-title", Label).render())
        assert "low / medium / high / xhigh / max" in title
        assert "共 1 个模型" in str(modal.query_one("#model-manager-summary", Label).render())

        model_list = modal.query_one("#model-manager-list", ListView)
        model_list.index = 0
        await pilot.press("right")
        await pilot.pause()

        row = model_list.children[0].query_one(Label)
        row_text = str(row.render())
        assert "effort：high" in row_text
        assert "格式：openai_chat" in row_text
        assert "● main" in row_text
        assert "当前运行" not in row_text
        assert "可按 Enter 切换为当前模型" not in row_text
        assert modal.query_one("#model-manager-add", Button).region.height > 0
        assert modal.query_one("#model-manager-select", Button).region.height > 0
        assert app.status_refreshes == 1

    reloaded = ModelManager(tmp_path)
    assert reloaded.get_current_model().reasoning_effort == "high"


@pytest.mark.anyio
async def test_model_manager_modal_changes_other_effort_without_switching_or_refreshing_status(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["current", "other"])
    current_key = manager.get_current_model().key
    other_key = next(model.key for model in manager.models if model.key != current_key)
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        model_list = modal.query_one("#model-manager-list", ListView)
        model_list.index = next(index for index, key in enumerate(modal._model_keys) if key == other_key)
        await pilot.press("right")
        await pilot.pause()

        assert manager.get_current_model().key == current_key
        assert app.status_refreshes == 0
        assert "effort：high" in str(model_list.children[model_list.index].query_one(Label).render())


@pytest.mark.anyio
async def test_model_manager_modal_selects_displayed_model_and_refreshes_runtime(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["current", "other"])
    current_key = manager.get_current_model().key
    other_key = next(model.key for model in manager.models if model.key != current_key)
    results = []
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal, results.append)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        model_list = modal.query_one("#model-manager-list", ListView)
        model_list.index = next(index for index, key in enumerate(modal._model_keys) if key == other_key)
        await pilot.press("enter")
        await pilot.pause()

    assert manager.get_current_model().key == other_key
    assert manager.get_current_model().reasoning_effort == "medium"
    assert results and results[0].startswith("selected:other")
    assert app.status_refreshes == 1


@pytest.mark.anyio
async def test_model_manager_modal_edits_model_config(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"])
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, EditModelModal)
        edit_modal = app.screen
        assert edit_modal.query_one("#edit-model-base-url", Input).value == "https://example.com"
        assert edit_modal.query_one("#edit-model-api-key", Input).value == "key"
        assert edit_modal.query_one("#edit-model-id", Input).value == "main"

        edit_modal.query_one("#edit-model-base-url", Input).value = "https://updated.example.com/v1"
        edit_modal.query_one("#edit-model-api-key", Input).value = "new-key"
        edit_modal.query_one("#edit-model-id", Input).value = "updated-model"
        edit_modal.query_one("#edit-model-alias", Input).value = "新名称"
        edit_modal.query_one("#edit-model-message-format", Select).value = "anthropic"
        edit_modal.action_submit()
        await pilot.pause()
        await pilot.pause()

        assert manager.models[0].model_id == "updated-model"
        assert manager.models[0].base_url == "https://updated.example.com/v1"
        assert manager.models[0].api_key == "new-key"
        assert manager.models[0].message_format == "anthropic"
        assert manager.models[0].alias == "新名称"


@pytest.mark.anyio
async def test_model_manager_modal_displays_model_alias(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"], aliases=["日常模型"])
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        model_list = modal.query_one("#model-manager-list", ListView)
        assert "日常模型 (main)" in str(model_list.children[0].query_one(Label).render())

@pytest.mark.anyio
async def test_model_manager_modal_scrolls_cards_while_actions_stay_visible(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model(
        "https://very-long-model-service.example.com/v1",
        "key",
        [f"model-{index}-" + "very-long-name-" * 4 for index in range(12)],
    )
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        model_list = modal.query_one("#model-manager-list", ListView)
        first_label = model_list.children[0].query_one(Label)

        assert model_list.max_scroll_y > 0
        assert first_label.size.height > 3
        assert modal.query_one("#model-manager-add", Button).region.height > 0
        assert modal.query_one("#model-manager-close", Button).region.height > 0

        await pilot.press(*(["down"] * (len(model_list.children) - 1)))
        await pilot.pause()
        assert model_list.index == len(model_list.children) - 1
        assert model_list.scroll_y > 0


@pytest.mark.anyio
async def test_model_manager_selects_next_then_previous_model_after_delete(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["first", "second", "third"])
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        model_list = modal.query_one("#model-manager-list", ListView)
        model_list.index = 1
        await pilot.pause()

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [model.model_id for model in manager.models] == ["first", "third"]
        assert_list_selection(model_list, 1)
        assert "third" in str(model_list.children[1].query_one(Label).render())

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [model.model_id for model in manager.models] == ["first"]
        assert_list_selection(model_list, 0)
        assert "first" in str(model_list.children[0].query_one(Label).render())


@pytest.mark.anyio
async def test_model_manager_focuses_add_after_deleting_only_model(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["only"])
    modal = ModelManagerModal(manager)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert manager.models == []
        assert modal.query_one("#model-manager-add", Button).has_focus


@pytest.mark.anyio
async def test_model_manager_modal_add_shortcut_works_when_empty(tmp_path):
    modal = ModelManagerModal(ModelManager(tmp_path))
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert modal.query_one("#model-manager-add", Button).has_focus

        await pilot.press("a")
        await pilot.pause()

        assert isinstance(app.screen, AddModelModal)


@pytest.mark.anyio
async def test_add_memory_modal_requires_content_and_reuse_condition():
    results = []
    modal = AddMemoryModal()
    app = ChoiceModalHost(modal, results.append)

    async with app.run_test(size=(90, 32)) as pilot:
        await pilot.pause()
        modal.query_one("#memory-add-category", Input).value = "workflow"
        modal.action_submit()
        await pilot.pause()

        assert "请填写记忆内容" in str(modal.query_one("#memory-add-error", Label).render())
        assert results == []

        modal.query_one("#memory-add-insight", TextArea).text = "始终先运行针对性测试。"
        modal.query_one("#memory-add-reuse-condition", TextArea).text = "当修改测试或验证流程时"
        modal.action_submit()
        await pilot.pause()

    assert results == [{
        "category": "workflow",
        "insight": "始终先运行针对性测试。",
        "evidence": "",
        "reuse_condition": "当修改测试或验证流程时",
    }]


@pytest.mark.anyio
async def test_memory_panel_add_shortcut_works_when_empty():
    provider = Mock()
    provider.list_long_term_memories.return_value = []
    modal = MemoryPanelModal(provider)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert modal.query_one("#memory-add", Button).has_focus

        await pilot.press("a")
        await pilot.pause()

        assert isinstance(app.screen, AddMemoryModal)


@pytest.mark.anyio
async def test_memory_panel_adds_through_append_and_returns_to_new_card():
    records = [{
        "id": "mem_old",
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
        "category": "preference",
        "insight": "旧记忆",
        "evidence": "",
        "reuse_condition": "旧条件",
        "status": "active",
    }]
    provider = Mock()
    provider.list_long_term_memories.side_effect = lambda: list(records)

    def append_memory(**values):
        record = {
            "id": "mem_new",
            "created_at": "2026-08-04 12:00:00",
            "updated_at": "2026-08-04 12:00:00",
            "status": "active",
            **values,
        }
        records.append(record)
        return record

    provider.append_long_term_memory.side_effect = append_memory
    modal = MemoryPanelModal(provider)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        assert "共 1 条 active 记忆" in str(modal.query_one("#memory-summary", Label).render())
        assert modal.query_one("#memory-add", Button).region.height > 0
        assert modal.query_one("#memory-close", Button).region.height > 0

        await pilot.click("#memory-add")
        await pilot.pause()
        add_modal = app.screen
        add_modal.query_one("#memory-add-category", Input).value = "project-convention"
        add_modal.query_one("#memory-add-insight", TextArea).text = "记忆面板支持主动添加。"
        add_modal.query_one("#memory-add-evidence", TextArea).text = "用户明确提出需求。"
        add_modal.query_one("#memory-add-reuse-condition", TextArea).text = "当修改记忆面板时"
        add_modal.action_submit()
        await pilot.pause()
        await pilot.pause()

        provider.append_long_term_memory.assert_called_once_with(
            category="project-convention",
            insight="记忆面板支持主动添加。",
            evidence="用户明确提出需求。",
            reuse_condition="当修改记忆面板时",
        )
        memory_list = modal.query_one("#memory-list", ListView)
        assert len(memory_list.children) == 2
        assert memory_list.index == 0
        assert "mem_new" in str(memory_list.children[0].query_one(Label).render())
        assert modal._expanded_id == "mem_new"
        assert "共 2 条 active 记忆" in str(modal.query_one("#memory-summary", Label).render())
        assert app.status_refreshes == 1


@pytest.mark.anyio
async def test_memory_panel_requires_confirmation_before_delete():
    records = [{
        "id": "mem_delete",
        "created_at": "2026-08-04 12:00:00",
        "updated_at": "2026-08-04 12:00:00",
        "category": "workflow",
        "insight": "待删除记忆",
        "evidence": "",
        "reuse_condition": "测试删除时",
        "status": "active",
    }]
    provider = Mock()
    provider.list_long_term_memories.side_effect = lambda: list(records)

    def delete_memory(memory_id):
        records[:] = [record for record in records if record["id"] != memory_id]
        return True

    provider.delete_long_term_memory.side_effect = delete_memory
    modal = MemoryPanelModal(provider)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert "确认删除长期记忆" in str(modal.query_one("#memory-title", Label).render())
        provider.delete_long_term_memory.assert_not_called()

        await pilot.press("n")
        await pilot.press("d")
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        provider.delete_long_term_memory.assert_called_once_with("mem_delete")
        assert "共 0 条 active 记忆" in str(modal.query_one("#memory-summary", Label).render())
        assert modal._deleted_ids == ["mem_delete"]
        assert app.status_refreshes == 1


@pytest.mark.anyio
async def test_memory_panel_selects_next_then_previous_memory_after_delete():
    records = [
        {
            "id": f"mem_{index}",
            "created_at": f"2026-08-04 12:0{index}:00",
            "updated_at": f"2026-08-04 12:0{index}:00",
            "category": "workflow",
            "insight": f"记忆 {index}",
            "evidence": "",
            "reuse_condition": "测试删除后选择",
            "status": "active",
        }
        for index in range(3)
    ]
    provider = Mock()
    provider.list_long_term_memories.side_effect = lambda: list(records)

    def delete_memory(memory_id):
        records[:] = [record for record in records if record["id"] != memory_id]
        return True

    provider.delete_long_term_memory.side_effect = delete_memory
    modal = MemoryPanelModal(provider)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        memory_list = modal.query_one("#memory-list", ListView)
        memory_list.index = 1
        await pilot.pause()

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [item["id"] for item in modal._memories] == ["mem_2", "mem_0"]
        assert_list_selection(memory_list, 1)
        assert "mem_0" in str(memory_list.children[1].query_one(Label).render())

        await pilot.press("d", "y")
        await pilot.pause()
        await pilot.pause()

        assert [item["id"] for item in modal._memories] == ["mem_2"]
        assert_list_selection(memory_list, 0)
        assert "mem_2" in str(memory_list.children[0].query_one(Label).render())


@pytest.mark.anyio
async def test_memory_panel_scrolls_cards_while_actions_stay_visible():
    records = [
        {
            "id": f"mem_{index}",
            "created_at": f"2026-08-04 12:{index:02d}:00",
            "updated_at": f"2026-08-04 12:{index:02d}:00",
            "category": "workflow",
            "insight": "很长的记忆内容" * 30,
            "evidence": "",
            "reuse_condition": "当相关任务出现时",
            "status": "active",
        }
        for index in range(12)
    ]
    provider = Mock()
    provider.list_long_term_memories.return_value = records
    modal = MemoryPanelModal(provider)
    app = ChoiceModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        memory_list = modal.query_one("#memory-list", ListView)
        first_label = memory_list.children[0].query_one(Label)

        assert memory_list.max_scroll_y > 0
        assert first_label.size.height > 3
        assert modal.query_one("#memory-add", Button).region.height > 0
        assert modal.query_one("#memory-close", Button).region.height > 0

        await pilot.press(*(["down"] * (len(memory_list.children) - 1)))
        await pilot.pause()
        assert memory_list.index == len(memory_list.children) - 1
        assert memory_list.scroll_y > 0


@pytest.mark.anyio
async def test_add_model_modal_returns_optional_alias_and_explicit_message_format():
    results = []
    modal = AddModelModal()
    app = ChoiceModalHost(modal, results.append)

    async with app.run_test() as pilot:
        modal.query_one("#model-base-url", Input).value = "https://api.anthropic.com"
        modal.query_one("#model-api-key", Input).value = "key"
        modal.query_one("#model-ids", Input).value = "claude-test"
        modal.query_one("#model-alias", Input).value = "  日常模型  "
        modal.query_one("#model-message-format", Select).value = "anthropic"
        modal.action_submit()
        await pilot.pause()

    assert results == [{
        "base_url": "https://api.anthropic.com",
        "api_key": "key",
        "model_input": "claude-test",
        "alias_input": "日常模型",
        "message_format": "anthropic",
    }]


def test_handle_models_does_not_refresh_status_after_modal_closes(tmp_path):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"])
    handler = object.__new__(CommandHandler)
    handler.console = Mock()

    for result in ("exit", "selected:main"):
        with patch("system.commands.get_model_manager", return_value=manager), \
                patch("system.commands.manage_models_tui", return_value=result), \
                patch("system.commands.refresh_status") as refresh:
            assert handler.handle_models()
            refresh.assert_not_called()



def test_llm_result_preserves_serializable_message_superset():
    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "Read", "arguments": '{"path":"README.md"}'},
    }]
    native_blocks = [{"type": "thinking", "thinking": "reason", "signature": "sig"}]
    result = build_llm_result(
        text="answer",
        reasoning="reason",
        tool_calls=tool_calls,
        content_blocks=[{"type": "text", "text": "answer"}],
        source_format="anthropic",
        source_model="claude-test",
        native_blocks=native_blocks,
        stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 20},
    )

    tool_calls[0]["id"] = "changed"
    native_blocks[0]["signature"] = "changed"

    assert result.text == "answer"
    assert result.reasoning == "reason"
    assert result.assistant_message["tool_calls"][0]["id"] == "call_1"
    assert result.assistant_message["message_metadata"] == {
        "source_format": "anthropic",
        "source_model": "claude-test",
        "native_blocks": [{"type": "thinking", "thinking": "reason", "signature": "sig"}],
    }
    assert result.assistant_message["stop_reason"] == "tool_use"
    assert result.assistant_message["usage"] == {"input_tokens": 10, "output_tokens": 20}
    json.dumps(result.assistant_message)


def test_strip_native_message_payloads_preserves_persisted_source_messages():
    messages = [{
        "role": "assistant",
        "content": "answer",
        "content_blocks": [
            {"type": "text", "text": "answer"},
            {"type": "native", "native_type": "compaction", "block": {"secret": "state"}},
        ],
        "tool_calls": [{
            "id": "call_1",
            "name": "Read",
            "arguments": "{}",
            "raw": {"provider": "payload"},
        }],
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-test",
            "native_blocks": [{"type": "thinking", "signature": "secret-signature"}],
        },
    }]

    sanitized = strip_native_message_payloads(messages)

    assert "native_blocks" not in sanitized[0]["message_metadata"]
    assert sanitized[0]["content_blocks"] == [{"type": "text", "text": "answer"}]
    assert "raw" not in sanitized[0]["tool_calls"][0]
    assert messages[0]["message_metadata"]["native_blocks"][0]["signature"] == "secret-signature"
    assert messages[0]["tool_calls"][0]["raw"] == {"provider": "payload"}


def test_conversation_round_trip_preserves_unified_message_superset(tmp_path):
    message = build_llm_result(
        text="answer",
        reasoning="summary",
        source_format="anthropic",
        source_model="claude-test",
        native_blocks=[{"type": "thinking", "thinking": "summary", "signature": "sig"}],
        stop_reason="end_turn",
        usage={"input_tokens": 1, "output_tokens": 2},
    ).assistant_message
    store = ConversationStore(tmp_path / "conversations")

    conversation = store.save_messages([message])

    assert store.load(conversation).messages == [message]


def test_openai_shaped_messages_can_be_rebuilt_for_anthropic_from_conversation(tmp_path):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"a.py"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "Read", "content": "file"},
    ]
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages(messages)

    system, rebuilt = llm_client_module.build_anthropic_request_messages(
        store.load(conversation).messages,
    )

    assert system == "system"
    assert rebuilt[1]["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "Read",
        "input": {"path": "a.py"},
    }
    assert rebuilt[2]["content"] == [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "file",
    }]


def test_tool_output_compaction_is_transactional_idempotent_and_protects_latest_groups():
    old_output = "甲" * 1200 + "乙" * 1200
    latest_output = "中" * 2400
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_old",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"old"}'},
            }],
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "tool_use", "id": "call_old"}],
            },
        },
        {"role": "tool", "tool_call_id": "call_old", "name": "Read", "content": old_output},
        {"role": "assistant", "content": "old done"},
        {"role": "user", "content": "latest request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_latest",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"latest"}'},
            }],
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "tool_use", "id": "call_latest"}],
            },
        },
        {"role": "tool", "tool_call_id": "call_latest", "name": "Read", "content": latest_output},
        {"role": "assistant", "content": "latest done"},
        {"role": "user", "content": "current orphan request"},
    ]

    assert memory.compact_tool_outputs(messages) is True
    compacted = messages[3]["content"]
    expected_marker = memory.TOOL_OUTPUT_COMPACT_MARKER.format(omitted_tokens=400)
    assert compacted == "甲" * 1000 + expected_marker + "乙" * 1000
    assert "native_blocks" not in messages[2]["message_metadata"]
    assert messages[7]["content"] == latest_output
    assert messages[6]["message_metadata"]["native_blocks"] == [{"type": "tool_use", "id": "call_latest"}]
    first_result = json.loads(json.dumps(messages, ensure_ascii=False))

    assert memory.compact_tool_outputs(messages) is False
    assert messages == first_result


def test_tool_output_compaction_includes_pretruncation_marker_in_payload():
    marker = "\n\n[...此处省略 6000 tokens...]\n\n"
    pretruncated = "甲" * 4000 + marker + "乙" * 4000

    compacted = memory._compact_tool_output_text(pretruncated)
    expected_marker = memory.TOOL_OUTPUT_COMPACT_MARKER.format(
        omitted_tokens=memory.estimate_text_tokens(pretruncated) - 2000,
    )

    assert compacted == "甲" * 1000 + expected_marker + "乙" * 1000
    assert "[...此处省略" not in compacted


def test_tool_output_compaction_uses_exact_token_boundaries():
    exact = "甲" * 2000
    over = "甲" * 1001 + "乙" * 1000
    long_but_token_light = "A" * 3000

    assert memory.TOOL_OUTPUT_COMPACT_MARKER == "\n\n...[该工具执行结果已被压缩 {omitted_tokens} tokens]...\n\n"
    assert memory._compact_tool_output_text(exact) == exact
    assert memory._compact_tool_output_text(long_but_token_light) == long_but_token_light
    assert memory._compact_tool_output_text(over) == (
        "甲" * 1000 + memory.TOOL_OUTPUT_COMPACT_MARKER.format(omitted_tokens=1) + "乙" * 1000
    )


def test_tool_output_compaction_uses_configured_even_retained_tokens():
    output = "甲" * 2401

    with patch.object(memory, "get_tool_output_compact_tokens", return_value=2400):
        compacted = memory._compact_tool_output_text(output)

    assert compacted == (
        "甲" * 1200
        + memory.TOOL_OUTPUT_COMPACT_MARKER.format(omitted_tokens=1)
        + "甲" * 1200
    )


def test_tool_output_compaction_handles_english_and_unicode_token_boundaries():
    for output in ("word " * 2000, "😀" * 2001, "中😀English " * 1000):
        compacted = memory._compact_tool_output_text(output)
        marker_match = memory._TOOL_OUTPUT_COMPACT_MARKER_PATTERN.search(compacted)
        assert marker_match is not None
        head = compacted[:marker_match.start()]
        tail = compacted[marker_match.end():]
        expected_tokens = memory._ENCODER.encode(output, disallowed_special=())
        expected_head = memory._ENCODER.decode_bytes(expected_tokens[:1000]).decode("utf-8", errors="ignore")
        expected_tail = memory._ENCODER.decode_bytes(expected_tokens[-1000:]).decode("utf-8", errors="ignore")

        assert head == expected_head
        assert tail == expected_tail
        assert memory.estimate_text_tokens(head) <= 1000
        assert memory.estimate_text_tokens(tail) <= 1000
        assert "�" not in compacted


def test_tool_output_compaction_fallback_is_conservative_without_tiktoken(monkeypatch):
    monkeypatch.setattr(memory, "_ENCODER", None)
    output = "甲" * 2001

    compacted = memory._compact_tool_output_text(output)

    expected_marker = memory.TOOL_OUTPUT_COMPACT_MARKER.format(omitted_tokens=1)
    assert compacted == "甲" * 1000 + expected_marker + "甲" * 1000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {
            "type": "function_call",
            "call_id": "call_old",
            "name": "Read",
            "arguments": "{}",
            "message_metadata": {"native_blocks": [{"type": "tool_use"}]},
        },
        {
            "type": "function_call_output",
            "call_id": "call_old",
            "content": "甲" * 2001,
        },
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
    ]

    assert memory.compact_tool_outputs(messages) is True
    assert messages[3]["content"] == (
        "甲" * 1000
        + memory.TOOL_OUTPUT_COMPACT_MARKER.format(omitted_tokens=1)
        + "甲" * 1000
    )
    assert "native_blocks" not in messages[2]["message_metadata"]


def test_tool_output_compaction_does_not_commit_partial_changes_on_error():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "tool", "tool_call_id": "old", "content": "x" * 2001},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
    ]
    original = json.loads(json.dumps(messages))

    with patch.object(memory, "_compact_tool_output_text", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            memory.compact_tool_outputs(messages)

    assert messages == original


def test_conversation_groups_keep_leading_users_together_and_identify_orphan_user():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
        {"role": "assistant", "content": "a5"},
        {"role": "assistant", "content": "a6"},
        {"role": "user", "content": "orphan"},
    ]

    assert memory._conversation_groups(messages) == [
        (1, 3, True),
        (3, 6, True),
        (6, 11, True),
        (11, 12, False),
    ]


@pytest.mark.parametrize("selected_tokens,expected", [
    (29, None),
    (30, (1, 3)),
    (50, (1, 3)),
    (51, None),
])
def test_partial_compaction_range_must_be_between_thirty_and_fifty_percent(selected_tokens, expected):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
        {"role": "user", "content": "orphan"},
    ]

    with patch.object(memory, "get_partial_compact_percentages", return_value=(30, 50)), \
            patch.object(memory, "estimate_tokens", return_value=selected_tokens):
        assert memory._select_partial_compaction_range(messages, 100, 100) == expected


def test_partial_compaction_range_over_context_limit_uses_current_context_minimum_without_maximum():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
        {"role": "user", "content": "orphan"},
    ]

    with patch.object(memory, "get_partial_compact_percentages", return_value=(30, 50)), \
            patch.object(memory, "estimate_tokens", return_value=59):
        assert memory._select_partial_compaction_range(messages, 100, 200) is None

    with patch.object(memory, "get_partial_compact_percentages", return_value=(30, 50)), \
            patch.object(memory, "estimate_tokens", return_value=60):
        assert memory._select_partial_compaction_range(messages, 100, 200) == (1, 3)

    with patch.object(memory, "get_partial_compact_percentages", return_value=(30, 50)), \
            patch.object(memory, "estimate_tokens", return_value=61):
        assert memory._select_partial_compaction_range(messages, 100, 101) == (1, 3)


def test_partial_compaction_range_uses_configured_percentages():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
    ]

    with patch.object(memory, "get_partial_compact_percentages", return_value=(20, 40)), \
            patch.object(memory, "estimate_tokens", return_value=20):
        assert memory._select_partial_compaction_range(messages, 100, 100) == (1, 3)

    with patch.object(memory, "get_partial_compact_percentages", return_value=(20, 40)), \
            patch.object(memory, "estimate_tokens", return_value=41):
        assert memory._select_partial_compaction_range(messages, 100, 100) is None


def test_partial_compaction_range_accumulates_oldest_complete_groups_only():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "old two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
        {"role": "user", "content": "orphan"},
    ]

    def token_count(selected_messages, tools_definition=None):
        return 20 if len(selected_messages) == 2 else 35

    with patch.object(memory, "get_partial_compact_percentages", return_value=(30, 50)), \
            patch.object(memory, "estimate_tokens", side_effect=token_count):
        assert memory._select_partial_compaction_range(messages, 100, 100) == (1, 5)


@pytest.mark.anyio
async def test_partial_compact_replaces_only_selected_groups_after_success():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "old two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
        {"role": "user", "content": "orphan"},
    ]
    selected = messages[1:5]

    with patch.object(memory, "_select_partial_compaction_range", return_value=(1, 5)), \
            patch.object(memory, "_summarize_messages", new_callable=AsyncMock, return_value="summary") as summarize:
        assert await memory.partial_compact(messages, 100, 100, "reason") is True

    assert summarize.await_args.args[0] == selected
    assert summarize.await_args.kwargs["require_memory_success"] is True
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "[Previous conversation compressed. Reason: reason] \n\nsummary"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Ready to proceed."},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
        {"role": "user", "content": "orphan"},
    ]


@pytest.mark.anyio
async def test_partial_compact_preserves_history_when_summary_or_memory_fails():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest answer"},
    ]
    original = json.loads(json.dumps(messages))

    with patch.object(memory, "_select_partial_compaction_range", return_value=(1, 3)), \
            patch.object(memory, "_summarize_messages", new_callable=AsyncMock, side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError, match="failed"):
            await memory.partial_compact(messages, 100, 100, "reason")

    assert messages == original


def test_estimate_tokens_ignores_private_native_payloads():
    messages = [{
        "role": "assistant",
        "content": "answer",
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-test",
            "native_blocks": [{"type": "thinking", "signature": "x" * 100_000}],
        },
    }]

    assert memory.estimate_tokens(messages) == memory.estimate_tokens(
        strip_native_message_payloads(messages)
    )


def test_estimate_token_breakdown_uses_explicit_role_tags():
    messages = [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "user text"},
        {
            "role": "assistant",
            "reasoning_content": "reasoning text",
            "content": "assistant text",
            "tool_calls": [{
                "id": "call_1",
                "name": "Read",
                "arguments": {"path": "README.md"},
            }],
        },
        {
            "role": "tool",
            "name": "Read",
            "tool_call_id": "call_1",
            "content": "tool output",
        },
    ]
    tools = [{"name": "Read", "description": "read a file"}]

    sections = memory._build_token_estimation_sections(messages, tools)
    breakdown = memory.estimate_token_breakdown(messages, tools)

    assert "<system>system text</system>" in sections["system"]
    assert "<system>name: Read" in sections["system"]
    assert sections["user"] == "<user>user text</user>"
    assert sections["reasoning"] == "<reasoning>reasoning text</reasoning>"
    assert "<assistant>assistant text</assistant>" in sections["assistant"]
    assert "<tool>name: Read" in sections["tool"]
    assert "arguments: path: README.md" in sections["tool"]
    assert "tool output" in sections["tool"]
    assert breakdown == {
        key: memory.estimate_text_tokens(value)
        for key, value in sections.items()
    }
    assert memory.estimate_tokens(messages, tools) == sum(breakdown.values())


@pytest.mark.anyio
async def test_token_usage_bar_click_opens_breakdown_modal():
    from system.tui_app import MakeCodeTuiApp
    from system.tui_modals import TokenUsageModal

    app = MakeCodeTuiApp(
        runtime_info_provider=lambda: "📈 Context: 10/100 Tokens (10.0%)",
        token_usage_provider=lambda: ({
            "system": 1,
            "user": 2,
            "reasoning": 3,
            "assistant": 4,
            "tool": 5,
        }, 100),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        runtime_info = app.query_one("#runtime-info-bar")
        token_usage = app.query_one("#token-usage-bar")
        assert runtime_info.tooltip is None
        assert isinstance(token_usage.tooltip, Panel)
        assert token_usage.tooltip.title == "📈 Context Tokens"
        await pilot.click(runtime_info)
        await pilot.pause()
        assert not isinstance(app.screen, InfoPanelModal)
        await pilot.click(token_usage)
        await pilot.pause()
        assert isinstance(app.screen, TokenUsageModal)
        dialog = app.screen.query_one("#token-usage-dialog")
        assert dialog.region.width < app.size.width
        assert dialog.region.height < app.size.height
        assert app.screen.query_one("#modal-close")
        assert app.screen.query("#token-usage-close").__len__() == 0
        assert app.screen.query_one("#token-usage-title").render().plain == "📈 上下文 Token 使用"
        assert app.screen.query_one("#token-usage-table")
        assert app.screen.query_one("#token-usage-table").query_one(".token-usage-header", expect_type=Label).render().plain == "类型"
        assert "system（含工具定义）" in "\n".join(
            str(label.render())
            for label in app.screen.query(".token-usage-label")
        )
        assert "tool" in "\n".join(
            str(label.render())
            for label in app.screen.query(".token-usage-label")
        )


@pytest.mark.anyio
async def test_manual_memory_update_uses_clean_role_based_context():
    history = [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read the file."},
                {"type": "image", "attachment_id": "image-1"},
            ],
        },
        {
            "role": "assistant",
            "content": "I will read it.",
            "reasoning_content": "private reasoning",
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
            },
            "tool_calls": [{
                "id": "call_1",
                "name": "FileRead",
                "arguments": '{"path":"README.md"}',
                "raw": {"secret": "private"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "FileRead",
            "content": "file contents",
        },
    ]

    with patch.object(memory, "memory_agent_loop", new_callable=AsyncMock, return_value=[]) as loop:
        await memory.manual_memory_update("remember convention", history)

    conversation_text = loop.await_args.kwargs["conversation_text"]
    assert conversation_text == (
        "### user:\n"
        "Read the file.\n\n"
        "### assistant:\n"
        "I will read it.\n\n"
        "### tools:\n"
        "name: FileRead\n"
        'arguments: {"path": "README.md"}\n'
        "output:\n"
        "file contents"
    )
    assert "system prompt" not in conversation_text
    assert "private reasoning" not in conversation_text
    assert "private-signature" not in conversation_text
    assert "native_blocks" not in conversation_text
    assert "attachment_id" not in conversation_text
    assert history[2]["message_metadata"]["native_blocks"][0]["signature"] == "private-signature"
    assert history[2]["tool_calls"][0]["raw"] == {"secret": "private"}


def test_clean_conversation_context_formats_messages_and_tools_without_reasoning():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Read the file."},
        {
            "role": "assistant",
            "reasoning_content": "private reasoning",
            "content": "I will read it.",
            "tool_calls": [{
                "id": "call_1",
                "name": "FileRead",
                "arguments": '{"path":"README.md"}',
                "raw": {"secret": "private"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "FileRead",
            "content": "file contents",
        },
        {"role": "assistant", "content": "The file says hello."},
        {"role": "user", "content": "Thanks."},
    ]

    context = memory.format_clean_conversation_context(messages)

    assert context == (
        "### user:\n"
        "Read the file.\n\n"
        "### assistant:\n"
        "I will read it.\n\n"
        "### tools:\n"
        "name: FileRead\n"
        "arguments: {\"path\": \"README.md\"}\n"
        "output:\n"
        "file contents\n\n"
        "### assistant:\n"
        "The file says hello.\n\n"
        "### user:\n"
        "Thanks."
    )
    assert "private reasoning" not in context
    assert "private" not in context


def test_clean_conversation_context_keeps_invalid_tool_arguments_as_raw_text():
    context = memory.format_clean_conversation_context([
        {"role": "user", "content": "Run it."},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "name": "RunTerminalCommand",
                "arguments": '{"command":',
            }],
        },
    ])

    assert "arguments: {\"command\":" in context


@pytest.mark.anyio
async def test_summary_request_uses_plain_text_compaction_context():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer", "reasoning_content": "reasoning"},
    ]
    fake_client = Mock()
    fake_client.get_summary_stream_events.return_value = object()

    with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
            patch.object(memory, "close_async_llm_client", new_callable=AsyncMock), \
            patch.object(memory, "_compact_console"), \
            patch.object(
                memory.StreamRenderer,
                "render_text_stream_async",
                new_callable=AsyncMock,
                return_value=("summary", [], None),
            ), \
            patch.object(memory, "memory_agent_loop", new_callable=AsyncMock, return_value=[]), \
            patch.object(memory, "print_formatted_text"), \
            patch.object(memory, "post_tui"):
        await memory.auto_compact(messages)

    summary_request = fake_client.get_summary_stream_events.call_args.args
    assert summary_request[0] == "### user:\nquestion\n\n### assistant:\nanswer"
    assert "reasoning" not in summary_request[0]


@pytest.mark.anyio
async def test_auto_compact_summary_ignores_private_native_payloads():
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "answer",
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
            },
        },
    ]
    fake_client = Mock()
    fake_client.get_summary_stream_events.return_value = object()

    with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
            patch.object(memory, "close_async_llm_client", new_callable=AsyncMock) as close_client, \
            patch.object(memory, "_compact_console"), \
            patch.object(
                memory.StreamRenderer,
                "render_text_stream_async",
                new_callable=AsyncMock,
                return_value=("summary", [], None),
            ), \
            patch.object(memory, "memory_agent_loop", new_callable=AsyncMock, return_value=[]) as memory_loop, \
            patch.object(memory, "print_formatted_text"), \
            patch.object(memory, "post_tui"):
        await memory.auto_compact(messages)

    conversation_text = memory_loop.await_args.kwargs["conversation_text"]
    assert "private-signature" not in conversation_text
    assert "native_blocks" not in conversation_text
    close_client.assert_awaited_once_with(fake_client)


@pytest.mark.anyio
async def test_auto_compact_clears_old_tool_history_before_memory_agent_and_preserves_new_history():
    messages = [{"role": "system", "content": "system"}]
    execution_id = memory.TOOL_EXECUTION_HISTORY.start("FileRead", {"path": "old.py"})
    memory.TOOL_EXECUTION_HISTORY.finish(execution_id, "old content")
    fake_client = Mock()
    fake_client.get_summary_stream_events.return_value = object()

    async def run_memory_agent(*args, **kwargs):
        assert memory.TOOL_EXECUTION_HISTORY.snapshot() == []
        new_execution_id = memory.TOOL_EXECUTION_HISTORY.start(
            "AppendLongTermMemory",
            {"insight": "new memory"},
            source="memory",
            actor=memory.MEMORY_AGENT_IDENTITY,
        )
        memory.TOOL_EXECUTION_HISTORY.finish(new_execution_id, "saved")
        return []

    memory_loop = AsyncMock(side_effect=run_memory_agent)

    try:
        with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
                patch.object(memory, "close_async_llm_client", new_callable=AsyncMock), \
                patch.object(memory, "_compact_console"), \
                patch.object(
                    memory.StreamRenderer,
                    "render_text_stream_async",
                    new_callable=AsyncMock,
                    return_value=("summary", [], None),
                ), \
                patch.object(memory, "memory_agent_loop", new=memory_loop), \
                patch.object(memory, "print_formatted_text"), \
                patch.object(memory, "post_tui"):
            await memory.auto_compact(messages)

        memory_loop.assert_awaited_once()
        records = memory.TOOL_EXECUTION_HISTORY.snapshot()
        assert len(records) == 1
        assert records[0].tool_name == "AppendLongTermMemory"
        assert records[0].source == "memory"
    finally:
        memory.TOOL_EXECUTION_HISTORY.clear()


@pytest.mark.anyio
async def test_auto_compact_preserves_tool_execution_history_when_compaction_fails():
    messages = [{"role": "system", "content": "system"}]
    execution_id = memory.TOOL_EXECUTION_HISTORY.start("FileRead", {"path": "old.py"})
    memory.TOOL_EXECUTION_HISTORY.finish(execution_id, "old content")

    try:
        with patch.object(memory, "_compact_console"), \
                patch.object(memory, "create_current_async_llm_client", return_value=None):
            with pytest.raises(RuntimeError, match="No model configured"):
                await memory.auto_compact(messages)

        assert len(memory.TOOL_EXECUTION_HISTORY.snapshot()) == 1
    finally:
        memory.TOOL_EXECUTION_HISTORY.clear()


@pytest.mark.anyio
async def test_async_chat_client_uses_configured_reasoning_effort():
    raw_client = Mock()
    raw_stream = _ClosableAsyncStream([
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ])
    raw_client.chat.completions.create = AsyncMock(return_value=raw_stream)
    client = AsyncChatAPIClient(raw_client, "test-model", "max")

    with patch("system.tui_app.set_client_request_active") as set_request_active:
        [event async for event in client.generate_stream([{"role": "user", "content": "hello"}])]

    request_kwargs = raw_client.chat.completions.create.call_args.kwargs
    assert request_kwargs["reasoning_effort"] == "max"
    assert request_kwargs["stream_options"] == {"include_usage": True}
    assert request_kwargs["prompt_cache_retention"] == "24h"
    assert request_kwargs["prompt_cache_key"].startswith("mc-pc2-")
    assert [call.args[0] for call in set_request_active.call_args_list] == [True, False]


def test_openai_message_sanitizer_preserves_reasoning_content_and_strips_private_fields():
    messages = [{
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "private reasoning",
        "tool_calls": [{
            "id": "call_1",
            "name": "Read",
            "arguments": '{"path":"README.md"}',
        }],
        "content_blocks": [{"type": "text", "text": "answer"}],
        "message_metadata": {"source_format": "anthropic", "native_blocks": []},
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1},
    }]

    assert sanitize_openai_messages(messages) == [{
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "private reasoning",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Read", "arguments": '{"path":"README.md"}'},
        }],
    }]


class _ClosableAsyncStream:
    def __init__(self, chunks, error_after_chunks=None):
        self._chunks = iter(chunks)
        self._error_after_chunks = error_after_chunks
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error_after_chunks is not None:
                raise self._error_after_chunks
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_async_chat_stream_retries_http2_error_before_output():
    first_stream = _ClosableAsyncStream(
        [],
        error_after_chunks=APIError(
            "Upstream HTTP/2 stream failed",
            request=httpx.Request("GET", "https://example.com"),
            body=None,
        ),
    )
    second_stream = _ClosableAsyncStream([
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="answer",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ])
    raw_client = Mock()
    raw_client.max_retries = 1
    raw_client.chat.completions.create = AsyncMock(side_effect=[first_stream, second_stream])
    client = AsyncChatAPIClient(raw_client, "test-model")

    with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
        events = [event async for event in client.generate_stream([{"role": "user", "content": "hello"}])]

    assert [event["type"] for event in events] == ["text", "done"]
    assert events[-1]["result"].text == "answer"
    assert raw_client.chat.completions.create.await_count == 2
    sleep.assert_awaited_once()
    assert first_stream.closed is True


@pytest.mark.anyio
async def test_async_chat_stream_cancellation_stops_before_retry():
    from system import stream_cancel

    first_stream = _ClosableAsyncStream(
        [],
        error_after_chunks=APIError(
            "Upstream HTTP/2 stream failed",
            request=httpx.Request("GET", "https://example.com"),
            body=None,
        ),
    )
    second_stream = _ClosableAsyncStream([
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="should not be requested",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ])
    raw_client = Mock()
    raw_client.max_retries = 1
    raw_client.chat.completions.create = AsyncMock(side_effect=[first_stream, second_stream])
    client = AsyncChatAPIClient(raw_client, "test-model")

    async def cancel_during_backoff(_delay):
        stream_cancel.cancel_current_response()

    stream_cancel.start_cancel_listener()
    try:
        with patch("utils.llm_client.asyncio.sleep", new=cancel_during_backoff), \
                patch("system.stream_cancel.post_tui"):
            events = [
                event
                async for event in client.generate_stream([{"role": "user", "content": "hello"}])
            ]
    finally:
        stream_cancel.stop_cancel_listener()

    assert events == []
    assert raw_client.chat.completions.create.await_count == 1
    assert first_stream.closed is True


@pytest.mark.anyio
async def test_tracked_sdk_retry_sleep_stops_when_response_is_cancelled():
    from system import stream_cancel

    client = llm_client_module._TrackedAsyncOpenAI(
        base_url="https://example.com",
        api_key="key",
    )

    async def cancel_during_sdk_backoff(_self, **_kwargs):
        stream_cancel.cancel_current_response()

    stream_cancel.start_cancel_listener()
    try:
        with patch.object(
            llm_client_module.AsyncOpenAI,
            "_sleep_for_retry",
            new=cancel_during_sdk_backoff,
        ), patch("system.stream_cancel.post_tui"):
            with pytest.raises(llm_client_module._LLMRequestCancelled):
                await client._sleep_for_retry(
                    retries_taken=0,
                    max_retries=1,
                    options=Mock(),
                    response=None,
                )
    finally:
        stream_cancel.stop_cancel_listener()
        await client.close()


@pytest.mark.anyio
async def test_async_chat_stream_does_not_replay_after_partial_http2_error():
    first_stream = _ClosableAsyncStream(
        [
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(
                        content="partial",
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                    ),
                    finish_reason=None,
                )],
                usage=None,
            ),
        ],
        error_after_chunks=APIError(
            "Upstream HTTP/2 stream failed",
            request=httpx.Request("GET", "https://example.com"),
            body=None,
        ),
    )
    raw_client = Mock()
    raw_client.max_retries = 5
    raw_client.chat.completions.create = AsyncMock(return_value=first_stream)
    client = AsyncChatAPIClient(raw_client, "test-model")

    with pytest.raises(APIError, match="Upstream HTTP/2 stream failed"):
        [event async for event in client.generate_stream([{"role": "user", "content": "hello"}])]

    assert raw_client.chat.completions.create.await_count == 1
    assert first_stream.closed is True


@pytest.mark.anyio
async def test_async_chat_stream_retries_empty_sse_parse_error_before_output():
    first_stream = _ClosableAsyncStream(
        [],
        error_after_chunks=json.JSONDecodeError("Expecting value", "", 0),
    )
    second_stream = _ClosableAsyncStream([
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="answer",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ])
    raw_client = Mock()
    raw_client.max_retries = 1
    raw_client.chat.completions.create = AsyncMock(side_effect=[first_stream, second_stream])
    client = AsyncChatAPIClient(raw_client, "test-model")

    with patch("utils.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
        events = [event async for event in client.generate_stream([{"role": "user", "content": "hello"}])]

    assert [event["type"] for event in events] == ["text", "done"]
    assert events[-1]["result"].text == "answer"
    assert raw_client.chat.completions.create.await_count == 2
    sleep.assert_awaited_once()
    assert first_stream.closed is True


@pytest.mark.anyio
async def test_async_chat_stream_does_not_replay_after_partial_output():
    first_stream = _ClosableAsyncStream(
        [
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(
                        content="partial",
                        reasoning_content=None,
                        reasoning=None,
                        tool_calls=None,
                    ),
                    finish_reason=None,
                )],
                usage=None,
            ),
        ],
        error_after_chunks=json.JSONDecodeError("Expecting value", "", 0),
    )
    raw_client = Mock()
    raw_client.max_retries = 5
    raw_client.chat.completions.create = AsyncMock(return_value=first_stream)
    client = AsyncChatAPIClient(raw_client, "test-model")

    with pytest.raises(json.JSONDecodeError):
        [event async for event in client.generate_stream([{"role": "user", "content": "hello"}])]

    assert raw_client.chat.completions.create.await_count == 1
    assert first_stream.closed is True


@pytest.mark.anyio
async def test_async_chat_stream_builds_unified_result_and_usage():
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="answer",
                    reasoning_content="reason",
                    reasoning=None,
                    tool_calls=[SimpleNamespace(
                        index=0,
                        id="call_1",
                        type="function",
                        function=SimpleNamespace(name="Read", arguments='{"path":"'),
                    )],
                ),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=[SimpleNamespace(
                        index=0,
                        id=None,
                        type=None,
                        function=SimpleNamespace(name=None, arguments='README.md"}'),
                    )],
                ),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        ),
    ]
    raw_stream = _ClosableAsyncStream(chunks)
    raw_client = Mock()
    raw_client.chat.completions.create = AsyncMock(return_value=raw_stream)
    client = AsyncChatAPIClient(raw_client, "test-model", "high")

    events = [event async for event in client.generate_stream([{
        "role": "user",
        "content": "hello",
        "message_metadata": {"source_format": "anthropic"},
    }], [{
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read",
            "parameters": {"type": "object", "properties": {}},
        },
    }])]

    assert [event["type"] for event in events] == ["text", "reasoning", "tool_calls", "done"]
    result = events[-1]["result"]
    assert result.text == "answer"
    assert result.reasoning == "reason"
    assert result.stop_reason == "tool_calls"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 20}
    assert result.tool_calls[0]["arguments"] == '{"path":"README.md"}'
    assert result.assistant_message["message_metadata"] == {
        "source_format": "openai_chat",
        "source_model": "test-model",
    }
    assert result.assistant_message["content_blocks"] == [
        {"type": "reasoning", "text": "reason"},
        {"type": "text", "text": "answer"},
        {
            "type": "tool_call",
            "id": "call_1",
            "name": "Read",
            "arguments": '{"path":"README.md"}',
        },
    ]
    assert raw_client.chat.completions.create.call_args.kwargs["messages"] == [{
        "role": "user",
        "content": "hello",
    }]
    assert set(raw_client.chat.completions.create.call_args.kwargs) == {
        "model",
        "messages",
        "stream",
        "stream_options",
        "reasoning_effort",
        "prompt_cache_key",
        "prompt_cache_retention",
        "tools",
    }
    assert raw_stream.closed is True


@pytest.mark.anyio
async def test_select_relevant_memory_ids_closes_temporary_recall_client():
    recall_client = FakeRecallClient()
    recall_client.client = Mock()

    with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
            patch.object(memory, "create_memory_recall_llm_client", return_value=recall_client):
        selected = await memory.select_relevant_memory_ids("query")

    assert selected == ["mem_a"]
    recall_client.client.close.assert_called_once_with()
    memory._MEMORY_RECALL_WINDOWS = {}


def test_previous_assistant_content_is_selected_for_user_pre_recall():
    content = main_module._get_previous_assistant_content(
        [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "上一轮 assistant 的回复"},
        ]
    )

    assert content == "上一轮 assistant 的回复"


def test_latest_empty_assistant_content_falls_back_to_older_non_empty_content():
    content = main_module._get_previous_assistant_content(
        [
            {"role": "assistant", "content": "更早的回复"},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": "   "},
            {"role": "assistant", "content": ""},
        ]
    )

    assert content == "更早的回复"


def test_user_pre_recall_has_no_valid_assistant_content():
    content = main_module._get_previous_assistant_content(
        [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": ""},
        ]
    )

    assert content == ""


@pytest.mark.anyio
async def test_user_request_passes_recall_query_to_agent_loop():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload="新的用户请求",
    ))
    history = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "上一轮 assistant 回复"},
    ]

    with patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock) as run_agent_loop, \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query("新的用户请求", history, command_handler)

    run_agent_loop.assert_awaited_once_with(history, recall_query="新的用户请求")


@pytest.mark.anyio
async def test_nm_request_skips_pre_recall_and_submits_suffix_as_user_query():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload="直接处理这个请求",
        skip_memory_recall=True,
    ))
    history = [{"role": "system", "content": "system"}]

    with patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock) as run_agent_loop, \
            patch.object(main_module, "post_tui") as post_tui, \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query("/nm 直接处理这个请求", history, command_handler)

    run_agent_loop.assert_awaited_once_with(history)
    assert history[-1] == {"role": "user", "content": "直接处理这个请求"}
    post_tui.assert_any_call(
        main_module.TuiRegion.BACKGROUND,
        "[#aaaaaa]🧠 已跳过本次请求的记忆预召回流程。[/#aaaaaa]",
    )


@pytest.mark.anyio
async def test_skill_command_passes_original_recall_query_and_loaded_content():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload=(
            "<skill name=\"demo-skill\">\nloaded demo\n</skill>\n\n"
            "User: /demo-skill 处理这个请求"
        ),
        original_query="/demo-skill 处理这个请求",
    ))
    history = [{"role": "system", "content": "system"}]

    with patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock) as run_agent_loop, \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query(
            "/demo-skill 处理这个请求",
            history,
            command_handler,
        )

    run_agent_loop.assert_awaited_once_with(
        history,
        recall_query="/demo-skill 处理这个请求",
    )
    message = history[-1]
    assert message["message_metadata"] == {
        "display_content": "/demo-skill 处理这个请求",
        "skill_command": True,
    }
    assert message["content"].startswith("<skill name=\"demo-skill\">\nloaded demo\n</skill>")
    assert message["content"].endswith("User: /demo-skill 处理这个请求")
    assert main_module._collect_user_message_content(history) == "/demo-skill 处理这个请求"


@pytest.mark.anyio
async def test_process_user_query_runs_agent_loop_for_title_detection():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload="hello",
    ))

    with patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock, return_value=True) as run_agent_loop, \
            patch.object(main_module, "generate_title", new_callable=AsyncMock) as generate_title, \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "refresh_status"):
        history = [{"role": "system", "content": "system"}]
        await main_module._process_user_query("hello", history, command_handler)

    run_agent_loop.assert_awaited_once_with(history, recall_query="hello")
    generate_title.assert_not_awaited()


@pytest.mark.anyio
async def test_agent_loop_resets_temporary_query_before_model_setup():
    with patch.object(main_module, "set_temporary_query_enabled") as set_enabled, \
            patch.object(main_module, "create_current_async_llm_client", return_value=None), \
            patch.object(main_module.console, "print"):
        committed = await main_module.agent_loop([
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ])

    assert committed is False
    set_enabled.assert_called_once_with(False)


@pytest.mark.anyio
async def test_agent_loop_injects_temporary_query_into_next_model_request():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    requests = []
    responses = [
        (
            "working",
            [],
            {"role": "assistant", "content": "working", "stop_reason": "pause_turn"},
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]

    class FakeClient:
        def append_assistant_message(self, history, raw_message):
            history.append(raw_message)

    async def stream_with_render(history, tools, client):
        requests.append(list(history))
        return responses[len(requests) - 1]

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "post_tui") as post_tui, \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "consume_temporary_query", side_effect=[None, "/reset project"]), \
            patch.object(main_module, "clear_temporary_query"), \
            patch.object(main_module, "set_temporary_query_enabled"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "_apply_pending_title"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert len(requests) == 2
    temporary_message = requests[1][-1]
    assert temporary_message["role"] == "user"
    assert temporary_message["message_metadata"] == {"temporary_query": True}
    assert main_module.TEMPORARY_INSTRUCTION_START in temporary_message["content"]
    assert "/reset project" in temporary_message["content"]
    assert "task currently in progress.\nAfter addressing it, resume the current task from where you left off." in temporary_message["content"]
    assert "Do not stop merely because you have responded to this instruction while the task remains incomplete" in temporary_message["content"]
    assert "unless the enclosed text explicitly changes, pauses, or cancels the task.\n\n/reset project" in temporary_message["content"]
    assert "Do not execute it as a MakeCode slash command" not in temporary_message["content"]
    assert main_module.TEMPORARY_INSTRUCTION_END in temporary_message["content"]
    content_payloads = [
        call.args[1]
        for call in post_tui.call_args_list
        if call.args and call.args[0] == main_module.TuiRegion.CONTENT
    ]
    assert content_payloads[0] == "[#3f3f46]─[/#3f3f46]"
    assert content_payloads[1].renderable.plain == temporary_message["content"]
    assert content_payloads[1].title == "[bold #22c55e]You[/bold #22c55e]"


@pytest.mark.anyio
async def test_agent_loop_discards_pending_temporary_query_when_final_round_ends():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        def append_assistant_message(self, history, raw_message):
            history.append(raw_message)

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=(
                    "done",
                    [],
                    {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
                    False,
                )),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "consume_temporary_query", return_value=None) as consume, \
            patch.object(main_module, "clear_temporary_query") as clear, \
            patch.object(main_module, "set_temporary_query_enabled"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "_apply_pending_title"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    consume.assert_called_once_with()
    assert clear.call_count >= 1


@pytest.mark.anyio
async def test_agent_loop_restores_temporary_query_before_clearing_after_final_round():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    events = []

    class FakeClient:
        def append_assistant_message(self, history, raw_message):
            history.append(raw_message)

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=(
                    "done",
                    [],
                    {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
                    False,
                )),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "consume_temporary_query", return_value=None), \
            patch.object(main_module, "restore_temporary_query_to_input", side_effect=lambda: events.append("restore")), \
            patch.object(main_module, "clear_temporary_query", side_effect=lambda: events.append("clear")), \
            patch.object(main_module, "set_temporary_query_enabled"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "_apply_pending_title"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert events[:2] == ["restore", "clear"]


@pytest.mark.anyio
async def test_agent_loop_removes_injected_temporary_query_when_cancelled():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        def append_assistant_message(self, history, raw_message):
            history.append(raw_message)

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "post_tui"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=("partial", [], None, True)),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages") as save_messages, \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "consume_temporary_query", return_value="temporary"), \
            patch.object(main_module, "clear_temporary_query") as clear, \
            patch.object(main_module, "set_temporary_query_enabled"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock) as generate_title, \
            patch.object(main_module, "_apply_pending_title"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is False
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    save_messages.assert_not_called()
    generate_title.assert_not_awaited()
    assert clear.call_count >= 1


@pytest.mark.anyio
async def test_agent_loop_checks_for_missing_title_once_after_all_iterations():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    responses = [
        (
            "",
            [],
            {"role": "assistant", "content": "", "stop_reason": "pause_turn"},
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]
    events = []

    async def stream_with_render(current_messages, current_tools, llm_client):
        events.append("stream")
        return responses.pop(0)

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

    def save_messages(current_messages):
        events.append("save")

    async def generate_title_if_missing(current_messages):
        assert current_messages is messages
        events.append("title")
        return False

    def apply_pending_title():
        events.append("apply")

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages", side_effect=save_messages), \
            patch.object(main_module, "_generate_title_if_missing", side_effect=generate_title_if_missing), \
            patch.object(main_module, "_apply_pending_title", side_effect=apply_pending_title), \
            patch.object(main_module, "estimate_tokens", return_value=0):
        committed = await main_module.agent_loop(
            messages,
            llm_client=FakeClient(),
        )

    assert committed is True
    assert events == [
        "stream",
        "save",
        "stream",
        "save",
        "title",
        "apply",
    ]


@pytest.mark.anyio
async def test_title_detection_uses_all_user_message_content_when_conversation_has_no_title(tmp_path):
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "第一条用户消息"},
        {"role": "assistant", "content": "assistant"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "第二条用户消息"},
                {"type": "text", "text": "补充内容"},
            ],
        },
    ]
    store = ConversationStore(tmp_path / "conversations")
    store.save_messages(history)

    with patch.object(main_module, "CONVERSATION_STORE", store), \
            patch.object(main_module, "_pending_title", None), \
            patch.object(main_module, "generate_title", AsyncMock(return_value="新标题")) as generate_title, \
            patch.object(main_module, "post_tui"):
        generated = await main_module._generate_title_if_missing(history)

        assert generated is True
        assert main_module._pending_title == "新标题"
        generate_title.assert_awaited_once_with(
            "第一条用户消息\n\n第二条用户消息\n\n补充内容"
        )


@pytest.mark.anyio
async def test_title_detection_skips_generation_when_conversation_has_title(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    store.save_messages([{"role": "system", "content": "system"}])
    store.update_title("现有标题")

    with patch.object(main_module, "CONVERSATION_STORE", store), \
            patch.object(main_module, "generate_title", new_callable=AsyncMock) as generate_title:
        generated = await main_module._generate_title_if_missing([
            {"role": "user", "content": "hello"},
        ])

    assert generated is False
    generate_title.assert_not_awaited()


@pytest.mark.anyio
async def test_regenerate_title_uses_all_user_message_content(tmp_path):
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "第一条用户消息"},
        {"role": "assistant", "content": "assistant"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "第二条用户消息"},
                {"type": "text", "text": "补充内容"},
            ],
        },
        {"role": "tool", "content": "tool result"},
    ]

    store = ConversationStore(tmp_path / "conversations")
    store.save_messages(history)

    with patch.object(main_module, "CONVERSATION_STORE", store), \
            patch.object(main_module, "_pending_title", None), \
            patch.object(main_module, "generate_title", AsyncMock(return_value="新标题")) as generate, \
            patch.object(main_module, "_apply_pending_title") as apply_title, \
            patch.object(main_module, "refresh_status") as refresh:
        await main_module._regenerate_conversation_title(history)

        generate.assert_awaited_once_with(
            "第一条用户消息\n\n第二条用户消息\n\n补充内容"
        )
        assert main_module._pending_title == "新标题"
        apply_title.assert_called_once_with()
        refresh.assert_called_once_with()


def test_applied_pending_title_becomes_current_display_title(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "system"}])
    original_root = conversation.parent

    with patch.object(main_module, "CONVERSATION_STORE", store), \
            patch.object(main_module, "_pending_title", "标题展示优化"):
        main_module._apply_pending_title()

        assert store.active_path == conversation
        assert store.active_path.parent == original_root
        assert main_module._get_current_conversation_title() == "标题展示优化"


@pytest.mark.anyio
async def test_loaded_title_refreshes_after_conversation_activation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    loaded_history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "loaded"},
    ]
    loaded_conversation = store.save_messages(loaded_history)
    store.update_title("加载标题")
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.LOAD_HISTORY,
        payload=(loaded_history, loaded_conversation),
    ))
    refreshed_titles = []

    with patch.object(main_module, "CONVERSATION_STORE", store), patch.object(
            main_module,
            "refresh_status",
            side_effect=lambda: refreshed_titles.append(
                main_module._get_current_conversation_title()
            ),
    ):
        history = [{"role": "system", "content": "system"}]
        await main_module._process_user_query("/load", history, command_handler)

    assert history == loaded_history
    assert refreshed_titles == ["加载标题"]


def test_untitled_conversation_has_stable_display_fallback(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    store.save_messages([{"role": "system", "content": "system"}])

    with patch.object(main_module, "CONVERSATION_STORE", store):
        assert main_module._get_current_conversation_title() == "未命名对话"


@pytest.mark.anyio
async def test_generate_title_uses_stream_result_and_closes_independent_async_client():
    class FakeTitleClient:
        def __init__(self):
            self.requests = []

        def format_tools(self, tools):
            return tools

        async def generate_stream(self, messages, tools):
            self.requests.append((list(messages), tools))
            yield {
                "type": "done",
                "result": SimpleNamespace(
                    text="",
                    tool_calls=[{
                        "name": "GenerateConversationTitle",
                        "arguments": '{"title":"title"}',
                    }],
                    stop_reason="tool_use",
                    assistant_message={"role": "assistant", "content": None},
                ),
            }

    title_client = FakeTitleClient()

    with patch.object(main_module, "create_current_async_llm_client", return_value=title_client), \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock) as close_client:
        title = await main_module.generate_title("hello")

    assert title == "title"
    close_client.assert_awaited_once_with(title_client)
    assert len(title_client.requests) == 1


@pytest.mark.anyio
async def test_generate_title_retries_with_bounded_rounds_until_tool_call():
    class FakeTitleClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return tools

        async def generate_stream(self, messages, tools):
            self.requests.append(list(messages))
            self.calls += 1
            if self.calls == 1:
                result = SimpleNamespace(
                    text="plain text is not a title",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={
                        "role": "assistant",
                        "content": "plain text is not a title",
                        "stop_reason": "end_turn",
                    },
                )
            else:
                result = SimpleNamespace(
                    text="",
                    tool_calls=[{
                        "name": "GenerateConversationTitle",
                        "arguments": '{"title":"title"}',
                    }],
                    stop_reason="tool_use",
                    assistant_message={"role": "assistant", "content": None},
                )
            yield {"type": "done", "result": result}

    title_client = FakeTitleClient()

    with patch.object(main_module, "create_current_async_llm_client", return_value=title_client), \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock):
        title = await main_module.generate_title("hello")

    assert title == "title"
    assert title_client.calls == 2
    assert title_client.requests[1][-2]["content"] == "plain text is not a title"
    assert "current_round=1 / max_round=8" in title_client.requests[1][-1]["content"]


@pytest.mark.anyio
async def test_generate_title_returns_validation_error_to_model_and_retries():
    class FakeTitleClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return tools

        def format_tool_result(self, tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

        async def generate_stream(self, messages, tools):
            self.calls += 1
            self.requests.append(list(messages))
            if self.calls == 1:
                tool_calls = [{
                    "id": "call_invalid",
                    "name": "GenerateConversationTitle",
                    "arguments": '{"title":"wrong","unexpected":true}',
                }]
            else:
                tool_calls = [{
                    "id": "call_valid",
                    "name": "GenerateConversationTitle",
                    "arguments": '{"title":"title"}',
                }]
            yield {
                "type": "done",
                "result": SimpleNamespace(
                    text="",
                    tool_calls=tool_calls,
                    stop_reason="tool_use",
                    assistant_message={"role": "assistant", "content": None, "tool_calls": tool_calls},
                ),
            }

    title_client = FakeTitleClient()
    with patch.object(main_module, "create_current_async_llm_client", return_value=title_client), \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock):
        title = await main_module.generate_title("hello", max_rounds=2)

    assert title == "title"
    assert title_client.calls == 2
    tool_result = title_client.requests[1][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["is_error"] is True
    assert "unexpected" in tool_result["content"]
    assert "extra_forbidden" in tool_result["content"]


@pytest.mark.anyio
async def test_generate_title_exits_at_max_rounds_without_tool_call():
    class FakeTitleClient:
        def __init__(self):
            self.calls = 0

        def format_tools(self, tools):
            return tools

        async def generate_stream(self, messages, tools):
            self.calls += 1
            yield {
                "type": "done",
                "result": SimpleNamespace(
                    text="plain text",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "plain text"},
                ),
            }

    title_client = FakeTitleClient()

    with patch.object(main_module, "create_current_async_llm_client", return_value=title_client), \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock):
        title = await main_module.generate_title("hello", max_rounds=3)

    assert title is None
    assert title_client.calls == 3


@pytest.mark.anyio
async def test_run_tool_handler_threads_sync_handlers_and_awaits_async_handlers_directly():
    event_loop_thread = threading.get_ident()

    def sync_handler():
        return threading.get_ident()

    async def async_handler():
        return threading.get_ident()

    sync_thread = await main_module._run_tool_handler(sync_handler, {})
    async_thread = await main_module._run_tool_handler(async_handler, {})

    assert sync_thread != event_loop_thread
    assert async_thread == event_loop_thread


@pytest.mark.anyio
async def test_agent_loop_cancel_discards_response_without_tools_or_conversation_save():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    fake_client = Mock()
    tool_call = {"id": "call_1", "name": "DoWork", "arguments": "{}"}

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=("partial", [tool_call], {"role": "assistant"}, True)),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages") as save_messages, \
            patch.object(main_module, "estimate_tokens", return_value=0):
        committed = await main_module.agent_loop(messages, llm_client=fake_client)

    assert committed is False
    fake_client.append_assistant_message.assert_not_called()
    fake_client.format_tool_result.assert_not_called()
    save_messages.assert_not_called()
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("initial_tokens,partial_result,partial_calls,tool_calls,expected_messages", [
    (69, None, 0, 0, []),
    (70, None, 0, 1, ["已触发第一层工具输出裁剪", "第一层工具输出裁剪已完成"]),
    (90, True, 1, 0, ["已触发第二层局部摘要压缩", "第二层局部摘要压缩已完成"]),
    (101, True, 1, 0, ["已触发第二层局部摘要压缩", "第二层局部摘要压缩已完成"]),
    (90, False, 1, 1, [
        "已触发第二层局部摘要压缩",
        "第二层局部摘要压缩未提交，回退执行第一层工具输出裁剪",
        "第一层工具输出裁剪已完成",
    ]),
])
async def test_agent_loop_runs_at_most_one_entry_compaction_layer(
        initial_tokens,
        partial_result,
        partial_calls,
        tool_calls,
        expected_messages,
):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
    ]
    partial = AsyncMock(return_value=partial_result)

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=initial_tokens) as estimate_tokens, \
            patch.object(main_module, "partial_compact", new=partial), \
            patch.object(main_module, "compact_tool_outputs", return_value=True) as compact_tool_outputs, \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                new_callable=AsyncMock,
                return_value=("", [], None, True),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages") as save_messages, \
            patch.object(main_module, "post_tui") as post_tui:
        committed = await main_module.agent_loop(messages, llm_client=Mock())

    assert committed is False
    assert partial.await_count == partial_calls
    if partial_calls:
        assert partial.await_args.args[2] == initial_tokens
    assert compact_tool_outputs.call_count == tool_calls
    assert estimate_tokens.call_count == 1
    save_messages.assert_not_called()
    background_messages = [
        str(call.args[1])
        for call in post_tui.call_args_list
        if call.args and call.args[0] == main_module.TuiRegion.BACKGROUND and len(call.args) > 1
    ]
    for expected_message in expected_messages:
        assert any(expected_message in message for message in background_messages)
    assert not any("第三层" in message for message in background_messages)


@pytest.mark.anyio
async def test_agent_loop_recalls_memory_after_entry_compaction():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
    ]
    events = []

    async def compact(*args):
        events.append("compact")
        return True

    async def recall(query, **kwargs):
        events.append("recall")
        assert query == "current request"
        assert kwargs["previous_assistant_content"] == "old answer"
        return {"content": "## mem_new\n- Insight: use the updated memory"}

    async def stream(*args):
        events.append("stream")
        return "", [], None, True

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=90), \
            patch.object(main_module, "partial_compact", new=AsyncMock(side_effect=compact)), \
            patch.object(main_module, "recall_long_term_memories", new=AsyncMock(side_effect=recall)), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", new=AsyncMock(side_effect=stream)), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module, "post_tui"):
        committed = await main_module.agent_loop(
            messages,
            llm_client=Mock(),
            recall_query="current request",
        )

    assert committed is False
    assert events == ["compact", "recall", "stream"]
    assert messages[-1]["content"].startswith("# Potentially Relevant Memories")
    assert messages[-1]["content"].endswith("current request")


@pytest.mark.anyio
async def test_agent_loop_prepends_recalled_memory_without_dropping_current_image():
    image = {
        "type": "image",
        "attachment_id": "img_00000000000000000000000000000000",
        "filename": "x.png",
        "media_type": "image/png",
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": [{"type": "text", "text": "question"}, image]},
    ]

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(
                main_module,
                "recall_long_term_memories",
                new=AsyncMock(return_value={"content": "## mem_new\n- Insight: relevant"}),
            ), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                new=AsyncMock(return_value=("", [], None, True)),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module, "post_tui"):
        committed = await main_module.agent_loop(
            messages,
            llm_client=Mock(),
            recall_query="question",
        )

    assert committed is False
    assert messages[-1]["content"][0]["text"].startswith("# Potentially Relevant Memories")
    assert messages[-1]["content"][1:] == [{"type": "text", "text": "question"}, image]


@pytest.mark.anyio
async def test_agent_loop_reports_when_first_layer_has_no_eligible_tool_outputs():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
    ]

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=70), \
            patch.object(main_module, "compact_tool_outputs", return_value=False), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                new_callable=AsyncMock,
                return_value=("", [], None, True),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module, "post_tui") as post_tui:
        committed = await main_module.agent_loop(messages, llm_client=Mock())

    assert committed is False
    background_messages = [
        str(call.args[1])
        for call in post_tui.call_args_list
        if call.args and call.args[0] == main_module.TuiRegion.BACKGROUND and len(call.args) > 1
    ]
    assert any("已触发第一层工具输出裁剪" in message for message in background_messages)
    assert any("没有可裁剪的较早工具输出" in message for message in background_messages)


@pytest.mark.anyio
async def test_agent_loop_renders_usage_after_entry_compaction_commits():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
    ]

    async def compact(current_messages, context_token_limit, current_context_tokens, reason):
        current_messages[1:3] = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "summary acknowledged"},
        ]
        return True

    rendered_snapshots = []

    def capture_usage(current_messages, **kwargs):
        rendered_snapshots.append(json.loads(json.dumps(current_messages)))

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=90), \
            patch.object(main_module, "partial_compact", new=AsyncMock(side_effect=compact)), \
            patch.object(main_module, "_render_token_usage", side_effect=capture_usage), \
            patch.object(
                main_module,
                "_stream_with_render",
                new_callable=AsyncMock,
                return_value=("", [], None, True),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module, "post_tui"):
        committed = await main_module.agent_loop(messages, llm_client=Mock())

    assert committed is False
    assert rendered_snapshots == [messages]
    assert messages[1:3] == [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "summary acknowledged"},
    ]


@pytest.mark.anyio
async def test_agent_loop_falls_back_to_tool_output_compaction_when_partial_compaction_fails():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
    ]

    with patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "get_context_token_limit", return_value=100), \
            patch.object(main_module, "get_compaction_thresholds", return_value=(70, 90)), \
            patch.object(main_module, "estimate_tokens", return_value=90), \
            patch.object(main_module, "partial_compact", new_callable=AsyncMock, side_effect=RuntimeError("failed")), \
            patch.object(main_module, "compact_tool_outputs") as compact_tool_outputs, \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                new_callable=AsyncMock,
                return_value=("", [], None, True),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module, "log_error_traceback"), \
            patch.object(main_module.console, "print"):
        committed = await main_module.agent_loop(messages, llm_client=Mock())

    assert committed is False
    compact_tool_outputs.assert_called_once_with(messages)


@pytest.mark.anyio
async def test_agent_loop_creates_and_closes_request_local_client():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

    local_client = FakeClient()
    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=(
                    "done",
                    [],
                    {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
                    False,
                )),
            ), \
            patch.object(main_module, "create_current_async_llm_client", return_value=local_client) as create_client, \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock) as close_client, \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "_apply_pending_title"):
        committed = await main_module.agent_loop(messages)

    assert committed is True
    create_client.assert_called_once_with()
    close_client.assert_awaited_once_with(local_client)


@pytest.mark.anyio
async def test_agent_loop_reads_current_context_limit_for_entry_and_render_without_auto_compact():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage") as render_token_usage, \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=(
                    "done",
                    [],
                    {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
                    False,
                )),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=1500) as estimate_tokens, \
            patch.object(main_module, "get_context_token_limit", return_value=2048) as get_limit, \
            patch.object(main_module, "auto_compact", new_callable=AsyncMock) as auto_compact, \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "refresh_status"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert get_limit.call_count == 2
    assert estimate_tokens.call_count == 1
    assert render_token_usage.call_args.kwargs["threshold"] == 2048
    auto_compact.assert_not_awaited()


@pytest.mark.anyio
async def test_agent_loop_cancel_after_committed_round_does_not_recheck_compaction():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    tool_call = {"id": "call_1", "name": "MissingTool", "arguments": "{}"}
    responses = [
        (
            "",
            [tool_call],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
                "stop_reason": "tool_use",
            },
            False,
        ),
        ("", [], None, True),
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", AsyncMock(side_effect=responses)), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0) as estimate_tokens, \
             patch.object(main_module, "auto_compact", new_callable=AsyncMock) as auto_compact, \
             patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert estimate_tokens.call_count == 1
    auto_compact.assert_not_awaited()


@pytest.mark.anyio
async def test_agent_loop_keeps_mcp_arguments_outside_builtin_pydantic_registry():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "use mcp"},
    ]
    tool_call = {
        "id": "call_mcp",
        "name": "mcp_tool",
        "arguments": json.dumps({"server_specific": "accepted"}),
    }
    responses = [
        (
            "",
            [tool_call],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
                "stop_reason": "tool_use",
            },
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    async def stream_with_render(current_messages, current_tools, client):
        return responses.pop(0)

    handler = Mock(return_value="mcp result")
    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {"mcp_tool": handler})), \
             patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
             patch.object(main_module, "estimate_tokens", return_value=0), \
             patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    handler.assert_called_once_with(server_specific="accepted")
    tool_result = next(item for item in messages if item.get("role") == "tool")
    assert tool_result["content"] == "mcp result"
    assert "is_error" not in tool_result


@pytest.mark.anyio
async def test_agent_loop_returns_builtin_validation_error_without_calling_handler():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "search"},
    ]
    invalid_call = {
        "id": "call_invalid",
        "name": "ContentSearch",
        "arguments": json.dumps({
            "content_regex": "TODO",
            "root_dir": ".",
            "filename": "*.py",
            "context_size": 1,
        }),
    }
    responses = [
        (
            "",
            [invalid_call],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [invalid_call],
                "stop_reason": "tool_use",
            },
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    async def stream_with_render(current_messages, current_tools, client):
        return responses.pop(0)

    handler = Mock()
    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
             patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
             patch.object(main_module, "estimate_tokens", return_value=0), \
             patch.object(main_module, "_apply_pending_title"), \
             patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False), \
            patch.object(main_module, "BASE_SUPER_TOOLS_HANDLERS", {"ContentSearch": handler}):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    handler.assert_not_called()
    tool_result = next(item for item in messages if item.get("role") == "tool")
    assert tool_result["is_error"] is True
    assert "filename" in tool_result["content"]
    assert "path_regex" in tool_result["content"]
    assert "Input value: '*.py'" in tool_result["content"]


@pytest.mark.anyio
async def test_agent_loop_resumes_pause_turn_and_marks_unknown_tool_result_as_error():
    TOOL_EXECUTION_HISTORY.clear()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    requests = []
    tool_call = {"id": "call_1", "name": "MissingTool", "arguments": "{}"}
    responses = [
        (
            "partial",
            [],
            {"role": "assistant", "content": "partial", "stop_reason": "pause_turn"},
            False,
        ),
        (
            "",
            [tool_call],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
                "stop_reason": "tool_use",
            },
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]

    async def stream_with_render(current_messages, current_tools, llm_client):
        requests.append(list(current_messages))
        return responses[len(requests) - 1]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
             patch.object(main_module.CONVERSATION_STORE, "save_messages") as save_messages, \
             patch.object(main_module, "estimate_tokens", return_value=0), \
             patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert len(requests) == 3
    assert requests[1][-1]["stop_reason"] == "pause_turn"
    tool_result = next(message for message in requests[2] if message.get("role") == "tool")
    assert tool_result["name"] == "MissingTool"
    assert tool_result["is_error"] is True
    assert TOOL_EXECUTION_HISTORY.snapshot() == []
    assert save_messages.call_count == 4


def test_deleting_current_model_selects_a_valid_process_local_fallback(tmp_path):
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["alpha", "beta"])
    current_key = manager.get_current_model().key

    assert manager.delete_model_by_key(current_key)

    assert manager.get_current_model() is not None
    assert manager.get_current_model().key != current_key
    assert manager.get_current_model().key in {model.key for model in models if model.key != current_key}
    assert manager.current_model_key == manager.get_current_model().key


@pytest.mark.anyio
async def test_agent_loops_close_only_their_own_request_local_clients():
    class FakeClient:
        def __init__(self, name):
            self.name = name

        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

    clients = [FakeClient("first"), FakeClient("second")]

    async def stream_with_render(messages, tools, llm_client):
        await asyncio.sleep(0)
        return (
            llm_client.name,
            [],
            {"role": "assistant", "content": llm_client.name, "stop_reason": "end_turn"},
            False,
        )

    with patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module, "create_current_async_llm_client", side_effect=clients), \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock) as close_client, \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "_apply_pending_title"):
        results = await asyncio.gather(
            main_module.agent_loop([{"role": "system", "content": "system"}, {"role": "user", "content": "one"}]),
            main_module.agent_loop([{"role": "system", "content": "system"}, {"role": "user", "content": "two"}]),
        )

    assert results == [True, True]
    assert close_client.await_count == 2
    assert {call.args[0] for call in close_client.await_args_list} == set(clients)


@pytest.mark.anyio
async def test_models_command_does_not_create_or_close_an_llm_client():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.CONTINUE,
    ))
    history = [{"role": "system", "content": "system"}]

    with patch.object(main_module, "create_current_async_llm_client") as create_client, \
            patch.object(main_module, "close_async_llm_client", new_callable=AsyncMock) as close_client:
        await main_module._process_user_query("/models", history, command_handler)

    create_client.assert_not_called()
    close_client.assert_not_awaited()
    command_handler.process_command.assert_awaited_once()


@pytest.mark.anyio
async def test_normal_query_enters_agent_loop():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload="hello",
        skip_memory_recall=True,
    ))
    history = [{"role": "system", "content": "system"}]

    with patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock) as agent_loop, \
            patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query("hello", history, command_handler)

    agent_loop.assert_awaited_once_with(history)
    assert history[-1] == {"role": "user", "content": "hello"}


def test_textual_submit_delegates_client_lifecycle_to_business_operations():
    history = [{"role": "system", "content": "system"}]
    command_handler = Mock()

    class FakeTuiApp:
        def __init__(self, **kwargs):
            self.submit_handler = kwargs["submit_handler"]

        def run(self):
            asyncio.run(self.submit_handler("hello"))

    with patch.object(main_module, "MakeCodeTuiApp", FakeTuiApp), \
            patch.object(main_module, "_process_user_query", new_callable=AsyncMock) as process_query:
        main_module._run_textual_main(history, command_handler, prompt_for_workdir=False)

    process_query.assert_awaited_once_with("hello", history, command_handler)


def test_textual_startup_load_scrolls_loaded_history_to_bottom():
    history = [{"role": "system", "content": "system"}]
    loaded_history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "loaded"},
    ]
    command_handler = Mock()
    command_handler.handle_load.return_value = (loaded_history, object())

    class FakeTuiApp:
        def __init__(self, **kwargs):
            self.startup_load_handler = kwargs["startup_load_handler"]

        def run(self):
            self.startup_load_handler()

    with patch.object(main_module, "MakeCodeTuiApp", FakeTuiApp), \
            patch.object(main_module.cli_module, "STARTUP_LOAD_REQUESTED", True), \
            patch.object(main_module, "scroll_all_panes_to_bottom") as scroll_to_bottom, \
            patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "refresh_status"):
        main_module._run_textual_main(
            history,
            command_handler,
            prompt_for_workdir=False,
            startup_load_id="conv_0123456789abcdef0123456789abcdef",
        )

    scroll_to_bottom.assert_called_once_with()


def test_current_client_factory_reflects_reasoning_effort_without_cache():
    medium_model = ModelConfig("https://example.com", "key", "same", reasoning_effort="medium")
    high_model = ModelConfig("https://example.com", "key", "same", reasoning_effort="high")
    created_clients = [Mock(), Mock()]

    with patch("utils.llm_client.get_current_model_config", side_effect=[medium_model, high_model]), \
            patch("utils.llm_client._create_async_chat_client", side_effect=created_clients) as create_client:
        assert llm_client_module.create_current_async_llm_client() is created_clients[0]
        assert llm_client_module.create_current_async_llm_client() is created_clients[1]

    assert create_client.call_count == 2


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://example.com", "https://example.com/v1"),
        ("https://example.com/", "https://example.com/v1"),
        ("https://example.com/v1", "https://example.com/v1"),
        ("https://example.com/v9", "https://example.com/v9"),
        ("https://example.com/v10", "https://example.com/v10"),
        ("https://example.com/v2026", "https://example.com/v2026"),
        ("https://example.com/version1", "https://example.com/version1/v1"),
    ],
)
def test_normalize_base_url_adds_default_version(base_url, expected):
    assert llm_client_module._normalize_base_url(base_url) == expected


@pytest.mark.anyio
async def test_async_llm_client_has_bounded_timeout_and_retries():
    model = ModelConfig("https://example.com", "key", "main")

    client = llm_client_module._create_async_chat_client(model)
    try:
        assert str(client.client.base_url) == "https://example.com/v1/"
        request = client.client._client.build_request("GET", "https://example.com/v1/test")
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 120.0,
            "write": 120.0,
            "pool": 120.0,
        }
        assert client.client.max_retries == 5
        assert client.client._should_retry(httpx.Response(404)) is True
    finally:
        await client.client.close()


def test_runtime_info_displays_current_reasoning_effort():
    model = ModelConfig("https://example.com", "key", "main", reasoning_effort="high")

    with patch("system.models.get_current_model_config", return_value=model), \
            patch("utils.hitl.get_hitl_status", return_value=True), \
            patch("utils.plan_mode.is_plan_mode", return_value=False):
        runtime_info = console_render.format_runtime_info()

    assert "Model: main (example.com) · Effort: high" in runtime_info
    assert "HITL" not in runtime_info
    assert "Format:" not in runtime_info


def test_runtime_info_displays_only_last_three_model_domain_levels():
    model = ModelConfig("https://aa.bb.cc.dd/v1", "key", "main")

    with patch("system.models.get_current_model_config", return_value=model), \
            patch("utils.hitl.get_hitl_status", return_value=True), \
            patch("utils.plan_mode.is_plan_mode", return_value=False):
        runtime_info = console_render.format_runtime_info()

    assert "Model: main (bb.cc.dd)" in runtime_info
    assert "aa.bb.cc.dd" not in runtime_info
    assert model.get_display_text() == "main (aa.bb.cc.dd)"
