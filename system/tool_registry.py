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
from utils.tool_aliases import resolve_tool_name
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
        include_delegate_tasks: bool = True,
        include_understand_image: bool | None = None,
    ) -> None:
        self.allowed_tools = frozenset(allowed_tools or ())
        self.disallowed_tools = frozenset(disallowed_tools or ())
        self.include_ask_user = include_ask_user
        self.include_delegate_tasks = include_delegate_tasks
        self.include_understand_image = include_understand_image

    @staticmethod
    def _leaf_names(tool: dict[str, Any]) -> set[str]:
        if tool.get("type") == "namespace":
            names: set[str] = set()
            for child in tool.get("tools", []):
                if isinstance(child, dict):
                    names.update(ToolRegistry._leaf_names(child))
            return names
        name = _tool_name(tool)
        return {name} if name else set()

    @classmethod
    def _all_leaf_names(cls, tools: Iterable[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for tool in tools:
            names.update(cls._leaf_names(tool))
        return names

    @classmethod
    def _expand_selectors(
        cls,
        tools: Iterable[dict[str, Any]],
        selectors: Iterable[str],
    ) -> set[str]:
        tool_list = list(tools)
        all_names = cls._all_leaf_names(tool_list)
        selected: set[str] = set()
        for selector in selectors:
            name = resolve_tool_name(str(selector).strip())
            if not name:
                continue
            if name in all_names:
                selected.add(name)
            for tool in tool_list:
                if tool.get("type") == "namespace" and tool.get("name") == name:
                    selected.update(cls._leaf_names(tool))
        return selected

    def _selected_names(
        self,
        tools: Iterable[dict[str, Any]],
        *,
        extra_names: Iterable[str] = (),
        plan_mode: bool = False,
    ) -> set[str]:
        tool_list = list(tools)
        extra = {resolve_tool_name(str(name)) for name in extra_names}
        all_names = self._all_leaf_names(tool_list) | extra
        if self.allowed_tools:
            selected = self._expand_selectors(tool_list, self.allowed_tools)
            selected.update(
                resolve_tool_name(str(name))
                for name in self.allowed_tools
                if resolve_tool_name(str(name)) in extra
            )
        else:
            selected = set(all_names)
        selected -= self._expand_selectors(tool_list, self.disallowed_tools)
        selected -= {
            resolve_tool_name(str(name))
            for name in self.disallowed_tools
            if resolve_tool_name(str(name)) in extra
        }
        if plan_mode:
            selected -= self._expand_selectors(tool_list, PLAN_MODE_BLOCKLIST)
            selected -= set(PLAN_MODE_BLOCKLIST) & extra
        return selected

    @classmethod
    def _filter_tool(
        cls,
        tool: dict[str, Any],
        selected: set[str],
    ) -> dict[str, Any] | None:
        if tool.get("type") == "namespace":
            children = [
                filtered
                for child in tool.get("tools", [])
                if isinstance(child, dict)
                and (filtered := cls._filter_tool(child, selected)) is not None
            ]
            if not children:
                return None
            filtered_tool = dict(tool)
            filtered_tool["tools"] = children
            return filtered_tool
        return dict(tool) if _tool_name(tool) in selected else None

    def _is_allowed(self, name: str) -> bool:
        return resolve_tool_name(name) in self._selected_names(self._builtin_tools())

    def is_tool_enabled(self, name: str) -> bool:
        return self._is_allowed(name)

    def unmatched_allowed_selectors(
        self,
        mcp_tools: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Return allow-list entries that cannot match any registered tool."""
        tools = [
            *self._builtin_tools(),
            *(GLOBAL_MCP_MANAGER.get_tools() if mcp_tools is None else mcp_tools),
        ]
        leaf_names = self._all_leaf_names(tools)
        namespace_names = {
            str(tool.get("name"))
            for tool in tools
            if tool.get("type") == "namespace" and tool.get("name")
        }
        unknown = []
        for selector in self.allowed_tools:
            name = resolve_tool_name(str(selector).strip())
            if name not in leaf_names and name not in namespace_names:
                unknown.append(str(selector))
        return unknown

    def tool_names(
        self,
        mcp_tools: list[dict[str, Any]] | None = None,
        *,
        plan_mode: bool = False,
    ) -> list[str]:
        """Return the enabled leaf tool names without formatting for a model."""
        mcp = GLOBAL_MCP_MANAGER.get_tools() if mcp_tools is None else mcp_tools
        selected = self._selected_names(
            self._builtin_tools(),
            extra_names=self._all_leaf_names(mcp),
            plan_mode=plan_mode,
        )
        return sorted(selected)

    def _builtin_tools(self) -> list[dict[str, Any]]:
        tools = [
            *COMMON_TOOLS,
            *MEMORY_RECALL_TOOLS,
            *MEMORY_SELF_MANAGEMENT_TOOLS,
            *SKILL_TOOLS,
            *TASK_MANAGER_TOOLS,
        ]
        if self.include_delegate_tasks:
            tools.extend(TEAM_TOOLS)
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
        plan = is_plan_mode() if plan_mode is None else plan_mode
        selected = self._selected_names(tools, plan_mode=plan)
        filtered = [
            filtered_tool
            for tool in tools
            if (filtered_tool := self._filter_tool(tool, selected)) is not None
        ]
        if llm_client is not None:
            return cast(
                list[dict[str, Any]],
                [dict(tool) for tool in llm_client.format_tools(filtered)],
            )
        return cast(
            list[dict[str, Any]],
            [dict(tool) for tool in format_tools_for_current_model(filtered)],
        )

    def handlers(self, mcp_handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        mcp_handlers = dict(mcp_handlers or {})
        handlers = {
            **self.BASE_SUPER_TOOLS_HANDLERS,
            **mcp_handlers,
        }
        selected = self._selected_names(
            self._builtin_tools(),
            extra_names=mcp_handlers,
        )
        return {
            name: handler
            for name, handler in handlers.items()
            if resolve_tool_name(name) in selected
        }

    def tool_models(self) -> dict[str, Any]:
        selected = self._selected_names(self._builtin_tools())
        return {
            name: model
            for name, model in self.BASE_SUPER_TOOL_MODELS.items()
            if resolve_tool_name(name) in selected
        }
