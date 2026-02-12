import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.mcp.config import load_mcp_server_configs
from app.services.mcp.config import (
    load_mcp_server_configs_from_file,
    mcp_server_configs_to_claude_desktop_config,
    parse_mcp_server_configs_payload,
    save_mcp_config_payload_to_file,
)


def test_load_mcp_server_configs_valid_json():
    raw = """
    [
      {
        "name": "fetch",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"]
      }
    ]
    """
    configs = load_mcp_server_configs(raw, 15)
    assert len(configs) == 1
    assert configs[0].name == "fetch"
    assert configs[0].transport == "stdio"
    assert configs[0].timeout_seconds == 15


def test_load_mcp_server_configs_invalid_json_returns_empty():
    configs = load_mcp_server_configs("{not-json}", 20)
    assert configs == []


def test_parse_claude_desktop_payload():
    payload = {
        "mcpServers": {
            "exa": {
                "type": "http",
                "url": "https://mcp.exa.ai/mcp",
                "headers": {"Authorization": "Bearer ${EXA_API_KEY}"},
            }
        }
    }
    configs = parse_mcp_server_configs_payload(payload, 20)
    assert len(configs) == 1
    assert configs[0].name == "exa"
    assert configs[0].transport == "streamable_http"
    assert configs[0].url == "https://mcp.exa.ai/mcp"


def test_load_mcp_server_configs_from_file(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        """
        {
          "mcpServers": {
            "firecrawl": {
              "type": "http",
              "url": "https://mcp.firecrawl.dev/v1"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    configs = load_mcp_server_configs("", 30, config_path=str(path))
    assert len(configs) == 1
    assert configs[0].name == "firecrawl"
    assert configs[0].transport == "streamable_http"

    configs_direct = load_mcp_server_configs_from_file(str(path), 30)
    assert len(configs_direct) == 1


def test_save_and_serialize_claude_desktop_config(tmp_path):
    payload = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
            }
        }
    }
    path = tmp_path / "saved_mcp.json"
    save_mcp_config_payload_to_file(str(path), payload)
    assert path.exists()

    configs = load_mcp_server_configs_from_file(str(path), 20)
    assert len(configs) == 1
    claude = mcp_server_configs_to_claude_desktop_config(configs)
    assert "mcpServers" in claude
    assert "filesystem" in claude["mcpServers"]
