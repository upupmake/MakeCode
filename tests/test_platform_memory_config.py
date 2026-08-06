import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from rich.text import Text

from system.models import MESSAGE_FORMATS, ModelConfig, ModelManager, REASONING_EFFORTS
from system import console_render, ts_validator, updater, window_attention
from system.commands import CommandAction, CommandHandler, CommandResult
from system.tool_history import TOOL_STATUS_FAILED, ToolExecutionHistory
from system.tui_modals import AddMemoryModal, AddModelModal, ChoiceModal, InfoPanelModal, McpSwitchModal, MemoryConfigModal, MemoryPanelModal, RecallModelPickerModal, LayoutModal, ModelManagerModal, TaskPanelModal
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
    assert memory.set_context_length(300) == 300

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["memory_size"] == 9
    assert saved["memory_recall_window_size"] == 3
    assert saved["context_length"] == 300

    config_file.write_text(json.dumps({
        "memory_size": 11,
        "memory_recall_window_size": 4,
        "context_length": 256,
    }), encoding="utf-8")

    assert memory.get_memory_size() == 11
    assert memory.get_memory_recall_window_size() == 4
    assert memory.get_context_length() == 256
    assert memory.get_context_token_limit() == 256 * 1024
    assert memory.set_memory_recall_window_size(5) == 5

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["memory_size"] == 11
    assert saved["context_length"] == 256
    assert saved["memory_recall_window_size"] == 5


def test_memory_config_modal_includes_global_context_length_field():
    fields = MemoryConfigModal._FIELDS

    assert fields["context_length"]["input_id"] == "memory-config-context-length"
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
        dialog.scroll_end(animate=False)
        await pilot.pause()
        assert dialog.scroll_y == dialog.max_scroll_y
        assert choice_list.region.y < dialog.region.bottom
        assert choice_list.region.bottom > dialog.region.y


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

        service_list.index = len(service_list.children) - 1
        await pilot.pause()
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
        assert "当前运行" in row_text
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

        memory_list.index = len(memory_list.children) - 1
        await pilot.pause()
        assert memory_list.scroll_y > 0


@pytest.mark.anyio
async def test_add_model_modal_returns_explicit_message_format():
    results = []
    modal = AddModelModal()
    app = ChoiceModalHost(modal, results.append)

    async with app.run_test() as pilot:
        modal.query_one("#model-base-url", Input).value = "https://api.anthropic.com"
        modal.query_one("#model-api-key", Input).value = "key"
        modal.query_one("#model-ids", Input).value = "claude-test"
        modal.query_one("#model-message-format", Select).value = "anthropic"
        modal.action_submit()
        await pilot.pause()

    assert results == [{
        "base_url": "https://api.anthropic.com",
        "api_key": "key",
        "model_input": "claude-test",
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
        "claude-test",
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


def test_micro_compact_invalidates_native_snapshot_for_cleared_tool_result():
    messages = [
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
        {"role": "tool", "tool_call_id": "call_old", "name": "Read", "content": "old result"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_new",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"new"}'},
            }],
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "tool_use", "id": "call_new"}],
            },
        },
        {"role": "tool", "tool_call_id": "call_new", "name": "Read", "content": "new result"},
    ]

    with patch.object(memory, "get_keep_recent_tool_call", return_value=1):
        memory.micro_compact(messages)

    assert messages[1]["content"].startswith("[Previous Read result cleared")
    assert "native_blocks" not in messages[0]["message_metadata"]
    assert messages[2]["message_metadata"]["native_blocks"] == [{"type": "tool_use", "id": "call_new"}]


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


@pytest.mark.anyio
async def test_manual_memory_update_strips_private_native_payloads():
    history = [{
        "role": "assistant",
        "content": "answer",
        "message_metadata": {
            "source_format": "anthropic",
            "source_model": "claude-test",
            "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
        },
    }]

    with patch.object(memory, "memory_agent_loop", new_callable=AsyncMock, return_value=[]) as loop:
        await memory.manual_memory_update("remember convention", history)

    conversation_text = loop.await_args.kwargs["conversation_text"]
    assert "private-signature" not in conversation_text
    assert "native_blocks" not in conversation_text
    assert history[0]["message_metadata"]["native_blocks"][0]["signature"] == "private-signature"


@pytest.mark.anyio
async def test_auto_compact_transcript_and_summary_ignore_private_native_payloads(tmp_path):
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

    with patch.object(memory, "TRANSCRIPT_DIR", tmp_path), \
            patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
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

    transcript = next(tmp_path.glob("transcript_*.jsonl")).read_text(encoding="utf-8")
    assert "private-signature" not in transcript
    assert "native_blocks" not in transcript
    conversation_text = memory_loop.await_args.kwargs["conversation_text"]
    assert "private-signature" not in conversation_text
    assert "native_blocks" not in conversation_text
    close_client.assert_awaited_once_with(fake_client)


