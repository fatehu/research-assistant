"""
Agent 工具定义和执行 - 支持共享知识库搜索
"""
import asyncio
import base64
import contextvars
import fnmatch
import httpx
import json
import math
import os
import re
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Callable, Type, Protocol, Mapping, Sequence, Literal
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, or_, and_, tuple_, cast, String as SQLString
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
from app.services.claude_code_collaboration_graph_service import ClaudeCodeCollaborationGraphService
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)
from app.services.html_page_semantics import analyze_html_page_semantics, resolve_html_probe_plan_with_llm
from app.services.google_drive_utils import is_google_drive_url, probe_google_drive_confirm_download
from app.services.llm_service import LLMService
from app.services.smart_chunking.token_utils import estimate_tokens, tokens_to_chars
from app.services.zoekt_cli_service import ZoektCliService


_TOOL_LIVE_EVENT_EMITTER: contextvars.ContextVar[Optional[Callable[[Dict[str, Any]], Any]]] = contextvars.ContextVar(
    "tool_live_event_emitter",
    default=None,
)


def set_tool_live_event_emitter(callback: Optional[Callable[[Dict[str, Any]], Any]]):
    return _TOOL_LIVE_EVENT_EMITTER.set(callback)


def reset_tool_live_event_emitter(token) -> None:
    _TOOL_LIVE_EVENT_EMITTER.reset(token)


async def emit_tool_live_event(payload: Dict[str, Any]) -> None:
    callback = _TOOL_LIVE_EVENT_EMITTER.get()
    if callback is None:
        return
    maybe_awaitable = callback(dict(payload or {}))
    if asyncio.iscoroutine(maybe_awaitable):
        await maybe_awaitable

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


def _normalize_repo_relative_path(value: Any) -> str:
    normalized = _normalize_relative_path(value)
    if normalized == "repo/source":
        return ""
    if normalized.startswith("repo/source/"):
        return normalized.removeprefix("repo/source/")
    if normalized.startswith("paper_repo/"):
        return normalized.removeprefix("paper_repo/")
    return normalized


def _extract_first_json_object(text: Any) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates: List[str] = [raw]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(item for item in fenced if item)
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        candidates.append(raw[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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

    def _resolve_timeout_seconds(self) -> Optional[float]:
        timeout = self.timeout_seconds
        if timeout is None:
            timeout = float(getattr(settings, "tool_default_timeout_seconds", 20))
        timeout_value = float(timeout)
        if timeout_value <= 0:
            return None
        return max(timeout_value, 1.0)

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
                if timeout_seconds is None:
                    result = await maybe_awaitable
                else:
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
                timeout_message = (
                    f"工具执行超时（>{timeout_seconds:.1f}s）"
                    if timeout_seconds is not None
                    else "工具执行超时"
                )
                contract = build_tool_error_contract(
                    code="timeout",
                    message=timeout_message,
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
    """Web 搜索工具 - Tavily -> Serper -> DDGS。"""

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

        if self.tavily_api_key:
            result = await self._safe_provider_call("tavily", self._tavily_search, query, max_results)
            if result.success:
                return result
            errors.append(f"tavily:{result.error or 'failed'}")

        if self.serper_api_key:
            result = await self._safe_provider_call("serper", self._serper_search, query, max_results)
            if result.success:
                return result
            errors.append(f"serper:{result.error or 'failed'}")

        result = await self._ddgs_search(query, max_results)
        if result.success:
            return result
        errors.append(f"ddgs:{result.error or 'failed'}")

        return ToolResult(
            success=False,
            output=f"网络搜索失败，已尝试 Tavily/Serper/DDGS。错误: {'; '.join(errors)}",
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

class PaperResearchSearchProjectZoektInput(BaseModel):
    project_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=800)
    max_results: int = Field(default=20, ge=1, le=100)
    context_lines: int = Field(default=0, ge=0, le=20)
    auto_index: bool = True
    force_reindex: bool = False

class PaperSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


class ProjectTreeInput(BaseModel):
    project_id: int = Field(ge=1)


class ProjectReadFileInput(BaseModel):
    project_id: int = Field(ge=1)
    relative_path: str = Field(min_length=1, max_length=400)


class ProjectWriteFileInput(BaseModel):
    project_id: int = Field(ge=1)
    relative_path: str = Field(min_length=1, max_length=400)
    content: str


class ProjectBashInput(BaseModel):
    project_id: int = Field(ge=1)
    command: str = Field(min_length=1, max_length=20000)


class ProjectClaudeInput(BaseModel):
    project_id: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=20000)
    continue_session: bool = False


class DocxGenerateWithClaudeInput(BaseModel):
    docx_id: Optional[str] = None
    template_id: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact_json: Optional[Dict[str, Any]] = None
    artifact_path: Optional[str] = None
    markdown: Optional[str] = None
    source_path: Optional[str] = None
    requirements: Optional[str] = None
    output_basename: Optional[str] = None
    continue_session: bool = False


class DocxRefineWithClaudeInput(BaseModel):
    docx_id: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=20000)
    target_docx_path: Optional[str] = Field(default=None, max_length=2000)
    output_basename: Optional[str] = None
    continue_session: bool = True


class LiteratureReviewStartInput(BaseModel):
    literature_review_id: Optional[str] = None
    topic: str = Field(min_length=1, max_length=1000)
    target_paper_count: int = Field(default=12, ge=1, le=100)
    notes: Optional[str] = None


class LiteratureReviewDownloadPdfInput(BaseModel):
    literature_review_id: str = Field(min_length=1, max_length=160)
    pdf_url: Optional[str] = Field(default=None, max_length=3000)
    arxiv_id: Optional[str] = Field(default=None, max_length=120)
    title: Optional[str] = Field(default=None, max_length=1000)
    abstract: Optional[str] = Field(default=None, max_length=50000)
    source: Optional[str] = Field(default=None, max_length=120)
    external_id: Optional[str] = Field(default=None, max_length=300)
    doi: Optional[str] = Field(default=None, max_length=300)
    url: Optional[str] = Field(default=None, max_length=3000)
    venue: Optional[str] = Field(default=None, max_length=500)
    year: Optional[int] = None
    authors: Optional[List[Dict[str, Any]]] = None
    citation_count: Optional[int] = None
    reference_count: Optional[int] = None
    fields_of_study: Optional[List[str]] = None
    paper_key: Optional[str] = Field(default=None, max_length=180)
    overwrite: bool = False


class LiteratureReviewReadInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    literature_review_id: str = Field(min_length=1, max_length=160)
    mode: Literal["list", "read"] = "list"
    relative_path: Optional[str] = Field(
        default=None,
        max_length=1200,
        validation_alias=AliasChoices("relative_path", "path"),
    )


class LiteratureReviewSearchZoektInput(BaseModel):
    literature_review_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=800)
    scope: Literal["all", "paper", "review"] = "all"
    max_results: int = Field(default=20, ge=1, le=100)
    context_lines: int = Field(default=2, ge=0, le=20)
    auto_index: bool = True
    force_reindex: bool = False


class LiteratureReviewPdfToMarkdownInput(BaseModel):
    literature_review_id: str = Field(min_length=1, max_length=160)
    paper_key: Optional[str] = Field(default=None, max_length=180)
    pdf_path: Optional[str] = Field(default=None, max_length=2000)
    mode: Literal["fast", "hybrid"] = "fast"


class ReviewWriterInput(BaseModel):
    literature_review_id: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=1000)
    mode: Literal["paper", "final"] = "paper"
    paper_key: Optional[str] = Field(default=None, max_length=180)
    md_path: Optional[str] = Field(default=None, max_length=2000)
    requirements: Optional[str] = None
    target_paper_count: int = Field(default=12, ge=1, le=100)


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
            normalized = _normalize_repo_relative_path(str(item or ""))
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
        normalized = _normalize_repo_relative_path(value)
        return normalized or None


class PaperResearchWriteExecutionSpecInput(BaseModel):
    project_id: int = Field(ge=1)
    execution_spec: Dict[str, Any] = Field(default_factory=dict)


class PaperResearchLaunchClaudeCodeInput(BaseModel):
    project_id: int = Field(ge=1)
    task: str = Field(min_length=1, max_length=20000)
    execution_id: Optional[str] = Field(default=None, max_length=120)
    model: Optional[str] = Field(default=None, max_length=200)
    max_turns: Optional[int] = Field(default=None, ge=1, le=200)
    add_dirs: List[str] = Field(default_factory=list, max_length=20)
    allowed_tools: List[str] = Field(default_factory=list, max_length=50)
    disallowed_tools: List[str] = Field(default_factory=list, max_length=50)
    append_system_prompt: Optional[str] = Field(default=None, max_length=12000)
    permission_mode: Optional[str] = Field(default=None, max_length=100)
    dangerously_skip_permissions: bool = True


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

    async def _load_project_tree_focus_context(
        self,
        db: AsyncSession,
        *,
        project_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        project_goal = str((project_payload or {}).get("goal") or "").strip()
        context: Dict[str, Any] = {
            "project_goal": project_goal,
            "user_goal": "",
            "active_topic": "",
            "current_stage": "",
            "workflow_binding": {},
            "latest_user_message": "",
            "recent_tool_calls": [],
        }
        if self.conversation_id is None:
            return context

        from app.models.conversation import Message, MessageRole
        from app.services.agent_runtime_service import get_agent_runtime_service

        latest_user_result = await db.execute(
            select(Message.content)
            .where(
                Message.conversation_id == int(self.conversation_id),
                Message.role == MessageRole.USER,
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        latest_user_message = latest_user_result.scalar_one_or_none()
        if latest_user_message is not None:
            context["latest_user_message"] = str(latest_user_message or "").strip()

        runtime_service = get_agent_runtime_service()
        state = dict(await runtime_service.get_conversation_context_state(int(self.conversation_id)) or {})
        workflow_binding = (
            dict(state.get("workflow_binding") or {})
            if isinstance(state.get("workflow_binding"), dict)
            else {}
        )
        context["user_goal"] = str(state.get("user_goal") or "").strip()
        context["active_topic"] = str(state.get("active_topic") or "").strip()
        context["current_stage"] = str(workflow_binding.get("current_stage") or "").strip()
        context["workflow_binding"] = workflow_binding

        tool_ledger_payload = dict(await runtime_service.get_conversation_tool_ledger(int(self.conversation_id)) or {})
        raw_entries = [
            dict(item)
            for item in list(tool_ledger_payload.get("entries") or [])
            if isinstance(item, dict)
        ]
        recent_calls: List[Dict[str, Any]] = []
        for item in reversed(raw_entries):
            if str(item.get("kind") or "").strip() != "tool_call":
                continue
            tool_name = str(item.get("tool_name") or "").strip()
            if not tool_name:
                continue
            recent_calls.append(
                {
                    "tool_name": tool_name,
                    "arguments": dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), dict) else {},
                    "status": str(item.get("status") or "").strip() or None,
                    "iteration": int(item.get("iteration") or 0),
                }
            )
            if len(recent_calls) >= 8:
                break
        context["recent_tool_calls"] = list(reversed(recent_calls))
        return context

    @classmethod
    async def _summarize_project_tree_for_agent(
        cls,
        *,
        tree: str,
        focus_context: Dict[str, Any],
    ) -> tuple[str, List[str]]:
        normalized_tree = str(tree or "").strip()
        if not normalized_tree:
            return "", []

        provider = str(getattr(settings, "agent_budget_compression_provider", "aliyun") or "aliyun").strip()
        llm = LLMService(provider)
        llm.config = dict(llm.config)
        llm.config["model"] = "qwen-turbo"
        timeout_seconds = max(
            float(getattr(settings, "agent_budget_compression_timeout_seconds", 8.0) or 8.0),
            0.1,
        )
        try:
            response = await asyncio.wait_for(
                llm.chat(
                    messages=[
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "agent_focus": focus_context,
                                    "project_tree": normalized_tree,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    system_prompt=(
                        "你是项目目录树整理器。"
                        "给定当前 agent 的目标、最近工具调用和完整 project tree，"
                        "请挑出当前最值得继续探索的目录层级和文件。"
                        "不要编造任何不存在的路径；只能使用输入 tree 中已有的路径。"
                        "focused_tree 必须保留层级结构，使用纯文本目录树，根节点用 `.`。"
                        "important_paths 必须是 project 根相对路径列表。"
                        "返回严格 JSON："
                        "{\"focused_tree\":\"...\",\"important_paths\":[\"...\"]}"
                    ),
                    temperature=0.1,
                    max_tokens=1200,
                    source="project_tree.focused_tree",
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"[ProjectTree] focused tree summarization failed: {exc}")
            return "", []

        payload = _extract_first_json_object(response.get("content", ""))
        if not isinstance(payload, dict):
            return "", []

        focused_tree = str(payload.get("focused_tree") or "").strip()
        important_paths = [
            _normalize_relative_path(item)
            for item in list(payload.get("important_paths") or [])
            if _normalize_relative_path(item)
        ]
        return focused_tree, list(dict.fromkeys(important_paths))

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
    def _paper_author_names(paper: Any) -> List[str]:
        if hasattr(paper, "author_names"):
            try:
                return [str(item).strip() for item in list(getattr(paper, "author_names") or []) if str(item).strip()]
            except Exception:
                pass
        authors = list(getattr(paper, "authors", []) or [])
        names: List[str] = []
        for item in authors:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _normalize_search_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _score_saved_paper_candidate(cls, *, paper: Any, query: str) -> int:
        query_norm = cls._normalize_search_text(query)
        if not query_norm:
            return 0

        title = cls._normalize_search_text(getattr(paper, "title", ""))
        abstract = cls._normalize_search_text(getattr(paper, "abstract", ""))
        venue = cls._normalize_search_text(getattr(paper, "venue", "") or getattr(paper, "journal", ""))
        arxiv_id = cls._normalize_search_text(getattr(paper, "arxiv_id", ""))
        authors_text = cls._normalize_search_text(" ".join(cls._paper_author_names(paper)))
        query_terms = [item for item in query_norm.split() if item]

        score = 0
        if query_norm == arxiv_id and arxiv_id:
            score += 220
        if query_norm == title and title:
            score += 200
        if query_norm in title and title:
            score += 120
        if query_norm in authors_text and authors_text:
            score += 80
        if query_norm in venue and venue:
            score += 40
        if query_norm in abstract and abstract:
            score += 25

        if query_terms and all(term in title for term in query_terms if title):
            score += 40
        if query_terms and all(term in authors_text for term in query_terms if authors_text):
            score += 25

        for term in query_terms:
            if term in title and title:
                score += 18
            if term in authors_text and authors_text:
                score += 12
            if term in venue and venue:
                score += 8
            if term in abstract and abstract:
                score += 4

        return score

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
    def _root_descriptor(cls, *, project_payload: Dict[str, Any], workspace: Any = None) -> Dict[str, Any]:
        descriptor = {
            "project_id": int(project_payload.get("id") or 0),
            "root_alias": cls._PROJECT_ROOT_ALIAS if workspace is not None else "project_root",
            "root_relative_prefix": ".",
        }
        if workspace is not None:
            descriptor["workspace_id"] = int(getattr(workspace, "id", 0) or 0)
            descriptor["notebook_id"] = str(getattr(workspace, "notebook_id", "") or "")
        return descriptor

    async def _resolve_project_payload_only(
        self,
        db: AsyncSession,
        *,
        project_id: int,
    ) -> Optional[Dict[str, Any]]:
        from app.services.project_service import ProjectService

        project_payload = await ProjectService(db).get_project_payload(project_id=int(project_id), user_id=self.user_id)
        return dict(project_payload) if isinstance(project_payload, dict) else None

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

    @staticmethod
    def _project_dir_for(project_id: int) -> Path:
        from app.services.project_paths import get_project_root_dir

        return get_project_root_dir(int(project_id))

    @classmethod
    def _resolve_project_path(
        cls,
        project_dir: Path,
        relative_path: Any,
        *,
        require_exists: bool = True,
    ) -> Optional[Path]:
        normalized = cls._normalize_relative_path(relative_path)
        if not normalized:
            return None
        candidate = Path(project_dir) / normalized
        try:
            resolved = candidate.resolve()
            root = Path(project_dir).resolve()
        except OSError:
            return None
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        if require_exists and not resolved.exists():
            return None
        return resolved

    @classmethod
    def _render_project_tree(cls, project_dir: Path) -> str:
        root = Path(project_dir)
        if not root.exists():
            return "."

        lines = ["."]

        def _walk(current_dir: Path, prefix: str) -> None:
            try:
                entries = sorted(
                    list(current_dir.iterdir()),
                    key=lambda item: (not item.is_dir(), str(item.name).lower(), str(item.name)),
                )
            except OSError:
                lines.append(f"{prefix}`-- [unreadable]")
                return

            for index, entry in enumerate(entries):
                is_last = index == len(entries) - 1
                branch = "`-- " if is_last else "|-- "
                label = f"{entry.name}/" if entry.is_dir() and not entry.is_symlink() else entry.name
                lines.append(f"{prefix}{branch}{label}")
                if entry.is_dir() and not entry.is_symlink():
                    _walk(entry, prefix + ("    " if is_last else "|   "))

        _walk(root, "")
        return "\n".join(lines)

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
                output="当前 Project 还没有可用的 repo/source。请先调用 paper_research_prepare。",
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
            repo_dir = workspace_dir / "repo" / "source"
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
    def _build_match_context(
        cls,
        *,
        root_dir: Path,
        relative_path: str,
        line_number: int,
        context_lines: int,
        max_chars: int = 800,
    ) -> Dict[str, Any]:
        if context_lines <= 0:
            return {}
        normalized_relative_path = _normalize_relative_path(relative_path)
        if not normalized_relative_path:
            return {}
        file_path = Path(root_dir) / normalized_relative_path
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
    def _build_repo_match_context(
        cls,
        *,
        repo_dir: Path,
        repo_relative_path: str,
        line_number: int,
        context_lines: int,
        max_chars: int = 800,
    ) -> Dict[str, Any]:
        return cls._build_match_context(
            root_dir=repo_dir,
            relative_path=repo_relative_path,
            line_number=line_number,
            context_lines=context_lines,
            max_chars=max_chars,
        )

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
        project_repo_dir = workspace_dir / "repo" / "source"
        if project_repo_dir.is_dir() and project_repo_dir != repo_dir:
            for path in project_repo_dir.rglob("*"):
                if path.is_file():
                    try:
                        relative_path = str(path.relative_to(project_repo_dir)).replace("\\", "/")
                        if any(part in _REPO_SKIPPED_DIRS for part in relative_path.split("/")):
                            continue
                        files.add(relative_path)
                    except ValueError:
                        continue
        return files

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
        "论文复现的 prepare 步骤。根据已保存的论文创建或复用 Project，"
        "并在 `/app/uploads/projects/{project_id}/reference/` 下生成 reference bundle。"
        "如果用户给的是内部 `paper_id`，优先直接传给这个工具；不要把 `paper_id` 直接当成 `project_id` 传给 `project_tree`、`project_read_file`、`project_bash`。"
        "主要用途是首次准备项目，或在论文、README、reference 明显过期时显式刷新。"
        "它会生成完整论文 markdown、论文解读和 README intake，不会执行训练，也不会启动旧 workspace/notebook 流程。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "integer", "description": "已保存论文的内部 paper_id。已知 paper_id 时优先直接传它。"},
            "paper_title": {"type": "string", "description": "没有 paper_id 时，用已保存论文标题做精确匹配。"},
            "project_id": {"type": "integer", "description": "已有 Project ID。传了它就优先在这个 Project 下准备 reference。"},
            "project_title": {"type": "string", "description": "需要新建 Project 时使用的标题；通常可以省略。"},
            "user_goal": {"type": "string", "description": "当前复现目标、调优目标或验证目标。会写入 Project 元信息。"},
            "create_project": {"type": "boolean", "default": True, "description": "没有匹配 Project 时是否自动创建。通常保持 true。"},
            "create_workspace": {"type": "boolean", "default": True, "description": "历史兼容字段，当前 project-only 流程会忽略它；不要依赖它。"},
            "refresh_intake": {
                "type": "boolean",
                "default": False,
                "description": "是否强制重建 `project/reference/` 下的 reference bundle。只有在 reference 明显过期或想覆盖旧结果时再设 true。",
            },
        },
        "required": [],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService
            from app.services.project_reference_builder_service import ProjectReferenceBuilderService

            paper = await self._resolve_paper(
                db,
                paper_id=kwargs.get("paper_id"),
                paper_title=kwargs.get("paper_title"),
            )
            if paper is None:
                return self._paper_not_found(kwargs.get("paper_id"), kwargs.get("paper_title"))

            project_service = ProjectService(db)
            project_payload = await self._resolve_project_payload(
                project_service,
                paper=paper,
                project_id=kwargs.get("project_id"),
                project_title=kwargs.get("project_title"),
                user_goal=kwargs.get("user_goal"),
                create_project=bool(kwargs.get("create_project", True)),
            )
            if project_payload is None:
                return ToolResult(
                    success=False,
                    output="没有可用 Project。请提供已有 project_id，或允许 create_project=true。",
                    error="project_not_found",
                    data={"paper_id": int(paper.id)},
                )

            builder = ProjectReferenceBuilderService(db)
            builder_summary = await builder.build(
                paper=paper,
                project_id=int(project_payload["id"]),
                user_id=self.user_id,
                refresh=bool(kwargs.get("refresh_intake", False)),
            )
            payload = {
                "workflow": "paper-reproduction",
                "action": "prepare",
                "paper": {
                    "id": int(paper.id),
                    "title": str(paper.title or ""),
                    "year": paper.year,
                    "venue": paper.venue,
                    "arxiv_id": paper.arxiv_id,
                },
                "project": self._project_payload(project_payload),
                "reference_builder": builder_summary,
            }
            lines = [
                "Project reference builder 完成。",
                f"- 论文: {payload['paper']['title']} (paper_id={payload['paper']['id']})",
                f"- Project: /projects/{int(project_payload['id'])}",
                f"- Project root: {builder_summary.get('project_root')}",
                f"- Reference root: {builder_summary.get('reference_root')}",
                f"- Reference ready: {bool(builder_summary.get('reference_ready'))}",
                "- Files:",
                *[
                    f"  - {relative_path}"
                    for relative_path in list(builder_summary.get("reference_files") or [])
                ],
            ]
            repo_materialization = dict(dict(builder_summary.get("repo_reference") or {}).get("repo_materialization") or {})
            if repo_materialization.get("status"):
                lines.append(f"- Repo status: {repo_materialization.get('status')}")
            return ToolResult(
                success=bool(builder_summary.get("reference_ready")),
                output="\n".join(lines),
                data=payload,
            )

        return await self._with_db(_handler)


class PaperResearchStatusTool(_PaperResearchToolBase):
    name = "paper_research_status"
    input_model = PaperResearchStatusInput
    description = (
        "读取当前论文复现 Project 的 reference 状态。"
        "它会根据 `project_id` 或 `paper_id` 定位 Project，然后只读检查 `project/reference/` 是否齐全、有哪些归档文件。"
        "当用户只给了 `paper_id`，优先先用这个工具，把 `paper_id` 解析成 Project，再继续调用 `project_tree`、`project_read_file`、`project_bash`。"
        "这个工具不创建 Project，不刷新 reference，也不执行训练。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "integer", "description": "已保存论文的内部 paper_id。已知 paper_id 时可直接用它定位对应 Project。"},
            "paper_title": {"type": "string", "description": "没有 paper_id 时，用已保存论文标题做精确匹配。"},
            "project_id": {"type": "integer", "description": "已有 Project ID。已知 project_id 时优先传它。"},
        },
        "required": [],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_service import ProjectService
            from app.services.project_paths import get_project_root_dir
            from app.services.project_reference_builder_service import ProjectReferenceBuilderService

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
            if project_payload is None:
                return ToolResult(
                    success=False,
                    output="没有可用 Project。",
                    error="project_not_found",
                    data={"paper_id": int(paper.id)},
                )

            project_id = int(project_payload["id"])
            project_root = get_project_root_dir(project_id, ensure_exists=False)
            builder = ProjectReferenceBuilderService(db)
            reference_ready = builder.reference_bundle_ready(project_root)
            reference_root = project_root / "reference"
            reference_files = [
                relative_path
                for relative_path in ProjectReferenceBuilderService.required_reference_relative_paths()
                if (project_root / relative_path).is_file()
            ]
            payload = {
                "workflow": "paper-reproduction",
                "action": "status",
                "paper": {
                    "id": int(paper.id),
                    "title": str(paper.title or ""),
                    "year": paper.year,
                    "venue": paper.venue,
                    "arxiv_id": paper.arxiv_id,
                },
                "project": self._project_payload(project_payload),
                "project_root": str(project_root),
                "reference_root": str(reference_root),
                "reference_ready": bool(reference_ready),
                "reference_files": reference_files,
            }
            lines = [
                "已读取 Project reference 状态。",
                f"- 论文: {payload['paper']['title']} (paper_id={payload['paper']['id']})",
                f"- Project: /projects/{project_id}",
                f"- Project root: {project_root}",
                f"- Reference root: {reference_root}",
                f"- Reference ready: {bool(reference_ready)}",
                "- Files:",
                *[f"  - {relative_path}" for relative_path in reference_files],
            ]
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data=payload,
            )

        return await self._with_db(_handler)


class PaperSearchTool(_PaperResearchToolBase):
    name = "paper_search"
    input_model = PaperSearchInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "搜索当前用户已保存的论文候选，用来确定 `paper_id`。"
        "适合输入论文标题、简称、作者名、研究主题或自然语言描述。"
        "它不是按内部 ID 精确查找的工具；如果你已经知道 `paper_id`，应直接传给 `paper_research_prepare` 或 `paper_research_status`，不要把 `113` 这种内部 ID 当搜索词。"
        "结果只返回识别论文所需的最小字段：paper_id、title、abstract、authors、year、venue。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "论文标题、简称、作者名、关键词或自然语言描述；不要传内部 paper_id 数字当搜索词。"},
            "max_results": {"type": "integer", "default": 5, "description": "最多返回多少个候选，范围 1-10。候选用于确认 paper_id，不是最终执行步骤。"},
        },
        "required": ["query"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.models.literature import Paper

            query = str(kwargs.get("query") or "").strip()
            max_results = int(kwargs.get("max_results") or 5)
            pattern = f"%{query[:180]}%"
            search_limit = max(20, min(120, max_results * 12))

            stmt = (
                select(Paper)
                .where(Paper.user_id == self.user_id)
                .where(
                    or_(
                        Paper.title.ilike(pattern),
                        Paper.abstract.ilike(pattern),
                        Paper.venue.ilike(pattern),
                        Paper.arxiv_id.ilike(pattern),
                        cast(Paper.authors, SQLString).ilike(pattern),
                    )
                )
                .order_by(Paper.updated_at.desc(), Paper.id.desc())
                .limit(search_limit)
            )
            result = await db.execute(stmt)
            matched_rows = list(result.scalars().all())

            if len(matched_rows) < max_results:
                fallback_stmt = (
                    select(Paper)
                    .where(Paper.user_id == self.user_id)
                    .order_by(Paper.updated_at.desc(), Paper.id.desc())
                    .limit(max(50, min(300, max_results * 30)))
                )
                fallback_result = await db.execute(fallback_stmt)
                for paper in list(fallback_result.scalars().all()):
                    if any(int(getattr(existing, "id", 0) or 0) == int(getattr(paper, "id", 0) or 0) for existing in matched_rows):
                        continue
                    matched_rows.append(paper)

            ranked: List[Dict[str, Any]] = []
            for paper in matched_rows:
                score = self._score_saved_paper_candidate(paper=paper, query=query)
                if score <= 0:
                    continue
                ranked.append(
                    {
                        "score": score,
                        "paper_id": int(getattr(paper, "id", 0) or 0),
                        "title": str(getattr(paper, "title", "") or ""),
                        "abstract": getattr(paper, "abstract", None),
                        "authors": self._paper_author_names(paper),
                        "year": getattr(paper, "year", None),
                        "venue": getattr(paper, "venue", None) or getattr(paper, "journal", None),
                    }
                )

            ranked.sort(
                key=lambda item: (
                    -int(item.get("score") or 0),
                    str(item.get("title") or "").lower(),
                    int(item.get("paper_id") or 0),
                )
            )
            candidates = [
                {
                    "paper_id": int(item["paper_id"]),
                    "title": str(item["title"] or ""),
                    "abstract": item.get("abstract"),
                    "authors": list(item.get("authors") or []),
                    "year": item.get("year"),
                    "venue": item.get("venue"),
                }
                for item in ranked[:max_results]
            ]

            if not candidates:
                return ToolResult(
                    success=True,
                    output=f"没有找到与 `{query}` 匹配的已保存论文。",
                    data={"query": query, "candidates": []},
                )

            lines = [
                f"已找到 {len(candidates)} 个论文候选。",
                f"- Query: {query}",
                "- Candidates:",
            ]
            for item in candidates:
                author_text = ", ".join(list(item.get("authors") or [])[:4]) or "unknown"
                year_text = item.get("year") if item.get("year") is not None else "unknown"
                venue_text = str(item.get("venue") or "").strip() or "unknown"
                lines.append(
                    f"- paper_id={item['paper_id']} | {item['title']} | {author_text} | {year_text} | {venue_text}"
                )
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    "query": query,
                    "candidates": candidates,
                },
            )

        return await self._with_db(_handler)


