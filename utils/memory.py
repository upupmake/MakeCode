import asyncio
import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid
import warnings
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import jieba
import jieba.analyse
from pydantic import Field
from rich.markdown import Markdown
from rich.markup import escape

from init import log_error_traceback
from settings import MEMORY_AGENT_MAX_ITERATIONS, MEMORY_RECALL_WINDOW_SIZE
from system.console_render import (
    _render_agent_response_message,
)
from system.console_render import (
    console as _compact_console,
)
from system.models import get_current_model_config
from system.stream_render import StreamRenderer
from system.tui_app import TuiRegion, post_tui
from utils import paths, text_tokens
from utils.llm_client import (
    close_async_llm_client,
    create_memory_management_llm_client,
    strip_native_message_payloads,
)
from utils.memory_catalog import MemoryStore, sort_memory_records
from utils.text_tokens import truncate_text_by_tokens
from utils.tool_validation import (
    ToolArgumentsModel,
    ToolArgumentValidationError,
    build_tool_definitions,
    validate_builtin_tool_arguments,
)
from utils.vision import strip_images_in_place, text_only_messages

jieba.setLogLevel(logging.ERROR)


MEMORY_AGENT_IDENTITY = "🧠 记忆代理"
ORCHESTRATOR_AGENT_ID = "Orchestrator"


def print_formatted_text(value):
    post_tui(TuiRegion.STATUS, str(value))

DEFAULT_CONTEXT_LENGTH = 350  # 单位: k (千 tokens)
DEFAULT_MEMORY_SIZE = 45
DEFAULT_MEMORY_RECALL_WINDOW_SIZE = MEMORY_RECALL_WINDOW_SIZE
DEFAULT_MEMORY_PRE_RECALL = False
DEFAULT_TOOL_OUTPUT_COMPACT_THRESHOLD = 70
DEFAULT_PARTIAL_COMPACT_THRESHOLD = 90
DEFAULT_TOOL_OUTPUT_COMPACT_TOKENS = 1500
DEFAULT_PARTIAL_COMPACT_MIN_PERCENT = 60
DEFAULT_PARTIAL_COMPACT_MAX_PERCENT = 99
TOOL_OUTPUT_COMPACT_MARKER = "\n\n...[该工具执行结果已被压缩 {omitted_tokens} tokens]...\n\n"
_TOOL_OUTPUT_COMPACT_MARKER_PATTERN = re.compile(
    r"(?:\n\n)?\.\.\.\[该工具执行结果已被压缩 \d+ tokens\]\.\.\.(?:\n\n)?"
)
_MEMORY_RECALL_WINDOWS: dict[str, list[list[str]]] = {}
_MEMORY_CONFIG_CACHE_LOCK = threading.RLock()
_MEMORY_CONFIG_CACHE_PATH: str | None = None
_MEMORY_CONFIG_CACHE_SIGNATURE: tuple[int, int, int] | None = None
_MEMORY_CONFIG_CACHE_DATA: dict | None = None
_MEMORY_STORE: MemoryStore | None = None
_MEMORY_DOCUMENT_FREQUENCY_CACHE: dict | None = None


def _reset_memory_config_cache() -> None:
    global _MEMORY_CONFIG_CACHE_PATH, _MEMORY_CONFIG_CACHE_SIGNATURE, _MEMORY_CONFIG_CACHE_DATA
    with _MEMORY_CONFIG_CACHE_LOCK:
        _MEMORY_CONFIG_CACHE_PATH = None
        _MEMORY_CONFIG_CACHE_SIGNATURE = None
        _MEMORY_CONFIG_CACHE_DATA = None


def _memory_config_file_signature() -> tuple[int, int, int] | None:
    try:
        stat = MEMORY_CONFIG_FILE.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


MAKECODE_DIR: Path
MEMORY_DIR: Path
MEMORY_DB_FILE: Path


def refresh_workspace_paths() -> None:
    global MAKECODE_DIR, MEMORY_DIR, MEMORY_DB_FILE
    MAKECODE_DIR = paths.workspace_makecode_dir()
    MEMORY_DIR = paths.workspace_memory_dir()
    MEMORY_DB_FILE = paths.workspace_memory_db_file()
    _reopen_memory_store(MEMORY_DB_FILE)
    reset_memory_recall_windows()


def _reopen_memory_store(db_path) -> None:
    """切换工作区时调用：关闭旧连接、按新 db 路径重新打开，并清除 DF 缓存。

    之前的实现只替换一个 Path 变量，不会清理内存里缓存的 FTS 索引，/cd 切换
    工作区后仍可能读到旧工作区的召回结果；这里连接替换和缓存清除是同一步。
    """
    global _MEMORY_STORE, _MEMORY_DOCUMENT_FREQUENCY_CACHE
    if _MEMORY_STORE is None:
        _MEMORY_STORE = MemoryStore(db_path)
    else:
        _MEMORY_STORE.reopen(db_path)
    _MEMORY_DOCUMENT_FREQUENCY_CACHE = None


def reset_memory_recall_windows() -> None:
    global _MEMORY_RECALL_WINDOWS
    _MEMORY_RECALL_WINDOWS = {}


MEMORY_CONFIG_FILE = paths.memory_config_file()
refresh_workspace_paths()


class AppendLongTermMemory(ToolArgumentsModel):
    """Append one new durable memory for a distinct future trigger not covered by active memories; do not use for examples of existing rules."""

    category: str = Field(
        ...,
        description=(
            "Memory category, e.g. 'preference', 'project-convention', "
            "'workflow', 'pitfall', 'environment', or 'release-process'."
        ),
    )
    insight: str = Field(
        ...,
        description="A durable actionable rule, preference, convention, or stable fact that is useful across future sessions.",
    )
    evidence: str = Field(
        ...,
        description="Brief source context explaining why this memory is justified; do not include long transcript excerpts.",
    )
    reuse_condition: str = Field(
        ...,
        description=(
            "A concrete future trigger for applying this memory, specific enough to decide when to recall it. "
            "Must contain an explicit trigger phrase such as 'when ...' / 'When ...' (or the Chinese equivalent '当...时'), "
            "naming the concrete task type, command, file, module, or workflow that should trigger recall."
        ),
    )


class DeleteLongTermMemory(ToolArgumentsModel):
    """Delete one or more active durable memories by ID only when each should no longer be recalled because it is obsolete, wrong, duplicated, or superseded."""

    memory_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Active memory IDs to delete after deciding each should no longer be recalled.",
    )


class UpdateLongTermMemory(ToolArgumentsModel):
    """Update an active durable memory by ID when preserving the same memory is better than appending a near-duplicate."""

    memory_id: str = Field(..., description="The active memory ID to update when the same trigger or behavior should be corrected, narrowed, expanded, or clarified.")
    category: str = Field(..., description="Updated memory category.")
    insight: str = Field(..., description="Updated durable actionable rule, preference, convention, or stable fact.")
    evidence: str = Field(..., description="Updated brief source context explaining why this memory is justified.")
    reuse_condition: str = Field(
        ...,
        description=(
            "Updated concrete future trigger for applying this memory. "
            "Must contain an explicit trigger phrase such as 'when ...' / 'When ...' (or the Chinese equivalent '当...时'), "
            "naming the concrete task type, command, file, module, or workflow that should trigger recall."
        ),
    )


