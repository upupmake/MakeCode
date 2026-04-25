import json
import sys
import threading
import time
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule

from init import WORKDIR, log_error_traceback, STARTUP_TERMINAL_SOURCE, STARTUP_TERMINAL_TYPE
from prompts import get_orchestrator_system_prompt, get_title_generation_system_prompt
# 导入命令模块
from system.commands import (
    COMMAND_DESCRIPTIONS,
    SlashCommandCompleter,
    CommandHandler,
    CommandAction,
)
from system.console_render import (
    _render_tool_call,
    _render_tool_output,
    toggle_sub_agent_console,
    _render_history,
    _render_token_usage,
    _render_startup_banner,
    _render_env_customization_hint,
    console,
)
from utils.hitl import get_hitl_status
from system.models import get_current_model_config
from system.stream_render import StreamRenderer
from system.ts_validator import init_ts_cache
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    run_edit,
    run_read,
    run_write,
)
from utils.file_access import AgentFileAccess
from utils.llm_client import llm_client
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.memory import (
    THRESHOLD,
    auto_compact,
    estimate_tokens,
    list_checkpoints,
    load_checkpoint,
    micro_compact,
    rename_checkpoint_with_title,
    save_checkpoint,
)
from utils.skills import SKILL_LOADER, SKILL_TOOLS, SKILL_TOOLS_HANDLERS
import utils.tasks as _tasks_module
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS
from utils.teams import TEAM, TEAM_TOOLS, TEAM_TOOLS_HANDLERS

STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"

USER_SESSION = None


def get_dynamic_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        WORKDIR,
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
    )


def get_current_tools_definition():
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    try:
        return llm_client.format_tools(
            COMMON_TOOLS
            + SKILL_TOOLS
            + TASK_MANAGER_TOOLS
            + TEAM_TOOLS
            + GLOBAL_MCP_MANAGER.get_tools()
        )
    except RuntimeError as exc:
        if "No model configured" in str(exc):
            return []
        raise


def get_base_super_tools():
    """获取基础工具定义"""
    try:
        return llm_client.format_tools(
            COMMON_TOOLS + SKILL_TOOLS + TASK_MANAGER_TOOLS + TEAM_TOOLS
        )
    except RuntimeError as exc:
        if "No model configured" in str(exc):
            return []
        raise


orchestrator_access = AgentFileAccess()

BASE_SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
    "RunRead": lambda path, regions, **kwargs: run_read(
        path, regions, orchestrator_access
    ),
    "RunWrite": lambda path, content, **kwargs: run_write(
        path, content, orchestrator_access
    ),
    "RunEdit": lambda path, edits, **kwargs: run_edit(
        path, edits, orchestrator_access
    ),
}


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


def generate_title(user_query: str) -> str:
    """Generate a short title for the conversation based on the first user query."""
    try:
        messages = [
            {"role": "system", "content": get_title_generation_system_prompt()},
            {"role": "user", "content": user_query},
        ]
        response = llm_client.generate(messages)
        # Parse response based on client type
        if hasattr(response, 'choices'):  # Chat API
            return response.choices[0].message.content.strip()
        else:  # Response API
            for item in response.output:
                if item.type == "message":
                    return next(
                        (c.text for c in item.content if c.type == "output_text"), ""
                    ).strip()
    except Exception as exc:
        log_error_traceback("Failed to generate title", exc)
    return None


def _stream_with_render(messages: list, current_tools: list):
    """
    优化的流式请求渲染：
    1. 思考阶段：使用原生 append 模式流式输出，配合 dim 样式，极致性能无闪烁。
    2. 正文阶段：采用带『节流 (Throttle)』的 Live + Markdown 实时渲染。
    """
    renderer = StreamRenderer(console=console, update_interval=0.1)
    stream = llm_client.generate_stream(messages, current_tools)
    text_content, tool_calls, raw_message = renderer.render(stream, agent_name="Orchestrator")

    return text_content, tool_calls, raw_message


def _is_no_model_configured_error(exc: Exception) -> bool:
    return "No model configured" in str(exc)


