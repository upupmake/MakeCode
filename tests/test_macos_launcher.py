import os
import shlex
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


@pytest.fixture
def macos_package(tmp_path):
    package = tmp_path / "package with spaces"
    binary = package / "MakeCode" / "MakeCode"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/zsh\nprint -r -- "$PWD" > "$1"\nprint -rl -- "${@:2}" >> "$1"\n',
        encoding="utf-8",
    )
    binary.chmod(0o700)
    launcher = package / "MakeCode.command"
    launcher.write_bytes(Path("assets/MakeCode.command").read_bytes())
    launcher.chmod(0o700)
    library = binary.parent / "_internal" / "nested" / "library"
    library.parent.mkdir(parents=True)
    library.write_text("test library", encoding="utf-8")
    return package


def _xattr(*arguments):
    return subprocess.run(
        ["/usr/bin/xattr", *map(str, arguments)], capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout


def _run_launcher(package, marker, answer=None):
    arguments = [
        "/bin/zsh", str(package / "MakeCode.command"), str(marker),
        "argument with spaces", "$literal",
    ]
    if answer is None:
        return subprocess.run(
            arguments, cwd=marker.parent, input="y\n", capture_output=True,
            text=True, timeout=10,
        )

    import pty

    master, slave = pty.openpty()
    try:
        os.write(master, (answer + "\n").encode())
        return subprocess.run(
            arguments, cwd=marker.parent, stdin=slave, capture_output=True,
            text=True, timeout=10,
        )
    finally:
        os.close(slave)
        os.close(master)


def test_macos_launcher_without_quarantine_preserves_arguments_and_workdir(macos_package, tmp_path):
    marker = tmp_path / "started"

    result = _run_launcher(macos_package, marker)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert marker.read_text(encoding="utf-8").splitlines() == [
        str(tmp_path.resolve()), "argument with spaces", "$literal",
    ]


@pytest.mark.parametrize("answer", ["y", "Y", "yes"])
def test_macos_launcher_confirmed_unquarantine_is_scoped_to_package(macos_package, tmp_path, answer):
    launcher = macos_package / "MakeCode.command"
    library = macos_package / "MakeCode" / "_internal" / "nested" / "library"
    unrelated = macos_package / "unrelated.txt"
    unrelated.write_text("not part of the app", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    external_library = outside / "library"
    external_library.write_text("outside the package", encoding="utf-8")
    external_link = macos_package / "MakeCode" / "external-link"
    internal_link = macos_package / "MakeCode" / "internal-link"
    external_link.symlink_to(outside, target_is_directory=True)
    internal_link.symlink_to(library)
    for path in (launcher, library, unrelated, external_library):
        _xattr("-w", "com.apple.quarantine", "0081;00000000;MakeCodeTests;", path)
    for link in (external_link, internal_link):
        _xattr("-ws", "com.apple.quarantine", "0081;00000000;MakeCodeTests;", link)
    _xattr("-w", "com.makecode.test", "preserve", library)
    marker = tmp_path / "started"

    result = _run_launcher(macos_package, marker, answer)

    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    assert "com.apple.quarantine" not in _xattr(launcher)
    assert "com.apple.quarantine" not in _xattr(library)
    assert "com.apple.quarantine" in _xattr(unrelated)
    assert "com.apple.quarantine" in _xattr(external_library)
    assert "com.apple.quarantine" not in _xattr("-s", external_link)
    assert "com.apple.quarantine" not in _xattr("-s", internal_link)
    assert _xattr("-p", "com.makecode.test", library).strip() == "preserve"
    assert "[y/N]" in result.stderr + result.stdout


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_macos_launcher_declining_keeps_quarantine_and_does_not_launch(macos_package, tmp_path, answer):
    library = macos_package / "MakeCode" / "_internal" / "nested" / "library"
    _xattr("-w", "com.apple.quarantine", "0081;00000000;MakeCodeTests;", library)
    marker = tmp_path / "started"

    result = _run_launcher(macos_package, marker, answer)

    assert result.returncode != 0
    assert not marker.exists()
    assert "com.apple.quarantine" in _xattr(library)


def test_macos_launcher_piped_confirmation_cannot_unquarantine(macos_package, tmp_path):
    library = macos_package / "MakeCode" / "_internal" / "nested" / "library"
    _xattr("-w", "com.apple.quarantine", "0081;00000000;MakeCodeTests;", library)
    marker = tmp_path / "started"

    result = _run_launcher(macos_package, marker)

    assert result.returncode != 0
    assert not marker.exists()
    assert "com.apple.quarantine" in _xattr(library)


@pytest.mark.parametrize("failure_stage", ["inspection", "removal"])
def test_macos_launcher_xattr_failure_does_not_launch(macos_package, tmp_path, failure_stage):
    fake_xattr = tmp_path / "fake xattr"
    inspection = (
        "exit 1" if failure_stage == "inspection"
        else 'print -r -- "$2: com.apple.quarantine"; exit 0'
    )
    fake_xattr.write_text(
        f'#!/bin/zsh\nif [[ "$1" == "-rs" ]]; then\n  {inspection}\nfi\nexit 1\n',
        encoding="utf-8",
    )
    fake_xattr.chmod(0o700)
    launcher = macos_package / "MakeCode.command"
    launcher.write_text(
        launcher.read_text(encoding="utf-8").replace("/usr/bin/xattr", shlex.quote(str(fake_xattr))),
        encoding="utf-8",
    )
    marker = tmp_path / "started"

    result = _run_launcher(macos_package, marker, "y")

    assert result.returncode != 0
    assert not marker.exists()

