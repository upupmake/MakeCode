import json
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

from rich.text import Text
from pydantic import Field, field_validator

from init import log_error_traceback
from utils.conversations import SCHEMA_VERSION, TASK_PLAN_FILE
from utils.hitl import check_permission
from utils.tool_validation import ToolArgumentsModel, build_tool_definitions


VALID_STATUS = {
    "pending",
    "completed",
}


class TaskToCreate(ToolArgumentsModel):
    subject: str = Field(
        ..., min_length=1, description="Task title, concise and action-oriented."
    )
    description: str = Field(
        default="", description="Optional detailed description for the task."
    )
    depend_on: list[str] = Field(
        default_factory=list, description="IDs of tasks this task depends on."
    )
    status: Literal["pending", "completed"] = Field(
        default="pending", description="Initial task status."
    )

    @field_validator("depend_on", mode="before")
    @classmethod
    def parse_stringified_deps(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                v = v.strip()
                if not v:
                    return v
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    v = parsed
                else:
                    return v
            except json.JSONDecodeError:
                pass

        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, str):
                    try:
                        item_parsed = json.loads(item.strip())
                        if isinstance(item_parsed, dict):
                            item = item_parsed
                    except json.JSONDecodeError:
                        pass

                if isinstance(item, dict):
                    if "task_id" in item:
                        res.append(str(item["task_id"]))
                    elif "id" in item:
                        res.append(str(item["id"]))
                    else:
                        res.append(str(item))
                elif item is not None:
                    res.append(str(item))
            return res
        return v


class CreateTasks(ToolArgumentsModel):
    """
    Create one or more new tasks in the topology plan.

    CONSTRAINTS:
    - `tasks` must contain at least one task.
    - Each `subject` is required and cannot be empty.
    - `depend_on` can only reference task IDs that existed before this call; tasks
      created in the same batch do not have IDs yet and cannot depend on each other here.
    - To add dependencies among tasks from the same batch, create them first, then call
      UpdateTasksDependencies with their assigned IDs.
    - MUST NOT create tasks that edit the same file without topology ordering.
    """

    tasks: list[TaskToCreate] = Field(
        ..., min_length=1, description="Tasks to create. task_id values are auto-generated."
    )


class TaskStatusUpdate(ToolArgumentsModel):
    task_id: str = Field(..., min_length=1, description="Target task ID.")
    status: Literal["pending", "completed"] = Field(
        ..., description="New status for the task."
    )


class UpdateTasksStatus(ToolArgumentsModel):
    """
    Update one or more task statuses atomically.
    Constraints:
    - `tasks` must contain at least one update
    - every task must exist and occur only once
    - each status must be one of pending/completed
    """

    tasks: list[TaskStatusUpdate] = Field(
        ..., min_length=1, description="Task status updates."
    )


class TaskDependenciesUpdate(ToolArgumentsModel):
    task_id: str = Field(..., min_length=1, description="Target task ID.")
    depend_on: list[str] = Field(
        default_factory=list, description="New full dependency list."
    )

    @field_validator("depend_on", mode="before")
    @classmethod
    def parse_stringified_deps(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                v = v.strip()
                if not v:
                    return v
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    v = parsed
                else:
                    return v
            except json.JSONDecodeError:
                pass

        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, str):
                    try:
                        item_parsed = json.loads(item.strip())
                        if isinstance(item_parsed, dict):
                            item = item_parsed
                    except json.JSONDecodeError:
                        pass

                if isinstance(item, dict):
                    if "task_id" in item:
                        res.append(str(item["task_id"]))
                    elif "id" in item:
                        res.append(str(item["id"]))
                    else:
                        res.append(str(item))
                elif item is not None:
                    res.append(str(item))
            return res
        return v


