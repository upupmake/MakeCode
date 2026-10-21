"""Terminal capability detection for simple mode."""

from __future__ import annotations

import os
import shutil
import sys

_PROFILE_PLAIN = "plain"
_PROFILE_BASIC = "basic"
_PROFILE_FULL = "full"

_VALID_PROFILES = {_PROFILE_PLAIN, _PROFILE_BASIC, _PROFILE_FULL}
_UTF8_ENCODINGS = {"utf-8", "utf8", "cp65001"}


def is_interactive() -> bool:
    return bool(
        sys.stdin
        and sys.stdout
        and sys.stdin.isatty()
        and sys.stdout.isatty()
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
    if not term:
        # Windows terminals often do not set TERM. A POSIX terminal
        # without TERM is usually a very basic "dumb" environment.
        return _PROFILE_BASIC if sys.platform == "win32" else _PROFILE_PLAIN
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
    return _PROFILE_BASIC


def profile_is_full() -> bool:
    return profile() == _PROFILE_FULL


def supports_prompt_toolkit() -> bool:
    return is_interactive() and profile() != _PROFILE_PLAIN


def ascii_ui() -> bool:
    if profile() != _PROFILE_FULL:
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

    if no_color():
        return Console(color_system=None, no_color=True)
    if profile() != _PROFILE_FULL:
        return Console(
            color_system="windows" if sys.platform == "win32" else "standard"
        )
    return Console()


def selected_marker() -> str:
    return "> " if ascii_ui() else "❯ "


def arrows_label() -> str:
    return "Up/Down" if ascii_ui() else "↑/↓"
