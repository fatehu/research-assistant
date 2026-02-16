"""
文献管理 API 路由
"""
import hashlib
import json
import os
import re
import time
import uuid
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete, distinct, text
from sqlalchemy.orm import selectinload
from loguru import logger
from pydantic import BaseModel, Field

from app.config import settings
from app.core.database import async_session_factory, get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.literature import (
    AskScope,
    AnnotationType,
    KnowledgeLinkStatus,
    LiteratureQAMessage,
    LiteratureQASession,
    Paper,
    PaperAnnotation,
    PaperCollection,
    PaperComment,
    PaperEntity,
    PaperKnowledgeLink,
    PaperRating,
    PaperReadSession,
    PaperSearchHistory,
    paper_collection_association,
)
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk, DocumentStatus
from app.models.role import GroupMember
from app.schemas.literature import (
    AddPaperToKnowledgeRequest,
    AddToCollectionRequest,
    CollectionKnowledgeReadinessItem,
    CollectionKnowledgeReadinessResponse,
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
    CollectionWithPapers,
    DownloadPdfRequest,
    LiteratureAskMessage as LiteratureAskMessageSchema,
    LiteratureAskRequest,
    LiteratureAskSession as LiteratureAskSessionSchema,
    PaperAnnotationCreate,
    PaperAnnotationResponse,
    PaperAnnotationUpdate,
    PaperCommentCreate,
    PaperCommentResponse,
    PaperCommentUpdate,
    PaperCreate,
    PaperKnowledgeLinkResponse,
    PaperRatingSummary,
    PaperRatingUpdate,
    PaperResponse,
    PaperSearchResponse,
    PaperSearchResult,
    PaperUpdate,
    ReaderSessionResponse,
    ReaderSessionUpdate,
    RemoveFromCollectionRequest,
    SavePaperFromSearchRequest,
    SearchHistoryResponse,
)
from app.services.chinese_segmentation_service import segment_text_for_fts
from app.services.contextual_compression_service import CompressionInput
from app.services.literature_service import PaperResult, get_literature_service
from app.services.llm_service import get_llm_service
from app.services.react_agent import AgentCore, AgentRuntimeContext
from app.services.agent_tools_impl.registry import ToolBase, ToolRegistry, ToolResult
from app.services.status_event_bus import (
    build_status_channel_for_user,
    iter_status_events,
    publish_status_event,
)

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover - optional dependency at runtime
    redis_async = None

router = APIRouter(prefix="/literature", tags=["literature"])


def paper_to_response(paper, collection_ids: List[int] = None) -> dict:
    """将 Paper 模型转换为响应字典"""
    if collection_ids is None:
        collection_ids = []
    
    return {
        "id": paper.id,
        "user_id": paper.user_id,
        "semantic_scholar_id": paper.semantic_scholar_id,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors or [],
        "year": paper.year,
        "venue": paper.venue,
        "citation_count": paper.citation_count or 0,
        "reference_count": paper.reference_count or 0,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "arxiv_url": paper.arxiv_url,
        "pdf_path": paper.pdf_path,
        "pdf_downloaded": paper.pdf_downloaded or False,
        "knowledge_base_id": paper.knowledge_base_id,
        "document_id": paper.document_id,
        "influential_citation_count": paper.influential_citation_count or 0,
        "fields_of_study": paper.fields_of_study or [],
        "tags": paper.tags or [],
        "is_read": paper.is_read or False,
        "read_at": paper.read_at,
        "notes": paper.notes,
        "rating": paper.rating,
        "source": paper.source or "manual",
        "published_date": paper.published_date,
        "created_at": paper.created_at,
        "updated_at": paper.updated_at,
        "collection_ids": collection_ids,
    }


ASK_CACHE_TTL_SECONDS = 600
_ask_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
_ask_redis_client = None
PAGE_COUNT_CACHE_TTL_SECONDS = 3600
_pdf_page_count_cache: Dict[str, tuple[float, int]] = {}


def _sse_payload(event: str, data: Any) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


async def _publish_paper_link_status_event(link: PaperKnowledgeLink) -> None:
    payload = {
        "event": "paper_link_status",
        "data": {
            "link_id": int(link.id),
            "paper_id": int(link.paper_id),
            "knowledge_base_id": int(link.knowledge_base_id),
            "document_id": int(link.document_id) if link.document_id else None,
            "status": str(link.status),
            "error_message": (link.error_message or None),
            "updated_at": (link.updated_at or datetime.utcnow()).isoformat(),
        },
    }
    try:
        await publish_status_event(build_status_channel_for_user(int(link.user_id)), payload)
    except Exception as exc:  # pragma: no cover - push failures should not break main path
        logger.warning(f"[Literature API] 发布论文入库状态事件失败 link={link.id}: {exc}")


def _normalize_text(value: Optional[str]) -> str:
    return " ".join((value or "").strip().lower().split())


