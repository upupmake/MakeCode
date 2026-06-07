import json
from unittest.mock import Mock, patch

from system.models import ModelConfig, ModelManager
from system import ts_validator, window_attention
from system.tui_modals import ChoiceModal, MemoryConfigModal, RecallModelPickerModal, AddModelModal, LayoutModal
from utils import memory
from utils.llm_client import create_memory_recall_llm_client


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

    def generate(self, messages, tools):
        self.generate_calls += 1
        self.messages.append(list(messages))
        return object()

    def parse_response(self, response):
        return "", [
            {
                "id": "call_1",
                "name": "SelectRelevantMemories",
                "arguments": json.dumps({"memory_ids": self.selected_ids}),
                "raw": {},
            }
        ], {"role": "assistant", "content": ""}

    def append_assistant_message(self, messages, raw_message):
        messages.append(raw_message)


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


def test_model_manager_preserves_unknown_top_level_fields(tmp_path):
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


def test_select_relevant_memory_ids_prefers_recall_client_over_global_client():
    recall_client = FakeRecallClient()
    global_client = FakeRecallClient()

    with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
            patch.object(memory, "create_memory_recall_llm_client", return_value=recall_client), \
            patch.object(memory, "llm_client", global_client):
        selected = memory.select_relevant_memory_ids("query")

    assert selected == ["mem_a"]
    assert recall_client.generate_calls == 1
    assert global_client.generate_calls == 0


def test_window_attention_is_noop_on_non_windows():
    with patch.object(window_attention.sys, "platform", "linux"):
        window_attention.request_window_attention()


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


def test_tui_modals_use_q_not_escape_for_cancel():
    for modal in [ChoiceModal, MemoryConfigModal, RecallModelPickerModal, AddModelModal, LayoutModal]:
        keys = {binding.key for binding in modal.BINDINGS}
        assert "q" in keys
        assert "escape" not in keys
