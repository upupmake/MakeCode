import asyncio
import json
import os
import threading
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Input, Label, Static

from init import resolve_chosen_workdir
from system.commands import COMMAND_DESCRIPTIONS, CommandAction, CommandHandler, _conversation_preview, _task_plan_preview
from system.console_render import _extract_message_text
from system.tui_app import MakeCodeInput, MakeCodeTuiApp
from system.tui_modals import StartupWorkdirModal
from system.tui_types import TuiEvent, TuiRegion
from utils.conversations import ConversationStore, SCHEMA_VERSION, SUB_AGENT_HISTORY_FILE, TASK_PLAN_FILE
from utils.mcp_manager import GlobalMCPManager
from utils.paths import directory_completion_candidates


TEST_LAYOUT_RATIOS = {
    "task": 2,
    "background": 3,
    "sub_agent": 1,
}


@pytest.fixture(autouse=True)
def isolate_tui_layout_config(monkeypatch):
    monkeypatch.setattr("system.tui_app.load_layout_ratios", lambda: dict(TEST_LAYOUT_RATIOS))


class DummyMcpManager:
    def add_server_config(self, server_name, cfg):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def delete_server_config(self, server_name):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def get_status_info(self):
        return {"config_path": "test"}


def make_handler(conversation_store=None):
    store = conversation_store or Mock(active_path=None)
    return CommandHandler(
        Console(),
        DummyMcpManager(),
        skill_loader=None,
        get_system_prompt_fn=lambda: "",
        conversation_store=store,
        auto_compact_fn=lambda *args, **kwargs: None,
    )


def test_mcp_help_command_registered():
    assert COMMAND_DESCRIPTIONS["/mcp-help"] == "显示 MCP 相关命令介绍。"


@pytest.mark.anyio
async def test_handle_compact_keeps_manual_global_compaction_available():
    saved_path = object()
    store = Mock(active_path=None)
    store.save_messages.return_value = saved_path
    handler = make_handler(store)
    handler.console = Mock()
    handler.auto_compact = AsyncMock()
    history = [{"role": "system", "content": "system"}]

    with patch("system.commands.set_agent_loop_active") as set_active, \
            patch("system.commands.reset_memory_recall_windows") as reset_windows, \
            patch("system.commands.refresh_status") as refresh_status:
        result = await handler.handle_compact(
            "/compact keep decisions",
            history,
            current_conversation=None,
        )

    assert result == (True, saved_path)
    handler.auto_compact.assert_awaited_once_with(
        history,
        reason="keep decisions",
        system_prompt_fn=handler.get_system_prompt_fn,
    )
    store.save_messages.assert_called_once_with(history)
    reset_windows.assert_called_once_with()
    refresh_status.assert_called_once_with()
    assert [mock_call.args[0] for mock_call in set_active.call_args_list] == [True, False]


