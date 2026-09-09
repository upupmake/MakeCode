"""
额外工具配置 — 与模型目录解耦，只保存启用状态和可选模型身份。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from system.models import ModelConfig, ModelKey, get_model_manager
from utils import paths


UNDERSTAND_IMAGE_TOOL_ID = "UnderstandImage"


def extra_tools_config_file() -> Path:
    return paths.install_makecode_dir() / "extra_tools.json"


def _empty_config() -> dict[str, Any]:
    return {
        UNDERSTAND_IMAGE_TOOL_ID: {
            "enabled": False,
            "model": None,
        }
    }


def _normalize_tool_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"enabled": False, "model": None}
    model = raw.get("model")
    if not isinstance(model, dict):
        model = None
    return {
        "enabled": raw.get("enabled") is True,
        "model": model,
    }


def _load_raw() -> dict[str, Any]:
    config_file = extra_tools_config_file()
    if not config_file.exists():
        migrated = _migrate_from_model_config()
        if migrated is not None:
            return migrated
        return _empty_config()
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_config()
    if not isinstance(data, dict):
        return _empty_config()
    return {
        UNDERSTAND_IMAGE_TOOL_ID: _normalize_tool_config(data.get(UNDERSTAND_IMAGE_TOOL_ID)),
    }


def _legacy_image_understanding_from_model_config() -> dict[str, Any] | None:
    config_file = paths.install_makecode_dir() / "model_config.json"
    if not config_file.exists():
        return None
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    image_understanding = data.get("image_understanding")
    if isinstance(image_understanding, dict):
        return _normalize_tool_config(image_understanding)
    model = data.get("image_understanding_model")
    if isinstance(model, dict):
        return {"enabled": False, "model": model}
    return None


def _strip_legacy_image_understanding() -> None:
    config_file = paths.install_makecode_dir() / "model_config.json"
    if not config_file.exists():
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    if "image_understanding" not in data and "image_understanding_model" not in data:
        return
    data.pop("image_understanding", None)
    data.pop("image_understanding_model", None)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_file.parent,
            prefix=f".{config_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, config_file)
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _migrate_from_model_config() -> dict[str, Any] | None:
    legacy = _legacy_image_understanding_from_model_config()
    if legacy is None:
        return None
    payload = {UNDERSTAND_IMAGE_TOOL_ID: legacy}
    if not _save_raw(payload):
        return payload
    _strip_legacy_image_understanding()
    return payload


def _save_raw(payload: dict[str, Any]) -> bool:
    config_dir = paths.install_makecode_dir()
    config_file = extra_tools_config_file()
    config_dir.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_dir,
            prefix=f".{config_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(payload, temp_file, ensure_ascii=False, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, config_file)
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return False
    return True


def is_understand_image_enabled() -> bool:
    return _load_raw()[UNDERSTAND_IMAGE_TOOL_ID]["enabled"] is True


def get_understand_image_model_key() -> Optional[ModelKey]:
    return ModelConfig.key_from_dict(_load_raw()[UNDERSTAND_IMAGE_TOOL_ID].get("model"))


def get_understand_image_model() -> Optional[ModelConfig]:
    manager = get_model_manager()
    if manager is None:
        return None
    return manager.get_model_by_key(get_understand_image_model_key())


def get_understand_image_model_display_text() -> str:
    model = get_understand_image_model()
    return model.get_display_text() if model else "同主模型"


def set_understand_image_enabled(enabled: bool) -> bool:
    payload = _load_raw()
    payload[UNDERSTAND_IMAGE_TOOL_ID] = {
        **payload[UNDERSTAND_IMAGE_TOOL_ID],
        "enabled": bool(enabled),
    }
    return _save_raw(payload)


def set_understand_image_config(enabled: bool, key: Optional[ModelKey] = None) -> bool:
    manager = get_model_manager()
    model = None
    if key is not None:
        if manager is None:
            return False
        model = manager.get_model_by_key(key)
        if model is None:
            return False
    payload = _load_raw()
    payload[UNDERSTAND_IMAGE_TOOL_ID] = {
        "enabled": bool(enabled),
        "model": model.to_identity_dict() if model else None,
    }
    return _save_raw(payload)
