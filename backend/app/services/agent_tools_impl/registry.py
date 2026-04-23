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
from urllib.parse import urljoin, urlparse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, or_, and_, tuple_
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

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
from app.services.aider_cli_service import AiderCliService
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)
from app.services.html_page_semantics import analyze_html_page_semantics, resolve_html_probe_plan_with_llm
from app.services.google_drive_utils import is_google_drive_url, probe_google_drive_confirm_download
from app.services.smart_chunking.token_utils import estimate_tokens, tokens_to_chars
from app.services.zoekt_cli_service import ZoektCliService

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


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return ""
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _collect_schema_constraints(schema: Mapping[str, Any]) -> Dict[str, Any]:
    constraints: Dict[str, Any] = {}
    for key in ("minimum", "maximum", "minLength", "maxLength", "enum", "default"):
        if key in schema:
            constraints[key] = schema[key]
    for branch in list(schema.get("anyOf") or []):
        if isinstance(branch, Mapping):
            for key, value in _collect_schema_constraints(branch).items():
                constraints.setdefault(key, value)
    return constraints


def _sync_tool_parameter_constraints(tool_cls: type) -> None:
    input_model = getattr(tool_cls, "input_model", None)
    parameters = getattr(tool_cls, "parameters", None)
    if input_model is None or not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return

    schema = input_model.model_json_schema()
    model_properties = schema.get("properties")
    if not isinstance(model_properties, dict):
        return

    merged_properties: Dict[str, Any] = dict(properties)
    for field_name, model_meta in model_properties.items():
        manual_meta = merged_properties.get(field_name)
        if not isinstance(manual_meta, dict):
            continue
        merged_meta = dict(manual_meta)
        for key, value in _collect_schema_constraints(model_meta).items():
            merged_meta.setdefault(key, value)
        merged_properties[field_name] = merged_meta

    tool_cls.parameters = {**parameters, "properties": merged_properties}


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
        issues: List[str] = []
        for item in list(exc.errors() or [])[:3]:
            loc = ".".join(str(part) for part in list(item.get("loc") or []) if str(part).strip())
            msg = str(item.get("msg") or "").strip()
            if loc and msg:
                issues.append(f"{loc}: {msg}")
            elif msg:
                issues.append(msg)
        output = "工具参数校验失败，请检查输入格式。"
        if issues:
            output = f"{output} " + "；".join(issues)
        contract = build_tool_error_contract(
            code="validation_error",
            message="工具参数校验失败，请检查输入格式。",
            stage="validate_input",
            detail=str(exc),
            retryable=False,
        )
        return ToolResult(
            success=False,
            output=output,
            error=str(contract["code"]),
            data=merge_error_contract({"validation_errors": exc.errors()}, contract),
        )

    @staticmethod
    def _clamp_ratio(raw_ratio: float) -> float:
        return min(max(raw_ratio, 0.2), 0.9)

    def _truncate_output_if_needed(self, output: str) -> tuple[str, bool, int]:
        safe_output = str(output or "")
        est_tokens = estimate_tokens(safe_output)
        if not bool(getattr(settings, "tool_output_truncation_enabled", False)):
            return safe_output, False, est_tokens
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
    mode: Literal["auto", "full", "chunk", "page", "line_range"] = "auto"
    max_chars: int = Field(default=20000, ge=200, le=200000)
    chunk_index: Optional[int] = Field(default=None, ge=1, le=1000000)
    chunk_chars: Optional[int] = Field(default=None, ge=200, le=200000)
    page: Optional[int] = Field(default=None, ge=1, le=1000000)
    page_size_lines: Optional[int] = Field(default=None, ge=1, le=5000)
    line_start: Optional[int] = Field(default=None, ge=1, le=2000000)
    line_end: Optional[int] = Field(default=None, ge=1, le=2000000)


class PaperResearchSearchOutputsInput(BaseModel):
    project_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=400)
    scope: Literal["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"] = "all"
    max_results: int = Field(default=20, ge=1, le=100)
    case_sensitive: bool = False
    is_regex: bool = False
    context_lines: int = Field(default=0, ge=0, le=20)


class PaperResearchReadRepoFileInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_relative_path: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "repo/source 下的 repo-relative 路径。"
            "如果不确定文件具体在哪个子目录，先用 `paper_research_search_repo`"
            " 按文件名或关键字定位真实路径，不要臆测 `scripts/` 等前缀。"
        ),
        validation_alias=AliasChoices("repo_relative_path", "relative_path", "path", "file_path", "file"),
    )
    mode: str = Field(default="auto")
    max_chars: int = Field(default=20000, ge=1, le=200000)
    chunk_index: Optional[int] = Field(default=None, ge=1, le=1000000)
    chunk_chars: Optional[int] = Field(default=None, ge=1, le=200000)
    page: Optional[int] = Field(default=None, ge=1, le=1000000)
    page_size_lines: Optional[int] = Field(default=None, ge=1, le=5000)
    line_start: Optional[int] = Field(default=None, ge=1, le=2000000)
    line_end: Optional[int] = Field(default=None, ge=1, le=2000000)

    @field_validator("repo_relative_path")
    @classmethod
    def _normalize_repo_relative_path(cls, value: str) -> str:
        normalized = _normalize_relative_path(value)
        if normalized == "repo/source":
            return ""
        if normalized.startswith("repo/source/"):
            return normalized.removeprefix("repo/source/")
        if normalized.startswith("paper_repo/"):
            return normalized.removeprefix("paper_repo/")
        return normalized

    @field_validator("mode")
    @classmethod
    def _normalize_mode(cls, value: str) -> str:
        normalized = str(value or "auto").strip().lower().replace("-", "_")
        alias_map = {
            "auto": "auto",
            "full": "full",
            "all": "full",
            "entire": "full",
            "chunk": "chunk",
            "chunks": "chunk",
            "page": "page",
            "pages": "page",
            "line": "line_range",
            "lines": "line_range",
            "line_range": "line_range",
        }
        resolved = alias_map.get(normalized)
        if not resolved:
            raise ValueError("mode must be one of auto/full/chunk/page/line_range")
        return resolved


class PaperResearchSearchRepoInput(BaseModel):
    project_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=20, ge=1, le=100)
    case_sensitive: bool = False
    is_regex: bool = False
    glob: Optional[str] = Field(default=None, max_length=200)
    context_lines: int = Field(default=0, ge=0, le=20)


class PaperResearchBuildZoektIndexInput(BaseModel):
    project_id: int = Field(ge=1)
    force_reindex: bool = False


class PaperResearchSearchRepoZoektInput(BaseModel):
    project_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=800)
    max_results: int = Field(default=20, ge=1, le=100)
    context_lines: int = Field(default=0, ge=0, le=20)
    auto_index: bool = True
    force_reindex: bool = False


class PaperResearchRunAiderInput(BaseModel):
    project_id: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=12000)
    target_root: Literal["repo", "workspace"] = "repo"
    mode: Literal["code", "architect", "ask"] = "code"
    editable_files: List[str] = Field(default_factory=list)
    read_only_files: List[str] = Field(default_factory=list)
    context_artifacts: List[str] = Field(default_factory=list)
    llm_provider: Optional[str] = Field(default=None, max_length=64)
    model_name: Optional[str] = Field(default=None, max_length=255)
    editor_model: Optional[str] = Field(default=None, max_length=255)
    weak_model: Optional[str] = Field(default=None, max_length=255)
    edit_format: Optional[str] = Field(default=None, max_length=64)
    editor_edit_format: Optional[str] = Field(default=None, max_length=64)
    reasoning_effort: Optional[str] = Field(default=None, max_length=32)
    dry_run: bool = False
    map_tokens: Optional[int] = Field(default=None, ge=0, le=64000)
    api_timeout_seconds: Optional[int] = Field(default=None, ge=30, le=3600)
    auto_test: bool = False
    test_cmd: Optional[str] = Field(default=None, max_length=2000)
    auto_lint: bool = False
    lint_cmds: List[str] = Field(default_factory=list)
    allow_dirty_repo: bool = False


class PaperResearchReadAiderRunInput(BaseModel):
    project_id: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    include_stdout: bool = True
    include_prompt: bool = False
    include_diff: bool = False
    max_chars: int = Field(default=20000, ge=200, le=200000)


class PaperResearchTailAiderLogInput(BaseModel):
    project_id: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    max_chars: int = Field(default=12000, ge=200, le=200000)


class PaperResearchWriteImplementationSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    implementation_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadImplementationSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    mode: Literal["auto", "full", "chunk", "page", "line_range"] = "auto"
    max_chars: int = Field(default=20000, ge=200, le=200000)
    chunk_index: Optional[int] = Field(default=None, ge=1, le=1000000)
    chunk_chars: Optional[int] = Field(default=None, ge=200, le=200000)
    page: Optional[int] = Field(default=None, ge=1, le=1000000)
    page_size_lines: Optional[int] = Field(default=None, ge=1, le=5000)
    line_start: Optional[int] = Field(default=None, ge=1, le=2000000)
    line_end: Optional[int] = Field(default=None, ge=1, le=2000000)


class PaperResearchWriteGroundingReportInput(BaseModel):
    project_id: int = Field(ge=1)
    grounding_report: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadGroundingReportInput(BaseModel):
    project_id: int = Field(ge=1)
    mode: Literal["auto", "full", "chunk", "page", "line_range"] = "auto"
    max_chars: int = Field(default=20000, ge=200, le=200000)
    chunk_index: Optional[int] = Field(default=None, ge=1, le=1000000)
    chunk_chars: Optional[int] = Field(default=None, ge=200, le=200000)
    page: Optional[int] = Field(default=None, ge=1, le=1000000)
    page_size_lines: Optional[int] = Field(default=None, ge=1, le=5000)
    line_start: Optional[int] = Field(default=None, ge=1, le=2000000)
    line_end: Optional[int] = Field(default=None, ge=1, le=2000000)


class PaperResearchWriteRunDraftsInput(BaseModel):
    project_id: int = Field(ge=1)
    run_drafts: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchReadRunDraftsInput(BaseModel):
    project_id: int = Field(ge=1)
    mode: Literal["auto", "full", "chunk", "page", "line_range"] = "auto"
    max_chars: int = Field(default=20000, ge=200, le=200000)
    chunk_index: Optional[int] = Field(default=None, ge=1, le=1000000)
    chunk_chars: Optional[int] = Field(default=None, ge=200, le=200000)
    page: Optional[int] = Field(default=None, ge=1, le=1000000)
    page_size_lines: Optional[int] = Field(default=None, ge=1, le=5000)
    line_start: Optional[int] = Field(default=None, ge=1, le=2000000)
    line_end: Optional[int] = Field(default=None, ge=1, le=2000000)


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
    resolve_download_gate: bool = False


class PaperResearchAssessRepoMainpathInput(BaseModel):
    project_id: int = Field(ge=1)


class PaperResearchListOutputsInput(BaseModel):
    project_id: int = Field(ge=1)
    scope: Literal["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"] = "all"


class PaperResearchDeleteOutputInput(BaseModel):
    project_id: int = Field(ge=1)
    relative_path: str = Field(min_length=1, max_length=400)


class PaperResearchCleanupScopeInput(BaseModel):
    project_id: int = Field(ge=1)
    scope: Literal["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"] = "all"


class PaperResearchGitStatusInput(BaseModel):
    project_id: int = Field(ge=1)
    include_untracked: bool = True
    max_entries: int = Field(default=200, ge=1, le=1000)


class PaperResearchGitDiffInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_relative_paths: List[str] = Field(
        default_factory=list,
        max_length=50,
        validation_alias=AliasChoices("repo_relative_paths", "paths"),
    )
    cached: bool = False
    ref: Optional[str] = Field(default=None, max_length=200)
    max_chars: int = Field(default=20000, ge=200, le=200000)

    @field_validator("repo_relative_paths", mode="before")
    @classmethod
    def _normalize_repo_relative_paths(cls, value: Any) -> List[str]:
        if value is None:
            return []
        raw_items = list(value) if isinstance(value, (list, tuple, set)) else [value]
        normalized_items: List[str] = []
        for item in raw_items:
            normalized = PaperResearchReadRepoFileInput._normalize_repo_relative_path(str(item or ""))
            if normalized:
                normalized_items.append(normalized)
        deduped: List[str] = []
        seen: Set[str] = set()
        for item in normalized_items:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped


class PaperResearchGitLogInput(BaseModel):
    project_id: int = Field(ge=1)
    repo_relative_paths: List[str] = Field(
        default_factory=list,
        max_length=50,
        validation_alias=AliasChoices("repo_relative_paths", "paths"),
    )
    max_count: int = Field(default=10, ge=1, le=100)

    @field_validator("repo_relative_paths", mode="before")
    @classmethod
    def _normalize_repo_relative_paths(cls, value: Any) -> List[str]:
        return PaperResearchGitDiffInput._normalize_repo_relative_paths(value)


class PaperResearchGitShowInput(BaseModel):
    project_id: int = Field(ge=1)
    ref: str = Field(min_length=1, max_length=200)
    repo_relative_path: Optional[str] = Field(
        default=None,
        max_length=400,
        validation_alias=AliasChoices("repo_relative_path", "relative_path", "path", "file_path", "file"),
    )
    max_chars: int = Field(default=20000, ge=200, le=200000)

    @field_validator("repo_relative_path")
    @classmethod
    def _normalize_repo_relative_path(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = PaperResearchReadRepoFileInput._normalize_repo_relative_path(value)
        return normalized or None


class PaperResearchWriteExecutionSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchWriteExecutionScriptInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)
    relative_path: Optional[str] = Field(
        default=None,
        max_length=400,
        validation_alias=AliasChoices("relative_path", "path", "file_path", "filename", "name"),
    )
    content: str = Field(min_length=1, max_length=300000)


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


class PaperResearchTailExecutionLogInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=120)
    max_log_chars: int = Field(default=12000, ge=200, le=200000)


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
    _GROUNDING_STATUSES = {"grounded", "absent", "blocked", "unknown"}
    _CANONICAL_FILE_SPECS: Dict[str, Dict[str, str]] = {
        "planning/paper_intake_result.json": {
            "kind": "planning",
            "name": "paper_intake_result",
            "content_type": "json",
            "actual_rel_path": "paper_intake_result.json",
        },
        "planning/paper_summary.json": {
            "kind": "planning",
            "name": "paper_summary",
            "content_type": "json",
            "actual_rel_path": "paper_summary.json",
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
        "repo/repo_readme_reproduction_intake.json": {
            "kind": "repo",
            "name": "repo_readme_reproduction_intake",
            "content_type": "json",
            "actual_rel_path": "repo_readme_reproduction_intake.json",
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
        "specs/grounding_report.json": {
            "kind": "spec",
            "name": "grounding_report",
            "content_type": "json",
            "actual_rel_path": "specs/grounding_report.json",
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

    @staticmethod
    def _normalize_line_preview(value: str, *, limit: int = 240) -> str:
        text = str(value or "").replace("\r", "").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(40, limit - 3)].rstrip()}..."

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
    def _workspace_missing_required_archives(cls, workspace_dir: Path, *, include_grounding: bool = False) -> bool:
        required_paths = [
            "paper_intake_result.json",
            "paper_summary.json",
            "experiment_spec.json",
            "workspace_adapter_manifest.json",
        ]
        if include_grounding:
            required_paths.append("specs/grounding_report.json")
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
        return _normalize_relative_path(value)

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

    @staticmethod
    def _normalize_repo_relative_path(value: Any) -> str:
        normalized = _normalize_relative_path(value)
        if normalized == "repo/source":
            return ""
        if normalized.startswith("repo/source/"):
            return normalized.removeprefix("repo/source/")
        if normalized.startswith("paper_repo/"):
            return normalized.removeprefix("paper_repo/")
        return normalized

    async def _resolve_repo_workspace(
        self,
        db: AsyncSession,
        *,
        project_id: int,
    ) -> tuple[Optional[Dict[str, Any]], Any, Optional[Path], Optional[ToolResult]]:
        project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
        if project_payload is None:
            return None, None, None, self._project_not_found(project_id)
        if workspace is None:
            return project_payload, None, None, self._workspace_not_ready(project_payload, project_id)
        repo_dir = self._workspace_dir_for(workspace) / "paper_repo"
        if not repo_dir.is_dir():
            return project_payload, workspace, None, ToolResult(
                success=False,
                output="当前 Project 还没有可用的 repo/source。请先调用 paper_research_prepare 或 paper_research_clone_repo。",
                error="repo_not_available",
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "project_id": int(project_id),
                    "relative_path": "repo/source",
                },
            )
        return project_payload, workspace, repo_dir, None

    @classmethod
    async def _run_repo_git_command(
        cls,
        *,
        repo_dir: Path,
        git_args: Sequence[str],
        timeout_seconds: float = 20.0,
    ) -> Dict[str, Any]:
        if not shutil.which("git"):
            return {
                "available": False,
                "returncode": None,
                "stdout": "",
                "stderr": "git_not_installed",
                "command": ["git", *list(git_args)],
            }
        command = ["git", "--no-pager", *list(git_args)]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return {
                "available": True,
                "timeout": True,
                "returncode": None,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "command": command,
            }
        return {
            "available": True,
            "timeout": False,
            "returncode": int(process.returncode or 0),
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "command": command,
        }

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
                # A generic HTML response proves the URL is reachable, but it does
                # not prove the target file is directly downloadable.
                return False, False, "html_page", "use_as_reference_page"
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

    @classmethod
    def _read_text_payload(
        cls,
        path: Path,
        *,
        mode: str,
        max_chars: int,
        chunk_index: Optional[int] = None,
        chunk_chars: Optional[int] = None,
        page: Optional[int] = None,
        page_size_lines: Optional[int] = None,
        line_start: Optional[int],
        line_end: Optional[int],
        default_window: int = 40,
    ) -> Dict[str, Any]:
        content = path.read_text(encoding="utf-8", errors="ignore")
        all_lines = content.splitlines()
        total_lines = len(all_lines)
        total_chars = len(content)
        effective_mode = str(mode or "auto").strip().lower() or "auto"

        if effective_mode == "auto":
            if line_start is not None or line_end is not None:
                effective_mode = "line_range"
            elif page is not None:
                effective_mode = "page"
            elif chunk_index is not None:
                effective_mode = "chunk"
            else:
                effective_mode = "full"

        if effective_mode == "full":
            return {
                "content": content,
                "truncated": False,
                "has_more": False,
                "mode": "full",
                "total_chars": total_chars,
                "returned_chars": total_chars,
                "total_lines": total_lines,
                "returned_line_count": total_lines,
                "line_start": 1 if total_lines > 0 else 0,
                "line_end": total_lines,
            }

        if effective_mode == "chunk":
            resolved_chunk_chars = max(int(chunk_chars or max_chars or 20000), 1)
            resolved_chunk_index = max(int(chunk_index or 1), 1)
            total_chunks = max(1, math.ceil(total_chars / resolved_chunk_chars))
            resolved_chunk_index = min(resolved_chunk_index, total_chunks)
            start_offset = (resolved_chunk_index - 1) * resolved_chunk_chars
            end_offset = min(total_chars, start_offset + resolved_chunk_chars)
            chunk_text = content[start_offset:end_offset]
            return {
                "content": chunk_text,
                "truncated": end_offset < total_chars,
                "has_more": end_offset < total_chars,
                "next_chunk_index": resolved_chunk_index + 1 if end_offset < total_chars else None,
                "mode": "chunk",
                "total_chars": total_chars,
                "returned_chars": len(chunk_text),
                "chunk_index": resolved_chunk_index,
                "chunk_chars": resolved_chunk_chars,
                "total_chunks": total_chunks,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "total_lines": total_lines,
            }

        if effective_mode == "page":
            resolved_page = max(int(page or 1), 1)
            resolved_page_size_lines = max(int(page_size_lines or default_window or 40), 1)
            if total_lines <= 0:
                return {
                    "content": "",
                    "truncated": False,
                    "has_more": False,
                    "mode": "page",
                    "total_chars": total_chars,
                    "returned_chars": 0,
                    "page": resolved_page,
                    "page_size_lines": resolved_page_size_lines,
                    "total_pages": 0,
                    "line_start": 0,
                    "line_end": 0,
                    "returned_line_count": 0,
                    "total_lines": 0,
                }
            total_pages = max(1, math.ceil(total_lines / resolved_page_size_lines))
            resolved_page = min(resolved_page, total_pages)
            resolved_start = ((resolved_page - 1) * resolved_page_size_lines) + 1
            resolved_end = min(total_lines, resolved_start + resolved_page_size_lines - 1)
            selected_lines = all_lines[resolved_start - 1:resolved_end]
            rendered = "\n".join(
                f"{line_no}: {line}"
                for line_no, line in enumerate(selected_lines, start=resolved_start)
            )
            return {
                "content": rendered,
                "truncated": resolved_page < total_pages,
                "has_more": resolved_page < total_pages,
                "next_page": resolved_page + 1 if resolved_page < total_pages else None,
                "mode": "page",
                "total_chars": total_chars,
                "returned_chars": len(rendered),
                "page": resolved_page,
                "page_size_lines": resolved_page_size_lines,
                "total_pages": total_pages,
                "line_start": resolved_start,
                "line_end": resolved_end,
                "returned_line_count": len(selected_lines),
                "total_lines": total_lines,
            }

        if total_lines <= 0:
            return {
                "content": "",
                "truncated": False,
                "has_more": False,
                "total_chars": 0,
                "returned_chars": 0,
                "mode": "line_range",
                "line_start": 0,
                "line_end": 0,
                "returned_line_count": 0,
                "total_lines": 0,
            }

        resolved_start = int(line_start or 0) if line_start is not None else None
        resolved_end = int(line_end or 0) if line_end is not None else None

        if resolved_start is None and resolved_end is None:
            resolved_start = 1
            resolved_end = min(total_lines, max(int(default_window or 40), 1))

        if resolved_start is None:
            resolved_end = min(total_lines, max(1, resolved_end or total_lines))
            resolved_start = max(1, resolved_end - default_window)
        elif resolved_end is None:
            resolved_start = max(1, resolved_start)
            resolved_end = min(total_lines, resolved_start + default_window)
        else:
            resolved_start = max(1, resolved_start)
            resolved_end = min(total_lines, max(resolved_start, resolved_end))

        selected_lines = all_lines[resolved_start - 1:resolved_end]
        numbered_lines = [
            f"{line_no}: {line}"
            for line_no, line in enumerate(selected_lines, start=resolved_start)
        ]
        rendered = "\n".join(numbered_lines)
        return {
            "content": rendered,
            "truncated": resolved_start > 1 or resolved_end < total_lines,
            "has_more": resolved_end < total_lines,
            "previous_line_start": max(1, resolved_start - max(default_window, 1)) if resolved_start > 1 else None,
            "next_line_start": resolved_end + 1 if resolved_end < total_lines else None,
            "total_chars": total_chars,
            "returned_chars": len(rendered),
            "mode": "line_range",
            "line_start": resolved_start,
            "line_end": resolved_end,
            "returned_line_count": len(selected_lines),
            "total_lines": total_lines,
        }

    @classmethod
    def _build_repo_match_context(
        cls,
        *,
        repo_dir: Path,
        repo_relative_path: str,
        line_number: int,
        context_lines: int,
        max_chars: int = 800,
    ) -> Dict[str, Any]:
        if context_lines <= 0:
            return {}
        file_path = repo_dir / repo_relative_path
        if not file_path.is_file():
            return {}
        preview = cls._read_text_payload(
            file_path,
            mode="line_range",
            max_chars=max_chars,
            chunk_index=None,
            chunk_chars=None,
            page=None,
            page_size_lines=None,
            line_start=max(1, int(line_number or 1) - context_lines),
            line_end=max(1, int(line_number or 1) + context_lines),
        )
        return {
            "context_start_line": preview.get("line_start"),
            "context_end_line": preview.get("line_end"),
            "context_text": preview.get("content"),
            "context_truncated": preview.get("truncated"),
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
    def _grounding_section_defaults(cls) -> Dict[str, Dict[str, Any]]:
        return {
            "repo": {
                "status": "unknown",
                "url": None,
                "resolved_ref": None,
                "default_branch": None,
                "commit_sha": None,
                "blockers": [],
                "blocker_details": [],
            },
            "entrypoint": {
                "status": "unknown",
                "candidates": [],
                "selected_candidate": None,
                "evidence_files": [],
                "blockers": [],
                "blocker_details": [],
            },
            "dataset": {
                "status": "unknown",
                "sources": [],
                "access_mode": None,
                "local_presence": {},
                "blockers": [],
                "blocker_details": [],
                "alternative_source_candidates": [],
            },
            "runtime": {
                "status": "unknown",
                "inspection_summary": None,
                "candidate_runtimes": [],
                "tool_availability": {},
                "blockers": [],
                "blocker_details": [],
            },
            "external_dependencies": {
                "status": "unknown",
                "urls": [],
                "probe_results": [],
                "blockers": [],
                "blocker_details": [],
                "alternative_source_candidates": [],
            },
        }

    @classmethod
    def _grounding_completion_summary(cls, report: Dict[str, Any]) -> Dict[str, Any]:
        summary = dict(report.get("summary") or {})
        repo = dict(report.get("repo") or {})
        entrypoint = dict(report.get("entrypoint") or {})
        dataset = dict(report.get("dataset") or {})
        runtime = dict(report.get("runtime") or {})
        external_dependencies = dict(report.get("external_dependencies") or {})
        repo_grounded = bool(summary.get("repo_grounded")) or str(repo.get("status") or "") == "grounded"
        entrypoint_grounded = bool(summary.get("entrypoint_grounded")) or str(entrypoint.get("status") or "") == "grounded"
        dataset_grounded = bool(summary.get("dataset_grounded")) or str(dataset.get("status") or "") == "grounded"
        runtime_grounded = bool(summary.get("runtime_grounded")) or str(runtime.get("status") or "") == "grounded"
        external_dependencies_grounded = bool(summary.get("external_dependencies_grounded")) or str(external_dependencies.get("status") or "") == "grounded"
        section_blockers: List[str] = []
        for section in (repo, entrypoint, dataset, runtime, external_dependencies):
            for blocker in list(section.get("blockers") or []):
                text = str(blocker or "").strip()
                if text:
                    section_blockers.append(text)
            for detail in list(section.get("blocker_details") or []):
                if not isinstance(detail, dict):
                    continue
                text = (
                    str(detail.get("reason") or "").strip()
                    or str(detail.get("message") or "").strip()
                    or str(detail.get("summary") or "").strip()
                )
                if text:
                    section_blockers.append(text)
        for blocker in list(summary.get("blockers") or []):
            text = str(blocker or "").strip()
            if text:
                section_blockers.append(text)
        explicit_next_actions = [str(item).strip() for item in list(summary.get("next_actions") or []) if str(item).strip()]
        explicit_status = str(summary.get("overall_status") or "").strip().lower()
        if explicit_status not in cls._GROUNDING_STATUSES:
            explicit_status = ""
        explicit_run_decision = str(summary.get("run_decision") or "").strip().lower()
        if explicit_run_decision not in {"ready", "runnable_with_patch", "blocked"}:
            explicit_run_decision = ""
        any_blocked = any(
            str(section.get("status") or "").strip().lower() == "blocked"
            for section in (repo, entrypoint, dataset, runtime, external_dependencies)
        ) or bool(section_blockers)
        all_grounded = all(
            (
                repo_grounded,
                entrypoint_grounded,
                dataset_grounded,
                runtime_grounded,
                external_dependencies_grounded,
            )
        )
        if explicit_status:
            overall_status = explicit_status
        elif all_grounded:
            overall_status = "grounded"
        elif any_blocked:
            overall_status = "blocked"
        elif any(
            str(section.get("status") or "").strip().lower() == "absent"
            for section in (repo, entrypoint, dataset, runtime, external_dependencies)
        ):
            overall_status = "absent"
        else:
            overall_status = "unknown"
        if explicit_run_decision:
            run_decision = explicit_run_decision
        elif repo_grounded and entrypoint_grounded and runtime_grounded and not any_blocked:
            run_decision = "ready"
        elif overall_status == "blocked":
            run_decision = "blocked"
        else:
            run_decision = "unknown"
        return {
            "repo_grounded": repo_grounded,
            "entrypoint_grounded": entrypoint_grounded,
            "dataset_grounded": dataset_grounded,
            "runtime_grounded": runtime_grounded,
            "external_dependencies_grounded": external_dependencies_grounded,
            "overall_status": overall_status,
            "run_decision": run_decision,
            "blockers": list(dict.fromkeys(section_blockers)),
            "next_actions": explicit_next_actions,
            "complete": run_decision in {"ready", "runnable_with_patch", "blocked"},
            "ready_for_next_stage": run_decision in {"ready", "runnable_with_patch"},
        }

    @classmethod
    def _normalize_grounding_report_payload(cls, payload: Dict[str, Any], *, workspace_dir: Path) -> Dict[str, Any]:
        normalized = dict(payload or {})
        repo_files = cls._repo_file_set(workspace_dir)
        defaults = cls._grounding_section_defaults()
        for section_name, section_defaults in defaults.items():
            merged_section = dict(section_defaults)
            merged_section.update(dict(normalized.get(section_name) or {}))
            status = str(merged_section.get("status") or "unknown").strip().lower() or "unknown"
            if status not in cls._GROUNDING_STATUSES:
                status = "unknown"
            merged_section["status"] = status
            for list_field in (
                "blockers",
                "blocker_details",
                "alternative_source_candidates",
                "candidates",
                "evidence_files",
                "sources",
                "candidate_runtimes",
                "urls",
                "probe_results",
            ):
                if list_field in merged_section and not isinstance(merged_section.get(list_field), list):
                    merged_section[list_field] = []
            normalized[section_name] = merged_section

        summary = dict(normalized.get("summary") or {})
        if not isinstance(summary.get("blockers") or [], list):
            summary["blockers"] = []
        if not isinstance(summary.get("next_actions") or [], list):
            summary["next_actions"] = []
        normalized["summary"] = summary

        entrypoint = dict(normalized.get("entrypoint") or {})
        selected_candidate = entrypoint.get("selected_candidate")
        if selected_candidate is None and list(entrypoint.get("candidates") or []):
            first_candidate = next(
                (
                    item
                    for item in list(entrypoint.get("candidates") or [])
                    if isinstance(item, dict) and cls._normalize_relative_path(item.get("path") or item.get("repo_relative_path") or item.get("path_or_hint") or "")
                ),
                None,
            )
            if first_candidate is not None:
                entrypoint["selected_candidate"] = first_candidate
        entrypoint_evidence: List[str] = []
        for item in list(entrypoint.get("evidence_files") or []):
            normalized_path = cls._normalize_relative_path(item)
            if normalized_path:
                entrypoint_evidence.append(normalized_path)
        for candidate in list(entrypoint.get("candidates") or []):
            if not isinstance(candidate, dict):
                continue
            candidate_path = cls._normalize_relative_path(
                candidate.get("path")
                or candidate.get("repo_relative_path")
                or candidate.get("path_or_hint")
                or ""
            )
            if candidate_path and not candidate_path.startswith("repo/source/") and candidate_path in repo_files:
                candidate_path = f"repo/source/{candidate_path}"
            if candidate_path:
                entrypoint_evidence.append(candidate_path)
        entrypoint["evidence_files"] = list(dict.fromkeys(entrypoint_evidence))
        normalized["entrypoint"] = entrypoint

        dataset = dict(normalized.get("dataset") or {})
        if not list(dataset.get("sources") or []):
            for alias in ("datasets", "items"):
                if isinstance(dataset.get(alias), list):
                    dataset["sources"] = list(dataset.get(alias) or [])
                    break
        normalized_sources: List[Any] = []
        dataset_probe_results: List[Dict[str, Any]] = []
        for item in list(dataset.get("sources") or []):
            if not isinstance(item, dict):
                normalized_sources.append(item)
                continue
            source = dict(item)
            source_url = str(
                source.get("url")
                or source.get("source_url")
                or source.get("download_url")
                or source.get("href")
                or source.get("link")
                or ""
            ).strip()
            probe_result = source.get("probe_result")
            if isinstance(probe_result, dict):
                probe_payload = dict(probe_result)
                if source_url:
                    probe_payload.setdefault("url", source_url)
                inferred_ok = cls._infer_probe_result_ok(probe_payload)
                if inferred_ok is not None and "ok" not in probe_payload:
                    probe_payload["ok"] = inferred_ok
                source["probe_result"] = probe_payload
                dataset_probe_results.append(probe_payload)
                if (
                    inferred_ok is True
                    and str(source.get("status") or "").strip().lower() in {"", "unknown", "partial", "blocked"}
                    and not list(source.get("blockers") or [])
                ):
                    source["status"] = "grounded"
            normalized_sources.append(source)
        dataset["sources"] = normalized_sources
        local_presence = dataset.get("local_presence")
        if isinstance(local_presence, bool):
            dataset["local_presence"] = {"available": bool(local_presence)}
        elif not isinstance(local_presence, dict):
            dataset["local_presence"] = {}
        dataset["blocker_details"] = cls._normalize_grounding_detail_items(dataset.get("blocker_details"))
        dataset["alternative_source_candidates"] = cls._normalize_grounding_candidate_items(
            dataset.get("alternative_source_candidates")
        )
        normalized["dataset"] = dataset

        runtime = dict(normalized.get("runtime") or {})
        tool_availability = runtime.get("tool_availability")
        if not isinstance(tool_availability, dict):
            runtime["tool_availability"] = {}
        runtime["blocker_details"] = cls._normalize_grounding_detail_items(runtime.get("blocker_details"))
        normalized["runtime"] = runtime

        external_dependencies = dict(normalized.get("external_dependencies") or {})
        raw_urls = list(external_dependencies.get("urls") or [])
        existing_probe_results = [*list(external_dependencies.get("probe_results") or []), *dataset_probe_results]
        normalized_urls: List[str] = []
        flattened_probe_results: List[Dict[str, Any]] = []
        for item in list(dataset.get("sources") or []):
            if not isinstance(item, dict):
                continue
            dataset_url = str(
                item.get("url")
                or item.get("source_url")
                or item.get("download_url")
                or item.get("href")
                or item.get("link")
                or ""
            ).strip()
            if dataset_url:
                normalized_urls.append(dataset_url)
        for item in raw_urls:
            if isinstance(item, str):
                url = str(item or "").strip()
                if url:
                    normalized_urls.append(url)
                continue
            if not isinstance(item, dict):
                continue
            url = str(
                item.get("url")
                or item.get("source_url")
                or item.get("download_url")
                or item.get("href")
                or item.get("link")
                or ""
            ).strip()
            if url:
                normalized_urls.append(url)
            nested_probe_results = list(item.get("probe_results") or [])
            singular_probe_result = item.get("probe_result")
            if isinstance(singular_probe_result, dict):
                nested_probe_results.append(singular_probe_result)
            for probe in nested_probe_results:
                if not isinstance(probe, dict):
                    continue
                probe_payload = dict(probe)
                probe_payload.setdefault("url", url)
                flattened_probe_results.append(probe_payload)
        merged_probe_results: List[Dict[str, Any]] = []
        for item in [*existing_probe_results, *flattened_probe_results]:
            if isinstance(item, dict):
                probe_payload = dict(item)
                inferred_ok = cls._infer_probe_result_ok(probe_payload)
                if inferred_ok is not None and "ok" not in probe_payload:
                    probe_payload["ok"] = inferred_ok
                merged_probe_results.append(probe_payload)
        external_dependencies["urls"] = list(dict.fromkeys(url for url in normalized_urls if url))
        external_dependencies["probe_results"] = merged_probe_results
        external_dependencies["blocker_details"] = cls._normalize_grounding_detail_items(
            external_dependencies.get("blocker_details")
        )
        external_dependencies["alternative_source_candidates"] = cls._normalize_grounding_candidate_items(
            external_dependencies.get("alternative_source_candidates")
        )
        normalized["external_dependencies"] = external_dependencies

        repo = dict(normalized.get("repo") or {})
        repo["blocker_details"] = cls._normalize_grounding_detail_items(repo.get("blocker_details"))
        repo_verification = str(repo.get("verification_status") or "").strip().lower()
        if (
            str(repo.get("status") or "").strip().lower() == "unknown"
            and not list(repo.get("blockers") or [])
            and not list(repo.get("blocker_details") or [])
        ):
            if repo_verification in {"verified", "cloneable", "ready"} or (
                str(repo.get("url") or "").strip()
                and (
                    str(repo.get("resolved_ref") or "").strip()
                    or str(repo.get("default_branch") or "").strip()
                    or str(repo.get("commit_sha") or "").strip()
                )
            ):
                repo["status"] = "grounded"
        normalized["repo"] = repo

        entrypoint = dict(normalized.get("entrypoint") or {})
        entrypoint_verification = str(entrypoint.get("verification_status") or "").strip().lower()
        if (
            str(entrypoint.get("status") or "").strip().lower() == "unknown"
            and not list(entrypoint.get("blockers") or [])
            and not list(entrypoint.get("blocker_details") or [])
        ):
            has_selected_candidate = isinstance(entrypoint.get("selected_candidate"), dict)
            has_evidence = bool(list(entrypoint.get("evidence_files") or []))
            if entrypoint_verification == "verified" or (has_selected_candidate and has_evidence):
                entrypoint["status"] = "grounded"
        normalized["entrypoint"] = entrypoint

        summary = dict(normalized.get("summary") or {})
        dataset_source_urls = cls._extract_grounding_urls(dataset.get("sources"))
        external_urls = cls._extract_grounding_urls(external_dependencies.get("urls"))
        successful_probe_urls, failed_probe_urls = cls._probe_result_url_sets(external_dependencies.get("probe_results"))
        failed_probe_map = cls._failed_probe_result_map(external_dependencies.get("probe_results"))

        dataset_status = str(dataset.get("status") or "").strip().lower()
        if dataset_status != "blocked":
            if any(url in failed_probe_urls for url in dataset_source_urls):
                dataset["status"] = "blocked"
            elif bool(dict(dataset.get("local_presence") or {}).get("available")):
                dataset["status"] = "grounded"
            elif dataset_source_urls and all(url in successful_probe_urls for url in dataset_source_urls):
                dataset["status"] = "grounded"
        external_status = str(external_dependencies.get("status") or "").strip().lower()
        if external_status != "blocked":
            if any(url in failed_probe_urls for url in external_urls):
                external_dependencies["status"] = "blocked"
            elif external_urls and all(url in successful_probe_urls for url in external_urls):
                external_dependencies["status"] = "grounded"

        cls._merge_probe_failures_into_grounding_section(
            dataset,
            urls=dataset_source_urls,
            failed_probe_map=failed_probe_map,
            label_resolver=cls._dataset_source_label_map(dataset.get("sources")),
            code="official_dataset_source_blocked",
            blocker_prefix="Official dataset source blocked",
        )
        cls._merge_probe_failures_into_grounding_section(
            external_dependencies,
            urls=external_urls,
            failed_probe_map=failed_probe_map,
            label_resolver={},
            code="official_external_dependency_blocked",
            blocker_prefix="Official external dependency blocked",
        )
        normalized["dataset"] = dataset
        normalized["external_dependencies"] = external_dependencies

        completion = cls._grounding_completion_summary(normalized)
        section_blockers = list(completion.get("blockers") or [])
        if not list(summary.get("blockers") or []):
            summary["blockers"] = section_blockers
        else:
            summary["blockers"] = list(
                dict.fromkeys(str(item).strip() for item in list(summary.get("blockers") or []) if str(item).strip())
            )
        for field in (
            "repo_grounded",
            "entrypoint_grounded",
            "dataset_grounded",
            "runtime_grounded",
            "external_dependencies_grounded",
            "overall_status",
        ):
            summary[field] = completion[field]
        summary["run_decision"] = completion["run_decision"]
        existing_next_actions = [
            str(item).strip()
            for item in list(summary.get("next_actions") or [])
            if str(item).strip()
        ]
        if existing_next_actions:
            summary["next_actions"] = existing_next_actions
        else:
            next_actions: List[str] = []
            blocked_source_search_needed = (
                (
                    str(dataset.get("status") or "").strip().lower() == "blocked"
                    and not list(dataset.get("alternative_source_candidates") or [])
                )
                or (
                    str(external_dependencies.get("status") or "").strip().lower() == "blocked"
                    and not list(external_dependencies.get("alternative_source_candidates") or [])
                )
            )
            if blocked_source_search_needed:
                next_actions.append(
                    "对 blocked 官方链接做一次 focused web_search/web_scrape，记录 `alternative_source_candidates`；不要覆盖 official blocker。"
                )
            if str(runtime.get("status") or "").strip().lower() == "unknown":
                next_actions.append("调用 `paper_research_inspect_runtime`，把 runtime 收敛成 grounded/absent/blocked。")
            if str(repo.get("status") or "").strip().lower() == "unknown":
                next_actions.append("继续 probe/clone repo，补齐 repo commit/ref 与 blocker 归因。")
            if str(entrypoint.get("status") or "").strip().lower() == "unknown":
                next_actions.append("继续 read/search repo，确认 entrypoint candidate、selected_candidate 与 evidence_files。")
            if completion["run_decision"] == "unknown":
                next_actions.append("先判断 repo 主路径能否运行；必要时调用 `paper_research_assess_repo_mainpath`，再把结论写入 `summary.run_decision`。")
            summary["next_actions"] = list(dict.fromkeys(next_actions))
        normalized["summary"] = summary
        return normalized

    @staticmethod
    def _extract_grounding_urls(items: Any) -> List[str]:
        urls: List[str] = []
        for item in list(items or []):
            candidate = ""
            if isinstance(item, str):
                candidate = item
            elif isinstance(item, dict):
                for key in ("url", "source_url", "download_url", "href", "link"):
                    raw_value = str(item.get(key) or "").strip()
                    if raw_value:
                        candidate = raw_value
                        break
            normalized = str(candidate or "").strip()
            if normalized.startswith(("http://", "https://")):
                urls.append(normalized)
        return list(dict.fromkeys(urls))

    @classmethod
    def _probe_result_url_sets(cls, probe_results: Any) -> tuple[set[str], set[str]]:
        successful: set[str] = set()
        failed: set[str] = set()
        for item in list(probe_results or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("final_url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            inferred_ok = cls._infer_probe_result_ok(item)
            if inferred_ok is True:
                successful.add(url)
            elif inferred_ok is False:
                failed.add(url)
        return successful, failed

    @staticmethod
    def _infer_probe_result_ok(item: Dict[str, Any]) -> Optional[bool]:
        if "ok" in item:
            raw_ok = item.get("ok")
            if isinstance(raw_ok, bool):
                return raw_ok
            return None
        raw_valid = item.get("valid")
        if isinstance(raw_valid, bool):
            return raw_valid
        diagnosis = str(item.get("diagnosis") or "").strip().lower()
        if diagnosis.startswith("valid_") or diagnosis in {"ready", "ok", "downloadable", "reference_page_ok", "followed_link_ok"}:
            return True
        if diagnosis in {
            "auth_required",
            "forbidden",
            "not_found",
            "accepted_but_empty",
            "redirect_broken",
            "checksum_mismatch",
            "gdrive_confirm_required",
            "html_page",
            "html_landing_page_for_file",
            "license_gate",
            "manual_download_required",
            "follow_link_failed",
            "follow_link_cycle",
            "follow_depth_exceeded",
            "repo_unreachable",
            "repo_page_reachable_but_not_cloneable",
        }:
            return False
        detected_kind = str(item.get("detected_kind") or item.get("kind") or "").strip().lower()
        if detected_kind == "html":
            return False
        content_type = str(item.get("content_type") or "").strip().lower()
        if "text/html" in content_type:
            return False
        raw_status = item.get("status_code", item.get("status"))
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = None
        if status_code is not None:
            if status_code in {200, 206}:
                return True
            if status_code >= 400:
                return False
        downloadable = item.get("downloadable")
        if isinstance(downloadable, bool):
            return downloadable
        return None

    @staticmethod
    def _normalize_grounding_detail_items(items: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for item in list(items or []):
            if isinstance(item, str):
                text = str(item or "").strip()
                if text:
                    normalized.append({"reason": text})
                continue
            if not isinstance(item, dict):
                continue
            payload = {str(key): value for key, value in dict(item).items() if str(key).strip()}
            if payload:
                normalized.append(payload)
        return normalized

    @staticmethod
    def _normalize_grounding_candidate_items(items: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for item in list(items or []):
            if isinstance(item, str):
                text = str(item or "").strip()
                if text:
                    normalized.append({"label": text})
                continue
            if not isinstance(item, dict):
                continue
            payload = {str(key): value for key, value in dict(item).items() if str(key).strip()}
            if payload:
                normalized.append(payload)
        return normalized

    @classmethod
    def _failed_probe_result_map(cls, probe_results: Any) -> Dict[str, Dict[str, Any]]:
        failed: Dict[str, Dict[str, Any]] = {}
        for item in list(probe_results or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("final_url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if cls._infer_probe_result_ok(item) is False and url not in failed:
                failed[url] = dict(item)
        return failed

    @staticmethod
    def _dataset_source_label_map(items: Any) -> Dict[str, str]:
        labels: Dict[str, str] = {}
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            url = str(
                item.get("url")
                or item.get("source_url")
                or item.get("download_url")
                or item.get("href")
                or item.get("link")
                or ""
            ).strip()
            label = str(item.get("name") or item.get("label") or item.get("title") or "").strip()
            if url and label:
                labels[url] = label
        return labels

    @classmethod
    def _merge_probe_failures_into_grounding_section(
        cls,
        section: Dict[str, Any],
        *,
        urls: Sequence[str],
        failed_probe_map: Dict[str, Dict[str, Any]],
        label_resolver: Dict[str, str],
        code: str,
        blocker_prefix: str,
    ) -> None:
        if str(section.get("status") or "").strip().lower() != "blocked":
            return
        blockers = [str(item).strip() for item in list(section.get("blockers") or []) if str(item).strip()]
        blocker_details = cls._normalize_grounding_detail_items(section.get("blocker_details"))
        existing_targets = {
            str(detail.get("target_url") or detail.get("url") or detail.get("target") or "").strip()
            for detail in blocker_details
            if isinstance(detail, dict)
        }
        for url in list(urls or []):
            failure = failed_probe_map.get(url)
            if not failure or url in existing_targets:
                continue
            diagnosis = str(failure.get("diagnosis") or "").strip().lower()
            raw_status = failure.get("status_code", failure.get("status"))
            try:
                status_code = int(raw_status)
            except (TypeError, ValueError):
                status_code = None
            label = str(label_resolver.get(url) or "").strip()
            target_text = label or url
            suffix_parts: List[str] = []
            if status_code is not None:
                suffix_parts.append(f"HTTP {status_code}")
            if diagnosis:
                suffix_parts.append(diagnosis)
            suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            reason = f"{blocker_prefix}: {target_text}{suffix}"
            blockers.append(reason)
            blocker_details.append(
                {
                    "code": code,
                    "target": label or None,
                    "target_url": url,
                    "reason": reason,
                    "diagnosis": diagnosis or None,
                    "status_code": status_code,
                }
            )
        section["blockers"] = list(dict.fromkeys(item for item in blockers if item))
        section["blocker_details"] = blocker_details

    @classmethod
    def _validate_grounding_report_payload(cls, payload: Dict[str, Any], *, workspace_dir: Path) -> List[str]:
        from app.services.project_runtime_service import ProjectRuntimeService

        errors: List[str] = []
        required_sections = ("repo", "entrypoint", "dataset", "runtime", "external_dependencies", "summary")
        for section in required_sections:
            if not isinstance(payload.get(section), dict):
                errors.append(f"`{section}` must be an object.")

        repo = dict(payload.get("repo") or {})
        entrypoint = dict(payload.get("entrypoint") or {})
        dataset = dict(payload.get("dataset") or {})
        runtime = dict(payload.get("runtime") or {})
        external_dependencies = dict(payload.get("external_dependencies") or {})
        summary = dict(payload.get("summary") or {})

        for section_name, section in (
            ("repo", repo),
            ("entrypoint", entrypoint),
            ("dataset", dataset),
            ("runtime", runtime),
            ("external_dependencies", external_dependencies),
        ):
            status = str(section.get("status") or "").strip().lower()
            if status not in cls._GROUNDING_STATUSES:
                errors.append(
                    f"`{section_name}.status` must be one of {sorted(cls._GROUNDING_STATUSES)}, got `{status}`."
                )
            blockers = section.get("blockers")
            if blockers is not None and not isinstance(blockers, list):
                errors.append(f"`{section_name}.blockers` must be a list when provided.")
            blocker_details = section.get("blocker_details")
            if blocker_details is not None and not isinstance(blocker_details, list):
                errors.append(f"`{section_name}.blocker_details` must be a list when provided.")
            elif isinstance(blocker_details, list):
                for index, item in enumerate(blocker_details):
                    if not isinstance(item, dict):
                        errors.append(f"`{section_name}.blocker_details[{index}]` must be an object.")

        repo_url = str(repo.get("url") or "").strip()
        if repo_url and not repo_url.startswith(("http://", "https://", "git@")):
            errors.append("`repo.url` must be http(s) or git@ when provided.")
        entrypoint_evidence = entrypoint.get("evidence_files")
        if entrypoint_evidence is not None and not isinstance(entrypoint_evidence, list):
            errors.append("`entrypoint.evidence_files` must be a list when provided.")
        else:
            for item in list(entrypoint_evidence or []):
                relative_path = cls._normalize_relative_path(item)
                if not relative_path:
                    errors.append(f"`entrypoint.evidence_files` contains invalid path `{item}`.")
                    continue
                resolved = ProjectRuntimeService.resolve_workspace_path(workspace_dir, relative_path, require_exists=False)
                if resolved is None:
                    errors.append(f"`entrypoint.evidence_files` contains out-of-scope path `{relative_path}`.")

        if dataset.get("sources") is not None and not isinstance(dataset.get("sources"), list):
            errors.append("`dataset.sources` must be a list when provided.")
        if not isinstance(dataset.get("local_presence") or {}, dict):
            errors.append("`dataset.local_presence` must be an object.")
        if dataset.get("alternative_source_candidates") is not None and not isinstance(dataset.get("alternative_source_candidates"), list):
            errors.append("`dataset.alternative_source_candidates` must be a list when provided.")
        elif isinstance(dataset.get("alternative_source_candidates"), list):
            for index, item in enumerate(list(dataset.get("alternative_source_candidates") or [])):
                if not isinstance(item, dict):
                    errors.append(f"`dataset.alternative_source_candidates[{index}]` must be an object.")
        if runtime.get("candidate_runtimes") is not None and not isinstance(runtime.get("candidate_runtimes"), list):
            errors.append("`runtime.candidate_runtimes` must be a list when provided.")
        if runtime.get("tool_availability") is not None and not isinstance(runtime.get("tool_availability"), dict):
            errors.append("`runtime.tool_availability` must be an object when provided.")
        if external_dependencies.get("urls") is not None and not isinstance(external_dependencies.get("urls"), list):
            errors.append("`external_dependencies.urls` must be a list when provided.")
        if external_dependencies.get("probe_results") is not None and not isinstance(external_dependencies.get("probe_results"), list):
            errors.append("`external_dependencies.probe_results` must be a list when provided.")
        else:
            for index, item in enumerate(list(external_dependencies.get("probe_results") or [])):
                if not isinstance(item, dict):
                    errors.append(f"`external_dependencies.probe_results[{index}]` must be an object.")
                    continue
                url = str(item.get("url") or item.get("final_url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    errors.append(
                        f"`external_dependencies.probe_results[{index}]` must include a valid `url` or `final_url`."
                    )
                inferred_ok = cls._infer_probe_result_ok(item)
                if "ok" in item and not isinstance(item.get("ok"), bool):
                    errors.append(f"`external_dependencies.probe_results[{index}].ok` must be a boolean.")
                elif inferred_ok is None:
                    errors.append(
                        f"`external_dependencies.probe_results[{index}]` must include a boolean `ok`, "
                        "or enough status/diagnosis fields to infer success."
                    )
        if external_dependencies.get("alternative_source_candidates") is not None and not isinstance(external_dependencies.get("alternative_source_candidates"), list):
            errors.append("`external_dependencies.alternative_source_candidates` must be a list when provided.")
        elif isinstance(external_dependencies.get("alternative_source_candidates"), list):
            for index, item in enumerate(list(external_dependencies.get("alternative_source_candidates") or [])):
                if not isinstance(item, dict):
                    errors.append(f"`external_dependencies.alternative_source_candidates[{index}]` must be an object.")

        dataset_status = str(dataset.get("status") or "").strip().lower()
        external_status = str(external_dependencies.get("status") or "").strip().lower()
        local_presence = dict(dataset.get("local_presence") or {})
        dataset_source_urls = cls._extract_grounding_urls(dataset.get("sources"))
        external_urls = cls._extract_grounding_urls(external_dependencies.get("urls"))
        successful_probe_urls, failed_probe_urls = cls._probe_result_url_sets(external_dependencies.get("probe_results"))

        if external_status == "grounded":
            if not external_urls:
                errors.append(
                    "`external_dependencies.status` is `grounded` but `external_dependencies.urls` is empty."
                )
            missing_external_probes = [url for url in external_urls if url not in successful_probe_urls]
            if missing_external_probes:
                preview = ", ".join(missing_external_probes[:4])
                errors.append(
                    "`external_dependencies.status` is `grounded` but these urls do not have successful "
                    f"`probe_results`: {preview}."
                )
            failed_external_urls = [url for url in external_urls if url in failed_probe_urls]
            if failed_external_urls:
                preview = ", ".join(failed_external_urls[:4])
                errors.append(
                    "`external_dependencies.status` is `grounded` but these urls have failed probe results: "
                    f"{preview}."
                )

        if dataset_status == "grounded" and dataset_source_urls and not bool(local_presence.get("available")):
            missing_dataset_probes = [url for url in dataset_source_urls if url not in successful_probe_urls]
            if missing_dataset_probes:
                preview = ", ".join(missing_dataset_probes[:4])
                errors.append(
                    "`dataset.status` is `grounded`, but these remote dataset sources do not have successful "
                    f"probe results and `dataset.local_presence.available` is not true: {preview}."
                )

        for field in (
            "repo_grounded",
            "entrypoint_grounded",
            "dataset_grounded",
            "runtime_grounded",
            "external_dependencies_grounded",
        ):
            if field in summary and not isinstance(summary.get(field), bool):
                errors.append(f"`summary.{field}` must be a boolean.")
        overall_status = str(summary.get("overall_status") or "").strip().lower()
        if overall_status and overall_status not in cls._GROUNDING_STATUSES:
            errors.append(
                f"`summary.overall_status` must be one of {sorted(cls._GROUNDING_STATUSES)}, got `{overall_status}`."
            )
        run_decision = str(summary.get("run_decision") or "").strip().lower()
        if run_decision and run_decision not in {"ready", "runnable_with_patch", "blocked"}:
            errors.append("`summary.run_decision` must be one of ready/runnable_with_patch/blocked when provided.")
        if run_decision in {"ready", "runnable_with_patch"}:
            missing_prereqs: List[str] = []
            if str(repo.get("status") or "").strip().lower() != "grounded":
                missing_prereqs.append("repo")
            if str(entrypoint.get("status") or "").strip().lower() != "grounded":
                missing_prereqs.append("entrypoint")
            if str(runtime.get("status") or "").strip().lower() != "grounded":
                missing_prereqs.append("runtime")
            if missing_prereqs:
                errors.append(
                    "`summary.run_decision` marks the main path as runnable, but these sections are not grounded: "
                    + ", ".join(missing_prereqs)
                    + "."
                )
        if summary.get("next_actions") is not None and not isinstance(summary.get("next_actions"), list):
            errors.append("`summary.next_actions` must be a list when provided.")
        if summary.get("blockers") is not None and not isinstance(summary.get("blockers"), list):
            errors.append("`summary.blockers` must be a list when provided.")
        return errors

    @staticmethod
    def _structured_validation_issue(
        *,
        path: str,
        code: str,
        message: str,
        evidence_needed: Optional[str] = None,
        suggested_tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "path": path,
            "code": code,
            "message": message,
        }
        if evidence_needed:
            payload["evidence_needed"] = evidence_needed
        if suggested_tool_calls:
            payload["suggested_tool_calls"] = suggested_tool_calls
        return payload

    @classmethod
    def _structured_grounding_validation_errors(
        cls,
        *,
        project_id: int,
        payload: Dict[str, Any],
        validation_errors: Sequence[str],
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        dataset = dict(payload.get("dataset") or {})
        external_dependencies = dict(payload.get("external_dependencies") or {})
        dataset_source_urls = cls._extract_grounding_urls(dataset.get("sources"))
        external_urls = cls._extract_grounding_urls(external_dependencies.get("urls"))

        for raw_error in validation_errors:
            message = str(raw_error or "").strip()
            if not message:
                continue
            if message.startswith("`dataset.status` is `grounded`, but these remote dataset sources do not have successful "):
                issues.append(
                    cls._structured_validation_issue(
                        path="dataset.sources",
                        code="missing_probe_or_local_evidence",
                        message=message,
                        evidence_needed="successful probe result for each required dataset URL, or dataset.local_presence.available=true",
                        suggested_tool_calls=[
                            {
                                "tool": "paper_research_probe_url",
                                "args": {
                                    "project_id": project_id,
                                    "url": url,
                                    "expected_kind": "file",
                                    "resolve_download_gate": True,
                                },
                            }
                            for url in dataset_source_urls[:4]
                        ]
                        or None,
                    )
                )
                continue
            if message.startswith("`external_dependencies.status` is `grounded` but these urls do not have successful "):
                issues.append(
                    cls._structured_validation_issue(
                        path="external_dependencies.urls",
                        code="missing_successful_probe",
                        message=message,
                        evidence_needed="successful probe result for each required external URL",
                        suggested_tool_calls=[
                            {
                                "tool": "paper_research_probe_url",
                                "args": {
                                    "project_id": project_id,
                                    "url": url,
                                    "resolve_download_gate": True,
                                },
                            }
                            for url in external_urls[:4]
                        ]
                        or None,
                    )
                )
                continue
            if message.startswith("`external_dependencies.status` is `grounded` but these urls have failed probe results:"):
                issues.append(
                    cls._structured_validation_issue(
                        path="external_dependencies.probe_results",
                        code="failed_probe_present",
                        message=message,
                        evidence_needed="replace failed probe results with successful ones, or mark the section blocked/partial instead of grounded",
                    )
                )
                continue
            if message.startswith("`summary.run_decision` marks the main path as runnable, but these sections are not grounded:"):
                issues.append(
                    cls._structured_validation_issue(
                        path="summary.run_decision",
                        code="run_decision_conflicts_with_sections",
                        message=message,
                        evidence_needed="align run_decision with grounded repo/entrypoint/runtime evidence",
                    )
                )
                continue
            field_match = re.match(r"^`([^`]+)` (must .+)$", message)
            if field_match:
                issues.append(
                    cls._structured_validation_issue(
                        path=field_match.group(1),
                        code="schema_constraint_failed",
                        message=message,
                    )
                )
                continue
            issues.append(
                cls._structured_validation_issue(
                    path="unknown",
                    code="validation_error",
                    message=message,
                )
            )
        return issues

    @classmethod
    def _implementation_grounding_conflicts(
        cls,
        *,
        payload: Dict[str, Any],
        grounding_report: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not isinstance(grounding_report, dict) or not grounding_report:
            return []
        conflicts: List[Dict[str, Any]] = []
        summary = dict(grounding_report.get("summary") or {})
        entrypoint = dict(grounding_report.get("entrypoint") or {})
        readiness = dict(payload.get("readiness") or {})
        baseline = dict(payload.get("baseline") or {})

        run_decision = str(summary.get("run_decision") or "").strip().lower()
        if bool(readiness.get("can_execute")) and run_decision not in {"ready", "runnable_with_patch"}:
            conflicts.append(
                {
                    "path": "readiness.can_execute",
                    "code": "conflicts_with_grounding_run_decision",
                    "message": (
                        f"`readiness.can_execute=true` but `grounding_report.summary.run_decision={run_decision or 'unknown'}`."
                    ),
                }
            )
        external_grounded = summary.get("external_dependencies_grounded")
        if isinstance(readiness.get("external_dependencies_grounded"), bool) and isinstance(external_grounded, bool):
            if readiness.get("external_dependencies_grounded") != external_grounded:
                conflicts.append(
                    {
                        "path": "readiness.external_dependencies_grounded",
                        "code": "conflicts_with_grounding_summary",
                        "message": (
                            "`readiness.external_dependencies_grounded` does not match "
                            "`grounding_report.summary.external_dependencies_grounded`."
                        ),
                    }
                )
        selected_candidate = dict(entrypoint.get("selected_candidate") or {})
        grounded_path = str(selected_candidate.get("path") or "").strip()
        entrypoint_path = cls._normalize_repo_relative_path(baseline.get("entrypoint_path_or_hint"))
        if grounded_path and entrypoint_path and grounded_path != entrypoint_path:
            conflicts.append(
                {
                    "path": "baseline.entrypoint_path_or_hint",
                    "code": "conflicts_with_grounding_entrypoint",
                    "message": (
                        f"`baseline.entrypoint_path_or_hint={entrypoint_path}` does not match "
                        f"`grounding_report.entrypoint.selected_candidate.path={grounded_path}`."
                    ),
                }
            )
        return conflicts

    @classmethod
    def _group_run_draft_validation_errors(
        cls,
        *,
        payload: Dict[str, Any],
        validation_errors: Sequence[str],
    ) -> Dict[str, Any]:
        drafts = [dict(item) for item in list(payload.get("drafts") or []) if isinstance(item, dict)]
        grouped: Dict[int, Dict[str, Any]] = {}
        global_errors: List[Dict[str, Any]] = []

        for raw_error in validation_errors:
            message = str(raw_error or "").strip()
            if not message:
                continue
            match = re.match(r"^drafts\[(\d+)\]\.([^\s]+)\s+(.*)$", message)
            if match:
                draft_index = int(match.group(1))
                field_path = match.group(2)
                suffix = match.group(3).strip()
                draft = drafts[draft_index] if 0 <= draft_index < len(drafts) else {}
                bucket = grouped.setdefault(
                    draft_index,
                    {
                        "draft_index": draft_index,
                        "draft_id": str(draft.get("id") or "").strip() or None,
                        "title": str(draft.get("title") or "").strip() or None,
                        "errors": [],
                    },
                )
                bucket["errors"].append(
                    {
                        "path": field_path,
                        "code": "draft_field_invalid",
                        "message": f"drafts[{draft_index}].{field_path} {suffix}",
                    }
                )
                continue
            if message.startswith("`drafts` must be a non-empty list."):
                global_errors.append(
                    {
                        "path": "drafts",
                        "code": "missing_drafts",
                        "message": message,
                    }
                )
                continue
            global_errors.append(
                {
                    "path": "unknown",
                    "code": "validation_error",
                    "message": message,
                }
            )
        return {
            "draft_errors": [grouped[index] for index in sorted(grouped)],
            "global_errors": global_errors,
        }

    @classmethod
    def _read_grounding_report_payload(cls, workspace_dir: Path) -> Dict[str, Any]:
        return cls._read_json_file(workspace_dir / "specs" / "grounding_report.json")

    @classmethod
    def _grounding_gate_state(cls, workspace_dir: Path) -> Dict[str, Any]:
        report = cls._read_grounding_report_payload(workspace_dir)
        if not report:
            return {
                "ready": False,
                "status": "missing",
                "relative_path": "specs/grounding_report.json",
                "blockers": [],
                "next_actions": ["先完成 grounding，并写入 `specs/grounding_report.json`。"],
                "report": {},
            }
        completion = cls._grounding_completion_summary(report)
        return {
            "ready": bool(completion.get("ready_for_next_stage")),
            "complete": bool(completion.get("complete")),
            "status": str(completion.get("overall_status") or "unknown"),
            "run_decision": str(completion.get("run_decision") or "unknown"),
            "relative_path": "specs/grounding_report.json",
            "blockers": list(completion.get("blockers") or []),
            "next_actions": list(completion.get("next_actions") or []),
            "report": report,
        }

    @classmethod
    def _grounding_gate_result(
        cls,
        *,
        project_payload: Dict[str, Any],
        workspace: Any,
        stage_name: str,
        action_label: str,
        gate_state: Dict[str, Any],
    ) -> ToolResult:
        blockers = [str(item).strip() for item in list(gate_state.get("blockers") or []) if str(item).strip()]
        next_actions = [str(item).strip() for item in list(gate_state.get("next_actions") or []) if str(item).strip()]
        status = str(gate_state.get("status") or "missing")
        lines = [
            f"{action_label} 前，必须先完成 grounding 阶段。",
            f"- Project: /projects/{int(project_payload.get('id') or 0)}",
            f"- Grounding report: {gate_state.get('relative_path')}",
            f"- Grounding status: {status}",
            f"- Run decision: {gate_state.get('run_decision') or 'unknown'}",
        ]
        if blockers:
            lines.append("- Grounding blockers:")
            lines.extend(f"  - {item}" for item in blockers[:8])
        if next_actions:
            lines.append("- Recommended next actions:")
            lines.extend(f"  - {item}" for item in next_actions[:6])
        else:
            if status == "blocked":
                lines.append("- Recommended next action: 对 blocked 官方链接做一次 focused web_search/web_scrape，记录 alternative_source_candidates；若仍无可信候选，再报告 blocker。")
            else:
                lines.append("- Recommended next action: 先 probe repo/url、inspect runtime，并写入 grounding_report。")
        return ToolResult(
            success=False,
            output="\n".join(lines),
            error="grounding_blocked" if status == "blocked" else "grounding_incomplete",
            data={
                **cls._root_descriptor(project_payload=project_payload, workspace=workspace),
                "project_id": int(project_payload.get("id") or 0),
                "current_stage": "grounding",
                "blocked_stage": stage_name,
                "grounding_report_relative_path": gate_state.get("relative_path"),
                "grounding_status": status,
                "grounding_complete": bool(gate_state.get("complete")),
                "grounding_ready": bool(gate_state.get("ready")),
                "run_decision": gate_state.get("run_decision"),
                "grounding_blockers": blockers,
                "next_actions": next_actions,
            },
        )

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
        worker_commands = dict(worker_environment.get("commands") or {})
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
        preferred_runtime_type = (
            str(
                next(
                    (
                        item.get("runtime_type")
                        for item in runtime_candidates
                        if str(item.get("status") or "").strip().lower() in {"ready", "available", "supported"}
                    ),
                    "",
                )
            ).strip()
            or str(runtime_candidates[0].get("runtime_type") or "").strip()
            if runtime_candidates
            else ""
        )
        installed_key_packages = [
            name
            for name, package_payload in worker_packages.items()
            if isinstance(package_payload, dict) and bool(package_payload.get("installed"))
        ][:12]
        available_commands = [
            name
            for name, command_payload in worker_commands.items()
            if isinstance(command_payload, dict) and bool(command_payload.get("available"))
        ][:12]
        normalized["runtime_snapshot"] = {
            "captured_from": "paper_research_inspect_runtime",
            "repo_root_relative_path": detected_repo_root,
            "runtime_worker_available": bool(dict(runtime_payload.get("runtime_worker") or {}).get("available")),
            "preferred_runtime_type": preferred_runtime_type,
            "candidate_summaries": [
                {
                    "runtime_type": str(item.get("runtime_type") or "").strip(),
                    "status": str(item.get("status") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                    "blockers": [str(blocker).strip() for blocker in list(item.get("blockers") or []) if str(blocker).strip()],
                    "evidence_files": [str(path).strip() for path in list(item.get("evidence_files") or [])[:6] if str(path).strip()],
                }
                for item in runtime_candidates[:6]
            ],
            "environment": {
                "python_version": str(dict(worker_environment.get("python") or {}).get("version") or "").strip(),
                "available_commands": available_commands,
                "installed_key_packages": installed_key_packages,
                "missing_required_packages": missing_runtime_packages,
            },
        }
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
    def _validate_implementation_spec_payload(cls, payload: Dict[str, Any], *, workspace_dir: Path) -> List[str]:
        from app.services.project_runtime_service import ProjectRuntimeService

        errors: List[str] = []
        required_object_fields = (
            "source_summary",
            "baseline",
            "repo_plan",
            "runtime_snapshot",
            "data_plan",
            "tuning_plan",
            "readiness",
        )
        for field in required_object_fields:
            if not isinstance(payload.get(field), dict):
                errors.append(f"`{field}` must be an object.")

        repo_root_relative_path = cls._normalize_relative_path(payload.get("repo_root_relative_path") or "")
        if not repo_root_relative_path:
            errors.append("`repo_root_relative_path` must be a workspace-relative path.")
        elif ProjectRuntimeService.resolve_workspace_path(workspace_dir, repo_root_relative_path, require_exists=False) is None:
            errors.append(f"`repo_root_relative_path` is outside workspace or invalid: {repo_root_relative_path}")

        for field in ("blockers", "next_actions", "evidence_log", "notes"):
            value = payload.get(field)
            if value is not None and not isinstance(value, list):
                errors.append(f"`{field}` must be a list when provided.")

        baseline = dict(payload.get("baseline") or {})
        readiness = dict(payload.get("readiness") or {})
        runtime_snapshot = dict(payload.get("runtime_snapshot") or {})
        repo_plan = dict(payload.get("repo_plan") or {})

        entrypoint_type = str(baseline.get("entrypoint_type") or "").strip().lower()
        entrypoint_aliases = {
            "repo": "repo_script",
            "repo_script": "repo_script",
            "python_script": "repo_script",
            "notebook": "notebook",
            "unknown": "unknown",
        }
        normalized_entrypoint_type = entrypoint_aliases.get(entrypoint_type, entrypoint_type)
        if normalized_entrypoint_type and normalized_entrypoint_type not in {"repo_script", "notebook", "unknown"}:
            errors.append("`baseline.entrypoint_type` must be one of repo_script/notebook/unknown.")

        readiness_bools = ("can_create_run_draft", "can_execute", "external_dependencies_grounded")
        for field in readiness_bools:
            if field in readiness and not isinstance(readiness.get(field), bool):
                errors.append(f"`readiness.{field}` must be a boolean.")

        entrypoint_path_or_hint = str(baseline.get("entrypoint_path_or_hint") or "").strip()
        if bool(readiness.get("can_create_run_draft")) or bool(readiness.get("can_execute")):
            if not normalized_entrypoint_type or normalized_entrypoint_type == "unknown":
                errors.append(
                    "`baseline.entrypoint_type` must be grounded before readiness.can_create_run_draft/can_execute is true."
                )
            if not entrypoint_path_or_hint:
                errors.append(
                    "`baseline.entrypoint_path_or_hint` is required before readiness.can_create_run_draft/can_execute is true."
                )

        if normalized_entrypoint_type in {"repo_script", "notebook"} and entrypoint_path_or_hint:
            repo_relative_path = cls._normalize_relative_path(entrypoint_path_or_hint)
            if repo_relative_path.startswith("repo/source/"):
                repo_relative_path = repo_relative_path.removeprefix("repo/source/")
            if not repo_relative_path:
                errors.append("`baseline.entrypoint_path_or_hint` must be a repo-relative path.")
            elif ProjectRuntimeService.resolve_workspace_path(workspace_dir, f"repo/source/{repo_relative_path}") is None:
                errors.append(
                    f"`baseline.entrypoint_path_or_hint` references missing repo file `{repo_relative_path}`."
                )

        repo_status = str(repo_plan.get("repo_status") or "").strip()
        if str(payload.get("mode") or "").strip() == "repo_driven" and not repo_status:
            errors.append("`repo_plan.repo_status` is required for repo_driven implementation specs.")
        for field in ("dependency_files", "entrypoint_candidates", "files_read"):
            if field in repo_plan and not isinstance(repo_plan.get(field), list):
                errors.append(f"`repo_plan.{field}` must be a list when provided.")

        captured_from = str(runtime_snapshot.get("captured_from") or "").strip()
        if captured_from and captured_from != "paper_research_inspect_runtime":
            errors.append("`runtime_snapshot.captured_from` must be `paper_research_inspect_runtime`.")
        if not isinstance(runtime_snapshot.get("candidate_summaries") or [], list):
            errors.append("`runtime_snapshot.candidate_summaries` must be a list.")
        if not isinstance(runtime_snapshot.get("environment") or {}, dict):
            errors.append("`runtime_snapshot.environment` must be an object.")

        return errors

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
        paper_summary = dict(summary.get("paper_summary") or {})
        return {
            "id": int(workspace.id),
            "status": str(workspace.status or ""),
            "title": str(workspace.title or ""),
            "notebook_id": workspace.notebook_id,
            "notebook_url": f"/code/{workspace.notebook_id}" if workspace.notebook_id else None,
            "execution_mode": spec.get("execution_mode") or summary.get("execution_mode"),
            "intake_status": dict(spec.get("intake_status") or {}),
            "intake_summary": _PaperResearchToolBase._intake_summary_from_workspace(workspace),
            "paper_summary": {
                "available": bool(paper_summary),
                "problem_definition": paper_summary.get("problem_definition"),
                "core_method": paper_summary.get("core_method"),
                "reproduction_risk_count": len(list(paper_summary.get("reproduction_risks") or [])),
                "verification_question_count": len(list(paper_summary.get("verification_questions") or [])),
                "teaching_outline_count": len(list(paper_summary.get("teaching_outline") or [])),
            },
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
        elif current_stage == "grounding":
            recommended_next_action = (
                "优先 probe repo/url、读取 repo/runtime 证据，并写入 grounding_report；"
                "在 grounding 完成前不要进入 implementation 或 execution。"
            )
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
            paper_summary = dict(workspace_payload.get("paper_summary") or {})
            if paper_summary.get("available"):
                lines.append(
                    "- Paper summary: available，可用于讲解/资料搜集/调优与验证设计"
                    + (
                        f"（复现风险 {int(paper_summary.get('reproduction_risk_count') or 0)} 条，"
                        f"验证问题 {int(paper_summary.get('verification_question_count') or 0)} 条）"
                    )
                )
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
            "relative_path": {
                "type": "string",
                "description": "manifest 中返回的 artifact 相对路径，例如 `planning/paper_summary.json` 或 `specs/grounding_report.json`。不要传绝对路径。",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "full", "chunk", "page", "line_range"],
                "default": "auto",
                "description": "读取模式。full 返回全文；chunk/page/line_range 返回原文分段，不做摘要。",
            },
            "max_chars": {
                "type": "integer",
                "default": 20000,
                "description": "兼容旧调用的字符窗口参数；chunk 模式下会作为默认 chunk_chars。",
            },
            "chunk_index": {"type": "integer", "description": "chunk 模式下的块序号（1-based）。"},
            "chunk_chars": {"type": "integer", "description": "chunk 模式下每块字符数。"},
            "page": {"type": "integer", "description": "page 模式下的页号（1-based，按行分页）。"},
            "page_size_lines": {"type": "integer", "description": "page 模式下每页多少行。"},
            "line_start": {"type": "integer", "description": "line_range 模式起始行号（1-based）。"},
            "line_end": {"type": "integer", "description": "line_range 模式结束行号（1-based）。"},
        },
        "required": ["project_id", "relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            relative_path = self._normalize_relative_path(kwargs.get("relative_path"))
            mode = str(kwargs.get("mode") or "auto")
            max_chars = int(kwargs.get("max_chars") or 20000)
            chunk_index = kwargs.get("chunk_index")
            chunk_chars = kwargs.get("chunk_chars")
            page = kwargs.get("page")
            page_size_lines = kwargs.get("page_size_lines")
            line_start = kwargs.get("line_start")
            line_end = kwargs.get("line_end")
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

            text_payload = self._read_text_payload(
                actual_path,
                mode=mode,
                max_chars=max_chars,
                chunk_index=chunk_index,
                chunk_chars=chunk_chars,
                page=page,
                page_size_lines=page_size_lines,
                line_start=line_start,
                line_end=line_end,
            )
            parsed_content: Any = None
            if spec["content_type"] == "json":
                try:
                    parsed_content = json.loads(actual_path.read_text(encoding="utf-8"))
                except Exception:
                    parsed_content = None
            lines = [
                f"已读取 artifact: {relative_path}",
                f"- Root alias: {self._PROJECT_ROOT_ALIAS}",
                f"- Content type: {spec['content_type']}",
                f"- Mode: {text_payload.get('mode')}",
                f"- Truncated: {text_payload['truncated']}",
                f"- Returned chars: {text_payload['returned_chars']}/{text_payload['total_chars']}",
                f"- Has more: {text_payload.get('has_more')}",
            ]
            if text_payload.get("chunk_index") is not None:
                lines.append(
                    f"- Chunk: {text_payload.get('chunk_index')}/{text_payload.get('total_chunks')} (next={text_payload.get('next_chunk_index')})"
                )
            if text_payload.get("page") is not None:
                lines.append(
                    f"- Page: {text_payload.get('page')}/{text_payload.get('total_pages')} (next={text_payload.get('next_page')})"
                )
            if text_payload.get("line_start") is not None:
                lines.append(
                    f"- Lines: {text_payload.get('line_start')}-{text_payload.get('line_end')} / {text_payload.get('total_lines')}"
                )
            lines.extend([
                "Content:",
                str(text_payload["content"]),
            ])
            data = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "relative_path": relative_path,
                "exists": True,
                "content_type": spec["content_type"],
                "content": parsed_content,
                **text_payload,
            }
            return ToolResult(success=True, output="\n".join(lines), data=data)

        return await self._with_db(_handler)


class PaperResearchSearchOutputsTool(_PaperResearchToolBase):
    name = "paper_research_search_outputs"
    input_model = PaperResearchSearchOutputsInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "在当前 Project/workspace 的归档产物中按关键词或正则搜索内容，适合搜索 `paper_intake_markdown.md`、planning/specs/drafts/executions 等输出。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "query": {"type": "string", "description": "搜索词或正则表达式。默认按固定字符串搜索；只有在 `is_regex=true` 时才按正则处理。"},
            "scope": {
                "type": "string",
                "enum": ["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"],
                "default": "all",
                "description": "只在指定 scope 的 workspace 产物里搜索；all 表示全部可管理输出。",
            },
            "max_results": {"type": "integer", "default": 20, "description": "最多返回多少条匹配。范围 1-100。"},
            "case_sensitive": {"type": "boolean", "default": False, "description": "是否大小写敏感。"},
            "is_regex": {"type": "boolean", "default": False, "description": "是否将 query 按正则表达式处理。"},
            "context_lines": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 20,
                "description": "返回每个命中点上下各多少行上下文。范围 0-20，适合先 search 再按行读局部。",
            },
        },
        "required": ["project_id", "query"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService

            project_id = int(kwargs["project_id"])
            query = str(kwargs.get("query") or "").strip()
            scope = str(kwargs.get("scope") or "all").strip().lower() or "all"
            max_results = int(kwargs.get("max_results") or 20)
            case_sensitive = bool(kwargs.get("case_sensitive", False))
            is_regex = bool(kwargs.get("is_regex", False))
            context_lines = int(kwargs.get("context_lines") or 0)

            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            service = ProjectService(db)
            outputs = await service.list_workspace_outputs(
                project_id=project_id,
                user_id=self.user_id,
                workspace_id=int(workspace.id),
            )
            if outputs is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            searchable_items: List[Dict[str, Any]] = []
            skipped_non_file_outputs: List[str] = []
            for item in list(outputs or []):
                relative_path = self._normalize_relative_path(item.get("relative_path"))
                if not relative_path:
                    continue
                if scope != "all" and str(item.get("scope") or "").strip() != scope:
                    continue
                if str(item.get("storage") or "file").strip() != "file":
                    skipped_non_file_outputs.append(relative_path)
                    continue
                target = workspace_dir / relative_path
                if not target.is_file():
                    continue
                searchable_items.append(
                    {
                        "relative_path": relative_path,
                        "scope": str(item.get("scope") or "").strip(),
                        "kind": str(item.get("kind") or "").strip(),
                        "path": target,
                    }
                )

            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(query, flags) if is_regex else None
            except re.error as exc:
                return ToolResult(
                    success=False,
                    output=f"搜索正则无效: {exc}",
                    error="invalid_search_regex",
                    data={"project_id": project_id, "query": query, "scope": scope},
                )
            fixed_query = query if case_sensitive else query.lower()
            matches: List[Dict[str, Any]] = []
            matched_files: Set[str] = set()
            truncated = False
            parse_errors = 0

            for item in searchable_items:
                try:
                    with Path(item["path"]).open("r", encoding="utf-8", errors="ignore") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            haystack = line if case_sensitive else line.lower()
                            matched = bool(regex.search(line)) if regex is not None else fixed_query in haystack
                            if not matched:
                                continue
                            relative_path = str(item["relative_path"])
                            matched_files.add(relative_path)
                            row: Dict[str, Any] = {
                                "relative_path": relative_path,
                                "line_number": line_number,
                                "line_text": self._normalize_line_preview(line),
                                "scope": item["scope"],
                                "kind": item["kind"],
                                "submatches": [],
                            }
                            if context_lines > 0:
                                preview = self._read_text_payload(
                                    Path(item["path"]),
                                    mode="line_range",
                                    max_chars=800,
                                    chunk_index=None,
                                    chunk_chars=None,
                                    page=None,
                                    page_size_lines=None,
                                    line_start=max(1, line_number - context_lines),
                                    line_end=max(1, line_number + context_lines),
                                )
                                row.update(
                                    {
                                        "context_start_line": preview.get("line_start"),
                                        "context_end_line": preview.get("line_end"),
                                        "context_text": preview.get("content"),
                                        "context_truncated": preview.get("truncated"),
                                    }
                                )
                            matches.append(row)
                            if len(matches) >= max_results:
                                truncated = True
                                break
                    if truncated:
                        break
                except Exception:
                    parse_errors += 1
                    continue

            result_lines: List[str] = []
            for item in matches:
                result_lines.append(
                    f"- {item.get('relative_path')}:{item.get('line_number')} | {item.get('line_text')}"
                )
                context_text = str(item.get("context_text") or "").strip()
                if context_text:
                    result_lines.append(
                        f"  context {item.get('context_start_line')}-{item.get('context_end_line')}:\n{context_text}"
                    )

            lines = [
                "已搜索 Project/workspace 归档产物。",
                f"- Project: /projects/{project_id}",
                f"- Scope: {scope}",
                f"- Query: {query}",
                f"- Regex: {is_regex}",
                f"- Case sensitive: {case_sensitive}",
                f"- Context lines: {context_lines}",
                f"- Searchable outputs: {len(searchable_items)}",
                f"- Matched files: {len(matched_files)}",
                f"- Returned matches: {len(matches)}/{max_results}",
                f"- Truncated: {truncated}",
                "- Matches:",
                *(result_lines or ["- none"]),
            ]
            if skipped_non_file_outputs:
                lines.append("- Skipped non-file outputs:")
                lines.extend(f"  - {item}" for item in skipped_non_file_outputs[:10])

            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "scope": scope,
                    "query": query,
                    "context_lines": context_lines,
                    "case_sensitive": case_sensitive,
                    "is_regex": is_regex,
                    "engine": "python_fallback",
                    "searchable_output_count": len(searchable_items),
                    "matched_file_count": len(matched_files),
                    "returned_matches": len(matches),
                    "truncated": truncated,
                    "parse_errors": parse_errors,
                    "skipped_non_file_outputs": skipped_non_file_outputs,
                    "matches": matches,
                },
            )

        return await self._with_db(_handler)


class PaperResearchReadRepoFileTool(_PaperResearchToolBase):
    name = "paper_research_read_repo_file"
    input_model = PaperResearchReadRepoFileInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "按 repo/source 下的相对路径读取单个仓库文件；路径不确定时先用 `paper_research_search_repo` 定位。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_relative_path": {
                "type": "string",
                "description": (
                    "repo/source 下的相对路径，例如 `README.md`、`train.py` 或 `configs/train.yaml`。"
                    "优先传 repo-relative 路径，不需要带 `repo/source/` 前缀。"
                    "如果不确定具体位于哪个子目录，先用 `paper_research_search_repo` 按文件名或关键字定位，"
                    "不要臆测 `scripts/` 等目录前缀。"
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "full", "chunk", "page", "line_range"],
                "default": "auto",
                "description": "读取模式。full 返回全文；chunk/page/line_range 返回原文分段，不做摘要。",
            },
            "max_chars": {"type": "integer", "default": 20000, "description": "兼容旧调用的字符窗口参数；chunk 模式下会作为默认 chunk_chars。"},
            "chunk_index": {"type": "integer", "description": "chunk 模式下的块序号（1-based）。"},
            "chunk_chars": {"type": "integer", "description": "chunk 模式下每块字符数。"},
            "page": {"type": "integer", "description": "page 模式下的页号（1-based，按行分页）。"},
            "page_size_lines": {"type": "integer", "description": "page 模式下每页多少行。"},
            "line_start": {"type": "integer", "description": "line_range 模式起始行号（1-based）。"},
            "line_end": {"type": "integer", "description": "line_range 模式结束行号（1-based）。"},
        },
        "required": ["project_id", "repo_relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            repo_relative_path = self._normalize_relative_path(kwargs.get("repo_relative_path"))
            mode = str(kwargs.get("mode") or "auto")
            max_chars = int(kwargs.get("max_chars") or 20000)
            chunk_index = kwargs.get("chunk_index")
            chunk_chars = kwargs.get("chunk_chars")
            page = kwargs.get("page")
            page_size_lines = kwargs.get("page_size_lines")
            line_start = kwargs.get("line_start")
            line_end = kwargs.get("line_end")
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
                    output=(
                        f"仓库文件不存在: `repo/source/{repo_relative_path}`。"
                        "这通常不是文件不可读，而是 repo_relative_path 猜错了。"
                        "如果不确定真实路径，请先调用 `paper_research_search_repo` 用文件名或关键字定位，"
                        "不要继续假设它在 `scripts/` 等目录下。"
                    ),
                    error="repo_file_not_found",
                    data={
                        "project_id": project_id,
                        "repo_relative_path": repo_relative_path,
                        "next_action": "paper_research_search_repo",
                        "path_hint_invalid": True,
                    },
                )

            text_payload = self._read_text_payload(
                target_path,
                mode=mode,
                max_chars=max_chars,
                chunk_index=chunk_index,
                chunk_chars=chunk_chars,
                page=page,
                page_size_lines=page_size_lines,
                line_start=line_start,
                line_end=line_end,
            )
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
                f"- Mode: {text_payload.get('mode')}",
                f"- Truncated: {text_payload['truncated']}",
                f"- Returned chars: {text_payload['returned_chars']}/{text_payload['total_chars']}",
            ]
            if text_payload.get("chunk_index") is not None:
                lines.append(
                    f"- Chunk: {text_payload.get('chunk_index')}/{text_payload.get('total_chunks')} (next={text_payload.get('next_chunk_index')})"
                )
            if text_payload.get("page") is not None:
                lines.append(
                    f"- Page: {text_payload.get('page')}/{text_payload.get('total_pages')} (next={text_payload.get('next_page')})"
                )
            if text_payload.get("line_start") is not None:
                lines.append(
                    f"- Returned lines: {text_payload.get('line_start')}-{text_payload.get('line_end')} / {text_payload.get('total_lines')}"
                )
            lines.extend([
                "Content:",
                str(text_payload["content"]),
            ])
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
            "query": {"type": "string", "description": "搜索词或正则表达式。默认按固定字符串搜索；只有在 `is_regex=true` 时才按正则处理。"},
            "max_results": {"type": "integer", "default": 20, "description": "最多返回多少条匹配。范围 1-100。"},
            "case_sensitive": {"type": "boolean", "default": False, "description": "是否大小写敏感。"},
            "is_regex": {"type": "boolean", "default": False, "description": "是否将 query 按正则表达式处理。"},
            "glob": {"type": "string", "description": "可选文件 glob，例如 `*.py`、`*.sh` 或 `**/*.ipynb`；只用于缩小文件范围。"},
            "context_lines": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 20,
                "description": "可选，返回每个命中点上下各多少行上下文。范围 0-20，适合先 search 再按行读局部。",
            },
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
            context_lines = int(kwargs.get("context_lines") or 0)
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
            if context_lines > 0:
                enriched_matches: List[Dict[str, Any]] = []
                for item in matches:
                    enriched_matches.append(
                        {
                            **item,
                            **self._build_repo_match_context(
                                repo_dir=repo_dir,
                                repo_relative_path=str(item.get("repo_relative_path") or ""),
                                line_number=int(item.get("line_number") or 1),
                                context_lines=context_lines,
                            ),
                        }
                    )
                matches = enriched_matches
            matched_files = list(search_payload.get("matched_files") or [])
            result_lines: List[str] = []
            for item in matches:
                result_lines.append(
                    f"- {item.get('relative_path')}:{item.get('line_number')} | {item.get('line_text')}"
                )
                context_text = str(item.get("context_text") or "").strip()
                if context_text:
                    result_lines.append(
                        f"  context {item.get('context_start_line')}-{item.get('context_end_line')}:\n{context_text}"
                    )
            lines = [
                "已搜索 repo/source。",
                f"- Project: /projects/{project_id}",
                f"- Engine: {search_payload.get('engine')}",
                f"- Query: {query}",
                f"- Regex: {is_regex}",
                f"- Case sensitive: {case_sensitive}",
                f"- Glob: {glob or 'none'}",
                f"- Context lines: {context_lines}",
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
                    "context_lines": context_lines,
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


class PaperResearchBuildZoektIndexTool(_PaperResearchToolBase):
    name = "paper_research_build_zoekt_index"
    input_model = PaperResearchBuildZoektIndexInput
    parallel_safe = True
    description = "为当前 Project 的 repo/source 构建或刷新 Zoekt 代码检索索引。适合在大仓库里做高质量代码检索前先建立索引。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "force_reindex": {"type": "boolean", "default": False, "description": "是否强制重建索引。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            force_reindex = bool(kwargs.get("force_reindex", False))
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error
            assert project_payload is not None and workspace is not None and repo_dir is not None

            workspace_dir = self._workspace_dir_for(workspace)
            payload = await ZoektCliService.build_index(
                repo_dir=repo_dir,
                workspace_dir=workspace_dir,
                force_reindex=force_reindex,
            )
            if not bool(payload.get("success")):
                error = str(payload.get("error") or "zoekt_index_failed")
                return ToolResult(
                    success=False,
                    output=(
                        "Zoekt 索引构建失败。\n"
                        f"- Project: /projects/{project_id}\n"
                        f"- Error: {error}\n"
                        f"- Search binary: {payload.get('search_binary') or 'missing'}\n"
                        f"- Git index binary: {payload.get('git_index_binary') or 'missing'}\n"
                        f"- Plain index binary: {payload.get('plain_index_binary') or 'missing'}\n"
                        f"- Stderr: {str(payload.get('stderr') or '').strip() or 'none'}"
                    ),
                    error=error,
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        **dict(payload or {}),
                    },
                )

            lines = [
                "Zoekt 索引已就绪。",
                f"- Project: /projects/{project_id}",
                f"- Status: {payload.get('status')}",
                f"- Engine: {payload.get('engine') or 'zoekt'}",
                f"- Repo head: {payload.get('repo_head') or 'unknown'}",
                f"- Repo dirty: {payload.get('repo_dirty')}",
                f"- Index dir: {payload.get('index_dir')}",
                f"- Index files: {payload.get('index_file_count')}",
            ]
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                },
            )

        return await self._with_db(_handler)


class PaperResearchSearchRepoZoektTool(_PaperResearchToolBase):
    name = "paper_research_search_repo_zoekt"
    input_model = PaperResearchSearchRepoZoektInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "使用 Zoekt 对 repo/source 做 trigram 代码检索。适合大仓库、跨文件搜索和高召回代码定位。"
        "query 使用 Zoekt 查询语法，例如 `classification-results file:\\.sh$`。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "query": {"type": "string", "description": "Zoekt 查询语法，例如 `classify file:\\.py$` 或 `symbol:train`。"},
            "max_results": {"type": "integer", "default": 20, "description": "最多返回多少条命中。范围 1-100。"},
            "context_lines": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 20,
                "description": "命中点上下各返回多少行局部上下文。",
            },
            "auto_index": {"type": "boolean", "default": True, "description": "索引缺失时是否自动构建 Zoekt 索引。"},
            "force_reindex": {"type": "boolean", "default": False, "description": "搜索前是否强制重建索引。"},
        },
        "required": ["project_id", "query"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            query = str(kwargs.get("query") or "").strip()
            max_results = int(kwargs.get("max_results") or 20)
            context_lines = int(kwargs.get("context_lines") or 0)
            auto_index = bool(kwargs.get("auto_index", True))
            force_reindex = bool(kwargs.get("force_reindex", False))
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error
            assert project_payload is not None and workspace is not None and repo_dir is not None

            workspace_dir = self._workspace_dir_for(workspace)
            index_payload: Dict[str, Any] = {}
            if auto_index or force_reindex:
                index_payload = await ZoektCliService.build_index(
                    repo_dir=repo_dir,
                    workspace_dir=workspace_dir,
                    force_reindex=force_reindex,
                )
                if not bool(index_payload.get("success")):
                    error = str(index_payload.get("error") or "zoekt_index_failed")
                    return ToolResult(
                        success=False,
                        output=(
                            "Zoekt 代码检索前的索引准备失败。\n"
                            f"- Project: /projects/{project_id}\n"
                            f"- Error: {error}\n"
                            f"- Search binary: {index_payload.get('search_binary') or 'missing'}\n"
                            f"- Git index binary: {index_payload.get('git_index_binary') or 'missing'}\n"
                            f"- Plain index binary: {index_payload.get('plain_index_binary') or 'missing'}\n"
                            f"- Stderr: {str(index_payload.get('stderr') or '').strip() or 'none'}"
                        ),
                        error=error,
                        data={
                            **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                            **dict(index_payload or {}),
                        },
                    )

            search_payload = await ZoektCliService.search(
                workspace_dir=workspace_dir,
                query=query,
                max_results=max_results,
            )
            if not bool(search_payload.get("success")):
                error = str(search_payload.get("error") or "zoekt_search_failed")
                if error == "zoekt_index_missing" and not auto_index:
                    error_message = "Zoekt 索引不存在。请先调用 `paper_research_build_zoekt_index`，或把 auto_index 设为 true。"
                else:
                    error_message = (
                        "Zoekt 代码检索失败。\n"
                        f"- Error: {error}\n"
                        f"- Search binary: {search_payload.get('search_binary') or 'missing'}\n"
                        f"- Index dir: {search_payload.get('index_dir') or 'missing'}"
                    )
                return ToolResult(
                    success=False,
                    output=error_message,
                    error=error,
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        **dict(search_payload or {}),
                    },
                )

            matches = list(search_payload.get("matches") or [])
            if context_lines > 0:
                enriched_matches: List[Dict[str, Any]] = []
                for item in matches:
                    line_number = int(item.get("line_number") or 0)
                    if line_number > 0:
                        enriched_matches.append(
                            {
                                **item,
                                **self._build_repo_match_context(
                                    repo_dir=repo_dir,
                                    repo_relative_path=str(item.get("repo_relative_path") or ""),
                                    line_number=line_number,
                                    context_lines=context_lines,
                                ),
                            }
                        )
                    else:
                        enriched_matches.append(dict(item))
                matches = enriched_matches

            result_lines: List[str] = []
            for item in matches:
                line_number = int(item.get("line_number") or 0)
                score = float(item.get("score") or 0.0)
                if line_number > 0:
                    result_lines.append(
                        f"- {item.get('relative_path')}:{line_number} | {item.get('line_text')} (score={score:.2f})"
                    )
                else:
                    result_lines.append(
                        f"- {item.get('relative_path')} | filename match (score={score:.2f})"
                    )
                context_text = str(item.get("context_text") or "").strip()
                if context_text:
                    result_lines.append(
                        f"  context {item.get('context_start_line')}-{item.get('context_end_line')}:\n{context_text}"
                    )
            lines = [
                "已使用 Zoekt 搜索 repo/source。",
                f"- Project: /projects/{project_id}",
                f"- Query: {query}",
                f"- Auto index: {auto_index}",
                f"- Force reindex: {force_reindex}",
                f"- Index status: {index_payload.get('status') or 'unchanged'}",
                f"- Index dir: {search_payload.get('index_dir')}",
                f"- Context lines: {context_lines}",
                f"- Matched files: {len(list(search_payload.get('matched_files') or []))}",
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
                    "context_lines": context_lines,
                    "auto_index": auto_index,
                    "force_reindex": force_reindex,
                    "engine": "zoekt",
                    "index_status": index_payload.get("status"),
                    "index_dir": search_payload.get("index_dir"),
                    "matched_file_count": len(list(search_payload.get("matched_files") or [])),
                    "returned_matches": len(matches),
                    "truncated": bool(search_payload.get("truncated")),
                    "matches": matches,
                    "matched_files": list(search_payload.get("matched_files") or []),
                    "zoekt": {
                        **dict(index_payload or {}),
                        **dict(search_payload or {}),
                    },
                },
            )

        return await self._with_db(_handler)


