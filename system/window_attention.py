from __future__ import annotations

import sys


def request_window_attention() -> None:
    if sys.platform != "win32":
        return

    from system.window_attention_win32 import request_window_attention_win32

    request_window_attention_win32()
