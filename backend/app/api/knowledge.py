"""
知识库 API 路由 - 支持共享知识库访问（可选）
"""
import asyncio
import hashlib
import math
import os
import json
import re
import shutil
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Any, List, Optional, Set
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_, and_, tuple_
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger

from app.config import settings
from app.core.database import async_session_factory, get_db
from app.core.security import get_current_user, get_current_user_for_stream
from app.models.user import User
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk, DocumentStatus
from app.models.literature import Paper
from app.schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseResponse,
    KnowledgeBaseListResponse,
    DocumentResponse,
    DocumentListResponse,
    DocumentDetailResponse,
    DocumentUploadResponse,
    ChunkResponse,
    ChunkListResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    ProcessingStatus,
    DocumentUploadMode,
    DocumentExtractProfile,
    DocumentExtractGranularity,
)
from app.services.document_service import get_document_processor
from app.services.embedding_service import (
    MODEL_DIMENSIONS,
    get_embedding_service_for_model_and_dimension,
)
from app.services.hybrid_retrieval_service import fuse_rrf, merge_rows_by_score
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.query_rewrite_service import QueryVariant, get_query_rewrite_service
from app.services.reranker_service import get_reranker_service, RerankerService
from app.services.vector_search_tuning import apply_hnsw_ef_search, resolve_ef_search
from app.services.chinese_segmentation_service import segment_text_for_fts
from app.services.contextual_retrieval_service import (
    build_adjacent_lookup_keys,
    build_context_summary,
    build_reranker_input,
    compose_embedding_input,
    merge_adjacent_context,
    normalize_adjacent_window,
)
from app.services.document_status_guard_service import (
    build_timeout_error_message,
    is_stale_processing_status,
)
from app.services.embedding_dimension_policy_service import get_embedding_dimension_policy_service
from app.services.dimension_rebuild_service import get_dimension_rebuild_service
from app.services.chunk_quality_gate_service import get_chunk_quality_gate_service
from app.services.pdf_rag_ingest_service import get_pdf_rag_ingest_service
from app.services.smart_chunking_service import (
    SmartChunkingService,
    ChunkConfig,
    ChunkingStrategy,
    ChunkLevel,
    get_preset_config,
)
from app.services.smart_chunking import (
    estimate_tokens as estimate_chunk_tokens,
    generate_chunk_id,
)
from app.services.status_event_bus import (
    build_status_channel_for_user,
    iter_status_events,
    publish_status_event,
)

# 共享功能导入（可选，如果模块不存在则禁用共享功能）
try:
    from app.models.role import SharedResource, GroupMember, ResearchGroup, UserRole
    SHARING_ENABLED = True
except ImportError:
    SHARING_ENABLED = False
    logger.warning("共享模块未安装，知识库共享功能已禁用")

router = APIRouter()

# 文件上传目录
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

_ACTIVE_DOCUMENT_TASKS: Set[int] = set()
_ACTIVE_DOCUMENT_TASKS_LOCK: Optional[asyncio.Lock] = None
_DOCUMENT_TASK_HANDLES: dict[int, asyncio.Task] = {}
_DOCUMENT_TASK_CANCEL_REQUESTS: Set[int] = set()
_DOCUMENT_TASK_RUN_SEMAPHORE: Optional[asyncio.Semaphore] = None
_DOCUMENT_TASK_RUN_SEMAPHORE_LIMIT: Optional[int] = None
_STATUS_STREAM_SNAPSHOT_LIMIT = 50
_REFERENCE_SECTION_RE = re.compile(
    r"^(?:#+\s*)?(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:references?|bibliography|参考文献)\s*$",
    re.IGNORECASE,
)
_PDF_SOURCE_SMALL_FRAGMENT_BLOCK_TYPES = {"heading", "footnote"}


def _trim_optional_text(value: Any, *, limit: int) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _estimate_new_chunk_count_for_dimension_policy(
    *,
    text: str,
    token_count: int,
    config: ChunkConfig,
) -> int:
    normalized_text = str(text or "")
    normalized_tokens = max(1, int(token_count or 0))
    normalized_chars = max(1, len(normalized_text))

    if bool(config.use_token_based):
        stride_tokens = max(
            1,
            int(config.base_chunk_tokens or 0) - int(config.overlap_tokens or 0),
        )
        return max(1, int(math.ceil(normalized_tokens / stride_tokens)))

    stride_chars = max(
        1,
        int(config.base_chunk_size or 0) - int(config.chunk_overlap or 0),
    )
    return max(1, int(math.ceil(normalized_chars / stride_chars)))


def _build_reranker_documents(candidates: list[Any]) -> list[str]:
    documents: list[str] = []
    for candidate in list(candidates or []):
        row = getattr(candidate, "row", None)
        if row is None:
            documents.append("")
            continue
        documents.append(
            build_reranker_input(
                content=getattr(row, "content", "") or "",
                context_summary=getattr(row, "context_summary", None),
                document_name=getattr(row, "document_name", None),
                section_title=getattr(row, "section_title", None),
                section_type=getattr(row, "section_type", None),
                max_context_length=int(settings.reranker_context_max_chars or 220),
                max_content_length=int(settings.reranker_snippet_max_chars or 960),
            )
        )
    return documents


