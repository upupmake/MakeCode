"""Cross-platform terminal title helpers."""

import ctypes
import sys


def _safe_terminal_title(title: str) -> str:
    return "".join(character for character in title if ord(character) >= 0x20 and character != "\x7f")


def _write_ansi_terminal_title(title: str) -> None:
    payload = f"\x1b]0;{title}\x07\x1b]2;{title}\x07"
    streams = []
    for name in ("__stderr__", "stderr", "__stdout__", "stdout"):
        stream = getattr(sys, name, None)
        if stream is not None and all(stream is not previous for previous in streams):
            streams.append(stream)

    for stream in streams:
        try:
            stream.write(payload)
            stream.flush()
            return
        except Exception:
            continue


def set_terminal_title(title: str) -> None:
    """Set the title of the terminal hosting the current process."""
    try:
        safe_title = _safe_terminal_title(title)
        if sys.platform == "win32":
            try:
                ctypes.windll.kernel32.SetConsoleTitleW(safe_title)
                return
            except Exception:
                pass
        _write_ansi_terminal_title(safe_title)
    except Exception:
        return
