from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import fitz
from loguru import logger
from PIL import Image

from app.config import settings
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
from app.services.smart_chunking.token_utils import estimate_tokens
from app.services.smart_chunking.types import generate_chunk_id


_SUPPORTED_BLOCK_TYPES = {
    "paragraph",
    "equation",
    "table",
}
_SUPPORTED_CHUNK_TYPES = {"paragraph", "equation", "table"}
_INGEST_BLOCK_TYPES = {"paragraph", "equation", "table"}
_SUPPORTED_MODEL_CHUNK_TYPES = {"paragraph", "equation", "table"}
_SUPPORTED_ZONES = {
    "full_width",
    "left_column",
    "right_column",
    "top_band",
    "bottom_band",
    "center_band",
    "margin",
    "unknown",
}
_SUPPORTED_SPANS = {"single_column", "double_column", "full_width", "unknown"}
_SUPPORTED_EXTRACT_GRANULARITIES = {"fine", "medium", "coarse"}
_SUPPORTED_CONTENT_ROLES = {
    "abstract_body",
    "body_paragraph",
    "reference_entry",
    "front_matter_misc",
    "equation_body",
    "table_body",
}
_SUPPORTED_CLEANUP_ACTIONS = {
    "keep",
    "drop_from_body",
    "route_to_metadata",
}
_SUPPORTED_CHUNK_BOUNDARY_STATES = {"open", "closed"}
_SUPPORTED_TABLE_HEADER_STATES = {"present", "repeated", "missing"}
_SUPPORTED_LIST_MARKER_TYPES = {"bullet", "numbered", "lettered", "roman"}
_PAGE_TRIM_WHITE_THRESHOLD = 245
_NOISE_HEADING_PATTERNS = (
    re.compile(r"^(fig(?:ure)?|table|tab\.)\s*\d+", re.IGNORECASE),
    re.compile(r"^(doi|pmid)\s*[:：]?", re.IGNORECASE),
)
_SECTION_TYPE_HINTS = {
    "abstract": "abstract",
    "introduction": "introduction",
    "background": "background",
    "related work": "related_work",
    "methods": "methods",
    "method": "methods",
    "materials and methods": "methods",
    "results": "results",
    "discussion": "discussion",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "references": "references",
    "acknowledg": "acknowledgements",
    "appendix": "appendix",
}
_ZONE_CHOICES = {"full_width", "left_column", "right_column", "top_band", "bottom_band", "margin", "unknown"}
_SPAN_CHOICES = {"single_column", "double_column", "full_width", "unknown"}
_BLOCK_TYPE_ALIASES = {
    "formula": "equation",
    "math": "equation",
    "data_table": "table",
    "body_text": "paragraph",
    "text": "paragraph",
    "reference": "paragraph",
    "references": "paragraph",
    "reference_entry": "paragraph",
    "citation": "paragraph",
    "bibliography": "paragraph",
}
_ZONE_ALIASES = {
    "center": "full_width",
    "centered": "full_width",
    "centre": "full_width",
    "middle": "full_width",
    "center_band": "full_width",
}
_SPAN_ALIASES = {
    "single": "single_column",
    "single-column": "single_column",
    "single column": "single_column",
    "double": "double_column",
    "double-column": "double_column",
    "double column": "double_column",
    "two-column": "double_column",
    "two column": "double_column",
}
_CONTENT_ROLE_ALIASES = {
    "abstract": "abstract_body",
    "abstract_text": "abstract_body",
    "body": "body_paragraph",
    "paragraph": "body_paragraph",
    "reference": "reference_entry",
    "references": "reference_entry",
    "front_matter": "front_matter_misc",
    "metadata": "front_matter_misc",
    "equation": "equation_body",
    "table": "table_body",
}
_CLEANUP_ACTION_ALIASES = {
    "drop": "drop_from_body",
    "remove": "drop_from_body",
    "skip": "drop_from_body",
    "metadata": "route_to_metadata",
    "metadata_only": "route_to_metadata",
    "route_to_front_matter": "route_to_metadata",
}
_CHUNK_BOUNDARY_ALIASES = {
    "continue": "open",
    "continued": "open",
    "incomplete": "open",
    "complete": "closed",
    "closed_chunk": "closed",
}
_TABLE_HEADER_STATE_ALIASES = {
    "has_header": "present",
    "with_header": "present",
    "header_present": "present",
    "repeat_header": "repeated",
    "repeated_header": "repeated",
    "header_repeated": "repeated",
    "no_header": "missing",
    "header_missing": "missing",
    "without_header": "missing",
}
_LIST_MARKER_TYPE_ALIASES = {
    "bulleted": "bullet",
    "unordered": "bullet",
    "numeric": "numbered",
    "number": "numbered",
    "ordered": "numbered",
    "alphabetic": "lettered",
    "alpha": "lettered",
    "letters": "lettered",
}
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_ARXIV_PATTERN = re.compile(r"\barXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE)
_KEYWORDS_PATTERN = re.compile(
    r"^\s*(keywords?|index terms?|ccs concepts?|additional keywords and phrases)\b",
    re.IGNORECASE,
)
_AFFILIATION_HINT_PATTERN = re.compile(
    r"\b(university|universidad|institute|institut|laboratory|college|school|department|faculty|hospital|center|centre|lab)\b",
    re.IGNORECASE,
)
_INDEX_TERMS_PATTERN = re.compile(r"^\s*index terms?\b", re.IGNORECASE)
_CORRESPONDING_AUTHOR_PATTERN = re.compile(r"\bcorresponding author\b", re.IGNORECASE)
_FUNDING_HINT_PATTERN = re.compile(
    r"\b(this work was supported|supported by|funded by|grant\b|award\b|project\b)\b",
    re.IGNORECASE,
)
_ROMAN_HEADING_PATTERN = re.compile(r"^(?:[IVXLCDM]+)(?:[.\s:：\-]|$)")
_LETTER_HEADING_PATTERN = re.compile(r"^[A-Z](?:[.)])?\s+[A-Z]")
_ENUM_HEADING_PATTERN = re.compile(r"^\d+\)\s+")
_RETRYABLE_DISCONNECT_MARKERS = (
    "connection aborted",
    "remotedisconnected",
    "remote end closed connection without response",
    "connection reset by peer",
)
_MULTIMODAL_MODEL_MAX_TOKENS = {
    "qwen3-vl-flash": 8192,
    "qwen-vl-ocr": 8192,
    "qwen-vl-ocr-latest": 8192,
}
_FRONT_MATTER_CONTENT_ROLES = {
    "front_matter_misc",
}


class OnlineMmIngestService:
    """Scientific PDF ingestion via multimodal extraction + deterministic chunking."""

    async def ingest_pdf(
        self,
        *,
        file_path: str,
        document_name: str,
        extract_profile: str = "general",
        extract_granularity: str = "medium",
    ) -> dict[str, Any]:
        profile = str(extract_profile or "general").strip().lower() or "general"
        granularity = self._normalize_extract_granularity(extract_granularity)
        extraction_result = await self.extract_pdf_blocks(
            file_path=file_path,
            document_name=document_name,
            extract_profile=profile,
            extract_granularity=granularity,
        )
        if not bool(extraction_result.get("ok")):
            return self._failure(
                str(extraction_result.get("failure_reason") or "online_mm_ingest_failed"),
                document_name=document_name,
                extract_profile=profile,
                extract_granularity=granularity,
                report=dict(extraction_result.get("report") or {}),
            )
        return await self.finalize_blocks(
            blocks=list(extraction_result.get("blocks") or []),
            document_name=document_name,
            extract_profile=profile,
            extract_granularity=granularity,
            extract_report=dict(extraction_result.get("report") or {}),
        )

    async def extract_pdf_blocks(
        self,
        *,
        file_path: str,
        document_name: str,
        extract_profile: str = "general",
        extract_granularity: str = "medium",
        cached_windows: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        pdf_path = Path(str(file_path or "").strip()).expanduser()
        profile = str(extract_profile or "general").strip().lower() or "general"
        granularity = self._normalize_extract_granularity(extract_granularity)
        if not pdf_path.is_file():
            return {"ok": False, "failure_reason": "pdf_file_missing", "report": {}}
        if not bool(getattr(settings, "kb_online_mm_ingest_enabled", False)):
            return {"ok": False, "failure_reason": "online_mm_ingest_disabled", "report": {}}

        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        dashscope_base = str(getattr(settings, "aliyun_dashscope_api_base", "") or base_url or "").strip()
        if not DashScopeMultimodalService.is_available():
            return {"ok": False, "failure_reason": "dashscope_sdk_unavailable", "report": {}}
        if not api_key or not base_url:
            return {"ok": False, "failure_reason": "aliyun_api_key_or_base_url_missing", "report": {}}

        page_limit = max(1, int(getattr(settings, "kb_online_mm_max_pages_soft_limit", 80) or 80))
        try:
            page_count = self._count_pages(pdf_path)
        except RuntimeError as exc:
            return {"ok": False, "failure_reason": str(exc), "report": {}}
        if page_count > page_limit:
            return {
                "ok": False,
                "failure_reason": f"page_count_exceeds_soft_limit:{page_count}>{page_limit}",
                "report": {"page_count": int(page_count), "page_limit": int(page_limit)},
            }

        primary_model = str(getattr(settings, "kb_online_mm_primary_model", "qwen3-vl-flash") or "qwen3-vl-flash").strip()
        pages_per_call = max(1, int(getattr(settings, "kb_online_mm_pages_per_call", 1) or 1))
        window_overlap = max(0, int(getattr(settings, "kb_online_mm_window_overlap", 0) or 0))
        extract_max_concurrency = max(1, int(getattr(settings, "kb_online_mm_extract_max_concurrency", 1) or 1))
        extract_max_tokens = max(1600, int(getattr(settings, "kb_online_mm_extract_max_tokens", 20000) or 20000))
        trim_whitespace = bool(getattr(settings, "kb_online_mm_trim_whitespace", True))
        trim_padding = max(0, int(getattr(settings, "kb_online_mm_trim_padding_px", 24) or 24))
        image_max_side = max(768, int(getattr(settings, "kb_online_mm_image_max_side", 1920) or 1920))
        image_max_pixels = max(
            1_000_000,
            int(getattr(settings, "kb_online_mm_image_max_pixels", 2_600_000) or 2_600_000),
        )

        page_usages: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        document_metadata = self._empty_document_metadata()
        cached_window_entries = self._normalize_window_cache_entries(
            cached_windows=cached_windows,
            document_name=document_name,
            extract_profile=profile,
            extract_granularity=granularity,
        )

        try:
            with tempfile.TemporaryDirectory(prefix="kb_online_mm_") as temp_dir:
                page_image_paths = self._render_pdf_pages(pdf_path=pdf_path, out_dir=Path(temp_dir))
                if not page_image_paths:
                    return {"ok": False, "failure_reason": "pdf_page_render_empty", "report": {}}

                page_image_by_number = {
                    index + 1: path
                    for index, path in enumerate(page_image_paths)
                }
                page_windows = self._build_page_windows(
                    page_image_paths=page_image_paths,
                    pages_per_call=pages_per_call,
                    overlap=window_overlap,
                )
                semaphore = asyncio.Semaphore(extract_max_concurrency)

                async def _run_live_window(window_index: int, window_spec: dict[str, Any]) -> dict[str, Any]:
                    async with semaphore:
                        page_numbers = list(window_spec.get("page_numbers") or [])
                        image_paths = list(window_spec.get("image_paths") or [])
                        try:
                            if len(page_numbers) == 1:
                                single_result = await self._extract_page_blocks(
                                    image_path=image_paths[0],
                                    page_number=int(page_numbers[0]),
                                    document_name=document_name,
                                    extract_profile=profile,
                                    extract_granularity=granularity,
                                    api_key=api_key,
                                    base_url=dashscope_base,
                                    model=primary_model,
                                    max_tokens=extract_max_tokens,
                                )
                                usage = dict(single_result.get("usage") or {})
                                usage["window_index"] = int(window_index)
                                usage["pages"] = [int(page_numbers[0])]
                                single_result["usage"] = usage
                                return single_result
                            return await self._extract_page_window_blocks(
                                image_paths=image_paths,
                                page_numbers=page_numbers,
                                document_name=document_name,
                                extract_profile=profile,
                                extract_granularity=granularity,
                                api_key=api_key,
                                base_url=dashscope_base,
                                model=primary_model,
                                max_tokens=extract_max_tokens,
                                window_index=window_index,
                            )
                        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                            return {
                                "ok": False,
                                "failure_reason": str(exc),
                                "model": str(primary_model or "").strip(),
                                "page_numbers": [int(page) for page in page_numbers if int(page or 0) > 0],
                                "window_entries": [],
                            }

                async def _extract_window_with_retry(window_index: int, window_spec: dict[str, Any]) -> dict[str, Any]:
                    nonlocal cached_window_entries
                    page_numbers = [int(page) for page in list(window_spec.get("page_numbers") or []) if int(page or 0) > 0]
                    cached_cover = self._collect_cached_window_entries(
                        page_numbers=page_numbers,
                        cached_entries=cached_window_entries,
                    )
                    if cached_cover:
                        return {
                            "ok": True,
                            "window_entries": cached_cover,
                            "from_cache": True,
                            "page_numbers": page_numbers,
                        }

                    live_result = await _run_live_window(window_index, window_spec)
                    if bool(live_result.get("ok")):
                        window_entry = self._make_window_cache_entry(
                            window_spec=window_spec,
                            page_result=live_result,
                            document_name=document_name,
                            extract_profile=profile,
                            extract_granularity=granularity,
                        )
                        if self._should_split_window_after_success(
                            page_result=live_result,
                            window_spec=window_spec,
                        ):
                            split_windows = self._split_window_spec(
                                window_spec=window_spec,
                                page_image_by_number=page_image_by_number,
                            )
                            split_entries: list[dict[str, Any]] = []
                            for offset, split_window in enumerate(split_windows, start=1):
                                split_result = await _extract_window_with_retry(
                                    (int(window_index) * 10) + offset,
                                    split_window,
                                )
                                if not bool(split_result.get("ok")):
                                    return split_result
                                split_entries.extend(list(split_result.get("window_entries") or []))
                            return {
                                "ok": True,
                                "window_entries": split_entries,
                                "from_cache": False,
                                "page_numbers": page_numbers,
                            }
                        cached_window_entries = self._upsert_window_cache_entry(
                            cached_entries=cached_window_entries,
                            new_entry=window_entry,
                        )
                        return {
                            "ok": True,
                            "window_entries": [window_entry],
                            "from_cache": False,
                            "page_numbers": page_numbers,
                        }

                    if self._should_split_failed_window(window_spec=window_spec, page_result=live_result):
                        split_windows = self._split_window_spec(
                            window_spec=window_spec,
                            page_image_by_number=page_image_by_number,
                        )
                        split_entries: list[dict[str, Any]] = []
                        for offset, split_window in enumerate(split_windows, start=1):
                            split_result = await _extract_window_with_retry(
                                (int(window_index) * 10) + offset,
                                split_window,
                            )
                            if not bool(split_result.get("ok")):
                                return split_result
                            split_entries.extend(list(split_result.get("window_entries") or []))
                        return {
                            "ok": True,
                            "window_entries": split_entries,
                            "from_cache": False,
                            "page_numbers": page_numbers,
                        }

                    return {
                        "ok": False,
                        "failure_reason": str(live_result.get("failure_reason") or "window_extract_failed"),
                        "model": str(live_result.get("model") or primary_model),
                        "page_numbers": page_numbers,
                        "window_entries": [],
                    }

                window_results = await asyncio.gather(
                    *[
                        _extract_window_with_retry(index, window_spec)
                        for index, window_spec in enumerate(page_windows, start=1)
                    ]
                )

                flattened_window_entries: list[dict[str, Any]] = []
                for page_result in window_results:
                    if not bool(page_result.get("ok")):
                        page_numbers = list(page_result.get("page_numbers") or [])
                        page_label = ",".join(str(int(page)) for page in page_numbers if int(page or 0) > 0)
                        return {
                            "ok": False,
                            "failure_reason": str(
                                page_result.get("failure_reason") or f"page_extract_failed:{page_label or 'window'}"
                            ),
                            "report": {
                                "page": page_label or None,
                                "page_count": int(page_count),
                                "model": str(page_result.get("model") or primary_model),
                            },
                            "window_cache": list(cached_window_entries),
                        }
                    flattened_window_entries.extend(list(page_result.get("window_entries") or []))

                window_cache_entries = self._dedupe_window_cache_entries(
                    entries=flattened_window_entries or cached_window_entries
                )
                seen_block_ids: set[str] = set()
                for window_entry in window_cache_entries:
                    page_usages.append(dict(window_entry.get("usage") or {}))
                    for block in list(window_entry.get("blocks") or []):
                        block_id = str(block.get("block_id") or "").strip()
                        if not block_id or block_id in seen_block_ids:
                            continue
                        seen_block_ids.add(block_id)
                        blocks.append(block)
                    document_metadata = self._merge_document_metadata(
                        document_metadata,
                        dict(window_entry.get("document_metadata") or {}),
                    )

                if not blocks:
                    return {
                        "ok": False,
                        "failure_reason": "page_blocks_empty",
                        "report": {"page_count": int(page_count)},
                        "window_cache": list(cached_window_entries),
                    }

                report = {
                    "mode": "online_mm",
                    "extract_profile": profile,
                    "extract_granularity": granularity,
                    "extractor": "online_mm_ingest",
                    "primary_model": primary_model,
                    "planner_model": "model_chunker",
                    "page_count": int(page_count),
                    "block_count": int(len(blocks)),
                    "pages_per_call": int(pages_per_call),
                    "window_overlap": int(min(window_overlap, max(0, pages_per_call - 1))),
                    "extract_window_count": int(len(page_windows)),
                    "resolved_window_count": int(len(window_cache_entries)),
                    "extract_max_concurrency": int(extract_max_concurrency),
                    "extract_max_tokens": int(extract_max_tokens),
                    "image_preprocess": {
                        "trim_whitespace": bool(trim_whitespace),
                        "trim_padding_px": int(trim_padding),
                        "max_side": int(image_max_side),
                        "max_pixels": int(image_max_pixels),
                    },
                    "usage": self._sum_usages([dict(item) for item in page_usages]),
                    "page_usages": page_usages,
                    "extract_duration_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
                    "document_metadata": dict(document_metadata),
                }
                return {
                    "ok": True,
                    "failure_reason": None,
                    "blocks": blocks,
                    "report": report,
                    "window_cache": window_cache_entries,
                }
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"[OnlineMmIngest] extract failed for {document_name}: {type(exc).__name__}: {exc}")
            return {
                "ok": False,
                "failure_reason": str(exc),
                "report": {},
                "window_cache": list(cached_window_entries or []),
            }

    async def finalize_blocks(
        self,
        *,
        blocks: list[dict[str, Any]],
        document_name: str,
        extract_profile: str = "general",
        extract_granularity: str = "medium",
        extract_report: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        profile = str(extract_profile or "general").strip().lower() or "general"
        granularity = self._normalize_extract_granularity(extract_granularity)
        normalized_blocks = self._apply_page_order_section_context(blocks=list(blocks or []))
        if not normalized_blocks:
            return self._failure(
                "page_blocks_empty",
                document_name=document_name,
                extract_profile=profile,
                extract_granularity=granularity,
                report=dict(extract_report or {}),
            )

        primary_model = str(getattr(settings, "kb_online_mm_primary_model", "qwen3-vl-flash") or "qwen3-vl-flash").strip()
        planner_model = "model_chunker"

        try:
            chunk_plan_result = self._build_model_chunk_plan(
                blocks=normalized_blocks,
                document_name=document_name,
                extract_profile=profile,
                extract_granularity=granularity,
            )
            if not bool(chunk_plan_result.get("ok")):
                return self._failure(
                    str(chunk_plan_result.get("failure_reason") or "chunk_plan_failed"),
                    document_name=document_name,
                    extract_profile=profile,
                    extract_granularity=granularity,
                    report={**dict(extract_report or {}), "block_count": int(len(normalized_blocks))},
                )

            chunk_plan = list(chunk_plan_result.get("chunks") or [])
            planner_usage = {}
            if not chunk_plan:
                return self._failure(
                    "chunk_plan_empty",
                    document_name=document_name,
                    extract_profile=profile,
                    extract_granularity=granularity,
                    report={**dict(extract_report or {}), "block_count": int(len(normalized_blocks))},
                )

            materialized = self._materialize_chunks(
                blocks=normalized_blocks,
                chunk_plan=chunk_plan,
                document_name=document_name,
                extract_profile=profile,
                extract_granularity=granularity,
                source_model=primary_model,
                document_metadata_seed=dict((extract_report or {}).get("document_metadata") or {}),
            )
            chunks = list(materialized.get("chunks") or [])
            context_chunks = list(materialized.get("context_chunks") or [])
            document_text = str(materialized.get("document_text") or "")
            if not chunks or not document_text.strip():
                return self._failure(
                    str(materialized.get("failure_reason") or "materialize_chunks_empty"),
                    document_name=document_name,
                    extract_profile=profile,
                    extract_granularity=granularity,
                    report={
                        **dict(extract_report or {}),
                        "block_count": int(len(normalized_blocks)),
                        "chunk_plan_count": int(len(chunk_plan)),
                    },
                )

            base_report = dict(extract_report or {})
            page_usages = list(base_report.get("page_usages") or [])
            total_usage = self._sum_usages([dict(item) for item in page_usages] + [dict(planner_usage or {})])
            report = {
                **base_report,
                "mode": "online_mm",
                "extract_profile": profile,
                "extract_granularity": granularity,
                "extractor": "online_mm_ingest",
                "planner_model": planner_model,
                "block_count": int(len(normalized_blocks)),
                "chunk_count": int(len(chunks)),
                "context_chunk_count": int(len(context_chunks)),
                "usage": total_usage,
                "planner_usage": planner_usage,
                "document_title": materialized.get("document_title"),
                "document_metadata": dict(materialized.get("document_metadata") or {}),
                "section_spine": list(materialized.get("section_spine") or []),
                "duration_ms": round(
                    float(base_report.get("extract_duration_ms") or 0.0)
                    + ((time.perf_counter() - started_at) * 1000.0),
                    2,
                ),
            }
            return {
                "applied": True,
                "extractor": "online_mm_ingest",
                "document_text": document_text,
                "blocks": normalized_blocks,
                "chunk_plan": chunk_plan,
                "chunks": chunks,
                "context_chunks": context_chunks,
                "report": report,
                "failure_reason": None,
            }
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"[OnlineMmIngest] finalize failed for {document_name}: {type(exc).__name__}: {exc}")
            return self._failure(
                str(exc),
                document_name=document_name,
                extract_profile=profile,
                extract_granularity=granularity,
                report=dict(extract_report or {}),
            )

    @staticmethod
    def _failure(
        failure_reason: str,
        *,
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
        report: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = {
            "mode": "online_mm",
            "extractor": "online_mm_ingest",
            "document_name": str(document_name or "").strip(),
            "extract_profile": str(extract_profile or "general").strip(),
            "extract_granularity": str(extract_granularity or "medium").strip(),
            "failure_reason": str(failure_reason or "online_mm_ingest_failed").strip(),
        }
        if report:
            payload.update(dict(report))
        return {
            "applied": False,
            "extractor": "online_mm_ingest",
            "document_text": "",
            "blocks": [],
            "chunk_plan": [],
            "chunks": [],
            "context_chunks": [],
            "report": payload,
            "failure_reason": payload["failure_reason"],
        }

    @staticmethod
    def _count_pages(pdf_path: Path) -> int:
        try:
            with fitz.open(pdf_path) as doc:
                return int(doc.page_count)
        except (RuntimeError, OSError, ValueError) as exc:
            raise RuntimeError(f"pdf_open_failed:{exc}") from exc

    @staticmethod
    def _render_pdf_pages(*, pdf_path: Path, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        dpi = max(72, int(getattr(settings, "kb_online_mm_render_dpi", 200) or 200))
        scale = float(dpi) / 72.0
        rendered_paths: list[Path] = []
        try:
            with fitz.open(pdf_path) as doc:
                for index in range(doc.page_count):
                    page = doc.load_page(index)
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    target = out_dir / f"page_{index + 1:04d}.png"
                    pix.save(target)
                    OnlineMmIngestService._preprocess_rendered_page_image(image_path=target)
                    rendered_paths.append(target)
        except (RuntimeError, OSError, ValueError) as exc:
            raise RuntimeError(f"pdf_render_failed:{exc}") from exc
        return rendered_paths

    @staticmethod
    def _preprocess_rendered_page_image(*, image_path: Path) -> dict[str, Any]:
        trim_whitespace = bool(getattr(settings, "kb_online_mm_trim_whitespace", True))
        trim_padding = max(0, int(getattr(settings, "kb_online_mm_trim_padding_px", 24) or 24))
        max_side = max(768, int(getattr(settings, "kb_online_mm_image_max_side", 1920) or 1920))
        max_pixels = max(1_000_000, int(getattr(settings, "kb_online_mm_image_max_pixels", 2_600_000) or 2_600_000))
        try:
            with Image.open(image_path) as original:
                image = original.convert("RGB")
                original_size = image.size
                processed = image
                cropped = False
                resized = False

                if trim_whitespace:
                    trimmed = OnlineMmIngestService._trim_page_whitespace(
                        image=processed,
                        padding=trim_padding,
                    )
                    cropped = trimmed.size != processed.size
                    processed = trimmed

                scaled = OnlineMmIngestService._downscale_page_image(
                    image=processed,
                    max_side=max_side,
                    max_pixels=max_pixels,
                )
                resized = scaled.size != processed.size
                processed = scaled

                processed.save(image_path, format="PNG", optimize=True, compress_level=9)
                return {
                    "original_size": [int(original_size[0]), int(original_size[1])],
                    "final_size": [int(processed.size[0]), int(processed.size[1])],
                    "cropped": bool(cropped),
                    "resized": bool(resized),
                }
        except (OSError, ValueError) as exc:
            logger.warning(f"[OnlineMmIngest] page preprocess skipped for {image_path.name}: {exc}")
            return {
                "original_size": None,
                "final_size": None,
                "cropped": False,
                "resized": False,
                "error": str(exc),
            }

    @staticmethod
    def _trim_page_whitespace(*, image: Image.Image, padding: int) -> Image.Image:
        grayscale = image.convert("L")
        content_mask = grayscale.point(
            lambda pixel: 255 if pixel < _PAGE_TRIM_WHITE_THRESHOLD else 0,
            mode="L",
        )
        bbox = content_mask.getbbox()
        if not bbox:
            return image
        left, top, right, bottom = bbox
        if right <= left or bottom <= top:
            return image
        x_padding = min(int(padding), max(0, image.width // 12))
        y_padding = min(int(padding), max(0, image.height // 12))
        crop_box = (
            max(0, left - x_padding),
            max(0, top - y_padding),
            min(image.width, right + x_padding),
            min(image.height, bottom + y_padding),
        )
        if crop_box == (0, 0, image.width, image.height):
            return image
        return image.crop(crop_box)

    @staticmethod
    def _downscale_page_image(*, image: Image.Image, max_side: int, max_pixels: int) -> Image.Image:
        width, height = image.size
        if width <= 0 or height <= 0:
            return image
        scale_by_side = min(1.0, float(max_side) / float(max(width, height)))
        scale_by_pixels = min(1.0, math.sqrt(float(max_pixels) / float(width * height)))
        scale = min(scale_by_side, scale_by_pixels)
        if scale >= 0.995:
            return image
        target_width = max(1, int(math.floor(width * scale)))
        target_height = max(1, int(math.floor(height * scale)))
        return image.resize((target_width, target_height), Image.Resampling.LANCZOS)

    @staticmethod
    def _build_page_windows(
        *,
        page_image_paths: list[Path],
        pages_per_call: int,
        overlap: int,
    ) -> list[dict[str, Any]]:
        if not page_image_paths:
            return []
        window_size = max(1, int(pages_per_call))
        stride = max(1, window_size - max(0, min(int(overlap), window_size - 1)))
        windows: list[dict[str, Any]] = []
        start = 0
        while start < len(page_image_paths):
            end = min(len(page_image_paths), start + window_size)
            image_paths = list(page_image_paths[start:end])
            page_numbers = [index + 1 for index in range(start, end)]
            windows.append({"image_paths": image_paths, "page_numbers": page_numbers})
            if end >= len(page_image_paths):
                break
            start += stride
        return windows

    @staticmethod
    def _normalize_window_cache_entries(
        *,
        cached_windows: Optional[list[dict[str, Any]]],
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in list(cached_windows or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("document_name") or "").strip() != str(document_name or "").strip():
                continue
            if str(item.get("extract_profile") or "").strip() != str(extract_profile or "").strip():
                continue
            if str(item.get("extract_granularity") or "").strip() != str(extract_granularity or "").strip():
                continue
            page_numbers = [int(page) for page in list(item.get("page_numbers") or []) if int(page or 0) > 0]
            blocks = [dict(block) for block in list(item.get("blocks") or []) if isinstance(block, dict)]
            if not page_numbers or not blocks:
                continue
            normalized.append(
                {
                    "page_numbers": page_numbers,
                    "blocks": blocks,
                    "usage": dict(item.get("usage") or {}),
                    "model": str(item.get("model") or "").strip() or None,
                    "document_metadata": dict(item.get("document_metadata") or {}),
                    "document_name": str(document_name or "").strip(),
                    "extract_profile": str(extract_profile or "").strip(),
                    "extract_granularity": str(extract_granularity or "").strip(),
                }
            )
        return OnlineMmIngestService._dedupe_window_cache_entries(entries=normalized)

    @staticmethod
    def _dedupe_window_cache_entries(*, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[int, ...], dict[str, Any]] = {}
        for entry in list(entries or []):
            page_key = tuple(int(page) for page in list(entry.get("page_numbers") or []) if int(page or 0) > 0)
            if not page_key:
                continue
            deduped[page_key] = dict(entry)
        return [deduped[key] for key in sorted(deduped.keys())]

    @staticmethod
    def _upsert_window_cache_entry(
        *,
        cached_entries: list[dict[str, Any]],
        new_entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return OnlineMmIngestService._dedupe_window_cache_entries(
            entries=[*list(cached_entries or []), dict(new_entry)]
        )

    @staticmethod
    def _empty_document_metadata() -> dict[str, Any]:
        return {
            "title": None,
            "authors": [],
            "affiliations": [],
            "emails": [],
            "identifiers": [],
            "front_matter_items": [],
        }

    def _merge_document_metadata(self, base: Optional[dict[str, Any]], incoming: Optional[dict[str, Any]]) -> dict[str, Any]:
        merged = self._empty_document_metadata()
        base_map = dict(base or {})
        incoming_map = dict(incoming or {})
        merged["title"] = str(incoming_map.get("title") or base_map.get("title") or "").strip() or None
        for field, limit in (
            ("authors", 16),
            ("affiliations", 16),
            ("emails", 32),
            ("identifiers", 16),
            ("front_matter_items", 24),
        ):
            values: list[str] = []
            for source in (base_map.get(field) or [], incoming_map.get(field) or []):
                candidates = list(source) if isinstance(source, (list, tuple, set)) else [source]
                for candidate in candidates:
                    self._append_unique_text(values, candidate)
                    if len(values) >= limit:
                        break
                if len(values) >= limit:
                    break
            merged[field] = values[:limit]
        return merged

    @staticmethod
    def _collect_cached_window_entries(
        *,
        page_numbers: list[int],
        cached_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        target_pages = [int(page) for page in list(page_numbers or []) if int(page or 0) > 0]
        if not target_pages:
            return []
        target_set = set(target_pages)
        exact_matches = [
            dict(entry)
            for entry in list(cached_entries or [])
            if [int(page) for page in list(entry.get("page_numbers") or [])] == target_pages
        ]
        if exact_matches:
            return exact_matches
        candidates = [
            dict(entry)
            for entry in list(cached_entries or [])
            if set(int(page) for page in list(entry.get("page_numbers") or []) if int(page or 0) > 0).issubset(target_set)
        ]
        if not candidates:
            return []
        page_hits: dict[int, int] = {}
        for entry in candidates:
            for page in [int(value) for value in list(entry.get("page_numbers") or []) if int(value or 0) > 0]:
                page_hits[page] = page_hits.get(page, 0) + 1
        if set(page_hits.keys()) != target_set:
            return []
        if any(count != 1 for count in page_hits.values()):
            return []
        return sorted(
            candidates,
            key=lambda row: tuple(int(page) for page in list(row.get("page_numbers") or [])),
        )

    @staticmethod
    def _make_window_cache_entry(
        *,
        window_spec: dict[str, Any],
        page_result: dict[str, Any],
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
    ) -> dict[str, Any]:
        return {
            "page_numbers": [int(page) for page in list(window_spec.get("page_numbers") or []) if int(page or 0) > 0],
            "blocks": [dict(block) for block in list(page_result.get("blocks") or []) if isinstance(block, dict)],
            "usage": dict(page_result.get("usage") or {}),
            "model": str(page_result.get("model") or "").strip() or None,
            "document_metadata": dict(page_result.get("document_metadata") or {}),
            "document_name": str(document_name or "").strip(),
            "extract_profile": str(extract_profile or "").strip(),
            "extract_granularity": str(extract_granularity or "").strip(),
        }

    @staticmethod
    def _split_window_spec(
        *,
        window_spec: dict[str, Any],
        page_image_by_number: dict[int, Path],
    ) -> list[dict[str, Any]]:
        page_numbers = [int(page) for page in list(window_spec.get("page_numbers") or []) if int(page or 0) > 0]
        if len(page_numbers) <= 1:
            return [dict(window_spec)]
        if len(page_numbers) == 2:
            split_groups = [[page_numbers[0]], [page_numbers[1]]]
        else:
            split_groups = [page_numbers[:-1], [page_numbers[-1]]]
        return [
            {
                "page_numbers": group,
                "image_paths": [page_image_by_number[int(page)] for page in group if int(page) in page_image_by_number],
            }
            for group in split_groups
            if group
        ]

    def _should_split_window_after_success(
        self,
        *,
        page_result: dict[str, Any],
        window_spec: dict[str, Any],
    ) -> bool:
        page_numbers = [int(page) for page in list(window_spec.get("page_numbers") or []) if int(page or 0) > 0]
        if len(page_numbers) <= 1:
            return False
        usage = dict(page_result.get("usage") or {})
        try:
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            completion_tokens = 0
        model = str(page_result.get("model") or "").strip().lower()
        ceiling = int(_MULTIMODAL_MODEL_MAX_TOKENS.get(model) or 8192)
        return completion_tokens >= ceiling

    @staticmethod
    def _should_split_failed_window(*, window_spec: dict[str, Any], page_result: dict[str, Any]) -> bool:
        page_numbers = [int(page) for page in list(window_spec.get("page_numbers") or []) if int(page or 0) > 0]
        if len(page_numbers) <= 1:
            return False
        reason = str(page_result.get("failure_reason") or "").strip().lower()
        return any(
            token in reason
            for token in ("max_tokens", "page_chunks_invalid", "window_chunks_invalid", "page_chunks_missing", "window_extract_failed")
        ) or any(
            token in reason
            for token in ("datainspectionfailed", "inappropriate content", "dashscope_multimodal_failed")
        )

    async def _extract_page_blocks(
        self,
        *,
        image_path: Path,
        page_number: int,
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        prompt = self._build_page_extract_prompt(
            document_name=document_name,
            page_number=page_number,
            extract_profile=extract_profile,
            extract_granularity=extract_granularity,
        )
        try:
            result = await self._call_multimodal_json_with_retry(
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=(
                    "You prepare final semantic RAG chunks from one scientific PDF page. "
                    "Return JSON only with top-level keys document_metadata and chunks. "
                    "Chunks must already follow reading order and semantic chunk boundaries."
                ),
                user_prompt=prompt,
                image_paths=[str(image_path)],
                max_tokens=self._resolve_multimodal_max_tokens(
                    model=model,
                    requested=max_tokens,
                    minimum=1200,
                ),
                temperature=0.0,
                retry_label=f"page:{int(page_number)}",
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "failure_reason": str(exc),
                "model": model,
                "page_numbers": [int(page_number)],
            }
        payload = self._normalize_page_payload(parsed=dict(result.get("parsed") or {}), page_number=page_number)
        normalized_blocks = list(payload.get("blocks") or [])
        if not normalized_blocks:
            return {
                "ok": False,
                "failure_reason": f"page_chunks_invalid:{page_number}:{model}",
                "model": model,
                "page_numbers": [int(page_number)],
            }
        return {
            "ok": True,
            "model": model,
            "usage": dict(result.get("usage") or {}),
            "page_numbers": [int(page_number)],
            "blocks": normalized_blocks,
            "document_metadata": dict(payload.get("document_metadata") or {}),
        }

    async def _extract_page_window_blocks(
        self,
        *,
        image_paths: list[Path],
        page_numbers: list[int],
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int,
        window_index: int,
    ) -> dict[str, Any]:
        prompt = self._build_window_extract_prompt(
            document_name=document_name,
            page_numbers=page_numbers,
            extract_profile=extract_profile,
            extract_granularity=extract_granularity,
        )
        try:
            result = await self._call_multimodal_json_with_retry(
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=(
                    "You prepare final semantic RAG chunks from multiple consecutive scientific PDF pages. "
                    "Return JSON only with top-level key pages. "
                    "Each page entry must contain document_metadata and final semantic chunks for that page."
                ),
                user_prompt=prompt,
                image_paths=[str(path) for path in list(image_paths or [])],
                max_tokens=self._resolve_multimodal_max_tokens(
                    model=model,
                    requested=max_tokens,
                    minimum=1600,
                ),
                temperature=0.0,
                retry_label=f"window:{','.join(str(int(page)) for page in page_numbers)}",
                max_attempts=1,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "failure_reason": str(exc),
                "model": model,
                "page_numbers": [int(page) for page in page_numbers],
            }
        payload = self._normalize_window_payload(parsed=dict(result.get("parsed") or {}), page_numbers=page_numbers)
        normalized_blocks = list(payload.get("blocks") or [])
        if not normalized_blocks:
            return {
                "ok": False,
                "failure_reason": f"window_chunks_invalid:{','.join(str(int(page)) for page in page_numbers)}:{model}",
                "model": model,
                "page_numbers": [int(page) for page in page_numbers],
            }
        usage = dict(result.get("usage") or {})
        usage["window_index"] = int(window_index)
        usage["pages"] = [int(page) for page in page_numbers]
        usage["image_count"] = len(list(image_paths or []))
        return {
            "ok": True,
            "model": model,
            "usage": usage,
            "page_numbers": [int(page) for page in page_numbers],
            "blocks": normalized_blocks,
            "document_metadata": dict(payload.get("document_metadata") or {}),
        }

    async def _call_multimodal_json_with_retry(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[str],
        max_tokens: int,
        temperature: float,
        retry_label: str,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        attempts = max(1, int(max_attempts or 1))
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return await DashScopeMultimodalService.chat_json(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    image_paths=image_paths,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt >= attempts or not self._is_retryable_disconnect_error(exc):
                    raise
                logger.warning(
                    "[OnlineMmIngest] transient multimodal disconnect, retrying once: label={} model={} attempt={} error={}",
                    str(retry_label or "").strip(),
                    str(model or "").strip(),
                    int(attempt),
                    str(exc),
                )
                await asyncio.sleep(0.8)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _is_retryable_disconnect_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        if not message:
            return False
        return any(token in message for token in _RETRYABLE_DISCONNECT_MARKERS)

    @staticmethod
    def _build_page_extract_prompt(
        *,
        document_name: str,
        page_number: int,
        extract_profile: str,
        extract_granularity: str,
    ) -> str:
        profile_text = {
            "academic_formula": "Prioritize equations and surrounding explanatory chunks.",
            "table_first": "Prioritize table structure, row/column fidelity, and compact table chunks.",
        }.get(extract_profile, "Balance paragraphs, equations, tables, and references.")
        granularity_text = {
            "fine": "Use fine chunking: create smaller semantic chunks and keep topic changes separate.",
            "coarse": "Use coarse chunking: keep semantically continuous discussion together in larger chunks.",
        }.get(extract_granularity, "Use medium chunking: prefer balanced semantic chunks.")
        return (
            f"Document: {document_name or 'unknown'}\n"
            f"Page: {int(page_number)}\n"
            f"Profile: {extract_profile}\n\n"
            f"Granularity: {extract_granularity}\n\n"
            "This output is for RAG ingestion, not layout analysis.\n"
            "Read the page in strict human reading order, then output final semantic chunks in that same order.\n"
            f"{profile_text}\n"
            f"{granularity_text}\n"
            "Common scientific paper structure is usually: title/authors/affiliations/emails in front matter, then Abstract, then Keywords/Index Terms/CCS Concepts, then sectioned body content, then Acknowledgements, then References, then Appendices.\n"
            "Do not output visual blocks. Output final chunks only.\n"
            "Allowed chunk_type values: paragraph, equation, table.\n"
            "Allowed content_role values: abstract_body, body_paragraph, reference_entry, front_matter_misc, equation_body, table_body.\n"
            "Allowed cleanup_action values: keep, drop_from_body, route_to_metadata.\n"
            "Reference chunks must use chunk_type=paragraph with content_role=reference_entry.\n"
            "Put title/author/affiliation/email/identifier into document_metadata, not into chunks.\n"
            "Put Keywords, Index Terms, CCS Concepts, Additional Keywords and Phrases, funding/support lines, correspondence notes, and other front-matter-only text into document_metadata.front_matter_items, not into body chunks.\n"
            "Keep Abstract as retrievable content, usually with section_path=[\"Abstract\"].\n"
            "Treat Acknowledgements as an end-matter section in the body hierarchy, not as front matter metadata.\n"
            "For references pages, group consecutive references into semantically compact reference chunks. Do not emit one chunk per citation unless truly necessary.\n"
            "If the current chunk is only a continuation of the previous page, set continues_previous=true.\n"
            "Optional merge hints when helpful: chunk_boundary=open|closed, table_header_state=present|repeated|missing, "
            "table_continues_previous=true|false, table_id_hint, table_caption, list_continues_previous=true|false, "
            "list_marker_type=bullet|numbered|lettered|roman, list_index_start, list_index_end.\n"
            "These merge hints are optional. Omit them when unclear.\n"
            "If the current page clearly belongs to a section/subsection, fill section_path and section_level_path. "
            "section_level_path must express structural depth, not chapter numbering literal value: main sections like I/II/III or 1/2/3 use level 1, A/B/C use level 2, and 1)/2)/3) under a subsection use level 3.\n"
            "When a page begins inside an already active section, include the inherited parent section_path instead of starting a new root section. For example, if the previous page established 'IV Methodology' and this page begins with 'B Navigation Policy Learning', output section_path=['IV Methodology','B Navigation Policy Learning'].\n"
            "Preserve source wording in text. Do not paraphrase or summarize.\n"
            "For equation chunks, fill latex when possible.\n"
            "For table chunks, fill table_markdown with a compact markdown table when possible.\n"
            "Optional lightweight hints: title_hint, zone, span.\n"
            "Omit keys that do not apply.\n"
            "Return JSON object:\n"
            "{\n"
            '  "document_metadata": {\n'
            '    "title": null,\n'
            '    "authors": [],\n'
            '    "affiliations": [],\n'
            '    "emails": [],\n'
            '    "identifiers": [],\n'
            '    "front_matter_items": []\n'
            "  },\n"
            '  "chunks": [\n'
            "    {\n"
            '      "chunk_type": "paragraph",\n'
            '      "order": 1,\n'
            '      "text": "content",\n'
            '      "content_role": "body_paragraph",\n'
            '      "cleanup_action": "keep",\n'
            '      "section_path": ["1 Introduction"],\n'
            '      "section_level_path": [1],\n'
            '      "zone": "left_column",\n'
            '      "span": "single_column",\n'
            '      "continues_previous": false\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "JSON only."
        )

    @staticmethod
    def _build_window_extract_prompt(
        *,
        document_name: str,
        page_numbers: list[int],
        extract_profile: str,
        extract_granularity: str,
    ) -> str:
        profile_text = {
            "academic_formula": "Prioritize equations and surrounding explanatory chunks.",
            "table_first": "Prioritize table structure, row/column fidelity, and compact table chunks.",
        }.get(extract_profile, "Balance paragraphs, equations, tables, and references.")
        granularity_text = {
            "fine": "Use fine chunking: create smaller semantic chunks and keep topic changes separate.",
            "coarse": "Use coarse chunking: keep semantically continuous discussion together in larger chunks.",
        }.get(extract_granularity, "Use medium chunking: prefer balanced semantic chunks.")
        page_label = ", ".join(str(int(page)) for page in list(page_numbers or []))
        return (
            f"Document: {document_name or 'unknown'}\n"
            f"Pages: {page_label}\n"
            f"Profile: {extract_profile}\n\n"
            f"Granularity: {extract_granularity}\n\n"
            "This output is for RAG ingestion, not layout analysis.\n"
            "For each page, read in strict human reading order and output final semantic chunks in that order.\n"
            f"{profile_text}\n"
            f"{granularity_text}\n"
            "Common scientific paper structure is usually: title/authors/affiliations/emails in front matter, then Abstract, then Keywords/Index Terms/CCS Concepts, then sectioned body content, then Acknowledgements, then References, then Appendices.\n"
            "Do not output visual blocks. Output final chunks only.\n"
            "Allowed chunk_type values: paragraph, equation, table.\n"
            "Allowed content_role values: abstract_body, body_paragraph, reference_entry, front_matter_misc, equation_body, table_body.\n"
            "Allowed cleanup_action values: keep, drop_from_body, route_to_metadata.\n"
            "Reference chunks must use chunk_type=paragraph with content_role=reference_entry.\n"
            "Put title/author/affiliation/email/identifier into document_metadata, not into chunks.\n"
            "Put Keywords, Index Terms, CCS Concepts, Additional Keywords and Phrases, funding/support lines, correspondence notes, and other front-matter-only text into document_metadata.front_matter_items, not into body chunks.\n"
            "Keep Abstract as retrievable content, usually with section_path=[\"Abstract\"].\n"
            "Treat Acknowledgements as an end-matter section in the body hierarchy, not as front matter metadata.\n"
            "For references pages, group consecutive references into semantically compact reference chunks. Do not emit one chunk per citation unless truly necessary.\n"
            "Page numbers in the JSON must match the input pages exactly.\n"
            "If a chunk continues from the previous page, set continues_previous=true.\n"
            "Optional merge hints when helpful: chunk_boundary=open|closed, table_header_state=present|repeated|missing, "
            "table_continues_previous=true|false, table_id_hint, table_caption, list_continues_previous=true|false, "
            "list_marker_type=bullet|numbered|lettered|roman, list_index_start, list_index_end.\n"
            "These merge hints are optional. Omit them when unclear.\n"
            "If the current page clearly belongs to a section/subsection, fill section_path and section_level_path. "
            "section_level_path must express structural depth, not chapter numbering literal value: main sections like I/II/III or 1/2/3 use level 1, A/B/C use level 2, and 1)/2)/3) under a subsection use level 3.\n"
            "When a page begins inside an already active section, include the inherited parent section_path instead of starting a new root section. For example, if the previous page established 'IV Methodology' and this page begins with 'B Navigation Policy Learning', output section_path=['IV Methodology','B Navigation Policy Learning'].\n"
            "Preserve source wording in text. Do not paraphrase or summarize.\n"
            "For equation chunks, fill latex when possible.\n"
            "For table chunks, fill table_markdown with a compact markdown table when possible.\n"
            "Optional lightweight hints: title_hint, zone, span.\n"
            "Omit keys that do not apply.\n"
            "Return JSON object:\n"
            "{\n"
            '  "pages": [\n'
            "    {\n"
            '      "page": 1,\n'
            '      "document_metadata": {\n'
            '        "title": null,\n'
            '        "authors": [],\n'
            '        "affiliations": [],\n'
            '        "emails": [],\n'
            '        "identifiers": [],\n'
            '        "front_matter_items": []\n'
            "      },\n"
            '      "chunks": [\n'
            "        {\n"
            '          "chunk_type": "paragraph",\n'
            '          "order": 1,\n'
            '          "text": "content",\n'
            '          "content_role": "body_paragraph",\n'
            '          "cleanup_action": "keep",\n'
            '          "section_path": ["1 Introduction"],\n'
            '          "section_level_path": [1],\n'
            '          "zone": "left_column",\n'
            '          "span": "single_column",\n'
            '          "continues_previous": false\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "JSON only."
        )

    def _normalize_page_payload(self, *, parsed: dict[str, Any], page_number: int) -> dict[str, Any]:
        document_metadata = self._normalize_document_metadata(parsed.get("document_metadata"))
        raw_chunks = (
            parsed.get("chunks")
            or parsed.get("items")
            or parsed.get("segments")
            or parsed.get("page_chunks")
            or parsed.get("content")
        )
        normalized = self._normalize_page_blocks(raw_blocks=list(raw_chunks or []), page_number=page_number)
        return {
            "blocks": normalized,
            "document_metadata": document_metadata,
        }

    def _normalize_window_payload(self, *, parsed: dict[str, Any], page_numbers: list[int]) -> dict[str, Any]:
        raw_pages = parsed.get("pages")
        document_metadata = self._empty_document_metadata()
        normalized: list[dict[str, Any]] = []
        if isinstance(raw_pages, list):
            allowed_pages = {int(page) for page in list(page_numbers or [])}
            for page_item in raw_pages:
                if not isinstance(page_item, dict):
                    continue
                try:
                    page_number = int(page_item.get("page"))
                except (TypeError, ValueError):
                    continue
                if page_number not in allowed_pages:
                    continue
                payload = self._normalize_page_payload(parsed=page_item, page_number=page_number)
                normalized.extend(list(payload.get("blocks") or []))
                document_metadata = self._merge_document_metadata(document_metadata, payload.get("document_metadata"))
        elif len(page_numbers) == 1:
            payload = self._normalize_page_payload(parsed=parsed, page_number=int(page_numbers[0]))
            normalized.extend(list(payload.get("blocks") or []))
            document_metadata = self._merge_document_metadata(document_metadata, payload.get("document_metadata"))
        normalized.sort(key=lambda row: (int(row.get("page") or 0), int(row.get("order") or 0), str(row.get("block_id") or "")))
        return {
            "blocks": normalized,
            "document_metadata": document_metadata,
        }

    @staticmethod
    def _normalize_page_blocks(*, raw_blocks: list[Any], page_number: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seq = 0
        for item in raw_blocks:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            latex = str(item.get("latex") or "").strip()
            table_markdown = str(item.get("table_markdown") or "").strip()
            raw_type = item.get("chunk_type") or item.get("type")
            block_type = OnlineMmIngestService._normalize_block_type(raw_type)
            raw_role = str(item.get("content_role") or "").strip().lower().replace("-", "_").replace(" ", "_")
            if block_type not in _SUPPORTED_MODEL_CHUNK_TYPES:
                if raw_role in {"reference_entry", "body_paragraph", "abstract_body", "front_matter_misc"} and (text or latex or table_markdown):
                    block_type = "paragraph"
                else:
                    continue
            if block_type == "equation" and not latex and text:
                latex = text
            if block_type == "table" and not table_markdown and text:
                table_markdown = text
            body = table_markdown if block_type == "table" else latex if block_type == "equation" else text
            if not body:
                continue
            next_order = int(item.get("order") or (seq + 1))
            content_role = OnlineMmIngestService._normalize_content_role(
                item.get("content_role"),
                block_type=block_type,
                text=text or body,
                page_number=page_number,
                order=next_order,
            )
            cleanup_action = OnlineMmIngestService._normalize_cleanup_action(
                item.get("cleanup_action"),
                block_type=block_type,
                content_role=content_role,
                text=text or body,
                chunk_hint=item.get("chunk_hint"),
                page_number=page_number,
                order=next_order,
            )
            section_path_titles = OnlineMmIngestService._normalize_string_list(item.get("section_path"), max_items=6, max_len=180)
            section_path_levels = OnlineMmIngestService._normalize_level_path(
                values=item.get("section_level_path"),
                fallback_len=len(section_path_titles),
            )
            if section_path_titles and len(section_path_levels) < len(section_path_titles):
                section_path_levels.extend(range(len(section_path_levels) + 1, len(section_path_titles) + 1))
            section_path_levels = OnlineMmIngestService._canonicalize_section_path_levels(
                titles=section_path_titles,
                levels=section_path_levels,
            )
            continues_previous = OnlineMmIngestService._normalize_optional_bool(
                item.get("continues_previous", item.get("continues_from_previous_page"))
            )
            table_continues_previous = OnlineMmIngestService._normalize_optional_bool(item.get("table_continues_previous"))
            list_continues_previous = OnlineMmIngestService._normalize_optional_bool(item.get("list_continues_previous"))
            seq += 1
            normalized.append(
                {
                    "block_id": f"p{int(page_number):04d}_b{seq:04d}",
                    "type": block_type,
                    "page": int(page_number),
                    "order": next_order,
                    "text": text or body,
                    "content_role": content_role,
                    "cleanup_action": cleanup_action,
                    "latex": latex or None,
                    "table_markdown": table_markdown or None,
                    "title_hint": str(item.get("title_hint") or item.get("title") or "").strip() or None,
                    "chunk_hint": OnlineMmIngestService._normalize_free_text(item.get("chunk_hint"), max_len=80),
                    "heading_anchor": (
                        section_path_titles[-1]
                        if section_path_titles
                        else OnlineMmIngestService._normalize_free_text(item.get("heading_anchor"), max_len=180)
                    ),
                    "heading_level": (
                        int(section_path_levels[-1])
                        if section_path_titles and section_path_levels
                        else OnlineMmIngestService._normalize_heading_level(item.get("heading_level"))
                    ),
                    "zone": OnlineMmIngestService._normalize_zone(item.get("zone")),
                    "span": OnlineMmIngestService._normalize_span(item.get("span")),
                    "continues_from_previous_page": bool(continues_previous),
                    "chunk_boundary": OnlineMmIngestService._normalize_chunk_boundary(item.get("chunk_boundary")),
                    "table_header_state": OnlineMmIngestService._normalize_table_header_state(item.get("table_header_state")),
                    "table_continues_previous": bool(table_continues_previous) if table_continues_previous is not None else None,
                    "table_id_hint": OnlineMmIngestService._normalize_free_text(item.get("table_id_hint"), max_len=80),
                    "table_caption": OnlineMmIngestService._normalize_free_text(item.get("table_caption"), max_len=240),
                    "list_continues_previous": bool(list_continues_previous) if list_continues_previous is not None else None,
                    "list_marker_type": OnlineMmIngestService._normalize_list_marker_type(item.get("list_marker_type")),
                    "list_index_start": OnlineMmIngestService._normalize_optional_int(item.get("list_index_start"), minimum=0, maximum=10000),
                    "list_index_end": OnlineMmIngestService._normalize_optional_int(item.get("list_index_end"), minimum=0, maximum=10000),
                    "relative_context": [],
                    "section_path_titles": section_path_titles,
                    "section_path_levels": section_path_levels,
                }
            )
        normalized.sort(key=lambda row: (int(row.get("page") or 0), int(row.get("order") or 0), str(row.get("block_id") or "")))
        return normalized

    def _build_model_chunk_plan(
        self,
        *,
        blocks: list[dict[str, Any]],
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
    ) -> dict[str, Any]:
        del document_name, extract_profile, extract_granularity
        ordered_blocks = sorted(
            list(blocks or []),
            key=lambda row: (
                int(row.get("page") or 0),
                int(row.get("order") or 0),
                str(row.get("block_id") or ""),
            ),
        )
        normalized: list[dict[str, Any]] = []
        chunk_seq = 0
        current_chunk: Optional[dict[str, Any]] = None

        def _next_chunk_id(prefix: str) -> str:
            nonlocal chunk_seq
            chunk_seq += 1
            return f"{prefix}_{chunk_seq:04d}"

        def _table_id_conflicts(previous_block: dict[str, Any], current_block: dict[str, Any]) -> bool:
            previous_hint = str(previous_block.get("table_id_hint") or "").strip().lower()
            current_hint = str(current_block.get("table_id_hint") or "").strip().lower()
            return bool(previous_hint and current_hint and previous_hint != current_hint)

        def _list_indices_continue(previous_block: dict[str, Any], current_block: dict[str, Any]) -> bool:
            previous_end = previous_block.get("list_index_end")
            current_start = current_block.get("list_index_start")
            if not isinstance(previous_end, int) or not isinstance(current_start, int):
                return False
            previous_marker = str(previous_block.get("list_marker_type") or "").strip()
            current_marker = str(current_block.get("list_marker_type") or "").strip()
            if previous_marker and current_marker and previous_marker != current_marker:
                return False
            return current_start == previous_end + 1

        def _should_merge_with_current(
            previous_chunk: Optional[dict[str, Any]],
            current_block: dict[str, Any],
            *,
            current_type: str,
            current_role: str,
            current_section_path: tuple[str, ...],
            current_section_levels: tuple[int, ...],
        ) -> bool:
            if previous_chunk is None:
                return False
            if previous_chunk.get("chunk_type") != current_type:
                return False
            if previous_chunk.get("content_role") != current_role:
                return False
            if tuple(previous_chunk.get("section_path") or ()) != current_section_path:
                return False
            if tuple(previous_chunk.get("section_levels") or ()) != current_section_levels:
                return False
            previous_block = previous_chunk.get("_last_block") or {}
            previous_page = int(previous_block.get("page") or 0)
            current_page = int(current_block.get("page") or 0)
            same_or_next_page = previous_page > 0 and current_page in {previous_page, previous_page + 1}
            current_continues = bool(current_block.get("continues_from_previous_page"))
            previous_open = str(previous_block.get("chunk_boundary") or "").strip().lower() == "open"

            if current_type == "table":
                if _table_id_conflicts(previous_block, current_block):
                    return False
                if bool(current_block.get("table_continues_previous")) and same_or_next_page:
                    return True
                if str(current_block.get("table_header_state") or "").strip().lower() in {"missing", "repeated"} and same_or_next_page:
                    return True

            list_marker = str(current_block.get("list_marker_type") or "").strip()
            if bool(current_block.get("list_continues_previous")) and list_marker and same_or_next_page:
                return True
            if list_marker and _list_indices_continue(previous_block, current_block) and same_or_next_page:
                return True

            return current_continues and same_or_next_page and (previous_open or current_type in {"paragraph", "equation", "table"})

        for block in ordered_blocks:
            block_id = str(block.get("block_id") or "").strip()
            block_type = str(block.get("type") or "").strip().lower()
            cleanup_action = str(block.get("cleanup_action") or "").strip()
            if not block_id or block_type not in _INGEST_BLOCK_TYPES:
                continue
            if cleanup_action in {"drop_from_body", "route_to_metadata"}:
                continue
            block_section_path = tuple(str(item) for item in list(block.get("section_path_titles") or []) if str(item or "").strip())
            block_section_levels = tuple(int(item) for item in list(block.get("section_path_levels") or []) if isinstance(item, int) and int(item) > 0)
            block_role = str(block.get("content_role") or "").strip()
            block_hint = str(block.get("chunk_hint") or "").strip() or None
            can_merge = _should_merge_with_current(
                current_chunk,
                block,
                current_type=block_type,
                current_role=block_role,
                current_section_path=block_section_path,
                current_section_levels=block_section_levels,
            )
            if can_merge:
                current_chunk["block_ids"].append(block_id)
                tags = set(current_chunk.get("retrieval_tags") or [])
                if block_hint:
                    tags.add(block_hint)
                current_chunk["retrieval_tags"] = list(sorted(tags))
                current_chunk["_last_block"] = block
                continue
            prefix = "eq" if block_type == "equation" else "tbl" if block_type == "table" else "p"
            current_chunk = {
                "chunk_id": _next_chunk_id(prefix),
                "chunk_type": block_type,
                "title": str(block.get("title_hint") or "").strip() or None,
                "block_ids": [block_id],
                "needs_parent_context": True,
                "retrieval_tags": [tag for tag in [block_type, block_hint, block_role] if tag],
                "content_role": block_role,
                "section_path": list(block_section_path),
                "section_levels": list(block_section_levels),
                "_last_block": block,
            }
            normalized.append(current_chunk)
        for chunk_item in normalized:
            chunk_item.pop("_last_block", None)
        return {"ok": True, "chunks": normalized, "usage": {}}

    def _materialize_chunks(
        self,
        *,
        blocks: list[dict[str, Any]],
        chunk_plan: list[dict[str, Any]],
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
        source_model: str,
        document_metadata_seed: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        ordered_blocks = sorted(
            list(blocks or []),
            key=lambda row: (
                int(row.get("page") or 0),
                int(row.get("order") or 0),
                str(row.get("block_id") or ""),
            ),
        )
        order_index = {
            str(block.get("block_id") or "").strip(): idx
            for idx, block in enumerate(ordered_blocks)
            if str(block.get("block_id") or "").strip()
        }
        block_by_id = {str(block.get("block_id") or "").strip(): block for block in blocks}
        title_candidate = self._detect_document_title(ordered_blocks)
        document_title = (
            str((document_metadata_seed or {}).get("title") or "").strip()
            or (str(title_candidate.get("text") or "").strip() if title_candidate else "")
            or None
        )
        document_label = document_title or str(document_name or "").strip() or "Document"
        document_context_id = "doc_root"
        front_matter_context_id = "front_matter"
        section_spine = self._build_section_spine(
            blocks=ordered_blocks,
            order_index=order_index,
            title_block_id=str(title_candidate.get("block_id") or "").strip() if title_candidate else "",
        )
        first_section_position = min(
            [
                int(section.get("anchor_position") or -1)
                for section in section_spine
                if int(section.get("anchor_position") or -1) >= 0
            ]
            or [-1]
        )
        document_metadata = self._extract_document_metadata(
            blocks=ordered_blocks,
            document_title=document_title,
            first_section_position=first_section_position,
            metadata_seed=document_metadata_seed,
        )
        front_matter_support_blocks = [
            block
            for index, block in enumerate(ordered_blocks)
            if self._block_body(block)
            and (
                index < first_section_position
                or str(block.get("content_role") or "").strip() in _FRONT_MATTER_CONTENT_ROLES
                or str(block.get("cleanup_action") or "").strip() == "route_to_metadata"
            )
            and str(block.get("content_role") or "").strip() != "abstract_body"
        ] if first_section_position >= 0 else [
            block
            for index, block in enumerate(ordered_blocks)
            if self._block_body(block)
            and (
                index < min(len(ordered_blocks), 24)
                or str(block.get("content_role") or "").strip() in _FRONT_MATTER_CONTENT_ROLES
                or str(block.get("cleanup_action") or "").strip() == "route_to_metadata"
            )
            and str(block.get("content_role") or "").strip() != "abstract_body"
        ]
        section_child_context_ids: dict[str, list[str]] = {}
        for section in section_spine:
            section_id = str(section.get("context_id") or "").strip()
            if not section_id:
                continue
            parent_section_id = str(section.get("parent_context_id") or "").strip()
            if parent_section_id:
                section_child_context_ids.setdefault(parent_section_id, []).append(section_id)
        document_parts: list[str] = []
        chunks: list[dict[str, Any]] = []
        context_chunks: list[dict[str, Any]] = []
        context_children: dict[str, list[dict[str, Any]]] = {}
        section_descendant_chunks: dict[str, list[dict[str, Any]]] = {}
        cursor = 0
        for chunk_item in chunk_plan:
            chunk_type = str(chunk_item.get("chunk_type") or "").strip().lower()
            if chunk_type not in _SUPPORTED_CHUNK_TYPES:
                continue
            picked_blocks = [
                block_by_id[block_id]
                for block_id in list(chunk_item.get("block_ids") or [])
                if str(block_id or "").strip() in block_by_id
            ]
            if not picked_blocks:
                continue
            first_block_id = str((chunk_item.get("block_ids") or [None])[0] or "").strip()
            first_block_position = order_index.get(first_block_id, -1)
            parent_candidate = self._resolve_parent_section(
                section_spine=section_spine,
                first_block_position=first_block_position,
            )
            parent_context_id: Optional[str] = None
            parent_section_title: Optional[str] = None
            parent_section_type: Optional[str] = None
            if parent_candidate:
                parent_context_id = str(parent_candidate.get("context_id") or "").strip() or None
                parent_section_title = str(parent_candidate.get("section_title") or "").strip() or None
                parent_section_type = str(parent_candidate.get("section_type") or "").strip() or None
            elif first_section_position >= 0 and 0 <= int(first_block_position) < int(first_section_position):
                parent_context_id = front_matter_context_id
                parent_section_title = "Front Matter"
                parent_section_type = "front_matter"
            else:
                parent_context_id = document_context_id
                parent_section_title = document_label
                parent_section_type = "document"
            content = self._compose_chunk_content(chunk_type=chunk_type, chunk_item=chunk_item, blocks=picked_blocks)
            if not content:
                continue
            start_char = cursor
            if document_parts:
                document_parts.append("\n\n")
                cursor += 2
                start_char = cursor
            document_parts.append(content)
            cursor += len(content)
            end_char = cursor
            pages = [int(block.get("page") or 0) for block in picked_blocks if int(block.get("page") or 0) > 0]
            heading_levels = sorted(
                {
                    int(level)
                    for level in [block.get("heading_level") for block in picked_blocks]
                    if isinstance(level, int) and level > 0
                }
            )
            zone_hints = sorted(
                {
                    str(zone).strip()
                    for zone in [block.get("zone") for block in picked_blocks]
                    if str(zone or "").strip()
                }
            )
            layout_spans = sorted(
                {
                    str(span).strip()
                    for span in [block.get("span") for block in picked_blocks]
                    if str(span or "").strip()
                }
            )
            chunk_hints = sorted(
                {
                    str(hint).strip()
                    for hint in [block.get("chunk_hint") for block in picked_blocks]
                    if str(hint or "").strip()
                }
            )
            heading_anchor_hints = sorted(
                {
                    str(hint).strip()
                    for hint in [block.get("heading_anchor") for block in picked_blocks]
                    if str(hint or "").strip()
                }
            )
            content_roles = sorted(
                {
                    str(role).strip()
                    for role in [block.get("content_role") for block in picked_blocks]
                    if str(role or "").strip()
                }
            )
            cleanup_actions = sorted(
                {
                    str(action).strip()
                    for action in [block.get("cleanup_action") for block in picked_blocks]
                    if str(action or "").strip()
                }
            )
            relative_context: list[str] = []
            for block in picked_blocks:
                raw_relative_context = block.get("relative_context")
                if isinstance(raw_relative_context, dict):
                    tokens = [
                        f"{str(key or '').strip()}:{str(value or '').strip()}"
                        for key, value in raw_relative_context.items()
                    ]
                elif isinstance(raw_relative_context, list):
                    tokens = [str(token or "").strip() for token in raw_relative_context]
                elif isinstance(raw_relative_context, str):
                    tokens = [raw_relative_context]
                else:
                    tokens = []
                for token in tokens:
                    normalized_token = str(token or "").strip()
                    if normalized_token and normalized_token not in relative_context:
                        relative_context.append(normalized_token)
                    if len(relative_context) >= 12:
                        break
                if len(relative_context) >= 12:
                    break
            chunk_title = str(chunk_item.get("title") or "").strip() or None
            resolved_section_title = parent_section_title or chunk_title
            has_citations = "[" in content and "]" in content
            chunk_id = generate_chunk_id(content, start_char)
            hierarchy_path_ids = [document_context_id]
            hierarchy_path_titles = [document_label]
            hierarchy_path_types = ["document"]
            hierarchy_path_levels = [0]
            if parent_candidate:
                hierarchy_path_ids.extend(
                    [str(item) for item in list(parent_candidate.get("path_context_ids") or []) if str(item or "").strip()]
                )
                hierarchy_path_titles.extend(
                    [str(item) for item in list(parent_candidate.get("path_titles") or []) if str(item or "").strip()]
                )
                hierarchy_path_types.extend(
                    [str(item) for item in list(parent_candidate.get("path_types") or []) if str(item or "").strip()]
                )
                hierarchy_path_levels.extend(
                    [
                        int(item)
                        for item in list(parent_candidate.get("path_levels") or [])
                        if isinstance(item, int) and int(item) > 0
                    ]
                )
            elif parent_context_id == front_matter_context_id:
                hierarchy_path_ids.append(front_matter_context_id)
                hierarchy_path_titles.append("Front Matter")
                hierarchy_path_types.append("front_matter")
                hierarchy_path_levels.append(1)
            chunk_row = {
                "id": chunk_id,
                "content": content,
                "start_char": int(start_char),
                "end_char": int(end_char),
                "metadata": {
                    "level": "paragraph",
                    "section_type": chunk_type,
                    "section_title": resolved_section_title,
                    "has_citations": has_citations,
                    "extra": {
                        "ingest_mode": "online_mm",
                        "chunk_type": chunk_type,
                        "chunk_title": chunk_title,
                        "block_ids": [str(block.get("block_id") or "").strip() for block in picked_blocks],
                        "page_span": [min(pages), max(pages)] if pages else [],
                        "heading_levels": heading_levels,
                        "zone_hints": zone_hints,
                        "layout_spans": layout_spans,
                        "chunk_hints": chunk_hints,
                        "heading_anchor_hints": heading_anchor_hints,
                        "content_roles": content_roles,
                        "cleanup_actions": cleanup_actions,
                        "continues_from_previous_page": any(
                            bool(block.get("continues_from_previous_page")) for block in picked_blocks
                        ),
                        "relative_context": relative_context,
                        "retrieval_tags": list(chunk_item.get("retrieval_tags") or []),
                        "source_model": source_model,
                        "document_name": str(document_name or "").strip(),
                        "document_title": document_title,
                        "extract_profile": str(extract_profile or "general").strip(),
                        "extract_granularity": str(extract_granularity or "medium").strip(),
                        "document_metadata": dict(document_metadata),
                        "source_order_span": [
                            min([int(block.get("order") or 0) for block in picked_blocks] or [0]),
                            max([int(block.get("order") or 0) for block in picked_blocks] or [0]),
                        ],
                        "context_path_ids": hierarchy_path_ids,
                        "context_path_titles": hierarchy_path_titles,
                        "context_path_types": hierarchy_path_types,
                        "context_path_levels": hierarchy_path_levels,
                        "context_depth": len(hierarchy_path_ids),
                    },
                },
            }
            if parent_context_id:
                chunk_row["metadata"]["parent_id"] = parent_context_id
                chunk_row["metadata"]["extra"]["parent_section_title"] = parent_section_title
                chunk_row["metadata"]["extra"]["parent_section_type"] = parent_section_type
                chunk_row["metadata"]["extra"]["parent_section_id"] = (
                    str(parent_candidate.get("context_id") or "").strip() or None if parent_candidate else None
                )
                chunk_row["metadata"]["extra"]["parent_anchor_page"] = int(parent_candidate.get("page") or 0) if parent_candidate else 0
                context_children.setdefault(parent_context_id, []).append(chunk_row)
            if parent_candidate:
                for ancestor_context_id in [
                    str(item) for item in list(parent_candidate.get("path_context_ids") or []) if str(item or "").strip()
                ]:
                    section_descendant_chunks.setdefault(ancestor_context_id, []).append(chunk_row)
            chunks.append(chunk_row)
        document_text = "".join(document_parts)
        if not document_text.strip():
            return {"chunks": [], "document_text": "", "failure_reason": "materialized_document_empty"}
        document_child_context_ids: list[str] = []
        front_matter_children = list(context_children.get(front_matter_context_id) or [])
        if front_matter_children or front_matter_support_blocks:
            if front_matter_children:
                front_matter_start = min(int(child.get("start_char") or 0) for child in front_matter_children)
                front_matter_end = max(int(child.get("end_char") or 0) for child in front_matter_children)
                front_matter_preview = "\n\n".join(
                    str(child.get("content") or "").strip()
                    for child in front_matter_children
                    if str(child.get("content") or "").strip()
                ).strip()
                front_matter_page_values = [
                    int(value)
                    for child in front_matter_children
                    for value in list(((child.get("metadata") or {}).get("extra") or {}).get("page_span") or [])
                    if int(value or 0) > 0
                ]
            else:
                front_matter_start = 0
                front_matter_end = 0
                front_matter_preview = "\n\n".join(
                    self._block_body(block)
                    for block in front_matter_support_blocks
                    if self._block_body(block)
                ).strip()
                front_matter_page_values = [
                    int(block.get("page") or 0)
                    for block in front_matter_support_blocks
                    if int(block.get("page") or 0) > 0
                ]
            if len(front_matter_preview) > 1200:
                front_matter_preview = front_matter_preview[:1200].rstrip() + "..."
            front_matter_title = "Front Matter"
            context_chunks.append(
                {
                    "id": front_matter_context_id,
                    "content": f"{front_matter_title}\n\n{front_matter_preview}".strip()
                    if front_matter_preview
                    else front_matter_title,
                    "start_char": int(front_matter_start),
                    "end_char": int(front_matter_end),
                    "metadata": {
                        "level": "section",
                        "section_type": "front_matter",
                        "section_title": front_matter_title,
                        "parent_id": document_context_id,
                        "has_citations": any(
                            bool((child.get("metadata") or {}).get("has_citations")) for child in front_matter_children
                        ),
                        "child_ids": [
                            str(child.get("id") or "").strip()
                            for child in front_matter_children
                            if str(child.get("id") or "").strip()
                        ],
                        "extra": {
                            "ingest_mode": "online_mm",
                            "context_kind": "front_matter",
                            "page_span": [
                                min(front_matter_page_values or [0]),
                                max(front_matter_page_values or [0]),
                            ],
                            "source_model": source_model,
                            "document_name": str(document_name or "").strip(),
                            "document_title": document_title,
                            "extract_profile": str(extract_profile or "general").strip(),
                            "extract_granularity": str(extract_granularity or "medium").strip(),
                            "document_metadata": dict(document_metadata),
                            "child_context_ids": [],
                            "descendant_chunk_ids": [
                                str(child.get("id") or "").strip()
                                for child in front_matter_children
                                if str(child.get("id") or "").strip()
                            ][:256],
                            "context_path_ids": [document_context_id, front_matter_context_id],
                            "context_path_titles": [document_label, front_matter_title],
                            "context_path_types": ["document", "front_matter"],
                            "context_path_levels": [0, 1],
                            "context_depth": 2,
                        },
                    },
                }
            )
            document_child_context_ids.append(front_matter_context_id)
        active_section_ids = {
            str(section.get("context_id") or "").strip()
            for section in section_spine
            if list(section_descendant_chunks.get(str(section.get("context_id") or "").strip()) or [])
        }
        for section in section_spine:
            section_id = str(section.get("context_id") or "").strip()
            if section_id not in active_section_ids:
                continue
            direct_children = list(context_children.get(section_id) or [])
            descendant_children = list(section_descendant_chunks.get(section_id) or [])
            assigned_children = descendant_children or direct_children
            child_context_ids = [
                child_id
                for child_id in list(section_child_context_ids.get(section_id) or [])
                if child_id in active_section_ids
            ]
            if not assigned_children:
                continue
            start_char = min(int(child.get("start_char") or 0) for child in assigned_children)
            end_char = max(int(child.get("end_char") or 0) for child in assigned_children)
            body_parts: list[str] = []
            for child in assigned_children:
                text = str(child.get("content") or "").strip()
                if not text:
                    continue
                body_parts.append(text)
                if sum(len(part) for part in body_parts) >= 1200:
                    break
            body_preview = "\n\n".join(body_parts).strip()
            if len(body_preview) > 1400:
                body_preview = body_preview[:1400].rstrip() + "..."
            section_title = str(section.get("section_title") or "").strip()
            context_content = f"{section_title}\n\n{body_preview}".strip() if body_preview else section_title
            page_values = [
                int(value)
                for child in assigned_children
                for value in list(((child.get("metadata") or {}).get("extra") or {}).get("page_span") or [])
                if int(value or 0) > 0
            ]
            section_parent_context_id = str(section.get("parent_context_id") or "").strip() or document_context_id
            section_path_ids = [document_context_id]
            section_path_titles = [document_label]
            section_path_types = ["document"]
            section_path_levels = [0]
            section_path_ids.extend(
                [str(item) for item in list(section.get("path_context_ids") or []) if str(item or "").strip()]
            )
            section_path_titles.extend(
                [str(item) for item in list(section.get("path_titles") or []) if str(item or "").strip()]
            )
            section_path_types.extend(
                [str(item) for item in list(section.get("path_types") or []) if str(item or "").strip()]
            )
            section_path_levels.extend(
                [
                    int(item)
                    for item in list(section.get("path_levels") or [])
                    if isinstance(item, int) and int(item) > 0
                ]
            )
            context_chunks.append(
                {
                    "id": section_id,
                    "content": context_content,
                    "start_char": int(start_char),
                    "end_char": int(end_char),
                    "metadata": {
                        "level": "section",
                        "section_type": str(section.get("section_type") or "section").strip() or "section",
                        "section_title": section_title or None,
                        "parent_id": section_parent_context_id,
                        "has_citations": any(bool((child.get("metadata") or {}).get("has_citations")) for child in assigned_children),
                        "child_ids": [str(child.get("id") or "").strip() for child in direct_children if str(child.get("id") or "").strip()],
                        "extra": {
                            "ingest_mode": "online_mm",
                            "context_kind": "section_spine",
                            "anchor_block_id": str(section.get("anchor_block_id") or "").strip(),
                            "anchor_page": int(section.get("page") or 0),
                            "anchor_heading_level": section.get("heading_level"),
                            "anchor_zone": section.get("zone"),
                            "anchor_span": section.get("span"),
                            "page_span": [min(page_values), max(page_values)] if page_values else [],
                            "source_model": source_model,
                            "document_name": str(document_name or "").strip(),
                            "document_title": document_title,
                            "extract_profile": str(extract_profile or "general").strip(),
                            "extract_granularity": str(extract_granularity or "medium").strip(),
                            "document_metadata": dict(document_metadata),
                            "child_context_ids": child_context_ids,
                            "descendant_chunk_ids": [
                                str(child.get("id") or "").strip()
                                for child in assigned_children
                                if str(child.get("id") or "").strip()
                            ][:256],
                            "context_path_ids": section_path_ids,
                            "context_path_titles": section_path_titles,
                            "context_path_types": section_path_types,
                            "context_path_levels": section_path_levels,
                            "context_depth": len(section_path_ids),
                        },
                    },
                }
            )
            if section_parent_context_id == document_context_id:
                document_child_context_ids.append(section_id)
        direct_document_children = [
            child
            for child in list(context_children.get(document_context_id) or [])
            if str(child.get("id") or "").strip()
        ]
        document_preview_parts: list[str] = []
        for ctx in context_chunks:
            title = str(((ctx.get("metadata") or {}).get("section_title")) or "").strip()
            content_preview = str(ctx.get("content") or "").strip()
            if content_preview:
                document_preview_parts.append(content_preview if not title else content_preview)
            if sum(len(part) for part in document_preview_parts) >= 1500:
                break
        if not document_preview_parts:
            for child in chunks:
                text = str(child.get("content") or "").strip()
                if not text:
                    continue
                document_preview_parts.append(text)
                if sum(len(part) for part in document_preview_parts) >= 1500:
                    break
        document_preview = "\n\n".join(document_preview_parts).strip()
        if len(document_preview) > 1800:
            document_preview = document_preview[:1800].rstrip() + "..."
        root_start = min([int(chunk.get("start_char") or 0) for chunk in chunks] or [0])
        root_end = max([int(chunk.get("end_char") or 0) for chunk in chunks] or [0])
        context_chunks.insert(
            0,
            {
                "id": document_context_id,
                "content": f"{document_label}\n\n{document_preview}".strip() if document_preview else document_label,
                "start_char": int(root_start),
                "end_char": int(root_end),
                "metadata": {
                    "level": "document",
                    "section_type": "document",
                    "section_title": document_label,
                    "has_citations": any(bool((child.get("metadata") or {}).get("has_citations")) for child in chunks),
                    "child_ids": [str(child.get("id") or "").strip() for child in direct_document_children],
                    "extra": {
                        "ingest_mode": "online_mm",
                        "context_kind": "document_root",
                        "child_context_ids": document_child_context_ids,
                        "descendant_chunk_ids": [
                            str(child.get("id") or "").strip()
                            for child in chunks
                            if str(child.get("id") or "").strip()
                        ][:256],
                        "page_span": [
                            min(
                                [
                                    int(value)
                                    for child in chunks
                                    for value in list(((child.get("metadata") or {}).get("extra") or {}).get("page_span") or [])
                                    if int(value or 0) > 0
                                ]
                                or [0]
                            ),
                            max(
                                [
                                    int(value)
                                    for child in chunks
                                    for value in list(((child.get("metadata") or {}).get("extra") or {}).get("page_span") or [])
                                    if int(value or 0) > 0
                                ]
                                or [0]
                            ),
                        ],
                        "source_model": source_model,
                        "document_name": str(document_name or "").strip(),
                        "document_title": document_title,
                        "extract_profile": str(extract_profile or "general").strip(),
                        "extract_granularity": str(extract_granularity or "medium").strip(),
                        "document_metadata": dict(document_metadata),
                        "context_path_ids": [document_context_id],
                        "context_path_titles": [document_label],
                        "context_path_types": ["document"],
                        "context_path_levels": [0],
                        "context_depth": 1,
                    },
                },
            },
        )
        return {
            "chunks": chunks,
            "context_chunks": context_chunks,
            "document_text": document_text,
            "document_title": document_title,
            "document_metadata": document_metadata,
            "section_spine": [
                {
                    "context_id": str(section.get("context_id") or "").strip(),
                    "parent_context_id": str(section.get("parent_context_id") or "").strip() or None,
                    "section_title": str(section.get("section_title") or "").strip(),
                    "section_type": str(section.get("section_type") or "").strip(),
                    "page": int(section.get("page") or 0),
                    "anchor_block_id": str(section.get("anchor_block_id") or "").strip(),
                    "heading_level": int(section.get("heading_level") or 0),
                    "path_context_ids": [
                        str(item)
                        for item in list(section.get("path_context_ids") or [])
                        if str(item or "").strip()
                    ],
                    "path_titles": [
                        str(item)
                        for item in list(section.get("path_titles") or [])
                        if str(item or "").strip()
                    ],
                    "path_levels": [
                        int(item)
                        for item in list(section.get("path_levels") or [])
                        if isinstance(item, int) and int(item) > 0
                    ],
                }
                for section in section_spine
            ],
        }

    def _compose_chunk_content(
        self,
        *,
        chunk_type: str,
        chunk_item: dict[str, Any],
        blocks: list[dict[str, Any]],
    ) -> str:
        title = str(chunk_item.get("title") or "").strip()
        if chunk_type == "equation":
            equations = [str(block.get("latex") or self._block_body(block) or "").strip() for block in blocks]
            equations = [item for item in equations if item]
            if not equations:
                return ""
            body = "\n\n".join(f"$$\n{item}\n$$" for item in equations)
            return f"{title}\n\n{body}".strip() if title else body
        if chunk_type == "table":
            title_parts = [title] if title else []
            body_parts = []
            for block in blocks:
                table_markdown = str(block.get("table_markdown") or self._block_body(block) or "").strip()
                if table_markdown:
                    body_parts.append(table_markdown)
            if not body_parts:
                return ""
            parts = title_parts + body_parts
            return "\n\n".join(parts).strip()
        body = "\n\n".join(self._block_body(block) for block in blocks if self._block_body(block))
        return f"{title}\n\n{body}".strip() if title else body.strip()

    @staticmethod
    def _block_body(block: dict[str, Any]) -> str:
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "equation":
            return str(block.get("latex") or block.get("text") or "").strip()
        if block_type == "table":
            return str(block.get("table_markdown") or block.get("text") or "").strip()
        return str(block.get("text") or "").strip()

    @staticmethod
    def _block_display_text(block: dict[str, Any]) -> str:
        return str(block.get("text") or "").strip()

    @staticmethod
    def _normalize_extract_granularity(value: Any) -> str:
        token = str(value or "").strip().lower()
        if token in _SUPPORTED_EXTRACT_GRANULARITIES:
            return token
        return "medium"

    @staticmethod
    def _resolve_multimodal_max_tokens(*, model: str, requested: Any, minimum: int) -> int:
        floor = max(1, int(minimum))
        try:
            requested_value = int(requested)
        except (TypeError, ValueError):
            requested_value = floor
        ceiling = int(_MULTIMODAL_MODEL_MAX_TOKENS.get(str(model or "").strip().lower()) or 8192)
        return max(floor, min(requested_value, ceiling))

    @staticmethod
    def _normalize_content_role(
        value: Any,
        *,
        block_type: str,
        text: str,
        page_number: int,
        order: int,
    ) -> str:
        forced = OnlineMmIngestService._force_content_role(
            block_type=block_type,
            text=text,
            page_number=page_number,
            order=order,
        )
        if forced:
            return forced
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if token:
            token = str(_CONTENT_ROLE_ALIASES.get(token) or token)
        if token in _SUPPORTED_CONTENT_ROLES:
            return token
        return OnlineMmIngestService._infer_content_role(
            block_type=block_type,
            text=text,
            page_number=page_number,
            order=order,
        )

    @staticmethod
    def _normalize_cleanup_action(
        value: Any,
        *,
        block_type: str,
        content_role: str,
        text: str,
        chunk_hint: Any,
        page_number: int,
        order: int,
    ) -> str:
        forced = OnlineMmIngestService._force_cleanup_action(
            block_type=block_type,
            content_role=content_role,
            text=text,
            page_number=page_number,
            order=order,
        )
        if forced:
            return forced
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if token:
            token = str(_CLEANUP_ACTION_ALIASES.get(token) or token)
        if token in _SUPPORTED_CLEANUP_ACTIONS:
            return token
        return OnlineMmIngestService._infer_cleanup_action(
            block_type=block_type,
            content_role=content_role,
            text=text,
            chunk_hint=chunk_hint,
            page_number=page_number,
            order=order,
        )

    @staticmethod
    def _infer_cleanup_action(
        *,
        block_type: str,
        content_role: str,
        text: str,
        chunk_hint: Any,
        page_number: int,
        order: int,
    ) -> str:
        hint = str(chunk_hint or "").strip().lower()
        if "page_number" in hint or re.fullmatch(r"\d{1,3}", str(text or "").strip()):
            return "drop_from_body"
        if content_role in _FRONT_MATTER_CONTENT_ROLES:
            return "route_to_metadata"
        return "keep"

    @staticmethod
    def _force_cleanup_action(
        *,
        block_type: str,
        content_role: str,
        text: str,
        page_number: int,
        order: int,
    ) -> Optional[str]:
        del block_type, content_role, order
        value = str(text or "").strip()
        lowered = value.lower()
        if _INDEX_TERMS_PATTERN.match(value) or _KEYWORDS_PATTERN.match(value):
            return "route_to_metadata"
        if page_number <= 2 and (
            _CORRESPONDING_AUTHOR_PATTERN.search(value)
            or _FUNDING_HINT_PATTERN.search(value)
            or _EMAIL_PATTERN.search(value)
            or _DOI_PATTERN.search(value)
            or _ARXIV_PATTERN.search(value)
        ):
            return "route_to_metadata"
        return None

    @staticmethod
    def _force_content_role(
        *,
        block_type: str,
        text: str,
        page_number: int,
        order: int,
    ) -> Optional[str]:
        del order
        lowered = str(text or "").strip().lower()
        block_type = str(block_type or "").strip().lower()
        if block_type == "equation":
            return "equation_body"
        if block_type == "table":
            return "table_body"
        if lowered.startswith("abstract"):
            return "abstract_body"
        if _INDEX_TERMS_PATTERN.match(text) or _KEYWORDS_PATTERN.match(text):
            return "front_matter_misc"
        if _EMAIL_PATTERN.search(lowered):
            return "front_matter_misc"
        if _DOI_PATTERN.search(text) or _ARXIV_PATTERN.search(text) or lowered.startswith("doi") or lowered.startswith("arxiv"):
            return "front_matter_misc"
        if _CORRESPONDING_AUTHOR_PATTERN.search(text):
            return "front_matter_misc"
        if page_number <= 2 and _FUNDING_HINT_PATTERN.search(text):
            return "front_matter_misc"
        if page_number <= 2 and _AFFILIATION_HINT_PATTERN.search(text):
            return "front_matter_misc"
        return None

    @staticmethod
    def _infer_content_role(
        *,
        block_type: str,
        text: str,
        page_number: int,
        order: int,
    ) -> str:
        lowered = str(text or "").strip().lower()
        block_type = str(block_type or "").strip().lower()
        if block_type == "equation":
            return "equation_body"
        if block_type == "table":
            return "table_body"
        if lowered.startswith("abstract"):
            return "abstract_body"
        if _EMAIL_PATTERN.search(lowered):
            return "front_matter_misc"
        if _DOI_PATTERN.search(text) or _ARXIV_PATTERN.search(text) or lowered.startswith("doi") or lowered.startswith("arxiv"):
            return "front_matter_misc"
        if page_number <= 2 and _AFFILIATION_HINT_PATTERN.search(text):
            return "front_matter_misc"
        if re.match(r"^\s*\[\d+\]", text):
            return "reference_entry"
        if page_number <= 2 and order <= 3 and len(lowered.split()) <= 16:
            return "front_matter_misc"
        return "body_paragraph"

    def _extract_document_metadata(
        self,
        *,
        blocks: list[dict[str, Any]],
        document_title: Optional[str],
        first_section_position: int,
        metadata_seed: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        front_matter_limit = int(first_section_position) if int(first_section_position) >= 0 else min(len(blocks), 24)
        front_matter_blocks = [
            block
            for index, block in enumerate(list(blocks or []))
            if index < front_matter_limit or str(block.get("content_role") or "").strip() in {
                "front_matter_misc",
            }
        ]
        authors: list[str] = []
        affiliations: list[str] = []
        emails: list[str] = []
        identifiers: list[str] = []
        front_matter_items: list[str] = []
        for block in front_matter_blocks:
            role = str(block.get("content_role") or "").strip()
            body = self._block_body(block)
            if not body:
                continue
            if role == "front_matter_misc":
                if _AFFILIATION_HINT_PATTERN.search(body):
                    self._append_unique_text(affiliations, body)
            for email in _EMAIL_PATTERN.findall(body):
                self._append_unique_text(emails, email)
            for identifier in self._extract_identifiers(body):
                self._append_unique_text(identifiers, identifier)
            if role == "front_matter_misc" and not _AFFILIATION_HINT_PATTERN.search(body):
                self._append_unique_text(front_matter_items, body)
        inferred = {
            "title": str(document_title or "").strip() or None,
            "authors": authors[:16],
            "affiliations": affiliations[:16],
            "emails": emails[:32],
            "identifiers": identifiers[:16],
            "front_matter_items": front_matter_items[:24],
        }
        return self._merge_document_metadata(inferred, metadata_seed)

    @staticmethod
    def _extract_identifiers(text: str) -> list[str]:
        values: list[str] = []
        for pattern in (_DOI_PATTERN, _ARXIV_PATTERN):
            for match in pattern.findall(str(text or "")):
                token = str(match or "").strip()
                if token and token not in values:
                    values.append(token)
        return values

    @staticmethod
    def _append_unique_text(target: list[str], value: Any) -> None:
        token = str(value or "").strip()
        if not token or token in target:
            return
        target.append(token[:240])

    @staticmethod
    def _normalize_heading_level(value: Any) -> Optional[int]:
        try:
            level = int(value)
        except (TypeError, ValueError):
            return None
        return level if 1 <= level <= 6 else None

    @staticmethod
    def _normalize_optional_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            token = str(value or "").strip().lower()
            if token in {"true", "yes", "1"}:
                return True
            if token in {"false", "no", "0"}:
                return False
        return None

    @staticmethod
    def _normalize_optional_int(value: Any, *, minimum: int, maximum: int) -> Optional[int]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if int(minimum) <= number <= int(maximum) else None

    @staticmethod
    def _normalize_block_type(value: Any) -> str:
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not token:
            return ""
        if token in _SUPPORTED_BLOCK_TYPES:
            return token
        return str(_BLOCK_TYPE_ALIASES.get(token) or token)

    @staticmethod
    def _normalize_zone(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower()
        if not token:
            return None
        token = str(_ZONE_ALIASES.get(token) or token)
        return token if token in _ZONE_CHOICES else token[:40]

    @staticmethod
    def _normalize_span(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower()
        if not token:
            return None
        token = str(_SPAN_ALIASES.get(token) or token)
        return token if token in _SPAN_CHOICES else token[:40]

    @staticmethod
    def _normalize_free_text(value: Any, *, max_len: int) -> Optional[str]:
        token = str(value or "").strip()
        if not token:
            return None
        return token[:max(1, int(max_len))]

    @staticmethod
    def _normalize_chunk_boundary(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not token:
            return None
        token = str(_CHUNK_BOUNDARY_ALIASES.get(token) or token)
        return token if token in _SUPPORTED_CHUNK_BOUNDARY_STATES else None

    @staticmethod
    def _normalize_table_header_state(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not token:
            return None
        token = str(_TABLE_HEADER_STATE_ALIASES.get(token) or token)
        return token if token in _SUPPORTED_TABLE_HEADER_STATES else None

    @staticmethod
    def _normalize_list_marker_type(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not token:
            return None
        token = str(_LIST_MARKER_TYPE_ALIASES.get(token) or token)
        return token if token in _SUPPORTED_LIST_MARKER_TYPES else None

    def _normalize_document_metadata(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return self._empty_document_metadata()
        return self._merge_document_metadata({}, value)

    @staticmethod
    def _normalize_string_list(value: Any, *, max_items: int, max_len: int) -> list[str]:
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            return []
        normalized: list[str] = []
        for item in items:
            token = str(item or "").strip()
            if not token or token in normalized:
                continue
            normalized.append(token[:max_len])
            if len(normalized) >= max_items:
                break
        return normalized

    @staticmethod
    def _normalize_level_path(values: Any, *, fallback_len: int) -> list[int]:
        if isinstance(values, list):
            normalized: list[int] = []
            for value in values:
                level = OnlineMmIngestService._normalize_heading_level(value)
                if level is None:
                    continue
                normalized.append(int(level))
                if len(normalized) >= max(1, int(fallback_len or 0)):
                    break
            return normalized
        if int(fallback_len or 0) <= 0:
            return []
        return list(range(1, int(fallback_len) + 1))

    @staticmethod
    def _canonicalize_section_path_levels(*, titles: list[str], levels: list[int]) -> list[int]:
        if not titles:
            return []
        canonical: list[int] = []
        for index, title in enumerate(list(titles or [])):
            explicit = levels[index] if index < len(levels) else None
            inferred = OnlineMmIngestService._infer_structural_heading_level(title)
            if inferred is None:
                next_level = int(explicit) if isinstance(explicit, int) and explicit > 0 else (canonical[-1] + 1 if canonical else 1)
            elif canonical and inferred <= canonical[-1]:
                next_level = canonical[-1] + 1
            else:
                next_level = inferred
            next_level = max(1, min(6, int(next_level)))
            canonical.append(next_level)
        return canonical

    def _apply_page_order_section_context(self, *, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered_blocks = sorted(
            list(blocks or []),
            key=lambda row: (
                int(row.get("page") or 0),
                int(row.get("order") or 0),
                str(row.get("block_id") or ""),
            ),
        )
        resolved_blocks: list[dict[str, Any]] = []
        active_titles: list[str] = []
        active_levels: list[int] = []

        for original_block in ordered_blocks:
            block = dict(original_block)
            path_titles = [str(item) for item in list(block.get("section_path_titles") or []) if str(item or "").strip()]
            path_levels = [int(item) for item in list(block.get("section_path_levels") or []) if isinstance(item, int) and int(item) > 0]

            if path_titles:
                if len(path_levels) < len(path_titles):
                    path_levels.extend(range(len(path_levels) + 1, len(path_titles) + 1))
                path_levels = self._canonicalize_section_path_levels(titles=path_titles, levels=path_levels)
                path_titles, path_levels = self._prepend_active_section_prefix(
                    path_titles=path_titles,
                    path_levels=path_levels,
                    active_titles=active_titles,
                    active_levels=active_levels,
                )
            elif self._should_inherit_active_section(block=block, active_titles=active_titles):
                path_titles = list(active_titles)
                path_levels = list(active_levels)

            if path_titles:
                path_levels = self._canonicalize_section_path_levels(titles=path_titles, levels=path_levels)
                block["section_path_titles"] = list(path_titles)
                block["section_path_levels"] = list(path_levels)
                block["heading_anchor"] = str(block.get("heading_anchor") or "").strip() or path_titles[-1]
                block["heading_level"] = int(path_levels[-1])
                if self._should_update_active_section(block=block):
                    active_titles = list(path_titles)
                    active_levels = list(path_levels)
            else:
                block["section_path_titles"] = []
                block["section_path_levels"] = []

            resolved_blocks.append(block)

        return resolved_blocks

    @staticmethod
    def _should_inherit_active_section(*, block: dict[str, Any], active_titles: list[str]) -> bool:
        if not active_titles:
            return False
        cleanup_action = str(block.get("cleanup_action") or "").strip()
        content_role = str(block.get("content_role") or "").strip()
        if cleanup_action in {"drop_from_body", "route_to_metadata"}:
            return False
        if content_role in _FRONT_MATTER_CONTENT_ROLES:
            return False
        return True

    @staticmethod
    def _should_update_active_section(*, block: dict[str, Any]) -> bool:
        cleanup_action = str(block.get("cleanup_action") or "").strip()
        content_role = str(block.get("content_role") or "").strip()
        if cleanup_action in {"drop_from_body", "route_to_metadata"}:
            return False
        if content_role in _FRONT_MATTER_CONTENT_ROLES:
            return False
        return bool(list(block.get("section_path_titles") or []))

    @staticmethod
    def _prepend_active_section_prefix(
        *,
        path_titles: list[str],
        path_levels: list[int],
        active_titles: list[str],
        active_levels: list[int],
    ) -> tuple[list[str], list[int]]:
        if not path_titles or not path_levels or not active_titles or not active_levels:
            return list(path_titles), list(path_levels)
        first_level = int(path_levels[0] or 1)
        if first_level <= 1:
            return list(path_titles), list(path_levels)

        prefix_pairs = [
            (str(title), int(level))
            for title, level in zip(active_titles, active_levels)
            if int(level) < first_level
        ]
        required_prefix_len = max(0, first_level - 1)
        if len(prefix_pairs) < required_prefix_len:
            return list(path_titles), list(path_levels)

        prefix_pairs = prefix_pairs[:required_prefix_len]
        prefix_titles = [title for title, _ in prefix_pairs]
        prefix_levels = [level for _, level in prefix_pairs]
        if path_titles[: len(prefix_titles)] == prefix_titles:
            return list(path_titles), list(path_levels)
        return prefix_titles + list(path_titles), prefix_levels + list(path_levels)

    @staticmethod
    def _infer_structural_heading_level(title: str) -> Optional[int]:
        value = str(title or "").strip()
        if not value:
            return None
        numbered = re.match(r"^(\d+(?:\.\d+){1,5})(?:[\s:：.\-]|$)", value)
        if numbered:
            return max(1, min(6, len(str(numbered.group(1) or "").split("."))))
        if _ENUM_HEADING_PATTERN.match(value):
            return 3
        if _LETTER_HEADING_PATTERN.match(value):
            return 2
        if _ROMAN_HEADING_PATTERN.match(value):
            return 1
        if re.match(r"^\d+(?:[\s:：.\-]|$)", value):
            return 1
        lowered = value.lower()
        if lowered in {"abstract", "references"} or lowered.startswith("appendix"):
            return 1
        return None

    @staticmethod
    def _detect_document_title(blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        explicit_titles = [
            block
            for block in list(blocks or [])
            if str(block.get("content_role") or "").strip() == "document_title"
            and int(block.get("page") or 0) == 1
        ]
        for block in explicit_titles:
            text = OnlineMmIngestService._block_display_text(block)
            if text and not OnlineMmIngestService._looks_like_noise_heading(text):
                return block
        page_one_headings = [
            block
            for block in list(blocks or [])
            if str(block.get("type") or "").strip().lower() == "heading"
            and int(block.get("page") or 0) == 1
            and int(block.get("order") or 0) <= 2
        ]
        for block in page_one_headings:
            text = OnlineMmIngestService._block_display_text(block)
            if len(text) >= 12 and len(text.split()) >= 3 and not OnlineMmIngestService._looks_like_noise_heading(text):
                return block
        return None

    @staticmethod
    def _looks_like_noise_heading(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return True
        lowered = value.lower()
        if "@" in lowered or "http://" in lowered or "https://" in lowered:
            return True
        if any(pattern.search(value) for pattern in _NOISE_HEADING_PATTERNS):
            return True
        return False

    def _build_section_spine(
        self,
        *,
        blocks: list[dict[str, Any]],
        order_index: dict[str, int],
        title_block_id: str,
    ) -> list[dict[str, Any]]:
        spine: list[dict[str, Any]] = []
        stack: list[dict[str, Any]] = []
        path_section_ids: dict[tuple[str, ...], str] = {}
        synthetic_abstract = self._build_synthetic_abstract_section(
            blocks=blocks,
            order_index=order_index,
            title_block_id=title_block_id,
        )
        if synthetic_abstract:
            spine.append(synthetic_abstract)
            stack.append(synthetic_abstract)
            path_section_ids[("Abstract",)] = str(synthetic_abstract.get("context_id") or "").strip()
        for block in list(blocks or []):
            block_id = str(block.get("block_id") or "").strip()
            path_titles = [str(item) for item in list(block.get("section_path_titles") or []) if str(item or "").strip()]
            path_levels = [int(item) for item in list(block.get("section_path_levels") or []) if isinstance(item, int) and int(item) > 0]
            if not block_id or not path_titles:
                continue
            if len(path_levels) < len(path_titles):
                path_levels.extend(range(len(path_levels) + 1, len(path_titles) + 1))
            path_levels = self._canonicalize_section_path_levels(titles=path_titles, levels=path_levels)
            for idx, title in enumerate(path_titles):
                prefix_titles = tuple(path_titles[: idx + 1])
                if prefix_titles in path_section_ids:
                    continue
                prefix_levels = path_levels[: idx + 1]
                parent_prefix = tuple(path_titles[:idx])
                parent_context_id = path_section_ids.get(parent_prefix) if parent_prefix else None
                context_id = f"sec_{block_id}_{idx + 1}"
                section = {
                    "context_id": context_id,
                    "parent_context_id": parent_context_id,
                    "anchor_block_id": block_id,
                    "anchor_position": int(order_index.get(block_id, -1)),
                    "page": int(block.get("page") or 0),
                    "section_title": title,
                    "section_type": self._infer_section_type(title),
                    "heading_level": int(prefix_levels[-1] if prefix_levels else idx + 1),
                    "zone": block.get("zone"),
                    "span": block.get("span"),
                    "path_context_ids": [path_section_ids[prefix_titles[:j + 1]] for j in range(len(prefix_titles) - 1)] + [context_id],
                    "path_titles": list(prefix_titles),
                    "path_types": [self._infer_section_type(item) for item in prefix_titles],
                    "path_levels": list(prefix_levels),
                }
                spine.append(section)
                path_section_ids[prefix_titles] = context_id
        for block in list(blocks or []):
            block_type = str(block.get("type") or "").strip().lower()
            content_role = str(block.get("content_role") or "").strip()
            if block_type != "heading" and content_role not in {"section_heading", "abstract_heading"}:
                continue
            block_id = str(block.get("block_id") or "").strip()
            if not block_id or block_id == str(title_block_id or "").strip():
                continue
            title = self._block_display_text(block)
            if not self._is_section_heading_candidate(title):
                continue
            heading_level = self._resolve_section_heading_level(
                title=title,
                explicit_level=block.get("heading_level"),
            )
            heading_level = self._adjust_contextual_heading_level(
                title=title,
                heading_level=heading_level,
                stack=stack,
            )
            while stack and int(stack[-1].get("heading_level") or 1) >= heading_level:
                stack.pop()
            parent_section = stack[-1] if stack else None
            context_id = f"sec_{block_id}"
            section_type = self._infer_section_type(title)
            path_context_ids = [str(item) for item in list((parent_section or {}).get("path_context_ids") or [])]
            path_titles = [str(item) for item in list((parent_section or {}).get("path_titles") or [])]
            path_types = [str(item) for item in list((parent_section or {}).get("path_types") or [])]
            path_levels = [int(item) for item in list((parent_section or {}).get("path_levels") or []) if isinstance(item, int)]
            path_context_ids.append(context_id)
            path_titles.append(title)
            path_types.append(section_type)
            path_levels.append(int(heading_level))
            section = {
                "context_id": context_id,
                "parent_context_id": str((parent_section or {}).get("context_id") or "").strip() or None,
                "anchor_block_id": block_id,
                "anchor_position": int(order_index.get(block_id, -1)),
                "page": int(block.get("page") or 0),
                "section_title": title,
                "section_type": section_type,
                "heading_level": int(heading_level),
                "zone": block.get("zone"),
                "span": block.get("span"),
                "path_context_ids": path_context_ids,
                "path_titles": path_titles,
                "path_types": path_types,
                "path_levels": path_levels,
            }
            spine.append(section)
            stack.append(section)
        return spine

    def _build_synthetic_abstract_section(
        self,
        *,
        blocks: list[dict[str, Any]],
        order_index: dict[str, int],
        title_block_id: str,
    ) -> Optional[dict[str, Any]]:
        explicit_abstract = any(
            str(block.get("content_role") or "").strip() == "abstract_heading"
            or (
                str(block.get("type") or "").strip().lower() == "heading"
                and self._infer_section_type(self._block_display_text(block)) == "abstract"
            )
            for block in list(blocks or [])
        )
        if explicit_abstract:
            return None
        for block in list(blocks or []):
            block_id = str(block.get("block_id") or "").strip()
            if not block_id or block_id == str(title_block_id or "").strip():
                continue
            title = self._block_display_text(block)
            content_role = str(block.get("content_role") or "").strip()
            block_type = str(block.get("type") or "").strip().lower()
            lowered = title.lower()
            if block_type == "heading" and self._is_section_heading_candidate(title):
                break
            if content_role == "abstract_body" or lowered.startswith("abstract"):
                context_id = f"sec_{block_id}_abstract"
                return {
                    "context_id": context_id,
                    "parent_context_id": None,
                    "anchor_block_id": block_id,
                    "anchor_position": int(order_index.get(block_id, -1)),
                    "page": int(block.get("page") or 0),
                    "section_title": "Abstract",
                    "section_type": "abstract",
                    "heading_level": 1,
                    "zone": block.get("zone"),
                    "span": block.get("span"),
                    "path_context_ids": [context_id],
                    "path_titles": ["Abstract"],
                    "path_types": ["abstract"],
                    "path_levels": [1],
                }
        return None

    def _resolve_parent_section(
        self,
        *,
        section_spine: list[dict[str, Any]],
        first_block_position: int,
    ) -> Optional[dict[str, Any]]:
        if first_block_position < 0:
            return None
        candidate: Optional[dict[str, Any]] = None
        for section in list(section_spine or []):
            raw_anchor_position = section.get("anchor_position")
            anchor_position = int(raw_anchor_position) if raw_anchor_position is not None else -1
            if anchor_position < 0 or anchor_position > first_block_position:
                break
            candidate = section
        return candidate

    @staticmethod
    def _is_section_heading_candidate(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        if OnlineMmIngestService._looks_like_noise_heading(value):
            return False
        if len(value) < 3 or len(value) > 180:
            return False
        if len(value.split()) > 18:
            return False
        return True

    @staticmethod
    def _infer_section_type(title: str) -> str:
        lowered = str(title or "").strip().lower()
        for token, section_type in _SECTION_TYPE_HINTS.items():
            if token in lowered:
                return section_type
        return "section"

    @staticmethod
    def _resolve_section_heading_level(*, title: str, explicit_level: Any) -> int:
        normalized = OnlineMmIngestService._normalize_heading_level(explicit_level)
        structural = OnlineMmIngestService._infer_structural_heading_level(title)
        if normalized is not None and structural is not None:
            return int(structural)
        if normalized is not None:
            return int(normalized)
        inferred = structural if structural is not None else OnlineMmIngestService._infer_numbered_heading_level(title)
        if inferred is not None:
            return int(inferred)
        return 1

    @staticmethod
    def _infer_numbered_heading_level(title: str) -> Optional[int]:
        value = str(title or "").strip()
        if not value:
            return None
        matched = re.match(r"^(\d+(?:\.\d+){0,5})(?:[\s:：.\-]|$)", value)
        if matched:
            return max(1, min(6, len(str(matched.group(1) or "").split("."))))
        return None

    @staticmethod
    def _adjust_contextual_heading_level(*, title: str, heading_level: int, stack: list[dict[str, Any]]) -> int:
        normalized_level = max(1, int(heading_level or 1))
        if normalized_level != 1 or not stack:
            return normalized_level
        lowered = str(title or "").strip().lower()
        if lowered == "references":
            return 1
        appendix_ancestor = next(
            (
                section
                for section in reversed(list(stack or []))
                if str(section.get("section_type") or "").strip() == "appendix"
            ),
            None,
        )
        if appendix_ancestor and OnlineMmIngestService._looks_like_appendix_subheading(title):
            return min(6, int(appendix_ancestor.get("heading_level") or 1) + 1)
        return normalized_level

    @staticmethod
    def _looks_like_appendix_subheading(title: str) -> bool:
        value = str(title or "").strip()
        if not value:
            return False
        lowered = value.lower()
        if "appendix" in lowered or lowered == "references":
            return False
        if OnlineMmIngestService._infer_numbered_heading_level(value) is not None:
            return False
        if re.match(r"^[A-Z](?:\.|\))\s", value):
            return False
        if value == value.upper() and len(value.split()) <= 6:
            return False
        if value.endswith(":"):
            return True
        return 1 <= len(value.split()) <= 6 and any(char.islower() for char in value)

    @staticmethod
    def _parse_json_object(raw_text: str) -> dict[str, Any]:
        text = str(raw_text or "").strip()
        if not text:
            return {}
        candidates = [text]
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                candidates.insert(0, "\n".join(lines[1:-1]).strip())
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        raise json.JSONDecodeError("invalid json object", text, 0)

    @staticmethod
    def _sum_usages(usages: list[dict[str, int]]) -> dict[str, int]:
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for usage in usages:
            total["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            total["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            total["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        return total

    @staticmethod
    def _openai_usage_dict(response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)


_online_mm_ingest_service: Optional[OnlineMmIngestService] = None
_online_mm_ingest_lock = Lock()


def get_online_mm_ingest_service() -> OnlineMmIngestService:
    global _online_mm_ingest_service
    if _online_mm_ingest_service is not None:
        return _online_mm_ingest_service
    with _online_mm_ingest_lock:
        if _online_mm_ingest_service is None:
            _online_mm_ingest_service = OnlineMmIngestService()
    return _online_mm_ingest_service
