"""
斜杠命令模块 - 负责处理所有内置命令和交互式界面
"""
import asyncio
import time
from asyncio import CancelledError
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Any

from prompt_toolkit import prompt
from prompt_toolkit.application import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from init import log_error_traceback
from system.console_render import toggle_sub_agent_console
from system.models import get_model_manager
from utils.tasks import list_task_plans, load_task_plan, get_task_plan_title
from utils.teams import list_team_histories, load_team_history, get_history_title
from utils.memory import get_checkpoint_title


class CommandAction(Enum):
    EXIT = auto()
    CONTINUE = auto()
    RUN_AGENT = auto()
    RESET_CHECKPOINT = auto()
    UPDATE_CHECKPOINT = auto()
    LOAD_HISTORY = auto()
    UPDATE_SYSTEM_PROMPT = auto()


@dataclass
class CommandResult:
    action: CommandAction
    payload: Any = None


# ============================================================================
# 命令描述定义
# ============================================================================

COMMAND_DESCRIPTIONS = {
    "/cmds": "列出所有的可用命令和功能描述",
    "/models": "管理模型配置：添加、删除、标记常用、选择当前模型",
    "/mcp-view": "查看当前已加载的 MCP 服务器和工具",
    "/mcp-restart": "重新启动 MCP 管理器并加载配置",
    "/mcp-switch": "交互式切换 MCP 服务启用/禁用状态，并支持确认或取消保存",
    "/load": "列出历史checkpoint并选择加载",
    "/skills-switch": "切换 skills 目录摘要注入状态 (开启/关闭)",
    "/skills-list": "列出当前工作区可用的 skills",
    "/compact": "压缩当前对话上下文",
    "/tools": "列出当前可用工具详细信息",
    "/tasks": "查看任务看板和当前执行进度",
    "/plan": "查看任务看板和当前执行进度",
    "/status": "汇报系统状态、已完成任务和下一步计划",
    "/sub-agent-console": "切换 Sub-Agent 的控制台输出状态，默认关闭",
    "/help": "显示使用帮助和自我介绍",
    "/workspace": "查看当前工作区目录结构",
    "/ls": "查看当前工作区目录结构",
    "/clear": "清空当前对话历史",
    "/reset": "清空当前对话历史",
    "/quit": "退出程序",
    "/exit": "退出程序",
}


# ============================================================================
# 命令补全器
# ============================================================================

