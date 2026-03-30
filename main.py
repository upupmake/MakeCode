import concurrent.futures
import json
import time
from typing import Any

from init import MODEL, WORKDIR, llm_client, log_error_traceback
from rich.progress import Progress, TextColumn, BarColumn

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    PROMPT_TOOLKIT_AVAILABLE = True
except Exception as exc:
    log_error_traceback("main prompt_toolkit import", exc)
    PromptSession = None
    KeyBindings = None
    Keys = None
    Style = None
    HTML = None
    NoConsoleScreenBufferError = RuntimeError
    PROMPT_TOOLKIT_AVAILABLE = False

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
from utils.memory import micro_compact, MEMORY_TOOLS, MEMORY_TOOLS_HANDLERS, THRESHOLD, estimate_tokens

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
PROMPT_TOOLKIT_DISABLED = False

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
    if isinstance(output, str): return output
    return json.dumps(output, ensure_ascii=False, indent=2)


def _render_orchestrator_message(text: str):
    if not text: return
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
                    progress.set_description_str(f"Orchestrator thinking" + "." * ((phase % 10) + 1))
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


def _disable_prompt_toolkit(reason: str):
    global USER_SESSION, PROMPT_TOOLKIT_DISABLED
    USER_SESSION = None
    PROMPT_TOOLKIT_DISABLED = True
    if RICH_AVAILABLE: console.print(f"[yellow]⚠️ Input fallback: prompt_toolkit disabled ({reason}).[/yellow]")


def _init_user_session():
    global USER_SESSION
    if USER_SESSION is not None or PROMPT_TOOLKIT_DISABLED or not PROMPT_TOOLKIT_AVAILABLE: return
    try:
        user_kb = KeyBindings()

        @user_kb.add(Keys.Enter)
        def _submit_query(event):
            event.current_buffer.validate_and_handle()

        @user_kb.add('c-n')
        def _insert_newline(event):
            event.current_buffer.insert_text("\n")

        # 颜值升级 2: 优雅的多行延续提示符（灰色点阵）
        def prompt_continuation(width, line_number, is_soft_wrap):
            return " " * (width - 4) + " ┊  "

        # 颜值升级 3: 提示符颜色配置
        custom_style = Style.from_dict({
            'prompt': 'bold #00ff00',  # 鲜艳的绿色
            'arrow': '#00ffff bold',  # 青色箭头
        })

        USER_SESSION = PromptSession(
            multiline=True,
            key_bindings=user_kb,
            prompt_continuation=prompt_continuation,
            style=custom_style
        )
    except Exception as exc:
        log_error_traceback("main init user session", exc)
        _disable_prompt_toolkit(str(exc))


def _read_user_query() -> str:
    _init_user_session()

    if RICH_AVAILABLE:
        console.print(
            "\n[dim]💡 Tip: Press [bold]Enter[/bold] to send, [bold]Ctrl+N[/bold] for newline.[/dim]"
        )

    if USER_SESSION is not None:
        try:
            # 使用带有自定义样式的提示符
            return USER_SESSION.prompt([
                ('class:prompt', '🤖 User '),
                ('class:arrow', '❯❯ '),
            ])
        except NoConsoleScreenBufferError as exc:
            log_error_traceback("main user input no console buffer", exc)
            _disable_prompt_toolkit("No Windows console screen buffer")
        except Exception as exc:
            log_error_traceback("main user input prompt failure", exc)
            _disable_prompt_toolkit(str(exc))

    return input("\n\033[1;32m🤖 User ❯❯ \033[0m")


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

        history.append({"role": "user", "content": query})
        agent_loop(history)
