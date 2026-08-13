import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any

from rich.console import RenderableType
from rich.cells import cell_len
from rich.errors import MarkupError
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.events import Click, Key, Resize
from textual.widgets import Button, Footer, Input, Label, RichLog, Static, TextArea

from system.tui_types import (
    TuiEvent,
    TuiRegion,
    load_layout_ratios,
    normalize_layout_ratios,
    save_layout_ratios,
)
from system.tui_modals import (
    AddModelModal,
    ChoiceModal,
    CopyContentModal,
    DelegateTasksModal,
    InfoPanelModal,
    LayoutModal,
    McpSwitchModal,
    MemoryConfigModal,
    MemoryPanelModal,
    ModelManagerModal,
    ModelPanelModal,
    RecallModelPickerModal,
    SkillsConfigModal,
    StartupWorkdirModal,
    TaskPanelModal,
    TemporaryQueryModal,
    ToolHistoryModal,
)
from utils import paths


class TuiBridge:
    def __init__(self) -> None:
        self._app: MakeCodeTuiApp | None = None
        self._app_thread_id: int | None = None
        self._pending: Queue[TuiEvent] = Queue()
        self._app_lock = threading.Lock()
        self._client_request_lock = threading.Lock()
        self._client_request_count = 0
        self._client_request_retries: dict[int, tuple[int, int]] = {}
        content_lock = threading.Lock()
        tools_lock = threading.Lock()
        task_lock = threading.Lock()
        background_lock = threading.Lock()
        sub_agent_lock = threading.Lock()
        self._region_locks = {
            TuiRegion.CONTENT: content_lock,
            TuiRegion.REASONING: content_lock,
            TuiRegion.TOOLS: tools_lock,
            TuiRegion.TASK: task_lock,
            TuiRegion.BACKGROUND: background_lock,
            TuiRegion.SUB_AGENT: sub_agent_lock,
            TuiRegion.STATUS: background_lock,
            TuiRegion.RUNTIME_INFO: background_lock,
        }

    def bind(self, app: "MakeCodeTuiApp") -> None:
        with self._app_lock:
            self._app = app
            self._app_thread_id = threading.get_ident()
            pending: list[TuiEvent] = []
            while not self._pending.empty():
                pending.append(self._pending.get())
        for event in pending:
            self._dispatch_event_locked(app, event)
        self._sync_client_request_state(app)

    def unbind(self, app: "MakeCodeTuiApp") -> None:
        with self._app_lock:
            if self._app is app:
                self._app = None
                self._app_thread_id = None

    def post(
        self,
        region: TuiRegion | str,
        payload: Any = None,
        *,
        clear: bool = False,
        tool_result_delta: int = 0,
        reset_tool_result_count: bool = False,
        tail: bool = False,
        active: bool | None = None,
    ) -> None:
        event = TuiEvent(TuiRegion(region), payload, clear, tool_result_delta, reset_tool_result_count, tail, active)
        with self._app_lock:
            app = self._app
            if app is None:
                self._pending.put(event)
                return
        self._dispatch_event_locked(app, event)

    def choose(
        self,
        title: str,
        options: list[str],
        *,
        allow_custom: bool = False,
        delete_handler: Callable[[str], None] | None = None,
        preview_handler: Callable[[str], tuple[str, RenderableType]] | None = None,
    ) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_choice_modal(title, options, allow_custom, delete_handler, preview_handler, future)
        else:
            app.call_from_thread(
                app.open_choice_modal,
                title,
                options,
                allow_custom,
                delete_handler,
                preview_handler,
                future,
            )
        return future.result()

    def choose_delegate_tasks(self, tasks: list[dict[str, str]]) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "cancel"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_delegate_tasks_modal(tasks, future)
        else:
            app.call_from_thread(app.open_delegate_tasks_modal, tasks, future)
        return future.result()

    def choose_add_model(self) -> dict[str, str] | None:
        with self._app_lock:
            app = self._app
        if app is None:
            return None
        future: Future[dict[str, str] | None] = Future()
        if self._is_app_thread():
            app.open_add_model_modal(future)
        else:
            app.call_from_thread(app.open_add_model_modal, future)
        return future.result()

    def choose_mcp_switch(self, server_switches: list[dict[str, Any]], mcp_manager: Any) -> str | dict:
        with self._app_lock:
            app = self._app
        if app is None:
            return {"action": "cancel"}
        future: Future[str | dict] = Future()
        if self._is_app_thread():
            app.open_mcp_switch_modal(server_switches, mcp_manager, future)
        else:
            app.call_from_thread(app.open_mcp_switch_modal, server_switches, mcp_manager, future)
        return future.result()

    def show_info_panel(self, title: str, content: RenderableType) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_info_panel_modal(title, content, future)
        else:
            app.call_from_thread(app.open_info_panel_modal, title, content, future)
        return future.result()

    def manage_tasks(self, task_manager: Any) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_task_panel_modal(task_manager, future)
        else:
            app.call_from_thread(app.open_task_panel_modal, task_manager, future)
        return future.result()

    def show_copy_content(self, messages: list[dict[str, Any]]) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_copy_content_modal(messages, future)
        else:
            app.call_from_thread(app.open_copy_content_modal, messages, future)
        return future.result()

    def show_tool_history(
        self,
        history: Any,
        messages: list[dict[str, Any]],
    ) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_tool_history_modal(history, messages, future)
        else:
            app.call_from_thread(
                app.open_tool_history_modal,
                history,
                messages,
                future,
            )
        return future.result()

    def manage_models(self, model_manager: Any) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_model_manager_modal(model_manager, future)
        else:
            app.call_from_thread(app.open_model_manager_modal, model_manager, future)
        return future.result()

    def manage_skills(self, skill_loader: Any) -> str | dict[str, Any]:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str | dict[str, Any]] = Future()
        if self._is_app_thread():
            app.open_skills_config_modal(skill_loader, future)
        else:
            app.call_from_thread(app.open_skills_config_modal, skill_loader, future)
        return future.result()

    def manage_layout(self) -> str | dict[str, int]:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str | dict[str, int]] = Future()
        if self._is_app_thread():
            app.open_layout_modal(future)
        else:
            app.call_from_thread(app.open_layout_modal, future)
        return future.result()

    def manage_memories(self, memory_provider: Any) -> list[str]:
        with self._app_lock:
            app = self._app
        if app is None:
            return []
        future: Future[list[str]] = Future()
        if self._is_app_thread():
            app.open_memory_panel_modal(memory_provider, future)
        else:
            app.call_from_thread(app.open_memory_panel_modal, memory_provider, future)
        return future.result()

    def manage_memory_config(self, values: dict[str, Any]) -> str | dict[str, Any]:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str | dict[str, Any]] = Future()
        if self._is_app_thread():
            app.open_memory_config_modal(values, future)
        else:
            app.call_from_thread(app.open_memory_config_modal, values, future)
        return future.result()

    def choose_recall_model(self, options: list[str]) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_recall_model_picker_modal(options, future)
        else:
            app.call_from_thread(app.open_recall_model_picker_modal, options, future)
        return future.result()

    def clear_temporary_query(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.clear_temporary_query()
        else:
            app.call_from_thread(app.clear_temporary_query)

    def set_temporary_query_enabled(self, enabled: bool) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.set_temporary_query_enabled(enabled)
        else:
            future: Future[None] = Future()
            app.call_from_thread(app.set_temporary_query_enabled, enabled, future)
            future.result()

    def consume_temporary_query(self) -> str | None:
        with self._app_lock:
            app = self._app
        if app is None:
            return None
        if self._is_app_thread():
            return app.consume_temporary_query()
        future: Future[str | None] = Future()
        app.call_from_thread(app.consume_temporary_query, future)
        return future.result()

    def _dispatch_event_locked(self, app: "MakeCodeTuiApp", event: TuiEvent) -> None:
        with self._region_locks[event.region]:
            self._dispatch_event(app, event)

    def _dispatch_event(self, app: "MakeCodeTuiApp", event: TuiEvent) -> None:
        if self._is_app_thread():
            app.handle_tui_event(event)
        else:
            app.call_from_thread(app.handle_tui_event, event)

    def _is_app_thread(self) -> bool:
        return self._app_thread_id == threading.get_ident()

    def set_agent_loop_active(self, active: bool) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.set_agent_loop_active(active)
        else:
            app.call_from_thread(app.set_agent_loop_active, active)

    def _client_request_state_locked(self) -> tuple[bool, int, int]:
        request_active = self._client_request_count > 0
        retry_count, max_retries = max(
            self._client_request_retries.values(), default=(0, 0)
        )
        return request_active, retry_count, max_retries

    def _sync_client_request_state(self, app: "MakeCodeTuiApp") -> None:
        with self._client_request_lock:
            request_active, retry_count, max_retries = self._client_request_state_locked()
        app.set_client_request_active(request_active, retry_count, max_retries)

    def _dispatch_client_request_state(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            self._sync_client_request_state(app)
        else:
            app.call_from_thread(self._sync_client_request_state, app)

    def set_client_request_active(self, active: bool, request_id: int | None = None) -> None:
        with self._client_request_lock:
            previous_state = self._client_request_state_locked()
            if active:
                self._client_request_count += 1
                if request_id is not None:
                    self._client_request_retries[request_id] = (0, 0)
            else:
                self._client_request_count = max(0, self._client_request_count - 1)
                if request_id is not None:
                    self._client_request_retries.pop(request_id, None)
            request_state = self._client_request_state_locked()
        if request_state != previous_state:
            self._dispatch_client_request_state()

    def set_client_request_retry(self, request_id: int, retry_count: int, max_retries: int) -> None:
        with self._client_request_lock:
            if request_id not in self._client_request_retries:
                return
            previous_state = self._client_request_state_locked()
            self._client_request_retries[request_id] = (retry_count, max_retries)
            request_state = self._client_request_state_locked()
        if request_state != previous_state:
            self._dispatch_client_request_state()

    def refresh_status(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.refresh_status()
        else:
            app.call_from_thread(app.refresh_status)

    def refresh_tools_title(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.refresh_tools_title()
        else:
            app.call_from_thread(app.refresh_tools_title)

    def flush_screen(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.flush_screen()
        else:
            app.call_from_thread(app.flush_screen)

    def begin_batch_render(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.begin_batch_render()
        else:
            app.call_from_thread(app.begin_batch_render)

    def end_batch_render(self) -> None:
        with self._app_lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.end_batch_render()
        else:
            app.call_from_thread(app.end_batch_render)


TUI_BRIDGE = TuiBridge()


class MakeCodeInput(TextArea):
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        app = self.app
        if not isinstance(app, MakeCodeTuiApp):
            return
        app.update_input_height()

    def _on_key(self, event: Key) -> None:
        app = self.app
        if not isinstance(app, MakeCodeTuiApp):
            return
        if event.key == "enter":
            app.submit_current_input()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+n":
            app.action_insert_newline()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+p":
            app.action_toggle_plan_mode()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+c":
            app.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            app.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "tab":
            app.complete_slash_command()
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and app.cd_completion_visible:
            app.move_cd_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and app.cd_completion_visible:
            app.move_cd_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and app.slash_hint_visible:
            app.move_slash_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and app.slash_hint_visible:
            app.move_slash_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up":
            if not app.should_navigate_input_history(-1):
                return
            app.navigate_input_history(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            if not app.should_navigate_input_history(1):
                return
            app.navigate_input_history(1)
            event.stop()
            event.prevent_default()
            return
        self.call_after_refresh(app.update_input_height)
        self.call_after_refresh(app.update_slash_hint)


class ConversationTitle(Static):
    def on_click(self, event: Click) -> None:
        app = self.app
        if not isinstance(app, MakeCodeTuiApp):
            return
        app.open_conversation_title_regeneration_modal()
        event.stop()


class TuiRichLog(RichLog):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._render_entries: list[tuple[Any, int | None, bool, bool, bool | None, bool]] = []
        self._rendered_entry_count = 0
        self._last_render_width: int | None = None
        self._reflow_scheduled = False
        self._reflowing = False

    def _content_width(self) -> int:
        return max(self.scrollable_content_region.width, 0)

    def _can_render(self) -> bool:
        return self._size_known and self._content_width() > 0

    def _schedule_reflow(self) -> None:
        if self._reflow_scheduled:
            return
        self._reflow_scheduled = True
        self.call_after_refresh(self._reflow_if_needed)

    def _write_without_recording(
        self,
        content: Any,
        width: int | None,
        expand: bool,
        shrink: bool,
        scroll_end: bool | None,
        animate: bool,
    ) -> None:
        if isinstance(content, Text):
            content = content.copy()
        try:
            super().write(
                content,
                width=width,
                expand=expand,
                shrink=shrink,
                scroll_end=scroll_end,
                animate=animate,
            )
        except MarkupError:
            super().write(
                Text(str(content)),
                width=width,
                expand=expand,
                shrink=shrink,
                scroll_end=scroll_end,
                animate=animate,
            )

    def write(
        self,
        content: Any,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> "TuiRichLog":
        stored_content = content.copy() if isinstance(content, Text) else content
        if self._can_render():
            self._write_without_recording(stored_content, width, expand, shrink, scroll_end, animate)
            self._render_entries.append((stored_content, width, expand, shrink, scroll_end, animate))
            self._rendered_entry_count += 1
            self._last_render_width = self._content_width()
        else:
            self._render_entries.append((stored_content, width, expand, shrink, scroll_end, animate))
        return self

    def clear(self) -> "TuiRichLog":
        self._render_entries.clear()
        self._rendered_entry_count = 0
        self._last_render_width = None
        super().clear()
        return self

    def on_resize(self, event: Resize) -> None:
        super().on_resize(event)
        self._schedule_reflow()

    def _reflow_if_needed(self) -> None:
        self._reflow_scheduled = False
        if self._reflowing or not self._can_render():
            return

        width = self._content_width()
        if self._rendered_entry_count == len(self._render_entries) and self._last_render_width == width:
            return

        was_at_bottom = self.is_vertical_scroll_end or self.scroll_y >= self.max_scroll_y - 1
        old_scroll_y = self.scroll_y
        self._reflowing = True
        try:
            super().clear()
            for content, entry_width, expand, shrink, _scroll_end, _animate in self._render_entries:
                self._write_without_recording(content, entry_width, expand, shrink, False, False)
            self._rendered_entry_count = len(self._render_entries)
            self._last_render_width = width
        finally:
            self._reflowing = False

        if was_at_bottom:
            self.call_after_refresh(lambda: self.scroll_end(animate=False, x_axis=False))
        else:
            self.call_after_refresh(
                lambda: self.scroll_to(
                    x=0,
                    y=min(old_scroll_y, self.max_scroll_y),
                    animate=False,
                    immediate=True,
                )
            )


class MakeCodeTuiApp(App[None]):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-grid {
        height: 1fr;
        min-height: 10;
    }

    #top-bar {
        height: 1;
        min-height: 1;
        max-height: 1;
        background: #1f2937;
    }

    #top-title {
        width: auto;
        max-width: 44;
        min-width: 0;
        height: 1;
        color: #e5e7eb;
        text-style: bold;
        content-align: left middle;
    }

    #top-status {
        width: 2fr;
        height: 1;
        color: #e5e7eb;
        content-align: right middle;
    }

    #top-clock {
        width: 10;
        min-width: 10;
        height: 1;
        color: #e5e7eb;
        content-align: right middle;
    }

    #quick-panel-shell {
        height: auto;
        min-height: 0;
        background: #111827;
    }

    #quick-panel-toggle {
        width: 18;
        min-width: 18;
        height: 1;
        min-height: 1;
        max-height: 1;
        margin: 0 0 0 1;
        background: #374151;
        color: #e5e7eb;
        border: none;
    }

    #quick-panel-toggle.compact {
        width: 12;
        min-width: 12;
    }

    #compact-pane-toggle {
        width: 18;
        min-width: 18;
        height: 1;
        min-height: 1;
        max-height: 1;
        margin: 0 0 0 1;
        background: #374151;
        color: #e5e7eb;
        border: none;
    }

    #compact-pane-toggle.compact {
        width: 14;
        min-width: 14;
    }

    #quick-panel-buttons {
        height: auto;
        padding: 0 1;
        background: #111827;
        grid-size: 10;
        grid-rows: 3;
        grid-gutter: 0 1;
    }

    #quick-panel-buttons.compact {
        grid-size: 5;
    }

    #quick-panel-buttons.hidden {
        display: none;
    }

    .quick-panel-button {
        width: 1fr;
        min-width: 10;
        height: 3;
    }

    #left-column {
        width: 72fr;
        height: 1fr;
    }

    #right-column {
        width: 28fr;
        height: 1fr;
    }

    .hidden {
        display: none;
    }

    .pane {
        border: round #d1d5db;
        border-title-color: white;
        padding: 0 1;
    }

    .pane-active {
        border: heavy #f59e0b;
    }

    .pane-log {
        height: 1fr;
    }

    .pane-tail {
        display: none;
        height: auto;
        max-height: 8;
    }

    .pane-tail-visible {
        display: block;
    }

    #content-pane,
    #tools-pane {
        height: 1fr;
    }

    #task-pane {
        height: 1fr;
    }

    #background-pane {
        height: 1fr;
    }

    #sub-agent-pane {
        height: 1fr;
    }

    #bottom-grid {
        height: auto;
        min-height: 3;
    }

    #bottom-grid.hidden {
        display: none;
    }

    #runtime-info-row {
        height: 1;
        min-height: 1;
        max-height: 1;
    }

    #runtime-info-bar {
        width: 1fr;
        height: 1;
        background: #111827;
        color: #e5e7eb;
    }

    #hitl-toggle {
        width: 14;
        min-width: 14;
        height: 1;
        min-height: 1;
        max-height: 1;
        background: #1f2937;
        color: #e5e7eb;
        border: none;
    }

    #slash-hints {
        display: none;
        height: 8;
        max-height: 8;
        border: round #f59e0b;
        background: #111827;
        color: #e5e7eb;
        padding: 0 1;
    }

    #slash-hints.visible {
        display: block;
    }

    #input-box {
        height: 5;
        min-height: 5;
        max-height: 6;
        border: round #22c55e;
    }

    #input-box.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+p", "toggle_plan_mode", "Toggle Plan/Act", priority=True),
        Binding("ctrl+c", "cancel_response", "Cancel", priority=True),
        Binding("escape", "cancel_response", "Cancel", priority=True),
        Binding("ctrl+n", "insert_newline", "New line", priority=True),
        Binding("f6", "toggle_compact_panes", "切换面板", priority=True, show=False),
        Binding("f7", "open_tool_history", "工具历史", priority=True, show=False),
        Binding("ctrl+g", "open_temporary_query", "追加临时指令", priority=True, show=False),
    ]

    def __init__(
        self,
        submit_handler: Callable[[str], Awaitable[str | None]] | None = None,
        runtime_info_provider: Callable[[], str] | None = None,
        header_info_provider: Callable[[], str] | None = None,
        conversation_title_provider: Callable[[], str | None] | None = None,
        conversation_title_regenerate_handler: Callable[[], Awaitable[None]] | None = None,
        messages_provider: Callable[[], list[dict[str, Any]]] | None = None,
        slash_commands_provider: Callable[[], dict[str, str]] | None = None,
        startup_workdir_provider: Callable[[], Any] | None = None,
        startup_workdir_handler: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._logs: dict[TuiRegion, RichLog] = {}
        self._panes: dict[TuiRegion, Vertical] = {}
        self._tails: dict[TuiRegion, Static] = {}
        self._status = "MakeCode ready"
        self._runtime_info = ""
        self._submit_handler = submit_handler
        self._submit_lock = threading.Lock()
        self._runtime_info_provider = runtime_info_provider
        self._header_info_provider = header_info_provider
        self._conversation_title_provider = conversation_title_provider
        self._conversation_title_regenerate_handler = conversation_title_regenerate_handler
        self._messages_provider = messages_provider
        self._slash_commands_provider = slash_commands_provider
        self._startup_workdir_provider = startup_workdir_provider
        self._startup_workdir_handler = startup_workdir_handler
        self._mode_label = "ACT"
        self._agent_loop_active = False
        self._temporary_query_enabled = False
        self._temporary_query: str | None = None
        self._client_request_active = False
        self._client_retry_count = 0
        self._client_max_retries = 0
        self._slash_matches: list[tuple[str, str]] = []
        self._slash_match_index = 0
        self._slash_hint_visible = False
        self._cd_completion_state: tuple[str, int, list[str], int] | None = None
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._input_history_draft = ""
        self._modal_active = False
        self._right_column_visible = True
        self._compact_show_runtime = False
        self._last_responsive_width = 0
        self._layout_ratios = load_layout_ratios()
        self._tool_result_count = 0
        self._pane_active_counts: dict[TuiRegion, int] = {}
        self._batch_render_depth = 0
        self._batch_scroll_regions: set[TuiRegion] = set()
        self._batch_runtime_dirty = False
        self._quick_panel_expanded = False
        self.title = "MakeCode"
        self.sub_title = "🎬 Act · Ready"

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield ConversationTitle("MakeCode", id="top-title")
            yield Button("▸ 快捷面板", id="quick-panel-toggle")
            yield Button("运行面板 F6", id="compact-pane-toggle")
            yield Static("", id="top-status")
            yield Static("", id="top-clock")
        with Vertical(id="quick-panel-shell"):
            with Grid(id="quick-panel-buttons", classes="hidden"):
                yield Button("🧰 工具历史", id="quick-tool-history", classes="quick-panel-button")
                yield Button("🧠 记忆", id="quick-memory", classes="quick-panel-button")
                yield Button("📚 技能", id="quick-skills", classes="quick-panel-button")
                yield Button("🔌 MCP", id="quick-mcp", classes="quick-panel-button")
                yield Button("🛠️ 命令", id="quick-commands", classes="quick-panel-button")
                yield Button("🤖 模型", id="quick-models", classes="quick-panel-button")
                yield Button("⚙️ 记忆配置", id="quick-memory-config", classes="quick-panel-button")
                yield Button("🧩 布局", id="quick-layout", classes="quick-panel-button")
                yield Button("🔀 MCP配置", id="quick-mcp-config", classes="quick-panel-button")
                yield Button("📝 复制", id="quick-copy", classes="quick-panel-button")
        with Horizontal(id="main-grid"):
            with Vertical(id="left-column"):
                with Vertical(id="content-pane", classes="pane"):
                    yield TuiRichLog(id="content-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="content-tail", classes="pane-tail")
                with Vertical(id="tools-pane", classes="pane"):
                    yield TuiRichLog(id="tools-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="tools-tail", classes="pane-tail")
                with Vertical(id="bottom-grid"):
                    yield Static("", id="slash-hints")
                    yield MakeCodeInput(id="input-box", placeholder='Prompt here e.g. "整理当前项目的架构"')
            with Vertical(id="right-column"):
                with Vertical(id="task-pane", classes="pane"):
                    yield TuiRichLog(id="task-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="task-tail", classes="pane-tail")
                with Vertical(id="background-pane", classes="pane"):
                    yield TuiRichLog(id="background-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="background-tail", classes="pane-tail")
                with Vertical(id="sub-agent-pane", classes="pane"):
                    yield TuiRichLog(id="sub-agent-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="sub-agent-tail", classes="pane-tail")
        with Horizontal(id="runtime-info-row"):
            yield Static(self._runtime_info, id="runtime-info-bar")
            yield Button("HITL", id="hitl-toggle")
        yield Footer()

    def on_mount(self) -> None:
        self._panes = {
            TuiRegion.CONTENT: self.query_one("#content-pane", Vertical),
            TuiRegion.REASONING: self.query_one("#content-pane", Vertical),
            TuiRegion.TASK: self.query_one("#task-pane", Vertical),
            TuiRegion.TOOLS: self.query_one("#tools-pane", Vertical),
            TuiRegion.BACKGROUND: self.query_one("#background-pane", Vertical),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-pane", Vertical),
        }
        self._logs = {
            TuiRegion.CONTENT: self.query_one("#content-log", RichLog),
            TuiRegion.REASONING: self.query_one("#content-log", RichLog),
            TuiRegion.TASK: self.query_one("#task-log", RichLog),
            TuiRegion.TOOLS: self.query_one("#tools-log", RichLog),
            TuiRegion.BACKGROUND: self.query_one("#background-log", RichLog),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-log", RichLog),
        }
        self._tails = {
            TuiRegion.CONTENT: self.query_one("#content-tail", Static),
            TuiRegion.REASONING: self.query_one("#content-tail", Static),
            TuiRegion.TASK: self.query_one("#task-tail", Static),
            TuiRegion.TOOLS: self.query_one("#tools-tail", Static),
            TuiRegion.BACKGROUND: self.query_one("#background-tail", Static),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-tail", Static),
        }
        self.query_one("#content-pane", Vertical).border_title = "Content"
        self.query_one("#task-pane", Vertical).border_title = "Task"
        self._update_tools_title()
        self.query_one("#background-pane", Vertical).border_title = "Background"
        self.query_one("#sub-agent-pane", Vertical).border_title = "Sub-Agent"
        self._apply_layout_ratios()
        self._update_header_status()
        self._update_input_title()
        self._update_hitl_button()
        self._update_runtime_info()
        self._update_clock()
        self._update_responsive_layout()
        self.set_interval(0.5, self._check_responsive_layout)
        self.set_interval(1.0, self._update_clock)
        TUI_BRIDGE.bind(self)
        from utils.tasks import render_task_pane

        render_task_pane()
        if self._startup_workdir_provider is not None and self._startup_workdir_handler is not None:
            self.call_after_refresh(self._open_startup_workdir_modal)
        else:
            self.query_one("#input-box", MakeCodeInput).focus()

    def _open_startup_workdir_modal(self) -> None:
        if self._startup_workdir_provider is None or self._startup_workdir_handler is None:
            return

        def _done(value: str | None) -> None:
            self._modal_active = False
            self._startup_workdir_handler(value or "abort")
            self.query_one("#input-box", MakeCodeInput).focus()

        self._modal_active = True
        self.push_screen(StartupWorkdirModal(self._startup_workdir_provider()), _done)

    def update_input_height(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        content_rows = min(max(input_box.wrapped_document.height, 3), 4)
        target_height = content_rows + 2
        if input_box.styles.height != target_height:
            input_box.styles.height = target_height

    def on_resize(self, event: Resize) -> None:
        self._update_responsive_layout(event.size.width)

    def _check_responsive_layout(self) -> None:
        self._update_responsive_layout()

    def _update_responsive_layout(self, width: int | None = None) -> None:
        width = width or self.size.width
        if width == self._last_responsive_width:
            return
        previous_width = self._last_responsive_width
        self._last_responsive_width = width
        if previous_width and previous_width < 140 <= width:
            self._right_column_visible = True
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        left_column = self.query_one("#left-column", Vertical)
        right_column = self.query_one("#right-column", Vertical)
        toggle = self.query_one("#compact-pane-toggle", Button)
        compact = self._last_responsive_width < 140
        if compact:
            self._right_column_visible = self._compact_show_runtime
        self._update_quick_panel()
        self._update_conversation_title()
        toggle.set_class(compact, "compact")

        if compact:
            left_column.set_class(self._compact_show_runtime, "hidden")
            right_column.set_class(not self._compact_show_runtime, "hidden")
            toggle.set_class(False, "hidden")
            toggle.label = "主面板 F6" if self._compact_show_runtime else "运行面板 F6"
            return

        self._compact_show_runtime = False
        left_column.set_class(False, "hidden")
        right_column.set_class(not self._right_column_visible, "hidden")
        toggle.set_class(False, "hidden")
        toggle.label = "隐藏运行面板 F6" if self._right_column_visible else "运行面板 F6"

    def action_toggle_compact_panes(self) -> None:
        if self.size.width >= 140:
            self._right_column_visible = not self._right_column_visible
        else:
            self._compact_show_runtime = not self._compact_show_runtime
        self._apply_responsive_layout()

    def _apply_layout_ratios(self) -> None:
        pane_ids = {
            "content": "#content-pane",
            "tools": "#tools-pane",
            "task": "#task-pane",
            "background": "#background-pane",
            "sub_agent": "#sub-agent-pane",
        }
        for key, selector in pane_ids.items():
            pane = self.query_one(selector, Vertical)
            ratio = self._layout_ratios[key]
            pane.set_class(ratio == 0, "hidden")
            if ratio > 0:
                pane.styles.height = f"{ratio}fr"

    def update_layout_ratios(self, ratios: dict[str, int]) -> None:
        self._layout_ratios = normalize_layout_ratios(ratios)
        save_layout_ratios(self._layout_ratios)
        self._apply_layout_ratios()

    def on_unmount(self) -> None:
        TUI_BRIDGE.unbind(self)

    def _update_tools_title(self) -> None:
        self.query_one("#tools-pane", Vertical).border_title = (
            f"Tools · Results: {self._tool_result_count} · F7 History"
        )

    def refresh_tools_title(self) -> None:
        self._update_tools_title()

    def flush_screen(self) -> None:
        self._driver.write("\x1b[2J\x1b[H")
        self._driver.flush()
        self.refresh(repaint=True, layout=True)

    def _set_pane_active(self, region: TuiRegion, active: bool) -> None:
        pane = self._panes.get(region)
        if pane is None:
            return
        current = self._pane_active_counts.get(region, 0)
        if active:
            current += 1
        else:
            current = max(current - 1, 0)
        self._pane_active_counts[region] = current
        pane_is_active = any(
            mapped_pane is pane and self._pane_active_counts.get(mapped_region, 0) > 0
            for mapped_region, mapped_pane in self._panes.items()
        )
        pane.set_class(pane_is_active, "pane-active")

    def _update_tail(self, region: TuiRegion, payload: Any) -> None:
        tail = self._tails.get(region)
        if tail is None:
            return
        if payload is None or payload == "":
            tail.update("")
            tail.set_class(False, "pane-tail-visible")
            return
        tail.update(payload)
        tail.set_class(True, "pane-tail-visible")

    def _is_log_at_bottom(self, log: RichLog) -> bool:
        return bool(log.is_vertical_scroll_end or log.scroll_y >= log.max_scroll_y - 1)

    def _scroll_log_end_after_refresh(self, log: RichLog) -> None:
        self.call_after_refresh(lambda: log.scroll_end(animate=False, x_axis=False))

    def _scroll_log_to_after_refresh(self, log: RichLog, y: int) -> None:
        self.call_after_refresh(lambda: log.scroll_to(y=y, animate=False))

    def _scroll_bottom_panes_after_refresh(self) -> None:
        for log in self._logs.values():
            if self._is_log_at_bottom(log):
                self._scroll_log_end_after_refresh(log)

    def begin_batch_render(self) -> None:
        self._batch_render_depth += 1

    def end_batch_render(self) -> None:
        if self._batch_render_depth == 0:
            return
        self._batch_render_depth -= 1
        if self._batch_render_depth > 0:
            return
        for region in self._batch_scroll_regions:
            log = self._logs.get(region)
            if log is not None:
                self._scroll_log_end_after_refresh(log)
        self._batch_scroll_regions.clear()
        if self._batch_runtime_dirty:
            self._update_runtime_info()
            self._batch_runtime_dirty = False

    def _mark_runtime_dirty(self) -> None:
        if self._batch_render_depth > 0:
            self._batch_runtime_dirty = True
        else:
            self._update_runtime_info()

    def handle_tui_event(self, event: TuiEvent) -> None:
        if event.region == TuiRegion.STATUS:
            self._runtime_info = str(event.payload)
            self._update_runtime_info()
            return
        if event.region == TuiRegion.RUNTIME_INFO:
            runtime_info = self.query_one("#runtime-info-bar", Static)
            runtime_info.update(str(event.payload))
            return

        log = self._logs[event.region]
        if event.active is not None:
            self._set_pane_active(event.region, event.active)
        if event.tail:
            should_scroll_end = self._is_log_at_bottom(log)
            self._update_tail(event.region, event.payload)
            if should_scroll_end:
                if self._batch_render_depth > 0:
                    self._batch_scroll_regions.add(event.region)
                else:
                    self._scroll_log_end_after_refresh(log)
            return
        if event.clear:
            log.clear()
            self._update_tail(event.region, "")
            self._pane_active_counts[event.region] = 0
            self._set_pane_active(event.region, False)
            if event.region == TuiRegion.TOOLS:
                self._tool_result_count = 0
                self._update_tools_title()
        if event.reset_tool_result_count:
            self._tool_result_count = 0
            self._update_tools_title()
        if event.tool_result_delta:
            self._tool_result_count += event.tool_result_delta
            self._update_tools_title()
        if event.region == TuiRegion.CONTENT and event.payload == "":
            self._mark_runtime_dirty()
            return
        if event.payload is not None and event.region in {TuiRegion.CONTENT, TuiRegion.REASONING, TuiRegion.TASK, TuiRegion.TOOLS, TuiRegion.BACKGROUND, TuiRegion.SUB_AGENT}:
            should_scroll_end = self._is_log_at_bottom(log)
            result_start_y = len(log.lines) if event.region == TuiRegion.TOOLS and event.tool_result_delta else None
            try:
                log.write(event.payload, expand=True, shrink=True, scroll_end=should_scroll_end and result_start_y is None)
            except MarkupError:
                log.write(Text(str(event.payload)), expand=True, shrink=True, scroll_end=should_scroll_end and result_start_y is None)
            if result_start_y is not None:
                self._scroll_log_to_after_refresh(log, result_start_y)
            elif should_scroll_end:
                if self._batch_render_depth > 0:
                    self._batch_scroll_regions.add(event.region)
                else:
                    self._scroll_log_end_after_refresh(log)
        elif event.payload is not None:
            log.write(event.payload)
        self._mark_runtime_dirty()

    def open_choice_modal(
        self,
        title: str,
        options: list[str],
        allow_custom: bool,
        delete_handler: Callable[[str], None] | None,
        preview_handler: Callable[[str], tuple[str, RenderableType]] | None,
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(
            ChoiceModal(title, options, allow_custom, delete_handler, preview_handler), _done
        )

    def open_temporary_query_modal(self) -> None:
        if (
            not self._agent_loop_active
            or not self._temporary_query_enabled
            or self._modal_active
        ):
            return
        pending_query = self._temporary_query
        modal = TemporaryQueryModal(pending_query)

        def _done(value: str | None) -> None:
            self._modal_active = False
            if (
                value is not None
                and self._agent_loop_active
                and self._temporary_query_enabled
            ):
                self._temporary_query = value

        self._modal_active = True
        self.push_screen(modal, _done)

    def consume_temporary_query(self, future: Future[str | None] | None = None) -> str | None:
        query = self._temporary_query if self._temporary_query_enabled else None
        self._temporary_query = None
        if query is not None and isinstance(self.screen, TemporaryQueryModal):
            self.screen.dismiss(None)
        if future is not None and not future.done():
            future.set_result(query)
        return query

    def clear_temporary_query(self) -> None:
        self._temporary_query = None

    def set_temporary_query_enabled(
        self,
        enabled: bool,
        future: Future[None] | None = None,
    ) -> None:
        self._temporary_query_enabled = enabled
        if not enabled:
            self._temporary_query = None
            if isinstance(self.screen, TemporaryQueryModal):
                self.screen.dismiss(None)
        if future is not None and not future.done():
            future.set_result(None)

    def action_open_temporary_query(self) -> None:
        self.open_temporary_query_modal()

    def open_conversation_title_regeneration_modal(self) -> None:
        if (
            self._agent_loop_active
            or self._submit_lock.locked()
            or self._modal_active
            or self._conversation_title_regenerate_handler is None
            or self._conversation_title_provider is None
        ):
            return
        try:
            if not self._conversation_title_provider():
                return
        except Exception:
            return

        confirm_option = "确认重新生成"

        def _done(value: str | None) -> None:
            self._modal_active = False
            if value == confirm_option:
                self._start_conversation_title_regeneration()
                return
            self.query_one("#input-box", MakeCodeInput).focus()

        self._modal_active = True
        self.push_screen(
            ChoiceModal(
                "重新生成对话标题？\n将使用当前对话中全部用户消息生成新标题。",
                [confirm_option, "取消"],
            ),
            _done,
        )

    def _start_conversation_title_regeneration(self) -> None:
        if self._agent_loop_active or self._conversation_title_regenerate_handler is None:
            return
        if not self._submit_lock.acquire(blocking=False):
            return
        self.set_agent_loop_active(True)
        self.run_worker(self._run_conversation_title_regeneration())

    async def _run_conversation_title_regeneration(self) -> None:
        try:
            await self._conversation_title_regenerate_handler()
        finally:
            self._submit_lock.release()
            self.set_agent_loop_active(False)

    def open_delegate_tasks_modal(
        self,
        tasks: list[dict[str, str]],
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "cancel")

        self._modal_active = True
        self.push_screen(DelegateTasksModal(tasks), _done)

    def open_model_panel_modal(
        self,
        title: str,
        options: list[str],
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ModelPanelModal(title, options), _done)

    def open_mcp_switch_modal(self, server_switches: list[dict[str, Any]], mcp_manager: Any, future: Future[str | dict]) -> None:
        def _done(value: str | dict | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or {"action": "cancel"})

        self._modal_active = True
        self.push_screen(McpSwitchModal(server_switches, mcp_manager), _done)

    def open_info_panel_modal(self, title: str, content: RenderableType, future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(InfoPanelModal(title, content), _done)

    def open_task_panel_modal(self, task_manager: Any, future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(TaskPanelModal(task_manager), _done)

    def open_copy_content_modal(self, messages: list[dict[str, Any]], future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(CopyContentModal(messages), _done)

    def open_tool_history_modal(
        self,
        history: Any,
        messages: list[dict[str, Any]],
        future: Future[str] | None = None,
    ) -> None:
        if self._modal_active:
            if future is not None and not future.done():
                future.set_result("<cancelled>")
            return

        def _done(value: str | None) -> None:
            self._modal_active = False
            if future is not None and not future.done():
                future.set_result(value or "<cancelled>")
            self.query_one("#input-box", MakeCodeInput).focus()

        self._modal_active = True
        self.push_screen(ToolHistoryModal(history, messages), _done)

    def open_model_manager_modal(self, model_manager: Any, future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ModelManagerModal(model_manager), _done)

    def open_skills_config_modal(
        self,
        skill_loader: Any,
        future: Future[str | dict[str, Any]],
    ) -> None:
        def _done(value: str | dict[str, Any] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(SkillsConfigModal(skill_loader), _done)

    def open_add_model_modal(self, future: Future[dict[str, str] | None]) -> None:
        def _done(value: dict[str, str] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value)

        self._modal_active = True
        self.push_screen(AddModelModal(), _done)

    def open_layout_modal(self, future: Future[str | dict[str, int]]) -> None:
        def _done(value: str | dict[str, int] | None) -> None:
            self._modal_active = False
            if isinstance(value, dict):
                self.update_layout_ratios(value)
                if not future.done():
                    future.set_result(dict(self._layout_ratios))
                return
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(LayoutModal(self._layout_ratios), _done)

    def open_memory_panel_modal(self, memory_provider: Any, future: Future[list[str]]) -> None:
        def _done(value: list[str] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or [])

        self._modal_active = True
        self.push_screen(MemoryPanelModal(memory_provider), _done)

    def open_memory_config_modal(self, values: dict[str, Any], future: Future[str | dict[str, Any]]) -> None:
        def _done(value: str | dict[str, Any] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(MemoryConfigModal(values), _done)

    def open_recall_model_picker_modal(self, options: list[str], future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(RecallModelPickerModal(options), _done)

    def action_toggle_plan_mode(self) -> None:
        from utils.plan_mode import toggle_plan_mode

        new_state = toggle_plan_mode()
        self._mode_label = "PLAN" if new_state else "ACT"
        self._update_header_status()
        self._update_input_title()
        self._update_runtime_info()
        self.handle_tui_event(TuiEvent(TuiRegion.STATUS, f"{self._mode_label} mode"))

    def action_insert_newline(self) -> None:
        if isinstance(self.screen, TemporaryQueryModal):
            self.screen.action_insert_newline()
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        input_box.insert("\n")

    def _record_input_history(self, text: str) -> None:
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_index = None
        self._input_history_draft = ""

    def should_navigate_input_history(self, direction: int) -> bool:
        input_box = self.query_one("#input-box", MakeCodeInput)
        location = input_box.cursor_location
        if direction < 0:
            return input_box.navigator.is_first_wrapped_line(location)
        return input_box.navigator.is_last_wrapped_line(location)

    def navigate_input_history(self, direction: int) -> None:
        if not self._input_history:
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        if self._input_history_index is None:
            self._input_history_draft = input_box.text
            if direction < 0:
                self._input_history_index = len(self._input_history) - 1
            else:
                return
        else:
            next_index = self._input_history_index + direction
            if next_index < 0:
                next_index = 0
            if next_index >= len(self._input_history):
                self._input_history_index = None
                input_box.load_text(self._input_history_draft)
                input_box.cursor_location = input_box.document.end
                self.update_slash_hint()
                return
            self._input_history_index = next_index

        input_box.load_text(self._input_history[self._input_history_index])
        input_box.cursor_location = input_box.document.end
        self.update_slash_hint()

    def on_key(self, event: Key) -> None:
        if self._modal_active:
            return
        if event.key == "ctrl+p":
            self.action_toggle_plan_mode()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+n":
            self.action_insert_newline()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+c":
            self.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            self.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "tab":
            self.complete_slash_command()
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and self.cd_completion_visible:
            self.move_cd_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self.cd_completion_visible:
            self.move_cd_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and self.slash_hint_visible:
            self.move_slash_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self.slash_hint_visible:
            self.move_slash_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up":
            if not self.should_navigate_input_history(-1):
                return
            self.navigate_input_history(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            if not self.should_navigate_input_history(1):
                return
            self.navigate_input_history(1)
            event.stop()
            event.prevent_default()
            return
        if event.key != "enter":
            self.update_input_height()
            self.update_slash_hint()
            return
        self.submit_current_input()
        event.stop()
        event.prevent_default()

    def submit_current_input(self) -> None:
        if self.slash_hint_visible and not self.cd_completion_visible:
            self.accept_slash_selection()
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        text = input_box.text.strip()
        if not text:
            return
        if self._submit_handler is None or not self._submit_lock.acquire(blocking=False):
            return
        try:
            self._record_input_history(text)
            input_box.load_text("")
            self.update_input_height()
            self._slash_matches = []
            self._slash_match_index = 0
            self._cd_completion_state = None
            self._hide_slash_hints()
            if text != "/flush":
                from system.console_render import render_content_user_message

                post_tui(TuiRegion.CONTENT, "[#3f3f46]─[/#3f3f46]")
                self.handle_tui_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message(text)))
            self._launch_submit_handler(text)
        except Exception:
            self._submit_lock.release()
            raise

    def _launch_submit_handler(self, text: str) -> None:
        threading.Thread(
            target=self._run_submit_handler,
            args=(text, True),
            daemon=True,
        ).start()

    def _run_submit_handler(
        self,
        text: str,
        owns_submit_lock: bool = False,
        bypass_submit_lock: bool = False,
    ) -> None:
        if self._submit_handler is None:
            return
        acquired_submit_lock = owns_submit_lock
        if not acquired_submit_lock and not bypass_submit_lock:
            if not self._submit_lock.acquire(blocking=False):
                return
            acquired_submit_lock = True
        try:
            result = asyncio.run(self._submit_handler(text))
            if result == "exit":
                self.call_from_thread(self.exit)
        finally:
            if acquired_submit_lock:
                self._submit_lock.release()

    def set_agent_loop_active(self, active: bool) -> None:
        was_active = self._agent_loop_active
        self._agent_loop_active = active
        if was_active and not active:
            self.set_temporary_query_enabled(False)
        self._update_header_status()
        self._update_input_visibility()
        if was_active and not active:
            self._scroll_bottom_panes_after_refresh()
        self._update_runtime_info()

    def set_client_request_active(
        self, active: bool, retry_count: int = 0, max_retries: int = 0
    ) -> None:
        self._client_request_active = active
        self._client_retry_count = retry_count
        self._client_max_retries = max_retries
        self._update_runtime_info()

    def _update_input_visibility(self) -> None:
        bottom_grid = self.query_one("#bottom-grid", Vertical)
        input_box = self.query_one("#input-box", MakeCodeInput)
        bottom_grid.set_class(self._agent_loop_active, "hidden")
        input_box.set_class(self._agent_loop_active, "hidden")
        if self._agent_loop_active:
            self._hide_slash_hints()
            return
        self.update_input_height()
        input_box.focus()

    def _update_conversation_title(self) -> None:
        conversation_title = ""
        if self._conversation_title_provider is not None:
            try:
                conversation_title = self._conversation_title_provider() or ""
            except Exception:
                conversation_title = ""
        display_title = f"MakeCode · {conversation_title}" if conversation_title else "MakeCode"
        self.title = display_title
        title_widget = self.query_one("#top-title", Static)
        responsive_width = self._last_responsive_width or self.size.width
        title_widget.styles.max_width = (
            max(0, (responsive_width - 36) // 3) if responsive_width < 140 else 44
        )
        title_widget.update(Text(display_title, overflow="ellipsis", no_wrap=True))
        title_widget.tooltip = conversation_title or None

    def _update_header_status(self) -> None:
        self._update_conversation_title()
        parts = []
        if self._header_info_provider is not None:
            try:
                header_info = self._header_info_provider()
            except Exception:
                header_info = ""
            if header_info:
                parts.append(header_info)
        status_text = " · ".join(parts)
        self.sub_title = status_text
        try:
            self.query_one("#top-status", Static).update(status_text)
        except Exception:
            pass

    def _update_clock(self) -> None:
        try:
            self.query_one("#top-clock", Static).update(datetime.now().strftime("%H:%M:%S"))
        except Exception:
            pass

    def _update_input_title(self) -> None:
        self.query_one("#input-box", MakeCodeInput).border_title = f"MakeCode · {self._mode_label} · Enter 发送/选择 · Ctrl+C 取消回复 · Ctrl+N 换行 · Ctrl+P 切换 · Ctrl+G 运行时介入 · ↑↓ 选择命令"

    def action_cancel_response(self) -> None:
        if isinstance(self.screen, TemporaryQueryModal):
            self.screen.action_cancel()
            return

        from system.stream_cancel import cancel_current_response

        if cancel_current_response():
            self.query_one("#input-box", MakeCodeInput).focus()

    def action_open_tool_history(self) -> None:
        if self._modal_active:
            return
        from system.tool_history import TOOL_EXECUTION_HISTORY

        messages = self._messages_provider() if self._messages_provider is not None else []
        self.open_tool_history_modal(TOOL_EXECUTION_HISTORY, list(messages))

    def action_toggle_hitl(self) -> None:
        from utils.hitl import toggle_hitl

        toggle_hitl()
        self._update_hitl_button()
        self._update_runtime_info()
        self.query_one("#input-box", MakeCodeInput).focus()

    def action_toggle_quick_panel(self) -> None:
        self._quick_panel_expanded = not self._quick_panel_expanded
        self._update_quick_panel()

    def _update_quick_panel(self) -> None:
        toggle = self.query_one("#quick-panel-toggle", Button)
        buttons = self.query_one("#quick-panel-buttons", Grid)
        compact = self._last_responsive_width < 140
        toggle.set_class(compact, "compact")
        buttons.set_class(compact, "compact")
        if compact:
            toggle.label = "▾ 快捷" if self._quick_panel_expanded else "▸ 快捷"
        else:
            toggle.label = "▾ 快捷面板" if self._quick_panel_expanded else "▸ 快捷面板"
        buttons.set_class(not self._quick_panel_expanded, "hidden")

    def _run_quick_command(self, command: str) -> None:
        if self._submit_handler is None:
            return
        threading.Thread(
            target=self._run_submit_handler,
            args=(command, False, True),
            daemon=True,
        ).start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "hitl-toggle":
            self.action_toggle_hitl()
            return
        if button_id == "quick-panel-toggle":
            self.action_toggle_quick_panel()
            return
        if button_id == "compact-pane-toggle":
            self.action_toggle_compact_panes()
            return
        quick_commands = {
            "quick-tool-history": "/tool-history",
            "quick-memory": "/memory-panel",
            "quick-skills": "/skills-list",
            "quick-mcp": "/mcp-view",
            "quick-commands": "/cmds",
            "quick-models": "/models",
            "quick-memory-config": "/memory-config",
            "quick-layout": "/layout",
            "quick-mcp-config": "/mcp-switch",
            "quick-copy": "/copy",
        }
        command = quick_commands.get(button_id or "")
        if command is not None:
            self._run_quick_command(command)

    def _update_hitl_button(self) -> None:
        try:
            from utils.hitl import get_hitl_status

            enabled = get_hitl_status()
        except Exception:
            enabled = False
        button = self.query_one("#hitl-toggle", Button)
        button.label = "HITL ON" if enabled else "HITL OFF"

    def refresh_status(self) -> None:
        self._update_header_status()
        self._update_hitl_button()
        self._update_runtime_info()

    def _update_runtime_info(self) -> None:
        if self._runtime_info_provider is None:
            return
        try:
            value = self._runtime_info_provider()
        except Exception:
            return
        runtime_info = self.query_one("#runtime-info-bar", Static)
        if self._client_request_active:
            if self._client_retry_count:
                client_state = (
                    "🌐 Client: REQUESTING · "
                    f"RETRY {self._client_retry_count}/{self._client_max_retries}"
                )
            else:
                client_state = "🌐 Client: REQUESTING"
            value = f"{client_state}  | {value}"
        runtime_info.update(value)

    def _get_slash_matches(self, text: str) -> list[tuple[str, str]]:
        stripped = text.strip()
        if not stripped.startswith("/") or " " in stripped:
            return []
        from system.commands import COMMAND_DESCRIPTIONS

        commands = (
            self._slash_commands_provider()
            if self._slash_commands_provider is not None
            else COMMAND_DESCRIPTIONS
        )
        # 输入已是完整命令时不再弹出候选，避免抢占上下键的历史导航
        if stripped in commands:
            return []

        return [
            (command, description)
            for command, description in commands.items()
            if command.startswith(stripped)
        ]

    def update_slash_hint(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        if self._cd_completion_state is not None and (
            self._cd_completion_state[0],
            self._cd_completion_state[1],
        ) != (input_box.text, input_box.cursor_location[1]):
            self._cd_completion_state = None
        matches = self._get_slash_matches(input_box.text)
        self._slash_matches = matches
        self._slash_match_index = 0
        if not matches:
            self._hide_slash_hints()
            return
        self._show_slash_hints(matches)

    def _show_slash_hints(self, matches: list[tuple[str, str]]) -> None:
        hint_box = self.query_one("#slash-hints", Static)
        selected = self._slash_match_index % len(matches)
        window_size = 6
        start = min(max(0, selected - window_size + 1), max(0, len(matches) - window_size))
        end = min(len(matches), start + window_size)
        lines = []
        hint_width = hint_box.size.width or 80
        for index, (command, desc) in enumerate(matches[start:end], start=start):
            marker = "❯ " if index == selected else "  "
            display_desc = desc
            if index != selected:
                desc_cell_limit = max(16, hint_width - cell_len(marker) - cell_len(command) - 8)
                if cell_len(desc) > desc_cell_limit:
                    desc_chars = []
                    desc_cells = 0
                    for char in desc:
                        char_cells = cell_len(char)
                        if desc_cells + char_cells > desc_cell_limit - 1:
                            break
                        desc_chars.append(char)
                        desc_cells += char_cells
                    display_desc = f"{''.join(desc_chars)}…"
            lines.append(f"{marker}[bold cyan]{command}[/bold cyan]  [#aaaaaa]{escape(display_desc)}[/#aaaaaa]")
        hint_box.update("\n".join(lines))
        hint_box.add_class("visible")
        self._slash_hint_visible = True

    @property
    def slash_hint_visible(self) -> bool:
        return self._slash_hint_visible

    def move_slash_selection(self, delta: int) -> None:
        matches = self._slash_matches or self._get_slash_matches(self.query_one("#input-box", MakeCodeInput).text)
        if not matches:
            return
        self._slash_matches = matches
        self._slash_match_index = (self._slash_match_index + delta) % len(matches)
        self._show_slash_hints(matches)

    @property
    def cd_completion_visible(self) -> bool:
        return self._cd_completion_state is not None and self._slash_hint_visible

    def move_cd_selection(self, delta: int) -> None:
        state = self._cd_completion_state
        if state is None:
            return
        text, cursor_offset, candidates, selected = state
        selected = (selected + delta) % len(candidates)
        self._cd_completion_state = (text, cursor_offset, candidates, selected)
        self._show_cd_candidates(candidates, selected)

    def accept_slash_selection(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        matches = self._slash_matches or self._get_slash_matches(input_box.text)
        if not matches:
            return
        command, _ = matches[self._slash_match_index % len(matches)]
        input_box.load_text(command)
        input_box.cursor_location = input_box.document.end
        self._hide_slash_hints()
        input_box.focus()

    def _hide_slash_hints(self) -> None:
        if not self._slash_hint_visible:
            return
        hint_box = self.query_one("#slash-hints", Static)
        hint_box.update("")
        hint_box.remove_class("visible")
        self._slash_hint_visible = False

    def _show_cd_candidates(self, candidates: list[str], selected: int) -> None:
        hint_box = self.query_one("#slash-hints", Static)
        window_size = 6
        start = min(max(0, selected - window_size + 1), max(0, len(candidates) - window_size))
        end = min(len(candidates), start + window_size)
        content = Text()
        for index, candidate in enumerate(candidates[start:end], start=start):
            if index > start:
                content.append("\n")
            marker = "❯ " if index == selected else "  "
            content.append(marker, style="bold" if index == selected else "")
            content.append(candidate, style="cyan" if index == selected else "white")
        hint_box.update(content)
        hint_box.add_class("visible")
        self._slash_hint_visible = True

    def _cd_completion_context(self, input_box: MakeCodeInput) -> tuple[str, int, str, str, bool] | None:
        text = input_box.text
        row, column = input_box.cursor_location
        if row != 0 or "\n" in text or not text.startswith("/cd "):
            return None
        cursor_offset = column
        if cursor_offset < 4:
            return None
        raw_fragment = text[4:cursor_offset]
        if not raw_fragment:
            return text, cursor_offset, "", "", False

        quote = raw_fragment[0] if raw_fragment[0] in {'"', "'"} else ""
        path_fragment = raw_fragment[1:] if quote else raw_fragment
        has_closing_quote = bool(quote and path_fragment.endswith(quote))
        if has_closing_quote:
            path_fragment = path_fragment[:-1]
        return text, cursor_offset, quote, path_fragment, has_closing_quote

    def _cd_completion_candidates(self, path_fragment: str) -> list[str]:
        return paths.directory_completion_candidates(path_fragment, paths.workdir())

    def _replace_cd_completion(
        self,
        input_box: MakeCodeInput,
        text: str,
        cursor_offset: int,
        quote: str,
        completed_path: str,
        has_closing_quote: bool,
    ) -> None:
        suffix = text[cursor_offset:]
        closing_quote_in_suffix = bool(quote and suffix.startswith(quote))
        closing_quote = quote if quote and (has_closing_quote or not closing_quote_in_suffix) else ""
        replacement = f"{quote}{completed_path}{closing_quote}"
        new_text = f"{text[:4]}{replacement}{suffix}"
        new_cursor_offset = 4 + len(replacement) - len(closing_quote)
        input_box.load_text(new_text)
        input_box.cursor_location = (0, new_cursor_offset)

    def _complete_cd_path(self, input_box: MakeCodeInput) -> bool:
        context = self._cd_completion_context(input_box)
        if context is None:
            self._cd_completion_state = None
            self._hide_slash_hints()
            return False

        text, cursor_offset, quote, path_fragment, has_closing_quote = context
        state = self._cd_completion_state
        continuing = state is not None and (state[0], state[1]) == (text, cursor_offset)
        if continuing:
            candidates = state[2]
            selected = state[3]
            completed_path = candidates[selected]
        else:
            self._cd_completion_state = None
            candidates = self._cd_completion_candidates(path_fragment)
            if not candidates:
                self._hide_slash_hints()
                return True
            selected = 0
            common_prefix = os.path.commonprefix(candidates)
            if len(common_prefix) > len(path_fragment):
                completed_path = common_prefix
            elif len(candidates) == 1:
                completed_path = candidates[0]
            else:
                completed_path = path_fragment

        self._replace_cd_completion(
            input_box,
            text,
            cursor_offset,
            quote,
            completed_path,
            has_closing_quote,
        )
        self._cd_completion_state = (
            input_box.text,
            input_box.cursor_location[1],
            candidates,
            selected,
        ) if not continuing and len(candidates) > 1 else None
        self._hide_slash_hints()
        if self._cd_completion_state is not None:
            self._show_cd_candidates(candidates, selected)
        input_box.focus()
        return True

    def complete_slash_command(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        if self._complete_cd_path(input_box):
            return
        matches = self._slash_matches or self._get_slash_matches(input_box.text)
        if not matches:
            return
        command, _ = matches[self._slash_match_index % len(matches)]
        input_box.load_text(command)
        input_box.cursor_location = input_box.document.end
        self._slash_matches = matches
        self._slash_match_index = (self._slash_match_index + 1) % len(matches)
        self._show_slash_hints(matches)


def post_tui(
    region: TuiRegion | str,
    payload: RenderableType | str | None = None,
    *,
    clear: bool = False,
    tool_result_delta: int = 0,
    reset_tool_result_count: bool = False,
    tail: bool = False,
    active: bool | None = None,
) -> None:
    TUI_BRIDGE.post(
        region,
        payload,
        clear=clear,
        tool_result_delta=tool_result_delta,
        reset_tool_result_count=reset_tool_result_count,
        tail=tail,
        active=active,
    )


def set_agent_loop_active(active: bool) -> None:
    TUI_BRIDGE.set_agent_loop_active(active)


def set_temporary_query_enabled(enabled: bool) -> None:
    TUI_BRIDGE.set_temporary_query_enabled(enabled)


def consume_temporary_query() -> str | None:
    return TUI_BRIDGE.consume_temporary_query()


def clear_temporary_query() -> None:
    TUI_BRIDGE.clear_temporary_query()


def set_client_request_active(active: bool, request_id: int | None = None) -> None:
    TUI_BRIDGE.set_client_request_active(active, request_id=request_id)


def set_client_request_retry(request_id: int, retry_count: int, max_retries: int) -> None:
    TUI_BRIDGE.set_client_request_retry(request_id, retry_count, max_retries)


def refresh_status() -> None:
    TUI_BRIDGE.refresh_status()


def refresh_tools_title() -> None:
    TUI_BRIDGE.refresh_tools_title()


def flush_tui_screen() -> None:
    TUI_BRIDGE.flush_screen()


def begin_tui_batch_render() -> None:
    TUI_BRIDGE.begin_batch_render()


def end_tui_batch_render() -> None:
    TUI_BRIDGE.end_batch_render()


def choose_model_panel_tui(title: str, options: list[str]) -> str:
    with TUI_BRIDGE._app_lock:
        app = TUI_BRIDGE._app
    if app is None:
        return "<cancelled>"
    future: Future[str] = Future()
    if TUI_BRIDGE._is_app_thread():
        app.open_model_panel_modal(title, options, future)
    else:
        app.call_from_thread(app.open_model_panel_modal, title, options, future)
    return future.result()


def choose_delegate_tasks_tui(tasks: list[dict[str, str]]) -> str:
    return TUI_BRIDGE.choose_delegate_tasks(tasks)


def manage_models_tui(model_manager: Any) -> str:
    return TUI_BRIDGE.manage_models(model_manager)


def manage_skills_tui(skill_loader: Any) -> str | dict[str, Any]:
    return TUI_BRIDGE.manage_skills(skill_loader)


def manage_layout_tui() -> str | dict[str, int]:
    return TUI_BRIDGE.manage_layout()


def manage_memories_tui(memory_provider: Any) -> list[str]:
    return TUI_BRIDGE.manage_memories(memory_provider)


def manage_memory_config_tui(values: dict[str, Any]) -> str | dict[str, Any]:
    return TUI_BRIDGE.manage_memory_config(values)


def choose_recall_model_tui(options: list[str]) -> str:
    return TUI_BRIDGE.choose_recall_model(options)


def choose_mcp_switch_tui(server_switches: list[dict[str, Any]], mcp_manager: Any) -> str | dict:
    return TUI_BRIDGE.choose_mcp_switch(server_switches, mcp_manager)


def show_info_panel_tui(title: str, content: RenderableType) -> str:
    return TUI_BRIDGE.show_info_panel(title, content)


def manage_tasks_tui(task_manager: Any) -> str:
    return TUI_BRIDGE.manage_tasks(task_manager)


def show_copy_content_tui(messages: list[dict[str, Any]]) -> str:
    return TUI_BRIDGE.show_copy_content(messages)


def show_tool_history_tui(
    history: Any,
    messages: list[dict[str, Any]],
) -> str:
    return TUI_BRIDGE.show_tool_history(history, list(messages))


def choose_add_model_tui() -> dict[str, str] | None:
    return TUI_BRIDGE.choose_add_model()


def choose_tui(
    title: str,
    options: list[str],
    *,
    allow_custom: bool = False,
    delete_handler: Callable[[str], None] | None = None,
    preview_handler: Callable[[str], tuple[str, RenderableType]] | None = None,
) -> str:
    return TUI_BRIDGE.choose(
        title,
        options,
        allow_custom=allow_custom,
        delete_handler=delete_handler,
        preview_handler=preview_handler,
    )
