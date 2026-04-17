import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

# 确保在 Windows 控制台下可以正确打印 Emoji
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML


def _get_error_log_path() -> Path:
    workdir = globals().get("WORKDIR")
    base_dir = workdir if isinstance(workdir, Path) else Path.cwd()
    log_path = base_dir / ".makecode" / "error.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


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
                "\n 📂 Select Workspace Directory (Use ↑/↓ arrows, Enter to confirm):\n",
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
                f"\n<ansiyellow> ⚠️ Setup cancelled. Defaulting to: {cwd}</ansiyellow>\n"
            )
        )
        return cwd

    if choice == "default":
        print_formatted_text(
            HTML(f"<ansigreen> ✅ Workspace set to: {cwd}</ansigreen>\n")
        )
        return cwd

    # 3. 用户选择了自定义输入路径
    try:
        print_formatted_text("\n ✏️ Enter custom workspace path:")
        user_input = prompt(
            [("class:prompt", "📂 Target Directory ❯❯ ")],
            style=Style.from_dict({"prompt": "bold #00ffff"}),
        )
    except (EOFError, KeyboardInterrupt) as exc:
        log_error_traceback("init custom workdir input interrupted", exc)
        print_formatted_text(
            HTML(
                f"\n<ansiyellow> ⚠️ Input cancelled. Defaulting to: {cwd}</ansiyellow>\n"
            )
        )
        return cwd

    if not user_input.strip():
        print_formatted_text(
            HTML(f"<ansigreen> ✅ Using default directory: {cwd}</ansigreen>\n")
        )
        return cwd

    target_path = Path(user_input.strip()).expanduser().resolve()

    if target_path.exists() and target_path.is_dir():
        print_formatted_text(
            HTML(f"<ansigreen> ✅ Workspace set to: {target_path}</ansigreen>\n")
        )
        return target_path
    else:
        print_formatted_text(
            HTML(
                f"<ansiyellow> ⚠️ Warning: Path '{target_path}' does not exist or is not a directory.\n"
                f"   Falling back to default: {cwd}</ansiyellow>\n"
            )
        )
        return cwd


def _interactive_choose_api_standard() -> str:
    """使用 prompt_toolkit 构建内联的 ↑/↓ 选择菜单"""
    options = [("chat", "Chat Completions API"), ("response", "Responses API")]
    selected_index = [0]

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
                "\n ⚙️ Select LLM API Standard (Use ↑/↓ arrows, Enter to confirm):\n",
            )
        ]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f"  ❯ {text}\n"))
            else:
                result.append(("class:unselected", f"    {text}\n"))
        return result

    control = FormattedTextControl(get_formatted_text)
    window = Window(content=control, height=len(options) + 2)
    layout = Layout(window)
    style = Style(
        [
            ("title", "fg:ansicyan bold"),
            ("selected", "fg:ansigreen bold"),
            ("unselected", "fg:ansigray"),
        ]
    )
    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()


def _init_api_standard() -> str:
    try:
        choice = _interactive_choose_api_standard()
    except Exception as exc:
        log_error_traceback("init interactive api standard", exc)
        choice = "abort"

    if choice in ("abort", "chat"):
        print_formatted_text(
            HTML(
                "<ansigreen> ✅ API Standard set to: Chat Completions API</ansigreen>\n"
            )
        )
        return "chat"
    else:
        print_formatted_text(
            HTML("<ansigreen> ✅ API Standard set to: Responses API</ansigreen>\n")
        )
        return "response"


def _load_env_files():
    """只从当前项目工作区目录加载 .env"""
    workdir_env = str(WORKDIR / ".env")
    try:
        with open(workdir_env, encoding="utf-8", mode="r") as f:
            for line in f.readlines():
                if line.strip() and not line.strip().startswith("#") and "=" in line:
                    key, value = line.strip().split("=", 1)
                    key = key.strip()
                    value = value.strip("'\"")

                    if key in os.environ:
                        if os.environ[key] != value:
                            print_formatted_text(
                                HTML(
                                    f"\n<ansiyellow> ⚠️ Conflict detected for environment variable: {key}</ansiyellow>"
                                )
                            )
                            print_formatted_text(f"  Current value : {os.environ[key]}")
                            print_formatted_text(f"  Value in .env : {value}")
                            try:
                                choice = prompt(
                                    [
                                        (
                                            "class:prompt",
                                            "❓ Override current value with .env? [y/N] ❯❯ ",
                                        )
                                    ],
                                    style=Style.from_dict({"prompt": "bold #00ffff"}),
                                )
                            except (EOFError, KeyboardInterrupt):
                                choice = "n"
                                print_formatted_text("")

                            if choice.strip().lower() == "y":
                                os.environ[key] = value
                                print_formatted_text(
                                    HTML(f"<ansigreen> ✅ Overridden {key}</ansigreen>")
                                )
                            else:
                                print_formatted_text(
                                    HTML(f"<ansigray> ⏭️ Skipped {key}</ansigray>")
                                )
                    else:
                        os.environ[key] = value
        print_formatted_text(
            HTML(
                f"<ansiblue> ℹ️ Loaded environment variables from Workspace: {workdir_env}</ansiblue>"
            )
        )
    except FileNotFoundError:
        pass


WORKDIR = _init_workdir()
MAKECODE_DIR = WORKDIR / ".makecode"
MAKECODE_DIR.mkdir(parents=True, exist_ok=True)

# 确保在 WORKDIR 初始化后加载项目专属 .env
_load_env_files()

API_STANDARD = _init_api_standard()

try:
    API_KEY = os.environ["OPENAI_API_KEY"]
    BASE_URL = os.environ["OPENAI_BASE_URL"]
    MODEL = os.environ["MODEL_ID"]
except KeyError as exc:
    log_error_traceback("init missing required env", exc)
    print_formatted_text(
        HTML(
            "\n<ansired> ⚠️ Error: Missing required environment variables.</ansired>\n"
            "<ansiyellow>Please ensure OPENAI_API_KEY, OPENAI_BASE_URL, and MODEL_ID are set in your .env file or system environment.</ansiyellow>"
        )
    )
    input("\nPress Enter to exit... (按回车键退出...)")
    sys.exit(1)