class RecallLongTermMemory(ToolArgumentsModel):
    """Recall long-term memories relevant to a query."""

    query: str = Field(
        ...,
        description=(
            "The current task, user request, or sub-question to recall relevant long-term memories for. "
            "Recall matches on concrete keywords, identifiers, and terminology, not general semantic similarity. "
            "Include specific terms likely to appear in a matching memory (symbol/variable/file names, exact "
            "commands, error strings, or precise technical nouns). Avoid vague or purely descriptive phrasing."
        ),
    )


class ManageLongTermMemory(ToolArgumentsModel):
    """Add, update, or delete long-term memories when the current conversation establishes durable information for future sessions."""

    prompt: str = Field(
        ...,
        description=(
            "The memory changes needed based on the durable information established in the current conversation. "
            "Exclude temporary task progress, one-off details, and facts directly readable from the repository."
        ),
    )


LONG_TERM_MEMORY_TOOLS, LONG_TERM_MEMORY_TOOL_MODELS = build_tool_definitions(
    AppendLongTermMemory,
    DeleteLongTermMemory,
    UpdateLongTermMemory,
)
MEMORY_RECALL_TOOLS, MEMORY_RECALL_TOOL_MODELS = build_tool_definitions(
    RecallLongTermMemory,
)
MEMORY_SELF_MANAGEMENT_TOOLS, MEMORY_SELF_MANAGEMENT_TOOL_MODELS = build_tool_definitions(
    ManageLongTermMemory,
)



def _new_memory_record(category: str, insight: str, evidence: str, reuse_condition: str) -> dict:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    memory_id = f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    return {
        "id": memory_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "category": category.strip(),
        "insight": insight.strip(),
        "evidence": evidence.strip(),
        "reuse_condition": reuse_condition.strip(),
        "status": "active",
    }


def _memory_sort_key(record: dict) -> str:
    return record.get("updated_at") or record.get("created_at") or ""


def append_long_term_memory(
        category: str,
        insight: str,
        evidence: str,
        reuse_condition: str,
        **kwargs,
) -> dict:
    store = _get_memory_store()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_records = store.list_active_memories()
    overflow_count = len(active_records) + 1 - get_memory_size()
    deleted_ids = []
    if overflow_count > 0:
        overflow_ids = [
            record.get("id", "")
            for record in sorted(active_records, key=_memory_sort_key)[:overflow_count]
        ]
        deleted_ids = store.soft_delete_memories(overflow_ids, updated_at=now)
        _invalidate_document_frequency_cache()

    record = _new_memory_record(category, insight, evidence, reuse_condition)
    store.insert_memory(record, fts_text=_memory_fts_text(record))
    _invalidate_document_frequency_cache()
    return {**record, "deleted_overflow_ids": deleted_ids}


def delete_long_term_memories(memory_ids: list[str]) -> dict:
    store = _get_memory_store()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deleted_ids = store.soft_delete_memories(memory_ids, updated_at=now)
    if deleted_ids:
        _invalidate_document_frequency_cache()
    missing_ids = [memory_id for memory_id in memory_ids if memory_id not in deleted_ids]
    return {"deleted_ids": deleted_ids, "missing_ids": missing_ids}


def update_long_term_memory(
        memory_id: str,
        category: str,
        insight: str,
        evidence: str,
        reuse_condition: str,
        **kwargs,
) -> dict:
    store = _get_memory_store()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_record = {
        "updated_at": now,
        "category": category.strip(),
        "insight": insight.strip(),
        "evidence": evidence.strip(),
        "reuse_condition": reuse_condition.strip(),
    }
    ok = store.update_memory_fields(
        memory_id,
        updated_record,
        fts_text=_memory_fts_text(updated_record),
    )
    if not ok:
        return {"error": f"active memory not found: {memory_id}"}
    _invalidate_document_frequency_cache()
    return {"id": memory_id, "status": "active", **updated_record}


LONG_TERM_MEMORY_TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "AppendLongTermMemory": append_long_term_memory,
    "DeleteLongTermMemory": lambda memory_ids, **kwargs: delete_long_term_memories(memory_ids),
    "UpdateLongTermMemory": update_long_term_memory,
}


def _validate_memory_size(size) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("memory size must be a positive integer")
    return size


def _validate_memory_recall_window_size(size) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("memory recall window size must be a positive integer")
    return size


def _validate_memory_pre_recall(enabled) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("memory pre-recall must be a boolean")
    return enabled


def _validate_context_length(length) -> int:
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise ValueError("context length must be a positive integer")
    return length


def _validate_compaction_thresholds(tool_output_threshold, partial_threshold) -> tuple[int, int]:
    if (
        isinstance(tool_output_threshold, bool)
        or not isinstance(tool_output_threshold, int)
        or isinstance(partial_threshold, bool)
        or not isinstance(partial_threshold, int)
    ):
        raise ValueError("compaction thresholds must be integers")
    if not 0 < tool_output_threshold < partial_threshold < 100:
        raise ValueError(
            "compaction thresholds must satisfy 0 < tool output threshold < partial threshold < 100"
        )
    return tool_output_threshold, partial_threshold


def _validate_tool_output_compact_tokens(tokens) -> int:
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0 or tokens % 2:
        raise ValueError("tool output compact tokens must be a positive even integer")
    return tokens


def _validate_partial_compact_percentages(min_percent, max_percent) -> tuple[int, int]:
    if (
        isinstance(min_percent, bool)
        or not isinstance(min_percent, int)
        or isinstance(max_percent, bool)
        or not isinstance(max_percent, int)
    ):
        raise ValueError("partial compaction percentages must be integers")
    if not 0 < min_percent < max_percent < 100:
        raise ValueError(
            "partial compaction percentages must satisfy 0 < minimum < maximum < 100"
        )
    return min_percent, max_percent


def _load_memory_config_from_disk() -> dict:
    global _MEMORY_CONFIG_CACHE_PATH, _MEMORY_CONFIG_CACHE_SIGNATURE, _MEMORY_CONFIG_CACHE_DATA
    cache_path = str(MEMORY_CONFIG_FILE)
    signature = _memory_config_file_signature()
    with _MEMORY_CONFIG_CACHE_LOCK:
        if (
            cache_path == _MEMORY_CONFIG_CACHE_PATH
            and signature == _MEMORY_CONFIG_CACHE_SIGNATURE
            and _MEMORY_CONFIG_CACHE_DATA is not None
        ):
            return _MEMORY_CONFIG_CACHE_DATA.copy()

        data = {}
        if signature is not None:
            try:
                with open(MEMORY_CONFIG_FILE, encoding="utf-8") as f:
                    loaded = json.load(f)
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                data = loaded

        _MEMORY_CONFIG_CACHE_PATH = cache_path
        _MEMORY_CONFIG_CACHE_SIGNATURE = signature
        _MEMORY_CONFIG_CACHE_DATA = data
        return data.copy()