def agent_loop(messages: list):
    """Agent 主循环：与 LLM 交互并执行工具调用"""
    global CURRENT_CHECKPOINT
    micro_compact(messages)
    current_handlers = {
        **BASE_SUPER_TOOLS_HANDLERS,
        **GLOBAL_MCP_MANAGER.get_handlers(),
    }
    current_super_tools = []

    while True:
        try:
            current_super_tools = get_current_tools_definition()
        except RuntimeError as exc:
            if _is_no_model_configured_error(exc):
                console.print(
                    "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
                )
                break
            raise
        _render_token_usage(
            messages,
            tools_definition=current_super_tools,
            system_prompt=get_dynamic_system_prompt(),
            threshold=THRESHOLD,
            estimate_tokens_fn=estimate_tokens,
        )

        try:
            text_content, tool_calls, raw_message = _stream_with_render(messages, current_super_tools)
        except Exception as e:
            if _is_no_model_configured_error(e):
                console.print(
                    "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
                )
                break
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"智能体执行出错: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            break

        llm_client.append_assistant_message(messages, raw_message)
        has_tool_call = len(tool_calls) > 0

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]

            _render_tool_call(tool_name, tool_args)

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
        _apply_pending_title()

        if not has_tool_call:
            break

    current_context_tokens = estimate_tokens(
        messages, tools_definition=current_super_tools, system_prompt=get_dynamic_system_prompt()
    )
    if current_context_tokens > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{current_context_tokens} exceeded threshold {THRESHOLD}."
        )
        try:
            auto_compact(messages, reason=compact_reason)
            console.print(
                "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
        except Exception as e:
            log_error_traceback("Orchestrator auto-compact error", e)
            error_msg = f"Error executing auto_compact: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")


