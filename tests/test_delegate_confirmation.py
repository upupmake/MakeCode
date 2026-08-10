import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Label

from system.tool_history import TOOL_STATUS_FAILED, ToolExecutionHistory
from system.tui_modals import DelegateTasksModal
from utils.teams import DelegateTasks, TeammateManager
from utils.tool_validation import ToolArgumentsModel


class DelegateConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manager = TeammateManager(
            Path(self.temp_dir.name) / "conv_test",
            "conv_test",
        )
        self.tasks = [
            {
                "task_id": "1",
                "role_name": "Code Reviewer",
                "context_prompt": "Review the delegated change and report verification evidence.",
            },
            {
                "task_id": "2",
                "role_name": "Test Engineer",
                "context_prompt": "Run independent regression tests and report verification evidence.",
            },
        ]

    def test_delegate_tasks_rejects_a_single_task(self):
        with self.assertRaisesRegex(ValueError, "at least 2 items"):
            DelegateTasks.model_validate(
                {
                    "tasks": [
                        {
                            "task_id": "1",
                            "role_name": "Code Reviewer",
                            "context_prompt": "Review the delegated change.",
                        }
                    ]
                }
            )

    async def _delegate(self, action: str):
        raw_client = SimpleNamespace(close=AsyncMock())
        llm_client = SimpleNamespace(client=raw_client)

        with (
            patch.object(self.manager, "_validate_delegation_tasks", return_value=self.tasks),
            patch("utils.teams.choose_delegate_tasks_tui", return_value=action) as choose,
            patch("utils.teams.request_window_attention"),
            patch("utils.teams.post_tui"),
            patch("utils.teams.print_formatted_text"),
            patch("utils.teams._workdir", return_value=Path(self.temp_dir.name)),
            patch("utils.teams.get_current_model_config", return_value=object()),
            patch("utils.teams._create_async_chat_client", return_value=llm_client),
            patch.object(
                self.manager,
                "_sub_agent_loop",
                AsyncMock(return_value={"report": "COMPLETION_STATUS: completed"}),
            ) as sub_agent_loop,
            patch("utils.teams.recall_long_term_memories", new_callable=AsyncMock) as recall,
            patch("utils.hitl.check_permission") as hitl,
        ):
            result = await self.manager.delegate_concurrently(self.tasks)
        hitl.assert_not_called()
        return result, choose, sub_agent_loop, recall

    async def test_approve_starts_delegation(self):
        result, choose, sub_agent_loop, recall = await self._delegate("approve")

        choose.assert_called_once_with(
            [
                {
                    "task_id": "1",
                    "role_name": "Code Reviewer",
                    "summary": "Review the delegated change and report verification evidence.",
                },
                {
                    "task_id": "2",
                    "role_name": "Test Engineer",
                    "summary": "Run independent regression tests and report verification evidence.",
                },
            ]
        )
        sub_agent_loop.assert_awaited()
        recall.assert_awaited()
        self.assertIn("Sub-Agents Execution Reports", result)

    async def test_trace_history_is_not_injected_into_provider_prompt(self):
        trace_marker = "TRACE_SECRET_MUST_NOT_ENTER_PROVIDER"
        trace_path = self.manager.runs_dir / "old" / "task_1_trace.jsonl"
        trace_path.parent.mkdir(parents=True)
        trace_path.write_text(
            json.dumps({"event": "agent_llm_output", "data": {"text": trace_marker}}) + "\n",
            encoding="utf-8",
        )
        self.manager.history = [{
            "conversation_id": "conv_test",
            "plan_task_id": "1",
            "status": "failed",
            "trace_log": str(trace_path.relative_to(self.manager.conversation_root)),
        }]

        _, _, sub_agent_loop, _ = await self._delegate("approve")

        delegated_prompts = [call.args[2] for call in sub_agent_loop.await_args_list]
        self.assertTrue(delegated_prompts)
        self.assertTrue(all(trace_marker not in prompt for prompt in delegated_prompts))

    async def test_orchestrator_choice_does_not_start_delegation(self):
        result, _, sub_agent_loop, recall = await self._delegate("orchestrator")

        sub_agent_loop.assert_not_awaited()
        recall.assert_not_awaited()
        self.assertIn("Orchestrator", result)
        self.assertIn("Do not call DelegateTasks again", result)

    async def test_cancel_does_not_start_delegation(self):
        result, _, sub_agent_loop, recall = await self._delegate("cancel")

        sub_agent_loop.assert_not_awaited()
        recall.assert_not_awaited()
        self.assertEqual("Sub-agent delegation cancelled by the user.", result)

    async def test_approved_delegation_closes_async_client(self):
        raw_client = SimpleNamespace(close=AsyncMock())
        llm_client = SimpleNamespace(client=raw_client)

        with (
            patch.object(self.manager, "_validate_delegation_tasks", return_value=self.tasks),
            patch.object(
                self.manager,
                "_sub_agent_loop",
                AsyncMock(return_value={"report": "COMPLETION_STATUS: completed"}),
            ),
            patch("utils.teams.choose_delegate_tasks_tui", return_value="approve"),
            patch("utils.teams.request_window_attention"),
            patch("utils.teams.post_tui"),
            patch("utils.teams.print_formatted_text"),
            patch("utils.teams._workdir", return_value=Path(self.temp_dir.name)),
            patch("utils.teams.get_current_model_config", return_value=object()),
            patch("utils.teams._create_async_chat_client", return_value=llm_client) as create_client,
            patch("utils.teams.recall_long_term_memories", AsyncMock(return_value={})) as recall,
        ):
            result = await self.manager.delegate_concurrently(self.tasks)

        self.assertIn("Sub-Agents Execution Reports", result)
        create_client.assert_called_once()
        self.assertEqual(2, recall.await_count)
        raw_client.close.assert_awaited_once_with()


