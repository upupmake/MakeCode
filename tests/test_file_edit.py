from utils import common


def _workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))
    return tmp_path


def test_file_edit_does_not_require_prior_file_read(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("before\ntarget\nafter\n", encoding="utf-8")

    result = common.file_edit(
        "sample.txt",
        [{
            "search_content": "before\ntarget\nafter",
            "replace_content": "before\nupdated\nafter",
        }],
    )

    assert result.startswith("Edited sample.txt: applied 1 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == "before\nupdated\nafter\n"


def test_file_edit_failed_match_does_not_modify_file(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
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

    assert result.startswith("Error: FileEdit rejected 1 edit block(s)")
    assert "Block 1: search_content not found" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_reports_matched_line_ranges(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("a = 1\nb = 2\nc = 3\nd = 4\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "b = 2\nc = 3", "replace_content": "b = 20\nc = 30"}],
    )

    assert "#1 exact" in result
    assert "lines 2-3 -> 2 line(s)" in result


def test_file_edit_rejects_overlapping_changed_blocks(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "def f(a):\n    x = 1\n    y = 2\n    z = 3\n    return x\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "    x = 1\n    y = 2", "replace_content": "    x = 1\n    y = 20"},
            {"search_content": "    y = 2\n    z = 3", "replace_content": "    y = 2\n    z = 30"},
        ],
    )

    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert "conflicting changes between blocks #1, #2" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_allows_adjacent_blocks_sharing_an_unchanged_context_line(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def f(a):\n    x = 1\n    y = 2\n    z = 3\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "def f(a):\n    x = 1\n    y = 2",
             "replace_content": "def f(a, b):\n    x = 1\n    y = 2"},
            {"search_content": "    x = 1\n    y = 2\n    z = 3",
             "replace_content": "    x = 1\n    y = 2\n    z = 300"},
        ],
    )

    assert result.startswith("Edited sample.py: applied 2 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == "def f(a, b):\n    x = 1\n    y = 2\n    z = 300\n"


def test_file_edit_rejects_blocks_out_of_forward_order(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def a():\n    p()\n    q()\n\ndef b():\n    p()\n    r()\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "def b():\n    p()\n    r()", "replace_content": "def b():\n    p()\n    q()"},
            {"search_content": "    p()\n    q()", "replace_content": "    p()\n    q2()"},
        ],
    )

    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert target.read_text(encoding="utf-8") == (
        "def a():\n    p()\n    q()\n\ndef b():\n    p()\n    r()\n"
    )


def test_file_edit_later_block_disambiguates_earlier_repeated_match(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text(
        "class A\n"
        "return value\n"
        "class B\n"
        "return value\n"
        "class C\n"
        "return other\n",
        encoding="utf-8",
    )

    result = common.file_edit(
        "sample.txt",
        [
            {"search_content": "return value", "replace_content": "return first"},
            {
                "search_content": "class B\nreturn value",
                "replace_content": "class B\nreturn second",
            },
        ],
    )

    assert result.startswith("Edited sample.txt: applied 2 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == (
        "class A\n"
        "return first\n"
        "class B\n"
        "return second\n"
        "class C\n"
        "return other\n"
    )


def test_file_edit_consumes_three_repeated_code_blocks_in_order(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "Service.java"
    original = (
        "class Service {\n"
        "    void one() { return query(); }\n"
        "    void one() { return query(); }\n"
        "    void one() { return query(); }\n"
        "}\n"
    )
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "Service.java",
        [
            {"search_content": "    void one() { return query(); }",
             "replace_content": "    void one() { return first(); }"},
            {"search_content": "    void one() { return query(); }",
             "replace_content": "    void one() { return second(); }"},
            {"search_content": "    void one() { return query(); }",
             "replace_content": "    void one() { return third(); }"},
        ],
    )

    assert result.startswith("Edited Service.java: applied 3 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == (
        "class Service {\n"
        "    void one() { return first(); }\n"
        "    void one() { return second(); }\n"
        "    void one() { return third(); }\n"
        "}\n"
    )


def test_file_edit_preserves_changes_when_blocks_share_unchanged_context(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text(
        "header\n"
        "first\n"
        "shared\n"
        "second\n"
        "footer\n",
        encoding="utf-8",
    )

    result = common.file_edit(
        "sample.txt",
        [
            {
                "search_content": "header\nfirst\nshared",
                "replace_content": "HEADER\nfirst changed\nshared",
            },
            {
                "search_content": "shared\nsecond\nfooter",
                "replace_content": "shared\nsecond changed\nFOOTER",
            },
        ],
    )

    assert result.startswith("Edited sample.txt: applied 2 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == (
        "HEADER\n"
        "first changed\n"
        "shared\n"
        "second changed\n"
        "FOOTER\n"
    )


def test_file_edit_rejects_change_in_shared_context_after_indent_matching(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "root\n    one\n    shared\n    two\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {
                "search_content": "  one\n  shared",
                "replace_content": "  one\n  changed shared",
            },
            {
                "search_content": "    shared\n    two",
                "replace_content": "    changed shared\n    two",
            },
        ],
    )

    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert "conflicting changes between blocks #1, #2" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_rejects_same_snapshot_insertion_point_conflict(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    original = "before\nanchor\nafter\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.txt",
        [
            {"search_content": "anchor", "replace_content": "anchor\nfirst"},
            {"search_content": "anchor", "replace_content": "anchor\nsecond"},
        ],
    )

    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert "conflicting changes between blocks #1, #2" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_applies_multiple_blocks_without_index_drift(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "a = 1", "replace_content": "a = 10\na_extra = 0"},
            {"search_content": "c = 3", "replace_content": "c = 30"},
            {"search_content": "e = 5", "replace_content": "e = 50"},
        ],
    )

    assert result.startswith("Edited sample.py: applied 3 edit block(s) atomically.")
    assert target.read_text(encoding="utf-8") == (
        "a = 10\na_extra = 0\nb = 2\nc = 30\nd = 4\ne = 50\n"
    )


