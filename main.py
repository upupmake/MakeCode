import json
import sys
import threading
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from init import (
    log_error_traceback,
    resolve_chosen_workdir,
    resolve_startup_workdir,
    set_workdir,
    should_prompt_for_workdir,
    STARTUP_TERMINAL_SOURCE,
    STARTUP_TERMINAL_TYPE,
)
from prompts import get_orchestrator_system_prompt, get_title_generation_system_prompt
# 导入命令模块
from system.commands import (
    CommandHandler,
    CommandAction,
)
from system.console_render import (
    _render_tool_call,
    _render_tool_output,
    _render_history,
    _render_token_usage,
    _render_startup_banner,
    _render_env_customization_hint,
    render_current_workdir,
    render_current_task_plan,
    format_runtime_info,
    console,
)
from system.updater import check_update, launch_updater
from utils.plan_mode import (
    is_plan_mode,
    is_plan_mode_command_allowed,
    PLAN_MODE_BLOCKLIST,
    PLAN_MODE_ALLOWED_COMMANDS,
)
from system.stream_render import StreamRenderer
from system.ts_validator import init_ts_cache
from system.tui_app import MakeCodeTuiApp, post_tui, TuiRegion, set_agent_loop_active, refresh_status, refresh_tools_title
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    file_edit,
    file_read,
    file_create,
)
from utils.file_access import AgentFileAccess
from utils.llm_client import llm_client
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils import paths
from utils.memory import (
    THRESHOLD,
    auto_compact,
    estimate_tokens,
    get_active_memory_count,
    list_checkpoints,
    load_checkpoint,
    micro_compact,
    rename_checkpoint_with_title,
    save_checkpoint,
)
from utils.skills import SKILL_LOADER, SKILL_TOOLS, SKILL_TOOLS_HANDLERS
import utils.tasks as _tasks_module
import utils.teams as _teams_module
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS
from utils.teams import TEAM_TOOLS, TEAM_TOOLS_HANDLERS
from tools.ask_user import ASK_USER_TOOLS, ASK_USER_TOOLS_HANDLERS

STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"

_PENDING_UPDATE_EXE_PATH = None


def _current_workdir():
    return paths.workdir()


def get_dynamic_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        str(_current_workdir()),
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
        plan_mode=is_plan_mode(),
    )


def get_current_tools_definition():
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    all_tools = _get_all_tools_definition()
    if is_plan_mode():
        # Plan Mode: 黑名单过滤，禁止写入/执行/委托工具
        filtered = [t for t in all_tools if t["function"]["name"] not in PLAN_MODE_BLOCKLIST]
        return filtered
    return all_tools


