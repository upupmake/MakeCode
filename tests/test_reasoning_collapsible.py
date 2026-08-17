import pytest
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

import system.console_render as console_render
from system.console_render import _render_history
from system.tui_app import CopyableContentGroup, ContentBlock, MakeCodeTuiApp
from system.tui_types import TuiEvent, TuiRegion


def _open_reasoning(app: MakeCodeTuiApp) -> None:
    app.handle_tui_event(
        TuiEvent(
            TuiRegion.REASONING,
            None,
            collapsible_title="🧠 Reasoning",
            collapsible_open=True,
        )
    )


def _close_reasoning(app: MakeCodeTuiApp) -> None:
    app.handle_tui_event(
        TuiEvent(
            TuiRegion.REASONING,
            None,
            collapsible_title="🧠 Reasoning",
            collapsible_close=True,
        )
    )


@pytest.mark.anyio
async def test_streaming_reasoning_goes_into_expanded_collapsible():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考第一段\n\n"))
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考第二段\n\n"))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 1
        collapsible = container.children[0]
        assert isinstance(collapsible, Collapsible)
        assert collapsible.collapsed is False
        contents = collapsible.query_one(Collapsible.Contents)
        assert len(contents.children) == 2

        texts = [str(child.content) for child in contents.children]
        assert texts[0] == "思考第一段\n\n"
        assert texts[1] == "思考第二段\n\n"


@pytest.mark.anyio
async def test_close_collapses_reasoning_and_releases_active_reference():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "缓冲内容\n\n"))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        collapsible = container.children[0]

        _close_reasoning(app)
        await pilot.pause()

        assert collapsible.collapsed is True
        assert app._active_reasoning_collapsible is None

        # 关闭后普通 REASONING 输出回到容器顶层，不再进入收纳容器
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "游离段落\n\n"))
        await pilot.pause()
        assert len(container.children) == 2
        assert isinstance(container.children[1], Static)


@pytest.mark.anyio
async def test_content_blocks_never_enter_reasoning_collapsible():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考\n\n"))
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "正文段落\n\n"))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 2
        assert isinstance(container.children[0], Collapsible)
        assert isinstance(container.children[1], Static)


@pytest.mark.anyio
async def test_each_reasoning_collapsible_toggles_independently():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for index in range(2):
            _open_reasoning(app)
            app.handle_tui_event(TuiEvent(TuiRegion.REASONING, f"第{index + 1}轮思考\n\n"))
            _close_reasoning(app)
            await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 2
        first, second = container.children
        assert isinstance(first, Collapsible)
        assert isinstance(second, Collapsible)
        assert first.collapsed is True
        assert second.collapsed is True

        first.collapsed = False
        await pilot.pause()

        assert first.collapsed is False
        assert second.collapsed is True


@pytest.mark.anyio
async def test_oneshot_reasoning_event_mounts_collapsed_collapsible():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.handle_tui_event(
            TuiEvent(
                TuiRegion.REASONING,
                console_render.render_model_markdown("回放的思考内容"),
                collapsible_title="🧠 Reasoning",
            )
        )
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 1
        collapsible = container.children[0]
        assert isinstance(collapsible, Collapsible)
        assert collapsible.collapsed is True
        assert len(collapsible.query_one(Collapsible.Contents).children) == 1


@pytest.mark.anyio
async def test_history_replay_skips_reasoning_and_renders_content(monkeypatch):
    posted = []

    def fake_post_tui(region, payload=None, **kwargs):
        posted.append((region, payload, kwargs))

    monkeypatch.setattr(console_render, "post_tui", fake_post_tui)

    messages = [
        {"role": "user", "content": "你好"},
        {
            "role": "assistant",
            "reasoning_content": "先分析问题",
            "content": "这是回答",
        },
    ]
    _render_history(messages)

    reasoning_posts = [item for item in posted if item[0] == TuiRegion.REASONING]
    assert reasoning_posts == []

    content_posts = [item for item in posted if item[0] == TuiRegion.CONTENT and item[1] is not None]
    assert len(content_posts) == 2


@pytest.mark.anyio
async def test_clear_content_removes_collapsibles_and_resets_active():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考\n\n"))
        await pilot.pause()

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "", clear=True))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 0
        assert app._active_reasoning_collapsible is None