def _write_memory_config_fields(values: dict) -> None:
    global _MEMORY_CONFIG_CACHE_PATH, _MEMORY_CONFIG_CACHE_SIGNATURE, _MEMORY_CONFIG_CACHE_DATA
    with _MEMORY_CONFIG_CACHE_LOCK:
        data = _load_memory_config_from_disk()
        data.update(values)
        MEMORY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=MEMORY_CONFIG_FILE.parent,
                delete=False,
            ) as f:
                temporary_path = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, MEMORY_CONFIG_FILE)
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
        _MEMORY_CONFIG_CACHE_PATH = str(MEMORY_CONFIG_FILE)
        _MEMORY_CONFIG_CACHE_SIGNATURE = _memory_config_file_signature()
        _MEMORY_CONFIG_CACHE_DATA = data.copy()


def _write_memory_config_field(field: str, value) -> None:
    _write_memory_config_fields({field: value})


def _get_memory_config_field(field: str, default, validator):
    data = _load_memory_config_from_disk()
    if field not in data:
        _write_memory_config_field(field, default)
        return default
    try:
        return validator(data[field])
    except ValueError:
        return default


def get_memory_size() -> int:
    return _get_memory_config_field(
        "memory_size",
        DEFAULT_MEMORY_SIZE,
        _validate_memory_size,
    )


def set_memory_size(size: int) -> int:
    size = _validate_memory_size(size)
    _write_memory_config_field("memory_size", size)
    return size


def get_compaction_thresholds() -> tuple[int, int]:
    data = _load_memory_config_from_disk()
    tool_output_threshold = data.get(
        "tool_output_compact_threshold",
        DEFAULT_TOOL_OUTPUT_COMPACT_THRESHOLD,
    )
    partial_threshold = data.get(
        "partial_compact_threshold",
        DEFAULT_PARTIAL_COMPACT_THRESHOLD,
    )
    try:
        thresholds = _validate_compaction_thresholds(
            tool_output_threshold,
            partial_threshold,
        )
    except ValueError:
        return (
            DEFAULT_TOOL_OUTPUT_COMPACT_THRESHOLD,
            DEFAULT_PARTIAL_COMPACT_THRESHOLD,
        )

    missing_values = {}
    if "tool_output_compact_threshold" not in data:
        missing_values["tool_output_compact_threshold"] = tool_output_threshold
    if "partial_compact_threshold" not in data:
        missing_values["partial_compact_threshold"] = partial_threshold
    if missing_values:
        _write_memory_config_fields(missing_values)
    return thresholds


def set_compaction_thresholds(
        tool_output_threshold: int,
        partial_threshold: int,
) -> tuple[int, int]:
    thresholds = _validate_compaction_thresholds(
        tool_output_threshold,
        partial_threshold,
    )
    _write_memory_config_fields({
        "tool_output_compact_threshold": thresholds[0],
        "partial_compact_threshold": thresholds[1],
    })
    return thresholds


def get_tool_output_compact_tokens() -> int:
    return _get_memory_config_field(
        "tool_output_compact_tokens",
        DEFAULT_TOOL_OUTPUT_COMPACT_TOKENS,
        _validate_tool_output_compact_tokens,
    )


def set_tool_output_compact_tokens(tokens: int) -> int:
    tokens = _validate_tool_output_compact_tokens(tokens)
    _write_memory_config_field("tool_output_compact_tokens", tokens)
    return tokens


def get_partial_compact_percentages() -> tuple[int, int]:
    data = _load_memory_config_from_disk()
    min_percent = data.get("partial_compact_min_percent", DEFAULT_PARTIAL_COMPACT_MIN_PERCENT)
    max_percent = data.get("partial_compact_max_percent", DEFAULT_PARTIAL_COMPACT_MAX_PERCENT)
    try:
        percentages = _validate_partial_compact_percentages(min_percent, max_percent)
    except ValueError:
        return DEFAULT_PARTIAL_COMPACT_MIN_PERCENT, DEFAULT_PARTIAL_COMPACT_MAX_PERCENT

    missing_values = {}
    if "partial_compact_min_percent" not in data:
        missing_values["partial_compact_min_percent"] = min_percent
    if "partial_compact_max_percent" not in data:
        missing_values["partial_compact_max_percent"] = max_percent
    if missing_values:
        _write_memory_config_fields(missing_values)
    return percentages


def set_partial_compact_percentages(
        min_percent: int,
        max_percent: int,
) -> tuple[int, int]:
    percentages = _validate_partial_compact_percentages(min_percent, max_percent)
    _write_memory_config_fields({
        "partial_compact_min_percent": percentages[0],
        "partial_compact_max_percent": percentages[1],
    })
    return percentages


def get_memory_recall_window_size() -> int:
    return _get_memory_config_field(
        "memory_recall_window_size",
        DEFAULT_MEMORY_RECALL_WINDOW_SIZE,
        _validate_memory_recall_window_size,
    )


def set_memory_recall_window_size(size: int) -> int:
    size = _validate_memory_recall_window_size(size)
    _write_memory_config_field("memory_recall_window_size", size)
    return size


def get_memory_pre_recall() -> bool:
    return _get_memory_config_field(
        "memory_pre_recall",
        DEFAULT_MEMORY_PRE_RECALL,
        _validate_memory_pre_recall,
    )


def set_memory_pre_recall(enabled: bool) -> bool:
    enabled = _validate_memory_pre_recall(enabled)
    _write_memory_config_field("memory_pre_recall", enabled)
    return enabled


def get_context_length() -> int:
    return _get_memory_config_field(
        "context_length",
        DEFAULT_CONTEXT_LENGTH,
        _validate_context_length,
    )


def set_context_length(length: int) -> int:
    length = _validate_context_length(length)
    _write_memory_config_field("context_length", length)
    return length


def get_context_token_limit() -> int:
    return get_context_length() * 1024


def get_active_memory_count() -> int:
    return len(list_long_term_memories())


def _get_memory_store() -> MemoryStore:
    global _MEMORY_STORE
    if _MEMORY_STORE is None:
        _MEMORY_STORE = MemoryStore(MEMORY_DB_FILE)
    return _MEMORY_STORE


def _memory_fts_text(record: dict) -> dict[str, str]:
    return {
        "insight": _segment_text_for_index(str(record.get("insight", "") or "")),
        "reuse_condition": _segment_text_for_index(str(record.get("reuse_condition", "") or "")),
    }


def _invalidate_document_frequency_cache() -> None:
    global _MEMORY_DOCUMENT_FREQUENCY_CACHE
    _MEMORY_DOCUMENT_FREQUENCY_CACHE = None


def _touch_recalled_memories(memory_ids: list[str]) -> None:
    selected_ids = normalize_memory_ids(memory_ids)
    if not selected_ids:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store = _get_memory_store()
    for memory_id in selected_ids:
        store.touch_memory(memory_id, now)