@pytest.mark.anyio
async def test_auto_compact_clears_old_tool_history_before_memory_agent_and_preserves_new_history(tmp_path):
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
        with patch.object(memory, "TRANSCRIPT_DIR", tmp_path), \
                patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
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
async def test_auto_compact_preserves_tool_execution_history_when_compaction_fails(tmp_path):
    messages = [{"role": "system", "content": "system"}]
    execution_id = memory.TOOL_EXECUTION_HISTORY.start("FileRead", {"path": "old.py"})
    memory.TOOL_EXECUTION_HISTORY.finish(execution_id, "old content")

    try:
        with patch.object(memory, "TRANSCRIPT_DIR", tmp_path), \
                patch.object(memory, "_compact_console"), \
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
async def test_user_request_pre_recall_receives_previous_assistant_content():
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
            patch.object(main_module, "recall_long_term_memories", AsyncMock(return_value={"content": ""})) as recall, \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock), \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query("新的用户请求", history, command_handler)

    assert recall.call_args.args[0] == "新的用户请求"
    assert recall.call_args.kwargs["previous_assistant_content"] == "上一轮 assistant 回复"


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
            patch.object(main_module, "recall_long_term_memories", AsyncMock()) as recall, \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock) as run_agent_loop, \
            patch.object(main_module, "post_tui") as post_tui, \
            patch.object(main_module, "refresh_status"):
        await main_module._process_user_query("/nm 直接处理这个请求", history, command_handler)

    recall.assert_not_awaited()
    run_agent_loop.assert_awaited_once_with(history)
    assert history[-1] == {"role": "user", "content": "直接处理这个请求"}
    post_tui.assert_any_call(
        main_module.TuiRegion.BACKGROUND,
        "[#aaaaaa]🧠 已跳过本次请求的记忆预召回流程。[/#aaaaaa]",
    )


@pytest.mark.anyio
async def test_process_user_query_runs_agent_loop_for_title_detection():
    command_handler = Mock()
    command_handler.process_command = AsyncMock(return_value=CommandResult(
        action=CommandAction.RUN_AGENT,
        payload="hello",
    ))

    with patch.object(main_module, "set_agent_loop_active"), \
            patch.object(main_module, "_ensure_active_conversation"), \
            patch.object(main_module, "recall_long_term_memories", AsyncMock(return_value={"content": ""})), \
            patch.object(main_module, "agent_loop", new_callable=AsyncMock, return_value=True) as run_agent_loop, \
            patch.object(main_module, "generate_title", new_callable=AsyncMock) as generate_title, \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "refresh_status"):
        history = [{"role": "system", "content": "system"}]
        await main_module._process_user_query("hello", history, command_handler)

    run_agent_loop.assert_awaited_once_with(history)
    generate_title.assert_not_awaited()


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

    with patch.object(main_module, "micro_compact"), \
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

    with patch.object(main_module, "micro_compact"), \
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
    with patch.object(main_module, "micro_compact"), \
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
async def test_agent_loop_reads_current_context_limit_for_render_and_compaction():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(current_messages, raw_message):
            current_messages.append(raw_message)

    with patch.object(main_module, "micro_compact"), \
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
            patch.object(main_module, "estimate_tokens", return_value=1500), \
            patch.object(main_module, "get_context_token_limit", side_effect=[2048, 1024]) as get_limit, \
            patch.object(main_module, "auto_compact", new_callable=AsyncMock) as auto_compact, \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "refresh_status"), \
            patch.object(main_module.console, "print"):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert get_limit.call_count == 2
    assert render_token_usage.call_args.kwargs["threshold"] == 2048
    assert "1500 exceeded threshold 1024" in auto_compact.await_args.kwargs["reason"]


@pytest.mark.anyio
async def test_agent_loop_cancel_after_committed_round_skips_auto_compact_check():
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

    with patch.object(main_module, "micro_compact"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", AsyncMock(side_effect=responses)), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens") as estimate_tokens, \
            patch.object(main_module, "auto_compact", new_callable=AsyncMock) as auto_compact, \
            patch.object(main_module, "_render_tool_call"), \
            patch.object(main_module, "_render_tool_output"), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    estimate_tokens.assert_not_called()
    auto_compact.assert_not_awaited()


@pytest.mark.anyio
async def test_agent_loop_resumes_pause_turn_and_marks_unknown_tool_result_as_error():
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

    with patch.object(main_module, "micro_compact"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages") as save_messages, \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "_render_tool_call"), \
            patch.object(main_module, "_render_tool_output"), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "TOOL_EXECUTION_HISTORY", ToolExecutionHistory()) as tool_history, \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    assert len(requests) == 3
    assert requests[1][-1]["stop_reason"] == "pause_turn"
    tool_result = next(message for message in requests[2] if message.get("role") == "tool")
    assert tool_result["name"] == "MissingTool"
    assert tool_result["is_error"] is True
    history_record = tool_history.snapshot()[0]
    assert history_record.source == "orchestrator"
    assert history_record.actor == "Orchestrator"
    assert history_record.status == TOOL_STATUS_FAILED
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

    with patch.object(main_module, "micro_compact"), \
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
        assert client.client.timeout.connect == 10
        assert client.client.timeout.read == 120
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
