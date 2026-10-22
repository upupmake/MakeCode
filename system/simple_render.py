"""纯终端渲染后端：不启动 Textual，按事件顺序把内容直接输出到 stdout。"""

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console, RenderableType
from rich.errors import MarkupError
from rich.text import Text

from system import simple_input
from system import simple_management
from system import simple_prompt
from system import simple_terminal
from system.simple_menu import ChoiceMenu
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
        self.console = console or simple_terminal.make_console()
        self._lock = threading.RLock()
        self._last_region: TuiRegion | None = None
        self._history: list[str] = []
        self._temporary_query_enabled = False
        self._temporary_query: str | None = None
        self._temporary_query_thread: threading.Thread | None = None
        self._temporary_query_session: Any = None
        self._temporary_query_stop = threading.Event()
        self._temporary_query_prompt_active = False
        self._foreground_input_active = False
        self._pending_input: str | None = None

    def handle(self, event: TuiEvent) -> None:
        if event.region in _SUPPRESSED_REGIONS:
            return

        with self._lock:
            if not self._event_has_output(event):
                if event.clear:
                    self._last_region = None
                return

            self._render_event(event)

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

    @contextmanager
    def _prompt_stdout(self):
        if not simple_terminal.supports_prompt_toolkit():
            yield
            return
        original_file = self.console.file
        with patch_stdout(raw=True):
            self.console.file = simple_terminal.safe_output(sys.stdout)
            try:
                yield
            finally:
                self.console.file = original_file

    def read_input(self, prompt: str, **kwargs: Any) -> str | None:
        self._foreground_input_active = True
        self._pause_temporary_query_for_foreground()
        try:
            if kwargs.get("default") is None:
                pending = self.take_restored_input()
                if pending is not None:
                    kwargs["default"] = pending
            with self._prompt_stdout():
                return simple_prompt.read_prompt(prompt, **kwargs)
        finally:
            self._foreground_input_active = False

    def set_temporary_query_enabled(self, enabled: bool) -> None:
        if not enabled:
            self._stop_temporary_query_listener(cancel_prompt=True)
            with self._lock:
                self._temporary_query_enabled = False
                self._temporary_query = None
            return
        if not simple_input.is_interactive():
            return
        with self._lock:
            if self._temporary_query_enabled:
                return
            self._temporary_query_enabled = True
            self._temporary_query = None
            self._temporary_query_stop.clear()
            self._temporary_query_thread = threading.Thread(
                target=self._temporary_query_loop,
                name="simple-temporary-query",
                daemon=True,
            )
            self._temporary_query_thread.start()

    def consume_temporary_query(self) -> str | None:
        with self._lock:
            if not self._temporary_query_enabled:
                return None
            query = self._temporary_query
            self._temporary_query = None
            return query

    def restore_temporary_query_to_input(
            self,
            fallback_query: str | None = None,
    ) -> None:
        self._stop_temporary_query_listener(cancel_prompt=False)
        with self._lock:
            self._temporary_query_enabled = False
            query = self._temporary_query or fallback_query
            self._temporary_query = None
            if query:
                self._pending_input = query

    def clear_temporary_query(self) -> None:
        with self._lock:
            self._temporary_query = None

    def take_restored_input(self) -> str | None:
        with self._lock:
            query = self._pending_input
            self._pending_input = None
            return query

    def _stop_temporary_query_listener(self, *, cancel_prompt: bool) -> None:
        thread = self._temporary_query_thread
        self._temporary_query_thread = None
        if thread is None:
            return
        self._temporary_query_stop.set()
        if cancel_prompt:
            self._cancel_temporary_query_session()
        elif self._temporary_query_prompt_active:
            with self._lock:
                self.console.print(
                    Text(
                        "Agent 已结束，请提交或按 Esc 取消临时指令。",
                        style="yellow",
                    )
                )
        if thread is not threading.current_thread():
            if cancel_prompt:
                thread.join(timeout=2.0)
            else:
                thread.join()

    def _temporary_query_loop(self) -> None:
        if sys.platform == "win32":
            self._temporary_query_loop_windows()
            return
        import select
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            simple_input._set_cbreak(fd)
            while not self._temporary_query_stop.is_set():
                if not self._lock.acquire(timeout=0.1):
                    continue
                try:
                    ready, _, _ = select.select([fd], [], [], 0.05)
                    if not ready:
                        continue
                    data = os.read(fd, 1)
                finally:
                    self._lock.release()
                if not data:
                    break
                if data == b"\x07":
                    self._temporary_query_prompt()
        except (OSError, ValueError):
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except (OSError, ValueError):
                pass

    def _temporary_query_loop_windows(self) -> None:
        import _thread
        import msvcrt

        while not self._temporary_query_stop.is_set():
            if not self._lock.acquire(timeout=0.05):
                continue
            try:
                if not msvcrt.kbhit():
                    character = None
                else:
                    character = msvcrt.getwch()
            finally:
                self._lock.release()
            if character is None:
                time.sleep(0.05)
                continue
            if character in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if character == "\x07":
                self._temporary_query_prompt()
            elif character == "\x03":
                _thread.interrupt_main()

    def _temporary_query_prompt(self) -> None:
        if (
            self._temporary_query_stop.is_set()
            or self._foreground_input_active
        ):
            return
        self._temporary_query_prompt_active = True
        try:
            with self._lock:
                self.console.print(
                    Text(
                        "临时指令：Enter 提交 · Ctrl+N 换行 · Esc 取消",
                        style="dim",
                    )
                )
            def ready(session) -> None:
                self._temporary_query_session = session

            with self._prompt_stdout():
                query = simple_prompt.read_prompt(
                    "\n临时指令> ",
                    multiline=True,
                    continuation_prompt="...> ",
                    cancel_on_escape=True,
                    on_session_ready=ready,
                )
        except (EOFError, KeyboardInterrupt):
            query = None
        finally:
            self._temporary_query_session = None
            self._temporary_query_prompt_active = False
        if query and query.strip():
            with self._lock:
                if self._temporary_query_enabled:
                    self._temporary_query = query
                else:
                    self._pending_input = query

    def _pause_temporary_query_for_foreground(self) -> None:
        if not self._temporary_query_prompt_active:
            return
        self._cancel_temporary_query_session()
        deadline = time.monotonic() + 2.0
        while (
            self._temporary_query_prompt_active
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

    def _cancel_temporary_query_session(self) -> None:
        session = self._temporary_query_session
        if session is None:
            return
        try:
            app = session.app
            loop = getattr(app, "loop", None)
            if loop is not None:
                loop.call_soon_threadsafe(
                    lambda: app.exit(exception=KeyboardInterrupt)
                )
            else:
                app.exit(exception=KeyboardInterrupt)
        except Exception:
            pass

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
        self._foreground_input_active = True
        self._pause_temporary_query_for_foreground()
        try:
            return self._choose_impl(
                title,
                options,
                allow_custom=allow_custom,
                delete_handler=delete_handler,
                preview_handler=preview_handler,
                search_texts=search_texts,
            )
        finally:
            self._foreground_input_active = False

    def _choose_impl(
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
            if not options and not allow_custom and not searchable:
                return "<cancelled>"
            if simple_terminal.supports_prompt_toolkit() and not allow_custom:
                try:
                    return self._choose_interactive(
                        title,
                        options,
                        delete_handler=delete_handler,
                        preview_handler=preview_handler,
                        search_texts=search_texts,
                    )
                except (EOFError, KeyboardInterrupt):
                    return "<cancelled>"
                except Exception as exc:
                    self.console.print(
                        Text(
                            f"菜单已降级为文本选择：{exc}",
                            style="yellow",
                        )
                    )
            visible = list(range(len(options)))
            self._render_choices(
                title,
                options,
                visible,
                allow_custom=allow_custom,
                searchable=searchable,
            )
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
            input: Any = None,
            output: Any = None,
    ) -> str:
        menu = ChoiceMenu(
            title,
            options,
            search_texts=search_texts,
            delete_handler=delete_handler,
            preview_handler=preview_handler,
            preview_callback=self._show_choice_preview,
        )
        return menu.run(input=input, output=output)

    def _show_choice_preview(self, title: Any, content: Any) -> None:
        self.console.print(Text(str(title), style="bold"))
        self._print(content)

    def choose_delegate_tasks(self, tasks: list[dict[str, str]]) -> str:
        self._pause_temporary_query_for_foreground()
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
            answer = self._read_input(
                "新工作目录（直接回车保持当前目录）",
                path_completion=True,
            )
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
            interactive = simple_terminal.supports_prompt_toolkit()
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
            path_completion: bool = False,
    ) -> str | None:
        try:
            with self._prompt_stdout():
                answer = simple_prompt.read_prompt(
                    f"{prompt}> ",
                    default=default,
                    history=[] if secret else self._history,
                    multiline=False,
                    secret=secret,
                    path_completion=path_completion,
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
