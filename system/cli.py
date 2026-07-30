import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from version import CURRENT_VERSION


COMMAND_DESCRIPTIONS = {
    "/cmds": "列出所有可用内置命令和功能描述。",
    "/models": "打开模型管理面板，可添加、删除、标记常用、选择当前模型。",
    "/layout": "调整 TUI 面板高度比例：左侧 Content/Tools，右侧 Task/Background/Sub-Agent。",
    "/flush": "完整刷新 TUI 屏幕，不改变任何面板中已有的内容。",
    "/mcp-view": "查看当前已加载的 MCP 服务器和工具。",
    "/mcp-help": "显示 MCP 相关命令介绍。",
    "/mcp-add": "<name> 添加 MCP 服务配置；stdio 示例：/mcp-add fs -- npx -y @server/pkg；HTTP 示例：/mcp-add api --url https://example.com/mcp --header X-Api-Key=xxx。服务名已存在时请先 /mcp-delete <name>。",
    "/mcp-delete": "<name> 删除 MCP 服务配置；会二次确认并停用运行中的服务。",
    "/mcp-restart": "重新启动 MCP 管理器并加载配置。",
    "/mcp-switch": "交互式切换 MCP 服务启用/禁用状态，并支持确认或取消保存。",
    "/load": "列出历史 checkpoint 并选择加载。",
    "/skills-switch": "切换 skills 目录摘要注入状态（开启/关闭）。",
    "/skills-list": "列出当前工作区可用的 skills。",
    "/compact": "[prompt] 压缩当前对话上下文；prompt 可选，不填则使用默认压缩提示，并自动尝试提取关键记忆信息。",
    "/memory-list": "列出当前保存的长期记忆。",
    "/memory-panel": "打开长期记忆交互面板，可查看详情并二次确认删除。",
    "/memory-delete": "<memory_id> [memory_id ...] 按 ID 删除一条或多条长期记忆。",
    "/memory-config": "打开记忆配置面板，修改 memory_size 和 keep_recent_tool_call。",
    "/memory-update": "[prompt] 根据用户请求主动管理长期记忆；prompt 可选，不填则根据当前对话使用默认记忆管理提示。",
    "/tasks": "查看完整任务看板和当前执行进度。",
    "/copy": "打开只读弹窗查看对话内容（user/assistant），支持选择文本并按 C 键复制。",
    "/plan": "进入或退出 Plan Mode；规划阶段只允许只读和任务规划工具。",
    "/sub-agent-console": "切换 Sub-Agent 的控制台输出状态，默认开启。",
    "/help": "显示帮助信息和所有可用命令。",
    "/new": "清空当前对话历史，并开启当前工作区的全新对话。",
    "/pwd": "显示当前工作目录。",
    "/cd": "<path> 切换当前工作目录，例如 /cd D:\\Projects\\Demo。",
    "/hitl": "切换 Human-in-the-Loop 拦截状态（开启/关闭）。",
    "/quit": "退出程序。",
    "/exit": "退出程序。",
    "/update": "检查并安装最新版本更新。",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MakeCode",
        description="AI-powered multi-agent command-line orchestrator.",
    )
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("-V", "--version", action="store_true", help="显示版本并退出")
    commands.add_argument("--commands", action="store_true", help="列出 TUI 中可用的斜杠命令并退出")
    commands.add_argument("--models-list", action="store_true", help="列出已配置模型并退出")
    commands.add_argument("--mcp-list", action="store_true", help="列出已配置 MCP 服务并退出")
    commands.add_argument("--skills-list", action="store_true", help="列出当前工作区可用 Skills 并退出")
    commands.add_argument("--check-update", action="store_true", help="检查新版本但不下载或安装")
    return parser


def _print_commands() -> None:
    print("MakeCode 内置斜杠命令：")
    for command, description in COMMAND_DESCRIPTIONS.items():
        print(f"  {command:<20} {description}")


def _plain_text(value: object) -> str:
    printable = "".join(char if char.isprintable() else " " for char in str(value))
    return " ".join(printable.split())


