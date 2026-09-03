import asyncio
import inspect
import os
import subprocess
import sys
import threading
from pathlib import Path

from system import cli as cli_module


if __name__ == "__main__":
    external_cli_exit_code = cli_module.run_external_cli(sys.argv[1:])
    if external_cli_exit_code is not None:
        raise SystemExit(external_cli_exit_code)


from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from pydantic import Field

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
from system.clipboard import (
    read_image_file_from_system_clipboard,
    read_image_from_system_clipboard,
)
from system.console_render import (
    _render_history,
    _render_token_usage,
    _render_startup_banner,
    _render_env_customization_hint,
    render_current_workdir,
    render_current_task_plan,
    render_content_user_message,
    render_tool_call_block,
    render_tool_result_block,
    format_runtime_info,
    console,
)
from system.updater import AUTO_UPDATE_SUPPORTED, check_update, launch_updater
from utils.plan_mode import (
    is_plan_mode,
    is_plan_mode_command_allowed,
    PLAN_MODE_BLOCKLIST,
    PLAN_MODE_ALLOWED_COMMANDS,
)
from system.stream_render import StreamRenderer
from system.ts_validator import init_ts_cache
from system.tui_app import (
    MakeCodeTuiApp,
    post_tui,
    TuiRegion,
    set_agent_loop_active,
    set_temporary_query_enabled,
    refresh_status,
    scroll_all_panes_to_bottom,
    consume_temporary_query,
    clear_temporary_query,
    restore_temporary_query_to_input,
)
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    COMMON_TOOL_MODELS,
    sanitize_title,
)
from utils.llm_client import (
    close_async_llm_client,
    create_current_async_llm_client,
    format_tools_for_current_model,
)
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils import paths
from utils.vision import (
    image_reference_marker,
    parse_image_placeholders,
    remove_image_placeholders,
    store_image_bytes_attachment,
)
from utils.memory import (
    MEMORY_RECALL_TOOLS,
    MEMORY_RECALL_TOOLS_HANDLERS,
    MEMORY_RECALL_TOOL_MODELS,
    MEMORY_SELF_MANAGEMENT_TOOLS,
    MEMORY_SELF_MANAGEMENT_TOOL_MODELS,
    ORCHESTRATOR_AGENT_ID,
    auto_compact,
    compact_tool_outputs,
    estimate_tokens,
    estimate_token_breakdown,
    get_active_memory_count,
    get_compaction_thresholds,
    get_context_token_limit,
    manual_memory_update,
    partial_compact,
    prepend_recalled_memory_to_query,
    recall_long_term_memories,
)
from utils.conversations import CONVERSATION_STORE
from utils.skills import SKILL_LOADER, SKILL_TOOLS, SKILL_TOOLS_HANDLERS, SKILL_TOOL_MODELS
import utils.tasks as _tasks_module
import utils.teams as _teams_module
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS, TASK_MANAGER_TOOL_MODELS
from utils.teams import TEAM_TOOLS, TEAM_TOOLS_HANDLERS, TEAM_TOOL_MODELS
from tools.ask_user import ASK_USER_TOOLS, ASK_USER_TOOLS_HANDLERS, ASK_USER_TOOL_MODELS
from utils.tool_validation import (
    ToolArgumentsModel,
    ToolArgumentValidationError,
    build_tool_definitions,
    is_tool_error_output,
    merge_tool_model_registries,
    parse_tool_arguments,
    validate_builtin_tool_arguments,
)
from system.tool_history import tool_result_status

STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


TEMPORARY_INSTRUCTION_START = "<makecode-temporary-user-instruction>"
TEMPORARY_INSTRUCTION_END = "</makecode-temporary-user-instruction>"


class GenerateConversationTitle(ToolArgumentsModel):
    """Structured title returned by the title-generation request."""

    title: str = Field(
        ...,
        description="A concise filename-safe conversation title, 1–30 Unicode characters.",
    )


TITLE_GENERATION_TOOLS, TITLE_GENERATION_TOOL_MODELS = build_tool_definitions(
    GenerateConversationTitle,
)



_PENDING_UPDATE_EXE_PATH = None


def _signal_legacy_updater_ready() -> None:
    ready_file = os.environ.pop("MAKECODE_UPDATE_READY_FILE", None)
    if ready_file:
        Path(ready_file).touch()