def _unique_nonempty_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for value in list(values or []):
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _is_reference_section_label(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip()
    return bool(_REFERENCE_SECTION_RE.match(first_line))


def _normalize_pdf_block_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _trim_text_range(text: str, start_char: int, end_char: int) -> tuple[int, int]:
    text_len = len(text)
    start = max(0, min(int(start_char or 0), text_len))
    end = max(start, min(int(end_char or 0), text_len))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _normalize_pdf_source_spans(source_spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_spans: list[dict[str, Any]] = []
    for span in list(source_spans or []):
        if not isinstance(span, dict):
            continue
        start_char = int(span.get("start_char") or 0)
        end_char = int(span.get("end_char") or 0)
        if end_char <= start_char:
            continue
        normalized_spans.append(
            {
                "start_char": start_char,
                "end_char": end_char,
                "block_id": str(span.get("block_id") or "").strip(),
                "block_type": _normalize_pdf_block_type(span.get("block_type")),
                "page_start": int(span.get("page_start") or 0),
                "page_end": int(span.get("page_end") or 0),
                "section_path": str(span.get("section_path") or "").strip(),
            }
        )
    normalized_spans.sort(key=lambda item: (item["start_char"], item["end_char"]))
    return normalized_spans


def _get_overlapping_pdf_source_spans(
    source_spans: list[dict[str, Any]],
    *,
    start_char: int,
    end_char: int,
) -> list[dict[str, Any]]:
    normalized_start = int(start_char or 0)
    normalized_end = int(end_char or 0)
    if normalized_end <= normalized_start:
        return []
    return [
        span
        for span in list(source_spans or [])
        if int(span.get("end_char") or 0) > normalized_start and int(span.get("start_char") or 0) < normalized_end
    ]


def _build_chunk_dict_from_range(
    *,
    base_chunk: dict[str, Any],
    text: str,
    start_char: int,
    end_char: int,
    postprocess_step: str,
) -> Optional[dict[str, Any]]:
    start, end = _trim_text_range(text, start_char, end_char)
    if end <= start:
        return None

    content = text[start:end]
    metadata = dict(base_chunk.get("metadata") or {})
    extra = dict(metadata.get("extra") or {})
    postprocess_steps = list(extra.get("postprocess_steps") or [])
    if postprocess_step not in postprocess_steps:
        postprocess_steps.append(postprocess_step)
    extra["postprocess_steps"] = postprocess_steps
    metadata["extra"] = extra
    metadata["position_ratio"] = round(start / max(len(text), 1), 4)
    metadata["token_count"] = estimate_chunk_tokens(content)

    return {
        "id": generate_chunk_id(content, start),
        "content": content,
        "start_char": start,
        "end_char": end,
        "metadata": metadata,
    }


def _merge_chunk_dicts(
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    text: str,
    postprocess_step: str,
) -> Optional[dict[str, Any]]:
    merged = _build_chunk_dict_from_range(
        base_chunk=left,
        text=text,
        start_char=min(int(left.get("start_char") or 0), int(right.get("start_char") or 0)),
        end_char=max(int(left.get("end_char") or 0), int(right.get("end_char") or 0)),
        postprocess_step=postprocess_step,
    )
    if merged is None:
        return None

    left_extra = dict((left.get("metadata") or {}).get("extra") or {})
    right_extra = dict((right.get("metadata") or {}).get("extra") or {})
    merged_extra = dict(left_extra)
    for key, value in right_extra.items():
        merged_extra.setdefault(key, value)
    postprocess_steps = list(merged_extra.get("postprocess_steps") or [])
    if postprocess_step not in postprocess_steps:
        postprocess_steps.append(postprocess_step)
    merged_extra["postprocess_steps"] = postprocess_steps
    merged["metadata"]["extra"] = merged_extra
    return merged


def _extract_pdf_source_section_keys(chunk: dict[str, Any]) -> set[str]:
    metadata = dict(chunk.get("metadata") or {})
    extra = dict(metadata.get("extra") or {})
    pdf_source = dict(extra.get("pdf_source") or {})
    section_paths = {
        str(item).strip()
        for item in list(pdf_source.get("section_paths") or [])
        if str(item or "").strip()
    }
    section_title = str(metadata.get("section_title") or "").strip()
    if section_title:
        section_paths.add(section_title)
    return section_paths


def _chunks_can_merge_structurally(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_keys = _extract_pdf_source_section_keys(left)
    right_keys = _extract_pdf_source_section_keys(right)
    if left_keys and right_keys and left_keys.isdisjoint(right_keys):
        return False
    gap = int(right.get("start_char") or 0) - int(left.get("end_char") or 0)
    return gap <= 64


def _merge_small_pdf_structural_fragments(
    chunks: list[dict[str, Any]],
    *,
    text: str,
    source_spans: list[dict[str, Any]],
    min_tokens: int,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    adjusted = sorted(list(chunks or []), key=lambda item: int(item.get("start_char") or 0))
    merges = 0
    idx = 0

    while idx < len(adjusted):
        chunk = adjusted[idx]
        metadata = dict(chunk.get("metadata") or {})
        token_count = int(metadata.get("token_count") or estimate_chunk_tokens(chunk.get("content", "")))
        extra = dict(metadata.get("extra") or {})
        pdf_source = dict(extra.get("pdf_source") or {})
        block_types = {
            _normalize_pdf_block_type(item)
            for item in list(pdf_source.get("block_types") or [])
            if _normalize_pdf_block_type(item)
        }

        if token_count >= max(1, int(min_tokens or 0)):
            idx += 1
            continue
        if not block_types or not block_types.issubset(_PDF_SOURCE_SMALL_FRAGMENT_BLOCK_TYPES):
            idx += 1
            continue

        candidate_indexes: list[int] = []
        if idx + 1 < len(adjusted):
            candidate_indexes.append(idx + 1)
        if idx - 1 >= 0:
            candidate_indexes.append(idx - 1)

        merged_chunk: Optional[dict[str, Any]] = None
        target_idx: Optional[int] = None
        for candidate_idx in candidate_indexes:
            left_idx, right_idx = sorted((idx, candidate_idx))
            left_chunk = adjusted[left_idx]
            right_chunk = adjusted[right_idx]
            if not _chunks_can_merge_structurally(left_chunk, right_chunk):
                continue
            preview = _merge_chunk_dicts(
                left=left_chunk,
                right=right_chunk,
                text=text,
                postprocess_step="merge_small_structural_fragment",
            )
            if preview is None:
                continue
            _enrich_chunks_with_pdf_source([preview], source_spans)
            _backfill_chunk_metadata_from_pdf_source([preview])
            preview_tokens = int((preview.get("metadata") or {}).get("token_count") or estimate_chunk_tokens(preview.get("content", "")))
            if preview_tokens > max(1, int(max_tokens or 0)):
                continue
            merged_chunk = preview
            target_idx = candidate_idx
            break

        if merged_chunk is None or target_idx is None:
            idx += 1
            continue

        left_idx, right_idx = sorted((idx, target_idx))
        adjusted[left_idx] = merged_chunk
        del adjusted[right_idx]
        merges += 1
        idx = max(0, left_idx - 1)

    return adjusted, merges


def _backfill_chunk_metadata_from_pdf_source(chunks: list[dict[str, Any]]) -> int:
    updated = 0
    for chunk in list(chunks or []):
        metadata = dict(chunk.get("metadata") or {})
        extra = dict(metadata.get("extra") or {})
        pdf_source = dict(extra.get("pdf_source") or {})
        block_types = {
            _normalize_pdf_block_type(item)
            for item in list(pdf_source.get("block_types") or [])
            if _normalize_pdf_block_type(item)
        }

        content_flags = dict(extra.get("content_flags") or {})
        if "table" in block_types:
            content_flags["has_table"] = True
        if "equation" in block_types:
            content_flags["has_equation"] = True
        if "caption" in block_types:
            content_flags["has_caption"] = True
        if "list_item" in block_types:
            content_flags["has_list"] = True
        extra["content_flags"] = content_flags

        if not str(metadata.get("section_title") or "").strip():
            for section_path in list(pdf_source.get("section_paths") or []):
                segments = [segment.strip() for segment in str(section_path or "").split(">") if segment.strip()]
                if not segments:
                    continue
                metadata["section_title"] = segments[-1]
                updated += 1
                break

        metadata["extra"] = extra
        chunk["metadata"] = metadata

    return updated


def _apply_pdf_source_structural_postprocess(
    chunks: list[dict[str, Any]],
    *,
    text: str,
    source_spans: list[dict[str, Any]],
    min_tokens: int,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized = sorted(list(chunks or []), key=lambda item: int(item.get("start_char") or 0))
    normalized_spans = _normalize_pdf_source_spans(source_spans)
    if not normalized or not normalized_spans:
        return normalized, {
            "total_input": int(len(normalized)),
            "total_output": int(len(normalized)),
            "split_count": 0,
            "merge_count": 0,
            "section_title_backfilled": 0,
        }

    _enrich_chunks_with_pdf_source(normalized, normalized_spans)

    merged_chunks, merge_count = _merge_small_pdf_structural_fragments(
        normalized,
        text=text,
        source_spans=normalized_spans,
        min_tokens=min_tokens,
        max_tokens=max_tokens,
    )
    _enrich_chunks_with_pdf_source(merged_chunks, normalized_spans)
    section_title_backfilled = _backfill_chunk_metadata_from_pdf_source(merged_chunks)

    return merged_chunks, {
        "total_input": int(len(normalized)),
        "total_output": int(len(merged_chunks)),
        "split_count": 0,
        "merge_count": int(merge_count),
        "section_title_backfilled": int(section_title_backfilled),
    }


def _enrich_chunks_with_pdf_source(
    chunks: list[dict[str, Any]],
    source_spans: list[dict[str, Any]],
) -> None:
    normalized_spans = _normalize_pdf_source_spans(source_spans)
    if not normalized_spans:
        return

    for chunk in list(chunks or []):
        if not isinstance(chunk, dict):
            continue
        chunk_start = int(chunk.get("start_char") or 0)
        chunk_end = int(chunk.get("end_char") or 0)
        if chunk_end <= chunk_start:
            continue

        matched = _get_overlapping_pdf_source_spans(
            normalized_spans,
            start_char=chunk_start,
            end_char=chunk_end,
        )
        if not matched:
            continue

        block_ids = _unique_nonempty_strings([span.get("block_id") for span in matched])
        block_types = _unique_nonempty_strings([span.get("block_type") for span in matched])
        section_paths = _unique_nonempty_strings([span.get("section_path") for span in matched])
        page_starts = [int(span.get("page_start") or 0) for span in matched if int(span.get("page_start") or 0) > 0]
        page_ends = [int(span.get("page_end") or 0) for span in matched if int(span.get("page_end") or 0) > 0]

        metadata = chunk.setdefault("metadata", {})
        extra = dict(metadata.get("extra") or {})
        extra["pdf_source"] = {
            "block_ids": block_ids,
            "block_types": block_types,
            "page_start": min(page_starts) if page_starts else None,
            "page_end": max(page_ends) if page_ends else None,
            "section_paths": section_paths,
        }
        metadata["extra"] = extra


def _is_reference_chunk(chunk: dict[str, Any]) -> bool:
    if not isinstance(chunk, dict):
        return False
    metadata = dict(chunk.get("metadata") or {})

    section_type = str(metadata.get("section_type") or "").strip().lower()
    if section_type == "references":
        return True

    if _is_reference_section_label(metadata.get("section_title")):
        return True

    extra = dict(metadata.get("extra") or {})
    pdf_source = dict(extra.get("pdf_source") or {})
    for section_path in list(pdf_source.get("section_paths") or []):
        for segment in str(section_path or "").split(">"):
            if _is_reference_section_label(segment):
                return True
    return False


def _filter_reference_chunks(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    filtered: list[dict[str, Any]] = []
    dropped = 0
    for chunk in list(chunks or []):
        if _is_reference_chunk(chunk):
            dropped += 1
            continue
        filtered.append(chunk)
    return filtered, dropped


def _sse_payload(event: str, data: Any) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


def _build_document_status_event_data(*, kb_id: int, doc: Document) -> dict[str, Any]:
    processing = _document_processing_snapshot(doc)
    return {
        "kb_id": int(kb_id),
        "document_id": int(doc.id),
        "status": str(doc.status),
        "processing_stage": processing["stage"],
        "processing_stage_label": processing["stage_label"],
        "processing_progress": processing["progress"],
        "processing_detail": processing["detail"],
        "chunk_count": int(doc.chunk_count or 0),
        "error_message": (doc.error_message or None),
        "updated_at": processing["updated_at"],
    }


async def _collect_status_stream_snapshot(
    db: AsyncSession,
    *,
    user_id: int,
    kb_id: Optional[int],
    limit: int = _STATUS_STREAM_SNAPSHOT_LIMIT,
) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit or _STATUS_STREAM_SNAPSHOT_LIMIT), 200))
    query = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(KnowledgeBase.user_id == int(user_id))
        .order_by(Document.created_at.desc())
        .limit(normalized_limit)
    )
    if kb_id is not None:
        query = query.where(Document.knowledge_base_id == int(kb_id))
    else:
        query = query.where(
            Document.status.in_(
                [
                    DocumentStatus.PENDING.value,
                    DocumentStatus.RUNNING.value,
                    DocumentStatus.TIMEOUT.value,
                    DocumentStatus.FAILED.value,
                    DocumentStatus.CANCELLED.value,
                ]
            )
        )

    result = await db.execute(query)
    docs = list(result.scalars().all())
    return [
        _build_document_status_event_data(kb_id=int(doc.knowledge_base_id), doc=doc)
        for doc in docs
    ]


async def _publish_document_status_event(
    *,
    user_id: int,
    kb_id: int,
    doc: Document,
) -> None:
    payload = {
        "event": "document_status",
        "data": _build_document_status_event_data(kb_id=int(kb_id), doc=doc),
    }
    try:
        await publish_status_event(build_status_channel_for_user(int(user_id)), payload)
    except Exception as exc:  # pragma: no cover - push failures should not break main path
        logger.warning(f"[Knowledge API] 发布文档状态事件失败 doc={doc.id}: {exc}")


def _get_active_document_tasks_lock() -> asyncio.Lock:
    global _ACTIVE_DOCUMENT_TASKS_LOCK
    if _ACTIVE_DOCUMENT_TASKS_LOCK is None:
        _ACTIVE_DOCUMENT_TASKS_LOCK = asyncio.Lock()
    return _ACTIVE_DOCUMENT_TASKS_LOCK


def _get_document_task_run_semaphore() -> asyncio.Semaphore:
    global _DOCUMENT_TASK_RUN_SEMAPHORE, _DOCUMENT_TASK_RUN_SEMAPHORE_LIMIT
    limit = max(1, int(getattr(settings, "knowledge_document_task_max_concurrency", 2) or 2))
    if _DOCUMENT_TASK_RUN_SEMAPHORE is None or _DOCUMENT_TASK_RUN_SEMAPHORE_LIMIT != limit:
        _DOCUMENT_TASK_RUN_SEMAPHORE = asyncio.Semaphore(limit)
        _DOCUMENT_TASK_RUN_SEMAPHORE_LIMIT = limit
    return _DOCUMENT_TASK_RUN_SEMAPHORE


async def _claim_document_task_slot(doc_id: int) -> bool:
    lock = _get_active_document_tasks_lock()
    async with lock:
        normalized_doc_id = int(doc_id)
        if normalized_doc_id in _ACTIVE_DOCUMENT_TASKS:
            return False
        _ACTIVE_DOCUMENT_TASKS.add(normalized_doc_id)
        return True


async def _release_document_task_slot(doc_id: int) -> None:
    lock = _get_active_document_tasks_lock()
    async with lock:
        _ACTIVE_DOCUMENT_TASKS.discard(int(doc_id))


def _is_document_task_active(doc_id: int) -> bool:
    return int(doc_id) in _ACTIVE_DOCUMENT_TASKS


def _is_document_task_scheduled(doc_id: int) -> bool:
    task = _DOCUMENT_TASK_HANDLES.get(int(doc_id))
    return task is not None and not task.done()


def _has_live_document_task(doc_id: int) -> bool:
    normalized_doc_id = int(doc_id)
    return _is_document_task_active(normalized_doc_id) or _is_document_task_scheduled(normalized_doc_id)


async def _mark_document_task_cancellation_requested(doc_id: int) -> None:
    lock = _get_active_document_tasks_lock()
    async with lock:
        _DOCUMENT_TASK_CANCEL_REQUESTS.add(int(doc_id))


async def _consume_document_task_cancellation_requested(doc_id: int) -> bool:
    lock = _get_active_document_tasks_lock()
    async with lock:
        normalized_doc_id = int(doc_id)
        if normalized_doc_id in _DOCUMENT_TASK_CANCEL_REQUESTS:
            _DOCUMENT_TASK_CANCEL_REQUESTS.discard(normalized_doc_id)
            return True
        return False


async def _finalize_document_task_handle(doc_id: int, task: asyncio.Task) -> None:
    lock = _get_active_document_tasks_lock()
    async with lock:
        normalized_doc_id = int(doc_id)
        current = _DOCUMENT_TASK_HANDLES.get(normalized_doc_id)
        if current is task:
            _DOCUMENT_TASK_HANDLES.pop(normalized_doc_id, None)
        _DOCUMENT_TASK_CANCEL_REQUESTS.discard(normalized_doc_id)


def _build_document_task_done_callback(doc_id: int):
    normalized_doc_id = int(doc_id)

    def _callback(task: asyncio.Task) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - defensive cleanup during shutdown
            current = _DOCUMENT_TASK_HANDLES.get(normalized_doc_id)
            if current is task:
                _DOCUMENT_TASK_HANDLES.pop(normalized_doc_id, None)
            _DOCUMENT_TASK_CANCEL_REQUESTS.discard(normalized_doc_id)
            return
        loop.create_task(_finalize_document_task_handle(normalized_doc_id, task))

    return _callback


async def _run_document_task_with_queue(doc_id: int, chunk_size: int, chunk_overlap: int) -> None:
    semaphore = _get_document_task_run_semaphore()
    queued_at = time.perf_counter()
    async with semaphore:
        wait_ms = (time.perf_counter() - queued_at) * 1000
        if wait_ms >= 10:
            logger.info(
                "[KnowledgeQueue] doc_id={} acquired processing slot after {}ms wait",
                int(doc_id),
                round(wait_ms, 2),
            )
        await process_document_task(
            doc_id=int(doc_id),
            chunk_size=int(chunk_size),
            chunk_overlap=int(chunk_overlap),
        )


async def _schedule_document_task(doc_id: int, chunk_size: int, chunk_overlap: int) -> bool:
    normalized_doc_id = int(doc_id)
    lock = _get_active_document_tasks_lock()
    async with lock:
        existing = _DOCUMENT_TASK_HANDLES.get(normalized_doc_id)
        if existing is not None:
            if not existing.done():
                return False
            _DOCUMENT_TASK_HANDLES.pop(normalized_doc_id, None)

        task = asyncio.create_task(
            _run_document_task_with_queue(
                doc_id=normalized_doc_id,
                chunk_size=int(chunk_size),
                chunk_overlap=int(chunk_overlap),
            )
        )
        _DOCUMENT_TASK_HANDLES[normalized_doc_id] = task
        task.add_done_callback(_build_document_task_done_callback(normalized_doc_id))
        return True


async def _cancel_document_task(doc_id: int, *, wait_timeout_seconds: float = 5.0) -> bool:
    normalized_doc_id = int(doc_id)
    task: Optional[asyncio.Task] = None

    lock = _get_active_document_tasks_lock()
    async with lock:
        existing = _DOCUMENT_TASK_HANDLES.get(normalized_doc_id)
        if existing is None:
            return False
        if existing.done():
            _DOCUMENT_TASK_HANDLES.pop(normalized_doc_id, None)
            return False
        _DOCUMENT_TASK_CANCEL_REQUESTS.add(normalized_doc_id)
        task = existing
        existing.cancel()

    try:
        await asyncio.wait_for(task, timeout=max(0.1, float(wait_timeout_seconds or 5.0)))
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning(f"[Knowledge API] 取消文档任务等待超时: doc_id={normalized_doc_id}")
    except Exception as exc:  # pragma: no cover - cancellation should not leak failure outward
        logger.debug(f"[Knowledge API] 文档任务取消完成时抛出异常 doc_id={normalized_doc_id}: {exc}")
    return True


def _build_error_detail(
    *,
    code: str,
    message: str,
    details: Optional[Any] = None,
    request_id: Optional[str] = None,
) -> dict:
    payload = {
        "code": str(code),
        "message": str(message),
        "details": details,
        "request_id": request_id,
    }
    return payload


def _compute_sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(bytes(content or b"")).hexdigest()


def _normalize_text_for_content_dedupe(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _document_dedupe_metadata(doc: Document) -> dict[str, Any]:
    metadata = dict(doc.metadata_ or {})
    dedupe = metadata.get("dedupe")
    if isinstance(dedupe, dict):
        return dict(dedupe)
    return {}


def _set_document_dedupe_metadata(doc: Document, **updates: Any) -> None:
    metadata = dict(doc.metadata_ or {})
    dedupe = dict(metadata.get("dedupe") or {})
    dedupe.update({key: value for key, value in updates.items() if value is not None})
    metadata["dedupe"] = dedupe
    doc.metadata_ = metadata


def _extract_documents_from_execute_result(result: Any) -> list[Document]:
    if result is None:
        return []
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        scalar_result = scalars()
        all_items = getattr(scalar_result, "all", None)
        if callable(all_items):
            return [item for item in all_items() if isinstance(item, Document)]
    rows_getter = getattr(result, "all", None)
    if callable(rows_getter):
        docs: list[Document] = []
        for row in rows_getter():
            if isinstance(row, Document):
                docs.append(row)
            elif isinstance(row, tuple) and row and isinstance(row[0], Document):
                docs.append(row[0])
        return docs
    return []


async def _find_duplicate_document_by_file_hash(
    db: AsyncSession,
    *,
    kb_id: int,
    file_size: int,
    file_sha256: str,
) -> Optional[Document]:
    result = await db.execute(
        select(Document).where(
            Document.knowledge_base_id == int(kb_id),
            Document.file_size == int(file_size),
            Document.status.in_(
                [
                    DocumentStatus.PENDING.value,
                    DocumentStatus.RUNNING.value,
                    DocumentStatus.COMPLETED.value,
                ]
            ),
        )
    )
    for candidate in _extract_documents_from_execute_result(result):
        dedupe = _document_dedupe_metadata(candidate)
        if str(dedupe.get("file_sha256") or "").strip() == str(file_sha256 or "").strip():
            return candidate
    return None


async def _find_duplicate_document_by_content_hash(
    db: AsyncSession,
    *,
    kb_id: int,
    content_hash: str,
    exclude_doc_id: int,
) -> Optional[Document]:
    result = await db.execute(
        select(Document).where(
            Document.knowledge_base_id == int(kb_id),
            Document.content_hash == str(content_hash or "").strip(),
            Document.id != int(exclude_doc_id),
            Document.status == DocumentStatus.COMPLETED.value,
        ).order_by(Document.id.asc())
    )
    candidates = _extract_documents_from_execute_result(result)
    if not candidates:
        return None
    for candidate in candidates:
        dedupe = _document_dedupe_metadata(candidate)
        if not dedupe.get("duplicate_of_document_id"):
            return candidate
    return candidates[0]


def _safe_remove_file(path: Optional[str], *, context: str) -> None:
    if not path:
        return
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning(f"[Knowledge API] 文件删除失败 context={context} path={path}: {exc}")


async def _document_file_has_other_references(db: AsyncSession, doc: Document) -> bool:
    file_path = str(doc.file_path or "").strip()
    if not file_path:
        return False

    other_doc_count = await db.scalar(
        select(func.count(Document.id)).where(
            Document.id != doc.id,
            Document.file_path == file_path,
        )
    )
    if int(other_doc_count or 0) > 0:
        return True

    linked_paper_count = await db.scalar(
        select(func.count(Paper.id)).where(Paper.pdf_path == file_path)
    )
    return int(linked_paper_count or 0) > 0


async def _recompute_kb_statistics(db: AsyncSession, kb_id: int) -> Optional[KnowledgeBase]:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        return None

    stats_row = (
        await db.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(Document.chunk_count), 0),
                func.coalesce(func.sum(Document.token_count), 0),
            ).where(Document.knowledge_base_id == kb_id)
        )
    ).one()

    kb.document_count = int(stats_row[0] or 0)
    kb.total_chunks = int(stats_row[1] or 0)
    kb.total_tokens = int(stats_row[2] or 0)
    return kb


_DOCUMENT_UPLOAD_MODES: Set[str] = {"local_fast", "local_hybrid", "online_mm", "auto"}
_DOCUMENT_EXTRACT_PROFILES: Set[str] = {"general", "academic_formula", "table_first"}
_DOCUMENT_EXTRACT_GRANULARITIES: Set[str] = {"fine", "medium", "coarse"}
_PROCESSING_STAGE_LABELS: dict[str, str] = {
    "queued": "排队中",
    "preparing": "准备任务",
    "online_mm_extract": "在线多模态提取中",
    "online_mm_finalize": "在线多模态整理中",
    "structured_ingest": "结构化提取中",
    "text_extract": "文本提取中",
    "chunking": "智能分块中",
    "quality_gate": "质量检查中",
    "embedding": "向量化中",
    "saving": "写入分片中",
    "finalizing": "入库收尾中",
    "completed": "处理完成",
    "failed": "处理失败",
    "timeout": "处理超时",
    "cancelled": "已取消",
}
_PROCESSING_STAGE_PROGRESS: dict[str, float] = {
    "queued": 0.0,
    "preparing": 5.0,
    "online_mm_extract": 15.0,
    "online_mm_finalize": 28.0,
    "structured_ingest": 25.0,
    "text_extract": 20.0,
    "chunking": 45.0,
    "quality_gate": 60.0,
    "embedding": 78.0,
    "saving": 92.0,
    "finalizing": 97.0,
    "completed": 100.0,
    "failed": 0.0,
    "timeout": 0.0,
    "cancelled": 0.0,
}
_STATUS_DEFAULT_MESSAGE: dict[str, str] = {
    DocumentStatus.PENDING.value: "等待处理",
    DocumentStatus.RUNNING.value: "处理中",
    DocumentStatus.COMPLETED.value: "处理完成",
    DocumentStatus.FAILED.value: "处理失败",
    DocumentStatus.TIMEOUT.value: "处理超时",
    DocumentStatus.CANCELLED.value: "处理已取消",
}
_STATUS_STAGE_FALLBACK: dict[str, str] = {
    DocumentStatus.PENDING.value: "queued",
    DocumentStatus.RUNNING.value: "preparing",
    DocumentStatus.COMPLETED.value: "completed",
    DocumentStatus.FAILED.value: "failed",
    DocumentStatus.TIMEOUT.value: "timeout",
    DocumentStatus.CANCELLED.value: "cancelled",
}


def _normalize_document_upload_mode(raw: Optional[str]) -> DocumentUploadMode:
    token = str(raw or settings.kb_online_mm_default_mode or "local_fast").strip().lower()
    if token not in _DOCUMENT_UPLOAD_MODES:
        raise HTTPException(status_code=400, detail=f"不支持的 ingest_mode: {token}")
    return token  # type: ignore[return-value]


def _normalize_document_extract_profile(raw: Optional[str]) -> DocumentExtractProfile:
    token = str(raw or "general").strip().lower()
    if token not in _DOCUMENT_EXTRACT_PROFILES:
        raise HTTPException(status_code=400, detail=f"不支持的 extract_profile: {token}")
    return token  # type: ignore[return-value]


def _normalize_document_extract_granularity(raw: Optional[str]) -> DocumentExtractGranularity:
    token = str(raw or "medium").strip().lower()
    if token not in _DOCUMENT_EXTRACT_GRANULARITIES:
        raise HTTPException(status_code=400, detail=f"不支持的 extract_granularity: {token}")
    return token  # type: ignore[return-value]


def _document_ingest_request(doc: Document) -> dict[str, Any]:
    metadata = dict(doc.metadata_ or {})
    request = metadata.get("ingest_request")
    if isinstance(request, dict):
        return dict(request)
    return {}


def _document_processing_mode(doc: Document) -> DocumentUploadMode:
    request = _document_ingest_request(doc)
    token = str(request.get("mode") or settings.kb_online_mm_default_mode or "local_fast").strip().lower()
    if token not in _DOCUMENT_UPLOAD_MODES:
        token = "local_fast"
    return token  # type: ignore[return-value]


