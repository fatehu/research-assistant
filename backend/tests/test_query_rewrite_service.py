import os
import sys
import time

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


def test_resolve_light_profile_strategies(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "query_rewrite_light_strategies", "synonym")

    strategies = service._resolve_strategies_for_profile(None, profile="light")

    assert strategies == ["synonym"]


@pytest.mark.asyncio
async def test_rewrite_disabled_returns_original(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", False)

    result = await service.rewrite_query("什么是 Attention")

    assert result.enabled is False
    assert result.fallback_reason == "disabled"
    assert [item.text for item in result.vector_variants] == ["什么是 Attention"]
    assert [item.strategy for item in result.vector_variants] == ["original"]
    assert result.llm_called is False


@pytest.mark.asyncio
async def test_rewrite_generates_multi_strategy_variants(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_max_synonyms", 3)
    monkeypatch.setattr(settings, "query_rewrite_max_subqueries", 3)
    monkeypatch.setattr(settings, "query_rewrite_max_variants", 8)
    monkeypatch.setattr(settings, "query_rewrite_hyde_max_chars", 240)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def fake_rewrite_with_llm(query: str, strategies: list[str]):
        return {
            "synonym_queries": ["神经网络 自然语言处理", "DL in NLP"],
            "hyde_document": (
                "Attention 机制通过 query、key、value 的相关性计算动态权重，"
                "能够捕获长距离依赖并提升序列建模能力。"
            ),
            "sub_queries": ["CNN 架构特点", "Transformer 架构特点"],
        }

    monkeypatch.setattr(service, "_rewrite_with_llm", fake_rewrite_with_llm)

    result = await service.rewrite_query(
        "对比 CNN 和 Transformer",
        requested_strategies=["synonym", "hyde", "decompose"],
        rewrite_mode="force",
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
    assert result.llm_called is True
    assert result.cache_hit is False


@pytest.mark.asyncio
async def test_rewrite_default_light_profile_only_uses_synonym(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_default_profile", "light")
    monkeypatch.setattr(settings, "query_rewrite_light_strategies", "synonym")
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    captured = {}

    async def fake_rewrite_with_llm(query: str, strategies: list[str]):
        captured["strategies"] = list(strategies)
        return {
            "synonym_queries": ["agent search"],
            "hyde_document": "This should be ignored because light profile only asks for synonym.",
            "sub_queries": ["should be ignored"],
        }

    monkeypatch.setattr(service, "_rewrite_with_llm", fake_rewrite_with_llm)

    result = await service.rewrite_query("agentic search", rewrite_mode="force")

    assert captured["strategies"] == ["synonym"]
    assert result.synonym_queries == ["agent search"]
    assert result.sub_queries == []
    assert result.hyde_document is None
    assert [item.strategy for item in result.vector_variants] == ["original", "synonym"]


@pytest.mark.asyncio
async def test_rewrite_llm_error_fallback(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def raise_error(query: str, strategies: list[str]):
        raise RuntimeError("mock rewrite error")

    monkeypatch.setattr(service, "_rewrite_with_llm", raise_error)

    result = await service.rewrite_query("什么是 Attention", requested_strategies=["hyde"], rewrite_mode="force")

    assert result.enabled is False
    assert result.fallback_reason == "rewrite_error"
    assert [item.text for item in result.vector_variants] == ["什么是 Attention"]
    assert result.llm_called is True


@pytest.mark.asyncio
async def test_auto_mode_skips_short_query(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 10)

    result = await service.rewrite_query("RAG", rewrite_mode="auto", use_query_rewrite=True)
    assert result.enabled is False
    assert result.fallback_reason == "skip_rewrite"
    assert result.skip_reason == "short_query"
    assert result.llm_called is False


@pytest.mark.asyncio
async def test_cache_hit_for_same_query(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)
    monkeypatch.setattr(settings, "query_rewrite_cache_size", 2000)
    monkeypatch.setattr(settings, "query_rewrite_cache_ttl_seconds", 1800)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    calls = {"n": 0}

    async def fake_rewrite_with_llm(query: str, strategies: list[str]):
        calls["n"] += 1
        return {
            "synonym_queries": ["机器学习"],
            "hyde_document": "这是一段足够长的 HyDE 文档内容，用于缓存测试。",
            "sub_queries": ["机器学习定义"],
        }

    monkeypatch.setattr(service, "_rewrite_with_llm", fake_rewrite_with_llm)

    first = await service.rewrite_query("什么是机器学习", rewrite_mode="force")
    second = await service.rewrite_query("什么是机器学习", rewrite_mode="force")

    assert calls["n"] == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.enabled is True


@pytest.mark.asyncio
async def test_cache_ttl_expired_triggers_recompute(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)
    monkeypatch.setattr(settings, "query_rewrite_cache_ttl_seconds", 1)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    calls = {"n": 0}

    async def fake_rewrite_with_llm(query: str, strategies: list[str]):
        calls["n"] += 1
        return {
            "synonym_queries": ["深度学习"],
            "hyde_document": "这是一段用于 TTL 过期测试的 HyDE 文档内容。",
            "sub_queries": ["深度学习定义"],
        }

    monkeypatch.setattr(service, "_rewrite_with_llm", fake_rewrite_with_llm)

    _ = await service.rewrite_query("深度学习是什么", rewrite_mode="force")
    assert calls["n"] == 1

    # Manually expire cache item.
    for key, (_, result) in list(service._cache.items()):
        service._cache[key] = (time.time() - 10, result)

    second = await service.rewrite_query("深度学习是什么", rewrite_mode="force")
    assert calls["n"] == 2
    assert second.cache_hit is False


@pytest.mark.asyncio
async def test_rewrite_mode_compat_with_legacy_param(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)
    monkeypatch.setattr(settings, "query_rewrite_skip_short_chars", 1)

    # Legacy off should still disable under auto mode.
    legacy_off = await service.rewrite_query("attention mechanism", use_query_rewrite=False, rewrite_mode="auto")
    assert legacy_off.fallback_reason == "disabled"

    # Force mode has higher priority than legacy off.
    monkeypatch.setattr(service, "_llm_available", lambda: False)
    forced = await service.rewrite_query("attention mechanism", use_query_rewrite=False, rewrite_mode="force")
    assert forced.fallback_reason == "llm_unavailable"
    assert forced.fallback_reason != "disabled"


@pytest.mark.asyncio
async def test_rewrite_profile_off_disables_even_when_query_rewrite_enabled(monkeypatch):
    service = QueryRewriteService()
    monkeypatch.setattr(settings, "enable_query_rewrite", True)

    result = await service.rewrite_query(
        "attention mechanism",
        use_query_rewrite=True,
        rewrite_mode="auto",
        rewrite_profile="off",
    )

    assert result.enabled is False
    assert result.fallback_reason == "disabled"
    assert result.skip_reason == "profile_off"
