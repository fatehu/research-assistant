"""MCP client manager for remote tool discovery and invocation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

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
            result = await self._call_server_tool(server, schema, arguments)
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
        schema: MCPToolSchema,
        arguments: Dict[str, Any],
    ) -> MCPCallResult:
        async def _handler(session: Any) -> MCPCallResult:
            call_result = await session.call_tool(schema.tool_name, arguments=arguments or {})
            return self._normalize_call_result(
                call_result,
                schema=schema,
                arguments=arguments,
            )

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

    @classmethod
    def _normalize_call_result(
        cls,
        call_result: Any,
        *,
        schema: Optional[MCPToolSchema] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> MCPCallResult:
        content_blocks = getattr(call_result, "content", None) or []
        is_error = bool(getattr(call_result, "isError", False))
        structured = cls._block_to_serializable(getattr(call_result, "structuredContent", None))

        text_parts: List[str] = []
        normalized_blocks: List[Dict[str, Any]] = []

        for block in content_blocks:
            block_type = str(getattr(block, "type", "") or "")
            normalized_blocks.append(
                {
                    "type": block_type,
                    "value": cls._block_to_serializable(block),
                }
            )

            if block_type == "text" and hasattr(block, "text"):
                text_parts.append(str(getattr(block, "text", "")))
            else:
                fallback = cls._block_to_serializable(block)
                text_parts.append(json.dumps(fallback, ensure_ascii=False))

        outer_output = "\n".join(part for part in text_parts if part).strip()
        tool_payload = cls._extract_embedded_tool_payload(structured, outer_output)
        embedded_data = cls._coerce_mapping(tool_payload.get("data")) if tool_payload else {}
        embedded_output = str(tool_payload.get("output") or "").strip() if tool_payload else ""
        embedded_error = str(tool_payload.get("error") or "").strip() if tool_payload else ""
        embedded_success = tool_payload.get("success") if isinstance(tool_payload, dict) else None

        normalized_payload = cls._normalize_enrichment_payload(
            schema=schema,
            arguments=arguments or {},
            raw_data=embedded_data,
            structured=structured,
            fallback_output=embedded_output or outer_output,
        )
        data = dict(embedded_data)
        data.update(normalized_payload)
        data["content_blocks"] = normalized_blocks
        data["is_error"] = is_error

        provider = str(
            data.get("provider")
            or (data.get("provenance") or {}).get("provider")
            or getattr(schema, "server_name", "")
            or ""
        ).strip()
        provider_route = str(
            data.get("provider_route")
            or (data.get("provenance") or {}).get("provider_route")
            or getattr(schema, "qualified_name", "")
            or ""
        ).strip()
        tool_kind = cls._infer_tool_kind(schema=schema, raw_data=embedded_data, structured=structured)
        provenance = cls._coerce_mapping(data.get("provenance"))
        provenance.update(
            {
                "source": "mcp",
                "execution_mode": str(provenance.get("execution_mode") or "direct"),
                "provider": provider or str(provenance.get("provider") or "").strip(),
                "provider_route": provider_route or str(provenance.get("provider_route") or "").strip(),
                "remote_server_name": str(
                    provenance.get("remote_server_name") or getattr(schema, "server_name", "") or ""
                ).strip(),
                "remote_tool_name": str(
                    provenance.get("remote_tool_name") or getattr(schema, "tool_name", "") or ""
                ).strip(),
                "qualified_tool_name": str(
                    provenance.get("qualified_tool_name") or getattr(schema, "qualified_name", "") or ""
                ).strip(),
                "normalization_version": "guided_reading_v1",
            }
        )
        if tool_kind:
            provenance["tool_kind"] = tool_kind
        data["provenance"] = provenance
        if provider:
            data["provider"] = provider
        if provider_route:
            data["provider_route"] = provider_route
        if tool_kind:
            data["tool_kind"] = tool_kind
        data["normalization_version"] = "guided_reading_v1"

        output = embedded_output or outer_output
        if not output:
            output = str(
                data.get("markdown")
                or data.get("text")
                or data.get("content")
                or ""
            ).strip()
        if not output and structured is not None:
            output = json.dumps(structured, ensure_ascii=False)
        if not output:
            output = "MCP 工具调用完成" if not is_error else "MCP 工具调用返回错误"

        return MCPCallResult(
            success=(bool(embedded_success) if embedded_success is not None else not is_error),
            output=output,
            data=data,
            error=embedded_error or ("mcp_tool_error" if is_error else None),
        )

    @staticmethod
    def _coerce_mapping(value: Any) -> Dict[str, Any]:
        if hasattr(value, "model_dump"):
            try:
                value = value.model_dump()
            except (TypeError, ValueError):
                value = None
        elif hasattr(value, "dict"):
            try:
                value = value.dict()
            except (TypeError, ValueError):
                value = None
        if isinstance(value, dict):
            return dict(value)
        return {}

    @staticmethod
    def _parse_json_object(value: Any) -> Dict[str, Any]:
        text = str(value or "").strip()
        if not text or text[:1] not in {"{", "["}:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @classmethod
    def _extract_embedded_tool_payload(
        cls,
        structured: Any,
        output: str,
    ) -> Dict[str, Any]:
        structured_mapping = cls._coerce_mapping(structured)
        if cls._looks_like_tool_payload(structured_mapping):
            return structured_mapping
        parsed_output = cls._parse_json_object(output)
        if cls._looks_like_tool_payload(parsed_output):
            return parsed_output
        return {}

    @staticmethod
    def _looks_like_tool_payload(mapping: Dict[str, Any]) -> bool:
        return bool(mapping) and any(key in mapping for key in ("success", "output", "data", "error"))

    @classmethod
    def _infer_tool_kind(
        cls,
        *,
        schema: Optional[MCPToolSchema],
        raw_data: Dict[str, Any],
        structured: Any,
    ) -> str:
        name_parts = " ".join(
            [
                str(getattr(schema, "qualified_name", "") or ""),
                str(getattr(schema, "tool_name", "") or ""),
                str(getattr(schema, "description", "") or ""),
            ]
        ).lower()
        if any(token in name_parts for token in ("search", "query", "brave", "tavily", "exa")):
            return "web_search"
        if any(token in name_parts for token in ("scrape", "crawl", "fetch", "extract", "browser")):
            return "web_scrape"

        candidate_mappings = [
            cls._coerce_mapping(raw_data.get("structured_content")),
            cls._coerce_mapping(raw_data),
            cls._coerce_mapping(structured),
        ]
        if any(isinstance(mapping.get("results"), list) for mapping in candidate_mappings if mapping):
            return "web_search"
        if any(
            any(mapping.get(key) for key in ("markdown", "text", "content", "html", "metadata"))
            for mapping in candidate_mappings
            if mapping
        ):
            return "web_scrape"
        return ""

    @classmethod
    def _normalize_enrichment_payload(
        cls,
        *,
        schema: Optional[MCPToolSchema],
        arguments: Dict[str, Any],
        raw_data: Dict[str, Any],
        structured: Any,
        fallback_output: str,
    ) -> Dict[str, Any]:
        tool_kind = cls._infer_tool_kind(schema=schema, raw_data=raw_data, structured=structured)
        if tool_kind == "web_search":
            return cls._normalize_search_payload(
                schema=schema,
                arguments=arguments,
                raw_data=raw_data,
                structured=structured,
                fallback_output=fallback_output,
            )
        if tool_kind == "web_scrape":
            return cls._normalize_scrape_payload(
                schema=schema,
                arguments=arguments,
                raw_data=raw_data,
                structured=structured,
                fallback_output=fallback_output,
            )

        normalized_structured = cls._coerce_mapping(raw_data.get("structured_content"))
        if not normalized_structured:
            normalized_structured = cls._coerce_mapping(structured)
        payload: Dict[str, Any] = {}
        if normalized_structured:
            payload["structured_content"] = normalized_structured
        return payload

    @staticmethod
    def _extract_first_list(candidates: List[Dict[str, Any]], keys: List[str]) -> List[Any]:
        for mapping in candidates:
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, list):
                    return list(value)
        return []

    @staticmethod
    def _extract_first_mapping(candidates: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Any]:
        for mapping in candidates:
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, dict):
                    return dict(value)
        return {}

    @staticmethod
    def _clean_excerpt(value: Any, *, limit: int = 180) -> str:
        text = " ".join(str(value or "").split()).strip()
        if limit and limit > 0:
            return text[:limit]
        return text

    @classmethod
    def _combine_reader_results(
        cls,
        rows: List[Dict[str, Any]],
        *,
        empty_summary: str,
    ) -> str:
        excerpts: List[str] = []
        for row in rows:
            excerpt = str(
                row.get("reader_excerpt") or row.get("snippet") or row.get("summary") or ""
            ).strip()
            if not excerpt:
                continue
            title = str(row.get("title") or "").strip()
            candidate = excerpt
            if title and title.lower() not in excerpt.lower():
                candidate = f"{title}: {excerpt}"
            if candidate and candidate not in excerpts:
                excerpts.append(candidate)
        return "\n\n".join(excerpts) if excerpts else empty_summary

    @staticmethod
    def _extract_hostname(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        host = str(parsed.netloc or parsed.path or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host

    @classmethod
    def _is_authoritative_public_source(cls, value: Any) -> bool:
        host = cls._extract_hostname(value)
        if not host:
            return False
        trusted_suffixes = (
            ".gov",
            ".edu",
            ".ac.uk",
            "nih.gov",
            "who.int",
            "nature.com",
            "nejm.org",
            "thelancet.com",
            "science.org",
            "usmle.org",
            "pubmed.ncbi.nlm.nih.gov",
        )
        return any(host == suffix or host.endswith(suffix) for suffix in trusted_suffixes)

    @classmethod
    def _summarize_domain_distribution(cls, rows: List[Dict[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        for row in rows:
            domain = cls._extract_hostname(row.get("url") or row.get("href") or row.get("domain") or "")
            if not domain:
                continue
            counts[domain] = counts.get(domain, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [
            {
                "domain": domain,
                "count": count,
                "authoritative": cls._is_authoritative_public_source(domain),
            }
            for domain, count in ranked[:limit]
        ]

    @classmethod
    def _normalize_search_result_item(
        cls,
        item: Any,
        *,
        provider: str = "",
        rank: int = 0,
    ) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        url = str(item.get("url") or item.get("link") or item.get("href") or item.get("source") or "").strip()
        title = str(item.get("title") or item.get("name") or item.get("label") or url or "Public resource").strip()
        snippet = str(
            item.get("snippet")
            or item.get("content")
            or item.get("summary")
            or item.get("description")
            or item.get("text")
            or item.get("answer")
            or ""
        ).strip()
        domain = cls._extract_hostname(url)
        normalized = {
            "rank": int(rank or 0),
            "title": title,
            "url": url,
            "snippet": snippet,
            "reader_excerpt": cls._clean_excerpt(snippet or title, limit=0),
            "type": str(item.get("type") or ("organic" if url else "result")).strip() or "result",
            "display_url": domain or url,
            "domain": domain,
            "is_authoritative_source": bool(domain and cls._is_authoritative_public_source(domain)),
        }
        for key in ("source", "date", "published_at", "score", "provider"):
            if item.get(key) is not None:
                normalized[key] = item.get(key)
        if provider and normalized.get("provider") is None:
            normalized["provider"] = provider
        return normalized

    @classmethod
    def _normalize_public_links(cls, rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            href = str(row.get("url") or row.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            link = {
                "label": str(row.get("title") or row.get("label") or href).strip(),
                "href": href,
            }
            snippet = cls._clean_excerpt(row.get("snippet") or row.get("summary") or "", limit=0)
            if snippet:
                link["snippet"] = snippet
            links.append(link)
        return links

    @classmethod
    def _normalize_search_payload(
        cls,
        *,
        schema: Optional[MCPToolSchema],
        arguments: Dict[str, Any],
        raw_data: Dict[str, Any],
        structured: Any,
        fallback_output: str,
    ) -> Dict[str, Any]:
        parsed_output = cls._parse_json_object(fallback_output)
        candidate_mappings = [
            cls._coerce_mapping(raw_data.get("structured_content")),
            cls._coerce_mapping(raw_data),
            cls._coerce_mapping(structured),
            parsed_output,
        ]
        candidate_mappings = [mapping for mapping in candidate_mappings if mapping]

        raw_results = cls._extract_first_list(
            candidate_mappings,
            ["results", "organic", "items", "search_results"],
        )
        normalized_results = [
            row
            for row in (
                cls._normalize_search_result_item(item, provider="", rank=index)
                for index, item in enumerate(raw_results, start=1)
            )
            if row
        ]

        structured_candidate = cls._coerce_mapping(structured)
        if cls._looks_like_tool_payload(structured_candidate):
            structured_candidate = cls._coerce_mapping(structured_candidate.get("structured_content"))
        structured_payload = cls._coerce_mapping(raw_data.get("structured_content"))
        if not structured_payload:
            structured_payload = structured_candidate
        if not structured_payload and parsed_output:
            structured_payload = dict(parsed_output)

        provider = str(
            raw_data.get("provider")
            or structured_payload.get("provider")
            or getattr(schema, "server_name", "")
            or ""
        ).strip()
        query = str(
            arguments.get("query")
            or arguments.get("q")
            or raw_data.get("query")
            or structured_payload.get("query")
            or parsed_output.get("query")
            or ""
        ).strip()
        total = int(raw_data.get("total") or structured_payload.get("total") or len(normalized_results))
        if provider:
            for row in normalized_results:
                row.setdefault("provider", provider)
        result_types = [
            str(item.get("type") or "").strip()
            for item in normalized_results
            if str(item.get("type") or "").strip()
        ]
        reader_summary = cls._combine_reader_results(
            normalized_results,
            empty_summary=f"No public web results found for '{query}'.",
        )
        domains = cls._summarize_domain_distribution(normalized_results)

        if normalized_results:
            structured_payload["results"] = normalized_results
        if query:
            structured_payload["query"] = query
        if provider:
            structured_payload["provider"] = provider
        structured_payload["total"] = total
        structured_payload["reader_summary"] = reader_summary
        structured_payload["result_types"] = list(dict.fromkeys(result_types))
        if domains:
            structured_payload["domains"] = domains

        payload: Dict[str, Any] = {
            "results": normalized_results,
            "total": total,
            "public_links": cls._normalize_public_links(normalized_results),
            "reader_summary": reader_summary,
            "result_types": list(dict.fromkeys(result_types)),
            "structured_content": structured_payload,
            "source_kind": "public_web_search",
        }
        if query:
            payload["query"] = query
        if provider:
            payload["provider"] = provider
        if domains:
            payload["domains"] = domains
        return payload

    @staticmethod
    def _normalize_scrape_link_item(item: Any) -> Dict[str, str]:
        if not isinstance(item, dict):
            return {}
        href = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
        if not href:
            return {}
        normalized = {
            "label": str(item.get("label") or item.get("title") or href).strip(),
            "href": href,
        }
        snippet = MCPClientManager._clean_excerpt(item.get("snippet") or item.get("summary") or "", limit=0)
        if snippet:
            normalized["snippet"] = snippet
        return normalized

    @classmethod
    def _normalize_scrape_payload(
        cls,
        *,
        schema: Optional[MCPToolSchema],
        arguments: Dict[str, Any],
        raw_data: Dict[str, Any],
        structured: Any,
        fallback_output: str,
    ) -> Dict[str, Any]:
        parsed_output = cls._parse_json_object(fallback_output)
        structured_candidate = cls._coerce_mapping(structured)
        if cls._looks_like_tool_payload(structured_candidate):
            structured_candidate = cls._coerce_mapping(structured_candidate.get("structured_content"))
        structured_payload = cls._coerce_mapping(raw_data.get("structured_content"))
        if not structured_payload:
            structured_payload = structured_candidate
        if not structured_payload and parsed_output:
            structured_payload = dict(parsed_output)

        candidate_mappings = [
            structured_payload,
            cls._coerce_mapping(raw_data),
            structured_candidate,
            parsed_output,
        ]
        candidate_mappings = [mapping for mapping in candidate_mappings if mapping]

        metadata = cls._extract_first_mapping(candidate_mappings, ["metadata", "meta"])
        url = str(
            arguments.get("url")
            or raw_data.get("url")
            or metadata.get("url")
            or ""
        ).strip()
        if url:
            metadata.setdefault("url", url)
        provider = str(
            raw_data.get("provider")
            or structured_payload.get("provider")
            or getattr(schema, "server_name", "")
            or ""
        ).strip()
        if provider:
            metadata.setdefault("provider", provider)

        markdown = str(
            structured_payload.get("markdown")
            or raw_data.get("markdown")
            or parsed_output.get("markdown")
            or ""
        ).strip()
        text = str(
            structured_payload.get("text")
            or raw_data.get("text")
            or parsed_output.get("text")
            or ""
        ).strip()
        content = str(
            structured_payload.get("content")
            or raw_data.get("content")
            or parsed_output.get("content")
            or ""
        ).strip()
        html = str(
            structured_payload.get("html")
            or raw_data.get("html")
            or parsed_output.get("html")
            or ""
        ).strip()

        raw_links = cls._extract_first_list(candidate_mappings, ["links"])
        normalized_links = [
            row
            for row in (cls._normalize_scrape_link_item(item) for item in raw_links)
            if row
        ]

        title = str(metadata.get("title") or metadata.get("page_title") or "").strip()
        source_domain = cls._extract_hostname(url)
        reader_summary = str(markdown or text or content or fallback_output or title).strip()
        public_links = normalized_links
        if url:
            source_link = {
                "label": title or str(urlparse(url).netloc or url).strip(),
                "href": url,
            }
            source_snippet = str(markdown or text or content or fallback_output or "").strip()
            if source_snippet:
                source_link["snippet"] = source_snippet
            public_links = [source_link] + [row for row in normalized_links if row.get("href") != url]

        if metadata:
            structured_payload["metadata"] = metadata
        if markdown:
            structured_payload["markdown"] = markdown
        if text:
            structured_payload["text"] = text
        if content:
            structured_payload["content"] = content
        if html:
            structured_payload["html"] = html
        if normalized_links:
            structured_payload["links"] = normalized_links
        if reader_summary:
            structured_payload["reader_summary"] = reader_summary
        if source_domain:
            structured_payload["source_domain"] = source_domain

        payload: Dict[str, Any] = {
            "public_links": public_links,
            "structured_content": structured_payload,
            "reader_summary": reader_summary,
            "source_kind": "public_web_page",
        }
        if url:
            payload["url"] = url
        if provider:
            payload["provider"] = provider
        if metadata:
            payload["metadata"] = metadata
        if markdown:
            payload["markdown"] = markdown
        if text:
            payload["text"] = text
        if content:
            payload["content"] = content
        if html:
            payload["html"] = html
        if normalized_links:
            payload["links"] = normalized_links
        if source_domain:
            payload["source_domain"] = source_domain
        return payload

    @staticmethod
    def _block_to_serializable(block: Any) -> Any:
        if block is None:
            return None
        if hasattr(block, "model_dump"):
            try:
                return block.model_dump()
            except (TypeError, ValueError):
                pass
        if hasattr(block, "dict"):
            try:
                return block.dict()
            except (TypeError, ValueError):
                pass
        if isinstance(block, dict):
            return block
        if isinstance(block, list):
            return list(block)
        return str(block)
