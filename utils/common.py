import locale
import os
import subprocess
import sys
from pathlib import Path
from shutil import which

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from init import WORKDIR


def make_response_tool(tool_dict):
    """Flatten pydantic_function_tool output for Responses API"""
    if "function" in tool_dict:
        func = tool_dict["function"]
        return {
            "type": "function",
            "name": func.get("name"),
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {}),
            "strict": func.get("strict", False)
        }
    return tool_dict


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
        except UnicodeDecodeError:
            sys_encoding = locale.getpreferredencoding()
            out = raw_output.decode(sys_encoding, errors='replace').strip()
        terminal_meta = f"{resolved_terminal}, source={STARTUP_TERMINAL_SOURCE}"
        return out if out else f"Command executed successfully with no output. (terminal: {terminal_meta})"

    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 120 seconds. Did you run an interactive prompt?"
    except FileNotFoundError:
        return (
            "Error: No supported terminal executable found. "
            f"startup_terminal={STARTUP_TERMINAL_TYPE or 'unavailable'} "
            f"(source={STARTUP_TERMINAL_SOURCE})."
        )
    except Exception as e:
        return f"Error executing command: {e}"


class RunRead(BaseModel):
    """Read contents of a file. Supports reading a specific line range."""
    path: str = Field(..., description="Path to the file to read, relative to workspace.")
    start: int | None = Field(None, description="Start line number (1-indexed). Optional.")
    end: int | None = Field(None, description="End line number (1-indexed). Optional.")


def run_read(path: str, start: int | None = None, end: int | None = None) -> str:
    try:
        # 显式指定 utf-8 编码，并使用 replace 处理无法解码的字节，防止读取崩溃
        text = safe_path(path).read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        total_lines = len(lines)

        s = start if start is not None else 1
        e = end if end is not None else total_lines

        s = max(1, s)
        e = min(total_lines, e)

        if s > e or s > total_lines:
            return f"Total lines: {total_lines}\n(Empty range or out of bounds)"

        sliced_lines = lines[s - 1:e]
        formatted_lines = [f"{i + s}: {line}" for i, line in enumerate(sliced_lines)]

        return f"Total lines: {total_lines}\n" + "\n".join(formatted_lines)
    except Exception as e:
        return f"Error: {e}"


class RunWrite(BaseModel):
    """Write content to a file, directly overwriting it and creating parent directories if needed."""
    path: str = Field(..., description="Path to the file to write, relative to workspace.")
    content: str = Field(..., description="The content to write to the file.")


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 强制使用 utf-8 写入，保持跨平台一致性
        fp.write_text(content, encoding='utf-8')
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


class RunEdit(BaseModel):
    """
    Replace a specific line range in a file with new content.
    CRITICAL REQUIREMENTS:
    1. You MUST call `RunRead` first.
    2. `new_content` MUST contain the EXACT absolute indentation (spaces) required. The tool does NOT auto-indent.
    3. Carefully check the `start` and `end` line numbers to avoid leaving orphaned code or duplicate signatures.
    """
    path: str = Field(..., description="Path to the file to edit, relative to workspace.")
    start: int = Field(..., description="Start line number (1-indexed) to replace.")
    end: int = Field(..., description="End line number (1-indexed) to replace.")
    new_content: str = Field(..., description="The new content to insert in the specified line range.")


def run_edit(path: str, start: int, end: int, new_content: str) -> str:
    try:
        fp = safe_path(path)
        if not fp.exists():
            return f"Error: File {path} not found."

        text = fp.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        total_lines = len(lines)

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
        return f"Error: {e}"


TOOLS = [
    make_response_tool(pydantic_function_tool(RunRead)),
    make_response_tool(pydantic_function_tool(RunWrite)),
    make_response_tool(pydantic_function_tool(RunEdit)),
]

FILE_NAMESPACE = {
    "type": "namespace",
    "name": "File",
    "description": (
        "Primary file operation tools for workspace files. Always prefer this namespace for file reads, "
        "writes, and edits instead of shell commands."
    ),
    "tools": TOOLS,
}

COMMON_TOOLS = [
    make_response_tool(pydantic_function_tool(RunTerminalCommand)),
    FILE_NAMESPACE,
    {"type": "web_search"},
]

COMMON_TOOLS_HANDLERS = {
    "RunTerminalCommand": run_terminal_command,
    "RunRead": run_read,
    "RunWrite": run_write,
    "RunEdit": run_edit,
}