def list_long_term_memories() -> list[dict]:
    return _get_memory_store().list_active_memories()


def _sorted_active_memory_records() -> list[dict]:
    return sort_memory_records(list_long_term_memories())


def _render_memory_record_markdown(record: dict) -> str:
    lines = [
        f"## {record.get('id', '')}",
        f"- Category: {record.get('category', '')}",
        f"- Updated at: {record.get('updated_at', '')}",
        f"- Insight: {record.get('insight', '')}",
        f"- Reuse condition: {record.get('reuse_condition', '')}",
    ]
    return "\n".join(lines)


def render_long_term_memory_markdown() -> str:
    records = _sorted_active_memory_records()
    if not records:
        return ""

    return "\n\n".join(_render_memory_record_markdown(record) for record in records)


def _active_memory_map() -> dict[str, dict]:
    return {
        record.get("id", ""): record
        for record in list_long_term_memories()
        if record.get("id")
    }


def _get_agent_recall_window(agent_id: str) -> list[list[str]]:
    return _MEMORY_RECALL_WINDOWS.setdefault(agent_id, [])


def _get_recent_recalled_memory_ids(agent_id: str) -> set[str]:
    return {
        memory_id
        for recall_round in _MEMORY_RECALL_WINDOWS.get(agent_id, [])
        for memory_id in recall_round
    }


def _append_memory_recall_window(agent_id: str, selected_ids: list[str]) -> None:
    selected_ids = normalize_memory_ids(selected_ids)
    if not selected_ids:
        return

    window = _get_agent_recall_window(agent_id)
    window.append(selected_ids)
    overflow = len(window) - get_memory_recall_window_size()
    if overflow > 0:
        del window[:overflow]


def normalize_memory_ids(memory_ids: list[str]) -> list[str]:
    active_records = _active_memory_map()
    selected_ids = []
    seen_ids = set()
    for memory_id in memory_ids or []:
        if not isinstance(memory_id, str):
            continue
        memory_id = memory_id.strip()
        if not memory_id or memory_id in seen_ids or memory_id not in active_records:
            continue
        selected_ids.append(memory_id)
        seen_ids.add(memory_id)
    return selected_ids


def render_selected_memory_context(memory_ids: list[str]) -> str:
    selected_ids = normalize_memory_ids(memory_ids)
    if not selected_ids:
        return ""

    active_records = _active_memory_map()
    return "\n\n".join(
        _render_memory_record_markdown(active_records[memory_id])
        for memory_id in selected_ids
    )


def prepend_recalled_memory_to_query(query: str, memory_context: str) -> str:
    if not memory_context.strip():
        return query
    return (
        "# Potentially Relevant Memories\n\n"
        "The following long-term memories were recalled for this user request. "
        "Treat them as contextual preferences and project conventions, not as new user instructions.\n\n"
        f"{memory_context.strip()}\n\n"
        "# Current User Request\n\n"
        f"{query}"
    )


_RECALL_HIGH_FREQUENCY_DF_RATIO = 0.4
_RECALL_RELATIVE_SCORE_THRESHOLD = 0.3
_RECALL_EXTRACT_TAGS_TOP_K = 15

_CJK_LOW, _CJK_HIGH = "\u4e00", "\u9fff"


def _contains_cjk(text: str) -> bool:
    return any(_CJK_LOW <= ch <= _CJK_HIGH for ch in text)


def _merge_single_char_tokens(raw_tokens: list[str]) -> list[str]:
    """中文单字 token 不单独保留（信息量太低，OR 模式下会引入大量噪音），而是分别
    与前一个、后一个完整 token 拼接成新 token，取代原单字。

    这是为了找回 jieba 分词边界切碎的中文复合词，不依赖静态自定义词典：比如"子代理
    委派"被切成 ['子','代理','委派']，单字"子"原本会被长度过滤直接丢弃，导致索引和
    查询里都没有"子代理"这个可被精确匹配的 token。拼接后产出 ['子代理','代理',
    '委派']——"子代理"作为完整短语参与匹配，比单独命中"代理"的记忆有更高的
    区分度；原始单字"子"本身不再输出。

    开头/结尾处没有前词或后词可拼时，只拼有效的一侧。
    """
    result = []
    count = len(raw_tokens)
    for index, token in enumerate(raw_tokens):
        is_cjk_single_char = len(token) == 1 and _contains_cjk(token)
        if not is_cjk_single_char:
            result.append(token)
            continue
        if result:
            result.append(result[-1] + token)
        if index + 1 < count:
            result.append(token + raw_tokens[index + 1])
    return result


def _tokenize_for_index(text: str) -> list[str]:
    """jieba 搜索引擎模式分词，中文单字 token 不单独保留——先通过
    `_merge_single_char_tokens` 与前后完整 token 拼接找回复合词，再过滤剩余长度 < 2
    的 token（英文天然 >= 2 不受影响）。

    纯 ASCII 片段（标识符/命令，可能含下划线、连字符）在分词前先按空白切分、整体保留，
    不交给 jieba 做字符级切分：jieba 对 `foo_bar_baz` 这类词会切成 foo/bar/baz 碎片，
    与查询侧 `_query_terms` 对纯 ASCII 关键词整体保留的处理不一致，导致索引里查不到
    对应短语。只有含中文的片段才用 jieba 分词。
    """
    tokens = []
    for fragment in re.split(r"(\s+)", text or ""):
        if not fragment.strip():
            continue
        if _contains_cjk(fragment):
            raw_tokens = [t for t in jieba.lcut_for_search(fragment) if any(ch.isalnum() for ch in t)]
            tokens.extend(_merge_single_char_tokens(raw_tokens))
        else:
            tokens.append(fragment)
    return [token for token in tokens if any(ch.isalnum() for ch in token) and len(token) >= 2]


def _segment_text_for_index(text: str) -> str:
    return " ".join(_tokenize_for_index(text))


def _query_terms(keyword: str) -> list[str]:
    """单个关键词 -> 查询词列表。

    纯 ASCII 关键词（变量名/标识符/命令）整体保留，交给 FTS5 按短语处理，保留内部
    下划线/连字符结构的相邻性；含中文的关键词用 jieba 分词并过滤长度 < 2 的 token。
    """
    if not _contains_cjk(keyword):
        stripped = keyword.strip()
        return [stripped] if stripped and any(ch.isalnum() for ch in stripped) else []
    return _tokenize_for_index(keyword)


def _extract_query_terms_from_text(text: str, top_k: int = _RECALL_EXTRACT_TAGS_TOP_K) -> list[str]:
    """长文本关键词提取：用 jieba.analyse.extract_tags（TF-IDF）取 top_k 个关键词，
    不做全量分词。供子代理预召回场景使用，避免长 context_prompt 全量分词后候选池失控。
    """
    tags = jieba.analyse.extract_tags(text or "", topK=top_k)
    terms = []
    for tag in tags:
        if not _contains_cjk(tag):
            if any(ch.isalnum() for ch in tag):
                terms.append(tag)
        elif len(tag) >= 2:
            terms.append(tag)
    return terms


