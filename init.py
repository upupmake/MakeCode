from dotenv import load_dotenv

load_dotenv(".env")
import os
from pathlib import Path

from openai import OpenAI

# 尝试导入 prompt_toolkit 用于构建高级交互菜单
try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.styles import Style
    from prompt_toolkit import prompt

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False


def _interactive_choose_mode(cwd: Path) -> str:
    """使用 prompt_toolkit 构建内联的 ↑/↓ 选择菜单"""
    options = [
        ("default", f"Current Directory ({cwd})"),
        ("custom", "Enter a custom path...")
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
        result = [("class:title", "\n📂 Select Workspace Directory (Use ↑/↓ arrows, Enter to confirm):\n")]
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

    style = Style([
        ("title", "fg:ansicyan bold"),
        ("selected", "fg:ansigreen bold"),
        ("unselected", "fg:ansigray"),
    ])

    # erase_when_done=True 会在选择完毕后擦除菜单，保持终端日志干净
    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()


def _init_workdir() -> Path:
    cwd = Path.cwd()

    # 1. 降级方案：如果没有安装 prompt_toolkit，使用传统 input
    if not PROMPT_TOOLKIT_AVAILABLE:
        try:
            print(f"\n\033[36m📂 Workspace Directory Setup\033[0m")
            user_input = input(
                f"\033[90mEnter path or press Enter for current ({cwd}):\033[0m\n\033[1;32m❯ \033[0m").strip()
            if not user_input:
                return cwd
            target = Path(user_input).expanduser().resolve()
            return target if target.exists() and target.is_dir() else cwd
        except (EOFError, KeyboardInterrupt):
            return cwd

    # 2. 高级方案：使用交互式菜单
    try:
        choice = _interactive_choose_mode(cwd)
    except Exception:
        choice = "abort"

    if choice == "abort":
        print(f"\n\033[33m⚠️  Setup cancelled. Defaulting to: {cwd}\033[0m\n")
        return cwd

    if choice == "default":
        print(f"\033[32m✅ Workspace set to: {cwd}\033[0m\n")
        return cwd

    # 3. 用户选择了自定义输入路径
    try:
        custom_style = Style.from_dict({'prompt': 'fg:ansigreen bold'})
        user_input = prompt("\n✏️  Enter custom workspace path:\n❯ ", style=custom_style).strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n\033[33m⚠️  Input cancelled. Defaulting to: {cwd}\033[0m\n")
        return cwd

    if not user_input:
        print(f"\033[32m✅ Using default directory: {cwd}\033[0m\n")
        return cwd

    target_path = Path(user_input).expanduser().resolve()

    if target_path.exists() and target_path.is_dir():
        print(f"\033[32m✅ Workspace set to: {target_path}\033[0m\n")
        return target_path
    else:
        print(f"\033[33m⚠️  Warning: Path '{target_path}' does not exist or is not a directory.\n"
              f"   Falling back to default: {cwd}\033[0m\n")
        return cwd


# 动态加载 WORKDIR
WORKDIR = _init_workdir()

# 初始化 OpenAI Client
client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY")
)
MODEL = os.environ["MODEL_ID"]
