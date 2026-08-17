from unittest.mock import Mock, patch

import pytest
from textual.selection import Selection

from system.console_render import (
    render_copyable_markdown,
    render_content_assistant_message,
    render_content_user_message,
)
from system.stream_render import StreamRenderer
from system.tui_app import CopyableContentGroup, ContentBlock, MakeCodeTuiApp
from system.tui_types import TuiEvent, TuiRegion


TEST_LAYOUT_RATIOS = {
    "task": 2,
    "background": 3,
    "sub_agent": 1,
}


@pytest.fixture(autouse=True)
def isolate_tui_layout_config(monkeypatch):
    monkeypatch.setattr("system.tui_app.load_layout_ratios", lambda: dict(TEST_LAYOUT_RATIOS))


def test_message_panels_carry_raw_text():
    user_panel = render_content_user_message("用户消息原文")
    assistant_panel = render_content_assistant_message("# 标题\n正文", identity="Assistant")

    assert user_panel.copy_text == "用户消息原文"
    assert assistant_panel.copy_text == "# 标题\n正文"


def test_copyable_group_get_selection_returns_raw_text():
    panel = render_content_user_message("整条消息原文")
    group = CopyableContentGroup(panel.copy_text)

    assert group.get_selection(Selection(None, None)) == ("整条消息原文", "\n")
    assert "on_click" not in ContentBlock.__dict__


def test_content_block_has_no_copy_behavior():
    """子 block 只负责渲染，不处理点击或选区复制。"""
    block = ContentBlock("普通文本块")

    assert "on_click" not in ContentBlock.__dict__
    assert "get_selection" not in ContentBlock.__dict__


def test_copyable_markdown_carries_raw_text():
    markdown = render_copyable_markdown("流式正文段落")

    assert markdown.copy_text == "流式正文段落"


def test_stream_committed_blocks_carry_raw_text_for_content_only():
    renderer = StreamRenderer()
    events = []

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload)),
    ):
        remaining, emitted = renderer._process_block_commit(
            "第一段完整\n\n第二段尾巴", "第一段完整\n\n第二段尾巴", region=TuiRegion.CONTENT
        )
        renderer._process_block_commit(
            "思考段落\n\n尾巴", "思考段落\n\n尾巴", region=TuiRegion.REASONING
        )
        renderer._safe_cleanup("收尾正文", region=TuiRegion.CONTENT)
        renderer._safe_cleanup("收尾思考", region=TuiRegion.REASONING)

    content_payloads = [payload for region, payload in events if region == TuiRegion.CONTENT and not isinstance(payload, str) and payload is not None]
    reasoning_payloads = [payload for region, payload in events if region == TuiRegion.REASONING and not isinstance(payload, str) and payload is not None]

    assert [payload.copy_text for payload in content_payloads] == ["第一段完整\n\n", "收尾正文"]
    assert all(not hasattr(payload, "copy_text") for payload in reasoning_payloads)


@pytest.mark.anyio
async def test_double_click_on_streamed_block_copies_complete_response_from_transparent_parent():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, None, active=True))
        app._handle_content_block_event(
            TuiEvent(TuiRegion.CONTENT, render_copyable_markdown("第一段", copy_text="第一段\n\n"))
        )
        app._handle_content_block_event(
            TuiEvent(TuiRegion.CONTENT, render_copyable_markdown("第二段"))
        )
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, None, active=False))
        await pilot.pause()

        container = app.query_one("#content-log")
        assert len(container.children) == 1
        group = container.children[0]
        assert isinstance(group, CopyableContentGroup)
        assert group.copy_text == "第一段\n\n第二段"
        assert group.has_class("copyable-content-group")
        blocks = group.query(ContentBlock)
        assert len(blocks) == 2

        with patch("system.tui_app.copy_to_system_clipboard", return_value=True) as system_copy:
            await pilot.double_click(blocks[1], offset=(2, 0))
            await pilot.pause()

        system_copy.assert_called_once_with("第一段\n\n第二段")


@pytest.mark.anyio
async def test_double_click_copies_whole_message_to_system_clipboard():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message("第一条消息")))
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, render_content_assistant_message("# 第二条\n正文")))
        await pilot.pause()

        blocks = app.query(ContentBlock)
        assert len(blocks) == 2

        with patch("system.tui_app.copy_to_system_clipboard", return_value=True) as system_copy:
            await pilot.double_click(blocks[0], offset=(2, 1))
            await pilot.pause()

        system_copy.assert_called_once_with("第一条消息")


@pytest.mark.anyio
async def test_double_click_falls_back_to_osc52_when_system_clipboard_unavailable():
    app = MakeCodeTuiApp()
    app.copy_to_clipboard = Mock()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message("唯一消息")))
        await pilot.pause()

        block = app.query_one(ContentBlock)

        with patch("system.tui_app.copy_to_system_clipboard", return_value=False) as system_copy:
            await pilot.double_click(block, offset=(2, 1))
            await pilot.pause()

        system_copy.assert_called_once_with("唯一消息")
        app.copy_to_clipboard.assert_called_once_with("唯一消息")


@pytest.mark.anyio
async def test_double_click_on_non_message_block_does_not_copy():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, "[bold cyan]📂 当前工作目录: /tmp/demo[/bold cyan]"))
        await pilot.pause()

        block = app.query_one(ContentBlock)

        with patch("system.tui_app.copy_to_system_clipboard", return_value=True) as system_copy:
            await pilot.double_click(block, offset=(2, 1))
            await pilot.pause()

        system_copy.assert_not_called()


@pytest.mark.anyio
async def test_single_click_does_not_copy():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._handle_content_block_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message("单击消息")))
        await pilot.pause()

        block = app.query_one(ContentBlock)

        with patch("system.tui_app.copy_to_system_clipboard", return_value=True) as system_copy:
            await pilot.click(block, offset=(2, 1))
            await pilot.pause()

        system_copy.assert_not_called()
