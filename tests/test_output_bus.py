import io

import pytest

from system.output_bus import OUTPUT_BUS
from system.output_sinks import SimpleSink, TextSink, TuiSink
from system.tui_types import TuiEvent, TuiRegion


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def restore_active_sink():
    original = OUTPUT_BUS.get_sink()
    yield
    OUTPUT_BUS.set_sink(original)


def test_set_emit_and_clear_sink():
    sink = RecordingSink()
    OUTPUT_BUS.set_sink(sink)

    assert OUTPUT_BUS.get_sink() is sink
    assert OUTPUT_BUS.emit(TuiEvent(TuiRegion.CONTENT, "hello")) is True
    assert len(sink.events) == 1

    OUTPUT_BUS.clear()
    assert OUTPUT_BUS.get_sink() is None
    assert OUTPUT_BUS.emit(TuiEvent(TuiRegion.CONTENT, "hello")) is False


def test_post_tui_uses_installed_sink():
    import system.tui_app as tui_app

    sink = RecordingSink()
    OUTPUT_BUS.set_sink(sink)

    tui_app.post_tui(TuiRegion.CONTENT, "hello", tail=True)

    assert len(sink.events) == 1
    assert sink.events[0].region == TuiRegion.CONTENT
    assert sink.events[0].payload == "hello"
    assert sink.events[0].tail is True


def test_simple_sink_delegates_to_renderer():
    class Renderer:
        def __init__(self):
            self.events = []

        def handle(self, event):
            self.events.append(event)

    renderer = Renderer()
    sink = SimpleSink(renderer)
    event = TuiEvent(TuiRegion.CONTENT, "hello")

    sink.emit(event)

    assert renderer.events == [event]


def test_tui_sink_forwards_all_event_fields():
    calls = []

    def post(region, payload, **kwargs):
        calls.append((region, payload, kwargs))

    sink = TuiSink(post)
    event = TuiEvent(
        TuiRegion.REASONING,
        "thinking",
        clear=True,
        tail=True,
        active=True,
        collapsible_title="title",
        collapsible_open=True,
        collapsible_close=True,
        collapsible_kind="reasoning",
        tail_after="after",
    )

    sink.emit(event)

    region, payload, kwargs = calls[0]
    assert region == TuiRegion.REASONING
    assert payload == "thinking"
    assert kwargs == {
        "clear": True,
        "tail": True,
        "active": True,
        "collapsible_title": "title",
        "collapsible_open": True,
        "collapsible_close": True,
        "collapsible_kind": "reasoning",
        "tail_after": "after",
    }


def test_text_sink_writes_plain_payload():
    stream = io.StringIO()
    sink = TextSink(stream)

    sink.emit(TuiEvent(TuiRegion.CONTENT, "hello"))
    sink.emit(TuiEvent(TuiRegion.CONTENT, "world\n"))

    assert stream.getvalue() == "hello\nworld\n"
