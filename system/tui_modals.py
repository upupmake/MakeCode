import json
from typing import Any
from pathlib import Path

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, TextArea

from system.tui_types import (
    LAYOUT_DEFAULT_RATIOS,
    LAYOUT_LEFT_KEYS,
    LAYOUT_RIGHT_KEYS,
    normalize_layout_ratios,
)


class ChoiceModal(ModalScreen[str]):
    CSS = """
    ChoiceModal, DelegateTasksModal, StartupWorkdirModal, ModelPanelModal, McpSwitchModal, ModelManagerModal, AddModelModal, LayoutModal, MemoryPanelModal, MemoryConfigModal, RecallModelPickerModal, InfoPanelModal, CopyContentModal {
        align: center middle;
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
        margin-bottom: 1;
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
        width: 88%;
        height: 86%;
        border: round #38bdf8;
        background: $surface;
        padding: 1 2;
    }

    #copy-title {
        height: auto;
        margin-bottom: 1;
    }

    #copy-text {
        height: 1fr;
        margin-top: 1;
    }

    #copy-actions {
        height: 3;
        margin-top: 1;
    }

    #copy-close {
        width: 16;
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
        margin-bottom: 1;
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
    ]

    def __init__(self, title: str, options: list[str], allow_custom: bool = False) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._allow_custom = allow_custom

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label(self._title, id="choice-title")
            if self._options:
                yield ListView(*[ListItem(Label(option)) for option in self._options], id="choice-list")
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
        self.dismiss(self._options[event.list_view.index or 0])

    def _on_key(self, event: Key) -> None:
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

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")


class DelegateTasksModal(ModalScreen[str]):
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
            yield Label("子智能体委派确认", id="delegate-title", markup=False)
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


class StartupWorkdirModal(ModalScreen[str]):
    CSS = ChoiceModal.CSS

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd
        self._selected_index = 0
        self._mode = "select"
        self._ignore_initial_custom_submit = False

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-dialog"):
            yield Label(id="startup-title")
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


class InfoPanelModal(ModalScreen[str]):
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
            yield Label(f"{self._title}\nq 关闭。", id="choice-title")
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


class CopyContentModal(ModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
    ]

    def __init__(self, messages: list[dict[str, str]]) -> None:
        super().__init__()
        self._messages = messages

    def compose(self) -> ComposeResult:
        with Vertical(id="copy-dialog"):
            yield Label(
                "📝 对话内容（只读）\nc 复制选中文本（无选区则复制全部）· q 关闭",
                id="copy-title",
            )
            yield TextArea(
                self._build_text(),
                id="copy-text",
                read_only=True,
                show_line_numbers=False,
            )
            yield Label("", id="copy-status")
            with Horizontal(id="copy-actions"):
                yield Button("关闭", id="copy-close", variant="primary")

    def on_mount(self) -> None:
        text_area = self.query_one("#copy-text", TextArea)
        text_area.focus()
        # Scroll to end
        last_line = text_area.document.line_count - 1
        last_col = len(text_area.document.get_line(last_line)) if last_line >= 0 else 0
        text_area.move_cursor((last_line, last_col))
        text_area.scroll_cursor_visible()

    def _on_key(self, event: Key) -> None:
        if event.key == "c":
            text_area = self.query_one("#copy-text", TextArea)
            selected = text_area.selected_text
            text_to_copy = selected if selected else text_area.text
            self.app.copy_to_clipboard(text_to_copy)
            self.query_one("#copy-status", Label).update("📋 已复制文本到剪贴板。")
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            # Don't close if TextArea has selection (let it handle q normally)
            text_area = self.query_one("#copy-text", TextArea)
            if text_area.selected_text:
                return
            self.action_close()
            event.stop()
            event.prevent_default()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss("closed")

    def _build_text(self) -> str:
        parts: list[str] = []
        for msg in self._messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                parts.append(f"{'─' * 8} User {'─' * 8}\n{content}")
            elif role == "assistant":
                section_parts = []
                if content:
                    section_parts.append(content)
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    if func.get("name") == "RunTerminalCommand":
                        try:
                            raw_args = func.get("arguments", "{}")
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                            cmd = args.get("command", "")
                            if cmd:
                                section_parts.append(f"[RunTerminalCommand] $ {cmd}")
                        except (json.JSONDecodeError, AttributeError):
                            pass
                if section_parts:
                    parts.append(f"{'─' * 4} Assistant {'─' * 4}\n" + "\n".join(section_parts))
            elif role == "tool" and msg.get("name") == "RunTerminalCommand":
                output = content.strip() if content else ""
                if output:
                    parts.append(f"{'─' * 4} Terminal Output {'─' * 4}\n{output}")
        return "\n\n".join(parts)


class McpSwitchModal(ModalScreen[str | dict]):
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
            yield Label(self._title_text(), id="choice-title")
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

    def _toggle_index(self, index: int) -> None:
        name = self._server_switches[index]["name"]
        self._draft_states[name] = not self._draft_states[name]
        self._refresh_server_row(index)


class ModelPanelModal(ModalScreen[str]):
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
            yield Label(self._title, id="choice-title")
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


class ModelManagerModal(ModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("q", "close", "Close", priority=True),
        Binding("f", "favorite", "Favorite", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, model_manager: Any) -> None:
        super().__init__()
        self._model_manager = model_manager
        self._model_keys: list[tuple[str, str, str] | None] = []
        self._pending_delete_key: tuple[str, str, str] | None = None
        self._pending_delete_index: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label("⚙️ 模型管理面板\nEnter 选择当前模型并关闭；f 切换常用；d 删除；选择添加模型可新增配置；q 关闭。", id="choice-title")
            yield ListView(id="choice-list")

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _model_label(self, model: Any, current_key: tuple[str, str, str] | None) -> str:
        markers = []
        if model.key == current_key:
            markers.append("✓")
        if model.is_favorite:
            markers.append("♥")
        marker_text = " ".join(markers) if markers else " "
        return f"[{marker_text:^3}] {model.get_display_text()}"

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
        keys: list[tuple[str, str, str] | None] = [None]
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

    def _refresh_model_row_by_key(self, model_key: tuple[str, str, str]) -> None:
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
        if self._model_manager.set_current_model_by_index(target_index):
            self.dismiss(f"selected:{selected_model.get_display_text()}")

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
        self.query_one("#choice-title", Label).update(
            "⚙️ 模型管理面板\nEnter 选择当前模型并关闭；f 切换常用；d 删除；选择添加模型可新增配置；q 关闭。"
        )

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
        self._model_manager.add_model(model_config["base_url"], model_config["api_key"], model_ids)
        self._reload_rows(selected_index)

    def _target_index(self, model_key: tuple[str, str, str]) -> int | None:
        return next(
            (index for index, model in enumerate(self._model_manager.models) if model.key == model_key),
            None,
        )


class MemoryPanelModal(ModalScreen[list[str]]):
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
            yield Label("🧠 长期记忆面板 (active: 0)\nEnter/Space 查看详情；d 删除；q 关闭。", id="choice-title")
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


class MemoryConfigModal(ModalScreen[str | dict[str, Any]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    _FIELDS = {
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
            yield Label(
                "🧠 记忆配置\n修改后按 Enter 或点击确认应用；配置值必须是大于 0 的整数。",
                id="choice-title",
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


class RecallModelPickerModal(ModalScreen[str]):
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
            yield Label("🧠 选择记忆召回模型\nEnter 选择；q 取消。", id="choice-title")

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


class AddModelModal(ModalScreen[dict[str, str] | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="model-form-dialog"):
            yield Label("➕ 添加模型", id="choice-title")
            yield Label("Base URL", classes="model-form-label")
            yield Input(placeholder="https://api.example.com/v1", id="model-base-url", classes="model-form-input")
            yield Label("API Key", classes="model-form-label")
            yield Input(placeholder="API Key", password=True, id="model-api-key", classes="model-form-input")
            yield Label("Model ID(s)（多个用逗号分隔）", classes="model-form-label")
            yield Input(placeholder="model-a, model-b", id="model-ids", classes="model-form-input")
            yield Label("提示：填写完成后点击“确定”，也可以按 Enter 提交。", id="model-form-hint")
            with Horizontal(id="custom-actions"):
                yield Button("确定", id="model-confirm", variant="success")
                yield Button("取消", id="custom-cancel", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#model-base-url", Input).focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_submit()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and not isinstance(self.focused, Input):
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
        if not base_url or not api_key or not model_input:
            return
        self.dismiss({"base_url": base_url, "api_key": api_key, "model_input": model_input})

    def action_cancel(self) -> None:
        self.dismiss(None)


class LayoutModal(ModalScreen[str | dict[str, int]]):
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
            yield Label(
                "🧩 Layout 布局比例\n点击按钮或按 Space 在 0-10 间循环；0 表示隐藏但继续接收渲染。",
                id="choice-title",
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

