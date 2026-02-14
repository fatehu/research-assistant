import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("mcp.server.fastmcp", reason="mcp package not installed")

from app.mcp_servers.common import normalize_transport, tool_result_to_payload
from app.mcp_servers.literature_server import LiteratureMCPService
from app.mcp_servers.web_server import WebMCPService
from app.services.agent_tools import ToolResult


class DummyTool:
    def __init__(self, result: ToolResult):
        self.result = result
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_web_mcp_service_wraps_web_search_and_scrape():
    search_tool = DummyTool(ToolResult(success=True, output="search ok", data={"k": 1}))
    scrape_tool = DummyTool(ToolResult(success=True, output="scrape ok", data={"url": "https://example.com"}))
    service = WebMCPService(web_search_tool=search_tool, web_scrape_tool=scrape_tool)

    search_payload = await service.web_search(query="mcp", max_results=3)
    scrape_payload = await service.web_scrape(
        url="https://example.com",
        extract="text",
        selector=".article",
        max_length=1200,
    )

    assert search_payload["success"] is True
    assert search_payload["output"] == "search ok"
    assert search_tool.calls == [{"query": "mcp", "max_results": 3}]

    assert scrape_payload["success"] is True
    assert scrape_payload["output"] == "scrape ok"
    assert scrape_tool.calls == [
        {
            "url": "https://example.com",
            "extract": "text",
            "selector": ".article",
            "max_length": 1200,
        }
    ]


@pytest.mark.asyncio
async def test_literature_mcp_service_wraps_local_literature_tool():
    literature_tool = DummyTool(ToolResult(success=True, output="lit ok", data={"papers": []}))
    service = LiteratureMCPService(literature_tool=literature_tool)

    payload = await service.literature_search(
        query="transformer",
        source="semantic_scholar",
        max_results=7,
        year_start=2020,
        year_end=2024,
    )

    assert payload["success"] is True
    assert payload["output"] == "lit ok"
    assert literature_tool.calls == [
        {
            "query": "transformer",
            "source": "semantic_scholar",
            "max_results": 7,
            "year_start": 2020,
            "year_end": 2024,
        }
    ]


def test_mcp_common_helpers():
    assert normalize_transport("streamable_http") == "streamable-http"
    assert normalize_transport("streamable-http") == "streamable-http"
    assert normalize_transport("sse") == "sse"
    assert normalize_transport("unknown", default="stdio") == "stdio"

    payload = tool_result_to_payload(
        ToolResult(success=False, output="failed", data={"code": 500}, error="http_error")
    )
    assert payload["success"] is False
    assert payload["output"] == "failed"
    assert payload["error"] == "http_error"
    assert payload["data"] == {"code": 500}
    assert payload["execution_time_ms"] == 0.0
    assert payload["output_tokens_estimate"] == 0
    assert payload["truncated"] is False
