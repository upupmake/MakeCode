import unittest
from unittest.mock import patch

from prompts import (
    get_orchestrator_system_prompt,
    get_report_assistant_system_prompt,
    get_sub_agent_summary_prompt,
    get_sub_agent_system_prompt,
    get_summary_user_prompt,
    get_title_generation_system_prompt,
)
from utils.common import sanitize_title
from utils.skills import SKILL_LOADER


class PromptPolicyTests(unittest.TestCase):
    def _orchestrator_prompt(self, plan_mode: bool) -> str:
        with patch.object(SKILL_LOADER, "render_prompt_block", return_value=""):
            return get_orchestrator_system_prompt(
                "/workspace",
                "zsh",
                "test",
                plan_mode=plan_mode,
            )

    def _sub_agent_prompt(self) -> str:
        with patch.object(SKILL_LOADER, "render_prompt_block", return_value=""):
            return get_sub_agent_system_prompt(
                "Tester",
                "/workspace",
                "zsh",
                "test",
            )

    def test_sub_agent_prompt_does_not_expose_delegation_concepts(self):
        prompt = self._sub_agent_prompt()

        self.assertNotIn("DelegateTasks", prompt)
        self.assertNotIn("dedicated sub-agent confirmation dialog", prompt)
        self.assertNotIn("Approve Delegation", prompt)
        self.assertNotIn("Orchestrator Execution", prompt)
        self.assertNotIn("MUST confirm with the user first", prompt)
        self.assertIn("Otherwise report the blocker clearly", prompt)

    def test_orchestrator_prompt_defines_all_delegation_dialog_outcomes(self):
        prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertIn("Approve Delegation starts the selected sub-agents", prompt)
        self.assertIn("execute that batch directly and must not delegate it again", prompt)
        self.assertIn("must neither delegate nor execute that batch automatically", prompt)
        self.assertIn("return control to the user", prompt)

    def test_act_mode_uses_task_manager_only_for_multi_step_planning(self):
        prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertIn("Use TaskManager for tasks that require multi-step planning", prompt)
        self.assertIn("execute simple single-step tasks directly without creating a task plan", prompt)
        self.assertNotIn("Always plan work with TaskManager first", prompt)
        self.assertIn("Execution loop for tasks that require multi-step planning", prompt)

    def test_orchestrator_prompt_limits_delegation_to_worthwhile_parallel_batches(self):
        prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertIn("at least two runnable tasks", prompt)
        self.assertIn("substantial enough or sufficiently well-suited to parallel execution", prompt)
        self.assertIn("Execute single tasks, serial task chains, and batches of trivial tasks directly", prompt)
        self.assertIn("sub-agents are not a substitute for unavailable background-task execution", prompt)

    def test_plan_mode_separates_destructive_plan_reset(self):
        prompt = self._orchestrator_prompt(plan_mode=True)
        planning_line = next(
            line for line in prompt.splitlines()
            if line.startswith(" - TaskManager planning tools")
        )

        self.assertNotIn("DeleteAllTasks", planning_line)
        self.assertIn("Destructive plan reset:", prompt)
        self.assertIn("DeleteAllTasks — use only when the user explicitly requests", prompt)
        self.assertIn("requires confirmation", prompt)

    def test_prompts_follow_the_users_language(self):
        expected = "Respond in the user's language"

        self.assertIn(expected, self._orchestrator_prompt(plan_mode=False))
        self.assertIn(expected, self._sub_agent_prompt())

    def test_orchestrator_prompt_includes_dynamic_mcp_config_path(self):
        with patch("prompts.paths.mcp_config_file", return_value="/config/mcp_config.json"):
            prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertIn("MCP configuration file: /config/mcp_config.json", prompt)

    def test_mode_prompts_keep_one_current_mode_statement(self):
        plan_prompt = self._orchestrator_prompt(plan_mode=True)
        act_prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertEqual(1, plan_prompt.count("You are in Plan Mode"))
        self.assertEqual(1, act_prompt.count("You are in Act Mode"))
        self.assertIn("Do not modify files, execute modification commands, or delegate tasks", plan_prompt)
        self.assertIn("stop modifications immediately and return to read-only planning", act_prompt)
        self.assertIn("Before delegation, call GetRunnableTasks", act_prompt)
        self.assertIn("user request or goal, limits and constraints", act_prompt)
        self.assertIn("allowed and disallowed scope", act_prompt)
        self.assertIn("expected output, verification evidence", act_prompt)

    def test_each_agent_prompt_has_one_verification_policy(self):
        orchestrator_prompt = self._orchestrator_prompt(plan_mode=False)
        sub_agent_prompt = self._sub_agent_prompt()

        self.assertEqual(1, orchestrator_prompt.count("# Verification Before Completion"))
        self.assertEqual(1, sub_agent_prompt.count("# Verification Before Completion"))
        self.assertEqual(1, orchestrator_prompt.count("If verification is unavailable"))
        self.assertEqual(1, sub_agent_prompt.count("If verification is unavailable"))
        self.assertIn("Independently verify delegated reports", orchestrator_prompt)
        self.assertIn("Report the concrete evidence", sub_agent_prompt)

    def test_recovery_report_prompts_keep_compact_machine_readable_protocol(self):
        summary_prompt = get_sub_agent_summary_prompt(
            executed_steps=4,
            max_steps=40,
            todo_snapshot="[>] verify",
            messages_text="[]",
        )
        assistant_prompt = get_report_assistant_system_prompt()

        for prompt in (summary_prompt, assistant_prompt):
            self.assertIn("## Status", prompt)
            self.assertIn("## Completed Work", prompt)
            self.assertIn("## Verification", prompt)
            self.assertIn("## Remaining Work", prompt)
            self.assertIn("## Blockers", prompt)
            self.assertIn("COMPLETION_STATUS: completed", prompt)
            self.assertIn("COMPLETION_STATUS: not_completed", prompt)
            self.assertNotIn("extremely detailed", prompt.lower())
            self.assertNotIn("Confidence Assessment", prompt)

        self.assertIn("Executed steps: 4/40", summary_prompt)
        self.assertIn("[>] verify", summary_prompt)

    def test_sub_agent_uses_todos_only_for_multi_step_tasks(self):
        prompt = self._sub_agent_prompt()

        self.assertIn("context_prompt is authoritative for task scope", prompt)
        self.assertIn("cannot expand or override the context_prompt", prompt)
        self.assertNotIn("SOLE source of truth", prompt)
        self.assertIn("Use TodoUpdate for multi-step tasks", prompt)
        self.assertIn("execute genuinely single-step tasks directly", prompt)
        self.assertNotIn("Call TodoUpdate to create", prompt)

    def test_act_mode_resolves_uncertainty_by_risk(self):
        prompt = self._orchestrator_prompt(plan_mode=False)

        self.assertIn("First use read-only inspection", prompt)
        self.assertIn("user-visible behavior, data, architecture, scope, or irreversible outcomes", prompt)
        self.assertIn("For low-risk implementation details, choose the smallest reasonable option", prompt)
        self.assertNotIn("Resolve ambiguous requirements with the user before creating tasks", prompt)

    def test_compaction_prompt_preserves_execution_continuity(self):
        prompt = get_summary_user_prompt("context limit")

        for expected in (
            "modified or newly created files",
            "Remaining tasks and exact next steps",
            "Verification commands or checks and their results",
            "Explicit user constraints, preferences, and approved choices",
            "Errors, blockers, unresolved questions",
            "Current TaskManager task statuses and dependencies",
        ):
            self.assertIn(expected, prompt)
        self.assertIn("Compaction reason: context limit", prompt)

    def test_compaction_prompt_only_preserves_memories_that_clearly_affected_the_round(self):
        prompt = get_summary_user_prompt("context limit")
        reminder = "后续如果当前上下文中缺少用户请求相关的记忆，可先进行一次记忆召回，但召回内容仅供参考。"

        self.assertIn("## Applied long-term memories", prompt)
        self.assertIn("conversation span represented by the provided JSON dump", prompt)
        self.assertIn("later assistant decision, tool action, implementation choice, verification command, or answer", prompt)
        self.assertIn("can be specifically connected to it", prompt)
        self.assertIn("A memory being recalled, potentially relevant, mentioned, or topically similar is not evidence", prompt)
        self.assertIn("If uncertain, treat it as not applied", prompt)
        self.assertIn("Preserve each applied memory ID exactly as recalled", prompt)
        self.assertIn("Memory IDs with the same concrete effect may be grouped in one list item", prompt)
        self.assertIn("- `<memory_id>`, `<memory_id>` — <brief concrete effect>", prompt)
        self.assertIn("wrap every memory ID in backticks", prompt)
        self.assertIn("place the reminder exactly once after the list", prompt)
        self.assertIn(reminder, prompt)
        self.assertIn("This section must be the final part of the summary", prompt)
        self.assertIn("no recalled memory clearly affected the represented conversation span", prompt)
        self.assertIn("omit the heading, memory IDs, explanations, and reminder sentence entirely", prompt)

    def test_title_prompt_has_explicit_unicode_character_limit(self):
        prompt = get_title_generation_system_prompt()

        self.assertIn("MUST be between 1 and 30 Unicode characters", prompt)
        self.assertIn("counting spaces and punctuation", prompt)
        self.assertNotIn("under 15 characters recommended", prompt)
        self.assertIn("filename-safe", prompt)
        long_title = "a" * 50
        self.assertEqual(long_title, sanitize_title(long_title))

    def test_title_prompt_prioritizes_current_user_request_over_recalled_memory(self):
        prompt = get_title_generation_system_prompt()

        self.assertIn("# Current User Request", prompt)
        self.assertIn("# Potentially Relevant Memories", prompt)
        self.assertIn("Ignore recalled memory when choosing the title topic", prompt)
        self.assertIn("If no Current User Request section exists", prompt)


if __name__ == "__main__":
    unittest.main()
