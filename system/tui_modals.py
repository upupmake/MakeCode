import json
import os
import shlex
from typing import Any, Callable, TypeVar
from pathlib import Path

from rich.console import RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.events import Click, Key, Resize
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Select, Static, TextArea, DataTable
from textual.widgets.text_area import Selection

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
    LAYOUT_RIGHT_KEYS,
    normalize_layout_ratios,
)
from utils import paths


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
    ChoiceModal, DelegateTasksModal, StartupWorkdirModal, ModelPanelModal, McpSwitchModal, McpToolsModal, McpViewModal, McpAddModal, ModelManagerModal, AddModelModal, AddMemoryModal, LayoutModal, MemoryPanelModal, MemoryConfigModal, RecallModelPickerModal, InfoPanelModal, CopyContentModal, TaskPanelModal, ToolHistoryModal, SkillsConfigModal, TemporaryQueryModal {
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

    #temporary-query-dialog {
        width: 88%;
        height: auto;
        max-height: 70%;
        border: round #a78bfa;
        background: $surface;
        padding: 1 2;
    }

    #temporary-query-input {
        height: 10;
        margin-top: 1;
        border: round #475569;
    }

    #temporary-query-hint {
        height: auto;
        margin-top: 1;
    }

    #temporary-query-actions {
        height: 3;
        margin-top: 1;
    }

    .temporary-query-action {
        width: 16;
        margin-right: 1;
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

    #startup-candidates {
        display: none;
        height: auto;
        max-height: 8;
        margin-top: 1;
        border: round #f59e0b;
        background: #111827;
        color: #e5e7eb;
        padding: 0 1;
    }

    #startup-candidates.visible {
        display: block;
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

    #tool-history-token-usage {
        width: 16;
        margin-left: 1;
    }

    #tool-history-filter-row.compact {
        height: auto;
        layout: vertical;
    }

    #tool-history-filter-row.compact > .tool-history-filter,
    #tool-history-filter-row.compact > #tool-history-token-usage {
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

    #copy-sections {
        height: 1fr;
        margin-top: 1;
        overflow-y: auto;
    }

    .copy-section-label {
        height: 1;
        margin-top: 1;
        text-style: bold;
    }

    .copy-section-label:first-child {
        margin-top: 0;
    }

    .copy-user-label {
        color: #22c55e;
    }

    .copy-assistant-label {
        color: #a78bfa;
    }

    .copy-terminal-label {
        color: #38bdf8;
    }

    .copy-section-text {
        height: auto;
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
        height: 86%;
        min-height: 18;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-title {
        height: auto;
        margin-bottom: 0;
    }

    #memory-summary {
        height: auto;
        margin-top: 1;
    }

    #memory-list {
        height: 1fr;
        min-height: 5;
        margin-top: 1;
        padding: 0 1;
        border: round #334155;
    }

    #memory-list > ListItem {
        height: auto;
        min-height: 3;
        margin-bottom: 1;
        padding: 1 2;
        border: round #1e293b;
    }

    #memory-list > ListItem.-highlight {
        border: round #f59e0b;
    }

    #memory-list > ListItem > Label {
        width: 1fr;
        height: auto;
    }

    #memory-detail {
        height: 10;
        min-height: 4;
        margin-top: 1;
        border: round #3b82f6;
        padding: 0 1;
    }

    #memory-help {
        height: auto;
        margin-top: 1;
    }

    #memory-actions {
        height: 3;
        margin-top: 1;
    }

    .memory-action {
        width: 1fr;
        margin: 0 1;
    }

    #memory-add-dialog {
        width: 82%;
        height: 88%;
        min-height: 20;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-add-fields {
        height: 1fr;
        padding: 0 1;
    }

    .memory-add-label {
        height: auto;
        margin-top: 1;
    }

    .memory-add-textarea {
        height: 5;
        min-height: 3;
    }

    #memory-add-insight {
        height: 7;
    }

    #memory-add-error {
        display: none;
        height: auto;
        margin-top: 1;
        color: #f87171;
    }

    #memory-add-hint {
        height: auto;
        margin-top: 1;
    }

    #memory-add-actions {
        height: 3;
        margin-top: 1;
    }

    .memory-add-action {
        width: 1fr;
        margin: 0 1;
    }

    #memory-config-dialog {
        width: 76;
        height: 90%;
        max-height: 90%;
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

    #layout-columns {
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

    #model-manager-dialog {
        width: 86%;
        height: 86%;
        min-height: 16;
        border: round #a78bfa;
        background: $surface;
        padding: 1 2;
    }

    #model-manager-title {
        height: auto;
        margin-bottom: 0;
        color: #e2e8f0;
    }

    #model-manager-summary {
        height: auto;
        margin-top: 1;
        color: #94a3b8;
    }

    #model-manager-list {
        height: 1fr;
        min-height: 5;
        margin-top: 1;
        padding: 0 1;
        border: round #334155;
    }

    #model-manager-list > ListItem {
        height: auto;
        min-height: 3;
        margin-bottom: 1;
        padding: 1 2;
        border: round #1e293b;
        background: #111827;
    }

    #model-manager-list > ListItem.-highlight {
        border: round #a78bfa;
        background: #2e1065;
    }

    #model-manager-list > ListItem > Label {
        width: 1fr;
        height: auto;
    }

    #model-manager-help {
        height: auto;
        margin-top: 1;
        color: #64748b;
    }

    #model-manager-actions {
        height: 3;
        margin-top: 1;
    }

    .model-manager-action {
        width: 1fr;
        margin: 0 1;
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

    #mcp-dialog {
        width: 86%;
        height: 86%;
        min-height: 16;
        border: round #22d3ee;
        background: $surface;
        padding: 1 2;
    }

    #mcp-title {
        height: auto;
        margin-bottom: 0;
    }

    #mcp-summary {
        height: auto;
        margin-top: 1;
    }

    #mcp-help {
        height: auto;
        margin-top: 1;
    }

    #mcp-list {
        height: 1fr;
        min-height: 5;
        margin-top: 1;
        padding: 0 1;
        border: round #334155;
    }

    #mcp-list > ListItem {
        height: auto;
        min-height: 3;
        margin-bottom: 1;
        padding: 1 2;
        border: round #1e293b;
    }

    #mcp-list > ListItem.-highlight {
        border: round #38bdf8;
    }

    #mcp-list > ListItem > Label {
        width: 1fr;
        height: auto;
    }

    #mcp-actions {
        height: 3;
        margin-top: 1;
    }

    .mcp-action {
        width: 1fr;
        margin: 0 1;
    }

    #mcp-tools-dialog {
        width: 86%;
        height: 82%;
        min-height: 16;
        border: round #22c55e;
        background: $surface;
        padding: 1 2;
    }

    #mcp-tools-title,
    #mcp-tools-summary,
    #mcp-tools-help {
        height: auto;
    }

    #mcp-tools-summary,
    #mcp-tools-help {
        margin-top: 1;
    }

    #mcp-tools-filter-row {
        height: 3;
        margin-top: 1;
    }

    #mcp-tools-filter-label {
        width: auto;
        height: 3;
        content-align: left middle;
        margin-right: 1;
    }

    #mcp-tools-status-filter {
        width: 24;
    }

    #mcp-tools-table {
        height: 1fr;
        min-height: 5;
        margin-top: 1;
        border: round #475569;
    }

    #mcp-tools-actions {
        height: 3;
        margin-top: 1;
    }

    .mcp-tools-action {
        width: 1fr;
        margin: 0 1;
    }

    #mcp-view-dialog {
        width: 90%;
        height: 88%;
        min-height: 18;
        border: round #22c55e;
        background: $surface;
        padding: 1 2;
    }

    #mcp-view-title,
    #mcp-view-filter-summary,
    #mcp-view-help {
        height: auto;
    }

    #mcp-view-summary {
        height: 10;
        min-height: 5;
        margin-top: 1;
        border: round #334155;
    }

    #mcp-view-filter-row {
        height: 3;
        margin-top: 1;
    }

    #mcp-view-filter-label {
        width: auto;
        height: 3;
        content-align: left middle;
        margin-right: 1;
    }

    #mcp-view-status-filter {
        width: 24;
    }

    #mcp-view-filter-summary,
    #mcp-view-help {
        margin-top: 1;
    }

    #mcp-view-tools-table {
        height: 1fr;
        min-height: 5;
        margin-top: 1;
        border: round #475569;
    }

    #mcp-add-dialog {
        width: 86%;
        height: 90%;
        min-height: 20;
        border: round #22d3ee;
        background: $surface;
        padding: 1 2;
    }

    #mcp-add-fields {
        height: 1fr;
        padding: 0 1;
    }

    .mcp-add-group {
        height: auto;
    }

    .mcp-add-label {
        height: auto;
        margin-top: 1;
    }

    .mcp-add-input {
        margin-top: 0;
    }

    .mcp-add-pairs {
        height: 4;
        min-height: 3;
        margin-top: 0;
    }

    #mcp-add-advanced-title {
        height: auto;
        margin-top: 1;
        padding-top: 1;
        border-top: solid #475569;
    }

    #mcp-add-error {
        display: none;
        height: auto;
        margin-top: 1;
        color: #f87171;
    }

    #mcp-add-hint {
        height: auto;
        margin-top: 1;
    }

    #mcp-add-actions {
        height: 3;
        margin-top: 1;
    }

    .mcp-add-action {
        width: 1fr;
        margin: 0 1;
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
        self._reload_generation = 0

    def _title_text(self) -> str:
        hints = []
        if self._preview_handler is not None:
            hints.append("v 预览选中项")
        if self._delete_handler is not None:
            hints.append("d 删除选中项 · y 确认删除 · n 取消删除")
        return f"{self._title}\n{' · '.join(hints)}" if hints else self._title

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="choice-dialog"):
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
        self._reload_generation += 1
        reload_generation = self._reload_generation

        async def _mount_rows() -> None:
            if reload_generation != self._reload_generation:
                return
            await choice_list.clear()
            if reload_generation != self._reload_generation:
                return
            await choice_list.extend(
                ListItem(Label(option, markup=False)) for option in self._options
            )
            if reload_generation != self._reload_generation:
                return
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


