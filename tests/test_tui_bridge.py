"""TuiBridge 事件队列：工作线程只入队，UI 线程按序批量 drain，并与其他跨线程调用保持先后顺序。"""
import asyncio
import sys
import threading
import time
from unittest.mock import patch

import pytest
from rich.text import Text

from system.stream_render import StreamRenderer
from system.tui_app import (
    TUI_BRIDGE,
    ContentBlock,
    CopyableContentGroup,
    InlineCollapsible,
    MakeCodeTuiApp,
    TuiBridge,
    begin_tui_batch_render,
    end_tui_batch_render,
    post_tui,
)
from system.tui_types import TuiRegion


class FakeApp:
    """记录 TuiBridge 对 App 的调用：call_later 只登记回调，由测试在“UI 线程”里手动触发。"""

    def __init__(self):
        self.handled = []
        self.callbacks = []
        self.accepts_callbacks = True

    def handle_tui_event(self, event):
        self.handled.append(event)

    def set_client_request_active(self, *args):
        pass

    def call_later(self, callback, *args, **kwargs):
        if not self.accepts_callbacks:
            return False
        self.callbacks.append((callback, args, kwargs))
        return True

    def run_callbacks(self):
        while self.callbacks:
            callback, args, kwargs = self.callbacks.pop(0)
            callback(*args, **kwargs)


def make_bridge():
    bridge = TuiBridge()
    app = FakeApp()
    bridge._app = app
    bridge._app_thread_id = threading.get_ident()  # 当前线程充当 UI 线程
    return bridge, app


def post_from_worker(bridge, *calls):
    def work():
        for region, payload, kwargs in calls:
            bridge.post(region, payload, **kwargs)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()


def payloads(app):
    return [event.payload for event in app.handled]


def test_worker_events_are_queued_and_handled_in_order_by_one_scheduled_drain():
    bridge, app = make_bridge()

    post_from_worker(bridge, *[(TuiRegion.CONTENT, f"block {i}", {}) for i in range(5)])

    assert app.handled == []
    assert len(app.callbacks) == 1
    app.run_callbacks()
    assert payloads(app) == [f"block {i}" for i in range(5)]
    assert app.callbacks == []


def test_ui_thread_post_handles_queued_events_first_to_keep_global_order():
    bridge, app = make_bridge()
    post_from_worker(bridge, (TuiRegion.CONTENT, "from worker 1", {}), (TuiRegion.CONTENT, "from worker 2", {}))

    bridge.post(TuiRegion.CONTENT, "from ui")

    assert payloads(app) == ["from worker 1", "from worker 2", "from ui"]


def test_consecutive_tail_events_of_one_region_collapse_to_the_latest():
    bridge, app = make_bridge()

    post_from_worker(
        bridge,
        (TuiRegion.CONTENT, Text("a"), {"tail": True}),
        (TuiRegion.CONTENT, Text("b"), {"tail": True}),
        (TuiRegion.CONTENT, Text("c"), {"tail": True}),
    )
    app.run_callbacks()

    assert [event.payload.plain for event in app.handled] == ["c"]


def test_tail_events_separated_by_other_events_are_all_kept():
    bridge, app = make_bridge()

    post_from_worker(
        bridge,
        (TuiRegion.CONTENT, Text("a"), {"tail": True}),
        (TuiRegion.CONTENT, "block", {}),
        (TuiRegion.CONTENT, Text("b"), {"tail": True}),
        (TuiRegion.REASONING, Text("r"), {"tail": True}),
    )
    app.run_callbacks()

    assert [getattr(event.payload, "plain", event.payload) for event in app.handled] == ["a", "block", "b", "r"]


def test_status_and_runtime_info_updates_collapse_per_region_only():
    bridge, app = make_bridge()

    post_from_worker(
        bridge,
        (TuiRegion.STATUS, "one", {}),
        (TuiRegion.STATUS, "two", {}),
        (TuiRegion.RUNTIME_INFO, "info", {}),
        (TuiRegion.STATUS, "three", {}),
    )
    app.run_callbacks()

    assert [(event.region, event.payload) for event in app.handled] == [
        (TuiRegion.STATUS, "two"),
        (TuiRegion.RUNTIME_INFO, "info"),
        (TuiRegion.STATUS, "three"),
    ]


def test_events_with_side_effects_are_never_collapsed():
    bridge, app = make_bridge()

    post_from_worker(
        bridge,
        (TuiRegion.CONTENT, "", {"tail": True}),
        (TuiRegion.CONTENT, None, {"active": True}),
        (TuiRegion.CONTENT, "", {"tail": True}),
        (TuiRegion.CONTENT, "paragraph", {"tail_after": Text("rest")}),
        (TuiRegion.CONTENT, "paragraph 2", {"tail_after": Text("rest 2")}),
    )
    app.run_callbacks()

    assert len(app.handled) == 5


