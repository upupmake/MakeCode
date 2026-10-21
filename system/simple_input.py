"""纯终端模式的方向键读取与行编辑。"""

import os
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
_IMAGE_DISPLAY_PLACEHOLDER_PATTERN = re.compile(r"\[图片：[^\]\n]+\]")
_BRACKETED_PASTE_START = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"


@dataclass(frozen=True)
class PasteEvent:
    text: str


def is_interactive() -> bool:
    return bool(
        sys.stdin
        and sys.stdout
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def _set_cbreak(fd: int) -> None:
    import termios
    import tty

    tty.setcbreak(fd)
    attributes = termios.tcgetattr(fd)
    attributes[3] &= ~termios.IEXTEN
    termios.tcsetattr(fd, termios.TCSADRAIN, attributes)


def read_key() -> str | PasteEvent:
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


@contextmanager
def raw_mode():
    if not is_interactive() or sys.platform == "win32":
        yield
        return

    fd = sys.stdin.fileno()
    import termios

    old_settings = termios.tcgetattr(fd)
    try:
        _set_cbreak(fd)
        with bracketed_paste():
            yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


@contextmanager
def bracketed_paste():
    if not is_interactive() or sys.platform == "win32":
        yield
        return
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()
    try:
        yield
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def _read_key_posix() -> str | PasteEvent:
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
    if first == b"\x16":
        return "ctrl_v"
    if first == b"\x0e":
        return "ctrl_n"
    if first == b"\x0f":
        return "ctrl_o"
    if first == b"\x10":
        return "ctrl_p"
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


def _read_escape_sequence(fd: int) -> str | PasteEvent:
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
    if sequence == _BRACKETED_PASTE_START:
        return _read_paste_payload(fd)
    return _parse_escape(sequence)


def _read_paste_payload(fd: int) -> PasteEvent:
    import select

    data = bytearray()
    while True:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        data.extend(chunk)
        if data.endswith(_BRACKETED_PASTE_END):
            del data[-len(_BRACKETED_PASTE_END):]
            break
    return PasteEvent(data.decode("utf-8", errors="replace"))


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


def _read_key_windows() -> str | PasteEvent:
    import msvcrt

    character = msvcrt.getwch()
    if character in ("\x00", "\xe0"):
        return _WINDOWS_KEYS.get(msvcrt.getwch(), "unknown")
    if character in ("\r", "\n"):
        return "enter"
    if character == "\x1b":
        return "escape"
    if character in ("\x7f", "\x08"):
        return "backspace"
    if character == "\x03":
        return "ctrl_c"
    if character == "\x04":
        return "ctrl_d"
    if character == "\x16":
        return "ctrl_v"
    if character == "\x0e":
        return "ctrl_n"
    if character == "\x0f":
        return "ctrl_o"
    if character == "\x10":
        return "ctrl_p"
    if character == "\t":
        return "tab"
    return character


def _serialize_input(
        text: str,
        entries: list[tuple[str, str]],
) -> str:
    if not entries:
        return text
    remaining = list(entries)
    result: list[str] = []
    position = 0
    for match in _IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(text):
        result.append(text[position:match.start()])
        for index, (placeholder, reference) in enumerate(remaining):
            if placeholder == match.group(0):
                result.append(reference)
                del remaining[index]
                break
        else:
            result.append(match.group(0))
        position = match.end()
    result.append(text[position:])
    return "".join(result)


def _display_serialized(
        text: str,
        handler: Callable[[str], tuple[str, list[tuple[str, str]]]] | None,
) -> tuple[str, list[tuple[str, str]]]:
    if handler is None:
        return text, []
    try:
        display_text, entries = handler(text)
    except Exception:
        return text, []
    return display_text, list(entries)
