import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Label

from system.tui_modals import DelegateTasksModal
from utils.teams import DelegateTasks, TeammateManager


class DelegateConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manager = TeammateManager(Path(self.temp_dir.name) / "team")
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

    def _delegate(self, action: str):
        run_dir = Path(self.temp_dir.name) / "runs"
        run_dir.mkdir()

        def run_async(coroutine):
            coroutine.close()
            return []

        with (
            patch.object(self.manager, "_validate_delegation_tasks", return_value=self.tasks),
            patch("utils.teams.choose_delegate_tasks_tui", return_value=action) as choose,
            patch("utils.teams.request_window_attention"),
            patch("utils.teams.post_tui"),
            patch("utils.teams._runs_dir", return_value=run_dir),
            patch("utils.teams.asyncio.run", side_effect=run_async) as run,
            patch("utils.hitl.check_permission") as hitl,
        ):
            result = self.manager.delegate_concurrently(self.tasks)
        hitl.assert_not_called()
        return result, choose, run

    def test_approve_starts_delegation(self):
        result, choose, run = self._delegate("approve")

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
        run.assert_called_once()
        self.assertIn("Sub-Agents Execution Reports", result)

    def test_orchestrator_choice_does_not_start_delegation(self):
        result, _, run = self._delegate("orchestrator")

        run.assert_not_called()
        self.assertIn("Orchestrator", result)
        self.assertIn("Do not call DelegateTasks again", result)

    def test_cancel_does_not_start_delegation(self):
        result, _, run = self._delegate("cancel")

        run.assert_not_called()
        self.assertEqual("Sub-agent delegation cancelled by the user.", result)


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
