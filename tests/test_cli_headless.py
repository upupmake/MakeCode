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


def test_stream_json_input_requires_stream_json_output():
    options = parse("--input-format", "stream-json", "--output-format", "stream-json")

    assert options is not None
    assert options.prompt is None
    assert options.input_format == "stream-json"
    assert options.output_format == "stream-json"


@pytest.mark.parametrize(
    "args",
    [
        ("-p", "hello", "--fork-session"),
        ("-p", "hello", "--session-id", "conv_x", "-c"),
        ("-p", "hello", "--input-format", "stream-json"),
        ("-p", "hello", "--include-partial-messages"),
        ("-p", "hello", "--input-format", "stream-json", "--output-format", "stream-json"),
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