def test_file_edit_reports_every_failing_block_at_once(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "a = 1\nb = 2\nc = 3\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "a = 1", "replace_content": "a = 10"},
            {"search_content": "missing one", "replace_content": "x"},
            {"search_content": "missing two", "replace_content": "y"},
        ],
    )

    assert "3 edit block(s)" in result
    assert "2 problem(s) found" in result
    assert "Block 2: search_content not found" in result
    assert "Block 3: search_content not found" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_rejects_chained_blocks(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"

    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "a = 1", "replace_content": "renamed = 1"},
            {"search_content": "renamed = 1", "replace_content": "renamed = 100"},
            {"search_content": "renamed = 100", "replace_content": "renamed = 999"},
        ],
    )
    assert result.startswith("Error: FileEdit rejected 3 edit block(s)")
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"

    # The dependency may also run against block order.
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "renamed = 1", "replace_content": "renamed = 100"},
            {"search_content": "a = 1", "replace_content": "renamed = 1"},
        ],
    )
    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_file_edit_rejects_sequence_that_needs_intermediate_disambiguation(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def a():\n    p()\n    q()\n\ndef b():\n    p()\n    q()\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [
            {"search_content": "def b():\n    p()\n    q()", "replace_content": "def b():\n    p()\n    r()"},
            {"search_content": "    p()\n    q()", "replace_content": "    p()\n    q2()"},
        ],
    )

    assert result.startswith("Error: FileEdit rejected 2 edit block(s)")
    assert target.read_text(encoding="utf-8") == (
        "def a():\n    p()\n    q()\n\ndef b():\n    p()\n    q()\n"
    )


def test_file_edit_rejects_ambiguous_block_with_candidate_line_numbers(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "p()\nq()\n\np()\nq()\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "p()\nq()", "replace_content": "p()\nq2()"}],
    )

    assert "matches 2 locations" in result
    assert "lines 1-2" in result
    assert "lines 4-5" in result
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_detects_copied_line_number_prefixes(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "def f():\n    x = 1\n    y = 2\n"
    target.write_text(original, encoding="utf-8")

    for search in ("2:    x = 1\n3:    y = 2", "2:     x = 1\n3:     y = 2"):
        result = common.file_edit(
            "sample.py",
            [{"search_content": search, "replace_content": "    x = 10\n    y = 2"}],
        )
        assert "'<line number>:' prefix" in result
        assert target.read_text(encoding="utf-8") == original


def test_file_edit_matches_uniform_indent_shift_and_reindents_replacement(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def f():\n    x = 1\n    y = 2\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "  x = 1\n  y = 2", "replace_content": "  x = 111\n  y = 222"}],
    )

    assert "#1 reindented" in result
    assert "(indent +2)" in result
    assert target.read_text(encoding="utf-8") == "def f():\n    x = 111\n    y = 222\n"


def test_file_edit_falls_back_to_trailing_whitespace_matching(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def f():\n    return value;   \n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{
            "search_content": "    return value;",
            "replace_content": "    return updated;",
        }],
    )

    assert "#1 trailing-whitespace" in result
    assert target.read_text(encoding="utf-8") == "def f():\n    return updated;\n"


