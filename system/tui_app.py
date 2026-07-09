from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from queue import Queue
from typing import Any

from rich.console import RenderableType
from rich.cells import cell_len
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key, Resize
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
    InfoPanelModal,
    LayoutModal,
    McpSwitchModal,
    MemoryConfigModal,
    MemoryPanelModal,
    ModelManagerModal,
    ModelPanelModal,
    RecallModelPickerModal,
    StartupWorkdirModal,
)


class TuiBridge:
    def __init__(self) -> None:
        self._app: MakeCodeTuiApp | None = None
        self._app_thread_id: int | None = None
        self._pending: Queue[TuiEvent] = Queue()
        self._app_lock = threading.Lock()
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

    def choose(self, title: str, options: list[str], *, allow_custom: bool = False) -> str:
        with self._app_lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_choice_modal(title, options, allow_custom, future)
        else:
            app.call_from_thread(app.open_choice_modal, title, options, allow_custom, future)
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

    def show_copy_content(self, messages: list[dict[str, str]]) -> str:
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
        min-width: 10;
        height: 1;
        color: #e5e7eb;
        text-style: bold;
        content-align: left middle;
    }

    #top-status {
        width: 1fr;
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
        background: #374151;
        color: #e5e7eb;
        border: none;
    }

    #quick-panel-buttons {
        height: auto;
        padding: 0 1;
        background: #111827;
    }

    #quick-panel-buttons.hidden {
        display: none;
    }

    .quick-panel-button {
        width: 1fr;
        min-width: 12;
        height: 3;
        margin: 0 1 1 0;
    }

    #left-column {
        width: 2fr;
        height: 1fr;
    }

    #right-column {
        width: 1fr;
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

    #content-pane {
        height: 1fr;
    }

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
        height: 4;
        min-height: 4;
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
    ]

    def __init__(
        self,
        submit_handler: Callable[[str], str | None] | None = None,
        runtime_info_provider: Callable[[], str] | None = None,
        header_info_provider: Callable[[], str] | None = None,
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
        self._runtime_info_provider = runtime_info_provider
        self._header_info_provider = header_info_provider
        self._startup_workdir_provider = startup_workdir_provider
        self._startup_workdir_handler = startup_workdir_handler
        self._mode_label = "ACT"
        self._agent_loop_active = False
        self._slash_matches: list[tuple[str, str]] = []
        self._slash_match_index = 0
        self._slash_hint_visible = False
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._input_history_draft = ""
        self._modal_active = False
        self._right_column_visible = True
        self._last_responsive_width = 0
        self._layout_ratios = load_layout_ratios()
        self._tool_result_count = 0
        self._tool_result_keep_limit = self._load_tool_result_keep_limit()
        self._pane_active_counts: dict[TuiRegion, int] = {}
        self._batch_render_depth = 0
        self._batch_scroll_regions: set[TuiRegion] = set()
        self._batch_runtime_dirty = False
        self._quick_panel_expanded = False
        self.title = "MakeCode"
        self.sub_title = "🎬 Act · Ready"

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("MakeCode", id="top-title")
            yield Button("▸ 快捷面板", id="quick-panel-toggle")
            yield Static("", id="top-status")
            yield Static("", id="top-clock")
        with Vertical(id="quick-panel-shell"):
            with Horizontal(id="quick-panel-buttons", classes="hidden"):
                yield Button("📋 任务", id="quick-tasks", classes="quick-panel-button")
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
                    yield RichLog(id="content-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="content-tail", classes="pane-tail")
                with Vertical(id="tools-pane", classes="pane"):
                    yield RichLog(id="tools-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="tools-tail", classes="pane-tail")
            with Vertical(id="right-column"):
                with Vertical(id="task-pane", classes="pane"):
                    yield RichLog(id="task-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="task-tail", classes="pane-tail")
                with Vertical(id="background-pane", classes="pane"):
                    yield RichLog(id="background-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="background-tail", classes="pane-tail")
                with Vertical(id="sub-agent-pane", classes="pane"):
                    yield RichLog(id="sub-agent-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="sub-agent-tail", classes="pane-tail")
        with Vertical(id="bottom-grid"):
            yield Static("", id="slash-hints")
            yield MakeCodeInput(id="input-box", placeholder='Prompt here e.g. "整理当前项目的架构"')
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
        content_rows = min(max(input_box.wrapped_document.height, 2), 4)
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
        self._last_responsive_width = width
        right_column = self.query_one("#right-column", Vertical)
        should_show_right_column = width >= 140
        if should_show_right_column == self._right_column_visible:
            return
        self._right_column_visible = should_show_right_column
        right_column.set_class(not should_show_right_column, "hidden")

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

    @staticmethod
    def _load_tool_result_keep_limit() -> int:
        from utils.memory import get_keep_recent_tool_call

        return get_keep_recent_tool_call()

    def _update_tools_title(self) -> None:
        self.query_one("#tools-pane", Vertical).border_title = (
            f"Tools · Results: {self._tool_result_count}/{self._tool_result_keep_limit}"
        )

    def refresh_tools_title(self) -> None:
        self._tool_result_keep_limit = self._load_tool_result_keep_limit()
        self._update_tools_title()

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
        self.call_after_refresh(lambda: log.scroll_end(animate=False))

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
            log.write(event.payload, expand=True, shrink=True, scroll_end=should_scroll_end)
            if should_scroll_end:
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
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ChoiceModal(title, options, allow_custom), _done)

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

    def open_copy_content_modal(self, messages: list[dict[str, str]], future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(CopyContentModal(messages), _done)

    def open_model_manager_modal(self, model_manager: Any, future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ModelManagerModal(model_manager), _done)

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
        if self.slash_hint_visible:
            self.accept_slash_selection()
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        text = input_box.text.strip()
        if not text:
            return
        self._record_input_history(text)
        input_box.load_text("")
        self.update_input_height()
        self._slash_matches = []
        self._slash_match_index = 0
        self._hide_slash_hints()
        from system.console_render import render_content_user_message

        post_tui(TuiRegion.CONTENT, "[#3f3f46]─[/#3f3f46]")
        self.handle_tui_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message(text)))
        if self._submit_handler is not None:
            threading.Thread(target=self._run_submit_handler, args=(text,), daemon=True).start()

    def _run_submit_handler(self, text: str) -> None:
        if self._submit_handler is None:
            return
        result = self._submit_handler(text)
        if result == "exit":
            self.call_from_thread(self.exit)

    def set_agent_loop_active(self, active: bool) -> None:
        was_active = self._agent_loop_active
        self._agent_loop_active = active
        self._update_header_status()
        self._update_input_visibility()
        if was_active and not active:
            self._scroll_bottom_panes_after_refresh()
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

    def _update_header_status(self) -> None:
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
        self.query_one("#input-box", MakeCodeInput).border_title = f"MakeCode · {self._mode_label} · Enter 发送/选择 · Ctrl+C 取消回复 · Ctrl+N 换行 · Ctrl+P 切换 · ↑↓ 选择命令"

    def action_cancel_response(self) -> None:
        from system.stream_cancel import cancel_current_response

        if cancel_current_response():
            self.query_one("#input-box", MakeCodeInput).focus()

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
        buttons = self.query_one("#quick-panel-buttons", Horizontal)
        toggle.label = "▾ 快捷面板" if self._quick_panel_expanded else "▸ 快捷面板"
        buttons.set_class(not self._quick_panel_expanded, "hidden")

    def _run_quick_command(self, command: str) -> None:
        if self._submit_handler is None:
            return
        threading.Thread(target=self._run_submit_handler, args=(command,), daemon=True).start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "hitl-toggle":
            self.action_toggle_hitl()
            return
        if button_id == "quick-panel-toggle":
            self.action_toggle_quick_panel()
            return
        quick_commands = {
            "quick-tasks": "/tasks",
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
        if self._agent_loop_active:
            value = f"⚙️ Agent: RUNNING  | {value}"
        runtime_info.update(value)

    def _get_slash_matches(self, text: str) -> list[tuple[str, str]]:
        stripped = text.strip()
        if not stripped.startswith("/") or " " in stripped:
            return []
        from system.commands import COMMAND_DESCRIPTIONS

        return [
            (command, description)
            for command, description in COMMAND_DESCRIPTIONS.items()
            if command.startswith(stripped)
        ]

    def update_slash_hint(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
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

    def complete_slash_command(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
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


def refresh_status() -> None:
    TUI_BRIDGE.refresh_status()


def refresh_tools_title() -> None:
    TUI_BRIDGE.refresh_tools_title()


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


def manage_models_tui(model_manager: Any) -> str:
    return TUI_BRIDGE.manage_models(model_manager)


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


def show_copy_content_tui(messages: list[dict[str, str]]) -> str:
    return TUI_BRIDGE.show_copy_content(messages)


def choose_add_model_tui() -> dict[str, str] | None:
    return TUI_BRIDGE.choose_add_model()


def choose_tui(title: str, options: list[str], *, allow_custom: bool = False) -> str:
    return TUI_BRIDGE.choose(title, options, allow_custom=allow_custom)
