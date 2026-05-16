import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.chat import _append_document_artifact_selection, _document_artifact_update_payload_from_observation
from app.services.agent_skill_service import AgentSkillService
from app.services.agent_tools_impl.registry import (
    DefaultToolProvider,
    DocumentArtifactReadTool,
    DocumentArtifactUpdateBlocksTool,
    ToolDependencyContext,
)


def test_document_artifact_selection_prompt_prefers_block_ids():
    prompt = _append_document_artifact_selection("请扩写这一段", ["intro", "method"])

    assert "intro, method" in prompt
    assert "避免空 block_ids 全量读取 artifact" in prompt
    assert "include_markdown=false" in prompt
    assert "按 block_id 精确读取" in prompt


def test_document_artifact_read_tool_description_prefers_targeted_reads():
    description = DocumentArtifactReadTool.description
    block_ids_description = DocumentArtifactReadTool.parameters["properties"]["block_ids"]["description"]
    markdown_description = DocumentArtifactReadTool.parameters["properties"]["include_markdown"]["description"]

    assert "优先按 block_ids 精确读取" in description
    assert "默认是列表模式" in description
    assert "不要默认空 block_ids 全量读取" in description
    assert "为空时默认列出所有 block" in block_ids_description
    assert DocumentArtifactReadTool.parameters["properties"]["include_markdown"]["default"] is False
    assert "只有传入 block_ids 时才会返回 Markdown" in markdown_description


@pytest.mark.asyncio
async def test_document_artifact_read_empty_block_ids_stays_in_list_mode(monkeypatch):
    captured = {}

    class FakeDocumentArtifactService:
        async def read_blocks_for_tool(self, *_args, **kwargs):
            captured.update(kwargs)
            return {
                "artifact_id": "docart-test",
                "blocks": [
                    {
                        "block_id": "intro",
                        "title": "背景介绍",
                        "markdown": "should-not-return",
                    }
                ],
            }

    import app.services.document_artifact_service as document_artifact_service

    monkeypatch.setattr(document_artifact_service, "DocumentArtifactService", FakeDocumentArtifactService)

    tool = DocumentArtifactReadTool(db=object(), user_id=1, conversation_id=7)
    result = await tool.execute(include_markdown=True)

    assert result.success is True
    assert captured["block_ids"] == []
    assert captured["include_markdown"] is False
    assert "空 block_ids 默认按列表模式返回" in result.output


@pytest.mark.asyncio
async def test_document_artifact_read_returns_markdown_for_explicit_block_ids(monkeypatch):
    captured = {}

    class FakeDocumentArtifactService:
        async def read_blocks_for_tool(self, *_args, **kwargs):
            captured.update(kwargs)
            return {"artifact_id": "docart-test", "blocks": [{"block_id": "intro", "markdown": "content"}]}

    import app.services.document_artifact_service as document_artifact_service

    monkeypatch.setattr(document_artifact_service, "DocumentArtifactService", FakeDocumentArtifactService)

    tool = DocumentArtifactReadTool(db=object(), user_id=1, conversation_id=7)
    result = await tool.execute(block_ids=["intro"], include_markdown=True)

    assert result.success is True
    assert captured["block_ids"] == ["intro"]
    assert captured["include_markdown"] is True


@pytest.mark.asyncio
async def test_document_artifact_read_multiple_blocks_adds_batch_write_hint(monkeypatch):
    class FakeDocumentArtifactService:
        async def read_blocks_for_tool(self, *_args, **kwargs):
            return {
                "artifact_id": "docart-test",
                "blocks": [
                    {"block_id": "intro", "markdown": "intro"},
                    {"block_id": "method", "markdown": "method"},
                ],
            }

    import app.services.document_artifact_service as document_artifact_service

    monkeypatch.setattr(document_artifact_service, "DocumentArtifactService", FakeDocumentArtifactService)

    tool = DocumentArtifactReadTool(db=object(), user_id=1, conversation_id=7)
    result = await tool.execute(block_ids=["intro", "method"], include_markdown=True)

    assert result.success is True
    assert "document_artifact_update_blocks" in result.output


@pytest.mark.asyncio
async def test_document_artifact_update_blocks_tool_writes_multiple_blocks(monkeypatch):
    captured = {}

    class FakeDocumentArtifactService:
        async def update_blocks(self, *_args, **kwargs):
            captured.update(kwargs)
            return {
                "artifact": {
                    "artifact_id": "docart-test",
                    "template_id": "tpl",
                    "title": "测试文档",
                    "blocks": [{"block_id": "intro"}, {"block_id": "method"}],
                    "updated_at": "2026-04-26T00:00:00",
                },
                "updated_blocks": [
                    {"block_id": "intro", "markdown": "intro text", "updated_at": "2026-04-26T00:00:00"},
                    {"block_id": "method", "markdown": "method text", "updated_at": "2026-04-26T00:00:00"},
                ],
            }

    import app.services.document_artifact_service as document_artifact_service

    monkeypatch.setattr(document_artifact_service, "DocumentArtifactService", FakeDocumentArtifactService)

    tool = DocumentArtifactUpdateBlocksTool(db=object(), user_id=1, conversation_id=7)
    result = await tool.execute(
        updates=[
            {"block_id": "intro", "markdown": "intro text"},
            {"block_id": "method", "markdown": "method text", "status": "draft"},
        ]
    )

    assert result.success is True
    assert [item["block_id"] for item in captured["updates"]] == ["intro", "method"]
    assert result.data["block_ids"] == ["intro", "method"]
    assert len(result.data["blocks"]) == 2


def test_default_tool_provider_registers_batch_artifact_update_tool():
    provider = DefaultToolProvider()
    tools = provider.build_default_tools(
        ToolDependencyContext(
            db=object(),
            db_session_factory=None,
            user_id=1,
            conversation_id=7,
            route_profile="chat",
        )
    )

    assert "document_artifact_update_blocks" in {tool.name for tool in tools}


def test_artifact_update_payload_supports_batch_blocks():
    payload = _document_artifact_update_payload_from_observation(
        {
            "tool": "document_artifact_update_blocks",
            "success": True,
            "data": {
                "artifact_id": "docart-test",
                "updated_at": "2026-04-26T00:00:00",
                "block_ids": ["intro", "method"],
                "blocks": [
                    {"block_id": "intro", "markdown": "intro"},
                    {"block_id": "method", "markdown": "method"},
                ],
            },
        }
    )

    assert payload["artifact_id"] == "docart-test"
    assert payload["block_ids"] == ["intro", "method"]
    assert len(payload["blocks"]) == 2


def test_artifact_parallel_writing_skill_triggers_for_multi_module_request():
    service = AgentSkillService(skills_root=Path(".agents/skills"))
    resolution = service.resolve("我是需要整体补到10000 每个模块2500就差不多", channel="chat")

    assert any(skill.name == "artifact-parallel-writing" for skill in resolution.active_skills)
    assert "document_artifact_update_blocks" in resolution.active_system_prompt
