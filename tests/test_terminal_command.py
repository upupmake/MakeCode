from unittest.mock import patch

import pytest

from utils import common


class FakeProcess:
    def __init__(self, return_code=0, stdout=b"", stderr=b""):
        self.pid = 1234
        self.returncode = return_code
        self.stdout = stdout
        self.stderr = stderr

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class RunningFakeProcess(FakeProcess):
    def __init__(self, stdout=b"", stderr=b""):
        super().__init__(None, stdout, stderr)


def _run_with_process(process, command="pytest -q"):
    with patch.object(common, "_resolve_startup_terminal_type", return_value="pwsh"), \
            patch.object(common, "_workdir"), \
            patch.object(common, "check_permission", return_value=(True, "")), \
            patch.object(common.subprocess, "Popen", return_value=process), \
            patch("system.stream_cancel.start_terminal_command"), \
            patch("system.stream_cancel.stop_terminal_command"), \
            patch("system.stream_cancel.is_terminal_cancelled", return_value=False):
        return common.run_terminal_command(command)


def test_terminal_result_reports_success_and_exit_code():
    result = _run_with_process(FakeProcess(0, stdout=b"tests passed"))

    assert result.startswith("Status: success\nExit code: 0")
    assert result.endswith("tests passed")


def test_terminal_process_does_not_inherit_tui_stdin():
    process = FakeProcess(0, stdout=b"ok")

    with patch.object(common, "_resolve_startup_terminal_type", return_value="pwsh"), \
            patch.object(common, "_workdir"), \
            patch.object(common, "check_permission", return_value=(True, "")), \
            patch.object(common.subprocess, "Popen", return_value=process) as popen, \
            patch("system.stream_cancel.start_terminal_command"), \
            patch("system.stream_cancel.stop_terminal_command"), \
            patch("system.stream_cancel.is_terminal_cancelled", return_value=False):
        common.run_terminal_command("pytest -q")

    assert popen.call_args.kwargs["stdin"] is common.subprocess.DEVNULL


def test_terminal_result_reports_nonzero_exit_as_failure_even_without_output():
    result = _run_with_process(FakeProcess(3))

    assert result.startswith("Status: failed\nExit code: 3")
    assert result.endswith("(no output)")


@pytest.mark.parametrize(
    "command",
    [
        "vim file.txt",
        "git status; ssh server",
        "sudo pytest",
        "rm -rf build",
        "rm -fr build",
        "format C:",
        "del /f output.txt",
        "nmap localhost",
        "sqlmap -u https://example.com",
    ],
)
def test_sensitive_terminal_commands_go_through_hitl_and_can_be_denied(command):
    with patch.object(common, "check_permission", return_value=(False, "用户拒绝执行")) as check_permission, \
            patch.object(common.subprocess, "Popen") as popen:
        result = common.run_terminal_command(command)

    assert result == "User Denied Execution. Reason: 用户拒绝执行"
    check_permission.assert_called_once_with("cmd", command.split()[0] if len(command.split()) == 1 else " ".join(command.split()[:2]), command)
    popen.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "format C:",
        "nmap localhost",
    ],
)
def test_sensitive_terminal_commands_can_execute_after_hitl_confirmation(command):
    with patch.object(common, "check_permission", return_value=(True, "")), \
            patch.object(common, "_resolve_startup_terminal_type", return_value="pwsh"), \
            patch.object(common, "_workdir"), \
            patch.object(common.subprocess, "Popen", return_value=FakeProcess(0, stdout=b"allowed")), \
            patch("system.stream_cancel.start_terminal_command"), \
            patch("system.stream_cancel.stop_terminal_command"), \
            patch("system.stream_cancel.is_terminal_cancelled", return_value=False):
        result = common.run_terminal_command(command)

    assert result.startswith("Status: success\nExit code: 0")
    assert result.endswith("allowed")


def test_terminal_policy_has_no_static_hard_block_list():
    assert not hasattr(common, "_TERMINAL_HARD_BLOCKS")
    assert not hasattr(common, "_hard_block_terminal_command")