class UpdateTasksDependencies(ToolArgumentsModel):
    """
    Rewrite one or more task dependency lists atomically.
    Constraints:
    - `tasks` must contain at least one update
    - every task must exist and occur only once
    - all dependencies must exist
    - tasks cannot depend on themselves
    - all updates together must pass topology validation
    """

    tasks: list[TaskDependenciesUpdate] = Field(
        ..., min_length=1, description="Task dependency updates."
    )


class TaskContentUpdate(ToolArgumentsModel):
    task_id: str = Field(..., min_length=1, description="Target task ID.")
    subject: str = Field(..., min_length=1, description="New task title, concise and action-oriented.")
    description: str = Field(default="", description="New detailed description for the task.")


class UpdateTasksContent(ToolArgumentsModel):
    """
    Update one or more task subjects and descriptions atomically.
    Constraints:
    - `tasks` must contain at least one update
    - every task must exist and occur only once
    - subjects cannot be empty
    """

    tasks: list[TaskContentUpdate] = Field(
        ..., min_length=1, description="Task content updates."
    )


class DeleteAllTasks(ToolArgumentsModel):
    """
    DANGER: Delete ALL tasks in the current topology plan.
    Use this ONLY when you need to completely restart the planning phase from scratch.
    You MUST provide confirm=True to execute this action.
    """
    confirm: bool = Field(
        ...,
        description="Must be set to True to confirm the deletion of all tasks."
    )


class GetRunnableTasks(ToolArgumentsModel):
    """
    Get current runnable frontier tasks.
    Runnable means:
    - status is `pending`
    - all dependencies are `completed`
    - completed tasks are excluded from topology consideration
    Usage rule:
    - Call this immediately before DelegateTasks and only delegate tasks returned here.
    """


class GetTaskTable(ToolArgumentsModel):
    """
    Get LLM-friendly detailed task table.
    Returns a compact structured payload with summary + rows,
    designed for direct model context consumption.
    """


