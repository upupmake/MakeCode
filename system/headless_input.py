"""Line-delimited JSON input loop for persistent headless sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from collections.abc import Callable
from typing import Any

from system.agent_runtime import AgentRuntime, TurnResult
from system.headless_ask import AskUserChannel
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
        writer: StreamJsonWriter | None,
        *,
        replay_user_messages: bool = False,
        stop_event: asyncio.Event | None = None,
        write_result: Callable[[TurnResult], int] | None = None,
        ask_channel: AskUserChannel | None = None,
    ) -> None:
        self.runtime = runtime
        self.writer = writer
        self.replay_user_messages = replay_user_messages
        self.stop_event = stop_event
        self._write_result = write_result
        self.ask_channel = ask_channel
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
                self._emit_error("用户消息没有可用的文本内容。")
                continue
            if self.replay_user_messages and self.writer is not None:
                self.writer.write_user_message(message)
            start_cancel_listener()
            try:
                if self.stop_event is not None and self.stop_event.is_set():
                    return exit_code
                result = await self.runtime.run_turn(query)
            finally:
                stop_cancel_listener()
            turn_exit_code = self._emit_result(result)
            if turn_exit_code != 0:
                exit_code = turn_exit_code

    def _emit_result(self, result: TurnResult) -> int:
        if self.writer is not None:
            self.writer.write_result(result)
            return 0 if not result.is_error else 1
        if self._write_result is not None:
            return self._write_result(result)
        return 1

    def _emit_error(self, message: str) -> None:
        if self.writer is not None:
            self.writer.write(
                {
                    "type": "error",
                    "error": message,
                    "session_id": self.writer.session_id,
                }
            )
        else:
            print(f"MakeCode: {message}", file=sys.stderr)

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
                if self.ask_channel is not None:
                    self.ask_channel.close()
                self._put(_EOF)
                return
            if self.ask_channel is not None and self.ask_channel.handle_line(line):
                continue
            line = line.strip()
            if not line:
                continue
            kind, payload = self._parse_line(line)
            if kind == "control":
                if payload == "interrupt":
                    cancel_current_response()
                continue
            if kind == "notification":
                self._notify(payload)
                continue
            if kind == "error":
                self._emit_error(payload)
                continue
            self._put(payload)

    def _put(self, item: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def _notify(self, notification: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(
                self.runtime.enqueue_notification, notification
            )

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
            if action == "notification":
                notification = payload.get("notification")
                if not isinstance(notification, dict):
                    return "error", "control/notification 缺少有效的 notification 对象。"
                return "notification", notification
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


class TextInputLoop(StreamJsonInputLoop):
    """Persistent multi-turn mode where each stdin line is one user message."""

    def __init__(
        self,
        runtime: AgentRuntime,
        write_result: Callable[[TurnResult], int],
        *,
        stop_event: asyncio.Event | None = None,
        ask_channel: AskUserChannel | None = None,
    ) -> None:
        super().__init__(
            runtime,
            None,
            stop_event=stop_event,
            ask_channel=ask_channel,
        )
        self._write_result = write_result

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
                continue
            start_cancel_listener()
            try:
                if self.stop_event is not None and self.stop_event.is_set():
                    return exit_code
                result = await self.runtime.run_turn(query)
            finally:
                stop_cancel_listener()
            turn_exit_code = self._write_result(result)
            if turn_exit_code != 0:
                exit_code = turn_exit_code

    def _read_stdin(self) -> None:
        while True:
            line = sys.stdin.readline()
            if line == "":
                if self.ask_channel is not None:
                    self.ask_channel.close()
                self._put(_EOF)
                return
            if self.ask_channel is not None and self.ask_channel.handle_line(line):
                continue
            line = line.strip()
            if not line:
                continue
            self._put({"role": "user", "content": line})
