import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.mcp.templates import get_mcp_server_templates


def test_mcp_templates_basic_shape():
    templates = get_mcp_server_templates()
    assert len(templates) >= 5

    ids = {item["id"] for item in templates}
    assert "filesystem" in ids
    assert "postgres" in ids
    assert "firecrawl" in ids

    for item in templates:
        assert isinstance(item.get("title"), str) and item["title"]
        assert isinstance(item.get("claude_desktop_config"), dict)
        assert "mcpServers" in item["claude_desktop_config"]
