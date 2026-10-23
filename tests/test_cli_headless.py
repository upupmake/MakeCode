import pytest

from system import cli as cli_module


def parse(*args):
    assert cli_module.run_external_cli(list(args)) is None
    return cli_module.HEADLESS_OPTIONS


def test_print_prompt_builds_headless_options():
    options = parse("-p", "hello")

    assert options is not None
    assert options.prompt == "hello"
    assert options.input_format == "text"
    assert options.output_format == "text"
    assert options.permission_mode == "default"


def test_print_without_prompt_reads_stdin_marker():
    options = parse("-p")

    assert options is not None
    assert options.prompt == ""


def test_positional_prompt_matches_claude_code_form():
    # claude -p --output-format json "prompt"
    options = parse("-p", "--output-format", "json", "hello")
    assert options is not None
    assert options.prompt == "hello"
    assert options.output_format == "json"

    # claude "prompt" -p
    options = parse("hello", "-p")
    assert options is not None
    assert options.prompt == "hello"


def test_prompt_cannot_be_provided_twice():
    with pytest.raises(SystemExit) as exc_info:
        cli_module.run_external_cli(["-p", "first", "second"])

    assert exc_info.value.code == 2


def test_positional_prompt_requires_print_mode():
    with pytest.raises(SystemExit) as exc_info:
        cli_module.run_external_cli(["hello"])

    assert exc_info.value.code == 2


def test_print_mode_accepts_stream_json_input_like_claude_code():
    options = parse(
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    )

    assert options is not None
    assert options.prompt == ""
    assert options.input_format == "stream-json"
    assert options.output_format == "stream-json"


def test_output_format_json_and_session_options():
    options = parse(
        "-p",
        "hello",
        "--output-format",
        "json",
        "--session-id",
        "conv_0123456789abcdef0123456789abcdef",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "FileEdit",
        "Bash",
        "--disallowedTools",
        "RunTerminalCommand",
        "--append-system-prompt",
        "be concise",
    )

    assert options is not None
    assert options.output_format == "json"
    assert options.session_id == "conv_0123456789abcdef0123456789abcdef"
    assert options.permission_mode == "acceptEdits"
    assert options.allowed_tools == ("FileEdit", "Bash")
    assert options.disallowed_tools == ("RunTerminalCommand",)
    assert options.append_system_prompt == "be concise"


def test_tool_lists_accept_comma_separated_values():
    options = parse(
        "-p",
        "hello",
        "--allowedTools",
        "File, Terminal,RunTerminalCommand",
        "--disallowedTools",
        "FileCreate,FileEdit",
    )

    assert options is not None
    assert options.allowed_tools == ("File", "Terminal", "RunTerminalCommand")
    assert options.disallowed_tools == ("FileCreate", "FileEdit")


def test_permission_mode_aliases_follow_claude_code():
    assert parse("-p", "hello", "--permission-mode", "manual").permission_mode == "default"
    assert parse("-p", "hello", "--permission-mode", "dontAsk").permission_mode == "default"


def test_debug_short_flag_is_accepted():
    options = parse("-p", "hello", "-d")

    assert options is not None
    assert options.debug == ""


def test_resume_continue_and_fork_options():
    options = parse("-r", "conv_0123456789abcdef0123456789abcdef", "-p", "hello")
    assert options is not None
    assert options.resume == "conv_0123456789abcdef0123456789abcdef"

    options = parse("-c", "-p", "hello")
    assert options is not None
    assert options.continue_latest is True

    options = parse(
        "--fork-session",
        "-r",
        "conv_0123456789abcdef0123456789abcdef",
        "-p",
        "hello",
    )
    assert options is not None
    assert options.fork_session is True


def test_stream_json_input_parses():
    options = parse("--input-format", "stream-json", "--output-format", "stream-json")

    assert options is not None
    assert options.prompt is None
    assert options.input_format == "stream-json"
    assert options.output_format == "stream-json"


def test_input_and_output_formats_are_independent():
    for output_format in ("text", "json"):
        options = parse(
            "--input-format", "stream-json",
            "--output-format", output_format,
        )
        assert options is not None
        assert options.input_format == "stream-json"
        assert options.output_format == output_format


def test_json_is_an_alias_for_stream_json_input():
    options = parse("--input-format", "json", "--output-format", "text")

    assert options is not None
    assert options.input_format == "stream-json"
    assert options.output_format == "text"


def test_explicit_text_input_without_prompt_enters_persistent_mode():
    options = parse("--input-format", "text")

    assert options is not None
    assert options.prompt is None
    assert options.input_format == "text"
    assert options.output_format == "text"


def test_text_input_allows_any_output_format():
    options = parse("--input-format", "text", "--output-format", "json")

    assert options is not None
    assert options.input_format == "text"
    assert options.output_format == "json"


def test_print_still_wins_over_persistent_text_mode():
    options = parse("--input-format", "text", "-p", "hello")

    assert options is not None
    assert options.prompt == "hello"


def test_default_invocation_still_starts_tui():
    assert cli_module.run_external_cli([]) is None
    assert cli_module.HEADLESS_OPTIONS is None


@pytest.mark.parametrize(
    "args",
    [
        ("-p", "hello", "--fork-session"),
        ("-p", "hello", "--session-id", "conv_x", "-c"),
        ("-p", "hello", "--input-format", "stream-json"),
        ("-p", "hello", "--include-partial-messages"),
        ("-p", "hello", "--input-format", "stream-json", "--output-format", "stream-json"),
        ("--input-format", "stream-json", "--output-format", "json", "--replay-user-messages"),
        ("-p", "hello", "--input-format", "json"),
        ("-p", "hello", "--simple"),
        ("-p", "hello", "--load"),
        ("--output-format", "json"),
        ("-p", "hello", "-r"),
    ],
)
def test_invalid_headless_combinations_exit_with_usage_error(args):
    with pytest.raises(SystemExit) as exc_info:
        cli_module.run_external_cli(list(args))

    assert exc_info.value.code == 2
