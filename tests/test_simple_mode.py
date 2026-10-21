import asyncio
from io import StringIO
from types import SimpleNamespace

from rich.console import Console
from rich.text import Text

import system.tui_app as tui_app
import system.stream_render as stream_render
from system.stream_render import StreamRenderer
from system.simple_render import SimpleRenderer
from system.tui_types import TuiEvent, TuiRegion


def _make_renderer() -> tuple[SimpleRenderer, StringIO]:
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        width=100,
    )
    return SimpleRenderer(console=console), output


def test_simple_flag_sets_global_mode_and_keeps_load_request():
    from system import cli as cli_module

    assert cli_module.run_external_cli(["--simple"]) is None
    assert cli_module.SIMPLE_MODE is True

    assert (
        cli_module.run_external_cli(
            ["--simple", "--load", "conv_0123456789abcdef0123456789abcdef"]
        )
        is None
    )
    assert cli_module.SIMPLE_MODE is True
    assert cli_module.STARTUP_LOAD_REQUESTED is True
    assert cli_module.STARTUP_LOAD_ID == "conv_0123456789abcdef0123456789abcdef"

    assert cli_module.run_external_cli([]) is None
    assert cli_module.SIMPLE_MODE is False


def test_renderer_prints_blocks_in_order_and_skips_stream_tail():
    renderer, output = _make_renderer()

    renderer.handle(TuiEvent(TuiRegion.CONTENT, "first block"))
    renderer.handle(TuiEvent(TuiRegion.CONTENT, Text("unfinished tail"), tail=True))
    renderer.handle(
        TuiEvent(
            TuiRegion.REASONING,
            None,
            collapsible_title="Reasoning",
            collapsible_open=True,
        )
    )
    renderer.handle(TuiEvent(TuiRegion.REASONING, "thinking block"))

    rendered = output.getvalue()
    assert rendered.index("first block") < rendered.index("Reasoning")
    assert rendered.index("Reasoning") < rendered.index("thinking block")
    assert "unfinished tail" not in rendered


def test_renderer_skips_task_pane_refreshes():
    renderer, output = _make_renderer()

    renderer.handle(TuiEvent(TuiRegion.TASK, Text("task pane content"), clear=True))
    renderer.handle(TuiEvent(TuiRegion.TASK, Text("task pane content")))

    assert output.getvalue() == ""


def test_choose_returns_numbered_option(monkeypatch):
    renderer, output = _make_renderer()
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")

    assert renderer.choose("Pick one", ["alpha", "beta"]) == "beta"
    assert "Pick one" in output.getvalue()
    assert "1. alpha" in output.getvalue()
    assert "2. beta" in output.getvalue()


def test_choose_supports_custom_input(monkeypatch):
    renderer, _ = _make_renderer()
    monkeypatch.setattr("builtins.input", lambda prompt="": "custom answer")

    assert renderer.choose("Pick one", ["alpha"], allow_custom=True) == "custom answer"


def test_choose_empty_custom_input(monkeypatch):
    renderer, _ = _make_renderer()
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert renderer.choose("Pick one", ["alpha"], allow_custom=True) == "<empty_input>"


def test_post_tui_routes_to_simple_backend(monkeypatch):
    class FakeBackend:
        def __init__(self) -> None:
            self.events: list[TuiEvent] = []

        def handle(self, event: TuiEvent) -> None:
            self.events.append(event)

    fake = FakeBackend()
    monkeypatch.setattr(tui_app, "_SIMPLE_BACKEND", fake)

    tui_app.post_tui(TuiRegion.CONTENT, "hello", tail_after="tail")

    assert len(fake.events) == 1
    assert fake.events[0].region == TuiRegion.CONTENT
    assert fake.events[0].payload == "hello"
    assert fake.events[0].tail_after == "tail"


def test_choose_tui_routes_to_simple_backend(monkeypatch):
    class FakeBackend:
        def choose(self, title, options, *, allow_custom=False):
            return options[0]

    monkeypatch.setattr(tui_app, "_SIMPLE_BACKEND", FakeBackend())

    assert tui_app.choose_tui("Pick", ["first", "second"]) == "first"


def test_enable_simple_mode_creates_backend(monkeypatch):
    monkeypatch.setattr(tui_app, "_SIMPLE_BACKEND", None)

    tui_app.enable_simple_mode()

    assert isinstance(tui_app._SIMPLE_BACKEND, SimpleRenderer)


def test_stream_renderer_outputs_completed_blocks_through_simple_backend(monkeypatch):
    renderer, output = _make_renderer()
    monkeypatch.setattr(tui_app, "_SIMPLE_BACKEND", renderer)
    monkeypatch.setattr(stream_render, "is_cancelled", lambda: False)

    async def stream():
        yield {"type": "reasoning", "content": "thinking"}
        yield {"type": "text", "content": "Hello"}
        yield {"type": "text", "content": "\n\nWorld"}
        yield {
            "type": "done",
            "result": SimpleNamespace(
                text="Hello\n\nWorld",
                tool_calls=[],
                assistant_message={},
            ),
        }

    asyncio.run(StreamRenderer().render_async(stream(), agent_name="Test"))

    rendered = output.getvalue()
    assert "thinking" in rendered
    assert "Hello" in rendered
    assert "World" in rendered
