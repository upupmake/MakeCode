from typing import Literal

from openai import pydantic_function_tool
from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    """A single task item with text and status."""
    id: str = Field(..., description="Unique identifier for the task.")
    text: str = Field(..., description="Description of the task.")
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

    def update(self, items: list[dict]):
        if len(items) > 20:
            raise ValueError("Too many todo items, max is 20")
        validated = []
        in_progress_count = 0
        for item in items:
            item = TaskItem(**item)
            text = item.text
            status = item.status
            item_id = item.id
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
                if in_progress_count > 1:
                    raise ValueError("Only one item can be in_progress")
            validated.append(item)
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
