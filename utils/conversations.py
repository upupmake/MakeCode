import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import paths
from utils.common import sanitize_title


SCHEMA_VERSION = 1
CONVERSATION_FILE = "conversation.json"
TASK_PLAN_FILE = "task_plan.json"
SUB_AGENT_HISTORY_FILE = "sub_agents/history.json"
SUB_AGENT_RUNS_DIR = "sub_agents/runs"
_CONVERSATION_ID_PATTERN = re.compile(r"conv_[0-9a-f]{32}")


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _read_object(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid 6.0 conversation data: {path}")
    return data


def _write_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: str
    path: Path
    root: Path
    title: str | None
    created_at: str
    updated_at: str
    messages: list
    task_plan: dict[str, Any] | None
    sub_agent_history: list[dict[str, Any]]


class ConversationStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.workspace_conversations_dir()
        self._active_id: str | None = None
        self._active_path: Path | None = None
        self._active_title: str | None = None
        self._active_created_at: str | None = None

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def active_path(self) -> Path | None:
        return self._active_path

    @property
    def active_root(self) -> Path | None:
        return self._active_path.parent if self._active_path is not None else None

    @property
    def active_title(self) -> str | None:
        return self._active_title

    def refresh_workspace(self) -> None:
        self.root = paths.workspace_conversations_dir()
        self.reset()

    def reset(self) -> None:
        self._active_id = None
        self._active_path = None
        self._active_title = None
        self._active_created_at = None

    def ensure_active(self) -> Path:
        if self._active_path is not None:
            return self._active_path
        conversation_id = f"conv_{uuid.uuid4().hex}"
        self._active_id = conversation_id
        self._active_path = self.root / conversation_id / CONVERSATION_FILE
        self._active_title = None
        self._active_created_at = _now()
        return self._active_path

    def save_messages(self, messages: list) -> Path:
        path = self.ensure_active()
        self._validate_conversation_path(path)
        if path.exists():
            self._validate_manifest(path)
        now = _now()
        data = {
            "schema_version": SCHEMA_VERSION,
            "conversation_id": self._active_id,
            "title": self._active_title,
            "created_at": self._active_created_at,
            "updated_at": now,
            "messages": messages,
            "artifacts": {
                "task_plan": TASK_PLAN_FILE,
                "sub_agent_history": SUB_AGENT_HISTORY_FILE,
                "sub_agent_runs": SUB_AGENT_RUNS_DIR,
            },
        }
        _write_object(path, data)
        return path

    def update_title(self, title: str) -> None:
        if self._active_path is None or not self._active_path.exists():
            raise RuntimeError("No saved conversation is active")
        safe_title = sanitize_title(title)
        if not safe_title:
            return
        data = self._validate_manifest(self._active_path)
        data["title"] = safe_title
        data["updated_at"] = _now()
        _write_object(self._active_path, data)
        self._active_title = safe_title

    def list_conversations(self) -> list[Path]:
        if not self.root.exists():
            return []
        files = []
        for path in self.root.glob(f"conv_*/{CONVERSATION_FILE}"):
            try:
                self._validate_manifest(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            files.append(path)
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return files

    def get_title(self, path: Path) -> str | None:
        return self._validate_manifest(path).get("title")

    def load(self, path: Path) -> ConversationSnapshot:
        path = Path(path)
        manifest = self._validate_manifest(path)
        conversation_id = manifest["conversation_id"]
        root = path.parent
        self._validate_sidecar_paths(root)

        task_plan_path = root / TASK_PLAN_FILE
        task_plan = None
        if task_plan_path.exists():
            if task_plan_path.is_symlink():
                raise ValueError(f"Invalid task plan path: {task_plan_path}")
            task_plan = _read_object(task_plan_path)
            self._validate_sidecar(task_plan, conversation_id, task_plan_path)
            if not isinstance(task_plan.get("tasks"), dict):
                raise ValueError(f"Invalid task plan data: {task_plan_path}")

        history_path = root / SUB_AGENT_HISTORY_FILE
        sub_agent_history: list[dict[str, Any]] = []
        if history_path.exists():
            if history_path.is_symlink() or history_path.parent.is_symlink():
                raise ValueError(f"Invalid sub-agent history path: {history_path}")
            history_data = _read_object(history_path)
            self._validate_sidecar(history_data, conversation_id, history_path)
            records = history_data.get("records")
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                raise ValueError(f"Invalid sub-agent history data: {history_path}")
            sub_agent_history = records

        return ConversationSnapshot(
            conversation_id=conversation_id,
            path=path,
            root=root,
            title=manifest.get("title"),
            created_at=manifest["created_at"],
            updated_at=manifest["updated_at"],
            messages=manifest["messages"],
            task_plan=task_plan,
            sub_agent_history=sub_agent_history,
        )

    def activate(self, snapshot: ConversationSnapshot) -> None:
        manifest = self._validate_manifest(snapshot.path)
        self._validate_sidecar_paths(snapshot.root)
        if manifest["conversation_id"] != snapshot.conversation_id:
            raise ValueError("Conversation snapshot ID mismatch")
        self._active_id = snapshot.conversation_id
        self._active_path = snapshot.path
        self._active_title = snapshot.title
        self._active_created_at = snapshot.created_at

    def delete(self, path: Path) -> None:
        path = Path(path)
        manifest = self._validate_manifest(path)
        self._validate_sidecar_paths(path.parent)
        if self._active_id == manifest["conversation_id"]:
            raise ValueError("当前对话正在使用，不能删除。")
        shutil.rmtree(path.parent)

    def _validate_manifest(self, path: Path) -> dict[str, Any]:
        path = Path(path)
        self._validate_conversation_path(path)
        data = _read_object(path)
        conversation_id = data.get("conversation_id")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported conversation schema: {path}")
        if conversation_id != path.parent.name:
            raise ValueError(f"Conversation ID does not match directory: {path}")
        if not isinstance(data.get("messages"), list) or not all(
            isinstance(message, dict) for message in data["messages"]
        ):
            raise ValueError(f"Invalid conversation messages: {path}")
        for message in data["messages"]:
            tool_calls = message.get("tool_calls")
            if tool_calls is not None and (
                not isinstance(tool_calls, list)
                or any(not isinstance(tool_call, dict) for tool_call in tool_calls)
            ):
                raise ValueError(f"Invalid conversation tool calls: {path}")
        if not isinstance(data.get("created_at"), str) or not isinstance(data.get("updated_at"), str):
            raise ValueError(f"Invalid conversation timestamps: {path}")
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError(f"Invalid conversation title: {path}")
        expected_artifacts = {
            "task_plan": TASK_PLAN_FILE,
            "sub_agent_history": SUB_AGENT_HISTORY_FILE,
            "sub_agent_runs": SUB_AGENT_RUNS_DIR,
        }
        if data.get("artifacts") != expected_artifacts:
            raise ValueError(f"Invalid conversation artifact map: {path}")
        return data

    def _validate_conversation_path(self, path: Path) -> None:
        self._validate_root()
        root = self.root.resolve()
        if (
            path.name != CONVERSATION_FILE
            or not _CONVERSATION_ID_PATTERN.fullmatch(path.parent.name)
            or path.is_symlink()
            or path.parent.is_symlink()
            or path.parent.parent.resolve() != root
            or path.parent.resolve() != root / path.parent.name
        ):
            raise ValueError(f"Invalid 6.0 conversation path: {path}")

    def _validate_root(self) -> None:
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError(f"Invalid 6.0 conversation root: {self.root}")

    @staticmethod
    def _validate_sidecar_paths(root: Path) -> None:
        paths_to_validate = (
            root / TASK_PLAN_FILE,
            root / "sub_agents",
            root / SUB_AGENT_HISTORY_FILE,
            root / SUB_AGENT_RUNS_DIR,
        )
        sub_agents_dir = root / "sub_agents"
        runs_dir = root / SUB_AGENT_RUNS_DIR
        if (
            any(path.is_symlink() for path in paths_to_validate)
            or (sub_agents_dir.exists() and not sub_agents_dir.is_dir())
            or (runs_dir.exists() and not runs_dir.is_dir())
        ):
            raise ValueError(f"Invalid conversation storage path: {root}")

    @staticmethod
    def _validate_sidecar(data: dict[str, Any], conversation_id: str, path: Path) -> None:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported conversation sidecar schema: {path}")
        if data.get("conversation_id") != conversation_id:
            raise ValueError(f"Conversation sidecar ID mismatch: {path}")


CONVERSATION_STORE = ConversationStore()
