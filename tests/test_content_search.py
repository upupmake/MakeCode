from pathlib import Path

import pytest

from utils import common
from utils.tool_validation import ToolArgumentValidationError, validate_builtin_tool_arguments


def _search_in_workspace(monkeypatch, tmp_path: Path, content_regex: str, context_size: int | None = None) -> str:
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    (tmp_path / "sample.txt").write_text(
        "before\nfirst needle\nbetween\nsecond needle\nafter\n",
        encoding="utf-8",
    )
    if context_size is None:
        return common.content_search(content_regex)
    return common.content_search(content_regex, context_size=context_size)


def test_content_search_defaults_to_one_context_line(monkeypatch, tmp_path):
    result = _search_in_workspace(monkeypatch, tmp_path, "needle")

    assert "1- before" in result
    assert "2: first needle" in result
    assert "3- between" in result
    assert "4: second needle" in result
    assert "5- after" in result


def test_content_search_merges_overlapping_context_ranges(monkeypatch, tmp_path):
    result = _search_in_workspace(monkeypatch, tmp_path, "needle", context_size=1)

    assert result.count("--") == 0
    assert result.index("1- before") < result.index("2: first needle")
    assert result.index("3- between") < result.index("4: second needle")


def test_content_search_context_is_limited_at_file_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    (tmp_path / "sample.txt").write_text("first needle\nlast\n", encoding="utf-8")

    result = common.content_search("needle", context_size=3)

    assert "1: first needle" in result
    assert "2- last" in result
    assert "0" not in result


def test_content_search_zero_context_returns_only_matching_lines(monkeypatch, tmp_path):
    result = _search_in_workspace(monkeypatch, tmp_path, "needle", context_size=0)

    assert "2: first needle" in result
    assert "4: second needle" in result
    assert "1- before" not in result
    assert "3- between" not in result
    assert "5- after" not in result


def test_content_search_rejects_negative_context_size(tmp_path, monkeypatch):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)

    result = common.content_search("needle", context_size=-1)

    assert result == "Error: context_size must be non-negative, got -1."


def test_content_search_schema_defaults_context_size_to_one():
    assert common.ContentSearch(content_regex="needle").context_size == 1
    with pytest.raises(ValueError):
        common.ContentSearch(content_regex="needle", context_size=-1)


def test_content_search_rejects_unknown_arguments():
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_builtin_tool_arguments(
            "ContentSearch",
            {
                "content_regex": "needle",
                "filename": "_regex>.*\\.py$",
            },
            common.ContentSearch,
        )

    error = str(exc_info.value)
    assert "filename" in error
    assert "extra_forbidden" in error
    assert "filename_regex" in error
    assert r"Input value: '_regex>.*\\.py$'" in error
