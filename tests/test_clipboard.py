import subprocess
from unittest.mock import Mock, patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label

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

            system_copy.assert_called_once_with("──── Assistant ────\ncopy me")
            app.copy_to_clipboard.assert_not_called()
            assert "已复制文本到系统剪贴板" in str(
                modal.query_one("#copy-status", Label).render()
            )


@pytest.mark.anyio
async def test_copy_modal_falls_back_to_osc52_when_system_clipboard_is_unavailable():
    modal = CopyContentModal([{"role": "assistant", "content": "copy me"}])
    app = CopyModalHost(modal)
    app.copy_to_clipboard = Mock()

    with patch("system.tui_modals.copy_to_system_clipboard", return_value=False):
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            app.copy_to_clipboard.assert_called_once_with("──── Assistant ────\ncopy me")
            assert "当前终端可能不支持系统剪贴板" in str(
                modal.query_one("#copy-status", Label).render()
            )