def _strip_sub_agent_task_wrapper(query: str) -> str:
    """子代理预召回 query 是 build_sub_agent_recall_query 拼接的 "# Sub-Agent Delegated
    Task / ## Task ID / ## Role / ## Context Prompt" 模板。关键词提取前先剥离这层包装，
    只对实际 context_prompt 内容做提取，因为模板包装词（Task/Role/Agent 等）不携带
    任何业务语义信息，混在里面只会捉捉挖挖去占 TF-IDF topK 的名额。
    """
    marker = "## Context Prompt"
    if marker in query:
        return query.split(marker, 1)[1]
    return query


def _tokenize_query(query: str, use_keyword_extraction: bool) -> list[str]:
    if use_keyword_extraction:
        return _extract_query_terms_from_text(_strip_sub_agent_task_wrapper(query))
    seen = set()
    terms = []
    for keyword in re.split(r"\s+", query.strip()):
        if keyword:
            for term in _query_terms(keyword):
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
    return terms


def _get_document_frequency() -> dict:
    """DF 统计直接扫磁盘 `memories` 表的 active 行，按内容缓存，写操作
    （append/update/delete）后通过 `_invalidate_document_frequency_cache`
    主动失效，不随每次召回重新扫表。
    """
    global _MEMORY_DOCUMENT_FREQUENCY_CACHE
    if _MEMORY_DOCUMENT_FREQUENCY_CACHE is None:
        _MEMORY_DOCUMENT_FREQUENCY_CACHE = _get_memory_store().get_document_frequency(_tokenize_for_index)
    return _MEMORY_DOCUMENT_FREQUENCY_CACHE


def _build_match_query(terms: list, active_record_count: int) -> str:
    if not terms:
        return ""
    document_frequency = _get_document_frequency()
    high_frequency_cutoff = active_record_count * _RECALL_HIGH_FREQUENCY_DF_RATIO
    rare_terms = [t for t in terms if document_frequency.get(t, 0) <= high_frequency_cutoff]
    effective_terms = rare_terms if rare_terms else terms
    quoted = ['"{}"'.format(t.replace('"', '""')) for t in effective_terms]
    return " OR ".join(quoted)


def _filter_by_relative_threshold(
        scored_ids: list,
        relative_threshold: float = _RECALL_RELATIVE_SCORE_THRESHOLD,
) -> list:
    """固定相对阈值：保留 score >= 最高分 * relative_threshold 的记忆 id，不设返回数量上限。

    不叠加固定最低分数兜底：BM25 分数的绝对尺度受记忆库总规模影响（IDF 项 log(N/n)
    随候选总数 N 浮动），记忆库较小（如新工作区刚开始积累记忆）时同一条真正相关的
    记忆算出的分数会系统性偏低，固定绝对阈值在那种场景下会把它直接过滤掉。相对阈值
    本身是对同一次查询内部的 top1 归一化，不受记忆库规模影响，足以单独承担筛选职责。
    """
    if not scored_ids:
        return []
    top_score = scored_ids[0][1]
    cutoff = top_score * relative_threshold if top_score > 0 else 0
    return [memory_id for memory_id, score in scored_ids if score >= cutoff]


def select_relevant_memory_ids(
        query: str,
        agent_id: str = ORCHESTRATOR_AGENT_ID,
        use_keyword_extraction: bool = False,
) -> list[str]:
    """基于 SQLite FTS5 + BM25 的本地关键词检索，纯本地计算、不发起任何模型调用。"""
    query = query.strip()
    if not query:
        return []

    store = _get_memory_store()
    active_record_count = len(store.list_active_memories())
    if active_record_count == 0:
        return []

    terms = _tokenize_query(query, use_keyword_extraction)
    if not terms:
        return []

    match_query = _build_match_query(terms, active_record_count)
    scored_ids = store.search_fts(match_query)
    # 相对阈值的 top1 基准必须用排除前的全量结果计算，再对通过阈值的结果执行纯 id 剔除。
    # 若在计算阈值前就剔除召回窗口里最近已召回的 id，而那正巧是全局 top1，
    # 则 top1 会被替换成更低分的候选，阈值基准跟着下滑，让本来远远不足格的弱关联记忆
    # 被误放行；连续多轮召回同一个 agent 的同类 query 时该问题会持续放大（已模拟验证）。
    selected_ids = _filter_by_relative_threshold(scored_ids)
    excluded_ids = _get_recent_recalled_memory_ids(agent_id)
    if excluded_ids:
        selected_ids = [memory_id for memory_id in selected_ids if memory_id not in excluded_ids]
    _append_memory_recall_window(agent_id, selected_ids)
    return selected_ids


