import subprocess
import sys
from pathlib import Path

import pytest

from system.approvals import (
    APPROVALS,
    ApprovalDecision,
    ApprovalRequest,
    DenyApprovalProvider,
    PolicyApprovalProvider,
    TuiApprovalProvider,
)


@pytest.fixture(autouse=True)
def reset_approval_provider():
    APPROVALS.set_provider(None)
    yield
    APPROVALS.set_provider(None)


def test_deny_provider_rejects_with_reason():
    decision = DenyApprovalProvider().approve(
        ApprovalRequest(action_type="cmd", action_name="git")
    )

    assert decision.allow is False
    assert decision.reason


def test_policy_provider_modes():
    request_cmd = ApprovalRequest(action_type="cmd", action_name="git status")
    request_edit = ApprovalRequest(action_type="tool", action_name="FileEdit")
    request_read = ApprovalRequest(action_type="tool", action_name="FileRead")

    assert PolicyApprovalProvider("default").approve(request_cmd).allow is False
    assert PolicyApprovalProvider("default").approve(request_edit).allow is False

    accept_edits = PolicyApprovalProvider("acceptEdits")
    assert accept_edits.approve(request_edit).allow is True
    assert accept_edits.approve(request_cmd).allow is False

    plan = PolicyApprovalProvider("plan")
    assert plan.approve(request_read).allow is True
    assert plan.approve(request_edit).allow is False
    assert plan.approve(request_cmd).allow is False

    bypass = PolicyApprovalProvider("bypassPermissions")
    assert bypass.approve(request_cmd).allow is True
    assert bypass.approve(request_edit).allow is True


def test_policy_provider_tool_allow_and_deny_lists():
    request = ApprovalRequest(action_type="tool", action_name="FileEdit")

    allowed = PolicyApprovalProvider("default", allowed_tools=["FileEdit"])
    assert allowed.approve(request).allow is True

    denied = PolicyApprovalProvider(
        "bypassPermissions",
        disallowed_tools=["FileEdit"],
    )
    assert denied.approve(request).allow is False


def test_tui_provider_maps_choices():
    def provider_for(choice):
        return TuiApprovalProvider(
            prompt=lambda action_key, details: choice,
            choose=lambda *args, **kwargs: "用户拒绝",
        )

    assert provider_for("1").approve(ApprovalRequest("cmd", "git")) == ApprovalDecision(True)
    assert provider_for("2").approve(ApprovalRequest("cmd", "git")) == ApprovalDecision(
        True, scope="session"
    )
    denied = provider_for("3").approve(ApprovalRequest("cmd", "git"))
    assert denied.allow is False
    assert denied.reason == "用户拒绝"


def test_hitl_uses_installed_provider_without_touching_interactive_prompt(monkeypatch):
    import utils.hitl as hitl

    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    hitl.SESSION_WHITELIST.clear()
    monkeypatch.setattr(
        hitl,
        "interactive_hitl_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("interactive prompt called")),
    )
    APPROVALS.set_provider(DenyApprovalProvider("provider denied"))

    allowed, reason = hitl.check_permission("cmd", "git status", "git status")

    assert allowed is False
    assert reason == "provider denied"


def test_hitl_session_scope_adds_whitelist(monkeypatch):
    import utils.hitl as hitl

    class SessionProvider:
        def __init__(self):
            self.calls = 0

        def approve(self, request):
            self.calls += 1
            return ApprovalDecision(allow=True, scope="session")

    provider = SessionProvider()
    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    hitl.SESSION_WHITELIST.clear()
    APPROVALS.set_provider(provider)

    assert hitl.check_permission("cmd", "git status", "") == (True, "")
    assert hitl.check_permission("cmd", "git status", "") == (True, "")
    assert provider.calls == 1


def test_hitl_path_permission_uses_provider_without_tui(monkeypatch, tmp_path):
    import utils.hitl as hitl

    target = (tmp_path / "outside" / "notes.txt").resolve()
    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    hitl.PATH_WHITELIST.clear()
    APPROVALS.set_provider(DenyApprovalProvider("path denied"))

    allowed, reason = hitl.check_path_permission(target, "FileRead")

    assert allowed is False
    assert reason == "path denied"


def test_importing_hitl_does_not_import_textual_tui():
    script = """
import sys
import utils.hitl

forbidden = {"system.tui_app", "textual"}.intersection(sys.modules)
assert not forbidden, sorted(forbidden)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
