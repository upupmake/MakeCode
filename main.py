import concurrent.futures
import json
import sys
import time
from typing import Any
from pathlib import Path

from rich.progress import Progress, TextColumn, BarColumn

from init import WORKDIR, llm_client, log_error_traceback

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError as exc:
    log_error_traceback("main prompt_toolkit import", exc)
    print(
        "\n\033[31mError: prompt_toolkit is required but not installed. Please install it using `pip install prompt_toolkit`.\033[0m")
    sys.exit(1)

try:
    from tqdm import tqdm

    TQDM_AVAILABLE = True
except Exception as exc:
    log_error_traceback("main tqdm import", exc)
    tqdm = None
    TQDM_AVAILABLE = False

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
    from rich import box

    RICH_AVAILABLE = True
except Exception as exc:
    log_error_traceback("main rich import", exc)
    Console = None
    Markdown = None
    Panel = None
    Syntax = None
    Text = None
    box = None
    RICH_AVAILABLE = False

from utils.common import COMMON_TOOLS, COMMON_TOOLS_HANDLERS, STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE
from utils.skills import SKILL_TOOLS, SKILL_TOOLS_HANDLERS
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS
from utils.teams import TEAM_TOOLS_HANDLERS, TEAM_TOOLS
from utils.memory import micro_compact, MEMORY_TOOLS, MEMORY_TOOLS_HANDLERS, THRESHOLD, estimate_tokens, save_checkpoint, list_checkpoints, load_checkpoint

console = Console() if RICH_AVAILABLE else None
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

SYSTEM = f"""You are the Orchestrator (Super-Agent) at {WORKDIR}.

Core operating policy:
1) Always plan work with TaskManager first.
2) Before any delegation, call GetRunnableTasks to obtain the current runnable frontier.
3) DelegateTasks is ONLY for runnable tasks from the latest GetRunnableTasks result.
4) After each delegation batch, re-check task state (GetTaskTable/GetRunnableTasks) and continue until done.

Execution guidance:
- Prefer parallel delegation for independent runnable tasks.
- Keep tool calls explicit and deterministic; avoid speculative actions.
- Sub-agents are stateless across delegated runs. Every DelegateTasks item must include complete, self-contained context_prompt (goal, constraints, relevant files/context, expected output/evidence).
- During topology planning and delegation, avoid assigning tasks that may write the same file into the same runnable batch.
- For tasks touching the same file, enforce dependency order in TaskManager (topological sequence) before delegation.
- For workspace file operations (reading, writing, editing, or text searching), strictly use the File namespace tools (RunRead, RunWrite, RunEdit, RunGrep). Do NOT use terminal commands for these tasks.
- RunWrite is only for creating and writing NEW files.
- For editing existing files, you MUST call RunRead first to confirm current content, then use RunEdit.
- For terminal/CLI tasks, use RunTerminalCommand directly.
  - Runtime terminal is fixed at startup: {STARTUP_TERMINAL_LABEL} (source={STARTUP_TERMINAL_SOURCE}).
- Final answers should summarize: completed tasks, remaining tasks, and next runnable tasks.
"""

SUPER_TOOLS = llm_client.format_tools(COMMON_TOOLS + SKILL_TOOLS + MEMORY_TOOLS + TASK_MANAGER_TOOLS + TEAM_TOOLS)

SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **MEMORY_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
}


def _extract_message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = [part["text"] for part in content if isinstance(part, dict) and part.get("text")]
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
    if RICH_AVAILABLE:
        console.print(
            Panel(
                Markdown(text),
                title="[bold magenta]🧠 Orchestrator[/bold magenta]",
                border_style="magenta",
                box=box.ROUNDED,
                padding=(1, 2)
            )
        )
    else:
        print(f"\n\033[35m[🧠 Orchestrator]:\n{text}\033[0m\n")


def _render_tool_call(name: str, arguments: Any):
    if RICH_AVAILABLE:
        body = Syntax(json.dumps(arguments, ensure_ascii=False, indent=2), "json", word_wrap=True,
                      theme="monokai") if isinstance(arguments, (dict, list)) else Text(str(arguments))
        console.print(
            Panel(body, title=f"[bold cyan]🛠️  Action: {name}[/bold cyan]", border_style="cyan", box=box.ROUNDED))
    else:
        print(f"\033[36m[🛠️  Action]: {name} -> {arguments}\033[0m")