@pytest.mark.anyio
async def test_enabled_skill_slash_command_expands_content_and_builtin_commands_keep_priority():
    skill_loader = Mock()
    skill_loader.get_slash_commands.return_value = {
        "/demo-skill": "demo description",
    }
    skill_loader.expand_slash_command.side_effect = lambda query, reserved: (
        "<skill name=\"demo-skill\">\nloaded demo\n</skill>\n\n"
        f"User: {query}"
        if query.startswith("/demo-skill")
        else None
    )
    handler = make_handler()
    handler.skill_loader = skill_loader

    result = await handler.process_command(
        "/demo-skill 处理这个请求",
        history=[],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert handler.get_slash_completion_commands()["/demo-skill"] == "Skill: demo description"
    assert result.action == CommandAction.RUN_AGENT
    assert result.payload == (
        "<skill name=\"demo-skill\">\nloaded demo\n</skill>\n\n"
        "User: /demo-skill 处理这个请求"
    )
    assert result.original_query == "/demo-skill 处理这个请求"

    handler.handle_cmds = Mock()
    builtin = await handler.process_command(
        "/help",
        history=[],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )
    assert builtin.action == CommandAction.CONTINUE
    handler.handle_cmds.assert_called_once()
    assert skill_loader.expand_slash_command.call_count == 1


def test_nm_command_registered_and_returns_plain_query_without_memory_recall():
    assert "/nm" in COMMAND_DESCRIPTIONS
    handler = make_handler()

    result = asyncio.run(handler.process_command(
        "/nm 请直接处理这个请求",
        history=[],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    ))

    assert result.action == CommandAction.RUN_AGENT
    assert result.payload == "请直接处理这个请求"
    assert result.skip_memory_recall is True


@pytest.mark.anyio
async def test_nm_without_query_shows_usage_and_does_not_run_agent():
    handler = make_handler()
    handler.console = Mock()

    result = await handler.process_command(
        "/nm",
        history=[],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert result.action == CommandAction.CONTINUE
    handler.console.print.assert_called_once()
    assert "用法：/nm <query>" in handler.console.print.call_args.args[0]


def test_mcp_switch_opens_management_panel_when_server_list_is_empty(monkeypatch):
    manager = Mock()
    manager.list_server_switches.return_value = []
    choose_switches = Mock(return_value={
        "action": "cancel",
        "disabled_updates": {},
        "deleted_results": [],
        "added_results": [],
    })
    monkeypatch.setattr("system.commands.interactive_switch_mcp_servers", choose_switches)
    handler = make_handler()
    handler.mcp_manager = manager
    handler.console = Mock()

    assert handler.handle_mcp_switch() is True

    choose_switches.assert_called_once_with([], manager)


def test_mcp_switch_reports_saved_tool_changes_when_server_draft_is_cancelled(monkeypatch):
    manager = Mock()
    manager.list_server_switches.return_value = [{"name": "filesystem"}]
    manager.get_status_info.return_value = {"config_path": "test"}
    choose_switches = Mock(return_value={
        "action": "cancel",
        "disabled_updates": {},
        "deleted_results": [],
        "added_results": [],
        "tool_results": [{
            "server": "filesystem",
            "result": {
                "saved": True,
                "disabled_tools": ["read.file"],
                "message": "saved",
            },
        }],
    })
    monkeypatch.setattr("system.commands.interactive_switch_mcp_servers", choose_switches)
    handler = make_handler()
    handler.mcp_manager = manager
    handler.console = Mock()

    assert handler.handle_mcp_switch() is True

    output = handler.console.print.call_args.args[0]
    assert "已取消本次 MCP 开关草稿修改" in output
    assert "已保存 MCP 工具开关" in output
    assert "filesystem" in output
    assert "read.file" in output
    manager.apply_switches.assert_not_called()


def test_mcp_switch_reports_saved_tool_changes_when_service_apply_fails(monkeypatch):
    manager = Mock()
    manager.list_server_switches.return_value = [{"name": "filesystem"}]
    manager.apply_switches.side_effect = RuntimeError("service apply failed")
    choose_switches = Mock(return_value={
        "action": "confirm",
        "disabled_updates": {"filesystem": False},
        "deleted_results": [],
        "added_results": [],
        "tool_results": [{
            "server": "filesystem",
            "result": {
                "saved": True,
                "disabled_tools": ["read.file"],
                "message": "saved",
            },
        }],
    })
    monkeypatch.setattr("system.commands.interactive_switch_mcp_servers", choose_switches)
    handler = make_handler()
    handler.mcp_manager = manager
    handler.console = Mock()

    assert handler.handle_mcp_switch() is True

    output = handler.console.print.call_args.args[0]
    assert "应用 MCP 开关变更失败" in output
    assert "已保存 MCP 工具开关" in output
    assert "read.file" in output


def test_mcp_view_renders_read_only_status_dashboard(monkeypatch):
    manager = Mock()
    manager.get_status_info.return_value = {
        "config_path": "/project/.makecode/mcp_config.json",
        "is_running": True,
        "tool_count": 2,
        "config_servers": ["filesystem", "remote-api", "disabled-api"],
        "enabled_config_servers": ["filesystem", "remote-api"],
        "disabled_servers": ["disabled-api"],
        "loaded_servers": ["filesystem", "remote-api"],
        "tools": [
            {
                "provider": "filesystem",
                "name": "read_file",
                "description": "读取文件内容",
            },
            {
                "provider": "remote-api",
                "name": "search_docs",
                "description": "搜索远程文档",
            },
        ],
    }
    shown = {}

    def show_panel(summary, tools):
        console = Console(record=True, width=100)
        console.print(summary)
        shown["summary"] = summary
        shown["tools"] = tools
        shown["text"] = console.export_text()
        return "closed"

    monkeypatch.setattr("system.commands.show_mcp_view_tui", show_panel)
    handler = make_handler()
    handler.mcp_manager = manager

    assert handler.handle_mcp_view() is True

    assert "MCP 状态总览" in shown["text"]
    assert "● 运行中" in shown["text"]
    assert "服务配置" in shown["text"]
    assert "3 个  filesystem · remote-api · disabled-api" in shown["text"]
    assert "● 2 个  filesystem · remote-api" in shown["text"]
    assert "○ 1 个  disabled-api" in shown["text"]
    assert "filesystem (工具：启用 1 个 · 停用 0 个)" in shown["text"]
    assert "remote-api (工具：启用 1 个" in shown["text"]
    assert len(shown["tools"]) == 2
    assert {tool["name"] for tool in shown["tools"]} == {"read_file", "search_docs"}


def test_mcp_view_summarizes_enabled_and_disabled_tools_per_loaded_server(monkeypatch):
    manager = Mock()
    manager.get_status_info.return_value = {
        "config_path": "/project/.makecode/mcp_config.json",
        "is_running": True,
        "tool_count": 2,
        "total_tool_count": 3,
        "config_servers": ["filesystem", "remote-api"],
        "enabled_config_servers": ["filesystem", "remote-api"],
        "disabled_servers": [],
        "loaded_servers": ["filesystem", "remote-api"],
        "tools": [
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
            {
                "provider": "remote-api",
                "name": "remote_search",
                "description": "Search documents",
                "disabled": False,
            },
        ],
    }
    shown = {}

    def show_panel(summary, tools):
        console = Console(record=True, width=120)
        console.print(summary)
        shown["tools"] = tools
        shown["text"] = console.export_text()
        return "closed"

    monkeypatch.setattr("system.commands.show_mcp_view_tui", show_panel)
    handler = make_handler()
    handler.mcp_manager = manager

    assert handler.handle_mcp_view() is True

    assert "filesystem (工具：启用 1 个 · 停用 1 个)" in shown["text"]
    assert "remote-api (工具：启用 1 个 · 停用 0 个)" in shown["text"]
    assert len(shown["tools"]) == 3


def test_mcp_view_marks_disabled_tools_without_adding_controls(monkeypatch):
    manager = Mock()
    manager.get_status_info.return_value = {
        "config_path": "/project/.makecode/mcp_config.json",
        "is_running": True,
        "tool_count": 1,
        "total_tool_count": 2,
        "config_servers": ["filesystem"],
        "enabled_config_servers": ["filesystem"],
        "disabled_servers": [],
        "loaded_servers": ["filesystem"],
        "tools": [
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
    }
    shown = {}

    def show_panel(summary, tools):
        console = Console(record=True, width=100)
        console.print(summary)
        shown["tools"] = tools
        shown["text"] = console.export_text()
        return "closed"

    monkeypatch.setattr("system.commands.show_mcp_view_tui", show_panel)
    handler = make_handler()
    handler.mcp_manager = manager

    assert handler.handle_mcp_view() is True

    assert len(shown["tools"]) == 2
    assert shown["tools"][0]["disabled"] is False
    assert shown["tools"][1]["disabled"] is True
    assert "确认应用" not in shown["text"]


def test_mcp_view_renders_empty_state_without_controls(monkeypatch):
    manager = Mock()
    manager.get_status_info.return_value = {
        "config_path": "/project/.makecode/mcp_config.json",
        "is_running": False,
        "tool_count": 0,
        "config_servers": [],
        "enabled_config_servers": [],
        "disabled_servers": [],
        "loaded_servers": [],
        "tools": [],
    }
    shown = {}

    def show_panel(summary, tools):
        console = Console(record=True, width=100)
        console.print(summary)
        shown["tools"] = tools
        shown["text"] = console.export_text()
        return "closed"

    monkeypatch.setattr("system.commands.show_mcp_view_tui", show_panel)
    handler = make_handler()
    handler.mcp_manager = manager

    assert handler.handle_mcp_view() is True

    assert "○ 未运行" in shown["text"]
    assert "0 个  未配置" in shown["text"]
    assert "○ 0 个  当前未加载" in shown["text"]
    assert shown["tools"] == []
    assert "MCP 后台管理器未运行" in shown["text"]
    assert "手动添加" not in shown["text"]
    assert "确认应用" not in shown["text"]
    assert "确认删除" not in shown["text"]


@pytest.mark.anyio
async def test_every_documented_slash_command_has_a_real_route_or_alias(monkeypatch):
    handler = make_handler()
    handler.console = Mock()
    history = [{"role": "system", "content": "system"}]
    conversation = object()

    sync_handlers = {
        "handle_mcp_help": None,
        "handle_mcp_view": None,
        "handle_mcp_add": True,
        "handle_mcp_delete": True,
        "handle_mcp_restart": None,
        "handle_mcp_switch": None,
        "handle_cmds": True,
        "handle_task_table": True,
        "handle_copy": True,
        "handle_tool_history": True,
        "handle_models": None,
        "handle_layout": None,
        "handle_update": None,
        "handle_skills_switch": "system",
        "handle_skills_list": True,
        "handle_new": None,
        "handle_cd": False,
        "handle_memory_list": True,
        "handle_memory_panel": conversation,
        "handle_memory_delete": conversation,
        "handle_memory_config": True,
        "handle_load": (history, conversation),
    }
    for name, return_value in sync_handlers.items():
        monkeypatch.setattr(handler, name, Mock(return_value=return_value))
    monkeypatch.setattr(handler, "handle_compact", AsyncMock(return_value=("summary", conversation)))
    monkeypatch.setattr(handler, "handle_memory_update", AsyncMock(return_value=[]))
    monkeypatch.setattr("system.commands.flush_tui_screen", Mock())
    monkeypatch.setattr("system.commands.toggle_plan_mode", Mock(return_value=True))
    monkeypatch.setattr("system.commands.render_current_workdir", Mock())
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())
    monkeypatch.setattr("system.commands.toggle_sub_agent_console", Mock(return_value=True))
    monkeypatch.setattr("system.commands.refresh_status", Mock())
    monkeypatch.setattr("system.commands.hitl_mod.toggle_hitl", Mock(return_value=True))

    query_by_command = {
        "/mcp-add": "/mcp-add demo -- command",
        "/mcp-delete": "/mcp-delete demo",
        "/memory-delete": "/memory-delete mem_1",
        "/memory-update": "/memory-update remember this",
        "/compact": "/compact keep decisions",
        "/cd": "/cd .",
    }
    actions = {}
    for command in COMMAND_DESCRIPTIONS:
        result = await handler.process_command(
            query_by_command.get(command, command),
            history,
            conversation,
            render_banner_fn=Mock(),
            render_hint_fn=Mock(),
            render_history_fn=Mock(),
        )
        actions[command] = result.action

    assert set(actions) == set(COMMAND_DESCRIPTIONS)
    assert actions["/quit"] == CommandAction.EXIT
    assert actions["/exit"] == CommandAction.EXIT
    assert all(action != CommandAction.RUN_AGENT for action in actions.values())
    assert actions["/help"] == actions["/cmds"] == CommandAction.CONTINUE


def test_mcp_quick_buttons_keep_audit_and_management_entries_separate():
    app = MakeCodeTuiApp.__new__(MakeCodeTuiApp)
    app._run_quick_command = Mock()

    audit_event = Mock()
    audit_event.button.id = "quick-mcp"
    app.on_button_pressed(audit_event)

    management_event = Mock()
    management_event.button.id = "quick-mcp-config"
    app.on_button_pressed(management_event)

    assert [item.args[0] for item in app._run_quick_command.call_args_list] == [
        "/mcp-view",
        "/mcp-switch",
    ]


def test_mcp_registry_snapshot_copies_tools_and_handlers_under_one_lock():
    manager = GlobalMCPManager()
    handler = Mock()
    manager._mcp_tools = [{"name": "server_read"}]
    manager._mcp_handlers = {"server_read": handler}

    tools, handlers = manager.get_registry_snapshot()
    tools.append({"name": "mutated"})
    handlers.clear()

    assert manager.get_registry_snapshot() == (
        [{"name": "server_read"}],
        {"server_read": handler},
    )


def test_mcp_manager_lists_tool_switches_by_original_name(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "disabled": False,
                "disabledTools": ["read.file"],
            }
        }
    }), encoding="utf-8")
    manager.clients["filesystem"] = object()
    manager._server_status_tools["filesystem"] = [
        {
            "name": "filesystem_read_file",
            "original_name": "read.file",
            "description": "Read a file",
            "provider": "filesystem",
        },
        {
            "name": "filesystem_write_file",
            "original_name": "write.file",
            "description": "Write a file",
            "provider": "filesystem",
        },
    ]

    switches = manager.list_tool_switches("filesystem")

    assert switches == {
        "server": "filesystem",
        "loaded": True,
        "server_disabled": False,
        "tools": [
            {
                "name": "filesystem_read_file",
                "original_name": "read.file",
                "description": "Read a file",
                "disabled": True,
                "enabled": False,
            },
            {
                "name": "filesystem_write_file",
                "original_name": "write.file",
                "description": "Write a file",
                "disabled": False,
                "enabled": True,
            },
        ],
    }


