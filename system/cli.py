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
    "/load": "列出 6.0 会话并选择加载；自动恢复任务计划和 Sub-Agent 历史。",
    "/skills-switch": "切换 skills 目录摘要注入状态（开启/关闭）。",
    "/skills-list": "打开当前工作区 Skills 配置面板，可搜索、按状态过滤并启用或禁用技能。",
    "/compact": "[prompt] 压缩当前对话上下文；prompt 可选，不填则使用默认压缩提示，并自动尝试提取关键记忆信息。",
    "/memory-list": "列出当前保存的长期记忆。",
    "/memory-panel": "打开长期记忆交互面板，可查看详情并二次确认删除。",
    "/memory-delete": "<memory_id> [memory_id ...] 按 ID 删除一条或多条长期记忆。",
    "/memory-config": "打开记忆配置面板，修改全局上下文长度和记忆设置。",
    "/memory-update": "[prompt] 根据用户请求主动管理长期记忆；prompt 可选，不填则根据当前对话使用默认记忆管理提示。",
    "/tasks": "查看完整任务看板和当前执行进度。",
    "/tool-history": "打开工具执行历史浏览器，支持按工具汇总、全文搜索、状态和来源筛选以及完整详情。",
    "/copy": "打开只读对话导出面板，仅查看用户提问、LLM 正文回答及终端命令输入输出，支持选择文本、按钮或快捷键复制。",
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
    commands.add_argument("--mcp-add", nargs=argparse.REMAINDER, metavar="ARG", help="添加禁用状态的 MCP 服务配置并退出")
    commands.add_argument("--skills-list", action="store_true", help="列出当前工作区可用 Skills 并退出")
    commands.add_argument("--memory-list", action="store_true", help="列出当前工作区长期记忆并退出")
    commands.add_argument("--check-update", action="store_true", help="检查新版本但不下载或安装")
    commands.add_argument("--update", action="store_true", help="检查、下载并安装最新版本")
    parser.add_argument("-y", "--yes", action="store_true", help="与 --update 配合使用，跳过安装确认")
    return parser


def _print_commands() -> None:
    print("MakeCode 内置斜杠命令：")
    for command, description in COMMAND_DESCRIPTIONS.items():
        print(f"  {command:<20} {description}")


def _plain_text(value: object) -> str:
    printable = "".join(char if char.isprintable() else " " for char in str(value))
    normalized = " ".join(printable.split())
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return normalized.encode(encoding, errors="replace").decode(encoding)


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
            f"{model.message_format} · effort={model.reasoning_effort}{label_text}"
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


def _mcp_add(arguments: Sequence[str]) -> int:
    from utils import paths
    from utils.mcp_config import MCP_ADD_USAGE, add_mcp_server_config, parse_mcp_add_args

    try:
        server_name, config = parse_mcp_add_args(arguments)
    except ValueError as exc:
        print(f"MCP 参数无效: {_plain_text(exc)}", file=sys.stderr)
        print(f"用法: MakeCode {MCP_ADD_USAGE}", file=sys.stderr)
        return 2

    config_file = paths.mcp_config_file(create=False)
    try:
        add_mcp_server_config(config_file, server_name, config)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"无法添加 MCP 服务配置: {_plain_text(exc)}", file=sys.stderr)
        return 1

    print(f"已添加 MCP 服务: {_plain_text(server_name)}")
    print(f"配置文件: {config_file}")
    print("新服务默认为禁用状态；请启动 MakeCode 后使用 /mcp-switch 启用。")
    return 0


