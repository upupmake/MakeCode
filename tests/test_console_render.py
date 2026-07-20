from io import StringIO
from unittest.mock import patch

from rich.console import Console
import pytest

from system.console_render import (
    _format_readable_ui,
    _render_tool_output,
    _terminal_output_text,
    render_content_assistant_message,
    render_model_markdown,
)
from system.stream_render import StreamRenderer
from system.tui_app import TuiRegion


def _render_terminal_output(output):
    posted = []
    with patch(
        "system.console_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: posted.append(payload),
    ):
        _render_tool_output("RunTerminalCommand", output)

    panel = next(payload for payload in posted if payload is not None)
    buffer = StringIO()
    Console(file=buffer, force_terminal=True, width=100).print(panel)
    return buffer.getvalue()


def test_tool_output_does_not_emit_terminal_screen_control_sequences():
    rendered = _render_terminal_output(
        "normal-before\x1b[31mRED\x1b[0m\x1b[2J\x1b[10;10Hnormal-after"
    )

    assert "normal-before" in rendered
    assert "normal-after" in rendered
    assert "\x1b[2J" not in rendered
    assert "\x1b[10;10H" not in rendered


def test_structured_tool_output_does_not_emit_terminal_screen_control_sequences():
    rendered = _render_terminal_output({"output": "before\x1b[2J\x1b[10;10Hafter"})

    assert "before" in rendered
    assert "after" in rendered
    assert "\x1b[2J" not in rendered
    assert "\x1b[10;10H" not in rendered


def test_terminal_output_preserves_text_before_carriage_return_control_sequences():
    rendered = _terminal_output_text(
        "session manager\r\x1b[2K\nstarted\r\x1b[2K\nshutting down"
    ).plain

    assert rendered == "session manager\nstarted\nshutting down"


def test_structured_tool_output_uses_distinct_key_and_value_colors():
    lines = _format_readable_ui({"name": "MakeCode", "count": 3}, decode_ansi=True)
    console = Console()

    key_style = lines[0].get_style_at_offset(console, 1)
    string_style = lines[0].get_style_at_offset(console, len(lines[0].plain) - 1)
    number_style = lines[1].get_style_at_offset(console, len(lines[1].plain) - 1)

    assert not key_style.bold
    assert key_style.color.name == "green"
    assert string_style.color.name == "white"
    assert number_style.color.name == "white"


def test_structured_tool_arguments_support_numeric_values():
    lines = _format_readable_ui({"count": 3})

    assert lines[0].plain == "❖ count: 3"


def test_list_of_json_objects_keeps_key_and_value_colors_distinct():
    lines = _format_readable_ui([{"enabled": True}], decode_ansi=True)
    console = Console()
    key_value_line = lines[1]

    key_style = key_value_line.get_style_at_offset(console, 3)
    value_style = key_value_line.get_style_at_offset(console, len(key_value_line.plain) - 1)

    assert key_style.color.name == "green"
    assert value_style.color.name == "white"


def _render_to_plain_text(renderable):
    buffer = StringIO()
    Console(file=buffer, width=100).print(renderable)
    return buffer.getvalue()


def test_model_markdown_displays_html_like_tags_as_text():
    rendered = _render_to_plain_text(render_model_markdown("<xxxx> aaaa </xxxx>"))

    assert "<xxxx> aaaa </xxxx>" in rendered


def test_assistant_panel_displays_tags_and_keeps_markdown_formatting():
    rendered = _render_to_plain_text(
        render_content_assistant_message("**正文** <thinking>内容</thinking>")
    )

    assert "正文" in rendered
    assert "<thinking>内容</thinking>" in rendered


def test_stream_error_releases_tool_calls_background_active_state():
    events = []

    def failing_stream():
        yield {"type": "tool_calls", "content": None}
        raise RuntimeError("stream interrupted")

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch("system.stream_render.is_cancelled", return_value=False):
        with pytest.raises(RuntimeError, match="stream interrupted"):
            StreamRenderer().render(failing_stream())

    background_active = [
        kwargs["active"]
        for region, _, kwargs in events
        if region == TuiRegion.BACKGROUND and "active" in kwargs
    ]
    assert background_active == [True, False]


def test_stream_error_releases_reasoning_and_content_active_state():
    events = []

    def failing_stream():
        yield {"type": "reasoning", "content": "思考中"}
        yield {"type": "text", "content": "正文"}
        raise RuntimeError("stream interrupted")

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch("system.stream_render.is_cancelled", return_value=False):
        with pytest.raises(RuntimeError, match="stream interrupted"):
            StreamRenderer().render(failing_stream())

    active_by_region = {
        region: [
            kwargs["active"]
            for event_region, _, kwargs in events
            if event_region == region and "active" in kwargs
        ]
        for region in (TuiRegion.REASONING, TuiRegion.CONTENT)
    }
    assert active_by_region == {
        TuiRegion.REASONING: [True, False],
        TuiRegion.CONTENT: [True, False],
    }


def test_cancelled_stream_releases_tool_calls_background_active_state():
    events = []

    def cancelled_stream():
        yield {"type": "tool_calls", "content": None}
        yield {"type": "text", "content": "不会继续渲染"}

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch(
        "system.stream_render.is_cancelled",
        side_effect=[False, True, True],
    ):
        text_content, tool_calls, raw_message = StreamRenderer().render(cancelled_stream())

    background_active = [
        kwargs["active"]
        for region, _, kwargs in events
        if region == TuiRegion.BACKGROUND and "active" in kwargs
    ]
    assert background_active == [True, False]
    assert (text_content, tool_calls, raw_message) == ("", [], None)