def test_mcp_manager_applies_tool_switches_without_reconnecting(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {"command": "npx", "disabled": False}
        }
    }), encoding="utf-8")
    manager.server_configs = manager.read_config()["mcpServers"]
    client = object()
    read_handler = Mock()
    write_handler = Mock()
    manager.clients["filesystem"] = client
    manager._server_tools["filesystem"] = [
        {"name": "filesystem_read_file", "description": "Read a file"},
        {"name": "filesystem_write_file", "description": "Write a file"},
    ]
    manager._server_status_tools["filesystem"] = [
        {
            "name": "filesystem_read_file",
            "original_name": "read.file",
            "description": "Read a file",
            "provider": "filesystem",
        },
        {
            "name": "filesystem_write_file",
            "original_name": "write.file",
            "description": "Write a file",
            "provider": "filesystem",
        },
    ]
    manager._make_handler = Mock(side_effect=lambda **kwargs: {
        "filesystem_read_file": read_handler,
        "filesystem_write_file": write_handler,
    }[kwargs["tool_name"]])
    with manager._db_lock:
        manager._rebuild_global_registry_locked()

    result = manager.apply_tool_switches("filesystem", ["read.file"])

    assert result["saved"] is True
    assert result["disabled_tools"] == ["read.file"]
    assert manager.read_config()["mcpServers"]["filesystem"]["disabledTools"] == ["read.file"]
    assert manager.clients["filesystem"] is client
    assert manager.get_registry_snapshot() == (
        [{"name": "filesystem_write_file", "description": "Write a file"}],
        {"filesystem_write_file": write_handler},
    )
    assert manager.get_status_info()["tool_count"] == 1
    assert manager.get_status_info()["total_tool_count"] == 2
    assert manager.get_status_info()["tools"] == [
        {
            "name": "filesystem_read_file",
            "description": "Read a file",
            "provider": "filesystem",
            "disabled": True,
            "enabled": False,
        },
        {
            "name": "filesystem_write_file",
            "description": "Write a file",
            "provider": "filesystem",
            "disabled": False,
            "enabled": True,
        },
    ]

    restored = manager.apply_tool_switches("filesystem", [])

    assert restored["saved"] is True
    assert manager.get_registry_snapshot()[0] == [
        {"name": "filesystem_read_file", "description": "Read a file"},
        {"name": "filesystem_write_file", "description": "Write a file"},
    ]
    assert manager.clients["filesystem"] is client


def test_mcp_manager_preserves_unavailable_disabled_tools_on_apply(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "disabled": False,
                "disabledTools": ["temporarily.missing"],
            }
        }
    }), encoding="utf-8")
    manager.server_configs = manager.read_config()["mcpServers"]
    manager.clients["filesystem"] = object()
    manager._server_tools["filesystem"] = [
        {"name": "filesystem_read_file", "description": "Read a file"},
    ]
    manager._server_status_tools["filesystem"] = [{
        "name": "filesystem_read_file",
        "original_name": "read.file",
        "description": "Read a file",
        "provider": "filesystem",
    }]

    result = manager.apply_tool_switches("filesystem", ["read.file"])

    assert result["disabled_tools"] == ["temporarily.missing", "read.file"]
    assert manager.read_config()["mcpServers"]["filesystem"]["disabledTools"] == [
        "temporarily.missing",
        "read.file",
    ]


