import sys
from concurrent.futures import Future
from unittest.mock import patch

import pytest
from textual.events import MouseDown
from textual.geometry import Offset
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from system import stream_cancel
from system.tui_app import MakeCodeTuiApp
from system.tui_modals import InfoPanelModal


def _footer_entries(app: MakeCodeTuiApp) -> list[tuple[str, str]]:
    footer = app.screen.query_one(Footer)
    return [
        (child.key, child.description)
        for child in footer.query(FooterKey)
    ]


def _idle_footer() -> list[tuple[str, str]]:
    return [
        ("ctrl+p", "切换计划/执行"),
        ("ctrl+n", "换行"),
    ]


def _version_label_laid_out(app: MakeCodeTuiApp) -> bool:
    labels = list(app.query("#footer-version"))
    return len(labels) == 1 and labels[0].region.width > 0


async def _pump_until(pilot, predicate, attempts: int = 60) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause()
    return predicate()


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_footer_shows_chinese_labels_and_hides_temporary_query_until_enabled():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        assert _footer_entries(app) == _idle_footer()

        app.set_agent_loop_active(True)
        await pilot.pause()
        assert _footer_entries(app) == []
        assert app._mode_label == "ACT"
        await pilot.press("ctrl+p", "ctrl+n")
        await pilot.pause()
        assert app._mode_label == "ACT"
        assert app.query_one("#input-box").text == ""

        app.set_temporary_query_enabled(True)
        await pilot.pause()
        assert _footer_entries(app) == [
            ("ctrl+g", "追加临时指令"),
        ]

        app.set_agent_loop_active(False)
        await pilot.pause()
        assert _footer_entries(app) == _idle_footer()


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_footer_plan_mode_label_follows_current_mode():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert ("ctrl+p", "切换计划/执行") in _footer_entries(app)

        await pilot.press("ctrl+p")
        await pilot.pause()
        assert ("ctrl+p", "切换计划/执行") in _footer_entries(app)


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_footer_shows_cancel_only_while_response_or_terminal_is_cancellable():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        assert ("ctrl+c", "取消回复") not in _footer_entries(app)

        stream_cancel.start_cancel_listener()
        await pilot.pause()
        assert _footer_entries(app)[0] == ("ctrl+c", "取消回复")

        stream_cancel.stop_cancel_listener()
        await pilot.pause()
        assert ("ctrl+c", "取消回复") not in _footer_entries(app)

        stream_cancel.start_terminal_command()
        await pilot.pause()
        assert ("ctrl+c", "取消回复") in _footer_entries(app)

        stream_cancel.stop_terminal_command()
        await pilot.pause()
        assert _footer_entries(app) == _idle_footer()


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_input_title_shows_cancel_and_temporary_query_hints_only_when_available():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        assert "Ctrl+C 取消回复" not in str(input_box.border_title)
        assert "Ctrl+G 运行时介入" not in str(input_box.border_title)
        assert "Ctrl+N 换行" in str(input_box.border_title)
        assert "Ctrl+P 切换" in str(input_box.border_title)

        stream_cancel.start_cancel_listener()
        await pilot.pause()
        assert "Ctrl+C 取消回复" in str(input_box.border_title)

        stream_cancel.stop_cancel_listener()
        app.set_agent_loop_active(True)
        app.set_temporary_query_enabled(True)
        await pilot.pause()
        assert "Ctrl+C 取消回复" not in str(input_box.border_title)
        assert "Ctrl+G 运行时介入" in str(input_box.border_title)
        assert "Ctrl+N 换行" not in str(input_box.border_title)
        assert "Ctrl+P 切换" not in str(input_box.border_title)


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_version_label_repeated_text_clicks_during_agent_loop_do_not_select():
    app = MakeCodeTuiApp()
    with patch("system.tui_app.fetch_latest_version_info") as fetch_latest:
        async with app.run_test(size=(192, 48)) as pilot:
            app.set_agent_loop_active(True)
            assert await _pump_until(pilot, lambda: _version_label_laid_out(app))

            for _ in range(8):
                assert await _pump_until(pilot, lambda: _version_label_laid_out(app))
                await pilot.mouse_down("#footer-version", offset=(3, 0))
                assert app.screen._select_state is None
                await pilot.mouse_up("#footer-version", offset=(3, 0))
                app.refresh_bindings()
                assert await _pump_until(pilot, lambda: _version_label_laid_out(app))
                await pilot.click("#footer-version", offset=(3, 0))
                await pilot.pause()
                assert app._agent_loop_active
                assert app._release_notes_modal is None

            fetch_latest.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("other_modal", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_version_label_stale_mouse_hit_after_footer_recompose_does_not_crash(other_modal):
    app = MakeCodeTuiApp()
    async with app.run_test(size=(192, 48)) as pilot:
        app.set_agent_loop_active(True)
        await pilot.pause()
        old_label = app.query_one("#footer-version")
        assert await _pump_until(pilot, lambda: old_label.region.width > 0)
        x, y = old_label.content_region.x + 2, old_label.content_region.y
        footer = app.query_one(Footer)
        footer.call_later(footer.recompose)
        assert await _pump_until(pilot, lambda: old_label.parent is None)

        if other_modal:
            app.call_later(app.push_screen, InfoPanelModal("其他窗口", "测试内容"))
            assert await _pump_until(pilot, lambda: isinstance(app.screen, InfoPanelModal))
        screen = app.screen
        event = MouseDown(
            None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y,
        )
        with patch.object(
            screen, "get_widget_and_offset_at", return_value=(old_label, Offset(0, 0)),
        ):
            screen._forward_event(event)
        assert screen._select_state is None


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
async def test_version_label_text_clicks_do_not_interfere_with_another_modal():
    app = MakeCodeTuiApp()
    with patch(
        "system.tui_app.fetch_latest_version_info",
        return_value={"version": "9.9.9", "release_log": "## Notes"},
    ) as fetch_latest:
        async with app.run_test(size=(192, 48)) as pilot:
            assert await _pump_until(pilot, lambda: _version_label_laid_out(app))
            label = app.query_one("#footer-version")
            position = (label.content_region.x + 2, label.content_region.y)
            app.call_later(app.open_info_panel_modal, "其他窗口", "测试内容", Future())
            assert await _pump_until(pilot, lambda: isinstance(app.screen, InfoPanelModal))
            modal = app.screen
            assert isinstance(modal, InfoPanelModal)

            for _ in range(8):
                await pilot.click(offset=position)
                assert app.screen is modal
            fetch_latest.assert_not_called()

            await pilot.press("q")
            assert await _pump_until(pilot, lambda: app._modal_active is False)
            assert await _pump_until(pilot, lambda: _version_label_laid_out(app))
            await pilot.click("#footer-version", offset=(3, 0))
            assert await _pump_until(pilot, lambda: fetch_latest.call_count == 1)
            assert isinstance(app.screen, InfoPanelModal)
            assert app.screen is not modal
            fetch_latest.assert_called_once_with(raise_errors=True)
