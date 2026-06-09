from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from utils import paths


class TuiRegion(StrEnum):
    CONTENT = "content"
    REASONING = "reasoning"
    TASK = "task"
    TOOLS = "tools"
    BACKGROUND = "background"
    SUB_AGENT = "sub_agent"
    STATUS = "status"
    RUNTIME_INFO = "runtime_info"


@dataclass(frozen=True)
class TuiEvent:
    region: TuiRegion
    payload: Any
    clear: bool = False
    tool_result_delta: int = 0
    reset_tool_result_count: bool = False
    tail: bool = False
    active: bool | None = None


LAYOUT_CONFIG_FILE = paths.layout_config_file()
LAYOUT_DEFAULT_RATIOS: dict[str, int] = {
    "content": 2,
    "tools": 1,
    "task": 2,
    "background": 3,
    "sub_agent": 1,
}
LAYOUT_LEFT_KEYS = ("content", "tools")
LAYOUT_RIGHT_KEYS = ("task", "background", "sub_agent")


def normalize_layout_ratios(data: Any) -> dict[str, int]:
    source = data if isinstance(data, dict) else {}
    ratios: dict[str, int] = {}
    for key, default in LAYOUT_DEFAULT_RATIOS.items():
        try:
            if key == "task" and key not in source and "reasoning" in source:
                value = int(source.get("reasoning", default))
            else:
                value = int(source.get(key, default))
        except (TypeError, ValueError):
            value = default
        ratios[key] = min(max(value, 0), 10)
    return ratios


def load_layout_ratios() -> dict[str, int]:
    try:
        data = json.loads(LAYOUT_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(LAYOUT_DEFAULT_RATIOS)
    return normalize_layout_ratios(data)


def save_layout_ratios(ratios: dict[str, int]) -> None:
    LAYOUT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT_CONFIG_FILE.write_text(
        json.dumps(normalize_layout_ratios(ratios), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
