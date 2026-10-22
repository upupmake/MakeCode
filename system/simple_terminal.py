"""Terminal capability detection for simple mode."""

from __future__ import annotations

import os
import re
import shutil
import sys

_PROFILE_PLAIN = "plain"
_PROFILE_BASIC = "basic"
_PROFILE_FULL = "full"

_VALID_PROFILES = {_PROFILE_PLAIN, _PROFILE_BASIC, _PROFILE_FULL}
_UTF8_ENCODINGS = {"utf-8", "utf8", "cp65001"}

_ICON_TRANSLATIONS = {
    "🛠": "[tool]",
    "🧠": "[reasoning]",
    "✅": "[ok]",
    "✓": "[ok]",
    "✔": "[ok]",
    "☑": "[ok]",
    "❌": "[x]",
    "✖": "[x]",
    "✗": "[x]",
    "⚠": "[warn]",
    "📂": "[dir]",
    "📁": "[dir]",
    "📄": "[file]",
    "🧾": "[log]",
    "🌳": "[tree]",
    "🔄": "[sync]",
    "💡": "[tip]",
    "👋": "[bye]",
    "⌨": "[key]",
    "🔧": "[tool]",
    "📌": "[pin]",
    "🧩": "[layout]",
    "📚": "[skills]",
    "🔌": "[mcp]",
    "🔍": "[search]",
    "📝": "[edit]",
    "🗑": "[del]",
    "💾": "[save]",
    "📋": "[copy]",
    "🆕": "[new]",
    "🚀": "[run]",
    "🔥": "[hot]",
    "🎉": "[ok]",
    "⏳": "[wait]",
    "⌛": "[wait]",
    "⚙": "[cfg]",
    "⚡": "[fast]",
    "🔗": "[link]",
    "🔒": "[lock]",
    "🔓": "[unlock]",
    "▶": ">",
    "◀": "<",
    "▸": ">",
    "◂": "<",
    "➜": ">",
    "→": ">",
    "←": "<",
    "↑": "^",
    "↓": "v",
    "↔": "<->",
    "↩": "<-",
    "⟳": "[sync]",
    "❯": ">",
    "●": "*",
    "○": "o",
    "★": "*",
    "☆": "*",
    "♥": "*",
    "■": "#",
    "□": "[]",
    "▪": "-",
    "▫": "-",
    "•": "*",
    "·": "-",
    "…": "...",
    "—": "-",
    "–": "-",
    "×": "x",
    "≤": "<=",
    "≥": ">=",
    "\u00a0": " ",
    "\ufe0e": "",
    "\ufe0f": "",
    "\u200d": "",
    "\u200b": "",
    "\u20e3": "",
}

_BOX_TRANSLATIONS = {
    **dict.fromkeys("╭╮╰╯┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬", "+"),
    **dict.fromkeys("─━═", "-"),
    **dict.fromkeys("│┃║", "|"),
}

_ICON_TRANSLATION_TABLE = str.maketrans({
    **_ICON_TRANSLATIONS,
    **_BOX_TRANSLATIONS,
})

_ICON_RANGES = re.compile(
    "["
    "\u2190-\u21ff"
    "\u2300-\u23ff"
    "\u2500-\u27bf"
    "\u2b00-\u2bff"
    "\ufe0e\ufe0f"
    "\u200d\u200b\u20e3"
    "\U0001f000-\U0001faff"
    "]"
)


