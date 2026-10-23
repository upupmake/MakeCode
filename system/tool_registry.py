"""Tool definitions and handlers shared by TUI and headless runtimes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar, cast

from tools.ask_user import ASK_USER_TOOL_MODELS, ASK_USER_TOOLS, ASK_USER_TOOLS_HANDLERS
from tools.extra_tools import is_understand_image_enabled
from tools.understand_image import (
    UNDERSTAND_IMAGE_TOOL_MODELS,
    UNDERSTAND_IMAGE_TOOLS,
    UNDERSTAND_IMAGE_TOOLS_HANDLERS,
)
from utils.common import (
    COMMON_TOOL_MODELS,
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
)
from utils.llm_client import format_tools_for_current_model
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.memory import (
    MEMORY_RECALL_TOOL_MODELS,
    MEMORY_RECALL_TOOLS,
    MEMORY_RECALL_TOOLS_HANDLERS,
    MEMORY_SELF_MANAGEMENT_TOOL_MODELS,
    MEMORY_SELF_MANAGEMENT_TOOLS,
)
from utils.plan_mode import PLAN_MODE_BLOCKLIST, is_plan_mode
from utils.skills import (
    SKILL_TOOL_MODELS,
    SKILL_TOOLS,
    SKILL_TOOLS_HANDLERS,
)
from utils.tasks import (
    TASK_MANAGER_TOOL_MODELS,
    TASK_MANAGER_TOOLS,
    TASK_MANAGER_TOOLS_HANDLERS,
)
from utils.teams import TEAM_TOOL_MODELS, TEAM_TOOLS, TEAM_TOOLS_HANDLERS
from utils.tool_validation import merge_tool_model_registries


def _tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(tool.get("name", ""))


class ToolRegistry:
    """Assemble built-in, skill, memory, team and MCP tools with a policy filter."""

    BASE_SUPER_TOOL_MODELS: ClassVar[dict[str, Any]] = merge_tool_model_registries(
        COMMON_TOOL_MODELS,
        MEMORY_RECALL_TOOL_MODELS,
        MEMORY_SELF_MANAGEMENT_TOOL_MODELS,
        SKILL_TOOL_MODELS,
        TASK_MANAGER_TOOL_MODELS,
        TEAM_TOOL_MODELS,
        ASK_USER_TOOL_MODELS,
        UNDERSTAND_IMAGE_TOOL_MODELS,
    )

    BASE_SUPER_TOOLS_HANDLERS: ClassVar[dict[str, Any]] = {
        **COMMON_TOOLS_HANDLERS,
        **MEMORY_RECALL_TOOLS_HANDLERS,
        **SKILL_TOOLS_HANDLERS,
        **TASK_MANAGER_TOOLS_HANDLERS,
        **TEAM_TOOLS_HANDLERS,
        **ASK_USER_TOOLS_HANDLERS,
        **UNDERSTAND_IMAGE_TOOLS_HANDLERS,
    }

    def __init__(
        self,
        *,
        allowed_tools: Iterable[str] | None = None,
        disallowed_tools: Iterable[str] | None = None,
        include_ask_user: bool = True,
        include_understand_image: bool | None = None,
    ) -> None:
        self.allowed_tools = frozenset(allowed_tools or ())
        self.disallowed_tools = frozenset(disallowed_tools or ())
        self.include_ask_user = include_ask_user
        self.include_understand_image = include_understand_image

    def _is_allowed(self, name: str) -> bool:
        if name in self.disallowed_tools:
            return False
        if not self.allowed_tools:
            return True
        return name in self.allowed_tools

    def _builtin_tools(self) -> list[dict[str, Any]]:
        tools = [
            *COMMON_TOOLS,
            *MEMORY_RECALL_TOOLS,
            *MEMORY_SELF_MANAGEMENT_TOOLS,
            *SKILL_TOOLS,
            *TASK_MANAGER_TOOLS,
            *TEAM_TOOLS,
        ]
        if self.include_ask_user:
            tools.extend(ASK_USER_TOOLS)
        if (
            self.include_understand_image
            if self.include_understand_image is not None
            else is_understand_image_enabled()
        ):
            tools.extend(UNDERSTAND_IMAGE_TOOLS)
        return cast(list[dict[str, Any]], tools)

    def definitions(
        self,
        *,
        mcp_tools: list[dict[str, Any]] | None = None,
        llm_client=None,
        plan_mode: bool | None = None,
    ) -> list[dict[str, Any]]:
        tools = [
            *self._builtin_tools(),
            *(GLOBAL_MCP_MANAGER.get_tools() if mcp_tools is None else mcp_tools),
        ]
        if plan_mode if plan_mode is not None else is_plan_mode():
            tools = [tool for tool in tools if _tool_name(tool) not in PLAN_MODE_BLOCKLIST]
        tools = [tool for tool in tools if self._is_allowed(_tool_name(tool))]
        if llm_client is not None:
            return cast(
                list[dict[str, Any]],
                [dict(tool) for tool in llm_client.format_tools(tools)],
            )
        return cast(
            list[dict[str, Any]],
            [dict(tool) for tool in format_tools_for_current_model(tools)],
        )

    def handlers(self, mcp_handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        handlers = {
            **self.BASE_SUPER_TOOLS_HANDLERS,
            **(mcp_handlers or {}),
        }
        if not self.include_ask_user:
            handlers = {
                name: handler
                for name, handler in handlers.items()
                if not name.startswith("AskUser")
            }
        return {
            name: handler
            for name, handler in handlers.items()
            if self._is_allowed(name)
        }

    def tool_models(self) -> dict[str, Any]:
        return {
            name: model
            for name, model in self.BASE_SUPER_TOOL_MODELS.items()
            if self._is_allowed(name)
        }
