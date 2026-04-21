"""
模型管理模块 - 负责管理 LLM 模型配置
"""
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


@dataclass
class ModelConfig:
    """模型配置"""
    base_url: str
    api_key: str
    model_id: str
    is_favorite: bool = False
    selected: bool = False
    max_context: int = 128  # 单位: k (千tokens)

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

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """从字典创建"""
        return cls(
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            model_id=data.get("model_id", ""),
            is_favorite=data.get("is_favorite", False),
            selected=data.get("selected", False),
            max_context=data.get("max_context", 128),
        )


class ModelManager:
    """模型管理器"""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.config_file = config_dir / "model_config.json"
        self.models: list[ModelConfig] = []
        self._load_config()

    def _sort_models(self):
        self.models.sort(key=lambda model: (not model.is_favorite, model.model_id.lower()))

    def _normalize_selected(self):
        selected_indexes = [index for index, model in enumerate(self.models) if model.selected]
        if len(selected_indexes) > 1:
            keep_index = selected_indexes[0]
            for index, model in enumerate(self.models):
                model.selected = index == keep_index
        elif not selected_indexes:
            return

    def _load_config(self):
        """加载配置文件（纯列表结构）"""
        if not self.config_file.exists():
            self.models = []
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                self.models = [ModelConfig.from_dict(item) for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                # 兼容旧结构
                self.models = [
                    ModelConfig.from_dict(item)
                    for item in data.get("models", [])
                    if isinstance(item, dict)
                ]
            else:
                self.models = []

            self._sort_models()
            self._normalize_selected()
        except Exception:
            self.models = []

    def _save_config(self):
        """保存配置文件（纯列表结构）"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._sort_models()
        self._normalize_selected()
        data = [model.to_dict() for model in self.models]
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _reload_from_disk(self):
        self._load_config()

    def is_configured(self) -> bool:
        self._reload_from_disk()
        return len(self.models) > 0

    def get_favorite_models(self) -> list[ModelConfig]:
        self._reload_from_disk()
        return [m for m in self.models if m.is_favorite]

    def get_current_model(self) -> Optional[ModelConfig]:
        self._reload_from_disk()
        selected_models = [model for model in self.models if model.selected]
        if selected_models:
            return selected_models[0]

        return self.models[0] if self.models else None

    def set_current_model_by_index(self, index: int) -> bool:
        self._reload_from_disk()
        if not (0 <= index < len(self.models)):
            return False

        for i, model in enumerate(self.models):
            model.selected = i == index
        self._save_config()
        return True

    def add_model(
        self,
        base_url: str,
        api_key: str,
        model_ids: list[str],
        max_contexts: Optional[list[int]] = None,
    ) -> list[ModelConfig]:
        self._reload_from_disk()

        if max_contexts is None:
            max_contexts = [128] * len(model_ids)

        while len(max_contexts) < len(model_ids):
            max_contexts.append(128)

        new_models = []
        for i, model_id in enumerate(model_ids):
            model = ModelConfig(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                model_id=model_id.strip(),
                is_favorite=False,
                selected=False,
                max_context=max_contexts[i] if i < len(max_contexts) else 128,
            )
            existing = any(
                existing_model.base_url == model.base_url
                and existing_model.model_id == model.model_id
                for existing_model in self.models
            )
            if not existing:
                self.models.append(model)
                new_models.append(model)

        if new_models:
            if not any(model.selected for model in self.models):
                self.models[0].selected = True
            self._save_config()

        return new_models

    def delete_model_by_index(self, index: int) -> bool:
        self._reload_from_disk()
        if not (0 <= index < len(self.models)):
            return False

        was_selected = self.models[index].selected
        del self.models[index]

        if self.models and was_selected and not any(model.selected for model in self.models):
            self.models[0].selected = True

        self._save_config()
        return True

    def toggle_favorite_by_index(self, index: int) -> bool:
        self._reload_from_disk()
        if not (0 <= index < len(self.models)):
            return False
        self.models[index].is_favorite = not self.models[index].is_favorite
        self._save_config()
        return True


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
