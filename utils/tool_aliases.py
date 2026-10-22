"""Code-level compatibility aliases for tool names."""

TOOL_NAME_ALIASES = {
    "Read": "FileRead",
    "ReadFile": "FileRead",
}


def resolve_tool_name(tool_name: str) -> str:
    return TOOL_NAME_ALIASES.get(tool_name, tool_name)
