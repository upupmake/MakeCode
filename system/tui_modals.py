import json
from typing import Any, Callable, TypeVar
from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Select, TextArea, DataTable

from system.models import MESSAGE_FORMATS, ModelKey, REASONING_EFFORTS
from system.clipboard import copy_to_system_clipboard
from system.tool_history import (
    TOOL_STATUS_BLOCKED,
    TOOL_STATUS_COMPACTED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_INCOMPLETE,
    TOOL_STATUS_RUNNING,
    TOOL_STATUS_SUCCEEDED,
    ToolExecutionHistory,
    ToolExecutionRecord,
    ToolExecutionSummary,
    format_tool_arguments,
    format_tool_value,
)
from system.tui_types import (
    LAYOUT_DEFAULT_RATIOS,
    LAYOUT_LEFT_KEYS,
    LAYOUT_RIGHT_KEYS,
    normalize_layout_ratios,
)


ModalResult = TypeVar("ModalResult")


class ClosableModalScreen(ModalScreen[ModalResult]):
    def action_close_modal(self) -> None:
        close_action = getattr(self, "action_cancel", None) or getattr(self, "action_close", None)
        if close_action is None:
            self.dismiss(None)
            return
        close_action()


class ModalCloseButton(Button):
    def __init__(self) -> None:
        super().__init__("×", id="modal-close", classes="modal-close")
        self.tooltip = "关闭窗口"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        screen = self.screen
        if isinstance(screen, ClosableModalScreen):
            screen.action_close_modal()


class ModalHeader(Horizontal):
    def __init__(self, title: RenderableType, *, title_id: str, markup: bool = True) -> None:
        super().__init__(classes="modal-header")
        self._title = title
        self._title_id = title_id
        self._markup = markup

    def compose(self) -> ComposeResult:
        yield Label(
            self._title,
            id=self._title_id,
            classes="modal-header-title",
            markup=self._markup,
        )
        yield ModalCloseButton()


