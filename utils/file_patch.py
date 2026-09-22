"""Strict unified-diff patch application for agent file operations."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from system.ts_validator import validate_code
from utils import paths
from utils.file_access import GLOBAL_FILE_CONTROLLER
from utils.hitl import check_paths_permission, check_permission
from utils.tool_validation import ToolArgumentsModel


_UTF8_BOM = b"\xef\xbb\xbf"
_FILE_HEADER = re.compile(
    r"^\*\*\* (Update|Add|Delete) File: (.*?)(?:[ \t]+\*\*\*)?[ \t]*$"
)
_HUNK_HEADER = re.compile(
    r"^@@(?: -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*)?$"
)
_FORMAT_HINT = (
    "Start directly with a file header such as '*** Update File: path'; "
    "use one file section per path and put all @@ hunks for that file in it."
)


class _PatchFormatError(ValueError):
    def __init__(self, messages: list[str] | tuple[str, ...]):
        self.messages = tuple(messages)
        super().__init__("; ".join(self.messages))


class FilePatch(ToolArgumentsModel):
    """Apply a strict unified-diff patch with independent per-file transactions.

    Start directly with ``*** Update File: path``, ``*** Add File: path``, or
    ``*** Delete File: path``; no outer wrapper. Read the current file first and copy
    exact context; never copy ``<line-number>:`` prefixes from FileRead output. Use one
    file section per path and put every ``@@`` hunk for that path in that section. Update
    hunk lines must start with one space (context), ``-`` (remove), or ``+`` (add); a
    blank context line is still a line containing one leading space. Add File content
    requires ``+`` on every line, and Delete File has no body. Each file is committed
    independently; if the result is partial, successful files are already committed;
    retry only the listed failed entries. Each Update file must contain at least one
    effective change. A context-only hunk or an identical ``-``/``+`` replacement may
    be included only as a locator alongside another changing hunk.

    Examples:
    Example 1 (one file with multiple @@ hunks in one section)::

        *** Update File: sample.txt
        @@
         a
        -b
        +B
        @@
         c
        -d
        +D

    Example 2 (multiple files in separate sections)::

        *** Update File: first.txt
        @@
        -before
        +after
        *** Add File: second.txt
        +new content
    """

    patch: str = Field(
        ...,
        min_length=1,
        description=(
            "Complete FilePatch text starting directly with '*** Update File: path', "
            "'*** Add File: path', or '*** Delete File: path', with no outer wrapper. "
            "Read the current file first and copy exact context from FileRead without "
            "line-number prefixes. Use one file section per path and put all @@ hunks "
            "for that path in it. Update hunk lines must start with one space for "
            "context, '-' for removals, or '+' for additions; blank context lines still "
            "need the leading space. Pure insertions use a zero-old-line hunk such as "
            "'@@ -0,0 +1,1 @@'; a bare '@@' pure insertion is allowed only for an empty "
            "file. Each Update file must contain at least one effective change. A "
            "context-only hunk or identical '-'/'+' replacement may be included only "
            "as a locator alongside another changing hunk. Add File content uses one "
            "'+' prefix per line; Delete File has no body. "
            "Each actual file may appear only once and is committed independently. If "
            "the result is partial, retry only the listed failed entries. See the tool "
            "description for complete examples."
        ),
    )


@dataclass(frozen=True)
class _Hunk:
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    old_start: int | None = None
    old_count: int | None = None


@dataclass(frozen=True)
class _PatchFile:
    operation: Literal["Update", "Add", "Delete"]
    path: str
    hunks: tuple[_Hunk, ...] = ()
    add_lines: tuple[str, ...] = ()
    parse_error: str | None = None


@dataclass
class _Plan:
    spec: _PatchFile
    path: Path
    existed: bool
    old_mode: int | None
    new_bytes: bytes | None
    hunk_count: int


def _patch_error(message: str) -> ValueError:
    return _PatchFormatError([message])


def _typed_failure(kind: str, message: str) -> str:
    return f"{kind}: {message}"


def _format_input_error(exc: Exception) -> str:
    messages = getattr(exc, "messages", None)
    if messages:
        if len(messages) == 1:
            return messages[0]
        return "Detected multiple format errors:\n" + "\n".join(
            f"  - {message}" for message in messages
        )
    errors = getattr(exc, "errors", None)
    if callable(errors):
        details = []
        for error in errors():
            location = ".".join(str(item) for item in error.get("loc", ())) or "patch"
            if error.get("type") == "string_too_short":
                details.append(f"{location} is empty")
            else:
                details.append(f"{location}: {error.get('msg', str(error))}")
        if details:
            return "; ".join(details)
    return str(exc)


def _explain_failure(message: str) -> str:
    if "matches multiple locations" in message:
        return f"{message}; Add enough unchanged context to make the hunk unique"
    if "context was not found" in message:
        return (
            f"{message}; re-read the file with FileRead and copy exact context "
            "without line-number prefixes"
        )
    if "overlaps another hunk" in message:
        return f"{message}; Merge overlapping hunks into one non-overlapping change"
    if "contains only no-op" in message:
        return (
            f"{message}; keep locator hunks only alongside at least one effective "
            "'-'/'+' change"
        )
    if "invalid line prefix" in message:
        return (
            f"{message}; blank context lines must be a single leading space, "
            "not an empty line"
        )
    if "must start with '+'" in message:
        return f"{message}; prefix every added-file content line with '+'"
    if "expected '@@' before update hunk" in message:
        return f"{message}; put every update hunk after the file header"
    if "declared by entry" in message or "refer to the same file" in message:
        return (
            f"{message}; Keep only one file header and combine multiple '@@' "
            "hunks for that path under it"
        )
    if "rollback failed" in message or "commit failed" in message:
        return f"{message}; inspect the affected files and any recovery backups"
    return message


def _is_file_boundary(line: str) -> bool:
    return _FILE_HEADER.match(line) is not None


def _is_hunk_header(line: str) -> bool:
    return line.startswith("@@")


def _parse_hunk_header(line: str) -> tuple[int, int] | None:
    if line == "@@":
        return None
    match = _HUNK_HEADER.match(line)
    if not match:
        raise ValueError("hunk header must be '@@' or standard unified-diff coordinates")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    return old_start, old_count


def _skip_to_file_boundary(lines: list[str], index: int, end: int) -> int:
    while index < end and not _is_file_boundary(lines[index]):
        index += 1
    return index


def _parse_patch(patch: str) -> list[_PatchFile]:
    normalized = patch.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if lines and lines[-1].rstrip(" \t") == "*** End Patch":
        lines.pop()
    format_errors: list[str] = []
    for line_number, line in enumerate(lines, 1):
        line_without_trailing_whitespace = line.rstrip(" \t")
        if line_without_trailing_whitespace == "*** End Patch":
            format_errors.append(
                f"unexpected patch marker '*** End Patch' at patch line {line_number}; "
                "it is only accepted as the final line"
            )
        elif line_without_trailing_whitespace == "*** Begin Patch":
            format_errors.append(
                f"unexpected patch marker '*** Begin Patch' at patch line {line_number}; "
                "remove this marker"
            )
    result: list[_PatchFile] = []
    index = 0
    end = len(lines)
    while index < end:
        match = _FILE_HEADER.match(lines[index])
        if not match:
            line = lines[index].rstrip(" \t")
            if line not in {"*** Begin Patch", "*** End Patch"}:
                format_errors.append(f"expected a file header at patch line {index + 1}")
            next_boundary = _skip_to_file_boundary(lines, index, end)
            index = next_boundary if next_boundary > index else index + 1
            continue
        operation = match.group(1)
        path = match.group(2).strip()
        path_error = None
        if not path or "\0" in path:
            path_error = f"invalid file path at line {index + 1}"
            path = "<invalid path>"
        index += 1

        if path_error is not None:
            index = _skip_to_file_boundary(lines, index, end)
            result.append(_PatchFile(operation, path, parse_error=path_error))
            continue

        if operation == "Add":
            content: list[str] = []
            parse_error = None
            while index < end and not _is_file_boundary(lines[index]):
                line = lines[index]
                if not line.startswith("+"):
                    parse_error = (
                        f"added file {path} has invalid content at patch line {index + 1}: "
                        f"{line!r}; every added-file content line must start with '+'"
                    )
                    index = _skip_to_file_boundary(lines, index, end)
                    break
                content.append(line[1:])
                index += 1
            result.append(_PatchFile("Add", path, add_lines=tuple(content), parse_error=parse_error))
            continue

        if operation == "Delete":
            parse_error = None
            if index < end and not _is_file_boundary(lines[index]):
                parse_error = f"deleted file {path} cannot contain patch content"
                index = _skip_to_file_boundary(lines, index, end)
            result.append(_PatchFile("Delete", path, parse_error=parse_error))
            continue

        hunks: list[_Hunk] = []
        parse_error = None
        while index < end and not _is_file_boundary(lines[index]):
            if not _is_hunk_header(lines[index]):
                parse_error = f"expected '@@' before update hunk for {path} at line {index + 1}"
                index = _skip_to_file_boundary(lines, index, end)
                break
            try:
                hunk_location = _parse_hunk_header(lines[index])
            except ValueError as exc:
                parse_error = f"invalid hunk header for {path} at line {index + 1}: {exc}"
                index = _skip_to_file_boundary(lines, index, end)
                break
            index += 1
            body: list[tuple[int, str]] = []
            while index < end and not _is_hunk_header(lines[index]) and not _is_file_boundary(lines[index]):
                body.append((index + 1, lines[index]))
                index += 1
            if not body:
                parse_error = f"empty update hunk for {path}"
                break

            old_lines: list[str] = []
            new_lines: list[str] = []
            parse_error = None
            for line_number, line in body:
                if line.startswith("+"):
                    new_lines.append(line[1:])
                elif line.startswith("-"):
                    old_lines.append(line[1:])
                elif line.startswith(" "):
                    context = line[1:]
                    old_lines.append(context)
                    new_lines.append(context)
                else:
                    parse_error = (
                        f"update hunk for {path} has invalid line prefix {line[:1]!r} "
                        f"at patch line {line_number}; hunk lines must start with ' ' "
                        "(context), '-' (remove), or '+' (add)"
                    )
                    break
            if parse_error is not None:
                break
            if not old_lines and hunk_location is not None and hunk_location[1] != 0:
                parse_error = (
                    f"update hunk for {path} has no context or removed lines, but its "
                    f"header declares {hunk_location[1]} old line(s)"
                )
                break
            if not old_lines and not new_lines:
                parse_error = f"update hunk for {path} does not contain any file lines"
                break
            hunks.append(
                _Hunk(
                    tuple(old_lines),
                    tuple(new_lines),
                    hunk_location[0] if hunk_location is not None else None,
                    hunk_location[1] if hunk_location is not None else None,
                )
            )

        if not hunks and parse_error is None:
            parse_error = f"update file {path} has no hunks"
        elif parse_error is None and not any(
            hunk.old_lines != hunk.new_lines for hunk in hunks
        ):
            parse_error = (
                f"update file {path} contains only no-op '@@' hunks; at least one "
                "hunk must make an effective change; context-only or identical "
                "'-'/'+' hunks are allowed only as locators alongside a changing hunk"
            )
        result.append(_PatchFile("Update", path, hunks=tuple(hunks), parse_error=parse_error))

    if not result:
        if format_errors:
            raise _PatchFormatError(format_errors)
        raise _patch_error("patch contains no file operations")
    if format_errors:
        raise _PatchFormatError(format_errors)
    return result


def _resolve_patch_path(raw_path: str, workdir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workdir / candidate
    return candidate.resolve(strict=False)


def _file_identity(path: Path) -> tuple[int, int] | None:
    """Return the filesystem identity for an existing path, when available."""
    try:
        file_stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    return file_stat.st_dev, file_stat.st_ino


def _decode_text(raw: bytes, path: Path) -> tuple[str, bool]:
    if b"\0" in raw:
        raise ValueError(f"{path} appears to be a binary file")
    has_bom = raw.startswith(_UTF8_BOM)
    try:
        text = (raw[len(_UTF8_BOM):] if has_bom else raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc
    return text, has_bom


def _text_lines(text: str) -> tuple[list[str], list[str]]:
    """Split text while retaining each line's original line ending."""
    if not text:
        return [], []
    parts = re.split(r"(\r\n|\n|\r)", text)
    lines: list[str] = []
    endings: list[str] = []
    for index in range(0, len(parts), 2):
        content = parts[index]
        ending = parts[index + 1] if index + 1 < len(parts) else ""
        if index == len(parts) - 1 and content == "" and ending == "":
            break
        lines.append(content)
        endings.append(ending)
    return lines, endings


