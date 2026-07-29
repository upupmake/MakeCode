import hashlib
import io
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from system import updater


class _Response:
    def __init__(self, content: bytes):
        self._stream = io.BytesIO(content)
        self.headers = {"Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_ssl_context_loads_system_and_bundled_ca():
    ssl_context = Mock()
    with (
        patch.object(updater.ssl, "create_default_context", return_value=ssl_context) as create_context,
        patch.object(updater.certifi, "where", return_value="/bundle/certifi/cacert.pem"),
    ):
        assert updater._create_ssl_context() is ssl_context

    create_context.assert_called_once_with()
    ssl_context.load_verify_locations.assert_called_once_with(cafile="/bundle/certifi/cacert.pem")


def test_check_update_uses_trusted_ca_context():
    ssl_context = object()
    response = _Response(b'{"version":"999.0.0"}')

    with (
        patch.object(updater, "_create_ssl_context", return_value=ssl_context),
        patch.object(updater.urllib.request, "urlopen", return_value=response) as urlopen,
    ):
        assert updater.check_update(raise_errors=True) == {"version": "999.0.0"}

    assert urlopen.call_args.kwargs["context"] is ssl_context


def test_linux_asset_requires_platform_manifest():
    with patch.object(updater, "UPDATE_PLATFORM_KEY", "linux-x86_64"):
        with pytest.raises(ValueError, match="缺少 platforms"):
            updater._get_platform_asset(
                {
                    "download_url": "https://example.com/MakeCode-Windows-X64.zip",
                    "sha256": "a" * 64,
                    "size": 1,
                }
            )


def test_windows_asset_supports_legacy_top_level_manifest():
    manifest = {
        "download_url": "https://example.com/MakeCode-Windows-X64.zip",
        "sha256": "a" * 64,
        "size": 1,
    }

    with patch.object(updater, "UPDATE_PLATFORM_KEY", "windows-x86_64"):
        assert updater._get_platform_asset(manifest) is manifest


def test_download_update_uses_linux_platform_asset(tmp_path):
    content = b"linux-update"
    asset = {
        "download_url": "https://example.com/MakeCode-Linux-X64.zip",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    manifest = {
        "platforms": {
            "windows-x86_64": {
                "download_url": "https://example.com/MakeCode-Windows-X64.zip",
                "sha256": "0" * 64,
                "size": 99,
            },
            "linux-x86_64": asset,
        }
    }

    ssl_context = object()
    with (
        patch.object(updater, "UPDATE_PLATFORM_KEY", "linux-x86_64"),
        patch.object(updater.tempfile, "mkdtemp", return_value=str(tmp_path)),
        patch.object(updater, "_create_ssl_context", return_value=ssl_context),
        patch.object(updater.urllib.request, "urlopen", return_value=_Response(content)) as urlopen,
    ):
        archive = updater.download_update(manifest)

    assert archive == tmp_path / "MakeCode-Linux-X64.zip"
    assert archive.read_bytes() == content
    assert urlopen.call_args.args[0].full_url == asset["download_url"]
    assert urlopen.call_args.kwargs["context"] is ssl_context


def test_launch_updater_runs_outside_install_directory(tmp_path):
    updater_dir = tmp_path / "updater"
    updater_dir.mkdir()
    updater_path = updater_dir / "updater.exe"
    archive = tmp_path / "update.zip"

    with (
        patch.object(updater, "AUTO_UPDATE_SUPPORTED", True),
        patch.object(updater, "_extract_updater_resource", return_value=updater_path),
        patch.object(updater.subprocess, "Popen") as popen,
        patch.object(updater.os, "_exit") as exit_process,
    ):
        updater.launch_updater(archive)

    assert popen.call_args.kwargs["cwd"] == str(updater_dir)
    assert popen.call_args.kwargs["close_fds"] is True
    exit_process.assert_called_once_with(0)