def _render_tool_output(name: str, output: Any):
    if RICH_AVAILABLE:
        text = _stringify_output(output).strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                body = Syntax(json.dumps(parsed, ensure_ascii=False, indent=2), "json", word_wrap=True, theme="monokai")
            except json.JSONDecodeError as exc:
                log_error_traceback("main render tool output json decode", exc)
                body = Text(text)
        else:
            body = Text(text, style="dim")
        console.print(
            Panel(body, title=f"[bold green]✅ Result: {name}[/bold green]", border_style="green", box=box.ROUNDED))
    else:
        print(f"\033[32m[✅ Result] {name}: {_stringify_output(output)}\033[0m")


def _request_with_progress(messages: list):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            llm_client.generate,
            messages=messages,
            tools=SUPER_TOOLS,
        )

        # 颜值升级 1: 使用 rich 的优雅 status 动画
        if RICH_AVAILABLE:
            with Progress(
                    BarColumn(bar_width=30),  # 在这里修改你想要的宽度！
                    TextColumn("[bold cyan]✨ Orchestrator is thinking..."),
                    transient=True,  # 任务完成后自动隐藏加载条，类似 console.status
                    console=console
            ) as progress:
                # total=None 表示进度未知，会触发左右来回弹跳的动画
                progress.add_task("", total=None)
                return future.result()
        elif TQDM_AVAILABLE:
            with tqdm(total=None, bar_format="{desc}", leave=False, dynamic_ncols=True) as progress:
                phase = 0
                while not future.done():
                    progress.set_description_str("Orchestrator thinking" + "." * ((phase % 10) + 1))
                    progress.refresh()
                    time.sleep(0.12)
                    phase += 1
            return future.result()
        else:
            print("Orchestrator thinking...", end="", flush=True)
            while not future.done():
                time.sleep(0.5)
                print(".", end="", flush=True)
            print()
            return future.result()


def _render_startup_banner():
    subtitle = f"Terminal Environment: [bold]{STARTUP_TERMINAL_LABEL}[/bold] (source={STARTUP_TERMINAL_SOURCE})"
    if RICH_AVAILABLE:
        console.print(
            Panel(
                Text(MAKECODE_ASCII.strip("\n"), style="bold bright_blue"),
                title="[bold white]MakeCode Agent[/bold white]",
                border_style="bright_blue",
                box=box.DOUBLE_EDGE,
                subtitle=subtitle,
                subtitle_align="center",
                padding=(1, 4)
            )
        )
    else:
        print("\033[96m" + MAKECODE_ASCII.strip("\n") + "\033[0m")
        print(f"\033[90m{subtitle}\033[0m")