@pytest.mark.anyio
async def test_sub_agent_loop_consumes_unified_result_without_legacy_done_content(tmp_path):
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.calls += 1
            self.requests.append(list(messages))
            if self.calls == 1:
                result = SimpleNamespace(
                    text="work finished",
                    tool_calls=[],
                    assistant_message={
                        "role": "assistant",
                        "content": "work finished",
                        "message_metadata": {
                            "source_format": "anthropic",
                            "source_model": "claude-test",
                            "native_blocks": [{"type": "thinking", "signature": "private-signature"}],
                        },
                    },
                )
            else:
                result = SimpleNamespace(
                    text="verified report\n\nCOMPLETION_STATUS: completed",
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": "verified report"},
                )
            yield {"type": "done", "result": result}

        def append_assistant_message(self, messages, raw_message):
            messages.append(raw_message)

        def format_tool_result(self, tool_id, tool_name, output):
            return {"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": output}

    client = FakeClient()
    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})):
        result = await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Complete the test task.",
            trace_log,
            client,
        )

    assert result["report"].endswith("COMPLETION_STATUS: completed")
    assert client.calls == 2
    assert client.requests[1][0]["role"] == "system"
    assert "private-signature" not in json.dumps(client.requests[1])
    assert "native_blocks" not in json.dumps(client.requests[1])


@pytest.mark.anyio
async def test_sub_agent_loop_awaits_async_tool_handlers(tmp_path):
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"
    async_handler = AsyncMock(return_value={"value": 4})

    class AsyncTool(ToolArgumentsModel):
        value: int

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.calls += 1
            self.requests.append(list(messages))
            if self.calls == 1:
                result = SimpleNamespace(
                    text="",
                    tool_calls=[{
                        "id": "call_1",
                        "name": "AsyncTool",
                        "arguments": '{"value": 2}',
                    }],
                    assistant_message={"role": "assistant", "content": None},
                )
            elif self.calls == 2:
                result = SimpleNamespace(
                    text="done",
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": "done"},
                )
            else:
                result = SimpleNamespace(
                    text="report\n\nCOMPLETION_STATUS: completed",
                    tool_calls=[],
                    assistant_message={"role": "assistant", "content": "report"},
                )
            yield {"type": "done", "result": result}

        def append_assistant_message(self, messages, raw_message):
            messages.append(raw_message)

        def format_tool_result(self, tool_id, tool_name, output):
            return {"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": output}

    client = FakeClient()
    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.COMMON_TOOLS_HANDLERS", {"AsyncTool": async_handler}), \
            patch("utils.teams.SUB_AGENT_TOOL_MODELS", {"AsyncTool": AsyncTool}), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})):
        result = await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Use the async tool.",
            trace_log,
            client,
        )

    async_handler.assert_awaited_once_with(value=2)
    assert result["report"].endswith("COMPLETION_STATUS: completed")
    assert client.requests[1][-1]["content"] == {"value": 4}


