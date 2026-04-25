import json
from abc import ABC, abstractmethod
from typing import Union, Generator

from openai import OpenAI, AsyncOpenAI

# Monkey-patch OpenAI SDK 重试策略：重试3次，等待 10s → 20s → 30s
import openai._constants as _openai_consts
_openai_consts.INITIAL_RETRY_DELAY = 10
_openai_consts.MAX_RETRY_DELAY = 30

from init import log_error_traceback
from prompts import get_summary_system_prompt, get_summary_user_prompt


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


class BaseLLMClient(ABC):
    def __init__(self, client: Union[OpenAI, AsyncOpenAI], model: str):
        self.client = client
        self.model = model

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
    def parse_response(self, response) -> tuple[str, list, any]:
        """
        Parses the API response.
        Returns: (text_content, tool_calls_list, raw_message)
        tool_calls_list items should have: "id", "name", "arguments", "raw"
        """
        pass

    @abstractmethod
    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: any
    ) -> dict:
        """Formats the result of a tool execution to be appended to messages."""
        pass

    @abstractmethod
    def append_assistant_message(self, messages: list, raw_message: any):
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
    def parse_response(self, response) -> tuple[str, list, any]:
        pass

    @abstractmethod
    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: any
    ) -> dict:
        pass

    @abstractmethod
    def append_assistant_message(self, messages: list, raw_message: any):
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
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        return self.client.chat.completions.create(**kwargs)

    def generate_stream(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages, "stream": True}
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

        for chunk in stream:
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

            # 流结束：统一解析所有累积的 delta，构建 done 事件
            if choice.finish_reason in ("tool_calls", "stop"):
                yield _build_done_event()
                return

        # 安全兜底：流 EOF 但未收到 finish_reason（如 finish_reason='length'）
        # 此时用已累积的数据构建 done 事件，避免 raw_message=None 崩溃
        yield _build_done_event()

    def parse_response(self, response) -> tuple[str, list, any]:
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
            self, tool_call_id: str, tool_name: str, output: any
    ) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str)
            else output,
        }

    def append_assistant_message(self, messages: list, raw_message: any):
        # Standard Chat API requires the assistant message to be appended exactly as it is (including tool_calls)
        msg_dict = (
            raw_message.model_dump()
            if hasattr(raw_message, "model_dump")
            else dict(raw_message)
        )
        messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        # Standard format doesn't need flattening, but it doesn't support "namespace" tools
        # We must extract all functions into a flat list
        result = []
        for t in pydantic_tools:
            if isinstance(t, dict) and t.get("type") == "namespace":
                for inner_t in t.get("tools", []):
                    name, desc, params = _extract_tool_info(inner_t)
                    func_def = {
                        "name": name,
                        "description": desc,
                        "parameters": params,
                    }
                    if "function" in inner_t:
                        func_def["strict"] = True
                    result.append({"type": "function", "function": func_def})
            else:
                name, desc, params = _extract_tool_info(t)
                func_def = {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                }
                if "function" in t:
                    func_def["strict"] = True
                result.append({"type": "function", "function": func_def})
        return result

    def get_summary(self, conversation_text: str, reason: str) -> str:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        res = self.client.chat.completions.create(model=self.model, messages=messages)
        return res.choices[0].message.content or ""

    def get_summary_stream(self, conversation_text: str, reason: str) -> Generator[str, None, None]:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]

        # 使用标准的 create 方法，开启 stream=True
        response_stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True
        )

        # 直接遍历返回的 stream 对象
        for chunk in response_stream:
            if chunk.choices:
                # 获取 delta content
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta


class AsyncChatAPIClient(ChatAPIClient, AsyncBaseLLMClient):
    async def generate(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        return await self.client.chat.completions.create(**kwargs)

    async def get_summary(self, conversation_text: str, reason: str) -> str:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        res = await self.client.chat.completions.create(
            model=self.model, messages=messages
        )
        return res.choices[0].message.content or ""


from system.models import get_current_model_config


def _create_llm_client():
    """根据当前模型配置动态创建 LLM 客户端"""
    current_model = get_current_model_config()
    if current_model is None:
        return None
    client = OpenAI(
        base_url=current_model.base_url,
        api_key=current_model.api_key,
        max_retries=3,
    )
    return ChatAPIClient(client, current_model.model_id)


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
