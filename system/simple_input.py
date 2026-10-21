"""纯终端模式的方向键读取与行编辑。"""

import os
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len


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


def read_line(
        prompt: str,
        *,
        default: str | None = None,
        history: list[str] | None = None,
        secret: bool = False,
        multiline: bool = False,
        continuation_prompt: str = "... ",
        image_handler: Callable[[], str | None] | None = None,
        image_display_handler: Callable[
            [str], tuple[str, list[tuple[str, str]]]
        ] | None = None,
        paste_handler: Callable[[str], str | None] | None = None,
        input_redraw_handler: Callable[
            [Callable[[], None] | None], None
        ] | None = None,
        input_clear_handler: Callable[
            [Callable[[], None] | None], None
        ] | None = None,
        input_lock: Any = None,
        completion_handler: Callable[[str], str | None] | None = None,
        ctrl_p_handler: Callable[[], str | None] | None = None,
) -> str | None:
    if not is_interactive():
        try:
            return input(prompt)
        except EOFError:
            return None

    try:
        if sys.platform == "win32":
            return _edit_line(
                prompt,
                default,
                history,
                secret,
                multiline,
                continuation_prompt,
                image_handler,
                image_display_handler,
                paste_handler,
                input_redraw_handler,
                input_clear_handler,
                input_lock,
                completion_handler,
                ctrl_p_handler,
            )

        fd = sys.stdin.fileno()
        import termios

        old_settings = termios.tcgetattr(fd)
        try:
            _set_cbreak(fd)
            with bracketed_paste():
                return _edit_line(
                    prompt,
                    default,
                    history,
                    secret,
                    multiline,
                    continuation_prompt,
                    image_handler,
                    image_display_handler,
                    paste_handler,
                    input_redraw_handler,
                    input_clear_handler,
                    input_lock,
                    completion_handler,
                    ctrl_p_handler,
                )
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    finally:
        if input_redraw_handler is not None:
            input_redraw_handler(None)
        if input_clear_handler is not None:
            input_clear_handler(None)