class ChoiceModal(ClosableModalScreen[str]):
    CSS = """
    ChoiceModal, DelegateTasksModal, StartupWorkdirModal, ModelPanelModal, McpSwitchModal, ModelManagerModal, AddModelModal, LayoutModal, MemoryPanelModal, MemoryConfigModal, RecallModelPickerModal, InfoPanelModal, CopyContentModal, TaskPanelModal, ToolHistoryModal, SkillsConfigModal {
        align: center middle;
    }

    .modal-header {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        align: left top;
    }

    .modal-header-title {
        width: 1fr;
        height: auto;
        margin-bottom: 0;
    }

    .modal-close {
        width: 3;
        min-width: 3;
        height: 1;
        min-height: 1;
        padding: 0;
        margin: 0;
        border: none;
    }

    #startup-dialog {
        width: 80;
        height: auto;
        border: round #38bdf8;
        background: $surface;
        padding: 1 2;
    }

    #startup-title {
        height: auto;
        margin-bottom: 0;
    }

    #startup-input {
        margin-top: 1;
    }

    #info-dialog {
        width: 88%;
        height: auto;
        max-height: 86%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #task-dialog {
        width: 88%;
        height: auto;
        max-height: 86%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #task-title {
        height: auto;
        margin-bottom: 0;
    }

    #task-table {
        height: auto;
        max-height: 24;
    }

    #task-actions {
        height: 3;
        margin-top: 1;
    }

    #task-close {
        width: 16;
    }

    #tool-history-dialog {
        width: 94%;
        height: 90%;
        border: round #22d3ee;
        background: $surface;
        padding: 1 2;
    }

    #tool-history-title {
        height: auto;
        margin-bottom: 0;
    }

    #tool-history-filter-row {
        height: 3;
        margin-top: 1;
    }

    #tool-history-search {
        width: 1fr;
    }

    .tool-history-filter {
        width: 20;
        margin-left: 1;
    }

    #tool-history-filter-row.compact {
        height: auto;
        layout: vertical;
    }

    #tool-history-filter-row.compact > .tool-history-filter {
        width: 100%;
        margin-left: 0;
    }

    #tool-history-content {
        height: 1fr;
        margin-top: 1;
    }

    #tool-history-list {
        width: 35fr;
        height: 1fr;
        border: round #475569;
    }

    #tool-history-list > ListItem {
        height: auto;
        margin-bottom: 1;
    }

    #tool-history-list > ListItem > Label {
        width: 1fr;
    }

    #tool-history-detail {
        width: 65fr;
        height: 1fr;
        margin-left: 1;
        border: round #3b82f6;
        padding: 0 1;
    }

    #tool-history-content.compact {
        layout: vertical;
    }

    #tool-history-content.compact > #tool-history-list,
    #tool-history-content.compact > #tool-history-detail {
        width: 100%;
        margin-left: 0;
    }

    .tool-history-hidden {
        display: none;
    }

    #tool-history-status {
        height: 1;
        color: #94a3b8;
    }

    #skills-dialog {
        width: 88%;
        height: 86%;
        border: round #22c55e;
        background: $surface;
        padding: 1 2;
    }

    #skills-title {
        height: auto;
        margin-bottom: 0;
    }

    #skills-filter-row {
        height: 3;
        margin-top: 1;
    }

    #skills-search {
        width: 1fr;
    }

    #skills-status-filter {
        width: 20;
        margin-left: 1;
    }

    #skills-list {
        height: 1fr;
        margin-top: 1;
        border: round #475569;
    }

    #skills-list > ListItem {
        height: auto;
        margin-bottom: 1;
    }

    #skills-list > ListItem > Label {
        width: 1fr;
    }

    #skills-status {
        height: auto;
        min-height: 1;
        margin-top: 1;
        color: #94a3b8;
    }

    #skills-actions {
        height: 3;
        margin-top: 1;
        align: right middle;
    }

    #skills-confirm {
        width: 34;
        margin-right: 1;
    }

    #skills-close {
        width: 12;
    }

    #info-content {
        height: auto;
        max-height: 28;
        min-height: 1;
        margin-top: 1;
    }

    #info-actions {
        height: 3;
        margin-top: 1;
    }

    #info-close {
        width: 16;
    }

    #copy-dialog {
        width: 92%;
        height: 90%;
        border: round #38bdf8;
        background: $surface;
        padding: 1 2;
    }

    #copy-title {
        height: auto;
        margin-bottom: 0;
    }

    #copy-summary {
        height: 1;
        margin-top: 1;
    }

    #copy-text {
        height: 1fr;
        margin-top: 1;
    }

    #copy-status {
        height: auto;
        min-height: 1;
        margin-top: 1;
    }

    #copy-actions {
        height: 3;
        margin-top: 1;
        align: right middle;
    }

    #copy-selection {
        width: 18;
        margin-right: 1;
    }

    #copy-all {
        width: 16;
        margin-right: 1;
    }

    #copy-close {
        width: 12;
    }

    #choice-dialog {
        width: 70%;
        height: auto;
        max-height: 80%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #delegate-dialog {
        width: 82%;
        height: auto;
        max-height: 86%;
        border: round #38bdf8;
        background: $surface;
        padding: 1 2;
    }

    #delegate-title {
        height: auto;
        color: #e2e8f0;
    }

    #delegate-subtitle {
        height: auto;
        margin-bottom: 1;
        color: #94a3b8;
    }

    #delegate-tasks {
        height: auto;
        max-height: 20;
        padding: 0 1 0 0;
    }

    #delegate-tasks-content {
        width: 1fr;
        height: auto;
    }

    .delegate-card {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        padding: 1 2;
        border: round #334155;
        background: #111827;
    }

    .delegate-card-heading {
        width: 1fr;
        height: auto;
        color: #7dd3fc;
    }

    .delegate-card-summary {
        width: 1fr;
        height: auto;
        margin-top: 1;
        color: #cbd5e1;
    }

    #delegate-action-help {
        height: auto;
        margin-top: 1;
        color: #94a3b8;
    }

    #delegate-actions {
        height: 3;
        margin-top: 1;
    }

    .delegate-action {
        width: 1fr;
        margin: 0 1;
    }

    #layout-dialog {
        width: 76;
        height: auto;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-dialog {
        width: 88%;
        height: auto;
        max-height: 86%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-config-dialog {
        width: 76;
        height: auto;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    .memory-config-label {
        height: 1;
        margin-top: 1;
    }

    .memory-config-input {
        margin-top: 0;
    }

    #memory-config-actions {
        height: 3;
        margin-top: 1;
    }

    .memory-config-button {
        width: 1fr;
        margin: 0 1;
    }

    #memory-list {
        height: 12;
        max-height: 12;
    }

    #memory-detail {
        height: 12;
        min-height: 1;
        margin-top: 1;
        border: round #3b82f6;
        padding: 0 1;
    }

    #layout-columns {
        height: auto;
    }

    .layout-column {
        width: 1fr;
        height: auto;
    }

    .layout-button {
        width: 100%;
        margin: 0 1;
    }

    #layout-actions {
        height: 3;
        margin-top: 1;
    }

    .layout-action-button {
        width: 1fr;
        margin: 0 1;
    }

    #choice-title {
        height: auto;
        margin-bottom: 0;
    }

    #choice-list {
        height: auto;
        max-height: 16;
    }

    #choice-list > ListItem {
        height: auto;
        margin-bottom: 1;
    }

    #choice-list > ListItem > Label {
        width: 1fr;
    }

    #custom-hint {
        height: auto;
        margin-top: 1;
        padding-top: 1;
        border-top: solid #555555;
        color: #aaaaaa;
    }

    #custom-input {
        margin-top: 1;
    }

    #custom-actions {
        height: 3;
        margin-top: 1;
    }

    #custom-cancel {
        width: 14;
    }

    #model-form-dialog {
        width: 72;
        height: auto;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    .model-form-label {
        height: 1;
        margin-top: 1;
    }

    .model-form-input {
        margin-top: 0;
    }

    #model-form-hint {
        height: auto;
        color: #aaaaaa;
        margin-top: 1;
    }

    #model-confirm {
        width: 14;
    }
    """

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("v", "preview", "Preview", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(
        self,
        title: str,
        options: list[str],
        allow_custom: bool = False,
        delete_handler: Callable[[str], None] | None = None,
        preview_handler: Callable[[str], tuple[str, RenderableType]] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._allow_custom = allow_custom
        self._delete_handler = delete_handler
        self._preview_handler = preview_handler
        self._pending_delete_index: int | None = None

    def _title_text(self) -> str:
        hints = []
        if self._preview_handler is not None:
            hints.append("v 预览选中项")
        if self._delete_handler is not None:
            hints.append("d 删除选中项 · y 确认删除 · n 取消删除")
        return f"{self._title}\n{' · '.join(hints)}" if hints else self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield ModalHeader(self._title_text(), title_id="choice-title", markup=False)
            if self._options:
                yield ListView(*[ListItem(Label(option, markup=False)) for option in self._options], id="choice-list")
            if self._allow_custom:
                yield Label("自定义输入（Enter 提交，q 取消）", id="custom-hint")
                yield Input(placeholder="输入自定义选项", id="custom-input")
                with Horizontal(id="custom-actions"):
                    yield Button("取消", id="custom-cancel", variant="warning")

    def on_mount(self) -> None:
        if self._options:
            choice_list = self.query_one("#choice-list", ListView)
            choice_list.index = 0
            choice_list.focus()
        elif self._allow_custom:
            self.query_one("#custom-input", Input).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._pending_delete_index is not None:
            return
        self.dismiss(self._options[event.list_view.index or 0])

    def _on_key(self, event: Key) -> None:
        if self._preview_handler is not None and not isinstance(self.focused, Input):
            if event.key == "v":
                self.action_preview()
                event.stop()
                event.prevent_default()
                return
        if self._delete_handler is not None and not isinstance(self.focused, Input):
            key_actions = {
                "d": self.action_delete,
                "y": self.action_confirm_delete,
                "n": self.action_cancel_delete,
            }
            action = key_actions.get(event.key)
            if action is not None:
                action()
                event.stop()
                event.prevent_default()
                return
        if event.key == "enter":
            self.action_confirm()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and not isinstance(self.focused, Input):
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else "<empty_input>")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "custom-cancel":
            self.action_cancel()
            return

    def action_confirm(self) -> None:
        if self._pending_delete_index is not None:
            return
        focused = self.focused
        if isinstance(focused, Input):
            value = focused.value.strip()
            self.dismiss(value if value else "<empty_input>")
            return
        if self._options:
            choice_list = self.query_one("#choice-list", ListView)
            index = choice_list.index if choice_list.index is not None else 0
            self.dismiss(self._options[index])
        elif self._allow_custom:
            value = self.query_one("#custom-input", Input).value.strip()
            self.dismiss(value if value else "<empty_input>")

    def action_preview(self) -> None:
        if self._preview_handler is None or not self._options or self._pending_delete_index is not None:
            return
        choice_list = self.query_one("#choice-list", ListView)
        index = choice_list.index if choice_list.index is not None else 0
        try:
            title, content = self._preview_handler(self._options[index])
        except Exception as exc:
            title = "预览失败"
            content = Text(f"无法读取选中项：{exc}", style="bold red")
        self.app.push_screen(InfoPanelModal(title, content))

    def action_delete(self) -> None:
        if self._delete_handler is None or not self._options:
            return
        choice_list = self.query_one("#choice-list", ListView)
        index = choice_list.index if choice_list.index is not None else 0
        self._pending_delete_index = index
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除选中项？\n"
            f"{self._options[index]}\n"
            "该操作不可撤销。按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._delete_handler is None or self._pending_delete_index is None:
            return
        index = self._pending_delete_index
        option = self._options[index]
        try:
            self._delete_handler(option)
        except Exception as exc:
            self.query_one("#choice-title", Label).update(
                f"❌ 删除失败：{exc}\n按 n 返回面板。"
            )
            return

        self._options.pop(index)
        self._pending_delete_index = None
        if not self._options:
            self.dismiss("<empty>")
            return

        choice_list = self.query_one("#choice-list", ListView)
        choice_list.clear()

        def _mount_rows() -> None:
            choice_list.extend(ListItem(Label(option, markup=False)) for option in self._options)
            choice_list.index = min(index, len(self._options) - 1)
            choice_list.focus()
            self.query_one("#choice-title", Label).update(self._title_text())

        self.call_after_refresh(_mount_rows)

    def action_cancel_delete(self) -> None:
        if self._pending_delete_index is None:
            return
        index = self._pending_delete_index
        self._pending_delete_index = None
        self.query_one("#choice-title", Label).update(self._title_text())
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = index
        choice_list.focus()

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")


class DelegateTasksModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, tasks: list[dict[str, str]]) -> None:
        super().__init__()
        self._tasks = tasks

    def compose(self) -> ComposeResult:
        with Vertical(id="delegate-dialog"):
            yield ModalHeader("子智能体委派确认", title_id="delegate-title", markup=False)
            yield Label(
                f"本批包含 {len(self._tasks)} 个独立任务。请选择由子智能体并行处理，或交回主智能体顺序执行。",
                id="delegate-subtitle",
                markup=False,
            )
            with VerticalScroll(id="delegate-tasks"):
                with Vertical(id="delegate-tasks-content"):
                    for task in self._tasks:
                        with Vertical(classes="delegate-card"):
                            yield Label(
                                f"TASK #{task['task_id']}  ·  {task['role_name']}",
                                classes="delegate-card-heading",
                                markup=False,
                            )
                            yield Label(
                                task["summary"],
                                classes="delegate-card-summary",
                                markup=False,
                            )
            yield Label(
                "批准委派：并行启动子智能体  ·  主智能体执行：不启动子智能体  ·  取消：停止本次操作",
                id="delegate-action-help",
                markup=False,
            )
            with Horizontal(id="delegate-actions"):
                yield Button("批准委派", id="delegate-approve", variant="success", classes="delegate-action")
                yield Button("主智能体执行", id="delegate-orchestrator", variant="primary", classes="delegate-action")
                yield Button("取消", id="delegate-cancel", variant="warning", classes="delegate-action")

    def on_mount(self) -> None:
        self.query_one("#delegate-approve", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "delegate-approve": "approve",
            "delegate-orchestrator": "orchestrator",
            "delegate-cancel": "cancel",
        }
        action = actions.get(event.button.id)
        if action:
            self.dismiss(action)

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class StartupWorkdirModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd
        self._selected_index = 0
        self._mode = "select"
        self._ignore_initial_custom_submit = False

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-dialog"):
            yield ModalHeader("", title_id="startup-title")
            yield Input(placeholder="输入自定义工作区路径", id="startup-input")

    def on_mount(self) -> None:
        custom_input = self.query_one("#startup-input", Input)
        custom_input.display = False
        self._refresh_text()

    def _on_key(self, event: Key) -> None:
        if event.key == "ctrl+c":
            event.stop()
            event.prevent_default()
            self.dismiss("abort")
            return
        if self._mode != "select":
            return
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self._selected_index = max(0, self._selected_index - 1)
            self._refresh_text()
            return
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self._selected_index = min(1, self._selected_index + 1)
            self._refresh_text()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self._selected_index == 0:
                self.dismiss("default")
                return
            self._mode = "custom"
            self._ignore_initial_custom_submit = True
            custom_input = self.query_one("#startup-input", Input)
            custom_input.display = True
            custom_input.focus()
            self.query_one("#startup-title", Label).update(
                "📂 输入自定义工作区路径（Enter 确认，Ctrl+C 取消）："
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "startup-input":
            return
        event.stop()
        if self._ignore_initial_custom_submit and not event.value:
            self._ignore_initial_custom_submit = False
            return
        self._ignore_initial_custom_submit = False
        self.dismiss(f"custom:{event.value}")

    def _refresh_text(self) -> None:
        options = [
            f"当前目录 ({self.cwd})",
            "输入自定义路径...",
        ]
        lines = ["📂 选择工作区目录（使用 ↑/↓ 方向键，Enter 确认，Ctrl+C 取消）：", ""]
        for index, text in enumerate(options):
            marker = "❯" if index == self._selected_index else " "
            lines.append(f"  {marker} {text}")
        self.query_one("#startup-title", Label).update("\n".join(lines))

    def action_cancel(self) -> None:
        self.dismiss("abort")


class TaskPanelModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
    ]

    def __init__(self, task_manager: Any) -> None:
        super().__init__()
        self._task_manager = task_manager

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            yield ModalHeader(self._title_text(), title_id="task-title")
            yield DataTable(id="task-table", cursor_type="row")
            with Horizontal(id="task-actions"):
                yield Button("关闭", id="task-close", variant="primary")

    def _title_text(self) -> str:
        return "当前任务计划（只读）\nq 关闭"

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("ID", "Subject", "Status", "Runnable")
        self._reload_rows()
        table.focus()

    def _reload_rows(self) -> None:
        table = self.query_one("#task-table", DataTable)
        previous_row = table.cursor_row
        table.clear(columns=False)
        rows = self._task_manager.get_task_table().get("rows", [])
        for row in rows:
            table.add_row(
                str(row["id"]),
                row["subject"],
                row["status"],
                "✓" if row.get("is_runnable") else "",
                key=str(row["id"]),
            )
        if rows:
            table.move_cursor(row=min(previous_row, len(rows) - 1), column=0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "task-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss("closed")


class ToolHistoryModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS
    BINDINGS: list[Binding] = []

    _STATUS_OPTIONS = [
        ("全部状态", ""),
        ("执行中", TOOL_STATUS_RUNNING),
        ("成功", TOOL_STATUS_SUCCEEDED),
        ("失败", TOOL_STATUS_FAILED),
        ("已阻止", TOOL_STATUS_BLOCKED),
        ("已压缩", TOOL_STATUS_COMPACTED),
        ("结果缺失", TOOL_STATUS_INCOMPLETE),
    ]
    _SOURCE_OPTIONS = [
        ("全部来源", ""),
        ("Orchestrator", "orchestrator"),
        ("记忆代理", "memory"),
        ("Sub-Agent", "sub_agent"),
    ]
    _STATUS_MARKERS = {
        TOOL_STATUS_RUNNING: "…",
        TOOL_STATUS_SUCCEEDED: "✓",
        TOOL_STATUS_FAILED: "✗",
        TOOL_STATUS_BLOCKED: "⊘",
        TOOL_STATUS_COMPACTED: "≈",
        TOOL_STATUS_INCOMPLETE: "?",
    }

    def __init__(self, history: ToolExecutionHistory) -> None:
        super().__init__()
        self._history = history
        self._view = "timeline"
        self._tool_filter = ""
        self._row_values: list[ToolExecutionRecord | ToolExecutionSummary] = []
        self._last_signature: tuple[Any, ...] | None = None
        self._compact = False
        self._detail_open = False

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-history-dialog"):
            yield ModalHeader(self._title_text(), title_id="tool-history-title")
            with Horizontal(id="tool-history-filter-row"):
                yield Input(placeholder="搜索工具名、参数、结果、错误或执行者…", id="tool-history-search")
                yield Select(self._STATUS_OPTIONS, value="", allow_blank=False, id="tool-history-status-filter", classes="tool-history-filter")
                yield Select(self._SOURCE_OPTIONS, value="", allow_blank=False, id="tool-history-source-filter", classes="tool-history-filter")
            with Horizontal(id="tool-history-content"):
                yield ListView(id="tool-history-list")
                yield TextArea(
                    "",
                    id="tool-history-detail",
                    read_only=True,
                    show_line_numbers=False,
                    soft_wrap=True,
                )
            yield Label("", id="tool-history-status")

    def on_mount(self) -> None:
        self._apply_responsive_layout(self.app.size.width)
        self._reload_rows(force=True)
        self.set_interval(0.5, self._refresh_if_changed)
        self.query_one("#tool-history-search", Input).focus()

    def on_resize(self, event: Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def _apply_responsive_layout(self, width: int) -> None:
        compact = width < 100
        if compact == self._compact and self._last_signature is not None:
            return
        self._compact = compact
        self.query_one("#tool-history-filter-row", Horizontal).set_class(compact, "compact")
        self.query_one("#tool-history-content", Horizontal).set_class(compact, "compact")
        self._update_compact_visibility()

    def _update_compact_visibility(self) -> None:
        history_list = self.query_one("#tool-history-list", ListView)
        detail = self.query_one("#tool-history-detail", TextArea)
        history_list.set_class(self._compact and self._detail_open, "tool-history-hidden")
        detail.set_class(self._compact and not self._detail_open, "tool-history-hidden")

    def _title_text(self) -> str:
        records = self._history.snapshot()
        failed = sum(record.status == TOOL_STATUS_FAILED for record in records)
        tool_count = len({record.tool_name for record in records})
        view = "时间线" if self._view == "timeline" else "按工具汇总"
        tool_filter = f" · 工具: {self._tool_filter}" if self._tool_filter else ""
        return (
            f"🧰 工具执行历史 · {len(records)} 次 · {tool_count} 种工具 · 失败 {failed} 次 · {view}{tool_filter}\n"
            "/ 搜索 · t 切换视图 · Enter 查看详情 · c 复制详情 · Esc 返回 · q 关闭"
        )

    def _filter_values(self) -> tuple[str, str, str]:
        text = self.query_one("#tool-history-search", Input).value.strip()
        status_value = self.query_one("#tool-history-status-filter", Select).value
        source_value = self.query_one("#tool-history-source-filter", Select).value
        status = status_value if isinstance(status_value, str) else ""
        source = source_value if isinstance(source_value, str) else ""
        return text, status, source

    def _current_signature(self) -> tuple[Any, ...]:
        records = self._history.snapshot()
        record_states = tuple(
            (record.sequence, record.status, record.finished_at)
            for record in records
        )
        return (
            record_states,
            self._view,
            self._tool_filter,
            *self._filter_values(),
        )

    def _refresh_if_changed(self) -> None:
        if self._current_signature() != self._last_signature:
            self._reload_rows()

    def _reload_rows(self, *, force: bool = False) -> None:
        signature = self._current_signature()
        if not force and signature == self._last_signature:
            return
        self._last_signature = signature
        selected_row = self._current_row()
        selected_key = self._row_key(selected_row) if selected_row is not None else None
        text, status, source = self._filter_values()
        if self._view == "summary":
            self._row_values = list(self._history.summaries(text=text, status=status, source=source))
        else:
            self._row_values = list(
                self._history.query(
                    text=text,
                    tool_name=self._tool_filter,
                    status=status,
                    source=source,
                )
            )
        selected_index = next(
            (
                index
                for index, item in enumerate(self._row_values)
                if self._row_key(item) == selected_key
            ),
            0,
        )

        history_list = self.query_one("#tool-history-list", ListView)
        history_list.clear()
        labels = [self._row_label(item) for item in self._row_values]
        if not labels:
            labels = ["暂无匹配的工具执行记录"]

        def _mount_rows() -> None:
            history_list.extend(ListItem(Label(label, markup=False)) for label in labels)
            history_list.index = min(selected_index, len(labels) - 1)
            self.query_one("#tool-history-title", Label).update(self._title_text())
            self.query_one("#tool-history-status", Label).update(
                f"当前显示 {len(self._row_values)} 项"
            )
            self._update_detail()

        self.call_after_refresh(_mount_rows)

    def _selected_index(self) -> int:
        history_list = self.query_one("#tool-history-list", ListView)
        return history_list.index if history_list.index is not None else 0

    def _current_row(self) -> ToolExecutionRecord | ToolExecutionSummary | None:
        if not self._row_values:
            return None
        return self._row_values[min(self._selected_index(), len(self._row_values) - 1)]

    def _row_label(self, item: ToolExecutionRecord | ToolExecutionSummary) -> str:
        if isinstance(item, ToolExecutionSummary):
            return (
                f"{item.tool_name} · {item.total} 次\n"
                f"    ✓ {item.succeeded}  ✗ {item.failed}  ⊘ {item.blocked}  … {item.running}"
            )
        marker = self._STATUS_MARKERS.get(item.status, "?")
        timestamp = f"{item.started_at[11:19]} " if item.started_at else ""
        duration = f" · {item.duration_ms}ms" if item.duration_ms is not None else ""
        actor = item.actor or item.source
        return f"{timestamp}{marker} {item.tool_name}{duration}\n    {actor} · {item.status}"

    @staticmethod
    def _row_key(item: ToolExecutionRecord | ToolExecutionSummary) -> str:
        if isinstance(item, ToolExecutionSummary):
            return f"summary:{item.tool_name}"
        return f"execution:{item.execution_id}"

    def _update_detail(self) -> None:
        detail = self.query_one("#tool-history-detail", TextArea)
        current = self._current_row()
        if current is None:
            detail.load_text("暂无详情。")
            return
        if isinstance(current, ToolExecutionSummary):
            detail.load_text(
                "\n".join(
                    [
                        f"工具: {current.tool_name}",
                        f"调用总数: {current.total}",
                        f"成功: {current.succeeded}",
                        f"失败: {current.failed}",
                        f"已阻止: {current.blocked}",
                        f"执行中: {current.running}",
                        "",
                        "按 Enter 查看该工具的执行时间线。",
                    ]
                )
            )
            return

        lines = [
            f"工具: {current.tool_name}",
            f"状态: {current.status}",
            f"来源: {current.source}",
            f"执行者: {current.actor}",
        ]
        if current.task_id:
            lines.append(f"任务 ID: {current.task_id}")
        lines.append(f"调用 ID: {current.tool_call_id or '-'}")
        if current.started_at:
            lines.append(f"开始: {current.started_at}")
        if current.finished_at:
            lines.append(f"结束: {current.finished_at}")
        if current.duration_ms is not None:
            lines.append(f"耗时: {current.duration_ms} ms")
        lines.extend([
            "",
            "Arguments",
            "─────────",
            format_tool_arguments(current.arguments),
            "",
            "Result",
            "──────",
            format_tool_value(current.result),
        ])
        if current.error:
            lines.extend(["", "Error", "─────", current.error])
        detail.load_text("\n".join(lines))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tool-history-search":
            self._last_signature = None
            self._reload_rows()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"tool-history-status-filter", "tool-history-source-filter"}:
            self._last_signature = None
            self._reload_rows()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "tool-history-list":
            self._update_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "tool-history-list":
            self.action_open_detail()

    def _on_key(self, event: Key) -> None:
        if event.key == "c" and isinstance(self.focused, TextArea):
            detail = self.query_one("#tool-history-detail", TextArea)
            text_to_copy = detail.selected_text or detail.text
            if copy_to_system_clipboard(text_to_copy):
                status = "📋 已复制工具详情到系统剪贴板。"
            else:
                self.app.copy_to_clipboard(text_to_copy)
                status = "📋 已发送复制请求；当前终端可能不支持系统剪贴板。"
            self.query_one("#tool-history-status", Label).update(status)
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and isinstance(self.focused, (Input, Select)):
            return
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            self.action_back()
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter" and not isinstance(self.focused, (Input, Select)):
            self.action_open_detail()
            event.stop()
            event.prevent_default()
            return
        if event.key == "t" and not isinstance(self.focused, (Input, Select)):
            self.action_toggle_view()
            event.stop()
            event.prevent_default()
            return
        if event.key == "/" and not isinstance(self.focused, Input):
            self.action_focus_search()
            event.stop()
            event.prevent_default()

    def action_open_detail(self) -> None:
        current = self._current_row()
        if isinstance(current, ToolExecutionSummary):
            self._tool_filter = current.tool_name
            self._view = "timeline"
            self._detail_open = False
            self._last_signature = None
            self._reload_rows()
            return
        if current is None:
            return
        self._detail_open = True
        self._update_compact_visibility()
        self.query_one("#tool-history-detail", TextArea).focus()

    def action_back(self) -> None:
        if self._compact and self._detail_open:
            self._detail_open = False
            self._update_compact_visibility()
            self.query_one("#tool-history-list", ListView).focus()
            return
        if self._tool_filter:
            self._tool_filter = ""
            self._last_signature = None
            self._reload_rows()
            return
        self.query_one("#tool-history-list", ListView).focus()

    def action_toggle_view(self) -> None:
        self._view = "summary" if self._view == "timeline" else "timeline"
        self._tool_filter = ""
        self._detail_open = False
        self._last_signature = None
        self._reload_rows()

    def action_focus_search(self) -> None:
        self.query_one("#tool-history-search", Input).focus()

    def action_close(self) -> None:
        self.dismiss("closed")


class SkillsConfigModal(ClosableModalScreen[str | dict[str, Any]]):
    CSS = ChoiceModal.CSS
    BINDINGS: list[Binding] = []

    _STATUS_OPTIONS = [
        ("全部状态", "all"),
        ("已启用", "enabled"),
        ("已禁用", "disabled"),
    ]

    def __init__(self, skill_loader: Any) -> None:
        super().__init__()
        self._skill_loader = skill_loader
        self._entries: list[dict[str, Any]] = []
        self._filtered_entries: list[dict[str, Any]] = []
        self._original_states: dict[str, bool] = {}
        self._draft_states: dict[str, bool] = {}
        self._row_reload_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="skills-dialog"):
            yield ModalHeader(self._title_text(), title_id="skills-title")
            with Horizontal(id="skills-filter-row"):
                yield Input(
                    placeholder="按技能名称或描述搜索…",
                    id="skills-search",
                )
                yield Select(
                    self._STATUS_OPTIONS,
                    value="all",
                    allow_blank=False,
                    id="skills-status-filter",
                )
            yield ListView(id="skills-list")
            yield Label("Enter/Space 修改草稿；/ 聚焦搜索；q 取消。", id="skills-status")
            with Horizontal(id="skills-actions"):
                yield Button("确认应用", id="skills-confirm", variant="success")
                yield Button("取消", id="skills-close", variant="primary")

    def on_mount(self) -> None:
        self._entries = self._skill_loader.get_skill_entries()
        self._original_states = {
            entry["name"]: bool(entry["enabled"])
            for entry in self._entries
        }
        self._draft_states = dict(self._original_states)
        self._reload_rows()
        self.query_one("#skills-search", Input).focus()

    def _title_text(self) -> str:
        enabled = sum(self._draft_states.values())
        disabled = len(self._draft_states) - enabled
        return f"📚 Skills 配置 · 全部 {len(self._entries)} · 已启用 {enabled} · 已禁用 {disabled}"

    def _filter_values(self) -> tuple[str, str]:
        search = self.query_one("#skills-search", Input).value.strip().casefold()
        status_value = self.query_one("#skills-status-filter", Select).value
        status = status_value if isinstance(status_value, str) else "all"
        return search, status

    def _current_entry(self) -> dict[str, Any] | None:
        choice_list = self.query_one("#skills-list", ListView)
        if choice_list.index is None or choice_list.index >= len(self._filtered_entries):
            return None
        return self._filtered_entries[choice_list.index]

    def _draft_changes(self) -> dict[str, bool]:
        return {
            name: enabled
            for name, enabled in self._draft_states.items()
            if enabled != self._original_states[name]
        }

    def _change_counts(self) -> tuple[int, int]:
        changes = self._draft_changes()
        enabled = sum(changes.values())
        return enabled, len(changes) - enabled

    @staticmethod
    def _entry_label(entry: dict[str, Any]) -> str:
        marker = "[✓]" if entry["enabled"] else "[×]"
        status = "已启用" if entry["enabled"] else "已禁用"
        description = " ".join(str(entry["description"]).split())
        return f"{marker} {entry['name']} · {status}\n    {description}"

    def _refresh_summary(self) -> None:
        self.query_one("#skills-title", Label).update(self._title_text())
        enabled_count, disabled_count = self._change_counts()
        change_count = enabled_count + disabled_count
        confirm = self.query_one("#skills-confirm", Button)
        confirm.display = change_count > 0
        confirm.label = f"确认应用（启用 {enabled_count}，禁用 {disabled_count}）"
        if change_count:
            change_text = f"待确认：将启用 {enabled_count} 个，禁用 {disabled_count} 个。"
        else:
            change_text = "尚未修改。"
        self.query_one("#skills-status", Label).update(
            f"显示 {len(self._filtered_entries)}/{len(self._entries)} 个技能。{change_text}"
            " Enter/Space 修改草稿；q 取消。"
        )

    def _reload_rows(self) -> None:
        selected = self._current_entry()
        selected_name = selected["name"] if selected is not None else None
        entries = [
            {**entry, "enabled": self._draft_states[entry["name"]]}
            for entry in self._entries
        ]
        search, status = self._filter_values()
        self._filtered_entries = [
            entry
            for entry in entries
            if (
                status == "all"
                or (status == "enabled" and entry["enabled"])
                or (status == "disabled" and not entry["enabled"])
            )
            and (
                not search
                or search in entry["name"].casefold()
                or search in entry["description"].casefold()
            )
        ]
        self._refresh_summary()
        choice_list = self.query_one("#skills-list", ListView)
        self._row_reload_generation += 1
        reload_generation = self._row_reload_generation
        choice_list.clear()
        labels = [self._entry_label(entry) for entry in self._filtered_entries]

        def _mount_rows() -> None:
            if reload_generation != self._row_reload_generation:
                return
            if labels:
                choice_list.extend(ListItem(Label(label, markup=False)) for label in labels)
                names = [entry["name"] for entry in self._filtered_entries]
                choice_list.index = names.index(selected_name) if selected_name in names else 0

        self.call_after_refresh(_mount_rows)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "skills-search":
            self._reload_rows()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "skills-status-filter":
            self._reload_rows()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "skills-list":
            self.action_toggle_skill()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "skills-confirm":
            self.action_confirm()
        elif event.button.id == "skills-close":
            self.action_close()

    def _on_key(self, event: Key) -> None:
        if event.key == "q" and not isinstance(self.focused, (Input, Select)):
            self.action_close()
        elif event.key == "/" and not isinstance(self.focused, Input):
            self.query_one("#skills-search", Input).focus()
        elif event.key == "space" and isinstance(self.focused, ListView):
            self.action_toggle_skill()
        else:
            return
        event.stop()
        event.prevent_default()

    def action_toggle_skill(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        choice_list = self.query_one("#skills-list", ListView)
        index = choice_list.index
        if index is None:
            return
        name = entry["name"]
        enabled = not self._draft_states[name]
        self._draft_states[name] = enabled
        updated_entry = {**entry, "enabled": enabled}
        _, status = self._filter_values()
        if status == "all":
            self._filtered_entries[index] = updated_entry
            choice_list.children[index].query_one(Label).update(
                self._entry_label(updated_entry)
            )
        else:
            self._filtered_entries.pop(index)
            choice_list.remove_items([index]).call_next(self)
        self._refresh_summary()

    def action_confirm(self) -> None:
        changes = self._draft_changes()
        if not changes:
            return
        enabled_count, disabled_count = self._change_counts()
        try:
            self._skill_loader.apply_skill_enabled_states(changes)
        except (OSError, UnicodeError, ValueError) as exc:
            self.query_one("#skills-status", Label).update(f"保存失败：{exc}")
            return
        self.dismiss({
            "action": "applied",
            "enabled": enabled_count,
            "disabled": disabled_count,
        })

    def action_close(self) -> None:
        self.dismiss("closed")


class InfoPanelModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
    ]

    def __init__(self, title: str, content: RenderableType) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="info-dialog"):
            yield ModalHeader(f"{self._title}\nq 关闭。", title_id="choice-title")
            yield RichLog(id="info-content", markup=True, wrap=True, min_width=1)
            with Horizontal(id="info-actions"):
                yield Button("关闭", id="info-close", variant="primary")

    def on_mount(self) -> None:
        content = self.query_one("#info-content", RichLog)
        content.write(self._content, expand=True, shrink=True, scroll_end=False)
        content.focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "info-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss("closed")


class CopyContentModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
    ]

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        super().__init__()
        self._messages = messages
        self._text = self._build_text()

    def compose(self) -> ComposeResult:
        question_count = sum(
            message.get("role") == "user" and bool(self._content_text(message))
            for message in self._messages
        )
        answer_count = sum(
            message.get("role") == "assistant" and bool(self._content_text(message))
            for message in self._messages
        )
        terminal_input_count = sum(
            len(self._terminal_commands(message)) for message in self._messages
        )
        terminal_output_count = sum(
            message.get("role") in {"tool", "function"}
            and message.get("name") == "RunTerminalCommand"
            and (
                message.get("content") is not None
                or message.get("output") is not None
            )
            for message in self._messages
        )
        with Vertical(id="copy-dialog"):
            yield ModalHeader(
                "对话内容导出\nc 复制选区（无选区则复制全部） · a 复制全部 · q 关闭",
                title_id="copy-title",
            )
            yield Label(
                f"提问 {question_count} · 回答 {answer_count} · "
                f"终端输入 {terminal_input_count} · 输出 {terminal_output_count} · "
                f"{len(self._text)} 字符",
                id="copy-summary",
            )
            yield TextArea(
                self._text,
                id="copy-text",
                read_only=True,
                show_line_numbers=False,
                soft_wrap=True,
            )
            yield Label("选择正文后可复制选区，也可直接复制全部内容。", id="copy-status")
            with Horizontal(id="copy-actions"):
                yield Button("复制选中", id="copy-selection", variant="primary")
                yield Button("复制全部", id="copy-all", variant="success")
                yield Button("关闭", id="copy-close", variant="warning")

    def on_mount(self) -> None:
        text_area = self.query_one("#copy-text", TextArea)
        text_area.focus()
        last_line = text_area.document.line_count - 1
        last_col = len(text_area.document.get_line(last_line)) if last_line >= 0 else 0
        text_area.move_cursor((last_line, last_col))
        text_area.scroll_cursor_visible()

    def _on_key(self, event: Key) -> None:
        if event.key == "c":
            self._copy_selected_or_all()
            event.stop()
            event.prevent_default()
            return
        if event.key == "a":
            self._copy_all()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            text_area = self.query_one("#copy-text", TextArea)
            if text_area.selected_text:
                return
            self.action_close()
            event.stop()
            event.prevent_default()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy-selection":
            self._copy_selection()
        elif event.button.id == "copy-all":
            self._copy_all()
        elif event.button.id == "copy-close":
            self.action_close()

    def _copy_selected_or_all(self) -> None:
        text_area = self.query_one("#copy-text", TextArea)
        selected = text_area.selected_text
        self._copy_text(selected or text_area.text, "选中文本" if selected else "全部内容")

    def _copy_selection(self) -> None:
        selected = self.query_one("#copy-text", TextArea).selected_text
        if not selected:
            self.query_one("#copy-status", Label).update("请先在正文中选择要复制的文本。")
            return
        self._copy_text(selected, "选中文本")

    def _copy_all(self) -> None:
        self._copy_text(self.query_one("#copy-text", TextArea).text, "全部内容")

    def _copy_text(self, text: str, scope: str) -> None:
        if copy_to_system_clipboard(text):
            status = f"已复制{scope}（{len(text)} 个字符）到系统剪贴板。"
        else:
            self.app.copy_to_clipboard(text)
            status = f"已发送{scope}（{len(text)} 个字符）复制请求；当前终端可能不支持系统剪贴板。"
        self.query_one("#copy-status", Label).update(status)

    def action_close(self) -> None:
        self.dismiss("closed")

    @staticmethod
    def _content_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            chunks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text")
            ]
            if chunks:
                return "\n\n".join(chunks)
        return "".join(
            block.get("text", "")
            for block in message.get("content_blocks") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )

    @staticmethod
    def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return [call for call in tool_calls if isinstance(call, dict)]
        return [
            block
            for block in message.get("content_blocks") or []
            if isinstance(block, dict) and block.get("type") == "tool_call"
        ]

    @staticmethod
    def _tool_call_parts(tool_call: dict[str, Any]) -> tuple[str, Any]:
        function = tool_call.get("function")
        if isinstance(function, dict):
            return function.get("name", ""), function.get("arguments", "")
        return tool_call.get("name", ""), tool_call.get("arguments", "")

    @classmethod
    def _terminal_commands(cls, message: dict[str, Any]) -> list[str]:
        commands: list[str] = []
        for tool_call in cls._tool_calls(message):
            name, arguments = cls._tool_call_parts(tool_call)
            if name != "RunTerminalCommand":
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict):
                continue
            command = arguments.get("command")
            if isinstance(command, str) and command:
                commands.append(command)
        return commands

    def _build_text(self) -> str:
        parts: list[str] = []
        user_number = 0
        assistant_number = 0
        terminal_input_number = 0
        terminal_output_number = 0
        for message in self._messages:
            role = message.get("role", "")
            if role == "user":
                content = self._content_text(message)
                if content:
                    user_number += 1
                    parts.append(f"──────── User · {user_number} ────────\n{content}")
                continue

            if role == "assistant":
                content = self._content_text(message)
                if content:
                    assistant_number += 1
                    parts.append(f"──── Assistant · {assistant_number} ────\n{content}")
                for command in self._terminal_commands(message):
                    terminal_input_number += 1
                    parts.append(
                        f"──── Terminal Input · {terminal_input_number} ────\n$ {command}"
                    )
                continue

            if role in {"tool", "function"} and message.get("name") == "RunTerminalCommand":
                output = message.get("content")
                if output is None:
                    output = message.get("output")
                if output is None:
                    continue
                terminal_output_number += 1
                output_text = output if isinstance(output, str) else format_tool_value(output)
                parts.append(
                    f"──── Terminal Output · {terminal_output_number} ────\n"
                    f"{output_text}"
                )
        return "\n\n".join(parts)


