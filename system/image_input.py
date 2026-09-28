from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from textual.events import Key, Paste
from textual.widgets import TextArea

from utils.vision import IMAGE_PLACEHOLDER_PATTERN, image_placeholder_text, image_reference_marker


IMAGE_DISPLAY_PLACEHOLDER_PATTERN = re.compile(r"\[图片：[^\]\n]+\]")


class ImageAwareTextArea(TextArea):
    def __init__(
        self,
        text: str = "",
        *,
        image_placeholder_handler: Callable[[str], tuple[str, list[dict[str, Any]]]] | None = None,
        image_clipboard_handler: Callable[[str | None], str | None] | None = None,
        image_error_handler: Callable[[Exception], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self._image_placeholder_handler = image_placeholder_handler
        self._image_clipboard_handler = image_clipboard_handler
        self._image_error_handler = image_error_handler
        display_text, entries = self._display_input_with_image_markers(text)
        self._input_image_markers = entries
        super().__init__(display_text, **kwargs)

    def _image_placeholder_matches(self, text: str):
        return sorted(
            [
                *IMAGE_PLACEHOLDER_PATTERN.finditer(text),
                *IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(text),
            ],
            key=lambda match: match.start(),
        )

    def _display_input_with_image_markers(
        self,
        text: str,
    ) -> tuple[str, list[tuple[str, str]]]:
        if not text or self._image_placeholder_handler is None:
            return text, []
        try:
            display_text, parts = self._image_placeholder_handler(text)
        except ValueError:
            return text, []
        entries = [
            (image_placeholder_text(part), image_reference_marker(part))
            for part in parts
            if part.get("type") == "image"
        ]
        return display_text, entries

    def load_serialized_text(self, text: str) -> None:
        display_text, entries = self._display_input_with_image_markers(text)
        self._input_image_markers = entries
        self.load_text(display_text)

    def remove_input_image_marker(self, text: str, start: int, display_text: str) -> None:
        if IMAGE_DISPLAY_PLACEHOLDER_PATTERN.fullmatch(display_text) is None:
            return
        image_index = sum(
            1 for match in IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(text[:start])
        )
        if (
            image_index < len(self._input_image_markers)
            and self._input_image_markers[image_index][0] == display_text
        ):
            del self._input_image_markers[image_index]

    def reconcile_input_image_markers(self, text: str) -> None:
        remaining = list(self._input_image_markers)
        reconciled = []
        for match in IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(text):
            for index, entry in enumerate(remaining):
                if entry[0] == match.group(0):
                    reconciled.append(entry)
                    del remaining[index]
                    break
        self._input_image_markers = reconciled

    def serialize_text(self, text: str | None = None) -> str:
        text = self.text if text is None else text
        self.reconcile_input_image_markers(text)
        if not self._input_image_markers:
            return text
        serialized = []
        position = 0
        image_index = 0
        for match in IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(text):
            serialized.append(text[position:match.start()])
            if (
                image_index < len(self._input_image_markers)
                and self._input_image_markers[image_index][0] == match.group(0)
            ):
                serialized.append(self._input_image_markers[image_index][1])
                image_index += 1
            else:
                serialized.append(match.group(0))
            position = match.end()
        serialized.append(text[position:])
        return "".join(serialized)

    def display_text(self, text: str) -> str:
        display_text, _ = self._display_input_with_image_markers(text)
        return display_text

    def _delete_image_placeholder(self, event: Key) -> bool:
        if event.key not in {"backspace", "delete"}:
            return False
        row, column = self.cursor_location
        cursor_index = self.document.get_index_from_location((row, column))
        if event.key == "backspace":
            match = next(
                (
                    candidate
                    for candidate in self._image_placeholder_matches(self.text)
                    if candidate.start() < cursor_index <= candidate.end()
                ),
                None,
            )
        else:
            match = next(
                (
                    candidate
                    for candidate in self._image_placeholder_matches(self.text)
                    if candidate.start() <= cursor_index < candidate.end()
                ),
                None,
            )
        if match is None:
            return False
        if match.re is IMAGE_DISPLAY_PLACEHOLDER_PATTERN:
            self.remove_input_image_marker(self.text, match.start(), match.group(0))
        start = self.document.get_location_from_index(match.start())
        end = self.document.get_location_from_index(match.end())
        self.document.replace_range(start, end, "")
        self.cursor_location = start
        event.stop()
        event.prevent_default()
        return True

    def _navigate_image_placeholder(self, event: Key) -> bool:
        if event.key not in {"left", "right"} or self.selection.start != self.selection.end:
            return False
        row, column = self.cursor_location
        cursor_index = self.document.get_index_from_location((row, column))
        for match in self._image_placeholder_matches(self.text):
            if event.key == "left" and match.start() < cursor_index <= match.end():
                target_index = match.start()
            elif event.key == "right" and match.start() <= cursor_index < match.end():
                target_index = match.end()
            else:
                continue
            self.cursor_location = self.document.get_location_from_index(target_index)
            event.stop()
            event.prevent_default()
            return True
        return False

    def _insert_display_text(
        self,
        display_text: str,
        entries: list[tuple[str, str]],
    ) -> None:
        cursor_index = self.document.get_index_from_location(self.cursor_location)
        image_index = sum(
            1 for match in IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(self.text[:cursor_index])
        )
        for placeholder, reference in entries:
            self._input_image_markers.insert(image_index, (placeholder, reference))
            image_index += 1
        self.insert(display_text)

    def _insert_pasted_text(self, text: str) -> None:
        display_text, entries = self._display_input_with_image_markers(text)
        if entries:
            self._insert_display_text(display_text, entries)
        else:
            self.insert(text)

    def paste_image_from_system_clipboard(self, paste_text: str | None = None) -> bool:
        if self._image_clipboard_handler is None:
            return False
        try:
            marker = self._image_clipboard_handler(paste_text)
        except TypeError:
            marker = self._image_clipboard_handler()
        except ValueError as exc:
            if self._image_error_handler is not None:
                self._image_error_handler(exc)
            return False
        if marker is None:
            return False
        if marker == "":
            return True
        display_text, entries = self._display_input_with_image_markers(marker)
        self._insert_display_text(display_text, entries)
        return True

    def on_paste(self, event: Paste) -> None:
        if self.paste_image_from_system_clipboard(event.text):
            event.stop()
            event.prevent_default()
            return
        self._insert_pasted_text(event.text)
        event.stop()
        event.prevent_default()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.reconcile_input_image_markers(self.text)

    async def _on_key(self, event: Key) -> None:
        if self._delete_image_placeholder(event) or self._navigate_image_placeholder(event):
            return
        await super()._on_key(event)
