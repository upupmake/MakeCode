from io import StringIO
from unittest.mock import patch

from rich.console import Console
import pytest

from system.console_render import (
    _render_tool_call,
    _render_tool_output,
    _terminal_output_text,
    render_content_assistant_message,
    render_model_markdown,
)
from system.stream_render import StreamRenderer
from system.tui_app import TuiRegion
from utils.llm_client import build_llm_result


def _render_tool_event(render, value):
    posted = []
    with patch(
        "system.console_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: posted.append(payload),
    ):
        render(value)

    panel = next(payload for payload in posted if payload is not None)
    buffer = StringIO()
    Console(file=buffer, force_terminal=False, width=120, color_system=None).print(panel.renderable)
    return buffer.getvalue()


def _render_terminal_output(output):
    return _render_tool_event(
        lambda value: _render_tool_output("RunTerminalCommand", value),
        output,
    )


def _render_tool_arguments(arguments):
    return _render_tool_event(
        lambda value: _render_tool_call("FileEdit", value),
        arguments,
    )


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


def test_structured_tool_arguments_use_json_layout_and_expand_multiline_strings():
    rendered = _render_tool_arguments({
        "edits": [{
            "search_content": "old line\n\nold code",
            "replace_content": "new line\n\nnew code",
        }],
        "count": 3,
        "enabled": True,
    })

    assert '"edits": [' in rendered
    assert '"search_content": "\n      old line\n      \n      old code"' in rendered
    assert "old line\\n\\nold code" not in rendered
    assert '"count": 3' in rendered
    assert '"enabled": true' in rendered
    assert "❖" not in rendered
    assert "[Item 1]" not in rendered


def test_json_string_tool_arguments_expand_real_newlines_but_preserve_literal_escapes():
    rendered = _render_tool_arguments(
        '{"content":"first line\\n\\nsecond line","literal":"first\\\\nsecond"}'
    )

    assert '"content": "\n  first line\n  \n  second line"' in rendered
    assert "first line\\n\\nsecond line" not in rendered
    assert '"literal": "first\\\\nsecond"' in rendered


def test_structured_tool_output_uses_json_layout_and_expands_multiline_strings():
    rendered = _render_terminal_output(
        '{"summary":"first line\\n\\nsecond line","details":{"code":"def run():\\n    return 1"}}'
    )

    assert '"summary": "\n  first line\n  \n  second line"' in rendered
    assert "first line\\n\\nsecond line" not in rendered
    assert '"details": {' in rendered
    assert '"code": "\n    def run():\n        return 1"' in rendered
    assert "❖" not in rendered


def test_terminal_output_preserves_text_before_carriage_return_control_sequences():
    rendered = _terminal_output_text(
        "session manager\r\x1b[2K\nstarted\r\x1b[2K\nshutting down"
    ).plain

    assert rendered == "session manager\nstarted\nshutting down"


def test_plain_tool_output_remains_plain_text():
    rendered = _render_terminal_output("first line\nsecond line")

    assert "first line\n" in rendered
    assert "second line" in rendered
    assert '"first line' not in rendered


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


@pytest.mark.anyio
async def test_stream_error_releases_tool_calls_background_active_state():
    events = []

    async def failing_stream():
        yield {"type": "tool_calls", "content": None}
        raise RuntimeError("stream interrupted")

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch("system.stream_render.is_cancelled", return_value=False):
        with pytest.raises(RuntimeError, match="stream interrupted"):
            await StreamRenderer().render_async(failing_stream())

    background_active = [
        kwargs["active"]
        for region, _, kwargs in events
        if region == TuiRegion.BACKGROUND and "active" in kwargs
    ]
    assert background_active == [True, False]


@pytest.mark.anyio
async def test_stream_error_releases_reasoning_and_content_active_state():
    events = []

    async def failing_stream():
        yield {"type": "reasoning", "content": "思考中"}
        yield {"type": "text", "content": "正文"}
        raise RuntimeError("stream interrupted")

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch("system.stream_render.is_cancelled", return_value=False):
        with pytest.raises(RuntimeError, match="stream interrupted"):
            await StreamRenderer().render_async(failing_stream())

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


@pytest.mark.anyio
async def test_cancelled_stream_releases_tool_calls_background_active_state():
    events = []

    async def cancelled_stream():
        yield {"type": "tool_calls", "content": None}
        yield {"type": "text", "content": "不会继续渲染"}

    with patch(
        "system.stream_render.post_tui",
        side_effect=lambda region, payload=None, **kwargs: events.append((region, payload, kwargs)),
    ), patch(
        "system.stream_render.is_cancelled",
        side_effect=[False, True, True],
    ):
        text_content, tool_calls, raw_message = await StreamRenderer().render_async(cancelled_stream())

    background_active = [
        kwargs["active"]
        for region, _, kwargs in events
        if region == TuiRegion.BACKGROUND and "active" in kwargs
    ]
    assert background_active == [True, False]
    assert (text_content, tool_calls, raw_message) == ("", [], None)


@pytest.mark.anyio
async def test_async_stream_renderer_consumes_unified_done_result():
    result = build_llm_result(
        text="async answer",
        reasoning="reason",
        source_format="openai_chat",
        source_model="test-model",
        stop_reason="stop",
    )

    async def stream():
        yield {"type": "reasoning", "content": "reason"}
        yield {"type": "text", "content": "async answer"}
        yield {"type": "done", "result": result}

    with patch("system.stream_render.post_tui"), patch(
        "system.stream_render.is_cancelled", return_value=False
    ):
        rendered = await StreamRenderer().render_async(stream())

    assert rendered == ("async answer", [], result.assistant_message)
