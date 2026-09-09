import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from system.commands import CommandHandler
from system.models import ModelConfig, ModelManager
from system.tui_modals import ExtraToolsModal
from tools.extra_tools import (
    get_understand_image_model,
    get_understand_image_model_display_text,
    is_understand_image_enabled,
    set_understand_image_config,
)
from tools import understand_image
from tools.understand_image import load_image_for_understanding, understand_image as understand_image_handler
from utils.llm_client import (
    build_anthropic_request_messages,
    create_image_understanding_llm_client,
    sanitize_openai_messages,
)
from utils.vision import image_media_type_from_bytes


MIN_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeStreamResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size: int = 64 * 1024):
        yield self.content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAsyncClient:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.requested_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, cop, tb):
        return False

    def stream(self, method: str, url: str):
        self.requested_url = url
        return FakeStreamResponse(self.content, self.status_code)


class FakeVisionClient:
    def __init__(self, text: str = "a red square"):
        self.text = text
        self.messages = None
        self.closed = False

    async def generate_stream(self, messages, tools=None):
        self.messages = messages
        yield {
            "type": "done",
            "result": SimpleNamespace(text=self.text),
        }


@pytest.fixture
def png_file(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    path.write_bytes(MIN_PNG)
    monkeypatch.setattr(understand_image, "safe_path", lambda p, tool_name="": Path(p).resolve())
    return path


def test_image_media_type_from_bytes_detects_png():
    assert image_media_type_from_bytes(MIN_PNG) == "image/png"
    assert image_media_type_from_bytes(b"not-an-image") is None


@pytest.mark.anyio
async def test_load_local_image_for_understanding(png_file):
    data, media_type = await load_image_for_understanding(str(png_file))

    assert data == MIN_PNG
    assert media_type == "image/png"


@pytest.mark.anyio
async def test_load_remote_image_for_understanding():
    client = FakeAsyncClient(MIN_PNG)
    with patch.object(understand_image.httpx, "AsyncClient", return_value=client):
        data, media_type = await load_image_for_understanding("https://example.com/photo.png")

    assert data == MIN_PNG
    assert media_type == "image/png"
    assert client.requested_url == "https://example.com/photo.png"


@pytest.mark.anyio
async def test_load_rejects_non_image_bytes(tmp_path, monkeypatch):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(understand_image, "safe_path", lambda p, tool_name="": Path(p).resolve())

    with pytest.raises(ValueError, match="Unsupported image type"):
        await load_image_for_understanding(str(notes))


def test_inline_image_blocks_convert_for_openai_and_anthropic():
    message = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "media_type": "image/png",
                "data": MIN_PNG,
            },
            {"type": "text", "text": "describe this"},
        ],
    }

    openai = sanitize_openai_messages([message])
    _, anthropic = build_anthropic_request_messages([message])

    assert openai[0]["content"][0]["type"] == "image_url"
    assert openai[0]["content"][0]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(MIN_PNG).decode("ascii")
    )
    assert openai[0]["content"][1] == {"type": "text", "text": "describe this"}
    assert anthropic[0]["content"][0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(MIN_PNG).decode("ascii"),
        },
    }
    assert message["content"][0]["data"] == MIN_PNG


@pytest.mark.anyio
async def test_understand_image_uses_dedicated_client_and_returns_text(png_file):
    client = FakeVisionClient("a tiny png")
    with (
        patch.object(understand_image, "is_understand_image_enabled", return_value=True),
        patch.object(understand_image, "create_image_understanding_llm_client", return_value=client),
        patch.object(understand_image, "close_async_llm_client", return_value=None) as close_client,
        patch.object(understand_image, "post_tui"),
    ):
        output = await understand_image_handler("What is this?", str(png_file))

    assert output == "a tiny png"
    assert client.messages[1]["content"][0] == {
        "type": "image",
        "media_type": "image/png",
        "data": MIN_PNG,
    }
    assert client.messages[1]["content"][1]["text"] == "What is this?"
    close_client.assert_awaited_once()


@pytest.mark.anyio
async def test_understand_image_returns_error_when_disabled(png_file):
    with patch.object(understand_image, "is_understand_image_enabled", return_value=False):
        output = await understand_image_handler("What is this?", "sample.png")

    assert "disabled" in output


@pytest.mark.anyio
async def test_understand_image_returns_error_when_no_model_configured(png_file):
    with (
        patch.object(understand_image, "is_understand_image_enabled", return_value=True),
        patch.object(understand_image, "create_image_understanding_llm_client", return_value=None),
    ):
        output = await understand_image_handler("What is this?", str(png_file))

    assert output.startswith("Error: No model configured")