def _render_env_customization_hint():
    hint_text = (
        "💡下次启动前可通过环境变量自定义模型：\n"
        "MODEL_ID=xxx\nOPENAI_BASE_URL=xxx\nOPENAI_API_KEY=xxx"
    )
    if RICH_AVAILABLE:
        console.print(
            Panel(
                Text(hint_text, style="bold yellow"),
                title="[bold yellow]环境变量提示[/bold yellow]",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
    else:
        lines = [
            "┌─────────────────────────────────────────────────────────────┐",
            "│💡下次启动前可通过环境变量自定义模型：                           │",
            "│  MODEL_ID=xxx                                               │",
            "│  OPENAI_BASE_URL=xxx                                        │",
            "│  OPENAI_API_KEY=xxx                                         │",
            "└─────────────────────────────────────────────────────────────┘",
        ]
        print("\n" + "\n".join(lines) + "\n")


COMMAND_DESCRIPTIONS = {
    "/cmds": "列出所有的可用命令和功能描述",
    "/load": "列出历史checkpoint并选择加载",
    "/skills": "列出当前可用的skills",
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
    "/exit": "退出程序"
}

class SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/'):
            for cmd, desc in COMMAND_DESCRIPTIONS.items():
                if cmd.startswith(text):
                    # display_meta 可以在补全菜单右侧漂亮地显示中文描述
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
            
            # 处理斜杠命令的自动补全逻辑
            if text.startswith('/'):
                # 如果当前输入的已经是完整的内置命令，直接提交
                if text in COMMAND_DESCRIPTIONS:
                    buffer.validate_and_handle()
                    return
                
                # 如果命令不完整，但补全菜单中有匹配项
                if buffer.complete_state and buffer.complete_state.completions:
                    if buffer.complete_state.current_completion:
                        # 场景1：用户用上下键明确选中了某一项
                        buffer.apply_completion(buffer.complete_state.current_completion)
                    else:
                        # 场景2：用户只敲了 /sk 就按回车，自动帮他补全成第一项 (/skills)
                        buffer.apply_completion(buffer.complete_state.completions[0])
                    return

            # 常规对话输入或完整命令，直接提交给大模型
            buffer.validate_and_handle()

        @user_kb.add('c-n')
        def _insert_newline(event):
            event.current_buffer.insert_text("\n")

        def prompt_continuation(width, line_number, is_soft_wrap):
            return " " * (width - 4) + " ┊  "

        custom_style = Style.from_dict({
            'prompt': 'bold #00ff00',
            'arrow': '#00ffff bold',
        })

        USER_SESSION = PromptSession(
            multiline=True,
            key_bindings=user_kb,
            prompt_continuation=prompt_continuation,
            style=custom_style,
            completer=command_completer,
            reserve_space_for_menu=5,
            complete_while_typing=True
        )
    except Exception as exc:
        log_error_traceback("main init user session", exc)
        print(f"\n\033[31mError initializing prompt session: {exc}\033[0m")
        sys.exit(1)


def _read_user_query() -> str:
    _init_user_session()

    if RICH_AVAILABLE:
        console.print(
            "\n[dim]💡 Tip: Press [bold]Enter[/bold] to send, [bold]Ctrl+N[/bold] for newline.[/dim]"
        )

    try:
        return USER_SESSION.prompt([
            ('class:prompt', '🤖 User '),
            ('class:arrow', '❯❯ '),
        ])
    except Exception as exc:
        log_error_traceback("main user input prompt failure", exc)
        raise


def agent_loop(messages: list):
    while True:
        micro_compact(messages)
        try:
            response = _request_with_progress(messages)
        except Exception as e:
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"Error during agent execution: {e}. Check .makecode/error.log for details."
            if RICH_AVAILABLE:
                console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            else:
                print(f"\033[31m⚠️ {error_msg}\033[0m")
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
                handler = SUPER_TOOLS_HANDLERS.get(tool_name)
                if handler:
                    output = handler(messages, **arguments) if tool_name == "Compact" else handler(**arguments)
                else:
                    output = f"Unknown tool: {tool_name}"
            except Exception as e:
                log_error_traceback(f"Orchestrator tool execution error: {tool_name}", e)
                output = f"Error executing {tool_name}: {e}. Check .makecode/error.log for details."

            _render_tool_output(tool_name, output)

            messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

        if not has_tool_call:
            break

    if estimate_tokens(messages) > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{estimate_tokens(messages)} exceeded threshold {THRESHOLD}."
        )
        try:
            output = SUPER_TOOLS_HANDLERS["Compact"](messages, reason=compact_reason)
            _render_tool_output("Compact", output)
        except Exception as e:
            log_error_traceback("Orchestrator auto-compact error", e)
            error_msg = f"Error executing Compact: {e}. Check .makecode/error.log for details."
            if RICH_AVAILABLE:
                console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            else:
                print(f"\033[31m⚠️ {error_msg}\033[0m")


def _interactive_choose_checkpoint(checkpoints: list) -> str:
    if not checkpoints:
        return "abort"
    
    options = []
    for cp in checkpoints:
        # cp is a Path object
        parts = cp.stem.split("_")
        uid = parts[-1] if len(parts) >= 4 else cp.name
        
        # 使用文件的最后修改时间
        mtime = cp.stat().st_mtime
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        
        desc = f"Checkpoint {uid} (Last updated: {date_str})"
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
        result = [("class:title", "\n📌 Select a Checkpoint to Load (Use ⬆/⬇ arrows, Enter to confirm):\n")]
        for i, (key, text) in enumerate(options):
            if i == selected_index[0]:
                result.append(("class:selected", f"  👉 {text}\n"))
            else:
                result.append(("class:unselected", f"     {text}\n"))
        return result

    control = FormattedTextControl(get_formatted_text)
    window = Window(content=control, height=len(options) + 2)
    layout = Layout(window)

    style = Style([
        ("title", "fg:ansicyan bold"),
        ("selected", "fg:ansigreen bold"),
        ("unselected", "fg:ansigray"),
    ])

    app = Application(layout=layout, key_bindings=kb, style=style, erase_when_done=True)
    return app.run()

CURRENT_CHECKPOINT = None

if __name__ == '__main__':
    _render_startup_banner()
    _render_env_customization_hint()
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = _read_user_query()
        except (EOFError, KeyboardInterrupt) as exc:
            log_error_traceback("main user input interrupted", exc)
            if RICH_AVAILABLE:
                console.print("\n[bold yellow]👋 Exiting MakeCode Agent. Goodbye![/bold yellow]")
            else:
                print("\n\033[33m👋 Exiting MakeCode Agent. Goodbye!\033[0m")
            break

        query = query.strip()
        if not query:
            continue
            
        if query in ["/quit", "/exit"]:
            if RICH_AVAILABLE:
                console.print("\n[bold yellow]👋 Exiting MakeCode Agent. Goodbye![/bold yellow]")
            else:
                print("\n\033[33m👋 Exiting MakeCode Agent. Goodbye!\033[0m")
            break

        if query == "/cmds":
            if RICH_AVAILABLE:
                from rich.table import Table
                table = Table(title="[bold cyan]🛠️ 可用内置命令列表[/bold cyan]", box=box.ROUNDED, expand=True)
                table.add_column("命令 (Command)", style="bold green", justify="left")
                table.add_column("描述 (Description)", style="white")
                for cmd, desc in COMMAND_DESCRIPTIONS.items():
                    table.add_row(cmd, desc)
                console.print(table)
            else:
                print("\n\033[36m🛠️ 可用内置命令列表:\033[0m")
                for cmd, desc in COMMAND_DESCRIPTIONS.items():
                    print(f"  \033[32m{cmd:<12}\033[0m - {desc}")
                print()
            continue

        if query in ["/clear", "/reset"]:
            history = [{"role": "system", "content": SYSTEM}]
            CURRENT_CHECKPOINT = None
            if RICH_AVAILABLE:
                console.print("\n[bold green]✨ 对话历史已清空，开启全新会话！[/bold green]")
            else:
                print("\n\033[32m✨ 对话历史已清空，开启全新会话！\033[0m")
            continue

        if query == "/load":
            checkpoints = list_checkpoints()
            if not checkpoints:
                if RICH_AVAILABLE:
                    console.print("\n[bold yellow]📂 没有找到任何历史对话记录 (No checkpoints found).[/bold yellow]")
                else:
                    print("\n\033[33m📂 没有找到任何历史对话记录 (No checkpoints found).\033[0m")
                continue

            # 如果已经在对话中(除去system prompt之外有其他内容)，并且当前还没有绑定任何 checkpoint，确保它被保存
            if len(history) > 1 and CURRENT_CHECKPOINT is None:
                CURRENT_CHECKPOINT = save_checkpoint(history)
            
            try:
                selected_path = _interactive_choose_checkpoint(checkpoints)
            except Exception as exc:
                log_error_traceback("main interactive load checkpoint", exc)
                selected_path = "abort"

            if selected_path == "abort":
                if RICH_AVAILABLE:
                    console.print("[dim]已取消加载。[/dim]")
                else:
                    print("\033[90m已取消加载。\033[0m")
                continue

            try:
                history = load_checkpoint(Path(selected_path))
                CURRENT_CHECKPOINT = Path(selected_path)
                if RICH_AVAILABLE:
                    console.print(f"\n[bold green]🚀 成功加载对话记录！当前上下文包含 {len(history)} 条消息。[/bold green]")
                else:
                    print(f"\n\033[32m🚀 成功加载对话记录！当前上下文包含 {len(history)} 条消息。\033[0m")
            except Exception as exc:
                log_error_traceback("main load checkpoint error", exc)
                if RICH_AVAILABLE:
                    console.print(f"\n[bold red]❌ 加载失败: {exc}[/bold red]")
                else:
                    print(f"\n\033[31m❌ 加载失败: {exc}\033[0m")
            continue

        # 核心逻辑：如果大模型需要处理软命令，把它和描述拼接在一起作为上下文
        if query in COMMAND_DESCRIPTIONS:
            query = f"{query} {COMMAND_DESCRIPTIONS[query]}"
        history.append({"role": "user", "content": query})
        agent_loop(history)
        CURRENT_CHECKPOINT = save_checkpoint(history, CURRENT_CHECKPOINT)