class TemporaryQueryModal(ClosableModalScreen[str | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("ctrl+n", "insert_newline", "New line", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, value: str | None = None) -> None:
        super().__init__()
        self._value = value or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="temporary-query-dialog"):
            yield ModalHeader("追加临时指令", title_id="temporary-query-title", markup=False)
            yield Label(
                "编辑内容只有再次提交后才会替换待处理指令；Agent 取走前，未提交草稿不会生效。"
                if self._value
                else "这条内容只会注入当前 Agent 循环的后续模型请求，不执行斜杠命令，也不触发记忆召回。",
                id="temporary-query-description",
                markup=False,
            )
            yield TextArea(
                self._value,
                id="temporary-query-input",
                show_line_numbers=False,
                soft_wrap=True,
            )
            yield Label(
                "Enter 提交；Ctrl+N 换行；Esc 取消。",
                id="temporary-query-hint",
                markup=False,
            )
            with Horizontal(id="temporary-query-actions"):
                yield Button("提交", id="temporary-query-submit", variant="success", classes="temporary-query-action")
                yield Button("关闭", id="temporary-query-cancel", variant="warning", classes="temporary-query-action")

    def on_mount(self) -> None:
        query_input = self.query_one("#temporary-query-input", TextArea)
        query_input.focus()
        query_input.cursor_location = query_input.document.end

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "temporary-query-submit":
            self.action_submit()
        elif event.button.id == "temporary-query-cancel":
            self.action_cancel()

    def action_submit(self) -> None:
        value = self.query_one("#temporary-query-input", TextArea).text.strip()
        if value:
            self.dismiss(value)

    def current_text(self) -> str:
        return self.query_one("#temporary-query-input", TextArea).text

    def action_insert_newline(self) -> None:
        self.query_one("#temporary-query-input", TextArea).insert("\n")

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        self._completion_candidates: list[str] = []
        self._completion_index = 0
        self._completion_input = ""
        self._completion_cursor = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-dialog"):
            yield ModalHeader("", title_id="startup-title")
            yield Input(placeholder="输入自定义工作区路径", id="startup-input")
            yield Static("", id="startup-candidates")

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
            if event.key == "tab":
                event.stop()
                event.prevent_default()
                self._complete_custom_path()
                return
            if event.key == "up" and self._completion_candidates:
                event.stop()
                event.prevent_default()
                self._move_completion_selection(-1)
                return
            if event.key == "down" and self._completion_candidates:
                event.stop()
                event.prevent_default()
                self._move_completion_selection(1)
                return
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
            self._hide_completion_candidates()
            custom_input.focus()
            self.query_one("#startup-title", Label).update(
                "📂 输入自定义工作区路径（Enter 确认）："
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "startup-input" or self._mode != "custom":
            return
        if event.value != self._completion_input:
            self._completion_candidates = []
            self._completion_index = 0
            self._completion_input = event.value
            self._hide_completion_candidates()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "startup-input":
            return
        event.stop()
        if self._ignore_initial_custom_submit and not event.value:
            self._ignore_initial_custom_submit = False
            return
        self._ignore_initial_custom_submit = False
        self.dismiss(f"custom:{event.value}")

    def _completion_context(self, value: str) -> tuple[str, str, bool]:
        quote = value[0] if value[:1] in {'"', "'"} else ""
        path_fragment = value[1:] if quote else value
        has_closing_quote = bool(quote and path_fragment.endswith(quote))
        if has_closing_quote:
            path_fragment = path_fragment[:-1]
        return quote, path_fragment, has_closing_quote

    def _show_completion_candidates(self) -> None:
        candidate_box = self.query_one("#startup-candidates", Static)
        selected = self._completion_index % len(self._completion_candidates)
        window_size = 6
        start = min(
            max(0, selected - window_size + 1),
            max(0, len(self._completion_candidates) - window_size),
        )
        end = min(len(self._completion_candidates), start + window_size)
        content = Text()
        for index, candidate in enumerate(self._completion_candidates[start:end], start=start):
            if index > start:
                content.append("\n")
            content.append("❯ " if index == selected else "  ", style="bold" if index == selected else "")
            content.append(candidate, style="cyan" if index == selected else "white")
        candidate_box.update(content)
        candidate_box.add_class("visible")

    def _hide_completion_candidates(self) -> None:
        candidate_box = self.query_one("#startup-candidates", Static)
        candidate_box.update("")
        candidate_box.remove_class("visible")

    def _replace_completion(
        self,
        custom_input: Input,
        value: str,
        cursor_position: int,
        quote: str,
        completed_path: str,
        has_closing_quote: bool,
    ) -> None:
        suffix = value[cursor_position:]
        closing_quote_in_suffix = bool(quote and suffix.startswith(quote))
        closing_quote = quote if quote and (has_closing_quote or not closing_quote_in_suffix) else ""
        replacement = f"{quote}{completed_path}{closing_quote}"
        completed_value = f"{replacement}{suffix}"
        completed_cursor = len(replacement) - len(closing_quote)
        self._completion_input = completed_value
        self._completion_cursor = completed_cursor
        custom_input.value = completed_value
        custom_input.cursor_position = completed_cursor

    def _complete_custom_path(self) -> None:
        custom_input = self.query_one("#startup-input", Input)
        value = custom_input.value
        cursor_position = custom_input.cursor_position
        raw_fragment = value[:cursor_position]
        quote, path_fragment, has_closing_quote = self._completion_context(raw_fragment)
        continuing = bool(self._completion_candidates) and (
            value,
            cursor_position,
        ) == (
            self._completion_input,
            self._completion_cursor,
        )
        if continuing:
            completed_path = self._completion_candidates[self._completion_index]
            self._replace_completion(
                custom_input,
                value,
                cursor_position,
                quote,
                completed_path,
                has_closing_quote,
            )
            self._completion_candidates = []
            self._completion_index = 0
            self._hide_completion_candidates()
            custom_input.focus()
            return

        candidates = paths.directory_completion_candidates(path_fragment, self.cwd)
        if not candidates:
            self._completion_candidates = []
            self._completion_index = 0
            self._completion_input = value
            self._completion_cursor = cursor_position
            self._hide_completion_candidates()
            return

        self._completion_index = 0
        common_prefix = os.path.commonprefix(candidates)
        if len(common_prefix) > len(path_fragment):
            self._replace_completion(
                custom_input,
                value,
                cursor_position,
                quote,
                common_prefix,
                has_closing_quote,
            )
        elif len(candidates) == 1:
            self._replace_completion(
                custom_input,
                value,
                cursor_position,
                quote,
                candidates[0],
                has_closing_quote,
            )
        else:
            self._completion_input = value
            self._completion_cursor = cursor_position

        if len(candidates) > 1:
            self._completion_candidates = candidates
            self._show_completion_candidates()
        else:
            self._completion_candidates = []
            self._hide_completion_candidates()
        custom_input.focus()

    def _move_completion_selection(self, delta: int) -> None:
        self._completion_index = (self._completion_index + delta) % len(self._completion_candidates)
        self._show_completion_candidates()

    def _refresh_text(self) -> None:
        options = [
            f"当前目录 ({self.cwd})",
            "输入自定义路径...",
        ]
        lines = ["📂 选择工作区目录（使用 ↑/↓ 方向键，Enter 确认）：", ""]
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

    def __init__(
        self,
        history: ToolExecutionHistory,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._message_history = ToolExecutionHistory()
        self._history = ToolExecutionHistory()
        if messages is None:
            self._history.replace_with(history)
        else:
            self._message_history.rebuild_from_messages(messages)
            self._history.replace_with(self._message_history)
            self._history.append_records(
                [
                    record
                    for record in history.snapshot(newest_first=False)
                    if record.source != "orchestrator"
                ]
            )
        self._view = "timeline"
        self._tool_filter = ""
        self._row_values: list[ToolExecutionRecord | ToolExecutionSummary] = []
        self._last_signature: tuple[Any, ...] | None = None
        self._reload_generation = 0
        self._compact = False
        self._detail_open = False

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-history-dialog"):
            yield ModalHeader(self._title_text(), title_id="tool-history-title")
            with Horizontal(id="tool-history-filter-row"):
                yield Input(placeholder="搜索工具名、参数、结果、错误或执行者…", id="tool-history-search")
                yield Select(self._STATUS_OPTIONS, value="", allow_blank=False, id="tool-history-status-filter", classes="tool-history-filter")
                yield Select(self._SOURCE_OPTIONS, value="", allow_blank=False, id="tool-history-source-filter", classes="tool-history-filter")
                yield Button("输出占比", id="tool-history-token-usage")
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
        return (
            self._view,
            self._tool_filter,
            *self._filter_values(),
        )

    def _reload_rows(self, *, force: bool = False) -> None:
        self._reload_generation += 1
        reload_generation = self._reload_generation
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
            if reload_generation != self._reload_generation:
                return
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

    def _tool_output_usage_content(self) -> RenderableType:
        from utils.memory import estimate_text_tokens

        usage, total_tokens = self._message_history.output_token_usage(estimate_text_tokens)
        if not usage:
            return Text("当前对话 messages 中没有主 Agent 工具输出。")

        table = Table(expand=True)
        table.add_column("排名", justify="right", no_wrap=True)
        table.add_column("工具")
        table.add_column("输出数", justify="right", no_wrap=True)
        table.add_column("Tokens", justify="right", no_wrap=True)
        table.add_column("占比", justify="right", no_wrap=True)
        for rank, item in enumerate(usage, start=1):
            ratio = item.tokens / total_tokens if total_tokens else 0
            table.add_row(
                str(rank),
                item.tool_name,
                str(item.output_count),
                f"{item.tokens:,}",
                f"{ratio:.2%}",
            )
        table.caption = (
            f"基于当前对话 messages · 仅统计主 Agent 工具输出 · "
            f"共 {sum(item.output_count for item in usage)} 次输出 "
            f"· 合计 {total_tokens:,} tokens"
        )
        return table

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "tool-history-token-usage":
            return
        event.stop()
        self.app.push_screen(
            InfoPanelModal("主 Agent 工具输出占比", self._tool_output_usage_content())
        )

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


class SelectBeforeActivateListView(ListView):
    # Textual 默认每次点击都发送 Selected；移动高亮时只选中，不激活。
    def _on_list_item__child_clicked(self, event: ListItem._ChildClicked) -> None:
        event.stop()
        event.prevent_default()
        self.focus()
        clicked_index = self._nodes.index(event.item)
        already_selected = self.index == clicked_index
        self.index = clicked_index
        if already_selected:
            self.post_message(self.Selected(self, event.item, clicked_index))


class SelectBeforeActivateDataTable(DataTable):
    # 鼠标首次点击其他行只移动光标，再次点击当前行才发送 RowSelected。
    async def _on_click(self, event: Click) -> None:
        row_index = event.style.meta.get("row")
        column_index = event.style.meta.get("column")
        if (
            not isinstance(row_index, int)
            or not isinstance(column_index, int)
            or row_index < 0
            or column_index < 0
        ):
            await super()._on_click(event)
            return

        event.stop()
        event.prevent_default()
        self.focus()
        already_selected = self.cursor_row == row_index
        self.move_cursor(row=row_index, column=column_index)
        if already_selected and row_index < self.row_count:
            row_key = self.ordered_rows[row_index].key
            self.post_message(self.RowSelected(self, row_index, row_key))


class McpDataTable(SelectBeforeActivateDataTable):
    # DataTable doesn't provide row rules, so keep one virtual separator line between rows.
    _ROW_SEPARATOR_STYLE = Style(color="#475569")

    @property
    def _y_offsets(self) -> list[tuple[Any, int]]:
        if self._update_count in self._offset_cache:
            return self._offset_cache[self._update_count]

        offsets: list[tuple[Any, int]] = []
        rows = self.ordered_rows
        for row_index, row in enumerate(rows):
            offsets.extend((row.key, line) for line in range(row.height))
            if row_index < len(rows) - 1:
                offsets.append((row.key, row.height))
        self._offset_cache[self._update_count] = offsets
        return offsets

    def _get_offsets(self, y: int) -> tuple[Any, int]:
        header_height = self.header_height
        if self.show_header:
            if y < header_height:
                return self._header_row_key, y
            y -= header_height
        if y < 0 or y >= len(self._y_offsets):
            raise LookupError(f"Y coord {y!r} is outside the DataTable")
        return self._y_offsets[y]

    def _row_y(self, row_index: int) -> int:
        y = sum(row.height for row in self.ordered_rows[:row_index]) + row_index
        if self.show_header:
            y += self.header_height
        return y

    def _get_row_region(self, row_index: int) -> Region:
        if not self.is_valid_row_index(row_index):
            return Region(0, 0, 0, 0)
        row_key = self._row_locations.get_key(row_index)
        row = self.rows[row_key]
        row_width = sum(column.get_render_width(self) for column in self.columns.values())
        return Region(0, self._row_y(row_index), max(self.size.width, row_width), row.height)

    def _get_cell_region(self, coordinate: Coordinate) -> Region:
        if not self.is_valid_coordinate(coordinate):
            return Region(0, 0, 0, 0)
        row_index, column_index = coordinate
        row_key = self._row_locations.get_key(row_index)
        x = (
            sum(column.get_render_width(self) for column in self.ordered_columns[:column_index])
            + self._row_label_column_width
        )
        column_key = self._column_locations.get_key(column_index)
        return Region(
            x,
            self._row_y(row_index),
            self.columns[column_key].get_render_width(self),
            self.rows[row_key].height,
        )

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        try:
            row_key, line = self._get_offsets(y)
        except LookupError:
            return Strip.blank(self.size.width, base_style)
        row = self.rows.get(row_key)
        if row is not None and line == row.height:
            width = max(self.size.width, self.virtual_size.width, x2)
            separator = Strip(
                [Segment("─" * width, self._ROW_SEPARATOR_STYLE)],
                cell_length=width,
            )
            return separator.crop(x1, x2).adjust_cell_length(self.size.width, base_style)
        return super()._render_line(y, x1, x2, base_style)


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
            yield SelectBeforeActivateListView(id="skills-list")
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
        self._selected_text_area: TextArea | None = None
        self._updating_selection = False

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
            with VerticalScroll(id="copy-sections"):
                for section_index, (label, content, style_class) in enumerate(self._build_sections()):
                    yield Label(label, classes=f"copy-section-label {style_class}", markup=False)
                    yield TextArea(
                        content,
                        id=f"copy-section-text-{section_index}",
                        classes="copy-section-text",
                        read_only=True,
                        show_line_numbers=False,
                        soft_wrap=True,
                    )
            yield Label("选择任意正文后可复制选区，也可直接复制全部内容。", id="copy-status")
            with Horizontal(id="copy-actions"):
                yield Button("复制选中", id="copy-selection", variant="primary")
                yield Button("复制全部", id="copy-all", variant="success")
                yield Button("关闭", id="copy-close", variant="warning")

    def on_mount(self) -> None:
        text_area = self.query_one("#copy-sections TextArea", TextArea)
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
            text_area = self.focused
            if isinstance(text_area, TextArea) and text_area.selected_text:
                return
            self.action_close()
            event.stop()
            event.prevent_default()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        text_area = event.text_area
        if not text_area.has_class("copy-section-text") or self._updating_selection:
            return
        if text_area.selected_text:
            self._updating_selection = True
            try:
                for other_text_area in self.query(".copy-section-text"):
                    if other_text_area is not text_area and other_text_area.selected_text:
                        other_text_area.selection = Selection(
                            other_text_area.selection.end,
                            other_text_area.selection.end,
                        )
            finally:
                self._updating_selection = False
            self._selected_text_area = text_area
        elif self._selected_text_area is text_area:
            self._selected_text_area = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy-selection":
            self._copy_selection()
        elif event.button.id == "copy-all":
            self._copy_all()
        elif event.button.id == "copy-close":
            self.action_close()

    def _selected_text(self) -> str:
        if self._selected_text_area is not None:
            return self._selected_text_area.selected_text
        return ""

    def _copy_selected_or_all(self) -> None:
        selected = self._selected_text()
        self._copy_text(selected or self._text, "选中文本" if selected else "全部内容")

    def _copy_selection(self) -> None:
        selected = self._selected_text()
        if not selected:
            self.query_one("#copy-status", Label).update("请先在正文中选择要复制的文本。")
            return
        self._copy_text(selected, "选中文本")

    def _copy_all(self) -> None:
        self._copy_text(self._text, "全部内容")

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

    def _build_sections(self) -> list[tuple[str, str, str]]:
        sections = []
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
                    sections.append((f"User · {user_number}", content, "copy-user-label"))
                continue
            if role == "assistant":
                content = self._content_text(message)
                if content:
                    assistant_number += 1
                    sections.append((f"Assistant · {assistant_number}", content, "copy-assistant-label"))
                for command in self._terminal_commands(message):
                    terminal_input_number += 1
                    sections.append((f"Terminal Input · {terminal_input_number}", f"$ {command}", "copy-terminal-label"))
                continue
            if role in {"tool", "function"} and message.get("name") == "RunTerminalCommand":
                output = message.get("content")
                if output is None:
                    output = message.get("output")
                if output is None:
                    continue
                terminal_output_number += 1
                output_text = output if isinstance(output, str) else format_tool_value(output)
                sections.append((f"Terminal Output · {terminal_output_number}", output_text, "copy-terminal-label"))
        return sections

    def _build_text(self) -> str:
        return "\n\n".join(f"[{label}]\n{content}" for label, content, _ in self._build_sections())


class McpAddModal(ClosableModalScreen[dict[str, Any] | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
    ]

    _TRANSPORTS = {"stdio", "streamable-http", "sse"}

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-add-dialog"):
            yield ModalHeader("➕ 手动添加 MCP 服务", title_id="choice-title")
            with VerticalScroll(id="mcp-add-fields"):
                yield Label("服务名称", classes="mcp-add-label")
                yield Input(placeholder="例如 filesystem 或 remote-api", id="mcp-add-name", classes="mcp-add-input")
                yield Label("传输类型", classes="mcp-add-label")
                yield Select(
                    [
                        ("本地 stdio", "stdio"),
                        ("远程 Streamable HTTP", "streamable-http"),
                        ("远程 SSE", "sse"),
                    ],
                    value="stdio",
                    allow_blank=False,
                    id="mcp-add-transport",
                    classes="mcp-add-input",
                )
                with Vertical(id="mcp-add-stdio-core", classes="mcp-add-group"):
                    yield Label("启动命令", classes="mcp-add-label")
                    yield Input(placeholder="例如 npx、uvx 或可执行文件路径", id="mcp-add-command", classes="mcp-add-input")
                    yield Label("命令参数（支持引号分组）", classes="mcp-add-label")
                    yield Input(placeholder='例如 -y "@scope/server" "/path with spaces"', id="mcp-add-args", classes="mcp-add-input")
                with Vertical(id="mcp-add-remote-core", classes="mcp-add-group"):
                    yield Label("服务 URL", classes="mcp-add-label")
                    yield Input(placeholder="https://example.com/mcp", id="mcp-add-url", classes="mcp-add-input")
                yield Label("高级参数（可选）", id="mcp-add-advanced-title")
                with Vertical(id="mcp-add-stdio-advanced", classes="mcp-add-group"):
                    yield Label("环境变量（每行 KEY=VALUE）", classes="mcp-add-label")
                    yield TextArea("", id="mcp-add-env", classes="mcp-add-pairs")
                    yield Label("工作目录", classes="mcp-add-label")
                    yield Input(placeholder="子进程工作目录", id="mcp-add-cwd", classes="mcp-add-input")
                    yield Label("保持子进程存活", classes="mcp-add-label")
                    yield Select(
                        [("使用默认值", ""), ("是", "true"), ("否", "false")],
                        value="",
                        allow_blank=False,
                        id="mcp-add-keep-alive",
                        classes="mcp-add-input",
                    )
                with Vertical(id="mcp-add-remote-advanced", classes="mcp-add-group"):
                    yield Label("请求头（每行 KEY=VALUE）", classes="mcp-add-label")
                    yield TextArea("", id="mcp-add-headers", classes="mcp-add-pairs")
                    yield Label("鉴权配置", classes="mcp-add-label")
                    yield Input(placeholder="例如 oauth 或 token", id="mcp-add-auth", classes="mcp-add-input")
                yield Label("响应超时（毫秒）", classes="mcp-add-label")
                yield Input(placeholder="例如 30000", id="mcp-add-timeout", classes="mcp-add-input")
                with Vertical(id="mcp-add-sse-advanced", classes="mcp-add-group"):
                    yield Label("SSE 读取超时（秒）", classes="mcp-add-label")
                    yield Input(placeholder="例如 60", id="mcp-add-sse-read-timeout", classes="mcp-add-input")
                yield Label("", id="mcp-add-error", markup=False)
                yield Label("新服务保存后保持禁用；返回列表后可自行启用。", id="mcp-add-hint")
            with Horizontal(id="mcp-add-actions"):
                yield Button("保存服务", id="mcp-add-confirm", variant="success", classes="mcp-add-action")
                yield Button("取消", id="mcp-add-cancel", variant="warning", classes="mcp-add-action")

    def on_mount(self) -> None:
        self._sync_transport_fields()
        self.query_one("#mcp-add-name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "mcp-add-transport":
            self._sync_transport_fields()

    def _transport(self) -> str:
        value = self.query_one("#mcp-add-transport", Select).value
        return str(value) if value in self._TRANSPORTS else "stdio"

    def _sync_transport_fields(self) -> None:
        transport = self._transport()
        is_stdio = transport == "stdio"
        self.query_one("#mcp-add-stdio-core").display = is_stdio
        self.query_one("#mcp-add-stdio-advanced").display = is_stdio
        self.query_one("#mcp-add-remote-core").display = not is_stdio
        self.query_one("#mcp-add-remote-advanced").display = not is_stdio
        self.query_one("#mcp-add-sse-advanced").display = transport == "sse"

    @staticmethod
    def _parse_pairs(text: str, field_name: str) -> dict[str, str]:
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"{field_name} 需要每行使用 KEY=VALUE 格式")
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                raise ValueError(f"{field_name} 的 KEY 不能为空")
            result[key] = value.strip()
        return result

    def _show_error(self, message: str) -> None:
        error_label = self.query_one("#mcp-add-error", Label)
        error_label.update(f"❌ {message}")
        error_label.display = True

    def _on_key(self, event: Key) -> None:
        if event.key == "q" and not isinstance(self.focused, (Input, Select, TextArea)):
            self.action_cancel()
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mcp-add-confirm":
            self.action_submit()
        elif event.button.id == "mcp-add-cancel":
            self.action_cancel()

    def action_submit(self) -> None:
        try:
            server_name = self.query_one("#mcp-add-name", Input).value.strip()
            if not server_name:
                raise ValueError("服务名称不能为空")
            transport = self._transport()
            cfg: dict[str, Any] = {"transport": transport}

            if transport == "stdio":
                command = self.query_one("#mcp-add-command", Input).value.strip()
                if not command:
                    raise ValueError("stdio 服务必须填写启动命令")
                cfg["command"] = command
                arguments = self.query_one("#mcp-add-args", Input).value.strip()
                if arguments:
                    cfg["args"] = shlex.split(arguments)
                env = self._parse_pairs(self.query_one("#mcp-add-env", TextArea).text, "环境变量")
                if env:
                    cfg["env"] = env
                cwd = self.query_one("#mcp-add-cwd", Input).value.strip()
                if cwd:
                    cfg["cwd"] = cwd
                keep_alive = self.query_one("#mcp-add-keep-alive", Select).value
                if keep_alive in {"true", "false"}:
                    cfg["keep_alive"] = keep_alive == "true"
            else:
                url = self.query_one("#mcp-add-url", Input).value.strip()
                if not url:
                    raise ValueError("远程服务必须填写 URL")
                cfg["url"] = url
                headers = self._parse_pairs(self.query_one("#mcp-add-headers", TextArea).text, "请求头")
                if headers:
                    cfg["headers"] = headers
                auth = self.query_one("#mcp-add-auth", Input).value.strip()
                if auth:
                    cfg["auth"] = auth

            timeout = self.query_one("#mcp-add-timeout", Input).value.strip()
            if timeout:
                cfg["timeout"] = int(timeout)
            if transport == "sse":
                sse_timeout = self.query_one("#mcp-add-sse-read-timeout", Input).value.strip()
                if sse_timeout:
                    cfg["sse_read_timeout"] = float(sse_timeout)
            cfg["disabled"] = True
        except (ValueError, TypeError) as exc:
            self._show_error(str(exc))
            return

        self.dismiss({"server_name": server_name, "cfg": cfg})

    def action_cancel(self) -> None:
        self.dismiss(None)


class McpToolsModal(ClosableModalScreen[dict[str, Any] | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Close", priority=True),
        Binding("enter", "confirm_or_toggle", "Toggle/Confirm", priority=True),
        Binding("space", "toggle", "Toggle", priority=True),
    ]

    _STATUS_OPTIONS = [
        ("全部状态", "all"),
        ("已启用", "enabled"),
        ("已禁用", "disabled"),
    ]

    def __init__(self, tool_switches: dict[str, Any], mcp_manager: Any) -> None:
        super().__init__()
        self._server_name = tool_switches["server"]
        self._loaded = bool(tool_switches.get("loaded", False))
        self._server_disabled = bool(tool_switches.get("server_disabled", False))
        self._can_manage = self._loaded and not self._server_disabled
        self._tools = list(tool_switches.get("tools", []))
        self._mcp_manager = mcp_manager
        self._status_filter = "all"
        self._visible_tool_indices: list[int] = []
        self._draft_states = {
            item["original_name"]: bool(item["disabled"])
            for item in self._tools
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-tools-dialog"):
            yield ModalHeader(
                f"🛠️ MCP 工具管理 · {self._server_name}",
                title_id="mcp-tools-title",
                markup=False,
            )
            yield Label(self._summary_text(), id="mcp-tools-summary", markup=False)
            with Horizontal(id="mcp-tools-filter-row"):
                yield Label("状态筛选", id="mcp-tools-filter-label", markup=False)
                yield Select(
                    self._STATUS_OPTIONS,
                    value="all",
                    allow_blank=False,
                    id="mcp-tools-status-filter",
                )
            yield McpDataTable(
                id="mcp-tools-table",
                cursor_type="row",
                show_row_labels=False,
            )
            yield Label(
                "↑↓ 选择工具 · Enter/Space 切换 · Tab 切换到操作按钮 · q 返回服务列表",
                id="mcp-tools-help",
                markup=False,
            )
            with Horizontal(id="mcp-tools-actions"):
                yield Button(
                    "确认应用",
                    id="mcp-tools-apply",
                    variant="success",
                    classes="mcp-tools-action",
                    disabled=not self._can_manage or not self._tools,
                )
                yield Button(
                    "返回服务列表",
                    id="mcp-tools-close",
                    variant="warning",
                    classes="mcp-tools-action",
                )

    def on_mount(self) -> None:
        table = self.query_one("#mcp-tools-table", DataTable)
        table.add_column("草稿状态", width=12, key="status")
        table.add_column("工具名称", width=30, key="name")
        table.add_column("MCP 原始名称", width=26, key="original_name")
        table.add_column("描述", width=42, key="description")
        self._reload_rows()
        if self._can_manage and self._tools:
            table.focus()
        else:
            self.query_one("#mcp-tools-close", Button).focus()

    def _summary_text(self) -> str:
        if self._server_disabled:
            return "服务当前未连接（配置已禁用） · 请先启用并连接服务后管理工具"
        if not self._loaded:
            return "服务当前未连接 · 请先启用并连接服务后管理工具"
        if not self._tools:
            return "服务已连接，但当前未提供任何工具"
        enabled_count = sum(not disabled for disabled in self._draft_states.values())
        return (
            f"共 {len(self._tools)} 个工具 · 草稿启用 {enabled_count} 个 · "
            f"当前显示 {len(self._visible_tool_indices)} 个 · 可在确认前自由切换"
        )

    def _filtered_tool_indices(self) -> list[int]:
        if self._status_filter == "all":
            return list(range(len(self._tools)))
        want_disabled = self._status_filter == "disabled"
        return [
            index
            for index, item in enumerate(self._tools)
            if self._draft_states[item["original_name"]] == want_disabled
        ]

    def _tool_status(self, item: dict[str, Any]) -> Text:
        disabled = self._draft_states[item["original_name"]]
        return Text(
            "○ 禁用" if disabled else "● 启用",
            style="#94a3b8" if disabled else "bold green",
        )

    def _reload_rows(self, selected_index: int = 0) -> None:
        table = self.query_one("#mcp-tools-table", DataTable)
        table.clear(columns=False)
        self._visible_tool_indices = self._filtered_tool_indices()
        for index in self._visible_tool_indices:
            item = self._tools[index]
            table.add_row(
                self._tool_status(item),
                Text(item["name"], style="bold green" if not self._draft_states[item["original_name"]] else "#94a3b8"),
                Text(item["original_name"], style="#a1a1aa"),
                item["description"],
                key=str(index),
                height=None,
            )
        self.query_one("#mcp-tools-summary", Label).update(self._summary_text())
        if self._visible_tool_indices:
            table.move_cursor(
                row=min(selected_index, len(self._visible_tool_indices) - 1),
                column=0,
                scroll=False,
            )

    def _selected_index(self) -> int:
        table = self.query_one("#mcp-tools-table", DataTable)
        return table.cursor_row if table.cursor_row is not None else 0

    def _selected_tool_index(self) -> int | None:
        index = self._selected_index()
        if index >= len(self._visible_tool_indices):
            return None
        return self._visible_tool_indices[index]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "mcp-tools-table":
            self.action_toggle()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "mcp-tools-status-filter":
            return
        selected_tool_index = self._selected_tool_index()
        self._status_filter = str(event.value)
        table = self.query_one("#mcp-tools-table", DataTable)
        selected_index = 0
        filtered_indices = self._filtered_tool_indices()
        if selected_tool_index in filtered_indices:
            selected_index = filtered_indices.index(selected_tool_index)
        self._reload_rows(selected_index)
        if self._can_manage and self._visible_tool_indices:
            table.focus()

    def action_toggle(self) -> None:
        if not self._can_manage or not isinstance(self.focused, DataTable):
            return
        tool_index = self._selected_tool_index()
        if tool_index is None:
            return
        original_name = self._tools[tool_index]["original_name"]
        self._draft_states[original_name] = not self._draft_states[original_name]
        table = self.query_one("#mcp-tools-table", DataTable)
        previous_visible_index = self._visible_tool_indices.index(tool_index)
        filtered_indices = self._filtered_tool_indices()
        if tool_index in filtered_indices:
            self._visible_tool_indices = filtered_indices
            row_key = str(tool_index)
            item = self._tools[tool_index]
            table.update_cell(row_key, "status", self._tool_status(item))
            table.update_cell(
                row_key,
                "name",
                Text(
                    item["name"],
                    style="bold green" if not self._draft_states[original_name] else "#94a3b8",
                ),
            )
            table.move_cursor(row=previous_visible_index, column=0, scroll=False)
            self.query_one("#mcp-tools-summary", Label).update(self._summary_text())
            table.focus()
            return

        table.remove_row(str(tool_index))
        self._visible_tool_indices = filtered_indices
        self.query_one("#mcp-tools-summary", Label).update(self._summary_text())
        if self._visible_tool_indices:
            table.move_cursor(
                row=min(previous_visible_index, len(self._visible_tool_indices) - 1),
                column=0,
                scroll=False,
            )
            table.focus()
        else:
            self.query_one("#mcp-tools-status-filter", Select).focus()

    def action_confirm_or_toggle(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            self.action_toggle()
        elif isinstance(focused, Button):
            if focused.id == "mcp-tools-apply":
                self.action_apply()
            elif focused.id == "mcp-tools-close":
                self.action_cancel()

    def action_apply(self) -> None:
        if not self._can_manage or not self._tools:
            return
        disabled_tools = [
            item["original_name"]
            for item in self._tools
            if self._draft_states[item["original_name"]]
        ]
        try:
            result = self._mcp_manager.apply_tool_switches(
                self._server_name,
                disabled_tools,
            )
        except Exception as exc:
            self.query_one("#mcp-tools-title", Label).update(
                "❌ 应用 MCP 工具开关失败\n"
                f"{self._server_name}: {exc}"
            )
            return
        self.dismiss({"server": self._server_name, "result": result})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mcp-tools-apply":
            self.action_apply()
        elif event.button.id == "mcp-tools-close":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class McpViewModal(ClosableModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
    ]

    _STATUS_OPTIONS = [
        ("全部状态", "all"),
        ("已启用", "enabled"),
        ("已禁用", "disabled"),
    ]

    def __init__(self, summary: RenderableType, tools: list[dict[str, Any]]) -> None:
        super().__init__()
        self._summary = summary
        self._tools = list(tools)
        self._status_filter = "all"

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-view-dialog"):
            yield ModalHeader("🔌 MCP 状态与工具", title_id="mcp-view-title", markup=False)
            yield RichLog(id="mcp-view-summary", markup=True, wrap=True, min_width=1)
            with Horizontal(id="mcp-view-filter-row"):
                yield Label("状态筛选", id="mcp-view-filter-label", markup=False)
                yield Select(
                    self._STATUS_OPTIONS,
                    value="all",
                    allow_blank=False,
                    id="mcp-view-status-filter",
                )
            yield Label(self._filter_summary(), id="mcp-view-filter-summary", markup=False)
            yield McpDataTable(
                id="mcp-view-tools-table",
                cursor_type="row",
                show_row_labels=False,
            )
            yield Label("↑↓ 浏览工具 · 状态筛选只影响显示 · q 关闭", id="mcp-view-help", markup=False)

    def on_mount(self) -> None:
        summary = self.query_one("#mcp-view-summary", RichLog)
        summary.write(self._summary, expand=True, shrink=True, scroll_end=False)
        table = self.query_one("#mcp-view-tools-table", DataTable)
        table.add_column("状态", width=12, key="status")
        table.add_column("服务节点", width=24, key="provider")
        table.add_column("工具名称", width=32, key="name")
        table.add_column("描述", width=48, key="description")
        self._reload_rows()
        if self._tools:
            table.focus()
        else:
            self.query_one("#mcp-view-status-filter", Select).focus()

    def _filtered_tools(self) -> list[dict[str, Any]]:
        if self._status_filter == "all":
            return list(self._tools)
        want_disabled = self._status_filter == "disabled"
        return [
            tool
            for tool in self._tools
            if bool(tool.get("disabled", False)) == want_disabled
        ]

    def _filter_summary(self) -> str:
        return f"当前显示 {len(self._filtered_tools())} / {len(self._tools)} 个工具"

    def _reload_rows(self, selected_index: int = 0) -> None:
        table = self.query_one("#mcp-view-tools-table", DataTable)
        table.clear(columns=False)
        tools = self._filtered_tools()
        for index, tool in enumerate(tools):
            disabled = bool(tool.get("disabled", False))
            table.add_row(
                Text("○ 禁用" if disabled else "● 启用", style="#94a3b8" if disabled else "bold green"),
                tool.get("provider", "Unknown"),
                Text(tool["name"], style="#a1a1aa" if disabled else "bold green"),
                tool.get("description", ""),
                key=str(index),
                height=None,
            )
        self.query_one("#mcp-view-filter-summary", Label).update(self._filter_summary())
        if tools:
            table.move_cursor(row=min(selected_index, len(tools) - 1), column=0, scroll=False)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "mcp-view-status-filter":
            return
        table = self.query_one("#mcp-view-tools-table", DataTable)
        selected_index = table.cursor_row if table.cursor_row is not None else 0
        current_tools = self._filtered_tools()
        selected_tool = (
            current_tools[selected_index]
            if selected_index < len(current_tools)
            else None
        )
        self._status_filter = str(event.value)
        filtered_tools = self._filtered_tools()
        if selected_tool is not None:
            selected_index = next(
                (
                    index
                    for index, tool in enumerate(filtered_tools)
                    if tool is selected_tool
                ),
                0,
            )
        else:
            selected_index = 0
        self._reload_rows(selected_index)
        if filtered_tools:
            table.focus()
        else:
            self.query_one("#mcp-view-status-filter", Select).focus()

    def action_close(self) -> None:
        self.dismiss("closed")


class McpSwitchModal(ClosableModalScreen[str | dict]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "confirm_or_toggle", "Toggle/Confirm", priority=True),
        Binding("space", "toggle", "Toggle", priority=True),
        Binding("t", "manage_tools", "Manage Tools", priority=True),
        Binding("a", "add", "Add", priority=True),
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
        self._added_results: list[dict[str, Any]] = []
        self._tool_results: list[dict[str, Any]] = []
        self._row_reload_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-dialog"):
            yield ModalHeader(self._title_text(), title_id="mcp-title", markup=False)
            yield Label(self._summary_text(), id="mcp-summary", markup=False)
            with SelectBeforeActivateListView(id="mcp-list"):
                for index, item in enumerate(self._server_switches):
                    yield self._server_item(index, item)
            yield Label(
                "↑↓ 选择服务 · Enter/Space 切换 · t 管理工具 · a 添加 · d 删除 · Tab 切换到操作按钮 · q 取消",
                id="mcp-help",
                markup=False,
            )
            with Horizontal(id="mcp-actions"):
                yield Button("确认应用", id="mcp-apply", variant="success", classes="mcp-action")
                yield Button("管理工具", id="mcp-tools", variant="primary", classes="mcp-action")
                yield Button("添加服务", id="mcp-add", variant="primary", classes="mcp-action")
                yield Button("取消", id="mcp-cancel", variant="warning", classes="mcp-action")

    def on_mount(self) -> None:
        choice_list = self.query_one("#mcp-list", ListView)
        if self._server_switches:
            choice_list.index = 0
            choice_list.focus()
        else:
            self.query_one("#mcp-add", Button).focus()

    def _title_text(self) -> str:
        return "🔌 MCP 服务管理"

    def _summary_text(self) -> str:
        enabled_count = sum(not disabled for disabled in self._draft_states.values())
        loaded_count = sum(item.get("loaded", False) for item in self._server_switches)
        return f"共 {len(self._server_switches)} 个服务 · 草稿启用 {enabled_count} 个 · 当前连接 {loaded_count} 个 · 可在应用前自由切换"

    def _reset_header(self) -> None:
        self.query_one("#mcp-title", Label).update(self._title_text())
        self.query_one("#mcp-summary", Label).update(self._summary_text())

    def _server_item(self, index: int, item: dict[str, Any]) -> ListItem:
        return ListItem(Label(self._server_label(item), markup=False), id=f"mcp-server-{index}")

    def _server_label(self, item: dict[str, Any]) -> Text:
        name = item["name"]
        enabled = not self._draft_states[name]
        loaded = item.get("loaded", False)
        status_txt = "启用" if enabled else "禁用"
        runtime_txt = "已加载" if loaded else "未加载"
        transport = item.get("transport", "未知协议")
        target = item.get("target", "未配置连接目标")
        tool_count = item.get("tool_count", 0)

        label = Text()
        label.append("● " if enabled else "○ ", style="green" if enabled else "#64748b")
        label.append(str(name))
        label.append("\n   草稿：")
        label.append(status_txt, style="green" if enabled else "#94a3b8")
        label.append(" · 运行：")
        runtime_style = "green" if loaded else "yellow" if enabled else "#64748b"
        label.append(runtime_txt, style=runtime_style)
        label.append(f" · 协议：{transport} · 工具：{tool_count}")
        label.append(f"\n   目标：{target}")
        return label

    def _selected_index(self) -> int:
        choice_list = self.query_one("#mcp-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _refresh_server_row(self, index: int) -> None:
        choice_list = self.query_one("#mcp-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._server_label(self._server_switches[index]))
        choice_list.index = index
        choice_list.focus()
        self.query_one("#mcp-summary", Label).update(self._summary_text())

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_name = None
        self._pending_delete_index = None
        self._reset_header()
        choice_list = self.query_one("#mcp-list", ListView)
        self._row_reload_generation += 1
        reload_generation = self._row_reload_generation

        async def _mount_rows() -> None:
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.clear()
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.extend(
                self._server_item(index, item)
                for index, item in enumerate(self._server_switches)
            )
            if reload_generation != self._row_reload_generation:
                return
            if not self._server_switches:
                self.query_one("#mcp-apply", Button).focus()
                return
            choice_list.index = min(selected_index or 0, len(self._server_switches) - 1)
            choice_list.focus()

        self.call_after_refresh(_mount_rows)

    def _dismiss_payload(self, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "disabled_updates": dict(self._draft_states),
            "deleted_results": list(self._deleted_results),
            "added_results": list(self._added_results),
            "tool_results": list(self._tool_results),
        }

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_toggle()

    def action_confirm_or_toggle(self) -> None:
        if self._pending_delete_name is not None:
            return
        focused = self.focused
        if isinstance(focused, ListView):
            self.action_toggle()
        elif isinstance(focused, Button):
            if focused.id == "mcp-apply":
                self.dismiss(self._dismiss_payload("confirm"))
            elif focused.id == "mcp-cancel":
                self.action_cancel()
            elif focused.id == "mcp-tools":
                self.action_manage_tools()
            elif focused.id == "mcp-add":
                self.action_add()

    def action_manage_tools(self) -> None:
        if self._pending_delete_name is not None or not self._server_switches:
            return
        selected_index = self._selected_index()
        if selected_index >= len(self._server_switches):
            return
        server_name = self._server_switches[selected_index]["name"]
        try:
            tool_switches = self._mcp_manager.list_tool_switches(server_name)
        except Exception as exc:
            self.query_one("#mcp-title", Label).update(
                "❌ 读取 MCP 工具列表失败\n"
                f"{server_name}: {exc}"
            )
            return
        self.app.push_screen(
            McpToolsModal(tool_switches, self._mcp_manager),
            lambda result: self._finish_manage_tools(selected_index, result),
        )

    def _finish_manage_tools(
        self,
        selected_index: int,
        tool_result: dict[str, Any] | None,
    ) -> None:
        if tool_result is not None:
            self._tool_results.append(tool_result)
            result = tool_result.get("result", {})
            saved = bool(result.get("saved"))
            icon = "✅" if saved else "ℹ️"
            status_text = "已保存" if saved else "未变更"
            self.query_one("#mcp-title", Label).update(
                f"{icon} MCP 工具开关{status_text}\n"
                f"{tool_result.get('server', '')} · {result.get('message', '')}"
            )
        choice_list = self.query_one("#mcp-list", ListView)
        if self._server_switches:
            choice_list.index = min(selected_index, len(self._server_switches) - 1)
            choice_list.focus()

    def action_add(self) -> None:
        if self._pending_delete_name is not None:
            return
        self.app.push_screen(McpAddModal(), self._finish_add_server)

    def _finish_add_server(self, form_result: dict[str, Any] | None) -> None:
        if form_result is None:
            return
        server_name = form_result["server_name"]
        cfg = form_result["cfg"]
        try:
            result = self._mcp_manager.add_server_config(server_name, cfg)
            server_switches = self._mcp_manager.list_server_switches()
        except Exception as exc:
            self.query_one("#mcp-title", Label).update(
                "❌ 添加 MCP 服务失败\n"
                f"{server_name}: {exc}\n"
                "请检查参数后重新添加。"
            )
            self.query_one("#mcp-add", Button).focus()
            return

        draft_states = dict(self._draft_states)
        self._server_switches = server_switches
        self._draft_states = {
            item["name"]: draft_states.get(item["name"], bool(item["disabled"]))
            for item in server_switches
        }
        self._added_results.append({"server": server_name, "result": result})
        selected_index = next(
            (index for index, item in enumerate(server_switches) if item["name"] == server_name),
            0,
        )
        self._reload_rows(selected_index)

    def action_toggle(self) -> None:
        if self._pending_delete_name is not None or not isinstance(self.focused, ListView):
            return
        index = self._selected_index()
        if index < len(self._server_switches):
            self._toggle_index(index)

    def action_delete(self) -> None:
        if not isinstance(self.focused, ListView):
            return
        index = self._selected_index()
        if index >= len(self._server_switches):
            return
        server_name = self._server_switches[index]["name"]
        self._pending_delete_name = server_name
        self._pending_delete_index = index
        self.query_one("#mcp-title", Label).update(
            "⚠️ 确认删除 MCP 服务配置？\n"
            f"{server_name}\n"
            "该操作会写入配置文件，并停用运行中的同名服务。按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_name is None:
            return
        server_name = self._pending_delete_name
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        try:
            result = self._mcp_manager.delete_server_config(server_name)
        except Exception as exc:
            self.query_one("#mcp-title", Label).update(
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
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        self._pending_delete_name = None
        self._pending_delete_index = None
        self._reset_header()
        choice_list = self.query_one("#mcp-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mcp-apply":
            if self._pending_delete_name is None:
                self.dismiss(self._dismiss_payload("confirm"))
        elif event.button.id == "mcp-add":
            self.action_add()
        elif event.button.id == "mcp-tools":
            self.action_manage_tools()
        elif event.button.id == "mcp-cancel":
            self.action_cancel()

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

    BINDINGS: list[Binding] = []

    def __init__(self, model_manager: Any) -> None:
        super().__init__()
        self._model_manager = model_manager
        self._model_keys: list[ModelKey | None] = []
        self._pending_delete_key: ModelKey | None = None
        self._pending_delete_index: int | None = None
        self._row_reload_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="model-manager-dialog"):
            yield ModalHeader(self._title_text(), title_id="model-manager-title", markup=False)
            yield Label(self._summary_text(), id="model-manager-summary", markup=False)
            yield ListView(id="model-manager-list")
            yield Label(
                "↑↓ 选择模型 · Enter 切换并关闭 · ←/→ 调整 effort · f 常用 · a 添加 · d 删除 · q 关闭",
                id="model-manager-help",
                markup=False,
            )
            with Horizontal(id="model-manager-actions"):
                yield Button("添加模型", id="model-manager-add", variant="primary", classes="model-manager-action")
                yield Button("设为当前", id="model-manager-select", variant="success", classes="model-manager-action")
                yield Button("关闭", id="model-manager-close", variant="warning", classes="model-manager-action")

    @staticmethod
    def _title_text() -> str:
        efforts = " / ".join(REASONING_EFFORTS)
        return f"⚙️ 模型管理 · 思考档位：{efforts}"

    def _summary_text(self) -> str:
        current = self._model_manager.get_current_model()
        current_text = (
            f"{current.get_display_text()} · {current.message_format} · effort {current.reasoning_effort}"
            if current
            else "未选择"
        )
        favorite_count = sum(model.is_favorite for model in self._model_manager.models)
        return f"共 {len(self._model_manager.models)} 个模型 · 常用 {favorite_count} 个 · 当前：{current_text}"

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#model-manager-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _model_label(self, model: Any, current_key: ModelKey | None) -> Text:
        is_current = model.key == current_key
        label = Text()
        label.append("● " if is_current else "○ ", style="bold green" if is_current else "#64748b")
        label.append(model.model_id, style="bold #e2e8f0")
        if model.is_favorite:
            label.append("  ♥ 常用", style="bold #f472b6")
        label.append(f"\n   服务：{model.get_display_name()}", style="#94a3b8")
        label.append(f" · 格式：{model.message_format}", style="#94a3b8")
        label.append(" · effort：", style="#94a3b8")
        label.append(model.reasoning_effort, style="bold #c4b5fd")
        return label

    def _reload_rows(
            self,
            selected_index: int | None = None,
            selected_key: ModelKey | None = None,
    ) -> None:
        self._pending_delete_key = None
        self._pending_delete_index = None
        loaded = self._model_manager._reload_from_disk()
        current_model = self._model_manager.get_current_model()
        current_key = current_model.key if current_model else None
        labels = [self._model_label(model, current_key) for model in self._model_manager.models]
        self._model_keys = [model.key for model in self._model_manager.models]
        if selected_key is not None:
            selected_index = next(
                (index for index, key in enumerate(self._model_keys) if key == selected_key),
                0,
            )
        if not labels:
            labels = [Text("暂无模型。点击“添加模型”或按 a 创建第一项配置。", style="#94a3b8")]
            self._model_keys = [None]

        self._reset_header()
        if not loaded:
            self.query_one("#model-manager-title", Label).update(
                "⚠️ 模型配置读取失败，已禁止写入以避免覆盖原文件。\n"
                f"{self._model_manager.get_load_error_display()}\n"
                "请修复 model_config.json 后重新打开 /models。"
            )
        choice_list = self.query_one("#model-manager-list", ListView)
        self._row_reload_generation += 1
        reload_generation = self._row_reload_generation

        async def _mount_rows() -> None:
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.clear()
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.extend(ListItem(Label(label, markup=False)) for label in labels)
            if reload_generation != self._row_reload_generation:
                return
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            if self._model_manager.models:
                choice_list.focus()
            else:
                self.query_one("#model-manager-add", Button).focus()

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
        choice_list = self.query_one("#model-manager-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._model_label(model, current_key))
        choice_list.index = index
        choice_list.focus()
        self.query_one("#model-manager-summary", Label).update(self._summary_text())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "model-manager-list":
            self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()
            return
        if event.key == "y" and self._pending_delete_key is not None:
            self.action_confirm_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "n" and self._pending_delete_key is not None:
            self.action_cancel_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "a":
            self.action_add()
            event.stop()
            event.prevent_default()
            return
        if not isinstance(self.focused, ListView):
            return
        key_actions = {
            "enter": self.action_select,
            "f": self.action_favorite,
            "d": self.action_delete,
            "left": self.action_decrease_effort,
            "right": self.action_increase_effort,
        }
        action = key_actions.get(event.key)
        if action is None:
            return
        action()
        event.stop()
        event.prevent_default()

    def action_select(self) -> None:
        if self._pending_delete_key is not None:
            return
        if not self._model_manager.models:
            self.action_add()
            return
        index = self._selected_index()
        if index >= len(self._model_keys):
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        selected_model = self._model_manager.models[target_index]
        current_model = self._model_manager.get_current_model()
        previous_runtime_key = current_model.runtime_key if current_model else None
        if self._model_manager.select_model(model_key, selected_model.reasoning_effort):
            current_model = self._model_manager.get_current_model()
            if current_model and current_model.runtime_key != previous_runtime_key:
                self.app.refresh_status()
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
        if index >= len(self._model_keys):
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
        if self._pending_delete_key is not None:
            return
        index = self._selected_index()
        if index >= len(self._model_keys):
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        if self._model_manager.toggle_favorite_by_index(target_index):
            self._reload_rows(selected_key=model_key)

    def action_delete(self) -> None:
        if self._pending_delete_key is not None:
            return
        index = self._selected_index()
        if index >= len(self._model_keys):
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
        self.query_one("#model-manager-title", Label).update(
            "⚠️ 确认删除模型？\n"
            f"{selected_model.get_display_text()}\n"
            "删除会立即写入模型配置。按 y 确认，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_key is None:
            return
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        deleting_current = self._model_manager.current_model_key == self._pending_delete_key
        deleted = self._model_manager.delete_model_by_key(self._pending_delete_key)
        self._reload_rows(selected_index)
        if deleted and deleting_current:
            self.app.refresh_status()

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        self._pending_delete_key = None
        self._pending_delete_index = None
        self._reset_header()
        choice_list = self.query_one("#model-manager-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def _reset_header(self) -> None:
        self.query_one("#model-manager-title", Label).update(self._title_text())
        self.query_one("#model-manager-summary", Label).update(self._summary_text())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "model-manager-add":
            self.action_add()
        elif event.button.id == "model-manager-select":
            self.action_select()
        elif event.button.id == "model-manager-close":
            self.action_close()

    def action_add(self) -> None:
        if self._pending_delete_key is None:
            self._add_model(self._selected_index())

    def action_close(self) -> None:
        self.dismiss("exit")

    def _add_model(self, selected_index: int) -> None:
        self.app.push_screen(AddModelModal(), lambda model_config: self._finish_add_model(model_config, selected_index))

    def _finish_add_model(self, model_config: dict[str, str] | None, selected_index: int) -> None:
        if model_config is None:
            self._reload_rows(selected_index)
            return
        current_model = self._model_manager.get_current_model()
        previous_runtime_key = current_model.runtime_key if current_model else None
        model_ids = [
            item.strip()
            for item in model_config["model_input"].replace("，", ",").split(",")
            if item.strip()
        ]
        new_models = self._model_manager.add_model(
            model_config["base_url"],
            model_config["api_key"],
            model_ids,
            message_format=model_config["message_format"],
        )
        selected_key = new_models[0].key if new_models else None
        self._reload_rows(selected_index, selected_key=selected_key)
        current_model = self._model_manager.get_current_model()
        if current_model and current_model.runtime_key != previous_runtime_key:
            self.app.refresh_status()

    def _target_index(self, model_key: ModelKey) -> int | None:
        return next(
            (index for index, model in enumerate(self._model_manager.models) if model.key == model_key),
            None,
        )


class MemoryPanelModal(ClosableModalScreen[list[str]]):
    CSS = ChoiceModal.CSS
    BINDINGS: list[Binding] = []

    def __init__(self, memory_provider: Any) -> None:
        super().__init__()
        self._memory_provider = memory_provider
        self._memories: list[dict[str, Any]] = []
        self._expanded_id: str | None = None
        self._pending_delete_id: str | None = None
        self._pending_delete_index: int | None = None
        self._deleted_ids: list[str] = []
        self._row_reload_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-dialog"):
            yield ModalHeader(self._title_text(), title_id="memory-title", markup=False)
            yield Label(self._summary_text(), id="memory-summary", markup=False)
            yield ListView(id="memory-list")
            yield RichLog(id="memory-detail", markup=False, wrap=True, min_width=1)
            yield Label(
                "↑↓ 选择记忆 · Enter/Space 查看详情 · a 添加 · d 删除 · Tab 切换到操作按钮 · q 关闭",
                id="memory-help",
                markup=False,
            )
            with Horizontal(id="memory-actions"):
                yield Button("添加记忆", id="memory-add", variant="primary", classes="memory-action")
                yield Button("删除选中", id="memory-delete", variant="error", classes="memory-action")
                yield Button("关闭", id="memory-close", variant="warning", classes="memory-action")

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#memory-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _memory_label(self, item: dict[str, Any]) -> Text:
        memory_id = str(item.get("id", ""))
        category = str(item.get("category", "未分类"))
        updated_at = str(item.get("updated_at", ""))
        insight = " ".join(str(item.get("insight", "")).splitlines())
        if len(insight) > 160:
            insight = f"{insight[:157]}..."

        label = Text()
        label.append("▼ " if memory_id == self._expanded_id else "● ", style="#f59e0b")
        label.append(category)
        label.append(f"  ·  {updated_at}")
        label.append(f"\n   {insight or '（无内容）'}")
        label.append(f"\n   ID: {memory_id}")
        return label

    def _reload_rows(self, selected_index: int | None = None, selected_id: str | None = None) -> None:
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._memories = sorted(
            self._memory_provider.list_long_term_memories(),
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        if selected_id is not None:
            selected_index = next(
                (index for index, item in enumerate(self._memories) if item.get("id") == selected_id),
                0,
            )
        self._reset_header()
        choice_list = self.query_one("#memory-list", ListView)
        self._row_reload_generation += 1
        reload_generation = self._row_reload_generation

        labels = [self._memory_label(item) for item in self._memories]
        if not labels:
            labels = [Text("暂无长期记忆。点击“添加记忆”或按 a 创建第一条记忆。")]

        async def _mount_rows() -> None:
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.clear()
            if reload_generation != self._row_reload_generation:
                return
            await choice_list.extend(ListItem(Label(label, markup=False)) for label in labels)
            if reload_generation != self._row_reload_generation:
                return
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            if self._memories:
                choice_list.focus()
            else:
                self.query_one("#memory-add", Button).focus()
            self._update_detail()

        self.call_after_refresh(_mount_rows)

    @staticmethod
    def _title_text() -> str:
        return "🧠 长期记忆管理"

    def _summary_text(self) -> str:
        categories = len({str(item.get("category", "")) for item in self._memories})
        return f"共 {len(self._memories)} 条 active 记忆 · {categories} 个分类 · 新增和删除会立即写入当前工作区"

    def _reset_header(self) -> None:
        self.query_one("#memory-title", Label).update(self._title_text())
        self.query_one("#memory-summary", Label).update(self._summary_text())

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
            detail.write("按 Enter/Space 展开当前记忆的完整内容。", expand=True, shrink=True)
            return
        detail.write(
            "\n".join(
                [
                    f"ID: {current.get('id', '')}",
                    f"Category: {current.get('category', '')}",
                    f"Created: {current.get('created_at', '')}",
                    f"Updated: {current.get('updated_at', '')}",
                    "",
                    f"Insight:\n{current.get('insight', '')}",
                    "",
                    f"Evidence:\n{current.get('evidence', '') or '（未填写）'}",
                    "",
                    f"Reuse condition:\n{current.get('reuse_condition', '')}",
                ]
            ),
            expand=True,
            shrink=True,
            scroll_end=False,
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "memory-list":
            self.action_toggle_detail()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "memory-list":
            self._update_detail()

    def _on_key(self, event: Key) -> None:
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()
            return
        if event.key == "y" and self._pending_delete_id is not None:
            self.action_confirm_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "n" and self._pending_delete_id is not None:
            self.action_cancel_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "a":
            self.action_add()
            event.stop()
            event.prevent_default()
            return
        if not isinstance(self.focused, ListView):
            return
        key_actions = {
            "enter": self.action_toggle_detail,
            "space": self.action_toggle_detail,
            "d": self.action_delete,
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
        choice_list = self.query_one("#memory-list", ListView)
        for index, item in enumerate(self._memories):
            choice_list.children[index].query_one(Label).update(self._memory_label(item))
        self._update_detail()
        choice_list.focus()

    def action_add(self) -> None:
        if self._pending_delete_id is not None:
            return
        self.app.push_screen(AddMemoryModal(), self._finish_add_memory)

    def _finish_add_memory(self, values: dict[str, str] | None) -> None:
        if values is None:
            self.query_one("#memory-list", ListView).focus()
            return
        try:
            record = self._memory_provider.append_long_term_memory(**values)
        except Exception as exc:
            self.query_one("#memory-title", Label).update(f"❌ 添加长期记忆失败：{exc}")
            return
        memory_id = record.get("id") if isinstance(record, dict) else None
        self._expanded_id = memory_id
        self._reload_rows(selected_id=memory_id)
        self.app.refresh_status()

    def action_delete(self) -> None:
        if self._pending_delete_id is not None:
            return
        current = self._current_memory()
        if current is None:
            return
        self._pending_delete_id = current.get("id")
        self._pending_delete_index = self._selected_index()
        self.query_one("#memory-title", Label).update(
            "⚠️ 确认删除长期记忆？\n"
            f"{self._pending_delete_id}\n"
            "删除会立即写入当前工作区。按 y 确认，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_id is None:
            return
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        deleted = self._memory_provider.delete_long_term_memory(self._pending_delete_id)
        if deleted:
            self._deleted_ids.append(self._pending_delete_id)
        if self._expanded_id == self._pending_delete_id:
            self._expanded_id = None
        self._reload_rows(selected_index)
        if deleted:
            self.app.refresh_status()

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index if self._pending_delete_index is not None else self._selected_index()
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._reset_header()
        choice_list = self.query_one("#memory-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "memory-add":
            self.action_add()
        elif event.button.id == "memory-delete":
            self.action_delete()
        elif event.button.id == "memory-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(list(self._deleted_ids))


class AddMemoryModal(ClosableModalScreen[dict[str, str] | None]):
    CSS = ChoiceModal.CSS

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-add-dialog"):
            yield ModalHeader("➕ 添加长期记忆", title_id="memory-add-title", markup=False)
            with VerticalScroll(id="memory-add-fields"):
                yield Label("分类（category）", classes="memory-add-label")
                yield Input(
                    placeholder="例如 preference、project-convention、workflow",
                    id="memory-add-category",
                )
                yield Label("内容（insight）", classes="memory-add-label")
                yield TextArea("", id="memory-add-insight", classes="memory-add-textarea")
                yield Label("依据（evidence，可选）", classes="memory-add-label")
                yield TextArea("", id="memory-add-evidence", classes="memory-add-textarea")
                yield Label("复用条件（reuse condition）", classes="memory-add-label")
                yield TextArea("", id="memory-add-reuse-condition", classes="memory-add-textarea")
                yield Label("", id="memory-add-error", markup=False)
                yield Label(
                    "Ctrl+Enter 保存；保存后直接调用 append 入库；若超过容量上限，append 逻辑会淘汰最旧的 active 记忆。",
                    id="memory-add-hint",
                    markup=False,
                )
            with Horizontal(id="memory-add-actions"):
                yield Button("保存记忆", id="memory-add-confirm", variant="success", classes="memory-add-action")
                yield Button("取消", id="memory-add-cancel", variant="warning", classes="memory-add-action")

    def on_mount(self) -> None:
        self.query_one("#memory-add-category", Input).focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "ctrl+enter":
            self.action_submit()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and not isinstance(self.focused, (Input, TextArea)):
            self.action_cancel()
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#memory-add-insight", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "memory-add-confirm":
            self.action_submit()
        elif event.button.id == "memory-add-cancel":
            self.action_cancel()

    def action_submit(self) -> None:
        category = self.query_one("#memory-add-category", Input).value.strip()
        insight = self.query_one("#memory-add-insight", TextArea).text.strip()
        evidence = self.query_one("#memory-add-evidence", TextArea).text.strip()
        reuse_condition = self.query_one("#memory-add-reuse-condition", TextArea).text.strip()
        if not category:
            self._show_error("请填写分类。")
            return
        if not insight:
            self._show_error("请填写记忆内容。")
            return
        if not reuse_condition:
            self._show_error("请填写复用条件。")
            return
        self.dismiss({
            "category": category,
            "insight": insight,
            "evidence": evidence,
            "reuse_condition": reuse_condition,
        })

    def _show_error(self, message: str) -> None:
        error = self.query_one("#memory-add-error", Label)
        error.update(message)
        error.display = True

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        "tool_output_compact_threshold": {
            "label": "第一层工具输出压缩阈值（%）",
            "input_id": "memory-config-tool-output-compact-threshold",
        },
        "partial_compact_threshold": {
            "label": "第二层局部压缩阈值（%）",
            "input_id": "memory-config-partial-compact-threshold",
        },
        "tool_output_compact_tokens": {
            "label": "第一层压缩后保留总 tokens（偶数）",
            "input_id": "memory-config-tool-output-compact-tokens",
        },
        "partial_compact_min_percent": {
            "label": "第二层可压缩落点下限（%）",
            "input_id": "memory-config-partial-compact-min-percent",
        },
        "partial_compact_max_percent": {
            "label": "第二层可压缩落点上限（%）",
            "input_id": "memory-config-partial-compact-max-percent",
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
        with VerticalScroll(id="memory-config-dialog"):
            yield ModalHeader(
                "🧠 记忆配置\n修改后按 Enter 或点击确认应用；配置值必须是大于 0 的整数。",
                title_id="choice-title",
            )
            for field in (
                "context_length",
                "memory_size",
                "tool_output_compact_threshold",
                "partial_compact_threshold",
                "memory_recall_window_size",
            ):
                meta = self._FIELDS[field]
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
            for field in (
                "tool_output_compact_tokens",
                "partial_compact_min_percent",
                "partial_compact_max_percent",
            ):
                meta = self._FIELDS[field]
                yield Label(f"{meta['label']} ({field})", classes="memory-config-label")
                yield Input(
                    value=str(self._values[field]),
                    id=meta["input_id"],
                    classes="memory-config-input",
                )
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
            if field == "tool_output_compact_tokens" and value % 2:
                self._show_error(f"{meta['label']} 必须是正偶数。")
                return None
            values[field] = value
        tool_output_threshold = values["tool_output_compact_threshold"]
        partial_threshold = values["partial_compact_threshold"]
        if not 0 < tool_output_threshold < partial_threshold < 100:
            self._show_error("压缩阈值必须满足 0 < 第一层阈值 < 第二层阈值 < 100。")
            return None
        partial_min_percent = values["partial_compact_min_percent"]
        partial_max_percent = values["partial_compact_max_percent"]
        if not 0 < partial_min_percent < partial_max_percent < 100:
            self._show_error("第二层可压缩落点必须满足 0 < 下限 < 上限 < 100。")
            return None
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
            with Vertical(id="layout-columns"):
                yield Label("右侧高度比例")
                for key in LAYOUT_RIGHT_KEYS:
                    yield Button(self._button_label(key), id=f"layout-{key}", classes="layout-button")
            with Horizontal(id="layout-actions"):
                yield Button("确认应用", id="layout-apply", variant="success", classes="layout-action-button")
                yield Button("重置默认", id="layout-reset", variant="primary", classes="layout-action-button")
                yield Button("取消", id="layout-cancel", variant="warning", classes="layout-action-button")

    def on_mount(self) -> None:
        self.query_one("#layout-task", Button).focus()

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
