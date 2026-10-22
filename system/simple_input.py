"""纯终端模式的图片占位符序列化与终端检测。"""

import re
import sys
from collections.abc import Callable

_IMAGE_DISPLAY_PLACEHOLDER_PATTERN = re.compile(r"\[图片：[^\]\n]+\]")


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
    # Keep Ctrl+C as SIGINT while still allowing single-key reads for Ctrl+G.
    attributes[3] |= termios.ISIG
    termios.tcsetattr(fd, termios.TCSADRAIN, attributes)


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