def test_mcp_manager_filters_colliding_schema_by_original_name(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(json.dumps({
        "mcpServers": {
            "server": {
                "command": "npx",
                "disabled": False,
                "disabledTools": ["read.file"],
            }
        }
    }), encoding="utf-8")
    manager.server_configs = manager.read_config()["mcpServers"]
    manager.clients["server"] = object()
    manager._server_tools["server"] = [
        {"name": "server_read_file", "description": "Disabled schema"},
        {"name": "server_read_file", "description": "Enabled schema"},
    ]
    manager._server_status_tools["server"] = [
        {
            "name": "server_read_file",
            "original_name": "read.file",
            "description": "Disabled schema",
            "provider": "server",
        },
        {
            "name": "server_read_file",
            "original_name": "read/file",
            "description": "Enabled schema",
            "provider": "server",
        },
    ]

    manager._make_handler = Mock(
        side_effect=lambda **kwargs: kwargs["original_name"]
    )

    with manager._db_lock:
        manager._rebuild_global_registry_locked()

    assert manager.get_registry_snapshot() == (
        [{"name": "server_read_file", "description": "Enabled schema"}],
        {"server_read_file": "read/file"},
    )


def test_mcp_manager_removes_tools_when_server_disconnect_cannot_run(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {"command": "npx", "disabled": False}
        }
    }), encoding="utf-8")
    manager.server_configs = manager.read_config()["mcpServers"]
    client = object()
    manager.clients["filesystem"] = client
    manager._server_tools["filesystem"] = [
        {"name": "filesystem_read_file", "description": "Read a file"},
    ]
    manager._server_status_tools["filesystem"] = [{
        "name": "filesystem_read_file",
        "original_name": "read.file",
        "description": "Read a file",
        "provider": "filesystem",
    }]
    with manager._db_lock:
        manager._rebuild_global_registry_locked()
    manager._is_running = True
    manager.loop = None

    result = manager.apply_switches({"filesystem": True})

    assert result["failed"] == [{
        "server": "filesystem",
        "action": "disable",
        "error": "MCP event loop 未运行",
    }]
    assert manager.clients["filesystem"] is client
    assert manager.get_registry_snapshot() == ([], {})
    assert manager.get_status_info()["tool_count"] == 0


def test_mcp_manager_does_not_forward_switch_metadata_to_fastmcp():
    manager = GlobalMCPManager()

    with patch("utils.mcp_manager.Client") as client:
        manager._build_client("filesystem", {
            "command": "npx",
            "args": ["server"],
            "disabled": False,
            "disabledTools": ["read.file"],
        })

    client.assert_called_once_with({
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["server"]}
        }
    })


def test_conversation_preview_excludes_recalled_memory_and_includes_assistant_text():
    preview = _conversation_preview([
        {
            "role": "user",
            "content": (
                "# Potentially Relevant Memories\n\n"
                "private recalled context\n\n"
                "# Current User Request\n\n"
                "actual question"
            ),
        },
        {"role": "assistant", "content": "assistant answer"},
        {"role": "system", "content": "system prompt"},
    ])

    console = Console(record=True, width=120)
    console.print(preview)
    rendered = console.export_text()

    assert "actual question" in rendered
    assert "assistant answer" in rendered
    assert "private recalled context" not in rendered
    assert "Potentially Relevant Memories" not in rendered
    assert "system prompt" not in rendered


def test_conversation_preview_reads_assistant_text_blocks():
    preview = _conversation_preview([
        {"role": "assistant", "content_blocks": [{"type": "text", "text": "block answer"}]},
    ])

    console = Console(record=True, width=120)
    console.print(preview)
    assert "block answer" in console.export_text()


def test_skill_command_history_preview_replay_and_copy_show_only_original_query(monkeypatch):
    message = {
        "role": "user",
        "content": (
            '<skill name="demo-skill">\nloaded demo\n</skill>\n\n'
            "User: /demo-skill 处理这个请求"
        ),
        "message_metadata": {
            "display_content": "/demo-skill 处理这个请求",
            "skill_command": True,
        },
    }

    assert _extract_message_text(message) == "/demo-skill 处理这个请求"
    preview = _conversation_preview([message])
    console = Console(record=True, width=120)
    console.print(preview)
    rendered = console.export_text()
    assert "/demo-skill 处理这个请求" in rendered
    assert "loaded demo" not in rendered

    show_copy = Mock(return_value="closed")
    monkeypatch.setattr("system.commands.show_copy_content_tui", show_copy)
    assert make_handler().handle_copy([message]) is True
    copied_message = show_copy.call_args.args[0][0]
    assert copied_message["content"] == "/demo-skill 处理这个请求"
    assert "loaded demo" not in copied_message["content"]


def test_copy_command_removes_recalled_memory_and_keeps_current_user_request(monkeypatch):
    message = {
        "role": "user",
        "content": (
            "# Potentially Relevant Memories\n\n"
            "The following long-term memories were recalled.\n\n"
            "## mem_1\n- Insight: private recalled context\n\n"
            "# Current User Request\n\n"
            "实际用户问题"
        ),
    }
    show_copy = Mock(return_value="closed")
    monkeypatch.setattr("system.commands.show_copy_content_tui", show_copy)

    assert make_handler().handle_copy([message]) is True

    copied_message = show_copy.call_args.args[0][0]
    assert copied_message["content"] == "实际用户问题"
    assert "Potentially Relevant Memories" not in copied_message["content"]
    assert "private recalled context" not in copied_message["content"]


def test_commands_panel_does_not_include_runtime_skill_commands(monkeypatch):
    handler = make_handler()
    handler.skill_loader = Mock()
    handler.skill_loader.get_slash_commands.return_value = {
        "/demo-skill": "demo description",
    }
    shown = {}

    def show_panel(title, content):
        console = Console(record=True, width=120)
        console.print(content)
        shown["text"] = console.export_text()
        return "closed"

    monkeypatch.setattr("system.commands.show_info_panel_tui", show_panel)

    assert handler.handle_cmds() is True
    assert "/demo-skill" not in shown["text"]
    assert all(command in shown["text"] for command in COMMAND_DESCRIPTIONS)


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


@pytest.mark.anyio
async def test_flush_command_registered_and_does_not_run_agent(monkeypatch):
    handler = make_handler()
    flushed = []
    monkeypatch.setattr("system.commands.flush_tui_screen", lambda: flushed.append(True))

    result = await handler.process_command(
        "/flush",
        history=[],
        current_conversation=None,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda history: None,
    )

    assert COMMAND_DESCRIPTIONS["/flush"] == "完整刷新 TUI 屏幕，不改变任何面板中已有的内容。"
    assert result.action.name == "CONTINUE"
    assert flushed == [True]


@pytest.mark.anyio
async def test_hitl_command_refreshes_tui_status(monkeypatch):
    handler = make_handler()
    toggle_hitl = Mock(return_value=False)
    refresh = Mock()
    monkeypatch.setattr("system.commands.hitl_mod.toggle_hitl", toggle_hitl)
    monkeypatch.setattr("system.commands.refresh_status", refresh)

    result = await handler.process_command(
        "/hitl",
        history=[],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert result.action == CommandAction.CONTINUE
    toggle_hitl.assert_called_once_with()
    refresh.assert_called_once_with()


def test_flush_screen_clears_terminal_and_requests_full_repaint():
    app = MakeCodeTuiApp.__new__(MakeCodeTuiApp)
    app._driver = Mock()
    app.refresh = Mock()

    app.flush_screen()

    app._driver.write.assert_called_once_with("\x1b[2J\x1b[H")
    app._driver.flush.assert_called_once_with()
    app.refresh.assert_called_once_with(repaint=True, layout=True)

def test_load_by_id_skips_picker(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "system"}])
    handler = make_handler(store)
    handler.console = Mock()
    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda *args, **kwargs: pytest.fail("direct load must not open the picker"),
    )
    monkeypatch.setattr("system.commands.post_tui", Mock())
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())

    loaded_history, loaded_conversation = handler.handle_load(
        [{"role": "system", "content": "old"}],
        None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
        conversation_id=conversation.parent.name,
    )

    assert loaded_history[0] == {"role": "system", "content": ""}
    assert loaded_conversation == conversation


