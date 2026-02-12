import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.mcp.config import load_mcp_server_configs


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
