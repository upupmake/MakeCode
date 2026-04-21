"""
控制台渲染模块：提供所有与控制台输出相关的渲染函数。
"""
import json
import threading
from typing import Any, List

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from init import log_error_traceback, STARTUP_TERMINAL_TYPE, STARTUP_TERMINAL_SOURCE

console = Console(force_terminal=True)

# 线程锁：用于多子智能体并发输出时保护控制台，防止输出交错
console_lock = threading.Lock()

# =============================================================================
# Sub-Agent 输出控制全局变量
# =============================================================================
# 控制 Sub-Agent 是否输出到主控制台
# True  = 正常输出
# False = 静默模式（默认），Sub-Agent 的输出不会显示在控制台（但仍会写入日志文件）
SHOW_SUB_AGENT_CONSOLE = False


def toggle_sub_agent_console() -> bool:
    """切换 Sub-Agent 控制台输出状态，返回切换后的状态值"""
    global SHOW_SUB_AGENT_CONSOLE
    SHOW_SUB_AGENT_CONSOLE = not SHOW_SUB_AGENT_CONSOLE
    return SHOW_SUB_AGENT_CONSOLE


def get_sub_agent_console():
    """获取当前 Sub-Agent 控制台输出状态"""
    return SHOW_SUB_AGENT_CONSOLE


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


def _extract_message_text(msg: dict) -> str:
    """从消息字典中提取文本内容"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = [
        part["text"] for part in content if isinstance(part, dict) and part.get("text")
    ]
    return "\n\n".join(chunks).strip()


def _format_readable_ui(data: Any, indent_level: int = 0) -> List[Text]:
    """递归解析结构化数据，将其转换为符合人类直觉的 Rich 组件列表"""
    renderables = []
    indent = "  " * indent_level

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and '\n' in value:
                renderables.append(Text(f"{indent}❖ {key}:", style="bold yellow"))
                lines = value.split('\n')
                # 构造类似引用的代码块
                block_text = Text("\n".join(f"{indent}{line}" for line in lines), style="white")
                renderables.append(block_text)

            elif isinstance(value, (dict, list)):
                # 遇到嵌套结构（如 edits 列表）：递归展开
                renderables.append(Text(f"{indent}❖ {key}:", style="bold yellow"))
                renderables.extend(_format_readable_ui(value, indent_level + 1))

            else:
                # 单行普通数值/字符串：直接键值对高亮显示
                renderables.append(Text.assemble(
                    (f"{indent}❖ {key}: ", "bold yellow"),
                    (str(value), "default")
                ))

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, (dict, list)):
                renderables.append(Text(f"{indent}• [Item {i + 1}]", style="bold cyan"))
                renderables.extend(_format_readable_ui(item, indent_level + 1))
            else:
                renderables.append(Text(f"{indent}• {item}", style="default"))

    else:
        renderables.append(Text(f"{indent}{data}", style="default"))

    return renderables


def _render_agent_response_message(text: str, identity: str = "🧠 Orchestrator", response_time: float = None):
    """渲染 Orchestrator 的消息"""
    if not text:
        return
    
    # 构建标题，包含响应时间（如果提供）
    if response_time is not None:
        time_str = f"({response_time:.2f}s)"
    else:
        time_str = ""
    
    title = f"[bold magenta] {identity} {time_str} [/bold magenta] "
    
    console.print(
        Panel(
            Markdown(text),
            title=title,
            border_style="magenta",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


def _render_tool_call(name: str, arguments: Any, identity: str = "🧠 Orchestrator"):
    """渲染工具调用"""
    display_data = arguments
    is_complex = False

    # 1. 解析参数
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if stripped and (stripped.startswith('{') or stripped.startswith('[')):
            try:
                display_data = json.loads(stripped)
                is_complex = True
            except json.JSONDecodeError:
                pass
    elif isinstance(arguments, (dict, list)):
        is_complex = True

    # 2. 渲染 UI
    if is_complex:
        # 使用 Group 将列表里的多行元素组合在一起
        ui_items = _format_readable_ui(display_data)
        body = Group(*ui_items)
    else:
        body = Text(str(display_data))

    # 3. 输出 Panel
    console.print(
        Panel(
            body,
            title=f"[bold cyan]🛠️ Action: {name} <- {identity} [/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


def _render_tool_output(name: str, output: Any, identity: str = "🧠 Orchestrator"):
    """渲染工具输出"""
    text = _stringify_output(output).strip()

    is_complex = False
    display_data = text

    # 尝试判断并解析 JSON 结构
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                display_data = parsed
                is_complex = True
        except json.JSONDecodeError as exc:
            log_error_traceback("main render tool output json decode", exc)

    # 渲染 UI
    if is_complex:
        # 复用之前写的结构化 UI 生成器
        ui_items = _format_readable_ui(display_data)
        body = Group(*ui_items)
    else:
        body = Text(text)

    console.print(
        Panel(
            body,
            title=f"[bold green]✅ Result: {name} <- {identity} [/bold green]",
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


def _render_user_message(text: str):
    """渲染用户消息"""
    if not text:
        return
    console.print(
        Panel(
            Text(text),
            title="[bold green]👤 User[/bold green]",
            border_style="green",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def _render_history(messages: list):
    """渲染历史消息"""
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        elif role == "user":
            _render_user_message(_extract_message_text(msg))
        elif role == "assistant":
            content = msg.get("content")
            if content:
                _render_agent_response_message(content)

            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                tc_func = (
                    tc.get("function", {})
                    if isinstance(tc, dict)
                    else getattr(tc, "function", {})
                )
                if tc_func:
                    tc_name = (
                        tc_func.get("name")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "name", "")
                    )
                    tc_args = (
                        tc_func.get("arguments")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "arguments", "")
                    )
                    if tc_name:
                        _render_tool_call(tc_name, tc_args)
        elif role == "tool" or role == "function":
            content = msg.get("content") or msg.get("output")
            name = msg.get("name") or "Tool"
            if content:
                _render_tool_output(name, content)


def _render_token_usage(
        messages: list,
        tools_definition: Any = None,
        system_prompt: str = "",
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
        system_prompt=system_prompt,
    )
    pct = (tokens / threshold) * 100
    color = "green" if pct < 70 else "yellow" if pct < 90 else "red"
    console.print()
    console.print(
        f"[{color} dim]📈 Context: {tokens}/{threshold} Tokens ({pct:.1f}%)[/]"
    )


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
        )
    )


def _render_env_customization_hint():
    """渲染模型配置提示"""
    hint_text = (
        "💡 模型配置已迁移到 MakeCode 配置面板：\n"
        "使用 /models 添加、删除、标记常用或切换当前模型。\n"
        "配置文件位置：.makecode/model_config.json"
    )
    console.print(
        Panel(
            Text(hint_text, style="bold yellow"),
            title="[bold yellow]模型配置提示[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )
