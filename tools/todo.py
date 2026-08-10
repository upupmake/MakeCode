import json
from typing import Literal, Any

from pydantic import Field, model_validator, field_validator


from utils.tool_validation import ToolArgumentsModel, build_tool_definitions


class TaskItem(ToolArgumentsModel):
    """A single task item with description and status."""
    id: str = Field(..., description="Unique identifier for the task.")
    description: str = Field(default="", description="Brief description of the task.")
    status: Literal["pending", "in_progress", "completed"] = Field(
        ...,
        description="Status of the task."
    )

    @model_validator(mode="before")
    @classmethod
    def parse_stringified_item(cls, data: Any) -> Any:
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
        
        # handle legacy field names: text/content -> description
        if isinstance(data, dict):
            if "text" in data and "description" not in data:
                data["description"] = data.pop("text")
            elif "content" in data and "description" not in data:
                data["description"] = data.pop("content")
                
        return data


class TodoUpdate(ToolArgumentsModel):
    """
    Update the todo list to track progress on multi-step tasks.

    PURPOSE:
    - Create a short actionable plan (2-6 items) at task start
    - Keep status updated as work progresses
    - Mark items completed when done

    CONSTRAINTS:
    - Maximum 20 tasks
    - Each task requires: id, description, status

    USAGE PATTERN:
    1. At task start: Create initial todo list
    2. During work: Update status as you progress
    3. At completion: Mark all items completed
    """
    todos: list[TaskItem] = Field(
        ...,
        description="List of todo items, each with: id (string), description (task description), status (pending/in_progress/completed).",
    )

    @field_validator("todos", mode="before")
    @classmethod
    def parse_stringified_todos(cls, v: Any) -> Any:
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


class TodoManager:
    def __init__(self):
        self.items: list[TaskItem] = []

    def update(self, todos: list[dict]):
        try:
            validated_model = TodoUpdate.model_validate({"todos": todos})
            spec_list = validated_model.todos
        except Exception as exc:
            from init import log_error_traceback
            log_error_traceback("TodoManager update validation", exc)
            raise ValueError(f"TodoUpdate format invalid: {exc}") from exc

        if len(spec_list) > 20:
            raise ValueError("Too many todo items, max is 20")
        
        validated = []
        for task_obj in spec_list:
            desc = task_obj.description
            status = task_obj.status
            item_id = task_obj.id
            if not desc:
                raise ValueError(f"Task {item_id}: description required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            validated.append(task_obj)
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        done = 0
        lines = []
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[Y]"}
        for item in self.items:
            lines.append(f"{marker[item.status]} #{item.id}: {item.description}")
            done += item.status == "completed"
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return '\n'.join(lines)


TODO_TOOLS, TODO_TOOL_MODELS = build_tool_definitions(TodoUpdate)
