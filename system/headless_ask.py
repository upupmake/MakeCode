"""AskUser channel for headless CLI modes.

The stdin reader thread dispatches each line through ``handle_line`` before
treating it as a user message. While an AskUser call is pending, the answer is
delivered to the waiting tool thread instead of the next-turn queue.

Two answer formats are supported:

* ``text``: the question is rendered to stderr and the next stdin line is the
  answer (number, option text, custom text, or empty to cancel).
* ``stream-json``: the question is emitted as a ``control_request`` event and
  answered with a ``control_response`` line carrying the same ``request_id``.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

from system.output_sinks import StreamJsonWriter
from system.stream_cancel import is_cancelled

_CANCELLED = {"choice": "<cancelled>"}
_EMPTY = {"choice": "<empty_input>"}


class _PendingAsk:
    def __init__(
        self,
        request_id: str,
        options: list[dict[str, Any]],
    ) -> None:
        self.request_id = request_id
        self.options = options
        self.event = threading.Event()
        self.answer: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def resolve(self, answer: dict[str, Any]) -> None:
        with self._lock:
            if self.answer is not None:
                return
            self.answer = answer
        self.event.set()


class AskUserChannel:
    """Route stdin lines to a pending AskUser call."""

    def __init__(
        self,
        *,
        answer_format: str,
        writer: StreamJsonWriter | None = None,
        direct_stdin: bool = False,
    ) -> None:
        if answer_format not in ("text", "stream-json"):
            raise ValueError(f"Unsupported AskUser answer format: {answer_format}")
        if direct_stdin and answer_format != "text":
            raise ValueError("direct stdin requires text answer format")
        self._answer_format = answer_format
        self._writer = writer
        self._direct_stdin = direct_stdin
        self._pending: _PendingAsk | None = None
        self._lock = threading.RLock()
        self._ask_lock = threading.Lock()
        self._closed = False
        self._counter = 0

    def ask(self, question: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        with self._ask_lock:
            with self._lock:
                if self._closed:
                    return dict(_CANCELLED)
            pending = self._begin(options)
            try:
                if self._writer is not None:
                    self._writer.write(
                        {
                            "type": "control_request",
                            "subtype": "ask_user",
                            "request_id": pending.request_id,
                            "question": question,
                            "options": [
                                {
                                    "content": str(option.get("content", "")),
                                    "is_recommended": bool(
                                        option.get("is_recommended", False)
                                    ),
                                }
                                for option in options
                            ],
                            "session_id": self._writer.session_id,
                        }
                    )
                if self._writer is None or self._answer_format == "text":
                    _render_text_question(question, options)
                if self._direct_stdin:
                    threading.Thread(
                        target=self._read_direct,
                        args=(pending,),
                        daemon=True,
                    ).start()
                self._wait(pending)
            finally:
                self._clear(pending)
            return pending.answer or _CANCELLED

    def handle_line(self, line: str) -> bool:
        """Deliver a stdin line to the pending ask; False means not consumed."""
        with self._lock:
            pending = self._pending
        if pending is None:
            return False
        if self._answer_format == "stream-json":
            if _handle_json_line(pending, line):
                return True
            # A human may answer the stderr menu with plain text even when
            # stdin uses the stream-json protocol. Structured JSON objects are
            # left to the input loop so user messages are not swallowed.
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return False
            pending.resolve(_parse_text_answer(line, pending.options))
            return True
        pending.resolve(_parse_text_answer(line, pending.options))
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = self._pending
        if pending is not None:
            pending.resolve(_CANCELLED)

    def _begin(self, options: list[dict[str, Any]]) -> _PendingAsk:
        with self._lock:
            self._counter += 1
            pending = _PendingAsk(f"ask_{self._counter}", options)
            self._pending = pending
        return pending

    def _clear(self, pending: _PendingAsk) -> None:
        with self._lock:
            if self._pending is pending:
                self._pending = None

    def _wait(self, pending: _PendingAsk) -> None:
        while not pending.event.wait(0.1):
            with self._lock:
                closed = self._closed
            if closed or is_cancelled():
                pending.resolve(_CANCELLED)
                return

    def _read_direct(self, pending: _PendingAsk) -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            pending.resolve(_CANCELLED)
            return
        if line == "":
            pending.resolve(_CANCELLED)
            return
        pending.resolve(_parse_text_answer(line, pending.options))


def _render_text_question(
    question: str,
    options: list[dict[str, Any]],
) -> None:
    lines = ["", f"[AskUser] {question}"]
    for index, option in enumerate(options, 1):
        label = str(option.get("content", ""))
        if option.get("is_recommended"):
            label = f"⭐ {label}（推荐）"
        lines.append(f"  {index}) {label}")
    lines.append("输入序号选择，直接输入自定义回答，空行表示未选择：")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def _parse_text_answer(
    line: str,
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    text = line.strip()
    if not text:
        return dict(_EMPTY)
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(options):
            return {"choice": str(options[index - 1].get("content", ""))}
    for option in options:
        content = str(option.get("content", ""))
        if text == content:
            return {"choice": content}
    return {"choice": text, "custom": True}


def _handle_json_line(pending: _PendingAsk, line: str) -> bool:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    message_type = payload.get("type")
    if message_type == "control":
        if payload.get("action") == "interrupt":
            pending.resolve(_CANCELLED)
            return True
        return False
    if message_type != "control_response":
        return False
    request_id = payload.get("request_id")
    if request_id is not None and request_id != pending.request_id:
        return False
    response = payload.get("response")
    if not isinstance(response, dict):
        if "choice" not in payload:
            return False
        response = payload
    pending.resolve(_parse_json_response(response, pending.options))
    return True


def _parse_json_response(
    response: dict[str, Any],
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    if response.get("cancelled"):
        return dict(_CANCELLED)
    choice = str(response.get("choice", ""))
    if choice in ("<cancelled>", "<empty_input>"):
        return {"choice": choice}
    if not choice:
        return dict(_EMPTY)
    if response.get("custom"):
        return {"choice": choice, "custom": True}
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(options):
            return {"choice": str(options[index - 1].get("content", ""))}
    for option in options:
        content = str(option.get("content", ""))
        if choice == content:
            return {"choice": content}
    return {"choice": choice, "custom": True}