@pytest.mark.anyio
async def test_sub_agent_returns_builtin_validation_error_without_calling_handler(tmp_path):
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"
    invalid_call = {
        "id": "call_invalid",
        "name": "ContentSearch",
        "arguments": json.dumps({
            "content_regex": "TODO",
            "search_dir": ".",
            "filename": "*.py",
            "context_size": 1,
        }),
    }
    handler = Mock()

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.calls += 1
            self.requests.append(list(messages))
            if self.calls == 1:
                result = SimpleNamespace(
                    text="",
                    tool_calls=[invalid_call],
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

        def append_assistant_message(self, messages, raw_message):
            messages.append(raw_message)

        def format_tool_result(self, tool_id, tool_name, output):
            return {"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": output}

    client = FakeClient()
    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.COMMON_TOOLS_HANDLERS", {"ContentSearch": handler}), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})):
        result = await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Use ContentSearch.",
            trace_log,
            client,
        )

    assert result["report"].endswith("COMPLETION_STATUS: completed")
    handler.assert_not_called()
    tool_result = client.requests[1][-1]
    assert tool_result["is_error"] is True
    assert "filename" in tool_result["content"]
    assert "filename_regex" in tool_result["content"]
    assert "_regex>" not in tool_result["content"]


@pytest.mark.anyio
async def test_sub_agent_resumes_pause_turn_in_main_loop_and_completion_report(tmp_path):
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.requests.append(list(messages))
            self.calls += 1
            if self.calls == 1:
                result = SimpleNamespace(
                    text="partial work",
                    tool_calls=[],
                    stop_reason="pause_turn",
                    assistant_message={
                        "role": "assistant",
                        "content": "partial work",
                        "stop_reason": "pause_turn",
                    },
                )
            elif self.calls == 2:
                result = SimpleNamespace(
                    text="work done",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "work done"},
                )
            elif self.calls == 3:
                result = SimpleNamespace(
                    text="partial report ",
                    tool_calls=[],
                    stop_reason="pause_turn",
                    assistant_message={
                        "role": "assistant",
                        "content": "partial report ",
                        "stop_reason": "pause_turn",
                    },
                )
            else:
                result = SimpleNamespace(
                    text="complete\n\nCOMPLETION_STATUS: completed",
                    tool_calls=[],
                    stop_reason="end_turn",
                    assistant_message={"role": "assistant", "content": "complete"},
                )
            yield {"type": "done", "result": result}

        def append_assistant_message(self, messages, raw_message):
            messages.append(raw_message)

        def format_tool_result(self, tool_id, tool_name, output):
            return {"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": output}

    client = FakeClient()
    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})):
        result = await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Complete the task.",
            trace_log,
            client,
        )

    assert client.calls == 4
    assert client.requests[1][-1]["stop_reason"] == "pause_turn"
    assert client.requests[3][-1]["stop_reason"] == "pause_turn"
    assert result["report"] == "partial report complete\n\nCOMPLETION_STATUS: completed"


