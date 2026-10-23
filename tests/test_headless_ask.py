import asyncio
import json
import sys
import threading
import time
from io import StringIO

import pytest

import system.headless_ask as ask_module
from system.agent_runtime import TurnResult
from system.ask_user_backend import ASK_USER
from system.headless_ask import AskUserChannel
from system.headless_input import StreamJsonInputLoop, TextInputLoop
from system.output_sinks import StreamJsonWriter
from tools import ask_user as ask_user_tool

OPTIONS = [
    {"content": "First", "is_recommended": False},
    {"content": "Second", "is_recommended": True},
]


def _wait_pending(channel: AskUserChannel, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with channel._lock:
            if channel._pending is not None:
                return
        time.sleep(0.01)
    raise AssertionError("AskUser did not become pending")


def _run_ask(channel: AskUserChannel, result: dict, question: str = "Pick one") -> threading.Thread:
    def run():
        result["answer"] = channel.ask(question, OPTIONS)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_text_channel_renders_menu_and_selects_by_number(capsys):
    channel = AskUserChannel(answer_format="text")
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)

    assert channel.handle_line("2\n") is True
    thread.join(2)

    assert not thread.is_alive()
    assert result["answer"] == {"choice": "Second"}
    err = capsys.readouterr().err
    assert "[AskUser] Pick one" in err
    assert "⭐ Second（推荐）" in err


def test_text_channel_supports_custom_and_empty_answers():
    channel = AskUserChannel(answer_format="text")
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)
    assert channel.handle_line("something else\n") is True
    thread.join(2)
    assert result["answer"] == {"choice": "something else", "custom": True}

    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)
    assert channel.handle_line("\n") is True
    thread.join(2)
    assert result["answer"] == {"choice": "<empty_input>"}


def test_stream_channel_emits_control_request_and_accepts_response():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    channel = AskUserChannel(answer_format="stream-json", writer=writer)
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)

    request = json.loads(stream.getvalue().splitlines()[-1])
    assert request["type"] == "control_request"
    assert request["subtype"] == "ask_user"
    assert request["question"] == "Pick one"
    assert request["options"] == [
        {"content": "First", "is_recommended": False},
        {"content": "Second", "is_recommended": True},
    ]
    assert request["session_id"] == "conv_test"

    response = json.dumps(
        {
            "type": "control_response",
            "request_id": request["request_id"],
            "response": {"choice": "First"},
        }
    )
    assert channel.handle_line(response) is True
    thread.join(2)
    assert result["answer"] == {"choice": "First"}


def test_stream_channel_ignores_other_requests_and_supports_interrupt():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    channel = AskUserChannel(answer_format="stream-json", writer=writer)
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)
    request_id = json.loads(stream.getvalue().splitlines()[-1])["request_id"]

    other = json.dumps(
        {
            "type": "control_response",
            "request_id": "ask_999",
            "response": {"choice": "First"},
        }
    )
    assert channel.handle_line(other) is False
    assert channel.handle_line('{"type":"control","action":"interrupt"}') is True
    thread.join(2)
    assert result["answer"] == {"choice": "<cancelled>"}
    assert request_id


def test_stream_channel_accepts_text_answer_when_no_writer(capsys):
    channel = AskUserChannel(answer_format="stream-json")
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)

    assert channel.handle_line('{"type":"user","content":"next"}\n') is False
    assert channel.handle_line("2\n") is True
    thread.join(2)

    assert result["answer"] == {"choice": "Second"}
    assert "[AskUser] Pick one" in capsys.readouterr().err


def test_channel_close_wakes_pending_with_cancelled():
    channel = AskUserChannel(answer_format="text")
    result = {}
    thread = _run_ask(channel, result)
    _wait_pending(channel)

    channel.close()
    thread.join(2)

    assert result["answer"] == {"choice": "<cancelled>"}


def test_closed_channel_returns_cancelled_without_rendering(capsys):
    channel = AskUserChannel(answer_format="text")
    channel.close()

    assert channel.ask("Pick one", OPTIONS) == {"choice": "<cancelled>"}
    assert capsys.readouterr().err == ""


def test_direct_channel_reads_answer_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("2\n"))
    channel = AskUserChannel(answer_format="text", direct_stdin=True)

    assert channel.ask("Pick one", OPTIONS) == {"choice": "Second"}


