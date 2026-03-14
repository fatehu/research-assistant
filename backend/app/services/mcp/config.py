"""MCP server configuration helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal

from loguru import logger

MCPTransport = Literal["stdio", "sse", "streamable_http"]
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _normalize_transport(raw: Any, *, default: MCPTransport = "stdio") -> MCPTransport:
    value = str(raw or "").strip().lower().replace("-", "_")
    if value in {"streamable_http", "sse", "stdio"}:
        return value  # type: ignore[return-value]
    if value in {"http", "https"}:
        return "streamable_http"
    return default


def _expand_env_placeholders(value: str) -> str:
    text = str(value or "")

    def _replace(match: re.Match[str]) -> str:
        return os.getenv(match.group(1), "")

    return _ENV_PLACEHOLDER_RE.sub(_replace, text)


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

        transport = _normalize_transport(
            payload.get("transport"),
            default="streamable_http" if payload.get("url") else "stdio",
        )

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
            transport=transport,
            enabled=bool(payload.get("enabled", True)),
            command=_expand_env_placeholders(str(payload.get("command", "")).strip()),
            args=[_expand_env_placeholders(str(x)) for x in args],
            env={str(k): _expand_env_placeholders(str(v)) for k, v in env.items()},
            cwd=_expand_env_placeholders(str(payload.get("cwd", "")).strip()),
            url=_expand_env_placeholders(str(payload.get("url", "")).strip()),
            headers={str(k): _expand_env_placeholders(str(v)) for k, v in headers.items()},
            timeout_seconds=timeout,
            sse_read_timeout_seconds=sse_read_timeout,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _iter_server_dicts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    if isinstance(payload.get("servers"), list):
        return [item for item in payload["servers"] if isinstance(item, dict)]

    mcp_servers = payload.get("mcpServers")
    if isinstance(mcp_servers, dict):
        items: List[Dict[str, Any]] = []
        for name, server_payload in mcp_servers.items():
            if not isinstance(server_payload, dict):
                continue
            item = dict(server_payload)
            item["name"] = str(name)
            if not item.get("transport"):
                inferred = item.get("type") or ("streamable_http" if item.get("url") else "stdio")
                item["transport"] = inferred
            items.append(item)
        return items

    if payload.get("name"):
        return [payload]

    return []


def parse_mcp_server_configs_payload(payload: Any, default_timeout_seconds: int) -> List[MCPServerConfig]:
    """Parse any accepted MCP payload format into normalized server configs."""

    raw_items = _iter_server_dicts(payload)
    configs: List[MCPServerConfig] = []
    for index, item in enumerate(raw_items):
        try:
            configs.append(MCPServerConfig.from_dict(item, default_timeout_seconds))
        except Exception as exc:  # pragma: no cover - defensive parse guard
            logger.warning(f"[MCP] Skip invalid server config at index {index}: {exc}")
    return configs


def parse_mcp_server_configs_text(raw_json: str, default_timeout_seconds: int) -> List[MCPServerConfig]:
    text = (raw_json or "").strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"[MCP] Invalid MCP config JSON: {exc}")
        return []

    return parse_mcp_server_configs_payload(payload, default_timeout_seconds)


def load_mcp_server_configs(
    raw_json: str,
    default_timeout_seconds: int,
    *,
    config_path: str = "",
) -> List[MCPServerConfig]:
    """Load MCP server configs from env JSON first, then optional config file."""

    env_configs = parse_mcp_server_configs_text(raw_json, default_timeout_seconds)
    if env_configs:
        return env_configs

    if config_path:
        return load_mcp_server_configs_from_file(config_path, default_timeout_seconds)
    return []


def load_mcp_server_configs_from_file(config_path: str, default_timeout_seconds: int) -> List[MCPServerConfig]:
    """Load MCP configs from a JSON file (supports list or claude_desktop_config style)."""

    path = Path(config_path).expanduser()
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[MCP] Failed to read config file '{path}': {exc}")
        return []

    configs = parse_mcp_server_configs_text(text, default_timeout_seconds)
    if not configs:
        logger.warning(f"[MCP] MCP config file '{path}' is empty or invalid")
    return configs


def save_mcp_config_payload_to_file(config_path: str, payload: Any) -> Path:
    """Persist arbitrary MCP config payload into file as UTF-8 JSON."""

    path = Path(config_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(serialized + "\n", encoding="utf-8")
    return path


def mcp_server_configs_to_dicts(configs: List[MCPServerConfig]) -> List[Dict[str, Any]]:
    return [config.to_dict() for config in configs]


def mcp_server_configs_to_claude_desktop_config(configs: List[MCPServerConfig]) -> Dict[str, Any]:
    """Build a claude_desktop_config-compatible structure from normalized configs."""

    mcp_servers: Dict[str, Any] = {}
    for config in configs:
        entry: Dict[str, Any] = {"enabled": bool(config.enabled)}

        if config.transport == "stdio":
            if config.command:
                entry["command"] = config.command
            if config.args:
                entry["args"] = config.args
            if config.env:
                entry["env"] = config.env
            if config.cwd:
                entry["cwd"] = config.cwd
            entry["transport"] = "stdio"
        elif config.transport == "sse":
            entry["type"] = "sse"
            entry["transport"] = "sse"
            if config.url:
                entry["url"] = config.url
            if config.headers:
                entry["headers"] = config.headers
        else:
            entry["type"] = "http"
            entry["transport"] = "streamable_http"
            if config.url:
                entry["url"] = config.url
            if config.headers:
                entry["headers"] = config.headers

        mcp_servers[config.name] = entry

    return {"mcpServers": mcp_servers}
