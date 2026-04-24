import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.markdown import Markdown
from rich.live import Live
from init import WORKDIR
from utils.common import sanitize_title
from utils.llm_client import llm_client

_compact_console = Console()
THRESHOLD = 1024 * 144
MAKECODE_DIR = WORKDIR / ".makecode"
TRANSCRIPT_DIR = MAKECODE_DIR / "transcripts"
CHECKPOINT_DIR = MAKECODE_DIR / "checkpoint"
KEEP_RECENT_TOOL_CALL = 64


def save_checkpoint(messages: list, filepath: Path = None, title: str = None) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    if filepath is None:
        uid = uuid.uuid4().hex[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if title:
            safe_title = sanitize_title(title)
            if safe_title:
                filename = f"ckpt_{safe_title}_{timestamp}_{uid}.json"
            else:
                filename = f"ckpt_{timestamp}_{uid}.json"
        else:
            filename = f"ckpt_{timestamp}_{uid}.json"
        filepath = CHECKPOINT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    return filepath


def get_checkpoint_title(filepath: Path) -> str:
    """Extract title from checkpoint filename if available."""
    stem = filepath.stem
    if not stem.startswith("ckpt_"):
        return None
        
    parts = stem.split("_")
    # Check if it has title format: ckpt_title_YYYYMMDD_HHMMSS_uid
    # Timestamp format is YYYYMMDD_HHMMSS which is 8_6 chars
    
    # Try to find timestamp
    for i, part in enumerate(parts):
        if len(part) == 8 and part.isdigit():  # YYYYMMDD
            # Check next part for time
            if i + 1 < len(parts) and len(parts[i+1]) == 6 and parts[i+1].isdigit():
                # Found timestamp at index i
                if i > 1:  # There is a title (ckpt_title_...)
                    title_parts = parts[1:i]
                    return " ".join(title_parts).replace("_", " ")
                return None
    return None


# --- Checkpoint rename --- #


def rename_checkpoint_with_title(filepath: Path, title: str) -> Path:
    """Rename an existing checkpoint file to include *title* in its name.

    Because ``sanitize_title`` never allows ``_``, we can discover the
    timestamp anchor by splitting on ``_`` and finding the 8-digit date
    segment followed by a 6-digit time segment.
    Everything between ``ckpt`` and that date is the (possibly empty)
    old title portion.
    """
    safe_title = sanitize_title(title)
    if not safe_title:
        return filepath

    stem = filepath.stem
    if not stem.startswith("ckpt_"):
        return filepath

    parts = stem.split("_")
    # Find date segment: 8-digit, followed by 6-digit time
    try:
        date_idx = next(
            i for i, p in enumerate(parts)
            if len(p) == 8 and p.isdigit()
               and i + 1 < len(parts)
               and len(parts[i + 1]) == 6 and parts[i + 1].isdigit()
        )
    except StopIteration:
        return filepath

    ts = f"{parts[date_idx]}_{parts[date_idx + 1]}"
    uid = parts[-1]
    new_path = filepath.parent / f"ckpt_{safe_title}_{ts}_{uid}.json"

    if new_path == filepath:
        return filepath

    if filepath.exists():
        filepath.rename(new_path)
    return new_path


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
        HTML(f"\n<ansiyellow>⚠️ tiktoken加载失败, token将使用估算模式 </ansiyellow>\n")
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
        HTML(f"\n<ansiyellow>[Transcript saved to: {transcript_path}]</ansiyellow>")
    )

    # Filter out original system messages to prevent system instructions clash
    filtered_messages = [m for m in messages if m.get("role") != "system"]
    conversation_text = json.dumps(filtered_messages, default=str, ensure_ascii=False)

    _compact_console.print(
        f"\n[bold yellow]⚡️ Compacting context...[/bold yellow]  "
        f"[dim]{reason}[/dim]"
    )
    _compact_console.rule("[bold cyan]📝 Summary", style="cyan")

    chunks: list[str] = []
    try:
        # 使用 Live 创建一个可实时刷新的上下文
        # refresh_per_second 可以控制刷新帧率，太高耗费性能，一般 10-15 足够流畅
        with Live(Markdown(""), console=_compact_console, refresh_per_second=15) as live:
            for chunk in llm_client.get_summary_stream(conversation_text, reason):
                chunks.append(chunk)
                current_text = "".join(chunks)
                # 每次收到新内容，重新解析并更新 Live 视图
                live.update(Markdown(current_text))

    except Exception as e:
        # 打印红色的错误提示，比原生的 print 更友好
        _compact_console.print(f"\n[bold red]Stream Error: {e}[/bold red]")

        # 流式失败时回退到普通调用
        fallback = llm_client.get_summary(conversation_text, reason)
        # 回退时也同样使用 Markdown 渲染
        _compact_console.print(Markdown(fallback))
        chunks = [fallback]

    # 不需要再单独 _compact_console.print() 换行，因为 Live 结束后自带换行效果
    _compact_console.rule(style="cyan")
    summary = "".join(chunks)

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
