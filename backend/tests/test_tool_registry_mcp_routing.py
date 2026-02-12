import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools


class CountingTool(agent_tools.Tool):
    def __init__(self, name: str, output: str | None = None):
        self.name = name
        self.description = f"{name} desc"
        self.parameters = {"type": "object", "properties": {}}
        self.calls = 0
        self.output = output or f"{name} local"

    async def execute(self, **kwargs):
        self.calls += 1
        return agent_tools.ToolResult(success=True, output=self.output, data={"kwargs": kwargs})


def _make_mcp_result(success: bool, output: str, error: str | None = None, data: Dict[str, Any] | None = None):
    return type(
        "MCPResult",
        (),
        {
            "success": success,
            "output": output,
            "data": data,
            "error": error,
        },
    )()


class FakeMCPManager:
    def __init__(self, responses: Dict[str, Any]):
        self.responses = responses
        self.call_history: List[tuple[str, Dict[str, Any]]] = []

    async def discover_tools(self, force_refresh: bool = False):
        return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        self.call_history.append((tool_name, arguments))
        value = self.responses.get(tool_name)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return _make_mcp_result(False, f"missing:{tool_name}", error="tool_not_found")
        return value


def _patch_default_tools(monkeypatch: pytest.MonkeyPatch, web_search_tool: CountingTool):
    monkeypatch.setattr(agent_tools, "KnowledgeSearchTool", lambda db, user_id, db_session_factory=None: CountingTool("knowledge_search"))
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: web_search_tool)
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: CountingTool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: CountingTool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: CountingTool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: CountingTool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: CountingTool("literature_search"))


@pytest.mark.asyncio
async def test_route_prefers_external_then_skip_local(monkeypatch):
    local_tool = CountingTool("web_search", output="local-web")
    fake_manager = FakeMCPManager(
        {
            "mcp.brave.search": _make_mcp_result(
                True,
                "remote-web",
                data={"provider": "brave"},
            )
        }
    )
    _patch_default_tools(monkeypatch, web_search_tool=local_tool)

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "mcp_tool_routes", '{"web_search": ["mcp.brave.search"]}')
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result = await registry.execute("web_search", query="llm news")

    assert result.success is True
    assert result.output == "remote-web"
    assert local_tool.calls == 0
    assert fake_manager.call_history == [("mcp.brave.search", {"query": "llm news"})]


@pytest.mark.asyncio
async def test_route_fallback_to_local_when_external_failed(monkeypatch):
    local_tool = CountingTool("web_search", output="local-web")
    fake_manager = FakeMCPManager(
        {"mcp.brave.search": _make_mcp_result(False, "remote failed", error="mcp_call_failed")}
    )
    _patch_default_tools(monkeypatch, web_search_tool=local_tool)

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "mcp_tool_routes", '{"web_search": ["mcp.brave.search"]}')
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result = await registry.execute("web_search", query="python mcp")

    assert result.success is True
    assert result.output == "local-web"
    assert local_tool.calls == 1
    assert fake_manager.call_history == [("mcp.brave.search", {"query": "python mcp"})]


@pytest.mark.asyncio
async def test_route_tries_next_candidate_on_missing(monkeypatch):
    local_tool = CountingTool("web_search", output="local-web")
    fake_manager = FakeMCPManager(
        {
            "mcp.brave.search": _make_mcp_result(False, "not found", error="tool_not_found"),
            "mcp.exa.search": _make_mcp_result(True, "exa ok", data={"provider": "exa"}),
        }
    )
    _patch_default_tools(monkeypatch, web_search_tool=local_tool)

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(
        agent_tools.settings,
        "mcp_tool_routes",
        '{"web_search": ["mcp.brave.search", "mcp.exa.search"]}',
    )
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result = await registry.execute("web_search", query="agent architecture")

    assert result.success is True
    assert result.output == "exa ok"
    assert local_tool.calls == 0
    assert fake_manager.call_history == [
        ("mcp.brave.search", {"query": "agent architecture"}),
        ("mcp.exa.search", {"query": "agent architecture"}),
    ]


@pytest.mark.asyncio
async def test_route_circuit_breaker_skips_remote_after_threshold(monkeypatch):
    local_tool = CountingTool("web_search", output="local-web")
    fake_manager = FakeMCPManager(
        {"mcp.brave.search": _make_mcp_result(False, "timeout", error="timeout")}
    )
    _patch_default_tools(monkeypatch, web_search_tool=local_tool)

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.settings, "mcp_tool_routes", '{"web_search": ["mcp.brave.search"]}')
    monkeypatch.setattr(agent_tools.settings, "mcp_route_timeout_seconds", 3)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_retry_attempts", 1)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_circuit_breaker_failures", 1)
    monkeypatch.setattr(agent_tools.settings, "mcp_route_circuit_breaker_open_seconds", 60)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)
    agent_tools.ToolRegistry._mcp_route_circuit_state = {}

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    result_1 = await registry.execute("web_search", query="first")
    result_2 = await registry.execute("web_search", query="second")

    assert result_1.success is True
    assert result_2.success is True
    assert result_1.output == "local-web"
    assert result_2.output == "local-web"
    assert local_tool.calls == 2
    assert fake_manager.call_history == [("mcp.brave.search", {"query": "first"})]
