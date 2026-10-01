import base64
import locale
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


class DirectoryPickerUnavailableError(RuntimeError):
    pass


_C_LOCALE_NAMES = {"c", "posix"}


def _normalize_language_tag(value: str) -> str:
    return value.split(":", 1)[0].split(".", 1)[0].replace("-", "_").strip().lower()


def _locale_language() -> str:
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(variable, "")
        if value:
            language = _normalize_language_tag(value)
            if language and language not in _C_LOCALE_NAMES:
                return language

    for category_name in ("LC_MESSAGES", "LC_CTYPE"):
        category = getattr(locale, category_name, None)
        if category is None:
            continue
        try:
            value = locale.getlocale(category)[0]
        except (locale.Error, ValueError):
            value = None
        if value:
            language = _normalize_language_tag(value)
            if language and language not in _C_LOCALE_NAMES:
                return language
    return "en"


def _macos_global_preferences_path() -> Path:
    return Path.home() / "Library/Preferences/.GlobalPreferences.plist"


def _macos_ui_language() -> str | None:
    try:
        with _macos_global_preferences_path().open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError):
        return None
    languages = data.get("AppleLanguages") if isinstance(data, dict) else None
    if not isinstance(languages, list) or not languages:
        return None
    first = languages[0]
    if not isinstance(first, str):
        return None
    language = first.strip()
    return language or None


def _windows_ui_language() -> str | None:
    try:
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError, ValueError):
        return None
    return locale.windows_locale.get(int(langid))


def _system_language() -> str:
    if sys.platform == "darwin":
        language = _macos_ui_language()
        if language:
            return language
    elif sys.platform == "win32":
        language = _windows_ui_language()
        if language:
            return language
    return _locale_language()


def _is_traditional_chinese(language: str) -> bool:
    tokens = set(_normalize_language_tag(language).split("_"))
    return bool(tokens & {"hant", "tw", "hk", "mo", "cht"})


def directory_picker_prompt() -> str:
    language = _normalize_language_tag(_system_language())
    if _is_traditional_chinese(language):
        return "選擇工作區目錄"
    if language == "zh" or language.startswith("zh_"):
        return "选择工作区目录"
    if language == "ja" or language.startswith("ja_"):
        return "ワークスペースフォルダを選択"
    return "Select Workspace Directory"


def _initial_directory(initial_dir: Path | str | None) -> Path | None:
    if initial_dir is None:
        return None
    path = Path(initial_dir).expanduser().resolve()
    if path.is_dir():
        return path
    return path.parent if path.parent.is_dir() else None


def _path_from_output(output: bytes | str | None) -> Path | None:
    if isinstance(output, bytes):
        text = output.decode("utf-8", "replace")
    else:
        text = output or ""
    text = text.strip()
    return Path(text).expanduser().resolve() if text else None


def _apple_script_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _choose_directory_macos(prompt: str, initial_dir: Path | None) -> Path | None:
    osascript = shutil.which("osascript")
    if not osascript:
        raise DirectoryPickerUnavailableError("当前 macOS 环境找不到 osascript。")
    initial_clause = (
        f' default location POSIX file "{_apple_script_text(str(initial_dir))}"'
        if initial_dir is not None
        else ""
    )
    script = (
        'tell application "Finder"\n'
        "    activate\n"
        f'    POSIX path of (choose folder with prompt "{_apple_script_text(prompt)}"'
        f"{initial_clause})\n"
        "end tell"
    )
    try:
        result = subprocess.run(
            [osascript, "-e", script],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return _path_from_output(result.stdout)


def _powershell_script_string(value: str) -> str:
    return value.replace("'", "''")


def _choose_directory_windows(
    prompt: str,
    initial_dir: Path | None,
) -> Path | None:
    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh")
    )
    if not powershell:
        raise DirectoryPickerUnavailableError("当前 Windows 环境找不到 PowerShell。")

    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$dialog.Description = '{_powershell_script_string(prompt)}'; "
        "$dialog.ShowNewFolderButton = $true; "
    )
    if initial_dir is not None:
        script += f"$dialog.SelectedPath = '{_powershell_script_string(str(initial_dir))}'; "
    script += (
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.SelectedPath) }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-EncodedCommand",
                encoded,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return _path_from_output(result.stdout)


def choose_directory(
    initial_dir: Path | str | None = None,
    *,
    prompt: str | None = None,
) -> Path | None:
    prompt = prompt or directory_picker_prompt()
    resolved_initial_dir = _initial_directory(initial_dir)
    if sys.platform == "darwin":
        return _choose_directory_macos(prompt, resolved_initial_dir)
    if sys.platform == "win32":
        return _choose_directory_windows(prompt, resolved_initial_dir)
    raise DirectoryPickerUnavailableError(
        f"当前平台不支持系统目录选择器：{sys.platform}"
    )