def test_drain_yields_after_time_slice_and_reschedules_remaining_events(monkeypatch):
    bridge, app = make_bridge()
    monkeypatch.setattr(TuiBridge, "_DRAIN_TIME_SLICE", 0.0)
    post_from_worker(bridge, *[(TuiRegion.CONTENT, f"block {i}", {}) for i in range(4)])

    callback, args, kwargs = app.callbacks.pop(0)
    callback(*args, **kwargs)

    assert payloads(app) == ["block 0"]
    assert len(app.callbacks) == 1  # 剩余事件已重新调度，没有一直占着 UI 线程
    app.run_callbacks()
    assert payloads(app) == [f"block {i}" for i in range(4)]


def test_failing_event_is_logged_and_does_not_drop_following_events():
    bridge, app = make_bridge()
    original = app.handle_tui_event

    def flaky(event):
        if event.payload == "bad":
            raise RuntimeError("render failed")
        original(event)

    app.handle_tui_event = flaky
    post_from_worker(
        bridge,
        (TuiRegion.CONTENT, "good 1", {}),
        (TuiRegion.CONTENT, "bad", {}),
        (TuiRegion.CONTENT, "good 2", {}),
    )

    with patch("system.tui_app._log_bridge_error") as log_error:
        app.run_callbacks()

    assert payloads(app) == ["good 1", "good 2"]
    log_error.assert_called_once()
    assert str(log_error.call_args.args[0]) == "render failed"


def test_event_posted_by_a_handler_runs_after_already_queued_events_without_reentry():
    bridge, app = make_bridge()
    depth = {"now": 0, "max": 0}
    original = app.handle_tui_event

    def reentrant(event):
        depth["now"] += 1
        depth["max"] = max(depth["max"], depth["now"])
        try:
            original(event)
            if event.payload == "first":
                bridge.post(TuiRegion.CONTENT, "posted by handler")
        finally:
            depth["now"] -= 1

    app.handle_tui_event = reentrant
    post_from_worker(bridge, (TuiRegion.CONTENT, "first", {}), (TuiRegion.CONTENT, "second", {}))

    app.run_callbacks()

    assert payloads(app) == ["first", "second", "posted by handler"]
    assert depth["max"] == 1


def test_unbind_discards_queued_events():
    bridge, app = make_bridge()
    post_from_worker(bridge, (TuiRegion.CONTENT, "stale", {}))

    bridge.unbind(app)
    app.run_callbacks()

    assert app.handled == []


def test_bind_replays_events_posted_before_binding_ahead_of_events_queued_after_it():
    bridge = TuiBridge()
    app = FakeApp()
    bridge.post(TuiRegion.CONTENT, "before bind 1")  # 此时没有绑定 App，进入 pending
    bridge.post(TuiRegion.CONTENT, "before bind 2")
    # 模拟绑定过程中另一个线程已经能看到 _app 并把事件入队
    with bridge._app_lock:
        bridge._app = app
        bridge._app_thread_id = threading.get_ident()
    post_from_worker(bridge, (TuiRegion.CONTENT, "queued during bind", {}))

    bridge.bind(app)
    app.run_callbacks()

    assert payloads(app) == ["before bind 1", "before bind 2", "queued during bind"]


def test_events_are_dropped_when_the_app_can_no_longer_schedule_callbacks():
    bridge, app = make_bridge()
    app.accepts_callbacks = False

    post_from_worker(bridge, (TuiRegion.CONTENT, "never shown", {}))

    assert app.handled == []
    assert len(bridge._event_queue) == 0


@pytest.mark.anyio
async def test_cross_thread_calls_run_after_events_posted_earlier_by_the_same_thread():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    order = []

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        original_handle = app.handle_tui_event
        original_set_active = app.set_agent_loop_active
        app.handle_tui_event = lambda event: (order.append(("event", event.payload)), original_handle(event))[1]
        app.set_agent_loop_active = lambda active: (order.append(("loop", active)), original_set_active(active))[1]
        done = threading.Event()

        def worker():
            for index in range(30):
                post_tui(TuiRegion.CONTENT, f"block {index}")
            TUI_BRIDGE.set_agent_loop_active(True)
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(500):
            if done.is_set():
                break
            await asyncio.sleep(0.01)
        thread.join(timeout=5)

        assert done.is_set()
        assert order == [("event", f"block {index}") for index in range(30)] + [("loop", True)]


async def _run_in_worker(pilot, work, timeout=15.0):
    """在工作线程里执行 work()（模拟 Agent 线程），结束后再等 UI 把已入队的事件处理完。"""
    done = threading.Event()
    failures = []

    def run():
        try:
            work()
        except Exception as exc:  # pragma: no cover
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    thread.join(timeout=5)
    assert done.is_set(), "worker thread did not finish"
    assert not failures, failures
    while TUI_BRIDGE._event_queue and time.monotonic() < deadline:
        await pilot.pause()
    for _ in range(3):
        await pilot.pause()


