import json
import os
import stat
from pathlib import Path

import pytest

from utils import paths
from utils.skill_catalog import discover_skills, skill_directories
from utils.skills import SkillLoader

_ENV_NAMES = (
    "MAKECODE_CONFIG_DIR",
    "MAKECODE_WORKSPACE_CONFIG_DIR",
    "MAKECODE_SKILLS_DIRS",
    "MAKECODE_MCP_CONFIG",
)


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    default_shared = tmp_path / "default-shared"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(paths, "_INSTALL_DIR", tmp_path / "install")
    monkeypatch.setattr(paths, "_INSTALL_MAKECODE_DIR", default_shared)
    monkeypatch.setattr(paths, "_WORKDIR", workspace)
    return default_shared, workspace


def _write_skill(directory: Path, name: str, description: str) -> None:
    skill_dir = directory / description
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{description}\n",
        encoding="utf-8",
    )


def test_default_paths_are_unchanged(isolated_paths):
    default_shared, workspace = isolated_paths

    assert paths.install_makecode_dir() == default_shared
    assert default_shared.is_dir()
    assert paths.workspace_makecode_dir() == workspace / ".makecode"
    assert paths.workspace_conversations_dir() == workspace / ".makecode" / "conversations"
    assert paths.workspace_memory_dir() == workspace / ".makecode" / "memory"
    assert paths.workspace_memory_db_file() == workspace / ".makecode" / "memory" / "memory.db"
    assert paths.workspace_skills_dir() == workspace / ".makecode" / "skills"
    assert paths.workspace_legacy_skills_dir() == workspace / "skills"
    assert paths.workspace_disabled_skills_file() == (
        workspace / ".makecode" / "disabled_skills.json"
    )
    assert paths.install_skills_dir() == default_shared / "skills"
    assert paths.mcp_config_file() == default_shared / "mcp_config.json"
    assert paths.memory_config_file() == default_shared / "memory_config.json"
    assert paths.layout_config_file() == default_shared / "layout_config.json"
    assert paths.error_log_file() == default_shared / "error.log"
    assert paths.mcp_stderr_log_file() == default_shared / "mcp_stderr.log"
    assert paths.skills_dirs_override() is None
    assert skill_directories() == [
        default_shared / "skills",
        workspace / ".makecode" / "skills",
        workspace / "skills",
    ]


def test_config_dir_override_moves_every_shared_file(isolated_paths, tmp_path, monkeypatch):
    from system.models import ModelManager
    from tools.extra_tools import extra_tools_config_file

    default_shared, workspace = isolated_paths
    config_root = tmp_path / "runner-config"
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))

    assert paths.install_makecode_dir() == config_root.resolve()
    assert paths.install_dir() == tmp_path / "install"
    model_config = ModelManager(paths.install_makecode_dir(create=False)).config_file
    shared_files = {
        model_config,
        extra_tools_config_file(),
        paths.mcp_config_file(),
        paths.memory_config_file(),
        paths.layout_config_file(),
        paths.error_log_file(),
        paths.mcp_stderr_log_file(),
        paths.install_skills_dir(),
    }
    assert all(path.parent == config_root.resolve() for path in shared_files)
    if os.name != "nt":
        assert config_root.stat().st_mode & 0o077 == 0

    for path in shared_files:
        if path.is_dir():
            (path / "example.txt").write_text("data", encoding="utf-8")
        else:
            path.write_text("data", encoding="utf-8")

    assert not default_shared.exists()
    assert not (workspace / ".makecode").exists()


def test_config_dir_relative_path_resolves_against_process_cwd(
    isolated_paths, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", "relative-config")

    assert paths.install_makecode_dir() == (tmp_path / "relative-config").resolve()


def test_config_dir_empty_string_keeps_default(isolated_paths, monkeypatch):
    default_shared, _ = isolated_paths
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", "")

    assert paths.install_makecode_dir() == default_shared


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_config_dir_permissions_are_not_changed(
    isolated_paths, tmp_path, monkeypatch
):
    config_root = tmp_path / "existing-config"
    config_root.mkdir()
    config_root.chmod(0o755)
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))

    assert paths.install_makecode_dir() == config_root.resolve()
    assert config_root.stat().st_mode & 0o777 == 0o755


def test_workspace_config_dir_override_moves_workspace_files(
    isolated_paths, tmp_path, monkeypatch
):
    _, workspace = isolated_paths
    workspace_config = tmp_path / "runner-workspace"
    monkeypatch.setenv("MAKECODE_WORKSPACE_CONFIG_DIR", str(workspace_config))

    from utils.conversations import ConversationStore

    assert paths.workdir() == workspace
    assert paths.workspace_makecode_dir() == workspace_config.resolve()
    assert ConversationStore().root == workspace_config.resolve() / "conversations"
    assert paths.workspace_conversations_dir() == workspace_config.resolve() / "conversations"
    assert paths.workspace_memory_dir() == workspace_config.resolve() / "memory"
    assert paths.workspace_memory_db_file() == workspace_config.resolve() / "memory" / "memory.db"
    assert paths.workspace_skills_dir() == workspace_config.resolve() / "skills"
    assert paths.workspace_disabled_skills_file() == (
        workspace_config.resolve() / "disabled_skills.json"
    )
    assert not (workspace / ".makecode").exists()