async def recall_long_term_memories(
        query: str,
        source: str = "RecallLongTermMemory",
        agent_id: str = ORCHESTRATOR_AGENT_ID,
        previous_assistant_content: str = "",
        use_keyword_extraction: bool = False,
) -> dict:
    query = query.strip()
    post_tui(TuiRegion.BACKGROUND, active=True)
    post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🧠 {escape(source)} 开始召回长期记忆。[/#aaaaaa]")
    post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🤖 召回智能体：{escape(agent_id)}[/#aaaaaa]")
    if query:
        post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🔎 召回查询：{escape(query)}[/#aaaaaa]")
    try:
        try:
            selected_ids = await asyncio.to_thread(
                select_relevant_memory_ids,
                query,
                agent_id=agent_id,
                use_keyword_extraction=use_keyword_extraction,
            )
        except Exception as exc:
            log_error_traceback("memory recall failed", exc)
            post_tui(TuiRegion.BACKGROUND, f"[bold red]🧠 记忆召回失败：{escape(str(exc))}[/bold red]")
            return {"ids": [], "content": "", "error": str(exc)}

        memory_context = render_selected_memory_context(selected_ids)
        _touch_recalled_memories(selected_ids)
        if selected_ids:
            recalled_ids = escape(", ".join(selected_ids))
            post_tui(
                TuiRegion.BACKGROUND,
                f"[bold green]🧠 记忆召回命中 {len(selected_ids)} 条：\n{recalled_ids}\n[/bold green]",
            )
            post_tui(TuiRegion.BACKGROUND, Markdown(memory_context))
        else:
            post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🧠 记忆召回未命中相关长期记忆。[/#aaaaaa]")
        post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🧠 记忆召回流程已结束。[/#aaaaaa]")
        return {"ids": selected_ids, "content": memory_context}
    except Exception as exc:
        log_error_traceback("memory recall failed", exc)
        post_tui(TuiRegion.BACKGROUND, f"[bold red]🧠 记忆召回失败：{escape(str(exc))}[/bold red]")
        return {"ids": [], "content": "", "error": str(exc)}
    finally:
        post_tui(TuiRegion.BACKGROUND, active=False)


MEMORY_RECALL_TOOLS_HANDLERS = {
    "RecallLongTermMemory": lambda query, **kwargs: recall_long_term_memories(
        query,
        source="Agent 主动召回",
        agent_id=ORCHESTRATOR_AGENT_ID,
    ),
}


async def memory_agent_loop(
        conversation_text: str,
        summary: str,
        reason: str,
        current_memory_content: str,
        tools: list,
        mode: str = "compact",
        max_iterations: int = MEMORY_AGENT_MAX_ITERATIONS,
        raise_on_error: bool = False,
) -> list[dict]:
    llm_client = create_memory_management_llm_client()
    if llm_client is None:
        raise RuntimeError("No model configured. Please use /models to configure a model first.")
    post_tui(TuiRegion.BACKGROUND, active=True)
    saved_outputs: list[dict] = []
    had_error = False
    try:
        post_tui(TuiRegion.BACKGROUND, "\n[bold yellow]🧠 正在管理长期记忆...[/bold yellow]")
        post_tui(TuiRegion.BACKGROUND, "[bold yellow]📓 记忆[/bold yellow]")
        messages = llm_client.get_memory_decision_messages(
            conversation_text,
            summary,
            reason,
            current_memory_content,
            mode=mode,
        )

        for round_index in range(max_iterations):
            try:
                text_content, memory_tool_calls, raw_message = await StreamRenderer().render_text_stream_async(
                    llm_client.generate_stream(
                        messages,
                        llm_client.format_tools(tools),
                    ),
                    region=TuiRegion.BACKGROUND,
                    render_live=False,
                    set_active=True,
                )
            except Exception as e:
                post_tui(TuiRegion.BACKGROUND, f"[bold red]记忆管理器错误：{e}[/bold red]")
                post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]记忆管理流程已结束。[/#aaaaaa]")
                if raise_on_error:
                    raise RuntimeError(f"Memory management failed: {e}") from e
                return saved_outputs

            if raw_message is not None:
                messages.append(raw_message)
            stop_reason = raw_message.get("stop_reason") if isinstance(raw_message, dict) else None

            _render_agent_response_message(
                text_content,
                identity=MEMORY_AGENT_IDENTITY,
                tui_region=TuiRegion.BACKGROUND,
            )

            if not memory_tool_calls:
                if stop_reason == "pause_turn":
                    continue
                break

            memory_changed = False
            for tool_call in memory_tool_calls:
                tool_name = tool_call.get("name")
                tool_id = tool_call.get("id")
                tool_args = tool_call.get("arguments")
                handler = LONG_TERM_MEMORY_TOOL_HANDLERS.get(tool_name)
                tool_error = False
                output = ""
                if not handler:
                    tool_error = True
                    output = f"未知记忆工具：{tool_name}"
                    post_tui(TuiRegion.BACKGROUND, f"[bold red]🧠 未知记忆工具：{escape(str(tool_name))}[/bold red]")
                else:
                    tool_changed = False
                    post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🧠 准备执行记忆写入工具：{escape(tool_name)}[/#aaaaaa]")
                    try:
                        arguments = validate_builtin_tool_arguments(
                            tool_name,
                            tool_args,
                            LONG_TERM_MEMORY_TOOL_MODELS[tool_name],
                        )
                        output = await asyncio.to_thread(handler, **arguments)
                        if isinstance(output, dict) and "error" in output:
                            tool_error = True
                        elif tool_name == "AppendLongTermMemory" and isinstance(output, dict):
                            tool_changed = True
                        elif tool_name == "DeleteLongTermMemory" and isinstance(output, dict):
                            tool_changed = bool(output.get("deleted_ids"))
                            tool_error = bool(output.get("missing_ids")) and not tool_changed
                        elif tool_name == "UpdateLongTermMemory" and isinstance(output, dict):
                            tool_changed = True
                        memory_changed = memory_changed or tool_changed
                    except ToolArgumentValidationError as exc:
                        tool_error = True
                        output = str(exc)
                    except Exception as e:
                        tool_error = True
                        output = f"执行 {tool_name} 出错：{e}。"
                    if tool_changed:
                        post_tui(TuiRegion.BACKGROUND, f"[bold green]🧠 记忆工具执行完成并产生变更：{escape(tool_name)}[/bold green]")
                    else:
                        post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🧠 记忆工具执行完成：{escape(tool_name)}[/#aaaaaa]")
                had_error = had_error or tool_error
                saved_outputs.append({"tool": tool_name, "output": output})
                if tool_id:
                    tool_result = llm_client.format_tool_result(tool_id, tool_name, output)
                    if tool_error:
                        tool_result["is_error"] = True
                    messages.append(tool_result)

            current_round = round_index + 1
            remaining_rounds = max_iterations - current_round
            if remaining_rounds > 0:
                latest_memory_content = render_long_term_memory_markdown() if memory_changed else ""
                latest_memory_section = ""
                if memory_changed:
                    latest_memory_section = (
                        "Latest long-term memory state after successful tool calls:\n\n"
                        f"{latest_memory_content or '(empty)'}\n\n"
                    )
                messages.append({
                    "role": "user",
                    "content": (
                        f"[auto generated] current_round={current_round} / max_round={max_iterations}. "
                        f"Remaining rounds: {remaining_rounds}.\n\n"
                        f"{latest_memory_section}"
                        "The memory management loop will exit automatically when the max round is reached, "
                        "regardless of whether all memory operations are complete. "
                        "Please finish memory management as soon as possible."
                    ),
                })

        if not saved_outputs:
            post_tui(TuiRegion.BACKGROUND, "[yellow]长期记忆没有变更。[/yellow]")
        post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]记忆管理流程已结束。[/#aaaaaa]")
        if raise_on_error and had_error:
            raise RuntimeError("Memory management completed with tool errors.")
        return saved_outputs
    finally:
        post_tui(TuiRegion.BACKGROUND, active=False)
        await close_async_llm_client(llm_client)


async def manual_memory_update(prompt: str, history: list | None = None) -> list[dict]:
    prompt = prompt.strip()
    return await memory_agent_loop(
        conversation_text=format_clean_conversation_context(history or []),
        summary="",
        reason=prompt,
        current_memory_content=render_long_term_memory_markdown(),
        tools=LONG_TERM_MEMORY_TOOLS,
        mode="active",
    )


_ENCODER = text_tokens._ENCODER
if _ENCODER is None:
    print_formatted_text(
        "\n[yellow]⚠️ tiktoken加载失败, token将使用估算模式 [/yellow]\n"
    )


def estimate_text_tokens(text: str) -> int:
    return text_tokens.estimate_text_tokens(text, encoder=_ENCODER)


def _token_value_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {_token_value_text(item)}"
            for key, item in value.items()
        )
    if isinstance(value, list):
        return "\n".join(_token_value_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _token_reasoning_text(message: dict, message_format: str | None = None) -> str:
    if message_format in {"anthropic", "openai_responses"}:
        metadata = message.get("message_metadata")
        # 这些协议只回放本协议产出的 reasoning/thinking，外来 reasoning 不会被发送。
        if not isinstance(metadata, dict) or metadata.get("source_format") != message_format:
            return ""
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    return "".join(
        block.get("text", "")
        for block in message.get("content_blocks") or []
        if isinstance(block, dict) and block.get("type") == "reasoning"
    )


def _token_tool_call_text(name: str, arguments: object) -> str:
    return "\n".join((
        f"name: {name}",
        f"arguments: {_token_value_text(arguments)}",
    ))


def _build_token_estimation_sections(
    messages: list,
    tools_definition: list | None = None,
    message_format: str | None = None,
) -> dict[str, str]:
    if message_format is None:
        # 未显式指定时按当前模型协议估算，保证与实际请求内容一致。
        current_model = get_current_model_config()
        message_format = current_model.message_format if current_model is not None else None
    projected_messages = text_only_messages(
        strip_native_message_payloads(messages)
    )
    sections: dict[str, list[str]] = {
        "system": [],
        "user": [],
        "reasoning": [],
        "assistant": [],
        "tool": [],
    }
    for message in projected_messages:
        role = message.get("role")
        if role == "system":
            text = _compaction_message_text(message)
            if text:
                sections["system"].append(f"<system>{text}</system>")
            continue
        if role == "user":
            text = _compaction_message_text(message)
            if text:
                sections["user"].append(f"<user>{text}</user>")
            continue
        if role == "assistant":
            reasoning = _token_reasoning_text(message, message_format)
            if reasoning:
                sections["reasoning"].append(f"<reasoning>{reasoning}</reasoning>")
            text = _compaction_message_text(message)
            if text:
                sections["assistant"].append(f"<assistant>{text}</assistant>")
            for _, name, arguments in _compaction_tool_calls(message):
                sections["tool"].append(
                    f"<tool>{_token_tool_call_text(name, arguments)}</tool>"
                )
            continue
        if role in {"tool", "function"} or message.get("type") == "function_call_output":
            name = str(message.get("name") or "")
            output = message.get("output", message.get("content", ""))
            text = _token_value_text(output)
            tool_content = "\n".join(
                part for part in (f"name: {name}" if name else "", text) if part
            )
            if tool_content:
                sections["tool"].append(f"<tool>{tool_content}</tool>")
            continue
        if message.get("type") == "function_call":
            for _, name, arguments in _compaction_tool_calls(message):
                sections["tool"].append(
                    f"<tool>{_token_tool_call_text(name, arguments)}</tool>"
                )
            continue
        if isinstance(role, str):
            text = _compaction_message_text(message)
            if text:
                sections["assistant"].append(f"<{role}>{text}</{role}>")

    for tool in tools_definition or []:
        sections["system"].append(f"<system>{_token_value_text(tool)}</system>")
    return {key: "\n\n".join(value) for key, value in sections.items()}


def estimate_token_breakdown(
    messages: list,
    tools_definition: list | None = None,
    message_format: str | None = None,
) -> dict[str, int]:
    return {
        key: estimate_text_tokens(value)
        for key, value in _build_token_estimation_sections(
            messages, tools_definition, message_format
        ).items()
    }


def estimate_tokens(messages: list, tools_definition: list | None = None, message_format: str | None = None):
    return sum(
        estimate_token_breakdown(messages, tools_definition, message_format).values()
    )


def _conversation_groups(messages: list[dict]) -> list[tuple[int, int, bool]]:
    groups: list[tuple[int, int, bool]] = []
    start: int | None = None
    has_assistant = False

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            continue
        if role == "user":
            if start is None:
                start = index
                has_assistant = False
            elif has_assistant:
                groups.append((start, index, True))
                start = index
                has_assistant = False
            continue
        if start is not None and (
            role == "assistant" or message.get("type") == "function_call"
        ):
            has_assistant = True

    if start is not None:
        groups.append((start, len(messages), has_assistant))
    return groups


def _protected_history_start(messages: list[dict]) -> int:
    complete_groups = [group for group in _conversation_groups(messages) if group[2]]
    if not complete_groups:
        return 0
    return complete_groups[-1][0]


def _tool_call_source_messages(messages: list[dict]) -> dict[str, dict]:
    sources = {}
    for message in messages:
        if message.get("type") == "function_call":
            call_id = message.get("call_id")
            if call_id:
                sources[str(call_id)] = message
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = tool_call.get("id") or tool_call.get("call_id")
            if call_id:
                sources[str(call_id)] = message
    return sources


def _compact_tool_output_text(text: str) -> str:
    if _TOOL_OUTPUT_COMPACT_MARKER_PATTERN.search(text):
        return text
    compact_tokens = get_tool_output_compact_tokens()
    return truncate_text_by_tokens(
        text,
        max_tokens=compact_tokens,
        edge_tokens=compact_tokens // 2,
        marker=TOOL_OUTPUT_COMPACT_MARKER,
        existing_marker_pattern=_TOOL_OUTPUT_COMPACT_MARKER_PATTERN,
        encoder=_ENCODER,
    )


def compact_tool_outputs(messages: list[dict]) -> bool:
    candidate = copy.deepcopy(messages)
    protected_start = _protected_history_start(candidate)
    if protected_start <= 0:
        return False

    sources = _tool_call_source_messages(candidate)
    changed = False
    for message in candidate[:protected_start]:
        if message.get("type") == "function_call_output":
            output_field = "output" if "output" in message else "content"
        elif message.get("role") == "tool":
            output_field = "content"
        else:
            continue

        output = message.get(output_field)
        if not isinstance(output, str):
            continue
        compacted = _compact_tool_output_text(output)
        if compacted == output:
            continue

        message[output_field] = compacted
        call_id = message.get("call_id") or message.get("tool_call_id")
        source_message = sources.get(str(call_id)) if call_id else None
        if source_message is not None:
            metadata = source_message.get("message_metadata")
            if isinstance(metadata, dict):
                metadata.pop("native_blocks", None)
        changed = True

    if changed:
        messages[:] = candidate
    return changed


def _select_partial_compaction_range(
        messages: list[dict],
        context_token_limit: int,
        current_context_tokens: int,
) -> tuple[int, int] | None:
    complete_groups = [group for group in _conversation_groups(messages) if group[2]]
    candidates = complete_groups[:-1]
    if not candidates:
        return None

    first_start = candidates[0][0]
    min_percent, max_percent = get_partial_compact_percentages()
    over_context_limit = current_context_tokens > context_token_limit
    compaction_base_tokens = current_context_tokens if over_context_limit else context_token_limit
    for _, end, _ in candidates:
        accumulated_tokens = estimate_tokens(messages[first_start:end])
        if accumulated_tokens * 100 < compaction_base_tokens * min_percent:
            continue
        if not over_context_limit and accumulated_tokens * 100 > context_token_limit * max_percent:
            return None
        return first_start, end
    return None


def _compaction_message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block
            if isinstance(block, str)
            else block.get("text", "")
            for block in content
            if isinstance(block, str)
            or (isinstance(block, dict) and block.get("type") == "text")
        )

    content_blocks = message.get("content_blocks")
    if isinstance(content_blocks, list):
        return "".join(
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if content is None:
        return ""
    return str(content)


def _compaction_tool_calls(message: dict) -> list[tuple[str, str, object]]:
    if message.get("type") == "function_call":
        return [(
            str(message.get("call_id") or ""),
            str(message.get("name") or ""),
            message.get("arguments", ""),
        )]

    tool_calls = message.get("tool_calls")
    if not tool_calls:
        tool_calls = [
            block
            for block in message.get("content_blocks") or []
            if isinstance(block, dict) and block.get("type") == "tool_call"
        ]

    calls = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        function = function if isinstance(function, dict) else {}
        arguments = tool_call.get("arguments")
        if arguments is None:
            arguments = function.get("arguments", tool_call.get("input", ""))
        calls.append((
            str(tool_call.get("id") or tool_call.get("call_id") or ""),
            str(tool_call.get("name") or function.get("name") or ""),
            arguments,
        ))
    return calls


def _compaction_tool_results(messages: list[dict]) -> dict[str, dict]:
    results = {}
    for message in messages:
        if (
            message.get("role") not in {"tool", "function"}
            and message.get("type") != "function_call_output"
        ):
            continue
        call_id = message.get("tool_call_id") or message.get("call_id")
        if call_id:
            results[str(call_id)] = message
    return results


def _compaction_value_text(value: object) -> str:
    if isinstance(value, str):
        text = value
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return text
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _compaction_tool_entry(
        name: str,
        arguments: object,
        result: dict | None,
) -> str:
    output = "" if result is None else result.get("output", result.get("content", ""))
    output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    lines = [
        f"name: {name}",
        f"arguments: {_compaction_value_text(arguments)}",
        "output:",
    ]
    if result is not None and result.get("is_error") is True:
        lines.append("[tool error]")
    if output:
        lines.append(output)
    return "\n".join(lines)


def format_clean_conversation_context(messages: list[dict]) -> str:
    clean_messages = text_only_messages(strip_native_message_payloads(messages))
    results = _compaction_tool_results(clean_messages)
    consumed_result_ids = set()
    sections = []

    for message in clean_messages:
        if message.get("role") == "system":
            continue

        role = message.get("role")
        if role == "user":
            text = _compaction_message_text(message)
            if text:
                sections.append(f"### user:\n{text}")
            continue

        if role == "assistant":
            text = _compaction_message_text(message)
            if text:
                sections.append(f"### assistant:\n{text}")
            calls = _compaction_tool_calls(message)
            if calls:
                entries = []
                for call_id, name, arguments in calls:
                    result = results.get(call_id) if call_id else None
                    if call_id:
                        consumed_result_ids.add(call_id)
                    entries.append(_compaction_tool_entry(name, arguments, result))
                sections.append("### tools:\n" + "\n\n".join(entries))
            continue

        if message.get("type") == "function_call":
            entries = [
                _compaction_tool_entry(name, arguments, results.get(call_id) if call_id else None)
                for call_id, name, arguments in _compaction_tool_calls(message)
            ]
            sections.append("### tools:\n" + "\n\n".join(entries))
            consumed_result_ids.update(
                call_id for call_id, _, _ in _compaction_tool_calls(message) if call_id
            )
            continue

        if role in {"tool", "function"} or message.get("type") == "function_call_output":
            call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
            if call_id in consumed_result_ids:
                continue
            sections.append(
                "### tools:\n"
                + _compaction_tool_entry(
                    str(message.get("name") or ""),
                    "(unavailable)",
                    message,
                )
            )

    return "\n\n".join(sections)


async def _summarize_messages(
        messages: list[dict],
        reason: str,
        *,
        require_memory_success: bool = False,
        memory_messages: list[dict] | None = None,
) -> str:
    conversation_text = format_clean_conversation_context(messages)
    memory_conversation_text = (
        format_clean_conversation_context(memory_messages)
        if memory_messages is not None
        else conversation_text
    )

    _compact_console.print(
        f"\n[bold yellow]⚡️ 正在压缩上下文...[/bold yellow]  "
        f"[#aaaaaa]{reason}[/#aaaaaa]"
    )
    _compact_console.rule("[bold cyan]📝 摘要", style="cyan")

    chunks: list[str] = []
    summary_client = create_memory_management_llm_client()
    if summary_client is None:
        raise RuntimeError("No model configured. Please use /models to configure a model first.")
    try:
        try:
            renderer = StreamRenderer(console=_compact_console, update_interval=0.1)
            summary, _, _ = await renderer.render_text_stream_async(
                summary_client.get_summary_stream_events(conversation_text, reason),
                set_active=True,
            )
            if summary:
                chunks.append(summary)
        except Exception as e:
            _compact_console.print(f"\n[bold red]流式摘要错误：{e}[/bold red]")
            fallback = await summary_client.get_summary(conversation_text, reason)
            _compact_console.print(Markdown(fallback))
            chunks = [fallback]
    finally:
        await close_async_llm_client(summary_client)

    summary = "".join(chunks)
    if not summary.strip():
        raise RuntimeError("Compaction summary was empty.")
    _compact_console.print("[#aaaaaa]摘要生成流程已结束。[/#aaaaaa]")

    await memory_agent_loop(
        conversation_text=memory_conversation_text,
        summary=summary,
        reason="Automatic memory extraction during context compaction. Extract durable, cross-session memories from the conversation if any valuable ones are found; skip if none.",
        current_memory_content=render_long_term_memory_markdown(),
        tools=LONG_TERM_MEMORY_TOOLS,
        raise_on_error=require_memory_success,
    )
    return summary


def _summary_messages(summary: str, reason: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": f"[Previous conversation compressed. Reason: {reason}] \n\n{summary}",
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from the summary. Ready to proceed.",
        },
    ]


async def partial_compact(
        messages: list[dict],
        context_token_limit: int,
        current_context_tokens: int,
        reason: str,
) -> bool:
    selected_range = _select_partial_compaction_range(
        messages,
        context_token_limit,
        current_context_tokens,
    )
    if selected_range is None:
        return False

    start, end = selected_range
    selected_messages = copy.deepcopy(messages[start:end])
    summary = await _summarize_messages(
        selected_messages,
        reason,
        require_memory_success=True,
        memory_messages=messages,
    )
    candidate = copy.deepcopy(messages)
    candidate[start:end] = _summary_messages(summary, reason)
    strip_images_in_place(candidate)
    messages[:] = candidate
    return True


async def auto_compact(
        messages: list,
        reason: str = "User triggered compact",
        system_prompt_fn=None,
) -> str:
    source_messages = copy.deepcopy(messages)
    summary = await _summarize_messages(
        source_messages,
        reason,
    )

    system_msgs = [message for message in source_messages if message.get("role") == "system"]
    if system_prompt_fn and system_msgs:
        system_msgs = [{"role": "system", "content": system_prompt_fn()}]

    messages[:] = system_msgs + _summary_messages(summary, reason)
    strip_images_in_place(messages)
    return "History successfully compacted and summarized."
