from utils import common


def test_file_edit_does_not_require_prior_file_read(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))
    target = tmp_path / "sample.txt"
    target.write_text("before\ntarget\nafter\n", encoding="utf-8")

    result = common.file_edit(
        "sample.txt",
        [{
            "search_content": "before\ntarget\nafter",
            "replace_content": "before\nupdated\nafter",
        }],
    )

    assert result == "Edited sample.txt: Successfully applied 1 edit block(s)."
    assert target.read_text(encoding="utf-8") == "before\nupdated\nafter\n"


def test_file_edit_failed_match_does_not_modify_file(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))
    target = tmp_path / "sample.txt"
    original = "before\ntarget\nafter\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.txt",
        [{
            "search_content": "completely unrelated content",
            "replace_content": "updated",
        }],
    )

    assert result.startswith("Error in edit block 1:")
    assert target.read_text(encoding="utf-8") == original
