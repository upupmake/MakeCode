"""prompt_toolkit based input for simple terminal mode."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app, run_in_terminal
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from system.simple_input import (
    _IMAGE_DISPLAY_PLACEHOLDER_PATTERN,
    _display_serialized,
    _serialize_input,
)
from system import simple_terminal
from utils import paths


class SlashCompleter(Completer):
    def __init__(self, commands: dict[str, str] | None) -> None:
        self._commands = dict(commands or {})

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text:
            return
        if text.startswith("/cd "):
            fragment = text[4:]
            for candidate in paths.directory_completion_candidates(
                fragment,
                paths.workdir(),
            ):
                yield Completion(
                    candidate,
                    start_position=-len(fragment),
                    display=candidate,
                    display_meta="目录",
                )
            return
        if not text.startswith("/") or " " in text:
            return
        for command in sorted(self._commands):
            if command.startswith(text):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=simple_terminal.sanitize_icons(
                        self._commands[command]
                    ),
                )


def _build_key_bindings(
        *,
        multiline: bool,
        image_handler: Callable[[], str | None] | None,
        image_display_handler: Callable[
            [str], tuple[str, list[tuple[str, str]]]
        ] | None,
        paste_handler: Callable[[str], str | None] | None,
        ctrl_p_handler: Callable[[], str | None] | None,
        cancel_on_escape: bool,
        image_entries: list[tuple[str, str]],
) -> KeyBindings:
    bindings = KeyBindings()

    def placeholder_before_cursor() -> int:
        line = get_app().current_buffer.document.current_line_before_cursor
        for match in _IMAGE_DISPLAY_PLACEHOLDER_PATTERN.finditer(line):
            if match.end() == len(line):
                return len(match.group(0))
        return 0

    def placeholder_after_cursor() -> int:
        line = get_app().current_buffer.document.current_line_after_cursor
        match = _IMAGE_DISPLAY_PLACEHOLDER_PATTERN.match(line)
        return len(match.group(0)) if match else 0

    @bindings.add(
        "backspace",
        filter=Condition(lambda: placeholder_before_cursor() > 0),
    )
    def _delete_placeholder_before(event) -> None:
        event.current_buffer.delete_before_cursor(placeholder_before_cursor())

    @bindings.add(
        "delete",
        filter=Condition(lambda: placeholder_after_cursor() > 0),
    )
    def _delete_placeholder_after(event) -> None:
        event.current_buffer.delete(placeholder_after_cursor())

    @bindings.add(
        "left",
        filter=Condition(lambda: placeholder_before_cursor() > 0),
    )
    def _skip_placeholder_left(event) -> None:
        event.current_buffer.cursor_position -= placeholder_before_cursor()

    @bindings.add(
        "right",
        filter=Condition(lambda: placeholder_after_cursor() > 0),
    )
    def _skip_placeholder_right(event) -> None:
        event.current_buffer.cursor_position += placeholder_after_cursor()

    @bindings.add("enter", filter=~has_completions)
    def _submit(event) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("enter", filter=has_completions)
    def _complete_first(event) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            buffer.validate_and_handle()
            return
        completion = state.current_completion or state.completions[0]
        if (
            completion.text == buffer.text
            and completion.start_position == -len(buffer.text)
        ):
            buffer.validate_and_handle()
            return
        buffer.apply_completion(completion)

    if multiline:
        @bindings.add("c-n")
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

    @Condition
    def at_first_line() -> bool:
        return get_app().current_buffer.document.cursor_position_row == 0

    @Condition
    def at_last_line() -> bool:
        document = get_app().current_buffer.document
        return document.cursor_position_row == document.line_count - 1

    @bindings.add("up", filter=at_first_line)
    def _history_previous(event) -> None:
        event.current_buffer.history_backward()

    @bindings.add("down", filter=at_last_line)
    def _history_next(event) -> None:
        event.current_buffer.history_forward()

    def insert_display(event, serialized: str) -> None:
        display_text, entries = _display_serialized(
            serialized,
            image_display_handler,
        )
        event.current_buffer.insert_text(display_text)
        image_entries.extend(entries)

    def show_message(message: str) -> None:
        try:
            run_in_terminal(lambda: print(message))
        except Exception:
            pass

    if image_handler is not None:
        @bindings.add("c-v")
        @bindings.add("c-o")
        def _attach_image(event) -> None:
            try:
                serialized = image_handler()
            except Exception as exc:
                show_message(f"附加图片失败：{exc}")
                return
            if not serialized:
                show_message("剪贴板中没有可附加的图片或图片文件。")
                return
            insert_display(event, serialized)

    @bindings.add(Keys.BracketedPaste)
    def _paste(event) -> None:
        payload = event.data
        handled = None
        if paste_handler is not None:
            try:
                handled = paste_handler(payload)
            except Exception as exc:
                show_message(f"粘贴处理失败：{exc}")
                return
        if handled == "":
            return
        insert_display(event, handled if handled is not None else payload)

    if ctrl_p_handler is not None:
        @bindings.add("c-p")
        def _toggle_plan(event) -> None:
            try:
                message = ctrl_p_handler()
            except Exception as exc:
                show_message(f"切换 Plan Mode 失败：{exc}")
                return
            if message:
                show_message(message)

    if cancel_on_escape:
        @bindings.add("escape")
        def _cancel(event) -> None:
            event.app.exit(exception=KeyboardInterrupt)

    return bindings


def read_prompt(
        prompt: str,
        *,
        default: str | None = None,
        history: list[str] | None = None,
        multiline: bool = True,
        secret: bool = False,
        continuation_prompt: str = "... ",
        completion_commands: dict[str, str] | None = None,
        path_completion: bool = False,
        image_handler: Callable[[], str | None] | None = None,
        image_display_handler: Callable[
            [str], tuple[str, list[tuple[str, str]]]
        ] | None = None,
        paste_handler: Callable[[str], str | None] | None = None,
        ctrl_p_handler: Callable[[], str | None] | None = None,
        cancel_on_escape: bool = False,
        on_session_ready: Callable[[PromptSession], None] | None = None,
) -> str | None:
    if not simple_terminal.supports_prompt_toolkit():
        try:
            return input(prompt)
        except EOFError:
            return None

    simple_terminal.prepare_prompt_toolkit()
    image_entries: list[tuple[str, str]] = []
    try:
        bindings = _build_key_bindings(
            multiline=multiline,
            image_handler=image_handler,
            image_display_handler=image_display_handler,
            paste_handler=paste_handler,
            ctrl_p_handler=ctrl_p_handler,
            cancel_on_escape=cancel_on_escape,
            image_entries=image_entries,
        )

        def continuation(width: int, line_number: int, is_soft_wrap: bool) -> str:
            if is_soft_wrap:
                return " " * width
            return " " * width + continuation_prompt

        toolbar = (
            "Enter 提交 · Ctrl+N 换行 · "
            f"{simple_terminal.arrows_label()} 跨行/历史 · Tab 补全"
            if multiline
            else "Enter 提交 · Tab 补全"
        )
        if path_completion:
            completer = PathCompleter(
                expanduser=True,
                only_directories=True,
                get_paths=lambda: [str(paths.workdir())],
            )
        else:
            completer = SlashCompleter(completion_commands)

        session = PromptSession(
            message=simple_terminal.sanitize_icons(prompt),
            history=InMemoryHistory(history or []),
            multiline=multiline,
            is_password=secret,
            key_bindings=bindings,
            completer=completer,
            complete_while_typing=(
                bool(completion_commands or path_completion)
                and simple_terminal.live_completion()
            ),
            reserve_space_for_menu=(
                6 if simple_terminal.profile_is_full() else 0
            ),
            prompt_continuation=continuation if multiline else None,
            bottom_toolbar=(
                toolbar if simple_terminal.show_bottom_toolbar() else None
            ),
        )
        if on_session_ready is not None:
            on_session_ready(session)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            in_thread = False
        else:
            in_thread = True
        text = session.prompt(
            default=default or "",
            in_thread=in_thread,
            color_depth=simple_terminal.prompt_color_depth(),
        )
        return _serialize_input(text, image_entries)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        try:
            return input(prompt)
        except EOFError:
            return None
