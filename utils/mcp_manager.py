import asyncio
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastmcp import Client
from rich.markup import escape

from system.tui_app import TuiRegion, post_tui, refresh_status
from utils import paths
from utils.mcp_config import add_mcp_server_config


def print_formatted_text(value):
    post_tui(TuiRegion.BACKGROUND, str(value))

from init import log_error_traceback

mcp_config_path = paths.mcp_config_file()


class GlobalMCPManager:
    def __init__(self):
        self.config_path = mcp_config_path
        self.console = None
        self.loop = None
        self.thread = None
        self._stop_event = None

        self.server_configs = {}
        self.clients = {}
        self._server_tools = {}
        self._server_status_tools = {}
        self._mcp_tools = []
        self._mcp_handlers = {}
        self._status_tools = []

        self._db_lock = threading.Lock()
        self._is_running = False

    def initialize(self, console):
        self.console = console

    def _load_config_dict(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_config_dict(self, config_dict: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)

    def read_config(self) -> dict:
        if not self.config_path or not self.config_path.exists():
            raise FileNotFoundError(f"MCP 配置文件不存在: {self.config_path}")
        return self._load_config_dict()

    @staticmethod
    def _display_target(cfg: dict) -> str:
        command = cfg.get("command")
        if command:
            return str(command)
        url = str(cfg.get("url") or "")
        if not url:
            return "未配置连接目标"
        try:
            parsed = urlsplit(url)
        except ValueError:
            parsed = None
        if parsed is None or not parsed.scheme or not parsed.netloc:
            return url.rsplit("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def list_server_switches(self) -> list:
        config_dict = self.read_config()
        servers = config_dict.get("mcpServers", {})
        result = []
        with self._db_lock:
            loaded_servers = set(self.clients.keys())
            tool_counts = {
                name: len(self._server_status_tools.get(name, []))
                for name in servers
            }
        for name, cfg in servers.items():
            disabled = bool(cfg.get("disabled", False))
            url = cfg.get("url")
            transport = cfg.get("transport") or cfg.get("type")
            if not transport:
                transport = "sse" if url and "/sse" in str(url).lower() else "streamable-http" if url else "stdio"
            result.append(
                {
                    "name": name,
                    "disabled": disabled,
                    "enabled": not disabled,
                    "loaded": name in loaded_servers,
                    "transport": transport,
                    "target": self._display_target(cfg),
                    "tool_count": tool_counts[name],
                }
            )
        return result

    def start_background(self):
        if self._is_running:
            return
        if not self.config_path or not self.config_path.exists():
            # 自动创建空的 MCP 配置文件
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self._save_config_dict({"mcpServers": {}})
                if self.console:
                    print_formatted_text(
                        f"[bold cyan]ℹ️ MCP 配置文件不存在，已自动创建空配置。\n   路径: {escape(str(self.config_path))}[/bold cyan]"
                    )
            except Exception as e:
                log_error_traceback("MCP Config Create Error", e)
                if self.console:
                    print_formatted_text(
                        f"[bold red]⚠️ 创建 MCP 配置文件失败: {escape(str(e))}[/bold red]"
                    )
            return

        try:
            config_dict = self._load_config_dict()
            self.server_configs = config_dict.get("mcpServers", {})
        except Exception as e:
            log_error_traceback("MCP Config Load Error", e)
            if self.console:
                print_formatted_text(
                    f"[bold red]⚠️ 加载 MCP 配置失败: {escape(str(e))}[/bold red]"
                )
            return

        if not self.server_configs:
            if self.console:
                print_formatted_text(
                    f"[bold yellow]⚠️ MCP 服务为空，暂无可用服务。\n   路径: {escape(str(self.config_path))}[/bold yellow]"
                )
            return

        if self.console:
            names = ", ".join(self.server_configs.keys())
            print_formatted_text(
                f"[cyan]🔄 识别到 {len(self.server_configs)} 个 MCP 服务 ({escape(names)})[/cyan]\n"
            )
        self.loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        self._is_running = True

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        current_loop = self.loop
        asyncio.set_event_loop(current_loop)
        try:
            current_loop.run_until_complete(self._async_lifecycle())
        except Exception as e:
            log_error_traceback("MCP Background Loop Error", e)
            if self.console:
                print_formatted_text(
                    f"[bold red]⚠️ MCP Background Loop Error: {escape(str(e))}[/bold red]"
                )
        finally:
            self._is_running = False
            try:
                if current_loop and not current_loop.is_closed():
                    import gc

                    gc.collect()
                    current_loop.run_until_complete(asyncio.sleep(0.1))
                    current_loop.close()
            except Exception:
                pass
            finally:
                self.loop = None
                self.thread = None
                self._stop_event = None

    def _build_tool_name(self, server_name: str, raw_name: str) -> str:
        raw_tool_name = f"{server_name}_{raw_name}"
        return re.sub(r"[^a-zA-Z0-9_-]", "_", raw_tool_name)[:64]

    def _rebuild_global_registry_locked(self):
        all_tools = []
        all_handlers = {}
        all_status_tools = []

        for server_name in self.server_configs.keys():
            all_tools.extend(self._server_tools.get(server_name, []))
            server_status_items = self._server_status_tools.get(server_name, [])
            all_status_tools.extend(server_status_items)
            client = self.clients.get(server_name)
            if not client:
                continue
            for item in server_status_items:
                tool_name = item.get("name")
                original_name = item.get("original_name")
                if not tool_name or not original_name:
                    continue
                all_handlers[tool_name] = self._make_handler(
                    client=client,
                    original_name=original_name,
                    tool_name=tool_name,
                )

        self._mcp_tools = all_tools
        self._mcp_handlers = all_handlers
        self._status_tools = [
            {
                "name": item["name"],
                "description": item["description"],
                "provider": item["provider"],
            }
            for item in all_status_tools
        ]

    def _make_handler(self, client, original_name: str, tool_name: str):
        def handler(**kwargs):
            try:
                if not self.loop:
                    return f"Error executing tool '{tool_name}': MCP event loop is not running"
                future = asyncio.run_coroutine_threadsafe(
                    client.call_tool(original_name, kwargs),
                    self.loop,
                )
                result = future.result(timeout=120)

                if hasattr(result, "content") and isinstance(result.content, list):
                    texts = [c.text for c in result.content if hasattr(c, "text")]
                    if texts:
                        return "\n".join(texts)
                    return str(result.content)

                if hasattr(result, "data"):
                    return str(result.data)
                if hasattr(result, "content"):
                    return str(result.content)
                return str(result)
            except Exception as ex:
                log_error_traceback(
                    f"MCP Tool Execution Error [{tool_name}]",
                    ex,
                )
                return f"Error executing tool '{tool_name}': {ex}"

        return handler

    def _should_use_safe_stdio_log(self, cfg: dict) -> bool:
        if sys.platform != "win32":
            return False
        if not isinstance(cfg, dict) or not cfg.get("command"):
            return False
        transport = cfg.get("transport") or cfg.get("type") or "stdio"
        return transport == "stdio"

    def _build_client(self, server_name: str, cfg: dict) -> Client:
        transport = {"mcpServers": {server_name: cfg}}
        if not self._should_use_safe_stdio_log(cfg):
            return Client(transport)

        from fastmcp.client.transports.stdio import StdioTransport

        stdio_transport = StdioTransport(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
            cwd=cfg.get("cwd"),
            keep_alive=cfg.get("keep_alive"),
            log_file=paths.mcp_stderr_log_file(),
        )
        return Client(stdio_transport)

    async def _connect_server(self, server_name: str, cfg: dict) -> bool:
        if cfg.get("disabled", False):
            if self.console:
                print_formatted_text(
                    f"[bold yellow]⚠️ MCP 服务 '{escape(str(server_name))}' 已被标记为禁用，跳过加载。[/bold yellow]"
                )
            return False

        if server_name in self.clients:
            return True

        client = None
        for attempt in range(2):
            client = self._build_client(server_name, cfg)
            try:
                # 独立管理连接生命周期
                await client.__aenter__()

                raw_tools = await client.list_tools()
                server_tools = []
                server_status_tools = []

                if self.console:
                    print_formatted_text(
                        f"[green]✅ 成功连接 MCP 服务: [bold]'{escape(str(server_name))}'[/bold] (已加载 {len(raw_tools)} 个工具)[/green]\n"
                    )

                for t in raw_tools:
                    tool_name = self._build_tool_name(server_name, t.name)
                    t_dict = (
                        t.model_dump(exclude_none=True)
                        if hasattr(t, "model_dump")
                        else dict(t)
                    )
                    t_dict["name"] = tool_name

                    if not t_dict.get("inputSchema"):
                        t_dict["inputSchema"] = {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }

                    server_tools.append(t_dict)
                    desc = (
                            t_dict.get("description")
                            or f"MCP Tool: {t.name} from {server_name}"
                    )
                    server_status_tools.append(
                        {
                            "name": tool_name,
                            "description": desc,
                            "provider": server_name,
                            "original_name": t.name,
                        }
                    )

                with self._db_lock:
                    self.clients[server_name] = client
                    self._server_tools[server_name] = server_tools
                    self._server_status_tools[server_name] = server_status_tools
                    self._rebuild_global_registry_locked()
                refresh_status()
                return True
            except Exception as e:
                # 清理失败的 Client 资源
                if client and hasattr(client, "__aexit__"):
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception:
                        pass
                
                with self._db_lock:
                    self.clients.pop(server_name, None)
                    self._server_tools.pop(server_name, None)
                    self._server_status_tools.pop(server_name, None)
                    self._rebuild_global_registry_locked()
                
                if attempt == 0:  # 第一次失败，重试
                    continue
                else:  # 第二次失败，抛出错误
                    log_error_traceback(f"MCP Server Load Error [{server_name}]", e)
                    if self.console:
                        print_formatted_text(
                            f"\r[bold red]⚠️ 无法加载 MCP 服务 '{escape(str(server_name))}': {escape(str(e))}[/bold red]"
                        )
                    raise
        
        return False

    async def _disconnect_server(self, server_name: str):
        client = None
        with self._db_lock:
            client = self.clients.pop(server_name, None)
            self._server_tools.pop(server_name, None)
            self._server_status_tools.pop(server_name, None)
            self._rebuild_global_registry_locked()
        refresh_status()

        if client and hasattr(client, "__aexit__"):
            try:
                # 优雅断开底层的清理
                await client.__aexit__(None, None, None)
            except Exception as e:
                log_error_traceback(f"MCP Server Close Error [{server_name}]", e)

    async def _async_lifecycle(self):
        try:
            # 并行加载所有服务
            async def _connect_with_error(server_name: str, cfg: dict):
                try:
                    await self._connect_server(server_name, cfg)
                    return server_name, True, None
                except Exception as e:
                    log_error_traceback(f"MCP Server Connect Failed [{server_name}]", e)
                    return server_name, False, e

            tasks = [
                _connect_with_error(name, cfg)
                for name, cfg in self.server_configs.items()
            ]
            results = await asyncio.gather(*tasks)

            # 打印失败的服务
            for server_name, success, error in results:
                if not success and self.console:
                    print_formatted_text(
                        f"[bold yellow]⚠️ MCP 服务 '{escape(str(server_name))}' 连接失败，跳过: {escape(str(error))}[/bold yellow]"
                    )

            with self._db_lock:
                self._rebuild_global_registry_locked()
            refresh_status()

            await self._stop_event.wait()
        except Exception as e:
            log_error_traceback("MCP Async Lifecycle Loop Error", e)
            if self.console:
                print_formatted_text(
                    f"\r[bold red]⚠️ MCP 后台连接异常断开: {escape(str(e))}[/bold red]"
                )
        finally:
            # 清理所有仍存活的客户端
            active_servers = list(self.clients.keys())
            for server_name in active_servers:
                await self._disconnect_server(server_name)

            with self._db_lock:
                self.clients.clear()
                self._server_tools = {}
                self._server_status_tools = {}
                self._mcp_tools = []
                self._mcp_handlers = {}
                self._status_tools = []
            refresh_status()

    def get_tools(self) -> list:
        with self._db_lock:
            return list(self._mcp_tools)

    def get_handlers(self) -> dict:
        with self._db_lock:
            return dict(self._mcp_handlers)

    def get_registry_snapshot(self) -> tuple[list, dict]:
        with self._db_lock:
            return list(self._mcp_tools), dict(self._mcp_handlers)

    def stop(self):
        with self._db_lock:
            self._mcp_tools = []
            self._mcp_handlers = {}
            self._status_tools = []
            self._server_tools = {}
            self._server_status_tools = {}
            # Do NOT clear self.clients here, let _async_lifecycle clean them up
            # so that _disconnect_server can gracefully close the connections.

        if self._is_running and self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)

        self._is_running = False

    def restart(self, config_path: Path = None):
        self.stop()
        if config_path:
            self.config_path = config_path

        if self.console:
            print_formatted_text(
                "\n[bold cyan]🔄 正在重新加载 MCP 配置并重启后台服务...[/bold cyan]"
            )

        self.start_background()

    def add_server_config(self, server_name: str, cfg: dict) -> dict:
        config_dict = add_mcp_server_config(self.config_path, server_name, cfg)
        servers = config_dict["mcpServers"]
        self.server_configs = servers

        enable_targets = [] if cfg.get("disabled", False) else [server_name]
        failed = []
        if enable_targets:
            if not self._is_running:
                self.start_background()
            elif self.loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._enable_servers_parallel(enable_targets, servers),
                        self.loop,
                    )
                    failed.extend(future.result(timeout=120))
                except Exception as e:
                    failed.append({"server": server_name, "action": "enable", "error": str(e)})
                    log_error_traceback(f"MCP Add Enable Error [{server_name}]", e)
            else:
                failed.append({"server": server_name, "action": "enable", "error": "MCP event loop 未运行"})

        return {
            "saved": True,
            "server": server_name,
            "enabled": enable_targets,
            "failed": failed,
            "message": "MCP 服务配置已保存，并已尝试启用服务。" if enable_targets else "MCP 服务配置已保存，当前为禁用状态。",
        }

    def delete_server_config(self, server_name: str) -> dict:
        config_dict = self.read_config()
        servers = config_dict.get("mcpServers", {})
        if server_name not in servers:
            raise ValueError(f"MCP 服务不存在: {server_name}")

        was_loaded = False
        with self._db_lock:
            was_loaded = server_name in self.clients

        servers.pop(server_name)
        self._save_config_dict(config_dict)
        self.server_configs = servers

        failed = []
        if was_loaded:
            if self._is_running and self.loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._disconnect_server(server_name),
                        self.loop,
                    )
                    future.result(timeout=30)
                    if self.console:
                        print_formatted_text(
                            f"[bold yellow]⏹️ 已停用 MCP 服务: '{escape(str(server_name))}'[/bold yellow]"
                        )
                except Exception as e:
                    failed.append({"server": server_name, "action": "disable", "error": str(e)})
                    log_error_traceback(f"MCP Delete Disable Error [{server_name}]", e)
            else:
                failed.append({"server": server_name, "action": "disable", "error": "MCP event loop 未运行"})
        else:
            with self._db_lock:
                self._rebuild_global_registry_locked()
            refresh_status()

        return {
            "saved": True,
            "server": server_name,
            "was_loaded": was_loaded,
            "failed": failed,
            "message": "MCP 服务配置已删除，并已尝试停用运行中的服务。" if was_loaded else "MCP 服务配置已删除。",
        }

    async def _enable_servers_parallel(self, enable_targets: list, servers: dict) -> list:
        """并行启用多个 MCP 服务"""
        failed = []
        
        async def _enable_one(server_name: str):
            try:
                ok = await self._connect_server(server_name, servers[server_name])
                if ok and self.console:
                    print_formatted_text(
                        f"[bold green]✅ 已启用 MCP 服务: '{escape(str(server_name))}'[/bold green]"
                    )
                if not ok:
                    return {"server": server_name, "action": "enable", "error": "连接失败"}
                return None
            except Exception as e:
                log_error_traceback(f"MCP Enable Error [{server_name}]", e)
                return {"server": server_name, "action": "enable", "error": str(e)}
        
        tasks = [_enable_one(name) for name in enable_targets]
        results = await asyncio.gather(*tasks)
        
        for result in results:
            if result:
                failed.append(result)
        
        return failed

    def apply_switches(self, disabled_updates: dict) -> dict:
        if not disabled_updates:
            return {
                "saved": False,
                "changed": [],
                "enabled": [],
                "disabled": [],
                "failed": [],
                "cancelled": False,
                "message": "没有检测到任何 MCP 开关变更。",
            }

        config_dict = self.read_config()
        servers = config_dict.get("mcpServers", {})
        if not servers:
            raise ValueError("MCP 配置文件中没有定义 mcpServers")

        changed = []
        enable_targets = []
        disable_targets = []

        for server_name, disabled in disabled_updates.items():
            if server_name not in servers:
                continue
            old_disabled = bool(servers[server_name].get("disabled", False))
            new_disabled = bool(disabled)
            if old_disabled == new_disabled:
                continue
            servers[server_name]["disabled"] = new_disabled
            changed.append(server_name)
            if new_disabled:
                disable_targets.append(server_name)
            else:
                enable_targets.append(server_name)

        if not changed:
            return {
                "saved": False,
                "changed": [],
                "enabled": [],
                "disabled": [],
                "failed": [],
                "cancelled": False,
                "message": "没有检测到任何 MCP 开关变更。",
            }

        self._save_config_dict(config_dict)
        self.server_configs = servers

        if not self._is_running:
            self.start_background()
            return {
                "saved": True,
                "changed": changed,
                "enabled": enable_targets,
                "disabled": disable_targets,
                "failed": [],
                "cancelled": False,
                "message": "配置已保存。由于 MCP 后台未运行，已按最新配置尝试启动。",
            }

        failed = []

        # 串行禁用服务（快速操作）
        for server_name in disable_targets:
            try:
                if self.loop:
                    future = asyncio.run_coroutine_threadsafe(
                        self._disconnect_server(server_name),
                        self.loop,
                    )
                    future.result(timeout=30)
                    if self.console:
                        print_formatted_text(
                            f"[bold yellow]⏹️ 已停用 MCP 服务: '{escape(str(server_name))}'[/bold yellow]"
                        )
                else:
                    failed.append(
                        {
                            "server": server_name,
                            "action": "disable",
                            "error": "MCP event loop 未运行",
                        }
                    )
            except Exception as e:
                failed.append(
                    {"server": server_name, "action": "disable", "error": str(e)}
                )
                log_error_traceback(f"MCP Disable Error [{server_name}]", e)

        # 并行启用服务
        if enable_targets:
            if self.loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._enable_servers_parallel(enable_targets, servers),
                        self.loop,
                    )
                    enable_failed = future.result(timeout=120)
                    failed.extend(enable_failed)
                except Exception as e:
                    for server_name in enable_targets:
                        failed.append(
                            {"server": server_name, "action": "enable", "error": str(e)}
                        )
                    log_error_traceback("MCP Parallel Enable Error", e)
            else:
                for server_name in enable_targets:
                    failed.append(
                        {
                            "server": server_name,
                            "action": "enable",
                            "error": "MCP event loop 未运行",
                        }
                    )

        if failed:
            message = "MCP 开关已保存，但部分增量启停失败。你可以执行 /mcp-restart 进行完整重载。"
        else:
            message = "MCP 开关已保存，并已按变更尝试增量启停服务。"

        return {
            "saved": True,
            "changed": changed,
            "enabled": enable_targets,
            "disabled": disable_targets,
            "failed": failed,
            "cancelled": False,
            "message": message,
        }

    def get_status_info(self) -> dict:
        config_servers = []
        disabled_servers = []
        enabled_config_servers = []
        try:
            config_dict = self.read_config()
            servers = config_dict.get("mcpServers", {})
            config_servers = list(servers.keys())
            disabled_servers = [
                name
                for name, cfg in servers.items()
                if bool(cfg.get("disabled", False))
            ]
            enabled_config_servers = [
                name
                for name, cfg in servers.items()
                if not bool(cfg.get("disabled", False))
            ]
        except Exception:
            pass

        with self._db_lock:
            return {
                "is_running": self._is_running,
                "config_path": str(self.config_path)
                if self.config_path
                else "Not configured",
                "tool_count": len(self._status_tools),
                "servers": list(self.server_configs.keys()),
                "config_servers": config_servers,
                "enabled_config_servers": enabled_config_servers,
                "disabled_servers": disabled_servers,
                "loaded_servers": list(self.clients.keys()),
                "tools": self._status_tools,
            }


GLOBAL_MCP_MANAGER = GlobalMCPManager()
