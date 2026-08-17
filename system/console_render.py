"""
控制台渲染模块：提供所有与控制台输出相关的渲染函数。
"""
import json
import re
import threading
from typing import Any

from markdown_it import MarkdownIt
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme

from init import log_error_traceback, STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE
from system.tool_history import format_tool_arguments, format_tool_value
from system.tui_app import TuiRegion, post_tui
from utils import paths


def render_current_workdir(label: str = "当前工作目录") -> None:
    workdir = escape(str(paths.workdir()))
    post_tui(TuiRegion.CONTENT, f"[bold cyan]📂 {label}: {workdir}[/bold cyan]")


def render_current_task_plan(target_console: Console) -> None:
    from utils.tasks import render_task_pane

    render_task_pane()

# 自定义主题：覆盖默认深色调，h1 金色醒目、h2-4 蓝青色系层次分明
_custom_theme = Theme({
    "markdown.block_quote": "bright_black",
    "markdown.h1": "bold yellow",
    "markdown.h2": "cyan underline",
    "markdown.h3": "deep_sky_blue1 bold",
    "markdown.h4": "steel_blue1 italic",
})

class TuiConsole(Console):
    def print(self, *objects: Any, **kwargs: Any) -> None:
        region = kwargs.pop("tui_region", TuiRegion.CONTENT)
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        if not objects:
            post_tui(region, "")
            return
        post_tui(region, active=True)
        try:
            for obj in objects:
                post_tui(region, obj)
            if end and end != "\n":
                post_tui(region, end.rstrip("\n"))
        finally:
            post_tui(region, active=False)

    def rule(self, title: str = "", **kwargs: Any) -> None:
        style = kwargs.get("style", "")
        text = f"[bold]{title}[/bold]" if title else "─" * 24
        post_tui(TuiRegion.CONTENT, active=True)
        try:
            post_tui(TuiRegion.CONTENT, Text.from_markup(text))
        finally:
            post_tui(TuiRegion.CONTENT, active=False)

    def clear(self) -> None:
        post_tui(TuiRegion.CONTENT, "", clear=True)


console = TuiConsole(force_terminal=True, theme=_custom_theme)

# 线程锁：用于多子智能体并发输出时保护控制台，防止输出交错
console_lock = threading.Lock()

# =============================================================================
# Sub-Agent 输出控制全局变量
# =============================================================================
# 控制 Sub-Agent 是否输出到主控制台
# True  = 正常输出（默认）
# False = 静默模式，Sub-Agent 的输出不会显示在控制台（但仍会写入日志文件）
SHOW_SUB_AGENT_CONSOLE = True


def toggle_sub_agent_console() -> bool:
    """切换 Sub-Agent 控制台输出状态，返回切换后的状态值"""
    global SHOW_SUB_AGENT_CONSOLE
    SHOW_SUB_AGENT_CONSOLE = not SHOW_SUB_AGENT_CONSOLE
    return SHOW_SUB_AGENT_CONSOLE


def get_sub_agent_console():
    """获取当前 Sub-Agent 控制台输出状态"""
    return SHOW_SUB_AGENT_CONSOLE


class CopyablePanel(Panel):
    """携带原始文本的面板，供 TUI 选区复制时提取内容"""

    def __init__(self, *args: Any, copy_text: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.copy_text = copy_text


def _content_panel(body: Any, title: str, border_style: str, copy_text: str = "") -> CopyablePanel:
    return CopyablePanel(
        body,
        title=title,
        title_align="left",
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 1),
        expand=True,
        copy_text=copy_text,
    )


def render_content_user_message(text: str) -> CopyablePanel:
    return _content_panel(
        Text(text, style="white"),
        "[bold #22c55e]You[/bold #22c55e]",
        "#22c55e",
        copy_text=text,
    )


def _parse_model_markdown(text: str):
    return (
        MarkdownIt()
        .disable(["html_block", "html_inline"])
        .enable("strikethrough")
        .enable("table")
        .parse(text)
    )


def render_model_markdown(text: str) -> Markdown:
    markdown = Markdown(text)
    markdown.parsed = _parse_model_markdown(text)
    return markdown


class CopyableMarkdown(Markdown):
    """携带原始文本的 Markdown 渲染对象，供 TUI 双击复制流式正文块"""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.copy_text = text
        self.parsed = _parse_model_markdown(text)


def render_copyable_markdown(text: str) -> CopyableMarkdown:
    return CopyableMarkdown(text)


def render_content_assistant_message(text: str, identity: str = "Assistant") -> CopyablePanel:
    return _content_panel(
        render_model_markdown(text),
        f"[bold #a78bfa]{identity}[/bold #a78bfa]",
        "#a78bfa",
        copy_text=text,
    )


