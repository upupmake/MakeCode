import copy

import pytest

from utils.conversations import ConversationStore
from utils.memory import prepend_recalled_memory_to_query
from utils.project_instructions import (
    INITIAL_NOTICE,
    MAX_TOTAL_BYTES,
    REMOVAL_NOTICE,
    REPLACEMENT_NOTICE,
    inject_project_instructions,
    load,
    render_removal,
)


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _messages(text="hello"):
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": text},
    ]


def _next_turn(messages, text="next"):
    messages.append({"role": "assistant", "content": "ok"})
    messages.append({"role": "user", "content": text})


def test_load_reads_root_to_workdir(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "AGENTS.md").write_text("package rules", encoding="utf-8")

    loaded = load(package)

    assert [item.text for item in loaded.files] == ["root rules", "package rules"]
    assert [item.path for item in loaded.files] == [
        (repo / "AGENTS.md").resolve(),
        (package / "AGENTS.md").resolve(),
    ]
    assert loaded.digest


def test_load_stops_at_project_root(tmp_path):
    outside = tmp_path / "AGENTS.md"
    outside.write_text("outside rules", encoding="utf-8")
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "AGENTS.md").write_text("package rules", encoding="utf-8")

    loaded = load(package)

    assert [item.text for item in loaded.files] == ["root rules", "package rules"]


def test_load_without_git_reads_only_workdir(tmp_path):
    parent = tmp_path / "AGENTS.md"
    parent.write_text("parent rules", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("work rules", encoding="utf-8")

    loaded = load(work)

    assert [item.text for item in loaded.files] == ["work rules"]


def test_load_accepts_git_file_marker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "AGENTS.md").write_text("package rules", encoding="utf-8")

    loaded = load(package)

    assert [item.text for item in loaded.files] == ["root rules", "package rules"]


def test_load_skips_empty_directory_and_whitespace_files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").mkdir()

    assert load(work).files == ()

    (work / "AGENTS.md").rmdir()
    (work / "AGENTS.md").write_text("", encoding="utf-8")
    assert load(work).files == ()

    (work / "AGENTS.md").write_text("   \n\n", encoding="utf-8")
    assert load(work).files == ()


