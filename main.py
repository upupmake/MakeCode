import concurrent.futures
import json
import sys
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, TextColumn, BarColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from init import WORKDIR, log_error_traceback
from prompts import get_orchestrator_system_prompt
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    STARTUP_TERMINAL_TYPE,
    STARTUP_TERMINAL_SOURCE,
    run_read,
    run_write,
    run_edit,
)
from utils.file_access import AgentFileAccess
from utils.llm_client import llm_client
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.memory import (
    micro_compact,
    auto_compact,
    THRESHOLD,
    estimate_tokens,
    save_checkpoint,
    list_checkpoints,
    load_checkpoint,
)
from utils.skills import (
    SKILL_TOOLS,
    SKILL_TOOLS_HANDLERS,
    SKILL_LOADER,
)
from utils.tasks import (
    TASK_MANAGER_TOOLS,
    TASK_MANAGER_TOOLS_HANDLERS,
    list_task_plans,
    load_task_plan,
)
from utils.teams import TEAM_TOOLS_HANDLERS, TEAM_TOOLS

console = Console(force_terminal=True)
STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"

MAKECODE_ASCII = r"""
███╗   ███╗ █████╗ ██╗  ██╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║█████╔╝ █████╗  ██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""

USER_SESSION = None


def build_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        WORKDIR,
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
    )


def refresh_system_prompt() -> str:
    global SYSTEM
    SYSTEM = build_system_prompt()
    return SYSTEM


def get_current_tools_definition():
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    return llm_client.format_tools(
        COMMON_TOOLS
        + SKILL_TOOLS
        + TASK_MANAGER_TOOLS
        + TEAM_TOOLS
        + GLOBAL_MCP_MANAGER.get_tools()
    )


SYSTEM = build_system_prompt()

BASE_SUPER_TOOLS = llm_client.format_tools(
    COMMON_TOOLS + SKILL_TOOLS + TASK_MANAGER_TOOLS + TEAM_TOOLS
)

orchestrator_access = AgentFileAccess()

BASE_SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **COMMON_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
    "RunRead": lambda path, start=None, end=None, **kwargs: run_read(
        path, start, end, orchestrator_access
    ),
    "RunWrite": lambda path, content, **kwargs: run_write(
        path, content, orchestrator_access
    ),
    "RunEdit": lambda path, start, end, new_content, **kwargs: run_edit(
        path, start, end, new_content, orchestrator_access
    ),
}


def _extract_message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = [
        part["text"] for part in content if isinstance(part, dict) and part.get("text")
    ]
    return "\n\n".join(chunks).strip()


def _parse_arguments(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        payload = arguments.strip()
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            log_error_traceback("main parse arguments json decode", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _stringify_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, indent=2)


def _render_orchestrator_message(text: str):
    if not text:
        return
    console.print(
        Panel(
            Markdown(text),
            title="[bold magenta] 🧠 Orchestrator[/bold magenta]",
            border_style="magenta",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


def _render_tool_call(name: str, arguments: Any):
    body = (
        Syntax(
            json.dumps(arguments, ensure_ascii=False, indent=2),
            "json",
            word_wrap=True,
            theme="monokai",
        )
        if isinstance(arguments, (dict, list))
        else Text(str(arguments))
    )
    console.print(
        Panel(
            body,
            title=f"[bold cyan] 🛠️ Action: {name}[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def _render_tool_output(name: str, output: Any):
    text = _stringify_output(output).strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            body = Syntax(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                "json",
                word_wrap=True,
                theme="monokai",
            )
        except json.JSONDecodeError as exc:
            log_error_traceback("main render tool output json decode", exc)
            body = Text(text)
    else:
        body = Text(text, style="dim")
    console.print(
        Panel(
            body,
            title=f"[bold green] ✅ Result: {name}[/bold green]",
            border_style="green",
            box=box.ROUNDED,
        )
    )


def _render_user_message(text: str):
    if not text:
        return
    console.print(
        Panel(
            Text(text),
            title="[bold green] 👤 User[/bold green]",
            border_style="green",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def _render_history(messages: list):
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        elif role == "user":
            _render_user_message(_extract_message_text(msg))
        elif role == "assistant":
            content = msg.get("content")
            if content:
                _render_orchestrator_message(content)

            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                tc_func = (
                    tc.get("function", {})
                    if isinstance(tc, dict)
                    else getattr(tc, "function", {})
                )
                if tc_func:
                    tc_name = (
                        tc_func.get("name")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "name", "")
                    )
                    tc_args = (
                        tc_func.get("arguments")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "arguments", "")
                    )
                    if tc_name:
                        _render_tool_call(tc_name, _parse_arguments(tc_args))
        elif role == "tool" or role == "function":
            content = msg.get("content") or msg.get("output")
            name = msg.get("name") or "Tool"
            if content:
                _render_tool_output(name, content)


def _render_token_usage(messages: list):
    tokens = estimate_tokens(
        messages, tools_definition=get_current_tools_definition(), system_prompt=SYSTEM
    )
    pct = (tokens / THRESHOLD) * 100
    color = "green" if pct < 70 else "yellow" if pct < 90 else "red"
    console.print(
        f"[{color} dim] 📈 Context: {tokens}/{THRESHOLD} Tokens ({pct:.1f}%)[/]"
    )


def _request_with_progress(messages: list, current_tools: list):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            llm_client.generate,
            messages=messages,
            tools=current_tools,
        )

        with Progress(
            BarColumn(bar_width=30),
            TextColumn("[bold cyan] ✨ Orchestrator is thinking..."),
            transient=True,
            console=console,
        ) as progress:
            progress.add_task("", total=None)
            return future.result()


def _render_startup_banner():
    subtitle = f"Terminal Environment: [bold]{STARTUP_TERMINAL_LABEL}[/bold] (source={STARTUP_TERMINAL_SOURCE})"
    console.print(
        Panel(
            Text(MAKECODE_ASCII.strip("\n"), style="bold bright_blue"),
            title="[bold white]MakeCode Agent[/bold white]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            subtitle=subtitle,
            subtitle_align="center",
            padding=(1, 4),
        )
    )


def _render_env_customization_hint():
    hint_text = (
        " 💡 下次启动前可通过环境变量自定义模型：\n"
        "MODEL_ID=xxx\nOPENAI_BASE_URL=xxx\nOPENAI_API_KEY=xxx"
    )
    console.print(
        Panel(
            Text(hint_text, style="bold yellow"),
            title="[bold yellow]环境变量提示[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


COMMAND_DESCRIPTIONS = {
    "/cmds": "列出所有的可用命令和功能描述",
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
    "/help": "显示使用帮助和自我介绍",
    "/workspace": "查看当前工作区目录结构",
    "/ls": "查看当前工作区目录结构",
    "/clear": "清空当前对话历史",
    "/reset": "清空当前对话历史",
    "/quit": "退出程序",
    "/exit": "退出程序",
}


class SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd, desc in COMMAND_DESCRIPTIONS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)


command_completer = SlashCommandCompleter()


def _init_user_session():
    global USER_SESSION
    if USER_SESSION is not None:
        return
    try:
        user_kb = KeyBindings()

        @user_kb.add(Keys.Enter)
        def _submit_query(event):
            buffer = event.current_buffer
            text = buffer.text.strip()

            if text.startswith("/"):
                if text in COMMAND_DESCRIPTIONS:
                    buffer.validate_and_handle()
                    return

                if buffer.complete_state and buffer.complete_state.completions:
                    if buffer.complete_state.current_completion:
                        buffer.apply_completion(
                            buffer.complete_state.current_completion
                        )
                    else:
                        buffer.apply_completion(buffer.complete_state.completions[0])
                    return

            buffer.validate_and_handle()

        @user_kb.add("c-n")
        def _insert_newline(event):
            event.current_buffer.insert_text("\n")

        def prompt_continuation(width, line_number, is_soft_wrap):
            return " " * (width - 4) + " ┊  "

        custom_style = Style.from_dict(
            {
                "prompt": "bold #00ff00",
                "arrow": "#00ffff bold",
            }
        )

        USER_SESSION = PromptSession(
            multiline=True,
            key_bindings=user_kb,
            prompt_continuation=prompt_continuation,
            style=custom_style,
            completer=command_completer,
            reserve_space_for_menu=5,
            complete_while_typing=True,
        )
    except Exception as exc:
        log_error_traceback("main init user session", exc)
        print_formatted_text(
            HTML(f"\n<ansired>Error initializing prompt session: {exc}</ansired>")
        )
        sys.exit(1)


def _read_user_query(messages: list = None) -> str:
    _init_user_session()

    console.print(
        "\n[dim] 💡 Tip: Press [bold]Enter[/bold] to send, [bold]Ctrl+N[/bold] for newline.[/dim]"
    )

    rprompt = []
    if messages is not None:
        tokens = estimate_tokens(
            messages,
            tools_definition=get_current_tools_definition(),
            system_prompt=SYSTEM,
        )
        pct = (tokens / THRESHOLD) * 100
        color = "ansigreen" if pct < 70 else "ansiyellow" if pct < 90 else "ansired"
        rprompt = [(f"fg:{color}", f" 📈 Tokens: {tokens}/{THRESHOLD} ({pct:.1f}%) ")]

    try:
        with patch_stdout():
            return USER_SESSION.prompt(
                [
                    ("class:prompt", " 🤖 User "),
                    ("class:arrow", "❯❯ "),
                ],
                rprompt=rprompt,
            )
    except Exception as exc:
        log_error_traceback("main user input prompt failure", exc)
        raise


def agent_loop(messages: list):
    global CURRENT_CHECKPOINT
    micro_compact(messages)
    current_super_tools = get_current_tools_definition()
    current_handlers = {
        **BASE_SUPER_TOOLS_HANDLERS,
        **GLOBAL_MCP_MANAGER.get_handlers(),
    }

    while True:
        _render_token_usage(messages)

        try:
            response = _request_with_progress(messages, current_super_tools)
        except Exception as e:
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"Error during agent execution: {e}."
            console.print(f"[bold red] ⚠️ {error_msg}[/bold red]")
            break

        text_content, tool_calls, raw_message = llm_client.parse_response(response)
        llm_client.append_assistant_message(messages, raw_message)
        has_tool_call = len(tool_calls) > 0

        if text_content:
            _render_orchestrator_message(text_content)

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]

            _render_tool_call(tool_name, _parse_arguments(tool_args))

            try:
                arguments = _parse_arguments(tool_args)
                handler = current_handlers.get(tool_name)
                if handler:
                    output = handler(**arguments)
                else:
                    output = f"Unknown tool: {tool_name}"
            except Exception as e:
                log_error_traceback(
                    f"Orchestrator tool execution error: {tool_name}", e
                )
                output = f"Error executing {tool_name}: {e}."

            _render_tool_output(tool_name, output)

            messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

        CURRENT_CHECKPOINT = save_checkpoint(messages, CURRENT_CHECKPOINT)

        if not has_tool_call:
            break
    current_context_tokens = estimate_tokens(
        messages, tools_definition=current_super_tools, system_prompt=SYSTEM
    )
    if current_context_tokens > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{current_context_tokens} exceeded threshold {THRESHOLD}."
        )
        try:
            auto_compact(messages, reason=compact_reason)
            console.print(
                "\n[bold green] ✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
        except Exception as e:
            log_error_traceback("Orchestrator auto-compact error", e)
            error_msg = f"Error executing auto_compact: {e}."
            console.print(f"[bold red] ⚠️ {error_msg}[/bold red]")


def _interactive_choose_checkpoint(
    checkpoints: list,
    title: str = "\n 📌 Select a Checkpoint to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
) -> str:
    if not checkpoints:
        return "abort"

    options = []
    for cp in checkpoints:
        parts = cp.stem.split("_")
        uid = parts[-1] if len(parts) >= 4 else cp.name
        mtime = cp.stat().st_mtime
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        desc = f"File: {uid} (Last updated: {date_str})"
        options.append((str(cp), desc))

    options.append(("abort", "取消 (Cancel)"))

    selected_index = [0]
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _(event):
        selected_index[0] = min(len(options) - 1, selected_index[0] + 1)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=options[selected_index[0]][0])

    @kb.add("c-c")
    def _(event):
        event.app.exit(result="abort")

    def get_formatted_text():
        result = [("class:title", title)]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f" 👉 {text}\n"))
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


def _interactive_switch_mcp_servers(server_switches: list) -> str | dict:
    if not server_switches:
        return "empty"

    selected_index = [0]
    draft_states = {item["name"]: bool(item["disabled"]) for item in server_switches}
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        selected_index[0] = max(0, selected_index[0] - 1)

    @kb.add("down")
    def _(event):
        selected_index[0] = min(len(server_switches) + 1, selected_index[0] + 1)

    @kb.add("space")
    def _(event):
        if selected_index[0] < len(server_switches):
            server_name = server_switches[selected_index[0]]["name"]
            draft_states[server_name] = not draft_states[server_name]

    @kb.add("enter")
    def _(event):
        if selected_index[0] == len(server_switches):
            event.app.exit(
                result={
                    "action": "confirm",
                    "disabled_updates": dict(draft_states),
                }
            )
        elif selected_index[0] == len(server_switches) + 1:
            event.app.exit(result={"action": "cancel"})

    @kb.add("c-c")
    def _(event):
        event.app.exit(result={"action": "cancel"})

    def get_formatted_text():
        lines = [
            (
                "class:title",
                "\n 🔀 MCP 服务开关面板\n"
                " 使用 ↑/↓ 选择，Space 切换启用/禁用，Enter 在底部执行确认或取消。\n"
                " 已启用 = disabled=False；已禁用 = disabled=True\n\n",
            )
        ]

        for i, item in enumerate(server_switches):
            name = item["name"]
            disabled = draft_states[name]
            enabled = not disabled
            loaded = item.get("loaded", False)
            marker = "👉" if i == selected_index[0] else "  "
            switch_box = "[x]" if enabled else "[ ]"
            runtime_txt = "已加载" if loaded else "未加载"
            status_txt = "启用" if enabled else "禁用"
            style = "class:selected" if i == selected_index[0] else "class:unselected"
            lines.append(
                (
                    style,
                    f" {marker} {switch_box} {name}    当前草稿: {status_txt}    运行态: {runtime_txt}\n",
                )
            )

        lines.append(("class:title", "\n"))
        confirm_style = (
            "class:selected"
            if selected_index[0] == len(server_switches)
            else "class:unselected"
        )
        cancel_style = (
            "class:selected"
            if selected_index[0] == len(server_switches) + 1
            else "class:unselected"
        )
        lines.append((confirm_style, "  [确认保存并应用变更]\n"))
        lines.append((cancel_style, "  [取消，不保存本次修改]\n"))
        return lines

    control = FormattedTextControl(get_formatted_text)
    window = Window(content=control, height=len(server_switches) + 8)
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


CURRENT_CHECKPOINT = None

if __name__ == "__main__":
    _render_startup_banner()
    _render_env_customization_hint()

    GLOBAL_MCP_MANAGER.initialize(console=console)
    GLOBAL_MCP_MANAGER.start_background()

    history = [{"role": "system", "content": SYSTEM}]
    try:
        while True:
            try:
                query = _read_user_query(history)
            except (EOFError, KeyboardInterrupt) as exc:
                log_error_traceback("main user input interrupted", exc)
                console.print(
                    "\n[bold yellow] 👋 Exiting MakeCode Agent. Goodbye![/bold yellow]"
                )
                break

            query = query.strip()
            if not query:
                continue

            if query in ["/quit", "/exit"]:
                console.print(
                    "\n[bold yellow] 👋 Exiting MakeCode Agent. Goodbye![/bold yellow]"
                )
                break

            if query == "/mcp-view":
                status = GLOBAL_MCP_MANAGER.get_status_info()
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
                console.print(summary_table)

                if not status.get("is_running"):
                    console.print(
                        "\n[bold yellow]⚠️ MCP 后台管理器当前未运行。若配置已准备好，可执行 /mcp-restart 或使用 /mcp-switch 保存启用状态后触发加载。[/bold yellow]"
                    )
                    continue

                if status.get("tool_count", 0) == 0:
                    console.print(
                        "\n[bold yellow]⚠️ 当前没有已加载的 MCP 工具。请检查配置中的启用状态、服务连通性，或尝试 /mcp-restart。[/bold yellow]"
                    )
                    continue

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

                console.print(table)
                continue

            if query == "/mcp-restart":
                GLOBAL_MCP_MANAGER.restart()
                continue

            if query == "/mcp-switch":
                console.print(
                    "\n[bold cyan]🔧 正在打开 MCP 开关面板...[/bold cyan]\n"
                    "[dim]操作说明：用 ↑/↓ 选择服务，按 Space 切换状态，移动到底部后按 Enter 选择确认或取消。[/dim]"
                )
                try:
                    server_switches = GLOBAL_MCP_MANAGER.list_server_switches()
                except FileNotFoundError as exc:
                    console.print(f"\n[bold yellow]⚠️ {exc}[/bold yellow]")
                    continue
                except Exception as exc:
                    log_error_traceback("main list mcp switches", exc)
                    console.print(f"\n[bold red]❌ 读取 MCP 配置失败: {exc}[/bold red]")
                    continue

                if not server_switches:
                    console.print(
                        "\n[bold yellow]⚠️ mcp_config.json 中没有可切换的 mcpServers。[/bold yellow]"
                    )
                    continue

                try:
                    switch_result = _interactive_switch_mcp_servers(server_switches)
                except Exception as exc:
                    log_error_traceback("main interactive mcp switch", exc)
                    console.print(
                        f"\n[bold red]❌ 打开 MCP 开关面板失败: {exc}[/bold red]"
                    )
                    continue

                if switch_result == "empty" or switch_result.get("action") == "cancel":
                    console.print(
                        "\n[bold yellow]↩️ 已取消本次 MCP 开关修改，配置文件未保存，运行中的服务状态保持不变。[/bold yellow]"
                    )
                    continue

                try:
                    apply_result = GLOBAL_MCP_MANAGER.apply_switches(
                        switch_result.get("disabled_updates", {})
                    )
                except Exception as exc:
                    log_error_traceback("main apply mcp switches", exc)
                    console.print(
                        f"\n[bold red]❌ 应用 MCP 开关变更失败: {exc}[/bold red]"
                    )
                    continue

                if not apply_result.get("saved"):
                    console.print(
                        f"\n[bold yellow]ℹ️ {apply_result.get('message', '没有检测到变更。')}[/bold yellow]"
                    )
                    continue

                changed = apply_result.get("changed", [])
                enabled = apply_result.get("enabled", [])
                disabled = apply_result.get("disabled", [])
                failed = apply_result.get("failed", [])

                summary_lines = [
                    "\n[bold green]✅ MCP 开关修改已保存到配置文件，并已尝试按变更增量启停服务。[/bold green]",
                    f"[dim]配置文件: {GLOBAL_MCP_MANAGER.get_status_info().get('config_path')}[/dim]",
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
                console.print("\n".join(summary_lines))
                continue

            if query == "/cmds":
                table = Table(
                    title="[bold cyan] 🛠️ 可用内置命令列表[/bold cyan]",
                    box=box.ROUNDED,
                    expand=True,
                )
                table.add_column("命令 (Command)", style="bold green", justify="left")
                table.add_column("描述 (Description)", style="white")
                for cmd, desc in COMMAND_DESCRIPTIONS.items():
                    table.add_row(cmd, desc)
                console.print(table)
                continue

            if query == "/skills-switch":
                status_text = SKILL_LOADER.toggle()
                refresh_system_prompt()
                history[0] = {"role": "system", "content": SYSTEM}
                status_style = "green" if SKILL_LOADER.is_enabled else "yellow"
                console.print(
                    f"\n[bold {status_style}]✨ Skills prompt catalog 状态已切换：{status_text}。[/bold {status_style}]"
                )
                console.print(
                    Panel(
                        Text(
                            SKILL_LOADER.render_prompt_block().strip(),
                            style="white",
                        ),
                        title="[bold cyan]Skills Catalog Status[/bold cyan]",
                        border_style="cyan",
                        box=box.ROUNDED,
                    )
                )
                continue

            if query == "/skills-list":
                skills_list_text = SKILL_LOADER.get_descriptions()
                console.print(
                    Panel(
                        Markdown(f"### 当前可用技能列表\n\n{skills_list_text}"),
                        title="[bold cyan]📚 Skills List[/bold cyan]",
                        border_style="cyan",
                        box=box.ROUNDED,
                    )
                )
                continue

            if query in ["/clear", "/reset"]:
                history = [{"role": "system", "content": SYSTEM}]
                CURRENT_CHECKPOINT = None
                console.print(
                    "\n[bold green] ✨ 对话历史已清空，开启全新会话！[/bold green]"
                )
                continue

            if query == "/compact":
                auto_compact(history, reason="User triggered compact")
                console.print(
                    "\n[bold green] ✨ 当前对话上下文已成功压缩并保存！[/bold green]"
                )
                CURRENT_CHECKPOINT = save_checkpoint(history, CURRENT_CHECKPOINT)
                continue

            if query == "/load":
                checkpoints = list_checkpoints()
                if not checkpoints:
                    console.print(
                        "\n[bold yellow] 📂 没有找到任何历史对话记录 (No checkpoints found).[/bold yellow]"
                    )
                    continue

                if len(history) > 1 and CURRENT_CHECKPOINT is None:
                    CURRENT_CHECKPOINT = save_checkpoint(history)

                try:
                    selected_path = _interactive_choose_checkpoint(checkpoints)
                except Exception as exc:
                    log_error_traceback("main interactive load checkpoint", exc)
                    selected_path = "abort"

                if selected_path == "abort":
                    console.print("[dim]已取消加载。[/dim]")
                    continue

                try:
                    history = load_checkpoint(Path(selected_path))
                    CURRENT_CHECKPOINT = Path(selected_path)

                    console.clear()
                    _render_startup_banner()
                    _render_env_customization_hint()
                    _render_history(history)

                    console.print(
                        f"\n[bold green] 🚀 成功加载对话记录！当前上下文包含 {len(history)} 条消息。[/bold green]"
                    )
                except Exception as exc:
                    log_error_traceback("main load checkpoint error", exc)
                    console.print(f"\n[bold red] ❌ 加载失败: {exc}[/bold red]")
                    continue

                task_plans = list_task_plans()
                if task_plans:
                    console.print(
                        "\n[bold cyan] 📋 发现保存的任务看板 (Task Plans)，是否要加载？[/bold cyan]"
                    )

                    try:
                        selected_task_path = _interactive_choose_checkpoint(
                            task_plans,
                            title="\n 📌 Select a Task Plan to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
                        )
                    except Exception as exc:
                        log_error_traceback("main interactive load task plan", exc)
                        selected_task_path = "abort"

                    if selected_task_path != "abort":
                        try:
                            plan_data = load_task_plan(Path(selected_task_path))
                            console.print(
                                "[bold green] 🚀 成功加载任务看板！[/bold green]"
                            )

                            has_incomplete = any(
                                task.get("status") != "completed"
                                for task in plan_data.get("tasks", {}).values()
                            )

                            if has_incomplete:
                                from utils.teams import (
                                    list_team_histories,
                                    load_team_history,
                                )

                                team_histories = list_team_histories()
                                if team_histories:
                                    console.print(
                                        "\n[bold cyan] 💡 发现子代理执行历史 (Team Histories)，是否要加载？[/bold cyan]"
                                    )

                                    try:
                                        selected_team_path = _interactive_choose_checkpoint(
                                            team_histories,
                                            title="\n 📌 Select a Team History to Load (Use ⬆ / ⬇ arrows, Enter to confirm):\n",
                                        )
                                    except Exception as exc:
                                        log_error_traceback(
                                            "main interactive load team history", exc
                                        )
                                        selected_team_path = "abort"

                                    if selected_team_path != "abort":
                                        try:
                                            load_team_history(Path(selected_team_path))
                                            console.print(
                                                "[bold green] ✅ 成功加载子代理执行历史！[/bold green]"
                                            )
                                        except Exception as exc:
                                            log_error_traceback(
                                                "main load team history error", exc
                                            )
                                            console.print(
                                                f"[bold red] ❌ 加载子代理执行历史失败: {exc}[/bold red]"
                                            )
                        except Exception as exc:
                            log_error_traceback("main load task plan error", exc)
                            console.print(
                                f"[bold red] ❌ 加载任务看板失败: {exc}[/bold red]"
                            )

                continue

            if query in COMMAND_DESCRIPTIONS:
                query = f"{query} {COMMAND_DESCRIPTIONS[query]}"
            history.append({"role": "user", "content": query})
            agent_loop(history)
    finally:
        GLOBAL_MCP_MANAGER.stop()
