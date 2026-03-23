import concurrent.futures
import json
import time
from typing import Any

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    PROMPT_TOOLKIT_AVAILABLE = True
except Exception:
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
except Exception:
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
except Exception:
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
from utils.memory import micro_compact, MEMORY_TOOLS, MEMORY_TOOLS_HANDLERS

from init import client, MODEL, WORKDIR

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
- For file operations, always prefer File namespace tools first.
- For terminal/CLI tasks, use RunTerminalCommand directly.
  - Runtime terminal is fixed at startup: {STARTUP_TERMINAL_LABEL} (source={STARTUP_TERMINAL_SOURCE}).
- Final answers should summarize: completed tasks, remaining tasks, and next runnable tasks.
"""

SUPER_TOOLS = COMMON_TOOLS + SKILL_TOOLS + MEMORY_TOOLS + TASK_MANAGER_TOOLS + TEAM_TOOLS

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


def _parse_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str): return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


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
            except json.JSONDecodeError:
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
            client.responses.create,
            model=MODEL,
            input=messages,
            tools=SUPER_TOOLS,
        )

        # 颜值升级 1: 使用 rich 的优雅 status 动画
        if RICH_AVAILABLE:
            with console.status("[bold cyan]✨ Orchestrator is thinking...", spinner="bouncingBar"):
                return future.result()
        elif TQDM_AVAILABLE:
            with tqdm(total=None, bar_format="{desc}", leave=False, dynamic_ncols=True) as progress:
                phase = 0
                while not future.done():
                    progress.set_description_str(f"Orchestrator thinking" + "." * ((phase % 6) + 1))
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
        except NoConsoleScreenBufferError:
            _disable_prompt_toolkit("No Windows console screen buffer")
        except Exception as exc:
            _disable_prompt_toolkit(str(exc))

    return input("\n\033[1;32m🤖 User ❯❯ \033[0m")


def agent_loop(messages: list):
    while True:
        micro_compact(messages)
        try:
            response = _request_with_progress(messages)
        except Exception as e:
            error_msg = f"Error during agent execution: {e}"
            if RICH_AVAILABLE:
                console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            else:
                print(f"\033[31m⚠️ {error_msg}\033[0m")
            break

        new_msgs = [item.model_dump(exclude_none=True) if hasattr(item, 'model_dump') else dict(item) for item in
                    response.output]
        messages.extend(new_msgs)

        has_tool_call = False
        for msg in new_msgs:
            if msg.get("type") == "function_call":
                has_tool_call = True
                _render_tool_call(msg.get("name"), _parse_arguments(msg.get("arguments")))
            else:
                _render_orchestrator_message(_extract_message_text(msg))

        for item in response.output:
            if item.type == "function_call":
                try:
                    arguments = _parse_arguments(item.arguments)
                    handler = SUPER_TOOLS_HANDLERS.get(item.name)
                    if handler:
                        output = handler(messages) if item.name == "Compact" else handler(**arguments)
                    else:
                        output = f"Unknown tool: {item.name}"
                except Exception as e:
                    output = f"Error executing {item.name}: {e}"

                _render_tool_output(item.name, output)

                messages.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
                })

        if not has_tool_call:
            break


if __name__ == '__main__':
    _render_startup_banner()
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = _read_user_query()
        except (EOFError, KeyboardInterrupt):
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
