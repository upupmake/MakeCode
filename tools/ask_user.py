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
    """One concise choice for a single user decision."""
    content: str = Field(..., description="Choice text shown to the user.")
    is_recommended: bool = Field(
        default=False,
        description="True only for the single evidence-based default.",
    )


class AskUser(ToolArgumentsModel):
    """
    Ask the user to make one bounded decision through the interactive choice panel.

    Use when the answer depends on user preference, domain knowledge, or an
    unresolved scope/high-impact choice that cannot be settled from the workspace
    or conversation. Do not use for facts available from tools, routine low-risk
    implementation choices, or approval already handled by HITL.

    Ask one concise question in the user's language and provide 2-4 mutually
    exclusive, actionable options. Mark at most one evidence-based default as
    recommended. Do not add an "Other" option; custom input is provided
    automatically.

    A selected option or custom input is the user's decision. "<cancelled>" and
    "<empty_input>" mean that no decision was provided.
    """
    question: str = Field(
        ...,
        description="One concise decision question in the user's language.",
    )
    options: list[Option] = Field(
        ...,
        min_length=1,
        description="The choices for the question.",
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
