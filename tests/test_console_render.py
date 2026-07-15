from io import StringIO
from unittest.mock import patch

from rich.console import Console

from system.console_render import _render_tool_output


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
