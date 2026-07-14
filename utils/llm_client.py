import copy
import json
from abc import ABC, abstractmethod
from typing import Any, Union, Generator

from openai import OpenAI, AsyncOpenAI

# Monkey-patch OpenAI SDK 重试策略：重试3次，等待 10s → 20s → 30s
import openai._constants as _openai_consts
_openai_consts.INITIAL_RETRY_DELAY = 10
_openai_consts.MAX_RETRY_DELAY = 30

from prompts import get_memory_decision_system_prompt, get_summary_system_prompt, get_summary_user_prompt


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


class BaseLLMClient(ABC):
    def __init__(
        self,
        client: Union[OpenAI, AsyncOpenAI],
        model: str,
        reasoning_effort: str = "medium",
    ):
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    @abstractmethod
    def generate(self, messages: list, tools: list = None):
        """Unified interface for generating a response."""
        pass

    @abstractmethod
    def generate_stream(self, messages: list, tools: list = None):
        """Streaming generation. Yields event dicts:
        {type: 'text', content: str}       - text delta
        {type: 'done', content: (text, tool_calls, raw_message)}  - stream finished
        """
        pass

    @abstractmethod
    def parse_response(self, response) -> tuple[str, list, Any]:
        """
        Parses the API response.
        Returns: (text_content, tool_calls_list, raw_message)
        tool_calls_list items should have: "id", "name", "arguments", "raw"
        """
        pass

    @abstractmethod
    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: Any
    ) -> dict:
        """Formats the result of the tool execution to be appended to messages."""
        pass

    @abstractmethod
    def append_assistant_message(self, messages: list, raw_message: Any):
        """Appends the assistant's response (with tool calls if any) to the history."""
        pass

    @abstractmethod
    def format_tools(self, pydantic_tools: list) -> list:
        """Formats the tool definitions for the specific API standard."""
        pass

    @abstractmethod
    def get_summary(self, conversation_text: str, reason: str) -> str:
        """Generates a summary of the conversation."""
        pass

    @abstractmethod
    def get_summary_stream(self, conversation_text: str, reason: str) -> Generator[str, None, None]:
        """Generates a streaming summary of the conversation, yielding text chunks."""
        pass


class AsyncBaseLLMClient(ABC):
    @abstractmethod
    async def generate(self, messages: list, tools: list = None):
        pass

    @abstractmethod
    def parse_response(self, response) -> tuple[str, list, Any]:
        pass

    @abstractmethod
    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: Any
    ) -> dict:
        pass

    @abstractmethod
    def append_assistant_message(self, messages: list, raw_message: Any):
        pass

    @abstractmethod
    def format_tools(self, pydantic_tools: list) -> list:
        pass

    @abstractmethod
    async def get_summary(self, conversation_text: str, reason: str) -> str:
        pass


