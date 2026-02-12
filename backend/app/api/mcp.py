"""MCP management APIs (config/templates/status)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core.security import get_current_user
from app.models.user import User
from app.services.mcp import (
    MCPClientManager,
    MCPServerManager,
    get_mcp_server_templates,
    load_mcp_server_configs,
    mcp_server_configs_to_claude_desktop_config,
    mcp_server_configs_to_dicts,
    parse_mcp_server_configs_payload,
    save_mcp_config_payload_to_file,
)

router = APIRouter()


class MCPConfigPayload(BaseModel):
    raw_json: Optional[str] = None
    claude_desktop_config: Optional[Dict[str, Any]] = None
    servers: Optional[List[Dict[str, Any]]] = None


class MCPStatusRefreshRequest(BaseModel):
    force_refresh: bool = True


def _safe_parse_routes(raw: str) -> Dict[str, List[str]]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    result: Dict[str, List[str]] = {}
    for key, value in payload.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, str):
            candidates = [value] if value else []
        elif isinstance(value, list):
            candidates = [str(item).strip() for item in value if str(item).strip()]
        else:
            continue
        if candidates:
            result[name] = candidates
    return result


def _current_configs():
    return load_mcp_server_configs(
        settings.mcp_servers,
        settings.mcp_call_timeout_seconds,
        config_path=settings.mcp_config_path,
    )


def _payload_from_request(payload: MCPConfigPayload) -> Any:
    if payload.raw_json and payload.raw_json.strip():
        try:
            return json.loads(payload.raw_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"raw_json 不是合法 JSON: {exc}") from exc
    if payload.claude_desktop_config is not None:
        return payload.claude_desktop_config
    if payload.servers is not None:
        return {"servers": payload.servers}
    raise HTTPException(status_code=400, detail="请提供 raw_json 或 claude_desktop_config 或 servers")


@router.get("/templates")
async def get_templates(_: User = Depends(get_current_user)):
    return {"templates": get_mcp_server_templates()}


@router.get("/config")
async def get_config(_: User = Depends(get_current_user)):
    configs = _current_configs()
    return {
        "enabled": settings.mcp_enabled,
        "tool_prefix": settings.mcp_tool_prefix,
        "call_timeout_seconds": settings.mcp_call_timeout_seconds,
        "config_path": settings.mcp_config_path,
        "tool_routes": _safe_parse_routes(settings.mcp_tool_routes),
        "servers": mcp_server_configs_to_dicts(configs),
        "claude_desktop_config": mcp_server_configs_to_claude_desktop_config(configs),
    }


@router.post("/config/validate")
async def validate_config(payload: MCPConfigPayload, _: User = Depends(get_current_user)):
    parsed_payload = _payload_from_request(payload)
    configs = parse_mcp_server_configs_payload(parsed_payload, settings.mcp_call_timeout_seconds)
    if not configs:
        raise HTTPException(status_code=400, detail="未解析到有效 MCP Server，请检查配置格式")

    return {
        "valid": True,
        "server_count": len(configs),
        "servers": mcp_server_configs_to_dicts(configs),
        "claude_desktop_config": mcp_server_configs_to_claude_desktop_config(configs),
    }


@router.put("/config")
async def save_config(payload: MCPConfigPayload, _: User = Depends(get_current_user)):
    if not settings.mcp_config_path:
        raise HTTPException(status_code=400, detail="MCP_CONFIG_PATH 未配置，无法持久化")

    parsed_payload = _payload_from_request(payload)
    configs = parse_mcp_server_configs_payload(parsed_payload, settings.mcp_call_timeout_seconds)
    if not configs:
        raise HTTPException(status_code=400, detail="未解析到有效 MCP Server，请检查配置格式")

    normalized_payload = mcp_server_configs_to_claude_desktop_config(configs)
    path = save_mcp_config_payload_to_file(settings.mcp_config_path, normalized_payload)

    return {
        "message": "MCP 配置已保存",
        "path": str(path),
        "server_count": len(configs),
        "servers": mcp_server_configs_to_dicts(configs),
        "claude_desktop_config": normalized_payload,
    }


@router.post("/status/refresh")
async def refresh_status(
    request: Optional[MCPStatusRefreshRequest] = None,
    _: User = Depends(get_current_user),
):
    req = request or MCPStatusRefreshRequest()
    configs = _current_configs()
    if not configs:
        return {"server_count": 0, "tool_count": 0, "servers": []}

    server_manager = MCPServerManager(configs)
    client_manager = MCPClientManager(server_manager, tool_prefix=settings.mcp_tool_prefix)
    schemas = await client_manager.discover_tools(force_refresh=req.force_refresh)

    tools_by_server: Dict[str, List[str]] = {}
    for schema in schemas:
        tools_by_server.setdefault(schema.server_name, []).append(schema.qualified_name)

    status_payload = []
    for status in server_manager.list_status():
        status_payload.append(
            {
                "name": status.name,
                "transport": status.transport,
                "enabled": status.enabled,
                "reachable": status.reachable,
                "discovered_tools": status.discovered_tools,
                "last_checked_at": status.last_checked_at.isoformat() if status.last_checked_at else None,
                "last_error": status.last_error,
                "tools": sorted(tools_by_server.get(status.name, [])),
            }
        )

    return {
        "server_count": len(configs),
        "tool_count": len(schemas),
        "servers": status_payload,
    }
