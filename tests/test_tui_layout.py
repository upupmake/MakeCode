import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from system.console_render import _render_startup_banner
from system.tool_history import TOOL_EXECUTION_HISTORY
from system.tui_app import MakeCodeTuiApp, TuiBridge
from system.tui_modals import ChoiceModal, ToolHistoryModal
from utils.skills import SkillLoader


TEST_LAYOUT_RATIOS = {
    "content": 2,
    "tools": 2,
    "task": 2,
    "background": 3,
    "sub_agent": 1,
}


@pytest.fixture(autouse=True)
def isolate_tui_layout_config(monkeypatch):
    monkeypatch.setattr("system.tui_app.load_layout_ratios", lambda: dict(TEST_LAYOUT_RATIOS))


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
async def test_top_bar_displays_and_refreshes_conversation_title():
    current_title = {"value": None}
    app = MakeCodeTuiApp(
        conversation_title_provider=lambda: current_title["value"],
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        title = app.query_one("#top-title")

        assert str(title.render()) == "MakeCode"
        assert title.tooltip is None

        current_title["value"] = "优化标题展示"
        app.refresh_status()
        await pilot.pause()

        assert str(title.render()) == "MakeCode · 优化标题展示"
        assert title.tooltip == "优化标题展示"
        assert app.title == "MakeCode · 优化标题展示"

        current_title["value"] = None
        app.refresh_status()
        await pilot.pause()

        assert str(title.render()) == "MakeCode"
        assert title.tooltip is None


@pytest.mark.anyio
async def test_clicking_conversation_title_keeps_input_hidden_until_regeneration_finishes():
    regeneration_started = asyncio.Event()
    finish_regeneration = asyncio.Event()

    async def regenerate_title():
        regeneration_started.set()
        await finish_regeneration.wait()

    app = MakeCodeTuiApp(
        conversation_title_provider=lambda: "现有标题",
        conversation_title_regenerate_handler=regenerate_title,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#top-title")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceModal)

        await pilot.press("enter")
        await asyncio.wait_for(regeneration_started.wait(), timeout=1)
        await pilot.pause()

        bottom_grid = app.query_one("#bottom-grid")
        input_box = app.query_one("#input-box")
        assert app._agent_loop_active
        assert bottom_grid.has_class("hidden")
        assert input_box.has_class("hidden")
        assert app._submit_lock.locked()

        finish_regeneration.set()
        for _ in range(10):
            await pilot.pause()
            if not app._agent_loop_active:
                break

        assert not app._agent_loop_active
        assert not bottom_grid.has_class("hidden")
        assert not input_box.has_class("hidden")
        assert not app._submit_lock.locked()


@pytest.mark.anyio
async def test_clicking_conversation_title_does_not_open_modal_while_agent_is_active():
    app = MakeCodeTuiApp(
        conversation_title_provider=lambda: "现有标题",
        conversation_title_regenerate_handler=Mock(),
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        app.set_agent_loop_active(True)

        await pilot.click("#top-title")
        await pilot.pause()

        assert not isinstance(app.screen, ChoiceModal)
        assert not app._modal_active


@pytest.mark.anyio
async def test_quick_panel_toggle_follows_actual_title_width():
    current_title = {"value": None}
    app = MakeCodeTuiApp(
        conversation_title_provider=lambda: current_title["value"],
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        title = app.query_one("#top-title")
        quick_toggle = app.query_one("#quick-panel-toggle")
        short_title_width = title.region.width

        assert quick_toggle.region.x == title.region.x + short_title_width + 1

        current_title["value"] = "优化标题展示"
        app.refresh_status()
        await pilot.pause()

        assert title.region.width > short_title_width
        assert quick_toggle.region.x == title.region.x + title.region.width + 1


@pytest.mark.anyio
async def test_title_keeps_header_status_visible_on_compact_layout():
    app = MakeCodeTuiApp(
        header_info_provider=lambda: "workspace · status",
        conversation_title_provider=lambda: "这是一个很长的对话标题用于验证窄屏省略展示",
    )

    async with app.run_test(size=(86, 40)) as pilot:
        await pilot.pause()

        title = app.query_one("#top-title")
        status = app.query_one("#top-status")
        assert title.region.width > 0
        assert status.region.width > title.region.width
        assert title.tooltip == "这是一个很长的对话标题用于验证窄屏省略展示"
        assert "workspace · status" in str(status.render())


@pytest.mark.anyio
async def test_f7_opens_tool_history_with_current_messages_and_advertises_shortcut():
    TOOL_EXECUTION_HISTORY.clear()
    execution_id = TOOL_EXECUTION_HISTORY.start("FileRead", {"path": "README.md"})
    TOOL_EXECUTION_HISTORY.finish(execution_id, "live history contents")
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_read",
                "name": "FileRead",
                "arguments": {"path": "README.md"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "name": "FileRead",
            "content": "current message contents",
        },
    ]
    app = MakeCodeTuiApp(messages_provider=lambda: messages)

    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert "F7 History" in str(app.query_one("#tools-pane").border_title)

            await pilot.press("f7")
            await pilot.pause()

            assert isinstance(app.screen, ToolHistoryModal)
            assert len(app.screen._row_values) == 1
            assert app.screen._row_values[0].tool_name == "FileRead"
            assert app.screen._row_values[0].result == "live history contents"
            assert app.screen._message_history.snapshot()[0].result == "current message contents"
            app.screen.action_close()
            await pilot.pause()
            assert not isinstance(app.screen, ToolHistoryModal)
            assert app._modal_active is False
    finally:
        TOOL_EXECUTION_HISTORY.clear()


@pytest.mark.anyio
async def test_compact_layout_switches_between_main_and_runtime_panes():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        left_column = app.query_one("#left-column")
        right_column = app.query_one("#right-column")
        quick_toggle = app.query_one("#quick-panel-toggle")
        toggle = app.query_one("#compact-pane-toggle")

        assert not left_column.has_class("hidden")
        assert right_column.has_class("hidden")
        assert not toggle.has_class("hidden")
        assert toggle.region.x == quick_toggle.region.x + quick_toggle.region.width + 1
        assert str(toggle.label) == "运行面板 F6"

        await pilot.press("f6")
        await pilot.pause()

        assert left_column.has_class("hidden")
        assert not right_column.has_class("hidden")
        assert str(toggle.label) == "主面板 F6"

        await pilot.click("#compact-pane-toggle")
        await pilot.pause()

        assert not left_column.has_class("hidden")
        assert right_column.has_class("hidden")
        assert str(toggle.label) == "运行面板 F6"


@pytest.mark.anyio
async def test_input_area_is_nested_in_left_column():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        main_grid = app.query_one("#main-grid")
        left_column = app.query_one("#left-column")
        right_column = app.query_one("#right-column")
        bottom_grid = app.query_one("#bottom-grid")
        input_box = app.query_one("#input-box")

        assert bottom_grid.parent is left_column
        assert input_box.parent is bottom_grid
        assert bottom_grid.region.x == left_column.region.x
        assert bottom_grid.region.width == left_column.region.width
        assert input_box.region.height == 5
        assert right_column.region.height == main_grid.region.height


@pytest.mark.anyio
async def test_wide_layout_keeps_both_columns_and_hides_compact_toggle():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        left_column = app.query_one("#left-column")
        right_column = app.query_one("#right-column")
        main_grid = app.query_one("#main-grid")
        toggle = app.query_one("#compact-pane-toggle")

        assert not left_column.has_class("hidden")
        assert not right_column.has_class("hidden")
        assert toggle.has_class("hidden")
        assert left_column.region.width + right_column.region.width == main_grid.region.width
        assert abs(right_column.region.width / main_grid.region.width - 0.28) < 0.02

        await pilot.press("f6")
        await pilot.pause()

        assert not left_column.has_class("hidden")
        assert not right_column.has_class("hidden")
        assert toggle.has_class("hidden")


@pytest.mark.anyio
async def test_runtime_pane_selection_resets_after_returning_to_wide_layout():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f6")
        await pilot.resize_terminal(180, 40)
        await pilot.pause()
        await pilot.resize_terminal(100, 40)
        await pilot.pause()

        assert not app.query_one("#left-column").has_class("hidden")
        assert app.query_one("#right-column").has_class("hidden")
        assert str(app.query_one("#compact-pane-toggle").label) == "运行面板 F6"


@pytest.mark.anyio
async def test_quick_panel_tool_history_button_routes_to_history_command():
    app = MakeCodeTuiApp()
    app._run_quick_command = Mock()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#quick-panel-toggle")
        await pilot.pause()

        button = app.query_one("#quick-tool-history")
        assert "工具历史" in str(button.label)

        await pilot.click("#quick-tool-history")
        await pilot.pause()

        app._run_quick_command.assert_called_once_with("/tool-history")


@pytest.mark.anyio
async def test_compact_quick_panel_wraps_all_actions_into_two_rows():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(86, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#quick-panel-toggle")
        await pilot.pause()

        buttons = list(app.query(".quick-panel-button"))
        row_counts = {}
        for button in buttons:
            row_counts[button.region.y] = row_counts.get(button.region.y, 0) + 1

        assert len(buttons) == 10
        assert sorted(row_counts.values()) == [5, 5]
        assert all(button.region.width > 0 for button in buttons)
        assert all(button.region.x + button.region.width <= app.size.width for button in buttons)
        assert all(
            button.region.y + button.region.height <= app.query_one("#main-grid").region.y
            for button in buttons
        )
        assert str(app.query_one("#quick-panel-toggle").label) == "▾ 快捷"


@pytest.mark.anyio
async def test_quick_panel_returns_to_one_row_after_widening_terminal():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#quick-panel-toggle")
        await pilot.pause()
        assert len({button.region.y for button in app.query(".quick-panel-button")}) == 2

        await pilot.resize_terminal(180, 40)
        await pilot.pause()

        buttons = list(app.query(".quick-panel-button"))
        assert len({button.region.y for button in buttons}) == 1
        assert all(button.region.x + button.region.width <= app.size.width for button in buttons)
        assert str(app.query_one("#quick-panel-toggle").label) == "▾ 快捷面板"


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


@pytest.mark.anyio
async def test_skills_panel_filters_draft_changes_and_discards_them_on_cancel(tmp_path):
    skills_dir = tmp_path / "skills"
    for name, description in [
        ("alpha", "Handles source code"),
        ("beta", "Writes release notes"),
    ]:
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
            encoding="utf-8",
        )
    config_file = tmp_path / "disabled_skills.json"
    loader = SkillLoader(skills_dir, config_file)
    app = MakeCodeTuiApp()

    async with app.run_test(size=(140, 40)) as pilot:
        result = asyncio.get_running_loop().create_future()
        app.open_skills_config_modal(loader, result)
        await pilot.pause()
        modal = app.screen

        search = modal.query_one("#skills-search")
        search.value = "release notes"
        await pilot.pause()
        assert [entry["name"] for entry in modal._filtered_entries] == ["beta"]

        search.value = "alpha"
        await pilot.pause()
        assert [entry["name"] for entry in modal._filtered_entries] == ["alpha"]

        search.value = ""
        await pilot.pause()
        skills_list = modal.query_one("#skills-list")
        skills_list.focus()
        original_rows = tuple(skills_list.children)
        original_index = skills_list.index
        original_scroll_y = skills_list.scroll_y
        await pilot.press("enter")
        await pilot.pause()
        assert tuple(skills_list.children) == original_rows
        assert skills_list.index == original_index
        assert skills_list.scroll_y == original_scroll_y
        assert "alpha" not in loader.disabled_skill_names
        assert not config_file.exists()
        assert "启用 0，禁用 1" in str(modal.query_one("#skills-confirm").label)

        status_filter = modal.query_one("#skills-status-filter")
        status_filter.value = "disabled"
        await pilot.pause()
        assert [entry["name"] for entry in modal._filtered_entries] == ["alpha"]

        status_filter.value = "enabled"
        await pilot.pause()
        assert [entry["name"] for entry in modal._filtered_entries] == ["beta"]

        await pilot.click("#skills-close")
        await pilot.pause()
        assert await result == "closed"
        assert not config_file.exists()
        assert "alpha" in loader.skills


@pytest.mark.anyio
async def test_skills_panel_concurrent_reloads_keep_last_row_toggleable(tmp_path):
    skills_dir = tmp_path / "skills"
    for index in range(30):
        name = f"skill-{index:02d}"
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Description {index}\n---\nbody\n",
            encoding="utf-8",
        )
    loader = SkillLoader(skills_dir, tmp_path / "disabled_skills.json")
    app = MakeCodeTuiApp()

    async with app.run_test(size=(140, 40)) as pilot:
        result = asyncio.get_running_loop().create_future()
        app.open_skills_config_modal(loader, result)
        await pilot.pause()
        modal = app.screen
        skills_list = modal.query_one("#skills-list")

        modal._reload_rows()
        modal._reload_rows()
        await pilot.pause()
        skills_list.focus()
        skills_list.index = len(skills_list.children) - 1
        await pilot.press("enter")
        await pilot.pause()
        assert modal._draft_states["skill-29"] is False

        await pilot.press("space")
        await pilot.pause()
        assert modal._draft_states["skill-29"] is True

        await pilot.click(skills_list.children[-1])
        await pilot.pause()
        assert modal._draft_states["skill-29"] is False
        assert len(skills_list.children) == len(modal._filtered_entries) == 30

        await pilot.click("#skills-close")
        await pilot.pause()
        assert await result == "closed"


@pytest.mark.anyio
async def test_skills_panel_toggle_preserves_scrolled_row_and_list_items(tmp_path):
    skills_dir = tmp_path / "skills"
    for index in range(30):
        name = f"skill-{index:02d}"
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Description {index}\n---\nbody\n",
            encoding="utf-8",
        )
    loader = SkillLoader(skills_dir, tmp_path / "disabled_skills.json")
    app = MakeCodeTuiApp()

    async with app.run_test(size=(140, 40)) as pilot:
        result = asyncio.get_running_loop().create_future()
        app.open_skills_config_modal(loader, result)
        await pilot.pause()
        modal = app.screen
        skills_list = modal.query_one("#skills-list")
        skills_list.focus()
        skills_list.index = 20
        await pilot.pause()
        original_rows = tuple(skills_list.children)
        original_scroll_y = skills_list.scroll_y

        assert original_scroll_y > 0
        await pilot.press("enter")
        await pilot.pause()

        assert tuple(skills_list.children) == original_rows
        assert skills_list.index == 20
        assert skills_list.scroll_y == original_scroll_y
        assert modal._filtered_entries[20]["enabled"] is False

        await pilot.click("#skills-close")
        await pilot.pause()
        assert await result == "closed"


@pytest.mark.anyio
async def test_skills_panel_applies_draft_once_and_reports_change_counts(tmp_path):
    skills_dir = tmp_path / "skills"
    for name in ("alpha", "beta"):
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n---\nbody\n",
            encoding="utf-8",
        )
    config_file = tmp_path / "disabled_skills.json"
    config_file.write_text('["beta"]', encoding="utf-8")
    loader = SkillLoader(skills_dir, config_file)
    app = MakeCodeTuiApp()

    async with app.run_test(size=(140, 40)) as pilot:
        result = asyncio.get_running_loop().create_future()
        app.open_skills_config_modal(loader, result)
        await pilot.pause()
        modal = app.screen
        skills_list = modal.query_one("#skills-list")
        skills_list.focus()

        await pilot.press("enter")
        await pilot.pause()
        skills_list.index = 1
        await pilot.press("enter")
        await pilot.pause()

        assert json.loads(config_file.read_text(encoding="utf-8")) == ["beta"]
        assert "启用 1，禁用 1" in str(modal.query_one("#skills-confirm").label)

        await pilot.click("#skills-confirm")
        await pilot.pause()

        assert await result == {"action": "applied", "enabled": 1, "disabled": 1}
        assert json.loads(config_file.read_text(encoding="utf-8")) == ["alpha"]
        assert "alpha" not in loader.skills
        assert "beta" in loader.skills
