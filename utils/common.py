import locale
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from init import WORKDIR, log_error_traceback


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


SUPPORTED_TERMINAL_TYPES = ("powershell", "pwsh", "cmd", "bash", "zsh", "sh")


def _terminal_exists(terminal: str) -> bool:
    if terminal == "cmd":
        return sys.platform == "win32" and bool(which("cmd") or os.getenv("ComSpec"))
    return which(terminal) is not None


def _detect_startup_terminal_type() -> tuple[str | None, str]:
    """
    通过硬编码优先级寻找可用的终端环境。
    返回: (终端名称, 来源标识)
    """
    if sys.platform == "win32":
        # Windows: 优先使用较新的 PowerShell Core，其次 Windows PowerShell，最后 cmd
        candidates = ["pwsh", "powershell", "cmd"]
    elif sys.platform == "darwin":
        # macOS: 从 macOS Catalina 开始，默认终端是 zsh
        candidates = ["zsh", "bash", "sh"]
    else:
        # Linux / 其他 POSIX: 默认 bash 为主
        candidates = ["bash", "zsh", "sh"]

    for terminal in candidates:
        if _terminal_exists(terminal):
            # 因为是硬编码优先级，source 统一标记为 platform-fallback
            return terminal, "platform-fallback"

    return None, "unavailable"


STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE = _detect_startup_terminal_type()


def _resolve_startup_terminal_type() -> str:
    if STARTUP_TERMINAL_TYPE:
        return STARTUP_TERMINAL_TYPE
    raise FileNotFoundError("No startup terminal detected.")


_STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"


