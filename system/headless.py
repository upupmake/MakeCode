"""Headless runner for one-shot external command invocation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from rich.console import Console

from init import set_workdir
from system.agent_runtime import AgentRuntime, TurnResult
from system.approvals import APPROVALS, PolicyApprovalProvider
from system.ask_user_backend import ASK_USER
from system.cli import HeadlessOptions
from system.output_bus import OUTPUT_BUS
from system.output_sinks import NullSink, StreamJsonWriter, build_init_payload
from system.stream_cancel import (
    cancel_current_response,
    start_cancel_listener,
    stop_cancel_listener,
)
from system.tool_registry import ToolRegistry
from system.tui_proxies import set_headless_mode
from utils import hitl
from utils import memory as memory_module
from utils import tasks as tasks_module
from utils import teams as teams_module
from utils.conversations import (
    CONVERSATION_FILE,
    CONVERSATION_STORE,
    ConversationLock,
    ConversationLockError,
    ConversationSnapshot,
    ConversationStore,
)
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.skills import SKILL_LOADER
from version import CURRENT_VERSION


class HeadlessError(RuntimeError):
    exit_code = 1


class HeadlessUsageError(HeadlessError):
    exit_code = 2


class HeadlessConfigError(HeadlessError):
    exit_code = 3


class _McpTerminationLogFilter(logging.Filter):
    """Drop the harmless 404 that streamable-http servers return on shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Session termination failed" not in record.getMessage()


