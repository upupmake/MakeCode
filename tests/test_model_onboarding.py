"""首次使用链路：服务商预设、环境变量回退、测试连接、启动引导。"""
import asyncio
import threading
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Label, Select

import main as main_module
import system.tui_app as tui_app
from system import simple_management
from system.commands import CommandHandler
from system.models import ModelConfig, ModelManager, get_provider_preset
from system.simple_render import SimpleRenderer
from system.tui_app import MakeCodeTuiApp
from system.tui_modals import (
    AddModelModal,
    EditModelModal,
    ModelManagerModal,
    StartupWorkdirModal,
)
from utils import llm_client


class ModalHost(App):
    def __init__(self, modal, on_dismiss=None):
        super().__init__()
        self._modal = modal
        self._on_dismiss = on_dismiss

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._on_dismiss)

    def refresh_status(self) -> None:
        pass


def _hint(modal) -> str:
    return str(modal.query_one("#model-form-hint", Label).render())


def test_provider_presets_expose_defaults_and_env_key(monkeypatch):
    openai = get_provider_preset("openai")
    anthropic = get_provider_preset("anthropic")

    assert (openai.base_url, openai.message_format) == ("https://api.openai.com/v1", "openai_chat")
    assert (anthropic.base_url, anthropic.message_format) == ("https://api.anthropic.com", "anthropic")
    assert get_provider_preset("custom") is None
    assert get_provider_preset(None) is None

    monkeypatch.setenv("OPENAI_API_KEY", "  sk-env  ")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert openai.api_key_from_env() == "sk-env"
    assert anthropic.api_key_from_env() == ""


def _fake_client(stream, closed):
    return SimpleNamespace(
        generate_stream=stream,
        client=SimpleNamespace(close=lambda: closed.append("client")),
    )


def _stream(events=(), error=None, hang=False, closed=None):
    async def generate(messages, tools=None):
        try:
            if hang:
                await asyncio.sleep(30)
            if error is not None:
                raise error
            for event in events:
                yield event
        finally:
            closed.append("stream")

    return generate


class StatusError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.anyio
async def test_check_model_connection_succeeds_on_first_event_and_closes_resources():
    closed = []
    stream = _stream([{"type": "text", "content": "pong"}, {"type": "done"}], closed=closed)
    config = ModelConfig("https://example.com", "key", "model-a")

    with patch.object(llm_client, "_create_async_chat_client", return_value=_fake_client(stream, closed)) as create:
        ok, message = await llm_client.check_model_connection(config)

    assert ok is True
    assert message.startswith("连接成功")
    create.assert_called_once_with(config, max_retries=0)
    assert closed == ["stream", "client"]


@pytest.mark.anyio
async def test_check_model_connection_reports_http_status_hint():
    closed = []
    stream = _stream(error=StatusError(401, "Incorrect API key provided"), closed=closed)

    with patch.object(llm_client, "_create_async_chat_client", return_value=_fake_client(stream, closed)):
        ok, message = await llm_client.check_model_connection(ModelConfig("https://example.com", "bad", "m"))

    assert ok is False
    assert "API Key 无效" in message
    assert "HTTP 401" in message
    assert "Incorrect API key provided" in message
    assert closed == ["stream", "client"]


@pytest.mark.anyio
async def test_check_model_connection_times_out_without_raising():
    closed = []
    stream = _stream(hang=True, closed=closed)

    with patch.object(llm_client, "_create_async_chat_client", return_value=_fake_client(stream, closed)):
        ok, message = await llm_client.check_model_connection(
            ModelConfig("https://example.com", "key", "m"),
            timeout=0.05,
        )

    assert ok is False
    assert message.startswith("连接超时")
    assert closed == ["stream", "client"]


@pytest.mark.anyio
async def test_check_model_connection_reports_client_creation_failure():
    with patch.object(llm_client, "_create_async_chat_client", side_effect=ValueError("bad url")):
        ok, message = await llm_client.check_model_connection(ModelConfig("x", "key", "m"))

    assert ok is False
    assert "ValueError" in message
    assert "bad url" in message


