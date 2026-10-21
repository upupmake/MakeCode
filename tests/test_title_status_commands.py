from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from system.commands import CommandHandler


class FakeMCPManager:
    def get_status_info(self):
        return {"loaded_servers": ["demo"], "tool_count": 2}


class FakeSkillLoader:
    skills = ["alpha", "beta"]


def make_handler():
    store = SimpleNamespace(
        active_path=Path("/tmp/conversation"),
        update_title=lambda title: None,
    )
    console = Console(
        file=StringIO(),
        force_terminal=False,
        color_system=None,
        width=100,
    )
    return CommandHandler(
        console=console,
        mcp_manager=FakeMCPManager(),
        skill_loader=FakeSkillLoader(),
        get_system_prompt_fn=lambda: "",
        conversation_store=store,
        auto_compact_fn=lambda *args, **kwargs: None,
    )


@pytest.mark.anyio
async def test_title_command_updates_custom_title():
    handler = make_handler()
    updates = []
    handler.conversation_title_update_fn = updates.append

    await handler.handle_title("/title 新标题")

    assert updates == ["新标题"]


@pytest.mark.anyio
async def test_title_command_regenerates_title(monkeypatch):
    handler = make_handler()
    called = []

    async def regenerate():
        called.append(True)

    handler.conversation_title_regenerate_fn = regenerate
    monkeypatch.setattr(
        "system.commands.choose_tui",
        lambda *args, **kwargs: "重新生成标题",
    )

    await handler.handle_title("/title")

    assert called == [True]


def test_status_command_prints_runtime_and_counts(monkeypatch):
    handler = make_handler()
    monkeypatch.setattr(
        "system.commands.get_active_memory_count",
        lambda: 3,
    )

    handler.handle_status(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
    )

    output = handler.console.file.getvalue()
    assert "工作目录:" in output
    assert "会话轮数: 1" in output
    assert "记忆: 3" in output
    assert "Skills: 2" in output
    assert "MCP: 1S/2T" in output


def test_tokens_command_prints_breakdown():
    handler = make_handler()
    handler.token_usage_provider = lambda: (
        {
            "system": 1000,
            "user": 2000,
            "reasoning": 50,
            "assistant": 500,
            "tool": 100,
        },
        10000,
    )

    handler.handle_tokens()

    output = handler.console.file.getvalue()
    assert "上下文 Token 使用" in output
    assert "system（含工具定义）: 1,000" in output
    assert "合计: 3,650 / 10,000" in output


def test_release_notes_command_prints_log_and_update_hint(monkeypatch):
    handler = make_handler()
    monkeypatch.setattr(
        "system.updater.fetch_latest_version_info",
        lambda **kwargs: {
            "version": "999.0.0",
            "release_log": "- 新增功能 A\n- 修复问题 B",
        },
    )

    handler.handle_release_notes()

    output = handler.console.file.getvalue()
    assert "新增功能 A" in output
    assert "修复问题 B" in output
    assert "999.0.0" in output
    assert "/update" in output
