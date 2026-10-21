import asyncio
from contextlib import nullcontext
from io import StringIO
from types import SimpleNamespace

from rich.console import Console
from rich.text import Text

import system.tui_app as tui_app
import system.stream_render as stream_render
import system.simple_input as simple_input
from system import simple_management
from system.models import ModelManager
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


def _feed_inputs(monkeypatch, values: list[str]) -> None:
    iterator = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(iterator))


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


def test_background_event_redraws_active_input(monkeypatch):
    output = StringIO()
    monkeypatch.setattr("sys.stdout", output)
    console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        width=100,
    )
    renderer = SimpleRenderer(console=console)
    renderer.register_input_redraw(lambda: output.write("PROMPT"))

    renderer.handle(TuiEvent(TuiRegion.BACKGROUND, "BACKGROUND LOG"))

    rendered = output.getvalue()
    assert "BACKGROUND LOG" in rendered
    assert rendered.rfind("PROMPT") > rendered.find("BACKGROUND LOG")


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
        def choose(self, title, options, *, allow_custom=False, **kwargs):
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


def test_choose_supports_search_and_confirmed_delete(monkeypatch):
    renderer, _ = _make_renderer()
    deleted = []
    _feed_inputs(monkeypatch, ["/beta", "1"])

    assert renderer.choose(
        "Pick",
        ["alpha", "beta"],
        search_texts=["alpha item", "beta item"],
    ) == "beta"

    _feed_inputs(monkeypatch, ["d 1", "y", "1"])
    assert renderer.choose(
        "Pick",
        ["alpha", "beta"],
        search_texts=["alpha item", "beta item"],
        delete_handler=deleted.append,
    ) == "beta"
    assert deleted == ["alpha"]


def test_manage_models_selects_model(tmp_path, monkeypatch):
    renderer, _ = _make_renderer()
    manager = ModelManager(tmp_path)
    manager.add_model(
        "https://api.example.com",
        "secret",
        ["model-a"],
    )
    _feed_inputs(monkeypatch, ["1"])

    result = simple_management.manage_models(renderer, manager)

    assert result.startswith("selected:")
    assert "model-a" in result


def test_manage_skills_toggles_and_applies(monkeypatch):
    renderer, _ = _make_renderer()

    class FakeSkillLoader:
        def __init__(self):
            self.applied = None

        def get_skill_entries(self):
            return [
                {"name": "alpha", "description": "first", "enabled": True},
                {"name": "beta", "description": "second", "enabled": False},
            ]

        def apply_skill_enabled_states(self, changes):
            self.applied = changes

    loader = FakeSkillLoader()
    _feed_inputs(monkeypatch, ["1", "a"])

    result = simple_management.manage_skills(renderer, loader)

    assert result == {"action": "applied", "enabled": 0, "disabled": 1}
    assert loader.applied == {"alpha": False}


def test_manage_memories_adds_memory(monkeypatch):
    renderer, _ = _make_renderer()

    class FakeMemoryProvider:
        def __init__(self):
            self.memories = []

        def list_long_term_memories(self):
            return list(self.memories)

        def append_long_term_memory(self, **values):
            record = {
                "id": f"mem_{len(self.memories) + 1}",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
                **values,
            }
            self.memories.append(record)
            return record

    provider = FakeMemoryProvider()
    _feed_inputs(monkeypatch, ["a", "category", "insight", "", "reuse", "q"])

    deleted = simple_management.manage_memories(renderer, provider)

    assert deleted == []
    assert provider.memories[0]["category"] == "category"
    assert provider.memories[0]["reuse_condition"] == "reuse"


def test_manage_memory_config_collects_valid_values(monkeypatch):
    renderer, _ = _make_renderer()
    values = {
        "context_length": 200,
        "memory_size": 30,
        "tool_output_compact_threshold": 60,
        "partial_compact_threshold": 85,
        "tool_output_compact_tokens": 2000,
        "partial_compact_min_percent": 40,
        "partial_compact_max_percent": 70,
        "memory_recall_window_size": 3,
        "memory_pre_recall": True,
        "memory_recall_model_key": None,
        "memory_recall_model_display": "同主模型",
    }
    _feed_inputs(
        monkeypatch,
        ["100", "20", "50", "80", "1000", "30", "60", "2", "n"],
    )

    result = simple_management.manage_memory_config(renderer, values, None)

    assert result["context_length"] == 100
    assert result["tool_output_compact_threshold"] == 50
    assert result["partial_compact_threshold"] == 80
    assert result["tool_output_compact_tokens"] == 1000
    assert result["partial_compact_min_percent"] == 30
    assert result["partial_compact_max_percent"] == 60
    assert result["memory_pre_recall"] is False


