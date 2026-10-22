from unittest.mock import patch

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from system.tui_modals import HitlModal
from utils import hitl


@pytest.fixture
def anyio_backend():
    return "asyncio"


class HitlHost(App):
    def __init__(self, modal: HitlModal) -> None:
        super().__init__()
        self.modal = modal
        self.result = None

    def on_mount(self) -> None:
        self.push_screen(self.modal, self._capture)

    def _capture(self, result) -> None:
        self.result = result
        self.exit()


async def _wait_for_hitl_modal(pilot, app) -> None:
    for _ in range(60):
        if isinstance(app.screen, HitlModal):
            return
        await pilot.pause()
    raise AssertionError("HITL modal was not mounted")


@pytest.mark.anyio
async def test_hitl_modal_puts_long_details_in_scrollable_area():
    long_command = "python -c " + "A" * 1200
    modal = HitlModal(
        "⚠️ 敏感操作已拦截",
        ["允许本次", "允许本会话", "拒绝并反馈原因"],
        summary="🤖 Agent: #0 - Orchestrator\n🛠️ Action: cmd:python",
        details=long_command,
    )
    app = HitlHost(modal)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_hitl_modal(pilot, app)
        details = app.screen.query_one("#hitl-details", Static)
        title = app.screen.query_one("#hitl-title", Label)
        scroll = app.screen.query_one("#hitl-body", VerticalScroll)

        for _ in range(20):
            if scroll.max_scroll_y > 0:
                break
            await pilot.pause()

        assert long_command in str(details.content)
        assert long_command not in str(title.content)
        assert scroll.max_scroll_y > 0


@pytest.mark.anyio
async def test_hitl_modal_renders_control_characters_visibly():
    modal = HitlModal(
        "⚠️ 敏感操作已拦截",
        ["允许本次", "允许本会话", "拒绝并反馈原因"],
        details="echo \x1b[97;;70:105:108:101:69:100:105:116u",
    )
    app = HitlHost(modal)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_hitl_modal(pilot, app)
        content = str(app.screen.query_one("#hitl-details", Static).content)

    assert "\\x1b[97;;70:105:108:101:69:100:105:116u" in content
    assert "\x1b" not in content


@pytest.mark.anyio
async def test_hitl_modal_keeps_content_visible_with_centered_layout():
    modal = HitlModal(
        "⚠️ 敏感操作已拦截",
        ["允许本次", "允许本会话", "拒绝并反馈原因"],
        details="git status",
    )
    app = HitlHost(modal)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_hitl_modal(pilot, app)
        dialog = app.screen.query_one("#hitl-dialog")
        assert app.screen.styles.background.a == pytest.approx(0.2)
        assert str(app.screen.styles.align_horizontal) == "center"
        assert str(app.screen.styles.align_vertical) == "middle"
        assert dialog.region.width < app.size.width
        assert dialog.region.height < app.size.height


@pytest.mark.anyio
async def test_hitl_modal_number_shortcut_returns_selected_option():
    modal = HitlModal(
        "⚠️ 敏感操作已拦截",
        ["允许本次", "允许本会话", "拒绝并反馈原因"],
        details="git status",
    )
    app = HitlHost(modal)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_hitl_modal(pilot, app)
        await pilot.press("2")
        await pilot.pause()

    assert app.result == "允许本会话"


@pytest.mark.anyio
async def test_hitl_modal_escape_returns_cancelled():
    modal = HitlModal(
        "⚠️ 敏感操作已拦截",
        ["允许本次", "允许本会话", "拒绝并反馈原因"],
        details="git status",
    )
    app = HitlHost(modal)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for_hitl_modal(pilot, app)
        await pilot.press("escape")
        await pilot.pause()

    assert app.result == "<cancelled>"


def test_check_permission_passes_command_as_details_not_title(monkeypatch):
    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    monkeypatch.setattr(hitl, "SESSION_WHITELIST", set())
    captured = {}

    def fake_prompt(title, options, *, summary="", details=""):
        captured.update(
            title=title,
            options=options,
            summary=summary,
            details=details,
        )
        return options[0]

    with patch.object(hitl, "prompt_hitl_tui", side_effect=fake_prompt):
        allowed, reason = hitl.check_permission(
            "cmd",
            "git status",
            "git status --short",
        )

    assert allowed is True
    assert reason == ""
    assert captured["title"] == "⚠️ 敏感操作已拦截"
    assert "Action: cmd:git status" in captured["summary"]
    assert captured["details"] == "git status --short"
    assert "git status --short" not in captured["summary"]


def test_check_path_permission_moves_path_and_whitelist_to_details(monkeypatch, tmp_path):
    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    monkeypatch.setattr(hitl, "PATH_WHITELIST", set())
    target = (tmp_path / "external" / "notes.txt").resolve()
    captured = {}

    def fake_prompt(title, options, *, summary="", details=""):
        captured.update(
            title=title,
            options=options,
            summary=summary,
            details=details,
        )
        return options[1]

    monkeypatch.setattr(hitl, "prompt_hitl_tui", fake_prompt)
    allowed, reason = hitl.check_path_permission(target, "FileRead")

    assert allowed is True
    assert reason == ""
    assert captured["title"] == "⚠️ 工作区外路径访问拦截"
    assert "Tool: FileRead" in captured["summary"]
    assert target.as_posix() in captured["details"]
    assert target.parent.as_posix() in captured["details"]
    assert target.parent.as_posix() in hitl.PATH_WHITELIST


def test_check_paths_permission_lists_all_pending_paths_in_details(monkeypatch, tmp_path):
    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    monkeypatch.setattr(hitl, "PATH_WHITELIST", set())
    first = (tmp_path / "one" / "a.txt").resolve()
    second = (tmp_path / "two" / "b.txt").resolve()
    captured = {}

    def fake_prompt(title, options, *, summary="", details=""):
        captured.update(
            title=title,
            options=options,
            summary=summary,
            details=details,
        )
        return options[0]

    monkeypatch.setattr(hitl, "prompt_hitl_tui", fake_prompt)
    allowed, reason = hitl.check_paths_permission([first, second], "FilePatch")

    assert allowed is True
    assert reason == ""
    assert first.as_posix() in captured["details"]
    assert second.as_posix() in captured["details"]
    assert first.parent.as_posix() in captured["details"]
    assert second.parent.as_posix() in captured["details"]
    assert hitl.PATH_WHITELIST == set()
