"""
Generative reader agent tool helpers.
"""

from __future__ import annotations

from typing import Sequence, Set

from app.config import settings
from app.services.agent_tools import ToolRegistry


DEFAULT_GENERATIVE_READER_AGENT_TOOL_NAMES = {
    "paper_read",
    "knowledge_search",
    "web_search",
    "web_scrape",
}


def resolve_generative_reader_agent_tool_whitelist() -> Set[str]:
    raw = str(getattr(settings, "generative_reader_agent_tool_whitelist", "") or "").strip()
    if not raw:
        return set(DEFAULT_GENERATIVE_READER_AGENT_TOOL_NAMES)
    output: Set[str] = set()
    for token in raw.split(","):
        name = str(token or "").strip()
        if name:
            output.add(name)
    if not output:
        output = set(DEFAULT_GENERATIVE_READER_AGENT_TOOL_NAMES)
    return output


def build_generative_reader_tool_registry(
    *,
    user_id: int,
    allowed_tool_names: Sequence[str],
) -> ToolRegistry:
    """Build a ToolRegistry for generative-reader usage."""
    registry = ToolRegistry(
        db=None,
        user_id=int(user_id),
        db_session_factory=None,
    )
    allow = {str(item).strip() for item in list(allowed_tool_names or []) if str(item).strip()}
    mcp_allow_prefixes = {name for name in allow if name.startswith("mcp.")}
    for tool_name in list(registry._tools.keys()):  # pylint: disable=protected-access
        if tool_name in allow:
            continue
        if mcp_allow_prefixes and any(tool_name.startswith(prefix) for prefix in mcp_allow_prefixes):
            continue
        registry._tools.pop(tool_name, None)  # pylint: disable=protected-access
    return registry

