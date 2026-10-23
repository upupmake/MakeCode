import json
import os
import re
import shutil
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from utils import paths
from utils.common import sanitize_title
from utils.tool_validation import normalize_tool_messages
from utils.vision import image_blocks_from_content, resolve_image_attachment

SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, RUNTIME_SCHEMA_VERSION})
CONVERSATION_FILE = "conversation.json"
TASK_PLAN_FILE = "task_plan.json"
SUB_AGENT_HISTORY_FILE = "sub_agents/history.json"
SUB_AGENT_RUNS_DIR = "sub_agents/runs"
ATTACHMENTS_DIR = "attachments"
_CONVERSATION_ID_PATTERN = re.compile(r"conv_[0-9a-f]{32}")


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _copy_user_content(message: dict[str, Any]) -> Any:
    metadata = message.get("message_metadata")
    display_content = metadata.get("display_content") if isinstance(metadata, dict) else None
    if isinstance(display_content, str):
        return display_content

    content = message.get("content")
    memory_prefix = "# Potentially Relevant Memories\n\n"
    request_marker = "# Current User Request\n\n"
    if isinstance(content, str) and content.startswith(memory_prefix) and request_marker in content:
        return content.split(request_marker, 1)[1]
    return content


def _assistant_search_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
    if isinstance(content, str) and content:
        return content
    return "\n\n".join(
        block.get("text", "")
        for block in message.get("content_blocks") or []
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text")
    )


def _conversation_search_text(
    conversation_id: str,
    title: str | None,
    messages: list,
) -> str:
    parts = [conversation_id]
    if title:
        parts.append(title)
    for message in messages:
        role = message.get("role")
        if role == "user":
            content = _copy_user_content(message)
        elif role == "assistant":
            content = _assistant_search_text(message)
        else:
            continue
        if isinstance(content, str) and content:
            parts.append(content)
    return "\n".join(parts)


