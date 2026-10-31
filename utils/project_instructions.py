"""Load AGENTS.md files and inject them as turn-scoped user context."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

FILENAME = "AGENTS.md"
MAX_TOTAL_BYTES = 32 * 1024

_PROJECT_ROOT_MARKER = ".git"
_INSTRUCTIONS_START = "<INSTRUCTIONS>"
_INSTRUCTIONS_END = "</INSTRUCTIONS>"
_MEMORY_HEADER = "# Potentially Relevant Memories"
_CURRENT_REQUEST_HEADER = "# Current User Request"
_MEMORY_REQUEST_SEPARATOR = f"\n\n{_CURRENT_REQUEST_HEADER}\n\n"

INITIAL_NOTICE = (
    "Loaded at the start of this turn. Changes to these instructions take effect "
    "from the next turn."
)
REPLACEMENT_NOTICE = (
    "These project instructions replace all previously provided project instructions."
)
REMOVAL_NOTICE = "The previously provided project instructions no longer apply."


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class LoadedInstructions:
    workdir: Path
    files: tuple[InstructionFile, ...] = ()
    digest: str | None = None


def load(workdir: str | Path) -> LoadedInstructions:
    """Load AGENTS.md files from the project root down to *workdir*."""
    workdir_path = Path(workdir).expanduser().resolve()
    files: list[InstructionFile] = []
    seen: set[Path] = set()
    remaining = MAX_TOTAL_BYTES

    for directory in _directory_chain(workdir_path):
        if remaining <= 0:
            break
        candidate = directory / FILENAME
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result = _read_instruction_file(resolved, remaining)
        if result is None:
            continue
        text, truncated, consumed = result
        files.append(InstructionFile(path=resolved, text=text, truncated=truncated))
        remaining -= consumed

    return LoadedInstructions(
        workdir=workdir_path,
        files=tuple(files),
        digest=_digest(files) if files else None,
    )


def render(
    loaded: LoadedInstructions,
    *,
    notice: str = INITIAL_NOTICE,
    replacement: bool = False,
) -> str:
    """Render a user-context block for the loaded instructions."""
    if not loaded.files:
        return ""

    lines = [f"# AGENTS.md instructions for {loaded.workdir}", "", notice]
    if replacement:
        lines.extend(["", REPLACEMENT_NOTICE])
    lines.extend(["", _INSTRUCTIONS_START])
    for index, item in enumerate(loaded.files):
        if index:
            lines.append("")
        lines.append(f"## {item.path}")
        lines.append(item.text.rstrip("\r\n"))
    lines.append(_INSTRUCTIONS_END)
    return "\n".join(lines)


def render_removal(workdir: str | Path) -> str:
    """Render the notice that withdraws previously injected instructions."""
    path = Path(workdir).expanduser().resolve()
    return f"# AGENTS.md instructions for {path}\n\n{REMOVAL_NOTICE}"


def inject_project_instructions(messages: list[dict], workdir: str | Path) -> str | None:
    """Inject the current AGENTS.md state into the latest user message.

    The injection happens at most once per user message. Unchanged instructions
    are not repeated; changed instructions are appended with a replacement
    notice, and deleted instructions produce a removal notice.
    """
    if not messages or messages[-1].get("role") != "user":
        return None

    message = messages[-1]
    metadata = message.get("message_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("project_instructions"), dict):
        return None

    previous_state = _previous_state(messages)
    loaded = load(workdir)

    if loaded.files:
        if previous_state is None or previous_state.get("kind") == "removal":
            kind = "initial"
            block = render(loaded)
        elif _state_key(previous_state) == _state_key_from_loaded(loaded):
            return None
        else:
            kind = "replacement"
            block = render(loaded, replacement=True)
    else:
        if previous_state is None or previous_state.get("kind") == "removal":
            return None
        kind = "removal"
        block = render_removal(loaded.workdir)

    if not isinstance(metadata, dict):
        metadata = {}
        message["message_metadata"] = metadata
    _set_display_content(metadata, message.get("content"))

    content = message.get("content")
    if isinstance(content, list):
        message["content"] = [{"type": "text", "text": block}, *content]
    else:
        message["content"] = block + "\n\n" + (content if isinstance(content, str) else "")

    metadata["project_instructions"] = {
        "digest": loaded.digest,
        "workdir": str(loaded.workdir),
        "paths": [str(item.path) for item in loaded.files],
        "kind": kind,
    }
    return block


def _directory_chain(workdir: Path) -> list[Path]:
    root = _find_project_root(workdir)
    try:
        relative = workdir.relative_to(root)
    except ValueError:
        return [workdir]

    chain = [root]
    current = root
    for part in relative.parts:
        current = current / part
        chain.append(current)
    return chain


def _find_project_root(workdir: Path) -> Path:
    for candidate in (workdir, *workdir.parents):
        if (candidate / _PROJECT_ROOT_MARKER).exists():
            return candidate
    return workdir


def _read_instruction_file(
    path: Path,
    remaining: int,
) -> tuple[str, bool, int] | None:
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(remaining + 1)
    except OSError:
        return None

    truncated = len(data) > remaining
    if truncated:
        data = data[:remaining]
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return None
    return text, truncated, len(data)


def _digest(files: list[InstructionFile]) -> str:
    hasher = hashlib.sha256()
    for item in files:
        hasher.update(str(item.path).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(item.text.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _state_key(state: dict) -> tuple[object, object]:
    return state.get("digest"), state.get("workdir")


def _state_key_from_loaded(loaded: LoadedInstructions) -> tuple[object, object]:
    return loaded.digest, str(loaded.workdir)


def _previous_state(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        metadata = message.get("message_metadata")
        if not isinstance(metadata, dict):
            continue
        state = metadata.get("project_instructions")
        if not isinstance(state, dict):
            continue
        if _contains_rendered_block(message, state):
            return state
    return None


def _contains_rendered_block(message: dict, state: dict) -> bool:
    text = _message_text(message)
    kind = state.get("kind")
    if kind == "removal":
        return REMOVAL_NOTICE in text
    if kind == "replacement" and REPLACEMENT_NOTICE not in text:
        return False
    if _INSTRUCTIONS_START not in text or _INSTRUCTIONS_END not in text:
        return False
    paths = state.get("paths")
    if isinstance(paths, list):
        return all(isinstance(path, str) and path in text for path in paths)
    return True


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _set_display_content(metadata: dict, content: object) -> None:
    if isinstance(metadata.get("display_content"), str):
        return
    if not isinstance(content, str):
        return

    display = content
    if content.startswith(_MEMORY_HEADER):
        _, separator, tail = content.partition(_MEMORY_REQUEST_SEPARATOR)
        if separator:
            display = tail
    metadata["display_content"] = display
