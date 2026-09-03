from utils import common, text_tokens


def _workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(common.paths, "_WORKDIR", tmp_path)
    return tmp_path


def test_file_read_renders_grep_style_prefix_without_a_separator_space(monkeypatch, tmp_path):
    """Everything after the first colon must be the verbatim line, indentation included."""
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "sample.py").write_text(
        "def outer():\n    if cond:\n        deep_call(a, b)\n",
        encoding="utf-8",
    )

    result = common.file_read("sample.py", [{"start": 1, "end": 3}])
    rendered = result.splitlines()[1:]

    assert rendered == [
        "1:def outer():",
        "2:    if cond:",
        "3:        deep_call(a, b)",
    ]
    assert [line.split(":", 1)[1] for line in rendered] == [
        "def outer():",
        "    if cond:",
        "        deep_call(a, b)",
    ]


def test_file_read_marks_skipped_lines_between_non_adjacent_regions(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "sample.py").write_text(
        "\n".join(f"line_{index}" for index in range(1, 8)) + "\n",
        encoding="utf-8",
    )

    result = common.file_read("sample.py", [{"start": 1, "end": 2}, {"start": 6, "end": 7}])

    assert result.splitlines()[1:] == [
        "1:line_1",
        "2:line_2",
        "@@ 3-5 skipped @@",
        "6:line_6",
        "7:line_7",
    ]


def test_file_read_merges_adjacent_regions_without_a_marker(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "sample.py").write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = common.file_read("sample.py", [{"start": 1, "end": 2}, {"start": 3, "end": 4}])

    assert "skipped" not in result
    assert result.splitlines()[1:] == ["1:a", "2:b", "3:c", "4:d"]


def test_file_read_output_survives_a_file_edit_round_trip(monkeypatch, tmp_path):
    """A block copied out of FileRead output, minus the prefix, must match exactly."""
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))
    target = tmp_path / "sample.py"
    target.write_text("def outer():\n    if cond:\n        deep_call(a, b)\n", encoding="utf-8")

    rendered = common.file_read("sample.py", [{"start": 2, "end": 3}]).splitlines()[1:]
    search_content = "\n".join(line.split(":", 1)[1] for line in rendered)

    result = common.file_edit(
        "sample.py",
        [{"search_content": search_content, "replace_content": "    if cond:\n        shallow(a)"}],
    )

    assert "#1 exact" in result
    assert target.read_text(encoding="utf-8") == "def outer():\n    if cond:\n        shallow(a)\n"


def test_file_read_does_not_expose_the_bom_as_line_content(monkeypatch, tmp_path):
    """FileEdit strips the BOM, so FileRead must not render it into line 1."""
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))
    target = tmp_path / "sample.py"
    target.write_bytes(b"\xef\xbb\xbfdef outer():\n    body()\n")

    rendered = common.file_read("sample.py", [{"start": 1, "end": 2}]).splitlines()[1:]

    assert rendered == ["1:def outer():", "2:    body()"]

    search_content = "\n".join(line.split(":", 1)[1] for line in rendered)
    result = common.file_edit(
        "sample.py",
        [{"search_content": search_content, "replace_content": "def outer(flag):\n    body()"}],
    )

    assert "#1 exact" in result
    assert target.read_bytes() == b"\xef\xbb\xbfdef outer(flag):\n    body()\n"


def test_truncated_file_read_keeps_whole_lines_around_the_marker(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "big.py").write_text(
        "\n".join(f"value_{index} = {index}" for index in range(4000)) + "\n",
        encoding="utf-8",
    )

    result = common.file_read("big.py", [{"start": 1, "end": 4000}])
    marker = common._OUTPUT_TRUNCATION_MARKER_PATTERN.search(result)

    assert marker is not None
    head_last = result[:marker.start()].splitlines()[-1]
    tail_first = result[marker.end():].splitlines()[0]
    assert common._LINE_NUMBER_PREFIX_PATTERN.match(head_last)
    assert common._LINE_NUMBER_PREFIX_PATTERN.match(tail_first)
    number, payload = head_last.split(":", 1)
    assert payload == f"value_{int(number) - 1} = {int(number) - 1}"
    number, payload = tail_first.split(":", 1)
    assert payload == f"value_{int(number) - 1} = {int(number) - 1}"


def test_truncate_output_line_alignment_is_opt_in(monkeypatch):
    text = "甲" * 5000 + "\n" + "乙" * 5000

    assert "\n" in common.truncate_output(text)
    assert common.truncate_output(text, line_aligned=True).count("\n") >= 4


def test_truncate_output_line_alignment_tolerates_text_without_newlines():
    text = "甲" * 5000 + "乙" * 5000
    aligned = common.truncate_output(text, line_aligned=True)

    assert aligned == common.truncate_output(text)
    assert "甲" in aligned and "乙" in aligned


def test_truncate_output_returns_short_text_unchanged_when_line_aligned():
    assert common.truncate_output("a\nb\nc", line_aligned=True) == "a\nb\nc"
    assert text_tokens.estimate_text_tokens("a\nb\nc") < 8000


def test_file_create_line_count_matches_file_read_total_lines(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(common, "check_permission", lambda *args: (True, ""))

    for name, content, expected in [
        ("trailing.py", "a = 1\nb = 2\n", 2),
        ("no_trailing.py", "a = 1\nb = 2", 2),
        ("single.py", "a = 1", 1),
    ]:
        created = common.file_create(name, content)
        read = common.file_read(name, [{"start": 1, "end": 999}])

        assert created == f"Created {name}: {expected} lines written"
        assert read.splitlines()[0].endswith(f"Total lines: {expected}")
