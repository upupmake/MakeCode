import datetime
import difflib
import json
import locale
import os
import re
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator, field_validator

from init import log_error_traceback, STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE
from system.ts_validator import validate_code
from utils import paths
from utils.file_access import GLOBAL_FILE_CONTROLLER
from utils.hitl import check_permission, check_path_permission
from utils.text_tokens import truncate_text_by_tokens
from utils.tool_validation import ToolArgumentsModel, build_tool_definitions, merge_tool_model_registries


_OUTPUT_TRUNCATION_MARKER_PATTERN = re.compile(
    r"(?:\n\n)?\[\.\.\.此处省略 \d+ tokens\.\.\.\](?:\n\n)?"
)
_OUTPUT_TRUNCATION_MAX_TOKENS = 8000
_OUTPUT_TRUNCATION_EDGE_TOKENS = 4000
_UTF8_BOM = b"\xef\xbb\xbf"


def _workdir() -> Path:
    return paths.workdir()


def safe_path(p: str, tool_name: str = "") -> Path:
    workdir = _workdir()
    path = (workdir / p).resolve()
    if not path.is_relative_to(workdir):
        allowed, reason = check_path_permission(path, tool_name)
        if not allowed:
            raise ValueError(f"Path escapes workspace: {p}. {reason}")
    return path


def _is_binary_file(filepath: Path) -> bool:
    """Check if a file is likely binary by inspecting its first 1024 bytes for a null byte."""
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
            if b"\0" in chunk:
                return True
        return False
    except Exception as exc:
        log_error_traceback("ContentSearch binary file check", exc)
        return True


def _normalized_search_path(path: Path) -> str:
    return path.resolve().as_posix()


def _is_excluded_dir_path(rel_path: Path, is_dir: bool, exclude_dirs: set[str]) -> bool:
    dir_parts = rel_path.parts if is_dir else rel_path.parts[:-1]
    return any(part.startswith(".") or part in exclude_dirs for part in dir_parts)


def _align_truncation_to_lines(text: str) -> str:
    """Drop the partial lines that a token-level cut leaves around the marker.

    Token slicing can end the head mid-line and start the tail mid-line, which
    produces fragments that look like whole numbered lines but are not present in
    the file. Copying such a fragment into FileEdit can never match.
    """
    match = _OUTPUT_TRUNCATION_MARKER_PATTERN.search(text)
    if match is None:
        return text

    head = text[:match.start()]
    tail = text[match.end():]

    head_cut = head.rfind("\n")
    if head_cut > 0:
        head = head[:head_cut]

    tail_cut = tail.find("\n")
    if 0 <= tail_cut < len(tail) - 1:
        tail = tail[tail_cut + 1:]

    return head + match.group(0) + tail


def truncate_output(
        text: str,
        max_tokens: int = _OUTPUT_TRUNCATION_MAX_TOKENS,
        *,
        line_aligned: bool = False,
) -> str:
    result = truncate_text_by_tokens(
        text,
        max_tokens=max_tokens,
        edge_tokens=_OUTPUT_TRUNCATION_EDGE_TOKENS,
        marker="\n\n[...此处省略 {omitted_tokens} tokens...]\n\n",
        existing_marker_pattern=_OUTPUT_TRUNCATION_MARKER_PATTERN,
    )
    if not line_aligned or result is text:
        return result
    return _align_truncation_to_lines(result)


def sanitize_title(title: str) -> str | None:
    """Sanitize a title string for safe use in filenames.

    Scans from the end of the string backwards. When an invalid character is
    found, everything before it (inclusive) is discarded — only the valid
    suffix is kept.  This handles models that prepend XML-like tags such as
    ``<thinking>...</thinking>正文`` by stripping the prefix automatically.

    Allowed characters: English letters, digits, Chinese (CJK), spaces, dots.
    Returns None if the result is empty after processing.
    """
    if not title:
        return None
    title = title.strip()
    if not title:
        return None

    # Allowed pattern: English letters, digits, CJK Unified Ideographs, space, dot, hyphen
    _allowed = re.compile(r'[a-zA-Z0-9\u4e00-\u9fff .-]')

    # Scan from the end to find the last invalid character
    last_invalid = -1
    for i in range(len(title) - 1, -1, -1):
        if not _allowed.match(title[i]):
            last_invalid = i
            break

    # Keep the valid suffix after the last invalid character
    if last_invalid >= 0:
        title = title[last_invalid + 1:]

    return title if title else None


def _resolve_startup_terminal_type() -> str:
    if STARTUP_TERMINAL_TYPE:
        return STARTUP_TERMINAL_TYPE
    raise FileNotFoundError("No startup terminal detected.")


_STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


class RunTerminalCommand(ToolArgumentsModel):
    """
    Execute a terminal command in non-interactive mode.

    EXECUTION POLICY:
    - Commands that may be destructive, require elevated privileges, or perform network diagnostics go through HITL confirmation.
    - Interactive and full-screen commands are unsupported by the non-interactive terminal and may time out.

    PREFERRED APPROACH:
    - For file read/write/edit: Use File tools (FileRead/FileCreate/FileEdit)
    - For file content search: Use ContentSearch (not grep, rg, findstr)
    - For file path regex search: Use FileSearch (not find, ls, dir)
    - Use this tool ONLY for: builds, tests, git, package management, system info

    TIMEOUT: 120 seconds hard limit. Running commands can be cancelled from the TUI.
    Results always include a status and exit code.
    """

    command: str = Field(
        ...,
        description=(
            "The terminal command string to execute in non-interactive mode. "
            f"Runtime terminal is fixed at startup: '{_STARTUP_TERMINAL_LABEL}' "
            f"(source={STARTUP_TERMINAL_SOURCE}). "
            "This tool only accepts command; terminal type is not configurable per call. "
            "Execution is bound to the workspace root directory and has a hard timeout of 120 seconds. "
            "Do not use this tool for normal workspace file read/write/edit operations."
        ),
    )