@pytest.mark.anyio
async def test_sub_agent_marks_unknown_tool_result_as_error(tmp_path):
    manager = TeammateManager(tmp_path / "team")
    trace_log = tmp_path / "trace.jsonl"

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def format_tools(self, tools):
            return []

        async def generate_stream(self, messages, tools):
            self.requests.append(list(messages))
            self.calls += 1
            if self.calls == 1:
                result = SimpleNamespace(
                    text="",
                    tool_calls=[{"id": "call_1", "name": "MissingTool", "arguments": "{}"}],
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

        def append_assistant_message(self, messages, raw_message):
            messages.append(raw_message)

        def format_tool_result(self, tool_id, tool_name, output):
            return {"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": output}

    client = FakeClient()
    tool_history = ToolExecutionHistory()
    with patch("utils.teams.get_sub_agent_console", return_value=False), \
            patch("utils.teams.GLOBAL_MCP_MANAGER.get_registry_snapshot", return_value=([], {})), \
            patch("utils.teams.TOOL_EXECUTION_HISTORY", tool_history):
        await manager._sub_agent_loop(
            "1",
            "Test Engineer",
            "Use the missing tool.",
            trace_log,
            client,
        )

    tool_result = client.requests[1][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["name"] == "MissingTool"
    assert tool_result["is_error"] is True
    history_record = tool_history.snapshot()[0]
    assert history_record.source == "sub_agent"
    assert history_record.actor == "#1 - Test Engineer"
    assert history_record.task_id == "1"
    assert history_record.status == TOOL_STATUS_FAILED


class DelegateModalHost(App):
    def __init__(self, modal: DelegateTasksModal, on_dismiss=None):
        super().__init__()
        self._modal = modal
        self._on_dismiss = on_dismiss

    def compose(self) -> ComposeResult:
        yield Label("host")

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._on_dismiss)


@pytest.mark.anyio
async def test_delegate_modal_renders_tasks_and_actions():
    modal = DelegateTasksModal(
        [
            {
                "task_id": "1",
                "role_name": "Code Reviewer",
                "summary": "Review the delegated change.",
            },
            {
                "task_id": "2",
                "role_name": "Test Engineer",
                "summary": "Run focused regression tests.",
            },
        ]
    )
    app = DelegateModalHost(modal)

    async with app.run_test() as pilot:
        await pilot.pause()
        headings = modal.query(".delegate-card-heading")
        summaries = modal.query(".delegate-card-summary")
        assert len(modal.query(".delegate-card")) == 2
        assert "TASK #1  ·  Code Reviewer" in str(headings[0].render())
        assert "TASK #2  ·  Test Engineer" in str(headings[1].render())
        assert "Review the delegated change." in str(summaries[0].render())
        assert "Run focused regression tests." in str(summaries[1].render())
        assert "并行启动子智能体" in str(modal.query_one("#delegate-action-help", Label).render())
        assert modal.query_one("#delegate-approve", Button).label.plain == "批准委派"
        assert modal.query_one("#delegate-orchestrator", Button).label.plain == "主智能体执行"
        assert modal.query_one("#delegate-cancel", Button).label.plain == "取消"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    [
        ("delegate-approve", "approve"),
        ("delegate-orchestrator", "orchestrator"),
        ("delegate-cancel", "cancel"),
    ],
)
async def test_delegate_modal_returns_selected_action(button_id, expected):
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    modal = DelegateTasksModal(
        [{"task_id": "1", "role_name": "Reviewer", "summary": "Review."}]
    )
    app = DelegateModalHost(modal, on_dismiss)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click(f"#{button_id}")
        await pilot.pause()

    assert result == expected


@pytest.mark.anyio
async def test_delegate_modal_escape_cancels():
    result = None

    def on_dismiss(value):
        nonlocal result
        result = value

    modal = DelegateTasksModal(
        [{"task_id": "1", "role_name": "Reviewer", "summary": "Review."}]
    )
    app = DelegateModalHost(modal, on_dismiss)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert result == "cancel"


@pytest.mark.anyio
async def test_delegate_modal_long_text_wraps_in_narrow_terminal():
    long_text = "LongUnbrokenDelegationSummary" * 12
    modal = DelegateTasksModal(
        [
            {
                "task_id": "1",
                "role_name": "ExtremelyLongSpecializedReviewerRole" * 3,
                "summary": long_text,
            }
        ]
    )
    app = DelegateModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        content = modal.query_one(".delegate-card-summary", Label)
        heading = modal.query_one(".delegate-card-heading", Label)
        tasks = modal.query_one("#delegate-tasks", VerticalScroll)
        assert content.size.width <= tasks.size.width
        assert heading.size.width <= tasks.size.width
        assert content.size.height > 1
        assert heading.size.height > 1
        assert modal.query_one("#delegate-approve", Button).region.height > 0
        assert modal.query_one("#delegate-orchestrator", Button).region.height > 0
        assert modal.query_one("#delegate-cancel", Button).region.height > 0


@pytest.mark.anyio
async def test_delegate_modal_many_tasks_scroll_in_narrow_terminal():
    modal = DelegateTasksModal(
        [
            {
                "task_id": str(index),
                "role_name": f"Role {index}",
                "summary": "Detailed delegated task summary " * 5,
            }
            for index in range(1, 13)
        ]
    )
    app = DelegateModalHost(modal)

    async with app.run_test(size=(62, 25)) as pilot:
        await pilot.pause()
        tasks = modal.query_one("#delegate-tasks", VerticalScroll)
        content = modal.query_one("#delegate-tasks-content", Vertical)
        assert content.virtual_size.height > tasks.size.height
        assert tasks.max_scroll_y > 0
        assert modal.query_one("#delegate-actions").region.height == 3


if __name__ == "__main__":
    unittest.main()