def _current_workdir():
    return paths.workdir()


def get_dynamic_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        str(_current_workdir()),
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
        plan_mode=is_plan_mode(),
    )


def get_current_tools_definition(mcp_tools: list | None = None, llm_client=None):
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    all_tools = _get_all_tools_definition(mcp_tools, llm_client=llm_client)
    if is_plan_mode():
        # Plan Mode: 黑名单过滤，禁止写入/执行/委托工具
        filtered = [
            tool
            for tool in all_tools
            if (tool.get("function") or {}).get("name", tool.get("name"))
            not in PLAN_MODE_BLOCKLIST
        ]
        return filtered
    return all_tools


def _get_all_tools_definition(mcp_tools: list | None = None, llm_client=None):
    """获取全部工具定义（不考虑 Plan Mode 过滤）"""
    tools = (
        COMMON_TOOLS
        + MEMORY_RECALL_TOOLS
        + MEMORY_SELF_MANAGEMENT_TOOLS
        + SKILL_TOOLS
        + TASK_MANAGER_TOOLS
        + TEAM_TOOLS
        + ASK_USER_TOOLS
        + (GLOBAL_MCP_MANAGER.get_tools() if mcp_tools is None else mcp_tools)
    )
    try:
        if llm_client is not None:
            return llm_client.format_tools(tools)
        return format_tools_for_current_model(tools)
    except RuntimeError as exc:
        if "No model configured" in str(exc):
            return []
        raise



BASE_SUPER_TOOL_MODELS = merge_tool_model_registries(
    COMMON_TOOL_MODELS,
    MEMORY_RECALL_TOOL_MODELS,
    MEMORY_SELF_MANAGEMENT_TOOL_MODELS,
    SKILL_TOOL_MODELS,
    TASK_MANAGER_TOOL_MODELS,
    TEAM_TOOL_MODELS,
    ASK_USER_TOOL_MODELS,
)

BASE_SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **MEMORY_RECALL_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
    **ASK_USER_TOOLS_HANDLERS,
}


async def generate_title(user_query: str, max_rounds: int = 8) -> str | None:
    """Generate a short title from the provided user conversation content."""
    title_client = None
    try:
        title_client = create_current_async_llm_client()
        if title_client is None:
            return None
        messages = [
            {"role": "system", "content": get_title_generation_system_prompt()},
            {"role": "user", "content": user_query},
        ]
        tools = title_client.format_tools(TITLE_GENERATION_TOOLS)
        for round_index in range(max_rounds):
            result = None
            async for event in title_client.generate_stream(messages, tools):
                if event.get("type") == "done":
                    result = event["result"]
            if result is None:
                break

            title_validation_failed = False
            for tool_call in result.tool_calls:
                if tool_call.get("name") != "GenerateConversationTitle":
                    continue
                try:
                    arguments = validate_builtin_tool_arguments(
                        "GenerateConversationTitle",
                        tool_call.get("arguments"),
                        TITLE_GENERATION_TOOL_MODELS["GenerateConversationTitle"],
                    )
                except ToolArgumentValidationError as exc:
                    log_error_traceback("Title tool argument validation", exc)
                    messages.append(result.assistant_message)
                    tool_result = title_client.format_tool_result(
                        tool_call.get("id"),
                        "GenerateConversationTitle",
                        str(exc),
                    )
                    tool_result["is_error"] = True
                    messages.append(tool_result)
                    title_validation_failed = True
                    break
                title = arguments["title"]
                return sanitize_title(title)

            if title_validation_failed:
                continue

            current_round = round_index + 1
            remaining_rounds = max_rounds - current_round
            if remaining_rounds <= 0:
                break
            messages.append(result.assistant_message)
            messages.append({
                "role": "user",
                "content": (
                    f"[auto generated] current_round={current_round} / max_round={max_rounds}. "
                    f"Remaining rounds: {remaining_rounds}.\n\n"
                    "Call GenerateConversationTitle exactly once now. The title generation loop exits "
                    "automatically at the max round. Do not return the title as plain text."
                ),
            })
    except Exception as exc:
        log_error_traceback("Failed to generate title", exc)
    finally:
        if title_client is not None:
            await close_async_llm_client(title_client)
    return None


