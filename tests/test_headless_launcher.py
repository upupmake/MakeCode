from types import SimpleNamespace

import main
from system import cli as cli_module


def test_headless_mode_never_relaunches_macos_terminal(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli_module, "HEADLESS_OPTIONS", object())
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("headless mode must not relaunch Terminal")
        ),
    )

    assert main._launch_macos_terminal_if_needed() is False
