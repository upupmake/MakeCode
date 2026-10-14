import sys

import pytest
from textual.widgets import Footer

from system import stream_cancel
from system.tui_app import MakeCodeTuiApp


def _footer_entries(app: MakeCodeTuiApp) -> list[tuple[str, str]]:
    footer = app.screen.query_one(Footer)
    return [(child.key, child.description) for child in footer.children]


def _idle_footer() -> list[tuple[str, str]]:
    return [
        ("ctrl+p", "切换计划/执行"),
        ("ctrl+n", "换行"),
    ]


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
