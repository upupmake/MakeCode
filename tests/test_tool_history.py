import json
from unittest.mock import Mock, patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label, TextArea

from system.tool_history import (
    TOOL_STATUS_BLOCKED,
    TOOL_STATUS_COMPACTED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_SUCCEEDED,
    ToolExecutionHistory,
    format_tool_arguments,
    format_tool_value,
)
from system.tui_modals import ToolHistoryModal


class ToolHistoryModalHost(App):
    def __init__(self, modal: ToolHistoryModal) -> None:
        super().__init__()
        self._modal = modal

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _build_modal_history() -> ToolExecutionHistory:
    history = ToolExecutionHistory()
    first = history.start("FileRead", {"path": "alpha.py"}, tool_call_id="call_read")
    history.finish(first, '{"needle":"result","count":2}')
    second = history.start(
        "RunTerminalCommand",
        {"command": "pytest -q"},
        tool_call_id="call_test",
        source="sub_agent",
        actor="#22 - Tester",
        task_id="22",
    )
    history.finish(second, "Error: failure", status=TOOL_STATUS_FAILED, error="failure")
    return history


def test_tool_history_records_searches_filters_and_summarizes_executions():
    history = ToolExecutionHistory()
    read_execution = history.start(
        "FileRead",
        {"path": "system/tui_app.py", "api_key": "secret"},
        tool_call_id="call_1",
    )
    history.finish(read_execution, "contains conversation title")
    terminal_execution = history.start(
        "RunTerminalCommand",
        {"command": "pytest -q"},
        tool_call_id="call_2",
        source="sub_agent",
        actor="#20 - Tester",
        task_id="20",
    )
    history.finish(
        terminal_execution,
        "Error: failed assertion",
        status=TOOL_STATUS_FAILED,
        error="failed assertion",
    )

    records = history.snapshot()
    assert [record.tool_name for record in records] == ["RunTerminalCommand", "FileRead"]
    assert records[1].arguments["api_key"] == "secret"
    assert history.query(text="conversation title")[0].tool_call_id == "call_1"
    assert history.query(source="sub_agent", status=TOOL_STATUS_FAILED)[0].task_id == "20"

    summaries = history.summaries()
    assert [(item.tool_name, item.total, item.succeeded, item.failed) for item in summaries] == [
        ("RunTerminalCommand", 1, 0, 1),
        ("FileRead", 1, 1, 0),
    ]


def test_tool_history_preserves_data_and_formats_arguments_as_indented_json():
    history = ToolExecutionHistory()
    execution_id = history.start(
        "McpCall",
        {
            "headers": {"Authorization": "Bearer abc.def"},
            "command": "TOKEN=secret-value run",
        },
    )
    history.finish(
        execution_id,
        {"token": "result-secret"},
        status=TOOL_STATUS_FAILED,
        error="Bearer error-secret",
    )

    record = history.snapshot()[0]
    assert record.arguments["headers"]["Authorization"] == "Bearer abc.def"
    assert record.arguments["command"] == "TOKEN=secret-value run"
    assert record.result == {"token": "result-secret"}
    assert record.error == "Bearer error-secret"
    assert format_tool_arguments(record.arguments) == (
        '{\n'
        '  "headers": {\n'
        '    "Authorization": "Bearer abc.def"\n'
        '  },\n'
        '  "command": "TOKEN=secret-value run"\n'
        '}'
    )
    assert format_tool_arguments('{"path":"old.py","recursive":true}') == (
        '{\n'
        '  "path": "old.py",\n'
        '  "recursive": true\n'
        '}'
    )
    assert format_tool_value('{"token":"result-secret","items":[1,2]}') == (
        '{\n'
        '  "token": "result-secret",\n'
        '  "items": [\n'
        '    1,\n'
        '    2\n'
        '  ]\n'
        '}'
    )
    assert format_tool_value("plain result") == "plain result"


def test_tool_formatting_decodes_nested_json_for_display_without_mutating_source():
    nested_json = json.dumps(
        {
            "content": "first line\n\nsecond line",
            "literal": r"first\nsecond",
            "count": 2,
        },
        ensure_ascii=False,
    )
    source = {"payload": nested_json, "enabled": True}

    rendered = format_tool_value(source)

    assert '"payload": {' in rendered
    assert '"content":\n      "\n      first line\n      \n      second line\n      "' in rendered
    assert '"literal": "first\\\\nsecond"' in rendered
    assert '"count": 2' in rendered
    assert '"enabled": true' in rendered
    assert '\\"content\\"' not in rendered
    assert source == {"payload": nested_json, "enabled": True}


