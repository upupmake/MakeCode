import json
from typing import Literal, Any

from openai import pydantic_function_tool
from pydantic import BaseModel, Field, model_validator, field_validator


class TaskItem(BaseModel):
    """A single task item with text and status."""
    id: str = Field(..., description="Unique identifier for the task.")
    text: str = Field(default="", description="Description of the task.")
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
        
        # handle cases where LLM hallucinated 'content' instead of 'text'
        if isinstance(data, dict):
            if "content" in data and "text" not in data:
                data["text"] = data.pop("content")
                
        return data


class TodoUpdate(BaseModel):
    """
        Update the todo list with items. Update task list. Track progress on multi-step tasks.
    """
    items: list[TaskItem] = Field(
        ...,
        description="List of todo items."
    )

    @field_validator("items", mode="before")
    @classmethod
    def parse_stringified_items(cls, v: Any) -> Any:
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

    def update(self, items: Any):
        try:
            validated_model = TodoUpdate.model_validate({"items": items})
            spec_list = validated_model.items
        except Exception as exc:
            from init import log_error_traceback
            log_error_traceback("TodoManager update validation", exc)
            raise ValueError(f"TodoUpdate format invalid: {exc}") from exc

        if len(spec_list) > 20:
            raise ValueError("Too many todo items, max is 20")
        
        validated = []
        in_progress_count = 0
        for task_obj in spec_list:
            text = task_obj.text
            status = task_obj.status
            item_id = task_obj.id
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
                if in_progress_count > 1:
                    raise ValueError("Only one item can be in_progress")
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
            lines.append(f"{marker[item.status]} #{item.id}: {item.text}")
            done += item.status == "completed"
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return '\n'.join(lines)


TODO_TOOLS = [
    pydantic_function_tool(TodoUpdate)
]