class TaskManager:
    """
    Agent-facing topology task manager.
    Per-task APIs:
      1) create_tasks
      2) update_tasks_status
      3) update_tasks_dependencies
      4) get_task
    Manager APIs:
      5) get_runnable_tasks
      6) get_task_table
    """

    def __init__(
            self,
            conversation_root: Path | None = None,
            conversation_id: str | None = None,
            data: dict[str, Any] | None = None,
    ):
        self.conversation_root = conversation_root
        self.conversation_id = conversation_id
        self.path = conversation_root / TASK_PLAN_FILE if conversation_root is not None else None
        if data is not None:
            self._validate_loaded_data(data, conversation_id)
            self._data = data
            self._validate_topology()
            return
        self._data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "conversation_id": conversation_id,
            "epic_id": uuid.uuid4().hex[:8],
            "next_id": 1,
            "tasks": {},
        }

    def _save(self) -> None:
        self._validate_storage_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self.path)
        render_task_pane()

    def _validate_storage_path(self) -> None:
        if self.path is None or self.conversation_root is None or self.conversation_id is None:
            raise RuntimeError("No active conversation for TaskManager")
        if (
            self.conversation_root.name != self.conversation_id
            or self.conversation_root.is_symlink()
            or self.path.is_symlink()
            or self.path.parent.resolve() != self.conversation_root.resolve()
        ):
            raise RuntimeError("Invalid TaskManager storage path")

    @classmethod
    def _validate_loaded_data(cls, data: dict[str, Any], conversation_id: str | None) -> None:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported task plan schema")
        if data.get("conversation_id") != conversation_id:
            raise ValueError("Task plan conversation ID mismatch")
        if not isinstance(data.get("epic_id"), str) or not data["epic_id"]:
            raise ValueError("Invalid task plan epic_id")
        if not isinstance(data.get("next_id"), int) or data["next_id"] < 1:
            raise ValueError("Invalid task plan next_id")
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            raise ValueError("Invalid task plan tasks")
        numeric_ids = []
        for task_id, task in tasks.items():
            if (
                not isinstance(task_id, str)
                or not task_id.isdigit()
                or int(task_id) < 1
                or not isinstance(task, dict)
                or task.get("id") != task_id
            ):
                raise ValueError("Invalid task plan task identity")
            numeric_ids.append(int(task_id))
            if not isinstance(task.get("subject"), str) or not task["subject"].strip():
                raise ValueError(f"Invalid task subject: {task_id}")
            if not isinstance(task.get("description"), str):
                raise ValueError(f"Invalid task description: {task_id}")
            if task.get("status") not in VALID_STATUS:
                raise ValueError(f"Invalid task status: {task_id}")
            depend_on = task.get("depend_on")
            if not isinstance(depend_on, list) or not all(isinstance(dep, str) for dep in depend_on):
                raise ValueError(f"Invalid task dependencies: {task_id}")
            if any(dep not in tasks for dep in depend_on):
                raise ValueError(f"Task dependencies not found: {task_id}")
        if numeric_ids and data["next_id"] <= max(numeric_ids):
            raise ValueError("Invalid task plan next_id")

    @staticmethod
    def _id_str(task_id: str | int) -> str:
        return str(task_id)

    @staticmethod
    def _id_sort_key(task_id: str) -> tuple[int, str]:
        return (0, f"{int(task_id):020d}") if task_id.isdigit() else (1, task_id)

    def _ensure_task_exists(self, task_id: str | int) -> str:
        tid = self._id_str(task_id)
        if tid not in self._data["tasks"]:
            raise ValueError(f"Task {task_id} not found")
        return tid

    def _ensure_tasks_exist(self, task_ids: list[str | int]) -> list[str]:
        # Filter out empty strings that LLM might mistakenly pass
        ids = [self._id_str(x).strip() for x in task_ids if self._id_str(x).strip()]
        missing = [x for x in ids if x not in self._data["tasks"]]
        if missing:
            raise ValueError(f"Tasks not found: {missing}")
        return ids

    def _task(self, task_id: str | int) -> dict[str, Any]:
        return self._data["tasks"][self._ensure_task_exists(task_id)]

    def _validate_status(self, status: str) -> None:
        if status not in VALID_STATUS:
            raise ValueError(
                f"Invalid status '{status}', must be one of {sorted(VALID_STATUS)}"
            )

    def _active_task_ids(self) -> set[str]:
        return {
            tid
            for tid, task in self._data["tasks"].items()
            if task["status"] != "completed"
        }

    def _validate_topology(self) -> None:
        """
        Validate DAG among active tasks.
        Completed tasks are excluded from topology calculation.
        """
        tasks = self._data["tasks"]
        active = self._active_task_ids()

        # Check for self-dependency
        for tid in active:
            if tid in tasks[tid].get("depend_on", []):
                raise ValueError(f"Task {tid} cannot depend on itself")

        indegree = {tid: 0 for tid in active}
        graph: dict[str, list[str]] = defaultdict(list)
        for tid in active:
            for dep in tasks[tid].get("depend_on", []):
                if dep in active:
                    graph[dep].append(tid)
                    indegree[tid] += 1

        q = deque(
            sorted(
                [tid for tid, deg in indegree.items() if deg == 0],
                key=self._id_sort_key,
            )
        )
        visited = 0
        while q:
            cur = q.popleft()
            visited += 1
            for nxt in graph.get(cur, []):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)

        if visited != len(active):
            cycle_nodes = sorted(
                [tid for tid, deg in indegree.items() if deg > 0], key=self._id_sort_key
            )
            raise ValueError(f"Cycle detected among active tasks: {cycle_nodes}")

    # -------- Per-task APIs --------
    def create_tasks(self, tasks: Any, **kwargs) -> list[dict[str, Any]]:
        try:
            validated_model = CreateTasks.model_validate({"tasks": tasks})
            validated_tasks = validated_model.tasks
            prepared_tasks = []
            next_id = self._data["next_id"]
            for offset, item in enumerate(validated_tasks):
                self._validate_status(item.status)
                dep_ids = self._ensure_tasks_exist(item.depend_on)
                task_id = str(next_id + offset)
                if task_id in dep_ids:
                    raise ValueError("Task cannot depend on itself")
                prepared_tasks.append({
                    "id": task_id,
                    "subject": item.subject,
                    "description": item.description,
                    "status": item.status,
                    "depend_on": sorted(set(dep_ids), key=self._id_sort_key),
                })
        except Exception as exc:
            log_error_traceback("CreateTasks validation", exc)
            raise ValueError(f"CreateTasks parameters invalid: {exc}") from exc

        for task in prepared_tasks:
            self._data["tasks"][task["id"]] = task
        self._data["next_id"] += len(prepared_tasks)

        try:
            self._validate_topology()
        except ValueError:
            for task in prepared_tasks:
                del self._data["tasks"][task["id"]]
            self._data["next_id"] -= len(prepared_tasks)
            raise

        self._save()
        return prepared_tasks

    def update_tasks_status(self, tasks: Any, **kwargs) -> list[dict[str, Any]]:
        try:
            validated_model = UpdateTasksStatus.model_validate({"tasks": tasks})
            updates = validated_model.tasks
            task_ids = self._ensure_tasks_exist(
                [self._id_str(item.task_id) for item in updates]
            )
            if len(task_ids) != len(updates):
                raise ValueError("task_id cannot be empty")
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("Each task_id can only occur once")
        except Exception as exc:
            log_error_traceback("UpdateTasksStatus validation", exc)
            raise ValueError(f"UpdateTasksStatus parameters invalid: {exc}") from exc

        old_statuses = {
            task_id: self._data["tasks"][task_id]["status"] for task_id in task_ids
        }
        for item in updates:
            self._data["tasks"][self._id_str(item.task_id)]["status"] = item.status

        try:
            for task_id in task_ids:
                task = self._data["tasks"][task_id]
                if task["status"] != "completed":
                    continue
                incomplete = [
                    dep for dep in task.get("depend_on", [])
                    if self._data["tasks"][dep]["status"] != "completed"
                ]
                if incomplete:
                    raise ValueError(
                        f"Cannot complete task '{task_id}': "
                        f"dependencies {incomplete} are not completed."
                    )
        except ValueError:
            for task_id, status in old_statuses.items():
                self._data["tasks"][task_id]["status"] = status
            raise

        self._save()
        return [self._data["tasks"][task_id] for task_id in task_ids]

    def update_tasks_dependencies(self, tasks: Any, **kwargs) -> list[dict[str, Any]]:
        try:
            updates = UpdateTasksDependencies.model_validate({"tasks": tasks}).tasks
            task_ids = self._ensure_tasks_exist([item.task_id for item in updates])
            if len(task_ids) != len(updates):
                raise ValueError("task_id cannot be empty")
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("Each task_id can only occur once")
            prepared = []
            for item, task_id in zip(updates, task_ids):
                dep_ids = self._ensure_tasks_exist(item.depend_on)
                if task_id in dep_ids:
                    raise ValueError(f"Task {task_id} cannot depend on itself")
                prepared.append((task_id, sorted(set(dep_ids), key=self._id_sort_key)))
        except Exception as exc:
            log_error_traceback("UpdateTasksDependencies validation", exc)
            raise ValueError(f"UpdateTasksDependencies parameters invalid: {exc}") from exc

        old_dependencies = {
            task_id: self._data["tasks"][task_id].get("depend_on", [])
            for task_id in task_ids
        }
        for task_id, depend_on in prepared:
            self._data["tasks"][task_id]["depend_on"] = depend_on

        try:
            self._validate_topology()
        except ValueError:
            for task_id, depend_on in old_dependencies.items():
                self._data["tasks"][task_id]["depend_on"] = depend_on
            raise

        self._save()
        return [self._data["tasks"][task_id] for task_id in task_ids]

    def get_task(self, task_id: str | int, **kwargs) -> dict[str, Any]:
        return self._task(task_id)

    def update_tasks_content(self, tasks: Any, **kwargs) -> list[dict[str, Any]]:
        try:
            updates = UpdateTasksContent.model_validate({"tasks": tasks}).tasks
            task_ids = self._ensure_tasks_exist([item.task_id for item in updates])
            if len(task_ids) != len(updates):
                raise ValueError("task_id cannot be empty")
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("Each task_id can only occur once")
            subjects = [item.subject.strip() for item in updates]
            if any(not subject for subject in subjects):
                raise ValueError("Task subject cannot be empty")
        except Exception as exc:
            log_error_traceback("UpdateTasksContent validation", exc)
            raise ValueError(f"UpdateTasksContent parameters invalid: {exc}") from exc

        for item, task_id, subject in zip(updates, task_ids, subjects):
            task = self._data["tasks"][task_id]
            task["subject"] = subject
            task["description"] = item.description
        self._save()
        return [self._data["tasks"][task_id] for task_id in task_ids]

    def delete_all_tasks(self, confirm: bool = False, **kwargs) -> dict[str, Any]:
        if not confirm:
            raise ValueError("DANGER: Deletion aborted. You must explicitly pass confirm=True to delete all tasks.")

        allowed, reason = check_permission("tool", "DeleteAllTasks",
                                           "WARNING: Attempting to delete ALL tasks in the topology plan.")
        if not allowed:
            return {"status": "error", "message": f"User Denied Execution. Reason: {reason}"}

        self._data["tasks"] = {}
        self._data["next_id"] = 1

        self._save()
        return {"status": "success", "message": "All tasks have been permanently deleted."}

    # -------- Manager APIs --------
    def get_runnable_tasks(self, **kwargs) -> list[dict[str, Any]]:
        tasks = self._data["tasks"]
        runnable = []
        for _, task in tasks.items():
            if task["status"] != "pending":
                continue
            if all(
                    dep in tasks and tasks[dep]["status"] == "completed"
                    for dep in task.get("depend_on", [])
            ):
                runnable.append(task)
        return sorted(runnable, key=lambda t: self._id_sort_key(t["id"]))

    def get_task_table(self, **kwargs) -> dict[str, Any]:
        tasks = sorted(
            self._data["tasks"].values(), key=lambda t: self._id_sort_key(t["id"])
        )
        runnable_ids = {t["id"] for t in self.get_runnable_tasks()}
        status_count = {"pending": 0, "completed": 0}
        rows = []
        for t in tasks:
            status = t["status"]
            if status in status_count:
                status_count[status] += 1
            rows.append(
                {
                    "id": t["id"],
                    "subject": t["subject"],
                    "status": status,
                    "depend_on": t.get("depend_on", []),
                    "is_runnable": t["id"] in runnable_ids,
                    "description": t.get("description", ""),
                }
            )

        return {
            "summary": {
                "epic_id": self._data["epic_id"],
                "total": len(tasks),
                "pending": status_count["pending"],
                "completed": status_count["completed"],
                "runnable_count": len(runnable_ids),
            },
            "columns": [
                "id",
                "subject",
                "status",
                "depend_on",
                "is_runnable",
                "description",
            ],
            "rows": rows,
        }