def _normalize_arxiv_id(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if raw.startswith("arxiv:"):
        raw = raw[6:]
    if "v" in raw and raw.rsplit("v", 1)[-1].isdigit():
        raw = raw.rsplit("v", 1)[0]
    return raw


def _build_paper_entity_identity(paper: Paper) -> Dict[str, Any]:
    doi_norm = _normalize_text(paper.doi)
    arxiv_norm = _normalize_arxiv_id(paper.arxiv_id)
    title_norm = _normalize_text(paper.title)
    year = paper.year

    if doi_norm:
        canonical_key = f"doi:{doi_norm}"
    elif arxiv_norm:
        canonical_key = f"arxiv:{arxiv_norm}"
    else:
        canonical_key = f"title:{title_norm}|year:{year or 0}"

    return {
        "canonical_key": canonical_key,
        "doi_norm": doi_norm or None,
        "arxiv_norm": arxiv_norm or None,
        "title_norm": title_norm or None,
        "year": year,
    }


async def _ensure_paper_entity(db: AsyncSession, paper: Paper) -> PaperEntity:
    if paper.paper_entity_id:
        existing = await db.get(PaperEntity, paper.paper_entity_id)
        if existing:
            return existing

    identity = _build_paper_entity_identity(paper)
    stmt = select(PaperEntity).where(PaperEntity.canonical_key == identity["canonical_key"])
    result = await db.execute(stmt)
    entity = result.scalar_one_or_none()
    if entity is None:
        entity = PaperEntity(**identity)
        db.add(entity)
        await db.flush()

    paper.paper_entity_id = entity.id
    return entity


async def _get_owned_paper_or_404(db: AsyncSession, current_user: User, paper_id: int) -> Paper:
    stmt = select(Paper).where(and_(Paper.id == paper_id, Paper.user_id == current_user.id))
    result = await db.execute(stmt)
    paper = result.scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _build_paper_pdf_file_path(
    user_id: int,
    paper_id: int,
    title: Optional[str],
    *,
    ensure_dir: bool = False,
) -> str:
    upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
    pdf_dir = os.path.join(upload_dir, str(user_id), "papers")
    if ensure_dir:
        os.makedirs(pdf_dir, exist_ok=True)
    safe_title = "".join(c for c in (title or "")[:50] if c.isalnum() or c in " -_").strip()
    filename = f"{safe_title or f'paper_{paper_id}'}_{paper_id}.pdf"
    return os.path.join(pdf_dir, filename)


def _resolve_local_pdf_path(user_id: int, paper: Paper) -> Optional[str]:
    candidates: List[str] = []
    if isinstance(paper.pdf_path, str) and paper.pdf_path.strip():
        candidates.append(paper.pdf_path.strip())
    default_path = _build_paper_pdf_file_path(
        user_id=user_id,
        paper_id=int(paper.id),
        title=paper.title,
        ensure_dir=False,
    )
    if default_path not in candidates:
        candidates.append(default_path)

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


async def _get_owned_collection_or_404(
    db: AsyncSession,
    current_user: User,
    collection_id: int,
) -> PaperCollection:
    stmt = select(PaperCollection).where(
        and_(PaperCollection.id == collection_id, PaperCollection.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    collection = result.scalar_one_or_none()
    if not collection:
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    return collection


async def _get_owned_kb_or_404(db: AsyncSession, current_user: User, kb_id: int) -> KnowledgeBase:
    stmt = select(KnowledgeBase).where(and_(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == current_user.id))
    result = await db.execute(stmt)
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


async def _get_same_group_user_ids(db: AsyncSession, user_id: int) -> set[int]:
    group_stmt = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    group_rows = (await db.execute(group_stmt)).fetchall()
    group_ids = [int(row[0]) for row in group_rows]
    if not group_ids:
        return set()

    members_stmt = select(distinct(GroupMember.user_id)).where(GroupMember.group_id.in_(group_ids))
    member_rows = (await db.execute(members_stmt)).fetchall()
    return {int(row[0]) for row in member_rows}


def _round_or_none(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _to_ask_message_response(row: LiteratureQAMessage) -> LiteratureAskMessageSchema:
    sources: List[Dict[str, Any]] = []
    if isinstance(row.sources, list):
        for source in row.sources:
            if isinstance(source, dict):
                sources.append(source)
    return LiteratureAskMessageSchema(
        id=int(row.id),
        session_id=int(row.session_id),
        role=str(row.role),
        content=str(row.content or ""),
        sources=sources,
        created_at=row.created_at,
    )


def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None
    return None


def _extract_page_from_metadata(metadata: Any) -> Optional[int]:
    if not isinstance(metadata, dict):
        return None

    direct_keys = (
        "page",
        "page_number",
        "page_num",
        "pdf_page",
        "pdf_page_number",
        "source_page",
        "source_page_number",
    )
    zero_based_keys = ("page_index", "page_idx", "pdf_page_index")

    for key in direct_keys:
        value = _to_int(metadata.get(key))
        if value is not None and value > 0:
            return value

    for key in zero_based_keys:
        value = _to_int(metadata.get(key))
        if value is not None and value >= 0:
            return value + 1

    for nested_key in ("location", "position", "source", "extra"):
        nested = metadata.get(nested_key)
        if isinstance(nested, dict):
            nested_page = _extract_page_from_metadata(nested)
            if nested_page is not None:
                return nested_page

    return None


def _extract_position_ratio_from_metadata(metadata: Any) -> Optional[float]:
    if not isinstance(metadata, dict):
        return None

    ratio = _to_float(metadata.get("position_ratio"))
    if ratio is None:
        for nested_key in ("location", "position", "source", "extra"):
            nested = metadata.get(nested_key)
            if isinstance(nested, dict):
                ratio = _to_float(nested.get("position_ratio"))
                if ratio is not None:
                    break
    if ratio is None:
        return None
    if ratio < 0:
        return 0.0
    if ratio > 1:
        return 1.0
    return ratio


def _clean_section_title(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    return text[:220]


def _extract_section_info(metadata: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(metadata, dict):
        return None, None

    title = _clean_section_title(metadata.get("section_title") or metadata.get("heading") or metadata.get("title"))
    section_type = metadata.get("section_type")
    if not isinstance(section_type, str):
        section_type = None
    else:
        section_type = section_type.strip().lower() or None

    if title or section_type:
        return title, section_type

    for nested_key in ("location", "position", "source", "extra"):
        nested = metadata.get(nested_key)
        if isinstance(nested, dict):
            nested_title, nested_type = _extract_section_info(nested)
            if nested_title or nested_type:
                return nested_title, nested_type

    return None, None


async def _get_pdf_page_count(file_path: Optional[str]) -> Optional[int]:
    path = (file_path or "").strip()
    if not path or not os.path.exists(path):
        return None

    now_ts = time.time()
    cached = _pdf_page_count_cache.get(path)
    if cached:
        expire_at, page_count = cached
        if expire_at > now_ts:
            return page_count
        _pdf_page_count_cache.pop(path, None)

    def _read_page_count() -> Optional[int]:
        try:
            import pypdf

            with open(path, "rb") as fp:
                return max(0, len(pypdf.PdfReader(fp).pages))
        except Exception as exc:
            logger.debug(f"[Literature Ask] 读取 PDF 页数失败 path={path}: {exc}")
            return None

    page_count = await asyncio.to_thread(_read_page_count)
    if page_count and page_count > 0:
        _pdf_page_count_cache[path] = (now_ts + PAGE_COUNT_CACHE_TTL_SECONDS, page_count)
        return page_count
    return None


def _ask_cache_key(user_id: int, kb_id: int, scope: str, target_id: int, question: str, mode: str) -> str:
    q_hash = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()
    normalized_mode = (mode or "classic").strip().lower()
    return f"lit:ask:v1:{user_id}:{kb_id}:{scope}:{target_id}:{normalized_mode}:{q_hash}"


async def _get_redis_client():
    global _ask_redis_client
    if redis_async is None:
        return None
    if _ask_redis_client is not None:
        return _ask_redis_client

    redis_url = (getattr(settings, "redis_url", "") or "").strip()
    if not redis_url:
        return None

    try:
        _ask_redis_client = redis_async.from_url(redis_url, decode_responses=True)
        return _ask_redis_client
    except Exception as exc:
        logger.warning(f"[Literature Ask] Redis 初始化失败，降级内存缓存: {exc}")
        return None


async def _ask_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    # Redis first
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning(f"[Literature Ask] Redis读取失败，降级内存缓存: {exc}")

    # In-memory fallback
    now_ts = time.time()
    item = _ask_cache_memory.get(cache_key)
    if not item:
        return None
    expire_at, payload = item
    if expire_at <= now_ts:
        _ask_cache_memory.pop(cache_key, None)
        return None
    return payload


async def _ask_cache_set(cache_key: str, payload: Dict[str, Any], ttl_seconds: int = ASK_CACHE_TTL_SECONDS) -> None:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
            return
        except Exception as exc:
            logger.warning(f"[Literature Ask] Redis写入失败，降级内存缓存: {exc}")

    _ask_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


async def _ask_cache_invalidate_prefix(prefix: str) -> None:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            cursor = "0"
            while True:
                cursor, keys = await redis_client.scan(cursor=cursor, match=f"{prefix}*", count=200)
                if keys:
                    await redis_client.delete(*keys)
                if cursor == 0 or cursor == "0":
                    break
        except Exception as exc:
            logger.warning(f"[Literature Ask] Redis失效失败: {exc}")

    for key in list(_ask_cache_memory.keys()):
        if key.startswith(prefix):
            _ask_cache_memory.pop(key, None)


async def _invalidate_ask_cache_for_scope(user_id: int, kb_id: int, scope: str, target_id: int) -> None:
    prefix = f"lit:ask:v1:{user_id}:{kb_id}:{scope}:{target_id}:"
    await _ask_cache_invalidate_prefix(prefix)


async def _invalidate_ask_cache_for_collection(user_id: int, collection_id: int) -> None:
    prefix = f"lit:ask:v1:{user_id}:"
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            cursor = "0"
            while True:
                cursor, keys = await redis_client.scan(cursor=cursor, match=f"{prefix}*", count=200)
                filtered = [k for k in keys if f":collection:{collection_id}:" in k]
                if filtered:
                    await redis_client.delete(*filtered)
                if cursor == 0 or cursor == "0":
                    break
        except Exception as exc:
            logger.warning(f"[Literature Ask] 收藏夹缓存失效失败: {exc}")

    for key in list(_ask_cache_memory.keys()):
        if key.startswith(prefix) and f":collection:{collection_id}:" in key:
            _ask_cache_memory.pop(key, None)


async def _resolve_collection_paper_ids(
    db: AsyncSession,
    current_user: User,
    collection_id: int,
) -> List[int]:
    await _get_owned_collection_or_404(db, current_user, collection_id)
    stmt = select(paper_collection_association.c.paper_id).where(
        paper_collection_association.c.collection_id == collection_id
    )
    rows = (await db.execute(stmt)).fetchall()
    return [int(row[0]) for row in rows]


async def _build_rating_summary(
    db: AsyncSession,
    current_user: User,
    paper_entity_id: int,
) -> PaperRatingSummary:
    my_stmt = select(PaperRating.rating).where(
        and_(
            PaperRating.paper_entity_id == int(paper_entity_id),
            PaperRating.user_id == int(current_user.id),
        )
    )
    my_row = (await db.execute(my_stmt)).first()
    my_rating = int(my_row[0]) if my_row else None

    global_stmt = select(func.avg(PaperRating.rating), func.count(PaperRating.id)).where(
        PaperRating.paper_entity_id == int(paper_entity_id)
    )
    global_avg_raw, global_count_raw = (await db.execute(global_stmt)).one()
    global_avg = float(global_avg_raw) if global_avg_raw is not None else None
    global_count = int(global_count_raw or 0)

    same_group_user_ids = await _get_same_group_user_ids(db, int(current_user.id))
    same_group_avg = None
    same_group_count = 0
    if same_group_user_ids:
        same_stmt = select(func.avg(PaperRating.rating), func.count(PaperRating.id)).where(
            and_(
                PaperRating.paper_entity_id == int(paper_entity_id),
                PaperRating.user_id.in_(list(same_group_user_ids)),
            )
        )
        same_avg_raw, same_count_raw = (await db.execute(same_stmt)).one()
        same_group_avg = float(same_avg_raw) if same_avg_raw is not None else None
        same_group_count = int(same_count_raw or 0)

    return PaperRatingSummary(
        my_rating=my_rating,
        global_avg=_round_or_none(global_avg),
        global_count=global_count,
        same_group_avg=_round_or_none(same_group_avg),
        same_group_count=same_group_count,
    )


async def _retrieve_scope_ready_links(
    db: AsyncSession,
    user_id: int,
    kb_id: int,
    paper_ids: Sequence[int],
) -> tuple[List[PaperKnowledgeLink], Dict[str, Any]]:
    if not paper_ids:
        return [], {"missing_paper_ids": [], "not_ready": []}

    stmt = select(PaperKnowledgeLink).where(
        and_(
            PaperKnowledgeLink.user_id == user_id,
            PaperKnowledgeLink.knowledge_base_id == kb_id,
            PaperKnowledgeLink.paper_id.in_(list(paper_ids)),
        )
    )
    links = (await db.execute(stmt)).scalars().all()
    by_paper_id = {int(item.paper_id): item for item in links}

    ready_links: List[PaperKnowledgeLink] = []
    missing_paper_ids: List[int] = []
    not_ready: List[Dict[str, Any]] = []

    for paper_id in paper_ids:
        item = by_paper_id.get(int(paper_id))
        if not item:
            missing_paper_ids.append(int(paper_id))
            continue
        if item.status == KnowledgeLinkStatus.READY.value and item.document_id:
            ready_links.append(item)
            continue
        not_ready.append(
            {
                "paper_id": int(paper_id),
                "status": item.status,
                "error_message": item.error_message,
            }
        )

    return ready_links, {"missing_paper_ids": missing_paper_ids, "not_ready": not_ready}


async def _retrieve_rag_sources(
    db: AsyncSession,
    knowledge_base_id: int,
    document_ids: Sequence[int],
    question: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    if not document_ids:
        return []

    fts_query = segment_text_for_fts(question or "")
    sql = text(
        """
        SELECT
            dc.id AS chunk_id,
            dc.document_id,
            d.original_filename AS document_name,
            d.file_path,
            dc.content,
            dc.chunk_index,
            dc.section_type,
            dc.section_title,
            dc.metadata,
            ts_rank_cd(
                to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)),
                websearch_to_tsquery('simple', :fts_query)
            ) AS score
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE
            dc.knowledge_base_id = :kb_id
            AND dc.document_id = ANY(:doc_ids)
            AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) IS NOT NULL
            AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) <> ''
            AND to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content))
                @@ websearch_to_tsquery('simple', :fts_query)
        ORDER BY score DESC, dc.id DESC
        LIMIT :top_k
        """
    )

    rows = []
    if fts_query.strip():
        try:
            rows = (
                await db.execute(
                    sql,
                    {
                        "kb_id": int(knowledge_base_id),
                        "doc_ids": list(document_ids),
                        "fts_query": fts_query,
                        "top_k": int(limit),
                    },
                )
            ).fetchall()
        except Exception as exc:
            logger.warning(f"[Literature Ask] FTS检索失败，回退ILIKE: {exc}")

    if not rows:
        fallback_stmt = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id,
                Document.original_filename.label("document_name"),
                Document.file_path,
                DocumentChunk.content,
                DocumentChunk.chunk_index,
                DocumentChunk.section_type,
                DocumentChunk.section_title,
                DocumentChunk.metadata_,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                and_(
                    DocumentChunk.knowledge_base_id == int(knowledge_base_id),
                    DocumentChunk.document_id.in_(list(document_ids)),
                    DocumentChunk.content.ilike(f"%{question[:200]}%"),
                )
            )
            .order_by(DocumentChunk.id.desc())
            .limit(int(limit))
        )
        rows = (await db.execute(fallback_stmt)).fetchall()

    results: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        metadata = getattr(row, "metadata", None)
        if metadata is None:
            metadata = getattr(row, "metadata_", None)
        page = _extract_page_from_metadata(metadata)
        page_source = "metadata" if page is not None else "unknown"
        if page is None:
            ratio = _extract_position_ratio_from_metadata(metadata)
            if ratio is not None:
                page_count = await _get_pdf_page_count(getattr(row, "file_path", None))
                if page_count and page_count > 0:
                    page = min(page_count, max(1, int(round((page_count - 1) * ratio + 1))))
                    page_source = "estimated"

        section_title = _clean_section_title(getattr(row, "section_title", None))
        section_type = getattr(row, "section_type", None)
        if not isinstance(section_type, str):
            section_type = None
        else:
            section_type = section_type.strip().lower() or None
        if section_title is None or section_type is None:
            meta_title, meta_type = _extract_section_info(metadata)
            if section_title is None:
                section_title = meta_title
            if section_type is None:
                section_type = meta_type

        content = (getattr(row, "content", "") or "").strip()
        snippet = content[:240]
        score_value = getattr(row, "score", None)
        score_float = float(score_value) if score_value is not None else None
        score_source = "fts" if score_float is not None else "fallback"
        results.append(
            {
                "idx": idx,
                "chunk_id": int(getattr(row, "chunk_id")),
                "document_id": int(getattr(row, "document_id")),
                "document_name": getattr(row, "document_name") or "未知文档",
                "page": page,
                "page_source": page_source,
                "section_title": section_title,
                "section_type": section_type,
                "snippet": snippet,
                "content": content[:1600],
                "chunk_index": int(getattr(row, "chunk_index") or 0),
                "score": round(score_float, 4) if score_float is not None else None,
                "score_source": score_source,
            }
        )
    return results


def _is_web_mcp_tool_name(tool_name: str) -> bool:
    name = str(tool_name or "").strip().lower()
    if not name.startswith("mcp."):
        return False
    return any(token in name for token in ("search", "scrape", "crawl", "fetch", "browser", "web"))


def _normalize_agent_source_rows(rows: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        idx = _to_int(row.get("idx")) or position
        document_id = _to_int(row.get("document_id"))
        if document_id is None:
            continue
        score = _to_float(row.get("score"))
        score_source = str(row.get("score_source") or ("fts" if score is not None else "fallback")).strip().lower()
        if score_source not in {"fts", "fallback", "paper_read"}:
            score_source = "fallback" if score is None else "fts"
        normalized.append(
            {
                "idx": int(idx),
                "chunk_id": _to_int(row.get("chunk_id")),
                "document_id": int(document_id),
                "document_name": str(row.get("document_name") or row.get("document") or "未知文档"),
                "page": _to_int(row.get("page")),
                "page_source": str(row.get("page_source") or "unknown"),
                "section_title": row.get("section_title"),
                "section_type": row.get("section_type"),
                "snippet": str(row.get("snippet") or ""),
                "score": round(float(score), 4) if score is not None else None,
                "score_source": score_source,
                "chunk_index": _to_int(row.get("chunk_index")) or 0,
                "content": str(row.get("content") or ""),
            }
        )
    normalized.sort(key=lambda item: int(item.get("idx") or 0))
    return normalized


def _build_public_sources_from_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    public_sources: List[Dict[str, Any]] = []
    for source in rows:
        public_sources.append(
            {
                "idx": source.get("idx"),
                "chunk_id": source.get("chunk_id"),
                "document_id": source.get("document_id"),
                "document_name": source.get("document_name"),
                "page": source.get("page"),
                "page_source": source.get("page_source"),
                "section_title": source.get("section_title"),
                "section_type": source.get("section_type"),
                "snippet": source.get("snippet") or "",
                "score": source.get("score"),
                "score_source": source.get("score_source"),
            }
        )
    return public_sources


class LiteratureScopedKnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=12)


class LiteratureSourceIndexAllocator:
    def __init__(self):
        self._source_index_by_key: Dict[str, int] = {}
        self._next_source_index = 1

    def resolve(self, key: str) -> int:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            normalized_key = f"anon:{self._next_source_index}"
        cached_idx = self._source_index_by_key.get(normalized_key)
        if cached_idx is not None and cached_idx > 0:
            return cached_idx
        next_idx = int(self._next_source_index)
        self._source_index_by_key[normalized_key] = next_idx
        self._next_source_index = next_idx + 1
        return next_idx


class LiteratureScopedKnowledgeSearchTool(ToolBase):
    name = "knowledge_search"
    parallel_safe = True
    description = "检索当前论文/收藏夹范围内的知识库片段，并返回可引用来源。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索问题或关键词",
            },
            "top_k": {
                "type": "integer",
                "description": "返回来源数量，默认 8，最大 12",
                "default": 8,
            },
        },
        "required": ["query"],
    }
    input_model = LiteratureScopedKnowledgeSearchInput
    retry_count = 0

    def __init__(
        self,
        db: AsyncSession,
        *,
        knowledge_base_id: int,
        knowledge_base_name: str,
        document_ids: Sequence[int],
        source_index_allocator: Optional["LiteratureSourceIndexAllocator"] = None,
    ):
        self.db = db
        self.knowledge_base_id = int(knowledge_base_id)
        self.knowledge_base_name = str(knowledge_base_name or f"KB#{knowledge_base_id}")
        self.document_ids = sorted({int(item) for item in document_ids if int(item) > 0})
        self.source_index_allocator = source_index_allocator or LiteratureSourceIndexAllocator()

    def _build_source_key(self, source: Dict[str, Any]) -> str:
        chunk_id = _to_int(source.get("chunk_id"))
        if chunk_id is not None and chunk_id > 0:
            return f"chunk:{chunk_id}"

        document_id = _to_int(source.get("document_id")) or 0
        page = _to_int(source.get("page")) or 0
        section_title = str(source.get("section_title") or "").strip().lower()
        snippet = str(source.get("snippet") or "").strip().lower()[:180]
        digest = hashlib.sha1(
            f"{document_id}|{page}|{section_title}|{snippet}".encode("utf-8")
        ).hexdigest()[:20]
        return f"fallback:{digest}"

    def _resolve_stable_source_index(self, source: Dict[str, Any]) -> int:
        key = self._build_source_key(source)
        return int(self.source_index_allocator.resolve(key))

    async def _execute(self, query: str, top_k: int = 8) -> ToolResult:
        if not self.document_ids:
            return ToolResult(
                success=False,
                output="当前范围没有可检索文档，请先完成入库处理。",
                data={"results": [], "total": 0},
                error="no_ready_documents",
            )

        started_at = time.perf_counter()
        rows = await _retrieve_rag_sources(
            self.db,
            knowledge_base_id=self.knowledge_base_id,
            document_ids=self.document_ids,
            question=query,
            limit=min(max(int(top_k or 8), 1), 12),
        )
        normalized_rows = _normalize_agent_source_rows(rows)
        stable_rows: List[Dict[str, Any]] = []
        for source in normalized_rows:
            item = dict(source)
            item["idx"] = self._resolve_stable_source_index(item)
            stable_rows.append(item)
        if not normalized_rows:
            return ToolResult(
                success=False,
                output="在当前论文范围内未检索到可用片段。",
                data={"results": [], "total": 0, "search_time_ms": (time.perf_counter() - started_at) * 1000},
                error="no_results",
            )

        output_lines: List[str] = []
        result_rows: List[Dict[str, Any]] = []
        for source in stable_rows:
            idx = int(source["idx"])
            page_value = source.get("page")
            page_text = str(page_value) if page_value is not None else "未知"
            section_title = str(source.get("section_title") or "").strip()
            section_suffix = f" | 章节: {section_title}" if section_title else ""
            snippet = str(source.get("snippet") or "")
            output_lines.append(
                f"[来源{idx}] 文档: {source['document_name']} | 页码: {page_text}{section_suffix}\n{snippet}"
            )
            result_rows.append(
                {
                    "idx": idx,
                    "chunk_id": source.get("chunk_id"),
                    "document_id": source["document_id"],
                    "document_name": source["document_name"],
                    "document": source["document_name"],
                    "knowledge_base": self.knowledge_base_name,
                    "knowledge_base_name": self.knowledge_base_name,
                    "page": source.get("page"),
                    "page_source": source.get("page_source"),
                    "section_title": source.get("section_title"),
                    "section_type": source.get("section_type"),
                    "snippet": source.get("snippet", ""),
                    "content": source.get("content", ""),
                    "chunk_index": int(source.get("chunk_index") or 0),
                    "score": source.get("score"),
                    "score_source": source.get("score_source"),
                }
            )

        search_time_ms = (time.perf_counter() - started_at) * 1000
        return ToolResult(
            success=True,
            output="\n\n".join(output_lines),
            data={
                "results": result_rows,
                "total": len(result_rows),
                "search_time_ms": round(search_time_ms, 2),
            },
        )


class LiteratureDirectPaperReadInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=12)


_PAPER_READ_CN_TO_EN_TERMS: Dict[str, List[str]] = {
    "摘要": ["abstract"],
    "引言": ["introduction", "background"],
    "背景": ["background"],
    "相关工作": ["related work"],
    "方法": ["method", "methodology", "approach"],
    "研究方法": ["method", "methodology", "approach"],
    "实验": ["experiment", "experiments", "evaluation"],
    "结果": ["result", "results"],
    "数据分析": ["analysis", "data analysis", "evaluation"],
    "讨论": ["discussion", "limitations"],
    "结论": ["conclusion", "conclusions"],
}
_PAPER_READ_EN_TO_CN_TERMS: Dict[str, List[str]] = {
    "abstract": ["摘要"],
    "introduction": ["引言"],
    "background": ["背景", "引言"],
    "related": ["相关工作"],
    "method": ["方法", "研究方法"],
    "methods": ["方法", "研究方法"],
    "methodology": ["方法", "研究方法"],
    "approach": ["方法"],
    "experiment": ["实验"],
    "experiments": ["实验"],
    "evaluation": ["实验", "评估"],
    "result": ["结果"],
    "results": ["结果"],
    "analysis": ["分析", "数据分析"],
    "discussion": ["讨论"],
    "limitation": ["局限性"],
    "limitations": ["局限性"],
    "conclusion": ["结论"],
    "conclusions": ["结论"],
}
_PAPER_READ_SECTION_BACKOFF_TERMS: List[str] = [
    "abstract",
    "introduction",
    "method",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]
_PAPER_READ_SECTION_BACKOFF_TERMS_ZH: List[str] = [
    "摘要",
    "引言",
    "相关工作",
    "方法",
    "实验",
    "结果",
    "讨论",
    "结论",
]


def _detect_text_language(text: str) -> str:
    value = str(text or "")
    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", value))
    en_chars = len(re.findall(r"[A-Za-z]", value))
    total = zh_chars + en_chars
    if total <= 0:
        return "unknown"
    zh_ratio = zh_chars / total
    en_ratio = en_chars / total
    if zh_ratio >= 0.55:
        return "zh"
    if en_ratio >= 0.55:
        return "en"
    return "mixed"


def _extract_query_terms(query: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", str(query or "").lower())
    seen: Set[str] = set()
    primary_terms: List[str] = []
    secondary_terms: List[str] = []
    query_lang = _detect_text_language(query)

    def _append(value: str, *, preferred: bool) -> None:
        normalized = str(value or "").strip().lower()
        if len(normalized) < 2:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        if preferred:
            primary_terms.append(normalized)
        else:
            secondary_terms.append(normalized)

    def _is_preferred_lang(term: str) -> bool:
        if query_lang not in {"zh", "en"}:
            return True
        term_lang = _detect_text_language(term)
        if term_lang == query_lang:
            return True
        if term_lang == "unknown":
            return True
        return False

    for token in tokens:
        value = token.strip().lower()
        _append(value, preferred=_is_preferred_lang(value))
        for mapped in _PAPER_READ_CN_TO_EN_TERMS.get(value, []):
            _append(mapped, preferred=_is_preferred_lang(mapped))
        for mapped in _PAPER_READ_EN_TO_CN_TERMS.get(value, []):
            _append(mapped, preferred=_is_preferred_lang(mapped))

    if query_lang == "zh":
        for fallback in _PAPER_READ_SECTION_BACKOFF_TERMS_ZH:
            _append(fallback, preferred=True)
        for fallback in _PAPER_READ_SECTION_BACKOFF_TERMS:
            _append(fallback, preferred=False)
    elif query_lang == "en":
        for fallback in _PAPER_READ_SECTION_BACKOFF_TERMS:
            _append(fallback, preferred=True)
        for fallback in _PAPER_READ_SECTION_BACKOFF_TERMS_ZH:
            _append(fallback, preferred=False)
    elif any(term in _PAPER_READ_CN_TO_EN_TERMS for term in tokens):
        for fallback in _PAPER_READ_SECTION_BACKOFF_TERMS:
            _append(fallback, preferred=False)

    return primary_terms + secondary_terms


class LiteratureDirectPaperReadTool(ToolBase):
    name = "paper_read"
    parallel_safe = True
    description = "直接阅读当前论文 PDF 并返回相关段落，不依赖知识库入库。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "问题或关键词（优先使用用户问题原文或同语言关键词）",
            },
            "top_k": {
                "type": "integer",
                "description": "返回来源数量，默认 6，最大 12",
                "default": 6,
            },
        },
        "required": ["query"],
    }
    input_model = LiteratureDirectPaperReadInput
    retry_count = 0

    def __init__(
        self,
        *,
        paper_id: int,
        paper_title: str,
        pdf_path: str,
        source_index_allocator: Optional["LiteratureSourceIndexAllocator"] = None,
    ):
        self.paper_id = int(paper_id)
        self.paper_title = str(paper_title or f"paper_{paper_id}")
        self.pdf_path = str(pdf_path or "")
        self.source_index_allocator = source_index_allocator or LiteratureSourceIndexAllocator()
        self._pages_cache: Optional[List[str]] = None

    def _build_source_key(self, page_no: int, snippet: str) -> str:
        digest = hashlib.sha1(f"{self.paper_id}|{page_no}|{snippet[:180]}".encode("utf-8")).hexdigest()[:20]
        return f"paper:{self.paper_id}:page:{page_no}:{digest}"

    async def _load_pdf_pages(self) -> List[str]:
        if self._pages_cache is not None:
            return self._pages_cache

        def _read_pages() -> List[str]:
            import pypdf

            rows: List[str] = []
            with open(self.pdf_path, "rb") as fp:
                reader = pypdf.PdfReader(fp)
                for page in reader.pages:
                    text_value = (page.extract_text() or "").replace("\u00a0", " ")
                    normalized = re.sub(r"\s+", " ", text_value).strip()
                    rows.append(normalized)
            return rows

        pages = await asyncio.to_thread(_read_pages)
        self._pages_cache = pages
        return pages

    @staticmethod
    def _build_snippet(content: str, terms: Sequence[str], max_len: int = 280) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        lower = text.lower()
        best_pos = -1
        for term in terms:
            pos = lower.find(term.lower())
            if pos >= 0 and (best_pos < 0 or pos < best_pos):
                best_pos = pos
        if best_pos < 0:
            return text[:max_len]
        start = max(0, best_pos - max_len // 3)
        end = min(len(text), start + max_len)
        return text[start:end]

    async def _execute(self, query: str, top_k: int = 6) -> ToolResult:
        if not self.pdf_path or not os.path.exists(self.pdf_path):
            return ToolResult(
                success=False,
                output="当前论文 PDF 不存在，请先下载论文。",
                data={"results": [], "total": 0},
                error="paper_pdf_not_found",
            )

        pages = await self._load_pdf_pages()
        if not pages:
            return ToolResult(
                success=False,
                output="未能读取到论文内容。",
                data={"results": [], "total": 0},
                error="paper_content_empty",
            )
        started_at = time.perf_counter()
        query_language = _detect_text_language(query)
        terms = _extract_query_terms(query)

        ranked: List[tuple[float, int, str]] = []
        for page_idx, content in enumerate(pages, start=1):
            if not content:
                continue
            lower = content.lower()
            hit_count = 0
            for term in terms:
                if term.lower() in lower:
                    hit_count += 1
            ratio = (hit_count / max(len(terms), 1)) if terms else 0.0
            # 对首屏摘要页给予轻微偏置，避免零命中时完全随机。
            page_bias = 0.04 if page_idx <= 2 else 0.0
            score = ratio + page_bias
            ranked.append((score, page_idx, content))

        if not ranked:
            return ToolResult(
                success=False,
                output="当前论文未提取到可检索文本。",
                data={"results": [], "total": 0},
                error="paper_content_not_searchable",
            )

        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = ranked[: min(max(int(top_k or 6), 1), 12)]
        top_score = float(selected[0][0]) if selected else 0.0
        low_hit = bool(top_score < 0.08)
        quality_label = "low" if low_hit else "ok"

        output_lines: List[str] = []
        output_lines.append(
            f"[检索诊断] quality={quality_label} | top_score={top_score:.4f} | query_lang={query_language}"
        )
        if low_hit:
            if query_language == "zh":
                output_lines.append("命中较弱：可将问题改写为英文关键词后再次调用 paper_read。")
            elif query_language == "en":
                output_lines.append("Low hit: consider retrying paper_read with a Chinese reformulation if the paper is Chinese.")
            else:
                output_lines.append("命中较弱：可改写 query（中英互换或更具体关键词）后重试一次。")

        result_rows: List[Dict[str, Any]] = []
        for score_value, page_no, content in selected:
            snippet = self._build_snippet(content, terms)
            key = self._build_source_key(page_no, snippet)
            idx = int(self.source_index_allocator.resolve(key))
            output_lines.append(f"[来源{idx}] 文档: {self.paper_title} | 页码: {page_no}\n{snippet}")
            result_rows.append(
                {
                    "idx": idx,
                    "chunk_id": None,
                    "document_id": int(self.paper_id),
                    "document_name": self.paper_title,
                    "document": self.paper_title,
                    "knowledge_base": "当前论文",
                    "knowledge_base_name": "当前论文",
                    "page": int(page_no),
                    "page_source": "metadata",
                    "section_title": None,
                    "section_type": "paper_page",
                    "snippet": snippet,
                    "content": content[:1800],
                    "chunk_index": int(page_no),
                    "score": round(float(score_value), 4),
                    "score_source": "paper_read",
                }
            )

        search_time_ms = (time.perf_counter() - started_at) * 1000
        return ToolResult(
            success=True,
            output="\n\n".join(output_lines),
            data={
                "results": result_rows,
                "total": len(result_rows),
                "search_time_ms": round(search_time_ms, 2),
                "query_language": query_language,
                "quality": quality_label,
                "top_score": round(top_score, 4),
                "suggest_retry": low_hit,
            },
        )


class LiteratureAskAgentCore(AgentCore):
    SYSTEM_PROMPT = """你是论文阅读问答助手（Agent 模式）。

你的目标是基于可验证证据给出高质量回答。
你需要自行决定是否调用工具、调用哪个工具以及调用次数。
不要机械套用固定流程，应根据问题类型动态选择 strategy（例如 knowledge_search、paper_read、web_search/MCP 网页工具）。

决策原则：
1. 当前论文可直接回答时，可使用 paper_read。
1.1 调用 paper_read 时，query 必须尽量复用用户问题原文，不要固定套用中文模板词。
1.2 若 paper_read 首次命中较弱（例如 quality=low 或片段明显不相关），可将 query 做中英互换后重试一次。
2. 需要知识库片段或跨文档证据时，可使用 knowledge_search。
3. 本地证据不足且确有必要时，再使用网页/MCP 工具，并标注时效风险。
4. 避免无意义重复调用，证据充分后直接作答。

边界：
1. 严禁编造事实；证据不足时明确说明“无法从当前资料确定”。
2. 回答先给结论，再给关键证据与引用。

可用工具：
{tools_description}
"""

    CITATION_POLICY_PROMPT = """## 引用规范（必须遵守）
1. 基于检索证据回答时，关键结论后必须加 `[来源X]`。
2. `X` 只能来自工具 observation 已出现过的来源编号。
3. 若使用网页来源，需在结论中明确“网页来源”与时间性风险。
4. 不得输出未在证据中出现的来源编号。
""".strip()

    def __init__(
        self,
        *,
        llm_service,
        tool_registry,
        allowed_tool_names: Set[str],
        max_iterations: Optional[int] = None,
        runtime_context: Optional[AgentRuntimeContext] = None,
    ):
        super().__init__(
            llm_service=llm_service,
            tool_registry=tool_registry,
            max_iterations=max_iterations,
            runtime_context=runtime_context,
        )
        self.allowed_tool_names = set(allowed_tool_names or set())

    @staticmethod
    def _build_observation_message(tool_name: str, observation_output: str) -> str:
        if tool_name == "paper_read":
            followup = (
                "请根据工具返回的信息继续。若检索诊断为 quality=low 或片段不相关，可将 query 做中英互换后再调用一次 paper_read。"
                "若要给出最终回答，必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中出现过的来源编号。"
                "如证据不足，请明确说明。请用<answer>标签给出最终回答。"
            )
        elif tool_name == "knowledge_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中出现过的来源编号。"
                "如证据不足，请明确说明。请用<answer>标签给出最终回答。"
            )
        else:
            followup = "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。"
        return f"<observation>\n{observation_output}\n</observation>\n\n{followup}"

    @classmethod
    def _build_observation_message_multi(cls, observations: Sequence[Any]) -> str:
        if not observations:
            return cls._build_observation_message("", "")
        has_citable_obs = any(getattr(item, "tool_name", "") in {"knowledge_search", "paper_read"} for item in observations)
        output = "\n\n".join(f"[{item.tool_name}]\n{item.observation_output}" for item in observations)
        if has_citable_obs:
            followup = "请综合所有 observation，答案中的引用必须只使用 observation 已出现过的 [来源X]。"
        else:
            followup = "请综合所有 observation 后继续。"
        return f"<observation>\n{output}\n</observation>\n\n{followup}"

    def _build_system_prompt(self, messages: Optional[List[Dict[str, Any]]] = None) -> str:
        user_text = self._latest_user_text(messages)
        include_names = {name for name in self.allowed_tool_names if self.tools.get(name)}
        try:
            tools_desc = self.tools.get_tools_description(include_tool_names=include_names, user_text=user_text)
        except TypeError:
            tools_desc = self.tools.get_tools_description(include_tool_names=include_names)
        selected_tools = sorted(include_names)
        self._last_tool_selection = {
            "intent": "literature_agentic",
            "selected_tools": selected_tools,
            "prompt_desc_tokens": 0,
            "schema_scope": "selected",
            "tool_selection_enabled": True,
        }
        return f"{self.SYSTEM_PROMPT.format(tools_description=tools_desc)}\n\n{self.CITATION_POLICY_PROMPT}"

    async def _execute_single_tool_call(self, context: Any, call: Any, *, parallel_group: str):  # type: ignore[override]
        executed = await super()._execute_single_tool_call(context, call, parallel_group=parallel_group)
        tool_name = str(getattr(call, "name", "") or "")
        if tool_name == "paper_read":
            try:
                context.allowed_source_labels.update(self._extract_source_labels(executed.observation_output))
            except Exception:
                pass
        return executed

    async def _compress_knowledge_observation(
        self,
        query: str,
        result: ToolResult,
        context: Optional[Any] = None,
    ) -> str:
        data = result.data if isinstance(result.data, dict) else {}
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            return result.output

        compression_inputs: List[CompressionInput] = []
        input_rows: List[tuple[int, int, Dict[str, Any]]] = []
        for local_source_id, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            stable_idx = _to_int(row.get("idx")) or local_source_id
            input_rows.append((local_source_id, int(stable_idx), row))
            compression_inputs.append(
                CompressionInput(
                    source_id=local_source_id,
                    doc_name=str(row.get("document") or row.get("document_name") or "unknown_doc"),
                    chunk_idx=int(self._safe_float(row.get("chunk_index"), 0)),
                    chunk_content=str(row.get("content") or ""),
                    reranker_score=float(row["reranker_score"]) if row.get("reranker_score") is not None else None,
                )
            )

        if not compression_inputs:
            return result.output

        if context is not None:
            context.compression_calls += 1

        compression_results = await self.contextual_compression_service.compress_chunks(query, compression_inputs)
        compression_map = {item.source_id: item for item in compression_results}
        parts: List[str] = []

        for local_source_id, stable_idx, row in input_rows:
            source_label = f"来源{stable_idx}"
            compressed = compression_map.get(local_source_id)
            if compressed and compressed.relevant_content:
                content = compressed.relevant_content
                score = compressed.relevance_score
                if context is not None:
                    context.compression_success_chunks += 1
            else:
                raw = str(row.get("content") or "").strip()
                if not raw:
                    continue
                content = f"[{source_label}] {raw[:320]}" + ("..." if len(raw) > 320 else "")
                score = 0.0
                if context is not None:
                    context.compression_fallback_chunks += 1

            retrieval_score = self._safe_float(row.get("score"), 0.0) * 100
            kb_name = row.get("knowledge_base") or row.get("knowledge_base_name") or "unknown_kb"
            doc_name = row.get("document") or row.get("document_name") or "unknown_doc"
            chunk_idx = int(self._safe_float(row.get("chunk_index"), 0))
            parts.append(
                f"\n[{source_label}] (retrieval score {retrieval_score:.1f}%)\n"
                f"Source: {kb_name} / {doc_name} / chunk {chunk_idx}\n"
                f"Compression score: {score:.1f}/10\n"
                f"Content: {content}"
            )

        if not parts:
            return result.output
        return f"Compressed contexts: {len(parts)}\n" + "".join(parts)