async def _stream_with_render(messages: list, current_tools: list, llm_client):
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
        text_content, tool_calls, raw_message = await renderer.render_async(
            stream,
            agent_name="Orchestrator",
        )
        cancelled = is_cancelled()
    finally:
        stop_cancel_listener()

    return text_content, tool_calls, raw_message, cancelled


def _is_no_model_configured_error(exc: Exception) -> bool:
    return "No model configured" in str(exc)


async def _run_tool_handler(handler, arguments: dict):
    if inspect.iscoroutinefunction(handler):
        return await handler(**arguments)
    output = await asyncio.to_thread(handler, **arguments)
    if inspect.isawaitable(output):
        return await output
    return output


async def agent_loop(
    messages: list,
    llm_client=None,
    *,
    recall_query: str | None = None,
) -> bool:
    """Agent 主循环：每次业务请求独占一个 LLM client。"""
    set_temporary_query_enabled(False)
    owns_client = llm_client is None
    if owns_client:
        if CONVERSATION_STORE.active_root is None:
            llm_client = create_current_async_llm_client()
        else:
            llm_client = create_current_async_llm_client(
                conversation_root=CONVERSATION_STORE.active_root,
            )
    if llm_client is None:
        console.print(
            "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
        )
        return False
    try:
        return await _agent_loop_with_client(messages, llm_client, recall_query=recall_query)
    finally:
        restore_temporary_query_to_input()
        set_temporary_query_enabled(False)
        clear_temporary_query()
        if owns_client:
            await close_async_llm_client(llm_client)