def test_load_by_invalid_id_falls_back_to_picker(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "system"}])
    handler = make_handler(store)
    handler.console = Mock()
    picked = []

    def choose_conversation(conversations, **kwargs):
        picked.append(conversations)
        return str(conversation)

    monkeypatch.setattr("system.commands.interactive_choose_conversation", choose_conversation)
    monkeypatch.setattr("system.commands.post_tui", Mock())
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())

    _, loaded_conversation = handler.handle_load(
        [{"role": "system", "content": "old"}],
        None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
        conversation_id="conv_ffffffffffffffffffffffffffffffff",
    )

    assert picked == [[conversation]]
    assert loaded_conversation == conversation


def test_load_cannot_delete_current_conversation(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "system"}])
    handler = make_handler(store)
    handler.console = Mock()

    def choose_conversation(conversations, **kwargs):
        assert conversations == [conversation]
        assert kwargs["delete_handler"] is not None
        assert kwargs["preview_handler"] is not None
        assert kwargs["title_handler"] is not None
        with pytest.raises(ValueError, match="当前对话正在使用"):
            kwargs["delete_handler"](conversation)
        return "abort"

    monkeypatch.setattr("system.commands.interactive_choose_conversation", choose_conversation)

    history = [{"role": "system", "content": "system"}]
    loaded_history, current_conversation = handler.handle_load(
        history,
        conversation,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    assert loaded_history is history
    assert current_conversation == conversation
    assert conversation.exists()


def test_load_conversation_automatically_restores_task_and_sub_agent_history(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    loaded = [
        {"role": "system", "content": "saved system"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call_1", "name": "FileRead", "arguments": "{}"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "FileRead", "content": "file"},
    ]
    conversation = store.save_messages(loaded)
    task_plan = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "epic_id": "abcd1234",
        "next_id": 2,
        "tasks": {"1": {"id": "1", "subject": "Task", "description": "", "status": "pending", "depend_on": []}},
    }
    (conversation.parent / TASK_PLAN_FILE).write_text(json.dumps(task_plan), encoding="utf-8")
    history_path = conversation.parent / SUB_AGENT_HISTORY_FILE
    history_path.parent.mkdir(parents=True)
    team_history = [{
        "conversation_id": store.active_id,
        "plan_task_id": "1",
        "role": "Tester",
        "status": "completed",
    }]
    history_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": store.active_id,
        "records": team_history,
    }), encoding="utf-8")
    store.reset()

    handler = make_handler(store)
    handler.console = Mock()
    tool_history = Mock()
    events = []
    begin_batch = Mock()

    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda conversations, **kwargs: str(conversation),
    )
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())
    monkeypatch.setattr("system.commands.post_tui", Mock())
    monkeypatch.setattr("system.commands.begin_tui_batch_render", begin_batch)
    monkeypatch.setattr("system.commands.TOOL_EXECUTION_HISTORY", tool_history)

    loaded_history, loaded_conversation = handler.handle_load(
        [{"role": "system", "content": "old"}],
        None,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: events.append(("render", messages)),
    )

    expected_messages = list(loaded)
    expected_messages[0] = {"role": "system", "content": ""}
    assert loaded_history == expected_messages
    assert loaded_conversation == conversation
    assert events == [("render", expected_messages)]
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    assert tasks_module.TASK_MANAGER.conversation_id == conversation.parent.name
    assert tasks_module.TASK_MANAGER._data == task_plan
    assert teams_module.TEAM.conversation_id == conversation.parent.name
    assert teams_module.TEAM.history == team_history
    tool_history.clear.assert_called_once_with()
    begin_batch.assert_called_once_with(force_scroll=True)


@pytest.mark.anyio
async def test_load_command_hides_input_and_scrolls_after_success(monkeypatch):
    handler = make_handler()
    loaded_history = [{"role": "system", "content": "loaded"}]
    active_states = []
    scroll_to_bottom = Mock()

    monkeypatch.setattr(
        handler,
        "handle_load",
        Mock(return_value=(loaded_history, object())),
    )
    monkeypatch.setattr("system.commands.set_agent_loop_active", active_states.append)
    monkeypatch.setattr("system.commands.scroll_all_panes_to_bottom", scroll_to_bottom)

    result = await handler.process_command(
        "/load",
        history=[{"role": "system", "content": "old"}],
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert result.action == CommandAction.LOAD_HISTORY
    assert active_states == [True, False]
    scroll_to_bottom.assert_called_once_with()


@pytest.mark.anyio
async def test_load_command_does_not_scroll_when_loading_is_cancelled(monkeypatch):
    handler = make_handler()
    history = [{"role": "system", "content": "old"}]
    active_states = []
    scroll_to_bottom = Mock()

    monkeypatch.setattr(handler, "handle_load", Mock(return_value=(history, None)))
    monkeypatch.setattr("system.commands.set_agent_loop_active", active_states.append)
    monkeypatch.setattr("system.commands.scroll_all_panes_to_bottom", scroll_to_bottom)

    result = await handler.process_command(
        "/load",
        history=history,
        current_conversation=None,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert result.action == CommandAction.LOAD_HISTORY
    assert active_states == [True, False]
    scroll_to_bottom.assert_not_called()



def test_load_conversation_without_sidecars_activates_empty_histories(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "saved"}])
    store.reset()
    handler = make_handler(store)
    handler.console = Mock()

    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda conversations, **kwargs: str(conversation),
    )
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())
    monkeypatch.setattr("system.commands.post_tui", Mock())

    handler.handle_load(
        [{"role": "system", "content": "old"}],
        None,
        render_banner_fn=lambda: None,
        render_hint_fn=lambda: None,
        render_history_fn=lambda messages: None,
    )

    from utils import tasks as tasks_module
    from utils import teams as teams_module

    assert tasks_module.TASK_MANAGER.conversation_id == conversation.parent.name
    assert tasks_module.TASK_MANAGER._data["tasks"] == {}
    assert teams_module.TEAM.conversation_id == conversation.parent.name
    assert teams_module.TEAM.history == []