@pytest.mark.anyio
async def test_check_model_connection_sync_works_inside_running_event_loop():
    async def fake_check(model_config, *, timeout):
        return True, f"ok {model_config.model_id} {timeout}"

    with patch.object(llm_client, "check_model_connection", fake_check):
        result = llm_client.check_model_connection_sync(ModelConfig("x", "key", "m"), timeout=3.0)

    assert result == (True, "ok m 3.0")


@pytest.mark.parametrize("message_format", ["openai_chat", "openai_responses", "anthropic"])
def test_chat_client_retries_default_to_global_and_can_be_overridden(message_format):
    config = ModelConfig("https://example.com", "key", "m", message_format=message_format)

    default_client = llm_client._create_async_chat_client(config)
    no_retry_client = llm_client._create_async_chat_client(config, max_retries=0)

    assert default_client.client.max_retries == llm_client._LLM_MAX_RETRIES
    assert no_retry_client.client.max_retries == 0


@pytest.mark.anyio
async def test_add_model_modal_preset_fills_format_url_and_env_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    results = []
    modal = AddModelModal()
    app = ModalHost(modal, results.append)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        modal.query_one("#model-preset", Select).value = "openai"
        await pilot.pause()

        assert modal.query_one("#model-message-format", Select).value == "openai_chat"
        assert modal.query_one("#model-base-url", Input).value == "https://api.openai.com/v1"
        assert modal.query_one("#model-api-key", Input).value == "sk-env"
        assert "OPENAI_API_KEY" in _hint(modal)

        modal.query_one("#model-ids", Input).value = "gpt-x"
        modal.action_submit()
        await pilot.pause()

    assert results == [{
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-env",
        "model_input": "gpt-x",
        "alias_input": "",
        "message_format": "openai_chat",
    }]


@pytest.mark.anyio
async def test_add_model_modal_preset_without_env_key_asks_for_manual_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    modal = AddModelModal()
    app = ModalHost(modal)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        modal.query_one("#model-preset", Select).value = "anthropic"
        await pilot.pause()

        assert modal.query_one("#model-message-format", Select).value == "anthropic"
        assert modal.query_one("#model-base-url", Input).value == "https://api.anthropic.com"
        assert modal.query_one("#model-api-key", Input).value == ""
        assert "未检测到环境变量 ANTHROPIC_API_KEY" in _hint(modal)


@pytest.mark.anyio
async def test_add_model_modal_preset_keeps_api_key_already_typed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    modal = AddModelModal()
    app = ModalHost(modal)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        modal.query_one("#model-api-key", Input).value = "typed-key"
        modal.query_one("#model-preset", Select).value = "openai"
        await pilot.pause()

        assert modal.query_one("#model-api-key", Input).value == "typed-key"
        assert modal.query_one("#model-base-url", Input).value == "https://api.openai.com/v1"


async def _press_test_button(pilot, app, modal):
    modal.query_one("#model-test", Button).press()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.anyio
async def test_add_model_modal_test_button_checks_first_model_and_shows_result():
    calls = []

    async def fake_check(model_config, **kwargs):
        calls.append((model_config.base_url, model_config.api_key, model_config.model_id, model_config.message_format))
        return True, "连接成功（0.1s）"

    modal = AddModelModal()
    app = ModalHost(modal)
    with patch.object(llm_client, "check_model_connection", fake_check):
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            modal.query_one("#model-base-url", Input).value = "https://example.com"
            modal.query_one("#model-api-key", Input).value = "key"
            modal.query_one("#model-ids", Input).value = "，model-a, model-b"
            modal.query_one("#model-message-format", Select).value = "anthropic"
            await _press_test_button(pilot, app, modal)

            assert calls == [("https://example.com", "key", "model-a", "anthropic")]
            assert _hint(modal) == "✅ 连接成功（0.1s）"
            assert not modal.query_one("#model-test", Button).disabled


