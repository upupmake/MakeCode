from pathlib import Path

import pytest

from utils.llm_client import ChatAPIClient
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS, TOOLS, TaskManager


def make_manager(tmp_path: Path) -> TaskManager:
    manager = TaskManager(tmp_path)
    manager._data["tasks"] = {
        "1": {"id": "1", "subject": "one", "description": "", "status": "pending", "depend_on": []},
        "2": {"id": "2", "subject": "two", "description": "", "status": "pending", "depend_on": []},
        "3": {"id": "3", "subject": "three", "description": "", "status": "pending", "depend_on": []},
    }
    manager._data["next_id"] = 4
    manager._save = lambda: None
    return manager


def test_batch_tool_names_replace_single_task_tools() -> None:
    names = {tool["function"]["name"] for tool in TOOLS}

    assert "UpdateTasksContent" in names
    assert "UpdateTasksDependencies" in names
    assert "UpdateTaskContent" not in names
    assert "UpdateTaskDependencies" not in names
    assert "UpdateTasksContent" in TASK_MANAGER_TOOLS_HANDLERS
    assert "UpdateTasksDependencies" in TASK_MANAGER_TOOLS_HANDLERS


def test_create_tasks_description_explains_same_batch_dependencies() -> None:
    create_tool = next(tool["function"] for tool in TOOLS if tool["function"]["name"] == "CreateTasks")

    assert "existed before this call" in create_tool["description"]
    assert "UpdateTasksDependencies" in create_tool["description"]


def test_batch_tool_schemas_use_tasks_arrays_without_refs() -> None:
    formatted = ChatAPIClient(None, "test").format_tools(TASK_MANAGER_TOOLS)
    tools = {tool["function"]["name"]: tool["function"] for tool in formatted}

    for name in ("UpdateTasksContent", "UpdateTasksDependencies"):
        parameters = tools[name]["parameters"]
        assert parameters["required"] == ["tasks"]
        assert parameters["properties"]["tasks"]["type"] == "array"
        assert parameters["properties"]["tasks"]["minItems"] == 1
        assert "$ref" not in str(parameters)
        assert "$defs" not in parameters
        assert "definitions" not in parameters

    content_item = tools["UpdateTasksContent"]["parameters"]["properties"]["tasks"]["items"]
    assert content_item["required"] == ["task_id", "subject", "description"]
    dependency_item = tools["UpdateTasksDependencies"]["parameters"]["properties"]["tasks"]["items"]
    assert dependency_item["required"] == ["task_id", "depend_on"]


def test_update_tasks_content_updates_multiple_tasks(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    result = manager.update_tasks_content([
        {"task_id": "1", "subject": " First ", "description": "a"},
        {"task_id": "2", "subject": "Second", "description": "b"},
    ])

    assert [task["subject"] for task in result] == ["First", "Second"]
    assert manager._data["tasks"]["1"]["description"] == "a"
    assert manager._data["tasks"]["2"]["description"] == "b"


@pytest.mark.parametrize("tasks", [
    [],
    [{"task_id": "1", "subject": "a"}, {"task_id": "1", "subject": "b"}],
    [{"task_id": "missing", "subject": "a"}],
    [{"task_id": "1", "subject": "   "}],
])
def test_update_tasks_content_rejects_invalid_batches(tmp_path: Path, tasks: list[dict]) -> None:
    manager = make_manager(tmp_path)
    original = {task_id: task.copy() for task_id, task in manager._data["tasks"].items()}

    with pytest.raises(ValueError, match="UpdateTasksContent parameters invalid"):
        manager.update_tasks_content(tasks)

    assert manager._data["tasks"] == original


def test_update_tasks_dependencies_applies_batch_atomically(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    result = manager.update_tasks_dependencies([
        {"task_id": "2", "depend_on": ["1"]},
        {"task_id": "3", "depend_on": ["2"]},
    ])

    assert [task["depend_on"] for task in result] == [["1"], ["2"]]


@pytest.mark.parametrize("tasks", [
    [],
    [{"task_id": "1", "depend_on": []}, {"task_id": "1", "depend_on": []}],
    [{"task_id": "missing", "depend_on": []}],
    [{"task_id": "1", "depend_on": ["missing"]}],
    [{"task_id": "1", "depend_on": ["1"]}],
])
def test_update_tasks_dependencies_rejects_invalid_batches(tmp_path: Path, tasks: list[dict]) -> None:
    manager = make_manager(tmp_path)
    original = {task_id: task.copy() for task_id, task in manager._data["tasks"].items()}

    with pytest.raises(ValueError, match="UpdateTasksDependencies parameters invalid"):
        manager.update_tasks_dependencies(tasks)

    assert manager._data["tasks"] == original


def test_update_tasks_dependencies_rolls_back_cycle(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    with pytest.raises(ValueError, match="Cycle detected"):
        manager.update_tasks_dependencies([
            {"task_id": "1", "depend_on": ["2"]},
            {"task_id": "2", "depend_on": ["1"]},
        ])

    assert manager._data["tasks"]["1"]["depend_on"] == []
    assert manager._data["tasks"]["2"]["depend_on"] == []


def test_delete_task_removes_dependency_references(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager._data["tasks"]["2"]["depend_on"] = ["1"]
    manager._data["tasks"]["3"]["depend_on"] = ["1", "2"]

    deleted = manager.delete_task("1")

    assert deleted["id"] == "1"
    assert set(manager._data["tasks"]) == {"2", "3"}
    assert manager._data["tasks"]["2"]["depend_on"] == []
    assert manager._data["tasks"]["3"]["depend_on"] == ["2"]


def test_delete_task_rejects_unknown_id_without_changes(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    original = {task_id: task.copy() for task_id, task in manager._data["tasks"].items()}

    with pytest.raises(ValueError, match="Task missing not found"):
        manager.delete_task("missing")

    assert manager._data["tasks"] == original
