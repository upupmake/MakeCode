import subprocess
from unittest.mock import Mock, patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label
from textual.widgets.text_area import Selection

from system.clipboard import copy_to_system_clipboard
from system.tui_modals import CopyContentModal


def test_copy_to_system_clipboard_uses_pbcopy_on_macos():
    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/pbcopy"),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("你好") is True

    run.assert_called_once_with(
        ["/usr/bin/pbcopy"],
        input="你好".encode("utf-8"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_copy_to_system_clipboard_uses_clip_on_windows():
    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", return_value=r"C:\\Windows\\System32\\clip.exe"),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("hello") is True

    run.assert_called_once_with(
        [r"C:\\Windows\\System32\\clip.exe"],
        input="hello".encode("utf-16le"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_copy_to_system_clipboard_prefers_wayland_on_linux():
    def which(command: str) -> str | None:
        return "/usr/bin/wl-copy" if command == "wl-copy" else None

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=which),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("hello") is True

    run.assert_called_once_with(
        ["/usr/bin/wl-copy"],
        input=b"hello",
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_copy_to_system_clipboard_uses_xclip_when_wayland_is_unavailable():
    def which(command: str) -> str | None:
        return "/usr/bin/xclip" if command == "xclip" else None

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=which),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("hello") is True

    run.assert_called_once_with(
        ["/usr/bin/xclip", "-selection", "clipboard"],
        input=b"hello",
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_copy_to_system_clipboard_uses_xsel_when_other_linux_tools_are_unavailable():
    def which(command: str) -> str | None:
        return "/usr/bin/xsel" if command == "xsel" else None

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=which),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("hello") is True

    run.assert_called_once_with(
        ["/usr/bin/xsel", "--clipboard", "--input"],
        input=b"hello",
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_copy_to_system_clipboard_falls_back_when_wayland_command_fails():
    paths = {
        "wl-copy": "/usr/bin/wl-copy",
        "xclip": "/usr/bin/xclip",
    }

    def run(command: list[str], **kwargs):
        if command[0] == "/usr/bin/wl-copy":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=paths.get),
        patch("system.clipboard.subprocess.run", side_effect=run) as run_mock,
    ):
        assert copy_to_system_clipboard("hello") is True

    assert [call.args[0] for call in run_mock.call_args_list] == [
        ["/usr/bin/wl-copy"],
        ["/usr/bin/xclip", "-selection", "clipboard"],
    ]


def test_copy_to_system_clipboard_returns_false_without_platform_tool():
    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", return_value=None),
        patch("system.clipboard.subprocess.run") as run,
    ):
        assert copy_to_system_clipboard("hello") is False

    run.assert_not_called()


def test_copy_to_system_clipboard_returns_false_when_command_fails():
    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/pbcopy"),
        patch(
            "system.clipboard.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["/usr/bin/pbcopy"]),
        ),
    ):
        assert copy_to_system_clipboard("hello") is False


class CopyModalHost(App):
    def __init__(self, modal: CopyContentModal):
        super().__init__()
        self._modal = modal

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self._modal)


@pytest.mark.anyio
async def test_copy_modal_uses_system_clipboard_without_osc52_fallback():
    modal = CopyContentModal([{"role": "assistant", "content": "copy me"}])
    app = CopyModalHost(modal)
    app.copy_to_clipboard = Mock()

    with patch("system.tui_modals.copy_to_system_clipboard", return_value=True) as system_copy:
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            system_copy.assert_called_once_with(modal._text)
            app.copy_to_clipboard.assert_not_called()
            assert "已复制全部内容" in str(
                modal.query_one("#copy-status", Label).render()
            )


@pytest.mark.anyio
async def test_copy_modal_falls_back_to_osc52_when_system_clipboard_is_unavailable():
    modal = CopyContentModal([{"role": "assistant", "content": "copy me"}])
    app = CopyModalHost(modal)
    app.copy_to_clipboard = Mock()

    with patch("system.tui_modals.copy_to_system_clipboard", return_value=False):
        async with app.run_test() as pilot:
            await pilot.click("#copy-all")
            await pilot.pause()

            app.copy_to_clipboard.assert_called_once_with(modal._text)
            assert "当前终端可能不支持系统剪贴板" in str(
                modal.query_one("#copy-status", Label).render()
            )


@pytest.mark.anyio
async def test_copy_selection_button_copies_only_selected_text():
    modal = CopyContentModal([{"role": "assistant", "content": "copy me"}])
    app = CopyModalHost(modal)

    with patch("system.tui_modals.copy_to_system_clipboard", return_value=True) as system_copy:
        async with app.run_test(size=(120, 40)) as pilot:
            text_area = modal.query_one("#copy-text")
            text_area.selection = Selection((1, 0), (1, 4))
            await pilot.click("#copy-selection")
            await pilot.pause()

            system_copy.assert_called_once_with("copy")
            assert "已复制选中文本（4 个字符）" in str(
                modal.query_one("#copy-status", Label).render()
            )


@pytest.mark.anyio
async def test_copy_selection_button_requires_a_selection():
    modal = CopyContentModal([{"role": "assistant", "content": "copy me"}])
    app = CopyModalHost(modal)

    with patch("system.tui_modals.copy_to_system_clipboard") as system_copy:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#copy-selection")
            await pilot.pause()

            system_copy.assert_not_called()
            assert "请先在正文中选择" in str(
                modal.query_one("#copy-status", Label).render()
            )


def test_copy_modal_includes_only_questions_answers_and_terminal_io():
    modal = CopyContentModal([
        {"role": "user", "content": [{"type": "text", "text": "question"}]},
        {
            "role": "assistant",
            "reasoning_content": "field reasoning",
            "content_blocks": [
                {"type": "reasoning", "text": "block reasoning"},
                {"type": "text", "text": "answer"},
                {
                    "type": "tool_call",
                    "name": "FileRead",
                    "arguments": {"path": "README.md"},
                },
                {
                    "type": "tool_call",
                    "name": "RunTerminalCommand",
                    "arguments": {"command": "pytest -q"},
                },
                {"type": "native", "block": {"signature": "private-signature"}},
            ],
            "message_metadata": {
                "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
            },
        },
        {"role": "tool", "name": "FileRead", "content": "file contents"},
        {"role": "tool", "name": "RunTerminalCommand", "content": "2 passed"},
        {"role": "function", "name": "LegacyTool", "content": "failed", "is_error": True},
    ])

    assert "User · 1" in modal._text
    assert "question" in modal._text
    assert "Assistant · 1" in modal._text
    assert "answer" in modal._text
    assert "Terminal Input · 1" in modal._text
    assert "$ pytest -q" in modal._text
    assert "Terminal Output · 1" in modal._text
    assert "2 passed" in modal._text
    assert "field reasoning" not in modal._text
    assert "block reasoning" not in modal._text
    assert "FileRead" not in modal._text
    assert "file contents" not in modal._text
    assert "LegacyTool" not in modal._text
    assert "private-signature" not in modal._text
