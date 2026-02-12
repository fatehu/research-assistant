"""MCP client manager for remote tool discovery and invocation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import MCPServerConfig
from .server_manager import MCPServerManager


@dataclass
class MCPToolSchema:
    """Normalized MCP tool schema used by local registry."""

    server_name: str
    tool_name: str
    qualified_name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Optional[Dict[str, Any]] = None


@dataclass
class MCPCallResult:
    """MCP tool call result normalized for local adapter."""

    success: bool
    output: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class MCPClientManager:
    """Manage MCP server calls and cache discovered remote tools."""

    def __init__(
        self,
        server_manager: MCPServerManager,
        *,
        tool_prefix: str = "mcp",
    ) -> None:
        self.server_manager = server_manager
        self.tool_prefix = tool_prefix or "mcp"
        self._tools_by_qualified_name: Dict[str, MCPToolSchema] = {}
        self._aliases: Dict[str, List[str]] = {}
        self._discovered = False
        self._import_error: Optional[str] = None

    def enabled(self) -> bool:
        return bool(self.server_manager.list_enabled_configs()) and self._sdk_available()

    def _sdk_available(self) -> bool:
        try:
            from mcp import ClientSession  # noqa: F401
            from mcp.client.sse import sse_client  # noqa: F401
            from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: F401
            from mcp.client.streamable_http import streamablehttp_client  # noqa: F401
            return True
        except Exception as exc:
            self._import_error = str(exc)
            return False

    async def discover_tools(self, *, force_refresh: bool = False) -> List[MCPToolSchema]:
        """Discover tools from all enabled MCP servers."""

        if not self.enabled():
            if self._import_error:
                logger.warning(f"[MCP] SDK unavailable: {self._import_error}")
            return []

        if self._discovered and not force_refresh:
            return list(self._tools_by_qualified_name.values())

        self._tools_by_qualified_name.clear()
        self._aliases.clear()

        for server in self.server_manager.list_enabled_configs():
            try:
                schemas = await self._discover_server_tools(server)
                for schema in schemas:
                    self._register_tool(schema)
                self.server_manager.update_status(
                    server.name,
                    reachable=True,
                    discovered_tools=len(schemas),
                    error=None,
                )
            except Exception as exc:  # pragma: no cover - network/process failures
                logger.warning(f"[MCP] discover failed on server={server.name}: {exc}")
                self.server_manager.update_status(
                    server.name,
                    reachable=False,
                    discovered_tools=0,
                    error=str(exc),
                )

        self._discovered = True
        return list(self._tools_by_qualified_name.values())

    def list_tool_schemas(self) -> List[MCPToolSchema]:
        return list(self._tools_by_qualified_name.values())

    def resolve_tool_schema(self, name: str) -> Optional[MCPToolSchema]:
        """Resolve both qualified name and unique short alias."""

        if name in self._tools_by_qualified_name:
            return self._tools_by_qualified_name[name]

        aliases = self._aliases.get(name) or []
        if len(aliases) == 1:
            return self._tools_by_qualified_name.get(aliases[0])
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPCallResult:
        """Call one MCP tool by name."""

        if not self.enabled():
            return MCPCallResult(
                success=False,
                output="MCP 未启用或 MCP SDK 不可用",
                error="mcp_unavailable",
            )

        if not self._discovered:
            await self.discover_tools()

        schema = self.resolve_tool_schema(tool_name)
        if not schema:
            return MCPCallResult(
                success=False,
                output=f"未找到 MCP 工具: {tool_name}",
                error="tool_not_found",
            )

        server = self.server_manager.get_config(schema.server_name)
        if not server:
            return MCPCallResult(
                success=False,
                output=f"MCP Server 配置不存在: {schema.server_name}",
                error="server_not_found",
            )

        try:
            result = await self._call_server_tool(server, schema.tool_name, arguments)
            self.server_manager.update_status(
                server.name,
                reachable=True,
                discovered_tools=self._count_server_tools(server.name),
                error=None,
            )
            return result
        except Exception as exc:  # pragma: no cover - network/process failures
            logger.warning(f"[MCP] call failed server={server.name} tool={schema.tool_name}: {exc}")
            self.server_manager.update_status(
                server.name,
                reachable=False,
                error=str(exc),
            )
            return MCPCallResult(
                success=False,
                output=f"MCP 工具调用失败: {exc}",
                error="mcp_call_failed",
            )

    def _register_tool(self, schema: MCPToolSchema) -> None:
        self._tools_by_qualified_name[schema.qualified_name] = schema
        aliases = self._aliases.setdefault(schema.tool_name, [])
        aliases.append(schema.qualified_name)

    def _count_server_tools(self, server_name: str) -> int:
        return sum(1 for schema in self._tools_by_qualified_name.values() if schema.server_name == server_name)

    async def _discover_server_tools(self, server: MCPServerConfig) -> List[MCPToolSchema]:
        async def _handler(session: Any) -> List[MCPToolSchema]:
            list_result = await session.list_tools()
            tools = getattr(list_result, "tools", []) or []
            schemas: List[MCPToolSchema] = []
            for tool in tools:
                tool_name = str(getattr(tool, "name", "") or "").strip()
                if not tool_name:
                    continue
                qualified_name = f"{self.tool_prefix}.{server.name}.{tool_name}"
                input_schema = getattr(tool, "inputSchema", None) or {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
                output_schema = getattr(tool, "outputSchema", None)
                description = str(getattr(tool, "description", "") or "").strip()
                schemas.append(
                    MCPToolSchema(
                        server_name=server.name,
                        tool_name=tool_name,
                        qualified_name=qualified_name,
                        description=description,
                        input_schema=input_schema,
                        output_schema=output_schema,
                    )
                )
            return schemas

        return await self._with_session(server, _handler)

    async def _call_server_tool(
        self,
        server: MCPServerConfig,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> MCPCallResult:
        async def _handler(session: Any) -> MCPCallResult:
            call_result = await session.call_tool(tool_name, arguments=arguments or {})
            return self._normalize_call_result(call_result)

        return await self._with_session(server, _handler)

    async def _with_session(self, server: MCPServerConfig, handler: Any) -> Any:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"MCP stdio server '{server.name}' missing command")

            params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=server.env or None,
                cwd=server.cwd or None,
            )

            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await handler(session)

        if server.transport == "sse":
            if not server.url:
                raise ValueError(f"MCP sse server '{server.name}' missing url")

            async with sse_client(
                server.url,
                headers=server.headers or None,
                timeout=float(server.timeout_seconds),
                sse_read_timeout=float(server.sse_read_timeout_seconds),
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await handler(session)

        if server.transport == "streamable_http":
            if not server.url:
                raise ValueError(f"MCP streamable_http server '{server.name}' missing url")

            async with streamablehttp_client(
                server.url,
                headers=server.headers or None,
                timeout=timedelta(seconds=server.timeout_seconds),
                sse_read_timeout=timedelta(seconds=server.sse_read_timeout_seconds),
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await handler(session)

        raise ValueError(f"Unsupported transport: {server.transport}")

    @staticmethod
    def _normalize_call_result(call_result: Any) -> MCPCallResult:
        content_blocks = getattr(call_result, "content", None) or []
        is_error = bool(getattr(call_result, "isError", False))
        structured = getattr(call_result, "structuredContent", None)

        text_parts: List[str] = []
        normalized_blocks: List[Dict[str, Any]] = []

        for block in content_blocks:
            block_type = str(getattr(block, "type", "") or "")
            normalized_blocks.append(
                {
                    "type": block_type,
                    "value": MCPClientManager._block_to_serializable(block),
                }
            )

            if block_type == "text" and hasattr(block, "text"):
                text_parts.append(str(getattr(block, "text", "")))
            else:
                fallback = MCPClientManager._block_to_serializable(block)
                text_parts.append(json.dumps(fallback, ensure_ascii=False))

        output = "\n".join(part for part in text_parts if part).strip()
        if not output and structured is not None:
            output = json.dumps(structured, ensure_ascii=False)
        if not output:
            output = "MCP 工具调用完成" if not is_error else "MCP 工具调用返回错误"

        data = {
            "content_blocks": normalized_blocks,
            "structured_content": structured,
            "is_error": is_error,
        }

        return MCPCallResult(
            success=not is_error,
            output=output,
            data=data,
            error="mcp_tool_error" if is_error else None,
        )

    @staticmethod
    def _block_to_serializable(block: Any) -> Any:
        if hasattr(block, "model_dump"):
            try:
                return block.model_dump()
            except Exception:
                pass
        if hasattr(block, "dict"):
            try:
                return block.dict()
            except Exception:
                pass
        if isinstance(block, dict):
            return block
        return str(block)