@pytest.mark.anyio
async def test_add_model_modal_test_button_requires_filled_fields():
    fake_check = Mock()
    modal = AddModelModal()
    app = ModalHost(modal)
    with patch.object(llm_client, "check_model_connection", fake_check):
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            modal.query_one("#model-base-url", Input).value = "https://example.com"
            await _press_test_button(pilot, app, modal)

            assert "请先填写" in _hint(modal)
            assert not modal.query_one("#model-test", Button).disabled
    fake_check.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ((False, "API Key 无效（HTTP 401）"), "❌ API Key 无效（HTTP 401）"),
        ((False, "bad [/x] [bold]"), "❌ bad [/x] [bold]"),
        (RuntimeError("boom"), "❌ 测试失败：boom"),
    ],
)
async def test_edit_model_modal_test_button_shows_failures_without_crashing(tmp_path, outcome, expected):
    manager = ModelManager(tmp_path)
    manager.add_model("https://example.com", "key", ["main"])
    seen = []

    async def fake_check(model_config, **kwargs):
        seen.append(model_config.model_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    modal = EditModelModal(manager.models[0])
    app = ModalHost(modal)
    with patch.object(llm_client, "check_model_connection", fake_check):
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await _press_test_button(pilot, app, modal)

            assert seen == ["main"]
            assert _hint(modal) == expected
            assert app.screen is modal
            assert not modal.query_one("#model-test", Button).disabled


@pytest.mark.anyio
async def test_model_manager_auto_add_opens_add_dialog_and_closes_after_first_model(tmp_path):
    manager = ModelManager(tmp_path)
    results = []
    modal = ModelManagerModal(manager, auto_add=True)
    app = ModalHost(modal, results.append)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        add_modal = app.screen
        assert isinstance(add_modal, AddModelModal)

        add_modal.query_one("#model-base-url", Input).value = "https://example.com"
        add_modal.query_one("#model-api-key", Input).value = "key"
        add_modal.query_one("#model-ids", Input).value = "main"
        add_modal.action_submit()
        await pilot.pause()
        await pilot.pause()

    assert results == ["exit"]
    assert [model.model_id for model in manager.models] == ["main"]


@pytest.mark.anyio
async def test_model_manager_auto_add_stays_open_when_add_dialog_is_cancelled(tmp_path):
    manager = ModelManager(tmp_path)
    results = []
    modal = ModelManagerModal(manager, auto_add=True)
    app = ModalHost(modal, results.append)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, AddModelModal)
        app.screen.action_cancel()
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal
    assert results == []
    assert manager.models == []


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["models_exist", "unreadable_config"])
async def test_model_manager_auto_add_is_skipped_when_guide_is_not_applicable(tmp_path, scenario):
    manager = ModelManager(tmp_path)
    if scenario == "models_exist":
        manager.add_model("https://example.com", "key", ["main"])
    else:
        manager.config_file.write_text("{not json", encoding="utf-8")
    modal = ModelManagerModal(manager, auto_add=True)
    app = ModalHost(modal)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal


@pytest.mark.anyio
async def test_startup_models_handler_runs_once_in_worker_thread_after_mount():
    calls = []
    app = MakeCodeTuiApp(startup_models_handler=lambda: calls.append(threading.current_thread()))

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(calls) == 1
        assert calls[0] is not threading.main_thread()
        assert app._modal_active is False
        assert app._startup_models_handler is None
        assert app.query_one("#input-box").has_focus


@pytest.mark.anyio
async def test_startup_models_handler_waits_for_workdir_modal(tmp_path):
    order = []
    app = MakeCodeTuiApp(
        startup_workdir_provider=lambda: tmp_path,
        startup_workdir_handler=lambda choice: order.append(("workdir", choice)),
        startup_models_handler=lambda: order.append(("models",)),
    )

    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, StartupWorkdirModal):
                break
        assert isinstance(app.screen, StartupWorkdirModal)
        assert order == []

        app.screen.dismiss("abort")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert order == [("workdir", "abort"), ("models",)]


@pytest.mark.anyio
async def test_app_without_startup_models_handler_just_focuses_input():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()

        assert app._modal_active is False
        assert app.query_one("#input-box").has_focus