def test_load_malformed_messages_keeps_previous_state(tmp_path, monkeypatch):
    from system.tool_history import ToolExecutionHistory
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    store = ConversationStore(tmp_path / "conversations")
    previous = store.save_messages([{"role": "system", "content": "previous"}])
    previous_snapshot = store.load(previous)
    previous_task_manager = tasks_module.TaskManager(previous.parent, previous.parent.name)
    previous_team = teams_module.TeammateManager(previous.parent, previous.parent.name, [])
    store.reset()
    malformed = store.save_messages([
        {"role": "system", "content": "malformed"},
        {"role": "assistant", "tool_calls": 1},
    ])
    store.activate(previous_snapshot)
    monkeypatch.setattr(tasks_module, "TASK_MANAGER", previous_task_manager)
    monkeypatch.setattr(teams_module, "TEAM", previous_team)
    tool_history = ToolExecutionHistory()
    tool_history.start("FileRead", "{}")
    monkeypatch.setattr("system.commands.TOOL_EXECUTION_HISTORY", tool_history)
    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda conversations, **kwargs: str(malformed),
    )
    handler = make_handler(store)
    handler.console = Mock()
    original_history = [{"role": "system", "content": "previous"}]

    loaded_history, active_path = handler.handle_load(
        original_history,
        previous,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert loaded_history is original_history
    assert active_path == previous
    assert store.active_path == previous
    assert tasks_module.TASK_MANAGER is previous_task_manager
    assert teams_module.TEAM is previous_team
    assert len(tool_history.query()) == 1
    assert tool_history.query()[0].tool_name == "FileRead"


def test_load_failure_keeps_previous_conversation_and_managers(tmp_path, monkeypatch):
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    store = ConversationStore(tmp_path / "conversations")
    previous = store.save_messages([{"role": "system", "content": "previous"}])
    previous_snapshot = store.load(previous)
    previous_task_manager = tasks_module.TaskManager(previous.parent, previous.parent.name)
    previous_team = teams_module.TeammateManager(previous.parent, previous.parent.name, [])

    store.reset()
    invalid = store.save_messages([{"role": "system", "content": "invalid"}])
    (invalid.parent / TASK_PLAN_FILE).write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "conversation_id": invalid.parent.name,
        "epic_id": "abcd1234",
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
    }), encoding="utf-8")
    store.activate(previous_snapshot)

    monkeypatch.setattr(tasks_module, "TASK_MANAGER", previous_task_manager)
    monkeypatch.setattr(teams_module, "TEAM", previous_team)
    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda conversations, **kwargs: str(invalid),
    )
    handler = make_handler(store)
    handler.console = Mock()
    original_history = [{"role": "system", "content": "previous"}]

    loaded_history, active_path = handler.handle_load(
        original_history,
        previous,
        render_banner_fn=Mock(),
        render_hint_fn=Mock(),
        render_history_fn=Mock(),
    )

    assert loaded_history is original_history
    assert active_path == previous
    assert store.active_path == previous
    assert tasks_module.TASK_MANAGER is previous_task_manager
    assert teams_module.TEAM is previous_team



def test_new_resets_conversation_task_and_team_bindings(tmp_path, monkeypatch):
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    store = ConversationStore(tmp_path / "conversations")
    conversation = store.save_messages([{"role": "system", "content": "system"}])
    task_manager = tasks_module.TaskManager(conversation.parent, conversation.parent.name)
    team = teams_module.TeammateManager(conversation.parent, conversation.parent.name, [])
    monkeypatch.setattr(tasks_module, "TASK_MANAGER", task_manager)
    monkeypatch.setattr(teams_module, "TEAM", team)
    monkeypatch.setattr(tasks_module, "render_task_pane", Mock())
    monkeypatch.setattr("system.commands.post_tui", Mock())
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())
    monkeypatch.setattr("system.commands.render_current_workdir", Mock())
    monkeypatch.setattr("system.commands.refresh_status", Mock())
    handler = make_handler(store)
    handler.console = Mock()
    history = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "old conversation"},
    ]

    handler.handle_new(history, conversation)

    assert store.active_path is None
    assert tasks_module.TASK_MANAGER.conversation_id is None
    assert teams_module.TEAM.conversation_id is None
    assert history == [{"role": "system", "content": ""}]


def test_reset_conversation_view_clears_tool_history(monkeypatch):
    handler = make_handler()
    tool_history = Mock()
    monkeypatch.setattr("system.commands.TOOL_EXECUTION_HISTORY", tool_history)
    monkeypatch.setattr("system.commands.post_tui", Mock())
    monkeypatch.setattr("system.commands.render_current_task_plan", Mock())
    history = [{"role": "system", "content": "old"}, {"role": "user", "content": "question"}]

    handler._reset_conversation_view(history)

    assert history == [{"role": "system", "content": ""}]
    tool_history.clear.assert_called_once_with()


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


def test_skills_command_returns_refreshed_system_prompt_after_changes(monkeypatch):
    loader = Mock()
    manage_skills = Mock(
        return_value={"action": "applied", "enabled": 2, "disabled": 1}
    )
    monkeypatch.setattr("system.commands.manage_skills_tui", manage_skills)
    handler = make_handler()
    handler.console = Mock()
    handler.skill_loader = loader
    handler.get_system_prompt_fn = Mock(return_value="updated system")

    assert handler.handle_skills_list() == "updated system"
    manage_skills.assert_called_once_with(loader)
    handler.get_system_prompt_fn.assert_called_once_with()
    handler.console.print.assert_called_once()
    assert "启用 2 个，禁用 1 个" in handler.console.print.call_args.args[0]


def test_skills_command_does_not_refresh_system_prompt_without_changes(monkeypatch):
    loader = Mock()
    manage_skills = Mock(return_value="closed")
    monkeypatch.setattr("system.commands.manage_skills_tui", manage_skills)
    handler = make_handler()
    handler.skill_loader = loader
    handler.get_system_prompt_fn = Mock(return_value="updated system")

    assert handler.handle_skills_list() is None
    handler.get_system_prompt_fn.assert_not_called()


def test_tool_history_command_passes_current_messages(monkeypatch):
    history = [
        {"role": "system", "content": "system prompt"},
        {"role": "tool", "tool_call_id": "call_read", "name": "FileRead", "content": "file"},
    ]
    tool_history = Mock()
    show_history = Mock(return_value="closed")
    monkeypatch.setattr("system.commands.TOOL_EXECUTION_HISTORY", tool_history)
    monkeypatch.setattr("system.commands.show_tool_history_tui", show_history)

    assert make_handler().handle_tool_history(history) is True

    show_history.assert_called_once_with(tool_history, history)


def test_copy_command_keeps_only_questions_answers_and_terminal_io(monkeypatch):
    history = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "visible reasoning",
            "tool_calls": [
                {
                    "id": "call_read",
                    "function": {"name": "FileRead", "arguments": '{"path":"README.md"}'},
                    "raw": {"provider": "private-read-payload"},
                },
                {
                    "id": "call_terminal",
                    "function": {
                        "name": "RunTerminalCommand",
                        "arguments": '{"command":"pytest -q"}',
                    },
                    "raw": {"provider": "private-terminal-payload"},
                },
            ],
            "content_blocks": [
                {"type": "reasoning", "text": "block reasoning"},
                {"type": "text", "text": "answer"},
            ],
            "message_metadata": {
                "source_format": "anthropic",
                "source_model": "claude-test",
                "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
            },
        },
        {"role": "tool", "tool_call_id": "call_read", "name": "FileRead", "content": "file"},
        {
            "role": "tool",
            "tool_call_id": "call_terminal",
            "name": "RunTerminalCommand",
            "content": "13 passed",
        },
    ]
    show_copy = Mock(return_value="closed")
    monkeypatch.setattr("system.commands.show_copy_content_tui", show_copy)

    assert make_handler().handle_copy(history) is True

    copied_messages = show_copy.call_args.args[0]
    assert [message["role"] for message in copied_messages] == ["user", "assistant", "tool"]
    assert copied_messages[0]["content"] == "question"
    assert copied_messages[1]["content"] == "answer"
    assert "reasoning_content" not in copied_messages[1]
    assert copied_messages[1]["content_blocks"] == [{"type": "text", "text": "answer"}]
    assert [
        tool_call["function"]["name"]
        for tool_call in copied_messages[1]["tool_calls"]
    ] == ["RunTerminalCommand"]
    assert "raw" not in copied_messages[1]["tool_calls"][0]
    assert copied_messages[2]["content"] == "13 passed"
    assert history[2]["reasoning_content"] == "visible reasoning"
    assert history[2]["message_metadata"]["native_blocks"][0]["signature"] == "private-signature"
    assert history[2]["tool_calls"][0]["raw"] == {"provider": "private-read-payload"}