def render_task_pane() -> None:
    from system.tui_app import TuiRegion, post_tui

    task_table = TASK_MANAGER.get_task_table()
    summary = task_table.get("summary", {})
    rows = task_table.get("rows", [])
    rows_by_id = {row["id"]: row for row in rows}
    post_tui(TuiRegion.TASK, "", clear=True)

    if not rows:
        post_tui(TuiRegion.TASK, Text("当前任务计划为空。", style="bold yellow"))
        return

    completed = summary.get("completed", 0)
    total = summary.get("total", len(rows))
    pending = summary.get("pending", 0)
    runnable = summary.get("runnable_count", 0)
    blocked = max(int(pending) - int(runnable), 0)

    text = Text()
    text.append("当前任务计划\n", style="bold cyan")
    text.append(f"✓ {completed}/{total} completed", style="green")
    text.append(" · ")
    text.append(f"▶ {runnable} runnable", style="yellow")
    text.append(" · ")
    text.append(f"□ {blocked} blocked\n\n", style="#aaaaaa")

    for row in rows:
        if row["status"] == "completed":
            icon = "✓"
            style = "green"
        elif row.get("is_runnable"):
            icon = "▶"
            style = "yellow"
        else:
            icon = "□"
            style = "#aaaaaa"
        text.append(f"{icon} {row['id']} ", style=style)
        text.append(str(row["subject"]))
        text.append("\n")

        depend_on = row.get("depend_on", [])
        if depend_on:
            waiting_deps = [
                dep_id for dep_id in depend_on
                if rows_by_id.get(dep_id, {}).get("status") != "completed"
            ]
            label = "waits" if waiting_deps else "deps"
            text.append(f"  {label}: ", style="#aaaaaa")
            for index, dep_id in enumerate(depend_on):
                if index:
                    text.append(" · ", style="#aaaaaa")
                dep_completed = rows_by_id.get(dep_id, {}).get("status") == "completed"
                dep_icon = "✓" if dep_completed else "□"
                dep_style = "green" if dep_completed else "#aaaaaa"
                text.append(f"{dep_icon} {dep_id}", style=dep_style)
            text.append("\n")

    post_tui(TuiRegion.TASK, text)


