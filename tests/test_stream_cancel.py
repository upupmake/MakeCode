import asyncio
from unittest.mock import patch

import pytest

from system import stream_cancel
from system.stream_render import StreamRenderer


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_cancel_stops_stream_that_has_not_emitted_any_event():
    started = asyncio.Event()
    closed = asyncio.Event()

    async def blocked_stream():
        started.set()
        try:
            await asyncio.Event().wait()
            yield {"type": "text", "content": "never"}
        finally:
            closed.set()

    stream_cancel.reset_cancel()
    stream_cancel.start_cancel_listener()
    task = None
    try:
        with patch("system.stream_render.post_tui"), \
                patch("system.stream_cancel.post_tui"):
            task = asyncio.create_task(
                StreamRenderer().render_async(
                    blocked_stream(),
                    agent_name="Orchestrator",
                )
            )
            stream_cancel.register_response_task(task)
            await asyncio.wait_for(started.wait(), timeout=1.0)

            assert stream_cancel.cancel_current_response() is True
            text, tool_calls, raw_message = await asyncio.wait_for(task, timeout=1.0)

        assert (text, tool_calls, raw_message) == ("", [], None)
        assert closed.is_set()
    finally:
        if task is not None:
            stream_cancel.unregister_response_task(task)
        stream_cancel.stop_cancel_listener()
        stream_cancel.reset_cancel()


@pytest.mark.anyio
async def test_cancel_current_response_cancels_registered_awaiting_task():
    stream_cancel.reset_cancel()
    stream_cancel.start_cancel_listener()
    task = asyncio.create_task(asyncio.sleep(60))
    stream_cancel.register_response_task(task)
    try:
        with patch("system.stream_cancel.post_tui"):
            assert stream_cancel.cancel_current_response() is True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        stream_cancel.unregister_response_task(task)
        stream_cancel.stop_cancel_listener()
        stream_cancel.reset_cancel()


@pytest.mark.anyio
async def test_stream_with_render_cancels_before_first_stream_event():
    import main

    started = asyncio.Event()

    class BlockedClient:
        def generate_stream(self, messages, tools):
            async def stream():
                started.set()
                await asyncio.Event().wait()
                yield {"type": "text", "content": "never"}

            return stream()

    stream_cancel.reset_cancel()
    try:
        with patch("system.stream_render.post_tui"), \
                patch("system.stream_cancel.post_tui"):
            task = asyncio.create_task(
                main._stream_with_render([], [], BlockedClient())
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)

            assert stream_cancel.cancel_current_response() is True
            text_content, tool_calls, raw_message, cancelled = await asyncio.wait_for(
                task,
                timeout=1.0,
            )

        assert (text_content, tool_calls, raw_message) == ("", [], None)
        assert cancelled is True
    finally:
        stream_cancel.reset_cancel()


@pytest.mark.anyio
async def test_stream_with_render_cancels_when_task_is_cancelled_before_start():
    import main

    class BlockedClient:
        def generate_stream(self, messages, tools):
            async def stream():
                await asyncio.Event().wait()
                yield {"type": "text", "content": "never"}

            return stream()

    real_register = stream_cancel.register_response_task

    def cancel_then_register(task):
        stream_cancel.cancel_current_response()
        real_register(task)

    stream_cancel.reset_cancel()
    try:
        with patch("system.stream_render.post_tui"), \
                patch("system.stream_cancel.post_tui"), \
                patch("system.stream_cancel.register_response_task", new=cancel_then_register):
            text_content, tool_calls, raw_message, cancelled = await asyncio.wait_for(
                main._stream_with_render([], [], BlockedClient()),
                timeout=1.0,
            )

        assert (text_content, tool_calls, raw_message) == ("", [], None)
        assert cancelled is True
    finally:
        stream_cancel.reset_cancel()
