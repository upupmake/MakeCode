"""
Plan Mode state management.

Plan Mode restricts the agent to read-only and planning tools only.
This ensures the LLM focuses on analysis and task topology planning
before any file modifications or command execution.

Toggle via Ctrl+P key or /plan command.
"""

# Tools blocked in Plan Mode: write/edit/delegate tools
PLAN_MODE_BLOCKLIST = frozenset({
    # File write/edit
    "FileCreate",
    "FileEdit",
    "FilePatch",
    # Memory writes
    "ManageLongTermMemory",
    # Team delegation
    "DelegateTasks",
})

# Allowed command prefixes in Plan Mode (extensible)
PLAN_MODE_ALLOWED_COMMANDS = (
    # Existing inspection / environment prefixes
    "git", "pip", "npm", "docker",
    # Repository search
    "grep", "rg", "find", "ag", "ack",
    # Path and file inspection
    "ls", "dir", "tree", "stat", "file", "wc", "du", "df",
    "pwd", "which", "where", "whereis", "type", "realpath", "readlink",
    # Text / content inspection
    "cat", "head", "tail", "sed", "awk", "cut", "sort",
    "uniq", "tr", "column", "nl", "od", "hexdump", "strings", "diff",
    "cmp", "comm", "jq", "yq",
    # Process / system inspection
    "ps", "uname", "whoami", "id", "env", "printenv",
    "date", "uptime", "hostname", "arch", "lsof", "netstat", "ss",
    # Windows cmd inspection
    "findstr", "fc", "comp", "systeminfo", "tasklist",
    # Windows PowerShell / pwsh inspection
    "Get-ChildItem", "gci", "Get-Content", "gc", "Get-Item", "gi",
    "Get-ItemProperty", "gp", "Get-Location", "gl", "Get-Command", "gcm",
    "Get-Help", "Select-String", "sls", "Select-Object",
    "Get-Process", "gps", "Get-Service", "gsv", "Get-Date",
    "Get-Host", "Get-ComputerInfo", "Get-Acl", "Resolve-Path", "rvpa",
    "Split-Path", "Join-Path", "Test-Path", "Measure-Object",
    "Format-Table", "Format-List", "Out-String", "ConvertTo-Json",
    "ConvertFrom-Json", "Get-FileHash", "Get-AuthenticodeSignature",
)

_PLAN_MODE_ALLOWED_COMMANDS_LOWER = frozenset(
    item.lower() for item in PLAN_MODE_ALLOWED_COMMANDS
)
_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")

# Global state
PLAN_MODE_ENABLED = False


def toggle_plan_mode(enabled: bool = None) -> bool:
    """Toggle plan mode. Returns new state."""
    global PLAN_MODE_ENABLED
    if enabled is not None:
        PLAN_MODE_ENABLED = enabled
    else:
        PLAN_MODE_ENABLED = not PLAN_MODE_ENABLED
    return PLAN_MODE_ENABLED


def is_plan_mode() -> bool:
    return PLAN_MODE_ENABLED


def is_plan_mode_command_allowed(command: str) -> bool:
    """Check if a terminal command is allowed in Plan Mode."""
    stripped_command = command.strip()
    if not stripped_command:
        return False
    if stripped_command[0] in "\"'":
        quote = stripped_command[0]
        end_quote = stripped_command.find(quote, 1)
        first_token = stripped_command[1:end_quote] if end_quote > 0 else stripped_command[1:]
    else:
        first_token = stripped_command.split(None, 1)[0]
    token = first_token.strip("\"'").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    lowered = token.lower()
    if lowered.endswith(_WINDOWS_EXECUTABLE_SUFFIXES):
        stem = token.rsplit(".", 1)[0]
    else:
        stem = token
    return lowered in _PLAN_MODE_ALLOWED_COMMANDS_LOWER or stem.lower() in _PLAN_MODE_ALLOWED_COMMANDS_LOWER
