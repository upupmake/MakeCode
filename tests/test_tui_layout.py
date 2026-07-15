import pytest

from system.console_render import _render_startup_banner
from system.tui_app import MakeCodeTuiApp


@pytest.mark.anyio
async def test_content_pane_minimum_width_keeps_startup_banner_on_six_lines():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(86, 40)) as pilot:
        await pilot.pause()
        _render_startup_banner()
        await pilot.pause()

        content_pane = app.query_one("#content-pane")
        content_log = app.query_one("#content-log")
        tools_pane = app.query_one("#tools-pane")

        assert content_pane.region.width == 86
        assert tools_pane.region.width == content_pane.region.width
        assert len(content_log.lines) == 10
