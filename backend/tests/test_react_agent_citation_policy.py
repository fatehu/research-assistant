import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools import ToolResult
from app.services.react_agent import AgentContext, ReActAgent


class _DummyLLM:
    provider = "test"
    config = {"model": "test-model"}

    async def chat(self, *args, **kwargs):
        return {"content": "修正后的回答 [来源1]"}


class _DummyTools:
    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"


def test_system_prompt_contains_citation_policy():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    prompt = agent._build_system_prompt()

    assert "知识检索引用规范" in prompt
    assert "[来源X]" in prompt
    assert "禁止编造不存在的来源编号" in prompt


def test_observation_message_for_knowledge_search_requires_citation():
    message = ReActAgent._build_observation_message(
        "knowledge_search",
        "[来源1] 这是检索结果",
    )

    assert "<observation>" in message
    assert "必须在关键结论后保留对应的 [来源X] 标注" in message
    assert "只能使用 observation 中出现过的来源编号" in message


def test_observation_message_for_other_tools_is_generic():
    message = ReActAgent._build_observation_message("calculator", "4")

    assert "<observation>" in message
    assert "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。" in message


def test_extract_source_labels_and_validation():
    labels = ReActAgent._extract_source_labels("A[来源1] B[来源2] C")
    assert labels == {"1", "2"}
    assert ReActAgent._citations_are_valid("结论 [来源1]", {"1", "2"}) is True
    assert ReActAgent._citations_are_valid("结论 [来源3]", {"1", "2"}) is False
    assert ReActAgent._citations_are_valid("结论无引用", {"1", "2"}) is False


@pytest.mark.asyncio
async def test_ensure_citation_compliance_repairs_answer():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    context = AgentContext(messages=[])
    context.allowed_source_labels = {"1", "2"}

    fixed = await agent._ensure_citation_compliance("这是没有引用的回答", context)

    assert "[来源1]" in fixed


@pytest.mark.asyncio
async def test_compress_observation_fallback_keeps_source_labels():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)

    class _NoCompression:
        async def compress_chunks(self, *args, **kwargs):
            return []

    agent.contextual_compression_service = _NoCompression()

    result = ToolResult(
        success=True,
        output="raw output",
        data={
            "results": [
                {
                    "content": "Transformer 的核心是自注意力机制。",
                    "score": 0.88,
                    "knowledge_base": "深度学习基础",
                    "document": "chapter3.md",
                    "chunk_index": 3,
                }
            ]
        },
    )

    observation = await agent._compress_knowledge_observation("Transformer 核心是什么", result)
    assert "[来源1]" in observation
