"""
斜杠命令模块 - 负责处理所有内置命令和交互式界面
"""
import json
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Any, Callable

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from init import log_error_traceback
from system.cli import COMMAND_DESCRIPTIONS
from system.console_render import render_current_task_plan, render_current_workdir, toggle_sub_agent_console
from system.models import get_model_manager
from system.tool_history import TOOL_EXECUTION_HISTORY
from system.tui_app import choose_model_panel_tui, choose_tui, post_tui, TuiRegion, choose_add_model_tui, choose_mcp_switch_tui, manage_models_tui, manage_skills_tui, manage_layout_tui, manage_memories_tui, manage_memory_config_tui, choose_recall_model_tui, show_info_panel_tui, manage_tasks_tui, show_copy_content_tui, show_tool_history_tui, set_agent_loop_active, refresh_status, refresh_tools_title, flush_tui_screen, begin_tui_batch_render, end_tui_batch_render
from utils import hitl as hitl_mod, paths
from utils.conversations import ConversationStore
from utils.llm_client import strip_native_message_payloads
from utils.mcp_config import parse_mcp_add_query
from utils.memory_catalog import sort_memory_records
from utils.plan_mode import toggle_plan_mode
from utils.memory import (
    delete_long_term_memory,
    get_active_memory_count,
    get_compaction_thresholds,
    get_context_length,
    get_memory_recall_window_size,
    get_partial_compact_percentages,
    get_tool_output_compact_tokens,
    get_memory_size,
    list_long_term_memories,
    manual_memory_update,
    reset_memory_recall_windows,
    set_compaction_thresholds,
    set_context_length,
    set_memory_recall_window_size,
    set_partial_compact_percentages,
    set_tool_output_compact_tokens,
    set_memory_size,
)
from system.updater import AUTO_UPDATE_SUPPORTED, check_update, download_update


class CommandAction(Enum):
    EXIT = auto()
    CONTINUE = auto()
    RUN_AGENT = auto()
    RESET_CONVERSATION = auto()
    LOAD_HISTORY = auto()
    UPDATE_SYSTEM_PROMPT = auto()
    LAUNCH_UPDATER_AND_EXIT = auto()


@dataclass
class CommandResult:
    action: CommandAction
    payload: Any = None
    skip_memory_recall: bool = False


# ============================================================================
# Conversation 选择器
# ============================================================================

def interactive_choose_conversation(
        conversations: list,
        title: str = "\n📌 Select a Conversation to Load (Use ⬆ / ⬇ arrows, Enter to confirm, Q to cancel):\n",
        delete_handler: Callable[[Path], None] | None = None,
        preview_handler: Callable[[Path], tuple[str, Any]] | None = None,
        title_handler: Callable[[Path], str | None] | None = None,
) -> str:
    """交互式选择 6.0 conversation。"""
    if not conversations:
        return "abort"

    options = []
    for conversation in conversations:
        conversation_id = conversation.parent.name
        mtime = conversation.stat().st_mtime
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        conversation_title = title_handler(conversation) if title_handler is not None else None
        if conversation_title:
            desc = f"{conversation_id} - {conversation_title} (最近一次更新时间：{date_str})"
        else:
            desc = f"{conversation_id} - 未命名对话 (最近一次更新时间：{date_str})"
        options.append((str(conversation), desc))

    choices = []
    lookup = {}
    for path_value, desc in options:
        choices.append(desc)
        lookup[desc] = path_value

    def _delete_choice(label: str) -> None:
        path = Path(lookup[label])
        if delete_handler is not None:
            delete_handler(path)

    def _preview_choice(label: str) -> tuple[str, Any]:
        path = Path(lookup[label])
        return preview_handler(path)

    selected = choose_tui(
        title.strip(),
        choices,
        delete_handler=_delete_choice if delete_handler is not None else None,
        preview_handler=_preview_choice if preview_handler is not None else None,
    )
    return lookup.get(selected, "abort")


def _conversation_preview(messages: list) -> Text:
    preview = Text()
    user_messages = [message for message in messages if message.get("role") == "user"]
    if not user_messages:
        preview.append("该对话没有 user 询问记录。", style="bold yellow")
        return preview

    for index, message in enumerate(user_messages, start=1):
        if index > 1:
            preview.append("\n\n")
        preview.append(f"[{index}] User\n", style="bold cyan")
        content = message.get("content", "")
        preview.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
    return preview


def _task_plan_preview(plan_data: dict) -> Group:
    tasks = plan_data.get("tasks", {})
    completed = sum(task.get("status") == "completed" for task in tasks.values())
    summary = Text(
        f"Epic ID: {plan_data.get('epic_id', '-')}\n"
        f"任务总数: {len(tasks)} · 已完成: {completed} · 未完成: {len(tasks) - completed}"
    )
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Subject")
    table.add_column("Status", no_wrap=True)
    table.add_column("Depend On")
    for task_id, task in tasks.items():
        table.add_row(
            str(task_id),
            str(task.get("subject", "")),
            str(task.get("status", "pending")),
            ", ".join(str(dep_id) for dep_id in task.get("depend_on", [])) or "-",
        )
    if not tasks:
        table.add_row("-", "当前任务计划为空", "-", "-")
    return Group(summary, table)


