import sys
from pathlib import Path


_is_frozen = getattr(sys, "frozen", False)
_INSTALL_DIR = Path(sys.executable).parent if _is_frozen else Path(__file__).resolve().parent.parent
if _is_frozen and sys.platform == "darwin":
    _INSTALL_MAKECODE_DIR = Path.home() / "Library" / "Application Support" / "MakeCode"
else:
    _INSTALL_MAKECODE_DIR = _INSTALL_DIR / ".makecode"
_INSTALL_MAKECODE_DIR.mkdir(parents=True, exist_ok=True)

_WORKDIR = Path.cwd().resolve()


def install_dir() -> Path:
    return _INSTALL_DIR


def install_makecode_dir() -> Path:
    _INSTALL_MAKECODE_DIR.mkdir(parents=True, exist_ok=True)
    return _INSTALL_MAKECODE_DIR


def workdir() -> Path:
    return _WORKDIR


def workspace_makecode_dir() -> Path:
    path = _WORKDIR / ".makecode"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_workdir(path: Path) -> Path:
    global _WORKDIR
    _WORKDIR = Path(path).expanduser().resolve()
    workspace_makecode_dir()
    return _WORKDIR


def workspace_transcript_dir() -> Path:
    return workspace_makecode_dir() / "transcripts"


def workspace_checkpoint_dir() -> Path:
    return workspace_makecode_dir() / "checkpoint"


def workspace_memory_dir() -> Path:
    return workspace_makecode_dir() / "memory"


def workspace_memory_jsonl_file() -> Path:
    return workspace_memory_dir() / "memory.jsonl"


def workspace_memory_config_file() -> Path:
    return workspace_memory_dir() / "memory_config.json"


def workspace_tasks_dir() -> Path:
    return workspace_makecode_dir() / "tasks"


def workspace_team_dir() -> Path:
    return workspace_makecode_dir() / "team"


def workspace_team_runs_dir() -> Path:
    return workspace_team_dir() / "runs"


def workspace_skills_dir() -> Path:
    return _WORKDIR / "skills"


def layout_config_file() -> Path:
    return install_makecode_dir() / "layout_config.json"


def mcp_config_file() -> Path:
    return install_makecode_dir() / "mcp_config.json"


def mcp_stderr_log_file() -> Path:
    return install_makecode_dir() / "mcp_stderr.log"


def error_log_file() -> Path:
    return install_makecode_dir() / "error.log"
