"""Global output sink registry.

The agent runtime emits UI events through this module. TUI, simple-terminal and
headless modes install different sinks; runtime code never imports Textual.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from system.tui_types import TuiEvent


@runtime_checkable
class OutputSink(Protocol):
    def emit(self, event: TuiEvent) -> None: ...

    def close(self) -> None: ...


class OutputBus:
    """Thread-safe holder for the active output sink."""

    def __init__(self) -> None:
        self._sink: OutputSink | None = None
        self._lock = threading.RLock()

    def set_sink(self, sink: OutputSink | None) -> None:
        with self._lock:
            self._sink = sink

    def get_sink(self) -> OutputSink | None:
        with self._lock:
            return self._sink

    def clear(self) -> None:
        self.set_sink(None)

    def emit(self, event: TuiEvent) -> bool:
        """Deliver an event to the active sink.

        Returns False when no sink is installed so callers can keep a legacy
        fallback during the migration window.
        """

        sink = self.get_sink()
        if sink is None:
            return False
        sink.emit(event)
        return True


OUTPUT_BUS = OutputBus()
