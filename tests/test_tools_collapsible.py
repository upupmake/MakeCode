import pytest
from textual.containers import VerticalScroll
from unittest.mock import AsyncMock, patch
from textual.widgets import Collapsible

import system.console_render as console_render
from system.console_render import _render_history
from system.tui_app import MakeCodeTuiApp
from system.tui_types import TuiEvent, TuiRegion


@pytest.mark.anyio
async def test_tool_round_creates_one_collapsible_per_tool_with_call_and_result_blocks():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.handle_tui_event(
            TuiEvent(
                TuiRegion.CONTENT,
                None,
                collapsible_title="🛠️ Tool: FileRead",
                collapsible_open=True,
                collapsible_kind="tools",
            )
        )
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "工具: FileRead\n\nArguments\n─────────\n{}"))
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "Result\n─────────\n读取成功"))
        app.handle_tui_event(
            TuiEvent(
                TuiRegion.CONTENT,
                None,
                collapsible_title="🛠️ Tool: FileRead",
                collapsible_close=True,
                collapsible_kind="tools",
            )
        )
        app.handle_tui_event(
            TuiEvent(
                TuiRegion.CONTENT,
                None,
                collapsible_title="🛠️ Tool: ContentSearch",
                collapsible_open=True,
                collapsible_kind="tools",
            )
        )
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "工具: ContentSearch\n\nArguments\n─────────\n{}"))
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "Result\n─────────\n搜索成功"))
        app.handle_tui_event(
            TuiEvent(
                TuiRegion.CONTENT,
                None,
                collapsible_title="🛠️ Tool: ContentSearch",
                collapsible_close=True,
                collapsible_kind="tools",
            )
        )
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 2
        first_collapsible = container.children[0]
        second_collapsible = container.children[1]
        assert first_collapsible.title == "🛠️ Tool: FileRead"
        assert second_collapsible.title == "🛠️ Tool: ContentSearch"
        assert first_collapsible.collapsed is True
        assert second_collapsible.collapsed is True
        first_contents = first_collapsible.query_one(Collapsible.Contents)
        second_contents = second_collapsible.query_one(Collapsible.Contents)
        assert len(first_contents.children) == 2
        assert len(second_contents.children) == 2
        assert "Arguments" in str(first_contents.children[0].content)
        assert "读取成功" in str(first_contents.children[1].content)
        assert "ContentSearch" in str(second_contents.children[0].content)
        assert "搜索成功" in str(second_contents.children[1].content)


def test_history_replay_renders_main_agent_tools_round(monkeypatch):
    posted = []

    def fake_post_tui(region, payload=None, **kwargs):
        posted.append((region, payload, kwargs))

    monkeypatch.setattr(console_render, "post_tui", fake_post_tui)
    _render_history([
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_read",
                    "function": {
                        "name": "FileRead",
                        "arguments": '{"path":"README.md"}',
                    },
                },
                {
                    "id": "call_search",
                    "function": {
                        "name": "ContentSearch",
                        "arguments": '{"content_regex":"TODO"}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "name": "FileRead",
            "content": "读取成功",
        },
        {
            "role": "tool",
            "tool_call_id": "call_search",
            "name": "ContentSearch",
            "content": "搜索成功",
        },
    ])

    tool_posts = [item for item in posted if item[0] == TuiRegion.CONTENT]
    assert len(tool_posts) == 8
    assert tool_posts[0][2]["collapsible_open"] is True
    assert tool_posts[0][2]["collapsible_title"] == "🛠️ Tool: FileRead"
    assert "Arguments" in str(tool_posts[1][1])
    assert "读取成功" in str(tool_posts[2][1])
    assert tool_posts[3][2]["collapsible_close"] is True
    assert tool_posts[3][2]["collapsible_title"] == "🛠️ Tool: FileRead"
    assert tool_posts[4][2]["collapsible_open"] is True
    assert tool_posts[4][2]["collapsible_title"] == "🛠️ Tool: ContentSearch"
    assert "Arguments" in str(tool_posts[5][1])
    assert "搜索成功" in str(tool_posts[6][1])
    assert tool_posts[7][2]["collapsible_close"] is True
    assert tool_posts[7][2]["collapsible_title"] == "🛠️ Tool: ContentSearch"


@pytest.mark.anyio
async def test_main_agent_live_tool_round_posts_call_and_result_blocks():
    import main as main_module

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read the file"},
    ]
    tool_call = {
        "id": "call_read",
        "name": "FileRead",
        "arguments": "{}",
    }
    second_tool_call = {
        "id": "call_search",
        "name": "ContentSearch",
        "arguments": "{}",
    }
    responses = [
        (
            "",
            [tool_call, second_tool_call],
            {"role": "assistant", "content": None, "tool_calls": [tool_call, second_tool_call]},
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done"},
            False,
        ),
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(history, message):
            history.append(message)

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
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "post_tui") as post_tui, \
            patch.object(main_module, "is_plan_mode", return_value=False), \
            patch.object(main_module, "consume_temporary_query", return_value=None), \
            patch.object(main_module, "clear_temporary_query"), \
            patch.object(main_module, "set_temporary_query_enabled"):
        committed = await main_module._agent_loop_with_client(messages, FakeClient())

    assert committed is True
    content_posts = [
        call for call in post_tui.call_args_list
        if call.args and call.args[0] == main_module.TuiRegion.CONTENT
    ]
    assert content_posts[0].kwargs["collapsible_open"] is True
    assert content_posts[0].kwargs["collapsible_title"] == "🛠️ Tool: FileRead"
    assert content_posts[0].kwargs["collapsible_kind"] == "tools"
    assert "FileRead" in str(content_posts[1].args[1])
    assert "Arguments" in str(content_posts[1].args[1])
    assert "Invalid arguments for FileRead" in str(content_posts[2].args[1])
    assert "Result" in str(content_posts[2].args[1])
    assert len(content_posts) == 8
    assert content_posts[0].kwargs["collapsible_open"] is True
    assert content_posts[0].kwargs["collapsible_title"] == "🛠️ Tool: FileRead"
    assert "FileRead" in str(content_posts[1].args[1])
    assert "Invalid arguments for FileRead" in str(content_posts[2].args[1])
    assert content_posts[3].kwargs["collapsible_close"] is True
    assert content_posts[3].kwargs["collapsible_title"] == "🛠️ Tool: FileRead"
    assert content_posts[4].kwargs["collapsible_open"] is True
    assert content_posts[4].kwargs["collapsible_title"] == "🛠️ Tool: ContentSearch"
    assert "ContentSearch" in str(content_posts[5].args[1])
    assert "Invalid arguments for ContentSearch" in str(content_posts[6].args[1])
    assert content_posts[7].kwargs["collapsible_close"] is True
    assert content_posts[7].kwargs["collapsible_title"] == "🛠️ Tool: ContentSearch"