# ============================================================================
# MCP 服务开关面板
# ============================================================================

def interactive_switch_mcp_servers(server_switches: list, mcp_manager: Any) -> str | dict:
    """交互式切换 MCP 服务启用/禁用状态"""
    return choose_mcp_switch_tui(server_switches, mcp_manager)


# ============================================================================
# 命令处理器
# ============================================================================

class CommandHandler:
    """命令处理器 - 统一处理所有斜杠命令"""

    DEFAULT_COMPACT_PROMPT = "User triggered compact"
    DEFAULT_MEMORY_UPDATE_PROMPT = "请根据当前对话上下文主动管理长期记忆，提取稳定偏好、项目约定和未来可复用信息；没有值得更新的内容则不变更。"

    def __init__(
            self,
            console: Console,
            mcp_manager,
            skill_loader,
            get_system_prompt_fn,
            conversation_store: ConversationStore,
            auto_compact_fn,
            apply_workdir_fn=None,
    ):
        self.console = console
        self.mcp_manager = mcp_manager
        self.skill_loader = skill_loader
        self.get_system_prompt_fn = get_system_prompt_fn
        self.conversation_store = conversation_store
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
            border_style="#52525b",
            expand=True,
            show_header=False,
            padding=(0, 1),
        )
        summary_table.add_column("状态项", style="bold #d4d4d8", justify="left", no_wrap=True)
        summary_table.add_column("详情", overflow="fold", ratio=3)

        config_display = Text()
        if config_servers:
            config_display.append(f"{len(config_servers)} 个", style="bold cyan")
            config_display.append("  ")
            config_display.append(" · ".join(config_servers), style="white")
        else:
            config_display.append("0 个  未配置", style="#a1a1aa")

        enabled_display = Text()
        if enabled_config_servers:
            enabled_display.append(f"● {len(enabled_config_servers)} 个", style="bold green")
            enabled_display.append("  ")
            enabled_display.append(" · ".join(enabled_config_servers), style="white")
        else:
            enabled_display.append("○ 0 个", style="#a1a1aa")

        disabled_display = Text()
        if disabled_servers:
            disabled_display.append(f"○ {len(disabled_servers)} 个", style="bold yellow")
            disabled_display.append("  ")
            disabled_display.append(" · ".join(disabled_servers), style="white")
        else:
            disabled_display.append("○ 0 个", style="#a1a1aa")

        loaded_display = Text()
        if loaded_servers:
            loaded_display.append(f"● {len(loaded_servers)} 个", style="bold green")
            loaded_display.append("  ")
            for index, name in enumerate(loaded_servers):
                if index:
                    loaded_display.append(" · ", style="#71717a")
                loaded_display.append(name, style="bold magenta")
                loaded_display.append(
                    f" ({tool_count_by_server.get(name, 0)} 工具)",
                    style="#a1a1aa",
                )
        else:
            loaded_display.append("○ 0 个  当前未加载", style="#a1a1aa")

        summary_table.add_row(
            "配置文件",
            Text(str(status.get("config_path", "Not configured")), style="#a1a1aa"),
        )
        summary_table.add_row(
            "后台状态",
            Text("● 运行中", style="bold green")
            if status.get("is_running")
            else Text("○ 未运行", style="bold yellow"),
        )
        summary_table.add_row("服务配置", config_display)
        summary_table.add_row("已启用", enabled_display)
        summary_table.add_row("已禁用", disabled_display)
        summary_table.add_row("已加载", loaded_display)

        table = Table(
            title=(
                "[bold cyan]🛠️ MCP 工具明细[/bold cyan] "
                f"[#a1a1aa]· {status['tool_count']} 个[/#a1a1aa]"
            ),
            box=box.ROUNDED,
            border_style="#52525b",
            header_style="bold #d4d4d8",
            expand=True,
            show_lines=True,
            padding=(0, 1),
        )
        table.add_column("服务节点", style="bold magenta", overflow="fold", ratio=2)
        table.add_column("工具名称", style="bold green", overflow="fold", ratio=3)
        table.add_column("描述", style="white", overflow="fold", ratio=5)

        for tool in status["tools"]:
            table.add_row(
                tool.get("provider", "Unknown"),
                tool["name"],
                tool["description"],
            )
        if not status["tools"]:
            table.add_row(
                Text("—", style="#71717a"),
                Text("暂无可用工具", style="yellow"),
                Text("当前没有已加载的 MCP 工具。", style="#a1a1aa"),
            )

        panel_items = [summary_table, Text(""), table]
        if not status.get("is_running"):
            notice = Text("○ MCP 后台管理器未运行", style="bold yellow")
            notice.append(
                f"\n  配置文件: {status.get('config_path', '未配置')}",
                style="#a1a1aa",
            )
            panel_items.extend([Text(""), notice])
        content = Group(*panel_items)
        if show_info_panel_tui("🔌 MCP 状态与工具", content) == "<cancelled>":
            self.console.print(content, tui_region=TuiRegion.TOOLS)
        return True

    def handle_mcp_restart(self) -> bool:
        """处理 /mcp-restart 命令"""
        self.mcp_manager.restart()
        return True

    def handle_mcp_help(self) -> bool:
        """处理 /mcp-help 命令"""
        content = Markdown(
            """
### MCP 命令帮助

MCP 配置文件位于安装目录的 `.makecode/mcp_config.json`。服务名是唯一标识；如果同名服务已存在，请先使用 `/mcp-delete <name>` 删除，再重新 `/mcp-add`。

#### `/mcp-help`
显示当前帮助说明。

#### `/mcp-view`
查看 MCP 状态总览和已加载工具明细，包括：配置文件路径、后台状态、配置中的服务、已启用/已禁用服务、当前已加载服务和工具列表。

#### `/mcp-add <name> [options] -- <cmd> [args...]`
添加一个 stdio MCP 服务配置。`<name>` 是服务唯一标识，会成为工具名前缀的一部分。服务名已存在时不会覆盖。`--` 后面的内容会作为启动命令解析，第一个值写入 `command`，其余值写入 `args`。

远程 MCP 服务使用：`/mcp-add <name> --url <url> [options]`。

常用参数：

- `--url <url>`：添加远程 MCP 服务，例如 Streamable HTTP 或 SSE 地址。
- `--transport stdio|streamable-http|http|sse`：指定传输类型。`http` 会按 fastmcp 行为归一化为 `streamable-http`；不填时，stdio 命令默认 `stdio`，普通 `--url` 默认 `streamable-http`，包含 `/sse` 的 URL 默认 `sse`。
- `--env KEY=VALUE`：stdio 子进程环境变量，可重复。
- `--header KEY=VALUE`：远程 MCP 请求头，可重复。
- `headers.KEY=VALUE`：另一种设置 headers 的写法，适合一次性补充多个嵌套字段。
- `env.KEY=VALUE`：另一种设置 env 的写法。
- `--cwd <path>`：stdio 子进程工作目录。
- `--auth <value>`：远程 MCP 鉴权配置，支持 token 字符串或 `oauth`。
- `--timeout <milliseconds>`：响应超时时间，单位毫秒。
- `--sse-read-timeout <seconds>`：SSE 读取超时。
- `--keep-alive true|false`：stdio 子进程是否保持存活。

示例：

```bash
/mcp-add fs -- npx -y @modelcontextprotocol/server-filesystem D:/PythonProject/Agent
/mcp-add git -- uvx mcp-server-git --repository D:/PythonProject/Agent
/mcp-add MiniMax --env MINIMAX_API_KEY=api_key --env MINIMAX_API_HOST=https://api.minimaxi.com -- uvx minimax-coding-plan-mcp -y
/mcp-add api --url https://example.com/mcp --header X-Api-Key=secret
/mcp-add api --url https://example.com/mcp headers.Authorization="Bearer token"
/mcp-add legacy --url https://example.com/sse --transport sse
```

#### `/mcp-delete <name>`
删除指定 MCP 服务配置。该命令会二次确认；确认后写入配置文件，并尝试停用运行中的同名服务。

#### `/mcp-switch`
打开交互式 MCP 服务管理面板。可手动添加本地 stdio、远程 Streamable HTTP 或 SSE 服务，也可切换已有服务的启用/禁用状态；新服务默认禁用，确认后保存开关修改并尝试增量启停服务。

#### `/mcp-restart`
重启 MCP 后台管理器，重新读取配置文件并重新连接所有启用的 MCP 服务。适合配置手动编辑后完整重载，或增量启停失败后恢复状态。
""".strip()
        )
        if show_info_panel_tui("🔌 MCP 命令帮助", content) == "<cancelled>":
            self.console.print(content, tui_region=TuiRegion.TOOLS)
        return True

    def handle_mcp_switch(self) -> bool:
        """处理 /mcp-switch 命令"""
        self.console.print(
            "\n[bold cyan]🔧 正在打开 MCP 服务管理面板...[/bold cyan]\n"
            "[#aaaaaa]操作说明：用 ↑/↓ 选择服务，按 Enter 或 Space 切换状态；可通过底部按钮添加服务、确认应用或取消。[/#aaaaaa]",
            tui_region=TuiRegion.TOOLS,
        )
        try:
            server_switches = self.mcp_manager.list_server_switches()
        except FileNotFoundError:
            server_switches = []
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch list", exc)
            self.console.print(f"\n[bold red]❌ 读取 MCP 配置失败: {exc}[/bold red]", tui_region=TuiRegion.TOOLS)
            return True

        try:
            switch_result = interactive_switch_mcp_servers(server_switches, self.mcp_manager)
        except Exception as exc:
            log_error_traceback("commands handle_mcp_switch interactive", exc)
            self.console.print(
                f"\n[bold red]❌ 打开 MCP 开关面板失败: {exc}[/bold red]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        deleted_lines = self._format_mcp_panel_delete_lines(switch_result.get("deleted_results", []))
        added_lines = self._format_mcp_panel_add_lines(switch_result.get("added_results", []))
        if switch_result.get("action") == "cancel":
            lines = [
                "\n[bold yellow]↩️ 已取消本次 MCP 开关草稿修改，开关状态未应用。[/bold yellow]"
            ]
            lines.extend(added_lines)
            lines.extend(deleted_lines)
            self.console.print("\n".join(lines), tui_region=TuiRegion.TOOLS)
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
            lines = [f"\n[bold yellow]ℹ️ {apply_result.get('message', '没有检测到变更。')}[/bold yellow]"]
            lines.extend(added_lines)
            lines.extend(deleted_lines)
            self.console.print("\n".join(lines), tui_region=TuiRegion.TOOLS)
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
        summary_lines.extend(added_lines)
        summary_lines.extend(deleted_lines)
        self.console.print("\n".join(summary_lines), tui_region=TuiRegion.TOOLS)
        return True

    def _format_mcp_add_lines(self, server_name: str, result: dict) -> list[str]:
        failed = result.get("failed", [])
        lines = [
            f"\n[bold green]✅ 已添加 MCP 服务:[/bold green] {escape(server_name)}",
            f"[#aaaaaa]配置文件: {self.mcp_manager.get_status_info().get('config_path')}[/#aaaaaa]",
            f"[#aaaaaa]{escape(result.get('message', ''))}[/#aaaaaa]",
        ]
        if failed:
            failure_text = "; ".join(
                f"{item['server']} ({item['action']} 失败: {item['error']})"
                for item in failed
            )
            lines.append(f"[bold red]服务启用失败:[/bold red] {escape(failure_text)}")
        return lines

    def _format_mcp_panel_add_lines(self, added_results: list[dict]) -> list[str]:
        lines = []
        for item in added_results:
            lines.extend(self._format_mcp_add_lines(item.get("server", ""), item.get("result", {})))
        return lines

    def _format_mcp_delete_lines(self, server_name: str, result: dict) -> list[str]:
        failed = result.get("failed", [])
        lines = [
            f"\n[bold green]✅ 已删除 MCP 服务配置:[/bold green] {escape(server_name)}",
            f"[#aaaaaa]配置文件: {self.mcp_manager.get_status_info().get('config_path')}[/#aaaaaa]",
            f"[#aaaaaa]{escape(result.get('message', ''))}[/#aaaaaa]",
        ]
        if failed:
            failure_text = "; ".join(
                f"{item['server']} ({item['action']} 失败: {item['error']})"
                for item in failed
            )
            lines.append(f"[bold red]服务停用失败:[/bold red] {escape(failure_text)}")
        return lines

    def _format_mcp_panel_delete_lines(self, deleted_results: list[dict]) -> list[str]:
        lines = []
        for item in deleted_results:
            lines.extend(self._format_mcp_delete_lines(item.get("server", ""), item.get("result", {})))
        return lines

    def _parse_mcp_add_config(self, query: str) -> tuple[str, dict]:
        return parse_mcp_add_query(query)

    def handle_mcp_add(self, query: str) -> bool:
        try:
            server_name, cfg = self._parse_mcp_add_config(query)
            result = self.mcp_manager.add_server_config(server_name, cfg)
        except Exception as exc:
            log_error_traceback("commands handle_mcp_add", exc)
            self.console.print(
                "\n[bold yellow]用法：/mcp-add <name> [options] -- <cmd> [args...] 或 /mcp-add <name> --url <url> [options][/bold yellow]\n"
                "[#aaaaaa]常用选项：--env KEY=VALUE、--header KEY=VALUE、--transport stdio|streamable-http|sse。也支持 headers.X=Y / env.X=Y。服务名已存在时请先 /mcp-delete <name>。[/#aaaaaa]\n"
                f"[bold red]❌ {escape(str(exc))}[/bold red]",
                tui_region=TuiRegion.TOOLS,
            )
            return True

        self.console.print("\n".join(self._format_mcp_add_lines(server_name, result)), tui_region=TuiRegion.TOOLS)
        return True

    def handle_mcp_delete(self, query: str) -> bool:
        parts = query.split()
        if len(parts) != 2:
            self.console.print("\n[bold yellow]用法：/mcp-delete <name>[/bold yellow]", tui_region=TuiRegion.TOOLS)
            return True

        server_name = parts[1]
        choice = choose_tui(
            f"确认删除 MCP 服务配置：{server_name}？\n该操作会写入配置文件，并停用运行中的同名服务。",
            ["确认删除", "取消"],
        )
        if choice != "确认删除":
            self.console.print("\n[#aaaaaa]已取消删除 MCP 服务配置。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
            return True

        try:
            result = self.mcp_manager.delete_server_config(server_name)
        except Exception as exc:
            log_error_traceback("commands handle_mcp_delete", exc)
            self.console.print(f"\n[bold red]❌ 删除 MCP 服务失败: {escape(str(exc))}[/bold red]", tui_region=TuiRegion.TOOLS)
            return True

        self.console.print("\n".join(self._format_mcp_delete_lines(server_name, result)), tui_region=TuiRegion.TOOLS)
        return True

    def handle_copy(self, history: list) -> bool:
        """处理 /copy 命令 - 打开只读弹窗查看对话内容"""
        messages = []
        for msg in strip_native_message_payloads(history):
            role = msg.get("role", "")
            content = msg.get("content")
            content_blocks = msg.get("content_blocks") or []
            has_text = (
                isinstance(content, str) and bool(content)
            ) or (
                isinstance(content, list)
                and any(
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and block.get("text")
                    for block in content
                )
            ) or any(
                isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text")
                for block in content_blocks
            )
            tool_calls = msg.get("tool_calls") or [
                block
                for block in content_blocks
                if isinstance(block, dict) and block.get("type") == "tool_call"
            ]
            terminal_calls = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                name = (
                    function.get("name", "")
                    if isinstance(function, dict)
                    else tool_call.get("name", "")
                )
                if name == "RunTerminalCommand":
                    terminal_calls.append(tool_call)

            if role == "user" and has_text:
                if isinstance(msg.get("content"), list):
                    msg["content"] = [
                        block
                        for block in msg["content"]
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                msg["content_blocks"] = [
                    block
                    for block in content_blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                messages.append(msg)
            elif role == "assistant" and (has_text or terminal_calls):
                msg.pop("reasoning_content", None)
                msg.pop("reasoning", None)
                if isinstance(msg.get("content"), list):
                    msg["content"] = [
                        block
                        for block in msg["content"]
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                msg["content_blocks"] = [
                    block
                    for block in content_blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                msg["tool_calls"] = terminal_calls
                messages.append(msg)
            elif role in ("tool", "function") and msg.get("name") == "RunTerminalCommand":
                messages.append(msg)
        if not messages:
            self.console.print("[#aaaaaa]当前对话暂无可查看的内容。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
            return True
        show_copy_content_tui(messages)
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
            table.add_row(cmd, escape(desc))
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

        manage_tasks_tui(TASK_MANAGER)
        return True

    def handle_update(self) -> Path | None:
        """处理 /update 命令 - 检查并安装更新"""
        import sys
        from version import CURRENT_VERSION

        self.console.print(f"\n[bold cyan]📋 当前版本: v{CURRENT_VERSION}[/bold cyan]")

        if not getattr(sys, 'frozen', False):
            self.console.print("[bold yellow]⚠️ 开发环境下不支持自动更新，请使用 pyinstaller 打包后再试。[/bold yellow]")
            return None
        if not AUTO_UPDATE_SUPPORTED:
            self.console.print("[bold yellow]⚠️ 当前平台不支持应用内自动更新，请从 GitHub Release 下载对应平台版本。[/bold yellow]")
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
            self.console.print("[#aaaaaa]替换完成后会显示结果，请手动重新启动 MakeCode[/#aaaaaa]")
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

    def handle_skills_list(self) -> str | None:
        """打开项目级 Skills 配置面板；确认变更后返回新的 system prompt。"""
        result = manage_skills_tui(self.skill_loader)
        if isinstance(result, dict) and result.get("action") == "applied":
            enabled = int(result.get("enabled", 0))
            disabled = int(result.get("disabled", 0))
            self.console.print(
                f"\n[bold green]Skills 配置已应用：启用 {enabled} 个，禁用 {disabled} 个。[/bold green]"
            )
            return self.get_system_prompt_fn()
        if result == "<cancelled>":
            self.console.print(Markdown(f"### 当前已启用技能列表\n\n{self.skill_loader.get_descriptions()}"))
        return None

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
        current_text = (
            f"{current_model.get_display_text()} · effort: {current_model.reasoning_effort}"
            f" · format: {current_model.message_format}"
            if current_model
            else "未选择"
        )
        self.console.print(f"\n[bold cyan]已退出模型面板，当前模型：[/bold cyan][bold green]{current_text}[/bold green]", tui_region=TuiRegion.TOOLS)
        return True

    def handle_layout(self) -> bool:
        """处理 /layout 命令"""
        result = manage_layout_tui()
        if isinstance(result, dict):
            self.console.print(
                "\n[bold green]✅ Layout 已应用：[/bold green]"
                f"左侧 Content/Tools = {result['content']}/{result['tools']}；"
                f"右侧 Task/Background/Sub-Agent = {result['task']}/{result['background']}/{result['sub_agent']}",
                tui_region=TuiRegion.TOOLS,
            )
        return True

    def handle_tool_history(self, history: list[dict[str, Any]]) -> bool:
        """打开当前对话的工具执行历史浏览器。"""
        show_tool_history_tui(TOOL_EXECUTION_HISTORY, history)
        return True

    def handle_new(self, history: list, current_conversation: Optional[Path]) -> tuple:
        """处理 /new 命令，返回 (should_continue, new_conversation)。"""
        from utils import tasks as tasks_module
        from utils import teams as teams_module

        self.conversation_store.reset()
        tasks_module.refresh_workspace_paths()
        teams_module.refresh_workspace_paths()
        reset_memory_recall_windows()
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
        TOOL_EXECUTION_HISTORY.clear()
        for region in (
            TuiRegion.CONTENT,
            TuiRegion.TOOLS,
            TuiRegion.TASK,
            TuiRegion.BACKGROUND,
            TuiRegion.SUB_AGENT,
        ):
            post_tui(region, "", clear=True)
        render_current_task_plan(self.console)

    async def handle_compact(self, query: str, history: list, current_conversation: Optional[Path]) -> tuple:
        """处理 /compact [prompt] 命令，返回当前 conversation 路径。"""
        parts = query.split(maxsplit=1)
        reason = parts[1].strip() if len(parts) == 2 and parts[1].strip() else self.DEFAULT_COMPACT_PROMPT

        set_agent_loop_active(True)
        try:
            await self.auto_compact(
                history,
                reason=reason,
                system_prompt_fn=self.get_system_prompt_fn,
            )
            reset_memory_recall_windows()
            self.console.print(
                "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
            conversation_path = self.conversation_store.save_messages(history)
            refresh_status()
            return True, conversation_path
        finally:
            set_agent_loop_active(False)

    def handle_memory_list(self) -> bool:
        """处理 /memory-list 命令"""
        memories = sort_memory_records(list_long_term_memories())
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

    def handle_memory_delete(self, query: str, history: list, current_conversation: Optional[Path]) -> Optional[Path]:
        """处理 /memory-delete <id> [id...] 命令"""
        parts = query.split()
        if len(parts) < 2:
            self.console.print("\n[bold yellow]用法：/memory-delete <memory_id> [memory_id ...][/bold yellow]")
            return current_conversation

        deleted_ids = []
        missing_ids = []
        for memory_id in parts[1:]:
            if delete_long_term_memory(memory_id):
                deleted_ids.append(memory_id)
            else:
                missing_ids.append(memory_id)

        conversation_path = current_conversation
        if deleted_ids:
            if history and history[0].get("role") == "system":
                history[0]["content"] = self.get_system_prompt_fn()
            conversation_path = self.conversation_store.save_messages(history)
            self.console.print(f"\n[bold green]已删除长期记忆：{', '.join(deleted_ids)}[/bold green]")
        if missing_ids:
            self.console.print(f"\n[bold yellow]未找到 active 长期记忆：{', '.join(missing_ids)}[/bold yellow]")
        return conversation_path

    def handle_memory_panel(self, history: list, current_conversation: Optional[Path]) -> Optional[Path]:
        """处理 /memory-panel 命令"""
        import utils.memory as memory_provider

        deleted_ids = manage_memories_tui(memory_provider)
        if not deleted_ids:
            return current_conversation

        if history and history[0].get("role") == "system":
            history[0]["content"] = self.get_system_prompt_fn()
        conversation_path = self.conversation_store.save_messages(history)
        self.console.print(f"\n[bold green]已删除长期记忆：{', '.join(deleted_ids)}[/bold green]")
        return conversation_path

    def handle_memory_config(self, query: str) -> bool:
        """处理 /memory-config 命令"""
        if query.strip() != "/memory-config":
            self.console.print("\n[bold yellow]用法：/memory-config[/bold yellow]", tui_region=TuiRegion.TOOLS)
            return True

        model_manager = get_model_manager()
        tool_output_threshold, partial_threshold = get_compaction_thresholds()
        partial_min_percent, partial_max_percent = get_partial_compact_percentages()
        current_values = {
            "context_length": get_context_length(),
            "memory_size": get_memory_size(),
            "tool_output_compact_threshold": tool_output_threshold,
            "partial_compact_threshold": partial_threshold,
            "tool_output_compact_tokens": get_tool_output_compact_tokens(),
            "partial_compact_min_percent": partial_min_percent,
            "partial_compact_max_percent": partial_max_percent,
            "memory_recall_window_size": get_memory_recall_window_size(),
            "memory_recall_model_key": model_manager.memory_recall_model_key if model_manager else None,
            "memory_recall_model_display": model_manager.get_memory_recall_model_display_text() if model_manager else "同主模型",
        }
        while True:
            result = manage_memory_config_tui(current_values)
            if result == "<cancelled>":
                self.console.print("\n[#aaaaaa]已取消记忆配置修改。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
                return True
            if not isinstance(result, dict):
                return True
            current_values.update(result)
            if result.get("__action") != "choose_recall_model":
                break
            if model_manager is None:
                self.console.print("\n[bold red]❌ 模型管理器未初始化，无法选择记忆召回模型。[/bold red]", tui_region=TuiRegion.TOOLS)
                continue
            model_manager._reload_from_disk()
            options = ["使用主模型（同主模型）"] + [model.get_display_text() for model in model_manager.models]
            keys = [None] + [model.key for model in model_manager.models]
            picker_result = choose_recall_model_tui(options)
            if not picker_result.startswith("select:"):
                continue
            try:
                selected_index = int(picker_result.removeprefix("select:"))
            except ValueError:
                continue
            if not (0 <= selected_index < len(keys)):
                continue
            current_values["memory_recall_model_key"] = keys[selected_index]
            current_values["memory_recall_model_display"] = "同主模型" if selected_index == 0 else options[selected_index]

        set_context_length(result["context_length"])
        set_memory_size(result["memory_size"])
        set_compaction_thresholds(
            result["tool_output_compact_threshold"],
            result["partial_compact_threshold"],
        )
        set_tool_output_compact_tokens(result["tool_output_compact_tokens"])
        set_partial_compact_percentages(
            result["partial_compact_min_percent"],
            result["partial_compact_max_percent"],
        )
        set_memory_recall_window_size(result["memory_recall_window_size"])
        if model_manager is not None:
            model_manager.set_memory_recall_model_by_key(result.get("memory_recall_model_key"))
        refresh_tools_title()
        refresh_status()
        recall_model_text = model_manager.get_memory_recall_model_display_text() if model_manager else "同主模型"
        self.console.print(
            "\n[bold green]记忆配置已更新[/bold green]\n"
            f"  context_length: {result['context_length']}k tokens\n"
            f"  memory_size: {result['memory_size']} "
            f"[#aaaaaa](当前 active：{get_active_memory_count()})[/#aaaaaa]\n"
            f"  tool_output_compact_threshold: {result['tool_output_compact_threshold']}%\n"
            f"  partial_compact_threshold: {result['partial_compact_threshold']}%\n"
            f"  tool_output_compact_tokens: {result['tool_output_compact_tokens']}\n"
            f"  partial_compact_range: {result['partial_compact_min_percent']}%-{result['partial_compact_max_percent']}%\n"
            f"  memory_recall_window_size: {result['memory_recall_window_size']}\n"
            f"  memory_recall_model: {recall_model_text}",
            tui_region=TuiRegion.TOOLS,
        )
        return True

    async def handle_memory_update(self, query: str, history: list) -> bool:
        """处理 /memory-update [prompt] 命令"""
        parts = query.split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) == 2 and parts[1].strip() else self.DEFAULT_MEMORY_UPDATE_PROMPT

        set_agent_loop_active(True)
        try:
            outputs = await manual_memory_update(prompt, history)
            if outputs and history and history[0].get("role") == "system":
                history[0]["content"] = self.get_system_prompt_fn()
        finally:
            set_agent_loop_active(False)
        return True

    def handle_load(
            self,
            history: list,
            current_conversation: Optional[Path],
            render_banner_fn,
            render_hint_fn,
            render_history_fn,
    ) -> tuple:
        """加载一份 6.0 conversation，并自动恢复其任务与 Sub-Agent 历史。"""
        conversations = self.conversation_store.list_conversations()
        if not conversations:
            self.console.print(
                "\n[bold yellow]📂 没有找到任何 6.0 对话记录。[/bold yellow]",
                tui_region=TuiRegion.TOOLS,
            )
            return history, self.conversation_store.active_path

        if len(history) > 1 and self.conversation_store.active_path is None:
            self.conversation_store.save_messages(history)

        def _delete_conversation(path: Path) -> None:
            self.conversation_store.delete(path)

        def _preview_conversation(path: Path) -> tuple[str, Text]:
            snapshot = self.conversation_store.load(path)
            return f"对话预览 · {snapshot.title or '未命名对话'}", _conversation_preview(snapshot.messages)

        try:
            selected_path = interactive_choose_conversation(
                conversations,
                delete_handler=_delete_conversation,
                preview_handler=_preview_conversation,
                title_handler=self.conversation_store.get_title,
            )
        except Exception as exc:
            log_error_traceback("commands handle_load conversation", exc)
            selected_path = "abort"

        if selected_path == "abort":
            self.console.print("[#aaaaaa]已取消加载。[/#aaaaaa]", tui_region=TuiRegion.TOOLS)
            return history, self.conversation_store.active_path

        try:
            snapshot = self.conversation_store.load(Path(selected_path))
            loaded = snapshot.messages
            if loaded and loaded[0].get("role") == "system":
                loaded[0]["content"] = self.get_system_prompt_fn()

            from utils import tasks as tasks_module
            from utils import teams as teams_module

            next_task_manager = tasks_module.TaskManager(
                snapshot.root,
                snapshot.conversation_id,
                snapshot.task_plan,
            )
            next_team = teams_module.TeammateManager(
                snapshot.root,
                snapshot.conversation_id,
                snapshot.sub_agent_history,
            )
        except Exception as exc:
            log_error_traceback("commands handle_load error", exc)
            self.console.print(f"\n[bold red]❌ 加载失败: {exc}[/bold red]", tui_region=TuiRegion.TOOLS)
            return history, self.conversation_store.active_path

        try:
            self.conversation_store.activate(snapshot)
        except Exception as exc:
            log_error_traceback("commands handle_load activation", exc)
            self.console.print(f"\n[bold red]❌ 加载失败: {exc}[/bold red]", tui_region=TuiRegion.TOOLS)
            return history, self.conversation_store.active_path

        tasks_module.TASK_MANAGER = next_task_manager
        teams_module.TEAM = next_team
        TOOL_EXECUTION_HISTORY.clear()
        reset_memory_recall_windows()
        hitl_mod.SESSION_WHITELIST.clear()
        hitl_mod.PATH_WHITELIST.clear()

        try:
            for region in (
                TuiRegion.CONTENT,
                TuiRegion.TOOLS,
                TuiRegion.TASK,
                TuiRegion.BACKGROUND,
                TuiRegion.SUB_AGENT,
            ):
                post_tui(region, "", clear=True)
            begin_tui_batch_render()
            try:
                render_banner_fn()
                render_hint_fn()
                render_history_fn(loaded)
                render_current_task_plan(self.console)
                if snapshot.sub_agent_history:
                    post_tui(
                        TuiRegion.SUB_AGENT,
                        json.dumps(snapshot.sub_agent_history, ensure_ascii=False, indent=2),
                    )
            finally:
                end_tui_batch_render()
        except Exception as exc:
            log_error_traceback("commands handle_load render", exc)

        self.console.print(
            f"\n[bold green]🚀 成功加载对话、任务与子智能体历史！当前上下文包含 {len(loaded)} 条消息。[/bold green]",
            tui_region=TuiRegion.TOOLS,
        )
        return loaded, snapshot.path

    async def process_command(
            self,
            query: str,
            history: list,
            current_conversation: Optional[Path],
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

        # /flush - 完整重绘 TUI，不修改面板内容
        if query == "/flush":
            flush_tui_screen()
            return CommandResult(action=CommandAction.CONTINUE)

        # MCP 相关命令
        if query == "/mcp-help":
            self.handle_mcp_help()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-view":
            self.handle_mcp_view()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-add" or query.startswith("/mcp-add "):
            self.handle_mcp_add(query)
            refresh_status()
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/mcp-delete" or query.startswith("/mcp-delete "):
            self.handle_mcp_delete(query)
            refresh_status()
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

        if query == "/copy":
            self.handle_copy(history)
            return CommandResult(action=CommandAction.CONTINUE)

        if query == "/tool-history":
            self.handle_tool_history(history)
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
            refresh_status()
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
            new_system = self.handle_skills_list()
            if isinstance(new_system, str):
                return CommandResult(action=CommandAction.UPDATE_SYSTEM_PROMPT, payload=new_system)
            return CommandResult(action=CommandAction.CONTINUE)

        # /new - 清空历史
        if query == "/new":
            self.handle_new(history, current_conversation)
            return CommandResult(action=CommandAction.RESET_CONVERSATION)

        # /pwd - 显示当前工作目录
        if query == "/pwd":
            render_current_workdir()
            return CommandResult(action=CommandAction.CONTINUE)

        # /cd - 切换当前工作目录，并开启全新会话
        if query == "/cd" or query.startswith("/cd "):
            changed = self.handle_cd(query, history)
            if changed:
                return CommandResult(action=CommandAction.RESET_CONVERSATION)
            return CommandResult(action=CommandAction.CONTINUE)

        # /compact [prompt] - 压缩上下文
        if query == "/compact" or query.startswith("/compact "):
            await self.handle_compact(query, history, current_conversation)
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-list - 列出长期记忆
        if query == "/memory-list":
            self.handle_memory_list()
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-panel - 交互式添加、查看和删除长期记忆
        if query == "/memory-panel":
            self.handle_memory_panel(history, current_conversation)
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-delete <id> - 删除长期记忆
        if query == "/memory-delete" or query.startswith("/memory-delete "):
            self.handle_memory_delete(query, history, current_conversation)
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-config - 查看或设置记忆配置
        if query == "/memory-config" or query.startswith("/memory-config "):
            self.handle_memory_config(query)
            return CommandResult(action=CommandAction.CONTINUE)

        # /memory-update [prompt] - 主动管理长期记忆
        if query == "/memory-update" or query.startswith("/memory-update "):
            await self.handle_memory_update(query, history)
            refresh_status()
            return CommandResult(action=CommandAction.CONTINUE)

        # /load - 加载历史
        if query == "/load":
            new_history, new_conversation = self.handle_load(
                history,
                current_conversation,
                render_banner_fn,
                render_hint_fn,
                render_history_fn,
            )
            return CommandResult(action=CommandAction.LOAD_HISTORY, payload=(new_history, new_conversation))

        # /nm <query> - 跳过本次请求的记忆预召回
        if query == "/nm" or query.startswith("/nm "):
            user_query = query.removeprefix("/nm").strip()
            if not user_query:
                self.console.print(
                    "\n[bold yellow]用法：/nm <query>[/bold yellow]",
                    tui_region=TuiRegion.TOOLS,
                )
                return CommandResult(action=CommandAction.CONTINUE)
            return CommandResult(
                action=CommandAction.RUN_AGENT,
                payload=user_query,
                skip_memory_recall=True,
            )

        # 其他命令 - 让 LLM 处理
        # 对于在 COMMAND_DESCRIPTIONS 中的命令，附加描述（与原始逻辑一致）
        if query in COMMAND_DESCRIPTIONS:
            return CommandResult(action=CommandAction.RUN_AGENT, payload=f"{query} {COMMAND_DESCRIPTIONS[query]}")
        return CommandResult(action=CommandAction.RUN_AGENT, payload=query)
