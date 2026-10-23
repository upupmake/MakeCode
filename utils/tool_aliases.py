"""Code-level compatibility aliases for tool names."""

TOOL_NAME_ALIASES = {
    "Read": "FileRead",
    "ReadFile": "FileRead",
    # Claude Code tool names, so existing scripts and prompts keep working.
    "Write": "FileCreate",
    "Edit": "FileEdit",
    "MultiEdit": "FileEdit",
    "Bash": "RunTerminalCommand",
    "Glob": "FileSearch",
    "Grep": "ContentSearch",
    "Skill": "LoadSkill",
    "Task": "DelegateTasks",
}


def resolve_tool_name(tool_name: str) -> str:
    return TOOL_NAME_ALIASES.get(tool_name, tool_name)
