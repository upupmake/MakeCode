import json
import logging
import sys
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest

import system.headless as headless_module
from system.agent_runtime import TurnResult
from system.cli import HeadlessOptions
from system.headless import HeadlessConfigError, HeadlessUsageError
from utils.conversations import ConversationLockError, ConversationStore


class FakeRuntime:
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.restored = None
        self.prompt = None
        self.result = TurnResult(
            text="ok",
            session_id="conv_test",
            messages=[],
        )
        FakeRuntime.instances.append(self)

    def restore(self, snapshot):
        self.restored = snapshot

    def close(self):
        pass

    async def run_turn(self, prompt):
        self.prompt = prompt
        return self.result


class TtyStringIO(StringIO):
    def isatty(self):
        return True


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    FakeRuntime.instances = []

    def store_factory(*, persist=True):
        return ConversationStore(tmp_path / "conversations", persist=persist)

    monkeypatch.setattr(headless_module, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(headless_module, "ConversationStore", store_factory)
    monkeypatch.setattr(headless_module.HeadlessRunner, "_prepare_environment", lambda self: None)
    monkeypatch.setattr(headless_module.HeadlessRunner, "_prepare_model", lambda self: None)
    monkeypatch.setattr(headless_module.HeadlessRunner, "_start_mcp", lambda self: None)
    monkeypatch.setattr(headless_module.HeadlessRunner, "_stop_mcp", lambda self: None)
    return tmp_path


def test_text_mode_emits_init_json_then_answer(runner_env, capsys):
    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    init = json.loads(lines[0])
    assert result == 0
    assert lines[1:] == ["ok"]
    assert captured.err == ""
    assert init["type"] == "system"
    assert init["subtype"] == "init"
    assert init["mode"] == "one-shot"
    assert init["output_format"] == "text"
    assert init["session_id"].startswith("conv_")
    assert init["tool_count"] == len(init["tools"]) > 0
    assert FakeRuntime.instances[0].prompt == "hi"


def test_json_mode_emits_init_and_result_objects(runner_env, capsys):
    options = HeadlessOptions(prompt="hi", output_format="json")

    result = headless_module.run_headless(options)

    captured = capsys.readouterr()
    payloads = [
        json.loads(line)
        for line in captured.out.splitlines()
        if line.strip()
    ]
    assert result == 0
    assert [payload["type"] for payload in payloads] == ["system", "result"]
    init = payloads[0]
    assert init["subtype"] == "init"
    assert init["mode"] == "one-shot"
    assert init["input_format"] == "text"
    assert init["output_format"] == "json"
    assert init["session_persisted"] is True
    payload = payloads[-1]
    assert payload["type"] == "result"
    assert payload["subtype"] == "success"
    assert payload["result"] == "ok"
    assert payload["session_id"] == "conv_test"


def test_persistent_json_mode_emits_init_before_results(runner_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("hello\n"))
    options = HeadlessOptions(
        prompt=None,
        input_format="text",
        output_format="json",
    )

    result = headless_module.run_headless(options)

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert result == 0
    assert payloads[0]["type"] == "system"
    assert payloads[0]["subtype"] == "init"
    assert payloads[0]["session_id"].startswith("conv_")
    assert payloads[0]["mode"] == "persistent"
    assert payloads[0]["input_format"] == "text"
    assert payloads[0]["output_format"] == "json"
    assert payloads[0]["tool_count"] == len(payloads[0]["tools"]) > 0
    assert isinstance(payloads[0]["mcp_servers"], list)
    assert set(payloads[0]["skills"]) == {"enabled", "active", "total"}
    assert payloads[-1]["type"] == "result"
    assert payloads[-1]["result"] == "ok"


def test_persistent_text_mode_emits_init_json_on_stdout(runner_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("hello\n"))
    options = HeadlessOptions(prompt=None, input_format="text")

    result = headless_module.run_headless(options)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    init = json.loads(lines[0])
    assert result == 0
    assert lines[1:] == ["ok"]
    assert captured.err == ""
    assert init["type"] == "system"
    assert init["subtype"] == "init"
    assert init["mode"] == "persistent"
    assert init["session_id"].startswith("conv_")
    assert init["tool_count"] == len(init["tools"]) > 0


def test_error_result_returns_nonzero_and_writes_stderr(runner_env, capsys, monkeypatch):
    original_init = FakeRuntime.__init__

    def failing_init(self, **kwargs):
        original_init(self, **kwargs)
        self.result = TurnResult(
            text="",
            session_id="conv_test",
            messages=[],
            is_error=True,
            error="boom",
        )

    monkeypatch.setattr(FakeRuntime, "__init__", failing_init)

    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert result == 1
    assert json.loads(lines[0])["subtype"] == "init"
    assert lines[1:] == []
    assert "boom" in captured.err


def test_prompt_without_value_reads_stdin(runner_env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("from stdin\n"))

    result = headless_module.run_headless(HeadlessOptions(prompt=""))

    assert result == 0
    assert FakeRuntime.instances[0].prompt == "from stdin"


def test_signal_before_turn_aborts_without_calling_model(runner_env, capsys, monkeypatch):
    import asyncio

    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt="hi"))
    runner._stop_event = asyncio.Event()
    runner._stop_event.set()
    runtime = FakeRuntime()
    store = ConversationStore(runner_env / "conversations")
    store.create()

    result = asyncio.run(runner._run_turns(runtime, store))

    assert result == 130
    assert runtime.prompt is None
    assert capsys.readouterr().out == ""


def test_headless_mode_filters_mcp_termination_warning(caplog):
    logger = logging.getLogger("mcp.client.streamable_http")
    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt="hi"))

    with caplog.at_level(logging.WARNING, logger="mcp.client.streamable_http"):
        runner._enter_headless_mode()
        try:
            logger.warning("Session termination failed: 404")
        finally:
            runner._leave_headless_mode()

    assert "Session termination failed" not in caplog.text


def test_headless_bypass_permissions_disables_hitl():
    from utils import hitl

    previous = hitl.get_hitl_status()
    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt="hi"))

    try:
        runner._enter_headless_mode()
        assert hitl.get_hitl_status() is False
    finally:
        runner._leave_headless_mode()

    assert hitl.get_hitl_status() is previous


def test_headless_init_excludes_interactive_and_delegate_tools(runner_env, capsys):
    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" not in init["tools"]
    assert "DelegateTasks" not in init["tools"]
    assert "Task" not in init["tools"]


def test_persistent_text_ask_user_is_enabled_with_tty(
    runner_env, capsys, monkeypatch
):
    monkeypatch.setattr(sys, "stdin", TtyStringIO("hello\n"))

    result = headless_module.run_headless(HeadlessOptions(prompt=None))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" in init["tools"]
    assert init["ask_user"] == "enabled"
    assert init["ask_user_channel"] == "text"
    assert FakeRuntime.instances[0].kwargs["ask_user_available"] is True


def test_stream_json_auto_enables_ask_user(runner_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO(""))

    result = headless_module.run_headless(
        HeadlessOptions(
            prompt=None,
            input_format="stream-json",
            output_format="stream-json",
        )
    )

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" in init["tools"]
    assert init["ask_user"] == "enabled"
    assert init["ask_user_channel"] == "stream-json"
    assert FakeRuntime.instances[0].kwargs["ask_user_available"] is True


def test_auto_enables_ask_user_for_persistent_tty(monkeypatch):
    class TtyStdin:
        def isatty(self):
            return True

    monkeypatch.setattr(headless_module.sys, "stdin", TtyStdin())
    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt=None))

    assert runner._resolve_ask_user_enabled() is True


def test_persistent_text_without_tty_disables_ask_user(
    runner_env, capsys, monkeypatch
):
    monkeypatch.setattr(sys, "stdin", StringIO("hello\n"))

    result = headless_module.run_headless(HeadlessOptions(prompt=None))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" not in init["tools"]
    assert init["ask_user"] == "disabled"
    assert init["ask_user_channel"] is None


def test_headless_restores_ask_user_backend(runner_env, monkeypatch):
    from system.ask_user_backend import ASK_USER

    previous = ASK_USER.get_backend()
    monkeypatch.setattr(sys, "stdin", TtyStringIO("hello\n"))

    result = headless_module.run_headless(HeadlessOptions(prompt=None))

    assert result == 0
    assert ASK_USER.get_backend() is previous


def test_one_shot_tty_enables_ask_user(runner_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", TtyStringIO(""))

    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" in init["tools"]
    assert init["ask_user"] == "enabled"
    assert init["ask_user_channel"] == "text"
    assert FakeRuntime.instances[0].kwargs["ask_user_available"] is True


def test_one_shot_without_tty_disables_ask_user(runner_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO(""))

    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" not in init["tools"]
    assert init["ask_user"] == "disabled"


def test_prompt_read_from_stdin_disables_ask_user(
    runner_env, capsys, monkeypatch
):
    monkeypatch.setattr(sys, "stdin", StringIO("from stdin\n"))

    result = headless_module.run_headless(HeadlessOptions(prompt=""))

    init = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result == 0
    assert "AskUser" not in init["tools"]
    assert init["ask_user"] == "disabled"


def test_mcp_timeout_aborts_before_request(monkeypatch):
    class SlowMCPManager:
        def initialize(self, console):
            pass

        def start_background(self):
            pass

        def wait_until_ready(self, timeout=0):
            return False

    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt="hi"))
    runner.MCP_READY_TIMEOUT = 0.01
    monkeypatch.setattr(headless_module, "GLOBAL_MCP_MANAGER", SlowMCPManager())

    with pytest.raises(HeadlessConfigError, match="MCP 初始化"):
        runner._start_mcp()


