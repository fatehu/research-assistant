import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.contextual_compression_service import (
    CompressionInput,
    ContextualCompressionService,
)


def test_extract_json_from_fenced_payload():
    payload = ContextualCompressionService._extract_json(
        """```json
{
  "relevant_content": "[来源1] Transformer 依赖自注意力机制。",
  "relevance_score": 9
}
```"""
    )
    assert payload["relevance_score"] == 9
    assert "Transformer" in payload["relevant_content"]


@pytest.mark.asyncio
async def test_compress_chunk_disabled(monkeypatch):
    service = ContextualCompressionService()
    chunk = CompressionInput(
        source_id=1,
        doc_name="deep_learning.pdf",
        chunk_idx=3,
        chunk_content="这是一个测试段落。",
    )

    monkeypatch.setattr(settings, "enable_contextual_compression", False)
    result = await service.compress_chunk("Transformer 是什么？", chunk)

    assert result.used_compression is False
    assert result.fallback_reason == "disabled"
    assert result.relevant_content == ""


@pytest.mark.asyncio
async def test_compress_chunk_success(monkeypatch):
    service = ContextualCompressionService()
    chunk = CompressionInput(
        source_id=1,
        doc_name="deep_learning.pdf",
        chunk_idx=3,
        chunk_content=(
            "Transformer 由编码器和解码器组成。"
            "其中最核心机制是自注意力。"
            "另外该段还包含一些与问题无关的背景描述。"
        ),
    )

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_min_relevance", 4.0)
    monkeypatch.setattr(service, "_llm_available", lambda: True)
    captured = {}

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            captured["source"] = kwargs.get("source")
            return {
                "content": (
                    '{"relevant_content":"[来源1] Transformer 的核心机制是自注意力。",'
                    '"relevance_score":8.5}'
                )
            }

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())

    result = await service.compress_chunk("Transformer 的核心是什么？", chunk)

    assert result.used_compression is True
    assert result.fallback_reason is None
    assert result.relevance_score == 8.5
    assert "[来源1]" in result.relevant_content
    assert "自注意力" in result.relevant_content
    assert captured["source"] == "retrieval.contextual_compression.single"


@pytest.mark.asyncio
async def test_compress_chunk_low_relevance_fallback_to_extractive(monkeypatch):
    service = ContextualCompressionService()
    chunk = CompressionInput(
        source_id=2,
        doc_name="deep_learning.pdf",
        chunk_idx=8,
        chunk_content="这段主要介绍课程安排，与模型结构关系不大。",
    )

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_min_relevance", 4.0)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            return {
                "content": (
                    '{"relevant_content":"[来源2] 该段几乎不涉及问题。",'
                    '"relevance_score":2}'
                )
            }

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())

    result = await service.compress_chunk("Transformer 的核心是什么？", chunk)

    assert result.used_compression is True
    assert result.fallback_reason == "low_relevance_extractive"
    assert result.relevant_content
    assert "[来源2]" in result.relevant_content


@pytest.mark.asyncio
async def test_compress_chunks_batch_calls_llm_once_for_five_chunks(monkeypatch):
    service = ContextualCompressionService()
    chunks = [
        CompressionInput(
            source_id=idx,
            doc_name="paper.pdf",
            chunk_idx=idx,
            chunk_content=f"第{idx}段内容，包含 Transformer 与注意力。",
        )
        for idx in range(1, 6)
    ]

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_mode", "batch")
    monkeypatch.setattr(settings, "contextual_compression_batch_max_chunks", 8)
    monkeypatch.setattr(settings, "contextual_compression_batch_retry_attempts", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    calls = {"n": 0}

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            calls["n"] += 1
            items = [
                {
                    "source_id": idx,
                    "relevant_content": f"[来源{idx}] 压缩后内容{idx}",
                    "relevance_score": 8.0,
                }
                for idx in range(1, 6)
            ]
            return {"content": json.dumps({"items": items}, ensure_ascii=False)}

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())

    results = await service.compress_chunks("Transformer 是什么？", chunks)

    assert calls["n"] == 1
    assert len(results) == 5
    assert all(item.relevant_content for item in results)
    assert all(item.fallback_reason is None for item in results)


@pytest.mark.asyncio
async def test_compress_chunks_batch_skip_high_reranker(monkeypatch):
    service = ContextualCompressionService()
    chunks = [
        CompressionInput(
            source_id=1,
            doc_name="paper.pdf",
            chunk_idx=1,
            chunk_content="高分段落",
            reranker_score=2.0,
        ),
        CompressionInput(
            source_id=2,
            doc_name="paper.pdf",
            chunk_idx=2,
            chunk_content="需要压缩的段落",
            reranker_score=0.3,
        ),
    ]

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_mode", "batch")
    monkeypatch.setattr(settings, "contextual_compression_skip_rerank_threshold", 0.82)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    calls = {"n": 0}

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            calls["n"] += 1
            return {
                "content": json.dumps(
                    {
                        "items": [
                            {
                                "source_id": 2,
                                "relevant_content": "[来源2] 压缩结果",
                                "relevance_score": 8.8,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())
    results = await service.compress_chunks("测试查询", chunks)
    by_id = {item.source_id: item for item in results}

    assert calls["n"] == 1
    assert by_id[1].fallback_reason == "skip_high_reranker"
    assert by_id[1].used_compression is False
    assert by_id[2].fallback_reason is None
    assert by_id[2].used_compression is True


@pytest.mark.asyncio
async def test_compress_chunks_batch_timeout_fallback_to_extractive(monkeypatch):
    service = ContextualCompressionService()
    chunks = [
        CompressionInput(
            source_id=1,
            doc_name="paper.pdf",
            chunk_idx=1,
            chunk_content="Transformer 的核心机制是自注意力。",
        ),
        CompressionInput(
            source_id=2,
            doc_name="paper.pdf",
            chunk_idx=2,
            chunk_content="该模型可以并行计算。",
        ),
    ]

    monkeypatch.setattr(settings, "enable_contextual_compression", True)
    monkeypatch.setattr(settings, "contextual_compression_mode", "batch")
    monkeypatch.setattr(settings, "contextual_compression_batch_retry_attempts", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            raise TimeoutError("timeout")

    monkeypatch.setattr(service, "_ensure_llm_service", lambda: _FakeLLM())

    results = await service.compress_chunks("Transformer", chunks)
    assert len(results) == 2
    assert all(item.fallback_reason == "batch_compression_error_extractive" for item in results)
    assert all(item.relevant_content for item in results)