class RunTerminalCommand(BaseModel):
    """
    IMPORTANT:
    - Strictly prohibit interactive, destructive (e.g., file deletion), privilege-escalating,
      or network-attacking dangerous commands.
    - For workspace file operations (read/write/edit), prefer File tools (RunRead/RunWrite/RunEdit).
      Do NOT use this tool for routine file manipulation when File tools can handle the task.
    """
    command: str = Field(
        ...,
        description=(
            "The terminal command string to execute in non-interactive mode. "
            f"Runtime terminal is fixed at startup: '{_STARTUP_TERMINAL_LABEL}' "
            f"(source={STARTUP_TERMINAL_SOURCE}). "
            "This tool only accepts command; terminal type is not configurable per call. "
            "Do not use this tool for normal workspace file read/write/edit operations."
        )
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
    try:
        resolved_terminal = _resolve_startup_terminal_type()
        r = subprocess.run(
            _build_terminal_argv(resolved_terminal, command),
            cwd=WORKDIR,
            capture_output=True,
            timeout=120
        )
        raw_output = r.stdout + r.stderr

        # 动态解码策略：优先 UTF-8，失败则回退到系统默认编码 (Windows 下通常是 GBK)
        try:
            out = raw_output.decode('utf-8').strip()
        except UnicodeDecodeError as exc:
            log_error_traceback("RunTerminalCommand utf8 decode fallback", exc)
            sys_encoding = locale.getpreferredencoding()
            out = raw_output.decode(sys_encoding, errors='replace').strip()
        terminal_meta = f"{resolved_terminal}, source={STARTUP_TERMINAL_SOURCE}"
        return out if out else f"Command executed successfully with no output. (terminal: {terminal_meta})"

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


class RunRead(BaseModel):
    """Read contents of a file. Supports reading a specific line range."""
    path: str = Field(..., description="Path to the file to read, relative to workspace.")
    start: int | None = Field(None, description="Start line number (1-indexed). Optional.")
    end: int | None = Field(None, description="End line number (1-indexed). Optional.")


def run_read(path: str, start: int | None = None, end: int | None = None, agent_access=None) -> str:
    from utils.file_access import GLOBAL_FILE_CONTROLLER
    try:
        with GLOBAL_FILE_CONTROLLER.global_lock:
            fp = safe_path(path)
            if not fp.exists():
                return f"Error: File {path} not found."
                
            # 显式指定 utf-8 编码，并使用 replace 处理无法解码的字节，防止读取崩溃
            text = fp.read_text(encoding='utf-8', errors='replace')
            
            mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                agent_access.record_access(path, mtime)
                
            lines = text.splitlines()
            total_lines = len(lines)

        try:
            s = int(start) if (start is not None and str(start).strip() != "") else 1
        except ValueError as exc:
            log_error_traceback("RunRead invalid start line", exc)
            s = 1

        try:
            e = int(end) if (end is not None and str(end).strip() != "") else total_lines
        except ValueError as exc:
            log_error_traceback("RunRead invalid end line", exc)
            e = total_lines

        s = max(1, s)
        e = min(total_lines, e)

        if s > e or s > total_lines:
            return f"Total lines: {total_lines}\n(Empty range or out of bounds)"

        sliced_lines = lines[s - 1:e]
        formatted_lines = [f"{i + s}: {line}" for i, line in enumerate(sliced_lines)]

        return f"Total lines: {total_lines}\n" + "\n".join(formatted_lines)
    except Exception as e:
        log_error_traceback("RunRead execution", e)
        return f"Error: {e}"


class RunWrite(BaseModel):
    """
    Create and write a NEW file.
    CRITICAL REQUIREMENTS:
    1. Use this tool only when the target file does NOT exist yet.
    2. If the file already exists and you need modifications, use RunRead first, then RunEdit.
    """
    path: str = Field(..., description="Path to the file to write, relative to workspace.")
    content: str = Field(..., description="The content to write to the file.")


def run_write(path: str, content: str, agent_access=None) -> str:
    from utils.file_access import GLOBAL_FILE_CONTROLLER
    try:
        with GLOBAL_FILE_CONTROLLER.global_lock:
            fp = safe_path(path)
            if fp.exists():
                return (
                    f"Error: File {path} already exists. "
                    "RunWrite is only for creating new files. "
                    "For modifications, call RunRead first, then RunEdit."
                )
            fp.parent.mkdir(parents=True, exist_ok=True)
            # 强制使用 utf-8 写入，保持跨平台一致性
            fp.write_text(content, encoding='utf-8')
            
            mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                agent_access.record_access(path, mtime)
                
            return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        log_error_traceback("RunWrite execution", e)
        return f"Error: {e}"


class RunEdit(BaseModel):
    """
    Replace a specific line range in a file with new content.
    CRITICAL REQUIREMENTS:
    1. You MUST call `RunRead` first.
    1.1 You MUST use this tool for modifying an existing file (do NOT use RunWrite to modify existing files).
    2. `new_content` MUST contain the EXACT absolute indentation (spaces) required. The tool does NOT auto-indent.
    3. Carefully check the `start` and `end` line numbers to avoid leaving orphaned code or duplicate signatures.
    """
    path: str = Field(..., description="Path to the file to edit, relative to workspace.")
    start: int = Field(..., description="Start line number (1-indexed) to replace.")
    end: int = Field(..., description="End line number (1-indexed) to replace.")
    new_content: str = Field(..., description="The new content to insert in the specified line range.")


def run_edit(path: str, start: int, end: int, new_content: str, agent_access=None) -> str:
    from utils.file_access import GLOBAL_FILE_CONTROLLER
    try:
        with GLOBAL_FILE_CONTROLLER.global_lock:
            fp = safe_path(path)
            if not fp.exists():
                return f"Error: File {path} not found."
                
            current_mtime = GLOBAL_FILE_CONTROLLER.get_real_mtime(fp)
            if agent_access:
                allowed, msg = agent_access.can_edit(path, current_mtime)
                if not allowed:
                    return msg

            text = fp.read_text(encoding='utf-8', errors='replace')
            lines = text.splitlines()
            total_lines = len(lines)

        try:
            start = int(start)
            end = int(end)
        except ValueError as exc:
            log_error_traceback("RunEdit invalid line range type", exc)
            return "Error: start and end must be integers."

        if start < 1 or end > total_lines or start > end:
            return f"Error: Invalid line range [{start}, {end}]. File has {total_lines} lines."

        # Extract surrounding context
        prefix = lines[:start - 1]
        suffix = lines[end:]

        # Insert new content (could be multiple lines)
        new_lines = new_content.splitlines() if new_content else []

        final_lines = prefix + new_lines + suffix
        # Ensure trailing newline matches original behavior (splitlines drops trailing newline)
        if text.endswith("\n") or not text:
            fp.write_text("\n".join(final_lines) + "\n", encoding='utf-8')
        else:
            fp.write_text("\n".join(final_lines), encoding='utf-8')

        return f"Edited {path}: Replaced lines {start} to {end}."
    except Exception as e:
        log_error_traceback("RunEdit execution", e)
        return f"Error: {e}"


class RunGrep(BaseModel):
    """
    Search for a regex pattern in text files within a specific directory.
    Automatically ignores binary files and hidden directories (starting with '.').
    """
    keyword_pattern: str = Field(
        ...,
        description="The Regex pattern or string to search for in the file contents."
    )
    target_dir: str = Field(
        default=".",
        description="Directory to search in, relative to workspace. Pinpoint specific source folders (e.g., 'src', 'app') to avoid scanning dependency directories."
    )
    filename_pattern: str | list[str] = Field(
        default=["*"],
        description="File name pattern(s) to filter files. Can be a string or a list of strings (e.g., ['*.py', '*.ts'], '*.js'). Defaults to ['*'] (all text files)."
    )


def _is_binary_file(filepath: Path) -> bool:
    """Check if a file is likely binary by inspecting its first 1024 bytes for a null byte."""
    try:
        with open(filepath, 'rb') as f:
            chunk = f.read(1024)
            if b'\0' in chunk:
                return True
        return False
    except Exception as exc:
        log_error_traceback("RunGrep binary file check", exc)
        return True


def run_grep(
        keyword_pattern: str,
        target_dir: str = ".",
        filename_pattern: str | list[str] = ["*"]
) -> str:
    try:
        regex = re.compile(keyword_pattern)
    except re.error as e:
        log_error_traceback("RunGrep regex compile", e)
        return f"Error: Invalid regex pattern '{keyword_pattern}': {e}"

    if isinstance(filename_pattern, str):
        patterns = [filename_pattern]
    else:
        patterns = filename_pattern

    clean_patterns = [p.replace("**/", "") for p in patterns]

    results = {}
    total_matches = 0
    MAX_MATCHES = 500

    try:
        base_dir = safe_path(target_dir)
        if not base_dir.is_dir():
            return f"Error: Target directory '{target_dir}' not found or is not a directory."
    except Exception as e:
        log_error_traceback("RunGrep resolve target dir", e)
        return f"Error resolving target directory: {e}"

    try:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for file in files:
                filepath = Path(root) / file
                if clean_patterns != ["*"] and clean_patterns != [""]:
                    if not any(filepath.match(p) for p in clean_patterns):
                        continue

                try:
                    rel_path_str = filepath.relative_to(WORKDIR).as_posix()
                except ValueError as exc:
                    log_error_traceback(f"RunGrep path outside workspace: {filepath}", exc)
                    continue

                if _is_binary_file(filepath):
                    continue

                matched_lines = []
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                matched_lines.append(f"{i}: {line.rstrip('\n')}")
                                total_matches += 1
                                if total_matches >= MAX_MATCHES:
                                    break
                except Exception as exc:
                    log_error_traceback(f"RunGrep file read: {filepath}", exc)
                    continue

                if matched_lines:
                    results[rel_path_str] = matched_lines

                if total_matches >= MAX_MATCHES:
                    break

            if total_matches >= MAX_MATCHES:
                break

    except Exception as e:
        log_error_traceback("RunGrep walk execution", e)
        return f"Error during grep search: {e}"

    if not results:
        return f"No matches found for '{keyword_pattern}' in dir '{target_dir}' matching {patterns}."

    output_blocks = []
    for file_path, lines in results.items():
        output_blocks.append(f"File: {file_path}")
        output_blocks.extend(lines)
        output_blocks.append("")

    if total_matches >= MAX_MATCHES:
        output_blocks.append(
            f"\n[!] Notice: Output truncated to first {MAX_MATCHES} matched lines to prevent context overflow.")

    return "\n".join(output_blocks).strip()


TOOLS = [
    pydantic_function_tool(RunRead),
    pydantic_function_tool(RunWrite),
    pydantic_function_tool(RunEdit),
    pydantic_function_tool(RunGrep),
]

FILE_NAMESPACE = {
    "type": "namespace",
    "name": "File",
    "description": (
        "Primary file operation tools for workspace files. Always prefer this namespace for file reads, "
        "writes, edits, and text searches instead of shell commands. "
        "IMPORTANT: Use RunWrite only to create/write new files. For existing-file changes, you must call "
        "RunRead first and then use RunEdit."
    ),
    "tools": TOOLS,
}

TERMINAL_NAMESPACE = {
    "type": "namespace",
    "name": "Terminal",
    "description": "Tools for executing terminal commands.",
    "tools": [
        pydantic_function_tool(RunTerminalCommand),
    ]
}

COMMON_TOOLS = [
    FILE_NAMESPACE,
    TERMINAL_NAMESPACE,
]

COMMON_TOOLS_HANDLERS = {
    "RunTerminalCommand": run_terminal_command,
    "RunRead": run_read,
    "RunWrite": run_write,
    "RunGrep": run_grep,
    "RunEdit": run_edit,
}