@pytest.mark.anyio
async def test_first_run_guide_end_to_end_through_the_real_bridge(tmp_path):
    manager = ModelManager(tmp_path)
    outcome = []
    app = MakeCodeTuiApp(
        startup_models_handler=lambda: outcome.append(tui_app.guide_first_model_tui(manager)),
    )

    with patch.object(tui_app, "_SIMPLE_BACKEND", None):
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, AddModelModal):
                    break
            add_modal = app.screen
            assert isinstance(add_modal, AddModelModal)
            assert app._modal_active is True

            add_modal.query_one("#model-preset", Select).value = "anthropic"
            await pilot.pause()
            add_modal.query_one("#model-api-key", Input).value = "key"
            add_modal.query_one("#model-ids", Input).value = "claude-x"
            add_modal.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert outcome == ["exit"]
            assert app._modal_active is False
            assert app.query_one("#input-box").has_focus

    model = manager.models[0]
    assert (model.base_url, model.message_format, model.model_id) == (
        "https://api.anthropic.com", "anthropic", "claude-x",
    )


def test_handle_models_guided_uses_first_model_guide(tmp_path):
    manager = ModelManager(tmp_path)
    handler = object.__new__(CommandHandler)
    handler.console = Mock()

    with patch("system.commands.get_model_manager", return_value=manager), \
            patch("system.commands.guide_first_model_tui", return_value="exit") as guide, \
            patch("system.commands.manage_models_tui") as manage:
        assert handler.handle_models(guided=True)

    guide.assert_called_once_with(manager)
    manage.assert_not_called()


def test_handle_models_default_still_opens_the_manager(tmp_path):
    manager = ModelManager(tmp_path)
    handler = object.__new__(CommandHandler)
    handler.console = Mock()

    with patch("system.commands.get_model_manager", return_value=manager), \
            patch("system.commands.guide_first_model_tui") as guide, \
            patch("system.commands.manage_models_tui", return_value="exit") as manage:
        assert handler.handle_models()

    manage.assert_called_once_with(manager)
    guide.assert_not_called()


def test_guide_first_model_tui_dispatches_by_backend(tmp_path):
    manager = ModelManager(tmp_path)
    backend = Mock()
    backend.guide_first_model.return_value = "exit"

    with patch.object(tui_app, "_SIMPLE_BACKEND", backend):
        assert tui_app.guide_first_model_tui(manager) == "exit"
    backend.guide_first_model.assert_called_once_with(manager)

    with patch.object(tui_app, "_SIMPLE_BACKEND", None), \
            patch.object(tui_app.TUI_BRIDGE, "manage_models", return_value="exit") as manage:
        assert tui_app.guide_first_model_tui(manager) == "exit"
    manage.assert_called_once_with(manager, auto_add=True)


@pytest.mark.parametrize(
    ("manager_state", "expected"),
    [("unconfigured", True), ("configured", False), ("missing", False)],
)
def test_model_setup_needed_only_when_no_model_is_configured(tmp_path, manager_state, expected):
    manager = ModelManager(tmp_path)
    if manager_state == "configured":
        manager.add_model("https://example.com", "key", ["main"])
    if manager_state == "missing":
        manager = None

    with patch.object(main_module, "get_model_manager", return_value=manager):
        assert main_module._model_setup_needed() is expected


@pytest.mark.parametrize("needed", [True, False])
def test_textual_main_wires_startup_models_handler_only_when_needed(needed):
    command_handler = Mock()
    captured = {}

    class FakeTuiApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    with patch.object(main_module, "MakeCodeTuiApp", FakeTuiApp), \
            patch.object(main_module, "_model_setup_needed", return_value=needed):
        main_module._run_textual_main([], command_handler, prompt_for_workdir=False)

    if needed:
        captured["startup_models_handler"]()
        command_handler.handle_models.assert_called_once_with(guided=True)
    else:
        assert captured["startup_models_handler"] is None


@pytest.mark.parametrize("needed", [True, False])
def test_simple_main_guides_model_setup_before_first_prompt(needed):
    order = []
    command_handler = Mock()
    command_handler.handle_models.side_effect = lambda guided=False: order.append("guide") or True

    def fake_read_input(*args, **kwargs):
        order.append("input")
        raise EOFError

    with patch.object(main_module, "_model_setup_needed", return_value=needed), \
            patch.object(main_module, "read_simple_input", side_effect=fake_read_input), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "_update_terminal_title"), \
            patch.object(main_module.cli_module, "STARTUP_LOAD_REQUESTED", False), \
            patch("signal.signal"):
        main_module._run_simple_main([], command_handler, prompt_for_workdir=False)

    assert order == (["guide", "input"] if needed else ["input"])


