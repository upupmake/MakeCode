import time
from typing import Iterator, Tuple, List, Any

from rich.markdown import Markdown
from rich.text import Text

from system.stream_cancel import is_cancelled
from system.tui_app import TuiRegion, post_tui


class StreamRenderer:
    """
    负责处理 LLM 流式输出的终端渲染器。
    思考过程与正文实时更新尾部，完整 Markdown 段落固定到日志。
    """

    def __init__(self, console=None, update_interval: float = 0.05):
        self.console = console
        self.update_interval = update_interval
        self.tail_update_interval = 0.5
        self._last_tail_update_at: dict[TuiRegion, float] = {}

    def render_text_stream(
            self,
            stream_generator: Iterator[dict],
            region: TuiRegion = TuiRegion.CONTENT,
            render_live: bool = True,
            set_active: bool = False,
    ) -> Tuple[str, List, Any]:
        text_content = ""
        emitted_text = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        if set_active:
            self._set_active(region, True)
        try:
            for event in stream_generator:
                if is_cancelled():
                    break
                event_type = event.get("type")

                if event_type == "text":
                    chunk = event["content"]
                    text_content += chunk
                    if render_live:
                        live_buffer += chunk
                        live_buffer, emitted_chunk = self._process_block_commit(text_content, live_buffer, region=region)
                        emitted_text += emitted_chunk
                        self._update_tail(live_buffer, region=region, force=bool(emitted_chunk))

                elif event_type == "done":
                    text_content_done, tool_calls, raw_message = event["content"]
                    if text_content_done:
                        text_content = text_content_done
                        if render_live and text_content.startswith(emitted_text):
                            live_buffer = text_content[len(emitted_text):]
                    break

        finally:
            if render_live:
                if live_buffer and not is_cancelled():
                    self._safe_cleanup(live_buffer, region=region)
                self._clear_tail(region)
            if set_active:
                self._set_active(region, False)

        if is_cancelled():
            return "", [], None

        return text_content, tool_calls, raw_message

    def render(self, stream_generator: Iterator[dict], agent_name: str = "Orchestrator") -> Tuple[str, List, Any]:
        self._print_header(agent_name)
        start_time = time.perf_counter()

        text_content = ""
        emitted_text = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        reasoning_started = False
        text_started = False

        reasoning_content = ""
        reasoning_buffer = ""
        tool_calls_started = False

        post_tui(TuiRegion.STATUS, f"Awakening {agent_name}...")

        try:
            for event in stream_generator:
                if is_cancelled():
                    break
                event_type = event.get("type")

                if event_type == "reasoning":
                    post_tui(TuiRegion.STATUS, f"{agent_name} reasoning")
                    reasoning_content, reasoning_buffer, reasoning_started = self._handle_reasoning(
                        event["content"], reasoning_content, reasoning_buffer, reasoning_started
                    )

                elif event_type == "tool_calls":
                    if not tool_calls_started:
                        self._handle_tool_calls_generation(agent_name)
                        tool_calls_started = True

                elif event_type == "text":
                    chunk = event["content"]
                    text_content += chunk
                    live_buffer += chunk

                    if not text_started and text_content.strip():
                        if reasoning_buffer:
                            self._safe_cleanup(reasoning_buffer, region=TuiRegion.REASONING)
                            reasoning_buffer = ""
                            self._clear_tail(TuiRegion.REASONING)
                        self._start_text_section(agent_name, reasoning_started)
                        text_started = True

                    if text_started:
                        live_buffer, emitted_chunk = self._process_block_commit(text_content, live_buffer)
                        emitted_text += emitted_chunk
                        self._update_tail(live_buffer, force=bool(emitted_chunk))

                elif event_type == "done":
                    text_content_done, tool_calls, raw_message = event["content"]
                    if text_content_done:
                        text_content = text_content_done
                        if text_started and text_content.startswith(emitted_text):
                            live_buffer = text_content[len(emitted_text):]
                    if tool_calls:
                        if not tool_calls_started:
                            self._handle_tool_calls_generation(agent_name)
                            tool_calls_started = True
                        self._handle_tool_calls_generated(agent_name)
                    break

        finally:
            self._safe_cleanup(reasoning_buffer, region=TuiRegion.REASONING)
            self._clear_tail(TuiRegion.REASONING)
            if live_buffer and text_started and not is_cancelled():
                self._safe_cleanup(live_buffer)
            self._clear_tail(TuiRegion.CONTENT)
            if reasoning_started and not text_started:
                self._set_active(TuiRegion.REASONING, False)
            if text_started:
                self._set_active(TuiRegion.CONTENT, False)

        if is_cancelled():
            if tool_calls_started:
                self._set_active(TuiRegion.BACKGROUND, False)
            self._print_cancelled(agent_name, start_time)
            return "", [], None

        self._handle_fallback(agent_name, text_content, reasoning_started, text_started)
        self._print_footer(agent_name, start_time)

        return text_content, tool_calls, raw_message
    # ==================== 私有辅助方法 ====================

    def _print_header(self, name: str):
        post_tui(TuiRegion.STATUS, f"{name} started")

    def _print_footer(self, name: str, start_time: float):
        elapsed = time.perf_counter() - start_time
        post_tui(TuiRegion.BACKGROUND, Text(f"✓ {name} completed in {elapsed:.2f}s", style="#aaaaaa"))
        post_tui(TuiRegion.STATUS, f"{name} completed in {elapsed:.2f}s")

    def _print_cancelled(self, name: str, start_time: float):
        elapsed = time.perf_counter() - start_time
        post_tui(TuiRegion.BACKGROUND, Text(f"⚠ {name} cancelled in {elapsed:.2f}s", style="#f59e0b"))
        post_tui(TuiRegion.STATUS, f"{name} cancelled")

    def _handle_reasoning(self, content: str, reasoning_content: str, reasoning_buffer: str, is_started: bool):
        if not is_started:
            self._set_active(TuiRegion.REASONING, True)
            post_tui(TuiRegion.REASONING, "\n[bold cyan]🧠 Reasoning...[/bold cyan]")

        reasoning_buffer += content
        reasoning_buffer, emitted_chunk = self._process_block_commit(reasoning_content, reasoning_buffer, region=TuiRegion.REASONING)
        self._update_tail(reasoning_buffer, region=TuiRegion.REASONING, force=bool(emitted_chunk))

        return reasoning_content, reasoning_buffer, True

    def _handle_tool_calls_generation(self, name: str):
        post_tui(TuiRegion.BACKGROUND, active=True)
        post_tui(TuiRegion.BACKGROUND, f"[#aaaaaa]🛠️ {name} 正在生成 tool_calls...[/#aaaaaa]")

    def _handle_tool_calls_generated(self, name: str):
        post_tui(TuiRegion.BACKGROUND, f"[bold green]🛠️ {name} tool_calls 生成完成[/bold green]")
        post_tui(TuiRegion.BACKGROUND, active=False)

    def _start_text_section(self, name: str, reasoning_started: bool):
        if reasoning_started:
            self._set_active(TuiRegion.REASONING, False)
        self._set_active(TuiRegion.CONTENT, True)
        if reasoning_started:
            post_tui(TuiRegion.CONTENT, "")
        post_tui(TuiRegion.CONTENT, f"\n[bold cyan]📝 {name} Content...[/bold cyan]")

    def _process_block_commit(self, full_text: str, current_buffer: str, region: TuiRegion = TuiRegion.CONTENT) -> tuple[str, str]:
        """
        增量渲染逻辑：
        如果段落结束，将完整段落输出为 Markdown，并保留未完成的尾部 buffer。
        返回值为 (剩余 buffer, 本次已输出的原始文本片段)。
        """
        in_code_block = full_text.count("```") % 2 != 0

        if not in_code_block and "\n\n" in current_buffer:
            parts = current_buffer.rsplit("\n\n", 1)
            if len(parts) == 2:
                complete_blocks, remaining_buffer = parts
                self._clear_tail(region)
                post_tui(region, Markdown(complete_blocks))
                return remaining_buffer, f"{complete_blocks}\n\n"

        return current_buffer, ""

    def _update_tail(self, buffer: str, region: TuiRegion = TuiRegion.CONTENT, force: bool = False):
        if not force:
            now = time.perf_counter()
            last_update = self._last_tail_update_at.get(region)
            if last_update is not None and now - last_update < self.tail_update_interval:
                return
            self._last_tail_update_at[region] = now
        post_tui(region, Text(buffer) if buffer.strip() else "", tail=True)

    def _clear_tail(self, region: TuiRegion = TuiRegion.CONTENT):
        self._last_tail_update_at.pop(region, None)
        post_tui(region, "", tail=True)

    def _set_active(self, region: TuiRegion, active: bool):
        post_tui(region, active=active)

    def _safe_cleanup(self, buffer: str, region: TuiRegion = TuiRegion.CONTENT):
        """流结束时输出剩余内容。"""
        if buffer.strip():
            post_tui(region, Markdown(buffer))

    def _handle_fallback(self, name: str, content: str, reasoning_started: bool, text_started: bool):
        if not text_started and content.strip():
            self._start_text_section(name, reasoning_started)
            post_tui(TuiRegion.CONTENT, Markdown(content))
            self._set_active(TuiRegion.CONTENT, False)
        elif not reasoning_started and not text_started:
            post_tui(TuiRegion.CONTENT, "")

