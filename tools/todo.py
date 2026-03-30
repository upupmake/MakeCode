import json
from typing import Literal

from openai import pydantic_function_tool
from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    """A single task item with text and status."""
    id: str = Field(..., description="Unique identifier for the task.")
    text: str = Field(default="", description="Description of the task.")
    status: Literal["pending", "in_progress", "completed"] = Field(
        ...,
        description="Status of the task."
    )


class TodoUpdate(BaseModel):
    """
        Update the todo list with items. Update task list. Track progress on multi-step tasks.
    """
    items: list[TaskItem] = Field(
        ...,
        description="List of todo items."
    )


class TodoManager:
    def __init__(self):
        self.items: list[TaskItem] = []

    def update(self, items: list[dict] | str):
        if isinstance(items, str):
            payload = items.strip()
            if not payload:
                raise ValueError("TodoUpdate.items is an empty string; expected a list.")
            try:
                items = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"TodoUpdate.items JSON parse error: {exc}") from exc
        if not isinstance(items, list):
            raise ValueError(f"TodoUpdate.items must be a list, got {type(items).__name__}")
        if len(items) > 20:
            raise ValueError("Too many todo items, max is 20")
        validated = []
        in_progress_count = 0
        for idx, item in enumerate(items):
            item_obj = item
            for _ in range(5):
                if isinstance(item_obj, str):
                    payload = item_obj.strip()
                    if not payload:
                        raise ValueError(f"TodoUpdate.items[{idx}] is an empty string.")
                    try:
                        item_obj = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"TodoUpdate.items[{idx}] JSON parse error: {exc}") from exc
                    continue
                break
            if not isinstance(item_obj, dict):
                raise ValueError(
                    f"TodoUpdate.items[{idx}] must be an object (dict), got {type(item_obj).__name__}"
                )
            item = dict(item_obj)
            # handle cases where LLM hallucinated 'content' instead of 'text'
            if "content" in item and "text" not in item:
                item["text"] = item.pop("content")
            
            task_obj = TaskItem(**item)
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