def test_multiline_code_displays_quotes_without_adding_escape_characters():
    code = (
        'if not stripped or stripped[0] not in "{[":\n'
        '    literal = r\'\\"\''
    )
    source = {"content": code}

    rendered = format_tool_arguments(source)

    assert 'if not stripped or stripped[0] not in "{[":' in rendered
    assert r'not in \"{[\"' not in rendered
    assert '"content":\n    "\n    if not stripped or stripped[0] not in "{[":\n        literal = r\'\\"\'\n    "' in rendered
    assert source == {"content": code}


def test_tool_formatting_decodes_repeated_top_level_json_string_layers_for_display():
    structured = json.dumps(
        {"summary": "first line\nsecond line", "items": [1, 2]},
        ensure_ascii=False,
    )
    double_encoded = json.dumps(structured, ensure_ascii=False)

    rendered = format_tool_value(double_encoded)

    assert rendered.startswith("{\n")
    assert '"summary":\n    "\n    first line\n    second line\n    "' in rendered
    assert '"items": [' in rendered
    assert '\\"summary\\"' not in rendered


def test_tool_history_rebuilds_openai_and_normalized_checkpoint_messages():
    history = ToolExecutionHistory()
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_old",
                "type": "function",
                "function": {"name": "FileRead", "arguments": '{"path":"old.py"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_old",
            "name": "FileRead",
            "content": "old content",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_blocked",
                "name": "FileEdit",
                "arguments": {"path": "a.py"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_blocked",
            "name": "FileEdit",
            "content": "⛔ Plan Mode active: 'FileEdit' is blocked.",
            "is_error": True,
        },
    ]

    assert history.rebuild_from_messages(messages) == 2
    records = history.snapshot(newest_first=False)
    assert records[0].tool_name == "FileRead"
    assert records[0].arguments == '{"path":"old.py"}'
    assert records[0].result == "old content"
    assert records[0].status == TOOL_STATUS_SUCCEEDED
    assert records[0].recovered is True
    assert records[1].status == TOOL_STATUS_BLOCKED


def test_tool_history_marks_compacted_checkpoint_results_and_clears_old_state():
    history = ToolExecutionHistory()
    execution_id = history.start("ContentSearch", {"content_regex": "old"})
    history.finish(execution_id, "old live result")

    history.rebuild_from_messages([
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_compacted",
                "name": "ContentSearch",
                "arguments": "{}",
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_compacted",
            "name": "ContentSearch",
            "content": "[Previous ContentSearch result cleared, arguments were: {}]",
        },
    ])

    records = history.snapshot()
    assert len(records) == 1
    assert records[0].status == TOOL_STATUS_COMPACTED
    assert "old live result" not in records[0].result


@pytest.mark.anyio
async def test_tool_history_modal_searches_toggles_summary_and_shows_full_detail():
    history = _build_modal_history()
    modal = ToolHistoryModal(history)
    app = ToolHistoryModalHost(modal)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        search = modal.query_one("#tool-history-search")
        search.value = "needle"
        await pilot.pause()

        assert len(modal._row_values) == 1
        assert modal._row_values[0].tool_name == "FileRead"
        detail_text = modal.query_one("#tool-history-detail", TextArea).text
        assert 'Arguments\n─────────\n{\n  "path": "alpha.py"\n}' in detail_text
        assert 'Result\n──────\n{\n  "needle": "result",\n  "count": 2\n}' in detail_text
        assert "任务 ID:" not in detail_text
        assert "开始:" in detail_text
        assert "结束:" in detail_text
        assert "耗时:" in detail_text
        assert "历史重建:" not in detail_text
        assert "checkpoint 未记录" not in detail_text

        search.value = ""
        modal.action_toggle_view()
        await pilot.pause()

        assert {item.tool_name for item in modal._row_values} == {"FileRead", "RunTerminalCommand"}
        modal.query_one("#tool-history-list").index = next(
            index for index, item in enumerate(modal._row_values) if item.tool_name == "FileRead"
        )
        modal.action_open_detail()
        await pilot.pause()

        assert modal._view == "timeline"
        assert modal._tool_filter == "FileRead"
        assert len(modal._row_values) == 1


