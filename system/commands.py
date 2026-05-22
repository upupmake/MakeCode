"""
斜杠命令模块 - 负责处理所有内置命令和交互式界面
"""
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Any

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from init import log_error_traceback
from system.console_render import render_current_task_plan, render_current_workdir, toggle_sub_agent_console
from system.models import get_model_manager
from system.tui_app import choose_model_panel_tui, choose_tui, post_tui, TuiRegion, choose_add_model_tui, choose_mcp_switch_tui, manage_models_tui, manage_layout_tui, manage_memories_tui, manage_memory_config_tui, show_info_panel_tui, set_agent_loop_active, refresh_status, refresh_tools_title, begin_tui_batch_render, end_tui_batch_render
from utils import hitl as hitl_mod, paths
from utils.plan_mode import toggle_plan_mode
from utils.tasks import list_task_plans, load_task_plan, get_task_plan_title
from utils.teams import list_team_histories, load_team_history, get_history_title
from utils.memory import (
    delete_long_term_memory,
    get_active_memory_count,
    get_checkpoint_title,
    get_keep_recent_tool_call,
    get_memory_size,
    list_long_term_memories,
    manual_memory_update,
    set_keep_recent_tool_call,
    set_memory_size,
)
from system.updater import check_update, download_update


class CommandAction(Enum):
    EXIT = auto()
    CONTINUE = auto()
    RUN_AGENT = auto()
    RESET_CHECKPOINT = auto()
    UPDATE_CHECKPOINT = auto()
    LOAD_HISTORY = auto()
    UPDATE_SYSTEM_PROMPT = auto()
    LAUNCH_UPDATER_AND_EXIT = auto()


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
    "/layout": "调整 TUI 面板高度比例：左侧 Content/Tools，右侧 Reasoning/Background/Sub-Agent",
    "/mcp-view": "查看当前已加载的 MCP 服务器和工具",
    "/mcp-restart": "重新启动 MCP 管理器并加载配置",
    "/mcp-switch": "交互式切换 MCP 服务启用/禁用状态，并支持确认或取消保存",
    "/load": "列出历史checkpoint并选择加载",
    "/skills-switch": "切换 skills 目录摘要注入状态 (开启/关闭)",
    "/skills-list": "列出当前工作区可用的 skills",
    "/compact": "压缩当前对话上下文（自动尝试提取关键记忆信息）",
    "/memory-list": "列出当前保存的长期记忆",
    "/memory-panel": "打开长期记忆交互面板，可查看详情并二次确认删除",
    "/memory-delete": "按 ID 删除一条长期记忆，例如 /memory-delete mem_20260510_abc123",
    "/memory-config": "打开记忆配置面板，修改 memory_size 和 keep_recent_tool_call",
    "/memory-update": "根据用户请求主动管理长期记忆，例如 /memory-update 记住：以后...",
    "/tasks": "查看任务看板和当前执行进度",
    "/plan": "进入/退出 Plan Mode — 规划阶段只允许只读和任务规划工具",
    "/sub-agent-console": "切换 Sub-Agent 的控制台输出状态，默认开启",
    "/help": "显示使用帮助和自我介绍",
    "/new": "清空当前对话历史",
    "/pwd": "显示当前工作目录",
    "/cd": "切换当前工作目录，例如 /cd D:\\Projects\\Demo",
    "/hitl": "切换 Human-in-the-Loop 拦截状态 (开启/关闭)",
    "/quit": "退出程序",
    "/exit": "退出程序",
    "/update": "检查并安装最新版本更新",
}


# ============================================================================
# Checkpoint 选择器
# ============================================================================