async def _build_literature_agent_tool_registry(
    *,
    db: AsyncSession,
    user_id: int,
    knowledge_base_id: int,
    knowledge_base_name: str,
    document_ids: Sequence[int],
    paper_id: Optional[int] = None,
    paper_title: Optional[str] = None,
    paper_pdf_path: Optional[str] = None,
) -> tuple[ToolRegistry, Set[str]]:
    registry = ToolRegistry(
        db=db,
        db_session_factory=async_session_factory,
        user_id=int(user_id),
    )

    source_index_allocator = LiteratureSourceIndexAllocator()

    scoped_tool = LiteratureScopedKnowledgeSearchTool(
        db,
        knowledge_base_id=int(knowledge_base_id),
        knowledge_base_name=str(knowledge_base_name or f"KB#{knowledge_base_id}"),
        document_ids=document_ids,
        source_index_allocator=source_index_allocator,
    )
    registry.register(scoped_tool)

    if paper_id and paper_pdf_path and os.path.exists(str(paper_pdf_path)):
        registry.register(
            LiteratureDirectPaperReadTool(
                paper_id=int(paper_id),
                paper_title=str(paper_title or f"paper_{paper_id}"),
                pdf_path=str(paper_pdf_path),
                source_index_allocator=source_index_allocator,
            )
        )

    def _is_allowed_tool_name(name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"knowledge_search", "paper_read", "web_search", "web_scrape"}:
            return True
        return _is_web_mcp_tool_name(normalized)

    refresh_mcp_tools = getattr(registry, "refresh_mcp_tools", None)
    if callable(refresh_mcp_tools):
        try:
            maybe_awaitable = refresh_mcp_tools(force_refresh=False)
            if hasattr(maybe_awaitable, "__await__"):
                await maybe_awaitable
        except Exception as exc:
            logger.warning(f"[Literature Ask] MCP 工具刷新失败，继续使用本地工具: {exc}")

    registry._tools = {name: tool for name, tool in registry._tools.items() if _is_allowed_tool_name(name)}  # type: ignore[attr-defined]
    registry._mcp_tools = {name: tool for name, tool in registry._mcp_tools.items() if _is_allowed_tool_name(name)}  # type: ignore[attr-defined]

    refresh_method = getattr(registry, "refresh_mcp_tools", None)
    if callable(refresh_method):
        original_refresh = refresh_method

        async def _refresh_filtered(force_refresh: bool = False) -> None:
            maybe = original_refresh(force_refresh=force_refresh)
            if hasattr(maybe, "__await__"):
                await maybe
            registry._mcp_tools = {  # type: ignore[attr-defined]
                name: tool for name, tool in registry._mcp_tools.items() if _is_allowed_tool_name(name)  # type: ignore[attr-defined]
            }

        registry.refresh_mcp_tools = _refresh_filtered  # type: ignore[assignment]

    allowed_tool_names = {
        str(item.get("function", {}).get("name"))
        for item in registry.list_tools()
        if isinstance(item, dict)
    }
    return registry, {name for name in allowed_tool_names if name}


