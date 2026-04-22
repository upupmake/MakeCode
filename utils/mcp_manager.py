import asyncio
import json
import re
import threading
from pathlib import Path

from fastmcp import Client
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

# 使用安装目录的配置
from init import INSTALL_MAKECODE_DIR
from init import log_error_traceback

mcp_config_path = INSTALL_MAKECODE_DIR / "mcp_config.json"


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

    def list_server_switches(self) -> list:
        config_dict = self.read_config()
        servers = config_dict.get("mcpServers", {})
        result = []
        with self._db_lock:
            loaded_servers = set(self.clients.keys())
        for name, cfg in servers.items():
            disabled = bool(cfg.get("disabled", False))
            result.append(
                {
                    "name": name,
                    "disabled": disabled,
                    "enabled": not disabled,
                    "loaded": name in loaded_servers,
                }
            )
        return result

    def start_background(self):
        if self._is_running:
            return
        if not self.config_path or not self.config_path.exists():
            if self.console:
                print_formatted_text(
                    HTML(
                        f"<ansiyellow><b>⚠️ MCP 配置文件不存在，已跳过加载。\n   路径: {self.config_path}</b></ansiyellow>"
                    )
                )
            return

        try:
            config_dict = self._load_config_dict()
            self.server_configs = config_dict.get("mcpServers", {})
        except Exception as e:
            log_error_traceback("MCP Config Load Error", e)
            if self.console:
                print_formatted_text(
                    HTML(f"<ansired><b>⚠️ Failed to load MCP config: {e}</b></ansired>")
                )
            return

        if not self.server_configs:
            if self.console:
                print_formatted_text(
                    HTML(
                        "<ansiyellow><b>⚠️ MCP 配置文件中没有定义 mcpServers</b></ansiyellow>"
                    )
                )
            return

        if self.console:
            names = ", ".join(self.server_configs.keys())
            print_formatted_text(
                HTML(
                    f"<ansicyan>🔄 识别到 {len(self.server_configs)} 个 MCP 服务 ({names})</ansicyan>\n"
                )
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
                    HTML(f"<ansired><b>⚠️ MCP Background Loop Error: {e}</b></ansired>")
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

    async def _connect_server(self, server_name: str, cfg: dict) -> bool:
        if cfg.get("disabled", False):
            if self.console:
                print_formatted_text(
                    HTML(
                        f"<ansiyellow><b>⚠️ MCP 服务 '{server_name}' 已被标记为禁用，跳过加载。</b></ansiyellow>"
                    )
                )
            return False

        if server_name in self.clients:
            return True

        client = Client({"mcpServers": {server_name: cfg}})
        try:
            # 独立管理连接生命周期
            await client.__aenter__()

            raw_tools = await client.list_tools()
            server_tools = []
            server_status_tools = []

            if self.console:
                print_formatted_text(
                    HTML(
                        f"<ansigreen>✅ 成功连接 MCP 服务: <b>'{server_name}'</b> (已加载 {len(raw_tools)} 个工具)</ansigreen>"
                    )
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
            return True
        except Exception as e:
            with self._db_lock:
                self.clients.pop(server_name, None)
                self._server_tools.pop(server_name, None)
                self._server_status_tools.pop(server_name, None)
                self._rebuild_global_registry_locked()
            log_error_traceback(f"MCP Server Load Error [{server_name}]", e)
            if self.console:
                print_formatted_text(
                    HTML(
                        f"\r<ansired><b>⚠️ 无法加载 MCP 服务 '{server_name}': {e}</b></ansired>"
                    )
                )
            return False

    async def _disconnect_server(self, server_name: str):
        client = None
        with self._db_lock:
            client = self.clients.pop(server_name, None)
            self._server_tools.pop(server_name, None)
            self._server_status_tools.pop(server_name, None)
            self._rebuild_global_registry_locked()

        if client and hasattr(client, "__aexit__"):
            try:
                # 优雅断开底层的清理
                await client.__aexit__(None, None, None)
            except Exception as e:
                log_error_traceback(f"MCP Server Close Error [{server_name}]", e)

    async def _async_lifecycle(self):
        try:
            for server_name, cfg in self.server_configs.items():
                await self._connect_server(server_name, cfg)

            with self._db_lock:
                self._rebuild_global_registry_locked()

            await self._stop_event.wait()
        except Exception as e:
            log_error_traceback("MCP Async Lifecycle Loop Error", e)
            if self.console:
                print_formatted_text(
                    HTML(f"\r<ansired><b>⚠️ MCP 后台连接异常断开: {e}</b></ansired>")
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

    def get_tools(self) -> list:
        with self._db_lock:
            return list(self._mcp_tools)

    def get_handlers(self) -> dict:
        with self._db_lock:
            return dict(self._mcp_handlers)

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
                HTML(
                    "\n<ansicyan><b>🔄 正在重新加载 MCP 配置并重启后台服务...</b></ansicyan>"
                )
            )

        self.start_background()

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
                            HTML(
                                f"<ansiyellow><b>⏹️ 已停用 MCP 服务: '{server_name}'</b></ansiyellow>"
                            )
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

        for server_name in enable_targets:
            try:
                if self.loop:
                    future = asyncio.run_coroutine_threadsafe(
                        self._connect_server(server_name, servers[server_name]),
                        self.loop,
                    )
                    ok = future.result(timeout=60)
                    if ok and self.console:
                        print_formatted_text(
                            HTML(
                                f"<ansigreen><b>✅ 已启用 MCP 服务: '{server_name}'</b></ansigreen>"
                            )
                        )
                    if not ok:
                        failed.append(
                            {
                                "server": server_name,
                                "action": "enable",
                                "error": "连接失败",
                            }
                        )
                else:
                    failed.append(
                        {
                            "server": server_name,
                            "action": "enable",
                            "error": "MCP event loop 未运行",
                        }
                    )
            except Exception as e:
                failed.append(
                    {"server": server_name, "action": "enable", "error": str(e)}
                )
                log_error_traceback(f"MCP Enable Error [{server_name}]", e)

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
