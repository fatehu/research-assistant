"""
Query rewrite service for retrieval recall improvement.

Strategies:
- synonym expansion
- HyDE hypothetical answer
- sub-question decomposition
"""
import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService


_ALLOWED_STRATEGIES = {"synonym", "hyde", "decompose"}


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


class QueryRewriteService:
    """LLM-based query rewriting with safe fallback to original query."""

    def __init__(self):
        self._llm_service: Optional[LLMService] = None

    def _base_result(
        self,
        query: str,
        *,
        enabled: bool,
        strategies: Optional[list[str]] = None,
        fallback_reason: Optional[str] = None,
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

    def _resolve_strategies(self, requested: Optional[list[str]]) -> list[str]:
        if requested:
            raw_list = requested
        else:
            raw_list = [
                item.strip()
                for item in (settings.query_rewrite_strategies or "").split(",")
                if item.strip()
            ]

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
用户原始 query：
{query}

请只返回一个 JSON 对象，字段必须完整且可被 json.loads 解析，不要输出解释。
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
            "你的目标是提升召回率，同时保持语义准确。"
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
    ) -> QueryRewriteResult:
        clean_query = self._normalize_query(query)
        if not clean_query:
            return self._base_result(clean_query, enabled=False, fallback_reason="empty_query")

        if not settings.enable_query_rewrite or not use_query_rewrite:
            return self._base_result(clean_query, enabled=False, fallback_reason="disabled")

        strategies = self._resolve_strategies(requested_strategies)
        if not strategies:
            return self._base_result(
                clean_query,
                enabled=False,
                fallback_reason="no_valid_strategy",
            )

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

        return QueryRewriteResult(
            original_query=clean_query,
            enabled=True,
            strategies=strategies,
            synonym_queries=synonym_queries,
            sub_queries=sub_queries,
            hyde_document=hyde_document,
            vector_variants=vector_variants,
            text_variants=text_variants,
            fallback_reason=None,
        )


_query_rewrite_service = QueryRewriteService()


def get_query_rewrite_service() -> QueryRewriteService:
    """Get global query rewrite service instance."""
    return _query_rewrite_service