def test_direct_channel_eof_returns_cancelled(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO(""))
    channel = AskUserChannel(answer_format="text", direct_stdin=True)

    assert channel.ask("Pick one", OPTIONS) == {"choice": "<cancelled>"}


def test_direct_channel_requires_text_format():
    with pytest.raises(ValueError):
        AskUserChannel(answer_format="stream-json", direct_stdin=True)


def test_channel_cancels_when_response_is_cancelled(monkeypatch):
    monkeypatch.setattr(ask_module, "is_cancelled", lambda: True)
    channel = AskUserChannel(answer_format="text")
    result = {}
    thread = _run_ask(channel, result)
    thread.join(2)

    assert not thread.is_alive()
    assert result["answer"] == {"choice": "<cancelled>"}


def test_ask_user_tool_uses_injected_backend():
    class Backend:
        def __init__(self):
            self.calls = []

        def ask(self, question, options):
            self.calls.append((question, options))
            return {"choice": "A"}

    backend = Backend()
    ASK_USER.set_backend(backend)
    try:
        output = ask_user_tool.ask_user("Pick", [{"content": "A"}])
    finally:
        ASK_USER.set_backend(None)

    assert json.loads(output) == {"choice": "A"}
    assert backend.calls == [("Pick", [{"content": "A", "is_recommended": False}])]


def test_ask_user_tool_falls_back_to_tui(monkeypatch):
    monkeypatch.setattr(ask_user_tool, "request_window_attention", lambda: None)
    monkeypatch.setattr(
        ask_user_tool,
        "choose_tui",
        lambda question, labels, allow_custom: labels[1],
    )

    output = ask_user_tool.ask_user(
        "Pick",
        [{"content": "Alpha"}, {"content": "Beta", "is_recommended": True}],
    )

    assert json.loads(output) == {"choice": "Beta"}


class _QueueStdin:
    def __init__(self):
        import queue

        self._queue = queue.Queue()

    def readline(self):
        line = self._queue.get()
        return "" if line is None else line

    def push(self, line):
        self._queue.put(line)


def test_stream_loop_routes_control_response_to_pending_ask(monkeypatch):
    stdin = _QueueStdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    channel = AskUserChannel(answer_format="stream-json", writer=writer)
    answers = {}

    class Runtime:
        async def run_turn(self, prompt):
            answers["answer"] = await asyncio.to_thread(
                channel.ask,
                "Pick one",
                OPTIONS,
            )
            return TurnResult(text="done", session_id="conv_test", messages=[])

    async def main():
        loop = StreamJsonInputLoop(Runtime(), writer, ask_channel=channel)
        task = asyncio.create_task(loop.run())
        stdin.push('{"type":"user","message":{"role":"user","content":"hi"}}\n')
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "control_request" in stream.getvalue():
                break
        request = json.loads(stream.getvalue().splitlines()[-1])
        stdin.push(
            json.dumps(
                {
                    "type": "control_response",
                    "request_id": request["request_id"],
                    "response": {"choice": "Second"},
                }
            )
            + "\n"
        )
        stdin.push(None)
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(main()) == 0
    assert answers["answer"] == {"choice": "Second"}


def test_text_loop_routes_answer_line_to_pending_ask(monkeypatch):
    stdin = _QueueStdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    channel = AskUserChannel(answer_format="text")
    answers = {}
    results = []

    class Runtime:
        async def run_turn(self, prompt):
            answers["answer"] = await asyncio.to_thread(
                channel.ask,
                "Pick one",
                OPTIONS,
            )
            return TurnResult(text="done", session_id="conv_test", messages=[])

    async def main():
        loop = TextInputLoop(
            Runtime(),
            lambda result: results.append(result) or 0,
            ask_channel=channel,
        )
        task = asyncio.create_task(loop.run())
        stdin.push("hi\n")
        for _ in range(200):
            await asyncio.sleep(0.01)
            with channel._lock:
                if channel._pending is not None:
                    break
        stdin.push("2\n")
        stdin.push(None)
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(main()) == 0
    assert answers["answer"] == {"choice": "Second"}
    assert results[0].text == "done"


def test_channel_rejects_unknown_answer_format():
    with pytest.raises(ValueError):
        AskUserChannel(answer_format="xml")
