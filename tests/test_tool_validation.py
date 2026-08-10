import traceback

import pytest
from pydantic import Field

import main as main_module
from tools import ask_user, todo
from utils import common, memory, skills, tasks, teams
from utils.common import ContentSearch, FileEdit, FileRead, RunTerminalCommand
from utils.tool_validation import (
    ToolArgumentsModel,
    ToolArgumentValidationError,
    build_tool_definitions,
    merge_tool_model_registries,
    parse_tool_arguments,
    validate_builtin_tool_arguments,
)


def test_parse_tool_arguments_accepts_dict_and_json_object():
    assert parse_tool_arguments("ContentSearch", {"content_regex": "needle"}) == {
        "content_regex": "needle"
    }
    assert parse_tool_arguments("ContentSearch", '{"content_regex":"needle"}') == {
        "content_regex": "needle"
    }
    assert parse_tool_arguments("ContentSearch", "") == {}


def test_parse_tool_arguments_rejects_invalid_json_with_raw_payload():
    raw_payload = '{"command":"top-secret-command'

    with pytest.raises(ToolArgumentValidationError) as exc_info:
        parse_tool_arguments("RunTerminalCommand", raw_payload)

    message = str(exc_info.value)
    assert "json_invalid" in message
    assert f"Input value: {raw_payload!r}" in message


def test_validation_errors_include_values_without_retaining_exception_context():
    value = "top-secret-command"

    failures = []
    for operation in (
        lambda: parse_tool_arguments("RunTerminalCommand", f'{{"command":"{value}'),
        lambda: validate_builtin_tool_arguments(
            "RunTerminalCommand",
            {"command": value, "unexpected": value},
            RunTerminalCommand,
        ),
    ):
        try:
            operation()
        except ToolArgumentValidationError as exc:
            failures.append(exc)

    assert len(failures) == 2
    for failure in failures:
        rendered_traceback = "".join(traceback.format_exception(failure))
        assert value in rendered_traceback
        assert failure.__cause__ is None
        assert failure.__context__ is None


def test_parse_tool_arguments_rejects_non_object():
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        parse_tool_arguments("FileRead", '["unexpected"]')

    message = str(exc_info.value)
    assert "object_required" in message
    assert "Input value: ['unexpected']" in message


def test_parse_tool_arguments_rejects_unescaped_control_characters():
    with pytest.raises(ToolArgumentValidationError, match="json_invalid"):
        parse_tool_arguments("RunTerminalCommand", '{"command":"line\nbreak"}')


def test_validate_builtin_tool_arguments_rejects_unknown_top_level_field():
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_builtin_tool_arguments(
            "ContentSearch",
            {
                "content_regex": "needle",
                "filename": "_regex>.*\\.py$",
            },
            ContentSearch,
        )

    message = str(exc_info.value)
    assert "filename" in message
    assert "extra_forbidden" in message
    assert "path_regex" in message
    assert r"Input value: '_regex>.*\\.py$'" in message


def test_validate_builtin_tool_arguments_rejects_unknown_nested_field():
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_builtin_tool_arguments(
            "FileEdit",
            {
                "path": "example.py",
                "edits": [{
                    "search_content": "old",
                    "replace_content": "new",
                    "unknown": "secret",
                }],
            },
            FileEdit,
        )

    message = str(exc_info.value)
    assert "edits[0].unknown" in message
    assert "Input value: 'secret'" in message


def test_validation_errors_include_type_constraint_and_missing_values():
    class ValueInputs(ToolArgumentsModel):
        count: int = Field(ge=1)
        label: str

    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_builtin_tool_arguments(
            "ValueInputs",
            {"count": 0, "label": 9},
            ValueInputs,
        )

    message = str(exc_info.value)
    assert "count" in message
    assert "greater_than_equal" in message
    assert "Input value: 0" in message
    assert "label" in message
    assert "string_type" in message
    assert "Input value: 9" in message

    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_builtin_tool_arguments("ValueInputs", {"count": 1}, ValueInputs)

    message = str(exc_info.value)
    assert "label" in message
    assert "missing" in message
    assert "Input value: <missing>" in message


def test_validate_builtin_tool_arguments_returns_normalized_model_dump():
    validated = validate_builtin_tool_arguments(
        "FileRead",
        {"path": "example.py", "regions": [{"start": 1, "end": 2}]},
        FileRead,
    )

    assert validated == {
        "path": "example.py",
        "regions": [{"start": 1, "end": 2}],
    }


def test_tool_argument_models_forbid_extra_fields_at_all_levels():
    class Nested(ToolArgumentsModel):
        value: str

    class Outer(ToolArgumentsModel):
        nested: Nested
        count: int = Field(default=1)

    with pytest.raises(ToolArgumentValidationError, match="extra_forbidden"):
        validate_builtin_tool_arguments(
            "Outer",
            {"nested": {"value": "ok", "extra": True}, "extra": True},
            Outer,
        )


def test_tool_definition_registry_rejects_duplicates_and_merges_without_overwrite():
    definitions, registry = build_tool_definitions(ContentSearch, FileRead)

    assert len(definitions) == 2
    assert registry == {"ContentSearch": ContentSearch, "FileRead": FileRead}
    assert merge_tool_model_registries(registry) == registry

    with pytest.raises(ValueError, match="Duplicate built-in tool model"):
        build_tool_definitions(ContentSearch, ContentSearch)

    with pytest.raises(ValueError, match="Duplicate built-in tool model"):
        merge_tool_model_registries(registry, {"ContentSearch": ContentSearch})


def test_builtin_tool_registries_match_handler_boundaries():
    registry_handler_pairs = (
        (common.COMMON_TOOL_MODELS, common.COMMON_TOOLS_HANDLERS),
        (skills.SKILL_TOOL_MODELS, skills.SKILL_TOOLS_HANDLERS),
        (tasks.TASK_MANAGER_TOOL_MODELS, tasks.TASK_MANAGER_TOOLS_HANDLERS),
        (teams.TEAM_TOOL_MODELS, teams.TEAM_TOOLS_HANDLERS),
        (ask_user.ASK_USER_TOOL_MODELS, ask_user.ASK_USER_TOOLS_HANDLERS),
        (memory.LONG_TERM_MEMORY_TOOL_MODELS, memory.LONG_TERM_MEMORY_TOOL_HANDLERS),
        (memory.MEMORY_RECALL_TOOL_MODELS, memory.MEMORY_RECALL_TOOLS_HANDLERS),
    )
    for registry, handlers in registry_handler_pairs:
        assert set(registry) == set(handlers)

    assert set(teams.SUB_AGENT_TOOL_MODELS) == (
        set(common.COMMON_TOOL_MODELS)
        | set(skills.SKILL_TOOL_MODELS)
        | set(todo.TODO_TOOL_MODELS)
    )
    assert set(main_module.BASE_SUPER_TOOL_MODELS) == (
        set(main_module.BASE_SUPER_TOOLS_HANDLERS) | {"RememberLongTermMemory"}
    )
    assert set(memory.LONG_TERM_MEMORY_TOOL_MODELS).isdisjoint(
        main_module.BASE_SUPER_TOOL_MODELS
    )