def test_workspace_config_dir_relative_path_resolves_against_workdir(
    isolated_paths, monkeypatch
):
    _, workspace = isolated_paths
    monkeypatch.setenv("MAKECODE_WORKSPACE_CONFIG_DIR", "runner-workspace")

    assert paths.workspace_makecode_dir() == workspace / "runner-workspace"


def test_workspace_config_dir_empty_string_keeps_default(isolated_paths, monkeypatch):
    _, workspace = isolated_paths
    monkeypatch.setenv("MAKECODE_WORKSPACE_CONFIG_DIR", "")

    assert paths.workspace_makecode_dir() == workspace / ".makecode"


def test_workspace_refresh_after_workdir_change_uses_relative_override(
    isolated_paths, tmp_path, monkeypatch
):
    _, first_workspace = isolated_paths
    second_workspace = tmp_path / "second-workspace"
    monkeypatch.setenv("MAKECODE_WORKSPACE_CONFIG_DIR", "runner-workspace")

    paths.set_workdir(first_workspace)
    assert paths.workspace_memory_db_file() == (
        first_workspace / "runner-workspace" / "memory" / "memory.db"
    )

    paths.set_workdir(second_workspace)
    assert paths.workspace_memory_db_file() == (
        second_workspace / "runner-workspace" / "memory" / "memory.db"
    )


def test_skills_dirs_override_replaces_defaults_and_keeps_priority(
    isolated_paths, tmp_path, monkeypatch
):
    default_shared, workspace = isolated_paths
    first_dir = tmp_path / "explicit-first"
    second_dir = tmp_path / "explicit-second"
    _write_skill(first_dir, "shared", "first")
    _write_skill(first_dir, "first-only", "first-only")
    _write_skill(second_dir, "shared", "second")
    _write_skill(second_dir, "second-only", "second-only")
    _write_skill(workspace / "skills", "legacy-only", "legacy-only")
    monkeypatch.setenv(
        "MAKECODE_SKILLS_DIRS",
        os.pathsep.join([str(first_dir), str(second_dir)]),
    )

    directories = skill_directories()
    assert directories == [first_dir.resolve(), second_dir.resolve()]
    assert not default_shared.exists()
    assert not (workspace / ".makecode").exists()
    discovered = discover_skills(directories)
    assert discovered["shared"]["meta"]["description"] == "first"
    assert list(discovered).index("first-only") < list(discovered).index("second-only")

    loader = SkillLoader()
    assert loader.skills_dirs == directories
    assert set(loader.skills) == {"shared", "first-only", "second-only"}
    prompt = loader.render_prompt_block()
    assert str(first_dir.resolve().as_posix()) in prompt
    assert "Default installation directory" not in prompt
    assert "MAKECODE_SKILLS_DIRS" in prompt
    assert not default_shared.exists()


def test_relative_skills_dirs_resolve_against_workdir(isolated_paths, monkeypatch):
    _, workspace = isolated_paths
    monkeypatch.setenv("MAKECODE_SKILLS_DIRS", "runner-skills")

    assert paths.skills_dirs_override() == [workspace / "runner-skills"]


def test_empty_skills_dirs_disables_scanning(isolated_paths, monkeypatch, capsys):
    default_shared, workspace = isolated_paths
    _write_skill(default_shared / "skills", "default-only", "default-only")
    _write_skill(workspace / "skills", "legacy-only", "legacy-only")
    monkeypatch.setenv("MAKECODE_SKILLS_DIRS", "")

    assert paths.skills_dirs_override() == []
    assert skill_directories() == []
    assert discover_skills() == {}
    assert SkillLoader().skills == {}
    assert "MAKECODE_SKILLS_DIRS is empty" in SkillLoader().render_prompt_block()

    from system.cli import run_external_cli

    assert run_external_cli(["--skills-list"]) == 0
    output = capsys.readouterr().out
    assert "Skills 搜索目录" in output
    assert "当前工作区没有可用 Skills。" in output
    assert "default-only" not in output
    assert "legacy-only" not in output


