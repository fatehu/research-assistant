"""Curated MCP server templates for quick onboarding."""

from __future__ import annotations

from typing import Any, Dict, List


def get_mcp_server_templates() -> List[Dict[str, Any]]:
    return [
        {
            "id": "filesystem",
            "title": "Filesystem (Official)",
            "description": "Local filesystem read/write tools via official MCP server.",
            "claude_desktop_config": {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                        "transport": "stdio",
                    }
                }
            },
        },
        {
            "id": "postgres",
            "title": "Postgres (Official)",
            "description": "Database query tools for PostgreSQL using official MCP server.",
            "claude_desktop_config": {
                "mcpServers": {
                    "postgres": {
                        "command": "npx",
                        "args": [
                            "-y",
                            "@modelcontextprotocol/server-postgres",
                            "postgresql://user:password@host:5432/database",
                        ],
                        "transport": "stdio",
                    }
                }
            },
        },
        {
            "id": "github",
            "title": "GitHub (Official Remote)",
            "description": "Git workflow through GitHub official remote MCP endpoint.",
            "claude_desktop_config": {
                "mcpServers": {
                    "github": {
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                    }
                }
            },
        },
        {
            "id": "brave_search",
            "title": "Brave Search",
            "description": "Web search MCP server maintained by Brave.",
            "claude_desktop_config": {
                "mcpServers": {
                    "brave": {
                        "command": "npx",
                        "args": ["-y", "@brave/brave-search-mcp-server"],
                        "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}"},
                        "transport": "stdio",
                    }
                }
            },
            "recommended_routes": {"web_search": ["mcp.brave.search"]},
        },
        {
            "id": "exa_search",
            "title": "Exa Search",
            "description": "Exa hosted MCP endpoint for web/research search.",
            "claude_desktop_config": {
                "mcpServers": {
                    "exa": {
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.exa.ai/mcp",
                        "headers": {"Authorization": "Bearer ${EXA_API_KEY}"},
                    }
                }
            },
            "recommended_routes": {"web_search": ["mcp.exa.search"]},
        },
        {
            "id": "firecrawl",
            "title": "Firecrawl",
            "description": "Firecrawl hosted MCP endpoint for scrape/extract/crawl.",
            "claude_desktop_config": {
                "mcpServers": {
                    "firecrawl": {
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.firecrawl.dev/v1",
                        "headers": {"Authorization": "Bearer ${FIRECRAWL_API_KEY}"},
                    }
                }
            },
            "recommended_routes": {"web_scrape": ["mcp.firecrawl.scrape", "mcp.firecrawl.extract"]},
        },
    ]
