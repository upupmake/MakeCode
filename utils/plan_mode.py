"""
Plan Mode state management.

Plan Mode restricts the agent to read-only and planning tools only.
This ensures the LLM focuses on analysis and task topology planning
before any file modifications or command execution.

Toggle via Tab key or /plan command.
"""

# Tools blocked in Plan Mode: write/execute/delegate tools
PLAN_MODE_BLOCKLIST = frozenset({
    # File write/edit
    "RunWrite",
    "RunEdit",
    # Terminal execution
    "RunTerminalCommand",
    # Team delegation
    "DelegateTasks",
})

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