def is_interactive() -> bool:
    return bool(
        sys.stdin
        and sys.stdout
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def _modern_windows_terminal() -> bool:
    """Windows terminals are identified by their own environment markers.

    ``TERM`` is usually unset on Windows even inside Windows Terminal, so the
    OS version alone says nothing about VT/Unicode support.
    """
    if sys.platform != "win32":
        return False
    env = os.environ
    if env.get("WT_SESSION") or env.get("WT_PROFILE_ID"):
        return True
    if env.get("ConEmuANSI", "").upper() == "ON":
        return True
    if env.get("TERM_PROGRAM", "").lower() == "vscode":
        return True
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return True
    return bool(
        env.get("WEZTERM_EXECUTABLE")
        or env.get("WEZTERM_PANE")
        or env.get("ALACRITTY_WINDOW_ID")
        or env.get("KITTY_WINDOW_ID")
    )


def profile() -> str:
    override = os.environ.get("MAKECODE_SIMPLE_PROFILE", "").strip().lower()
    if override in _VALID_PROFILES:
        return override
    if not is_interactive():
        return _PROFILE_PLAIN
    term = os.environ.get("TERM", "").strip().lower()
    if term in {"dumb", "unknown"}:
        return _PROFILE_PLAIN
    if term in {
        "vt100",
        "vt102",
        "ansi",
        "linux",
        "cons25",
        "cygwin",
    }:
        return _PROFILE_BASIC
    if (
        "256color" in term
        or "truecolor" in term
        or term in {
            "xterm",
            "screen",
            "tmux",
            "rxvt",
            "alacritty",
            "kitty",
            "wezterm",
        }
    ):
        return _PROFILE_FULL
    if _modern_windows_terminal():
        return _PROFILE_FULL
    if not term:
        # Windows terminals often do not set TERM. A POSIX terminal
        # without TERM is usually a very basic "dumb" environment.
        return _PROFILE_BASIC if sys.platform == "win32" else _PROFILE_PLAIN
    return _PROFILE_BASIC


def profile_is_full() -> bool:
    return profile() == _PROFILE_FULL


def supports_prompt_toolkit() -> bool:
    return is_interactive() and profile() != _PROFILE_PLAIN


def ascii_ui() -> bool:
    if os.environ.get("MAKECODE_SIMPLE_UNICODE") == "1":
        return False
    if profile() != _PROFILE_FULL:
        return True
    if sys.platform == "win32" and not _modern_windows_terminal():
        return True
    encoding = (sys.stdout.encoding or "").lower().replace("-", "")
    return encoding not in _UTF8_ENCODINGS


def no_color() -> bool:
    return (
        profile() == _PROFILE_PLAIN
        or "NO_COLOR" in os.environ
        or os.environ.get("TERM", "").strip().lower() == "dumb"
    )


def terminal_size() -> os.terminal_size:
    return shutil.get_terminal_size((80, 24))


def menu_rows() -> int:
    height = terminal_size().lines
    return max(3, min(12, height - 8))


def show_bottom_toolbar() -> bool:
    return profile() == _PROFILE_FULL and terminal_size().lines >= 12


def live_completion() -> bool:
    return profile() == _PROFILE_FULL


def prepare_prompt_toolkit() -> None:
    """Avoid CPR waits on terminals that do not answer cursor queries."""
    if profile() == _PROFILE_BASIC:
        os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")


def prompt_color_depth():
    from prompt_toolkit.output import ColorDepth

    if no_color():
        return ColorDepth.DEPTH_1_BIT
    if profile() != _PROFILE_FULL:
        return ColorDepth.DEPTH_4_BIT
    return None


def make_console():
    from rich.console import Console

    output = safe_output(sys.stdout)
    if no_color():
        return Console(file=output, color_system=None, no_color=True)
    if profile() != _PROFILE_FULL:
        return Console(
            file=output,
            color_system="windows" if sys.platform == "win32" else "standard"
        )
    return Console(file=output)


def selected_marker() -> str:
    return "> " if ascii_ui() else "❯ "


def arrows_label() -> str:
    return "Up/Down" if ascii_ui() else "↑/↓"


def _replace_icon(match: re.Match[str]) -> str:
    character = match.group(0)
    if character in _ICON_TRANSLATIONS:
        return _ICON_TRANSLATIONS[character]
    if character in _BOX_TRANSLATIONS:
        return _BOX_TRANSLATIONS[character]
    return ""


def sanitize_icons(text: str) -> str:
    if not ascii_ui():
        return text
    text = text.translate(_ICON_TRANSLATION_TABLE)
    return _ICON_RANGES.sub(_replace_icon, text)


class _AsciiOutput:
    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, text):
        return self._stream.write(sanitize_icons(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def safe_output(stream):
    if not ascii_ui():
        return stream
    if isinstance(stream, _AsciiOutput):
        return stream
    return _AsciiOutput(stream)
