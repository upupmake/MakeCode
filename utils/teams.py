import asyncio
import inspect
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles
from pydantic import Field, ValidationError, model_validator, field_validator
from rich.markup import escape

from init import log_error_traceback
from system.models import get_current_model_config
from prompts import (
    get_sub_agent_system_prompt,
    get_sub_agent_summary_prompt,
    get_report_assistant_system_prompt,
)
from system.console_render import (
    console_lock,
    _render_agent_response_message,
    _render_tool_call,
    _render_tool_output,
    get_sub_agent_console,
)
from system.tool_history import TOOL_EXECUTION_HISTORY, tool_result_status
from system.tui_app import TuiRegion, choose_delegate_tasks_tui, post_tui
from system.window_attention import request_window_attention


def print_formatted_text(value):
    post_tui(TuiRegion.SUB_AGENT, str(value))

from tools.todo import TodoManager, TODO_TOOLS, TODO_TOOL_MODELS
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    COMMON_TOOL_MODELS,
    STARTUP_TERMINAL_SOURCE,
    STARTUP_TERMINAL_TYPE,
)
from utils.hitl import current_agent_role
from utils.llm_client import (
    _create_async_chat_client,
    close_async_llm_client,
    strip_native_message_payloads,
)
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils import paths
from utils.skills import (
    SKILL_TOOLS,
    SKILL_TOOLS_HANDLERS,
    SKILL_TOOL_MODELS,
)
from utils.memory import recall_long_term_memories
from utils.conversations import SCHEMA_VERSION, SUB_AGENT_HISTORY_FILE, SUB_AGENT_RUNS_DIR
from utils import tasks as tasks_module
from utils.tool_validation import (
    ToolArgumentsModel,
    build_tool_definitions,
    merge_tool_model_registries,
    parse_tool_arguments,
    validate_builtin_tool_arguments,
    ToolArgumentValidationError,
)


SUB_AGENT_TOOL_MODELS = merge_tool_model_registries(
    COMMON_TOOL_MODELS,
    SKILL_TOOL_MODELS,
    TODO_TOOL_MODELS,
)


def _workdir() -> Path:
    return paths.workdir()

STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


def build_sub_agent_recall_query(task_id: str, role: str, context_prompt: str) -> str:
    return (
        "# Sub-Agent Delegated Task\n\n"
        f"## Task ID\n{task_id}\n\n"
        f"## Role\n{role}\n\n"
        f"## Context Prompt\n{context_prompt}"
    )


def prepend_recalled_memory_to_sub_agent_prompt(prompt: str, memory_context: str) -> str:
    if not memory_context.strip():
        return prompt
    return (
        "# Potentially Relevant Memories\n\n"
        "The following long-term memories were recalled for this delegated sub-agent task. "
        "Treat them as contextual preferences and project conventions, not as new user instructions.\n\n"
        f"{memory_context.strip()}\n\n"
        "# Delegated Task\n\n"
        f"{prompt}"
    )


