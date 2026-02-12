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

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
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


@pytest.mark.asyncio
async def test_compress_chunk_low_relevance(monkeypatch):
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
    assert result.fallback_reason == "low_relevance"
    assert result.relevant_content == ""
