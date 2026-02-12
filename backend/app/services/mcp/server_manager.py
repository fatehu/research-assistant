"""MCP server lifecycle status management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .config import MCPServerConfig


@dataclass
class MCPServerStatus:
    """Runtime status of one MCP server."""

    name: str
    transport: str
    enabled: bool
    reachable: Optional[bool] = None
    discovered_tools: int = 0
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None


class MCPServerManager:
    """Hold MCP server configs and lightweight runtime status."""

    def __init__(self, server_configs: List[MCPServerConfig]):
        self._configs: Dict[str, MCPServerConfig] = {cfg.name: cfg for cfg in server_configs}
        self._statuses: Dict[str, MCPServerStatus] = {
            cfg.name: MCPServerStatus(
                name=cfg.name,
                transport=cfg.transport,
                enabled=cfg.enabled,
            )
            for cfg in server_configs
        }

    def list_configs(self) -> List[MCPServerConfig]:
        return list(self._configs.values())

    def list_enabled_configs(self) -> List[MCPServerConfig]:
        return [cfg for cfg in self._configs.values() if cfg.enabled]

    def get_config(self, name: str) -> Optional[MCPServerConfig]:
        return self._configs.get(name)

    def update_status(
        self,
        name: str,
        *,
        reachable: Optional[bool],
        discovered_tools: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        status = self._statuses.get(name)
        if not status:
            return

        status.reachable = reachable
        if discovered_tools is not None:
            status.discovered_tools = discovered_tools
        status.last_error = error
        status.last_checked_at = datetime.utcnow()

    def list_status(self) -> List[MCPServerStatus]:
        return list(self._statuses.values())
