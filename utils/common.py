import datetime
import difflib
import fnmatch
import json
import locale
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from openai import pydantic_function_tool
from pydantic import BaseModel, Field, model_validator, field_validator

from init import log_error_traceback, STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE
from system.ts_validator import validate_code
from utils import paths
from utils.file_access import GLOBAL_FILE_CONTROLLER
from utils.hitl import check_permission, check_path_permission


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


def _split_filename_patterns(filename_pattern: str) -> list[str]:
    patterns = [p.strip() for p in filename_pattern.split("|") if p.strip()]
    return patterns or ["*"]


def _matches_filename_pattern(rel_path: Path, patterns: list[str]) -> bool:
    if patterns == ["*"]:
        return True

    rel_posix = rel_path.as_posix()
    name = rel_path.name
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if pattern.startswith("**/"):
            pattern = pattern[3:]
        if "/" in pattern:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
        elif fnmatch.fnmatch(name, pattern):
            return True
    return False


def _is_excluded_dir_path(rel_path: Path, is_dir: bool, exclude_dirs: set[str]) -> bool:
    dir_parts = rel_path.parts if is_dir else rel_path.parts[:-1]
    return any(part.startswith(".") or part in exclude_dirs for part in dir_parts)


def sanitize_title(title: str, max_len: int = 50) -> str | None:
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

    # Truncate to max_len
    title = title[:max_len]
    return title if title else None


def _resolve_startup_terminal_type() -> str:
    if STARTUP_TERMINAL_TYPE:
        return STARTUP_TERMINAL_TYPE
    raise FileNotFoundError("No startup terminal detected.")


_STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


