from rich.console import Console

from system.commands import COMMAND_DESCRIPTIONS, CommandHandler
from utils.mcp_manager import GlobalMCPManager


class DummyMcpManager:
    def add_server_config(self, server_name, cfg):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def delete_server_config(self, server_name):
        return {"saved": True, "server": server_name, "failed": [], "message": "ok"}

    def get_status_info(self):
        return {"config_path": "test"}


def make_handler():
    return CommandHandler(
        Console(),
        DummyMcpManager(),
        skill_loader=None,
        get_system_prompt_fn=lambda: "",
        save_checkpoint_fn=lambda history, checkpoint: checkpoint,
        load_checkpoint_fn=lambda path: None,
        list_checkpoints_fn=lambda: [],
        auto_compact_fn=lambda *args, **kwargs: None,
    )


def test_mcp_help_command_registered():
    assert "/mcp-help" in COMMAND_DESCRIPTIONS
    assert "/mcp-add" in COMMAND_DESCRIPTIONS["/mcp-help"]


def test_parse_mcp_add_stdio_repeated_args_and_env():
    handler = make_handler()

    name, cfg = handler._parse_mcp_add_config(
        "/mcp-add fs --command npx --arg -y --arg @modelcontextprotocol/server-filesystem "
        "--arg D:/Work --env NODE_ENV=test --keep-alive false"
    )

    assert name == "fs"
    assert cfg == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/Work"],
        "env": {"NODE_ENV": "test"},
        "keep_alive": False,
        "transport": "stdio",
    }


def test_parse_mcp_add_remote_headers_and_http_normalization():
    handler = make_handler()

    name, cfg = handler._parse_mcp_add_config(
        "/mcp-add api --url https://example.com/mcp --transport http "
        "--header X-Api-Key=secret headers.Authorization=Bearer-token --auth oauth --timeout 30000"
    )

    assert name == "api"
    assert cfg["transport"] == "streamable-http"
    assert cfg["headers"] == {
        "X-Api-Key": "secret",
        "Authorization": "Bearer-token",
    }
    assert cfg["auth"] == "oauth"
    assert cfg["timeout"] == 30000


def test_parse_mcp_add_requires_command_or_url():
    handler = make_handler()

    try:
        handler._parse_mcp_add_config("/mcp-add missing")
    except ValueError as exc:
        assert "--command 或 --url" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_mcp_manager_rejects_duplicate_server_name(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"

    manager.add_server_config(
        "api",
        {"url": "https://example.com/mcp", "transport": "streamable-http", "disabled": True},
    )

    try:
        manager.add_server_config(
            "api",
            {"url": "https://example.com/other", "transport": "streamable-http", "disabled": True},
        )
    except ValueError as exc:
        assert "请先执行 /mcp-delete api" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_mcp_manager_add_and_delete_disabled_config(tmp_path):
    manager = GlobalMCPManager()
    manager.config_path = tmp_path / "mcp_config.json"

    add_result = manager.add_server_config(
        "disabled-api",
        {"url": "https://example.com/mcp", "transport": "streamable-http", "disabled": True},
    )
    assert add_result["saved"] is True
    assert add_result["enabled"] == []
    assert "disabled-api" in manager.read_config()["mcpServers"]

    delete_result = manager.delete_server_config("disabled-api")
    assert delete_result["saved"] is True
    assert "disabled-api" not in manager.read_config()["mcpServers"]