_PROJECT_TOOL_SCOPE_DESCRIPTION = (
    "适用范围：Project 工具只用于论文复现、代码优化、代码编写等 Project 工作区任务；"
    "Project 根目录固定为 `/app/uploads/projects/{project_id}`。"
    "不要用于 DOCX 生成、文献综述工作区、模板管理、普通文件下载/查看，"
    "也不要作为 Claude/docx 工具失败后的 fallback。"
)


class ProjectTreeTool(_PaperResearchToolBase):
    name = "project_tree"
    input_model = ProjectTreeInput
    parallel_safe = True
    output_max_tokens = 32000
    description = (
        _PROJECT_TOOL_SCOPE_DESCRIPTION +
        "返回指定 Project 根目录的目录树，用来浏览项目结构、确认文件位置和发现可读文件。"
        "根目录就是 `/app/uploads/projects/{project_id}`。"
        "`project_id` 只能传 Project ID，不能传论文 `paper_id`。"
        "如果现在只知道 `paper_id`，先调用 `paper_research_status(paper_id=...)` 或 `paper_research_prepare(paper_id=...)` 去解析对应 Project。"
        "输出中的路径都按 Project 根目录的相对路径展示。"
        "这个工具会保留完整目录树原文；同时会结合当前 agent 目标和最近工具调用，额外给出一份更聚焦的树整理和重要文件路径。"
        "它只显示结构，不显示文件内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID。用于查看 `/app/uploads/projects/{project_id}` 的目录结构。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            project_dir = self._project_dir_for(project_id)
            tree = self._render_project_tree(project_dir)
            focus_context = await self._load_project_tree_focus_context(
                db,
                project_payload=project_payload,
            )
            focused_tree, important_paths = await self._summarize_project_tree_for_agent(
                tree=tree,
                focus_context=focus_context,
            )
            lines = [
                "已生成 Project 目录树。",
                f"- Project: /projects/{project_id}",
            ]
            if focused_tree:
                lines.extend(
                    [
                        "Focused tree:",
                        focused_tree,
                    ]
                )
            if important_paths:
                lines.extend(
                    [
                        "Important paths:",
                        *[f"- {path}" for path in important_paths],
                    ]
                )
            lines.extend(
                [
                    "Tree:",
                    tree,
                ]
            )
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    "project_id": project_id,
                    "tree": tree,
                    "focused_tree": focused_tree,
                    "important_paths": important_paths,
                },
            )

        return await self._with_db(_handler)


