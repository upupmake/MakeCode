import json
from unittest.mock import AsyncMock, Mock, patch

from rich.text import Text

from system.models import ModelConfig, ModelManager, REASONING_EFFORTS
from system import console_render, ts_validator, updater, window_attention
from system.tui_modals import ChoiceModal, MemoryConfigModal, RecallModelPickerModal, AddModelModal, LayoutModal, ModelManagerModal
from utils import llm_client as llm_client_module, memory
from utils.llm_client import ChatAPIClient, AsyncChatAPIClient, DynamicLLMClientProxy, create_memory_recall_llm_client


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


def test_dynamic_llm_client_proxy_reuses_client_until_model_changes():
    first_model = ModelConfig("https://example.com", "key", "first")
    second_model = ModelConfig("https://example.com", "key", "second")
    created_clients = [Mock(), Mock()]
    proxy = DynamicLLMClientProxy()

    llm_client_module._cached_llm_client = None
    llm_client_module._cached_model_key = None
    with patch("utils.llm_client.get_current_model_config", side_effect=[first_model, first_model, second_model]), \
            patch("utils.llm_client._create_chat_client", side_effect=created_clients) as create_client:
        assert proxy.client is created_clients[0].client
        assert proxy.client is created_clients[0].client
        assert proxy.client is created_clients[1].client

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
    memory._MEMORY_RECALL_WINDOWS = {}


def test_memory_recall_window_size_config_preserves_existing_fields(tmp_path):
    memory.refresh_workspace_paths()
    original_config_file = memory.MEMORY_CONFIG_FILE
    original_cache = memory._MEMORY_CONFIG_CACHE
    try:
        memory.MEMORY_CONFIG_FILE = tmp_path / "memory_config.json"
        memory._MEMORY_CONFIG_CACHE = None
        memory.MEMORY_CONFIG_FILE.write_text(json.dumps({"memory_size": 9}), encoding="utf-8")

        assert memory.get_memory_recall_window_size() == 3
        assert memory.set_memory_recall_window_size(5) == 5

        saved = json.loads(memory.MEMORY_CONFIG_FILE.read_text(encoding="utf-8"))
        assert saved["memory_size"] == 9
        assert saved["memory_recall_window_size"] == 5
    finally:
        memory.MEMORY_CONFIG_FILE = original_config_file
        memory._MEMORY_CONFIG_CACHE = original_cache


def test_memory_config_modal_includes_recall_window_size_field():
    fields = MemoryConfigModal._FIELDS

    assert "memory_size" in fields
    assert "keep_recent_tool_call" in fields
    assert fields["memory_recall_window_size"]["input_id"] == "memory-config-memory-recall-window-size"


def test_window_attention_is_noop_on_non_windows():
    with patch.object(window_attention.sys, "platform", "linux"):
        window_attention.request_window_attention()


def test_launch_updater_rejects_unsupported_platform(tmp_path):
    with patch.object(updater, "AUTO_UPDATE_SUPPORTED", False):
        with pytest.raises(RuntimeError, match="不支持应用内自动更新"):
            updater.launch_updater(tmp_path / "MakeCode")


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
    for modal in [ChoiceModal, MemoryConfigModal, RecallModelPickerModal, AddModelModal, LayoutModal]:
        keys = {binding.key for binding in modal.BINDINGS}
        assert "q" in keys
        assert "escape" not in keys


# ---------- ChoiceModal 渲染与交互测试 ----------

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Label


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

    async with app.run_test() as pilot:
        await pilot.pause()
        title = str(modal.query_one("#choice-title", Label).render())
        assert "low / medium / high / xhigh / max" in title

        choice_list = modal.query_one("#choice-list")
        choice_list.index = 1
        await pilot.press("right")
        await pilot.pause()

        row = choice_list.children[1].query_one(Label)
        assert "effort: high" in str(row.render())
        assert app.status_refreshes == 1

    reloaded = ModelManager(tmp_path)
    assert reloaded.get_current_model().reasoning_effort == "high"


def test_chat_client_uses_configured_reasoning_effort():
    raw_client = Mock()
    raw_client.chat.completions.create.return_value = object()
    client = ChatAPIClient(raw_client, "test-model", "xhigh")

    client.generate([{"role": "user", "content": "hello"}])

    assert raw_client.chat.completions.create.call_args.kwargs["reasoning_effort"] == "xhigh"


@pytest.mark.anyio
async def test_async_chat_client_uses_configured_reasoning_effort():
    raw_client = Mock()
    raw_client.chat.completions.create = AsyncMock(return_value=object())
    client = AsyncChatAPIClient(raw_client, "test-model", "max")

    await client.generate([{"role": "user", "content": "hello"}])

    assert raw_client.chat.completions.create.call_args.kwargs["reasoning_effort"] == "max"


def test_llm_client_cache_changes_when_reasoning_effort_changes():
    medium_model = ModelConfig("https://example.com", "key", "same", reasoning_effort="medium")
    high_model = ModelConfig("https://example.com", "key", "same", reasoning_effort="high")
    created_clients = [Mock(), Mock()]

    llm_client_module._cached_llm_client = None
    llm_client_module._cached_model_key = None
    with patch("utils.llm_client.get_current_model_config", side_effect=[medium_model, high_model]), \
            patch("utils.llm_client._create_chat_client", side_effect=created_clients) as create_client:
        assert llm_client_module._create_llm_client() is created_clients[0]
        assert llm_client_module._create_llm_client() is created_clients[1]

    assert create_client.call_count == 2


def test_sync_llm_client_has_bounded_timeout_and_retries():
    model = ModelConfig("https://example.com", "key", "main")

    client = llm_client_module._create_chat_client(model)
    try:
        assert client.client.timeout.connect == 10
        assert client.client.timeout.read == 120
        assert client.client.max_retries == 2
    finally:
        client.client.close()


@pytest.mark.anyio
async def test_async_llm_client_has_bounded_timeout_and_retries():
    model = ModelConfig("https://example.com", "key", "main")

    client = llm_client_module._create_async_chat_client(model)
    try:
        assert client.client.timeout.connect == 10
        assert client.client.timeout.read == 120
        assert client.client.max_retries == 2
    finally:
        await client.client.close()


def test_runtime_info_displays_current_reasoning_effort():
    model = ModelConfig("https://example.com", "key", "main", reasoning_effort="high")

    with patch("system.models.get_current_model_config", return_value=model), \
            patch("utils.hitl.get_hitl_status", return_value=True), \
            patch("utils.plan_mode.is_plan_mode", return_value=False):
        runtime_info = console_render.format_runtime_info()

    assert "Model: main (example.com) · Effort: high" in runtime_info
