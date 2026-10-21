"""纯终端模式的方向键读取与行编辑。"""

import os
import sys
from contextlib import contextmanager

from rich.cells import cell_len


def is_interactive() -> bool:
    return bool(
        sys.stdin
        and sys.stdout
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def read_key() -> str:
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


@contextmanager
def raw_mode():
    if not is_interactive() or sys.platform == "win32":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_key_posix() -> str:
    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    if not first:
        return "eof"
    if first == b"\x1b":
        return _read_escape_sequence(fd)
    if first in (b"\r", b"\n"):
        return "enter"
    if first in (b"\x7f", b"\x08"):
        return "backspace"
    if first == b"\x03":
        return "ctrl_c"
    if first == b"\x04":
        return "ctrl_d"
    if first == b"\t":
        return "tab"

    if first[0] < 0x80:
        return first.decode("ascii", errors="replace")

    length = _utf8_length(first[0])
    data = first
    for _ in range(length - 1):
        data += os.read(fd, 1)
    return data.decode("utf-8", errors="replace")


def _utf8_length(first_byte: int) -> int:
    if 0xC0 <= first_byte <= 0xDF:
        return 2
    if 0xE0 <= first_byte <= 0xEF:
        return 3
    if 0xF0 <= first_byte <= 0xF7:
        return 4
    return 1


def _read_escape_sequence(fd: int) -> str:
    import select

    sequence = b"\x1b"
    while True:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            break
        next_byte = os.read(fd, 1)
        if not next_byte:
            break
        sequence += next_byte
        if _escape_complete(sequence):
            break
    return _parse_escape(sequence)


def _escape_complete(sequence: bytes) -> bool:
    if len(sequence) < 2:
        return False
    if sequence[1:2] not in (b"[", b"O"):
        return True
    if len(sequence) == 2:
        return False
    last = sequence[-1:]
    return last.isalpha() or last == b"~"


def _parse_escape(sequence: bytes) -> str:
    if sequence == b"\x1b":
        return "escape"
    text = sequence.decode("ascii", errors="ignore")
    final = text[-1:] if text else ""
    if final in {"A", "B", "C", "D"}:
        return {
            "A": "up",
            "B": "down",
            "C": "right",
            "D": "left",
        }[final]
    if final == "H":
        return "home"
    if final == "F":
        return "end"
    if final == "Z":
        return "tab"
    if text.endswith("~"):
        code = text[2:-1].split(";", 1)[0]
        if code == "1":
            return "home"
        if code in {"4", "8"}:
            return "end"
        if code == "3":
            return "delete"
        if code == "5":
            return "page_up"
        if code == "6":
            return "page_down"
    return "escape"


_WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "S": "delete",
    "I": "page_up",
    "Q": "page_down",
}


def _read_key_windows() -> str:
    import msvcrt

    character = msvcrt.getwch()
    if character in ("\x00", "\xe0"):
        return _WINDOWS_KEYS.get(msvcrt.getwch(), "unknown")
    if character in ("\r", "\n"):
        return "enter"
    if character in ("\x7f", "\x08"):
        return "backspace"
    if character == "\x03":
        return "ctrl_c"
    if character == "\x04":
        return "ctrl_d"
    if character == "\t":
        return "tab"
    return character


def read_line(
        prompt: str,
        *,
        default: str | None = None,
        history: list[str] | None = None,
        secret: bool = False,
) -> str | None:
    if not is_interactive():
        try:
            return input(prompt)
        except EOFError:
            return None

    if sys.platform == "win32":
        return _edit_line(prompt, default, history, secret)

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return _edit_line(prompt, default, history, secret)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _edit_line(
        prompt: str,
        default: str | None,
        history: list[str] | None,
        secret: bool,
) -> str | None:
    buffer = list(default or "")
    cursor = len(buffer)
    history = list(history or [])
    history_index = len(history)
    draft = ""

    def redraw() -> None:
        text = "*" * len(buffer) if secret else "".join(buffer)
        prefix = "*" * cursor if secret else "".join(buffer[:cursor])
        back = cell_len(text) - cell_len(prefix)
        sys.stdout.write("\r\x1b[2K" + prompt + text)
        if back > 0:
            sys.stdout.write(f"\x1b[{back}D")
        sys.stdout.flush()

    initial = "*" * len(buffer) if secret else "".join(buffer)
    sys.stdout.write(prompt + initial)
    sys.stdout.flush()
    while True:
        key = read_key()

        if key == "enter":
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(buffer)
        if key == "ctrl_c":
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt
        if key == "ctrl_d":
            if not buffer:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None
            continue
        if key == "eof":
            sys.stdout.write("\n")
            sys.stdout.flush()
            return None

        if key == "left":
            if cursor > 0:
                cursor -= 1
                redraw()
            continue
        if key == "right":
            if cursor < len(buffer):
                cursor += 1
                redraw()
            continue
        if key == "home":
            cursor = 0
            redraw()
            continue
        if key == "end":
            cursor = len(buffer)
            redraw()
            continue
        if key == "backspace":
            if cursor > 0:
                buffer.pop(cursor - 1)
                cursor -= 1
                redraw()
            continue
        if key == "delete":
            if cursor < len(buffer):
                buffer.pop(cursor)
                redraw()
            continue
        if key == "up":
            if history:
                if history_index == len(history):
                    draft = "".join(buffer)
                if history_index > 0:
                    history_index -= 1
                    buffer = list(history[history_index])
                    cursor = len(buffer)
                    redraw()
            continue
        if key == "down":
            if history:
                if history_index < len(history) - 1:
                    history_index += 1
                    buffer = list(history[history_index])
                elif history_index == len(history) - 1:
                    history_index = len(history)
                    buffer = list(draft)
                cursor = len(buffer)
                redraw()
            continue
        if key in {"escape", "tab", "page_up", "page_down", "unknown"}:
            continue

        if len(key) == 1 and key.isprintable():
            buffer.insert(cursor, key)
            cursor += 1
            redraw()
