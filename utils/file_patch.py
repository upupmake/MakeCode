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
_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_FILE_HEADER = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.*)$")
_HUNK_HEADER = re.compile(
    r"^@@(?: -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*)?$"
)


class FilePatch(ToolArgumentsModel):
    """Apply a strict unified-diff patch with independent per-file transactions.

    A patch may update, add, or delete one or more files. Update hunks use standard
    unified-diff context lines (one leading space), plus ``-`` removed and ``+`` added
    lines. Added-file content must use one ``+`` prefix per line.
    """

    patch: str = Field(
        ...,
        min_length=1,
        description=(
            "Complete FilePatch text beginning with '*** Begin Patch' and ending with "
            "'*** End Patch'. The patch may update, add, or delete one or more files. "
            "Update hunks use one-space-prefixed context lines, '-' removals, "
            "and '+' additions. Pure insertions use a zero-old-line hunk such as "
            "'@@ -0,0 +1,1 @@'; a bare '@@' pure insertion is allowed only for an empty file. "
            "Add File content uses one '+' prefix per line. "
            "Each actual file may appear only once; each file is committed independently."
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
    return ValueError(f"FilePatch parse error: {message}")


def _is_file_boundary(line: str) -> bool:
    return line == _PATCH_END or _FILE_HEADER.match(line) is not None


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
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != _PATCH_BEGIN:
        raise _patch_error(f"patch must start with '{_PATCH_BEGIN}'")
    if len(lines) < 2 or lines[-1] != _PATCH_END:
        raise _patch_error(f"patch must end with '{_PATCH_END}'")

    result: list[_PatchFile] = []
    index = 1
    end = len(lines) - 1
    while index < end:
        match = _FILE_HEADER.match(lines[index])
        if not match:
            raise _patch_error(f"expected a file header at line {index + 1}")
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
                        f"added file {path} must contain only '+' lines "
                        f"(line {index + 1})"
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
            changed = False
            parse_error = None
            for line_number, line in body:
                if line.startswith("+"):
                    new_lines.append(line[1:])
                    changed = True
                elif line.startswith("-"):
                    old_lines.append(line[1:])
                    changed = True
                elif line.startswith(" "):
                    context = line[1:]
                    old_lines.append(context)
                    new_lines.append(context)
                else:
                    parse_error = (
                        f"update hunk for {path} contains an invalid line prefix "
                        f"at line {line_number}; context lines must start with a space"
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
            if not changed or old_lines == new_lines:
                parse_error = f"update hunk for {path} does not change anything"
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
        result.append(_PatchFile("Update", path, hunks=tuple(hunks), parse_error=parse_error))

    if not result:
        raise _patch_error("patch contains no file operations")
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
    for number, hunk in enumerate(hunks, 1):
        try:
            span = _locate_hunk(lines, hunk)
        except ValueError as exc:
            raise ValueError(f"{path}: hunk {number} {exc}") from exc
        if span is None:
            raise ValueError(f"{path}: hunk {number} context was not found")
        start, end = span
        if any(
            (start < other_end and other_start < end)
            or (start == other_start and start == end == other_start == other_end)
            for other_start, other_end, _ in located
        ):
            raise ValueError(f"{path}: hunk {number} overlaps another hunk")
        located.append((start, end, hunk.new_lines))

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
            return f"Error: Invalid arguments provided to FilePatch. {exc}"

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
                failures[index] = spec.parse_error
                continue
            try:
                resolved.append((spec, _resolve_patch_path(spec.path, workdir)))
            except Exception as exc:
                resolved.append((spec, None))
                failures[index] = f"could not resolve path: {exc}"

        def mark_duplicate_paths(items: list[tuple[_PatchFile, Path | None]]) -> None:
            by_path: dict[Path, list[int]] = {}
            for index, (_, path) in enumerate(items, 1):
                if path is None:
                    continue
                by_path.setdefault(path, []).append(index)
            for path, indexes in by_path.items():
                if len(indexes) > 1:
                    for index in indexes:
                        failures[index] = (
                            f"resolved path {path} is used by multiple patch entries "
                            f"({', '.join(str(item) for item in indexes)})"
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
                labels = ", ".join(specs[index - 1].path for index in indexes)
                for index in indexes:
                    failures[index] = (
                        f"resolved paths refer to the same file ({labels}); "
                        "use one patch entry for this file"
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
                failures[index] = f"could not resolve path after approval: {exc}"
                continue
            if original != current:
                failures[index] = "path changed while waiting for approval; re-read and retry"
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
                    failures[index] = spec.parse_error
                    continue
                if external_permission_error and not path.is_relative_to(workdir):
                    failures[index] = external_permission_error
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
                    failures[index] = str(exc)

            committed: list[tuple[int, _Plan]] = []
            for index, plan in planned:
                try:
                    _commit([plan])
                except Exception as exc:
                    failures[index] = str(exc)
                else:
                    committed.append((index, plan))

            report = []
            if failures:
                report.append(
                    f"Error: FilePatch completed partially: {len(committed)} file(s) patched, "
                    f"{len(failures)} file(s) failed."
                )
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
                report.append("Failures:")
                for index, (spec, _) in enumerate(resolved, 1):
                    if index in failures:
                        report.append(f"  {spec.path}: {failures[index]}")
            return "\n".join(report)
    except Exception as exc:
        return f"Error: FilePatch failed; no changes were saved. {exc}"
