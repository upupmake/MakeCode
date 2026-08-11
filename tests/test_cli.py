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
    assert "--mcp-add" in result.stdout
    assert "--skills-list" in result.stdout
    assert "--memory-list" in result.stdout
    assert "--check-update" in result.stdout
    assert "--update" in result.stdout
    assert "-y, --yes" in result.stdout
    assert "当前工作目录" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("option", ["--models-list", "--mcp-list", "--skills-list", "--memory-list"])
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


def test_commands_does_not_include_runtime_skill_slash_commands(tmp_path, monkeypatch, capsys):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "enabled-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: enabled-skill\ndescription: enabled skill description\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "utils.skill_catalog.skill_directories",
        lambda *, create=True: [skills_dir],
    )
    monkeypatch.setattr("utils.paths._WORKDIR", tmp_path)

    assert run_external_cli(["--commands"]) == 0

    output = capsys.readouterr().out
    assert "/enabled-skill" not in output
    assert all(command in output for command in COMMAND_DESCRIPTIONS)


def test_update_exits_before_interactive_startup_in_source_environment():
    result = _run_main("--update", "--yes")

    assert result.returncode == 1
    assert "源码运行环境不支持自动更新" in result.stderr
    assert "当前工作目录" not in result.stdout


def test_mcp_add_does_not_import_interactive_or_mcp_runtime_modules():
    script = """
import sys
import tempfile
from pathlib import Path

from system.cli import run_external_cli
from utils import paths

with tempfile.TemporaryDirectory() as directory:
    config_file = Path(directory) / "mcp_config.json"
    paths.mcp_config_file = lambda *, create=True: config_file
    assert run_external_cli(["--mcp-add", "api", "--url", "https://example.com/mcp"]) == 0

forbidden = {"init", "system.commands", "system.tui_app", "utils.mcp_manager", "fastmcp"}.intersection(sys.modules)
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
    assert "context=" not in output
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
    monkeypatch.setattr("utils.paths._WORKDIR", tmp_path)
    (tmp_path / ".makecode").mkdir()
    (tmp_path / ".makecode" / "disabled_skills.json").write_text(
        '["shared"]', encoding="utf-8"
    )

    assert run_external_cli(["--skills-list"]) == 0

    output = capsys.readouterr().out
    assert "shared · install description · 已禁用" in output
    assert "workspace description" not in output
    assert "legacy description" not in output


def test_memory_list_reads_active_records_in_updated_order(tmp_path, monkeypatch, capsys):
    makecode_dir = tmp_path / ".makecode"
    memory_file = makecode_dir / "memory" / "memory.jsonl"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text(
        "\n".join(
            [
                json.dumps({
                    "id": "newer",
                    "status": "active",
                    "category": "workflow",
                    "updated_at": "2026-07-30 12:00:00",
                    "insight": "newer insight",
                    "reuse_condition": "newer reuse",
                }),
                json.dumps({
                    "id": "deleted",
                    "status": "deleted",
                    "category": "workflow",
                    "updated_at": "2026-07-30 10:00:00",
                    "insight": "deleted insight",
                    "reuse_condition": "deleted reuse",
                }),
                json.dumps({
                    "id": "older",
                    "status": "active",
                    "category": "preference",
                    "created_at": "2026-07-30 11:00:00",
                    "insight": "older\ninsight",
                    "reuse_condition": "older\treuse",
                }),
            ]
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "utils.paths.workspace_makecode_dir",
        lambda *, create=True: makecode_dir,
    )

    assert run_external_cli(["--memory-list"]) == 0

    output = capsys.readouterr().out
    assert "active: 2" in output
    assert output.index("older") < output.index("newer")
    assert "older insight" in output
    assert "older reuse" in output
    assert "deleted" not in output


def test_mcp_add_writes_disabled_config_without_printing_secrets(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "mcp_config.json"
    monkeypatch.setattr(
        "utils.paths.mcp_config_file",
        lambda *, create=True: config_file,
    )

    assert run_external_cli([
        "--mcp-add",
        "api",
        "--url",
        "https://example.com/mcp",
        "--transport",
        "http",
        "--header",
        "Authorization=Bearer-secret",
    ]) == 0

    config = json.loads(config_file.read_text(encoding="utf-8"))["mcpServers"]["api"]
    assert config == {
        "url": "https://example.com/mcp",
        "transport": "streamable-http",
        "disabled": True,
        "headers": {"Authorization": "Bearer-secret"},
    }
    output = capsys.readouterr().out
    assert "已添加 MCP 服务: api" in output
    assert "默认为禁用状态" in output
    assert "Bearer-secret" not in output


def test_mcp_add_stdio_preserves_command_arguments_after_separator(tmp_path, monkeypatch):
    config_file = tmp_path / "mcp_config.json"
    monkeypatch.setattr(
        "utils.paths.mcp_config_file",
        lambda *, create=True: config_file,
    )

    assert run_external_cli([
        "--mcp-add",
        "filesystem",
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        ".",
    ]) == 0

    config = json.loads(config_file.read_text(encoding="utf-8"))["mcpServers"]["filesystem"]
    assert config == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "disabled": True,
        "transport": "stdio",
    }


def test_mcp_add_preserves_invalid_existing_config(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "mcp_config.json"
    original = b"{invalid-json"
    config_file.write_bytes(original)
    monkeypatch.setattr(
        "utils.paths.mcp_config_file",
        lambda *, create=True: config_file,
    )

    assert run_external_cli(["--mcp-add", "api", "--url", "https://example.com/mcp"]) == 1
    assert config_file.read_bytes() == original
    assert "无法添加 MCP 服务配置" in capsys.readouterr().err


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


class _TTYInput:
    def isatty(self):
        return True


@pytest.mark.parametrize("yes_option", ["-y", "--yes"])
def test_update_yes_downloads_and_launches_updater(yes_option, tmp_path, monkeypatch, capsys):
    version_info = {"version": "9.9.9", "release_log": "Test release"}
    update_archive = tmp_path / "update.zip"
    launched = []

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("system.updater.AUTO_UPDATE_SUPPORTED", True)
    monkeypatch.setattr("system.updater.check_update", lambda *, raise_errors: version_info)

    def download_update(info, progress_callback):
        assert info is version_info
        progress_callback(5, 10)
        return update_archive

    monkeypatch.setattr("system.updater.download_update", download_update)
    monkeypatch.setattr("system.updater.launch_updater", launched.append)

    assert run_external_cli(["--update", yes_option]) == 0

    output = capsys.readouterr().out
    assert "发现新版本: 9.9.9" in output
    assert "Test release" in output
    assert "下载进度:" in output
    assert launched == [update_archive]


def test_update_requires_confirmation_before_download(monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdin", _TTYInput())
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr("system.updater.AUTO_UPDATE_SUPPORTED", True)
    monkeypatch.setattr(
        "system.updater.check_update",
        lambda *, raise_errors: {"version": "9.9.9"},
    )
    monkeypatch.setattr(
        "system.updater.download_update",
        lambda *args, **kwargs: pytest.fail("cancelled update must not download"),
    )

    assert run_external_cli(["--update"]) == 0
    assert "已取消更新" in capsys.readouterr().out


def test_update_without_tty_requires_explicit_yes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("system.updater.AUTO_UPDATE_SUPPORTED", True)
    monkeypatch.setattr(
        "system.updater.check_update",
        lambda *, raise_errors: {"version": "9.9.9"},
    )
    monkeypatch.setattr(
        "system.updater.download_update",
        lambda *args, **kwargs: pytest.fail("unconfirmed update must not download"),
    )

    assert run_external_cli(["--update"]) == 1
    assert "非交互终端无法确认更新" in capsys.readouterr().err


def test_update_rejects_unsupported_platform_before_network(monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("system.updater.AUTO_UPDATE_SUPPORTED", False)
    monkeypatch.setattr(
        "system.updater.check_update",
        lambda *args, **kwargs: pytest.fail("unsupported platform must not check for updates"),
    )

    assert run_external_cli(["--update", "--yes"]) == 1
    assert "当前平台不支持自动更新" in capsys.readouterr().err
