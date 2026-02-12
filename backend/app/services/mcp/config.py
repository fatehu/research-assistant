"""MCP server configuration helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from loguru import logger

MCPTransport = Literal["stdio", "sse", "streamable_http"]


@dataclass
class MCPServerConfig:
    """Configuration for one MCP server endpoint."""

    name: str
    transport: MCPTransport = "stdio"
    enabled: bool = True
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 20
    sse_read_timeout_seconds: int = 300

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], default_timeout_seconds: int) -> "MCPServerConfig":
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server config missing 'name'")

        transport = str(payload.get("transport", "stdio")).strip().lower()
        if transport not in {"stdio", "sse", "streamable_http"}:
            raise ValueError(f"Unsupported MCP transport: {transport}")

        args = payload.get("args") or []
        if not isinstance(args, list):
            raise ValueError("'args' must be a list")

        env = payload.get("env") or {}
        headers = payload.get("headers") or {}

        if not isinstance(env, dict):
            raise ValueError("'env' must be an object")
        if not isinstance(headers, dict):
            raise ValueError("'headers' must be an object")

        timeout = int(payload.get("timeout_seconds", default_timeout_seconds))
        sse_read_timeout = int(payload.get("sse_read_timeout_seconds", max(timeout * 10, 300)))

        return cls(
            name=name,
            transport=transport,  # type: ignore[arg-type]
            enabled=bool(payload.get("enabled", True)),
            command=str(payload.get("command", "")).strip(),
            args=[str(x) for x in args],
            env={str(k): str(v) for k, v in env.items()},
            cwd=str(payload.get("cwd", "")).strip(),
            url=str(payload.get("url", "")).strip(),
            headers={str(k): str(v) for k, v in headers.items()},
            timeout_seconds=timeout,
            sse_read_timeout_seconds=sse_read_timeout,
        )


def load_mcp_server_configs(raw_json: str, default_timeout_seconds: int) -> List[MCPServerConfig]:
    """Parse MCP server config list from JSON string."""

    text = (raw_json or "").strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"[MCP] Invalid MCP_SERVERS JSON, fallback to empty list: {exc}")
        return []

    if not isinstance(payload, list):
        logger.warning("[MCP] MCP_SERVERS must be a JSON array, fallback to empty list")
        return []

    configs: List[MCPServerConfig] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            logger.warning(f"[MCP] MCP_SERVERS[{index}] is not an object, ignored")
            continue
        try:
            config = MCPServerConfig.from_dict(item, default_timeout_seconds)
            configs.append(config)
        except Exception as exc:  # pragma: no cover - defensive parse guard
            logger.warning(f"[MCP] Skip invalid server config at index {index}: {exc}")

    return configs