def _document_extract_profile(doc: Document) -> DocumentExtractProfile:
    request = _document_ingest_request(doc)
    token = str(request.get("extract_profile") or "general").strip().lower()
    if token not in _DOCUMENT_EXTRACT_PROFILES:
        token = "general"
    return token  # type: ignore[return-value]


def _document_extract_granularity(doc: Document) -> DocumentExtractGranularity:
    request = _document_ingest_request(doc)
    token = str(request.get("extract_granularity") or "medium").strip().lower()
    if token not in _DOCUMENT_EXTRACT_GRANULARITIES:
        token = "medium"
    return token  # type: ignore[return-value]


def _resolve_pdf_rag_structured_mode(requested_ingest_mode: DocumentUploadMode) -> str:
    return "hybrid" if requested_ingest_mode == "local_hybrid" else "fast"


def _document_processing_snapshot(doc: Document) -> dict[str, Any]:
    metadata = dict(doc.metadata_ or {})
    raw_state = metadata.get("processing_state")
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    normalized_status = str(doc.status or DocumentStatus.PENDING.value).strip().lower() or DocumentStatus.PENDING.value

    stage = str(state.get("stage") or "").strip().lower()
    fallback_stage = _STATUS_STAGE_FALLBACK.get(normalized_status, "failed")
    if normalized_status in {
        DocumentStatus.COMPLETED.value,
        DocumentStatus.FAILED.value,
        DocumentStatus.TIMEOUT.value,
        DocumentStatus.CANCELLED.value,
    }:
        stage = fallback_stage
    elif not stage:
        stage = fallback_stage

    stage_label = (
        str(state.get("stage_label") or "").strip()
        or _PROCESSING_STAGE_LABELS.get(stage)
        or _STATUS_DEFAULT_MESSAGE.get(normalized_status, "处理中")
    )
    progress_value = state.get("progress")
    try:
        progress = float(progress_value)
    except (TypeError, ValueError):
        progress = _PROCESSING_STAGE_PROGRESS.get(stage, 0.0)
    progress = max(0.0, min(100.0, progress))

    detail = str(state.get("detail") or "").strip() or None
    updated_at = (
        str(state.get("updated_at") or "").strip()
        or (doc.updated_at or doc.created_at or datetime.utcnow()).isoformat()
    )
    current = state.get("current")
    total = state.get("total")
    return {
        "stage": stage,
        "stage_label": stage_label,
        "progress": progress,
        "detail": detail,
        "updated_at": updated_at,
        "current": int(current) if isinstance(current, (int, float)) else None,
        "total": int(total) if isinstance(total, (int, float)) else None,
    }


