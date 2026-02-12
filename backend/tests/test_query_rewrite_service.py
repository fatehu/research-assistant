import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.query_rewrite_service import QueryRewriteService


def test_extract_json_from_fenced_block():
    payload = QueryRewriteService._extract_json(
        """```json
{
  "synonym_queries": ["a", "b"],
  "hyde_document": "test",
  "sub_queries": ["c"]
}
```"""
    )
    assert payload["synonym_queries"] == ["a", "b"]
    assert payload["sub_queries"] == ["c"]


def test_resolve_strategy_aliases():
    service = QueryRewriteService()
    strategies = service._resolve_strategies(["同义扩展", "HyDE", "子问题分解", "invalid"])
    assert strategies == ["synonym", "hyde", "decompose"]


@pytest.mark.asyncio
async def test_rewrite_disabled_returns_original(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", False)

    result = await service.rewrite_query("什么是 Attention")

    assert result.enabled is False
    assert result.fallback_reason == "disabled"
    assert [item.text for item in result.vector_variants] == ["什么是 Attention"]
    assert [item.strategy for item in result.vector_variants] == ["original"]


@pytest.mark.asyncio
async def test_rewrite_generates_multi_strategy_variants(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_max_synonyms", 3)
    monkeypatch.setattr(settings, "query_rewrite_max_subqueries", 3)
    monkeypatch.setattr(settings, "query_rewrite_max_variants", 8)
    monkeypatch.setattr(settings, "query_rewrite_hyde_max_chars", 240)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def fake_rewrite_with_llm(query: str, strategies: list[str]):
        return {
            "synonym_queries": ["神经网络 自然语言处理", "DL in NLP"],
            "hyde_document": "Attention 机制通过 query、key、value 的相关性计算动态权重，"
            "能够捕捉长距离依赖并提升序列建模能力，在机器翻译和文本理解中表现优秀。",
            "sub_queries": ["CNN 架构特点", "Transformer 架构特点"],
        }

    monkeypatch.setattr(service, "_rewrite_with_llm", fake_rewrite_with_llm)

    result = await service.rewrite_query(
        "对比 CNN 和 Transformer",
        requested_strategies=["synonym", "hyde", "decompose"],
    )

    vector_strategies = [item.strategy for item in result.vector_variants]
    text_strategies = [item.strategy for item in result.text_variants]

    assert result.enabled is True
    assert result.fallback_reason is None
    assert result.synonym_queries
    assert result.sub_queries
    assert result.hyde_document is not None
    assert "hyde" in vector_strategies
    assert "hyde" not in text_strategies


@pytest.mark.asyncio
async def test_rewrite_llm_error_fallback(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def raise_error(query: str, strategies: list[str]):
        raise RuntimeError("mock rewrite error")

    monkeypatch.setattr(service, "_rewrite_with_llm", raise_error)

    result = await service.rewrite_query("什么是 Attention", requested_strategies=["hyde"])

    assert result.enabled is False
    assert result.fallback_reason == "rewrite_error"
    assert [item.text for item in result.vector_variants] == ["什么是 Attention"]
