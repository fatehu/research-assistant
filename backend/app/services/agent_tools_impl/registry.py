"""
Agent 工具定义和执行 - 支持共享知识库搜索
"""
import asyncio
import base64
import fnmatch
import httpx
import json
import math
import os
import re
import shutil
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Callable, Type, Protocol, Mapping, Sequence, Literal
from dataclasses import dataclass
from urllib.parse import urlparse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, or_, and_, tuple_
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import settings
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.services.embedding_service import get_embedding_service_for_model_and_dimension
from app.services.hybrid_retrieval_service import fuse_rrf, merge_rows_by_score
from app.services.query_rewrite_service import (
    QueryVariant,
    QueryRewriteResult,
    get_query_rewrite_service,
)
from app.services.reranker_service import get_reranker_service, RerankerService
from app.services.vector_search_tuning import apply_hnsw_ef_search, resolve_ef_search
from app.services.chinese_segmentation_service import segment_text_for_fts
from app.services.contextual_retrieval_service import (
    build_adjacent_lookup_keys,
    build_reranker_input,
    merge_adjacent_context,
    normalize_adjacent_window,
)
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)
from app.services.smart_chunking.token_utils import estimate_tokens, tokens_to_chars

# 尝试导入共享模块（可选）
try:
    from app.models.role import SharedResource, GroupMember, ResearchGroup, UserRole
    from app.models.user import User
    SHARING_ENABLED = True
except ImportError:
    SHARING_ENABLED = False


@dataclass
class EnhancedToolResult:
    """工具执行结果（增强版）。"""

    success: bool
    output: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    output_tokens_estimate: int = 0
    truncated: bool = False


ToolResult = EnhancedToolResult


_REPO_SKIPPED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
}


