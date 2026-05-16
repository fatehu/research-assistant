import os
import sys

from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools


class DummyTool(agent_tools.Tool):
    def __init__(self, name: str):
        self.name = name
        self.description = f"{name} desc"
        self.parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return agent_tools.ToolResult(success=True, output=f"{self.name} ok", data={"kwargs": kwargs})


@dataclass
class FakeSchema:
    server_name: str
    tool_name: str
    qualified_name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any] | None = None


class FakeMCPManager:
    def __init__(self, schemas: List[FakeSchema] | None = None):
        self.schemas = schemas or []
        self.call_history: List[tuple[str, Dict[str, Any]]] = []
        self.discover_calls = 0

    async def discover_tools(self, force_refresh: bool = False):
        self.discover_calls += 1
        return self.schemas

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        self.call_history.append((tool_name, arguments))
        if tool_name in {schema.qualified_name for schema in self.schemas}:
            return type(
                "MCPResult",
                (),
                {
                    "success": True,
                    "output": f"remote:{tool_name}",
                    "data": {"arguments": arguments},
                    "error": None,
                },
            )()

        return type(
            "MCPResult",
            (),
            {
                "success": False,
                "output": f"missing:{tool_name}",
                "data": None,
                "error": "tool_not_found",
            },
        )()


def _patch_default_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(agent_tools, "KnowledgeSearchTool", lambda db, user_id, db_session_factory=None: DummyTool("knowledge_search"))
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: DummyTool("web_search"))
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: DummyTool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: DummyTool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: DummyTool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: DummyTool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: DummyTool("literature_search"))


def _reset_shared_mcp_state():
    agent_tools.ToolRegistry.reset_shared_mcp_cache()


@pytest.mark.asyncio
async def test_refresh_mcp_tools_and_expose_schema(monkeypatch):
    _reset_shared_mcp_state()
    _patch_default_tools(monkeypatch)
    schema = FakeSchema(
        server_name="fetch",
        tool_name="search",
        qualified_name="mcp.fetch.search",
        description="remote search",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    fake_manager = FakeMCPManager([schema])

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    await registry.refresh_mcp_tools()

    tool_names = [item["function"]["name"] for item in registry.list_tools()]
    assert "mcp.fetch.search" in tool_names
    assert "mcp.fetch.search" in registry.get_tools_description()
    assert fake_manager.discover_calls == 1


@pytest.mark.asyncio
async def test_execute_prefers_local_tool_over_mcp(monkeypatch):
    _reset_shared_mcp_state()
    _patch_default_tools(monkeypatch)
    schema = FakeSchema(
        server_name="fetch",
        tool_name="search",
        qualified_name="mcp.fetch.search",
        description="remote search",
        input_schema={"type": "object", "properties": {}},
    )
    fake_manager = FakeMCPManager([schema])

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    await registry.refresh_mcp_tools()
    registry.register(DummyTool("mcp.fetch.search"))

    result = await registry.execute("mcp.fetch.search", query="abc")
    assert result.success is True
    assert result.output == "mcp.fetch.search ok"
    assert fake_manager.call_history == []


@pytest.mark.asyncio
async def test_execute_calls_mcp_when_local_tool_missing(monkeypatch):
    _reset_shared_mcp_state()
    _patch_default_tools(monkeypatch)
    schema = FakeSchema(
        server_name="fetch",
        tool_name="search",
        qualified_name="mcp.fetch.search",
        description="remote search",
        input_schema={"type": "object", "properties": {}},
    )
    fake_manager = FakeMCPManager([schema])

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)

    registry = agent_tools.ToolRegistry(db=None, user_id=1)
    await registry.refresh_mcp_tools()

    result = await registry.execute("mcp.fetch.search", query="abc")
    assert result.success is True
    assert result.output == "remote:mcp.fetch.search"
    assert fake_manager.call_history == [("mcp.fetch.search", {"query": "abc"})]


@pytest.mark.asyncio
async def test_refresh_mcp_tools_reuses_shared_discovery_cache(monkeypatch):
    _reset_shared_mcp_state()
    _patch_default_tools(monkeypatch)
    schema = FakeSchema(
        server_name="fetch",
        tool_name="search",
        qualified_name="mcp.fetch.search",
        description="remote search",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    fake_manager = FakeMCPManager([schema])

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_mcp_client_manager", lambda self: fake_manager)

    first = agent_tools.ToolRegistry(db=None, user_id=1)
    second = agent_tools.ToolRegistry(db=None, user_id=2)

    await first.refresh_mcp_tools()
    await second.refresh_mcp_tools()

    assert fake_manager.discover_calls == 1
    assert "mcp.fetch.search" in [item["function"]["name"] for item in second.list_tools()]


@pytest.mark.asyncio
async def test_warmup_shared_mcp_tools_populates_registry_cache(monkeypatch):
    _reset_shared_mcp_state()
    _patch_default_tools(monkeypatch)
    schema = FakeSchema(
        server_name="fetch",
        tool_name="search",
        qualified_name="mcp.fetch.search",
        description="remote search",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    fake_manager = FakeMCPManager([schema])

    monkeypatch.setattr(agent_tools.settings, "mcp_enabled", True)
    monkeypatch.setattr(agent_tools.ToolRegistry, "_create_standalone_mcp_client_manager", classmethod(lambda cls: fake_manager))

    report = await agent_tools.ToolRegistry.warmup_shared_mcp_tools(force_refresh=False)
    registry = agent_tools.ToolRegistry(db=None, user_id=1, initialize_mcp=False)
    registry._mcp_client_manager = fake_manager
    await registry.refresh_mcp_tools()

    assert report["status"] == "ready"
    assert report["tool_count"] == 1
    assert fake_manager.discover_calls == 1
    assert "mcp.fetch.search" in [item["function"]["name"] for item in registry.list_tools()]