def _set_document_processing_stage(
    doc: Document,
    *,
    stage: str,
    detail: Optional[str] = None,
    progress: Optional[float] = None,
    current: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    metadata = dict(doc.metadata_ or {})
    state = dict(metadata.get("processing_state") or {})
    normalized_stage = str(stage or "").strip().lower() or "preparing"
    state["stage"] = normalized_stage
    state["stage_label"] = _PROCESSING_STAGE_LABELS.get(normalized_stage, normalized_stage)
    default_progress = _PROCESSING_STAGE_PROGRESS.get(normalized_stage, 0.0)
    state["progress"] = max(0.0, min(100.0, float(default_progress if progress is None else progress)))
    if detail is None:
        state.pop("detail", None)
    else:
        normalized_detail = str(detail).strip()
        if normalized_detail:
            state["detail"] = normalized_detail
        else:
            state.pop("detail", None)
    if current is None:
        state.pop("current", None)
    else:
        state["current"] = max(0, int(current))
    if total is None:
        state.pop("total", None)
    else:
        state["total"] = max(0, int(total))
    state["updated_at"] = datetime.utcnow().isoformat()
    metadata["processing_state"] = state
    doc.metadata_ = metadata


def _build_document_response(doc: Document) -> DocumentResponse:
    processing = _document_processing_snapshot(doc)
    return DocumentResponse(
        id=doc.id,
        knowledge_base_id=doc.knowledge_base_id,
        filename=doc.filename,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        file_type=doc.file_type,
        status=doc.status,
        processing_stage=processing["stage"],
        processing_stage_label=processing["stage_label"],
        processing_progress=processing["progress"],
        processing_detail=processing["detail"],
        processing_mode=_document_processing_mode(doc),
        extract_profile=_document_extract_profile(doc),
        extract_granularity=_document_extract_granularity(doc),
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        token_count=doc.token_count,
        char_count=doc.char_count,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        processed_at=doc.processed_at,
    )


def _document_has_resume_cache(doc: Document) -> bool:
    metadata = dict(doc.metadata_ or {})
    block_cache = dict(metadata.get("online_mm_block_cache") or {})
    window_cache = dict(metadata.get("online_mm_window_cache") or {})
    return bool(block_cache.get("blocks") or window_cache.get("windows"))


def _document_retry_metadata(doc: Document) -> dict[str, Any]:
    metadata = dict(doc.metadata_ or {})
    return dict(metadata.get("retry_request") or {})


def _document_retry_requested_recently(
    doc: Document,
    *,
    minimum_interval_seconds: int = 45,
) -> bool:
    retry_request = _document_retry_metadata(doc)
    raw_requested_at = str(retry_request.get("requested_at") or "").strip()
    if not raw_requested_at:
        return False
    try:
        requested_at = datetime.fromisoformat(raw_requested_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    requested_at_ts = requested_at.timestamp()
    now_ts = datetime.utcnow().timestamp()
    return (now_ts - requested_at_ts) < max(1, int(minimum_interval_seconds or 45))


def _mark_document_retry_requested(
    doc: Document,
    *,
    reason: str,
    trigger: str,
) -> None:
    metadata = dict(doc.metadata_ or {})
    retry_request = dict(metadata.get("retry_request") or {})
    retry_request["count"] = max(0, int(retry_request.get("count") or 0)) + 1
    retry_request["reason"] = str(reason or "manual_retry").strip() or "manual_retry"
    retry_request["trigger"] = str(trigger or "manual").strip() or "manual"
    retry_request["requested_at"] = datetime.utcnow().isoformat()
    metadata["retry_request"] = retry_request
    doc.metadata_ = metadata
    _set_document_processing_stage(doc, stage="queued")


def _mark_stale_document_timeout(doc: Document) -> bool:
    stale_timeout_seconds = max(
        int(getattr(settings, "document_processing_stale_timeout_seconds", 7200)),
        60,
    )
    last_updated_at = doc.updated_at or doc.created_at
    if not is_stale_processing_status(
        status=doc.status,
        last_updated_at=last_updated_at,
        timeout_seconds=stale_timeout_seconds,
    ):
        return False

    previous_error = (doc.error_message or "").strip()
    timeout_error = build_timeout_error_message(stale_timeout_seconds)
    doc.status = DocumentStatus.TIMEOUT.value
    doc.error_message = f"{previous_error} | {timeout_error}" if previous_error else timeout_error
    _set_document_processing_stage(doc, stage="timeout", detail=timeout_error)
    return True


# ========== 可用嵌入模型注册表 ==========

# 面向用户的模型描述信息
EMBEDDING_MODEL_CATALOG = [
    {
        "id": "mock/deterministic",
        "name": "Mock Deterministic (CI)",
        "dimension": 256,
        "provider": "mock",
        "description": "确定性哈希向量，用于 CI / smoke / 离线验证",
        "max_tokens": 16384,
    },
    # 本地模型 (sentence-transformers)
    {
        "id": "BAAI/bge-m3",
        "name": "BGE-M3 (推荐)",
        "dimension": 1024,
        "provider": "local",
        "description": "多语言SOTA, 支持中英文, 科研论文表现优秀",
        "max_tokens": 8192,
    },
    {
        "id": "BAAI/bge-large-zh-v1.5",
        "name": "BGE-Large-ZH",
        "dimension": 1024,
        "provider": "local",
        "description": "中文优化, 适合纯中文文档",
        "max_tokens": 512,
    },
    {
        "id": "BAAI/bge-large-en-v1.5",
        "name": "BGE-Large-EN",
        "dimension": 1024,
        "provider": "local",
        "description": "英文优化, 适合纯英文文档",
        "max_tokens": 512,
    },
    {
        "id": "allenai/specter2",
        "name": "SPECTER2 (科研专用)",
        "dimension": 768,
        "provider": "local",
        "description": "Allen AI 专为科研论文设计, 仅英文",
        "max_tokens": 512,
    },
    {
        "id": "BAAI/bge-base-zh-v1.5",
        "name": "BGE-Base-ZH",
        "dimension": 768,
        "provider": "local",
        "description": "中文轻量级, 速度更快",
        "max_tokens": 512,
    },
    {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "name": "MiniLM-L6 (轻量)",
        "dimension": 384,
        "provider": "local",
        "description": "超轻量英文模型, 适合快速原型",
        "max_tokens": 256,
    },
    # API 模型
    {
        "id": "text-embedding-v2",
        "name": "阿里云 text-embedding-v2",
        "dimension": 1536,
        "provider": "aliyun",
        "description": "阿里云 DashScope, 支持中英文",
        "max_tokens": 2048,
    },
    {
        "id": "text-embedding-3-small",
        "name": "OpenAI text-embedding-3-small",
        "dimension": 1536,
        "provider": "openai",
        "description": "OpenAI 嵌入模型, 英文表现优秀",
        "max_tokens": 8191,
    },
    {
        "id": "nomic-embed-text",
        "name": "Nomic Embed (Ollama)",
        "dimension": 768,
        "provider": "ollama",
        "description": "Ollama 本地 API, 开源可控",
        "max_tokens": 8192,
    },
]


# ========== 共享知识库辅助函数 ==========

async def get_shared_kb_ids(current_user: User, db: AsyncSession) -> Set[int]:
    """获取共享给当前用户的知识库ID集合"""
    if not SHARING_ENABLED:
        logger.debug("共享功能未启用")
        return set()

    logger.debug(f"获取用户 {current_user.id} 的共享知识库")

    # 获取用户加入的研究组
    group_ids_result = await db.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
    )
    group_ids = [row[0] for row in group_ids_result.fetchall()]
    logger.debug(f"用户加入的研究组: {group_ids}")
    
    # 如果是导师，获取管理的研究组
    if current_user.role == UserRole.MENTOR.value:
        mentor_groups_result = await db.execute(
            select(ResearchGroup.id).where(ResearchGroup.mentor_id == current_user.id)
        )
        mentor_group_ids = [row[0] for row in mentor_groups_result.fetchall()]
        group_ids = list(set(group_ids + mentor_group_ids))
        logger.debug(f"导师管理的研究组: {mentor_group_ids}")
    
    # 构建共享条件
    conditions = [
        and_(
            SharedResource.shared_with_type == 'user',
            SharedResource.shared_with_id == current_user.id
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
        logger.debug(f"用户的导师ID: {current_user.mentor_id}")
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
        logger.debug(f"研究组导师IDs: {mentor_ids}")
        if mentor_ids:
            conditions.append(
                and_(
                    SharedResource.shared_with_type == 'all_students',
                    SharedResource.owner_id.in_(mentor_ids)
                )
            )
    
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
    logger.info(f"用户 {current_user.id} 可访问的共享知识库: {result}")
    return result


# ========== 嵌入模型列表 ==========

@router.get("/embedding-models")
async def list_embedding_models(
    current_user: User = Depends(get_current_user),
):
    """
    获取可用的嵌入模型列表
    
    返回所有支持的嵌入模型及其配置信息，前端用于创建知识库时选择模型。
    会标注当前系统正在使用的模型。
    """
    from app.config import settings
    from app.models.knowledge import EMBEDDING_DIMENSION
    
    current_model = (
        settings.mock_embedding_model
        if settings.embedding_provider == "mock"
        else settings.local_embedding_model
        if settings.embedding_provider == "local"
        else settings.aliyun_embedding_model
        if settings.embedding_provider == "aliyun"
        else "text-embedding-3-small"
        if settings.embedding_provider == "openai"
        else "nomic-embed-text"
    )
    
    models = []
    for model in EMBEDDING_MODEL_CATALOG:
        models.append({
            **model,
            "is_current": model["id"] == current_model,
            "compatible": model["dimension"] == EMBEDDING_DIMENSION,
        })
    
    return {
        "models": models,
        "current_model": current_model,
        "current_provider": settings.embedding_provider,
        "current_dimension": EMBEDDING_DIMENSION,
    }


# ========== 知识库 CRUD ==========

@router.get("/knowledge-bases", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取知识库列表"""
    # 查询总数
    count_query = select(func.count(KnowledgeBase.id)).where(
        KnowledgeBase.user_id == current_user.id
    )
    total = (await db.execute(count_query)).scalar() or 0

    # 查询列表
    query = (
        select(KnowledgeBase)
        .where(KnowledgeBase.user_id == current_user.id)
        .order_by(KnowledgeBase.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    items = result.scalars().all()

    for item in items:
        await _recompute_kb_statistics(db, int(item.id))
    await db.commit()
    
    return KnowledgeBaseListResponse(
        items=[KnowledgeBaseResponse.model_validate(item) for item in items],
        total=total
    )


@router.get("/available")
async def get_available_knowledge_bases(
    include_shared: bool = Query(True, description="是否包含共享的知识库"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    获取用户可用的所有知识库（自己的 + 共享的）
    用于 AI 对话时选择知识库
    
    参数:
    - include_shared: 是否包含共享的知识库，默认 True
    """
    result = {
        "own": [],
        "shared": [],
        "sharing_enabled": SHARING_ENABLED,
    }
    
    # 1. 获取自己的知识库
    own_result = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.user_id == current_user.id)
        .order_by(KnowledgeBase.updated_at.desc())
    )
    own_kbs = own_result.scalars().all()
    
    for kb in own_kbs:
        result["own"].append({
            "id": kb.id,
            "name": kb.name,
            "description": kb.description,
            "document_count": kb.document_count,
            "total_chunks": kb.total_chunks,
        })
    
    # 2. 获取共享的知识库（可选）
    if include_shared and SHARING_ENABLED:
        shared_kb_ids = await get_shared_kb_ids(current_user, db)
        
        if shared_kb_ids:
            for kb_id in shared_kb_ids:
                kb = await db.get(KnowledgeBase, kb_id)
                if kb:
                    owner = await db.get(User, kb.user_id)
                    owner_name = owner.full_name or owner.username if owner else "未知"
                    
                    result["shared"].append({
                        "id": kb.id,
                        "name": kb.name,
                        "description": kb.description,
                        "document_count": kb.document_count,
                        "total_chunks": kb.total_chunks,
                        "owner_id": kb.user_id,
                        "owner_name": owner_name,
                    })
    
    return result


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建知识库"""
    # 根据选择的模型确定向量维度
    embedding_model = data.embedding_model
    embedding_dimension = MODEL_DIMENSIONS.get(embedding_model)
    if not embedding_dimension:
        # 未知模型，使用系统默认维度
        from app.models.knowledge import EMBEDDING_DIMENSION
        embedding_dimension = EMBEDDING_DIMENSION
        logger.warning(f"未知嵌入模型 {embedding_model}，使用系统默认维度 {embedding_dimension}")
    
    kb = KnowledgeBase(
        user_id=current_user.id,
        name=data.name,
        description=data.description,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        chunk_size=data.chunk_size,
        chunk_overlap=data.chunk_overlap,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    
    logger.info(f"用户 {current_user.id} 创建知识库: {kb.name}, 模型: {embedding_model}, 维度: {embedding_dimension}")
    return KnowledgeBaseResponse.model_validate(kb)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取知识库详情"""
    kb = await db.get(KnowledgeBase, kb_id)
    
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    await _recompute_kb_statistics(db, kb_id)
    await db.commit()
    await db.refresh(kb)
    
    return KnowledgeBaseResponse.model_validate(kb)


@router.put("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: int,
    data: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新知识库"""
    kb = await db.get(KnowledgeBase, kb_id)
    
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    
    # Handle chunking_config specially
    if "chunking_config" in update_data:
        config = update_data.pop("chunking_config")
        # Ensure metadata_ is a dict
        current_metadata = dict(kb.metadata_) if kb.metadata_ else {}
        current_metadata["chunking_config"] = config
        # Assign new dict to trigger SQLAlchemy update
        kb.metadata_ = current_metadata
    
    for key, value in update_data.items():
        setattr(kb, key, value)
    
    kb.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(kb)
    
    return KnowledgeBaseResponse.model_validate(kb)


@router.delete("/knowledge-bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除知识库"""
    kb = await db.get(KnowledgeBase, kb_id)
    
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 删除关联的文件
    query = select(Document).where(Document.knowledge_base_id == kb_id)
    result = await db.execute(query)
    documents = result.scalars().all()
    
    for doc in documents:
        preserve_file = await _document_file_has_other_references(db, doc)
        if doc.file_path and os.path.exists(doc.file_path) and not preserve_file:
            _safe_remove_file(doc.file_path, context=f"delete_kb:{kb_id}")
        elif preserve_file:
            logger.info(f"[Knowledge API] 删除知识库时保留共享文件: kb={kb_id}, doc={doc.id}, path={doc.file_path}")
    
    await db.delete(kb)
    await db.commit()
    
    logger.info(f"用户 {current_user.id} 删除知识库: {kb_id}")
    return {"message": "删除成功"}


# ========== 文档管理 ==========

@router.get("/knowledge-bases/{kb_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    kb_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档列表"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.user_id != current_user.id:
        shared_kb_ids: set[int] = set()
        if SHARING_ENABLED:
            shared_kb_ids = await get_shared_kb_ids(current_user, db)
        if int(kb_id) not in shared_kb_ids:
            raise HTTPException(status_code=404, detail="知识库不存在")

    search_token = str(search or "").strip()
    filters = [Document.knowledge_base_id == kb_id]
    if search_token:
        like_pattern = f"%{search_token}%"
        filters.append(
            or_(
                Document.original_filename.ilike(like_pattern),
                Document.filename.ilike(like_pattern),
            )
        )

    # 查询总数
    count_query = select(func.count(Document.id)).where(*filters)
    total = (await db.execute(count_query)).scalar() or 0
    
    # 查询列表
    query = (
        select(Document)
        .where(*filters)
        .order_by(Document.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    items = result.scalars().all()

    changed_docs = [doc for doc in items if _mark_stale_document_timeout(doc)]
    if changed_docs:
        await db.commit()
        for doc in changed_docs:
            await db.refresh(doc)
            await _publish_document_status_event(
                user_id=int(current_user.id),
                kb_id=int(kb_id),
                doc=doc,
            )
    
    return DocumentListResponse(
        items=[_build_document_response(item) for item in items],
        total=total
    )


@router.get("/events/stream")
async def stream_knowledge_status_events(
    request: Request,
    kb_id: Optional[int] = Query(default=None, ge=1, description="可选：仅订阅指定知识库"),
    current_user: User = Depends(get_current_user_for_stream),
):
    """知识库状态事件流（SSE）。"""
    if kb_id is not None:
        async with async_session_factory() as db:
            kb = await db.get(KnowledgeBase, int(kb_id))
            if not kb or kb.user_id != current_user.id:
                raise HTTPException(status_code=404, detail="知识库不存在")

    channel = build_status_channel_for_user(int(current_user.id))

    async def event_generator():
        yield _sse_payload(
            "connected",
            {
                "scope": "knowledge",
                "user_id": int(current_user.id),
                "kb_id": int(kb_id) if kb_id is not None else None,
                "ts": datetime.utcnow().isoformat(),
            },
        )
        async with async_session_factory() as db:
            snapshot_items = await _collect_status_stream_snapshot(
                db,
                user_id=int(current_user.id),
                kb_id=int(kb_id) if kb_id is not None else None,
            )
        for item in snapshot_items:
            if await request.is_disconnected():
                return
            yield _sse_payload("document_status", item)

        async for item in iter_status_events(channel):
            if await request.is_disconnected():
                break

            event = str(item.get("event") or "").strip()
            data = item.get("data")

            if event == "heartbeat":
                yield _sse_payload("heartbeat", data)
                continue

            if event != "document_status" or not isinstance(data, dict):
                continue

            event_kb_id = int(data.get("kb_id") or 0)
            if kb_id is not None and event_kb_id != int(kb_id):
                continue

            yield _sse_payload("document_status", data)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/knowledge-bases/{kb_id}/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    ingest_mode: str = Form(default="local_fast"),
    extract_profile: str = Form(default="general"),
    extract_granularity: str = Form(default="medium"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传文档"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 验证文件类型
    file_type = file.filename.split('.')[-1].lower() if '.' in file.filename else 'txt'
    allowed_types = ['txt', 'md', 'markdown', 'pdf', 'html', 'htm']
    
    if file_type not in allowed_types:
        raise HTTPException(
            status_code=400, 
            detail=f"不支持的文件类型: {file_type}，支持: {', '.join(allowed_types)}"
        )

    normalized_ingest_mode = _normalize_document_upload_mode(ingest_mode)
    normalized_extract_profile = _normalize_document_extract_profile(extract_profile)
    normalized_extract_granularity = _normalize_document_extract_granularity(extract_granularity)
    if normalized_ingest_mode == "online_mm":
        if file_type != "pdf":
            raise HTTPException(status_code=400, detail="online_mm 仅支持 PDF 文件")
        if not bool(settings.kb_online_mm_ingest_enabled):
            raise HTTPException(status_code=400, detail="在线多模态入库当前未启用")
    
    file_id = str(uuid.uuid4())
    try:
        content = await file.read()
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=_build_error_detail(
                code="file_save_failed",
                message="文件保存失败",
                details=str(e),
                request_id=file_id,
            ),
        )

    file_sha256 = _compute_sha256_bytes(content)
    duplicate_doc = await _find_duplicate_document_by_file_hash(
        db,
        kb_id=int(kb_id),
        file_size=len(content),
        file_sha256=file_sha256,
    )
    if duplicate_doc is not None:
        raise HTTPException(
            status_code=409,
            detail=_build_error_detail(
                code="duplicate_file_upload",
                message="同一知识库中已存在相同文件",
                details={
                    "duplicate_of_document_id": int(duplicate_doc.id),
                    "duplicate_status": str(duplicate_doc.status or ""),
                    "duplicate_filename": str(duplicate_doc.original_filename or duplicate_doc.filename or ""),
                },
                request_id=file_id,
            ),
        )

    # 保存文件
    file_name = f"{file_id}.{file_type}"
    file_path = os.path.join(UPLOAD_DIR, str(current_user.id), str(kb_id))
    os.makedirs(file_path, exist_ok=True)
    full_path = os.path.join(file_path, file_name)
    try:
        with open(full_path, 'wb') as f:
            f.write(content)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=_build_error_detail(
                code="file_save_failed",
                message="文件保存失败",
                details=str(e),
                request_id=file_id,
            ),
        )
    
    # 创建文档记录
    doc = Document(
        knowledge_base_id=kb_id,
        filename=file_name,
        original_filename=file.filename,
        file_path=full_path,
        file_size=len(content),
        file_type=file_type,
        mime_type=file.content_type,
        status=DocumentStatus.PENDING.value,
        metadata_={
            "ingest_request": {
                "mode": normalized_ingest_mode,
                "extract_profile": normalized_extract_profile,
                "extract_granularity": normalized_extract_granularity,
                "requested_by": int(current_user.id),
                "requested_at": datetime.utcnow().isoformat(),
            },
            "dedupe": {
                "file_sha256": file_sha256,
                "duplicate_type": None,
                "duplicate_of_document_id": None,
                "indexed": None,
            },
        },
    )
    _set_document_processing_stage(doc, stage="queued")
    db.add(doc)
    try:
        await db.flush()
        await _recompute_kb_statistics(db, kb_id)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        _safe_remove_file(full_path, context=f"create_doc:{kb_id}")
        raise HTTPException(
            status_code=500,
            detail=_build_error_detail(
                code="document_create_failed",
                message="文档记录写入失败",
                details=str(exc),
                request_id=file_id,
            ),
        )
    await db.refresh(doc)
    await _publish_document_status_event(
        user_id=int(current_user.id),
        kb_id=int(kb_id),
        doc=doc,
    )
    
    # 直接注册异步任务句柄，支持文档级取消与状态治理。
    await _schedule_document_task(doc.id, kb.chunk_size, kb.chunk_overlap)
    
    logger.info(f"用户 {current_user.id} 上传文档: {file.filename} -> {doc.id}")
    
    return DocumentUploadResponse(
        id=doc.id,
        filename=doc.filename,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        file_type=doc.file_type,
        status=doc.status,
        processing_stage=_document_processing_snapshot(doc)["stage"],
        processing_stage_label=_document_processing_snapshot(doc)["stage_label"],
        processing_progress=_document_processing_snapshot(doc)["progress"],
        processing_detail=_document_processing_snapshot(doc)["detail"],
        processing_mode=normalized_ingest_mode,
        extract_profile=normalized_extract_profile,
        extract_granularity=normalized_extract_granularity,
        message="文件上传成功，正在处理中..."
    )


async def process_document_task(doc_id: int, chunk_size: int, chunk_overlap: int):
    """后台处理文档任务"""
    from app.core.database import async_session_factory

    if not await _claim_document_task_slot(doc_id):
        logger.info(f"[Knowledge API] 跳过重复文档任务: doc_id={doc_id}")
        return

    async with async_session_factory() as db:
        task_trace_id = uuid.uuid4().hex[:8]
        task_started_at = time.perf_counter()

        def _task_elapsed_ms() -> float:
            return (time.perf_counter() - task_started_at) * 1000

        logger.info(
            f"[doc:{task_trace_id}] 任务开始: 文档={doc_id}, "
            f"chunk_size={chunk_size}, chunk_overlap={chunk_overlap}"
        )
        try:
            # 获取文档
            doc = await db.get(Document, doc_id)
            if not doc:
                logger.warning(f"[doc:{task_trace_id}] 文档不存在: 文档={doc_id}")
                return

            owner_user_id: Optional[int] = None
            owner_kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if owner_kb:
                owner_user_id = int(owner_kb.user_id)

            async def _emit_status() -> None:
                if owner_user_id is None:
                    return
                await _publish_document_status_event(
                    user_id=owner_user_id,
                    kb_id=int(doc.knowledge_base_id),
                    doc=doc,
                )

            async def _set_stage(
                stage: str,
                *,
                detail: Optional[str] = None,
                progress: Optional[float] = None,
                current: Optional[int] = None,
                total: Optional[int] = None,
            ) -> None:
                _set_document_processing_stage(
                    doc,
                    stage=stage,
                    detail=detail,
                    progress=progress,
                    current=current,
                    total=total,
                )
                await db.commit()
                await _emit_status()
            
            # 更新状态为处理中
            doc.status = DocumentStatus.RUNNING.value
            await _set_stage("preparing")
            
            # 创建处理器
            processor = get_document_processor(chunk_size, chunk_overlap)
            
            # 嵌入模型与维度在分块完成后按策略动态决策
            embedding_svc = None
            logger.info(
                f"[doc:{task_trace_id}] 文档开始处理，嵌入维度将按规模自适应, "
                f"elapsed={_task_elapsed_ms():.2f}ms"
            )

            # 获取知识库以读取分块配置
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if not kb:
                logger.error(f"知识库不存在: {doc.knowledge_base_id}")
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "知识库不存在"
                _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                await db.commit()
                await _emit_status()
                return

            kb_config = kb.chunking_config
            ingest_request = _document_ingest_request(doc)
            requested_ingest_mode = _document_processing_mode(doc)
            requested_extract_profile = _document_extract_profile(doc)
            requested_extract_granularity = _document_extract_granularity(doc)
            embedding_model = (kb.embedding_model or "").strip() or settings.local_embedding_model
            policy_service = get_embedding_dimension_policy_service()
            text = ""
            primary_chunks = []
            context_chunks = []
            pdf_source_spans: list[dict[str, Any]] = []
            normalized_text = ""
            normalized_text_hash = ""
            normalized_text_token_count = 0
            normalized_text_char_count = 0
            frozen_embedding_svc = None
            frozen_dimension_decision = None
            frozen_existing_chunks = 0
            frozen_estimated_new_chunks = 0

            if doc.file_type.lower() == "pdf" and requested_ingest_mode == "online_mm":
                if not bool(settings.kb_online_mm_ingest_enabled):
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = "在线多模态入库未启用"
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    await _emit_status()
                    return

                extract_started_at = time.perf_counter()
                await _set_stage(
                    "online_mm_extract",
                    detail=f"profile={requested_extract_profile}, granularity={requested_extract_granularity}",
                )
                logger.info(
                    f"[doc:{task_trace_id}] 开始在线多模态 PDF 入库链路: {doc_id}, "
                    f"profile={requested_extract_profile}, granularity={requested_extract_granularity}"
                )
                try:
                    from app.services.online_mm_ingest_service import get_online_mm_ingest_service
                except Exception as exc:
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = f"在线多模态入库服务不可用: {exc}"[:2000]
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    await _emit_status()
                    return

                online_mm_service = get_online_mm_ingest_service()
                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                cache_key = "online_mm_block_cache"
                window_cache_key = "online_mm_window_cache"
                cached_extract = dict(current_metadata.get(cache_key) or {})
                cached_window_extract = dict(current_metadata.get(window_cache_key) or {})
                cached_blocks = list(cached_extract.get("blocks") or [])
                cached_report = dict(cached_extract.get("report") or {})
                cached_windows = list(cached_window_extract.get("windows") or [])
                cache_matches = (
                    str(cached_extract.get("document_name") or "").strip() == str(doc.original_filename or doc.filename or "").strip()
                    and str(cached_extract.get("extract_profile") or "").strip() == str(requested_extract_profile or "").strip()
                    and str(cached_extract.get("extract_granularity") or "").strip() == str(requested_extract_granularity or "").strip()
                    and bool(cached_blocks)
                )
                window_cache_matches = (
                    str(cached_window_extract.get("document_name") or "").strip() == str(doc.original_filename or doc.filename or "").strip()
                    and str(cached_window_extract.get("extract_profile") or "").strip() == str(requested_extract_profile or "").strip()
                    and str(cached_window_extract.get("extract_granularity") or "").strip() == str(requested_extract_granularity or "").strip()
                    and bool(cached_windows)
                )
                supports_staged_online_mm = all(
                    hasattr(online_mm_service, attr)
                    for attr in ("extract_pdf_blocks", "finalize_blocks")
                )

                if not supports_staged_online_mm:
                    online_mm_result = await online_mm_service.ingest_pdf(
                        file_path=doc.file_path,
                        document_name=doc.original_filename or doc.filename or "",
                        extract_profile=requested_extract_profile,
                        extract_granularity=requested_extract_granularity,
                    )
                elif cache_matches:
                    logger.info(
                        f"[doc:{task_trace_id}] 复用在线多模态抽取缓存: blocks={len(cached_blocks)}, "
                        f"elapsed={_task_elapsed_ms():.2f}ms"
                    )
                    await _set_stage("online_mm_finalize", detail="复用抽取缓存，整理结果")
                    online_mm_result = await online_mm_service.finalize_blocks(
                        blocks=cached_blocks,
                        document_name=doc.original_filename or doc.filename or "",
                        extract_profile=requested_extract_profile,
                        extract_granularity=requested_extract_granularity,
                        extract_report=cached_report,
                    )
                else:
                    extract_result = await online_mm_service.extract_pdf_blocks(
                        file_path=doc.file_path,
                        document_name=doc.original_filename or doc.filename or "",
                        extract_profile=requested_extract_profile,
                        extract_granularity=requested_extract_granularity,
                        cached_windows=cached_windows if window_cache_matches else None,
                    )
                    current_metadata["online_mm_ingest"] = dict(extract_result.get("report") or {})
                    current_metadata["ingest_request"] = {
                        **ingest_request,
                        "mode": requested_ingest_mode,
                        "extract_profile": requested_extract_profile,
                        "extract_granularity": requested_extract_granularity,
                    }
                    extracted_window_cache = list(extract_result.get("window_cache") or [])
                    if extracted_window_cache:
                        current_metadata[window_cache_key] = {
                            "document_name": doc.original_filename or doc.filename or "",
                            "extract_profile": requested_extract_profile,
                            "extract_granularity": requested_extract_granularity,
                            "cached_at": datetime.utcnow().isoformat(),
                            "windows": extracted_window_cache,
                        }
                    else:
                        current_metadata.pop(window_cache_key, None)
                    if bool(extract_result.get("ok")):
                        current_metadata[cache_key] = {
                            "document_name": doc.original_filename or doc.filename or "",
                            "extract_profile": requested_extract_profile,
                            "extract_granularity": requested_extract_granularity,
                            "cached_at": datetime.utcnow().isoformat(),
                            "report": dict(extract_result.get("report") or {}),
                            "blocks": list(extract_result.get("blocks") or []),
                        }
                        current_metadata.pop(window_cache_key, None)
                        doc.metadata_ = current_metadata
                        await db.commit()
                        await _emit_status()
                        await _set_stage("online_mm_finalize", detail="在线多模态抽取完成，正在整理块")
                        online_mm_result = await online_mm_service.finalize_blocks(
                            blocks=list(extract_result.get("blocks") or []),
                            document_name=doc.original_filename or doc.filename or "",
                            extract_profile=requested_extract_profile,
                            extract_granularity=requested_extract_granularity,
                            extract_report=dict(extract_result.get("report") or {}),
                        )
                    else:
                        online_mm_result = {
                            "applied": False,
                            "failure_reason": str(extract_result.get("failure_reason") or "online_mm_ingest_failed"),
                            "document_text": "",
                            "chunks": [],
                            "context_chunks": [],
                            "report": dict(extract_result.get("report") or {}),
                        }
                text = str(online_mm_result.get("document_text") or "")
                current_metadata["online_mm_ingest"] = dict(online_mm_result.get("report") or {})
                current_metadata["ingest_request"] = {
                    **ingest_request,
                    "mode": requested_ingest_mode,
                    "extract_profile": requested_extract_profile,
                    "extract_granularity": requested_extract_granularity,
                }
                if bool(online_mm_result.get("applied")):
                    current_metadata.pop(cache_key, None)
                    current_metadata.pop(window_cache_key, None)
                doc.metadata_ = current_metadata
                if not bool(online_mm_result.get("applied")):
                    reason = str(online_mm_result.get("failure_reason") or "online_mm_ingest_failed").strip()
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = f"在线多模态入库失败: {reason}"[:2000]
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    await _emit_status()
                    return

                primary_chunks = list(online_mm_result.get("chunks") or [])
                context_chunks = list(online_mm_result.get("context_chunks") or [])
                logger.info(
                    f"[doc:{task_trace_id}] 在线多模态 PDF 链路完成: chars={len(text)}, "
                    f"chunks={len(primary_chunks)}, context_chunks={len(context_chunks)}, "
                    f"stage_ms={(time.perf_counter() - extract_started_at) * 1000:.2f}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )

            elif doc.file_type.lower() == "pdf" and bool(settings.pdf_rag_line_pipeline_enabled):
                extract_started_at = time.perf_counter()
                await _set_stage(
                    "structured_ingest",
                    detail=f"mode={_resolve_pdf_rag_structured_mode(requested_ingest_mode)}",
                )
                logger.info(f"[doc:{task_trace_id}] 开始本地 PDF 结构化提取链路: {doc_id}")
                pdf_rag_service = get_pdf_rag_ingest_service()
                pdf_result = await pdf_rag_service.ingest_pdf(
                    file_path=doc.file_path,
                    document_name=doc.original_filename or doc.filename or "",
                    mode=_resolve_pdf_rag_structured_mode(requested_ingest_mode),
                )
                text = str(pdf_result.get("document_text") or "")
                pdf_source_spans = list(pdf_result.get("document_source_spans") or [])
                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                current_metadata["pdf_rag_ingest"] = dict(pdf_result.get("report") or {})
                if pdf_result.get("extractor"):
                    current_metadata["pdf_extractor"] = pdf_result.get("extractor")
                doc.metadata_ = current_metadata
                if bool(pdf_result.get("applied")):
                    logger.info(
                        f"[doc:{task_trace_id}] 本地 PDF 结构化提取完成: chars={len(text)}, next=smart_chunking, "
                        f"stage_ms={(time.perf_counter() - extract_started_at) * 1000:.2f}, "
                        f"elapsed={_task_elapsed_ms():.2f}ms"
                    )
                else:
                    logger.warning(
                        f"[doc:{task_trace_id}] 本地 PDF 结构化提取失败，回退通用提取链路: "
                        f"reason={pdf_result.get('failure_reason')}, elapsed={_task_elapsed_ms():.2f}ms"
                    )

            if not text.strip() and not primary_chunks:
                extract_started_at = time.perf_counter()
                await _set_stage("text_extract")
                logger.info(f"[doc:{task_trace_id}] 开始提取文档文本: {doc_id}")
                text = await processor.extract_text(doc.file_path, doc.file_type)
                logger.info(
                    f"[doc:{task_trace_id}] 文本提取完成: chars={len(text)}, "
                    f"stage_ms={(time.perf_counter() - extract_started_at) * 1000:.2f}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )
            elif text.strip() and not primary_chunks:
                logger.info(
                    f"[doc:{task_trace_id}] 复用上游提取文本进入统一智能分块: chars={len(text)}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )

            if not text.strip():
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档内容为空"
                _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                await db.commit()
                await _emit_status()
                return

            if doc.file_type.lower() == "pdf" and processor.last_pdf_extractor:
                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                current_metadata["pdf_extractor"] = processor.last_pdf_extractor
                doc.metadata_ = current_metadata

            normalized_text = _normalize_text_for_content_dedupe(text)
            normalized_text_hash = processor.compute_hash(normalized_text)
            duplicate_content_doc = await _find_duplicate_document_by_content_hash(
                db,
                kb_id=int(doc.knowledge_base_id),
                content_hash=normalized_text_hash,
                exclude_doc_id=int(doc.id),
            )
            if duplicate_content_doc is not None:
                doc.content = None
                doc.content_hash = normalized_text_hash
                doc.char_count = 0
                doc.token_count = 0
                doc.chunk_count = 0
                doc.status = DocumentStatus.COMPLETED.value
                doc.error_message = None
                doc.processed_at = datetime.utcnow()
                _set_document_dedupe_metadata(
                    doc,
                    content_hash_normalized=normalized_text_hash,
                    duplicate_type="content_exact",
                    duplicate_of_document_id=int(duplicate_content_doc.id),
                    indexed=False,
                    duplicate_stage="post_extract",
                )
                _set_document_processing_stage(
                    doc,
                    stage="completed",
                    detail=f"与文档 #{duplicate_content_doc.id} 内容完全重复，已跳过分块与向量化",
                )
                await _recompute_kb_statistics(db, int(doc.knowledge_base_id))
                await db.commit()
                await _emit_status()
                logger.info(
                    f"[doc:{task_trace_id}] 检测到内容重复，跳过后续入库: "
                    f"doc={doc.id}, duplicate_of={duplicate_content_doc.id}, elapsed={_task_elapsed_ms():.2f}ms"
                )
                return

            _set_document_dedupe_metadata(
                doc,
                content_hash_normalized=normalized_text_hash,
                duplicate_type=None,
                duplicate_of_document_id=None,
            )
            normalized_text_char_count = len(text)
            normalized_text_token_count = processor.estimate_tokens(text)

            if not primary_chunks:
                # 分片
                chunk_started_at = time.perf_counter()
                await _set_stage(
                    "chunking",
                    detail=f"strategy={kb_config.get('strategy', 'hybrid')}",
                )
                logger.info(f"[doc:{task_trace_id}] 开始智能分块: {doc_id}")

                # 准备配置
                chunk_config = ChunkConfig(
                    strategy=ChunkingStrategy(kb_config.get("strategy", "hybrid")),
                    base_chunk_size=kb.chunk_size,
                    chunk_overlap=kb.chunk_overlap,
                    breakpoint_percentile=kb_config.get("breakpoint_percentile", 95.0),
                    semantic_threshold=kb_config.get("semantic_threshold", 0.75),
                    min_semantic_chunk=kb_config.get("min_semantic_chunk", 100),
                    max_semantic_chunk=kb_config.get("max_semantic_chunk", 1500),
                    enable_hierarchical=kb_config.get("enable_hierarchical", True),
                    detect_academic_structure=kb_config.get("detect_academic_structure", True),
                    preserve_citations=kb_config.get("preserve_citations", True),

                    # ===== V3 Token 计量 =====
                    use_token_based=kb_config.get("use_token_based", True),
                    base_chunk_tokens=kb_config.get("base_chunk_tokens", 128),
                    overlap_tokens=kb_config.get("overlap_tokens", 16),
                    min_semantic_tokens=kb_config.get("min_semantic_tokens", 32),
                    max_semantic_tokens=kb_config.get("max_semantic_tokens", 384),
                )

                if "hierarchy_levels" in kb_config:
                    chunk_config.hierarchy_levels = [ChunkLevel(l) for l in kb_config["hierarchy_levels"]]

                estimated_text_tokens = processor.estimate_tokens(text)
                frozen_existing_chunks = await policy_service.estimate_kb_paragraph_chunks(db, kb.id)
                frozen_estimated_new_chunks = _estimate_new_chunk_count_for_dimension_policy(
                    text=text,
                    token_count=estimated_text_tokens,
                    config=chunk_config,
                )
                frozen_dimension_decision = policy_service.decide_dimension(
                    corpus_chunks=int(frozen_existing_chunks) + int(frozen_estimated_new_chunks),
                    embedding_model=embedding_model,
                    previous_dimension=kb.embedding_dimension,
                )
                frozen_embedding_svc = get_embedding_service_for_model_and_dimension(
                    embedding_model,
                    frozen_dimension_decision.target_dimension,
                )
                logger.info(
                    f"[doc:{task_trace_id}] [dimension_policy:chunking] kb={kb.id}, doc={doc_id}, "
                    f"model={embedding_model}, existing_chunks={frozen_existing_chunks}, "
                    f"estimated_new_chunks={frozen_estimated_new_chunks}, "
                    f"projected_chunks={frozen_dimension_decision.corpus_chunks}, "
                    f"target_dim={frozen_dimension_decision.target_dimension}, "
                    f"reason={frozen_dimension_decision.reason}"
                )

                # 执行分块
                smart_service = SmartChunkingService(embedding_svc=frozen_embedding_svc)
                result = await smart_service.chunk_document(text, chunk_config, doc.file_type)
                logger.info(
                    f"[doc:{task_trace_id}] 智能分块完成: 层级分块={'是' if result.get('hierarchy') else '否'}, "
                    f"stage_ms={(time.perf_counter() - chunk_started_at) * 1000:.2f}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )

                # ===== [Fix 1] 收集分块 =====
                # 核心原则：paragraph 级作为检索单元（生成 embedding）
                #          section/document 级作为上下文参考（不生成 embedding，存入文档 metadata）
                if result.get("hierarchy"):
                    hierarchy = result["hierarchy"]
                    for level, level_chunks in hierarchy.items():
                        for chunk_data in level_chunks:
                            if not isinstance(chunk_data, dict):
                                chunk_data = (
                                    smart_service._chunk_to_dict(chunk_data)
                                    if hasattr(chunk_data, "metadata")
                                    else chunk_data
                                )

                            chunk_level = chunk_data.get("metadata", {}).get("level", "paragraph")
                            if chunk_level == "paragraph":
                                primary_chunks.append(chunk_data)
                            else:
                                context_chunks.append(chunk_data)
                else:
                    for val in result.get("chunks", []):
                        primary_chunks.append({
                            "id": val.id,
                            "content": val.content,
                            "start_char": val.start_char,
                            "end_char": val.end_char,
                            "metadata": {
                                "level": val.metadata.level.value if val.metadata.level else "paragraph",
                                "section_type": val.metadata.section_type,
                                "section_title": val.metadata.section_title,
                                "parent_id": val.metadata.parent_id,
                                "child_ids": val.metadata.child_ids,
                                "semantic_score": val.metadata.semantic_score,
                                "has_citations": val.metadata.has_citations,
                                "position_ratio": val.metadata.position_ratio,
                                "keywords": val.metadata.keywords,
                                "token_count": val.metadata.token_count,
                                "extra": val.metadata.extra,
                            }
                        })

            if not text.strip():
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档内容为空"
                _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                await db.commit()
                await _emit_status()
                return

            doc.content = text
            doc.content_hash = normalized_text_hash or processor.compute_hash(_normalize_text_for_content_dedupe(text))
            doc.char_count = normalized_text_char_count or len(text)
            doc.token_count = normalized_text_token_count or processor.estimate_tokens(text)
            
            chunks_to_save = primary_chunks
            
            if not chunks_to_save:
                # 降级：如果 primary_chunks 为空但 context_chunks 有内容，使用 context
                if context_chunks:
                    chunks_to_save = context_chunks
                    logger.warning(f"文档 {doc_id} 没有段落级分块，降级使用章节级")
            
            if not chunks_to_save:
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档分片失败：无有效分块"
                _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                await db.commit()
                await _emit_status()
                return
                
            # 按位置排序
            chunks_to_save.sort(key=lambda x: x["start_char"])

            # Chunk quality gate (optional): score + mark bad chunks + local repair.
            if bool(settings.chunk_quality_gate_enabled):
                gate_started_at = time.perf_counter()
                await _set_stage("quality_gate")
                gate_service = get_chunk_quality_gate_service()
                logger.info(
                    f"[doc:{task_trace_id}] 开始 chunk quality gate: "
                    f"input_chunks={len(chunks_to_save)}, "
                    f"max_checked={max(1, int(settings.chunk_quality_gate_max_chunks or 300))}, "
                    f"repair_enabled={bool(settings.chunk_repair_enabled)}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )
                gate_result = await gate_service.gate_chunks(
                    chunks_to_save,
                    document_name=doc.original_filename or doc.filename or "",
                )
                chunks_to_save = list(gate_result.get("chunks") or [])
                gate_report = dict(gate_result.get("report") or {})
                gate_report["stage_ms"] = (time.perf_counter() - gate_started_at) * 1000
                gate_report["failure_reason"] = gate_result.get("failure_reason")

                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                current_metadata["chunk_quality_gate"] = gate_report
                doc.metadata_ = current_metadata
                logger.info(
                    f"[doc:{task_trace_id}] chunk quality gate: "
                    f"input={gate_report.get('total_input', 0)}, "
                    f"output={gate_report.get('total_output', len(chunks_to_save))}, "
                    f"bad={gate_report.get('bad_count', 0)}, "
                    f"repaired={gate_report.get('repaired_count', 0)}, "
                    f"unrepaired_bad={gate_report.get('unrepaired_bad_count', 0)}, "
                    f"dropped_bad={gate_report.get('dropped_bad_count', 0)}, "
                    f"should_fail={bool(gate_result.get('should_fail_document'))}, "
                    f"elapsed={_task_elapsed_ms():.2f}ms"
                )

                if bool(gate_result.get("should_fail_document")):
                    reason = str(gate_result.get("failure_reason") or "chunk_quality_gate_failed")
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = f"chunk quality gate failed: {reason}"[:2000]
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    await _emit_status()
                    return

                if not chunks_to_save:
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = "chunk quality gate dropped all chunks"
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    await _emit_status()
                    return

            if pdf_source_spans:
                chunks_to_save, structural_report = _apply_pdf_source_structural_postprocess(
                    chunks_to_save,
                    text=text,
                    source_spans=pdf_source_spans,
                    min_tokens=int(chunk_config.min_semantic_tokens or 0),
                    max_tokens=int(chunk_config.max_semantic_tokens or 0),
                )
                _enrich_chunks_with_pdf_source(context_chunks, pdf_source_spans)
                if any(
                    int(structural_report.get(key) or 0) > 0
                    for key in ("split_count", "merge_count", "section_title_backfilled")
                ):
                    current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                    current_metadata["pdf_structural_postprocess"] = {
                        **structural_report,
                        "applied_at": datetime.utcnow().isoformat(),
                    }
                    doc.metadata_ = current_metadata
                    logger.info(
                        f"[doc:{task_trace_id}] PDF 结构后处理: "
                        f"input={structural_report.get('total_input', len(chunks_to_save))}, "
                        f"output={structural_report.get('total_output', len(chunks_to_save))}, "
                        f"splits={structural_report.get('split_count', 0)}, "
                        f"merges={structural_report.get('merge_count', 0)}, "
                        f"section_titles={structural_report.get('section_title_backfilled', 0)}"
                    )
                chunks_to_save.sort(key=lambda item: int(item.get("start_char") or 0))

            reference_primary_dropped = 0
            reference_context_dropped = 0
            chunks_to_save, reference_primary_dropped = _filter_reference_chunks(chunks_to_save)
            context_chunks, reference_context_dropped = _filter_reference_chunks(context_chunks)
            if reference_primary_dropped or reference_context_dropped:
                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                current_metadata["reference_filter"] = {
                    "primary_dropped": int(reference_primary_dropped),
                    "context_dropped": int(reference_context_dropped),
                    "applied_at": datetime.utcnow().isoformat(),
                }
                doc.metadata_ = current_metadata
                logger.info(
                    f"[doc:{task_trace_id}] 参考文献块已跳过入库: "
                    f"primary_dropped={reference_primary_dropped}, "
                    f"context_dropped={reference_context_dropped}, "
                    f"remaining_primary={len(chunks_to_save)}, remaining_context={len(context_chunks)}"
                )

            if not chunks_to_save:
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档分片失败：过滤参考文献后无有效主分块"
                _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                await db.commit()
                await _emit_status()
                return

            # 生成嵌入向量
            await _set_stage("embedding", current=0, total=len(chunks_to_save))
            logger.info(f"[doc:{task_trace_id}] 开始生成嵌入向量: {doc_id}, {len(chunks_to_save)} 个分片")
            if frozen_embedding_svc is not None and frozen_dimension_decision is not None:
                existing_chunks = int(frozen_existing_chunks)
                projected_chunks = int(existing_chunks) + len(chunks_to_save)
                decision = frozen_dimension_decision
                embedding_svc = frozen_embedding_svc
                logger.info(
                    f"[doc:{task_trace_id}] [dimension_policy:reuse] kb={kb.id}, doc={doc_id}, "
                    f"model={embedding_model}, existing_chunks={existing_chunks}, "
                    f"estimated_new_chunks={frozen_estimated_new_chunks}, actual_new_chunks={len(chunks_to_save)}, "
                    f"projected_estimate={decision.corpus_chunks}, projected_actual={projected_chunks}, "
                    f"target_dim={decision.target_dimension}, reason={decision.reason}"
                )
            else:
                existing_chunks = await policy_service.estimate_kb_paragraph_chunks(db, kb.id)
                projected_chunks = int(existing_chunks) + len(chunks_to_save)
                decision = policy_service.decide_dimension(
                    corpus_chunks=projected_chunks,
                    embedding_model=embedding_model,
                    previous_dimension=kb.embedding_dimension,
                )
                embedding_svc = get_embedding_service_for_model_and_dimension(
                    embedding_model,
                    decision.target_dimension,
                )
                logger.info(
                    f"[doc:{task_trace_id}] [dimension_policy] kb={kb.id}, doc={doc_id}, model={embedding_model}, "
                    f"existing_chunks={existing_chunks}, projected_chunks={projected_chunks}, "
                    f"prev_dim={kb.embedding_dimension}, target_dim={decision.target_dimension}, reason={decision.reason}"
                )

            chunk_context_summaries: list[str] = []
            embedding_inputs: list[str] = []
            for chunk_data in chunks_to_save:
                meta = chunk_data.get("metadata", {}) or {}
                context_summary = build_context_summary(
                    document_name=doc.original_filename,
                    chunk_level=meta.get("level", "paragraph"),
                    section_title=meta.get("section_title"),
                    section_type=meta.get("section_type"),
                    metadata=meta,
                )
                chunk_context_summaries.append(context_summary)
                embedding_inputs.append(
                    compose_embedding_input(
                        content=chunk_data["content"],
                        context_summary=context_summary,
                        chunk_level=meta.get("level", "paragraph"),
                    )
                )

            embedding_started_at = time.perf_counter()
            embeddings = await processor.embed_chunks(embedding_inputs, embedding_svc=embedding_svc)
            logger.info(
                f"[doc:{task_trace_id}] 嵌入向量完成: chunks={len(embedding_inputs)}, "
                f"dimension={decision.target_dimension}, "
                f"stage_ms={(time.perf_counter() - embedding_started_at) * 1000:.2f}, "
                f"elapsed={_task_elapsed_ms():.2f}ms"
            )
            
            # 创建分片记录
            await _set_stage("saving", current=0, total=len(chunks_to_save))
            smart_id_map = {} # str_id -> DocumentChunk
            
            for i, chunk_data in enumerate(chunks_to_save):
                meta = chunk_data["metadata"]
                extra_meta = dict(meta.get("extra") or {})
                section_type = _trim_optional_text(meta.get("section_type"), limit=50)
                section_title = _trim_optional_text(meta.get("section_title"), limit=500)
                chunk_embedding = embeddings[i] if i < len(embeddings) else None
                chunk_embedding_dimension = (
                    len(chunk_embedding) if chunk_embedding is not None else embedding_svc.get_dimension()
                )
                
                chunk = DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    content=chunk_data["content"],
                    content_segmented=segment_text_for_fts(chunk_data["content"]),
                    context_summary=chunk_context_summaries[i] if i < len(chunk_context_summaries) else None,
                    chunk_index=i,
                    start_char=chunk_data["start_char"],
                    end_char=chunk_data["end_char"],
                    embedding=chunk_embedding,
                    embedding_model=embedding_svc._get_model(),
                    embedding_dimension=chunk_embedding_dimension,
                    char_count=len(chunk_data["content"]),
                    token_count=processor.estimate_tokens(chunk_data["content"]),
                    
                    # 智能分块字段
                    chunk_level=meta.get("level", "paragraph"),
                    section_type=section_type,
                    section_title=section_title,
                    has_citations=meta.get("has_citations", False),
                    metadata_={
                        "position_ratio": meta.get("position_ratio"),
                        "keywords": meta.get("keywords"),
                        "original_id": chunk_data["id"],
                        **extra_meta,
                    },
                )
                db.add(chunk)
                smart_id_map[chunk_data["id"]] = chunk
            
            # [Fix 12 Correction] 同时也保存 context_chunks (section/document)，但不生成 embedding
            # 这样可以在 search 时通过 parent_id 回溯到父级 chunk
            for chunk_data in context_chunks:
                meta = chunk_data["metadata"]
                extra_meta = dict(meta.get("extra") or {})
                section_type = _trim_optional_text(meta.get("section_type"), limit=50)
                section_title = _trim_optional_text(meta.get("section_title"), limit=500)
                # 为 context chunk 分配 index，接在 primary 后面
                # 注意：这里 index 可能不连续，但对检索影响不大
                
                chunk = DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    content=chunk_data["content"],
                    content_segmented=segment_text_for_fts(chunk_data["content"]),
                    context_summary=build_context_summary(
                        document_name=doc.original_filename,
                        chunk_level=meta.get("level", "section"),
                        section_title=meta.get("section_title"),
                        section_type=meta.get("section_type"),
                        metadata=meta,
                    ),
                    chunk_index=-1, # context chunk index 设为 -1 或其他标记
                    start_char=chunk_data["start_char"],
                    end_char=chunk_data["end_char"],
                    embedding=None, # 不生成 embedding
                    embedding_model=embedding_svc._get_model(),
                    embedding_dimension=embedding_svc.get_dimension(),
                    char_count=len(chunk_data["content"]),
                    token_count=processor.estimate_tokens(chunk_data["content"]),
                    
                    # 智能分块字段
                    chunk_level=meta.get("level", "section"),
                    section_type=section_type,
                    section_title=section_title,
                    has_citations=meta.get("has_citations", False),
                    metadata_={
                        "position_ratio": meta.get("position_ratio"),
                        "keywords": meta.get("keywords"),
                        "original_id": chunk_data["id"],
                        **extra_meta,
                    },
                )
                db.add(chunk)
                smart_id_map[chunk_data["id"]] = chunk
            
            await db.flush() # Generate IDs
            
            # 更新父子关系
            await db.flush() # Generate IDs
            
            # 更新父子关系 (现在 context chunks 也在 smart_id_map 中了，可以链接)
            all_chunks = chunks_to_save + context_chunks
            for chunk_data in all_chunks:
                smart_id = chunk_data["id"]
                parent_smart_id = chunk_data["metadata"].get("parent_id")
                
                if parent_smart_id and parent_smart_id in smart_id_map and smart_id in smart_id_map:
                    child_db_chunk = smart_id_map[smart_id]
                    parent_db_chunk = smart_id_map[parent_smart_id]
                    child_db_chunk.parent_chunk_id = parent_db_chunk.id
            
            # [Fix 1] 将 section 级上下文存入文档 metadata，供检索时回溯使用
            if context_chunks:
                section_context = {}
                for ctx in context_chunks:
                    ctx_id = ctx.get("id", "")
                    section_context[ctx_id] = {
                        "content": ctx["content"][:500],  # 存摘要，避免过大
                        "section_type": _trim_optional_text(
                            ctx.get("metadata", {}).get("section_type"),
                            limit=50,
                        ),
                        "section_title": _trim_optional_text(
                            ctx.get("metadata", {}).get("section_title"),
                            limit=500,
                        ),
                        "start_char": ctx.get("start_char", 0),
                        "end_char": ctx.get("end_char", 0),
                    }
                doc_meta = dict(doc.metadata_) if doc.metadata_ else {}
                doc_meta["section_context"] = section_context
                doc.metadata_ = doc_meta
            
            # 更新文档状态
            doc.status = DocumentStatus.COMPLETED.value
            doc.error_message = None
            doc.chunk_count = len(chunks_to_save)
            doc.processed_at = datetime.utcnow()
            _set_document_dedupe_metadata(
                doc,
                content_hash_normalized=normalized_text_hash or doc.content_hash,
                duplicate_type=None,
                duplicate_of_document_id=None,
                indexed=True,
            )
            _set_document_processing_stage(doc, stage="finalizing", current=len(chunks_to_save), total=len(chunks_to_save))
            
            # 更新知识库统计
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            should_schedule_rebuild = False
            if kb:
                await _recompute_kb_statistics(db, int(doc.knowledge_base_id))
                kb.embedding_model = embedding_model
                kb.embedding_dimension = int(decision.target_dimension)
                kb_meta = dict(kb.metadata_ or {})
                policy_meta = dict(kb_meta.get("embedding_dimension_policy") or {})
                policy_meta.update(
                    {
                        "policy": settings.embedding_dimension_policy,
                        "reason": decision.reason,
                        "corpus_chunks": decision.corpus_chunks,
                        "target_dimension": int(decision.target_dimension),
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                )
                kb_meta["embedding_dimension_policy"] = policy_meta
                kb.metadata_ = kb_meta

                should_schedule_rebuild = bool(
                    settings.embedding_dim_rebuild_async
                    and decision.should_rebuild
                    and int(existing_chunks) > 0
                )

            await db.commit()
            _set_document_processing_stage(doc, stage="completed", current=len(chunks_to_save), total=len(chunks_to_save))
            await db.commit()
            await _emit_status()
            logger.info(
                f"[doc:{task_trace_id}] 数据落库完成: chunks_saved={len(chunks_to_save)}, "
                f"context_chunks={len(context_chunks)}, elapsed={_task_elapsed_ms():.2f}ms"
            )
            if should_schedule_rebuild:
                rebuild_service = get_dimension_rebuild_service()
                rebuild_start = await rebuild_service.schedule_kb_rebuild(
                    kb_id=kb.id,
                    target_dimension=int(decision.target_dimension),
                    trigger_reason=f"adaptive_policy_doc_{doc_id}",
                )
                logger.info(
                    f"[doc:{task_trace_id}] [dimension_rebuild] kb={kb.id}, target={decision.target_dimension}, "
                    f"scheduled={rebuild_start.scheduled}, reason={rebuild_start.reason}"
                )

            logger.info(
                f"[doc:{task_trace_id}] 文档处理完成: {doc_id}, {len(chunks_to_save)} 个分片, "
                f"total_ms={_task_elapsed_ms():.2f}"
            )
            
        except asyncio.CancelledError:
            logger.info(f"[doc:{task_trace_id}] 文档任务收到取消信号: {doc_id}")

            try:
                await db.rollback()
            except Exception as rollback_exc:
                logger.warning(f"[doc:{task_trace_id}] 文档取消回滚异常 {doc_id}: {rollback_exc}")

            explicit_cancel = await _consume_document_task_cancellation_requested(doc_id)
            if explicit_cancel:
                try:
                    doc = await db.get(Document, doc_id)
                    if doc:
                        kb_for_owner = await db.get(KnowledgeBase, doc.knowledge_base_id)
                        doc.status = DocumentStatus.CANCELLED.value
                        doc.error_message = "文档处理已取消"
                        doc.processed_at = None
                        _set_document_processing_stage(doc, stage="cancelled", detail="用户取消任务")
                        await db.commit()
                        if kb_for_owner:
                            await _publish_document_status_event(
                                user_id=int(kb_for_owner.user_id),
                                kb_id=int(doc.knowledge_base_id),
                                doc=doc,
                            )
                except Exception as persist_exc:
                    logger.error(f"[doc:{task_trace_id}] 文档取消状态写回异常 {doc_id}: {persist_exc}")
            else:
                logger.info(f"[doc:{task_trace_id}] 文档任务被中断，保留当前状态以便后续恢复: {doc_id}")
            return
        except Exception as e:
            logger.exception(f"[doc:{task_trace_id}] 处理文档失败 {doc_id}: {e}")

            # 先回滚失败事务，否则后续状态更新会被隐式拒绝
            try:
                await db.rollback()
            except Exception as rollback_exc:
                logger.warning(f"[doc:{task_trace_id}] 文档失败回滚异常 {doc_id}: {rollback_exc}")

            try:
                doc = await db.get(Document, doc_id)
                if doc:
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = str(e)[:2000]
                    _set_document_processing_stage(doc, stage="failed", detail=doc.error_message)
                    await db.commit()
                    kb_for_owner = await db.get(KnowledgeBase, doc.knowledge_base_id)
                    if kb_for_owner:
                        await _publish_document_status_event(
                            user_id=int(kb_for_owner.user_id),
                            kb_id=int(doc.knowledge_base_id),
                            doc=doc,
                        )
            except Exception as persist_exc:
                logger.error(f"[doc:{task_trace_id}] 文档失败状态写回异常 {doc_id}: {persist_exc}")
        finally:
            await _release_document_task_slot(doc_id)


async def resume_interrupted_document_tasks_on_startup() -> dict[str, Any]:
    if not bool(getattr(settings, "knowledge_resume_running_documents_on_startup", True)):
        return {"enabled": False, "scheduled": 0, "marked_failed": 0, "documents": []}

    startup_limit = max(
        1,
        int(getattr(settings, "knowledge_resume_running_documents_limit", 20) or 20),
    )
    scheduled: list[tuple[int, int, int]] = []
    marked_failed = 0
    recovered_doc_ids: list[int] = []

    async with async_session_factory() as db:
        result = await db.execute(
            select(Document, KnowledgeBase)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(Document.status == DocumentStatus.RUNNING.value)
            .order_by(Document.updated_at.asc().nullslast(), Document.id.asc())
            .limit(startup_limit)
        )
        rows = list(result.all())
        for doc, kb in rows:
            if _is_document_task_active(int(doc.id)):
                continue
            if not doc.file_path or not os.path.exists(doc.file_path):
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "处理在服务重启后无法恢复：源文件不存在"
                marked_failed += 1
                continue
            resume_reason = "startup_resume_from_cache" if _document_has_resume_cache(doc) else "startup_resume_restart"
            _mark_document_retry_requested(doc, reason=resume_reason, trigger="startup")
            doc.status = DocumentStatus.PENDING.value
            doc.error_message = None
            scheduled.append((int(doc.id), int(kb.chunk_size or 500), int(kb.chunk_overlap or 50)))
            recovered_doc_ids.append(int(doc.id))

        if rows:
            await db.commit()

    for doc_id, chunk_size, chunk_overlap in scheduled:
        await _schedule_document_task(doc_id=doc_id, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    return {
        "enabled": True,
        "scheduled": len(scheduled),
        "marked_failed": int(marked_failed),
        "documents": recovered_doc_ids,
    }


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}", response_model=DocumentDetailResponse)
async def get_document(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档详情"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    processing = _document_processing_snapshot(doc)
    return DocumentDetailResponse(
        id=doc.id,
        knowledge_base_id=doc.knowledge_base_id,
        filename=doc.filename,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        file_type=doc.file_type,
        status=doc.status,
        processing_stage=processing["stage"],
        processing_stage_label=processing["stage_label"],
        processing_progress=processing["progress"],
        processing_detail=processing["detail"],
        processing_mode=_document_processing_mode(doc),
        extract_profile=_document_extract_profile(doc),
        extract_granularity=_document_extract_granularity(doc),
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        token_count=doc.token_count,
        char_count=doc.char_count,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        processed_at=doc.processed_at,
        content=doc.content[:5000] if doc.content else None,  # 限制内容长度
        metadata=doc.metadata_ or {},
    )


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除文档"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    if _has_live_document_task(int(doc.id)):
        await _cancel_document_task(int(doc.id))
        if _has_live_document_task(int(doc.id)):
            raise HTTPException(status_code=409, detail="文档任务仍在取消中，请稍后重试删除")
    
    # 删除文件
    preserve_file = await _document_file_has_other_references(db, doc)
    if doc.file_path and os.path.exists(doc.file_path) and not preserve_file:
        _safe_remove_file(doc.file_path, context=f"delete_doc:{doc_id}")
    elif preserve_file:
        logger.info(f"[Knowledge API] 保留共享文件: doc={doc_id}, path={doc.file_path}")
    
    await db.delete(doc)
    await db.flush()
    await _recompute_kb_statistics(db, kb_id)
    await db.commit()
    
    logger.info(f"用户 {current_user.id} 删除文档: {doc_id}")
    return {"message": "删除成功"}


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/status", response_model=ProcessingStatus)
async def get_document_status(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档处理状态"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    # 计算进度
    
    if _mark_stale_document_timeout(doc):
        await db.commit()
        await db.refresh(doc)
        await _publish_document_status_event(
            user_id=int(current_user.id),
            kb_id=int(kb_id),
            doc=doc,
        )
        logger.warning(
            "文档状态因处理超时自动失败: "
            f"doc_id={doc.id}, kb_id={kb_id}, last_updated_at={doc.updated_at or doc.created_at}, "
            f"timeout_seconds={max(int(getattr(settings, 'document_processing_stale_timeout_seconds', 7200)), 60)}"
        )

    if (
        doc.status in {DocumentStatus.RUNNING.value, DocumentStatus.PENDING.value}
        and not _has_live_document_task(int(doc.id))
        and bool(doc.file_path)
        and os.path.exists(str(doc.file_path))
        and not _document_retry_requested_recently(doc, minimum_interval_seconds=45)
    ):
        resume_from_cache = _document_has_resume_cache(doc)
        if doc.status == DocumentStatus.RUNNING.value:
            reason = "status_poll_resume_from_cache" if resume_from_cache else "status_poll_resume_restart"
        else:
            reason = "status_poll_queue_resume_from_cache" if resume_from_cache else "status_poll_queue_resume_restart"
        _mark_document_retry_requested(
            doc,
            reason=reason,
            trigger="status_poll",
        )
        doc.status = DocumentStatus.PENDING.value
        doc.error_message = None
        await db.commit()
        await db.refresh(doc)
        await _publish_document_status_event(
            user_id=int(current_user.id),
            kb_id=int(kb_id),
            doc=doc,
        )
        await _schedule_document_task(
            doc_id=int(doc.id),
            chunk_size=int(kb.chunk_size or 500),
            chunk_overlap=int(kb.chunk_overlap or 50),
        )
        logger.info(
            "[KnowledgeResume] status poll resumed interrupted document: doc_id={}, kb_id={}, reason={}",
            int(doc.id),
            int(kb_id),
            reason,
        )

    processing = _document_processing_snapshot(doc)
    message = str(processing["stage_label"] or _STATUS_DEFAULT_MESSAGE.get(str(doc.status), "处理中"))
    if doc.status in {
        DocumentStatus.FAILED.value,
        DocumentStatus.TIMEOUT.value,
        DocumentStatus.CANCELLED.value,
    } and doc.error_message:
        message = _STATUS_DEFAULT_MESSAGE.get(str(doc.status), message)

    return ProcessingStatus(
        document_id=doc.id,
        status=doc.status,
        progress=float(processing["progress"]),
        message=message,
        processing_stage=processing["stage"],
        processing_stage_label=processing["stage_label"],
        processing_detail=processing["detail"],
        chunk_count=doc.chunk_count or 0,
        error=doc.error_message,
    )


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/retry", response_model=ProcessingStatus)
async def retry_document_processing(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重试/恢复文档处理任务"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=400, detail="源文件不存在，无法重试")

    if doc.status == DocumentStatus.RUNNING.value and _has_live_document_task(int(doc.id)):
        processing = _document_processing_snapshot(doc)
        return ProcessingStatus(
            document_id=doc.id,
            status=doc.status,
            progress=float(processing["progress"]),
            message=str(processing["stage_label"] or "文档已在处理中"),
            processing_stage=processing["stage"],
            processing_stage_label=processing["stage_label"],
            processing_detail=processing["detail"],
            chunk_count=doc.chunk_count or 0,
            error=doc.error_message,
        )

    _mark_document_retry_requested(
        doc,
        reason="manual_retry_from_cache" if _document_has_resume_cache(doc) else "manual_retry_restart",
        trigger="manual",
    )
    doc.status = DocumentStatus.PENDING.value
    doc.error_message = None
    doc.processed_at = None
    await db.commit()
    await _publish_document_status_event(
        user_id=int(current_user.id),
        kb_id=int(kb_id),
        doc=doc,
    )

    await _schedule_document_task(doc.id, kb.chunk_size, kb.chunk_overlap)

    return ProcessingStatus(
        document_id=doc.id,
        status=doc.status,
        progress=0,
        message="已加入重试队列",
        processing_stage="queued",
        processing_stage_label=_PROCESSING_STAGE_LABELS["queued"],
        processing_detail=None,
        chunk_count=doc.chunk_count or 0,
        error=doc.error_message,
    )


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/cancel", response_model=ProcessingStatus)
async def cancel_document_processing(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消单个文档处理任务，不影响 backend 其他请求。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    if doc.status in {
        DocumentStatus.COMPLETED.value,
        DocumentStatus.FAILED.value,
        DocumentStatus.TIMEOUT.value,
        DocumentStatus.CANCELLED.value,
    }:
        processing = _document_processing_snapshot(doc)
        return ProcessingStatus(
            document_id=doc.id,
            status=doc.status,
            progress=float(processing["progress"]),
            message=str(processing["stage_label"] or _STATUS_DEFAULT_MESSAGE.get(str(doc.status), "处理中")),
            processing_stage=processing["stage"],
            processing_stage_label=processing["stage_label"],
            processing_detail=processing["detail"],
            chunk_count=doc.chunk_count or 0,
            error=doc.error_message,
        )

    await _cancel_document_task(int(doc.id))
    await db.refresh(doc)

    if doc.status != DocumentStatus.CANCELLED.value:
        doc.status = DocumentStatus.CANCELLED.value
        doc.error_message = "文档处理已取消"
        doc.processed_at = None
        _set_document_processing_stage(doc, stage="cancelled", detail="用户取消任务")
        await db.commit()
        await _publish_document_status_event(
            user_id=int(current_user.id),
            kb_id=int(kb_id),
            doc=doc,
        )
    else:
        await _publish_document_status_event(
            user_id=int(current_user.id),
            kb_id=int(kb_id),
            doc=doc,
        )

    processing = _document_processing_snapshot(doc)
    return ProcessingStatus(
        document_id=doc.id,
        status=doc.status,
        progress=float(processing["progress"]),
        message=str(processing["stage_label"] or _STATUS_DEFAULT_MESSAGE.get(str(doc.status), "处理中")),
        processing_stage=processing["stage"],
        processing_stage_label=processing["stage_label"],
        processing_detail=processing["detail"],
        chunk_count=doc.chunk_count or 0,
        error=doc.error_message,
    )


# ========== 分片管理 ==========

@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    kb_id: int,
    doc_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档分片列表"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    doc = await db.get(Document, doc_id)
    if not doc or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    # 查询总数
    count_query = select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc_id)
    total = (await db.execute(count_query)).scalar() or 0
    
    # 查询列表
    query = (
        select(DocumentChunk)
        .where(DocumentChunk.document_id == doc_id)
        .order_by(DocumentChunk.chunk_index)
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    items = result.scalars().all()
    
    return ChunkListResponse(
        items=[ChunkResponse.model_validate(item) for item in items],
        total=total
    )


# ========== 向量搜索 ==========

@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    request: SearchRequest,
    http_request: Request,
    include_shared: bool = Query(True, description="是否包含共享的知识库"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    向量搜索 - 使用 pgvector 进行高效相似度检索
    
    pgvector 使用余弦距离 (1 - 余弦相似度) 进行搜索
    <=> 操作符返回余弦距离，越小越相似
    
    参数:
    - include_shared: 是否包含共享的知识库，默认 True（包含共享知识库）
    """
    search_trace_id = uuid.uuid4().hex[:8]
    search_started_at = time.perf_counter()

    def _elapsed_ms() -> float:
        return (time.perf_counter() - search_started_at) * 1000

    async def _ensure_client_connected(stage: str) -> None:
        if http_request is None:
            return
        if await http_request.is_disconnected():
            logger.warning(
                f"[search:{search_trace_id}] 客户端已断开，终止搜索: stage={stage}, elapsed={_elapsed_ms():.2f}ms"
            )
            raise HTTPException(status_code=499, detail="客户端已取消请求")

    logger.info(
        f"[search:{search_trace_id}] 搜索开始: 用户={current_user.id}, "
        f"查询长度={len(request.query)}, top_k={request.top_k}, 阈值={request.score_threshold}, "
        f"改写={request.use_query_rewrite}, 混合检索={request.use_hybrid}, "
        f"精排={request.use_reranker}, 压缩={request.use_contextual_compression}, "
        f"相邻上下文={request.include_adjacent_chunks}, 父级上下文={request.include_parent_context}, "
        f"主超时ms={settings.search_timeout_primary_ms}"
    )
    await _ensure_client_connected("start")
    
    # 获取用户可访问的知识库ID
    own_kb_result = await db.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.user_id == current_user.id)
    )
    own_kb_ids = set(row[0] for row in own_kb_result.fetchall())
    
    # 如果启用共享，获取共享的知识库ID
    shared_kb_ids = set()
    if include_shared and SHARING_ENABLED:
        shared_kb_ids = await get_shared_kb_ids(current_user, db)
    
    accessible_kb_ids = own_kb_ids | shared_kb_ids
    
    # 确定要搜索的知识库
    if request.knowledge_base_ids:
        # 验证知识库权限
        for kb_id in request.knowledge_base_ids:
            if kb_id in accessible_kb_ids:
                continue  # 有权限
            # 检查是否为公开知识库
            kb = await db.get(KnowledgeBase, kb_id)
            if not kb or not kb.is_public:
                raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在或无权限")
        kb_ids = request.knowledge_base_ids
    else:
        # 搜索用户所有可访问的知识库
        kb_ids = list(accessible_kb_ids)
        
        if not kb_ids:
            logger.info(
                f"[search:{search_trace_id}] 无可访问知识库，elapsed={_elapsed_ms():.2f}ms"
            )
            return SearchResponse(
                query=request.query,
                results=[],
                total=0,
                search_time_ms=round(_elapsed_ms(), 2)
            )

    logger.info(
        f"[search:{search_trace_id}] 权限解析完成: 自有库={len(own_kb_ids)}, "
        f"共享库={len(shared_kb_ids)}, 本次检索库={len(kb_ids)}, elapsed={_elapsed_ms():.2f}ms"
    )
    await _ensure_client_connected("access_resolved")

    rewrite_service = get_query_rewrite_service()
    rewrite_started_at = time.perf_counter()
    await _ensure_client_connected("before_query_rewrite")
    rewrite_result = await rewrite_service.rewrite_query(
        request.query,
        rewrite_mode=request.rewrite_mode,
        use_query_rewrite=request.use_query_rewrite,
        requested_strategies=request.query_rewrite_strategies,
    )
    logger.info(
        f"[search:{search_trace_id}] 查询改写完成: 启用={rewrite_result.enabled}, "
        f"缓存命中={rewrite_result.cache_hit}, 调用LLM={rewrite_result.llm_called}, "
        f"跳过原因={rewrite_result.skip_reason or '-'}, "
        f"向量变体数={len(rewrite_result.vector_variants)}, "
        f"文本变体数={len(rewrite_result.text_variants)}, "
        f"stage_ms={(time.perf_counter() - rewrite_started_at) * 1000:.2f}, "
        f"elapsed={_elapsed_ms():.2f}ms"
    )
    await _ensure_client_connected("query_rewrite_done")
    
    # 使用 pgvector 进行向量相似度搜索
    # <=> 是余弦距离运算符 (cosine distance = 1 - cosine similarity)
    # 距离越小，相似度越高
    # 我们需要将距离阈值转换为：score_threshold 对应 distance_threshold = 1 - score_threshold
    distance_threshold = 1 - request.score_threshold
    use_reranker = settings.enable_reranker and request.use_reranker
    use_hybrid = settings.enable_hybrid_retrieval and request.use_hybrid
    final_top_k = request.top_k

    reranker_candidate_k = (
        max(final_top_k, settings.reranker_top_k)
        if use_reranker
        else final_top_k
    )
    vector_top_k = max(
        reranker_candidate_k,
        settings.hybrid_vector_top_k if use_hybrid else 0,
    )
    text_top_k = (
        max(reranker_candidate_k, settings.hybrid_text_top_k)
        if use_hybrid
        else 0
    )
    fusion_limit = reranker_candidate_k
    
    # 构建 pgvector 原生查询，按 (embedding_model, embedding_dimension) 分组检索
    from sqlalchemy import text

    base_where_clauses = ["dc.knowledge_base_id = ANY(:kb_ids)"]
    base_params = {"kb_ids": kb_ids}

    if request.chunk_level and request.chunk_level != "all":
        base_where_clauses.append("dc.chunk_level = :chunk_level")
        base_params["chunk_level"] = request.chunk_level

    if request.section_type:
        base_where_clauses.append("dc.section_type = :section_type")
        base_params["section_type"] = request.section_type

    vector_groups_where_sql = " AND ".join(
        base_where_clauses
        + [
            "dc.embedding IS NOT NULL",
            "dc.embedding_dimension IS NOT NULL",
        ]
    )
    vector_groups_sql = text(
        f"""
        SELECT
            COALESCE(NULLIF(dc.embedding_model, ''), :default_embedding_model) AS embedding_model,
            dc.embedding_dimension AS embedding_dimension,
            COUNT(*) AS chunk_count
        FROM document_chunks dc
        WHERE {vector_groups_where_sql}
        GROUP BY COALESCE(NULLIF(dc.embedding_model, ''), :default_embedding_model), dc.embedding_dimension
        ORDER BY chunk_count DESC
        """
    )
    vector_group_started_at = time.perf_counter()
    await _ensure_client_connected("before_vector_groups")
    vector_groups = (
        await db.execute(
            vector_groups_sql,
            {
                **base_params,
                "default_embedding_model": settings.local_embedding_model,
            },
        )
    ).fetchall()
    logger.info(
        f"[search:{search_trace_id}] 向量分组完成: 分组数={len(vector_groups)}, "
        f"分组块总数={sum(int(getattr(g, 'chunk_count', 0) or 0) for g in vector_groups)}, "
        f"stage_ms={(time.perf_counter() - vector_group_started_at) * 1000:.2f}, "
        f"elapsed={_elapsed_ms():.2f}ms"
    )

    vector_rows = []
    vector_group_rows = []
    vector_variants = rewrite_result.vector_variants
    if not vector_variants:
        vector_variants = rewrite_result.vector_variants = [
            QueryVariant(text=request.query, strategy="original")
        ]

    total_chunks = 0
    resolved_ef_search = int(settings.pgvector_hnsw_ef_search)
    ef_search_debug: list[dict[str, int]] = []
    retrieval_dimensions: set[int] = set()

    for group in vector_groups:
        await _ensure_client_connected("vector_group_loop")
        group_started_at = time.perf_counter()
        group_model = str(getattr(group, "embedding_model", "") or settings.local_embedding_model).strip()
        group_dimension = int(getattr(group, "embedding_dimension", 0) or 0)
        group_chunks = int(getattr(group, "chunk_count", 0) or 0)
        if group_dimension <= 0:
            continue

        retrieval_dimensions.add(group_dimension)
        total_chunks += group_chunks

        group_embedding_svc = get_embedding_service_for_model_and_dimension(
            group_model,
            group_dimension,
        )
        vector_texts = [variant.text for variant in vector_variants]
        vector_embeddings: list[list[float]] = []
        embedding_mode = "batch"
        embedding_started_at = time.perf_counter()
        try:
            vector_embeddings = await group_embedding_svc.embed_texts(vector_texts, is_query=True)
            if len(vector_embeddings) != len(vector_texts):
                raise ValueError(
                    f"embedding count mismatch: {len(vector_embeddings)} vs {len(vector_texts)}"
                )
        except Exception as e:
            embedding_mode = "single_fallback"
            logger.warning(
                f"[search:{search_trace_id}] 批量查询向量生成失败: 模型={group_model}, 维度={group_dimension}, "
                f"回退单条生成: {e}"
            )
            vector_embeddings = []
            for variant in vector_variants:
                await _ensure_client_connected("vector_embed_single_fallback")
                try:
                    emb = await group_embedding_svc.embed_text(variant.text, is_query=True)
                except Exception as single_exc:
                    logger.warning(
                        f"[search:{search_trace_id}] 单条查询向量生成失败: 策略={variant.strategy}, "
                        f"模型={group_model}, 维度={group_dimension}: {single_exc}"
                    )
                    emb = []
                vector_embeddings.append(emb)
        logger.info(
            f"[search:{search_trace_id}] 查询向量生成完成: 模型={group_model}, 维度={group_dimension}, "
            f"变体数={len(vector_texts)}, 模式={embedding_mode}, "
            f"stage_ms={(time.perf_counter() - embedding_started_at) * 1000:.2f}"
        )

        distance_expr = (
            f"(dc.embedding::vector({group_dimension}) <=> "
            f"(:query_vector)::vector({group_dimension}))"
        )
        vector_where_sql = " AND ".join(
            base_where_clauses
            + [
                "dc.embedding IS NOT NULL",
                "dc.embedding_dimension = :vector_dimension",
                f"{distance_expr} <= :distance_threshold",
            ]
        )
        vector_sql = text(
            f"""
            SELECT
                dc.id,
                dc.document_id,
                dc.knowledge_base_id,
                dc.content,
                dc.chunk_index,
                dc.metadata,
                dc.chunk_level,
                dc.section_type,
                dc.section_title,
                dc.context_summary,
                dc.parent_chunk_id,
                dc.embedding_model,
                dc.embedding_dimension,
                1 - {distance_expr} AS similarity,
                NULL::float AS text_score,
                d.original_filename AS document_name,
                kb.name AS knowledge_base_name
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
            WHERE {vector_where_sql}
            ORDER BY {distance_expr}
            LIMIT :vector_top_k
            """
        )

        resolved_group_ef = resolve_ef_search(total_chunks=group_chunks, dimension=group_dimension)
        await apply_hnsw_ef_search(
            db,
            resolved_group_ef,
            source=f"knowledge.search.dim{group_dimension}",
        )
        resolved_ef_search = max(resolved_ef_search, resolved_group_ef)
        ef_search_debug.append(
            {
                "dimension": group_dimension,
                "chunks": group_chunks,
                "ef_search": resolved_group_ef,
            }
        )

        group_base_params = {
            **base_params,
            "vector_dimension": group_dimension,
        }
        group_variant_hits = 0
        for idx, variant in enumerate(vector_variants):
            await _ensure_client_connected("vector_variant_loop")
            query_embedding = vector_embeddings[idx] if idx < len(vector_embeddings) else []
            if not query_embedding:
                continue
            if len(query_embedding) != group_dimension:
                logger.warning(
                    f"[search:{search_trace_id}] 跳过查询变体（维度不匹配）: 模型={group_model}, "
                    f"分组维度={group_dimension}, 向量维度={len(query_embedding)}"
                )
                continue

            vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
            vector_params = {
                **group_base_params,
                "query_vector": vector_str,
                "distance_threshold": distance_threshold,
                "vector_top_k": vector_top_k,
            }
            rows = (await db.execute(vector_sql, vector_params)).fetchall()
            if rows:
                vector_group_rows.append((variant.strategy, variant.text, rows))
                group_variant_hits += len(rows)

        logger.info(
            f"[search:{search_trace_id}] 向量分组检索完成: 模型={group_model}, 维度={group_dimension}, "
            f"语料块={group_chunks}, ef_search={resolved_group_ef}, 命中行={group_variant_hits}, "
            f"stage_ms={(time.perf_counter() - group_started_at) * 1000:.2f}, elapsed={_elapsed_ms():.2f}ms"
        )

    vector_rows = merge_rows_by_score(
        vector_group_rows,
        score_attr="similarity",
        query_attr="matched_vector_query",
        strategy_attr="matched_vector_strategy",
        limit=vector_top_k,
    )
    logger.info(
        f"[search:{search_trace_id}] 向量结果合并完成: 合并命中={len(vector_rows)}, "
        f"分组结果={len(vector_group_rows)}, elapsed={_elapsed_ms():.2f}ms"
    )
    if not vector_rows and not use_hybrid:
        raise HTTPException(status_code=400, detail="无法生成有效查询向量")

    text_rows = []
    text_group_rows_count = 0
    if use_hybrid:
        await _ensure_client_connected("before_hybrid_fts")
        text_stage_started_at = time.perf_counter()
        text_variants = rewrite_result.text_variants or [
            QueryVariant(text=request.query, strategy="original")
        ]
        text_where_sql = " AND ".join(
            base_where_clauses
            + [
                "COALESCE(NULLIF(dc.content_segmented, ''), dc.content) IS NOT NULL",
                "COALESCE(NULLIF(dc.content_segmented, ''), dc.content) <> ''",
                "to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)) @@ websearch_to_tsquery('simple', :fts_query)",
            ]
        )
        text_sql = text(f"""
            SELECT 
                dc.id,
                dc.document_id,
                dc.knowledge_base_id,
                dc.content,
                dc.chunk_index,
                dc.metadata,
                dc.chunk_level,
                dc.section_type,
                dc.section_title,
                dc.context_summary,
                dc.parent_chunk_id,
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
            WHERE {text_where_sql}
            ORDER BY text_score DESC
            LIMIT :text_top_k
        """)
        text_group_rows = []
        for variant in text_variants:
            await _ensure_client_connected("hybrid_fts_variant_loop")
            if not variant.text.strip():
                continue
            fts_query = segment_text_for_fts(variant.text)
            if not fts_query.strip():
                continue
            text_params = {
                **base_params,
                "fts_query": fts_query,
                "text_top_k": text_top_k,
            }
            try:
                rows = (await db.execute(text_sql, text_params)).fetchall()
                if rows:
                    text_group_rows.append((variant.strategy, variant.text, rows))
            except Exception as e:
                logger.warning(
                    f"[search:{search_trace_id}] 全文检索失败: 策略={variant.strategy}, "
                    f"回退其他候选: {e}"
                )
                continue

        text_rows = merge_rows_by_score(
            text_group_rows,
            score_attr="text_score",
            query_attr="matched_text_query",
            strategy_attr="matched_text_strategy",
            limit=text_top_k,
        )
        text_group_rows_count = len(text_group_rows)
        logger.info(
            f"[search:{search_trace_id}] 混合全文检索完成: 变体数={len(text_variants)}, "
            f"分组结果={text_group_rows_count}, 合并命中={len(text_rows)}, "
            f"stage_ms={(time.perf_counter() - text_stage_started_at) * 1000:.2f}, "
            f"elapsed={_elapsed_ms():.2f}ms"
        )

    fuse_started_at = time.perf_counter()
    await _ensure_client_connected("before_rrf_fuse")
    fused_candidates = fuse_rrf(
        vector_rows=vector_rows,
        text_rows=text_rows if use_hybrid else [],
        rrf_k=settings.hybrid_rrf_k,
        limit=fusion_limit,
    )
    logger.info(
        f"[search:{search_trace_id}] RRF融合完成: 融合候选={len(fused_candidates)}, "
        f"stage_ms={(time.perf_counter() - fuse_started_at) * 1000:.2f}, elapsed={_elapsed_ms():.2f}ms"
    )

    selected_candidates = []
    if use_reranker and fused_candidates:
        await _ensure_client_connected("before_rerank")
        rerank_started_at = time.perf_counter()
        try:
            reranker = get_reranker_service()
            rerank_documents = _build_reranker_documents(fused_candidates)
            rerank_lengths = [len(doc) for doc in rerank_documents if doc]
            avg_chars = (sum(rerank_lengths) / len(rerank_lengths)) if rerank_lengths else 0.0
            max_chars = max(rerank_lengths) if rerank_lengths else 0
            avg_est_tokens = (avg_chars / 4.0) if avg_chars > 0 else 0.0
            logger.info(
                f"[search:{search_trace_id}] 精排输入准备: 候选={len(rerank_documents)}, "
                f"avg_chars={avg_chars:.1f}, max_chars={max_chars}, "
                f"avg_est_tokens={avg_est_tokens:.1f}, elapsed={_elapsed_ms():.2f}ms"
            )
            reranked = await reranker.rerank(
                query=request.query,
                documents=rerank_documents,
                top_k=final_top_k,
            )
            selected_candidates = [
                (fused_candidates[idx], score)
                for idx, score in reranked
                if 0 <= idx < len(fused_candidates)
            ]
            logger.info(
                f"[search:{search_trace_id}] 精排完成: 输入候选={len(fused_candidates)}, "
                f"输出候选={len(selected_candidates)}, "
                f"stage_ms={(time.perf_counter() - rerank_started_at) * 1000:.2f}, "
                f"elapsed={_elapsed_ms():.2f}ms"
            )
        except Exception as e:
            logger.warning(
                f"[search:{search_trace_id}] 精排失败，回退检索排序: {e}"
            )

    if not selected_candidates:
        selected_candidates = [
            (candidate, None)
            for candidate in fused_candidates[:final_top_k]
        ]
    logger.info(
        f"[search:{search_trace_id}] 候选选择完成: 最终候选={len(selected_candidates)}, "
        f"elapsed={_elapsed_ms():.2f}ms"
    )
    
    # 构建结果
    contextual_compression = get_contextual_compression_service()
    compression_inputs = []
    for source_id, (candidate, reranker_score) in enumerate(selected_candidates, start=1):
        row = candidate.row
        compression_inputs.append(
            CompressionInput(
                source_id=source_id,
                doc_name=(row.document_name or "未知文档"),
                chunk_idx=int(getattr(row, "chunk_index", 0) or 0),
                chunk_content=row.content or "",
                reranker_score=float(reranker_score) if reranker_score is not None else None,
            )
        )
    compression_started_at = time.perf_counter()
    logger.info(
        f"[search:{search_trace_id}] 压缩阶段开始: 输入片段={len(compression_inputs)}, "
        f"启用压缩={request.use_contextual_compression}"
    )
    await _ensure_client_connected("before_compression")
    compression_results = await contextual_compression.compress_chunks(
        request.query,
        compression_inputs,
        use_contextual_compression=request.use_contextual_compression,
    )
    compression_used_count = sum(1 for item in compression_results if item.used_compression)
    compression_fallback_count = sum(1 for item in compression_results if item.fallback_reason)
    logger.info(
        f"[search:{search_trace_id}] 压缩阶段完成: 输出片段={len(compression_results)}, "
        f"实际压缩={compression_used_count}, 回退次数={compression_fallback_count}, "
        f"stage_ms={(time.perf_counter() - compression_started_at) * 1000:.2f}, "
        f"elapsed={_elapsed_ms():.2f}ms"
    )
    compression_by_source_id = {
        item.source_id: item
        for item in compression_results
    }

    results = []
    parent_ids_to_fetch = set()
    adjacent_targets: list[tuple[int, int, int]] = []
    
    max_rrf_score = max((c.rrf_score for c in fused_candidates), default=0.0)
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

        metadata = dict(row.metadata or {})
        metadata["retrieval_mode"] = "hybrid" if use_hybrid else "vector"
        row_dimension = getattr(row, "embedding_dimension", None)
        if row_dimension is not None:
            metadata["retrieval_dimension"] = int(row_dimension)
        elif retrieval_dimensions:
            metadata["retrieval_dimension"] = int(sorted(retrieval_dimensions)[0])
        row_embedding_model = getattr(row, "embedding_model", None)
        if row_embedding_model:
            metadata["retrieval_embedding_model"] = row_embedding_model
        metadata["rrf_score"] = round(float(candidate.rrf_score), 6)
        metadata["contextual_compression_enabled"] = bool(
            compression_result and compression_result.used_compression
        )
        metadata["contextual_compression_source"] = source_label
        metadata["contextual_compression_score"] = compression_score
        metadata["contextual_compression_fallback"] = compression_fallback
        if compressed_content:
            metadata["contextual_compression_excerpt"] = compressed_content
        metadata["query_rewrite_enabled"] = rewrite_result.enabled
        metadata["query_rewrite_strategies"] = rewrite_result.strategies
        metadata["query_rewrite_cache_hit"] = rewrite_result.cache_hit
        metadata["query_rewrite_skip_reason"] = rewrite_result.skip_reason
        metadata["query_rewrite_llm_called"] = rewrite_result.llm_called
        if rewrite_result.fallback_reason:
            metadata["query_rewrite_fallback"] = rewrite_result.fallback_reason
        matched_vector_query = getattr(row, "matched_vector_query", None)
        matched_vector_strategy = getattr(row, "matched_vector_strategy", None)
        matched_text_query = getattr(row, "matched_text_query", None)
        matched_text_strategy = getattr(row, "matched_text_strategy", None)
        if matched_vector_query:
            metadata["matched_vector_query"] = matched_vector_query
        if matched_vector_strategy:
            metadata["matched_vector_strategy"] = matched_vector_strategy
        if matched_text_query:
            metadata["matched_text_query"] = matched_text_query
        if matched_text_strategy:
            metadata["matched_text_strategy"] = matched_text_strategy
        if candidate.vector_rank is not None:
            metadata["vector_rank"] = candidate.vector_rank
        if candidate.text_rank is not None:
            metadata["text_rank"] = candidate.text_rank
        if vector_score is not None:
            metadata["vector_score"] = vector_score
        if text_score is not None:
            metadata["text_score"] = text_score

        if reranker_score is not None:
            metadata["reranker_score"] = round(float(reranker_score), 4)
            score = round(RerankerService.normalize_score(float(reranker_score)), 4)
        elif use_hybrid and max_rrf_score > 0:
            score = round(candidate.rrf_score / max_rrf_score, 4)
        elif vector_score is not None:
            score = vector_score
        else:
            score = 0.0

        item = SearchResultItem(
            chunk_id=row.id,
            document_id=row.document_id,
            knowledge_base_id=row.knowledge_base_id,
            document_name=row.document_name or "未知文档",
            knowledge_base_name=row.knowledge_base_name or "未知知识库",
            content=compressed_content or row.content,
            score=score,
            chunk_index=row.chunk_index,
            metadata=metadata,
            chunk_level=getattr(row, 'chunk_level', None),
            section_type=getattr(row, 'section_type', None),
            section_title=getattr(row, 'section_title', None),
        )
        results.append(item)

        chunk_index = int(getattr(row, "chunk_index", -1) or -1)
        if request.include_adjacent_chunks and chunk_index >= 0:
            adjacent_targets.append((len(results) - 1, int(row.document_id), chunk_index))
        
        # [Fix 12] 收集需要回溯的父级 chunk
        parent_id = getattr(row, 'parent_chunk_id', None)
        if request.include_parent_context and parent_id:
            parent_ids_to_fetch.add((len(results) - 1, parent_id))
    
    # [Fix 12] 批量回溯父级上下文
    if parent_ids_to_fetch:
        await _ensure_client_connected("before_parent_context")
        parent_ctx_started_at = time.perf_counter()
        parent_ids = [pid for _, pid in parent_ids_to_fetch]
        parent_result = await db.execute(
            select(DocumentChunk.id, DocumentChunk.content, DocumentChunk.section_title)
            .where(DocumentChunk.id.in_(parent_ids))
        )
        parent_map = {row.id: row for row in parent_result.fetchall()}
        
        for idx, pid in parent_ids_to_fetch:
            if pid in parent_map:
                parent = parent_map[pid]
                # [Fix 13] 优先展示父级 section_title，而非截取正文开头
                # 旧逻辑: parent.content[:300] — 对大章节块常常返回无关文字
                if parent.section_title:
                    # 有标题时：展示标题 + 正文开头的简短摘要
                    title_prefix = f"📌 {parent.section_title}"
                    # 跳过标题行本身，取正文前 200 字符作为补充
                    content_lines = parent.content.split('\n')
                    # 找到非标题的第一行
                    body_start = 0
                    for li, line in enumerate(content_lines):
                        stripped = line.strip()
                        if stripped and not stripped.startswith('#') and not re.match(r'^(\d+[\.\d]*)\s', stripped):
                            body_start = li
                            break
                    body_preview = '\n'.join(content_lines[body_start:]).strip()[:200]
                    if body_preview:
                        results[idx].parent_context = f"{title_prefix}\n{body_preview}..."
                    else:
                        results[idx].parent_context = title_prefix
                else:
                    # 无标题时：回退到旧逻辑，但截取更短
                    results[idx].parent_context = parent.content[:200] + "..."
                if not results[idx].section_title:
                    results[idx].section_title = parent.section_title
        logger.info(
            f"[search:{search_trace_id}] 父级上下文补全完成: 目标数={len(parent_ids_to_fetch)}, "
            f"stage_ms={(time.perf_counter() - parent_ctx_started_at) * 1000:.2f}, "
            f"elapsed={_elapsed_ms():.2f}ms"
        )

    # 相邻窗口上下文补充
    if request.include_adjacent_chunks and adjacent_targets:
        await _ensure_client_connected("before_adjacent_context")
        adjacent_ctx_started_at = time.perf_counter()
        window = normalize_adjacent_window(request.adjacent_window)
        neighbor_keys: set[tuple[int, int]] = set()
        for _, doc_id, chunk_index in adjacent_targets:
            neighbor_keys.update(build_adjacent_lookup_keys(doc_id, chunk_index, window))

        adjacent_map: dict[tuple[int, int], Any] = {}
        if neighbor_keys:
            adjacent_rows = await db.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.document_id,
                    DocumentChunk.chunk_index,
                    DocumentChunk.chunk_level,
                    DocumentChunk.section_title,
                    DocumentChunk.content,
                ).where(
                    tuple_(DocumentChunk.document_id, DocumentChunk.chunk_index).in_(list(neighbor_keys))
                )
            )
            adjacent_map = {
                (int(row.document_id), int(row.chunk_index)): row
                for row in adjacent_rows.fetchall()
            }

        for idx, doc_id, chunk_index in adjacent_targets:
            metadata = dict(results[idx].metadata or {})
            metadata["adjacent_context"] = merge_adjacent_context(
                document_id=doc_id,
                chunk_index=chunk_index,
                window=window,
                row_map=adjacent_map,
            )
            results[idx].metadata = metadata
        logger.info(
            f"[search:{search_trace_id}] 相邻上下文补全完成: 目标数={len(adjacent_targets)}, "
            f"窗口={window}, 查询键数={len(neighbor_keys)}, "
            f"stage_ms={(time.perf_counter() - adjacent_ctx_started_at) * 1000:.2f}, "
            f"elapsed={_elapsed_ms():.2f}ms"
        )
    
    search_time = _elapsed_ms()
    
    logger.info(
        f"[search:{search_trace_id}] 向量搜索完成: 查询片段='{request.query[:50]}...', 结果数={len(results)}, "
        f"混合检索={use_hybrid}, 精排={use_reranker}, "
        f"查询改写={rewrite_result.enabled}, "
        f"改写变体={len(rewrite_result.vector_variants)}, "
        f"向量命中={len(vector_rows)}, 文本命中={len(text_rows)}, "
        f"ef_search={resolved_ef_search}, 语料规模={total_chunks}, 维度={sorted(retrieval_dimensions)}, "
        f"ef明细={ef_search_debug}, "
        f"总耗时={search_time:.2f}ms"
    )
    
    return SearchResponse(
        query=request.query,
        results=results,
        total=len(results),
        search_time_ms=round(search_time, 2)
    )