@pytest.mark.anyio
async def test_skill_slash_commands_are_available_to_tui_completion():
    app = MakeCodeTuiApp(
        slash_commands_provider=lambda: {
            "/help": "help",
            "/demo-skill": "Skill: demo description",
        }
    )

    assert app._get_slash_matches("/demo") == [
        ("/demo-skill", "Skill: demo description")
    ]
    assert app._get_slash_matches("/demo-skill") == []
    assert app._get_slash_matches("/disabled-skill") == []


@pytest.mark.anyio
async def test_submitting_flush_preserves_existing_pane_content():
    submitted = []
    submitted_event = threading.Event()

    async def submit_handler(text):
        submitted.append(text)
        submitted_event.set()

    app = MakeCodeTuiApp(submit_handler=submit_handler)

    async with app.run_test() as pilot:
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "existing content"))
        await pilot.pause()
        content_log = app.query_one("#content-log")
        before = list(content_log.children)

        input_box = app.query_one("#input-box", MakeCodeInput)
        input_box.load_text("/flush")
        app.submit_current_input()
        await pilot.pause()
        assert await asyncio.to_thread(submitted_event.wait, 1)

        assert list(content_log.children) == before
        assert submitted == ["/flush"]


def test_directory_completion_candidates_preserve_separator_style(tmp_path):
    (tmp_path / "parent" / "child").mkdir(parents=True)

    assert directory_completion_candidates("par", tmp_path) == [f"parent{os.sep}"]
    assert directory_completion_candidates("parent/ch", tmp_path) == ["parent/child/"]
    if os.sep == "\\":
        assert directory_completion_candidates(r"parent\ch", tmp_path) == [
            "parent\\child\\"
        ]


class StartupWorkdirModalHost(App):
    def __init__(self, modal: StartupWorkdirModal):
        super().__init__()
        self.modal = modal
        self.result = None

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self.modal, self._on_dismiss)

    def _on_dismiss(self, result: str | None) -> None:
        self.result = result


@pytest.mark.anyio
async def test_startup_workdir_completion_uses_common_prefix_and_selected_candidate(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    (tmp_path / "alpine-file").write_text("not a directory", encoding="utf-8")
    modal = StartupWorkdirModal(tmp_path)
    app = StartupWorkdirModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.press("down", "enter")
        await pilot.pause()
        custom_input = modal.query_one("#startup-input", Input)
        candidate_box = modal.query_one("#startup-candidates", Static)
        assert custom_input.display
        assert app.result is None

        custom_input.value = "al"
        custom_input.cursor_position = len("al")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert custom_input.value == "alp"
        assert custom_input.cursor_position == len("alp")
        assert candidate_box.has_class("visible")
        assert f"alpha{os.sep}" in str(candidate_box.content)
        assert f"alpine{os.sep}" in str(candidate_box.content)
        assert f"alpine-file{os.sep}" not in str(candidate_box.content)

        await pilot.press("down", "tab")
        await pilot.pause()
        completed_path = f"alpine{os.sep}"
        assert custom_input.value == completed_path
        assert custom_input.cursor_position == len(completed_path)
        assert not candidate_box.has_class("visible")

        await pilot.press("enter")
        await pilot.pause()
        assert app.result == f"custom:{completed_path}"


@pytest.mark.anyio
async def test_startup_workdir_completion_supports_tilde_and_quoted_space_paths(tmp_path):
    (tmp_path / "space dir").mkdir()
    modal = StartupWorkdirModal(tmp_path)
    app = StartupWorkdirModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.press("down", "enter")
        await pilot.pause()
        custom_input = modal.query_one("#startup-input", Input)

        custom_input.value = "~"
        custom_input.cursor_position = len("~")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert custom_input.value == "~/"
        assert custom_input.cursor_position == len("~/")

        custom_input.value = '"space'
        custom_input.cursor_position = len('"space')
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        quoted_path = f'"space dir{os.sep}"'
        assert custom_input.value == quoted_path
        assert custom_input.cursor_position == len(quoted_path) - 1
        await pilot.press("enter")
        await pilot.pause()

    assert app.result == f'custom:"space dir{os.sep}"'
    assert resolve_chosen_workdir(app.result, tmp_path) == (tmp_path / "space dir").resolve()


@pytest.mark.anyio
async def test_startup_workdir_completion_preserves_text_after_cursor(tmp_path):
    (tmp_path / "space dir").mkdir()
    modal = StartupWorkdirModal(tmp_path)
    app = StartupWorkdirModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.press("down", "enter")
        await pilot.pause()
        custom_input = modal.query_one("#startup-input", Input)
        custom_input.value = "space-suffix"
        custom_input.cursor_position = len("space")
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()

        completed_path = f"space dir{os.sep}"
        assert custom_input.value == f"{completed_path}-suffix"
        assert custom_input.cursor_position == len(completed_path)


@pytest.mark.anyio
async def test_cd_path_completion_uses_longest_common_prefix_and_allows_selecting_candidates(tmp_path, monkeypatch):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpine-file").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)

    app = MakeCodeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)
        candidate_box = app.query_one("#slash-hints")
        input_box.load_text("/cd al")
        input_box.cursor_location = input_box.document.end

        input_box.focus()
        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == "/cd alp"
        assert candidate_box.has_class("visible")
        assert f"alpha{os.sep}" in str(candidate_box.content)
        assert f"alpine{os.sep}" in str(candidate_box.content)
        assert f"alpine-file{os.sep}" not in str(candidate_box.content)

        await pilot.press("up")
        await pilot.pause()
        assert f"❯ alpine{os.sep}" in str(candidate_box.content)

        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == f"/cd alpine{os.sep}"
        assert not candidate_box.has_class("visible")


@pytest.mark.anyio
async def test_cd_path_completion_from_empty_argument_shows_directory_candidates(tmp_path, monkeypatch):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)

    app = MakeCodeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)
        candidate_box = app.query_one("#slash-hints")
        input_box.load_text("/cd ")
        input_box.cursor_location = input_box.document.end

        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == "/cd "
        assert candidate_box.has_class("visible")
        assert f"alpha{os.sep}" in str(candidate_box.content)
        assert f"beta{os.sep}" in str(candidate_box.content)

        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == f"/cd alpha{os.sep}"
        assert not candidate_box.has_class("visible")


@pytest.mark.anyio
async def test_cd_path_completion_keeps_all_candidates_and_scrolls_a_six_item_window(tmp_path, monkeypatch):
    for index in range(8):
        (tmp_path / f"dir{index}").mkdir()
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)

    app = MakeCodeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)
        candidate_box = app.query_one("#slash-hints")
        input_box.load_text("/cd d")
        input_box.cursor_location = input_box.document.end

        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == "/cd dir"
        assert f"dir0{os.sep}" in str(candidate_box.content)
        assert f"dir5{os.sep}" in str(candidate_box.content)
        assert f"dir6{os.sep}" not in str(candidate_box.content)

        await pilot.press(*["down"] * 7)
        await pilot.pause()
        assert f"❯ dir7{os.sep}" in str(candidate_box.content)
        assert f"dir0{os.sep}" not in str(candidate_box.content)
        assert f"dir2{os.sep}" in str(candidate_box.content)

        await pilot.press("tab")
        await pilot.pause()
        assert input_box.text == f"/cd dir7{os.sep}"