def _find_occurrences(lines: list[str], needle: tuple[str, ...]) -> list[tuple[int, int]]:
    size = len(needle)
    if not size or size > len(lines):
        return []
    return [
        (index, index + size)
        for index in range(len(lines) - size + 1)
        if tuple(lines[index:index + size]) == needle
    ]


def _locate_hunk(
    lines: list[str], hunk: _Hunk
) -> tuple[int, int] | None:
    if hunk.old_lines:
        spans = _find_occurrences(lines, hunk.old_lines)
        if not spans:
            return None
        if len(spans) > 1:
            locations = ", ".join(f"{start + 1}-{end}" for start, end in spans[:5])
            if len(spans) > 5:
                locations += f", +{len(spans) - 5} more"
            raise ValueError(f"matches multiple locations ({locations})")
        return spans[0]

    if hunk.old_start is None:
        if not lines:
            return (0, 0)
        raise ValueError("pure addition hunk needs unified-diff coordinates for a non-empty file")
    insertion = hunk.old_start
    if hunk.old_start == 0:
        insertion = 0
    if insertion < 0 or insertion > len(lines):
        raise ValueError(f"pure addition hunk location {hunk.old_start} is outside the file")
    return insertion, insertion


def _apply_hunks(text: str, hunks: tuple[_Hunk, ...], path: Path) -> tuple[bytes, int]:
    lines, endings = _text_lines(text)
    located: list[tuple[int, int, tuple[str, ...]]] = []
    hunk_errors: list[str] = []
    for number, hunk in enumerate(hunks, 1):
        try:
            span = _locate_hunk(lines, hunk)
        except ValueError as exc:
            hunk_errors.append(f"hunk {number} {exc}")
            continue
        if span is None:
            hunk_errors.append(f"hunk {number} context was not found")
            continue
        start, end = span
        if any(
            (start < other_end and other_start < end)
            or (start == other_start and start == end == other_start == other_end)
            for other_start, other_end, _ in located
        ):
            hunk_errors.append(f"hunk {number} overlaps another hunk")
            continue
        located.append((start, end, hunk.new_lines))
    if hunk_errors:
        raise ValueError(
            f"{path}: {'; '.join(hunk_errors)}" if len(hunk_errors) == 1
            else f"{path}: Detected {len(hunk_errors)} hunk errors: {'; '.join(hunk_errors)}"
        )

    for start, end, replacement in sorted(located, key=lambda item: item[0], reverse=True):
        old_endings = endings[start:end]
        appended_after_unterminated_line = (
            bool(replacement)
            and start == len(lines)
            and start > 0
            and endings[start - 1] == ""
        )
        if replacement:
            if appended_after_unterminated_line:
                endings[start - 1] = "\n"
            if len(old_endings) == len(replacement):
                replacement_endings = old_endings
            else:
                default_ending = next((ending for ending in old_endings if ending), "\n")
                replacement_endings = [default_ending] * len(replacement)
                if old_endings and old_endings[-1] == "":
                    replacement_endings[-1] = ""
                elif appended_after_unterminated_line:
                    replacement_endings[-1] = ""
        else:
            replacement_endings = []
        lines[start:end] = list(replacement)
        endings[start:end] = replacement_endings
    assembled = "".join(line + ending for line, ending in zip(lines, endings))
    return assembled.encode("utf-8"), len(hunks)