def test_manage_layout_saves_ratios(monkeypatch):
    renderer, _ = _make_renderer()
    saved = {}
    monkeypatch.setattr(
        simple_management,
        "load_layout_ratios",
        lambda: {"task": 3, "background": 4, "sub_agent": 2},
    )
    monkeypatch.setattr(
        simple_management,
        "save_layout_ratios",
        lambda ratios: saved.update(ratios),
    )
    _feed_inputs(monkeypatch, ["5", "6", "7"])

    result = simple_management.manage_layout(renderer)

    assert result == {"task": 5, "background": 6, "sub_agent": 7}
    assert saved == result


def test_choose_mcp_switch_toggles_and_confirms(monkeypatch):
    renderer, _ = _make_renderer()

    class FakeMCPManager:
        def list_server_switches(self):
            return [
                {
                    "name": "demo",
                    "disabled": False,
                    "runtime_state": "loaded",
                    "transport": "stdio",
                    "target": "npx demo",
                    "tool_count": 2,
                }
            ]

    _feed_inputs(monkeypatch, ["1", "y"])

    result = simple_management.choose_mcp_switch(
        renderer,
        [
            {
                "name": "demo",
                "disabled": False,
                "runtime_state": "loaded",
            }
        ],
        FakeMCPManager(),
    )

    assert result["action"] == "confirm"
    assert result["disabled_updates"] == {"demo": True}


def test_update_terminal_title_uses_conversation_title(monkeypatch):
    import main

    monkeypatch.setattr(main, "_get_current_conversation_title", lambda: "Demo")
    monkeypatch.setattr(main, "_terminal_title_supported", lambda: True)
    captured = []
    monkeypatch.setattr(main, "set_terminal_title", captured.append)

    main._update_terminal_title()

    assert captured == ["MakeCode · Demo"]


def test_interactive_choose_navigates_with_arrow_keys(monkeypatch):
    renderer, _ = _make_renderer()
    keys = iter(["down", "down", "up", "enter"])
    monkeypatch.setattr(simple_input, "is_interactive", lambda: True)
    monkeypatch.setattr(simple_input, "raw_mode", lambda: nullcontext())
    monkeypatch.setattr(simple_input, "read_key", lambda: next(keys))

    assert renderer.choose("Pick", ["alpha", "beta", "gamma"]) == "beta"


def test_arrow_escape_sequences_are_parsed():
    assert simple_input._parse_escape(b"\x1b[A") == "up"
    assert simple_input._parse_escape(b"\x1bOA") == "up"
    assert simple_input._parse_escape(b"\x1b[B") == "down"
    assert simple_input._parse_escape(b"\x1b[1;5C") == "right"
    assert simple_input._parse_escape(b"\x1b[D") == "left"
    assert simple_input._parse_escape(b"\x1b[H") == "home"
    assert simple_input._parse_escape(b"\x1b[3~") == "delete"


def test_image_placeholders_serialize_back_to_attachment_markers():
    text = "看看 [图片：a.png] 和 [图片：b.png]"
    entries = [
        ("[图片：a.png]", "[[image:id=aaa]]"),
        ("[图片：b.png]", "[[image:id=bbb]]"),
    ]

    assert simple_input._serialize_input(text, entries) == (
        "看看 [[image:id=aaa]] 和 [[image:id=bbb]]"
    )


def test_deleted_image_placeholder_is_not_serialized():
    text = "只看 [图片：b.png]"
    entries = [
        ("[图片：a.png]", "[[image:id=aaa]]"),
        ("[图片：b.png]", "[[image:id=bbb]]"),
    ]

    assert simple_input._serialize_input(text, entries) == (
        "只看 [[image:id=bbb]]"
    )


def test_choose_add_model_collects_fields(monkeypatch):
    renderer, _ = _make_renderer()
    _feed_inputs(
        monkeypatch,
        ["openai_chat", "https://api.example.com", "secret", "model-a", "alias-a"],
    )

    fields = simple_management.choose_add_model(renderer)

    assert fields == {
        "base_url": "https://api.example.com",
        "api_key": "secret",
        "model_input": "model-a",
        "alias_input": "alias-a",
        "message_format": "openai_chat",
    }


def test_manage_extra_tools_toggles_and_saves(monkeypatch):
    renderer, _ = _make_renderer()
    saved = []
    monkeypatch.setattr(
        "tools.extra_tools.set_understand_image_config",
        lambda enabled, key: saved.append((enabled, key)) or True,
    )
    monkeypatch.setattr("system.models.get_model_manager", lambda: None)
    _feed_inputs(monkeypatch, ["t", "q"])

    result = simple_management.manage_extra_tools(
        renderer,
        {
            "enabled": False,
            "model_key": None,
            "model_display": "同主模型",
        },
    )

    assert result == "<closed>"
    assert saved == [(True, None)]


def test_show_copy_content_copies_all(monkeypatch):
    renderer, _ = _make_renderer()
    copied = []
    monkeypatch.setattr(
        simple_management,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )
    _feed_inputs(monkeypatch, ["c"])

    result = simple_management.show_copy_content(
        renderer,
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
    )

    assert result == "closed"
    assert "question" in copied[0]
    assert "answer" in copied[0]