async def _agent_loop_with_client(
    messages: list,
    llm_client,
    *,
    recall_query: str | None = None,
) -> bool:
    committed_response = False
    was_cancelled = False

    async def _manage_long_term_memory(prompt: str, **kwargs):
        post_tui(TuiRegion.BACKGROUND, active=True)
        post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🧠 Agent 主动请求管理长期记忆：{escape(prompt.strip())}[/#aaaaaa]")
        try:
            return await manual_memory_update(prompt, messages)
        finally:
            post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🧠 Agent 主动记忆管理流程已返回。[/#aaaaaa]")
            post_tui(TuiRegion.BACKGROUND, active=False)

    mcp_tools, mcp_handlers = GLOBAL_MCP_MANAGER.get_registry_snapshot()
    current_handlers = {
        **BASE_SUPER_TOOLS_HANDLERS,
        **mcp_handlers,
        "ManageLongTermMemory": _manage_long_term_memory, # 单独注册，因为只提供给 Agent 主循环使用，不暴露给技能调用
    }
    try:
        current_super_tools = get_current_tools_definition(mcp_tools, llm_client=llm_client)
    except RuntimeError as exc:
        if _is_no_model_configured_error(exc):
            console.print(
                "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
            )
            return False
        raise

    messages[0] = {"role": "system", "content": get_dynamic_system_prompt()}
    context_token_limit = get_context_token_limit()
    initial_context_tokens = estimate_tokens(
        messages,
        tools_definition=current_super_tools,
    )
    tool_output_threshold, partial_threshold = get_compaction_thresholds()

    if initial_context_tokens * 100 >= context_token_limit * partial_threshold:
        post_tui(
            TuiRegion.BACKGROUND,
            "[bold yellow]⚡️ 已触发第二层局部摘要压缩。[/bold yellow]",
        )
        compact_reason = (
            f"Pre agent_loop partial compact triggered: estimated tokens "
            f"{initial_context_tokens} reached {partial_threshold}% of threshold "
            f"{context_token_limit}."
        )
        partial_succeeded = False
        try:
            partial_succeeded = await partial_compact(
                messages,
                context_token_limit,
                initial_context_tokens,
                compact_reason,
            )
        except Exception as exc:
            log_error_traceback("Orchestrator partial compact error", exc)
            console.print(
                f"[bold red]⚠️ {escape(f'局部上下文压缩失败：{exc}')}[/bold red]"
            )
        if partial_succeeded:
            post_tui(
                TuiRegion.BACKGROUND,
                "[bold green]✅ 第二层局部摘要压缩已完成。[/bold green]",
            )
        else:
            post_tui(
                TuiRegion.BACKGROUND,
                "[bold yellow]↩️ 第二层局部摘要压缩未提交，回退执行第一层工具输出裁剪。[/bold yellow]",
            )
            tool_outputs_compacted = compact_tool_outputs(messages)
            if tool_outputs_compacted:
                post_tui(
                    TuiRegion.BACKGROUND,
                    "[bold green]✅ 第一层工具输出裁剪已完成。[/bold green]",
                )
            else:
                post_tui(
                    TuiRegion.BACKGROUND,
                    "[#aaaaaa]第一层已检查，没有可裁剪的较早工具输出。[/#aaaaaa]",
                )
    elif initial_context_tokens * 100 >= context_token_limit * tool_output_threshold:
        post_tui(
            TuiRegion.BACKGROUND,
            "[bold yellow]⚡️ 已触发第一层工具输出裁剪。[/bold yellow]",
        )
        tool_outputs_compacted = compact_tool_outputs(messages)
        if tool_outputs_compacted:
            post_tui(
                TuiRegion.BACKGROUND,
                "[bold green]✅ 第一层工具输出裁剪已完成。[/bold green]",
            )
        else:
            post_tui(
                TuiRegion.BACKGROUND,
                "[#aaaaaa]第一层已检查，没有可裁剪的较早工具输出。[/#aaaaaa]",
            )

    if recall_query is not None:
        recall_result = await recall_long_term_memories(
            recall_query,
            previous_assistant_content=_get_previous_assistant_content(messages),
            source="用户请求预召回",
            agent_id=ORCHESTRATOR_AGENT_ID,
        )
        memory_context = recall_result.get("content", "")
        if memory_context:
            latest_user_message = next(
                message for message in reversed(messages) if message.get("role") == "user"
            )
            content = latest_user_message.get("content")
            if isinstance(content, list):
                latest_user_message["content"] = [
                    {
                        "type": "text",
                        "text": prepend_recalled_memory_to_query("", memory_context),
                    },
                    *content,
                ]
            else:
                latest_user_message["content"] = prepend_recalled_memory_to_query(
                    content if isinstance(content, str) else "",
                    memory_context,
                )

    def _append_temporary_query() -> dict | None:
        temporary_query = consume_temporary_query()
        if temporary_query is None:
            return None
        message = {
            "role": "user",
            "content": (
                f"{TEMPORARY_INSTRUCTION_START}\n"
                "Treat the enclosed text as an additional or supplemental instruction for the task currently in progress.\n"
                "After addressing it, resume the current task from where you left off. Do not stop merely because you have "
                "responded to this instruction while the task remains incomplete, unless the enclosed text explicitly changes, "
                "pauses, or cancels the task.\n\n"
                f"{temporary_query}\n"
                f"{TEMPORARY_INSTRUCTION_END}"
            ),
            "message_metadata": {"temporary_query": True},
        }
        messages.append(message)
        post_tui(TuiRegion.CONTENT, "[#3f3f46]─[/#3f3f46]")
        post_tui(TuiRegion.CONTENT, render_content_user_message(message["content"]))
        return message

    set_temporary_query_enabled(True)
    while True:
        temporary_query_message = _append_temporary_query()
        # Update system prompt to reflect current plan mode state
        messages[0] = {"role": "system", "content": get_dynamic_system_prompt()}
        context_token_limit = get_context_token_limit()

        _render_token_usage(
            messages,
            tools_definition=current_super_tools,
            threshold=context_token_limit,
            estimate_tokens_fn=estimate_tokens,
        )

        try:
            text_content, tool_calls, raw_message, cancelled = await _stream_with_render(
                messages,
                current_super_tools,
                llm_client,
            )
        except Exception as e:
            if _is_no_model_configured_error(e):
                console.print(
                    "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
                )
                if temporary_query_message is not None and messages and messages[-1] is temporary_query_message:
                    messages.pop()
                clear_temporary_query()
                break
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"智能体执行出错: {e}."
            console.print(f"[bold red]⚠️ {escape(error_msg)}[/bold red]")
            if temporary_query_message is not None and messages and messages[-1] is temporary_query_message:
                messages.pop()
            clear_temporary_query()
            break

        # 用户取消：丢弃部分模型回复，不执行工具调用，回到输入等待
        if cancelled:
            was_cancelled = True
            if temporary_query_message is not None and messages and messages[-1] is temporary_query_message:
                messages.pop()
            clear_temporary_query()
            break

        llm_client.append_assistant_message(messages, raw_message)
        committed_response = True
        CONVERSATION_STORE.save_messages(messages)
        has_tool_call = len(tool_calls) > 0
        stop_reason = raw_message.get("stop_reason") if isinstance(raw_message, dict) else None

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]
            post_tui(
                TuiRegion.CONTENT,
                collapsible_title=f"🛠️ Tool: {tool_name}",
                collapsible_open=True,
                collapsible_kind="tools",
            )
            post_tui(
                TuiRegion.CONTENT,
                render_tool_call_block(tool_name, tool_args, tool_call_id=tool_id),
            )
            tool_error = False
            output = ""

            try:
                handler = current_handlers.get(tool_name)
                if not handler:
                    tool_error = True
                    output = f"Unknown tool: {tool_name}"
                elif tool_name in BASE_SUPER_TOOL_MODELS:
                    arguments = validate_builtin_tool_arguments(
                        tool_name,
                        tool_args,
                        BASE_SUPER_TOOL_MODELS[tool_name],
                    )
                elif tool_name in mcp_handlers:
                    arguments = parse_tool_arguments(tool_name, tool_args)
                else:
                    raise RuntimeError(
                        f"Built-in tool model not registered: {tool_name}"
                    )

                if not tool_error and is_plan_mode() and tool_name in PLAN_MODE_BLOCKLIST:
                    tool_error = True
                    output = (
                        f"⛔ Plan Mode active: '{tool_name}' is blocked. "
                        f"Complete your plan first, then exit Plan Mode to execute."
                    )
                elif not tool_error and is_plan_mode() and tool_name == "RunTerminalCommand":
                    cmd = arguments.get("command", "")
                    if is_plan_mode_command_allowed(cmd):
                        output = await _run_tool_handler(handler, arguments)
                        tool_error = is_tool_error_output(output)
                    else:
                        tool_error = True
                        output = (
                            f"⛔ Plan Mode: this command is not allowed. "
                            f"Only {', '.join(PLAN_MODE_ALLOWED_COMMANDS)} commands are permitted in Plan Mode."
                        )
                elif not tool_error:
                    output = await _run_tool_handler(handler, arguments)
                    tool_error = is_tool_error_output(output)
            except ToolArgumentValidationError as exc:
                tool_error = True
                output = str(exc)
            except Exception as e:
                tool_error = True
                log_error_traceback(
                    f"Orchestrator tool execution error: {tool_name}", e
                )
                output = f"Error executing {tool_name}: {e}."

            tool_result = llm_client.format_tool_result(
                tool_id,
                tool_name,
                output,
            )
            if tool_error:
                tool_result["is_error"] = True
            messages.append(tool_result)
            post_tui(
                TuiRegion.CONTENT,
                render_tool_result_block(
                    output,
                    status=tool_result_status(is_error=tool_error, output=output),
                    error=str(output) if tool_error else "",
                ),
            )
            post_tui(
                TuiRegion.CONTENT,
                collapsible_title=f"🛠️ Tool: {tool_name}",
                collapsible_close=True,
                collapsible_kind="tools",
            )

        if has_tool_call:
            CONVERSATION_STORE.save_messages(messages)

        if not has_tool_call and stop_reason != "pause_turn":
            break

    restore_temporary_query_to_input()
    set_temporary_query_enabled(False)
    clear_temporary_query()
    if not committed_response:
        clear_temporary_query()
        return False
    if was_cancelled:
        return True

    title_generated = await _generate_title_if_missing(messages)
    _apply_pending_title()
    if title_generated:
        refresh_status()

    return True


def _init_tree_sitter_cache(console: Console):
    """初始化 tree-sitter 语言包缓存"""
    post_tui(TuiRegion.BACKGROUND, active=True)
    post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🌳 正在初始化语法解析器...[/#aaaaaa]")
    try:
        init_ts_cache()
        post_tui(TuiRegion.BACKGROUND, "[bold green]🌳 语法解析器初始化完成[/bold green]")
    except Exception as e:
        log_error_traceback("tree-sitter cache init", e)
        post_tui(TuiRegion.BACKGROUND, f"[bold red]⚠️ 语法解析器初始化失败: {escape(str(e))}[/bold red]")
    finally:
        post_tui(TuiRegion.BACKGROUND, active=False)


def _start_tree_sitter_cache_init_background():
    threading.Thread(target=_init_tree_sitter_cache, args=(console,), daemon=True).start()


_pending_title = None



def _get_current_conversation_title() -> str | None:
    active_path = CONVERSATION_STORE.active_path
    if active_path is None or not active_path.exists():
        return None
    return CONVERSATION_STORE.active_title or "未命名对话"


def _refresh_workspace_state() -> None:
    from utils import memory as memory_module
    from utils import tasks as tasks_module
    from utils import teams as teams_module

    memory_module.refresh_workspace_paths()
    CONVERSATION_STORE.refresh_workspace()
    tasks_module.refresh_workspace_paths()
    teams_module.refresh_workspace_paths()
    SKILL_LOADER.refresh_workspace()


def _apply_workdir(path) -> None:
    set_workdir(path)
    _refresh_workspace_state()



def _ensure_active_conversation() -> None:
    path = CONVERSATION_STORE.ensure_active()
    conversation_id = CONVERSATION_STORE.active_id
    root = path.parent
    if _tasks_module.TASK_MANAGER.conversation_id != conversation_id:
        _tasks_module.activate_conversation(root, conversation_id)
    if _teams_module.TEAM.conversation_id != conversation_id:
        _teams_module.activate_conversation(root, conversation_id)



def _apply_pending_title():
    """Apply a generated title to the active conversation metadata."""
    global _pending_title
    active_path = CONVERSATION_STORE.active_path
    if _pending_title is None or active_path is None or not active_path.exists():
        if _pending_title is not None and active_path is None:
            _pending_title = None
        return
    title = _pending_title
    _pending_title = None
    try:
        CONVERSATION_STORE.update_title(title)
    except Exception as exc:
        log_error_traceback("Failed to apply pending title", exc)


def _collect_user_message_content(history: list) -> str:
    user_contents = []
    for message in history:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        metadata = message.get("message_metadata")
        display_content = metadata.get("display_content") if isinstance(metadata, dict) else None
        content = display_content if isinstance(display_content, str) else content
        if isinstance(content, str):
            text = remove_image_placeholders(content).strip()
        elif isinstance(content, list):
            text = "\n\n".join(
                remove_image_placeholders(block.get("text", "")).strip()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip()
            )
        else:
            text = ""
        if text:
            user_contents.append(text)
    return "\n\n".join(user_contents)


async def _generate_title_if_missing(history: list) -> bool:
    global _pending_title
    if (
        CONVERSATION_STORE.active_path is None
        or not CONVERSATION_STORE.active_path.exists()
        or CONVERSATION_STORE.active_title
    ):
        return False

    title_source = _collect_user_message_content(history)
    if not title_source:
        return False

    post_tui(TuiRegion.BACKGROUND, active=True)
    post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 正在生成对话标题...[/#aaaaaa]")
    try:
        title = await generate_title(title_source)
        if title:
            _pending_title = title
            post_tui(TuiRegion.BACKGROUND, f"[bold green]🏷️ 对话标题生成完成：{title}[/bold green]")
            return True
        post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 对话标题生成结束：未生成可用标题[/#aaaaaa]")
    except Exception as exc:
        log_error_traceback("Failed to generate title", exc)
        post_tui(TuiRegion.BACKGROUND, f"[bold red]🏷️ 对话标题生成失败：{escape(str(exc))}[/bold red]")
    finally:
        post_tui(TuiRegion.BACKGROUND, active=False)
    return False


async def _regenerate_conversation_title(history: list) -> None:
    global _pending_title
    if CONVERSATION_STORE.active_path is None or not CONVERSATION_STORE.active_path.exists():
        return

    title_source = _collect_user_message_content(history)
    if not title_source:
        return

    post_tui(TuiRegion.BACKGROUND, active=True)
    post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 正在根据全部用户消息重新生成对话标题...[/#aaaaaa]")
    try:
        title = await generate_title(title_source)
        if title:
            _pending_title = title
            _apply_pending_title()
            post_tui(TuiRegion.BACKGROUND, f"[bold green]🏷️ 对话标题已更新：{title}[/bold green]")
        else:
            post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 对话标题重新生成结束：未生成可用标题[/#aaaaaa]")
    except Exception as exc:
        log_error_traceback("Failed to regenerate title", exc)
        post_tui(TuiRegion.BACKGROUND, f"[bold red]🏷️ 对话标题重新生成失败：{escape(str(exc))}[/bold red]")
    finally:
        refresh_status()
        post_tui(TuiRegion.BACKGROUND, active=False)


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


def _parse_input_images(text: str) -> tuple[str, list[dict[str, str]]]:
    _ensure_active_conversation()
    return parse_image_placeholders(text, CONVERSATION_STORE.active_root)


def _paste_image_from_system_clipboard() -> str | None:
    file_image = read_image_file_from_system_clipboard()
    if file_image is not None:
        data, filename, media_type = file_image
    else:
        image = read_image_from_system_clipboard()
        if image is None:
            return None
        data, media_type = image
        extension = media_type.removeprefix("image/")
        filename = f"clipboard.{('jpg' if extension == 'jpeg' else extension)}"
    _ensure_active_conversation()
    block = store_image_bytes_attachment(
        CONVERSATION_STORE.active_root,
        data,
        filename,
        media_type,
    )
    return image_reference_marker(block)


def _message_from_user_query(user_query: str) -> tuple[dict, str]:
    display_text, parts = _parse_input_images(user_query)
    image_present = any(part.get("type") == "image" for part in parts)
    if not image_present:
        return {"role": "user", "content": user_query}, display_text

    return {"role": "user", "content": parts}, display_text


def _get_previous_assistant_content(history: list) -> str:
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        assistant_content = message.get("content", "")
        if not isinstance(assistant_content, str) or not assistant_content.strip():
            continue
        return assistant_content.strip()
    return ""


async def _process_user_query(query: str, history: list, command_handler: CommandHandler) -> str | None:
    global _pending_title, _PENDING_UPDATE_EXE_PATH

    query = query.strip()
    if not query:
        return None

    command_result = await command_handler.process_command(
        query=query,
        history=history,
        current_conversation=CONVERSATION_STORE.active_path,
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
        user_query = command_result.payload
        original_query = command_result.original_query
        try:
            set_agent_loop_active(True)
            _ensure_active_conversation()
            if command_result.skip_memory_recall:
                post_tui(
                    TuiRegion.BACKGROUND,
                    "[#aaaaaa]🧠 已跳过本次请求的记忆预召回流程。[/#aaaaaa]",
                )
            user_message, display_query = _message_from_user_query(user_query)
            user_message_record = user_message
            if original_query is not None:
                user_message_record["message_metadata"] = {
                    "display_content": original_query,
                    "skill_command": True,
                }
            elif isinstance(user_message_record.get("content"), list):
                user_message_record["message_metadata"] = {
                    "display_content": display_query,
                }
            history.append(user_message_record)

            if command_result.skip_memory_recall:
                await agent_loop(history)
            else:
                await agent_loop(
                    history,
                    recall_query=remove_image_placeholders(original_query or user_query),
                )
        except RuntimeError as exc:
            console.print(f"[bold yellow]⚠️ {escape(str(exc))}[/bold yellow]")
        finally:
            set_agent_loop_active(False)
        _apply_pending_title()
        refresh_status()
        return None
    if command_result.action == CommandAction.RESET_CONVERSATION:
        _pending_title = None
        refresh_status()
    elif command_result.action == CommandAction.LOAD_HISTORY:
        history[:], _ = command_result.payload
        _pending_title = None
        refresh_status()
    elif command_result.action == CommandAction.UPDATE_SYSTEM_PROMPT:
        history[0] = {"role": "system", "content": command_result.payload}
        refresh_status()
    return None


def _run_textual_main(
        history: list,
        command_handler: CommandHandler,
        prompt_for_workdir: bool,
        startup_load_id: str | None = None,
) -> None:
    async def submit_handler(query: str) -> str | None:
        return await _process_user_query(query, history, command_handler)

    async def conversation_title_regenerate_handler() -> None:
        await _regenerate_conversation_title(history)

    def startup_load_handler() -> None:
        nonlocal history
        set_agent_loop_active(True)
        try:
            loaded_history, _ = command_handler.handle_load(
                history,
                CONVERSATION_STORE.active_path,
                _render_startup_banner,
                _render_env_customization_hint,
                _render_history,
                conversation_id=startup_load_id,
            )
            if loaded_history is not history:
                history = loaded_history
                scroll_all_panes_to_bottom()
        finally:
            set_agent_loop_active(False)
        refresh_status()

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
        post_tui(TuiRegion.BACKGROUND, f"[bold green]📂 Workspace switched to: {selected_workdir}[/bold green]")
        render_current_workdir("当前工作目录已切换")

    def runtime_info_provider() -> str:
        return format_runtime_info()

    def token_usage_provider() -> tuple[dict[str, int], int]:
        return (
            estimate_token_breakdown(
                history,
                tools_definition=get_current_tools_definition(),
            ),
            get_context_token_limit(),
        )

    def header_info_provider() -> str:
        workdir = paths.workdir()
        workdir_str = str(workdir)
        if len(workdir_str) > 60 and len(workdir.parts) > 5:
            workdir_str = str(Path(*workdir.parts[:3], "...", *workdir.parts[-2:]))
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
        return f"📂 {workdir_str} · 💬 {session_turns} · 🧠 {memory_count} · 📚 Skills {skills_count} · MCP {server_count}S/{tool_count}T"

    app = MakeCodeTuiApp(
        submit_handler=submit_handler,
        runtime_info_provider=runtime_info_provider,
        token_usage_provider=token_usage_provider,
        header_info_provider=header_info_provider,
        conversation_title_provider=_get_current_conversation_title,
        conversation_title_regenerate_handler=conversation_title_regenerate_handler,
        messages_provider=lambda: history,
        slash_commands_provider=command_handler.get_slash_completion_commands,
        startup_workdir_provider=startup_workdir_provider if prompt_for_workdir else None,
        startup_workdir_handler=startup_workdir_handler if prompt_for_workdir else None,
        startup_load_handler=startup_load_handler if cli_module.STARTUP_LOAD_REQUESTED else None,
        image_placeholder_handler=_parse_input_images,
        image_clipboard_handler=_paste_image_from_system_clipboard,
    )
    app.run()


def _launch_macos_terminal_if_needed() -> bool:
    if (
        sys.platform != "darwin"
        or not getattr(sys, "frozen", False)
        or os.environ.get("MAKECODE_TERMINAL_RELAUNCHED") == "1"
        or sys.stdin.isatty()
    ):
        return False

    script = """
on run argv
    set executablePath to item 1 of argv
    set commandLine to "env MAKECODE_TERMINAL_RELAUNCHED=1 " & quoted form of executablePath
    set argumentCount to count of argv
    if argumentCount > 1 then
        repeat with index from 2 to argumentCount
            set commandLine to commandLine & " " & quoted form of (item index of argv)
        end repeat
    end if
    tell application "Terminal"
        activate
        do script commandLine
    end tell
end run
"""
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, sys.executable, *sys.argv[1:]],
        check=True,
    )
    return True


if __name__ == "__main__":
    if _launch_macos_terminal_if_needed():
        raise SystemExit(0)

    startup_workdir = resolve_startup_workdir()
    _apply_workdir(startup_workdir)
    _signal_legacy_updater_ready()
    prompt_for_workdir = should_prompt_for_workdir() and not cli_module.STARTUP_LOAD_REQUESTED

    _render_startup_banner()
    _render_env_customization_hint()
    render_current_workdir()

    # 后台检查更新（仅支持自动更新的打包环境）
    if getattr(sys, 'frozen', False) and AUTO_UPDATE_SUPPORTED:
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
        conversation_store=CONVERSATION_STORE,
        auto_compact_fn=auto_compact,
        apply_workdir_fn=_apply_workdir,
    )

    try:
        _run_textual_main(
            history,
            command_handler,
            prompt_for_workdir,
            startup_load_id=cli_module.STARTUP_LOAD_ID,
        )
    finally:
        GLOBAL_MCP_MANAGER.stop()

    if CONVERSATION_STORE.active_id:
        executable = (
            "MakeCode"
            if getattr(sys, "frozen", False)
            else f'python "{Path(__file__).resolve()}"'
        )
        print(f"{executable} --load {CONVERSATION_STORE.active_id}")

    if _PENDING_UPDATE_EXE_PATH is not None:
        launch_updater(_PENDING_UPDATE_EXE_PATH)