def test_session_lock_conflict_is_config_error(runner_env, capsys, monkeypatch):
    class BusyLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise ConversationLockError("busy")

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(headless_module, "ConversationLock", BusyLock)

    result = headless_module.run_headless(HeadlessOptions(prompt="hi"))

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.out.splitlines()[0])["subtype"] == "init"
    assert "正在被其他进程使用" in captured.err


def test_debug_file_open_failure_is_usage_error(runner_env, capsys):
    options = HeadlessOptions(
        prompt="hi",
        debug_file=Path("/nonexistent-dir-xyz/debug.jsonl"),
    )

    result = headless_module.run_headless(options)

    captured = capsys.readouterr()
    assert result == 2
    assert "无法打开调试日志" in captured.err


def test_unknown_allowed_tool_is_usage_error(runner_env, capsys):
    options = HeadlessOptions(prompt="hi", allowed_tools=("NotARealTool",))

    result = headless_module.run_headless(options)

    captured = capsys.readouterr()
    assert result == 2
    assert "未知的工具" in captured.err


def test_stream_json_mode_emits_protocol_events(runner_env, capsys, monkeypatch):
    async def run_turn(self, prompt):
        handler = self.kwargs["event_handler"]
        handler({"type": "text_delta", "text": "ok"})
        handler(
            {
                "type": "assistant_done",
                "message": {"role": "assistant", "content": "ok"},
                "text": "ok",
                "tool_calls": [],
            }
        )
        return self.result

    monkeypatch.setattr(FakeRuntime, "run_turn", run_turn)

    result = headless_module.run_headless(
        HeadlessOptions(
            prompt="hi",
            output_format="stream-json",
            include_partial_messages=True,
        )
    )

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert result == 0
    assert [payload["type"] for payload in payloads] == [
        "system",
        "assistant_delta",
        "assistant",
        "result",
    ]
    assert payloads[0]["subtype"] == "init"
    assert "FileRead" in payloads[0]["tools"]
    assert payloads[0]["mode"] == "one-shot"
    assert payloads[0]["output_format"] == "stream-json"
    assert payloads[-1]["subtype"] == "success"


def test_fork_session_activates_new_session_for_stream_init(runner_env, capsys):
    source = ConversationStore(runner_env / "conversations")
    source.save_messages([{"role": "user", "content": "previous"}])
    source_id = source.active_id

    result = headless_module.run_headless(
        HeadlessOptions(
            prompt="hi",
            resume=source_id,
            fork_session=True,
            output_format="stream-json",
        )
    )

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    forked = FakeRuntime.instances[0].restored.conversation_id
    assert result == 0
    assert forked != source_id
    assert payloads[0]["type"] == "system"
    assert payloads[0]["session_id"] == forked


def test_invalid_resume_id_is_usage_error(runner_env):
    runner = headless_module.HeadlessRunner(HeadlessOptions(prompt="hi", resume="not-an-id"))
    store = ConversationStore(runner_env / "conversations")

    with pytest.raises(HeadlessUsageError):
        runner._resolve_session(store)


def test_resume_loads_snapshot(runner_env):
    store = ConversationStore(runner_env / "conversations")
    store.save_messages(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "previous"},
        ]
    )
    conversation_id = store.active_id
    FakeRuntime.instances = []

    result = headless_module.run_headless(
        HeadlessOptions(prompt="hi", resume=conversation_id)
    )

    assert result == 0
    restored = FakeRuntime.instances[0].restored
    assert restored is not None
    assert restored.conversation_id == conversation_id
    assert restored.messages[-1]["content"] == "previous"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX termios only")
def test_prepare_terminal_input_restores_cooked_enter(monkeypatch):
    import os
    import pty
    import termios

    class TtyStream:
        def __init__(self, fd):
            self._fd = fd

        def isatty(self):
            return True

        def fileno(self):
            return self._fd

    master_fd, slave_fd = pty.openpty()
    try:
        attributes = termios.tcgetattr(slave_fd)
        attributes[0] &= ~termios.ICRNL
        attributes[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
        monkeypatch.setattr(headless_module.sys, "stdin", TtyStream(slave_fd))

        runner = headless_module.HeadlessRunner(HeadlessOptions(prompt=None))
        runner._prepare_terminal_input()

        updated = termios.tcgetattr(slave_fd)
        assert updated[0] & termios.ICRNL
        assert updated[3] & (termios.ICANON | termios.ECHO | termios.ISIG)

        runner._restore_terminal_input()

        restored = termios.tcgetattr(slave_fd)
        assert not restored[0] & termios.ICRNL
        assert not restored[3] & (termios.ICANON | termios.ECHO | termios.ISIG)
    finally:
        os.close(master_fd)
        os.close(slave_fd)