class Tool:
    """工具协议（兼容旧实现）。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    parallel_safe: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError


class ToolBase(Tool, ABC):
    """增强工具基类：超时、重试、Pydantic 校验、输出 token 估算与截断。"""

    timeout_seconds: Optional[float] = None
    retry_count: Optional[int] = None
    input_model: Optional[Type[BaseModel]] = None
    output_max_tokens: Optional[int] = None

    @abstractmethod
    async def _execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def _resolve_timeout_seconds(self) -> float:
        timeout = self.timeout_seconds
        if timeout is None:
            timeout = float(getattr(settings, "tool_default_timeout_seconds", 20))
        return max(float(timeout), 1.0)

    def _resolve_retry_count(self) -> int:
        retries = self.retry_count
        if retries is None:
            retries = int(getattr(settings, "tool_default_retry_count", 1))
        return max(int(retries), 0)

    def _resolve_output_max_tokens(self) -> int:
        max_tokens = self.output_max_tokens
        if max_tokens is None:
            max_tokens = int(getattr(settings, "tool_output_max_tokens", 1200))
        return max(int(max_tokens), 64)

    @staticmethod
    def _validation_error_result(exc: ValidationError) -> ToolResult:
        contract = build_tool_error_contract(
            code="validation_error",
            message="工具参数校验失败，请检查输入格式。",
            stage="validate_input",
            detail=str(exc),
            retryable=False,
        )
        return ToolResult(
            success=False,
            output=str(contract["message"]),
            error=str(contract["code"]),
            data=merge_error_contract({"validation_errors": exc.errors()}, contract),
        )

    @staticmethod
    def _clamp_ratio(raw_ratio: float) -> float:
        return min(max(raw_ratio, 0.2), 0.9)

    def _truncate_output_if_needed(self, output: str) -> tuple[str, bool, int]:
        safe_output = str(output or "")
        est_tokens = estimate_tokens(safe_output)
        max_tokens = self._resolve_output_max_tokens()
        if est_tokens <= max_tokens:
            return safe_output, False, est_tokens

        char_budget = max(tokens_to_chars(max_tokens, safe_output), 120)
        marker = "\n\n...[TRUNCATED]...\n\n"
        ratio = self._clamp_ratio(float(getattr(settings, "tool_output_truncate_head_ratio", 0.75)))

        head_chars = max(40, int(char_budget * ratio))
        tail_budget = max(0, char_budget - head_chars - len(marker))
        if tail_budget > 0 and len(safe_output) > head_chars:
            truncated_output = f"{safe_output[:head_chars]}{marker}{safe_output[-tail_budget:]}"
        else:
            truncated_output = f"{safe_output[: max(40, char_budget - len(marker))]}{marker}"

        return truncated_output, True, estimate_tokens(truncated_output)

    def _validate_input(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if self.input_model is None:
            return kwargs
        validated = self.input_model.model_validate(kwargs)
        return validated.model_dump(exclude_none=True)

    def _with_finalized_result(
        self,
        result: ToolResult,
        *,
        started_at: float,
        retry_attempt: int,
    ) -> ToolResult:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        output, truncated, output_tokens_estimate = self._truncate_output_if_needed(result.output)
        merged_data = dict(result.data or {})
        merged_data.setdefault("retry_attempt", retry_attempt)
        if truncated:
            merged_data.setdefault("output_truncated", True)

        return ToolResult(
            success=bool(result.success),
            output=output,
            data=merged_data or None,
            error=result.error,
            execution_time_ms=elapsed_ms,
            output_tokens_estimate=output_tokens_estimate,
            truncated=bool(result.truncated or truncated),
        )

    async def execute(self, **kwargs) -> ToolResult:
        started_at = time.perf_counter()
        try:
            validated_kwargs = self._validate_input(kwargs)
        except ValidationError as exc:
            result = self._validation_error_result(exc)
            return self._with_finalized_result(result, started_at=started_at, retry_attempt=0)

        retries = self._resolve_retry_count()
        max_attempts = retries + 1
        timeout_seconds = self._resolve_timeout_seconds()
        last_result: ToolResult = ToolResult(success=False, output="工具执行失败", error="unknown_error")

        for attempt in range(1, max_attempts + 1):
            try:
                maybe_awaitable = self._execute(**validated_kwargs)
                result = await asyncio.wait_for(maybe_awaitable, timeout=timeout_seconds)
                if not isinstance(result, ToolResult):
                    raise TypeError(f"Tool returned unsupported result type: {type(result)}")
                last_result = result
                if result.success:
                    return self._with_finalized_result(
                        result,
                        started_at=started_at,
                        retry_attempt=attempt,
                    )
            except asyncio.TimeoutError:
                contract = build_tool_error_contract(
                    code="timeout",
                    message=f"工具执行超时（>{timeout_seconds:.1f}s）",
                    tool_name=self.name,
                    stage="execute",
                    retryable=(attempt < max_attempts),
                    metadata={
                        "timeout_seconds": timeout_seconds,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                last_result = ToolResult(
                    success=False,
                    output=str(contract["message"]),
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )
            except Exception as exc:
                contract = build_tool_error_contract(
                    code="tool_execution_exception",
                    message="工具执行失败",
                    tool_name=self.name,
                    stage="execute",
                    detail=str(exc),
                    retryable=(attempt < max_attempts),
                    metadata={
                        "exception_type": type(exc).__name__,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                last_result = ToolResult(
                    success=False,
                    output=f"{contract['message']}: {exc}",
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )

            if attempt < max_attempts:
                logger.warning(
                    f"[ToolBase] retry tool={self.name}, attempt={attempt}/{max_attempts}, error={last_result.error}"
                )

        return self._with_finalized_result(
            last_result,
            started_at=started_at,
            retry_attempt=max_attempts,
        )


@dataclass
class ToolDependencyContext:
    db: Optional[AsyncSession]
    db_session_factory: Optional[Callable[[], AsyncSession]]
    user_id: Optional[int]
    conversation_id: Optional[int] = None
    notebook_id: Optional[str] = None
    kernel_manager: Any = None
    notebooks_store: Optional[dict] = None
    user_authorized: bool = False
    route_profile: str = "chat"


class ToolProvider(Protocol):
    def build_default_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        ...

    def build_notebook_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        ...


class MCPRemoteTool(Tool):
    """Adapter: expose MCP remote tools through local Tool protocol."""

    def __init__(self, schema: Any, mcp_client_manager: Any):
        self.schema = schema
        self.mcp_client_manager = mcp_client_manager
        self.name = str(schema.qualified_name)
        self.description = str(schema.description or f"MCP 远程工具（{schema.server_name}.{schema.tool_name}）")
        self.parameters = schema.input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs) -> ToolResult:
        result = await self.mcp_client_manager.call_tool(self.name, kwargs)
        return ToolResult(
            success=bool(result.success),
            output=str(result.output),
            data=result.data if isinstance(result.data, dict) else {"raw": result.data},
            error=result.error,
        )


def _coerce_mcp_schema_properties(schema: Any) -> Dict[str, Dict[str, Any]]:
    input_schema = getattr(schema, "input_schema", None)
    if not isinstance(input_schema, Mapping):
        return {}
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for key, value in properties.items():
        if not str(key).strip():
            continue
        normalized[str(key)] = dict(value) if isinstance(value, Mapping) else {}
    return normalized


def _pick_mcp_property_name(properties: Mapping[str, Any], candidates: Sequence[str]) -> str:
    lower_to_actual = {str(name).strip().lower(): str(name) for name in properties.keys() if str(name).strip()}
    for candidate in candidates:
        actual = lower_to_actual.get(str(candidate).strip().lower())
        if actual:
            return actual
    return ""


def _copy_matching_mcp_arguments(
    *,
    translated: Dict[str, Any],
    original: Mapping[str, Any],
    properties: Mapping[str, Any],
) -> Dict[str, Any]:
    for key in properties.keys():
        normalized_key = str(key).strip()
        if not normalized_key or normalized_key in translated:
            continue
        if normalized_key in original:
            translated[normalized_key] = original[normalized_key]
    return translated


def _normalize_requested_formats(value: Any) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip().lower() for item in list(value) if str(item).strip()]


def _choose_scrape_format_value(property_schema: Mapping[str, Any], formats: Sequence[str]) -> str:
    normalized_formats = [str(item).strip().lower() for item in list(formats or []) if str(item).strip()]
    enum_values = property_schema.get("enum")
    if not isinstance(enum_values, Sequence) or isinstance(enum_values, (str, bytes)):
        return normalized_formats[0] if normalized_formats else "markdown"

    normalized_enum: Dict[str, str] = {
        str(item).strip().lower(): str(item)
        for item in list(enum_values)
        if str(item).strip()
    }
    preferred = normalized_formats or ["markdown"]
    for candidate in preferred:
        actual = normalized_enum.get(candidate)
        if actual:
            return actual
        if candidate == "markdown":
            for fallback in ("text", "md", "markdown"):
                actual = normalized_enum.get(fallback)
                if actual:
                    return actual
        if candidate == "html":
            for fallback in ("html", "raw"):
                actual = normalized_enum.get(fallback)
                if actual:
                    return actual
    for fallback in ("markdown", "text", "html"):
        actual = normalized_enum.get(fallback)
        if actual:
            return actual
    first_value = next(iter(normalized_enum.values()), "")
    return first_value or (normalized_formats[0] if normalized_formats else "markdown")


def _build_routed_scrape_prompt(*, formats: Sequence[str], only_main_content: bool) -> str:
    preferred_format = "markdown" if "markdown" in formats else ("html" if "html" in formats else "text")
    focus_clause = "Focus on the main article content." if only_main_content else "Include the full page context."
    return (
        "Extract reader-facing webpage content with headings, summaries, and important supporting links. "
        f"{focus_clause} Prefer {preferred_format} output when possible."
    )


def _looks_like_research_web_query(query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return False
    patterns = (
        r"\barxiv\b",
        r"\bpaper\b",
        r"\bresearch\b",
        r"\bdoi\b",
        r"\bpdf\b",
        r"\bpreprint\b",
        r"论文",
        r"研究",
        r"学术",
        r"文献",
        r"期刊",
        r"arxiv",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _default_routed_search_categories(query: str) -> List[str]:
    if _looks_like_research_web_query(query):
        return ["research", "pdf"]
    return []


_AUTHORITATIVE_PUBLIC_DOMAIN_SUFFIXES = (
    ".gov",
    ".edu",
    ".ac.uk",
    "nih.gov",
    "who.int",
    "nature.com",
    "nejm.org",
    "thelancet.com",
    "science.org",
    "usmle.org",
    "pubmed.ncbi.nlm.nih.gov",
)


def _extract_hostname(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = str(parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _clean_reader_excerpt(value: Any, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if limit and limit > 0:
        return text[:limit]
    return text


_DIRECT_URL_PATTERN = re.compile(r"https?://[^\s<>()\"'`]+", re.IGNORECASE)


def _extract_urls_from_text(value: Any, *, limit: int = 5) -> List[str]:
    text = str(value or "")
    if not text:
        return []
    urls: List[str] = []
    seen: set[str] = set()
    for match in _DIRECT_URL_PATTERN.finditer(text):
        candidate = str(match.group(0) or "").strip().rstrip(".,);:]!?")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= limit:
            break
    return urls


def _extract_query_filename_hints(query: Any, *, limit: int = 4) -> List[str]:
    text = str(query or "").strip()
    if not text:
        return []
    matches = re.findall(
        r'([A-Za-z0-9_.-]+\.(?:h5|hdf5|zip|json|csv|tsv|txt|md|pdf|pt|pth|ckpt|bin|tar(?:\.[A-Za-z0-9]+)?))',
        text,
        flags=re.IGNORECASE,
    )
    filenames: List[str] = []
    seen: set[str] = set()
    for raw in matches:
        value = str(raw or "").strip()
        lowered = value.lower()
        if not value or lowered in seen:
            continue
        seen.add(lowered)
        filenames.append(value)
        if len(filenames) >= limit:
            break
    return filenames


def _url_path_basename(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = str(parsed.path or "").rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1].strip()


def _looks_like_downloadable_file_url(value: Any) -> bool:
    basename = _url_path_basename(value).lower()
    if not basename:
        return False
    return basename.endswith(
        (
            ".h5",
            ".hdf5",
            ".zip",
            ".json",
            ".csv",
            ".tsv",
            ".txt",
            ".md",
            ".pdf",
            ".pt",
            ".pth",
            ".ckpt",
            ".bin",
            ".tar",
            ".tar.gz",
            ".tar.bz2",
            ".tar.xz",
        )
    )


def _is_authoritative_public_source(value: Any) -> bool:
    host = _extract_hostname(value)
    if not host:
        return False
    return any(host == suffix or host.endswith(suffix) for suffix in _AUTHORITATIVE_PUBLIC_DOMAIN_SUFFIXES)


def _dedupe_public_links(rows: Sequence[Mapping[str, Any]], *, limit: int = 5) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen: set[str] = set()
    for row in list(rows or []):
        href = str(row.get("url") or row.get("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        item: Dict[str, str] = {
            "label": str(row.get("title") or row.get("label") or href).strip(),
            "href": href,
        }
        snippet = _clean_reader_excerpt(
            row.get("reader_excerpt") or row.get("snippet") or row.get("summary") or "",
            limit=0,
        )
        if snippet:
            item["snippet"] = snippet
        links.append(item)
        if limit and len(links) >= limit:
            break
    return links


def _collect_ranked_reader_texts(
    rows: Sequence[Mapping[str, Any]],
    *,
    empty_summary: str,
) -> str:
    excerpts: List[str] = []
    for row in list(rows or []):
        excerpt = str(
            row.get("reader_excerpt")
            or row.get("snippet")
            or row.get("summary")
            or ""
        ).strip()
        if not excerpt:
            continue
        title = str(row.get("title") or "").strip()
        candidate = excerpt
        if title and title.lower() not in excerpt.lower():
            candidate = f"{title}: {excerpt}"
        if candidate and candidate not in excerpts:
            excerpts.append(candidate)
    return "\n\n".join(excerpts) if excerpts else empty_summary


def _summarize_ranked_reader_excerpts(
    rows: Sequence[Mapping[str, Any]],
    *,
    empty_summary: str,
    limit: int = 2,
) -> str:
    excerpts: List[str] = []
    for row in list(rows or []):
        excerpt = _clean_reader_excerpt(
            row.get("reader_excerpt") or row.get("snippet") or row.get("summary") or "",
            limit=140,
        )
        if not excerpt:
            continue
        title = _clean_reader_excerpt(row.get("title") or "", limit=80)
        candidate = excerpt
        if title and title.lower() not in excerpt.lower():
            candidate = _clean_reader_excerpt(f"{title}: {excerpt}", limit=160)
        if candidate and candidate not in excerpts:
            excerpts.append(candidate)
        if len(excerpts) >= limit:
            break
    return " | ".join(excerpts) if excerpts else empty_summary


def _summarize_domain_distribution(rows: Sequence[Mapping[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for row in list(rows or []):
        domain = _extract_hostname(row.get("url") or row.get("href") or row.get("domain") or "")
        if not domain:
            continue
        counts[domain] = counts.get(domain, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        {
            "domain": domain,
            "count": count,
            "authoritative": _is_authoritative_public_source(domain),
        }
        for domain, count in ranked[:limit]
    ]


def _build_tool_provenance(
    *,
    source: str,
    execution_mode: str,
    provider: str,
    provider_route: str,
    tool_kind: str,
    local_tool_name: str,
) -> Dict[str, Any]:
    return {
        "source": str(source or "").strip(),
        "execution_mode": str(execution_mode or "").strip(),
        "provider": str(provider or "").strip(),
        "provider_route": str(provider_route or "").strip(),
        "tool_kind": str(tool_kind or "").strip(),
        "local_tool_name": str(local_tool_name or "").strip(),
        "normalization_version": "guided_reading_v1",
    }


def _normalize_web_search_result_item(
    item: Mapping[str, Any],
    *,
    provider: str,
    rank: int,
) -> Dict[str, Any]:
    url = str(
        item.get("url")
        or item.get("link")
        or item.get("href")
        or item.get("source")
        or ""
    ).strip()
    title = str(
        item.get("title")
        or item.get("name")
        or item.get("label")
        or item.get("answer")
        or url
        or "Public resource"
    ).strip()
    snippet = str(
        item.get("snippet")
        or item.get("summary")
        or item.get("content")
        or item.get("description")
        or item.get("text")
        or item.get("answer")
        or ""
    ).strip()
    domain = _extract_hostname(url)
    reader_excerpt = _clean_reader_excerpt(snippet or title, limit=0)
    embedded_urls = _extract_urls_from_text(" ".join(part for part in (title, snippet) if part), limit=5)
    normalized: Dict[str, Any] = {
        "rank": int(rank),
        "title": title,
        "url": url,
        "snippet": snippet,
        "reader_excerpt": reader_excerpt,
        "type": str(item.get("type") or ("organic" if url else "result")).strip() or "result",
        "display_url": domain or url,
        "domain": domain,
        "is_authoritative_source": bool(domain and _is_authoritative_public_source(domain)),
        "embedded_urls": embedded_urls,
    }
    for key in ("source", "date", "published_at", "score"):
        if item.get(key) is not None:
            normalized[key] = item.get(key)
    if provider:
        normalized["provider"] = provider
    return normalized


def _build_web_search_payload(
    *,
    query: str,
    provider: str,
    provider_route: str,
    results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    normalized_results = [
        _normalize_web_search_result_item(item, provider=provider, rank=index)
        for index, item in enumerate(list(results or []), start=1)
        if isinstance(item, Mapping)
    ]
    filename_hints = {value.lower() for value in _extract_query_filename_hints(query)}
    candidate_download_urls: List[Dict[str, Any]] = []
    candidate_seen: set[str] = set()
    for row in normalized_results:
        embedded_urls = [
            str(item or "").strip()
            for item in list(row.get("embedded_urls") or [])
            if str(item or "").strip()
        ]
        matched_urls: List[str] = []
        for href in embedded_urls:
            basename = _url_path_basename(href).lower()
            if filename_hints:
                if basename and basename in filename_hints:
                    matched_urls.append(href)
            elif _looks_like_downloadable_file_url(href):
                matched_urls.append(href)
        if matched_urls:
            row["candidate_download_urls"] = matched_urls
        for href in matched_urls:
            if href in candidate_seen:
                continue
            candidate_seen.add(href)
            candidate_download_urls.append(
                {
                    "url": href,
                    "result_rank": int(row.get("rank") or 0),
                    "source_result_url": str(row.get("url") or "").strip(),
                    "source_result_domain": str(row.get("domain") or "").strip(),
                    "matched_filename": _url_path_basename(href),
                }
            )
    public_links = _dedupe_public_links(normalized_results, limit=len(normalized_results) or 0)
    result_types = [
        str(item.get("type") or "").strip()
        for item in normalized_results
        if str(item.get("type") or "").strip()
    ]
    reader_summary = _collect_ranked_reader_texts(
        normalized_results,
        empty_summary=f"No public web results found for '{query}'.",
    )
    structured_content = {
        "query": str(query or "").strip(),
        "provider": str(provider or "").strip(),
        "total": len(normalized_results),
        "results": normalized_results,
        "reader_summary": reader_summary,
        "result_types": list(dict.fromkeys(result_types)),
        "domains": _summarize_domain_distribution(normalized_results),
        "candidate_download_urls": candidate_download_urls,
    }
    provenance = _build_tool_provenance(
        source="local",
        execution_mode="direct",
        provider=provider,
        provider_route=provider_route,
        tool_kind="web_search",
        local_tool_name="web_search",
    )
    return {
        "query": str(query or "").strip(),
        "provider": str(provider or "").strip(),
        "provider_route": str(provider_route or "").strip(),
        "source_kind": "public_web_search",
        "results": normalized_results,
        "total": len(normalized_results),
        "public_links": public_links,
        "candidate_download_urls": candidate_download_urls,
        "reader_summary": reader_summary,
        "structured_content": structured_content,
        "provenance": provenance,
    }


def _normalize_knowledge_search_result_item(
    item: Mapping[str, Any],
    *,
    rank: int,
) -> Dict[str, Any]:
    normalized = dict(item)
    content = str(item.get("content") or "").strip()
    knowledge_base = str(item.get("knowledge_base") or "未知").strip()
    document = str(item.get("document") or "未知").strip()
    reader_excerpt = _clean_reader_excerpt(content, limit=240)
    normalized.update(
        {
            "rank": int(rank),
            "reader_excerpt": reader_excerpt,
            "source_label": f"{knowledge_base} / {document}",
            "citation_label": f"{document} · chunk {int(item.get('chunk_index') or 0)}",
        }
    )
    return normalized


def _build_knowledge_search_payload(
    *,
    query: str,
    results: Sequence[Mapping[str, Any]],
    search_time_ms: float,
) -> Dict[str, Any]:
    normalized_results = [
        _normalize_knowledge_search_result_item(item, rank=index)
        for index, item in enumerate(list(results or []), start=1)
        if isinstance(item, Mapping)
    ]
    kb_hits: Dict[str, int] = {}
    document_hits: Dict[str, int] = {}
    retrieval_modes: List[str] = []
    for row in normalized_results:
        kb_name = str(row.get("knowledge_base") or "").strip()
        document_name = str(row.get("document") or "").strip()
        retrieval_mode = str(row.get("retrieval_mode") or "").strip()
        if kb_name:
            kb_hits[kb_name] = kb_hits.get(kb_name, 0) + 1
        if document_name:
            document_hits[document_name] = document_hits.get(document_name, 0) + 1
        if retrieval_mode:
            retrieval_modes.append(retrieval_mode)
    reader_summary = _summarize_ranked_reader_excerpts(
        normalized_results,
        empty_summary=f"No knowledge-base passages found for '{query}'.",
    )
    structured_content = {
        "query": str(query or "").strip(),
        "provider": "local_pgvector",
        "total": len(normalized_results),
        "search_time_ms": round(float(search_time_ms or 0.0), 3),
        "results": normalized_results,
        "reader_summary": reader_summary,
        "retrieval_modes": list(dict.fromkeys(retrieval_modes)),
        "knowledge_base_hits": [
            {"knowledge_base": name, "count": count}
            for name, count in sorted(kb_hits.items(), key=lambda item: (-item[1], item[0]))
        ],
        "document_hits": [
            {"document": name, "count": count}
            for name, count in sorted(document_hits.items(), key=lambda item: (-item[1], item[0]))
        ],
    }
    provenance = _build_tool_provenance(
        source="knowledge_base",
        execution_mode="direct",
        provider="local_pgvector",
        provider_route="local.knowledge_search.pgvector",
        tool_kind="knowledge_search",
        local_tool_name="knowledge_search",
    )
    return {
        "query": str(query or "").strip(),
        "provider": "local_pgvector",
        "provider_route": "local.knowledge_search.pgvector",
        "source_kind": "knowledge_base_search",
        "results": normalized_results,
        "total": len(normalized_results),
        "search_time_ms": round(float(search_time_ms or 0.0), 3),
        "reader_summary": reader_summary,
        "structured_content": structured_content,
        "provenance": provenance,
    }


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchTool(ToolBase):
    """Web 搜索工具 - Serper -> Tavily -> DDGS。"""

    name = "web_search"
    parallel_safe = True
    description = "搜索互联网获取最新信息。当用户问题涉及新闻、实时信息、天气、或需要网络查询时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认5，最大10",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    input_model = WebSearchInput
    timeout_seconds = 15
    retry_count = 0

    def __init__(self):
        self.serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
        self.tavily_api_key = (
            str(
                getattr(settings, "tavily_api_key", "")
                or os.getenv("TAVILY_API_KEY", "")
                or os.getenv("MCP_TAVILY_API_KEY", "")
            )
            .strip()
        )
        if self.serper_api_key:
            logger.info(f"[WebSearch] Serper API key 已配置 (长度: {len(self.serper_api_key)})")
        else:
            logger.warning("[WebSearch] 未配置 SERPER_API_KEY")
        if self.tavily_api_key:
            logger.info(f"[WebSearch] Tavily API key 已配置 (长度: {len(self.tavily_api_key)})")
        else:
            logger.warning("[WebSearch] 未配置 TAVILY_API_KEY")

    async def _execute(self, query: str, max_results: int = 5) -> ToolResult:
        errors: List[str] = []
        logger.info(f"[WebSearch] query={query}, max_results={max_results}")

        if self.serper_api_key:
            result = await self._safe_provider_call("serper", self._serper_search, query, max_results)
            if result.success:
                return result
            errors.append(f"serper:{result.error or 'failed'}")

        if self.tavily_api_key:
            result = await self._safe_provider_call("tavily", self._tavily_search, query, max_results)
            if result.success:
                return result
            errors.append(f"tavily:{result.error or 'failed'}")

        result = await self._ddgs_search(query, max_results)
        if result.success:
            return result
        errors.append(f"ddgs:{result.error or 'failed'}")

        return ToolResult(
            success=False,
            output=f"网络搜索失败，已尝试 Serper/Tavily/DDGS。错误: {'; '.join(errors)}",
            error="web_search_all_failed",
        )

    async def _safe_provider_call(
        self,
        provider_name: str,
        provider_fn: Callable[[str, int], Any],
        query: str,
        max_results: int,
    ) -> ToolResult:
        try:
            return await provider_fn(query, max_results)
        except httpx.RequestError as exc:
            logger.warning(f"[WebSearch] {provider_name} request error: {exc}")
            return ToolResult(
                success=False,
                output=f"{provider_name} 请求失败: {exc}",
                error=f"{provider_name}_request_error",
            )
        except Exception as exc:  # pragma: no cover - 防御性保护 fallback 链路
            logger.exception(f"[WebSearch] {provider_name} unexpected error: {exc}")
            return ToolResult(
                success=False,
                output=f"{provider_name} 执行异常: {exc}",
                error=f"{provider_name}_exception",
            )

    async def _serper_search(self, query: str, max_results: int) -> ToolResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": self.serper_api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "q": query,
                    "num": max_results,
                    "gl": "cn",
                    "hl": "zh-cn",
                },
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    output=f"Serper API 请求失败: HTTP {response.status_code}",
                    error=f"serper_http_{response.status_code}",
                )

            data = response.json()
            results: List[Dict[str, Any]] = []

            if "knowledgeGraph" in data:
                kg = data["knowledgeGraph"]
                results.append(
                    {
                        "type": "knowledge_graph",
                        "title": kg.get("title", ""),
                        "description": kg.get("description", ""),
                        "attributes": kg.get("attributes", {}),
                    }
                )

            if "answerBox" in data:
                ab = data["answerBox"]
                answer = ab.get("answer") or ab.get("snippet") or ab.get("title", "")
                if answer:
                    results.append(
                        {
                            "type": "answer_box",
                            "answer": answer,
                            "source": ab.get("link", ""),
                        }
                    )

            for item in data.get("organic", [])[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", ""),
                        "date": item.get("date", ""),
                    }
                )

            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data=_build_web_search_payload(
                    query=query,
                    provider="serper",
                    provider_route="local.web_search.serper",
                    results=results,
                ),
            )

    async def _tavily_search(self, query: str, max_results: int) -> ToolResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.tavily_api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    output=f"Tavily API 请求失败: HTTP {response.status_code}",
                    error=f"tavily_http_{response.status_code}",
                )

            payload = response.json()
            results: List[Dict[str, Any]] = []
            for item in payload.get("results", [])[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("content", ""),
                    }
                )

            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data=_build_web_search_payload(
                    query=query,
                    provider="tavily",
                    provider_route="local.web_search.tavily",
                    results=results,
                ),
            )

    async def _ddgs_search(self, query: str, max_results: int) -> ToolResult:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ToolResult(
                success=False,
                output="duckduckgo-search 未安装，无法执行 DDGS 兜底搜索。",
                error="ddgs_not_installed",
            )

        def _search_sync() -> List[Dict[str, Any]]:
            with DDGS() as ddgs:
                rows = ddgs.text(query, max_results=max_results)
                return list(rows)

        try:
            rows = await asyncio.to_thread(_search_sync)
            results: List[Dict[str, Any]] = []
            for item in rows[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("href", ""),
                        "snippet": item.get("body", ""),
                    }
                )
            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data=_build_web_search_payload(
                    query=query,
                    provider="ddgs",
                    provider_route="local.web_search.ddgs",
                    results=results,
                ),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output=f"DDGS 搜索失败: {exc}",
                error="ddgs_failed",
            )

    def _format_results(self, query: str, results: List[Dict[str, Any]]) -> str:
        if not results:
            return f"未找到关于 '{query}' 的搜索结果。"

        parts = [f"搜索 '{query}' 的结果："]
        organic_index = 0
        for item in results:
            result_type = item.get("type", "organic")
            if result_type == "knowledge_graph":
                parts.append(f"\n[知识卡片] {item.get('title', '')}")
                if item.get("description"):
                    parts.append(f"\n{item['description']}")
                for key, value in list((item.get("attributes") or {}).items())[:3]:
                    parts.append(f"\n- {key}: {value}")
                continue

            if result_type == "answer_box":
                parts.append(f"\n[直接答案] {item.get('answer', '')}")
                if item.get("source"):
                    parts.append(f"\n来源: {item.get('source', '')}")
                continue

            organic_index += 1
            parts.append(f"\n\n[结果{organic_index}] {item.get('title', '')}")
            if item.get("url"):
                parts.append(f"\n链接: {item['url']}")
            if item.get("snippet"):
                parts.append(f"\n摘要: {item['snippet']}")

        return "".join(parts)


class WebScrapeInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    formats: Optional[List[str]] = None
    only_main_content: bool = True


class WebScrapeTool(ToolBase):
    """网页抓取工具壳：优先交给 MCP 路由（例如 Firecrawl）。"""

    name = "web_scrape"
    parallel_safe = True
    description = "抓取网页正文、结构化内容或提取页面关键信息。优先由 MCP 抓取工具处理。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL",
            },
            "formats": {
                "type": "array",
                "items": {"type": "string"},
                "description": "期望的输出格式，例如 markdown、html、text",
            },
            "only_main_content": {
                "type": "boolean",
                "description": "是否尽量仅抓取正文区域",
                "default": True,
            },
        },
        "required": ["url"],
    }
    input_model = WebScrapeInput
    timeout_seconds = 45
    retry_count = 0

    async def _execute(
        self,
        url: str,
        formats: Optional[List[str]] = None,
        only_main_content: bool = True,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            output="web_scrape 当前依赖外部 MCP 抓取服务；请检查 Firecrawl MCP 路由是否已启用。",
            error="web_scrape_mcp_required",
            data={
                "url": url,
                "formats": formats or [],
                "only_main_content": bool(only_main_content),
            },
        )


class KnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    include_adjacent_chunks: bool = False
    adjacent_window: int = Field(default=1, ge=1, le=3)
    knowledge_base_ids: Optional[List[int]] = None
    document_ids: Optional[List[int]] = None
    use_reranker: Optional[bool] = None
    use_hybrid: Optional[bool] = None
    use_query_rewrite: Optional[bool] = None
    query_rewrite_profile: Optional[Literal["off", "light", "deep"]] = None
    use_contextual_compression: Optional[bool] = None


class PaperResearchPrepareInput(BaseModel):
    paper_id: Optional[int] = Field(default=None, ge=1)
    paper_title: Optional[str] = Field(default=None, max_length=500)
    project_id: Optional[int] = Field(default=None, ge=1)
    project_title: Optional[str] = Field(default=None, max_length=300)
    user_goal: Optional[str] = Field(default=None, max_length=2000)
    create_project: bool = True
    create_workspace: bool = True
    refresh_intake: bool = False


class PaperResearchStatusInput(BaseModel):
    paper_id: Optional[int] = Field(default=None, ge=1)
    paper_title: Optional[str] = Field(default=None, max_length=500)
    project_id: Optional[int] = Field(default=None, ge=1)


class PaperResearchCloneRepoInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_url: Optional[str] = Field(default=None, max_length=1000)
    refresh: bool = False


class PaperResearchCreateRunDraftInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    paper_id: Optional[int] = Field(default=None, ge=1)
    paper_title: Optional[str] = Field(default=None, max_length=500)
    project_id: Optional[int] = Field(default=None, ge=1)
    run_label: str = Field(min_length=1, max_length=200)
    run_kind: Literal["baseline", "variant"] = "variant"
    model_name: Optional[str] = Field(default=None, max_length=255)
    hypothesis: Optional[str] = Field(default=None, max_length=2000)
    params: Dict[str, Any] = Field(default_factory=dict)
    variant_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchArtifactManifestInput(BaseModel):
    project_id: int = Field(ge=1)


class PaperResearchReadArtifactInput(BaseModel):
    project_id: int = Field(ge=1)
    relative_path: str = Field(min_length=1, max_length=260)
    max_chars: int = Field(default=20000, ge=200, le=200000)


class PaperResearchReadRepoFileInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_relative_path: str = Field(min_length=1, max_length=400)
    max_chars: int = Field(default=20000, ge=200, le=200000)


class PaperResearchSearchRepoInput(BaseModel):
    project_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=20, ge=1, le=100)
    case_sensitive: bool = False
    is_regex: bool = False
    glob: Optional[str] = Field(default=None, max_length=200)


class PaperResearchWriteImplementationSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    implementation_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadImplementationSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    max_chars: int = Field(default=20000, ge=200, le=200000)


class PaperResearchWriteRunDraftsInput(BaseModel):
    project_id: int = Field(ge=1)
    run_drafts: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadRunDraftsInput(BaseModel):
    project_id: int = Field(ge=1)
    max_chars: int = Field(default=20000, ge=200, le=200000)


class PaperResearchInspectRuntimeInput(BaseModel):
    project_id: int = Field(ge=1)


class PaperResearchProbeRepoInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_url: Optional[str] = Field(default=None, max_length=1000)


class PaperResearchProbeUrlInput(BaseModel):
    project_id: int = Field(ge=1)
    url: str = Field(min_length=1, max_length=2000)
    expected_kind: Literal["auto", "html", "file", "hdf5", "zip", "json", "text"] = "auto"
    read_bytes: int = Field(default=64, ge=8, le=512)


class PaperResearchWriteExecutionSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadExecutionSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)


class PaperResearchStartExecutionInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)


class PaperResearchReadExecutionInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)
    include_logs: bool = True
    max_log_chars: int = Field(default=20000, ge=0, le=200000)


class PaperResearchCancelExecutionInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)


class _PaperResearchToolBase(ToolBase):
    """Deterministic operations used by the paper planning / repro-prep skill playbook."""

    parallel_safe = False
    retry_count = 0
    timeout_seconds = 720.0
    output_max_tokens = 2600
    _PROJECT_ROOT_ALIAS = "project_workspace"
    _RUN_DRAFT_KINDS = {
        "env_setup",
        "data_prep",
        "smoke_test",
        "baseline_repro",
        "evaluation",
        "first_tuning",
        "custom",
    }
    _RUN_DRAFT_ENTRYPOINT_TYPES = {
        "repo_script",
        "notebook",
        "config",
        "readme_command",
        "dataset_step",
        "manual_step",
        "unknown",
    }
    _CANONICAL_FILE_SPECS: Dict[str, Dict[str, str]] = {
        "planning/paper_intake_result.json": {
            "kind": "planning",
            "name": "paper_intake_result",
            "content_type": "json",
            "actual_rel_path": "paper_intake_result.json",
        },
        "planning/experiment_spec.json": {
            "kind": "planning",
            "name": "experiment_spec",
            "content_type": "json",
            "actual_rel_path": "experiment_spec.json",
        },
        "planning/paper_intake_markdown.md": {
            "kind": "planning",
            "name": "paper_intake_markdown",
            "content_type": "text",
            "actual_rel_path": "paper_intake_markdown.md",
        },
        "planning/paper_intake_payload.json": {
            "kind": "planning",
            "name": "paper_intake_payload",
            "content_type": "json",
            "actual_rel_path": "paper_intake_payload.json",
        },
        "repo/repo_reference.json": {
            "kind": "repo",
            "name": "repo_reference",
            "content_type": "json",
            "actual_rel_path": "repo_reference.json",
        },
        "repo/repo_file_index.json": {
            "kind": "repo",
            "name": "repo_file_index",
            "content_type": "json",
            "actual_rel_path": "repo_file_index.json",
        },
        "repo/repo_history_url_candidates.json": {
            "kind": "repo",
            "name": "repo_history_url_candidates",
            "content_type": "json",
            "actual_rel_path": "repo_history_url_candidates.json",
        },
        "repo/repo_readme_excerpt.md": {
            "kind": "repo",
            "name": "repo_readme_excerpt",
            "content_type": "text",
            "actual_rel_path": "repo_readme_excerpt.md",
        },
        "meta/workspace_adapter_manifest.json": {
            "kind": "meta",
            "name": "workspace_adapter_manifest",
            "content_type": "json",
            "actual_rel_path": "workspace_adapter_manifest.json",
        },
        "meta/workspace_readme.md": {
            "kind": "meta",
            "name": "workspace_readme",
            "content_type": "text",
            "actual_rel_path": "WORKSPACE_README.md",
        },
        "specs/implementation_spec.json": {
            "kind": "spec",
            "name": "implementation_spec",
            "content_type": "json",
            "actual_rel_path": "specs/implementation_spec.json",
        },
        "drafts/run_drafts.json": {
            "kind": "draft",
            "name": "run_drafts",
            "content_type": "json",
            "actual_rel_path": "drafts/run_drafts.json",
        },
    }

    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
        conversation_id: Optional[int] = None,
        route_profile: str = "chat",
    ):
        self.db = db
        self.user_id = int(user_id)
        self.db_session_factory = db_session_factory
        self.conversation_id = int(conversation_id) if conversation_id is not None else None
        self.route_profile = str(route_profile or "chat").strip().lower() or "chat"

    async def _with_db(self, handler: Callable[[AsyncSession], Any]) -> ToolResult:
        if self.db is not None:
            return await handler(self.db)
        if self.db_session_factory is None:
            return ToolResult(
                success=False,
                output="论文研究工具不可用：数据库会话未初始化。",
                error="db_unavailable",
            )
        async with self.db_session_factory() as session:
            return await handler(session)

    async def _resolve_paper(
        self,
        db: AsyncSession,
        *,
        paper_id: Optional[int],
        paper_title: Optional[str],
    ) -> Any:
        from app.models.literature import Paper

        if paper_id is not None:
            result = await db.execute(
                select(Paper).where(Paper.id == int(paper_id), Paper.user_id == self.user_id)
            )
            return result.scalar_one_or_none()

        title = str(paper_title or "").strip()
        if not title:
            return None
        result = await db.execute(
            select(Paper)
            .where(Paper.user_id == self.user_id, Paper.title.ilike(f"%{title[:180]}%"))
            .order_by(Paper.updated_at.desc(), Paper.id.desc())
            .limit(5)
        )
        rows = list(result.scalars().all())
        if len(rows) == 1:
            return rows[0]
        title_lower = title.lower()
        return next((row for row in rows if str(row.title or "").strip().lower() == title_lower), None)

    @staticmethod
    def _paper_not_found(paper_id: Optional[int], paper_title: Optional[str]) -> ToolResult:
        return ToolResult(
            success=False,
            output="没有找到可用论文。请提供已保存论文的 paper_id，或给出更完整的论文标题。",
            error="paper_not_found",
            data={"paper_id": paper_id, "paper_title": paper_title},
        )

    async def _resolve_project_payload(
        self,
        project_service: Any,
        *,
        paper: Any,
        project_id: Optional[int],
        project_title: Optional[str] = None,
        user_goal: Optional[str] = None,
        create_project: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if project_id is not None:
            return await project_service.get_project_payload(project_id=int(project_id), user_id=self.user_id)

        existing = await project_service.list_project_payloads(user_id=self.user_id, paper_id=int(paper.id))
        if existing:
            return dict(existing[0])
        if not create_project:
            return None

        created = await project_service.create_project(
            user_id=self.user_id,
            title=project_title or f"{str(paper.title or '')[:120]} - Research Project",
            goal=user_goal or f"围绕《{paper.title}》做复现、调优与创新验证",
            status="draft",
            paper_ids=[int(paper.id)],
        )
        return await project_service.get_project_payload(project_id=int(created.id), user_id=self.user_id)

    async def _link_project_workspace(
        self,
        project_service: Any,
        *,
        project_payload: Optional[Dict[str, Any]],
        paper: Any,
        workspace: Any,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(project_payload, dict) or not project_payload.get("id") or workspace is None:
            return project_payload
        project = await project_service.link_workspace(
            project_id=int(project_payload["id"]),
            user_id=self.user_id,
            workspace_id=int(workspace.id),
            paper_id=int(paper.id),
            role="primary_reproduction",
        )
        if project is None:
            return project_payload
        refreshed = await project_service.get_project_payload(project_id=int(project.id), user_id=self.user_id)
        return refreshed or project_payload

    @staticmethod
    def _bool_like(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    @classmethod
    def _intake_summary_from_workspace(cls, workspace: Any) -> Dict[str, Any]:
        if workspace is None:
            return {
                "has_workspace": False,
                "has_llm_intake": False,
                "has_markdown": False,
                "source_mode": None,
                "extractor": None,
                "total_chars": None,
                "sent_chars": None,
                "truncated": None,
                "error": None,
                "complete": False,
                "needs_refresh": False,
            }

        spec = dict(getattr(workspace, "experiment_spec_json", {}) or {})
        summary = dict(getattr(workspace, "summary_json", {}) or {})
        intake_status = dict(spec.get("intake_status") or {})
        input_info = dict(intake_status.get("input") or summary.get("paper_llm_input") or {})
        markdown_info = dict(intake_status.get("markdown") or summary.get("paper_markdown_meta") or {})
        readiness = dict(summary.get("readiness") or {})
        error = intake_status.get("error") or summary.get("paper_intake_error")
        has_llm_intake = cls._bool_like(intake_status.get("has_llm_intake")) or bool(summary.get("paper_intake"))
        sent_chars = input_info.get("sent_chars", markdown_info.get("sent_chars"))
        total_chars = input_info.get("total_chars", markdown_info.get("total_chars"))
        has_markdown = (
            cls._bool_like(readiness.get("has_markdown"))
            or bool(sent_chars)
            or bool(total_chars)
            or bool(input_info.get("source_mode") or markdown_info.get("source_mode"))
        )
        complete = bool(has_llm_intake and not error)
        return {
            "has_workspace": True,
            "has_llm_intake": bool(has_llm_intake),
            "has_markdown": bool(has_markdown),
            "source_mode": input_info.get("source_mode") or markdown_info.get("source_mode"),
            "extractor": input_info.get("extractor") or markdown_info.get("extractor"),
            "total_chars": total_chars,
            "sent_chars": sent_chars,
            "truncated": input_info.get("truncated", markdown_info.get("truncated")),
            "error": str(error) if error else None,
            "complete": complete,
            "needs_refresh": bool(not complete),
        }

    @classmethod
    def _workspace_needs_intake_refresh(cls, workspace: Any) -> bool:
        return bool(cls._intake_summary_from_workspace(workspace).get("needs_refresh"))

    @classmethod
    def _workspace_missing_required_archives(cls, workspace_dir: Path) -> bool:
        required_paths = (
            "paper_intake_result.json",
            "experiment_spec.json",
            "workspace_adapter_manifest.json",
        )
        root = Path(workspace_dir)
        return any(not (root / rel_path).is_file() for rel_path in required_paths)

    @staticmethod
    def _project_not_found(project_id: int) -> ToolResult:
        return ToolResult(
            success=False,
            output=f"没有找到可用 Project（project_id={int(project_id)}）。",
            error="project_not_found",
            data={"project_id": int(project_id)},
        )

    @classmethod
    def _normalize_relative_path(cls, value: Any) -> str:
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            return ""
        if raw.startswith("/"):
            return ""
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            return ""
        return "/".join(parts)

    @classmethod
    def _artifact_spec_for_path(cls, relative_path: str) -> Optional[Dict[str, str]]:
        normalized = cls._normalize_relative_path(relative_path)
        if not normalized:
            return None
        spec = cls._CANONICAL_FILE_SPECS.get(normalized)
        if spec is None:
            return None
        return {"relative_path": normalized, **dict(spec)}

    @classmethod
    def _root_descriptor(cls, *, project_payload: Dict[str, Any], workspace: Any) -> Dict[str, Any]:
        return {
            "project_id": int(project_payload.get("id") or 0),
            "workspace_id": int(workspace.id),
            "notebook_id": str(workspace.notebook_id or ""),
            "root_alias": cls._PROJECT_ROOT_ALIAS,
            "root_relative_prefix": ".",
        }

    async def _resolve_project_workspace(
        self,
        db: AsyncSession,
        *,
        project_id: int,
    ) -> tuple[Optional[Dict[str, Any]], Any]:
        from app.models.literature import PaperExperimentWorkspace
        from app.services.project_service import ProjectService

        project_payload = await ProjectService(db).get_project_payload(project_id=int(project_id), user_id=self.user_id)
        if not isinstance(project_payload, dict):
            return None, None

        workspace_payload = dict(project_payload.get("primary_workspace") or {})
        if not workspace_payload:
            workspaces = list(project_payload.get("workspaces") or [])
            if workspaces:
                workspace_payload = dict(workspaces[0] or {})
        workspace_id = int(workspace_payload.get("id") or 0)
        if workspace_id <= 0:
            return project_payload, None

        result = await db.execute(
            select(PaperExperimentWorkspace).where(
                PaperExperimentWorkspace.id == workspace_id,
                PaperExperimentWorkspace.user_id == self.user_id,
            )
        )
        workspace = result.scalar_one_or_none()
        return project_payload, workspace

    @staticmethod
    def _workspace_not_ready(project_payload: Optional[Dict[str, Any]], project_id: int) -> ToolResult:
        project_url = None
        if isinstance(project_payload, dict) and project_payload.get("id") is not None:
            project_url = f"/projects/{int(project_payload['id'])}"
        lines = [
            f"Project 已存在，但还没有可用 workspace（project_id={int(project_id)}）。",
            "请先调用 paper_research_prepare，确保 planning 产物和 workspace 已创建。",
        ]
        if project_url:
            lines.insert(1, f"- Project: {project_url}")
        return ToolResult(
            success=False,
            output="\n".join(lines),
            error="workspace_not_ready",
            data={
                "project_id": int(project_id),
                "project": _PaperResearchToolBase._project_payload(project_payload),
            },
        )

    def _workspace_dir_for(self, workspace: Any) -> Path:
        from app.services.notebook_workspace_service import ensure_notebook_workspace

        return Path(ensure_notebook_workspace(str(workspace.notebook_id), self.user_id))

    @classmethod
    def _artifact_actual_path(cls, workspace_dir: Path, relative_path: str) -> Optional[Path]:
        spec = cls._artifact_spec_for_path(relative_path)
        if spec is None:
            return None
        return workspace_dir / str(spec["actual_rel_path"])

    @staticmethod
    def _read_json_file(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(content) if isinstance(content, dict) else {}

    @classmethod
    def _classify_magic_bytes(cls, head_bytes: bytes, content_type: str) -> str:
        normalized_type = str(content_type or "").lower()
        if head_bytes.startswith(b"\x89HDF\r\n\x1a\n"):
            return "hdf5"
        if head_bytes.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return "zip"
        if head_bytes.startswith(b"\x1f\x8b"):
            return "gzip"
        if "json" in normalized_type:
            return "json"
        if "html" in normalized_type:
            return "html"
        if "text/" in normalized_type:
            return "text"
        if head_bytes.startswith((b"{", b"[")):
            return "json"
        if head_bytes.startswith((b"<!DOCTYPE html", b"<html", b"<HTML")):
            return "html"
        try:
            decoded = head_bytes.decode("utf-8")
        except UnicodeDecodeError:
            decoded = ""
        if decoded and all((ord(ch) >= 32 or ch in "\r\n\t") for ch in decoded):
            return "text"
        return "binary" if head_bytes else "unknown"

    @classmethod
    def _probe_url_diagnosis(
        cls,
        *,
        status_code: Optional[int],
        content_length: Optional[int],
        detected_kind: str,
        expected_kind: str,
        head_bytes: bytes,
    ) -> tuple[bool, bool, str, str]:
        normalized_expected = str(expected_kind or "auto").strip().lower() or "auto"
        if status_code is None:
            return False, False, "request_failed", "do_not_execute_download"
        if status_code >= 400:
            return False, False, f"http_{status_code}", "diagnose_official_source_failure"
        if status_code == 202 and not head_bytes and int(content_length or 0) == 0:
            return False, False, "accepted_but_empty", "diagnose_official_source_failure"
        if int(content_length or 0) == 0 and not head_bytes:
            return False, False, "empty_response", "do_not_execute_download"

        downloadable = bool(head_bytes or int(content_length or 0) > 0)
        ok = 200 <= status_code < 300 and downloadable

        if normalized_expected == "auto":
            if detected_kind == "html":
                return ok, downloadable, "html_page", "use_as_reference_page"
            if detected_kind == "hdf5":
                return ok, downloadable, "valid_hdf5", "use_as_official_source"
            if detected_kind in {"zip", "gzip", "binary", "json", "text"}:
                return ok, downloadable, f"valid_{detected_kind}", "use_as_official_source"
            return ok, downloadable, "response_observed", "inspect_before_execute"

        kind_aliases = {
            "file": {"hdf5", "zip", "gzip", "binary", "json", "text"},
            "hdf5": {"hdf5"},
            "zip": {"zip"},
            "json": {"json"},
            "text": {"text"},
            "html": {"html"},
        }
        expected_kinds = kind_aliases.get(normalized_expected, {normalized_expected})
        if detected_kind in expected_kinds:
            return ok, downloadable, f"valid_{detected_kind}", "use_as_official_source"
        if detected_kind == "html" and normalized_expected in {"file", "hdf5", "zip"}:
            return False, downloadable, "html_landing_page_for_file", "diagnose_official_source_failure"
        return False, downloadable, f"unexpected_content:{detected_kind}", "inspect_before_execute"

    @staticmethod
    def _parse_git_ls_remote(stdout: str) -> Dict[str, Any]:
        default_branch = None
        head_sha = None
        for raw_line in str(stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("ref: ") and line.endswith("\tHEAD"):
                ref = line.removeprefix("ref: ").split("\t", 1)[0].strip()
                default_branch = ref.removeprefix("refs/heads/")
                continue
            if line.endswith("\tHEAD"):
                head_sha = line.split("\t", 1)[0].strip() or None
        return {
            "default_branch": default_branch,
            "head_sha": head_sha,
        }

    @classmethod
    def _repo_local_signals(cls, workspace_dir: Path) -> Dict[str, Any]:
        repo_dir = workspace_dir / "paper_repo"
        if not repo_dir.is_dir():
            return {
                "materialized": False,
                "readme_present": False,
                "license_present": False,
            }
        repo_files = cls._repo_file_set(workspace_dir)
        readme_present = any(path.lower().startswith("readme") for path in repo_files)
        license_present = any(path.lower().startswith("license") or path.lower().startswith("copying") for path in repo_files)
        return {
            "materialized": True,
            "readme_present": bool(readme_present),
            "license_present": bool(license_present),
        }

    @classmethod
    def _build_artifact_manifest(cls, *, project_payload: Dict[str, Any], workspace: Any, workspace_dir: Path) -> Dict[str, Any]:
        artifacts: List[Dict[str, Any]] = []
        for relative_path, spec in cls._CANONICAL_FILE_SPECS.items():
            actual_path = workspace_dir / str(spec["actual_rel_path"])
            entry: Dict[str, Any] = {
                "kind": str(spec["kind"]),
                "name": str(spec["name"]),
                "relative_path": relative_path,
                "content_type": str(spec["content_type"]),
                "exists": actual_path.is_file(),
            }
            if actual_path.is_file():
                try:
                    entry["size_bytes"] = int(actual_path.stat().st_size)
                except OSError:
                    entry["size_bytes"] = None
            artifacts.append(entry)

        repo_reference_path = workspace_dir / "repo_reference.json"
        repo_source_dir = workspace_dir / "paper_repo"
        repo_status = "missing"
        repo_url = ""
        if repo_reference_path.is_file():
            try:
                repo_reference = json.loads(repo_reference_path.read_text(encoding="utf-8"))
            except Exception:
                repo_reference = {}
            repo_status = str(repo_reference.get("status") or "missing")
            repo_url = str(repo_reference.get("repo_url") or "").strip()

        return {
            **cls._root_descriptor(project_payload=project_payload, workspace=workspace),
            "project": cls._project_payload(project_payload),
            "artifacts": artifacts,
            "repo": {
                "available": bool(repo_source_dir.exists() and repo_source_dir.is_dir()),
                "status": repo_status,
                "repo_url": repo_url or None,
                "root_relative_path": "repo/source",
            },
        }

    @staticmethod
    def _read_text_preview(path: Path, *, max_chars: int) -> Dict[str, Any]:
        content = path.read_text(encoding="utf-8", errors="ignore")
        clipped = content[:max_chars]
        return {
            "content": clipped,
            "truncated": len(content) > len(clipped),
            "total_chars": len(content),
            "returned_chars": len(clipped),
        }

    @classmethod
    def _repo_file_set(cls, workspace_dir: Path) -> Set[str]:
        index_path = workspace_dir / "repo_file_index.json"
        files: Set[str] = set()
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                index = {}
            for item in list(index.get("files") or []):
                normalized = cls._normalize_relative_path(item)
                if normalized:
                    if any(part in _REPO_SKIPPED_DIRS for part in normalized.split("/")):
                        continue
                    files.add(normalized)
        repo_dir = workspace_dir / "paper_repo"
        if repo_dir.is_dir():
            for path in repo_dir.rglob("*"):
                if path.is_file():
                    try:
                        relative_path = str(path.relative_to(repo_dir)).replace("\\", "/")
                        if any(part in _REPO_SKIPPED_DIRS for part in relative_path.split("/")):
                            continue
                        files.add(relative_path)
                    except ValueError:
                        continue
        return files

    @classmethod
    def _normalize_implementation_spec_payload(
        cls,
        payload: Dict[str, Any],
        *,
        workspace_dir: Path,
        runtime_inspection: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized = dict(payload or {})
        repo_files = cls._repo_file_set(workspace_dir)
        runtime_payload = dict(runtime_inspection or {})
        detected_repo_root = str(dict(runtime_payload.get("repo") or {}).get("detected_root_relative_path") or "").strip() or "repo/source"
        normalized["repo_root_relative_path"] = detected_repo_root

        repo_evidence = dict(normalized.get("repo_evidence") or {})
        dataset_structure = dict(repo_evidence.get("dataset_structure") or {})
        expected_dataset_files = [
            cls._normalize_relative_path(item)
            for item in list(dataset_structure.get("expected_files") or [])
            if cls._normalize_relative_path(item)
        ]
        existing_dataset_files = [item for item in expected_dataset_files if item in repo_files]
        missing_dataset_files = [item for item in expected_dataset_files if item not in repo_files]
        dataset_ready = bool(expected_dataset_files) and not missing_dataset_files
        if expected_dataset_files:
            dataset_structure["existing_files"] = existing_dataset_files
            dataset_structure["missing_files"] = missing_dataset_files
            dataset_structure["current_status"] = "present_in_repo" if dataset_ready else ("partial" if existing_dataset_files else str(dataset_structure.get("current_status") or "missing"))
            repo_evidence["dataset_structure"] = dataset_structure
            normalized["repo_evidence"] = repo_evidence

        runtime_requirements = dict(normalized.get("runtime_requirements") or {})
        worker_environment = dict(dict(runtime_payload.get("runtime_worker") or {}).get("environment") or {})
        worker_packages = dict(worker_environment.get("packages") or {})
        package_aliases = {
            "sklearn": "scikit-learn",
            "scikit_learn": "scikit-learn",
            "jupyter_repo2docker": "jupyter-repo2docker",
        }
        missing_runtime_packages: List[str] = []
        for item in list(runtime_requirements.get("dependencies") or []):
            package_name = str(item or "").strip()
            if not package_name:
                continue
            normalized_name = package_aliases.get(package_name.replace("-", "_").lower(), package_name)
            package_payload = dict(worker_packages.get(normalized_name) or worker_packages.get(package_name) or {})
            if not bool(package_payload.get("installed")):
                missing_runtime_packages.append(package_name)
        if missing_runtime_packages:
            runtime_requirements["missing_packages"] = missing_runtime_packages
        elif "missing_packages" in runtime_requirements:
            runtime_requirements["missing_packages"] = []
        if runtime_requirements:
            normalized["runtime_requirements"] = runtime_requirements

        runtime_candidates = [item for item in list(runtime_payload.get("runtime_candidates") or []) if isinstance(item, dict)]
        blockers = normalized.get("blockers")
        if isinstance(blockers, list):
            normalized_blockers: List[Any] = []
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    normalized_blockers.append(blocker)
                    continue
                blocker_type = str(blocker.get("type") or "").strip().lower()
                if blocker_type == "dataset_missing" and dataset_ready:
                    continue
                if blocker_type == "runtime_unknown" and runtime_candidates:
                    continue
                normalized_blockers.append(blocker)
            has_runtime_packages_blocker = any(
                isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "runtime_packages_missing"
                for item in normalized_blockers
            )
            if missing_runtime_packages and not has_runtime_packages_blocker:
                normalized_blockers.append(
                    {
                        "type": "runtime_packages_missing",
                        "description": "Runtime worker is available, but required project packages are still missing.",
                        "severity": "medium",
                        "action_required": "Install missing packages before baseline execution.",
                        "packages": missing_runtime_packages,
                    }
                )
            normalized["blockers"] = normalized_blockers

        next_actions = normalized.get("next_actions")
        if isinstance(next_actions, list):
            normalized_actions: List[Any] = []
            for action in next_actions:
                action_text = str(action or "").strip()
                lowered = action_text.lower()
                if dataset_ready and "download dataset" in lowered:
                    continue
                if runtime_candidates and "check python environment" in lowered:
                    continue
                normalized_actions.append(action)
            if missing_runtime_packages:
                missing_text = ", ".join(missing_runtime_packages)
                install_action = f"Install missing project packages before baseline: {missing_text}"
                if install_action not in [str(item) for item in normalized_actions]:
                    normalized_actions.insert(0, install_action)
            normalized["next_actions"] = normalized_actions

        return normalized

    @classmethod
    def _validate_run_drafts_payload(cls, payload: Dict[str, Any], *, workspace_dir: Path) -> List[str]:
        errors: List[str] = []
        drafts = payload.get("drafts")
        if not isinstance(drafts, list) or not drafts:
            return ["`drafts` must be a non-empty list."]

        repo_files = cls._repo_file_set(workspace_dir)
        canonical_paths = set(cls._CANONICAL_FILE_SPECS.keys())
        for index, draft in enumerate(drafts):
            prefix = f"drafts[{index}]"
            if not isinstance(draft, dict):
                errors.append(f"{prefix} must be an object.")
                continue

            kind = str(draft.get("kind") or "").strip()
            if kind not in cls._RUN_DRAFT_KINDS:
                errors.append(
                    f"{prefix}.kind must be one of {sorted(cls._RUN_DRAFT_KINDS)}, got `{kind}`."
                )

            entrypoint = draft.get("entrypoint")
            if not isinstance(entrypoint, dict):
                errors.append(f"{prefix}.entrypoint must be an object, not a string or array.")
                continue

            entrypoint_type = str(entrypoint.get("type") or "").strip()
            if entrypoint_type not in cls._RUN_DRAFT_ENTRYPOINT_TYPES:
                errors.append(
                    f"{prefix}.entrypoint.type must be one of {sorted(cls._RUN_DRAFT_ENTRYPOINT_TYPES)}, got `{entrypoint_type}`."
                )
            path_or_hint = str(entrypoint.get("path_or_hint") or "").strip()
            if entrypoint_type in {"repo_script", "notebook", "config"}:
                repo_relative_path = cls._normalize_relative_path(path_or_hint)
                if not repo_relative_path:
                    errors.append(f"{prefix}.entrypoint.path_or_hint must be a repo-relative path.")
                elif repo_relative_path not in repo_files:
                    errors.append(
                        f"{prefix}.entrypoint.path_or_hint references missing repo file `{repo_relative_path}`. "
                        "Use readme_command/dataset_step/manual_step for README-only actions."
                    )
                else:
                    entrypoint["path_or_hint"] = repo_relative_path
                    entrypoint["verified"] = True
            elif entrypoint_type in {"readme_command", "dataset_step", "manual_step"}:
                if not path_or_hint:
                    errors.append(f"{prefix}.entrypoint.path_or_hint is required for `{entrypoint_type}`.")
                entrypoint["verified"] = False

            evidence_files = draft.get("evidence_files")
            if not isinstance(evidence_files, list) or not evidence_files:
                errors.append(f"{prefix}.evidence_files must list at least one archived evidence path.")
                continue
            for evidence in evidence_files:
                evidence_path = cls._normalize_relative_path(evidence)
                if not evidence_path:
                    errors.append(f"{prefix}.evidence_files contains an invalid path `{evidence}`.")
                    continue
                if evidence_path.startswith("repo/source/"):
                    repo_path = evidence_path.removeprefix("repo/source/")
                    if repo_path not in repo_files:
                        errors.append(f"{prefix}.evidence_files references missing repo file `{evidence_path}`.")
                elif evidence_path not in canonical_paths:
                    errors.append(
                        f"{prefix}.evidence_files references non-canonical artifact `{evidence_path}`."
                    )

        return errors

    @classmethod
    def _normalize_run_drafts_payload(cls, payload: Dict[str, Any], *, workspace_dir: Path) -> Dict[str, Any]:
        normalized = dict(payload or {})
        drafts = normalized.get("drafts")
        if not isinstance(drafts, list):
            return normalized

        repo_files = cls._repo_file_set(workspace_dir)
        canonical_paths = set(cls._CANONICAL_FILE_SPECS.keys())
        normalized_drafts: List[Dict[str, Any]] = []

        for item in drafts:
            if not isinstance(item, dict):
                normalized_drafts.append(item)
                continue

            draft = dict(item)
            if not str(draft.get("id") or "").strip() and str(draft.get("draft_id") or "").strip():
                draft["id"] = str(draft.get("draft_id") or "").strip()
            if not str(draft.get("title") or "").strip() and str(draft.get("label") or "").strip():
                draft["title"] = str(draft.get("label") or "").strip()
            if not str(draft.get("objective") or "").strip():
                fallback_objective = str(draft.get("description") or draft.get("goal") or "").strip()
                if fallback_objective:
                    draft["objective"] = fallback_objective
            if (not isinstance(draft.get("params"), dict) or not draft.get("params")) and isinstance(draft.get("changes"), dict):
                draft["params"] = dict(draft.get("changes") or {})
            if not isinstance(draft.get("grounding_notes"), list):
                notes = draft.get("notes")
                if isinstance(notes, list):
                    draft["grounding_notes"] = [str(note).strip() for note in notes if str(note or "").strip()]
                elif str(notes or "").strip():
                    draft["grounding_notes"] = [str(notes).strip()]

            entrypoint = draft.get("entrypoint")
            if isinstance(entrypoint, dict):
                normalized_entrypoint = dict(entrypoint)
                entrypoint_type = str(normalized_entrypoint.get("type") or "").strip()
                if entrypoint_type == "python_script":
                    normalized_entrypoint["type"] = "repo_script"
                if not str(normalized_entrypoint.get("path_or_hint") or "").strip() and str(normalized_entrypoint.get("path") or "").strip():
                    normalized_entrypoint["path_or_hint"] = str(normalized_entrypoint.get("path") or "").strip()
                draft["entrypoint"] = normalized_entrypoint

            evidence_files = draft.get("evidence_files")
            if isinstance(evidence_files, list):
                normalized_evidence_files: List[str] = []
                for evidence in evidence_files:
                    evidence_path = cls._normalize_relative_path(evidence)
                    if not evidence_path:
                        normalized_evidence_files.append(str(evidence))
                        continue
                    if evidence_path in canonical_paths or evidence_path.startswith("repo/source/"):
                        normalized_evidence_files.append(evidence_path)
                        continue
                    if evidence_path in repo_files:
                        normalized_evidence_files.append(f"repo/source/{evidence_path}")
                        continue
                    normalized_evidence_files.append(evidence_path)
                draft["evidence_files"] = normalized_evidence_files

            normalized_drafts.append(draft)

        normalized["drafts"] = normalized_drafts
        return normalized

    @staticmethod
    def _workspace_payload(workspace: Any) -> Optional[Dict[str, Any]]:
        if workspace is None:
            return None
        spec = dict(getattr(workspace, "experiment_spec_json", {}) or {})
        summary = dict(getattr(workspace, "summary_json", {}) or {})
        brief = dict(spec.get("optimization_brief") or {})
        return {
            "id": int(workspace.id),
            "status": str(workspace.status or ""),
            "title": str(workspace.title or ""),
            "notebook_id": workspace.notebook_id,
            "notebook_url": f"/code/{workspace.notebook_id}" if workspace.notebook_id else None,
            "execution_mode": spec.get("execution_mode") or summary.get("execution_mode"),
            "intake_status": dict(spec.get("intake_status") or {}),
            "intake_summary": _PaperResearchToolBase._intake_summary_from_workspace(workspace),
            "task": dict(spec.get("task") or {}),
            "baseline": dict(spec.get("baseline") or {}),
            "datasets": [dict(item) for item in list(spec.get("datasets") or []) if isinstance(item, dict)][:8],
            "metrics": [dict(item) for item in list(spec.get("metrics") or []) if isinstance(item, dict)][:8],
            "safe_knobs": [dict(item) for item in list(spec.get("safe_knobs") or []) if isinstance(item, dict)][:12],
            "optimization_candidates": [
                dict(item) for item in list(spec.get("optimization_candidates") or []) if isinstance(item, dict)
            ][:10],
            "model_swap_candidates": [
                dict(item) for item in list(spec.get("allowed_model_swaps") or []) if isinstance(item, dict)
            ][:8],
            "first_runs": [dict(item) for item in list(brief.get("first_runs") or []) if isinstance(item, dict)][:8],
            "recommended_strategy": brief.get("recommended_strategy"),
            "runs": [
                {
                    "id": int(item.id),
                    "label": str(item.label or ""),
                    "run_kind": str(item.run_kind or ""),
                    "status": str(item.status or ""),
                    "model_name": item.model_name,
                    "params": dict(item.params_json or {}),
                    "metrics": dict(item.metrics_json or {}),
                    "notebook_cell_id": item.notebook_cell_id,
                }
                for item in list(getattr(workspace, "runs", []) or [])[:8]
            ],
        }

    @staticmethod
    def _project_payload(project: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(project, dict) or project.get("id") is None:
            return None
        return {
            "id": int(project["id"]),
            "title": str(project.get("title") or ""),
            "status": str(project.get("status") or ""),
            "goal": project.get("goal"),
            "project_url": f"/projects/{int(project['id'])}",
            "paper_count": int(project.get("paper_count") or 0),
            "workspace_count": int(project.get("workspace_count") or 0),
        }

    @staticmethod
    def _selected_runtime_workspace(
        runtime_overview: Optional[Dict[str, Any]],
        *,
        workspace_id: Optional[int],
    ) -> Dict[str, Any]:
        if not isinstance(runtime_overview, dict):
            return {}
        workspaces = [item for item in list(runtime_overview.get("workspaces") or []) if isinstance(item, dict)]
        if workspace_id is not None:
            selected = next((item for item in workspaces if int(item.get("workspace_id") or 0) == int(workspace_id)), None)
            if selected is not None:
                return selected
        primary_workspace_id = runtime_overview.get("primary_workspace_id")
        if primary_workspace_id is not None:
            selected = next((item for item in workspaces if int(item.get("workspace_id") or 0) == int(primary_workspace_id)), None)
            if selected is not None:
                return selected
        return workspaces[0] if workspaces else {}

    @classmethod
    def _status_summary_from_runtime(
        cls,
        runtime_overview: Optional[Dict[str, Any]],
        *,
        workspace_id: Optional[int],
        project_id: Optional[int],
    ) -> Dict[str, Any]:
        runtime_payload = runtime_overview if isinstance(runtime_overview, dict) else {}
        selected_workspace = cls._selected_runtime_workspace(runtime_overview, workspace_id=workspace_id)
        results = dict(selected_workspace.get("results") or {})
        baseline_status = str(results.get("baseline_status") or "missing").strip().lower() or "missing"
        baseline_execution_id = str(results.get("baseline_execution_id") or "").strip() or None
        tuning_status = str(results.get("tuning_status") or "missing").strip().lower() or "missing"
        compare_status = str(results.get("compare_status") or "missing").strip().lower() or "missing"
        current_stage = str(selected_workspace.get("current_stage") or runtime_payload.get("current_stage") or "planning").strip().lower() or "planning"
        current_status = str(selected_workspace.get("current_status") or runtime_payload.get("current_status") or "draft").strip().lower() or "draft"

        recommended_next_action = "先调用 paper_research_prepare，生成或刷新 structured intake / workspace。"
        running_execution = next(
            (
                item
                for item in list(selected_workspace.get("recent_executions") or [])
                if isinstance(item, dict) and str(item.get("status") or "").strip().lower() in {"pending", "running"}
            ),
            None,
        )
        if isinstance(running_execution, dict):
            execution_id = str(running_execution.get("execution_id") or "").strip() or "<execution_id>"
            recommended_next_action = (
                f"先调用 paper_research_read_execution(project_id={int(project_id or 0)}, execution_id=\"{execution_id}\") "
                "继续观察正在运行的 execution，不要重复启动新的 baseline 或 tuning。"
            )
        elif baseline_status == "completed" and baseline_execution_id:
            if tuning_status == "completed":
                recommended_next_action = (
                    f"先调用 paper_research_read_execution(project_id={int(project_id or 0)}, execution_id=\"{baseline_execution_id}\") "
                    "确认 baseline 指标；若要继续分析调优结果，再读取最近的 tuning execution 或 compare 产物。"
                )
            else:
                recommended_next_action = (
                    f"先调用 paper_research_read_execution(project_id={int(project_id or 0)}, execution_id=\"{baseline_execution_id}\") "
                    "读取 baseline 指标与命令；围绕该 baseline 做 first_tuning，不要回退到 env_setup 或重新跑 baseline。"
                )
        elif current_stage == "planning":
            recommended_next_action = "先调用 paper_research_prepare，准备/刷新论文 intake 与 workspace。"
        elif current_stage in {"implementation_prep", "run_drafts"}:
            recommended_next_action = "优先读取 implementation_spec / run_drafts，确认 repo、数据和 baseline 草案后再启动 execution。"
        elif current_stage == "execution":
            recommended_next_action = "优先读取最新 execution 状态；只有在没有运行中任务时才继续启动 baseline。"

        return {
            "current_stage": current_stage,
            "current_status": current_status,
            "baseline_status": baseline_status,
            "baseline_execution_id": baseline_execution_id,
            "tuning_status": tuning_status,
            "tuning_execution_id": str(results.get("tuning_execution_id") or "").strip() or None,
            "compare_status": compare_status,
            "recommended_next_action": recommended_next_action,
        }

    def _result(
        self,
        *,
        action: str,
        paper: Any,
        project: Optional[Dict[str, Any]],
        workspace: Any,
        extra: Optional[Dict[str, Any]] = None,
        success: bool = True,
    ) -> ToolResult:
        payload: Dict[str, Any] = {
            "workflow": "paper-reproduction",
            "action": action,
            "paper": {
                "id": int(paper.id),
                "title": str(paper.title or ""),
                "year": paper.year,
                "venue": paper.venue,
                "arxiv_id": paper.arxiv_id,
            },
            "project": self._project_payload(project),
            "workspace": self._workspace_payload(workspace),
        }
        if extra:
            payload.update(extra)

        workspace_payload = dict(payload.get("workspace") or {})
        project_payload = dict(payload.get("project") or {})
        intake_summary = dict(payload.get("intake_summary") or workspace_payload.get("intake_summary") or {})
        status_summary = dict(payload.get("status_summary") or {})
        lines = [
            f"论文研究流程步骤完成：{action}",
            f"- 论文: {payload['paper']['title']} (paper_id={payload['paper']['id']})",
        ]
        if project_payload:
            lines.append(f"- Project: {project_payload.get('project_url')} ({project_payload.get('title')})")
        else:
            lines.append("- Project: 暂未创建")
        if workspace_payload:
            lines.append(f"- Workspace: {workspace_payload.get('title')} (id={workspace_payload.get('id')})")
            if workspace_payload.get("notebook_url"):
                lines.append(f"- Notebook: {workspace_payload.get('notebook_url')}")
            if action == "status" and status_summary:
                lines.append(
                    f"- 当前阶段: {status_summary.get('current_stage') or 'unknown'} / {status_summary.get('current_status') or 'unknown'}"
                )
                lines.append(
                    f"- baseline_status: {status_summary.get('baseline_status') or 'missing'}"
                    + (
                        f" (execution_id={status_summary.get('baseline_execution_id')})"
                        if status_summary.get("baseline_execution_id")
                        else ""
                    )
                )
                lines.append(
                    f"- tuning_status: {status_summary.get('tuning_status') or 'missing'}"
                    + (
                        f" (execution_id={status_summary.get('tuning_execution_id')})"
                        if status_summary.get("tuning_execution_id")
                        else ""
                    )
                )
                lines.append(f"- compare_status: {status_summary.get('compare_status') or 'missing'}")
                if status_summary.get("recommended_next_action"):
                    lines.append(f"- recommended_next_action: {status_summary.get('recommended_next_action')}")
            source_mode = intake_summary.get("source_mode") or "unknown"
            extractor = intake_summary.get("extractor") or "unknown"
            sent_chars = intake_summary.get("sent_chars")
            total_chars = intake_summary.get("total_chars")
            truncated = intake_summary.get("truncated")
            char_text = ""
            if sent_chars is not None or total_chars is not None:
                char_text = f", sent_chars={sent_chars or 0}, total_chars={total_chars or 0}"
            lines.append(
                f"- PDF/Markdown 输入: source_mode={source_mode}, extractor={extractor}, truncated={truncated}{char_text}"
            )
            if intake_summary.get("has_llm_intake"):
                lines.append("- Structured intake JSON: available，可作为完成规划复用")
            else:
                lines.append("- Structured intake JSON: missing，当前不是完成品，不能当作已完成规划复用")
            if intake_summary.get("error"):
                lines.append(f"- Intake error: {intake_summary.get('error')}")
            if payload.get("intake_refreshed"):
                lines.append(f"- Intake refresh: 已刷新（{payload.get('intake_refresh_reason') or 'requested'}）")
            elif intake_summary.get("needs_refresh"):
                lines.append("- Intake refresh: 需要刷新；再次 prepare 会重新生成 structured JSON")
            task = dict(workspace_payload.get("task") or {})
            if task.get("task_type") or task.get("problem_statement"):
                lines.append(f"- 任务: {task.get('task_type') or task.get('problem_statement')}")
            optimization_candidates = [
                str((item or {}).get("name") or (item or {}).get("id") or "").strip()
                for item in list(workspace_payload.get("optimization_candidates") or [])
                if isinstance(item, dict) and str((item.get("name") or item.get("id") or "")).strip()
            ]
            if optimization_candidates:
                lines.append(f"- 优化候选: {', '.join(optimization_candidates[:10])}")
        else:
            lines.append("- Workspace: 暂未创建")
        if payload.get("created_run"):
            run = dict(payload["created_run"])
            lines.append(f"- 已创建 run 草案: {run.get('label')} (run_id={run.get('id')})")
        if action == "status":
            lines.append("注意：status 只读取现状，不会自动创建 Project、刷新 intake 或启动训练。")
        else:
            lines.append("注意：该流程只准备计划和草案，不会自动执行训练。")
        return ToolResult(success=success, output="\n".join(lines), data=payload)


class PaperResearchPrepareTool(_PaperResearchToolBase):
    name = "paper_research_prepare"
    input_model = PaperResearchPrepareInput
    description = (
        "论文复现/调优研究流程的准备步骤。根据已保存 paper_id 创建或复用轻量 Project，"
        "按需解析 PDF 生成实验 workspace/notebook 草案，并返回 baseline、指标、可调参数和 first runs。不会执行训练。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "integer", "description": "已保存论文的内部 paper_id，优先提供。"},
            "paper_title": {"type": "string", "description": "没有 paper_id 时用于精确匹配已保存论文标题。"},
            "project_id": {"type": "integer", "description": "已有研究项目 ID；缺省时按论文复用或创建。"},
            "project_title": {"type": "string", "description": "需要新建 Project 时的标题。"},
            "user_goal": {"type": "string", "description": "用户的复现、调优或创新验证目标。"},
            "create_project": {"type": "boolean", "default": True, "description": "没有 Project 时是否创建轻量 Project。"},
            "create_workspace": {"type": "boolean", "default": True, "description": "是否创建/复用实验 workspace 和 notebook 草案。"},
            "refresh_intake": {
                "type": "boolean",
                "default": False,
                "description": "是否强制重新生成 PDF/Markdown -> structured intake JSON；即使为 false，已有 workspace 缺失或失败的 intake 也会自动刷新。",
            },
        },
        "required": [],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.paper_experiment_service import PaperExperimentService
            from app.services.project_service import ProjectService

            paper = await self._resolve_paper(
                db,
                paper_id=kwargs.get("paper_id"),
                paper_title=kwargs.get("paper_title"),
            )
            if paper is None:
                return self._paper_not_found(kwargs.get("paper_id"), kwargs.get("paper_title"))

            project_service = ProjectService(db)
            experiment_service = PaperExperimentService(db)
            project_payload = await self._resolve_project_payload(
                project_service,
                paper=paper,
                project_id=kwargs.get("project_id"),
                project_title=kwargs.get("project_title"),
                user_goal=kwargs.get("user_goal"),
                create_project=bool(kwargs.get("create_project", True)),
            )

            workspace = await experiment_service.get_workspace(paper_id=int(paper.id), user_id=self.user_id)
            intake_before = self._intake_summary_from_workspace(workspace)
            refresh_requested = bool(kwargs.get("refresh_intake", False))
            missing_workspace_archives = (
                workspace is not None
                and bool(kwargs.get("create_workspace", True))
                and self._workspace_missing_required_archives(self._workspace_dir_for(workspace))
            )
            auto_refresh_needed = (
                workspace is not None
                and bool(kwargs.get("create_workspace", True))
                and self._workspace_needs_intake_refresh(workspace)
            )
            intake_refreshed = False
            intake_refresh_reason = None
            if workspace is not None and (refresh_requested or auto_refresh_needed):
                intake_refresh_reason = "explicit refresh_intake=true" if refresh_requested else "existing structured intake missing or failed"
                workspace = await experiment_service.refresh_workspace_intake(paper=paper, workspace=workspace)
                intake_refreshed = True
            elif workspace is not None and missing_workspace_archives:
                from app.services.paper_experiment_adapter_service import PaperExperimentAdapterService

                intake_refresh_reason = "workspace archive files missing"
                summary_json = dict(getattr(workspace, "summary_json", {}) or {})
                experiment_spec_json = dict(getattr(workspace, "experiment_spec_json", {}) or {})
                adapter_manifest = PaperExperimentAdapterService().ensure_workspace_archive_from_existing_state(
                    paper=paper,
                    workspace_dir=self._workspace_dir_for(workspace),
                    summary=summary_json,
                    experiment_spec=experiment_spec_json,
                )
                summary_json["workspace_adapter"] = adapter_manifest
                experiment_spec_json["workspace_adapter"] = adapter_manifest
                workspace.summary_json = summary_json
                workspace.experiment_spec_json = experiment_spec_json
                workspace.updated_at = datetime.utcnow()
                await db.commit()
            elif workspace is None and bool(kwargs.get("create_workspace", True)):
                workspace = await experiment_service.bootstrap_workspace(paper=paper, user_id=self.user_id)
                intake_refresh_reason = "created new workspace and intake"

            project_payload = await self._link_project_workspace(
                project_service,
                project_payload=project_payload,
                paper=paper,
                workspace=workspace,
            )
            return self._result(
                action="prepare",
                paper=paper,
                project=project_payload,
                workspace=workspace,
                extra={
                    "intake_before": intake_before,
                    "intake_summary": self._intake_summary_from_workspace(workspace),
                    "intake_refreshed": intake_refreshed,
                    "intake_refresh_reason": intake_refresh_reason,
                },
            )

        return await self._with_db(_handler)


class PaperResearchStatusTool(_PaperResearchToolBase):
    name = "paper_research_status"
    input_model = PaperResearchStatusInput
    description = "查看论文研究 Project/workspace 状态。只读取状态，不创建 Project，不解析 PDF，不执行训练。"
    parameters = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "integer", "description": "已保存论文的内部 paper_id。"},
            "paper_title": {"type": "string", "description": "没有 paper_id 时用于精确匹配已保存论文标题。"},
            "project_id": {"type": "integer", "description": "已有研究项目 ID。"},
        },
        "required": [],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.paper_experiment_service import PaperExperimentService
            from app.services.project_service import ProjectService

            project_service = ProjectService(db)
            project_payload: Optional[Dict[str, Any]] = None
            paper = None
            if kwargs.get("project_id") is not None:
                project_payload = await project_service.get_project_payload(
                    project_id=int(kwargs["project_id"]),
                    user_id=self.user_id,
                )
                if isinstance(project_payload, dict) and project_payload.get("primary_paper"):
                    primary = dict(project_payload["primary_paper"])
                    paper = await self._resolve_paper(db, paper_id=int(primary["id"]), paper_title=None)
            if paper is None:
                paper = await self._resolve_paper(
                    db,
                    paper_id=kwargs.get("paper_id"),
                    paper_title=kwargs.get("paper_title"),
                )
            if paper is None:
                return self._paper_not_found(kwargs.get("paper_id"), kwargs.get("paper_title"))
            if project_payload is None:
                project_payload = await self._resolve_project_payload(
                    project_service,
                    paper=paper,
                    project_id=None,
                    create_project=False,
                )
            workspace = await PaperExperimentService(db).get_workspace(paper_id=int(paper.id), user_id=self.user_id)
            runtime_overview = None
            project_id = int(project_payload["id"]) if isinstance(project_payload, dict) and project_payload.get("id") is not None else None
            if project_id is not None:
                runtime_overview = await project_service.get_project_runtime_overview(
                    project_id=project_id,
                    user_id=self.user_id,
                )
            workspace_id = int(workspace.id) if workspace is not None and getattr(workspace, "id", None) is not None else None
            status_summary = self._status_summary_from_runtime(
                runtime_overview,
                workspace_id=workspace_id,
                project_id=project_id,
            )
            return self._result(
                action="status",
                paper=paper,
                project=project_payload,
                workspace=workspace,
                extra={
                    "runtime_overview": runtime_overview,
                    "status_summary": status_summary,
                },
            )

        return await self._with_db(_handler)


class PaperResearchCloneRepoTool(_PaperResearchToolBase):
    name = "paper_research_clone_repo"
    input_model = PaperResearchCloneRepoInput
    description = (
        "在当前 Project workspace 中物化或刷新论文关联 repo，并重建 repo_reference/repo_file_index。"
        "这是 repo 获取的原子能力，不会重新跑 PDF intake。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_url": {"type": "string", "description": "可选，显式指定要克隆/替换的 GitHub repo URL。"},
            "refresh": {"type": "boolean", "default": False, "description": "是否强制重拉并替换现有 repo。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.paper_experiment_adapter_service import PaperExperimentAdapterService

            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            experiment_spec = dict(getattr(workspace, "experiment_spec_json", {}) or {})
            result = await PaperExperimentAdapterService().materialize_repo(
                workspace_dir=workspace_dir,
                repo_url=str(kwargs.get("repo_url") or "").strip() or None,
                experiment_spec=experiment_spec,
                refresh=bool(kwargs.get("refresh", False)),
            )

            summary_json = dict(getattr(workspace, "summary_json", {}) or {})
            adapter_manifest = dict(summary_json.get("workspace_adapter") or {})
            adapter_manifest["repo"] = dict(result.get("repo") or {})
            adapter_manifest["repo_index"] = {
                "repo_file_index_file": "repo_file_index.json",
                "readme_excerpt_file": dict(result.get("repo_index") or {}).get("readme_excerpt_file"),
                "indexed_file_count": int(dict(result.get("repo_index") or {}).get("indexed_file_count") or 0),
            }
            summary_json["workspace_adapter"] = adapter_manifest
            workspace.summary_json = summary_json
            workspace.updated_at = datetime.utcnow()
            await db.commit()

            repo = dict(result.get("repo") or {})
            repo_index = dict(result.get("repo_index") or {})
            lines = [
                "已处理 repo materialize。",
                f"- Project: /projects/{project_id}",
                f"- Status: {repo.get('status') or result.get('status')}",
                f"- Repo URL: {repo.get('repo_url') or kwargs.get('repo_url') or 'none'}",
                f"- Indexed files: {repo_index.get('indexed_file_count') or 0}",
                f"- Readme excerpt: {repo_index.get('readme_excerpt_file') or 'none'}",
                f"- Repo history candidates: {repo_index.get('repo_history_candidates_file') or 'none'} ({repo_index.get('history_candidate_count') or 0})",
            ]
            if repo.get("message"):
                lines.append(f"- Message: {repo.get('message')}")
            failed_statuses = {
                "missing",
                "unsupported_host",
                "clone_failed",
                "archive_failed",
                "archive_extract_failed",
                "blocked_existing_repo_url_mismatch",
            }
            return ToolResult(
                success=str(result.get("status") or "") not in failed_statuses,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **result,
                },
                error=None if str(result.get("status") or "") not in failed_statuses else "repo_materialize_failed",
            )

        return await self._with_db(_handler)


class PaperResearchCreateRunDraftTool(_PaperResearchToolBase):
    name = "paper_research_create_run_draft"
    input_model = PaperResearchCreateRunDraftInput
    description = (
        "在已有论文实验 workspace 中创建 baseline 或 variant run 草案，并写入对应 Notebook cell。"
        "只创建草案，不执行训练。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "integer", "description": "已保存论文的内部 paper_id。"},
            "paper_title": {"type": "string", "description": "没有 paper_id 时用于精确匹配已保存论文标题。"},
            "project_id": {"type": "integer", "description": "可选研究项目 ID，用于返回项目入口。"},
            "run_label": {"type": "string", "description": "运行草案名称，例如 baseline-epochs-5。"},
            "run_kind": {"type": "string", "enum": ["baseline", "variant"], "default": "variant"},
            "model_name": {"type": "string", "description": "模型或替换模型名称。"},
            "hypothesis": {"type": "string", "description": "本次 run 要验证的假设。"},
            "params": {"type": "object", "description": "参数覆盖，例如 {\"epochs\": 5, \"learning_rate\": 0.001}。"},
            "variant_spec": {"type": "object", "description": "结构化变体说明。"},
        },
        "required": ["run_label"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.paper_experiment_service import PaperExperimentService
            from app.services.project_service import ProjectService

            paper = await self._resolve_paper(
                db,
                paper_id=kwargs.get("paper_id"),
                paper_title=kwargs.get("paper_title"),
            )
            if paper is None:
                return self._paper_not_found(kwargs.get("paper_id"), kwargs.get("paper_title"))

            experiment_service = PaperExperimentService(db)
            workspace = await experiment_service.get_workspace(paper_id=int(paper.id), user_id=self.user_id)
            if workspace is None:
                return ToolResult(
                    success=False,
                    output="尚未创建实验 workspace。请先调用 paper_research_prepare。",
                    error="workspace_not_found",
                )

            resolved_kind = "baseline" if str(kwargs.get("run_kind") or "") == "baseline" else "variant"
            baseline = next(
                (item for item in list(workspace.runs or []) if str(item.run_kind or "") == "baseline"),
                None,
            )
            run = await experiment_service.create_run(
                workspace=workspace,
                label=str(kwargs["run_label"]),
                run_kind=resolved_kind,
                model_name=kwargs.get("model_name"),
                hypothesis=kwargs.get("hypothesis"),
                params=dict(kwargs.get("params") or {}),
                variant_spec=dict(kwargs.get("variant_spec") or {}),
                base_run_id=(int(baseline.id) if baseline is not None and resolved_kind == "variant" else None),
            )
            refreshed = await experiment_service.get_workspace(paper_id=int(paper.id), user_id=self.user_id)
            project_payload = await self._resolve_project_payload(
                ProjectService(db),
                paper=paper,
                project_id=kwargs.get("project_id"),
                create_project=False,
            )
            created_run = {
                "id": int(run.id),
                "label": str(run.label or ""),
                "run_kind": str(run.run_kind or ""),
                "status": str(run.status or ""),
                "params": dict(run.params_json or {}),
                "variant_spec": dict(run.variant_spec_json or {}),
                "notebook_cell_id": run.notebook_cell_id,
            }
            return self._result(
                action="create_run_draft",
                paper=paper,
                project=project_payload,
                workspace=refreshed or workspace,
                extra={"created_run": created_run},
            )

        return await self._with_db(_handler)


class PaperResearchArtifactManifestTool(_PaperResearchToolBase):
    name = "paper_research_get_artifact_manifest"
    input_model = PaperResearchArtifactManifestInput
    parallel_safe = True
    description = (
        "返回当前 Project 工作区的固定逻辑根和可用 artifact 清单。"
        "所有后续读取都应基于 manifest 提供的 relative_path。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            manifest = self._build_artifact_manifest(
                project_payload=project_payload,
                workspace=workspace,
                workspace_dir=workspace_dir,
            )
            artifact_lines = [
                f"- {item.get('relative_path')} [{('exists' if item.get('exists') else 'missing')}]"
                for item in list(manifest.get("artifacts") or [])
            ]
            lines = [
                "已生成 Project 工作区 artifact manifest。",
                f"- Project: /projects/{project_id}",
                f"- Root alias: {manifest.get('root_alias')}",
                f"- Workspace ID: {manifest.get('workspace_id')}",
                f"- Notebook: /code/{manifest.get('notebook_id')}",
                f"- Repo root: {manifest.get('repo', {}).get('root_relative_path')} (available={manifest.get('repo', {}).get('available')})",
                "- Artifacts:",
                *artifact_lines,
            ]
            return ToolResult(success=True, output="\n".join(lines), data=manifest)

        return await self._with_db(_handler)


class PaperResearchReadArtifactTool(_PaperResearchToolBase):
    name = "paper_research_read_artifact"
    input_model = PaperResearchReadArtifactInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "按固定 relative_path 读取 Project 工作区中的 planning/repo/meta/spec artifact。"
        "不接受任意绝对路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "relative_path": {"type": "string", "description": "manifest 中返回的 artifact 相对路径。"},
            "max_chars": {"type": "integer", "default": 20000, "description": "文本 artifact 最多返回字符数。"},
        },
        "required": ["project_id", "relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            relative_path = self._normalize_relative_path(kwargs.get("relative_path"))
            max_chars = int(kwargs.get("max_chars") or 20000)
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            spec = self._artifact_spec_for_path(relative_path)
            if spec is None:
                allowed = ", ".join(sorted(self._CANONICAL_FILE_SPECS.keys()))
                return ToolResult(
                    success=False,
                    output=(
                        f"不支持的 artifact 路径: `{relative_path}`。"
                        f"请先调用 paper_research_get_artifact_manifest。允许路径: {allowed}"
                    ),
                    error="artifact_path_not_allowed",
                    data={"project_id": project_id, "relative_path": relative_path, "allowed_paths": sorted(self._CANONICAL_FILE_SPECS.keys())},
                )

            workspace_dir = self._workspace_dir_for(workspace)
            actual_path = self._artifact_actual_path(workspace_dir, relative_path)
            if actual_path is None or not actual_path.is_file():
                return ToolResult(
                    success=False,
                    output=f"artifact 不存在: `{relative_path}`。",
                    error="artifact_not_found",
                    data={
                        "project_id": project_id,
                        "relative_path": relative_path,
                        "exists": False,
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    },
                )

            if spec["content_type"] == "json":
                content = json.loads(actual_path.read_text(encoding="utf-8"))
                output_body = json.dumps(content, ensure_ascii=False, indent=2)
                lines = [
                    f"已读取 artifact: {relative_path}",
                    f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                    "- Content type: json",
                    "Content:",
                    output_body,
                ]
                data = {
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "relative_path": relative_path,
                    "exists": True,
                    "content_type": "json",
                    "content": content,
                    "truncated": False,
                }
                return ToolResult(success=True, output="\n".join(lines), data=data)

            text_payload = self._read_text_preview(actual_path, max_chars=max_chars)
            lines = [
                f"已读取 artifact: {relative_path}",
                f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                f"- Content type: {spec['content_type']}",
                f"- Truncated: {text_payload['truncated']}",
                f"- Returned chars: {text_payload['returned_chars']}/{text_payload['total_chars']}",
                "Content:",
                str(text_payload["content"]),
            ]
            data = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "relative_path": relative_path,
                "exists": True,
                "content_type": spec["content_type"],
                **text_payload,
            }
            return ToolResult(success=True, output="\n".join(lines), data=data)

        return await self._with_db(_handler)


class PaperResearchReadRepoFileTool(_PaperResearchToolBase):
    name = "paper_research_read_repo_file"
    input_model = PaperResearchReadRepoFileInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "按 repo/source 下的相对路径读取单个仓库文件。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_relative_path": {"type": "string", "description": "repo/source 下的相对路径，例如 README.md 或 train.py。"},
            "max_chars": {"type": "integer", "default": 20000, "description": "最多返回字符数。"},
        },
        "required": ["project_id", "repo_relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            repo_relative_path = self._normalize_relative_path(kwargs.get("repo_relative_path"))
            max_chars = int(kwargs.get("max_chars") or 20000)
            if not repo_relative_path:
                return ToolResult(success=False, output="repo_relative_path 无效。", error="invalid_repo_relative_path")
            if any(part in _REPO_SKIPPED_DIRS for part in repo_relative_path.split("/")):
                return ToolResult(
                    success=False,
                    output=f"不允许读取内部仓库元数据路径: `repo/source/{repo_relative_path}`。",
                    error="repo_path_blocked",
                    data={"project_id": project_id, "repo_relative_path": repo_relative_path},
                )

            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            repo_dir = workspace_dir / "paper_repo"
            if not repo_dir.is_dir():
                return ToolResult(
                    success=False,
                    output="当前 Project 还没有可用的 repo/source。请先调用 paper_research_prepare 并检查 repo_reference。",
                    error="repo_not_available",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "relative_path": "repo/source",
                    },
                )

            target_path = (repo_dir / repo_relative_path).resolve()
            try:
                target_path.relative_to(repo_dir.resolve())
            except ValueError:
                return ToolResult(
                    success=False,
                    output=f"不允许读取 repo/source 之外的路径: `{repo_relative_path}`。",
                    error="repo_path_out_of_scope",
                    data={"project_id": project_id, "repo_relative_path": repo_relative_path},
                )

            if not target_path.is_file():
                return ToolResult(
                    success=False,
                    output=f"仓库文件不存在: `repo/source/{repo_relative_path}`。",
                    error="repo_file_not_found",
                    data={"project_id": project_id, "repo_relative_path": repo_relative_path},
                )

            text_payload = self._read_text_preview(target_path, max_chars=max_chars)
            relative_path = f"repo/source/{repo_relative_path}"
            data = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "relative_path": relative_path,
                "exists": True,
                **text_payload,
            }
            lines = [
                f"已读取 repo 文件: {relative_path}",
                f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                f"- Truncated: {text_payload['truncated']}",
                f"- Returned chars: {text_payload['returned_chars']}/{text_payload['total_chars']}",
                "Content:",
                str(text_payload["content"]),
            ]
            return ToolResult(success=True, output="\n".join(lines), data=data)

        return await self._with_db(_handler)


class PaperResearchSearchRepoTool(_PaperResearchToolBase):
    name = "paper_research_search_repo"
    input_model = PaperResearchSearchRepoInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "在 repo/source 中按关键词或正则搜索内容，返回文件、行号和片段。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "query": {"type": "string", "description": "搜索词或正则表达式。"},
            "max_results": {"type": "integer", "default": 20, "description": "最多返回多少条匹配。"},
            "case_sensitive": {"type": "boolean", "default": False, "description": "是否大小写敏感。"},
            "is_regex": {"type": "boolean", "default": False, "description": "是否将 query 按正则表达式处理。"},
            "glob": {"type": "string", "description": "可选文件 glob，例如 `*.py` 或 `**/*.ipynb`。"},
        },
        "required": ["project_id", "query"],
    }

    @staticmethod
    def _decode_rg_value(payload: Any) -> str:
        if isinstance(payload, dict):
            text_value = payload.get("text")
            if isinstance(text_value, str):
                return text_value
            bytes_value = payload.get("bytes")
            if isinstance(bytes_value, str):
                try:
                    return base64.b64decode(bytes_value).decode("utf-8", errors="ignore")
                except Exception:
                    return ""
        if isinstance(payload, str):
            return payload
        return ""

    @staticmethod
    def _normalize_line_preview(value: str, *, limit: int = 240) -> str:
        text = str(value or "").replace("\r", "").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(40, limit - 3)].rstrip()}..."

    @classmethod
    async def _search_with_rg(
        cls,
        *,
        repo_dir: Path,
        query: str,
        max_results: int,
        case_sensitive: bool,
        is_regex: bool,
        glob: Optional[str],
    ) -> Dict[str, Any]:
        if not shutil.which("rg"):
            return {"available": False, "engine": "rg"}

        command = [
            "rg",
            "--json",
            "--line-number",
            "--color",
            "never",
            "--hidden",
            "--glob",
            "!.git",
            "--threads",
            "1",
        ]
        if case_sensitive:
            command.append("--case-sensitive")
        else:
            command.append("--ignore-case")
        if not is_regex:
            command.append("--fixed-strings")
        if glob:
            command.extend(["--glob", str(glob)])
        command.extend([query, "."])

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        matches: List[Dict[str, Any]] = []
        matched_files: Set[str] = set()
        truncated = False
        parse_errors = 0
        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="ignore"))
                except Exception:
                    parse_errors += 1
                    continue
                if str(event.get("type") or "") != "match":
                    continue
                data = dict(event.get("data") or {})
                repo_relative_path = cls._normalize_relative_path(cls._decode_rg_value(data.get("path")))
                if not repo_relative_path:
                    continue
                line_number = int(data.get("line_number") or 0)
                line_text = cls._normalize_line_preview(cls._decode_rg_value(data.get("lines")))
                submatches = [
                    cls._normalize_line_preview(cls._decode_rg_value(item.get("match")), limit=120)
                    for item in list(data.get("submatches") or [])
                    if cls._decode_rg_value(item.get("match"))
                ]
                matched_files.add(repo_relative_path)
                matches.append(
                    {
                        "repo_relative_path": repo_relative_path,
                        "relative_path": f"repo/source/{repo_relative_path}",
                        "line_number": line_number,
                        "line_text": line_text,
                        "submatches": submatches,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    process.terminate()
                    break
        finally:
            try:
                await asyncio.wait_for(process.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()

        return {
            "available": True,
            "engine": "rg",
            "matches": matches,
            "matched_files": sorted(matched_files),
            "returned_matches": len(matches),
            "truncated": truncated,
            "parse_errors": parse_errors,
            "returncode": process.returncode,
        }

    @classmethod
    def _search_with_python_fallback(
        cls,
        *,
        repo_dir: Path,
        repo_files: Sequence[str],
        query: str,
        max_results: int,
        case_sensitive: bool,
        is_regex: bool,
        glob: Optional[str],
    ) -> Dict[str, Any]:
        matches: List[Dict[str, Any]] = []
        matched_files: Set[str] = set()
        truncated = False
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(query, flags) if is_regex else None
        fixed_query = query if case_sensitive else query.lower()
        parse_errors = 0

        for repo_relative_path in sorted(str(item or "").strip() for item in repo_files if str(item or "").strip()):
            if glob and not fnmatch.fnmatch(repo_relative_path, glob):
                continue
            file_path = repo_dir / repo_relative_path
            if not file_path.is_file():
                continue
            try:
                with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        haystack = line if case_sensitive else line.lower()
                        matched = bool(regex.search(line)) if regex is not None else fixed_query in haystack
                        if not matched:
                            continue
                        matched_files.add(repo_relative_path)
                        matches.append(
                            {
                                "repo_relative_path": repo_relative_path,
                                "relative_path": f"repo/source/{repo_relative_path}",
                                "line_number": line_number,
                                "line_text": cls._normalize_line_preview(line),
                                "submatches": [],
                            }
                        )
                        if len(matches) >= max_results:
                            truncated = True
                            break
                if truncated:
                    break
            except Exception:
                parse_errors += 1
                continue

        return {
            "available": True,
            "engine": "python_fallback",
            "matches": matches,
            "matched_files": sorted(matched_files),
            "returned_matches": len(matches),
            "truncated": truncated,
            "parse_errors": parse_errors,
        }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            query = str(kwargs.get("query") or "").strip()
            max_results = int(kwargs.get("max_results") or 20)
            case_sensitive = bool(kwargs.get("case_sensitive", False))
            is_regex = bool(kwargs.get("is_regex", False))
            glob = str(kwargs.get("glob") or "").strip() or None
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            repo_dir = workspace_dir / "paper_repo"
            if not repo_dir.is_dir():
                return ToolResult(
                    success=False,
                    output="当前 Project 还没有可用的 repo/source。请先调用 paper_research_prepare 并检查 repo_reference。",
                    error="repo_not_available",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "relative_path": "repo/source",
                    },
                )

            repo_files = sorted(self._repo_file_set(workspace_dir))
            if not repo_files:
                repo_files = [
                    self._normalize_relative_path(path.relative_to(repo_dir))
                    for path in repo_dir.rglob("*")
                    if path.is_file()
                ]

            try:
                rg_payload = await self._search_with_rg(
                    repo_dir=repo_dir,
                    query=query,
                    max_results=max_results,
                    case_sensitive=case_sensitive,
                    is_regex=is_regex,
                    glob=glob,
                )
                if bool(rg_payload.get("available")) and int(rg_payload.get("returncode") or 0) in {0, 1, -15}:
                    search_payload = rg_payload
                else:
                    search_payload = self._search_with_python_fallback(
                        repo_dir=repo_dir,
                        repo_files=repo_files,
                        query=query,
                        max_results=max_results,
                        case_sensitive=case_sensitive,
                        is_regex=is_regex,
                        glob=glob,
                    )
            except re.error as exc:
                return ToolResult(
                    success=False,
                    output=f"搜索正则无效: {exc}",
                    error="invalid_search_regex",
                    data={"project_id": project_id, "query": query, "glob": glob},
                )

            matches = list(search_payload.get("matches") or [])
            matched_files = list(search_payload.get("matched_files") or [])
            result_lines = [
                f"- {item.get('relative_path')}:{item.get('line_number')} | {item.get('line_text')}"
                for item in matches
            ]
            lines = [
                "已搜索 repo/source。",
                f"- Project: /projects/{project_id}",
                f"- Engine: {search_payload.get('engine')}",
                f"- Query: {query}",
                f"- Regex: {is_regex}",
                f"- Case sensitive: {case_sensitive}",
                f"- Glob: {glob or 'none'}",
                f"- Matched files: {len(matched_files)}",
                f"- Returned matches: {len(matches)}/{max_results}",
                f"- Truncated: {bool(search_payload.get('truncated'))}",
                "- Matches:",
                *(result_lines or ["- none"]),
            ]
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "query": query,
                    "glob": glob,
                    "case_sensitive": case_sensitive,
                    "is_regex": is_regex,
                    "engine": str(search_payload.get("engine") or "unknown"),
                    "total_repo_files": len(repo_files),
                    "matched_file_count": len(matched_files),
                    "returned_matches": len(matches),
                    "truncated": bool(search_payload.get("truncated")),
                    "matches": matches,
                },
            )

        return await self._with_db(_handler)


class PaperResearchWriteImplementationSpecTool(_PaperResearchToolBase):
    name = "paper_research_write_implementation_spec"
    input_model = PaperResearchWriteImplementationSpecInput
    description = "将 implementation-ready JSON 归档到当前 Project 工作区。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "implementation_spec": {"type": "object", "description": "要保存的 implementation spec JSON。"},
        },
        "required": ["project_id", "implementation_spec"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            relative_path = "specs/implementation_spec.json"
            actual_path = workspace_dir / "specs" / "implementation_spec.json"
            payload = dict(kwargs.get("implementation_spec") or {})
            payload.setdefault("schema_version", "implementation_spec_v1")
            payload.setdefault("project_id", int(project_id))
            payload.setdefault("workspace_id", int(workspace.id))
            payload.setdefault("notebook_id", str(workspace.notebook_id or ""))
            payload.setdefault("root_alias", self._PROJECT_ROOT_ALIAS)
            payload.setdefault("repo_root_relative_path", "repo/source")
            from app.services.project_runtime_service import ProjectRuntimeService

            runtime_inspection = await ProjectRuntimeService().inspect_runtime(
                workspace_dir=workspace_dir,
                project_id=project_id,
                workspace_id=int(workspace.id),
                notebook_id=str(workspace.notebook_id or ""),
            )
            payload = self._normalize_implementation_spec_payload(
                payload,
                workspace_dir=workspace_dir,
                runtime_inspection=runtime_inspection,
            )

            actual_path.parent.mkdir(parents=True, exist_ok=True)
            actual_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            data = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "relative_path": relative_path,
                "saved": True,
                "content": payload,
            }
            lines = [
                "已写入 implementation spec。",
                f"- Project: /projects/{project_id}",
                f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                f"- Relative path: {relative_path}",
            ]
            return ToolResult(success=True, output="\n".join(lines), data=data)

        return await self._with_db(_handler)


class PaperResearchReadImplementationSpecTool(_PaperResearchToolBase):
    name = "paper_research_read_implementation_spec"
    input_model = PaperResearchReadImplementationSpecInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 已归档的 implementation spec。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "max_chars": {"type": "integer", "default": 20000, "description": "文本输出字符上限；JSON 会完整返回到结构化 data 中。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        return await PaperResearchReadArtifactTool(
            self.db,
            self.user_id,
            db_session_factory=self.db_session_factory,
        ).execute(
            project_id=int(kwargs["project_id"]),
            relative_path="specs/implementation_spec.json",
            max_chars=int(kwargs.get("max_chars") or 20000),
        )


class PaperResearchWriteRunDraftsTool(_PaperResearchToolBase):
    name = "paper_research_write_run_drafts"
    input_model = PaperResearchWriteRunDraftsInput
    description = (
        "校验并归档 implementation-spec 派生出的 run drafts JSON 到当前 Project 工作区。"
        "每个 draft 必须使用当前 schema：id/title/objective/entrypoint{type,path_or_hint}/"
        "depends_on/data_requirements/env_requirements/params/expected_outputs/blockers/evidence_files/grounding_notes。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "run_drafts": {
                "type": "object",
                "description": (
                    "要保存的 run_drafts JSON。entrypoint.type 只能是 repo_script/notebook/config/"
                    "readme_command/dataset_step/manual_step/unknown。"
                    "repo_script/notebook/config 必须提供 repo-relative 的 entrypoint.path_or_hint，例如 seq2seq.py。"
                    "evidence_files 必须是 canonical artifact 路径，例如 repo/source/seq2seq.py 或 specs/implementation_spec.json。"
                ),
            },
        },
        "required": ["project_id", "run_drafts"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            relative_path = "drafts/run_drafts.json"
            actual_path = workspace_dir / "drafts" / "run_drafts.json"
            payload = dict(kwargs.get("run_drafts") or {})
            payload.setdefault("schema_version", "run_drafts_v1")
            payload.setdefault("project_id", int(project_id))
            payload.setdefault("workspace_id", int(workspace.id))
            payload.setdefault("notebook_id", str(workspace.notebook_id or ""))
            payload.setdefault("root_alias", self._PROJECT_ROOT_ALIAS)
            payload.setdefault("implementation_spec_relative_path", "specs/implementation_spec.json")
            payload.setdefault("repo_root_relative_path", "repo/source")
            payload = self._normalize_run_drafts_payload(payload, workspace_dir=workspace_dir)

            validation_errors = self._validate_run_drafts_payload(payload, workspace_dir=workspace_dir)
            if validation_errors:
                return ToolResult(
                    success=False,
                    output=(
                        "run_drafts JSON 未通过归档校验，未写入文件。\n"
                        + "\n".join(f"- {item}" for item in validation_errors[:12])
                    ),
                    error="run_drafts_schema_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "relative_path": relative_path,
                        "saved": False,
                        "validation_errors": validation_errors,
                        "allowed_kinds": sorted(self._RUN_DRAFT_KINDS),
                        "allowed_entrypoint_types": sorted(self._RUN_DRAFT_ENTRYPOINT_TYPES),
                        "required_draft_fields": [
                            "id",
                            "kind",
                            "title",
                            "objective",
                            "entrypoint",
                            "depends_on",
                            "data_requirements",
                            "env_requirements",
                            "params",
                            "expected_outputs",
                            "blockers",
                            "evidence_files",
                            "grounding_notes",
                        ],
                        "entrypoint_contract": {
                            "field": "entrypoint.path_or_hint",
                            "repo_relative_example": "seq2seq.py",
                            "canonical_evidence_example": "repo/source/seq2seq.py",
                        },
                    },
                )

            actual_path.parent.mkdir(parents=True, exist_ok=True)
            actual_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            data = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "relative_path": relative_path,
                "saved": True,
                "content": payload,
            }
            lines = [
                "已写入 run drafts。",
                f"- Project: /projects/{project_id}",
                f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                f"- Relative path: {relative_path}",
            ]
            return ToolResult(success=True, output="\n".join(lines), data=data)

        return await self._with_db(_handler)


class PaperResearchReadRunDraftsTool(_PaperResearchToolBase):
    name = "paper_research_read_run_drafts"
    input_model = PaperResearchReadRunDraftsInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 已归档的 run drafts JSON。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "max_chars": {"type": "integer", "default": 20000, "description": "文本输出字符上限；JSON 会完整返回到结构化 data 中。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        return await PaperResearchReadArtifactTool(
            self.db,
            self.user_id,
            db_session_factory=self.db_session_factory,
        ).execute(
            project_id=int(kwargs["project_id"]),
            relative_path="drafts/run_drafts.json",
            max_chars=int(kwargs.get("max_chars") or 20000),
        )


class PaperResearchInspectRuntimeTool(_PaperResearchToolBase):
    name = "paper_research_inspect_runtime"
    input_model = PaperResearchInspectRuntimeInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "扫描当前 Project workspace/repo 的可执行环境信号，返回 devcontainer、Docker、repo2docker、"
        "papermill、plain-python 等 runtime candidates。只检测，不执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            payload = await ProjectRuntimeService().inspect_runtime(
                workspace_dir=workspace_dir,
                project_id=project_id,
                workspace_id=int(workspace.id),
                notebook_id=str(workspace.notebook_id or ""),
            )
            candidate_lines = [
                (
                    f"- {item.get('runtime_type')}: status={item.get('status')}, "
                    f"evidence={', '.join(list(item.get('evidence_files') or [])[:3]) or 'none'}, "
                    f"blockers={', '.join(list(item.get('blockers') or [])) or 'none'}"
                )
                for item in list(payload.get("runtime_candidates") or [])
            ]
            lines = [
                "已扫描 Project runtime candidates。",
                f"- Project: /projects/{project_id}",
                f"- Notebook: /code/{payload.get('notebook_id')}",
                f"- Repo available: {payload.get('repo', {}).get('available')}",
                f"- Detected repo root: {payload.get('repo', {}).get('detected_root_relative_path') or 'unknown'}",
                f"- Runtime worker: {payload.get('runtime_worker', {}).get('available')}",
                "- Runtime candidates:",
                *(candidate_lines or ["- none"]),
            ]
            return ToolResult(success=True, output="\n".join(lines), data=payload)

        return await self._with_db(_handler)


class PaperResearchProbeRepoTool(_PaperResearchToolBase):
    name = "paper_research_probe_repo"
    input_model = PaperResearchProbeRepoInput
    parallel_safe = True
    timeout_seconds = 45.0
    description = (
        "检查官方仓库 URL 是否仍可访问、是否可 clone，并返回默认分支等最小 repo 存活信号。"
        "优先使用当前 Project 已归档的 repo_reference。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_url": {"type": "string", "description": "可选，显式指定要探测的仓库 URL；缺省时优先使用当前 Project 的 repo_reference。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            repo_reference = self._read_json_file(workspace_dir / "repo_reference.json")
            repo_url = str(kwargs.get("repo_url") or repo_reference.get("repo_url") or "").strip()
            if not repo_url:
                return ToolResult(
                    success=False,
                    output="没有可探测的 repo URL。请先提供 repo_url，或先让 Project 归档 repo_reference。",
                    error="repo_url_missing",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "repo_url": None,
                        "source": "missing",
                    },
                )

            parsed = urlparse(repo_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return ToolResult(
                    success=False,
                    output=f"repo URL 无效或不受支持: {repo_url}",
                    error="repo_url_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "repo_url": repo_url,
                    },
                )

            headers = {
                "User-Agent": "Mozilla/5.0 (paper-research-probe)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            status_code: Optional[int] = None
            final_url = repo_url
            page_ok = False
            page_error = None
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
                try:
                    response = await client.get(repo_url)
                    status_code = int(response.status_code)
                    final_url = str(response.url)
                    page_ok = 200 <= response.status_code < 400
                except Exception as exc:  # noqa: BLE001
                    page_error = f"{type(exc).__name__}: {exc}"

            cloneable = False
            git_error = None
            parsed_git: Dict[str, Any] = {}
            try:
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "ls-remote",
                    "--symref",
                    repo_url,
                    "HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    git_error = "git_ls_remote_timeout"
                else:
                    if process.returncode == 0:
                        cloneable = True
                        parsed_git = self._parse_git_ls_remote(stdout.decode("utf-8", errors="ignore"))
                    else:
                        git_error = stderr.decode("utf-8", errors="ignore").strip() or f"git_exit_{process.returncode}"
            except FileNotFoundError:
                git_error = "git_not_available"
            except Exception as exc:  # noqa: BLE001
                git_error = f"{type(exc).__name__}: {exc}"

            local_signals = self._repo_local_signals(workspace_dir)
            diagnosis = "ready" if cloneable else "repo_page_reachable_but_not_cloneable" if page_ok else "repo_unreachable"
            suggested_next_action = (
                "use_as_official_repo"
                if cloneable
                else "diagnose_official_repo_failure"
                if page_ok or status_code in {301, 302, 307, 308}
                else "do_not_execute_clone"
            )
            payload = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "project_id": project_id,
                "repo_url": repo_url,
                "source": "explicit" if str(kwargs.get("repo_url") or "").strip() else "repo_reference",
                "host": parsed.netloc,
                "status_code": status_code,
                "final_url": final_url,
                "reachable": bool(page_ok),
                "cloneable": bool(cloneable),
                "default_branch": parsed_git.get("default_branch"),
                "head_sha": parsed_git.get("head_sha"),
                "readme_present": local_signals.get("readme_present"),
                "license_present": local_signals.get("license_present"),
                "materialized": local_signals.get("materialized"),
                "diagnosis": diagnosis,
                "suggested_next_action": suggested_next_action,
                "page_error": page_error,
                "git_error": git_error,
            }
            lines = [
                "已探测 repo 存活状态。",
                f"- Project: /projects/{project_id}",
                f"- Repo URL: {repo_url}",
                f"- Reachable: {payload['reachable']}",
                f"- Cloneable: {payload['cloneable']}",
                f"- Default branch: {payload.get('default_branch') or 'unknown'}",
                f"- Diagnosis: {diagnosis}",
            ]
            if git_error:
                lines.append(f"- Git error: {git_error}")
            if page_error:
                lines.append(f"- Page error: {page_error}")
            return ToolResult(
                success=bool(cloneable or page_ok),
                output="\n".join(lines),
                data=payload,
                error=None if cloneable or page_ok else "repo_probe_failed",
            )

        return await self._with_db(_handler)


class PaperResearchProbeUrlTool(_PaperResearchToolBase):
    name = "paper_research_probe_url"
    input_model = PaperResearchProbeUrlInput
    parallel_safe = True
    timeout_seconds = 45.0
    description = (
        "对论文流程中的官方下载链接或外部文档链接做轻量测活。"
        "只读取响应头和极小字节片段，不执行真实下载。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "url": {"type": "string", "description": "要探测的 URL。应优先使用 README/论文中给出的官方 URL。"},
            "expected_kind": {
                "type": "string",
                "enum": ["auto", "html", "file", "hdf5", "zip", "json", "text"],
                "default": "auto",
                "description": "期望拿到的内容类型，用于判断官方链接是否已失效或落到错误页面。",
            },
            "read_bytes": {"type": "integer", "default": 64, "description": "最多读取的响应头字节数，用于 magic-bytes 判断。"},
        },
        "required": ["project_id", "url"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            url = str(kwargs.get("url") or "").strip()
            expected_kind = str(kwargs.get("expected_kind") or "auto").strip().lower() or "auto"
            read_bytes = max(int(kwargs.get("read_bytes") or 64), 8)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return ToolResult(
                    success=False,
                    output=f"URL 无效或不受支持: {url}",
                    error="probe_url_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "url": url,
                    },
                )

            headers = {
                "User-Agent": "Mozilla/5.0 (paper-research-probe)",
                "Accept": "*/*",
            }
            head_status = None
            get_status = None
            final_url = url
            content_type = ""
            content_length = None
            head_bytes = b""
            request_error = None
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
                try:
                    head_response = await client.head(url)
                    head_status = int(head_response.status_code)
                    final_url = str(head_response.url)
                    content_type = str(head_response.headers.get("content-type") or "")
                    raw_content_length = str(head_response.headers.get("content-length") or "").strip()
                    content_length = int(raw_content_length) if raw_content_length.isdigit() else None
                except Exception as exc:  # noqa: BLE001
                    request_error = f"HEAD {type(exc).__name__}: {exc}"

                try:
                    async with client.stream("GET", url, headers={"Range": f"bytes=0-{read_bytes - 1}"}) as response:
                        get_status = int(response.status_code)
                        final_url = str(response.url)
                        if not content_type:
                            content_type = str(response.headers.get("content-type") or "")
                        raw_content_length = str(response.headers.get("content-length") or "").strip()
                        if content_length is None and raw_content_length.isdigit():
                            content_length = int(raw_content_length)
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                remaining = read_bytes - len(head_bytes)
                                head_bytes += chunk[:remaining]
                            if len(head_bytes) >= read_bytes:
                                break
                except Exception as exc:  # noqa: BLE001
                    if request_error:
                        request_error = f"{request_error}; GET {type(exc).__name__}: {exc}"
                    else:
                        request_error = f"GET {type(exc).__name__}: {exc}"

            detected_kind = self._classify_magic_bytes(head_bytes, content_type)
            ok, downloadable, diagnosis, suggested_next_action = self._probe_url_diagnosis(
                status_code=get_status or head_status,
                content_length=content_length,
                detected_kind=detected_kind,
                expected_kind=expected_kind,
                head_bytes=head_bytes,
            )
            payload = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "project_id": project_id,
                "url": url,
                "expected_kind": expected_kind,
                "head_status": head_status,
                "status_code": get_status or head_status,
                "final_url": final_url,
                "content_type": content_type or None,
                "content_length": content_length,
                "downloadable": bool(downloadable),
                "ok": bool(ok),
                "detected_kind": detected_kind,
                "magic_bytes_hex": head_bytes[:16].hex() or None,
                "magic_bytes_ascii": head_bytes[:32].decode("utf-8", errors="replace") if head_bytes else "",
                "diagnosis": diagnosis,
                "suggested_next_action": suggested_next_action,
                "request_error": request_error,
            }
            lines = [
                "已探测外部 URL 存活状态。",
                f"- Project: /projects/{project_id}",
                f"- URL: {url}",
                f"- Status: {payload.get('status_code')}",
                f"- Content-Type: {payload.get('content_type') or 'unknown'}",
                f"- Content-Length: {payload.get('content_length') if payload.get('content_length') is not None else 'unknown'}",
                f"- Detected kind: {detected_kind}",
                f"- Downloadable: {payload['downloadable']}",
                f"- Diagnosis: {diagnosis}",
            ]
            if request_error:
                lines.append(f"- Request error: {request_error}")
            return ToolResult(
                success=bool(ok),
                output="\n".join(lines),
                data=payload,
                error=None if ok else "url_probe_failed",
            )

        return await self._with_db(_handler)


class PaperResearchWriteExecutionSpecTool(_PaperResearchToolBase):
    name = "paper_research_write_execution_spec"
    input_model = PaperResearchWriteExecutionSpecInput
    description = (
        "将单次执行计划 execution_spec 归档到 Project workspace。"
        "只写入执行说明，不自动运行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_spec": {
                "type": "object",
                "description": (
                    "单次执行计划。必须包含 runtime_type。可选 execution_id/draft_id/label/cwd/command/"
                    "input_notebook/parameters/expected_outputs/evidence_files/external_dependencies/"
                    "preflight_checks/generated_files。preflight_checks 必须是对象数组，例如 "
                    "[{\"name\":\"check_python\",\"required\":true,\"status\":\"passed\"}]；不要传 "
                    "{\"check_python\": true} 这种 map。generated_files 只能写入 executions、generated 或 tmp 下的"
                    "执行级文件，不能覆盖 repo/source。"
                ),
            },
        },
        "required": ["project_id", "execution_spec"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            try:
                saved = ProjectRuntimeService().write_execution_spec(
                    workspace_dir=workspace_dir,
                    project_id=project_id,
                    workspace_id=int(workspace.id),
                    notebook_id=str(workspace.notebook_id or ""),
                    execution_spec=dict(kwargs.get("execution_spec") or {}),
                )
            except ValueError as exc:
                return ToolResult(
                    success=False,
                    output=f"execution_spec 无效，未写入: {exc}",
                    error="execution_spec_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "saved": False,
                    },
                )

            validation = dict(dict(saved.get("content") or {}).get("validation") or {})
            warning_lines = [f"- {item}" for item in list(validation.get("warnings") or [])]
            lines = [
                "已写入 execution spec。",
                f"- Project: /projects/{project_id}",
                f"- Execution ID: {saved.get('execution_id')}",
                f"- Relative path: {saved.get('relative_path')}",
                f"- Runtime type: {dict(saved.get('content') or {}).get('runtime_type')}",
            ]
            if warning_lines:
                lines.append("- Runtime warnings:")
                lines.extend(warning_lines)
            return ToolResult(success=True, output="\n".join(lines), data=saved)

        return await self._with_db(_handler)


class PaperResearchReadExecutionSpecTool(_PaperResearchToolBase):
    name = "paper_research_read_execution_spec"
    input_model = PaperResearchReadExecutionSpecInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 已归档的单次 execution_spec。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "execution spec ID。"},
        },
        "required": ["project_id", "execution_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            execution_id = str(kwargs.get("execution_id") or "").strip()
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            try:
                content = ProjectRuntimeService().read_execution_spec(
                    workspace_dir=self._workspace_dir_for(workspace),
                    execution_id=execution_id,
                )
            except FileNotFoundError:
                return ToolResult(
                    success=False,
                    output=f"execution spec 不存在: `{execution_id}`。",
                    error="execution_spec_not_found",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "execution_id": execution_id,
                        "exists": False,
                    },
                )
            relative_path = f"executions/{content.get('execution_id')}/execution_spec.json"
            lines = [
                f"已读取 execution spec: {relative_path}",
                f"- Runtime type: {content.get('runtime_type')}",
                "Content:",
                json.dumps(content, ensure_ascii=False, indent=2, default=str),
            ]
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "execution_id": str(content.get("execution_id") or execution_id),
                    "relative_path": relative_path,
                    "exists": True,
                    "content": content,
                },
            )

        return await self._with_db(_handler)


class PaperResearchStartExecutionTool(_PaperResearchToolBase):
    name = "paper_research_start_execution"
    input_model = PaperResearchStartExecutionInput
    description = (
        "启动已归档 execution_spec。当前进程内只直接支持 papermill/plain-python；"
        "Docker/devcontainer/repo2docker 会在缺少 runtime worker 时返回 blocked，不做假执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "execution spec ID。"},
        },
        "required": ["project_id", "execution_id"],
    }

    @staticmethod
    def _execution_summary(spec: Dict[str, Any], execution_id: str) -> Dict[str, Any]:
        draft_id = str(spec.get("draft_id") or "").strip()
        label = str(spec.get("label") or "").strip()
        runtime_type = str(spec.get("runtime_type") or "").strip()
        command = [str(item or "").strip() for item in list(spec.get("command") or []) if str(item or "").strip()]
        command_text = " ".join(command).lower()
        identifiers = " ".join(part for part in [execution_id, draft_id, label] if part).lower()

        display_name = label or draft_id or execution_id
        stage = "execution"
        purpose = "后台执行任务"
        note = ""
        next_action = "完成后继续当前论文任务，我会读取 execution 结果/日志并决定下一步。"

        looks_like_probe = (
            len(command) >= 3
            and command[0].startswith("python")
            and command[1] == "-c"
        )
        env_markers = any(token in identifiers for token in ["env", "deps", "dependency", "probe", "install"])
        if looks_like_probe or env_markers:
            stage = "env_setup"
            purpose = "环境依赖检查或补依赖"
            note = "这一步不是最终的 baseline/tuning 训练，只是在为后续执行清理环境阻塞。"
            next_action = "完成后请继续当前论文任务；我会读取这一步结果，确认是否补依赖成功，再回到 baseline/tuning。"
        elif "baseline" in identifiers:
            stage = "baseline_repro"
            purpose = "baseline 复现实验"
            next_action = "完成后请继续当前论文任务；我会读取指标和日志，判断 baseline 是否成功以及下一步是否进入 tuning。"
        elif any(token in identifiers for token in ["tuning", "compare", "sweep"]):
            stage = "tuning"
            purpose = "调优或对比实验"
            next_action = "完成后请继续当前论文任务；我会读取 tuning 结果并与 baseline 对比。"
        elif any(token in identifiers for token in ["data_prep", "prepare", "preprocess"]):
            stage = "data_prep"
            purpose = "数据准备任务"
            next_action = "完成后请继续当前论文任务；我会确认数据产物是否可用于 baseline。"
        elif runtime_type == "papermill":
            purpose = "Notebook 执行任务"

        if not display_name:
            display_name = execution_id
        return {
            "display_name": display_name,
            "stage": stage,
            "purpose": purpose,
            "note": note,
            "next_action": next_action,
        }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            execution_id = str(kwargs.get("execution_id") or "").strip()
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            service = ProjectRuntimeService()
            spec_content: Dict[str, Any] = {}
            try:
                spec_content = service.read_execution_spec(
                    workspace_dir=self._workspace_dir_for(workspace),
                    execution_id=execution_id,
                )
            except FileNotFoundError:
                spec_content = {}
            try:
                payload = await service.start_execution(
                    project_id=project_id,
                    workspace_id=int(workspace.id),
                    workspace_dir=self._workspace_dir_for(workspace),
                    execution_id=execution_id,
                )
            except FileNotFoundError:
                return ToolResult(
                    success=False,
                    output=f"execution spec 不存在: `{execution_id}`。请先调用 paper_research_write_execution_spec。",
                    error="execution_spec_not_found",
                    data={"project_id": project_id, "execution_id": execution_id},
                )
            status = str(payload.get("status") or "")
            normalized_execution_id = str(payload.get("execution_id") or execution_id)
            background_started = status in {"running", "pending"}
            background_completed = status in {"completed", "failed", "blocked", "cancelled"}
            execution_summary = self._execution_summary(spec_content, normalized_execution_id)
            if (
                background_started
                and self.route_profile == "chat"
                and self.conversation_id is not None
            ):
                try:
                    from app.services.execution_continuation_service import get_execution_continuation_manager

                    await get_execution_continuation_manager().schedule(
                        user_id=self.user_id,
                        conversation_id=self.conversation_id,
                        project_id=project_id,
                        execution_id=normalized_execution_id,
                        stage=str(execution_summary.get("stage") or "").strip(),
                        purpose=str(execution_summary.get("purpose") or "").strip(),
                        active_skill_names=["paper-reproduction"],
                    )
                except Exception as exc:
                    logger.warning(
                        "[PaperResearch] failed to schedule execution continuation: conversation_id={}, execution_id={}, error={}",
                        self.conversation_id,
                        normalized_execution_id,
                        exc,
                    )
            if background_started:
                lines = [
                    "已启动后台 execution。",
                    f"- Project: /projects/{project_id}",
                    f"- Execution ID: {normalized_execution_id}",
                    f"- 当前执行: {execution_summary.get('display_name')}",
                    f"- 阶段: {execution_summary.get('stage')}",
                    f"- 目的: {execution_summary.get('purpose')}",
                    f"- Status: {status}",
                ]
                if execution_summary.get("note"):
                    lines.append(f"- Note: {execution_summary.get('note')}")
                if execution_summary.get("next_action"):
                    lines.append(f"- Next: {execution_summary.get('next_action')}")
            else:
                lines = [
                    "execution 已处理。",
                    f"- Project: /projects/{project_id}",
                    f"- Execution ID: {normalized_execution_id}",
                    f"- Status: {status}",
                ]
            if payload.get("message"):
                lines.append(f"- Message: {payload.get('message')}")
            if payload.get("error"):
                lines.append(f"- Error: {payload.get('error')}")
            return ToolResult(
                success=status in {"running", "pending", "completed", "blocked"},
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload),
                    "background_execution": {
                        "execution_id": normalized_execution_id,
                        "project_id": project_id,
                        "status": status,
                        "display_name": execution_summary.get("display_name"),
                        "stage": execution_summary.get("stage"),
                        "purpose": execution_summary.get("purpose"),
                        "note": execution_summary.get("note"),
                        "next_action": execution_summary.get("next_action"),
                    },
                    "background_execution_user_summary": "\n".join(lines),
                    "background_execution_started": background_started,
                    "background_execution_completed": background_completed,
                },
                error=None if status in {"running", "pending", "completed", "blocked"} else str(payload.get("error") or "execution_failed"),
            )

        return await self._with_db(_handler)


class PaperResearchReadExecutionTool(_PaperResearchToolBase):
    name = "paper_research_read_execution"
    input_model = PaperResearchReadExecutionInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取 execution 状态、结果和日志尾部。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "execution ID。"},
            "include_logs": {"type": "boolean", "default": True},
            "max_log_chars": {"type": "integer", "default": 20000},
        },
        "required": ["project_id", "execution_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            execution_id = str(kwargs.get("execution_id") or "").strip()
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            payload = await ProjectRuntimeService().get_execution(
                workspace_dir=self._workspace_dir_for(workspace),
                project_id=project_id,
                execution_id=execution_id,
                include_logs=bool(kwargs.get("include_logs", True)),
                max_log_chars=int(kwargs.get("max_log_chars") or 20000),
            )
            result = dict(payload.get("result") or {})
            lines = [
                "已读取 execution 状态。",
                f"- Project: /projects/{project_id}",
                f"- Execution ID: {payload.get('execution_id') or execution_id}",
                f"- Status: {payload.get('status')}",
                f"- Result exists: {result.get('result_exists')}",
                f"- Log exists: {result.get('log_exists')}",
            ]
            if result.get("error"):
                lines.append(f"- Error: {result.get('error')}")
            if result.get("message"):
                lines.append(f"- Message: {result.get('message')}")
            if kwargs.get("include_logs", True) and result.get("log"):
                lines.extend(["Log tail:", str(result.get("log") or "")])
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **payload,
                },
            )

        return await self._with_db(_handler)


class PaperResearchCancelExecutionTool(_PaperResearchToolBase):
    name = "paper_research_cancel_execution"
    input_model = PaperResearchCancelExecutionInput
    description = "取消当前进程中仍在运行的 Project execution。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "execution ID。"},
        },
        "required": ["project_id", "execution_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeService

            project_id = int(kwargs["project_id"])
            execution_id = str(kwargs.get("execution_id") or "").strip()
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            payload = await ProjectRuntimeService().cancel_execution(
                project_id=project_id,
                execution_id=execution_id,
                workspace_dir=self._workspace_dir_for(workspace),
            )
            if payload is None:
                return ToolResult(
                    success=False,
                    output=f"没有找到当前进程中运行的 execution: `{execution_id}`。",
                    error="execution_not_running",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "execution_id": execution_id,
                    },
                )
            return ToolResult(
                success=True,
                output=f"已请求取消 execution `{execution_id}`。",
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload),
                },
            )

        return await self._with_db(_handler)


@dataclass
class KnowledgeRetrieveRuntime:
    use_reranker: bool
    use_hybrid: bool
    final_top_k: int
    distance_threshold: float
    reranker_candidate_k: int
    vector_top_k: int
    text_top_k: int
    fusion_limit: int


@dataclass
class KnowledgeRetrieveState:
    rewrite_result: QueryRewriteResult
    runtime: KnowledgeRetrieveRuntime
    resolved_kb_ids: Set[int]
    resolved_document_ids: Set[int]
    fused_candidates: List[Any]
    vector_rows: List[Any]
    text_rows: List[Any]
    retrieval_dimensions: Set[int]
    resolved_ef_search: int
    total_chunks: int
    ef_search_debug: List[Dict[str, int]]


class KnowledgeSearchTool(ToolBase):
    """知识库搜索工具 - 使用 pgvector 进行向量检索"""
    name = "knowledge_search"
    parallel_safe = True
    description = "搜索用户的知识库，检索与查询相关的文档片段。当用户问题涉及他们上传的文档、论文、资料时使用此工具。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询内容，应该是与问题相关的关键词或短语"
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            },
            "include_adjacent_chunks": {
                "type": "boolean",
                "description": "是否返回命中 chunk 的相邻上下文",
                "default": False
            },
            "adjacent_window": {
                "type": "integer",
                "description": "相邻窗口大小（1-3）",
                "default": 1
            },
            "knowledge_base_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "可选：仅在指定知识库内检索"
            },
            "document_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "可选：仅在指定文档内检索"
            },
            "use_reranker": {
                "type": "boolean",
                "description": "可选：覆盖默认 reranker 开关"
            },
            "use_hybrid": {
                "type": "boolean",
                "description": "可选：覆盖默认 hybrid retrieval 开关"
            },
            "use_query_rewrite": {
                "type": "boolean",
                "description": "可选：覆盖默认 query rewrite 开关"
            },
            "query_rewrite_profile": {
                "type": "string",
                "enum": ["off", "light", "deep"],
                "description": "可选：覆盖 query rewrite 层级。light 仅轻量同义扩展，deep 使用完整多策略改写。"
            },
            "use_contextual_compression": {
                "type": "boolean",
                "description": "可选：覆盖默认 contextual compression 开关"
            }
        },
        "required": ["query"]
    }
    input_model = KnowledgeSearchInput
    retry_count = 0
    
    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = user_id
        self.db_session_factory = db_session_factory
        self.query_rewrite_service = get_query_rewrite_service()
    
    def _resolve_timeout_seconds(self) -> float:
        primary_timeout = float(getattr(settings, "knowledge_search_timeout_ms", 45000)) / 1000.0
        return max(primary_timeout, super()._resolve_timeout_seconds())

    async def _execute(
        self,
        query: str,
        top_k: int = 5,
        include_adjacent_chunks: bool = False,
        adjacent_window: int = 1,
        knowledge_base_ids: Optional[List[int]] = None,
        document_ids: Optional[List[int]] = None,
        use_reranker: Optional[bool] = None,
        use_hybrid: Optional[bool] = None,
        use_query_rewrite: Optional[bool] = None,
        query_rewrite_profile: Optional[str] = None,
        use_contextual_compression: Optional[bool] = None,
    ) -> ToolResult:
        """执行知识库搜索（自动选择会话策略）"""
        if self.db is not None:
            return await self._execute_with_db(
                self.db,
                query,
                top_k,
                include_adjacent_chunks=include_adjacent_chunks,
                adjacent_window=adjacent_window,
                knowledge_base_ids=knowledge_base_ids,
                document_ids=document_ids,
                use_reranker=use_reranker,
                use_hybrid=use_hybrid,
                use_query_rewrite=use_query_rewrite,
                query_rewrite_profile=query_rewrite_profile,
                use_contextual_compression=use_contextual_compression,
            )

        if self.db_session_factory is None:
            return ToolResult(
                success=False,
                output="知识库搜索不可用：数据库会话未初始化",
                error="db_session_unavailable",
            )

        try:
            async with self.db_session_factory() as db:
                return await self._execute_with_db(
                    db,
                    query,
                    top_k,
                    include_adjacent_chunks=include_adjacent_chunks,
                    adjacent_window=adjacent_window,
                    knowledge_base_ids=knowledge_base_ids,
                    document_ids=document_ids,
                    use_reranker=use_reranker,
                    use_hybrid=use_hybrid,
                    use_query_rewrite=use_query_rewrite,
                    query_rewrite_profile=query_rewrite_profile,
                    use_contextual_compression=use_contextual_compression,
                )
        except Exception as e:
            logger.error(f"知识库搜索失败（短会话模式）: {e}")
            return ToolResult(
                success=False,
                output=f"搜索过程中发生错误: {str(e)}",
                error=str(e)
            )

    async def _execute_with_db(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
        include_adjacent_chunks: bool = False,
        adjacent_window: int = 1,
        knowledge_base_ids: Optional[List[int]] = None,
        document_ids: Optional[List[int]] = None,
        use_reranker: Optional[bool] = None,
        use_hybrid: Optional[bool] = None,
        use_query_rewrite: Optional[bool] = None,
        query_rewrite_profile: Optional[str] = None,
        use_contextual_compression: Optional[bool] = None,
    ) -> ToolResult:
        """执行知识库搜索 - 使用 pgvector 原生向量搜索，支持共享知识库"""
        try:
            start_time = time.time()
            runtime = self._resolve_retrieve_runtime(
                top_k,
                use_reranker=use_reranker,
                use_hybrid=use_hybrid,
            )

            # 1) Rewrite
            rewrite_kwargs: dict[str, Any] = {
                "use_query_rewrite": use_query_rewrite,
            }
            if query_rewrite_profile is not None:
                rewrite_kwargs["query_rewrite_profile"] = query_rewrite_profile

            rewrite_result = await self._rewrite(
                query,
                **rewrite_kwargs,
            )

            # 2) Retrieve
            retrieve_payload = await self._retrieve(
                db,
                query,
                rewrite_result,
                runtime,
                requested_kb_ids=knowledge_base_ids,
                requested_document_ids=document_ids,
            )
            if isinstance(retrieve_payload, ToolResult):
                return retrieve_payload

            # 3) Rerank
            selected_candidates = await self._rerank(query, retrieve_payload)

            # 4) Compress
            results = await self._compress(
                db=db,
                query=query,
                state=retrieve_payload,
                selected_candidates=selected_candidates,
                include_adjacent_chunks=include_adjacent_chunks,
                adjacent_window=adjacent_window,
                use_contextual_compression=use_contextual_compression,
            )

            search_time = (time.time() - start_time) * 1000
            output = self._format_retrieval_output(query=query, results=results, search_time=search_time)
            self._log_retrieval_metrics(
                query=query,
                search_time=search_time,
                state=retrieve_payload,
                result_count=len(results),
            )

            payload = _build_knowledge_search_payload(
                query=query,
                results=results,
                search_time_ms=search_time,
            )
            payload["retrieval_runtime"] = {
                "use_reranker": runtime.use_reranker,
                "use_hybrid": runtime.use_hybrid,
                "final_top_k": runtime.final_top_k,
            }
            if getattr(rewrite_result, "enabled", False):
                rewrite_strategies = list(getattr(rewrite_result, "strategies", []) or [])
                payload["retrieval_runtime"]["query_rewrite_profile"] = (
                    "light" if rewrite_strategies == ["synonym"] else "deep"
                )
            payload["retrieval_scope"] = {
                "knowledge_base_ids": sorted(int(item) for item in retrieve_payload.resolved_kb_ids),
                "document_ids": sorted(int(item) for item in retrieve_payload.resolved_document_ids),
            }

            return ToolResult(
                success=True,
                output=output,
                data=payload,
            )

        except Exception as e:
            logger.error(f"知识库搜索失败: {e}")
            return ToolResult(
                success=False,
                output=f"搜索过程中发生错误: {str(e)}",
                error=str(e),
            )

    def _resolve_retrieve_runtime(
        self,
        top_k: int,
        *,
        use_reranker: Optional[bool] = None,
        use_hybrid: Optional[bool] = None,
    ) -> KnowledgeRetrieveRuntime:
        use_reranker = bool(settings.enable_reranker) if use_reranker is None else bool(use_reranker)
        use_hybrid = bool(settings.enable_hybrid_retrieval) if use_hybrid is None else bool(use_hybrid)
        final_top_k = max(int(top_k), 1)
        score_threshold = max(
            0.0,
            min(float(settings.agent_knowledge_score_threshold), 1.0),
        )
        distance_threshold = 1 - score_threshold
        reranker_candidate_k = (
            max(final_top_k, int(settings.reranker_top_k))
            if use_reranker
            else final_top_k
        )
        vector_top_k = max(
            reranker_candidate_k,
            int(settings.hybrid_vector_top_k) if use_hybrid else 0,
        )
        text_top_k = (
            max(reranker_candidate_k, int(settings.hybrid_text_top_k))
            if use_hybrid
            else 0
        )
        return KnowledgeRetrieveRuntime(
            use_reranker=use_reranker,
            use_hybrid=use_hybrid,
            final_top_k=final_top_k,
            distance_threshold=distance_threshold,
            reranker_candidate_k=reranker_candidate_k,
            vector_top_k=vector_top_k,
            text_top_k=text_top_k,
            fusion_limit=reranker_candidate_k,
        )

    async def _rewrite(
        self,
        query: str,
        *,
        use_query_rewrite: Optional[bool] = None,
        query_rewrite_profile: Optional[str] = None,
    ) -> QueryRewriteResult:
        kwargs: Dict[str, Any] = {
            "rewrite_mode": "auto",
            "use_query_rewrite": True if use_query_rewrite is None else bool(use_query_rewrite),
        }
        if query_rewrite_profile:
            kwargs["rewrite_profile"] = query_rewrite_profile
        return await self.query_rewrite_service.rewrite_query(query, **kwargs)

    async def _resolve_scope(
        self,
        db: AsyncSession,
        *,
        requested_kb_ids: Optional[Sequence[int]] = None,
        requested_document_ids: Optional[Sequence[int]] = None,
    ) -> tuple[Set[int], Set[int]]:
        kb_query = select(KnowledgeBase.id).where(KnowledgeBase.user_id == self.user_id)
        kb_result = await db.execute(kb_query)
        accessible_kb_ids = set(row[0] for row in kb_result.fetchall())
        shared_kb_ids = await self._get_shared_kb_ids(db)
        accessible_kb_ids |= shared_kb_ids

        normalized_requested_kb_ids = {
            int(item)
            for item in list(requested_kb_ids or [])
            if isinstance(item, int) and int(item) > 0
        }
        resolved_kb_ids = (
            accessible_kb_ids & normalized_requested_kb_ids
            if normalized_requested_kb_ids
            else set(accessible_kb_ids)
        )

        normalized_requested_document_ids = {
            int(item)
            for item in list(requested_document_ids or [])
            if isinstance(item, int) and int(item) > 0
        }
        if not normalized_requested_document_ids:
            return resolved_kb_ids, set()

        docs_query = select(Document.id, Document.knowledge_base_id).where(
            Document.id.in_(normalized_requested_document_ids)
        )
        docs_result = await db.execute(docs_query)
        resolved_document_ids: Set[int] = set()
        document_kb_ids: Set[int] = set()
        for doc_id, kb_id in docs_result.fetchall():
            if int(kb_id) not in accessible_kb_ids:
                continue
            if normalized_requested_kb_ids and int(kb_id) not in resolved_kb_ids:
                continue
            resolved_document_ids.add(int(doc_id))
            document_kb_ids.add(int(kb_id))

        if not resolved_document_ids:
            return set(), set()

        resolved_kb_ids = document_kb_ids

        return resolved_kb_ids, resolved_document_ids

    async def _retrieve(
        self,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        runtime: KnowledgeRetrieveRuntime,
        *,
        requested_kb_ids: Optional[Sequence[int]] = None,
        requested_document_ids: Optional[Sequence[int]] = None,
    ) -> ToolResult | KnowledgeRetrieveState:
        kb_ids, document_ids = await self._resolve_scope(
            db,
            requested_kb_ids=requested_kb_ids,
            requested_document_ids=requested_document_ids,
        )
        if not kb_ids:
            return ToolResult(
                success=True,
                output=(
                    "当前临时 RAG 作用域下没有可检索的知识库或文档。"
                    if requested_kb_ids or requested_document_ids
                    else "用户没有创建任何知识库，也没有收到共享的知识库，无法搜索相关内容。建议用户先上传文档到知识库，或请导师共享知识库。"
                ),
                data={
                    "results": [],
                    "total": 0,
                    "retrieval_scope": {
                        "knowledge_base_ids": sorted(int(item) for item in kb_ids),
                        "document_ids": sorted(int(item) for item in document_ids),
                    },
                },
            )

        kb_id_list = list(kb_ids)
        vector_rows, retrieval_dimensions, total_chunks, resolved_ef_search, ef_search_debug = await self._retrieve_vector_rows(
            db=db,
            query=query,
            rewrite_result=rewrite_result,
            kb_ids=kb_id_list,
            document_ids=list(document_ids),
            runtime=runtime,
        )
        text_rows = await self._retrieve_text_rows(
            db=db,
            query=query,
            rewrite_result=rewrite_result,
            kb_ids=kb_id_list,
            document_ids=list(document_ids),
            runtime=runtime,
        )

        fused_candidates = fuse_rrf(
            vector_rows=vector_rows,
            text_rows=text_rows if runtime.use_hybrid else [],
            rrf_k=settings.hybrid_rrf_k,
            limit=runtime.fusion_limit,
        )
        if not fused_candidates:
            return ToolResult(
                success=True,
                output="未找到与查询相关的内容。可能知识库中没有相关信息，或者需要调整搜索关键词。",
                data={"results": [], "total": 0},
            )

        return KnowledgeRetrieveState(
            rewrite_result=rewrite_result,
            runtime=runtime,
            resolved_kb_ids=kb_ids,
            resolved_document_ids=document_ids,
            fused_candidates=fused_candidates,
            vector_rows=vector_rows,
            text_rows=text_rows,
            retrieval_dimensions=retrieval_dimensions,
            resolved_ef_search=resolved_ef_search,
            total_chunks=total_chunks,
            ef_search_debug=ef_search_debug,
        )

    async def _retrieve_vector_rows(
        self,
        *,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        kb_ids: List[int],
        document_ids: List[int],
        runtime: KnowledgeRetrieveRuntime,
    ) -> tuple[list[Any], set[int], int, int, list[dict[str, int]]]:
        vector_groups_sql = text(
            """
            SELECT
                COALESCE(NULLIF(embedding_model, ''), :default_embedding_model) AS embedding_model,
                embedding_dimension,
                COUNT(*) AS chunk_count
            FROM document_chunks
            WHERE knowledge_base_id = ANY(:kb_ids)
                AND (:filter_by_document_ids = FALSE OR document_id = ANY(:document_ids))
                AND embedding IS NOT NULL
                AND embedding_dimension IS NOT NULL
            GROUP BY COALESCE(NULLIF(embedding_model, ''), :default_embedding_model), embedding_dimension
            ORDER BY chunk_count DESC
            """
        )
        vector_groups = (
            await db.execute(
                vector_groups_sql,
                {
                    "kb_ids": kb_ids,
                    "document_ids": document_ids,
                    "filter_by_document_ids": bool(document_ids),
                    "default_embedding_model": settings.local_embedding_model,
                },
            )
        ).fetchall()

        vector_group_rows: list[tuple[str, str, list[Any]]] = []
        vector_variants = getattr(rewrite_result, "vector_variants", None) or [
            QueryVariant(text=query, strategy="original")
        ]
        total_chunks = 0
        resolved_ef_search = int(settings.pgvector_hnsw_ef_search)
        retrieval_dimensions: set[int] = set()
        ef_search_debug: list[dict[str, int]] = []

        for group in vector_groups:
            group_model = str(
                getattr(group, "embedding_model", "") or settings.local_embedding_model
            ).strip()
            group_dimension = int(getattr(group, "embedding_dimension", 0) or 0)
            group_chunks = int(getattr(group, "chunk_count", 0) or 0)
            if group_dimension <= 0:
                continue

            retrieval_dimensions.add(group_dimension)
            total_chunks += group_chunks

            group_embedding_service = get_embedding_service_for_model_and_dimension(
                group_model,
                group_dimension,
            )
            vector_texts = [variant.text for variant in vector_variants]
            vector_embeddings: list[list[float]] = []
            try:
                vector_embeddings = await group_embedding_service.embed_texts(
                    vector_texts,
                    is_query=True,
                )
                if len(vector_embeddings) != len(vector_texts):
                    raise ValueError(
                        f"embedding count mismatch: {len(vector_embeddings)} vs {len(vector_texts)}"
                    )
            except Exception as e:
                logger.warning(
                    f"[KnowledgeSearch] Batch embedding failed for model={group_model}, "
                    f"dim={group_dimension}: {e}"
                )
                vector_embeddings = []
                for variant in vector_variants:
                    try:
                        emb = await group_embedding_service.embed_text(
                            variant.text,
                            is_query=True,
                        )
                    except Exception as single_exc:
                        logger.warning(
                            f"[KnowledgeSearch] Single embedding failed for strategy={variant.strategy}, "
                            f"model={group_model}, dim={group_dimension}: {single_exc}"
                        )
                        emb = []
                    vector_embeddings.append(emb)

            distance_expr = (
                f"(dc.embedding::vector({group_dimension}) <=> "
                f"(:query_vector)::vector({group_dimension}))"
            )
            vector_sql = text(
                f"""
                SELECT
                    dc.id,
                    dc.document_id,
                    dc.knowledge_base_id,
                    dc.content,
                    dc.chunk_index,
                    dc.section_type,
                    dc.section_title,
                    dc.context_summary,
                    dc.embedding_model,
                    dc.embedding_dimension,
                    1 - {distance_expr} AS similarity,
                    NULL::float AS text_score,
                    d.original_filename AS document_name,
                    kb.name AS knowledge_base_name
                FROM document_chunks dc
                JOIN documents d ON dc.document_id = d.id
                JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
                WHERE dc.knowledge_base_id = ANY(:kb_ids)
                    AND (:filter_by_document_ids = FALSE OR dc.document_id = ANY(:document_ids))
                    AND dc.embedding IS NOT NULL
                    AND dc.embedding_dimension = :vector_dimension
                    AND {distance_expr} <= :distance_threshold
                ORDER BY {distance_expr}
                LIMIT :vector_top_k
                """
            )

            resolved_group_ef = resolve_ef_search(
                total_chunks=group_chunks,
                dimension=group_dimension,
            )
            await apply_hnsw_ef_search(
                db,
                resolved_group_ef,
                source=f"knowledge_search_tool.dim{group_dimension}",
            )
            resolved_ef_search = max(resolved_ef_search, resolved_group_ef)
            ef_search_debug.append(
                {
                    "dimension": group_dimension,
                    "chunks": group_chunks,
                    "ef_search": resolved_group_ef,
                }
            )

            for idx, variant in enumerate(vector_variants):
                query_embedding = vector_embeddings[idx] if idx < len(vector_embeddings) else []
                if not query_embedding or len(query_embedding) != group_dimension:
                    continue

                vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
                result = await db.execute(
                    vector_sql,
                    {
                        "query_vector": vector_str,
                        "distance_threshold": runtime.distance_threshold,
                        "kb_ids": kb_ids,
                        "document_ids": document_ids,
                        "filter_by_document_ids": bool(document_ids),
                        "vector_dimension": group_dimension,
                        "vector_top_k": runtime.vector_top_k,
                    },
                )
                rows = result.fetchall()
                if rows:
                    vector_group_rows.append((variant.strategy, variant.text, rows))

        vector_rows = merge_rows_by_score(
            vector_group_rows,
            score_attr="similarity",
            query_attr="matched_vector_query",
            strategy_attr="matched_vector_strategy",
            limit=runtime.vector_top_k,
        )
        return vector_rows, retrieval_dimensions, total_chunks, resolved_ef_search, ef_search_debug

    async def _retrieve_text_rows(
        self,
        *,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        kb_ids: List[int],
        document_ids: List[int],
        runtime: KnowledgeRetrieveRuntime,
    ) -> list[Any]:
        if not runtime.use_hybrid:
            return []

        text_variants = getattr(rewrite_result, "text_variants", None) or [
            QueryVariant(text=query, strategy="original")
        ]
        text_sql = text(
            """
            SELECT
                dc.id,
                dc.document_id,
                dc.knowledge_base_id,
                dc.content,
                dc.chunk_index,
                dc.section_type,
                dc.section_title,
                dc.context_summary,
                NULL::float as similarity,
                ts_rank_cd(
                    to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)),
                    websearch_to_tsquery('simple', :fts_query)
                ) as text_score,
                d.original_filename as document_name,
                kb.name as knowledge_base_name
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
            WHERE dc.knowledge_base_id = ANY(:kb_ids)
                AND (:filter_by_document_ids = FALSE OR dc.document_id = ANY(:document_ids))
                AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) IS NOT NULL
                AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) <> ''
                AND to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)) @@ websearch_to_tsquery('simple', :fts_query)
            ORDER BY text_score DESC
            LIMIT :text_top_k
            """
        )
        text_group_rows: list[tuple[str, str, list[Any]]] = []
        for variant in text_variants:
            if not variant.text.strip():
                continue
            fts_query = segment_text_for_fts(variant.text)
            if not fts_query.strip():
                continue
            try:
                text_result = await db.execute(
                    text_sql,
                    {
                        "fts_query": fts_query,
                        "kb_ids": kb_ids,
                        "document_ids": document_ids,
                        "filter_by_document_ids": bool(document_ids),
                        "text_top_k": runtime.text_top_k,
                    },
                )
                rows = text_result.fetchall()
                if rows:
                    text_group_rows.append((variant.strategy, variant.text, rows))
            except Exception as e:
                logger.warning(
                    f"[KnowledgeSearch] Full-text query failed for "
                    f"strategy={variant.strategy}: {e}"
                )

        return merge_rows_by_score(
            text_group_rows,
            score_attr="text_score",
            query_attr="matched_text_query",
            strategy_attr="matched_text_strategy",
            limit=runtime.text_top_k,
        )

    async def _rerank(
        self,
        query: str,
        state: KnowledgeRetrieveState,
    ) -> List[tuple[Any, Optional[float]]]:
        selected_candidates: list[tuple[Any, Optional[float]]] = []
        if state.runtime.use_reranker:
            try:
                reranker = get_reranker_service()
                reranked = await reranker.rerank(
                    query=query,
                    documents=[
                        build_reranker_input(
                            content=getattr(candidate.row, "content", "") or "",
                            context_summary=getattr(candidate.row, "context_summary", None),
                            document_name=getattr(candidate.row, "document_name", None),
                            section_title=getattr(candidate.row, "section_title", None),
                            section_type=getattr(candidate.row, "section_type", None),
                            max_context_length=int(settings.reranker_context_max_chars or 220),
                            max_content_length=int(settings.reranker_snippet_max_chars or 960),
                        )
                        for candidate in state.fused_candidates
                    ],
                    top_k=state.runtime.final_top_k,
                )
                selected_candidates = [
                    (state.fused_candidates[idx], score)
                    for idx, score in reranked
                    if 0 <= idx < len(state.fused_candidates)
                ]
            except Exception as e:
                logger.warning(f"[KnowledgeSearch] Reranker failed, fallback to retrieval ranking: {e}")

        if not selected_candidates:
            selected_candidates = [
                (candidate, None)
                for candidate in state.fused_candidates[: state.runtime.final_top_k]
            ]
        return selected_candidates

    async def _compress(
        self,
        *,
        db: AsyncSession,
        query: str,
        state: KnowledgeRetrieveState,
        selected_candidates: List[tuple[Any, Optional[float]]],
        include_adjacent_chunks: bool,
        adjacent_window: int,
        use_contextual_compression: Optional[bool],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        adjacent_targets: list[tuple[int, int, int]] = []
        max_rrf_score = max((c.rrf_score for c in state.fused_candidates), default=0.0)
        use_hybrid = state.runtime.use_hybrid
        rewrite_result = state.rewrite_result
        retrieval_dimensions = state.retrieval_dimensions
        contextual_compression = get_contextual_compression_service()
        compression_inputs: list[CompressionInput] = []

        for source_id, (candidate, reranker_score) in enumerate(selected_candidates, start=1):
            row = candidate.row
            compression_inputs.append(
                CompressionInput(
                    source_id=source_id,
                    doc_name=(row.document_name or "未知文档"),
                    chunk_idx=int(getattr(row, "chunk_index", 0) or 0),
                    chunk_content=getattr(row, "content", "") or "",
                    reranker_score=float(reranker_score) if reranker_score is not None else None,
                )
            )

        compression_results = await contextual_compression.compress_chunks(
            query,
            compression_inputs,
            use_contextual_compression=(
                True if use_contextual_compression is None else bool(use_contextual_compression)
            ),
        )
        compression_by_source_id = {
            item.source_id: item
            for item in compression_results
        }

        for source_id, (candidate, reranker_score) in enumerate(selected_candidates, start=1):
            row = candidate.row
            vector_score = (
                round(float(candidate.vector_score), 4)
                if candidate.vector_score is not None
                else None
            )
            text_score = (
                round(float(candidate.text_score), 4)
                if candidate.text_score is not None
                else None
            )

            if reranker_score is not None:
                score = round(RerankerService.normalize_score(float(reranker_score)), 4)
            elif use_hybrid and max_rrf_score > 0:
                score = round(candidate.rrf_score / max_rrf_score, 4)
            elif vector_score is not None:
                score = vector_score
            else:
                score = 0.0

            compression_result = compression_by_source_id.get(source_id)
            compressed_content = (
                compression_result.relevant_content
                if compression_result
                else ""
            )
            compression_score = (
                round(float(compression_result.relevance_score), 2)
                if compression_result
                else 0.0
            )
            compression_fallback = (
                compression_result.fallback_reason
                if compression_result
                else "not_attempted"
            )
            source_label = f"来源{source_id}"

            retrieval_dimension = int(getattr(row, "embedding_dimension", 0) or 0)
            if retrieval_dimension <= 0 and retrieval_dimensions:
                retrieval_dimension = int(sorted(retrieval_dimensions)[0])

            result_item = {
                "content": compressed_content or row.content,
                "score": score,
                "document": row.document_name or "未知",
                "knowledge_base": row.knowledge_base_name or "未知",
                "document_id": row.document_id,
                "chunk_id": row.id,
                "chunk_index": row.chunk_index,
                "retrieval_mode": "hybrid" if use_hybrid else "vector",
                "query_rewrite_enabled": getattr(rewrite_result, "enabled", False),
                "query_rewrite_strategies": list(getattr(rewrite_result, "strategies", []) or []),
                "query_rewrite_fallback": getattr(rewrite_result, "fallback_reason", None),
                "query_rewrite_cache_hit": bool(getattr(rewrite_result, "cache_hit", False)),
                "query_rewrite_skip_reason": getattr(rewrite_result, "skip_reason", None),
                "query_rewrite_llm_called": bool(getattr(rewrite_result, "llm_called", False)),
                "matched_vector_query": getattr(row, "matched_vector_query", None),
                "matched_vector_strategy": getattr(row, "matched_vector_strategy", None),
                "matched_text_query": getattr(row, "matched_text_query", None),
                "matched_text_strategy": getattr(row, "matched_text_strategy", None),
                "vector_rank": candidate.vector_rank,
                "text_rank": candidate.text_rank,
                "rrf_score": round(float(candidate.rrf_score), 6),
                "vector_score": vector_score,
                "text_score": text_score,
                "reranker_score": round(float(reranker_score), 4) if reranker_score is not None else None,
                "retrieval_dimension": retrieval_dimension,
                "retrieval_embedding_model": getattr(row, "embedding_model", None),
                "contextual_compression_enabled": bool(
                    compression_result and compression_result.used_compression
                ),
                "contextual_compression_source": source_label,
                "contextual_compression_score": compression_score,
                "contextual_compression_fallback": compression_fallback,
            }
            if compressed_content:
                result_item["contextual_compression_excerpt"] = compressed_content
            results.append(result_item)

            chunk_index = int(getattr(row, "chunk_index", -1) or -1)
            if include_adjacent_chunks and chunk_index >= 0:
                adjacent_targets.append((len(results) - 1, int(row.document_id), chunk_index))

        if include_adjacent_chunks and adjacent_targets:
            window = normalize_adjacent_window(adjacent_window)
            neighbor_keys: set[tuple[int, int]] = set()
            for _, doc_id, chunk_index in adjacent_targets:
                neighbor_keys.update(build_adjacent_lookup_keys(doc_id, chunk_index, window))

            adjacent_map: dict[tuple[int, int], Any] = {}
            if neighbor_keys:
                adjacent_result = await db.execute(
                    select(
                        DocumentChunk.id,
                        DocumentChunk.document_id,
                        DocumentChunk.chunk_index,
                        DocumentChunk.chunk_level,
                        DocumentChunk.section_title,
                        DocumentChunk.content,
                    ).where(tuple_(DocumentChunk.document_id, DocumentChunk.chunk_index).in_(list(neighbor_keys)))
                )
                adjacent_map = {
                    (int(row.document_id), int(row.chunk_index)): row
                    for row in adjacent_result.fetchall()
                }

            for idx, doc_id, chunk_index in adjacent_targets:
                results[idx]["adjacent_context"] = merge_adjacent_context(
                    document_id=doc_id,
                    chunk_index=chunk_index,
                    window=window,
                    row_map=adjacent_map,
                )
        return results

    @staticmethod
    def _format_retrieval_output(*, query: str, results: list[dict[str, Any]], search_time: float) -> str:
        output_parts = [f"找到 {len(results)} 条与“{query}”相关的知识库线索：\n"]
        for i, r in enumerate(results, 1):
            reader_excerpt = str(r.get("reader_excerpt") or r.get("content") or "").strip()
            retrieval_mode = str(r.get("retrieval_mode") or "").strip()
            source_label = str(r.get("source_label") or f"{r.get('knowledge_base', '未知')} / {r.get('document', '未知')}").strip()
            output_parts.append(
                f"\n【线索{i}】(相关度: {r['score']*100:.1f}%)\n"
                f"来源: {source_label}\n"
                f"检索方式: {retrieval_mode or 'vector'}\n"
                f"要点: {reader_excerpt[:500]}{'...' if len(reader_excerpt) > 500 else ''}"
            )
        output_parts.append(f"\n\n(搜索耗时: {search_time:.2f}ms)")
        return "".join(output_parts)

    def _log_retrieval_metrics(
        self,
        *,
        query: str,
        search_time: float,
        state: KnowledgeRetrieveState,
        result_count: int,
    ) -> None:
        rewrite_result = state.rewrite_result
        logger.info(
            f"[KnowledgeSearch] query='{query[:50]}...', results={result_count}, "
            f"hybrid={state.runtime.use_hybrid}, reranker={state.runtime.use_reranker}, "
            f"query_rewrite={getattr(rewrite_result, 'enabled', False)}, "
            f"rewrite_variants={len(getattr(rewrite_result, 'vector_variants', []) or [])}, "
            f"vector_hits={len(state.vector_rows)}, text_hits={len(state.text_rows)}, "
            f"ef_search={state.resolved_ef_search}, corpus_size={state.total_chunks}, "
            f"dims={sorted(state.retrieval_dimensions)}, ef_detail={state.ef_search_debug}, "
            f"time={search_time:.2f}ms"
        )

    async def _get_shared_kb_ids(self, db: AsyncSession) -> Set[int]:
        """获取共享给当前用户的知识库ID"""
        if not SHARING_ENABLED:
            logger.debug("共享功能未启用 (agent_tools)")
            return set()
        
        try:
            logger.debug(f"获取用户 {self.user_id} 的共享知识库 (agent_tools)")
            
            # 获取当前用户信息
            user_result = await db.execute(
                select(User).where(User.id == self.user_id)
            )
            current_user = user_result.scalar_one_or_none()
            if not current_user:
                logger.warning(f"用户 {self.user_id} 不存在")
                return set()
            
            logger.debug(f"当前用户: {current_user.username}, 角色: {current_user.role}, 导师ID: {current_user.mentor_id}")
            
            # 获取用户加入的研究组
            group_ids_result = await db.execute(
                select(GroupMember.group_id).where(GroupMember.user_id == self.user_id)
            )
            group_ids = [row[0] for row in group_ids_result.fetchall()]
            logger.debug(f"用户加入的研究组: {group_ids}")
            
            # 如果是导师，获取管理的研究组
            if current_user.role == UserRole.MENTOR.value:
                mentor_groups_result = await db.execute(
                    select(ResearchGroup.id).where(ResearchGroup.mentor_id == self.user_id)
                )
                mentor_group_ids = [row[0] for row in mentor_groups_result.fetchall()]
                group_ids = list(set(group_ids + mentor_group_ids))
            
            # 构建共享条件
            conditions = [
                and_(
                    SharedResource.shared_with_type == 'user',
                    SharedResource.shared_with_id == self.user_id
                ),
            ]
            
            if group_ids:
                conditions.append(
                    and_(
                        SharedResource.shared_with_type == 'group',
                        SharedResource.shared_with_id.in_(group_ids)
                    )
                )
            
            if current_user.mentor_id:
                conditions.append(
                    and_(
                        SharedResource.shared_with_type == 'all_students',
                        SharedResource.owner_id == current_user.mentor_id
                    )
                )
            
            if current_user.role == UserRole.STUDENT.value and group_ids:
                mentor_ids_result = await db.execute(
                    select(ResearchGroup.mentor_id).where(ResearchGroup.id.in_(group_ids))
                )
                mentor_ids = [row[0] for row in mentor_ids_result.fetchall()]
                if mentor_ids:
                    conditions.append(
                        and_(
                            SharedResource.shared_with_type == 'all_students',
                            SharedResource.owner_id.in_(mentor_ids)
                        )
                    )
            
            # 查询共享的知识库ID
            shared_result = await db.execute(
                select(SharedResource.resource_id).where(
                    and_(
                        SharedResource.resource_type == 'knowledge_base',
                        or_(*conditions),
                        or_(
                            SharedResource.expires_at == None,
                            SharedResource.expires_at > datetime.utcnow()
                        )
                    )
                )
            )
            
            # resource_id 是字符串，需要转为整数（知识库ID是整数）
            result = set()
            for row in shared_result.fetchall():
                try:
                    result.add(int(row[0]))
                except (ValueError, TypeError):
                    logger.warning(f"无效的知识库ID: {row[0]}")
            logger.info(f"用户 {self.user_id} 可访问的共享知识库 (agent_tools): {result}")
            return result
        except Exception as e:
            logger.warning(f"获取共享知识库失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return set()


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=512)


class CalculatorTool(ToolBase):
    """计算器工具 - 执行数学计算"""
    name = "calculator"
    parallel_safe = True
    description = "执行数学计算，支持基本运算、三角函数、对数、幂运算等。当需要进行数值计算时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，如 '2+3*4', 'sqrt(16)', 'sin(3.14/2)', 'log(100, 10)'"
            }
        },
        "required": ["expression"]
    }
    input_model = CalculatorInput
    
    def __init__(self):
        # 安全的数学函数映射
        self.safe_functions = {
            'abs': abs,
            'round': round,
            'min': min,
            'max': max,
            'sum': sum,
            'pow': pow,
            'sqrt': math.sqrt,
            'sin': math.sin,
            'cos': math.cos,
            'tan': math.tan,
            'asin': math.asin,
            'acos': math.acos,
            'atan': math.atan,
            'sinh': math.sinh,
            'cosh': math.cosh,
            'tanh': math.tanh,
            'log': math.log,
            'log10': math.log10,
            'log2': math.log2,
            'exp': math.exp,
            'floor': math.floor,
            'ceil': math.ceil,
            'factorial': math.factorial,
            'gcd': math.gcd,
            'pi': math.pi,
            'e': math.e,
            'radians': math.radians,
            'degrees': math.degrees,
        }
    
    async def _execute(self, expression: str) -> ToolResult:
        """执行数学计算（asteval 安全求值）。"""
        expr = expression.strip()
        if not expr:
            return ToolResult(success=False, output="表达式不能为空", error="empty_expression")

        # 拒绝高风险语法
        forbidden_tokens = ["__", "import", "lambda", ";", "{", "}", "[", "]"]
        if any(token in expr for token in forbidden_tokens):
            return ToolResult(success=False, output="表达式包含不安全语法", error="unsafe_expression")

        # 限制标识符仅来自白名单
        allowed_names = set(self.safe_functions.keys())
        identifiers = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expr)
        for name in identifiers:
            if name not in allowed_names:
                return ToolResult(
                    success=False,
                    output=f"不支持的函数或变量: {name}",
                    error="invalid_identifier",
                )

        try:
            from asteval import Interpreter
        except ImportError:
            return ToolResult(
                success=False,
                output="asteval 未安装，无法执行安全求值。",
                error="asteval_not_installed",
            )

        try:
            evaluator = Interpreter(usersyms=dict(self.safe_functions), minimal=True)
            result = evaluator(expr)
            if evaluator.error:
                detail = "; ".join(err.get_error()[1] for err in evaluator.error)
                return ToolResult(
                    success=False,
                    output=f"计算错误: {detail}",
                    error="eval_error",
                )

            if isinstance(result, float):
                result_str = str(int(result)) if result.is_integer() else f"{result:.10g}"
            else:
                result_str = str(result)

            return ToolResult(
                success=True,
                output=f"计算结果: {expression} = {result_str}",
                data={"expression": expression, "result": result},
            )
        except ZeroDivisionError:
            return ToolResult(success=False, output="错误: 除数不能为零", error="division_by_zero")
        except Exception as exc:
            return ToolResult(success=False, output=f"计算错误: {exc}", error="calculation_error")


class DateTimeTool(Tool):
    """日期时间工具"""
    name = "datetime"
    parallel_safe = True
    description = "获取当前日期时间，或进行日期计算。当用户询问时间、日期相关问题时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型: 'now'(当前时间), 'date'(当前日期), 'weekday'(星期几), 'timestamp'(时间戳)",
                "enum": ["now", "date", "weekday", "timestamp", "format"]
            },
            "format": {
                "type": "string",
                "description": "日期格式，如 '%Y-%m-%d %H:%M:%S'，仅在 action='format' 时使用",
                "default": "%Y-%m-%d %H:%M:%S"
            }
        },
        "required": ["action"]
    }
    
    async def execute(self, action: str, format: str = "%Y-%m-%d %H:%M:%S") -> ToolResult:
        """获取日期时间信息"""
        try:
            now = datetime.now()
            
            if action == "now":
                result = now.strftime("%Y-%m-%d %H:%M:%S")
                output = f"当前时间: {result}"
            elif action == "date":
                result = now.strftime("%Y年%m月%d日")
                output = f"当前日期: {result}"
            elif action == "weekday":
                weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                result = weekdays[now.weekday()]
                output = f"今天是: {result}"
            elif action == "timestamp":
                result = int(now.timestamp())
                output = f"当前时间戳: {result}"
            elif action == "format":
                result = now.strftime(format)
                output = f"格式化时间: {result}"
            else:
                return ToolResult(
                    success=False,
                    output=f"不支持的操作: {action}",
                    error="invalid_action"
                )
            
            return ToolResult(
                success=True,
                output=output,
                data={"action": action, "result": result, "timestamp": int(now.timestamp())}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"日期时间操作错误: {str(e)}",
                error=str(e)
            )


class TextAnalysisTool(Tool):
    """文本分析工具"""
    name = "text_analysis"
    parallel_safe = True
    description = "分析文本的基本统计信息，如字数、词数、句子数等。用于文本分析需求。"
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要分析的文本内容"
            },
            "analysis_type": {
                "type": "string",
                "description": "分析类型: 'stats'(统计), 'keywords'(关键词提取)",
                "enum": ["stats", "keywords"],
                "default": "stats"
            }
        },
        "required": ["text"]
    }
    
    async def execute(self, text: str, analysis_type: str = "stats") -> ToolResult:
        """分析文本"""
        try:
            if analysis_type == "stats":
                # 基本统计
                char_count = len(text)
                char_no_space = len(text.replace(" ", "").replace("\n", ""))
                
                # 中文字数
                chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
                
                # 英文单词数
                english_words = len(re.findall(r'[a-zA-Z]+', text))
                
                # 句子数（简单估计）
                sentences = len(re.findall(r'[。！？.!?]+', text)) or 1
                
                # 段落数
                paragraphs = len([p for p in text.split('\n') if p.strip()])
                
                output = f"""文本统计分析:
- 总字符数: {char_count}
- 字符数(不含空格): {char_no_space}
- 中文字数: {chinese_chars}
- 英文单词数: {english_words}
- 句子数: {sentences}
- 段落数: {paragraphs}
- 平均句长: {char_no_space / sentences:.1f} 字符"""
                
                return ToolResult(
                    success=True,
                    output=output,
                    data={
                        "char_count": char_count,
                        "char_no_space": char_no_space,
                        "chinese_chars": chinese_chars,
                        "english_words": english_words,
                        "sentences": sentences,
                        "paragraphs": paragraphs
                    }
                )
            
            elif analysis_type == "keywords":
                # 简单的关键词提取（基于词频）
                # 中文分词简单处理
                words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text.lower())
                
                # 过滤停用词（简单列表）
                stopwords = {'的', '是', '在', '和', '了', '有', '不', '这', '为', '上', 
                            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                            'to', 'of', 'and', 'in', 'that', 'it', 'for', 'on', 'with'}
                words = [w for w in words if w not in stopwords and len(w) > 1]
                
                # 统计词频
                word_freq = {}
                for w in words:
                    word_freq[w] = word_freq.get(w, 0) + 1
                
                # 取前10个高频词
                top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:10]
                
                output = "关键词提取（按频率排序）:\n"
                for word, freq in top_words:
                    output += f"- {word}: {freq}次\n"
                
                return ToolResult(
                    success=True,
                    output=output,
                    data={"keywords": dict(top_words)}
                )
            
            else:
                return ToolResult(
                    success=False,
                    output=f"不支持的分析类型: {analysis_type}",
                    error="invalid_analysis_type"
                )
                
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"文本分析错误: {str(e)}",
                error=str(e)
            )


class UnitConverterTool(Tool):
    """单位转换工具"""
    name = "unit_converter"
    parallel_safe = True
    description = "进行常见单位转换，如长度、重量、温度、数据存储等。"
    parameters = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "要转换的数值"
            },
            "from_unit": {
                "type": "string",
                "description": "源单位，如 'km', 'mile', 'kg', 'lb', 'celsius', 'fahrenheit', 'GB', 'MB'"
            },
            "to_unit": {
                "type": "string",
                "description": "目标单位"
            }
        },
        "required": ["value", "from_unit", "to_unit"]
    }
    
    def __init__(self):
        # 单位转换因子（都转换为基本单位）
        self.conversions = {
            # 长度 (基本单位: 米)
            'm': 1, 'km': 1000, 'cm': 0.01, 'mm': 0.001,
            'mile': 1609.344, 'yard': 0.9144, 'foot': 0.3048, 'inch': 0.0254,
            '米': 1, '千米': 1000, '厘米': 0.01, '毫米': 0.001,
            
            # 重量 (基本单位: 克)
            'g': 1, 'kg': 1000, 'mg': 0.001, 'ton': 1000000,
            'lb': 453.592, 'oz': 28.3495,
            '克': 1, '千克': 1000, '毫克': 0.001, '吨': 1000000,
            
            # 数据存储 (基本单位: 字节)
            'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4,
            'byte': 1, 'bit': 0.125,
        }
        
        # 单位类别
        self.categories = {
            'length': ['m', 'km', 'cm', 'mm', 'mile', 'yard', 'foot', 'inch', '米', '千米', '厘米', '毫米'],
            'weight': ['g', 'kg', 'mg', 'ton', 'lb', 'oz', '克', '千克', '毫克', '吨'],
            'data': ['B', 'KB', 'MB', 'GB', 'TB', 'byte', 'bit'],
        }
    
    def _get_category(self, unit: str) -> Optional[str]:
        for category, units in self.categories.items():
            if unit in units:
                return category
        return None
    
    async def execute(self, value: float, from_unit: str, to_unit: str) -> ToolResult:
        """执行单位转换"""
        try:
            # 温度特殊处理
            if from_unit.lower() in ['celsius', 'c', '摄氏度'] and to_unit.lower() in ['fahrenheit', 'f', '华氏度']:
                result = value * 9/5 + 32
                return ToolResult(
                    success=True,
                    output=f"{value}°C = {result:.2f}°F",
                    data={"value": value, "from": from_unit, "to": to_unit, "result": result}
                )
            elif from_unit.lower() in ['fahrenheit', 'f', '华氏度'] and to_unit.lower() in ['celsius', 'c', '摄氏度']:
                result = (value - 32) * 5/9
                return ToolResult(
                    success=True,
                    output=f"{value}°F = {result:.2f}°C",
                    data={"value": value, "from": from_unit, "to": to_unit, "result": result}
                )
            
            # 检查单位是否支持
            if from_unit not in self.conversions:
                return ToolResult(
                    success=False,
                    output=f"不支持的源单位: {from_unit}",
                    error="unsupported_unit"
                )
            if to_unit not in self.conversions:
                return ToolResult(
                    success=False,
                    output=f"不支持的目标单位: {to_unit}",
                    error="unsupported_unit"
                )
            
            # 检查单位是否属于同一类别
            from_category = self._get_category(from_unit)
            to_category = self._get_category(to_unit)
            
            if from_category != to_category:
                return ToolResult(
                    success=False,
                    output=f"无法在不同类别的单位之间转换: {from_unit}({from_category}) -> {to_unit}({to_category})",
                    error="category_mismatch"
                )
            
            # 执行转换
            base_value = value * self.conversions[from_unit]
            result = base_value / self.conversions[to_unit]
            
            return ToolResult(
                success=True,
                output=f"{value} {from_unit} = {result:.6g} {to_unit}",
                data={"value": value, "from": from_unit, "to": to_unit, "result": result}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"单位转换错误: {str(e)}",
                error=str(e)
            )


class ActivateSkillInput(BaseModel):
    skill_name: str = Field(..., min_length=1, description="要激活的 skill 名称，必须来自当前可用 skill 目录。")
    mode: Literal["replace", "append"] = Field(
        default="replace",
        description="replace 表示替换当前会话的 active skills；append 表示追加。",
    )


class ActivateSkillTool(ToolBase):
    name = "activate_skill"
    description = "为当前会话激活一个技能包。激活后，后续同一会话会自动带上该 skill 的说明与约束，由主模型继续决定直接回答还是调用相关工具。"
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "要激活的 skill 名称，例如 paper-reproduction。",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "default": "replace",
                "description": "replace=替换当前 active skills；append=追加。",
            },
        },
        "required": ["skill_name"],
    }
    input_model = ActivateSkillInput

    def __init__(self, *, user_id: int, conversation_id: Optional[int]):
        self.user_id = int(user_id)
        self.conversation_id = int(conversation_id) if conversation_id is not None else None

    async def _execute(self, skill_name: str, mode: str = "replace") -> ToolResult:
        from app.services.agent_runtime_service import get_agent_runtime_service
        from app.services.agent_skill_service import get_agent_skill_service

        if self.conversation_id is None:
            contract = build_tool_error_contract(
                code="conversation_required",
                message="activate_skill 只能在绑定会话的 chat 回合中使用。",
                tool_name=self.name,
                stage="execute",
                retryable=False,
            )
            return ToolResult(
                success=False,
                output=str(contract["message"]),
                error=str(contract["code"]),
                data=merge_error_contract(None, contract),
            )

        skill_service = get_agent_skill_service()
        skill = skill_service.get_skill(skill_name)
        if skill is None:
            available = [item.name for item in skill_service._load_skills()]
            contract = build_tool_error_contract(
                code="skill_not_found",
                message=f"未找到 skill: {skill_name}",
                tool_name=self.name,
                stage="execute",
                retryable=False,
                metadata={"available_skills": available},
            )
            return ToolResult(
                success=False,
                output=f"{contract['message']}。可用 skills: {', '.join(available)}",
                error=str(contract["code"]),
                data=merge_error_contract(None, contract),
            )

        runtime_service = get_agent_runtime_service()
        state = dict(await runtime_service.get_conversation_context_state(self.conversation_id) or {})
        current_active = [
            str(item or "").strip()
            for item in list(state.get("active_skill_names") or [])
            if str(item or "").strip()
        ]
        normalized_mode = str(mode or "replace").strip().lower() or "replace"
        if normalized_mode == "append":
            next_active = list(current_active)
            if skill.name not in next_active:
                next_active.append(skill.name)
        else:
            next_active = [skill.name]
        state["active_skill_names"] = next_active
        state["active_skill_updated_at"] = datetime.utcnow().isoformat()
        await runtime_service.upsert_conversation_context_state(self.conversation_id, state)

        lines = [
            f"已激活 skill: {skill.name}",
            f"- conversation_id: {self.conversation_id}",
            f"- active skills: {', '.join(next_active)}",
        ]
        if str(skill.when_to_use or "").strip():
            lines.append(f"- when_to_use: {str(skill.when_to_use).strip()}")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "activated_skill": skill.name,
                "active_skill_names": next_active,
                "mode": normalized_mode,
            },
        )


class LiteratureSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    source: str = Field(default="auto")
    max_results: int = Field(default=5, ge=1, le=20)
    year_start: Optional[int] = None
    year_end: Optional[int] = None


class LiteratureSearchTool(ToolBase):
    """学术文献搜索工具 - 自动在多个学术数据源间回退。"""
    name = "literature_search"
    parallel_safe = True
    description = "搜索学术论文和文献。默认会自动尝试 OpenAlex、Semantic Scholar、arXiv、PubMed、CrossRef，并在需要时进行多源融合。适用于学术研究、文献综述、找相关论文等场景。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，可以是论文标题、作者名、研究主题等"
            },
            "source": {
                "type": "string",
                "description": "数据源: auto（默认，自动多路尝试）、semantic_scholar、arxiv、pubmed、openalex、crossref，或 multi（多源并行融合）",
                "enum": ["auto", "semantic_scholar", "arxiv", "pubmed", "openalex", "crossref", "multi"],
                "default": "auto"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            },
            "year_start": {
                "type": "integer",
                "description": "起始年份过滤（可选）"
            },
            "year_end": {
                "type": "integer",
                "description": "结束年份过滤（可选）"
            }
        },
        "required": ["query"]
    }
    input_model = LiteratureSearchInput
    
    def __init__(self):
        from app.services.literature_service import get_literature_service
        self.service = get_literature_service()
    
    async def _execute(
        self,
        query: str,
        source: str = "auto",
        max_results: int = 5,
        year_start: int = None,
        year_end: int = None
    ) -> ToolResult:
        """执行学术文献搜索"""
        logger.info(f"[LiteratureSearch] 搜索: {query}, source={source}")
        
        try:
            kwargs = {}
            if year_start and year_end:
                kwargs["year_range"] = (year_start, year_end)
            
            if source == "multi":
                multi_source_count = 4
                if hasattr(self.service, "multi_source_count"):
                    try:
                        multi_source_count = int(self.service.multi_source_count())
                    except Exception:
                        multi_source_count = 4
                per_source = max(1, math.ceil(max_results / max(1, multi_source_count)))
                result = await self.service.search_multi(
                    query=query,
                    limit_per_source=per_source,
                    **kwargs,
                )
                papers = result.get("papers", [])[:max_results]
                result["papers"] = papers
            else:
                result = await self.service.search(
                    query=query,
                    source=source,
                    limit=max_results,
                    **kwargs,
                )

            if "error" in result:
                return ToolResult(
                    success=False,
                    output=f"搜索失败: {result['error']}",
                    error=result["error"]
                )
            
            papers = result.get("papers", [])
            requested_source = str(source or "auto").strip() or "auto"
            resolved_source = str(result.get("resolved_source") or requested_source).strip() or requested_source
            
            if not papers:
                return ToolResult(
                    success=True,
                    output=f"未找到关于 '{query}' 的学术论文。",
                    data={
                        "papers": [],
                        "query": query,
                        "source": requested_source,
                        "resolved_source": result.get("resolved_source"),
                        "attempted_sources": result.get("attempted_sources", []),
                        "partial_errors": result.get("partial_errors", {}),
                    }
                )
            
            # 格式化输出
            output = self._format_results(query, resolved_source, papers)
            
            return ToolResult(
                success=True,
                output=output,
                data={
                    "papers": [self._paper_to_dict(p) for p in papers],
                    "query": query,
                    "source": requested_source,
                    "resolved_source": resolved_source,
                    "attempted_sources": result.get("attempted_sources", []),
                    "partial_errors": result.get("partial_errors", {}),
                    "sources": result.get("sources", {}),
                    "total": result.get("total", len(papers))
                }
            )
            
        except Exception as e:
            logger.error(f"[LiteratureSearch] 搜索错误: {e}")
            return ToolResult(
                success=False,
                output=f"文献搜索错误: {str(e)}",
                error=str(e)
            )
    
    def _format_results(self, query: str, source: str, papers: list) -> str:
        """格式化搜索结果"""
        source_name = {
            "auto": "自动学术搜索链",
            "semantic_scholar": "Semantic Scholar",
            "arxiv": "arXiv",
            "pubmed": "PubMed",
            "openalex": "OpenAlex",
            "crossref": "CrossRef",
            "multi": "OpenAlex + Semantic Scholar + arXiv + PubMed",
        }.get(source, source)
        output_parts = [f"在 {source_name} 搜索 '{query}' 的结果：\n"]
        
        for i, paper in enumerate(papers, 1):
            # 作者列表
            authors = paper.authors[:3] if paper.authors else []
            author_str = ", ".join([a.get("name", "") for a in authors])
            if len(paper.authors) > 3:
                author_str += " 等"
            
            output_parts.append(f"\n【{i}】{paper.title}")
            if paper.year:
                output_parts.append(f" ({paper.year})")
            output_parts.append(f"\n作者: {author_str or '未知'}")
            
            if paper.venue:
                output_parts.append(f"\n发表: {paper.venue}")
            
            if paper.citation_count > 0:
                output_parts.append(f"\n引用数: {paper.citation_count}")
            
            if paper.abstract:
                # 截断摘要
                abstract = paper.abstract[:200] + "..." if len(paper.abstract) > 200 else paper.abstract
                output_parts.append(f"\n摘要: {abstract}")
            
            if paper.url:
                output_parts.append(f"\n链接: {paper.url}")
            
            output_parts.append("\n")
        
        return "".join(output_parts)
    
    def _paper_to_dict(self, paper) -> dict:
        """将论文对象转换为字典"""
        return {
            "source": paper.source,
            "external_id": paper.external_id,
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "venue": paper.venue,
            "citation_count": paper.citation_count,
            "reference_count": paper.reference_count,
            "url": paper.url,
            "pdf_url": paper.pdf_url,
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "fields_of_study": paper.fields_of_study
        }


class DefaultToolProvider:
    """默认工具提供器：负责实例化工具，不负责注册。"""

    def build_default_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        tools: List[Tool] = []
        if (ctx.db or ctx.db_session_factory) and ctx.user_id:
            if str(getattr(ctx, "route_profile", "chat") or "chat").strip().lower() == "chat" and ctx.conversation_id:
                tools.append(
                    ActivateSkillTool(
                        user_id=int(ctx.user_id),
                        conversation_id=int(ctx.conversation_id),
                    )
                )
            tools.append(
                KnowledgeSearchTool(
                    ctx.db,
                    int(ctx.user_id),
                    db_session_factory=ctx.db_session_factory,
                )
            )
            if str(getattr(ctx, "route_profile", "chat") or "chat").strip().lower() == "chat":
                tools.extend(
                    [
                        PaperResearchPrepareTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchStatusTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchCloneRepoTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchArtifactManifestTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadArtifactTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadRepoFileTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchSearchRepoTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteImplementationSpecTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadImplementationSpecTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteRunDraftsTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadRunDraftsTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchInspectRuntimeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteExecutionSpecTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadExecutionSpecTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchStartExecutionTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                            conversation_id=ctx.conversation_id,
                            route_profile=ctx.route_profile,
                        ),
                        PaperResearchReadExecutionTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchCancelExecutionTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchCreateRunDraftTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                    ]
                )

        tools.extend(
            [
                WebSearchTool(),
                WebScrapeTool(),
                CalculatorTool(),
                DateTimeTool(),
                TextAnalysisTool(),
                UnitConverterTool(),
                LiteratureSearchTool(),
            ]
        )
        return tools

    def build_notebook_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        if not ctx.notebook_id or not ctx.kernel_manager:
            return []
        try:
            from app.services.notebook_tools import create_notebook_tools

            return create_notebook_tools(
                kernel_manager=ctx.kernel_manager,
                notebooks_store=ctx.notebooks_store,
                notebook_id=ctx.notebook_id,
                user_authorized=ctx.user_authorized,
            )
        except Exception as exc:
            logger.warning(f"无法构建 Notebook 工具集: {exc}")
            return []


class ToolRegistry:
    """工具注册表 - 支持 Notebook 工具扩展"""
    _mcp_route_circuit_state: Dict[str, Dict[str, Any]] = {}
    _shared_mcp_client_manager: Any = None
    _shared_mcp_tool_schemas: Dict[str, Any] = {}
    _shared_mcp_refresh_lock: Optional[asyncio.Lock] = None
    _ROUTE_PROFILE_CHAT = "chat"
    _ROUTE_PROFILE_CODELAB = "codelab"
    _INTENT_TOOL_MAP: Dict[str, Set[str]] = {
        "knowledge_query": {"knowledge_search"},
        "web_query": {"web_search", "web_scrape"},
        "code_task": {
            "notebook_execute",
            "notebook_variables",
            "notebook_cell",
            "pip_install",
            "code_analysis",
            "calculator",
            "unit_converter",
        },
        "literature_task": {"literature_search"},
        "general_chat": {"datetime", "calculator", "text_analysis"},
    }
    _CODELAB_INTENT_TOOL_MAP: Dict[str, Set[str]] = {
        "knowledge_query": {"knowledge_search"},
        "web_query": {"web_search", "web_scrape"},
        "code_task": {
            "notebook_execute",
            "notebook_variables",
            "notebook_cell",
            "notebook_cleanup",
            "pip_install",
            "code_analysis",
            "calculator",
            "unit_converter",
            "text_analysis",
        },
        "literature_task": {"literature_search"},
        "general_chat": {"datetime", "calculator", "text_analysis"},
    }
    _CODELAB_NOTEBOOK_BASE_TOOLS: Set[str] = {
        "notebook_execute",
        "notebook_variables",
        "notebook_cell",
        "notebook_cleanup",
        "pip_install",
        "code_analysis",
    }
    _CODELAB_NOTEBOOK_MUTATION_TOOLS: Set[str] = {
        "notebook_execute",
        "notebook_cleanup",
        "pip_install",
    }
    _CODELAB_FALLBACK_ALLOWLIST: Set[str] = {
        "datetime",
        "calculator",
        "text_analysis",
        "unit_converter",
    }
    _ALWAYS_AVAILABLE_TOOL_NAMES: Set[str] = {"activate_skill"}
    _CODELAB_FOLLOWUP_ONLY_PATTERNS: tuple[str, ...] = (
        r"^\s*(继续|继续说|继续做|继续分析|继续处理|接着|然后|然后呢|展开|继续下去)\s*$",
        r"^\s*(continue|go on|keep going|retry|again|fix it|continue please)\s*$",
    )
    _CODELAB_NEGATIVE_WEB_PATTERNS: tuple[str, ...] = (
        r"(不要|别|不用|无需|不必|不能|禁止|先不要|暂时不要)\s*(再)?\s*(去)?\s*(联网|上网|搜索互联网|搜(索)?网页|搜(索)?网站|查(看)?网页|查(看)?网站|web|internet|online)",
        r"(不要|别|不用|无需|不必|不能|禁止|先不要|暂时不要).{0,6}(网页|网站|web|internet|online)",
    )
    _CODELAB_NEGATIVE_KNOWLEDGE_PATTERNS: tuple[str, ...] = (
        r"(不要|别|不用|无需|不必|不能|禁止|先不要|暂时不要)\s*(查|用|走)?\s*(知识库|rag|向量检索|knowledge base|vector store|kb)",
    )
    
    def __init__(
        self, 
        db: AsyncSession = None, 
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
        user_id: int = None,
        conversation_id: Optional[int] = None,
        # Notebook 上下文参数
        notebook_id: str = None,
        kernel_manager = None,
        notebooks_store: dict = None,
        user_authorized: bool = False,  # 用户是否授权 Agent 操作 Notebook
        tool_provider: Optional[ToolProvider] = None,
        route_profile: Optional[str] = None,
        initialize_mcp: bool = True,
    ):
        self.db = db
        self.db_session_factory = db_session_factory
        self.user_id = user_id
        self.conversation_id = int(conversation_id) if conversation_id is not None else None
        self.notebook_id = notebook_id
        self.kernel_manager = kernel_manager
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
        profile = str(route_profile or "").strip().lower()
        if profile not in {self._ROUTE_PROFILE_CHAT, self._ROUTE_PROFILE_CODELAB}:
            profile = self._ROUTE_PROFILE_CODELAB if notebook_id and kernel_manager else self._ROUTE_PROFILE_CHAT
        self.route_profile = profile
        self._tools: Dict[str, Tool] = {}
        self._mcp_tools: Dict[str, MCPRemoteTool] = {}
        self._mcp_client_manager: Any = None
        self._initialize_mcp = bool(initialize_mcp)
        self._tool_provider: ToolProvider = tool_provider or DefaultToolProvider()
        self._tool_context = ToolDependencyContext(
            db=self.db,
            db_session_factory=self.db_session_factory,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            notebook_id=self.notebook_id,
            kernel_manager=self.kernel_manager,
            notebooks_store=self.notebooks_store,
            user_authorized=self.user_authorized,
            route_profile=self.route_profile,
        )
        self._mcp_tool_routes: Dict[str, List[str]] = self._load_mcp_tool_routes()
        self._register_default_tools()
        
        # 如果提供了 Notebook 上下文，注册 Notebook 工具
        if notebook_id and kernel_manager:
            self._register_notebook_tools()

        if self._initialize_mcp:
            self._init_mcp_client_manager()
    
    def _register_default_tools(self):
        """注册默认工具"""
        for tool in self._tool_provider.build_default_tools(self._tool_context):
            self.register(tool)
    
    def _register_notebook_tools(self):
        """注册 Notebook 专用工具"""
        notebook_tools = self._tool_provider.build_notebook_tools(self._tool_context)
        for tool in notebook_tools:
            self.register(tool)
        if notebook_tools:
            logger.info(f"已注册 Notebook 工具集，授权状态: {self.user_authorized}")

    def _init_mcp_client_manager(self) -> None:
        """Initialize MCP client manager when MCP is enabled."""
        if not settings.mcp_enabled:
            return
        try:
            self._mcp_client_manager = self._ensure_shared_mcp_client_manager()
            self._sync_local_mcp_tools_from_shared_cache()
            logger.info("[MCP] MCP client manager initialized (shared)")
        except Exception as exc:
            logger.warning(f"[MCP] init failed, fallback to local tools only: {exc}")
            self._mcp_client_manager = None

    def _ensure_shared_mcp_client_manager(self):
        cls = type(self)
        if cls._shared_mcp_client_manager is not None:
            return cls._shared_mcp_client_manager
        manager = self._create_mcp_client_manager()
        cls._shared_mcp_client_manager = manager
        return manager

    @classmethod
    def _create_standalone_mcp_client_manager(cls):
        from app.services.mcp import MCPClientManager, MCPServerManager, load_mcp_server_configs

        configs = load_mcp_server_configs(
            settings.mcp_servers,
            settings.mcp_call_timeout_seconds,
            config_path=getattr(settings, "mcp_config_path", ""),
        )
        if not configs:
            logger.warning("[MCP] MCP_ENABLED=true but MCP_SERVERS is empty")

        server_manager = MCPServerManager(configs)
        return MCPClientManager(server_manager, tool_prefix=settings.mcp_tool_prefix)

    def _create_mcp_client_manager(self):
        return self._create_standalone_mcp_client_manager()

    @classmethod
    def _get_shared_mcp_refresh_lock(cls) -> asyncio.Lock:
        if cls._shared_mcp_refresh_lock is None:
            cls._shared_mcp_refresh_lock = asyncio.Lock()
        return cls._shared_mcp_refresh_lock

    def _sync_local_mcp_tools_from_shared_cache(self) -> None:
        if not self._mcp_client_manager:
            return
        self._mcp_tools = {
            schema.qualified_name: MCPRemoteTool(schema=schema, mcp_client_manager=self._mcp_client_manager)
            for schema in self._shared_mcp_tool_schemas.values()
        }

    @classmethod
    async def _discover_shared_mcp_tool_schemas(cls, *, force_refresh: bool = False) -> Dict[str, Any]:
        manager = cls._shared_mcp_client_manager
        if manager is None:
            manager = cls._create_standalone_mcp_client_manager()
            cls._shared_mcp_client_manager = manager
        if manager is None:
            return {}
        if cls._shared_mcp_tool_schemas and not force_refresh:
            return dict(cls._shared_mcp_tool_schemas)

        async with cls._get_shared_mcp_refresh_lock():
            if cls._shared_mcp_tool_schemas and not force_refresh:
                return dict(cls._shared_mcp_tool_schemas)
            schemas = await manager.discover_tools(force_refresh=force_refresh)
            cls._shared_mcp_tool_schemas = {
                schema.qualified_name: schema
                for schema in list(schemas or [])
                if getattr(schema, "qualified_name", None)
            }
            return dict(cls._shared_mcp_tool_schemas)

    @classmethod
    async def warmup_shared_mcp_tools(cls, *, force_refresh: bool = False) -> Dict[str, Any]:
        if not settings.mcp_enabled:
            return {"enabled": False, "status": "disabled", "tool_count": 0}
        try:
            schemas = await cls._discover_shared_mcp_tool_schemas(force_refresh=force_refresh)
        except Exception as exc:
            logger.warning(f"[MCP] shared warmup failed: {exc}")
            return {
                "enabled": True,
                "status": "error",
                "tool_count": 0,
                "error": str(exc),
            }
        return {
            "enabled": True,
            "status": "ready",
            "tool_count": len(schemas),
        }

    @classmethod
    def reset_shared_mcp_cache(cls) -> None:
        cls._shared_mcp_client_manager = None
        cls._shared_mcp_tool_schemas = {}
        cls._shared_mcp_refresh_lock = None

    def _load_mcp_tool_routes(self) -> Dict[str, List[str]]:
        """Load local-tool to remote-tool route mappings from MCP_TOOL_ROUTES."""
        raw = (getattr(settings, "mcp_tool_routes", "") or "").strip()
        if not raw:
            return {}

        try:
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning(f"[MCP] invalid MCP_TOOL_ROUTES JSON: {exc}")
            return {}

        if not isinstance(payload, dict):
            logger.warning("[MCP] MCP_TOOL_ROUTES must be a JSON object")
            return {}

        routes: Dict[str, List[str]] = {}
        for local_tool, remote_tools in payload.items():
            local_name = str(local_tool or "").strip()
            if not local_name:
                continue

            if isinstance(remote_tools, str):
                candidates = [remote_tools.strip()] if remote_tools.strip() else []
            elif isinstance(remote_tools, list):
                candidates = [str(item).strip() for item in remote_tools if str(item).strip()]
            else:
                logger.warning(f"[MCP] skip invalid route for tool={local_name}, expected string/list")
                continue

            if candidates:
                routes[local_name] = candidates
        return routes

    @classmethod
    def _is_circuit_open(cls, route_key: str) -> bool:
        state = cls._mcp_route_circuit_state.get(route_key)
        if not state:
            return False

        opened_until = float(state.get("opened_until", 0.0) or 0.0)
        if opened_until <= 0:
            return False

        now = time.time()
        if now < opened_until:
            return True

        state["opened_until"] = 0.0
        state["failures"] = 0
        return False

    @classmethod
    def _record_circuit_success(cls, route_key: str) -> None:
        state = cls._mcp_route_circuit_state.setdefault(
            route_key,
            {"failures": 0, "opened_until": 0.0},
        )
        state["failures"] = 0
        state["opened_until"] = 0.0

    @classmethod
    def _record_circuit_failure(cls, route_key: str, error: str) -> None:
        state = cls._mcp_route_circuit_state.setdefault(
            route_key,
            {"failures": 0, "opened_until": 0.0},
        )
        state["failures"] = int(state.get("failures", 0)) + 1

        threshold = max(int(getattr(settings, "mcp_route_circuit_breaker_failures", 3)), 1)
        if state["failures"] < threshold:
            return

        open_seconds = max(int(getattr(settings, "mcp_route_circuit_breaker_open_seconds", 120)), 1)
        state["opened_until"] = time.time() + open_seconds
        state["failures"] = 0
        logger.warning(
            f"[MCP] circuit opened route={route_key}, open_seconds={open_seconds}, last_error={error}"
        )

    async def _resolve_routed_mcp_schema(self, route_key: str) -> Any:
        if not self._mcp_client_manager:
            return None

        resolve_schema = getattr(self._mcp_client_manager, "resolve_tool_schema", None)
        schema = resolve_schema(route_key) if callable(resolve_schema) else None
        if schema is not None:
            return schema

        discover_tools = getattr(self._mcp_client_manager, "discover_tools", None)
        if callable(discover_tools):
            maybe_awaitable = discover_tools(force_refresh=False)
            if hasattr(maybe_awaitable, "__await__"):
                await maybe_awaitable
            schema = resolve_schema(route_key) if callable(resolve_schema) else None
        return schema

    def _translate_routed_web_search_arguments(
        self,
        *,
        schema: Any,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        properties = _coerce_mcp_schema_properties(schema)
        translated: Dict[str, Any] = {}
        query = str(arguments.get("query") or arguments.get("q") or "").strip()
        if not query:
            return dict(arguments)

        query_key = _pick_mcp_property_name(
            properties,
            ["query", "q", "search_term", "searchTerm", "keywords", "keyword", "term", "input"],
        )
        translated[query_key or "query"] = query

        max_results_value = arguments.get("max_results")
        if max_results_value is not None:
            count_key = _pick_mcp_property_name(
                properties,
                ["max_results", "maxResults", "limit", "count", "num_results", "numResults", "size", "top_k", "topK", "k"],
            )
            if count_key:
                translated[count_key] = int(max_results_value)
            elif "max_results" in arguments:
                translated["max_results"] = int(max_results_value)

        categories_key = _pick_mcp_property_name(properties, ["categories", "category"])
        if categories_key and categories_key not in translated and categories_key not in arguments:
            default_categories = _default_routed_search_categories(query)
            if default_categories:
                translated[categories_key] = default_categories

        ignore_invalid_key = _pick_mcp_property_name(
            properties,
            ["ignoreInvalidURLs", "ignore_invalid_urls", "ignoreInvalidUrls"],
        )
        if ignore_invalid_key and ignore_invalid_key not in translated and ignore_invalid_key not in arguments:
            translated[ignore_invalid_key] = True

        scrape_options_key = _pick_mcp_property_name(
            properties,
            ["scrapeOptions", "scrape_options", "extractOptions", "extract_options"],
        )
        if scrape_options_key and scrape_options_key not in translated and scrape_options_key not in arguments:
            scrape_options: Dict[str, Any] = {}
            requested_formats = _normalize_requested_formats(arguments.get("formats"))
            scrape_options["formats"] = requested_formats or ["markdown"]
            scrape_options["onlyMainContent"] = bool(arguments.get("only_main_content", True))
            scrape_options["removeBase64Images"] = True
            scrape_options["blockAds"] = True
            translated[scrape_options_key] = scrape_options

        return _copy_matching_mcp_arguments(
            translated=translated,
            original=arguments,
            properties=properties,
        )

    def _translate_routed_web_scrape_arguments(
        self,
        *,
        schema: Any,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        properties = _coerce_mcp_schema_properties(schema)
        translated: Dict[str, Any] = {}
        url = str(arguments.get("url") or arguments.get("href") or "").strip()
        if not url:
            return dict(arguments)

        if "urls" in properties:
            translated["urls"] = [url]
        else:
            url_key = _pick_mcp_property_name(properties, ["url", "href", "link"])
            translated[url_key or "url"] = url

        requested_formats = _normalize_requested_formats(arguments.get("formats"))
        format_key = _pick_mcp_property_name(properties, ["formats", "format", "response_format", "output_format"])
        if format_key:
            if str(format_key).strip().lower() == "formats":
                translated[format_key] = requested_formats or ["markdown"]
            else:
                translated[format_key] = _choose_scrape_format_value(properties.get(format_key) or {}, requested_formats)
        else:
            extract_key = _pick_mcp_property_name(properties, ["extract", "mode"])
            if extract_key:
                translated[extract_key] = _choose_scrape_format_value(properties.get(extract_key) or {}, requested_formats)

        only_main_content = bool(arguments.get("only_main_content", True))
        main_content_key = _pick_mcp_property_name(
            properties,
            ["only_main_content", "onlyMainContent", "main_content_only", "mainContentOnly"],
        )
        if main_content_key:
            translated[main_content_key] = only_main_content

        block_ads_key = _pick_mcp_property_name(properties, ["blockAds", "block_ads"])
        if block_ads_key and block_ads_key not in translated and block_ads_key not in arguments:
            translated[block_ads_key] = True

        remove_base64_key = _pick_mcp_property_name(
            properties,
            ["removeBase64Images", "remove_base64_images"],
        )
        if remove_base64_key and remove_base64_key not in translated and remove_base64_key not in arguments:
            translated[remove_base64_key] = True

        prompt_key = _pick_mcp_property_name(properties, ["prompt", "instruction", "instructions"])
        if prompt_key and prompt_key not in translated and prompt_key not in arguments:
            translated[prompt_key] = _build_routed_scrape_prompt(
                formats=requested_formats,
                only_main_content=only_main_content,
            )

        return _copy_matching_mcp_arguments(
            translated=translated,
            original=arguments,
            properties=properties,
        )

    def _translate_routed_mcp_arguments(
        self,
        *,
        tool_name: str,
        route_key: str,
        schema: Any,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        if tool_name == "web_search":
            return self._translate_routed_web_search_arguments(schema=schema, arguments=arguments)
        if tool_name == "web_scrape":
            return self._translate_routed_web_scrape_arguments(schema=schema, arguments=arguments)
        return dict(arguments)

    def _decorate_routed_mcp_result(
        self,
        *,
        tool_name: str,
        route_key: str,
        schema: Any,
        original_arguments: Dict[str, Any],
        routed_arguments: Dict[str, Any],
        result: Any,
    ) -> Dict[str, Any]:
        base_data = result.data if isinstance(result.data, dict) else {"raw": result.data}
        normalized_data = dict(base_data)
        try:
            from app.services.mcp.client import MCPClientManager

            normalized_enrichment = MCPClientManager._normalize_enrichment_payload(
                schema=schema,
                arguments=dict(routed_arguments or original_arguments or {}),
                raw_data=normalized_data,
                structured=normalized_data.get("structured_content"),
                fallback_output=str(getattr(result, "output", "") or ""),
            )
        except (ImportError, TypeError, ValueError):
            normalized_enrichment = {}
        if isinstance(normalized_enrichment, dict):
            normalized_data.update(normalized_enrichment)
        provenance = dict(normalized_data.get("provenance") or {})
        provider = str(
            normalized_data.get("provider")
            or provenance.get("provider")
            or getattr(schema, "server_name", "")
            or ""
        ).strip()
        provider_route = str(
            normalized_data.get("provider_route")
            or provenance.get("provider_route")
            or route_key
        ).strip()
        provenance.update(
            {
                "source": "mcp",
                "execution_mode": "routed",
                "provider": provider or str(provenance.get("provider") or "").strip(),
                "provider_route": provider_route,
                "local_tool_name": str(tool_name or "").strip(),
                "remote_server_name": str(
                    provenance.get("remote_server_name") or getattr(schema, "server_name", "") or ""
                ).strip(),
                "remote_tool_name": str(
                    provenance.get("remote_tool_name") or getattr(schema, "tool_name", "") or ""
                ).strip(),
                "qualified_tool_name": str(
                    provenance.get("qualified_tool_name") or getattr(schema, "qualified_name", "") or route_key
                ).strip(),
                "tool_kind": str(provenance.get("tool_kind") or tool_name or "").strip(),
                "normalization_version": "guided_reading_v1",
            }
        )
        if dict(original_arguments or {}) != dict(routed_arguments or {}):
            provenance["argument_translation"] = {
                "applied": True,
                "original_arguments": dict(original_arguments or {}),
                "translated_arguments": dict(routed_arguments or {}),
            }
        normalized_data["provenance"] = provenance
        normalized_data["routed_via_mcp"] = True
        normalized_data["local_tool_name"] = str(tool_name or "").strip()
        normalized_data["provider_route"] = provider_route
        normalized_data["remote_tool_name"] = str(
            normalized_data.get("remote_tool_name") or getattr(schema, "tool_name", "") or ""
        ).strip()
        normalized_data["remote_server_name"] = str(
            normalized_data.get("remote_server_name") or getattr(schema, "server_name", "") or ""
        ).strip()
        normalized_data["tool_kind"] = str(normalized_data.get("tool_kind") or tool_name or "").strip()
        normalized_data["normalization_version"] = "guided_reading_v1"
        if provider:
            normalized_data["provider"] = provider
        return normalized_data

    async def _call_mcp_tool_with_retry(self, route_key: str, arguments: Dict[str, Any]):
        if not self._mcp_client_manager:
            return None

        if self._is_circuit_open(route_key):
            logger.warning(f"[MCP] circuit open, skip route={route_key}")
            return type(
                "MCPRouteResult",
                (),
                {
                    "success": False,
                    "output": f"MCP route circuit open: {route_key}",
                    "data": None,
                    "error": "circuit_open",
                },
            )()

        timeout_seconds = max(int(getattr(settings, "mcp_route_timeout_seconds", 15)), 1)
        retry_attempts = max(int(getattr(settings, "mcp_route_retry_attempts", 2)), 1)
        backoff_seconds = float(getattr(settings, "mcp_route_retry_backoff_seconds", 0.5))
        last_result = None

        for attempt in range(1, retry_attempts + 1):
            try:
                maybe_awaitable = self._mcp_client_manager.call_tool(route_key, arguments)
                result = await asyncio.wait_for(maybe_awaitable, timeout=timeout_seconds)
                last_result = result
                if result.success:
                    self._record_circuit_success(route_key)
                    return result
            except asyncio.TimeoutError:
                last_result = type(
                    "MCPRouteResult",
                    (),
                    {
                        "success": False,
                        "output": f"MCP route timeout after {timeout_seconds}s: {route_key}",
                        "data": None,
                        "error": "timeout",
                    },
                )()
            except Exception as exc:
                last_result = type(
                    "MCPRouteResult",
                    (),
                    {
                        "success": False,
                        "output": f"MCP route call failed: {exc}",
                        "data": None,
                        "error": "mcp_route_exception",
                    },
                )()

            if attempt < retry_attempts and backoff_seconds > 0:
                await asyncio.sleep(backoff_seconds * attempt)

        if last_result and not last_result.success:
            self._record_circuit_failure(route_key, str(last_result.error or "unknown_error"))
        return last_result

    async def _execute_routed_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[ToolResult]:
        """Try remote MCP routes first and fallback to local tool when all fail."""
        if not self._mcp_client_manager:
            return None

        candidates = self._mcp_tool_routes.get(tool_name) or []
        if not candidates:
            return None

        for route_key in candidates:
            route_schema = await self._resolve_routed_mcp_schema(route_key)
            routed_arguments = self._translate_routed_mcp_arguments(
                tool_name=tool_name,
                route_key=route_key,
                schema=route_schema,
                arguments=arguments,
            )
            result = await self._call_mcp_tool_with_retry(route_key, routed_arguments)
            if not result:
                continue
            if result.success:
                logger.info(f"[MCP] routed success local={tool_name} remote={route_key}")
                return ToolResult(
                    success=True,
                    output=str(result.output),
                    data=self._decorate_routed_mcp_result(
                        tool_name=tool_name,
                        route_key=route_key,
                        schema=route_schema,
                        original_arguments=arguments,
                        routed_arguments=routed_arguments,
                        result=result,
                    ),
                    error=result.error,
                )
            logger.warning(
                f"[MCP] routed call failed local={tool_name} remote={route_key} error={result.error}"
            )

        return None

    async def refresh_mcp_tools(self, force_refresh: bool = False) -> None:
        """Refresh remote MCP tool cache."""
        if not self._mcp_client_manager:
            return

        await self._discover_shared_mcp_tool_schemas(force_refresh=force_refresh)
        self._sync_local_mcp_tools_from_shared_cache()

    def _iter_all_tools(self) -> List[Tool]:
        return list(self._tools.values()) + list(self._mcp_tools.values())

    def _intent_tool_map_for_profile(self) -> Dict[str, Set[str]]:
        if self.route_profile == self._ROUTE_PROFILE_CODELAB:
            return self._CODELAB_INTENT_TOOL_MAP
        return self._INTENT_TOOL_MAP

    def _uses_intent_tool_filtering(self) -> bool:
        """Only codelab keeps intent-based tool narrowing; chat exposes the full pool."""
        if not bool(getattr(settings, "tool_selection_enabled", True)):
            return False
        return self.route_profile == self._ROUTE_PROFILE_CODELAB

    @staticmethod
    def classify_intent(user_text: str) -> str:
        text = (user_text or "").lower()
        if not text.strip():
            return "general_chat"

        if any(token in text for token in ["论文", "文献", "paper", "arxiv", "pubmed", "citation"]):
            return "literature_task"
        if any(
            token in text
            for token in [
                "代码",
                "notebook",
                "python",
                "cell",
                "运行",
                "debug",
                "报错",
                "画图",
                "绘图",
                "可视化",
                "plot",
                "matplotlib",
                "seaborn",
                "pandas",
                "numpy",
                "余弦",
                "正弦",
                "散点图",
                "折线图",
                "柱状图",
            ]
        ):
            return "code_task"

        knowledge_tokens = [
            "知识库",
            "文档",
            "资料",
            "文件",
            "附件",
            "上传资料",
            "上传文件",
            "我上传",
            "kb",
            "rag",
            "chunk",
            "向量检索",
            "knowledge base",
            "vector store",
            "my file",
            "my files",
            "my document",
            "my documents",
            "my docs",
            "uploaded",
            "upload",
        ]
        knowledge_patterns = [
            r"\b(my|this|that)\s+(uploaded\s+)?(pdf|docx?|pptx?|xlsx?|csv|file|document)\b",
            r"\b(summarize|summary|analyze|extract)\s+(my|this|that)\s+(pdf|docx?|file|document)\b",
            r"(根据|基于).*(我(上传|的)?|知识库|文档|资料|文件|附件|pdf)",
            r"(总结|概括|提炼|归纳).*(我(上传|的)?|文档|资料|文件|附件|pdf)",
        ]
        knowledge_hit = any(token in text for token in knowledge_tokens) or any(
            re.search(pattern, text) for pattern in knowledge_patterns
        )
        if knowledge_hit:
            return "knowledge_query"

        if any(
            token in text
            for token in [
                "网页",
                "网站",
                "新闻",
                "实时",
                "today",
                "latest",
                "搜索互联网",
                "web",
                "internet",
                "online",
            ]
        ):
            return "web_query"
        return "general_chat"

    @classmethod
    def classify_codelab_intent(cls, user_text: str) -> str:
        text = str(user_text or "").lower()
        if not text.strip():
            return "code_task"

        if any(token in text for token in ["论文", "文献", "paper", "arxiv", "pubmed", "citation"]):
            return "literature_task"

        notebook_tokens = [
            "代码",
            "notebook",
            "python",
            "cell",
            "单元格",
            "变量",
            "dataframe",
            "df",
            "运行",
            "debug",
            "报错",
            "机器学习",
            "建模",
            "训练",
            "预测",
            "分析",
            "画图",
            "绘图",
            "可视化",
            "plot",
            "matplotlib",
            "seaborn",
            "pandas",
            "numpy",
            "sklearn",
            "model",
            "ml",
        ]
        local_workspace_tokens = [
            "上传",
            "upload",
            "uploaded",
            "文件",
            "csv",
            "xlsx",
            "excel",
            "dataset",
            "data set",
            "数据集",
            "表格",
        ]
        local_only_tokens = [
            "当前 notebook",
            "当前notebook",
            "当前 cell",
            "当前cell",
            "当前单元格",
            "当前状态",
            "工作区",
            "本地文件",
            "已上传",
        ]
        explicit_web = cls._has_codelab_explicit_web_request(text)
        explicit_knowledge = cls._has_codelab_explicit_knowledge_request(text)

        if cls._looks_like_notebook_local_file_task(text):
            return "code_task"
        if (
            (any(token in text for token in notebook_tokens) or any(token in text for token in local_workspace_tokens))
            and (cls._has_codelab_negative_web_instruction(text) or any(token in text for token in local_only_tokens))
        ):
            return "code_task"
        if explicit_web:
            return "web_query"
        if any(token in text for token in notebook_tokens):
            return "code_task"
        if any(token in text for token in local_workspace_tokens) and not explicit_knowledge:
            return "code_task"
        if explicit_knowledge:
            return "knowledge_query"
        return "code_task"

    def _select_codelab_tool_names(self, user_text: str = "") -> List[str]:
        text = str(user_text or "")
        selected: Set[str] = set(self._CODELAB_NOTEBOOK_BASE_TOOLS)
        selected.update(name for name in self._fallback_tools() if name in self._CODELAB_FALLBACK_ALLOWLIST)

        if self._has_codelab_explicit_knowledge_request(text):
            selected.add("knowledge_search")

        if self._has_codelab_explicit_web_request(text):
            selected.update({"web_search", "web_scrape"})

        lowered = text.lower()
        if any(token in lowered for token in ["论文", "文献", "paper", "arxiv", "pubmed", "citation"]):
            selected.add("literature_search")

        if not self.user_authorized:
            selected.difference_update(self._CODELAB_NOTEBOOK_MUTATION_TOOLS)

        for tool in self._iter_all_tools():
            if not tool.name.startswith("mcp."):
                continue
            if "web_search" in selected or "web_scrape" in selected:
                if self._mcp_tool_matches_intent(tool, "web_query"):
                    selected.add(tool.name)
            if "knowledge_search" in selected:
                if self._mcp_tool_matches_intent(tool, "knowledge_query"):
                    selected.add(tool.name)
            if "literature_search" in selected:
                if self._mcp_tool_matches_intent(tool, "literature_task"):
                    selected.add(tool.name)

        return [tool.name for tool in self._iter_all_tools() if tool.name in selected]

    @staticmethod
    def _parse_csv_names(value: str) -> Set[str]:
        return {item.strip() for item in (value or "").split(",") if item.strip()}

    def _fallback_tools(self) -> Set[str]:
        return self._parse_csv_names(str(getattr(settings, "tool_selection_fallback_tools", "")))

    @classmethod
    def _looks_like_codelab_followup_only(cls, user_text: str) -> bool:
        text = str(user_text or "").strip()
        if not text:
            return False
        return any(re.match(pattern, text, re.IGNORECASE) for pattern in cls._CODELAB_FOLLOWUP_ONLY_PATTERNS)

    @classmethod
    def _has_codelab_negative_web_instruction(cls, text: str) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in cls._CODELAB_NEGATIVE_WEB_PATTERNS)

    @classmethod
    def _has_codelab_negative_knowledge_instruction(cls, text: str) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in cls._CODELAB_NEGATIVE_KNOWLEDGE_PATTERNS)

    @classmethod
    def _has_codelab_explicit_web_request(cls, text: str) -> bool:
        if cls._has_codelab_negative_web_instruction(text):
            return False
        return any(
            token in text
            for token in [
                "网页",
                "网站",
                "实时",
                "today",
                "latest",
                "联网",
                "搜索互联网",
                "web",
                "internet",
                "online",
            ]
        )

    @classmethod
    def _has_codelab_explicit_knowledge_request(cls, text: str) -> bool:
        if cls._has_codelab_negative_knowledge_instruction(text):
            return False
        return any(
            token in text
            for token in [
                "知识库",
                "rag",
                "向量检索",
                "knowledge base",
                "vector store",
                "kb",
            ]
        )

    def _mcp_tool_matches_intent(self, tool: Tool, intent: str) -> bool:
        text = f"{tool.name} {tool.description}".lower()
        if intent == "web_query":
            return any(k in text for k in ["web", "search", "scrape", "crawl", "fetch", "browser"])
        if intent == "knowledge_query":
            return any(k in text for k in ["knowledge", "kb", "rag", "vector", "document"])
        if intent == "literature_task":
            return any(k in text for k in ["literature", "paper", "arxiv", "pubmed", "semantic", "crossref", "openalex"])
        if intent == "code_task":
            return any(k in text for k in ["code", "notebook", "python", "execute", "analysis", "pip"])
        return False

    @staticmethod
    def _looks_like_notebook_local_file_task(user_text: str) -> bool:
        text = str(user_text or "").lower()
        if not text.strip():
            return False

        local_file_tokens = [
            "上传",
            "upload",
            "uploaded",
            "文件",
            "csv",
            "xlsx",
            "excel",
            "dataset",
            "data set",
            "数据集",
            "表格",
        ]
        notebook_task_tokens = [
            "notebook",
            "cell",
            "python",
            "机器学习",
            "建模",
            "训练",
            "预测",
            "分析",
            "画图",
            "可视化",
            "案例",
            "pandas",
            "numpy",
            "sklearn",
            "model",
            "ml",
        ]
        return (
            any(token in text for token in local_file_tokens)
            and any(token in text for token in notebook_task_tokens)
            and not ToolRegistry._has_codelab_explicit_knowledge_request(text)
            and not ToolRegistry._has_codelab_explicit_web_request(text)
        )

    def resolve_intent(self, user_text: str) -> str:
        if self.route_profile == self._ROUTE_PROFILE_CODELAB:
            if self.notebook_id and self.kernel_manager and self._looks_like_codelab_followup_only(user_text):
                return "code_task"
            return self.classify_codelab_intent(user_text)
        if self.notebook_id and self.kernel_manager and self._looks_like_notebook_local_file_task(user_text):
            return "code_task"
        return self.classify_intent(user_text)

    def select_tool_names_for_user_text(self, user_text: str = "") -> List[str]:
        if self.route_profile == self._ROUTE_PROFILE_CODELAB:
            return self._select_codelab_tool_names(user_text)
        if not self._uses_intent_tool_filtering():
            return [tool.name for tool in self._iter_all_tools()]
        return self.select_tool_names_for_intent(self.resolve_intent(user_text), user_text=user_text)

    def select_tool_names_for_intent(self, intent: str, user_text: str = "") -> List[str]:
        if not self._uses_intent_tool_filtering():
            return [tool.name for tool in self._iter_all_tools()]

        if self.route_profile == self._ROUTE_PROFILE_CODELAB:
            return self._select_codelab_tool_names(user_text)

        intent_tool_map = self._intent_tool_map_for_profile()
        notebook_local_file_task = bool(
            self.notebook_id and self.kernel_manager and self._looks_like_notebook_local_file_task(user_text)
        )
        codelab_followup_only = bool(
            self.route_profile == self._ROUTE_PROFILE_CODELAB
            and self.notebook_id
            and self.kernel_manager
            and self._looks_like_codelab_followup_only(user_text)
        )
        if notebook_local_file_task or codelab_followup_only:
            resolved_intent = "code_task"
        else:
            resolved_intent = intent if intent in intent_tool_map else self.resolve_intent(user_text)
        selected = set(intent_tool_map.get(resolved_intent, set()))
        selected.update(self._ALWAYS_AVAILABLE_TOOL_NAMES)
        fallback_tools = self._fallback_tools()
        if self.route_profile == self._ROUTE_PROFILE_CODELAB:
            selected.update(name for name in fallback_tools if name in self._CODELAB_FALLBACK_ALLOWLIST)
        else:
            selected.update(fallback_tools)

        # Notebook 场景下，仅在代码任务里默认保留 Notebook 工具，避免普通聊天也被带去读写 Notebook。
        if self.notebook_id and self.kernel_manager and resolved_intent == "code_task":
            selected.update(self._CODELAB_NOTEBOOK_BASE_TOOLS)

        # 未授权时，剥离明确的改写类工具，避免普通问答或建议场景误触写操作。
        if self.route_profile == self._ROUTE_PROFILE_CODELAB and not self.user_authorized:
            selected.difference_update(self._CODELAB_NOTEBOOK_MUTATION_TOOLS)

        for tool in self._iter_all_tools():
            if (
                not notebook_local_file_task
                and tool.name.startswith("mcp.")
                and not (
                    (self.route_profile == self._ROUTE_PROFILE_CODELAB and resolved_intent != "web_query")
                    or (self.route_profile == self._ROUTE_PROFILE_CHAT and resolved_intent == "code_task")
                )
                and self._mcp_tool_matches_intent(tool, resolved_intent)
            ):
                selected.add(tool.name)

        return [tool.name for tool in self._iter_all_tools() if tool.name in selected]

    def _filter_tools(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> List[Tool]:
        tools = self._iter_all_tools()
        if include_tool_names:
            allow = set(include_tool_names)
            return [tool for tool in tools if tool.name in allow]
        if not self._uses_intent_tool_filtering():
            return tools
        if intent:
            names = set(self.select_tool_names_for_intent(intent, user_text=user_text))
            return [tool for tool in tools if tool.name in names]
        return tools
    
    def register(self, tool: Tool):
        """注册工具"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self._tools.get(name) or self._mcp_tools.get(name)
    
    def list_tools(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> List[Dict[str, Any]]:
        """获取工具列表（用于发送给 LLM）"""
        filtered_tools = self._filter_tools(
            intent=intent,
            include_tool_names=include_tool_names,
            user_text=user_text,
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "x_parallel_safe": bool(getattr(tool, "parallel_safe", False)),
                }
            }
            for tool in filtered_tools
        ]
    
    def get_tools_description(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> str:
        """获取工具描述（用于 ReAct prompt）"""
        descriptions = []
        for tool in self._filter_tools(
            intent=intent,
            include_tool_names=include_tool_names,
            user_text=user_text,
        ):
            params = tool.parameters.get('properties', {})
            required = tool.parameters.get('required', [])
            
            params_desc = []
            for k, v in params.items():
                param_str = f"{k}: {v.get('type', 'any')}"
                if k in required:
                    param_str += " (必填)"
                if 'description' in v:
                    param_str += f" - {v['description']}"
                params_desc.append(param_str)
            
            descriptions.append(
                f"**{tool.name}**: {tool.description}\n"
                f"  参数: {', '.join(params_desc) if params_desc else '无'}"
            )
        return "\n\n".join(descriptions)
    
    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行工具"""
        local_tool = self._tools.get(tool_name)
        if local_tool:
            routed_result = await self._execute_routed_mcp_tool(tool_name, kwargs)
            if routed_result:
                return routed_result

        tool = self.get(tool_name)
        if tool:
            try:
                logger.info(f"执行工具: {tool_name}, 参数: {kwargs}")
                result = await tool.execute(**kwargs)
                retry_attempt = None
                if isinstance(result.data, dict):
                    retry_attempt = result.data.get("retry_attempt")
                logger.info(
                    f"工具执行完成: {tool_name}, 成功: {result.success}, "
                    f"retry_attempt={retry_attempt}, "
                    f"execution_time_ms={result.execution_time_ms:.2f}, "
                    f"tokens_est={result.output_tokens_estimate}, truncated={result.truncated}"
                )
                return result
            except Exception as e:
                logger.error(f"工具执行失败 {tool_name}: {e}")
                contract = build_tool_error_contract(
                    code="tool_execute_failed",
                    message="工具执行失败",
                    tool_name=tool_name,
                    stage="dispatch",
                    detail=str(e),
                    retryable=False,
                    metadata={"exception_type": type(e).__name__},
                )
                return ToolResult(
                    success=False,
                    output=f"{contract['message']}: {e}",
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )

        if self._mcp_client_manager:
            mcp_result = await self._mcp_client_manager.call_tool(tool_name, kwargs)
            if mcp_result.error != "tool_not_found":
                return ToolResult(
                    success=mcp_result.success,
                    output=mcp_result.output,
                    data=mcp_result.data,
                    error=mcp_result.error,
                )

        contract = build_tool_error_contract(
            code="tool_not_found",
            message=f"未找到工具: {tool_name}",
            tool_name=tool_name,
            stage="dispatch",
            retryable=False,
            metadata={"available_tools": [t.name for t in self._iter_all_tools()]},
        )
        return ToolResult(
            success=False,
            output=f"{contract['message']}。可用工具: {', '.join(contract['metadata']['available_tools'])}",
            error=str(contract["code"]),
            data=merge_error_contract(None, contract),
        )


def get_tool_registry(
    db: Optional[AsyncSession],
    user_id: int,
    db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    conversation_id: Optional[int] = None,
    route_profile: Optional[str] = None,
    initialize_mcp: bool = True,
) -> ToolRegistry:
    """获取工具注册表"""
    return ToolRegistry(
        db=db,
        user_id=user_id,
        db_session_factory=db_session_factory,
        conversation_id=conversation_id,
        route_profile=route_profile,
        initialize_mcp=initialize_mcp,
    )
