"""纯终端渲染后端：不启动 Textual，按事件顺序把内容直接输出到 stdout。"""

import threading
from typing import Any

from rich.console import Console, RenderableType
from rich.errors import MarkupError
from rich.text import Text

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

    def handle(self, event: TuiEvent) -> None:
        if event.region in _SUPPRESSED_REGIONS:
            return

        with self._lock:
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

            # 流式尾部是尚未定型的增量文本，完整段落会单独提交，这里跳过避免重复输出。
            if event.tail:
                return

            if event.payload is None or event.payload == "":
                return

            self._print_region_label(event.region)
            self._print(event.payload)

    def choose(
            self,
            title: str,
            options: list[str],
            *,
            allow_custom: bool = False,
            **_: Any,
    ) -> str:
        with self._lock:
            self.console.print()
            self.console.print(Text(title, style="bold"))
            for index, option in enumerate(options, 1):
                self.console.print(Text(f"  {index}. {option}"))
            if not options and not allow_custom:
                return "<cancelled>"
            if allow_custom:
                self.console.print(
                    Text(
                        "输入序号选择，或直接输入自定义内容；空输入提交空值，Ctrl+C/Ctrl+D 取消。",
                        style="dim",
                    )
                )
            else:
                self.console.print(Text("输入序号选择；Ctrl+C/Ctrl+D 取消。", style="dim"))

            while True:
                answer = self._read_input("选择")
                if answer is None:
                    return "<cancelled>"
                answer = answer.strip()
                if not answer:
                    return "<empty_input>" if allow_custom else "<cancelled>"
                if answer.isdigit():
                    index = int(answer)
                    if 1 <= index <= len(options):
                        return options[index - 1]
                if allow_custom:
                    return answer
                self.console.print(Text(f"无效选择：{answer}", style="yellow"))

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
            self.console.print(
                Text("  1. 批准委派  2. 主智能体执行  3. 取消", style="dim")
            )
            while True:
                answer = self._read_input("选择")
                if answer is None:
                    return "cancel"
                answer = answer.strip().lower()
                if answer in {"1", "approve", "y", "yes"}:
                    return "approve"
                if answer in {"2", "orchestrator", "o"}:
                    return "orchestrator"
                if answer in {"3", "cancel", "n", "no", ""}:
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
        self.notice("简单模式暂不支持添加模型面板；请使用 TUI 模式或直接编辑模型配置文件。")
        return None

    def choose_mcp_switch(
            self,
            server_switches: list[dict[str, Any]],
            mcp_manager: Any,
    ) -> str | dict:
        self.notice("简单模式暂不支持 MCP 管理面板；可使用 /mcp-view 查看当前状态。")
        return {"action": "cancel"}

    def manage_tasks(self, task_manager: Any) -> str:
        self.print_tasks(task_manager)
        return "closed"

    def show_copy_content(self, messages: list[dict[str, Any]]) -> str:
        self.notice("简单模式暂不支持复制面板；可直接选择终端文本进行复制。")
        return "<cancelled>"

    def manage_models(self, model_manager: Any) -> str:
        self.notice("简单模式暂不支持交互式模型管理面板；可使用 --models-list 查看当前配置。")
        return "<cancelled>"

    def manage_skills(self, skill_loader: Any) -> str | dict[str, Any]:
        self.notice("简单模式暂不支持 Skills 配置面板；/skills-list 会打印当前启用的技能。")
        return "<cancelled>"

    def manage_layout(self) -> str | dict[str, int]:
        self.notice("简单模式没有多面板布局，/layout 仅适用于 TUI 模式。")
        return "<cancelled>"

    def manage_memories(self, memory_provider: Any) -> list[str]:
        self.notice("简单模式暂不支持记忆管理面板；请使用 /memory-list 查看长期记忆。")
        return []

    def manage_memory_config(self, values: dict[str, Any]) -> str | dict[str, Any]:
        self.notice("简单模式暂不支持记忆配置面板。")
        return "<cancelled>"

    def choose_recall_model(self, options: list[str]) -> str:
        self.notice("简单模式暂不支持模型选择面板。")
        return "<cancelled>"

    def manage_extra_tools(self, values: dict[str, Any]) -> str | dict[str, Any]:
        self.notice("简单模式暂不支持额外工具配置面板。")
        return "<cancelled>"

    def choose_image_understanding_model(self, options: list[str]) -> str:
        self.notice("简单模式暂不支持图片理解模型选择面板。")
        return "<cancelled>"

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

    def _read_input(self, prompt: str) -> str | None:
        try:
            return input(f"{prompt}> ")
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return None

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
