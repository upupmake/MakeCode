import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.memory as memory
from prompts import get_orchestrator_system_prompt, get_sub_agent_system_prompt
from utils import text_tokens
from utils.common import truncate_output
from utils.teams import build_sub_agent_recall_query, prepend_recalled_memory_to_sub_agent_prompt


MEMORY_RECORDS = [
    {
        "id": "mem_old",
        "category": "workflow",
        "updated_at": "2026-01-01 00:00:00",
        "insight": "old insight about legacy workflow handling",
        "evidence": "old evidence should not be in candidates",
        "reuse_condition": "when handling old workflow",
        "status": "active",
    },
    {
        "id": "mem_new",
        "category": "project-convention",
        "updated_at": "2026-02-01 00:00:00",
        "insight": "new insight should be injected about foo_bar_baz handling",
        "evidence": "new evidence should not be injected",
        "reuse_condition": "when handling new convention foo_bar_baz",
        "status": "active",
    },
]


def reset_recall_index():
    memory._MEMORY_FTS5_INDEX = None
    memory._MEMORY_FTS5_INDEX_SIGNATURE = None


class MemoryRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        memory._MEMORY_RECALL_WINDOWS = {}
        reset_recall_index()

    def tearDown(self):
        memory._MEMORY_RECALL_WINDOWS = {}
        reset_recall_index()

    # ---- 分词 ----

    def test_tokenize_for_index_filters_short_cjk_tokens_but_keeps_english(self):
        tokens = memory._tokenize_for_index("子代理委派 memory_recall_model 测试")
        self.assertNotIn("子", tokens)
        self.assertIn("代理", tokens)
        self.assertIn("委派", tokens)
        self.assertIn("memory_recall_model", tokens)
        self.assertIn("测试", tokens)

    def test_tokenize_for_index_merges_single_char_token_with_neighbors(self):
        # "子"被 jieba 切成单字，不再单独保留，而是分别与前后完整 token 拼接出
        # "子代理"这个完整复合词，让它可以作为精确短语参与匹配。
        tokens = memory._tokenize_for_index("子代理委派")
        self.assertNotIn("子", tokens)
        self.assertIn("子代理", tokens)
        self.assertIn("代理", tokens)
        self.assertIn("委派", tokens)

    def test_tokenize_for_index_merges_single_char_token_at_text_boundary(self):
        # 单字出现在开头/结尾且只有一侧有邻词时，只拼接有效的一侧。
        tokens = memory._tokenize_for_index("长期记忆库")
        self.assertNotIn("库", tokens)
        self.assertIn("记忆库", tokens)
        self.assertIn("长期", tokens)
        self.assertIn("记忆", tokens)

    def test_segment_text_for_index_joins_tokens_with_space(self):
        segmented = memory._segment_text_for_index("记忆召回测试")
        self.assertEqual(segmented, " ".join(memory._tokenize_for_index("记忆召回测试")))

    def test_tokenize_query_deduplicates_terms_across_keywords(self):
        # 查询里包含重复关键词或关键词分词后产生重复 token 时，最终 term 列表应该去重，
        # 避免在 FTS5 MATCH 表达式里带着重复的 OR 分支（不影响正确性，但浪费计算）。
        terms = memory._tokenize_query("子代理 子代理委派", False)
        self.assertEqual(len(terms), len(set(terms)))
        self.assertIn("子代理", terms)
        self.assertIn("委派", terms)

    def test_query_terms_keeps_pure_ascii_keyword_whole(self):
        terms = memory._query_terms("memory_recall_model")
        self.assertEqual(terms, ["memory_recall_model"])

    def test_query_terms_splits_and_filters_chinese_keyword(self):
        terms = memory._query_terms("子代理委派")
        self.assertNotIn("子", terms)
        self.assertIn("代理", terms)
        self.assertIn("委派", terms)

    def test_query_terms_merges_single_char_keyword_with_neighbors(self):
        terms = memory._query_terms("子代理委派")
        self.assertIn("子代理", terms)

    def test_ascii_proper_nouns_stay_intact_without_custom_dictionary(self):
        # 纯 ASCII 的专有名词不依赖自定义词典：_tokenize_for_index 对纯 ASCII 片段
        # 按空白整体保留，不交给 jieba 切分字符，所以 jieba 默认行为已经不会把它们切碎。
        for term in ["MakeCode", "TaskManager", "FilePatch", "DelegateTasks"]:
            tokens = memory._tokenize_for_index(f"关于{term}的说明")
            self.assertIn(term, tokens)

    # ---- DF 上限机制 ----

    def test_document_frequency_counts_token_presence_per_record(self):
        records = [
            {"id": "mem_1", "insight": "测试 测试 测试", "reuse_condition": "when 测试"},
            {"id": "mem_2", "insight": "测试 其他", "reuse_condition": "when other"},
            {"id": "mem_3", "insight": "无关内容", "reuse_condition": "when unrelated"},
        ]
        index = memory._MemoryFts5Index(records)
        self.assertEqual(index.document_frequency.get("测试"), 2)

    def test_high_frequency_token_is_excluded_from_match_but_rare_term_still_hits(self):
        records = [
            {"id": f"mem_{i}", "insight": "测试 商品", "reuse_condition": "when 测试"}
            for i in range(8)
        ]
        records.append({"id": "mem_target", "insight": "测试 独有关键字", "reuse_condition": "when 测试"})
        index = memory._MemoryFts5Index(records)
        # “测试”在 9 条中全部出现，占比 100% > 0.4，应被排除出触发集合；
        # “独有关键字”被 jieba 切分为 [独有,关键,关键字]，这些 token 均只在 mem_target 出现，仍应命中。
        query_terms = memory._query_terms("独有关键字")
        results = index.search(memory._query_terms("测试") + query_terms)
        hit_ids = [memory_id for memory_id, _ in results]
        self.assertIn("mem_target", hit_ids)
        self.assertNotEqual(len(hit_ids), len(records))

    def test_all_high_frequency_terms_fallback_to_full_match(self):
        records = [
            {"id": f"mem_{i}", "insight": "测试 商品", "reuse_condition": "when 测试"}
            for i in range(5)
        ]
        index = memory._MemoryFts5Index(records)
        # 查询词全部是高频词时，兜底全部保留参与匹配，而不是查询直接落空。
        results = index.search(memory._query_terms("测试") + memory._query_terms("商品"))
        self.assertEqual(len(results), len(records))

    # ---- 筛选阈值 ----

    def test_filter_by_relative_threshold_empty_input_returns_empty(self):
        self.assertEqual(memory._filter_by_relative_threshold([]), [])

    def test_filter_by_relative_threshold_keeps_single_candidate(self):
        self.assertEqual(memory._filter_by_relative_threshold([("mem_a", 3.0)]), ["mem_a"])

    def test_filter_by_relative_threshold_uses_relative_cutoff_when_top_score_is_high(self):
        scored = [("mem_a", 10.0), ("mem_b", 4.0), ("mem_c", 1.0)]
        selected = memory._filter_by_relative_threshold(scored)
        self.assertEqual(selected, ["mem_a", "mem_b"])

    def test_filter_by_relative_threshold_scales_with_small_pool_low_scores(self):
        # 小记忆池下 BM25 绝对分数本身会偏低（IDF 随候选总数 N 浮动），相对阈值只看
        # 与 top1 的比例，不受绝对分数尺度影响，所以低分池子里同样能正确筛出相关记忆。
        scored = [("mem_a", 2.0), ("mem_b", 1.6), ("mem_c", 0.5)]
        selected = memory._filter_by_relative_threshold(scored)
        self.assertEqual(selected, ["mem_a", "mem_b"])

    # ---- 召回窗口排除 ----

    def test_recall_window_excludes_recently_recalled_ids_for_same_agent(self):
        records = [
            {"id": "mem_a", "category": "workflow", "updated_at": "2026-01-01 00:00:00",
             "insight": "the foo_bar_baz module needs refactoring for the new recall pipeline",
             "reuse_condition": "when working on foo_bar_baz module changes", "status": "active"},
            {"id": "mem_b", "category": "workflow", "updated_at": "2026-01-02 00:00:00",
             "insight": "completely unrelated content about something else entirely", "status": "active",
             "reuse_condition": "when doing unrelated work"},
        ]
        background_records = [
            {"id": f"mem_bg_{i}", "category": "workflow", "updated_at": "2026-01-01 00:00:00",
             "insight": f"some background note number {i} about routine maintenance tasks",
             "reuse_condition": f"when doing routine task {i}", "status": "active"}
            for i in range(10)
        ]
        with patch.object(memory, "list_long_term_memories", return_value=records + background_records):
            first = memory.select_relevant_memory_ids("foo_bar_baz", agent_id="agent_a")
            second = memory.select_relevant_memory_ids("foo_bar_baz", agent_id="agent_a")

        self.assertEqual(first, ["mem_a"])
        self.assertEqual(second, [])

    # ---- recall_long_term_memories 整体流程（纯本地计算，无模型调用） ----

    async def test_recall_long_term_memories_returns_matching_ids_without_any_model_call(self):
        records = [
            {"id": "mem_old", "category": "workflow", "updated_at": "2026-01-01 00:00:00",
             "insight": "old insight about legacy workflow handling with unrelated filler words filler words filler words",
             "evidence": "old evidence should not be in candidates",
             "reuse_condition": "when handling old workflow filler words filler words", "status": "active"},
            {"id": "mem_new", "category": "project-convention", "updated_at": "2026-02-01 00:00:00",
             "insight": "new insight should be injected about foo_bar_baz foo_bar_baz foo_bar_baz handling",
             "evidence": "new evidence should not be injected",
             "reuse_condition": "when handling new convention foo_bar_baz foo_bar_baz", "status": "active"},
        ] + [
            {"id": f"mem_bg_{i}", "category": "workflow", "updated_at": "2026-01-01 00:00:00",
             "insight": f"some background note number {i} about routine maintenance tasks",
             "reuse_condition": f"when doing routine task {i}", "status": "active"}
            for i in range(10)
        ]
        with patch.object(memory, "list_long_term_memories", return_value=records), \
                patch.object(memory, "create_memory_management_llm_client") as create_client, \
                patch.object(memory, "_touch_recalled_memories"):
            result = await memory.recall_long_term_memories("foo_bar_baz")

        self.assertIsNone(result.get("error"))
        self.assertIn("mem_new", result["ids"])
        self.assertNotIn("mem_old", result["ids"])
        self.assertIn("mem_new", result["content"])
        self.assertIn("new insight should be injected about foo_bar_baz foo_bar_baz foo_bar_baz handling", result["content"])
        self.assertNotIn("new evidence should not be injected", result["content"])
        create_client.assert_not_called()

    async def test_recall_long_term_memories_returns_empty_for_unrelated_query(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "create_memory_management_llm_client") as create_client, \
                patch.object(memory, "_touch_recalled_memories"):
            result = await memory.recall_long_term_memories("区块链 blockchain")

        self.assertEqual(result["ids"], [])
        self.assertEqual(result["content"], "")
        create_client.assert_not_called()

    async def test_recall_touches_selected_memories_before_rendering_context(self):
        events = []

        def render_context(memory_ids):
            events.append(("render", memory_ids))
            return "selected context"

        with patch.object(memory, "select_relevant_memory_ids", return_value=["mem_new"]), \
                patch.object(memory, "_touch_recalled_memories", side_effect=lambda ids: events.append(("touch", ids))), \
                patch.object(memory, "render_selected_memory_context", side_effect=render_context), \
                patch.object(memory, "post_tui"), \
                patch.object(memory, "Markdown"):
            result = await memory.recall_long_term_memories("query")

        self.assertEqual(result["content"], "selected context")
        self.assertEqual(events, [("touch", ["mem_new"]), ("render", ["mem_new"])])

    async def test_recall_background_places_ids_and_details_on_following_lines(self):
        memory_context = "## mem_new\n- Insight: selected context"

        with patch.object(memory, "select_relevant_memory_ids", return_value=["mem_new", "mem_old"]), \
                patch.object(memory, "_touch_recalled_memories"), patch.object(
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

    async def test_recall_long_term_memories_downgrades_internal_exception_to_error_result(self):
        with patch.object(memory, "select_relevant_memory_ids", side_effect=RuntimeError("boom")):
            result = await memory.recall_long_term_memories("query")

        self.assertEqual(result, {"ids": [], "content": "", "error": "boom"})

    def test_select_relevant_memory_ids_returns_empty_for_blank_query(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            self.assertEqual(memory.select_relevant_memory_ids("   "), [])

    def test_select_relevant_memory_ids_returns_empty_when_no_active_memories(self):
        with patch.object(memory, "list_long_term_memories", return_value=[]):
            self.assertEqual(memory.select_relevant_memory_ids("foo_bar_baz"), [])

    # ---- 子代理场景：TF-IDF 关键词提取 + 剥离模板包装 ----

    def test_strip_sub_agent_task_wrapper_removes_template_before_context_prompt(self):
        query = build_sub_agent_recall_query("7", "Backend Developer", "实际的任务内容 foo_bar_baz")
        stripped = memory._strip_sub_agent_task_wrapper(query)
        self.assertNotIn("Task ID", stripped)
        self.assertNotIn("Backend Developer", stripped)
        self.assertIn("实际的任务内容 foo_bar_baz", stripped)

    def test_tokenize_query_uses_extraction_path_for_sub_agent_and_strips_wrapper(self):
        query = build_sub_agent_recall_query("7", "Backend Developer", "实际的任务内容 foo_bar_baz")
        with patch.object(memory, "_extract_query_terms_from_text", return_value=["foo_bar_baz"]) as extract:
            terms = memory._tokenize_query(query, use_keyword_extraction=True)

        self.assertEqual(terms, ["foo_bar_baz"])
        called_with_text = extract.call_args.args[0]
        self.assertNotIn("Task ID", called_with_text)
        self.assertNotIn("Backend Developer", called_with_text)

    def test_tokenize_query_uses_full_segmentation_path_when_not_extraction(self):
        with patch.object(memory, "_extract_query_terms_from_text") as extract:
            terms = memory._tokenize_query("memory_recall_model 测试", use_keyword_extraction=False)

        extract.assert_not_called()
        self.assertIn("memory_recall_model", terms)
        self.assertIn("测试", terms)

    async def test_sub_agent_recall_path_uses_keyword_extraction(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "_tokenize_query", return_value=["foo_bar_baz"]) as tokenize, \
                patch.object(memory, "create_memory_management_llm_client") as create_client:
            query = build_sub_agent_recall_query("7", "Backend Developer", "实际的任务内容")
            await memory.recall_long_term_memories(query, agent_id="#7 - Backend Developer", use_keyword_extraction=True)

        tokenize.assert_called_once_with(query, True)
        create_client.assert_not_called()

    # ---- 其余保留行为 ----

    def test_selected_memory_context_filters_ids_and_excludes_evidence(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            selected_ids = memory.normalize_memory_ids(["mem_new", "missing", "mem_new", "mem_old"])
            context = memory.render_selected_memory_context(["mem_new", "missing", "mem_new"])

        self.assertEqual(selected_ids, ["mem_new", "mem_old"])
        self.assertIn("mem_new", context)
        self.assertIn("Insight: new insight should be injected about foo_bar_baz handling", context)
        self.assertIn("Reuse condition: when handling new convention foo_bar_baz", context)
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
        self.assertIn("new insight should be injected about foo_bar_baz handling", injected)
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
        with patch.object(memory, "create_memory_management_llm_client", return_value=fake_client), \
                patch.object(memory, "close_async_llm_client", new_callable=AsyncMock) as close_client, \
                patch.object(memory.StreamRenderer, "render_text_stream_async", new_callable=AsyncMock, side_effect=stream_results), \
                 patch.object(memory, "post_tui"), \
                 patch.object(memory, "_render_agent_response_message"):
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

        with patch.object(memory, "create_memory_management_llm_client", return_value=fake_client), \
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
                 patch.object(memory, "_render_agent_response_message"):
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

        with patch.object(memory, "create_memory_management_llm_client", return_value=fake_client), \
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
                 patch.object(memory, "_render_agent_response_message"):
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

    def test_recall_window_drops_oldest_round_when_over_limit(self):
        memory._MEMORY_RECALL_WINDOWS = {"agent_a": [["mem_old"], ["mem_new"]]}

        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "get_memory_recall_window_size", return_value=2):
            memory._append_memory_recall_window("agent_a", ["mem_old", "mem_new"])

        self.assertEqual(memory._MEMORY_RECALL_WINDOWS["agent_a"], [["mem_new"], ["mem_old", "mem_new"]])
        memory._MEMORY_RECALL_WINDOWS = {}

    def test_truncate_output_uses_exact_token_boundary(self):
        exact = "甲" * 12000
        over = "甲" * 6000 + "乙" * 6001
        token_light = "A" * 20000

        self.assertEqual(truncate_output(exact), exact)
        self.assertEqual(truncate_output(token_light), token_light)
        self.assertEqual(
            truncate_output(over),
            "甲" * 6000 + "\n\n[...此处省略 1 tokens...]\n\n" + "乙" * 6000,
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
            encoder.decode_bytes(tokens[:6000]).decode("utf-8", errors="ignore"),
        )
        self.assertEqual(
            tail,
            encoder.decode_bytes(tokens[-6000:]).decode("utf-8", errors="ignore"),
        )
        self.assertNotIn("�", result)
        self.assertLessEqual(memory.estimate_text_tokens(head), 6000)
        self.assertLessEqual(memory.estimate_text_tokens(tail), 6000)

    def test_truncate_output_retruncating_existing_output_is_idempotent(self):
        text = "甲" * 4000 + "中😀English " * 2000 + "乙" * 4000
        first = truncate_output(text)
        self.assertEqual(truncate_output(first), first)

        second = memory._compact_tool_output_text(first)
        expected_omitted_tokens = memory.estimate_text_tokens(first) - 1500
        expected_marker = memory.TOOL_OUTPUT_COMPACT_MARKER.format(
            omitted_tokens=expected_omitted_tokens,
        )
        self.assertEqual(
            second,
            "甲" * 750 + expected_marker + "乙" * 750,
        )
        self.assertEqual(memory._compact_tool_output_text(second), second)
        self.assertEqual(truncate_output(second), second)
        self.assertEqual(second.count("[该工具执行结果已被压缩 "), 1)

    def test_truncate_output_fallback_preserves_unicode(self):
        text = "𐍈" * 12001
        with patch.object(text_tokens, "_ENCODER", None):
            result = truncate_output(text)

        self.assertEqual(
            result,
            "𐍈" * 6000 + "\n\n[...此处省略 1 tokens...]\n\n" + "𐍈" * 6000,
        )
        self.assertNotIn("�", result)

    def test_truncate_output_excludes_existing_marker_tokens(self):
        marker = "\n\n[...此处省略 123 tokens...]\n\n"
        text = "甲" * 6000 + marker + "乙" * 6000
        self.assertEqual(truncate_output(text), text)

        compact_marker = "\n\n...[该工具执行结果已被压缩 123 tokens]...\n\n"
        compacted_text = "甲" * 6000 + compact_marker + "乙" * 6000
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
        self.assertEqual(head, "甲" * 6000)
        self.assertEqual(tail, "乙" * 6000)
        self.assertEqual(result.count("[...此处省略"), 1)

    async def test_recall_tool_handler_returns_selected_context_without_real_model(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS), \
                patch.object(memory, "select_relevant_memory_ids", return_value=["mem_new"]):
            result = await memory.MEMORY_RECALL_TOOLS_HANDLERS["RecallLongTermMemory"]("当前任务")

        self.assertEqual(result["ids"], ["mem_new"])
        self.assertIn("mem_new", result["content"])
        self.assertIn("new insight should be injected about foo_bar_baz handling", result["content"])
        self.assertNotIn("new evidence should not be injected", result["content"])

    def test_system_prompts_do_not_embed_full_user_memory(self):
        with patch.object(memory, "list_long_term_memories", return_value=MEMORY_RECORDS):
            orchestrator_prompt = get_orchestrator_system_prompt("D:/workspace", "pwsh", plan_mode=False)
            sub_agent_prompt = get_sub_agent_system_prompt("Tester", "D:/workspace", "pwsh")

        self.assertNotIn("# User Memory", orchestrator_prompt)
        self.assertNotIn("old insight about legacy workflow handling", orchestrator_prompt)
        self.assertIn("# Long-Term Memory Actions", orchestrator_prompt)
        self.assertIn("RecallLongTermMemory", orchestrator_prompt)
        self.assertIn("ManageLongTermMemory", orchestrator_prompt)
        self.assertNotIn("RememberLongTermMemory", orchestrator_prompt)
        self.assertIn("added to, updated in, or deleted from long-term memory", orchestrator_prompt)

        self.assertNotIn("# User Memory", sub_agent_prompt)
        self.assertNotIn("RecallLongTermMemory", sub_agent_prompt)
        self.assertNotIn("ManageLongTermMemory", sub_agent_prompt)
        self.assertNotIn("RememberLongTermMemory", sub_agent_prompt)
        self.assertNotIn("old insight about legacy workflow handling", sub_agent_prompt)

    def test_memory_agent_tools_stay_independent_from_orchestrator_memory_tools(self):
        long_term_tool_names = [tool["function"]["name"] for tool in memory.LONG_TERM_MEMORY_TOOLS]
        recall_tool_names = [tool["function"]["name"] for tool in memory.MEMORY_RECALL_TOOLS]
        self_memory_tool_names = [tool["function"]["name"] for tool in memory.MEMORY_SELF_MANAGEMENT_TOOLS]
        manage_tool = memory.MEMORY_SELF_MANAGEMENT_TOOLS[0]["function"]

        self.assertEqual(
            long_term_tool_names,
            ["AppendLongTermMemory", "DeleteLongTermMemory", "UpdateLongTermMemory"],
        )
        self.assertEqual(recall_tool_names, ["RecallLongTermMemory"])
        self.assertEqual(self_memory_tool_names, ["ManageLongTermMemory"])
        self.assertEqual(
            manage_tool["description"],
            "Add, update, or delete long-term memories when the current conversation establishes durable information for future sessions.",
        )
        self.assertIn(
            "durable information established in the current conversation",
            manage_tool["parameters"]["properties"]["prompt"]["description"],
        )
        self.assertNotIn("manager", manage_tool["description"].lower())
        self.assertNotIn("RecallLongTermMemory", long_term_tool_names)
        self.assertNotIn("ManageLongTermMemory", long_term_tool_names)
        self.assertNotIn("RememberLongTermMemory", self_memory_tool_names)


if __name__ == "__main__":
    unittest.main()