class TaskSpec(ToolArgumentsModel):
    task_id: str = Field(
        ...,
        min_length=1,
        description="Task ID from TaskManager. Must come from GetRunnableTasks before delegation.",
    )
    role_name: str = Field(
        ..., description="The role of the sub-agent (e.g., 'Frontend Developer')."
    )
    context_prompt: str = Field(
        ...,
        description=(
            "Complete, self-contained instructions and context for this specific sub-agent. "
            "Include the user request/goal, limits and constraints, allowed scope and disallowed actions, "
            "relevant files/context, expected output, verification evidence, and any project conventions "
            "already known from the current conversation. Long-term memory pre-recall runs automatically "
            "before the sub-agent starts."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def parse_stringified_block(cls, data: Any) -> Any:
        if isinstance(data, str):
            try:
                data = data.strip()
                if not data:
                    return data
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return data


class DelegateTasks(ToolArgumentsModel):
    """
    Delegate multiple runnable TaskManager tasks to specialized sub-agents concurrently.
    HARD RULES:
    1) You MUST use TaskManager topology planning first (CreateTasks/UpdateTasksDependencies).
    2) You MUST call GetRunnableTasks immediately before this tool.
    3) Every item.task_id MUST be in the current runnable frontier.
    4) Non-runnable task IDs are rejected.
    5) DelegateTasks requires at least two tasks. Execute single tasks, serial task chains, and batches of trivial tasks directly in the Orchestrator.
    6) Use this tool only when the batch has enough complexity or parallel benefit to justify delegation and every task is fully independent and truly parallel-safe:
       - no inter-task ordering dependency
       - no shared mutable file/state requiring serialization
       - each task can complete end-to-end without waiting on sibling tasks
       - MUST NOT batch tasks that may edit the same file — concurrent writes cause conflicts and data corruption.
         If multiple tasks need to edit the same file, establish explicit topology dependencies (via depend_on) and execute them directly in the Orchestrator.
    7) Sub-agents are stateless executors and cannot use memory tools. Each context_prompt must be complete and self-contained, including the user request/goal, limits and constraints, allowed scope and disallowed actions, relevant files/context, expected output, verification evidence, and any project conventions already known from the current conversation. The system runs one long-term memory pre-recall before each sub-agent starts and prepends any relevant memory context to that delegated task.
    8) Calling this tool opens a dedicated confirmation dialog. The user may approve delegation, assign the batch to the Orchestrator for direct execution, or cancel without executing it.
    """

    tasks: list[TaskSpec] = Field(
        ...,
        min_length=2,
        description=(
            "At least two runnable tasks to delegate concurrently. "
            "Use only for a worthwhile batch of fully independent, parallel-safe tasks with enough complexity "
            "or parallel benefit to justify delegation. Execute single, serial, or trivial tasks directly. "
            "Do not pass a string; pass a list of task objects."
        ),
    )

    @field_validator("tasks", mode="before")
    @classmethod
    def parse_stringified_tasks(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                v = v.strip()
                if not v:
                    return v
                # strict=False allows control chars (e.g. unescaped newlines from LLM)
                parsed = json.loads(v, strict=False)
                # Handle double-encoded JSON string
                if isinstance(parsed, str):
                    parsed = json.loads(parsed, strict=False)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return v


class TeammateManager:
    def __init__(
            self,
            conversation_root: Path | None = None,
            conversation_id: str | None = None,
            history: list[dict[str, Any]] | None = None,
    ):
        self.conversation_root = conversation_root
        self.conversation_id = conversation_id
        self.history_path = (
            conversation_root / SUB_AGENT_HISTORY_FILE
            if conversation_root is not None
            else None
        )
        self.runs_dir = (
            conversation_root / SUB_AGENT_RUNS_DIR
            if conversation_root is not None
            else None
        )
        self.history = list(history or [])
        if conversation_id is not None:
            for record in self.history:
                if record.get("conversation_id") != conversation_id:
                    raise ValueError("Sub-Agent history conversation ID mismatch")

    async def _save_history(self, lock: asyncio.Lock):
        """写入时加锁，保证多子节点并发完成时不会写坏文件"""
        self._validate_storage_paths()
        async with lock:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.history_path.with_name(
                f".{self.history_path.name}.{uuid.uuid4().hex}.tmp"
            )
            async with aiofiles.open(temporary, "w", encoding="utf-8") as f:
                await f.write(json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "conversation_id": self.conversation_id,
                    "records": self.history,
                }, ensure_ascii=False, indent=2))
            temporary.replace(self.history_path)

    def _validate_storage_paths(self) -> None:
        if (
            self.conversation_root is None
            or self.history_path is None
            or self.runs_dir is None
            or self.conversation_id is None
        ):
            raise RuntimeError("No active conversation for Sub-Agent history")
        root = self.conversation_root.resolve()
        sub_agents_dir = self.history_path.parent
        if (
            self.conversation_root.name != self.conversation_id
            or self.conversation_root.is_symlink()
            or self.history_path.is_symlink()
            or sub_agents_dir.is_symlink()
            or self.runs_dir.is_symlink()
            or sub_agents_dir.resolve() != root / "sub_agents"
            or self.runs_dir.resolve() != root / SUB_AGENT_RUNS_DIR
        ):
            raise RuntimeError("Invalid Sub-Agent storage path")

    def _validate_delegation_tasks(self, tasks: Any) -> list[dict]:
        try:
            validated_model = DelegateTasks.model_validate({"tasks": tasks})
            spec_list = validated_model.tasks
        except ValidationError as exc:
            log_error_traceback("DelegateTasks payload validation", exc)
            raise ValueError(f"DelegateTasks.tasks format invalid: {exc.errors()}") from exc

        normalized: list[dict] = []
        seen_ids: set[str] = set()
        unknown_ids: list[str] = []

        for spec in spec_list:
            tid = str(spec.task_id).strip()
            if tid in seen_ids:
                raise ValueError(f"Duplicate task_id in DelegateTasks payload: {tid}")
            seen_ids.add(tid)

            try:
                tasks_module.TASK_MANAGER.get_task(task_id=tid)
            except Exception as exc:
                log_error_traceback(f"DelegateTasks unknown task_id check #{tid}", exc)
                unknown_ids.append(tid)

            normalized.append(
                {
                    "task_id": tid,
                    "role_name": spec.role_name,
                    "context_prompt": spec.context_prompt,
                }
            )

        if unknown_ids:
            raise ValueError(
                f"Unknown task_id(s): {unknown_ids}. "
                "Create tasks in TaskManager first, then call GetRunnableTasks."
            )

        runnable_ids = {t["id"] for t in tasks_module.TASK_MANAGER.get_runnable_tasks()}
        non_runnable = [
            item["task_id"]
            for item in normalized
            if item["task_id"] not in runnable_ids
        ]
        if non_runnable:
            runnable_list = sorted(
                runnable_ids, key=lambda x: (0, int(x)) if x.isdigit() else (1, x)
            )
            raise ValueError(
                "DelegateTasks only accepts runnable tasks from TaskManager.GetRunnableTasks. "
                f"Non-runnable task_id(s): {non_runnable}. Current runnable: {runnable_list}."
            )

        return normalized

    async def delegate_concurrently(self, tasks: list[dict]) -> str:
        if not tasks:
            return "Error: No tasks provided to delegate."
        try:
            tasks = self._validate_delegation_tasks(tasks)
        except Exception as e:
            log_error_traceback("DelegateTasks preflight validation", e)
            return f"Error: {e}"

        # 使用子智能体专用确认窗口，不进入通用 HITL 审批队列
        delegation_items = []
        for task in tasks:
            summary = " ".join(task["context_prompt"].split())
            if len(summary) > 240:
                summary = f"{summary[:237]}..."
            delegation_items.append(
                {
                    "task_id": task["task_id"],
                    "role_name": task["role_name"],
                    "summary": summary,
                }
            )
        with console_lock:
            request_window_attention()
            delegation_action = choose_delegate_tasks_tui(delegation_items)
        if delegation_action == "orchestrator":
            return (
                "Sub-agent delegation declined: the user chose to have the Orchestrator "
                "execute these tasks directly. Do not call DelegateTasks again for this batch."
            )
        if delegation_action != "approve":
            return "Sub-agent delegation cancelled by the user."
        self._validate_storage_paths()
        post_tui(TuiRegion.SUB_AGENT, active=True)
        post_tui(TuiRegion.BACKGROUND, active=True)
        # 1. 创建本次调用的专属文件夹
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"run_{run_timestamp}_{uuid.uuid4().hex[:6]}"
        current_run_dir = self.runs_dir / run_id
        current_run_dir.mkdir(parents=True, exist_ok=True)

        print_formatted_text(
            f"\n[yellow][Orchestrator] 正在并发唤醒 {len(tasks)} 个子节点... 日志目录: {escape(run_id)}[/yellow]\n"
        )

        async def _run_all():
            current_model = get_current_model_config()
            if current_model is None:
                raise RuntimeError("No model configured. Please use /models to configure a model first.")
            local_async_llm_client = _create_async_chat_client(current_model)

            lock = asyncio.Lock()

            async def worker(task_info: dict):
                plan_task_id = task_info["task_id"]
                role = task_info["role_name"]
                original_prompt = task_info["context_prompt"]

                recall_query = build_sub_agent_recall_query(plan_task_id, role, original_prompt)
                recall_result = await recall_long_term_memories(
                    recall_query,
                    source="Sub-Agent 任务预召回",
                    agent_id=f"#{plan_task_id} - {role}",
                )
                prompt = prepend_recalled_memory_to_sub_agent_prompt(
                    original_prompt,
                    recall_result.get("content", "") if isinstance(recall_result, dict) else "",
                )

                runtime_task_id = f"task_{plan_task_id}_{uuid.uuid4().hex[:6]}"
                start_time = datetime.now().isoformat()

                # 为该 Sub-Agent 分配专属的行动日志文件
                log_file_path = current_run_dir / f"{runtime_task_id}_trace.jsonl"

                # 记录初始信息到总的 history
                task_record = {
                    "conversation_id": self.conversation_id,
                    "task_plan_id": tasks_module.TASK_MANAGER._data["epic_id"],
                    "run_id": run_id,
                    "task_id": runtime_task_id,
                    "plan_task_id": plan_task_id,
                    "role": role,
                    "status": "running",
                    "start_time": start_time,
                    "prompt": prompt,
                    "trace_log": str(log_file_path.relative_to(self.conversation_root)),
                }

                async with lock:
                    self.history.append(task_record)
                await self._save_history(lock)

                print_formatted_text(
                    f"[blue]  [Spawn] 子节点 '{escape(str(role))}' 开始工作... (TaskManager #{plan_task_id}) [/blue]"
                )

                try:
                    # 将日志文件路径传入执行沙盒
                    sub_result = await self._sub_agent_loop(
                        plan_task_id, role, prompt, log_file_path, local_async_llm_client
                    )
                    report = sub_result["report"]

                    # 从 report 中解析 COMPLETION_STATUS
                    succeeded = False
                    if "COMPLETION_STATUS: completed" in report:
                        succeeded = True
                    elif "COMPLETION_STATUS: not_completed" in report:
                        succeeded = False
                    else:
                        # 如果没有明确的状态，默认为未完成
                        succeeded = False

                    history_status = "completed" if succeeded else "failed"
                except Exception as exc:
                    log_error_traceback(
                        f"Sub-agent crash: {role} (Task #{plan_task_id})", exc
                    )
                    report = f"Error: Sub-agent crashed - {exc}."
                    succeeded = False
                    history_status = "failed"

                # 任务完成，更新总 history 状态
                async with lock:
                    for record in self.history:
                        if record["task_id"] == runtime_task_id:
                            record["status"] = history_status
                            record["end_time"] = datetime.now().isoformat()
                            record["report"] = report
                await self._save_history(lock)

                print_formatted_text(
                    f"[green]  [Done] 子节点 '{escape(str(role))}' 任务结束 (TaskManager #{plan_task_id}) [/green]"
                )
                return {
                    "task_id": plan_task_id,
                    "role": role,
                    "report": report,
                }

            try:
                coroutines = [worker(t) for t in tasks]
                return await asyncio.gather(*coroutines, return_exceptions=True)
            finally:
                await close_async_llm_client(local_async_llm_client)

        try:
            raw_results = await _run_all()
            results = []
            for idx, res in enumerate(raw_results):
                if isinstance(res, Exception):
                    task_id = tasks[idx]["task_id"]
                    log_error_traceback(
                        f"Asyncio gather exception for Task #{task_id}", res
                    )
                    results.append(
                        {
                            "task_id": task_id,
                            "role": tasks[idx]["role_name"],
                            "report": f"Error: Sub-agent unhandled exception - {res}.",
                        }
                    )
                else:
                    results.append(res)

            print_formatted_text(
                "\n[yellow][Orchestrator] 所有任务已完成，汇总报告已生成。[/yellow]\n"
            )

            final_combined_report = (
                f"### Run ID: {run_id} | Sub-Agents Execution Reports ###\n\n"
            )
            for item in sorted(
                    results,
                    key=lambda x: (
                            int(x["task_id"]) if str(x["task_id"]).isdigit() else str(x["task_id"])
                    ),
            ):
                final_combined_report += (
                    f"==== Task #{item['task_id']} | Role: {item['role']} ====\n"
                    f"{item['report']}\n\n"
                )

            return final_combined_report
        finally:
            post_tui(TuiRegion.SUB_AGENT, active=False)
            post_tui(TuiRegion.BACKGROUND, active=False)

    async def _sub_agent_loop(
            self, plan_task_id: str, role: str, prompt: str, log_file: Path, local_async_llm_client
    ) -> dict:
        """子节点独立的运行沙盒，将每一步决策实时写入 JSONL"""

        current_agent_role.set(f"#{plan_task_id} - {role}")

        # 同步渲染包装器：在线程锁保护下执行 Rich 渲染，防止并发输出交错
        def _safe_render(render_fn, **kwargs):
            with console_lock:
                render_fn(**kwargs)

        # 辅助函数：实时追加日志
        async def append_trace(event_type: str, data: Any):
            async with aiofiles.open(log_file, "a", encoding="utf-8") as f:
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "event": event_type,
                    "data": data,
                }
                await f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        sys_prompt = get_sub_agent_system_prompt(
            role,
            str(_workdir()),
            STARTUP_TERMINAL_LABEL,
            STARTUP_TERMINAL_SOURCE,
        )

        # 记录初始启动状态
        await append_trace(
            "agent_spawned",
            {"role": role, "sys_prompt": sys_prompt, "user_prompt": prompt},
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]

        local_todo = TodoManager()

        mcp_tools, mcp_handlers = GLOBAL_MCP_MANAGER.get_registry_snapshot()
        sub_agent_tools = local_async_llm_client.format_tools(
            COMMON_TOOLS
            + SKILL_TOOLS
            + TODO_TOOLS
            + mcp_tools
        )
        sub_handlers = {
            **COMMON_TOOLS_HANDLERS,
            **SKILL_TOOLS_HANDLERS,
            **mcp_handlers,
            "TodoUpdate": lambda **kw: (
                local_todo.update(kw["todos"]) if "todos" in kw
                else f"Error: TodoUpdate requires 'todos' field, got keys: {list(kw.keys())}"
            ),
        }
        max_steps = 40

        async def _build_completion_report(
                stop_reason: str, executed_steps: int
        ) -> str:
            """生成详细的完成报告，并判断是否应该标记为 completed"""
            todo_snapshot = local_todo.render()
            messages_text = json.dumps(
                strip_native_message_payloads(messages),
                ensure_ascii=False,
                default=str,
                indent=2,
            )
            summary_prompt = get_sub_agent_summary_prompt(
                executed_steps, max_steps, todo_snapshot, messages_text
            )
            fallback_messages = [
                {
                    "role": "system",
                    "content": get_report_assistant_system_prompt(),
                },
                {"role": "user", "content": summary_prompt},
            ]
            try:
                summary_parts = []
                for _ in range(8):
                    stream_result = None
                    async for event in local_async_llm_client.generate_stream(
                        messages=fallback_messages, tools=[]
                    ):
                        if event.get("type") == "done":
                            stream_result = event["result"]
                    if stream_result is None:
                        break
                    if stream_result.text:
                        summary_parts.append(stream_result.text)
                    if getattr(stream_result, "stop_reason", None) != "pause_turn":
                        break
                    fallback_messages.append(stream_result.assistant_message)
                summary_text = "".join(summary_parts).strip()
                if summary_text:
                    return summary_text
            except Exception as exc:
                log_error_traceback(
                    f"Sub-agent fallback summary generation error (Role: {role})", exc
                )

            return (
                "Sub-agent stopped and fallback summary generation failed.\n\n"
                f"Stop reason: {stop_reason}\n"
                f"Executed steps: {executed_steps}/{max_steps}\n\n"
                "COMPLETION_STATUS: not_completed\n"
                "The task is not complete. Continue from existing todo states."
            )

        stop_reason = "step_limit_exhausted"

        for step in range(max_steps):  # 最大 max_steps 步限制
            try:
                start_time = time.perf_counter()
                stream_result = None
                async for event in local_async_llm_client.generate_stream(
                    messages=messages,
                    tools=sub_agent_tools,
                ):
                    if event.get("type") == "done":
                        stream_result = event.get("result")
                        break
                if stream_result is None:
                    raise RuntimeError("Sub-agent stream ended without a final done event.")
                response_time = time.perf_counter() - start_time
            except Exception as e:
                log_error_traceback(f"Sub-agent API generation error (Role: {role})", e)
                await append_trace("api_error", str(e))
                # API 错误也走兜底总结
                final_report = await _build_completion_report(
                    stop_reason=f"api_error: {e}", executed_steps=step
                )
                await append_trace("task_error", final_report)
                return {"report": final_report}

            text_content = stream_result.text
            tool_calls = stream_result.tool_calls
            raw_message = stream_result.assistant_message

            # append assistant message to history
            local_async_llm_client.append_assistant_message(messages, raw_message)

            await append_trace(
                f"step_{step}_llm_output",
                {"text": text_content, "tool_calls": [tc["name"] for tc in tool_calls]},
            )

            # 回显子智能体的文本回复到主控制台（如果启用）
            if get_sub_agent_console():
                kw_args = {
                    'identity': f"#{plan_task_id} - {role}",
                    'text': text_content,
                    'response_time': response_time,
                    'tui_region': TuiRegion.BACKGROUND,
                }
                await asyncio.to_thread(
                    _safe_render, _render_agent_response_message, **kw_args
                )

            has_tool_call = len(tool_calls) > 0
            stop_reason = getattr(stream_result, "stop_reason", None)

            # 处理工具调用
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_id = tc["id"]
                tool_args = tc["arguments"]
                execution_id = TOOL_EXECUTION_HISTORY.start(
                    tool_name,
                    tool_args,
                    tool_call_id=tool_id,
                    source="sub_agent",
                    actor=f"#{plan_task_id} - {role}",
                    task_id=plan_task_id,
                )
                # 回显工具调用参数到主控制台（如果启用）
                if get_sub_agent_console():
                    kw_args = {
                        'identity': f"#{plan_task_id} - {role}",
                        'name': tool_name,
                        'arguments': tool_args,
                        'tui_region': TuiRegion.BACKGROUND,
                    }
                    await asyncio.to_thread(
                        _safe_render, _render_tool_call, **kw_args
                    )

                args = tool_args
                tool_error = False
                try:
                    handler = sub_handlers.get(tool_name)
                    if not handler:
                        tool_error = True
                        output = f"Unknown tool: {tool_name}"
                    elif tool_name in SUB_AGENT_TOOL_MODELS:
                        args = validate_builtin_tool_arguments(
                            tool_name,
                            tool_args,
                            SUB_AGENT_TOOL_MODELS[tool_name],
                        )
                        if inspect.iscoroutinefunction(handler):
                            output = await handler(**args)
                        else:
                            output = await asyncio.to_thread(handler, **args)
                            if inspect.isawaitable(output):
                                output = await output
                        tool_error = isinstance(output, str) and output.startswith("Error:")
                    elif tool_name in mcp_handlers:
                        args = parse_tool_arguments(tool_name, tool_args)
                        if inspect.iscoroutinefunction(handler):
                            output = await handler(**args)
                        else:
                            output = await asyncio.to_thread(handler, **args)
                            if inspect.isawaitable(output):
                                output = await output
                        tool_error = isinstance(output, str) and output.startswith("Error:")
                    else:
                        raise RuntimeError(
                            f"Built-in tool model not registered: {tool_name}"
                        )
                except ToolArgumentValidationError as exc:
                    tool_error = True
                    output = str(exc)
                except Exception as e:
                    tool_error = True
                    log_error_traceback(
                        f"Sub-agent tool execution error (Role: {role}, Tool: {tool_name})",
                        e,
                    )
                    output = f"Error: {e}."
                TOOL_EXECUTION_HISTORY.finish(
                    execution_id,
                    output,
                    status=tool_result_status(is_error=tool_error, output=output),
                    error=str(output) if tool_error else "",
                )
                # 回显工具输出结果到主控制台（如果启用）
                if get_sub_agent_console():
                    kw_args = {
                        'identity': f"#{plan_task_id} - {role}",
                        'name': tool_name,
                        'output': output,
                        'tui_region': TuiRegion.BACKGROUND,
                    }
                    await asyncio.to_thread(
                        _safe_render, _render_tool_output, **kw_args
                    )

                # 记录工具调用的详细结果
                await append_trace(
                    f"step_{step}_tool_execution",
                    {
                        "tool_name": tool_name,
                        "arguments": args,
                        "output": output,
                    },
                )

                tool_result = local_async_llm_client.format_tool_result(
                    tool_id, tool_name, output
                )
                if tool_error:
                    tool_result["is_error"] = True
                messages.append(tool_result)

            # 检查是否应该跳出循环
            if not has_tool_call and stop_reason != "pause_turn":
                stop_reason = "model_returned_no_tool_call"
                break

            # 如果达到最大步数
            if step == max_steps - 1:
                stop_reason = "step_limit_exhausted"
                break

        # 所有情况都走兜底总结机制
        final_report = await _build_completion_report(
            stop_reason=stop_reason, executed_steps=step + 1
        )
        await append_trace("task_completed", final_report)
        return {"report": final_report}
