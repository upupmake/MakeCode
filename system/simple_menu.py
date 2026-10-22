"""prompt_toolkit based selection menu for simple terminal mode."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Dimension,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from system import simple_terminal

_STYLE = Style.from_dict({
    "title": "bold",
    "selected": "reverse",
    "dim": "ansibrightblack",
    "status": "ansibrightblack",
    "error": "ansiyellow",
    "hint": "ansibrightblack",
})


class ChoiceMenu:
    def __init__(
            self,
            title: str,
            options: list[str],
            *,
            search_texts: list[str] | None = None,
            delete_handler: Callable[[str], None] | None = None,
            preview_handler: Callable[[str], tuple[Any, Any]] | None = None,
            preview_callback: Callable[[Any, Any], None] | None = None,
    ) -> None:
        self.title = title
        self.options = list(options)
        self.search_texts = (
            list(search_texts) if search_texts is not None else None
        )
        self.searchable = search_texts is not None
        self.delete_handler = delete_handler
        self.preview_handler = preview_handler
        self.preview_callback = preview_callback
        self.visible = list(range(len(self.options)))
        self.selected = 0
        self.offset = 0
        self.message = ""
        self.confirm_index: int | None = None
        self.rows = max(3, simple_terminal.menu_rows())
        self.input_area: TextArea | None = None
        self._app: Application | None = None

    def run(
            self,
            *,
            input: Any = None,
            output: Any = None,
    ) -> str:
        simple_terminal.prepare_prompt_toolkit()
        app = self._build_app(input=input, output=output)
        self._app = app
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                in_thread = False
            else:
                in_thread = True
            result = app.run(in_thread=in_thread, handle_sigint=False)
            return result if result is not None else "<cancelled>"
        except (EOFError, KeyboardInterrupt):
            return "<cancelled>"
        finally:
            self._app = None

    def _build_app(self, *, input: Any, output: Any) -> Application:
        self.input_area = TextArea(
            prompt="> ",
            multiline=False,
            height=1,
            accept_handler=self._accept,
            style="class:input",
        )
        confirming = Condition(lambda: self.confirm_index is not None)
        empty_input = Condition(lambda: not self.input_area.text)

        title_window = Window(
            FormattedTextControl(
                lambda: [
                    (
                        "class:title",
                        simple_terminal.sanitize_icons(self.title),
                    )
                ],
            ),
            height=Dimension(min=1, max=3),
            wrap_lines=True,
            dont_extend_height=True,
        )
        list_window = Window(
            FormattedTextControl(self._render_list),
            height=Dimension(min=1, max=self.rows + 1),
            wrap_lines=True,
            dont_extend_height=True,
        )
        status_window = Window(
            FormattedTextControl(self._render_status),
            height=1,
            wrap_lines=False,
        )
        hint_window = Window(
            FormattedTextControl(self._render_hint),
            height=1,
            wrap_lines=False,
        )
        layout = Layout(
            HSplit([
                title_window,
                list_window,
                status_window,
                self.input_area,
                hint_window,
            ]),
            focused_element=self.input_area,
        )

        bindings = KeyBindings()

        @bindings.add("up")
        def _up(event) -> None:
            self._move(-1)

        @bindings.add("down")
        def _down(event) -> None:
            self._move(1)

        @bindings.add("left", filter=empty_input)
        def _left(event) -> None:
            self._move(-1)

        @bindings.add("right", filter=empty_input)
        def _right(event) -> None:
            self._move(1)

        @bindings.add("pageup")
        def _page_up(event) -> None:
            self._move(-self.rows)

        @bindings.add("pagedown")
        def _page_down(event) -> None:
            self._move(self.rows)

        @bindings.add("c-c")
        def _cancel(event) -> None:
            self._cancel()

        @bindings.add("escape", filter=~confirming)
        def _escape(event) -> None:
            self._cancel()

        @bindings.add("c-d", filter=empty_input)
        def _eof(event) -> None:
            self._cancel()

        @bindings.add("enter", filter=confirming)
        def _ignore_enter_while_confirming(event) -> None:
            pass

        @bindings.add("y", filter=confirming)
        def _confirm_delete(event) -> None:
            self._finish_delete(True)

        @bindings.add("Y", filter=confirming)
        def _confirm_delete_upper(event) -> None:
            self._finish_delete(True)

        @bindings.add("n", filter=confirming)
        def _cancel_delete(event) -> None:
            self._finish_delete(False)

        @bindings.add("N", filter=confirming)
        def _cancel_delete_upper(event) -> None:
            self._finish_delete(False)

        @bindings.add("escape", filter=confirming)
        def _escape_delete(event) -> None:
            self._finish_delete(False)

        return Application(
            layout=layout,
            key_bindings=bindings,
            style=_STYLE,
            full_screen=False,
            color_depth=simple_terminal.prompt_color_depth(),
            input=input,
            output=output,
        )

    def _render_list(self):
        if not self.visible:
            return [("class:dim", "  （没有匹配项）")]
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + self.rows:
            self.offset = self.selected - self.rows + 1
        end = min(len(self.visible), self.offset + self.rows)
        lines: list[tuple[str, str]] = []
        if self.offset > 0:
            up = "^" if simple_terminal.ascii_ui() else "↑"
            lines.append((
                "class:dim",
                f"  {up} 还有更多\n",
            ))
        for position in range(self.offset, end):
            index = self.visible[position]
            style = "class:selected" if position == self.selected else ""
            marker = (
                simple_terminal.selected_marker()
                if position == self.selected
                else "  "
            )
            lines.append((
                style,
                f"{marker}{position + 1}. "
                f"{simple_terminal.sanitize_icons(self.options[index])}",
            ))
            if position != end - 1:
                lines.append(("", "\n"))
        if end < len(self.visible):
            down = "v" if simple_terminal.ascii_ui() else "↓"
            lines.append(("class:dim", f"\n  {down} 还有更多"))
        return lines

    def _render_status(self):
        if self.confirm_index is not None:
            return [("class:error", "确认删除？[y/N]")]
        if self.message:
            return [(
                "class:error",
                simple_terminal.sanitize_icons(f"⚠ {self.message}"),
            )]
        if not self.visible:
            return [("class:dim", "没有匹配项")]
        index = self.visible[self.selected]
        return [(
            "class:status",
            f"选中 {self.selected + 1}/{len(self.visible)}: "
            f"{simple_terminal.sanitize_icons(self.options[index])}",
        )]

    def _render_hint(self):
        if self.searchable:
            text = (
                f"{simple_terminal.arrows_label()} 选择 · 数字+Enter · /关键词 · "
                "p N 预览 · d N 删除 · q 取消"
            )
        else:
            text = (
                f"{simple_terminal.arrows_label()} 选择 · "
                "数字+Enter · q 取消"
            )
        return [("class:hint", text)]

    def _accept(self, buffer) -> None:
        if self.confirm_index is not None:
            return
        command = buffer.text.strip()
        buffer.reset()
        if not command:
            self._select_current()
            return

        lowered = command.casefold()
        if lowered in {"q", "quit", "exit"}:
            self._cancel()
            return
        if command.isdigit():
            index = self._resolve_position(command)
            if index is None:
                self.message = "无效序号"
            else:
                self._exit(result=self.options[index])
                return
        elif self.searchable and command.startswith("/"):
            self._apply_search(command)
        elif self.searchable and command.split(maxsplit=1)[0].casefold() in {
            "p",
            "d",
        }:
            self._handle_operation(command)
        else:
            self.message = "无效选择"
        self._refresh()

    def _select_current(self) -> None:
        if not self.visible:
            self.message = "没有可选项"
            self._refresh()
            return
        self._exit(result=self.options[self.visible[self.selected]])

    def _apply_search(self, command: str) -> None:
        tokens = command[1:].strip().casefold().split()
        if not tokens:
            self.visible = list(range(len(self.options)))
        else:
            self.visible = [
                index
                for index in range(len(self.options))
                if all(
                    token in (
                        self.search_texts[index]
                        if self.search_texts is not None
                        and index < len(self.search_texts)
                        else ""
                    ).casefold()
                    for token in tokens
                )
            ]
        self.selected = 0
        self.offset = 0
        self.message = ""

    def _handle_operation(self, command: str) -> None:
        parts = command.split(maxsplit=1)
        operation = parts[0].casefold()
        if len(parts) < 2:
            self.message = f"用法：{operation} <序号>"
            return
        index = self._resolve_position(parts[1])
        if index is None:
            self.message = "无效序号"
            return
        if operation == "p":
            self._preview(index)
            return
        if self.delete_handler is None:
            self.message = "当前选择不支持删除"
            return
        self.confirm_index = index
        self.input_area.read_only = True
        self.input_area.text = ""
        self.message = ""
        self._refresh()

    def _preview(self, index: int) -> None:
        if self.preview_handler is None:
            self.message = "当前选择不支持预览"
            return
        try:
            title, content = self.preview_handler(self.options[index])
        except Exception as exc:
            self.message = f"预览失败：{exc}"
            return
        self.message = ""
        if self.preview_callback is not None:
            try:
                run_in_terminal(
                    lambda: self.preview_callback(title, content)
                )
            except Exception as exc:
                self.message = f"预览显示失败：{exc}"

    def _finish_delete(self, confirmed: bool) -> None:
        index = self.confirm_index
        self.confirm_index = None
        self.input_area.read_only = False
        self.input_area.text = ""
        if index is not None and confirmed and self.delete_handler is not None:
            try:
                self.delete_handler(self.options[index])
            except Exception as exc:
                self.message = f"删除失败：{exc}"
            else:
                self.options.pop(index)
                if (
                    self.search_texts is not None
                    and index < len(self.search_texts)
                ):
                    self.search_texts.pop(index)
                self.visible = list(range(len(self.options)))
                self.selected = 0
                self.offset = 0
                self.message = ""
        self._app.layout.focus(self.input_area)
        self._refresh()

    def _resolve_position(self, raw: str) -> int | None:
        try:
            position = int(raw.strip())
        except (TypeError, ValueError):
            return None
        if 1 <= position <= len(self.visible):
            return self.visible[position - 1]
        return None

    def _move(self, delta: int) -> None:
        if not self.visible:
            return
        self.selected = (self.selected + delta) % len(self.visible)
        self.message = ""
        self._refresh()

    def _cancel(self) -> None:
        self._exit(result="<cancelled>")

    def _exit(self, *, result: str) -> None:
        if self._app is None or self._app.is_done:
            return
        self._app.exit(result=result)

    def _refresh(self) -> None:
        if self._app is not None:
            self._app.invalidate()
