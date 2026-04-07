import asyncio
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from fastmcp import Client
from init import log_error_traceback, llm_client


class GlobalMCPManager:
    def __init__(self):
        self.config_path = None
        self.console = None
        self.loop = None
        self.thread = None
        self._stop_event = None

        self.server_configs = {}
        self.clients = {}

        self._mcp_tools = []
        self._mcp_handlers = {}
        self._status_tools = []  # 存储带 provider 信息的原生结构，用于 view 命令

        self._db_lock = threading.Lock()
        self._is_running = False

    def initialize(self, config_path: Path, console):
        self.config_path = config_path
        self.console = console

    def start_background(self):
        if not self.config_path or not self.config_path.exists():
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config_dict = json.load(f)
                self.server_configs = config_dict.get("mcpServers", {})
        except Exception as e:
            log_error_traceback("MCP Config Load Error", e)
            if self.console:
                self.console.print(
                    f"[bold red] ⚠️ Failed to load MCP config: {e}[/bold red]"
                )
            return

        if not self.server_configs:
            if self.console:
                self.console.print(
                    "[bold yellow] ⚠️ MCP 配置文件中没有定义 mcpServers[/bold yellow]"
                )
            return

        self.loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        self._is_running = True

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._async_lifecycle())
        except Exception as e:
            log_error_traceback("MCP Background Loop Error", e)
            if self.console:
                self.console.print(
                    f"[bold red] ⚠️ MCP Background Loop Error: {e}[/bold red]"
                )
        finally:
            self._is_running = False

    async def _async_lifecycle(self):
        mcp_raw_schemas = []
        handlers = {}
        status_tools = []
        loaded_servers = []

        try:
            async with AsyncExitStack() as stack:
                # 逐个加载 server，实现真正的隔离和精确识别
                for server_name, cfg in self.server_configs.items():
                    client = Client({"mcpServers": {server_name: cfg}})
                    try:
                        await stack.enter_async_context(client)
                        self.clients[server_name] = client

                        raw_tools = await client.list_tools()
                        loaded_servers.append(server_name)

                        for t in raw_tools:
                            tool_name = f"{server_name}_{t.name}"
                            desc = (
                                t.description
                                or f"MCP Tool: {t.name} from {server_name}"
                            )

                            mcp_raw_schemas.append(
                                {
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "description": desc,
                                        "parameters": t.inputSchema or {},
                                    },
                                }
                            )

                            status_tools.append(
                                {
                                    "name": tool_name,
                                    "description": desc,
                                    "provider": server_name,
                                }
                            )

                            def make_handler(
                                c=client, original_name=t.name, t_name=tool_name
                            ):
                                def handler(**kwargs):
                                    try:
                                        future = asyncio.run_coroutine_threadsafe(
                                            c.call_tool(original_name, kwargs),
                                            self.loop,
                                        )
                                        result = future.result()

                                        # 处理 FastMCP 默认返回结构，提取纯文本
                                        if hasattr(result, "content") and isinstance(
                                            result.content, list
                                        ):
                                            texts = [
                                                c.text
                                                for c in result.content
                                                if hasattr(c, "text")
                                            ]
                                            if texts:
                                                return "\n".join(texts)
                                            return str(result.content)

                                        if hasattr(result, "data"):
                                            return str(result.data)
                                        elif hasattr(result, "content"):
                                            return str(result.content)
                                        return str(result)
                                    except Exception as ex:
                                        log_error_traceback(
                                            f"MCP Tool Execution Error [{t_name}]",
                                            ex,
                                        )
                                        return f"Error executing tool '{t_name}': {ex}"

                                return handler

                            handlers[tool_name] = make_handler()

                    except Exception as e:
                        log_error_traceback(f"MCP Server Load Error [{server_name}]", e)
                        if self.console:
                            self.console.print(
                                f"\r[bold red] ⚠️ 无法加载 MCP 服务 '{server_name}': {e}[/bold red]"
                            )

                with self._db_lock:
                    self._mcp_tools = mcp_raw_schemas
                    self._mcp_handlers = handlers
                    self._status_tools = status_tools

                # Keep all successfully connected clients alive
                if self.clients:
                    await self._stop_event.wait()

        except Exception as e:
            log_error_traceback("MCP Async Lifecycle Stack Error", e)
            if self.console:
                self.console.print(
                    f"\r[bold red] ⚠️ MCP 后台连接异常断开: {e}[/bold red]"
                )

    def get_tools(self) -> list:
        with self._db_lock:
            return list(self._mcp_tools)

    def get_handlers(self) -> dict:
        with self._db_lock:
            return dict(self._mcp_handlers)

    def stop(self):
        if self._is_running and self.loop:
            self.loop.call_soon_threadsafe(self._stop_event.set)
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)

        with self._db_lock:
            self._mcp_tools = []
            self._mcp_handlers = {}
            self._status_tools = []
            self.clients = {}
            self._is_running = False

    def restart(self, config_path: Path = None):
        self.stop()
        if config_path:
            self.config_path = config_path

        if self.console:
            self.console.print("[dim] 🔄 重载 MCP 配置并启动后台连接...[/dim]")

        self.start_background()

    def get_status_info(self) -> dict:
        with self._db_lock:
            return {
                "is_running": self._is_running,
                "config_path": str(self.config_path)
                if self.config_path
                else "Not configured",
                "tool_count": len(self._status_tools),
                "servers": list(self.server_configs.keys()),
                "tools": self._status_tools,
            }


GLOBAL_MCP_MANAGER = GlobalMCPManager()
