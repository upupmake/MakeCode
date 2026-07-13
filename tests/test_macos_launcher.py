import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS launcher test")


def test_macos_launcher_runs_bundled_onedir_binary(tmp_path):
    package = tmp_path / "package"
    launcher = package / "MakeCode.command"
    binary = package / "MakeCode" / "MakeCode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/zsh\nprintenv MAKECODE_LAUNCH_TEST > \"$1\"\n", encoding="utf-8")
    binary.chmod(0o700)

    source = Path("assets/MakeCode.command")
    launcher.write_bytes(source.read_bytes())
    launcher.chmod(0o700)

    marker = tmp_path / "marker"
    env = os.environ.copy()
    env["MAKECODE_LAUNCH_TEST"] = "started"
    subprocess.run([str(launcher), str(marker)], env=env, check=True)

    assert marker.read_text(encoding="utf-8").strip() == "started"


def test_macos_launcher_is_executable():
    launcher = Path("assets/MakeCode.command")

    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111

