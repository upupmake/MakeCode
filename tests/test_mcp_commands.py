from unittest.mock import Mock

import pytest
from rich.console import Console

from system.commands import COMMAND_DESCRIPTIONS, CommandHandler, _checkpoint_preview, _task_plan_preview
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


def test_checkpoint_preview_only_contains_user_messages():
    preview = _checkpoint_preview([
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ])

    assert "first question" in preview.plain
    assert "second question" in preview.plain
    assert "system prompt" not in preview.plain
    assert "first answer" not in preview.plain


def test_task_plan_preview_contains_summary_and_tasks():
    preview = _task_plan_preview({
        "epic_id": "abcd1234",
        "tasks": {
            "1": {"subject": "Inspect code", "status": "completed", "depend_on": []},
            "2": {"subject": "Add preview", "status": "pending", "depend_on": ["1"]},
        },
    })
    console = Console(record=True, width=100)
    console.print(preview)
    rendered = console.export_text()

    assert "任务总数: 2" in rendered
    assert "已完成: 1" in rendered
    assert "Inspect code" in rendered
    assert "Add preview" in rendered
    assert "1" in rendered


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


def test_load_cannot_delete_current_checkpoint(tmp_path, monkeypatch):
    checkpoint = tmp_path / "ckpt_20260715_120000_abcd1234.json"
    checkpoint.write_text("[]", encoding="utf-8")
    handler = make_handler()
    handler.console = Mock()
    handler.list_checkpoints = lambda: [checkpoint]

    def choose_checkpoint(checkpoints, title=None, delete_handler=None, preview_handler=None):
        assert checkpoints == [checkpoint]
        assert delete_handler is not None
        assert preview_handler is not None
        with pytest.raises(ValueError, match="当前 checkpoint 正在使用"):
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
    assert current_checkpoint == checkpoint
    assert checkpoint.exists()


