import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ============================================================================
# 安装目录 (INSTALL_DIR) - 软件源码/打包所在目录
# ============================================================================
# 兼容 PyInstaller 打包环境
if getattr(sys, "frozen", False):
    # 打包后运行：使用 PyInstaller 的临时解压目录
    INSTALL_DIR = Path(sys._MEIPASS)
else:
    # 开发环境：使用源码目录
    INSTALL_DIR = Path(__file__).resolve().parent

# 安装目录下的配置目录
INSTALL_MAKECODE_DIR = INSTALL_DIR / ".makecode"

# 确保安装目录的配置目录存在
INSTALL_MAKECODE_DIR.mkdir(parents=True, exist_ok=True)


def _get_error_log_path() -> Path:
    """错误日志路径 - 放在安装目录下"""
    log_path = INSTALL_MAKECODE_DIR / "error.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


# 确保在 Windows 控制台下可以正确打印 Emoji
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML


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


try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.styles import Style
    from prompt_toolkit import prompt
except ImportError as exc:
    log_error_traceback("init prompt_toolkit import", exc)
    print_formatted_text(
        HTML(
            "\n<ansired>Error: prompt_toolkit is required but not installed. Please install it using `pip install prompt_toolkit`.</ansired>"
        )
    )
    sys.exit(1)


def _interactive_choose_mode(cwd: Path) -> str:
    """使用 prompt_toolkit 构建内联的 ↑/↓ 选择菜单"""
    options = [
        ("default", f"Current Directory ({cwd})"),
        ("custom", "Enter a custom path..."),
    ]
    selected_index = [0]  # 使用列表以在闭包中修改状态

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _(event):
        selected_index[0] = min(len(options) - 1, selected_index[0] + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=options[selected_index[0]][0])

    @kb.add("c-c")
    def _(event):
        event.app.exit(result="abort")

    def get_formatted_text():
        result = [
            (
                "class:title",
                "\n📂 Select Workspace Directory (Use ↑/↓ arrows, Enter to confirm):\n",
            )
        ]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f"  ❯ {text}\n"))
            else:
                result.append(("class:unselected", f"    {text}\n"))
        return result

    control = FormattedTextControl(get_formatted_text)
    # 动态计算高度，防止菜单把终端历史记录顶没
    window = Window(content=control, height=len(options) + 2)
    layout = Layout(window)

    style = Style(
        [
            ("title", "fg:ansicyan bold"),
            ("selected", "fg:ansigreen bold"),
            ("unselected", "fg:ansigray"),
        ]
    )

    # erase_when_done=True 会在选择完毕后擦除菜单，保持终端日志干净
    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()


def _init_workdir() -> Path:
    cwd = Path.cwd()

    try:
        choice = _interactive_choose_mode(cwd)
    except Exception as exc:
        log_error_traceback("init interactive choose mode", exc)
        choice = "abort"

    if choice == "abort":
        print_formatted_text(
            HTML(
                f"\n<ansiyellow>⚠️ Setup cancelled. Defaulting to: {cwd}</ansiyellow>\n"
            )
        )
        return cwd

    if choice == "default":
        print_formatted_text(
            HTML(f"<ansigreen>✅ Workspace set to: {cwd}</ansigreen>\n")
        )
        return cwd

    # 3. 用户选择了自定义输入路径
    try:
        print_formatted_text("\n✏️ Enter custom workspace path:")
        user_input = prompt(
            [("class:prompt", "📂 Target Directory ❯❯ ")],
            style=Style.from_dict({"prompt": "bold #00ffff"}),
        )
    except (EOFError, KeyboardInterrupt) as exc:
        log_error_traceback("init custom workdir input interrupted", exc)
        print_formatted_text(
            HTML(
                f"\n<ansiyellow>⚠️ Input cancelled. Defaulting to: {cwd}</ansiyellow>\n"
            )
        )
        return cwd

    if not user_input.strip():
        print_formatted_text(
            HTML(f"<ansigreen>✅ Using default directory: {cwd}</ansigreen>\n")
        )
        return cwd

    target_path = Path(user_input.strip()).expanduser().resolve()

    if target_path.exists() and target_path.is_dir():
        print_formatted_text(
            HTML(f"<ansigreen>✅ Workspace set to: {target_path}</ansigreen>\n")
        )
        return target_path
    else:
        print_formatted_text(
            HTML(
                f"<ansiyellow>⚠️ Warning: Path '{target_path}' does not exist or is not a directory.\n"
                f"   Falling back to default: {cwd}</ansiyellow>\n"
            )
        )
        return cwd


WORKDIR = _init_workdir()
MAKECODE_DIR = WORKDIR / ".makecode"
MAKECODE_DIR.mkdir(parents=True, exist_ok=True)

API_STANDARD = "chat"

import sys
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
MODEL_MANAGER = init_model_manager(INSTALL_MAKECODE_DIR)