class PaperResearchRunAiderTool(_PaperResearchToolBase):
    name = "paper_research_run_aider"
    input_model = PaperResearchRunAiderInput
    output_max_tokens = 9000
    description = (
        "使用隔离安装的 aider CLI 对当前 Project 做受控编辑。"
        "支持 `target_root=repo` 修改 repo/source，也支持 `target_root=workspace` 局部修改 planning/specs/drafts 下的 JSON/Markdown 产物。"
        "优先把 `editable_files` 缩到最小；`read_only_files` 只提供上下文不允许修改；"
        "`mode=architect` 适合多文件或 edit-format 容易失真的场景；"
        "`dry_run=true` 只预演不落盘；`auto_test + test_cmd` 可在编辑后自动跑验证。"
        "默认禁用 aider auto-commit，避免偷偷提交 git。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "instruction": {
                "type": "string",
                "description": "交给 aider 的单次自然语言任务说明。要求清楚写出目标、限制和验收标准。",
            },
            "target_root": {
                "type": "string",
                "enum": ["repo", "workspace"],
                "default": "repo",
                "description": "repo=在 repo/source 根目录运行 aider；workspace=在 Project workspace 根目录运行，并用 --no-git 允许局部改 JSON/MD truth files。",
            },
            "mode": {
                "type": "string",
                "enum": ["code", "architect", "ask"],
                "default": "code",
                "description": "code=直接编辑；architect=先规划再编辑，通常更稳；ask=以只读分析为主，适合修改前讨论或 dry_run。",
            },
            "editable_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许 aider 修改的相对路径列表。repo 模式下相对 repo/source；workspace 模式下相对 workspace 根。建议尽量小，减少上下文和误改范围。",
            },
            "read_only_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只读上下文文件列表，加入 chat 但不允许修改。适合 README、schema、参考实现、关键配置等。",
            },
            "context_artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "把已有 workspace 归档产物内容拼进 prompt。支持 `paper_summary`、`paper_intake_result`、`experiment_spec`、`grounding_report`、`implementation_spec`、`run_drafts` 或直接给相对路径。",
            },
            "llm_provider": {
                "type": "string",
                "description": "可选，覆盖 aider 使用的 LLM provider。默认走当前后端默认 provider，并通过 OpenAI-compatible API 方式接入。",
            },
            "model_name": {"type": "string", "description": "可选，覆盖主模型。未显式写 provider 前缀时会自动按 openai-compatible 模型名处理。"},
            "editor_model": {"type": "string", "description": "可选，architect 模式下的 editor model。"},
            "weak_model": {"type": "string", "description": "可选，用于 commit message/history summarization 的弱模型。"},
            "edit_format": {"type": "string", "description": "可选，覆盖 aider 主 edit format。"},
            "editor_edit_format": {"type": "string", "description": "可选，覆盖 architect/editor 二段式里的 editor edit format。"},
            "reasoning_effort": {"type": "string", "description": "可选，传给 aider 的 reasoning_effort 参数。仅在底层模型支持时才有意义。"},
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "true 时只预演，不修改文件。适合先看 aider 会怎么动手。",
            },
            "map_tokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": 64000,
                "description": "可选，覆盖 aider repo map token budget。大仓库默认不宜过高。",
            },
            "api_timeout_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 3600,
                "description": "可选，单次模型 API timeout，作用于 aider 内部 LLM 请求。",
            },
            "auto_test": {
                "type": "boolean",
                "default": False,
                "description": "是否在编辑后自动执行 test_cmd。",
            },
            "test_cmd": {
                "type": "string",
                "description": "可选，传给 aider 的测试命令，例如 `pytest backend/tests/test_x.py -q`。配合 auto_test=true 使用。",
            },
            "auto_lint": {
                "type": "boolean",
                "default": False,
                "description": "是否在编辑后自动执行 lint_cmds。",
            },
            "lint_cmds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，传给 aider 的 lint commands。",
            },
            "allow_dirty_repo": {
                "type": "boolean",
                "default": False,
                "description": "repo 模式下是否允许在已有未提交改动的工作树里继续运行 aider。默认 false，避免混入用户脏改动。",
            },
        },
        "required": ["project_id", "instruction"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            target_root = str(kwargs.get("target_root") or "repo").strip().lower() or "repo"
            if target_root == "repo":
                project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
                if repo_error is not None:
                    return repo_error
                assert project_payload is not None and workspace is not None and repo_dir is not None
                workspace_dir = self._workspace_dir_for(workspace)
            else:
                project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
                if project_payload is None:
                    return self._project_not_found(project_id)
                if workspace is None:
                    return self._workspace_not_ready(project_payload, project_id)
                workspace_dir = self._workspace_dir_for(workspace)

            payload = await AiderCliService.run(
                workspace_dir=workspace_dir,
                instruction=str(kwargs.get("instruction") or ""),
                target_root=target_root,
                editable_files=list(kwargs.get("editable_files") or []),
                read_only_files=list(kwargs.get("read_only_files") or []),
                context_artifacts=list(kwargs.get("context_artifacts") or []),
                provider=str(kwargs.get("llm_provider") or settings.default_llm_provider or "").strip() or None,
                model_name=str(kwargs.get("model_name") or "").strip() or None,
                editor_model=str(kwargs.get("editor_model") or "").strip() or None,
                weak_model=str(kwargs.get("weak_model") or "").strip() or None,
                mode=str(kwargs.get("mode") or "code").strip() or "code",
                edit_format=str(kwargs.get("edit_format") or "").strip() or None,
                editor_edit_format=str(kwargs.get("editor_edit_format") or "").strip() or None,
                reasoning_effort=str(kwargs.get("reasoning_effort") or "").strip() or None,
                dry_run=bool(kwargs.get("dry_run", False)),
                map_tokens=kwargs.get("map_tokens"),
                api_timeout_seconds=kwargs.get("api_timeout_seconds"),
                auto_test=bool(kwargs.get("auto_test", False)),
                test_cmd=str(kwargs.get("test_cmd") or "").strip() or None,
                auto_lint=bool(kwargs.get("auto_lint", False)),
                lint_cmds=list(kwargs.get("lint_cmds") or []),
                allow_dirty_repo=bool(kwargs.get("allow_dirty_repo", False)),
            )

            lines = [
                "aider 已执行。",
                f"- Project: /projects/{project_id}",
                f"- Run ID: {payload.get('run_id')}",
                f"- Target root: {payload.get('target_root')}",
                f"- Mode: {payload.get('mode')}",
                f"- Success: {bool(payload.get('success'))}",
                f"- Dry run: {bool(payload.get('dry_run'))}",
                f"- Changed files: {payload.get('changed_file_count')}",
                f"- Run dir: {payload.get('run_dir')}",
            ]
            if payload.get("diff_path"):
                lines.append(f"- Diff: {payload.get('diff_path')}")
            if payload.get("stdout_path"):
                lines.append(f"- Stdout: {payload.get('stdout_path')}")
            if payload.get("error"):
                lines.append(f"- Error: {payload.get('error')}")
            excerpt = str(payload.get("stdout_excerpt") or "").strip()
            if excerpt:
                lines.extend(["Stdout excerpt:", excerpt])
            return ToolResult(
                success=bool(payload.get("success")),
                output="\n".join(lines),
                error=str(payload.get("error") or "") or None,
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                    "aider_run": {
                        "run_id": payload.get("run_id"),
                        "target_root": payload.get("target_root"),
                        "mode": payload.get("mode"),
                        "success": bool(payload.get("success")),
                        "changed_file_count": int(payload.get("changed_file_count") or 0),
                        "changed_files": list(payload.get("changed_files") or []),
                    },
                },
            )

        return await self._with_db(_handler)


