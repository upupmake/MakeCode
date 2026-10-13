import json

import pytest

from system import tui_mode
from utils import memory


@pytest.fixture(autouse=True)
def reset_tui_simple_mode():
    """simple 模式是进程级状态，必须逐测试复位。

    有测试会遍历执行所有已注册斜杠命令（含 /simple），不复位会污染后续用例。
    """
    tui_mode.set_simple_mode(False)
    yield
    tui_mode.set_simple_mode(False)


@pytest.fixture(autouse=True)
def isolate_memory_config(tmp_path, monkeypatch):
    config_file = tmp_path / "memory_config.json"
    config_file.write_text(
        json.dumps({
            "memory_size": memory.DEFAULT_MEMORY_SIZE,
            "memory_recall_window_size": memory.DEFAULT_MEMORY_RECALL_WINDOW_SIZE,
            "memory_pre_recall": memory.DEFAULT_MEMORY_PRE_RECALL,
            "context_length": memory.DEFAULT_CONTEXT_LENGTH,
            "tool_output_compact_threshold": memory.DEFAULT_TOOL_OUTPUT_COMPACT_THRESHOLD,
            "partial_compact_threshold": memory.DEFAULT_PARTIAL_COMPACT_THRESHOLD,
            "tool_output_compact_tokens": memory.DEFAULT_TOOL_OUTPUT_COMPACT_TOKENS,
            "partial_compact_min_percent": memory.DEFAULT_PARTIAL_COMPACT_MIN_PERCENT,
            "partial_compact_max_percent": memory.DEFAULT_PARTIAL_COMPACT_MAX_PERCENT,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "MEMORY_CONFIG_FILE", config_file)
    memory._reset_memory_config_cache()
    yield
    memory._reset_memory_config_cache()