def _skills_list() -> int:
    from utils import paths
    from utils.skill_catalog import (
        discover_skills,
        read_disabled_skill_names,
        skill_directories,
    )

    parse_errors = []

    def record_parse_error(skill_file: Path, exc: Exception) -> None:
        parse_errors.append((skill_file, exc))

    try:
        directories = skill_directories(create=False)
        skills = discover_skills(directories, on_parse_error=record_parse_error)
        disabled_skill_names = read_disabled_skill_names(
            paths.workspace_disabled_skills_file(create=False)
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
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

    print(f"当前 Skills ({len(skills)}):")
    for index, (name, skill) in enumerate(skills.items(), 1):
        description = _plain_text(skill["meta"]["description"])
        directory = Path(skill["path"]).parent.resolve()
        status = "已禁用" if name in disabled_skill_names else "已启用"
        print(f"  {index}. {_plain_text(name)} · {description} · {status}")
        print(f"     {directory}")
    return 0


def _memory_list() -> int:
    from utils import paths
    from utils.memory_catalog import read_memory_records, sort_memory_records

    memory_file = paths.workspace_makecode_dir(create=False) / "memory" / "memory.jsonl"
    print(f"长期记忆文件: {memory_file}")
    try:
        memories = sort_memory_records(read_memory_records(memory_file))
    except (OSError, UnicodeError) as exc:
        print(f"无法读取长期记忆: {exc}", file=sys.stderr)
        return 1

    if not memories:
        print("暂无长期记忆（active: 0）。")
        return 0

    print(f"当前长期记忆（active: {len(memories)}）:")
    for index, memory in enumerate(memories, 1):
        memory_id = _plain_text(memory.get("id", ""))
        category = _plain_text(memory.get("category", ""))
        updated_at = _plain_text(memory.get("updated_at") or memory.get("created_at", ""))
        insight = _plain_text(memory.get("insight", ""))
        reuse_condition = _plain_text(memory.get("reuse_condition", ""))
        print(f"  {index}. {memory_id} · {category} · {updated_at}")
        print(f"     Insight: {insight}")
        print(f"     Reuse condition: {reuse_condition}")
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
    print("此命令只执行检查；可使用 --update 或 TUI /update 安装更新。")
    return 0


def _update(*, assume_yes: bool) -> int:
    from system.updater import AUTO_UPDATE_SUPPORTED, check_update, download_update, launch_updater

    print(f"当前版本: {CURRENT_VERSION}")
    if not getattr(sys, "frozen", False):
        print("源码运行环境不支持自动更新，请使用 PyInstaller 打包版本。", file=sys.stderr)
        return 1
    if not AUTO_UPDATE_SUPPORTED:
        print("当前平台不支持自动更新，请从 GitHub Release 手动下载。", file=sys.stderr)
        return 1

    print("正在检查更新...")
    try:
        version_info = check_update(raise_errors=True)
    except Exception as exc:
        print(f"检查更新失败: {exc}", file=sys.stderr)
        return 1

    if version_info is None:
        print("当前已是最新版本。")
        return 0

    new_version = version_info.get("version", "未知")
    print(f"发现新版本: {new_version}")
    release_log = version_info.get("release_log")
    if release_log:
        print(f"更新说明:\n{release_log}")

    if not assume_yes:
        if sys.stdin is None or not sys.stdin.isatty():
            print("非交互终端无法确认更新；请使用 --update --yes 明确授权。", file=sys.stderr)
            return 1
        try:
            answer = input("是否下载并安装更新？[y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消更新。")
            return 0
        if answer not in {"y", "yes"}:
            print("已取消更新。")
            return 0

    progress_state = {"percent": -1, "megabytes": -1, "shown": False}

    def show_progress(downloaded: int, total: int | None) -> None:
        if total:
            percent = int(downloaded / total * 100)
            if percent == progress_state["percent"] and downloaded < total:
                return
            progress_state["percent"] = percent
            text = f"\r下载进度: {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB ({percent}%)"
        else:
            megabytes = downloaded // 1024 // 1024
            if megabytes == progress_state["megabytes"]:
                return
            progress_state["megabytes"] = megabytes
            text = f"\r已下载: {megabytes} MB"
        progress_state["shown"] = True
        print(text, end="", flush=True)

    print("正在下载更新...")
    try:
        update_archive = download_update(version_info, progress_callback=show_progress)
    except Exception as exc:
        if progress_state["shown"]:
            print()
        print(f"下载更新失败: {exc}", file=sys.stderr)
        return 1
    if progress_state["shown"]:
        print()
    if update_archive is None:
        print("下载或校验失败。", file=sys.stderr)
        return 1

    print("下载完成，正在退出主程序并启动更新程序...", flush=True)
    print("替换完成后会显示结果，请手动重新启动 MakeCode。", flush=True)
    try:
        launch_updater(update_archive)
    except Exception as exc:
        print(f"启动更新程序失败: {exc}", file=sys.stderr)
        return 1
    return 0


def run_external_cli(argv: Sequence[str]) -> int | None:
    if not argv:
        return None
    if argv[0] == "--mcp-add":
        return _mcp_add(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.yes and not args.update:
        parser.error("-y/--yes 只能与 --update 一起使用")
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
    if args.mcp_add is not None:
        return _mcp_add(args.mcp_add)
    if args.skills_list:
        return _skills_list()
    if args.memory_list:
        return _memory_list()
    if args.check_update:
        return _check_update()
    if args.update:
        return _update(assume_yes=args.yes)
    return None
