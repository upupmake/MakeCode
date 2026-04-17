import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

from init import WORKDIR
from utils.llm_client import llm_client

THRESHOLD = 1024 * 128
MAKECODE_DIR = WORKDIR / ".makecode"
TRANSCRIPT_DIR = MAKECODE_DIR / "transcripts"
CHECKPOINT_DIR = MAKECODE_DIR / "checkpoint"
KEEP_RECENT_TOOL_CALL = 64


def save_checkpoint(messages: list, filepath: Path = None) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    if filepath is None:
        uid = uuid.uuid4().hex[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ckpt_{timestamp}_{uid}.json"
        filepath = CHECKPOINT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    return filepath


def list_checkpoints() -> list:
    if not CHECKPOINT_DIR.exists():
        return []
    files = list(CHECKPOINT_DIR.glob("ckpt_*.json"))
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files


def load_checkpoint(filepath: Path) -> list:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


try:
    import tiktoken
    import os
    import sys

    # Determine base path for the bundled executable or normal execution
    if getattr(sys, "frozen", False):
        _base_path = Path(sys._MEIPASS)
    else:
        _base_path = Path(__file__).parent.parent

    # Use local cache if it exists (for offline/packaged environments)
    _local_cache = _base_path / "tiktoken_cache"
    if _local_cache.exists():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(_local_cache)

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except ImportError:
    print_formatted_text(
        HTML(f"\n<ansiyellow> ⚠️ tiktoken加载失败, token将使用估算模式 </ansiyellow>\n")
    )
    _ENCODER = None


def estimate_tokens(
        messages: list, tools_definition: list = None, system_prompt: str = None
):
    # 计算基础文本的 token 数
    text = json.dumps(messages, ensure_ascii=False)
    if _ENCODER:
        base_tokens = len(_ENCODER.encode(text, disallowed_special=()))
    else:
        base_tokens = len(text) // 2

    # 加上系统提示词的 token 数
    if system_prompt:
        prompt_text = json.dumps(system_prompt, ensure_ascii=False)
        if _ENCODER:
            base_tokens += len(_ENCODER.encode(prompt_text, disallowed_special=()))
        else:
            base_tokens += len(prompt_text) // 2
    else:
        # 默认系统提示词开销
        base_tokens += 3000

    # 加上工具定义的 token 数
    if tools_definition:
        tools_text = json.dumps(tools_definition, ensure_ascii=False)
        if _ENCODER:
            base_tokens += len(_ENCODER.encode(tools_text, disallowed_special=()))
        else:
            base_tokens += len(tools_text) // 2

    return base_tokens


def micro_compact(input_list: list) -> list:
    tool_results = []
    for msg in input_list:
        if msg.get("type") == "function_call_output" or msg.get("role") == "tool":
            tool_results.append(msg)

    if len(tool_results) <= KEEP_RECENT_TOOL_CALL:
        return input_list

    tool_call_info_map = {}
    for msg in input_list:
        if msg.get("type") == "function_call":
            tool_call_info_map[msg.get("call_id")] = {
                "name": msg.get("name"),
                "arguments": msg.get("arguments"),
            }
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = (
                    tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                )
                tc_func = (
                    tc.get("function", {})
                    if isinstance(tc, dict)
                    else getattr(tc, "function", None)
                )
                if tc_func:
                    tc_name = (
                        tc_func.get("name")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "name", None)
                    )
                    tc_args = (
                        tc_func.get("arguments")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "arguments", None)
                    )
                    if tc_id:
                        tool_call_info_map[tc_id] = {
                            "name": tc_name,
                            "arguments": tc_args,
                        }

    to_clear = tool_results[:-KEEP_RECENT_TOOL_CALL]
    for result in to_clear:
        call_id = result.get("call_id") or result.get("tool_call_id")
        info = tool_call_info_map.get(call_id, {})
        tool_name = info.get("name", "unknown tool")
        tool_arguments = info.get("arguments", {})

        replacement = (
            f"[Previous {tool_name} result cleared, arguments were: {tool_arguments}]"
        )
        if "output" in result:
            result["output"] = replacement
        elif "content" in result:
            result["content"] = replacement

    return input_list


def auto_compact(messages: list, reason: str = "User triggered compact") -> str:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print_formatted_text(
        HTML(f"<ansiyellow>[Transcript saved to: {transcript_path}]</ansiyellow>")
    )

    # Ask LLM to summarize via Responses API
    print_formatted_text(
        HTML(
            f"<ansiyellow>[Compacting conversation context... reason: {reason}]</ansiyellow>"
        )
    )

    # Filter out original system messages to prevent system instructions clash
    filtered_messages = [m for m in messages if m.get("role") != "system"]

    # Dump the filtered conversation history into a single string
    conversation_text = json.dumps(filtered_messages, default=str, ensure_ascii=False)

    summary = llm_client.get_summary(conversation_text, reason)

    system_msgs = [m for m in messages if m.get("role") == "system"]

    summary_msgs = [
        {
            "role": "user",
            "content": f"[Previous conversation compressed. Reason: {reason}] \n\n{summary}",
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from the summary. Ready to proceed.",
        },
    ]

    # Rebuild history in-place
    new_history = system_msgs + summary_msgs
    messages.clear()
    messages.extend(new_history)

    return "History successfully compacted and summarized."
