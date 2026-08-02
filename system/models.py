"""
模型管理模块 - 负责管理 LLM 模型配置
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "medium"
MESSAGE_FORMATS = ("openai_chat", "anthropic")
DEFAULT_MESSAGE_FORMAT = "openai_chat"
ModelKey = tuple[str, str, str, str]


def normalize_reasoning_effort(value: object) -> str:
    return value if isinstance(value, str) and value in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def normalize_message_format(value: object) -> str:
    return value if isinstance(value, str) and value in MESSAGE_FORMATS else DEFAULT_MESSAGE_FORMAT


@dataclass
class ModelConfig:
    """模型配置"""
    base_url: str
    api_key: str
    model_id: str
    is_favorite: bool = False
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    message_format: str = DEFAULT_MESSAGE_FORMAT

    @property
    def key(self) -> ModelKey:
        return (self.base_url, self.api_key, self.model_id, self.message_format)

    @property
    def runtime_key(self) -> tuple[str, str, str, str, str]:
        return (*self.key, self.reasoning_effort)

    def get_display_name(self) -> str:
        """获取域名前缀用于显示"""
        try:
            parsed = urlparse(self.base_url if "://" in self.base_url else f"https://{self.base_url}")
            domain = parsed.netloc or self.base_url
            domain = re.sub(r':\d+', '', domain)
            return domain
        except Exception:
            return self.base_url

    def get_display_text(self) -> str:
        """获取在面板中显示的文本: model_id (域名)"""
        domain = self.get_display_name()
        return f"{self.model_id} ({domain})"

    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)

    def to_identity_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model_id": self.model_id,
            "message_format": self.message_format,
        }

    @classmethod
    def key_from_dict(cls, data: dict | None) -> Optional[ModelKey]:
        if not isinstance(data, dict):
            return None
        base_url = data.get("base_url")
        api_key = data.get("api_key")
        model_id = data.get("model_id")
        if not base_url or api_key is None or not model_id:
            return None
        return (base_url, api_key, model_id, normalize_message_format(data.get("message_format")))

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """从字典创建"""
        return cls(
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            model_id=data.get("model_id", ""),
            is_favorite=data.get("is_favorite", False),
            reasoning_effort=normalize_reasoning_effort(data.get("reasoning_effort")),
            message_format=normalize_message_format(data.get("message_format")),
        )


class ModelManager:
    """模型管理器"""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.config_file = config_dir / "model_config.json"
        self.models: list[ModelConfig] = []
        self.current_model: Optional[ModelConfig] = None
        self.current_model_key: Optional[ModelKey] = None
        self.last_selected_key: Optional[ModelKey] = None
        self.memory_recall_model_key: Optional[ModelKey] = None
        self.load_error: Optional[str] = None
        self._raw_config: Optional[dict] = None
        self._load_config()
        self._set_initial_current_model()

    def _sort_models(self):
        self.models.sort(key=lambda model: (not model.is_favorite, model.model_id.lower()))

    def _get_model_by_key(self, key: Optional[ModelKey]) -> Optional[ModelConfig]:
        if key is None:
            return None
        for model in self.models:
            if model.key == key:
                return model
        return None

    def _get_default_model(self) -> Optional[ModelConfig]:
        if not self.models:
            return None
        for model in self.models:
            if model.is_favorite:
                return model
        return self.models[0]

    def _set_current_model(self, model: Optional[ModelConfig]):
        self.current_model = model
        self.current_model_key = model.key if model else None

    def _set_initial_current_model(self):
        if self.current_model is not None:
            return
        last_selected_model = self._get_model_by_key(self.last_selected_key)
        if last_selected_model:
            self._set_current_model(last_selected_model)
            return
        self._set_current_model(self._get_default_model())

    def _load_config(self):
        """加载配置文件"""
        if not self.config_file.exists():
            self.models = []
            self.last_selected_key = None
            self.memory_recall_model_key = None
            self.load_error = None
            self._raw_config = None
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.load_error = None
            raw_config = data.copy() if isinstance(data, dict) else None
            last_selected_key = None
            memory_recall_model_key = None
            if isinstance(data, list):
                selected_item = next(
                    (item for item in data if isinstance(item, dict) and item.get("selected")),
                    None,
                )
                last_selected_key = ModelConfig.key_from_dict(selected_item) if selected_item else None
                models = [ModelConfig.from_dict(item) for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                models_data = data.get("models", [])
                if not isinstance(models_data, list):
                    raise ValueError("models 字段必须是列表")
                last_selected_key = ModelConfig.key_from_dict(data.get("last_selected", {}))
                memory_recall_model_key = ModelConfig.key_from_dict(data.get("memory_recall_model", {}))
                if last_selected_key is None:
                    selected_item = next(
                        (item for item in models_data if isinstance(item, dict) and item.get("selected")),
                        None,
                    )
                    last_selected_key = ModelConfig.key_from_dict(selected_item) if selected_item else None
                models = [
                    ModelConfig.from_dict(item)
                    for item in models_data
                    if isinstance(item, dict)
                ]
            else:
                raise ValueError("模型配置必须是对象或列表")

            self.models = models
            self.last_selected_key = last_selected_key
            self.memory_recall_model_key = memory_recall_model_key
            self._raw_config = raw_config
            self._sort_models()
        except Exception as exc:
            self.load_error = str(exc)

    def _ensure_config_loaded_for_save(self) -> bool:
        if self.load_error is None:
            return True
        if not self.config_file.exists():
            return True
        return False

    def _build_save_payload(self) -> dict:
        payload = self._raw_config.copy() if isinstance(self._raw_config, dict) else {}
        payload["version"] = payload.get("version", 2)
        payload["last_selected"] = self._get_last_selected_payload()
        payload["memory_recall_model"] = self._get_memory_recall_model_payload()
        payload["models"] = [model.to_dict() for model in self.models]
        return payload

    def get_load_error_display(self) -> str:
        return f"无法读取模型配置文件 {self.config_file}: {self.load_error}" if self.load_error else ""

    def _get_last_selected_payload(self) -> Optional[dict]:
        model = self._get_model_by_key(self.last_selected_key)
        if model is None:
            model = self._get_default_model()
            self.last_selected_key = model.key if model else None
        return model.to_identity_dict() if model else None

    def _get_memory_recall_model_payload(self) -> Optional[dict]:
        model = self.get_memory_recall_model()
        return model.to_identity_dict() if model else None

    def _save_config(self):
        """保存配置文件"""
        if not self._ensure_config_loaded_for_save():
            return False
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._sort_models()
        payload = self._build_save_payload()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.config_dir,
                prefix=f".{self.config_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(payload, temp_file, ensure_ascii=False, indent=4)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.config_file)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        self._raw_config = payload.copy()
        self.load_error = None
        return True

    def _reload_from_disk(self) -> bool:
        self._load_config()
        return self.load_error is None

    def is_configured(self) -> bool:
        return len(self.models) > 0

    def get_favorite_models(self) -> list[ModelConfig]:
        return [m for m in self.models if m.is_favorite]

    def get_current_model(self) -> Optional[ModelConfig]:
        return self.current_model

    def get_memory_recall_model(self) -> Optional[ModelConfig]:
        model = self._get_model_by_key(self.memory_recall_model_key)
        if model is None:
            self.memory_recall_model_key = None
        return model

    def get_memory_recall_model_display_text(self) -> str:
        model = self.get_memory_recall_model()
        return model.get_display_text() if model else "同主模型"

    def set_memory_recall_model_by_key(self, key: Optional[ModelKey]) -> bool:
        if not self._reload_from_disk():
            return False
        if key is not None and self._get_model_by_key(key) is None:
            return False
        self.memory_recall_model_key = key
        return self._save_config()

    def set_current_model_by_index(self, index: int) -> bool:
        if not (0 <= index < len(self.models)):
            return False
        return self.select_model(self.models[index].key, self.models[index].reasoning_effort)

    def select_model(self, key: ModelKey, reasoning_effort: str) -> bool:
        if not self._reload_from_disk():
            return False
        model = self._get_model_by_key(key)
        if model is None or reasoning_effort not in REASONING_EFFORTS:
            return False
        model.reasoning_effort = reasoning_effort
        self._set_current_model(model)
        self.last_selected_key = model.key
        return self._save_config()

    def set_reasoning_effort(self, key: ModelKey, reasoning_effort: str) -> bool:
        if not self._reload_from_disk():
            return False
        model = self._get_model_by_key(key)
        if model is None or reasoning_effort not in REASONING_EFFORTS:
            return False
        model.reasoning_effort = reasoning_effort
        if self.current_model_key == key:
            self._set_current_model(model)
        return self._save_config()

    def add_model(
        self,
        base_url: str,
        api_key: str,
        model_ids: list[str],
        message_format: str = DEFAULT_MESSAGE_FORMAT,
    ) -> list[ModelConfig]:
        self._reload_from_disk()
        if self.load_error is not None or message_format not in MESSAGE_FORMATS:
            return []

        new_models = []
        for model_id in model_ids:
            model = ModelConfig(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                model_id=model_id.strip(),
                is_favorite=False,
                message_format=message_format,
            )
            existing = any(existing_model.key == model.key for existing_model in self.models)
            if not existing:
                self.models.append(model)
                new_models.append(model)

        if new_models:
            if self.current_model is None:
                self._set_initial_current_model()
            self._save_config()

        return new_models

    def delete_model_by_index(self, index: int) -> bool:
        if not self._reload_from_disk():
            return False
        if not (0 <= index < len(self.models)):
            return False

        return self.delete_model_by_key(self.models[index].key)

    def delete_model_by_key(self, key: ModelKey) -> bool:
        if not self._reload_from_disk():
            return False
        delete_index = next(
            (index for index, model in enumerate(self.models) if model.key == key),
            None,
        )
        if delete_index is None:
            return False

        deleted_model = self.models[delete_index]
        deleting_current_model = self.current_model_key == deleted_model.key
        del self.models[delete_index]

        if self.last_selected_key == deleted_model.key:
            self.last_selected_key = None
        if self.memory_recall_model_key == deleted_model.key:
            self.memory_recall_model_key = None
        if deleting_current_model:
            fallback_model = self._get_default_model()
            self._set_current_model(fallback_model)
            self.last_selected_key = fallback_model.key if fallback_model else None

        return self._save_config()

    def toggle_favorite_by_index(self, index: int) -> bool:
        if not self._reload_from_disk():
            return False
        if not (0 <= index < len(self.models)):
            return False
        self.models[index].is_favorite = not self.models[index].is_favorite
        saved = self._save_config()
        if saved and self.current_model is None:
            self._set_initial_current_model()
        return saved


_model_manager: Optional[ModelManager] = None


def init_model_manager(config_dir: Path) -> ModelManager:
    global _model_manager
    _model_manager = ModelManager(config_dir)
    return _model_manager


def get_model_manager() -> Optional[ModelManager]:
    return _model_manager


def get_current_model_config() -> Optional[ModelConfig]:
    if _model_manager:
        return _model_manager.get_current_model()
    return None
