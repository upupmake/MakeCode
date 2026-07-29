import os
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import updater


def _executable_name() -> str:
    return "MakeCode.exe" if updater.IS_WINDOWS else "MakeCode"


def _write_bundle(path: Path, marker: str = "new") -> None:
    executable = zipfile.ZipInfo(f"MakeCode/{_executable_name()}")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    runtime = zipfile.ZipInfo("MakeCode/_internal/runtime.dll")
    runtime.external_attr = (stat.S_IFREG | 0o644) << 16
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(executable, marker)
        bundle.writestr(runtime, marker)


def test_extract_update_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("MakeCode/../../outside.txt", "unsafe")

    with pytest.raises(ValueError, match="不安全路径"):
        updater.extract_update_archive(archive, tmp_path / "staging")


def test_extract_update_archive_requires_onedir_layout(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"MakeCode/{_executable_name()}", "new")

    with pytest.raises(ValueError, match="更新包结构无效"):
        updater.extract_update_archive(archive, tmp_path / "staging")


@pytest.mark.skipif(updater.IS_WINDOWS, reason="POSIX symlink behavior")
def test_extract_update_archive_preserves_safe_symlink(tmp_path):
    archive = tmp_path / "update.zip"
    _write_bundle(archive)
    symlink = zipfile.ZipInfo("MakeCode/_internal/runtime-link.dll")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(symlink, "runtime.dll")

    app_dir = updater.extract_update_archive(archive, tmp_path / "staging")

    assert (app_dir / "_internal" / "runtime-link.dll").is_symlink()
    assert os.readlink(app_dir / "_internal" / "runtime-link.dll") == "runtime.dll"


@pytest.mark.skipif(updater.IS_WINDOWS, reason="POSIX symlink behavior")
def test_extract_update_archive_rejects_escaping_symlink(tmp_path):
    archive = tmp_path / "update.zip"
    _write_bundle(archive)
    symlink = zipfile.ZipInfo("MakeCode/_internal/runtime-link.dll")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(symlink, "../../outside.dll")

    with pytest.raises(ValueError, match="不安全符号链接"):
        updater.extract_update_archive(archive, tmp_path / "staging")


def test_invalid_archive_does_not_remove_current_install(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    executable = install_dir / _executable_name()
    executable.write_text("old", encoding="utf-8")
    executable.chmod(0o755)
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"MakeCode/{_executable_name()}", "new")

    with pytest.raises(ValueError, match="更新包结构无效"):
        updater.install_update(archive, install_dir)

    assert executable.read_text(encoding="utf-8") == "old"


def test_install_update_replaces_directory_and_preserves_user_data(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    executable = install_dir / _executable_name()
    executable.write_text("old", encoding="utf-8")
    executable.chmod(0o755)
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)

    with (
        patch.object(updater.subprocess, "Popen") as popen,
        patch.object(updater, "wait_for_ready"),
    ):
        updater.install_update(archive, install_dir)

    assert (install_dir / _executable_name()).read_text(encoding="utf-8") == "new"
    assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    popen.assert_called_once()
    assert not list(tmp_path.glob(".MakeCode.*"))


def test_install_update_rolls_back_when_new_app_cannot_start(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    executable = install_dir / _executable_name()
    executable.write_text("old", encoding="utf-8")
    executable.chmod(0o755)
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)

    with patch.object(updater.subprocess, "Popen", side_effect=OSError("cannot start")):
        with pytest.raises(OSError, match="cannot start"):
            updater.install_update(archive, install_dir)

    assert (install_dir / _executable_name()).read_text(encoding="utf-8") == "old"
    assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    assert not list(tmp_path.glob(".MakeCode.*"))


def test_linux_install_update_replaces_directory_and_preserves_user_data(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode").write_text("old", encoding="utf-8")
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"

    executable = zipfile.ZipInfo("MakeCode/MakeCode")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    runtime = zipfile.ZipInfo("MakeCode/_internal/runtime.so")
    runtime.external_attr = (stat.S_IFREG | 0o644) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(executable, "new")
        bundle.writestr(runtime, "new")

    with (
        patch.object(updater, "IS_WINDOWS", False),
        patch.object(updater.os, "access", return_value=True),
        patch.object(updater.subprocess, "Popen"),
        patch.object(updater, "wait_for_ready"),
    ):
        updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode").read_text(encoding="utf-8") == "new"
    assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    assert not list(tmp_path.glob(".MakeCode.*"))


@pytest.mark.skipif(not updater.IS_WINDOWS, reason="Windows directory-lock regression")
def test_windows_update_succeeds_when_terminal_cwd_is_install_dir(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode.exe").write_text("old", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)
    original_cwd = Path.cwd()

    try:
        os.chdir(install_dir)
        with (
            patch.object(updater.subprocess, "Popen"),
            patch.object(updater, "wait_for_ready"),
        ):
            updater.install_update(archive, install_dir)
    finally:
        os.chdir(original_cwd)

    assert (install_dir / "MakeCode.exe").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".MakeCode.*"))
