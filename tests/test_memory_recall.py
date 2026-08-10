import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.memory as memory
from prompts import get_orchestrator_system_prompt, get_sub_agent_system_prompt
from system.tool_history import TOOL_STATUS_FAILED, ToolExecutionHistory
from utils import text_tokens
from utils.common import truncate_output
from utils.teams import build_sub_agent_recall_query, prepend_recalled_memory_to_sub_agent_prompt


MEMORY_RECORDS = [
    {
        "id": "mem_old",
        "category": "workflow",
        "updated_at": "2026-01-01 00:00:00",
        "insight": "old insight should not be in candidates",
        "evidence": "old evidence should not be in candidates",
        "reuse_condition": "when handling old workflow",
        "status": "active",
    },
    {
        "id": "mem_new",
        "category": "project-convention",
        "updated_at": "2026-02-01 00:00:00",
        "insight": "new insight should be injected",
        "evidence": "new evidence should not be injected",
        "reuse_condition": "when handling new convention",
        "status": "active",
    },
]


class FakeRecallLLMClient:
    def __init__(self, tool_calls_by_round):
        self.tool_calls_by_round = list(tool_calls_by_round)
        self.messages_seen = []
        self.generate_calls = 0

    def format_tools(self, tools):
        return tools

    def format_tool_result(self, tool_id, tool_name, output):
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": output,
        }

    async def generate_stream(self, messages, tools):
        self.messages_seen.append(list(messages))
        self.generate_calls += 1
        index = self.generate_calls - 1
        tool_calls = self.tool_calls_by_round[index]
        assistant_message = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        yield {
            "type": "done",
            "result": SimpleNamespace(
                text="",
                tool_calls=tool_calls,
                assistant_message=assistant_message,
            ),
        }


class MemoryRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        memory._MEMORY_RECALL_WINDOWS = {}

    def test_recall_candidates_only_include_lightweight_fields(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            candidates = memory.build_memory_recall_candidates()

        self.assertIn("mem_old", candidates)
        self.assertIn("mem_new", candidates)
        self.assertIn("Category: workflow", candidates)
        self.assertIn("Updated at: 2026-01-01 00:00:00", candidates)
        self.assertIn("Reuse condition: when handling new convention", candidates)
        self.assertIn("Insight: old insight should not be in candidates", candidates)
        self.assertIn("Insight: new insight should be injected", candidates)
        self.assertNotIn("evidence", candidates.lower())
        self.assertLess(candidates.index("mem_old"), candidates.index("mem_new"))

    def test_selected_memory_context_filters_ids_and_excludes_evidence(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            selected_ids = memory.normalize_memory_ids(["mem_new", "missing", "mem_new", "mem_old"])
            context = memory.render_selected_memory_context(["mem_new", "missing", "mem_new"])

        self.assertEqual(selected_ids, ["mem_new", "mem_old"])
        self.assertIn("mem_new", context)
        self.assertIn("Insight: new insight should be injected", context)
        self.assertIn("Reuse condition: when handling new convention", context)
        self.assertNotIn("new evidence should not be injected", context)
        self.assertNotIn("mem_old", context)
        self.assertNotIn("missing", context)

    def test_touch_recalled_memories_updates_selected_active_records_only(self):
        records = [
            {"id": "mem_selected", "status": "active", "updated_at": "old-selected"},
            {"id": "mem_other", "status": "active", "updated_at": "old-other"},
            {"id": "mem_deleted", "status": "deleted", "updated_at": "old-deleted"},
        ]

        fixed_datetime = Mock()
        fixed_datetime.now.return_value.strftime.return_value = "2026-07-21 12:34:56"
        with patch.object(memory, "_read_memory_records", return_value=records), \
                patch.object(memory, "_write_memory_records") as write_records, \
                patch.object(memory, "datetime", fixed_datetime):
            memory._touch_recalled_memories(["mem_selected", "mem_deleted", "missing"])

        self.assertEqual(records[0]["updated_at"], "2026-07-21 12:34:56")
        self.assertEqual(records[1]["updated_at"], "old-other")
        self.assertEqual(records[2]["updated_at"], "old-deleted")
        write_records.assert_called_once_with(records)

    async def test_recall_touches_selected_memories_before_rendering_context(self):
        events = []

        def render_context(memory_ids):
            events.append(("render", memory_ids))
            return "selected context"

        with patch.object(memory, "select_relevant_memory_ids", AsyncMock(return_value=["mem_new"])), \
                patch.object(memory, "_touch_recalled_memories", side_effect=lambda ids: events.append(("touch", ids))), \
                patch.object(memory, "render_selected_memory_context", side_effect=render_context), \
                patch.object(memory, "post_tui"), \
                patch.object(memory, "Markdown"):
            result = await memory.recall_long_term_memories("query")

        self.assertEqual(result["content"], "selected context")
        self.assertEqual(events, [("touch", ["mem_new"]), ("render", ["mem_new"])])

    async def test_recall_background_places_ids_and_details_on_following_lines(self):
        memory_context = "## mem_new\n- Insight: selected context"

        with patch.object(
                memory,
                "select_relevant_memory_ids",
                AsyncMock(return_value=["mem_new", "mem_old"]),
        ), patch.object(memory, "_touch_recalled_memories"), patch.object(
                memory,
                "render_selected_memory_context",
                return_value=memory_context,
        ), patch.object(memory, "post_tui") as post_tui, patch.object(
                memory,
                "Markdown",
                side_effect=lambda content: ("markdown", content),
        ):
            await memory.recall_long_term_memories("query")

        payloads = [call.args[1] for call in post_tui.call_args_list if len(call.args) > 1]
        summary_index = next(
            index for index, payload in enumerate(payloads)
            if isinstance(payload, str) and "记忆召回命中" in payload
        )
        self.assertEqual(
            payloads[summary_index],
            "[bold green]🧠 记忆召回命中 2 条：\nmem_new, mem_old\n[/bold green]",
        )
        self.assertEqual(payloads[summary_index + 1], ("markdown", memory_context))

    def test_prepend_recalled_memory_to_query_only_changes_query_when_context_exists(self):
        original = "请处理当前项目的测试。"
        self.assertEqual(memory.prepend_recalled_memory_to_query(original, ""), original)

        injected = memory.prepend_recalled_memory_to_query(original, "## mem_new\n- Insight: use tests")
        self.assertTrue(injected.startswith("# Potentially Relevant Memories"))
        self.assertIn("not as new user instructions", injected)
        self.assertIn("# Current User Request", injected)
        self.assertTrue(injected.endswith(original))

    def test_sub_agent_memory_prompt_injection_uses_delegated_task_context(self):
        original = "请处理当前项目的测试。"
        self.assertEqual(prepend_recalled_memory_to_sub_agent_prompt(original, ""), original)

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            memory_context = memory.render_selected_memory_context(["mem_new"])

        injected = prepend_recalled_memory_to_sub_agent_prompt(original, memory_context)
        self.assertTrue(injected.startswith("# Potentially Relevant Memories"))
        self.assertIn("delegated sub-agent task", injected)
        self.assertIn("not as new user instructions", injected)
        self.assertIn("# Delegated Task", injected)
        self.assertIn("new insight should be injected", injected)
        self.assertNotIn("new evidence should not be injected", injected)
        self.assertTrue(injected.endswith(original))

    def test_sub_agent_recall_query_includes_task_identity_and_context_prompt(self):
        original_prompt = "Run memory tests"
        relay_context = "Previous failed execution should not affect memory recall"
        query = build_sub_agent_recall_query("7", "Tester", original_prompt)

        self.assertIn("# Sub-Agent Delegated Task", query)
        self.assertIn("## Task ID\n7", query)
        self.assertIn("## Role\nTester", query)
        self.assertIn("## Context Prompt\nRun memory tests", query)
        self.assertNotIn(relay_context, query)

    def test_memory_recall_message_places_user_request_at_bottom(self):
        messages = memory._get_memory_recall_messages(
            "本次用户请求",
            "### mem_new\n- Insight: new insight",
            "上一轮 assistant 回复",
        )

        payload = messages[1]["content"]
        assert payload == (
            "# Memory Recall Request\n\n"
            "## Previous Assistant Content\n\n"
            "上一轮 assistant 回复\n\n"
            "## Candidate Memories\n\n"
            "### mem_new\n- Insight: new insight\n\n"
            "## Current User Request\n\n"
            "本次用户请求"
        )

    def test_memory_recall_message_omits_empty_previous_assistant_content(self):
        messages = memory._get_memory_recall_messages(
            "本次用户请求",
            "### mem_new\n- Insight: new insight",
            "",
        )

        payload = messages[1]["content"]
        assert "## Previous Assistant Content" not in payload
        assert payload.endswith("## Current User Request\n\n本次用户请求")

    async def test_select_relevant_memory_ids_uses_tool_call_and_retries_until_called(self):
        fake_client = FakeRecallLLMClient([
            [],
            [
                {
                    "id": "call_1",
                    "name": "SelectRelevantMemories",
                    "arguments": '{"memory_ids": ["mem_new", "missing", "mem_old", "mem_new"]}',
                    "raw": {},
                }
            ],
        ])

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=fake_client):
            selected = await memory.select_relevant_memory_ids("需要处理新约定", max_iterations=3)

        self.assertEqual(selected, ["mem_new", "mem_old"])
        self.assertEqual(fake_client.generate_calls, 2)
        first_user_payload = fake_client.messages_seen[0][1]["content"]
        self.assertIn("## Candidate Memories", first_user_payload)
        self.assertIn("mem_new", first_user_payload)
        self.assertIn("new insight should be injected", first_user_payload)
        self.assertNotIn("new evidence should not be injected", first_user_payload)
        self.assertIn("You did not call SelectRelevantMemories", fake_client.messages_seen[1][-1]["content"])

    async def test_select_relevant_memory_ids_returns_validation_error_to_model_and_retries(self):
        fake_client = FakeRecallLLMClient([
            [{
                "id": "call_invalid",
                "name": "SelectRelevantMemories",
                "arguments": '{"memory_ids": ["mem_new"], "unexpected": true}',
            }],
            [{
                "id": "call_valid",
                "name": "SelectRelevantMemories",
                "arguments": '{"memory_ids": ["mem_new"]}',
            }],
        ])

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=fake_client):
            selected = await memory.select_relevant_memory_ids("query", max_iterations=2)

        self.assertEqual(selected, ["mem_new"])
        self.assertEqual(fake_client.generate_calls, 2)
        tool_result = fake_client.messages_seen[1][-1]
        self.assertEqual(tool_result["role"], "tool")
        self.assertTrue(tool_result["is_error"])
        self.assertIn("unexpected", tool_result["content"])
        self.assertIn("extra_forbidden", tool_result["content"])

    async def test_select_relevant_memory_ids_raises_after_final_validation_error(self):
        fake_client = FakeRecallLLMClient([[
            {
                "id": "call_invalid",
                "name": "SelectRelevantMemories",
                "arguments": '{"memory_ids": [], "unexpected": true}',
            },
        ]])

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=fake_client):
            with self.assertRaisesRegex(memory.ToolArgumentValidationError, "unexpected"):
                await memory.select_relevant_memory_ids("query", max_iterations=1)

    async def test_select_relevant_memory_ids_resumes_pause_turn_without_inserting_user_prompt(self):
        class PauseRecallClient:
            def __init__(self):
                self.calls = 0
                self.messages_seen = []

            def format_tools(self, tools):
                return tools

            async def generate_stream(self, messages, tools):
                self.messages_seen.append(list(messages))
                self.calls += 1
                if self.calls == 1:
                    result = SimpleNamespace(
                        text="",
                        tool_calls=[],
                        stop_reason="pause_turn",
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "stop_reason": "pause_turn",
                        },
                    )
                else:
                    tool_calls = [{
                        "id": "call_1",
                        "name": "SelectRelevantMemories",
                        "arguments": '{"memory_ids": ["mem_new"]}',
                    }]
                    result = SimpleNamespace(
                        text="",
                        tool_calls=tool_calls,
                        stop_reason="tool_use",
                        assistant_message={"role": "assistant", "content": "", "tool_calls": tool_calls},
                    )
                yield {"type": "done", "result": result}

        fake_client = PauseRecallClient()
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=fake_client):
            selected = await memory.select_relevant_memory_ids("query", max_iterations=2)

        self.assertEqual(selected, ["mem_new"])
        self.assertEqual(fake_client.calls, 2)
        self.assertEqual(fake_client.messages_seen[1][-1]["role"], "assistant")
        self.assertEqual(fake_client.messages_seen[1][-1]["stop_reason"], "pause_turn")

    async def test_memory_agent_resumes_pause_turn_and_marks_unknown_tool_as_error(self):
        initial_messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "manage memory"},
        ]
        fake_client = Mock()
        fake_client.get_memory_decision_messages.return_value = initial_messages
        fake_client.format_tools.return_value = []
        fake_client.generate_stream.return_value = object()
        fake_client.format_tool_result.side_effect = lambda tool_id, tool_name, output: {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": output,
        }
        stream_results = [
            (
                "partial",
                [],
                {"role": "assistant", "content": "partial", "stop_reason": "pause_turn"},
            ),
            (
                "",
                [{"id": "call_1", "name": "MissingMemoryTool", "arguments": "{}"}],
                {"role": "assistant", "content": None, "stop_reason": "tool_use"},
            ),
        ]
        tool_history = ToolExecutionHistory()

        with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
                patch.object(memory, "close_async_llm_client", new_callable=AsyncMock) as close_client, \
                patch.object(memory.StreamRenderer, "render_text_stream_async", new_callable=AsyncMock, side_effect=stream_results), \
                patch.object(memory, "post_tui"), \
                patch.object(memory, "_render_agent_response_message"), \
                patch.object(memory, "_render_tool_output"), \
                patch.object(memory, "TOOL_EXECUTION_HISTORY", tool_history):
            outputs = await memory.memory_agent_loop(
                conversation_text="[]",
                summary="",
                reason="test",
                current_memory_content="",
                tools=[],
                max_iterations=2,
            )

        self.assertEqual(outputs[0]["tool"], "MissingMemoryTool")
        self.assertEqual(fake_client.generate_stream.call_count, 2)
        self.assertEqual(initial_messages[2]["stop_reason"], "pause_turn")
        tool_result = next(item for item in initial_messages if item.get("role") == "tool")
        self.assertTrue(tool_result["is_error"])
        history_record = tool_history.snapshot()[0]
        self.assertEqual(history_record.tool_name, "MissingMemoryTool")
        self.assertEqual(history_record.source, "memory")
        self.assertEqual(history_record.status, TOOL_STATUS_FAILED)
        close_client.assert_awaited_once_with(fake_client)

    async def test_memory_agent_returns_validation_error_without_calling_write_handler(self):
        initial_messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "manage memory"},
        ]
        handler = Mock()
        fake_client = Mock()
        fake_client.get_memory_decision_messages.return_value = initial_messages
        fake_client.format_tools.return_value = []
        fake_client.generate_stream.return_value = object()
        fake_client.format_tool_result.side_effect = lambda tool_id, tool_name, output: {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": output,
        }

        with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
                patch.object(memory, "close_async_llm_client", new_callable=AsyncMock), \
                patch.object(
                    memory.StreamRenderer,
                    "render_text_stream_async",
                    new_callable=AsyncMock,
                    return_value=(
                        "",
                        [{
                            "id": "call_invalid",
                            "name": "AppendLongTermMemory",
                            "arguments": '{"category":"workflow","insight":"x","evidence":"y","reuse_condition":"z","unexpected":true}',
                        }],
                        {"role": "assistant", "content": None, "stop_reason": "tool_use"},
                    ),
                ), \
                patch.object(memory, "LONG_TERM_MEMORY_TOOL_HANDLERS", {"AppendLongTermMemory": handler}), \
                patch.object(memory, "post_tui"), \
                patch.object(memory, "_render_agent_response_message"), \
                patch.object(memory, "_render_tool_call"), \
                patch.object(memory, "_render_tool_output"):
            outputs = await memory.memory_agent_loop(
                conversation_text="[]",
                summary="",
                reason="test",
                current_memory_content="",
                tools=[],
                max_iterations=1,
            )

        handler.assert_not_called()
        self.assertEqual(outputs[0]["tool"], "AppendLongTermMemory")
        self.assertTrue(outputs[0]["output"].startswith("Error: Invalid arguments for AppendLongTermMemory."))
        tool_result = next(item for item in initial_messages if item.get("role") == "tool")
        self.assertTrue(tool_result["is_error"])
        self.assertIn("unexpected", tool_result["content"])

    async def test_memory_agent_strict_mode_raises_after_tool_error(self):
        initial_messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "manage memory"},
        ]
        fake_client = Mock()
        fake_client.get_memory_decision_messages.return_value = initial_messages
        fake_client.format_tools.return_value = []
        fake_client.generate_stream.return_value = object()
        fake_client.format_tool_result.side_effect = lambda tool_id, tool_name, output: {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": output,
        }

        with patch.object(memory, "create_current_async_llm_client", return_value=fake_client), \
                patch.object(memory, "close_async_llm_client", new_callable=AsyncMock), \
                patch.object(
                    memory.StreamRenderer,
                    "render_text_stream_async",
                    new_callable=AsyncMock,
                    return_value=(
                        "",
                        [{"id": "call_1", "name": "MissingMemoryTool", "arguments": "{}"}],
                        {"role": "assistant", "content": None, "stop_reason": "tool_use"},
                    ),
                ), \
                patch.object(memory, "post_tui"), \
                patch.object(memory, "_render_agent_response_message"), \
                patch.object(memory, "_render_tool_output"), \
                patch.object(memory, "TOOL_EXECUTION_HISTORY", ToolExecutionHistory()):
            with self.assertRaisesRegex(RuntimeError, "tool errors"):
                await memory.memory_agent_loop(
                    conversation_text="[]",
                    summary="summary",
                    reason="test",
                    current_memory_content="",
                    tools=[],
                    max_iterations=1,
                    raise_on_error=True,
                )

    def test_recall_window_filters_only_current_agent_candidates(self):
        memory._MEMORY_RECALL_WINDOWS = {
            "Orchestrator": [["mem_new"]],
            "#1 - Tester": [["mem_old"]],
        }

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            orchestrator_candidates = memory.build_memory_recall_candidates(agent_id="Orchestrator")
            tester_candidates = memory.build_memory_recall_candidates(agent_id="#1 - Tester")

        self.assertNotIn("mem_new", orchestrator_candidates)
        self.assertIn("mem_old", orchestrator_candidates)
        self.assertIn("mem_new", tester_candidates)
        self.assertNotIn("mem_old", tester_candidates)
        memory._MEMORY_RECALL_WINDOWS = {}

    async def test_select_relevant_memory_ids_updates_per_agent_window_only_when_non_empty(self):
        selected_client = FakeRecallLLMClient([
            [
                {
                    "id": "call_1",
                    "name": "SelectRelevantMemories",
                    "arguments": '{"memory_ids": ["mem_new"]}',
                    "raw": {},
                }
            ],
        ])
        empty_client = FakeRecallLLMClient([
            [
                {
                    "id": "call_2",
                    "name": "SelectRelevantMemories",
                    "arguments": '{"memory_ids": []}',
                    "raw": {},
                }
            ],
        ])

        memory._MEMORY_RECALL_WINDOWS = {}
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=selected_client):
            selected = await memory.select_relevant_memory_ids("query", agent_id="agent_a")

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_recall_llm_client", return_value=empty_client):
            empty = await memory.select_relevant_memory_ids("query", agent_id="agent_b")

        self.assertEqual(selected, ["mem_new"])
        self.assertEqual(empty, [])
        self.assertEqual(memory._MEMORY_RECALL_WINDOWS, {"agent_a": [["mem_new"]]})
        memory._MEMORY_RECALL_WINDOWS = {}

    def test_recall_window_drops_oldest_round_when_over_limit(self):
        memory._MEMORY_RECALL_WINDOWS = {"agent_a": [["mem_old"], ["mem_new"]]}

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "get_memory_recall_window_size", return_value=2):
            memory._append_memory_recall_window("agent_a", ["mem_old", "mem_new"])

        self.assertEqual(memory._MEMORY_RECALL_WINDOWS["agent_a"], [["mem_new"], ["mem_old", "mem_new"]])
        memory._MEMORY_RECALL_WINDOWS = {}

    def test_truncate_insight_short_text_not_truncated(self):
        self.assertEqual(memory._truncate_insight("short insight"), "short insight")
        self.assertEqual(memory._truncate_insight(""), "")
        self.assertEqual(memory._truncate_insight(None), "")

    def test_truncate_insight_long_text_truncated(self):
        long_text = "甲" * 50 + "乙" * 50 + "丙" * 50  # 150 tokens
        result = memory._truncate_insight(long_text)
        self.assertIn("甲" * 50, result)
        self.assertIn("丙", result)
        self.assertIn("[...内容截断...]", result)
        self.assertNotIn("乙", result)

    def test_truncate_insight_boundary(self):
        exact_100 = "甲" * 100
        self.assertEqual(memory._truncate_insight(exact_100), exact_100)
        over_100 = "甲" * 50 + "乙" * 51  # 101 tokens
        result = memory._truncate_insight(over_100)
        self.assertIn("[...内容截断...]", result)

    def test_truncate_insight_uses_tokens_and_preserves_utf8_boundaries(self):
        text = "𐍈" * 80 + "甲" * 80 + "𐍈" * 80
        encoder = tiktoken.get_encoding("o200k_base")
        tokens = encoder.encode(text, disallowed_special=())

        result = memory._truncate_insight(text)
        head, tail = result.split(" [...内容截断...] ")

        self.assertEqual(
            head,
            encoder.decode_bytes(tokens[:50]).decode("utf-8", errors="ignore"),
        )
        self.assertEqual(
            tail,
            encoder.decode_bytes(tokens[-50:]).decode("utf-8", errors="ignore"),
        )
        self.assertNotIn("�", result)
        self.assertLessEqual(memory.estimate_text_tokens(head), 50)
        self.assertLessEqual(memory.estimate_text_tokens(tail), 50)
        self.assertEqual(memory._truncate_insight(result), result)

    def test_truncate_insight_does_not_count_existing_marker_tokens(self):
        text = "甲" * 50 + " [...内容截断...] " + "乙" * 50
        self.assertEqual(memory._truncate_insight(text), text)

        over_limit = "甲" * 70 + " [...内容截断...] " + "乙" * 70
        retruncated = memory._truncate_insight(over_limit)
        self.assertEqual(retruncated, "甲" * 50 + " [...内容截断...] " + "乙" * 50)
        self.assertEqual(retruncated.count("[...内容截断...]"), 1)

    def test_truncate_output_uses_exact_token_boundary(self):
        exact = "甲" * 8000
        over = "甲" * 4000 + "乙" * 4001
        token_light = "A" * 20000

        self.assertEqual(truncate_output(exact), exact)
        self.assertEqual(truncate_output(token_light), token_light)
        self.assertEqual(
            truncate_output(over),
            "甲" * 4000 + "\n\n[...此处省略 1 tokens...]\n\n" + "乙" * 4000,
        )

    def test_truncate_output_uses_tokens_and_preserves_utf8_boundaries(self):
        family = "👨‍👩‍👧‍👦"
        text = family * 500 + "甲" * 9000 + family * 500
        encoder = tiktoken.get_encoding("o200k_base")
        tokens = encoder.encode(text, disallowed_special=())

        result = truncate_output(text)
        marker = "[...此处省略"
        self.assertIn(marker, result)
        head, tail = result.split("\n\n", 2)[0], result.rsplit("\n\n", 2)[-1]
        self.assertEqual(
            head,
            encoder.decode_bytes(tokens[:4000]).decode("utf-8", errors="ignore"),
        )
        self.assertEqual(
            tail,
            encoder.decode_bytes(tokens[-4000:]).decode("utf-8", errors="ignore"),
        )
        self.assertNotIn("�", result)
        self.assertLessEqual(memory.estimate_text_tokens(head), 4000)
        self.assertLessEqual(memory.estimate_text_tokens(tail), 4000)

    def test_truncate_output_retruncating_existing_output_is_idempotent(self):
        text = "甲" * 4000 + "中😀English " * 2000 + "乙" * 4000
        first = truncate_output(text)
        self.assertEqual(truncate_output(first), first)

        second = memory._compact_tool_output_text(first)
        expected_omitted_tokens = memory.estimate_text_tokens(first) - 2000
        expected_marker = memory.TOOL_OUTPUT_COMPACT_MARKER.format(
            omitted_tokens=expected_omitted_tokens,
        )
        self.assertEqual(
            second,
            "甲" * 1000 + expected_marker + "乙" * 1000,
        )
        self.assertEqual(memory._compact_tool_output_text(second), second)
        self.assertEqual(truncate_output(second), second)
        self.assertEqual(second.count("[该工具执行结果已被压缩 "), 1)

    def test_truncate_output_fallback_preserves_unicode(self):
        text = "𐍈" * 8001
        with patch.object(text_tokens, "_ENCODER", None):
            result = truncate_output(text)

        self.assertEqual(
            result,
            "𐍈" * 4000 + "\n\n[...此处省略 1 tokens...]\n\n" + "𐍈" * 4000,
        )
        self.assertNotIn("�", result)

    def test_truncate_output_excludes_existing_marker_tokens(self):
        marker = "\n\n[...此处省略 123 tokens...]\n\n"
        text = "甲" * 4000 + marker + "乙" * 4000
        self.assertEqual(truncate_output(text), text)

        compact_marker = "\n\n...[该工具执行结果已被压缩 123 tokens]...\n\n"
        compacted_text = "甲" * 4000 + compact_marker + "乙" * 4000
        truncated = truncate_output(compacted_text)
        self.assertIn("[...此处省略 ", truncated)
        self.assertNotIn("...[该工具执行结果已被压缩 123 tokens]...", truncated)

    def test_truncate_output_retruncates_payload_around_existing_marker(self):
        marker = "\n\n[...此处省略 123 tokens...]\n\n"
        text = "甲" * 7000 + marker + "乙" * 7000
        result = truncate_output(text)
        head, marker_and_tail = result.split("\n\n", 1)
        marker_text, tail = marker_and_tail.split("\n\n", 1)
        self.assertTrue(marker_text.startswith("[...此处省略"))
        self.assertEqual(head, "甲" * 4000)
        self.assertEqual(tail, "乙" * 4000)
        self.assertEqual(result.count("[...此处省略"), 1)

    def test_recall_candidates_truncate_long_insight(self):
        records = [
            {
                "id": "mem_long",
                "category": "workflow",
                "updated_at": "2026-01-01 00:00:00",
                "insight": "甲" * 80 + "乙" * 80,  # 160 tokens
                "reuse_condition": "test",
                "status": "active",
            },
        ]
        with patch.object(memory, "list_long_term_memories", return_value=records):
            candidates = memory.build_memory_recall_candidates()
        self.assertIn("甲" * 50, candidates)
        self.assertIn("乙", candidates)
        self.assertIn("[...内容截断...]", candidates)

    async def test_recall_tool_handler_returns_selected_context_without_real_model(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "select_relevant_memory_ids", AsyncMock(return_value=["mem_new"])):
            result = await memory.MEMORY_RECALL_TOOLS_HANDLERS["RecallLongTermMemory"]("当前任务")

        self.assertEqual(result["ids"], ["mem_new"])
        self.assertIn("mem_new", result["content"])
        self.assertIn("new insight should be injected", result["content"])
        self.assertNotIn("new evidence should not be injected", result["content"])

    def test_system_prompts_do_not_embed_full_user_memory(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            orchestrator_prompt = get_orchestrator_system_prompt("D:/workspace", "pwsh", "test", plan_mode=False)
            sub_agent_prompt = get_sub_agent_system_prompt("Tester", "D:/workspace", "pwsh", "test")

        self.assertNotIn("# User Memory", orchestrator_prompt)
        self.assertNotIn("old insight should not be in candidates", orchestrator_prompt)
        self.assertIn("# Long-Term Memory Actions", orchestrator_prompt)
        self.assertIn("RecallLongTermMemory", orchestrator_prompt)
        self.assertIn("RememberLongTermMemory", orchestrator_prompt)
        self.assertIn("durable, reusable preference", orchestrator_prompt)
        self.assertIn("Tool-specific schemas and argument requirements are defined by the tools themselves", orchestrator_prompt)

        self.assertNotIn("# User Memory", sub_agent_prompt)
        self.assertNotIn("RecallLongTermMemory", sub_agent_prompt)
        self.assertNotIn("RememberLongTermMemory", sub_agent_prompt)
        self.assertNotIn("old insight should not be in candidates", sub_agent_prompt)

    def test_memory_agent_tools_stay_independent_from_orchestrator_memory_tools(self):
        long_term_tool_names = [tool["function"]["name"] for tool in memory.LONG_TERM_MEMORY_TOOLS]
        recall_tool_names = [tool["function"]["name"] for tool in memory.MEMORY_RECALL_TOOLS]
        self_memory_tool_names = [tool["function"]["name"] for tool in memory.MEMORY_SELF_MANAGEMENT_TOOLS]

        self.assertEqual(
            long_term_tool_names,
            ["AppendLongTermMemory", "DeleteLongTermMemory", "UpdateLongTermMemory"],
        )
        self.assertEqual(recall_tool_names, ["RecallLongTermMemory"])
        self.assertEqual(self_memory_tool_names, ["RememberLongTermMemory"])
        self.assertNotIn("RecallLongTermMemory", long_term_tool_names)
        self.assertNotIn("RememberLongTermMemory", long_term_tool_names)


if __name__ == "__main__":
    unittest.main()