def test_windows_process_tree_termination_uses_taskkill():
    process = RunningFakeProcess()

    with patch.object(common.os, "name", "nt"), \
            patch.object(common.subprocess, "run") as run:
        common._terminate_process_tree(process)

    run.assert_called_once_with(
        ["taskkill", "/PID", "1234", "/T", "/F"],
        stdout=common.subprocess.DEVNULL,
        stderr=common.subprocess.DEVNULL,
        check=False,
        timeout=5,
    )


def test_posix_process_tree_termination_targets_process_group():
    process = RunningFakeProcess()

    with patch.object(common.os, "name", "posix"), \
            patch.object(common.os, "killpg", create=True) as killpg:
        common._terminate_process_tree(process)

    killpg.assert_called_once_with(1234, common.signal.SIGTERM)


def test_process_tree_termination_skips_already_exited_process():
    process = FakeProcess(0)

    with patch.object(common.subprocess, "run") as run, \
            patch.object(common.os, "killpg", create=True) as killpg:
        common._terminate_process_tree(process)

    run.assert_not_called()
    killpg.assert_not_called()


def test_terminal_cancellation_terminates_process_tree_and_cleans_up_registration():
    process = RunningFakeProcess(stdout=b"partial output")

    with patch.object(common, "_resolve_startup_terminal_type", return_value="pwsh"), \
            patch.object(common, "_workdir"), \
            patch.object(common, "check_permission", return_value=(True, "")), \
            patch.object(common.subprocess, "Popen", return_value=process), \
            patch.object(common, "_terminate_process_tree", side_effect=lambda proc: setattr(proc, "returncode", -1)) as terminate, \
            patch("system.stream_cancel.start_terminal_command") as start_terminal, \
            patch("system.stream_cancel.stop_terminal_command") as stop_terminal, \
            patch("system.stream_cancel.is_terminal_cancelled", return_value=True):
        result = common.run_terminal_command("pytest -q")

    assert result.startswith("Status: cancelled\nExit code: -1")
    assert result.endswith("partial output")
    terminate.assert_called_once_with(process)
    start_terminal.assert_called_once_with()
    stop_terminal.assert_called_once_with()


def test_terminal_timeout_terminates_process_tree_and_reports_timeout():
    process = RunningFakeProcess(stderr=b"still running")

    with patch.object(common, "_resolve_startup_terminal_type", return_value="pwsh"), \
            patch.object(common, "_workdir"), \
            patch.object(common, "check_permission", return_value=(True, "")), \
            patch.object(common.subprocess, "Popen", return_value=process), \
            patch.object(common, "_terminate_process_tree", side_effect=lambda proc: setattr(proc, "returncode", -1)) as terminate, \
            patch.object(common, "log_error_traceback"), \
            patch.object(common.time, "monotonic", side_effect=[0, 121]), \
            patch.object(common.time, "sleep"), \
            patch("system.stream_cancel.start_terminal_command"), \
            patch("system.stream_cancel.stop_terminal_command"), \
            patch("system.stream_cancel.is_terminal_cancelled", return_value=False):
        result = common.run_terminal_command("pytest -q")

    assert result.startswith("Status: timed_out\nExit code: -1")
    assert result.endswith("still running")
    terminate.assert_called_once_with(process)


def test_cancel_current_response_cancels_active_terminal_command():
    from system import stream_cancel

    stream_cancel.reset_cancel()
    try:
        with patch.object(stream_cancel, "post_tui") as post_tui:
            stream_cancel.start_terminal_command()
            assert stream_cancel.cancel_current_response() is True
            assert stream_cancel.is_terminal_cancelled() is True
            post_tui.assert_called_once()
    finally:
        stream_cancel.stop_terminal_command()
        stream_cancel.reset_cancel()


def test_stop_cancel_listener_clears_response_cancel_signal():
    from system import stream_cancel

    stream_cancel.reset_cancel()
    try:
        with patch.object(stream_cancel, "post_tui"):
            stream_cancel.start_cancel_listener()
            assert stream_cancel.cancel_current_response() is True
            assert stream_cancel.is_cancelled() is True
            stream_cancel.stop_cancel_listener()
            assert stream_cancel.is_cancelled() is False
    finally:
        stream_cancel.reset_cancel()
