import json
import time

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from init import WORKDIR, llm_client

THRESHOLD = 10240 * 12
MAKECODE_DIR = WORKDIR / ".makecode"
TRANSCRIPT_DIR = MAKECODE_DIR / "transcripts"
KEEP_RECENT = 24


def estimate_tokens(messages: list):
    return len(json.dumps(messages)) // 2


def micro_compact(input_list: list) -> list:
    tool_results = []
    for msg in input_list:
        if msg.get("type") == "function_call_output" or msg.get("role") == "tool":
            tool_results.append(msg)

    if len(tool_results) <= KEEP_RECENT:
        return input_list

    tool_call_info_map = {}
    for msg in input_list:
        if msg.get("type") == "function_call":
            tool_call_info_map[msg.get("call_id")] = {
                "name": msg.get("name"),
                "arguments": msg.get("arguments")
            }
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                tc_func = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                if tc_func:
                    tc_name = tc_func.get("name") if isinstance(tc_func, dict) else getattr(tc_func, "name", None)
                    tc_args = tc_func.get("arguments") if isinstance(tc_func, dict) else getattr(tc_func, "arguments", None)
                    if tc_id:
                        tool_call_info_map[tc_id] = {
                            "name": tc_name,
                            "arguments": tc_args
                        }

    to_clear = tool_results[:-KEEP_RECENT]
    for result in to_clear:
        call_id = result.get("call_id") or result.get("tool_call_id")
        info = tool_call_info_map.get(call_id, {})
        tool_name = info.get("name", "unknown tool")
        tool_arguments = info.get("arguments", {})
        
        replacement = f"[Previous {tool_name} result cleared, arguments were: {tool_arguments}]"
        if "output" in result:
            result["output"] = replacement
        elif "content" in result:
            result["content"] = replacement

    return input_list


class Compact(BaseModel):
    reason: str = Field(
        default="User triggered compact",
        description="Reason for compacting the conversation."
    )


def auto_compact(messages: list, reason: str = "User triggered compact") -> str:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print(f"\033[33m[Transcript saved to: {transcript_path}]\033[0m")

    # Ask LLM to summarize via Responses API
    print(f"\033[33m[Compacting conversation context... reason: {reason}]\033[0m")

    # Filter out original system messages to prevent system instructions clash
    filtered_messages = [m for m in messages if m.get("role") != "system"]

    # Dump the filtered conversation history into a single string
    conversation_text = json.dumps(filtered_messages, default=str, ensure_ascii=False)

    summary = llm_client.get_summary(conversation_text, reason)

    # Find the start of the current turn to ensure we don't orphan function calls
    last_user_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    system_msgs = [m for m in messages if m.get("role") == "system"]
    keep_msgs = messages[last_user_idx:]

    summary_msgs = [
        {"role": "user", "content": f"[Previous conversation compressed. Reason: {reason}] \n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."}
    ]

    # Rebuild history in-place
    new_history = system_msgs + summary_msgs + keep_msgs
    messages.clear()
    messages.extend(new_history)

    return "History successfully compacted and summarized."


MEMORY_TOOLS = [
    pydantic_function_tool(Compact),
]

MEMORY_TOOLS_HANDLERS = {
    "Compact": auto_compact,
}