def _build_terminal_argv(terminal_type: str, command: str) -> list[str]:
    shell = os.path.basename(terminal_type).lower()
    if shell == "powershell":
        return [terminal_type, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "pwsh":
        return [terminal_type, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return [terminal_type, "/d", "/s", "/c", command]
    if shell in {"bash", "sh", "zsh"}:
        return [terminal_type, "-lc", command]
    raise ValueError(f"Unsupported terminal type: {terminal_type}")


_TERMINAL_COMMAND_TIMEOUT = 120
_TERMINAL_POLL_INTERVAL = 0.1


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            if process.poll() is None:
                process.terminate()

    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                process.kill()
        process.wait(timeout=2)


def _decode_terminal_output(raw_output: bytes) -> str:
    # 动态解码策略：优先 UTF-8，依次尝试多种编码
    out = None
    encodings = ['utf-8', 'gbk', 'gb2312', locale.getpreferredencoding()]
    for enc in encodings:
        try:
            out = raw_output.decode(enc).strip()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if out is None:
        out = raw_output.decode('utf-8', errors='replace').strip()
    return truncate_output(out)


def _format_terminal_result(
        status: str,
        return_code: int | None,
        output: str,
        terminal_meta: str,
) -> str:
    code_text = str(return_code) if return_code is not None else "none"
    if not output:
        output = "(no output)"
    return (
        f"Status: {status}\n"
        f"Exit code: {code_text}\n"
        f"Terminal: {terminal_meta}\n\n"
        f"{output}"
    )


def run_terminal_command(command: str) -> str:
    parts = command.strip().split()
    if len(parts) > 1:
        action_name = " ".join(parts[:2])
    else:
        action_name = parts[0] if parts else "unknown"

    allowed, reason = check_permission("cmd", action_name, command)
    if not allowed:
        return f"User Denied Execution. Reason: {reason}"

    process = None
    terminal_cancel_started = False
    try:
        resolved_terminal = _resolve_startup_terminal_type()
        terminal_meta = f"{resolved_terminal}, source={STARTUP_TERMINAL_SOURCE}"
        popen_kwargs = {
            "cwd": _workdir(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        from system.stream_cancel import (
            is_terminal_cancelled,
            start_terminal_command,
        )

        start_terminal_command()
        terminal_cancel_started = True
        process = subprocess.Popen(
            _build_terminal_argv(resolved_terminal, command),
            **popen_kwargs,
        )
        deadline = time.monotonic() + _TERMINAL_COMMAND_TIMEOUT
        timed_out = False
        cancelled = False
        stdout = b""
        stderr = b""
        while True:
            if is_terminal_cancelled():
                cancelled = True
                _terminate_process_tree(process)
                stdout, stderr = process.communicate()
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_tree(process)
                stdout, stderr = process.communicate()
                break

            try:
                stdout, stderr = process.communicate(
                    timeout=min(_TERMINAL_POLL_INTERVAL, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue

        output = _decode_terminal_output(stdout + stderr)
        if cancelled:
            return _format_terminal_result("cancelled", process.returncode, output, terminal_meta)
        if timed_out:
            return _format_terminal_result("timed_out", process.returncode, output, terminal_meta)
        status = "success" if process.returncode == 0 else "failed"
        return _format_terminal_result(status, process.returncode, output, terminal_meta)

    except FileNotFoundError as exc:
        log_error_traceback("RunTerminalCommand terminal missing", exc)
        return (
            "Status: startup_error\n"
            "Exit code: none\n"
            "Error: No supported terminal executable found. "
            f"startup_terminal={STARTUP_TERMINAL_TYPE or 'unavailable'} "
            f"(source={STARTUP_TERMINAL_SOURCE})."
        )
    except Exception as e:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process)
            process.communicate()
        log_error_traceback("RunTerminalCommand execution", e)
        return f"Status: execution_error\nExit code: none\nError executing command: {e}"
    finally:
        if terminal_cancel_started:
            from system.stream_cancel import stop_terminal_command
            stop_terminal_command()


class ReadBlock(ToolArgumentsModel):
    """A block specifying a line range to read from a file."""
    start: int = Field(
        ...,
        description="Start line number (1-indexed) to read.",
    )
    end: int = Field(
        ...,
        description="End line number (1-indexed) to read. Inclusive.",
    )

    @model_validator(mode="before")
    @classmethod
    def parse_stringified_block(cls, data: Any) -> Any:
        if isinstance(data, str):
            try:
                data = data.strip()
                if not data:
                    return data
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return data


class FileRead(ToolArgumentsModel):
    """
    Read contents of a file. Reads only the specified line ranges.

    LINE NUMBERING:
    - Line numbers are 1-indexed (first line is 1, not 0)
    - 'end' is INCLUSIVE (e.g., {start:1, end:100} reads lines 1-100)

    OUTPUT FORMAT (same convention as `grep -n`):
    - Each line is rendered as `<line number>:<verbatim line content>`. Everything after
      the FIRST colon is the exact file content, including its indentation.
    - The `<line number>:` prefix is display-only and is NOT part of the file. Never copy
      it into FileEdit's search_content or replace_content.
    - Non-adjacent regions are separated by a `@@ <a>-<b> skipped @@` marker. Lines on
      opposite sides of that marker are NOT adjacent in the file.

    PERFORMANCE GUIDELINES:
    1. Provide specific regions when possible to reduce context usage.
    2. PREFER providing MULTIPLE regions in a SINGLE call rather than multiple separate calls.
       Example: regions=[{"start":1,"end":150},{"start":300,"end":450}]
    3. Overlapping or adjacent regions will be automatically merged for efficiency.

    WORKFLOW: Before calling FileRead, estimate all line ranges you need, then provide them all at once.
    """

    path: str = Field(
        ...,
        description="Path to the file to read, relative to workspace by default. Paths outside workspace require user permission."
    )
    regions: list[ReadBlock] = Field(
        ...,
        min_length=1,
        description="List of line ranges to read. Must contain at least one region. PREFER: Provide MULTIPLE regions in a SINGLE call rather than multiple separate calls. Example: regions=[{start:1,end:100},{start:200,end:300}]"
    )

    @field_validator("regions", mode="before")
    @classmethod
    def parse_stringified_regions(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("regions must be a non-empty list")
            if v.lower() in {"none", "null"}:
                raise ValueError("regions must be a non-empty list")
            if v == "[]":
                raise ValueError("regions must be a non-empty list")
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        if isinstance(v, list) and len(v) == 0:
            raise ValueError("regions must be a non-empty list")
        return v


def merge_intervals(intervals: list[list[int]]) -> list[list[int]]:
    """
    合并重叠或相邻的区间

    算法：排序 + 贪心合并
    时间复杂度：O(n log n)
    空间复杂度：O(n)

    Args:
        intervals: 区间列表，每个区间为 [start, end]

    Returns:
        合并后的区间列表
    """
    if not intervals:
        return []

    # 按起始位置排序
    intervals.sort(key=lambda x: x[0])

    # 合并重叠/相邻区间
    merged = [intervals[0]]
    for curr in intervals[1:]:
        prev = merged[-1]
        if curr[0] <= prev[1] + 1:  # 重叠或相邻
            prev[1] = max(prev[1], curr[1])
        else:
            merged.append(curr)

    return merged


def file_read(
        path: str, regions: list[dict]
) -> str:
    try:
        try:
            validated = FileRead.model_validate({"path": path, "regions": regions})
            path = validated.path
            regions = validated.regions
        except Exception as exc:
            return f"Error: Invalid arguments provided to FileRead. {exc}"

        fp = safe_path(path, "FileRead")
        file_lock = GLOBAL_FILE_CONTROLLER.get_lock(fp)
        with file_lock:
            if not fp.exists():
                return f"Error: File {path} not found."

            # 检查是否为二进制文件，防止读取二进制文件
            if _is_binary_file(fp):
                return f"Error: File {path} appears to be a binary file and cannot be read as text."

            # 显式指定 utf-8 编码，并使用 replace 处理无法解码的字节，防止读取崩溃
            # utf-8-sig 让 BOM 不出现在第 1 行内容里，与 FileEdit 的快照保持一致
            text = fp.read_text(encoding="utf-8-sig", errors="replace")

            lines = text.splitlines()
            total_lines = len(lines)

        # 读取指定区域
        # 收集所有有效区间（regions已经是经过model_validate的ReadBlock列表）
        intervals = []
        for region in regions:
            s = region.start
            e = region.end

            # 边界约束
            s = max(1, s)
            e = min(total_lines, e)

            if s <= e:
                intervals.append([s, e])

        if not intervals:
            return f"File: {path}, Total lines: {total_lines}\n(No valid lines to read)"

        # 合并区间
        merged = merge_intervals(intervals)

        # 格式化输出：不连续的 region 之间插入断点标记，避免模型跨空洞拼接行
        formatted_lines = []
        previous_end = None
        for s, e in merged:
            if previous_end is not None:
                formatted_lines.append(f"@@ {previous_end + 1}-{s - 1} skipped @@")
            formatted_lines.extend(f"{n}:{lines[n - 1]}" for n in range(s, e + 1))
            previous_end = e

        if not formatted_lines:
            return f"File: {path}, Total lines: {total_lines}\n(No valid lines to read)"

        return truncate_output(
            f"File: {path}, Total lines: {total_lines}\n" + "\n".join(formatted_lines),
            line_aligned=True,
        )
    except Exception as e:
        log_error_traceback("FileRead execution", e)
        return f"Error: {e}"


class FileCreate(ToolArgumentsModel):
    """
    Create and write a NEW file, or overwrite a completely empty file.

    CRITICAL REQUIREMENTS:
    1. Use this tool ONLY when the target file does NOT exist yet, or is empty.
    2. If the file already exists and has content, use FileEdit.
    3. Parent directories will be automatically created if they don't exist.

    ENCODING: Files are written in UTF-8 encoding.
    """

    path: str = Field(
        ...,
        description="Path to the file to write, relative to workspace by default. Paths outside workspace require user permission."
    )
    content: str = Field(..., description="The content to write to the file.")


def file_create(path: str, content: str) -> str:
    try:
        try:
            validated = FileCreate.model_validate({"path": path, "content": content})
            path = validated.path
            content = validated.content
        except Exception as exc:
            return f"Error: Invalid arguments provided to FileCreate. {exc}"

        allowed, reason = check_permission("tool", "FileCreate", path)
        if not allowed:
            return f"User Denied Execution. Reason: {reason}"

        fp = safe_path(path, "FileCreate")
        file_lock = GLOBAL_FILE_CONTROLLER.get_lock(fp)
        with file_lock:
            if fp.exists() and fp.stat().st_size > 0:
                # 进一步检查是否全是空白字符
                existing_content = fp.read_text(
                    encoding="utf-8", errors="ignore"
                ).strip()
                if existing_content:
                    return (
                        f"Error: File {path} already exists and is not empty. "
                        "FileCreate is only for creating new files or writing to empty ones. "
                        "For modifications, use FileEdit."
                    )
            fp.parent.mkdir(parents=True, exist_ok=True)

            # 强制使用 utf-8 写入，保持跨平台一致性
            fp.write_text(content, encoding="utf-8")

            is_valid, err_msg = validate_code(path, content)
            if not is_valid:
                return f"Success with Warning: 文件已写入，但检测到语法错误(Syntax error)\n\n{err_msg}"

            return f"Created {path}: {len(content.splitlines())} lines written"
    except Exception as e:
        log_error_traceback("FileCreate execution", e)
        return f"Error: {e}"


class EditBlock(ToolArgumentsModel):
    """
    Represents a single whole-line search-and-replace operation.
    `search_content` is matched against the file as it was read, and the matched lines
    are replaced by `replace_content`.
    """

    search_content: str = Field(
        ...,
        description=(
            "The EXACT original lines to be replaced, copied verbatim from the file. "
            "CRITICAL RULES: "
            "1. You MUST include sufficient context (2-3 unchanged lines before and after the target) "
            "so that the block matches exactly one location in the file. "
            "2. Whole lines only, with the original indentation. "
            "3. Never include FileRead's or ContentSearch's '<line number>:' prefix, never wrap the "
            "block in ``` fences, and never use '...' to skip lines."
        ),
    )
    replace_content: str = Field(
        ...,
        description=(
            "The NEW lines that replace `search_content`. "
            "CRITICAL RULES: "
            "1. If you included unchanged context lines in `search_content`, you MUST duplicate them exactly here, otherwise they will be permanently deleted! "
            "2. Ensure absolute indentation spaces are perfectly maintained. "
            "3. Use an empty string to delete the matched lines."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def parse_stringified_block(cls, data: Any) -> Any:
        if isinstance(data, str):
            try:
                data = data.strip()
                if not data:
                    return data
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return data


class FileEdit(ToolArgumentsModel):
    """
    Replace whole-line blocks in an existing file.

    HOW TO USE PERFECTLY:
    1. Provide the exact lines to change in `search_content`, adding 2-3 lines of unchanged
       code above and below as context.
    2. Write the modified version into `replace_content`, making sure to KEEP the unchanged
       context lines!
    3. Put EVERY edit to the same file into ONE call, so they either all apply or none do.
       Do not issue two FileEdit calls for the same file in the same turn.

    MATCHING RULES:
    - Every block is first matched against the file as it was read, so blocks copied from one
      FileRead result do not interfere with each other.
    - A block that cannot be placed yet is retried against the text produced by the blocks
      already applied, so chaining one block onto another block's output still works.
    - Matching is whole-line: exact lines first, then ignoring trailing whitespace and a
      uniform indentation shift (the replacement is re-indented by the same amount).
    - Each block MUST match exactly one location. If any block never resolves, nothing is
      written and every problem is reported at once.

    WARNINGS:
    - Never invent code or guess indentation.
    - Never use `...` to skip code.
    - Never copy the `<line number>:` prefix from FileRead or ContentSearch output.
    - If your search block is not unique, include more context lines.
    """

    path: str = Field(
        ...,
        description="Path to the file you want to edit."
    )
    edits: list[EditBlock] = Field(
        ...,
        min_length=1,
        description=(
            "A non-empty list of edits. Each edit has: search_content (exact lines to find) and "
            "replace_content (new lines). Blocks are matched against the file as it was read; a "
            "block that cannot be placed yet is retried against the text earlier blocks produced. "
            "All blocks are applied atomically."
        ),
    )

    @field_validator("edits", mode="before")
    @classmethod
    def parse_stringified_edits(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                v = v.strip()
                if not v:
                    return v
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return v


_LINE_NUMBER_PREFIX_PATTERN = re.compile(r"^(\d+)[:\-](.*)$")
_DIAGNOSTIC_DIFF_MAX_LINES = 24
_AMBIGUOUS_LOCATIONS_SHOWN = 5
_FILE_EDIT_SNAPSHOT_NOTE = (
    "Blocks are matched against the file as it was read; a block that cannot be placed yet is "
    "retried against the text produced by the blocks already applied. Nothing is written "
    "unless every block resolves, so re-read the file and resubmit all blocks together."
)


def _split_edit_lines(text: str) -> list[str]:
    """Split an edit payload into whole lines, tolerating CRLF and a trailing newline."""
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _find_exact_spans(file_lines: list[str], search_lines: list[str]) -> list[tuple[int, int]]:
    """Locate every whole-line exact occurrence of the search block."""
    size = len(search_lines)
    if size == 0 or size > len(file_lines):
        return []
    first = search_lines[0]
    return [
        (index, index + size)
        for index in range(len(file_lines) - size + 1)
        if file_lines[index] == first and file_lines[index: index + size] == search_lines
    ]


def _match_indent_shift(window: list[str], search_lines: list[str]) -> tuple[str, bool] | None:
    """Match a window ignoring trailing whitespace and a uniform indentation shift.

    Returns ``(delta, search_is_deeper)`` where ``delta`` is the whitespace separating the
    file's indentation from the search block's indentation, or None when the window does
    not correspond to the search block.
    """
    shift: tuple[str, bool] | None = None
    for file_line, search_line in zip(window, search_lines):
        if file_line.strip() != search_line.strip():
            return None
        if not search_line.strip():
            continue

        file_indent = _leading_whitespace(file_line)
        search_indent = _leading_whitespace(search_line)
        if file_indent == search_indent:
            current = ("", False)
        elif file_indent.endswith(search_indent):
            current = (file_indent[: len(file_indent) - len(search_indent)], False)
        elif search_indent.endswith(file_indent):
            current = (search_indent[: len(search_indent) - len(file_indent)], True)
        else:
            return None

        if shift is None:
            shift = current
        elif shift != current:
            return None
    return ("", False) if shift is None else shift


def _find_shifted_spans(
        file_lines: list[str], search_lines: list[str]
) -> list[tuple[int, int, str, bool]]:
    size = len(search_lines)
    if size == 0 or size > len(file_lines):
        return []
    first = search_lines[0].strip()
    spans = []
    for index in range(len(file_lines) - size + 1):
        if file_lines[index].strip() != first:
            continue
        shift = _match_indent_shift(file_lines[index: index + size], search_lines)
        if shift is not None:
            spans.append((index, index + size, shift[0], shift[1]))
    return spans


def _reindent(lines: list[str], delta: str, search_is_deeper: bool) -> list[str]:
    if not delta:
        return list(lines)
    if search_is_deeper:
        return [
            line[len(delta):] if line.startswith(delta) else line
            for line in lines
        ]
    return [delta + line if line.strip() else line for line in lines]


def _apply_located(file_lines: list[str], located: list[dict]) -> list[str]:
    """Splice replacements bottom-up so earlier spans keep their original indices."""
    result = list(file_lines)
    for item in sorted(located, key=lambda entry: entry["start"], reverse=True):
        result[item["start"]: item["end"]] = item["replace_lines"]
    return result


def _search_anchor(
        file_lines: list[str], search_lines: list[str]
) -> tuple[str, int] | None:
    """Pick the search line to anchor diagnostics on, and its offset in the block.

    Prefers the rarest line that exists verbatim in the file. When no line exists
    verbatim, falls back to the file line closest to the block's most distinctive line so
    the model still gets a location instead of a bare "not found".
    """
    counts = Counter(line.strip() for line in file_lines)
    exact = sorted(
        (counts[line.strip()], offset)
        for offset, line in enumerate(search_lines)
        if line.strip() and counts[line.strip()]
    )
    if exact:
        _, offset = exact[0]
        return search_lines[offset].strip(), offset

    probe_offset = max(
        (offset for offset, line in enumerate(search_lines) if line.strip()),
        key=lambda offset: len(search_lines[offset].strip()),
        default=None,
    )
    if probe_offset is None:
        return None
    close = difflib.get_close_matches(
        search_lines[probe_offset].strip(),
        [line.strip() for line in file_lines if line.strip()],
        n=1,
        cutoff=0.6,
    )
    return (close[0], probe_offset) if close else None


def _closest_region(
        file_lines: list[str], search_lines: list[str]
) -> tuple[int, int, float] | None:
    """Find the file region most similar to the search block, anchored on one line.

    Anchoring keeps this diagnostic cheap: only windows around a matching line are
    scored, instead of every window in the file.
    """
    anchor_info = _search_anchor(file_lines, search_lines)
    if anchor_info is None:
        return None

    anchor, anchor_offset = anchor_info
    size = len(search_lines)
    search_text = "\n".join(line.strip() for line in search_lines)

    best = None
    for index, line in enumerate(file_lines):
        if line.strip() != anchor:
            continue
        start = max(0, index - anchor_offset)
        end = min(len(file_lines), start + size)
        window_text = "\n".join(item.strip() for item in file_lines[start:end])
        ratio = difflib.SequenceMatcher(None, window_text, search_text).ratio()
        if best is None or ratio > best[2]:
            best = (start, end, ratio)
    return best


def _region_diff(
        path: str, file_lines: list[str], start: int, end: int, search_lines: list[str]
) -> list[str]:
    diff = list(difflib.unified_diff(
        file_lines[start:end],
        search_lines,
        fromfile=f"{path} lines {start + 1}-{end}",
        tofile="your search_content",
        lineterm="",
        n=1,
    ))
    if len(diff) > _DIAGNOSTIC_DIFF_MAX_LINES:
        diff = diff[:_DIAGNOSTIC_DIFF_MAX_LINES] + ["... (diff truncated)"]
    return diff


def _numbered_prefix_hint(search_lines: list[str], file_lines: list[str]) -> str:
    """Detect a search block that was copied straight out of numbered tool output.

    Every line must carry an increasing `<line number>:` prefix whose payload equals the
    real file line, which makes false positives on genuine code effectively impossible.
    """
    if len(search_lines) < 2:
        return ""

    previous_number = None
    for line in search_lines:
        match = _LINE_NUMBER_PREFIX_PATTERN.match(line)
        if not match:
            return ""
        number, payload = int(match.group(1)), match.group(2)
        if previous_number is not None and number <= previous_number:
            return ""
        previous_number = number
        if not 1 <= number <= len(file_lines):
            return ""
        actual = file_lines[number - 1]
        if payload != actual and payload != f" {actual}":
            return ""

    return (
        "Every line of this block carries a FileRead/ContentSearch '<line number>:' prefix. "
        "That prefix is display-only and is not part of the file. Resubmit the block with "
        "the prefixes removed."
    )


def _describe_missing_block(
        index: int, path: str, file_lines: list[str], search_lines: list[str]
) -> str:
    lines = [f"Block {index}: search_content not found in {path}."]

    prefix_hint = _numbered_prefix_hint(search_lines, file_lines)
    if prefix_hint:
        lines.append(f"  {prefix_hint}")
        return "\n".join(lines)

    closest = _closest_region(file_lines, search_lines)
    if closest is None:
        lines.append(
            "  No line of this search_content exists anywhere in the file. "
            "Re-read the file before retrying."
        )
        return "\n".join(lines)

    start, end, ratio = closest
    lines.append(f"  Closest region: lines {start + 1}-{end} (similarity {ratio:.2f})")
    lines.extend(f"  {item}" for item in _region_diff(path, file_lines, start, end, search_lines))
    return "\n".join(lines)


def _describe_ambiguous_block(index: int, spans: list[tuple[int, int, str, bool]]) -> str:
    shown = spans[:_AMBIGUOUS_LOCATIONS_SHOWN]
    locations = ", ".join(f"lines {start + 1}-{end}" for start, end, _, _ in shown)
    if len(spans) > len(shown):
        locations += f", +{len(spans) - len(shown)} more"
    return (
        f"Block {index}: search_content matches {len(spans)} locations ({locations}). "
        "Add more surrounding context so it matches exactly one location."
    )


def _locate_block(
        file_lines: list[str], search_lines: list[str]
) -> tuple[list[tuple[int, int, str, bool]], str]:
    """Return every span the block matches, preferring exact lines over an indent shift."""
    spans = [
        (start, end, "", False)
        for start, end in _find_exact_spans(file_lines, search_lines)
    ]
    if spans:
        return spans, "exact"
    return _find_shifted_spans(file_lines, search_lines), "reindented"


def _describe_unresolved_block(
        index: int,
        path: str,
        current_lines: list[str],
        search_lines: list[str],
        snapshot_spans: list[tuple[int, int, str, bool]],
        applied: list[dict],
) -> str:
    spans, _ = _locate_block(current_lines, search_lines)
    if len(spans) > 1:
        return _describe_ambiguous_block(index, spans)

    if len(snapshot_spans) == 1 and applied:
        start, end = snapshot_spans[0][0], snapshot_spans[0][1]
        blockers = sorted(
            item["index"] for item in applied
            if item["stage"] == 1 and item["start"] < end and start < item["end"]
        )
        culprit = (
            " Block " + ", ".join(f"#{i}" for i in blockers) + " already rewrote those lines;"
            " merge the overlapping edits into a single block."
            if blockers else
            " An earlier block in this call changed those lines."
        )
        return (
            f"Block {index}: search_content matched the file as it was read "
            f"(lines {start + 1}-{end}) but no longer matches after earlier blocks were "
            f"applied.{culprit}"
        )

    return _describe_missing_block(index, path, current_lines, search_lines)


def _detect_newline(text: str) -> str:
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    cr_count = text.count("\r") - crlf_count
    if crlf_count and crlf_count >= max(lf_count, cr_count):
        return "\r\n"
    if cr_count > lf_count:
        return "\r"
    return "\n"


def _write_file_atomically(fp: Path, payload: bytes, mode: int) -> None:
    temp_path = fp.with_name(f"{fp.name}.makecode.tmp")
    try:
        temp_path.write_bytes(payload)
        os.chmod(temp_path, mode & 0o7777)
        os.replace(temp_path, fp)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def file_edit(path: str, edits: Any) -> str:
    try:
        try:
            validated = FileEdit.model_validate({"path": path, "edits": edits})
            path = validated.path
            parsed_blocks = validated.edits
        except Exception as exc:
            return f"Error: Invalid arguments provided to FileEdit. {exc}"

        allowed, reason = check_permission("tool", "FileEdit", path)
        if not allowed:
            return f"User Denied Execution. Reason: {reason}"

        fp = safe_path(path, "FileEdit")
        file_lock = GLOBAL_FILE_CONTROLLER.get_lock(fp)
        with file_lock:
            if not fp.exists():
                return f"Error: File {path} not found."

            raw = fp.read_bytes()
            if b"\0" in raw[:1024]:
                return (
                    f"Error: File {path} appears to be a binary file and cannot be edited as text."
                )

            original_mode = fp.stat().st_mode
            has_bom = raw.startswith(_UTF8_BOM)
            try:
                text = (raw[len(_UTF8_BOM):] if has_bom else raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                return (
                    f"Error: File {path} is not valid UTF-8 ({exc.reason} at byte {exc.start}). "
                    "FileEdit will not rewrite it because that would corrupt the undecodable bytes."
                )

            newline = _detect_newline(text)
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            trailing_newline = normalized.endswith("\n")
            if not normalized:
                file_lines = []
            else:
                file_lines = normalized.split("\n")
                if trailing_newline:
                    file_lines.pop()

            # Locate and apply in stages. Every block is first matched against the file as it
            # was read; a block that cannot be placed yet is deferred and retried against the
            # text the applied blocks produced. That keeps chained edits working while still
            # refusing to let a stale block silently undo an earlier one.
            problems: list[tuple[int, str]] = []
            pending: list[dict] = []
            for index, block in enumerate(parsed_blocks, 1):
                search_lines = _split_edit_lines(block.search_content)
                if not any(line.strip() for line in search_lines):
                    problems.append(
                        (index, f"Block {index}: search_content is empty or only whitespace.")
                    )
                    continue
                pending.append({
                    "index": index,
                    "search_lines": search_lines,
                    "replace_lines": _split_edit_lines(block.replace_content),
                })

            snapshot_spans = {
                item["index"]: _locate_block(file_lines, item["search_lines"])[0]
                for item in pending
            }

            current = list(file_lines)
            applied: list[dict] = []
            stage = 0
            while pending:
                stage += 1
                resolved, unmatched = [], []
                for item in pending:
                    spans, tier = _locate_block(current, item["search_lines"])
                    if len(spans) == 1:
                        resolved.append((item, spans[0], tier))
                    else:
                        unmatched.append(item)

                accepted, deferred = [], []
                for item, span, tier in resolved:
                    start, end, delta, search_is_deeper = span
                    if any(start < other["end"] and other["start"] < end for other in accepted):
                        deferred.append(item)
                        continue
                    accepted.append({
                        "index": item["index"],
                        "start": start,
                        "end": end,
                        "tier": tier,
                        "delta": delta,
                        "search_is_deeper": search_is_deeper,
                        "stage": stage,
                        "replace_lines": _reindent(item["replace_lines"], delta, search_is_deeper),
                    })

                if not accepted:
                    pending = unmatched
                    break

                current = _apply_located(current, accepted)
                applied.extend(accepted)
                pending = sorted(unmatched + deferred, key=lambda item: item["index"])

            for item in pending:
                problems.append((
                    item["index"],
                    _describe_unresolved_block(
                        item["index"], path, current, item["search_lines"],
                        snapshot_spans[item["index"]], applied,
                    ),
                ))

            if problems:
                report = [
                    f"Error: FileEdit rejected {len(parsed_blocks)} edit block(s) for {path}: "
                    f"{len(problems)} problem(s) found. No changes were saved."
                ]
                for _, message in sorted(problems, key=lambda item: item[0]):
                    report.append("")
                    report.append(message)
                report.append("")
                report.append(_FILE_EDIT_SNAPSHOT_NOTE)
                return "\n".join(report)

            # Commit atomically, preserving the file's original identity.
            assembled = newline.join(current) + (newline if trailing_newline else "")
            payload = assembled.encode("utf-8")
            _write_file_atomically(fp, _UTF8_BOM + payload if has_bom else payload, original_mode)

            report = [f"Edited {path}: applied {len(applied)} edit block(s) atomically."]
            for item in sorted(applied, key=lambda entry: entry["index"]):
                detail = (
                    f"  #{item['index']} {item['tier']:<11} "
                    f"lines {item['start'] + 1}-{item['end']} "
                    f"-> {len(item['replace_lines'])} line(s)"
                )
                if item["delta"]:
                    direction = "-" if item["search_is_deeper"] else "+"
                    detail += f" (indent {direction}{len(item['delta'])})"
                if item["stage"] > 1:
                    detail += f" (chained, stage {item['stage']})"
                report.append(detail)
            if any(item["stage"] > 1 for item in applied):
                report.append(
                    "  note: chained blocks matched the text produced by earlier blocks, so "
                    "their line numbers refer to that intermediate state."
                )

            is_valid, err_msg = validate_code(path, assembled)
            if not is_valid:
                report.append("")
                report.append(f"Warning: 检测到语法错误(Syntax error)\n\n{err_msg}")

        return "\n".join(report)

    except Exception as e:
        log_error_traceback("FileEdit execution", e)
        return f"Error: {e}"


class ContentSearch(ToolArgumentsModel):
    """
    Search for a regex pattern in text files within a specific directory.

    OUTPUT FORMAT (same convention as `grep -n`):
    - Matched lines are rendered as `<line number>:<verbatim line content>`, context lines
      as `<line number>-<verbatim line content>`. Everything after the first separator is
      the exact file content, including its indentation.
    - That prefix is display-only and is NOT part of the file. Never copy it into FileEdit.
    - Non-adjacent ranges are separated by a `@@ <a>-<b> skipped @@` marker.

    AUTO-EXCLUDED:
    - Binary files (detected by null bytes)
    - Hidden directories (starting with '.'), unless the hidden directory itself is specified as root_dir
    - Build/dependency dirs: build, dist, __pycache__, node_modules, target, venv, site-packages, htmlcov

    LIMITS:
    - Maximum 500 matches returned (truncated if exceeded)
    - For large codebases, use specific root_dir to narrow scope
    """

    content_regex: str = Field(
        ...,
        description="Python regex pattern to search for in the file contents.",
    )
    root_dir: str = Field(
        default=".",
        description="Root directory to recursively search, relative to workspace by default. Paths outside workspace require user permission. Pinpoint specific source folders (e.g., 'src', 'app') to avoid scanning dependency directories.",
    )
    path_regex: str = Field(
        default=".*",
        description="Python regex pattern matched against each file's absolute normalized path. Defaults to '.*'.",
    )
    context_size: int = Field(
        default=1,
        ge=0,
        description="Number of context lines to include before and after each matched line.",
    )


def content_search(
        content_regex: str,
        root_dir: str = ".",
        path_regex: str = ".*",
        context_size: int = 1,
) -> str:
    if context_size < 0:
        return f"Error: context_size must be non-negative, got {context_size}."

    try:
        regex = re.compile(content_regex)
    except re.error as e:
        log_error_traceback("ContentSearch content regex compile", e)
        return f"Error: Invalid content_regex '{content_regex}': {e}"

    try:
        file_path_pattern = re.compile(path_regex)
    except re.error as e:
        log_error_traceback("ContentSearch path regex compile", e)
        return f"Error: Invalid path_regex '{path_regex}': {e}"

    results = {}
    total_matches = 0
    MAX_MATCHES = 500

    try:
        base_dir = safe_path(root_dir, "ContentSearch")
        if not base_dir.is_dir():
            return f"Error: Root directory '{root_dir}' not found or is not a directory."
    except Exception as e:
        log_error_traceback("ContentSearch resolve root dir", e)
        return f"Error resolving root directory: {e}"

    EXCLUDE_DIRS = {
        "build", "dist", "__pycache__", "node_modules", "target",
        "venv", "site-packages", "htmlcov"
    }

    try:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDE_DIRS]

            for file in files:
                filepath = Path(root) / file
                try:
                    rel_path = filepath.relative_to(base_dir)
                except ValueError:
                    continue

                if not file_path_pattern.search(_normalized_search_path(filepath)):
                    continue

                rel_path_str = rel_path.as_posix()

                if _is_binary_file(filepath):
                    continue

                matched_line_numbers = []
                try:
                    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
                        file_lines = f.readlines()

                    for i, line in enumerate(file_lines, 1):
                        if regex.search(line):
                            matched_line_numbers.append(i)
                            total_matches += 1
                            if total_matches >= MAX_MATCHES:
                                break
                except Exception as exc:
                    log_error_traceback(f"ContentSearch file read: {filepath}", exc)
                    continue

                if matched_line_numbers:
                    context_ranges = []
                    for line_number in matched_line_numbers:
                        start = max(1, line_number - context_size)
                        end = min(len(file_lines), line_number + context_size)
                        if context_ranges and start <= context_ranges[-1][1] + 1:
                            context_ranges[-1] = (context_ranges[-1][0], max(context_ranges[-1][1], end))
                        else:
                            context_ranges.append((start, end))

                    output_lines = []
                    matched_line_set = set(matched_line_numbers)
                    previous_end = None
                    for start, end in context_ranges:
                        if previous_end is not None:
                            output_lines.append(f"@@ {previous_end + 1}-{start - 1} skipped @@")
                        for line_number in range(start, end + 1):
                            marker = ":" if line_number in matched_line_set else "-"
                            output_lines.append(
                                f"{line_number}{marker}{file_lines[line_number - 1].rstrip('\n')}"
                            )
                        previous_end = end
                    results[rel_path_str] = output_lines

                if total_matches >= MAX_MATCHES:
                    break

            if total_matches >= MAX_MATCHES:
                break

    except Exception as e:
        log_error_traceback("ContentSearch walk execution", e)
        return f"Error during content search: {e}"

    if not results:
        return f"No matches found for content_regex '{content_regex}' in root_dir '{root_dir}' matching path_regex '{path_regex}'."

    output_blocks = []
    if base_dir != _workdir():
        output_blocks.append(f"(paths relative to {base_dir.as_posix()})")
        output_blocks.append("")
    for file_path, lines in results.items():
        output_blocks.append(f"File: {file_path}")
        output_blocks.extend(lines)
        output_blocks.append("")

    if total_matches >= MAX_MATCHES:
        output_blocks.append(
            f"\n[!] Notice: Output truncated to first {MAX_MATCHES} matched lines to prevent context overflow."
        )

    return truncate_output("\n".join(output_blocks).strip(), line_aligned=True)


class FileSearch(ToolArgumentsModel):
    """
    Search for files and/or directories matching a regex pattern against absolute normalized paths.

    AUTO-EXCLUDED:
    - Hidden directories (starting with '.'), unless the hidden directory itself is specified as root_dir
    - Build/dependency dirs: build, dist, __pycache__, node_modules, target, venv, site-packages, htmlcov

    LIMITS:
    - Maximum 500 items returned (truncated if exceeded)
    - For large codebases, use specific root_dir to narrow scope
    """

    path_regex: str = Field(
        default=".*",
        description="Python regex pattern matched against each item's absolute normalized path. Defaults to '.*'.",
    )
    root_dir: str = Field(
        default=".",
        description=(
            "Root directory to recursively search, relative to workspace by default. Paths outside workspace require user permission. "
            "Pinpoint specific source folders (e.g., 'src', 'app') to avoid scanning dependency directories."
        ),
    )
    type: str = Field(
        default="all",
        description=(
            "Type of items to return: 'file' for files only, 'dir' for directories only, "
            "'all' for both files and directories. Defaults to 'all'."
        ),
    )


def file_search(
        path_regex: str = ".*",
        root_dir: str = ".",
        type: str = "all",
) -> str:
    EXCLUDE_DIRS = {
        "build", "dist", "__pycache__", "node_modules", "target",
        "venv", "site-packages", "htmlcov"
    }
    MAX_ITEMS = 500

    # Validate type parameter
    if type not in ("file", "dir", "all"):
        return f"Error: Invalid type '{type}'. Must be 'file', 'dir', or 'all'."

    try:
        regex = re.compile(path_regex)
    except re.error as e:
        log_error_traceback("FileSearch path regex compile", e)
        return f"Error: Invalid path_regex '{path_regex}': {e}"

    # Determine item type label for output
    type_label = {"file": "file(s)", "dir": "director(ies)", "all": "item(s)"}[type]

    try:
        base_dir = safe_path(root_dir, "FileSearch")
        if not base_dir.is_dir():
            return f"Error: Root directory '{root_dir}' not found or is not a directory."
    except Exception as e:
        log_error_traceback("FileSearch resolve root dir", e)
        return f"Error resolving root directory: {e}"

    try:
        matched_files = set()
        matched_dirs = set()
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDE_DIRS]

            entries = []
            if type in ("dir", "all"):
                entries.extend((Path(root) / d, True) for d in dirs)
            if type in ("file", "all"):
                entries.extend((Path(root) / f, False) for f in files)

            for item, is_dir in entries:
                if len(matched_files) + len(matched_dirs) >= MAX_ITEMS:
                    break

                # Relative to base_dir for display
                try:
                    rel_path = item.relative_to(base_dir)
                except ValueError:
                    continue

                if _is_excluded_dir_path(rel_path, is_dir, EXCLUDE_DIRS):
                    continue

                if not regex.search(_normalized_search_path(item)):
                    continue

                rel_str = rel_path.as_posix()
                if is_dir:
                    matched_dirs.add(rel_str)
                else:
                    matched_files.add(rel_str)

            if len(matched_files) + len(matched_dirs) >= MAX_ITEMS:
                break

        total_count = len(matched_files) + len(matched_dirs)
        if total_count == 0:
            return f"No {type_label} found matching path_regex '{path_regex}' in root_dir '{root_dir}'."

        # Format output: directories always with [DIR] prefix and trailing /
        sorted_files = sorted(matched_files)
        sorted_dirs = sorted(matched_dirs)
        lines = [f"[DIR] {d}/" for d in sorted_dirs] + list(sorted_files)

        if base_dir == _workdir():
            output = f"Found {total_count} {type_label} matching path_regex '{path_regex}' in root_dir '{root_dir}':\n\n"
        else:
            output = f"Found {total_count} {type_label} matching path_regex '{path_regex}' in root_dir '{root_dir}' (paths relative to {base_dir.as_posix()}):\n\n"
        output += "\n".join(lines)

        if total_count >= MAX_ITEMS:
            output += f"\n\n[!] Notice: Output truncated to first {MAX_ITEMS} items."

        return truncate_output(output)

    except Exception as e:
        log_error_traceback("FileSearch execution", e)
        return f"Error during file search: {e}"


class GetSystemTime(ToolArgumentsModel):
    """
    Get the exact current system time.

    USE CASES:
    - Timestamping operations or logging
    - Calculating elapsed time between operations
    - Recording when tasks were started/completed

    RETURNS: Datetime string in format "YYYY-MM-DD HH:MM:SS"
    """

    pass


def get_system_time(**kwargs) -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


FILE_TOOLS, FILE_TOOL_MODELS = build_tool_definitions(
    FileRead,
    FileCreate,
    FileEdit,
    ContentSearch,
    FileSearch,
)
TERMINAL_TOOLS, TERMINAL_TOOL_MODELS = build_tool_definitions(RunTerminalCommand)
SYSTEM_TIME_TOOLS, SYSTEM_TIME_TOOL_MODELS = build_tool_definitions(GetSystemTime)
COMMON_TOOL_MODELS = merge_tool_model_registries(
    FILE_TOOL_MODELS,
    TERMINAL_TOOL_MODELS,
    SYSTEM_TIME_TOOL_MODELS,
)


FILE_NAMESPACE = {
    "type": "namespace",
    "name": "File",
    "description": (
        "Primary file operation tools. Always prefer this namespace for file reads, "
        "writes, edits, text searches and file searches instead of shell commands. "
        "IMPORTANT: Use FileCreate only to create/write new or completely empty files. "
        "Use FileEdit for existing-file changes; edit blocks are matched against the current file contents."
    ),
    "tools": FILE_TOOLS,
}

TERMINAL_NAMESPACE = {
    "type": "namespace",
    "name": "Terminal",
    "description": "Tools for executing terminal commands.",
    "tools": TERMINAL_TOOLS,
}

COMMON_TOOLS = [
    FILE_NAMESPACE,
    TERMINAL_NAMESPACE,
    SYSTEM_TIME_TOOLS[0],
]

COMMON_TOOLS_HANDLERS = {
    "RunTerminalCommand": run_terminal_command,
    "FileRead": file_read,
    "FileCreate": file_create,
    "ContentSearch": content_search,
    "FileSearch": file_search,
    "FileEdit": file_edit,
    "GetSystemTime": get_system_time,
}