TASK_MANAGER = TaskManager()



def activate_conversation(
        conversation_root: Path,
        conversation_id: str,
        data: dict[str, Any] | None = None,
) -> None:
    global TASK_MANAGER
    TASK_MANAGER = TaskManager(conversation_root, conversation_id, data)
    render_task_pane()



def refresh_workspace_paths() -> None:
    global TASK_MANAGER
    TASK_MANAGER = TaskManager()
    render_task_pane()


TOOLS, TASK_MANAGER_TOOL_MODELS = build_tool_definitions(
    CreateTasks,
    UpdateTasksContent,
    UpdateTasksStatus,
    UpdateTasksDependencies,
    DeleteAllTasks,
    GetRunnableTasks,
    GetTaskTable,
)


TASK_MANAGER_NAMESPACE = {
    "type": "namespace",
    "name": "TaskManager",
    "description": (
        "Task topology planning and execution state tools. "
        "Recommended flow: CreateTasks -> UpdateTasksDependencies -> GetRunnableTasks -> DelegateTasks "
        "(DelegateTasks lives in Team tools and should only receive runnable task IDs). "
        "MUST NOT put tasks that edit the same file in the same batch — if multiple tasks need to edit the same file, "
        "establish explicit topology dependencies (via depend_on) so they execute sequentially."
    ),
    "tools": TOOLS,
}

TASK_MANAGER_TOOLS = [
    TASK_MANAGER_NAMESPACE,
]

# Lazy lookup — resolve TASK_MANAGER at call time so that replacing the
# module-level instance (e.g. when a title becomes available) is picked up.
TASK_MANAGER_TOOLS_HANDLERS = {
    "CreateTasks": lambda **kw: TASK_MANAGER.create_tasks(**kw),
    "UpdateTasksContent": lambda **kw: TASK_MANAGER.update_tasks_content(**kw),
    "UpdateTasksStatus": lambda **kw: TASK_MANAGER.update_tasks_status(**kw),
    "UpdateTasksDependencies": lambda **kw: TASK_MANAGER.update_tasks_dependencies(**kw),
    "DeleteAllTasks": lambda **kw: TASK_MANAGER.delete_all_tasks(**kw),
    "GetRunnableTasks": lambda **kw: TASK_MANAGER.get_runnable_tasks(**kw),
    "GetTaskTable": lambda **kw: TASK_MANAGER.get_task_table(**kw),
}
