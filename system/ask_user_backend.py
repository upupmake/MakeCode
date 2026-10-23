"""Pluggable AskUser backend.

TUI mode uses the existing interactive choice panel. CLI modes install a
backend so the tool can ask through stdin/stdout without importing Textual.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol


class AskUserBackend(Protocol):
    def ask(self, question: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the same JSON-ready payload as the TUI AskUser handler."""
        ...


class AskUserRegistry:
    """Thread-safe holder for the active AskUser backend."""

    def __init__(self) -> None:
        self._backend: AskUserBackend | None = None
        self._lock = threading.RLock()

    def set_backend(self, backend: AskUserBackend | None) -> None:
        with self._lock:
            self._backend = backend

    def get_backend(self) -> AskUserBackend | None:
        with self._lock:
            return self._backend


ASK_USER = AskUserRegistry()