def _edit_line(
        prompt: str,
        default: str | None,
        history: list[str] | None,
        secret: bool,
        multiline: bool,
        continuation_prompt: str,
        image_handler: Callable[[], str | None] | None,
        image_display_handler: Callable[
            [str], tuple[str, list[tuple[str, str]]]
        ] | None,
        paste_handler: Callable[[str], str | None] | None,
        input_redraw_handler: Callable[
            [Callable[[], None] | None], None
        ] | None,
        input_clear_handler: Callable[
            [Callable[[], None] | None], None
        ] | None,
        input_lock: Any,
        completion_handler: Callable[[str], str | None] | None,
        ctrl_p_handler: Callable[[], str | None] | None,
) -> str | None:
    buffer = list(default or "")
    committed: list[str] = []
    image_entries: list[tuple[str, str]] = []
    cursor = len(buffer)
    history = list(history or [])
    history_index = len(history)
    draft = ""
    first_prompt = prompt.lstrip("\r\n")
    current_prompt = first_prompt

    def masked(text: str) -> str:
        return "*" * len(text) if secret else text

    def write(text: str) -> None:
        if input_lock is None:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        with input_lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def redraw() -> None:
        text = masked("".join(buffer))
        prefix = masked("".join(buffer[:cursor]))
        back = cell_len(text) - cell_len(prefix)
        output = "\r\x1b[2K" + current_prompt + text
        if back > 0:
            output += f"\x1b[{back}D"
        write(output)

    def clear_input() -> None:
        if committed:
            write(f"\r\x1b[{len(committed)}A\r\x1b[J")
        else:
            write("\r\x1b[J")

    def redraw_all() -> None:
        if not committed:
            text = masked("".join(buffer))
            suffix = masked("".join(buffer[cursor:]))
            output = first_prompt + text
            if suffix:
                output += f"\x1b[{cell_len(suffix)}D"
            write(output)
            return

        output = [first_prompt + masked(committed[0])]
        for line in committed[1:]:
            output.append("\n" + continuation_prompt + masked(line))
        output.append(
            "\n" + continuation_prompt + masked("".join(buffer))
        )
        suffix = masked("".join(buffer[cursor:]))
        if suffix:
            output.append(f"\x1b[{cell_len(suffix)}D")
        write("".join(output))

    def full_text() -> str:
        return "\n".join(committed + ["".join(buffer)])

    def serialize() -> str:
        return _serialize_input(full_text(), image_entries)

    def notice(message: str) -> None:
        clear_input()
        write(message + "\n")
        redraw_all()

    def insert_display(
            display_text: str,
            entries: list[tuple[str, str]],
    ) -> None:
        nonlocal cursor
        display_text = (
            display_text.replace("\r\n", "\n").replace("\r", "\n")
        )
        if "\n" in display_text:
            display_text = display_text.replace("\n", " ")
        current = "".join(buffer)
        image_index = len(
            _IMAGE_DISPLAY_PLACEHOLDER_PATTERN.findall(current[:cursor])
        )
        buffer[cursor:cursor] = list(display_text)
        cursor += len(display_text)
        for offset, entry in enumerate(entries):
            image_entries.insert(image_index + offset, entry)
        redraw()

    def insert_text(text: str) -> None:
        nonlocal buffer, cursor, current_prompt
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not multiline:
            insert_display(text.replace("\n", " "), [])
            return
        if "\n" not in text:
            insert_display(text, [])
            return
        combined = "".join(buffer) + text
        lines = combined.split("\n")
        buffer = list(lines[0])
        cursor = len(buffer)
        redraw()
        committed.extend(lines[:-1])
        buffer = list(lines[-1])
        cursor = len(buffer)
        current_prompt = continuation_prompt
        output = ["\n"]
        for line in lines[1:-1]:
            output.append(masked(line) + "\n")
        output.append(current_prompt + masked("".join(buffer)))
        write("".join(output))

    def insert_paste(payload: str) -> None:
        handled = None
        if paste_handler is not None:
            try:
                handled = paste_handler(payload)
            except Exception as exc:
                notice(f"粘贴处理失败：{exc}")
                return
        if handled == "":
            return
        if handled:
            display_text, entries = _display_serialized(
                handled, image_display_handler
            )
            insert_display(display_text, entries)
            return
        if image_display_handler is not None:
            display_text, entries = _display_serialized(
                payload, image_display_handler
            )
            if entries:
                insert_display(display_text, entries)
                return
        insert_text(payload)

    def load_serialized(serialized: str) -> None:
        nonlocal buffer, cursor, committed, image_entries, current_prompt
        display_text, entries = _display_serialized(
            serialized, image_display_handler
        )
        display_text = (
            display_text.replace("\r\n", "\n").replace("\r", "\n")
        )
        lines = display_text.split("\n")
        clear_input()
        committed = lines[:-1]
        buffer = list(lines[-1])
        cursor = len(buffer)
        image_entries = list(entries)
        current_prompt = continuation_prompt if committed else first_prompt
        output = []
        if committed:
            output.append(first_prompt + masked(lines[0]))
            for line in lines[1:-1]:
                output.append(
                    "\n" + continuation_prompt + masked(line)
                )
            output.append(
                "\n" + continuation_prompt + masked(lines[-1])
            )
        else:
            output.append(first_prompt + masked(lines[0]))
        write("".join(output))

    initial = masked("".join(buffer))
    if input_lock is None:
        sys.stdout.write(prompt + initial)
        sys.stdout.flush()
        if input_redraw_handler is not None:
            input_redraw_handler(redraw_all)
        if input_clear_handler is not None:
            input_clear_handler(clear_input)
    else:
        with input_lock:
            sys.stdout.write(prompt + initial)
            sys.stdout.flush()
            if input_redraw_handler is not None:
                input_redraw_handler(redraw_all)
            if input_clear_handler is not None:
                input_clear_handler(clear_input)
    while True:
        key = read_key()

        if isinstance(key, PasteEvent):
            insert_paste(key.text)
            continue

        if key == "enter":
            write("\n")
            return serialize()
        if key == "ctrl_c":
            write("\n")
            raise KeyboardInterrupt
        if key == "ctrl_d":
            if not buffer and not committed:
                write("\n")
                return None
            continue
        if key == "eof":
            write("\n")
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
            elif committed:
                previous = committed.pop()
                buffer = list(previous) + buffer
                cursor = len(previous)
                current_prompt = (
                    continuation_prompt if committed else first_prompt
                )
                suffix = masked("".join(buffer[cursor:]))
                output = (
                    "\r\x1b[1A\r\x1b[J"
                    + current_prompt
                    + masked("".join(buffer))
                )
                if suffix:
                    output += f"\x1b[{cell_len(suffix)}D"
                write(output)
            continue
        if key == "delete":
            if cursor < len(buffer):
                buffer.pop(cursor)
                redraw()
            continue
        if key == "ctrl_n":
            if multiline:
                committed.append("".join(buffer))
                buffer = []
                cursor = 0
                current_prompt = continuation_prompt
                write("\n" + current_prompt)
            continue
        if key in {"ctrl_v", "ctrl_o"}:
            if image_handler is not None:
                try:
                    serialized = image_handler()
                except Exception as exc:
                    notice(f"附加图片失败：{exc}")
                else:
                    if serialized:
                        display_text, entries = _display_serialized(
                            serialized, image_display_handler
                        )
                        insert_display(display_text, entries)
                    else:
                        notice("剪贴板中没有可附加的图片或图片文件。")
            continue
        if key == "up":
            if history:
                if history_index == len(history):
                    draft = serialize()
                if history_index > 0:
                    history_index -= 1
                    load_serialized(history[history_index])
            continue
        if key == "down":
            if history:
                if history_index < len(history) - 1:
                    history_index += 1
                    load_serialized(history[history_index])
                elif history_index == len(history) - 1:
                    history_index = len(history)
                    load_serialized(draft)
            continue
        if key == "tab":
            if completion_handler is not None:
                try:
                    replacement = completion_handler("".join(buffer))
                except Exception:
                    replacement = None
                if replacement is not None and replacement != "".join(buffer):
                    buffer = list(replacement)
                    cursor = len(buffer)
                    redraw()
            continue
        if key == "ctrl_p":
            if ctrl_p_handler is not None:
                try:
                    message = ctrl_p_handler()
                except Exception as exc:
                    notice(f"切换 Plan Mode 失败：{exc}")
                else:
                    if message:
                        notice(message)
            continue
        if key in {"escape", "page_up", "page_down", "unknown"}:
            continue

        if len(key) == 1 and key.isprintable():
            buffer.insert(cursor, key)
            cursor += 1
            redraw()


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