TEAM = TeammateManager()



def activate_conversation(
        conversation_root: Path,
        conversation_id: str,
        history: list[dict[str, Any]] | None = None,
) -> None:
    global TEAM
    TEAM = TeammateManager(conversation_root, conversation_id, history)



def refresh_workspace_paths() -> None:
    global TEAM
    TEAM = TeammateManager()


TEAM_NAMESPACE_TOOLS, TEAM_TOOL_MODELS = build_tool_definitions(DelegateTasks)


TEAM_NAMESPACE = {
    "type": "namespace",
    "name": "Team",
    "description": (
        "Sub-agent delegation tools. DelegateTasks requires a worthwhile batch of at least two tasks and must "
        "be called only after TaskManager topology planning and a fresh GetRunnableTasks query. Each delegated "
        "item must include a runnable task_id. Delegate only when all tasks are fully independent, safe to run "
        "in parallel, and have enough complexity or parallel benefit to justify delegation. Execute single tasks, "
        "serial task chains, and batches of trivial tasks directly in the Orchestrator. Do not delegate tasks that "
        "may write the same file in the same batch; enforce topology order and execute them directly. "
        "Sub-agents are stateless executors and cannot use memory tools, so each context_prompt must be "
        "complete and self-contained with the user request/goal, limits and constraints, allowed scope and "
        "disallowed actions, relevant files/context, expected output, verification evidence, and any project "
        "conventions already known from the current conversation. The system runs one long-term memory "
        "pre-recall before each sub-agent starts and prepends any relevant memory context to that delegated task. "
        "Calling DelegateTasks opens a dedicated confirmation dialog where the user can approve delegation, "
        "assign the batch to the Orchestrator, or cancel it without execution."
    ),
    "tools": TEAM_NAMESPACE_TOOLS,
}

TEAM_TOOLS = TEAM_NAMESPACE_TOOLS


async def _delegate_tasks_handler(tasks, **kwargs):
    return await TEAM.delegate_concurrently(tasks)


TEAM_TOOLS_HANDLERS = {
    "DelegateTasks": _delegate_tasks_handler
}
