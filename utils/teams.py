import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles
from openai import AsyncOpenAI, pydantic_function_tool
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML
from pydantic import BaseModel, Field, ValidationError, model_validator, field_validator
from typing import Any

from init import (
    WORKDIR,
    log_error_traceback,
    API_KEY,
    BASE_URL,
    MODEL,
    API_STANDARD,
)
from prompts import (
    get_sub_agent_system_prompt,
    get_sub_agent_summary_prompt,
    get_report_assistant_system_prompt,
)
from tools.todo import TodoManager, TODO_TOOLS
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    STARTUP_TERMINAL_SOURCE,
    STARTUP_TERMINAL_TYPE,
    run_read,
    run_write,
    run_edit,
)
from utils.file_access import AgentFileAccess
from utils.llm_client import AsyncChatAPIClient, AsyncResponseAPIClient
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.skills import (
    SKILL_TOOLS,
    SKILL_TOOLS_HANDLERS,
)
from utils.tasks import TASK_MANAGER

MAKECODE_DIR = WORKDIR / ".makecode"
TEAM_DIR = MAKECODE_DIR / "team"
RUNS_DIR = TEAM_DIR / "runs"  # 新增：存放每次并发调用的文件夹
STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


class TaskSpec(BaseModel):
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
        description="Detailed instructions and context for this specific sub-agent.",
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