def test_image_understanding_tools_are_hidden_when_disabled():
    import main as main_module

    with (
        patch.object(main_module, "is_understand_image_enabled", return_value=False),
        patch.object(main_module, "format_tools_for_current_model", side_effect=lambda tools: tools),
        patch.object(main_module.GLOBAL_MCP_MANAGER, "get_tools", return_value=[]),
    ):
        names = [
            (tool.get("function") or {}).get("name", tool.get("name"))
            for tool in main_module._get_all_tools_definition()
        ]
    assert "UnderstandImage" not in names

    with (
        patch.object(main_module, "is_understand_image_enabled", return_value=True),
        patch.object(main_module, "format_tools_for_current_model", side_effect=lambda tools: tools),
        patch.object(main_module.GLOBAL_MCP_MANAGER, "get_tools", return_value=[]),
        patch.object(main_module, "is_plan_mode", return_value=True),
    ):
        names = [
            (tool.get("function") or {}).get("name", tool.get("name"))
            for tool in main_module._get_all_tools_definition()
        ]
        plan_names = [
            (tool.get("function") or {}).get("name", tool.get("name"))
            for tool in main_module.get_current_tools_definition()
        ]
    assert "UnderstandImage" in names
    assert "UnderstandImage" in plan_names
    from utils.plan_mode import PLAN_MODE_BLOCKLIST
    assert "UnderstandImage" not in PLAN_MODE_BLOCKLIST


def test_create_image_understanding_llm_client_uses_configured_model_and_falls_back():
    vision_model = ModelConfig("https://example.com", "key", "vision-model")
    current_model = ModelConfig("https://example.com", "key", "current-model")

    mock_manager = Mock()
    mock_manager.get_current_model.return_value = current_model
    with (
        patch("utils.llm_client.get_understand_image_model", return_value=vision_model),
        patch("utils.llm_client.get_model_manager", return_value=mock_manager),
    ):
        client = create_image_understanding_llm_client()
    assert client.model == "vision-model"

    mock_manager2 = Mock()
    mock_manager2.get_current_model.return_value = current_model
    with (
        patch("utils.llm_client.get_understand_image_model", return_value=None),
        patch("utils.llm_client.get_model_manager", return_value=mock_manager2),
    ):
        fallback_client = create_image_understanding_llm_client()
    assert fallback_client.model == "current-model"

    mock_manager3 = Mock()
    mock_manager3.get_current_model.return_value = None
    with (
        patch("utils.llm_client.get_understand_image_model", return_value=None),
        patch("utils.llm_client.get_model_manager", return_value=mock_manager3),
    ):
        assert create_image_understanding_llm_client() is None


def test_extra_tools_persists_understand_image_config(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["main", "vision"])
    vision_key = models[1].key

    with patch("tools.extra_tools.get_model_manager", return_value=manager):
        assert is_understand_image_enabled() is False
        assert set_understand_image_config(True, vision_key)
        assert is_understand_image_enabled() is True
        assert get_understand_image_model().key == vision_key
        assert get_understand_image_model_display_text() == models[1].get_display_text()

    saved = json.loads((tmp_path / "extra_tools.json").read_text(encoding="utf-8"))
    assert saved["UnderstandImage"]["enabled"] is True
    assert saved["UnderstandImage"]["model"]["model_id"] == "vision"
    assert "image_understanding" not in json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))

    with patch("tools.extra_tools.get_model_manager", return_value=manager):
        assert manager.delete_model_by_key(vision_key)
        assert get_understand_image_model() is None
        assert get_understand_image_model_display_text() == "同主模型"
        assert is_understand_image_enabled() is True


def test_extra_tools_migrates_legacy_model_config(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)
    (tmp_path / "model_config.json").write_text(json.dumps({
        "version": 2,
        "last_selected": None,
        "memory_recall_model": None,
        "image_understanding": {
            "enabled": True,
            "model": {
                "base_url": "https://example.com",
                "api_key": "key",
                "model_id": "vision",
                "message_format": "openai_chat",
            },
        },
        "models": [],
    }), encoding="utf-8")

    assert is_understand_image_enabled() is True
    saved = json.loads((tmp_path / "extra_tools.json").read_text(encoding="utf-8"))
    assert saved["UnderstandImage"]["enabled"] is True
    assert saved["UnderstandImage"]["model"]["model_id"] == "vision"
    model_config = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert "image_understanding" not in model_config


def test_extra_tools_keeps_legacy_config_if_migration_save_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)
    (tmp_path / "model_config.json").write_text(json.dumps({
        "version": 2,
        "image_understanding": {"enabled": True, "model": None},
        "models": [],
    }), encoding="utf-8")
    monkeypatch.setattr("tools.extra_tools._save_raw", lambda payload: False)

    assert is_understand_image_enabled() is True
    assert not (tmp_path / "extra_tools.json").exists()
    model_config = json.loads((tmp_path / "model_config.json").read_text(encoding="utf-8"))
    assert model_config["image_understanding"]["enabled"] is True


