"""Content 面板在长历史下的渲染路径：面板高亮、批量回放、尾部原子提交与按需布局。"""
from unittest.mock import patch
import sys

import pytest
from rich.text import Text

from system.tui_app import MakeCodeTuiApp
from system.tui_types import TuiEvent, TuiRegion


def _post_history(app, count=40):
    for index in range(count):
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, f"历史内容 {index}\n\n"))


@pytest.mark.anyio
async def test_pane_active_toggle_restyles_only_the_pane_not_its_descendants():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _post_history(app)
        await pilot.pause()
        pane = app.query_one("#content-pane")

        with patch.object(app.stylesheet, "update_nodes", wraps=app.stylesheet.update_nodes) as update_nodes:
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, None, active=True))
            assert pane.has_class("pane-active")
            assert pane.styles.border_top[0] == "heavy"
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, None, active=False))
            assert not pane.has_class("pane-active")
            assert pane.styles.border_top[0] == "round"

        # 只对面板节点自身应用样式；Content 里的几十个子控件不会被同步重算
        assert [list(call.args[0]) for call in update_nodes.call_args_list] == [[pane], [pane]]


@pytest.mark.anyio
async def test_pane_active_stays_until_every_active_scope_is_released():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#content-pane")

        with patch.object(app.stylesheet, "update_nodes", wraps=app.stylesheet.update_nodes) as update_nodes:
            for active in (True, True, False):
                app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, None, active=active))
            assert pane.has_class("pane-active")
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, None, active=False))
            assert not pane.has_class("pane-active")

        assert update_nodes.call_count == 2  # 只在首次进入和最后一次离开时应用样式


@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
@pytest.mark.anyio
async def test_batch_render_suspends_layout_and_scrolls_once_when_it_ends():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        content = app.query_one("#content-log")
        idle_height = content.virtual_size.height  # 空面板的虚拟高度就是视口高度

        with patch.object(app, "call_after_refresh", wraps=app.call_after_refresh) as scheduled:
            app.begin_batch_render(force_scroll=True)
            _post_history(app, 60)
            await pilot.pause()
            await pilot.pause()
            assert content.virtual_size.height == idle_height  # 批次内不做布局
            assert content.max_scroll_y == 0
            assert scheduled.call_count == 0  # 批次内不逐事件排队“刷新后滚到底部”

            app.end_batch_render()
            await pilot.pause()
            await pilot.pause()

            assert content.virtual_size.height >= 60
            assert content.max_scroll_y > 0
            assert content.scroll_y == content.max_scroll_y
            assert scheduled.call_count <= 8  # 与事件数量无关


@pytest.mark.anyio
async def test_nested_batch_render_resumes_layout_only_when_outermost_batch_ends():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        content = app.query_one("#content-log")
        idle_height = content.virtual_size.height

        app.begin_batch_render()
        app.begin_batch_render()
        _post_history(app, 30)
        app.end_batch_render()
        await pilot.pause()
        await pilot.pause()
        assert content.virtual_size.height == idle_height

        app.end_batch_render()
        await pilot.pause()
        await pilot.pause()
        assert content.virtual_size.height >= 30


@pytest.mark.anyio
async def test_batch_render_resumes_layout_even_when_finishing_fails():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        content = app.query_one("#content-log")

        app.begin_batch_render(force_scroll=True)
        _post_history(app, 30)
        with patch.object(app, "scroll_all_panes_to_bottom", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                app.end_batch_render()
        await pilot.pause()
        await pilot.pause()

        assert content.virtual_size.height >= 30  # 收尾出错也必须恢复重绘与布局


@pytest.mark.anyio
async def test_content_event_with_tail_after_mounts_block_and_updates_tail_together():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        content = app.query_one("#content-log")
        tail = app.query_one("#content-tail")

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "第一段", tail_after=Text("剩余尾部")))
        await pilot.pause()
        assert len(content.children) == 1
        assert tail.has_class("pane-tail-visible")
        assert tail.content.plain == "剩余尾部"

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "第二段", tail_after=""))
        await pilot.pause()
        assert len(content.children) == 2
        assert not tail.has_class("pane-tail-visible")


@pytest.mark.anyio
async def test_content_event_without_tail_after_leaves_tail_untouched():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        tail = app.query_one("#content-tail")

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("流式尾部"), tail=True))
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "只挂载，不动尾部"))
        await pilot.pause()

        assert tail.has_class("pane-tail-visible")
        assert tail.content.plain == "流式尾部"


@pytest.mark.anyio
async def test_reasoning_block_with_tail_after_updates_tail_while_mounting_inside_collapsible():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        tail = app.query_one("#content-tail")

        app.handle_tui_event(
            TuiEvent(TuiRegion.REASONING, None, collapsible_title="🧠 Reasoning", collapsible_open=True)
        )
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考段落", tail_after=Text("思考尾部")))
        await pilot.pause()

        assert tail.has_class("pane-tail-visible")
        assert tail.content.plain == "思考尾部"


@pytest.mark.anyio
async def test_log_region_event_with_tail_after_updates_that_regions_tail():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        tail = app.query_one("#background-tail")

        app.handle_tui_event(TuiEvent(TuiRegion.BACKGROUND, "后台输出", tail_after=Text("后台尾部")))
        await pilot.pause()
        assert tail.has_class("pane-tail-visible")
        assert tail.content.plain == "后台尾部"

        app.handle_tui_event(TuiEvent(TuiRegion.BACKGROUND, "后台输出 2", tail_after=""))
        await pilot.pause()
        assert not tail.has_class("pane-tail-visible")


@pytest.mark.anyio
async def test_tail_grows_to_fit_without_relayout_when_text_fits_and_resets_after_hiding():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tail = app.query_one("#content-tail")
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("一行"), tail=True))
        await pilot.pause()
        one_line_height = tail.size.height
        assert one_line_height == 1

        def layout_requests(action):
            with patch.object(tail, "refresh", wraps=tail.refresh) as refresh:
                action()
            return [call for call in refresh.call_args_list if call.kwargs.get("layout")]

        same_height = layout_requests(
            lambda: app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("另一行"), tail=True))
        )
        assert same_height == []  # 高度没变：只重绘，不触发整个面板重排
        assert tail.content.plain == "另一行"

        taller = layout_requests(
            lambda: app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("很长的尾部文本" * 30), tail=True))
        )
        assert taller  # 换行后变高：必须重新布局
        await pilot.pause()
        grown_height = tail.size.height
        assert grown_height > one_line_height
        assert grown_height == min(tail.visual.get_height(tail.styles, tail.size.width), 8)  # 没有被裁剪

        shorter = layout_requests(
            lambda: app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("短"), tail=True))
        )
        assert shorter == []  # 变矮不缩回，也不触发重排
        await pilot.pause()
        assert tail.size.height == grown_height

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "", tail=True))
        await pilot.pause()
        assert not tail.has_class("pane-tail-visible")
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, Text("一行"), tail=True))
        await pilot.pause()
        assert tail.size.height == one_line_height  # 隐藏后撑高的最小高度已清除
