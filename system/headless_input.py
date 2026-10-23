"""Line-delimited JSON input loop for persistent headless sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from typing import Any

from system.agent_runtime import AgentRuntime
from system.output_sinks import StreamJsonWriter
from system.stream_cancel import (
    cancel_current_response,
    start_cancel_listener,
    stop_cancel_listener,
)

_EOF = object()


class StreamJsonInputLoop:
    def __init__(
        self,
        runtime: AgentRuntime,
        writer: StreamJsonWriter,
        *,
        replay_user_messages: bool = False,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.runtime = runtime
        self.writer = writer
        self.replay_user_messages = replay_user_messages
        self.stop_event = stop_event
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._read_stdin, daemon=True).start()
        exit_code = 0
        while True:
            message = await self._next_message()
            if message is _EOF or message is None:
                return exit_code
            if self.stop_event is not None and self.stop_event.is_set():
                return exit_code
            query = self._query_text(message)
            if not query:
                self.writer.write(
                    {
                        "type": "error",
                        "error": "用户消息没有可用的文本内容。",
                        "session_id": self.writer.session_id,
                    }
                )
                continue
            if self.replay_user_messages:
                self.writer.write_user_message(message)
            start_cancel_listener()
            try:
                if self.stop_event is not None and self.stop_event.is_set():
                    return exit_code
                result = await self.runtime.run_turn(query)
            finally:
                stop_cancel_listener()
            self.writer.write_result(result)
            if result.is_error:
                exit_code = 1

    async def _next_message(self) -> Any:
        if self.stop_event is not None and self.stop_event.is_set():
            return None
        queue_task = asyncio.create_task(self._queue.get())
        if self.stop_event is None:
            return await queue_task
        stop_task = asyncio.create_task(self.stop_event.wait())
        done, pending = await asyncio.wait(
            {queue_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if stop_task in done and stop_task.result():
            return None
        return queue_task.result()

    def _read_stdin(self) -> None:
        while True:
            line = sys.stdin.readline()
            if line == "":
                self._put(_EOF)
                return
            line = line.strip()
            if not line:
                continue
            kind, payload = self._parse_line(line)
            if kind == "control":
                if payload == "interrupt":
                    cancel_current_response()
                continue
            if kind == "error":
                self.writer.write(
                    {
                        "type": "error",
                        "error": payload,
                        "session_id": self.writer.session_id,
                    }
                )
                continue
            self._put(payload)

    def _put(self, item: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

    @staticmethod
    def _parse_line(line: str) -> tuple[str, Any]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            return "error", f"无效的 JSON: {exc}"
        if not isinstance(payload, dict):
            return "error", "输入消息必须是 JSON 对象。"
        message_type = payload.get("type")
        if message_type == "control":
            action = payload.get("action")
            if action == "interrupt":
                return "control", "interrupt"
            return "error", f"未知 control action: {action}"
        if message_type not in (None, "user"):
            return "error", f"未知消息类型: {message_type}"
        message = payload.get("message")
        if not isinstance(message, dict):
            if "content" not in payload:
                return "error", "用户消息缺少 message 或 content。"
            message = {"role": "user", "content": payload["content"]}
        return "user", message

    @staticmethod
    def _query_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text")
            ]
            return "\n".join(parts).strip()
        return ""
