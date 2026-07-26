import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from utils import paths

INSTALL_DIR = paths.install_dir()
INSTALL_MAKECODE_DIR = paths.install_makecode_dir()
WORKDIR = paths.workdir()
MAKECODE_DIR = paths.workspace_makecode_dir()


def set_workdir(path: Path) -> Path:
    global WORKDIR, MAKECODE_DIR
    WORKDIR = paths.set_workdir(path)
    MAKECODE_DIR = paths.workspace_makecode_dir()
    return WORKDIR


def should_prompt_for_workdir() -> bool:
    return os.getenv("MAKECODE_NON_INTERACTIVE") != "1"


def resolve_startup_workdir() -> Path:
    return Path.cwd().resolve()


def resolve_chosen_workdir(choice: str, cwd: Path | None = None) -> Path:
    cwd = (cwd or Path.cwd()).resolve()
    if choice == "abort" or choice == "default":
        return cwd
    user_input = choice.removeprefix("custom:") if choice.startswith("custom:") else ""
    if not user_input.strip():
        return cwd
    target_path = Path(user_input.strip()).expanduser().resolve()
    if target_path.exists() and target_path.is_dir():
        return target_path
    return cwd


def _get_error_log_path() -> Path:
    """错误日志路径 - 放在安装目录下"""
    log_path = paths.error_log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


# 确保在 Windows 控制台下可以安全打印 Emoji
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log_error_traceback(context: str, exc: Exception):
    try:
        log_path = _get_error_log_path()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                f"\n[{datetime.now().isoformat()}] [{context}] {type(exc).__name__}: {str(exc)}\n"
            )
            traceback.print_exc(file=f)
    except Exception as logging_exc:
        try:
            with open("makecode_init_fallback_error.log", "a", encoding="utf-8") as f:
                f.write(
                    f"\n[{datetime.now().isoformat()}] [log_error_traceback failure] "
                    f"{type(logging_exc).__name__}: {logging_exc}\n"
                )
        except Exception:
            pass


from shutil import which

SUPPORTED_TERMINAL_TYPES = ("powershell", "pwsh", "cmd", "bash", "zsh", "sh")


def _terminal_exists(terminal: str) -> bool:
    if terminal == "cmd":
        return sys.platform == "win32" and bool(which("cmd") or os.getenv("ComSpec"))
    return which(terminal) is not None


def _detect_startup_terminal_type() -> tuple[str | None, str]:
    """
    通过硬编码优先级寻找可用的终端环境。
    返回: (终端名称, 来源标识)
    """
    if sys.platform == "win32":
        # Windows: 优先使用较新的 PowerShell Core，其次 Windows PowerShell，最后 cmd
        candidates = ["pwsh", "powershell", "cmd"]
    elif sys.platform == "darwin":
        # macOS: 从 macOS Catalina 开始，默认终端是 zsh
        candidates = ["zsh", "bash", "sh"]
    else:
        # Linux / 其他 POSIX: 默认 bash 为主
        candidates = ["bash", "zsh", "sh"]

    for terminal in candidates:
        if _terminal_exists(terminal):
            # 因为是硬编码优先级，source 统一标记为 platform-fallback
            return terminal, "platform-fallback"

    return None, "unavailable"


STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE = _detect_startup_terminal_type()

# 初始化模型管理器 - 使用安装目录的配置
from system.models import init_model_manager
MODEL_MANAGER = init_model_manager(paths.install_makecode_dir())
