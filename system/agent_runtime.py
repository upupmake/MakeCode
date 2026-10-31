"""Independent agent runtime shared by headless mode.

The class owns conversation history, LLM client lifecycle, tool dispatch and
memory/compaction hooks. It never imports Textual; all progress is emitted as
plain event dictionaries through ``event_handler``.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import logging
import time
from collections.abc import Callable, Iterable
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
from utils.project_instructions import inject_project_instructions
from utils.skills import SKILL_LOADER
from utils.tool_aliases import resolve_tool_name
from utils.tool_validation import (
    ToolArgumentValidationError,
    is_tool_error_output,
    parse_tool_arguments,
    validate_builtin_tool_arguments,
)

logger = logging.getLogger(__name__)

_INJECTION_QUEUE_MAXSIZE = 100
_NOTIFICATION_VERSION = 1
_NOTIFICATION_INTENTS = frozenset({"decision", "todo", "fyi"})
_NOTIFICATION_INSTRUCTION = (
    "这是运行期间到达的系统通知，不是新的用户任务。\n"
    "你可以继续当前工作，或按需通过可用工具查看完整内容。\n"
    "intent=decision 的消息是对你此前提问的答复，必须完整阅读，"
    "并据此调整后续工作，不能静默忽略。"
)


@dataclass
class _PendingInjection:
    """A context block waiting to be appended before the next model request."""

    metadata: dict[str, Any]
    render: Callable[[set[str]], tuple[str, list[str]] | None]


def _xml_text(value: str) -> str:
    return html.escape(value, quote=False)


def _xml_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _normalize_notification_item(
    notification_id: str,
    index: int,
    raw_item: Any,
) -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        logger.warning(
            "忽略通知 %s 的第 %d 项：item 必须是 JSON 对象。",
            notification_id,
            index + 1,
        )
        return None
    delivery_id = raw_item.get("delivery_id")
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        logger.warning(
            "忽略通知 %s 的第 %d 项：缺少有效的 delivery_id。",
            notification_id,
            index + 1,
        )
        return None
    intent = raw_item.get("intent")
    if intent not in _NOTIFICATION_INTENTS:
        logger.warning(
            "忽略通知 %s 的第 %d 项：不支持 intent %r。",
            notification_id,
            index + 1,
            intent,
        )
        return None
    text = raw_item.get("text")
    preview = raw_item.get("preview")
    if intent == "decision":
        if not isinstance(text, str) or not text:
            logger.warning(
                "忽略通知 %s 的第 %d 项：decision 缺少完整 text。",
                notification_id,
                index + 1,
            )
            return None
    elif not isinstance(preview, str):
        logger.warning(
            "忽略通知 %s 的第 %d 项：%s 缺少 preview。",
            notification_id,
            index + 1,
            intent,
        )
        return None
    sender = raw_item.get("from")
    created_at = raw_item.get("created_at")
    message_id = raw_item.get("message_id")
    return {
        "delivery_id": delivery_id,
        "message_id": message_id if isinstance(message_id, str) else "",
        "intent": intent,
        "from": sender if isinstance(sender, str) and sender else "user",
        "created_at": created_at if isinstance(created_at, str) else "",
        "text": text if isinstance(text, str) else "",
        "preview": preview if isinstance(preview, str) else "",
    }


def _normalize_notification(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        logger.warning("忽略通知：通知必须是 JSON 对象。")
        return None
    notification_id = payload.get("id")
    if not isinstance(notification_id, str) or not notification_id.strip():
        logger.warning("忽略通知：缺少有效的 id。")
        return None
    version = payload.get("version", _NOTIFICATION_VERSION)
    if version != _NOTIFICATION_VERSION:
        logger.warning("忽略通知 %s：不支持 version %r。", notification_id, version)
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        logger.warning("忽略通知 %s：items 必须是列表。", notification_id)
        return None
    items = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _normalize_notification_item(notification_id, index, raw_item)
        if item is None or item["delivery_id"] in seen_ids:
            continue
        seen_ids.add(item["delivery_id"])
        items.append(item)
    return {"id": notification_id, "items": items}


def format_notification(notification_id: str, items: list[dict[str, Any]]) -> str:
    """Render structured notification items as controlled XML context."""
    count = len(items)
    decisions = sum(1 for item in items if item["intent"] == "decision")
    if decisions:
        summary = (
            f"当前 Turn 执行期间收到 {count} 条新消息，其中 {decisions} 条需要决策。"
        )
    else:
        summary = f"当前 Turn 执行期间收到 {count} 条新消息。"
    lines = [
        f'<notification version="{_NOTIFICATION_VERSION}" '
        f'id="{_xml_attr(notification_id)}" count="{count}" decisions="{decisions}">',
        f"  <summary>{_xml_text(summary)}</summary>",
        "",
    ]
    for item in items:
        delivery_id = _xml_attr(item["delivery_id"])
        sender = _xml_attr(item["from"])
        created_at = _xml_attr(item["created_at"])
        if item["intent"] == "decision":
            message_id = _xml_attr(item["message_id"])
            lines.append(
                f'  <decision delivery_id="{delivery_id}" message_id="{message_id}" '
                f'from="{sender}" created_at="{created_at}">'
            )
            lines.append(f"    <text>{_xml_text(item['text'])}</text>")
            lines.append("  </decision>")
        else:
            intent = _xml_attr(item["intent"])
            lines.append(
                f'  <message delivery_id="{delivery_id}" intent="{intent}" '
                f'from="{sender}" created_at="{created_at}">'
            )
            lines.append(f"    <preview>{_xml_text(item['preview'])}</preview>")
            lines.append("  </message>")
        lines.append("")
    lines.append("  <instruction>")
    lines.extend(f"    {line}" for line in _NOTIFICATION_INSTRUCTION.splitlines())
    lines.append("  </instruction>")
    lines.append("</notification>")
    return "\n".join(lines)


def _merge_injection_metadata(
    rendered: list[tuple[_PendingInjection, str, list[str]]],
) -> dict[str, Any]:
    if len(rendered) == 1:
        record, _, delivery_ids = rendered[0]
        metadata = dict(record.metadata)
        if delivery_ids:
            metadata["delivery_ids"] = delivery_ids
        return metadata
    notification_ids = [
        str(record.metadata["notification_id"])
        for record, _, _ in rendered
        if record.metadata.get("notification") is True
        and record.metadata.get("notification_id")
    ]
    delivery_ids = [delivery_id for _, _, ids in rendered for delivery_id in ids]
    if len(notification_ids) == len(rendered):
        return {
            "notification": True,
            "notification_id": ",".join(notification_ids),
            "delivery_ids": delivery_ids,
        }
    metadata: dict[str, Any] = {"injected": True, "delivery_ids": delivery_ids}
    for record, _, _ in rendered:
        for key, value in record.metadata.items():
            metadata.setdefault(key, value)
    return metadata


@dataclass
class TurnResult:
    text: str
    session_id: str
    messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    compactions: list[dict[str, Any]] = field(default_factory=list)
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
        ask_user_available: bool = False,
    ) -> None:
        self.store = store or CONVERSATION_STORE
        self.registry = registry or ToolRegistry()
        self.llm_client_factory = llm_client_factory or create_current_async_llm_client
        self.event_handler = event_handler
        self.append_system_prompt = append_system_prompt
        self.plan_mode = plan_mode
        self.headless = headless
        self.ask_user_available = ask_user_available
        self.messages: list[dict[str, Any]] = []
        self._handlers: dict[str, Any] = {}
        self._tool_models: dict[str, Any] = {}
        self._permission_denials: list[dict[str, Any]] = []
        self._system_prompt_cache: str | None = None
        self._turn_usage: dict[str, int] = {}
        self._turn_compactions: list[dict[str, Any]] = []
        self._pending_injections: asyncio.Queue[_PendingInjection] = asyncio.Queue(
            maxsize=_INJECTION_QUEUE_MAXSIZE
        )
        self._turn_injected_ids: set[str] = set()

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
        # Built once per runtime so the cached prefix stays byte-identical even
        # if skills files or the wall clock change mid-session.
        if self._system_prompt_cache is not None:
            return self._system_prompt_cache
        prompt = get_orchestrator_system_prompt(
            str(paths.workdir()),
            "headless",
            plan_mode=self.plan_mode,
        )
        if self.headless:
            if self.ask_user_available:
                prompt += (
                    "\n\n# Headless Mode\n"
                    "You are running through a CLI channel. AskUser is available: call it "
                    "when a decision genuinely depends on user preference or scope, then "
                    "wait for its tool result. Interactive HITL prompts are unavailable. "
                    "Sub-agent delegation and DelegateTasks are unavailable; execute tasks "
                    "directly. Actions denied by the configured permission policy must not "
                    "be retried unchanged."
                )
            else:
                prompt += (
                    "\n\n# Headless Mode\n"
                    "You are running non-interactively. AskUser and interactive HITL prompts "
                    "are unavailable; do not wait for user input. Sub-agent delegation and "
                    "DelegateTasks are unavailable; execute tasks directly. Actions denied by "
                    "the configured permission policy must not be retried unchanged."
                )
        if self.append_system_prompt:
            prompt = f"{prompt}\n\n{self.append_system_prompt}"
        self._system_prompt_cache = prompt
        return prompt

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {}
        cached_tokens = None
        for key in ("input_tokens_details", "prompt_tokens_details"):
            details = usage.get(key)
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                cached_tokens = details.get("cached_tokens")
                break
        if cached_tokens is None:
            cached_tokens = usage.get("cache_read_input_tokens")
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens")
        if output_tokens is None:
            output_tokens = usage.get("completion_tokens")
        creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        cached_tokens = int(cached_tokens or 0)
        # Anthropic reports only uncached input in ``input_tokens`` while
        # OpenAI reports the whole prompt. Normalize both to total prompt tokens
        # so cache hit ratios are comparable across protocols.
        if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
            total_input_tokens = int(input_tokens or 0) + cached_tokens + creation_tokens
        else:
            total_input_tokens = int(input_tokens or 0)
        return {
            "input_tokens": total_input_tokens,
            "cached_tokens": cached_tokens,
            "cache_creation_tokens": creation_tokens,
            "uncached_input_tokens": max(
                total_input_tokens - cached_tokens - creation_tokens,
                0,
            ),
            "output_tokens": int(output_tokens or 0),
        }

    def _accumulate_usage(self, usage: Any) -> None:
        for key, value in self._normalize_usage(usage).items():
            self._turn_usage[key] = self._turn_usage.get(key, 0) + value

    def _prepare_messages(self, definitions: list[dict[str, Any]]) -> None:
        system_message = {"role": "system", "content": self._system_prompt()}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)

    def _emit_compact(self, phase: str, scope: str, **fields: Any) -> None:
        event = {
            "type": "compact",
            "phase": phase,
            "scope": scope,
            **fields,
        }
        if phase != "summary_delta":
            self._turn_compactions.append(event)
        self._emit(event)

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
            self._emit_compact(
                "start",
                "partial",
                trigger="auto",
                reason=reason,
                tokens_before=initial_context_tokens,
                threshold_percent=partial_threshold,
            )
            status, error = await self._maybe_partial_compact(
                reason,
                context_token_limit,
                initial_context_tokens,
            )
            if status == "success":
                tokens_after = estimate_tokens(
                    self.messages,
                    tools_definition=definitions,
                )
                self._emit_compact(
                    "end",
                    "partial",
                    status="success",
                    committed=True,
                    tokens_before=initial_context_tokens,
                    tokens_after=tokens_after,
                )
                return
            self._emit_compact(
                "end",
                "partial",
                status=status,
                committed=False,
                tokens_before=initial_context_tokens,
                tokens_after=initial_context_tokens,
                error=error,
            )
        self._emit_compact(
            "start",
            "tool_output",
            tokens_before=initial_context_tokens,
            threshold_percent=tool_output_threshold,
        )
        changed = compact_tool_outputs(self.messages)
        tokens_after = (
            estimate_tokens(self.messages, tools_definition=definitions)
            if changed
            else initial_context_tokens
        )
        self._emit_compact(
            "end",
            "tool_output",
            status="success" if changed else "noop",
            changed=changed,
            tokens_before=initial_context_tokens,
            tokens_after=tokens_after,
        )

    async def _maybe_partial_compact(
        self,
        reason: str,
        context_token_limit: int,
        initial_context_tokens: int,
    ) -> tuple[str, str | None]:
        try:
            compacted = await partial_compact(
                self.messages,
                context_token_limit,
                initial_context_tokens,
                reason,
                on_summary_delta=lambda text: self._emit_compact(
                    "summary_delta",
                    "partial",
                    text=text,
                ),
                on_summary=lambda summary: self._emit_compact(
                    "summary",
                    "partial",
                    summary=summary,
                ),
                on_memory_start=lambda: self._emit_compact(
                    "memory_start",
                    "partial",
                ),
                on_memory_end=lambda error: self._emit_compact(
                    "memory_end",
                    "partial",
                    status="failed" if error else "success",
                    error=error,
                ),
            )
        except Exception as exc:
            return "failed", str(exc)
        return ("success" if compacted else "skipped"), None

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
            self._inject_pending_injections()
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
                    self._accumulate_usage(getattr(result, "usage", None))
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

    def enqueue_injection(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        dedupe_ids: Iterable[str] = (),
    ) -> None:
        """Queue context to append before the next model request.

        Queued injections are merged into a single ``role: user`` message and
        appended at the top of the agent loop, so a running tool call is never
        interrupted. ``dedupe_ids`` keep the same payload from being injected
        twice within one turn. Must be called on the event loop thread.
        """
        if not isinstance(content, str) or not content.strip():
            logger.warning("忽略消息注入：content 不能为空。")
            return
        ids = tuple(
            value
            for value in dedupe_ids
            if isinstance(value, str) and value.strip()
        )

        def render(seen: set[str]) -> tuple[str, list[str]] | None:
            remaining = [value for value in ids if value not in seen]
            if ids and not remaining:
                return None
            return content, remaining

        self._enqueue_injection(
            _PendingInjection(dict(metadata or {}), render)
        )

    def enqueue_notification(self, notification: dict[str, Any]) -> None:
        """Queue a stream-json control notification for context injection."""
        normalized = _normalize_notification(notification)
        if normalized is None:
            return
        items = normalized["items"]
        if not items:
            return
        notification_id = str(normalized["id"])

        def render(seen: set[str]) -> tuple[str, list[str]] | None:
            pending = [
                item for item in items if item["delivery_id"] not in seen
            ]
            if not pending:
                return None
            return (
                format_notification(notification_id, pending),
                [item["delivery_id"] for item in pending],
            )

        self._enqueue_injection(
            _PendingInjection(
                {"notification": True, "notification_id": notification_id},
                render,
            )
        )

    def _enqueue_injection(self, record: _PendingInjection) -> None:
        if self._pending_injections.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._pending_injections.get_nowait()
            logger.warning(
                "消息注入队列已满（上限 %d），丢弃最旧的注入项。",
                _INJECTION_QUEUE_MAXSIZE,
            )
        try:
            self._pending_injections.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning("消息注入队列已满，丢弃新的注入项。")

    def _drain_injections(self) -> list[_PendingInjection]:
        records = []
        while True:
            try:
                records.append(self._pending_injections.get_nowait())
            except asyncio.QueueEmpty:
                return records

    def _inject_pending_injections(self) -> None:
        records = self._drain_injections()
        if not records:
            return
        rendered: list[tuple[_PendingInjection, str, list[str]]] = []
        for record in records:
            try:
                result = record.render(self._turn_injected_ids)
            except Exception:
                logger.warning("消息注入渲染失败，已跳过。", exc_info=True)
                continue
            if result is None:
                continue
            content, delivery_ids = result
            if not content:
                continue
            rendered.append((record, content, delivery_ids))
            self._turn_injected_ids.update(delivery_ids)
        if not rendered:
            return
        self.messages.append(
            {
                "role": "user",
                "content": "\n\n".join(content for _, content, _ in rendered),
                **_merge_injection_metadata(rendered),
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
        self._turn_injected_ids = set()
        self._handlers = {}
        self._permission_denials = []
        self._turn_usage = {}
        self._turn_compactions = []
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
            inject_project_instructions(self.messages, paths.workdir())
            text, cancelled = await self._agent_loop(client, definitions)
            self._save()
            usage = {
                "context_tokens": estimate_tokens(
                    self.messages,
                    tools_definition=definitions,
                )
            }
            usage.update(self._turn_usage)
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
            compactions=list(self._turn_compactions),
            is_error=is_error,
            error=error,
            cancelled=cancelled,
            permission_denials=list(self._permission_denials),
            duration_ms=duration_ms,
        )