class ProjectReadFileTool(_PaperResearchToolBase):
    name = "project_read_file"
    input_model = ProjectReadFileInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        _PROJECT_TOOL_SCOPE_DESCRIPTION +
        "读取 Project 根目录中的单个文件，并返回完整文件内容。"
        "`relative_path` 必须是相对于 `/app/uploads/projects/{project_id}` 的相对路径。"
        "`project_id` 只能传 Project ID，不能传论文 `paper_id`。"
        "如果只知道 `paper_id`，先用 `paper_research_status` 或 `paper_research_prepare` 解析出 Project。"
        "这个工具适合在已经知道文件路径时读取完整内容；如果还不知道文件在哪，先用 `project_tree` 或 `paper_research_search_project_zoekt`。"
        "它只允许读取 Project 根目录内的文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID。"},
            "relative_path": {"type": "string", "description": "相对于 Project 根目录的文件路径，例如 `reference/paper/paper_interpretation.md`。"},
        },
        "required": ["project_id", "relative_path"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            relative_path = self._normalize_relative_path(kwargs.get("relative_path"))
            if not relative_path:
                return ToolResult(
                    success=False,
                    output="relative_path 无效。",
                    error="invalid_relative_path",
                    data={"project_id": project_id, "relative_path": str(kwargs.get("relative_path") or "")},
                )

            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            project_dir = self._project_dir_for(project_id)
            target = self._resolve_project_path(project_dir, relative_path, require_exists=False)
            if target is None:
                return ToolResult(
                    success=False,
                    output=f"不允许读取 Project 根目录之外的路径: `{relative_path}`。",
                    error="project_path_out_of_scope",
                    data={"project_id": project_id, "relative_path": relative_path},
                )
            if not target.exists():
                return ToolResult(
                    success=False,
                    output=f"Project 文件不存在: `{relative_path}`。",
                    error="project_file_not_found",
                    data={"project_id": project_id, "relative_path": relative_path},
                )
            if not target.is_file():
                return ToolResult(
                    success=False,
                    output=f"目标不是文件，不能读取: `{relative_path}`。",
                    error="project_path_not_file",
                    data={"project_id": project_id, "relative_path": relative_path},
                )

            content = target.read_text(encoding="utf-8", errors="replace")
            return ToolResult(
                success=True,
                output="\n".join(
                    [
                        f"已读取 Project 文件: {relative_path}",
                        "Content:",
                        content,
                    ]
                ),
                data={
                    "project_id": project_id,
                    "relative_path": relative_path,
                    "content": content,
                },
            )

        return await self._with_db(_handler)


class ProjectWriteFileTool(_PaperResearchToolBase):
    name = "project_write_file"
    input_model = ProjectWriteFileInput
    output_max_tokens = 9000
    description = (
        _PROJECT_TOOL_SCOPE_DESCRIPTION +
        "把完整内容写入 Project 根目录中的单个文件。"
        "`relative_path` 必须是相对于 `/app/uploads/projects/{project_id}` 的相对路径。"
        "`project_id` 只能传 Project ID，不能传论文 `paper_id`。"
        "如果只知道 `paper_id`，先用 `paper_research_status` 或 `paper_research_prepare` 解析出 Project。"
        "`content` 会作为该文件的完整最终内容写入；这是整文件覆盖，不是追加写入。"
        "如果父目录不存在，会自动创建。它只允许写入 Project 根目录内的文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID。"},
            "relative_path": {"type": "string", "description": "相对于 Project 根目录的文件路径，例如 `notes/summary.md`。"},
            "content": {"type": "string", "description": "要写入文件的完整最终内容。调用前应假设它会覆盖旧文件。"},
        },
        "required": ["project_id", "relative_path", "content"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            relative_path = self._normalize_relative_path(kwargs.get("relative_path"))
            if not relative_path:
                return ToolResult(
                    success=False,
                    output="relative_path 无效。",
                    error="invalid_relative_path",
                    data={"project_id": project_id, "relative_path": str(kwargs.get("relative_path") or "")},
                )

            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            project_dir = self._project_dir_for(project_id)
            target = self._resolve_project_path(project_dir, relative_path, require_exists=False)
            if target is None:
                return ToolResult(
                    success=False,
                    output=f"不允许写入 Project 根目录之外的路径: `{relative_path}`。",
                    error="project_path_out_of_scope",
                    data={"project_id": project_id, "relative_path": relative_path},
                )
            if target.exists() and not target.is_file():
                return ToolResult(
                    success=False,
                    output=f"目标不是文件，不能写入: `{relative_path}`。",
                    error="project_path_not_file",
                    data={"project_id": project_id, "relative_path": relative_path},
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(kwargs.get("content") or ""), encoding="utf-8")
            return ToolResult(
                success=True,
                output="\n".join(
                    [
                        "已写入 Project 文件。",
                        f"- Project: /projects/{project_id}",
                        f"- Relative path: {relative_path}",
                    ]
                ),
                data={
                    "project_id": project_id,
                    "relative_path": relative_path,
                    "written": True,
                },
            )

        return await self._with_db(_handler)


class ProjectBashTool(_PaperResearchToolBase):
    name = "project_bash"
    input_model = ProjectBashInput
    output_max_tokens = 9000
    description = (
        _PROJECT_TOOL_SCOPE_DESCRIPTION +
        "在 Project 根目录里执行一条 bash 命令。"
        "工作目录固定为 `/app/uploads/projects/{project_id}`。"
        "`project_id` 只能传 Project ID，不能传论文 `paper_id`。"
        "如果只知道 `paper_id`，先用 `paper_research_status` 或 `paper_research_prepare` 解析出 Project。"
        "这个工具只适合在论文复现/代码优化 Project 里运行必要 shell 命令、检查代码、调用 CLI 或做最小脚本操作。"
        "它不会切到 Project 根目录之外执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID。命令会在 `/app/uploads/projects/{project_id}` 下执行。"},
            "command": {"type": "string", "description": "要执行的完整 bash 命令。"},
        },
        "required": ["project_id", "command"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeWorkerClient

            project_id = int(kwargs["project_id"])
            command = str(kwargs.get("command") or "").strip()
            if not command:
                return ToolResult(
                    success=False,
                    output="command 不能为空。",
                    error="invalid_command",
                    data={"project_id": project_id, "command": ""},
                )

            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            if not shutil.which("bash"):
                return ToolResult(
                    success=False,
                    output="当前环境没有可用的 bash。",
                    error="bash_not_available",
                    data={"project_id": project_id, "command": command},
                )

            project_dir = self._project_dir_for(project_id)
            if ProjectRuntimeWorkerClient.enabled():
                try:
                    worker_payload = await ProjectRuntimeWorkerClient().bash(
                        project_id=project_id,
                        workspace_dir=project_dir,
                        command=command,
                    )
                except Exception as exc:
                    return ToolResult(
                        success=False,
                        output="\n".join(
                            [
                                "Project bash 调用 runtime-worker 失败。",
                                f"- Project: /projects/{project_id}",
                                f"- Command: {command}",
                                f"- Error: {type(exc).__name__}: {exc}",
                            ]
                        ),
                        error="project_bash_worker_failed",
                        data={
                            "project_id": project_id,
                            "command": command,
                            "cwd": str(project_dir),
                        },
                    )
                return ToolResult(
                    success=bool(worker_payload.get("success")),
                    output="\n".join(
                        [
                            "已通过 runtime-worker 执行 Project bash 命令。",
                            f"- Project: /projects/{project_id}",
                            f"- Command: {command}",
                            f"- Exit code: {worker_payload.get('exit_code')}",
                            "Stdout:",
                            str(worker_payload.get("stdout") or "(empty)"),
                            "Stderr:",
                            str(worker_payload.get("stderr") or "(empty)"),
                        ]
                    ),
                    error=str(worker_payload.get("error") or "") or None,
                    data={
                        "project_id": project_id,
                        "command": command,
                        "cwd": str(project_dir),
                        "exit_code": worker_payload.get("exit_code"),
                        "stdout": str(worker_payload.get("stdout") or ""),
                        "stderr": str(worker_payload.get("stderr") or ""),
                        "worker": str(worker_payload.get("worker") or "runtime-worker"),
                    },
                )
            try:
                process = await asyncio.create_subprocess_exec(
                    "bash",
                    "-lc",
                    command,
                    cwd=str(project_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=120.0)
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                return ToolResult(
                    success=False,
                    output="\n".join(
                        [
                            "Project bash 执行超时。",
                            f"- Project: /projects/{project_id}",
                            f"- Command: {command}",
                            "Stdout:",
                            stdout or "(empty)",
                            "Stderr:",
                            stderr or "(empty)",
                        ]
                    ),
                    error="project_bash_timeout",
                    data={
                        "project_id": project_id,
                        "command": command,
                        "cwd": str(project_dir),
                        "exit_code": None,
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = int(process.returncode or 0)
            return ToolResult(
                success=exit_code == 0,
                output="\n".join(
                    [
                        "已执行 Project bash 命令。",
                        f"- Project: /projects/{project_id}",
                        f"- Command: {command}",
                        f"- Exit code: {exit_code}",
                        "Stdout:",
                        stdout or "(empty)",
                        "Stderr:",
                        stderr or "(empty)",
                    ]
                ),
                error=None if exit_code == 0 else "project_bash_failed",
                data={
                    "project_id": project_id,
                    "command": command,
                    "cwd": str(project_dir),
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

        return await self._with_db(_handler)


class ProjectClaudeTool(_PaperResearchToolBase):
    name = "project_claude"
    input_model = ProjectClaudeInput
    timeout_seconds = 0.0
    output_max_tokens = 9000
    description = (
        _PROJECT_TOOL_SCOPE_DESCRIPTION +
        "在 runtime-worker 里调用 Claude Code，让它在当前 Project 根目录工作。"
        "它会把 `/app/uploads/projects/{project_id}` 作为 Claude 的当前目录，并返回 Claude 的 session_id 和文本结果。"
        "如果当前 Project 目录已有 Claude session，就自动复用；没有就自动新建。"
        "通常只需要传 prompt。"
        "它用于论文复现、代码优化、代码编写项目，不用于 DOCX 生成或文献综述生成。"
        "这个工具只负责和 Claude 交互，不直接执行 bash。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID。Claude 会在 `/app/uploads/projects/{project_id}` 目录下工作。"},
            "prompt": {"type": "string", "description": "发给 Claude Code 的完整提示。"},
            "continue_session": {"type": "boolean", "default": False, "description": "可选强制继续开关。通常不用传；工具会自动在当前 Project 目录已有 session 时复用。"},
        },
        "required": ["project_id", "prompt"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.project_runtime_service import ProjectRuntimeWorkerClient

            project_id = int(kwargs["project_id"])
            prompt = str(kwargs.get("prompt") or "").strip()
            continue_session = bool(kwargs.get("continue_session"))
            if not prompt:
                return ToolResult(
                    success=False,
                    output="prompt 不能为空。",
                    error="invalid_prompt",
                    data={"project_id": project_id, "prompt": "", "continue_session": continue_session},
                )

            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            if not ProjectRuntimeWorkerClient.enabled():
                return ToolResult(
                    success=False,
                    output="runtime-worker 未启用，当前不能调用 Claude Code。",
                    error="runtime_worker_disabled",
                    data={"project_id": project_id, "prompt": prompt, "continue_session": continue_session},
                )

            project_dir = self._project_dir_for(project_id)
            try:
                live_stream_payload: Optional[Dict[str, Any]] = None
                if _TOOL_LIVE_EVENT_EMITTER.get() is not None:
                    async for stream_item in ProjectRuntimeWorkerClient().claude_stream(
                        project_id=project_id,
                        workspace_dir=project_dir,
                        prompt=prompt,
                        continue_session=continue_session,
                    ):
                        stream_item_type = str(stream_item.get("type") or "")
                        if stream_item_type == "stream_error":
                            text = str(
                                stream_item.get("error")
                                or stream_item.get("text")
                                or "runtime-worker stream interrupted"
                            )
                            await emit_tool_live_event(
                                {
                                    "type": "tool_output",
                                    "data": {
                                        "tool": self.name,
                                        "input": {
                                            "project_id": project_id,
                                            "prompt": prompt,
                                        },
                                        "stream": "stderr",
                                        "text": f"runtime-worker stream warning: {text}\n",
                                    },
                                }
                            )
                            continue
                        if stream_item_type == "chunk":
                            text = str(stream_item.get("text") or "")
                            if text:
                                await emit_tool_live_event(
                                    {
                                        "type": "tool_output",
                                        "data": {
                                            "tool": self.name,
                                            "input": {
                                                "project_id": project_id,
                                                "prompt": prompt,
                                            },
                                            "stream": str(stream_item.get("stream") or "stdout"),
                                            "text": text,
                                        },
                                    }
                                )
                            continue
                        if str(stream_item.get("type") or "") == "result" and isinstance(stream_item.get("payload"), dict):
                            live_stream_payload = dict(stream_item.get("payload") or {})
                    worker_payload = live_stream_payload or {
                        "project_id": project_id,
                        "workspace_dir": str(project_dir),
                        "prompt": prompt,
                        "continue_session": continue_session,
                        "session_id": "",
                        "assistant_text": "",
                        "result_text": "",
                        "is_error": True,
                        "exit_code": None,
                        "stdout": "",
                        "stderr": "",
                        "error": "project_claude_stream_missing_result",
                        "worker": "runtime-worker",
                    }
                else:
                    worker_payload = await ProjectRuntimeWorkerClient().claude(
                        project_id=project_id,
                        workspace_dir=project_dir,
                        prompt=prompt,
                        continue_session=continue_session,
                    )
            except Exception as exc:
                return ToolResult(
                    success=False,
                    output="\n".join(
                        [
                            "Project Claude 调用 runtime-worker 失败。",
                            f"- Project: /projects/{project_id}",
                            f"- Error: {type(exc).__name__}: {exc}",
                        ]
                    ),
                    error="project_claude_worker_failed",
                    data={
                        "project_id": project_id,
                        "prompt": prompt,
                        "continue_session": continue_session,
                        "cwd": str(project_dir),
                    },
                )

            result_text = str(worker_payload.get("result_text") or "").strip()
            assistant_text = str(worker_payload.get("assistant_text") or "").strip()
            rendered_text = result_text or assistant_text or str(worker_payload.get("stdout") or "").strip() or "(empty)"
            return ToolResult(
                success=not bool(worker_payload.get("is_error")),
                output="\n".join(
                    [
                        "已通过 runtime-worker 调用 Claude Code。",
                        f"- Project: /projects/{project_id}",
                        f"- Continue session: {continue_session}",
                        f"- Session: {str(worker_payload.get('session_id') or '').strip() or '(missing)'}",
                        "Claude result:",
                        rendered_text,
                    ]
                ),
                error=str(worker_payload.get("error") or "") or None,
                data={
                    "project_id": project_id,
                    "prompt": prompt,
                    "continue_session": continue_session,
                    "cwd": str(project_dir),
                    "session_id": str(worker_payload.get("session_id") or ""),
                    "assistant_text": assistant_text,
                    "result_text": result_text,
                    "stdout": str(worker_payload.get("stdout") or ""),
                    "stderr": str(worker_payload.get("stderr") or ""),
                    "exit_code": worker_payload.get("exit_code"),
                    "worker": str(worker_payload.get("worker") or "runtime-worker"),
                    "is_error": bool(worker_payload.get("is_error")),
                },
            )

        return await self._with_db(_handler)


class DocxGenerateWithClaudeTool(ToolBase):
    name = "docx_generate_with_claude"
    input_model = DocxGenerateWithClaudeInput
    timeout_seconds = 0.0
    retry_count = 0
    output_max_tokens = 9000
    description = (
        "把文档 artifact/source、模板文件和生成要求的路径写入独立 docx 工作目录清单，然后调用 runtime-worker 里的 Claude Code "
        "使用官方 document-skills/docx 生成 DOCX/PDF。"
        "它不属于 Project，不需要 project_id；工作目录固定为 `/app/uploads/docx/{docx_id}`。"
        "优先接收当前对话 active document artifact；也可传 artifact_json/artifact_path、source_path 或 markdown。"
        "正常情况下只把 artifact_path、template_file_paths、requirements_path 等路径交给 Claude，不复制模板文件，"
        "也不要求 Claude 用 Read 把大文件全文读进对话流。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "docx_id": {"type": "string", "description": "可选文档任务 ID。未提供时自动生成，用作 `/app/uploads/docx/{docx_id}` 目录名。"},
            "template_id": {"type": "string", "description": "可选模板 ID。工具会把 `/app/uploads/docx/templates/{template_id}/files` 下的原始模板路径写入输入清单，并把模板 DOCX 约束附加到 requirements.md。"},
            "artifact_id": {"type": "string", "description": "可选文档 artifact ID。未传 source 时，工具会优先读取当前对话 active artifact；传 artifact_id 时会尝试读取该 artifact。"},
            "artifact_json": {"type": "object", "description": "可选结构化文档草稿 JSON。仅当没有 artifact_path/active artifact 文件时，工具才会在工作区物化为 artifact.json。"},
            "artifact_path": {"type": "string", "description": "可选 artifact JSON 文件路径。支持 `/app/uploads` 下绝对路径或相对上传目录路径。"},
            "markdown": {"type": "string", "description": "可选完整 Markdown 原文。仅当直接传 markdown 时，工具会在工作区物化为 source.md；长文优先传 source_path。"},
            "source_path": {"type": "string", "description": "可选已有 Markdown 文件路径。支持 `/app/uploads` 下绝对路径或相对上传目录路径。"},
            "requirements": {"type": "string", "description": "可选文档生成要求、模板说明、章节要求、格式要求。未提供时工具会根据 artifact/template 生成基础要求。"},
            "output_basename": {"type": "string", "description": "可选输出文件基础名，不含扩展名。默认 generated_document。"},
            "continue_session": {"type": "boolean", "default": False, "description": "是否强制继续该 docx 工作目录下已有 Claude session。通常不用传；runtime 会自动检测可续 session。"},
        },
    }

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
        conversation_id: Optional[int] = None,
    ):
        self.db = db
        self.user_id = int(user_id) if user_id is not None else None
        self.db_session_factory = db_session_factory
        self.conversation_id = int(conversation_id) if conversation_id is not None else None

    @staticmethod
    def _upload_root() -> Path:
        configured = str(os.getenv("UPLOAD_DIR") or "").strip()
        if configured:
            return Path(os.path.abspath(configured))
        mounted = Path("/app/uploads")
        if mounted.exists():
            return mounted.resolve()
        return Path(os.path.abspath("./uploads"))

    @staticmethod
    def _safe_slug(value: Any, *, fallback: str) -> str:
        text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
        return (text or fallback)[:120]

    @classmethod
    def _new_docx_id(cls) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return cls._safe_slug(f"docx-{timestamp}-{uuid.uuid4().hex[:8]}", fallback="docx")

    @classmethod
    def _resolve_source_file(cls, raw_path: Any, *, upload_root: Path) -> Optional[Path]:
        raw = str(raw_path or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = upload_root / raw
        try:
            resolved = candidate.resolve()
            root = upload_root.resolve()
            resolved.relative_to(root)
        except Exception:
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _parse_artifact_payload(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        raw = str(value or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed or {}) if isinstance(parsed, dict) else {}

    @classmethod
    def _artifact_to_markdown(cls, artifact: Dict[str, Any]) -> str:
        title = str(artifact.get("title") or "文档草稿").strip() or "文档草稿"
        lines: List[str] = [
            f"# {title}",
            "",
            f"<!-- artifact_id: {artifact.get('artifact_id') or ''} -->",
            f"<!-- template_id: {artifact.get('template_id') or ''} -->",
            "",
        ]
        global_constraints = str(artifact.get("global_constraints") or "").strip()
        if global_constraints:
            lines.extend(["<!-- global_constraints", global_constraints, "-->", ""])
        blocks = list(artifact.get("blocks") or [])
        for index, raw_block in enumerate(blocks, start=1):
            if not isinstance(raw_block, dict):
                continue
            title = str(raw_block.get("title") or f"章节 {index}").strip() or f"章节 {index}"
            heading_path = raw_block.get("heading_path")
            if isinstance(heading_path, list) and heading_path:
                title = str(heading_path[-1] or title).strip() or title
            block_id = str(raw_block.get("block_id") or f"block-{index}").strip()
            lines.extend(
                [
                    f"## {title}",
                    "",
                    f"<!-- block_id: {block_id} -->",
                    f"<!-- status: {raw_block.get('status') or ''}; target_words: {raw_block.get('target_words') or 0} -->",
                ]
            )
            block_constraints = str(raw_block.get("block_constraints") or "").strip()
            if block_constraints:
                lines.extend(["<!-- block_constraints", block_constraints, "-->"])
            markdown = str(raw_block.get("markdown") or "").strip()
            lines.extend(["", markdown or "> 本块尚未填写。", ""])
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _read_artifact_json_file(cls, path: Path) -> Dict[str, Any]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(parsed or {}) if isinstance(parsed, dict) else {}

    async def _load_conversation_artifact(
        self,
        *,
        upload_root: Path,
        artifact_id: str,
    ) -> tuple[Dict[str, Any], str]:
        if self.conversation_id is None or self.user_id is None:
            return {}, ""
        from app.services.document_artifact_service import DocumentArtifactService

        service = DocumentArtifactService(upload_root=upload_root)
        if artifact_id:
            candidate = service._artifact_path(int(self.conversation_id), artifact_id)
            if candidate.is_file():
                return self._read_artifact_json_file(candidate), str(candidate)
        if self.db_session_factory is not None:
            async with self.db_session_factory() as db:
                artifact = await service.get_active_artifact(
                    db,
                    user_id=int(self.user_id),
                    conversation_id=int(self.conversation_id),
                )
                payload = dict(artifact or {})
                active_id = str(payload.get("artifact_id") or "").strip()
                active_path = service._artifact_path(int(self.conversation_id), active_id) if active_id else None
                return payload, str(active_path) if active_path is not None and active_path.is_file() else ""
        if self.db is not None:
            artifact = await service.get_active_artifact(
                self.db,
                user_id=int(self.user_id),
                conversation_id=int(self.conversation_id),
            )
            payload = dict(artifact or {})
            active_id = str(payload.get("artifact_id") or "").strip()
            active_path = service._artifact_path(int(self.conversation_id), active_id) if active_id else None
            return payload, str(active_path) if active_path is not None and active_path.is_file() else ""
        return {}, ""

    @staticmethod
    def _relaxed_chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except Exception:
            pass

    @classmethod
    def _collect_generated_files(cls, workspace_dir: Path, *, output_basename: str) -> Dict[str, Any]:
        def _pick(extension: str) -> str:
            preferred = workspace_dir / f"{output_basename}.{extension}"
            if preferred.is_file():
                return str(preferred)
            candidates = sorted(
                workspace_dir.glob(f"*.{extension}"),
                key=lambda item: item.stat().st_mtime if item.exists() else 0.0,
                reverse=True,
            )
            return str(candidates[0]) if candidates else ""

        docx_path = _pick("docx")
        pdf_path = _pick("pdf")
        return {
            "docx_path": docx_path,
            "pdf_path": pdf_path,
            "files": sorted(
                str(path)
                for path in workspace_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".md", ".json", ".docx", ".pdf", ".png"}
            ),
        }

    @staticmethod
    def _read_docx_validation_result(workspace_dir: Path) -> Dict[str, Any]:
        path = workspace_dir / "validation_result.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload or {}) if isinstance(payload, dict) else {}

    @classmethod
    def _infer_validation_status_from_workspace(cls, workspace_dir: Path, *, fallback: str = "") -> str:
        payload = cls._read_docx_validation_result(workspace_dir)
        raw_status = str(payload.get("status") or payload.get("validation_status") or "").strip().lower()
        if raw_status in {"success", "passed", "pass", "ok"}:
            return "passed"
        if raw_status in {"failed", "failure", "error"}:
            return "failed"
        return fallback

    @classmethod
    def _write_docx_request_metadata(
        cls,
        metadata_file: Path,
        *,
        docx_id: str,
        template_id: str,
        template_name: str,
        artifact_id: str,
        conversation_id: Optional[int],
        user_id: Optional[int],
        output_basename: str,
        source_file: Optional[Path],
        artifact_file: Optional[Path],
        artifact_source_path: str,
        requirements_file: Path,
        default_docx_style_file: Path,
        md_constraints_file: Path,
        input_manifest_file: Optional[Path],
        template_files: List[str],
        status: str,
        validation_status: str,
        session_id: str,
        files: Dict[str, Any],
        error: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = {
            "docx_id": docx_id,
            "template_id": template_id,
            "template_name": template_name,
            "artifact_id": artifact_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "output_basename": output_basename,
            "source_file": str(source_file) if source_file else "",
            "artifact_file": str(artifact_file) if artifact_file else "",
            "artifact_source_path": artifact_source_path,
            "requirements_file": str(requirements_file),
            "default_docx_style_prompt_file": str(default_docx_style_file),
            "md_constraints_path": str(md_constraints_file) if template_id else "",
            "input_manifest_file": str(input_manifest_file) if input_manifest_file else "",
            "template_files": template_files,
            "status": status,
            "validation_status": validation_status,
            "session_id": session_id,
            "docx_path": files.get("docx_path") or "",
            "pdf_path": files.get("pdf_path") or "",
            "files": files.get("files") or [],
            "error": error,
            "updated_at": datetime.utcnow().isoformat(),
        }
        if extra:
            payload.update(extra)
        metadata_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        cls._relaxed_chmod(metadata_file, 0o666)

    @staticmethod
    def _infer_validation_status(*, worker_payload: Dict[str, Any], docx_path: str) -> str:
        combined_text = "\n".join(
            str(worker_payload.get(key) or "")
            for key in ("result_text", "assistant_text", "stdout", "stderr")
        )
        if "All validations PASSED" in combined_text or "All validations passed" in combined_text:
            return "passed"
        if bool(worker_payload.get("is_error")):
            return "failed"
        return "generated_unverified" if docx_path else "missing"

    @staticmethod
    def _build_claude_prompt(
        *,
        workspace_dir: Path,
        output_basename: str,
        input_manifest_file: Path,
        artifact_file: Optional[Path],
        source_file: Optional[Path],
        requirements_file: Path,
        template_files: List[str],
    ) -> str:
        docx_path = workspace_dir / f"{output_basename}.docx"
        pdf_path = workspace_dir / f"{output_basename}.pdf"
        template_hint = "\n".join(f"- {path}" for path in template_files[:20]) or "- (none)"
        return "\n".join(
            [
                "你现在只负责 DOCX 文档生成，不处理 Project/论文复现语义。",
                f"工作目录：{workspace_dir}",
                f"输入清单：{input_manifest_file}",
                "关键输入路径：",
                f"- artifact_path: {artifact_file or ''}",
                f"- source_path: {source_file or ''}",
                f"- requirements_path: {requirements_file}",
                "- template_file_paths:",
                template_hint,
                "任务：",
                "1. 先读取小清单 docx_inputs_manifest.json，按里面的路径处理 artifact、source、requirements 和模板文件。",
                "2. artifact_path 是结构化章节、block 顺序和正文内容的权威来源；source_path 只在清单提供时作为 Markdown 原文参考。",
                "   如果 source_path 指向 source.md，它是由 artifact.blocks 按顺序展开的纯 Markdown 草稿，便于整体阅读；仍以 artifact_path 的 block 结构和约束为准。",
                "3. 如有模板文件，优先参考原始 template_file_paths 中的样例、指南、图片或规范文件。",
                "4. 优先使用官方 document-skills/docx 工作流和校验脚本；不要只生成一个能打开的空壳文件。",
                f"5. DOCX 输出到：{docx_path}",
                f"6. 如环境支持，同时生成 PDF 预览到：{pdf_path}",
                "7. 如果无法完成，明确说明阻塞点，不要假装成功。",
                "",
                "流式输出约束（必须遵守）：",
                "- 不要用 Read 工具把 artifact_path、source_path、requirements_path 或模板文件的全文读入对话流。",
                "- 如需读取大文件，使用 Python/脚本按路径读取并直接生成 DOCX/PDF；stdout/stderr 只输出短进度和最终路径。",
                "- 不要把 artifact/source/requirements/template 的全文打印到 stdout/stderr。",
                "- 不要调用 project_* 工具；docx 工作区不是 Project。",
                "最终回复必须给出 docx_path、pdf_path（没有则空）、validation_status、notes。",
            ]
        )

    async def _upsert_docx_job(self, template_service: Any, payload: Dict[str, Any]) -> None:
        if self.db_session_factory is not None:
            async with self.db_session_factory() as db:
                await template_service.upsert_generation_job(
                    db,
                    user_id=self.user_id,
                    job=payload,
                )
                await db.commit()
            return
        if self.db is not None:
            await template_service.upsert_generation_job(
                self.db,
                user_id=self.user_id,
                job=payload,
            )
            if self.db.in_transaction():
                await self.db.commit()

    async def _execute(self, **kwargs) -> ToolResult:
        from app.services.docx_runtime_service import DocxRuntimeWorkerClient
        from app.services.docx_template_service import DocxTemplateService

        docx_id = self._safe_slug(kwargs.get("docx_id"), fallback="") or self._new_docx_id()
        template_id = self._safe_slug(kwargs.get("template_id"), fallback="")
        artifact_id = self._safe_slug(kwargs.get("artifact_id"), fallback="")
        output_basename = self._safe_slug(kwargs.get("output_basename"), fallback="generated_document")
        requirements = str(kwargs.get("requirements") or "").strip()
        markdown = kwargs.get("markdown")
        source_path = str(kwargs.get("source_path") or "").strip()
        artifact_path = str(kwargs.get("artifact_path") or "").strip()
        continue_session = bool(kwargs.get("continue_session"))

        upload_root = self._upload_root()
        template_service = DocxTemplateService(upload_root=upload_root)
        workspace_dir = upload_root / "docx" / docx_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._relaxed_chmod(workspace_dir, 0o777)

        artifact_payload = self._parse_artifact_payload(kwargs.get("artifact_json"))
        artifact_source_path = ""
        if not artifact_payload and artifact_path:
            resolved_artifact = self._resolve_source_file(artifact_path, upload_root=upload_root)
            if resolved_artifact is None:
                return ToolResult(
                    success=False,
                    output="artifact_path 不存在，或不在上传目录内。",
                    error="invalid_artifact_path",
                    data={"docx_id": docx_id, "artifact_path": artifact_path, "workspace_dir": str(workspace_dir)},
                )
            artifact_payload = self._read_artifact_json_file(resolved_artifact)
            artifact_source_path = str(resolved_artifact)
        if not artifact_payload and not markdown and not source_path:
            artifact_payload, artifact_source_path = await self._load_conversation_artifact(
                upload_root=upload_root,
                artifact_id=artifact_id,
            )
        if artifact_payload:
            artifact_id = self._safe_slug(artifact_id or artifact_payload.get("artifact_id"), fallback=artifact_id or "artifact")
            if not template_id:
                template_id = self._safe_slug(artifact_payload.get("template_id"), fallback="")
            if not requirements:
                requirements = "\n".join(
                    [
                        "请基于 artifact_path 指向的结构化文档草稿生成正式 DOCX 文档。",
                        "artifact 是结构化草稿，必须按 blocks 顺序组织章节；block.markdown 是正文内容来源。",
                        "如存在 template_file_paths 和模板 DOCX 约束，优先遵循模板文件和 DOCX 约束。",
                    ]
                )

        artifact_file: Optional[Path] = None
        if artifact_payload:
            if artifact_source_path:
                artifact_file = Path(artifact_source_path)
            else:
                artifact_file = workspace_dir / "artifact.json"
                artifact_file.write_text(
                    json.dumps(artifact_payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                self._relaxed_chmod(artifact_file, 0o666)

        source_file: Optional[Path] = None
        if markdown is not None and str(markdown) != "":
            source_file = workspace_dir / "source.md"
            source_file.write_text(str(markdown), encoding="utf-8")
        elif source_path:
            resolved_source = self._resolve_source_file(source_path, upload_root=upload_root)
            if resolved_source is None:
                return ToolResult(
                    success=False,
                    output="source_path 不存在，或不在上传目录内。",
                    error="invalid_source_path",
                    data={"docx_id": docx_id, "source_path": source_path, "workspace_dir": str(workspace_dir)},
            )
            source_file = resolved_source
        elif artifact_payload:
            source_file = workspace_dir / "source.md"
            source_file.write_text(self._artifact_to_markdown(artifact_payload), encoding="utf-8")
        else:
            return ToolResult(
                success=False,
                output="必须提供 markdown、source_path、artifact_json、artifact_path，或在当前对话中存在 active document artifact。",
                error="missing_source",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        requirements_file = workspace_dir / "requirements.md"
        if not requirements:
            requirements = "请基于输入清单中的 artifact_path/source_path 生成正式 DOCX 文档；如存在模板文件或模板约束，优先遵循模板。"
        requirements_file.write_text(requirements, encoding="utf-8")
        default_docx_style_prompt = template_service.get_default_docx_style_prompt()
        default_docx_style_file = workspace_dir / "default_docx_style_prompt.md"
        default_docx_style_file.write_text(default_docx_style_prompt, encoding="utf-8")
        if default_docx_style_prompt.strip():
            requirements = "\n\n".join(
                [
                    requirements.rstrip(),
                    "# 平台默认 DOCX 样式要求",
                    default_docx_style_prompt.strip(),
                ]
            ).strip()
            requirements_file.write_text(requirements, encoding="utf-8")
        metadata_file = workspace_dir / "docx_request.json"
        metadata_file.write_text(
            json.dumps(
                {
                    "docx_id": docx_id,
                    "template_id": template_id,
                    "artifact_id": artifact_id,
                    "conversation_id": self.conversation_id,
                    "user_id": self.user_id,
                    "output_basename": output_basename,
                    "source_file": str(source_file) if source_file else "",
                    "artifact_file": str(artifact_file) if artifact_file else "",
                    "artifact_source_path": artifact_source_path,
                    "requirements_file": str(requirements_file),
                    "default_docx_style_prompt_file": str(default_docx_style_file),
                    "status": "preparing",
                    "created_at": datetime.utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        for path in (source_file, requirements_file, default_docx_style_file, metadata_file):
            if path is not None:
                self._relaxed_chmod(path, 0o666)

        template_payload: Optional[Dict[str, Any]] = None
        template_files: List[str] = []
        template_file_refs: List[Dict[str, Any]] = []
        md_constraints_file = workspace_dir / "template_md_constraints.md"
        if template_id:
            try:
                refs = template_service.template_file_references(template_id=template_id)
            except ValueError:
                return ToolResult(
                    success=False,
                    output=f"模板不存在：{template_id}",
                    error="template_not_found",
                    data={"docx_id": docx_id, "template_id": template_id, "workspace_dir": str(workspace_dir)},
                )
            template_payload = dict(refs.get("template") or {})
            template_file_refs = [dict(item or {}) for item in list(refs.get("files") or []) if isinstance(item, dict)]
            template_files = [str(item.get("path") or "") for item in template_file_refs if str(item.get("path") or "").strip()]
            md_constraints = str(template_payload.get("md_constraints") or "")
            docx_constraints = str(template_payload.get("docx_constraints") or "")
            md_constraints_file.write_text(md_constraints, encoding="utf-8")
            if docx_constraints.strip():
                requirements = "\n\n".join(
                    [
                        requirements.rstrip(),
                        "# 模板 DOCX 约束",
                        docx_constraints.strip(),
                    ]
                ).strip()
                requirements_file.write_text(requirements, encoding="utf-8")
            self._relaxed_chmod(md_constraints_file, 0o666)

        input_manifest_file = workspace_dir / "docx_inputs_manifest.json"
        input_manifest = {
            "docx_id": docx_id,
            "workspace_dir": str(workspace_dir),
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "artifact_id": artifact_id,
            "artifact_path": str(artifact_file) if artifact_file else "",
            "source_path": str(source_file) if source_file else "",
            "requirements_path": str(requirements_file),
            "default_docx_style_prompt_path": str(default_docx_style_file),
            "md_constraints_path": str(md_constraints_file) if template_id else "",
            "template_id": template_id,
            "template_name": str((template_payload or {}).get("name") or ""),
            "template_files_dir": str((template_payload or {}).get("files_path") or ""),
            "template_files": template_file_refs,
            "output_basename": output_basename,
            "output_docx_path": str(workspace_dir / f"{output_basename}.docx"),
            "output_pdf_path": str(workspace_dir / f"{output_basename}.pdf"),
            "created_at": datetime.utcnow().isoformat(),
        }
        input_manifest_file.write_text(json.dumps(input_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._relaxed_chmod(input_manifest_file, 0o666)

        running_job_payload = {
            "docx_id": docx_id,
            "template_id": template_id,
            "template_name": str((template_payload or {}).get("name") or ""),
            "artifact_id": artifact_id,
            "conversation_id": self.conversation_id,
            "workspace_dir": str(workspace_dir),
            "source_path": str(source_file) if source_file else "",
            "artifact_path": str(artifact_file) if artifact_file else "",
            "requirements_path": str(requirements_file),
            "output_basename": output_basename,
            "status": "running",
            "validation_status": "",
            "files": [],
            "metadata": {
                "artifact_source_path": artifact_source_path,
                "input_manifest_path": str(input_manifest_file),
                "default_docx_style_prompt_file": str(default_docx_style_file),
                "md_constraints_path": str(md_constraints_file) if template_id else "",
                "template_files": template_files,
                "template_file_refs": template_file_refs,
            },
        }
        metadata_file.write_text(
            json.dumps(
                {
                    "docx_id": docx_id,
                    "template_id": template_id,
                    "template_name": running_job_payload["template_name"],
                    "artifact_id": artifact_id,
                    "conversation_id": self.conversation_id,
                    "user_id": self.user_id,
                    "output_basename": output_basename,
                    "source_file": str(source_file) if source_file else "",
                    "artifact_file": str(artifact_file) if artifact_file else "",
                    "artifact_source_path": artifact_source_path,
                    "requirements_file": str(requirements_file),
                    "default_docx_style_prompt_file": str(default_docx_style_file),
                    "md_constraints_path": str(md_constraints_file) if template_id else "",
                    "input_manifest_file": str(input_manifest_file),
                    "template_files": template_files,
                    "template_file_refs": template_file_refs,
                    "status": "running",
                    "created_at": datetime.utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._relaxed_chmod(metadata_file, 0o666)
        await self._upsert_docx_job(template_service, running_job_payload)

        if not DocxRuntimeWorkerClient.enabled():
            await self._upsert_docx_job(
                template_service,
                {
                    **running_job_payload,
                    "status": "failed",
                    "error": "runtime_worker_disabled",
                },
            )
            return ToolResult(
                success=False,
                output="runtime-worker 未启用，当前不能调用 Claude Code 生成 DOCX。",
                error="runtime_worker_disabled",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        prompt = self._build_claude_prompt(
            workspace_dir=workspace_dir,
            output_basename=output_basename,
            input_manifest_file=input_manifest_file,
            artifact_file=artifact_file,
            source_file=source_file,
            requirements_file=requirements_file,
            template_files=template_files,
        )
        live_stream_payload: Optional[Dict[str, Any]] = None
        stream_errors: List[str] = []
        try:
            if _TOOL_LIVE_EVENT_EMITTER.get() is not None:
                async for stream_item in DocxRuntimeWorkerClient().claude_stream(
                    docx_id=docx_id,
                    workspace_dir=workspace_dir,
                    prompt=prompt,
                    continue_session=continue_session,
                ):
                    stream_item_type = str(stream_item.get("type") or "")
                    if stream_item_type == "stream_error":
                        stream_error_text = str(
                            stream_item.get("error")
                            or stream_item.get("text")
                            or "runtime-worker stream interrupted"
                        )
                        stream_errors.append(stream_error_text)
                        await emit_tool_live_event(
                            {
                                "type": "tool_output",
                                "data": {
                                    "tool": self.name,
                                    "input": {
                                        "docx_id": docx_id,
                                        "workspace_dir": str(workspace_dir),
                                    },
                                    "stream": "stderr",
                                    "text": f"runtime-worker stream warning: {stream_error_text}\n",
                                },
                            }
                        )
                        continue
                    if str(stream_item.get("type") or "") == "chunk":
                        text = str(stream_item.get("text") or "")
                        if text:
                            await emit_tool_live_event(
                                {
                                    "type": "tool_output",
                                    "data": {
                                        "tool": self.name,
                                        "input": {
                                            "docx_id": docx_id,
                                            "workspace_dir": str(workspace_dir),
                                        },
                                        "stream": str(stream_item.get("stream") or "stdout"),
                                        "text": text,
                                    },
                                }
                            )
                        continue
                    if str(stream_item.get("type") or "") == "result" and isinstance(stream_item.get("payload"), dict):
                        live_stream_payload = dict(stream_item.get("payload") or {})
                worker_payload = live_stream_payload or {
                    "docx_id": docx_id,
                    "workspace_dir": str(workspace_dir),
                    "prompt": prompt,
                    "continue_session": continue_session,
                    "session_id": "",
                    "assistant_text": "",
                    "result_text": "",
                    "is_error": True,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "\n".join(stream_errors),
                    "error": "docx_claude_stream_interrupted" if stream_errors else "docx_claude_stream_missing_result",
                    "worker": "runtime-worker",
                }
            else:
                worker_payload = await DocxRuntimeWorkerClient().claude(
                    docx_id=docx_id,
                    workspace_dir=workspace_dir,
                    prompt=prompt,
                    continue_session=continue_session,
                )
        except Exception as exc:
            stream_error = f"{type(exc).__name__}: {exc}"
            files = self._collect_generated_files(workspace_dir, output_basename=output_basename)
            if files.get("docx_path"):
                recovered_payload = dict(live_stream_payload or {})
                validation_status = self._infer_validation_status_from_workspace(
                    workspace_dir,
                    fallback="generated_after_stream_error",
                )
                recovered_error = f"stream_transport_error_after_output: {stream_error}"
                recovered_job_payload = {
                    **running_job_payload,
                    "status": "completed",
                    "docx_path": files.get("docx_path") or "",
                    "pdf_path": files.get("pdf_path") or "",
                    "files": files.get("files") or [],
                    "validation_status": validation_status,
                    "session_id": str(recovered_payload.get("session_id") or ""),
                    "error": recovered_error,
                    "metadata": {
                        **dict(running_job_payload.get("metadata") or {}),
                        "assistant_text": str(recovered_payload.get("assistant_text") or ""),
                        "result_text": str(recovered_payload.get("result_text") or ""),
                        "stream_error": stream_error,
                        "recovered_from_stream_error": True,
                    },
                }
                self._write_docx_request_metadata(
                    metadata_file,
                    docx_id=docx_id,
                    template_id=template_id,
                    template_name=str((template_payload or {}).get("name") or ""),
                    artifact_id=artifact_id,
                    conversation_id=self.conversation_id,
                    user_id=self.user_id,
                    output_basename=output_basename,
                    source_file=source_file,
                    artifact_file=artifact_file,
                    artifact_source_path=artifact_source_path,
                    requirements_file=requirements_file,
                    default_docx_style_file=default_docx_style_file,
                    md_constraints_file=md_constraints_file,
                    input_manifest_file=input_manifest_file,
                    template_files=template_files,
                    status="completed",
                    validation_status=validation_status,
                    session_id=str(recovered_payload.get("session_id") or ""),
                    files=files,
                    error=recovered_error,
                    extra={"stream_error": stream_error, "recovered_from_stream_error": True},
                )
                await self._upsert_docx_job(template_service, recovered_job_payload)
                return ToolResult(
                    success=True,
                    output="\n".join(
                        [
                            "Claude 已生成 DOCX，但 runtime-worker 流式连接在结束前中断；平台已从工作区产物恢复为成功。",
                            f"- Docx ID: {docx_id}",
                            f"- Workspace: {workspace_dir}",
                            f"- DOCX: {files.get('docx_path') or '(missing)'}",
                            f"- PDF: {files.get('pdf_path') or '(missing)'}",
                            f"- Validation: {validation_status}",
                            f"- Stream error: {stream_error}",
                        ]
                    ),
                    error=None,
                    data={
                        "docx_id": docx_id,
                        "template_id": template_id,
                        "template_name": str((template_payload or {}).get("name") or ""),
                        "artifact_id": artifact_id,
                        "conversation_id": self.conversation_id,
                        "workspace_dir": str(workspace_dir),
                        "source_path": str(source_file) if source_file else "",
                        "artifact_path": str(artifact_file) if artifact_file else "",
                        "requirements_path": str(requirements_file),
                        "input_manifest_path": str(input_manifest_file),
                        "md_constraints_path": str(md_constraints_file) if template_id else "",
                        "output_basename": output_basename,
                        "docx_path": files.get("docx_path") or "",
                        "pdf_path": files.get("pdf_path") or "",
                        "files": files.get("files") or [],
                        "template_files": template_files,
                        "validation_status": validation_status,
                        "session_id": str(recovered_payload.get("session_id") or ""),
                        "stream_error": stream_error,
                        "recovered_from_stream_error": True,
                        "worker": "runtime-worker",
                        "is_error": False,
                    },
                )
            await self._upsert_docx_job(
                template_service,
                {
                    **running_job_payload,
                    "status": "failed",
                    "error": stream_error,
                },
            )
            return ToolResult(
                success=False,
                output="\n".join(
                    [
                        "DOCX Claude 调用 runtime-worker 失败。",
                        f"- Docx ID: {docx_id}",
                        f"- Workspace: {workspace_dir}",
                        f"- Error: {stream_error}",
                    ]
                ),
                error="docx_claude_worker_failed",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        files = self._collect_generated_files(workspace_dir, output_basename=output_basename)
        validation_status = self._infer_validation_status(
            worker_payload=worker_payload,
            docx_path=str(files.get("docx_path") or ""),
        )
        workspace_validation_status = self._infer_validation_status_from_workspace(workspace_dir)
        if workspace_validation_status:
            validation_status = workspace_validation_status
        result_text = str(worker_payload.get("result_text") or "").strip()
        assistant_text = str(worker_payload.get("assistant_text") or "").strip()
        rendered_text = result_text or assistant_text or "(empty)"
        stream_missing_result = str(worker_payload.get("error") or "") in {
            "docx_claude_stream_missing_result",
            "docx_claude_stream_interrupted",
        }
        success = bool(files.get("docx_path")) and (
            not bool(worker_payload.get("is_error")) or stream_missing_result
        )
        completed_status = "completed" if success else "failed"
        completed_error = str(worker_payload.get("error") or "") or ("" if success else "docx_missing_output")
        if success and stream_missing_result:
            completed_error = "stream_missing_result_after_output"
        completed_job_payload = {
            **running_job_payload,
            "status": completed_status,
            "docx_path": files.get("docx_path") or "",
            "pdf_path": files.get("pdf_path") or "",
            "files": files.get("files") or [],
            "validation_status": validation_status,
            "session_id": str(worker_payload.get("session_id") or ""),
            "error": completed_error,
            "metadata": {
                **dict(running_job_payload.get("metadata") or {}),
                "assistant_text": assistant_text,
                "result_text": result_text,
                "stream_missing_result_recovered": bool(success and stream_missing_result),
            },
        }
        metadata_file.write_text(
            json.dumps(
                {
                    "docx_id": docx_id,
                    "template_id": template_id,
                    "template_name": str((template_payload or {}).get("name") or ""),
                    "artifact_id": artifact_id,
                    "conversation_id": self.conversation_id,
                    "user_id": self.user_id,
                    "output_basename": output_basename,
                    "source_file": str(source_file) if source_file else "",
                    "artifact_file": str(artifact_file) if artifact_file else "",
                    "artifact_source_path": artifact_source_path,
                    "requirements_file": str(requirements_file),
                    "default_docx_style_prompt_file": str(default_docx_style_file),
                    "md_constraints_path": str(md_constraints_file) if template_id else "",
                    "input_manifest_file": str(input_manifest_file),
                    "template_files": template_files,
                    "template_file_refs": template_file_refs,
                    "status": completed_status,
                    "validation_status": validation_status,
                    "session_id": str(worker_payload.get("session_id") or ""),
                    "docx_path": files.get("docx_path") or "",
                    "pdf_path": files.get("pdf_path") or "",
                    "files": files.get("files") or [],
                    "error": completed_job_payload["error"],
                    "updated_at": datetime.utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._relaxed_chmod(metadata_file, 0o666)
        await self._upsert_docx_job(template_service, completed_job_payload)
        failure_guidance_lines: List[str] = []
        if not success:
            failure_guidance_lines = [
                "范围提示：DOCX 生成未产出目标文件时，不要改用 project_tree、project_read_file、project_bash 或 project_claude 检查/补救。",
                "docx 工作区不是 Project；如需重试请重新调用 docx_generate_with_claude，或向用户报告 Claude 未产出文件。",
            ]
        return ToolResult(
            success=success,
            output="\n".join(
                [
                    "已通过 runtime-worker 调用 Claude Code 生成 DOCX。",
                    f"- Docx ID: {docx_id}",
                    f"- Workspace: {workspace_dir}",
                    f"- Continue session: {continue_session}",
                    f"- Session: {str(worker_payload.get('session_id') or '').strip() or '(missing)'}",
                    f"- DOCX: {files.get('docx_path') or '(missing)'}",
                    f"- PDF: {files.get('pdf_path') or '(missing)'}",
                    f"- Validation: {validation_status}",
                    *failure_guidance_lines,
                    "Claude result:",
                    rendered_text,
                ]
            ),
            error=None if success else (str(worker_payload.get("error") or "") or "docx_missing_output"),
            data={
                "docx_id": docx_id,
                "template_id": template_id,
                "template_name": str((template_payload or {}).get("name") or ""),
                "artifact_id": artifact_id,
                "conversation_id": self.conversation_id,
                "workspace_dir": str(workspace_dir),
                "source_path": str(source_file) if source_file else "",
                "artifact_path": str(artifact_file) if artifact_file else "",
                "requirements_path": str(requirements_file),
                "input_manifest_path": str(input_manifest_file),
                "md_constraints_path": str(md_constraints_file) if template_id else "",
                "output_basename": output_basename,
                "docx_path": files.get("docx_path") or "",
                "pdf_path": files.get("pdf_path") or "",
                "files": files.get("files") or [],
                "template_files": template_files,
                "validation_status": validation_status,
                "session_id": str(worker_payload.get("session_id") or ""),
                "assistant_text": assistant_text,
                "result_text": result_text,
                "stdout": str(worker_payload.get("stdout") or ""),
                "stderr": str(worker_payload.get("stderr") or ""),
                "exit_code": worker_payload.get("exit_code"),
                "worker": str(worker_payload.get("worker") or "runtime-worker"),
                "is_error": bool(worker_payload.get("is_error")),
            },
        )


class DocxRefineWithClaudeTool(DocxGenerateWithClaudeTool):
    name = "docx_refine_with_claude"
    input_model = DocxRefineWithClaudeInput
    timeout_seconds = 0.0
    retry_count = 0
    output_max_tokens = 9000
    description = (
        "在已有 `/app/uploads/docx/{docx_id}` 工作目录内继续调用 runtime-worker 里的 Claude Code，"
        "让它修改、润色或修复该 docx_id 目录下已经生成的 DOCX/PDF。"
        "它不创建新 docx 工作区，不属于 Project，不需要 project_id；只允许操作指定 docx_id 工作目录内的文件。"
        "适合根据用户反馈修改已有 DOCX、修复目录/页码/样式、补充内容或重新导出 PDF。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "docx_id": {"type": "string", "description": "必填，已有 DOCX 工作区 ID，对应 `/app/uploads/docx/{docx_id}`。"},
            "instruction": {"type": "string", "description": "必填，用户对现有 DOCX 的修改要求。不要传整篇文档全文；引用工作区路径即可。"},
            "target_docx_path": {"type": "string", "description": "可选，要修改的 DOCX 路径。必须位于该 docx_id 工作目录内；未传则自动选择当前输出 DOCX。"},
            "output_basename": {"type": "string", "description": "可选输出文件基础名。不传时沿用现有输出名，通常会原位更新当前 DOCX。"},
            "continue_session": {"type": "boolean", "default": True, "description": "是否继续该 docx 工作区已有 Claude session。默认 true。"},
        },
        "required": ["docx_id", "instruction"],
    }

    @classmethod
    def _resolve_workspace_file(cls, raw_path: Any, *, workspace_dir: Path) -> Optional[Path]:
        raw = str(raw_path or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace_dir / raw
        try:
            resolved = candidate.resolve()
            resolved.relative_to(workspace_dir.resolve())
        except Exception:
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _read_json_if_exists(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload or {}) if isinstance(payload, dict) else {}

    @classmethod
    def _pick_target_docx(cls, workspace_dir: Path, *, raw_target: str, metadata: Dict[str, Any]) -> Optional[Path]:
        explicit = cls._resolve_workspace_file(raw_target, workspace_dir=workspace_dir)
        if explicit is not None and explicit.suffix.lower() == ".docx":
            return explicit
        metadata_path = cls._resolve_workspace_file(metadata.get("docx_path"), workspace_dir=workspace_dir)
        if metadata_path is not None and metadata_path.suffix.lower() == ".docx":
            return metadata_path
        output_basename = str(metadata.get("output_basename") or "").strip()
        if output_basename:
            candidate = workspace_dir / f"{cls._safe_slug(output_basename, fallback='generated_document')}.docx"
            if candidate.is_file():
                return candidate
        candidates = sorted(
            workspace_dir.glob("*.docx"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0.0,
            reverse=True,
        )
        return candidates[0] if candidates else None

    @staticmethod
    def _workspace_doc_outputs_snapshot(workspace_dir: Path) -> Dict[str, float]:
        return {
            str(path): path.stat().st_mtime
            for path in workspace_dir.glob("*")
            if path.is_file() and path.suffix.lower() in {".docx", ".pdf"}
        }

    @staticmethod
    def _changed_doc_outputs(workspace_dir: Path, before: Dict[str, float]) -> List[str]:
        changed: List[str] = []
        for path in workspace_dir.glob("*"):
            if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
                continue
            current = path.stat().st_mtime
            previous = before.get(str(path))
            if previous is None or abs(current - previous) > 0.001:
                changed.append(str(path))
        return sorted(changed)

    @staticmethod
    def _build_refine_prompt(
        *,
        workspace_dir: Path,
        request_file: Path,
        target_docx: Path,
        output_docx: Path,
        output_pdf: Path,
        instruction: str,
    ) -> str:
        return "\n".join(
            [
                "你现在只负责在已有 DOCX 工作区内修改文档，不处理 Project/论文复现语义。",
                f"工作目录：{workspace_dir}",
                f"修改请求文件：{request_file}",
                f"目标 DOCX：{target_docx}",
                f"期望 DOCX 输出：{output_docx}",
                f"期望 PDF 输出：{output_pdf}",
                "",
                "用户修改要求：",
                instruction.strip(),
                "",
                "工作规则：",
                "1. 只在当前 docx_id 工作目录内读写文件；不要调用 project_* 工具，不要访问 `/app/uploads/projects`。",
                "2. 可以读取 docx_inputs_manifest.json、docx_request.json、requirements.md、template_md_constraints.md 等小清单/约束文件。",
                "3. 不要用 Read 把大型 artifact/source/template 文件全文读入对话流；如需读取，使用脚本按路径读取并直接处理。",
                "4. 修改目标 DOCX 时可以原位覆盖；如果需要保留原件，先在同目录创建简短备份文件。",
                "5. 修改后尽量重新导出 PDF 预览；如果无法导出 PDF，明确说明原因。",
                "6. stdout/stderr 只输出短进度和最终路径，不要打印大文件全文。",
                "最终回复必须给出 docx_path、pdf_path（没有则空）、changed_files、validation_status、notes。",
            ]
        )

    async def _execute(self, **kwargs) -> ToolResult:
        from app.services.docx_runtime_service import DocxRuntimeWorkerClient
        from app.services.docx_template_service import DocxTemplateService

        docx_id = self._safe_slug(kwargs.get("docx_id"), fallback="")
        if not docx_id:
            return ToolResult(success=False, output="docx_id 不能为空。", error="missing_docx_id")
        instruction = str(kwargs.get("instruction") or "").strip()
        if not instruction:
            return ToolResult(success=False, output="instruction 不能为空。", error="missing_instruction")

        upload_root = self._upload_root()
        workspace_dir = (upload_root / "docx" / docx_id).resolve()
        try:
            workspace_dir.relative_to((upload_root / "docx").resolve())
        except Exception:
            return ToolResult(
                success=False,
                output="docx_id 无效，不能解析到 DOCX 工作区。",
                error="invalid_docx_id",
                data={"docx_id": docx_id},
            )
        if not workspace_dir.is_dir():
            return ToolResult(
                success=False,
                output=f"DOCX 工作区不存在：{workspace_dir}",
                error="docx_workspace_not_found",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        metadata_file = workspace_dir / "docx_request.json"
        input_manifest_file = workspace_dir / "docx_inputs_manifest.json"
        metadata = self._read_json_if_exists(metadata_file)
        manifest = self._read_json_if_exists(input_manifest_file)
        raw_target_docx = str(kwargs.get("target_docx_path") or "").strip()
        if raw_target_docx:
            explicit_target = self._resolve_workspace_file(raw_target_docx, workspace_dir=workspace_dir)
            if explicit_target is None or explicit_target.suffix.lower() != ".docx":
                return ToolResult(
                    success=False,
                    output="target_docx_path 不存在、不是 DOCX，或不在该 docx_id 工作区内。",
                    error="invalid_target_docx_path",
                    data={"docx_id": docx_id, "target_docx_path": raw_target_docx, "workspace_dir": str(workspace_dir)},
                )
        target_docx = self._pick_target_docx(
            workspace_dir,
            raw_target=raw_target_docx,
            metadata={**manifest, **metadata},
        )
        if target_docx is None:
            return ToolResult(
                success=False,
                output="该 docx_id 工作区下没有可修改的 DOCX 文件。",
                error="target_docx_not_found",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        output_basename = self._safe_slug(
            kwargs.get("output_basename") or metadata.get("output_basename") or target_docx.stem,
            fallback=target_docx.stem or "generated_document",
        )
        output_docx = workspace_dir / f"{output_basename}.docx"
        output_pdf = workspace_dir / f"{output_basename}.pdf"
        continue_session = bool(kwargs.get("continue_session", True))
        request_file = workspace_dir / "docx_refine_request.json"
        request_payload = {
            "docx_id": docx_id,
            "workspace_dir": str(workspace_dir),
            "target_docx_path": str(target_docx),
            "output_docx_path": str(output_docx),
            "output_pdf_path": str(output_pdf),
            "instruction": instruction,
            "input_manifest_path": str(input_manifest_file) if input_manifest_file.is_file() else "",
            "docx_request_path": str(metadata_file) if metadata_file.is_file() else "",
            "created_at": datetime.utcnow().isoformat(),
        }
        request_file.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._relaxed_chmod(request_file, 0o666)

        template_service = DocxTemplateService(upload_root=upload_root)
        running_job_payload = {
            "docx_id": docx_id,
            "template_id": str(metadata.get("template_id") or manifest.get("template_id") or ""),
            "template_name": str(metadata.get("template_name") or manifest.get("template_name") or ""),
            "artifact_id": str(metadata.get("artifact_id") or manifest.get("artifact_id") or ""),
            "conversation_id": self.conversation_id or metadata.get("conversation_id") or manifest.get("conversation_id"),
            "workspace_dir": str(workspace_dir),
            "source_path": str(metadata.get("source_file") or metadata.get("source_path") or manifest.get("source_path") or ""),
            "requirements_path": str(metadata.get("requirements_file") or metadata.get("requirements_path") or manifest.get("requirements_path") or ""),
            "output_basename": output_basename,
            "docx_path": str(target_docx),
            "pdf_path": str(output_pdf) if output_pdf.is_file() else str(metadata.get("pdf_path") or ""),
            "status": "running",
            "validation_status": "",
            "files": self._collect_generated_files(workspace_dir, output_basename=output_basename).get("files") or [],
            "metadata": {
                **dict(metadata.get("metadata") or {}),
                "refine_request_path": str(request_file),
                "target_docx_path": str(target_docx),
                "input_manifest_path": str(input_manifest_file) if input_manifest_file.is_file() else "",
            },
        }
        await self._upsert_docx_job(template_service, running_job_payload)

        if not DocxRuntimeWorkerClient.enabled():
            await self._upsert_docx_job(
                template_service,
                {**running_job_payload, "status": "failed", "error": "runtime_worker_disabled"},
            )
            return ToolResult(
                success=False,
                output="runtime-worker 未启用，当前不能调用 Claude Code 修改 DOCX。",
                error="runtime_worker_disabled",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        prompt = self._build_refine_prompt(
            workspace_dir=workspace_dir,
            request_file=request_file,
            target_docx=target_docx,
            output_docx=output_docx,
            output_pdf=output_pdf,
            instruction=instruction,
        )
        before_outputs = self._workspace_doc_outputs_snapshot(workspace_dir)
        live_stream_payload: Optional[Dict[str, Any]] = None
        stream_errors: List[str] = []
        try:
            if _TOOL_LIVE_EVENT_EMITTER.get() is not None:
                async for stream_item in DocxRuntimeWorkerClient().claude_stream(
                    docx_id=docx_id,
                    workspace_dir=workspace_dir,
                    prompt=prompt,
                    continue_session=continue_session,
                ):
                    stream_item_type = str(stream_item.get("type") or "")
                    if stream_item_type == "stream_error":
                        stream_error_text = str(
                            stream_item.get("error")
                            or stream_item.get("text")
                            or "runtime-worker stream interrupted"
                        )
                        stream_errors.append(stream_error_text)
                        await emit_tool_live_event(
                            {
                                "type": "tool_output",
                                "data": {
                                    "tool": self.name,
                                    "input": {"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
                                    "stream": "stderr",
                                    "text": f"runtime-worker stream warning: {stream_error_text}\n",
                                },
                            }
                        )
                        continue
                    if stream_item_type == "chunk":
                        text = str(stream_item.get("text") or "")
                        if text:
                            await emit_tool_live_event(
                                {
                                    "type": "tool_output",
                                    "data": {
                                        "tool": self.name,
                                        "input": {"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
                                        "stream": str(stream_item.get("stream") or "stdout"),
                                        "text": text,
                                    },
                                }
                            )
                        continue
                    if stream_item_type == "result" and isinstance(stream_item.get("payload"), dict):
                        live_stream_payload = dict(stream_item.get("payload") or {})
                worker_payload = live_stream_payload or {
                    "docx_id": docx_id,
                    "workspace_dir": str(workspace_dir),
                    "prompt": prompt,
                    "continue_session": continue_session,
                    "session_id": "",
                    "assistant_text": "",
                    "result_text": "",
                    "is_error": True,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "\n".join(stream_errors),
                    "error": "docx_refine_stream_interrupted" if stream_errors else "docx_refine_stream_missing_result",
                    "worker": "runtime-worker",
                }
            else:
                worker_payload = await DocxRuntimeWorkerClient().claude(
                    docx_id=docx_id,
                    workspace_dir=workspace_dir,
                    prompt=prompt,
                    continue_session=continue_session,
                )
        except Exception as exc:
            stream_error = f"{type(exc).__name__}: {exc}"
            changed_files = self._changed_doc_outputs(workspace_dir, before_outputs)
            files = self._collect_generated_files(workspace_dir, output_basename=output_basename)
            if changed_files and files.get("docx_path"):
                validation_status = self._infer_validation_status_from_workspace(
                    workspace_dir,
                    fallback="refined_after_stream_error",
                )
                await self._upsert_docx_job(
                    template_service,
                    {
                        **running_job_payload,
                        "status": "completed",
                        "docx_path": files.get("docx_path") or "",
                        "pdf_path": files.get("pdf_path") or "",
                        "files": files.get("files") or [],
                        "validation_status": validation_status,
                        "error": f"stream_transport_error_after_refine: {stream_error}",
                        "metadata": {
                            **dict(running_job_payload.get("metadata") or {}),
                            "changed_files": changed_files,
                            "stream_error": stream_error,
                        },
                    },
                )
                return ToolResult(
                    success=True,
                    output="\n".join(
                        [
                            "Claude 已修改 DOCX，但 runtime-worker 流式连接在结束前中断；平台已从工作区变更恢复为成功。",
                            f"- Docx ID: {docx_id}",
                            f"- Workspace: {workspace_dir}",
                            f"- DOCX: {files.get('docx_path') or '(missing)'}",
                            f"- PDF: {files.get('pdf_path') or '(missing)'}",
                            f"- Changed files: {', '.join(changed_files)}",
                            f"- Stream error: {stream_error}",
                        ]
                    ),
                    data={
                        "docx_id": docx_id,
                        "workspace_dir": str(workspace_dir),
                        "docx_path": files.get("docx_path") or "",
                        "pdf_path": files.get("pdf_path") or "",
                        "changed_files": changed_files,
                        "stream_error": stream_error,
                        "worker": "runtime-worker",
                        "is_error": False,
                    },
                )
            await self._upsert_docx_job(
                template_service,
                {**running_job_payload, "status": "failed", "error": stream_error},
            )
            return ToolResult(
                success=False,
                output="\n".join(
                    [
                        "DOCX refine 调用 runtime-worker 失败。",
                        f"- Docx ID: {docx_id}",
                        f"- Workspace: {workspace_dir}",
                        f"- Error: {stream_error}",
                    ]
                ),
                error="docx_refine_worker_failed",
                data={"docx_id": docx_id, "workspace_dir": str(workspace_dir)},
            )

        changed_files = self._changed_doc_outputs(workspace_dir, before_outputs)
        files = self._collect_generated_files(workspace_dir, output_basename=output_basename)
        validation_status = self._infer_validation_status(
            worker_payload=worker_payload,
            docx_path=str(files.get("docx_path") or ""),
        )
        workspace_validation_status = self._infer_validation_status_from_workspace(workspace_dir)
        if workspace_validation_status:
            validation_status = workspace_validation_status
        result_text = str(worker_payload.get("result_text") or "").strip()
        assistant_text = str(worker_payload.get("assistant_text") or "").strip()
        rendered_text = result_text or assistant_text or "(empty)"
        stream_missing_result = str(worker_payload.get("error") or "") in {
            "docx_refine_stream_missing_result",
            "docx_refine_stream_interrupted",
        }
        success = bool(files.get("docx_path")) and (
            not bool(worker_payload.get("is_error")) or bool(changed_files and stream_missing_result)
        )
        completed_status = "completed" if success else "failed"
        completed_error = str(worker_payload.get("error") or "") or ("" if success else "docx_refine_missing_output")
        completed_job_payload = {
            **running_job_payload,
            "status": completed_status,
            "docx_path": files.get("docx_path") or "",
            "pdf_path": files.get("pdf_path") or "",
            "files": files.get("files") or [],
            "validation_status": validation_status,
            "session_id": str(worker_payload.get("session_id") or ""),
            "error": completed_error,
            "metadata": {
                **dict(running_job_payload.get("metadata") or {}),
                "assistant_text": assistant_text,
                "result_text": result_text,
                "changed_files": changed_files,
            },
        }
        await self._upsert_docx_job(template_service, completed_job_payload)
        return ToolResult(
            success=success,
            output="\n".join(
                [
                    "已通过 runtime-worker 调用 Claude Code 修改 DOCX。",
                    f"- Docx ID: {docx_id}",
                    f"- Workspace: {workspace_dir}",
                    f"- Continue session: {continue_session}",
                    f"- Session: {str(worker_payload.get('session_id') or '').strip() or '(missing)'}",
                    f"- Target DOCX: {target_docx}",
                    f"- DOCX: {files.get('docx_path') or '(missing)'}",
                    f"- PDF: {files.get('pdf_path') or '(missing)'}",
                    f"- Changed files: {', '.join(changed_files) if changed_files else '(none detected)'}",
                    f"- Validation: {validation_status}",
                    "Claude result:",
                    rendered_text,
                ]
            ),
            error=None if success else completed_error,
            data={
                "docx_id": docx_id,
                "workspace_dir": str(workspace_dir),
                "target_docx_path": str(target_docx),
                "docx_path": files.get("docx_path") or "",
                "pdf_path": files.get("pdf_path") or "",
                "changed_files": changed_files,
                "files": files.get("files") or [],
                "validation_status": validation_status,
                "session_id": str(worker_payload.get("session_id") or ""),
                "assistant_text": assistant_text,
                "result_text": result_text,
                "stdout": str(worker_payload.get("stdout") or ""),
                "stderr": str(worker_payload.get("stderr") or ""),
                "exit_code": worker_payload.get("exit_code"),
                "worker": str(worker_payload.get("worker") or "runtime-worker"),
                "is_error": bool(worker_payload.get("is_error")),
            },
        )


class PaperResearchSearchProjectZoektTool(_PaperResearchToolBase):
    name = "paper_research_search_project_zoekt"
    input_model = PaperResearchSearchProjectZoektInput
    parallel_safe = True
    output_max_tokens = 9000
    description = (
        "使用 Zoekt 对整个 Project 根目录做高性能文本检索。"
        "适合跨文件搜索代码、README、Markdown、JSON、配置文件和脚本内容，也支持文件名过滤、正则搜索、大小写控制、布尔组合、路径过滤、语言过滤和符号搜索。"
        "返回路径都是相对于 Project 根目录的路径。"
        "`project_id` 只能传 Project ID，不能传论文 `paper_id`。"
        "如果只知道 `paper_id`，先用 `paper_research_status` 或 `paper_research_prepare` 解析出 Project。"
        "重要：它是文本搜索，不是目录浏览。想看结构请用 `project_tree`；想读具体文件请用 `project_read_file`；不要把 `*` 当成“列出所有文件”的查询。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "query": {
                "type": "string",
                "description": (
                "Zoekt 查询语法。优先提供具体术语、文件名或过滤条件，不要用 `*` 试图列目录。"
                "常用写法："
                "`fastText`（普通词搜索）、"
                "`file:README`（按文件名/路径筛选）、"
                "`content:\"train_supervised\"`（按内容短语搜索）、"
                    "`regex:/train_(supervised|unsupervised)/`（正则搜索）、"
                    "`case:yes content:\"FastText\"`（大小写敏感）、"
                    "`lang:python content:\"load_model\"`（按语言过滤）、"
                    "`sym:\"FastText\"`（搜索符号）、"
                    "`file:\\.md$ reproduction`（路径/扩展名过滤）、"
                    "`(README or docs) -file:website/`（布尔组合和排除）、"
                    "`type:filename README`（只返回文件名匹配）。"
                    "做论文复现探索时，优先用任务化查询，例如："
                    "`(README or docs) supervised`、"
                    "`bucket wordNgrams dim lr epoch`、"
                    "`file:classification-results.sh test`、"
                    "`file:dictionary.cc getLine`。"
                    "如果 0 结果，先把查询缩短成一个强术语或一个具体文件名，再逐步加过滤。"
                ),
            },
            "max_results": {"type": "integer", "default": 20, "description": "最多返回多少条命中，范围 1-100。更适合有目标地搜索，不适合把整个项目全列出来。"},
            "context_lines": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 20,
                "description": "每个命中点上下各返回多少行局部上下文。想看更多命中附近内容时增大它。",
            },
            "auto_index": {"type": "boolean", "default": True, "description": "索引缺失时是否自动构建或复用 Project Zoekt 索引。通常保持 true。"},
            "force_reindex": {"type": "boolean", "default": False, "description": "搜索前是否强制重建 Project Zoekt 索引。只有怀疑索引过期时再设 true。"},
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
            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            project_dir = self._project_dir_for(project_id)
            index_payload: Dict[str, Any] = {}
            if auto_index or force_reindex:
                index_payload = await ZoektCliService.build_project_index(
                    project_dir=project_dir,
                    workspace_dir=project_dir,
                    force_reindex=force_reindex,
                )
                if not bool(index_payload.get("success")):
                    error = str(index_payload.get("error") or "zoekt_index_failed")
                    return ToolResult(
                        success=False,
                        output=(
                            "Zoekt Project 搜索前的索引准备失败。\n"
                            f"- Project: /projects/{project_id}\n"
                            f"- Error: {error}\n"
                            f"- Search binary: {index_payload.get('search_binary') or 'missing'}\n"
                            f"- Git index binary: {index_payload.get('git_index_binary') or 'missing'}\n"
                            f"- Plain index binary: {index_payload.get('plain_index_binary') or 'missing'}\n"
                            f"- Stderr: {str(index_payload.get('stderr') or '').strip() or 'none'}"
                        ),
                        error=error,
                        data={
                            **self._root_descriptor(project_payload=project_payload),
                            **dict(index_payload or {}),
                        },
                    )

            search_payload = await ZoektCliService.search_project(
                workspace_dir=project_dir,
                query=query,
                max_results=max_results,
            )
            if not bool(search_payload.get("success")):
                error = str(search_payload.get("error") or "zoekt_search_failed")
                if error == "zoekt_index_missing" and not auto_index:
                    error_message = "Project Zoekt 索引不存在。请把 auto_index 设为 true，或先执行一次 Project Zoekt 搜索来自动建索引。"
                else:
                    error_message = (
                        "Zoekt Project 搜索失败。\n"
                        f"- Error: {error}\n"
                        f"- Search binary: {search_payload.get('search_binary') or 'missing'}\n"
                        f"- Index dir: {search_payload.get('index_dir') or 'missing'}"
                    )
                return ToolResult(
                    success=False,
                    output=error_message,
                    error=error,
                    data={
                        **self._root_descriptor(project_payload=project_payload),
                        **dict(search_payload or {}),
                    },
                )

            matches = list(search_payload.get("matches") or [])
            if context_lines > 0:
                enriched_matches: List[Dict[str, Any]] = []
                for item in matches:
                    line_number = int(item.get("line_number") or 0)
                    if line_number > 0:
                        relative_path = str(item.get("source_relative_path") or item.get("relative_path") or "")
                        enriched_matches.append(
                            {
                                **item,
                                "project_relative_path": relative_path,
                                **self._build_match_context(
                                    root_dir=project_dir,
                                    relative_path=relative_path,
                                    line_number=line_number,
                                    context_lines=context_lines,
                                ),
                            }
                        )
                    else:
                        enriched_matches.append(
                            {
                                **item,
                                "project_relative_path": str(item.get("source_relative_path") or item.get("relative_path") or ""),
                            }
                        )
                matches = enriched_matches
            else:
                matches = [
                    {
                        **item,
                        "project_relative_path": str(item.get("source_relative_path") or item.get("relative_path") or ""),
                    }
                    for item in matches
                ]

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
            matched_relative_paths = list(search_payload.get("matched_relative_paths") or [])
            lines = [
                "已使用 Zoekt 搜索 Project 根目录。",
                f"- Project: /projects/{project_id}",
                f"- Query: {query}",
                f"- Auto index: {auto_index}",
                f"- Force reindex: {force_reindex}",
                f"- Index status: {index_payload.get('status') or 'unchanged'}",
                f"- Index dir: {search_payload.get('index_dir')}",
                f"- Context lines: {context_lines}",
                f"- Matched files: {len(matched_relative_paths)}",
                f"- Returned matches: {len(matches)}/{max_results}",
                f"- Truncated: {bool(search_payload.get('truncated'))}",
                "- Matches:",
                *(result_lines or ["- none"]),
            ]
            return ToolResult(
                success=True,
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload),
                    "query": query,
                    "context_lines": context_lines,
                    "auto_index": auto_index,
                    "force_reindex": force_reindex,
                    "engine": "zoekt",
                    "index_status": index_payload.get("status"),
                    "index_dir": search_payload.get("index_dir"),
                    "matched_file_count": len(matched_relative_paths),
                    "returned_matches": len(matches),
                    "truncated": bool(search_payload.get("truncated")),
                    "matches": matches,
                    "matched_files": matched_relative_paths,
                    "zoekt": {
                        **dict(index_payload or {}),
                        **dict(search_payload or {}),
                    },
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
        "轻量探测官方仓库 URL 是否仍可访问、是否可 clone，并返回默认分支等最小远程存活信号。"
        "优先使用当前 Project 已归档的 repo URL；也可以显式传 `repo_url`。"
        "这个工具只验证远程仓库状态，不会修改 Project 文件，也不会替代本地项目内的文本搜索。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "repo_url": {"type": "string", "description": "可选，显式指定要探测的官方仓库 URL；缺省时优先使用当前 Project 已归档的仓库 URL。"},
        },
        "required": ["project_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

            project_dir = self._project_dir_for(project_id)
            readme_intake = self._read_json_file(project_dir / "reference" / "repo" / "readme_intake.json")
            repo_reference = self._read_json_file(project_dir / "repo_reference.json")
            explicit_repo_url = str(kwargs.get("repo_url") or "").strip()
            intake_repo_url = str(readme_intake.get("repo_url") or "").strip()
            legacy_repo_url = str(repo_reference.get("repo_url") or "").strip()
            repo_url = explicit_repo_url or intake_repo_url or legacy_repo_url
            repo_url_source = (
                "explicit"
                if explicit_repo_url
                else "reference/repo/readme_intake.json"
                if intake_repo_url
                else "repo_reference"
                if legacy_repo_url
                else "missing"
            )
            if not repo_url:
                return ToolResult(
                    success=False,
                    output="没有可探测的 repo URL。请先提供 repo_url，或先通过 paper_research_prepare 生成 Project reference。",
                    error="repo_url_missing",
                    data={
                        **self._root_descriptor(project_payload=project_payload),
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
                        **self._root_descriptor(project_payload=project_payload),
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

            local_signals = self._repo_local_signals(project_dir)
            diagnosis = "ready" if cloneable else "repo_page_reachable_but_not_cloneable" if page_ok else "repo_unreachable"
            suggested_next_action = (
                "use_as_official_repo"
                if cloneable
                else "diagnose_official_repo_failure"
                if page_ok or status_code in {301, 302, 307, 308}
                else "do_not_execute_clone"
            )
            payload = {
                **self._root_descriptor(project_payload=project_payload),
                "project_id": project_id,
                "repo_url": repo_url,
                "source": repo_url_source,
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
        "对论文 README、论文正文或外部资源里的 URL 做轻量测活。"
        "如果直接命中文件流，会立即确认；如果返回 HTML 页面，会先做页面语义解析，再在小范围内继续 resolve 页面中的候选下载或资源链接。"
        "Google Drive 的 confirm、virus-scan 或 download gate 页面应视为“链接仍存活但还需要确认步骤”，不是 dead link；只有明确 not_found、access_denied、quota 等终态才算 blocked。"
        "它只读取响应头、极小字节片段和少量 HTML 内容，不会执行真实下载。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "url": {"type": "string", "description": "要探测的 URL。优先用于 README、论文或官方页面里给出的外部资源链接。"},
            "expected_kind": {
                "type": "string",
                "enum": ["auto", "html", "file", "hdf5", "zip", "json", "text"],
                "default": "auto",
                "description": "期望内容类型。README、项目页、文档页优先用 `html`；数据集、模型权重、压缩包、Google Drive 下载门页这类真实下载链接优先用 `file`。只有在明确需要验证特定文件格式时再用 hdf5、zip、json 或 text。",
            },
            "read_bytes": {"type": "integer", "default": 64, "description": "最多读取多少字节用于 magic-bytes 判断，范围 8-512；不会下载整个文件。"},
            "resolve_download_gate": {
                "type": "boolean",
                "default": False,
                "description": "只在明确遇到下载门页或确认页时再设 true。开启后会继续解析 Google Drive confirm、cookie、download-form 等门页，把它尽量解析到真实文件流；如果结果是 confirm_required、download_gate 或 virus-scan warning，应把它理解成“链接存活但需要确认步骤”。",
            },
        },
        "required": ["project_id", "url"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            project_id = int(kwargs["project_id"])
            project_payload = await self._resolve_project_payload_only(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)

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
                        **self._root_descriptor(project_payload=project_payload),
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
                **self._root_descriptor(project_payload=project_payload),
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


class PaperResearchLaunchClaudeCodeTool(_PaperResearchToolBase):
    name = "paper_research_launch_claude_code"
    input_model = PaperResearchLaunchClaudeCodeInput
    description = (
        "通过 LangGraph 控制面在 runtime-worker 里启动一次 Claude Code 协同执行。"
        "这个工具会自动读取 stage1 产物、生成 task brief、写 execution_spec、并启动 claude_code runtime。"
        "适合把明确的 repo 任务交给容器里的 Claude Code 自主推进，再通过 paper_research_read_execution 观察进度。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "研究项目 ID。"},
            "task": {
                "type": "string",
                "description": (
                    "要交给 Claude Code 的明确任务。应直接描述 repo 内要完成的事，例如："
                    "read the repo, run the documented baseline, fix concrete runtime blockers, and report progress."
                ),
            },
            "execution_id": {
                "type": "string",
                "description": "可选，自定义 execution_id。缺省时自动生成 claude-code-xxxx。",
            },
            "model": {
                "type": "string",
                "description": "可选，覆盖默认 Claude/百炼模型名，例如 qwen3.6-plus。",
            },
            "max_turns": {
                "type": "integer",
                "description": "可选，限制 Claude Code 本轮最大 turns 数。",
            },
            "add_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，额外授予 Claude Code 访问的工作区目录。",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，显式允许的 Claude Code tool 名单。",
            },
            "disallowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，显式禁用的 Claude Code tool 名单。",
            },
            "append_system_prompt": {
                "type": "string",
                "description": "可选，追加到 Claude Code 的 system prompt。",
            },
            "permission_mode": {
                "type": "string",
                "description": "可选，仅在 dangerously_skip_permissions=false 时使用的 Claude Code permission mode。",
            },
            "dangerously_skip_permissions": {
                "type": "boolean",
                "default": True,
                "description": "是否以 yolo/跳过权限确认模式启动 Claude Code。",
            },
        },
        "required": ["project_id", "task"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        async def _handler(db: AsyncSession) -> ToolResult:
            from app.services.execution_continuation_service import get_execution_continuation_manager

            project_id = int(kwargs["project_id"])
            project_payload, workspace = await self._resolve_project_workspace(db, project_id=project_id)
            if project_payload is None:
                return self._project_not_found(project_id)
            if workspace is None:
                return self._workspace_not_ready(project_payload, project_id)

            workspace_dir = self._workspace_dir_for(workspace)
            service = ClaudeCodeCollaborationGraphService()
            result = await service.launch(
                project_id=project_id,
                workspace_id=int(workspace.id),
                workspace_dir=workspace_dir,
                project_title=str(project_payload.get("title") or "").strip(),
                task=str(kwargs.get("task") or "").strip(),
                execution_id=str(kwargs.get("execution_id") or "").strip() or None,
                model=str(kwargs.get("model") or "").strip() or None,
                max_turns=kwargs.get("max_turns"),
                add_dirs=list(kwargs.get("add_dirs") or []),
                allowed_tools=list(kwargs.get("allowed_tools") or []),
                disallowed_tools=list(kwargs.get("disallowed_tools") or []),
                append_system_prompt=str(kwargs.get("append_system_prompt") or "").strip(),
                permission_mode=str(kwargs.get("permission_mode") or "").strip(),
                dangerously_skip_permissions=bool(kwargs.get("dangerously_skip_permissions", True)),
            )

            launch_result = dict(result.get("launch_result") or {})
            status = str(launch_result.get("status") or "").strip().lower()
            normalized_execution_id = str(result.get("execution_id") or launch_result.get("execution_id") or "").strip()

            if (
                status in {"running", "pending"}
                and self.route_profile == "chat"
                and self.conversation_id is not None
                and normalized_execution_id
            ):
                try:
                    await get_execution_continuation_manager().schedule(
                        user_id=self.user_id,
                        conversation_id=self.conversation_id,
                        project_id=project_id,
                        execution_id=normalized_execution_id,
                        stage="execution",
                        purpose="Claude Code repo collaboration",
                        active_skill_names=["paper-reproduction"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[PaperResearch] failed to schedule Claude Code continuation: conversation_id={}, execution_id={}, error={}",
                        self.conversation_id,
                        normalized_execution_id,
                        exc,
                    )

            lines = [
                "已启动 Claude Code 协同执行。",
                f"- Project: /projects/{project_id}",
                f"- Execution ID: {normalized_execution_id or 'unknown'}",
                f"- Status: {status or 'unknown'}",
                f"- Task brief: {result.get('task_brief_relative_path') or 'unknown'}",
                f"- Execution spec: {result.get('execution_spec_relative_path') or 'unknown'}",
            ]
            if result.get("launch_summary"):
                lines.append("- Launch summary:")
                lines.extend(str(result.get("launch_summary") or "").splitlines())
            if launch_result.get("message"):
                lines.append(f"- Message: {launch_result.get('message')}")
            if launch_result.get("error"):
                lines.append(f"- Error: {launch_result.get('error')}")

            return ToolResult(
                success=status in {"running", "pending", "completed"},
                output="\n".join(lines),
                data={
                    **self._root_descriptor(project_payload=project_payload, workspace=workspace),
                    **result,
                    "background_execution": {
                        "execution_id": normalized_execution_id,
                        "project_id": project_id,
                        "status": status,
                        "display_name": "Claude Code collaboration",
                        "stage": "execution",
                        "purpose": "Claude Code repo collaboration",
                        "next_action": "调用 paper_research_read_execution 观察进度与结果。",
                    },
                    "background_execution_user_summary": "\n".join(lines),
                    "background_execution_started": status in {"running", "pending"},
                    "background_execution_completed": status in {"completed", "failed", "blocked", "cancelled"},
                },
                error=None if status in {"running", "pending", "completed"} else str(launch_result.get("error") or "claude_code_launch_failed"),
            )

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


class DocumentArtifactReadInput(BaseModel):
    block_ids: List[str] = Field(
        default_factory=list,
        max_length=50,
        description="可选：只读取指定 block_id；为空时默认只列出 block 信息，不返回 Markdown 全文。",
    )
    include_constraints: bool = Field(default=True, description="是否包含整体和分块写作约束。")
    include_markdown: bool = Field(default=False, description="是否包含 block 当前 Markdown 内容；只有传入 block_ids 时才会返回 Markdown。")


class DocumentArtifactUpdateBlockInput(BaseModel):
    block_id: str = Field(..., min_length=1, max_length=120, description="要更新的 block_id。")
    markdown: str = Field(..., max_length=300000, description="写入该 block 的完整 Markdown 内容。")
    status: Optional[str] = Field(default="draft", max_length=40, description="可选状态，例如 draft/final。")


class DocumentArtifactBlockUpdateItem(BaseModel):
    block_id: str = Field(..., min_length=1, max_length=120, description="要更新的 block_id。")
    markdown: str = Field(..., max_length=300000, description="写入该 block 的完整 Markdown 内容。")
    status: Optional[str] = Field(default="draft", max_length=40, description="可选状态，例如 draft/final。")


class DocumentArtifactUpdateBlocksInput(BaseModel):
    updates: List[DocumentArtifactBlockUpdateItem] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="批量更新列表；每项包含 block_id、完整 markdown 和可选 status。",
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


class DocumentArtifactReadTool(ToolBase):
    name = "document_artifact_read"
    description = (
        "读取当前会话绑定的文档 artifact。用于按模板填写内容、修改某些章节前，查看整体约束、block 列表和现有 Markdown。"
        "优先按 block_ids 精确读取；不要默认空 block_ids 全量读取。"
        "默认是列表模式：空 block_ids 只返回 block_id、标题、heading_path、约束等列表信息，不返回 Markdown。"
        "如果不知道 block_id，直接调用本工具列出 block 信息，再按需要的 block_id 二次读取。"
        "返回中包含 artifact_path；如果只是要交给 docx_generate_with_claude 生成 DOCX，优先传 artifact_id/template_id，"
        "不要为了 DOCX 生成把 include_markdown=true 的全文读进上下文。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "block_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "推荐填写：只读取指定 block_id。为空时默认列出所有 block 的标题/路径/约束，不返回 Markdown。",
                "default": [],
            },
            "include_constraints": {
                "type": "boolean",
                "description": "是否返回整体/分块写作约束。",
                "default": True,
            },
            "include_markdown": {
                "type": "boolean",
                "description": "是否返回当前 Markdown 内容。只有传入 block_ids 时才会返回 Markdown；未知 block_id 时保持 false 列 block 标题。",
                "default": False,
            },
        },
        "required": [],
    }
    input_model = DocumentArtifactReadInput
    retry_count = 0
    output_max_tokens = 5000

    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        *,
        conversation_id: Optional[int],
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = int(user_id)
        self.conversation_id = int(conversation_id) if conversation_id is not None else None
        self.db_session_factory = db_session_factory

    async def _with_db(self, handler: Callable[[AsyncSession], Any]) -> ToolResult:
        if self.db is not None:
            return await handler(self.db)
        if self.db_session_factory is None:
            return ToolResult(success=False, output="document artifact 工具不可用：数据库会话未初始化。", error="db_unavailable")
        async with self.db_session_factory() as session:
            return await handler(session)

    async def _execute(
        self,
        block_ids: Optional[List[str]] = None,
        include_constraints: bool = True,
        include_markdown: bool = False,
    ) -> ToolResult:
        if self.conversation_id is None:
            return ToolResult(success=False, output="document_artifact_read 只能在绑定会话的 chat 回合中使用。", error="conversation_required")

        normalized_block_ids = [str(item).strip() for item in list(block_ids or []) if str(item).strip()]
        requested_markdown_without_ids = bool(include_markdown) and not normalized_block_ids
        effective_include_markdown = bool(include_markdown) and bool(normalized_block_ids)

        async def handler(db: AsyncSession) -> ToolResult:
            from app.services.document_artifact_service import DocumentArtifactService

            try:
                payload = await DocumentArtifactService().read_blocks_for_tool(
                    db,
                    user_id=self.user_id,
                    conversation_id=int(self.conversation_id),
                    block_ids=normalized_block_ids,
                    include_constraints=include_constraints,
                    include_markdown=effective_include_markdown,
                )
            except ValueError as exc:
                return ToolResult(success=False, output=str(exc), error="document_artifact_unavailable")
            if requested_markdown_without_ids:
                payload["notice"] = (
                    "空 block_ids 默认按列表模式返回，未返回 Markdown 全文。"
                    "请从 blocks 中选择需要的 block_id 后，再用 include_markdown=true 精确读取。"
                )
                payload["list_mode"] = True
            if len(normalized_block_ids) > 1 and effective_include_markdown:
                payload["workflow_hint"] = (
                    "本次已读取多个 block 的 Markdown。若接下来需要同时补写/扩写多个模块，"
                    "请先形成多块写入计划，并优先调用 document_artifact_update_blocks 一次提交多个 block；"
                    "不要把更新 JSON 或 Markdown 当普通回答输出。"
                )
            return ToolResult(
                success=True,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                data=payload,
            )

        return await self._with_db(handler)


class DocumentArtifactUpdateBlockTool(ToolBase):
    name = "document_artifact_update_block"
    description = (
        "更新当前会话文档 artifact 的一个 block。只能写入当前 active artifact 内已有 block_id 的完整 Markdown，不创建新文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "block_id": {
                "type": "string",
                "description": "要更新的 block_id，先用 document_artifact_read 查看可用 block。",
            },
            "markdown": {
                "type": "string",
                "description": "该 block 的完整 Markdown 内容。调用时应提交完整块内容，而不是 diff。",
            },
            "status": {
                "type": "string",
                "description": "可选状态，例如 draft/final。",
                "default": "draft",
            },
        },
        "required": ["block_id", "markdown"],
    }
    input_model = DocumentArtifactUpdateBlockInput
    retry_count = 0
    output_max_tokens = 1200

    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        *,
        conversation_id: Optional[int],
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = int(user_id)
        self.conversation_id = int(conversation_id) if conversation_id is not None else None
        self.db_session_factory = db_session_factory

    async def _with_db(self, handler: Callable[[AsyncSession], Any]) -> ToolResult:
        if self.db is not None:
            return await handler(self.db)
        if self.db_session_factory is None:
            return ToolResult(success=False, output="document artifact 工具不可用：数据库会话未初始化。", error="db_unavailable")
        async with self.db_session_factory() as session:
            return await handler(session)

    async def _execute(self, block_id: str, markdown: str, status: str = "draft") -> ToolResult:
        if self.conversation_id is None:
            return ToolResult(success=False, output="document_artifact_update_block 只能在绑定会话的 chat 回合中使用。", error="conversation_required")

        async def handler(db: AsyncSession) -> ToolResult:
            from app.services.document_artifact_service import DocumentArtifactService

            try:
                artifact = await DocumentArtifactService().update_block(
                    db,
                    user_id=self.user_id,
                    conversation_id=int(self.conversation_id),
                    block_id=block_id,
                    markdown=markdown,
                    status=status,
                )
            except ValueError as exc:
                return ToolResult(success=False, output=str(exc), error="document_artifact_update_failed")
            blocks = list(artifact.get("blocks") or [])
            block_count = len(blocks)
            updated_block = next(
                (
                    block
                    for block in blocks
                    if isinstance(block, dict) and str(block.get("block_id") or "") == str(block_id)
                ),
                None,
            )
            output = "\n".join(
                [
                    "已更新文档 artifact block。",
                    f"- artifact_id: {artifact.get('artifact_id')}",
                    f"- block_id: {block_id}",
                    f"- blocks: {block_count}",
                    f"- updated_at: {artifact.get('updated_at')}",
                ]
            )
            return ToolResult(
                success=True,
                output=output,
                data={
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact": {
                        "artifact_id": artifact.get("artifact_id"),
                        "template_id": artifact.get("template_id"),
                        "title": artifact.get("title"),
                        "block_count": block_count,
                        "updated_at": artifact.get("updated_at"),
                    },
                    "block_id": block_id,
                    "block": updated_block,
                    "updated_at": artifact.get("updated_at"),
                },
            )

        return await self._with_db(handler)


class DocumentArtifactUpdateBlocksTool(ToolBase):
    name = "document_artifact_update_blocks"
    description = (
        "批量更新当前会话文档 artifact 的多个 block。适合一次读取多个 block 后，按同一计划一次提交多块更新。"
        "每个 update 都必须包含已有 block_id 和该 block 的完整 Markdown；后端会先校验全部 block，再一次性写入。"
        "不要把准备写入的 JSON 或 Markdown 当作普通回答输出。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "updates": {
                "type": "array",
                "description": "批量更新列表。每项写一个 block；多块扩写/补全时优先使用本工具，避免连续单块写入时丢失流程。",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_id": {
                            "type": "string",
                            "description": "要更新的 block_id，必须来自当前 artifact。",
                        },
                        "markdown": {
                            "type": "string",
                            "description": "该 block 的完整 Markdown 内容，不是 diff。",
                        },
                        "status": {
                            "type": "string",
                            "description": "可选状态，例如 draft/final。",
                            "default": "draft",
                        },
                    },
                    "required": ["block_id", "markdown"],
                },
                "minItems": 1,
                "maxItems": 20,
            }
        },
        "required": ["updates"],
    }
    input_model = DocumentArtifactUpdateBlocksInput
    retry_count = 0
    output_max_tokens = 1600

    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        *,
        conversation_id: Optional[int],
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = int(user_id)
        self.conversation_id = int(conversation_id) if conversation_id is not None else None
        self.db_session_factory = db_session_factory

    async def _with_db(self, handler: Callable[[AsyncSession], Any]) -> ToolResult:
        if self.db is not None:
            return await handler(self.db)
        if self.db_session_factory is None:
            return ToolResult(success=False, output="document artifact 工具不可用：数据库会话未初始化。", error="db_unavailable")
        async with self.db_session_factory() as session:
            return await handler(session)

    async def _execute(self, updates: List[Dict[str, Any]]) -> ToolResult:
        if self.conversation_id is None:
            return ToolResult(success=False, output="document_artifact_update_blocks 只能在绑定会话的 chat 回合中使用。", error="conversation_required")

        normalized_updates = [dict(item or {}) for item in list(updates or [])]

        async def handler(db: AsyncSession) -> ToolResult:
            from app.services.document_artifact_service import DocumentArtifactService

            try:
                result = await DocumentArtifactService().update_blocks(
                    db,
                    user_id=self.user_id,
                    conversation_id=int(self.conversation_id),
                    updates=normalized_updates,
                )
            except ValueError as exc:
                return ToolResult(success=False, output=str(exc), error="document_artifact_update_failed")

            artifact = dict(result.get("artifact") or {})
            updated_blocks = [dict(item) for item in list(result.get("updated_blocks") or []) if isinstance(item, dict)]
            block_ids = [str(block.get("block_id") or "") for block in updated_blocks if str(block.get("block_id") or "")]
            block_lines = [
                f"- {block.get('block_id')}: {len(str(block.get('markdown') or ''))} chars"
                for block in updated_blocks
            ]
            output = "\n".join(
                [
                    "已批量更新文档 artifact blocks。",
                    f"- artifact_id: {artifact.get('artifact_id')}",
                    f"- updated_blocks: {len(updated_blocks)}",
                    f"- updated_at: {artifact.get('updated_at')}",
                    *block_lines,
                ]
            )
            return ToolResult(
                success=True,
                output=output,
                data={
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact": {
                        "artifact_id": artifact.get("artifact_id"),
                        "template_id": artifact.get("template_id"),
                        "title": artifact.get("title"),
                        "block_count": len(list(artifact.get("blocks") or [])),
                        "updated_at": artifact.get("updated_at"),
                    },
                    "block_ids": block_ids,
                    "blocks": updated_blocks,
                    "updated_at": artifact.get("updated_at"),
                },
            )

        return await self._with_db(handler)


class LiteratureSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    source: str = Field(default="auto")
    max_results: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    page_token: Optional[str] = Field(default=None, max_length=2000)
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    fields: Optional[List[str]] = None
    open_access: bool = False
    sort_by: Optional[str] = None
    sort_order: str = "desc"
    abstract_max_chars: int = Field(default=800, ge=0, le=6000)

    @field_validator("fields", mode="before")
    @classmethod
    def _normalize_fields(cls, value: Any) -> Optional[List[str]]:
        if value is None:
            return None
        if isinstance(value, str):
            items = [item.strip() for item in re.split(r"[,;，；]", value) if item.strip()]
            return items or None
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            items = [str(item).strip() for item in list(value) if str(item or "").strip()]
            return items or None
        return None


class LiteratureSearchTool(ToolBase):
    """学术文献搜索工具 - 自动在多个学术数据源间回退。"""
    name = "literature_search"
    parallel_safe = True
    description = "搜索学术论文和文献。默认使用 auto，在 OpenAlex、Semantic Scholar、arXiv、PubMed、CrossRef 间自动回退。适用于学术研究、文献综述、找相关论文等场景。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，可以是论文标题、作者名、研究主题等"
            },
            "source": {
                "type": "string",
                "description": "数据源: auto（默认，自动回退）、semantic_scholar、arxiv、pubmed、openalex、crossref",
                "enum": ["auto", "semantic_scholar", "arxiv", "pubmed", "openalex", "crossref"],
                "default": "auto"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认10，最多100。做综述时可提高到20-50，再配合 offset/page_token 翻页。",
                "default": 10
            },
            "offset": {
                "type": "integer",
                "description": "偏移量。支持 offset 分页的数据源可用；Semantic Scholar/CrossRef 优先使用 page_token。",
                "default": 0
            },
            "page_token": {
                "type": "string",
                "description": "续页 token/cursor。Semantic Scholar、CrossRef 等支持时会返回 next_token，可传回继续搜索。"
            },
            "year_start": {
                "type": "integer",
                "description": "起始年份过滤（可选）"
            },
            "year_end": {
                "type": "integer",
                "description": "结束年份过滤（可选）"
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "研究领域过滤。Semantic Scholar 用 fieldsOfStudy；arXiv 可传 cs.AI/cs.LG 等分类；其他源会尽量忽略或降级。"
            },
            "open_access": {
                "type": "boolean",
                "description": "仅返回开放获取或有开放许可倾向的结果；不同数据源支持程度不同。",
                "default": False
            },
            "sort_by": {
                "type": "string",
                "description": "排序字段：relevance, latest, citations, updated, submitted, recent, title, author, journal, references。",
                "enum": ["relevance", "latest", "citations", "updated", "submitted", "recent", "title", "author", "journal", "references"]
            },
            "sort_order": {
                "type": "string",
                "description": "排序方向：desc 或 asc。",
                "enum": ["desc", "asc"],
                "default": "desc"
            },
            "abstract_max_chars": {
                "type": "integer",
                "description": "每条结果在工具输出中保留的摘要字符数，默认800，最多6000。完整摘要也会保留在结构化 data 中。",
                "default": 800
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
        max_results: int = 10,
        offset: int = 0,
        page_token: Optional[str] = None,
        year_start: int = None,
        year_end: int = None,
        fields: Optional[List[str]] = None,
        open_access: bool = False,
        sort_by: Optional[str] = None,
        sort_order: str = "desc",
        abstract_max_chars: int = 800,
    ) -> ToolResult:
        """执行学术文献搜索"""
        logger.info(f"[LiteratureSearch] 搜索: {query}, source={source}")

        try:
            kwargs = {}
            if year_start is not None or year_end is not None:
                kwargs["year_range"] = (year_start, year_end)
            if fields:
                kwargs["fields_of_study"] = [str(item).strip() for item in list(fields or []) if str(item).strip()]
            if open_access:
                kwargs["open_access_only"] = True
            if page_token:
                kwargs["page_token"] = page_token
            if sort_by:
                kwargs["sort_by"] = sort_by
                kwargs["sort_order"] = sort_order

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
                    offset=offset,
                    year_range=kwargs.get("year_range"),
                    fields_of_study=kwargs.get("fields_of_study"),
                    open_access_only=kwargs.get("open_access_only", False),
                    sort_by=kwargs.get("sort_by"),
                    sort_order=kwargs.get("sort_order"),
                )
                papers = result.get("papers", [])[:max_results]
                result["papers"] = papers
            else:
                result = await self.service.search(
                    query=query,
                    source=source,
                    limit=max_results,
                    offset=offset,
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
                        "offset": offset,
                        "next_token": result.get("next_token"),
                    },
                )

            # 格式化输出
            output = self._format_results(query, resolved_source, papers, abstract_max_chars=abstract_max_chars)

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
                    "total": result.get("total", len(papers)),
                    "offset": result.get("offset", offset),
                    "has_more": result.get("has_more"),
                    "next_token": result.get("next_token"),
                }
            )

        except Exception as e:
            logger.error(f"[LiteratureSearch] 搜索错误: {e}")
            return ToolResult(
                success=False,
                output=f"文献搜索错误: {str(e)}",
                error=str(e)
            )

    def _format_results(self, query: str, source: str, papers: list, *, abstract_max_chars: int = 800) -> str:
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
                max_chars = max(0, int(abstract_max_chars or 0))
                abstract = str(paper.abstract or "")
                if max_chars and len(abstract) > max_chars:
                    abstract = abstract[:max_chars] + "..."
                elif max_chars <= 0:
                    abstract = ""
                if abstract:
                    output_parts.append(f"\n摘要: {abstract}")

            if paper.doi:
                output_parts.append(f"\nDOI: {paper.doi}")

            if paper.arxiv_id:
                output_parts.append(f"\narXiv: {paper.arxiv_id}")

            if paper.pdf_url:
                output_parts.append(f"\nPDF: {paper.pdf_url}")

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


class _LiteratureReviewWorkspaceMixin:
    """Shared path and manifest helpers for literature review tools."""

    def __init__(self, user_id: Optional[int] = None):
        self.user_id = int(user_id) if user_id is not None else None

    @staticmethod
    def _upload_root() -> Path:
        configured = str(os.getenv("UPLOAD_DIR") or "").strip()
        if configured:
            return Path(os.path.abspath(configured))
        mounted = Path("/app/uploads")
        if mounted.exists():
            return mounted.resolve()
        return Path(os.path.abspath("./uploads"))

    @staticmethod
    def _safe_slug(value: Any, *, fallback: str) -> str:
        text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
        return (text or fallback)[:160]

    @classmethod
    def _new_review_id(cls) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return cls._safe_slug(f"review-{timestamp}-{uuid.uuid4().hex[:8]}", fallback="review")

    @classmethod
    def _review_id(cls, raw: Any) -> str:
        return cls._safe_slug(raw, fallback="") or cls._new_review_id()

    @classmethod
    def _review_root_for(cls, literature_review_id: str) -> Path:
        review_id = cls._review_id(literature_review_id)
        return cls._upload_root() / "literature_reviews" / review_id

    @staticmethod
    def _ensure_review_dirs(root: Path) -> None:
        for name in ("searches", "pdf", "md", "review"):
            (root / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _manifest_path(root: Path) -> Path:
        return root / "manifest.json"

    @classmethod
    def _load_manifest(cls, root: Path) -> Dict[str, Any]:
        path = cls._manifest_path(root)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _write_manifest(cls, root: Path, manifest: Dict[str, Any]) -> None:
        cls._ensure_review_dirs(root)
        payload = dict(manifest or {})
        payload["updated_at"] = datetime.utcnow().isoformat()
        cls._manifest_path(root).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    @classmethod
    def _update_manifest_paper(cls, root: Path, paper_key: str, values: Dict[str, Any]) -> Dict[str, Any]:
        manifest = cls._load_manifest(root)
        papers = manifest.get("papers")
        if not isinstance(papers, dict):
            papers = {}
        existing = dict(papers.get(paper_key) or {}) if isinstance(papers.get(paper_key), dict) else {}
        existing.update({key: value for key, value in dict(values or {}).items() if value is not None})
        existing["paper_key"] = paper_key
        existing["updated_at"] = datetime.utcnow().isoformat()
        papers[paper_key] = existing
        manifest["papers"] = papers
        cls._write_manifest(root, manifest)
        return existing

    @classmethod
    def _path_under_root(cls, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    @classmethod
    def _resolve_review_file(cls, root: Path, raw_path: str, *, default_subdir: str) -> Optional[Path]:
        raw = str(raw_path or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            if "/" not in raw.replace("\\", "/"):
                candidate = root / default_subdir / raw
            else:
                candidate = root / raw
        if not cls._path_under_root(candidate, root):
            return None
        resolved = candidate.resolve()
        return resolved if resolved.is_file() else None

    @classmethod
    def _paper_key_from_metadata(cls, *, title: Any = None, doi: Any = None, arxiv_id: Any = None, external_id: Any = None) -> str:
        seed = str(doi or arxiv_id or external_id or title or "").strip()
        if not seed:
            seed = f"paper-{uuid.uuid4().hex[:8]}"
        return cls._safe_slug(seed.lower(), fallback=f"paper-{uuid.uuid4().hex[:8]}")

    @staticmethod
    def _compact_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _metadata_value(cls, metadata: Dict[str, Any], key: str) -> str:
        value = dict(metadata or {}).get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        return cls._compact_text(value)

    @classmethod
    def _author_names(cls, metadata: Dict[str, Any]) -> List[str]:
        raw_authors = dict(metadata or {}).get("authors")
        if not isinstance(raw_authors, list):
            return []
        names: List[str] = []
        for item in raw_authors:
            if isinstance(item, dict):
                name = cls._compact_text(
                    item.get("name")
                    or item.get("display_name")
                    or item.get("author")
                    or item.get("full_name")
                )
            else:
                name = cls._compact_text(item)
            if name:
                names.append(name)
        return names

    @staticmethod
    def _doi_url(doi: str) -> str:
        value = str(doi or "").strip()
        if not value:
            return ""
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            return value
        value = re.sub(r"^(?:doi:\s*)", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"^(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE).strip()
        return f"https://doi.org/{value}" if value else ""

    @staticmethod
    def _bibtex_escape(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    @classmethod
    def _citation_strings(cls, *, paper_key: str, metadata: Dict[str, Any]) -> Dict[str, str]:
        authors = cls._author_names(metadata)
        authors_text = ", ".join(authors) if authors else "作者未提供"
        bibtex_authors = " and ".join(authors)
        title = cls._metadata_value(metadata, "title") or "题名未提供"
        year = cls._metadata_value(metadata, "year") or "年份未提供"
        venue = cls._metadata_value(metadata, "venue")
        doi = cls._metadata_value(metadata, "doi")
        doi_url = cls._doi_url(doi)
        arxiv_id = cls._metadata_value(metadata, "arxiv_id")
        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
        url = cls._metadata_value(metadata, "url")
        pdf_url = cls._metadata_value(metadata, "pdf_url")
        best_link = doi_url or url or arxiv_url or pdf_url

        apa_parts = [f"{authors_text}.", f"({year}).", f"{title}."]
        if venue:
            apa_parts.append(f"{venue}.")
        if best_link:
            apa_parts.append(best_link)
        apa = " ".join(apa_parts)

        gbt_parts = [f"{authors_text}.", f"{title}[J/OL]."]
        if venue:
            gbt_parts.append(f"{venue},")
        gbt_parts.append(f"{year}.")
        if doi:
            gbt_parts.append(f"DOI: {doi}.")
        elif best_link:
            gbt_parts.append(best_link)
        gbt = " ".join(gbt_parts)

        bibtex_key = cls._safe_slug(paper_key, fallback="paper").replace(".", "-")
        bibtex_fields = [f"  title = {{{cls._bibtex_escape(title)}}}"]
        if bibtex_authors:
            bibtex_fields.append(f"  author = {{{cls._bibtex_escape(bibtex_authors)}}}")
        if year != "年份未提供":
            bibtex_fields.append(f"  year = {{{cls._bibtex_escape(year)}}}")
        if venue:
            bibtex_fields.append(f"  journal = {{{cls._bibtex_escape(venue)}}}")
        if doi:
            bibtex_fields.append(f"  doi = {{{cls._bibtex_escape(doi)}}}")
        if best_link:
            bibtex_fields.append(f"  url = {{{cls._bibtex_escape(best_link)}}}")
        bibtex = "@misc{" + bibtex_key + ",\n" + ",\n".join(bibtex_fields) + "\n}"
        return {"apa": apa, "gbt7714": gbt, "bibtex": bibtex}

    @classmethod
    def _paper_metadata_for_path(cls, root: Path, *, paper_key: str = "", relative_path: str = "") -> Dict[str, Any]:
        manifest = cls._load_manifest(root)
        papers = manifest.get("papers") if isinstance(manifest, dict) else {}
        if not isinstance(papers, dict):
            return {}

        normalized_key = cls._safe_slug(paper_key, fallback="")
        if normalized_key and isinstance(papers.get(normalized_key), dict):
            metadata = dict(papers.get(normalized_key) or {})
            metadata.setdefault("paper_key", normalized_key)
            return metadata

        normalized_rel = str(relative_path or "").replace("\\", "/").strip("/")
        normalized_name = Path(normalized_rel).name
        for key, value in papers.items():
            if not isinstance(value, dict):
                continue
            metadata = dict(value or {})
            candidate_paths = [
                metadata.get("md_path"),
                metadata.get("review_path"),
                metadata.get("pdf_path"),
            ]
            for candidate in candidate_paths:
                candidate_rel = str(candidate or "").replace("\\", "/").strip()
                if not candidate_rel:
                    continue
                if normalized_rel and (
                    candidate_rel.endswith(normalized_rel)
                    or candidate_rel.endswith(f"/{normalized_rel}")
                    or Path(candidate_rel).name == normalized_name
                ):
                    metadata.setdefault("paper_key", str(key))
                    return metadata
        return {}

    @classmethod
    def _paper_identity(cls, root: Path, *, paper_key: str = "", relative_path: str = "") -> Dict[str, Any]:
        resolved_key = cls._safe_slug(paper_key or Path(str(relative_path or "")).stem, fallback="")
        metadata = cls._paper_metadata_for_path(root, paper_key=resolved_key, relative_path=relative_path)
        if metadata.get("paper_key"):
            resolved_key = cls._safe_slug(metadata.get("paper_key"), fallback=resolved_key)
        doi = cls._metadata_value(metadata, "doi")
        arxiv_id = cls._metadata_value(metadata, "arxiv_id")
        doi_url = cls._doi_url(doi)
        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
        link = cls._metadata_value(metadata, "url") or doi_url or arxiv_url or cls._metadata_value(metadata, "pdf_url")
        citations = cls._citation_strings(paper_key=resolved_key or "paper", metadata=metadata)
        return {
            "paper_key": resolved_key,
            "title": cls._metadata_value(metadata, "title") or resolved_key or "未提供",
            "authors": cls._author_names(metadata),
            "year": cls._metadata_value(metadata, "year"),
            "venue": cls._metadata_value(metadata, "venue"),
            "doi": doi,
            "link": link,
            "pdf_url": cls._metadata_value(metadata, "pdf_url"),
            "citation_apa": citations["apa"],
            "citation_gbt7714": citations["gbt7714"],
            "citation_bibtex": citations["bibtex"],
            "metadata": metadata,
        }


class LiteratureReviewStartTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "literature_review_start"
    input_model = LiteratureReviewStartInput
    parallel_safe = False
    description = (
        "创建或恢复论文综述任务目录。工作根目录固定为 "
        "`/app/uploads/literature_reviews/{literature_review_id}`，不属于 Project，也不绑定知识库。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "literature_review_id": {"type": "string", "description": "可选综述任务 ID。未提供时自动生成。"},
            "topic": {"type": "string", "description": "明确的综述主题。没有明确主题时应先询问用户。"},
            "target_paper_count": {"type": "integer", "default": 12, "description": "目标可读全文论文数，默认 12。"},
            "notes": {"type": "string", "description": "可选补充要求。"},
        },
        "required": ["topic"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        self._ensure_review_dirs(root)
        manifest = self._load_manifest(root)
        manifest.update(
            {
                "literature_review_id": review_id,
                "topic": str(kwargs.get("topic") or "").strip(),
                "target_paper_count": int(kwargs.get("target_paper_count") or 12),
                "notes": str(kwargs.get("notes") or "").strip(),
                "user_id": self.user_id,
                "created_at": manifest.get("created_at") or datetime.utcnow().isoformat(),
            }
        )
        self._write_manifest(root, manifest)
        return ToolResult(
            success=True,
            output="\n".join(
                [
                    "论文综述任务已准备。",
                    f"- literature_review_id: {review_id}",
                    f"- topic: {manifest['topic']}",
                    f"- target_paper_count: {manifest['target_paper_count']}",
                    f"- root: {root}",
                    "- directories: pdf/, md/, review/",
                ]
            ),
            data={
                "literature_review_id": review_id,
                "root": str(root),
                "pdf_dir": str(root / "pdf"),
                "md_dir": str(root / "md"),
                "review_dir": str(root / "review"),
                "manifest_path": str(self._manifest_path(root)),
                "topic": manifest["topic"],
                "target_paper_count": manifest["target_paper_count"],
            },
        )


class LiteratureReviewDownloadPdfTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "literature_review_download_pdf"
    input_model = LiteratureReviewDownloadPdfInput
    timeout_seconds = 180.0
    retry_count = 0
    parallel_safe = False
    description = (
        "把综述候选论文 PDF 下载到 `/app/uploads/literature_reviews/{literature_review_id}/pdf/`。"
        "会按论文页下载逻辑尝试 arXiv 与直连 PDF 候选；不会加入知识库，也不会写 Project。"
    )
    parameters = {
        "type": "object",
            "properties": {
                "literature_review_id": {"type": "string", "description": "综述任务 ID。"},
                "pdf_url": {"type": "string", "description": "直连 PDF URL。"},
                "arxiv_id": {"type": "string", "description": "可选 arXiv ID；缺少 pdf_url 时会生成 arXiv PDF 链接。"},
                "title": {"type": "string", "description": "论文标题，用于 manifest 和文件名。"},
                "abstract": {"type": "string", "description": "搜索接口返回的原始摘要；review_writer 会按元数据引用，不让模型补写。"},
                "source": {"type": "string", "description": "搜索来源，如 openalex/arxiv/semantic_scholar。"},
                "external_id": {"type": "string", "description": "外部论文 ID。"},
                "doi": {"type": "string", "description": "DOI。"},
                "url": {"type": "string", "description": "论文页面链接。"},
                "venue": {"type": "string", "description": "期刊/会议/来源名称。"},
                "year": {"type": "integer", "description": "发表年份。"},
                "authors": {"type": "array", "items": {"type": "object"}, "description": "作者列表。"},
                "citation_count": {"type": "integer", "description": "搜索接口返回的引用数。"},
                "reference_count": {"type": "integer", "description": "搜索接口返回的参考文献数。"},
                "fields_of_study": {"type": "array", "items": {"type": "string"}, "description": "搜索接口返回的学科/领域标签。"},
                "paper_key": {"type": "string", "description": "可选稳定论文 key。未提供时根据 DOI/arXiv/title 生成。"},
                "overwrite": {"type": "boolean", "default": False, "description": "是否覆盖已下载 PDF。"},
            },
        "required": ["literature_review_id"],
    }
    _MDPI_ISSN_SLUGS: Dict[str, str] = {
        "1424-8220": "sensors",
        "2072-4292": "remotesensing",
        "2073-4395": "agronomy",
    }
    _MDPI_CODE_SLUGS: Dict[str, str] = {
        "s": "sensors",
        "rs": "remotesensing",
        "agronomy": "agronomy",
        "sustainability": "sustainability",
        "applsci": "applsci",
        "plants": "plants",
        "agriculture": "agriculture",
        "animals": "animals",
        "water": "water",
        "energies": "energies",
        "ijms": "ijms",
        "ijerph": "ijerph",
        "foods": "foods",
    }
    _MDPI_VENUE_SLUGS: Dict[str, str] = {
        "remote sensing": "remotesensing",
        "sensors": "sensors",
        "agronomy": "agronomy",
    }

    @staticmethod
    def _normalize_arxiv_id(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw.startswith("arxiv:"):
            raw = raw[6:]
        if "v" in raw and raw.rsplit("v", 1)[-1].isdigit():
            raw = raw.rsplit("v", 1)[0]
        return raw

    @classmethod
    def _extract_arxiv_id_from_text(cls, value: Any) -> Optional[str]:
        token = unquote(str(value or "").strip())
        if not token:
            return None

        url_patterns = (
            r"10\.48550/arxiv\.([^\s\"'<>?#]+)",
            r"arxiv(?:\.org)?/(?:abs|pdf|html)/([^/?#]+)",
            r"\barxiv:\s*([^\s]+)",
        )
        for pattern in url_patterns:
            match = re.search(pattern, token, flags=re.IGNORECASE)
            if match:
                return cls._normalize_arxiv_id(match.group(1).removesuffix(".pdf").rstrip(").,;]}"))

        direct = token.strip().removesuffix(".pdf")
        if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z\-]+)?/\d{7})(?:v\d+)?", direct, flags=re.IGNORECASE):
            return cls._normalize_arxiv_id(direct)
        return None

    @classmethod
    def _infer_arxiv_id_from_candidates(cls, *values: Any) -> Optional[str]:
        for value in values:
            arxiv_id = cls._extract_arxiv_id_from_text(value)
            if arxiv_id:
                return arxiv_id
        return None

    @classmethod
    def _build_arxiv_pdf_url(cls, arxiv_id: Any) -> Optional[str]:
        normalized = cls._normalize_arxiv_id(arxiv_id)
        if not normalized:
            return None
        return f"https://arxiv.org/pdf/{normalized}"

    @classmethod
    def _mdpi_slug_from_metadata(cls, venue: Any, doi: Any, issn: str = "") -> str:
        venue_key = re.sub(r"\s+", " ", str(venue or "").strip().lower())
        if venue_key in cls._MDPI_VENUE_SLUGS:
            return cls._MDPI_VENUE_SLUGS[venue_key]
        compact_venue = re.sub(r"[^a-z0-9]+", "", venue_key)
        if compact_venue:
            return compact_venue

        if issn and issn in cls._MDPI_ISSN_SLUGS:
            return cls._MDPI_ISSN_SLUGS[issn]

        doi_tail = re.sub(
            r"^(?:https?://)?(?:dx\.)?doi\.org/10\.3390/",
            "",
            str(doi or "").strip().lower(),
            flags=re.IGNORECASE,
        )
        doi_tail = re.sub(r"^10\.3390/", "", doi_tail, flags=re.IGNORECASE)
        match = re.match(r"([a-z]+)", doi_tail)
        if match:
            code = match.group(1)
            return cls._MDPI_CODE_SLUGS.get(code, code)
        return ""

    @classmethod
    def _build_mdpi_pdf_candidates(cls, kwargs: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        for value in (kwargs.get("pdf_url"), kwargs.get("url"), kwargs.get("external_id")):
            token = str(value or "").strip()
            if not token:
                continue
            parsed = urlparse(token)
            if "mdpi.com" not in parsed.netloc.lower():
                continue
            match = re.search(
                r"/(?P<issn>\d{4}-\d{3}[\dXx])/(?P<volume>\d+)/(?P<issue>\d+)/(?P<article>\d+)/pdf\b",
                parsed.path,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            slug = cls._mdpi_slug_from_metadata(kwargs.get("venue"), kwargs.get("doi"), match.group("issn"))
            if not slug:
                continue
            volume = str(int(match.group("volume")))
            article = match.group("article").lstrip("0") or "0"
            article_tokens = [article.zfill(5)]
            if article_tokens[0] != article:
                article_tokens.append(article)
            for article_token in article_tokens:
                base = f"{slug}-{volume}-{article_token}"
                for suffix in ("", "-v2", "-v3"):
                    candidates.append(
                        f"https://mdpi-res.com/d_attachment/{slug}/{base}/article_deploy/{base}{suffix}.pdf"
                    )
        return candidates

    @classmethod
    def _extract_ieee_arnumber(cls, *values: Any) -> Optional[str]:
        for value in values:
            token = unquote(str(value or "").strip())
            if not token:
                continue
            for pattern in (
                r"[?&]arnumber=(\d+)",
                r"/document/(\d+)",
                r"/0*(\d{7,8})\.pdf\b",
            ):
                match = re.search(pattern, token, flags=re.IGNORECASE)
                if match:
                    return str(int(match.group(1)))
        return None

    @classmethod
    def _build_ieee_pdf_candidates(cls, kwargs: Dict[str, Any]) -> List[str]:
        arnumber = cls._extract_ieee_arnumber(kwargs.get("pdf_url"), kwargs.get("url"), kwargs.get("external_id"))
        if not arnumber:
            return []
        return [f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"]

    @classmethod
    def _build_pdf_download_candidates(cls, kwargs: Dict[str, Any]) -> List[str]:
        arxiv_id = cls._infer_arxiv_id_from_candidates(
            kwargs.get("arxiv_id"),
            kwargs.get("external_id") if str(kwargs.get("source") or "").strip().lower() == "arxiv" else None,
            kwargs.get("pdf_url"),
            kwargs.get("url"),
            kwargs.get("doi"),
        )

        candidates: List[str] = []
        if arxiv_id:
            arxiv_pdf_url = cls._build_arxiv_pdf_url(arxiv_id)
            if arxiv_pdf_url:
                candidates.append(arxiv_pdf_url)

        candidates.extend(cls._build_mdpi_pdf_candidates(kwargs))

        direct_pdf_url = str(kwargs.get("pdf_url") or "").strip()
        if direct_pdf_url:
            candidates.append(direct_pdf_url)

        for candidate in (
            kwargs.get("url"),
            kwargs.get("external_id"),
        ):
            token = str(candidate or "").strip()
            if token.lower().split("?", 1)[0].endswith(".pdf"):
                candidates.append(token)

        candidates.extend(cls._build_ieee_pdf_candidates(kwargs))

        unique_candidates: List[str] = []
        seen: Set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_candidates.append(normalized)
        return unique_candidates

    async def _execute(self, **kwargs) -> ToolResult:
        from app.services.literature_service import get_literature_service

        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        self._ensure_review_dirs(root)
        pdf_candidates = self._build_pdf_download_candidates(kwargs)
        if not pdf_candidates:
            return ToolResult(
                success=False,
                output="缺少可下载的 PDF URL。请优先选择带 pdf_url 的搜索结果，或传 arxiv_id。",
                error="missing_pdf_url",
                data={"literature_review_id": review_id, "root": str(root)},
            )

        paper_key = self._safe_slug(kwargs.get("paper_key"), fallback="") or self._paper_key_from_metadata(
            title=kwargs.get("title"),
            doi=kwargs.get("doi"),
            arxiv_id=kwargs.get("arxiv_id"),
            external_id=kwargs.get("external_id"),
        )
        pdf_path = root / "pdf" / f"{paper_key}.pdf"
        download_url = ""
        download_errors: List[Dict[str, str]] = []
        if pdf_path.exists() and not bool(kwargs.get("overwrite")):
            success = True
            error = ""
            download_url = pdf_candidates[0]
        else:
            success = False
            error = ""
            service = get_literature_service()
            for candidate_url in pdf_candidates:
                success, error = await service.download_pdf(candidate_url, str(pdf_path))
                if success:
                    download_url = candidate_url
                    break
                download_errors.append({"url": candidate_url, "error": error or "unknown_error"})
        if not success:
            attempted = "\n".join(
                f"- {item['url']}: {item['error']}"
                for item in download_errors
            )
            return ToolResult(
                success=False,
                output=f"PDF 下载失败，已尝试 {len(pdf_candidates)} 个候选链接。\n{attempted}",
                error="pdf_download_failed",
                data={
                    "literature_review_id": review_id,
                    "paper_key": paper_key,
                    "pdf_url": pdf_candidates[0],
                    "attempted_pdf_urls": pdf_candidates,
                    "download_errors": download_errors,
                    "pdf_path": str(pdf_path),
                },
            )

        paper_meta = self._update_manifest_paper(
            root,
            paper_key,
            {
                "title": str(kwargs.get("title") or "").strip(),
                "abstract": str(kwargs.get("abstract") or "").strip(),
                "source": str(kwargs.get("source") or "").strip(),
                "external_id": str(kwargs.get("external_id") or "").strip(),
                "doi": str(kwargs.get("doi") or "").strip(),
                "arxiv_id": str(kwargs.get("arxiv_id") or "").strip(),
                "url": str(kwargs.get("url") or "").strip(),
                "venue": str(kwargs.get("venue") or "").strip(),
                "year": kwargs.get("year"),
                "authors": kwargs.get("authors") or [],
                "citation_count": kwargs.get("citation_count"),
                "reference_count": kwargs.get("reference_count"),
                "fields_of_study": kwargs.get("fields_of_study") or [],
                "pdf_url": download_url or pdf_candidates[0],
                "original_pdf_url": str(kwargs.get("pdf_url") or "").strip(),
                "attempted_pdf_urls": pdf_candidates,
                "pdf_download_errors": download_errors,
                "pdf_path": str(pdf_path),
                "pdf_downloaded": True,
            },
        )
        return ToolResult(
            success=True,
            output="\n".join(
                [
                    "PDF 已保存到论文综述目录。",
                    f"- literature_review_id: {review_id}",
                    f"- paper_key: {paper_key}",
                    f"- pdf_path: {pdf_path}",
                    f"- download_url: {download_url or pdf_candidates[0]}",
                    "下一步通常调用 literature_review_pdf_to_markdown 生成完整 Markdown。",
                ]
            ),
            data={
                "literature_review_id": review_id,
                "paper_key": paper_key,
                "pdf_url": download_url or pdf_candidates[0],
                "attempted_pdf_urls": pdf_candidates,
                "pdf_path": str(pdf_path),
                "paper": paper_meta,
            },
        )


class LiteratureReviewReadTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "literature_review_read"
    input_model = LiteratureReviewReadInput
    timeout_seconds = 20.0
    retry_count = 0
    output_max_tokens = 64000
    parallel_safe = True
    description = (
        "按 review_id 浏览文献综述工作区内已经生成的 review Markdown。职责边界：只列出和读取 "
        "`review/*.md` 与 `review/final.md`，用于查看单篇论文 review 或最终综述；list 会为每个单篇 "
        "review 附带所属论文的标题、作者、年份、来源、DOI/链接和引用格式。不要用本工具读取论文全文 "
        "`md/*.md`，也不要读取 PDF-to-Markdown 解析报告 `md/*.json`。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "literature_review_id": {"type": "string", "description": "综述任务 ID，例如 review-20260425053243-85976a3c。"},
            "mode": {"type": "string", "enum": ["list", "read"], "default": "list", "description": "list=列出已有 review/*.md，并附每篇 review 对应的论文元数据；read=全量读取指定 review Markdown。"},
            "relative_path": {"type": "string", "description": "mode=read 时使用。必须传 list 返回的 review 路径，例如 review/final.md 或 review/10.3390-s21113758.md。"},
        },
        "required": ["literature_review_id"],
    }

    @classmethod
    def _resolve_readable_artifact(cls, root: Path, raw_path: str) -> Optional[Path]:
        raw = str(raw_path or "").strip().replace("\\", "/")
        if not raw or "\x00" in raw:
            return None
        candidate = Path(raw)
        allowed_roots = [root / "review"]
        if candidate.is_absolute():
            if not any(cls._path_under_root(candidate, allowed_root) for allowed_root in allowed_roots):
                return None
        else:
            parts = [part for part in raw.split("/") if part not in {"", "."}]
            if not parts or any(part == ".." for part in parts):
                return None
            if len(parts) == 1:
                candidate = root / "review" / parts[0]
            else:
                candidate = root / "/".join(parts)
            if not any(cls._path_under_root(candidate, allowed_root) for allowed_root in allowed_roots):
                return None
        resolved = candidate.resolve()
        if resolved.suffix.lower() != ".md" or not resolved.is_file():
            return None
        return resolved

    def _artifact_file_entry(self, root: Path, path: Path) -> Dict[str, Any]:
        relative_path = path.relative_to(root).as_posix()
        paper_key = self._safe_slug(path.stem, fallback=path.stem)
        identity = self._paper_identity(root, paper_key=paper_key, relative_path=relative_path)
        stat = path.stat()
        return {
            "relative_path": relative_path,
            "filename": path.name,
            "kind": "final_review" if relative_path == "review/final.md" else "paper_review",
            "paper_key": identity.get("paper_key") or paper_key,
            "title": identity.get("title") or "未提供",
            "authors": identity.get("authors") or [],
            "year": identity.get("year") or "",
            "venue": identity.get("venue") or "",
            "doi": identity.get("doi") or "",
            "link": identity.get("link") or "",
            "citation_gbt7714": identity.get("citation_gbt7714") or "",
            "size_bytes": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        }

    def _metadata_lines_for_entry(self, entry: Dict[str, Any], *, include_citation: bool = True) -> List[str]:
        if entry.get("kind") == "final_review":
            return [
                "   - kind: final_review",
                "   - 所属论文: 综合综述，不对应单篇论文",
                f"   - 文件大小: {entry.get('size_bytes') or 0} bytes",
            ]
        authors = ", ".join(entry["authors"]) if entry.get("authors") else "未提供"
        doi = str(entry.get("doi") or "").strip()
        link = str(entry.get("link") or "").strip()
        doi_link = self._doi_url(doi) if doi else link
        lines = [
            f"   - kind: {entry.get('kind') or 'unknown'}",
            f"   - paper_key: {entry.get('paper_key') or '未提供'}",
            f"   - 标题: {entry.get('title') or '未提供'}",
            f"   - 作者: {authors}",
            f"   - 年份/来源: {entry.get('year') or '未提供'} / {entry.get('venue') or '未提供'}",
            f"   - DOI 链接: {doi_link or '未提供'}",
        ]
        if include_citation:
            lines.append(f"   - GB/T 7714: {entry.get('citation_gbt7714') or '未提供'}")
        return lines

    async def _execute(self, **kwargs) -> ToolResult:
        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        review_dir = root / "review"
        mode = str(kwargs.get("mode") or "list").strip().lower()
        if not root.is_dir():
            return ToolResult(
                success=False,
                output=f"找不到文献综述工作区：{review_id}",
                error="literature_review_not_found",
                data={"literature_review_id": review_id, "root": str(root)},
            )

        if mode == "read":
            path = self._resolve_readable_artifact(root, str(kwargs.get("relative_path") or ""))
            if path is None:
                return ToolResult(
                    success=False,
                    output="找不到可读取的 review Markdown。请先用 literature_review_read mode=list 获取路径，再传 relative_path。可读范围仅限：review/*.md、review/final.md。",
                    error="literature_review_artifact_not_found",
                    data={"literature_review_id": review_id, "review_dir": str(review_dir)},
                )
            content = path.read_text(encoding="utf-8", errors="replace")
            fence = "markdown"
            entry = self._artifact_file_entry(root, path)
            output_lines = [
                "已全量读取文献综述 review Markdown。",
                f"- literature_review_id: {review_id}",
                f"- relative_path: {entry['relative_path']}",
                f"- kind: {entry['kind']}",
                *self._metadata_lines_for_entry(entry, include_citation=True),
            ]
            output_lines.extend(
                [
                    "",
                    f"```{fence}",
                    content,
                    "```",
                ]
            )
            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                data={
                    "literature_review_id": review_id,
                    "relative_path": entry["relative_path"],
                    "entry": entry,
                    "content": content,
                },
            )

        review_files = sorted(path for path in review_dir.glob("*.md") if path.is_file()) if review_dir.is_dir() else []
        final_files = [path for path in review_files if path.name == "final.md"]
        paper_review_files = [path for path in review_files if path.name != "final.md"]
        ordered_review_files = final_files + paper_review_files
        review_entries = [self._artifact_file_entry(root, path) for path in ordered_review_files]
        lines = [
            "已列出文献综述 review Markdown 文件。",
            f"- literature_review_id: {review_id}",
            f"- review_dir: {review_dir}",
            f"- review_md_count: {len(review_entries)}",
            "- 职责: 本工具只列出和读取已生成的 `review/*.md`；每个单篇 review 会附所属论文元数据。",
            "- 不读取: 论文全文 `md/*.md` 和 PDF-to-Markdown 解析报告 `md/*.json` 不属于本工具范围。",
            "- 如需论文全文证据，请用 `literature_review_search_zoekt` 检索 `scope=paper`；如需生成单篇/最终 review，请用 `review_writer`。",
            "",
        ]
        if not review_entries:
            lines.append("未找到 `review/*.md`。如果已有 PDF/全文 Markdown，请先调用 `review_writer mode=paper/final` 生成 review。")
        else:
            for index, entry in enumerate(review_entries, start=1):
                lines.append(f"{index}. `{entry['relative_path']}`")
                lines.extend(self._metadata_lines_for_entry(entry, include_citation=True))
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "literature_review_id": review_id,
                "root": str(root),
                "review_dir": str(review_dir),
                "review_files": review_entries,
            },
        )

class LiteratureReviewSearchZoektTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "literature_review_search_zoekt"
    input_model = LiteratureReviewSearchZoektInput
    timeout_seconds = 240.0
    retry_count = 0
    output_max_tokens = 12000
    parallel_safe = False
    description = (
        "在指定文献综述任务内用 Zoekt 检索 Markdown。scope=paper 检索 `md/*.md` 论文全文，"
        "这些论文通常是英文，必须优先使用英文 query；scope=review 检索 `review/*.md` 单篇/最终综述，"
        "可以使用中文 query；scope=all 同时检索两者。优先用它做定向证据查找、局部原文定位和片段翻译，"
        "不要用工具输出尝试整篇论文翻译。返回命中行、可选上下文，以及论文标题、作者、DOI/链接和引用格式。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "literature_review_id": {"type": "string", "description": "综述任务 ID，例如 review-20260425053243-85976a3c。"},
            "query": {"type": "string", "description": "Zoekt 查询语法。检索论文全文 md/*.md 时优先使用英文；检索 review/*.md 时可使用中文。"},
            "scope": {"type": "string", "enum": ["all", "paper", "review"], "default": "all", "description": "paper=英文论文全文 md/*.md；review=中文/用户语言综述 review/*.md；all=两者都搜。"},
            "max_results": {"type": "integer", "default": 20, "description": "最多返回命中数，1-100。"},
            "context_lines": {"type": "integer", "default": 2, "description": "每个命中行前后附带的上下文行数，0-20。"},
            "auto_index": {"type": "boolean", "default": True, "description": "索引缺失时是否自动构建。通常保持 true。"},
            "force_reindex": {"type": "boolean", "default": False, "description": "搜索前是否强制重建 Zoekt 索引。"},
        },
        "required": ["literature_review_id", "query"],
    }

    @staticmethod
    def _read_context(path: Path, *, line_number: int, context_lines: int) -> List[Dict[str, Any]]:
        if line_number <= 0 or context_lines <= 0:
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []
        start = max(1, int(line_number) - int(context_lines))
        end = min(len(lines), int(line_number) + int(context_lines))
        return [
            {"line_number": number, "text": lines[number - 1]}
            for number in range(start, end + 1)
        ]

    async def _execute(self, **kwargs) -> ToolResult:
        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        md_dir = root / "md"
        review_dir = root / "review"
        query = str(kwargs.get("query") or "").strip()
        scope = str(kwargs.get("scope") or "all").strip().lower()
        if scope not in {"all", "paper", "review"}:
            scope = "all"
        max_results = int(kwargs.get("max_results") or 20)
        context_lines = int(kwargs.get("context_lines") or 0)
        if not root.is_dir():
            return ToolResult(
                success=False,
                output=f"找不到文献综述工作区：{review_id}",
                error="literature_review_not_found",
                data={"literature_review_id": review_id, "root": str(root)},
            )

        target_candidates: List[tuple[str, Path, str]] = []
        if scope in {"all", "paper"}:
            target_candidates.append(("paper", md_dir, "md"))
        if scope in {"all", "review"}:
            target_candidates.append(("review", review_dir, "review"))
        targets = [
            item
            for item in target_candidates
            if item[1].is_dir() and any(path.is_file() and path.suffix.lower() == ".md" for path in item[1].glob("*.md"))
        ]
        if not targets:
            detail = "md/*.md 和 review/*.md 都不存在"
            if scope == "paper":
                detail = "md/*.md 不存在。请先下载 PDF 并调用 literature_review_pdf_to_markdown。"
            elif scope == "review":
                detail = "review/*.md 不存在。请先调用 review_writer 生成单篇或最终 review。"
            return ToolResult(
                success=False,
                output=f"没有可检索的 Markdown：{detail}",
                error="literature_review_markdown_empty",
                data={"literature_review_id": review_id, "root": str(root), "md_dir": str(md_dir), "review_dir": str(review_dir), "scope": scope},
            )

        index_payloads: List[Dict[str, Any]] = []
        search_payloads: List[Dict[str, Any]] = []
        matches: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for target_kind, search_dir, relative_prefix in targets:
            index_payload: Optional[Dict[str, Any]] = None
            if bool(kwargs.get("auto_index", True)) or bool(kwargs.get("force_reindex", False)):
                index_payload = await ZoektCliService.build_project_index(
                    project_dir=search_dir,
                    workspace_dir=search_dir,
                    force_reindex=bool(kwargs.get("force_reindex", False)),
                )
                index_payloads.append({"scope": target_kind, **dict(index_payload or {})})
                if not bool(index_payload.get("success")):
                    failures.append({"scope": target_kind, "stage": "index", "payload": index_payload})
                    if scope != "all":
                        return ToolResult(
                            success=False,
                            output=(
                                "文献综述 Markdown Zoekt 索引准备失败。\n"
                                f"- scope: {target_kind}\n"
                                f"- error: {index_payload.get('error') or 'unknown'}\n"
                                f"- dir: {search_dir}"
                            ),
                            error=str(index_payload.get("error") or "literature_review_zoekt_index_failed"),
                            data={"literature_review_id": review_id, "scope": scope, "index": index_payload},
                        )
                    continue

            search_payload = await ZoektCliService.search_project(
                workspace_dir=search_dir,
                query=query,
                max_results=max_results,
            )
            search_payloads.append({"scope": target_kind, **dict(search_payload or {})})
            if not bool(search_payload.get("success")):
                failures.append({"scope": target_kind, "stage": "search", "payload": search_payload})
                if scope != "all":
                    error_message = str(search_payload.get("error") or "literature_review_zoekt_search_failed")
                    if error_message == "zoekt_index_missing":
                        output = "文献综述 Markdown Zoekt 索引不存在。请把 auto_index 设为 true。"
                    else:
                        output = f"文献综述 Markdown Zoekt 搜索失败。\n- scope: {target_kind}\n- error: {error_message}\n- query: {query}"
                    return ToolResult(
                        success=False,
                        output=output,
                        error=error_message,
                        data={"literature_review_id": review_id, "scope": scope, "index": index_payload, "search": search_payload},
                    )
                continue

            for item in list(search_payload.get("matches") or []):
                source_relative_path = str(item.get("source_relative_path") or item.get("repo_relative_path") or "").strip().replace("\\", "/")
                if not source_relative_path:
                    continue
                actual_path = (search_dir / source_relative_path).resolve()
                if not self._path_under_root(actual_path, search_dir) or not actual_path.is_file():
                    continue
                relative_path = f"{relative_prefix}/{source_relative_path}"
                paper_key = self._safe_slug(Path(source_relative_path).stem, fallback="")
                identity = self._paper_identity(root, paper_key=paper_key, relative_path=relative_path)
                enriched = dict(item)
                enriched["scope"] = target_kind
                enriched["source_relative_path"] = source_relative_path
                enriched["relative_path"] = relative_path
                enriched["paper"] = {key: value for key, value in identity.items() if key != "metadata"}
                if context_lines > 0:
                    enriched["context"] = self._read_context(
                        actual_path,
                        line_number=int(item.get("line_number") or 0),
                        context_lines=context_lines,
                    )
                matches.append(enriched)

        matches = sorted(matches, key=lambda row: float(row.get("score") or 0.0), reverse=True)[:max_results]
        if not matches and failures and len(failures) == len(targets):
            error_message = str((failures[0].get("payload") or {}).get("error") or "literature_review_zoekt_search_failed")
            return ToolResult(
                success=False,
                output=f"文献综述 Markdown Zoekt 搜索失败。\n- scope: {scope}\n- error: {error_message}\n- query: {query}",
                error=error_message,
                data={"literature_review_id": review_id, "scope": scope, "failures": failures, "index": index_payloads, "search": search_payloads},
            )

        lines = [
            "已使用 Zoekt 搜索文献综述 Markdown。",
            f"- literature_review_id: {review_id}",
            f"- scope: {scope}",
            f"- searched_dirs: {', '.join(f'{kind}={path}' for kind, path, _prefix in targets)}",
            f"- query: {query}",
            f"- returned_matches: {len(matches)}",
            f"- truncated: {any(bool(payload.get('truncated')) for payload in search_payloads)}",
            "- 提示: `scope=paper` 搜索英文论文全文，请优先用英文 query；`scope=review` 搜索中文/用户语言综述，可用中文 query。",
            "",
        ]
        if not matches:
            lines.append(
                "未命中。若要找论文全文证据，请用英文术语重试并设置 scope=paper；"
                "若要找已写出的综述段落，请用中文术语重试并设置 scope=review。"
            )
        for index, item in enumerate(matches, start=1):
            paper = dict(item.get("paper") or {})
            authors = ", ".join(list(paper.get("authors") or [])) or "未提供"
            line_number = int(item.get("line_number") or 0)
            location = f"{item.get('relative_path')}:{line_number}" if line_number > 0 else str(item.get("relative_path") or "")
            lines.extend(
                [
                    f"{index}. `{location}`",
                    f"   - scope: {item.get('scope') or 'unknown'}",
                    f"   - paper_key: {paper.get('paper_key') or '未提供'}",
                    f"   - 标题: {paper.get('title') or '未提供'}",
                    f"   - 作者: {authors}",
                    f"   - 年份/来源: {paper.get('year') or '未提供'} / {paper.get('venue') or '未提供'}",
                    f"   - DOI/链接: {paper.get('doi') or paper.get('link') or '未提供'}",
                    f"   - GB/T 7714: {paper.get('citation_gbt7714') or '未提供'}",
                    f"   - 命中: {str(item.get('line_text') or '').strip()}",
                ]
            )
            context = list(item.get("context") or [])
            if context:
                lines.append("   - 上下文:")
                for context_item in context:
                    lines.append(f"     {context_item['line_number']}: {context_item['text']}")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "literature_review_id": review_id,
                "root": str(root),
                "md_dir": str(md_dir),
                "review_dir": str(review_dir),
                "scope": scope,
                "query": query,
                "matches": matches,
                "failures": failures,
                "index": index_payloads,
                "search": search_payloads,
            },
        )

class LiteratureReviewPdfToMarkdownTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "literature_review_pdf_to_markdown"
    input_model = LiteratureReviewPdfToMarkdownInput
    timeout_seconds = 300.0
    retry_count = 0
    output_max_tokens = 9000
    parallel_safe = False
    description = (
        "读取综述目录中的 PDF，并用本地 PDF-to-Markdown 管线生成完整 Markdown 到 "
        "`/app/uploads/literature_reviews/{literature_review_id}/md/`。"
        "返回 md_path、report_path、页数和字符数，不把完整 Markdown 放入 observation。"
        "后续应把 md_path 或 paper_key 交给 review_writer mode=paper 生成 review/*.md。"
        "如果需要了解原文，请优先使用 literature_review_search_zoekt 做定向检索；如果已有成品综述，"
        "优先使用 literature_review_read 读取 review/*.md。不要用本工具做整篇论文翻译。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "literature_review_id": {"type": "string", "description": "综述任务 ID。"},
            "paper_key": {"type": "string", "description": "论文 key；默认读取 pdf/{paper_key}.pdf 并写 md/{paper_key}.md。"},
            "pdf_path": {"type": "string", "description": "可选 PDF 路径。支持综述 root 下相对路径或绝对路径。"},
            "mode": {"type": "string", "enum": ["fast", "hybrid"], "default": "fast", "description": "PDF 解析模式。默认 fast。"},
        },
        "required": ["literature_review_id"],
    }

    async def _execute(self, **kwargs) -> ToolResult:
        from app.services.pdf_rag_ingest_service import get_pdf_rag_ingest_service

        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        self._ensure_review_dirs(root)
        paper_key = self._safe_slug(kwargs.get("paper_key"), fallback="")
        pdf_path = self._resolve_review_file(root, str(kwargs.get("pdf_path") or ""), default_subdir="pdf")
        if pdf_path is None and paper_key:
            candidate = root / "pdf" / f"{paper_key}.pdf"
            pdf_path = candidate.resolve() if candidate.is_file() and self._path_under_root(candidate, root) else None
        if pdf_path is None:
            return ToolResult(
                success=False,
                output="找不到可读取的 PDF。请传 paper_key 或综述目录内的 pdf_path。",
                error="pdf_not_found",
                data={"literature_review_id": review_id, "root": str(root)},
            )
        if not paper_key:
            paper_key = self._safe_slug(pdf_path.stem, fallback=f"paper-{uuid.uuid4().hex[:8]}")

        ingest = await get_pdf_rag_ingest_service().ingest_pdf(
            file_path=str(pdf_path),
            document_name=pdf_path.name,
            mode=str(kwargs.get("mode") or "fast"),
        )
        markdown = str(ingest.get("document_text") or "")
        if not markdown.strip():
            return ToolResult(
                success=False,
                output=f"PDF 转 Markdown 失败或无文本内容: {ingest.get('failure_reason') or 'empty_markdown'}",
                error="pdf_markdown_empty",
                data={
                    "literature_review_id": review_id,
                    "paper_key": paper_key,
                    "pdf_path": str(pdf_path),
                    "ingest": ingest,
                },
            )

        md_path = root / "md" / f"{paper_key}.md"
        md_path.write_text(markdown, encoding="utf-8")
        report_path = root / "md" / f"{paper_key}.json"
        report_path.write_text(
            json.dumps(
                {
                    "paper_key": paper_key,
                    "pdf_path": str(pdf_path),
                    "md_path": str(md_path),
                    "extractor": ingest.get("extractor"),
                    "report": ingest.get("report") or {},
                    "document_source_spans": ingest.get("document_source_spans") or [],
                    "created_at": datetime.utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        report = dict(ingest.get("report") or {})
        self._update_manifest_paper(
            root,
            paper_key,
            {
                "pdf_path": str(pdf_path),
                "md_path": str(md_path),
                "markdown_chars": len(markdown),
                "page_count": report.get("page_count"),
                "extractor": ingest.get("extractor"),
                "pdf_read_at": datetime.utcnow().isoformat(),
            },
        )
        header = "\n".join(
            [
                "PDF 已完整转换为 Markdown。",
                f"- literature_review_id: {review_id}",
                f"- paper_key: {paper_key}",
                f"- pdf_path: {pdf_path}",
                f"- md_path: {md_path}",
                f"- chars: {len(markdown)}",
                f"- pages: {report.get('page_count', 'unknown')}",
            ]
        )
        return ToolResult(
            success=True,
            output=header,
            data={
                "literature_review_id": review_id,
                "paper_key": paper_key,
                "pdf_path": str(pdf_path),
                "md_path": str(md_path),
                "report_path": str(report_path),
                "markdown_chars": len(markdown),
                "page_count": report.get("page_count"),
                "extractor": ingest.get("extractor"),
            },
        )


class ReviewWriterTool(_LiteratureReviewWorkspaceMixin, ToolBase):
    name = "review_writer"
    input_model = ReviewWriterInput
    timeout_seconds = 0.0
    retry_count = 0
    output_max_tokens = 12000
    parallel_safe = False
    description = (
        "读取 literature_review 目录中的完整 Markdown 或单篇 review，使用平台默认 LLM 生成基础 Markdown 综述。"
        "单篇结果写入 `/review/{paper_key}.md`，最终综述写入 `/review/final.md`。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "literature_review_id": {"type": "string", "description": "综述任务 ID。"},
            "topic": {"type": "string", "description": "明确综述主题。"},
            "mode": {"type": "string", "enum": ["paper", "final"], "default": "paper", "description": "paper=生成单篇 review；final=汇总已有单篇 review。"},
            "paper_key": {"type": "string", "description": "单篇模式下的论文 key。"},
            "md_path": {"type": "string", "description": "可选完整论文 Markdown 路径。支持综述 root 下相对路径或绝对路径。"},
            "requirements": {"type": "string", "description": "额外写作要求。"},
            "target_paper_count": {"type": "integer", "default": 12, "description": "最终综述最低单篇 review 数量，默认 12。"},
        },
        "required": ["literature_review_id", "topic"],
    }

    @staticmethod
    def _llm_max_tokens() -> int:
        return max(2048, int(getattr(settings, "llm_max_tokens", 4096) or 4096))

    async def _call_llm(self, *, system_prompt: str, user_prompt: str) -> str:
        response = await LLMService().chat(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.2,
            max_tokens=self._llm_max_tokens(),
            source="literature_review.review_writer",
        )
        return str(response.get("content") or "").strip()

    def _resolve_md_for_paper(self, root: Path, paper_key: str, md_path: str) -> Optional[Path]:
        resolved = self._resolve_review_file(root, md_path, default_subdir="md") if md_path else None
        if resolved is not None:
            return resolved
        if paper_key:
            candidate = root / "md" / f"{paper_key}.md"
            if candidate.is_file() and self._path_under_root(candidate, root):
                return candidate.resolve()
        return None

    def _paper_metadata(self, root: Path, paper_key: str) -> Dict[str, Any]:
        manifest = self._load_manifest(root)
        papers = manifest.get("papers") if isinstance(manifest, dict) else {}
        if isinstance(papers, dict) and isinstance(papers.get(paper_key), dict):
            return dict(papers.get(paper_key) or {})
        return {}

    @staticmethod
    def _compact_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _metadata_value(cls, metadata: Dict[str, Any], key: str) -> str:
        value = metadata.get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        return cls._compact_text(value)

    @classmethod
    def _author_names(cls, metadata: Dict[str, Any]) -> List[str]:
        raw_authors = metadata.get("authors")
        if not isinstance(raw_authors, list):
            return []
        names: List[str] = []
        for item in raw_authors:
            if isinstance(item, dict):
                name = cls._compact_text(
                    item.get("name")
                    or item.get("display_name")
                    or item.get("author")
                    or item.get("full_name")
                )
            else:
                name = cls._compact_text(item)
            if name:
                names.append(name)
        return names

    @staticmethod
    def _doi_url(doi: str) -> str:
        value = str(doi or "").strip()
        if not value:
            return ""
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            return value
        value = re.sub(r"^(?:doi:\s*)", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"^(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE).strip()
        return f"https://doi.org/{value}" if value else ""

    @staticmethod
    def _bibtex_escape(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    @classmethod
    def _citation_strings(cls, *, paper_key: str, metadata: Dict[str, Any]) -> Dict[str, str]:
        authors = cls._author_names(metadata)
        authors_text = ", ".join(authors) if authors else "作者未提供"
        bibtex_authors = " and ".join(authors)
        title = cls._metadata_value(metadata, "title") or "题名未提供"
        year = cls._metadata_value(metadata, "year") or "年份未提供"
        venue = cls._metadata_value(metadata, "venue")
        doi = cls._metadata_value(metadata, "doi")
        doi_url = cls._doi_url(doi)
        arxiv_id = cls._metadata_value(metadata, "arxiv_id")
        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
        url = cls._metadata_value(metadata, "url")
        pdf_url = cls._metadata_value(metadata, "pdf_url")
        best_link = doi_url or url or arxiv_url or pdf_url

        apa_parts = [f"{authors_text}.", f"({year}).", f"{title}."]
        if venue:
            apa_parts.append(f"{venue}.")
        if best_link:
            apa_parts.append(best_link)
        apa = " ".join(apa_parts)

        gbt_parts = [f"{authors_text}.", f"{title}[J/OL]."]
        if venue:
            gbt_parts.append(f"{venue},")
        gbt_parts.append(f"{year}.")
        if doi:
            gbt_parts.append(f"DOI: {doi}.")
        elif best_link:
            gbt_parts.append(best_link)
        gbt = " ".join(gbt_parts)

        bibtex_key = cls._safe_slug(paper_key, fallback="paper").replace(".", "-")
        bibtex_fields = [f"  title = {{{cls._bibtex_escape(title)}}}"]
        if bibtex_authors:
            bibtex_fields.append(f"  author = {{{cls._bibtex_escape(bibtex_authors)}}}")
        if year != "年份未提供":
            bibtex_fields.append(f"  year = {{{cls._bibtex_escape(year)}}}")
        if venue:
            bibtex_fields.append(f"  journal = {{{cls._bibtex_escape(venue)}}}")
        if doi:
            bibtex_fields.append(f"  doi = {{{cls._bibtex_escape(doi)}}}")
        if best_link:
            bibtex_fields.append(f"  url = {{{cls._bibtex_escape(best_link)}}}")
        bibtex = "@misc{" + bibtex_key + ",\n" + ",\n".join(bibtex_fields) + "\n}"
        return {"apa": apa, "gbt7714": gbt, "bibtex": bibtex}

    @staticmethod
    def _present_or_missing(value: str) -> str:
        return value if str(value or "").strip() else "未提供"

    @classmethod
    def _fixed_paper_source_block(cls, *, paper_key: str, metadata: Dict[str, Any]) -> str:
        title = cls._metadata_value(metadata, "title") or paper_key
        authors = ", ".join(cls._author_names(metadata))
        abstract = str(metadata.get("abstract") or "").strip()
        doi = cls._metadata_value(metadata, "doi")
        doi_url = cls._doi_url(doi)
        arxiv_id = cls._metadata_value(metadata, "arxiv_id")
        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
        citations = cls._citation_strings(paper_key=paper_key, metadata=metadata)
        fields = metadata.get("fields_of_study")
        fields_text = ", ".join(str(item).strip() for item in fields if str(item).strip()) if isinstance(fields, list) else cls._compact_text(fields)
        return "\n".join(
            [
                "## 检索摘要、链接与引用",
                "",
                "> 下面信息由平台根据 `literature_search` / `literature_review_download_pdf` 元数据生成；缺失字段只标记为“未提供”，不由模型补写。",
                "",
                f"- paper_key：{paper_key}",
                f"- 标题：{cls._present_or_missing(title)}",
                f"- 作者：{cls._present_or_missing(authors)}",
                f"- 年份：{cls._present_or_missing(cls._metadata_value(metadata, 'year'))}",
                f"- 来源/会议期刊：{cls._present_or_missing(cls._metadata_value(metadata, 'venue'))}",
                f"- 数据源：{cls._present_or_missing(cls._metadata_value(metadata, 'source'))}",
                f"- 外部 ID：{cls._present_or_missing(cls._metadata_value(metadata, 'external_id'))}",
                f"- DOI：{cls._present_or_missing(doi)}",
                f"- DOI 链接：{cls._present_or_missing(doi_url)}",
                f"- arXiv：{cls._present_or_missing(arxiv_id)}",
                f"- arXiv 链接：{cls._present_or_missing(arxiv_url)}",
                f"- 论文页面：{cls._present_or_missing(cls._metadata_value(metadata, 'url'))}",
                f"- PDF URL：{cls._present_or_missing(cls._metadata_value(metadata, 'pdf_url'))}",
                f"- 引用数：{cls._present_or_missing(cls._metadata_value(metadata, 'citation_count'))}",
                f"- 参考文献数：{cls._present_or_missing(cls._metadata_value(metadata, 'reference_count'))}",
                f"- 领域标签：{cls._present_or_missing(fields_text)}",
                "",
                "### 检索摘要",
                "",
                abstract if abstract else "未提供",
                "",
                "### 引用格式",
                "",
                f"- APA：{citations['apa']}",
                f"- GB/T 7714：{citations['gbt7714']}",
                "",
                "```bibtex",
                citations["bibtex"],
                "```",
            ]
        )

    @staticmethod
    def _attach_fixed_paper_block(content: str, fixed_block: str, *, fallback_title: str) -> str:
        body = str(content or "").strip()
        if fixed_block in body:
            return body
        if body.startswith("# "):
            lines = body.splitlines()
            title_line = lines[0].strip()
            rest = "\n".join(lines[1:]).strip()
            return f"{title_line}\n\n{fixed_block}\n\n{rest}".strip()
        return f"# {fallback_title}\n\n{fixed_block}\n\n{body}".strip()

    @staticmethod
    def _table_cell(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        return text.replace("|", "\\|") if text else "未提供"

    def _fixed_reference_catalog(self, *, root: Path, review_files: List[Path]) -> str:
        rows = [
            "| paper_key | 标题 | 作者 | 年份 | DOI | 链接 | PDF | APA | GB/T 7714 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for path in review_files:
            paper_key = self._safe_slug(path.stem, fallback=path.stem)
            metadata = self._paper_metadata(root, paper_key)
            citations = self._citation_strings(paper_key=paper_key, metadata=metadata)
            doi = self._metadata_value(metadata, "doi")
            doi_url = self._doi_url(doi)
            arxiv_id = self._metadata_value(metadata, "arxiv_id")
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
            link = self._metadata_value(metadata, "url") or doi_url or arxiv_url
            rows.append(
                "| "
                + " | ".join(
                    [
                        self._table_cell(paper_key),
                        self._table_cell(self._metadata_value(metadata, "title")),
                        self._table_cell(", ".join(self._author_names(metadata))),
                        self._table_cell(self._metadata_value(metadata, "year")),
                        self._table_cell(doi),
                        self._table_cell(link),
                        self._table_cell(self._metadata_value(metadata, "pdf_url")),
                        self._table_cell(citations["apa"]),
                        self._table_cell(citations["gbt7714"]),
                    ]
                )
                + " |"
            )
        return "\n".join(["## 参考论文清单（固定元数据）", "", *rows])

    @staticmethod
    def _attach_fixed_reference_catalog(content: str, catalog: str) -> str:
        body = str(content or "").strip()
        if "## 参考论文清单（固定元数据）" in body:
            return body
        return f"{body}\n\n{catalog}".strip()

    async def _write_paper_review(self, *, root: Path, review_id: str, topic: str, paper_key: str, md_path: Path, requirements: str) -> ToolResult:
        markdown = md_path.read_text(encoding="utf-8")
        metadata = self._paper_metadata(root, paper_key)
        fixed_source_block = self._fixed_paper_source_block(paper_key=paper_key, metadata=metadata)
        fallback_title = self._metadata_value(metadata, "title") or paper_key
        system_prompt = (
            "你是严谨的科研论文综述写作助手。只能依据用户提供的论文全文 Markdown 和元数据写作；"
            "不要编造未出现的实验、结论、引用或比较。链接、DOI、引用格式只能使用平台提供的固定元数据块；"
            "固定元数据块缺失的字段必须保持“未提供”。输出中文 Markdown。"
        )
        user_prompt = "\n".join(
            [
                f"综述主题：{topic}",
                f"论文 key：{paper_key}",
                f"论文元数据：{json.dumps(metadata, ensure_ascii=False, default=str)}",
                f"额外要求：{requirements or '无'}",
                "",
                "下面是平台生成的固定元数据块。不要改写、补齐或删除其中的摘要、链接、DOI 和引用格式：",
                fixed_source_block,
                "",
                "请为这篇论文生成一份用于后续总综述汇编的单篇 review Markdown。",
                "不要重复固定元数据块；你的可生成部分结构固定为：",
                "## 与综述主题的关系",
                "## 核心问题与方法",
                "## 关键发现",
                "## 证据与实验",
                "## 局限与争议",
                "## 可纳入最终综述的要点",
                "",
                "下面是完整论文 Markdown：",
                markdown,
            ]
        )
        content = await self._call_llm(system_prompt=system_prompt, user_prompt=user_prompt)
        if not content:
            return ToolResult(
                success=False,
                output="review_writer 未生成内容。",
                error="empty_review",
                data={"literature_review_id": review_id, "paper_key": paper_key, "md_path": str(md_path)},
            )
        content = self._attach_fixed_paper_block(content, fixed_source_block, fallback_title=fallback_title)
        review_path = root / "review" / f"{paper_key}.md"
        review_path.write_text(content, encoding="utf-8")
        self._update_manifest_paper(
            root,
            paper_key,
            {
                "md_path": str(md_path),
                "review_path": str(review_path),
                "review_chars": len(content),
                "review_written_at": datetime.utcnow().isoformat(),
            },
        )
        return ToolResult(
            success=True,
            output="\n".join(
                [
                    "单篇论文 review 已生成。",
                    f"- literature_review_id: {review_id}",
                    f"- paper_key: {paper_key}",
                    f"- md_path: {md_path}",
                    f"- review_path: {review_path}",
                    f"- chars: {len(content)}",
                ]
            ),
            data={
                "literature_review_id": review_id,
                "paper_key": paper_key,
                "md_path": str(md_path),
                "review_path": str(review_path),
                "review_chars": len(content),
            },
        )

    async def _write_final_review(self, *, root: Path, review_id: str, topic: str, requirements: str, target_paper_count: int) -> ToolResult:
        review_dir = root / "review"
        review_files = sorted(
            path for path in review_dir.glob("*.md")
            if path.is_file() and path.name != "final.md"
        )
        if len(review_files) < target_paper_count:
            return ToolResult(
                success=False,
                output=(
                    f"单篇 review 数量不足：当前 {len(review_files)} 篇，目标 {target_paper_count} 篇。"
                    "请继续搜索、下载、调用 literature_review_pdf_to_markdown 并生成单篇 review，或显式降低 target_paper_count。"
                ),
                error="insufficient_reviews",
                data={
                    "literature_review_id": review_id,
                    "review_count": len(review_files),
                    "target_paper_count": target_paper_count,
                    "review_dir": str(review_dir),
                },
            )
        combined_reviews: List[str] = []
        for path in review_files:
            combined_reviews.append(f"\n\n<!-- source_review: {path.name} -->\n\n{path.read_text(encoding='utf-8')}")
        fixed_reference_catalog = self._fixed_reference_catalog(root=root, review_files=review_files)
        system_prompt = (
            "你是科研综述作者。只能依据用户提供的单篇 review 汇总写最终综述；"
            "不要补充未在单篇 review 中出现的事实。参考论文的 DOI、链接和引用格式只能使用平台提供的固定参考论文清单；"
            "缺失字段必须保持“未提供”。必须围绕综述主题重新组织成一篇完整综述，而不是简单拼接摘要。"
            "正文中的关键事实、方法、结论和比较必须用方括号引用对应 paper_key，例如 [10.1109-access.2020.3048415]。"
            "文末必须包含“## 参考文献”，且只能使用固定参考论文清单中的论文与引用信息。输出中文 Markdown。"
        )
        user_prompt = "\n".join(
            [
                f"综述主题：{topic}",
                f"单篇 review 数量：{len(review_files)}",
                f"额外要求：{requirements or '无'}",
                "",
                "固定参考论文清单如下。最终文末如列参考论文，必须只使用这份清单中的 DOI、链接和引用格式，不得补写：",
                fixed_reference_catalog,
                "",
                "请基于全部单篇 review 生成最终综述 Markdown。要求：",
                "- 不是简单拼接，必须围绕主题综合归纳研究路线、共识、分歧、证据强弱和未来方向。",
                "- 正文每个实质性论断、代表性工作比较、方法归纳后都要给出 paper_key 引用。",
                "- 如果某个结论无法由单篇 review 支撑，必须删除或标记为证据不足，不得补写。",
                "- 文末必须有“## 参考文献”，每条参考文献只能来自固定参考论文清单，优先采用 GB/T 7714 字段；缺失字段保持“未提供”。",
                "",
                "建议结构：",
                "# 综述标题",
                "## 摘要",
                "## 研究背景与问题定义",
                "## 主要研究路线",
                "## 代表性工作比较",
                "## 共识、分歧与证据强度",
                "## 局限与未来方向",
                "## 参考文献",
                "",
                "下面是全部单篇 review：",
                "\n".join(combined_reviews),
            ]
        )
        content = await self._call_llm(system_prompt=system_prompt, user_prompt=user_prompt)
        if not content:
            return ToolResult(
                success=False,
                output="review_writer 未生成最终综述。",
                error="empty_final_review",
                data={"literature_review_id": review_id, "review_count": len(review_files)},
            )
        content = self._attach_fixed_reference_catalog(content, fixed_reference_catalog)
        final_path = review_dir / "final.md"
        final_path.write_text(content, encoding="utf-8")
        manifest = self._load_manifest(root)
        manifest["final_review_path"] = str(final_path)
        manifest["final_review_written_at"] = datetime.utcnow().isoformat()
        manifest["final_review_source_count"] = len(review_files)
        self._write_manifest(root, manifest)
        return ToolResult(
            success=True,
            output=content,
            data={
                "literature_review_id": review_id,
                "final_review_path": str(final_path),
                "review_count": len(review_files),
                "target_paper_count": target_paper_count,
                "review_dir": str(review_dir),
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        review_id = self._review_id(kwargs.get("literature_review_id"))
        root = self._review_root_for(review_id)
        self._ensure_review_dirs(root)
        topic = str(kwargs.get("topic") or "").strip()
        requirements = str(kwargs.get("requirements") or "").strip()
        mode = str(kwargs.get("mode") or "paper").strip().lower()
        if mode == "final":
            return await self._write_final_review(
                root=root,
                review_id=review_id,
                topic=topic,
                requirements=requirements,
                target_paper_count=int(kwargs.get("target_paper_count") or 12),
            )

        paper_key = self._safe_slug(kwargs.get("paper_key"), fallback="")
        md_path = self._resolve_md_for_paper(root, paper_key, str(kwargs.get("md_path") or ""))
        if md_path is None:
            return ToolResult(
                success=False,
                output="找不到完整论文 Markdown。请先调用 literature_review_pdf_to_markdown，或传入综述目录内的 md_path。",
                error="md_not_found",
                data={"literature_review_id": review_id, "paper_key": paper_key, "root": str(root)},
            )
        if not paper_key:
            paper_key = self._safe_slug(md_path.stem, fallback=f"paper-{uuid.uuid4().hex[:8]}")
        return await self._write_paper_review(
            root=root,
            review_id=review_id,
            topic=topic,
            paper_key=paper_key,
            md_path=md_path,
            requirements=requirements,
        )


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
                tools.extend(
                    [
                        DocumentArtifactReadTool(
                            ctx.db,
                            int(ctx.user_id),
                            conversation_id=int(ctx.conversation_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        DocumentArtifactUpdateBlockTool(
                            ctx.db,
                            int(ctx.user_id),
                            conversation_id=int(ctx.conversation_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        DocumentArtifactUpdateBlocksTool(
                            ctx.db,
                            int(ctx.user_id),
                            conversation_id=int(ctx.conversation_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                    ]
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
                        PaperSearchTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        ProjectTreeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        ProjectReadFileTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        ProjectWriteFileTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        ProjectBashTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        ProjectClaudeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                        ),
                        DocxGenerateWithClaudeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                            conversation_id=ctx.conversation_id,
                        ),
                        DocxRefineWithClaudeTool(
                            ctx.db,
                            int(ctx.user_id),
                            db_session_factory=ctx.db_session_factory,
                            conversation_id=ctx.conversation_id,
                        ),
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
                        PaperResearchSearchProjectZoektTool(
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
                LiteratureReviewStartTool(user_id=ctx.user_id),
                LiteratureReviewDownloadPdfTool(user_id=ctx.user_id),
                LiteratureReviewReadTool(user_id=ctx.user_id),
                LiteratureReviewSearchZoektTool(user_id=ctx.user_id),
                LiteratureReviewPdfToMarkdownTool(user_id=ctx.user_id),
                ReviewWriterTool(user_id=ctx.user_id),
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
        "document_generation": {"docx_generate_with_claude", "docx_refine_with_claude"},
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

        if any(
            token in text
            for token in [
                "docx",
                "word",
                "生成文档",
                "文档生成",
                "生成word",
                "生成 word",
                "生成docx",
                "生成 docx",
                "写国基",
                "国基",
                "项目书",
                "申请书",
            ]
        ):
            return "document_generation"

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
        elif resolved_intent == "document_generation":
            pass
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