@pytest.mark.anyio
async def test_cd_path_candidate_selection_does_not_change_enter_submission(tmp_path, monkeypatch):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)
    submitted = []
    submitted_event = threading.Event()

    async def submit_handler(text):
        submitted.append(text)
        submitted_event.set()

    app = MakeCodeTuiApp(submit_handler=submit_handler)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)
        input_box.load_text("/cd al")
        input_box.cursor_location = input_box.document.end
        input_box.focus()

        await pilot.press("tab", "down", "enter")
        await pilot.pause()
        assert await asyncio.to_thread(submitted_event.wait, 1)
        assert submitted == ["/cd alp"]


@pytest.mark.anyio
async def test_cd_path_completion_only_completes_directories_and_preserves_absolute_paths(tmp_path, monkeypatch):
    (tmp_path / "target-dir").mkdir()
    (tmp_path / "target-file").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)

    app = MakeCodeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)
        input_box.load_text(f"/cd {tmp_path}/target")
        input_box.cursor_location = input_box.document.end

        app.complete_slash_command()

        assert input_box.text == f"/cd {tmp_path}/target-dir/"


@pytest.mark.anyio
async def test_cd_path_completion_supports_tilde_and_quoted_paths(tmp_path, monkeypatch):
    (tmp_path / "space dir").mkdir()
    monkeypatch.setattr("system.tui_app.paths.workdir", lambda: tmp_path)

    app = MakeCodeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box", MakeCodeInput)

        input_box.load_text('/cd "space"')
        input_box.cursor_location = (0, len('/cd "space'))
        app.complete_slash_command()
        quoted_path = f'/cd "space dir{os.sep}"'
        assert input_box.text == quoted_path
        assert input_box.cursor_location == (0, len(quoted_path) - 1)

        input_box.load_text("/cd ~")
        input_box.cursor_location = input_box.document.end
        app.complete_slash_command()
        assert input_box.text == "/cd ~/"


def test_submit_worker_owns_single_asyncio_run_boundary():
    running_loops = []

    async def submit_handler(text):
        running_loops.append((text, asyncio.get_running_loop()))

    app = MakeCodeTuiApp(submit_handler=submit_handler)
    real_asyncio_run = asyncio.run
    with patch("system.tui_app.asyncio.run", wraps=real_asyncio_run) as run:
        app._run_submit_handler("hello")

    run.assert_called_once()
    assert len(running_loops) == 1
    assert running_loops[0][0] == "hello"


def test_quick_command_bypasses_submit_lock_without_releasing_it():
    submitted = []
    submitted_event = threading.Event()

    async def submit_handler(text):
        submitted.append(text)
        submitted_event.set()

    app = MakeCodeTuiApp(submit_handler=submit_handler)
    assert app._submit_lock.acquire(blocking=False)
    try:
        app._run_quick_command("/tasks")
        assert submitted_event.wait(timeout=1)
        assert submitted == ["/tasks"]
        assert not app._submit_lock.acquire(blocking=False)
    finally:
        app._submit_lock.release()


@pytest.mark.anyio
async def test_tui_displays_invalid_rich_markup_as_plain_text():
    app = MakeCodeTuiApp()
    payload = "&mt=doc&dt=doc','https://ku.baidu-int.com/knowledge/example[/link]"

    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, payload))
        await pilot.pause()

        content_log = app.query_one("#content-log")
        rendered = "".join(str(child.content) for child in content_log.children)
        assert payload in rendered


@pytest.mark.anyio
async def test_scroll_all_panes_to_bottom_forces_non_bottom_viewports_to_end():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        for index in range(30):
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, f"内容 {index}\n\n"))
            app.handle_tui_event(TuiEvent(TuiRegion.BACKGROUND, f"后台 {index}\n\n"))
        await pilot.pause()

        content_scroller = app.query_one("#content-log")
        background_log = app._logs[TuiRegion.BACKGROUND]
        content_scroller.scroll_to(y=0, animate=False, immediate=True)
        background_log.scroll_to(y=0, animate=False, immediate=True)
        assert content_scroller.scroll_y == 0
        assert background_log.scroll_y == 0

        app.scroll_all_panes_to_bottom()
        await pilot.pause()
        await pilot.pause()

        assert content_scroller.scroll_y == content_scroller.max_scroll_y
        assert background_log.scroll_y == background_log.max_scroll_y


@pytest.mark.anyio
async def test_force_scroll_batch_does_not_check_previous_viewport_position():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app._is_log_at_bottom = Mock(side_effect=AssertionError("log bottom check should be skipped"))
        app._is_scroller_at_bottom = Mock(side_effect=AssertionError("scroller bottom check should be skipped"))

        content_scroller = app.query_one("#content-log")
        background_log = app._logs[TuiRegion.BACKGROUND]

        app.begin_batch_render(force_scroll=True)
        for index in range(40):
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, f"历史内容 {index}\n\n"))
            app.handle_tui_event(TuiEvent(TuiRegion.BACKGROUND, f"后台输出 {index}\n\n"))

        await pilot.pause()
        await pilot.pause()
        # 回放过程中（批次尚未结束）就应始终在底部
        assert content_scroller.scroll_y == content_scroller.max_scroll_y
        assert background_log.scroll_y == background_log.max_scroll_y

        app.end_batch_render()
        await pilot.pause()
        await pilot.pause()

        assert content_scroller.scroll_y == content_scroller.max_scroll_y
        assert background_log.scroll_y == background_log.max_scroll_y


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


def test_parse_mcp_add_rejects_disabled_field_override():
    handler = make_handler()

    with pytest.raises(ValueError, match="始终以禁用状态添加"):
        handler._parse_mcp_add_config("/mcp-add api --url https://example.com/mcp disabled=")


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


def test_mcp_manager_lists_display_metadata_without_url_secrets(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"
    manager.config_path.write_text(
        json.dumps({
            "mcpServers": {
                "remote": {
                    "url": "https://user:password@example.com/mcp?token=secret-query#fragment",
                    "transport": "streamable-http",
                    "disabled": False,
                },
                "local": {
                    "command": "uvx",
                    "args": ["secret-argument"],
                    "env": {"TOKEN": "secret-env"},
                    "disabled": True,
                },
            }
        }),
        encoding="utf-8",
    )
    manager.clients["remote"] = object()
    manager._server_status_tools["remote"] = [{"name": "one"}, {"name": "two"}]

    switches = {item["name"]: item for item in manager.list_server_switches()}

    assert switches["remote"] == {
        "name": "remote",
        "disabled": False,
        "enabled": True,
        "loaded": True,
        "transport": "streamable-http",
        "target": "https://example.com/mcp",
        "tool_count": 2,
    }
    assert switches["local"]["target"] == "uvx"
    assert switches["local"]["tool_count"] == 0
    serialized = json.dumps(switches, ensure_ascii=False)
    for secret in ("password", "secret-query", "secret-argument", "secret-env"):
        assert secret not in serialized


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
