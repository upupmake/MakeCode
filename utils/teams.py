import concurrent.futures
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from init import WORKDIR, llm_client
from tools.todo import TodoManager, TODO_TOOLS
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    STARTUP_TERMINAL_SOURCE,
    STARTUP_TERMINAL_TYPE,
)
from utils.skills import SKILL_TOOLS, SKILL_TOOLS_HANDLERS
from utils.tasks import TASK_MANAGER

MAKECODE_DIR = WORKDIR / ".makecode"
TEAM_DIR = MAKECODE_DIR / "team"
RUNS_DIR = TEAM_DIR / "runs"  # 新增：存放每次并发调用的文件夹
STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


class TaskSpec(BaseModel):
    task_id: str = Field(
        ...,
        min_length=1,
        description="Task ID from TaskManager. Must come from GetRunnableTasks before delegation."
    )
    role_name: str = Field(..., description="The role of the sub-agent (e.g., 'Frontend Developer').")
    context_prompt: str = Field(..., description="Detailed instructions and context for this specific sub-agent.")


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
    """
    tasks: list[TaskSpec] = Field(
        ...,
        description=(
            "Runnable tasks to delegate concurrently. "
            "Use only for fully independent, parallel-safe tasks. "
            "Each item must include task_id, role_name, and context_prompt."
        )
    )


class SubmitTaskReport(BaseModel):
    """Submit the final detailed report of your work back to the Orchestrator."""
    report: str = Field(
        ...,
        description="Detailed explanation of what was accomplished, including findings or blockers."
    )


class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

        self.session_id = uuid.uuid4().hex[:8]
        self.history_path = self.dir / f"task_history_{self.session_id}.json"
        self.history = self._load_history()

        # 多线程修改统一配置文件时，必须加锁防冲突
        self._db_lock = threading.Lock()

    def _load_history(self) -> list:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        return []

    def _save_history(self):
        """写入时加锁，保证多子节点并发完成时不会写坏文件"""
        with self._db_lock:
            self.history_path.write_text(
                json.dumps(self.history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    def _set_plan_task_status(self, task_id: str, status: str):
        with self._db_lock:
            TASK_MANAGER.update_task_status(task_id=task_id, status=status)

    def _validate_delegation_tasks(self, tasks: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        seen_ids: set[str] = set()
        unknown_ids: list[str] = []

        for raw in tasks:
            spec = TaskSpec(**raw)
            tid = str(spec.task_id).strip()
            if tid in seen_ids:
                raise ValueError(f"Duplicate task_id in DelegateTasks payload: {tid}")
            seen_ids.add(tid)

            try:
                TASK_MANAGER.get_task(task_id=tid)
            except Exception:
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
        non_runnable = [item["task_id"] for item in normalized if item["task_id"] not in runnable_ids]
        if non_runnable:
            runnable_list = sorted(runnable_ids, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
            raise ValueError(
                "DelegateTasks only accepts runnable tasks from TaskManager.GetRunnableTasks. "
                f"Non-runnable task_id(s): {non_runnable}. Current runnable: {runnable_list}."
            )

        return normalized

    def delegate_concurrently(self, tasks: list[dict]) -> str:
        if not tasks:
            return "Error: No tasks provided to delegate."
        try:
            tasks = self._validate_delegation_tasks(tasks)
        except Exception as e:
            return f"Error: {e}"

        # 1. 创建本次调用的专属文件夹
        run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"run_{run_timestamp}"
        current_run_dir = RUNS_DIR / run_id
        current_run_dir.mkdir(exist_ok=True)

        print(f"\n\033[33m[Orchestrator] 正在并发唤醒 {len(tasks)} 个子节点... 日志目录: {run_id}\033[0m\n")

        results: list[dict] = []

        def worker(task_info: dict):
            plan_task_id = task_info["task_id"]
            role = task_info["role_name"]
            prompt = task_info["context_prompt"]

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
                "trace_log": str(log_file_path.relative_to(WORKDIR))  # 保存相对路径方便查看
            }

            self._set_plan_task_status(plan_task_id, "in_progress")

            with self._db_lock:
                self.history.append(task_record)
            self._save_history()

            print(f"\033[34m  -> [Spawn] 子节点 '{role}' 开始工作... (TaskManager #{plan_task_id})\033[0m")

            try:
                # 将日志文件路径传入执行沙盒
                sub_result = self._sub_agent_loop(role, prompt, log_file_path)
                report = sub_result["report"]
                succeeded = sub_result["status"] == "completed"
                final_plan_status = "completed" if succeeded else "pending"
                self._set_plan_task_status(plan_task_id, final_plan_status)
                history_status = "completed" if succeeded else "failed"
            except Exception as exc:
                from init import log_error_traceback
                log_error_traceback(f"Sub-agent crash: {role} (Task #{plan_task_id})", exc)
                report = f"Error: Sub-agent crashed - {exc}. Check .makecode/error.log for details."
                succeeded = False
                history_status = "failed"
                self._set_plan_task_status(plan_task_id, "pending")

            # 任务完成，更新总 history 状态
            with self._db_lock:
                for record in self.history:
                    if record["task_id"] == runtime_task_id:
                        record["status"] = history_status
                        record["end_time"] = datetime.now().isoformat()
            self._save_history()

            print(f"\033[32m  <- [Done] 子节点 '{role}' 任务结束。\033[0m")
            return {
                "task_id": plan_task_id,
                "role": role,
                "report": report,
                "status": "completed" if succeeded else "failed",
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks), 5)) as executor:
            future_to_task_id = {executor.submit(worker, t): t["task_id"] for t in tasks}
            for future in concurrent.futures.as_completed(future_to_task_id):
                plan_task_id = future_to_task_id[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    from init import log_error_traceback
                    log_error_traceback(f"Thread pool execution error for Task #{plan_task_id}", exc)
                    results.append(
                        {
                            "task_id": plan_task_id,
                            "role": "unknown",
                            "report": f"Error: Sub-agent crashed - {exc}. Check .makecode/error.log for details.",
                            "status": "failed",
                        }
                    )
                    self._set_plan_task_status(plan_task_id, "pending")

        print(f"\n\033[33m[Orchestrator] 所有任务已完成，汇总报告已生成。\033[0m\n")

        final_combined_report = f"### Run ID: {run_id} | Sub-Agents Execution Reports ###\n\n"
        for item in sorted(results,
                           key=lambda x: int(x["task_id"]) if str(x["task_id"]).isdigit() else str(x["task_id"])):
            final_combined_report += (
                f"==== Task #{item['task_id']} | Role: {item['role']} | Status: {item['status']} ====\n"
                f"{item['report']}\n\n"
            )

        return final_combined_report

    def _sub_agent_loop(self, role: str, prompt: str, log_file: Path) -> dict:
        """子节点独立的运行沙盒，将每一步决策实时写入 JSONL"""

        # 辅助函数：实时追加日志
        def append_trace(event_type: str, data: any):
            with open(log_file, "a", encoding="utf-8") as f:
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "event": event_type,
                    "data": data
                }
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        sys_prompt = (
            f"You are a '{role}', working at {WORKDIR}. "
            f"You have been assigned a specific task by the Orchestrator. "
            f"Use available tools to complete the task. "
            f"Your task is independent from sibling sub-agents in this run; do not assume ordering from them. "
            f"For workspace file operations (reading, writing, editing, or text searching), strictly use the File namespace tools (RunRead, RunWrite, RunEdit, RunGrep). Do NOT use terminal commands for these tasks. "
            f"For CLI/build/test tasks, use RunTerminalCommand directly. "
            f"Runtime terminal is fixed at startup: {STARTUP_TERMINAL_LABEL} (source={STARTUP_TERMINAL_SOURCE}). "
            f"Before execution, call 'TodoUpdate' to create a short actionable plan (2-6 items) and keep it updated. "
            f"Use skills tools when domain-specific methods are needed. "
            f"CRITICAL: Once the task is fully completed, you MUST call 'SubmitTaskReport' with outcomes, evidence, and blockers."
        )

        # 记录初始启动状态
        append_trace("agent_spawned", {"role": role, "sys_prompt": sys_prompt, "user_prompt": prompt})

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt}
        ]

        local_todo = TodoManager()

        sub_agent_tools = llm_client.format_tools(COMMON_TOOLS + SKILL_TOOLS + TODO_TOOLS + [
            pydantic_function_tool(SubmitTaskReport)
        ])
        sub_handlers = {
            **COMMON_TOOLS_HANDLERS,
            **SKILL_TOOLS_HANDLERS,
            "TodoUpdate": lambda items, **kwargs: local_todo.update(items)
        }

        final_report = "Error: Sub-agent terminated without submitting a report."

        for step in range(30):  # 最大 30 步限制
            try:
                response = llm_client.generate(
                    messages=messages,
                    tools=sub_agent_tools,
                )
            except Exception as e:
                from init import log_error_traceback
                log_error_traceback(f"Sub-agent API generation error (Role: {role})", e)
                append_trace("api_error", str(e))
                return {"status": "failed", "report": f"API Error in sub-agent: {e}. Check .makecode/error.log for details."}

            text_content, tool_calls, raw_message = llm_client.parse_response(response)
            
            # append assistant message to history
            llm_client.append_assistant_message(messages, raw_message)
            
            append_trace(f"step_{step}_llm_output", {
                "text": text_content,
                "tool_calls": [tc["name"] for tc in tool_calls]
            })

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
                    append_trace("task_completed", final_report)
                    task_completed = True
                    # Need to append the tool result to close the tool call loop even if breaking
                    messages.append(llm_client.format_tool_result(tool_id, tool_name, "Task submitted"))
                    break

                try:
                    handler = sub_handlers.get(tool_name)
                    if handler:
                        if isinstance(tool_args, str):
                            args = json.loads(tool_args) if tool_args.strip() else {}
                        else:
                            args = tool_args or {}
                        output = handler(**args)
                    else:
                        output = f"Unknown tool: {tool_name}"
                except Exception as e:
                    from init import log_error_traceback
                    log_error_traceback(f"Sub-agent tool execution error (Role: {role}, Tool: {tool_name})", e)
                    output = f"Error: {e}. Check .makecode/error.log for details."

                # 记录工具调用的详细结果
                append_trace(f"step_{step}_tool_execution", {
                    "tool_name": tool_name,
                    "arguments": args if 'args' in locals() else tool_args,
                    "output": output
                })

                messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

            if task_completed or not has_tool_call:
                break

        if final_report.startswith("Error:"):
            return {"status": "failed", "report": final_report}
        return {"status": "completed", "report": final_report}


TEAM = TeammateManager(TEAM_DIR)

TEAM_NAMESPACE_TOOLS = [
    pydantic_function_tool(DelegateTasks)
]

TEAM_NAMESPACE = {
    "type": "namespace",
    "name": "Team",
    "description": (
        "Sub-agent delegation tools. DelegateTasks must be called only after TaskManager topology planning "
        "and a fresh GetRunnableTasks query. Each delegated item must include a runnable task_id. "
        "Only delegate when tasks are fully independent and safe to run in parallel."
    ),
    "tools": TEAM_NAMESPACE_TOOLS,
}

TEAM_TOOLS = [
    pydantic_function_tool(DelegateTasks)
]

TEAM_TOOLS_HANDLERS = {
    "DelegateTasks": lambda tasks, **kwargs: TEAM.delegate_concurrently(tasks)
}
