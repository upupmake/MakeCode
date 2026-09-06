import importlib

import pytest

from utils import common


patch_impl = importlib.import_module("utils.file_patch")


def _workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    monkeypatch.setattr(patch_impl.paths, "_WORKDIR", tmp_path)
    monkeypatch.setattr(patch_impl, "check_permission", lambda *args: (True, ""))
    monkeypatch.setattr(patch_impl, "check_paths_permission", lambda *args: (True, ""))
    return tmp_path


def test_file_patch_updates_an_existing_file(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "app.py"
    target.write_text("def run():\n    return 1\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: app.py\n"
        "@@\n"
        " def run():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert "M app.py (1 hunk(s))" in result
    assert target.read_text(encoding="utf-8") == "def run():\n    return 2\n"


def test_file_patch_applies_multiple_non_overlapping_hunks_bottom_up(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "@@\n"
        " c\n"
        "-d\n"
        "+D\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "a\nB\nc\nD\n"


def test_file_patch_adds_and_deletes_files_in_one_call(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    old = tmp_path / "old.txt"
    old.write_text("old\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "+new\n"
        "+content\n"
        "*** Delete File: old.txt\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 2 file(s) atomically.")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new\ncontent\n"
    assert not old.exists()


def test_file_patch_supports_absolute_and_parent_paths_inside_workspace(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "nested" / "sample.txt"
    target.parent.mkdir()
    target.write_text("before\n", encoding="utf-8")

    result = common.file_patch(
        f"*** Begin Patch\n*** Update File: {target.parent / '..' / target.parent.name / target.name}\n@@\n-before\n+after\n*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "after\n"


def test_file_patch_rejects_ambiguous_hunk_without_writing(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    original = "same\nvalue\n\nsame\nvalue\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@\n"
        " same\n"
        "-value\n"
        "+changed\n"
        "*** End Patch"
    )

    assert result.startswith("Error: FilePatch completed partially: 0 file(s) patched, 1 file(s) failed.")
    assert "matches multiple locations" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_patch_updates_an_existing_empty_file_with_pure_addition(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: empty.txt\n"
        "@@\n"
        "+content\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "content\n"


def test_file_patch_uses_coordinates_for_pure_addition_in_non_empty_file(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@ -1,0 +2,1 @@\n"
        "+inserted\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "first\ninserted\nsecond\n"


def test_file_patch_pure_addition_at_eof_preserves_line_boundaries_without_final_newline(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("first", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@ -1,0 +2,1 @@\n"
        "+second\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "first\nsecond"


def test_file_patch_commits_valid_file_when_another_fails(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    first = tmp_path / "first.txt"
    first.write_text("before\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: first.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** Update File: missing.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch"
    )

    assert result.startswith("Error: FilePatch completed partially:")
    assert "missing.txt: target not found: missing.txt" in result
    assert first.read_text(encoding="utf-8") == "after\n"


def test_file_patch_rejects_case_aliases_of_the_same_file(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    first = tmp_path / "Case.txt"
    alias = tmp_path / "case.txt"
    first.write_text("before\n", encoding="utf-8")
    if not alias.exists():
        pytest.skip("test filesystem is case-sensitive")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: Case.txt\n"
        "@@\n"
        "-before\n"
        "+first change\n"
        "*** Update File: case.txt\n"
        "@@\n"
        "-before\n"
        "+second change\n"
        "*** End Patch"
    )

    assert result.startswith(
        "Error: FilePatch completed partially: 0 file(s) patched, 2 file(s) failed."
    )
    assert "resolved paths refer to the same file" in result
    assert first.read_text(encoding="utf-8") == "before\n"


def test_file_patch_preserves_bom_and_crlf(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_bytes(b"\xef\xbb\xbfa = 1\r\nb = 2\r\n")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@\n"
        " a = 1\n"
        "-b = 2\n"
        "+b = 3\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_bytes() == b"\xef\xbb\xbfa = 1\r\nb = 3\r\n"


def test_file_patch_preserves_mixed_line_endings_outside_the_hunk(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"a\r\nb\nc\r")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: mixed.txt\n"
        "@@\n"
        " a\r\n"
        "-b\n"
        "+B\n"
        " c\r\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_bytes() == b"a\r\nB\nc\r"


def test_file_patch_supports_control_prefixes_in_context(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "prefixes.txt"
    target.write_text("+keep\n@@keep\n*** keep\nvalue\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: prefixes.txt\n"
        "@@\n"
        " +keep\n"
        " @@keep\n"
        " *** keep\n"
        "-value\n"
        "+changed\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "+keep\n@@keep\n*** keep\nchanged\n"


def test_file_patch_supports_a_literal_backslash_in_context(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "prefixes.txt"
    target.write_text("\\+keep\nvalue\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: prefixes.txt\n"
        "@@\n"
        " \\+keep\n"
        "-value\n"
        "+changed\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert target.read_text(encoding="utf-8") == "\\+keep\nchanged\n"


def test_file_patch_keeps_valid_files_when_another_file_section_is_malformed(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("before\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: good.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** Update File: bad.txt\n"
        "not a hunk\n"
        "*** End Patch"
    )

    assert result.startswith("Error: FilePatch completed partially: 1 file(s) patched, 1 file(s) failed.")
    assert "bad.txt: expected '@@'" in result
    assert good.read_text(encoding="utf-8") == "after\n"


def test_file_patch_requires_plus_lines_for_added_files(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "not prefixed\n"
        "*** End Patch"
    )

    assert result.startswith("Error: FilePatch completed partially: 0 file(s) patched, 1 file(s) failed.")
    assert "new.txt: added file new.txt must contain only '+' lines" in result
    assert not (tmp_path / "new.txt").exists()


def test_file_patch_requests_one_approval_for_all_external_paths(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    outside_a = tmp_path.parent / "file_patch_external_a.txt"
    outside_b = tmp_path.parent / "file_patch_external_b.txt"
    outside_a.write_text("a\n", encoding="utf-8")
    outside_b.write_text("b\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        patch_impl,
        "check_paths_permission",
        lambda paths, tool: calls.append((paths, tool)) or (True, ""),
    )

    result = common.file_patch(
        f"*** Begin Patch\n"
        f"*** Update File: {outside_a}\n@@\n-a\n+A\n"
        f"*** Update File: {outside_b}\n@@\n-b\n+B\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 2 file(s) atomically.")
    assert len(calls) == 1
    assert {path.resolve() for path in calls[0][0]} == {outside_a.resolve(), outside_b.resolve()}
    assert outside_a.read_text(encoding="utf-8") == "A\n"
    assert outside_b.read_text(encoding="utf-8") == "B\n"


def test_file_patch_rolls_back_when_commit_fails(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    original_replace = patch_impl.os.replace
    calls = 0

    def fail_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("simulated commit failure")
        return original_replace(source, destination)

    monkeypatch.setattr(patch_impl.os, "replace", fail_once)
    result = common.file_patch(
        f"*** Begin Patch\n"
        f"*** Update File: {first.name}\n@@\n-one\n+ONE\n"
        f"*** Update File: {second.name}\n@@\n-two\n+TWO\n"
        "*** End Patch"
    )

    assert result.startswith("Error: FilePatch completed partially:")
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_file_patch_keeps_other_files_when_one_path_cannot_be_resolved(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("before\n", encoding="utf-8")

    original_resolve = patch_impl._resolve_patch_path

    def fail_for_one_path(raw_path, workdir):
        if raw_path == "broken.txt":
            raise RuntimeError("simulated symlink loop")
        return original_resolve(raw_path, workdir)

    monkeypatch.setattr(patch_impl, "_resolve_patch_path", fail_for_one_path)
    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: good.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** Update File: broken.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch"
    )

    assert result.startswith(
        "Error: FilePatch completed partially: 1 file(s) patched, 1 file(s) failed."
    )
    assert "broken.txt: could not resolve path: simulated symlink loop" in result
    assert good.read_text(encoding="utf-8") == "after\n"


def test_file_patch_retains_backup_when_rollback_restore_fails(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    original_replace = patch_impl.os.replace
    calls = 0

    def fail_commit_and_restore(source, destination):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("simulated replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(patch_impl.os, "replace", fail_commit_and_restore)
    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch"
    )

    backups = list(tmp_path.glob(".sample.txt.makecode-backup-*"))
    assert result.startswith(
        "Error: FilePatch completed partially: 0 file(s) patched, 1 file(s) failed."
    )
    assert "rollback failed and recovery backup was retained" in result
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "before\n"


def test_file_patch_does_not_turn_validation_exception_into_commit_failure(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        patch_impl,
        "validate_code",
        lambda *args: (_ for _ in ()).throw(RuntimeError("validator unavailable")),
    )

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: sample.py\n"
        "@@\n"
        "-value = 1\n"
        "+value = 2\n"
        "*** End Patch"
    )

    assert result.startswith("Patched 1 file(s) atomically.")
    assert "syntax validation failed for sample.py: validator unavailable" in result
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_file_patch_keeps_valid_files_when_another_file_path_is_invalid(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("before\n", encoding="utf-8")

    result = common.file_patch(
        "*** Begin Patch\n"
        "*** Update File: \x00bad.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** Update File: good.txt\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch"
    )

    assert result.startswith(
        "Error: FilePatch completed partially: 1 file(s) patched, 1 file(s) failed."
    )
    assert "<invalid path>: invalid file path" in result
    assert good.read_text(encoding="utf-8") == "after\n"
