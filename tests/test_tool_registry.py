from system.tool_registry import ToolRegistry


class FakeClient:
    def format_tools(self, tools):
        return tools


def _names(tools):
    names = set()
    for tool in tools:
        if tool.get("type") == "namespace":
            names.update(_names(tool.get("tools", [])))
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if name:
            names.add(name)
    return names


def _definitions(registry):
    return registry.definitions(
        mcp_tools=[],
        llm_client=FakeClient(),
        plan_mode=False,
    )


def test_leaf_tool_filter_keeps_only_requested_action():
    registry = ToolRegistry(
        allowed_tools=["FileEdit"],
        include_ask_user=False,
        include_delegate_tasks=False,
    )

    assert _names(_definitions(registry)) == {"FileEdit"}
    assert "FileEdit" in registry.handlers()
    assert "FileRead" not in registry.handlers()


def test_namespace_filter_expands_to_all_contained_tools():
    registry = ToolRegistry(
        allowed_tools=["File"],
        include_ask_user=False,
        include_delegate_tasks=False,
    )

    names = _names(_definitions(registry))
    assert {"FileRead", "FileCreate", "FileEdit", "FilePatch"} <= names
    assert {"ContentSearch", "FileSearch"} <= names
    assert "RunTerminalCommand" not in names
    assert "DelegateTasks" not in names
    assert "FileCreate" in registry.handlers()


def test_disallowed_namespace_removes_all_contained_tools():
    registry = ToolRegistry(disallowed_tools=["File"])

    names = _names(_definitions(registry))
    assert "FileRead" not in names
    assert "FileCreate" not in names
    assert "RunTerminalCommand" in names
    assert "FileRead" not in registry.handlers()


def test_plan_mode_hides_write_tools_recursively():
    registry = ToolRegistry(include_delegate_tasks=True)

    names = _names(
        registry.definitions(
            mcp_tools=[],
            llm_client=FakeClient(),
            plan_mode=True,
        )
    )

    assert "FileRead" in names
    assert "FileCreate" not in names
    assert "FileEdit" not in names
    assert "FilePatch" not in names
    assert "ManageLongTermMemory" not in names
    assert "DelegateTasks" not in names


def test_headless_disables_delegate_and_ask_user_tools():
    registry = ToolRegistry(
        include_ask_user=False,
        include_delegate_tasks=False,
    )

    names = _names(_definitions(registry))
    handlers = registry.handlers()

    assert "AskUser" not in names
    assert "DelegateTasks" not in names
    assert "AskUser" not in handlers
    assert "DelegateTasks" not in handlers