def _read_object(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as file:
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
    runtime: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConversationListItem:
    path: Path
    conversation_id: str
    title: str | None
    searchable_text: str


class ConversationStore:
    def __init__(self, root: Path | None = None, *, persist: bool = True) -> None:
        self.root = root or paths.workspace_conversations_dir()
        self.persist = persist
        self._active_id: str | None = None
        self._active_path: Path | None = None
        self._active_title: str | None = None
        self._active_created_at: str | None = None
        self._active_runtime: dict[str, Any] | None = None

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

    @property
    def active_runtime(self) -> dict[str, Any] | None:
        return self._active_runtime

    def refresh_workspace(self) -> None:
        self.root = paths.workspace_conversations_dir()
        self.reset()

    def reset(self) -> None:
        self._active_id = None
        self._active_path = None
        self._active_title = None
        self._active_created_at = None
        self._active_runtime = None

    @staticmethod
    def normalize_conversation_id(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("conv_"):
            candidate = candidate[5:]
        if re.fullmatch(r"[0-9a-fA-F]{32}", candidate):
            return f"conv_{candidate.lower()}"
        compact = candidate.replace("-", "")
        if re.fullmatch(r"[0-9a-fA-F]{32}", compact):
            return f"conv_{compact.lower()}"
        raise ValueError(f"Invalid conversation ID: {value}")

    def create(
        self,
        conversation_id: str | None = None,
        *,
        title: str | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> Path:
        if conversation_id is None:
            conversation_id = f"conv_{uuid.uuid4().hex}"
        else:
            conversation_id = self.normalize_conversation_id(conversation_id)
        path = self.root / conversation_id / CONVERSATION_FILE
        if path.parent.exists():
            raise FileExistsError(f"Conversation already exists: {conversation_id}")
        if self.persist:
            self.root.mkdir(parents=True, exist_ok=True)
            self._validate_conversation_path(path)
        self._active_id = conversation_id
        self._active_path = path
        self._active_title = title
        self._active_created_at = _now()
        self._active_runtime = runtime
        return path

    def ensure_active(self) -> Path:
        if self._active_path is not None:
            return self._active_path
        return self.create()

    def find_latest(self) -> ConversationSnapshot | None:
        items = self.list_conversations()
        if not items:
            return None
        return self.load(items[0].path)

    def save_messages(
        self,
        messages: list,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> Path:
        path = self.ensure_active()
        if runtime is not None:
            self._active_runtime = runtime
        if not self.persist:
            return path
        self._validate_conversation_path(path)
        self._validate_sidecar_paths(path.parent)
        if path.exists():
            self._validate_manifest(path)
        messages = normalize_tool_messages(messages)
        now = _now()
        artifacts = {
            "task_plan": TASK_PLAN_FILE,
            "sub_agent_history": SUB_AGENT_HISTORY_FILE,
            "sub_agent_runs": SUB_AGENT_RUNS_DIR,
        }
        if any(image_blocks_from_content(message.get("content")) for message in messages):
            artifacts["attachments"] = ATTACHMENTS_DIR
        data = {
            "schema_version": SCHEMA_VERSION,
            "conversation_id": self._active_id,
            "title": self._active_title,
            "created_at": self._active_created_at,
            "updated_at": now,
            "messages": messages,
            "artifacts": artifacts,
        }
        if self._active_runtime is not None:
            data["schema_version"] = RUNTIME_SCHEMA_VERSION
            data["runtime"] = self._active_runtime
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

    def list_conversations(self) -> list[ConversationListItem]:
        if not self.root.exists():
            return []
        items = []
        for path in self.root.glob(f"conv_*/{CONVERSATION_FILE}"):
            try:
                manifest = self._validate_manifest(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            title = manifest.get("title")
            items.append(
                ConversationListItem(
                    path=path,
                    conversation_id=manifest["conversation_id"],
                    title=title if isinstance(title, str) else None,
                    searchable_text=_conversation_search_text(
                        manifest["conversation_id"],
                        title if isinstance(title, str) else None,
                        manifest["messages"],
                    ),
                )
            )
        items.sort(key=lambda item: item.path.stat().st_mtime, reverse=True)
        return items

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
            runtime=manifest.get("runtime"),
        )

    def activate(self, snapshot: ConversationSnapshot) -> None:
        if self.persist:
            manifest = self._validate_manifest(snapshot.path)
            self._validate_sidecar_paths(snapshot.root)
            if manifest["conversation_id"] != snapshot.conversation_id:
                raise ValueError("Conversation snapshot ID mismatch")
        self._active_id = snapshot.conversation_id
        self._active_path = snapshot.path
        self._active_title = snapshot.title
        self._active_created_at = snapshot.created_at
        self._active_runtime = snapshot.runtime

    def fork(
        self,
        snapshot: ConversationSnapshot,
        conversation_id: str | None = None,
    ) -> ConversationSnapshot:
        if conversation_id is None:
            conversation_id = f"conv_{uuid.uuid4().hex}"
        else:
            conversation_id = self.normalize_conversation_id(conversation_id)
        new_root = self.root / conversation_id
        if new_root.exists():
            raise FileExistsError(f"Conversation already exists: {conversation_id}")
        if not self.persist:
            self.create(conversation_id)
            path = self._active_path
            assert path is not None
            return ConversationSnapshot(
                conversation_id=conversation_id,
                path=path,
                root=path.parent,
                title=snapshot.title,
                created_at=self._active_created_at or _now(),
                updated_at=_now(),
                messages=list(snapshot.messages),
                task_plan=snapshot.task_plan,
                sub_agent_history=list(snapshot.sub_agent_history),
                runtime=snapshot.runtime,
            )
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot.root, new_root, symlinks=True)
        manifest_path = new_root / CONVERSATION_FILE
        manifest = _read_object(manifest_path)
        manifest["conversation_id"] = conversation_id
        manifest["created_at"] = _now()
        manifest["updated_at"] = _now()
        for sidecar_name in (TASK_PLAN_FILE, SUB_AGENT_HISTORY_FILE):
            sidecar_path = new_root / sidecar_name
            if sidecar_path.exists():
                sidecar = _read_object(sidecar_path)
                sidecar["conversation_id"] = conversation_id
                _write_object(sidecar_path, sidecar)
        _write_object(manifest_path, manifest)
        return self.load(manifest_path)

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
        if data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
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
            for block in image_blocks_from_content(message.get("content")):
                resolve_image_attachment(path.parent, block)
        if not isinstance(data.get("created_at"), str) or not isinstance(data.get("updated_at"), str):
            raise ValueError(f"Invalid conversation timestamps: {path}")
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError(f"Invalid conversation title: {path}")
        runtime = data.get("runtime")
        if runtime is not None and not isinstance(runtime, dict):
            raise ValueError(f"Invalid conversation runtime metadata: {path}")
        base_artifacts = {
            "task_plan": TASK_PLAN_FILE,
            "sub_agent_history": SUB_AGENT_HISTORY_FILE,
            "sub_agent_runs": SUB_AGENT_RUNS_DIR,
        }
        actual_artifacts = data.get("artifacts")
        allowed_artifacts = (
            base_artifacts,
            {**base_artifacts, "attachments": ATTACHMENTS_DIR},
        )
        image_present = any(
            image_blocks_from_content(message.get("content"))
            for message in data["messages"]
        )
        if actual_artifacts not in allowed_artifacts or (
            image_present and actual_artifacts != allowed_artifacts[1]
        ):
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
            root / ATTACHMENTS_DIR,
        )
        sub_agents_dir = root / "sub_agents"
        runs_dir = root / SUB_AGENT_RUNS_DIR
        attachments_dir = root / ATTACHMENTS_DIR
        if (
            any(path.is_symlink() for path in paths_to_validate)
            or (sub_agents_dir.exists() and not sub_agents_dir.is_dir())
            or (runs_dir.exists() and not runs_dir.is_dir())
            or (attachments_dir.exists() and not attachments_dir.is_dir())
            or (
                attachments_dir.exists()
                and any(path.is_symlink() or not path.is_file() for path in attachments_dir.iterdir())
            )
        ):
            raise ValueError(f"Invalid conversation storage path: {root}")

    @staticmethod
    def _validate_sidecar(data: dict[str, Any], conversation_id: str, path: Path) -> None:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported conversation sidecar schema: {path}")
        if data.get("conversation_id") != conversation_id:
            raise ValueError(f"Conversation sidecar ID mismatch: {path}")


class ConversationLockError(RuntimeError):
    pass


class ConversationLock:
    """Exclusive per-conversation lock shared by TUI and headless processes."""

    def __init__(self, root: Path, *, timeout: float = 0.0) -> None:
        self.root = Path(root)
        self.timeout = max(timeout, 0.0)
        self._file: BinaryIO | None = None
        self._stack = ExitStack()

    def __enter__(self) -> "ConversationLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()

    def acquire(self) -> "ConversationLock":
        self.root.mkdir(parents=True, exist_ok=True)
        self._file = self._stack.enter_context(open(self.root / ".lock", "a+b"))  # noqa: SIM115 - lock lifetime is the context
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock_file()
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._stack.close()
                    self._file = None
                    raise ConversationLockError(
                        f"Conversation is already in use: {self.root}"
                    ) from exc
                time.sleep(0.05)

    def _lock_file(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            self._file.write(b"\0")
            self._file.flush()
            self._file.seek(0)
            msvcrt.locking(  # type: ignore[attr-defined]
                self._file.fileno(), msvcrt.LK_NBLCK, 1  # type: ignore[attr-defined]
            )
            return
        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    self._file.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._stack.close()
            self._file = None


CONVERSATION_STORE = ConversationStore()