async def _run_document_processing_for_link(link_id: int, doc_id: int, chunk_size: int, chunk_overlap: int) -> None:
    """
    论文入库后台任务：
    1) link -> processing
    2) 复用 knowledge.process_document_task
    3) 根据 document.status 回写 link 状态
    """
    from app.api.knowledge import process_document_task
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        link = await db.get(PaperKnowledgeLink, link_id)
        if not link:
            return
        link.status = KnowledgeLinkStatus.PROCESSING.value
        link.error_message = None
        await db.commit()
        await db.refresh(link)
        await _publish_paper_link_status_event(link)

    await process_document_task(doc_id, chunk_size, chunk_overlap)

    async with async_session_factory() as db:
        link = await db.get(PaperKnowledgeLink, link_id)
        doc = await db.get(Document, doc_id)
        if not link:
            return

        if doc and doc.status == DocumentStatus.COMPLETED.value:
            link.status = KnowledgeLinkStatus.READY.value
            link.error_message = None
            link.document_id = doc.id
        else:
            link.status = KnowledgeLinkStatus.FAILED.value
            link.error_message = (doc.error_message if doc else "文档处理失败") if doc else "文档不存在"

        await db.commit()
        await db.refresh(link)
        await _publish_paper_link_status_event(link)

        # 文档状态变化后清理问答缓存（论文级 + 所在收藏夹级）
        await _invalidate_ask_cache_for_scope(
            user_id=int(link.user_id),
            kb_id=int(link.knowledge_base_id),
            scope="paper",
            target_id=int(link.paper_id),
        )

        coll_stmt = select(paper_collection_association.c.collection_id).where(
            paper_collection_association.c.paper_id == link.paper_id
        )
        coll_rows = (await db.execute(coll_stmt)).fetchall()
        for row in coll_rows:
            await _invalidate_ask_cache_for_scope(
                user_id=int(link.user_id),
                kb_id=int(link.knowledge_base_id),
                scope="collection",
                target_id=int(row[0]),
            )


def _knowledge_not_ready_error(details: Dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "KNOWLEDGE_NOT_READY",
            "message": "目标论文尚未在所选知识库完成入库处理，请先加入知识库并等待处理完成。",
            "details": details,
        },
    )


def _resolve_literature_agent_max_iterations() -> int:
    configured = _to_int(getattr(settings, "literature_agent_max_iterations", None))
    if configured is None or configured <= 0:
        configured = _to_int(getattr(settings, "react_max_iterations", None)) or 8
    return max(2, min(int(configured), 20))


def _to_comment_response(comment: PaperComment) -> PaperCommentResponse:
    return PaperCommentResponse(
        id=comment.id,
        paper_entity_id=comment.paper_entity_id,
        user_id=comment.user_id,
        parent_id=comment.parent_id,
        content=comment.content,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        author={
            "id": comment.user.id if comment.user else comment.user_id,
            "username": comment.user.username if comment.user else "",
            "full_name": comment.user.full_name if comment.user else None,
            "avatar": comment.user.avatar if comment.user else None,
        },
    )


# ============ 论文搜索 ============

