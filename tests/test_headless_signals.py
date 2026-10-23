import asyncio
import json
import signal
import sys
from io import StringIO

from system.agent_runtime import TurnResult
from system.cli import HeadlessOptions
from system.headless import HeadlessRunner
from system.headless_input import StreamJsonInputLoop
from system.output_sinks import StreamJsonWriter


class FakeRuntime:
    async def run_turn(self, query):
        return TurnResult(text=query, session_id="conv_test", messages=[])


def test_sigint_handler_sets_exit_code_and_cancels(monkeypatch):
    runner = HeadlessRunner(HeadlessOptions(prompt="hi"))
    calls = []
    monkeypatch.setattr(
        "system.headless.cancel_current_response",
        lambda: calls.append(True),
    )

    runner._install_signal_handlers()
    try:
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
    finally:
        runner._restore_signal_handlers()

    assert runner._signal_exit_code == 130
    assert calls == [True]


def test_run_returns_signal_code_on_keyboard_interrupt(monkeypatch):
    runner = HeadlessRunner(HeadlessOptions(prompt="hi"))
    runner._signal_exit_code = 143

    async def raise_interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(HeadlessRunner, "_run_async", raise_interrupt)

    assert runner.run() == 143


def test_cancelled_result_returns_130():
    runner = HeadlessRunner(HeadlessOptions(prompt="hi"))
    result = TurnResult(
        text="",
        session_id="conv_test",
        messages=[],
        cancelled=True,
    )

    assert runner._write_result(result) == 130


def test_stream_input_stop_event_exits_without_result(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO(""))
    stream = StringIO()
    stop_event = asyncio.Event()
    stop_event.set()

    exit_code = asyncio.run(
        StreamJsonInputLoop(
            FakeRuntime(),
            StreamJsonWriter(stream, session_id="conv_test"),
            stop_event=stop_event,
        ).run()
    )

    assert exit_code == 0
    assert stream.getvalue() == ""


def test_stream_result_with_error_is_still_json(monkeypatch):
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    result = TurnResult(
        text="",
        session_id="conv_test",
        messages=[],
        is_error=True,
        error="boom",
    )

    writer.write_result(result)

    payload = json.loads(stream.getvalue())
    assert payload["type"] == "result"
    assert payload["subtype"] == "error"
    assert payload["is_error"] is True
    assert payload["error"] == "boom"
