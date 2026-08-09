import os
import sys
from pathlib import Path


_is_frozen = getattr(sys, "frozen", False)
_INSTALL_DIR = Path(sys.executable).parent if _is_frozen else Path(__file__).resolve().parent.parent
if _is_frozen and sys.platform == "darwin":
    _INSTALL_MAKECODE_DIR = Path.home() / "Library" / "Application Support" / "MakeCode"
else:
    _INSTALL_MAKECODE_DIR = _INSTALL_DIR / ".makecode"

_WORKDIR = Path.cwd().resolve()


def install_dir() -> Path:
    return _INSTALL_DIR


def install_makecode_dir(*, create: bool = True) -> Path:
    if create:
        _INSTALL_MAKECODE_DIR.mkdir(parents=True, exist_ok=True)
    return _INSTALL_MAKECODE_DIR


def workdir() -> Path:
    return _WORKDIR


def workspace_makecode_dir(*, create: bool = True) -> Path:
    path = _WORKDIR / ".makecode"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def set_workdir(path: Path) -> Path:
    global _WORKDIR
    _WORKDIR = Path(path).expanduser().resolve()
    workspace_makecode_dir()
    return _WORKDIR


def directory_completion_candidates(path_fragment: str, base_dir: Path) -> list[str]:
    if path_fragment == "~":
        return ["~/"] if Path.home().is_dir() else []

    separator_index = max(path_fragment.rfind("/"), path_fragment.rfind(os.sep))
    if path_fragment.endswith(("/", os.sep)):
        parent_text = path_fragment
        name_prefix = ""
    elif separator_index >= 0:
        parent_text = path_fragment[:separator_index + 1]
        name_prefix = path_fragment[separator_index + 1:]
    else:
        parent_text = ""
        name_prefix = path_fragment

    parent_path = Path(os.path.expanduser(parent_text or "."))
    if not parent_path.is_absolute():
        parent_path = Path(base_dir) / parent_path
    try:
        directories = [
            child.name
            for child in parent_path.resolve().iterdir()
            if child.is_dir()
            and child.name.startswith(name_prefix)
            and (name_prefix.startswith(".") or not child.name.startswith("."))
        ]
    except OSError:
        return []

    directories.sort()
    separator = "/" if "/" in path_fragment or os.sep == "/" else os.sep
    return [f"{parent_text}{directory}{separator}" for directory in directories]


def workspace_conversations_dir() -> Path:
    return workspace_makecode_dir() / "conversations"


def workspace_transcript_dir() -> Path:
    return workspace_makecode_dir() / "transcripts"


def workspace_memory_dir() -> Path:
    return workspace_makecode_dir() / "memory"


def workspace_memory_jsonl_file() -> Path:
    return workspace_memory_dir() / "memory.jsonl"


def workspace_memory_config_file() -> Path:
    return workspace_memory_dir() / "memory_config.json"


def install_skills_dir(*, create: bool = True) -> Path:
    path = _INSTALL_DIR / ".makecode" / "skills"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_skills_dir(*, create: bool = True) -> Path:
    path = workspace_makecode_dir(create=create) / "skills"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_legacy_skills_dir() -> Path:
    return _WORKDIR / "skills"


def workspace_disabled_skills_file(*, create: bool = True) -> Path:
    return workspace_makecode_dir(create=create) / "disabled_skills.json"


def layout_config_file() -> Path:
    return install_makecode_dir() / "layout_config.json"


def mcp_config_file(*, create: bool = True) -> Path:
    return install_makecode_dir(create=create) / "mcp_config.json"


def mcp_stderr_log_file() -> Path:
    return install_makecode_dir() / "mcp_stderr.log"


def error_log_file() -> Path:
    return install_makecode_dir() / "error.log"
