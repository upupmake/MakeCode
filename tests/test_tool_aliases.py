import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from utils.common import COMMON_TOOL_MODELS, COMMON_TOOLS
from utils.teams import TeammateManager
from utils.tool_aliases import resolve_tool_name


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _tool_names(tools):
    names = set()
    for tool in tools:
        function = tool.get("function")
        if function:
            names.add(function["name"])
        names.update(_tool_names(tool.get("tools", [])))
    return names


def test_read_aliases_resolve_to_file_read_only_in_code():
    assert resolve_tool_name("Read") == "FileRead"
    assert resolve_tool_name("ReadFile") == "FileRead"
    assert resolve_tool_name("FileRead") == "FileRead"
    assert resolve_tool_name("RunTerminalCommand") == "RunTerminalCommand"

    names = _tool_names(COMMON_TOOLS)
    assert "FileRead" in names
    assert "Read" not in names
    assert "ReadFile" not in names
    assert "Read" not in COMMON_TOOL_MODELS
    assert "ReadFile" not in COMMON_TOOL_MODELS


@pytest.mark.anyio
@pytest.mark.parametrize("alias", ["Read", "ReadFile"])
async def test_agent_loop_routes_read_aliases_to_file_read(alias):
    import main as main_module

    tool_call = {
        "id": "call_read",
        "name": alias,
        "arguments": '{"path": "README.md", "regions": [{"start": 1, "end": 1}]}',
    }
    responses = [
        (
            "",
            [tool_call],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
                "stop_reason": "tool_use",
            },
            False,
        ),
        (
            "done",
            [],
            {"role": "assistant", "content": "done", "stop_reason": "end_turn"},
            False,
        ),
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(messages, raw_message):
            messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    async def stream_with_render(messages, tools, llm_client):
        return responses.pop(0)

    handler = Mock(return_value="file content")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read the file"},
    ]

    with patch.object(main_module, "BASE_SUPER_TOOLS_HANDLERS", {"FileRead": handler}), \
            patch.object(main_module, "compact_tool_outputs"), \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(main_module, "_stream_with_render", side_effect=stream_with_render), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "estimate_tokens", return_value=0), \
            patch.object(main_module, "refresh_token_usage"), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False):
        committed = await main_module.agent_loop(messages, llm_client=FakeClient())

    assert committed is True
    handler.assert_called_once_with(
        path="README.md",
        regions=[{"start": 1, "end": 1}],
    )
    tool_result = next(message for message in messages if message.get("role") == "tool")
    assert tool_result["name"] == "FileRead"


@pytest.mark.anyio
async def test_sub_agent_routes_read_alias_to_file_read(tmp_path):
    tool_call = {
        "id": "call_read",
        "name": "Read",
        "arguments": json.dumps({
            "path": "README.md",
            "regions": [{"start": 1, "end": 1}],
        }),
    }

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                result = SimpleNamespace(
                    text="",
                    tool_calls=[tool_call],
                    stop_reason="tool_use",
                    assistant_message={"role": "assistant", "content": None},
                )
            elif self.calls == 2:
                result = SimpleNamespace(
                    text="done",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "done"},
                )
            else:
                result = SimpleNamespace(
                    text="report\n\nCOMPLETION_STATUS: completed",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "report"},
                )
            yield {"type": "done", "result": result}

        @staticmethod
        def append_assistant_message(messages, raw_message):
            messages.append(raw_message)

        @staticmethod
        def format_tool_result(tool_id, tool_name, output):
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": output,
            }

    handler = Mock(return_value="file content")
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"

    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.COMMON_TOOLS_HANDLERS", {"FileRead": handler}), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})):
        result = await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Use Read.",
            trace_log,
            FakeClient(),
        )

    handler.assert_called_once_with(
        path="README.md",
        regions=[{"start": 1, "end": 1}],
    )
    assert result["report"].endswith("COMPLETION_STATUS: completed")
