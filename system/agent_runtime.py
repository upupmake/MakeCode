"""Independent agent runtime shared by headless mode.

The class owns conversation history, LLM client lifecycle, tool dispatch and
memory/compaction hooks. It never imports Textual; all progress is emitted as
plain event dictionaries through ``event_handler``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prompts import get_orchestrator_system_prompt
from system.models import get_current_model_config
from system.stream_cancel import (
    is_cancelled,
    register_response_task,
    unregister_response_task,
)
from system.tool_registry import ToolRegistry
from utils import paths
from utils import tasks as tasks_module
from utils import teams as teams_module
from utils.conversations import (
    CONVERSATION_STORE,
    ConversationSnapshot,
    ConversationStore,
)
from utils.llm_client import (
    close_async_llm_client,
    create_current_async_llm_client,
)
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.memory import (
    ORCHESTRATOR_AGENT_ID,
    compact_tool_outputs,
    estimate_tokens,
    get_compaction_thresholds,
    get_context_token_limit,
    get_memory_pre_recall,
    manual_memory_update,
    partial_compact,
    prepend_recalled_memory_to_query,
    recall_long_term_memories,
)
from utils.plan_mode import (
    PLAN_MODE_ALLOWED_COMMANDS,
    PLAN_MODE_BLOCKLIST,
    is_plan_mode,
    is_plan_mode_command_allowed,
)
from utils.skills import SKILL_LOADER
from utils.tool_aliases import resolve_tool_name
from utils.tool_validation import (
    ToolArgumentValidationError,
    is_tool_error_output,
    parse_tool_arguments,
    validate_builtin_tool_arguments,
)


@dataclass
class TurnResult:
    text: str
    session_id: str
    messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    is_error: bool = False
    error: str | None = None
    cancelled: bool = False
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


class AgentRuntime:
    def __init__(
        self,
        *,
        store: ConversationStore | None = None,
        registry: ToolRegistry | None = None,
        llm_client_factory: Callable[..., Any] | None = None,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
        append_system_prompt: str | None = None,
        plan_mode: bool = False,
        headless: bool = False,
    ) -> None:
        self.store = store or CONVERSATION_STORE
        self.registry = registry or ToolRegistry()
        self.llm_client_factory = llm_client_factory or create_current_async_llm_client
        self.event_handler = event_handler
        self.append_system_prompt = append_system_prompt
        self.plan_mode = plan_mode
        self.headless = headless
        self.messages: list[dict[str, Any]] = []
        self._handlers: dict[str, Any] = {}
        self._tool_models: dict[str, Any] = {}
        self._permission_denials: list[dict[str, Any]] = []

    def restore(self, snapshot: ConversationSnapshot) -> None:
        self.store.activate(snapshot)
        self.messages = list(snapshot.messages)
        self._activate_workspace()

    def reset(self) -> None:
        self.store.reset()
        self.messages = []

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_handler is None:
            return
        with contextlib.suppress(Exception):
            self.event_handler(event)

    def _runtime_metadata(self) -> dict[str, Any]:
        try:
            config = get_current_model_config()
            model = getattr(config, "model_id", None)
        except Exception:
            model = None
        return {
            "model": model,
            "workdir": str(paths.workdir()),
            "plan_mode": self.plan_mode,
        }

    def _activate_workspace(self) -> None:
        path = self.store.ensure_active()
        if not self.store.persist:
            tasks_module.TASK_MANAGER.persist = False
            return
        tasks_module.TASK_MANAGER.persist = True
        conversation_id = self.store.active_id
        if conversation_id is None:
            return
        root = path.parent
        if tasks_module.TASK_MANAGER.conversation_id != conversation_id:
            tasks_module.activate_conversation(root, conversation_id)
        if teams_module.TEAM.conversation_id != conversation_id:
            teams_module.activate_conversation(root, conversation_id)
        SKILL_LOADER.refresh_workspace()

    def _system_prompt(self) -> str:
        prompt = get_orchestrator_system_prompt(
            str(paths.workdir()),
            "headless",
            plan_mode=self.plan_mode,
        )
        if self.headless:
            prompt += (
                "\n\n# Headless Mode\n"
                "You are running non-interactively. AskUser and interactive HITL prompts "
                "are unavailable; do not wait for user input. Sub-agent delegation and "
                "DelegateTasks are unavailable; execute tasks directly. Actions denied by "
                "the configured permission policy must not be retried unchanged."
            )
        if self.append_system_prompt:
            prompt = f"{prompt}\n\n{self.append_system_prompt}"
        return prompt

    def _prepare_messages(self, definitions: list[dict[str, Any]]) -> None:
        system_message = {"role": "system", "content": self._system_prompt()}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)

    async def _compact(self, definitions: list[dict[str, Any]]) -> None:
        context_token_limit = get_context_token_limit()
        initial_context_tokens = estimate_tokens(
            self.messages,
            tools_definition=definitions,
        )
        tool_output_threshold, partial_threshold = get_compaction_thresholds()
        if initial_context_tokens * 100 < context_token_limit * tool_output_threshold:
            return
        if initial_context_tokens * 100 >= context_token_limit * partial_threshold:
            reason = (
                f"Pre headless turn partial compact triggered: estimated tokens "
                f"{initial_context_tokens} reached {partial_threshold}% of threshold "
                f"{context_token_limit}."
            )
            if await self._maybe_partial_compact(
                reason,
                context_token_limit,
                initial_context_tokens,
            ):
                return
        compact_tool_outputs(self.messages)

    async def _maybe_partial_compact(
        self,
        reason: str,
        context_token_limit: int,
        initial_context_tokens: int,
    ) -> bool:
        try:
            return await partial_compact(
                self.messages,
                context_token_limit,
                initial_context_tokens,
                reason,
            )
        except Exception:
            return False

    async def _recall(self, query: str) -> None:
        if not get_memory_pre_recall():
            return
        recall_result = await recall_long_term_memories(
            query,
            previous_assistant_content=self._previous_assistant_content(),
            source="用户请求预召回",
            agent_id=ORCHESTRATOR_AGENT_ID,
        )
        memory_context = recall_result.get("content", "")
        if not memory_context:
            return
        for message in reversed(self.messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    {
                        "type": "text",
                        "text": prepend_recalled_memory_to_query("", memory_context),
                    },
                    *content,
                ]
            else:
                message["content"] = prepend_recalled_memory_to_query(
                    content if isinstance(content, str) else "",
                    memory_context,
                )
            return

    def _previous_assistant_content(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    async def _manage_long_term_memory(self, prompt: str, **kwargs):
        return await manual_memory_update(prompt, self.messages)

    async def _execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        plan_mode: bool,
    ) -> tuple[str, bool]:
        if plan_mode and tool_name in PLAN_MODE_BLOCKLIST:
            return (
                f"⛔ Plan Mode active: '{tool_name}' is blocked. "
                "Complete your plan first, then exit Plan Mode to execute.",
                True,
            )
        if plan_mode and tool_name == "RunTerminalCommand":
            command = str(arguments.get("command", ""))
            if not is_plan_mode_command_allowed(command):
                return (
                    f"⛔ Plan Mode: this command is not allowed. "
                    f"Only {', '.join(PLAN_MODE_ALLOWED_COMMANDS)} commands are permitted in Plan Mode.",
                    True,
                )
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"Unknown tool: {tool_name}", True
        return await self._run_tool_handler(handler, arguments)

    @staticmethod
    async def _run_tool_handler(handler, arguments: dict[str, Any]):
        try:
            if inspect.iscoroutinefunction(handler):
                output = await handler(**arguments)
            else:
                output = await asyncio.to_thread(handler, **arguments)
                if inspect.isawaitable(output):
                    output = await output
            return output, is_tool_error_output(output)
        except Exception as exc:
            return f"Error executing tool: {exc}", True

    async def _agent_loop(
        self,
        client,
        definitions: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        while True:
            response_text = ""
            tool_calls: list[dict[str, Any]] = []
            raw_message = None
            async for event in client.generate_stream(self.messages, definitions):
                if is_cancelled():
                    return response_text, True
                event_type = event.get("type")
                if event_type == "text":
                    chunk = event["content"]
                    response_text += chunk
                    self._emit({"type": "text_delta", "text": chunk})
                elif event_type == "reasoning":
                    self._emit({"type": "reasoning_delta", "text": event["content"]})
                elif event_type == "done":
                    result = event["result"]
                    response_text = result.text or response_text
                    tool_calls = result.tool_calls
                    raw_message = result.assistant_message
                    break
            if raw_message is None:
                return response_text, False
            client.append_assistant_message(self.messages, raw_message)
            self._emit(
                {
                    "type": "assistant_done",
                    "message": raw_message,
                    "text": response_text,
                    "tool_calls": tool_calls,
                }
            )
            self._save()
            if not tool_calls:
                return response_text, False
            for tool_call in tool_calls:
                tool_name = resolve_tool_name(tool_call["name"])
                tool_id = tool_call["id"]
                tool_args = tool_call["arguments"]
                self._emit(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tool_name,
                        "input": tool_args,
                    }
                )
                try:
                    if tool_name in self._tool_models:
                        arguments = validate_builtin_tool_arguments(
                            tool_name,
                            tool_args,
                            self._tool_models[tool_name],
                        )
                    else:
                        arguments = parse_tool_arguments(tool_name, tool_args)
                    output, tool_error = await self._execute_tool(
                        tool_name,
                        arguments,
                        plan_mode=self.plan_mode or is_plan_mode(),
                    )
                except ToolArgumentValidationError as exc:
                    output, tool_error = str(exc), True
                except Exception as exc:
                    output, tool_error = f"Error executing {tool_name}: {exc}.", True
                tool_result = client.format_tool_result(tool_id, tool_name, output)
                if tool_error:
                    tool_result["is_error"] = True
                    if output.startswith("User Denied Execution"):
                        self._permission_denials.append(
                            {"tool": tool_name, "reason": output}
                        )
                self.messages.append(tool_result)
                self._emit(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "name": tool_name,
                        "content": output,
                        "is_error": tool_error,
                    }
                )
                self._save()

    def _save(self) -> None:
        self.store.save_messages(self.messages, runtime=self._runtime_metadata())

    def close(self) -> None:
        """Release tool handlers so MCP clients can be collected before shutdown."""
        self._handlers.clear()
        self._tool_models.clear()

    async def run_turn(self, query: str) -> TurnResult:
        started = time.perf_counter()
        self._handlers = {}
        self._permission_denials = []
        client = None
        response_task = asyncio.current_task()
        if response_task is not None:
            register_response_task(response_task)
        try:
            self._activate_workspace()
            self.messages.append({"role": "user", "content": query})
            client = self.llm_client_factory(
                conversation_root=self.store.active_root,
            )
            if client is None:
                return self._result("", is_error=True, error="No model configured", started=started)
            mcp_tools, mcp_handlers = GLOBAL_MCP_MANAGER.get_registry_snapshot()
            definitions = self.registry.definitions(
                mcp_tools=mcp_tools,
                llm_client=client,
                plan_mode=self.plan_mode or is_plan_mode(),
            )
            self._handlers = self.registry.handlers(mcp_handlers)
            if (
                not (self.plan_mode or is_plan_mode())
                and self.registry.is_tool_enabled("ManageLongTermMemory")
            ):
                self._handlers["ManageLongTermMemory"] = self._manage_long_term_memory
            self._tool_models = self.registry.tool_models()
            self._prepare_messages(definitions)
            if any(message.get("role") == "user" for message in self.messages):
                await self._compact(definitions)
            await self._recall(query)
            text, cancelled = await self._agent_loop(client, definitions)
            self._save()
            usage = {
                "context_tokens": estimate_tokens(
                    self.messages,
                    tools_definition=definitions,
                )
            }
            return self._result(
                text,
                cancelled=cancelled,
                usage=usage,
                started=started,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self._save()
            return self._result("", cancelled=True, started=started)
        except Exception as exc:
            return self._result(
                "",
                is_error=True,
                error=str(exc),
                started=started,
            )
        finally:
            if response_task is not None:
                unregister_response_task(response_task)
            if client is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await close_async_llm_client(client)

    def _result(
        self,
        text: str,
        *,
        is_error: bool = False,
        error: str | None = None,
        cancelled: bool = False,
        usage: dict[str, int] | None = None,
        started: float | None = None,
    ) -> TurnResult:
        duration_ms = int((time.perf_counter() - started) * 1000) if started else 0
        return TurnResult(
            text=text,
            session_id=self.store.active_id or "",
            messages=list(self.messages),
            usage=usage or {},
            is_error=is_error,
            error=error,
            cancelled=cancelled,
            permission_denials=list(self._permission_denials),
            duration_ms=duration_ms,
        )
