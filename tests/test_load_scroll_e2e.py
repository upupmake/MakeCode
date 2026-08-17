"""按真实 /load 链路（工作线程 + TuiConsole + _render_history）验证滚动行为。"""
import threading

import pytest

from system.commands import CommandHandler
from system.console_render import (
    console as tui_console,
    _render_env_customization_hint,
    _render_history,
    _render_startup_banner,
)
from system.tui_app import (
    MakeCodeTuiApp,
    TUI_BRIDGE,
    post_tui,
    scroll_all_panes_to_bottom,
    set_agent_loop_active,
)
from system.tui_types import TuiRegion
from utils.conversations import ConversationStore


def _build_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    messages = [{"role": "system", "content": "system"}]
    for index in range(12):
        messages.append({"role": "user", "content": f"用户问题 {index}\n" + "细节\n" * 6})
        messages.append({
            "role": "assistant",
            "reasoning_content": f"思考过程 {index}\n" + "推理\n" * 4,
            "content": f"助手回答 {index}\n" + ("回答内容\n" * 8),
            "tool_calls": [{
                "id": f"call_{index}",
                "function": {"name": "FileRead", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{index}",
            "name": "FileRead",
            "content": "工具结果\n" + ("结果行\n" * 10),
        })
    store.save_messages(messages)
    store.reset()
    return store, messages


def _make_handler(store):
    return CommandHandler(
        console=tui_console,
        mcp_manager=None,
        skill_loader=None,
        get_system_prompt_fn=lambda: "",
        conversation_store=store,
        auto_compact_fn=lambda *args, **kwargs: None,
    )


@pytest.mark.anyio
async def test_real_load_flow_scrolls_every_pane_to_bottom(tmp_path, monkeypatch):
    store, _ = _build_conversation(tmp_path)
    conversation = store.list_conversations()[0]
    monkeypatch.setattr(
        "system.commands.interactive_choose_conversation",
        lambda conversations, **kwargs: str(conversation),
    )
    handler = _make_handler(store)

    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        TUI_BRIDGE.bind(app)
        try:
            # 预置旧内容并把视口停在中上部，模拟加载前的任意滚动位置
            for index in range(30):
                post_tui(TuiRegion.CONTENT, f"旧内容 {index}\n\n")
                post_tui(TuiRegion.TOOLS, f"旧工具输出 {index}\n\n")
            await pilot.pause()
            content_scroller = app.query_one("#content-log")
            tools_log = app._logs[TuiRegion.TOOLS]
            task_log = app._logs[TuiRegion.TASK]
            content_scroller.scroll_to(y=0, animate=False, immediate=True)
            tools_log.scroll_to(y=0, animate=False, immediate=True)
            task_log.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()

            done = threading.Event()
            failures = []

            def worker():
                try:
                    set_agent_loop_active(True)
                    new_history = None
                    try:
                        new_history, _ = handler.handle_load(
                            [{"role": "system", "content": "old"}],
                            None,
                            _render_startup_banner,
                            _render_env_customization_hint,
                            _render_history,
                        )
                    finally:
                        set_agent_loop_active(False)
                    if new_history is not None:
                        scroll_all_panes_to_bottom()
                except Exception as exc:  # pragma: no cover
                    failures.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=worker)
            thread.start()
            for _ in range(600):
                if done.is_set():
                    break
                await pilot.pause()
            done.wait(timeout=10)
            thread.join(timeout=10)
            for _ in range(10):
                await pilot.pause()

            assert not failures, failures
            assert content_scroller.scroll_y == content_scroller.max_scroll_y, (
                "content", content_scroller.scroll_y, content_scroller.max_scroll_y,
            )
            assert tools_log.scroll_y == tools_log.max_scroll_y, (
                "tools", tools_log.scroll_y, tools_log.max_scroll_y,
            )
            assert task_log.scroll_y == task_log.max_scroll_y, (
                "task", task_log.scroll_y, task_log.max_scroll_y,
            )
        finally:
            TUI_BRIDGE.unbind(app)


@pytest.mark.anyio
async def test_load_with_real_choice_modal_scrolls_every_pane_to_bottom(tmp_path):
    """不 mock 选择器：真实 modal 选择后，验证所有 pane 到底部。"""
    store, _ = _build_conversation(tmp_path)
    handler = _make_handler(store)

    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        TUI_BRIDGE.bind(app)
        try:
            done = threading.Event()
            failures = []
            results = {}

            def worker():
                try:
                    set_agent_loop_active(True)
                    new_history = None
                    try:
                        new_history, results["path"] = handler.handle_load(
                            [{"role": "system", "content": "old"}],
                            None,
                            _render_startup_banner,
                            _render_env_customization_hint,
                            _render_history,
                        )
                    finally:
                        set_agent_loop_active(False)
                    if new_history is not None:
                        scroll_all_panes_to_bottom()
                except Exception as exc:  # pragma: no cover
                    failures.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=worker)
            thread.start()

            # 等待选择 modal 出现并确认默认项
            for _ in range(200):
                if len(app.screen_stack) > 1 or done.is_set():
                    break
                await pilot.pause()
            if len(app.screen_stack) > 1:
                await pilot.press("enter")

            for _ in range(600):
                if done.is_set():
                    break
                await pilot.pause()
            done.wait(timeout=10)
            thread.join(timeout=10)
            for _ in range(10):
                await pilot.pause()

            assert not failures, failures
            assert results.get("path") is not None
            content_scroller = app.query_one("#content-log")
            tools_log = app._logs[TuiRegion.TOOLS]
            task_log = app._logs[TuiRegion.TASK]
            assert content_scroller.scroll_y == content_scroller.max_scroll_y, (
                "content", content_scroller.scroll_y, content_scroller.max_scroll_y,
            )
            assert tools_log.scroll_y == tools_log.max_scroll_y, (
                "tools", tools_log.scroll_y, tools_log.max_scroll_y,
            )
            assert task_log.scroll_y == task_log.max_scroll_y, (
                "task", task_log.scroll_y, task_log.max_scroll_y,
            )
        finally:
            TUI_BRIDGE.unbind(app)
