"""Internal MCP server for literature search."""

import os
from typing import Any, Dict, Optional

from loguru import logger
from mcp.server.fastmcp import FastMCP

from app.mcp_servers.common import normalize_transport, read_host_port, tool_result_to_payload
from app.services.agent_tools import LiteratureSearchTool, ToolResult


class LiteratureMCPService:
    """Wrap existing literature tool for MCP exposure."""

    def __init__(self, literature_tool: Optional[Any] = None) -> None:
        self.literature_tool = literature_tool or LiteratureSearchTool()

    async def literature_search(
        self,
        query: str,
        source: str = "semantic_scholar",
        max_results: int = 5,
        year_start: Optional[int] = None,
        year_end: Optional[int] = None,
    ) -> Dict[str, Any]:
        result: ToolResult = await self.literature_tool.execute(
            query=query,
            source=source,
            max_results=max_results,
            year_start=year_start,
            year_end=year_end,
        )
        return tool_result_to_payload(result)


def create_literature_mcp_app(service: Optional[LiteratureMCPService] = None) -> FastMCP:
    host, port = read_host_port("MCP_LITERATURE", default_port=8092)
    app = FastMCP(
        name="literature-server",
        host=host,
        port=port,
        stateless_http=True,
    )
    svc = service or LiteratureMCPService()

    @app.tool(
        name="literature_search",
        description="Search papers from semantic_scholar/arxiv with optional year filters.",
    )
    async def literature_search(
        query: str,
        source: str = "semantic_scholar",
        max_results: int = 5,
        year_start: Optional[int] = None,
        year_end: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await svc.literature_search(
            query=query,
            source=source,
            max_results=max_results,
            year_start=year_start,
            year_end=year_end,
        )

    return app


def run() -> None:
    app = create_literature_mcp_app()
    transport = normalize_transport(
        os.getenv("MCP_LITERATURE_TRANSPORT", "streamable-http"),
        default="streamable-http",
    )
    logger.info(
        f"[MCP-Literature] starting transport={transport} host={app.settings.host} port={app.settings.port}"
    )
    app.run(transport=transport)


if __name__ == "__main__":
    run()