class ChatAPIClient(BaseLLMClient):
    """Implementation for the standard OpenAI Chat Completions API standard."""

    def generate(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages, "reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = tools
        return self.client.chat.completions.create(**kwargs)

    def generate_stream(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages, "stream": True, "reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = tools

        stream = self.client.chat.completions.create(**kwargs)

        # 累积所有 delta，最终拼合为完整的 raw_message
        # 原理：stream 返回的每个 chunk.choices[0].delta 是 message 的一个片段，
        # 将所有 delta 的有效字段逐步合并，即可重建与非流式 message 一致的结构，
        # 确保任何出现的字段都不会丢失。
        # 只有文本片段需要实时 yield（用于流式渲染），工具调用等其余字段全部留到最后统一解析。
        response_deltas = []  # 保存所有 delta，最终拼合为 raw_message

        def _build_done_event():
            """根据累积的 delta 列表构建 done 事件"""
            # 拼合所有 delta 为完整的 raw_message
            # 纯文本类字段（增量字符串，需要拼接）
            _TEXT_FIELDS = ("content", "reasoning_content", "reasoning")

            raw_message = {}
            merged_text_parts = {field: [] for field in _TEXT_FIELDS}
            merged_tool_calls = {}  # idx -> {id, type, function: {name, arguments}}
            for delta in response_deltas:
                for key, value in delta:
                    if value is None:
                        continue
                    if key in _TEXT_FIELDS:
                        merged_text_parts[key].append(value)
                    elif key == "tool_calls":
                        for tc in value:
                            idx = tc.index
                            if idx not in merged_tool_calls:
                                merged_tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                            if tc.id:
                                merged_tool_calls[idx]["id"] = tc.id
                            if hasattr(tc, "type") and tc.type:
                                merged_tool_calls[idx]["type"] = tc.type
                            if tc.function:
                                if tc.function.name:
                                    merged_tool_calls[idx]["function"]["name"] = tc.function.name
                                if tc.function.arguments:
                                    merged_tool_calls[idx]["function"]["arguments"] += tc.function.arguments
                    else:
                        # 标量字段直接覆盖（如 role, refusal 等）
                        raw_message[key] = value

            # 组装 raw_message：所有文本字段统一拼合写入
            for field in _TEXT_FIELDS:
                parts = merged_text_parts[field]
                raw_message[field] = "".join(parts) if parts else None
            raw_message["role"] = "assistant"
            # 移除 content 以外值为 None 的字段，保持消息干净
            for k in list(raw_message.keys()):
                if k != "content" and raw_message[k] is None:
                    del raw_message[k]
            # text 仍取 content 作为主文本返回
            text = raw_message.get("content") or ""

            # 过滤无效的 tool_calls（id 或 name 为空则丢弃）
            valid_tool_calls = {
                idx: tc for idx, tc in merged_tool_calls.items()
                if tc["id"] and tc["function"]["name"]
            }
            if valid_tool_calls:
                raw_message["tool_calls"] = [
                    valid_tool_calls[idx]
                    for idx in sorted(valid_tool_calls.keys())
                ]

            # 构建 tool_calls 列表（给调用方使用）
            tool_calls = []
            for idx in sorted(valid_tool_calls.keys()):
                tc = valid_tool_calls[idx]
                tool_calls.append({
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                    "raw": tc,
                })

            return {"type": "done", "content": (text, tool_calls, raw_message)}

        from system.stream_cancel import stream_cancel_event

        for chunk in stream:
            # ESC 取消检查：用户按下 ESC 后立即中断流式读取
            if stream_cancel_event.is_set():
                break

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            # 保存 delta 用于最终拼合
            response_deltas.append(delta)

            # 实时 yield 文本片段（用于流式渲染）
            if delta.content:
                yield {"type": "text", "content": delta.content}

            # 实时 yield reasoning 片段（用于思考过程流式渲染）
            reasoning_val = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning_val:
                yield {"type": "reasoning", "content": reasoning_val}

            if getattr(delta, "tool_calls", None):
                yield {"type": "tool_calls", "content": None}

            # 流结束：统一解析所有累积的 delta，构建 done 事件
            if choice.finish_reason in ("tool_calls", "stop"):
                yield _build_done_event()
                return

        # 安全兜底：流 EOF 但未收到 finish_reason（如 finish_reason='length'）
        # 此时用已累积的数据构建 done 事件，避免 raw_message=None 崩溃
        yield _build_done_event()

    def parse_response(self, response) -> tuple[str, list, Any]:
        message = response.choices[0].message
        text_content = message.content or ""
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                # Chat standard returns arguments as a JSON string
                tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                        "raw": tc,
                    }
                )
        return text_content, tool_calls, message

    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: Any
    ) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str)
            else output,
        }

    def append_assistant_message(self, messages: list, raw_message: Any):
        # Standard Chat API requires the assistant message to be appended exactly as it is (including tool_calls)
        msg_dict = (
            raw_message.model_dump()
            if hasattr(raw_message, "model_dump")
            else dict(raw_message)
        )
        messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        # Standard format doesn't support "namespace" tools, so extract all functions into a flat list.
        result = []
        for t in pydantic_tools:
            if isinstance(t, dict) and t.get("type") == "namespace":
                for inner_t in t.get("tools", []):
                    name, desc, params = _extract_tool_info(inner_t)
                    func_def = {
                        "name": name,
                        "description": desc,
                        "parameters": _inline_schema_refs(params),
                    }
                    if "function" in inner_t:
                        func_def["strict"] = True
                    result.append({"type": "function", "function": func_def})
            else:
                name, desc, params = _extract_tool_info(t)
                func_def = {
                    "name": name,
                    "description": desc,
                    "parameters": _inline_schema_refs(params),
                }
                if "function" in t:
                    func_def["strict"] = True
                result.append({"type": "function", "function": func_def})
        return result

    def get_summary(self, conversation_text: str, reason: str, tools: list = None) -> str:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        kwargs = {"model": self.model, "messages": messages, "reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = self.format_tools(tools)
        res = self.client.chat.completions.create(**kwargs)
        text, tool_calls, raw_message = self.parse_response(res)
        if tools:
            return text, tool_calls, raw_message
        return text

    def get_summary_stream_events(self, conversation_text: str, reason: str, tools: list = None) -> Generator[dict, None, None]:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        yield from self.generate_stream(messages, self.format_tools(tools) if tools else None)

    def get_summary_stream(self, conversation_text: str, reason: str) -> Generator[str, None, None]:
        for event in self.get_summary_stream_events(conversation_text, reason):
            if event.get("type") == "text":
                yield event.get("content", "")

    def get_memory_decision_messages(self, conversation_text: str, summary: str, reason: str, current_memory_content: str, mode: str = "compact") -> list:
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

    def get_memory_decision(self, conversation_text: str, summary: str, reason: str, current_memory_content: str, tools: list, mode: str = "compact") -> tuple[str, list, Any]:
        messages = self.get_memory_decision_messages(
            conversation_text,
            summary,
            reason,
            current_memory_content,
            mode=mode,
        )
        res = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self.format_tools(tools),
            reasoning_effort=self.reasoning_effort,
        )
        return self.parse_response(res)

    def get_memory_decision_stream(self, conversation_text: str, summary: str, reason: str, current_memory_content: str, tools: list, mode: str = "compact"):
        messages = self.get_memory_decision_messages(
            conversation_text,
            summary,
            reason,
            current_memory_content,
            mode=mode,
        )
        return self.get_memory_decision_stream_messages(messages, tools)

    def get_memory_decision_stream_messages(self, messages: list, tools: list):
        for event in self.generate_stream(messages, self.format_tools(tools)):
            if event.get("type") == "done":
                return event["content"]
        return "", [], None