class PaperResearchReadAiderRunTool(_PaperResearchToolBase):
    name = "paper_research_read_aider_run"
    input_model = PaperResearchReadAiderRunInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取已归档 aider run 的摘要、stdout、prompt 或 diff。适合在 run 后回看它到底改了什么、为什么失败。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "run_id": {"type": "string", "description": "aider run ID。"},
            "include_stdout": {"type": "boolean", "default": True},
            "include_prompt": {"type": "boolean", "default": False},
            "include_diff": {"type": "boolean", "default": False},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 200000, "default": 20000},
        },
        "required": ["project_id", "run_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)
            payload = AiderCliService.read_run(
                workspace_dir=self._workspace_dir_for(workspace),
                run_id=str(kwargs.get("run_id") or "").strip(),
                include_stdout=bool(kwargs.get("include_stdout", True)),
                include_prompt=bool(kwargs.get("include_prompt", False)),
                include_diff=bool(kwargs.get("include_diff", False)),
                max_chars=int(kwargs.get("max_chars") or 20000),
            )
            lines = [
                "已读取 aider run。",
                f"- Project: /projects/{project_id}",
                f"- Run ID: {payload.get('run_id') or kwargs.get('run_id')}",
                f"- Success: {bool(payload.get('success'))}",
            ]
            if payload.get("error"):
                lines.append(f"- Error: {payload.get('error')}")
            if payload.get("message"):
                lines.append(f"- Message: {payload.get('message')}")
            if payload.get("stdout"):
                lines.extend(["Stdout:", str(payload.get("stdout") or "")])
            if payload.get("diff"):
                lines.extend(["Diff:", str(payload.get("diff") or "")])
            if payload.get("prompt"):
                lines.extend(["Prompt:", str(payload.get("prompt") or "")])
            return ToolResult(
                success=bool(payload.get("success")),
                output="\n".join(lines),
                error=str(payload.get("error") or "") or None,
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                },
            )

        return await self._with_db(_handler)