def interactive_choose_checkpoint(
        checkpoints: list,
        title: str = "\n📌 Select a Checkpoint to Load (Use ⬆ / ⬇ arrows, Enter to confirm, Q to cancel):\n",
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
            desc = f"{uid} - {cp_title} (最近一次更新时间：{date_str})"
        else:
            desc = f"{uid} (最近一次更新时间：{date_str})"

        options.append((str(cp), desc))

    choices = []
    lookup = {}
    for path_value, desc in options:
        choices.append(desc)
        lookup[desc] = path_value

    selected = choose_tui(title.strip(), choices)
    return lookup.get(selected, "abort")


# ============================================================================
# MCP 服务开关面板
# ============================================================================

def interactive_switch_mcp_servers(server_switches: list) -> str | dict:
    """交互式切换 MCP 服务启用/禁用状态"""
    if not server_switches:
        return "empty"
    return choose_mcp_switch_tui(server_switches)


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
            apply_workdir_fn=None,
    ):
        self.console = console
        self.mcp_manager = mcp_manager
        self.skill_loader = skill_loader
        self.get_system_prompt_fn = get_system_prompt_fn
        self.save_checkpoint = save_checkpoint_fn
        self.load_checkpoint = load_checkpoint_fn
        self.list_checkpoints = list_checkpoints_fn
        self.auto_compact = auto_compact_fn
        self.apply_workdir = apply_workdir_fn

    def handle_mcp_view(self) -> bool:
        """处理 /mcp-view 命令"""
        status = self.mcp_manager.get_status_info()
        config_servers = status.get("config_servers", [])
        enabled_config_servers = status.get("enabled_config_servers", [])
        disabled_servers = status.get("disabled_servers", [])
        loaded_servers = status.get("loaded_servers", [])

        # 统计每个已加载服务的工具数量
        tool_count_by_server = {}
        for tool in status.get("tools", []):
            provider = tool.get("provider", "Unknown")
            tool_count_by_server[provider] = tool_count_by_server.get(provider, 0) + 1

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
        if loaded_servers:
            loaded_display = ", ".join(
                f"{name} ({tool_count_by_server.get(name, 0)})"
                for name in loaded_servers
            )
        else:
            loaded_display = "(无)"
        summary_table.add_row("当前已加载服务", loaded_display)
        table = Table(
            title=f"[bold cyan]🛠️ 已加载的 MCP 工具明细 (共 {status['tool_count']} 个)[/bold cyan]",
            box=box.ROUNDED,
            expand=True,
        )
        table.add_column(
            "服务节点", style="bold magenta", justify="left", no_wrap=True
        )
        table.add_column(
            "工具名称", style="bold green", justify="left", overflow="fold"
        )
        table.add_column("描述", style="white", overflow="fold")

        for tool in status["tools"]:
            table.add_row(
                tool.get("provider", "Unknown"),
                tool["name"],
                tool["description"],
            )

        notices = []
        if not status.get("is_running"):
            notices.append(
                f"[bold yellow]⚠️ MCP 后台管理器未运行。\n配置路径: {status.get('config_path', '未配置')}[/bold yellow]"
            )

        if status.get("tool_count", 0) == 0:
            notices.append(
                f"[bold yellow]⚠️ MCP 服务为空，暂无可用工具。\n配置路径: {status.get('config_path', '未配置')}[/bold yellow]"
            )

        panel_items = [summary_table, table]
        if notices:
            panel_items.append(Text.from_markup("\n\n".join(notices)))
        content = Group(*panel_items)
        if show_info_panel_tui("🔌 MCP 状态与工具", content) == "<cancelled>":
            self.console.print(content, tui_region=TuiRegion.TOOLS)
        return True

    def handle_mcp_restart(self) -> bool:
        """处理 /mcp-restart 命令"""
        self.mcp_manager.restart()
        return True

    def handle_mcp_switch(self) -> bool:
        """处理 /mcp-switch 命令"""
        self.console.print(
            "\n[bold cyan]🔧 正在打开 MCP 开关面板...[/bold cyan]\n"
            "[#aaaaaa]操作说明：用 ↑/↓ 选择服务，按 Space 切换状态，移动到底部后按 Enter 选择确认或取消。[/#aaaaaa]",
            tui_region=TuiRegion.TOOLS,
        )
        try:
            server_switches = self.mcp_manager.list_server_switches()
        except FileNotFoundError as exc:
            self.console.print(f"\n[bold yellow]⚠️ {exc}[/bold yellow]", tui_region=TuiRegion.TOOLS)
            return True
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch list", exc)
            self.console.print(f"\n[bold red]❌ 读取 MCP 配置失败: {exc}[/bold red]", tui_region=TuiRegion.TOOLS)
            return True

        if not server_switches:
            self.console.print(
                f"\n[bold yellow]⚠️ MCP 服务为空，暂无可切换的服务。\n   配置路径: {self.mcp_manager.config_path}[/bold yellow]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        try:
            switch_result = interactive_switch_mcp_servers(server_switches)
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch interactive", exc)
            self.console.print(
                f"\n[bold red]❌ 打开 MCP 开关面板失败: {exc}[/bold red]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        if switch_result == "empty" or switch_result.get("action") == "cancel":
            self.console.print(
                "\n[bold yellow]↩️ 已取消本次 MCP 开关修改，配置文件未保存，运行中的服务状态保持不变。[/bold yellow]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        try:
            apply_result = self.mcp_manager.apply_switches(
                switch_result.get("disabled_updates", {})
            )
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch apply", exc)
            self.console.print(
                f"\n[bold red]❌ 应用 MCP 开关变更失败: {exc}[/bold red]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        if not apply_result.get("saved"):
            self.console.print(
                f"\n[bold yellow]ℹ️ {apply_result.get('message', '没有检测到变更。')}[/bold yellow]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        changed = apply_result.get("changed", [])
        enabled = apply_result.get("enabled", [])
        disabled = apply_result.get("disabled", [])
        failed = apply_result.get("failed", [])

        summary_lines = [
            "\n[bold green]✅ MCP 开关修改已保存到配置文件，并已尝试按变更增量启停服务。[/bold green]",
            f"[#aaaaaa]配置文件: {self.mcp_manager.get_status_info().get('config_path')}[/#aaaaaa]",
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
        self.console.print("\n".join(summary_lines), tui_region=TuiRegion.TOOLS)
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
        if show_info_panel_tui("🛠️ 可用内置命令列表", table) == "<cancelled>":
            self.console.print(table)
        return True

    def handle_task_table(self) -> bool:
        """处理 /tasks 命令"""
        from utils.tasks import TASK_MANAGER

        task_table = TASK_MANAGER.get_task_table()
        rows = task_table.get("rows", [])
        if not rows:
            content = Text("当前任务计划为空。", style="bold yellow")
            if show_info_panel_tui("当前任务计划", content) == "<cancelled>":
                self.console.print(content)
            return True

        tbl = Table(title="当前任务计划", show_lines=False, box=box.ROUNDED, expand=True)
        tbl.add_column("ID", style="cyan", width=4)
        tbl.add_column("Subject", style="white")
        tbl.add_column("Status", style="green")
        tbl.add_column("Runnable", style="yellow", width=8)
        for row in rows:
            tbl.add_row(
                str(row["id"]),
                row["subject"],
                row["status"],
                "✓" if row.get("is_runnable") else "",
            )
        if show_info_panel_tui("当前任务计划", tbl) == "<cancelled>":
            self.console.print(tbl)
        return True

    def handle_update(self) -> Path | None:
        """处理 /update 命令 - 检查并安装更新"""
        import sys
        from version import CURRENT_VERSION

        self.console.print(f"\n[bold cyan]📋 当前版本: v{CURRENT_VERSION}[/bold cyan]")

        if not getattr(sys, 'frozen', False):
            self.console.print("[bold yellow]⚠️ 开发环境下不支持自动更新，请使用 pyinstaller 打包后再试。[/bold yellow]")
            return None

        set_agent_loop_active(True)
        try:
            self.console.print("[bold cyan]🔍 正在检查更新...[/bold cyan]")

            try:
                version_info = check_update(raise_errors=True)
            except Exception as exc:
                self.console.print(f"[bold red]❌ 检查更新失败: {exc}[/bold red]")
                return None

            if version_info is None:
                self.console.print("[bold green]✅ 当前已是最新版本！[/bold green]")
                return None

            new_version = version_info.get('version', '未知')
            release_log = version_info.get('release_log', '')

            self.console.print(f"\n[bold yellow]📢 发现新版本: v{new_version}[/bold yellow]")
            if release_log:
                self.console.print("[#aaaaaa]更新内容:[/#aaaaaa]")
                self.console.print(Markdown(release_log))

            answer = choose_tui("是否下载并安装更新？", ["是", "否"])

            if answer != '是':
                self.console.print("[#aaaaaa]已取消更新[/#aaaaaa]")
                return None

            self.console.print("[bold cyan]📥 正在下载更新...[/bold cyan]")

            # 进度显示
            progress_state = {"pct": -1, "mb": -1}

            def _progress(downloaded: int, total: int | None) -> None:
                downloaded_mb = downloaded // 1024 // 1024
                if total:
                    pct_int = int(downloaded / total * 100)
                    if pct_int == progress_state["pct"] and downloaded < total:
                        return
                    progress_state["pct"] = pct_int
                    pct = downloaded / total * 100
                    bar_len = 30
                    filled = int(bar_len * downloaded / total)
                    bar = "█" * filled + "░" * (bar_len - filled)
                    progress_text = f"  {bar} {pct:.1f}%  ({downloaded_mb}MB / {total // 1024 // 1024}MB)"
                else:
                    if downloaded_mb == progress_state["mb"]:
                        return
                    progress_state["mb"] = downloaded_mb
                    progress_text = f"  已下载: {downloaded_mb} MB"
                self.console.print(progress_text)

            try:
                new_exe_path = download_update(version_info, progress_callback=_progress)
            except Exception as exc:
                self.console.print(f"\n[bold red]❌ 下载失败: {exc}[/bold red]")
                return None

            if new_exe_path is None:
                self.console.print("\n[bold red]❌ 下载失败，请稍后重试[/bold red]")
                return None

            self.console.print("[bold green]✅ 下载完成！正在退出主程序并启动更新程序...[/bold green]")
            self.console.print("[#aaaaaa]程序将自动退出并完成更新，更新后请手动重启程序[/#aaaaaa]")
            return new_exe_path
        finally:
            set_agent_loop_active(False)

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
        content = Markdown(f"### 当前可用技能列表\n\n{skills_list_text}")
        if show_info_panel_tui("📚 Skills List", content) == "<cancelled>":
            self.console.print(content)
        return True

    def handle_models(self) -> bool:
        """处理 /models 命令"""
        model_manager = get_model_manager()
        if model_manager is None:
            self.console.print("\n[bold red]❌ 模型管理器未初始化。[/bold red]", tui_region=TuiRegion.TOOLS)
            return True

        result = manage_models_tui(model_manager)
        if result.startswith("selected:"):
            selected_text = result.removeprefix("selected:")
            self.console.print(f"\n[bold green]✅ 当前模型已切换为: {selected_text}[/bold green]", tui_region=TuiRegion.TOOLS)

        current_model = model_manager.get_current_model()
        current_text = current_model.get_display_text() if current_model else "未选择"
        self.console.print(f"\n[bold cyan]已退出模型面板，当前模型：[/bold cyan][bold green]{current_text}[/bold green]", tui_region=TuiRegion.TOOLS)
        return True

    def handle_layout(self) -> bool:
        """处理 /layout 命令"""
        result = manage_layout_tui()
        if isinstance(result, dict):
            self.console.print(
                "\n[bold green]✅ Layout 已应用：[/bold green]"
                f"左侧 Content/Tools = {result['content']}/{result['tools']}；"
                f"右侧 Reasoning/Background/Sub-Agent = {result['reasoning']}/{result['background']}/{result['sub_agent']}",
                tui_region=TuiRegion.TOOLS,
            )
        return True

    def handle_new(self, history: list, current_checkpoint: Optional[Path]) -> tuple:
        """处理 /new 命令，返回 (should_continue, new_checkpoint)"""
        self._reset_hitl_session()
        self._reset_conversation_view(history)
        self.console.print(
            "\n[bold green]✨ 对话历史已清空，开启全新会话！[/bold green]"
        )
        render_current_workdir()
        refresh_status()
        return True, None

    def handle_cd(self, query: str, history: list) -> bool:
        """处理 /cd 命令，切换当前工作目录。"""
        if self.apply_workdir is None:
            self.console.print("[bold red]⚠️ 当前环境不支持切换工作目录。[/bold red]")
            return False

        raw_path = query.removeprefix("/cd").strip()
        if not raw_path:
            self.console.print("[bold yellow]用法：/cd <目录路径>[/bold yellow]")
            render_current_workdir()
            return False
        if (raw_path.startswith('"') and raw_path.endswith('"')) or (raw_path.startswith("'") and raw_path.endswith("'")):
            raw_path = raw_path[1:-1].strip()

        current_workdir = Path(paths.workdir()).resolve()
        target_path = Path(raw_path).expanduser()
        if not target_path.is_absolute():
            target_path = current_workdir / target_path
        target_path = target_path.resolve()

        if not target_path.exists() or not target_path.is_dir():
            self.console.print(f"[bold red]⚠️ 目录不存在或不是有效目录：{target_path}[/bold red]")
            render_current_workdir()
            return False
        if target_path == current_workdir:
            self.console.print("[#aaaaaa]📂 目标目录与当前工作目录相同，未切换。[/#aaaaaa]")
            render_current_workdir()
            return False

        self.apply_workdir(target_path)
        self._reset_hitl_session()
        self._reset_conversation_view(history)
        refresh_status()
        refresh_tools_title()
        self.console.print("[bold green]✅ 工作目录已切换，已开启全新会话。[/bold green]")
        render_current_workdir()
        return True

    def _reset_hitl_session(self) -> None:
        if not hitl_mod.get_hitl_status():
            hitl_mod.toggle_hitl(enabled=True)
            self.console.print("[#aaaaaa]🛡️ Human-in-the-Loop 已恢复为开启状态[/#aaaaaa]")
        else:
            hitl_mod.SESSION_WHITELIST.clear()
            hitl_mod.PATH_WHITELIST.clear()

    def _reset_conversation_view(self, history: list) -> None:
        history.clear()
        history.append({"role": "system", "content": self.get_system_prompt_fn()})
        for region in (
            TuiRegion.CONTENT,
            TuiRegion.TOOLS,
            TuiRegion.REASONING,
            TuiRegion.BACKGROUND,
            TuiRegion.SUB_AGENT,
        ):
            post_tui(region, "", clear=True)

    def handle_compact(self, history: list, current_checkpoint: Optional[Path]) -> tuple:
        """处理 /compact 命令，返回 (should_continue, new_checkpoint)"""
        set_agent_loop_active(True)
        try:
            self.auto_compact(
                history,
                reason="User triggered compact",
                system_prompt_fn=self.get_system_prompt_fn,
            )
            self.console.print(
                "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
            new_checkpoint = self.save_checkpoint(history, current_checkpoint)
            refresh_status()
            return True, new_checkpoint
        finally:
            set_agent_loop_active(False)

    def handle_memory_list(self) -> bool:
        """处理 /memory-list 命令"""
        memories = list_long_term_memories()
        active_count = len(memories)
        if not memories:
            self.console.print("\n[bold yellow]暂无长期记忆。[/bold yellow] [#aaaaaa](active: 0)[/#aaaaaa]")
            return True

        table = Table(
            title=f"[bold cyan]长期记忆[/bold cyan] [#aaaaaa](active: {active_count})[/#aaaaaa]",
            box=box.ROUNDED,
            expand=True,
            show_lines=True,
        )
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Category", style="green", overflow="fold")
        table.add_column("Updated At", style="magenta", overflow="fold")
        table.add_column("Insight", style="white", overflow="fold")
        table.add_column("Reuse Condition", style="white", overflow="fold")
        for item in memories:
            table.add_row(
                item.get("id", ""),
                item.get("category", ""),
                item.get("updated_at", ""),
                item.get("insight", ""),
                item.get("reuse_condition", ""),
            )
        self.console.print(table)
        return True

    def handle_memory_delete(self, query: str, history: list, current_checkpoint: Optional[Path]) -> Optional[Path]:
        """处理 /memory-delete <id> [id...] 命令"""
        parts = query.split()
        if len(parts) < 2:
            self.console.print("\n[bold yellow]用法：/memory-delete <memory_id> [memory_id ...][/bold yellow]")
            return current_checkpoint

        deleted_ids = []
        missing_ids = []
        for memory_id in parts[1:]:
            if delete_long_term_memory(memory_id):
                deleted_ids.append(memory_id)
            else:
                missing_ids.append(memory_id)

        new_checkpoint = current_checkpoint
        if deleted_ids:
            if history and history[0].get("role") == "system":
                history[0]["content"] = self.get_system_prompt_fn()
            new_checkpoint = self.save_checkpoint(history, current_checkpoint)
            self.console.print(f"\n[bold green]已删除长期记忆：{', '.join(deleted_ids)}[/bold green]")
        if missing_ids:
            self.console.print(f"\n[bold yellow]未找到 active 长期记忆：{', '.join(missing_ids)}[/bold yellow]")
        return new_checkpoint

    def handle_memory_panel(self, history: list, current_checkpoint: Optional[Path]) -> Optional[Path]:
        """处理 /memory-panel 命令"""
        import utils.memory as memory_provider

        deleted_ids = manage_memories_tui(memory_provider)
        if not deleted_ids:
            return current_checkpoint

        if history and history[0].get("role") == "system":
            history[0]["content"] = self.get_system_prompt_fn()
        new_checkpoint = self.save_checkpoint(history, current_checkpoint)
        self.console.print(f"\n[bold green]已删除长期记忆：{', '.join(deleted_ids)}[/bold green]")
        return new_checkpoint

    def handle_memory_config(self, query: str) -> bool:
        """处理 /memory-config 命令"""
        if query.strip() != "/memory-config":
            self.console.print("\n[bold yellow]用法：/memory-config[/bold yellow]", tui_region=TuiRegion.TOOLS)
            return True

        current_values = {
            "memory_size": get_memory_size(),
            "keep_recent_tool_call": get_keep_recent_tool_call(),
        }
        result = manage_memory_config_tui(current_values)
        if result == "<cancelled>":
            self.console.print("\n[#aaaaaa]已取消记忆配置修改。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
            return True
        if not isinstance(result, dict):
            return True

        set_memory_size(result["memory_size"])
        set_keep_recent_tool_call(result["keep_recent_tool_call"])
        refresh_tools_title()
        self.console.print(
            "\n[bold green]记忆配置已更新[/bold green]\n"
            f"  memory_size: {result['memory_size']} "
            f"[#aaaaaa](当前 active：{get_active_memory_count()})[/#aaaaaa]\n"
            f"  keep_recent_tool_call: {result['keep_recent_tool_call']}",
            tui_region=TuiRegion.TOOLS,
        )
        return True

    def handle_memory_update(self, query: str, history: list) -> bool:
        """处理 /memory-update <prompt> 命令"""
        parts = query.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            self.console.print("\n[bold yellow]用法：/memory-update <memory update request>[/bold yellow]")
            return True

        set_agent_loop_active(True)
        try:
            outputs = manual_memory_update(parts[1].strip(), history)
            if outputs and history and history[0].get("role") == "system":
                history[0]["content"] = self.get_system_prompt_fn()
        finally:
            set_agent_loop_active(False)
        return True

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
                "\n[bold yellow]📂 没有找到任何历史对话记录 (No checkpoints found).[/bold yellow]",
                tui_region=TuiRegion.TOOLS,
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
            self.console.print("[#aaaaaa]已取消加载。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
            return history, current_checkpoint

        try:
            loaded = self.load_checkpoint(Path(selected_path))
            if loaded and loaded[0].get("role") == "system":
                loaded[0]["content"] = self.get_system_prompt_fn()
            new_checkpoint = Path(selected_path)

            for region in (
                TuiRegion.CONTENT,
                TuiRegion.TOOLS,
                TuiRegion.REASONING,
                TuiRegion.BACKGROUND,
                TuiRegion.SUB_AGENT,
            ):
                post_tui(region, "", clear=True)
            begin_tui_batch_render()
            try:
                render_banner_fn()
                render_hint_fn()
                render_history_fn(loaded)
            finally:
                end_tui_batch_render()

            self.console.print(
                f"\n[bold green]🚀 成功加载对话记录！当前上下文包含 {len(loaded)} 条消息。[/bold green]",
                tui_region=TuiRegion.TOOLS,
            )
            refresh_status()
            hitl_mod.SESSION_WHITELIST.clear()
            hitl_mod.PATH_WHITELIST.clear()
        except Exception as exc:
            log_error_traceback("commands handle_load error", exc)
            self.console.print(f"\n[bold red]❌ 加载失败: {exc}[/bold red]", tui_region=TuiRegion.TOOLS)
            return history, current_checkpoint

        # 检查任务看板
        task_plans = list_task_plans()
        if task_plans:
            self.console.print(
                "\n[bold cyan]📋 发现保存的任务看板 (Task Plans)，是否要加载？[/bold cyan]",
                tui_region=TuiRegion.TOOLS,
            )

            try:
                selected_task_path = interactive_choose_checkpoint(
                    task_plans,
                    title="\n📌 Select a Task Plan to Load (Use ⬆ / ⬇ arrows, Enter to confirm, Q to cancel):\n",
                )
            except Exception as exc:
                log_error_traceback("commands handle_load task plan", exc)
                selected_task_path = "abort"

            if selected_task_path != "abort":
                try:
                    plan_data = load_task_plan(Path(selected_task_path))
                    self.console.print(
                        "[bold green]🚀 成功加载任务看板！[/bold green]",
                        tui_region=TuiRegion.TOOLS,
                    )

                    has_incomplete = any(
                        task.get("status") != "completed"
                        for task in plan_data.get("tasks", {}).values()
                    )

                    if has_incomplete:
                        team_histories = list_team_histories()
                        if team_histories:
                            self.console.print(
                                "\n[bold cyan]💡 发现子代理执行历史 (Team Histories)，是否要加载？[/bold cyan]",
                                tui_region=TuiRegion.TOOLS,
                            )

                            try:
                                selected_team_path = interactive_choose_checkpoint(
                                    team_histories,
                                    title="\n📌 Select a Team History to Load (Use ⬆ / ⬇ arrows, Enter to confirm, Q to cancel):\n",
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
                                        "[bold green]✅ 成功加载子代理执行历史！[/bold green]",
                                        tui_region=TuiRegion.TOOLS,
                                    )
                                except Exception as exc:
                                    log_error_traceback(
                                        "commands handle_load team history error", exc
                                    )
                                    self.console.print(
                                        f"[bold red]❌ 加载子代理执行历史失败: {exc}[/bold red]",
                                        tui_region=TuiRegion.TOOLS,
                                    )
                except Exception as exc:
                    log_error_traceback("commands handle_load task plan error", exc)
                    self.console.print(
                        f"\n[bold red]❌ 加载任务看板失败: {exc}[/bold red]",
                        tui_region=TuiRegion.TOOLS,
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
            refresh_status()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-switch":
            self.handle_mcp_switch()
            refresh_status()
            return CommandResult(action=CommandAction.CONTINUE)

        # /cmds, /help - 列出命令
        if query in ["/cmds", "/help"]:
            self.handle_cmds()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/tasks":
            self.handle_task_table()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/models":
            self.handle_models()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/layout":
            self.handle_layout()
            return CommandResult(action=CommandAction.CONTINUE)

        # /plan - 切换 Plan Mode
        if query == "/plan":
            new_state = toggle_plan_mode()
            if new_state:
                self.console.print(
                    "\n[bold cyan]📋 Plan Mode 已启用[/bold cyan]"
                )
                self.console.print(
                    "[#aaaaaa]📋 只允许只读和规划工具。使用 /plan 或 Ctrl+P 切回执行模式。[/#aaaaaa]"
                )
            else:
                self.console.print(
                    "\n[bold green]✅ Plan Mode 已退出，所有工具已恢复。[/bold green]"
                )
                render_current_task_plan(self.console)
            return CommandResult(action=CommandAction.CONTINUE)

        # /hitl - 切换 HITL 拦截状态
        if query == "/hitl":
            new_state = hitl_mod.toggle_hitl()
            status = "开启" if new_state else "关闭"
            status_color = "green" if new_state else "yellow"
            self.console.print(f"\n[bold]🛡️ Human-in-the-Loop 状态: [{status_color}]{status}[/{status_color}][/bold]")
            if not new_state:
                self.console.print("[#aaaaaa]⚠️ 警告：所有敏感操作将自动执行，不再需要确认[/#aaaaaa]")
            return CommandResult(action=CommandAction.CONTINUE)

        # /sub-agent-console - 切换 Sub-Agent 的控制台输出状态
        if query == "/sub-agent-console":
            new_state = toggle_sub_agent_console()
            status = "开启" if new_state else "关闭"
            status_color = "green" if new_state else "yellow"
            self.console.print(f"\n[bold]📊 Sub-Agent 输出状态: [{status_color}]{status}[/{status_color}][/bold]")
            return CommandResult(action=CommandAction.CONTINUE)

        # /update - 检查更新
        if query == "/update":
            new_exe_path = self.handle_update()
            if new_exe_path is not None:
                return CommandResult(action=CommandAction.LAUNCH_UPDATER_AND_EXIT, payload=new_exe_path)
            return CommandResult(action=CommandAction.CONTINUE)

        # /skills 相关命令
        if query == "/skills-switch":
            new_system = self.handle_skills_switch()
            return CommandResult(action=CommandAction.UPDATE_SYSTEM_PROMPT, payload=new_system)

        if query == "/skills-list":
            self.handle_skills_list()
            return CommandResult(action=CommandAction.CONTINUE)

        # /new - 清空历史
        if query == "/new":
            self.handle_new(history, current_checkpoint)
            return CommandResult(action=CommandAction.RESET_CHECKPOINT)

        # /pwd - 显示当前工作目录
        if query == "/pwd":
            render_current_workdir()
            return CommandResult(action=CommandAction.CONTINUE)

        # /cd - 切换当前工作目录，并开启全新会话
        if query == "/cd" or query.startswith("/cd "):
            changed = self.handle_cd(query, history)
            if changed:
                return CommandResult(action=CommandAction.RESET_CHECKPOINT)
            return CommandResult(action=CommandAction.CONTINUE)

        # /compact - 压缩上下文
        if query == "/compact":
            _, new_checkpoint = self.handle_compact(history, current_checkpoint)
            return CommandResult(action=CommandAction.UPDATE_CHECKPOINT, payload=new_checkpoint)

        # /memory-list - 列出长期记忆
        if query == "/memory-list":
            self.handle_memory_list()
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-panel - 交互式查看和删除长期记忆
        if query == "/memory-panel":
            new_checkpoint = self.handle_memory_panel(history, current_checkpoint)
            return CommandResult(action=CommandAction.UPDATE_CHECKPOINT, payload=new_checkpoint)

        # /memory-delete <id> - 删除长期记忆
        if query == "/memory-delete" or query.startswith("/memory-delete "):
            new_checkpoint = self.handle_memory_delete(query, history, current_checkpoint)
            return CommandResult(action=CommandAction.UPDATE_CHECKPOINT, payload=new_checkpoint)

        # /memory-config - 查看或设置记忆配置
        if query == "/memory-config" or query.startswith("/memory-config "):
            self.handle_memory_config(query)
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-update <prompt> - 主动管理长期记忆
        if query == "/memory-update" or query.startswith("/memory-update "):
            self.handle_memory_update(query, history)
            refresh_status()
            return CommandResult(action=CommandAction.CONTINUE)

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
