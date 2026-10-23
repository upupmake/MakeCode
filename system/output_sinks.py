"""Output sink implementations for TUI, simple terminal and headless modes."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from typing import Any, TextIO

from system.tui_types import TuiEvent


class TuiSink:
    def __init__(self, post: Callable[..., None]) -> None:
        self._post = post

    def emit(self, event: TuiEvent) -> None:
        self._post(
            event.region,
            event.payload,
            clear=event.clear,
            tail=event.tail,
            active=event.active,
            collapsible_title=event.collapsible_title,
            collapsible_open=event.collapsible_open,
            collapsible_close=event.collapsible_close,
            collapsible_kind=event.collapsible_kind,
            tail_after=event.tail_after,
        )

    def close(self) -> None:
        pass


class SimpleSink:
    def __init__(self, renderer: Any) -> None:
        self._renderer = renderer

    def emit(self, event: TuiEvent) -> None:
        self._renderer.handle(event)

    def close(self) -> None:
        pass


class NullSink:
    """Discard UI-only events in headless mode."""

    def emit(self, event: TuiEvent) -> None:
        pass

    def close(self) -> None:
        pass


class StreamJsonWriter:
    """Line-delimited JSON writer for the headless stream-json protocol."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        session_id: str = "",
        include_partial: bool = False,
    ) -> None:
        self._stream = stream or sys.stdout
        self._session_id = session_id
        self._include_partial = include_partial
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    def write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def init(
        self,
        *,
        model: str | None,
        cwd: str,
        permission_mode: str,
        tools: list[str] | None = None,
    ) -> None:
        self.write(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self._session_id,
                "model": model,
                "cwd": cwd,
                "tools": tools or [],
                "permission_mode": permission_mode,
                "api_version": 1,
            }
        )

    def write_user_message(self, message: dict[str, Any]) -> None:
        self.write(
            {
                "type": "user",
                "message": message,
                "session_id": self._session_id,
            }
        )

    def handle_runtime_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "text_delta":
            if self._include_partial:
                self.write(
                    {
                        "type": "assistant_delta",
                        "text": event.get("text", ""),
                        "session_id": self._session_id,
                    }
                )
            return
        if event_type == "assistant_done":
            self.write(
                {
                    "type": "assistant",
                    "message": event.get("message"),
                    "session_id": self._session_id,
                }
            )
            return
        if event_type == "tool_use":
            tool_input = event.get("input")
            if isinstance(tool_input, str):
                try:
                    parsed_input = json.loads(tool_input)
                except (TypeError, ValueError):
                    pass
                else:
                    if isinstance(parsed_input, dict):
                        tool_input = parsed_input
            self.write(
                {
                    "type": "tool_use",
                    "id": event.get("id"),
                    "name": event.get("name"),
                    "input": tool_input,
                    "session_id": self._session_id,
                }
            )
            return
        if event_type == "tool_result":
            self.write(
                {
                    "type": "tool_result",
                    "tool_use_id": event.get("tool_use_id"),
                    "name": event.get("name"),
                    "content": event.get("content"),
                    "is_error": event.get("is_error", False),
                    "session_id": self._session_id,
                }
            )

    def write_result(self, result: Any) -> None:
        self.write(
            {
                "type": "result",
                "subtype": (
                    "cancelled"
                    if result.cancelled
                    else "error"
                    if result.is_error
                    else "success"
                ),
                "is_error": result.is_error,
                "result": result.text,
                "session_id": result.session_id,
                "usage": result.usage,
                "permission_denials": result.permission_denials,
                "duration_ms": result.duration_ms,
                "error": result.error,
            }
        )


class TextSink:
    """Minimal text sink used by headless mode before richer protocol support."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def emit(self, event: TuiEvent) -> None:
        payload = event.payload
        if payload is None:
            return
        text = getattr(payload, "plain", None)
        if text is None:
            text = str(payload)
        if not text:
            return
        self._stream.write(text)
        if not text.endswith("\n"):
            self._stream.write("\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.flush()
