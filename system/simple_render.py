"""纯终端渲染后端：不启动 Textual，按事件顺序把内容直接输出到 stdout。"""

import shutil
import sys
import threading
from typing import Any

from rich.console import Console, RenderableType
from rich.cells import cell_len
from rich.errors import MarkupError
from rich.text import Text

from system import simple_management
from system import simple_input
from system.tui_types import TuiEvent, TuiRegion


_REGION_LABELS = {
    TuiRegion.BACKGROUND: "Background",
    TuiRegion.SUB_AGENT: "Sub-Agent",
}

# Task 面板在 TUI 中是整块刷新，逐次打印会造成大量重复内容；
# 简单模式只在用户显式执行 /tasks 时输出任务表。
_SUPPRESSED_REGIONS = {
    TuiRegion.STATUS,
    TuiRegion.RUNTIME_INFO,
    TuiRegion.TASK,
}


class SimpleRenderer:
    """把 TuiEvent 顺序渲染到普通终端。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._lock = threading.RLock()
        self._last_region: TuiRegion | None = None
        self._history: list[str] = []
        self._input_clear: Any = None
        self._input_redraw: Any = None

    def handle(self, event: TuiEvent) -> None:
        if event.region in _SUPPRESSED_REGIONS:
            return

        with self._lock:
            if not self._event_has_output(event):
                if event.clear:
                    self._last_region = None
                return

            clear = self._input_clear
            redraw = self._input_redraw
            if clear is not None:
                clear()
            elif redraw is not None:
                sys.stdout.write("\r\x1b[2K")
                sys.stdout.flush()
            self._render_event(event)
            if redraw is not None:
                try:
                    redraw()
                except Exception:
                    self._input_redraw = None

    @staticmethod
    def _event_has_output(event: TuiEvent) -> bool:
        if event.clear:
            return event.region == TuiRegion.CONTENT
        if event.collapsible_title is not None:
            return bool(
                event.collapsible_open or event.payload not in (None, "")
            )
        if event.tail:
            return False
        return event.payload not in (None, "")

    def _render_event(self, event: TuiEvent) -> None:
        if event.clear:
            if event.region == TuiRegion.CONTENT:
                self.console.print("\n[dim]──────── 新对话 ────────[/dim]\n")
            self._last_region = None
            return

        if event.collapsible_title is not None:
            if event.collapsible_open:
                self._print_region_label(event.region)
                self.console.print(Text(event.collapsible_title, style="bold"))
            if event.payload not in (None, ""):
                self._print(event.payload)
            return

        self._print_region_label(event.region)
        self._print(event.payload)

    def register_input_redraw(self, callback: Any) -> None:
        with self._lock:
            self._input_redraw = callback

    def register_input_clear(self, callback: Any) -> None:
        with self._lock:
            self._input_clear = callback

    def read_input(self, prompt: str, **kwargs: Any) -> str | None:
        return simple_input.read_line(
            prompt,
            input_clear_handler=self.register_input_clear,
            input_redraw_handler=self.register_input_redraw,
            input_lock=self._lock,
            **kwargs,
        )

    def choose(
            self,
            title: str,
            options: list[str],
            *,
            allow_custom: bool = False,
            delete_handler: Any = None,
            preview_handler: Any = None,
            search_texts: list[str] | None = None,
    ) -> str:
        with self._lock:
            options = list(options)
            search_texts = list(search_texts) if search_texts is not None else None
            searchable = search_texts is not None
            if simple_input.is_interactive() and not allow_custom:
                return self._choose_interactive(
                    title,
                    options,
                    delete_handler=delete_handler,
                    preview_handler=preview_handler,
                    search_texts=search_texts,
                )
            visible = list(range(len(options)))
            self._render_choices(
                title,
                options,
                visible,
                allow_custom=allow_custom,
                searchable=searchable,
            )
            if not options and not allow_custom and not searchable:
                return "<cancelled>"

            while True:
                answer = self._read_input("选择")
                if answer is None:
                    return "<cancelled>"
                answer = answer.strip()
                if not answer:
                    return "<empty_input>" if allow_custom else "<cancelled>"
                if (
                    not searchable
                    and not allow_custom
                    and answer.lower() in {"q", "quit", "exit"}
                ):
                    return "<cancelled>"
                if searchable:
                    lowered = answer.lower()
                    if lowered in {"q", "quit", "exit"}:
                        return "<cancelled>"
                    if answer.startswith("/"):
                        tokens = answer[1:].strip().casefold().split()
                        visible = [
                            index
                            for index in range(len(options))
                            if all(
                                token
                                in (
                                    search_texts[index]
                                    if index < len(search_texts)
                                    else ""
                                ).casefold()
                                for token in tokens
                            )
                        ]
                        self._render_choices(
                            title,
                            options,
                            visible,
                            allow_custom=allow_custom,
                            searchable=True,
                        )
                        continue
                    parts = answer.split(maxsplit=1)
                    operation = parts[0].lower()
                    if operation in {"p", "d"} and len(parts) == 2:
                        index = self._visible_choice(parts[1], visible)
                        if index is None:
                            self.console.print(Text("无效序号。", style="yellow"))
                            continue
                        if operation == "p":
                            if preview_handler is None:
                                self.console.print(Text("当前选择不支持预览。", style="yellow"))
                                continue
                            try:
                                preview_title, content = preview_handler(options[index])
                            except Exception as exc:
                                self.console.print(Text(f"预览失败：{exc}", style="yellow"))
                                continue
                            self.console.print(Text(str(preview_title), style="bold"))
                            self._print(content)
                            continue
                        if delete_handler is None:
                            self.console.print(Text("当前选择不支持删除。", style="yellow"))
                            continue
                        confirm = self._read_input("确认删除？[y/N]")
                        if confirm is None or confirm.strip().lower() not in {
                            "y",
                            "yes",
                            "是",
                        }:
                            continue
                        try:
                            delete_handler(options[index])
                        except Exception as exc:
                            self.console.print(Text(f"删除失败：{exc}", style="yellow"))
                            continue
                        options.pop(index)
                        if index < len(search_texts):
                            search_texts.pop(index)
                        visible = list(range(len(options)))
                        self._render_choices(
                            title,
                            options,
                            visible,
                            allow_custom=allow_custom,
                            searchable=True,
                        )
                        continue
                if answer.isdigit():
                    index = self._visible_choice(answer, visible)
                    if index is not None:
                        return options[index]
                if allow_custom:
                    return answer
                self.console.print(Text(f"无效选择：{answer}", style="yellow"))

    def _render_choices(
            self,
            title: str,
            options: list[str],
            visible: list[int],
            *,
            allow_custom: bool,
            searchable: bool,
    ) -> None:
        self.console.print()
        self.console.print(Text(title, style="bold"))
        for position, index in enumerate(visible, 1):
            self.console.print(Text(f"  {position}. {options[index]}"))
        if searchable:
            self.console.print(
                Text(
                    "序号=选择；/关键词=搜索；p 序号=预览；d 序号=删除；q=取消。",
                    style="dim",
                )
            )
        elif allow_custom:
            self.console.print(
                Text(
                    "输入序号选择，或直接输入自定义内容；空输入提交空值，Ctrl+C/Ctrl+D 取消。",
                    style="dim",
                )
            )
        else:
            self.console.print(Text("输入序号选择；Ctrl+C/Ctrl+D 取消。", style="dim"))

    @staticmethod
    def _visible_choice(raw: str, visible: list[int]) -> int | None:
        try:
            position = int(raw.strip())
        except (TypeError, ValueError):
            return None
        if 1 <= position <= len(visible):
            return visible[position - 1]
        return None

    def _choose_interactive(
            self,
            title: str,
            options: list[str],
            *,
            delete_handler: Any,
            preview_handler: Any,
            search_texts: list[str] | None,
    ) -> str:
        searchable = search_texts is not None
        visible = list(range(len(options)))
        selected = 0
        buffer = ""
        message = ""

        with simple_input.raw_mode():
            self._render_interactive_menu(title, options, visible, searchable)
            self._write_interactive_status(
                options,
                visible,
                selected,
                buffer,
                message,
            )
            while True:
                key = simple_input.read_key()

                if isinstance(key, simple_input.PasteEvent):
                    buffer += key.text.replace("\n", " ")
                    message = ""
                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )
                    continue

                if key in {"up", "left"}:
                    if visible:
                        selected = (selected - 1) % len(visible)
                    buffer = ""
                    message = ""
                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )
                    continue

                if key in {"down", "right"}:
                    if visible:
                        selected = (selected + 1) % len(visible)
                    buffer = ""
                    message = ""
                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )
                    continue

                if key in {"ctrl_c", "eof", "escape"}:
                    self._end_interactive_status()
                    return "<cancelled>"

                if key == "ctrl_d":
                    if not buffer:
                        self._end_interactive_status()
                        return "<cancelled>"
                    continue

                if key == "enter":
                    if not buffer:
                        if not visible:
                            self._end_interactive_status()
                            return "<cancelled>"
                        self._end_interactive_status()
                        return options[visible[selected]]

                    command = buffer.strip()
                    buffer = ""
                    if not command:
                        continue
                    lowered = command.lower()
                    if lowered in {"q", "quit", "exit"}:
                        self._end_interactive_status()
                        return "<cancelled>"

                    if command.isdigit():
                        index = self._visible_choice(command, visible)
                        if index is not None:
                            self._end_interactive_status()
                            return options[index]
                        message = "无效序号"
                    elif searchable and command.startswith("/"):
                        tokens = command[1:].strip().casefold().split()
                        visible = [
                            index
                            for index in range(len(options))
                            if all(
                                token
                                in (
                                    search_texts[index]
                                    if index < len(search_texts)
                                    else ""
                                ).casefold()
                                for token in tokens
                            )
                        ]
                        selected = 0
                        message = ""
                        self._end_interactive_status()
                        self._render_interactive_menu(
                            title, options, visible, searchable
                        )
                    elif searchable and command.split(maxsplit=1)[0].lower() in {
                        "p",
                        "d",
                    }:
                        parts = command.split(maxsplit=1)
                        operation = parts[0].lower()
                        if len(parts) < 2:
                            message = f"用法：{operation} <序号>"
                        else:
                            index = self._visible_choice(parts[1], visible)
                            if index is None:
                                message = "无效序号"
                            elif operation == "p":
                                if preview_handler is None:
                                    message = "当前选择不支持预览"
                                else:
                                    try:
                                        preview_title, content = preview_handler(
                                            options[index]
                                        )
                                    except Exception as exc:
                                        message = f"预览失败：{exc}"
                                    else:
                                        message = ""
                                        self._end_interactive_status()
                                        self.console.print(
                                            Text(str(preview_title), style="bold")
                                        )
                                        self._print(content)
                                        self._render_interactive_menu(
                                            title, options, visible, searchable
                                        )
                            else:
                                if delete_handler is None:
                                    message = "当前选择不支持删除"
                                else:
                                    self._end_interactive_status()
                                    sys.stdout.write("确认删除？[y/N] ")
                                    sys.stdout.flush()
                                    confirm = simple_input.read_key()
                                    sys.stdout.write("\n")
                                    sys.stdout.flush()
                                    if confirm in {"y", "Y"}:
                                        try:
                                            delete_handler(options[index])
                                        except Exception as exc:
                                            message = f"删除失败：{exc}"
                                        else:
                                            message = ""
                                            options.pop(index)
                                            if (
                                                search_texts is not None
                                                and index < len(search_texts)
                                            ):
                                                search_texts.pop(index)
                                            visible = list(range(len(options)))
                                            selected = 0
                                    self._render_interactive_menu(
                                        title, options, visible, searchable
                                    )
                    else:
                        message = "无效选择"

                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )
                    continue

                if key == "backspace":
                    if buffer:
                        buffer = buffer[:-1]
                    message = ""
                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )
                    continue

                if len(key) == 1 and key.isprintable():
                    buffer += key
                    message = ""
                    self._write_interactive_status(
                        options, visible, selected, buffer, message
                    )

    def _render_interactive_menu(
            self,
            title: str,
            options: list[str],
            visible: list[int],
            searchable: bool,
    ) -> None:
        self.console.print()
        self.console.print(Text(title, style="bold"))
        if visible:
            for position, index in enumerate(visible, 1):
                self.console.print(Text(f"  {position}. {options[index]}"))
        else:
            self.console.print(Text("  （没有匹配项）", style="dim"))
        if searchable:
            hint = (
                "↑/↓/←/→ 移动，Enter 选择；数字+Enter 直接选择；"
                "/关键词 搜索；p 序号 预览；d 序号 删除；q 取消。"
            )
        else:
            hint = "↑/↓/←/→ 移动，Enter 选择；数字+Enter 直接选择；q 取消。"
        self.console.print(Text(hint, style="dim"))

    def _write_interactive_status(
            self,
            options: list[str],
            visible: list[int],
            selected: int,
            buffer: str,
            message: str,
    ) -> None:
        if message:
            text = f"⚠ {message}"
        elif buffer:
            text = f"> {buffer}"
        elif visible:
            text = (
                f"选中 {selected + 1}/{len(visible)}: "
                f"{options[visible[selected]]}"
            )
        else:
            text = "选中: -"
        width = max(shutil.get_terminal_size((80, 20)).columns - 1, 20)
        sys.stdout.write("\r\x1b[2K" + self._truncate_status(text, width))
        sys.stdout.flush()

    @staticmethod
    def _end_interactive_status() -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    @staticmethod
    def _truncate_status(text: str, width: int) -> str:
        if cell_len(text) <= width:
            return text
        result = []
        cells = 0
        for character in text:
            character_width = cell_len(character)
            if cells + character_width > width - 1:
                break
            result.append(character)
            cells += character_width
        return "".join(result) + "…"

    def choose_delegate_tasks(self, tasks: list[dict[str, str]]) -> str:
        with self._lock:
            self.console.print()
            self.console.print(Text("子智能体委派确认", style="bold"))
            for task in tasks:
                self.console.print(
                    Text(
                        f"  TASK #{task.get('task_id', '')} · {task.get('role_name', '')}",
                        style="bold",
                    )
                )
                self.console.print(Text(f"    {task.get('summary', '')}", style="dim"))
            choice = self.choose(
                "请选择",
                ["批准委派", "主智能体执行", "取消"],
            )
        if choice == "批准委派":
            return "approve"
        if choice == "主智能体执行":
            return "orchestrator"
        return "cancel"

    def prompt_workdir(self) -> str:
        from utils import paths

        with self._lock:
            self.console.print(Text(f"当前工作目录：{paths.workdir()}"))
            answer = self._read_input("新工作目录（直接回车保持当前目录）")
            if answer is None:
                return "abort"
            answer = answer.strip()
            return f"custom:{answer}" if answer else "default"

    def show_info_panel(self, title: str, content: RenderableType) -> str:
        with self._lock:
            self.console.rule(Text(str(title), style="bold"))
            self._print(content)
        return "closed"

    def show_mcp_view(self, summary: RenderableType, tools: list[dict[str, Any]]) -> str:
        with self._lock:
            self.console.rule(Text("MCP", style="bold"))
            self._print(summary)
            if tools:
                self.console.print(Text("工具明细", style="bold"))
                for tool in tools:
                    self.console.print(
                        Text(
                            f"  {tool.get('provider', '')} · {tool.get('name', '')}: "
                            f"{tool.get('description', '')}",
                            style="dim",
                        )
                    )
        return "closed"

    def choose_add_model(self) -> dict[str, str] | None:
        return simple_management.choose_add_model(self)

    def choose_mcp_switch(
            self,
            server_switches: list[dict[str, Any]],
            mcp_manager: Any,
    ) -> str | dict:
        return simple_management.choose_mcp_switch(
            self,
            server_switches,
            mcp_manager,
        )

    def manage_tasks(self, task_manager: Any) -> str:
        self.print_tasks(task_manager)
        return "closed"

    def show_copy_content(self, messages: list[dict[str, Any]]) -> str:
        return simple_management.show_copy_content(self, messages)

    def manage_models(self, model_manager: Any) -> str:
        return simple_management.manage_models(self, model_manager)

    def manage_skills(self, skill_loader: Any) -> str | dict[str, Any]:
        return simple_management.manage_skills(self, skill_loader)

    def manage_layout(self) -> str | dict[str, int]:
        return simple_management.manage_layout(self)

    def manage_memories(self, memory_provider: Any) -> list[str]:
        return simple_management.manage_memories(self, memory_provider)

    def manage_memory_config(self, values: dict[str, Any]) -> str | dict[str, Any]:
        from system.models import get_model_manager

        return simple_management.manage_memory_config(
            self,
            values,
            get_model_manager(),
        )

    def choose_recall_model(self, options: list[str]) -> str:
        return simple_management.choose_recall_model(self, options)

    def manage_extra_tools(self, values: dict[str, Any]) -> str | dict[str, Any]:
        return simple_management.manage_extra_tools(self, values)

    def choose_image_understanding_model(self, options: list[str]) -> str:
        return simple_management.choose_image_understanding_model(self, options)

    def print_tasks(self, task_manager: Any) -> None:
        task_table = task_manager.get_task_table()
        rows = task_table.get("rows") or []
        summary = task_table.get("summary") or {}
        with self._lock:
            self.console.print()
            if not rows:
                self.console.print(Text("当前任务计划为空。", style="yellow"))
                return
            completed = summary.get("completed", 0)
            total = summary.get("total", len(rows))
            runnable = summary.get("runnable_count", 0)
            self.console.print(
                Text(
                    f"当前任务计划 · ✓ {completed}/{total} completed · ▶ {runnable} runnable",
                    style="bold cyan",
                )
            )
            for row in rows:
                if row.get("status") == "completed":
                    icon = "✓"
                elif row.get("is_runnable"):
                    icon = "▶"
                else:
                    icon = "□"
                self.console.print(
                    Text(f"  {icon} {row.get('id', '')} {row.get('subject', '')}")
                )

    def notice(self, message: str) -> None:
        with self._lock:
            self.console.print(Text(message, style="yellow"))

    def write(self, payload: Any) -> None:
        with self._lock:
            self._print(payload)

    def prompt(
            self,
            label: str,
            *,
            default: str | None = None,
            secret: bool = False,
    ) -> str | None:
        with self._lock:
            interactive = simple_input.is_interactive()
            suffix = (
                ""
                if interactive or default is None
                else f" [{default}]"
            )
            answer = self._read_input(
                f"{label}{suffix}",
                default=default,
                secret=secret,
            )
        if answer is None:
            return None
        answer = answer.strip()
        if not answer and default is not None:
            return default
        return answer

    def _read_input(
            self,
            prompt: str,
            *,
            default: str | None = None,
            secret: bool = False,
    ) -> str | None:
        try:
            answer = simple_input.read_line(
                f"{prompt}> ",
                default=default,
                history=[] if secret else self._history,
                secret=secret,
            )
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return None
        if answer is not None and answer.strip() and not secret:
            if not self._history or self._history[-1] != answer:
                self._history.append(answer)
        return answer

    def _print_region_label(self, region: TuiRegion) -> None:
        label = _REGION_LABELS.get(region)
        if label is not None and region != self._last_region:
            self.console.print(f"[dim]── {label} ──[/dim]")
        self._last_region = region

    def _print(self, payload: Any) -> None:
        try:
            self.console.print(payload)
        except MarkupError:
            self.console.print(Text(str(payload)))
