"""Helpers for rendering untrusted terminal text safely."""


def sanitize_control_characters(value: str) -> str:
    """Render control characters visibly instead of letting them corrupt layout."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    visible: list[str] = []
    for character in text:
        code = ord(character)
        if character == "\n":
            visible.append("\n")
        elif character == "\t":
            visible.append("    ")
        elif code < 0x20 or 0x7F <= code < 0xA0:
            visible.append(f"\\x{code:02x}")
        else:
            visible.append(character)
    return "".join(visible)