@pytest.mark.anyio
async def test_tool_history_modal_expands_multiline_json_strings():
    history = ToolExecutionHistory()
    execution_id = history.start(
        "FileEdit",
        {
            "edits": [{
                "search_content": "old line\n\nold code",
                "replace_content": "new line\n\nnew code",
            }],
        },
    )
    history.finish(
        execution_id,
        '{"summary":"first line\\n\\nsecond line","details":{"code":"def run():\\n    return 1"}}',
    )
    modal = ToolHistoryModal(history)
    app = ToolHistoryModalHost(modal)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        detail_text = modal.query_one("#tool-history-detail", TextArea).text

        assert '"search_content":\n        "\n        old line\n        \n        old code\n        "' in detail_text
        assert '"replace_content":\n        "\n        new line\n        \n        new code\n        "' in detail_text
        assert '"summary":\n    "\n    first line\n    \n    second line\n    "' in detail_text
        assert '"code":\n      "\n      def run():\n          return 1\n      "' in detail_text
        assert "old line\\n\\nold code" not in detail_text
        assert "first line\\n\\nsecond line" not in detail_text


@pytest.mark.anyio
async def test_tool_history_modal_omits_unavailable_checkpoint_metadata():
    history = ToolExecutionHistory()
    history.rebuild_from_messages([
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_recovered",
                "name": "FileRead",
                "arguments": '{"path":"old.py"}',
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_recovered",
            "name": "FileRead",
            "content": "old content",
        },
    ])
    modal = ToolHistoryModal(history)
    app = ToolHistoryModalHost(modal)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert modal._row_label(modal._row_values[0]).startswith("✓ FileRead")
        assert "历史" not in modal._row_label(modal._row_values[0])
        detail_text = modal.query_one("#tool-history-detail", TextArea).text

        assert "调用 ID: call_recovered" in detail_text
        assert "任务 ID:" not in detail_text
        assert "开始:" not in detail_text
        assert "结束:" not in detail_text
        assert "耗时:" not in detail_text
        assert "历史重建:" not in detail_text
        assert "checkpoint 未记录" not in detail_text


@pytest.mark.anyio
async def test_tool_history_modal_copies_full_detail_to_system_clipboard():
    modal = ToolHistoryModal(_build_modal_history())
    app = ToolHistoryModalHost(modal)
    app.copy_to_clipboard = Mock()

    with patch("system.tui_modals.copy_to_system_clipboard", return_value=True) as system_copy:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            modal.query_one("#tool-history-list").index = 1
            modal.action_open_detail()
            await pilot.pause()
            detail_text = modal.query_one("#tool-history-detail", TextArea).text

            await pilot.press("c")
            await pilot.pause()

            system_copy.assert_called_once_with(detail_text)
            app.copy_to_clipboard.assert_not_called()
            assert "已复制工具详情到系统剪贴板" in str(
                modal.query_one("#tool-history-status", Label).render()
            )


@pytest.mark.anyio
async def test_tool_history_modal_refreshes_non_latest_running_record_without_losing_selection():
    history = ToolExecutionHistory()
    older_id = history.start("FileRead", {"path": "older.py"})
    history.start("ContentSearch", {"content_regex": "newer"})
    modal = ToolHistoryModal(history)
    app = ToolHistoryModalHost(modal)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        modal.query_one("#tool-history-list").index = 1
        selected_execution_id = modal._current_row().execution_id

        history.finish(older_id, "older result")
        modal._refresh_if_changed()
        await pilot.pause()

        assert modal._current_row().execution_id == selected_execution_id
        assert modal._current_row().status == TOOL_STATUS_SUCCEEDED


@pytest.mark.anyio
async def test_tool_history_modal_uses_list_then_detail_on_compact_terminal():
    modal = ToolHistoryModal(_build_modal_history())
    app = ToolHistoryModalHost(modal)

    async with app.run_test(size=(86, 40)) as pilot:
        await pilot.pause()
        history_list = modal.query_one("#tool-history-list")
        detail = modal.query_one("#tool-history-detail")

        assert not history_list.has_class("tool-history-hidden")
        assert detail.has_class("tool-history-hidden")

        modal.action_open_detail()
        await pilot.pause()

        assert history_list.has_class("tool-history-hidden")
        assert not detail.has_class("tool-history-hidden")

        modal.action_back()
        await pilot.pause()

        assert not history_list.has_class("tool-history-hidden")
        assert detail.has_class("tool-history-hidden")