def _models_list() -> int:
    from system.models import ModelManager
    from utils import paths

    manager = ModelManager(paths.install_makecode_dir(create=False))
    print(f"模型配置: {manager.config_file}")
    if manager.load_error is not None:
        print(manager.get_load_error_display(), file=sys.stderr)
        return 1
    if not manager.models:
        print("未配置模型。")
        return 0

    print(f"已配置模型 ({len(manager.models)}):")
    for index, model in enumerate(manager.models, 1):
        labels = []
        if model.key == manager.current_model_key:
            labels.append("当前")
        if model.is_favorite:
            labels.append("收藏")
        label_text = f" ({', '.join(labels)})" if labels else ""
        print(
            f"  {index}. {_plain_text(model.model_id)} · {_display_model_host(model.base_url)} · "
            f"{model.message_format} · effort={model.reasoning_effort} · "
            f"context={_plain_text(model.max_context)}k{label_text}"
        )
    return 0


def _display_url(value: object) -> str:
    url = _plain_text(value)
    try:
        parsed = urlsplit(url)
    except ValueError:
        parsed = None
    if parsed is None or not parsed.scheme or not parsed.netloc:
        return url.rsplit("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _display_model_host(base_url: object) -> str:
    url = _plain_text(base_url)
    try:
        parsed = urlsplit(url if "://" in url else f"https://{url}")
    except ValueError:
        return url.rsplit("@", 1)[-1].split("/", 1)[0] or "未知地址"
    return parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0] or "未知地址"


def _mcp_list() -> int:
    from utils import paths

    config_file = paths.mcp_config_file(create=False)
    print(f"MCP 配置: {config_file}")
    if not config_file.exists():
        print("未配置 MCP 服务。")
        return 0

    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("MCP 配置必须是对象")
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers 字段必须是对象")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"无法读取 MCP 配置文件 {config_file}: {exc}", file=sys.stderr)
        return 1

    if not servers:
        print("未配置 MCP 服务。")
        return 0

    print(f"已配置 MCP 服务 ({len(servers)}):")
    for index, (name, raw_config) in enumerate(servers.items(), 1):
        config = raw_config if isinstance(raw_config, dict) else {}
        status = "禁用" if config.get("disabled", False) else "启用"
        url = config.get("url")
        transport = config.get("transport") or config.get("type")
        if not transport:
            transport = "sse" if url and "/sse" in str(url).lower() else "streamable-http" if url else "stdio"
        target = f"url={_display_url(url)}" if url else f"command={_plain_text(config.get('command', '未配置'))}"
        print(f"  {index}. {_plain_text(name)} · {status} · {_plain_text(transport)} · {target}")
    return 0


def _skills_list() -> int:
    from utils.skill_catalog import discover_skills, skill_directories

    parse_errors = []

    def record_parse_error(skill_file: Path, exc: Exception) -> None:
        parse_errors.append((skill_file, exc))

    try:
        directories = skill_directories(create=False)
        skills = discover_skills(directories, on_parse_error=record_parse_error)
    except (OSError, UnicodeError) as exc:
        print(f"无法读取 Skills: {exc}", file=sys.stderr)
        return 1

    print("Skills 搜索目录（优先级从高到低）:")
    for directory in directories:
        print(f"  {directory.resolve()}")
    for skill_file, exc in parse_errors:
        print(f"跳过无法解析的 Skill {skill_file}: {exc}", file=sys.stderr)

    if not skills:
        print("当前工作区没有可用 Skills。")
        return 0

    print(f"当前可用 Skills ({len(skills)}):")
    for index, (name, skill) in enumerate(skills.items(), 1):
        description = _plain_text(skill["meta"]["description"])
        directory = Path(skill["path"]).parent.resolve()
        print(f"  {index}. {_plain_text(name)} · {description}")
        print(f"     {directory}")
    return 0


def _check_update() -> int:
    from system.updater import check_update

    print(f"当前版本: {CURRENT_VERSION}")
    print("正在检查更新...")
    try:
        version_info = check_update(raise_errors=True)
    except Exception as exc:
        print(f"检查更新失败: {exc}")
        return 1

    if version_info is None:
        print("当前已是最新版本。")
        return 0

    print(f"发现新版本: {version_info.get('version', '未知')}")
    release_log = version_info.get("release_log")
    if release_log:
        print(f"更新说明:\n{release_log}")
    print("此命令只执行检查；请启动 MakeCode 后使用 /update 安装更新。")
    return 0


def run_external_cli(argv: Sequence[str]) -> int | None:
    if not argv:
        return None

    args = _build_parser().parse_args(argv)
    if args.version:
        print(f"MakeCode {CURRENT_VERSION}")
        return 0
    if args.commands:
        _print_commands()
        return 0
    if args.models_list:
        return _models_list()
    if args.mcp_list:
        return _mcp_list()
    if args.skills_list:
        return _skills_list()
    if args.check_update:
        return _check_update()
    return None
