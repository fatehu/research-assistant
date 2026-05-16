import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.agent_tools import ToolResult
from app.services.contextual_compression_service import (
    CompressionInput,
    CompressionResult,
    ContextualCompressionService,
)
from app.services.react_agent import ReActAgent


class _DummyLLM:
    provider = "ollama"
    config = {"model": "dummy"}


class _DummyTools:
    pass


@pytest.mark.asyncio
async def test_batch_pipeline_api_like_inputs_respects_reranker_skip(monkeypatch):
    service = ContextualCompressionService()
    chunks = [
        CompressionInput(
            source_id=1,
            doc_name="doc-a",
            chunk_idx=1,
            chunk_content="这是最高相关片段",
            reranker_score=2.0,
        ),
        CompressionInput(
            source_id=2,
            doc_name="doc-b",
            chunk_idx=2,
            chunk_content="这是需要压缩的片段",
            reranker_score=0.12,
        ),
    ]

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_mode", "batch")
    monkeypatch.setattr(settings, "contextual_compression_skip_rerank_threshold", 0.82)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            return {"content": '{"items":[{"source_id":2,"relevant_content":"[来源2] 压缩文本","relevance_score":8.3}]}'}

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())
    results = await service.compress_chunks("query", chunks)
    by_id = {item.source_id: item for item in results}

    assert by_id[1].fallback_reason == "skip_high_reranker"
    assert by_id[1].used_compression is False
    assert by_id[2].fallback_reason is None
    assert by_id[2].used_compression is True


@pytest.mark.asyncio
async def test_react_agent_pipeline_passes_reranker_score_to_compression():
    agent = ReActAgent(_DummyLLM(), _DummyTools())
    captured = {}

    class _SpyCompressionService:
        async def compress_chunks(self, query, chunks, use_contextual_compression=True):
            captured["query"] = query
            captured["chunks"] = chunks
            return [
                CompressionResult(
                    source_id=item.source_id,
                    source_label=f"来源{item.source_id}",
                    doc_name=item.doc_name,
                    chunk_idx=item.chunk_idx,
                    relevant_content=f"[来源{item.source_id}] mock",
                    relevance_score=8.0,
                    used_compression=True,
                    fallback_reason=None,
                    raw_response=None,
                )
                for item in chunks
            ]

    agent.contextual_compression_service = _SpyCompressionService()

    result = ToolResult(
        success=True,
        output="raw",
        data={
            "results": [
                {
                    "content": "chunk content",
                    "score": 0.88,
                    "document": "doc-1",
                    "knowledge_base": "kb-1",
                    "chunk_index": 3,
                    "reranker_score": 0.93,
                }
            ]
        },
        error=None,
    )

    compressed = await agent._compress_knowledge_observation("query", result, None)
    assert "Compressed contexts: 1" in compressed
    assert captured["query"] == "query"
    assert len(captured["chunks"]) == 1
    assert captured["chunks"][0].reranker_score == 0.93


@pytest.mark.asyncio
async def test_react_agent_pipeline_omits_compression_score_when_service_disabled():
    agent = ReActAgent(_DummyLLM(), _DummyTools())

    class _DisabledCompressionService:
        async def compress_chunks(self, query, chunks, use_contextual_compression=True):
            return [
                CompressionResult(
                    source_id=item.source_id,
                    source_label=f"来源{item.source_id}",
                    doc_name=item.doc_name,
                    chunk_idx=item.chunk_idx,
                    relevant_content="",
                    relevance_score=0.0,
                    used_compression=False,
                    fallback_reason="disabled",
                    raw_response=None,
                )
                for item in chunks
            ]

    agent.contextual_compression_service = _DisabledCompressionService()

    result = ToolResult(
        success=True,
        output="raw",
        data={
            "results": [
                {
                    "content": "chunk content",
                    "score": 0.88,
                    "document": "doc-1",
                    "knowledge_base": "kb-1",
                    "chunk_index": 3,
                }
            ]
        },
        error=None,
    )

    observation = await agent._compress_knowledge_observation("query", result, None)
    assert "Knowledge contexts: 1" in observation
    assert "Compression score" not in observation
    assert "[来源1] chunk content" in observation