class AsyncChatAPIClient(ChatAPIClient, AsyncBaseLLMClient):
    async def generate(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages, "reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = tools
        return await self.client.chat.completions.create(**kwargs)

    async def generate_stream(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages, "stream": True, "reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = tools

        stream = await self.client.chat.completions.create(**kwargs)
        response_deltas = []

        def _build_done_event():
            _TEXT_FIELDS = ("content", "reasoning_content", "reasoning")

            raw_message = {}
            merged_text_parts = {field: [] for field in _TEXT_FIELDS}
            merged_tool_calls = {}
            for delta in response_deltas:
                for key, value in delta:
                    if value is None:
                        continue
                    if key in _TEXT_FIELDS:
                        merged_text_parts[key].append(value)
                    elif key == "tool_calls":
                        for tc in value:
                            idx = tc.index
                            if idx not in merged_tool_calls:
                                merged_tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                            if tc.id:
                                merged_tool_calls[idx]["id"] = tc.id
                            if hasattr(tc, "type") and tc.type:
                                merged_tool_calls[idx]["type"] = tc.type
                            if tc.function:
                                if tc.function.name:
                                    merged_tool_calls[idx]["function"]["name"] = tc.function.name
                                if tc.function.arguments:
                                    merged_tool_calls[idx]["function"]["arguments"] += tc.function.arguments
                    else:
                        raw_message[key] = value

            for field in _TEXT_FIELDS:
                parts = merged_text_parts[field]
                raw_message[field] = "".join(parts) if parts else None
            raw_message["role"] = "assistant"
            for k in list(raw_message.keys()):
                if k != "content" and raw_message[k] is None:
                    del raw_message[k]
            text = raw_message.get("content") or ""

            valid_tool_calls = {
                idx: tc for idx, tc in merged_tool_calls.items()
                if tc["id"] and tc["function"]["name"]
            }
            if valid_tool_calls:
                raw_message["tool_calls"] = [
                    valid_tool_calls[idx]
                    for idx in sorted(valid_tool_calls.keys())
                ]

            tool_calls = []
            for idx in sorted(valid_tool_calls.keys()):
                tc = valid_tool_calls[idx]
                tool_calls.append({
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                    "raw": tc,
                })

            return {"type": "done", "content": (text, tool_calls, raw_message)}

        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            response_deltas.append(delta)

            if delta.content:
                yield {"type": "text", "content": delta.content}

            reasoning_val = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning_val:
                yield {"type": "reasoning", "content": reasoning_val}

            if choice.finish_reason in ("tool_calls", "stop"):
                yield _build_done_event()
                return

        yield _build_done_event()

    async def get_summary(self, conversation_text: str, reason: str) -> str:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        res = await self.client.chat.completions.create(
            model=self.model, messages=messages, reasoning_effort=self.reasoning_effort
        )
        return res.choices[0].message.content or ""


from system.models import get_current_model_config, get_model_manager


_cached_llm_client = None
_cached_model_key = None


def _create_chat_client(model_config):
    client = OpenAI(
        base_url=model_config.base_url,
        api_key=model_config.api_key,
        max_retries=3,
        default_headers={"User-Agent": "MakeCode Agent"},
    )
    return ChatAPIClient(client, model_config.model_id, model_config.reasoning_effort)


def _create_llm_client():
    """根据当前模型配置动态创建 LLM 客户端"""
    global _cached_llm_client, _cached_model_key
    current_model = get_current_model_config()
    if current_model is None:
        _cached_llm_client = None
        _cached_model_key = None
        return None
    if current_model.runtime_key != _cached_model_key:
        _cached_llm_client = _create_chat_client(current_model)
        _cached_model_key = current_model.runtime_key
    return _cached_llm_client


def create_memory_recall_llm_client():
    manager = get_model_manager()
    if manager is None:
        return None
    recall_model = manager.get_memory_recall_model()
    if recall_model is None:
        current_model = manager.get_current_model()
        if current_model is None:
            return None
        return _create_chat_client(current_model)
    return _create_chat_client(recall_model)


class DynamicLLMClientProxy:
    """动态 LLM 客户端代理：每次调用时获取当前模型配置"""

    def _get_client(self):
        client = _create_llm_client()
        if client is None:
            raise RuntimeError("No model configured. Please use /models to configure a model first.")
        return client

    def __getattr__(self, item):
        return getattr(self._get_client(), item)


llm_client = DynamicLLMClientProxy()


def reload_llm_client():
    """兼容旧调用，当前为动态代理无需重载"""
    return _create_llm_client()