@router.get("/search", response_model=PaperSearchResponse)
async def search_papers(
    query: str = Query(..., min_length=1, description="搜索关键词"),
    source: str = Query("semantic_scholar", description="数据源: semantic_scholar, arxiv, pubmed, openalex, crossref"),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    year_start: Optional[int] = Query(None, description="起始年份"),
    year_end: Optional[int] = Query(None, description="结束年份"),
    fields: Optional[str] = Query(None, description="研究领域，逗号分隔"),
    open_access: bool = Query(False, description="仅开放获取"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    搜索论文
    
    支持的数据源:
    - semantic_scholar: Semantic Scholar (综合学术搜索，有引用数据)
    - arxiv: arXiv (预印本，计算机/物理/数学)
    - pubmed: PubMed (生物医学文献)
    - openalex: OpenAlex (开放学术图谱)
    - crossref: CrossRef (DOI 元数据)
    """
    logger.info(f"[Literature API] 搜索: {query}, source={source}, user={current_user.id}")
    
    service = get_literature_service()
    
    # 构建搜索参数
    kwargs = {}
    if year_start and year_end:
        kwargs["year_range"] = (year_start, year_end)
    if fields:
        kwargs["fields_of_study"] = fields.split(",")
    if open_access:
        kwargs["open_access_only"] = True
    
    # 执行搜索
    result = await service.search(query, source, limit, offset, **kwargs)
    
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    
    # 检查哪些论文已保存
    papers = result.get("papers", [])
    search_results = []
    
    for paper in papers:
        is_saved = False
        saved_paper_id = None
        
        # 检查是否已保存
        if paper.external_id:
            if source == "semantic_scholar":
                stmt = select(Paper).where(
                    and_(
                        Paper.user_id == current_user.id,
                        Paper.semantic_scholar_id == paper.external_id
                    )
                )
            elif source == "arxiv":
                stmt = select(Paper).where(
                    and_(
                        Paper.user_id == current_user.id,
                        Paper.arxiv_id == paper.external_id
                    )
                )
            elif source == "pubmed":
                stmt = select(Paper).where(
                    and_(
                        Paper.user_id == current_user.id,
                        Paper.pubmed_id == paper.external_id
                    )
                )
            elif paper.doi:  # crossref, openalex 用 DOI
                stmt = select(Paper).where(
                    and_(
                        Paper.user_id == current_user.id,
                        Paper.doi == paper.doi
                    )
                )
            else:
                stmt = select(Paper).where(
                    and_(
                        Paper.user_id == current_user.id,
                        Paper.title == paper.title
                    )
                )
            
            existing = await db.execute(stmt)
            existing_paper = existing.scalar_one_or_none()
            if existing_paper:
                is_saved = True
                saved_paper_id = existing_paper.id
        
        search_results.append(PaperSearchResult(
            source=paper.source,
            external_id=paper.external_id,
            title=paper.title,
            abstract=paper.abstract,
            authors=[{"name": a.get("name", ""), "authorId": a.get("authorId"), "affiliations": a.get("affiliations", [])} for a in paper.authors],
            year=paper.year,
            venue=paper.venue,
            citation_count=paper.citation_count,
            reference_count=paper.reference_count,
            url=paper.url,
            pdf_url=paper.pdf_url,
            arxiv_id=paper.arxiv_id,
            doi=paper.doi,
            fields_of_study=paper.fields_of_study,
            is_saved=is_saved,
            saved_paper_id=saved_paper_id
        ))
    
    # 保存搜索历史
    history = PaperSearchHistory(
        user_id=current_user.id,
        query=query,
        source=source,
        result_count=result.get("total", 0),
        filters={"year_start": year_start, "year_end": year_end, "fields": fields, "open_access": open_access}
    )
    db.add(history)
    await db.commit()
    
    return PaperSearchResponse(
        total=result.get("total", 0),
        offset=offset,
        papers=search_results,
        query=query,
        source=source
    )


@router.get("/search/history", response_model=List[SearchHistoryResponse])
async def get_search_history(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取搜索历史"""
    stmt = select(PaperSearchHistory).where(
        PaperSearchHistory.user_id == current_user.id
    ).order_by(PaperSearchHistory.created_at.desc()).limit(limit)
    
    result = await db.execute(stmt)
    return result.scalars().all()


# ============ 论文管理 ============

@router.get("/papers", response_model=List[PaperResponse])
async def get_papers(
    collection_id: Optional[int] = Query(None, description="收藏夹 ID"),
    is_read: Optional[bool] = Query(None, description="阅读状态"),
    tag: Optional[str] = Query(None, description="标签"),
    min_rating: Optional[int] = Query(None, ge=1, le=5, description="最低评分"),
    year_start: Optional[int] = Query(None, description="起始年份"),
    year_end: Optional[int] = Query(None, description="结束年份"),
    source: Optional[str] = Query(None, description="来源: semantic_scholar, arxiv, pubmed, openalex, crossref"),
    search: Optional[str] = Query(None, description="搜索标题/摘要"),
    sort_by: str = Query("created_at", description="排序字段: created_at, rating, citation_count, year, title"),
    sort_order: str = Query("desc", description="排序方向: asc, desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取用户的论文列表"""
    stmt = select(Paper).where(Paper.user_id == current_user.id)
    
    # 收藏夹过滤
    if collection_id:
        stmt = stmt.join(paper_collection_association).where(
            paper_collection_association.c.collection_id == collection_id
        )
    
    # 阅读状态过滤
    if is_read is not None:
        stmt = stmt.where(Paper.is_read == is_read)
    
    # 评分过滤
    if min_rating:
        stmt = stmt.where(Paper.rating >= min_rating)
    
    # 年份过滤
    if year_start:
        stmt = stmt.where(Paper.year >= year_start)
    if year_end:
        stmt = stmt.where(Paper.year <= year_end)
    
    # 来源过滤
    if source:
        stmt = stmt.where(Paper.source == source)
    
    # 标签过滤
    if tag:
        stmt = stmt.where(Paper.tags.contains([tag]))
    
    # 搜索
    if search:
        stmt = stmt.where(
            or_(
                Paper.title.ilike(f"%{search}%"),
                Paper.abstract.ilike(f"%{search}%")
            )
        )
    
    # 排序
    sort_column = getattr(Paper, sort_by, Paper.created_at)
    if sort_order == "desc":
        stmt = stmt.order_by(sort_column.desc())
    else:
        stmt = stmt.order_by(sort_column.asc())
    
    stmt = stmt.offset(offset).limit(limit)
    
    result = await db.execute(stmt)
    papers = result.scalars().all()
    
    # 获取收藏夹关联
    paper_responses = []
    for paper in papers:
        # 获取论文所属的收藏夹
        coll_stmt = select(paper_collection_association.c.collection_id).where(
            paper_collection_association.c.paper_id == paper.id
        )
        coll_result = await db.execute(coll_stmt)
        collection_ids = [row[0] for row in coll_result.fetchall()]
        
        paper_responses.append(PaperResponse(**paper_to_response(paper, collection_ids)))
    
    return paper_responses


@router.get("/papers/{paper_id}", response_model=PaperResponse)
async def get_paper(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取论文详情"""
    stmt = select(Paper).where(
        and_(Paper.id == paper_id, Paper.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    paper = result.scalar_one_or_none()
    
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    
    # 获取收藏夹
    coll_stmt = select(paper_collection_association.c.collection_id).where(
        paper_collection_association.c.paper_id == paper.id
    )
    coll_result = await db.execute(coll_stmt)
    collection_ids = [row[0] for row in coll_result.fetchall()]
    
    return PaperResponse(**paper_to_response(paper, collection_ids))


@router.post("/papers", response_model=PaperResponse)
async def save_paper(
    request: SavePaperFromSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """保存论文（从搜索结果）"""
    logger.info(f"[Literature API] 保存论文: {request.title[:50]}...")
    
    # 检查是否已存在
    if request.source == "semantic_scholar" and request.external_id:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == current_user.id,
                Paper.semantic_scholar_id == request.external_id
            )
        )
    elif request.source == "arxiv" and request.arxiv_id:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == current_user.id,
                Paper.arxiv_id == request.arxiv_id
            )
        )
    else:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == current_user.id,
                Paper.title == request.title
            )
        )
    
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    
    if existing:
        raise HTTPException(status_code=400, detail="论文已存在")
    
    # 创建论文
    paper = Paper(
        user_id=current_user.id,
        semantic_scholar_id=request.external_id if request.source == "semantic_scholar" else None,
        arxiv_id=request.arxiv_id,
        doi=request.doi,
        title=request.title,
        abstract=request.abstract,
        authors=request.authors,
        year=request.year,
        venue=request.venue,
        citation_count=request.citation_count,
        reference_count=request.reference_count,
        url=request.url,
        pdf_url=request.pdf_url,
        arxiv_url=f"https://arxiv.org/abs/{request.arxiv_id}" if request.arxiv_id else None,
        fields_of_study=request.fields_of_study,
        source=request.source,
        raw_data=request.raw_data
    )
    
    db.add(paper)
    await db.flush()
    await _ensure_paper_entity(db, paper)
    
    # 添加到收藏夹
    collection_ids = request.collection_ids or []
    
    # 如果没有指定收藏夹，添加到默认收藏夹
    if not collection_ids:
        default_stmt = select(PaperCollection).where(
            and_(
                PaperCollection.user_id == current_user.id,
                PaperCollection.is_default == True
            )
        )
        default_result = await db.execute(default_stmt)
        # 使用 scalars().first() 来安全处理可能存在的多个默认收藏夹
        default_collection = default_result.scalars().first()
        
        if default_collection:
            collection_ids = [default_collection.id]
    
    for coll_id in collection_ids:
        await db.execute(
            paper_collection_association.insert().values(
                paper_id=paper.id,
                collection_id=coll_id
            )
        )
        # 更新收藏夹计数
        await db.execute(
            select(PaperCollection).where(PaperCollection.id == coll_id).with_for_update()
        )
        await db.execute(
            PaperCollection.__table__.update().where(
                PaperCollection.id == coll_id
            ).values(paper_count=PaperCollection.paper_count + 1)
        )
    
    await db.commit()
    await db.refresh(paper)
    
    return PaperResponse(**paper_to_response(paper, collection_ids))


@router.patch("/papers/{paper_id}", response_model=PaperResponse)
async def update_paper(
    paper_id: int,
    update: PaperUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新论文"""
    stmt = select(Paper).where(
        and_(Paper.id == paper_id, Paper.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    paper = result.scalar_one_or_none()
    
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    
    update_data = update.model_dump(exclude_unset=True)
    
    # 处理阅读状态变更
    was_read = paper.is_read
    is_becoming_read = "is_read" in update_data and update_data["is_read"] and not was_read
    is_becoming_unread = "is_read" in update_data and not update_data["is_read"] and was_read
    
    if is_becoming_read:
        update_data["read_at"] = datetime.utcnow()
    
    # 处理评分变更（5星自动收藏）
    was_favorited = paper.rating == 5 if paper.rating else False
    is_becoming_favorited = "rating" in update_data and update_data["rating"] == 5 and not was_favorited
    
    for key, value in update_data.items():
        setattr(paper, key, value)
    
    await db.commit()
    await db.refresh(paper)
    
    # 自动管理收藏夹
    # 获取用户的默认收藏夹
    read_coll = await db.execute(
        select(PaperCollection).where(
            and_(PaperCollection.user_id == current_user.id, PaperCollection.name == "已读")
        )
    )
    read_collection = read_coll.scalar_one_or_none()
    
    unread_coll = await db.execute(
        select(PaperCollection).where(
            and_(PaperCollection.user_id == current_user.id, PaperCollection.name == "待读")
        )
    )
    unread_collection = unread_coll.scalar_one_or_none()
    
    fav_coll = await db.execute(
        select(PaperCollection).where(
            and_(PaperCollection.user_id == current_user.id, PaperCollection.name == "收藏")
        )
    )
    fav_collection = fav_coll.scalar_one_or_none()
    
    # 标记为已读：移到「已读」，从「待读」移除
    if is_becoming_read and read_collection:
        # 添加到已读
        exists = await db.execute(
            select(paper_collection_association).where(
                and_(
                    paper_collection_association.c.paper_id == paper.id,
                    paper_collection_association.c.collection_id == read_collection.id
                )
            )
        )
        if not exists.first():
            await db.execute(
                paper_collection_association.insert().values(
                    paper_id=paper.id,
                    collection_id=read_collection.id
                )
            )
            read_collection.paper_count += 1
        
        # 从待读移除
        if unread_collection:
            await db.execute(
                paper_collection_association.delete().where(
                    and_(
                        paper_collection_association.c.paper_id == paper.id,
                        paper_collection_association.c.collection_id == unread_collection.id
                    )
                )
            )
            if unread_collection.paper_count > 0:
                unread_collection.paper_count -= 1
    
    # 标记为未读：从「已读」移除，移到「待读」
    if is_becoming_unread:
        if read_collection:
            await db.execute(
                paper_collection_association.delete().where(
                    and_(
                        paper_collection_association.c.paper_id == paper.id,
                        paper_collection_association.c.collection_id == read_collection.id
                    )
                )
            )
            if read_collection.paper_count > 0:
                read_collection.paper_count -= 1
        
        if unread_collection:
            exists = await db.execute(
                select(paper_collection_association).where(
                    and_(
                        paper_collection_association.c.paper_id == paper.id,
                        paper_collection_association.c.collection_id == unread_collection.id
                    )
                )
            )
            if not exists.first():
                await db.execute(
                    paper_collection_association.insert().values(
                        paper_id=paper.id,
                        collection_id=unread_collection.id
                    )
                )
                unread_collection.paper_count += 1
    
    # 5星评分自动添加到「收藏」
    if is_becoming_favorited and fav_collection:
        exists = await db.execute(
            select(paper_collection_association).where(
                and_(
                    paper_collection_association.c.paper_id == paper.id,
                    paper_collection_association.c.collection_id == fav_collection.id
                )
            )
        )
        if not exists.first():
            await db.execute(
                paper_collection_association.insert().values(
                    paper_id=paper.id,
                    collection_id=fav_collection.id
                )
            )
            fav_collection.paper_count += 1
    
    await db.commit()
    
    # 获取收藏夹
    coll_stmt = select(paper_collection_association.c.collection_id).where(
        paper_collection_association.c.paper_id == paper.id
    )
    coll_result = await db.execute(coll_stmt)
    collection_ids = [row[0] for row in coll_result.fetchall()]
    
    return PaperResponse(**paper_to_response(paper, collection_ids))


@router.delete("/papers/{paper_id}")
async def delete_paper(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除论文"""
    stmt = select(Paper).where(
        and_(Paper.id == paper_id, Paper.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    paper = result.scalar_one_or_none()
    
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    
    # 更新收藏夹计数
    coll_stmt = select(paper_collection_association.c.collection_id).where(
        paper_collection_association.c.paper_id == paper.id
    )
    coll_result = await db.execute(coll_stmt)
    for row in coll_result.fetchall():
        await db.execute(
            PaperCollection.__table__.update().where(
                PaperCollection.id == row[0]
            ).values(paper_count=func.greatest(PaperCollection.paper_count - 1, 0))
        )
    
    await db.delete(paper)
    await db.commit()
    
    return {"message": "论文已删除"}


# ============ 收藏夹管理 ============

@router.get("/collections", response_model=List[CollectionResponse])
async def get_collections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取收藏夹列表"""
    stmt = select(PaperCollection).where(
        PaperCollection.user_id == current_user.id
    ).order_by(PaperCollection.is_default.desc(), PaperCollection.created_at.asc())
    
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get(
    "/collections/{collection_id}/knowledge-readiness",
    response_model=CollectionKnowledgeReadinessResponse,
)
async def get_collection_knowledge_readiness(
    collection_id: int,
    knowledge_base_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取收藏夹在指定知识库下的入库就绪度摘要。"""
    await _get_owned_collection_or_404(db, current_user, int(collection_id))
    await _get_owned_kb_or_404(db, current_user, int(knowledge_base_id))

    paper_stmt = (
        select(Paper)
        .join(paper_collection_association, paper_collection_association.c.paper_id == Paper.id)
        .where(
            and_(
                paper_collection_association.c.collection_id == int(collection_id),
                Paper.user_id == current_user.id,
            )
        )
        .order_by(Paper.created_at.desc(), Paper.id.desc())
    )
    papers = list((await db.execute(paper_stmt)).scalars().all())
    if not papers:
        return CollectionKnowledgeReadinessResponse(
            collection_id=int(collection_id),
            knowledge_base_id=int(knowledge_base_id),
            total_papers=0,
            ready_papers=0,
            processing_papers=0,
            pending_papers=0,
            failed_papers=0,
            missing_papers=0,
            can_cross_paper_answer=False,
            papers=[],
        )

    paper_ids = [int(item.id) for item in papers]
    link_stmt = select(PaperKnowledgeLink).where(
        and_(
            PaperKnowledgeLink.user_id == current_user.id,
            PaperKnowledgeLink.knowledge_base_id == int(knowledge_base_id),
            PaperKnowledgeLink.paper_id.in_(paper_ids),
        )
    )
    links = list((await db.execute(link_stmt)).scalars().all())
    link_by_paper_id = {int(item.paper_id): item for item in links}

    counts = {
        "ready": 0,
        "processing": 0,
        "pending": 0,
        "failed": 0,
        "missing": 0,
    }
    items: List[CollectionKnowledgeReadinessItem] = []
    for paper in papers:
        link = link_by_paper_id.get(int(paper.id))
        if link is None:
            status_value = "missing"
            document_id = None
            error_message = None
        else:
            raw_status = str(link.status or "").strip().lower()
            if raw_status == KnowledgeLinkStatus.READY.value and link.document_id:
                status_value = "ready"
            elif raw_status == KnowledgeLinkStatus.PROCESSING.value:
                status_value = "processing"
            elif raw_status == KnowledgeLinkStatus.PENDING.value:
                status_value = "pending"
            elif raw_status == KnowledgeLinkStatus.FAILED.value:
                status_value = "failed"
            else:
                status_value = "missing"
            document_id = int(link.document_id) if link.document_id else None
            error_message = link.error_message

        counts[status_value] += 1
        items.append(
            CollectionKnowledgeReadinessItem(
                paper_id=int(paper.id),
                title=str(paper.title or f"paper_{paper.id}"),
                status=status_value,
                document_id=document_id,
                error_message=error_message,
                pdf_available=bool(_resolve_local_pdf_path(int(current_user.id), paper)),
            )
        )

    return CollectionKnowledgeReadinessResponse(
        collection_id=int(collection_id),
        knowledge_base_id=int(knowledge_base_id),
        total_papers=len(paper_ids),
        ready_papers=int(counts["ready"]),
        processing_papers=int(counts["processing"]),
        pending_papers=int(counts["pending"]),
        failed_papers=int(counts["failed"]),
        missing_papers=int(counts["missing"]),
        can_cross_paper_answer=bool(counts["ready"] > 0),
        papers=items,
    )


@router.post("/collections", response_model=CollectionResponse)
async def create_collection(
    collection: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建收藏夹"""
    new_collection = PaperCollection(
        user_id=current_user.id,
        name=collection.name,
        description=collection.description,
        color=collection.color,
        icon=collection.icon,
        collection_type=collection.collection_type
    )
    
    db.add(new_collection)
    await db.commit()
    await db.refresh(new_collection)
    
    return new_collection


@router.patch("/collections/{collection_id}", response_model=CollectionResponse)
async def update_collection(
    collection_id: int,
    update: CollectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新收藏夹"""
    stmt = select(PaperCollection).where(
        and_(
            PaperCollection.id == collection_id,
            PaperCollection.user_id == current_user.id
        )
    )
    result = await db.execute(stmt)
    collection = result.scalar_one_or_none()
    
    if not collection:
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    
    if collection.is_default:
        raise HTTPException(status_code=400, detail="默认收藏夹不可修改")
    
    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(collection, key, value)
    
    await db.commit()
    await db.refresh(collection)
    
    return collection


@router.delete("/collections/{collection_id}")
async def delete_collection(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除收藏夹"""
    stmt = select(PaperCollection).where(
        and_(
            PaperCollection.id == collection_id,
            PaperCollection.user_id == current_user.id
        )
    )
    result = await db.execute(stmt)
    collection = result.scalar_one_or_none()
    
    if not collection:
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    
    if collection.is_default:
        raise HTTPException(status_code=400, detail="默认收藏夹不可删除")
    
    await db.delete(collection)
    await db.commit()
    
    return {"message": "收藏夹已删除"}


@router.post("/collections/add-paper")
async def add_paper_to_collection(
    request: AddToCollectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """将论文添加到收藏夹"""
    # 验证论文
    paper_stmt = select(Paper).where(
        and_(Paper.id == request.paper_id, Paper.user_id == current_user.id)
    )
    paper_result = await db.execute(paper_stmt)
    if not paper_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="论文不存在")
    
    for coll_id in request.collection_ids:
        # 验证收藏夹
        coll_stmt = select(PaperCollection).where(
            and_(
                PaperCollection.id == coll_id,
                PaperCollection.user_id == current_user.id
            )
        )
        coll_result = await db.execute(coll_stmt)
        if not coll_result.scalar_one_or_none():
            continue
        
        # 检查是否已存在
        exists_stmt = select(paper_collection_association).where(
            and_(
                paper_collection_association.c.paper_id == request.paper_id,
                paper_collection_association.c.collection_id == coll_id
            )
        )
        exists_result = await db.execute(exists_stmt)
        if exists_result.first():
            continue
        
        # 添加关联
        await db.execute(
            paper_collection_association.insert().values(
                paper_id=request.paper_id,
                collection_id=coll_id
            )
        )
        
        # 更新计数
        await db.execute(
            PaperCollection.__table__.update().where(
                PaperCollection.id == coll_id
            ).values(paper_count=PaperCollection.paper_count + 1)
        )
    
    await db.commit()
    for coll_id in request.collection_ids:
        await _invalidate_ask_cache_for_collection(current_user.id, int(coll_id))
    return {"message": "已添加到收藏夹"}


@router.post("/collections/remove-paper")
async def remove_paper_from_collection(
    request: RemoveFromCollectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """从收藏夹移除论文"""
    # 验证收藏夹
    coll_stmt = select(PaperCollection).where(
        and_(
            PaperCollection.id == request.collection_id,
            PaperCollection.user_id == current_user.id
        )
    )
    coll_result = await db.execute(coll_stmt)
    if not coll_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    
    # 删除关联
    await db.execute(
        delete(paper_collection_association).where(
            and_(
                paper_collection_association.c.paper_id == request.paper_id,
                paper_collection_association.c.collection_id == request.collection_id
            )
        )
    )
    
    # 更新计数
    await db.execute(
        PaperCollection.__table__.update().where(
            PaperCollection.id == request.collection_id
        ).values(paper_count=func.greatest(PaperCollection.paper_count - 1, 0))
    )
    
    await db.commit()
    await _invalidate_ask_cache_for_collection(current_user.id, int(request.collection_id))
    return {"message": "已从收藏夹移除"}


# ============ PDF 下载 ============

@router.post("/papers/{paper_id}/download-pdf")
async def download_paper_pdf(
    paper_id: int,
    knowledge_base_id: Optional[int] = None,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """下载论文 PDF 并可选添加到知识库"""
    # 获取论文
    stmt = select(Paper).where(
        and_(Paper.id == paper_id, Paper.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    paper = result.scalar_one_or_none()
    
    if not paper:
        raise HTTPException(status_code=404, detail="论文不存在")
    
    if not paper.pdf_url:
        raise HTTPException(status_code=400, detail="该论文没有 PDF 下载链接")
    
    if paper.pdf_downloaded and paper.pdf_path and not knowledge_base_id:
        return {"message": "PDF 已下载", "pdf_path": paper.pdf_path}
    
    if paper.pdf_downloaded and paper.pdf_path and os.path.exists(paper.pdf_path):
        pdf_path = paper.pdf_path
        filename = os.path.basename(pdf_path)
    else:
        pdf_path = _build_paper_pdf_file_path(
            user_id=current_user.id,
            paper_id=int(paper.id),
            title=paper.title,
            ensure_dir=True,
        )
        filename = os.path.basename(pdf_path)

        # 下载 PDF
        service = get_literature_service()
        success = await service.download_pdf(paper.pdf_url, pdf_path)

        if not success:
            raise HTTPException(status_code=500, detail="PDF 下载失败")

        # 更新论文记录
        paper.pdf_path = pdf_path
        paper.pdf_downloaded = True
    
    # 如果指定了知识库，添加到知识库
    if knowledge_base_id:
        kb = await _get_owned_kb_or_404(db, current_user, int(knowledge_base_id))

        doc = Document(
            knowledge_base_id=kb.id,
            filename=filename,
            original_filename=filename,
            file_path=pdf_path,
            file_size=os.path.getsize(pdf_path),
            file_type="pdf",
            mime_type="application/pdf",
            status=DocumentStatus.PENDING.value,
            metadata_={"paper_id": paper.id, "title": paper.title},
        )
        db.add(doc)
        await db.flush()

        link_stmt = select(PaperKnowledgeLink).where(
            and_(
                PaperKnowledgeLink.user_id == current_user.id,
                PaperKnowledgeLink.paper_id == paper.id,
                PaperKnowledgeLink.knowledge_base_id == kb.id,
            )
        )
        link = (await db.execute(link_stmt)).scalar_one_or_none()
        if link is None:
            link = PaperKnowledgeLink(
                user_id=current_user.id,
                paper_id=paper.id,
                knowledge_base_id=kb.id,
                status=KnowledgeLinkStatus.PENDING.value,
            )
            db.add(link)
            await db.flush()

        link.document_id = doc.id
        link.status = KnowledgeLinkStatus.PENDING.value
        link.error_message = None

        paper.knowledge_base_id = kb.id
        paper.document_id = doc.id

        if background_tasks:
            background_tasks.add_task(
                _run_document_processing_for_link,
                link.id,
                doc.id,
                int(kb.chunk_size),
                int(kb.chunk_overlap),
            )
    
    await db.commit()
    if knowledge_base_id:
        await _invalidate_ask_cache_for_scope(
            user_id=current_user.id,
            kb_id=int(knowledge_base_id),
            scope="paper",
            target_id=paper.id,
        )
    
    return {
        "message": "PDF 下载成功",
        "pdf_path": pdf_path,
        "knowledge_base_id": knowledge_base_id,
        "document_id": paper.document_id
    }


@router.get("/papers/{paper_id}/pdf")
async def stream_paper_pdf(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取论文 PDF 文件流（阅读器专用，只读，不触发下载）。"""
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)

    pdf_path = _resolve_local_pdf_path(user_id=current_user.id, paper=paper)
    if not pdf_path:
        raise HTTPException(status_code=404, detail="论文 PDF 不存在，请先下载")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path),
    )


async def process_document_background(doc_id: int, kb_id: int, file_path: str):
    """兼容旧调用：转发到知识库文档处理任务。"""
    from app.api.knowledge import process_document_task
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if not kb:
            logger.warning(f"[Literature API] process_document_background: 知识库不存在 kb={kb_id}")
            return
        chunk_size = int(kb.chunk_size or 500)
        chunk_overlap = int(kb.chunk_overlap or 50)

    await process_document_task(doc_id, chunk_size, chunk_overlap)


# ============ 阅读会话 ============

@router.get("/papers/{paper_id}/reader/session", response_model=ReaderSessionResponse)
async def get_reader_session(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    stmt = select(PaperReadSession).where(
        and_(
            PaperReadSession.user_id == current_user.id,
            PaperReadSession.paper_id == paper.id,
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        session = PaperReadSession(
            user_id=current_user.id,
            paper_id=paper.id,
            page=1,
            zoom="100%",
            scroll_y=0,
            selected_kb_id=paper.knowledge_base_id,
            last_anchor={},
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

    return ReaderSessionResponse(
        page=session.page or 1,
        zoom=session.zoom or "100%",
        scroll_y=session.scroll_y or 0,
        selected_kb_id=session.selected_kb_id,
        last_anchor=session.last_anchor or {},
        updated_at=session.updated_at,
    )


@router.put("/papers/{paper_id}/reader/session", response_model=ReaderSessionResponse)
async def update_reader_session(
    paper_id: int,
    payload: ReaderSessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    stmt = select(PaperReadSession).where(
        and_(
            PaperReadSession.user_id == current_user.id,
            PaperReadSession.paper_id == paper.id,
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        session = PaperReadSession(
            user_id=current_user.id,
            paper_id=paper.id,
        )
        db.add(session)

    session.page = int(payload.page)
    session.zoom = payload.zoom
    session.scroll_y = int(payload.scroll_y)
    session.selected_kb_id = payload.selected_kb_id
    session.last_anchor = payload.last_anchor or {}

    await db.commit()
    await db.refresh(session)

    return ReaderSessionResponse(
        page=session.page,
        zoom=session.zoom,
        scroll_y=session.scroll_y,
        selected_kb_id=session.selected_kb_id,
        last_anchor=session.last_anchor or {},
        updated_at=session.updated_at,
    )


# ============ 批注 ============

@router.get("/papers/{paper_id}/annotations", response_model=List[PaperAnnotationResponse])
async def list_annotations(
    paper_id: int,
    page: Optional[int] = Query(None, ge=1),
    type: Optional[str] = Query(None, description="highlight|note"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_owned_paper_or_404(db, current_user, paper_id)

    stmt = select(PaperAnnotation).where(
        and_(
            PaperAnnotation.user_id == current_user.id,
            PaperAnnotation.paper_id == paper_id,
        )
    )
    if page is not None:
        stmt = stmt.where(PaperAnnotation.page == page)
    if type in {AnnotationType.HIGHLIGHT.value, AnnotationType.NOTE.value}:
        stmt = stmt.where(PaperAnnotation.annotation_type == type)

    stmt = stmt.order_by(PaperAnnotation.created_at.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PaperAnnotationResponse(
            id=row.id,
            user_id=row.user_id,
            paper_id=row.paper_id,
            annotation_type=row.annotation_type,
            page=row.page,
            quote_text=row.quote_text,
            anchor=row.anchor_json or {},
            content=row.content,
            color=row.color or "#f59e0b",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.post("/papers/{paper_id}/annotations", response_model=PaperAnnotationResponse)
async def create_annotation(
    paper_id: int,
    payload: PaperAnnotationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_owned_paper_or_404(db, current_user, paper_id)

    item = PaperAnnotation(
        user_id=current_user.id,
        paper_id=paper_id,
        annotation_type=payload.annotation_type,
        page=payload.page,
        quote_text=payload.quote_text,
        anchor_json=payload.anchor or {},
        content=payload.content,
        color=payload.color,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)

    return PaperAnnotationResponse(
        id=item.id,
        user_id=item.user_id,
        paper_id=item.paper_id,
        annotation_type=item.annotation_type,
        page=item.page,
        quote_text=item.quote_text,
        anchor=item.anchor_json or {},
        content=item.content,
        color=item.color or "#f59e0b",
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.patch("/papers/{paper_id}/annotations/{annotation_id}", response_model=PaperAnnotationResponse)
async def update_annotation(
    paper_id: int,
    annotation_id: int,
    payload: PaperAnnotationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_owned_paper_or_404(db, current_user, paper_id)

    stmt = select(PaperAnnotation).where(
        and_(
            PaperAnnotation.id == annotation_id,
            PaperAnnotation.paper_id == paper_id,
            PaperAnnotation.user_id == current_user.id,
        )
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="批注不存在")

    updates = payload.model_dump(exclude_unset=True)
    if "anchor" in updates:
        item.anchor_json = updates.pop("anchor") or {}
    for key, value in updates.items():
        setattr(item, key, value)

    await db.commit()
    await db.refresh(item)
    return PaperAnnotationResponse(
        id=item.id,
        user_id=item.user_id,
        paper_id=item.paper_id,
        annotation_type=item.annotation_type,
        page=item.page,
        quote_text=item.quote_text,
        anchor=item.anchor_json or {},
        content=item.content,
        color=item.color or "#f59e0b",
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.delete("/papers/{paper_id}/annotations/{annotation_id}")
async def delete_annotation(
    paper_id: int,
    annotation_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_owned_paper_or_404(db, current_user, paper_id)

    stmt = select(PaperAnnotation).where(
        and_(
            PaperAnnotation.id == annotation_id,
            PaperAnnotation.paper_id == paper_id,
            PaperAnnotation.user_id == current_user.id,
        )
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="批注不存在")

    await db.delete(item)
    await db.commit()
    return {"message": "批注已删除"}


# ============ 评论 ============

@router.get("/papers/{paper_id}/comments", response_model=List[PaperCommentResponse])
async def list_comments(
    paper_id: int,
    filter: str = Query("all", pattern="^(all|same_group)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)

    stmt = (
        select(PaperComment)
        .options(selectinload(PaperComment.user))
        .where(
            and_(
                PaperComment.paper_entity_id == entity.id,
                PaperComment.deleted_at.is_(None),
            )
        )
        .order_by(PaperComment.created_at.asc(), PaperComment.id.asc())
    )

    if filter == "same_group":
        same_group_user_ids = await _get_same_group_user_ids(db, current_user.id)
        if not same_group_user_ids:
            return []
        stmt = stmt.where(PaperComment.user_id.in_(list(same_group_user_ids)))

    comments = (await db.execute(stmt)).scalars().all()
    return [_to_comment_response(comment) for comment in comments]


@router.post("/papers/{paper_id}/comments", response_model=PaperCommentResponse)
async def create_comment(
    paper_id: int,
    payload: PaperCommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)

    parent_id = payload.parent_id
    if parent_id is not None:
        parent_stmt = select(PaperComment).where(
            and_(
                PaperComment.id == parent_id,
                PaperComment.paper_entity_id == entity.id,
                PaperComment.deleted_at.is_(None),
            )
        )
        parent_comment = (await db.execute(parent_stmt)).scalar_one_or_none()
        if parent_comment is None:
            raise HTTPException(status_code=404, detail="父评论不存在")
        if parent_comment.parent_id is not None:
            raise HTTPException(status_code=400, detail="仅支持一级回复")

    comment = PaperComment(
        paper_entity_id=entity.id,
        user_id=current_user.id,
        parent_id=parent_id,
        content=payload.content.strip(),
    )
    db.add(comment)
    await db.commit()

    load_stmt = (
        select(PaperComment)
        .options(selectinload(PaperComment.user))
        .where(PaperComment.id == comment.id)
    )
    saved = (await db.execute(load_stmt)).scalar_one()
    return _to_comment_response(saved)


@router.patch("/papers/{paper_id}/comments/{comment_id}", response_model=PaperCommentResponse)
async def update_comment(
    paper_id: int,
    comment_id: int,
    payload: PaperCommentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)

    stmt = (
        select(PaperComment)
        .options(selectinload(PaperComment.user))
        .where(
            and_(
                PaperComment.id == comment_id,
                PaperComment.paper_entity_id == entity.id,
                PaperComment.deleted_at.is_(None),
            )
        )
    )
    comment = (await db.execute(stmt)).scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="评论不存在")

    is_admin = str(getattr(current_user, "role", "") or "").lower() == "admin"
    if comment.user_id != current_user.id and not is_admin:
        raise HTTPException(status_code=403, detail="无权限编辑该评论")

    comment.content = payload.content.strip()
    await db.commit()

    refresh_stmt = (
        select(PaperComment)
        .options(selectinload(PaperComment.user))
        .where(PaperComment.id == comment.id)
    )
    updated = (await db.execute(refresh_stmt)).scalar_one()
    return _to_comment_response(updated)


@router.delete("/papers/{paper_id}/comments/{comment_id}")
async def delete_comment(
    paper_id: int,
    comment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)

    stmt = select(PaperComment).where(
        and_(
            PaperComment.id == comment_id,
            PaperComment.paper_entity_id == entity.id,
            PaperComment.deleted_at.is_(None),
        )
    )
    comment = (await db.execute(stmt)).scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="评论不存在")

    is_admin = str(getattr(current_user, "role", "") or "").lower() == "admin"
    if comment.user_id != current_user.id and not is_admin:
        raise HTTPException(status_code=403, detail="无权限删除该评论")

    comment.deleted_at = datetime.utcnow()
    await db.commit()
    return {"message": "评论已删除"}


# ============ 评分 ============

@router.put("/papers/{paper_id}/rating", response_model=PaperRatingSummary)
async def put_paper_rating(
    paper_id: int,
    payload: PaperRatingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)

    stmt = select(PaperRating).where(
        and_(
            PaperRating.paper_entity_id == entity.id,
            PaperRating.user_id == current_user.id,
        )
    )
    rating = (await db.execute(stmt)).scalar_one_or_none()
    if rating is None:
        rating = PaperRating(
            paper_entity_id=entity.id,
            user_id=current_user.id,
            rating=payload.rating,
        )
        db.add(rating)
    else:
        rating.rating = payload.rating

    # 兼容旧逻辑：同步当前论文记录 rating 字段
    paper.rating = payload.rating

    await db.commit()
    return await _build_rating_summary(db, current_user, entity.id)


@router.get("/papers/{paper_id}/ratings/summary", response_model=PaperRatingSummary)
async def get_rating_summary(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    entity = await _ensure_paper_entity(db, paper)
    await db.commit()
    return await _build_rating_summary(db, current_user, entity.id)


# ============ 入库链路 ============

@router.post("/papers/{paper_id}/add-to-knowledge", response_model=PaperKnowledgeLinkResponse)
async def add_paper_to_knowledge(
    paper_id: int,
    payload: AddPaperToKnowledgeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    kb = await _get_owned_kb_or_404(db, current_user, payload.knowledge_base_id)

    # 确保本地 PDF 可用
    pdf_path = paper.pdf_path
    if not pdf_path or not os.path.exists(pdf_path):
        if not paper.pdf_url:
            raise HTTPException(status_code=400, detail="论文缺少 PDF 文件与下载链接，无法入库")
        pdf_path = _build_paper_pdf_file_path(
            user_id=current_user.id,
            paper_id=int(paper.id),
            title=paper.title,
            ensure_dir=True,
        )
        success = await get_literature_service().download_pdf(paper.pdf_url, pdf_path)
        if not success:
            raise HTTPException(status_code=500, detail="PDF 下载失败，无法加入知识库")
        paper.pdf_downloaded = True
        paper.pdf_path = pdf_path

    original_filename = os.path.basename(pdf_path)
    doc = Document(
        knowledge_base_id=kb.id,
        filename=original_filename,
        original_filename=original_filename,
        file_path=pdf_path,
        file_size=os.path.getsize(pdf_path),
        file_type="pdf",
        mime_type="application/pdf",
        status=DocumentStatus.PENDING.value,
        metadata_={"paper_id": paper.id, "title": paper.title},
    )
    db.add(doc)
    await db.flush()

    link_stmt = select(PaperKnowledgeLink).where(
        and_(
            PaperKnowledgeLink.user_id == current_user.id,
            PaperKnowledgeLink.paper_id == paper.id,
            PaperKnowledgeLink.knowledge_base_id == kb.id,
        )
    )
    link = (await db.execute(link_stmt)).scalar_one_or_none()
    if link is None:
        link = PaperKnowledgeLink(
            user_id=current_user.id,
            paper_id=paper.id,
            knowledge_base_id=kb.id,
            status=KnowledgeLinkStatus.PENDING.value,
        )
        db.add(link)
        await db.flush()

    link.document_id = doc.id
    link.status = KnowledgeLinkStatus.PENDING.value
    link.error_message = None

    paper.knowledge_base_id = kb.id
    paper.document_id = doc.id

    await db.commit()
    await db.refresh(link)
    await _publish_paper_link_status_event(link)

    background_tasks.add_task(
        _run_document_processing_for_link,
        link.id,
        doc.id,
        int(kb.chunk_size),
        int(kb.chunk_overlap),
    )

    await _invalidate_ask_cache_for_scope(
        user_id=current_user.id,
        kb_id=kb.id,
        scope="paper",
        target_id=paper.id,
    )

    coll_stmt = select(paper_collection_association.c.collection_id).where(
        paper_collection_association.c.paper_id == paper.id
    )
    coll_rows = (await db.execute(coll_stmt)).fetchall()
    for row in coll_rows:
        await _invalidate_ask_cache_for_scope(
            user_id=current_user.id,
            kb_id=kb.id,
            scope="collection",
            target_id=int(row[0]),
        )

    return link


@router.get("/papers/{paper_id}/knowledge-links", response_model=List[PaperKnowledgeLinkResponse])
async def list_paper_knowledge_links(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_owned_paper_or_404(db, current_user, paper_id)

    stmt = (
        select(PaperKnowledgeLink)
        .where(
            and_(
                PaperKnowledgeLink.user_id == current_user.id,
                PaperKnowledgeLink.paper_id == paper_id,
            )
        )
        .order_by(PaperKnowledgeLink.updated_at.desc(), PaperKnowledgeLink.id.desc())
    )
    links = (await db.execute(stmt)).scalars().all()

    # 轻量同步：按 document.status 回写 link 状态
    need_commit = False
    changed_link_ids: set[int] = set()
    for link in links:
        if not link.document_id:
            continue
        doc = await db.get(Document, int(link.document_id))
        if not doc:
            continue
        if doc.status == DocumentStatus.COMPLETED.value and link.status != KnowledgeLinkStatus.READY.value:
            link.status = KnowledgeLinkStatus.READY.value
            link.error_message = None
            need_commit = True
            changed_link_ids.add(int(link.id))
        elif doc.status == DocumentStatus.FAILED.value and link.status != KnowledgeLinkStatus.FAILED.value:
            link.status = KnowledgeLinkStatus.FAILED.value
            link.error_message = doc.error_message
            need_commit = True
            changed_link_ids.add(int(link.id))
        elif doc.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value} and link.status != KnowledgeLinkStatus.PROCESSING.value:
            link.status = KnowledgeLinkStatus.PROCESSING.value
            need_commit = True
            changed_link_ids.add(int(link.id))

    if need_commit:
        await db.commit()
        for link in links:
            if int(link.id) in changed_link_ids:
                await _publish_paper_link_status_event(link)

    return links


@router.get("/events/stream")
async def stream_literature_status_events(
    request: Request,
    paper_id: Optional[int] = Query(default=None, ge=1, description="可选：仅订阅指定论文"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """文献模块状态事件流（SSE）。"""
    if paper_id is not None:
        await _get_owned_paper_or_404(db, current_user, int(paper_id))

    channel = build_status_channel_for_user(int(current_user.id))

    async def event_generator():
        yield _sse_payload(
            "connected",
            {
                "scope": "literature",
                "user_id": int(current_user.id),
                "paper_id": int(paper_id) if paper_id is not None else None,
                "ts": datetime.utcnow().isoformat(),
            },
        )
        async for item in iter_status_events(channel):
            if await request.is_disconnected():
                break

            event = str(item.get("event") or "").strip()
            data = item.get("data")

            if event == "heartbeat":
                yield _sse_payload("heartbeat", data)
                continue

            if event != "paper_link_status" or not isinstance(data, dict):
                continue

            event_paper_id = int(data.get("paper_id") or 0)
            if paper_id is not None and event_paper_id != int(paper_id):
                continue

            yield _sse_payload("paper_link_status", data)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ask/sessions", response_model=List[LiteratureAskSessionSchema])
async def list_literature_ask_sessions(
    scope: Optional[AskScope] = Query(default=None),
    paper_id: Optional[int] = Query(default=None, ge=1),
    collection_id: Optional[int] = Query(default=None, ge=1),
    knowledge_base_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if paper_id is not None:
        await _get_owned_paper_or_404(db, current_user, int(paper_id))
    if collection_id is not None:
        await _get_owned_collection_or_404(db, current_user, int(collection_id))
    if knowledge_base_id is not None:
        await _get_owned_kb_or_404(db, current_user, int(knowledge_base_id))

    stmt = select(LiteratureQASession).where(LiteratureQASession.user_id == current_user.id)
    if scope is not None:
        stmt = stmt.where(LiteratureQASession.scope == scope.value)
    if paper_id is not None:
        stmt = stmt.where(LiteratureQASession.paper_id == int(paper_id))
    if collection_id is not None:
        stmt = stmt.where(LiteratureQASession.collection_id == int(collection_id))
    if knowledge_base_id is not None:
        stmt = stmt.where(LiteratureQASession.knowledge_base_id == int(knowledge_base_id))

    stmt = (
        stmt.order_by(LiteratureQASession.updated_at.desc(), LiteratureQASession.id.desc())
        .offset(offset)
        .limit(limit)
    )
    sessions = (await db.execute(stmt)).scalars().all()
    return list(sessions)


@router.get(
    "/ask/sessions/{session_id}/messages",
    response_model=List[LiteratureAskMessageSchema],
)
async def list_literature_ask_messages(
    session_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session_stmt = select(LiteratureQASession).where(
        and_(
            LiteratureQASession.id == int(session_id),
            LiteratureQASession.user_id == current_user.id,
        )
    )
    session = (await db.execute(session_stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="问答会话不存在")

    msg_stmt = (
        select(LiteratureQAMessage)
        .where(LiteratureQAMessage.session_id == int(session_id))
        .order_by(LiteratureQAMessage.created_at.asc(), LiteratureQAMessage.id.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(msg_stmt)).scalars().all()
    return [_to_ask_message_response(row) for row in rows]


# ============ 询问（SSE） ============

@router.post("/ask")
async def literature_ask(
    payload: LiteratureAskRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ask_mode = str(payload.mode or "agentic").strip().lower()
    if ask_mode not in {"agentic", "classic"}:
        raise HTTPException(status_code=400, detail="mode 仅支持 agentic 或 classic")

    scope = payload.scope
    paper: Optional[Paper] = None
    paper_pdf_path: Optional[str] = None
    if scope == AskScope.PAPER.value:
        if payload.paper_id is None:
            raise HTTPException(status_code=400, detail="scope=paper 时必须提供 paper_id")
        paper = await _get_owned_paper_or_404(db, current_user, int(payload.paper_id))
        paper_pdf_path = _resolve_local_pdf_path(int(current_user.id), paper)
        target_id = paper.id
        paper_ids = [paper.id]
    else:
        if payload.collection_id is None:
            raise HTTPException(status_code=400, detail="scope=collection 时必须提供 collection_id")
        target_id = int(payload.collection_id)
        paper_ids = await _resolve_collection_paper_ids(db, current_user, target_id)
        if not paper_ids:
            raise _knowledge_not_ready_error(
                {
                    "scope": "collection",
                    "collection_id": target_id,
                    "missing_paper_ids": [],
                    "not_ready": [],
                    "message": "当前收藏夹没有论文，无法提问。",
                }
            )

    kb = await _get_owned_kb_or_404(db, current_user, int(payload.knowledge_base_id))
    ready_links, ready_details = await _retrieve_scope_ready_links(
        db,
        user_id=current_user.id,
        kb_id=kb.id,
        paper_ids=paper_ids,
    )
    allow_agentic_pdf_only = (
        ask_mode == "agentic"
        and scope == AskScope.PAPER.value
        and paper is not None
        and bool(paper_pdf_path)
    )

    if not ready_links and not allow_agentic_pdf_only:
        ready_details.update(
            {
                "scope": scope,
                "knowledge_base_id": kb.id,
                "paper_ids": paper_ids,
            }
        )
        raise _knowledge_not_ready_error(ready_details)

    document_ids = sorted({int(link.document_id) for link in ready_links if link.document_id})
    if not document_ids and not allow_agentic_pdf_only:
        ready_details.update(
            {
                "scope": scope,
                "knowledge_base_id": kb.id,
                "paper_ids": paper_ids,
                "message": "已入库文档缺失，建议重新入库处理。",
            }
        )
        raise _knowledge_not_ready_error(ready_details)

    if payload.session_id is not None:
        session_stmt = select(LiteratureQASession).where(
            and_(
                LiteratureQASession.id == int(payload.session_id),
                LiteratureQASession.user_id == current_user.id,
            )
        )
        session = (await db.execute(session_stmt)).scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="问答会话不存在")
        if (
            session.scope != scope
            or int(session.knowledge_base_id) != int(kb.id)
            or int(session.paper_id or 0) != (target_id if scope == AskScope.PAPER.value else 0)
            or int(session.collection_id or 0) != (target_id if scope == AskScope.COLLECTION.value else 0)
        ):
            raise HTTPException(status_code=400, detail="会话与当前提问范围不一致")
    else:
        session = LiteratureQASession(
            user_id=current_user.id,
            scope=scope,
            paper_id=target_id if scope == AskScope.PAPER.value else None,
            collection_id=target_id if scope == AskScope.COLLECTION.value else None,
            knowledge_base_id=kb.id,
            title=payload.question.strip()[:80],
        )
        db.add(session)
        await db.flush()

    user_message = LiteratureQAMessage(
        session_id=session.id,
        role="user",
        content=payload.question.strip(),
        sources=[],
    )
    db.add(user_message)
    session.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user_message)

    cache_key = _ask_cache_key(
        user_id=current_user.id,
        kb_id=kb.id,
        scope=scope,
        target_id=target_id,
        question=payload.question,
        mode=ask_mode,
    )
    cached_payload = await _ask_cache_get(cache_key)
    if cached_payload and isinstance(cached_payload, dict):
        cached_answer = str(cached_payload.get("answer") or "").strip()
        cached_sources = cached_payload.get("sources") or []
        assistant = LiteratureQAMessage(
            session_id=session.id,
            role="assistant",
            content=cached_answer,
            sources=cached_sources,
        )
        db.add(assistant)
        session.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(assistant)

        async def cached_stream():
            yield _sse_payload("start", {"session_id": session.id, "cache_hit": True, "mode": ask_mode})
            step = 40
            for idx in range(0, len(cached_answer), step):
                yield _sse_payload("token", {"text": cached_answer[idx: idx + step]})
            yield _sse_payload("sources", cached_sources)
            yield _sse_payload(
                "done",
                {
                    "session_id": session.id,
                    "message_id": assistant.id,
                    "cache_hit": True,
                    "mode": ask_mode,
                },
            )

        return StreamingResponse(
            cached_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if ask_mode == "agentic":
        history_stmt = (
            select(LiteratureQAMessage)
            .where(
                and_(
                    LiteratureQAMessage.session_id == session.id,
                    LiteratureQAMessage.id != user_message.id,
                )
            )
            .order_by(LiteratureQAMessage.created_at.desc())
            .limit(10)
        )
        history_rows = list((await db.execute(history_stmt)).scalars().all())
        history_rows.reverse()

        agent_messages: List[Dict[str, str]] = []
        for row in history_rows:
            if row.role not in {"user", "assistant"}:
                continue
            agent_messages.append({"role": row.role, "content": row.content})
        agent_messages.append({"role": "user", "content": payload.question.strip()})

        async def agentic_stream():
            answer = ""
            saved_message_id: Optional[int] = None
            sources_by_idx: Dict[int, Dict[str, Any]] = {}

            def merge_sources(rows: Sequence[Dict[str, Any]]) -> None:
                for source in rows:
                    idx = _to_int(source.get("idx"))
                    if idx is None or idx <= 0:
                        continue
                    existing = sources_by_idx.get(int(idx))
                    if existing is None:
                        sources_by_idx[int(idx)] = dict(source)
                        continue

                    merged = dict(existing)
                    for key in (
                        "chunk_id",
                        "document_id",
                        "document_name",
                        "page",
                        "page_source",
                        "section_title",
                        "section_type",
                        "score",
                        "score_source",
                        "chunk_index",
                    ):
                        if merged.get(key) in (None, "", "unknown") and source.get(key) not in (None, ""):
                            merged[key] = source.get(key)

                    existing_snippet = str(merged.get("snippet") or "")
                    incoming_snippet = str(source.get("snippet") or "")
                    if len(incoming_snippet) > len(existing_snippet):
                        merged["snippet"] = incoming_snippet

                    existing_content = str(merged.get("content") or "")
                    incoming_content = str(source.get("content") or "")
                    if len(incoming_content) > len(existing_content):
                        merged["content"] = incoming_content

                    sources_by_idx[int(idx)] = merged
            try:
                yield _sse_payload(
                    "start",
                    {
                        "session_id": session.id,
                        "knowledge_base_id": kb.id,
                        "scope": scope,
                        "cache_hit": False,
                        "mode": "agentic",
                    },
                )

                tool_registry, allowed_tool_names = await _build_literature_agent_tool_registry(
                    db=db,
                    user_id=int(current_user.id),
                    knowledge_base_id=int(kb.id),
                    knowledge_base_name=str(kb.name or f"KB#{kb.id}"),
                    document_ids=document_ids,
                    paper_id=int(paper.id) if paper is not None else None,
                    paper_title=str(paper.title or "") if paper is not None else None,
                    paper_pdf_path=paper_pdf_path,
                )
                if not allowed_tool_names:
                    raise RuntimeError("Agent 工具初始化失败：可用工具为空")

                llm_service = await get_llm_service()
                runtime_context = AgentRuntimeContext(
                    user_id=int(current_user.id),
                    channel="literature",
                    conversation_id=int(session.id),
                )
                agent = LiteratureAskAgentCore(
                    llm_service=llm_service,
                    tool_registry=tool_registry,
                    allowed_tool_names=allowed_tool_names,
                    max_iterations=_resolve_literature_agent_max_iterations(),
                    runtime_context=runtime_context,
                )

                async for event in agent.run(agent_messages, stream=True):
                    event_type = str(event.get("type") or "").strip().lower()
                    event_data = event.get("data")
                    if event_type == "observation" and isinstance(event_data, dict):
                        if str(event_data.get("tool") or "").strip() in {"knowledge_search", "paper_read"}:
                            payload_data = event_data.get("data")
                            normalized = _normalize_agent_source_rows(
                                payload_data.get("results") if isinstance(payload_data, dict) else None
                            )
                            if normalized:
                                merge_sources(normalized)
                    elif event_type == "answer":
                        answer = str(event_data or "").strip()
                    elif event_type == "done" and isinstance(event_data, dict):
                        done_answer = str(event_data.get("answer") or "").strip()
                        if done_answer:
                            answer = done_answer

                if not answer:
                    answer = "无法从当前资料中提取到可回答的信息。"

                latest_sources = [sources_by_idx[key] for key in sorted(sources_by_idx.keys())]
                if not latest_sources:
                    fallback_sources = await _retrieve_rag_sources(
                        db,
                        knowledge_base_id=kb.id,
                        document_ids=document_ids,
                        question=payload.question,
                        limit=6,
                    )
                    latest_sources = _normalize_agent_source_rows(fallback_sources)

                public_sources = _build_public_sources_from_rows(latest_sources)

                await _ask_cache_set(
                    cache_key,
                    {
                        "answer": answer,
                        "sources": public_sources,
                    },
                    ttl_seconds=ASK_CACHE_TTL_SECONDS,
                )

                async with async_session_factory() as save_db:
                    assistant = LiteratureQAMessage(
                        session_id=session.id,
                        role="assistant",
                        content=answer,
                        sources=public_sources,
                    )
                    save_db.add(assistant)
                    session_row = await save_db.get(LiteratureQASession, session.id)
                    if session_row:
                        session_row.updated_at = datetime.utcnow()
                    await save_db.commit()
                    await save_db.refresh(assistant)
                    saved_message_id = assistant.id

                step = 40
                for idx in range(0, len(answer), step):
                    yield _sse_payload("token", {"text": answer[idx: idx + step]})
                yield _sse_payload("sources", public_sources)
                yield _sse_payload(
                    "done",
                    {
                        "session_id": session.id,
                        "message_id": saved_message_id,
                        "cache_hit": False,
                        "mode": "agentic",
                    },
                )
            except Exception as exc:
                logger.exception(f"[Literature Ask] Agentic 询问失败: {exc}")
                yield _sse_payload("error", {"message": str(exc)})

        return StreamingResponse(
            agentic_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    sources = await _retrieve_rag_sources(
        db,
        knowledge_base_id=kb.id,
        document_ids=document_ids,
        question=payload.question,
        limit=8,
    )
    if not sources:
        fallback_stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                Document.original_filename,
                Document.file_path,
                DocumentChunk.content,
                DocumentChunk.chunk_index,
                DocumentChunk.section_type,
                DocumentChunk.section_title,
                DocumentChunk.metadata_,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                and_(
                    DocumentChunk.knowledge_base_id == kb.id,
                    DocumentChunk.document_id.in_(document_ids),
                )
            )
            .order_by(DocumentChunk.id.desc())
            .limit(6)
        )
        fallback_rows = (await db.execute(fallback_stmt)).fetchall()
        for idx, row in enumerate(fallback_rows, start=1):
            metadata = getattr(row, "metadata_", None)
            content = (getattr(row, "content", "") or "").strip()
            page = _extract_page_from_metadata(metadata)
            page_source = "metadata" if page is not None else "unknown"
            if page is None:
                ratio = _extract_position_ratio_from_metadata(metadata)
                if ratio is not None:
                    page_count = await _get_pdf_page_count(getattr(row, "file_path", None))
                    if page_count and page_count > 0:
                        page = min(page_count, max(1, int(round((page_count - 1) * ratio + 1))))
                        page_source = "estimated"
            section_title = _clean_section_title(getattr(row, "section_title", None))
            section_type = getattr(row, "section_type", None)
            if not isinstance(section_type, str):
                section_type = None
            else:
                section_type = section_type.strip().lower() or None
            if section_title is None or section_type is None:
                meta_title, meta_type = _extract_section_info(metadata)
                if section_title is None:
                    section_title = meta_title
                if section_type is None:
                    section_type = meta_type
            sources.append(
                {
                    "idx": idx,
                    "chunk_id": int(getattr(row, "id")),
                    "document_id": int(getattr(row, "document_id")),
                    "document_name": getattr(row, "original_filename") or "未知文档",
                    "page": page,
                    "page_source": page_source,
                    "section_title": section_title,
                    "section_type": section_type,
                    "snippet": content[:240],
                    "content": content[:1600],
                    "chunk_index": int(getattr(row, "chunk_index") or 0),
                    "score": None,
                    "score_source": "fallback",
                }
            )

    history_stmt = (
        select(LiteratureQAMessage)
        .where(
            and_(
                LiteratureQAMessage.session_id == session.id,
                LiteratureQAMessage.id != user_message.id,
            )
        )
        .order_by(LiteratureQAMessage.created_at.desc())
        .limit(10)
    )
    history_rows = list((await db.execute(history_stmt)).scalars().all())
    history_rows.reverse()

    messages = []
    for row in history_rows:
        if row.role not in {"user", "assistant"}:
            continue
        messages.append({"role": row.role, "content": row.content})

    context_blocks = []
    for source in sources:
        page_text = str(source["page"]) if source.get("page") is not None else "未知"
        context_blocks.append(
            f"[{source['idx']}] 文档: {source['document_name']} | 页码: {page_text}\n{source['content']}"
        )

    joined_context = "\n\n".join(context_blocks)
    enriched_user_content = (
        f"问题：{payload.question.strip()}\n\n"
        f"请仅基于以下检索片段回答。\n"
        f"若证据不足，请明确说明“无法从当前资料确定”。\n\n"
        f"检索片段：\n{joined_context}"
    )
    messages.append({"role": "user", "content": enriched_user_content})

    system_prompt = (
        "你是论文阅读问答助手。"
        "必须基于提供的检索片段回答，不得编造事实。"
        "回答尽量简洁准确，引用时使用[序号]标注。"
    )

    llm_service = await get_llm_service()
    public_sources = [
        {
            "idx": source.get("idx"),
            "chunk_id": source["chunk_id"],
            "document_id": source["document_id"],
            "document_name": source["document_name"],
            "page": source.get("page"),
            "page_source": source.get("page_source"),
            "section_title": source.get("section_title"),
            "section_type": source.get("section_type"),
            "snippet": source["snippet"],
            "score": source.get("score"),
            "score_source": source.get("score_source"),
        }
        for source in sources
    ]

    async def stream():
        chunks: List[str] = []
        saved_message_id: Optional[int] = None
        try:
            yield _sse_payload(
                "start",
                {
                    "session_id": session.id,
                    "knowledge_base_id": kb.id,
                    "scope": scope,
                    "cache_hit": False,
                    "mode": "classic",
                },
            )

            async for token in llm_service.chat_stream(messages=messages, system_prompt=system_prompt):
                if token:
                    chunks.append(token)
                    yield _sse_payload("token", {"text": token})

            answer = "".join(chunks).strip()
            if not answer:
                answer = "无法从当前资料中提取到可回答的信息。"

            await _ask_cache_set(
                cache_key,
                {
                    "answer": answer,
                    "sources": public_sources,
                },
                ttl_seconds=ASK_CACHE_TTL_SECONDS,
            )

            async with async_session_factory() as save_db:
                assistant = LiteratureQAMessage(
                    session_id=session.id,
                    role="assistant",
                    content=answer,
                    sources=public_sources,
                )
                save_db.add(assistant)
                session_row = await save_db.get(LiteratureQASession, session.id)
                if session_row:
                    session_row.updated_at = datetime.utcnow()
                await save_db.commit()
                await save_db.refresh(assistant)
                saved_message_id = assistant.id

            yield _sse_payload("sources", public_sources)
            yield _sse_payload(
                "done",
                {
                    "session_id": session.id,
                    "message_id": saved_message_id,
                    "cache_hit": False,
                    "mode": "classic",
                },
            )
        except Exception as exc:
            logger.exception(f"[Literature Ask] 询问失败: {exc}")
            yield _sse_payload("error", {"message": str(exc)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============ 初始化默认收藏夹 ============

@router.post("/init")
async def init_user_literature(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """初始化用户的文献管理（创建默认收藏夹）"""
    # 预定义的收藏夹配置
    default_collection_configs = [
        ("所有论文", "所有保存的论文", "#3b82f6", "folder", "default", True),
        ("待读", "待阅读的论文", "#f59e0b", "clock", "reading_list", False),
        ("已读", "已阅读的论文", "#10b981", "check", "reading_list", False),
        ("收藏", "重要论文", "#ef4444", "star", "custom", False),
    ]
    
    # 一次性查询所有已存在的收藏夹名称
    existing_result = await db.execute(
        select(PaperCollection.name).where(
            PaperCollection.user_id == current_user.id
        )
    )
    existing_names = set(row[0] for row in existing_result.fetchall())
    
    # 如果已有所有默认收藏夹，直接返回
    default_names = set(config[0] for config in default_collection_configs)
    if default_names.issubset(existing_names):
        return {"message": "已初始化"}
    
    # 只创建不存在的收藏夹
    created_count = 0
    for name, description, color, icon, coll_type, is_default in default_collection_configs:
        if name not in existing_names:
            new_coll = PaperCollection(
                user_id=current_user.id,
                name=name,
                description=description,
                color=color,
                icon=icon,
                collection_type=coll_type,
                is_default=is_default
            )
            db.add(new_coll)
            created_count += 1
    
    if created_count > 0:
        try:
            await db.commit()
            return {"message": f"初始化成功，创建了 {created_count} 个收藏夹"}
        except Exception as e:
            await db.rollback()
            logger.warning(f"[Literature API] 初始化时发生冲突: {e}")
            return {"message": "已初始化"}
    
    return {"message": "已初始化"}