def _stream_in_worker(stream_factory):
    return lambda: asyncio.run(StreamRenderer().render_async(stream_factory(), "Orchestrator"))


@pytest.mark.anyio
async def test_stream_renderer_from_worker_thread_renders_every_paragraph_in_order():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    text = "段落一\n\n段落二\n\n段落三\n\n尾部"

    async def stream():
        for index in range(0, len(text), 3):
            yield {"type": "text", "content": text[index:index + 3]}

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _run_in_worker(pilot, _stream_in_worker(stream))

        groups = list(app.query(CopyableContentGroup))
        assert "".join(group.copy_text for group in groups) == text
        assert not app.query_one("#content-tail").has_class("pane-tail-visible")
        assert not app.query_one("#content-pane").has_class("pane-active")


@pytest.mark.anyio
async def test_reasoning_then_text_stream_keeps_reasoning_collapsed_and_text_complete():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    reasoning = "思考一\n\n思考二\n\n思考尾"
    answer = "回答一\n\n回答二\n\n回答尾"

    async def stream():
        for index in range(0, len(reasoning), 3):
            yield {"type": "reasoning", "content": reasoning[index:index + 3]}
        for index in range(0, len(answer), 3):
            yield {"type": "text", "content": answer[index:index + 3]}

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _run_in_worker(pilot, _stream_in_worker(stream))

        collapsibles = list(app.query(InlineCollapsible))
        assert len(collapsibles) == 1
        assert collapsibles[0].collapsed
        assert len(collapsibles[0].query(ContentBlock)) == 3
        groups = list(app.query(CopyableContentGroup))
        assert "".join(group.copy_text for group in groups) == answer
        assert not app.query_one("#content-tail").has_class("pane-tail-visible")
        assert not app.query_one("#content-pane").has_class("pane-active")


@pytest.mark.anyio
async def test_cancelled_stream_leaves_no_active_pane_or_tail_behind():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    cancelled = {"on": False}
    yielded = []

    async def stream():
        for index in range(10):
            yielded.append(index)
            yield {"type": "text", "content": "取消之前的正文，"}
            if index == 4:
                cancelled["on"] = True

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        with patch("system.stream_render.is_cancelled", side_effect=lambda: cancelled["on"]):
            await _run_in_worker(pilot, _stream_in_worker(stream))

        assert len(yielded) < 10  # 取消确实中断了流
        assert not app.query_one("#content-tail").has_class("pane-tail-visible")
        assert not app.query_one("#content-pane").has_class("pane-active")
        assert app._pane_active_counts.get(TuiRegion.CONTENT, 0) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Textual test timing is unstable on Windows CI")
@pytest.mark.anyio
async def test_events_posted_before_end_batch_render_are_processed_inside_the_batch():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    depths = []

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        content = app.query_one("#content-log")
        original_handle = app.handle_tui_event
        app.handle_tui_event = lambda event: (depths.append(app._batch_render_depth), original_handle(event))[1]

        def replay():
            begin_tui_batch_render(force_scroll=True)
            try:
                for index in range(80):
                    post_tui(TuiRegion.CONTENT, f"历史内容 {index}\n\n")
            finally:
                end_tui_batch_render()

        await _run_in_worker(pilot, replay)

        assert len(depths) == 80
        assert set(depths) == {1}  # 结束批次前，已入队的事件全部在批次内处理完
        assert content.virtual_size.height >= 80
        assert content.scroll_y == content.max_scroll_y > 0


@pytest.mark.anyio
async def test_concurrent_producers_lose_no_events_and_keep_per_thread_order():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    seen = []
    producers, per_producer = 4, 150

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        original_handle = app.handle_tui_event
        app.handle_tui_event = lambda event: (seen.append(event.payload), original_handle(event))[1]

        def produce(index):
            for item in range(per_producer):
                post_tui(TuiRegion.BACKGROUND, f"t{index}-{item}")

        threads = [threading.Thread(target=produce, args=(index,)) for index in range(producers)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 15
        while len(seen) < producers * per_producer and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        for thread in threads:
            thread.join(timeout=5)

        assert len(seen) == producers * per_producer
        for index in range(producers):
            mine = [payload for payload in seen if payload.startswith(f"t{index}-")]
            assert mine == [f"t{index}-{item}" for item in range(per_producer)]


@pytest.mark.anyio
async def test_call_from_thread_keeps_arguments_return_values_and_async_callbacks():
    app = MakeCodeTuiApp(runtime_info_provider=lambda: "")
    results = {}

    async def double(value):
        await asyncio.sleep(0)
        return value * 2

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        def work():
            results["sync"] = app.call_from_thread(lambda left, right=1: left + right, 1, right=2)
            results["async"] = app.call_from_thread(double, 21)

        await _run_in_worker(pilot, work)

        assert results == {"sync": 3, "async": 42}