def _init_tree_sitter_cache(console: Console):
    """初始化 tree-sitter 语言包缓存"""
    try:
        init_ts_cache()
    except Exception as e:
        console.print(f"[red]⚠️ 语法解析器初始化失败: {e}[/red]")


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
            return " " * (width - 4) + " ┃  "

        custom_style = Style.from_dict(
            {
                "prompt": "bold #00ff00",
                "arrow": "#00ffff bold",
                "bottom_toolbar": "bg:#3c3c3c fg:#e0e0e0",
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
            HTML(f"\n<ansired>初始化提示会话失败: {exc}</ansired>")
        )
        sys.exit(1)


def _read_user_query(messages: list = None) -> str:
    _init_user_session()

    console.print(
        "\n[dim]💡 Tips：按 [bold]Enter[/bold] 发送消息，按 [bold]Ctrl+N[/bold] 换行。[/dim]"
    )

    # 将 rprompt 变量名改为 bottom_toolbar
    bottom_toolbar_content = None
    if messages is not None:
        tokens = estimate_tokens(
            messages,
            tools_definition=get_current_tools_definition(),
            system_prompt=get_dynamic_system_prompt(),
        )
        pct = (tokens / THRESHOLD) * 100
        color = "ansigreen" if pct < 70 else "ansiyellow" if pct < 90 else "ansired"
        # 组装 toolbar 内容
        _tb_bg = "bg:#1a1a2e"
        bottom_toolbar_content = [(f"{_tb_bg} fg:{color} bold", f" 📈 Tokens: {tokens}/{THRESHOLD} ({pct:.1f}%) ")]

        # 追加当前模型信息
        current_model = get_current_model_config()
        if current_model:
            model_text = current_model.get_display_text()
            bottom_toolbar_content.append((f"{_tb_bg} fg:#e0e0e0 bold", f" 🤖 Model: {model_text} "))

        # 追加 HITL 状态
        hitl_on = get_hitl_status()
        hitl_color = "ansigreen" if hitl_on else "ansired"
        hitl_text = "ON" if hitl_on else "OFF"
        bottom_toolbar_content.append((f"{_tb_bg} fg:{hitl_color} bold", f" 🛡️ HITL: {hitl_text} "))

    try:
        with patch_stdout():
            return USER_SESSION.prompt(
                [
                    ("class:prompt", "🤖 User "),
                    ("class:arrow", "❯❯ "),
                ],
                # 原来的 rprompt=rprompt 替换为 bottom_toolbar
                bottom_toolbar=bottom_toolbar_content,
            )
    except Exception as exc:
        log_error_traceback("main user input prompt failure", exc)
        raise


CURRENT_CHECKPOINT = None
_pending_title = None


def _apply_pending_title():
    """Apply a pending title that was generated in the background.

    Called synchronously from the main thread (agent_loop) after each
    save_checkpoint to avoid race conditions with file I/O.
    """
    global _pending_title, CURRENT_CHECKPOINT
    if _pending_title is None or CURRENT_CHECKPOINT is None:
        if _pending_title is not None and CURRENT_CHECKPOINT is None:
            _pending_title = None  # checkpoint was reset — discard pending title
        return
    title = _pending_title
    _pending_title = None
    try:
        new_ckpt = rename_checkpoint_with_title(CURRENT_CHECKPOINT, title)
        if new_ckpt != CURRENT_CHECKPOINT:
            CURRENT_CHECKPOINT = new_ckpt
        _tasks_module.TASK_MANAGER.rename_with_title(title)
        TEAM.rename_history_with_title(title)
    except Exception as exc:
        log_error_traceback("Failed to apply pending title", exc)


if __name__ == "__main__":
    _render_startup_banner()
    _render_env_customization_hint()

    # 初始化 tree-sitter 语言包缓存
    _init_tree_sitter_cache(console)

    GLOBAL_MCP_MANAGER.initialize(console=console)
    GLOBAL_MCP_MANAGER.start_background()

    history = [{"role": "system", "content": get_dynamic_system_prompt()}]

    command_handler = CommandHandler(
        console=console,
        mcp_manager=GLOBAL_MCP_MANAGER,
        skill_loader=SKILL_LOADER,
        get_system_prompt_fn=get_dynamic_system_prompt,
        save_checkpoint_fn=save_checkpoint,
        load_checkpoint_fn=load_checkpoint,
        list_checkpoints_fn=list_checkpoints,
        auto_compact_fn=auto_compact,
    )

    try:
        while True:
            try:
                query = _read_user_query(history)
            except (EOFError, KeyboardInterrupt) as exc:
                log_error_traceback("main user input interrupted", exc)
                console.print(
                    "\n[bold yellow]👋 正在退出 MakeCode CLI。再见！[/bold yellow]"
                )
                break

            query = query.strip()
            if not query:
                continue

            # 处理命令
            command_result = command_handler.process_command(
                query=query,
                history=history,
                current_checkpoint=CURRENT_CHECKPOINT,
                render_banner_fn=_render_startup_banner,
                render_hint_fn=_render_env_customization_hint,
                render_history_fn=_render_history,
            )

            if command_result.action == CommandAction.EXIT:
                break
            elif command_result.action == CommandAction.CONTINUE:
                continue
            elif command_result.action == CommandAction.RUN_AGENT:
                history.append({"role": "user", "content": command_result.payload})
                
                # Generate title on first user message (parallel with agent_loop)
                if CURRENT_CHECKPOINT is None and any(msg['role'] == 'user' for msg in history):
                    # Save initial checkpoint without title — fast, no blocking
                    CURRENT_CHECKPOINT = save_checkpoint(history)

                    # Kick off title generation in a background thread
                    def _title_worker():
                        global _pending_title
                        try:
                            title = generate_title(query)
                            if title:
                                _pending_title = title
                        except Exception as exc:
                            log_error_traceback("Failed to generate title", exc)

                    _title_thread = threading.Thread(target=_title_worker, daemon=True)
                    _title_thread.start()
                else:
                    _title_thread = None
                
                try:
                    agent_loop(history)
                except RuntimeError as exc:
                    console.print(f"[bold yellow]⚠️ {exc}[/bold yellow]")
                # Wait for title generation to finish, then apply rename
                if _title_thread is not None:
                    _title_thread.join(timeout=10)
                _apply_pending_title()
            elif command_result.action == CommandAction.RESET_CHECKPOINT:
                CURRENT_CHECKPOINT = None
                _pending_title = None
            elif command_result.action == CommandAction.LOAD_HISTORY:
                history, CURRENT_CHECKPOINT = command_result.payload
                _pending_title = None
            elif command_result.action == CommandAction.UPDATE_CHECKPOINT:
                CURRENT_CHECKPOINT = command_result.payload
            elif command_result.action == CommandAction.UPDATE_SYSTEM_PROMPT:
                history[0] = {"role": "system", "content": command_result.payload}
    finally:
        GLOBAL_MCP_MANAGER.stop()
