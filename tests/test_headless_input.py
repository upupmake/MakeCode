import asyncio
import json
import sys
from io import StringIO

from system.agent_runtime import TurnResult
from system.headless_input import StreamJsonInputLoop, TextInputLoop
from system.output_sinks import StreamJsonWriter


class FakeRuntime:
    def __init__(self):
        self.queries = []

    async def run_turn(self, query):
        self.queries.append(query)
        return TurnResult(text=query, session_id="conv_test", messages=[])


def _run_loop(monkeypatch, lines, *, replay=False):
    monkeypatch.setattr(sys, "stdin", StringIO("\n".join(lines) + "\n"))
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    runtime = FakeRuntime()

    exit_code = asyncio.run(
        StreamJsonInputLoop(
            runtime,
            writer,
            replay_user_messages=replay,
        ).run()
    )

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    return exit_code, runtime, payloads


def test_stream_input_processes_multiple_user_messages(monkeypatch):
    exit_code, runtime, payloads = _run_loop(
        monkeypatch,
        [
            json.dumps({"type": "user", "message": {"role": "user", "content": "first"}}),
            json.dumps({"type": "user", "content": "second"}),
        ],
    )

    assert exit_code == 0
    assert runtime.queries == ["first", "second"]
    assert [payload["type"] for payload in payloads] == ["result", "result"]
    assert [payload["result"] for payload in payloads] == ["first", "second"]


def test_stream_input_replays_user_messages(monkeypatch):
    _, _, payloads = _run_loop(
        monkeypatch,
        [json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})],
        replay=True,
    )

    assert [payload["type"] for payload in payloads] == ["user", "result"]
    assert payloads[0]["message"]["content"] == "hello"


def test_stream_input_reports_invalid_json_and_continues(monkeypatch):
    _, runtime, payloads = _run_loop(
        monkeypatch,
        [
            "not-json",
            json.dumps({"type": "user", "content": "ok"}),
        ],
    )

    assert runtime.queries == ["ok"]
    assert payloads[0]["type"] == "error"
    assert payloads[1]["type"] == "result"


def test_stream_input_returns_nonzero_after_error(monkeypatch):
    class ErrorRuntime(FakeRuntime):
        async def run_turn(self, query):
            return TurnResult(
                text="",
                session_id="conv_test",
                messages=[],
                is_error=True,
                error="boom",
            )

    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(json.dumps({"type": "user", "content": "hi"}) + "\n"),
    )
    stream = StringIO()
    exit_code = asyncio.run(
        StreamJsonInputLoop(
            ErrorRuntime(),
            StreamJsonWriter(stream, session_id="conv_test"),
        ).run()
    )

    assert exit_code == 1
    assert json.loads(stream.getvalue())["subtype"] == "error"


def test_parse_line_accepts_interrupt_control():
    assert StreamJsonInputLoop._parse_line(
        json.dumps({"type": "control", "action": "interrupt"})
    ) == ("control", "interrupt")


def test_text_input_loop_runs_one_turn_per_line(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("first\n\nsecond\n"))
    runtime = FakeRuntime()
    results = []

    exit_code = asyncio.run(
        TextInputLoop(runtime, lambda result: results.append(result.text) or 0).run()
    )

    assert exit_code == 0
    assert runtime.queries == ["first", "second"]
    assert results == ["first", "second"]


def test_text_input_loop_keeps_error_exit_code(monkeypatch):
    class ErrorRuntime(FakeRuntime):
        async def run_turn(self, query):
            return TurnResult(
                text="",
                session_id="conv_test",
                messages=[],
                is_error=True,
                error="boom",
            )

    monkeypatch.setattr(sys, "stdin", StringIO("hi\n"))
    exit_code = asyncio.run(TextInputLoop(ErrorRuntime(), lambda result: 1).run())

    assert exit_code == 1


def test_stream_input_without_writer_uses_result_callback(monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            "not-json\n"
            + json.dumps({"type": "user", "content": "ok"})
            + "\n"
        ),
    )
    runtime = FakeRuntime()
    results = []

    exit_code = asyncio.run(
        StreamJsonInputLoop(
            runtime,
            None,
            write_result=lambda result: results.append(result.text) or 0,
        ).run()
    )

    assert exit_code == 0
    assert runtime.queries == ["ok"]
    assert results == ["ok"]
