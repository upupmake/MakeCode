import json
from abc import ABC, abstractmethod

from openai import OpenAI
from init import log_error_traceback
from prompts import get_summary_system_prompt, get_summary_user_prompt


def _make_response_tool(tool_dict):
    """Flatten pydantic_function_tool output for Responses API"""
    if "function" in tool_dict:
        func = tool_dict["function"]
        return {
            "type": "function",
            "name": func.get("name"),
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {}),
            "strict": func.get("strict", False)
        }
    return tool_dict


class BaseLLMClient(ABC):
    def __init__(self, client: OpenAI, model: str):
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
    def format_tool_result(self, tool_call_id: str, tool_name: str, output: any) -> dict:
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


class ResponseAPIClient(BaseLLMClient):
    """Implementation for the custom/beta Responses API standard."""

    def generate(self, messages: list, tools: list = None):
        return self.client.responses.create(
            model=self.model,
            input=messages,
            tools=tools or []
        )

    def parse_response(self, response) -> tuple[str, list, any]:
        text_content = ""
        tool_calls = []
        for item in response.output:
            if item.type == "message":
                text_content += next((c.text for c in item.content if c.type == "output_text"), "")
            elif item.type == "function_call":
                tool_calls.append({
                    "id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                    "raw": item
                })
        # The raw message in this case is the list of outputs, but we append them differently
        return text_content, tool_calls, response.output

    def format_tool_result(self, tool_call_id: str, tool_name: str, output: any) -> dict:
        return {
            "type": "function_call_output",
            "call_id": tool_call_id,
            "output": json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
        }

    def append_assistant_message(self, messages: list, raw_message: any):
        # Response API expects each output item to be appended directly
        for item in raw_message:
            msg_dict = item.model_dump(exclude_none=True) if hasattr(item, 'model_dump') else dict(item)
            
            # Defend against LLM generating malformed JSON arguments
            if msg_dict.get("type") == "function_call" and "arguments" in msg_dict:
                args = msg_dict["arguments"]
                if isinstance(args, str):
                    try:
                        json.loads(args)
                    except json.JSONDecodeError as exc:
                        log_error_traceback("ResponseAPI malformed function arguments", exc)
                        msg_dict["arguments"] = "{}"
                        
            messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        result = []
        for t in pydantic_tools:
            if isinstance(t, dict) and t.get("type") == "namespace":
                new_t = t.copy()
                new_t["tools"] = [_make_response_tool(inner_t) for inner_t in t.get("tools", [])]
                result.append(new_t)
            else:
                result.append(_make_response_tool(t))

        # Responses API supports a native web_search tool, let's append it by default
        result.append({"type": "web_search"})

        return result

    def get_summary(self, conversation_text: str, reason: str) -> str:
        summary_request = [
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(conversation_text, reason)}
        ]
        res = self.client.responses.create(
            model=self.model,
            instructions=get_summary_system_prompt(),
            input=summary_request
        )
        for item in res.output:
            if item.type == "message":
                return next((c.text for c in item.content if c.type == "output_text"), "")
        return ""


class ChatAPIClient(BaseLLMClient):
    """Implementation for the standard OpenAI Chat Completions API standard."""

    def generate(self, messages: list, tools: list = None):
        kwargs = {
            "model": self.model,
            "messages": messages
        }
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
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "raw": tc
                })
        return text_content, tool_calls, message

    def format_tool_result(self, tool_call_id: str, tool_name: str, output: any) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
        }

    def append_assistant_message(self, messages: list, raw_message: any):
        # Standard Chat API requires the assistant message to be appended exactly as it is (including tool_calls)
        msg_dict = raw_message.model_dump(exclude_none=True) if hasattr(raw_message, 'model_dump') else dict(raw_message)
        
        # Defend against LLM generating malformed JSON in tool call arguments.
        # If we send malformed JSON back in the history, many backends (e.g. vLLM, Ollama) will crash with a 400 Bad Request ("Extra data").
        if msg_dict.get("tool_calls"):
            for tc in msg_dict["tool_calls"]:
                if tc.get("type") == "function" and "function" in tc:
                    args = tc["function"].get("arguments")
                    if isinstance(args, str):
                        try:
                            json.loads(args)
                        except json.JSONDecodeError as exc:
                            log_error_traceback("ChatAPI malformed function arguments", exc)
                            # Replace malformed arguments with empty JSON to prevent backend crash on next turn
                            tc["function"]["arguments"] = "{}"

        messages.append(msg_dict)

    def format_tools(self, pydantic_tools: list) -> list:
        # Standard format doesn't need flattening, but it doesn't support "namespace" tools
        # We must extract all functions into a flat list
        result = []
        for t in pydantic_tools:
            if isinstance(t, dict) and t.get("type") == "namespace":
                for inner_t in t.get("tools", []):
                    result.append(inner_t)
            else:
                result.append(t)
        return result

    def get_summary(self, conversation_text: str, reason: str) -> str:
        messages = [
            {"role": "system", "content": get_summary_system_prompt()},
            {"role": "user", "content": conversation_text},
            {"role": "user", "content": get_summary_user_prompt(conversation_text, reason)}
        ]
        res = self.client.chat.completions.create(
            model=self.model,
            messages=messages
        )
        return res.choices[0].message.content or ""