MAKECODE_ASCII = r"""
███╗   ███╗ █████╗ ██╗  ██╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║█████╔╝ █████╗  ██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""


def _stringify_output(output: Any) -> str:
    """将输出转换为字符串，如果是可序列化的对象则格式化为 JSON"""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, indent=2)


_TERMINAL_CONTROL_TRANSLATION = dict.fromkeys(
    code for code in (*range(0x20), *range(0x7F, 0xA0)) if code not in {0x09, 0x0A}
)


def _terminal_output_text(value: Any, style: str = "") -> Text:
    terminal_text = str(value).replace("\r\n", "\n")
    terminal_text = re.sub(
        r"\r(?=(?:\x1b\[[0-?]*[ -/]*[@-~])*\n)",
        "",
        terminal_text,
    ).replace("\r", "\n")
    plain = Text.from_ansi(terminal_text).plain.translate(_TERMINAL_CONTROL_TRANSLATION)
    return Text(plain, style=style)


def _extract_message_text(msg: dict) -> str:
    """从消息字典中提取文本内容"""
    metadata = msg.get("message_metadata")
    display_content = metadata.get("display_content") if isinstance(metadata, dict) else None
    content = display_content if isinstance(display_content, str) else msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = [
        part["text"] for part in content if isinstance(part, dict) and part.get("text")
    ]
    return "\n\n".join(chunks).strip()


def _render_agent_response_message(
        text: str,
        identity: str = "Assistant",
        response_time: float = None,
        tui_region: TuiRegion = TuiRegion.CONTENT,
):
    """渲染 Agent 的消息"""
    if not text:
        return

    console.print(render_content_assistant_message(text, identity), tui_region=tui_region)


def _render_tool_call(
        name: str,
        arguments: Any,
        tui_region: TuiRegion,
        identity: str = "🧠 Orchestrator",
):
    """渲染工具调用"""
    body = Text(format_tool_arguments(arguments), style="white")

    console.print(
        Panel(
            body,
            title=f"[bold cyan]🛠️ Action: {name} <- {identity} [/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=True,
        ),
        tui_region=tui_region,
    )


def _render_tool_output(
        name: str,
        output: Any,
        tui_region: TuiRegion,
        identity: str = "🧠 Orchestrator",
):
    """渲染工具输出"""
    text = _stringify_output(output).strip()
    body = _terminal_output_text(format_tool_value(text), style="white")

    console.print(
        Panel(
            body,
            title=f"[bold green]✅ Result: {name} <- {identity} [/bold green]",
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=True,
        ),
        tui_region=tui_region,
    )


def _render_user_message(text: str):
    """渲染用户消息"""
    if not text:
        return
    console.print(render_content_user_message(text), tui_region=TuiRegion.CONTENT)


def _render_history(messages: list):
    """渲染历史消息"""
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        elif role == "user":
            _render_user_message(_extract_message_text(msg))
        elif role == "assistant":
            reasoning_content = msg.get("reasoning_content") or msg.get("reasoning")
            if reasoning_content:
                post_tui(
                    TuiRegion.REASONING,
                    render_model_markdown(reasoning_content),
                    collapsible_title="🧠 Reasoning",
                )

            content = msg.get("content")
            if content:
                _render_agent_response_message(content)


def format_runtime_info(tokens: int | None = None, threshold: int = 80000) -> str:
    from system.models import get_current_model_config
    from utils.hitl import get_hitl_status
    from utils.plan_mode import is_plan_mode

    mode_text = "📋 Plan" if is_plan_mode() else "🎬 Act"
    current_model = get_current_model_config()
    if current_model:
        domain_parts = current_model.get_display_name().split(".")
        status_domain = ".".join(domain_parts[-3:])
        model_text = (
            f"{current_model.model_id} ({status_domain}) · Effort: {current_model.reasoning_effort}"
        )
    else:
        model_text = "未选择"
    token_text = "N/A" if tokens is None else f"{tokens}/{threshold} ({(tokens / threshold) * 100:.1f}%)"
    hitl_text = "ON" if get_hitl_status() else "OFF"
    return f"{mode_text} | 🤖 Model: {model_text} | 📈 Tokens: {token_text} | 🛡️ HITL: {hitl_text}"


def _render_runtime_info(tokens: int | None = None, threshold: int = 80000) -> None:
    post_tui(TuiRegion.RUNTIME_INFO, format_runtime_info(tokens, threshold))


def _render_token_usage(
        messages: list,
        tools_definition: Any = None,
        threshold: int = 80000,
        estimate_tokens_fn: callable = None,
):
    """
    渲染 token 使用情况。
    注意：此函数需要外部提供 estimate_tokens 函数，以避免循环导入。
    如果未提供 estimate_tokens_fn，则无法显示 token 使用情况。
    """
    if estimate_tokens_fn is None:
        # 如果没有提供估算函数，则跳过显示
        return

    tokens = estimate_tokens_fn(
        messages,
        tools_definition=tools_definition,
    )
    pct = (tokens / threshold) * 100
    post_tui(TuiRegion.STATUS, f"📈 Context: {tokens}/{threshold} Tokens ({pct:.1f}%)")
    _render_runtime_info(tokens, threshold)


def _render_startup_banner():
    """渲染启动横幅"""
    STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"
    subtitle = f"Terminal Environment: [bold]{STARTUP_TERMINAL_LABEL}[/bold] (source={STARTUP_TERMINAL_SOURCE})"
    console.print(
        Panel(
            Text(MAKECODE_ASCII.strip("\n"), style="bold bright_blue"),
            title="[bold white]MakeCode CLI[/bold white]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            subtitle=subtitle,
            subtitle_align="center",
            padding=(1, 4),
            expand=True,
        ),
        tui_region=TuiRegion.CONTENT,
    )


def _render_env_customization_hint():
    """渲染模型配置提示"""
    hint_text = (
        "💡 模型配置已迁移到 MakeCode 配置面板：\n"
        "使用 /models 添加、删除、标记常用或切换当前模型。"
    )
    console.print(
        Panel(
            Text(hint_text, style="bold yellow"),
            title="[bold yellow]模型配置提示[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=True,
        ),
        tui_region=TuiRegion.CONTENT,
    )
