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


class _NoCompression:
    async def compress_chunks(self, *args, **kwargs):
        return []


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
    assert context.citation_repair_attempts == 1
    assert context.citation_repair_successes == 1


@pytest.mark.asyncio
async def test_compress_observation_fallback_keeps_source_labels():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)

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


def test_build_rag_metrics_with_required_citation():
    context = AgentContext(messages=[])
    context.final_answer = "Transformer 的核心是自注意力机制 [来源1]"
    context.allowed_source_labels = {"1", "2"}
    context.knowledge_search_calls = 1
    context.compression_calls = 1
    context.compression_success_chunks = 1
    context.compression_fallback_chunks = 1

    metrics = ReActAgent._build_rag_metrics(context)
    assert metrics["knowledge_search_calls"] == 1
    assert metrics["source_labels_count"] == 2
    assert metrics["answer_citation_count"] == 1
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["source_labels"] == ["来源1", "来源2"]


class _ScriptedLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self._call_count = 0

    async def chat(self, *args, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            return {
                "content": '<think>需要检索知识库</think>'
                '<action>{"tool":"knowledge_search","input":{"query":"Transformer 核心"}}'
                "</action>"
            }
        return {
            "content": "<think>根据检索结果整理答案</think>"
            "<answer>Transformer 核心是自注意力机制 [来源1]</answer>"
        }

    async def chat_stream(self, *args, **kwargs):
        yield ""


class _ScriptedTools:
    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "knowledge_search"
        return ToolResult(
            success=True,
            output="raw output",
            data={
                "results": [
                    {
                        "content": "Transformer 的核心是自注意力机制。",
                        "score": 0.92,
                        "knowledge_base": "深度学习基础",
                        "document": "chapter3.md",
                        "chunk_index": 3,
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_run_done_event_contains_rag_metrics_baseline():
    agent = ReActAgent(_ScriptedLLM(), _ScriptedTools(), max_iterations=3)
    agent.contextual_compression_service = _NoCompression()

    events = []
    async for event in agent.run([{"role": "user", "content": "Transformer 核心是什么"}], stream=False):
        events.append(event)

    done_events = [event for event in events if event.get("type") == "done"]
    assert len(done_events) == 1

    payload = done_events[0]["data"]
    metrics = payload["rag_metrics"]
    assert metrics["knowledge_search_calls"] == 1
    assert metrics["source_labels_count"] == 1
    assert metrics["citation_required"] is True
    assert metrics["citation_valid"] is True
    assert metrics["answer_citation_count"] >= 1
    assert "来源1" in metrics["source_labels"]
