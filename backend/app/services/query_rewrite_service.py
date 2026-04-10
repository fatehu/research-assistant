"""
Query rewrite service for retrieval recall improvement.

Strategies:
- synonym expansion
- HyDE hypothetical answer
- sub-question decomposition
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService


_ALLOWED_STRATEGIES = {"synonym", "hyde", "decompose"}
_ALLOWED_REWRITE_PROFILES = {"off", "light", "deep"}


@dataclass
class QueryVariant:
    """One retrieval query variant produced by rewrite pipeline."""

    text: str
    strategy: str


@dataclass
class QueryRewriteResult:
    """Final rewrite payload consumed by retrieval."""

    original_query: str
    enabled: bool
    strategies: list[str]
    synonym_queries: list[str]
    sub_queries: list[str]
    hyde_document: Optional[str]
    vector_variants: list[QueryVariant]
    text_variants: list[QueryVariant]
    fallback_reason: Optional[str] = None
    cache_hit: bool = False
    skip_reason: Optional[str] = None
    llm_called: bool = False


class QueryRewriteService:
    """LLM-based query rewriting with safe fallback to original query."""

    def __init__(self):
        self._llm_service: Optional[LLMService] = None
        self._cache: OrderedDict[str, tuple[float, QueryRewriteResult]] = OrderedDict()

    def _base_result(
        self,
        query: str,
        *,
        enabled: bool,
        strategies: Optional[list[str]] = None,
        fallback_reason: Optional[str] = None,
        cache_hit: bool = False,
        skip_reason: Optional[str] = None,
        llm_called: bool = False,
    ) -> QueryRewriteResult:
        clean_query = self._normalize_query(query)
        base_variant = QueryVariant(text=clean_query, strategy="original")
        return QueryRewriteResult(
            original_query=clean_query,
            enabled=enabled,
            strategies=strategies or [],
            synonym_queries=[],
            sub_queries=[],
            hyde_document=None,
            vector_variants=[base_variant] if clean_query else [],
            text_variants=[base_variant] if clean_query else [],
            fallback_reason=fallback_reason,
            cache_hit=cache_hit,
            skip_reason=skip_reason,
            llm_called=llm_called,
        )

    @staticmethod
    def _normalize_query(query: str) -> str:
        return re.sub(r"\s+", " ", (query or "")).strip()

    @staticmethod
    def _normalize_strategy_name(strategy: str) -> Optional[str]:
        if not strategy:
            return None

        s = strategy.strip().lower()
        alias_map = {
            "synonym": "synonym",
            "synonyms": "synonym",
            "expand": "synonym",
            "equivalent": "synonym",
            "同义": "synonym",
            "同义扩展": "synonym",
            "hyde": "hyde",
            "hypothetical": "hyde",
            "假设文档": "hyde",
            "假设答案": "hyde",
            "decompose": "decompose",
            "decomposition": "decompose",
            "subquery": "decompose",
            "sub-question": "decompose",
            "subquestion": "decompose",
            "子问题": "decompose",
            "子问题分解": "decompose",
        }
        normalized = alias_map.get(s, s)
        return normalized if normalized in _ALLOWED_STRATEGIES else None

    @staticmethod
    def _normalize_rewrite_profile(profile: Optional[str]) -> Optional[str]:
        value = str(profile or "").strip().lower()
        return value if value in _ALLOWED_REWRITE_PROFILES else None

    def _resolve_strategies(self, requested: Optional[list[str]]) -> list[str]:
        return self._resolve_strategies_for_profile(requested, profile="deep")

    def _resolve_strategies_for_profile(
        self,
        requested: Optional[list[str]],
        *,
        profile: str,
    ) -> list[str]:
        normalized_profile = self._normalize_rewrite_profile(profile) or "deep"
        if requested:
            raw_list = requested
        else:
            source = (
                settings.query_rewrite_light_strategies
                if normalized_profile == "light"
                else settings.query_rewrite_strategies
            )
            raw_list = [item.strip() for item in (source or "").split(",") if item.strip()]

        strategies: list[str] = []
        seen: set[str] = set()
        for raw in raw_list:
            normalized = self._normalize_strategy_name(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            strategies.append(normalized)
        return strategies

    def _ensure_llm_service(self) -> LLMService:
        if self._llm_service is None:
            self._llm_service = LLMService()
        return self._llm_service

    def _llm_available(self) -> bool:
        llm = self._ensure_llm_service()
        if llm.provider == "ollama":
            return True
        api_key = (llm.config.get("api_key") or "").strip()
        if not api_key:
            return False
        lower_key = api_key.lower()
        if lower_key in {"your-api-key", "changeme", "replace-me"}:
            return False
        if lower_key.startswith("your-") or lower_key.startswith("your_"):
            return False
        return True

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if not text:
            return {}

        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                raise
            return json.loads(match.group(0))

    @staticmethod
    def _sanitize_list(items: Any, *, max_items: int) -> list[str]:
        if max_items <= 0:
            return []
        if not isinstance(items, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            value = re.sub(r"\s+", " ", str(item or "")).strip()
            if len(value) < 2:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
            if len(out) >= max_items:
                break
        return out

    @staticmethod
    def _sanitize_hyde(text: Any, *, max_chars: int) -> Optional[str]:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(value) < 20:
            return None
        if len(value) > max_chars:
            return value[:max_chars].rstrip()
        return value

    @staticmethod
    def _dedupe_variants(
        variants: list[QueryVariant],
        *,
        max_items: int,
    ) -> list[QueryVariant]:
        result: list[QueryVariant] = []
        seen: set[str] = set()
        for variant in variants:
            text = re.sub(r"\s+", " ", variant.text).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(QueryVariant(text=text, strategy=variant.strategy))
            if len(result) >= max_items:
                break
        return result

    @staticmethod
    def should_skip_rewrite(query: str) -> Optional[str]:
        clean = QueryRewriteService._normalize_query(query)
        if not clean:
            return "empty_query"

        if len(clean) <= max(1, settings.query_rewrite_skip_short_chars):
            return "short_query"

        keyword_like_patterns = [
            r"[\"“”][^\"“”]+[\"“”]",  # quoted phrase
            r"\b(?:and|or|not)\b",  # boolean keywords
            r"(?:\+|-|site:|filetype:|intitle:)",  # search operators
        ]
        for pattern in keyword_like_patterns:
            if re.search(pattern, clean, re.IGNORECASE):
                return "keyword_query"
        return None

    @staticmethod
    def _clone_result(result: QueryRewriteResult) -> QueryRewriteResult:
        return QueryRewriteResult(
            original_query=result.original_query,
            enabled=result.enabled,
            strategies=list(result.strategies),
            synonym_queries=list(result.synonym_queries),
            sub_queries=list(result.sub_queries),
            hyde_document=result.hyde_document,
            vector_variants=[QueryVariant(text=item.text, strategy=item.strategy) for item in result.vector_variants],
            text_variants=[QueryVariant(text=item.text, strategy=item.strategy) for item in result.text_variants],
            fallback_reason=result.fallback_reason,
            cache_hit=result.cache_hit,
            skip_reason=result.skip_reason,
            llm_called=result.llm_called,
        )

    def _cache_key(self, query: str, strategies: list[str]) -> str:
        llm = self._ensure_llm_service()
        payload = {
            "query": query,
            "strategies": strategies,
            "provider": llm.provider,
            "model": llm.config.get("model"),
            "max_synonyms": settings.query_rewrite_max_synonyms,
            "max_subqueries": settings.query_rewrite_max_subqueries,
            "max_variants": settings.query_rewrite_max_variants,
            "hyde_max_chars": settings.query_rewrite_hyde_max_chars,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _cache_get(self, key: str) -> Optional[QueryRewriteResult]:
        ttl = max(1, int(settings.query_rewrite_cache_ttl_seconds))
        item = self._cache.get(key)
        if item is None:
            return None
        ts, value = item
        if (time.time() - ts) > ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        hit = self._clone_result(value)
        hit.cache_hit = True
        return hit

    def _cache_set(self, key: str, value: QueryRewriteResult) -> None:
        max_size = max(1, int(settings.query_rewrite_cache_size))
        clone = self._clone_result(value)
        clone.cache_hit = False
        self._cache[key] = (time.time(), clone)
        self._cache.move_to_end(key)
        while len(self._cache) > max_size:
            self._cache.popitem(last=False)

    @staticmethod
    def _resolve_rewrite_mode(
        rewrite_mode: Optional[str],
        use_query_rewrite: bool,
    ) -> tuple[str, bool]:
        mode = (rewrite_mode or "auto").strip().lower()
        if mode not in {"auto", "force", "off"}:
            mode = "auto"

        if mode == "off":
            return mode, False
        if mode == "force":
            return mode, True
        return mode, bool(use_query_rewrite)

    @classmethod
    def _resolve_rewrite_profile(
        cls,
        rewrite_profile: Optional[str],
        *,
        use_query_rewrite: bool,
    ) -> str:
        normalized = cls._normalize_rewrite_profile(rewrite_profile)
        if normalized:
            return normalized
        if not use_query_rewrite:
            return "off"
        configured = cls._normalize_rewrite_profile(settings.query_rewrite_default_profile)
        return configured or "light"

    def _build_prompt(self, query: str, strategies: list[str]) -> str:
        synonym_hint = (
            f"- synonym_queries: 生成 2 到 {settings.query_rewrite_max_synonyms} 条语义等价查询。"
            if "synonym" in strategies
            else "- synonym_queries: []"
        )
        hyde_hint = (
            "- hyde_document: 生成一段假设答案，覆盖核心术语，长度 120-200 字。"
            if "hyde" in strategies
            else "- hyde_document: \"\""
        )
        sub_hint = (
            f"- sub_queries: 如果是复杂问题，拆分最多 {settings.query_rewrite_max_subqueries} 个子问题；简单问题返回 []。"
            if "decompose" in strategies
            else "- sub_queries: []"
        )
        return f"""