class HeadlessRunner:
    MCP_READY_TIMEOUT = 30.0

    def __init__(self, options: HeadlessOptions) -> None:
        self.options = options
        self._debug_stream: TextIO | None = None
        self._debug_stack = contextlib.ExitStack()
        self._stream_writer: StreamJsonWriter | None = None
        self._workdir = Path.cwd()
        self._signal_exit_code: int | None = None
        self._previous_signal_handlers: dict[int, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._previous_sink: Any = None
        self._previous_provider: Any = None
        self._previous_hitl = True
        self._previous_ask_backend: Any = None
        self._mcp_log_filter: _McpTerminationLogFilter | None = None
        self._tty_attributes: tuple[int, list[Any]] | None = None
        self._ask_channel: Any = None
        self._ask_user_enabled = False

    def run(self) -> int:
        try:
            return asyncio.run(self._run_async())
        except HeadlessError as exc:
            print(f"MakeCode: {exc}", file=sys.stderr)
            return exc.exit_code
        except KeyboardInterrupt:
            return self._signal_exit_code or 130
        except Exception as exc:
            print(f"MakeCode: {exc}", file=sys.stderr)
            return 1

    async def _run_async(self) -> int:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(encoding="utf-8", errors="replace")
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._enter_headless_mode()
        self._install_signal_handlers()
        self._prepare_terminal_input()
        runtime: AgentRuntime | None = None
        try:
            self._prepare_environment()
            self._prepare_model()
            self._prepare_debug_stream()
            self._ask_user_enabled = self._resolve_ask_user_enabled()
            self._start_mcp()
            store = ConversationStore(persist=not self.options.no_session_persistence)
            snapshot = self._resolve_session(store)
            registry = ToolRegistry(
                allowed_tools=self.options.allowed_tools,
                disallowed_tools=self.options.disallowed_tools,
                include_ask_user=self._ask_user_enabled,
                include_delegate_tasks=False,
            )
            mcp_tools = GLOBAL_MCP_MANAGER.get_tools()
            unknown_tools = registry.unmatched_allowed_selectors(mcp_tools)
            if unknown_tools:
                raise HeadlessUsageError(
                    f"未知的工具: {', '.join(unknown_tools)}"
                )
            if self.options.output_format == "stream-json":
                self._stream_writer = StreamJsonWriter(
                    session_id=store.active_id or "",
                    include_partial=self.options.include_partial_messages,
                )
            if self._ask_user_enabled:
                from system.headless_ask import AskUserChannel

                self._ask_channel = AskUserChannel(
                    answer_format=(
                        "stream-json"
                        if self.options.input_format == "stream-json"
                        else "text"
                    ),
                    writer=self._stream_writer,
                    direct_stdin=self.options.prompt is not None,
                )
                ASK_USER.set_backend(self._ask_channel)
            tool_names = self._tool_names(registry, mcp_tools)
            init_payload = build_init_payload(
                session_id=store.active_id or "",
                model=self._current_model_name(),
                cwd=str(self._workdir),
                permission_mode=self.options.permission_mode,
                tools=tool_names,
                extra=self._startup_fields(
                    snapshot=snapshot,
                    tool_names=tool_names,
                    mcp_tools=mcp_tools,
                ),
            )
            if self._stream_writer is not None:
                self._stream_writer.write(init_payload)
            else:
                self._write_startup_payload(init_payload)
            runtime = AgentRuntime(
                store=store,
                registry=registry,
                event_handler=self._handle_event,
                append_system_prompt=self.options.append_system_prompt,
                plan_mode=self.options.permission_mode == "plan",
                headless=True,
                ask_user_available=self._ask_user_enabled,
            )
            if snapshot is not None:
                runtime.restore(snapshot)
            if store.persist and store.active_root is not None:
                try:
                    with ConversationLock(store.active_root):
                        return await self._run_turns(runtime, store)
                except ConversationLockError as exc:
                    raise HeadlessConfigError(
                        f"会话正在被其他进程使用: {store.active_root}"
                    ) from exc
            return await self._run_turns(runtime, store)
        finally:
            if runtime is not None:
                runtime.close()
            self._stop_mcp()
            self._close_debug_stream()
            self._restore_signal_handlers()
            self._restore_terminal_input()
            self._leave_headless_mode()

    def _prepare_terminal_input(self) -> None:
        """Make interactive stdin line-oriented before the input loop starts.

        A program that crashed while owning the terminal can leave ICRNL off,
        so Enter arrives as CR, readline() never sees a line terminator and
        the terminal echoes the key as ``^M``. Restore the flags the
        line-based input loops rely on and remember the previous state.
        """
        if not self._uses_stdin():
            return
        if sys.stdin is None:
            return
        try:
            if not sys.stdin.isatty():
                return
            import termios

            fd = sys.stdin.fileno()
            previous = termios.tcgetattr(fd)
        except (ImportError, OSError, ValueError):
            return
        updated = list(previous)
        updated[0] |= termios.ICRNL
        updated[3] |= termios.ICANON | termios.ECHO | termios.ISIG
        if updated == previous:
            return
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, updated)
        except (OSError, ValueError):
            return
        self._tty_attributes = (fd, previous)

    def _restore_terminal_input(self) -> None:
        saved = self._tty_attributes
        if saved is None:
            return
        self._tty_attributes = None
        fd, previous = saved
        try:
            import termios

            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        except (ImportError, OSError, ValueError):
            pass

    def _uses_stdin(self) -> bool:
        return self.options.input_format == "stream-json" or self.options.prompt is None

    def _resolve_ask_user_enabled(self) -> bool:
        if self.options.input_format == "stream-json":
            return True
        if self.options.prompt == "":
            # The one-shot prompt itself is read from stdin to EOF, so no
            # answer channel remains afterwards.
            return False
        return sys.stdin is not None and sys.stdin.isatty()

    def _startup_fields(
        self,
        *,
        snapshot: ConversationSnapshot | None,
        tool_names: list[str],
        mcp_tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from system.models import get_current_model_config
        from utils import paths
        from utils.skill_catalog import skill_directories

        model_config = get_current_model_config()
        config_paths = {
            "shared_config_dir": str(paths.install_makecode_dir(create=False).resolve()),
            "workspace_config_dir": str(
                paths.workspace_makecode_dir(create=False).resolve()
            ),
            "skills_dirs": [
                str(directory.resolve())
                for directory in skill_directories(create=False)
            ],
            "mcp_config": str(paths.mcp_config_file(create=False).resolve()),
        }
        return {
            "version": CURRENT_VERSION,
            "mode": "persistent" if self._uses_stdin() else "one-shot",
            "input_format": self.options.input_format,
            "output_format": self.options.output_format,
            "model_alias": (model_config.alias if model_config else "") or None,
            "model_format": model_config.message_format if model_config else None,
            "model_effort": model_config.reasoning_effort if model_config else None,
            "model_host": self._model_host(model_config),
            "platform": sys.platform,
            "arch": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool_count": len(tool_names),
            "allowed_tools": list(self.options.allowed_tools),
            "disallowed_tools": list(self.options.disallowed_tools),
            "mcp_servers": sorted(GLOBAL_MCP_MANAGER.server_configs),
            "mcp_tool_count": len(mcp_tools),
            "skills": {
                "enabled": SKILL_LOADER.is_enabled,
                "active": len(SKILL_LOADER.skills),
                "total": len(SKILL_LOADER.all_skills),
            },
            "config_paths": config_paths,
            "capabilities": {
                "configurable_paths": True,
                "skills_dirs_override": True,
            },
            "session_persisted": not self.options.no_session_persistence,
            "resumed": snapshot is not None,
            "include_partial_messages": self.options.include_partial_messages,
            "replay_user_messages": self.options.replay_user_messages,
            "ask_user": "enabled" if self._ask_user_enabled else "disabled",
            "ask_user_channel": (
                "stream-json"
                if self.options.input_format == "stream-json"
                else "text"
            )
            if self._ask_user_enabled
            else None,
        }

    @staticmethod
    def _model_host(model_config: Any) -> str | None:
        if model_config is None:
            return None
        base_url = model_config.base_url
        try:
            parsed = urlsplit(
                base_url if "://" in base_url else f"https://{base_url}"
            )
        except ValueError:
            return None
        return parsed.netloc.rsplit("@", 1)[-1] or None

    async def _run_turns(self, runtime: AgentRuntime, store: ConversationStore) -> int:
        if self.options.input_format == "stream-json":
            from system.headless_input import StreamJsonInputLoop

            exit_code = await StreamJsonInputLoop(
                runtime,
                self._stream_writer,
                replay_user_messages=self.options.replay_user_messages,
                stop_event=self._stop_event,
                write_result=(
                    None if self._stream_writer is not None else self._write_result
                ),
                ask_channel=self._ask_channel,
            ).run()
            return self._signal_exit_code or exit_code
        if self.options.prompt is None:
            from system.headless_input import TextInputLoop

            exit_code = await TextInputLoop(
                runtime,
                self._write_result,
                stop_event=self._stop_event,
                ask_channel=self._ask_channel,
            ).run()
            return self._signal_exit_code or exit_code
        if self._stop_event is not None and self._stop_event.is_set():
            exit_code = self._write_result(
                TurnResult(
                    text="",
                    session_id=store.active_id or "",
                    messages=[],
                    cancelled=True,
                )
            )
            return self._signal_exit_code or exit_code
        prompt = self._read_prompt()
        start_cancel_listener()
        try:
            if self._stop_event is not None and self._stop_event.is_set():
                result = TurnResult(
                    text="",
                    session_id=store.active_id or "",
                    messages=[],
                    cancelled=True,
                )
            else:
                result = await runtime.run_turn(prompt)
        finally:
            stop_cancel_listener()
        exit_code = self._write_result(result)
        return self._signal_exit_code or exit_code

    def _install_signal_handlers(self) -> None:
        def handle(signum, frame) -> None:
            self._signal_exit_code = 128 + int(signum)
            cancel_current_response()
            if self._loop is not None and self._stop_event is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._stop_event.set)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                self._previous_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handle)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_signal_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
        self._previous_signal_handlers.clear()

    def _enter_headless_mode(self) -> None:
        self._previous_sink = OUTPUT_BUS.get_sink()
        self._previous_provider = APPROVALS.get_provider()
        self._previous_hitl = hitl.get_hitl_status()
        self._previous_ask_backend = ASK_USER.get_backend()
        self._mcp_log_filter = _McpTerminationLogFilter()
        logging.getLogger("mcp.client.streamable_http").addFilter(
            self._mcp_log_filter
        )
        OUTPUT_BUS.set_sink(NullSink())
        set_headless_mode(True)
        hitl.toggle_hitl(
            enabled=self.options.permission_mode != "bypassPermissions"
        )
        APPROVALS.set_provider(
            PolicyApprovalProvider(
                self.options.permission_mode,
                self.options.allowed_tools,
                self.options.disallowed_tools,
            )
        )

    def _leave_headless_mode(self) -> None:
        if self._ask_channel is not None:
            self._ask_channel.close()
            self._ask_channel = None
        ASK_USER.set_backend(None)
        if self._mcp_log_filter is not None:
            logging.getLogger("mcp.client.streamable_http").removeFilter(
                self._mcp_log_filter
            )
            self._mcp_log_filter = None
        APPROVALS.set_provider(self._previous_provider)
        hitl.toggle_hitl(enabled=self._previous_hitl)
        ASK_USER.set_backend(self._previous_ask_backend)
        set_headless_mode(False)
        OUTPUT_BUS.set_sink(self._previous_sink)

    def _prepare_environment(self) -> None:
        workdir = (self.options.workdir or Path.cwd()).expanduser().resolve()
        if not workdir.is_dir():
            raise HeadlessUsageError(f"工作目录不存在: {workdir}")
        self._workdir = workdir
        set_workdir(workdir)
        memory_module.refresh_workspace_paths()
        CONVERSATION_STORE.refresh_workspace()
        tasks_module.refresh_workspace_paths()
        teams_module.refresh_workspace_paths()
        SKILL_LOADER.refresh_workspace()

    @staticmethod
    def _current_model_name() -> str | None:
        from system.models import get_current_model_config

        config = get_current_model_config()
        return getattr(config, "model_id", None)

    def _tool_names(
        self,
        registry: ToolRegistry,
        mcp_tools: list[dict[str, Any]],
    ) -> list[str]:
        return registry.tool_names(
            mcp_tools,
            plan_mode=self.options.permission_mode == "plan",
        )

    def _prepare_model(self) -> None:
        from system.models import get_model_manager

        manager = get_model_manager()
        if manager is None:
            raise HeadlessConfigError("模型管理器未初始化。")
        model = manager.get_current_model()
        if self.options.model:
            needle = self.options.model.casefold()
            model = next(
                (
                    candidate
                    for candidate in manager.models
                    if candidate.model_id.casefold() == needle
                    or candidate.alias.casefold() == needle
                ),
                None,
            )
            if model is None:
                raise HeadlessConfigError(f"未找到模型: {self.options.model}")
            manager.current_model = model
            manager.current_model_key = model.key
        if model is None:
            raise HeadlessConfigError("未配置模型。")
        if self.options.effort:
            model.reasoning_effort = self.options.effort

    def _prepare_debug_stream(self) -> None:
        if self.options.debug_file is not None:
            try:
                self._debug_stream = self._debug_stack.enter_context(
                    Path(self.options.debug_file).open("a", encoding="utf-8")  # noqa: SIM115
                )
            except OSError as exc:
                raise HeadlessUsageError(
                    f"无法打开调试日志 {self.options.debug_file}: {exc}"
                ) from exc
        elif self.options.debug is not None:
            self._debug_stream = sys.stderr

    def _close_debug_stream(self) -> None:
        self._debug_stack.close()
        self._debug_stream = None

    def _read_prompt(self) -> str:
        if self.options.prompt is None:
            raise HeadlessUsageError("headless 文本模式需要 -p/--print 提供 prompt。")
        prompt = self.options.prompt
        if prompt == "":
            if sys.stdin.isatty():
                raise HeadlessUsageError("未提供 prompt，且 stdin 是交互终端。")
            prompt = sys.stdin.read()
        prompt = prompt.strip()
        if not prompt:
            raise HeadlessUsageError("prompt 为空。")
        return prompt

    @staticmethod
    def _normalize_session_id(store: ConversationStore, value: str) -> str:
        try:
            return store.normalize_conversation_id(value)
        except ValueError as exc:
            raise HeadlessUsageError(f"无效的会话 ID: {value}") from exc

    def _resolve_session(self, store: ConversationStore) -> ConversationSnapshot | None:
        if self.options.resume:
            conversation_id = self._normalize_session_id(store, self.options.resume)
            path = store.root / conversation_id / CONVERSATION_FILE
            if not path.exists():
                if path.parent.exists():
                    raise HeadlessConfigError(
                        f"会话尚未完成初始化，可能正被其他进程使用: {conversation_id}"
                    )
                raise HeadlessConfigError(f"会话不存在: {conversation_id}")
            try:
                snapshot = store.load(path)
            except (OSError, ValueError) as exc:
                raise HeadlessConfigError(
                    f"无法读取会话 {conversation_id}: {exc}"
                ) from exc
            if self.options.fork_session:
                forked = store.fork(snapshot)
                store.activate(forked)
                return forked
            store.activate(snapshot)
            return snapshot
        if self.options.continue_latest:
            try:
                latest = store.find_latest()
            except (OSError, ValueError) as exc:
                raise HeadlessConfigError(f"无法读取最近会话: {exc}") from exc
            if latest is None:
                store.create()
                return None
            if self.options.fork_session:
                forked = store.fork(latest)
                store.activate(forked)
                return forked
            store.activate(latest)
            return latest
        if self.options.session_id:
            conversation_id = self._normalize_session_id(store, self.options.session_id)
            path = store.root / conversation_id / CONVERSATION_FILE
            if path.exists():
                try:
                    snapshot = store.load(path)
                except (OSError, ValueError) as exc:
                    raise HeadlessConfigError(
                        f"无法读取会话 {conversation_id}: {exc}"
                    ) from exc
                store.activate(snapshot)
                return snapshot
            store.create(conversation_id)
            return None
        store.create()
        return None

    def _start_mcp(self) -> None:
        console = Console(
            file=sys.stderr,
            force_terminal=False,
            color_system=None,
            width=120,
        )
        GLOBAL_MCP_MANAGER.initialize(console=console)
        GLOBAL_MCP_MANAGER.start_background()
        ready = False
        deadline = time.monotonic() + self.MCP_READY_TIMEOUT
        while time.monotonic() < deadline:
            if GLOBAL_MCP_MANAGER.wait_until_ready(timeout=0.2):
                ready = True
                break
            if self._stop_event is not None and self._stop_event.is_set():
                break
        if not ready:
            if self._stop_event is not None and self._stop_event.is_set():
                return
            raise HeadlessConfigError(
                f"MCP 初始化在 {self.MCP_READY_TIMEOUT:g} 秒内未完成，已中止请求。"
            )

    def _stop_mcp(self) -> None:
        with contextlib.suppress(Exception):
            GLOBAL_MCP_MANAGER.stop()

    def _handle_event(self, event: dict[str, Any]) -> None:
        if self._stream_writer is not None:
            self._stream_writer.handle_runtime_event(event)
        if self._debug_stream is None:
            return
        self._debug_stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._debug_stream.flush()

    def _write_startup_payload(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _write_result(self, result: TurnResult) -> int:
        if self._stream_writer is not None:
            self._stream_writer.write_result(result)
        elif self.options.output_format == "json":
            payload = {
                "type": "result",
                "subtype": (
                    "cancelled"
                    if result.cancelled
                    else "error"
                    if result.is_error
                    else "success"
                ),
                "is_error": result.is_error,
                "result": result.text,
                "session_id": result.session_id,
                "usage": result.usage,
                "compactions": result.compactions,
                "permission_denials": result.permission_denials,
                "duration_ms": result.duration_ms,
                "error": result.error,
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        else:
            if result.text:
                sys.stdout.write(result.text)
                if not result.text.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
            if result.error:
                print(result.error, file=sys.stderr)
        if result.cancelled:
            return 130
        return 0 if not result.is_error else 1


def run_headless(options: HeadlessOptions) -> int:
    return HeadlessRunner(options).run()
