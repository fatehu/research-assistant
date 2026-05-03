"""Curated MCP server templates for quick onboarding."""

from __future__ import annotations

from typing import Any, Dict, List


def get_mcp_server_templates() -> List[Dict[str, Any]]:
    return [
        {
            "id": "research_assistant_used_stack",
            "title": "Research Assistant 总模板",
            "description": "当前科研系统常用 MCP 组合：Tavily、Firecrawl、内部 Web、内部文献服务，并保留本地 stdio 工具的关闭项。",
            "claude_desktop_config": {
                "mcpServers": {
                    "tavily": {
                        "enabled": True,
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${MCP_TAVILY_API_KEY}",
                    },
                    "firecrawl": {
                        "enabled": True,
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.firecrawl.dev/${MCP_FIRECRAWL_API_KEY}/v2/mcp",
                    },
                    "web_internal": {
                        "enabled": True,
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "http://mcp_web:8091/mcp",
                    },
                    "literature_internal": {
                        "enabled": True,
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "http://mcp_literature:8092/mcp",
                    },
                    "filesystem": {
                        "enabled": False,
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/app"],
                        "transport": "stdio",
                    },
                    "fetch": {
                        "enabled": False,
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-fetch"],
                        "transport": "stdio",
                    },
                    "sequential-thinking": {
                        "enabled": False,
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
                        "transport": "stdio",
                    },
                }
            },
            "recommended_routes": {
                "web_search": ["mcp.tavily.tavily_search", "mcp.web_internal.web_search"],
                "web_scrape": [
                    "mcp.firecrawl.firecrawl_scrape",
                    "mcp.firecrawl.firecrawl_extract",
                    "mcp.web_internal.web_scrape",
                ],
                "literature_search": ["mcp.literature_internal.literature_search"],
            },
        },
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
            "id": "fetch",
            "title": "Fetch (Official)",
            "description": "Fetch and normalize web pages through the official MCP fetch server.",
            "claude_desktop_config": {
                "mcpServers": {
                    "fetch": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-fetch"],
                        "transport": "stdio",
                    }
                }
            },
            "recommended_routes": {"web_scrape": ["mcp.fetch.fetch"]},
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
                        "headers": {"Authorization": "Bearer ${MCP_GITHUB_TOKEN}"},
                    }
                }
            },
        },
        {
            "id": "sequential_thinking",
            "title": "Sequential Thinking",
            "description": "Structured step-by-step reasoning via the MCP sequential-thinking server.",
            "claude_desktop_config": {
                "mcpServers": {
                    "sequential-thinking": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
                        "transport": "stdio",
                    }
                }
            },
        },
        {
            "id": "memory",
            "title": "Memory (Official)",
            "description": "Persistent memory tools for reusable facts and working memory.",
            "claude_desktop_config": {
                "mcpServers": {
                    "memory": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-memory"],
                        "transport": "stdio",
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
                        "env": {"BRAVE_API_KEY": "${MCP_BRAVE_API_KEY}"},
                        "transport": "stdio",
                    }
                }
            },
            "recommended_routes": {"web_search": ["mcp.brave.search"]},
        },
        {
            "id": "tavily_search",
            "title": "Tavily Search",
            "description": "Tavily hosted MCP endpoint for agentic web/research search via API-key URL.",
            "claude_desktop_config": {
                "mcpServers": {
                    "tavily": {
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${MCP_TAVILY_API_KEY}",
                    }
                }
            },
            "recommended_routes": {"web_search": ["mcp.tavily.tavily_search"]},
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
                        "headers": {"Authorization": "Bearer ${MCP_EXA_API_KEY}"},
                    }
                }
            },
            "recommended_routes": {"web_search": ["mcp.exa.search"]},
        },
        {
            "id": "firecrawl",
            "title": "Firecrawl",
            "description": "Firecrawl hosted MCP endpoint for scrape/extract/crawl via API-key URL.",
            "claude_desktop_config": {
                "mcpServers": {
                    "firecrawl": {
                        "type": "http",
                        "transport": "streamable_http",
                        "url": "https://mcp.firecrawl.dev/${MCP_FIRECRAWL_API_KEY}/v2/mcp",
                    }
                }
            },
            "recommended_routes": {"web_scrape": ["mcp.firecrawl.firecrawl_scrape", "mcp.firecrawl.firecrawl_extract"]},
        },
    ]