def test_extra_tools_command_saves_after_choosing_model(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["main", "vision"])
    handler = CommandHandler(
        Mock(),
        mcp_manager=None,
        skill_loader=None,
        get_system_prompt_fn=lambda: "",
        conversation_store=Mock(active_path=None),
        auto_compact_fn=lambda *args, **kwargs: None,
    )
    config_calls = []

    def fake_config_tui(values):
        config_calls.append(dict(values))
        if len(config_calls) == 1:
            return {"enabled": True, "model_key": None, "model_display": "同主模型", "__action": "choose_model"}
        if values.get("__action") == "choose_model":
            raise AssertionError("stale __action leaked into the next config panel")
        return "<closed>"

    vision_index = next(index for index, model in enumerate(manager.models) if model.model_id == "vision")
    with (
        patch("system.commands.get_model_manager", return_value=manager),
        patch("system.commands.manage_extra_tools_tui", side_effect=fake_config_tui),
        patch("system.commands.choose_image_understanding_model_tui", return_value=f"select:{vision_index + 1}"),
        patch("system.commands.refresh_status"),
        patch("tools.extra_tools.get_model_manager", return_value=manager),
    ):
        handler.handle_extra_tools("/extra-tools")

    assert len(config_calls) == 2
    with patch("tools.extra_tools.get_model_manager", return_value=manager):
        assert is_understand_image_enabled() is True
        assert get_understand_image_model().key == models[1].key


def test_extra_tools_config_rolls_back_when_save_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)
    manager = ModelManager(tmp_path)
    models = manager.add_model("https://example.com", "key", ["main", "vision"])
    with (
        patch("tools.extra_tools.get_model_manager", return_value=manager),
        patch("tools.extra_tools._save_raw", return_value=False),
    ):
        assert set_understand_image_config(True, models[1].key) is False
        assert is_understand_image_enabled() is False
        assert get_understand_image_model() is None


@pytest.mark.anyio
async def test_extra_tools_toggle_enables_immediately(tmp_path, monkeypatch):
    from textual.app import App, ComposeResult
    from textual.widgets import Button, Label

    monkeypatch.setattr("tools.extra_tools.extra_tools_config_file", lambda: tmp_path / "extra_tools.json")
    monkeypatch.setattr("tools.extra_tools.paths.install_makecode_dir", lambda: tmp_path)

    class Host(App):
        def __init__(self, modal):
            super().__init__()
            self._modal = modal
            self.result = None

        def compose(self) -> ComposeResult:
            yield Label("host")

        def on_mount(self) -> None:
            self.push_screen(self._modal, lambda value: setattr(self, "result", value))

    modal = ExtraToolsModal({
        "enabled": False,
        "model_key": None,
        "model_display": "同主模型",
    })
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#extra-tools-understand-image").collapsed = False
        await pilot.pause()
        modal.query_one("#extra-tools-toggle", Button).press()
        await pilot.pause()

    assert is_understand_image_enabled() is True
    saved = json.loads((tmp_path / "extra_tools.json").read_text(encoding="utf-8"))
    assert saved["UnderstandImage"]["enabled"] is True
    assert modal._values["enabled"] is True


@pytest.mark.anyio
async def test_extra_tools_understand_image_shows_vision_model_hint():
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class Host(App):
        def __init__(self, modal):
            super().__init__()
            self._modal = modal

        def compose(self) -> ComposeResult:
            yield Label("host")

        def on_mount(self) -> None:
            self.push_screen(self._modal)

    modal = ExtraToolsModal({
        "enabled": False,
        "model_key": None,
        "model_display": "同主模型",
    })
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#extra-tools-understand-image").collapsed = False
        await pilot.pause()
        hint = modal.query_one("#extra-tools-understand-image-hint", Label)
        assert str(hint.render()) == "需指定支持图片输入模型"


@pytest.mark.anyio
async def test_extra_tools_enter_on_choose_model_opens_picker():
    from textual.app import App, ComposeResult
    from textual.widgets import Button, Label

    class Host(App):
        def __init__(self, modal):
            super().__init__()
            self._modal = modal
            self.result = None

        def compose(self) -> ComposeResult:
            yield Label("host")

        def on_mount(self) -> None:
            self.push_screen(self._modal, lambda value: setattr(self, "result", value))

    modal = ExtraToolsModal({
        "enabled": False,
        "model_key": None,
        "model_display": "同主模型",
    })
    app = Host(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal.query_one("#extra-tools-understand-image").collapsed = False
        await pilot.pause()
        modal.query_one("#extra-tools-choose-model", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

    assert isinstance(app.result, dict)
    assert app.result.get("__action") == "choose_model"
    assert app.result.get("enabled") is False