def _unique_sibling(path: Path, prefix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    os.close(fd)
    temporary = Path(name)
    temporary.unlink(missing_ok=True)
    return temporary


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_parent(path: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise ValueError(f"parent of {path} is not a directory")
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def _fsync_directory(path: Path) -> None:
    try:
        flags = getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, os.O_RDONLY | flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (AttributeError, OSError):
        pass


def _commit(plans: list[_Plan]) -> None:
    temporary_paths: dict[Path, Path] = {}
    backup_paths: dict[Path, Path] = {}
    created_dirs: list[Path] = []
    try:
        for plan in plans:
            if plan.new_bytes is None:
                continue
            _ensure_parent(plan.path, created_dirs)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{plan.path.name}.makecode-",
                dir=str(plan.path.parent),
            )
            temporary = Path(temporary_name)
            temporary_paths[plan.path] = temporary
            open_fd = fd
            try:
                stream = os.fdopen(fd, "wb")
                open_fd = None
                with stream:
                    stream.write(plan.new_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, plan.old_mode if plan.old_mode is not None else 0o600)
            except Exception:
                if open_fd is not None:
                    os.close(open_fd)
                raise

        for plan in plans:
            if not plan.existed:
                if plan.path.exists():
                    raise ValueError(f"target appeared during patch: {plan.path}")
                continue
            backup = _unique_sibling(plan.path, f".{plan.path.name}.makecode-backup-")
            os.replace(plan.path, backup)
            backup_paths[plan.path] = backup

        for plan in plans:
            temporary = temporary_paths.get(plan.path)
            if temporary is not None:
                os.replace(temporary, plan.path)

        for backup in backup_paths.values():
            _unlink_quietly(backup)
        for directory in created_dirs:
            _fsync_directory(directory)
    except Exception as commit_error:
        for plan in reversed(plans):
            if plan.path in temporary_paths and plan.path.exists() and not plan.existed:
                _unlink_quietly(plan.path)
            elif plan.path in temporary_paths and plan.path.exists() and plan.path in backup_paths:
                _unlink_quietly(plan.path)
        restore_errors: list[str] = []
        for path, backup in backup_paths.items():
            if backup.exists():
                try:
                    os.replace(backup, path)
                except Exception as restore_error:
                    restore_errors.append(f"{path} from {backup}: {restore_error}")
        for temporary in temporary_paths.values():
            _unlink_quietly(temporary)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if restore_errors:
            details = "; ".join(restore_errors)
            raise RuntimeError(
                f"{commit_error}; rollback failed and recovery backup was retained: {details}"
            ) from commit_error
        raise
    finally:
        for temporary in temporary_paths.values():
            _unlink_quietly(temporary)


def file_patch(patch: str) -> str:
    try:
        try:
            validated = FilePatch.model_validate({"patch": patch})
            specs = _parse_patch(validated.patch)
        except Exception as exc:
            details = _format_input_error(exc)
            if _FORMAT_HINT not in details:
                details = f"{details}. {_FORMAT_HINT}"
            return f"Error: FilePatch rejected before any file changes. Format error: {details}"

        raw_summary = ", ".join(f"{spec.operation} {spec.path}" for spec in specs)
        allowed, reason = check_permission("tool", "FilePatch", raw_summary)
        if not allowed:
            return f"User Denied Execution. Reason: {reason}"

        workdir = paths.workdir().resolve()
        resolved: list[tuple[_PatchFile, Path | None]] = []
        failures: dict[int, str] = {}

        for index, spec in enumerate(specs, 1):
            if spec.parse_error is not None:
                resolved.append((spec, None))
                failures[index] = _typed_failure("Format error", spec.parse_error)
                continue
            try:
                resolved.append((spec, _resolve_patch_path(spec.path, workdir)))
            except Exception as exc:
                resolved.append((spec, None))
                failures[index] = _typed_failure("Path error", f"could not resolve path: {exc}")

        def mark_duplicate_paths(items: list[tuple[_PatchFile, Path | None]]) -> None:
            by_path: dict[Path, list[int]] = {}
            for index, (_, path) in enumerate(items, 1):
                if index in failures or path is None:
                    continue
                by_path.setdefault(path, []).append(index)
            for path, indexes in by_path.items():
                if len(indexes) > 1:
                    entries = ", ".join(
                        f"entry {index} ({specs[index - 1].operation} {specs[index - 1].path})"
                        for index in indexes
                    )
                    for index in indexes:
                        failures[index] = _typed_failure("Path conflict",
                            f"resolved path {path} is declared by {entries}; "
                            "combine all changes for this file into one file section"
                        )

        def mark_alias_paths(items: list[tuple[_PatchFile, Path | None]]) -> None:
            by_identity: dict[tuple[int, int], list[int]] = {}
            for index, (_, path) in enumerate(items, 1):
                if index in failures or path is None:
                    continue
                identity = _file_identity(path)
                if identity is not None:
                    by_identity.setdefault(identity, []).append(index)
            for _identity, indexes in by_identity.items():
                if len(indexes) < 2:
                    continue
                entries = ", ".join(
                    f"entry {index} ({specs[index - 1].operation} {specs[index - 1].path})"
                    for index in indexes
                )
                for index in indexes:
                    failures[index] = _typed_failure("Path conflict",
                        f"resolved paths refer to the same file ({entries}); "
                        "combine them into one patch entry for this file"
                    )

        mark_duplicate_paths(resolved)
        outside = [
            path for index, (_, path) in enumerate(resolved, 1)
            if index not in failures and path is not None and not path.is_relative_to(workdir)
        ]
        allowed, reason = check_paths_permission(outside, "FilePatch")
        external_permission_error = reason if not allowed else ""

        rechecked: list[tuple[_PatchFile, Path | None]] = []
        for index, (spec, original) in enumerate(resolved, 1):
            if original is None:
                rechecked.append((spec, None))
                continue
            try:
                current = _resolve_patch_path(spec.path, workdir)
            except Exception as exc:
                rechecked.append((spec, None))
                failures[index] = _typed_failure(
                    "Path error", f"could not resolve path after approval: {exc}"
                )
                continue
            if original != current:
                failures[index] = _typed_failure(
                    "Path changed", "path changed while waiting for approval; re-read and retry"
                )
            rechecked.append((spec, current))
        resolved = rechecked
        mark_duplicate_paths(resolved)

        paths_to_lock = sorted(
            {path for _, path in resolved if path is not None},
            key=lambda path: path.as_posix(),
        )
        with ExitStack() as stack:
            for path in paths_to_lock:
                stack.enter_context(GLOBAL_FILE_CONTROLLER.get_lock(path))

            mark_alias_paths(resolved)
            planned: list[tuple[int, _Plan]] = []
            for index, (spec, path) in enumerate(resolved, 1):
                if index in failures or path is None:
                    continue
                if spec.parse_error is not None:
                    failures[index] = _typed_failure("Format error", spec.parse_error)
                    continue
                if external_permission_error and not path.is_relative_to(workdir):
                    failures[index] = _typed_failure("Permission error", external_permission_error)
                    continue
                try:
                    exists = path.exists()
                    if spec.operation == "Add":
                        if exists:
                            raise ValueError(f"cannot add existing path: {spec.path}")
                        payload = (
                            "\n".join(spec.add_lines) + "\n" if spec.add_lines else ""
                        ).encode("utf-8")
                        planned.append((index, _Plan(spec, path, False, None, payload, 1)))
                        continue

                    if not exists:
                        raise ValueError(f"target not found: {spec.path}")
                    if not path.is_file():
                        raise ValueError(f"target is not a regular file: {spec.path}")

                    if spec.operation == "Delete":
                        planned.append((
                            index,
                            _Plan(spec, path, True, stat.S_IMODE(path.stat().st_mode), None, 0),
                        ))
                        continue

                    raw = path.read_bytes()
                    text, has_bom = _decode_text(raw, path)
                    payload, hunk_count = _apply_hunks(text, spec.hunks, path)
                    if has_bom:
                        payload = _UTF8_BOM + payload
                    planned.append((
                        index,
                        _Plan(
                            spec,
                            path,
                            True,
                            stat.S_IMODE(path.stat().st_mode),
                            payload,
                            hunk_count,
                        ),
                    ))
                except Exception as exc:
                    message = str(exc)
                    kind = "Hunk error" if spec.operation == "Update" and "hunk" in message else "Preflight error"
                    failures[index] = _typed_failure(kind, message)

            committed: list[tuple[int, _Plan]] = []
            for index, plan in planned:
                try:
                    _commit([plan])
                except Exception as exc:
                    message = f"commit failed: {exc}"
                    kind = "Rollback error" if "rollback failed" in message else "Commit error"
                    failures[index] = _typed_failure(kind, message)
                else:
                    committed.append((index, plan))

            report = []
            if failures:
                if committed:
                    report.append(
                        f"Error: FilePatch completed partially: {len(committed)} file(s) patched, "
                        f"{len(failures)} patch entry(s) failed."
                    )
                    report.append(
                        "Successful entries are already committed; do not resubmit them. "
                        "Retry only the failed entries below."
                    )
                else:
                    report.append(
                        f"Error: FilePatch failed: 0 file(s) patched, "
                        f"{len(failures)} patch entry(s) failed."
                    )
                    if any("rollback failed" in message for message in failures.values()):
                        report.append(
                            "The operation may have changed files; inspect the affected files "
                            "and recovery backups."
                        )
                    else:
                        report.append("No files were changed; fix the failed entries and retry.")
            else:
                report.append(f"Patched {len(committed)} file(s) atomically.")

            for _, plan in committed:
                marker = {"Update": "M", "Add": "A", "Delete": "D"}[plan.spec.operation]
                detail = f"  {marker} {plan.spec.path}"
                if plan.hunk_count:
                    detail += f" ({plan.hunk_count} hunk(s))"
                report.append(detail)
                if plan.new_bytes is not None and plan.spec.operation != "Delete":
                    content_bytes = (
                        plan.new_bytes[len(_UTF8_BOM):]
                        if plan.new_bytes.startswith(_UTF8_BOM)
                        else plan.new_bytes
                    )
                    content = content_bytes.decode("utf-8")
                    try:
                        is_valid, err_msg = validate_code(plan.spec.path, content)
                    except Exception as exc:
                        report.append(
                            f"  Warning: syntax validation failed for {plan.spec.path}: {exc}"
                        )
                        continue
                    if not is_valid:
                        report.append(f"  Warning: syntax error in {plan.spec.path}\n{err_msg}")
            if failures:
                report.append("Detected failures (retry only these entries):")
                for index, (spec, _) in enumerate(resolved, 1):
                    if index in failures:
                        report.append(
                            f"  [entry {index}] {spec.operation} {spec.path}: "
                            f"{_explain_failure(failures[index])}"
                        )
            return "\n".join(report)
    except Exception as exc:
        return (
            "Error: FilePatch aborted unexpectedly; inspect the workspace before retrying. "
            f"Details: {exc}"
        )
