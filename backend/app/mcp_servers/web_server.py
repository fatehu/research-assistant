"""Internal MCP server for web capabilities."""

import os
from typing import Any, Dict, Optional

from loguru import logger
from mcp.server.fastmcp import FastMCP

from app.mcp_servers.common import normalize_transport, read_host_port, tool_result_to_payload
from app.services.agent_tools import ToolResult, WebSearchTool
from app.services.notebook_tools import WebScrapeTool


class WebMCPService:
    """Wrap existing web tools for MCP exposure."""

    def __init__(
        self,
        web_search_tool: Optional[Any] = None,
        web_scrape_tool: Optional[Any] = None,
    ) -> None:
        self.web_search_tool = web_search_tool or WebSearchTool()
        self.web_scrape_tool = web_scrape_tool or WebScrapeTool()

    async def web_search(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        result: ToolResult = await self.web_search_tool.execute(
            query=query,
            max_results=max_results,
        )
        return tool_result_to_payload(result)

    async def web_scrape(
        self,
        url: str,
        extract: str = "text",
        selector: Optional[str] = None,
        max_length: int = 5000,
    ) -> Dict[str, Any]:
        result: ToolResult = await self.web_scrape_tool.execute(
            url=url,
            extract=extract,
            selector=selector,
            max_length=max_length,
        )
        return tool_result_to_payload(result)


def create_web_mcp_app(service: Optional[WebMCPService] = None) -> FastMCP:
    host, port = read_host_port("MCP_WEB", default_port=8091)
    app = FastMCP(
        name="web-server",
        host=host,
        port=port,
        stateless_http=True,
    )
    svc = service or WebMCPService()

    @app.tool(name="web_search", description="Search the internet for current information.")
    async def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
        return await svc.web_search(query=query, max_results=max_results)

    @app.tool(name="web_scrape", description="Fetch webpage content with text/html/link extraction.")
    async def web_scrape(
        url: str,
        extract: str = "text",
        selector: Optional[str] = None,
        max_length: int = 5000,
    ) -> Dict[str, Any]:
        return await svc.web_scrape(
            url=url,
            extract=extract,
            selector=selector,
            max_length=max_length,
        )

    return app


def run() -> None:
    app = create_web_mcp_app()
    transport = normalize_transport(
        os.getenv("MCP_WEB_TRANSPORT", "streamable-http"),
        default="streamable-http",
    )
    logger.info(f"[MCP-Web] starting transport={transport} host={app.settings.host} port={app.settings.port}")
    app.run(transport=transport)


if __name__ == "__main__":
    run()