def _make_renderer():
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    return SimpleRenderer(console=console), output


def _feed_inputs(monkeypatch, values):
    iterator = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(iterator))


def test_simple_guide_adds_preset_model_with_env_key_and_tests_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    renderer, output = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, ["openai", "", "", "", "gpt-x", ""])
    checked = []

    def fake_check(model_config, **kwargs):
        checked.append(model_config.model_id)
        return True, "连接成功（0.1s）"

    with patch.object(llm_client, "check_model_connection_sync", fake_check):
        result = simple_management.guide_first_model(renderer, manager)

    assert result == "exit"
    model = manager.models[0]
    assert (model.base_url, model.api_key, model.message_format, model.model_id) == (
        "https://api.openai.com/v1", "sk-env", "openai_chat", "gpt-x",
    )
    assert checked == ["gpt-x"]
    assert "✅ 连接成功" in output.getvalue()


def test_simple_guide_without_env_key_prompts_manually_and_reports_failed_check(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    renderer, output = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, ["anthropic", "", "", "typed-key", "claude-x", ""])

    with patch.object(llm_client, "check_model_connection_sync", return_value=(False, "API Key 无效（HTTP 401）")):
        result = simple_management.guide_first_model(renderer, manager)

    assert result == "exit"
    model = manager.models[0]
    assert (model.base_url, model.api_key, model.message_format) == (
        "https://api.anthropic.com", "typed-key", "anthropic",
    )
    assert "未检测到环境变量 ANTHROPIC_API_KEY" in output.getvalue()
    assert "❌ API Key 无效（HTTP 401）" in output.getvalue()


@pytest.mark.parametrize(
    ("answers", "expected_notice"),
    [
        (["nope"], "未知预设：nope"),
        (["custom", "", ""], "Base URL 不能为空"),
    ],
)
def test_simple_guide_cancels_without_adding_or_testing(tmp_path, monkeypatch, answers, expected_notice):
    renderer, output = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, answers)

    with patch.object(llm_client, "check_model_connection_sync") as check:
        result = simple_management.guide_first_model(renderer, manager)

    assert result == "<cancelled>"
    assert manager.models == []
    check.assert_not_called()
    assert expected_notice in output.getvalue()


def test_simple_guide_reports_unreadable_config(tmp_path):
    renderer, output = _make_renderer()
    manager = ModelManager(tmp_path)
    manager.config_file.write_text("{not json", encoding="utf-8")

    assert simple_management.guide_first_model(renderer, manager) == "<cancelled>"
    assert "无法读取模型配置文件" in output.getvalue()


def test_simple_manage_models_adds_with_preset_then_tests_selected_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    renderer, _ = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, ["a openai", "", "", "", "gpt-x", "", "t 1", "q"])
    checked = []

    def fake_check(model_config, **kwargs):
        checked.append(model_config.model_id)
        return True, "连接成功（0.1s）"

    with patch.object(llm_client, "check_model_connection_sync", fake_check):
        result = simple_management.manage_models(renderer, manager)

    assert result == "exit"
    assert manager.models[0].api_key == "sk-env"
    assert manager.models[0].base_url == "https://api.openai.com/v1"
    assert checked == ["gpt-x"]


def test_simple_preset_keeps_typed_api_key_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    renderer, _ = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, ["a openai", "", "", "typed-key", "gpt-x", "", "q"])

    simple_management.manage_models(renderer, manager)

    assert manager.models[0].api_key == "typed-key"


def test_simple_manage_models_rejects_unknown_preset(tmp_path, monkeypatch):
    renderer, output = _make_renderer()
    manager = ModelManager(tmp_path)
    _feed_inputs(monkeypatch, ["a nope", "q"])

    simple_management.manage_models(renderer, manager)

    assert manager.models == []
    assert "未知预设：nope" in output.getvalue()