class RunTerminalCommand(BaseModel):
    """
    Execute a terminal command in non-interactive mode.

    PROHIBITED COMMANDS:
    - Interactive: vim, nano, top, ssh, ftp
    - Destructive: rm -rf, format, del /f
    - Privilege escalation: sudo, runas
    - Network attacks: nmap, sqlmap

    PREFERRED APPROACH:
    - For file read/write/edit: Use File tools (FileRead/FileCreate/FileEdit)
    - For file content search: Use ContentSearch (not grep, rg, findstr)
    - For file name/pattern search: Use FileSearch (not find, ls, dir)
    - Use this tool ONLY for: builds, tests, git, package management, system info

    TIMEOUT: 120 seconds hard limit.
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
    if terminal_type == "powershell":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    if terminal_type == "pwsh":
        return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
    if terminal_type == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    if terminal_type in {"bash", "sh"}:
        return [terminal_type, "-lc", command]
    raise ValueError(f"Unsupported terminal type: {terminal_type}")


def run_terminal_command(command: str) -> str:
    parts = command.strip().split()
    if len(parts) > 1:
        action_name = " ".join(parts[:2])
    else:
        action_name = parts[0] if parts else "unknown"
    allowed, reason = check_permission("cmd", action_name, command)
    if not allowed:
        return f"User Denied Execution. Reason: {reason}"

    try:
        resolved_terminal = _resolve_startup_terminal_type()
        r = subprocess.run(
            _build_terminal_argv(resolved_terminal, command),
            cwd=_workdir(),
            capture_output=True,
            timeout=120,
        )
        raw_output = r.stdout + r.stderr

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
        # 智能截断：保留开头50行 + 结尾250行，避免丢失关键信息
        HEAD_LINES = 50
        TAIL_LINES = 250
        lines = out.splitlines()
        if len(lines) > HEAD_LINES + TAIL_LINES:
            omitted = len(lines) - HEAD_LINES - TAIL_LINES
            out = (
                "\n".join(lines[:HEAD_LINES])
                + f"\n\n... (省略 {omitted} 行) ...\n\n"
                + "\n".join(lines[-TAIL_LINES:])
            )
        terminal_meta = f"{resolved_terminal}, source={STARTUP_TERMINAL_SOURCE}"
        return (
            out
            if out
            else f"Command executed successfully with no output. (terminal: {terminal_meta})"
        )

    except subprocess.TimeoutExpired as exc:
        log_error_traceback("RunTerminalCommand timeout", exc)
        return "Error: Command timed out after 120 seconds. Did you run an interactive prompt?"
    except FileNotFoundError as exc:
        log_error_traceback("RunTerminalCommand terminal missing", exc)
        return (
            "Error: No supported terminal executable found. "
            f"startup_terminal={STARTUP_TERMINAL_TYPE or 'unavailable'} "
            f"(source={STARTUP_TERMINAL_SOURCE})."
        )
    except Exception as e:
        log_error_traceback("RunTerminalCommand execution", e)
        return f"Error executing command: {e}"


class ReadBlock(BaseModel):
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


class FileRead(BaseModel):
    """
    Read contents of a file. Reads only the specified line ranges.

    LINE NUMBERING:
    - Line numbers are 1-indexed (first line is 1, not 0)
    - 'end' is INCLUSIVE (e.g., {start:1, end:100} reads lines 1-100)

    PERFORMANCE GUIDELINES:
    1. Provide specific regions when possible to reduce context usage.
    2. PREFER providing MULTIPLE regions in a SINGLE call rather than multiple separate calls.
       Example: regions=[{"start":1,"end":150},{"start":300,"end":450}]
    3. Overlapping or adjacent regions will be automatically merged for efficiency.

    WORKFLOW: Before calling FileRead, estimate all line ranges you need, then provide them all at once.
    """

    path: str = Field(
        ..., description="Path to the file to read, relative to workspace by default. Paths outside workspace require user permission."
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
        path: str, regions: list[dict], agent_access=None
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
            text = fp.read_text(encoding="utf-8", errors="replace")

            mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                agent_access.record_access(str(fp.resolve()), mtime)

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

        # 收集行号
        line_numbers = []
        for s, e in merged:
            line_numbers.extend(range(s, e + 1))

        if not line_numbers:
            return f"File: {path}, Total lines: {total_lines}\n(No valid lines to read)"

        # 格式化输出
        formatted_lines = [f"{n}: {lines[n - 1]}" for n in line_numbers]

        return f"File: {path}, Total lines: {total_lines}\n" + "\n".join(formatted_lines)
    except Exception as e:
        log_error_traceback("FileRead execution", e)
        return f"Error: {e}"


class FileCreate(BaseModel):
    """
    Create and write a NEW file, or overwrite a completely empty file.

    CRITICAL REQUIREMENTS:
    1. Use this tool ONLY when the target file does NOT exist yet, or is empty.
    2. If the file already exists and has content, use FileRead first, then FileEdit.
    3. Parent directories will be automatically created if they don't exist.

    ENCODING: Files are written in UTF-8 encoding.
    """

    path: str = Field(
        ..., description="Path to the file to write, relative to workspace by default. Paths outside workspace require user permission."
    )
    content: str = Field(..., description="The content to write to the file.")


def file_create(path: str, content: str, agent_access=None) -> str:
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
                        "For modifications, call FileRead first, then FileEdit."
                    )
            fp.parent.mkdir(parents=True, exist_ok=True)

            # 强制使用 utf-8 写入，保持跨平台一致性
            fp.write_text(content, encoding="utf-8")

            is_valid, err_msg = validate_code(path, content)
            if not is_valid:
                return f"Success with Warning: 文件已写入，但检测到语法错误(Syntax error)\n\n{err_msg}"

            mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                agent_access.record_access(str(fp.resolve()), mtime)

            return f"Created {path}: {content.count('\n') + 1} lines written"
    except Exception as e:
        log_error_traceback("FileCreate execution", e)
        return f"Error: {e}"


class EditBlock(BaseModel):
    """
    Represents a single search-and-replace operation.
    It locates the exact text matching `search_content` and replaces it with `replace_content`.
    """

    search_content: str = Field(
        ...,
        description=(
            "The EXACT original text to be replaced. "
            "CRITICAL RULES: "
            "1. You MUST include sufficient context (2-3 unchanged lines before and after the target) to uniquely identify the location. "
            "2. You must output the exact literal text. Indentation and line breaks must perfectly match the original file."
        ),
    )
    replace_content: str = Field(
        ...,
        description=(
            "The NEW text that will replace `search_content`. "
            "CRITICAL RULES: "
            "1. If you included unchanged context lines in `search_content`, you MUST duplicate them exactly here, otherwise they will be permanently deleted! "
            "2. Ensure absolute indentation spaces are perfectly maintained."
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


class FileEdit(BaseModel):
    """
    Replace specific text blocks in a file with new content.

    PREREQUISITE: You MUST call FileRead first to get the current file content.

    HOW TO USE PERFECTLY:
    1. Read the file first using `FileRead`.
    2. Identify the exact lines you want to change.
    3. Copy those lines into `search_content`, adding 2-3 lines of unchanged code above and below as context.
    4. Write the modified version into `replace_content`, making sure to KEEP the unchanged context lines!

    MATCHING RULES (in order):
    1. Exact match tried first
    2. If no exact match, whitespace-tolerant matching attempted
    3. Fuzzy matching (95% similarity) as last resort
    - If multiple matches found, the edit is REJECTED

    WARNINGS:
    - Never invent code or guess indentation.
    - Never use `...` to skip code.
    - If your search block is not unique, include more context lines.
    """

    path: str = Field(
        ...,
        description="Path to the file you want to edit."
    )
    edits: list[EditBlock] = Field(
        ...,
        description="A list of edits. Each edit has: search_content (exact text to find) and replace_content (new text). Processed sequentially. Do not overlap target regions."
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


def apply_edit_block(file_text: str, search: str, replace: str) -> tuple[bool, str, str]:
    """
    尝试在文本中替换块，包含三重容错机制：精确匹配 -> Strip匹配 -> Difflib模糊匹配
    """
    # 统一换行符
    file_text = file_text.replace("\r\n", "\n")
    search = search.replace("\r\n", "\n")
    replace = replace.replace("\r\n", "\n")

    # 1. 尝试精确匹配
    count = file_text.count(search)
    if count == 1:
        return True, file_text.replace(search, replace), ""
    elif count > 1:
        return False, file_text, "Search content found multiple times. Please include more context to make it unique. IMPORTANT: Please call FileRead to re-read the file and obtain the latest content before retrying."

    # 2. 尝试容错匹配 (去除首尾空白)
    search_stripped = search.strip()
    if not search_stripped:
        return False, file_text, "Search content cannot be empty or only whitespace."

    count_stripped = file_text.count(search_stripped)
    if count_stripped == 1:
        start_idx = file_text.find(search_stripped)
        end_idx = start_idx + len(search_stripped)
        new_text = file_text[:start_idx] + replace.strip() + file_text[end_idx:]
        return True, new_text, ""
    elif count_stripped > 1:
        return False, file_text, "Stripped search content matches multiple locations. Please include more context. IMPORTANT: Please call FileRead to re-read the file and obtain the latest content before retrying."

    # 3. difflib 模糊匹配兜底
    SIMILARITY_THRESHOLD = 0.95

    file_lines = file_text.splitlines()
    search_lines = search_stripped.splitlines()
    replace_lines = replace.splitlines()

    search_len = len(search_lines)
    if search_len == 0 or not file_lines:
        return False, file_text, "Search content NOT found. IMPORTANT: Please call FileRead to re-read the file and obtain the latest content before retrying."

    best_ratio = 0.0
    best_start_idx = -1
    best_end_idx = -1

    max_window_diff = 2
    min_window = max(1, search_len - max_window_diff)
    max_window = min(len(file_lines), search_len + max_window_diff)

    for window_len in range(min_window, max_window + 1):
        for i in range(len(file_lines) - window_len + 1):
            window_text = "\n".join(file_lines[i: i + window_len]).strip()
            ratio = difflib.SequenceMatcher(None, window_text, search_stripped).ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_start_idx = i
                best_end_idx = i + window_len

    if best_ratio >= SIMILARITY_THRESHOLD:
        new_lines = file_lines[:best_start_idx] + replace_lines + file_lines[best_end_idx:]
        new_text = "\n".join(new_lines)
        return True, new_text, f"(Warning: Exact match failed. Used fuzzy match with similarity {best_ratio:.2f})"

    return False, file_text, (
        f"Search content NOT found. Best match similarity was {best_ratio:.2f} "
        f"(needs >= {SIMILARITY_THRESHOLD}). Ensure exact indentation and spaces. "
        f"IMPORTANT: The file may have been modified since your last read. Please call FileRead again to get the latest content before retrying the edit."
    )

def file_edit(path: str, edits: Any, agent_access=None) -> str:
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

            current_mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                allowed, msg = agent_access.can_edit(str(fp.resolve()), current_mtime)
                if not allowed:
                    return msg

            text = fp.read_text(encoding="utf-8", errors="replace")
            warnings = []
            for i, block in enumerate(parsed_blocks):
                success, new_text, msg = apply_edit_block(
                    text, block.search_content, block.replace_content
                )

                if not success:
                    return f"Error in edit block {i + 1}:\n{msg}\nNo changes were saved. Please call FileRead to re-read the file and obtain the latest content before retrying."

                if "Warning" in msg:
                    warnings.append(f"Block {i + 1}: {msg}")

                text = new_text

            if not text.endswith("\n"):
                text += "\n"

            fp.write_text(text, encoding="utf-8")

            is_valid, err_msg = validate_code(path, text)
            if not is_valid:
                return f"Edited {path}: 成功应用 {len(parsed_blocks)} 个编辑块，但检测到语法错误(Syntax error)\n\n{err_msg}"

        success_msg = f"Edited {path}: Successfully applied {len(parsed_blocks)} edit block(s)."
        if warnings:
            success_msg += "\n" + "\n".join(warnings)
        return success_msg

    except Exception as e:
        log_error_traceback("FileEdit execution", e)
        return f"Error: {e}"


class ContentSearch(BaseModel):
    """
    Search for a regex pattern in text files within a specific directory.

    AUTO-EXCLUDED:
    - Binary files (detected by null bytes)
    - Hidden directories (starting with '.')
    - Build/dependency dirs: build, dist, __pycache__, node_modules, target, venv, site-packages, htmlcov

    LIMITS:
    - Maximum 500 matches returned (truncated if exceeded)
    - For large codebases, use specific target_dir to narrow scope
    """

    content_pattern: str = Field(
        ...,
        description="The Regex pattern or string to search for in the file contents.",
    )
    target_dir: str = Field(
        default=".",
        description="Directory to search in, relative to workspace by default. Paths outside workspace require user permission. Pinpoint specific source folders (e.g., 'src', 'app') to avoid scanning dependency directories.",
    )
    filename_pattern: str = Field(
        default="*",
        description="File name pattern to filter files. Supports glob patterns with pipe separation for multiple patterns, e.g., '*.py', '*.py|*.js|*.vue'. Defaults to '*' (all text files).",
    )


def content_search(
        content_pattern: str,
        target_dir: str = ".",
        filename_pattern: str = "*",
) -> str:
    try:
        regex = re.compile(content_pattern)
    except re.error as e:
        log_error_traceback("ContentSearch regex compile", e)
        return f"Error: Invalid regex pattern '{content_pattern}': {e}"

    patterns = _split_filename_patterns(filename_pattern)

    results = {}
    total_matches = 0
    MAX_MATCHES = 500

    try:
        base_dir = safe_path(target_dir, "ContentSearch")
        if not base_dir.is_dir():
            return f"Error: Target directory '{target_dir}' not found or is not a directory."
    except Exception as e:
        log_error_traceback("ContentSearch resolve target dir", e)
        return f"Error resolving target directory: {e}"

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

                if not _matches_filename_pattern(rel_path, patterns):
                    continue

                rel_path_str = rel_path.as_posix()

                if _is_binary_file(filepath):
                    continue

                matched_lines = []
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                matched_lines.append(f"{i}: {line.rstrip('\n')}")
                                total_matches += 1
                                if total_matches >= MAX_MATCHES:
                                    break
                except Exception as exc:
                    log_error_traceback(f"ContentSearch file read: {filepath}", exc)
                    continue

                if matched_lines:
                    results[rel_path_str] = matched_lines

                if total_matches >= MAX_MATCHES:
                    break

            if total_matches >= MAX_MATCHES:
                break

    except Exception as e:
        log_error_traceback("ContentSearch walk execution", e)
        return f"Error during grep search: {e}"

    if not results:
        return f"No matches found for '{content_pattern}' in dir '{target_dir}' matching {patterns}."

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

    return "\n".join(output_blocks).strip()


class FileSearch(BaseModel):
    """
    Search for files and/or directories matching a glob pattern.

    AUTO-EXCLUDED:
    - Hidden directories (starting with '.')
    - Build/dependency dirs: build, dist, __pycache__, node_modules, target, venv, site-packages, htmlcov

    LIMITS:
    - Maximum 500 items returned (truncated if exceeded)
    - For large codebases, use specific target_dir to narrow scope
    """

    filename_pattern: str = Field(
        default="*",
        description=(
            "Glob pattern to match file names. Supports multiple patterns separated by '|'. "
            "Examples: '*.py', '*.py|*.js|*.vue'. Defaults to '*' (all files)."
        ),
    )
    target_dir: str = Field(
        default=".",
        description=(
            "Directory to search in, relative to workspace by default. Paths outside workspace require user permission. "
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
        filename_pattern: str = "*",
        target_dir: str = ".",
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

    patterns = _split_filename_patterns(filename_pattern)

    # Determine item type label for output
    type_label = {"file": "file(s)", "dir": "director(ies)", "all": "item(s)"}[type]

    try:
        base_dir = safe_path(target_dir, "FileSearch")
        if not base_dir.is_dir():
            return f"Error: Target directory '{target_dir}' not found or is not a directory."
    except Exception as e:
        log_error_traceback("FileSearch resolve target dir", e)
        return f"Error resolving target directory: {e}"

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

                if not _matches_filename_pattern(rel_path, patterns):
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
            return f"No {type_label} found matching pattern '{filename_pattern}' in directory '{target_dir}'."

        # Format output: directories always with [DIR] prefix and trailing /
        sorted_files = sorted(matched_files)
        sorted_dirs = sorted(matched_dirs)
        lines = [f"[DIR] {d}/" for d in sorted_dirs] + list(sorted_files)

        if base_dir == _workdir():
            output = f"Found {total_count} {type_label} matching '{filename_pattern}' in '{target_dir}':\n\n"
        else:
            output = f"Found {total_count} {type_label} matching '{filename_pattern}' in '{target_dir}' (paths relative to {base_dir.as_posix()}):\n\n"
        output += "\n".join(lines)

        if total_count >= MAX_ITEMS:
            output += f"\n\n[!] Notice: Output truncated to first {MAX_ITEMS} items."

        return output

    except Exception as e:
        log_error_traceback("FileSearch execution", e)
        return f"Error during glob search: {e}"


class GetSystemTime(BaseModel):
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


TOOLS = [
    pydantic_function_tool(FileRead),
    pydantic_function_tool(FileCreate),
    pydantic_function_tool(FileEdit),
    pydantic_function_tool(ContentSearch),
    pydantic_function_tool(FileSearch),
]

FILE_NAMESPACE = {
    "type": "namespace",
    "name": "File",
    "description": (
        "Primary file operation tools. Always prefer this namespace for file reads, "
        "writes, edits, text searches and file searches instead of shell commands. "
        "IMPORTANT: Use FileCreate only to create/write new or completely empty files. For existing-file changes, you must call "
        "FileRead first and then use FileEdit."
    ),
    "tools": TOOLS,
}

TERMINAL_NAMESPACE = {
    "type": "namespace",
    "name": "Terminal",
    "description": "Tools for executing terminal commands.",
    "tools": [
        pydantic_function_tool(RunTerminalCommand),
    ],
}

COMMON_TOOLS = [
    FILE_NAMESPACE,
    TERMINAL_NAMESPACE,
    pydantic_function_tool(GetSystemTime),
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