class PaperResearchTailAiderLogTool(_PaperResearchToolBase):
    name = "paper_research_tail_aider_log"
    input_model = PaperResearchTailAiderLogInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "快速读取 aider stdout 日志尾部。适合看最近一次编辑 run 的错误、模型拒绝、edit format 失败等。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "run_id": {"type": "string", "description": "aider run ID。"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 200000, "default": 12000},
        },
        "required": ["project_id", "run_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)
            payload = AiderCliService.tail_log(
                workspace_dir=self._workspace_dir_for(workspace),
                run_id=str(kwargs.get("run_id") or "").strip(),
                max_chars=int(kwargs.get("max_chars") or 12000),
            )
            lines = [
                "已读取 aider 日志尾部。",
                f"- Project: /projects/{project_id}",
                f"- Run ID: {payload.get('run_id') or kwargs.get('run_id')}",
            ]
            if payload.get("error"):
                lines.append(f"- Error: {payload.get('error')}")
            if payload.get("message"):
                lines.append(f"- Message: {payload.get('message')}")
            if payload.get("tail"):
                lines.extend(["Log tail:", str(payload.get("tail") or "")])
            return ToolResult(
                success=bool(payload.get("success")),
                output="\n".join(lines),
                error=str(payload.get("error") or "") or None,
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                },
            )

        return await self._with_db(_handler)


