import asyncio
import copy
import hashlib
import hmac
import inspect
import itertools
import json
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import httpx
from anthropic import AsyncAnthropic
from openai import APIError, AsyncOpenAI, Timeout

from prompts import get_memory_decision_system_prompt, get_summary_system_prompt, get_summary_user_prompt


_LLM_TIMEOUT = Timeout(120, connect=10)
_LLM_MAX_RETRIES = 5
_CLIENT_REQUEST_IDS = itertools.count(1)
_CURRENT_CLIENT_REQUEST_ID: ContextVar[int | None] = ContextVar(
    "current_client_request_id", default=None
)


class MessageMetadata(TypedDict, total=False):
    source_format: Literal["openai_chat", "anthropic"]
    source_model: str
    native_blocks: list[dict[str, Any]]


class LLMMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: Any
    reasoning_content: str
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    name: str
    content_blocks: list[dict[str, Any]]
    message_metadata: MessageMetadata
    stop_reason: str
    usage: dict[str, Any]


@dataclass
class LLMResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: LLMMessage = field(default_factory=lambda: {"role": "assistant", "content": ""})
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None


def _normalized_content_blocks(
    text: str,
    reasoning: str,
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if reasoning:
        blocks.append({"type": "reasoning", "text": reasoning})
    if text:
        blocks.append({"type": "text", "text": text})
    for tool_call in tool_calls:
        function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        blocks.append({
            "type": "tool_call",
            "id": tool_call.get("id", ""),
            "name": tool_call.get("name") or function.get("name", ""),
            "arguments": tool_call.get("arguments", function.get("arguments", "")),
        })
    return blocks


def build_assistant_message(
    *,
    text: str = "",
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    content_blocks: list[dict[str, Any]] | None = None,
    source_format: Literal["openai_chat", "anthropic"],
    source_model: str,
    native_blocks: list[dict[str, Any]] | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> LLMMessage:
    message: LLMMessage = {"role": "assistant", "content": text or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = copy.deepcopy(tool_calls)
    if content_blocks:
        message["content_blocks"] = copy.deepcopy(content_blocks)

    metadata: MessageMetadata = {
        "source_format": source_format,
        "source_model": source_model,
    }
    if native_blocks:
        metadata["native_blocks"] = copy.deepcopy(native_blocks)
    message["message_metadata"] = metadata
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    if usage is not None:
        message["usage"] = copy.deepcopy(usage)
    return message


def build_llm_result(
    *,
    text: str = "",
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    assistant_tool_calls: list[dict[str, Any]] | None = None,
    content_blocks: list[dict[str, Any]] | None = None,
    source_format: Literal["openai_chat", "anthropic"],
    source_model: str,
    native_blocks: list[dict[str, Any]] | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> LLMResult:
    normalized_tool_calls = copy.deepcopy(tool_calls or [])
    normalized_assistant_tool_calls = copy.deepcopy(
        assistant_tool_calls if assistant_tool_calls is not None else normalized_tool_calls
    )
    normalized_blocks = copy.deepcopy(content_blocks) if content_blocks is not None else _normalized_content_blocks(
        text,
        reasoning,
        normalized_tool_calls,
    )
    return LLMResult(
        text=text,
        reasoning=reasoning,
        tool_calls=normalized_tool_calls,
        assistant_message=build_assistant_message(
            text=text,
            reasoning=reasoning,
            tool_calls=normalized_assistant_tool_calls,
            content_blocks=normalized_blocks,
            source_format=source_format,
            source_model=source_model,
            native_blocks=native_blocks,
            stop_reason=stop_reason,
            usage=usage,
        ),
        stop_reason=stop_reason,
        usage=copy.deepcopy(usage),
    )


def strip_native_message_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = copy.deepcopy(messages)
    for message in sanitized:
        metadata = message.get("message_metadata")
        if isinstance(metadata, dict):
            metadata.pop("native_blocks", None)

        content_blocks = message.get("content_blocks")
        if isinstance(content_blocks, list):
            message["content_blocks"] = [
                block
                for block in content_blocks
                if not isinstance(block, dict) or block.get("type") != "native"
            ]

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    tool_call.pop("raw", None)
    return sanitized


def _set_client_request_retry(
    retry_count: int,
    max_retries: int,
    response: httpx.Response | None,
    reason: str | None = None,
) -> None:
    request_id = _CURRENT_CLIENT_REQUEST_ID.get()
    if request_id is None:
        return
    try:
        from system.tui_app import TuiRegion, post_tui, set_client_request_retry

        set_client_request_retry(request_id, retry_count, max_retries)
        reason_text = reason or (
            f"HTTP {response.status_code}" if response is not None else "连接或超时错误"
        )
        post_tui(
            TuiRegion.BACKGROUND,
            f"[#aaaaaa]🌐 LLM 请求失败（{reason_text}），正在重试 {retry_count}/{max_retries}。[/#aaaaaa]",
        )
    except Exception:
        pass


class _LLMRequestCancelled(Exception):
    pass


def _is_response_cancelled() -> bool:
    from system.stream_cancel import is_cancelled

    return is_cancelled()


def _raise_if_response_cancelled() -> None:
    if _is_response_cancelled():
        raise _LLMRequestCancelled()


class _TrackedAsyncOpenAI(AsyncOpenAI):
    def _should_retry(self, response: httpx.Response) -> bool:
        # 中转场景下 404 多为上游瞬时路由失败（部分渠道缺模型），计入重试
        if response.status_code == 404 and response.headers.get("x-should-retry") is None:
            return True
        return super()._should_retry(response)

    async def _sleep_for_retry(self, *, retries_taken, max_retries, options, response) -> None:
        _raise_if_response_cancelled()
        _set_client_request_retry(retries_taken + 1, max_retries, response)
        await super()._sleep_for_retry(
            retries_taken=retries_taken,
            max_retries=max_retries,
            options=options,
            response=response,
        )
        _raise_if_response_cancelled()


class _TrackedAsyncAnthropic(AsyncAnthropic):
    def _should_retry(self, response: httpx.Response) -> bool:
        # 中转场景下 404 多为上游瞬时路由失败（部分渠道缺模型），计入重试
        if response.status_code == 404 and response.headers.get("x-should-retry") is None:
            return True
        return super()._should_retry(response)

    async def _sleep_for_retry(self, *, retries_taken, max_retries, options, response) -> None:
        _raise_if_response_cancelled()
        _set_client_request_retry(retries_taken + 1, max_retries, response)
        await super()._sleep_for_retry(
            retries_taken=retries_taken,
            max_retries=max_retries,
            options=options,
            response=response,
        )
        _raise_if_response_cancelled()


@contextmanager
def _client_request_active():
    request_id = next(_CLIENT_REQUEST_IDS)
    request_token = _CURRENT_CLIENT_REQUEST_ID.set(request_id)
    active_started = False
    try:
        from system.tui_app import set_client_request_active

        set_client_request_active(True, request_id=request_id)
        active_started = True
    except Exception:
        pass
    try:
        yield
    finally:
        if active_started:
            try:
                from system.tui_app import set_client_request_active

                set_client_request_active(False, request_id=request_id)
            except Exception:
                pass
        try:
            _CURRENT_CLIENT_REQUEST_ID.reset(request_token)
        except ValueError:
            _CURRENT_CLIENT_REQUEST_ID.set(None)


async def _tracked_async_stream(create_stream):
    stream = await create_stream()
    try:
        async for chunk in stream:
            yield chunk
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def _extract_tool_info(raw_tool):
    """
    统一提取器：兼容 pydantic_function_tool 和 MCP 原生 Tool
    返回: (name, description, parameters)
    """
    if "function" in raw_tool:
        func = raw_tool["function"]
        name = func.get("name")
        desc = func.get("description", "")
        params = func.get("parameters", {})
    else:
        name = raw_tool.get("name")
        desc = raw_tool.get("description", "")
        params = raw_tool.get("inputSchema", {})

    return name, desc, params


def _inline_schema_refs(schema):
    schema = copy.deepcopy(schema)
    root = copy.deepcopy(schema)

    def resolve_ref(ref):
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {"$ref": ref}

        current = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or part not in current:
                return {"$ref": ref}
            current = current[part]
        return walk(copy.deepcopy(current))

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                resolved = resolve_ref(node["$ref"])
                siblings = {k: walk(v) for k, v in node.items() if k != "$ref"}
                if isinstance(resolved, dict):
                    resolved.update(siblings)
                return resolved
            return {
                k: walk(v)
                for k, v in node.items()
                if k not in {"$defs", "definitions"}
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def format_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        source_tools = tool.get("tools", []) if isinstance(tool, dict) and tool.get("type") == "namespace" else [tool]
        for source_tool in source_tools:
            name, desc, params = _extract_tool_info(source_tool)
            function = {
                "name": name,
                "description": desc,
                "parameters": _inline_schema_refs(params),
            }
            if "function" in source_tool:
                function["strict"] = True
            result.append({"type": "function", "function": function})
    return sorted(result, key=lambda item: item["function"]["name"] or "")


class AsyncBaseLLMClient(ABC):
    def __init__(
        self,
        client: Any,
        model: str,
        reasoning_effort: str = "medium",
    ):
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    @abstractmethod
    async def generate_stream(
        self,
        messages: list,
        tools: list = None,
    ):
        """Yields stream events and ends with a done event containing LLMResult."""
        raise NotImplementedError

    def format_tool_result(
            self,
            tool_call_id: str,
            tool_name: str,
            output: Any,
            is_error: bool = False,
    ) -> dict:
        result = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str)
            else output,
        }
        if is_error:
            result["is_error"] = True
        return result

    def append_assistant_message(self, messages: list, raw_message: Any):
        msg_dict = (
            raw_message.model_dump()
            if hasattr(raw_message, "model_dump")
            else dict(raw_message)
        )
        messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        return format_openai_tools(pydantic_tools)

    async def get_summary(self, conversation_text: str, reason: str) -> str:
        async for event in self.get_summary_stream_events(conversation_text, reason):
            if event.get("type") == "done":
                return event["result"].text
        return ""

    async def get_summary_stream_events(
        self,
        conversation_text: str,
        reason: str,
        tools: list = None,
    ):
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        formatted_tools = self.format_tools(tools) if tools else None
        text_parts = []
        last_result = None
        for _ in range(8):
            final_result = None
            async for event in self.generate_stream(messages, formatted_tools):
                if event.get("type") == "done":
                    final_result = event["result"]
                    continue
                if event.get("type") == "text":
                    text_parts.append(event.get("content", ""))
                yield event
            if final_result is None:
                return
            last_result = final_result
            if getattr(final_result, "stop_reason", None) != "pause_turn":
                final_result.text = "".join(text_parts) or final_result.text
                final_result.assistant_message["content"] = final_result.text or None
                yield {"type": "done", "result": final_result}
                return
            messages.append(final_result.assistant_message)
        if last_result is not None:
            last_result.text = "".join(text_parts) or last_result.text
            last_result.assistant_message["content"] = last_result.text or None
            yield {"type": "done", "result": last_result}

    def get_memory_decision_messages(
        self,
        conversation_text: str,
        summary: str,
        reason: str,
        current_memory_content: str,
        mode: str = "compact",
    ) -> list:
        summary_section = f"## Summary\n{summary}\n\n" if summary.strip() else ""
        return [
            {"role": "system", "content": get_memory_decision_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"# Memory Management Request\n\n"
                    f"## Mode\n{mode}\n\n"
                    f"## Reason or User Request\n{reason}\n\n"
                    f"## Current Active Long-Term Memories\n{current_memory_content or '(none)'}\n\n"
                    f"{summary_section}"
                    f"## Conversation Transcript JSON\n{conversation_text}"
                ),
            },
        ]


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    try:
        return {key: copy.deepcopy(item) for key, item in value}
    except (TypeError, ValueError):
        return {
            key: copy.deepcopy(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }


def _is_retryable_openai_stream_error(exc: Exception) -> bool:
    return isinstance(exc, APIError) and str(exc) == "Upstream HTTP/2 stream failed"


def _openai_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    result = []
    for raw_tool_call in tool_calls or []:
        tool_call = _to_plain_dict(raw_tool_call)
        function = tool_call.get("function")
        if function is not None:
            function = _to_plain_dict(function)
            name = function.get("name", "")
            arguments = function.get("arguments", "")
        else:
            name = tool_call.get("name", "")
            arguments = tool_call.get("arguments", "")
        result.append({
            "id": tool_call.get("id", ""),
            "type": tool_call.get("type") or "function",
            "function": {"name": name, "arguments": arguments},
        })
    return result


def _tool_calls_from_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = []
    for block in message.get("content_blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_call":
            continue
        arguments = block.get("arguments", "")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        tool_calls.append({
            "id": block.get("id", ""),
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": arguments,
            },
        })
    return tool_calls


def _text_from_content_blocks(message: dict[str, Any]) -> str:
    return "".join(
        block.get("text", "")
        for block in message.get("content_blocks") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def sanitize_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for message in messages:
        role = message.get("role")
        clean_message: dict[str, Any] = {"role": role}
        if "content" in message:
            content = copy.deepcopy(message.get("content"))
            if role == "assistant" and content is None:
                content = _text_from_content_blocks(message) or None
            clean_message["content"] = content
        elif role == "assistant":
            normalized_text = _text_from_content_blocks(message)
            if normalized_text:
                clean_message["content"] = normalized_text
        if role in {"system", "user", "assistant", "tool"} and message.get("name"):
            clean_message["name"] = message["name"]
        if role == "assistant":
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                clean_message["reasoning_content"] = reasoning_content
            tool_calls = message.get("tool_calls") or _tool_calls_from_content_blocks(message)
            if tool_calls:
                clean_message["tool_calls"] = _openai_tool_calls(tool_calls)
        if role == "tool":
            clean_message["tool_call_id"] = message.get("tool_call_id", "")
        sanitized.append(clean_message)
    return sanitized


def build_openai_prompt_cache_key(
    *,
    api_key: str,
    base_url: str,
    model: str,
    reasoning_effort: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> str:
    leading_system_messages = []
    for message in messages:
        if message.get("role") != "system":
            break
        leading_system_messages.append(message)

    identity = {
        "version": 2,
        "base_url": base_url,
        "message_format": "openai_chat",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "system_messages": leading_system_messages,
        "tools": tools or [],
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(
        api_key.encode("utf-8"),
        b"makecode-prompt-cache-v2\0" + canonical,
        hashlib.sha256,
    ).hexdigest()[:48]
    return f"mc-pc2-{digest}"


class AsyncChatAPIClient(AsyncBaseLLMClient):
    def __init__(
        self,
        client: Any,
        model: str,
        reasoning_effort: str = "medium",
        *,
        base_url: str = "",
        api_key: str = "",
    ):
        super().__init__(client, model, reasoning_effort)
        self.base_url = base_url
        self.api_key = api_key

    async def generate_stream(
        self,
        messages: list,
        tools: list = None,
    ):
        with _client_request_active():
            async for event in self._generate_stream(messages, tools):
                yield event

    async def _generate_stream(
        self,
        messages: list,
        tools: list = None,
    ):
        request_messages = sanitize_openai_messages(messages)
        kwargs = {
            "model": self.model,
            "messages": request_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": self.reasoning_effort,
            "prompt_cache_key": build_openai_prompt_cache_key(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                messages=request_messages,
                tools=tools,
            ),
            "prompt_cache_retention": "24h",
        }
        if tools:
            kwargs["tools"] = tools

        configured_max_retries = getattr(self.client, "max_retries", 0)
        max_retries = (
            max(0, configured_max_retries)
            if isinstance(configured_max_retries, int)
            else 0
        )
        retries_taken = 0
        output_started = False
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        merged_tool_calls: dict[int, dict[str, Any]] = {}
        stop_reason = None
        usage = None
        tool_calls_started = False

        while True:
            if _is_response_cancelled():
                return
            text_parts = []
            reasoning_parts = []
            merged_tool_calls = {}
            stop_reason = None
            usage = None
            tool_calls_started = False
            stream = _tracked_async_stream(
                lambda: self.client.chat.completions.create(**kwargs)
            )
            try:
                async for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = _to_plain_dict(chunk_usage)
                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]
                    delta = choice.delta
                    content = getattr(delta, "content", None)
                    if content:
                        output_started = True
                        text_parts.append(content)
                        yield {"type": "text", "content": content}

                    reasoning = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                    )
                    if reasoning:
                        output_started = True
                        reasoning_parts.append(reasoning)
                        yield {"type": "reasoning", "content": reasoning}

                    delta_tool_calls = getattr(delta, "tool_calls", None)
                    if delta_tool_calls:
                        output_started = True
                        if not tool_calls_started:
                            tool_calls_started = True
                            yield {"type": "tool_calls"}
                        for fallback_index, raw_tool_call in enumerate(delta_tool_calls):
                            tool_call = _to_plain_dict(raw_tool_call)
                            index = tool_call.get("index")
                            if index is None:
                                index = fallback_index
                            merged = merged_tool_calls.setdefault(index, {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                            if tool_call.get("id"):
                                merged["id"] = tool_call["id"]
                            if tool_call.get("type"):
                                merged["type"] = tool_call["type"]
                            function = tool_call.get("function")
                            if function is not None:
                                function = _to_plain_dict(function)
                                if function.get("name"):
                                    merged["function"]["name"] = function["name"]
                                if function.get("arguments"):
                                    merged["function"]["arguments"] += function["arguments"]

                    if choice.finish_reason is not None:
                        stop_reason = choice.finish_reason
                break
            except _LLMRequestCancelled:
                return
            except (json.JSONDecodeError, APIError) as exc:
                if isinstance(exc, APIError) and not _is_retryable_openai_stream_error(exc):
                    raise
                if output_started or retries_taken >= max_retries:
                    raise
                retries_taken += 1
                reason = (
                    "流式响应格式错误"
                    if isinstance(exc, json.JSONDecodeError)
                    else str(exc)
                )
                _set_client_request_retry(
                    retries_taken,
                    max_retries,
                    None,
                    reason,
                )
                await asyncio.sleep(min(0.5 * (2 ** (retries_taken - 1)), 8.0))
                if _is_response_cancelled():
                    return
            finally:
                await stream.aclose()

        assistant_tool_calls = [
            merged_tool_calls[index]
            for index in sorted(merged_tool_calls)
            if merged_tool_calls[index]["id"] and merged_tool_calls[index]["function"]["name"]
        ]
        tool_calls = [
            {
                "id": tool_call["id"],
                "name": tool_call["function"]["name"],
                "arguments": tool_call["function"]["arguments"],
                "raw": copy.deepcopy(tool_call),
            }
            for tool_call in assistant_tool_calls
        ]
        result = build_llm_result(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            assistant_tool_calls=assistant_tool_calls,
            source_format="openai_chat",
            source_model=self.model,
            stop_reason=stop_reason,
            usage=usage,
        )
        yield {
            "type": "done",
            "result": result,
        }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _anthropic_tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return copy.deepcopy(arguments)
    if arguments is None or arguments == "":
        return {}
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise ValueError("Anthropic tool input must decode to a JSON object")
    return parsed


def _anthropic_tool_use_block(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function", {})
    name = tool_call.get("name") or function.get("name", "")
    arguments = tool_call.get("arguments", function.get("arguments", ""))
    return {
        "type": "tool_use",
        "id": tool_call.get("id", ""),
        "name": name,
        "input": _anthropic_tool_input(arguments),
    }


def _anthropic_user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": "" if content is None else str(content)}]

    blocks = []
    for block in content:
        if isinstance(block, str):
            blocks.append({"type": "text", "text": block})
        elif isinstance(block, dict):
            blocks.append(copy.deepcopy(block))
    return blocks


def _anthropic_assistant_content(message: dict[str, Any], model: str) -> list[dict[str, Any]]:
    metadata = message.get("message_metadata")
    if (
        isinstance(metadata, dict)
        and metadata.get("source_format") == "anthropic"
        and metadata.get("source_model") == model
        and isinstance(metadata.get("native_blocks"), list)
    ):
        return copy.deepcopy(metadata["native_blocks"])

    blocks = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks.extend(
            copy.deepcopy(block)
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not any(block.get("type") == "text" for block in blocks):
        normalized_text = _text_from_content_blocks(message)
        if normalized_text:
            blocks.append({"type": "text", "text": normalized_text})
    tool_calls = message.get("tool_calls") or _tool_calls_from_content_blocks(message)
    for tool_call in tool_calls:
        blocks.append(_anthropic_tool_use_block(tool_call))
    return blocks


def build_anthropic_request_messages(
    messages: list[dict[str, Any]],
    model: str,
) -> tuple[str, list[dict[str, Any]]]:
    system_parts = []
    request_messages = []
    index = 0

    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "system":
            text = _content_text(message.get("content"))
            if text:
                system_parts.append(text)
            index += 1
            continue

        if role == "tool":
            tool_results = []
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_message = messages[index]
                block = {
                    "type": "tool_result",
                    "tool_use_id": tool_message.get("tool_call_id", ""),
                    "content": tool_message.get("content", ""),
                }
                if tool_message.get("is_error") is True:
                    block["is_error"] = True
                tool_results.append(block)
                index += 1
            request_messages.append({"role": "user", "content": tool_results})
            continue

        if role == "assistant":
            content = _anthropic_assistant_content(message, model)
        elif role == "user":
            content = _anthropic_user_content(message.get("content"))
        else:
            index += 1
            continue
        request_messages.append({"role": role, "content": content})
        index += 1

    return "\n\n".join(system_parts), request_messages


def format_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        candidates = tool.get("tools", []) if tool.get("type") == "namespace" else [tool]
        for candidate in candidates:
            if "function" in candidate:
                function = candidate["function"]
                formatted = {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "input_schema": _inline_schema_refs(function.get("parameters", {})),
                }
                if function.get("strict") is True:
                    formatted["strict"] = True
            else:
                formatted = {
                    "name": candidate.get("name"),
                    "description": candidate.get("description", ""),
                    "input_schema": _inline_schema_refs(
                        candidate.get("input_schema", candidate.get("inputSchema", {}))
                    ),
                }
                if candidate.get("strict") is True:
                    formatted["strict"] = True
            result.append(formatted)
    return sorted(result, key=lambda item: item.get("name") or "")


def _normalized_anthropic_blocks(native_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for block in native_blocks:
        block_type = block.get("type")
        if block_type == "text":
            normalized.append({"type": "text", "text": block.get("text", "")})
        elif block_type == "thinking":
            normalized.append({"type": "reasoning", "text": block.get("thinking", "")})
        elif block_type == "redacted_thinking":
            normalized.append({"type": "redacted_reasoning"})
        elif block_type == "tool_use":
            normalized.append({
                "type": "tool_call",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
            })
        else:
            normalized.append({
                "type": "native",
                "native_type": block_type,
                "block": copy.deepcopy(block),
            })
    return normalized


class AnthropicMessagesClient(AsyncBaseLLMClient):
    def format_tools(self, pydantic_tools: list) -> list:
        return format_anthropic_tools(pydantic_tools)

    async def generate_stream(
        self,
        messages: list,
        tools: list = None,
    ):
        system, request_messages = build_anthropic_request_messages(messages, self.model)
        kwargs = {
            "model": self.model,
            "max_tokens": 64_000,
            "messages": request_messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.reasoning_effort},
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = tools

        with _client_request_active():
            try:
                async with self.client.messages.stream(**kwargs) as stream:
                    tool_calls_started = False
                    async for event in stream:
                        if _is_response_cancelled():
                            return
                        event_type = getattr(event, "type", None)
                        if event_type == "content_block_start":
                            block_type = getattr(event.content_block, "type", None)
                            if block_type == "tool_use" and not tool_calls_started:
                                tool_calls_started = True
                                yield {"type": "tool_calls"}
                        elif event_type == "content_block_delta":
                            delta_type = getattr(event.delta, "type", None)
                            if delta_type == "text_delta" and event.delta.text:
                                yield {"type": "text", "content": event.delta.text}
                            elif delta_type == "thinking_delta" and event.delta.thinking:
                                yield {"type": "reasoning", "content": event.delta.thinking}
                    final_message = await stream.get_final_message()
            except _LLMRequestCancelled:
                return

        native_blocks = [_to_plain_dict(block) for block in final_message.content]
        text = "".join(
            block.get("text", "")
            for block in native_blocks
            if block.get("type") == "text"
        )
        reasoning = "".join(
            block.get("thinking", "")
            for block in native_blocks
            if block.get("type") == "thinking"
        )
        assistant_tool_calls = []
        tool_calls = []
        for block in native_blocks:
            if block.get("type") != "tool_use":
                continue
            arguments = json.dumps(block.get("input", {}), ensure_ascii=False)
            assistant_tool_call = {
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": arguments,
                },
            }
            assistant_tool_calls.append(assistant_tool_call)
            tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": arguments,
                "raw": copy.deepcopy(block),
            })

        result = build_llm_result(
            text=text,
            reasoning=reasoning,
            tool_calls=tool_calls,
            assistant_tool_calls=assistant_tool_calls,
            content_blocks=_normalized_anthropic_blocks(native_blocks),
            source_format="anthropic",
            source_model=self.model,
            native_blocks=native_blocks,
            stop_reason=final_message.stop_reason,
            usage=_to_plain_dict(final_message.usage),
        )
        yield {
            "type": "done",
            "result": result,
        }


from system.models import get_current_model_config, get_model_manager


def _normalize_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if not re.search(r"/v[0-9]+$", base_url):
        return f"{base_url}/v1"
    return base_url


def _normalize_anthropic_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if re.search(r"/v1$", base_url):
        return base_url[:-3]
    return base_url


def _create_async_chat_client(model_config):
    if model_config.message_format == "anthropic":
        client = _TrackedAsyncAnthropic(
            base_url=_normalize_anthropic_base_url(model_config.base_url),
            api_key=model_config.api_key,
            timeout=_LLM_TIMEOUT,
            max_retries=_LLM_MAX_RETRIES,
            default_headers={"User-Agent": "MakeCode Agent"},
        )
        return AnthropicMessagesClient(
            client,
            model_config.model_id,
            model_config.reasoning_effort,
        )

    normalized_base_url = _normalize_base_url(model_config.base_url)
    client = _TrackedAsyncOpenAI(
        base_url=normalized_base_url,
        api_key=model_config.api_key,
        timeout=_LLM_TIMEOUT,
        max_retries=_LLM_MAX_RETRIES,
        default_headers={"User-Agent": "MakeCode Agent"},
    )
    return AsyncChatAPIClient(
        client,
        model_config.model_id,
        model_config.reasoning_effort,
        base_url=normalized_base_url,
        api_key=model_config.api_key,
    )


def format_tools_for_current_model(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_model = get_current_model_config()
    if current_model is None:
        raise RuntimeError("No model configured. Please use /models to configure a model first.")
    if current_model.message_format == "anthropic":
        return format_anthropic_tools(tools)
    return format_openai_tools(tools)


async def close_async_llm_client(client) -> None:
    raw_client = getattr(client, "client", None)
    close = getattr(raw_client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def create_current_async_llm_client():
    current_model = get_current_model_config()
    if current_model is None:
        return None
    return _create_async_chat_client(current_model)


def create_memory_recall_llm_client():
    manager = get_model_manager()
    if manager is None:
        return None
    recall_model = manager.get_memory_recall_model()
    if recall_model is None:
        current_model = manager.get_current_model()
        if current_model is None:
            return None
        return _create_async_chat_client(current_model)
    return _create_async_chat_client(recall_model)
