"""Shared helpers for internal MCP servers."""

from __future__ import annotations

import os
from typing import Any, Dict

from app.services.agent_tools import ToolResult


def normalize_transport(raw: str, default: str = "streamable-http") -> str:
    """Normalize env transport value to FastMCP accepted values."""
    value = (raw or "").strip().lower()
    if value in {"streamable_http", "streamable-http"}:
        return "streamable-http"
    if value in {"stdio", "sse"}:
        return value
    return default


def read_host_port(prefix: str, default_port: int) -> tuple[str, int]:
    host = os.getenv(f"{prefix}_HOST", "0.0.0.0").strip() or "0.0.0.0"
    raw_port = os.getenv(f"{prefix}_PORT", str(default_port)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        port = default_port
    return host, port


def tool_result_to_payload(result: ToolResult) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": bool(result.success),
        "output": str(result.output),
    }
    if result.error:
        payload["error"] = str(result.error)
    if result.data is not None:
        payload["data"] = result.data
    return payload
