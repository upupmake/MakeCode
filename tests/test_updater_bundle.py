import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import updater


def _write_bundle(path: Path, marker: str = "new") -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("MakeCode/MakeCode.exe", marker)
        bundle.writestr("MakeCode/_internal/runtime.dll", marker)


def test_extract_update_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("MakeCode/../../outside.txt", "unsafe")

    with pytest.raises(ValueError, match="不安全路径"):
        updater.extract_update_archive(archive, tmp_path / "staging")


def test_extract_update_archive_requires_onedir_layout(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("MakeCode/MakeCode.exe", "new")

    with pytest.raises(ValueError, match="更新包结构无效"):
        updater.extract_update_archive(archive, tmp_path / "staging")


def test_invalid_archive_does_not_remove_current_install(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode.exe").write_text("old", encoding="utf-8")
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("MakeCode/MakeCode.exe", "new")

    with pytest.raises(ValueError, match="更新包结构无效"):
        updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode.exe").read_text(encoding="utf-8") == "old"


def test_install_update_replaces_directory_and_preserves_user_data(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode.exe").write_text("old", encoding="utf-8")
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)

    with (
        patch.object(updater.subprocess, "Popen") as popen,
        patch.object(updater, "wait_for_ready"),
    ):
        updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode.exe").read_text(encoding="utf-8") == "new"
    assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    popen.assert_called_once()
    assert not list(tmp_path.glob(".MakeCode.*"))


def test_install_update_rolls_back_when_new_app_cannot_start(tmp_path):
    install_dir = tmp_path / "MakeCode"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "MakeCode.exe").write_text("old", encoding="utf-8")
    (install_dir / ".makecode").mkdir()
    (install_dir / ".makecode" / "model_config.json").write_text("config", encoding="utf-8")
    archive = tmp_path / "update.zip"
    _write_bundle(archive)

    with patch.object(updater.subprocess, "Popen", side_effect=OSError("cannot start")):
        with pytest.raises(OSError, match="cannot start"):
            updater.install_update(archive, install_dir)

    assert (install_dir / "MakeCode.exe").read_text(encoding="utf-8") == "old"
    assert (install_dir / ".makecode" / "model_config.json").read_text(encoding="utf-8") == "config"
    assert not list(tmp_path.glob(".MakeCode.*"))