def test_load_task_plan_list_cannot_delete_current_plan(tmp_path, monkeypatch):
    checkpoint = tmp_path / "ckpt_20260715_120000_abcd1234.json"
    checkpoint.write_text("[]", encoding="utf-8")
    current_plan = tmp_path / "task_plan_current_abcd1234.json"
    current_plan.write_text("{}", encoding="utf-8")
    other_plan = tmp_path / "task_plan_other_efgh5678.json"
    other_plan.write_text("{}", encoding="utf-8")
    handler = make_handler()
    handler.console = Mock()
    handler.list_checkpoints = lambda: [checkpoint]
    handler.load_checkpoint = lambda path: [{"role": "system", "content": "system"}]
    chooser_calls = 0

    def choose_checkpoint(checkpoints, title=None, delete_handler=None, preview_handler=None):
        nonlocal chooser_calls
        chooser_calls += 1
        assert preview_handler is not None
        if chooser_calls == 1:
            return str(checkpoint)
        assert checkpoints == [current_plan, other_plan]
        assert "Select a Task Plan to Load" in title
        assert delete_handler is not None
        with pytest.raises(ValueError, match="当前 Task Plan 正在使用"):
            delete_handler(current_plan)
        delete_handler(other_plan)
        return "abort"

    reset_task_plan = Mock()
    monkeypatch.setattr("system.commands.interactive_choose_checkpoint", choose_checkpoint)
    monkeypatch.setattr("system.commands.list_task_plans", lambda: [current_plan, other_plan])
    monkeypatch.setattr("utils.tasks.TASK_MANAGER", Mock(path=current_plan))
    monkeypatch.setattr("system.commands.refresh_task_workspace_paths", reset_task_plan)
    monkeypatch.setattr("system.commands.render_current_task_plan", lambda console: None)

    loaded_history, loaded_checkpoint = handler.handle_load(
        [{"role": "system", "content": "system"}],
        checkpoint,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    assert loaded_history == [{"role": "system", "content": ""}]
    assert loaded_checkpoint == checkpoint
    assert current_plan.exists()
    assert not other_plan.exists()
    reset_task_plan.assert_called_once_with()


def test_load_without_saved_task_plans_resets_current_plan(tmp_path, monkeypatch):
    checkpoint = tmp_path / "ckpt_20260715_120000_abcd1234.json"
    checkpoint.write_text("[]", encoding="utf-8")
    handler = make_handler()
    handler.console = Mock()
    handler.list_checkpoints = lambda: [checkpoint]
    handler.load_checkpoint = lambda path: [{"role": "system", "content": "system"}]
    reset_task_plan = Mock()

    monkeypatch.setattr(
        "system.commands.interactive_choose_checkpoint",
        lambda checkpoints, title=None, delete_handler=None, preview_handler=None: str(checkpoint),
    )
    monkeypatch.setattr("system.commands.list_task_plans", lambda: [])
    monkeypatch.setattr("system.commands.refresh_task_workspace_paths", reset_task_plan)
    monkeypatch.setattr("system.commands.render_current_task_plan", lambda console: None)

    loaded_history, loaded_checkpoint = handler.handle_load(
        [{"role": "system", "content": "system"}],
        checkpoint,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    assert loaded_history == [{"role": "system", "content": ""}]
    assert loaded_checkpoint == checkpoint
    reset_task_plan.assert_called_once_with()


def test_load_selected_task_plan_does_not_reset_it(tmp_path, monkeypatch):
    checkpoint = tmp_path / "ckpt_20260715_120000_abcd1234.json"
    checkpoint.write_text("[]", encoding="utf-8")
    task_plan = tmp_path / "task_plan_selected_abcd1234.json"
    task_plan.write_text("{}", encoding="utf-8")
    handler = make_handler()
    handler.console = Mock()
    handler.list_checkpoints = lambda: [checkpoint]
    handler.load_checkpoint = lambda path: [{"role": "system", "content": "system"}]
    chooser_calls = 0

    def choose_checkpoint(checkpoints, title=None, delete_handler=None, preview_handler=None):
        nonlocal chooser_calls
        chooser_calls += 1
        assert preview_handler is not None
        return str(checkpoint) if chooser_calls == 1 else str(task_plan)

    load_selected_plan = Mock(return_value={"tasks": {}})
    reset_task_plan = Mock()
    monkeypatch.setattr("system.commands.interactive_choose_checkpoint", choose_checkpoint)
    monkeypatch.setattr("system.commands.list_task_plans", lambda: [task_plan])
    monkeypatch.setattr("system.commands.load_task_plan", load_selected_plan)
    monkeypatch.setattr("system.commands.refresh_task_workspace_paths", reset_task_plan)
    monkeypatch.setattr("system.commands.render_current_task_plan", lambda console: None)

    loaded_history, loaded_checkpoint = handler.handle_load(
        [{"role": "system", "content": "system"}],
        checkpoint,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    assert loaded_history == [{"role": "system", "content": ""}]
    assert loaded_checkpoint == checkpoint
    load_selected_plan.assert_called_once_with(task_plan)
    reset_task_plan.assert_not_called()


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


@pytest.mark.anyio
async def test_tui_displays_invalid_rich_markup_as_plain_text():
    app = MakeCodeTuiApp()
    payload = "Expected markup value (found '=True, width=80) for s in samples]`')"

    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, payload))
        await pilot.pause()

        content_log = app._logs[TuiRegion.CONTENT]
        rendered = "\n".join(line.text for line in content_log.lines)
        assert payload in rendered


@pytest.mark.anyio
async def test_long_tool_result_scrolls_to_result_start():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.handle_tui_event(TuiEvent(TuiRegion.TOOLS, "tool call"))
        await pilot.pause()
        tools_log = app._logs[TuiRegion.TOOLS]
        result_start_y = len(tools_log.lines)

        app.handle_tui_event(
            TuiEvent(
                TuiRegion.TOOLS,
                "\n".join(f"result line {index}" for index in range(20)),
                tool_result_delta=1,
            )
        )
        await pilot.pause()
        await pilot.pause()

        assert tools_log.scroll_y == result_start_y
        assert tools_log.scroll_y < tools_log.max_scroll_y


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