def test_file_edit_whitespace_fallback_preserves_unchanged_context(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "Service.java"
    original = (
        "class Service {   \n"
        "    String value() {\n"
        "        return query();\n"
        "    }\n"
        "}\n"
    )
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "Service.java",
        [{
            "search_content": "class Service {\n    String value() {\n        return query();",
            "replace_content": "class Service {\n    String value() {\n        return cachedQuery();",
        }],
    )

    assert "#1 trailing-whitespace" in result
    assert target.read_text(encoding="utf-8") == (
        "class Service {\n"
        "    String value() {\n"
        "        return cachedQuery();\n"
        "    }\n"
        "}\n"
    )


def test_file_edit_falls_back_to_leading_and_trailing_whitespace_matching(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("def f():\n    return value;   \n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{
            "search_content": "return value;",
            "replace_content": "return updated;",
        }],
    )

    assert "#1 reindented" in result
    assert target.read_text(encoding="utf-8") == "def f():\n    return updated;\n"


def test_file_edit_does_not_match_across_partial_lines(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    original = "def g(xs):\n    return xs\n"
    target.write_text(original, encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "return x", "replace_content": "return y"}],
    )

    assert result.startswith("Error: FileEdit rejected 1 edit block(s)")
    assert target.read_text(encoding="utf-8") == original


def test_file_edit_deletes_matched_lines_on_empty_replacement(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "b = 2", "replace_content": ""}],
    )

    assert "-> 0 line(s)" in result
    assert target.read_text(encoding="utf-8") == "a = 1\nc = 3\n"


def test_file_edit_insertion_at_unterminated_eof_keeps_line_boundary(
    monkeypatch, tmp_path
):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.py"
    target.write_bytes(b"a = 1\nb = 2")

    result = common.file_edit(
        "sample.py",
        [{"search_content": "b = 2", "replace_content": "b = 2\nc = 3"}],
    )

    assert result.startswith("Edited sample.py: applied 1 edit block(s) atomically.")
    assert target.read_bytes() == b"a = 1\nb = 2\nc = 3"


def test_file_edit_rejects_empty_edit_list(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "sample.txt"
    original = b"a = 1\r\nb = 2"
    target.write_bytes(original)

    result = common.file_edit("sample.txt", [])

    assert result.startswith("Error: Invalid arguments provided to FileEdit.")
    assert target.read_bytes() == original


def test_file_edit_preserves_crlf_bom_and_missing_trailing_newline(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)

    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
    common.file_edit("crlf.txt", [{"search_content": "b = 2", "replace_content": "b = 20"}])
    assert crlf.read_bytes() == b"a = 1\r\nb = 20\r\nc = 3\r\n"

    no_newline = tmp_path / "tail.txt"
    no_newline.write_bytes(b"a = 1\nb = 2")
    common.file_edit("tail.txt", [{"search_content": "b = 2", "replace_content": "b = 20"}])
    assert no_newline.read_bytes() == b"a = 1\nb = 20"

    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbfa = 1\nb = 2\n")
    common.file_edit("bom.txt", [{"search_content": "b = 2", "replace_content": "b = 20"}])
    assert bom.read_bytes() == b"\xef\xbb\xbfa = 1\nb = 20\n"


def test_file_edit_preserves_mixed_line_endings_outside_changes(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    target = tmp_path / "mixed.java"
    target.write_bytes(b"class A {\r\n    old();\n    keep();\r}\r\n")

    result = common.file_edit(
        "mixed.java",
        [{"search_content": "    old();", "replace_content": "    updated();"}],
    )

    assert result.startswith("Edited mixed.java: applied 1 edit block(s) atomically.")
    assert target.read_bytes() == b"class A {\r\n    updated();\n    keep();\r}\r\n"


def test_file_edit_refuses_non_utf8_and_binary_files(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)

    latin1 = tmp_path / "latin1.txt"
    latin1.write_bytes("caf\u00e9 = 1\n".encode("latin-1"))
    result = common.file_edit("latin1.txt", [{"search_content": "= 1", "replace_content": "= 2"}])
    assert "is not valid UTF-8" in result
    assert latin1.read_bytes() == "caf\u00e9 = 1\n".encode("latin-1")

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01binary")
    result = common.file_edit("blob.bin", [{"search_content": "binary", "replace_content": "text"}])
    assert "appears to be a binary file" in result
    assert binary.read_bytes() == b"\x00\x01binary"
