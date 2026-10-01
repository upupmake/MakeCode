import base64
import os
import plistlib
import subprocess
from unittest.mock import patch

import pytest

from utils.directory_picker import (
    DirectoryPickerUnavailableError,
    _macos_ui_language,
    choose_directory,
    directory_picker_prompt,
)


def test_directory_picker_prompt_follows_macos_ui_language_not_terminal_lang():
    with (
        patch("utils.directory_picker.sys.platform", "darwin"),
        patch("utils.directory_picker._macos_ui_language", return_value="en-US"),
        patch.dict("utils.directory_picker.os.environ", {"LANG": "zh_CN.UTF-8"}, clear=True),
    ):
        assert directory_picker_prompt() == "Select Workspace Directory"


def test_directory_picker_prompt_follows_windows_ui_language():
    with (
        patch("utils.directory_picker.sys.platform", "win32"),
        patch("utils.directory_picker._windows_ui_language", return_value="zh_TW"),
        patch.dict("utils.directory_picker.os.environ", {"LANG": "en_US.UTF-8"}, clear=True),
    ):
        assert directory_picker_prompt() == "選擇工作區目錄"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-Hans-CN", "选择工作区目录"),
        ("zh_CN", "选择工作区目录"),
        ("zh-Hant-TW", "選擇工作區目錄"),
        ("zh_HK", "選擇工作區目錄"),
        ("ja-JP", "ワークスペースフォルダを選択"),
        ("en-US", "Select Workspace Directory"),
        ("fr_FR", "Select Workspace Directory"),
    ],
)
def test_directory_picker_prompt_matches_linux_locale(language, expected):
    with (
        patch("utils.directory_picker.sys.platform", "linux"),
        patch.dict(
            "utils.directory_picker.os.environ",
            {"LANG": f"{language}.UTF-8"},
            clear=True,
        ),
    ):
        assert directory_picker_prompt() == expected


def test_linux_directory_picker_is_unavailable(tmp_path):
    with patch("utils.directory_picker.sys.platform", "linux"):
        with pytest.raises(DirectoryPickerUnavailableError, match="不支持系统目录选择器"):
            choose_directory(tmp_path)


def test_macos_ui_language_reads_first_apple_language(tmp_path):
    plist = tmp_path / ".GlobalPreferences.plist"
    with plist.open("wb") as handle:
        plistlib.dump({"AppleLanguages": ["zh-Hant-TW", "en-US"]}, handle)

    with patch("utils.directory_picker._macos_global_preferences_path", return_value=plist):
        assert _macos_ui_language() == "zh-Hant-TW"


def test_macos_directory_picker_returns_selected_path_and_uses_initial_directory(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()

    with (
        patch("utils.directory_picker.sys.platform", "darwin"),
        patch("utils.directory_picker.shutil.which", return_value="/usr/bin/osascript"),
        patch(
            "utils.directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/osascript"],
                0,
                stdout=(str(selected) + "/\n").encode(),
            ),
        ) as run,
    ):
        assert choose_directory(tmp_path) == selected.resolve()

    command = run.call_args.args[0]
    assert command[:2] == ["/usr/bin/osascript", "-e"]
    assert "default location POSIX file" in command[2]
    assert "env" not in run.call_args.kwargs
    assert 'tell application "Finder"' in command[2]
    assert "activate" in command[2]
    assert "choose folder with prompt" in command[2]


def test_windows_directory_picker_uses_sta_powershell_and_decodes_unicode_path(tmp_path):
    selected = tmp_path / "Windows 目录"
    selected.mkdir()

    with (
        patch("utils.directory_picker.sys.platform", "win32"),
        patch("utils.directory_picker.shutil.which", return_value="powershell.exe"),
        patch(
            "utils.directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["powershell.exe"],
                0,
                stdout=(str(selected) + "\n").encode("utf-8"),
            ),
        ) as run,
    ):
        assert choose_directory(tmp_path) == selected.resolve()

    command = run.call_args.args[0]
    assert command[0] == "powershell.exe"
    assert "-STA" in command
    script = base64.b64decode(command[command.index("-EncodedCommand") + 1]).decode("utf-16le")
    assert "FolderBrowserDialog" in script
    assert str(tmp_path) in script


def test_unsupported_platform_reports_that_picker_is_unavailable(tmp_path):
    with patch("utils.directory_picker.sys.platform", "emscripten"):
        with pytest.raises(DirectoryPickerUnavailableError, match="不支持系统目录选择器"):
            choose_directory(tmp_path)
