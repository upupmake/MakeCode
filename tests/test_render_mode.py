"""渲染模式相关回归测试：simple 模式降级与内容块压实。

这里的测试刻意用同步函数包 asyncio.run，避免依赖 pytest 的异步插件。
"""

import asyncio

import pytest
from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import Static

from system import cli
from system import tui_mode
from system.console_render import CopyableText, render_copyable_markdown, render_model_markdown
from system.tui_app import (
    CONTENT_BLOCK_LIMIT,
    MakeCodeTuiApp,
    RICHLOG_MAX_ENTRIES,
    _payload_plain_text,
    _renderable_plain_text,
    _static_renderable,
    post_tui,
)
from system.tui_types import TuiRegion


@pytest.fixture(autouse=True)
def reset_simple_mode():
    tui_mode.set_simple_mode(False)
    yield
    tui_mode.set_simple_mode(False)


async def _settle(pilot, rounds: int = 12) -> None:
    """给异步 worker（内容压实等）若干个消息周期完成。"""
    for _ in range(rounds):
        await pilot.pause(0.05)


def test_markdown_rendering_degrades_in_simple_mode():
    assert isinstance(render_model_markdown("# hi"), Markdown)
    assert getattr(render_copyable_markdown("body", copy_text="raw"), "copy_text") == "raw"

    tui_mode.set_simple_mode(True)
    plain = render_model_markdown("# hi")
    assert isinstance(plain, Text) and not isinstance(plain, Markdown)

    copyable = render_copyable_markdown("body", copy_text="raw")
    assert isinstance(copyable, CopyableText)
    assert copyable.copy_text == "raw"
    assert copyable.group_stream_blocks is True


def test_plain_text_extraction_helpers():
    assert _renderable_plain_text(Text("abc")) == "abc"
    assert _renderable_plain_text(render_model_markdown("# src")) == "# src"
    tui_mode.set_simple_mode(True)
    assert _payload_plain_text(render_copyable_markdown("body", copy_text="raw")) == "raw"


def test_simple_mode_state_is_process_local_only():
    """simple 模式不写任何配置文件，只存在于当前进程。"""
    assert tui_mode.is_simple_mode() is False
    assert tui_mode.toggle_simple_mode() is True
    assert tui_mode.is_simple_mode() is True
    assert tui_mode.toggle_simple_mode() is False


def test_cli_simple_flag_is_parsed():
    assert cli.run_external_cli(["--simple"]) is None
    assert cli.SIMPLE_MODE_REQUESTED is True

    assert cli.run_external_cli([]) is None
    assert cli.SIMPLE_MODE_REQUESTED is False


def test_content_blocks_are_compacted_when_over_limit():
    async def scenario() -> int:
        app = MakeCodeTuiApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for index in range(CONTENT_BLOCK_LIMIT + 120):
                post_tui(TuiRegion.CONTENT, f"block-{index}")
            await _settle(pilot)
            children = list(app._content_scroller.children)
            # 压实生效：块数被压回上限附近
            assert len(children) <= CONTENT_BLOCK_LIMIT, len(children)
            # 归档文本不会因为反复压实而指数膨胀
            total = sum(len(getattr(child, "copy_text", "") or "") for child in children)
            assert total < 200000, total
            # 最近的内容仍然保留
            assert any(
                f"block-{CONTENT_BLOCK_LIMIT + 119}" in (getattr(child, "copy_text", "") or "")
                for child in children
            )
            # 至少产生一个归档块
            assert any(child.has_class("archived-block") for child in children)
            return len(children)

    assert asyncio.run(scenario()) <= CONTENT_BLOCK_LIMIT


def test_compaction_catches_up_with_a_large_single_burst():
    """一次性投递远超「固定批量 × 轮数」的块数也必须能压回上限内。

    /load 渲染历史走 begin_tui_batch_render，是同步突发投递。
    旧实现「固定清 80 块 × 最多 8 轮」= 单次最多清 640 块，会追不上。
    """
    burst = 2000

    async def scenario() -> int:
        app = MakeCodeTuiApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for index in range(burst):
                post_tui(TuiRegion.CONTENT, f"block-{index}")
            await _settle(pilot, rounds=20)
            return len(app._content_scroller.children)

    count = asyncio.run(scenario())
    assert count <= CONTENT_BLOCK_LIMIT, f"突发 {burst} 块后仍超限：{count}"


def test_simple_mode_hides_panes_and_skips_hidden_regions():
    async def scenario() -> None:
        app = MakeCodeTuiApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert not app.query_one("#right-column").has_class("hidden")

            tui_mode.set_simple_mode(True)
            app.apply_simple_mode()
            await pilot.pause()
            assert app.query_one("#right-column").has_class("hidden")
            assert app._clock_timer is None

            log = app.query_one("#background-log")
            before = len(log._render_entries)
            post_tui(TuiRegion.BACKGROUND, "should be skipped in simple mode")
            await pilot.pause()
            assert len(log._render_entries) == before

            tui_mode.set_simple_mode(False)
            app.apply_simple_mode()
            await pilot.pause()
            assert not app.query_one("#right-column").has_class("hidden")
            assert app._clock_timer is not None

    asyncio.run(scenario())


def test_rich_log_entries_are_capped():
    async def scenario() -> int:
        app = MakeCodeTuiApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            log = app.query_one("#background-log")
            for index in range(RICHLOG_MAX_ENTRIES + 150):
                post_tui(TuiRegion.BACKGROUND, f"entry-{index}")
            await _settle(pilot, rounds=10)
            return len(log._render_entries)

    assert asyncio.run(scenario()) <= RICHLOG_MAX_ENTRIES


