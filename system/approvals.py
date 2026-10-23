"""Approval providers used by HITL and headless modes."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from utils.tool_aliases import resolve_tool_name

ApprovalScope = Literal["once", "session", "path"]

TOOL_SELECTOR_MEMBERS: dict[str, frozenset[str]] = {
    "File": frozenset(
        {"FileRead", "FileCreate", "FileEdit", "FilePatch", "ContentSearch", "FileSearch"}
    ),
    "Terminal": frozenset({"RunTerminalCommand"}),
    "TaskManager": frozenset(
        {
            "CreateTasks",
            "UpdateTasksContent",
            "UpdateTasksStatus",
            "UpdateTasksDependencies",
            "DeleteAllTasks",
            "GetRunnableTasks",
            "GetTaskTable",
        }
    ),
    "Skills": frozenset({"LoadSkill"}),
    "Team": frozenset({"DelegateTasks"}),
}


def _matches_selector(selector: str, action_name: str) -> bool:
    selector = resolve_tool_name(selector)
    action_name = resolve_tool_name(action_name)
    return action_name == selector or action_name in TOOL_SELECTOR_MEMBERS.get(
        selector, ()
    )


@dataclass(frozen=True)
class ApprovalRequest:
    action_type: str
    action_name: str
    details: str = ""
    agent_role: str = ""
    path: str | None = None
    paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalDecision:
    allow: bool
    scope: ApprovalScope = "once"
    reason: str = ""


class ApprovalProvider(Protocol):
    def approve(self, request: ApprovalRequest) -> ApprovalDecision: ...


class DenyApprovalProvider:
    """Default provider for headless runs: never block on an interactive UI."""

    def __init__(self, reason: str = "headless 模式默认拒绝需要人工确认的操作。") -> None:
        self._reason = reason

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(allow=False, reason=self._reason)


class TuiApprovalProvider:
    """Adapter around the existing interactive HITL prompt."""

    def __init__(
        self,
        prompt: Callable[[str, str], str],
        choose: Callable[..., str],
    ) -> None:
        self._prompt = prompt
        self._choose = choose

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        choice = self._prompt(
            f"{request.action_type}:{request.action_name}",
            request.details,
        )
        if choice == "1":
            return ApprovalDecision(allow=True)
        if choice == "2":
            return ApprovalDecision(allow=True, scope="session")
        if choice == "3":
            reason = self._choose(
                "请输入拒绝原因（反馈给 Agent）",
                [],
                allow_custom=True,
            )
            if reason == "<cancelled>":
                reason = "用户通过取消操作中断了操作。"
            return ApprovalDecision(
                allow=False,
                reason=reason or "用户拒绝执行，未提供具体原因。",
            )
        return ApprovalDecision(allow=False, reason="用户取消了操作。")


class PolicyApprovalProvider:
    """Non-interactive policy used by headless permission modes."""

    WRITE_TOOLS = frozenset({"FileCreate", "FileEdit", "FilePatch"})
    MODES = frozenset({"default", "acceptEdits", "plan", "bypassPermissions"})

    def __init__(
        self,
        mode: str = "default",
        allowed_tools: Iterable[str] | None = None,
        disallowed_tools: Iterable[str] | None = None,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"Unsupported permission mode: {mode}")
        self.mode = mode
        self.allowed_tools = frozenset(allowed_tools or ())
        self.disallowed_tools = frozenset(disallowed_tools or ())

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if any(
            _matches_selector(selector, request.action_name)
            for selector in self.disallowed_tools
        ):
            return ApprovalDecision(
                allow=False,
                reason=f"工具 {request.action_name} 已被 --disallowedTools 禁止。",
            )
        if any(
            _matches_selector(selector, request.action_name)
            for selector in self.allowed_tools
        ):
            return ApprovalDecision(allow=True)

        if self.mode == "bypassPermissions":
            return ApprovalDecision(allow=True)

        if self.mode == "plan":
            if request.action_type == "cmd" or (
                request.action_type == "tool"
                and request.action_name in self.WRITE_TOOLS
            ):
                return ApprovalDecision(
                    allow=False,
                    reason="Plan Mode 只允许只读操作。",
                )
            return ApprovalDecision(allow=True)

        if self.mode == "acceptEdits":
            if (
                request.action_type == "tool"
                and request.action_name in self.WRITE_TOOLS
            ):
                return ApprovalDecision(allow=True)
            return ApprovalDecision(
                allow=False,
                reason="acceptEdits 只自动放行工作区内的文件编辑。",
            )

        return ApprovalDecision(
            allow=False,
            reason="非交互模式默认拒绝需要人工确认的操作。",
        )


class ApprovalRegistry:
    """Thread-safe holder for the active approval provider."""

    def __init__(self) -> None:
        self._provider: ApprovalProvider | None = None
        self._lock = threading.RLock()

    def set_provider(self, provider: ApprovalProvider | None) -> None:
        with self._lock:
            self._provider = provider

    def get_provider(self) -> ApprovalProvider | None:
        with self._lock:
            return self._provider


APPROVALS = ApprovalRegistry()