@pytest.mark.anyio
async def test_clear_content_resets_scroll_position():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        for index in range(30):
            app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, f"内容 {index}\n\n"))
        await pilot.pause()
        container = app.query_one("#content-log", VerticalScroll)
        container.scroll_end(animate=False)
        await pilot.pause()
        assert container.scroll_y > 0

        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "", clear=True))
        await pilot.pause()

        assert len(container.children) == 0
        assert container.scroll_y == 0


@pytest.mark.anyio
async def test_collapsible_and_blocks_track_container_width():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考\n\n"))
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, "正文" * 60))
        _close_reasoning(app)
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        collapsible, block = container.children
        inner_width = container.content_region.width
        assert collapsible.region.width == inner_width
        assert block.region.width == inner_width

        await pilot.resize_terminal(100, 40)
        await pilot.pause()
        await pilot.pause()

        new_width = container.content_region.width
        assert new_width < inner_width
        assert collapsible.region.width == new_width
        assert block.region.width == new_width


@pytest.mark.anyio
async def test_content_invalid_markup_falls_back_to_plain_text_block():
    app = MakeCodeTuiApp()
    payload = "&mt=doc&dt=doc','https://example.com/knowledge/demo[/link]"

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.handle_tui_event(TuiEvent(TuiRegion.CONTENT, payload))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        assert len(container.children) == 1
        assert str(container.children[0].content) == payload


@pytest.mark.anyio
async def test_render_async_stream_finalizes_reasoning_collapsible():
    from types import SimpleNamespace

    from system.stream_render import StreamRenderer

    async def fake_stream():
        yield {"type": "reasoning", "content": "先想一想\n\n"}
        yield {"type": "reasoning", "content": "继续想\n\n"}
        yield {"type": "text", "content": "这是回答第一段\n\n"}
        yield {"type": "text", "content": "回答第二段"}
        yield {
            "type": "done",
            "result": SimpleNamespace(
                text="这是回答第一段\n\n回答第二段",
                tool_calls=[],
                assistant_message=None,
            ),
        }

    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        renderer = StreamRenderer()
        text, tool_calls, raw = await renderer.render_async(fake_stream(), agent_name="Orchestrator")
        await pilot.pause()

        assert text == "这是回答第一段\n\n回答第二段"

        container = app.query_one("#content-log", VerticalScroll)
        collapsible = container.children[0]
        assert isinstance(collapsible, Collapsible)
        assert collapsible.collapsed is True
        assert len(collapsible.query_one(Collapsible.Contents).children) == 2
        assert isinstance(container.children[1], Static)
        assert isinstance(container.children[2], CopyableContentGroup)


@pytest.mark.anyio
async def test_reasoning_parent_toggles_when_clicking_content_block():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _open_reasoning(app)
        app.handle_tui_event(TuiEvent(TuiRegion.REASONING, "思考内容\n\n"))
        await pilot.pause()

        container = app.query_one("#content-log", VerticalScroll)
        collapsible = container.children[0]
        block = collapsible.query_one(ContentBlock)
        assert collapsible.collapsed is False

        await pilot.click(block, offset=(2, 0))
        await pilot.pause()

        assert collapsible.collapsed is True


@pytest.mark.anyio
async def test_render_async_tool_calls_only_stream_still_collapses_reasoning():
    from types import SimpleNamespace

    from system.stream_render import StreamRenderer

    async def fake_stream():
        yield {"type": "reasoning", "content": "只思考不输出正文\n\n"}
        yield {
            "type": "tool_calls",
            "content": None,
        }
        yield {
            "type": "done",
            "result": SimpleNamespace(
                text="",
                tool_calls=[{"id": "1", "function": {"name": "FileRead", "arguments": "{}"}}],
                assistant_message=None,
            ),
        }

    app = MakeCodeTuiApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        renderer = StreamRenderer()
        text, tool_calls, raw = await renderer.render_async(fake_stream(), agent_name="Orchestrator")
        await pilot.pause()

        assert text == ""
        assert len(tool_calls) == 1

        container = app.query_one("#content-log", VerticalScroll)
        collapsible = container.children[0]
        assert isinstance(collapsible, Collapsible)
        assert collapsible.collapsed is True
        assert len(collapsible.query_one(Collapsible.Contents).children) == 1
