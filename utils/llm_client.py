import json
from abc import ABC, abstractmethod
from typing import Union, Generator, AsyncGenerator

from openai import OpenAI, AsyncOpenAI

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


def _make_response_tool(tool_dict):
    """Flatten pydantic_function_tool output for Responses API"""
    name, desc, params = _extract_tool_info(tool_dict)
    tool_def = {
        "type": "function",
        "name": name,
        "description": desc,
        "parameters": params,
    }
    if "function" in tool_dict:
        tool_def["strict"] = True
    return tool_def


class BaseLLMClient(ABC):
    def __init__(self, client: Union[OpenAI, AsyncOpenAI], model: str):
        self.client = client
        self.model = model

    @abstractmethod
    def generate(self, messages: list, tools: list = None):
        """Unified interface for generating a response."""
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


class ResponseAPIClient(BaseLLMClient):
    """Implementation for the custom/beta Responses API standard."""

    def generate(self, messages: list, tools: list = None):
        return self.client.responses.create(
            model=self.model, input=messages, tools=tools or []
        )

    def parse_response(self, response) -> tuple[str, list, any]:
        text_content = ""
        tool_calls = []
        for item in response.output:
            if item.type == "message":
                text_content += next(
                    (c.text for c in item.content if c.type == "output_text"), ""
                )
            elif item.type == "function_call":
                tool_calls.append(
                    {
                        "id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments,
                        "raw": item,
                    }
                )
        # The raw message in this case is the list of outputs, but we append them differently
        return text_content, tool_calls, response.output

    def format_tool_result(
            self, tool_call_id: str, tool_name: str, output: any
    ) -> dict:
        return {
            "type": "function_call_output",
            "call_id": tool_call_id,
            "output": json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str)
            else output,
        }

    def append_assistant_message(self, messages: list, raw_message: any):
        # Response API expects each output item to be appended directly
        for item in raw_message:
            msg_dict = (
                item.model_dump()
                if hasattr(item, "model_dump")
                else dict(item)
            )
            messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        result = []
        for t in pydantic_tools:
            if isinstance(t, dict) and t.get("type") == "namespace":
                # Flatten the namespace by extracting and converting its inner tools
                for inner_t in t.get("tools", []):
                    result.append(_make_response_tool(inner_t))
            else:
                result.append(_make_response_tool(t))

        # Responses API supports a native web_search tool, let's append it by default
        result.append({"type": "web_search"})

        return result

    def get_summary(self, conversation_text: str, reason: str) -> str:
        summary_request = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        res = self.client.responses.create(model=self.model, input=summary_request)
        for item in res.output:
            if item.type == "message":
                return next(
                    (c.text for c in item.content if c.type == "output_text"), ""
                )
        return ""

    def get_summary_stream(self, conversation_text: str, reason: str) -> Generator[str, None, None]:
        summary_request = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        with self.client.responses.stream(model=self.model, input=summary_request) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta


class ChatAPIClient(BaseLLMClient):
    """Implementation for the standard OpenAI Chat Completions API standard."""

    def generate(self, messages: list, tools: list = None):
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        return self.client.chat.completions.create(**kwargs)

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


class AsyncResponseAPIClient(ResponseAPIClient, AsyncBaseLLMClient):
    async def generate(self, messages: list, tools: list = None):
        return await self.client.responses.create(
            model=self.model, input=messages, tools=tools or []
        )

    async def get_summary(self, conversation_text: str, reason: str) -> str:
        summary_request = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(reason)},
        ]
        res = await self.client.responses.create(
            model=self.model, input=summary_request
        )
        for item in res.output:
            if item.type == "message":
                return next(
                    (c.text for c in item.content if c.type == "output_text"), ""
                )
        return ""


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


from init import API_STANDARD
from system.models import get_current_model_config


def _create_llm_client():
    """根据当前模型配置动态创建 LLM 客户端"""
    current_model = get_current_model_config()
    if current_model is None:
        return None
    client = OpenAI(
        base_url=current_model.base_url,
        api_key=current_model.api_key,
        max_retries=2,
    )
    if API_STANDARD == "chat":
        return ChatAPIClient(client, current_model.model_id)
    return ResponseAPIClient(client, current_model.model_id)


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
