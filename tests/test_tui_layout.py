import threading
from unittest.mock import Mock

import pytest

from system.console_render import _render_startup_banner
from system.tui_app import MakeCodeTuiApp, TuiBridge


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


@pytest.mark.anyio
async def test_runtime_info_displays_client_request_state():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_client_request_active(True)
        await pilot.pause()
        runtime_info = app.query_one("#runtime-info-bar")
        assert "Client: REQUESTING" in str(runtime_info.render())

        app.set_client_request_active(False)
        await pilot.pause()
        assert "Client: REQUESTING" not in str(runtime_info.render())


@pytest.mark.anyio
async def test_runtime_info_displays_retry_count_without_agent_running():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "runtime")

    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_agent_loop_active(True)
        app.set_client_request_active(True, retry_count=2, max_retries=2)
        await pilot.pause()

        rendered = str(app.query_one("#runtime-info-bar").render())
        assert "Client: REQUESTING · RETRY 2/2" in rendered
        assert "Agent: RUNNING" not in rendered


def test_tui_bridge_keeps_request_active_until_all_requests_finish():
    bridge = TuiBridge()
    app = Mock()
    bridge._app = app
    bridge._app_thread_id = threading.get_ident()

    bridge.set_client_request_active(True)
    bridge.set_client_request_active(True)
    bridge.set_client_request_active(False)
    bridge.set_client_request_active(False)

    assert [call.args[0] for call in app.set_client_request_active.call_args_list] == [True, False]


def test_tui_bridge_tracks_retry_count_per_concurrent_request():
    bridge = TuiBridge()
    app = Mock()
    bridge._app = app
    bridge._app_thread_id = threading.get_ident()

    bridge.set_client_request_active(True, request_id=1)
    bridge.set_client_request_active(True, request_id=2)
    bridge.set_client_request_retry(1, 1, 2)
    bridge.set_client_request_retry(2, 2, 2)
    bridge.set_client_request_active(False, request_id=2)
    bridge.set_client_request_active(False, request_id=1)

    assert [call.args for call in app.set_client_request_active.call_args_list] == [
        (True, 0, 0),
        (True, 1, 2),
        (True, 2, 2),
        (True, 1, 2),
        (False, 0, 0),
    ]
