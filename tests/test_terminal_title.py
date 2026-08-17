import ctypes
import sys
from unittest.mock import Mock

from utils.terminal import set_terminal_title


def test_set_terminal_title_uses_ansi_escape_sequence(monkeypatch):
    stderr = Mock()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "__stderr__", stderr)

    set_terminal_title("标题\n测试")

    stderr.write.assert_called_once_with("\x1b]0;标题测试\x07\x1b]2;标题测试\x07")
    stderr.flush.assert_called_once_with()


def test_set_terminal_title_uses_windows_console_api(monkeypatch):
    kernel32 = Mock()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", Mock(kernel32=kernel32), raising=False)

    set_terminal_title("MakeCode")

    kernel32.SetConsoleTitleW.assert_called_once_with("MakeCode")


def test_set_terminal_title_ignores_terminal_errors(monkeypatch):
    stderr = Mock()
    stderr.write.side_effect = OSError("terminal unavailable")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "__stderr__", stderr)

    set_terminal_title("MakeCode")


def test_set_terminal_title_falls_back_to_stderr(monkeypatch):
    primary = Mock()
    primary.write.side_effect = OSError("primary unavailable")
    fallback = Mock()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "__stderr__", primary)
    monkeypatch.setattr(sys, "stderr", fallback)

    set_terminal_title("MakeCode")

    fallback.write.assert_called_once_with("\x1b]0;MakeCode\x07\x1b]2;MakeCode\x07")
    fallback.flush.assert_called_once_with()
