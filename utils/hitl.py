from contextvars import ContextVar
from pathlib import Path

from system.console_render import console_lock
from system.tui_app import choose_tui

# Global Session Whitelist
SESSION_WHITELIST = set()

# Global Path Whitelist (stores allowed external directory prefixes)
PATH_WHITELIST: set[str] = set()

# Global HITL Switch (默认开启)
HITL_ENABLED = True

# Context Variable for Agent Role
current_agent_role = ContextVar("current_agent_role", default="#0 - Orchestrator")


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
    PATH_WHITELIST.clear()  # 切换时清空路径白名单
    return HITL_ENABLED


def get_hitl_status() -> bool:
    """获取当前 HITL 状态"""
    return HITL_ENABLED


def interactive_hitl_prompt(action_key: str) -> str:
    """交互式拦截选项面板"""
    options = [
        f"允许本次执行 `{action_key}`",
        f"允许整个会话期间执行 `{action_key}`",
        "拒绝执行，并反馈原因",
    ]
    choice = choose_tui("⚠️ 敏感操作已拦截", options, allow_custom=False)
    if choice == options[0]:
        return "1"
    if choice == options[1]:
        return "2"
    if choice == options[2]:
        return "3"
    return "abort"


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

        agent_name = current_agent_role.get()
        title = (
            "⚠️ 敏感操作已拦截\n"
            f"🤖 Agent: {agent_name}\n"
            f"🛠️ Action: {action_key}\n"
            f"🎯 Details: {details}"
        )

        choice = interactive_hitl_prompt(title)

        if choice == '1':
            return True, ""
        elif choice == '2':
            SESSION_WHITELIST.add(action_key)
            return True, ""
        elif choice == '3':
            reason = choose_tui("请输入拒绝原因（反馈给 Agent）", [], allow_custom=True)
            if reason == "<cancelled>":
                reason = "用户通过取消操作中断了操作。"
            return False, reason or "用户拒绝执行，未提供具体原因。"
        elif choice == 'abort':
            return False, "用户取消了操作。"

    return False, "未知错误"


def _is_path_whitelisted(resolved_path: Path) -> bool:
    """Check if a resolved path is under any whitelisted directory prefix."""
    path_str = resolved_path.as_posix()
    for prefix in list(PATH_WHITELIST):
        if path_str == prefix or path_str.startswith(prefix + "/"):
            return True
    return False


def check_path_permission(resolved_path: Path, tool_name: str) -> tuple[bool, str]:
    """Check if an out-of-workspace path access is allowed via HITL."""
    if not HITL_ENABLED:
        return True, ""

    if _is_path_whitelisted(resolved_path):
        return True, ""

    with console_lock:
        # Double check in case another thread added it while waiting
        if _is_path_whitelisted(resolved_path):
            return True, ""

        agent_name = current_agent_role.get()
        path_str = resolved_path.as_posix()
        whitelist_dir = resolved_path.as_posix() if resolved_path.is_dir() else resolved_path.parent.as_posix()
        options = [
            "允许本次访问",
            f"允许整个会话期间（目录: {whitelist_dir}，含子目录）",
            "拒绝",
        ]
        title = (
            "⚠️ 工作区外路径访问拦截\n"
            f"🤖 Agent: {agent_name}\n"
            f"🛠️ Tool: {tool_name}\n"
            f"📁 Path: {path_str}"
        )
        choice = choose_tui(title, options, allow_custom=False)

        if choice == options[0]:
            return True, ""
        elif choice == options[1]:
            PATH_WHITELIST.add(whitelist_dir)
            return True, ""
        elif choice == options[2]:
            reason = choose_tui("请输入拒绝原因（反馈给 Agent）", [], allow_custom=True)
            if reason == "<cancelled>":
                reason = "用户取消了操作。"
            return False, reason or "用户拒绝执行，未提供具体原因。"
        elif choice == '<cancelled>':
            return False, "用户取消了操作。"

    return False, "未知错误"


def check_paths_permission(resolved_paths: list[Path], tool_name: str) -> tuple[bool, str]:
    """Approve a group of workspace-external paths with one HITL prompt."""
    unique_paths = sorted({path.resolve() for path in resolved_paths}, key=lambda path: path.as_posix())
    if not unique_paths or not HITL_ENABLED:
        return True, ""

    pending = [path for path in unique_paths if not _is_path_whitelisted(path)]
    if not pending:
        return True, ""

    with console_lock:
        pending = [path for path in pending if not _is_path_whitelisted(path)]
        if not pending:
            return True, ""

        path_lines = "\n".join(f"- {path.as_posix()}" for path in pending)
        options = [
            "允许本次批量访问",
            "允许这些路径所在目录在本次会话访问",
            "拒绝",
        ]
        title = (
            "⚠️ 工作区外路径访问拦截\n"
            f"🤖 Agent: {current_agent_role.get()}\n"
            f"🛠️ Tool: {tool_name}\n"
            f"📁 Paths:\n{path_lines}"
        )
        choice = choose_tui(title, options, allow_custom=False)

        if choice == options[0]:
            return True, ""
        if choice == options[1]:
            for path in pending:
                PATH_WHITELIST.add(path.as_posix() if path.is_dir() else path.parent.as_posix())
            return True, ""
        if choice == options[2]:
            reason = choose_tui("请输入拒绝原因（反馈给 Agent）", [], allow_custom=True)
            if reason == "<cancelled>":
                reason = "用户取消了操作。"
            return False, reason or "用户拒绝执行，未提供具体原因。"
        if choice == "<cancelled>":
            return False, "用户取消了操作。"

    return False, "未知错误"
