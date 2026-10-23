"""Headless runner for one-shot external command invocation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

from init import set_workdir
from system.agent_runtime import AgentRuntime, TurnResult
from system.approvals import APPROVALS, PolicyApprovalProvider
from system.cli import HeadlessOptions
from system.output_bus import OUTPUT_BUS
from system.output_sinks import NullSink, StreamJsonWriter
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
    ConversationSnapshot,
    ConversationStore,
)
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.skills import SKILL_LOADER


class HeadlessError(RuntimeError):
    exit_code = 1


class HeadlessUsageError(HeadlessError):
    exit_code = 2


class HeadlessConfigError(HeadlessError):
    exit_code = 3


class HeadlessRunner:
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
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(encoding="utf-8", errors="replace")
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._enter_headless_mode()
        self._install_signal_handlers()
        try:
            self._prepare_environment()
            self._prepare_model()
            self._prepare_debug_stream()
            self._start_mcp()
            store = ConversationStore(persist=not self.options.no_session_persistence)
            snapshot = self._resolve_session(store)
            if self.options.output_format == "stream-json":
                self._stream_writer = StreamJsonWriter(
                    session_id=store.active_id or "",
                    include_partial=self.options.include_partial_messages,
                )
                self._stream_writer.init(
                    model=self._current_model_name(),
                    cwd=str(self._workdir),
                    permission_mode=self.options.permission_mode,
                )
            runtime = AgentRuntime(
                store=store,
                registry=ToolRegistry(
                    allowed_tools=self.options.allowed_tools,
                    disallowed_tools=self.options.disallowed_tools,
                    include_ask_user=False,
                ),
                event_handler=self._handle_event,
                append_system_prompt=self.options.append_system_prompt,
                plan_mode=self.options.permission_mode == "plan",
            )
            if snapshot is not None:
                runtime.restore(snapshot)
            if store.persist and store.active_root is not None:
                with ConversationLock(store.active_root):
                    return await self._run_turns(runtime, store)
            return await self._run_turns(runtime, store)
        finally:
            self._stop_mcp()
            self._close_debug_stream()
            self._restore_signal_handlers()
            self._leave_headless_mode()

    async def _run_turns(self, runtime: AgentRuntime, store: ConversationStore) -> int:
        if self.options.input_format == "stream-json":
            if self._stream_writer is None:
                raise HeadlessUsageError(
                    "stream-json 输入必须配合 stream-json 输出。"
                )
            from system.headless_input import StreamJsonInputLoop

            exit_code = await StreamJsonInputLoop(
                runtime,
                self._stream_writer,
                replay_user_messages=self.options.replay_user_messages,
                stop_event=self._stop_event,
            ).run()
            return self._signal_exit_code or exit_code
        prompt = self._read_prompt()
        start_cancel_listener()
        try:
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
        OUTPUT_BUS.set_sink(NullSink())
        set_headless_mode(True)
        hitl.toggle_hitl(enabled=True)
        APPROVALS.set_provider(
            PolicyApprovalProvider(
                self.options.permission_mode,
                self.options.allowed_tools,
                self.options.disallowed_tools,
            )
        )

    def _leave_headless_mode(self) -> None:
        APPROVALS.set_provider(self._previous_provider)
        hitl.toggle_hitl(enabled=self._previous_hitl)
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
            self._debug_stream = self._debug_stack.enter_context(
                Path(self.options.debug_file).open("a", encoding="utf-8")  # noqa: SIM115
            )
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

    def _resolve_session(self, store: ConversationStore) -> ConversationSnapshot | None:
        if self.options.resume:
            conversation_id = store.normalize_conversation_id(self.options.resume)
            path = store.root / conversation_id / CONVERSATION_FILE
            if not path.exists():
                raise HeadlessConfigError(f"会话不存在: {conversation_id}")
            snapshot = store.load(path)
            if self.options.fork_session:
                return store.fork(snapshot)
            store.activate(snapshot)
            return snapshot
        if self.options.continue_latest:
            latest = store.find_latest()
            if latest is None:
                store.create()
                return None
            if self.options.fork_session:
                return store.fork(latest)
            store.activate(latest)
            return latest
        if self.options.session_id:
            conversation_id = store.normalize_conversation_id(self.options.session_id)
            path = store.root / conversation_id / CONVERSATION_FILE
            if path.exists():
                snapshot = store.load(path)
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