def test_skill_list_uses_explicit_directory_order(
    isolated_paths, tmp_path, monkeypatch, capsys
):
    _, workspace = isolated_paths
    first_dir = tmp_path / "explicit-first"
    second_dir = tmp_path / "explicit-second"
    _write_skill(first_dir, "shared", "first")
    _write_skill(second_dir, "shared", "second")
    _write_skill(workspace / "skills", "legacy-only", "legacy-only")
    monkeypatch.setenv(
        "MAKECODE_SKILLS_DIRS",
        os.pathsep.join([str(first_dir), str(second_dir)]),
    )

    from system.cli import run_external_cli

    assert run_external_cli(["--skills-list"]) == 0
    output = capsys.readouterr().out
    assert output.index(str(first_dir.resolve())) < output.index(
        str(second_dir.resolve())
    )
    assert "shared · first · 已启用" in output
    assert "shared · second" not in output
    assert "legacy-only" not in output


def test_mcp_config_override_wins_over_config_dir(isolated_paths, tmp_path, monkeypatch):
    config_root = tmp_path / "runner-config"
    explicit_mcp = tmp_path / "explicit" / "mcp.json"
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("MAKECODE_MCP_CONFIG", str(explicit_mcp))

    assert paths.mcp_config_file() == explicit_mcp
    assert paths.mcp_config_file(create=False) == explicit_mcp


def test_mcp_config_falls_back_to_config_dir(isolated_paths, tmp_path, monkeypatch):
    config_root = tmp_path / "runner-config"
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))
    monkeypatch.delenv("MAKECODE_MCP_CONFIG", raising=False)

    assert paths.mcp_config_file() == config_root.resolve() / "mcp_config.json"


def test_empty_mcp_config_override_falls_back_to_config_dir(
    isolated_paths, tmp_path, monkeypatch
):
    config_root = tmp_path / "runner-config"
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("MAKECODE_MCP_CONFIG", "")

    assert paths.mcp_config_file() == config_root.resolve() / "mcp_config.json"


def test_tui_layout_config_follows_config_dir(isolated_paths, tmp_path, monkeypatch):
    from system.tui_types import load_layout_ratios, save_layout_ratios

    default_shared, _ = isolated_paths
    config_root = tmp_path / "runner-config"
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))

    save_layout_ratios({"task": 5, "background": 2, "sub_agent": 1})

    assert load_layout_ratios() == {"task": 5, "background": 2, "sub_agent": 1}
    assert (config_root.resolve() / "layout_config.json").is_file()
    assert not default_shared.exists()


def test_unwritable_config_dir_raises_clear_error(isolated_paths, tmp_path, monkeypatch):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permission checks are not meaningful for this user")
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(readonly / "nested"))

    try:
        with pytest.raises(PermissionError):
            paths.install_makecode_dir()
    finally:
        readonly.chmod(stat.S_IRWXU)


def test_system_init_reports_config_paths_and_capabilities(
    isolated_paths, tmp_path, monkeypatch
):
    from system.cli import HeadlessOptions

    default_shared, workspace = isolated_paths
    config_root = tmp_path / "runner-config"
    workspace_config = tmp_path / "runner-workspace"
    skills_dir = tmp_path / "runner-skills"
    skills_dir.mkdir()
    monkeypatch.setenv("MAKECODE_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("MAKECODE_WORKSPACE_CONFIG_DIR", str(workspace_config))
    monkeypatch.setenv("MAKECODE_SKILLS_DIRS", str(skills_dir))
    monkeypatch.delenv("MAKECODE_MCP_CONFIG", raising=False)
    monkeypatch.setattr(paths, "_WORKDIR", workspace)

    from system.headless import HeadlessRunner

    fields = HeadlessRunner(HeadlessOptions(prompt="hi"))._startup_fields(
        snapshot=None,
        tool_names=[],
        mcp_tools=[],
    )

    assert fields["config_paths"] == {
        "shared_config_dir": str(config_root.resolve()),
        "workspace_config_dir": str(workspace_config.resolve()),
        "skills_dirs": [str(skills_dir.resolve())],
        "mcp_config": str(config_root.resolve() / "mcp_config.json"),
    }
    assert fields["capabilities"] == {
        "configurable_paths": True,
        "skills_dirs_override": True,
        "notification_channel": "stream-json-notification-v1",
    }
    assert not default_shared.exists()
    assert not (workspace / ".makecode").exists()
    json.dumps(fields)


def test_system_init_reports_real_default_paths(isolated_paths):
    from system.cli import HeadlessOptions
    from system.headless import HeadlessRunner

    default_shared, workspace = isolated_paths
    fields = HeadlessRunner(HeadlessOptions(prompt="hi"))._startup_fields(
        snapshot=None,
        tool_names=[],
        mcp_tools=[],
    )

    assert fields["config_paths"]["shared_config_dir"] == str(default_shared.resolve())
    assert fields["config_paths"]["workspace_config_dir"] == str(
        (workspace / ".makecode").resolve()
    )
    assert fields["config_paths"]["skills_dirs"] == [
        str((default_shared / "skills").resolve()),
        str((workspace / ".makecode" / "skills").resolve()),
        str((workspace / "skills").resolve()),
    ]
    assert fields["config_paths"]["mcp_config"] == str(
        (default_shared / "mcp_config.json").resolve()
    )
