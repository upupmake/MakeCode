"""Behavioral baseline pinned before the headless runtime refactor.

These tests describe behavior that must survive the OutputSink / ApprovalProvider
refactor. They intentionally avoid asserting anything about headless flags that
will be added later.
"""

import pytest

from system.cli import run_external_cli


def test_unknown_external_cli_option_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        run_external_cli(["--definitely-not-a-real-option"])

    assert exc_info.value.code == 2


def test_post_tui_routes_to_active_simple_backend(monkeypatch):
    import system.tui_app as tui_app
    from system.output_bus import OUTPUT_BUS
    from system.output_sinks import SimpleSink
    from system.tui_types import TuiRegion

    class FakeBackend:
        def __init__(self):
            self.events = []

        def handle(self, event):
            self.events.append(event)

    backend = FakeBackend()
    monkeypatch.setattr(tui_app, "_SIMPLE_BACKEND", backend)
    original_sink = OUTPUT_BUS.get_sink()
    OUTPUT_BUS.set_sink(SimpleSink(backend))
    try:
        tui_app.post_tui(TuiRegion.CONTENT, "hello")
    finally:
        OUTPUT_BUS.set_sink(original_sink)

    assert len(backend.events) == 1
    assert backend.events[0].region == TuiRegion.CONTENT
    assert backend.events[0].payload == "hello"


def test_check_permission_prompts_then_remembers_session_choice(monkeypatch):
    import utils.hitl as hitl

    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    hitl.SESSION_WHITELIST.clear()
    prompts = []

    def fake_prompt(action_key, details):
        prompts.append((action_key, details))
        return "2"

    monkeypatch.setattr(hitl, "interactive_hitl_prompt", fake_prompt)

    assert hitl.check_permission("cmd", "git status", "git status") == (True, "")
    assert hitl.check_permission("cmd", "git status", "git status") == (True, "")
    assert len(prompts) == 1
    assert prompts[0][0] == "cmd:git status"

    hitl.SESSION_WHITELIST.clear()


def test_check_permission_denial_returns_reason(monkeypatch):
    import utils.hitl as hitl

    monkeypatch.setattr(hitl, "HITL_ENABLED", True)
    hitl.SESSION_WHITELIST.clear()
    monkeypatch.setattr(hitl, "interactive_hitl_prompt", lambda action_key, details: "3")
    monkeypatch.setattr(hitl, "choose_tui", lambda *args, **kwargs: "用户拒绝")

    allowed, reason = hitl.check_permission("cmd", "rm -rf build", "rm -rf build")

    assert allowed is False
    assert reason == "用户拒绝"
