import asyncio
from contextvars import ContextVar

from prompt_toolkit import prompt
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from system.console_render import console_lock

# Global Session Whitelist
SESSION_WHITELIST = set()

# Global HITL Switch (默认开启)
HITL_ENABLED = True

# Context Variable for Agent Role
current_agent_role = ContextVar("current_agent_role", default="#0 - Orchestrator")

console = Console()


def toggle_hitl(enabled: bool = None) -> bool:
    """切换 HITL 状态，返回新状态

    Args:
        enabled: 传 True/False 直接设置，传 None 则切换
    """
    global HITL_ENABLED
    if enabled is not None:
        HITL_ENABLED = enabled
    else:
        HITL_ENABLED = not HITL_ENABLED
    SESSION_WHITELIST.clear()  # 切换时清空白名单
    return HITL_ENABLED


def get_hitl_status() -> bool:
    """获取当前 HITL 状态"""
    return HITL_ENABLED


def interactive_hitl_prompt(action_key: str) -> str:
    """交互式拦截选项面板"""
    options = [
        ("1", f"允许本次执行 `{action_key}`"),
        ("2", f"允许整个会话期间执行 `{action_key}`"),
        ("3", "拒绝执行，并反馈原因"),
    ]

    selected_index = [0]
    kb = KeyBindings()

    @kb.add("up")
    def _go_up(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _go_down(event):
        selected_index[0] = min(len(options) - 1, selected_index[0] + 1)

    @kb.add("enter")
    def _confirm(event):
        event.app.exit(result=options[selected_index[0]][0])

    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result="abort")  # Ctrl+C 直接中断，不询问原因

    def get_formatted_text():
        result = [("class:title", "\n请使用 ↑/↓ 选择操作，Enter 确认:\n")]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f"👉 [{key}] {text}\n"))
            else:
                result.append(("class:unselected", f"   [{key}] {text}\n"))
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


def check_permission(action_type: str, action_name: str, details: str) -> tuple[bool, str]:
    # 如果 HITL 关闭，直接允许所有操作
    if not HITL_ENABLED:
        return True, ""

    action_key = f"{action_type}:{action_name}"

    if action_key in SESSION_WHITELIST:
        return True, ""

    with console_lock:
        # Double check in case another thread added it while waiting
        if action_key in SESSION_WHITELIST:
            return True, ""

        # Ensure the current thread has an event loop for prompt_toolkit
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("Event loop is closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        agent_name = current_agent_role.get()

        # Render UI
        panel_text = Text()
        panel_text.append(f"🤖 Agent: ", style="bold cyan")
        panel_text.append(f"{agent_name}\n")
        panel_text.append(f"🛠️ Action: ", style="bold yellow")
        panel_text.append(f"{action_key}\n")
        panel_text.append(f"🎯 Details: ", style="bold green")
        panel_text.append(f"{details}")

        panel = Panel(
            panel_text,
            title="⚠️ 敏感操作已拦截",
            border_style="red",
            expand=False
        )
        console.print(panel)

        choice = interactive_hitl_prompt(action_key)

        if choice == '1':
            return True, ""
        elif choice == '2':
            SESSION_WHITELIST.add(action_key)
            return True, ""
        elif choice == '3':
            try:
                reason = prompt("请输入拒绝原因（反馈给 Agent）: ").strip()
            except KeyboardInterrupt:
                reason = "用户通过 Ctrl+C 中断了操作。"
            except EOFError:
                reason = "用户通过 EOF 中断了操作。"
            return False, reason or "用户拒绝执行，未提供具体原因。"
        elif choice == 'abort':
            return False, "用户通过 Ctrl+C 中断了操作。"

    return False, "未知错误"