class McpSwitchModal(ClosableModalScreen[str | dict]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "confirm_or_toggle", "Toggle/Confirm", priority=True),
        Binding("space", "toggle", "Toggle", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, server_switches: list[dict[str, Any]], mcp_manager: Any) -> None:
        super().__init__()
        self._server_switches = server_switches
        self._mcp_manager = mcp_manager
        self._draft_states = {item["name"]: bool(item["disabled"]) for item in server_switches}
        self._pending_delete_name: str | None = None
        self._pending_delete_index: int | None = None
        self._deleted_results: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield ModalHeader(self._title_text(), title_id="choice-title")
            yield ListView(*[ListItem(Label(label)) for label in self._labels()], id="choice-list")

    def on_mount(self) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def _title_text(self) -> str:
        return "🔀 MCP 服务开关面板\n选择服务可切换启用/禁用；d 删除选中服务；选择确认应用保存。"

    def _reset_title(self) -> None:
        self.query_one("#choice-title", Label).update(self._title_text())

    def _labels(self) -> list[str]:
        choices = []
        for item in self._server_switches:
            choices.append(self._server_label(item))
        choices.extend(["确认应用", "取消"])
        return choices

    def _server_label(self, item: dict[str, Any]) -> str:
        name = item["name"]
        enabled = not self._draft_states[name]
        loaded = item.get("loaded", False)
        switch_box = "[√]" if enabled else "[×]"
        runtime_txt = "已加载" if loaded else "未加载"
        status_txt = "启用" if enabled else "禁用"
        return f"{switch_box} {name}    当前草稿: {status_txt}    运行态: {runtime_txt}"

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _refresh_server_row(self, index: int) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._server_label(self._server_switches[index]))
        choice_list.index = index
        choice_list.focus()

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_name = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.clear()

        def _mount_rows() -> None:
            labels = self._labels()
            choice_list.extend(ListItem(Label(label)) for label in labels)
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            choice_list.focus()

        self.call_after_refresh(_mount_rows)

    def _dismiss_payload(self, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "disabled_updates": dict(self._draft_states),
            "deleted_results": list(self._deleted_results),
        }

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_confirm_or_toggle()

    def _on_key(self, event: Key) -> None:
        key_actions = {
            "enter": self.action_confirm_or_toggle,
            "space": self.action_toggle,
            "d": self.action_delete,
            "y": self.action_confirm_delete,
            "n": self.action_cancel_delete,
        }
        action = key_actions.get(event.key)
        if action is None:
            return
        action()
        event.stop()
        event.prevent_default()

    def action_confirm_or_toggle(self) -> None:
        if self._pending_delete_name is not None:
            return
        index = self._selected_index()
        if index < len(self._server_switches):
            self._toggle_index(index)
            return
        if index == len(self._server_switches):
            self.dismiss(self._dismiss_payload("confirm"))
            return
        self.dismiss(self._dismiss_payload("cancel"))

    def action_toggle(self) -> None:
        if self._pending_delete_name is not None:
            return
        index = self._selected_index()
        if index < len(self._server_switches):
            self._toggle_index(index)

    def action_delete(self) -> None:
        index = self._selected_index()
        if index >= len(self._server_switches):
            return
        server_name = self._server_switches[index]["name"]
        self._pending_delete_name = server_name
        self._pending_delete_index = index
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除 MCP 服务配置？\n"
            f"{server_name}\n"
            "该操作会写入配置文件，并停用运行中的同名服务。按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_name is None:
            return
        server_name = self._pending_delete_name
        selected_index = self._pending_delete_index or self._selected_index()
        try:
            result = self._mcp_manager.delete_server_config(server_name)
        except Exception as exc:
            self.query_one("#choice-title", Label).update(
                "❌ 删除 MCP 服务失败\n"
                f"{server_name}: {exc}\n"
                "按 n 返回面板。"
            )
            return
        self._deleted_results.append({"server": server_name, "result": result})
        self._server_switches = [item for item in self._server_switches if item["name"] != server_name]
        self._draft_states.pop(server_name, None)
        self._reload_rows(selected_index)

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index or self._selected_index()
        self._pending_delete_name = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(self._dismiss_payload("cancel"))

    def _toggle_index(self, index: int) -> None:
        name = self._server_switches[index]["name"]
        self._draft_states[name] = not self._draft_states[name]
        self._refresh_server_row(index)


class ModelPanelModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("f", "favorite", "Favorite", priority=True),
        Binding("d", "delete", "Delete", priority=True),
    ]

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield ModalHeader(self._title, title_id="choice-title")
            yield ListView(*[ListItem(Label(option)) for option in self._options], id="choice-list")

    def on_mount(self) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _dismiss_action(self, action: str) -> None:
        self.dismiss(f"{action}:{self._selected_index()}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_select()
            event.stop()
            event.prevent_default()
            return
        if event.key == "f":
            self.action_favorite()
            event.stop()
            event.prevent_default()
            return
        if event.key == "d":
            self.action_delete()
            event.stop()
            event.prevent_default()
            return

    def action_select(self) -> None:
        self._dismiss_action("select")

    def action_favorite(self) -> None:
        self._dismiss_action("favorite")

    def action_delete(self) -> None:
        self._dismiss_action("delete")

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")


class ModelManagerModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("q", "close", "Close", priority=True),
        Binding("f", "favorite", "Favorite", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("left", "decrease_effort", "Lower Effort", priority=True),
        Binding("right", "increase_effort", "Higher Effort", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, model_manager: Any) -> None:
        super().__init__()
        self._model_manager = model_manager
        self._model_keys: list[ModelKey | None] = []
        self._pending_delete_key: ModelKey | None = None
        self._pending_delete_index: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield ModalHeader(self._title_text(), title_id="choice-title")
            yield ListView(id="choice-list")

    @staticmethod
    def _title_text() -> str:
        efforts = " / ".join(REASONING_EFFORTS)
        return (
            "⚙️ 模型管理面板\n"
            f"思考档位：{efforts}\n"
            "Enter 选择当前模型并关闭；←/→ 调整档位；f 切换常用；d 删除；q 关闭。"
        )

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _model_label(self, model: Any, current_key: ModelKey | None) -> str:
        markers = []
        if model.key == current_key:
            markers.append("✓")
        if model.is_favorite:
            markers.append("♥")
        marker_text = " ".join(markers) if markers else " "
        return (
            f"[{marker_text:^3}] {model.get_display_text()}"
            f" · effort: {model.reasoning_effort} · format: {model.message_format}"
        )

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_key = None
        self._pending_delete_index = None
        self._reset_title()
        loaded = self._model_manager._reload_from_disk()
        if not loaded:
            self.query_one("#choice-title", Label).update(
                "⚠️ 模型配置读取失败，已禁止写入以避免覆盖原文件。\n"
                f"{self._model_manager.get_load_error_display()}\n"
                "请修复 model_config.json 后重新打开 /models；q 关闭。"
            )
        current_model = self._model_manager.get_current_model()
        current_key = current_model.key if current_model else None
        labels = ["➕ 添加模型"]
        keys: list[ModelKey | None] = [None]
        for model in self._model_manager.models:
            labels.append(self._model_label(model, current_key))
            keys.append(model.key)
        labels.append("退出")
        keys.append(None)
        self._model_keys = keys

        choice_list = self.query_one("#choice-list", ListView)
        choice_list.clear()

        def _mount_rows() -> None:
            choice_list.extend(ListItem(Label(label)) for label in labels)
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            choice_list.focus()

        self.call_after_refresh(_mount_rows)

    def _refresh_model_row_by_key(self, model_key: ModelKey) -> None:
        index = next((index for index, key in enumerate(self._model_keys) if key == model_key), None)
        if index is None:
            return
        model = next((model for model in self._model_manager.models if model.key == model_key), None)
        if model is None:
            return
        current_model = self._model_manager.get_current_model()
        current_key = current_model.key if current_model else None
        choice_list = self.query_one("#choice-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._model_label(model, current_key))
        choice_list.index = index
        choice_list.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_select()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()
            return
        if event.key == "f":
            self.action_favorite()
            event.stop()
            event.prevent_default()
            return
        if event.key == "d":
            self.action_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "left":
            self.action_decrease_effort()
            event.stop()
            event.prevent_default()
            return
        if event.key == "right":
            self.action_increase_effort()
            event.stop()
            event.prevent_default()
            return
        if event.key == "y":
            self.action_confirm_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "n":
            self.action_cancel_delete()
            event.stop()
            event.prevent_default()
            return

    def action_select(self) -> None:
        index = self._selected_index()
        if index == 0:
            self._add_model(index)
            return
        if index == len(self._model_keys) - 1:
            self.dismiss("exit")
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        selected_model = self._model_manager.models[target_index]
        if self._model_manager.select_model(model_key, selected_model.reasoning_effort):
            self.dismiss(
                f"selected:{selected_model.get_display_text()} · effort: {selected_model.reasoning_effort}"
                f" · format: {selected_model.message_format}"
            )

    def action_decrease_effort(self) -> None:
        self._change_reasoning_effort(-1)

    def action_increase_effort(self) -> None:
        self._change_reasoning_effort(1)

    def _change_reasoning_effort(self, offset: int) -> None:
        if self._pending_delete_key is not None:
            return
        index = self._selected_index()
        if index == 0 or index == len(self._model_keys) - 1:
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        model = self._model_manager.models[target_index]
        effort_index = REASONING_EFFORTS.index(model.reasoning_effort)
        next_index = max(0, min(effort_index + offset, len(REASONING_EFFORTS) - 1))
        if next_index == effort_index:
            return
        if self._model_manager.set_reasoning_effort(model_key, REASONING_EFFORTS[next_index]):
            self._refresh_model_row_by_key(model_key)
            if self._model_manager.current_model_key == model_key:
                self.app.refresh_status()

    def action_favorite(self) -> None:
        index = self._selected_index()
        if index == 0 or index == len(self._model_keys) - 1:
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        if self._model_manager.toggle_favorite_by_index(target_index):
            self._refresh_model_row_by_key(model_key)

    def action_delete(self) -> None:
        index = self._selected_index()
        if index == 0 or index == len(self._model_keys) - 1:
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        selected_model = self._model_manager.models[target_index]
        self._pending_delete_key = model_key
        self._pending_delete_index = index
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除模型？\n"
            f"{selected_model.get_display_text()}\n"
            "按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_key is None:
            return
        selected_index = self._pending_delete_index or self._selected_index()
        self._model_manager.delete_model_by_key(self._pending_delete_key)
        self._reload_rows(selected_index)

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index or self._selected_index()
        self._pending_delete_key = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def _reset_title(self) -> None:
        self.query_one("#choice-title", Label).update(self._title_text())

    def action_close(self) -> None:
        self.dismiss("exit")

    def _add_model(self, selected_index: int) -> None:
        self.app.push_screen(AddModelModal(), lambda model_config: self._finish_add_model(model_config, selected_index))

    def _finish_add_model(self, model_config: dict[str, str] | None, selected_index: int) -> None:
        if model_config is None:
            self._reload_rows(selected_index)
            return
        model_ids = [
            item.strip()
            for item in model_config["model_input"].replace("，", ",").split(",")
            if item.strip()
        ]
        self._model_manager.add_model(
            model_config["base_url"],
            model_config["api_key"],
            model_ids,
            message_format=model_config["message_format"],
        )
        self._reload_rows(selected_index)

    def _target_index(self, model_key: ModelKey) -> int | None:
        return next(
            (index for index, model in enumerate(self._model_manager.models) if model.key == model_key),
            None,
        )


class MemoryPanelModal(ClosableModalScreen[list[str]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
        Binding("enter", "toggle_detail", "Details", priority=True),
        Binding("space", "toggle_detail", "Details", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, memory_provider: Any) -> None:
        super().__init__()
        self._memory_provider = memory_provider
        self._memories: list[dict[str, Any]] = []
        self._expanded_id: str | None = None
        self._pending_delete_id: str | None = None
        self._pending_delete_index: int | None = None
        self._deleted_ids: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="memory-dialog"):
            yield ModalHeader(
                "🧠 长期记忆面板 (active: 0)\nEnter/Space 查看详情；d 删除；q 关闭。",
                title_id="choice-title",
            )
            yield ListView(id="memory-list")
            yield RichLog(id="memory-detail", markup=True, wrap=True, min_width=1)

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#memory-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _memory_label(self, item: dict[str, Any]) -> str:
        memory_id = item.get("id", "")
        marker = "▼" if memory_id == self._expanded_id else " "
        category = item.get("category", "")
        updated_at = item.get("updated_at", "")
        insight = str(item.get("insight", "")).replace("\n", " ")
        if len(insight) > 72:
            insight = f"{insight[:69]}..."
        return f"[{marker}] {memory_id} · {category} · {updated_at}\n    {insight}"

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._memories = sorted(
            self._memory_provider.list_long_term_memories(),
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        )
        self._reset_title()
        choice_list = self.query_one("#memory-list", ListView)
        choice_list.clear()

        labels = [self._memory_label(item) for item in self._memories] or ["暂无长期记忆"]

        def _mount_rows() -> None:
            choice_list.extend(ListItem(Label(label)) for label in labels)
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            choice_list.focus()
            self._update_detail()

        self.call_after_refresh(_mount_rows)

    def _title_text(self) -> str:
        return f"🧠 长期记忆面板 (active: {len(self._memories)})\nEnter/Space 查看详情；d 删除；q 关闭。"

    def _reset_title(self) -> None:
        self.query_one("#choice-title", Label).update(self._title_text())

    def _current_memory(self) -> dict[str, Any] | None:
        if not self._memories:
            return None
        index = self._selected_index()
        if index >= len(self._memories):
            return None
        return self._memories[index]

    def _update_detail(self) -> None:
        detail = self.query_one("#memory-detail", RichLog)
        detail.clear()
        current = self._current_memory()
        if current is None:
            detail.write("暂无详情。", expand=True, shrink=True)
            return
        if current.get("id") != self._expanded_id:
            detail.write("选中记忆后按 Enter/Space 查看详情。", expand=True, shrink=True)
            return
        detail.write(
            "\n".join(
                [
                    f"ID: {current.get('id', '')}",
                    f"Category: {current.get('category', '')}",
                    f"Updated: {current.get('updated_at', '')}",
                    "",
                    f"Insight:\n{current.get('insight', '')}",
                    "",
                    f"Evidence:\n{current.get('evidence', '')}",
                    "",
                    f"Reuse condition:\n{current.get('reuse_condition', '')}",
                ]
            ),
            expand=True,
            shrink=True,
            scroll_end=False,
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_toggle_detail()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._update_detail()

    def _on_key(self, event: Key) -> None:
        key_actions = {
            "enter": self.action_toggle_detail,
            "space": self.action_toggle_detail,
            "d": self.action_delete,
            "y": self.action_confirm_delete,
            "n": self.action_cancel_delete,
            "q": self.action_close,
        }
        action = key_actions.get(event.key)
        if action is None:
            return
        action()
        event.stop()
        event.prevent_default()

    def action_toggle_detail(self) -> None:
        if self._pending_delete_id is not None:
            return
        current = self._current_memory()
        if current is None:
            return
        memory_id = current.get("id")
        self._expanded_id = None if self._expanded_id == memory_id else memory_id
        index = self._selected_index()
        choice_list = self.query_one("#memory-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._memory_label(current))
        self._update_detail()
        choice_list.index = index
        choice_list.focus()

    def action_delete(self) -> None:
        current = self._current_memory()
        if current is None:
            return
        self._pending_delete_id = current.get("id")
        self._pending_delete_index = self._selected_index()
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除长期记忆？\n"
            f"{self._pending_delete_id}\n"
            "按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_id is None:
            return
        selected_index = self._pending_delete_index or self._selected_index()
        if self._memory_provider.delete_long_term_memory(self._pending_delete_id):
            self._deleted_ids.append(self._pending_delete_id)
        if self._expanded_id == self._pending_delete_id:
            self._expanded_id = None
        self._reload_rows(selected_index)

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index or self._selected_index()
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#memory-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def action_close(self) -> None:
        self.dismiss(list(self._deleted_ids))


class MemoryConfigModal(ClosableModalScreen[str | dict[str, Any]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    _FIELDS = {
        "context_length": {
            "label": "全局上下文长度（k tokens）",
            "input_id": "memory-config-context-length",
        },
        "memory_size": {
            "label": "长期记忆容量上限",
            "input_id": "memory-config-memory-size",
        },
        "keep_recent_tool_call": {
            "label": "近期工具调用结果保留数量",
            "input_id": "memory-config-keep-recent-tool-call",
        },
        "memory_recall_window_size": {
            "label": "记忆召回抑制窗口大小",
            "input_id": "memory-config-memory-recall-window-size",
        },
    }

    def __init__(self, values: dict[str, Any]) -> None:
        super().__init__()
        self._values = values

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-config-dialog"):
            yield ModalHeader(
                "🧠 记忆配置\n修改后按 Enter 或点击确认应用；配置值必须是大于 0 的整数。",
                title_id="choice-title",
            )
            for field, meta in self._FIELDS.items():
                yield Label(f"{meta['label']} ({field})", classes="memory-config-label")
                yield Input(
                    value=str(self._values[field]),
                    id=meta["input_id"],
                    classes="memory-config-input",
                )
            yield Label(
                f"记忆召回模型：{self._values.get('memory_recall_model_display', '同主模型')}",
                id="memory-config-recall-model",
                classes="memory-config-label",
            )
            yield Button("选择记忆召回模型", id="memory-config-choose-recall-model", classes="memory-config-button")
            with Horizontal(id="memory-config-actions"):
                yield Button("确认应用", id="memory-config-apply", variant="success", classes="memory-config-button")
                yield Button("取消", id="memory-config-cancel", variant="warning", classes="memory-config-button")

    def on_mount(self) -> None:
        self.query_one("#memory-config-memory-size", Input).focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            if getattr(self.focused, "id", None) == "memory-config-choose-recall-model":
                self._dismiss_values("choose_recall_model")
            else:
                self.action_submit()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            self.action_cancel()
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "memory-config-choose-recall-model":
            self._dismiss_values("choose_recall_model")
            return
        if event.button.id == "memory-config-apply":
            self.action_submit()
            return
        if event.button.id == "memory-config-cancel":
            self.action_cancel()

    def _collect_values(self) -> dict[str, Any] | None:
        values = {}
        for field, meta in self._FIELDS.items():
            raw_value = self.query_one(f"#{meta['input_id']}", Input).value.strip()
            try:
                value = int(raw_value)
            except ValueError:
                self._show_error(f"{meta['label']} 必须是大于 0 的整数。")
                return None
            if value <= 0:
                self._show_error(f"{meta['label']} 必须是大于 0 的整数。")
                return None
            values[field] = value
        values["memory_recall_model_key"] = self._values.get("memory_recall_model_key")
        values["memory_recall_model_display"] = self._values.get("memory_recall_model_display", "同主模型")
        return values

    def _dismiss_values(self, action: str) -> None:
        values = self._collect_values()
        if values is None:
            return
        values["__action"] = action
        self.dismiss(values)

    def action_submit(self) -> None:
        values = self._collect_values()
        if values is None:
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")

    def _show_error(self, message: str) -> None:
        self.query_one("#choice-title", Label).update(
            "🧠 记忆配置\n"
            f"[bold yellow]{message}[/bold yellow]\n"
            "修改后按 Enter 或点击确认应用；配置值必须是大于 0 的整数。"
        )


class RecallModelPickerModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("q", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, options: list[str]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield ModalHeader("🧠 选择记忆召回模型\nEnter 选择；q 取消。", title_id="choice-title")

            yield ListView(*[ListItem(Label(option)) for option in self._options], id="choice-list")

    def on_mount(self) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_select()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            self.action_cancel()
            event.stop()
            event.prevent_default()

    def action_select(self) -> None:
        self.dismiss(f"select:{self._selected_index()}")

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")


class AddModelModal(ClosableModalScreen[dict[str, str] | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="model-form-dialog"):
            yield ModalHeader("➕ 添加模型", title_id="choice-title")
            yield Label("Base URL", classes="model-form-label")
            yield Input(placeholder="https://api.example.com/v1", id="model-base-url", classes="model-form-input")
            yield Label("API Key", classes="model-form-label")
            yield Input(placeholder="API Key", password=True, id="model-api-key", classes="model-form-input")
            yield Label("Model ID(s)（多个用逗号分隔）", classes="model-form-label")
            yield Input(placeholder="model-a, model-b", id="model-ids", classes="model-form-input")
            yield Label("消息格式", classes="model-form-label")
            yield Select(
                [(message_format, message_format) for message_format in MESSAGE_FORMATS],
                value="openai_chat",
                allow_blank=False,
                id="model-message-format",
                classes="model-form-input",
            )
            yield Label("提示：填写完成后点击“确定”，也可以按 Enter 提交。", id="model-form-hint")
            with Horizontal(id="custom-actions"):
                yield Button("确定", id="model-confirm", variant="success")
                yield Button("取消", id="custom-cancel", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#model-base-url", Input).focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter" and isinstance(self.focused, Select):
            return
        if event.key == "enter":
            self.action_submit()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and not isinstance(self.focused, (Input, Select)):
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "model-confirm":
            self.action_submit()
        if event.button.id == "custom-cancel":
            self.action_cancel()

    def action_submit(self) -> None:
        base_url = self.query_one("#model-base-url", Input).value.strip()
        api_key = self.query_one("#model-api-key", Input).value.strip()
        model_input = self.query_one("#model-ids", Input).value.strip()
        message_format = self.query_one("#model-message-format", Select).value
        if not base_url or not api_key or not model_input or message_format not in MESSAGE_FORMATS:
            return
        self.dismiss({
            "base_url": base_url,
            "api_key": api_key,
            "model_input": model_input,
            "message_format": message_format,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class LayoutModal(ClosableModalScreen[str | dict[str, int]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("space", "increment_focused", "Increment", priority=True),
    ]

    _LABELS = {
        "content": "Content",
        "tools": "Tools",
        "task": "Task",
        "background": "Background",
        "sub_agent": "Sub-Agent",
    }

    def __init__(self, ratios: dict[str, int]) -> None:
        super().__init__()
        self._ratios = normalize_layout_ratios(ratios)

    def compose(self) -> ComposeResult:
        with Vertical(id="layout-dialog"):
            yield ModalHeader(
                "🧩 Layout 布局比例\n点击按钮或按 Space 在 0-10 间循环；0 表示隐藏但继续接收渲染。",
                title_id="choice-title",
            )
            with Horizontal(id="layout-columns"):
                with Vertical(classes="layout-column"):
                    yield Label("左侧高度比例")
                    for key in LAYOUT_LEFT_KEYS:
                        yield Button(self._button_label(key), id=f"layout-{key}", classes="layout-button")
                with Vertical(classes="layout-column"):
                    yield Label("右侧高度比例")
                    for key in LAYOUT_RIGHT_KEYS:
                        yield Button(self._button_label(key), id=f"layout-{key}", classes="layout-button")
            with Horizontal(id="layout-actions"):
                yield Button("确认应用", id="layout-apply", variant="success", classes="layout-action-button")
                yield Button("重置默认", id="layout-reset", variant="primary", classes="layout-action-button")
                yield Button("取消", id="layout-cancel", variant="warning", classes="layout-action-button")

    def on_mount(self) -> None:
        self.query_one("#layout-content", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("layout-"):
            action = button_id.removeprefix("layout-")
            if action in self._ratios:
                self._increment(action)
                return
            if action == "apply":
                self.dismiss(dict(self._ratios))
                return
            if action == "reset":
                self._ratios = dict(LAYOUT_DEFAULT_RATIOS)
                self._refresh_buttons()
                return
            if action == "cancel":
                self.action_cancel()

    def _on_key(self, event: Key) -> None:
        if event.key == "q" and not isinstance(self.focused, Input):
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return
        if event.key == "space":
            self.action_increment_focused()
            event.stop()
            event.prevent_default()

    def action_increment_focused(self) -> None:
        focused = self.focused
        if not isinstance(focused, Button) or focused.id is None:
            return
        key = focused.id.removeprefix("layout-")
        if key in self._ratios:
            self._increment(key)

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")

    def _button_label(self, key: str) -> str:
        return f"{self._LABELS[key]} {self._ratios[key]}"

    def _increment(self, key: str) -> None:
        self._ratios[key] = (self._ratios[key] + 1) % 11
        self._refresh_button(key)

    def _refresh_buttons(self) -> None:
        for key in self._ratios:
            self._refresh_button(key)

    def _refresh_button(self, key: str) -> None:
        button = self.query_one(f"#layout-{key}", Button)
        button.label = self._button_label(key)
        button.focus()