用户原始 query：{query}

请只返回一个 JSON 对象（可被 json.loads 解析），不要输出解释。
{synonym_hint}
{hyde_hint}
{sub_hint}

格式示例：
{{
  "synonym_queries": ["..."],
  "hyde_document": "...",
  "sub_queries": ["..."]
}}
""".strip()

    async def _rewrite_with_llm(self, query: str, strategies: list[str]) -> dict[str, Any]:
        llm = self._ensure_llm_service()
        system_prompt = (
            "你是检索系统的 Query Rewrite 专家。"
            "目标是提升召回率并保持语义准确。"
            "必须只输出 JSON。"
        )
        prompt = self._build_prompt(query, strategies)
        response = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=system_prompt,
                temperature=settings.query_rewrite_temperature,
                max_tokens=min(settings.llm_max_tokens, 800),
            ),
            timeout=max(1, settings.query_rewrite_timeout_seconds),
        )
        return self._extract_json(response.get("content", ""))

    async def rewrite_query(
        self,
        query: str,
        *,
        use_query_rewrite: bool = True,
        requested_strategies: Optional[list[str]] = None,
        rewrite_mode: Optional[str] = None,
        rewrite_profile: Optional[str] = None,
    ) -> QueryRewriteResult:
        clean_query = self._normalize_query(query)
        if not clean_query:
            return self._base_result(clean_query, enabled=False, fallback_reason="empty_query")

        mode, should_rewrite = self._resolve_rewrite_mode(rewrite_mode, use_query_rewrite)
        if not settings.enable_query_rewrite or not should_rewrite:
            return self._base_result(clean_query, enabled=False, fallback_reason="disabled")

        profile = self._resolve_rewrite_profile(
            rewrite_profile,
            use_query_rewrite=should_rewrite,
        )
        if profile == "off":
            return self._base_result(
                clean_query,
                enabled=False,
                fallback_reason="disabled",
                skip_reason="profile_off",
            )

        strategies = self._resolve_strategies_for_profile(requested_strategies, profile=profile)
        if not strategies:
            return self._base_result(
                clean_query,
                enabled=False,
                fallback_reason="no_valid_strategy",
            )

        if mode == "auto":
            skip_reason = self.should_skip_rewrite(clean_query)
            if skip_reason:
                return self._base_result(
                    clean_query,
                    enabled=False,
                    strategies=strategies,
                    fallback_reason="skip_rewrite",
                    skip_reason=skip_reason,
                )

        cache_key = self._cache_key(clean_query, strategies)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if not self._llm_available():
            return self._base_result(
                clean_query,
                enabled=False,
                strategies=strategies,
                fallback_reason="llm_unavailable",
            )

        try:
            payload = await self._rewrite_with_llm(clean_query, strategies)
        except Exception as exc:
            logger.warning(f"[QueryRewrite] LLM rewrite failed, fallback to original query: {exc}")
            return self._base_result(
                clean_query,
                enabled=False,
                strategies=strategies,
                fallback_reason="rewrite_error",
                llm_called=True,
            )

        synonym_queries = (
            self._sanitize_list(
                payload.get("synonym_queries", []),
                max_items=max(0, settings.query_rewrite_max_synonyms),
            )
            if "synonym" in strategies
            else []
        )
        sub_queries = (
            self._sanitize_list(
                payload.get("sub_queries", []),
                max_items=max(0, settings.query_rewrite_max_subqueries),
            )
            if "decompose" in strategies
            else []
        )
        hyde_document = (
            self._sanitize_hyde(
                payload.get("hyde_document"),
                max_chars=max(60, settings.query_rewrite_hyde_max_chars),
            )
            if "hyde" in strategies
            else None
        )

        vector_variants = [QueryVariant(text=clean_query, strategy="original")]
        vector_variants.extend(QueryVariant(text=q, strategy="synonym") for q in synonym_queries)
        vector_variants.extend(QueryVariant(text=q, strategy="decompose") for q in sub_queries)
        if hyde_document:
            vector_variants.append(QueryVariant(text=hyde_document, strategy="hyde"))
        vector_variants = self._dedupe_variants(
            vector_variants,
            max_items=max(1, settings.query_rewrite_max_variants),
        )

        text_variants = [QueryVariant(text=clean_query, strategy="original")]
        text_variants.extend(QueryVariant(text=q, strategy="synonym") for q in synonym_queries)
        text_variants.extend(QueryVariant(text=q, strategy="decompose") for q in sub_queries)
        text_variants = self._dedupe_variants(
            text_variants,
            max_items=max(1, settings.query_rewrite_max_variants),
        )

        result = QueryRewriteResult(
            original_query=clean_query,
            enabled=True,
            strategies=strategies,
            synonym_queries=synonym_queries,
            sub_queries=sub_queries,
            hyde_document=hyde_document,
            vector_variants=vector_variants,
            text_variants=text_variants,
            fallback_reason=None,
            cache_hit=False,
            skip_reason=None,
            llm_called=True,
        )
        self._cache_set(cache_key, result)
        return result


_query_rewrite_service = QueryRewriteService()


def get_query_rewrite_service() -> QueryRewriteService:
    """Get global query rewrite service instance."""
    return _query_rewrite_service