def test_simple_tail_preview_keeps_only_the_latest_lines():
    from system.stream_render import (
        SIMPLE_TAIL_MAX_CHARS,
        SIMPLE_TAIL_MAX_LINES,
        _simple_tail_preview,
    )

    assert _simple_tail_preview("") == ""

    short = "\n".join(f"line-{i}" for i in range(SIMPLE_TAIL_MAX_LINES))
    assert _simple_tail_preview(short) == short

    long_text = "\n".join(f"line-{i}" for i in range(SIMPLE_TAIL_MAX_LINES + 5))
    preview = _simple_tail_preview(long_text).splitlines()
    assert preview == [f"line-{i}" for i in range(5, 5 + SIMPLE_TAIL_MAX_LINES)]

    # 整面没有换行的文字：按行截断等于没截断，必须靠字数兜底
    wall = "字" * (SIMPLE_TAIL_MAX_CHARS * 4)
    preview_wall = _simple_tail_preview(wall)
    assert len(preview_wall) == SIMPLE_TAIL_MAX_CHARS
    assert wall.endswith(preview_wall), "预览必须是这一段的结尾，而不是开头"


def _collect_tail_payloads(simple: bool, text: str):
    """跑一遍流式渲染，收集所有 tail 事件携带的 payload。"""
    from system import stream_render
    from system.stream_render import StreamRenderer

    captured = []
    original = stream_render.post_tui

    def fake_post_tui(region, payload=None, **kwargs):
        if kwargs.get("tail"):
            captured.append(payload)

    async def single_chunk_stream():
        # 一次性整段吐出、且不含空行 → 永远不会触发段落提交，
        # 也就是「长段落一直不提交」这个最坏情况。
        yield {"type": "text", "content": text}

    tui_mode.set_simple_mode(simple)
    stream_render.post_tui = fake_post_tui
    try:
        renderer = StreamRenderer()
        renderer.tail_update_interval = 0.0
        asyncio.run(renderer.render_text_stream_async(single_chunk_stream()))
    finally:
        stream_render.post_tui = original
        tui_mode.set_simple_mode(False)
    return captured


def test_simple_mode_keeps_streaming_a_long_single_paragraph_visible():
    """simple 模式下长段落 / 长代码块在整段提交前也必须持续可见。

    尾部预览是贴着底部的固定条（`.pane-tail` max-height: 8）且裁的是开头的行，
    所以必须截取末尾若干行；否则整段提交前界面看起来是卡死的。
    """
    from system.stream_render import SIMPLE_TAIL_MAX_LINES

    text = "\n".join(f"line-{i}" for i in range(30))

    simple_payloads = [p for p in _collect_tail_payloads(True, text) if p != ""]
    assert simple_payloads, "simple 模式下长段落完全没有尾部预览，界面会看起来卡死"
    for payload in simple_payloads:
        assert isinstance(payload, Text)
        assert len(payload.plain.splitlines()) <= SIMPLE_TAIL_MAX_LINES
    # 预览内容是最新的行，而不是段落开头
    assert simple_payloads[-1].plain.splitlines()[-1] == "line-29"

    full_payloads = [p for p in _collect_tail_payloads(False, text) if p != ""]
    assert full_payloads, "完整模式行为不应被改动"
    # 完整模式不做截断，整段缓冲都贴在尾部
    assert len(full_payloads[-1].plain.splitlines()) == 30


def test_simple_mode_clears_the_tail_when_the_stream_ends():
    text = "\n".join(f"line-{i}" for i in range(10))
    payloads = _collect_tail_payloads(True, text)
    assert payloads[-1] == "", "流结束时必须把尾部预览清空，否则会残留旧内容"


def test_tail_event_drives_the_real_tail_widget():
    """走真实控件：尾部预览事件必须让预览条可见、内容可读，清空事件必须把它藏起来。

    分层说明：**截断**逻辑在 `StreamRenderer._update_tail`（由
    `test_simple_mode_keeps_streaming_a_long_single_paragraph_visible` 覆盖）；
    这一层只验证「事件 → 控件状态」这条线是通的，顺带看住
    `_static_renderable` 能真正读回控件内容。
    """
    from system.stream_render import SIMPLE_TAIL_MAX_LINES, _simple_tail_preview

    text = "\n".join(f"line-{i}" for i in range(30))

    async def scenario(simple: bool) -> tuple[bool, str, bool]:
        app = MakeCodeTuiApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            tui_mode.set_simple_mode(simple)
            app.apply_simple_mode()
            await pilot.pause()

            # 投递 StreamRenderer 在这一层实际会发出的内容
            payload = _simple_tail_preview(text) if simple else text
            post_tui(TuiRegion.CONTENT, Text(payload), tail=True)
            await pilot.pause()
            tail = app.query_one("#content-tail", Static)
            visible = tail.has_class("pane-tail-visible")
            plain = _renderable_plain_text(_static_renderable(tail))

            # 清空事件
            post_tui(TuiRegion.CONTENT, "", tail=True)
            await pilot.pause()
            still_visible = tail.has_class("pane-tail-visible")
            return visible, plain, still_visible

    visible, plain, still_visible = asyncio.run(scenario(True))
    assert visible, "simple 模式下尾部预览条必须可见，否则长段落期间界面是静止的"
    lines = plain.splitlines()
    assert lines, "没能从尾部控件读回内容（_static_renderable 在你的 textual 版本上失效了）"
    assert lines[-1] == "line-29", f"预览应是最新内容，实际结尾：{lines[-1]!r}"
    assert len(lines) <= SIMPLE_TAIL_MAX_LINES
    assert not still_visible, "清空事件必须真的把预览条藏起来"

    # 完整模式行为不变：不截断，整段缓冲都贴在尾部
    visible_full, plain_full, _ = asyncio.run(scenario(False))
    assert visible_full
    assert len(plain_full.splitlines()) == 30
