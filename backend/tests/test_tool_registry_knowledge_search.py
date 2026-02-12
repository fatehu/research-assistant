import os
import sys

from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import agent_tools


def _fake_tool(name: str):
    return SimpleNamespace(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )


def test_tool_registry_registers_knowledge_search_only_when_db_available(monkeypatch):
    monkeypatch.setattr(agent_tools, "KnowledgeSearchTool", lambda db, user_id: _fake_tool("knowledge_search"))
    monkeypatch.setattr(agent_tools, "WebSearchTool", lambda: _fake_tool("web_search"))
    monkeypatch.setattr(agent_tools, "CalculatorTool", lambda: _fake_tool("calculator"))
    monkeypatch.setattr(agent_tools, "DateTimeTool", lambda: _fake_tool("datetime"))
    monkeypatch.setattr(agent_tools, "TextAnalysisTool", lambda: _fake_tool("text_analysis"))
    monkeypatch.setattr(agent_tools, "UnitConverterTool", lambda: _fake_tool("unit_converter"))
    monkeypatch.setattr(agent_tools, "LiteratureSearchTool", lambda: _fake_tool("literature_search"))

    with_db = agent_tools.ToolRegistry(db=object(), user_id=1)
    without_db = agent_tools.ToolRegistry(db=None, user_id=1)

    assert "knowledge_search" in with_db._tools
    assert "knowledge_search" not in without_db._tools