class SlashCommandCompleter(Completer):
    """斜杠命令自动补全器"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd, desc in COMMAND_DESCRIPTIONS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)


# ============================================================================
# Checkpoint 选择器
# ============================================================================

def interactive_choose_checkpoint(
        checkpoints: list,
        title: str = "\n📌 Select a Checkpoint to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
) -> str:
    """交互式选择 checkpoint"""
    if not checkpoints:
        return "abort"

    options = []
    for cp in checkpoints:
        stem = cp.stem
        parts = stem.split("_")
        if stem.startswith("ckpt_"):
            uid = parts[-1] if len(parts) >= 4 else cp.name
        elif stem.startswith("task_plan_") or stem.startswith("task_history_"):
            uid = parts[-1]  # epic_id / session_id is always last
        else:
            uid = cp.name
        mtime = cp.stat().st_mtime
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        
        # Extract title based on file type
        if stem.startswith("ckpt_"):
            cp_title = get_checkpoint_title(cp)
        elif stem.startswith("task_plan_"):
            cp_title = get_task_plan_title(cp)
        elif stem.startswith("task_history_"):
            cp_title = get_history_title(cp)
        else:
            cp_title = None
        
        if cp_title:
            desc = f"{uid} - {cp_title} (最近一次对话时间：{date_str})"
        else:
            desc = f"{uid} (最近一次对话时间：{date_str})"
            
        options.append((str(cp), desc))

    options.append(("abort", "取消 (Cancel)"))

    selected_index = [0]
    kb = KeyBindings()

    @kb.add("up")
    def _go_up(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _go_down(event):
        selected_index[0] = min(len(options) - 1, selected_index[0] + 1)

    @kb.add("enter")
    def _confirm(event):
        event.app.exit(result=options[selected_index[0]][0])

    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result="abort")

    def get_formatted_text():
        result = [("class:title", title)]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f"👉 {text}\n"))
            else:
                result.append(("class:unselected", f"     {text}\n"))
        return result

    control = FormattedTextControl(get_formatted_text)
    window = Window(content=control, height=len(options) + 2)
    layout = Layout(window)

    style = Style(
        [
            ("title", "fg:ansicyan bold"),
            ("selected", "fg:ansigreen bold"),
            ("unselected", "fg:ansigray"),
        ]
    )

    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()


# ============================================================================
# MCP 服务开关面板
# ============================================================================

def interactive_switch_mcp_servers(server_switches: list) -> str | dict:
    """交互式切换 MCP 服务启用/禁用状态"""
    if not server_switches:
        return "empty"

    selected_index = [0]
    draft_states = {item["name"]: bool(item["disabled"]) for item in server_switches}
    kb = KeyBindings()

    @kb.add("up")
    def _go_up(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _go_down(event):
        selected_index[0] = min(len(server_switches) - 1, selected_index[0] + 1)

    @kb.add("tab")
    def _toggle(event):
        if selected_index[0] < len(server_switches):
            server_name = server_switches[selected_index[0]]["name"]
            draft_states[server_name] = not draft_states[server_name]

    @kb.add("enter")
    def _confirm(event):
        event.app.exit(
            result={
                "action": "confirm",
                "disabled_updates": dict(draft_states),
            }
        )

    @kb.add("q")
    @kb.add("Q")
    def _quit(event):
        event.app.exit(result={"action": "cancel"})

    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result={"action": "cancel"})

    def get_formatted_text():
        lines = [
            (
                "class:title",
                "\n🔀 MCP 服务开关面板\n"
                " ↑/↓ 选择 | Tab 切换启用/禁用 | Enter 确认应用 | Q 取消\n\n"
            )
        ]

        for i, item in enumerate(server_switches):
            name = item["name"]
            disabled = draft_states[name]
            enabled = not disabled
            loaded = item.get("loaded", False)
            marker = "👉" if i == selected_index[0] else "  "
            switch_box = "[√]" if enabled else "[×]"
            runtime_txt = "已加载" if loaded else "未加载"
            status_txt = "启用" if enabled else "禁用"
            style = "class:selected" if i == selected_index[0] else "class:unselected"
            lines.append(
                (
                    style,
                    f" {marker}  {switch_box}  {name}    当前草稿: {status_txt}    运行态: {runtime_txt}\n",
                )
            )

        return lines

    control = FormattedTextControl(get_formatted_text)
    window = Window(content=control, height=len(server_switches) + 5)
    layout = Layout(window)
    style = Style(
        [
            ("title", "fg:ansicyan bold"),
            ("selected", "fg:ansigreen bold"),
            ("unselected", "fg:ansigray"),
        ]
    )
    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()


# ============================================================================
# 命令处理器
# ============================================================================

class CommandHandler:
    """命令处理器 - 统一处理所有斜杠命令"""

    def __init__(
            self,
            console: Console,
            mcp_manager,
            skill_loader,
            get_system_prompt_fn,
            save_checkpoint_fn,
            load_checkpoint_fn,
            list_checkpoints_fn,
            auto_compact_fn,
    ):
        self.console = console
        self.mcp_manager = mcp_manager
        self.skill_loader = skill_loader
        self.get_system_prompt_fn = get_system_prompt_fn
        self.save_checkpoint = save_checkpoint_fn
        self.load_checkpoint = load_checkpoint_fn
        self.list_checkpoints = list_checkpoints_fn
        self.auto_compact = auto_compact_fn

    def handle_mcp_view(self) -> bool:
        """处理 /mcp-view 命令"""
        status = self.mcp_manager.get_status_info()
        config_servers = status.get("config_servers", [])
        enabled_config_servers = status.get("enabled_config_servers", [])
        disabled_servers = status.get("disabled_servers", [])
        loaded_servers = status.get("loaded_servers", [])

        summary_table = Table(
            title="[bold cyan]🔌 MCP 状态总览[/bold cyan]",
            box=box.ROUNDED,
            expand=True,
        )
        summary_table.add_column("项目", style="bold green", justify="left")
        summary_table.add_column("内容", style="white")
        summary_table.add_row(
            "配置文件", status.get("config_path", "Not configured")
        )
        summary_table.add_row(
            "后台状态",
            "运行中" if status.get("is_running") else "未运行",
        )
        summary_table.add_row(
            "配置中的服务",
            ", ".join(config_servers) if config_servers else "(无)",
        )
        summary_table.add_row(
            "配置中已启用",
            ", ".join(enabled_config_servers)
            if enabled_config_servers
            else "(无)",
        )
        summary_table.add_row(
            "配置中已禁用",
            ", ".join(disabled_servers) if disabled_servers else "(无)",
        )
        summary_table.add_row(
            "当前已加载服务",
            ", ".join(loaded_servers) if loaded_servers else "(无)",
        )
        summary_table.add_row(
            "当前已加载工具数", str(status.get("tool_count", 0))
        )
        self.console.print(summary_table)

        if not status.get("is_running"):
            self.console.print(
                "\n[bold yellow]⚠️ MCP 后台管理器当前未运行。若配置已准备好，可执行 /mcp-restart 或使用 /mcp-switch 保存启用状态后触发加载。[/bold yellow]"
            )
            return True

        if status.get("tool_count", 0) == 0:
            self.console.print(
                "\n[bold yellow]⚠️ 当前没有已加载的 MCP 工具。请检查配置中的启用状态、服务连通性，或尝试 /mcp-restart。[/bold yellow]"
            )
            return True

        table = Table(
            title=f"[bold cyan]🛠️ 已加载的 MCP 工具明细 (共 {status['tool_count']} 个)[/bold cyan]",
            box=box.ROUNDED,
            expand=True,
        )
        table.add_column(
            "服务节点 (Loaded Server)", style="bold magenta", justify="left"
        )
        table.add_column(
            "工具名称 (Tool Name)", style="bold green", justify="left"
        )
        table.add_column("描述 (Description)", style="white")

        for tool in status["tools"]:
            table.add_row(
                tool.get("provider", "Unknown"),
                tool["name"],
                tool["description"],
            )

        self.console.print(table)
        return True

    def handle_mcp_restart(self) -> bool:
        """处理 /mcp-restart 命令"""
        self.mcp_manager.restart()
        return True

    def handle_mcp_switch(self) -> bool:
        """处理 /mcp-switch 命令"""
        self.console.print(
            "\n[bold cyan]🔧 正在打开 MCP 开关面板...[/bold cyan]\n"
            "[dim]操作说明：用 ↑/↓ 选择服务，按 Space 切换状态，移动到底部后按 Enter 选择确认或取消。[/dim]"
        )
        try:
            server_switches = self.mcp_manager.list_server_switches()
        except FileNotFoundError as exc:
            self.console.print(f"\n[bold yellow]⚠️ {exc}[/bold yellow]")
            return True
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch list", exc)
            self.console.print(f"\n[bold red]❌ 读取 MCP 配置失败: {exc}[/bold red]")
            return True

        if not server_switches:
            self.console.print(
                "\n[bold yellow]⚠️ mcp_config.json 中没有可切换的 mcpServers。[/bold yellow]"
            )
            return True

        try:
            switch_result = interactive_switch_mcp_servers(server_switches)
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch interactive", exc)
            self.console.print(
                f"\n[bold red]❌ 打开 MCP 开关面板失败: {exc}[/bold red]"
            )
            return True

        if switch_result == "empty" or switch_result.get("action") == "cancel":
            self.console.print(
                "\n[bold yellow]↩️ 已取消本次 MCP 开关修改，配置文件未保存，运行中的服务状态保持不变。[/bold yellow]"
            )
            return True

        try:
            apply_result = self.mcp_manager.apply_switches(
                switch_result.get("disabled_updates", {})
            )
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch apply", exc)
            self.console.print(
                f"\n[bold red]❌ 应用 MCP 开关变更失败: {exc}[/bold red]"
            )
            return True

        if not apply_result.get("saved"):
            self.console.print(
                f"\n[bold yellow]ℹ️ {apply_result.get('message', '没有检测到变更。')}[/bold yellow]"
            )
            return True

        changed = apply_result.get("changed", [])
        enabled = apply_result.get("enabled", [])
        disabled = apply_result.get("disabled", [])
        failed = apply_result.get("failed", [])

        summary_lines = [
            "\n[bold green]✅ MCP 开关修改已保存到配置文件，并已尝试按变更增量启停服务。[/bold green]",
            f"[dim]配置文件: {self.mcp_manager.get_status_info().get('config_path')}[/dim]",
        ]
        if changed:
            summary_lines.append(
                f"[green]已变更服务:[/green] {', '.join(changed)}"
            )
        if enabled:
            summary_lines.append(
                f"[green]本次启用:[/green] {', '.join(enabled)}"
            )
        if disabled:
            summary_lines.append(
                f"[yellow]本次停用:[/yellow] {', '.join(disabled)}"
            )
        if failed:
            failure_text = "; ".join(
                f"{item['server']} ({item['action']} 失败: {item['error']})"
                for item in failed
            )
            summary_lines.append(
                f"[bold red]部分服务切换失败:[/bold red] {failure_text}"
            )
        self.console.print("\n".join(summary_lines))
        return True

    def handle_cmds(self) -> bool:
        """处理 /cmds 命令"""
        table = Table(
            title="[bold cyan]🛠️ 可用内置命令列表[/bold cyan]",
            box=box.ROUNDED,
            expand=True,
        )
        table.add_column("命令 (Command)", style="bold green", justify="left")
        table.add_column("描述 (Description)", style="white")
        for cmd, desc in COMMAND_DESCRIPTIONS.items():
            table.add_row(cmd, desc)
        self.console.print(table)
        return True

    def handle_skills_switch(self) -> str:
        """处理 /skills-switch 命令，返回新的 system prompt"""
        status_text = self.skill_loader.toggle()
        new_system = self.get_system_prompt_fn()
        status_style = "green" if self.skill_loader.is_enabled else "yellow"
        self.console.print(
            f"\n[bold {status_style}]✨ Skills prompt catalog 状态已切换：{status_text}。[/bold {status_style}]"
        )
        self.console.print(
            Panel(
                Text(
                    self.skill_loader.render_prompt_block().strip(),
                    style="white",
                ),
                title="[bold cyan]Skills Catalog Status[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )
        return new_system

    def handle_skills_list(self) -> bool:
        """处理 /skills-list 命令"""
        skills_list_text = self.skill_loader.get_descriptions()
        self.console.print(
            Panel(
                Markdown(f"### 当前可用技能列表\n\n{skills_list_text}"),
                title="[bold cyan]📚 Skills List[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )
        return True

    def handle_models(self) -> bool:
        """处理 /models 命令"""
        model_manager = get_model_manager()
        if model_manager is None:
            self.console.print("\n[bold red]❌ 模型管理器未初始化。[/bold red]")
            return True

        selected_index = [0]
        message = ["↑/↓ 选择模型   A 添加   D 删除   F 常用切换   S 设为当前   Enter 选中并退出   Q 退出"]
        kb = KeyBindings()

        def refresh_models():
            model_manager._reload_from_disk()

        def clamp_selection():
            refresh_models()
            if not model_manager.models:
                selected_index[0] = 0
            else:
                selected_index[0] = max(0, min(selected_index[0], len(model_manager.models) - 1))

        async def add_model_flow():
            def sync_input_flow():
                self.console.print("\n[bold cyan]➕ 添加模型[/bold cyan]")
                base_url = input("Base URL: ").strip()
                api_key = input("API Key: ").strip()
                model_input = input("Model ID(s)（多个用逗号分隔）: ").strip()
                return {
                    "status": "ok",
                    "base_url": base_url,
                    "api_key": api_key,
                    "model_input": model_input,
                }

            try:
                result = await run_in_terminal(sync_input_flow)
            except (EOFError, KeyboardInterrupt, CancelledError):
                result = {"status": "cancel"}
            if result.get("status") == "cancel":
                message[0] = "⏭️ 已取消添加模型。"
                app.invalidate()
                return

            base_url = result.get("base_url", "")
            api_key = result.get("api_key", "")
            model_input = result.get("model_input", "")
            model_ids = [item.strip() for item in model_input.replace("，", ",").split(",") if item.strip()]

            if not base_url or not api_key or not model_ids:
                message[0] = "❌ base_url、api_key、model_id 不能为空。"
                app.invalidate()
                return

            added_models = model_manager.add_model(base_url, api_key, model_ids)
            clamp_selection()
            if added_models:
                selected_index[0] = len(model_manager.models) - len(added_models)
                message[0] = f"✅ 已添加 {len(added_models)} 个模型。"
            else:
                message[0] = "⚠️ 未添加任何新模型，可能都已存在。"
            app.invalidate()

        async def confirm_delete_flow(delete_index: int, display_text: str):
            def sync_confirm_flow():
                self.console.print(f"\n[bold yellow]⚠️ 确认删除模型[/bold yellow]\n{display_text}")
                confirm = input("确认删除？输入 y 确认，其它任意键取消: ").strip().lower()
                return {"confirmed": confirm in {"y", "yes"}}

            try:
                result = await run_in_terminal(sync_confirm_flow)
            except (EOFError, KeyboardInterrupt, CancelledError):
                result = {"confirmed": False}

            if not result.get("confirmed"):
                message[0] = f"⏭️ 已取消删除: {display_text}"
                app.invalidate()
                return

            if model_manager.delete_model_by_index(delete_index):
                clamp_selection()
                message[0] = f"✅ 已删除模型: {display_text}"
            else:
                message[0] = f"❌ 删除失败: {display_text}"
            app.invalidate()

        def get_selected_model():
            clamp_selection()
            if not model_manager.models:
                message[0] = "⚠️ 当前没有可操作的模型。"
                return None
            return model_manager.models[selected_index[0]]

        @kb.add("up")
        def _go_up(event):
            refresh_models()
            if model_manager.models:
                selected_index[0] = max(0, selected_index[0] - 1)
                event.app.invalidate()

        @kb.add("down")
        def _go_down(event):
            refresh_models()
            if model_manager.models:
                selected_index[0] = min(len(model_manager.models) - 1, selected_index[0] + 1)
                event.app.invalidate()

        @kb.add("a")
        @kb.add("A")
        def _add(event):
            event.app.create_background_task(add_model_flow())
            event.app.invalidate()

        @kb.add("d")
        @kb.add("D")
        def _delete(event):
            target_model = get_selected_model()
            if target_model is None:
                event.app.invalidate()
                return
            display_text = target_model.get_display_text()
            delete_index = selected_index[0]
            event.app.create_background_task(confirm_delete_flow(delete_index, display_text))
            event.app.invalidate()

        @kb.add("f")
        @kb.add("F")
        def _favorite(event):
            target_model = get_selected_model()
            if target_model is None:
                event.app.invalidate()
                return
            before = target_model.is_favorite
            display_text = target_model.get_display_text()
            if model_manager.toggle_favorite_by_index(selected_index[0]):
                state = "设为常用" if not before else "取消常用"
                message[0] = f"✅ 已{state}: {display_text}"
            else:
                message[0] = f"❌ 常用状态切换失败: {display_text}"
            event.app.invalidate()

        @kb.add("s")
        @kb.add("S")
        def _select(event):
            target_model = get_selected_model()
            if target_model is None:
                event.app.invalidate()
                return
            display_text = target_model.get_display_text()
            if model_manager.set_current_model_by_index(selected_index[0]):
                message[0] = f"✅ 当前模型已切换为: {display_text}"
            else:
                message[0] = f"❌ 模型切换失败: {display_text}"
            event.app.invalidate()

        @kb.add("enter")
        def _select_and_exit(event):
            target_model = get_selected_model()
            if target_model is None:
                event.app.exit(result=True)
                return
            display_text = target_model.get_display_text()
            model_manager.set_current_model_by_index(selected_index[0])
            message[0] = f"✅ 当前模型已切换为: {display_text}"
            event.app.invalidate()
            event.app.exit(result=True)

        @kb.add("q")
        @kb.add("Q")
        @kb.add("c-c")
        def _quit(event):
            event.app.exit(result=True)

        def get_formatted_text():
            clamp_selection()
            current_model = model_manager.get_current_model()
            result = [
                ("class:title", "⚙️ 模型管理面板\n"),
                ("class:hint", "↑/↓ 选择模型   A 添加   D 删除   F 常用切换   S 设为当前   Enter 选中并退出   Q 退出\n\n"),
            ]
            if not model_manager.models:
                result.append(("class:empty", "  暂无模型。按 A 添加模型，按 Q 退出。\n"))
            else:
                for index, model in enumerate(model_manager.models):
                    selected = index == selected_index[0]
                    markers = []
                    if current_model is model:
                        markers.append("✓")
                    if model.is_favorite:
                        markers.append("♥")
                    marker_text = " ".join(markers) if markers else " "
                    line = f"  {index + 1:>2}. [{marker_text:^3}] {model.get_display_text()}\n"
                    result.append(("class:selected" if selected else "class:unselected", line))
            result.append(("class:message", f"\n{message[0]}\n"))
            return result

        app = Application(
            layout=Layout(Window(content=FormattedTextControl(get_formatted_text))),
            key_bindings=kb,
            style=Style.from_dict(
                {
                    "title": "bold #00ffff",
                    "hint": "#aaaaaa",
                    "selected": "bold #00ffff",
                    "unselected": "#ffffff",
                    "empty": "#ffff00",
                    "message": "#ffff00",
                }
            ),
            full_screen=False,
        )
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        current_model = model_manager.get_current_model()
        current_text = current_model.get_display_text() if current_model else "未选择"
        self.console.print(f"\n[bold cyan]已退出模型面板，当前模型：[/bold cyan][bold green]{current_text}[/bold green]")
        return True

    def handle_clear_reset(self, history: list, current_checkpoint: Optional[Path]) -> tuple:
        """处理 /clear 和 /reset 命令，返回 (should_continue, new_checkpoint)"""
        from utils.hitl import SESSION_WHITELIST
        SESSION_WHITELIST.clear()

        history.clear()
        history.append({"role": "system", "content": self.get_system_prompt_fn()})
        self.console.print(
            "\n[bold green]✨ 对话历史已清空，开启全新会话！[/bold green]"
        )
        return True, None

    def handle_compact(self, history: list, current_checkpoint: Optional[Path]) -> tuple:
        """处理 /compact 命令，返回 (should_continue, new_checkpoint)"""
        self.auto_compact(history, reason="User triggered compact")
        self.console.print(
            "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
        )
        new_checkpoint = self.save_checkpoint(history, current_checkpoint)
        return True, new_checkpoint

    def handle_load(
            self,
            history: list,
            current_checkpoint: Optional[Path],
            render_banner_fn,
            render_hint_fn,
            render_history_fn,
    ) -> tuple:
        """处理 /load 命令，返回 (new_history, new_checkpoint)"""
        checkpoints = self.list_checkpoints()
        if not checkpoints:
            self.console.print(
                "\n[bold yellow]📂 没有找到任何历史对话记录 (No checkpoints found).[/bold yellow]"
            )
            return history, current_checkpoint

        if len(history) > 1 and current_checkpoint is None:
            current_checkpoint = self.save_checkpoint(history)

        try:
            selected_path = interactive_choose_checkpoint(checkpoints)
        except Exception as exc:
            log_error_traceback("commands handle_load checkpoint", exc)
            selected_path = "abort"

        if selected_path == "abort":
            self.console.print("[dim]已取消加载。[/dim]")
            return history, current_checkpoint

        try:
            loaded = self.load_checkpoint(Path(selected_path))
            if loaded and loaded[0].get("role") == "system":
                loaded[0]["content"] = self.get_system_prompt_fn()
            new_checkpoint = Path(selected_path)

            self.console.clear()
            render_banner_fn()
            render_hint_fn()
            render_history_fn(loaded)

            self.console.print(
                f"\n[bold green]🚀 成功加载对话记录！当前上下文包含 {len(loaded)} 条消息。[/bold green]"
            )
        except Exception as exc:
            log_error_traceback("commands handle_load error", exc)
            self.console.print(f"\n[bold red]❌ 加载失败: {exc}[/bold red]")
            return history, current_checkpoint

        # 检查任务看板
        task_plans = list_task_plans()
        if task_plans:
            self.console.print(
                "\n[bold cyan]📋 发现保存的任务看板 (Task Plans)，是否要加载？[/bold cyan]"
            )

            try:
                selected_task_path = interactive_choose_checkpoint(
                    task_plans,
                    title="\n📌 Select a Task Plan to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
                )
            except Exception as exc:
                log_error_traceback("commands handle_load task plan", exc)
                selected_task_path = "abort"

            if selected_task_path != "abort":
                try:
                    plan_data = load_task_plan(Path(selected_task_path))
                    self.console.print(
                        "[bold green]🚀 成功加载任务看板！[/bold green]"
                    )

                    has_incomplete = any(
                        task.get("status") != "completed"
                        for task in plan_data.get("tasks", {}).values()
                    )

                    if has_incomplete:
                        team_histories = list_team_histories()
                        if team_histories:
                            self.console.print(
                                "\n[bold cyan]💡 发现子代理执行历史 (Team Histories)，是否要加载？[/bold cyan]"
                            )

                            try:
                                selected_team_path = interactive_choose_checkpoint(
                                    team_histories,
                                    title="\n📌 Select a Team History to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
                                )
                            except Exception as exc:
                                log_error_traceback(
                                    "commands handle_load team history", exc
                                )
                                selected_team_path = "abort"

                            if selected_team_path != "abort":
                                try:
                                    load_team_history(Path(selected_team_path))
                                    self.console.print(
                                        "[bold green]✅ 成功加载子代理执行历史！[/bold green]"
                                    )
                                except Exception as exc:
                                    log_error_traceback(
                                        "commands handle_load team history error", exc
                                    )
                                    self.console.print(
                                        f"[bold red]❌ 加载子代理执行历史失败: {exc}[/bold red]"
                                    )
                except Exception as exc:
                    log_error_traceback("commands handle_load task plan error", exc)
                    self.console.print(
                        f"\n[bold red]❌ 加载任务看板失败: {exc}[/bold red]"
                    )
        return loaded, new_checkpoint

    def process_command(
            self,
            query: str,
            history: list,
            current_checkpoint: Optional[Path],
            render_banner_fn,
            render_hint_fn,
            render_history_fn,
    ) -> CommandResult:
        """
        处理命令入口，返回结构化的 CommandResult
        """
        # /quit, /exit - 退出程序
        if query in ["/quit", "/exit"]:
            self.console.print(
                "\n[bold yellow]👋 正在退出 MakeCode CLI。再见！[/bold yellow]"
            )
            return CommandResult(action=CommandAction.EXIT)

        # MCP 相关命令
        if query == "/mcp-view":
            self.handle_mcp_view()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-restart":
            self.handle_mcp_restart()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-switch":
            self.handle_mcp_switch()
            return CommandResult(action=CommandAction.CONTINUE)

        # /cmds - 列出命令
        if query == "/cmds":
            self.handle_cmds()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/models":
            self.handle_models()
            return CommandResult(action=CommandAction.CONTINUE)

        # /sub-agent-console - 切换 Sub-Agent 的控制台输出状态
        if query == "/sub-agent-console":
            new_state = toggle_sub_agent_console()
            status = "开启" if new_state else "关闭"
            status_color = "green" if new_state else "yellow"
            self.console.print(f"\n[bold]📊 Sub-Agent 输出状态: [{status_color}]{status}[/{status_color}][/bold]")
            return CommandResult(action=CommandAction.CONTINUE)

        # /skills 相关命令
        if query == "/skills-switch":
            new_system = self.handle_skills_switch()
            return CommandResult(action=CommandAction.UPDATE_SYSTEM_PROMPT, payload=new_system)

        if query == "/skills-list":
            self.handle_skills_list()
            return CommandResult(action=CommandAction.CONTINUE)

        # /clear, /reset - 清空历史
        if query in ["/clear", "/reset"]:
            self.handle_clear_reset(history, current_checkpoint)
            return CommandResult(action=CommandAction.RESET_CHECKPOINT)

        # /compact - 压缩上下文
        if query == "/compact":
            _, new_checkpoint = self.handle_compact(history, current_checkpoint)
            return CommandResult(action=CommandAction.UPDATE_CHECKPOINT, payload=new_checkpoint)

        # /load - 加载历史
        if query == "/load":
            new_history, new_checkpoint = self.handle_load(
                history,
                current_checkpoint,
                render_banner_fn,
                render_hint_fn,
                render_history_fn,
            )
            return CommandResult(action=CommandAction.LOAD_HISTORY, payload=(new_history, new_checkpoint))

        # 其他命令 - 让 LLM 处理
        # 对于在 COMMAND_DESCRIPTIONS 中的命令，附加描述（与原始逻辑一致）
        if query in COMMAND_DESCRIPTIONS:
            return CommandResult(action=CommandAction.RUN_AGENT, payload=f"{query} {COMMAND_DESCRIPTIONS[query]}")
        return CommandResult(action=CommandAction.RUN_AGENT, payload=query)
