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
        if updater.IS_MACOS:
            launcher = zipfile.ZipInfo(updater.MACOS_LAUNCHER_NAME)
            launcher.external_attr = (stat.S_IFREG | 0o755) << 16
            bundle.writestr(launcher, marker)


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
    if updater.IS_MACOS:
        launcher = install_dir.parent / updater.MACOS_LAUNCHER_NAME
        launcher.write_text("old-launcher", encoding="utf-8")
        launcher.chmod(0o755)

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
    if updater.IS_MACOS:
        launcher = install_dir.parent / updater.MACOS_LAUNCHER_NAME
        launcher.write_text("old-launcher", encoding="utf-8")
        launcher.chmod(0o755)

    updater.install_update(archive, install_dir)

    assert (install_dir / _executable_name()).read_text(encoding="utf-8") == "new"
    if updater.IS_MACOS:
        assert not (install_dir / ".makecode").exists()
        assert (install_dir.parent / updater.MACOS_LAUNCHER_NAME).read_text(encoding="utf-8") == "new"
    else:
        assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    assert not list(tmp_path.glob(".MakeCode.*"))


def test_install_update_rolls_back_when_replacement_fails(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    executable = install_dir / _executable_name()
    executable.write_text("old", encoding="utf-8")
    executable.chmod(0o755)
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)
    if updater.IS_WINDOWS:
        original_move_entries = updater._move_entries
        move_calls = 0

        def fail_new_install(source, destination, skip_names=()):
            nonlocal move_calls
            move_calls += 1
            if move_calls == 2:
                raise OSError("cannot install")
            return original_move_entries(source, destination, skip_names)

        failure_patch = patch.object(updater, "_move_entries", side_effect=fail_new_install)
    else:
        original_replace = updater.os.replace

        def fail_new_install(source, destination):
            source = Path(source)
            if Path(destination) == install_dir and source.parent.name.startswith(".MakeCode.staging."):
                raise OSError("cannot install")
            return original_replace(source, destination)

        failure_patch = patch.object(updater.os, "replace", side_effect=fail_new_install)
    if updater.IS_MACOS:
        launcher = install_dir.parent / updater.MACOS_LAUNCHER_NAME
        launcher.write_text("old-launcher", encoding="utf-8")
        launcher.chmod(0o755)

    with failure_patch:
        with pytest.raises(OSError, match="cannot install"):
            updater.install_update(archive, install_dir)

    assert (install_dir / _executable_name()).read_text(encoding="utf-8") == "old"
    if updater.IS_MACOS:
        assert (install_dir.parent / updater.MACOS_LAUNCHER_NAME).read_text(encoding="utf-8") == "old-launcher"
        assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    else:
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
        patch.object(updater, "IS_MACOS", False),
        patch.object(updater.os, "access", return_value=True),
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
        updater.install_update(archive, install_dir)
    finally:
        os.chdir(original_cwd)

    assert (install_dir / "MakeCode.exe").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".MakeCode.*"))


def test_updater_main_reports_manual_restart(tmp_path, capsys):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    executable = install_dir / _executable_name()
    executable.write_text("old", encoding="utf-8")
    executable.chmod(0o755)
    archive = tmp_path / "update.zip"
    _write_bundle(archive)
    if updater.IS_MACOS:
        launcher = install_dir.parent / updater.MACOS_LAUNCHER_NAME
        launcher.write_text("old-launcher", encoding="utf-8")
        launcher.chmod(0o755)

    argv = [
        "updater",
        "--install-dir", str(install_dir),
        "--archive", str(archive),
        "--pid", "0",
    ]
    with (
        patch("sys.argv", argv),
        patch.object(updater, "_configure_file_logging"),
        patch.object(updater, "wait_process_exit") as wait_process_exit,
    ):
        updater.main()

    assert "MakeCode 更新成功，请手动重新启动。" in capsys.readouterr().out
    wait_process_exit.assert_not_called()
    assert (install_dir / _executable_name()).read_text(encoding="utf-8") == "new"
    assert not archive.exists()


def _write_macos_bundle(path: Path, marker: str = "new") -> None:
    executable = zipfile.ZipInfo("MakeCode/MakeCode")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    runtime = zipfile.ZipInfo("MakeCode/_internal/runtime.dylib")
    runtime.external_attr = (stat.S_IFREG | 0o644) << 16
    launcher = zipfile.ZipInfo(updater.MACOS_LAUNCHER_NAME)
    launcher.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(executable, marker)
        bundle.writestr(runtime, marker)
        bundle.writestr(launcher, marker)


