"""
主动向用户提问工具 — 允许 Agent 在不确定时主动询问用户意见。
"""
import json
from typing import Any

from pydantic import Field, field_validator

from utils.tool_validation import ToolArgumentsModel, build_tool_definitions

from system.console_render import console_lock
from system.tui_app import choose_tui
from system.window_attention import request_window_attention


class Option(ToolArgumentsModel):
    """A single option presented to the user."""
    content: str = Field(..., description="The text content of this option.")
    is_recommended: bool = Field(
        default=False,
        description="Whether this option is recommended by the agent.",
    )


class AskUser(ToolArgumentsModel):
    """
    Proactively ask the user a question and wait for their response.

    WHEN TO USE:
    - The requirement is ambiguous and you need clarification
    - There are multiple valid approaches and you need the user to choose
    - A decision requires user preference or domain knowledge

    BEHAVIOR:
    - Presents the question and options in an interactive panel
    - The user can select a listed option
    - Returns a JSON string with the user's choice
    """
    question: str = Field(
        ...,
        description="The question or message to present to the user.",
    )
    options: list[Option] = Field(
        ...,
        min_length=1,
        description="List of options for the user to choose from.",
    )

    @field_validator("options", mode="before")
    @classmethod
    def parse_stringified_options(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("options must be a non-empty list")
            if v.lower() in {"none", "null"}:
                raise ValueError("options must be a non-empty list")
            if v == "[]":
                raise ValueError("options must contain at least one option")
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v


def ask_user(question: str, options: list, **kwargs) -> str:
    """Handler for the AskUser tool."""
    try:
        validated = AskUser.model_validate({"question": question, "options": options})
        question = validated.question
        parsed_options = validated.options
    except Exception as exc:
        return f"Error: Invalid arguments provided to AskUser. {exc}"

    with console_lock:
        labels = [
            f"⭐ {opt.content} （推荐）" if opt.is_recommended else opt.content
            for opt in parsed_options
        ]
        request_window_attention()
        choice = choose_tui(question, labels, allow_custom=True)

    if choice == "<cancelled>":
        return json.dumps({"choice": "<cancelled>"}, ensure_ascii=False)
    if choice == "<empty_input>":
        return json.dumps({"choice": "<empty_input>"}, ensure_ascii=False)

    for opt, label in zip(parsed_options, labels):
        if choice == label:
            return json.dumps({"choice": opt.content}, ensure_ascii=False)

    return json.dumps({"choice": choice, "custom": True}, ensure_ascii=False)


ASK_USER_TOOLS, ASK_USER_TOOL_MODELS = build_tool_definitions(AskUser)

ASK_USER_TOOLS_HANDLERS = {
    "AskUser": ask_user,
}
