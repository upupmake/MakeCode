from contextvars import ContextVar
from pathlib import Path

from system.approvals import APPROVALS, ApprovalRequest
from system.console_render import console_lock

# Global Session Whitelist
SESSION_WHITELIST: set[str] = set()

# Global Path Whitelist (stores allowed external directory prefixes)
PATH_WHITELIST: set[str] = set()

# Global HITL Switch (默认开启)
HITL_ENABLED = True

# Context Variable for Agent Role
current_agent_role = ContextVar("current_agent_role", default="#0 - Orchestrator")


def toggle_hitl(enabled: bool | None = None) -> bool:
    """切换 HITL 状态，返回新状态

    Args:
        enabled: 传 True/False 直接设置，传 None 则切换
    """
    global HITL_ENABLED
    HITL_ENABLED = enabled if enabled is not None else not HITL_ENABLED
    SESSION_WHITELIST.clear()  # 切换时清空白名单
    PATH_WHITELIST.clear()  # 切换时清空路径白名单
    return HITL_ENABLED


def get_hitl_status() -> bool:
    """获取当前 HITL 状态"""
    return HITL_ENABLED


def choose_tui(*args, **kwargs):
    """Lazy proxy so importing HITL never imports the Textual TUI."""
    from system.tui_app import choose_tui as _choose_tui

    return _choose_tui(*args, **kwargs)


def prompt_hitl_tui(*args, **kwargs):
    """Lazy proxy so importing HITL never imports the Textual TUI."""
    from system.tui_app import prompt_hitl_tui as _prompt_hitl_tui

    return _prompt_hitl_tui(*args, **kwargs)


def interactive_hitl_prompt(action_key: str, details: str) -> str:
    """交互式拦截选项面板"""
    options = [
        "允许本次",
        "允许本会话",
        "拒绝并反馈原因",
    ]
    choice = prompt_hitl_tui(
        "⚠️ 敏感操作已拦截",
        options,
        summary=(
            f"🤖 Agent: {current_agent_role.get()}\n"
            f"🛠️ Action: {action_key}"
        ),
        details=details,
    )
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

        provider = APPROVALS.get_provider()
        if provider is not None:
            decision = provider.approve(
                ApprovalRequest(
                    action_type=action_type,
                    action_name=action_name,
                    details=details,
                    agent_role=current_agent_role.get(),
                )
            )
            if decision.allow and decision.scope == "session":
                SESSION_WHITELIST.add(action_key)
            return decision.allow, decision.reason

        choice = interactive_hitl_prompt(action_key, details)

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

        provider = APPROVALS.get_provider()
        if provider is not None:
            decision = provider.approve(
                ApprovalRequest(
                    action_type="path",
                    action_name=tool_name,
                    details=f"Path: {path_str}",
                    agent_role=agent_name,
                    path=path_str,
                )
            )
            if decision.allow:
                if decision.scope in {"session", "path"}:
                    PATH_WHITELIST.add(whitelist_dir)
                return True, ""
            return False, decision.reason or "用户拒绝了工作区外路径访问。"

        options = [
            "允许本次访问",
            "允许本会话访问该目录",
            "拒绝",
        ]
        choice = prompt_hitl_tui(
            "⚠️ 工作区外路径访问拦截",
            options,
            summary=(
                f"🤖 Agent: {agent_name}\n"
                f"🛠️ Tool: {tool_name}"
            ),
            details=(
                f"📁 Path:\n{path_str}\n\n"
                f"📂 会话白名单目录:\n{whitelist_dir}（含子目录）"
            ),
        )

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
        whitelist_dirs = sorted({
            path.as_posix() if path.is_dir() else path.parent.as_posix()
            for path in pending
        })

        provider = APPROVALS.get_provider()
        if provider is not None:
            decision = provider.approve(
                ApprovalRequest(
                    action_type="path",
                    action_name=tool_name,
                    details=f"Paths:\n{path_lines}",
                    agent_role=current_agent_role.get(),
                    paths=tuple(path.as_posix() for path in pending),
                )
            )
            if decision.allow:
                if decision.scope in {"session", "path"}:
                    for directory in whitelist_dirs:
                        PATH_WHITELIST.add(directory)
                return True, ""
            return False, decision.reason or "用户拒绝了工作区外路径访问。"

        options = [
            "允许本次批量访问",
            "允许这些目录本会话访问",
            "拒绝",
        ]
        choice = prompt_hitl_tui(
            "⚠️ 工作区外路径访问拦截",
            options,
            summary=(
                f"🤖 Agent: {current_agent_role.get()}\n"
                f"🛠️ Tool: {tool_name}"
            ),
            details=(
                f"📁 Paths:\n{path_lines}\n\n"
                "📂 会话白名单目录:\n"
                + "\n".join(f"- {directory}" for directory in whitelist_dirs)
            ),
        )

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
