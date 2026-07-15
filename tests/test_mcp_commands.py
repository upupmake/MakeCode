from unittest.mock import Mock

import pytest
from rich.console import Console

from system.commands import COMMAND_DESCRIPTIONS, CommandHandler
from system.tui_app import MakeCodeInput, MakeCodeTuiApp
from system.tui_types import TuiEvent, TuiRegion
from utils.mcp_manager import GlobalMCPManager


class DummyMcpManager:
    def add_server_config(self, server_name, cfg):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def delete_server_config(self, server_name):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def get_status_info(self):
        return {"config_path": "test"}


def make_handler():
    return CommandHandler(
        Console(),
        DummyMcpManager(),
        skill_loader=None,
        get_system_prompt_fn=lambda: "",
        save_checkpoint_fn=lambda history, checkpoint: checkpoint,
        load_checkpoint_fn=lambda path: None,
        list_checkpoints_fn=lambda: [],
        auto_compact_fn=lambda *args, **kwargs: None,
    )


def test_mcp_help_command_registered():
    assert COMMAND_DESCRIPTIONS["/mcp-help"] == "显示 MCP 相关命令介绍。"


def test_flush_command_registered_and_does_not_run_agent(monkeypatch):
    handler = make_handler()
    flushed = []
    monkeypatch.setattr("system.commands.flush_tui_screen", lambda: flushed.append(True))

    result = handler.process_command(
        "/flush",
        history=[],
        current_checkpoint=None,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda history: None,
    )

    assert COMMAND_DESCRIPTIONS["/flush"] == "完整刷新 TUI 屏幕，不改变任何面板中已有的内容。"
    assert result.action.name == "CONTINUE"
    assert flushed == [True]


def test_flush_screen_clears_terminal_and_requests_full_repaint():
    app = MakeCodeTuiApp.__new__(MakeCodeTuiApp)
    app._driver = Mock()
    app.refresh = Mock()

    app.flush_screen()

    app._driver.write.assert_called_once_with("\x1b[2J\x1b[H")
    app._driver.flush.assert_called_once_with()
    app.refresh.assert_called_once_with(repaint=True, layout=True)


def test_load_can_delete_current_checkpoint_and_clear_binding(tmp_path, monkeypatch):
    checkpoint = tmp_path / "ckpt_20260715_120000_abcd1234.json"
    checkpoint.write_text("[]", encoding="utf-8")
    handler = make_handler()
    handler.console = Mock()
    handler.list_checkpoints = lambda: [checkpoint]

    def choose_checkpoint(checkpoints, title=None, delete_handler=None):
        assert checkpoints == [checkpoint]
        assert delete_handler is not None
        delete_handler(checkpoint)
        return "abort"

    monkeypatch.setattr("system.commands.interactive_choose_checkpoint", choose_checkpoint)

    history = [{"role": "system", "content": "system"}]
    loaded_history, current_checkpoint = handler.handle_load(
        history,
        checkpoint,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    assert loaded_history is history
    assert current_checkpoint is None
    assert not checkpoint.exists()


def test_tasks_command_opens_task_management_panel(monkeypatch):
    manager = Mock()
    manager.get_task_table.return_value = {
        "rows": [
            {
                "id": "7",
                "subject": "Delete me",
                "status": "pending",
                "is_runnable": True,
            }
        ]
    }
    manage_tasks = Mock(return_value="closed")

    monkeypatch.setattr("utils.tasks.TASK_MANAGER", manager)
    monkeypatch.setattr("system.commands.manage_tasks_tui", manage_tasks)

    assert make_handler().handle_task_table() is True
    manage_tasks.assert_called_once_with(manager)


@pytest.mark.anyio
async def test_submitting_flush_preserves_existing_pane_content():
    submitted = []
    app = MakeCodeTuiApp(submit_handler=lambda text: submitted.append(text))

    async with app.run_test() as pilot:
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "existing content"))
        await pilot.pause()
        content_log = app._logs[TuiRegion.CONTENT]
        before = list(content_log.lines)

        input_box = app.query_one("#input-box", MakeCodeInput)
        input_box.load_text("/flush")
        app.submit_current_input()
        await pilot.pause()

        assert list(content_log.lines) == before
        assert submitted == ["/flush"]


def test_parse_mcp_add_stdio_command_parts_and_env():
    handler = make_handler()

    name, cfg = handler._parse_mcp_add_config(
        "/mcp-add MiniMax --env MINIMAX_API_KEY=api_key "
        "--env MINIMAX_API_HOST=https://api.minimaxi.com --keep-alive false "
        "-- uvx minimax-coding-plan-mcp -y"
    )

    assert name == "MiniMax"
    assert cfg == {
        "command": "uvx",
        "args": ["minimax-coding-plan-mcp", "-y"],
        "env": {
            "MINIMAX_API_KEY": "api_key",
            "MINIMAX_API_HOST": "https://api.minimaxi.com",
        },
        "keep_alive": False,
        "disabled": True,
        "transport": "stdio",
    }


def test_parse_mcp_add_remote_headers_and_http_normalization():
    handler = make_handler()

    name, cfg = handler._parse_mcp_add_config(
        "/mcp-add api --url https://example.com/mcp --transport http "
        "--header X-Api-Key=secret headers.Authorization=Bearer-token --auth oauth --timeout 30000"
    )

    assert name == "api"
    assert cfg["transport"] == "streamable-http"
    assert cfg["disabled"] is True
    assert cfg["headers"] == {
        "X-Api-Key": "secret",
        "Authorization": "Bearer-token",
    }
    assert cfg["auth"] == "oauth"
    assert cfg["timeout"] == 30000


def test_parse_mcp_add_rejects_disabled_option():
    handler = make_handler()

    try:
        handler._parse_mcp_add_config("/mcp-add api --url https://example.com/mcp --disabled")
    except ValueError as exc:
        assert "未知参数: --disabled" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_parse_mcp_add_rejects_old_command_arg_syntax():
    handler = make_handler()

    try:
        handler._parse_mcp_add_config("/mcp-add fs --command npx --arg -y")
    except ValueError as exc:
        assert "-- 后的启动命令或 --url" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_parse_mcp_add_requires_command_or_url():
    handler = make_handler()

    try:
        handler._parse_mcp_add_config("/mcp-add missing")
    except ValueError as exc:
        assert "-- 后的启动命令或 --url" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_mcp_manager_rejects_duplicate_server_name(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"

    manager.add_server_config(
        "api",
        {"url": "https://example.com/mcp", "transport": "streamable-http", "disabled": True},
    )

    try:
        manager.add_server_config(
            "api",
            {"url": "https://example.com/other", "transport": "streamable-http", "disabled": True},
        )
    except ValueError as exc:
        assert "请先执行 /mcp-delete api" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_mcp_manager_add_and_delete_disabled_config(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"

    add_result = manager.add_server_config(
        "disabled-api",
        {"url": "https://example.com/mcp", "transport": "streamable-http", "disabled": True},
    )
    assert add_result["saved"] is True
    assert add_result["enabled"] == []
    assert "disabled-api" in manager.read_config()["mcpServers"]

    delete_result = manager.delete_server_config("disabled-api")
    assert delete_result["saved"] is True
    assert "disabled-api" not in manager.read_config()["mcpServers"]