def test_load_replaces_invalid_utf8(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_bytes(b"\xff\xfevalid text")

    loaded = load(work)

    assert len(loaded.files) == 1
    assert "valid text" in loaded.files[0].text


def test_load_truncates_total_budget_outer_to_inner(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("a" * (20 * 1024), encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "AGENTS.md").write_text("b" * (20 * 1024), encoding="utf-8")

    loaded = load(package)

    assert [item.truncated for item in loaded.files] == [False, True]
    assert len(loaded.files[0].text) == 20 * 1024
    assert len(loaded.files[1].text) == MAX_TOTAL_BYTES - 20 * 1024


def test_load_truncates_single_large_file(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("a" * (MAX_TOTAL_BYTES + 100), encoding="utf-8")

    loaded = load(work)

    assert len(loaded.files) == 1
    assert loaded.files[0].truncated is True
    assert len(loaded.files[0].text) == MAX_TOTAL_BYTES


def test_load_deduplicates_symlinked_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("shared rules", encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    try:
        (package / "AGENTS.md").symlink_to(repo / "AGENTS.md")
    except OSError:
        pytest.skip("symlinks are unavailable")

    loaded = load(package)

    assert len(loaded.files) == 1
    assert loaded.files[0].text == "shared rules"


def test_digest_is_deterministic_and_changes_with_content(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    instructions = work / "AGENTS.md"
    instructions.write_text("first", encoding="utf-8")

    first = load(work).digest
    second = load(work).digest
    instructions.write_text("second", encoding="utf-8")
    third = load(work).digest

    assert first == second
    assert first != third


def test_load_does_not_create_files(tmp_path):
    work = tmp_path / "empty"
    work.mkdir()

    load(work)

    assert not (work / "AGENTS.md").exists()


def test_inject_initial_adds_block_and_display_content(tmp_path):
    repo = _repo(tmp_path)
    instructions = repo / "AGENTS.md"
    instructions.write_text("root rules", encoding="utf-8")
    messages = _messages("hello")

    block = inject_project_instructions(messages, repo)

    assert block is not None
    assert block.startswith(f"# AGENTS.md instructions for {repo.resolve()}")
    assert INITIAL_NOTICE in block
    assert f"## {instructions.resolve()}" in block
    assert "<INSTRUCTIONS>" in block
    assert messages[-1]["content"] == f"{block}\n\nhello"
    state = messages[-1]["message_metadata"]["project_instructions"]
    assert state["kind"] == "initial"
    assert state["workdir"] == str(repo.resolve())
    assert state["paths"] == [str(instructions.resolve())]
    assert state["digest"]
    assert messages[-1]["message_metadata"]["display_content"] == "hello"


def test_inject_does_not_repeat_within_the_same_message(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    before = copy.deepcopy(messages[-1])

    assert inject_project_instructions(messages, repo) is None
    assert messages[-1] == before


def test_inject_does_not_repeat_unchanged_instructions(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    _next_turn(messages)

    assert inject_project_instructions(messages, repo) is None
    assert messages[-1] == {"role": "user", "content": "next"}


def test_inject_replaces_changed_instructions(tmp_path):
    repo = _repo(tmp_path)
    instructions = repo / "AGENTS.md"
    instructions.write_text("old rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    _next_turn(messages)
    instructions.write_text("new rules", encoding="utf-8")

    block = inject_project_instructions(messages, repo)

    assert block is not None
    assert REPLACEMENT_NOTICE in block
    assert "new rules" in block
    assert messages[-1]["content"].startswith(block)
    assert messages[-1]["message_metadata"]["project_instructions"]["kind"] == "replacement"


def test_inject_removes_deleted_instructions(tmp_path):
    repo = _repo(tmp_path)
    instructions = repo / "AGENTS.md"
    instructions.write_text("old rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    _next_turn(messages)
    instructions.unlink()

    block = inject_project_instructions(messages, repo)

    assert block == render_removal(repo.resolve())
    assert REMOVAL_NOTICE in block
    assert "<INSTRUCTIONS>" not in messages[-1]["content"]
    state = messages[-1]["message_metadata"]["project_instructions"]
    assert state["kind"] == "removal"
    assert state["digest"] is None
    assert state["paths"] == []


def test_inject_reappearing_file_starts_over_after_removal(tmp_path):
    repo = _repo(tmp_path)
    instructions = repo / "AGENTS.md"
    instructions.write_text("old rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    _next_turn(messages)
    instructions.unlink()
    inject_project_instructions(messages, repo)
    _next_turn(messages)
    instructions.write_text("new rules", encoding="utf-8")

    block = inject_project_instructions(messages, repo)

    assert block is not None
    assert REPLACEMENT_NOTICE not in block
    assert "new rules" in block
    assert messages[-1]["message_metadata"]["project_instructions"]["kind"] == "initial"


def test_inject_workdir_change_is_a_replacement(tmp_path):
    first_repo = _repo(tmp_path, "first")
    (first_repo / "AGENTS.md").write_text("first rules", encoding="utf-8")
    messages = _messages("hello")
    inject_project_instructions(messages, first_repo)
    _next_turn(messages)

    second_repo = _repo(tmp_path, "second")
    (second_repo / "AGENTS.md").write_text("second rules", encoding="utf-8")

    block = inject_project_instructions(messages, second_repo)

    assert block is not None
    assert REPLACEMENT_NOTICE in block
    assert "second rules" in block
    state = messages[-1]["message_metadata"]["project_instructions"]
    assert state["kind"] == "replacement"
    assert state["workdir"] == str(second_repo.resolve())


def test_inject_list_content_prepends_text_block(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": b"private-image"},
            ],
            "message_metadata": {"display_content": "hello"},
        },
    ]

    block = inject_project_instructions(messages, repo)

    assert block is not None
    content = messages[-1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == block
    assert content[1:] == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": b"private-image"},
    ]
    assert messages[-1]["message_metadata"]["display_content"] == "hello"


def test_inject_without_files_is_a_noop(tmp_path):
    work = tmp_path / "empty"
    work.mkdir()
    messages = _messages("hello")

    assert inject_project_instructions(messages, work) is None
    assert messages[-1] == {"role": "user", "content": "hello"}


def test_inject_skips_when_last_message_is_not_user(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "ok"},
    ]

    assert inject_project_instructions(messages, repo) is None
    assert messages[1] == {"role": "user", "content": "hello"}


def test_inject_ignores_metadata_when_the_block_was_summarized_away(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("fresh rules", encoding="utf-8")
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "summary of the previous conversation",
            "message_metadata": {
                "project_instructions": {
                    "digest": "stale",
                    "workdir": str(repo.resolve()),
                    "paths": [],
                    "kind": "initial",
                },
            },
        },
        {"role": "assistant", "content": "understood"},
        {"role": "user", "content": "hello"},
    ]

    block = inject_project_instructions(messages, repo)

    assert block is not None
    assert REPLACEMENT_NOTICE not in block
    assert "fresh rules" in block
    assert messages[-1]["message_metadata"]["project_instructions"]["kind"] == "initial"


def test_inject_ignores_metadata_when_the_file_paths_were_summarized_away(tmp_path):
    repo = _repo(tmp_path)
    instructions = repo / "AGENTS.md"
    instructions.write_text("fresh rules", encoding="utf-8")
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "<INSTRUCTIONS>\npartial summary\n</INSTRUCTIONS>",
            "message_metadata": {
                "project_instructions": {
                    "digest": "stale",
                    "workdir": str(repo.resolve()),
                    "paths": [str(instructions.resolve())],
                    "kind": "initial",
                },
            },
        },
        {"role": "assistant", "content": "understood"},
        {"role": "user", "content": "hello"},
    ]

    block = inject_project_instructions(messages, repo)

    assert block is not None
    assert REPLACEMENT_NOTICE not in block
    assert messages[-1]["message_metadata"]["project_instructions"]["kind"] == "initial"


def test_inject_derives_display_content_from_memory_wrapper(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = _messages(prepend_recalled_memory_to_query("hello", "remembered"))

    inject_project_instructions(messages, repo)

    assert messages[-1]["message_metadata"]["display_content"] == "hello"


def test_inject_preserves_existing_display_content(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "<skill name=\"demo\">\nloaded\n</skill>\nUser: /demo 处理这个请求",
            "message_metadata": {
                "display_content": "/demo 处理这个请求",
                "skill_command": True,
            },
        },
    ]

    inject_project_instructions(messages, repo)

    assert messages[-1]["message_metadata"]["display_content"] == "/demo 处理这个请求"
    assert messages[-1]["message_metadata"]["skill_command"] is True


def test_injected_message_keeps_title_source_clean(tmp_path):
    import main as main_module

    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = _messages("hello")

    inject_project_instructions(messages, repo)

    assert main_module._collect_user_message_content(messages) == "hello"


def test_injected_message_is_hidden_from_history_renderer(tmp_path):
    from system.console_render import _extract_message_text

    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    messages = _messages("hello")

    inject_project_instructions(messages, repo)

    assert _extract_message_text(messages[-1]) == "hello"


def test_inject_uses_previous_state_after_conversation_round_trip(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    store = ConversationStore(tmp_path / "conversations")
    store.create()
    messages = _messages("hello")
    inject_project_instructions(messages, repo)
    path = store.save_messages(messages)

    restored = store.load(path).messages
    restored.append({"role": "assistant", "content": "ok"})
    restored.append({"role": "user", "content": "next"})

    assert inject_project_instructions(restored, repo) is None
    assert restored[-1] == {"role": "user", "content": "next"}


@pytest.mark.anyio
async def test_main_agent_loop_invokes_project_instruction_injection():
    from unittest.mock import AsyncMock, patch

    import main as main_module

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    class FakeClient:
        @staticmethod
        def append_assistant_message(history, message):
            history.append(message)

    with patch.object(main_module, "inject_project_instructions") as inject, \
            patch.object(main_module, "get_dynamic_system_prompt", return_value="system"), \
            patch.object(main_module, "get_current_tools_definition", return_value=[]), \
            patch.object(main_module, "_render_token_usage"), \
            patch.object(
                main_module,
                "_stream_with_render",
                AsyncMock(return_value=("", [], {"role": "assistant", "content": "done"}, False)),
            ), \
            patch.object(main_module.GLOBAL_MCP_MANAGER, "get_registry_snapshot", return_value=([], {})), \
            patch.object(main_module.CONVERSATION_STORE, "save_messages"), \
            patch.object(main_module, "_generate_title_if_missing", new_callable=AsyncMock, return_value=False), \
            patch.object(main_module, "_apply_pending_title"), \
            patch.object(main_module, "post_tui"), \
            patch.object(main_module, "is_plan_mode", return_value=False), \
            patch.object(main_module, "consume_temporary_query", return_value=None), \
            patch.object(main_module, "clear_temporary_query"), \
            patch.object(main_module, "set_temporary_query_enabled"):
        committed = await main_module._agent_loop_with_client(
            messages,
            FakeClient(),
            skip_compaction=True,
        )

    assert committed is True
    inject.assert_called_once_with(messages, main_module.paths.workdir())