class PaperResearchAssessRepoMainpathTool(_PaperResearchToolBase):
    name = "paper_research_assess_repo_mainpath"
    input_model = PaperResearchAssessRepoMainpathInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "基于 README、repo_file_index、entrypoint hints 和仓库文件名，评估当前 Project 最可能的 repo 主路径。"
        "这是主路径判断工具，不执行代码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
        },
        "required": ["project_id"],
    }

    @staticmethod
    def _normalize_hint_token(value: str) -> str:
        token = re.sub(r"[^a-z0-9._/-]+", " ", str(value or "").strip().lower())
        token = re.sub(r"\s+", " ", token).strip()
        return token

    @staticmethod
    def _extract_readme_commands(text: str) -> List[str]:
        commands: List[str] = []
        content = str(text or "")
        fence_pattern = re.compile(r"```(?:bash|sh|shell|python)?\n(.*?)```", re.DOTALL | re.IGNORECASE)
        for block in fence_pattern.findall(content):
            for line in str(block).splitlines():
                stripped = line.strip()
                if stripped:
                    commands.append(stripped)
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r"^(python|python3|bash|sh|make|docker|docker-compose|docker compose|papermill|jupyter)\b", stripped):
                commands.append(stripped)
        deduped: List[str] = []
        seen: set[str] = set()
        for item in commands:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped[:12]

    @staticmethod
    def _extract_readme_intake_commands(payload: Dict[str, Any]) -> List[str]:
        commands: List[str] = []
        for item in list(payload.get("run_commands") or []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            if command:
                commands.append(command)
        for item in list(payload.get("installation_steps") or []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            if command:
                commands.append(command)
        deduped: List[str] = []
        seen: set[str] = set()
        for item in commands:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped[:12]

    @staticmethod
    def _extract_readme_intake_path_hints(payload: Dict[str, Any]) -> List[str]:
        hints: List[str] = []
        for item in list(payload.get("entrypoints") or []):
            if not isinstance(item, dict):
                continue
            hint = str(item.get("path_or_hint") or "").strip()
            if hint:
                hints.append(hint)
        for item in list(payload.get("run_commands") or []):
            if not isinstance(item, dict):
                continue
            hint = str(item.get("entrypoint_path_or_hint") or "").strip()
            if hint:
                hints.append(hint)
        for item in list(payload.get("focus_files") or []):
            text = str(item or "").strip()
            if text:
                hints.append(text)
        return hints[:20]

    @staticmethod
    def _extract_readme_intake_evidence(payload: Dict[str, Any]) -> List[str]:
        evidence: List[str] = []
        for item in list(payload.get("evidence_snippets") or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                evidence.append(text)
        for item in list(payload.get("run_commands") or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("evidence_text") or "").strip()
            if text:
                evidence.append(text)
        deduped: List[str] = []
        seen: set[str] = set()
        for item in evidence:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped[:8]

    @staticmethod
    def _classify_mainpath(path: str) -> str:
        lowered = str(path or "").strip().lower()
        if lowered.endswith(".ipynb"):
            return "notebook"
        if lowered.endswith(".sh"):
            return "shell_script"
        if lowered.endswith(".py"):
            return "python_script"
        if lowered.endswith("docker-compose.yml") or lowered.endswith("compose.yaml"):
            return "docker_compose"
        if lowered.endswith("dockerfile"):
            return "dockerfile"
        if lowered.endswith("readme.md"):
            return "readme"
        return "file"

    @classmethod
    def _score_mainpath_candidate(
        cls,
        *,
        path: str,
        hints: Sequence[str],
        readme_commands: Sequence[str],
        readme_text: str,
    ) -> tuple[int, List[str], List[str], List[str]]:
        lowered = str(path or "").strip().lower()
        score = 0
        reasons: List[str] = []
        cautions: List[str] = []
        evidence_excerpts: List[str] = []
        if lowered.endswith(".sh"):
            score += 5
            reasons.append("shell entrypoint")
        elif lowered.endswith(".py"):
            score += 4
            reasons.append("python entrypoint")
        elif lowered.endswith(".ipynb"):
            score += 3
            reasons.append("notebook entrypoint")
        for token in ("classification-results", "train", "main", "run", "demo", "example", "eval", "infer", "predict"):
            if token in lowered:
                score += 3
                reasons.append(f"filename contains `{token}`")
                break
        for hint in hints:
            if hint and hint in lowered:
                score += 5
                reasons.append(f"matches entrypoint hint `{hint}`")
                evidence_excerpts.append(f"Hint match: {hint}")
        basename = lowered.split("/")[-1]
        readme_text_lower = readme_text.lower()
        if basename and basename in readme_text_lower:
            score += 4
            reasons.append("mentioned in README excerpt")
            evidence_excerpts.append(f"README mentions `{basename}`")
        matching_commands = [
            command
            for command in readme_commands
            if basename and basename in str(command).strip().lower()
        ]
        if matching_commands:
            score += 8
            reasons.append("appears in README command")
            evidence_excerpts.extend(f"README command: {command}" for command in matching_commands[:2])
        if "/alignment/" in f"/{lowered}":
            score -= 5
            cautions.append("alignment subdirectory looks task-specific and may not match the current paper task")
        if any(token in lowered for token in ("/examples/", "/example/", "/demo/")):
            score -= 3
            cautions.append("example/demo style path is often illustrative rather than the main reproduction path")
        if lowered.startswith("docs/") or "/docs/" in lowered:
            score -= 4
            cautions.append("documentation path is usually not an executable main path")
        return score, reasons, cautions, evidence_excerpts

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
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
                    output="当前 Project 还没有可用的 repo/source。请先准备 workspace 或 clone repo。",
                    error="repo_not_available",
                    data={**self._root_descriptor(project_payload=project_payload, workspace=workspace), "project_id": project_id},
                )

            repo_index = self._read_json_file(workspace_dir / "repo_file_index.json")
            repo_reference = self._read_json_file(workspace_dir / "repo_reference.json")
            experiment_spec = self._read_json_file(workspace_dir / "experiment_spec.json")
            readme_intake = self._read_json_file(
                workspace_dir / str(repo_index.get("readme_reproduction_intake_file") or "repo_readme_reproduction_intake.json")
            )
            readme_excerpt = ""
            excerpt_path = workspace_dir / str(repo_index.get("readme_excerpt_file") or "repo_readme_excerpt.md")
            if excerpt_path.is_file():
                try:
                    readme_excerpt = excerpt_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    readme_excerpt = ""

            hint_tokens = [
                self._normalize_hint_token(str(item.get("value") or item.get("evidence_text") or ""))
                for item in list(experiment_spec.get("entrypoint_hints") or [])
                if isinstance(item, dict)
            ]
            hint_tokens.extend(self._normalize_hint_token(item) for item in self._extract_readme_intake_path_hints(readme_intake))
            hint_tokens = [item for item in hint_tokens if item]

            repo_files = [str(item).strip() for item in list(repo_index.get("files") or []) if str(item).strip()]
            ranked_seed = [dict(item) for item in list(repo_index.get("entrypoint_candidates") or []) if isinstance(item, dict)]
            candidate_paths: List[str] = []
            for item in ranked_seed:
                path = str(item.get("path") or "").strip()
                if path:
                    candidate_paths.append(path)
            for path in repo_files:
                lowered = path.lower()
                if lowered.endswith((".sh", ".py", ".ipynb")) or lowered in {"readme.md", "docker-compose.yml", "compose.yaml", "dockerfile"}:
                    candidate_paths.append(path)
            deduped_paths: List[str] = []
            seen_paths: set[str] = set()
            for path in candidate_paths:
                key = path.strip()
                if not key or key in seen_paths:
                    continue
                seen_paths.add(key)
                deduped_paths.append(key)

            scored_candidates: List[Dict[str, Any]] = []
            readme_commands = self._extract_readme_intake_commands(readme_intake)
            if not readme_commands:
                readme_commands = self._extract_readme_commands(readme_excerpt)
            for path in deduped_paths:
                score, reasons, cautions, evidence_excerpts = self._score_mainpath_candidate(
                    path=path,
                    hints=hint_tokens,
                    readme_commands=readme_commands,
                    readme_text=readme_excerpt,
                )
                if evidence_excerpts:
                    evidence_excerpts = list(dict.fromkeys([*evidence_excerpts, *self._extract_readme_intake_evidence(readme_intake)]))
                if score <= 0:
                    continue
                scored_candidates.append(
                    {
                        "path": path,
                        "entrypoint_type": self._classify_mainpath(path),
                        "score": score,
                        "reasons": reasons,
                        "cautions": cautions,
                        "evidence_excerpts": evidence_excerpts[:4],
                    }
                )
            scored_candidates.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
            selected_candidate = scored_candidates[0] if scored_candidates else None
            status = "identified" if selected_candidate else ("ambiguous" if scored_candidates else "missing")
            top_candidates: List[Dict[str, Any]] = []
            for index, item in enumerate(scored_candidates[:8]):
                candidate = {
                    "path": item.get("path"),
                    "entrypoint_type": item.get("entrypoint_type"),
                    "score": item.get("score"),
                    "task_match": "high" if int(item.get("score") or 0) >= 12 else ("medium" if int(item.get("score") or 0) >= 7 else "low"),
                    "reasons": list(item.get("reasons") or []),
                    "cautions": list(item.get("cautions") or []),
                    "evidence_excerpts": list(item.get("evidence_excerpts") or []),
                }
                if index == 0:
                    candidate["why_selected"] = ", ".join(list(item.get("reasons") or [])) or "highest score"
                else:
                    not_selected_parts = []
                    if item.get("cautions"):
                        not_selected_parts.append(", ".join(list(item.get("cautions") or [])[:2]))
                    not_selected_parts.append("lower score than selected candidate")
                    candidate["why_not_selected"] = "; ".join(part for part in not_selected_parts if part)
                top_candidates.append(candidate)
            lines = [
                "已评估 repo 主路径。",
                f"- Project: /projects/{project_id}",
                f"- Repo URL: {repo_reference.get('repo_url') or 'unknown'}",
                f"- Status: {status}",
                f"- README commands: {len(readme_commands)}",
                f"- README intake: {bool(readme_intake)}",
                f"- Candidate count: {len(scored_candidates)}",
            ]
            if selected_candidate:
                lines.append(
                    f"- Selected main path: repo/source/{selected_candidate.get('path')} "
                    f"({selected_candidate.get('entrypoint_type')}, score={selected_candidate.get('score')})"
                )
                if selected_candidate.get("reasons"):
                    lines.append(f"- Why: {', '.join(list(selected_candidate.get('reasons') or []))}")
                if selected_candidate.get("evidence_excerpts"):
                    lines.append("- Selected evidence:")
                    lines.extend(f"  - {item}" for item in list(selected_candidate.get("evidence_excerpts") or [])[:3])
            if readme_commands:
                lines.append("- README commands:")
                lines.extend(f"  - {item}" for item in readme_commands[:6])
            if top_candidates:
                lines.append("- Top candidates:")
                for item in top_candidates:
                    lines.append(
                        f"  - repo/source/{item.get('path')} [{item.get('entrypoint_type')}] score={item.get('score')}"
                    )
                    if item.get("why_not_selected"):
                        lines.append(f"    why_not_selected: {item.get('why_not_selected')}")

            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "project_id": project_id,
                    "status": status,
                    "repo_url": repo_reference.get("repo_url"),
                    "readme_main_commands": readme_commands,
                    "main_entry_candidates": scored_candidates[:12],
                    "top_candidates": top_candidates,
                    "selected_main_path": selected_candidate,
                    "selected_main_path_reason": ", ".join(list(selected_candidate.get("reasons") or [])) if selected_candidate else None,
                },
            )

        return await self._with_db(_handler)


class PaperResearchListOutputsTool(_PaperResearchToolBase):
    name = "paper_research_list_outputs"
    input_model = PaperResearchListOutputsInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "列出当前 Project/workspace 可管理的归档产物，可按 scope 过滤，供 LLM 判断是否需要清理现场。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "scope": {
                "type": "string",
                "enum": ["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"],
                "default": "all",
                "description": "只返回指定 scope 的产物；all 返回全部。",
            },
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService

            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            scope = str(kwargs.get("scope") or "all").strip().lower() or "all"
            service = ProjectService(db)
            outputs = await service.list_workspace_outputs(
                project_id=project_id,
                user_id=self.user_id,
                workspace_id=int(workspace.id),
            )
            if outputs is None:
                return self._workspace_not_ready(project_payload, project_id)
            items = list(outputs or [])
            if scope != "all":
                items = [item for item in items if str(item.get("scope") or "").strip() == scope]

            lines = [
                "已列出当前 Project/workspace 的归档产物。",
                f"- Project: /projects/{project_id}",
                f"- Scope: {scope}",
                f"- Count: {len(items)}",
            ]
            if items:
                lines.append("- Outputs:")
                for item in items[:40]:
                    lines.append(
                        "  - "
                        f"{item.get('relative_path')} "
                        f"[scope={item.get('scope')}, kind={item.get('kind')}, present={bool(item.get('present'))}]"
                    )
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "scope": scope,
                    "outputs": items,
                    "count": len(items),
                },
            )

        return await self._with_db(_handler)


class PaperResearchDeleteOutputTool(_PaperResearchToolBase):
    name = "paper_research_delete_output"
    input_model = PaperResearchDeleteOutputInput
    description = "删除当前 Project/workspace 下的单个归档产物文件或 compare_report 记录。用于让 LLM 清理旧现场后重做。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "relative_path": {"type": "string", "description": "要删除的产物相对路径，例如 `specs/grounding_report.json`。"},
        },
        "required": ["project_id", "relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService

            project_id = int(kwargs["project_id"])
            relative_path = str(kwargs.get("relative_path") or "").strip()
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            service = ProjectService(db)
            payload = await service.delete_workspace_output(
                project_id=project_id,
                user_id=self.user_id,
                workspace_id=int(workspace.id),
                relative_path=relative_path,
            )
            if payload is None:
                return ToolResult(
                    success=False,
                    output=f"未找到可删除的产物：{relative_path}",
                    error="workspace_output_not_found",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "relative_path": relative_path,
                    },
                )
            return ToolResult(
                success=True,
                output=(
                    "已删除 Project/workspace 产物。\n"
                    f"- Project: /projects/{project_id}\n"
                    f"- Relative path: {relative_path}"
                ),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                },
            )

        return await self._with_db(_handler)


class PaperResearchCleanupScopeTool(_PaperResearchToolBase):
    name = "paper_research_cleanup_scope"
    input_model = PaperResearchCleanupScopeInput
    description = "按 scope 清理当前 Project/workspace 产物，例如 planning/grounding/executions。用于让 LLM 重做某阶段前先清现场。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "scope": {
                "type": "string",
                "enum": ["all", "planning", "repo_analysis", "grounding", "implementation", "run_drafts", "executions", "results"],
                "default": "all",
                "description": "要清理的产物范围。all 表示清空全部可管理产物并保留 paper_repo。",
            },
        },
        "required": ["project_id", "scope"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService

            project_id = int(kwargs["project_id"])
            scope = str(kwargs.get("scope") or "all").strip().lower() or "all"
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            service = ProjectService(db)
            payload = await service.cleanup_workspace_outputs_scope(
                project_id=project_id,
                user_id=self.user_id,
                workspace_id=int(workspace.id),
                scope=scope,
            )
            if payload is None:
                return self._workspace_not_ready(project_payload, project_id)

            lines = [
                "已清理 Project/workspace 产物。",
                f"- Project: /projects/{project_id}",
                f"- Scope: {scope}",
                f"- Deleted files: {int(payload.get('deleted_file_count') or 0)}",
                f"- Deleted dirs: {int(payload.get('deleted_dir_count') or 0)}",
                f"- Deleted runs: {int(payload.get('deleted_run_count') or 0)}",
            ]
            deleted_paths = [str(item).strip() for item in list(payload.get("deleted_paths") or []) if str(item).strip()]
            if deleted_paths:
                lines.append("- Deleted paths:")
                lines.extend(f"  - {item}" for item in deleted_paths[:30])
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **dict(payload or {}),
                },
            )

        return await self._with_db(_handler)


class PaperResearchGitStatusTool(_PaperResearchToolBase):
    name = "paper_research_git_status"
    input_model = PaperResearchGitStatusInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 仓库的 git status，返回分支、脏文件和未跟踪文件。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "include_untracked": {"type": "boolean", "default": True, "description": "是否包含未跟踪文件。"},
            "max_entries": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000, "description": "最多返回多少条状态项。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error

            command = ["status", "--short", "--branch"]
            if not bool(kwargs.get("include_untracked", True)):
                command.append("--untracked-files=no")
            git_result = await self._run_repo_git_command(repo_dir=repo_dir, git_args=command)
            if not git_result.get("available"):
                return ToolResult(success=False, output="当前环境没有可用的 git 命令。", error="git_not_installed")
            if git_result.get("timeout"):
                return ToolResult(success=False, output="git status 超时，未能返回结果。", error="git_status_timeout")
            if int(git_result.get("returncode") or 0) != 0:
                stderr = str(git_result.get("stderr") or "").strip()
                return ToolResult(
                    success=False,
                    output=f"git status 失败: {stderr or 'unknown error'}",
                    error="git_status_failed",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "command": git_result.get("command"),
                        "stderr": stderr,
                    },
                )

            max_entries = int(kwargs.get("max_entries") or 200)
            lines = str(git_result.get("stdout") or "").splitlines()
            branch_line = lines[0].strip() if lines and lines[0].startswith("## ") else None
            status_lines = lines[1:] if branch_line else lines
            entries = status_lines[:max_entries]
            truncated = len(status_lines) > len(entries)
            clean = len(status_lines) == 0

            output_lines = [
                "已读取 repo git status。",
                f"- Project: /projects/{project_id}",
                f"- Repo root: repo/source",
                f"- Branch: {branch_line.removeprefix('## ').strip() if branch_line else 'unknown'}",
                f"- Clean: {clean}",
                f"- Returned entries: {len(entries)}/{len(status_lines)}",
            ]
            if entries:
                output_lines.append("Status entries:")
                output_lines.extend(entries)
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "branch": branch_line.removeprefix("## ").strip() if branch_line else None,
                    "clean": clean,
                    "entries": entries,
                    "returned_entries": len(entries),
                    "total_entries": len(status_lines),
                    "truncated": truncated,
                    "command": git_result.get("command"),
                },
            )

        return await self._with_db(_handler)


class PaperResearchGitDiffTool(_PaperResearchToolBase):
    name = "paper_research_git_diff"
    input_model = PaperResearchGitDiffInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 仓库的 git diff，可按 pathspec 聚焦具体文件。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_relative_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，repo/source 下的 pathspec 列表；不需要带 `repo/source/` 前缀。",
            },
            "cached": {"type": "boolean", "default": False, "description": "是否读取 staged diff（git diff --cached）。"},
            "ref": {"type": "string", "description": "可选，对比基准 ref，例如 `HEAD~1`。"},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 200, "maximum": 200000, "description": "最多返回多少字符。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error

            repo_relative_paths = list(kwargs.get("repo_relative_paths") or [])
            cached = bool(kwargs.get("cached", False))
            ref = str(kwargs.get("ref") or "").strip() or None
            max_chars = int(kwargs.get("max_chars") or 20000)
            command = ["diff", "--no-ext-diff"]
            if cached:
                command.append("--cached")
            if ref:
                command.append(ref)
            if repo_relative_paths:
                command.append("--")
                command.extend(repo_relative_paths)

            git_result = await self._run_repo_git_command(repo_dir=repo_dir, git_args=command)
            if not git_result.get("available"):
                return ToolResult(success=False, output="当前环境没有可用的 git 命令。", error="git_not_installed")
            if git_result.get("timeout"):
                return ToolResult(success=False, output="git diff 超时，未能返回结果。", error="git_diff_timeout")
            if int(git_result.get("returncode") or 0) != 0:
                stderr = str(git_result.get("stderr") or "").strip()
                return ToolResult(
                    success=False,
                    output=f"git diff 失败: {stderr or 'unknown error'}",
                    error="git_diff_failed",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "command": git_result.get("command"),
                        "stderr": stderr,
                    },
                )

            diff_text = str(git_result.get("stdout") or "")
            truncated = len(diff_text) > max_chars
            rendered = diff_text[:max_chars]
            output_lines = [
                "已读取 repo git diff。",
                f"- Project: /projects/{project_id}",
                f"- Repo root: repo/source",
                f"- Cached: {cached}",
                f"- Ref: {ref or 'working-tree vs index/HEAD'}",
                f"- Paths: {', '.join(repo_relative_paths) if repo_relative_paths else '(all)'}",
                f"- Returned chars: {len(rendered)}/{len(diff_text)}",
                "Diff:",
                rendered or "(no diff)",
            ]
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "diff": rendered,
                    "total_chars": len(diff_text),
                    "returned_chars": len(rendered),
                    "truncated": truncated,
                    "cached": cached,
                    "ref": ref,
                    "repo_relative_paths": repo_relative_paths,
                    "command": git_result.get("command"),
                },
            )

        return await self._with_db(_handler)


