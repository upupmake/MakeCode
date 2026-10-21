"""prompt_toolkit based input for simple terminal mode."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app, run_in_terminal
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from system.simple_input import (
    _display_serialized,
    _serialize_input,
    is_interactive,
)
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
                    display_meta=self._commands[command],
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

    @bindings.add("enter")
    def _submit(event) -> None:
        event.current_buffer.validate_and_handle()

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
    if not is_interactive():
        try:
            return input(prompt)
        except EOFError:
            return None

    image_entries: list[tuple[str, str]] = []
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
        "Enter 提交 · Ctrl+N 换行 · ↑/↓ 跨行/历史 · Tab 补全"
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
        message=prompt,
        history=InMemoryHistory(history or []),
        multiline=multiline,
        is_password=secret,
        key_bindings=bindings,
        completer=completer,
        complete_while_typing=bool(completion_commands or path_completion),
        reserve_space_for_menu=6,
        prompt_continuation=continuation if multiline else None,
        bottom_toolbar=toolbar,
    )
    if on_session_ready is not None:
        on_session_ready(session)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        in_thread = False
    else:
        in_thread = True
    text = session.prompt(default=default or "", in_thread=in_thread)
    return _serialize_input(text, image_entries)