def test_macos_install_update_replaces_app_and_launcher(tmp_path):
    package = tmp_path / "package"
    install_dir = package / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode").write_text("old", encoding="utf-8")
    (install_dir / "MakeCode").chmod(0o755)
    launcher = package / updater.MACOS_LAUNCHER_NAME
    launcher.write_text("old-launcher", encoding="utf-8")
    launcher.chmod(0o755)
    archive = tmp_path / "update.zip"
    _write_macos_bundle(archive)

    with patch.object(updater, "IS_MACOS", True), patch.object(updater, "IS_WINDOWS", False):
        updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode").read_text(encoding="utf-8") == "new"
    assert launcher.read_text(encoding="utf-8") == "new"
    assert launcher.stat().st_mode & 0o111
    assert not (install_dir / ".makecode").exists()
    assert not list(package.glob(".MakeCode.*"))


def test_macos_install_update_rolls_back_app_and_launcher(tmp_path):
    package = tmp_path / "package"
    install_dir = package / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode").write_text("old", encoding="utf-8")
    (install_dir / "MakeCode").chmod(0o755)
    launcher = package / updater.MACOS_LAUNCHER_NAME
    launcher.write_text("old-launcher", encoding="utf-8")
    launcher.chmod(0o755)
    archive = tmp_path / "update.zip"
    _write_macos_bundle(archive)
    original_replace = updater.os.replace

    def fail_new_launcher(source, destination):
        source = Path(source)
        destination = Path(destination)
        if (
            destination == launcher
            and source.name == updater.MACOS_LAUNCHER_NAME
            and ".MakeCode.staging." in source.parent.name
        ):
            raise OSError("cannot install launcher")
        return original_replace(source, destination)

    with (
        patch.object(updater, "IS_MACOS", True),
        patch.object(updater, "IS_WINDOWS", False),
        patch.object(updater.os, "replace", side_effect=fail_new_launcher),
    ):
        with pytest.raises(OSError, match="cannot install launcher"):
            updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode").read_text(encoding="utf-8") == "old"
    assert launcher.read_text(encoding="utf-8") == "old-launcher"
    assert not list(package.glob(".MakeCode.*"))


def test_macos_extract_update_archive_requires_launcher(tmp_path):
    archive = tmp_path / "update.zip"
    executable = zipfile.ZipInfo("MakeCode/MakeCode")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    runtime = zipfile.ZipInfo("MakeCode/_internal/runtime.dylib")
    runtime.external_attr = (stat.S_IFREG | 0o644) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(executable, "new")
        bundle.writestr(runtime, "new")

    with patch.object(updater, "IS_MACOS", True), patch.object(updater, "IS_WINDOWS", False):
        with pytest.raises(ValueError, match="缺少可执行的 MakeCode.command"):
            updater.extract_update_archive(archive, tmp_path / "staging")


def test_non_macos_extract_update_archive_rejects_launcher(tmp_path):
    archive = tmp_path / "update.zip"
    executable = zipfile.ZipInfo(f"MakeCode/{_executable_name()}")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    runtime = zipfile.ZipInfo("MakeCode/_internal/runtime.bin")
    runtime.external_attr = (stat.S_IFREG | 0o644) << 16
    launcher = zipfile.ZipInfo(updater.MACOS_LAUNCHER_NAME)
    launcher.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(executable, "new")
        bundle.writestr(runtime, "new")
        bundle.writestr(launcher, "launcher")

    with patch.object(updater, "IS_MACOS", False):
        with pytest.raises(ValueError, match="当前平台更新包不能包含 MakeCode.command"):
            updater.extract_update_archive(archive, tmp_path / "staging")


def test_macos_install_update_requires_existing_launcher(tmp_path):
    package = tmp_path / "package"
    install_dir = package / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode").write_text("old", encoding="utf-8")
    (install_dir / "MakeCode").chmod(0o755)
    archive = tmp_path / "update.zip"
    _write_macos_bundle(archive)

    with patch.object(updater, "IS_MACOS", True), patch.object(updater, "IS_WINDOWS", False):
        with pytest.raises(ValueError, match="安装目录旁缺少 MakeCode.command"):
            updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode").read_text(encoding="utf-8") == "old"
    assert not (package / updater.MACOS_LAUNCHER_NAME).exists()
