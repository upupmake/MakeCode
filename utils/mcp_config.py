import argparse
import json
import shlex
from collections.abc import Sequence
from pathlib import Path


MCP_ADD_USAGE = "--mcp-add <name> [options] -- <cmd> [args...] 或 --mcp-add <name> --url <url> [options]"


class _McpArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "启用", "是"}:
        return True
    if normalized in {"0", "false", "no", "off", "禁用", "否"}:
        return False
    raise ValueError(f"无法解析布尔值: {value}")


def _parse_pair(value: str, option_name: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"{option_name} 需要 KEY=VALUE 格式")
    key, item_value = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"{option_name} 的 KEY 不能为空")
    return key, item_value


def _set_nested_field(cfg: dict, key: str, value: str) -> None:
    if "." not in key:
        cfg[key] = value
        return
    parent, child = key.split(".", 1)
    if not parent or not child:
        raise ValueError(f"字段格式无效: {key}")
    target = cfg.setdefault(parent, {})
    if not isinstance(target, dict):
        raise ValueError(f"字段 {parent} 已存在且不是对象，不能设置 {key}")
    target[child] = value


def _build_mcp_add_parser(command_name: str) -> argparse.ArgumentParser:
    parser = _McpArgumentParser(prog=command_name, add_help=False)
    parser.add_argument("name")
    parser.add_argument("--url")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "http", "sse"])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--cwd")
    parser.add_argument("--auth")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--sse-read-timeout", dest="sse_read_timeout", type=float)
    parser.add_argument("--keep-alive", dest="keep_alive")
    return parser


def parse_mcp_add_args(arguments: Sequence[str], *, command_name: str = "--mcp-add") -> tuple[str, dict]:
    if not arguments:
        raise ValueError(f"用法：{MCP_ADD_USAGE.replace('--mcp-add', command_name)}")

    parse_tokens = list(arguments)
    command_parts = []
    if "--" in parse_tokens:
        separator_index = parse_tokens.index("--")
        command_parts = parse_tokens[separator_index + 1:]
        parse_tokens = parse_tokens[:separator_index]

    try:
        namespace, extra_fields = _build_mcp_add_parser(command_name).parse_known_args(parse_tokens)
    except SystemExit as exc:
        raise ValueError("参数格式无效") from exc

    server_name = namespace.name
    if server_name.startswith("-"):
        raise ValueError(f"{command_name} 需要先提供服务名")
    if not command_parts and not namespace.url:
        raise ValueError(f"{command_name} 必须提供 -- 后的启动命令或 --url")
    if command_parts and namespace.url:
        raise ValueError("--url 不能和 -- 后的启动命令同时使用")

    cfg = {}
    if command_parts:
        cfg["command"] = command_parts[0]
        if len(command_parts) > 1:
            cfg["args"] = command_parts[1:]
    if namespace.url:
        cfg["url"] = namespace.url
    if namespace.transport:
        cfg["transport"] = namespace.transport
    if namespace.cwd:
        cfg["cwd"] = namespace.cwd
    if namespace.auth:
        cfg["auth"] = namespace.auth
    if namespace.timeout is not None:
        cfg["timeout"] = namespace.timeout
    if namespace.sse_read_timeout is not None:
        cfg["sse_read_timeout"] = namespace.sse_read_timeout
    if namespace.keep_alive is not None:
        cfg["keep_alive"] = _parse_bool(namespace.keep_alive)
    cfg["disabled"] = True

    for item in namespace.env:
        key, value = _parse_pair(item, "--env")
        cfg.setdefault("env", {})[key] = value
    for item in namespace.header:
        key, value = _parse_pair(item, "--header")
        cfg.setdefault("headers", {})[key] = value
    for item in extra_fields:
        if "=" not in item:
            raise ValueError(f"未知参数: {item}")
        key, value = _parse_pair(item, "字段")
        if key == "disabled":
            raise ValueError("不支持配置 disabled；新服务始终以禁用状态添加")
        _set_nested_field(cfg, key, value)

    if "url" in cfg and "transport" not in cfg:
        cfg["transport"] = "sse" if "/sse" in cfg["url"] else "streamable-http"
    if cfg.get("transport") == "http":
        cfg["transport"] = "streamable-http"
    if "command" in cfg and "transport" not in cfg:
        cfg["transport"] = "stdio"

    return server_name, cfg


def parse_mcp_add_query(query: str) -> tuple[str, dict]:
    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        raise ValueError(f"命令参数解析失败: {exc}") from exc
    if not tokens or tokens[0] != "/mcp-add":
        raise ValueError(f"用法：{MCP_ADD_USAGE.replace('--mcp-add', '/mcp-add')}")
    return parse_mcp_add_args(tokens[1:], command_name="/mcp-add")


def add_mcp_server_config(config_path: Path, server_name: str, cfg: dict) -> dict:
    if config_path.exists():
        config_dict = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_dict, dict):
            raise ValueError("MCP 配置必须是对象")
    else:
        config_dict = {"mcpServers": {}}

    servers = config_dict.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 字段必须是对象")
    if server_name in servers:
        raise ValueError(f"MCP 服务已存在: {server_name}。请先执行 /mcp-delete {server_name} 后再添加。")

    servers[server_name] = cfg
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_dict