def _get_all_tools_definition():
    """获取全部工具定义（不考虑 Plan Mode 过滤）"""
    try:
        return llm_client.format_tools(
            COMMON_TOOLS
            + SKILL_TOOLS
            + TASK_MANAGER_TOOLS
            + TEAM_TOOLS
            + ASK_USER_TOOLS
            + GLOBAL_MCP_MANAGER.get_tools()
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
    **ASK_USER_TOOLS_HANDLERS,
    "FileRead": lambda path, regions, **kwargs: file_read(
        path, regions, orchestrator_access
    ),
    "FileCreate": lambda path, content, **kwargs: file_create(
        path, content, orchestrator_access
    ),
    "FileEdit": lambda path, edits, **kwargs: file_edit(
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
            parsed = json.loads(payload, strict=False)
        except json.JSONDecodeError as exc:
            log_error_traceback("main parse arguments json decode", exc)
            return {"_error": f"Failed to parse tool arguments: {exc}. Raw: {payload[:200]}"}
        if isinstance(parsed, dict):
            return parsed
        log_error_traceback(
            "main parse arguments type mismatch",
            ValueError(f"Expected dict, got {type(parsed).__name__}"),
        )
        return {"_error": f"Tool arguments parsed to {type(parsed).__name__}, expected dict. Raw: {payload[:200]}"}
    log_error_traceback(
        "main parse arguments unexpected type",
        TypeError(f"Unexpected type: {type(arguments).__name__}"),
    )
    return {"_error": f"Unexpected arguments type: {type(arguments).__name__}"}


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
    流式请求渲染：
    1. reasoning 和正文均交给 StreamRenderer 处理。
    2. StreamRenderer 负责按完整 Markdown 段落增量输出，并返回工具调用信息。
    """
    from system.stream_cancel import start_cancel_listener, stop_cancel_listener, is_cancelled

    renderer = StreamRenderer(console=console, update_interval=0.1)
    start_cancel_listener()
    try:
        stream = llm_client.generate_stream(messages, current_tools)
        text_content, tool_calls, raw_message = renderer.render(stream, agent_name="Orchestrator")
        cancelled = is_cancelled()
    finally:
        stop_cancel_listener()

    return text_content, tool_calls, raw_message, cancelled


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
        # Update system prompt to reflect current plan mode state
        messages[0] = {"role": "system", "content": get_dynamic_system_prompt()}

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
            threshold=THRESHOLD,
            estimate_tokens_fn=estimate_tokens,
        )

        try:
            text_content, tool_calls, raw_message, cancelled = _stream_with_render(messages, current_super_tools)
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

        # 用户取消：丢弃部分模型回复，不执行工具调用，回到输入等待
        if cancelled:
            break

        llm_client.append_assistant_message(messages, raw_message)
        has_tool_call = len(tool_calls) > 0

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]

            post_tui(TuiRegion.TOOLS, active=True)
            try:
                _render_tool_call(tool_name, tool_args)

                try:
                    arguments = _parse_arguments(tool_args)
                    # Plan Mode safety net: block write/execute/delegate tools
                    if is_plan_mode() and tool_name in PLAN_MODE_BLOCKLIST:
                        output = (
                            f"⛔ Plan Mode active: '{tool_name}' is blocked. "
                            f"Complete your plan first, then exit Plan Mode to execute."
                        )
                    elif is_plan_mode() and tool_name == "RunTerminalCommand":
                        cmd = arguments.get("command", "")
                        if is_plan_mode_command_allowed(cmd):
                            handler = current_handlers.get(tool_name)
                            output = handler(**arguments)
                        else:
                            output = (
                                f"⛔ Plan Mode: this command is not allowed. "
                                f"Only {', '.join(PLAN_MODE_ALLOWED_COMMANDS)} commands are permitted in Plan Mode."
                            )
                    else:
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
            finally:
                post_tui(TuiRegion.TOOLS, active=False)

            messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

        CURRENT_CHECKPOINT = save_checkpoint(messages, CURRENT_CHECKPOINT)
        _apply_pending_title()

        if not has_tool_call:
            break

    current_context_tokens = estimate_tokens(
        messages, tools_definition=current_super_tools
    )
    if current_context_tokens > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{current_context_tokens} exceeded threshold {THRESHOLD}."
        )
        try:
            auto_compact(messages, reason=compact_reason, system_prompt_fn=get_dynamic_system_prompt)
            CURRENT_CHECKPOINT = save_checkpoint(messages, CURRENT_CHECKPOINT)
            console.print(
                "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
            refresh_status()
        except Exception as e:
            log_error_traceback("Orchestrator auto-compact error", e)
            error_msg = f"Error executing auto_compact: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")


def _init_tree_sitter_cache(console: Console):
    """初始化 tree-sitter 语言包缓存"""
    post_tui(TuiRegion.BACKGROUND, active=True)
    post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🌳 正在初始化语法解析器...[/#aaaaaa]")
    try:
        init_ts_cache()
        post_tui(TuiRegion.BACKGROUND, "[bold green]🌳 语法解析器初始化完成[/bold green]")
    except Exception as e:
        log_error_traceback("tree-sitter cache init", e)
        post_tui(TuiRegion.BACKGROUND, f"[bold red]⚠️ 语法解析器初始化失败: {e}[/bold red]")
    finally:
        post_tui(TuiRegion.BACKGROUND, active=False)


def _start_tree_sitter_cache_init_background():
    threading.Thread(target=_init_tree_sitter_cache, args=(console,), daemon=True).start()


CURRENT_CHECKPOINT = None
_pending_title = None


def _refresh_workspace_state() -> None:
    from utils import memory as memory_module
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    memory_module.refresh_workspace_paths()
    tasks_module.refresh_workspace_paths()
    teams_module.refresh_workspace_paths()
    SKILL_LOADER.refresh_workspace()


def _apply_workdir(path) -> None:
    set_workdir(path)
    orchestrator_access.visited_files.clear()
    _refresh_workspace_state()



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
        _teams_module.TEAM.rename_history_with_title(title)
    except Exception as exc:
        log_error_traceback("Failed to apply pending title", exc)



def _background_update_check():
    """后台检查更新，有新版本时提示用户（不阻塞启动）。"""
    active_started = False
    try:
        version_info = check_update()
        if not version_info:
            return
        new_version = version_info.get('version', '未知')
        release_log = version_info.get('release_log', '')
        post_tui(TuiRegion.BACKGROUND, active=True)
        active_started = True
        post_tui(TuiRegion.BACKGROUND, f"[bold yellow]📢 发现新版本 v{new_version}，输入 /update 查看详情并更新[/bold yellow]")
        if release_log:
            post_tui(TuiRegion.BACKGROUND, Markdown(release_log))
    except Exception:
        pass  # 静默失败，不影响正常使用
    finally:
        if active_started:
            post_tui(TuiRegion.BACKGROUND, active=False)


def _process_user_query(query: str, history: list, command_handler: CommandHandler) -> str | None:
    global CURRENT_CHECKPOINT, _pending_title, _PENDING_UPDATE_EXE_PATH

    query = query.strip()
    if not query:
        return None

    command_result = command_handler.process_command(
        query=query,
        history=history,
        current_checkpoint=CURRENT_CHECKPOINT,
        render_banner_fn=_render_startup_banner,
        render_hint_fn=_render_env_customization_hint,
        render_history_fn=_render_history,
    )

    if command_result.action == CommandAction.EXIT:
        return "exit"
    if command_result.action == CommandAction.LAUNCH_UPDATER_AND_EXIT:
        _PENDING_UPDATE_EXE_PATH = command_result.payload
        return "exit"
    if command_result.action == CommandAction.CONTINUE:
        return None
    if command_result.action == CommandAction.RUN_AGENT:
        history.append({"role": "user", "content": command_result.payload})

        if CURRENT_CHECKPOINT is None and any(msg['role'] == 'user' for msg in history):
            CURRENT_CHECKPOINT = save_checkpoint(history)

            def _title_worker():
                global _pending_title
                post_tui(TuiRegion.BACKGROUND, active=True)
                post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 正在生成对话标题...[/#aaaaaa]")
                try:
                    title = generate_title(query)
                    if title:
                        _pending_title = title
                        post_tui(TuiRegion.BACKGROUND, f"[bold green]🏷️ 对话标题生成完成：{title}[/bold green]")
                    else:
                        post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 对话标题生成结束：未生成可用标题[/#aaaaaa]")
                except Exception as exc:
                    log_error_traceback("Failed to generate title", exc)
                    post_tui(TuiRegion.BACKGROUND, f"[bold red]🏷️ 对话标题生成失败：{exc}[/bold red]")
                finally:
                    post_tui(TuiRegion.BACKGROUND, active=False)

            _title_thread = threading.Thread(target=_title_worker, daemon=True)
            _title_thread.start()
        else:
            _title_thread = None

        try:
            set_agent_loop_active(True)
            agent_loop(history)
        except RuntimeError as exc:
            console.print(f"[bold yellow]⚠️ {exc}[/bold yellow]")
        finally:
            set_agent_loop_active(False)
        if _title_thread is not None:
            _title_thread.join(timeout=10)
        _apply_pending_title()
        refresh_status()
        return None
    if command_result.action == CommandAction.RESET_CHECKPOINT:
        CURRENT_CHECKPOINT = None
        _pending_title = None
        refresh_status()
    elif command_result.action == CommandAction.LOAD_HISTORY:
        history[:], CURRENT_CHECKPOINT = command_result.payload
        _pending_title = None
        refresh_status()
    elif command_result.action == CommandAction.UPDATE_CHECKPOINT:
        CURRENT_CHECKPOINT = command_result.payload
        refresh_status()
    elif command_result.action == CommandAction.UPDATE_SYSTEM_PROMPT:
        history[0] = {"role": "system", "content": command_result.payload}
        refresh_status()
    return None


def _run_textual_main(history: list, command_handler: CommandHandler, prompt_for_workdir: bool) -> None:
    def submit_handler(query: str) -> str | None:
        return _process_user_query(query, history, command_handler)

    def startup_workdir_provider():
        return _current_workdir()

    def startup_workdir_handler(choice: str) -> None:
        current_workdir = _current_workdir()
        selected_workdir = resolve_chosen_workdir(choice, current_workdir)
        if selected_workdir == current_workdir:
            return
        _apply_workdir(selected_workdir)
        history[0] = {"role": "system", "content": get_dynamic_system_prompt()}
        refresh_status()
        refresh_tools_title()
        post_tui(TuiRegion.BACKGROUND, f"[bold green]📂 Workspace switched to: {selected_workdir}[/bold green]")
        render_current_workdir("当前工作目录已切换")

    def runtime_info_provider() -> str:
        tokens = estimate_tokens(
            history,
            tools_definition=get_current_tools_definition(),
        )
        return format_runtime_info(tokens, THRESHOLD)

    def header_info_provider() -> str:
        session_turns = sum(1 for message in history if message.get("role") == "user")
        try:
            memory_count = get_active_memory_count()
        except Exception:
            memory_count = 0
        try:
            skills_count = len(SKILL_LOADER.skills)
        except Exception:
            skills_count = 0
        try:
            mcp_status = GLOBAL_MCP_MANAGER.get_status_info()
            server_count = len(mcp_status.get("loaded_servers", []))
            tool_count = int(mcp_status.get("tool_count", 0))
        except Exception:
            server_count = 0
            tool_count = 0
        return f"💬 {session_turns} · 🧠 {memory_count} · 📚 Skills {skills_count} · MCP {server_count}S/{tool_count}T"

    app = MakeCodeTuiApp(
        submit_handler=submit_handler,
        runtime_info_provider=runtime_info_provider,
        header_info_provider=header_info_provider,
        startup_workdir_provider=startup_workdir_provider if prompt_for_workdir else None,
        startup_workdir_handler=startup_workdir_handler if prompt_for_workdir else None,
    )
    app.run()


if __name__ == "__main__":
    startup_workdir = resolve_startup_workdir()
    _apply_workdir(startup_workdir)
    prompt_for_workdir = should_prompt_for_workdir()

    _render_startup_banner()
    _render_env_customization_hint()
    render_current_workdir()

    # 后台检查更新（仅打包环境）
    if getattr(sys, 'frozen', False):
        threading.Thread(target=_background_update_check, daemon=True).start()

    # 后台初始化 tree-sitter 语言包缓存
    _start_tree_sitter_cache_init_background()

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
        apply_workdir_fn=_apply_workdir,
    )

    try:
        _run_textual_main(history, command_handler, prompt_for_workdir)
    finally:
        GLOBAL_MCP_MANAGER.stop()

    if _PENDING_UPDATE_EXE_PATH is not None:
        launch_updater(_PENDING_UPDATE_EXE_PATH)