class PaperResearchGitLogTool(_PaperResearchToolBase):
    name = "paper_research_git_log"
    input_model = PaperResearchGitLogInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 仓库最近提交历史，可按 pathspec 聚焦文件。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_relative_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，repo/source 下的 pathspec 列表；不需要带 `repo/source/` 前缀。",
            },
            "max_count": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100, "description": "最多返回多少条提交记录。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error

            repo_relative_paths = list(kwargs.get("repo_relative_paths") or [])
            max_count = int(kwargs.get("max_count") or 10)
            command = [
                "log",
                f"-n{max_count}",
                "--date=short",
                "--pretty=format:%H%x09%h%x09%ad%x09%s",
            ]
            if repo_relative_paths:
                command.append("--")
                command.extend(repo_relative_paths)

            git_result = await self._run_repo_git_command(repo_dir=repo_dir, git_args=command)
            if not git_result.get("available"):
                return ToolResult(success=False, output="当前环境没有可用的 git 命令。", error="git_not_installed")
            if git_result.get("timeout"):
                return ToolResult(success=False, output="git log 超时，未能返回结果。", error="git_log_timeout")
            if int(git_result.get("returncode") or 0) != 0:
                stderr = str(git_result.get("stderr") or "").strip()
                return ToolResult(
                    success=False,
                    output=f"git log 失败: {stderr or 'unknown error'}",
                    error="git_log_failed",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "command": git_result.get("command"),
                        "stderr": stderr,
                    },
                )

            commits: List[Dict[str, Any]] = []
            rendered_lines: List[str] = []
            for raw_line in str(git_result.get("stdout") or "").splitlines():
                parts = raw_line.split("\t", 3)
                if len(parts) < 4:
                    continue
                full_sha, short_sha, commit_date, subject = parts
                commits.append(
                    {
                        "sha": full_sha,
                        "short_sha": short_sha,
                        "date": commit_date,
                        "subject": subject,
                    }
                )
                rendered_lines.append(f"{short_sha} {commit_date} {subject}")

            output_lines = [
                "已读取 repo git log。",
                f"- Project: /projects/{project_id}",
                f"- Repo root: repo/source",
                f"- Paths: {', '.join(repo_relative_paths) if repo_relative_paths else '(all)'}",
                f"- Returned commits: {len(commits)}",
            ]
            if rendered_lines:
                output_lines.append("Commits:")
                output_lines.extend(rendered_lines)
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "commits": commits,
                    "repo_relative_paths": repo_relative_paths,
                    "command": git_result.get("command"),
                },
            )

        return await self._with_db(_handler)


class PaperResearchGitShowTool(_PaperResearchToolBase):
    name = "paper_research_git_show"
    input_model = PaperResearchGitShowInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取某个 git ref 的提交摘要，或直接读取该 ref 下某个文件的内容。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "ref": {"type": "string", "description": "git ref，例如 `HEAD`、`HEAD~1`、commit sha。"},
            "repo_relative_path": {"type": "string", "description": "可选，repo/source 下的文件路径；提供后读取 `ref:path` 的文件内容。"},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 200, "maximum": 200000, "description": "最多返回多少字符。"},
        },
        "required": ["project_id", "ref"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload, workspace, repo_dir, repo_error = await self._resolve_repo_workspace(db, project_id=project_id)
            if repo_error is not None:
                return repo_error

            ref = str(kwargs.get("ref") or "").strip()
            repo_relative_path = self._normalize_repo_relative_path(kwargs.get("repo_relative_path"))
            max_chars = int(kwargs.get("max_chars") or 20000)
            if repo_relative_path:
                command = ["show", f"{ref}:{repo_relative_path}"]
            else:
                command = ["show", "--stat", "--summary", "--format=fuller", ref]

            git_result = await self._run_repo_git_command(repo_dir=repo_dir, git_args=command)
            if not git_result.get("available"):
                return ToolResult(success=False, output="当前环境没有可用的 git 命令。", error="git_not_installed")
            if git_result.get("timeout"):
                return ToolResult(success=False, output="git show 超时，未能返回结果。", error="git_show_timeout")
            if int(git_result.get("returncode") or 0) != 0:
                stderr = str(git_result.get("stderr") or "").strip()
                return ToolResult(
                    success=False,
                    output=f"git show 失败: {stderr or 'unknown error'}",
                    error="git_show_failed",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "command": git_result.get("command"),
                        "stderr": stderr,
                        "ref": ref,
                        "repo_relative_path": repo_relative_path or None,
                    },
                )

            content = str(git_result.get("stdout") or "")
            truncated = len(content) > max_chars
            rendered = content[:max_chars]
            output_lines = [
                "已读取 repo git show。",
                f"- Project: /projects/{project_id}",
                f"- Repo root: repo/source",
                f"- Ref: {ref}",
                f"- Path: {repo_relative_path or '(commit summary)'}",
                f"- Returned chars: {len(rendered)}/{len(content)}",
                "Content:",
                rendered,
            ]
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "ref": ref,
                    "repo_relative_path": repo_relative_path or None,
                    "content": rendered,
                    "total_chars": len(content),
                    "returned_chars": len(rendered),
                    "truncated": truncated,
                    "command": git_result.get("command"),
                },
            )

        return await self._with_db(_handler)


class PaperResearchWriteGroundingReportTool(_PaperResearchToolBase):
    name = "paper_research_write_grounding_report"
    input_model = PaperResearchWriteGroundingReportInput
    description = "将 grounding 阶段的结构化证据闭环报告归档到当前 Project 工作区。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID（>=1）。"},
            "grounding_report": {
                "type": "object",
                "description": (
                    "要保存的 grounding_report JSON。必须覆盖 repo、entrypoint、dataset、runtime、"
                    "external_dependencies、summary 六个顶层对象，并将事实收敛为 grounded/absent/blocked/unknown。"
                    "summary 里应尽量明确 `run_decision=ready|runnable_with_patch|blocked`，表示当前 repo 主路径的可运行判断。"
                    "如果把一组 dataset/external URLs 声明为 grounded，必须为每个必要官方链接提供成功的 probe 结果，"
                    "或明确证明本地已存在。canonical 结构中 `dataset.sources` 保存数据源，"
                    "`external_dependencies.urls` 只放 URL 字符串，`external_dependencies.probe_results` 单独保存逐条 probe 结果。"
                    "每条 `external_dependencies.probe_results` 至少应包含 `url` 与布尔 `ok`，可附带 `status_code/content_type/detected_kind/diagnosis/page_kind`。"
                    "如果 probe 结果是 `gdrive_confirm_required`、`download_gate`、Google Drive virus-scan warning，或脚本已能自动处理 confirm gate，这表示官方链接仍然存活且可恢复；"
                    "此时不要把 `dataset.status` / `external_dependencies.status` 写成 `blocked`，也不要把它重写成 `not_found`。"
                    "只有明确 `not_found` / `access_denied` / `quota_limited` / `http_4xx` 这类终态时，才应把对应 section 写成 `blocked`。"
                    "blocked 场景应把失败原因写进各 section 的 `blockers`/`blocker_details`，并把替代源候选写进 "
                    "`dataset.alternative_source_candidates` 或 `external_dependencies.alternative_source_candidates`。"
                ),
            },
        },
        "required": ["project_id", "grounding_report"],
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
            relative_path = "specs/grounding_report.json"
            actual_path = workspace_dir / "specs" / "grounding_report.json"
            payload = dict(kwargs.get("grounding_report") or {})
            payload.setdefault("schema_version", "grounding_report_v1")
            payload.setdefault("paper_id", project_payload.get("paper_id"))
            payload.setdefault("project_id", int(project_id))
            payload.setdefault("workspace_id", int(workspace.id))
            payload.setdefault("notebook_id", str(workspace.notebook_id or ""))
            payload.setdefault("root_alias", self._PROJECT_ROOT_ALIAS)
            payload.setdefault("repo_root_relative_path", "repo/source")
            payload = self._normalize_grounding_report_payload(payload, workspace_dir=workspace_dir)
            validation_errors = self._validate_grounding_report_payload(payload, workspace_dir=workspace_dir)
            if validation_errors:
                structured_errors = self._structured_grounding_validation_errors(
                    project_id=project_id,
                    payload=payload,
                    validation_errors=validation_errors,
                )
                return ToolResult(
                    success=False,
                    output=(
                        "grounding_report JSON 未通过归档校验，未写入文件。\n"
                        + "\n".join(f"- {item}" for item in validation_errors[:12])
                    ),
                    error="grounding_report_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "relative_path": relative_path,
                        "saved": False,
                        "validation_errors": validation_errors,
                        "structured_validation_errors": structured_errors,
                    },
                )

            actual_path.parent.mkdir(parents=True, exist_ok=True)
            actual_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            completion = self._grounding_completion_summary(payload)
            lines = [
                "已写入 grounding report。",
                f"- Project: /projects/{project_id}",
                f"- Relative path: {relative_path}",
                f"- Overall status: {completion.get('overall_status')}",
            ]
            blockers = [str(item).strip() for item in list(completion.get("blockers") or []) if str(item).strip()]
            next_actions = [str(item).strip() for item in list(completion.get("next_actions") or []) if str(item).strip()]
            if blockers:
                lines.append("- Blockers:")
                lines.extend(f"  - {item}" for item in blockers[:6])
            if next_actions:
                lines.append("- Next actions:")
                lines.extend(f"  - {item}" for item in next_actions[:4])
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "relative_path": relative_path,
                    "saved": True,
                    "current_stage": "grounding",
                    "grounding_status": completion.get("overall_status"),
                    "grounding_ready": bool(completion.get("complete")),
                    "content": payload,
                },
            )

        return await self._with_db(_handler)


class PaperResearchReadGroundingReportTool(_PaperResearchToolBase):
    name = "paper_research_read_grounding_report"
    input_model = PaperResearchReadGroundingReportInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "读取当前 Project 已归档的 grounding_report。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "mode": {
                "type": "string",
                "enum": ["auto", "full", "chunk", "page", "line_range"],
                "default": "auto",
                "description": "读取模式。full 返回全文；chunk/page/line_range 返回原文分段，不做摘要。",
            },
            "max_chars": {"type": "integer", "default": 20000, "description": "兼容旧调用的字符窗口参数；chunk 模式下会作为默认 chunk_chars。"},
            "chunk_index": {"type": "integer", "description": "chunk 模式下的块序号（1-based）。"},
            "chunk_chars": {"type": "integer", "description": "chunk 模式下每块字符数。"},
            "page": {"type": "integer", "description": "page 模式下的页号（1-based，按行分页）。"},
            "page_size_lines": {"type": "integer", "description": "page 模式下每页多少行。"},
            "line_start": {"type": "integer", "description": "line_range 模式起始行号（1-based）。"},
            "line_end": {"type": "integer", "description": "line_range 模式结束行号（1-based）。"},
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
            relative_path="specs/grounding_report.json",
            mode=str(kwargs.get("mode") or "auto"),
            max_chars=int(kwargs.get("max_chars") or 20000),
            chunk_index=kwargs.get("chunk_index"),
            chunk_chars=kwargs.get("chunk_chars"),
            page=kwargs.get("page"),
            page_size_lines=kwargs.get("page_size_lines"),
            line_start=kwargs.get("line_start"),
            line_end=kwargs.get("line_end"),
        )


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
            validation_errors = self._validate_implementation_spec_payload(payload, workspace_dir=workspace_dir)
            grounding_report = self._read_grounding_report_payload(workspace_dir)
            grounding_conflicts = self._implementation_grounding_conflicts(
                payload=payload,
                grounding_report=grounding_report,
            )
            if validation_errors or grounding_conflicts:
                return ToolResult(
                    success=False,
                    output=(
                        "implementation_spec JSON 未通过归档校验，未写入文件。\n"
                        + "\n".join(f"- {item}" for item in validation_errors[:12])
                        + (
                            (
                                "\n- 与 grounding_report 的冲突:\n"
                                + "\n".join(f"  - {item.get('message')}" for item in grounding_conflicts[:8])
                            )
                            if grounding_conflicts
                            else ""
                        )
                    ),
                    error="implementation_spec_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "relative_path": relative_path,
                        "saved": False,
                        "validation_errors": validation_errors,
                        "schema_errors": [
                            {
                                "path": re.match(r"^`([^`]+)`", item).group(1) if re.match(r"^`([^`]+)`", item) else "unknown",
                                "code": "schema_constraint_failed",
                                "message": item,
                            }
                            for item in validation_errors
                        ],
                        "grounding_conflicts": grounding_conflicts,
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
            "mode": {
                "type": "string",
                "enum": ["auto", "full", "chunk", "page", "line_range"],
                "default": "auto",
                "description": "读取模式。full 返回全文；chunk/page/line_range 返回原文分段，不做摘要。",
            },
            "max_chars": {"type": "integer", "default": 20000, "description": "兼容旧调用的字符窗口参数；chunk 模式下会作为默认 chunk_chars。"},
            "chunk_index": {"type": "integer", "description": "chunk 模式下的块序号（1-based）。"},
            "chunk_chars": {"type": "integer", "description": "chunk 模式下每块字符数。"},
            "page": {"type": "integer", "description": "page 模式下的页号（1-based，按行分页）。"},
            "page_size_lines": {"type": "integer", "description": "page 模式下每页多少行。"},
            "line_start": {"type": "integer", "description": "line_range 模式起始行号（1-based）。"},
            "line_end": {"type": "integer", "description": "line_range 模式结束行号（1-based）。"},
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
            mode=str(kwargs.get("mode") or "auto"),
            max_chars=int(kwargs.get("max_chars") or 20000),
            chunk_index=kwargs.get("chunk_index"),
            chunk_chars=kwargs.get("chunk_chars"),
            page=kwargs.get("page"),
            page_size_lines=kwargs.get("page_size_lines"),
            line_start=kwargs.get("line_start"),
            line_end=kwargs.get("line_end"),
        )


