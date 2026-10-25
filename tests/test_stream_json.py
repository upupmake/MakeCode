import json
from io import StringIO

from system.agent_runtime import TurnResult
from system.output_sinks import StreamJsonWriter


def _lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_stream_json_writer_emits_init_events_and_result():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test", include_partial=True)
    writer.init(
        model="test-model",
        cwd="/tmp",
        permission_mode="default",
        tools=["FileRead"],
    )
    writer.handle_runtime_event({"type": "text_delta", "text": "hello"})
    writer.handle_runtime_event(
        {
            "type": "assistant_done",
            "message": {"role": "assistant", "content": "hello"},
            "text": "hello",
            "tool_calls": [],
        }
    )
    writer.handle_runtime_event(
        {"type": "tool_use", "id": "t1", "name": "FileRead", "input": {"path": "a"}}
    )
    writer.handle_runtime_event(
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "name": "FileRead",
            "content": "data",
            "is_error": False,
        }
    )
    writer.write_result(
        TurnResult(
            text="hello",
            session_id="conv_test",
            messages=[],
            usage={"context_tokens": 10},
            duration_ms=5,
        )
    )

    payloads = _lines(stream)
    assert [payload["type"] for payload in payloads] == [
        "system",
        "assistant_delta",
        "assistant",
        "tool_use",
        "tool_result",
        "result",
    ]
    assert payloads[0]["subtype"] == "init"
    assert payloads[0]["session_id"] == "conv_test"
    assert payloads[1]["text"] == "hello"
    assert payloads[-1]["subtype"] == "success"
    assert payloads[-1]["result"] == "hello"


def test_stream_json_writer_parses_json_string_tool_input():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")

    writer.handle_runtime_event(
        {
            "type": "tool_use",
            "id": "t1",
            "name": "FileRead",
            "input": '{"path": "a"}',
        }
    )

    payload = _lines(stream)[0]
    assert payload["input"] == {"path": "a"}


def test_stream_json_writer_suppresses_partial_deltas_by_default():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    writer.handle_runtime_event({"type": "text_delta", "text": "hello"})
    writer.handle_runtime_event(
        {
            "type": "assistant_done",
            "message": {"role": "assistant", "content": "hello"},
            "text": "hello",
            "tool_calls": [],
        }
    )

    payloads = _lines(stream)
    assert [payload["type"] for payload in payloads] == ["assistant"]


def test_stream_json_writer_emits_compact_events_and_result_compactions():
    stream = StringIO()
    writer = StreamJsonWriter(stream, session_id="conv_test")
    writer.handle_runtime_event(
        {
            "type": "compact",
            "phase": "start",
            "scope": "tool_output",
            "tokens_before": 100,
        }
    )
    writer.handle_runtime_event(
        {
            "type": "compact",
            "phase": "summary_delta",
            "scope": "partial",
            "text": "sum",
        }
    )
    writer.write_result(
        TurnResult(
            text="ok",
            session_id="conv_test",
            messages=[],
            compactions=[
                {
                    "type": "compact",
                    "phase": "end",
                    "scope": "tool_output",
                    "status": "success",
                }
            ],
        )
    )

    payloads = _lines(stream)
    assert [payload["type"] for payload in payloads] == [
        "compact",
        "compact",
        "result",
    ]
    assert payloads[0]["session_id"] == "conv_test"
    assert payloads[1]["text"] == "sum"
    assert payloads[-1]["compactions"] == [
        {
            "type": "compact",
            "phase": "end",
            "scope": "tool_output",
            "status": "success",
        }
    ]