class DelegateTasks(BaseModel):
    """
    Delegate multiple runnable TaskManager tasks to specialized sub-agents concurrently.
    HARD RULES:
    1) You MUST use TaskManager topology planning first (CreateTask/UpdateTaskDependencies).
    2) You MUST call GetRunnableTasks immediately before this tool.
    3) Every item.task_id MUST be in the current runnable frontier.
    4) Non-runnable task IDs are rejected.
    5) Use this tool only when tasks are fully independent and truly parallel-safe:
       - no inter-task ordering dependency
       - no shared mutable file/state requiring serialization
       - each task can complete end-to-end without waiting on sibling tasks
       - avoid batching tasks that may write the same file concurrently
    6) Sub-agents are stateless. Each context_prompt must be complete and self-contained.
    """

    tasks: list[TaskSpec] = Field(
        ...,
        description=(
            "Runnable tasks to delegate concurrently. "
            "Use only for fully independent, parallel-safe tasks. "
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
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return v


class SubmitTaskReport(BaseModel):
    """Submit the final detailed report of your work back to the Orchestrator."""

    report: str = Field(
        ...,
        description="Detailed explanation of what was accomplished, including findings or blockers.",
    )


class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

        self.session_id = uuid.uuid4().hex[:8]
        self.history_path = self.dir / f"task_history_{self.session_id}.json"
        self.history = self._load_history()

    def _load_history(self) -> list:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        return []

    async def _save_history(self, lock: asyncio.Lock):
        """写入时加锁，保证多子节点并发完成时不会写坏文件"""
        async with lock:
            async with aiofiles.open(self.history_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(self.history, ensure_ascii=False, indent=2))

    async def _set_plan_task_status(
        self, task_id: str, status: str, lock: asyncio.Lock
    ):
        async with lock:
            TASK_MANAGER.update_task_status(task_id=task_id, status=status)

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
                TASK_MANAGER.get_task(task_id=tid)
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

        runnable_ids = {t["id"] for t in TASK_MANAGER.get_runnable_tasks()}
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

    async def _get_last_failed_context(
        self, plan_task_id: str, lock: asyncio.Lock
    ) -> str:
        last_record = None
        async with lock:
            for record in reversed(self.history):
                if record.get("plan_task_id") == plan_task_id:
                    last_record = record
                    break

        if not last_record or last_record.get("status") == "completed":
            return ""

        trace_log_path = WORKDIR / last_record.get("trace_log", "")
        if not trace_log_path.exists():
            return ""

        formatted_log = [
            f"\n### PREVIOUS ATTEMPT LOG (Task #{plan_task_id}) ###",
            "A previous agent attempted this task but did not finish successfully. Below is the complete trace of their actions:\n",
        ]

        try:
            async with aiofiles.open(trace_log_path, "r", encoding="utf-8") as f:
                async for line in f:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    evt_type = event.get("event")
                    data = event.get("data", {})
                    timestamp = event.get("timestamp")

                    formatted_log.append(f"--- [{timestamp}] Event: {evt_type} ---")
                    if evt_type.endswith("_llm_output"):
                        text = data.get("text", "")
                        tools = data.get("tool_calls", [])
                        if text:
                            formatted_log.append(f"Thoughts:\n{text}")
                        if tools:
                            formatted_log.append(f"Tool Intent: {tools}")
                    elif evt_type.endswith("_tool_execution"):
                        t_name = data.get("tool_name", "")
                        t_args = data.get("arguments", {})
                        t_out = data.get("output", "")
                        formatted_log.append(f"Tool Call: {t_name}")
                        formatted_log.append(
                            f"Arguments:\n{json.dumps(t_args, ensure_ascii=False, indent=2)}"
                        )
                        formatted_log.append(f"Result:\n{t_out}")
                    else:
                        if isinstance(data, dict):
                            formatted_log.append(
                                json.dumps(data, ensure_ascii=False, indent=2)
                            )
                        else:
                            formatted_log.append(str(data))
                    formatted_log.append("")
        except Exception as e:
            return f"\n[Error reading previous trace log: {e}]\n"

        formatted_log.append("### END OF PREVIOUS ATTEMPT LOG ###")
        formatted_log.append(
            "Please resume the task from where it left off, avoiding the errors that caused the previous failure.\n"
        )
        return "\n".join(formatted_log)

    def delegate_concurrently(self, tasks: list[dict]) -> str:
        if not tasks:
            return "Error: No tasks provided to delegate."
        try:
            tasks = self._validate_delegation_tasks(tasks)
        except Exception as e:
            log_error_traceback("DelegateTasks preflight validation", e)
            return f"Error: {e}"

        # 1. 创建本次调用的专属文件夹
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"run_{run_timestamp}"
        current_run_dir = RUNS_DIR / run_id
        current_run_dir.mkdir(exist_ok=True)

        print_formatted_text(
            HTML(
                f"\n<ansiyellow>[Orchestrator] 正在并发唤醒 {len(tasks)} 个子节点... 日志目录: {run_id}</ansiyellow>\n"
            )
        )

        async def _run_all():
            async_client = AsyncOpenAI(
                base_url=BASE_URL, api_key=API_KEY, max_retries=2
            )
            if API_STANDARD == "chat":
                local_async_llm_client = AsyncChatAPIClient(async_client, MODEL)
            else:
                local_async_llm_client = AsyncResponseAPIClient(async_client, MODEL)

            lock = asyncio.Lock()

            async def worker(task_info: dict):
                plan_task_id = task_info["task_id"]
                role = task_info["role_name"]
                prompt = task_info["context_prompt"]

                previous_context = await self._get_last_failed_context(
                    plan_task_id, lock
                )
                if previous_context:
                    prompt = f"{prompt}\n\n{previous_context}"
                    print_formatted_text(
                        HTML(
                            f"<ansimagenta>  [Recovery] 发现子节点 '{role}' (Task #{plan_task_id}) 之前的失败记录，已加载并注入到新任务的上下文中。</ansimagenta>"
                        )
                    )

                runtime_task_id = f"task_{plan_task_id}_{uuid.uuid4().hex[:6]}"
                start_time = datetime.now().isoformat()

                # 为该 Sub-Agent 分配专属的行动日志文件
                log_file_path = current_run_dir / f"{runtime_task_id}_trace.jsonl"

                # 记录初始信息到总的 history
                task_record = {
                    "run_id": run_id,
                    "task_id": runtime_task_id,
                    "plan_task_id": plan_task_id,
                    "role": role,
                    "status": "running",
                    "start_time": start_time,
                    "prompt": prompt,
                    "trace_log": str(
                        log_file_path.relative_to(WORKDIR)
                    ),  # 保存相对路径方便查看
                }

                await self._set_plan_task_status(plan_task_id, "in_progress", lock)

                async with lock:
                    self.history.append(task_record)
                await self._save_history(lock)

                print_formatted_text(
                    HTML(
                        f"<ansiblue>  [Spawn] 子节点 '{role}' 开始工作... (TaskManager #{plan_task_id})</ansiblue>"
                    )
                )

                try:
                    # 将日志文件路径传入执行沙盒
                    sub_result = await self._sub_agent_loop(
                        role, prompt, log_file_path, local_async_llm_client
                    )
                    report = sub_result["report"]
                    succeeded = sub_result["status"] == "completed"
                    final_plan_status = "completed" if succeeded else "pending"
                    await self._set_plan_task_status(
                        plan_task_id, final_plan_status, lock
                    )
                    history_status = "completed" if succeeded else "failed"
                except Exception as exc:
                    log_error_traceback(
                        f"Sub-agent crash: {role} (Task #{plan_task_id})", exc
                    )
                    report = f"Error: Sub-agent crashed - {exc}."
                    succeeded = False
                    history_status = "failed"
                    await self._set_plan_task_status(plan_task_id, "pending", lock)

                # 任务完成，更新总 history 状态
                async with lock:
                    for record in self.history:
                        if record["task_id"] == runtime_task_id:
                            record["status"] = history_status
                            record["end_time"] = datetime.now().isoformat()
                await self._save_history(lock)

                print_formatted_text(
                    HTML(f"<ansigreen>  [Done] 子节点 '{role}' 任务结束。</ansigreen>")
                )
                return {
                    "task_id": plan_task_id,
                    "role": role,
                    "report": report,
                    "status": "completed" if succeeded else "failed",
                }

            try:
                coroutines = [worker(t) for t in tasks]
                return await asyncio.gather(*coroutines, return_exceptions=True)
            finally:
                await async_client.close()

        raw_results = asyncio.run(_run_all())
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
                        "status": "failed",
                    }
                )
            else:
                results.append(res)

        print_formatted_text(
            HTML(
                f"\n<ansiyellow>[Orchestrator] 所有任务已完成，汇总报告已生成。</ansiyellow>\n"
            )
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
                f"==== Task #{item['task_id']} | Role: {item['role']} | Status: {item['status']} ====\n"
                f"{item['report']}\n\n"
            )

        return final_combined_report

    async def _sub_agent_loop(
        self, role: str, prompt: str, log_file: Path, local_async_llm_client
    ) -> dict:
        """子节点独立的运行沙盒，将每一步决策实时写入 JSONL"""

        # 辅助函数：实时追加日志
        async def append_trace(event_type: str, data: any):
            async with aiofiles.open(log_file, "a", encoding="utf-8") as f:
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "event": event_type,
                    "data": data,
                }
                await f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        sys_prompt = get_sub_agent_system_prompt(
            role,
            WORKDIR,
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

        sub_agent_tools = local_async_llm_client.format_tools(
            COMMON_TOOLS
            + SKILL_TOOLS
            + TODO_TOOLS
            + [pydantic_function_tool(SubmitTaskReport)]
            + GLOBAL_MCP_MANAGER.get_tools()
        )
        agent_access = AgentFileAccess()

        sub_handlers = {
            **COMMON_TOOLS_HANDLERS,
            **SKILL_TOOLS_HANDLERS,
            **GLOBAL_MCP_MANAGER.get_handlers(),
            "TodoUpdate": lambda items, **kwargs: local_todo.update(items),
            "RunRead": lambda path, start=None, end=None, **kwargs: run_read(
                path, start, end, agent_access
            ),
            "RunWrite": lambda path, content, **kwargs: run_write(
                path, content, agent_access
            ),
            "RunEdit": lambda path, edits, **kwargs: run_edit(
                path, edits, agent_access
            ),
        }
        max_steps = 40

        async def _build_incomplete_report(
            stop_reason: str, executed_steps: int
        ) -> str:
            todo_snapshot = local_todo.render()
            messages_text = json.dumps(
                messages, ensure_ascii=False, default=str, indent=2
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
                fallback_response = await local_async_llm_client.generate(
                    messages=fallback_messages, tools=[]
                )
                summary_text, _, _ = local_async_llm_client.parse_response(
                    fallback_response
                )
                summary_text = (summary_text or "").strip()
                if summary_text:
                    return summary_text
            except Exception as exc:
                log_error_traceback(
                    f"Sub-agent fallback summary generation error (Role: {role})", exc
                )

            return (
                "Sub-agent stopped before formal completion and fallback summary generation failed.\n\n"
                f"Stop reason: {stop_reason}\n"
                f"Executed steps: {executed_steps}/{max_steps}\n\n"
                "The task is not complete. Continue from existing todo states and submit a final report."
            )

        final_report = "Error: Sub-agent terminated without submitting a report."
        stop_reason = "step_limit_exhausted_without_submit"

        for step in range(max_steps):  # 最大 max_steps 步限制
            try:
                response = await local_async_llm_client.generate(
                    messages=messages,
                    tools=sub_agent_tools,
                )
            except Exception as e:
                log_error_traceback(f"Sub-agent API generation error (Role: {role})", e)
                await append_trace("api_error", str(e))
                return {
                    "status": "failed",
                    "report": f"API Error in sub-agent: {e}.",
                }

            text_content, tool_calls, raw_message = (
                local_async_llm_client.parse_response(response)
            )

            # append assistant message to history
            local_async_llm_client.append_assistant_message(messages, raw_message)

            await append_trace(
                f"step_{step}_llm_output",
                {"text": text_content, "tool_calls": [tc["name"] for tc in tool_calls]},
            )

            has_tool_call = len(tool_calls) > 0
            task_completed = False

            for tc in tool_calls:
                tool_name = tc["name"]
                tool_id = tc["id"]
                tool_args = tc["arguments"]

                if tool_name == "SubmitTaskReport":
                    if isinstance(tool_args, str):
                        args = json.loads(tool_args) if tool_args.strip() else {}
                    else:
                        args = tool_args or {}
                    final_report = args.get("report", "No report provided.")
                    await append_trace("task_completed", final_report)
                    task_completed = True
                    # Need to append the tool result to close the tool call loop even if breaking
                    messages.append(
                        local_async_llm_client.format_tool_result(
                            tool_id, tool_name, "Task submitted"
                        )
                    )
                    break

                try:
                    handler = sub_handlers.get(tool_name)
                    if handler:
                        if isinstance(tool_args, str):
                            args = json.loads(tool_args) if tool_args.strip() else {}
                        else:
                            args = tool_args or {}

                        output = await asyncio.to_thread(handler, **args)
                    else:
                        output = f"Unknown tool: {tool_name}"
                except Exception as e:
                    log_error_traceback(
                        f"Sub-agent tool execution error (Role: {role}, Tool: {tool_name})",
                        e,
                    )
                    output = f"Error: {e}."

                # 记录工具调用的详细结果
                await append_trace(
                    f"step_{step}_tool_execution",
                    {
                        "tool_name": tool_name,
                        "arguments": args if "args" in locals() else tool_args,
                        "output": output,
                    },
                )

                messages.append(
                    local_async_llm_client.format_tool_result(
                        tool_id, tool_name, output
                    )
                )

            if task_completed or not has_tool_call:
                if not task_completed and not has_tool_call:
                    stop_reason = "model_returned_no_tool_call_before_submit"
                break

        if final_report.startswith("Error:"):
            final_report = await _build_incomplete_report(
                stop_reason=stop_reason, executed_steps=max_steps
            )
            await append_trace("task_incomplete", final_report)
            return {"status": "failed", "report": final_report}
        return {"status": "completed", "report": final_report}


TEAM = TeammateManager(TEAM_DIR)


def list_team_histories() -> list[Path]:
    if not TEAM_DIR.exists():
        return []
    files = list(TEAM_DIR.glob("task_history_*.json"))
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files


def load_team_history(filepath: Path) -> list:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    TEAM.history_path = filepath
    TEAM.session_id = filepath.stem.split("_")[-1]
    TEAM.history = data
    return data


TEAM_NAMESPACE_TOOLS = [pydantic_function_tool(DelegateTasks)]

TEAM_NAMESPACE = {
    "type": "namespace",
    "name": "Team",
    "description": (
        "Sub-agent delegation tools. DelegateTasks must be called only after TaskManager topology planning "
        "and a fresh GetRunnableTasks query. Each delegated item must include a runnable task_id. "
        "Only delegate when tasks are fully independent and safe to run in parallel. "
        "Do not delegate tasks that may write the same file in the same batch; enforce topology order first. "
        "Sub-agents are stateless across runs, so each delegated item's context_prompt must be complete and self-contained."
    ),
    "tools": TEAM_NAMESPACE_TOOLS,
}

TEAM_TOOLS = [pydantic_function_tool(DelegateTasks)]

TEAM_TOOLS_HANDLERS = {
    "DelegateTasks": lambda tasks, **kwargs: TEAM.delegate_concurrently(tasks)
}