class PaperResearchWriteRunDraftsTool(_PaperResearchToolBase):
    name = "paper_research_write_run_drafts"
    input_model = PaperResearchWriteRunDraftsInput
    description = (
        "校验并归档 implementation-spec 派生出的 run drafts JSON 到当前 Project 工作区。"
        "每个 draft 必须使用当前 schema：id/title/objective/entrypoint{type,path_or_hint}/"
        "depends_on/data_requirements/env_requirements/params/expected_outputs/blockers/evidence_files/grounding_notes。"
        "repo_script/notebook/config 只能引用已经存在的 repo 文件；README 里的手工步骤或命令摘要应写成 "
        "readme_command/dataset_step/manual_step，不要伪造一个 repo_script。"
        "这里的 repo_script 只表示“真实存在的 repo 文件”，并不等于 execution_spec 阶段一定使用 "
        "execution_intent.repo_script；如果该文件是可执行 shell 脚本，execution 阶段通常应直接写 argv。"
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
                    "如果当前动作只是“按 README 运行某条命令”或“先下载数据再执行”，优先用 readme_command/dataset_step/manual_step；"
                    "不要把尚未存在的 wrapper 脚本名填成 repo_script。"
                    "无效示例：entrypoint.type=repo_script 且 path_or_hint=classification-results-ag-news-only.sh（仓库里并不存在）。"
                    "有效示例：entrypoint.type=repo_script,path_or_hint=classification-results.sh；"
                    "或 entrypoint.type=readme_command,path_or_hint='run classification-results.sh after make'。"
                    "注意：如果该 repo_script 是 shell 脚本，execution_spec 阶段应直接写 argv，如 "
                    "[\"./classification-results.sh\"]，不要把它误翻译成 Python repo_script intent。"
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
                grouped_errors = self._group_run_draft_validation_errors(
                    payload=payload,
                    validation_errors=validation_errors,
                )
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
                        "draft_errors": grouped_errors.get("draft_errors") or [],
                        "global_errors": grouped_errors.get("global_errors") or [],
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
            "mode": {
                "type": "string",
                "enum": ["auto", "full", "chunk", "page", "line_range"],
                "default": "auto",
                "description": "读取模式。full 返回全文；chunk/page/line_range 返回原文分段，不做摘要。",
            },
            "max_chars": {"type": "integer", "default": 20000, "description": "兼容旧调用的字符窗口参数；chunk 模式下会作为默认 chunk_chars。"},
            "chunk_index": {"type": "integer", "description": "chunk 模式下的块序号（1-based）。"},
            "chunk_chars": {"type": "integer", "description": "chunk 模式下每块字符数。"},
            "page": {"type": "integer", "description": "page 模式下的页号（1-based，按行分页）。"},
            "page_size_lines": {"type": "integer", "description": "page 模式下每页多少行。"},
            "line_start": {"type": "integer", "description": "line_range 模式起始行号（1-based）。"},
            "line_end": {"type": "integer", "description": "line_range 模式结束行号（1-based）。"},
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
            mode=str(kwargs.get("mode") or "auto"),
            max_chars=int(kwargs.get("max_chars") or 20000),
            chunk_index=kwargs.get("chunk_index"),
            chunk_chars=kwargs.get("chunk_chars"),
            page=kwargs.get("page"),
            page_size_lines=kwargs.get("page_size_lines"),
            line_start=kwargs.get("line_start"),
            line_end=kwargs.get("line_end"),
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
            "repo_url": {"type": "string", "description": "可选，显式指定要探测的官方仓库 URL；缺省时优先使用当前 Project 的 repo_reference。"},
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
        "如果直接命中文件流，会立即确认；如果返回 HTML 页，会先做页面语义解析并在小范围内继续 resolve 页面中的候选下载/资源链接，再给出最终标记。"
        "Google Drive 的 confirm/virus-scan/download gate 页面应视为“官方链接存活但仍需确认步骤”，不是 dead link；只有明确 not_found/access_denied/quota 等终态才算 blocked。"
        "只读取响应头、极小字节片段和少量 HTML 内容，不执行真实下载。"
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
                "description": "期望拿到的内容类型。对 README/项目页/文档页优先用 html；对数据集、模型权重、压缩包、Google Drive 下载门页这类真实下载链接优先用 file。只有在你明确需要证明特定文件格式时才用 hdf5/zip/json/text；否则不要把 .tar.gz / gzip 这类可下载文件误判成 zip 失败。",
            },
            "read_bytes": {"type": "integer", "default": 64, "description": "最多读取多少字节用于 magic-bytes 判断。范围 8-512；不会下载整个文件。"},
            "resolve_download_gate": {
                "type": "boolean",
                "default": False,
                "description": "仅当探到下载门页/确认页时显式设为 true。开启后会尝试解析 Google Drive confirm/cookie/download-form，把门页继续解析成真实文件流验证；若结果是 gdrive_confirm_required / download_gate / virus-scan warning，应把它当作“链接存活、仍需确认步骤”，而不是 dead link。普通 HTML 参考页会自动做一次内部 resolve，不需要依赖这个开关。",
            },
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
            resolve_download_gate = bool(kwargs.get("resolve_download_gate"))
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
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
                async def _fetch_probe_snapshot(target_url: str, *, allow_gate_resolution: bool) -> Dict[str, Any]:
                    head_status = None
                    get_status = None
                    final_target_url = target_url
                    content_type = ""
                    content_length = None
                    head_bytes = b""
                    request_error = None
                    page_semantics: Optional[Dict[str, Any]] = None
                    html_text = ""

                    try:
                        head_response = await client.head(target_url)
                        head_status = int(head_response.status_code)
                        final_target_url = str(head_response.url)
                        content_type = str(head_response.headers.get("content-type") or "")
                        raw_content_length = str(head_response.headers.get("content-length") or "").strip()
                        content_length = int(raw_content_length) if raw_content_length.isdigit() else None
                    except Exception as exc:  # noqa: BLE001
                        request_error = f"HEAD {type(exc).__name__}: {exc}"

                    try:
                        async with client.stream("GET", target_url, headers={"Range": f"bytes=0-{read_bytes - 1}"}) as response:
                            get_status = int(response.status_code)
                            final_target_url = str(response.url)
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

                    initial_kind = self._classify_magic_bytes(head_bytes, content_type)
                    if is_google_drive_url(final_target_url or target_url) and initial_kind == "html":
                        try:
                            html_response = await client.get(target_url)
                        except Exception as exc:  # noqa: BLE001
                            if request_error:
                                request_error = f"{request_error}; HTML {type(exc).__name__}: {exc}"
                            else:
                                request_error = f"HTML {type(exc).__name__}: {exc}"
                        else:
                            html_text = html_response.text or ""
                            page_semantics = await analyze_html_page_semantics(
                                html_text,
                                url=target_url,
                                final_url=str(html_response.url),
                                content_type=str(html_response.headers.get("content-type") or content_type or ""),
                                source="agent_tools_impl.probe_url_html_semantics",
                            )
                        if allow_gate_resolution:
                            try:
                                confirmed = await probe_google_drive_confirm_download(
                                    client=client,
                                    url=target_url,
                                    read_bytes=read_bytes,
                                )
                            except Exception as exc:  # noqa: BLE001
                                if request_error:
                                    request_error = f"{request_error}; GDRIVE {type(exc).__name__}: {exc}"
                                else:
                                    request_error = f"GDRIVE {type(exc).__name__}: {exc}"
                            else:
                                if confirmed:
                                    get_status = int(confirmed.get("status_code") or get_status or 0) or get_status
                                    final_target_url = str(confirmed.get("final_url") or final_target_url or target_url)
                                    content_type = str(confirmed.get("content_type") or content_type or "")
                                    if confirmed.get("content_length") is not None:
                                        content_length = confirmed.get("content_length")
                                    head_bytes = bytes(confirmed.get("head_bytes") or b"")
                    elif initial_kind == "html":
                        try:
                            html_response = await client.get(target_url)
                        except Exception as exc:  # noqa: BLE001
                            if request_error:
                                request_error = f"{request_error}; HTML {type(exc).__name__}: {exc}"
                            else:
                                request_error = f"HTML {type(exc).__name__}: {exc}"
                        else:
                            html_text = html_response.text or ""
                            page_semantics = await analyze_html_page_semantics(
                                html_text,
                                url=target_url,
                                final_url=str(html_response.url),
                                content_type=str(html_response.headers.get("content-type") or content_type or ""),
                                source="agent_tools_impl.probe_url_html_semantics",
                            )

                    detected_kind = self._classify_magic_bytes(head_bytes, content_type)
                    direct_ok, direct_downloadable, direct_diagnosis, direct_next_action = self._probe_url_diagnosis(
                        status_code=get_status or head_status,
                        content_length=content_length,
                        detected_kind=detected_kind,
                        expected_kind=expected_kind,
                        head_bytes=head_bytes,
                    )
                    if direct_diagnosis == "html_page" and is_google_drive_url(final_target_url or target_url):
                        direct_diagnosis = "gdrive_confirm_required"
                        direct_next_action = (
                            "download_with_confirm_cookie_helper"
                            if allow_gate_resolution
                            else "retry_with_resolve_download_gate"
                        )
                    if (
                        is_google_drive_url(final_target_url or target_url)
                        and direct_diagnosis in {"download_gate", "gdrive_confirm_required", "html_page"}
                        and not allow_gate_resolution
                    ):
                        direct_next_action = "retry_with_resolve_download_gate"

                    return {
                        "target_url": target_url,
                        "head_status": head_status,
                        "status_code": get_status or head_status,
                        "final_url": final_target_url,
                        "content_type": content_type or None,
                        "content_length": content_length,
                        "head_bytes": head_bytes,
                        "request_error": request_error,
                        "page_semantics": page_semantics,
                        "html_text": html_text,
                        "detected_kind": detected_kind,
                        "direct_ok": bool(direct_ok),
                        "direct_downloadable": bool(direct_downloadable),
                        "direct_diagnosis": direct_diagnosis,
                        "direct_next_action": direct_next_action,
                    }

                async def _resolve_probe_target(
                    target_url: str,
                    *,
                    depth: int,
                    visited: Set[str],
                    trace: List[Dict[str, Any]],
                ) -> Dict[str, Any]:
                    snapshot = await _fetch_probe_snapshot(target_url, allow_gate_resolution=resolve_download_gate)
                    current_url = str(snapshot.get("final_url") or target_url or "")
                    visited.add(current_url or target_url)
                    status_code = snapshot.get("status_code")
                    reachable = bool(status_code is not None and int(status_code) < 400)
                    detected_kind = str(snapshot.get("detected_kind") or "")
                    page_semantics = dict(snapshot.get("page_semantics") or {})
                    direct_ok = bool(snapshot.get("direct_ok"))
                    direct_downloadable = bool(snapshot.get("direct_downloadable"))
                    direct_diagnosis = str(snapshot.get("direct_diagnosis") or "")
                    direct_next_action = str(snapshot.get("direct_next_action") or "")

                    trace_entry = {
                        "depth": int(depth),
                        "url": target_url,
                        "final_url": current_url or target_url,
                        "status_code": status_code,
                        "detected_kind": detected_kind,
                        "page_kind": str(page_semantics.get("page_kind") or ""),
                        "diagnosis": direct_diagnosis or str(page_semantics.get("diagnosis") or ""),
                    }

                    if detected_kind != "html":
                        trace_entry["resolution"] = "direct_file_ok" if direct_ok else "direct_probe_failed"
                        trace.append(trace_entry)
                        return {
                            **snapshot,
                            "reachable": reachable,
                            "usable": bool(direct_ok),
                            "ok": bool(direct_ok),
                            "downloadable": bool(direct_downloadable),
                            "direct_file_ok": bool(direct_ok),
                            "diagnosis": direct_diagnosis,
                            "suggested_next_action": direct_next_action,
                            "reason": direct_diagnosis,
                            "resolution_status": "direct_file_ok" if direct_ok else "direct_probe_failed",
                            "resolution_trace": list(trace),
                            "resolved_target_url": current_url or target_url,
                            "resolved_target_kind": detected_kind,
                            "resolved_status_code": status_code,
                            "resolved_content_type": snapshot.get("content_type"),
                            "resolved_content_length": snapshot.get("content_length"),
                            "resolved_downloadable": bool(direct_downloadable),
                            "resolved_via": "direct",
                            "html_resolution": None,
                        }

                    resolution = await resolve_html_probe_plan_with_llm(
                        str(snapshot.get("html_text") or ""),
                        url=target_url,
                        final_url=current_url or target_url,
                        content_type=str(snapshot.get("content_type") or ""),
                        expected_kind=expected_kind,
                        source="agent_tools_impl.probe_url_html_resolver",
                        semantics=page_semantics,
                    )
                    resolution_status = str(resolution.get("resolution") or "").strip().lower() or "blocked"
                    selected_follow_url = str(
                        resolution.get("selected_absolute_url")
                        or urljoin(current_url or target_url, str(resolution.get("selected_href") or ""))
                    ).strip()
                    final_diagnosis = str(resolution.get("diagnosis") or direct_diagnosis or page_semantics.get("diagnosis") or "")
                    final_next_action = str(
                        resolution.get("suggested_next_action")
                        or direct_next_action
                        or page_semantics.get("suggested_next_action")
                        or ""
                    )
                    final_reason = str(resolution.get("reason") or page_semantics.get("rationale") or final_diagnosis or "")

                    trace_entry["resolution"] = resolution_status
                    if selected_follow_url:
                        trace_entry["selected_follow_url"] = selected_follow_url
                    trace.append(trace_entry)

                    if (
                        resolution_status == "follow_link"
                        and selected_follow_url
                        and depth < 2
                        and selected_follow_url not in visited
                    ):
                        child = await _resolve_probe_target(
                            selected_follow_url,
                            depth=depth + 1,
                            visited=visited,
                            trace=trace,
                        )
                        child_ok = bool(child.get("ok"))
                        return {
                            **snapshot,
                            "reachable": reachable,
                            "usable": child_ok,
                            "ok": child_ok,
                            "downloadable": bool(child.get("downloadable")),
                            "direct_file_ok": False,
                            "diagnosis": "followed_link_ok" if child_ok else (str(child.get("diagnosis") or final_diagnosis or "follow_link_failed")),
                            "suggested_next_action": "use_followed_resource" if child_ok else (str(child.get("suggested_next_action") or final_next_action or "")),
                            "reason": str(child.get("reason") or final_reason),
                            "resolution_status": "followed_link_ok" if child_ok else "follow_link_failed",
                            "resolution_trace": list(trace),
                            "resolved_target_url": str(child.get("resolved_target_url") or child.get("final_url") or selected_follow_url),
                            "resolved_target_kind": str(child.get("resolved_target_kind") or child.get("detected_kind") or ""),
                            "resolved_status_code": child.get("resolved_status_code", child.get("status_code")),
                            "resolved_content_type": child.get("resolved_content_type", child.get("content_type")),
                            "resolved_content_length": child.get("resolved_content_length", child.get("content_length")),
                            "resolved_downloadable": bool(child.get("resolved_downloadable", child.get("downloadable"))),
                            "resolved_via": "follow_link",
                            "html_resolution": resolution,
                        }

                    ok = resolution_status == "reference_page_ok"
                    if resolution_status == "follow_link" and selected_follow_url and selected_follow_url in visited:
                        final_diagnosis = "follow_link_cycle"
                        final_next_action = "inspect_before_execute"
                        final_reason = "页面建议继续跟随的链接已访问过，停止循环探测。"
                        resolution_status = "blocked"
                        ok = False
                    elif resolution_status == "follow_link" and selected_follow_url and depth >= 2:
                        final_diagnosis = "follow_depth_exceeded"
                        final_next_action = "inspect_before_execute"
                        final_reason = "HTML 探活已达到内部跳转上限，停止继续跟随。"
                        resolution_status = "blocked"
                        ok = False
                    elif resolution_status not in {"reference_page_ok"}:
                        ok = False

                    return {
                        **snapshot,
                        "reachable": reachable,
                        "usable": bool(ok),
                        "ok": bool(ok),
                        "downloadable": False,
                        "direct_file_ok": False,
                        "diagnosis": final_diagnosis,
                        "suggested_next_action": final_next_action,
                        "reason": final_reason,
                        "resolution_status": resolution_status,
                        "resolution_trace": list(trace),
                        "resolved_target_url": current_url or target_url,
                        "resolved_target_kind": "html",
                        "resolved_status_code": status_code,
                        "resolved_content_type": snapshot.get("content_type"),
                        "resolved_content_length": snapshot.get("content_length"),
                        "resolved_downloadable": False,
                        "resolved_via": "html_resolution",
                        "html_resolution": resolution,
                    }

                resolved = await _resolve_probe_target(url, depth=0, visited=set(), trace=[])

            payload = {
                **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                "project_id": project_id,
                "url": url,
                "expected_kind": expected_kind,
                "resolve_download_gate": resolve_download_gate,
                "head_status": resolved.get("head_status"),
                "status_code": resolved.get("status_code"),
                "final_url": resolved.get("final_url"),
                "content_type": resolved.get("content_type"),
                "content_length": resolved.get("content_length"),
                "reachable": bool(resolved.get("reachable")),
                "usable": bool(resolved.get("usable")),
                "paper_aligned": None,
                "reason": str(resolved.get("reason") or ""),
                "downloadable": bool(resolved.get("downloadable")),
                "direct_downloadable": bool(resolved.get("direct_downloadable")),
                "ok": bool(resolved.get("ok")),
                "direct_file_ok": bool(resolved.get("direct_file_ok")),
                "detected_kind": str(resolved.get("detected_kind") or ""),
                "magic_bytes_hex": bytes(resolved.get("head_bytes") or b"")[:16].hex() or None,
                "magic_bytes_ascii": (
                    bytes(resolved.get("head_bytes") or b"")[:32].decode("utf-8", errors="replace")
                    if resolved.get("head_bytes")
                    else ""
                ),
                "diagnosis": str(resolved.get("diagnosis") or ""),
                "suggested_next_action": str(resolved.get("suggested_next_action") or ""),
                "request_error": resolved.get("request_error"),
                "page_title": str((resolved.get("page_semantics") or {}).get("title") or ""),
                "page_kind": str((resolved.get("page_semantics") or {}).get("page_kind") or ""),
                "page_signals": list((resolved.get("page_semantics") or {}).get("signals") or []),
                "page_text_excerpt": str((resolved.get("page_semantics") or {}).get("text_excerpt") or ""),
                "page_links": list((resolved.get("page_semantics") or {}).get("links") or [])[:6],
                "page_forms": list((resolved.get("page_semantics") or {}).get("forms") or [])[:4],
                "page_semantics_source": str((resolved.get("page_semantics") or {}).get("classification_source") or ""),
                "page_semantics_rationale": str((resolved.get("page_semantics") or {}).get("rationale") or ""),
                "resolution_status": str(resolved.get("resolution_status") or ""),
                "resolved_target_url": str(resolved.get("resolved_target_url") or ""),
                "resolved_target_kind": str(resolved.get("resolved_target_kind") or ""),
                "resolved_status_code": resolved.get("resolved_status_code"),
                "resolved_content_type": resolved.get("resolved_content_type"),
                "resolved_content_length": resolved.get("resolved_content_length"),
                "resolved_downloadable": bool(resolved.get("resolved_downloadable")),
                "resolved_via": str(resolved.get("resolved_via") or ""),
                "resolution_trace": list(resolved.get("resolution_trace") or []),
                "html_resolution": dict(resolved.get("html_resolution") or {}) if isinstance(resolved.get("html_resolution"), dict) else None,
            }
            payload = {
                **payload,
            }
            lines = [
                "已探测外部 URL 存活状态。",
                f"- Project: /projects/{project_id}",
                f"- URL: {url}",
                f"- Status: {payload.get('status_code')}",
                f"- Content-Type: {payload.get('content_type') or 'unknown'}",
                f"- Content-Length: {payload.get('content_length') if payload.get('content_length') is not None else 'unknown'}",
                f"- Reachable: {payload.get('reachable')}",
                f"- Usable: {payload.get('usable')}",
                f"- Detected kind: {payload.get('detected_kind')}",
                f"- Downloadable: {payload['downloadable']}",
                f"- Diagnosis: {payload.get('diagnosis')}",
                f"- Resolution status: {payload.get('resolution_status') or 'unknown'}",
                f"- Resolve download gate: {resolve_download_gate}",
            ]
            if payload.get("page_title"):
                lines.append(f"- Page title: {payload.get('page_title')}")
            if payload.get("page_kind"):
                lines.append(f"- Page kind: {payload.get('page_kind')}")
            if payload.get("page_signals"):
                signals_preview = ", ".join(str(item) for item in list(payload.get("page_signals") or [])[:6])
                if signals_preview:
                    lines.append(f"- Page signals: {signals_preview}")
            if payload.get("page_text_excerpt"):
                lines.append(f"- Page text excerpt: {payload.get('page_text_excerpt')[:300]}")
            if payload.get("resolved_target_url") and payload.get("resolved_target_url") != payload.get("final_url"):
                lines.append(f"- Resolved target URL: {payload.get('resolved_target_url')}")
            if payload.get("resolved_target_kind"):
                lines.append(f"- Resolved target kind: {payload.get('resolved_target_kind')}")
            if payload.get("suggested_next_action"):
                lines.append(f"- Suggested next action: {payload.get('suggested_next_action')}")
            if payload.get("page_semantics_rationale"):
                lines.append(f"- Rationale: {payload.get('page_semantics_rationale')}")
            if payload.get("request_error"):
                lines.append(f"- Request error: {payload.get('request_error')}")
            return ToolResult(
                success=bool(payload.get("ok")),
                output="\n".join(lines),
                data=payload,
                error=None if payload.get("ok") else "url_probe_failed",
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
                    "单次执行计划。优先提供 execution_intent，由 backend 确定性渲染最终 cwd/command；"
                    "不要再自由拼 shell。execution_intent 建议包含 runtime_type/entrypoint_type/"
                    "entrypoint_path/cwd_mode/args。兼容旧格式时也可直接传 runtime_type/cwd/command/"
                    "input_notebook/parameters/expected_outputs/evidence_files/external_dependencies/"
                    "preflight_checks/generated_files。preflight_checks 必须是对象数组，例如 "
                    "[{\"name\":\"check_python\",\"required\":true,\"status\":\"passed\"}]；不要传 "
                    "{\"check_python\": true} 这种 map。generated_files 每项至少需要 content，"
                    "并应显式给出 relative_path；如果误写成 path/file_path/filename/name，writer 会尝试吸收。"
                    "generated_files 只能写入 executions、generated 或 tmp 下的执行级文件，不能覆盖 repo/source。"
                    "execution_intent 与原始 command/cwd/input_notebook 不能混用。"
                    "如果 runtime_type 是 plain-python/dockerfile/docker_compose/repo2docker/devcontainer，"
                    "command 必须是直接 argv 数组，例如 [\"python\",\"train.py\",\"--epochs\",\"5\"] 或 "
                    "[\"./classification-results.sh\"]；"
                    "不要传 [\"bash\",\"-lc\",\"...\"]、here-doc、source venv && python ... 这类 shell wrapper。"
                    "execution_intent.entrypoint_type=repo_script 适用于 Python repo 文件；"
                    "如果真实入口是可执行 shell 脚本，请直接用 argv 调它，不要把 .sh 填成 repo_script。"
                    "如果你确实需要 wrapper 脚本，先调用 paper_research_write_execution_script 把脚本写到 "
                    "executions/{execution_id}/...，再在 execution_spec 里通过 execution_intent 或直接 argv 引用它。"
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
                detail = str(exc)
                guidance = ""
                lowered = detail.lower()
                if "shell wrapper commands are not allowed" in lowered:
                    guidance = (
                        " 合法写法示例：Python repo 文件可用 "
                        "`execution_intent={\"runtime_type\":\"plain-python\",\"entrypoint_type\":\"repo_script\","
                        "\"entrypoint_path\":\"train.py\",\"args\":[\"--epochs\",\"5\"]}`；"
                        "可执行 shell 脚本可直接用 "
                        "`command=[\"./classification-results.sh\"]`。"
                        "只有在你确实需要新 wrapper/辅助程序时，才先调用 "
                        "`paper_research_write_execution_script` 再引用它。"
                    )
                return ToolResult(
                    success=False,
                    output=f"execution_spec 无效，未写入: {detail}.{guidance}",
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


class PaperResearchWriteExecutionScriptTool(_PaperResearchToolBase):
    name = "paper_research_write_execution_script"
    input_model = PaperResearchWriteExecutionScriptInput
    description = (
        "将执行级脚本写入 Project workspace 的 executions/{execution_id}/ 下。"
        "适合生成 tuning variant 脚本或小型辅助程序，不会改动 repo/source。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "目标 execution_id；不是 draft_id。脚本会被约束在 executions/{execution_id}/ 下。"},
            "relative_path": {
                "type": "string",
                "description": (
                    "可选，执行级脚本相对路径。可以只传文件名如 train_variant.py，"
                    "writer 会自动放到 executions/{execution_id}/ 下。禁止写 repo/source。"
                ),
            },
            "content": {
                "type": "string",
                "description": "脚本内容。推荐用于 Python variant 脚本或轻量辅助文件；不能为空。",
            },
        },
        "required": ["project_id", "execution_id", "content"],
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

            workspace_dir = self._workspace_dir_for(workspace)
            try:
                saved = ProjectRuntimeService().write_execution_generated_file(
                    workspace_dir=workspace_dir,
                    execution_id=execution_id,
                    relative_path=kwargs.get("relative_path"),
                    content=str(kwargs.get("content") or ""),
                )
            except ValueError as exc:
                return ToolResult(
                    success=False,
                    output=f"执行级脚本无效，未写入: {exc}",
                    error="execution_script_invalid",
                    data={
                        **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                        "project_id": project_id,
                        "execution_id": execution_id,
                        "saved": False,
                    },
                )

            entrypoint_hint = dict(saved.get("entrypoint_hint") or {})
            lines = [
                "已写入 execution 脚本。",
                f"- Project: /projects/{project_id}",
                f"- Execution ID: {saved.get('execution_id')}",
                f"- Relative path: {saved.get('relative_path')}",
            ]
            if entrypoint_hint:
                lines.append(
                    "- Execution intent hint: "
                    + json.dumps(entrypoint_hint, ensure_ascii=False, sort_keys=True)
                )
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
            "execution_id": {"type": "string", "description": "execution spec ID；不是 draft_id。"},
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
            "execution_id": {"type": "string", "description": "execution spec ID；不是 draft_id。只有已归档 execution_spec 才能启动。"},
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

            workspace_dir = self._workspace_dir_for(workspace)

            service = ProjectRuntimeService()
            spec_content: Dict[str, Any] = {}
            try:
                spec_content = service.read_execution_spec(
                    workspace_dir=workspace_dir,
                    execution_id=execution_id,
                )
            except FileNotFoundError:
                spec_content = {}
            try:
                payload = await service.start_execution(
                    project_id=project_id,
                    workspace_id=int(workspace.id),
                    workspace_dir=workspace_dir,
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
            "execution_id": {"type": "string", "description": "execution ID；不是 draft_id。"},
            "include_logs": {"type": "boolean", "default": True},
            "max_log_chars": {"type": "integer", "default": 20000, "description": "日志最多返回多少字符。范围 0-200000。"},
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


class PaperResearchTailExecutionLogTool(_PaperResearchToolBase):
    name = "paper_research_tail_execution_log"
    input_model = PaperResearchTailExecutionLogInput
    parallel_safe = True
    output_max_tokens = 9000
    description = "快速读取 execution 日志尾部，适合持续观察运行进度或错误。"
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "execution_id": {"type": "string", "description": "execution ID；不是 draft_id。"},
            "max_log_chars": {"type": "integer", "default": 12000, "minimum": 200, "maximum": 200000, "description": "最多返回多少日志字符。"},
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
                include_logs=True,
                max_log_chars=int(kwargs.get("max_log_chars") or 12000),
            )
            result = dict(payload.get("result") or {})
            log_text = str(result.get("log") or "")
            output_lines = [
                "已读取 execution 日志尾部。",
                f"- Project: /projects/{project_id}",
                f"- Execution ID: {payload.get('execution_id') or execution_id}",
                f"- Status: {payload.get('status')}",
                f"- Log exists: {result.get('log_exists')}",
            ]
            if result.get("error"):
                output_lines.append(f"- Error: {result.get('error')}")
            if result.get("message"):
                output_lines.append(f"- Message: {result.get('message')}")
            output_lines.extend(["Log tail:", log_text or "(empty)"])
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    "project_id": project_id,
                    "execution_id": payload.get("execution_id") or execution_id,
                    "status": payload.get("status"),
                    "log_exists": bool(result.get("log_exists")),
                    "log": log_text,
                    "result_exists": bool(result.get("result_exists")),
                    "message": result.get("message"),
                    "error": result.get("error"),
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
            "execution_id": {"type": "string", "description": "execution ID；不是 draft_id。"},
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


for _paper_tool_cls in list(globals().values()):
    if (
        isinstance(_paper_tool_cls, type)
        and issubclass(_paper_tool_cls, _PaperResearchToolBase)
        and _paper_tool_cls is not _PaperResearchToolBase
    ):
        _sync_tool_parameter_constraints(_paper_tool_cls)


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
                        PaperResearchSearchOutputsTool(
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
                        PaperResearchGitStatusTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchGitDiffTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchGitLogTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchGitShowTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchRunAiderTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadAiderRunTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchTailAiderLogTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchAssessRepoMainpathTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchListOutputsTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchDeleteOutputTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchCleanupScopeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteGroundingReportTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchReadGroundingReportTool(
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
                        PaperResearchProbeRepoTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchProbeUrlTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteExecutionSpecTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        PaperResearchWriteExecutionScriptTool(
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
                        PaperResearchTailExecutionLogTool(
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
