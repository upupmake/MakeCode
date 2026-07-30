import json
import subprocess
import sys
from pathlib import Path

import pytest

from system.cli import COMMAND_DESCRIPTIONS, run_external_cli
from version import CURRENT_VERSION


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_main(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_no_arguments_keeps_interactive_startup_path():
    assert run_external_cli([]) is None


def test_version_exits_before_interactive_startup():
    result = _run_main("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == f"MakeCode {CURRENT_VERSION}"
    assert result.stderr == ""


def test_version_does_not_import_interactive_runtime_modules():
    script = """
import runpy
import sys

sys.argv = ["main.py", "--version"]
try:
    runpy.run_path("main.py", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0

forbidden = {"init", "system.commands", "system.tui_app"}.intersection(sys.modules)
assert not forbidden, sorted(forbidden)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_help_exits_before_interactive_startup():
    result = _run_main("--help")

    assert result.returncode == 0
    assert "usage: MakeCode" in result.stdout
    assert "--models-list" in result.stdout
    assert "--mcp-list" in result.stdout
    assert "--skills-list" in result.stdout
    assert "--check-update" in result.stdout
    assert "当前工作目录" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("option", ["--models-list", "--mcp-list", "--skills-list"])
def test_list_commands_do_not_import_interactive_runtime_modules(option):
    script = f"""
import sys
from system.cli import run_external_cli

assert run_external_cli(["{option}"]) == 0
forbidden = {{"init", "system.commands", "system.tui_app", "utils.mcp_manager"}}.intersection(sys.modules)
assert not forbidden, sorted(forbidden)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_commands_reuses_slash_command_catalog(capsys):
    exit_code = run_external_cli(["--commands"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "MakeCode 内置斜杠命令" in output
    assert all(command in output for command in COMMAND_DESCRIPTIONS)


def test_model_list_marks_current_model_without_exposing_api_key(tmp_path, monkeypatch, capsys):
    (tmp_path / "model_config.json").write_text(
        json.dumps({
            "last_selected": {
                "base_url": "https://user:secret-url-password@example.com/v1",
                "api_key": "secret-model-key",
                "model_id": "selected-model",
                "message_format": "openai_chat",
            },
            "models": [
                {
                    "base_url": "https://user:secret-url-password@example.com/v1",
                    "api_key": "secret-model-key",
                    "model_id": "selected-model",
                    "reasoning_effort": "high",
                    "message_format": "openai_chat",
                    "max_context": 256,
                }
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "utils.paths.install_makecode_dir",
        lambda *, create=True: tmp_path,
    )

    assert run_external_cli(["--models-list"]) == 0

    output = capsys.readouterr().out
    assert "selected-model" in output
    assert "effort=high" in output
    assert "context=256k" in output
    assert "当前" in output
    assert "example.com" in output
    assert "secret-model-key" not in output
    assert "secret-url-password" not in output


def test_mcp_list_hides_secrets_and_reports_config_state(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "mcp_config.json"
    config_file.write_text(
        json.dumps({
            "mcpServers": {
                "remote": {
                    "url": "https://user:password@example.com/mcp?token=secret-query#fragment",
                    "transport": "streamable-http",
                    "headers": {"Authorization": "Bearer secret-header"},
                    "disabled": False,
                },
                "local": {
                    "command": "uvx",
                    "args": ["secret-argument"],
                    "env": {"TOKEN": "secret-env"},
                    "disabled": True,
                },
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "utils.paths.mcp_config_file",
        lambda *, create=True: config_file,
    )

    assert run_external_cli(["--mcp-list"]) == 0

    output = capsys.readouterr().out
    assert "remote · 启用 · streamable-http · url=https://example.com/mcp" in output
    assert "local · 禁用 · stdio · command=uvx" in output
    for secret in ("password", "secret-query", "secret-header", "secret-argument", "secret-env"):
        assert secret not in output


def test_skill_list_uses_existing_directory_priority(tmp_path, monkeypatch, capsys):
    install_dir = tmp_path / "install"
    workspace_dir = tmp_path / "workspace"
    legacy_dir = tmp_path / "legacy"
    for directory, description in (
        (legacy_dir, "legacy description"),
        (workspace_dir, "workspace description"),
        (install_dir, "install description"),
    ):
        skill_dir = directory / "shared"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: {description}\n---\nbody\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "utils.skill_catalog.skill_directories",
        lambda *, create=True: [install_dir, workspace_dir, legacy_dir],
    )

    assert run_external_cli(["--skills-list"]) == 0

    output = capsys.readouterr().out
    assert "shared · install description" in output
    assert "workspace description" not in output
    assert "legacy description" not in output


def test_check_update_only_reports_available_version(monkeypatch, capsys):
    monkeypatch.setattr(
        "system.updater.check_update",
        lambda *, raise_errors: {
            "version": "9.9.9",
            "release_log": "Test release",
        },
    )

    exit_code = run_external_cli(["--check-update"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"当前版本: {CURRENT_VERSION}" in output
    assert "发现新版本: 9.9.9" in output
    assert "Test release" in output
    assert "只执行检查" in output
