"""
文献管理 API 路由
"""
import base64
import hashlib
import json
import os
import re
import time
import uuid
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse, unquote
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, BackgroundTasks, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete, distinct, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from loguru import logger
from pydantic import BaseModel, Field

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional at runtime
    BeautifulSoup = None

from app.config import settings
from app.core.database import async_session_factory, get_db
from app.core.security import get_current_user, get_current_user_for_stream
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
    PaperReaderPageCache,
    PaperReaderPlanCache,
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
    ImportPaperByLinkRequest,
    ImportPaperByLinkResponse,
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
    ReaderGenerativePlanRequest,
    ReaderGenerativePlanResponse,
    ReaderExperiencePlanRequest,
    ReaderExperiencePlanResponse,
    ReaderExperienceBlockRewriteRequest,
    ReaderExperienceBlockRewriteResponse,
    ReaderExperienceV2Response,
    ReaderWorkbenchV2Response,
    ReaderGenerativePrefetchRequest,
    ReaderGenerativePrefetchResponse,
    ReaderGenerativeRequest,
    ReaderComposePrefetchRequest,
    ReaderComposePrefetchResponse,
    ReaderComposeFetchResponse,
    ReaderComposeRequest,
    ReaderComposeReviewAutoPatchRequest,
    ReaderComposeReviewAutoPatchResponse,
    ReaderComposeReviewDiagnostic,
    ReaderComposeReviewImportRequest,
    ReaderComposeReviewObservationRequest,
    ReaderComposeReviewPatchRequest,
    ReaderComposeReviewPublishRequest,
    ReaderComposeReviewPublishResponse,
    ReaderComposeReviewSessionRequest,
    ReaderComposeReviewSnapshot,
    ReaderAdjacentPageContext,
    ReaderExperienceBlockExplainRequest,
    ReaderExperienceBlockExplainTurn,
    ReaderInlineQueryRequest,
    ReaderNodeActionRequest,
    ReaderNodeActionResponse,
    ReaderPageGrounding,
    ExperienceSessionV2ContextCarry,
    ExperienceSessionV2Iteration,
    ExperienceSessionV2NarrativeBrief,
    ExperienceSessionV2ArtifactDraft,
    ExperienceSessionV2ArtifactDraftNode,
    ExperienceSessionV2ArtifactDraftResourceRequest,
    ExperienceSessionV2,
    ReadingDossierV2,
    ReadingDossierV2AdjacentPageRow,
    PageArtifactV2,
    PageArtifactV2ReadingBlock,
    PageArtifactV2AuthoredPlanInput,
    _looks_like_legacy_adjacent_payload_stuffing,
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
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
from app.services.generative_reader_agent_runtime import get_generative_reader_agent_runtime
from app.services.literature_service import PaperResult, get_literature_service
from app.services.literature_reader_compose_service import GROUNDED_FIGURE_ASSET_VERSION, get_literature_reader_compose_service
from app.services.literature_reader_service import get_literature_reader_service
from app.services.llm_service import get_llm_service
from app.services.render_pipeline_contract import RenderPipelineContractError
from app.services.react_agent import AgentCore, AgentRuntimeContext
from app.services.agent_tools_impl.registry import ToolBase, ToolRegistry, ToolResult
from app.services.reader_single_agent_controller import parse_json_dict_from_model_text
from app.services.document_status_guard_service import (
    build_timeout_error_message,
    is_stale_processing_status,
)
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
_READER_PLAN_CACHE_NAMESPACE_VERSION = "v33"
_READING_DOSSIER_V2_NAMESPACE = "lit:reading_dossier:v2"
_READER_FIGURE_ASSET_PATH_RE = re.compile(
    r"^/api/v1/literature/reader/figure-assets/(?P<paper_id>\d+)/(?P<page>\d+)/(?P<asset_id>[0-9A-Za-z_.-]{1,96})$"
)

_PAGE_ARTIFACT_V2_SUPPORTED_NODE_KINDS = {
    "heading",
    "paragraph",
    "original_excerpt",
    "authored_explanation",
    "figure_slot",
    "table_slot",
    "equation_slot",
    "media_slot",
    "aside_content",
    "term_annotation",
    "external_resource",
}
_ARTIFACT_DRAFT_V2_SUPPORTED_NODE_KINDS = {
    "heading",
    "paragraph",
    "original_excerpt",
    "figure_slot",
    "table_slot",
    "equation_slot",
    "aside",
    "term_note",
    "external_resource",
}
_EXPERIENCE_V2_BLOCK_REWRITE_SUPPORTED_SEGMENT_KINDS = {
    "heading",
    "paragraph",
    "authored_explanation",
    "aside_content",
    "term_annotation",
}

_ADJACENT_PAGE_STRUCTURED_PRIMARY_ATTEMPTS = 3
_EXPERIENCE_V2_RUNTIME_VERSION = "artifactdraft_v10_opening_bridges_prompt_only"
_EXPERIENCE_V2_ARTIFACT_DRAFT_MAX_RETRIEVAL_ROUNDS = 2
_EXPERIENCE_SESSION_V2_NARRATIVE_BRIEF_REQUIRED_FIELDS = {
    "focus_page",
    "current_page_main_arc",
    "continuity_resolutions",
    "required_media_refs",
    "content_strategy",
    "presentation_strategy",
}
_EXPERIENCE_SESSION_V2_ARTIFACT_DRAFT_REQUIRED_FIELDS = {
    "focus_page",
    "template_hint",
    "layout_recipe",
    "presentation_mode",
    "nodes",
    "resource_requests",
}

def _build_mojibake_variants(text: str) -> Set[str]:
    raw = str(text or "").encode("utf-8")
    replaced = raw.decode("gbk", errors="replace")
    ignored = raw.decode("gbk", errors="ignore")
    replacement_char = chr(0xFFFD)
    variants = {
        replaced,
        ignored,
        replaced.replace(replacement_char, "?"),
    }
    return {item for item in variants if item and item != text}


_CANONICAL_COLLECTION_NAMES = ("所有论文", "待读", "已读", "收藏")
_CANONICAL_COLLECTION_DESCRIPTIONS = (
    "所有保存的论文",
    "待阅读的论文",
    "已阅读的论文",
    "重要论文",
)

_MOJIBAKE_COLLECTION_NAME_MAP: Dict[str, str] = {}
for _name in _CANONICAL_COLLECTION_NAMES:
    for _token in _build_mojibake_variants(_name):
        _MOJIBAKE_COLLECTION_NAME_MAP[_token] = _name

_MOJIBAKE_COLLECTION_DESCRIPTION_MAP: Dict[str, str] = {}
for _desc in _CANONICAL_COLLECTION_DESCRIPTIONS:
    for _token in _build_mojibake_variants(_desc):
        _MOJIBAKE_COLLECTION_DESCRIPTION_MAP[_token] = _desc

_DEFAULT_COLLECTION_CANONICAL_NAMES = {"所有论文", "待读", "已读", "收藏"}


def _normalize_collection_name(value: Optional[str]) -> str:
    token = str(value or "").strip()
    return _MOJIBAKE_COLLECTION_NAME_MAP.get(token, token)


def _normalize_collection_description(value: Optional[str]) -> str:
    token = str(value or "")
    return _MOJIBAKE_COLLECTION_DESCRIPTION_MAP.get(token, token)


async def _merge_collection_memberships(
    db: AsyncSession,
    source_collection_id: int,
    target_collection_id: int,
) -> None:
    if int(source_collection_id) == int(target_collection_id):
        return

    source_rows = await db.execute(
        select(paper_collection_association.c.paper_id).where(
            paper_collection_association.c.collection_id == int(source_collection_id)
        )
    )
    source_paper_ids = [int(row[0]) for row in source_rows.fetchall()]
    for paper_id in source_paper_ids:
        exists_result = await db.execute(
            select(paper_collection_association.c.paper_id).where(
                and_(
                    paper_collection_association.c.paper_id == int(paper_id),
                    paper_collection_association.c.collection_id == int(target_collection_id),
                )
            )
        )
        if exists_result.first() is None:
            await db.execute(
                paper_collection_association.insert().values(
                    paper_id=int(paper_id),
                    collection_id=int(target_collection_id),
                )
            )

    await db.execute(
        delete(paper_collection_association).where(
            paper_collection_association.c.collection_id == int(source_collection_id)
        )
    )


async def _repair_user_collection_mojibake(
    db: AsyncSession,
    user_id: int,
) -> bool:
    rows = await db.execute(
        select(PaperCollection)
        .where(PaperCollection.user_id == int(user_id))
        .order_by(PaperCollection.id.asc())
    )
    user_collections = list(rows.scalars().all())
    if not user_collections:
        return False

    changed = False
    for coll in user_collections:
        normalized_name = _normalize_collection_name(getattr(coll, "name", None))
        if normalized_name and str(coll.name or "") != normalized_name:
            coll.name = normalized_name
            changed = True

        normalized_description = _normalize_collection_description(
            getattr(coll, "description", None)
        )
        if str(coll.description or "") != normalized_description:
            coll.description = normalized_description
            changed = True

    grouped_by_name: Dict[str, List[PaperCollection]] = {}
    for coll in user_collections:
        grouped_by_name.setdefault(str(coll.name or ""), []).append(coll)

    for canonical_name in _DEFAULT_COLLECTION_CANONICAL_NAMES:
        same_name_collections = list(grouped_by_name.get(canonical_name) or [])
        if len(same_name_collections) <= 1:
            continue

        if canonical_name == "所有论文":
            same_name_collections.sort(
                key=lambda item: (0 if bool(item.is_default) else 1, int(item.id))
            )
        else:
            same_name_collections.sort(key=lambda item: int(item.id))

        keep = same_name_collections[0]
        if canonical_name == "所有论文" and not bool(keep.is_default):
            keep.is_default = True
            changed = True

        for dup in same_name_collections[1:]:
            await _merge_collection_memberships(
                db=db,
                source_collection_id=int(dup.id),
                target_collection_id=int(keep.id),
            )
            await db.delete(dup)
            changed = True

    if not changed:
        return False

    await db.flush()

    after_rows = await db.execute(
        select(PaperCollection).where(PaperCollection.user_id == int(user_id))
    )
    after_collections = list(after_rows.scalars().all())
    for coll in after_collections:
        count_result = await db.execute(
            select(func.count())
            .select_from(paper_collection_association)
            .where(paper_collection_association.c.collection_id == int(coll.id))
        )
        real_count = int(count_result.scalar() or 0)
        if int(coll.paper_count or 0) != real_count:
            coll.paper_count = real_count

    return True


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
        "pdf_url": _resolve_pdf_download_url(paper),
        "arxiv_url": paper.arxiv_url,
        "pdf_path": paper.pdf_path,
        "pdf_downloaded": paper.pdf_downloaded or False,
        "knowledge_base_id": None,
        "document_id": None,
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
GENERATIVE_PLAN_CACHE_TTL_SECONDS = 3600
_generative_plan_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
EXPERIENCE_PLAN_CACHE_TTL_SECONDS = 3600
_experience_plan_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS = 3600
_experience_session_v2_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
_experience_session_v2_fast_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS = 3600
_page_artifact_v2_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
_page_artifact_v2_fast_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
ADJACENT_PAGE_STRUCTURED_V2_CACHE_TTL_SECONDS = 3600
_adjacent_page_structured_v2_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
GENERATIVE_PLAN_CACHE_KIND = "generative_plan"
EXPERIENCE_PLAN_CACHE_KIND = "experience_plan"
EXPERIENCE_SESSION_V2_CACHE_KIND = "experience_session_v2"
EXPERIENCE_SESSION_V2_CACHE_NAMESPACE = "lit:experience_session:v2"
EXPERIENCE_SESSION_V2_FAST_CACHE_NAMESPACE = "lit:experience_session:v2:fast"
PAGE_ARTIFACT_V2_CACHE_KIND = "page_artifact_v2"
PAGE_ARTIFACT_V2_CACHE_NAMESPACE = "lit:page_artifact:v2"
PAGE_ARTIFACT_V2_FAST_CACHE_NAMESPACE = "lit:page_artifact:v2:fast"
ADJACENT_PAGE_STRUCTURED_V2_CACHE_KIND = "adjacent_page_structured_v2"
ADJACENT_PAGE_STRUCTURED_V2_CACHE_NAMESPACE = "lit:adjacent_page_structured:v2"
PAGE_COUNT_CACHE_TTL_SECONDS = 3600
_pdf_page_count_cache: Dict[str, tuple[float, int]] = {}


async def _plan_cache_db_get(cache_key: str, plan_kind: str) -> tuple[Optional[Dict[str, Any]], Optional[datetime]]:
    try:
        async with async_session_factory() as session:
            stmt = (
                select(PaperReaderPlanCache)
                .where(
                    and_(
                        PaperReaderPlanCache.cache_key == cache_key,
                        PaperReaderPlanCache.plan_kind == plan_kind,
                    )
                )
                .order_by(PaperReaderPlanCache.updated_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if not record:
                return None, None
            expires_at = record.expires_at
            if expires_at and expires_at <= datetime.utcnow():
                await session.delete(record)
                await session.commit()
                return None, None
            return _jsonable_dict(record.payload_json or {}), expires_at
    except Exception as exc:
        logger.warning(f"[Literature PlanCache] DB read failed cache_key={cache_key}: {exc}")
        return None, None


async def _plan_cache_db_get_latest_by_compose_signature(
    *,
    plan_kind: str,
    user_id: int,
    paper_id: int,
    page: int,
    compose_source_signature: str,
    cache_key_like: str,
) -> tuple[Optional[Dict[str, Any]], Optional[datetime], Optional[str]]:
    compose_source_signature = str(compose_source_signature or "").strip()
    cache_key_like = str(cache_key_like or "").strip()
    if not compose_source_signature or not cache_key_like:
        return None, None, None
    try:
        async with async_session_factory() as session:
            stmt = (
                select(PaperReaderPlanCache)
                .where(
                    and_(
                        PaperReaderPlanCache.plan_kind == str(plan_kind or "").strip(),
                        PaperReaderPlanCache.user_id == int(user_id),
                        PaperReaderPlanCache.paper_id == int(paper_id),
                        PaperReaderPlanCache.page == int(page),
                        PaperReaderPlanCache.compose_source_signature == compose_source_signature,
                        PaperReaderPlanCache.cache_key.like(cache_key_like),
                    )
                )
                .order_by(PaperReaderPlanCache.updated_at.desc())
            )
            result = await session.execute(stmt)
            records = list(result.scalars().all())
            if not records:
                return None, None, None
            expired_found = False
            now = datetime.utcnow()
            for record in records:
                expires_at = record.expires_at
                if expires_at and expires_at <= now:
                    await session.delete(record)
                    expired_found = True
                    continue
                if expired_found:
                    await session.commit()
                return _jsonable_dict(record.payload_json or {}), expires_at, str(record.cache_key or "").strip()
            if expired_found:
                await session.commit()
            return None, None, None
    except Exception as exc:
        logger.warning(
            f"[Literature PlanCache] compose-signature DB read failed kind={plan_kind} user_id={user_id} "
            f"paper_id={paper_id} page={page}: {exc}"
        )
        return None, None, None


async def _plan_cache_db_get_latest_by_cache_key_like(
    *,
    plan_kind: str,
    user_id: int,
    paper_id: int,
    page: int,
    cache_key_like: str,
) -> tuple[Optional[Dict[str, Any]], Optional[datetime], Optional[str]]:
    cache_key_like = str(cache_key_like or "").strip()
    if not cache_key_like:
        return None, None, None
    try:
        async with async_session_factory() as session:
            stmt = (
                select(PaperReaderPlanCache)
                .where(
                    and_(
                        PaperReaderPlanCache.plan_kind == str(plan_kind or "").strip(),
                        PaperReaderPlanCache.user_id == int(user_id),
                        PaperReaderPlanCache.paper_id == int(paper_id),
                        PaperReaderPlanCache.page == int(page),
                        PaperReaderPlanCache.cache_key.like(cache_key_like),
                    )
                )
                .order_by(PaperReaderPlanCache.updated_at.desc())
            )
            result = await session.execute(stmt)
            records = list(result.scalars().all())
            if not records:
                return None, None, None
            expired_found = False
            now = datetime.utcnow()
            for record in records:
                expires_at = record.expires_at
                if expires_at and expires_at <= now:
                    await session.delete(record)
                    expired_found = True
                    continue
                if expired_found:
                    await session.commit()
                return _jsonable_dict(record.payload_json or {}), expires_at, str(record.cache_key or "").strip()
            if expired_found:
                await session.commit()
            return None, None, None
    except Exception as exc:
        logger.warning(
            f"[Literature PlanCache] stable DB read failed kind={plan_kind} user_id={user_id} "
            f"paper_id={paper_id} page={page}: {exc}"
        )
        return None, None, None


async def _plan_cache_db_set(
    cache_key: str,
    plan_kind: str,
    payload: Dict[str, Any],
    *,
    user_id: int,
    paper_id: int,
    page: int,
    compose_source_signature: str,
    ttl_seconds: int,
) -> None:
    if any(item is None for item in (user_id, paper_id, page, compose_source_signature)):
        return
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
    payload_json = _jsonable_dict(payload)
    values = {
        "plan_kind": plan_kind,
        "cache_key": cache_key,
        "user_id": int(user_id),
        "paper_id": int(paper_id),
        "page": int(page),
        "compose_source_signature": str(compose_source_signature or "").strip(),
        "payload_json": payload_json,
        "expires_at": expires_at,
        "created_at": now,
        "updated_at": now,
    }
    try:
        async with async_session_factory() as session:
            stmt = pg_insert(PaperReaderPlanCache).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["cache_key"],
                set_={
                    "payload_json": payload_json,
                    "expires_at": expires_at,
                    "compose_source_signature": values["compose_source_signature"],
                    "user_id": values["user_id"],
                    "paper_id": values["paper_id"],
                    "page": values["page"],
                    "updated_at": now,
                },
            )
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:
        logger.warning(f"[Literature PlanCache] DB write failed cache_key={cache_key}: {exc}")


def _adjacent_page_structured_v2_parser_version_token() -> str:
    primary = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash").strip()
    max_tokens = max(2048, int(getattr(settings, "reader_mm_parser_max_tokens", 7000) or 7000))
    return f"adjacent_structured_v2:{primary}:max_tokens={max_tokens}"


def _adjacent_page_structured_v2_cache_key(
    *,
    paper_id: int,
    page: int,
    relation: str,
    image_path: str,
    image_url: str,
) -> str:
    relation_token = str(relation or "").strip() or "adjacent"
    image_sig_basis = {
        "image_url": str(image_url or "").strip(),
        "image_path": os.path.abspath(str(image_path or "").strip()) if str(image_path or "").strip() else "",
        "size": os.path.getsize(str(image_path or "").strip()) if str(image_path or "").strip() and os.path.exists(str(image_path or "").strip()) else 0,
        "mtime": int(os.path.getmtime(str(image_path or "").strip())) if str(image_path or "").strip() and os.path.exists(str(image_path or "").strip()) else 0,
    }
    image_sig = hashlib.sha256(
        json.dumps(image_sig_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    parser_sig = hashlib.sha256(_adjacent_page_structured_v2_parser_version_token().encode("utf-8")).hexdigest()[:12]
    return (
        f"{ADJACENT_PAGE_STRUCTURED_V2_CACHE_NAMESPACE}:ordered_structured_context.v1:"
        f"{int(paper_id)}:{int(page)}:{relation_token}:{image_sig}:{parser_sig}"
    )


async def _adjacent_page_structured_v2_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature AdjacentStructuredV2] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = _adjacent_page_structured_v2_cache_memory.get(cache_key)
    if not item:
        db_payload, expires_at = await _plan_cache_db_get(cache_key, ADJACENT_PAGE_STRUCTURED_V2_CACHE_KIND)
        if isinstance(db_payload, dict):
            ttl_seconds = ADJACENT_PAGE_STRUCTURED_V2_CACHE_TTL_SECONDS
            if isinstance(expires_at, datetime):
                now_dt = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
                delta = (expires_at - now_dt).total_seconds()
                ttl_seconds = max(1, int(delta)) if delta > 0 else 1
            _adjacent_page_structured_v2_cache_memory[cache_key] = (time.time() + ttl_seconds, db_payload)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(db_payload, ensure_ascii=False),
                        ex=max(1, int(ttl_seconds)),
                    )
                except Exception as exc:
                    logger.warning(f"[Literature AdjacentStructuredV2] Redis回填失败，保留内存/DB缓存: {exc}")
            return db_payload, "db"
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        _adjacent_page_structured_v2_cache_memory.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _adjacent_page_structured_v2_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    *,
    user_id: int,
    paper_id: int,
    page: int,
    ttl_seconds: int = ADJACENT_PAGE_STRUCTURED_V2_CACHE_TTL_SECONDS,
) -> None:
    compose_source_signature = _adjacent_page_structured_v2_parser_version_token()
    await _plan_cache_db_set(
        cache_key,
        ADJACENT_PAGE_STRUCTURED_V2_CACHE_KIND,
        payload,
        user_id=int(user_id),
        paper_id=int(paper_id),
        page=int(page),
        compose_source_signature=compose_source_signature,
        ttl_seconds=ttl_seconds,
    )

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature AdjacentStructuredV2] Redis写入失败，降级内存缓存: {exc}")

    _adjacent_page_structured_v2_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


def _sse_payload(event: str, data: Any) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


def _parse_int_allowlist(raw: str) -> Set[int]:
    values: Set[int] = set()
    text = str(raw or "").strip()
    if not text:
        return values
    for part in text.split(","):
        token = str(part or "").strip()
        if token.isdigit():
            values.add(int(token))
    return values


def _reader_pipeline_mode() -> str:
    explicit = str(getattr(settings, "reader_pipeline_mode", "legacy") or "").strip().lower()
    raw_mode_env = str(os.getenv("READER_PIPELINE_MODE", "") or "").strip().lower()
    valid_modes = {"legacy", "single_agent_v2"}

    if raw_mode_env:
        if explicit in valid_modes:
            return explicit
        logger.warning(
            f"[Literature API] invalid READER_PIPELINE_MODE='{raw_mode_env}', fallback to compatibility switch."
        )

    if explicit == "single_agent_v2":
        return "single_agent_v2"
    if bool(getattr(settings, "reader_simplified_pipeline_enabled", False)):
        logger.warning(
            "[Literature API] reader_simplified_pipeline_enabled is deprecated; "
            "use reader_pipeline_mode=single_agent_v2"
        )
        return "single_agent_v2"
    return "legacy"


def _is_single_agent_v2_active(*, paper_id: int, page: Optional[int] = None) -> bool:
    if _reader_pipeline_mode() != "single_agent_v2":
        return False
    paper_allow = _parse_int_allowlist(str(getattr(settings, "reader_pipeline_allowlist_papers", "") or ""))
    page_allow = _parse_int_allowlist(str(getattr(settings, "reader_pipeline_allowlist_pages", "") or ""))
    if not paper_allow:
        paper_allow = _parse_int_allowlist(str(getattr(settings, "reader_simplified_allowlist_papers", "") or ""))
    if not page_allow:
        page_allow = _parse_int_allowlist(str(getattr(settings, "reader_simplified_allowlist_pages", "") or ""))
    if paper_allow and int(paper_id) not in paper_allow:
        return False
    if page is not None and page_allow and int(page) not in page_allow:
        return False
    return True


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


def _normalize_external_link(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", token, flags=re.IGNORECASE):
        return token
    if token.startswith("//"):
        return f"https:{token}"
    if "." in token and " " not in token:
        return f"https://{token}"
    return token


def _extract_doi_from_text(value: Optional[str]) -> Optional[str]:
    token = unquote(str(value or "").strip())
    if not token:
        return None

    match = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", token, flags=re.IGNORECASE)
    if not match:
        return None

    doi = match.group(0).strip().rstrip(").,;]}")
    doi = doi.split("?", 1)[0].split("#", 1)[0]
    doi = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi or None


def _extract_pubmed_id_from_text(value: Optional[str]) -> Optional[str]:
    token = unquote(str(value or "").strip())
    if not token:
        return None

    patterns = (
        r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)",
        r"\bpmid[:\s]*([0-9]+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, token, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _extract_openalex_id_from_text(value: Optional[str]) -> Optional[str]:
    token = unquote(str(value or "").strip())
    if not token:
        return None
    match = re.search(r"(?:api\.)?openalex\.org/(?:works/)?(W\d+)\b", token, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def _extract_semantic_scholar_id_from_text(value: Optional[str]) -> Optional[str]:
    token = unquote(str(value or "").strip())
    if not token:
        return None
    match = re.search(r"semanticscholar\.org/paper/(?:[^/?#]+/)?([A-Za-z0-9-]{16,})", token, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _extract_arxiv_id_from_text(value: Optional[str]) -> Optional[str]:
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
            return _normalize_arxiv_id(match.group(1).removesuffix(".pdf").rstrip(").,;]}"))

    direct = token.strip()
    direct = direct.removesuffix(".pdf")
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z\-]+)?/\d{7})(?:v\d+)?", direct, flags=re.IGNORECASE):
        return _normalize_arxiv_id(direct)
    return None


def _extract_year_from_text(value: Optional[str]) -> Optional[int]:
    token = str(value or "").strip()
    if not token:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", token)
    if not match:
        return None
    try:
        return int(match.group(0))
    except (TypeError, ValueError):
        return None


def _extract_meta_values(soup: Any, keys: Sequence[str]) -> List[str]:
    if soup is None:
        return []

    key_set = {str(key).strip().lower() for key in keys if str(key).strip()}
    values: List[str] = []
    for meta in soup.find_all("meta"):
        key = (
            meta.get("name")
            or meta.get("property")
            or meta.get("http-equiv")
            or meta.get("itemprop")
            or ""
        ).strip().lower()
        if key not in key_set:
            continue
        content = str(meta.get("content") or "").strip()
        if content:
            values.append(content)
    return values


def _build_manual_paper_result_from_html(url: str, html: str) -> Optional[PaperResult]:
    if not html or BeautifulSoup is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    title_values = _extract_meta_values(soup, ["citation_title", "dc.title", "og:title", "twitter:title"])
    title = title_values[0] if title_values else ""
    if not title:
        title_tag = soup.find("title")
        title = str(title_tag.get_text(strip=True) if title_tag else "").strip()

    if not title:
        return None

    author_values = _extract_meta_values(soup, ["citation_author", "dc.creator", "author"])
    authors = [{"name": name.strip()} for name in author_values if str(name).strip()]

    abstract_values = _extract_meta_values(
        soup,
        ["citation_abstract", "description", "og:description", "twitter:description", "dc.description"],
    )
    abstract = abstract_values[0].strip() if abstract_values else None

    venue_values = _extract_meta_values(
        soup,
        ["citation_journal_title", "citation_conference_title", "citation_inbook_title", "dc.source", "og:site_name"],
    )
    venue = venue_values[0].strip() if venue_values else None

    year_values = _extract_meta_values(
        soup,
        ["citation_publication_date", "citation_online_date", "dc.date", "article:published_time"],
    )
    year = _extract_year_from_text(year_values[0] if year_values else None)

    doi_values = _extract_meta_values(soup, ["citation_doi", "dc.identifier"])
    doi = _extract_doi_from_text(doi_values[0] if doi_values else html)

    pdf_values = _extract_meta_values(soup, ["citation_pdf_url", "pdf_url"])
    pdf_url = urljoin(url, pdf_values[0]) if pdf_values else None

    external_seed = doi or url or title
    external_id = hashlib.md5(external_seed.encode("utf-8")).hexdigest()

    return PaperResult(
        source="manual",
        external_id=external_id,
        title=title,
        abstract=abstract,
        authors=authors,
        year=year,
        venue=venue,
        citation_count=0,
        reference_count=0,
        url=url,
        pdf_url=pdf_url,
        arxiv_id=None,
        doi=doi,
        fields_of_study=[],
        raw_data={"import_method": "html_meta", "source_url": url},
    )


async def _resolve_doi_to_paper(service: Any, doi: str) -> Optional[PaperResult]:
    normalized = _extract_doi_from_text(doi)
    if not normalized:
        return None

    openalex_paper = await service.openalex.get_paper_by_doi(normalized)
    crossref_paper = await service.crossref.get_paper_by_doi(normalized)

    if openalex_paper and crossref_paper:
        if not openalex_paper.abstract and crossref_paper.abstract:
            openalex_paper.abstract = crossref_paper.abstract
        if not openalex_paper.venue and crossref_paper.venue:
            openalex_paper.venue = crossref_paper.venue
        if not openalex_paper.url and crossref_paper.url:
            openalex_paper.url = crossref_paper.url
        if not openalex_paper.pdf_url and crossref_paper.pdf_url:
            openalex_paper.pdf_url = crossref_paper.pdf_url
        if not openalex_paper.authors and crossref_paper.authors:
            openalex_paper.authors = crossref_paper.authors
        return openalex_paper

    return openalex_paper or crossref_paper


def _build_save_request_from_paper_result(
    paper: PaperResult,
    *,
    collection_ids: Optional[Sequence[int]] = None,
    imported_link: Optional[str] = None,
) -> SavePaperFromSearchRequest:
    inferred_arxiv_id = _infer_arxiv_id_from_candidates(
        paper.arxiv_id,
        paper.url,
        paper.doi,
        imported_link,
    )
    inferred_pdf_url = paper.pdf_url or _build_arxiv_pdf_url(inferred_arxiv_id)
    external_id = str(paper.external_id or paper.url or paper.doi or "").strip()
    if not external_id:
        external_seed = f"{paper.title}|{paper.year or ''}|{paper.doi or ''}|{paper.url or ''}"
        external_id = hashlib.md5(external_seed.encode("utf-8")).hexdigest()

    raw_data = dict(paper.raw_data or {})
    if imported_link:
        raw_data["imported_link"] = imported_link

    return SavePaperFromSearchRequest(
        source=str(paper.source or "manual"),
        external_id=external_id,
        title=paper.title,
        abstract=paper.abstract,
        authors=paper.authors or [],
        year=paper.year,
        venue=paper.venue,
        citation_count=int(paper.citation_count or 0),
        reference_count=int(paper.reference_count or 0),
        url=paper.url,
        pdf_url=inferred_pdf_url,
        arxiv_id=inferred_arxiv_id or paper.arxiv_id,
        doi=paper.doi,
        fields_of_study=paper.fields_of_study or [],
        raw_data=raw_data,
        collection_ids=[int(item) for item in (collection_ids or [])],
    )


def _build_saved_lookup_keys(paper: PaperResult) -> List[str]:
    keys: List[str] = []

    semantic_id = (paper.external_id or "").strip()
    if paper.source == "semantic_scholar" and semantic_id:
        keys.append(f"s2:{semantic_id}")

    arxiv_id = _normalize_arxiv_id(paper.arxiv_id or (paper.external_id if paper.source == "arxiv" else None))
    if arxiv_id:
        keys.append(f"arxiv:{arxiv_id}")

    pubmed_id = (paper.external_id or "").strip()
    if paper.source == "pubmed" and pubmed_id:
        keys.append(f"pubmed:{pubmed_id}")

    doi_norm = _normalize_text(paper.doi)
    if doi_norm:
        keys.append(f"doi:{doi_norm}")

    title = (paper.title or "").strip()
    if title:
        keys.append(f"title:{title}")

    return keys


async def _load_saved_paper_lookup(
    db: AsyncSession,
    user_id: int,
    papers: Sequence[PaperResult],
) -> Dict[str, int]:
    semantic_ids: Set[str] = set()
    arxiv_ids: Set[str] = set()
    pubmed_ids: Set[str] = set()
    dois: Set[str] = set()
    titles: Set[str] = set()

    for paper in papers:
        semantic_id = (paper.external_id or "").strip()
        if paper.source == "semantic_scholar" and semantic_id:
            semantic_ids.add(semantic_id)

        arxiv_id = _normalize_arxiv_id(paper.arxiv_id or (paper.external_id if paper.source == "arxiv" else None))
        if arxiv_id:
            arxiv_ids.add(arxiv_id)

        pubmed_id = (paper.external_id or "").strip()
        if paper.source == "pubmed" and pubmed_id:
            pubmed_ids.add(pubmed_id)

        doi_norm = _normalize_text(paper.doi)
        if doi_norm:
            dois.add(doi_norm)

        title = (paper.title or "").strip()
        if title:
            titles.add(title)

    predicates = []
    if semantic_ids:
        predicates.append(Paper.semantic_scholar_id.in_(semantic_ids))
    if arxiv_ids:
        predicates.append(Paper.arxiv_id.in_(arxiv_ids))
    if pubmed_ids:
        predicates.append(Paper.pubmed_id.in_(pubmed_ids))
    if dois:
        predicates.append(func.lower(Paper.doi).in_(dois))
    if titles:
        predicates.append(Paper.title.in_(titles))

    if not predicates:
        return {}

    rows = await db.execute(
        select(
            Paper.id,
            Paper.semantic_scholar_id,
            Paper.arxiv_id,
            Paper.pubmed_id,
            Paper.doi,
            Paper.title,
        ).where(
            and_(
                Paper.user_id == int(user_id),
                or_(*predicates),
            )
        )
    )

    lookup: Dict[str, int] = {}
    for row in rows.all():
        paper_id = int(row[0])
        semantic_scholar_id = row[1]
        arxiv_id = row[2]
        pubmed_id = row[3]
        doi = row[4]
        title = row[5]

        if semantic_scholar_id:
            lookup[f"s2:{semantic_scholar_id}"] = paper_id
        if arxiv_id:
            lookup[f"arxiv:{_normalize_arxiv_id(arxiv_id)}"] = paper_id
        if pubmed_id:
            lookup[f"pubmed:{pubmed_id}"] = paper_id
        if doi:
            lookup[f"doi:{_normalize_text(doi)}"] = paper_id
        if title:
            lookup[f"title:{str(title).strip()}"] = paper_id

    return lookup


def _resolve_saved_paper_id(paper: PaperResult, saved_lookup: Mapping[str, int]) -> Optional[int]:
    for key in _build_saved_lookup_keys(paper):
        paper_id = saved_lookup.get(key)
        if paper_id is not None:
            return int(paper_id)
    return None


async def _load_collection_ids_for_paper(db: AsyncSession, paper_id: int) -> List[int]:
    rows = await db.execute(
        select(paper_collection_association.c.collection_id).where(
            paper_collection_association.c.paper_id == int(paper_id)
        )
    )
    return [int(row[0]) for row in rows.fetchall()]


async def _add_paper_to_collections_if_missing(
    db: AsyncSession,
    *,
    paper_id: int,
    user_id: int,
    collection_ids: Sequence[int],
) -> List[int]:
    normalized_ids = [int(item) for item in collection_ids if item is not None]
    if not normalized_ids:
        return []

    existing_ids = set(await _load_collection_ids_for_paper(db, int(paper_id)))
    added_ids: List[int] = []

    for coll_id in normalized_ids:
        if coll_id in existing_ids:
            continue

        coll_result = await db.execute(
            select(PaperCollection).where(
                and_(
                    PaperCollection.id == int(coll_id),
                    PaperCollection.user_id == int(user_id),
                )
            )
        )
        collection = coll_result.scalar_one_or_none()
        if not collection:
            continue

        await db.execute(
            paper_collection_association.insert().values(
                paper_id=int(paper_id),
                collection_id=int(coll_id),
            )
        )
        await db.execute(
            PaperCollection.__table__.update().where(
                PaperCollection.id == int(coll_id)
            ).values(paper_count=PaperCollection.paper_count + 1)
        )
        added_ids.append(int(coll_id))
        existing_ids.add(int(coll_id))

    return added_ids


async def _resolve_target_collection_ids_for_save(
    db: AsyncSession,
    *,
    user_id: int,
    requested_collection_ids: Optional[Sequence[int]],
) -> List[int]:
    collection_ids = [int(item) for item in dict.fromkeys(requested_collection_ids or [])]
    if collection_ids:
        return collection_ids

    default_result = await db.execute(
        select(PaperCollection).where(
            and_(
                PaperCollection.user_id == int(user_id),
                PaperCollection.is_default == True,
            )
        )
    )
    default_collection = default_result.scalars().first()
    if not default_collection:
        return []
    return [int(default_collection.id)]


async def _find_existing_paper_for_request(
    db: AsyncSession,
    *,
    user_id: int,
    request: SavePaperFromSearchRequest,
) -> Optional[Paper]:
    request_doi = _normalize_text(request.doi)
    request_arxiv_id = _normalize_arxiv_id(
        request.arxiv_id or (request.external_id if request.source == "arxiv" else None)
    )

    if request.source == "semantic_scholar" and request.external_id:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == int(user_id),
                Paper.semantic_scholar_id == request.external_id,
            )
        )
    elif request.source == "arxiv" and request_arxiv_id:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == int(user_id),
                Paper.arxiv_id == request_arxiv_id,
            )
        )
    elif request.source == "pubmed" and request.external_id:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == int(user_id),
                Paper.pubmed_id == request.external_id,
            )
        )
    elif request_doi:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == int(user_id),
                func.lower(Paper.doi) == request_doi,
            )
        )
    else:
        stmt = select(Paper).where(
            and_(
                Paper.user_id == int(user_id),
                Paper.title == request.title,
            )
        )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _resolve_paper_from_link(
    service: Any,
    raw_link: str,
) -> Tuple[PaperResult, str, str]:
    candidate = _normalize_external_link(raw_link)
    if not candidate:
        raise HTTPException(status_code=422, detail="请输入论文链接或 DOI / arXiv 标识")

    arxiv_id = _extract_arxiv_id_from_text(candidate)
    if arxiv_id:
        paper = await service.arxiv.get_paper(arxiv_id)
        if paper:
            return paper, "arxiv", f"https://arxiv.org/abs/{arxiv_id}"

    doi = _extract_doi_from_text(candidate)
    if doi:
        paper = await _resolve_doi_to_paper(service, doi)
        if paper:
            return paper, "doi", f"https://doi.org/{doi}"

    pubmed_id = _extract_pubmed_id_from_text(candidate)
    if pubmed_id:
        paper = await service.pubmed.get_paper(pubmed_id)
        if paper:
            return paper, "pubmed", f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/"

    openalex_id = _extract_openalex_id_from_text(candidate)
    if openalex_id:
        paper = await service.openalex.get_paper(openalex_id)
        if paper:
            return paper, "openalex", f"https://openalex.org/{openalex_id}"

    semantic_scholar_id = _extract_semantic_scholar_id_from_text(candidate)
    if semantic_scholar_id:
        paper = await service.s2.get_paper(semantic_scholar_id)
        if paper:
            return paper, "semantic_scholar", candidate

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="目前仅支持 DOI、arXiv、PubMed、OpenAlex、Semantic Scholar 或网页链接")

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "ResearchAssistant/1.0"},
        ) as client:
            response = await client.get(candidate)
    except Exception as exc:
        logger.error(f"[Literature API] 链接解析请求失败: {exc}")
        raise HTTPException(status_code=502, detail="链接请求失败，请稍后重试") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=404, detail="无法访问该链接，无法导入论文")

    final_url = str(response.url)

    doi = _extract_doi_from_text(final_url)
    if doi:
        paper = await _resolve_doi_to_paper(service, doi)
        if paper:
            return paper, "doi_redirect", final_url

    arxiv_id = _extract_arxiv_id_from_text(final_url)
    if arxiv_id:
        paper = await service.arxiv.get_paper(arxiv_id)
        if paper:
            return paper, "arxiv_redirect", final_url

    pubmed_id = _extract_pubmed_id_from_text(final_url)
    if pubmed_id:
        paper = await service.pubmed.get_paper(pubmed_id)
        if paper:
            return paper, "pubmed_redirect", final_url

    openalex_id = _extract_openalex_id_from_text(final_url)
    if openalex_id:
        paper = await service.openalex.get_paper(openalex_id)
        if paper:
            return paper, "openalex_redirect", final_url

    manual_paper = _build_manual_paper_result_from_html(final_url, response.text)
    if manual_paper and manual_paper.doi:
        resolved = await _resolve_doi_to_paper(service, manual_paper.doi)
        if resolved:
            if not resolved.url and manual_paper.url:
                resolved.url = manual_paper.url
            if not resolved.pdf_url and manual_paper.pdf_url:
                resolved.pdf_url = manual_paper.pdf_url
            if not resolved.abstract and manual_paper.abstract:
                resolved.abstract = manual_paper.abstract
            if not resolved.venue and manual_paper.venue:
                resolved.venue = manual_paper.venue
            return resolved, "html_meta_doi", final_url

    if manual_paper:
        return manual_paper, "html_meta", final_url

    raise HTTPException(status_code=404, detail="无法从该链接解析论文信息，请尝试 DOI、arXiv 或 PubMed 链接")


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


def _build_arxiv_pdf_url(arxiv_id: Optional[str]) -> Optional[str]:
    normalized = _normalize_arxiv_id(arxiv_id)
    if not normalized:
        return None
    return f"https://arxiv.org/pdf/{normalized}"


def _infer_arxiv_id_from_candidates(*values: Optional[str]) -> Optional[str]:
    for value in values:
        arxiv_id = _extract_arxiv_id_from_text(value)
        if arxiv_id:
            return arxiv_id
    return None


def _build_pdf_download_candidates(paper: Paper) -> List[str]:
    raw_data = getattr(paper, "raw_data", {}) or {}
    direct_pdf_url = str(getattr(paper, "pdf_url", "") or "").strip()
    arxiv_id = _infer_arxiv_id_from_candidates(
        getattr(paper, "arxiv_id", None),
        getattr(paper, "arxiv_url", None),
        getattr(paper, "url", None),
        getattr(paper, "doi", None),
        raw_data.get("imported_link"),
        raw_data.get("source_url"),
        raw_data.get("id"),
    )

    candidates: List[str] = []

    if arxiv_id:
        arxiv_pdf_url = _build_arxiv_pdf_url(arxiv_id)
        if arxiv_pdf_url:
            candidates.append(arxiv_pdf_url)

    if direct_pdf_url:
        candidates.append(direct_pdf_url)

    for candidate in (
        raw_data.get("pdf_url"),
        raw_data.get("oa_url"),
        getattr(paper, "url", None),
        getattr(paper, "arxiv_url", None),
    ):
        token = str(candidate or "").strip()
        if token.lower().endswith(".pdf"):
            candidates.append(token)

    unique_candidates: List[str] = []
    seen: Set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_candidates.append(normalized)
    return unique_candidates


def _resolve_pdf_download_url(paper: Paper) -> Optional[str]:
    candidates = _build_pdf_download_candidates(paper)
    return candidates[0] if candidates else None


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


async def _get_owned_kb_or_none(db: AsyncSession, current_user: User, kb_id: Optional[int]) -> Optional[KnowledgeBase]:
    try:
        resolved_kb_id = int(kb_id or 0)
    except (TypeError, ValueError):
        return None
    if resolved_kb_id <= 0:
        return None
    stmt = select(KnowledgeBase).where(
        and_(KnowledgeBase.id == resolved_kb_id, KnowledgeBase.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _normalize_reader_selected_kb_id(
    db: AsyncSession,
    current_user: User,
    selected_kb_id: Optional[int],
) -> Optional[int]:
    kb = await _get_owned_kb_or_none(db, current_user, selected_kb_id)
    if kb is None:
        return None
    return int(kb.id)


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
        except ValueError:
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


async def _extract_adjacent_page_reference_text(
    *,
    image_path: str,
    relation: str,
    page: int,
) -> Optional[Dict[str, Any]]:
    if not str(image_path or "").strip() or not os.path.exists(str(image_path or "").strip()):
        return None
    if not DashScopeMultimodalService.is_available():
        return None

    api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
    base_url = str(getattr(settings, "aliyun_dashscope_api_base", "") or getattr(settings, "aliyun_base_url", "") or "").strip()
    model = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash").strip()
    if not api_key or not base_url or not model:
        return None

    prompt = (
        "You are extracting structured reference context from a neighboring PDF page image.\n"
        "Return JSON only.\n"
        "This is for continuity/context, not for replacing current-page evidence.\n"
        "Read the page and produce a compact structured description that helps another agent understand carry-over text, figures, tables, and formulas.\n"
        "Ignore page chrome, decorative labels, axis ticks, and obvious OCR garbage unless they are part of a figure/table caption.\n"
        "Return shape: "
        '{"page":0,"relation":"previous_page|next_page","reference_only":true,'
        '"summary":"...",'
        '"body_text":"...",'
        '"figures":[{"label":"Figure 1","description":"..."}],'
        '"tables":[{"label":"Table 1","description":"..."}],'
        '"equations":[{"label":"(1)","description":"..."}],'
        '"continuation_hints":["..."],'
        '"raw_text":"..."}'
    )
    user_prompt = (
        f"target_page={int(page)}\n"
        f"relation={relation}\n"
        "This content is reference-only for the CURRENT page and must not override current-page evidence.\n"
        "Extract and organize useful continuity context. Keep body_text under 1200 chars and each description concise."
    )
    try:
        result = await DashScopeMultimodalService.chat_json(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=prompt,
            user_prompt=user_prompt,
            image_paths=[str(image_path)],
            max_tokens=900,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning(f"[Literature Experience] adjacent page OCR failed page={page} relation={relation}: {exc}")
        return None

    parsed = dict(result.get("parsed") or {}) if isinstance(result, dict) else {}
    summary = str(parsed.get("summary") or "").strip()
    body_text = str(parsed.get("body_text") or parsed.get("text") or "").strip()
    raw_text = str(parsed.get("raw_text") or result.get("raw_text") or "").strip()
    figures = [
        {
            "label": str(item.get("label") or "").strip(),
            "description": str(item.get("description") or "").strip(),
        }
        for item in list(parsed.get("figures") or [])
        if isinstance(item, dict) and (str(item.get("label") or "").strip() or str(item.get("description") or "").strip())
    ][:4]
    tables = [
        {
            "label": str(item.get("label") or "").strip(),
            "description": str(item.get("description") or "").strip(),
        }
        for item in list(parsed.get("tables") or [])
        if isinstance(item, dict) and (str(item.get("label") or "").strip() or str(item.get("description") or "").strip())
    ][:4]
    equations = [
        {
            "label": str(item.get("label") or "").strip(),
            "description": str(item.get("description") or "").strip(),
        }
        for item in list(parsed.get("equations") or [])
        if isinstance(item, dict) and (str(item.get("label") or "").strip() or str(item.get("description") or "").strip())
    ][:4]
    continuation_hints = [
        str(item).strip()
        for item in list(parsed.get("continuation_hints") or [])
        if str(item).strip()
    ][:6]
    if not any([summary, body_text, raw_text, figures, tables, equations, continuation_hints]):
        return None
    return {
        "page": int(parsed.get("page") or page),
        "relation": str(parsed.get("relation") or relation).strip() or relation,
        "reference_only": True,
        "source": "vlflash_page_ocr",
        "summary": summary[:400],
        "body_text": body_text[:1200],
        "figures": figures,
        "tables": tables,
        "equations": equations,
        "continuation_hints": continuation_hints,
        "raw_text": raw_text[:1600],
    }


def _build_adjacent_page_image_payload(
    *,
    image_path: str,
    image_url: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "url": str(image_url or "").strip(),
        "width": None,
        "height": None,
    }
    path = str(image_path or "").strip()
    if not path or not os.path.exists(path):
        return payload
    try:
        from PIL import Image

        with Image.open(path) as image:
            payload["width"], payload["height"] = image.size
    except Exception:
        logger.debug(f"[Literature Experience] adjacent page image size unavailable path={path}")
    return payload


def _normalize_structured_adjacent_content_stream_item(
    raw_item: Mapping[str, Any],
    *,
    seq: int,
) -> Dict[str, Any]:
    item = _jsonable_dict(raw_item)
    raw_type = str(item.get("type") or "").strip().lower()
    normalized_type = raw_type
    type_aliases = {
        "heading": "header",
        "title": "header",
        "section_title": "header",
        "section_header": "header",
        "subheading": "header",
        "image": "figure",
        "chart": "figure",
        "formula": "equation",
        "math": "equation",
        "footnote": "footer",
        "list_item": "paragraph",
        "bullet": "paragraph",
        "link": "paragraph",
    }
    if raw_type in type_aliases:
        normalized_type = type_aliases[raw_type]
    normalized = {
        "seq": int(item.get("seq") or seq),
        "type": normalized_type,
        "text": str(item.get("text") or "").strip(),
        "ocr_text": str(item.get("ocr_text") or item.get("raw_text") or "").strip(),
        "role": str(item.get("role") or "").strip(),
        "label": str(item.get("label") or "").strip(),
        "caption": str(item.get("caption") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "columns": [str(col).strip() for col in list(item.get("columns") or []) if str(col).strip()],
        "rows": [
            [str(cell).strip() for cell in list(row or [])]
            for row in list(item.get("rows") or [])
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
        ],
        "normalized_text": str(item.get("normalized_text") or "").strip(),
        "meta": {
            **_jsonable_dict(item.get("meta") or {}),
            **({"raw_type": raw_type} if raw_type and raw_type != normalized_type else {}),
        },
    }
    return normalized


def _coerce_adjacent_page_structured_result(
    *,
    result: Mapping[str, Any],
    page: int,
    relation: str,
    image_path: str,
    image_url: str,
) -> Dict[str, Any]:
    parsed = dict(result.get("parsed") or {}) if isinstance(result, Mapping) else {}
    raw_stream = list(parsed.get("content_stream") or [])
    if not parsed:
        raw_text = str(result.get("raw_text") or "").strip()
        usage = _jsonable_dict(result.get("usage") or {})
        completion_tokens = int(usage.get("completion_tokens") or 0)
        parser_max_tokens = max(2048, int(getattr(settings, "reader_mm_parser_max_tokens", 7000) or 7000))
        if raw_text and completion_tokens >= parser_max_tokens:
            raise ValueError(
                "neighboring-page structured context generation failed: model output was truncated before valid JSON could be parsed"
            )
        raise ValueError(
            "neighboring-page structured context generation failed: model did not return parseable ordered JSON"
        )
    if not raw_stream:
        raise ValueError(
            "neighboring-page structured context generation failed: ordered JSON missing content_stream"
        )
    content_stream = [
        _normalize_structured_adjacent_content_stream_item(item, seq=index)
        for index, item in enumerate(raw_stream, start=1)
        if isinstance(item, Mapping)
    ]
    if not content_stream:
        raise ValueError(
            "neighboring-page structured context generation failed: ordered JSON content_stream rows were invalid"
        )
    row_payload = {
        "page": int(parsed.get("page") or page),
        "relation": str(parsed.get("relation") or relation).strip() or relation,
        "source": str(parsed.get("source") or "neighbor_page_vlm_parse").strip() or "neighbor_page_vlm_parse",
        "fidelity": "ordered_structured_context",
        "reference_only": False,
        "page_image": _build_adjacent_page_image_payload(image_path=image_path, image_url=image_url),
        "page_summary": str(parsed.get("page_summary") or parsed.get("summary") or "").strip(),
        "content_stream": content_stream,
        "continuation_hints": [
            str(item).strip()
            for item in list(parsed.get("continuation_hints") or [])
            if str(item).strip()
        ][:6],
        "raw_text": str(parsed.get("raw_text") or result.get("raw_text") or "").strip(),
        "meta": _jsonable_dict(parsed.get("meta") or {}),
    }
    try:
        return ReadingDossierV2AdjacentPageRow.model_validate(row_payload).model_dump(mode="json")
    except Exception as exc:
        raise ValueError(
            f"neighboring-page structured context generation failed: invalid ordered JSON row: {exc}"
        ) from exc


def _adjacent_page_structured_model_attempts() -> List[str]:
    primary = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash").strip()
    return [primary for _ in range(_ADJACENT_PAGE_STRUCTURED_PRIMARY_ATTEMPTS) if primary]


async def _extract_adjacent_page_structured_context_v2(
    *,
    image_path: str,
    relation: str,
    page: int,
    image_url: str = "",
) -> Dict[str, Any]:
    if not str(image_path or "").strip() or not os.path.exists(str(image_path or "").strip()):
        raise ValueError("neighboring-page structured context not implemented: adjacent page image is missing")
    if not DashScopeMultimodalService.is_available():
        raise ValueError("neighboring-page structured context not implemented: multimodal parser unavailable")

    api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
    base_url = str(getattr(settings, "aliyun_dashscope_api_base", "") or getattr(settings, "aliyun_base_url", "") or "").strip()
    attempts = _adjacent_page_structured_model_attempts()
    parser_max_tokens = max(2048, int(getattr(settings, "reader_mm_parser_max_tokens", 7000) or 7000))
    if not api_key or not base_url or not attempts:
        raise ValueError("neighboring-page structured context not implemented: multimodal parser config missing")

    prompt = (
        "You are extracting ordered structured continuity context from a neighboring PDF page image.\n"
        "Return JSON only.\n"
        "This is for continuity-aware generation, not a compact summary lane.\n"
        "Preserve page reading order and emit ordered content_stream rows.\n"
        "Do not collapse the page into summary + body_text.\n"
        "Use this shape exactly: "
        '{"page":0,"relation":"previous_page|next_page","source":"neighbor_page_vlm_parse","fidelity":"ordered_structured_context",'
        '"reference_only":false,"page_summary":"...",'
        '"content_stream":[{"seq":1,"type":"paragraph|figure|table|equation|caption|header|footer","text":"...",'
        '"ocr_text":"...","role":"body","label":"","caption":"","description":"","columns":[],"rows":[],"normalized_text":""}],'
        '"continuation_hints":["..."],"raw_text":"...","meta":{}}'
    )
    user_prompt = (
        f"target_page={int(page)}\n"
        f"relation={relation}\n"
        "Preserve sequential reading order and local continuity near the page boundary.\n"
        "Keep figure labels/captions, table rows/cells, equation normalized text, and ordered OCR/body fragments when available.\n"
        "If structure is imperfect, keep ordered raw text rows instead of collapsing to a short summary."
    )
    attempt_errors: List[str] = []
    for attempt_index, model in enumerate(attempts, start=1):
        retry_hint = ""
        if attempt_errors:
            retry_hint = (
                "Previous output did not satisfy ordered_structured_context JSON requirements. "
                "Return strict JSON only. Preserve content_stream in page reading order. "
                "Do not emit markdown. Keep page_summary brief, continuation_hints concise, and raw_text compact enough to finish the JSON object. "
                f"Previous failure: {attempt_errors[-1]}"
            )
        result = await DashScopeMultimodalService.chat_json(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=prompt,
            user_prompt=(user_prompt + ("\nretry_hint=" + retry_hint if retry_hint else "")),
            image_paths=[str(image_path)],
            max_tokens=parser_max_tokens,
            temperature=0.0,
        )
        try:
            row_payload = _coerce_adjacent_page_structured_result(
                result=result,
                page=page,
                relation=relation,
                image_path=image_path,
                image_url=image_url,
            )
            row_payload.setdefault("meta", {})
            row_payload["meta"] = {
                **_jsonable_dict(row_payload.get("meta") or {}),
                "parser_model": model,
                "parser_version": f"adjacent_structured_v2:{model}",
                "attempt_index": attempt_index,
                "attempt_count": len(attempts),
            }
            return row_payload
        except ValueError as exc:
            attempt_errors.append(f"attempt={attempt_index},model={model},error={str(exc)}")
            continue
    raise ValueError(
        "neighboring-page structured context generation failed after explicit model attempts: "
        + " | ".join(attempt_errors)
    )


def _build_experience_page_dossier(
    *,
    focus_page: int,
    compose_payload: Mapping[str, Any],
    adjacent_page_context: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    runtime = get_generative_reader_agent_runtime()
    build_target_map = getattr(runtime, "_build_current_page_target_map", None)
    is_abstract_target = getattr(runtime, "_looks_like_abstract_page_target", None)
    targets: List[Dict[str, Any]] = []
    enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
    if callable(build_target_map):
        target_map = build_target_map(
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
        )
        dossier_targets = [
            dict(item)
            for item in list(target_map.values())
            if isinstance(item, Mapping) and str(item.get("target_id") or "").strip()
        ]
        if callable(is_abstract_target):
            concrete_targets = [
                item for item in dossier_targets
                if not is_abstract_target(item)
            ]
            if concrete_targets:
                dossier_targets = concrete_targets
        seen_target_ids: set[str] = set()
        for item in dossier_targets[:48]:
            target_id = str(item.get("target_id") or "").strip()
            if not target_id or target_id in seen_target_ids:
                continue
            seen_target_ids.add(target_id)
            targets.append(
                {
                    "target_id": target_id,
                    "kind": str(item.get("kind") or item.get("target_kind") or "").strip(),
                    "target_kind": str(item.get("target_kind") or item.get("kind") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "summary": str(item.get("summary") or item.get("excerpt") or "").strip(),
                    "excerpt": str(item.get("excerpt") or item.get("summary") or "").strip(),
                    "figure_label": str(item.get("figure_label") or "").strip(),
                    "section_label": str(item.get("section_label") or "").strip(),
                    "component_type": str(item.get("component_type") or "").strip(),
                    "node_id": str(item.get("node_id") or "").strip(),
                    "resolved_from_target_id": str(item.get("resolved_from_target_id") or "").strip(),
                    "source_block_ids": [
                        str(raw).strip()
                        for raw in list(item.get("source_block_ids") or [])[:8]
                        if str(raw).strip()
                    ],
                }
            )
    if not targets:
        targets = [
            {
                "target_id": str(item.get("target_id") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "target_kind": str(item.get("target_kind") or item.get("kind") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "excerpt": str(item.get("excerpt") or item.get("summary") or "").strip(),
                "figure_label": str(item.get("figure_label") or "").strip(),
                "section_label": str(item.get("section_label") or "").strip(),
                "component_type": str(item.get("component_type") or "").strip(),
                "node_id": str(item.get("node_id") or "").strip(),
                "resolved_from_target_id": str(item.get("resolved_from_target_id") or "").strip(),
                "source_block_ids": [
                    str(raw).strip()
                    for raw in list(item.get("source_block_ids") or [])[:8]
                    if str(raw).strip()
                ],
            }
            for item in list(enrichment_bundle.get("targets") or [])[:24]
            if isinstance(item, Mapping)
        ]
    assets = [
        {
            "kind": str(item.get("kind") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "source": str(item.get("source") or "").strip(),
        }
        for item in list((compose_payload or {}).get("assets") or [])[:24]
        if isinstance(item, Mapping)
    ]
    quality_report = dict((compose_payload or {}).get("quality_report") or {})
    current_page = {
        "page": int(focus_page),
        "build_mode": str((compose_payload or {}).get("build_mode") or "").strip(),
        "pipeline_version": str((compose_payload or {}).get("pipeline_version") or "").strip(),
        "status": str((compose_payload or {}).get("status") or "").strip(),
        "degraded_reason": str((compose_payload or {}).get("degraded_reason") or "").strip(),
        "decision_log": [str(item).strip() for item in list((compose_payload or {}).get("decision_log") or []) if str(item).strip()][:12],
        "targets": targets,
        "assets": assets,
        "quality": {
            "overall": quality_report.get("overall"),
            "layout_monotony": quality_report.get("layout_monotony"),
            "stop_reason": quality_report.get("stop_reason"),
        },
    }
    adjacent_refs = [
        {
            "page": int(item.get("page") or 0),
            "relation": str(item.get("relation") or "").strip(),
            "reference_only": bool(item.get("reference_only")),
            "source": str(item.get("source") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
            "body_text": str(item.get("body_text") or "").strip(),
            "figures": list(item.get("figures") or []),
            "tables": list(item.get("tables") or []),
            "equations": list(item.get("equations") or []),
            "continuation_hints": [str(row).strip() for row in list(item.get("continuation_hints") or []) if str(row).strip()],
        }
        for item in list(adjacent_page_context or [])
        if isinstance(item, Mapping) and int(item.get("page") or 0) > 0
    ]
    return {
        "focus_page": int(focus_page),
        "current_page": current_page,
        "adjacent_page_context": adjacent_refs,
    }


def _normalize_adjacent_page_context_for_reading_dossier_v2(
    raw_item: Mapping[str, Any],
    *,
    limits: Mapping[str, Any],
) -> ReadingDossierV2AdjacentPageRow:
    payload = _jsonable_dict(raw_item)
    if not payload:
        raise ValueError("neighboring-page structured context not implemented: adjacent page row is empty")
    has_structured_stream = bool(payload.get("content_stream"))
    if not has_structured_stream:
        raise ValueError(
            "neighboring-page structured context not implemented: ordered_structured_context cannot be built from compact summary fields"
        )
    row = ReadingDossierV2AdjacentPageRow.model_validate(payload)
    normalized_stream = list(row.content_stream or [])[: int(limits.get("max_content_stream_items") or 48)]
    normalized = row.model_copy(
        update={
            "page_summary": str(row.page_summary or "")[: int(limits.get("max_page_summary_chars") or 400)],
            "content_stream": normalized_stream,
            "continuation_hints": [
                str(item).strip()
                for item in list(row.continuation_hints or [])
                if str(item).strip()
            ][: int(limits.get("max_continuation_hints") or 6)],
            "raw_text": str(row.raw_text or "")[: int(limits.get("max_raw_text_chars") or 1600)],
        }
    )
    return normalized


def _reading_dossier_v2_adjacent_context_signature(rows: Sequence[ReadingDossierV2AdjacentPageRow]) -> str:
    canonical_rows: List[Dict[str, Any]] = []
    for row in list(rows or []):
        row_payload = row.model_dump(mode="json") if isinstance(row, ReadingDossierV2AdjacentPageRow) else _jsonable_dict(row)
        canonical_rows.append(
            {
                "page": int(row_payload.get("page") or 0),
                "relation": str(row_payload.get("relation") or "").strip(),
                "source": str(row_payload.get("source") or "").strip(),
                "fidelity": str(row_payload.get("fidelity") or "").strip(),
                "content_stream": list(row_payload.get("content_stream") or []),
                "continuation_hints": list(row_payload.get("continuation_hints") or []),
                "raw_text": str(row_payload.get("raw_text") or "").strip(),
            }
        )
    canonical_rows.sort(key=lambda item: (int(item.get("page") or 0), str(item.get("relation") or "")))
    encoded = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _reading_dossier_v2_adjacent_parser_version(rows: Sequence[ReadingDossierV2AdjacentPageRow]) -> str:
    parser_versions: List[str] = []
    for row in list(rows or []):
        payload = row.model_dump(mode="json") if isinstance(row, ReadingDossierV2AdjacentPageRow) else _jsonable_dict(row)
        meta = _jsonable_dict(payload.get("meta") or {})
        candidates = [
            str(meta.get("parser_version") or "").strip(),
            str(meta.get("parser_model") or "").strip(),
            str(meta.get("parse_version") or "").strip(),
            str(payload.get("source") or "").strip(),
        ]
        for value in candidates:
            if value:
                parser_versions.append(value)
                break
    if not parser_versions:
        return "adjacent_parser:unknown"
    unique_versions = sorted(set(parser_versions))
    return "|".join(unique_versions)[:256]


def _build_reading_dossier_v2(
    *,
    focus_page: int,
    reader_profile: str,
    compose_payload: Mapping[str, Any],
    adjacent_page_context: Sequence[Mapping[str, Any]],
    dossier_namespace: str = _READING_DOSSIER_V2_NAMESPACE,
    source_sig_hash: Optional[str] = None,
) -> Dict[str, Any]:
    compose_payload_dict = _jsonable_dict(compose_payload)
    compose_source_signature = str(compose_payload_dict.get("source_signature") or "").strip()
    compose_pipeline_version = str(compose_payload_dict.get("pipeline_version") or "").strip()
    source_sig_hash_value = str(source_sig_hash or "").strip()
    if not source_sig_hash_value:
        source_sig_hash_value = (
            hashlib.sha256(compose_source_signature.encode("utf-8")).hexdigest()[:16]
            if compose_source_signature
            else ""
        )

    focus_page_num = int(focus_page)
    compose_assets_for_current_page: List[Dict[str, Any]] = []
    for item in list(compose_payload_dict.get("assets") or []):
        if not isinstance(item, Mapping):
            continue
        payload = _jsonable_dict(item)
        href = str(payload.get("href") or payload.get("url") or "").strip()
        if not href:
            continue
        meta = _jsonable_dict(payload.get("meta") or {})
        asset_page = int(meta.get("page") or focus_page_num)
        if asset_page != focus_page_num:
            continue
        compose_assets_for_current_page.append(
            {
                "kind": str(payload.get("kind") or "").strip(),
                "label": str(payload.get("label") or "").strip(),
                "source": str(payload.get("source") or "").strip(),
                "href": href,
                "meta": {
                    "asset_id": str(meta.get("asset_id") or "").strip(),
                    "layout_unique_id": str(meta.get("layout_unique_id") or "").strip(),
                    "layout_id": str(meta.get("layout_id") or meta.get("source_layout_id") or "").strip(),
                    "page": asset_page,
                },
            }
        )
    page_grounding_payload = compose_payload_dict.get("page_grounding_v1")
    current_page_degraded = False
    current_page_degraded_reason = ""
    if isinstance(page_grounding_payload, Mapping):
        try:
            current_page_grounding = ReaderPageGrounding.model_validate(page_grounding_payload)
        except Exception:
            current_page_degraded = True
            current_page_degraded_reason = "invalid_page_grounding_v1"
            current_page_grounding = ReaderPageGrounding(page=focus_page_num)
    else:
        current_page_degraded = True
        current_page_degraded_reason = "missing_page_grounding_v1"
        current_page_grounding = ReaderPageGrounding(page=focus_page_num)

    limits = {
        "reference_only": False,
        "max_pages": 2,
        "max_page_summary_chars": 400,
        "max_content_stream_items": 48,
        "max_continuation_hints": 6,
        "max_raw_text_chars": 1600,
    }
    adjacent_rows: List[ReadingDossierV2AdjacentPageRow] = []
    for item in list(adjacent_page_context or []):
        if not isinstance(item, Mapping):
            continue
        normalized = _normalize_adjacent_page_context_for_reading_dossier_v2(item, limits=limits)
        adjacent_rows.append(normalized)
        if len(adjacent_rows) >= limits["max_pages"]:
            break
    adjacent_context_sig_hash = _reading_dossier_v2_adjacent_context_signature(adjacent_rows)
    adjacent_context_parser_version = _reading_dossier_v2_adjacent_parser_version(adjacent_rows)

    dossier = ReadingDossierV2(
        focus_page=focus_page_num,
        reader_profile=str(reader_profile or "").strip() or "curious_generalist",
        compose_source_signature=compose_source_signature,
        current_page={
            "owner": "compose/page_grounding_v1",
            "fidelity": "grounded_evidence",
            "build_meta": {
                "status": str(compose_payload_dict.get("status") or "").strip(),
                "build_mode": str(compose_payload_dict.get("build_mode") or "").strip(),
                "pipeline_version": compose_pipeline_version,
                "degraded": bool(current_page_degraded),
                "degraded_reason": current_page_degraded_reason,
                "compose_assets": compose_assets_for_current_page[:24],
            },
            "rich_grounding": current_page_grounding,
        },
        adjacent_pages={
            "owner": "api/adjacent_page_extraction",
            "fidelity": "ordered_structured_context",
            "reference_only": False,
            "limits": limits,
            "pages": adjacent_rows,
        },
        derived_adjacent_bridge_cues={
            "owner": "runtime",
            "fidelity": "derived_summary",
            "items": [],
        },
        cache_meta={
            "dossier_namespace": str(dossier_namespace or "").strip() or _READING_DOSSIER_V2_NAMESPACE,
            "compose_pipeline_version": compose_pipeline_version,
            "source_sig_hash": source_sig_hash_value,
            "adjacent_context_parser_version": adjacent_context_parser_version,
            "adjacent_context_sig_hash": adjacent_context_sig_hash,
            "adjacent_context_page_scope_version": "ordered_structured_context.v1",
        },
        meta={
            "compose_status": str(compose_payload_dict.get("status") or "").strip(),
            "compose_build_mode": str(compose_payload_dict.get("build_mode") or "").strip(),
            "current_page_grounding_degraded": bool(current_page_degraded),
            "current_page_grounding_degraded_reason": current_page_degraded_reason,
        },
    )
    return dossier.model_dump(mode="json")


def _build_page_artifact_v2_from_dossier(
    *,
    reading_dossier: Mapping[str, Any],
    authored_plan: Mapping[str, Any],
    dossier_signature: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    reader_excerpt_max_chars: Optional[int] = None
    dossier_payload = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    authored_payload = PageArtifactV2AuthoredPlanInput.model_validate(_jsonable_dict(authored_plan)).model_dump(mode="json")

    focus_page = int(dossier_payload.get("focus_page") or 1)
    current_lane = _jsonable_dict(dossier_payload.get("current_page") or {})
    current_lane_build_meta = _jsonable_dict(current_lane.get("build_meta") or {})
    rich_grounding = _jsonable_dict(current_lane.get("rich_grounding") or {})
    reading_nodes = list(rich_grounding.get("reading_nodes") or [])
    layout_atoms = list(rich_grounding.get("layout_atoms") or [])
    evidence_map = list(rich_grounding.get("evidence_map") or [])
    page_image = _jsonable_dict(rich_grounding.get("page_image") or {})
    page_image_url = str(page_image.get("url") or "").strip()
    page_image_path = str(page_image.get("path") or "").strip()
    current_page_assets = [
        _jsonable_dict(item)
        for item in list(current_lane_build_meta.get("compose_assets") or [])
        if isinstance(item, Mapping)
    ]

    excerpt_rows: List[Dict[str, Any]] = []
    for node in reading_nodes:
        if not isinstance(node, Mapping):
            continue
        if not bool(node.get("include_in_main_flow", True)):
            continue
        node_kind = str(node.get("node_kind") or (_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip().lower()
        text = str(node.get("clean_text") or node.get("normalized_text") or node.get("raw_text") or "").strip()
        if not text:
            continue
        if _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=reader_excerpt_max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()],
                block_ids=[str(item).strip() for item in list(node.get("source_block_ids") or []) if str(item).strip()],
                node_id=str(node.get("node_id") or "").strip(),
                max_chars=reader_excerpt_max_chars,
            )
        )

    for atom in layout_atoms:
        if not isinstance(atom, Mapping):
            continue
        if not bool(atom.get("include_in_main_flow", True)):
            continue
        node_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        text = str(atom.get("clean_text") or atom.get("normalized_text") or atom.get("raw_text") or "").strip()
        if not text:
            continue
        allow_media_caption_excerpt = _allows_media_caption_excerpt(node_kind)
        if (not allow_media_caption_excerpt) and _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=reader_excerpt_max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(atom.get("layout_id") or "").strip()] if str(atom.get("layout_id") or "").strip() else [],
                block_ids=[str(item).strip() for item in list(atom.get("canonical_block_ids") or []) if str(item).strip()],
                node_id="",
                max_chars=reader_excerpt_max_chars,
            )
        )

    if not excerpt_rows:
        raise ValueError("page_artifact_v2 requires current-page excerpt anchors from reading_dossier_v2")

    candidate_excerpt_rows = [
        {
            "text": str(row.get("text") or ""),
            "layout_ids": list(row.get("layout_ids") or []),
            "block_ids": list(row.get("block_ids") or []),
            "node_id": str(row.get("node_id") or "").strip(),
        }
        for row in excerpt_rows
    ]

    excerpt_overrides = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("excerpt_overrides") or [])
        if isinstance(item, Mapping)
    ]
    if not excerpt_overrides:
        raise ValueError("artifact draft generation failed: draft did not select any current-page excerpts")

    selected_excerpt_rows: List[Dict[str, Any]] = []
    seen_excerpt_tokens: Set[str] = set()
    for override in excerpt_overrides:
        matched_row = _resolve_excerpt_against_current_page_grounding(
            override=override,
            candidate_excerpt_rows=candidate_excerpt_rows,
            reading_nodes=reading_nodes,
            layout_atoms=layout_atoms,
            seen_excerpt_tokens=seen_excerpt_tokens,
            max_chars=reader_excerpt_max_chars,
        )
        if matched_row:
            selected_excerpt_rows.append(matched_row)

    if not selected_excerpt_rows:
        raise ValueError("artifact draft generation failed: draft-selected excerpts could not be resolved against current-page grounding")
    excerpt_rows = selected_excerpt_rows

    evidence_by_layout: Dict[str, List[str]] = {}
    for item in evidence_map:
        if not isinstance(item, Mapping):
            continue
        layout_id = str(item.get("source_layout_id") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not layout_id or not evidence_id:
            continue
        evidence_by_layout.setdefault(layout_id, []).append(evidence_id)

    expected_reading_node_ids = list(
        dict.fromkeys([str(row.get("node_id") or "").strip() for row in excerpt_rows if str(row.get("node_id") or "").strip()])
    )
    expected_layout_ids = list(
        dict.fromkeys(
            [
                str(layout_id).strip()
                for row in excerpt_rows
                for layout_id in list(row.get("layout_ids") or [])
                if str(layout_id).strip()
            ]
        )
    )
    expected_block_ids = list(
        dict.fromkeys(
            [
                str(block_id).strip()
                for row in excerpt_rows
                for block_id in list(row.get("block_ids") or [])
                if str(block_id).strip()
            ]
        )
    )
    expected_evidence_ids = list(
        dict.fromkeys(
            [
                str(evidence_id).strip()
                for row in excerpt_rows
                for layout_id in list(row.get("layout_ids") or [])
                for evidence_id in list(evidence_by_layout.get(str(layout_id).strip(), []) or [])
                if str(evidence_id).strip()
            ]
        )
    )

    figure_layout_ids = list(
        dict.fromkeys(
            [
                str(layout_id).strip()
                for node in reading_nodes
                if isinstance(node, Mapping)
                and (
                    str(node.get("node_kind") or "").strip() == "figure"
                    or str((_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip() == "figure"
                )
                for layout_id in list(node.get("source_layout_ids") or [])
                if str(layout_id).strip()
            ]
            + [
                str(atom.get("layout_id") or "").strip()
                for atom in layout_atoms
                if isinstance(atom, Mapping)
                and (
                    str(atom.get("layout_type") or "").strip() == "figure"
                    or str(atom.get("node_kind") or "").strip() == "figure"
                )
                and str(atom.get("layout_id") or "").strip()
            ]
        )
    )

    layout_atoms_by_id: Dict[str, Dict[str, Any]] = {
        str(atom.get("layout_id") or "").strip(): _jsonable_dict(atom)
        for atom in layout_atoms
        if isinstance(atom, Mapping) and str(atom.get("layout_id") or "").strip()
    }
    def _normalize_figure_layout_asset_id(raw: Any) -> str:
        token = str(raw or "").strip()
        if not token:
            return ""
        token = re.sub(r"[^0-9a-zA-Z_.-]+", "_", token).strip("_.-")
        return token[:96]

    figure_asset_ref_by_layout_id: Dict[str, str] = {}
    for asset in current_page_assets:
        asset_payload = _jsonable_dict(asset)
        href = str(asset_payload.get("href") or asset_payload.get("url") or "").strip()
        kind = str(asset_payload.get("kind") or "").strip().lower()
        meta = _jsonable_dict(asset_payload.get("meta") or {})
        if not href or kind != "image_hint":
            continue
        candidate_ids = [
            str(meta.get("layout_id") or "").strip(),
            str(meta.get("source_layout_id") or "").strip(),
            str(meta.get("layout_unique_id") or "").strip(),
            str(meta.get("asset_id") or "").strip(),
        ]
        for raw_layout_id in candidate_ids:
            if not raw_layout_id:
                continue
            figure_asset_ref_by_layout_id.setdefault(raw_layout_id, href)
            normalized_layout_id = _normalize_figure_layout_asset_id(raw_layout_id)
            if normalized_layout_id:
                figure_asset_ref_by_layout_id.setdefault(normalized_layout_id, href)

    inferred_paper_id = 0
    grounding_url_match = re.search(
        r"/api/v1/literature/reader/grounding-page-assets/(?P<paper_id>\d+)/(?P<page>\d+)$",
        page_image_url,
    )
    if grounding_url_match:
        inferred_paper_id = int(grounding_url_match.group("paper_id") or 0)
    if not inferred_paper_id:
        path_match = re.search(r"/paper_(\d+)(?:/|$)", page_image_path)
        if path_match:
            inferred_paper_id = int(path_match.group(1) or 0)

    def _resolve_figure_asset_ref_for_layout(layout_id: str, payload: Mapping[str, Any]) -> str:
        explicit_ref = str(
            payload.get("figure_asset_ref")
            or payload.get("media_asset_ref")
            or payload.get("asset_ref")
            or payload.get("page_asset_ref")
            or payload.get("asset_url")
            or payload.get("media_url")
            or payload.get("href")
            or payload.get("url")
            or ""
        ).strip()
        if explicit_ref:
            return explicit_ref

        normalized_layout_id = _normalize_figure_layout_asset_id(layout_id)
        mapped_ref = (
            figure_asset_ref_by_layout_id.get(str(layout_id).strip())
            or figure_asset_ref_by_layout_id.get(normalized_layout_id)
            or ""
        ).strip()
        if mapped_ref:
            return mapped_ref

        if inferred_paper_id <= 0 or not normalized_layout_id:
            return ""
        base_match = re.search(
            r"^(?P<base>https?://[^/]+)?(?P<prefix>/api/v1/literature/reader)/grounding-page-assets/\d+/\d+$",
            page_image_url,
        )
        if base_match:
            return (
                f"{str(base_match.group('base') or '').strip()}"
                f"{str(base_match.group('prefix') or '').strip()}"
                f"/figure-assets/{int(inferred_paper_id)}/{int(focus_page)}/{normalized_layout_id}"
            )
        return f"/api/v1/literature/reader/figure-assets/{int(inferred_paper_id)}/{int(focus_page)}/{normalized_layout_id}"

    def _collect_layout_ids_for_kind(kind: str) -> List[str]:
        normalized_kind = str(kind or "").strip()
        return list(
            dict.fromkeys(
                [
                    str(node_layout_id).strip()
                    for node in reading_nodes
                    if isinstance(node, Mapping)
                    and (
                        str(node.get("node_kind") or "").strip() == normalized_kind
                        or str((_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip() == normalized_kind
                    )
                    for node_layout_id in list(node.get("source_layout_ids") or [])
                    if str(node_layout_id).strip()
                ]
                + [
                    str(atom.get("layout_id") or "").strip()
                    for atom in layout_atoms
                    if isinstance(atom, Mapping)
                    and (
                        str(atom.get("layout_type") or "").strip() == normalized_kind
                        or str(atom.get("node_kind") or "").strip() == normalized_kind
                    )
                    and str(atom.get("layout_id") or "").strip()
                ]
            )
        )

    table_layout_ids = _collect_layout_ids_for_kind("table")
    equation_layout_ids = _collect_layout_ids_for_kind("equation")

    requested_node_kinds = {
        str(item).strip()
        for item in list(authored_payload.get("requested_node_kinds") or [])
        if str(item).strip()
    }
    unsupported_requested_kinds = sorted(requested_node_kinds - _PAGE_ARTIFACT_V2_SUPPORTED_NODE_KINDS)
    if unsupported_requested_kinds:
        raise ValueError(
            "requested artifact node kind not supported yet: "
            + ", ".join(unsupported_requested_kinds)
        )

    def _resolve_grounded_slot(
        *,
        slot_kind: str,
        row: Mapping[str, Any],
        available_layout_ids: Sequence[str],
        fallback_to_page_image: bool = False,
        require_concrete_figure_asset_ref: bool = False,
        allow_missing_evidence_binding: bool = False,
    ) -> Dict[str, Any]:
        payload = _jsonable_dict(row)
        requested_layout_id = str(
            payload.get("source_layout_id")
            or payload.get("layout_id")
            or payload.get("binding_layout_id")
            or ""
        ).strip()
        if requested_layout_id and requested_layout_id not in set(available_layout_ids):
            raise ValueError(f"media slot binding could not be resolved: {slot_kind}")
        binding_layout_id = requested_layout_id if requested_layout_id in set(available_layout_ids) else ""
        if not binding_layout_id and len(available_layout_ids) == 1:
            binding_layout_id = str(list(available_layout_ids)[0]).strip()
        binding_evidence_ids = list(evidence_by_layout.get(binding_layout_id, []) or [])[:8] if binding_layout_id else []
        if binding_layout_id:
            binding_kind = f"{slot_kind.replace('_slot', '')}_layout_anchor"
        elif fallback_to_page_image and page_image_url:
            binding_kind = "page_image_anchor"
        else:
            raise ValueError(
                f"media slot binding could not be resolved: {slot_kind}"
            )
        missing_binding_evidence = bool(binding_kind.endswith("_layout_anchor") and not binding_evidence_ids)
        if missing_binding_evidence and not allow_missing_evidence_binding:
            raise ValueError(
                f"media slot binding could not be resolved: {slot_kind}"
            )
        page_asset_ref = page_image_url
        if binding_kind.endswith("_layout_anchor") and require_concrete_figure_asset_ref:
            page_asset_ref = _resolve_figure_asset_ref_for_layout(binding_layout_id, payload)
            if not page_asset_ref:
                raise ValueError(f"media slot binding concrete asset ref missing: {slot_kind}")
        label = str(payload.get("label") or payload.get("title") or slot_kind).strip()
        caption = str(payload.get("caption") or payload.get("description") or "").strip()
        text = caption or label
        block_meta = {
            "label": label,
            "from": "authoring_plan",
            "binding_kind": binding_kind,
            "binding_layout_id": binding_layout_id,
            "binding_source_ref": str(payload.get("figure_ref") or requested_layout_id or label).strip(),
            "page_image_url": page_image_url,
            "page_asset_ref": page_asset_ref,
            "media_binding": {
                "binding_kind": binding_kind,
                "binding_layout_id": binding_layout_id,
                "binding_source_ref": str(payload.get("figure_ref") or requested_layout_id or label).strip(),
                "page_asset_ref": page_asset_ref,
                "page_image_url": page_image_url,
            },
            **_jsonable_dict(payload.get("meta") or {}),
        }
        if slot_kind == "figure_slot":
            block_meta["figure_binding"] = dict(block_meta["media_binding"])
        if missing_binding_evidence:
            block_meta["binding_resolution"] = "layout_anchor_without_evidence"
            block_meta["media_binding"]["binding_resolution"] = "layout_anchor_without_evidence"
            if slot_kind == "figure_slot":
                block_meta["figure_binding"]["binding_resolution"] = "layout_anchor_without_evidence"
        atom_payload = _jsonable_dict(layout_atoms_by_id.get(binding_layout_id) or {})
        if slot_kind == "table_slot" and atom_payload.get("table_cells"):
            table_cells = list(atom_payload.get("table_cells") or [])
            rows: List[List[str]] = []
            for cell in table_cells:
                cell_payload = _jsonable_dict(cell)
                row_index = int(cell_payload.get("row_start") or 0)
                while len(rows) <= row_index:
                    rows.append([])
                rows[row_index].append(str(cell_payload.get("text") or "").strip())
            block_meta["table_rows"] = rows
        return {
            "segment_kind": slot_kind,
            "text": text or f"{slot_kind} content",
            "source_layout_ids": [binding_layout_id] if binding_layout_id else [],
            "source_block_ids": [],
            "evidence_ids": binding_evidence_ids,
            "meta": block_meta,
        }

    blocks: List[Dict[str, Any]] = []
    main_segment_ids: List[str] = []
    skipped_slot_bindings: List[Dict[str, Any]] = []
    authored_explanations = [
        str(item).strip()
        for item in list(authored_payload.get("authored_explanations") or [])
        if str(item).strip()
    ]
    authored_text_blocks = [
        {
            "segment_kind": str(_jsonable_dict(item).get("segment_kind") or "").strip(),
            "text": str(_jsonable_dict(item).get("text") or "").strip(),
            "meta": _jsonable_dict(_jsonable_dict(item).get("meta") or {}),
        }
        for item in list(authored_payload.get("authored_text_blocks") or [])
        if isinstance(item, Mapping)
    ]
    if not authored_text_blocks:
        authored_text_blocks = [
            {
                "segment_kind": "authored_explanation",
                "text": text,
                "meta": {"from": "authoring_plan"},
            }
            for text in authored_explanations
        ]

    draft_node_sequence = [
        _jsonable_dict(item)
        for item in list(_jsonable_dict(authored_payload.get("meta") or {}).get("draft_node_sequence") or [])
        if isinstance(item, Mapping)
    ]

    def _next_segment_id(prefix: str, counters: Dict[str, int]) -> str:
        counters[prefix] = int(counters.get(prefix) or 0) + 1
        return f"{prefix}-{counters[prefix]}"

    def _append_grounded_slot_block(
        *,
        segment_id: str,
        slot_kind: str,
        row: Mapping[str, Any],
        available_layout_ids: Sequence[str],
        fallback_to_page_image: bool = False,
        require_concrete_figure_asset_ref: bool = False,
        allow_missing_evidence_binding: bool = False,
    ) -> bool:
        try:
            resolved = _resolve_grounded_slot(
                slot_kind=slot_kind,
                row=row,
                available_layout_ids=available_layout_ids,
                fallback_to_page_image=fallback_to_page_image,
                require_concrete_figure_asset_ref=require_concrete_figure_asset_ref,
                allow_missing_evidence_binding=allow_missing_evidence_binding,
            )
        except ValueError as exc:
            detail = str(exc).strip()
            if detail.startswith("media slot binding could not be resolved:"):
                skipped_slot_bindings.append(
                    {
                        "segment_id": segment_id,
                        "slot_kind": slot_kind,
                        "label": str(_jsonable_dict(row).get("label") or _jsonable_dict(row).get("title") or slot_kind).strip(),
                        "source_layout_id": str(
                            _jsonable_dict(row).get("source_layout_id")
                            or _jsonable_dict(row).get("layout_id")
                            or _jsonable_dict(row).get("binding_layout_id")
                            or ""
                        ).strip(),
                        "reason": detail,
                    }
                )
                return False
            raise
        blocks.append(
            {
                "segment_id": segment_id,
                "source_lane": "authoring_plan",
                "page": focus_page,
                **resolved,
            }
        )
        return True

    if draft_node_sequence:
        segment_counters: Dict[str, int] = {}
        used_excerpt_tokens: Set[str] = set()

        def _resolve_selected_excerpt(node_payload: Mapping[str, Any]) -> Dict[str, Any]:
            matched = _resolve_excerpt_against_current_page_grounding(
                override=node_payload,
                candidate_excerpt_rows=excerpt_rows,
                reading_nodes=reading_nodes,
                layout_atoms=layout_atoms,
                seen_excerpt_tokens=used_excerpt_tokens,
                max_chars=reader_excerpt_max_chars,
            )
            if matched:
                return matched
            raise ValueError("artifact draft generation failed: draft-selected excerpts could not be resolved against current-page grounding")

        for node in draft_node_sequence:
            node_kind = str(node.get("node_kind") or "").strip()
            node_meta = _jsonable_dict(node.get("meta") or {})
            if node_kind in {"heading", "paragraph", "authored_explanation"}:
                segment_kind = node_kind if node_kind in {"heading", "paragraph"} else "authored_explanation"
                blocks.append(
                    {
                        "segment_id": _next_segment_id("seg-explain", segment_counters),
                        "segment_kind": segment_kind,
                        "source_lane": "authoring_plan",
                        "page": focus_page,
                        "text": str(node.get("text") or "").strip(),
                        "source_layout_ids": [],
                        "source_block_ids": [],
                        "evidence_ids": [],
                        "meta": {"from": "authoring_plan", **node_meta},
                    }
                )
                continue
            if node_kind == "original_excerpt":
                row = _resolve_selected_excerpt(node)
                evidence_ids: List[str] = []
                for layout_id in list(row.get("layout_ids") or []):
                    for evidence_id in evidence_by_layout.get(layout_id, []):
                        if evidence_id not in evidence_ids:
                            evidence_ids.append(evidence_id)
                segment_id = _next_segment_id("seg-excerpt", segment_counters)
                main_segment_ids.append(segment_id)
                blocks.append(
                    {
                        "segment_id": segment_id,
                        "segment_kind": "original_excerpt",
                        "source_lane": "current_page",
                        "page": focus_page,
                        "text": str(row.get("text") or ""),
                        "source_layout_ids": list(row.get("layout_ids") or []),
                        "source_block_ids": list(row.get("block_ids") or []),
                        "evidence_ids": evidence_ids[:8],
                        "meta": {
                            "from": "reading_dossier_v2.current_page",
                            "source_node_id": str(row.get("node_id") or "").strip(),
                            **_jsonable_dict(row.get("meta") or {}),
                        },
                    }
                )
                continue
            if node_kind in {"figure_slot", "table_slot", "equation_slot"}:
                available_layout_ids = (
                    figure_layout_ids if node_kind == "figure_slot"
                    else table_layout_ids if node_kind == "table_slot"
                    else equation_layout_ids
                )
                prefix = "seg-figure" if node_kind == "figure_slot" else "seg-table" if node_kind == "table_slot" else "seg-equation"
                _append_grounded_slot_block(
                    segment_id=_next_segment_id(prefix, segment_counters),
                    slot_kind=node_kind,
                    row=node,
                    available_layout_ids=available_layout_ids,
                    fallback_to_page_image=(node_kind == "figure_slot"),
                    require_concrete_figure_asset_ref=(node_kind == "figure_slot"),
                    allow_missing_evidence_binding=True,
                )
                continue
            if node_kind == "aside":
                aside_text = str(node.get("text") or "").strip()
                if not aside_text:
                    raise ValueError("requested artifact node kind not supported yet: aside_content requires text")
                blocks.append(
                    {
                        "segment_id": _next_segment_id("seg-aside", segment_counters),
                        "segment_kind": "aside_content",
                        "source_lane": "authoring_plan",
                        "page": focus_page,
                        "text": aside_text,
                        "source_layout_ids": [],
                        "source_block_ids": [],
                        "evidence_ids": [],
                        "meta": {
                            "label": str(node.get("label") or "Aside").strip(),
                            "from": "authoring_plan",
                            **node_meta,
                        },
                    }
                )
                continue
            if node_kind == "term_note":
                term = str(node.get("term") or node.get("label") or "术语").strip()
                definition = str(node.get("definition") or node.get("text") or "").strip()
                text = f"{term}: {definition}".strip(": ").strip()
                blocks.append(
                    {
                        "segment_id": _next_segment_id("seg-term", segment_counters),
                        "segment_kind": "term_annotation",
                        "source_lane": "authoring_plan",
                        "page": focus_page,
                        "text": text or term,
                        "source_layout_ids": [],
                        "source_block_ids": [],
                        "evidence_ids": [],
                        "meta": {"term": term, "from": "authoring_plan", **node_meta},
                    }
                )
                continue
            if node_kind == "external_resource":
                resolved_resources = [
                    _jsonable_dict(item)
                    for item in list(node.get("resolved_resources") or [])
                    if isinstance(item, Mapping)
                ]
                if not resolved_resources:
                    raise ValueError("artifact draft generation blocked by missing retrieval result: external_resource resolved_resources unavailable")
                for entry in resolved_resources:
                    url = str(entry.get("url") or "").strip()
                    if not url:
                        raise ValueError("artifact draft generation blocked by missing retrieval result: external_resource resolved url missing")
                    blocks.append(
                        {
                            "segment_id": _next_segment_id("seg-resource", segment_counters),
                            "segment_kind": "external_resource",
                            "source_lane": "authoring_plan",
                            "page": focus_page,
                            "text": str(node.get("label") or entry.get("label") or url).strip(),
                            "source_layout_ids": [],
                            "source_block_ids": [],
                            "evidence_ids": [],
                            "meta": {
                                "url": url,
                                "resource_type": str(entry.get("resource_type") or "").strip(),
                                "from": "authoring_plan",
                                **node_meta,
                            },
                        }
                    )
                continue
            raise ValueError(f"requested artifact node kind not supported yet: {node_kind}")

        authored_text_blocks = []
        excerpt_rows = []
        authored_payload["figure_slots"] = []
        authored_payload["table_slots"] = []
        authored_payload["equation_slots"] = []
        authored_payload["media_slots"] = []
        authored_payload["term_annotations"] = []
        authored_payload["external_resources"] = []
        authored_payload["aside_blocks"] = []

    for idx, row in enumerate(excerpt_rows, start=1):
        evidence_ids: List[str] = []
        for layout_id in list(row.get("layout_ids") or []):
            for evidence_id in evidence_by_layout.get(layout_id, []):
                if evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
        segment_id = f"seg-excerpt-{idx}"
        main_segment_ids.append(segment_id)
        blocks.append(
            {
                "segment_id": segment_id,
                "segment_kind": "original_excerpt",
                "source_lane": "current_page",
                "page": focus_page,
                "text": str(row.get("text") or ""),
                "source_layout_ids": list(row.get("layout_ids") or []),
                "source_block_ids": list(row.get("block_ids") or []),
                "evidence_ids": evidence_ids[:8],
                "meta": {
                    "from": "reading_dossier_v2.current_page",
                    "source_node_id": str(row.get("node_id") or "").strip(),
                    **_jsonable_dict(row.get("meta") or {}),
                },
            }
        )
        if idx <= len(authored_text_blocks):
            authored_block = _jsonable_dict(authored_text_blocks[idx - 1])
            blocks.append(
                {
                    "segment_id": f"seg-explain-{idx}",
                    "segment_kind": str(authored_block.get("segment_kind") or "authored_explanation").strip() or "authored_explanation",
                    "source_lane": "authoring_plan",
                    "page": focus_page,
                    "text": str(authored_block.get("text") or "").strip(),
                    "source_layout_ids": [],
                    "source_block_ids": [],
                    "evidence_ids": [],
                    "meta": {"from": "authoring_plan", **_jsonable_dict(authored_block.get("meta") or {})},
                }
            )

    for idx in range(len(excerpt_rows), len(authored_text_blocks)):
        authored_block = _jsonable_dict(authored_text_blocks[idx])
        blocks.append(
            {
                "segment_id": f"seg-explain-{idx + 1}",
                "segment_kind": str(authored_block.get("segment_kind") or "authored_explanation").strip() or "authored_explanation",
                "source_lane": "authoring_plan",
                "page": focus_page,
                "text": str(authored_block.get("text") or "").strip(),
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {"from": "authoring_plan", **_jsonable_dict(authored_block.get("meta") or {})},
            }
        )

    authored_figure_slots = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("figure_slots") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_figure_slots, start=1):
        _append_grounded_slot_block(
            segment_id=f"seg-figure-{idx}",
            slot_kind="figure_slot",
            row=row,
            available_layout_ids=figure_layout_ids,
            fallback_to_page_image=True,
            require_concrete_figure_asset_ref=True,
            allow_missing_evidence_binding=True,
        )

    authored_table_slots = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("table_slots") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_table_slots, start=1):
        _append_grounded_slot_block(
            segment_id=f"seg-table-{idx}",
            slot_kind="table_slot",
            row=row,
            available_layout_ids=table_layout_ids,
            allow_missing_evidence_binding=True,
        )

    authored_equation_slots = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("equation_slots") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_equation_slots, start=1):
        _append_grounded_slot_block(
            segment_id=f"seg-equation-{idx}",
            slot_kind="equation_slot",
            row=row,
            available_layout_ids=equation_layout_ids,
            allow_missing_evidence_binding=True,
        )

    authored_media_slots = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("media_slots") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_media_slots, start=1):
        media_type = str(row.get("media_type") or "").strip()
        available_layout_ids: List[str]
        if media_type == "table":
            available_layout_ids = table_layout_ids
        elif media_type == "equation":
            available_layout_ids = equation_layout_ids
        elif media_type == "figure":
            available_layout_ids = figure_layout_ids
        else:
            raise ValueError(f"requested artifact node kind not supported yet: media_slot:{media_type or 'unknown'}")
        _append_grounded_slot_block(
            segment_id=f"seg-media-{idx}",
            slot_kind="media_slot",
            row=row,
            available_layout_ids=available_layout_ids,
            fallback_to_page_image=(media_type == "figure"),
            require_concrete_figure_asset_ref=(media_type == "figure"),
            allow_missing_evidence_binding=True,
        )

    authored_term_annotations = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("term_annotations") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_term_annotations, start=1):
        term = str(row.get("term") or f"term_{idx}").strip()
        definition = str(row.get("definition") or row.get("explanation") or "").strip()
        text = f"{term}: {definition}".strip(": ").strip()
        blocks.append(
            {
                "segment_id": f"seg-term-{idx}",
                "segment_kind": "term_annotation",
                "source_lane": "authoring_plan",
                "page": focus_page,
                "text": text or term,
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {"term": term, "from": "authoring_plan", **_jsonable_dict(row.get("meta") or {})},
            }
        )

    authored_external_resources = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("external_resources") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_external_resources, start=1):
        blocks.append(
            {
                "segment_id": f"seg-resource-{idx}",
                "segment_kind": "external_resource",
                "source_lane": "authoring_plan",
                "page": focus_page,
                "text": str(row.get("label") or row.get("title") or f"External resource {idx}").strip(),
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {
                    "url": str(row.get("url") or "").strip(),
                    "resource_type": str(row.get("resource_type") or "").strip(),
                    "from": "authoring_plan",
                    **_jsonable_dict(row.get("meta") or {}),
                },
            }
        )

    authored_aside_blocks = [
        _jsonable_dict(item)
        for item in list(authored_payload.get("aside_blocks") or [])
        if isinstance(item, Mapping)
    ]
    for idx, row in enumerate(authored_aside_blocks, start=1):
        aside_text = str(row.get("text") or row.get("body") or row.get("title") or "").strip()
        if not aside_text:
            raise ValueError("requested artifact node kind not supported yet: aside_content requires text")
        blocks.append(
            {
                "segment_id": f"seg-aside-{idx}",
                "segment_kind": "aside_content",
                "source_lane": "authoring_plan",
                "page": focus_page,
                "text": aside_text,
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {
                    "label": str(row.get("label") or row.get("title") or f"Aside {idx}").strip(),
                    "from": "authoring_plan",
                    **_jsonable_dict(row.get("meta") or {}),
                },
            }
        )

    adjacent_pages = [
        int(item.get("page") or 0)
        for item in list((_jsonable_dict(dossier_payload.get("adjacent_pages") or {})).get("pages") or [])
        if isinstance(item, Mapping) and int(item.get("page") or 0) > 0
    ]
    adjacent_lane = _jsonable_dict(dossier_payload.get("adjacent_pages") or {})
    reading_node_ids = list(expected_reading_node_ids)
    layout_ids = list(
        dict.fromkeys(
            [
                str(layout_id).strip()
                for layout_id in expected_layout_ids
                if str(layout_id).strip()
            ]
        )
    )
    block_ids = list(
        dict.fromkeys(
            [
                str(block_id).strip()
                for block_id in expected_block_ids
                if str(block_id).strip()
            ]
        )
    )
    evidence_ids = list(
        dict.fromkeys(
            [
                str(evidence_id).strip()
                for evidence_id in expected_evidence_ids
                if str(evidence_id).strip()
            ]
        )
    )
    if not any([reading_node_ids, layout_ids, block_ids, evidence_ids]):
        raise ValueError("page_artifact_v2 requires current-page spine anchors")

    signature = str(dossier_signature or "").strip() or _reading_dossier_v2_signature(dossier_payload)
    authored_meta = _jsonable_dict(authored_payload.get("meta") or {})
    artifact_payload = {
        "version": "page_artifact_v2",
        "artifact_contract_id": "page_artifact_v2.contract.v1",
        "focus_page": focus_page,
        "reader_profile": str(dossier_payload.get("reader_profile") or "curious_generalist").strip() or "curious_generalist",
        "dossier_signature": signature,
        "session_id": str(session_id or "").strip() or None,
        "template_id": str(authored_payload.get("template_id") or "").strip(),
        "layout_recipe": str(authored_payload.get("layout_recipe") or "").strip(),
        "presentation_mode": str(authored_payload.get("presentation_mode") or "").strip(),
        "widget_family": str(authored_payload.get("widget_family") or "").strip(),
        "motion_preset": str(authored_payload.get("motion_preset") or "").strip(),
        "interaction_policy": str(authored_payload.get("interaction_policy") or "").strip(),
        "reading_blocks": blocks,
        "current_page_spine": {
            "page": focus_page,
            "owner": "reading_dossier_v2.current_page",
            "primary": True,
            "reading_node_ids": reading_node_ids,
            "layout_ids": layout_ids,
            "block_ids": block_ids,
            "evidence_ids": evidence_ids,
            "main_segment_ids": main_segment_ids,
            "meta": {
                "dominant_lane": "current_page",
                "coverage_mode": "draft_selected_excerpt_rows",
                "selected_excerpt_count": len(excerpt_rows),
                "candidate_excerpt_count": len(candidate_excerpt_rows),
                "represented_excerpt_count": len(main_segment_ids),
                "coverage_ratio": float(len(main_segment_ids) / len(candidate_excerpt_rows)) if candidate_excerpt_rows else 0.0,
                "excerpt_coverage": {
                    "available_main_flow_count": len(candidate_excerpt_rows),
                    "covered_main_flow_count": len(main_segment_ids),
                    "coverage_ratio": float(len(main_segment_ids) / len(candidate_excerpt_rows)) if candidate_excerpt_rows else 0.0,
                },
            },
        },
        "provenance": {
            "continuity_mode": "current_page_primary_ordered_adjacent_context",
            "adjacent_context_pages": adjacent_pages[:6],
            "include_adjacent_as_coequal_anchor": False,
            "source_lanes": {
                "current_page": _jsonable_dict(dossier_payload.get("current_page") or {}),
                "adjacent_pages_meta": {
                    "owner": str(adjacent_lane.get("owner") or "").strip(),
                    "fidelity": str(adjacent_lane.get("fidelity") or "").strip(),
                    "reference_only": bool((_jsonable_dict(adjacent_lane.get("limits") or {})).get("reference_only", False)),
                    "count": len(adjacent_pages),
                },
            },
            "meta": {"adjacent_context_is_latent": True},
        },
        "meta": {
            "dossier_contract": str(dossier_payload.get("dossier_contract") or "").strip(),
            "artifact_build_mode": "phase3_model_draft_promotion",
            **({"skipped_slot_bindings": skipped_slot_bindings} if skipped_slot_bindings else {}),
            **(
                {"reader_opening": _jsonable_dict(authored_meta.get("reader_opening") or {})}
                if _jsonable_dict(authored_meta.get("reader_opening") or {})
                else {}
            ),
            **(
                {"reader_outro": _jsonable_dict(authored_meta.get("reader_outro") or {})}
                if _jsonable_dict(authored_meta.get("reader_outro") or {})
                else {}
            ),
        },
    }
    return PageArtifactV2.model_validate(artifact_payload).model_dump(mode="json")


def _validate_page_artifact_v2_contract(payload: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        artifact = PageArtifactV2.model_validate(_jsonable_dict(payload))
    except Exception as exc:
        return {
            "valid": False,
            "renderable": False,
            "errors": [str(exc)],
        }

    data = artifact.model_dump(mode="json")
    errors: List[str] = []
    required_presentation_fields = (
        "template_id",
        "layout_recipe",
        "presentation_mode",
        "widget_family",
        "motion_preset",
        "interaction_policy",
    )
    for field in required_presentation_fields:
        if not str(data.get(field) or "").strip():
            errors.append(f"missing presentation field: {field}")

    original_blocks = [
        item for item in list(data.get("reading_blocks") or [])
        if str(item.get("segment_kind") or "") == "original_excerpt"
    ]
    figure_blocks = [
        item for item in list(data.get("reading_blocks") or [])
        if str(item.get("segment_kind") or "") == "figure_slot"
    ]
    table_blocks = [
        item for item in list(data.get("reading_blocks") or [])
        if str(item.get("segment_kind") or "") == "table_slot"
    ]
    equation_blocks = [
        item for item in list(data.get("reading_blocks") or [])
        if str(item.get("segment_kind") or "") == "equation_slot"
    ]
    media_blocks = [
        item for item in list(data.get("reading_blocks") or [])
        if str(item.get("segment_kind") or "") == "media_slot"
    ]
    if not original_blocks:
        errors.append("missing original excerpt blocks")
    for block in original_blocks:
        if str(block.get("source_lane") or "") != "current_page":
            errors.append("original excerpt block must come from current_page lane")
            break
        if int(block.get("page") or 0) != int(data.get("focus_page") or 0):
            errors.append("original excerpt block page must match focus_page")
            break

    spine = _jsonable_dict(data.get("current_page_spine") or {})
    if not bool(spine.get("primary", False)):
        errors.append("current_page_spine must be primary")
    if not list(spine.get("main_segment_ids") or []):
        errors.append("current_page_spine main_segment_ids missing")
    original_ids = {str(item.get("segment_id") or "").strip() for item in original_blocks}
    spine_ids = {str(item).strip() for item in list(spine.get("main_segment_ids") or []) if str(item).strip()}
    if spine_ids and any(segment_id not in original_ids for segment_id in spine_ids):
        errors.append("current_page_spine main_segment_ids must not include non-original blocks")

    represented_node_ids = {
        str((_jsonable_dict(item.get("meta") or {})).get("source_node_id") or "").strip()
        for item in original_blocks
        if str((_jsonable_dict(item.get("meta") or {})).get("source_node_id") or "").strip()
    }
    expected_node_ids = {str(item).strip() for item in list(spine.get("reading_node_ids") or []) if str(item).strip()}

    represented_layout_ids = {
        str(layout_id).strip()
        for item in original_blocks
        for layout_id in list(item.get("source_layout_ids") or [])
        if str(layout_id).strip()
    }
    expected_layout_ids = {str(item).strip() for item in list(spine.get("layout_ids") or []) if str(item).strip()}

    spine_meta = _jsonable_dict(spine.get("meta") or {})
    represented_excerpt_count = int(spine_meta.get("represented_excerpt_count") or len(original_blocks))
    coverage_ratio = float(
        (_jsonable_dict(spine_meta.get("excerpt_coverage") or {})).get("coverage_ratio")
        or spine_meta.get("coverage_ratio")
        or 0.0
    )
    if represented_excerpt_count <= 0:
        errors.append("current_page_spine must preserve at least one draft-selected current-page excerpt")
    elif expected_node_ids and represented_node_ids and not expected_node_ids.intersection(represented_node_ids) and coverage_ratio <= 0:
        errors.append("original excerpt blocks must preserve at least one current-page reading node anchor")
    elif expected_layout_ids and represented_layout_ids and not expected_layout_ids.intersection(represented_layout_ids) and coverage_ratio <= 0:
        errors.append("original excerpt blocks must preserve at least one current-page layout anchor")

    provenance = _jsonable_dict(data.get("provenance") or {})
    current_lane = _jsonable_dict((_jsonable_dict(provenance.get("source_lanes") or {})).get("current_page") or {})
    rich_grounding = _jsonable_dict(current_lane.get("rich_grounding") or {})
    figure_layout_ids = {
        str(layout_id).strip()
        for node in list(rich_grounding.get("reading_nodes") or [])
        if isinstance(node, Mapping)
        and (
            str(node.get("node_kind") or "").strip() == "figure"
            or str((_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip() == "figure"
        )
        for layout_id in list(node.get("source_layout_ids") or [])
        if str(layout_id).strip()
    }
    figure_layout_ids.update(
        {
            str(atom.get("layout_id") or "").strip()
            for atom in list(rich_grounding.get("layout_atoms") or [])
            if isinstance(atom, Mapping)
            and (
                str(atom.get("layout_type") or "").strip() == "figure"
                or str(atom.get("node_kind") or "").strip() == "figure"
            )
            and str(atom.get("layout_id") or "").strip()
        }
    )
    evidence_by_layout: Dict[str, List[str]] = {}
    for item in list(rich_grounding.get("evidence_map") or []):
        if not isinstance(item, Mapping):
            continue
        layout_id = str(item.get("source_layout_id") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not layout_id or not evidence_id:
            continue
        evidence_by_layout.setdefault(layout_id, []).append(evidence_id)
    page_image_url = str((_jsonable_dict(rich_grounding.get("page_image") or {})).get("url") or "").strip()
    table_layout_ids = {
        str(layout_id).strip()
        for node in list(rich_grounding.get("reading_nodes") or [])
        if isinstance(node, Mapping)
        and (
            str(node.get("node_kind") or "").strip() == "table"
            or str((_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip() == "table"
        )
        for layout_id in list(node.get("source_layout_ids") or [])
        if str(layout_id).strip()
    }
    table_layout_ids.update(
        {
            str(atom.get("layout_id") or "").strip()
            for atom in list(rich_grounding.get("layout_atoms") or [])
            if isinstance(atom, Mapping)
            and (
                str(atom.get("layout_type") or "").strip() == "table"
                or str(atom.get("node_kind") or "").strip() == "table"
            )
            and str(atom.get("layout_id") or "").strip()
        }
    )
    equation_layout_ids = {
        str(layout_id).strip()
        for node in list(rich_grounding.get("reading_nodes") or [])
        if isinstance(node, Mapping)
        and (
            str(node.get("node_kind") or "").strip() == "equation"
            or str((_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip() == "equation"
        )
        for layout_id in list(node.get("source_layout_ids") or [])
        if str(layout_id).strip()
    }
    equation_layout_ids.update(
        {
            str(atom.get("layout_id") or "").strip()
            for atom in list(rich_grounding.get("layout_atoms") or [])
            if isinstance(atom, Mapping)
            and (
                str(atom.get("layout_type") or "").strip() == "equation"
                or str(atom.get("node_kind") or "").strip() == "equation"
            )
            and str(atom.get("layout_id") or "").strip()
        }
    )

    def _validate_bound_slot_blocks(
        slot_kind: str,
        blocks_to_validate: Sequence[Mapping[str, Any]],
        allowed_layout_ids: Set[str],
        allow_page_image_anchor: bool = False,
    ) -> None:
        for block in blocks_to_validate:
            meta = _jsonable_dict(block.get("meta") or {})
            binding = _jsonable_dict(meta.get("media_binding") or meta.get("figure_binding") or {})
            binding_kind = str(meta.get("binding_kind") or binding.get("binding_kind") or "").strip()
            bound_layout_ids = [str(layout_id).strip() for layout_id in list(block.get("source_layout_ids") or []) if str(layout_id).strip()]
            page_asset_ref = str(binding.get("page_asset_ref") or meta.get("page_asset_ref") or meta.get("page_image_url") or page_image_url).strip()
            if not binding_kind:
                errors.append(f"{slot_kind} block must declare binding_kind")
                return
            if not page_asset_ref:
                errors.append(f"{slot_kind} block must include a renderable current-page asset binding")
                return
            if binding_kind.endswith("_layout_anchor"):
                if not bound_layout_ids:
                    errors.append(f"{slot_kind} block must bind to at least one current-page layout")
                    return
                if any(layout_id not in allowed_layout_ids for layout_id in bound_layout_ids):
                    errors.append(f"{slot_kind} block binding must resolve to a current-page {slot_kind.replace('_slot', '')} layout")
                    return
                expected_bound_evidence = {
                    evidence_id
                    for layout_id in bound_layout_ids
                    for evidence_id in list(evidence_by_layout.get(layout_id, []) or [])
                    if str(evidence_id).strip()
                }
                bound_evidence_ids = {str(item).strip() for item in list(block.get("evidence_ids") or []) if str(item).strip()}
                if expected_bound_evidence and not bound_evidence_ids.intersection(expected_bound_evidence):
                    errors.append(f"{slot_kind} block must carry evidence binding for its current-page anchor")
                    return
                requires_concrete_figure_asset = (
                    slot_kind == "figure_slot"
                    or (
                        slot_kind == "media_slot"
                        and any(layout_id in figure_layout_ids for layout_id in bound_layout_ids)
                    )
                )
                if requires_concrete_figure_asset and page_asset_ref == page_image_url:
                    errors.append(f"{slot_kind} block must carry concrete figure/media asset ref for deterministic layout binding")
                    return
            elif not (allow_page_image_anchor and binding_kind == "page_image_anchor"):
                errors.append(f"{slot_kind} block binding_kind is unsupported")
                return

    _validate_bound_slot_blocks("figure_slot", figure_blocks, figure_layout_ids, allow_page_image_anchor=True)
    _validate_bound_slot_blocks("table_slot", table_blocks, table_layout_ids)
    _validate_bound_slot_blocks("equation_slot", equation_blocks, equation_layout_ids)
    _validate_bound_slot_blocks("media_slot", media_blocks, figure_layout_ids | table_layout_ids | equation_layout_ids, allow_page_image_anchor=True)
    if bool(provenance.get("include_adjacent_as_coequal_anchor", False)):
        errors.append("adjacent pages cannot be co-equal anchors")

    return {
        "valid": not errors,
        "renderable": not errors,
        "errors": errors,
        "artifact": data,
    }


def _is_page_artifact_v2_renderable(payload: Mapping[str, Any]) -> bool:
    report = _validate_page_artifact_v2_contract(payload)
    return bool(report.get("renderable"))


def _attach_concrete_experience_context_to_payload(
    *,
    payload: Mapping[str, Any] | None,
    focus_page: int,
    compose_payload: Mapping[str, Any],
    adjacent_page_context: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    normalized = _jsonable_dict(payload)
    if not normalized:
        return {}
    meta = dict(normalized.get("meta") or {})
    meta["page_dossier"] = _build_experience_page_dossier(
        focus_page=int(focus_page),
        compose_payload=compose_payload,
        adjacent_page_context=adjacent_page_context,
    )
    if adjacent_page_context:
        meta["adjacent_page_context"] = [
            _jsonable_dict(row)
            for row in list(adjacent_page_context or [])
            if isinstance(row, Mapping)
        ]
    normalized["meta"] = meta
    return normalized


def _extract_adjacent_page_context_from_plan_meta(plan_payload: Mapping[str, Any] | None) -> List[Dict[str, Any]]:
    if not isinstance(plan_payload, Mapping):
        return []
    meta = plan_payload.get("meta")
    if not isinstance(meta, Mapping):
        return []
    rows: List[Dict[str, Any]] = []
    for item in list(meta.get("adjacent_page_context") or []):
        if not isinstance(item, Mapping):
            continue
        rows.append({
            "page": int(item.get("page") or 0),
            "relation": str(item.get("relation") or "").strip(),
            "reference_only": bool(item.get("reference_only")),
            "source": str(item.get("source") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
            "body_text": str(item.get("body_text") or "").strip(),
            "figures": list(item.get("figures") or []),
            "tables": list(item.get("tables") or []),
            "equations": list(item.get("equations") or []),
            "continuation_hints": [
                str(row).strip()
                for row in list(item.get("continuation_hints") or [])
                if str(row).strip()
            ],
            "raw_text": str(item.get("raw_text") or "").strip(),
        })
    return [row for row in rows if int(row.get("page") or 0) > 0]


def _extract_page_dossier_from_plan_meta(plan_payload: Mapping[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(plan_payload, Mapping):
        return {}
    meta = plan_payload.get("meta")
    if not isinstance(meta, Mapping):
        return {}
    dossier = meta.get("page_dossier")
    if not isinstance(dossier, Mapping):
        return {}
    return json.loads(json.dumps(dossier, ensure_ascii=False, default=str))


async def _build_experience_adjacent_page_context(
    *,
    compose_service: Any,
    paper: Paper,
    focus_page: int,
) -> List[Dict[str, Any]]:
    pdf_path = compose_service._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
        user_id=int(paper.user_id),
        paper_id=int(paper.id),
        paper_title=paper.title,
        paper_pdf_path=paper.pdf_path,
    )
    if not pdf_path or not os.path.exists(pdf_path):
        return []

    max_page = await _get_pdf_page_count(pdf_path)
    if not max_page or max_page <= 1:
        return []

    refs: List[Dict[str, Any]] = []
    for relation, page_num in (("previous_page", int(focus_page) - 1), ("next_page", int(focus_page) + 1)):
        if page_num < 1 or page_num > int(max_page):
            continue
        try:
            await compose_service.ensure_page_render_asset(
                paper_id=int(paper.id),
                page=int(page_num),
                pdf_path=str(pdf_path),
            )
            image_path = compose_service._find_existing_page_render_asset_path(  # pylint: disable=protected-access
                paper_id=int(paper.id),
                page=int(page_num),
            )
        except Exception as exc:
            logger.warning(
                f"[Literature Experience] adjacent page render failed paper={paper.id} page={page_num} relation={relation}: {exc}"
            )
            continue
        item = await _extract_adjacent_page_reference_text(
            image_path=str(image_path or ""),
            relation=relation,
            page=int(page_num),
        )
        if item:
            refs.append(item)
    return refs


async def _build_experience_adjacent_page_structured_context_v2(
    *,
    compose_service: Any,
    paper: Paper,
    focus_page: int,
    current_user: Optional[User] = None,
) -> List[Dict[str, Any]]:
    pdf_path = compose_service._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
        user_id=int(paper.user_id),
        paper_id=int(paper.id),
        paper_title=paper.title,
        paper_pdf_path=paper.pdf_path,
    )
    if not pdf_path or not os.path.exists(pdf_path):
        logger.warning(
            f"[Literature Experience] adjacent structured context skipped: pdf path missing paper={paper.id} focus_page={focus_page}"
        )
        return []

    max_page = await _get_pdf_page_count(pdf_path)
    if not max_page or max_page <= 1:
        return []

    rows: List[Dict[str, Any]] = []
    for relation, page_num in (("previous_page", int(focus_page) - 1), ("next_page", int(focus_page) + 1)):
        if page_num < 1 or page_num > int(max_page):
            continue
        try:
            asset_url = await compose_service.ensure_page_render_asset(
                paper_id=int(paper.id),
                page=int(page_num),
                pdf_path=str(pdf_path),
            )
            image_path = compose_service._find_existing_page_render_asset_path(  # pylint: disable=protected-access
                paper_id=int(paper.id),
                page=int(page_num),
            )
            if not image_path:
                raise ValueError(f"render asset missing for page {page_num}")
        except Exception as exc:
            logger.warning(
                f"[Literature Experience] adjacent structured context skipped paper={paper.id} focus_page={focus_page} "
                f"page={page_num} relation={relation}: {exc}"
            )
            continue
        cache_key = _adjacent_page_structured_v2_cache_key(
            paper_id=int(paper.id),
            page=int(page_num),
            relation=relation,
            image_path=str(image_path),
            image_url=str(asset_url or "").strip(),
        )
        cached_row, cache_layer = await _adjacent_page_structured_v2_cache_get(cache_key)
        row: Optional[Dict[str, Any]] = None
        if isinstance(cached_row, Mapping):
            try:
                row = ReadingDossierV2AdjacentPageRow.model_validate(_jsonable_dict(cached_row)).model_dump(mode="json")
                row["meta"] = {
                    **_jsonable_dict(row.get("meta") or {}),
                    "page_scope_cache_key": cache_key,
                    "page_scope_cache_layer": cache_layer,
                }
            except Exception as exc:
                logger.warning(
                    f"[Literature Experience] adjacent structured cache invalid, rebuilding paper={paper.id} "
                    f"focus_page={focus_page} page={page_num} relation={relation}: {exc}"
                )
                row = None

        if row is None:
            try:
                row = await _extract_adjacent_page_structured_context_v2(
                    image_path=str(image_path),
                    relation=relation,
                    page=int(page_num),
                    image_url=str(asset_url or "").strip(),
                )
                try:
                    await _adjacent_page_structured_v2_cache_set(
                        cache_key,
                        row,
                        user_id=int(getattr(current_user, "id", 0) or getattr(paper, "user_id", 0) or 0),
                        paper_id=int(paper.id),
                        page=int(page_num),
                    )
                except Exception as cache_exc:
                    logger.warning(
                        f"[Literature Experience] adjacent structured cache write failed paper={paper.id} "
                        f"focus_page={focus_page} page={page_num} relation={relation}: {cache_exc}"
                    )
                row["meta"] = {
                    **_jsonable_dict(row.get("meta") or {}),
                    "page_scope_cache_key": cache_key,
                    "page_scope_cache_layer": "built",
                }
            except Exception as exc:
                logger.warning(
                    f"[Literature Experience] adjacent structured extraction skipped paper={paper.id} "
                    f"focus_page={focus_page} page={page_num} relation={relation}: {exc}"
                )
                continue
        rows.append(row)
    return rows


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


def _generative_plan_cache_key(
    *,
    user_id: int,
    paper_id: int,
    page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    user_intent: str,
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:16]
    sig_hash = hashlib.sha256(str(compose_source_signature or "").strip().encode("utf-8")).hexdigest()[:16]
    model_token = str(getattr(settings, "generative_reader_agent_model", "") or getattr(settings, "reader_agent_model", "") or "").strip()
    provider_token = str(getattr(settings, "generative_reader_agent_provider", "") or getattr(settings, "reader_agent_provider", "") or "").strip()
    model_hash = hashlib.sha256(f"{provider_token}:{model_token}".encode("utf-8")).hexdigest()[:12]
    return (
        f"lit:genplan:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:"
        f"{int(page)}:{int(selected_kb_id)}:{sig_hash}:{intent_hash}:{model_hash}"
    )


def _experience_plan_cache_key(
    *,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    generative_plan_signature: str,
    user_intent: str,
    reader_profile: str,
    focus_section_ids: Sequence[str],
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:16]
    sig_hash = hashlib.sha256(str(compose_source_signature or "").strip().encode("utf-8")).hexdigest()[:16]
    plan_hash = hashlib.sha256(str(generative_plan_signature or "").strip().encode("utf-8")).hexdigest()[:16]
    profile_hash = hashlib.sha256(str(reader_profile or "").strip().encode("utf-8")).hexdigest()[:12]
    sections_hash = hashlib.sha256(
        "|".join(sorted(str(item).strip() for item in list(focus_section_ids or []) if str(item).strip())).encode("utf-8")
    ).hexdigest()[:12]
    model_token = str(getattr(settings, "generative_reader_agent_model", "") or getattr(settings, "reader_agent_model", "") or "").strip()
    provider_token = str(getattr(settings, "generative_reader_agent_provider", "") or getattr(settings, "reader_agent_provider", "") or "").strip()
    model_hash = hashlib.sha256(f"{provider_token}:{model_token}".encode("utf-8")).hexdigest()[:12]
    return (
        f"lit:experience:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:{int(focus_page)}:{int(selected_kb_id)}:"
        f"{sig_hash}:{plan_hash}:{intent_hash}:{profile_hash}:{sections_hash}:{model_hash}"
    )


def _reading_dossier_v2_signature(reading_dossier: Mapping[str, Any]) -> str:
    payload = _jsonable_dict(reading_dossier)
    cache_meta = _jsonable_dict(payload.get("cache_meta") or {})
    adjacent_pages = list((_jsonable_dict(payload.get("adjacent_pages") or {})).get("pages") or [])
    adjacent_sig_hash = str(cache_meta.get("adjacent_context_sig_hash") or "").strip()
    if not adjacent_sig_hash:
        adjacent_sig_hash = _reading_dossier_v2_adjacent_context_signature(
            [ReadingDossierV2AdjacentPageRow.model_validate(item) for item in adjacent_pages if isinstance(item, Mapping)]
        )
    adjacent_parser_version = str(cache_meta.get("adjacent_context_parser_version") or "").strip()
    if not adjacent_parser_version:
        adjacent_parser_version = _reading_dossier_v2_adjacent_parser_version(
            [ReadingDossierV2AdjacentPageRow.model_validate(item) for item in adjacent_pages if isinstance(item, Mapping)]
        )
    adjacent_pages_signature_basis = [
        {
            "page": int(row_payload.get("page") or 0),
            "relation": str(row_payload.get("relation") or "").strip(),
            "source": str(row_payload.get("source") or "").strip(),
            "fidelity": str(row_payload.get("fidelity") or "").strip(),
            "page_image_url": str(_jsonable_dict(row_payload.get("page_image") or {}).get("url") or "").strip(),
            "content_stream": list(row_payload.get("content_stream") or []),
            "continuation_hints": list(row_payload.get("continuation_hints") or []),
            "raw_text": str(row_payload.get("raw_text") or "").strip(),
            "meta": _jsonable_dict(row_payload.get("meta") or {}),
        }
        for row_payload in (_jsonable_dict(item) for item in adjacent_pages if isinstance(item, Mapping))
    ]
    canonical = {
        "version": str(payload.get("version") or "").strip(),
        "dossier_contract": str(payload.get("dossier_contract") or "").strip(),
        "focus_page": int(payload.get("focus_page") or 0),
        "compose_source_signature": str(payload.get("compose_source_signature") or "").strip(),
        "cache_meta": {
            **cache_meta,
            "adjacent_context_parser_version": adjacent_parser_version,
            "adjacent_context_sig_hash": adjacent_sig_hash,
        },
        "adjacent_context_sig_hash": adjacent_sig_hash,
        "adjacent_pages_signature_basis": adjacent_pages_signature_basis,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _experience_v2_cache_signature(
    *,
    compose_source_signature: str,
    dossier_signature: str,
) -> str:
    stable_signature = str(compose_source_signature or "").strip()
    if stable_signature:
        return stable_signature
    return str(dossier_signature or "").strip()


def _experience_session_v2_cache_key(
    *,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    dossier_signature: str,
    user_intent: str,
    reader_profile: str,
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:12]
    profile_hash = hashlib.sha256(str(reader_profile or "").strip().encode("utf-8")).hexdigest()[:12]
    dossier_hash = hashlib.sha256(str(dossier_signature or "").strip().encode("utf-8")).hexdigest()[:16]
    return (
        f"{EXPERIENCE_SESSION_V2_CACHE_NAMESPACE}:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:"
        f"{int(focus_page)}:{int(selected_kb_id)}:{_EXPERIENCE_V2_RUNTIME_VERSION}:{dossier_hash}:{intent_hash}:{profile_hash}"
    )


def _page_artifact_v2_cache_key(
    *,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    dossier_signature: str,
    user_intent: str,
    reader_profile: str,
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:12]
    profile_hash = hashlib.sha256(str(reader_profile or "").strip().encode("utf-8")).hexdigest()[:12]
    dossier_hash = hashlib.sha256(str(dossier_signature or "").strip().encode("utf-8")).hexdigest()[:16]
    return (
        f"{PAGE_ARTIFACT_V2_CACHE_NAMESPACE}:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:"
        f"{int(focus_page)}:{int(selected_kb_id)}:{_EXPERIENCE_V2_RUNTIME_VERSION}:{dossier_hash}:{intent_hash}:{profile_hash}"
    )


def _experience_v2_cache_key_like_pattern(
    *,
    namespace: str,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    user_intent: str,
    reader_profile: str,
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:12]
    profile_hash = hashlib.sha256(str(reader_profile or "").strip().encode("utf-8")).hexdigest()[:12]
    return (
        f"{str(namespace or '').strip()}:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:"
        f"{int(focus_page)}:{int(selected_kb_id)}:{_EXPERIENCE_V2_RUNTIME_VERSION}:%:{intent_hash}:{profile_hash}"
    )


def _experience_v2_fast_cache_key(
    *,
    namespace: str,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    user_intent: str,
    reader_profile: str,
) -> str:
    intent_hash = hashlib.sha256(str(user_intent or "").strip().encode("utf-8")).hexdigest()[:12]
    profile_hash = hashlib.sha256(str(reader_profile or "").strip().encode("utf-8")).hexdigest()[:12]
    return (
        f"{str(namespace or '').strip()}:{_READER_PLAN_CACHE_NAMESPACE_VERSION}:{int(user_id)}:{int(paper_id)}:"
        f"{int(focus_page)}:{int(selected_kb_id)}:{_EXPERIENCE_V2_RUNTIME_VERSION}:{intent_hash}:{profile_hash}"
    )


def _plan_cache_ttl_seconds_from_expires_at(
    expires_at: Optional[datetime],
    *,
    default_ttl_seconds: int,
) -> int:
    ttl_seconds = max(1, int(default_ttl_seconds))
    if isinstance(expires_at, datetime):
        now_dt = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
        delta = (expires_at - now_dt).total_seconds()
        ttl_seconds = max(1, int(delta)) if delta > 0 else 1
    return ttl_seconds


async def _experience_v2_cache_db_get_by_compose_signature(
    *,
    plan_kind: str,
    namespace: str,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    user_intent: str,
    reader_profile: str,
) -> tuple[Optional[Dict[str, Any]], Optional[datetime], Optional[str]]:
    cache_key_like = _experience_v2_cache_key_like_pattern(
        namespace=namespace,
        user_id=user_id,
        paper_id=paper_id,
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    return await _plan_cache_db_get_latest_by_compose_signature(
        plan_kind=plan_kind,
        user_id=user_id,
        paper_id=paper_id,
        page=focus_page,
        compose_source_signature=compose_source_signature,
        cache_key_like=cache_key_like,
    )


async def _experience_v2_cache_db_get_latest_stable(
    *,
    plan_kind: str,
    namespace: str,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    user_intent: str,
    reader_profile: str,
) -> tuple[Optional[Dict[str, Any]], Optional[datetime], Optional[str]]:
    cache_key_like = _experience_v2_cache_key_like_pattern(
        namespace=namespace,
        user_id=user_id,
        paper_id=paper_id,
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    return await _plan_cache_db_get_latest_by_cache_key_like(
        plan_kind=plan_kind,
        user_id=user_id,
        paper_id=paper_id,
        page=focus_page,
        cache_key_like=cache_key_like,
    )


async def _experience_v2_fast_cache_get(
    cache_key: str,
    *,
    memory_store: Dict[str, tuple[float, Dict[str, Any]]],
) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature ExperienceV2 FastCache] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = memory_store.get(cache_key)
    if not item:
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        memory_store.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _experience_v2_fast_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: int,
    memory_store: Dict[str, tuple[float, Dict[str, Any]]],
) -> None:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature ExperienceV2 FastCache] Redis写入失败，降级内存缓存: {exc}")

    memory_store[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


async def _experience_session_v2_fast_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    return await _experience_v2_fast_cache_get(
        cache_key,
        memory_store=_experience_session_v2_fast_cache_memory,
    )


async def _experience_session_v2_fast_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS,
) -> None:
    await _experience_v2_fast_cache_set(
        cache_key,
        payload,
        ttl_seconds=ttl_seconds,
        memory_store=_experience_session_v2_fast_cache_memory,
    )


async def _page_artifact_v2_fast_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    return await _experience_v2_fast_cache_get(
        cache_key,
        memory_store=_page_artifact_v2_fast_cache_memory,
    )


async def _page_artifact_v2_fast_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
) -> None:
    await _experience_v2_fast_cache_set(
        cache_key,
        payload,
        ttl_seconds=ttl_seconds,
        memory_store=_page_artifact_v2_fast_cache_memory,
    )


def _clean_reader_facing_excerpt_text(
    text: str,
    *,
    max_chars: Optional[int],
) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    if max_chars is None or int(max_chars) <= 0:
        return normalized
    if len(normalized) <= max_chars:
        return normalized
    clipped = normalized[:max_chars].rstrip(" ,;:，。；、")
    if not clipped:
        return normalized[:max_chars]
    return f"{clipped}…"


def _normalize_excerpt_match_text(text: str) -> str:
    normalized = _clean_reader_facing_excerpt_text(str(text or ""), max_chars=1200).lower()
    normalized = re.sub(r"\[[^\]]*\]", " ", normalized)
    normalized = re.sub(r"[^0-9a-z\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _excerpt_texts_loosely_match(left: str, right: str) -> bool:
    a = _normalize_excerpt_match_text(left)
    b = _normalize_excerpt_match_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer and len(shorter) >= 24:
        return True
    if len(shorter) < 24:
        return False
    a_words = [token for token in a.split(" ") if token]
    b_words = [token for token in b.split(" ") if token]
    if not a_words or not b_words:
        return False
    overlap = len(set(a_words) & set(b_words))
    threshold = max(4, min(len(a_words), len(b_words)) // 2)
    return overlap >= threshold


def _build_excerpt_resolution_match_token(
    *,
    row: Mapping[str, Any],
    matched_layout_ids: Sequence[str],
    matched_block_ids: Sequence[str],
    fallback_text: str = "",
) -> str:
    return "|".join(
        [
            str(_jsonable_dict(row).get("node_id") or "").strip(),
            ",".join(sorted(str(item).strip() for item in list(matched_layout_ids or []) if str(item).strip())),
            ",".join(sorted(str(item).strip() for item in list(matched_block_ids or []) if str(item).strip())),
            _normalize_excerpt_match_text(fallback_text)[:120],
        ]
    )


def _is_ocr_heavy_excerpt_candidate(*, text: str, node_kind: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return True
    if len(normalized) > 1800:
        return True
    lowered_kind = str(node_kind or "").strip().lower()
    if lowered_kind in {"figure", "table", "equation", "media"} and len(normalized) > 220:
        return True
    if re.match(r"^(fig(?:ure)?|table)\s*[\dA-Za-z.:_-]+", normalized, flags=re.IGNORECASE) and len(normalized) > 140:
        return True
    symbol_count = sum(1 for ch in normalized if (not ch.isalnum()) and (not ch.isspace()))
    digit_count = sum(1 for ch in normalized if ch.isdigit())
    token_count = max(1, len(normalized))
    if len(normalized) > 220 and (normalized.count("|") >= 6 or normalized.count("\t") >= 4):
        return True
    if len(normalized) > 260 and (symbol_count / token_count) > 0.33:
        return True
    if len(normalized) > 260 and (digit_count / token_count) > 0.22:
        return True
    return False


def _allows_media_caption_excerpt(node_kind: Any) -> bool:
    normalized = str(node_kind or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"figure", "table", "equation", "media", "caption"}:
        return True
    if normalized.startswith(("figure_", "table_", "equation_", "media_")):
        return True
    if normalized.endswith(("_caption", "_legend", "_name")) and normalized.split("_", 1)[0] in {
        "figure",
        "table",
        "equation",
        "media",
    }:
        return True
    return False


def _split_current_page_excerpt_row(
    *,
    text: str,
    layout_ids: Sequence[str],
    block_ids: Sequence[str],
    node_id: str,
    max_chars: Optional[int],
    max_blocks_per_chunk: int = 6,
) -> List[Dict[str, Any]]:
    full_text_limit = None if max_chars is None or int(max_chars) <= 0 else max(1200, int(max_chars) * 4)
    cleaned_full = _clean_reader_facing_excerpt_text(str(text or ""), max_chars=full_text_limit)
    normalized_layout_ids = [str(item).strip() for item in list(layout_ids or []) if str(item).strip()]
    normalized_block_ids = [str(item).strip() for item in list(block_ids or []) if str(item).strip()]
    base_row = {
        "text": _clean_reader_facing_excerpt_text(cleaned_full, max_chars=max_chars),
        "layout_ids": normalized_layout_ids[:6],
        "block_ids": normalized_block_ids[:24],
        "node_id": str(node_id or "").strip(),
    }
    if not cleaned_full:
        return []
    if len(normalized_block_ids) <= max_blocks_per_chunk:
        return [base_row]

    sentence_parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+", cleaned_full)
        if part and part.strip()
    ]
    if len(sentence_parts) < 2:
        return [base_row]

    total_blocks = len(normalized_block_ids)
    chunk_rows: List[Dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, total_blocks, max_blocks_per_chunk), start=1):
        block_chunk = normalized_block_ids[start : start + max_blocks_per_chunk]
        if not block_chunk:
            continue
        sentence_start = min(
            len(sentence_parts) - 1,
            max(0, round(len(sentence_parts) * (start / total_blocks))),
        )
        sentence_end = min(
            len(sentence_parts),
            max(sentence_start + 1, round(len(sentence_parts) * ((start + len(block_chunk)) / total_blocks))),
        )
        chunk_text = " ".join(sentence_parts[sentence_start:sentence_end]).strip()
        cleaned_chunk = _clean_reader_facing_excerpt_text(chunk_text or cleaned_full, max_chars=max_chars)
        if not cleaned_chunk:
            continue
        chunk_rows.append(
            {
                "text": cleaned_chunk,
                "layout_ids": normalized_layout_ids[:6],
                "block_ids": block_chunk,
                "node_id": f"{str(node_id or '').strip()}#chunk-{chunk_index}" if str(node_id or "").strip() else "",
            }
        )

    return chunk_rows or [base_row]


def _build_current_page_excerpt_rows(
    *,
    reading_nodes: Sequence[Mapping[str, Any]],
    layout_atoms: Sequence[Mapping[str, Any]],
    max_chars: Optional[int],
    include_non_main_flow: bool = False,
) -> List[Dict[str, Any]]:
    excerpt_rows: List[Dict[str, Any]] = []
    for node in reading_nodes:
        if not isinstance(node, Mapping):
            continue
        if (not include_non_main_flow) and not bool(node.get("include_in_main_flow", True)):
            continue
        node_kind = str(node.get("node_kind") or (_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip().lower()
        text = str(node.get("clean_text") or node.get("normalized_text") or node.get("raw_text") or "").strip()
        if not text or _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()],
                block_ids=[str(item).strip() for item in list(node.get("source_block_ids") or []) if str(item).strip()],
                node_id=str(node.get("node_id") or "").strip(),
                max_chars=max_chars,
            )
        )
    for atom in layout_atoms:
        if not isinstance(atom, Mapping):
            continue
        if (not include_non_main_flow) and not bool(atom.get("include_in_main_flow", True)):
            continue
        node_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        text = str(atom.get("clean_text") or atom.get("normalized_text") or atom.get("raw_text") or "").strip()
        if not text:
            continue
        allow_media_caption_excerpt = _allows_media_caption_excerpt(node_kind)
        if (not allow_media_caption_excerpt) and _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(atom.get("layout_id") or "").strip()] if str(atom.get("layout_id") or "").strip() else [],
                block_ids=[str(item).strip() for item in list(atom.get("canonical_block_ids") or []) if str(item).strip()],
                node_id="",
                max_chars=max_chars,
            )
        )
    return excerpt_rows


def _resolve_excerpt_against_current_page_grounding(
    *,
    override: Mapping[str, Any],
    candidate_excerpt_rows: Sequence[Mapping[str, Any]],
    reading_nodes: Sequence[Mapping[str, Any]],
    layout_atoms: Sequence[Mapping[str, Any]],
    seen_excerpt_tokens: Set[str],
    max_chars: Optional[int],
) -> Optional[Dict[str, Any]]:
    override_layout_id_list = [str(item).strip() for item in list(override.get("source_layout_ids") or []) if str(item).strip()]
    override_layout_ids = set(override_layout_id_list)
    override_block_id_list = [str(item).strip() for item in list(override.get("source_block_ids") or []) if str(item).strip()]
    override_block_ids = set(override_block_id_list)
    override_display_text = _clean_reader_facing_excerpt_text(
        str(override.get("display_text") or "").strip(),
        max_chars=max_chars,
    )
    override_meta = _jsonable_dict(override.get("meta") or {})
    override_translation_zh = str(override.get("translation_zh") or override_meta.get("translation_zh") or "").strip()
    if override_translation_zh:
        override_meta["translation_zh"] = override_translation_zh

    def _match(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        for row in rows:
            row_layout_id_list = [str(item).strip() for item in list(row.get("layout_ids") or []) if str(item).strip()]
            row_layout_ids = set(row_layout_id_list)
            row_block_id_list = [str(item).strip() for item in list(row.get("block_ids") or []) if str(item).strip()]
            row_block_ids = set(row_block_id_list)
            matched_by_source = bool(
                (row_layout_ids and row_layout_ids & override_layout_ids)
                or (row_block_ids and row_block_ids & override_block_ids)
            )
            matched_by_text = bool(override_display_text) and _excerpt_texts_loosely_match(
                override_display_text,
                str(row.get("text") or ""),
            )
            if not (matched_by_source or matched_by_text):
                continue
            matched_layout_ids = [item for item in row_layout_id_list if item in override_layout_ids] or row_layout_id_list
            matched_block_ids = (
                [item for item in row_block_id_list if item in override_block_ids]
                or override_block_id_list
                or row_block_id_list
            )
            match_token = _build_excerpt_resolution_match_token(
                row=row,
                matched_layout_ids=matched_layout_ids,
                matched_block_ids=matched_block_ids,
                fallback_text=override_display_text or str(row.get("text") or ""),
            )
            if match_token in seen_excerpt_tokens:
                continue
            seen_excerpt_tokens.add(match_token)
            return {
                "text": override_display_text or str(row.get("text") or ""),
                "layout_ids": matched_layout_ids,
                "block_ids": matched_block_ids,
                "node_id": str(row.get("node_id") or "").strip(),
                "meta": {**_jsonable_dict(row.get("meta") or {}), **override_meta} if override_meta else _jsonable_dict(row.get("meta") or {}),
            }
        return None

    matched = _match(candidate_excerpt_rows)
    if matched:
        return matched
    if not override_layout_ids and not override_block_ids and not override_display_text:
        return None
    direct_rows = _build_current_page_excerpt_rows(
        reading_nodes=reading_nodes,
        layout_atoms=layout_atoms,
        max_chars=max_chars,
    )
    matched = _match(direct_rows)
    if matched:
        return matched
    if not override_layout_ids and not override_block_ids:
        return None
    non_main_flow_rows = _build_current_page_excerpt_rows(
        reading_nodes=reading_nodes,
        layout_atoms=layout_atoms,
        max_chars=max_chars,
        include_non_main_flow=True,
    )
    return _match(non_main_flow_rows)


def _build_current_page_excerpt_rows(
    *,
    reading_nodes: Sequence[Mapping[str, Any]],
    layout_atoms: Sequence[Mapping[str, Any]],
    max_chars: Optional[int],
    include_non_main_flow: bool = False,
) -> List[Dict[str, Any]]:
    excerpt_rows: List[Dict[str, Any]] = []
    for node in reading_nodes:
        if not isinstance(node, Mapping):
            continue
        if (not include_non_main_flow) and not bool(node.get("include_in_main_flow", True)):
            continue
        node_kind = str(node.get("node_kind") or (_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip().lower()
        text = str(node.get("clean_text") or node.get("normalized_text") or node.get("raw_text") or "").strip()
        if not text or _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()],
                block_ids=[str(item).strip() for item in list(node.get("source_block_ids") or []) if str(item).strip()],
                node_id=str(node.get("node_id") or "").strip(),
                max_chars=max_chars,
            )
        )
    for atom in layout_atoms:
        if not isinstance(atom, Mapping):
            continue
        if (not include_non_main_flow) and not bool(atom.get("include_in_main_flow", True)):
            continue
        node_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        text = str(atom.get("clean_text") or atom.get("normalized_text") or atom.get("raw_text") or "").strip()
        if not text:
            continue
        allow_media_caption_excerpt = _allows_media_caption_excerpt(node_kind)
        if (not allow_media_caption_excerpt) and _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        cleaned = _clean_reader_facing_excerpt_text(text, max_chars=max_chars)
        if not cleaned:
            continue
        excerpt_rows.extend(
            _split_current_page_excerpt_row(
                text=cleaned,
                layout_ids=[str(atom.get("layout_id") or "").strip()] if str(atom.get("layout_id") or "").strip() else [],
                block_ids=[str(item).strip() for item in list(atom.get("canonical_block_ids") or []) if str(item).strip()],
                node_id="",
                max_chars=max_chars,
            )
        )
    return excerpt_rows


def _resolve_excerpt_against_current_page_grounding(
    *,
    override: Mapping[str, Any],
    candidate_excerpt_rows: Sequence[Mapping[str, Any]],
    reading_nodes: Sequence[Mapping[str, Any]],
    layout_atoms: Sequence[Mapping[str, Any]],
    seen_excerpt_tokens: Set[str],
    max_chars: Optional[int],
) -> Optional[Dict[str, Any]]:
    override_layout_id_list = [str(item).strip() for item in list(override.get("source_layout_ids") or []) if str(item).strip()]
    override_layout_ids = set(override_layout_id_list)
    override_block_id_list = [str(item).strip() for item in list(override.get("source_block_ids") or []) if str(item).strip()]
    override_block_ids = set(override_block_id_list)
    override_display_text = _clean_reader_facing_excerpt_text(
        str(override.get("display_text") or "").strip(),
        max_chars=max_chars,
    )
    override_meta = _jsonable_dict(override.get("meta") or {})
    override_translation_zh = str(override.get("translation_zh") or override_meta.get("translation_zh") or "").strip()
    if override_translation_zh:
        override_meta["translation_zh"] = override_translation_zh

    def _match(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        for row in rows:
            row_layout_id_list = [str(item).strip() for item in list(row.get("layout_ids") or []) if str(item).strip()]
            row_layout_ids = set(row_layout_id_list)
            row_block_id_list = [str(item).strip() for item in list(row.get("block_ids") or []) if str(item).strip()]
            row_block_ids = set(row_block_id_list)
            matched_by_source = bool(
                (row_layout_ids and row_layout_ids & override_layout_ids)
                or (row_block_ids and row_block_ids & override_block_ids)
            )
            matched_by_text = bool(override_display_text) and _excerpt_texts_loosely_match(
                override_display_text,
                str(row.get("text") or ""),
            )
            if not (matched_by_source or matched_by_text):
                continue
            matched_layout_ids = [item for item in row_layout_id_list if item in override_layout_ids] or row_layout_id_list
            matched_block_ids = (
                [item for item in row_block_id_list if item in override_block_ids]
                or override_block_id_list
                or row_block_id_list
            )
            match_token = _build_excerpt_resolution_match_token(
                row=row,
                matched_layout_ids=matched_layout_ids,
                matched_block_ids=matched_block_ids,
                fallback_text=override_display_text or str(row.get("text") or ""),
            )
            if match_token in seen_excerpt_tokens:
                continue
            seen_excerpt_tokens.add(match_token)
            return {
                "text": override_display_text or str(row.get("text") or ""),
                "layout_ids": matched_layout_ids,
                "block_ids": matched_block_ids,
                "node_id": str(row.get("node_id") or "").strip(),
                "meta": {**_jsonable_dict(row.get("meta") or {}), **override_meta} if override_meta else _jsonable_dict(row.get("meta") or {}),
            }
        return None

    resolved = _match(candidate_excerpt_rows)
    if resolved:
        return resolved
    if not override_layout_ids and not override_block_ids and not override_display_text:
        return None
    direct_excerpt_rows = _build_current_page_excerpt_rows(
        reading_nodes=reading_nodes,
        layout_atoms=layout_atoms,
        max_chars=max_chars,
    )
    resolved = _match(direct_excerpt_rows)
    if resolved:
        return resolved
    if not override_layout_ids and not override_block_ids:
        return None
    non_main_flow_excerpt_rows = _build_current_page_excerpt_rows(
        reading_nodes=reading_nodes,
        layout_atoms=layout_atoms,
        max_chars=max_chars,
        include_non_main_flow=True,
    )
    return _match(non_main_flow_excerpt_rows)


def _infer_excerpt_candidate_kind(
    *,
    node_kind: str,
    source_layout_ids: Sequence[Any],
    layout_atoms_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    normalized_kind = str(node_kind or "").strip().lower()
    if normalized_kind and normalized_kind not in {"paragraph", "text", "body"}:
        return normalized_kind
    for raw_layout_id in list(source_layout_ids or []):
        layout_id = str(raw_layout_id or "").strip()
        if not layout_id:
            continue
        atom = _jsonable_dict(layout_atoms_by_id.get(layout_id) or {})
        layout_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        if layout_kind and layout_kind not in {"paragraph", "text", "body"}:
            return layout_kind
    return normalized_kind or "paragraph"


def _compact_media_slot_label(raw_label: str, fallback: str) -> str:
    normalized = re.sub(r"\s+", " ", str(raw_label or "")).strip()
    if not normalized:
        return fallback
    match = re.match(r"^(Figure|Fig\.?|Table|Equation)\s*([A-Za-z0-9()._-]+)", normalized, flags=re.IGNORECASE)
    if match:
        prefix = match.group(1)
        value = match.group(2)
        prefix = "Figure" if prefix.lower().startswith("fig") else prefix.title()
        return f"{prefix} {value}".strip()
    if len(normalized) <= 48:
        return normalized
    return fallback


def _experience_session_v2_reader_agent_config() -> Dict[str, Any]:
    provider = str(getattr(settings, "reader_agent_provider", "") or "").strip() or "aliyun"
    provider_config = dict(settings.get_llm_config(provider) or {})
    api_key = str(provider_config.get("api_key") or "").strip()
    base_url = str(provider_config.get("base_url") or "").strip()
    model = str(getattr(settings, "reader_agent_model", "") or provider_config.get("model") or "").strip()
    timeout_seconds = max(8.0, float(int(getattr(settings, "reader_agent_timeout_ms", 90000) or 90000) / 1000.0))
    max_tokens = max(1024, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000))
    if not api_key or not base_url or not model:
        raise ValueError("narrative brief generation failed: reader agent model unavailable")
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
    }


def _build_experience_session_v2_narrative_brief_prompt_payload(
    *,
    reading_dossier: Mapping[str, Any],
    focus_page: int,
    reader_profile: str,
    user_intent: str,
) -> Dict[str, Any]:
    dossier = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    adjacent_pages = [
        ReadingDossierV2AdjacentPageRow.model_validate(item).model_dump(mode="json")
        for item in list((_jsonable_dict(dossier.get("adjacent_pages") or {})).get("pages") or [])
        if isinstance(item, Mapping)
    ]
    current_lane = _jsonable_dict(dossier.get("current_page") or {})
    rich_grounding = _jsonable_dict(current_lane.get("rich_grounding") or {})
    current_page = {
        "page": int(focus_page),
        "owner": str(current_lane.get("owner") or "").strip(),
        "fidelity": str(current_lane.get("fidelity") or "").strip(),
        "page_image": _jsonable_dict(rich_grounding.get("page_image") or {}),
        "layout_atoms": list(rich_grounding.get("layout_atoms") or []),
        "reading_nodes": list(rich_grounding.get("reading_nodes") or []),
        "evidence_map": list(rich_grounding.get("evidence_map") or []),
    }
    return {
        "task": "Generate experience_session_v2 narrative_brief JSON for bootstrap only.",
        "focus_page": int(focus_page),
        "reader_profile": str(reader_profile or "").strip() or "curious_generalist",
        "user_intent": str(user_intent or "").strip(),
        "language_policy": {
            "strategy_language": "zh-CN",
            "reader_facing_guidance_language": "zh-CN",
            "preserve_canonical_source_labels": True,
            "do_not_translate_original_excerpts": True,
        },
        "rules": {
            "current_page_is_primary_narrative_anchor": True,
            "adjacent_pages_are_support_context_only": True,
            "produce_reading_strategy_not_final_page_copy": True,
            "preserve_page_boundary_continuity": True,
            "identify_required_media_assets_to_surface": True,
            "strategy_language": "zh-CN",
            "forbidden_outputs": [
                "html",
                "javascript",
                "final artifact prose",
                "reader-facing page copy",
            ],
            "output_format": "strict_json_only",
        },
        "required_fields": sorted(_EXPERIENCE_SESSION_V2_NARRATIVE_BRIEF_REQUIRED_FIELDS),
        "recommended_fields": [
            "opening_key_points",
            "previous_page_bridge",
            "next_page_bridge",
            "reader_attention_order",
            "must_surface_nodes",
            "suppressed_threads",
        ],
        "strategy_shape_preferences": {
            "goal": (
                "Produce a source-grounded reading-strategy object with enough detail to drive downstream "
                "artifact drafting. Do not over-compress the strategy just to keep it short."
            ),
            "current_page_main_arc": {
                "preferred_form": "concise_string_or_small_object",
                "max_sentences": 5,
                "focus": [
                    "what this page is doing",
                    "what the reader should attend to first",
                    "why this page matters in the local flow",
                    "which textual and visual evidence must remain visible in the reader flow",
                ],
            },
            "continuity_resolutions": {
                "preferred_form": "list_of_2_to_6_concrete_items_or_small_object",
                "avoid": [
                    "one long paragraph",
                    "page-by-page recap",
                ],
                "focus": [
                    "what must be repaired from the previous page",
                    "what will matter on the next page",
                    "which continuity details are important enough to shape drafting",
                ],
            },
            "opening_key_points": {
                "preferred_form": "ordered_list_of_2_to_4_short_reader_facing_points",
                "focus": [
                    "the key takeaways the reader should hold before entering detailed excerpts",
                    "what makes the current page important right now",
                    "what the figure/table/equation is proving on this page",
                ],
                "avoid": [
                    "long quoted excerpts",
                    "copying the first original paragraph verbatim",
                ],
            },
            "previous_page_bridge": {
                "preferred_form": "small_object",
                "focus": [
                    "previous page number when available",
                    "1 to 2 short previous-page takeaways that matter now",
                    "one bridge sentence explaining how the current page picks up from there",
                ],
            },
            "next_page_bridge": {
                "preferred_form": "small_object",
                "focus": [
                    "next page number when available",
                    "1 to 2 short next-page takeaways that matter next",
                    "one bridge sentence explaining how the current page flows forward",
                ],
            },
            "content_strategy": {
                "preferred_form": "concise_string_or_shallow_object_with_enough_detail",
                "focus": [
                    "attention order",
                    "must-surface media",
                    "whether term notes or external resources are needed",
                    "which minor threads to suppress",
                    "how much original excerpting the downstream draft will likely need",
                    "whether the draft should feel figure-first, text-first, or interleaved",
                ],
            },
            "presentation_strategy": {
                "preferred_form": "concise_string_or_shallow_object_with_enough_detail",
                "focus": [
                    "reader-surface bias",
                    "visual anchor priority",
                    "high-level layout emphasis",
                    "how dense or spacious the final reader page should feel",
                    "whether continuity should stay implicit or become briefly explicit",
                ],
                "forbid": [
                    "implementation notes",
                    "accessibility checklists",
                    "micro-interaction specs unless essential",
                ],
            },
            "reader_attention_order": {
                "preferred_form": "ordered_list_of_2_to_5_reader_steps",
                "focus": [
                    "what the reader should notice first",
                    "what comes next in the page-local reasoning flow",
                ],
            },
            "must_surface_nodes": {
                "preferred_form": "small_list_of_specific page-local anchors",
                "focus": [
                    "figure/table/equation labels",
                    "current-page paragraph or reading-node anchors",
                    "must-keep evidence hooks",
                ],
            },
            "suppressed_threads": {
                "preferred_form": "small_list",
                "focus": [
                    "threads that should stay secondary or absent from the reader-facing page",
                ],
            },
        },
        "reading_dossier_v2": {
            "version": str(dossier.get("version") or "").strip(),
            "focus_page": int(dossier.get("focus_page") or focus_page),
            "reader_profile": str(dossier.get("reader_profile") or "").strip(),
            "current_page": current_page,
            "adjacent_pages": {
                "owner": str((_jsonable_dict(dossier.get("adjacent_pages") or {})).get("owner") or "").strip(),
                "fidelity": str((_jsonable_dict(dossier.get("adjacent_pages") or {})).get("fidelity") or "").strip(),
                "pages": adjacent_pages,
            },
            "meta": _jsonable_dict(dossier.get("meta") or {}),
        },
    }


def _experience_session_v2_narrative_brief_system_prompt() -> str:
    return (
        "You are generating the bootstrap narrative_brief for experience_session_v2.\n"
        "This is an internal reading-strategy object, not the final page and not reader-facing copy.\n"
        "Write like a reading director giving grounded downstream guidance, not like a summarizer and not like an implementation spec.\n"
        "Hard rules:\n"
        "1) The current page is the only primary narrative anchor.\n"
        "2) Neighboring pages are support context for continuity, figure/table understanding, and semantic repair only.\n"
        "3) Use the full current-page grounding and the full neighboring-page structured context provided.\n"
        "4) Produce a reading strategy, not final artifact prose and not a summary splice.\n"
        "5) Ground every strategy choice in the provided source material; do not invent claims not supported by the dossier.\n"
        "6) Do not emit HTML, JavaScript, markdown, or arbitrary page copy.\n"
        "7) Output strict JSON only.\n"
        "8) Required top-level fields: focus_page, current_page_main_arc, continuity_resolutions, required_media_refs, content_strategy, presentation_strategy.\n"
        "9) Optional but strongly preferred top-level fields: opening_key_points, previous_page_bridge, next_page_bridge, reader_attention_order, must_surface_nodes, suppressed_threads.\n"
        "10) Field-shape rules: current_page_main_arc, content_strategy, and presentation_strategy may be either a concise string or a structured JSON object; continuity_resolutions may be a string, list, or structured JSON object; required_media_refs must remain a JSON array.\n"
        "11) current_page_main_arc should sound like a reading-director instruction, not a paper summary and not an implementation note.\n"
        "12) continuity_resolutions should be concrete continuity decisions, usually 2 to 4 items, and should avoid 'this page completes the previous page' style phrasing unless absolutely necessary.\n"
        "13) opening_key_points should provide 2 to 4 short Chinese takeaways for the opening of the current page. Do not use a long original excerpt as the opening cue.\n"
        "14) If a previous page exists and materially matters, provide previous_page_bridge as an object such as {\"page\":6,\"key_points\":[...],\"bridge_text\":\"...\"}. Keep it short and subordinate to the current page.\n"
        "15) If a next page exists and materially matters, provide next_page_bridge as an object such as {\"page\":8,\"key_points\":[...],\"bridge_text\":\"...\"}. Use it to show what the current page is handing forward.\n"
        "16) reader_attention_order should give the intended reading order in short imperative or descriptive steps.\n"
        "17) must_surface_nodes should identify the page-local evidence anchors that the downstream draft must keep visible.\n"
        "18) suppressed_threads should identify lower-value threads that should stay secondary or omitted.\n"
        "19) required_media_refs should list the media/assets that must be surfaced to understand the current page, including multiple items when the page genuinely needs them.\n"
        "20) content_strategy should express reading order, excerpt density, evidence emphasis, and whether term notes or external resources are needed.\n"
        "21) presentation_strategy should express high-level reader-surface decisions only; avoid implementation detail, accessibility checklists, or renderer micro-specs unless they are essential to comprehension.\n"
        "22) Prefer required_media_refs items as objects like {\"type\":\"figure\",\"label\":\"Fig 3\",\"description\":\"...\"}; short strings are acceptable only when they clearly name the required media.\n"
        "23) Do not optimize for brevity alone. The strategy should be concise but sufficiently complete to drive a full reader page.\n"
        "24) Write strategy text in Simplified Chinese by default so workbench inspection and downstream drafting stay aligned with a Chinese reader-facing experience.\n"
        "25) Preserve canonical labels or abbreviations such as Figure 3, DOI, USMLE, and exact quoted source phrases when they are the clearest reference.\n"
        "26) When a technical term matters, explain it in Chinese and include the original English term in parentheses on first mention when helpful.\n"
        "27) Avoid awkward hyphenated Chinese compounds for technical relations. For example, render answer-explanation concordance as '答案与解释的一致性（answer-explanation concordance）' rather than '答案-解释一致性'.\n"
    )


def _validate_experience_session_v2_narrative_brief_payload(
    raw_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    payload = _jsonable_dict(raw_payload)
    brief = ExperienceSessionV2NarrativeBrief.model_validate(payload)
    return brief.model_dump(mode="json")


def _narrative_brief_leaf_strings(value: Any, *, max_items: int = 16, _depth: int = 0) -> List[str]:
    if _depth > 5 or max_items <= 0:
        return []
    if isinstance(value, str):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        preferred_scalars: List[str] = []
        for key in (
            "primary_claim",
            "claim_text",
            "summary",
            "text",
            "description",
            "definition",
            "narrative_flow",
            "resolution_action",
            "repair_note",
            "reader_guidance",
            "layout_recommendation",
            "primary_focus",
            "approach",
            "context",
            "presentation_note",
        ):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                preferred_scalars.append(item.strip())
        if preferred_scalars:
            return preferred_scalars[:max_items]
        items: List[str] = []
        for child in value.values():
            items.extend(_narrative_brief_leaf_strings(child, max_items=max_items - len(items), _depth=_depth + 1))
            if len(items) >= max_items:
                break
        return items[:max_items]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items: List[str] = []
        for child in value:
            items.extend(_narrative_brief_leaf_strings(child, max_items=max_items - len(items), _depth=_depth + 1))
            if len(items) >= max_items:
                break
        return items[:max_items]
    text = str(value).strip()
    return [text] if text else []


def _compact_narrative_brief_text(value: Any, *, max_chars: int = 260) -> str:
    if isinstance(value, str):
        return _clean_reader_facing_excerpt_text(str(value), max_chars=max_chars)
    if isinstance(value, Mapping):
        for key in ("primary_claim", "claim_text", "summary", "text", "description"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return _clean_reader_facing_excerpt_text(item, max_chars=max_chars)
    leaf_strings = _narrative_brief_leaf_strings(value, max_items=6)
    if not leaf_strings:
        return ""
    return _clean_reader_facing_excerpt_text(" ".join(leaf_strings), max_chars=max_chars)


def _compact_narrative_brief_lines(value: Any, *, max_items: int = 4, max_chars: int = 220) -> List[str]:
    if isinstance(value, str):
        text = _clean_reader_facing_excerpt_text(value, max_chars=max_chars)
        return [text] if text else []
    if isinstance(value, Mapping):
        lines: List[str] = []
        for key, child in value.items():
            child_lines = _compact_narrative_brief_lines(child, max_items=max_items, max_chars=max_chars)
            if not child_lines:
                continue
            if len(child_lines) == 1:
                line = f"{key}: {child_lines[0]}".strip()
                lines.append(_clean_reader_facing_excerpt_text(line, max_chars=max_chars))
            else:
                lines.extend(child_lines)
            if len(lines) >= max_items:
                break
        return lines[:max_items]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        lines: List[str] = []
        for child in value:
            lines.extend(_compact_narrative_brief_lines(child, max_items=max_items - len(lines), max_chars=max_chars))
            if len(lines) >= max_items:
                break
        return lines[:max_items]
    text = _clean_reader_facing_excerpt_text(str(value), max_chars=max_chars)
    return [text] if text else []


def _compact_reader_bridge_payload(raw_bridge: Any) -> Dict[str, Any]:
    payload = _jsonable_dict(raw_bridge or {})
    if not payload and isinstance(raw_bridge, str):
        text = _clean_reader_facing_excerpt_text(str(raw_bridge), max_chars=200)
        return {"bridge_text": text} if text else {}
    if not payload:
        return {}

    page = int(
        payload.get("page")
        or payload.get("page_number")
        or payload.get("from_page")
        or payload.get("to_page")
        or 0
    )
    key_points = [
        _clean_reader_facing_excerpt_text(str(item), max_chars=160)
        for item in list(
            payload.get("key_points")
            or payload.get("takeaways")
            or payload.get("page_points")
            or payload.get("page_takeaways")
            or []
        )
        if str(item).strip()
    ][:3]
    if not key_points:
        leaf_strings = _narrative_brief_leaf_strings(
            payload.get("specific_resolutions")
            or payload.get("reading_focus")
            or payload.get("main_points")
            or payload,
            max_items=4,
        )
        key_points = [
            _clean_reader_facing_excerpt_text(item, max_chars=160)
            for item in leaf_strings
            if str(item).strip()
        ][:3]

    bridge_text = _compact_narrative_brief_text(
        payload.get("bridge_text")
        or payload.get("bridge_to_current_page")
        or payload.get("bridge_from_current_page")
        or payload.get("transition_text")
        or payload.get("repair_note")
        or payload.get("resolution_action")
        or payload.get("reader_guidance")
        or payload.get("summary"),
        max_chars=220,
    )
    if not bridge_text:
        candidate_lines = [
            item
            for item in _narrative_brief_leaf_strings(payload, max_items=8)
            if item not in key_points
        ]
        if candidate_lines:
            bridge_text = _clean_reader_facing_excerpt_text(candidate_lines[0], max_chars=220)

    compact: Dict[str, Any] = {}
    if page > 0:
        compact["page"] = page
    if key_points:
        compact["key_points"] = key_points
    if bridge_text:
        compact["bridge_text"] = bridge_text
    return compact


def _build_reader_adjacent_preview_payload(raw_row: Any) -> Dict[str, Any]:
    row = _jsonable_dict(raw_row or {})
    if not row:
        return {}

    page = int(row.get("page") or 0)
    page_summary = _compact_narrative_brief_text(
        row.get("page_summary")
        or row.get("summary")
        or row.get("body_text")
        or row.get("raw_text"),
        max_chars=240,
    )

    continuation_points = [
        _clean_reader_facing_excerpt_text(str(item), max_chars=140)
        for item in list(row.get("continuation_hints") or [])
        if str(item).strip()
    ][:2]

    anchor_points: List[str] = []
    secondary_points: List[str] = []
    for item in list(row.get("content_stream") or []):
        payload = _jsonable_dict(item or {})
        item_type = str(payload.get("type") or "").strip().lower()
        label = _clean_reader_facing_excerpt_text(str(payload.get("label") or ""), max_chars=80)
        caption = _clean_reader_facing_excerpt_text(str(payload.get("caption") or ""), max_chars=110)
        text = _clean_reader_facing_excerpt_text(
            str(
                payload.get("text")
                or payload.get("normalized_text")
                or payload.get("description")
                or payload.get("ocr_text")
                or ""
            ),
            max_chars=110,
        )
        line = ""
        if item_type in {"figure", "table", "equation"}:
            anchor = label or caption or text
            if anchor:
                line = f"{item_type.title()} 焦点：{anchor}"
        elif item_type == "header":
            anchor = label or text
            if anchor:
                line = f"章节落点：{anchor}"
        elif item_type == "caption":
            anchor = caption or text
            if anchor:
                line = f"图注线索：{anchor}"
        elif item_type == "paragraph" and text:
            line = text
        if not line or line in continuation_points or line in anchor_points or line in secondary_points:
            continue
        if item_type in {"figure", "table", "equation", "header", "caption"}:
            anchor_points.append(line)
        else:
            secondary_points.append(line)
        if len(anchor_points) >= 2 and len(secondary_points) >= 1:
            break

    key_points = (continuation_points + anchor_points + secondary_points)[:4]

    preview: Dict[str, Any] = {}
    if page > 0:
        preview["page"] = page
    if page_summary:
        preview["summary"] = page_summary
    if key_points:
        preview["key_points"] = key_points
    return preview


def _build_reader_frame_from_narrative_brief(
    narrative_brief: Mapping[str, Any],
    *,
    reading_dossier: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    brief = _jsonable_dict(narrative_brief or {})
    continuity = _jsonable_dict(brief.get("continuity_resolutions") or {})
    dossier = _jsonable_dict(reading_dossier or {})
    opening_key_points = [
        _clean_reader_facing_excerpt_text(str(item), max_chars=180)
        for item in list(brief.get("opening_key_points") or [])
        if str(item).strip()
    ][:4]
    if not opening_key_points:
        opening_key_points = [
            _clean_reader_facing_excerpt_text(str(item), max_chars=180)
            for item in list(brief.get("reader_attention_order") or [])
            if str(item).strip()
        ][:4]

    previous_page_bridge = _compact_reader_bridge_payload(
        brief.get("previous_page_bridge")
        or continuity.get("from_previous_page")
        or continuity.get("previous_page")
    )
    next_page_bridge = _compact_reader_bridge_payload(
        brief.get("next_page_bridge")
        or continuity.get("to_next_page")
        or continuity.get("next_page")
    )
    adjacent_rows = [
        _jsonable_dict(item or {})
        for item in list((_jsonable_dict(dossier.get("adjacent_pages") or {})).get("pages") or [])
        if isinstance(item, Mapping)
    ]
    previous_page_preview = _build_reader_adjacent_preview_payload(
        next((row for row in adjacent_rows if str(row.get("relation") or "").strip() == "previous_page"), {})
    )
    next_page_preview = _build_reader_adjacent_preview_payload(
        next((row for row in adjacent_rows if str(row.get("relation") or "").strip() == "next_page"), {})
    )

    reader_opening: Dict[str, Any] = {}
    summary = _compact_narrative_brief_text(brief.get("current_page_main_arc"), max_chars=320)
    if summary:
        reader_opening["summary"] = summary
    if opening_key_points:
        reader_opening["key_points"] = opening_key_points
    if previous_page_bridge:
        reader_opening["previous_page_bridge"] = previous_page_bridge
    if previous_page_preview:
        reader_opening["previous_page_preview"] = previous_page_preview

    reader_outro: Dict[str, Any] = {}
    if next_page_bridge:
        reader_outro["next_page_bridge"] = next_page_bridge
    if next_page_preview:
        reader_outro["next_page_preview"] = next_page_preview

    return {
        "reader_opening": reader_opening,
        "reader_outro": reader_outro,
    }


def _compact_narrative_brief_payload(narrative_brief: Mapping[str, Any]) -> Dict[str, Any]:
    brief = _jsonable_dict(narrative_brief or {})
    reader_frame = _build_reader_frame_from_narrative_brief(brief)
    reader_opening = _jsonable_dict(reader_frame.get("reader_opening") or {})
    reader_outro = _jsonable_dict(reader_frame.get("reader_outro") or {})
    return {
        "focus_page": int(brief.get("focus_page") or 0),
        "current_page_main_arc": _compact_narrative_brief_text(brief.get("current_page_main_arc"), max_chars=520),
        "content_strategy": _compact_narrative_brief_text(brief.get("content_strategy"), max_chars=360),
        "presentation_strategy": _compact_narrative_brief_text(brief.get("presentation_strategy"), max_chars=360),
        "required_media_refs": list(brief.get("required_media_refs") or [])[:8],
        "opening_key_points": list(reader_opening.get("key_points") or [])[:4],
        "previous_page_bridge": _jsonable_dict(reader_opening.get("previous_page_bridge") or {}),
        "next_page_bridge": _jsonable_dict(reader_outro.get("next_page_bridge") or {}),
        "reader_attention_order": [
            _clean_reader_facing_excerpt_text(str(item), max_chars=180)
            for item in list(brief.get("reader_attention_order") or [])
            if str(item).strip()
        ][:6],
        "must_surface_nodes": [
            _clean_reader_facing_excerpt_text(str(item), max_chars=120)
            for item in list(brief.get("must_surface_nodes") or [])
            if str(item).strip()
        ][:8],
        "suppressed_threads": [
            _clean_reader_facing_excerpt_text(str(item), max_chars=120)
            for item in list(brief.get("suppressed_threads") or [])
            if str(item).strip()
        ][:6],
        "continuity_resolutions": _compact_narrative_brief_lines(brief.get("continuity_resolutions"), max_items=6, max_chars=320),
    }


async def _call_experience_session_v2_narrative_brief_model(
    *,
    system_prompt: str,
    user_prompt_payload: Mapping[str, Any],
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
) -> Dict[str, Any]:
    del provider
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": str(system_prompt or "")},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(_jsonable_dict(user_prompt_payload), ensure_ascii=False),
                        }
                    ],
                },
            ],
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout_seconds,
        ),
        timeout=timeout_seconds + 1.0,
    )
    try:
        content = str((response.choices[0].message.content or "")).strip()
    except Exception as exc:
        raise ValueError("narrative brief generation failed: model returned empty content") from exc
    parsed = await parse_json_dict_from_model_text(content)
    if not parsed:
        raise ValueError("narrative brief generation failed: invalid JSON output")
    return parsed


async def _generate_experience_session_v2_narrative_brief(
    *,
    reading_dossier: Mapping[str, Any],
    focus_page: int,
    reader_profile: str,
    user_intent: str,
) -> Dict[str, Any]:
    prompt_payload = _build_experience_session_v2_narrative_brief_prompt_payload(
        reading_dossier=reading_dossier,
        focus_page=focus_page,
        reader_profile=reader_profile,
        user_intent=user_intent,
    )
    config = _experience_session_v2_reader_agent_config()
    system_prompt = _experience_session_v2_narrative_brief_system_prompt()

    async def _call_with_prompt(active_system_prompt: str) -> Dict[str, Any]:
        return await _call_experience_session_v2_narrative_brief_model(
            system_prompt=active_system_prompt,
            user_prompt_payload=prompt_payload,
            provider=str(config.get("provider") or "").strip(),
            api_key=str(config.get("api_key") or "").strip(),
            base_url=str(config.get("base_url") or "").strip(),
            model=str(config.get("model") or "").strip(),
            timeout_seconds=float(config.get("timeout_seconds") or 0.0),
            max_tokens=int(config.get("max_tokens") or 0),
        )

    try:
        parsed = await _call_with_prompt(system_prompt)
    except ValueError as exc:
        if "invalid JSON output" not in str(exc):
            raise
        retry_prompt = (
            system_prompt
            + "24) Final reminder: return exactly one JSON object with no code fences, no commentary, and no prose outside the JSON object.\n"
        )
        parsed = await _call_with_prompt(retry_prompt)
    brief_payload = _validate_experience_session_v2_narrative_brief_payload(parsed)
    brief_meta = _jsonable_dict(brief_payload.get("meta") or {})
    brief_meta.update(
        {
            "generator_mode": "model_generated_bootstrap",
            "build_mode": "phase2_model_narrative_brief",
            "model_provider": str(config.get("provider") or "").strip(),
            "model_name": str(config.get("model") or "").strip(),
            "adjacent_context_pages": [
                int(_jsonable_dict(item).get("page") or 0)
                for item in list((_jsonable_dict(reading_dossier).get("adjacent_pages") or {}).get("pages") or [])
                if int(_jsonable_dict(item).get("page") or 0) > 0
            ],
            "prompt_contract": "experience_session_v2_narrative_brief_json_v2",
            "reader_profile": str(reader_profile or "").strip() or "curious_generalist",
            "user_intent_present": bool(str(user_intent or "").strip()),
        }
    )
    brief_payload["meta"] = brief_meta
    return ExperienceSessionV2NarrativeBrief.model_validate(brief_payload).model_dump(mode="json")


def _experience_session_v2_artifact_agent_config() -> Dict[str, Any]:
    provider = str(
        getattr(settings, "reader_artifact_agent_provider", "") or getattr(settings, "reader_agent_provider", "") or ""
    ).strip() or "aliyun"
    provider_config = dict(settings.get_llm_config(provider) or {})
    api_key = str(provider_config.get("api_key") or "").strip()
    base_url = str(provider_config.get("base_url") or "").strip()
    model = str(
        getattr(settings, "reader_artifact_agent_model", "")
        or getattr(settings, "reader_agent_model", "")
        or provider_config.get("model")
        or ""
    ).strip()
    timeout_ms = int(
        getattr(settings, "reader_artifact_agent_timeout_ms", 0)
        or getattr(settings, "reader_agent_timeout_ms", 90000)
        or 90000
    )
    max_tokens = int(
        getattr(settings, "reader_artifact_agent_max_tokens", 0)
        or getattr(settings, "reader_agent_max_tokens", 7000)
        or 7000
    )
    timeout_seconds = max(8.0, float(timeout_ms / 1000.0))
    max_tokens = max(1024, max_tokens)
    if not api_key or not base_url or not model:
        raise ValueError("artifact draft generation failed: reader artifact agent model unavailable")
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
    }


def _reader_experience_block_explain_system_prompt(explain_kind: str) -> str:
    normalized_kind = str(explain_kind or "").strip().lower()
    task_line = (
        "你要把当前图块讲清楚。"
        if normalized_kind == "figure"
        else "你要把当前这段讲读内容讲得更通俗。"
    )
    shape_line = (
        "7) 如果是在解释图表，尽量按三个短小标题分段：第一眼看什么 / 图里发现了什么 / 它支持了本页什么结论；每段 1-3 句，避免一整坨长段落。\n"
        if normalized_kind == "figure"
        else "7) 先用一句最直白的话点明核心意思，再用 2-3 小步把难点拆开讲，少堆术语。\n"
    )
    return (
        "你是当前论文页里的讲读助手，只能基于用户提供的当前块局部材料回答。\n"
        f"{task_line}\n"
        "硬性要求：\n"
        "1) 只使用当前块材料；不要检索知识库，不要提知识库、证据不足、检索结果、参考文献列表。\n"
        "2) 不要扩展到整篇论文，也不要讲到无关页面。\n"
        "3) 全部用简体中文回答，直接对读者说话。\n"
        "4) 如果材料有限，也要先基于现有材料尽量解释清楚，再用一句话指出当前块里还看不到什么；不要拒答。\n"
        "5) 语气像老师带着读，不要输出提示词、JSON、项目符号模板名或系统说明。\n"
        "6) 如果是 follow-up，请延续前面的讲解，不要从头重复整段。\n"
        f"{shape_line}"
    )


async def _create_reader_experience_block_explain_stream(
    *,
    client: AsyncOpenAI,
    request_kwargs: Dict[str, Any],
):
    try:
        return await client.chat.completions.create(
            **request_kwargs,
            extra_body={"enable_thinking": False},
        )
    except Exception as exc:
        message = str(exc).lower()
        disable_thinking_unsupported = (
            "enable_thinking" in message
            or "cannot unmarshal" in message
            or "invalid_request_error" in message
        )
        if not disable_thinking_unsupported:
            raise
        return await client.chat.completions.create(**request_kwargs)


def _reader_image_media_type(candidate_ext: str) -> str:
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(str(candidate_ext or "").strip().lower(), "application/octet-stream")


def _locate_reader_figure_asset_candidate_file(paper_id: int, page: int, asset_id: str) -> tuple[Optional[str], str]:
    normalized_asset_id = str(asset_id or "").strip()
    if not normalized_asset_id or not re.fullmatch(r"[0-9A-Za-z_.-]{1,96}", normalized_asset_id):
        return None, ""

    upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
    base_dir = os.path.abspath(
        os.path.join(upload_dir, "reader_figure_assets", str(int(paper_id)), f"p{int(page)}")
    )
    if not os.path.isdir(base_dir):
        return None, ""
    candidate_asset_ids = [
        normalized_asset_id
        if normalized_asset_id.endswith(f"_{GROUNDED_FIGURE_ASSET_VERSION}")
        else f"{normalized_asset_id}_{GROUNDED_FIGURE_ASSET_VERSION}"
    ]
    for candidate_asset_id in candidate_asset_ids:
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = os.path.abspath(os.path.join(base_dir, f"{candidate_asset_id}.{ext}"))
            if not path.startswith(base_dir + os.sep):
                continue
            if os.path.exists(path):
                return path, ext
    return None, ""


async def _resolve_reader_figure_asset_candidate_file(
    *,
    db: AsyncSession,
    paper: Paper,
    page: int,
    asset_id: str,
) -> tuple[Optional[str], str]:
    candidate_path, candidate_ext = _locate_reader_figure_asset_candidate_file(
        paper_id=int(paper.id),
        page=int(page),
        asset_id=asset_id,
    )
    if candidate_path:
        return candidate_path, candidate_ext

    normalized_asset_id = str(asset_id or "").strip()
    try:
        cache_stmt = (
            select(PaperReaderPageCache)
            .where(
                and_(
                    PaperReaderPageCache.paper_id == int(paper.id),
                    PaperReaderPageCache.page == int(page),
                    PaperReaderPageCache.source_signature.like("compose_v3|%"),
                )
            )
            .order_by(PaperReaderPageCache.updated_at.desc(), PaperReaderPageCache.id.desc())
            .limit(1)
        )
        cache_row = (await db.execute(cache_stmt)).scalar_one_or_none()
        payload_json = dict(getattr(cache_row, "payload_json", None) or {}) if cache_row else {}
        layouts = [
            row
            for row in list((payload_json.get("docmind_structure") or {}).get("layouts") or [])
            if isinstance(row, dict) and str(row.get("type") or "").strip().lower() == "figure"
        ]
        pdf_path = _resolve_local_pdf_path(user_id=int(paper.user_id), paper=paper)
        if layouts and pdf_path and os.path.exists(pdf_path):
            compose_service = get_literature_reader_compose_service()
            await asyncio.to_thread(
                compose_service._build_figure_assets_sync,  # pylint: disable=protected-access
                int(paper.id),
                int(page),
                str(pdf_path),
                payload_json,
                layouts,
            )
            candidate_path, candidate_ext = _locate_reader_figure_asset_candidate_file(
                paper_id=int(paper.id),
                page=int(page),
                asset_id=normalized_asset_id,
            )
    except Exception as exc:
        logger.warning(
            "[Literature API] figure asset lazy-build failed "
            f"paper={paper.id} page={page} asset_id={normalized_asset_id}: {exc}"
        )
    return candidate_path, candidate_ext


def _encode_local_image_file_as_data_url(path: str, media_type: str) -> str:
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


async def _normalize_reader_experience_block_explain_image_url(
    *,
    db: AsyncSession,
    paper: Paper,
    raw_url: str,
) -> str:
    image_url = str(raw_url or "").strip()
    if not image_url or image_url.startswith("data:image/"):
        return image_url

    parsed = urlparse(image_url)
    candidate_path = str(parsed.path or image_url).strip() if parsed.scheme else image_url
    match = _READER_FIGURE_ASSET_PATH_RE.fullmatch(candidate_path)
    if not match:
        return image_url

    target_paper_id = int(match.group("paper_id") or 0)
    target_page = int(match.group("page") or 0)
    asset_id = str(match.group("asset_id") or "").strip()
    if target_paper_id != int(paper.id):
        raise ValueError("当前图块图片资源与当前论文不匹配，无法发起图解释。")
    if target_page <= 0 or not asset_id:
        raise ValueError("当前图块图片资源地址不完整，无法发起图解释。")

    candidate_file, candidate_ext = await _resolve_reader_figure_asset_candidate_file(
        db=db,
        paper=paper,
        page=target_page,
        asset_id=asset_id,
    )
    if not candidate_file or not candidate_ext:
        raise ValueError("当前图块图片资源不存在或尚未生成，无法只解释这张图。")

    media_type = _reader_image_media_type(candidate_ext)
    if not media_type.startswith("image/"):
        raise ValueError(f"当前图块图片类型不可用：{candidate_ext or 'unknown'}")
    try:
        return _encode_local_image_file_as_data_url(candidate_file, media_type)
    except Exception as exc:
        raise ValueError(f"当前图块图片读取失败：{str(exc).strip() or 'unknown error'}") from exc


def _friendly_reader_experience_block_explain_error_message(exc: Exception) -> str:
    message = str(exc or "").strip() or "局部讲解失败"
    lowered = message.lower()
    if "provided url does not appear to be valid" in lowered:
        return "当前图块图片地址无效，模型无法读取。请刷新当前页后重试。"
    if "image length and width do not meet the model restrictions" in lowered:
        return "当前图块图片尺寸不符合模型限制，无法发起图解释。"
    return message


def _build_reader_experience_block_explain_messages(
    payload: ReaderExperienceBlockExplainRequest,
) -> List[Dict[str, Any]]:
    explain_kind = str(payload.explain_kind or "").strip().lower()
    context_lines: List[str] = [f"当前页：第 {int(payload.page)} 页", f"当前块 ID：{str(payload.block_id).strip()}"]
    initial_content: Any = ""

    if explain_kind == "figure":
        figure_label = str(payload.figure_label or "").strip()
        figure_caption = str(payload.figure_caption or "").strip()
        figure_text = str(payload.figure_text or "").strip()
        figure_image_url = str(payload.figure_image_url or "").strip()
        if figure_label:
            context_lines.append(f"图块标签：{figure_label}")
        if figure_caption:
            context_lines.append(f"图块说明：{figure_caption}")
        if figure_text and figure_text != figure_caption:
            context_lines.append(f"图块正文：{figure_text}")
        text_payload = (
            "以下是当前图块的固定局部材料。后续回答只允许基于这些材料。\n\n"
            + "\n".join(context_lines)
        )
        if figure_image_url:
            initial_content = [
                {"type": "image_url", "image_url": {"url": figure_image_url}},
                {"type": "text", "text": text_payload},
            ]
        else:
            initial_content = text_payload
    else:
        source_excerpt = str(payload.source_excerpt or "").strip()
        source_translation = str(payload.source_translation_zh or "").strip()
        explanation_text = str(payload.explanation_text or "").strip()
        if source_excerpt:
            context_lines.append(f"原文摘录：{source_excerpt}")
        if source_translation:
            context_lines.append(f"原文中文译文：{source_translation}")
        if explanation_text:
            context_lines.append(f"当前讲读：{explanation_text}")
        initial_content = (
            "以下是当前块的固定局部材料。后续回答只允许基于这些材料。\n\n"
            + "\n".join(context_lines)
        )

    messages: List[Dict[str, str]] = [
        {
            "role": "user",
            "content": initial_content,
        }
    ]
    for turn in list(payload.history or [])[-12:]:
        role = str(getattr(turn, "role", "") or "").strip().lower()
        content = str(getattr(turn, "content", "") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": str(payload.question or "").strip()})
    return messages


def _compact_experience_v2_block_rewrite_text(raw: Any, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if len(text) <= max_chars:
        return text
    return text[: max(1, int(max_chars) - 1)].rstrip() + "…"


def _find_experience_v2_block_index(
    blocks: Sequence[Mapping[str, Any]],
    block_id: str,
) -> int:
    target_id = str(block_id or "").strip()
    for index, block in enumerate(blocks):
        if str((_jsonable_dict(block)).get("segment_id") or "").strip() == target_id:
            return index
    return -1


def _experience_v2_block_rewrite_is_supported(block: Mapping[str, Any]) -> bool:
    return str(_jsonable_dict(block).get("segment_kind") or "").strip() in _EXPERIENCE_V2_BLOCK_REWRITE_SUPPORTED_SEGMENT_KINDS


def _serialize_experience_v2_block_for_rewrite_context(
    block: Mapping[str, Any],
    *,
    max_chars: int = 260,
) -> Dict[str, Any]:
    payload = PageArtifactV2ReadingBlock.model_validate(_jsonable_dict(block)).model_dump(mode="json")
    meta = _jsonable_dict(payload.get("meta") or {})
    label = str(
        meta.get("reader_title")
        or meta.get("display_term")
        or meta.get("label")
        or meta.get("group_label")
        or ""
    ).strip()
    return {
        "segment_id": str(payload.get("segment_id") or "").strip(),
        "segment_kind": str(payload.get("segment_kind") or "").strip(),
        "text": _compact_experience_v2_block_rewrite_text(payload.get("text") or "", max_chars=max_chars),
        "label": label,
        "reader_role": str(meta.get("reader_role") or "").strip(),
        "group_label": str(meta.get("group_label") or meta.get("section_label") or "").strip(),
        "placement": str(meta.get("placement") or "").strip(),
        "lane": str(meta.get("lane") or "").strip(),
    }


def _find_nearest_experience_v2_excerpt_context(
    blocks: Sequence[Mapping[str, Any]],
    *,
    target_index: int,
    preferred_group_id: str,
) -> Dict[str, Any]:
    def _matches_group(candidate: Mapping[str, Any]) -> bool:
        if not preferred_group_id:
            return False
        meta = _jsonable_dict(_jsonable_dict(candidate).get("meta") or {})
        candidate_group_id = str(meta.get("group_id") or meta.get("section_id") or "").strip()
        return bool(candidate_group_id and candidate_group_id == preferred_group_id)

    for group_only in (True, False):
        for offset in range(0, len(blocks)):
            candidate_indexes = [target_index - offset]
            if offset > 0:
                candidate_indexes.append(target_index + offset)
            for candidate_index in candidate_indexes:
                if candidate_index < 0 or candidate_index >= len(blocks):
                    continue
                candidate = _jsonable_dict(blocks[candidate_index])
                if str(candidate.get("segment_kind") or "").strip() != "original_excerpt":
                    continue
                if group_only and not _matches_group(candidate):
                    continue
                meta = _jsonable_dict(candidate.get("meta") or {})
                return {
                    "segment_id": str(candidate.get("segment_id") or "").strip(),
                    "text": _compact_experience_v2_block_rewrite_text(candidate.get("text") or "", max_chars=420),
                    "translation_zh": _compact_experience_v2_block_rewrite_text(
                        meta.get("translation_zh") or meta.get("reader_translation_zh") or "",
                        max_chars=420,
                    ),
                }
    return {}


def _experience_v2_block_rewrite_system_prompt(segment_kind: str) -> str:
    normalized_kind = str(segment_kind or "").strip().lower()
    tone_hint = {
        "heading": "保持标题感，简洁、有方向感，不要写成长段。",
        "paragraph": "保持讲读段落口吻，帮助读者顺着当前页主线理解。",
        "authored_explanation": "保持讲读段落口吻，帮助读者顺着当前页主线理解。",
        "aside_content": "保持页边提示或旁注口吻，简洁、补充性强，不要喧宾夺主。",
        "term_annotation": "保持术语注释口吻，先说概念，再说它在这一页为什么重要。",
    }.get(normalized_kind, "保持当前块在页面中的角色与语气。")
    return (
        "你是论文阅读页里的局部改写助手，只允许重写一个现成 block 的 reader-facing text。\n"
        "硬性要求：\n"
        "1) 只返回 strict JSON，格式必须是 {\"text\":\"...\"}。\n"
        "2) 只改写目标 block 的 text；不要新增字段，不要输出 markdown code fence。\n"
        "3) 不要改变 block 的功能、位置、结构、source ids、evidence ids、media binding 或 external resource 绑定。\n"
        "4) 只基于提供的当前块与局部上下文改写，不要编造新事实，不要引入新的来源或邻页情节。\n"
        "5) 全部用简体中文输出，优先满足用户的改写要求。\n"
        f"6) {tone_hint}\n"
        "7) 如果当前块已经足够好，也要按用户提示做出可见但克制的优化，而不是原样照抄。\n"
    )


def _build_experience_v2_block_rewrite_prompt_payload(
    *,
    paper: Paper,
    artifact_payload: Mapping[str, Any],
    narrative_brief: Mapping[str, Any],
    block_id: str,
    rewrite_prompt: str,
    reader_profile: str,
    user_intent: str,
) -> Dict[str, Any]:
    artifact = PageArtifactV2.model_validate(_jsonable_dict(artifact_payload)).model_dump(mode="json")
    blocks = list(artifact.get("reading_blocks") or [])
    target_index = _find_experience_v2_block_index(blocks, block_id)
    if target_index < 0:
        raise ValueError("block rewrite failed: target block not found in current artifact")
    target_block = PageArtifactV2ReadingBlock.model_validate(_jsonable_dict(blocks[target_index])).model_dump(mode="json")
    if not _experience_v2_block_rewrite_is_supported(target_block):
        raise ValueError("block rewrite failed: current block kind does not support local rewrite")

    target_meta = _jsonable_dict(target_block.get("meta") or {})
    preferred_group_id = str(target_meta.get("group_id") or target_meta.get("section_id") or "").strip()
    previous_blocks = [
        _serialize_experience_v2_block_for_rewrite_context(blocks[index])
        for index in range(max(0, target_index - 2), target_index)
    ]
    next_blocks = [
        _serialize_experience_v2_block_for_rewrite_context(blocks[index])
        for index in range(target_index + 1, min(len(blocks), target_index + 3))
    ]
    nearest_excerpt = _find_nearest_experience_v2_excerpt_context(
        blocks,
        target_index=target_index,
        preferred_group_id=preferred_group_id,
    )
    artifact_meta = _jsonable_dict(artifact.get("meta") or {})
    reader_opening = _jsonable_dict(artifact_meta.get("reader_opening") or {})
    compact_brief = _compact_narrative_brief_payload(narrative_brief or {}) if narrative_brief else {}

    return {
        "task": "Rewrite exactly one page_artifact_v2 reading block and return strict JSON only.",
        "focus_page": int(artifact.get("focus_page") or 1),
        "reader_profile": str(reader_profile or "").strip() or "curious_generalist",
        "user_intent": str(user_intent or "").strip(),
        "user_rewrite_prompt": str(rewrite_prompt or "").strip(),
        "paper": {
            "title": _compact_experience_v2_block_rewrite_text(paper.title or "", max_chars=220),
            "abstract": _compact_experience_v2_block_rewrite_text(paper.abstract or "", max_chars=420),
        },
        "page_context": {
            "reader_opening_summary": _compact_experience_v2_block_rewrite_text(
                reader_opening.get("summary") or compact_brief.get("current_page_main_arc") or "",
                max_chars=320,
            ),
            "opening_key_points": list(reader_opening.get("key_points") or compact_brief.get("opening_key_points") or [])[:4],
            "current_page_main_arc": _compact_experience_v2_block_rewrite_text(
                compact_brief.get("current_page_main_arc") or "",
                max_chars=320,
            ),
        },
        "rewrite_contract": {
            "editable_field": "text_only",
            "segment_id_must_stay_same": str(target_block.get("segment_id") or "").strip(),
            "segment_kind_must_stay_same": str(target_block.get("segment_kind") or "").strip(),
            "do_not_change": [
                "segment_id",
                "segment_kind",
                "source_layout_ids",
                "source_block_ids",
                "evidence_ids",
                "meta bindings",
                "external resource urls",
            ],
            "output_schema": {"text": "string"},
        },
        "target_block": {
            **_serialize_experience_v2_block_for_rewrite_context(target_block, max_chars=1800),
            "current_text": str(target_block.get("text") or "").strip(),
            "source_layout_ids": list(target_block.get("source_layout_ids") or []),
            "source_block_ids": list(target_block.get("source_block_ids") or []),
            "evidence_ids": list(target_block.get("evidence_ids") or []),
        },
        "local_context": {
            "previous_blocks": previous_blocks,
            "next_blocks": next_blocks,
            "nearest_original_excerpt": nearest_excerpt,
        },
    }


def _validate_experience_v2_block_rewrite_model_payload(payload: Mapping[str, Any]) -> Dict[str, str]:
    normalized = _jsonable_dict(payload)
    text = str(normalized.get("text") or normalized.get("rewritten_text") or "").strip()
    if not text:
        raise ValueError("block rewrite failed: model returned empty text")
    return {"text": text}


def _apply_experience_v2_block_rewrite_to_artifact(
    *,
    artifact_payload: Mapping[str, Any],
    block_id: str,
    rewritten_text: str,
    rewrite_prompt: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    artifact = PageArtifactV2.model_validate(_jsonable_dict(artifact_payload)).model_dump(mode="json")
    blocks = [PageArtifactV2ReadingBlock.model_validate(_jsonable_dict(item)).model_dump(mode="json") for item in list(artifact.get("reading_blocks") or [])]
    target_index = _find_experience_v2_block_index(blocks, block_id)
    if target_index < 0:
        raise ValueError("block rewrite failed: target block not found in current artifact")
    target_block = _jsonable_dict(blocks[target_index])
    if not _experience_v2_block_rewrite_is_supported(target_block):
        raise ValueError("block rewrite failed: current block kind does not support local rewrite")

    updated_block = dict(target_block)
    updated_block["text"] = str(rewritten_text or "").strip()
    block_meta = _jsonable_dict(updated_block.get("meta") or {})
    block_meta["manual_rewrite"] = {
        "updated_at": datetime.utcnow().isoformat(),
        "source": "user_prompt",
        "prompt_excerpt": _compact_experience_v2_block_rewrite_text(rewrite_prompt, max_chars=160),
        "overwritable_by_regenerate": True,
    }
    updated_block["meta"] = block_meta
    updated_block = PageArtifactV2ReadingBlock.model_validate(updated_block).model_dump(mode="json")
    blocks[target_index] = updated_block
    artifact["reading_blocks"] = blocks

    artifact_meta = _jsonable_dict(artifact.get("meta") or {})
    rewrite_entries = [
        item
        for item in list(artifact_meta.get("manual_block_rewrites") or [])
        if str(_jsonable_dict(item).get("segment_id") or "").strip() != str(block_id or "").strip()
    ]
    rewrite_entries.append(
        {
            "segment_id": str(updated_block.get("segment_id") or "").strip(),
            "segment_kind": str(updated_block.get("segment_kind") or "").strip(),
            "updated_at": datetime.utcnow().isoformat(),
            "source": "user_prompt",
            "overwritable_by_regenerate": True,
        }
    )
    artifact_meta["manual_block_rewrites"] = rewrite_entries[-24:]
    artifact["meta"] = artifact_meta
    artifact = PageArtifactV2.model_validate(artifact).model_dump(mode="json")
    return artifact, updated_block


def _build_reader_v2_seed_resource_bundle(
    *,
    paper: Paper,
    compose_payload: Mapping[str, Any],
    narrative_brief: Mapping[str, Any],
) -> Dict[str, Any]:
    seed_resources = _collect_reader_v2_external_resources(paper=paper, compose_payload=compose_payload)
    bundle_entries: List[Dict[str, Any]] = []
    for idx, resource in enumerate(seed_resources, start=1):
        payload = _jsonable_dict(resource)
        url = str(payload.get("url") or "").strip()
        label = str(payload.get("label") or url or f"resource-{idx}").strip()
        bundle_entries.append(
            {
                "resource_id": f"seed:{idx}",
                "label": label,
                "url": url,
                "resource_type": str(payload.get("resource_type") or "seed").strip() or "seed",
                "summary": label,
                "source_tool": "seed",
                "renderable": bool(url),
                "meta": {"seeded": True},
            }
        )
    continuity_rows = _compact_narrative_brief_lines(
        _jsonable_dict(narrative_brief).get("continuity_resolutions"),
        max_items=4,
        max_chars=220,
    )
    required_media_refs = [
        _jsonable_dict(item)
        for item in list((_jsonable_dict(narrative_brief).get("required_media_refs") or []))
        if isinstance(item, Mapping)
    ]
    return {
        "bundle_entries": bundle_entries,
        "external_resources": [
            {
                "resource_id": entry["resource_id"],
                "label": entry["label"],
                "url": entry["url"],
                "resource_type": entry["resource_type"],
            }
            for entry in bundle_entries
            if bool(entry.get("renderable")) and str(entry.get("url") or "").strip()
        ],
        "required_media_refs": required_media_refs[:8],
        "continuity_resolutions": continuity_rows[:4],
        "meta": {
            "bundle_mode": "phase3_seeded_resource_bundle",
            "retrieval_rounds": 0,
            "resource_request_affordance": {
                "available": True,
                "intent": "learning_support_required_initial_web_round",
                "must_use_tools": True,
                "minimum_initial_web_requests": 1,
                "maximum_initial_web_requests": 2,
                "guidance": (
                    "Seeded paper/doi/pdf links are only a starting bundle. "
                    "The first artifact-draft pass must request one or two public-web resources "
                    "that improve reader-facing learning support beyond paper/doi/pdf links."
                ),
                "allowed_tools": ["paper_read", "knowledge_search", "web_search", "web_scrape"],
                "suggested_resource_kinds": [
                    "term_explainer",
                    "encyclopedia_background",
                    "official_docs_or_tutorial",
                    "video_explainer",
                ],
                "suggested_targets": [
                    "Wikipedia or comparable encyclopedia entries",
                    "official documentation/tutorial pages",
                    "YouTube video pages",
                    "Bilibili video pages",
                ],
            },
        },
    }


def _resource_bundle_has_nonseed_public_web_entries(resource_bundle: Mapping[str, Any]) -> bool:
    bundle = _jsonable_dict(resource_bundle or {})
    for raw_entry in list(bundle.get("bundle_entries") or []):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = _jsonable_dict(raw_entry)
        url = str(entry.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        source_tool = str(entry.get("source_tool") or "").strip()
        meta = _jsonable_dict(entry.get("meta") or {})
        if source_tool and source_tool != "seed":
            return True
        if not bool(meta.get("seeded")):
            return True
    return False


def _experience_v2_artifact_draft_can_finalize_with_current_resources(
    artifact_draft: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
) -> bool:
    draft = _jsonable_dict(artifact_draft or {})
    nodes = [item for item in list(draft.get("nodes") or []) if isinstance(item, Mapping)]
    if not nodes:
        return False

    substantive_node_count = 0
    for raw_node in nodes:
        node = _jsonable_dict(raw_node)
        node_kind = str(node.get("node_kind") or "").strip()
        if node_kind not in _ARTIFACT_DRAFT_V2_SUPPORTED_NODE_KINDS:
            continue
        if any(
            [
                str(node.get("text") or "").strip(),
                str(node.get("display_text") or "").strip(),
                str(node.get("translation_zh") or "").strip(),
                str(node.get("label") or "").strip(),
                str(node.get("caption") or "").strip(),
                str(node.get("term") or "").strip(),
                str(node.get("definition") or "").strip(),
                list(node.get("resource_ref_ids") or []),
            ]
        ):
            substantive_node_count += 1

    return substantive_node_count >= 3 and _resource_bundle_has_nonseed_public_web_entries(resource_bundle)


def _build_page_artifact_v2_compact_source_context(
    *,
    reading_dossier: Mapping[str, Any],
    focus_page: int,
) -> Dict[str, Any]:
    draft_excerpt_candidate_max_chars: Optional[int] = None
    dossier = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    current_lane = _jsonable_dict(dossier.get("current_page") or {})
    rich_grounding = _jsonable_dict(current_lane.get("rich_grounding") or {})
    reading_nodes = list(rich_grounding.get("reading_nodes") or [])
    layout_atoms = list(rich_grounding.get("layout_atoms") or [])

    excerpt_candidates: List[Dict[str, Any]] = []
    for node in reading_nodes:
        if not isinstance(node, Mapping):
            continue
        if not bool(node.get("include_in_main_flow", True)):
            continue
        node_kind = str(node.get("node_kind") or (_jsonable_dict(node.get("meta") or {})).get("layout_type") or "").strip().lower()
        text = str(node.get("clean_text") or node.get("normalized_text") or node.get("raw_text") or "").strip()
        if not text or _is_ocr_heavy_excerpt_candidate(text=text, node_kind=node_kind):
            continue
        excerpt_candidates.extend(
            {
                "display_text": str(item.get("text") or "").strip(),
                "node_kind": node_kind or "paragraph",
                "source_layout_ids": list(item.get("layout_ids") or []),
                "source_block_ids": list(item.get("block_ids") or []),
            }
            for item in _split_current_page_excerpt_row(
                text=text,
                layout_ids=[str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()],
                block_ids=[str(item).strip() for item in list(node.get("source_block_ids") or []) if str(item).strip()],
                node_id=str(node.get("node_id") or "").strip(),
                max_chars=draft_excerpt_candidate_max_chars,
            )
            if str(item.get("text") or "").strip()
        )
    for atom in layout_atoms:
        if not isinstance(atom, Mapping):
            continue
        if not bool(atom.get("include_in_main_flow", True)):
            continue
        layout_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        text = str(atom.get("clean_text") or atom.get("normalized_text") or atom.get("raw_text") or "").strip()
        if not text:
            continue
        allow_media_caption_excerpt = _allows_media_caption_excerpt(layout_kind)
        if (not allow_media_caption_excerpt) and _is_ocr_heavy_excerpt_candidate(text=text, node_kind=layout_kind):
            continue
        excerpt_candidates.extend(
            {
                "display_text": str(item.get("text") or "").strip(),
                "node_kind": layout_kind or "paragraph",
                "source_layout_ids": list(item.get("layout_ids") or []),
                "source_block_ids": list(item.get("block_ids") or []),
            }
            for item in _split_current_page_excerpt_row(
                text=text,
                layout_ids=[str(atom.get("layout_id") or "").strip()] if str(atom.get("layout_id") or "").strip() else [],
                block_ids=[str(item).strip() for item in list(atom.get("canonical_block_ids") or []) if str(item).strip()],
                node_id="",
                max_chars=draft_excerpt_candidate_max_chars,
            )
            if str(item.get("text") or "").strip()
        )
    media_candidates: Dict[str, List[Dict[str, Any]]] = {"figure": [], "table": [], "equation": []}
    for atom in layout_atoms:
        if not isinstance(atom, Mapping):
            continue
        layout_kind = str(atom.get("layout_type") or atom.get("node_kind") or "").strip().lower()
        if layout_kind not in media_candidates:
            continue
        layout_id = str(atom.get("layout_id") or "").strip()
        if not layout_id:
            continue
        media_candidates[layout_kind].append(
            {
                "label": _compact_media_slot_label(
                    str(atom.get("clean_text") or atom.get("normalized_text") or layout_id).strip(),
                    f"{layout_kind.title()} {len(media_candidates[layout_kind]) + 1}",
                ),
                "source_layout_id": layout_id,
            }
        )
    return {
        "focus_page": int(focus_page),
        "excerpt_candidates": excerpt_candidates[:14],
        "media_candidates": media_candidates,
    }


def _build_experience_session_v2_artifact_draft_prompt_payload(
    *,
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
    previous_draft: Optional[Mapping[str, Any]] = None,
    include_full_dossier: bool,
) -> Dict[str, Any]:
    dossier = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload)).model_dump(mode="json")
    narrative_brief = _find_latest_experience_session_v2_narrative_brief(session)
    if not narrative_brief:
        raise ValueError("artifact draft generation failed: narrative brief layer missing in session execution")
    focus_page = int(dossier.get("focus_page") or session.get("focus_page") or 1)
    bundle = _jsonable_dict(resource_bundle or {})
    must_request_public_web_resources = not _resource_bundle_has_nonseed_public_web_entries(bundle)
    payload: Dict[str, Any] = {
        "task": "Generate experience_session_v2 artifact_draft JSON for bounded Phase 3 drafting.",
        "mode": "bootstrap_full_context" if include_full_dossier else "revise_compact_context",
        "rules": {
            "current_page_primary": True,
            "adjacent_pages_support_only": True,
            "generate_reader_facing_text": True,
            "reader_facing_language": "zh-CN",
            "generate_html": False,
            "generate_js": False,
            "strict_json_only": True,
            "external_resources_must_reference_bundle_entries": True,
            "if_additional_resources_are_needed": "emit resource_requests instead of inventing URLs",
            "allowed_node_kinds": sorted(_ARTIFACT_DRAFT_V2_SUPPORTED_NODE_KINDS),
            "allowed_tools_for_resource_requests": ["paper_read", "knowledge_search", "web_search", "web_scrape"],
            "max_resource_requests_per_round": 2,
            "minimum_public_web_resource_requests_before_finalizing": 1 if must_request_public_web_resources else 0,
            "content_completeness_priority": True,
            "do_not_optimize_for_brevity_only": True,
            "keep_reader_page_coherent_and_substantive": True,
            "prefer_section_grouping_over_flat_card_dump": True,
            "prefer_inline_excerpt_when_it_keeps_the_main_reading_flow_clear": True,
        },
        "teaching_sequence_preferences": {
            "target_shape": "ordered_teaching_node_sequence",
            "authored_language": "zh-CN",
            "preferred_reader_roles": [
                "anchor_excerpt",
                "teaching_explanation",
                "continuity_bridge",
                "support_note",
                "visual_evidence",
            ],
            "default_main_flow_pattern": [
                "heading",
                "original_excerpt",
                "paragraph",
                "original_excerpt",
                "paragraph",
            ],
            "excerpt_expectation": "Prefer multiple short excerpt anchors over one long excerpt dump. Keep most excerpts to 1-3 sentences or roughly <= 420 characters when possible.",
            "grouping_expectation": "Use group_id/group_label or section_id/section_label so each teaching run reads like a coherent mini-section rather than isolated cards.",
            "bridge_expectation": "If continuity from neighboring pages matters, absorb it into a short paragraph with reader_role=continuity_bridge instead of exposing page-boundary repair as the main headline.",
            "support_expectation": "Use term_note/aside only when they materially help the local teaching flow. Omit low-value support.",
        },
        "narrative_brief": narrative_brief,
        "resource_bundle": {
            "bundle_entries": [
                {
                    "resource_id": str(_jsonable_dict(item).get("resource_id") or "").strip(),
                    "label": str(_jsonable_dict(item).get("label") or "").strip(),
                    "url": str(_jsonable_dict(item).get("url") or "").strip(),
                    "resource_type": str(_jsonable_dict(item).get("resource_type") or "").strip(),
                    "summary": str(_jsonable_dict(item).get("summary") or "").strip(),
                    "renderable": bool(_jsonable_dict(item).get("renderable", False)),
                }
                for item in list(bundle.get("bundle_entries") or [])
                if isinstance(item, Mapping)
            ][:24],
            "required_media_refs": list(bundle.get("required_media_refs") or [])[:16],
            "continuity_resolutions": list(bundle.get("continuity_resolutions") or [])[:8],
            "meta": {
                "resource_request_affordance": _jsonable_dict(_jsonable_dict(bundle.get("meta") or {}).get("resource_request_affordance") or {}),
            },
        },
        "anchor_excerpt_candidates": _build_page_artifact_v2_compact_source_context(
            reading_dossier=dossier,
            focus_page=focus_page,
        ).get("excerpt_candidates", [])[:12],
    }
    if include_full_dossier:
        payload["reading_dossier_v2"] = dossier
    else:
        payload["compact_source_context"] = _build_page_artifact_v2_compact_source_context(
            reading_dossier=dossier,
            focus_page=focus_page,
        )
        if isinstance(previous_draft, Mapping):
            payload["previous_artifact_draft"] = ExperienceSessionV2ArtifactDraft.model_validate(
                _jsonable_dict(previous_draft)
            ).model_dump(mode="json")
    return payload


def _experience_session_v2_artifact_draft_system_prompt() -> str:
    return (
        "You are generating the bounded Phase 3 artifact_draft for experience_session_v2.\n"
        "This is a structured drafting object, not the final rendered page and not HTML.\n"
        "Hard rules:\n"
        "1) The current page is the only primary narrative anchor.\n"
        "2) Neighboring pages are support context only for continuity and figure/table/equation understanding.\n"
        "3) Ground authored content in the provided dossier, narrative_brief, and retrieved resources; do not invent unsupported claims.\n"
        "4) Generate most reader-facing text directly in nodes; do not leave the page to helper-written prose.\n"
        "5) Distinguish authored narrative from original_excerpt nodes.\n"
        "6) original_excerpt.display_text may lightly fix OCR, spacing, charset, or stuck-text issues, but must remain an excerpt rather than a paraphrase.\n"
        "7) The draft should be sufficiently complete to support a full guided-reading page; do not underwrite the page just to stay short.\n"
        "8) Images, tables, and equations must be emitted as structured slots, never as HTML/markdown.\n"
        "9) External-resource nodes may only reference bundle entry IDs that already exist in the provided resource_bundle.\n"
        "10) If more resources are needed, emit resource_requests with explicit allowed tools; do not invent URLs.\n"
        "11) Output strict JSON only.\n"
        "12) Required top-level fields: focus_page, template_hint, layout_recipe, presentation_mode, nodes, resource_requests.\n"
        "13) Supported node kinds: heading, paragraph, original_excerpt, figure_slot, table_slot, equation_slot, aside, term_note, external_resource.\n"
        "14) Do not emit markdown, HTML, JavaScript, or final renderer code.\n"
        "15) The nodes array must read like an ordered teaching-node sequence for the current page, not a loose registry dump.\n"
        "16) In the main reading lane, prefer short anchor excerpts followed immediately by teaching paragraphs that unpack them.\n"
        "17) Use reader_role in node meta with these exact values when relevant: anchor_excerpt, teaching_explanation, continuity_bridge, support_note, visual_evidence.\n"
        "18) Use group_id/group_label or section_id/section_label so the node sequence forms coherent teaching sections.\n"
        "19) Do not make continuity repair the dominant opening of the page unless it is genuinely the page's main task.\n"
        "20) Prefer several smaller original_excerpt nodes over a single very long excerpt. Avoid excerpt dumps.\n"
        "21) Insert figure/table/equation slots at the point where they advance the explanation; do not dump them separately from the teaching flow.\n"
        "22) Use support_note nodes sparingly. A page may have little or no support rail if the main flow is sufficient.\n"
        "22.1) Use figure/table/equation slots only when they can bind to current-page media with source_layout_ids. If no current-page anchor exists, prefer external_resource when resource_ref_ids are available; otherwise explain the visual idea as paragraph/aside instead of emitting an unbound slot.\n"
        "23) Use these exact node fields inside nodes[] only:\n"
        '    - heading: {"node_kind":"heading","text":"..."}\n'
        '    - paragraph: {"node_kind":"paragraph","text":"..."}\n'
        '    - original_excerpt: {"node_kind":"original_excerpt","display_text":"...","translation_zh":"...","source_layout_ids":["..."],"source_block_ids":["..."]}\n'
        '    - figure_slot/table_slot/equation_slot (current-page bound media only): {"node_kind":"figure_slot","label":"...","caption":"...","source_layout_ids":["..."]}\n'
        '    - aside: {"node_kind":"aside","text":"..."}\n'
        '    - term_note: {"node_kind":"term_note","term":"...","definition":"..."}\n'
        '    - external_resource: {"node_kind":"external_resource","label":"...","resource_ref_ids":["resource_id"]}\n'
        "24) resource_requests is a top-level array, not a node. Never place resource_request objects inside nodes[].\n"
        '    - web search request inside resource_requests[]: {"request_id":"req-...","tool_name":"web_search","query":"...","reason":"...","max_results":3}\n'
        '    - web scrape request inside resource_requests[]: {"request_id":"req-...","tool_name":"web_scrape","url":"https://...","reason":"...","max_results":1}\n'
        "25) Use node meta to express page composition when helpful: lane(main|support), placement(block|inline|rail), prominence(hero|primary|secondary), group_id, group_label, section_id, section_label, reader_role.\n"
        "26) Do not emit unnecessary support blocks. If support content is low-value, omit it instead of filling the rail.\n"
        "27) Do not auto-surface every excerpt or media item; surface only what the draft needs for the reader-facing page.\n"
        "28) Do not embed full resource objects inside nodes; use resource_ref_ids only.\n"
        "29) Do not replace required field names with aliases such as content, body, source_layout_id, resource_refs, resources, or tool.\n"
        "30) All reader-facing authored text must be written in Simplified Chinese, including heading.text, paragraph.text, aside.text, term_note.definition, and authored external_resource labels.\n"
        "31) Keep canonical labels such as Figure 3, DOI, USMLE, URLs, and source titles in their original form when they are the clearest anchor.\n"
        "32) original_excerpt.display_text should remain the source-language excerpt with only light OCR/spacing fixes.\n"
        "33) For every original_excerpt node, also provide translation_zh as a faithful Simplified Chinese translation shown to the reader beneath the excerpt.\n"
        "34) translation_zh should preserve the meaning of the excerpt and may smooth OCR/spacing issues, but it must not add claims that are absent from the source excerpt.\n"
        "35) For term_note, explain the concept in Chinese and include the original English term in parentheses on first mention when helpful.\n"
        "36) template_hint, layout_recipe, and presentation_mode should normally be concise strings; if you need structured planning detail, put it under meta rather than replacing those fields with objects.\n"
        "37) Prefer natural Chinese technical phrasing. Avoid awkward hyphenated compounds such as '答案-解释一致性'; prefer forms like '答案与解释的一致性' and keep the English term in parentheses on first mention when helpful.\n"
        "38) Seeded paper/doi/pdf links are not the only allowed external resources. The first draft round should request 1 or 2 public-web resources beyond seed links unless such resources are already present in the bundle.\n"
        "39) resource_requests may be used for term explainers, encyclopedia background, official docs/tutorials, and video explainer pages such as YouTube or Bilibili pages when they materially help the reader.\n"
        "40) At least one initial resource_request should use web_search or web_scrape. Prefer web_search when the exact URL is not already known.\n"
        "41) After public-web resources are retrieved, prefer surfacing at least one materially useful external_resource node from those fetched results rather than showing seed paper/doi links only.\n"
        "42) If previous_artifact_draft is provided and resource_bundle already contains non-seed public-web resources, revise the page with those fetched resources first and prefer finalizing without more resource_requests.\n"
        "43) Do not keep emitting resource_requests just to gather adjacent explainers, near-duplicate pages, or alternate phrasings of the same concept once the page is already teachable with the current bundle.\n"
    )


def _validate_experience_session_v2_artifact_draft_payload(
    raw_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    payload = _jsonable_dict(raw_payload)
    missing_fields = [
        key
        for key in sorted(_EXPERIENCE_SESSION_V2_ARTIFACT_DRAFT_REQUIRED_FIELDS)
        if key not in payload
    ]
    if missing_fields:
        raise ValueError(
            "artifact draft generation failed: missing required draft fields: "
            + ", ".join(missing_fields)
        )
    draft = ExperienceSessionV2ArtifactDraft.model_validate(payload)
    return draft.model_dump(mode="json")


def _build_experience_v2_mandatory_resource_request_prompt_payload(
    *,
    paper: Paper,
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
) -> Dict[str, Any]:
    dossier = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload)).model_dump(mode="json")
    narrative_brief = _find_latest_experience_session_v2_narrative_brief(session)
    if not narrative_brief:
        raise ValueError("artifact draft generation failed: mandatory resource planning missing narrative brief")
    bundle = _jsonable_dict(resource_bundle or {})
    compact_source_context = _build_page_artifact_v2_compact_source_context(
        reading_dossier=dossier,
        focus_page=int(dossier.get("focus_page") or session.get("focus_page") or 1),
    )
    return {
        "task": "Plan 1-2 mandatory public-web resource_requests for experience_session_v2 before final artifact drafting.",
        "paper": {
            "title": str(getattr(paper, "title", "") or "").strip(),
            "url": str(getattr(paper, "url", "") or "").strip(),
            "doi": str(getattr(paper, "doi", "") or "").strip(),
            "arxiv_url": str(getattr(paper, "arxiv_url", "") or "").strip(),
        },
        "rules": {
            "strict_json_only": True,
            "return_shape": {"resource_requests": "[1..2 items]"},
            "minimum_resource_requests": 1,
            "maximum_resource_requests": 2,
            "must_include_public_web_tool": True,
            "allowed_tools": ["web_search", "web_scrape"],
            "prefer_web_search_when_exact_url_unknown": True,
            "avoid_seed_duplicates": True,
            "target_reader_value": "term explainer, encyclopedia background, official tutorial/docs, or video explainer page",
        },
        "narrative_brief": {
            "current_page_main_arc": _jsonable_dict(narrative_brief).get("current_page_main_arc"),
            "opening_key_points": list(_jsonable_dict(narrative_brief).get("opening_key_points") or [])[:4],
            "reader_attention_order": list(_jsonable_dict(narrative_brief).get("reader_attention_order") or [])[:6],
            "required_media_refs": list(_jsonable_dict(narrative_brief).get("required_media_refs") or [])[:4],
        },
        "resource_bundle": {
            "existing_external_resources": [
                {
                    "resource_id": str(_jsonable_dict(item).get("resource_id") or "").strip(),
                    "label": str(_jsonable_dict(item).get("label") or "").strip(),
                    "url": str(_jsonable_dict(item).get("url") or "").strip(),
                    "resource_type": str(_jsonable_dict(item).get("resource_type") or "").strip(),
                    "source_tool": str(_jsonable_dict(item).get("source_tool") or "").strip(),
                }
                for item in list(bundle.get("bundle_entries") or [])
                if isinstance(item, Mapping)
            ][:16],
            "affordance": _jsonable_dict(_jsonable_dict(bundle.get("meta") or {}).get("resource_request_affordance") or {}),
        },
        "compact_source_context": {
            "excerpt_candidates": list(compact_source_context.get("excerpt_candidates") or [])[:8],
            "media_candidates": _jsonable_dict(compact_source_context.get("media_candidates") or {}),
        },
    }


def _experience_v2_mandatory_resource_request_system_prompt() -> str:
    return (
        "You are planning mandatory public-web resource_requests for experience_session_v2.\n"
        "Return strict JSON only with shape {\"resource_requests\":[...] }.\n"
        "Hard rules:\n"
        "1) Emit 1 or 2 resource_requests.\n"
        "2) At least one request must use web_search or web_scrape.\n"
        "3) Prefer public explainer resources that help a reader understand the current page, not generic paper metadata.\n"
        "4) Avoid duplicating seeded paper/doi/pdf/arxiv links.\n"
        "5) Prefer web_search when the exact URL is not already known.\n"
        "6) Queries and reasons should be concise but specific to the current page's concepts, figures, equations, or terminology.\n"
        "7) Output only the JSON object, no commentary.\n"
    )


def _validate_experience_v2_mandatory_resource_request_payload(
    raw_payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    payload = _jsonable_dict(raw_payload)
    requests: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw_request in list(payload.get("resource_requests") or []):
        if not isinstance(raw_request, Mapping):
            continue
        request = ExperienceSessionV2ArtifactDraftResourceRequest.model_validate(_jsonable_dict(raw_request)).model_dump(mode="json")
        key = "|".join(
            [
                str(request.get("tool_name") or "").strip(),
                str(request.get("query") or "").strip(),
                str(request.get("url") or "").strip(),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        requests.append(request)
        if len(requests) >= 2:
            break
    if not requests:
        raise ValueError("artifact draft generation failed: mandatory web resource planning produced no resource_requests")
    if not any(str(item.get("tool_name") or "").strip() in {"web_search", "web_scrape"} for item in requests):
        raise ValueError("artifact draft generation failed: mandatory web resource planning requires public-web requests")
    return requests


async def _generate_experience_v2_mandatory_resource_requests(
    *,
    paper: Paper,
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    prompt_payload = _build_experience_v2_mandatory_resource_request_prompt_payload(
        paper=paper,
        reading_dossier=reading_dossier,
        session_payload=session_payload,
        resource_bundle=resource_bundle,
    )
    config = _experience_session_v2_artifact_agent_config()
    parsed = await _call_experience_session_v2_artifact_draft_model(
        system_prompt=_experience_v2_mandatory_resource_request_system_prompt(),
        user_prompt_payload=prompt_payload,
        provider=str(config.get("provider") or "").strip(),
        api_key=str(config.get("api_key") or "").strip(),
        base_url=str(config.get("base_url") or "").strip(),
        model=str(config.get("model") or "").strip(),
        timeout_seconds=float(config.get("timeout_seconds") or 0.0),
        max_tokens=min(2400, max(512, int(config.get("max_tokens") or 0))),
    )
    return _validate_experience_v2_mandatory_resource_request_payload(parsed)


async def _call_experience_session_v2_artifact_draft_model(
    *,
    system_prompt: str,
    user_prompt_payload: Mapping[str, Any],
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
) -> Dict[str, Any]:
    del provider
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": str(system_prompt or "")},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(_jsonable_dict(user_prompt_payload), ensure_ascii=False),
                        }
                    ],
                },
            ],
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout_seconds,
        ),
        timeout=timeout_seconds + 1.0,
    )
    try:
        content = str((response.choices[0].message.content or "")).strip()
    except Exception as exc:
        raise ValueError("artifact draft generation failed: model returned empty content") from exc
    parsed = await parse_json_dict_from_model_text(content)
    if not parsed:
        raise ValueError("artifact draft generation failed: invalid JSON output")
    return parsed


async def _generate_experience_session_v2_artifact_draft(
    *,
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
    previous_draft: Optional[Mapping[str, Any]] = None,
    include_full_dossier: bool,
) -> Dict[str, Any]:
    prompt_payload = _build_experience_session_v2_artifact_draft_prompt_payload(
        reading_dossier=reading_dossier,
        session_payload=session_payload,
        resource_bundle=resource_bundle,
        previous_draft=previous_draft,
        include_full_dossier=include_full_dossier,
    )
    config = _experience_session_v2_artifact_agent_config()
    parsed = await _call_experience_session_v2_artifact_draft_model(
        system_prompt=_experience_session_v2_artifact_draft_system_prompt(),
        user_prompt_payload=prompt_payload,
        provider=str(config.get("provider") or "").strip(),
        api_key=str(config.get("api_key") or "").strip(),
        base_url=str(config.get("base_url") or "").strip(),
        model=str(config.get("model") or "").strip(),
        timeout_seconds=float(config.get("timeout_seconds") or 0.0),
        max_tokens=int(config.get("max_tokens") or 0),
    )
    draft_payload = _validate_experience_session_v2_artifact_draft_payload(parsed)
    draft_meta = _jsonable_dict(draft_payload.get("meta") or {})
    draft_meta.update(
        {
            "generator_mode": "model_generated_artifact_draft",
            "build_mode": "phase3_model_artifact_draft",
            "model_provider": str(config.get("provider") or "").strip(),
            "model_name": str(config.get("model") or "").strip(),
            "prompt_contract": "experience_session_v2_artifact_draft_json_v2",
            "full_context": bool(include_full_dossier),
        }
    )
    draft_payload["meta"] = draft_meta
    return ExperienceSessionV2ArtifactDraft.model_validate(draft_payload).model_dump(mode="json")


def _build_reader_v2_resource_entry_id(*parts: Any) -> str:
    canonical = "|".join(str(part or "").strip() for part in parts if str(part or "").strip())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"res:{digest}"


def _normalize_reader_v2_resource_bundle_entries_from_tool_result(
    *,
    request: Mapping[str, Any],
    tool_result: ToolResult,
) -> List[Dict[str, Any]]:
    request_payload = _jsonable_dict(request)
    data = tool_result.data if isinstance(tool_result.data, dict) else {}
    tool_name = str(request_payload.get("tool_name") or "").strip()
    normalized: List[Dict[str, Any]] = []
    results = list(_jsonable_dict(data).get("results") or [])
    if results:
        for position, row in enumerate(results, start=1):
            payload = _jsonable_dict(row)
            url = str(payload.get("url") or payload.get("href") or payload.get("link") or "").strip()
            label = str(
                payload.get("title")
                or payload.get("label")
                or payload.get("document_name")
                or payload.get("document")
                or url
                or f"{tool_name}-{position}"
            ).strip()
            summary = str(payload.get("snippet") or payload.get("summary") or payload.get("content") or "").strip()
            normalized.append(
                {
                    "resource_id": _build_reader_v2_resource_entry_id(tool_name, request_payload.get("request_id"), position, url, label),
                    "label": label,
                    "url": url,
                    "resource_type": tool_name,
                    "summary": _clean_reader_facing_excerpt_text(summary or label, max_chars=320),
                    "source_tool": tool_name,
                    "renderable": bool(url),
                    "meta": {
                        "request_id": str(request_payload.get("request_id") or "").strip(),
                        "query": str(request_payload.get("query") or "").strip(),
                    },
                }
            )
        return normalized

    fallback_url = str(_jsonable_dict(data).get("url") or request_payload.get("url") or "").strip()
    fallback_output = _clean_reader_facing_excerpt_text(str(tool_result.output or "").strip(), max_chars=320)
    if tool_name == "web_scrape" and fallback_url:
        return [
            {
                "resource_id": _build_reader_v2_resource_entry_id(tool_name, request_payload.get("request_id"), fallback_url),
                "label": fallback_url,
                "url": fallback_url,
                "resource_type": tool_name,
                "summary": fallback_output or fallback_url,
                "source_tool": tool_name,
                "renderable": True,
                "meta": {"request_id": str(request_payload.get("request_id") or "").strip()},
            }
        ]

    if fallback_output:
        return [
            {
                "resource_id": _build_reader_v2_resource_entry_id(tool_name, request_payload.get("request_id"), fallback_output[:80]),
                "label": str(request_payload.get("query") or request_payload.get("reason") or tool_name).strip() or tool_name,
                "url": fallback_url,
                "resource_type": tool_name,
                "summary": fallback_output,
                "source_tool": tool_name,
                "renderable": bool(fallback_url),
                "meta": {"request_id": str(request_payload.get("request_id") or "").strip()},
            }
        ]
    return []


def _merge_reader_v2_resource_bundle_entries(
    *,
    resource_bundle: Mapping[str, Any],
    new_entries: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    bundle = _jsonable_dict(resource_bundle or {})
    existing_entries = [
        _jsonable_dict(item)
        for item in list(bundle.get("bundle_entries") or [])
        if isinstance(item, Mapping)
    ]
    seen_keys: Set[str] = set()
    merged: List[Dict[str, Any]] = []
    for item in existing_entries + [_jsonable_dict(entry) for entry in list(new_entries or []) if isinstance(entry, Mapping)]:
        url = str(item.get("url") or "").strip()
        resource_id = str(item.get("resource_id") or "").strip()
        dedupe_key = url or resource_id
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        merged.append(item)
    bundle["bundle_entries"] = merged
    bundle["external_resources"] = [
        {
            "resource_id": str(item.get("resource_id") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "resource_type": str(item.get("resource_type") or "").strip(),
        }
        for item in merged
        if bool(item.get("renderable")) and str(item.get("url") or "").strip()
    ]
    bundle_meta = _jsonable_dict(bundle.get("meta") or {})
    bundle_meta["retrieval_rounds"] = int(bundle_meta.get("retrieval_rounds") or 0)
    bundle["meta"] = bundle_meta
    return bundle


async def _execute_experience_v2_artifact_resource_requests(
    *,
    db: AsyncSession,
    current_user: User,
    paper: Paper,
    selected_kb_id: int,
    requests: Sequence[Mapping[str, Any]],
    resource_bundle: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not list(requests or []):
        return _jsonable_dict(resource_bundle), []

    nonfatal_empty_retrieval_errors = {"no_results", "no_completed_documents"}
    registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
        db=db,
        current_user=current_user,
        paper=paper,
        selected_kb_id=selected_kb_id,
    )
    if registry is None:
        raise ValueError("artifact draft generation blocked by missing retrieval result: tool registry unavailable")
    bundle = _jsonable_dict(resource_bundle)
    bundle_meta = _jsonable_dict(bundle.get("meta") or {})
    nonfatal_feedback = list(bundle_meta.get("nonfatal_request_feedback") or [])
    tool_trace: List[Dict[str, Any]] = []
    accumulated_entries: List[Dict[str, Any]] = []
    for round_index, raw_request in enumerate(list(requests or []), start=1):
        request = ExperienceSessionV2ArtifactDraftResourceRequest.model_validate(_jsonable_dict(raw_request)).model_dump(mode="json")
        tool_name = str(request.get("tool_name") or "").strip()
        if tool_name not in allowed_tool_names:
            raise ValueError(f"artifact draft generation blocked by missing retrieval result: unsupported tool {tool_name}")
        args: Dict[str, Any] = {}
        if tool_name == "web_scrape":
            args["url"] = str(request.get("url") or "").strip()
            args["formats"] = ["markdown"]
            args["only_main_content"] = True
        else:
            args["query"] = str(request.get("query") or "").strip()
            args["top_k"] = int(request.get("max_results") or 3)
        started_at = time.perf_counter()
        result = await registry.execute(tool_name, **args)
        latency_ms = int(round((time.perf_counter() - started_at) * 1000))
        if not bool(result.success):
            normalized_error = str(result.error or "").strip().lower()
            if tool_name == "knowledge_search" and normalized_error in nonfatal_empty_retrieval_errors:
                nonfatal_feedback.append(
                    {
                        "tool_name": tool_name,
                        "request_id": str(request.get("request_id") or "").strip(),
                        "query": str(request.get("query") or "").strip(),
                        "status": normalized_error or "empty_result",
                        "message": str(result.output or "knowledge_search returned no usable results").strip(),
                    }
                )
                tool_trace.append(
                    {
                        "round_index": int(round_index),
                        "tool_name": tool_name,
                        "success": False,
                        "latency_ms": latency_ms,
                        "note": str(request.get("reason") or "").strip(),
                        "meta": {
                            "tool_identity": tool_name,
                            "tool_arguments": args,
                            "request_id": str(request.get("request_id") or "").strip(),
                            "normalized_entry_count": 0,
                            "nonfatal_empty_result": True,
                            "error": normalized_error or "empty_result",
                        },
                    }
                )
                continue
            raise ValueError(
                "artifact draft generation blocked by missing retrieval result: "
                f"{tool_name} {str(result.error or 'failed').strip()}"
            )
        normalized_entries = _normalize_reader_v2_resource_bundle_entries_from_tool_result(
            request=request,
            tool_result=result,
        )
        if not normalized_entries:
            if tool_name == "knowledge_search":
                nonfatal_feedback.append(
                    {
                        "tool_name": tool_name,
                        "request_id": str(request.get("request_id") or "").strip(),
                        "query": str(request.get("query") or "").strip(),
                        "status": "empty_result",
                        "message": "knowledge_search returned no normalized bundle entries",
                    }
                )
                tool_trace.append(
                    {
                        "round_index": int(round_index),
                        "tool_name": tool_name,
                        "success": True,
                        "latency_ms": latency_ms,
                        "note": str(request.get("reason") or "").strip(),
                        "meta": {
                            "tool_identity": tool_name,
                            "tool_arguments": args,
                            "request_id": str(request.get("request_id") or "").strip(),
                            "normalized_entry_count": 0,
                            "nonfatal_empty_result": True,
                            "error": "empty_result",
                        },
                    }
                )
                continue
            raise ValueError(
                "artifact draft generation blocked by missing retrieval result: "
                f"{tool_name} returned no normalized bundle entries"
            )
        accumulated_entries.extend(normalized_entries)
        tool_trace.append(
            {
                "round_index": int(round_index),
                "tool_name": tool_name,
                "success": True,
                "latency_ms": latency_ms,
                "note": str(request.get("reason") or "").strip(),
                "meta": {
                    "tool_identity": tool_name,
                    "tool_arguments": args,
                    "request_id": str(request.get("request_id") or "").strip(),
                    "normalized_entry_count": len(normalized_entries),
                },
            }
        )
    merged_bundle = _merge_reader_v2_resource_bundle_entries(
        resource_bundle=bundle,
        new_entries=accumulated_entries,
    )
    merged_meta = _jsonable_dict(merged_bundle.get("meta") or {})
    if nonfatal_feedback:
        merged_meta["nonfatal_request_feedback"] = nonfatal_feedback[-8:]
    merged_meta["retrieval_rounds"] = int(merged_meta.get("retrieval_rounds") or 0) + 1
    merged_bundle["meta"] = merged_meta
    return merged_bundle, tool_trace


def _summarize_experience_v2_artifact_draft(
    artifact_draft: Mapping[str, Any],
) -> Dict[str, Any]:
    draft = ExperienceSessionV2ArtifactDraft.model_validate(_jsonable_dict(artifact_draft)).model_dump(mode="json")
    nodes = [
        _jsonable_dict(item)
        for item in list(draft.get("nodes") or [])
        if isinstance(item, Mapping)
    ]
    return {
        "template_id": str(draft.get("template_hint") or "").strip(),
        "layout_recipe": str(draft.get("layout_recipe") or "").strip(),
        "presentation_mode": str(draft.get("presentation_mode") or "").strip(),
        "requested_node_kinds": sorted(
            {str(item.get("node_kind") or "").strip() for item in nodes if str(item.get("node_kind") or "").strip()}
        ),
        "resource_request_count": len(list(draft.get("resource_requests") or [])),
        "node_count": len(nodes),
    }


def _promote_experience_v2_artifact_draft_to_authored_plan(
    *,
    artifact_draft: Mapping[str, Any],
    resource_bundle: Mapping[str, Any],
) -> Dict[str, Any]:
    draft = ExperienceSessionV2ArtifactDraft.model_validate(_jsonable_dict(artifact_draft)).model_dump(mode="json")
    bundle = _jsonable_dict(resource_bundle or {})
    bundle_entries = {
        str(_jsonable_dict(item).get("resource_id") or "").strip(): _jsonable_dict(item)
        for item in list(bundle.get("bundle_entries") or [])
        if isinstance(item, Mapping) and str(_jsonable_dict(item).get("resource_id") or "").strip()
    }
    authored_explanations: List[str] = []
    authored_text_blocks: List[Dict[str, Any]] = []
    excerpt_overrides: List[Dict[str, Any]] = []
    figure_slots: List[Dict[str, Any]] = []
    table_slots: List[Dict[str, Any]] = []
    equation_slots: List[Dict[str, Any]] = []
    aside_blocks: List[Dict[str, Any]] = []
    term_annotations: List[Dict[str, Any]] = []
    external_resources: List[Dict[str, Any]] = []
    requested_node_kinds: Set[str] = set()
    draft_node_sequence: List[Dict[str, Any]] = []

    for raw_node in list(draft.get("nodes") or []):
        node = ExperienceSessionV2ArtifactDraftNode.model_validate(_jsonable_dict(raw_node)).model_dump(mode="json")
        node_kind = str(node.get("node_kind") or "").strip()
        draft_node_sequence.append(
            {
                "node_kind": node_kind,
                "text": str(node.get("text") or "").strip(),
                "display_text": str(node.get("display_text") or "").strip(),
                "translation_zh": str(node.get("translation_zh") or "").strip(),
                "label": str(node.get("label") or "").strip(),
                "caption": str(node.get("caption") or "").strip(),
                "term": str(node.get("term") or "").strip(),
                "definition": str(node.get("definition") or "").strip(),
                "source_layout_ids": list(node.get("source_layout_ids") or []),
                "source_block_ids": list(node.get("source_block_ids") or []),
                "resource_ref_ids": list(node.get("resource_ref_ids") or []),
                "meta": _jsonable_dict(node.get("meta") or {}),
            }
        )
        if node_kind == "heading":
            text = str(node.get("text") or "").strip()
            authored_explanations.append(text)
            authored_text_blocks.append(
                {
                    "segment_kind": "heading",
                    "text": text,
                    "meta": {"from_draft_node_kind": "heading", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "paragraph":
            text = str(node.get("text") or "").strip()
            authored_explanations.append(text)
            authored_text_blocks.append(
                {
                    "segment_kind": "paragraph",
                    "text": text,
                    "meta": {"from_draft_node_kind": "paragraph", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "original_excerpt":
            excerpt_meta = {"from_draft_node_kind": "original_excerpt", **_jsonable_dict(node.get("meta") or {})}
            translation_zh = str(node.get("translation_zh") or "").strip()
            if translation_zh:
                excerpt_meta["translation_zh"] = translation_zh
            excerpt_overrides.append(
                {
                    "display_text": str(node.get("display_text") or "").strip(),
                    "source_layout_ids": list(node.get("source_layout_ids") or []),
                    "source_block_ids": list(node.get("source_block_ids") or []),
                    "meta": excerpt_meta,
                }
            )
            continue
        if node_kind == "figure_slot":
            requested_node_kinds.add("figure_slot")
            figure_slots.append(
                {
                    "label": str(node.get("label") or "Figure").strip(),
                    "caption": str(node.get("caption") or node.get("text") or "").strip(),
                    "source_layout_id": str((list(node.get("source_layout_ids") or []) or [""])[0]).strip(),
                    "meta": {"from_draft_node_kind": "figure_slot", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "table_slot":
            requested_node_kinds.add("table_slot")
            table_slots.append(
                {
                    "label": str(node.get("label") or "Table").strip(),
                    "caption": str(node.get("caption") or node.get("text") or "").strip(),
                    "source_layout_id": str((list(node.get("source_layout_ids") or []) or [""])[0]).strip(),
                    "meta": {"from_draft_node_kind": "table_slot", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "equation_slot":
            requested_node_kinds.add("equation_slot")
            equation_slots.append(
                {
                    "label": str(node.get("label") or "Equation").strip(),
                    "caption": str(node.get("caption") or node.get("text") or "").strip(),
                    "source_layout_id": str((list(node.get("source_layout_ids") or []) or [""])[0]).strip(),
                    "meta": {"from_draft_node_kind": "equation_slot", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "aside":
            requested_node_kinds.add("aside_content")
            aside_blocks.append(
                {
                    "label": str(node.get("label") or "Aside").strip(),
                    "text": str(node.get("text") or "").strip(),
                    "meta": {"from_draft_node_kind": "aside", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "term_note":
            requested_node_kinds.add("term_annotation")
            term_annotations.append(
                {
                    "term": str(node.get("term") or node.get("label") or "术语").strip(),
                    "definition": str(node.get("definition") or node.get("text") or "").strip(),
                    "meta": {"from_draft_node_kind": "term_note", **_jsonable_dict(node.get("meta") or {})},
                }
            )
            continue
        if node_kind == "external_resource":
            requested_node_kinds.add("external_resource")
            ref_ids = [str(item).strip() for item in list(node.get("resource_ref_ids") or []) if str(item).strip()]
            if not ref_ids:
                raise ValueError("requested artifact node kind not supported yet: external_resource missing resource_ref_ids")
            resolved_resources: List[Dict[str, Any]] = []
            for ref_id in ref_ids:
                entry = _jsonable_dict(bundle_entries.get(ref_id) or {})
                if not entry:
                    raise ValueError(f"artifact draft generation blocked by missing retrieval result: resource_ref {ref_id}")
                url = str(entry.get("url") or "").strip()
                if not url:
                    raise ValueError(f"artifact draft generation blocked by missing retrieval result: resource_ref {ref_id} has no url")
                resolved_resources.append(
                    {
                        "resource_id": ref_id,
                        "label": str(entry.get("label") or url).strip(),
                        "url": url,
                        "resource_type": str(entry.get("resource_type") or "").strip() or "external",
                    }
                )
                external_resources.append(
                    {
                        "label": str(node.get("label") or entry.get("label") or url).strip(),
                        "url": url,
                        "resource_type": str(entry.get("resource_type") or "").strip() or "external",
                        "meta": {"from_draft_node_kind": "external_resource", **_jsonable_dict(node.get("meta") or {})},
                    }
                )
            draft_node_sequence[-1]["resolved_resources"] = resolved_resources
            continue
        raise ValueError(f"requested artifact node kind not supported yet: {node_kind}")

    if not authored_text_blocks and not authored_explanations:
        raise ValueError("artifact draft generation failed: draft produced no authored narrative paragraphs")

    return PageArtifactV2AuthoredPlanInput.model_validate(
        {
            "template_id": str(draft.get("template_hint") or "").strip(),
            "layout_recipe": str(draft.get("layout_recipe") or "").strip(),
            "presentation_mode": str(draft.get("presentation_mode") or "").strip(),
            "widget_family": str(draft.get("widget_family") or "").strip() or "reader_v2_surface",
            "motion_preset": str(draft.get("motion_preset") or "").strip() or "calm_progressive",
            "interaction_policy": str(draft.get("interaction_policy") or "").strip() or "reader_first_guided",
            "authored_explanations": authored_explanations,
            "authored_text_blocks": authored_text_blocks,
            "excerpt_overrides": excerpt_overrides,
            "figure_slots": figure_slots,
            "table_slots": table_slots,
            "equation_slots": equation_slots,
            "aside_blocks": aside_blocks,
            "term_annotations": term_annotations,
            "external_resources": external_resources,
            "requested_node_kinds": sorted(
                {"original_excerpt", "term_annotation", *requested_node_kinds}
                | {item.get("segment_kind") for item in authored_text_blocks if str(item.get("segment_kind") or "").strip()}
            ),
            "meta": {
                "from": "experience_v2_artifact_draft_promotion",
                "presentation_rationale": str(draft.get("presentation_mode") or "").strip(),
                "draft_node_sequence": draft_node_sequence,
            },
        }
    ).model_dump(mode="json")


def _find_latest_experience_session_v2_narrative_brief(
    session_payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    payload = _jsonable_dict(session_payload)
    session_meta = _jsonable_dict(payload.get("meta") or {})
    latest = _jsonable_dict(session_meta.get("latest_narrative_brief") or {})
    if latest:
        return latest
    for iteration in reversed(list(payload.get("iterations") or [])):
        brief = _jsonable_dict((_jsonable_dict(iteration)).get("narrative_brief") or {})
        if brief:
            return brief
    return None


def _find_latest_experience_session_v2_artifact_draft(
    session_payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    payload = _jsonable_dict(session_payload)
    session_meta = _jsonable_dict(payload.get("meta") or {})
    latest = _jsonable_dict(session_meta.get("latest_artifact_draft") or {})
    if latest:
        return latest
    for iteration in reversed(list(payload.get("iterations") or [])):
        meta = _jsonable_dict((_jsonable_dict(iteration)).get("meta") or {})
        draft = _jsonable_dict(meta.get("artifact_draft") or {})
        if draft:
            return draft
    return None


def _build_experience_session_v2(
    *,
    cache_key: str,
    reading_dossier: Mapping[str, Any],
    focus_page: int,
    reader_profile: str,
    max_iterations: int,
    max_tool_rounds: int,
    narrative_brief: Mapping[str, Any],
    session_id: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    dossier_payload = ReadingDossierV2.model_validate(_jsonable_dict(reading_dossier)).model_dump(mode="json")
    signature = _reading_dossier_v2_signature(dossier_payload)
    validated_narrative_brief = ExperienceSessionV2NarrativeBrief.model_validate(
        _jsonable_dict(narrative_brief or {})
    ).model_dump(mode="json")
    session_meta = _jsonable_dict(meta or {})
    session_meta["latest_narrative_brief"] = validated_narrative_brief
    session_meta["experience_v2_runtime_version"] = _EXPERIENCE_V2_RUNTIME_VERSION
    session_payload = {
        "session_id": str(session_id or uuid.uuid4().hex),
        "cache_namespace": EXPERIENCE_SESSION_V2_CACHE_NAMESPACE,
        "plan_kind": EXPERIENCE_SESSION_V2_CACHE_KIND,
        "cache_key": str(cache_key or "").strip(),
        "focus_page": int(focus_page),
        "reader_profile": str(reader_profile or "").strip() or "curious_generalist",
        "dossier_signature": signature,
        "runtime_budget": {
            "max_iterations": int(max_iterations),
            "max_tool_rounds": int(max_tool_rounds),
        },
        "iterations": [
            {
                "iteration_index": 1,
                "phase": "bootstrap",
                "context_carry": {
                    "mode": "full_dossier_bootstrap",
                    "full_dossier": dossier_payload,
                    "delta_packet": {},
                    "state_handle": "iter:1:bootstrap",
                },
                "narrative_brief": validated_narrative_brief,
                "tool_trace": [],
                "stop_reason": "",
                "meta": {
                    "bootstrap_full_dossier": True,
                    "narrative_strategy_ready": True,
                },
            }
        ],
        "resume": {
            "preferred_strategy": "resume",
            "resumable": True,
            "resume_state_handle": "iter:1:bootstrap",
            "resume_token": "",
            "last_failed_iteration": 0,
            "failure_count": 0,
            "meta": {},
        },
        "artifact_promotion": {
            "promotion_ready": False,
            "completed_artifact_exists": False,
            "no_second_full_generation_pass": True,
            "artifact_ref": "",
            "promoted_fields": {"narrative_brief": validated_narrative_brief},
            "meta": {"reserved_for_phase3": True},
        },
        "meta": session_meta,
    }
    return ExperienceSessionV2.model_validate(session_payload).model_dump(mode="json")


def _append_experience_session_v2_iteration(
    session_payload: Mapping[str, Any],
    *,
    delta_packet: Optional[Mapping[str, Any]],
    state_handle: str,
    phase: str = "revise",
    tool_trace: Optional[Sequence[Mapping[str, Any]]] = None,
    stop_reason: str = "",
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    raw_session_payload = _jsonable_dict(session_payload)
    if not _find_latest_experience_session_v2_narrative_brief(raw_session_payload):
        raise ValueError("narrative brief layer missing in session execution")
    session = ExperienceSessionV2.model_validate(raw_session_payload)
    if bool(session.artifact_promotion.completed_artifact_exists):
        raise ValueError("completed artifact already exists; second full-generation pass is blocked")
    session_data = session.model_dump(mode="json")
    latest_narrative_brief = _find_latest_experience_session_v2_narrative_brief(session_data)
    if not latest_narrative_brief:
        raise ValueError("narrative brief layer missing in session execution")
    runtime_budget = _jsonable_dict(session_data.get("runtime_budget") or {})
    iterations = list(session_data.get("iterations") or [])
    max_iterations = int(runtime_budget.get("max_iterations") or 0)
    if max_iterations > 0 and len(iterations) >= max_iterations:
        raise ValueError("experience_session_v2 max_iterations exceeded")

    def _extract_tool_identity(item: Mapping[str, Any]) -> str:
        meta = _jsonable_dict(item.get("meta") or {})
        return str(
            item.get("tool_identity")
            or item.get("tool_name")
            or item.get("tool")
            or item.get("name")
            or item.get("id")
            or meta.get("tool_identity")
            or meta.get("tool_name")
            or ""
        ).strip()

    def _extract_tool_arguments(item: Mapping[str, Any]) -> Any:
        if "tool_args" in item:
            return item.get("tool_args")
        if "arguments" in item:
            return item.get("arguments")
        if "args" in item:
            return item.get("args")
        if "input" in item:
            return item.get("input")
        if "params" in item:
            return item.get("params")
        meta = _jsonable_dict(item.get("meta") or {})
        if "tool_arguments" in meta:
            return meta.get("tool_arguments")
        if "arguments" in meta:
            return meta.get("arguments")
        return {}

    incoming_tool_trace: List[Dict[str, Any]] = []
    for raw_item in list(tool_trace or []):
        item = _jsonable_dict(raw_item)
        tool_identity = _extract_tool_identity(item)
        args_payload = _jsonable_dict(_extract_tool_arguments(item))
        meta_payload = _jsonable_dict(item.get("meta") or {})
        if tool_identity and not str(meta_payload.get("tool_identity") or "").strip():
            meta_payload["tool_identity"] = tool_identity
        if args_payload and "tool_arguments" not in meta_payload:
            meta_payload["tool_arguments"] = args_payload
        item["meta"] = meta_payload
        incoming_tool_trace.append(item)
    max_tool_rounds = int(runtime_budget.get("max_tool_rounds") or 0)
    used_tool_rounds = sum(len(list((_jsonable_dict(item).get("tool_trace") or []))) for item in iterations)
    if max_tool_rounds > 0 and (used_tool_rounds + len(incoming_tool_trace)) > max_tool_rounds:
        raise ValueError("experience_session_v2 max_tool_rounds exceeded")

    dossier_signature = str(session_data.get("dossier_signature") or "").strip()

    def _tool_call_signature(raw_item: Mapping[str, Any]) -> Optional[str]:
        item = _jsonable_dict(raw_item)
        tool_identity = _extract_tool_identity(item)
        if not tool_identity:
            return None
        canonical = {
            "dossier_signature": dossier_signature,
            "tool_identity": tool_identity,
            "arguments": _jsonable_dict(_extract_tool_arguments(item)),
        }
        return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    seen_signatures: Set[str] = set()
    for iteration in iterations:
        for existing_call in list((_jsonable_dict(iteration).get("tool_trace") or [])):
            signature = _tool_call_signature(existing_call)
            if signature:
                seen_signatures.add(signature)
    for call in incoming_tool_trace:
        signature = _tool_call_signature(call)
        if signature and signature in seen_signatures:
            raise ValueError("experience_session_v2 duplicate tool call blocked")
        if signature:
            seen_signatures.add(signature)

    delta_payload = _jsonable_dict(delta_packet or {})
    forbidden_replay_keys = {
        "full_dossier",
        "reading_dossier",
        "adjacent_pages",
        "adjacent_page_context",
        "neighboring_page_context",
    }
    for key in forbidden_replay_keys:
        if key in delta_payload:
            raise ValueError(f"experience_session_v2 revise turn cannot replay full neighboring-page structured payload: {key}")
    compact_brief = _compact_narrative_brief_payload(latest_narrative_brief)
    compact_brief["focus_page"] = int(compact_brief.get("focus_page") or session.focus_page)
    delta_payload.setdefault("narrative_brief", compact_brief)

    next_index = len(session_data.get("iterations") or []) + 1
    session_data.setdefault("iterations", []).append(
        {
            "iteration_index": int(next_index),
            "phase": str(phase or "revise").strip() or "revise",
            "context_carry": {
                "mode": "delta_state_handle",
                "delta_packet": delta_payload,
                "state_handle": str(state_handle or "").strip(),
            },
            "narrative_brief": latest_narrative_brief,
            "tool_trace": incoming_tool_trace,
            "stop_reason": str(stop_reason or "").strip(),
            "meta": {
                **_jsonable_dict(meta or {}),
                "narrative_strategy_ready": True,
            },
        }
    )
    session_data.setdefault("resume", {})["preferred_strategy"] = "resume"
    session_data["resume"]["resumable"] = True
    session_data["resume"]["resume_state_handle"] = str(state_handle or "").strip()
    session_data["status"] = "running"
    session_data["stop_reason"] = ""
    session_data.setdefault("meta", {})["latest_narrative_brief"] = latest_narrative_brief
    return ExperienceSessionV2.model_validate(session_data).model_dump(mode="json")


def _mark_experience_session_v2_failed(
    session_payload: Mapping[str, Any],
    *,
    stop_reason: str,
    resume_state_handle: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload))
    resolved_reason = str(stop_reason or "").strip() or "experience_session_failed"
    session.status = "failed"
    session.stop_reason = resolved_reason
    session.resume.preferred_strategy = "resume"
    session.resume.resumable = True
    session.resume.failure_count = int(session.resume.failure_count) + 1
    session.resume.last_failed_iteration = len(session.iterations)
    session.resume.resume_state_handle = str(
        resume_state_handle or session.resume.resume_state_handle or f"iter:{len(session.iterations)}:failed"
    ).strip()
    session.resume.resume_token = uuid.uuid4().hex
    failure_meta = _jsonable_dict(meta or {})
    merged_meta = _jsonable_dict(session.meta)
    if failure_meta:
        merged_meta.setdefault("failure_meta", {}).update(failure_meta)
        session.meta = merged_meta
    return session.model_dump(mode="json")


def _collect_reader_v2_external_resources(
    *,
    paper: Paper,
    compose_payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()

    def _is_internal_reader_asset(url: str, resource_type: str) -> bool:
        normalized_url = str(url or "").strip().lower()
        normalized_type = str(resource_type or "").strip().lower()
        if normalized_type in {"image_hint", "page_image", "figure_asset", "grounding_asset"}:
            return True
        return any(
            token in normalized_url
            for token in (
                "/api/v1/literature/reader/figure-assets/",
                "/api/v1/literature/reader/grounding-page-assets/",
            )
        )

    def _push_resource(label: str, url: str, resource_type: str) -> None:
        normalized_url = str(url or "").strip()
        normalized_label = str(label or "").strip()
        if not normalized_url or normalized_url in seen_urls or _is_internal_reader_asset(normalized_url, resource_type):
            return
        seen_urls.add(normalized_url)
        resources.append(
            {
                "label": normalized_label or normalized_url,
                "url": normalized_url,
                "resource_type": resource_type,
            }
        )

    _push_resource("Paper URL", getattr(paper, "url", ""), "paper")
    doi = str(getattr(paper, "doi", "") or "").strip()
    if doi:
        doi_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        _push_resource("DOI", doi_url, "doi")
    _push_resource("ArXiv", getattr(paper, "arxiv_url", ""), "arxiv")
    _push_resource("PDF", _resolve_pdf_download_url(paper), "pdf")

    for asset in list((_jsonable_dict(compose_payload).get("assets") or [])):
        item = _jsonable_dict(asset)
        candidate_url = str(item.get("url") or item.get("href") or "").strip()
        candidate_label = str(item.get("title") or item.get("label") or item.get("kind") or "Resource").strip()
        candidate_type = str(item.get("kind") or item.get("type") or "asset").strip()
        if candidate_url:
            _push_resource(candidate_label, candidate_url, candidate_type)

    return resources[:4]


def _build_page_artifact_v2_authored_plan_from_session(
    *,
    paper: Paper,
    compose_payload: Mapping[str, Any],
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
    artifact_draft: Optional[Mapping[str, Any]] = None,
    resource_bundle: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload)).model_dump(mode="json")
    del paper, compose_payload
    latest_narrative_brief = _find_latest_experience_session_v2_narrative_brief(session)
    if not latest_narrative_brief:
        raise ValueError("artifact draft generation failed: narrative brief layer missing in session execution")
    resolved_artifact_draft = (
        ExperienceSessionV2ArtifactDraft.model_validate(_jsonable_dict(artifact_draft)).model_dump(mode="json")
        if isinstance(artifact_draft, Mapping)
        else _find_latest_experience_session_v2_artifact_draft(session)
    )
    if not resolved_artifact_draft:
        raise ValueError("artifact draft generation failed: artifact_draft layer missing in session execution")
    resolved_resource_bundle = (
        _jsonable_dict(resource_bundle)
        if isinstance(resource_bundle, Mapping)
        else _jsonable_dict(_jsonable_dict(session.get("meta") or {}).get("latest_resource_bundle") or {})
    )
    if not resolved_resource_bundle:
        raise ValueError("artifact draft generation blocked by missing retrieval result: resource bundle unavailable")
    authored_plan = _promote_experience_v2_artifact_draft_to_authored_plan(
        artifact_draft=resolved_artifact_draft,
        resource_bundle=resolved_resource_bundle,
    )
    reader_frame = _build_reader_frame_from_narrative_brief(
        latest_narrative_brief,
        reading_dossier=reading_dossier,
    )
    authored_meta = _jsonable_dict(authored_plan.get("meta") or {})
    if _jsonable_dict(reader_frame.get("reader_opening") or {}):
        authored_meta["reader_opening"] = _jsonable_dict(reader_frame.get("reader_opening") or {})
    if _jsonable_dict(reader_frame.get("reader_outro") or {}):
        authored_meta["reader_outro"] = _jsonable_dict(reader_frame.get("reader_outro") or {})
    if authored_meta:
        authored_plan["meta"] = authored_meta
    return resolved_resource_bundle, authored_plan


def _complete_experience_session_v2(
    session_payload: Mapping[str, Any],
    *,
    artifact_ref: str,
    artifact_payload: Mapping[str, Any],
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload))
    artifact = PageArtifactV2.model_validate(_jsonable_dict(artifact_payload))
    session.status = "completed"
    session.stop_reason = "completed_page_artifact_v2_ready"
    session.artifact_promotion.promotion_ready = True
    session.artifact_promotion.completed_artifact_exists = True
    session.artifact_promotion.artifact_ref = str(artifact_ref or "").strip()
    promoted_fields = _jsonable_dict(session.artifact_promotion.promoted_fields or {})
    promoted_fields.update(
        {
            "artifact_contract_id": str(artifact.artifact_contract_id),
            "template_id": str(artifact.template_id),
            "layout_recipe": str(artifact.layout_recipe),
            "presentation_mode": str(artifact.presentation_mode),
            "widget_family": str(artifact.widget_family),
            "motion_preset": str(artifact.motion_preset),
            "interaction_policy": str(artifact.interaction_policy),
        }
    )
    session.artifact_promotion.promoted_fields = promoted_fields
    merged_meta = _jsonable_dict(session.meta)
    if meta:
        merged_meta.update(_jsonable_dict(meta))
    session.meta = merged_meta
    return session.model_dump(mode="json")


def _compose_signature_cache_candidates(compose_source_signature: str) -> List[str]:
    normalized = str(compose_source_signature or "").strip()
    if not normalized:
        return [""]
    return [normalized, ""]


def _generative_plan_cache_key_candidates(
    *,
    user_id: int,
    paper_id: int,
    page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    user_intent: str,
) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for signature_token in _compose_signature_cache_candidates(compose_source_signature):
        cache_key = _generative_plan_cache_key(
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            selected_kb_id=int(selected_kb_id),
            compose_source_signature=signature_token,
            user_intent=str(user_intent or "").strip(),
        )
        if cache_key in seen:
            continue
        seen.add(cache_key)
        candidates.append((cache_key, signature_token))
    return candidates


def _experience_plan_cache_key_candidates(
    *,
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    generative_plan_signature: str,
    user_intent: str,
    reader_profile: str,
    focus_section_ids: Sequence[str],
) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for signature_token in _compose_signature_cache_candidates(compose_source_signature):
        cache_key = _experience_plan_cache_key(
            user_id=int(user_id),
            paper_id=int(paper_id),
            focus_page=int(focus_page),
            selected_kb_id=int(selected_kb_id),
            compose_source_signature=signature_token,
            generative_plan_signature=str(generative_plan_signature or "").strip(),
            user_intent=str(user_intent or "").strip(),
            reader_profile=str(reader_profile or "").strip(),
            focus_section_ids=list(focus_section_ids or []),
        )
        if cache_key in seen:
            continue
        seen.add(cache_key)
        candidates.append((cache_key, signature_token))
    return candidates


async def _persist_completed_generative_plan_variants(
    *,
    payload: Dict[str, Any],
    user_id: int,
    paper_id: int,
    page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    user_intent: str,
) -> None:
    for cache_key, signature_token in _generative_plan_cache_key_candidates(
        user_id=int(user_id),
        paper_id=int(paper_id),
        page=int(page),
        selected_kb_id=int(selected_kb_id),
        compose_source_signature=compose_source_signature,
        user_intent=str(user_intent or "").strip(),
    ):
        await _generative_plan_cache_set(
            cache_key,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=signature_token,
        )


async def _persist_completed_experience_plan_variants(
    *,
    payload: Dict[str, Any],
    user_id: int,
    paper_id: int,
    focus_page: int,
    selected_kb_id: int,
    compose_source_signature: str,
    generative_plan_signature: str,
    user_intent: str,
    reader_profile: str,
    focus_section_ids: Sequence[str],
) -> None:
    for cache_key, signature_token in _experience_plan_cache_key_candidates(
        user_id=int(user_id),
        paper_id=int(paper_id),
        focus_page=int(focus_page),
        selected_kb_id=int(selected_kb_id),
        compose_source_signature=compose_source_signature,
        generative_plan_signature=str(generative_plan_signature or "").strip(),
        user_intent=str(user_intent or "").strip(),
        reader_profile=str(reader_profile or "").strip(),
        focus_section_ids=list(focus_section_ids or []),
    ):
        await _experience_plan_cache_set(
            cache_key,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(focus_page),
            compose_source_signature=signature_token,
        )


def _plan_signature(payload: Mapping[str, Any]) -> str:
    try:
        normalized = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _jsonable_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        try:
            cloned = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
            return cloned if isinstance(cloned, dict) else dict(payload)
        except Exception:
            return dict(payload)
    if isinstance(payload, Mapping):
        return {str(key): value for key, value in dict(payload).items()}
    return {}


def _generative_plan_has_guided_reading_structure(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    story_substrate = dict(payload.get("story_substrate") or {})
    page_brief = dict(payload.get("page_brief") or {})
    if str(story_substrate.get("page_id") or "").strip():
        return True
    if list(story_substrate.get("main_claims") or []) or list(story_substrate.get("evidence_units") or []):
        return True
    if str(page_brief.get("page_goal") or "").strip():
        return True
    if list(page_brief.get("reading_path") or []) or list(page_brief.get("storyboard") or []):
        return True
    return False


def _repair_sparse_cached_generative_plan(
    *,
    runtime: Any,
    page: int,
    user_intent: str,
    compose_payload: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
    repair_reason: str,
) -> Dict[str, Any]:
    current = _jsonable_dict(plan_payload)
    if not current or _generative_plan_has_guided_reading_structure(current):
        return current

    build_seed_plan = getattr(runtime, "build_seed_plan", None)
    if not callable(build_seed_plan):
        return current

    try:
        repaired_seed = build_seed_plan(
            page=int(page),
            user_intent=str(user_intent or "").strip(),
            compose_payload=compose_payload,
        )
    except Exception as exc:
        logger.warning(
            "[Literature API] sparse cached generative plan repair failed "
            f"page={page}: {exc}"
        )
        return current

    if not isinstance(repaired_seed, Mapping):
        return current

    repaired = _jsonable_dict(repaired_seed)
    for key in ("version", "status", "shell_mode"):
        if str(current.get(key) or "").strip():
            repaired[key] = current.get(key)
    for key in ("used_tools", "tool_trace"):
        if list(current.get(key) or []):
            repaired[key] = list(current.get(key) or [])
    for key in current.keys():
        if key not in repaired:
            repaired[key] = current.get(key)
    meta = dict(repaired.get("meta") or {})
    meta.update(dict(current.get("meta") or {}))
    meta["cached_plan_repaired"] = True
    meta["cached_plan_repair_reason"] = str(repair_reason or "").strip()
    repaired["meta"] = meta
    return repaired


def _ensure_cached_compose_payload_contract(
    *,
    compose_service: Any,
    page: int,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = _jsonable_dict(payload)
    ensure_contract = getattr(compose_service, "_ensure_payload_contract", None)
    if not callable(ensure_contract):
        return normalized
    try:
        repaired = ensure_contract(page=int(page), payload=normalized)
    except Exception as exc:
        logger.warning(
            "[Literature API] cached compose payload contract repair failed "
            f"page={page}: {exc}"
        )
        return normalized
    return repaired if isinstance(repaired, dict) else normalized


def _build_cached_generative_seed_plan(
    *,
    runtime: Any,
    page: int,
    user_intent: str,
    compose_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    composed_seed = _jsonable_dict((compose_payload or {}).get("generative_reader_plan") or {})

    build_seed_plan = getattr(runtime, "build_seed_plan", None)
    runtime_seed: Dict[str, Any] = {}
    if callable(build_seed_plan):
        try:
            built_seed = build_seed_plan(
                page=int(page),
                user_intent=str(user_intent or "").strip(),
                compose_payload=compose_payload,
            )
        except Exception as exc:
            logger.warning(
                "[Literature API] cached generative seed build failed "
                f"page={page}: {exc}"
            )
        else:
            if isinstance(built_seed, Mapping):
                runtime_seed = _jsonable_dict(built_seed)

    if composed_seed:
        return _repair_sparse_cached_generative_plan(
            runtime=runtime,
            page=int(page),
            user_intent=str(user_intent or "").strip(),
            compose_payload=compose_payload,
            plan_payload=composed_seed,
            repair_reason="compose_seed_missing_guided_structure",
        )

    if runtime_seed:
        return runtime_seed

    return {}


def _summarize_cached_generative_plan(plan: Mapping[str, Any]) -> Dict[str, int]:
    normalized = _jsonable_dict(plan)
    story_substrate = dict(normalized.get("story_substrate") or {})
    page_brief = dict(normalized.get("page_brief") or {})
    meta = dict(normalized.get("meta") or {})
    planning_brief = dict(meta.get("planning_brief") or {})

    resource_modules = [
        row for row in list(normalized.get("resource_modules") or []) if isinstance(row, Mapping)
    ]
    interaction_modules = [
        row for row in list(normalized.get("interaction_modules") or []) if isinstance(row, Mapping)
    ]
    js_widgets = [row for row in list(normalized.get("js_widgets") or []) if isinstance(row, Mapping)]
    module_count = len(resource_modules) + len(interaction_modules) + len(js_widgets)

    story_signal_count = sum(
        len([row for row in list(story_substrate.get(key) or []) if isinstance(row, Mapping)])
        for key in (
            "main_claims",
            "evidence_units",
            "terms_to_explain",
            "background_gaps",
            "narrative_turns",
        )
    )
    brief_signal_count = (
        len([item for item in list(page_brief.get("reading_path") or []) if str(item or "").strip()])
        + len([item for item in list(page_brief.get("experience_hooks") or []) if str(item or "").strip()])
        + len([item for item in list(page_brief.get("storyboard") or []) if isinstance(item, Mapping)])
        + len([item for item in list(page_brief.get("body_flow_target_ids") or []) if str(item or "").strip()])
        + int(bool(str(page_brief.get("page_goal") or "").strip()))
        + int(bool(str(page_brief.get("hero_angle") or "").strip()))
        + int(bool(str(page_brief.get("primary_focus_target_id") or "").strip()))
    )
    planning_signal_count = (
        len([item for item in list(planning_brief.get("recommended_sections") or []) if str(item or "").strip()])
        + len([item for item in list(planning_brief.get("tool_hints") or []) if str(item or "").strip()])
        + len([item for item in list(planning_brief.get("guided_beat_seed") or []) if isinstance(item, Mapping)])
        + len([item for item in list(planning_brief.get("body_flow_target_ids") or []) if str(item or "").strip()])
        + int(bool(str(planning_brief.get("summary") or "").strip()))
    )
    stage_count = len([row for row in list(meta.get("runtime_stage_trace") or []) if isinstance(row, Mapping)])
    score = (
        module_count * 6
        + story_signal_count * 2
        + brief_signal_count * 2
        + planning_signal_count * 3
        + stage_count
    )
    return {
        "module_count": module_count,
        "story_signal_count": story_signal_count,
        "brief_signal_count": brief_signal_count,
        "planning_signal_count": planning_signal_count,
        "stage_count": stage_count,
        "score": score,
    }


def _is_scaffold_like_generative_plan(plan: Mapping[str, Any]) -> bool:
    normalized = _jsonable_dict(plan)
    if not normalized:
        return True

    status = str(normalized.get("status") or "").strip()
    meta = dict(normalized.get("meta") or {})
    summary = _summarize_cached_generative_plan(normalized)
    fallback_reason = str(meta.get("fallback_reason") or "").strip()

    if status in {"", "draft"}:
        return True
    if bool(meta.get("seed_plan")):
        return True
    if fallback_reason in {"seed_plan", "empty_module_plan", "agent_not_run"}:
        return True
    if (
        summary["module_count"] == 0
        and summary["story_signal_count"] <= 2
        and summary["brief_signal_count"] <= 3
        and summary["planning_signal_count"] == 0
        and summary["stage_count"] == 0
    ):
        return True
    if (
        summary["score"] <= 12
        and summary["module_count"] <= 1
        and summary["story_signal_count"] <= 2
        and summary["brief_signal_count"] <= 4
        and summary["planning_signal_count"] == 0
        and summary["stage_count"] == 0
    ):
        return True
    return False


def _has_full_generative_plan_inspect_payload(plan: Mapping[str, Any]) -> bool:
    normalized = _jsonable_dict(plan)
    if not normalized or _is_scaffold_like_generative_plan(normalized):
        return False

    meta = dict(normalized.get("meta") or {})
    planning_brief = dict(meta.get("planning_brief") or {})
    planner_output = dict(meta.get("planner_output") or {})
    tool_enrichment_packet = dict(meta.get("tool_enrichment_packet") or {})
    runtime_stage_trace = [
        row for row in list(meta.get("runtime_stage_trace") or []) if isinstance(row, Mapping)
    ]
    guided_beats = [row for row in list(normalized.get("guided_beats") or []) if isinstance(row, Mapping)]
    planner_guided_beats = [
        row for row in list(planner_output.get("guided_beats") or []) if isinstance(row, Mapping)
    ]

    if not planning_brief:
        return False
    if not planner_output:
        return False
    if not tool_enrichment_packet:
        return False
    if not runtime_stage_trace:
        return False
    if not guided_beats and not planner_guided_beats:
        return False
    return True


_INTERMEDIATE_ARTIFACT_STAGE_TOKENS = {
    "seed",
    "seed_plan",
    "draft",
    "critic",
    "critique",
    "review",
    "intermediate",
    "staging",
    "partial",
    "provisional",
}


def _stage_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _payload_has_intermediate_artifact_stage(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    meta = dict(payload.get("meta") or {})
    stage_candidates = [
        payload.get("status"),
        payload.get("stage"),
        payload.get("artifact_stage"),
        payload.get("artifact_kind"),
        meta.get("status"),
        meta.get("stage"),
        meta.get("artifact_stage"),
        meta.get("artifact_kind"),
        meta.get("plan_stage"),
        meta.get("review_stage"),
        meta.get("lifecycle_stage"),
    ]
    for value in stage_candidates:
        if _stage_token(value) in _INTERMEDIATE_ARTIFACT_STAGE_TOKENS:
            return True
    if bool(meta.get("seed_plan")):
        return True
    if meta.get("runtime_build_plan_evidence") is False:
        return True
    return False


def _is_completed_generative_plan_for_experience(plan: Mapping[str, Any] | None) -> bool:
    if not isinstance(plan, Mapping):
        return False
    if _stage_token(plan.get("status")) != "done":
        return False
    if _payload_has_intermediate_artifact_stage(plan):
        return False
    return True


def _is_completed_experience_manuscript(plan: Mapping[str, Any] | None) -> bool:
    if not isinstance(plan, Mapping):
        return False
    if _stage_token(plan.get("status")) != "done":
        return False
    if _payload_has_intermediate_artifact_stage(plan):
        return False

    manuscript = plan.get("teaching_manuscript")
    if isinstance(manuscript, Mapping):
        if _stage_token(manuscript.get("status")) != "done":
            return False
        if _payload_has_intermediate_artifact_stage(manuscript):
            return False

        segments = [row for row in list(manuscript.get("segments") or []) if isinstance(row, Mapping)]
        if not segments:
            return False
        for row in segments:
            if str(row.get("teaching_text") or "").strip():
                return True
            if str(row.get("anchor_excerpt") or "").strip():
                return True
            if list(row.get("target_ids") or []):
                return True
        return False

    if str(plan.get("page_story_title") or "").strip():
        return True
    if str(dict(plan.get("hero") or {}).get("title") or "").strip():
        return True
    if list(plan.get("main_sections") or []):
        return True
    return False


def _should_prefer_compose_derived_plan(
    *,
    cached_plan: Mapping[str, Any],
    derived_plan: Mapping[str, Any],
) -> bool:
    if not _is_scaffold_like_generative_plan(cached_plan):
        return False
    if _is_scaffold_like_generative_plan(derived_plan):
        return False
    cached_summary = _summarize_cached_generative_plan(cached_plan)
    derived_summary = _summarize_cached_generative_plan(derived_plan)
    return derived_summary["score"] > cached_summary["score"]


def _can_derive_cached_generative_plan(
    *,
    compose_payload: Mapping[str, Any],
    seed_plan: Mapping[str, Any],
) -> bool:
    enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
    target_count = len(
        [
            row
            for row in list(enrichment_bundle.get("targets") or [])
            if isinstance(row, Mapping) and str(row.get("target_id") or "").strip()
        ]
    )
    if target_count > 0:
        return True
    if list(seed_plan.get("resource_modules") or []):
        return True
    if list(seed_plan.get("interaction_modules") or []):
        return True
    if list(seed_plan.get("js_widgets") or []):
        return True
    return False


def _derive_staged_generative_plan_from_cached_compose(
    *,
    runtime: Any,
    page: int,
    user_intent: str,
    compose_payload: Mapping[str, Any],
    adjacent_page_context: Sequence[Mapping[str, Any]],
    page_dossier: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    seed_plan = _build_cached_generative_seed_plan(
        runtime=runtime,
        page=int(page),
        user_intent=str(user_intent or "").strip(),
        compose_payload=compose_payload,
    )
    if not seed_plan:
        return None
    if not _can_derive_cached_generative_plan(
        compose_payload=compose_payload,
        seed_plan=seed_plan,
    ):
        return None

    original_status = str(seed_plan.get("status") or "").strip() or "draft"
    prepared = _jsonable_dict(seed_plan)
    prepared["status"] = "done"
    prepared.setdefault("version", "v1")
    prepared.setdefault("shell_mode", "resource_augmented_reader")
    prepared.setdefault("used_tools", [])
    prepared.setdefault("tool_trace", [])

    planning_brief: Dict[str, Any] = {}
    build_planning_brief = getattr(runtime, "_build_planning_brief", None)
    if callable(build_planning_brief):
        try:
            built_brief = build_planning_brief(
                page=int(page),
                user_intent=str(user_intent or "").strip(),
                enrichment_bundle=dict((compose_payload or {}).get("enrichment_bundle") or {}),
                page_dossier=page_dossier,
                adjacent_page_context=adjacent_page_context,
            )
        except Exception as exc:
            logger.warning(
                "[Literature API] cached planning brief build failed "
                f"page={page}: {exc}"
            )
        else:
            if isinstance(built_brief, Mapping):
                planning_brief = _jsonable_dict(built_brief)

    target_count = len(
        [
            row
            for row in list(dict((compose_payload or {}).get("enrichment_bundle") or {}).get("targets") or [])
            if isinstance(row, Mapping)
        ]
    )
    stage_row_builder = getattr(runtime, "_build_runtime_stage_row", None)
    if callable(stage_row_builder):
        runtime_stage_trace = [
            stage_row_builder(
                stage_id="cached_compose_derivation",
                stage_kind="materialization",
                status="done",
                summary="Derived a staged generative plan from cached compose data without a live agent run.",
                meta={
                    "focus_page": int(page),
                    "target_count": int(target_count),
                    "seed_status": original_status,
                    "compose_source_signature": str(compose_payload.get("source_signature") or "").strip(),
                },
            )
        ]
    else:
        runtime_stage_trace = [
            {
                "stage_id": "cached_compose_derivation",
                "stage_kind": "materialization",
                "status": "done",
                "summary": "Derived a staged generative plan from cached compose data without a live agent run.",
                "meta": {
                    "focus_page": int(page),
                    "target_count": int(target_count),
                    "seed_status": original_status,
                    "compose_source_signature": str(compose_payload.get("source_signature") or "").strip(),
                },
            }
        ]

    prepared_meta = dict(prepared.get("meta") or {})
    prepared_meta.pop("fallback_reason", None)
    prepared_meta["seed_plan"] = False
    prepared_meta["derived_from"] = "cached_compose_payload"
    prepared_meta["compose_seed_status"] = original_status
    prepared_meta["cache_miss"] = True
    if planning_brief:
        prepared_meta["planning_brief"] = planning_brief
    prepared_meta["adjacent_page_context"] = [
        _jsonable_dict(row)
        for row in list(adjacent_page_context or [])
        if isinstance(row, Mapping)
    ]
    prepared_meta["page_dossier"] = _jsonable_dict(page_dossier)
    prepared_meta["runtime_stage_trace"] = runtime_stage_trace
    prepared["meta"] = prepared_meta

    finalize_plan = getattr(runtime, "_finalize_plan", None)
    if callable(finalize_plan):
        try:
            finalized = finalize_plan(
                parsed=prepared,
                page=int(page),
                user_intent=str(user_intent or "").strip(),
                enrichment_bundle=dict((compose_payload or {}).get("enrichment_bundle") or {}),
                compose_payload=compose_payload,
                used_tools=list(prepared.get("used_tools") or []),
                tool_trace=[
                    dict(row)
                    for row in list(prepared.get("tool_trace") or [])
                    if isinstance(row, Mapping)
                ],
                adjacent_page_context=adjacent_page_context,
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )
        except Exception as exc:
            logger.warning(
                "[Literature API] cached generative plan finalization failed "
                f"page={page}: {exc}"
            )
        else:
            if isinstance(finalized, Mapping):
                prepared = _jsonable_dict(finalized)

    prepared["status"] = "done"
    final_meta = dict(prepared.get("meta") or {})
    final_meta.pop("fallback_reason", None)
    final_meta["seed_plan"] = False
    final_meta["derived_from"] = "cached_compose_payload"
    final_meta["compose_seed_status"] = original_status
    final_meta["cache_miss"] = True
    if planning_brief and not isinstance(final_meta.get("planning_brief"), Mapping):
        final_meta["planning_brief"] = planning_brief
    if not isinstance(final_meta.get("runtime_stage_trace"), list):
        final_meta["runtime_stage_trace"] = runtime_stage_trace
    prepared["meta"] = final_meta
    return prepared


def _mark_cached_compose_plan_as_provisional_seed(
    plan_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    provisional = _jsonable_dict(plan_payload)
    original_status = str(provisional.get("status") or "").strip() or "draft"
    provisional["status"] = "draft"

    meta = dict(provisional.get("meta") or {})
    meta["seed_plan"] = True
    meta["runtime_build_plan_evidence"] = False
    meta.setdefault("derived_from", "cached_compose_payload")
    meta.setdefault("compose_seed_status", original_status)
    provisional["meta"] = meta
    return provisional


def _promote_provisional_plan_for_experience_build(
    plan_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    promoted = _jsonable_dict(plan_payload)
    promoted["status"] = "done"
    return promoted


async def _generative_plan_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature GenerativePlan] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = _generative_plan_cache_memory.get(cache_key)
    if not item:
        db_payload, expires_at = await _plan_cache_db_get(cache_key, GENERATIVE_PLAN_CACHE_KIND)
        if isinstance(db_payload, dict):
            ttl_seconds = GENERATIVE_PLAN_CACHE_TTL_SECONDS
            if isinstance(expires_at, datetime):
                delta = (expires_at - datetime.utcnow()).total_seconds()
                ttl_seconds = max(1, int(delta)) if delta > 0 else 1
            _generative_plan_cache_memory[cache_key] = (time.time() + ttl_seconds, db_payload)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(db_payload, ensure_ascii=False),
                        ex=max(1, int(ttl_seconds)),
                    )
                except Exception as exc:
                    logger.warning(f"[Literature GenerativePlan] Redis回填失败，保留内存/DB缓存: {exc}")
            return db_payload, "db"
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        _generative_plan_cache_memory.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _generative_plan_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = GENERATIVE_PLAN_CACHE_TTL_SECONDS,
    *,
    user_id: Optional[int] = None,
    paper_id: Optional[int] = None,
    page: Optional[int] = None,
    compose_source_signature: Optional[str] = None,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            GENERATIVE_PLAN_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature GenerativePlan] Redis写入失败，降级内存缓存: {exc}")

    _generative_plan_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


async def _persist_generative_plan_cache(
    cache_key: str,
    payload: Dict[str, Any],
    *,
    user_id: Optional[int],
    paper_id: Optional[int],
    page: Optional[int],
    compose_source_signature: Optional[str],
    ttl_seconds: int = GENERATIVE_PLAN_CACHE_TTL_SECONDS,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            GENERATIVE_PLAN_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )


async def _experience_plan_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature ExperiencePlan] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = _experience_plan_cache_memory.get(cache_key)
    if not item:
        db_payload, expires_at = await _plan_cache_db_get(cache_key, EXPERIENCE_PLAN_CACHE_KIND)
        if isinstance(db_payload, dict):
            ttl_seconds = EXPERIENCE_PLAN_CACHE_TTL_SECONDS
            if isinstance(expires_at, datetime):
                delta = (expires_at - datetime.utcnow()).total_seconds()
                ttl_seconds = max(1, int(delta)) if delta > 0 else 1
            _experience_plan_cache_memory[cache_key] = (time.time() + ttl_seconds, db_payload)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(db_payload, ensure_ascii=False),
                        ex=max(1, int(ttl_seconds)),
                    )
                except Exception as exc:
                    logger.warning(f"[Literature ExperiencePlan] Redis回填失败，保留内存/DB缓存: {exc}")
            return db_payload, "db"
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        _experience_plan_cache_memory.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _experience_plan_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = EXPERIENCE_PLAN_CACHE_TTL_SECONDS,
    *,
    user_id: Optional[int] = None,
    paper_id: Optional[int] = None,
    page: Optional[int] = None,
    compose_source_signature: Optional[str] = None,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            EXPERIENCE_PLAN_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature ExperiencePlan] Redis写入失败，降级内存缓存: {exc}")

    _experience_plan_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


async def _persist_experience_plan_cache(
    cache_key: str,
    payload: Dict[str, Any],
    *,
    user_id: Optional[int],
    paper_id: Optional[int],
    page: Optional[int],
    compose_source_signature: Optional[str],
    ttl_seconds: int = EXPERIENCE_PLAN_CACHE_TTL_SECONDS,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            EXPERIENCE_PLAN_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )


async def _experience_session_v2_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature ExperienceSessionV2] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = _experience_session_v2_cache_memory.get(cache_key)
    if not item:
        db_payload, expires_at = await _plan_cache_db_get(cache_key, EXPERIENCE_SESSION_V2_CACHE_KIND)
        if isinstance(db_payload, dict):
            ttl_seconds = EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS
            if isinstance(expires_at, datetime):
                now_dt = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
                delta = (expires_at - now_dt).total_seconds()
                ttl_seconds = max(1, int(delta)) if delta > 0 else 1
            _experience_session_v2_cache_memory[cache_key] = (time.time() + ttl_seconds, db_payload)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(db_payload, ensure_ascii=False),
                        ex=max(1, int(ttl_seconds)),
                    )
                except Exception as exc:
                    logger.warning(f"[Literature ExperienceSessionV2] Redis回填失败，保留内存/DB缓存: {exc}")
            return db_payload, "db"
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        _experience_session_v2_cache_memory.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _experience_session_v2_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS,
    *,
    user_id: Optional[int] = None,
    paper_id: Optional[int] = None,
    page: Optional[int] = None,
    compose_source_signature: Optional[str] = None,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            EXPERIENCE_SESSION_V2_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature ExperienceSessionV2] Redis写入失败，降级内存缓存: {exc}")

    _experience_session_v2_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


async def _page_artifact_v2_cache_get(cache_key: str) -> tuple[Optional[Dict[str, Any]], str]:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(cache_key)
            if payload:
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data, "redis"
        except Exception as exc:
            logger.warning(f"[Literature PageArtifactV2] Redis读取失败，降级内存缓存: {exc}")

    now_ts = time.time()
    item = _page_artifact_v2_cache_memory.get(cache_key)
    if not item:
        db_payload, expires_at = await _plan_cache_db_get(cache_key, PAGE_ARTIFACT_V2_CACHE_KIND)
        if isinstance(db_payload, dict):
            ttl_seconds = PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS
            if isinstance(expires_at, datetime):
                now_dt = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
                delta = (expires_at - now_dt).total_seconds()
                ttl_seconds = max(1, int(delta)) if delta > 0 else 1
            _page_artifact_v2_cache_memory[cache_key] = (time.time() + ttl_seconds, db_payload)
            if redis_client is not None:
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(db_payload, ensure_ascii=False),
                        ex=max(1, int(ttl_seconds)),
                    )
                except Exception as exc:
                    logger.warning(f"[Literature PageArtifactV2] Redis回填失败，保留内存/DB缓存: {exc}")
            return db_payload, "db"
        return None, "none"
    expire_at, payload = item
    if expire_at <= now_ts:
        _page_artifact_v2_cache_memory.pop(cache_key, None)
        return None, "none"
    return payload, "memory"


async def _page_artifact_v2_cache_set(
    cache_key: str,
    payload: Dict[str, Any],
    ttl_seconds: int = PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
    *,
    user_id: Optional[int] = None,
    paper_id: Optional[int] = None,
    page: Optional[int] = None,
    compose_source_signature: Optional[str] = None,
) -> None:
    if all(value is not None for value in (user_id, paper_id, page, compose_source_signature)):
        await _plan_cache_db_set(
            cache_key,
            PAGE_ARTIFACT_V2_CACHE_KIND,
            payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=str(compose_source_signature or "").strip(),
            ttl_seconds=ttl_seconds,
        )

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[Literature PageArtifactV2] Redis写入失败，降级内存缓存: {exc}")

    _page_artifact_v2_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


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
        if item.status == KnowledgeLinkStatus.COMPLETED.value and item.document_id:
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
            logger.warning(f"[Literature Ask] FTS 检索失败，回退 ILIKE: {exc}")

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
                error="no_completed_documents",
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
你需要自行决定是否调用工具、调用哪一个工具以及调用次数。
不要机械套用固定流程，应根据问题类型动态选择 strategy（例如 knowledge_search、paper_read、web_search/MCP 网页工具）。

决策原则：
1. 当前论文可直接回答时，可使用 paper_read。
1.1 调用 paper_read 时，query 必须尽量复用用户问题原文，不要固定套用中文模板词。
1.2 若 paper_read 首次命中较弱（例如 quality=low 或片段明显不相关），可将 query 做中英互换后重试一次。
2. 需要知识库片段或跨文档证据时，可使用 knowledge_search。
3. 本地证据不足且确有必要时，再使用网页/MCP 工具，并标注时效性风险。
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
3. 若使用网页来源，需在结论中明确“网页来源”与时效性风险。
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
                "请根据工具返回的信息继续。若检索诊断为 quality=low 或片段不相关，"
                "可将 query 做中英互换后再调用一次 paper_read。"
                "若要给出最终回答，必须在关键结论后保留对应的 [来源X] 标注，"
                "且只能使用 observation 中已出现过的来源编号。"
                "如证据不足，请明确说明。请用 <answer> 标签给出最终回答。"
            )
        elif tool_name == "knowledge_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中已出现过的来源编号。"
                "如证据不足，请明确说明。请用 <answer> 标签给出最终回答。"
            )
        else:
            followup = "请根据工具返回的信息继续。如果已有足够信息，请用 <answer> 标签给出最终回答。"
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
        base_prompt = f"{self.SYSTEM_PROMPT.format(tools_description=tools_desc)}\n\n{self.CITATION_POLICY_PROMPT}"
        return self._compose_profile_prompt_sections(
            base_prompt,
            available_tools=selected_tools,
            include_generic_citation_policy=False,
        )

    async def _execute_single_tool_call(self, context: Any, call: Any, *, parallel_group: str):  # type: ignore[override]
        executed = await super()._execute_single_tool_call(context, call, parallel_group=parallel_group)
        tool_name = str(getattr(call, "name", "") or "")
        if tool_name == "paper_read":
            try:
                context.allowed_source_labels.update(self._extract_source_labels(executed.observation_output))
            except (AttributeError, TypeError):
                logger.debug("[Literature Ask] skip source-label update: context unavailable")
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


async def _build_generative_reader_agent_tool_registry_for_paper(
    *,
    db: AsyncSession,
    current_user: User,
    paper: Paper,
    selected_kb_id: Optional[int],
) -> tuple[Optional[ToolRegistry], Set[str]]:
    semantic_tool_names = {"paper_read", "knowledge_search", "web_search", "web_scrape"}
    paper_pdf_path = _resolve_local_pdf_path(int(current_user.id), paper)
    resolved_kb_id = int(selected_kb_id or 0)

    if resolved_kb_id > 0:
        kb = await _get_owned_kb_or_none(db, current_user, int(resolved_kb_id))
        if kb is not None:
            ready_links, _ = await _retrieve_scope_ready_links(
                db,
                user_id=int(current_user.id),
                kb_id=int(kb.id),
                paper_ids=[int(paper.id)],
            )
            document_ids = sorted({int(link.document_id) for link in ready_links if getattr(link, "document_id", None)})
            registry, allowed = await _build_literature_agent_tool_registry(
                db=db,
                user_id=int(current_user.id),
                knowledge_base_id=int(kb.id),
                knowledge_base_name=str(kb.name or f"KB#{kb.id}"),
                document_ids=document_ids,
                paper_id=int(paper.id),
                paper_title=str(getattr(paper, "title", None) or f"paper_{paper.id}"),
                paper_pdf_path=paper_pdf_path,
            )
            return registry, {
                name
                for name in allowed
                if name in semantic_tool_names or _is_web_mcp_tool_name(name)
            }

    if not paper_pdf_path or not os.path.exists(str(paper_pdf_path)):
        return None, set()

    registry = ToolRegistry(
        db=db,
        db_session_factory=async_session_factory,
        user_id=int(current_user.id),
    )
    registry.register(
        LiteratureDirectPaperReadTool(
            paper_id=int(paper.id),
            paper_title=str(getattr(paper, "title", None) or f"paper_{paper.id}"),
            pdf_path=str(paper_pdf_path),
            source_index_allocator=LiteratureSourceIndexAllocator(),
        )
    )

    def _is_allowed_tool_name(name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"paper_read", "web_search", "web_scrape"}:
            return True
        return _is_web_mcp_tool_name(normalized)

    refresh_mcp_tools = getattr(registry, "refresh_mcp_tools", None)
    if callable(refresh_mcp_tools):
        try:
            maybe_awaitable = refresh_mcp_tools(force_refresh=False)
            if hasattr(maybe_awaitable, "__await__"):
                await maybe_awaitable
        except Exception as exc:
            logger.warning(f"[GenerativeReader] MCP 工具刷新失败，继续使用本地工具: {exc}")

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
    return registry, {
        name
        for name in allowed_tool_names
        if name in semantic_tool_names or _is_web_mcp_tool_name(name)
    }


def _derive_link_status_from_document(doc: Optional[Document]) -> tuple[str, Optional[str], Optional[int]]:
    """
    根据文档状态统一推导论文入库 link 状态。
    返回: (link_status, error_message, document_id)
    """
    if doc is None:
        return KnowledgeLinkStatus.FAILED.value, "文档不存在", None
    normalized_status = str(doc.status or "").strip().lower()
    if normalized_status == DocumentStatus.COMPLETED.value:
        return KnowledgeLinkStatus.COMPLETED.value, None, int(doc.id)
    if normalized_status == DocumentStatus.TIMEOUT.value:
        return KnowledgeLinkStatus.TIMEOUT.value, (doc.error_message or "文档处理超时"), int(doc.id)
    if normalized_status in {DocumentStatus.CANCELLED.value, "canceled"}:
        return KnowledgeLinkStatus.CANCELLED.value, (doc.error_message or "文档处理已取消"), int(doc.id)
    if normalized_status == DocumentStatus.FAILED.value:
        return KnowledgeLinkStatus.FAILED.value, (doc.error_message or "文档处理失败"), int(doc.id)
    if normalized_status == DocumentStatus.PENDING.value:
        return KnowledgeLinkStatus.PENDING.value, None, int(doc.id)
    return KnowledgeLinkStatus.RUNNING.value, None, int(doc.id)


def _mark_stale_document_timeout(doc: Optional[Document]) -> bool:
    """将长时间未收尾的 processing 文档统一回写为 timeout。"""
    if doc is None:
        return False

    stale_timeout_seconds = max(
        int(getattr(settings, "document_processing_stale_timeout_seconds", 7200)),
        60,
    )
    last_updated_at = getattr(doc, "updated_at", None) or getattr(doc, "created_at", None)
    if not is_stale_processing_status(
        status=getattr(doc, "status", None),
        last_updated_at=last_updated_at,
        timeout_seconds=stale_timeout_seconds,
    ):
        return False

    previous_error = str(getattr(doc, "error_message", "") or "").strip()
    timeout_error = build_timeout_error_message(stale_timeout_seconds)
    doc.status = DocumentStatus.TIMEOUT.value
    doc.error_message = f"{previous_error} | {timeout_error}" if previous_error else timeout_error
    return True


async def _sync_link_status_from_document(
    db: AsyncSession,
    link: PaperKnowledgeLink,
) -> tuple[bool, bool]:
    """
    以 document 为权威来源，修正文档超时与 link 状态。
    返回: (document_changed, link_changed)
    """
    if not link.document_id:
        return False, False

    doc = await db.get(Document, int(link.document_id))
    if not doc:
        return False, False

    document_changed = _mark_stale_document_timeout(doc)
    next_status, next_error, resolved_doc_id = _derive_link_status_from_document(doc)

    link_changed = False
    if link.status != next_status or (link.error_message or None) != (next_error or None):
        link.status = next_status
        link.error_message = next_error
        link_changed = True
    if resolved_doc_id is not None and link.document_id != resolved_doc_id:
        link.document_id = resolved_doc_id
        link_changed = True

    return document_changed, link_changed


async def _run_document_processing_for_link(link_id: int, doc_id: int, chunk_size: int, chunk_overlap: int) -> None:
    """
    论文入库后台任务：
    1) link -> running
    2) 复用 knowledge.process_document_task
    3) 根据 document.status 回写 link 状态
    """
    from app.api.knowledge import process_document_task
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        link = await db.get(PaperKnowledgeLink, link_id)
        if not link:
            return
        link.status = KnowledgeLinkStatus.RUNNING.value
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

        link_status, error_message, resolved_doc_id = _derive_link_status_from_document(doc)
        link.status = link_status
        link.error_message = error_message
        if resolved_doc_id is not None:
            link.document_id = resolved_doc_id

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
        configured = _to_int(getattr(settings, "react_max_iterations", None)) or 14
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
    source: str = Query(
        str(getattr(settings, "literature_search_default_source", "auto") or "auto"),
        description="数据源：auto, semantic_scholar, arxiv, pubmed, openalex, crossref, multi",
    ),
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
    - auto: 按配置的 provider 链自动回退（默认）
    - semantic_scholar: Semantic Scholar (综合学术搜索，有引用数据)
    - arxiv: arXiv (预印本平台，含 cs/physics/math 等学科)
    - pubmed: PubMed (生物医学文献)
    - openalex: OpenAlex (开放学术图谱)
    - crossref: CrossRef (DOI 元数据)
    - multi: OpenAlex + Semantic Scholar + arXiv + PubMed 并行融合
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
    if source == "multi":
        result = await service.search_multi(
            query=query,
            limit_per_source=limit,
            offset=offset,
            year_range=kwargs.get("year_range"),
        )
    else:
        result = await service.search(query, source, limit, offset, **kwargs)

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    # 检查哪些论文已保存
    papers = result.get("papers", [])
    search_results = []

    saved_lookup = await _load_saved_paper_lookup(db, current_user.id, papers)

    for paper in papers:
        saved_paper_id = _resolve_saved_paper_id(paper, saved_lookup)
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
            is_saved=saved_paper_id is not None,
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
        offset=result.get("offset", offset),
        has_more=bool(result.get("has_more", offset + len(search_results) < int(result.get("total", 0) or 0))),
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
    collection_id: Optional[int] = Query(None, description="收藏夹ID"),
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
    """保存论文（从搜索结果）。"""
    logger.info(f"[Literature API] 保存论文: {request.title[:50]}...")

    collection_ids = await _resolve_target_collection_ids_for_save(
        db,
        user_id=current_user.id,
        requested_collection_ids=request.collection_ids,
    )
    request_arxiv_id = _normalize_arxiv_id(
        _infer_arxiv_id_from_candidates(
            request.arxiv_id,
            request.external_id if request.source == "arxiv" else None,
            request.doi,
            request.url,
            (request.raw_data or {}).get("imported_link"),
        )
    )
    request_pdf_url = request.pdf_url or _build_arxiv_pdf_url(request_arxiv_id)
    existing = await _find_existing_paper_for_request(
        db,
        user_id=current_user.id,
        request=request,
    )

    if existing:
        if collection_ids:
            await _add_paper_to_collections_if_missing(
                db,
                paper_id=existing.id,
                user_id=current_user.id,
                collection_ids=collection_ids,
            )
            await db.commit()
        existing_collection_ids = await _load_collection_ids_for_paper(db, existing.id)
        return PaperResponse(**paper_to_response(existing, existing_collection_ids))

    # 创建论文
    paper = Paper(
        user_id=current_user.id,
        semantic_scholar_id=request.external_id if request.source == "semantic_scholar" else None,
        arxiv_id=request_arxiv_id or None,
        pubmed_id=request.external_id if request.source == "pubmed" else None,
        doi=request.doi,
        title=request.title,
        abstract=request.abstract,
        authors=request.authors,
        year=request.year,
        venue=request.venue,
        citation_count=request.citation_count,
        reference_count=request.reference_count,
        url=request.url,
        pdf_url=request_pdf_url,
        arxiv_url=f"https://arxiv.org/abs/{request_arxiv_id}" if request_arxiv_id else None,
        fields_of_study=request.fields_of_study,
        source=request.source,
        raw_data=request.raw_data
    )

    try:
        db.add(paper)
        await db.flush()
        await _ensure_paper_entity(db, paper)

        if collection_ids:
            await _add_paper_to_collections_if_missing(
                db,
                paper_id=paper.id,
                user_id=current_user.id,
                collection_ids=collection_ids,
            )

        await db.commit()
        await db.refresh(paper)
        response_collection_ids = await _load_collection_ids_for_paper(db, paper.id)
        return PaperResponse(**paper_to_response(paper, response_collection_ids))
    except IntegrityError as exc:
        await db.rollback()
        logger.warning(
            f"[Literature API] 保存论文命中并发唯一键冲突，回退到已有论文: user={current_user.id}, "
            f"source={request.source}, external_id={request.external_id}"
        )
        existing = await _find_existing_paper_for_request(
            db,
            user_id=current_user.id,
            request=request,
        )
        if existing is None:
            raise
        if collection_ids:
            await _add_paper_to_collections_if_missing(
                db,
                paper_id=existing.id,
                user_id=current_user.id,
                collection_ids=collection_ids,
            )
            await db.commit()
        existing_collection_ids = await _load_collection_ids_for_paper(db, existing.id)
        return PaperResponse(**paper_to_response(existing, existing_collection_ids))


@router.post("/papers/import-link", response_model=ImportPaperByLinkResponse)
async def import_paper_by_link(
    request: ImportPaperByLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """通过外部链接解析并入库论文。"""
    logger.info(f"[Literature API] 链接入库: {request.link[:120]}")

    service = get_literature_service()
    paper_result, resolved_source, normalized_link = await _resolve_paper_from_link(
        service,
        request.link,
    )
    save_request = _build_save_request_from_paper_result(
        paper_result,
        collection_ids=request.collection_ids,
        imported_link=normalized_link,
    )

    existing = await _find_existing_paper_for_request(
        db,
        user_id=current_user.id,
        request=save_request,
    )

    if existing:
        if request.collection_ids:
            await _add_paper_to_collections_if_missing(
                db,
                paper_id=existing.id,
                user_id=current_user.id,
                collection_ids=request.collection_ids,
            )
            await db.commit()

        collection_ids = await _load_collection_ids_for_paper(db, existing.id)
        return ImportPaperByLinkResponse(
            paper=PaperResponse(**paper_to_response(existing, collection_ids)),
            already_exists=True,
            resolved_source=resolved_source,
            normalized_link=normalized_link,
        )

    saved_paper = await save_paper(
        request=save_request,
        db=db,
        current_user=current_user,
    )
    return ImportPaperByLinkResponse(
        paper=saved_paper,
        already_exists=False,
        resolved_source=resolved_source,
        normalized_link=normalized_link,
    )


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
    
    # 处理评分变更（5 星自动收藏）
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
    
    # 5 星评分自动添加到「收藏」
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
    repaired = await _repair_user_collection_mojibake(db, int(current_user.id))
    if repaired:
        await db.commit()

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
            completed_papers=0,
            running_papers=0,
            pending_papers=0,
            failed_papers=0,
            timeout_papers=0,
            cancelled_papers=0,
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
    changed_link_ids: set[int] = set()
    need_commit = False
    for link in links:
        document_changed, link_changed = await _sync_link_status_from_document(db, link)
        if document_changed or link_changed:
            need_commit = True
        if link_changed:
            changed_link_ids.add(int(link.id))
    if need_commit:
        await db.commit()
        for link in links:
            if int(link.id) in changed_link_ids:
                await db.refresh(link)
                await _publish_paper_link_status_event(link)
    link_by_paper_id = {int(item.paper_id): item for item in links}

    counts = {
        "completed": 0,
        "running": 0,
        "pending": 0,
        "failed": 0,
        "timeout": 0,
        "cancelled": 0,
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
            if raw_status == KnowledgeLinkStatus.COMPLETED.value and link.document_id:
                status_value = "completed"
            elif raw_status == KnowledgeLinkStatus.RUNNING.value:
                status_value = "running"
            elif raw_status == KnowledgeLinkStatus.PENDING.value:
                status_value = "pending"
            elif raw_status == KnowledgeLinkStatus.FAILED.value:
                status_value = "failed"
            elif raw_status == KnowledgeLinkStatus.TIMEOUT.value:
                status_value = "timeout"
            elif raw_status in {KnowledgeLinkStatus.CANCELLED.value, "canceled"}:
                status_value = "cancelled"
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
        completed_papers=int(counts["completed"]),
        running_papers=int(counts["running"]),
        pending_papers=int(counts["pending"]),
        failed_papers=int(counts["failed"]),
        timeout_papers=int(counts["timeout"]),
        cancelled_papers=int(counts["cancelled"]),
        missing_papers=int(counts["missing"]),
        can_cross_paper_answer=bool(counts["completed"] > 0),
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
        raise HTTPException(status_code=400, detail="默认收藏夹不允许修改")
    
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
    
    download_candidates = _build_pdf_download_candidates(paper)
    if not download_candidates:
        raise HTTPException(status_code=400, detail="该论文暂无可用 PDF 下载链接")

    if paper.pdf_downloaded and paper.pdf_path and os.path.exists(paper.pdf_path) and not knowledge_base_id:
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
        success = False
        download_error = ""
        download_url = ""
        for candidate_url in download_candidates:
            success, download_error = await service.download_pdf(candidate_url, pdf_path)
            if success:
                download_url = candidate_url
                break

        if not success:
            raise HTTPException(status_code=502, detail=download_error or "PDF 下载失败")

        # 更新论文记录
        if not paper.pdf_url or str(paper.pdf_url).strip() != download_url:
            paper.pdf_url = download_url
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
            metadata_={
                "paper_id": paper.id,
                "title": paper.title,
                "ingest_request": {
                    "mode": "local_fast",
                    "extract_profile": "general",
                    "extract_granularity": "medium",
                    "requested_by": int(current_user.id),
                    "requested_at": datetime.utcnow().isoformat(),
                    "source": "literature_download_pdf",
                },
            },
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
        "document_id": int(doc.id) if knowledge_base_id else None,
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
        content_disposition_type="inline",
    )


@router.get("/reader/figure-assets/{paper_id}/{page}/{asset_id}")
async def stream_reader_figure_asset(
    paper_id: int,
    page: int,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
):
    """按需返回已缓存的 figure 图片资产（文件 URL，不使用 base64）。"""
    # TODO(security): 当前路由为前端 <img src> 兼容而临时放开鉴权。
    # 后续需收敛为：1) 短时效签名 URL（推荐）或 2) 前端 fetch(Bearer)+blob 渲染。
    # 目的：避免“知道 URL 即可读取资源”的风险。
    paper = await db.get(Paper, int(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if int(page) <= 0:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")

    normalized_asset_id = str(asset_id or "").strip()
    if not normalized_asset_id or not re.fullmatch(r"[0-9A-Za-z_.-]{1,96}", normalized_asset_id):
        raise HTTPException(status_code=400, detail="asset_id 非法")

    candidate_path, candidate_ext = await _resolve_reader_figure_asset_candidate_file(
        db=db,
        paper=paper,
        page=int(page),
        asset_id=normalized_asset_id,
    )

    if not candidate_path:
        raise HTTPException(status_code=404, detail="图片资源不存在")

    media_type = _reader_image_media_type(candidate_ext)

    return FileResponse(path=candidate_path, media_type=media_type, filename=os.path.basename(candidate_path))


@router.get("/reader/page-assets/{paper_id}/{page}")
async def stream_reader_page_asset(
    paper_id: int,
    page: int,
    db: AsyncSession = Depends(get_db),
):
    """按需返回已缓存的整页 PDF render 资产（文件 URL，不使用 base64）。"""
    paper = await db.get(Paper, int(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if int(page) <= 0:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")

    compose_service = get_literature_reader_compose_service()
    candidate_path = compose_service._find_existing_page_render_asset_path(  # pylint: disable=protected-access
        paper_id=int(paper.id),
        page=int(page),
    )
    if not candidate_path:
        pdf_path = _resolve_local_pdf_path(user_id=int(paper.user_id), paper=paper)
        if pdf_path and os.path.exists(pdf_path):
            try:
                candidate_url = await compose_service.ensure_page_render_asset(
                    paper_id=int(paper.id),
                    page=int(page),
                    pdf_path=str(pdf_path),
                )
                if candidate_url:
                    candidate_path = compose_service._find_existing_page_render_asset_path(  # pylint: disable=protected-access
                        paper_id=int(paper.id),
                        page=int(page),
                    )
            except Exception as exc:
                logger.warning(
                    "[Literature API] page render asset lazy-build failed "
                    f"paper={paper_id} page={page}: {exc}"
                )

    if not candidate_path or not os.path.exists(candidate_path):
        raise HTTPException(status_code=404, detail="页面渲染资源不存在")

    return FileResponse(
        path=candidate_path,
        media_type="image/jpeg",
        filename=os.path.basename(candidate_path),
    )


@router.get("/reader/grounding-page-assets/{paper_id}/{page}")
async def stream_reader_grounding_page_asset(
    paper_id: int,
    page: int,
    db: AsyncSession = Depends(get_db),
):
    """返回已本地化持久化的 DocMind 整页图，避免继续暴露临时 URL。"""
    paper = await db.get(Paper, int(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if int(page) <= 0:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")

    compose_service = get_literature_reader_compose_service()
    candidate_path = compose_service._find_existing_grounding_page_image_path(  # pylint: disable=protected-access
        paper_id=int(paper.id),
        page=int(page),
    )
    if not candidate_path or not os.path.exists(candidate_path):
        raise HTTPException(status_code=404, detail="页面本地化渲染图不存在")

    ext = str(os.path.splitext(candidate_path)[1] or "").lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(
        path=candidate_path,
        media_type=media_type,
        filename=os.path.basename(candidate_path),
    )


@router.get("/reader/docmind-page-image/{paper_id}/{page}")
async def stream_reader_docmind_page_image(
    paper_id: int,
    page: int,
    db: AsyncSession = Depends(get_db),
):
    """返回已本地化的 DocMind 整页渲染图，不再直接代理临时远端 URL。"""
    paper = await db.get(Paper, int(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if int(page) <= 0:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")

    service = get_literature_reader_service()
    compose_service = get_literature_reader_compose_service()
    try:
        payload, _ = await service.build_or_get_page_payload(
            db=db,
            user_id=int(paper.user_id),
            paper=paper,
            page=int(page),
            selected_kb_id=None,
            force_refresh=False,
            prefer_agent=False,
            style_hint=None,
            publish_ready_event_enabled=False,
        )
    except Exception as exc:
        logger.warning(
            "[Literature API] docmind page image build failed "
            f"paper={paper_id} page={page}: {exc}"
        )
        raise HTTPException(status_code=404, detail="页面原始渲染图不存在") from exc

    docmind_structure = dict((payload or {}).get("docmind_structure") or {})
    image_path = str(docmind_structure.get("page_image_path") or "").strip()
    if image_path and os.path.exists(image_path):
        ext = os.path.splitext(image_path)[1].lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        return FileResponse(
            path=image_path,
            media_type=media_type,
            filename=os.path.basename(image_path),
        )

    localized_path = compose_service._ensure_local_grounding_page_image(  # pylint: disable=protected-access
        paper_id=int(paper.id),
        page=int(page),
        page_image_url=str(docmind_structure.get("page_image_url") or "").strip(),
        page_image_path=image_path,
    )
    if localized_path and os.path.exists(localized_path):
        ext = os.path.splitext(localized_path)[1].lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        return FileResponse(
            path=localized_path,
            media_type=media_type,
            filename=os.path.basename(localized_path),
        )

    raise HTTPException(status_code=404, detail="页面原始渲染图不存在")


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


def _chunk_reader_blocks(blocks: Sequence[Dict[str, Any]], size: int = 5) -> List[List[Dict[str, Any]]]:
    batch_size = max(1, int(size))
    values = list(blocks or [])
    return [values[i: i + batch_size] for i in range(0, len(values), batch_size)]


async def _prefetch_reader_pages_background(
    *,
    user_id: int,
    paper_id: int,
    pages: Sequence[int],
    selected_kb_id: Optional[int],
    style_hint: Optional[str],
) -> None:
    if not pages:
        return
    service = get_literature_reader_service()
    async with async_session_factory() as db:
        stmt = select(Paper).where(and_(Paper.id == int(paper_id), Paper.user_id == int(user_id)))
        paper = (await db.execute(stmt)).scalar_one_or_none()
        if paper is None:
            return
        await service.prefetch_pages(
            db=db,
            user_id=int(user_id),
            paper=paper,
            pages=pages,
            selected_kb_id=selected_kb_id,
            style_hint=style_hint,
        )


async def _prefetch_reader_composed_pages_background(
    *,
    user_id: int,
    paper_id: int,
    pages: Sequence[int],
    selected_kb_id: Optional[int],
    pipeline_version: Optional[str],
    style_intent: Optional[str],
    latency_budget_ms: Optional[int],
    quality_target: Optional[float],
    max_iterations: Optional[int],
    theme_mode: Optional[str],
    detail_level: Optional[str],
    compare_mode: Optional[bool],
    citation_tldr: Optional[bool],
) -> None:
    if not pages:
        return
    service = get_literature_reader_compose_service()
    async with async_session_factory() as db:
        stmt = select(Paper).where(and_(Paper.id == int(paper_id), Paper.user_id == int(user_id)))
        paper = (await db.execute(stmt)).scalar_one_or_none()
        if paper is None:
            return
        await service.prefetch_pages(
            db=db,
            user_id=int(user_id),
            paper=paper,
            pages=pages,
            selected_kb_id=selected_kb_id,
            pipeline_version_override=pipeline_version,
            style_intent=style_intent,
            latency_budget_ms=latency_budget_ms,
            quality_target=quality_target,
            max_iterations=max_iterations,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
            citation_tldr=citation_tldr,
        )


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
            selected_kb_id=None,
            last_anchor={},
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
    else:
        normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
            db=db,
            current_user=current_user,
            selected_kb_id=session.selected_kb_id,
        )
        if normalized_selected_kb_id != session.selected_kb_id:
            session.selected_kb_id = normalized_selected_kb_id
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
    session.selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )
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


@router.post("/papers/{paper_id}/reader/generative/stream")
async def stream_reader_generative_page(
    paper_id: int,
    payload: ReaderGenerativeRequest,
    request: Request,
    current_user: User = Depends(get_current_user_for_stream),
):
    service = get_literature_reader_service()
    page_num = max(1, int(payload.page))

    async def event_generator():
        try:
            async with async_session_factory() as db:
                paper = await _get_owned_paper_or_404(db, current_user, paper_id)
                normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
                    db=db,
                    current_user=current_user,
                    selected_kb_id=payload.selected_kb_id,
                )
                page_payload, meta = await service.build_or_get_page_payload(
                    db=db,
                    user_id=int(current_user.id),
                    paper=paper,
                    page=page_num,
                    selected_kb_id=normalized_selected_kb_id,
                    force_refresh=bool(payload.force_refresh),
                    prefer_agent=bool(getattr(payload, "prefer_agent", False)),
                    style_hint=payload.style_hint,
                    publish_ready_event_enabled=False,
                )
            if await request.is_disconnected():
                return

            yield _sse_payload(
                "start",
                {
                    "cache_hit": bool(meta.cache_hit),
                    "cache_layer": meta.cache_layer,
                    "build_mode": str(meta.build_mode),
                    "page": int(page_num),
                    "parser_version": meta.parser_version,
                },
            )
            yield _sse_payload(
                "skeleton",
                {
                    "sections": list(page_payload.get("sections") or []),
                    "summary": str(page_payload.get("summary") or ""),
                    "style_recommendation": str(page_payload.get("style_key") or "journal_classic"),
                    "style_tuning": dict(page_payload.get("style_tuning") or {}),
                    "structure_confidence": float(page_payload.get("structure_confidence") or 0.0),
                },
            )

            for block_chunk in _chunk_reader_blocks(page_payload.get("blocks") or [], size=6):
                if await request.is_disconnected():
                    return
                yield _sse_payload("chunk", {"blocks": block_chunk})

            yield _sse_payload("assets", {"assets": list(page_payload.get("assets") or [])})
            yield _sse_payload(
                "done",
                {
                    "payload": page_payload,
                    "cache_meta": {
                        "cache_hit": bool(meta.cache_hit),
                        "cache_layer": meta.cache_layer,
                        "build_mode": str(meta.build_mode),
                        "source_signature": meta.source_signature,
                        "source_sig_hash": meta.source_sig_hash,
                    },
                },
            )
        except Exception as exc:
            logger.exception(f"[Literature API] generative stream failed paper={paper_id}, page={page_num}: {exc}")
            yield _sse_payload("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/papers/{paper_id}/reader/generative/prefetch",
    response_model=ReaderGenerativePrefetchResponse,
)
async def prefetch_reader_generative_pages(
    paper_id: int,
    payload: ReaderGenerativePrefetchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_service()
    pdf_path = _resolve_local_pdf_path(user_id=int(current_user.id), paper=paper)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=409, detail="本地 PDF 不存在，请先下载后再执行预读。")
    max_page = await _get_pdf_page_count(pdf_path)
    queued, skipped = service.queue_prefetch(
        pages=list(payload.pages or []),
        max_page=max_page,
    )

    if queued:
        background_tasks.add_task(
            _prefetch_reader_pages_background,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            pages=queued,
            selected_kb_id=payload.selected_kb_id,
            style_hint=payload.style_hint,
        )

    return ReaderGenerativePrefetchResponse(queued=queued, skipped=skipped)


# ============ 批注 ============


# ============ Reader Composed ============

@router.post(
    "/papers/{paper_id}/reader/composed/generative-plan",
    response_model=ReaderGenerativePlanResponse,
)
async def get_reader_composed_generative_plan(
    paper_id: int,
    payload: ReaderGenerativePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    runtime = get_generative_reader_agent_runtime()
    page_num = max(1, int(payload.page))
    normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )

    composed_payload, meta = await compose_service.build_or_get_composed_payload(
        db=db,
        user_id=int(current_user.id),
        paper=paper,
        page=page_num,
        selected_kb_id=normalized_selected_kb_id,
        pipeline_version_override=getattr(payload, "pipeline_version", None),
        force_refresh=bool(payload.force_refresh),
        regenerate=bool(payload.regenerate),
        latency_budget_ms=payload.latency_budget_ms,
        quality_target=payload.quality_target,
        max_iterations=getattr(payload, "max_iterations", None),
        style_intent=payload.style_intent,
        theme_mode=getattr(payload, "theme_mode", None),
        detail_level=getattr(payload, "detail_level", None),
        compare_mode=getattr(payload, "compare_mode", None),
        citation_tldr=getattr(payload, "citation_tldr", None),
        publish_ready_event_enabled=False,
    )
    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or meta.build_mode or "")
    compose_source_signature = str(composed_payload.get("source_signature") or meta.source_signature or "")
    source_sig_hash = str(meta.source_sig_hash or "")
    plan_cache_hit = False
    plan_cache_layer = "none"
    cache_key = _generative_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=page_num,
        selected_kb_id=int(normalized_selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        user_intent=str(payload.user_intent or "").strip(),
    )
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_plan, cache_layer = await _generative_plan_cache_get(cache_key)
        if isinstance(cached_plan, dict):
            cached_plan = _repair_sparse_cached_generative_plan(
                runtime=runtime,
                page=page_num,
                user_intent=str(payload.user_intent or "").strip(),
                compose_payload=composed_payload,
                plan_payload=cached_plan,
                repair_reason="cached_generative_plan_hit",
            )
            if _has_full_generative_plan_inspect_payload(cached_plan):
                plan_cache_hit = True
                plan_cache_layer = cache_layer
                adjacent_page_context = _extract_adjacent_page_context_from_plan_meta(cached_plan)
                page_dossier = _extract_page_dossier_from_plan_meta(cached_plan)
                return ReaderGenerativePlanResponse(
                    page=page_num,
                    plan=cached_plan,
                    enrichment_bundle=dict(composed_payload.get("enrichment_bundle") or {}),
                    scheme_choice=dict(composed_payload.get("scheme_choice") or {}),
                    compose_status=compose_status,
                    compose_build_mode=compose_build_mode,
                    compose_source_signature=compose_source_signature,
                    source_sig_hash=source_sig_hash,
                    cache_hit=bool(meta.cache_hit),
                    cache_layer=str(meta.cache_layer or "none"),
                    plan_cache_hit=plan_cache_hit,
                    plan_cache_layer=plan_cache_layer,
                    adjacent_page_context=adjacent_page_context,
                    page_dossier=page_dossier,
                )
    adjacent_page_context = await _build_experience_adjacent_page_context(
        compose_service=compose_service,
        paper=paper,
        focus_page=page_num,
    )
    page_dossier = _build_experience_page_dossier(
        focus_page=page_num,
        compose_payload=composed_payload,
        adjacent_page_context=adjacent_page_context,
    )
    tool_registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
        db=db,
        current_user=current_user,
        paper=paper,
        selected_kb_id=normalized_selected_kb_id,
    )
    plan = await runtime.build_plan(
        user_id=int(current_user.id),
        page=page_num,
        user_intent=str(payload.user_intent or "").strip(),
        compose_payload=composed_payload,
        tool_registry=tool_registry,
        allowed_tool_names=sorted(list(allowed_tool_names)),
        adjacent_page_context=adjacent_page_context,
        page_dossier=page_dossier,
    )
    plan_payload = plan if isinstance(plan, dict) else json.loads(json.dumps(plan, ensure_ascii=False, default=str))
    await _generative_plan_cache_set(
        cache_key,
        plan_payload,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=page_num,
        compose_source_signature=compose_source_signature,
    )
    return ReaderGenerativePlanResponse(
        page=page_num,
        plan=plan_payload,
        enrichment_bundle=dict(composed_payload.get("enrichment_bundle") or {}),
        scheme_choice=dict(composed_payload.get("scheme_choice") or {}),
        compose_status=compose_status,
        compose_build_mode=compose_build_mode,
        compose_source_signature=compose_source_signature,
        source_sig_hash=source_sig_hash,
        cache_hit=bool(meta.cache_hit),
        cache_layer=str(meta.cache_layer or "none"),
        plan_cache_hit=plan_cache_hit,
        plan_cache_layer=plan_cache_layer,
        adjacent_page_context=adjacent_page_context,
        page_dossier=page_dossier,
    )


def _build_reader_facing_experience_plan(
    *,
    focus_page: int,
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = _jsonable_dict(plan)
    normalized["focus_page"] = int(focus_page)
    normalized["status"] = "done"
    normalized["layout_variant"] = str(normalized.get("layout_variant") or "resource_augmented_reader").strip() or "resource_augmented_reader"

    hero = dict(normalized.get("hero") or {})
    title = str(hero.get("title") or "").strip()
    subtitle = str(hero.get("subtitle") or "").strip()
    summary = str(hero.get("summary") or "").strip()
    hero["display_title"] = str(hero.get("display_title") or "").strip() or title
    hero["display_subtitle"] = str(hero.get("display_subtitle") or "").strip() or subtitle
    hero["display_summary"] = str(hero.get("display_summary") or "").strip() or summary
    normalized["hero"] = hero

    manuscript = dict(normalized.get("teaching_manuscript") or {})
    segments = [
        _jsonable_dict(row)
        for row in list(manuscript.get("segments") or [])
        if isinstance(row, Mapping)
    ]
    if segments and not any(str(row.get("segment_type") or "").strip() == "body" for row in segments):
        best_row: Optional[Dict[str, Any]] = None
        best_score = -1
        for row in segments:
            score = 0
            meta = dict(row.get("meta") or {})
            adjacent_bridge = str(row.get("adjacent_bridge") or "").strip()
            title = str(row.get("title") or "").strip()
            role = str(meta.get("role") or "").strip()
            section_type = str(meta.get("section_type") or "").strip()
            if str(row.get("adjacent_bridge") or "").strip():
                score += 6
            if "线索" in adjacent_bridge:
                score += 8
            if list(row.get("reference_links") or []):
                score += 6
            if list(row.get("target_ids") or []):
                score += 2
            if str(row.get("teaching_text") or "").strip():
                score += 2
            if role in {"body", "reading_flow", "reading", "body_flow"}:
                score += 10
            if section_type in {"body", "reading_flow", "reading_flow_stage", "body_flow"}:
                score += 10
            if "正文" in title or "body" in title.lower():
                score += 8
            if role in {"opening", "focus"}:
                score -= 6
            if section_type in {"hero", "focus_stage"}:
                score -= 6
            if score > best_score:
                best_row = row
                best_score = score
        if isinstance(best_row, dict) and best_score >= 0:
            best_row["segment_type"] = "body"
    for row in segments:
        if str(row.get("segment_type") or "").strip() != "body":
            continue
        adjacent_bridge = str(row.get("adjacent_bridge") or "").strip()
        if adjacent_bridge.startswith("读到这里时，先") and "线索" not in adjacent_bridge:
            tail = adjacent_bridge.split("再看", 1)[1].strip() if "再看" in adjacent_bridge else adjacent_bridge.removeprefix("读到这里时，先").strip()
            row["adjacent_bridge"] = f"读到这里时，把前后文的线索先接起来，再看{tail}"
    normalized["teaching_manuscript"] = {
        "version": str(manuscript.get("version") or "v1"),
        "status": "done",
        "segments": segments,
    }
    return normalized


def _sanitize_experience_response_enrichment_bundle(
    *,
    enrichment_bundle: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = _jsonable_dict(enrichment_bundle)
    targets: List[Dict[str, Any]] = []
    allowed_target_kinds = {"section", "paragraph", "figure", "table", "equation", "structure"}
    for item in list(normalized.get("targets") or []):
        if not isinstance(item, Mapping):
            continue
        target_kind = str(item.get("target_kind") or "paragraph").strip() or "paragraph"
        if target_kind not in allowed_target_kinds:
            target_kind = "paragraph"
        targets.append(
            {
                "target_id": str(item.get("target_id") or "").strip(),
                "node_id": str(item.get("node_id") or item.get("target_id") or "").strip(),
                "target_kind": target_kind,
                "component_type": str(item.get("component_type") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "excerpt": str(item.get("excerpt") or "").strip(),
                "source_block_ids": [
                    str(raw).strip()
                    for raw in list(item.get("source_block_ids") or [])
                    if str(raw).strip()
                ],
                "source_atom_ids": [
                    str(raw).strip()
                    for raw in list(item.get("source_atom_ids") or [])
                    if str(raw).strip()
                ],
                "section_label": str(item.get("section_label") or "").strip(),
                "figure_label": str(item.get("figure_label") or "").strip(),
                "suggested_resource_types": [
                    str(raw).strip()
                    for raw in list(item.get("suggested_resource_types") or [])
                    if str(raw).strip()
                ],
                "meta": dict(item.get("meta") or {}),
            }
        )
    normalized["targets"] = [row for row in targets if str(row.get("target_id") or "").strip()]
    normalized["version"] = str(normalized.get("version") or "v1")
    normalized["resource_modules"] = list(normalized.get("resource_modules") or [])
    normalized["interaction_modules"] = list(normalized.get("interaction_modules") or [])
    normalized["meta"] = dict(normalized.get("meta") or {})
    return normalized


def _inject_experience_context_into_plan_meta(
    *,
    plan_payload: Mapping[str, Any],
    adjacent_page_context: Sequence[Mapping[str, Any]],
    page_dossier: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = _jsonable_dict(plan_payload)
    meta = dict(normalized.get("meta") or {})
    if adjacent_page_context and not list(meta.get("adjacent_page_context") or []):
        meta["adjacent_page_context"] = [
            _jsonable_dict(row)
            for row in list(adjacent_page_context or [])
            if isinstance(row, Mapping)
        ]
    if page_dossier and not isinstance(meta.get("page_dossier"), Mapping):
        meta["page_dossier"] = _jsonable_dict(page_dossier)
    normalized["meta"] = meta
    return normalized


def _experience_plan_has_delivery_ready_body_segment(plan_payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(plan_payload, Mapping):
        return False
    manuscript = dict(plan_payload.get("teaching_manuscript") or {})
    for row in list(manuscript.get("segments") or []):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("segment_type") or "").strip() != "body":
            continue
        if str(row.get("teaching_text") or "").strip() or list(row.get("target_ids") or []):
            return True
    return False


def _build_reader_experience_plan_response_payload(
    *,
    focus_page: int,
    plan: Mapping[str, Any],
    generative_plan: Mapping[str, Any],
    compose_payload: Mapping[str, Any],
    enrichment_bundle: Mapping[str, Any],
    compose_status: str,
    compose_build_mode: str,
    compose_source_signature: str,
    source_sig_hash: str,
    cache_hit: bool,
    cache_layer: str,
    generative_plan_cache_hit: bool,
    generative_plan_cache_layer: str,
    experience_cache_hit: bool,
    experience_cache_layer: str,
    adjacent_page_context: Sequence[Mapping[str, Any]],
    page_dossier: Mapping[str, Any],
) -> Dict[str, Any]:
    reader_plan = _build_reader_facing_experience_plan(
        focus_page=int(focus_page),
        plan=plan,
    )
    generative_payload = _jsonable_dict(generative_plan)
    compose_payload_dict = _jsonable_dict(compose_payload)
    response_compose_payload: Dict[str, Any] = {}
    if any(
        compose_payload_dict.get(key)
        for key in (
            "ui_plan",
            "page_grounding_v1",
            "page_structure_v3",
            "main_block_ids",
            "assets",
            "scheme_choice",
        )
    ):
        response_compose_payload = compose_payload_dict
    return {
        "focus_page": focus_page,
        "plan": reader_plan,
        "generative_plan": generative_payload,
        "compose_payload": response_compose_payload,
        "enrichment_bundle": _sanitize_experience_response_enrichment_bundle(
            enrichment_bundle=enrichment_bundle,
        ),
        "compose_status": compose_status,
        "compose_build_mode": compose_build_mode,
        "compose_source_signature": compose_source_signature,
        "source_sig_hash": source_sig_hash,
        "cache_hit": cache_hit,
        "cache_layer": cache_layer,
        "generative_plan_cache_hit": generative_plan_cache_hit,
        "generative_plan_cache_layer": generative_plan_cache_layer,
        "experience_cache_hit": experience_cache_hit,
        "experience_cache_layer": experience_cache_layer,
        "adjacent_page_context": [
            _jsonable_dict(row)
            for row in list(adjacent_page_context or [])
            if isinstance(row, Mapping)
        ],
        "page_dossier": _jsonable_dict(page_dossier),
    }


async def _repair_cached_experience_plan_payload(
    *,
    runtime,
    cache_key: str,
    plan_payload: Mapping[str, Any],
    generative_plan_payload: Mapping[str, Any],
    compose_payload: Mapping[str, Any],
    user_intent: str,
    reader_profile: str,
    focus_section_ids: Sequence[str],
    user_id: int,
    paper_id: int,
    page: int,
    compose_source_signature: str,
) -> Dict[str, Any]:
    current = dict(plan_payload or {})
    repair = getattr(runtime, "_validate_experience_plan_contract", None)
    if not callable(repair):
        return current
    repaired = repair(current)
    repaired_payload = (
        repaired
        if isinstance(repaired, dict)
        else json.loads(json.dumps(repaired, ensure_ascii=False, default=str))
    )
    adjacent_page_context = _extract_adjacent_page_context_from_plan_meta(generative_plan_payload)
    rebuild_needed = False
    should_upgrade_manuscript = getattr(runtime, "_teaching_manuscript_needs_upgrade", None)
    derive_adjacent_bridge_cues = getattr(runtime, "_derive_adjacent_bridge_cues", None)
    tool_enrichment_packet = dict(dict(generative_plan_payload or {}).get("meta") or {}).get("tool_enrichment_packet")
    if callable(should_upgrade_manuscript):
        generative_modules = dict(generative_plan_payload or {})
        rebuild_needed = bool(
            should_upgrade_manuscript(
                manuscript=dict(repaired_payload.get("teaching_manuscript") or {}),
                adjacent_bridge_cues=list(dict(repaired_payload.get("meta") or {}).get("adjacent_bridge_cues") or []),
                adjacent_page_context=adjacent_page_context,
                resource_modules=list(generative_modules.get("resource_modules") or []),
                interaction_modules=list(generative_modules.get("interaction_modules") or []),
                tool_enrichment_packet=dict(tool_enrichment_packet or {}),
            )
        )
        if not rebuild_needed and adjacent_page_context and callable(derive_adjacent_bridge_cues):
            rebuild_needed = bool(
                should_upgrade_manuscript(
                    manuscript=dict(repaired_payload.get("teaching_manuscript") or {}),
                    adjacent_bridge_cues=derive_adjacent_bridge_cues(adjacent_page_context),
                    adjacent_page_context=adjacent_page_context,
                    resource_modules=list(generative_modules.get("resource_modules") or []),
                    interaction_modules=list(generative_modules.get("interaction_modules") or []),
                    tool_enrichment_packet=dict(tool_enrichment_packet or {}),
                )
            )
    if not rebuild_needed and not _experience_plan_has_delivery_ready_body_segment(repaired_payload):
        rebuild_needed = True
    if rebuild_needed and isinstance(generative_plan_payload, Mapping) and isinstance(compose_payload, Mapping):
        rebuilt = runtime.build_experience_plan(
            paper_id=int(paper_id),
            focus_page=int(page),
            user_intent=str(user_intent or "").strip(),
            reader_profile=str(reader_profile or "").strip() or "curious_generalist",
            focus_section_ids=list(focus_section_ids or []),
            compose_payload=compose_payload,
            generative_plan=generative_plan_payload,
        )
        rebuilt_payload = (
            rebuilt
            if isinstance(rebuilt, dict)
            else json.loads(json.dumps(rebuilt, ensure_ascii=False, default=str))
        )
        repaired_payload = rebuilt_payload
    if repaired_payload != current:
        await _experience_plan_cache_set(
            cache_key,
            repaired_payload,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            compose_source_signature=compose_source_signature,
        )
    return repaired_payload


async def _build_reader_experience_plan_cached_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    runtime = get_generative_reader_agent_runtime()
    focus_page = max(1, int(payload.focus_page or payload.page or 1))
    normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )

    composed_payload = await compose_service.get_latest_cached_payload_only(
        db=db,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
    )
    if not isinstance(composed_payload, dict):
        raise HTTPException(status_code=404, detail="No cached reader payload available for this page")
    composed_payload = _ensure_cached_compose_payload_contract(
        compose_service=compose_service,
        page=focus_page,
        payload=composed_payload,
    )

    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or "compose_cache")
    compose_source_signature = str(composed_payload.get("source_signature") or "")
    source_sig_hash = ""
    selected_kb_id = int(normalized_selected_kb_id or 0)
    user_intent = str(payload.user_intent or "").strip()
    reader_profile = str(payload.reader_profile or "").strip()
    focus_section_ids = list(payload.focus_section_ids or [])
    adjacent_page_context: List[Dict[str, Any]] = []
    page_dossier: Dict[str, Any] = {}
    adjacent_context_loaded = False

    async def _ensure_experience_context() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        nonlocal adjacent_page_context, page_dossier, adjacent_context_loaded
        if adjacent_context_loaded:
            return adjacent_page_context, page_dossier
        built_adjacent: List[Dict[str, Any]] = []
        if hasattr(paper, "user_id") and getattr(paper, "user_id", None) is not None:
            built_adjacent = await _build_experience_adjacent_page_context(
                compose_service=compose_service,
                paper=paper,
                focus_page=focus_page,
            )
        else:
            build_adjacent = _build_experience_adjacent_page_context
            if getattr(build_adjacent, "__module__", "") != __name__:
                built_adjacent = await build_adjacent(
                    compose_service=compose_service,
                    paper=paper,
                    focus_page=focus_page,
                )
        adjacent_page_context = [
            _jsonable_dict(row)
            for row in list(built_adjacent or [])
            if isinstance(row, Mapping)
        ]
        page_dossier = _build_experience_page_dossier(
            focus_page=focus_page,
            compose_payload=composed_payload,
            adjacent_page_context=adjacent_page_context,
        )
        adjacent_context_loaded = True
        return adjacent_page_context, page_dossier

    generative_plan_payload: Optional[Dict[str, Any]] = None
    generative_cache_hit = False
    generative_cache_layer = "none"
    cached_generative_candidate: Optional[Dict[str, Any]] = None
    cached_generative_layer = "none"
    for plan_cache_key, _signature_token in _generative_plan_cache_key_candidates(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=compose_source_signature,
        user_intent=user_intent,
    ):
        cached_plan, cached_layer = await _generative_plan_cache_get(plan_cache_key)
        if not isinstance(cached_plan, dict):
            continue
        repaired_cached_plan = _repair_sparse_cached_generative_plan(
            runtime=runtime,
            page=focus_page,
            user_intent=user_intent,
            compose_payload=composed_payload,
            plan_payload=cached_plan,
            repair_reason="cached_experience_generative_plan_hit",
        )
        cached_generative_candidate = repaired_cached_plan
        cached_generative_layer = cached_layer
        break

    if not isinstance(generative_plan_payload, dict):
        derived_page_dossier = _build_experience_page_dossier(
            focus_page=focus_page,
            compose_payload=composed_payload,
            adjacent_page_context=[],
        )
        compose_derived_plan = _derive_staged_generative_plan_from_cached_compose(
            runtime=runtime,
            page=focus_page,
            user_intent=user_intent,
            compose_payload=composed_payload,
            adjacent_page_context=[],
            page_dossier=derived_page_dossier,
        )
        compose_seed_plan = _build_cached_generative_seed_plan(
            runtime=runtime,
            page=focus_page,
            user_intent=user_intent,
            compose_payload=composed_payload,
        )

        if isinstance(cached_generative_candidate, dict) and _is_completed_generative_plan_for_experience(cached_generative_candidate):
            if (
                isinstance(compose_derived_plan, dict)
                and _is_completed_generative_plan_for_experience(compose_derived_plan)
                and _should_prefer_compose_derived_plan(
                    cached_plan=cached_generative_candidate,
                    derived_plan=compose_derived_plan,
                )
            ):
                generative_plan_payload = compose_derived_plan
                generative_cache_hit = False
                generative_cache_layer = "derived"
            else:
                generative_plan_payload = cached_generative_candidate
                generative_cache_hit = True
                generative_cache_layer = cached_generative_layer
        elif isinstance(compose_derived_plan, dict):
            generative_plan_payload = _mark_cached_compose_plan_as_provisional_seed(compose_derived_plan)
            generative_cache_hit = False
            generative_cache_layer = "derived_seed"
        elif isinstance(compose_seed_plan, dict) and compose_seed_plan:
            generative_plan_payload = _mark_cached_compose_plan_as_provisional_seed(compose_seed_plan)
            generative_cache_hit = False
            generative_cache_layer = "derived_seed"
        else:
            raise HTTPException(status_code=404, detail="No completed experience manuscript cached for this page")

    generative_plan_signature = _plan_signature(generative_plan_payload)
    if isinstance(generative_plan_payload, Mapping):
        adjacent_page_context = _extract_adjacent_page_context_from_plan_meta(generative_plan_payload)
        page_dossier = _extract_page_dossier_from_plan_meta(generative_plan_payload)

    if not _is_completed_generative_plan_for_experience(generative_plan_payload):
        raise HTTPException(status_code=404, detail="No completed experience manuscript cached for this page")

    experience_cache_hit = False
    experience_cache_layer = "none"
    experience_plan_payload: Optional[Dict[str, Any]] = None
    saw_cached_experience_payload = False
    experience_compose_source_signature = (
        ""
        if generative_cache_layer in {"derived", "derived_seed"} and not generative_cache_hit
        else compose_source_signature
    )
    for experience_cache_key, signature_token in _experience_plan_cache_key_candidates(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=experience_compose_source_signature,
        generative_plan_signature=generative_plan_signature,
        user_intent=user_intent,
        reader_profile=reader_profile,
        focus_section_ids=focus_section_ids,
    ):
        cached_experience, cached_exp_layer = await _experience_plan_cache_get(experience_cache_key)
        if not isinstance(cached_experience, dict):
            continue
        saw_cached_experience_payload = True
        repaired_cached_experience = await _repair_cached_experience_plan_payload(
            runtime=runtime,
            cache_key=experience_cache_key,
            plan_payload=cached_experience,
            generative_plan_payload=generative_plan_payload,
            compose_payload=composed_payload,
            user_intent=user_intent,
            reader_profile=reader_profile,
            focus_section_ids=focus_section_ids,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=signature_token,
        )
        if _is_completed_experience_manuscript(repaired_cached_experience):
            experience_plan_payload = repaired_cached_experience
            experience_cache_hit = True
            experience_cache_layer = cached_exp_layer
            break

    if not experience_cache_hit:
        if not page_dossier and (
            not saw_cached_experience_payload
            or generative_cache_layer in {"derived", "derived_seed"}
        ):
            adjacent_page_context, page_dossier = await _ensure_experience_context()
            generative_plan_payload = _inject_experience_context_into_plan_meta(
                plan_payload=generative_plan_payload,
                adjacent_page_context=adjacent_page_context,
                page_dossier=page_dossier,
            )
        built_experience = runtime.build_experience_plan(
            paper_id=int(paper.id),
            focus_page=focus_page,
            user_intent=user_intent,
            reader_profile=reader_profile or "curious_generalist",
            focus_section_ids=focus_section_ids,
            compose_payload=composed_payload,
            generative_plan=generative_plan_payload,
        )
        experience_plan_payload = (
            built_experience
            if isinstance(built_experience, dict)
            else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
        )
        if not _is_completed_experience_manuscript(experience_plan_payload):
            raise HTTPException(status_code=404, detail="No completed experience manuscript cached for this page")
        if generative_cache_layer == "derived_seed":
            experience_meta = dict(experience_plan_payload.get("meta") or {})
            experience_meta["seed_plan"] = True
            experience_meta["runtime_build_plan_evidence"] = False
            experience_plan_payload["meta"] = experience_meta
        await _persist_completed_experience_plan_variants(
            payload=experience_plan_payload,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            compose_source_signature=experience_compose_source_signature,
            generative_plan_signature=generative_plan_signature,
            user_intent=user_intent,
            reader_profile=reader_profile,
            focus_section_ids=focus_section_ids,
        )
        experience_cache_hit = True
        experience_cache_layer = "derived_seed" if generative_cache_layer == "derived_seed" else "derived"

    adjacent_page_context = _extract_adjacent_page_context_from_plan_meta(generative_plan_payload)
    page_dossier = _extract_page_dossier_from_plan_meta(generative_plan_payload)

    return _build_reader_experience_plan_response_payload(
        focus_page=focus_page,
        plan=experience_plan_payload,
        generative_plan=generative_plan_payload,
        compose_payload=composed_payload,
        enrichment_bundle=dict(composed_payload.get("enrichment_bundle") or {}),
        compose_status=compose_status,
        compose_build_mode=compose_build_mode,
        compose_source_signature=compose_source_signature,
        source_sig_hash=source_sig_hash,
        cache_hit=bool(composed_payload.get("cache_hit")),
        cache_layer=str(composed_payload.get("cache_layer") or "db_latest"),
        generative_plan_cache_hit=generative_cache_hit,
        generative_plan_cache_layer=generative_cache_layer,
        experience_cache_hit=experience_cache_hit,
        experience_cache_layer=experience_cache_layer,
        adjacent_page_context=adjacent_page_context,
        page_dossier=page_dossier,
    )


@router.post(
    "/papers/{paper_id}/experience/plan/cached",
    response_model=ReaderExperiencePlanResponse,
    response_class=JSONResponse,
)
async def get_reader_experience_plan_cached_http(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_plan_cached_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return JSONResponse(content=response_payload)


async def get_reader_experience_plan_cached(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_plan_cached_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return ReaderExperiencePlanResponse.model_validate(response_payload)


async def _build_reader_experience_plan_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    runtime = get_generative_reader_agent_runtime()
    focus_page = max(1, int(payload.focus_page or payload.page or 1))
    normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )

    composed_payload, meta = await compose_service.build_or_get_composed_payload(
        db=db,
        user_id=int(current_user.id),
        paper=paper,
        page=focus_page,
        selected_kb_id=normalized_selected_kb_id,
        force_refresh=bool(payload.force_refresh),
        regenerate=bool(payload.regenerate),
        latency_budget_ms=payload.latency_budget_ms,
        quality_target=payload.quality_target,
        max_iterations=getattr(payload, "max_iterations", None),
        style_intent=payload.style_intent,
        theme_mode=getattr(payload, "theme_mode", None),
        detail_level=getattr(payload, "detail_level", None),
        compare_mode=getattr(payload, "compare_mode", None),
        citation_tldr=getattr(payload, "citation_tldr", None),
        publish_ready_event_enabled=False,
    )
    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or meta.build_mode or "")
    compose_source_signature = str(composed_payload.get("source_signature") or meta.source_signature or "")
    source_sig_hash = str(meta.source_sig_hash or "")
    selected_kb_id = int(normalized_selected_kb_id or 0)
    user_intent = str(payload.user_intent or "").strip()
    reader_profile = str(payload.reader_profile or "").strip()
    focus_section_ids = list(payload.focus_section_ids or [])
    adjacent_page_context = await _build_experience_adjacent_page_context(
        compose_service=compose_service,
        paper=paper,
        focus_page=focus_page,
    )
    page_dossier = _build_experience_page_dossier(
        focus_page=focus_page,
        compose_payload=composed_payload,
        adjacent_page_context=adjacent_page_context,
    )

    generative_cache_hit = False
    generative_cache_layer = "none"
    plan_cache_key = _generative_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=compose_source_signature,
        user_intent=user_intent,
    )
    generative_plan_payload: Optional[Dict[str, Any]] = None
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_plan, cached_layer = await _generative_plan_cache_get(plan_cache_key)
        if isinstance(cached_plan, dict):
            repaired_cached_plan = _repair_sparse_cached_generative_plan(
                runtime=runtime,
                page=focus_page,
                user_intent=user_intent,
                compose_payload=composed_payload,
                plan_payload=cached_plan,
                repair_reason="live_experience_generative_plan_hit",
            )
            if _is_completed_generative_plan_for_experience(repaired_cached_plan):
                generative_plan_payload = repaired_cached_plan
                generative_cache_hit = True
                generative_cache_layer = cached_layer

    if not isinstance(generative_plan_payload, dict):
        tool_registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
            db=db,
            current_user=current_user,
            paper=paper,
            selected_kb_id=normalized_selected_kb_id,
        )
        generated_plan = await runtime.build_plan(
            user_id=int(current_user.id),
            page=focus_page,
            user_intent=user_intent,
            compose_payload=composed_payload,
            tool_registry=tool_registry,
            allowed_tool_names=sorted(list(allowed_tool_names)),
            adjacent_page_context=adjacent_page_context,
            page_dossier=page_dossier,
        )
        generative_plan_payload = (
            generated_plan
            if isinstance(generated_plan, dict)
            else json.loads(json.dumps(generated_plan, ensure_ascii=False, default=str))
        )
    await _persist_completed_generative_plan_variants(
        payload=generative_plan_payload,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=compose_source_signature,
        user_intent=user_intent,
    )
    generative_plan_signature = _plan_signature(generative_plan_payload)

    experience_cache_hit = False
    experience_cache_layer = "none"
    experience_cache_key = _experience_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=compose_source_signature,
        generative_plan_signature=generative_plan_signature,
        user_intent=user_intent,
        reader_profile=reader_profile,
        focus_section_ids=focus_section_ids,
    )
    experience_plan_payload: Dict[str, Any]
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_experience, cached_layer = await _experience_plan_cache_get(experience_cache_key)
        if isinstance(cached_experience, dict):
            repaired_cached_experience = await _repair_cached_experience_plan_payload(
                runtime=runtime,
                cache_key=experience_cache_key,
                plan_payload=cached_experience,
                generative_plan_payload=generative_plan_payload,
                compose_payload=composed_payload,
                user_intent=user_intent,
                reader_profile=reader_profile,
                focus_section_ids=focus_section_ids,
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                page=focus_page,
                compose_source_signature=compose_source_signature,
            )
            if _is_completed_experience_manuscript(repaired_cached_experience):
                experience_plan_payload = repaired_cached_experience
                experience_cache_hit = True
                experience_cache_layer = cached_layer

    if not experience_cache_hit:
        generative_plan_for_experience = generative_plan_payload
        if not _is_completed_generative_plan_for_experience(generative_plan_payload):
            generative_plan_for_experience = _promote_provisional_plan_for_experience_build(generative_plan_payload)
        built_experience = runtime.build_experience_plan(
            paper_id=int(paper.id),
            focus_page=focus_page,
            user_intent=user_intent,
            reader_profile=reader_profile or "curious_generalist",
            focus_section_ids=focus_section_ids,
            compose_payload=composed_payload,
            generative_plan=generative_plan_for_experience,
        )
        experience_plan_payload = (
            built_experience
            if isinstance(built_experience, dict)
            else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
        )
        if not _is_completed_experience_manuscript(experience_plan_payload):
            raise HTTPException(status_code=409, detail="completed_experience_manuscript_not_ready")
    await _persist_completed_experience_plan_variants(
        payload=experience_plan_payload,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        compose_source_signature=compose_source_signature,
        generative_plan_signature=generative_plan_signature,
        user_intent=user_intent,
        reader_profile=reader_profile,
        focus_section_ids=focus_section_ids,
    )

    return _build_reader_experience_plan_response_payload(
        focus_page=focus_page,
        plan=experience_plan_payload,
        generative_plan=generative_plan_payload,
        compose_payload=composed_payload,
        enrichment_bundle=dict(composed_payload.get("enrichment_bundle") or {}),
        compose_status=compose_status,
        compose_build_mode=compose_build_mode,
        compose_source_signature=compose_source_signature,
        source_sig_hash=source_sig_hash,
        cache_hit=bool(meta.cache_hit),
        cache_layer=str(meta.cache_layer or "none"),
        generative_plan_cache_hit=generative_cache_hit,
        generative_plan_cache_layer=generative_cache_layer,
        experience_cache_hit=experience_cache_hit,
        experience_cache_layer=experience_cache_layer,
        adjacent_page_context=adjacent_page_context,
        page_dossier=page_dossier,
    )


def _build_reader_experience_v2_response_payload(
    *,
    focus_page: int,
    status: str,
    artifact: Optional[Mapping[str, Any]],
    compose_payload: Mapping[str, Any],
    compose_status: str,
    compose_build_mode: str,
    compose_source_signature: str,
    source_sig_hash: str,
    artifact_cache_hit: bool,
    artifact_cache_layer: str,
    session_cache_hit: bool,
    session_cache_layer: str,
    session_payload: Optional[Mapping[str, Any]] = None,
    failure_detail: str = "",
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return ReaderExperienceV2Response(
        focus_page=int(focus_page),
        status=str(status or "generating").strip() or "generating",
        artifact=_jsonable_dict(artifact) if isinstance(artifact, Mapping) else None,
        compose_payload=_jsonable_dict(compose_payload),
        compose_status="fallback" if str(compose_status or "").strip() == "fallback" else "done",
        compose_build_mode=str(compose_build_mode or "").strip(),
        compose_source_signature=str(compose_source_signature or "").strip(),
        source_sig_hash=str(source_sig_hash or "").strip(),
        artifact_cache_hit=bool(artifact_cache_hit),
        artifact_cache_layer=str(artifact_cache_layer or "none").strip() or "none",
        session_cache_hit=bool(session_cache_hit),
        session_cache_layer=str(session_cache_layer or "none").strip() or "none",
        session_id=str(_jsonable_dict(session_payload or {}).get("session_id") or "").strip(),
        session_status=str(_jsonable_dict(session_payload or {}).get("status") or "").strip(),
        failure_detail=str(failure_detail or "").strip(),
        meta=_jsonable_dict(meta or {}),
    ).model_dump(mode="json")


def _build_reader_workbench_v2_response_payload(
    *,
    focus_page: int,
    status: str,
    compose_payload: Mapping[str, Any],
    compose_status: str,
    compose_build_mode: str,
    compose_source_signature: str,
    source_sig_hash: str,
    reading_dossier: Optional[Mapping[str, Any]],
    session_payload: Optional[Mapping[str, Any]],
    artifact: Optional[Mapping[str, Any]],
    artifact_validation: Optional[Mapping[str, Any]],
    artifact_cache_hit: bool,
    artifact_cache_layer: str,
    session_cache_hit: bool,
    session_cache_layer: str,
    failure_detail: str = "",
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return ReaderWorkbenchV2Response(
        focus_page=int(focus_page),
        status=str(status or "empty").strip() or "empty",
        compose_payload=_jsonable_dict(compose_payload),
        compose_status="fallback" if str(compose_status or "").strip() == "fallback" else "done",
        compose_build_mode=str(compose_build_mode or "").strip(),
        compose_source_signature=str(compose_source_signature or "").strip(),
        source_sig_hash=str(source_sig_hash or "").strip(),
        reading_dossier=_jsonable_dict(reading_dossier) if isinstance(reading_dossier, Mapping) else None,
        session=_jsonable_dict(session_payload) if isinstance(session_payload, Mapping) else None,
        artifact=_jsonable_dict(artifact) if isinstance(artifact, Mapping) else None,
        artifact_validation=_jsonable_dict(artifact_validation or {}),
        artifact_cache_hit=bool(artifact_cache_hit),
        artifact_cache_layer=str(artifact_cache_layer or "none").strip() or "none",
        session_cache_hit=bool(session_cache_hit),
        session_cache_layer=str(session_cache_layer or "none").strip() or "none",
        failure_detail=str(failure_detail or "").strip(),
        meta=_jsonable_dict(meta or {}),
    ).model_dump(mode="json")


async def _prepare_reader_experience_v2_runtime(
    *,
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession,
    current_user: User,
) -> Dict[str, Any]:
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    focus_page = max(1, int(payload.focus_page or payload.page or 1))
    normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )
    composed_payload, meta = await compose_service.build_or_get_composed_payload(
        db=db,
        user_id=int(current_user.id),
        paper=paper,
        page=focus_page,
        selected_kb_id=normalized_selected_kb_id,
        force_refresh=bool(payload.force_refresh),
        regenerate=bool(payload.regenerate),
        latency_budget_ms=payload.latency_budget_ms,
        quality_target=payload.quality_target,
        max_iterations=getattr(payload, "max_iterations", None),
        style_intent=payload.style_intent,
        theme_mode=getattr(payload, "theme_mode", None),
        detail_level=getattr(payload, "detail_level", None),
        compare_mode=getattr(payload, "compare_mode", None),
        citation_tldr=getattr(payload, "citation_tldr", None),
        publish_ready_event_enabled=False,
    )
    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or meta.build_mode or "")
    compose_source_signature = str(composed_payload.get("source_signature") or meta.source_signature or "")
    source_sig_hash = str(meta.source_sig_hash or "")
    reader_profile = str(payload.reader_profile or "").strip() or "curious_generalist"
    user_intent = str(payload.user_intent or "").strip()
    selected_kb_id = int(normalized_selected_kb_id or 0)

    try:
        adjacent_page_context = await _build_experience_adjacent_page_structured_context_v2(
            compose_service=compose_service,
            paper=paper,
            focus_page=focus_page,
            current_user=current_user,
        )
    except ValueError as exc:
        logger.warning(
            f"[Literature Experience] adjacent structured context degraded to empty paper={paper_id} "
            f"focus_page={focus_page}: {exc}"
        )
        adjacent_page_context = []

    try:
        reading_dossier = _build_reading_dossier_v2(
            focus_page=focus_page,
            reader_profile=reader_profile,
            compose_payload=composed_payload,
            adjacent_page_context=adjacent_page_context,
            source_sig_hash=source_sig_hash,
        )
    except ValueError as exc:
        logger.warning(
            f"[Literature Experience] reading dossier v2 build failed paper={paper_id} focus_page={focus_page}: {exc}"
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dossier_meta = _jsonable_dict(reading_dossier.get("meta") or {})
    if bool(dossier_meta.get("current_page_grounding_degraded")):
        raise HTTPException(
            status_code=409,
            detail=(
                "current-page grounding unavailable for v2 route: "
                + str(dossier_meta.get("current_page_grounding_degraded_reason") or "missing_page_grounding_v1")
            ),
        )

    dossier_signature = _reading_dossier_v2_signature(reading_dossier)
    cache_signature = _experience_v2_cache_signature(
        compose_source_signature=compose_source_signature,
        dossier_signature=dossier_signature,
    )
    session_cache_key = _experience_session_v2_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        dossier_signature=cache_signature,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    artifact_cache_key = _page_artifact_v2_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        dossier_signature=cache_signature,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    cached_session, session_cache_layer = await _experience_session_v2_cache_get(session_cache_key)
    cached_artifact, artifact_cache_layer = await _page_artifact_v2_cache_get(artifact_cache_key)
    if not isinstance(cached_session, Mapping):
        legacy_session, legacy_expires_at, legacy_session_key = await _experience_v2_cache_db_get_by_compose_signature(
            plan_kind=EXPERIENCE_SESSION_V2_CACHE_KIND,
            namespace=EXPERIENCE_SESSION_V2_CACHE_NAMESPACE,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            compose_source_signature=compose_source_signature,
            user_intent=user_intent,
            reader_profile=reader_profile,
        )
        if isinstance(legacy_session, Mapping):
            await _experience_session_v2_cache_set(
                session_cache_key,
                _jsonable_dict(legacy_session),
                ttl_seconds=_plan_cache_ttl_seconds_from_expires_at(
                    legacy_expires_at,
                    default_ttl_seconds=EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS,
                ),
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                page=focus_page,
                compose_source_signature=compose_source_signature,
            )
            cached_session = _jsonable_dict(legacy_session)
            session_cache_layer = (
                f"db_compose_signature:{legacy_session_key}"
                if str(legacy_session_key or "").strip()
                else "db_compose_signature"
            )
    if not isinstance(cached_artifact, Mapping):
        legacy_artifact, legacy_expires_at, legacy_artifact_key = await _experience_v2_cache_db_get_by_compose_signature(
            plan_kind=PAGE_ARTIFACT_V2_CACHE_KIND,
            namespace=PAGE_ARTIFACT_V2_CACHE_NAMESPACE,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            compose_source_signature=compose_source_signature,
            user_intent=user_intent,
            reader_profile=reader_profile,
        )
        if isinstance(legacy_artifact, Mapping):
            await _page_artifact_v2_cache_set(
                artifact_cache_key,
                _jsonable_dict(legacy_artifact),
                ttl_seconds=_plan_cache_ttl_seconds_from_expires_at(
                    legacy_expires_at,
                    default_ttl_seconds=PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
                ),
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                page=focus_page,
                compose_source_signature=compose_source_signature,
            )
            cached_artifact = _jsonable_dict(legacy_artifact)
            artifact_cache_layer = (
                f"db_compose_signature:{legacy_artifact_key}"
                if str(legacy_artifact_key or "").strip()
                else "db_compose_signature"
            )
    if not isinstance(cached_artifact, Mapping):
        stable_artifact, stable_expires_at, stable_artifact_key = await _experience_v2_cache_db_get_latest_stable(
            plan_kind=PAGE_ARTIFACT_V2_CACHE_KIND,
            namespace=PAGE_ARTIFACT_V2_CACHE_NAMESPACE,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            user_intent=user_intent,
            reader_profile=reader_profile,
        )
        if isinstance(stable_artifact, Mapping):
            await _page_artifact_v2_cache_set(
                artifact_cache_key,
                _jsonable_dict(stable_artifact),
                ttl_seconds=_plan_cache_ttl_seconds_from_expires_at(
                    stable_expires_at,
                    default_ttl_seconds=PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
                ),
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                page=focus_page,
                compose_source_signature=compose_source_signature,
            )
            cached_artifact = _jsonable_dict(stable_artifact)
            artifact_cache_layer = (
                f"db_stable:{stable_artifact_key}"
                if str(stable_artifact_key or "").strip()
                else "db_stable"
            )
    artifact_validation = {}
    if isinstance(cached_artifact, Mapping):
        artifact_validation = _validate_page_artifact_v2_contract(cached_artifact)
        if not artifact_validation.get("valid"):
            raise HTTPException(
                status_code=409,
                detail="completed page_artifact_v2 not available: cached artifact failed validation",
            )
        await _backfill_reader_experience_v2_fast_caches(
            session_fast_key=_experience_v2_fast_cache_key(
                namespace=EXPERIENCE_SESSION_V2_FAST_CACHE_NAMESPACE,
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                focus_page=focus_page,
                selected_kb_id=selected_kb_id,
                user_intent=user_intent,
                reader_profile=reader_profile,
            ),
            artifact_fast_key=_experience_v2_fast_cache_key(
                namespace=PAGE_ARTIFACT_V2_FAST_CACHE_NAMESPACE,
                user_id=int(current_user.id),
                paper_id=int(paper.id),
                focus_page=focus_page,
                selected_kb_id=selected_kb_id,
                user_intent=user_intent,
                reader_profile=reader_profile,
            ),
            session_payload=cached_session if isinstance(cached_session, Mapping) else None,
            artifact_payload=cached_artifact,
        )
    return {
        "paper": paper,
        "focus_page": focus_page,
        "selected_kb_id": selected_kb_id,
        "reader_profile": reader_profile,
        "user_intent": user_intent,
        "compose_payload": composed_payload,
        "compose_status": compose_status,
        "compose_build_mode": compose_build_mode,
        "compose_source_signature": compose_source_signature,
        "source_sig_hash": source_sig_hash,
        "reading_dossier": reading_dossier,
        "dossier_signature": dossier_signature,
        "session_cache_key": session_cache_key,
        "artifact_cache_key": artifact_cache_key,
        "cached_session": _jsonable_dict(cached_session) if isinstance(cached_session, Mapping) else None,
        "session_cache_hit": isinstance(cached_session, Mapping),
        "session_cache_layer": session_cache_layer,
        "cached_artifact": _jsonable_dict(cached_artifact) if isinstance(cached_artifact, Mapping) else None,
        "artifact_cache_hit": isinstance(cached_artifact, Mapping),
        "artifact_cache_layer": artifact_cache_layer,
        "artifact_validation": artifact_validation,
    }


async def _run_reader_experience_v2_artifact_drafting_loop(
    *,
    db: AsyncSession,
    current_user: User,
    paper: Paper,
    selected_kb_id: int,
    compose_payload: Mapping[str, Any],
    reading_dossier: Mapping[str, Any],
    session_payload: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    session_payload = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload)).model_dump(mode="json")
    session = ExperienceSessionV2.model_validate(session_payload)
    resource_bundle = _build_reader_v2_seed_resource_bundle(
        paper=paper,
        compose_payload=compose_payload,
        narrative_brief=_find_latest_experience_session_v2_narrative_brief(session_payload) or {},
    )
    latest_artifact_draft = await _generate_experience_session_v2_artifact_draft(
        reading_dossier=reading_dossier,
        session_payload=session_payload,
        resource_bundle=resource_bundle,
        include_full_dossier=True,
    )
    if (
        not list((_jsonable_dict(latest_artifact_draft).get("resource_requests") or []))
        and not _resource_bundle_has_nonseed_public_web_entries(resource_bundle)
        and bool(
            _jsonable_dict(_jsonable_dict(resource_bundle.get("meta") or {}).get("resource_request_affordance") or {}).get("must_use_tools")
        )
    ):
        forced_requests = await _generate_experience_v2_mandatory_resource_requests(
            paper=paper,
            reading_dossier=reading_dossier,
            session_payload=session_payload,
            resource_bundle=resource_bundle,
        )
        latest_artifact_draft = _jsonable_dict(latest_artifact_draft)
        latest_artifact_draft["resource_requests"] = forced_requests
        forced_meta = _jsonable_dict(latest_artifact_draft.get("meta") or {})
        forced_meta["forced_public_web_resource_round"] = True
        latest_artifact_draft["meta"] = forced_meta
    retrieval_rounds = 0
    max_retrieval_rounds = min(
        _EXPERIENCE_V2_ARTIFACT_DRAFT_MAX_RETRIEVAL_ROUNDS,
        max(1, int(session.runtime_budget.max_iterations) - len(list(session.iterations or []))),
    )

    while list((_jsonable_dict(latest_artifact_draft).get("resource_requests") or [])):
        retrieval_rounds += 1
        if retrieval_rounds > max_retrieval_rounds:
            if _experience_v2_artifact_draft_can_finalize_with_current_resources(
                latest_artifact_draft,
                resource_bundle,
            ):
                latest_artifact_draft = _jsonable_dict(latest_artifact_draft)
                dropped_requests = len(list(latest_artifact_draft.get("resource_requests") or []))
                latest_artifact_draft["resource_requests"] = []
                draft_meta = _jsonable_dict(latest_artifact_draft.get("meta") or {})
                draft_meta["resource_request_budget_exhausted"] = True
                draft_meta["dropped_pending_resource_requests"] = dropped_requests
                latest_artifact_draft["meta"] = draft_meta
                bundle_meta = _jsonable_dict(resource_bundle.get("meta") or {})
                bundle_meta["resource_request_budget_exhausted"] = True
                bundle_meta["dropped_pending_resource_requests"] = dropped_requests
                resource_bundle = {**_jsonable_dict(resource_bundle), "meta": bundle_meta}
                break
            raise ValueError("artifact draft generation blocked by retrieval round budget exhausted")
        requested_resources = [
            ExperienceSessionV2ArtifactDraftResourceRequest.model_validate(_jsonable_dict(item)).model_dump(mode="json")
            for item in list(_jsonable_dict(latest_artifact_draft).get("resource_requests") or [])
            if isinstance(item, Mapping)
        ]
        if len(requested_resources) > 2:
            raise ValueError("artifact draft generation blocked by retrieval request budget exceeded")
        current_session = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload))
        used_tool_rounds = sum(len(list(iteration.tool_trace or [])) for iteration in list(current_session.iterations or []))
        remaining_tool_rounds = int(current_session.runtime_budget.max_tool_rounds) - int(used_tool_rounds)
        if remaining_tool_rounds <= 0 or len(requested_resources) > remaining_tool_rounds:
            raise ValueError("artifact draft generation blocked by retrieval budget exhausted")
        resource_bundle, tool_trace = await _execute_experience_v2_artifact_resource_requests(
            db=db,
            current_user=current_user,
            paper=paper,
            selected_kb_id=selected_kb_id,
            requests=requested_resources,
            resource_bundle=resource_bundle,
        )
        latest_artifact_draft = await _generate_experience_session_v2_artifact_draft(
            reading_dossier=reading_dossier,
            session_payload=session_payload,
            resource_bundle=resource_bundle,
            previous_draft=latest_artifact_draft,
            include_full_dossier=False,
        )
        session_payload = _append_experience_session_v2_iteration(
            session_payload,
            phase="revise",
            delta_packet={
                "working_state": {
                    "resource_bundle": resource_bundle,
                    "artifact_draft_summary": _summarize_experience_v2_artifact_draft(latest_artifact_draft),
                    "pending_resource_requests": [],
                    "draft_round": int(retrieval_rounds + 1),
                },
            },
            state_handle=f"iter:{len(list((_jsonable_dict(session_payload).get('iterations') or []))) + 1}:artifact-draft-r{retrieval_rounds}",
            tool_trace=tool_trace,
            meta={
                "artifact_draft_ready": True,
                "artifact_draft": latest_artifact_draft,
                "draft_round": int(retrieval_rounds + 1),
            },
        )

    if retrieval_rounds == 0:
        session_payload = _append_experience_session_v2_iteration(
            session_payload,
            phase="revise",
            delta_packet={
                "working_state": {
                    "resource_bundle": resource_bundle,
                    "artifact_draft_summary": _summarize_experience_v2_artifact_draft(latest_artifact_draft),
                    "pending_resource_requests": [],
                    "draft_round": 1,
                },
            },
            state_handle="iter:2:artifact-draft",
            tool_trace=[],
            meta={
                "artifact_draft_ready": True,
                "artifact_draft": latest_artifact_draft,
                "draft_round": 1,
            },
        )

    session_payload = ExperienceSessionV2.model_validate(_jsonable_dict(session_payload)).model_dump(mode="json")
    session_meta = _jsonable_dict(session_payload.get("meta") or {})
    session_meta["latest_artifact_draft"] = latest_artifact_draft
    session_meta["latest_resource_bundle"] = resource_bundle
    session_payload["meta"] = session_meta

    resource_bundle, authored_plan = _build_page_artifact_v2_authored_plan_from_session(
        paper=paper,
        compose_payload=compose_payload,
        reading_dossier=reading_dossier,
        session_payload=session_payload,
        artifact_draft=latest_artifact_draft,
        resource_bundle=resource_bundle,
    )
    return session_payload, resource_bundle, latest_artifact_draft, authored_plan


async def _run_reader_experience_v2_build(
    *,
    runtime_state: Mapping[str, Any],
    db: AsyncSession,
    current_user: User,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    paper = runtime_state["paper"]
    focus_page = int(runtime_state["focus_page"])
    selected_kb_id = int(runtime_state["selected_kb_id"])
    compose_payload = _jsonable_dict(runtime_state["compose_payload"])
    reading_dossier = _jsonable_dict(runtime_state["reading_dossier"])
    compose_source_signature = str(runtime_state["compose_source_signature"] or "").strip()
    session_cache_key = str(runtime_state["session_cache_key"] or "").strip()
    artifact_cache_key = str(runtime_state["artifact_cache_key"] or "").strip()
    reader_profile = str(runtime_state["reader_profile"] or "").strip()
    user_intent = str(runtime_state.get("user_intent") or "").strip()
    session_fast_key = _experience_v2_fast_cache_key(
        namespace=EXPERIENCE_SESSION_V2_FAST_CACHE_NAMESPACE,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    artifact_fast_key = _experience_v2_fast_cache_key(
        namespace=PAGE_ARTIFACT_V2_FAST_CACHE_NAMESPACE,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    compose_source_signature = str(compose_source_signature or "").strip()
    session_payload = _jsonable_dict(runtime_state.get("cached_session") or {})
    if session_payload and str(session_payload.get("status") or "").strip() == "failed":
        session_payload = {}
    if not session_payload:
        try:
            narrative_brief = await _generate_experience_session_v2_narrative_brief(
                reading_dossier=reading_dossier,
                focus_page=focus_page,
                reader_profile=reader_profile,
                user_intent=user_intent,
            )
            session_payload = _build_experience_session_v2(
                cache_key=session_cache_key,
                reading_dossier=reading_dossier,
                focus_page=focus_page,
                reader_profile=reader_profile,
                max_iterations=4,
                max_tool_rounds=4,
                narrative_brief=narrative_brief,
                meta={"route_kind": "experience_v2_live"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _experience_session_v2_cache_set(
            session_cache_key,
            session_payload,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=compose_source_signature,
        )

    try:
        session_payload, resource_bundle, artifact_draft, authored_plan = await _run_reader_experience_v2_artifact_drafting_loop(
            db=db,
            current_user=current_user,
            paper=paper,
            selected_kb_id=selected_kb_id,
            compose_payload=compose_payload,
            reading_dossier=reading_dossier,
            session_payload=session_payload,
        )
        artifact_payload = _build_page_artifact_v2_from_dossier(
            reading_dossier=reading_dossier,
            authored_plan=authored_plan,
            dossier_signature=str(runtime_state["dossier_signature"] or "").strip(),
            session_id=str(session_payload.get("session_id") or "").strip(),
        )
        artifact_validation = _validate_page_artifact_v2_contract(artifact_payload)
        if not artifact_validation.get("valid") or not artifact_validation.get("renderable"):
            raise ValueError(
                "completed page_artifact_v2 not available: "
                + "; ".join([str(item) for item in list(artifact_validation.get("errors") or [])[:3]])
            )
        await _page_artifact_v2_cache_set(
            artifact_cache_key,
            artifact_payload,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=compose_source_signature,
        )
        session_payload = _complete_experience_session_v2(
            session_payload,
            artifact_ref=artifact_cache_key,
            artifact_payload=artifact_payload,
            meta={
                "latest_resource_bundle": resource_bundle,
                "latest_artifact_draft": artifact_draft,
                "latest_presentation_rationale": _jsonable_dict(authored_plan.get("meta") or {}).get("presentation_rationale"),
            },
        )
        await _experience_session_v2_cache_set(
            session_cache_key,
            session_payload,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=compose_source_signature,
        )
        await _backfill_reader_experience_v2_fast_caches(
            session_fast_key=session_fast_key,
            artifact_fast_key=artifact_fast_key,
            session_payload=session_payload,
            artifact_payload=artifact_payload,
        )
        return session_payload, artifact_payload, artifact_validation, resource_bundle
    except HTTPException:
        failed_session = _mark_experience_session_v2_failed(
            session_payload,
            stop_reason="completed page_artifact_v2 not available",
            resume_state_handle=str(_jsonable_dict(session_payload.get("resume") or {}).get("resume_state_handle") or "iter:1:bootstrap"),
        )
        await _experience_session_v2_cache_set(
            session_cache_key,
            failed_session,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=compose_source_signature,
        )
        raise
    except Exception as exc:
        failed_session = _mark_experience_session_v2_failed(
            session_payload,
            stop_reason=str(exc),
            resume_state_handle="iter:2:artifact-draft",
        )
        await _experience_session_v2_cache_set(
            session_cache_key,
            failed_session,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            page=focus_page,
            compose_source_signature=compose_source_signature,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _backfill_reader_experience_v2_fast_caches(
    *,
    session_fast_key: str,
    artifact_fast_key: str,
    session_payload: Optional[Mapping[str, Any]] = None,
    artifact_payload: Optional[Mapping[str, Any]] = None,
    session_ttl_seconds: int = EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS,
    artifact_ttl_seconds: int = PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
) -> None:
    if session_fast_key and isinstance(session_payload, Mapping):
        await _experience_session_v2_fast_cache_set(
            session_fast_key,
            _jsonable_dict(session_payload),
            ttl_seconds=session_ttl_seconds,
        )
    if artifact_fast_key and isinstance(artifact_payload, Mapping):
        await _page_artifact_v2_fast_cache_set(
            artifact_fast_key,
            _jsonable_dict(artifact_payload),
            ttl_seconds=artifact_ttl_seconds,
        )


def _build_reader_experience_v2_fast_cache_layer(
    *,
    layer: str,
    db_cache_key: Optional[str] = None,
) -> str:
    normalized = str(layer or "").strip().lower()
    if normalized in {"memory", "redis"}:
        return f"fast_{normalized}"
    if normalized == "db":
        return f"db_stable_fast:{db_cache_key}" if str(db_cache_key or "").strip() else "db_stable_fast"
    return normalized or "none"


async def _build_reader_experience_v2_cached_fast_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession,
    current_user: User,
) -> Optional[Dict[str, Any]]:
    if bool(payload.force_refresh or payload.regenerate):
        return None

    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    focus_page = max(1, int(payload.focus_page or payload.page or 1))
    normalized_selected_kb_id = await _normalize_reader_selected_kb_id(
        db=db,
        current_user=current_user,
        selected_kb_id=payload.selected_kb_id,
    )
    selected_kb_id = int(normalized_selected_kb_id or 0)
    reader_profile = str(payload.reader_profile or "").strip() or "curious_generalist"
    user_intent = str(payload.user_intent or "").strip()

    artifact_fast_key = _experience_v2_fast_cache_key(
        namespace=PAGE_ARTIFACT_V2_FAST_CACHE_NAMESPACE,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )
    session_fast_key = _experience_v2_fast_cache_key(
        namespace=EXPERIENCE_SESSION_V2_FAST_CACHE_NAMESPACE,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=selected_kb_id,
        user_intent=user_intent,
        reader_profile=reader_profile,
    )

    cached_artifact, artifact_layer = await _page_artifact_v2_fast_cache_get(artifact_fast_key)
    stable_artifact_key: Optional[str] = None
    artifact_expires_at: Optional[datetime] = None
    if not isinstance(cached_artifact, Mapping):
        stable_artifact, artifact_expires_at, stable_artifact_key = await _experience_v2_cache_db_get_latest_stable(
            plan_kind=PAGE_ARTIFACT_V2_CACHE_KIND,
            namespace=PAGE_ARTIFACT_V2_CACHE_NAMESPACE,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            user_intent=user_intent,
            reader_profile=reader_profile,
        )
        if isinstance(stable_artifact, Mapping):
            cached_artifact = _jsonable_dict(stable_artifact)
            artifact_layer = "db"
            await _page_artifact_v2_fast_cache_set(
                artifact_fast_key,
                cached_artifact,
                ttl_seconds=_plan_cache_ttl_seconds_from_expires_at(
                    artifact_expires_at,
                    default_ttl_seconds=PAGE_ARTIFACT_V2_CACHE_TTL_SECONDS,
                ),
            )

    artifact_validation: Dict[str, Any] = {}
    if isinstance(cached_artifact, Mapping):
        artifact_validation = _validate_page_artifact_v2_contract(cached_artifact)
        if not artifact_validation.get("valid") or not artifact_validation.get("renderable"):
            cached_artifact = None
            artifact_layer = "none"
            artifact_validation = {}

    cached_session, session_layer = await _experience_session_v2_fast_cache_get(session_fast_key)
    stable_session_key: Optional[str] = None
    session_expires_at: Optional[datetime] = None
    if not isinstance(cached_session, Mapping):
        stable_session, session_expires_at, stable_session_key = await _experience_v2_cache_db_get_latest_stable(
            plan_kind=EXPERIENCE_SESSION_V2_CACHE_KIND,
            namespace=EXPERIENCE_SESSION_V2_CACHE_NAMESPACE,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            focus_page=focus_page,
            selected_kb_id=selected_kb_id,
            user_intent=user_intent,
            reader_profile=reader_profile,
        )
        if isinstance(stable_session, Mapping):
            cached_session = _jsonable_dict(stable_session)
            session_layer = "db"
            await _experience_session_v2_fast_cache_set(
                session_fast_key,
                cached_session,
                ttl_seconds=_plan_cache_ttl_seconds_from_expires_at(
                    session_expires_at,
                    default_ttl_seconds=EXPERIENCE_SESSION_V2_CACHE_TTL_SECONDS,
                ),
            )

    if isinstance(cached_artifact, Mapping):
        return _build_reader_experience_v2_response_payload(
            focus_page=focus_page,
            status="ready",
            artifact=cached_artifact,
            compose_payload={},
            compose_status="done",
            compose_build_mode="cached_fast_path",
            compose_source_signature="",
            source_sig_hash="",
            artifact_cache_hit=True,
            artifact_cache_layer=_build_reader_experience_v2_fast_cache_layer(
                layer=artifact_layer,
                db_cache_key=stable_artifact_key,
            ),
            session_cache_hit=isinstance(cached_session, Mapping),
            session_cache_layer=_build_reader_experience_v2_fast_cache_layer(
                layer=session_layer,
                db_cache_key=stable_session_key,
            ),
            session_payload=cached_session if isinstance(cached_session, Mapping) else None,
            meta={
                "artifact_validation": artifact_validation,
                "cache_mode": "fast_path",
                "compose_payload_deferred": True,
            },
        )

    if isinstance(cached_session, Mapping) and str(cached_session.get("status") or "").strip() == "failed":
        return _build_reader_experience_v2_response_payload(
            focus_page=focus_page,
            status="failed",
            artifact=None,
            compose_payload={},
            compose_status="done",
            compose_build_mode="cached_fast_path",
            compose_source_signature="",
            source_sig_hash="",
            artifact_cache_hit=False,
            artifact_cache_layer="none",
            session_cache_hit=True,
            session_cache_layer=_build_reader_experience_v2_fast_cache_layer(
                layer=session_layer,
                db_cache_key=stable_session_key,
            ),
            session_payload=cached_session,
            failure_detail=str(cached_session.get("stop_reason") or "completed page_artifact_v2 not available").strip(),
            meta={"cache_mode": "fast_path"},
        )

    return None


async def _build_reader_experience_v2_cached_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession,
    current_user: User,
) -> Dict[str, Any]:
    fast_response = await _build_reader_experience_v2_cached_fast_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    if isinstance(fast_response, dict):
        return fast_response

    runtime_state = await _prepare_reader_experience_v2_runtime(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    cached_artifact = runtime_state["cached_artifact"]
    cached_session = runtime_state["cached_session"]
    if isinstance(cached_artifact, Mapping):
        return _build_reader_experience_v2_response_payload(
            focus_page=runtime_state["focus_page"],
            status="ready",
            artifact=cached_artifact,
            compose_payload=runtime_state["compose_payload"],
            compose_status=runtime_state["compose_status"],
            compose_build_mode=runtime_state["compose_build_mode"],
            compose_source_signature=runtime_state["compose_source_signature"],
            source_sig_hash=runtime_state["source_sig_hash"],
            artifact_cache_hit=runtime_state["artifact_cache_hit"],
            artifact_cache_layer=runtime_state["artifact_cache_layer"],
            session_cache_hit=runtime_state["session_cache_hit"],
            session_cache_layer=runtime_state["session_cache_layer"],
            session_payload=cached_session,
            meta={"artifact_validation": runtime_state["artifact_validation"]},
        )
    if isinstance(cached_session, Mapping) and str(cached_session.get("status") or "").strip() == "failed":
        return _build_reader_experience_v2_response_payload(
            focus_page=runtime_state["focus_page"],
            status="failed",
            artifact=None,
            compose_payload=runtime_state["compose_payload"],
            compose_status=runtime_state["compose_status"],
            compose_build_mode=runtime_state["compose_build_mode"],
            compose_source_signature=runtime_state["compose_source_signature"],
            source_sig_hash=runtime_state["source_sig_hash"],
            artifact_cache_hit=False,
            artifact_cache_layer="none",
            session_cache_hit=runtime_state["session_cache_hit"],
            session_cache_layer=runtime_state["session_cache_layer"],
            session_payload=cached_session,
            failure_detail=str(cached_session.get("stop_reason") or "completed page_artifact_v2 not available").strip(),
            meta={},
        )
    return _build_reader_experience_v2_response_payload(
        focus_page=runtime_state["focus_page"],
        status="generating",
        artifact=None,
        compose_payload=runtime_state["compose_payload"],
        compose_status=runtime_state["compose_status"],
        compose_build_mode=runtime_state["compose_build_mode"],
        compose_source_signature=runtime_state["compose_source_signature"],
        source_sig_hash=runtime_state["source_sig_hash"],
        artifact_cache_hit=False,
        artifact_cache_layer="none",
        session_cache_hit=runtime_state["session_cache_hit"],
        session_cache_layer=runtime_state["session_cache_layer"],
        session_payload=cached_session,
        failure_detail="completed page_artifact_v2 not available",
        meta={},
    )


async def _build_reader_experience_v2_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession,
    current_user: User,
) -> Dict[str, Any]:
    runtime_state = await _prepare_reader_experience_v2_runtime(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    if isinstance(runtime_state["cached_artifact"], Mapping) and not (payload.force_refresh or payload.regenerate):
        return _build_reader_experience_v2_response_payload(
            focus_page=runtime_state["focus_page"],
            status="ready",
            artifact=runtime_state["cached_artifact"],
            compose_payload=runtime_state["compose_payload"],
            compose_status=runtime_state["compose_status"],
            compose_build_mode=runtime_state["compose_build_mode"],
            compose_source_signature=runtime_state["compose_source_signature"],
            source_sig_hash=runtime_state["source_sig_hash"],
            artifact_cache_hit=runtime_state["artifact_cache_hit"],
            artifact_cache_layer=runtime_state["artifact_cache_layer"],
            session_cache_hit=runtime_state["session_cache_hit"],
            session_cache_layer=runtime_state["session_cache_layer"],
            session_payload=runtime_state["cached_session"],
            meta={"artifact_validation": runtime_state["artifact_validation"]},
        )

    session_payload, artifact_payload, artifact_validation, resource_bundle = await _run_reader_experience_v2_build(
        runtime_state=runtime_state,
        db=db,
        current_user=current_user,
    )
    return _build_reader_experience_v2_response_payload(
        focus_page=runtime_state["focus_page"],
        status="ready",
        artifact=artifact_payload,
        compose_payload=runtime_state["compose_payload"],
        compose_status=runtime_state["compose_status"],
        compose_build_mode=runtime_state["compose_build_mode"],
        compose_source_signature=runtime_state["compose_source_signature"],
        source_sig_hash=runtime_state["source_sig_hash"],
        artifact_cache_hit=False,
        artifact_cache_layer="built",
        session_cache_hit=runtime_state["session_cache_hit"],
        session_cache_layer=runtime_state["session_cache_layer"],
        session_payload=session_payload,
        meta={
            "artifact_validation": artifact_validation,
            "resource_bundle": resource_bundle,
        },
    )


async def _build_reader_workbench_v2_payload(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession,
    current_user: User,
) -> Dict[str, Any]:
    runtime_state = await _prepare_reader_experience_v2_runtime(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    cached_session = runtime_state["cached_session"]
    cached_artifact = runtime_state["cached_artifact"]
    artifact_validation = runtime_state["artifact_validation"]
    failure_detail = ""
    status = "empty"
    meta: Dict[str, Any] = {}

    if not isinstance(cached_artifact, Mapping) and (
        payload.force_refresh
        or payload.regenerate
        or not isinstance(cached_session, Mapping)
        or str(cached_session.get("status") or "").strip() != "failed"
    ):
        try:
            cached_session, cached_artifact, artifact_validation, resource_bundle = await _run_reader_experience_v2_build(
                runtime_state=runtime_state,
                db=db,
                current_user=current_user,
            )
            meta["resource_bundle"] = resource_bundle
        except HTTPException as exc:
            failure_detail = str(exc.detail or "").strip()
            refreshed_session, refreshed_layer = await _experience_session_v2_cache_get(str(runtime_state["session_cache_key"] or "").strip())
            cached_session = _jsonable_dict(refreshed_session) if isinstance(refreshed_session, Mapping) else cached_session
            runtime_state["session_cache_layer"] = refreshed_layer
            runtime_state["session_cache_hit"] = isinstance(refreshed_session, Mapping)

    if isinstance(cached_artifact, Mapping):
        status = "ready"
    elif isinstance(cached_session, Mapping):
        session_status = str(cached_session.get("status") or "").strip()
        status = "failed" if session_status == "failed" else "running"
        if session_status == "failed" and not failure_detail:
            failure_detail = str(cached_session.get("stop_reason") or "completed page_artifact_v2 not available").strip()

    if isinstance(cached_session, Mapping):
        session_meta = _jsonable_dict(cached_session.get("meta") or {})
        presentation_rationale = session_meta.get("latest_presentation_rationale")
        if presentation_rationale:
            meta["presentation_rationale"] = presentation_rationale

    return _build_reader_workbench_v2_response_payload(
        focus_page=runtime_state["focus_page"],
        status=status,
        compose_payload=runtime_state["compose_payload"],
        compose_status=runtime_state["compose_status"],
        compose_build_mode=runtime_state["compose_build_mode"],
        compose_source_signature=runtime_state["compose_source_signature"],
        source_sig_hash=runtime_state["source_sig_hash"],
        reading_dossier=runtime_state["reading_dossier"],
        session_payload=cached_session,
        artifact=cached_artifact,
        artifact_validation=artifact_validation,
        artifact_cache_hit=runtime_state["artifact_cache_hit"],
        artifact_cache_layer=runtime_state["artifact_cache_layer"],
        session_cache_hit=runtime_state["session_cache_hit"],
        session_cache_layer=runtime_state["session_cache_layer"],
        failure_detail=failure_detail,
        meta=meta,
    )


@router.post(
    "/papers/{paper_id}/experience-v2/cached",
    response_model=ReaderExperienceV2Response,
    response_class=JSONResponse,
)
async def get_reader_experience_v2_cached_http(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_v2_cached_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return JSONResponse(content=response_payload)


@router.post(
    "/papers/{paper_id}/experience-v2",
    response_model=ReaderExperienceV2Response,
    response_class=JSONResponse,
)
async def get_reader_experience_v2_http(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_v2_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return JSONResponse(content=response_payload)


@router.post(
    "/papers/{paper_id}/workbench-v2",
    response_model=ReaderWorkbenchV2Response,
    response_class=JSONResponse,
)
async def get_reader_workbench_v2_http(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_workbench_v2_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return JSONResponse(content=response_payload)


@router.post(
    "/papers/{paper_id}/experience/plan",
    response_model=ReaderExperiencePlanResponse,
    response_class=JSONResponse,
)
async def get_reader_experience_plan_http(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_plan_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return JSONResponse(content=response_payload)


async def get_reader_experience_plan(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response_payload = await _build_reader_experience_plan_payload(
        paper_id=paper_id,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    return ReaderExperiencePlanResponse.model_validate(response_payload)


@router.post(
    "/papers/{paper_id}/reader/composed/cached",
    response_model=ReaderComposeFetchResponse,
)
async def get_reader_composed_page_cached(
    paper_id: int,
    payload: ReaderComposeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    page_num = max(1, int(payload.page))

    composed_payload = await compose_service.get_latest_cached_payload_only(
        db=db,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=page_num,
        pipeline_version_override=getattr(payload, "pipeline_version", None),
    )
    if not isinstance(composed_payload, dict):
        raise HTTPException(status_code=404, detail="No cached reader payload available for this page")
    payload_for_response = dict(composed_payload)
    ensure_contract = getattr(compose_service, "_ensure_payload_contract", None)
    if callable(ensure_contract):
        payload_for_response = ensure_contract(page=page_num, payload=payload_for_response)
    cache_layer_value = str(payload_for_response.get("cache_layer") or "").strip().lower()
    if cache_layer_value not in {"redis", "db", "none"}:
        payload_for_response["cache_layer"] = "db"

    return ReaderComposeFetchResponse(
        payload=payload_for_response,
        cache_meta={
            "cache_hit": True,
            "cache_layer": "db",
            "build_mode": str(payload_for_response.get("build_mode") or ""),
            "source_signature": str(payload_for_response.get("source_signature") or ""),
            "source_sig_hash": "",
        },
    )


@router.post(
    "/papers/{paper_id}/reader/composed",
    response_model=ReaderComposeFetchResponse,
)
async def get_reader_composed_page(
    paper_id: int,
    payload: ReaderComposeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    page_num = max(1, int(payload.page))

    composed_payload, meta = await compose_service.build_or_get_composed_payload(
        db=db,
        user_id=int(current_user.id),
        paper=paper,
        page=page_num,
        selected_kb_id=payload.selected_kb_id,
        force_refresh=bool(payload.force_refresh),
        regenerate=bool(payload.regenerate),
        latency_budget_ms=payload.latency_budget_ms,
        quality_target=payload.quality_target,
        max_iterations=getattr(payload, "max_iterations", None),
        style_intent=payload.style_intent,
        theme_mode=getattr(payload, "theme_mode", None),
        detail_level=getattr(payload, "detail_level", None),
        compare_mode=getattr(payload, "compare_mode", None),
        citation_tldr=getattr(payload, "citation_tldr", None),
        publish_ready_event_enabled=False,
    )
    return ReaderComposeFetchResponse(
        payload=composed_payload,
        cache_meta={
            "cache_hit": bool(meta.cache_hit),
            "cache_layer": str(meta.cache_layer or "none"),
            "build_mode": str(meta.build_mode or ""),
            "source_signature": str(meta.source_signature or ""),
            "source_sig_hash": str(meta.source_sig_hash or ""),
        },
    )

@router.post("/papers/{paper_id}/reader/composed/stream")
async def stream_reader_composed_page(
    paper_id: int,
    payload: ReaderComposeRequest,
    request: Request,
    current_user: User = Depends(get_current_user_for_stream),
):
    service = get_literature_reader_compose_service()
    page_num = max(1, int(payload.page))

    async def event_generator():
        heartbeat_interval_seconds = 3.0
        progress_state: Dict[str, Any] = {
            "stage": "compose_pending",
            "message": "正在准备阅读骨架",
            "build_started_at": time.perf_counter(),
            "stage_started_at": time.perf_counter(),
        }
        event_queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()
        stream_closed = False

        async def enqueue_progress_event(event: str, data: Any) -> None:
            if stream_closed:
                return
            if event == "stage" and isinstance(data, Mapping):
                now = time.perf_counter()
                stage_token = str(data.get("stage") or "").strip()
                status_token = str(data.get("status") or "").strip().lower()
                message_token = str(data.get("message") or "").strip()
                if status_token == "started":
                    progress_state["stage"] = stage_token or progress_state.get("stage") or "compose_pending"
                    progress_state["message"] = message_token or progress_state.get("message") or "正在生成阅读界面"
                    progress_state["stage_started_at"] = now
                elif status_token == "done":
                    if stage_token:
                        progress_state["stage"] = stage_token
                    if message_token:
                        progress_state["message"] = message_token
            await event_queue.put((str(event or "").strip() or "stage", data))

        def sanitize_stream_ui_plan(raw_ui_plan: Any, payload_hint: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
            candidate = dict(raw_ui_plan or {}) if isinstance(raw_ui_plan, Mapping) else {}
            sanitize = getattr(service, "_sanitize_ui_plan_for_runtime", None)
            if not callable(sanitize):
                return candidate
            try:
                return sanitize(
                    page=page_num,
                    payload=dict(payload_hint or {}),
                    ui_plan=candidate,
                )
            except Exception as exc:
                logger.warning(
                    "[Literature API] stream ui_plan sanitize failed "
                    f"paper={paper_id} page={page_num}: {exc}"
                )
                return candidate

        async def build_payload_in_background() -> None:
            try:
                async with async_session_factory() as db:
                    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
                    composed_payload, meta = await service.build_or_get_composed_payload(
                        db=db,
                        user_id=int(current_user.id),
                        paper=paper,
                        page=page_num,
                        selected_kb_id=payload.selected_kb_id,
                        pipeline_version_override=getattr(payload, "pipeline_version", None),
                        force_refresh=bool(payload.force_refresh),
                        regenerate=bool(payload.regenerate),
                        latency_budget_ms=payload.latency_budget_ms,
                        quality_target=payload.quality_target,
                        max_iterations=getattr(payload, "max_iterations", None),
                        style_intent=payload.style_intent,
                        theme_mode=getattr(payload, "theme_mode", None),
                        detail_level=getattr(payload, "detail_level", None),
                        compare_mode=getattr(payload, "compare_mode", None),
                        citation_tldr=getattr(payload, "citation_tldr", None),
                        publish_ready_event_enabled=False,
                        progress_callback=enqueue_progress_event,
                    )
                if stream_closed:
                    return
                await event_queue.put(
                    (
                        "__build_complete__",
                        {
                            "composed_payload": composed_payload,
                            "meta": meta,
                        },
                    )
                )
            except RenderPipelineContractError as exc:
                logger.warning(
                    f"[Literature API] composed stream contract failed paper={paper_id}, page={page_num}: "
                    f"stage={exc.stage} code={exc.code} message={exc.message}"
                )
                if not stream_closed:
                    await event_queue.put(("__contract_error__", exc.to_dict()))
            except Exception as exc:
                logger.exception(f"[Literature API] composed stream failed paper={paper_id}, page={page_num}: {exc}")
                if not stream_closed:
                    await event_queue.put(("__error__", {"message": str(exc)}))

        build_task: Optional[asyncio.Task[Any]] = None
        try:
            logger.info(
                "[Literature API] composed stream start "
                f"paper={paper_id} page={page_num} force_refresh={bool(payload.force_refresh)} "
                f"regenerate={bool(payload.regenerate)}"
            )
            yield _sse_payload(
                "start",
                {
                    "cache_hit": False,
                    "cache_layer": "none",
                    "build_mode": "compose_pending",
                    "page": int(page_num),
                    "engine_version": "",
                    "budget": {
                        "latency_budget_ms": int(
                            payload.latency_budget_ms
                            or int(getattr(settings, "reader_compose_latency_budget_ms", 20000) or 20000)
                        ),
                        "quality_target": float(payload.quality_target or 0.86),
                    },
                },
            )
            build_task = asyncio.create_task(build_payload_in_background())

            composed_payload: Optional[Dict[str, Any]] = None
            meta = None
            while True:
                if await request.is_disconnected():
                    stream_closed = True
                    logger.warning(
                        f"[Literature API] composed stream client disconnected before emit done "
                        f"paper={paper_id} page={page_num}"
                    )
                    return
                try:
                    event_name, event_data = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=heartbeat_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    if build_task is not None and build_task.done() and event_queue.empty():
                        break
                    now = time.perf_counter()
                    yield _sse_payload(
                        "heartbeat",
                        {
                            "page": int(page_num),
                            "stage": str(progress_state.get("stage") or "compose_pending"),
                            "message": str(progress_state.get("message") or "正在生成阅读界面"),
                            "elapsed_ms": int(max(0.0, (now - float(progress_state.get("build_started_at") or now)) * 1000.0)),
                            "stage_elapsed_ms": int(max(0.0, (now - float(progress_state.get("stage_started_at") or now)) * 1000.0)),
                        },
                    )
                    continue
                if event_name == "__build_complete__":
                    composed_payload = dict((event_data or {}).get("composed_payload") or {})
                    meta = (event_data or {}).get("meta")
                    break
                if event_name == "__contract_error__":
                    error_payload = dict(event_data or {})
                    yield _sse_payload(
                        "component_error",
                        {
                            "message": str(error_payload.get("message") or "render_pipeline_contract_error"),
                            "stage": str(error_payload.get("stage") or ""),
                            "code": str(error_payload.get("code") or ""),
                            "details": dict(error_payload.get("details") or {}),
                            "errors": [str(error_payload.get("code") or "")],
                        },
                    )
                    yield _sse_payload(
                        "error",
                        {
                            "message": str(error_payload.get("message") or "render_pipeline_contract_error"),
                            "stage": str(error_payload.get("stage") or ""),
                            "code": str(error_payload.get("code") or ""),
                        },
                    )
                    return
                if event_name == "__error__":
                    yield _sse_payload("error", {"message": str((event_data or {}).get("message") or "stream_build_failed")})
                    return
                if event_name in {"plan_draft", "plan_patch"} and isinstance(event_data, Mapping):
                    event_dict = dict(event_data or {})
                    event_dict["ui_plan"] = sanitize_stream_ui_plan(event_dict.get("ui_plan"), {})
                    event_data = event_dict
                yield _sse_payload(event_name, event_data)

            if composed_payload is None or meta is None:
                yield _sse_payload("error", {"message": "stream_build_incomplete"})
                return

            logger.info(
                "[Literature API] composed stream built "
                f"paper={paper_id} page={page_num} cache_hit={bool(meta.cache_hit)} cache_layer={meta.cache_layer} "
                f"build_mode={meta.build_mode} iterations={int(meta.iterations or 0)} degraded={bool(meta.degraded)} "
                f"stop_reason={str(meta.stop_reason or '')} status={str(composed_payload.get('status') or '')} "
                f"degraded_reason={str(composed_payload.get('degraded_reason') or '')}"
            )
            if await request.is_disconnected():
                stream_closed = True
                logger.warning(
                    f"[Literature API] composed stream client disconnected before emit done "
                    f"paper={paper_id} page={page_num}"
                )
                return

            trace_rows = list(composed_payload.get("iteration_trace") or [])
            if trace_rows:
                first = trace_rows[0]
                yield _sse_payload(
                    "plan_draft",
                    {
                        "iteration": int(first.get("iteration") or 1),
                        "ui_plan": sanitize_stream_ui_plan(
                            first.get("ui_plan") or composed_payload.get("ui_plan") or {},
                            composed_payload,
                        ),
                        "phase": "skeleton",
                        "layout_lock": True,
                    },
                )
                first_ops = [row for row in list(first.get("ui_ops") or []) if isinstance(row, dict)]
                if first_ops:
                    yield _sse_payload(
                        "component_patch",
                        {
                            "iteration": int(first.get("iteration") or 1),
                            "seq": 1,
                            "ui_ops": first_ops,
                            "source": "agent",
                        },
                    )
                first_agent_trace = [row for row in list(first.get("agent_trace") or []) if isinstance(row, dict)]
                if first_agent_trace:
                    yield _sse_payload(
                        "agent_trace",
                        {
                            "iteration": int(first.get("iteration") or 1),
                            "trace": first_agent_trace,
                            "tool_calls": [row for row in list(first.get("agent_tool_calls") or []) if isinstance(row, dict)],
                        },
                    )
                for row in trace_rows[1:]:
                    if await request.is_disconnected():
                        return
                    yield _sse_payload(
                        "plan_patch",
                        {
                            "iteration": int(row.get("iteration") or 0),
                            "ui_plan": sanitize_stream_ui_plan(row.get("ui_plan") or {}, composed_payload),
                            "phase": "semantic",
                            "patch_type": "node_replace",
                        },
                    )
                    row_ops = [item for item in list(row.get("ui_ops") or []) if isinstance(item, dict)]
                    if row_ops:
                        yield _sse_payload(
                            "component_patch",
                            {
                                "iteration": int(row.get("iteration") or 0),
                                "seq": int(row.get("iteration") or 0),
                                "ui_ops": row_ops,
                                "source": "agent",
                            },
                        )
                    row_agent_trace = [item for item in list(row.get("agent_trace") or []) if isinstance(item, dict)]
                    if row_agent_trace:
                        yield _sse_payload(
                            "agent_trace",
                            {
                                "iteration": int(row.get("iteration") or 0),
                                "trace": row_agent_trace,
                                "tool_calls": [item for item in list(row.get("agent_tool_calls") or []) if isinstance(item, dict)],
                            },
                        )
                    yield _sse_payload(
                        "quality",
                        {
                            "iteration": int(row.get("iteration") or 0),
                            "quality_report": row.get("quality_report") or {},
                            "mm_assist_used": bool((row.get("quality_report") or {}).get("mm_assist_used")),
                            "mm_fallback_used": bool((row.get("quality_report") or {}).get("mm_fallback_used")),
                            "cross_column_merge_ratio": float((row.get("quality_report") or {}).get("cross_column_merge_ratio") or 0.0),
                            "sidebar_recall": float((row.get("quality_report") or {}).get("sidebar_recall") or 0.0),
                        },
                    )
            else:
                yield _sse_payload(
                    "plan_draft",
                    {
                        "iteration": 1,
                        "ui_plan": sanitize_stream_ui_plan(composed_payload.get("ui_plan") or {}, composed_payload),
                        "phase": "skeleton",
                        "layout_lock": True,
                    },
                )
                final_ops = [
                    row
                    for row in list(((composed_payload.get("ui_plan") or {}).get("ui_ops") or []))
                    if isinstance(row, dict)
                ]
                if final_ops:
                    yield _sse_payload(
                        "component_patch",
                        {
                            "iteration": 1,
                            "seq": 1,
                            "ui_ops": final_ops,
                            "source": "agent",
                        },
                    )

            yield _sse_payload("assets", {"assets": list(composed_payload.get("assets") or [])})
            yield _sse_payload(
                "quality",
                {
                    "iteration": int(meta.iterations or 0),
                    "quality_report": composed_payload.get("quality_report") or {},
                    "mm_assist_used": bool((composed_payload.get("quality_report") or {}).get("mm_assist_used")),
                    "mm_fallback_used": bool((composed_payload.get("quality_report") or {}).get("mm_fallback_used")),
                    "cross_column_merge_ratio": float((composed_payload.get("quality_report") or {}).get("cross_column_merge_ratio") or 0.0),
                    "sidebar_recall": float((composed_payload.get("quality_report") or {}).get("sidebar_recall") or 0.0),
                },
            )
            trace_meta = dict(((composed_payload.get("ui_plan") or {}).get("trace_meta") or {}))
            assembly_errors = [
                str(item).strip()
                for item in list(trace_meta.get("assembly_validation_errors") or [])
                if str(item).strip()
            ] + [
                str(item).strip()
                for item in list(trace_meta.get("assembly_apply_errors") or [])
                if str(item).strip()
            ]
            if assembly_errors:
                yield _sse_payload(
                    "component_error",
                    {
                        "message": "component_patch_validation_failed",
                        "errors": assembly_errors[:20],
                    },
                )
            yield _sse_payload(
                "done",
                {
                    "status": str(composed_payload.get("status") or "done"),
                    "degraded_reason": str(composed_payload.get("degraded_reason") or ""),
                    "payload": composed_payload,
                    "cache_meta": {
                        "cache_hit": bool(meta.cache_hit),
                        "cache_layer": meta.cache_layer,
                        "build_mode": str(meta.build_mode),
                        "source_signature": meta.source_signature,
                        "source_sig_hash": meta.source_sig_hash,
                    },
                    "iteration_stats": {
                        "iterations": int(meta.iterations or 0),
                        "degraded": bool(meta.degraded),
                        "stop_reason": str(meta.stop_reason or ""),
                    },
                    "overlay_meta": {
                        "overlay_applied": bool(composed_payload.get("overlay_applied")),
                        "overlay_count": int(composed_payload.get("overlay_count") or 0),
                    },
                    "qwen_plan_meta": dict(composed_payload.get("qwen_plan_meta") or {}),
                    "parser_chain_meta": dict(composed_payload.get("parser_chain_meta") or {}),
                    "docmind_meta": dict(composed_payload.get("docmind_meta") or {}),
                    "page_structure_source": str((composed_payload.get("page_structure_v3") or {}).get("source") or ""),
                    "pipeline_contract_meta": dict(composed_payload.get("pipeline_contract_meta") or {}),
                    "stage1_structural_annotations": dict(composed_payload.get("stage1_structural_annotations") or {}),
                    "stage2_design_layout": dict(composed_payload.get("stage2_design_layout") or {}),
                    "canonical_atoms": dict(composed_payload.get("canonical_atoms") or {}),
                    "atom_semantics": dict(composed_payload.get("atom_semantics") or {}),
                    "deterministic_page_skeleton": dict(composed_payload.get("deterministic_page_skeleton") or {}),
                    "stage2_style_plan": dict(composed_payload.get("stage2_style_plan") or {}),
                    "minimal_gate_report": dict(composed_payload.get("minimal_gate_report") or {}),
                    "candidate_ranking": dict(composed_payload.get("candidate_ranking") or {}),
                    "repair_report": dict(composed_payload.get("repair_report") or {}),
                    "segment_id_map": dict(composed_payload.get("segment_id_map") or {}),
                    "layout_advice_meta": dict(composed_payload.get("layout_advice_meta") or {}),
                    "segment_stats": {
                        "used": bool((composed_payload.get("segment_map_meta") or {}).get("used")),
                        "reason": str((composed_payload.get("segment_map_meta") or {}).get("reason") or ""),
                        "source": str((composed_payload.get("segment_map") or {}).get("source") or ""),
                        "segment_count": len(list((composed_payload.get("segment_map") or {}).get("segments") or [])),
                    },
                    "validation_report": dict(composed_payload.get("validation_report") or {}),
                    "node_gate_stats": dict(composed_payload.get("node_gate_report") or {}),
                },
            )
            logger.info(
                f"[Literature API] composed stream done emitted paper={paper_id} page={page_num}"
            )
        except Exception as exc:
            logger.exception(f"[Literature API] composed stream failed paper={paper_id}, page={page_num}: {exc}")
            yield _sse_payload("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/papers/{paper_id}/reader/composed/review-session",
    response_model=ReaderComposeReviewSnapshot,
)
async def create_reader_composed_review_session(
    paper_id: int,
    payload: ReaderComposeReviewSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        snapshot = await service.create_review_session(
            db=db,
            user_id=int(current_user.id),
            paper=paper,
            page=max(1, int(payload.page)),
            selected_kb_id=payload.selected_kb_id,
            force_refresh=bool(payload.force_refresh),
            regenerate=bool(payload.regenerate),
            latency_budget_ms=payload.latency_budget_ms,
            quality_target=payload.quality_target,
            max_iterations=payload.max_iterations,
            style_intent=payload.style_intent,
            theme_mode=payload.theme_mode,
            detail_level=payload.detail_level,
            compare_mode=payload.compare_mode,
            citation_tldr=payload.citation_tldr,
            snapshot_label=payload.snapshot_label,
            prefer_cache_clone=bool(payload.prefer_cache_clone),
            allow_recompute_on_cache_miss=bool(payload.allow_recompute_on_cache_miss),
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_cache_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    return snapshot


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/import",
    response_model=ReaderComposeReviewSnapshot,
)
async def import_reader_composed_review_session(
    paper_id: int,
    payload: ReaderComposeReviewImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        snapshot = await service.create_review_session_from_payload(
            db=db,
            user_id=int(current_user.id),
            paper=paper,
            payload=payload.payload.model_dump(),
            snapshot_label=payload.snapshot_label,
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_payload_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    return snapshot


@router.get(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}",
    response_model=ReaderComposeReviewSnapshot,
)
async def get_reader_composed_review_snapshot(
    paper_id: int,
    session_id: str,
    snapshot_id: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    snapshot = await service.get_review_snapshot(
        session_id=str(session_id),
        snapshot_id=snapshot_id,
    )
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    if int(snapshot.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return snapshot


def _parse_review_observation_diagnostics_json(raw: Optional[str]) -> List[Dict[str, Any]]:
    token = str(raw or "").strip()
    if not token:
        return []
    try:
        parsed = json.loads(token)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="diagnostics_json 不是合法 JSON") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail="diagnostics_json 必须是数组")
    normalized: List[Dict[str, Any]] = []
    for row in parsed:
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail="diagnostics_json 只能包含对象")
        normalized.append(ReaderComposeReviewDiagnostic.model_validate(row).model_dump())
    return normalized


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/patch",
    response_model=ReaderComposeReviewSnapshot,
)
async def patch_reader_composed_review_snapshot(
    paper_id: int,
    session_id: str,
    payload: ReaderComposeReviewPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        snapshot = await service.apply_review_patch(
            session_id=str(session_id),
            snapshot_id=payload.snapshot_id,
            ui_ops=[row for row in list(payload.ui_ops or []) if isinstance(row, dict)],
            decision_log_append=[str(item).strip() for item in list(payload.decision_log_append or []) if str(item).strip()],
            omission_decisions=[row.model_dump() for row in list(payload.omission_decisions or [])] if payload.omission_decisions is not None else None,
            scheme_choice=payload.scheme_choice.model_dump() if payload.scheme_choice is not None else None,
            note=payload.note,
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_session_not_found", "review_snapshot_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    if int(snapshot.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return snapshot


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/observation",
    response_model=ReaderComposeReviewSnapshot,
)
async def observe_reader_composed_review_snapshot(
    paper_id: int,
    session_id: str,
    payload: ReaderComposeReviewObservationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        snapshot = await service.record_review_observation(
            session_id=str(session_id),
            snapshot_id=payload.snapshot_id,
            render_image_url=payload.render_image_url,
            render_image_path=None,
            render_image_media_type=None,
            diagnostics=[row.model_dump() for row in list(payload.diagnostics or [])],
            note=payload.note,
            source=payload.source,
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_session_not_found", "review_snapshot_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    if int(snapshot.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return snapshot


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/observation-image",
    response_model=ReaderComposeReviewSnapshot,
)
async def upload_reader_composed_review_observation_image(
    paper_id: int,
    session_id: str,
    image: UploadFile = File(...),
    snapshot_id: Optional[str] = Form(default=None),
    diagnostics_json: Optional[str] = Form(default=None),
    note: Optional[str] = Form(default=None),
    source: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        image_bytes = await image.read()
        stored = await service.store_review_observation_image(
            session_id=str(session_id),
            snapshot_id=snapshot_id,
            filename=image.filename,
            content_type=image.content_type,
            data=image_bytes,
        )
        snapshot = await service.record_review_observation(
            session_id=str(session_id),
            snapshot_id=str(stored.get("snapshot_id") or snapshot_id or ""),
            render_image_url=str(stored.get("render_image_url") or ""),
            render_image_path=str(stored.get("file_path") or ""),
            render_image_media_type=str(stored.get("media_type") or ""),
            diagnostics=_parse_review_observation_diagnostics_json(diagnostics_json),
            note=note,
            source=source or "uploaded_render_image",
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_session_not_found", "review_snapshot_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    finally:
        await image.close()
    if int(snapshot.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return snapshot


@router.get(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/observation-image/{snapshot_id}",
)
async def stream_reader_composed_review_observation_image(
    paper_id: int,
    session_id: str,
    snapshot_id: str,
    db: AsyncSession = Depends(get_db),
):
    # TODO(security): 当前路由为前端 <img src> 与多模态 URL 兼容而临时放开鉴权。
    # 后续应切换为短时效签名 URL 或前端 fetch(Bearer)+blob 渲染。
    paper = await db.get(Paper, int(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    service = get_literature_reader_compose_service()
    resolved = await service.resolve_review_observation_image(
        session_id=str(session_id),
        snapshot_id=str(snapshot_id),
    )
    if not isinstance(resolved, dict):
        raise HTTPException(status_code=404, detail="review observation image not found")
    if int(resolved.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review observation image not found")
    return FileResponse(
        path=str(resolved.get("file_path") or ""),
        media_type=str(resolved.get("media_type") or "application/octet-stream"),
        filename=os.path.basename(str(resolved.get("file_path") or "")),
    )


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/auto-patch",
    response_model=ReaderComposeReviewAutoPatchResponse,
)
async def auto_patch_reader_composed_review_snapshot(
    paper_id: int,
    session_id: str,
    payload: ReaderComposeReviewAutoPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        result = await service.auto_patch_review_snapshot(
            session_id=str(session_id),
            snapshot_id=payload.snapshot_id,
            user_id=int(current_user.id),
            user_intent=payload.user_intent,
            note=payload.note,
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_session_not_found", "review_snapshot_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    snapshot = dict(result.get("snapshot") or {})
    if int(snapshot.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return result


@router.post(
    "/papers/{paper_id}/reader/composed/review-session/{session_id}/publish",
    response_model=ReaderComposeReviewPublishResponse,
)
async def publish_reader_composed_review_snapshot(
    paper_id: int,
    session_id: str,
    payload: ReaderComposeReviewPublishRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        result = await service.publish_review_snapshot(
            db=db,
            user_id=int(current_user.id),
            paper=paper,
            session_id=str(session_id),
            snapshot_id=payload.snapshot_id,
            note=payload.note,
        )
    except ValueError as exc:
        token = str(exc)
        status_code = 404 if token in {"review_session_not_found", "review_snapshot_not_found"} else 409
        raise HTTPException(status_code=status_code, detail=token) from exc
    if int(result.get("paper_id") or 0) != int(paper.id):
        raise HTTPException(status_code=404, detail="review session snapshot not found")
    return result


@router.post(
    "/papers/{paper_id}/reader/composed/prefetch",
    response_model=ReaderComposePrefetchResponse,
)
async def prefetch_reader_composed_pages(
    paper_id: int,
    payload: ReaderComposePrefetchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    pdf_path = _resolve_local_pdf_path(user_id=int(current_user.id), paper=paper)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=409, detail="本地 PDF 不存在，请先下载后再执行预读。")
    max_page = await _get_pdf_page_count(pdf_path)
    queued, skipped = service.queue_prefetch(
        pages=list(payload.pages or []),
        max_page=max_page,
    )

    if queued:
        background_tasks.add_task(
            _prefetch_reader_composed_pages_background,
            user_id=int(current_user.id),
            paper_id=int(paper.id),
            pages=queued,
            selected_kb_id=payload.selected_kb_id,
            pipeline_version=getattr(payload, "pipeline_version", None),
            style_intent=payload.style_intent,
            latency_budget_ms=payload.latency_budget_ms,
            quality_target=payload.quality_target,
            max_iterations=getattr(payload, "max_iterations", None),
            theme_mode=getattr(payload, "theme_mode", None),
            detail_level=getattr(payload, "detail_level", None),
            compare_mode=getattr(payload, "compare_mode", None),
            citation_tldr=getattr(payload, "citation_tldr", None),
        )

    return ReaderComposePrefetchResponse(queued=queued, skipped=skipped)


@router.post(
    "/papers/{paper_id}/reader/composed/node/action",
    response_model=ReaderNodeActionResponse,
)
async def action_reader_composed_node(
    paper_id: int,
    payload: ReaderNodeActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _is_single_agent_v2_active(paper_id=int(paper_id), page=int(payload.page)):
        return ReaderNodeActionResponse(
            patch_type="node_update",
            node_before=None,
            node_after=None,
            quality_delta=0.0,
            overlay_saved=False,
            message="Node actions are disabled in single_agent_v2 mode.",
            disabled=True,
            disabled_reason="single_agent_v2_node_action_disabled",
        )
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    try:
        result = await service.perform_node_action(
            db=db,
            user_id=int(current_user.id),
            paper=paper,
            page=int(payload.page),
            node_id=str(payload.node_id),
            action=str(payload.action),
            reason=payload.reason,
            selected_kb_id=payload.selected_kb_id,
            style_intent=payload.style_intent,
            theme_mode=getattr(payload, "theme_mode", None),
            detail_level=getattr(payload, "detail_level", None),
            compare_mode=getattr(payload, "compare_mode", None),
            citation_tldr=getattr(payload, "citation_tldr", None),
        )
        return ReaderNodeActionResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/reader/composed/inline-query/stream")
async def stream_reader_composed_inline_query(
    paper_id: int,
    payload: ReaderInlineQueryRequest,
    request: Request,
    current_user: User = Depends(get_current_user_for_stream),
):
    service = get_literature_reader_compose_service()

    async def event_generator():
        try:
            async with async_session_factory() as db:
                paper = await _get_owned_paper_or_404(db, current_user, paper_id)
                prepared = await service.prepare_inline_query_answer(
                    db=db,
                    user_id=int(current_user.id),
                    paper=paper,
                    page=int(payload.page),
                    node_id=str(payload.node_id),
                    question=str(payload.question),
                    scope=str(payload.scope),
                    selected_kb_id=payload.selected_kb_id,
                    style_intent=payload.style_intent,
                    theme_mode=getattr(payload, "theme_mode", None),
                    detail_level=getattr(payload, "detail_level", None),
                    compare_mode=getattr(payload, "compare_mode", None),
                    citation_tldr=getattr(payload, "citation_tldr", None),
                )
            if await request.is_disconnected():
                return
            if bool(prepared.get("disabled")):
                yield _sse_payload(
                    "disabled",
                    {
                        "disabled": True,
                        "disabled_reason": str(prepared.get("disabled_reason") or "inline_query_contract_failed"),
                        "message": str(prepared.get("message") or "当前段落追问不可用，请改用右侧“询问”进行全文问答。"),
                    },
                )
                yield _sse_payload(
                    "done",
                    {
                        "disabled": True,
                        "disabled_reason": str(prepared.get("disabled_reason") or "inline_query_contract_failed"),
                        "sources": [],
                    },
                )
                return
            sources = []
            for anchor in list(prepared.get("anchor_refs") or [])[:3]:
                if not isinstance(anchor, dict):
                    continue
                sources.append(
                    {
                        "page": int(anchor.get("page") or payload.page or 1),
                        "start_char": int(anchor.get("start_char") or 0),
                        "end_char": int(anchor.get("end_char") or 0),
                        "quote": str(anchor.get("quote") or anchor.get("quote_text") or "")[:240] or None,
                        "quote_text": str(anchor.get("quote_text") or "")[:240] or None,
                    }
                )

            yield _sse_payload("start", {"page": int(payload.page), "node_id": str(payload.node_id)})
            chunks: List[str] = []
            try:
                async for token in service._stream_inline_answer_tokens(
                    question=str(prepared.get("question") or payload.question or ""),
                    context_text=str(prepared.get("context_text") or ""),
                    scope=str(prepared.get("scope") or payload.scope or "section"),
                ):
                    if await request.is_disconnected():
                        return
                    chunks.append(token)
                    yield _sse_payload("token", {"text": token})
            except Exception as exc:
                logger.exception(f"[Literature API] composed inline query stream failed paper={paper_id}: {exc}")
            answer_text = service._normalize_spaces("".join(chunks))
            if not answer_text:
                answer_text = service._fallback_inline_answer(str(prepared.get("question") or payload.question or ""))
            node = service._build_inline_answer_node(
                question=str(prepared.get("question") or payload.question or "").strip(),
                answer=answer_text,
                anchor_refs=list(prepared.get("anchor_refs") or []),
                source_block_ids=list(prepared.get("source_block_ids") or []),
            )
            yield _sse_payload("sources", sources)
            yield _sse_payload("done", {"node": node, "sources": sources})
        except Exception as exc:
            logger.exception(f"[Literature API] composed inline query failed paper={paper_id}: {exc}")
            yield _sse_payload("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/papers/{paper_id}/experience-v2/block-explain/stream")
async def stream_reader_experience_v2_block_explain(
    paper_id: int,
    payload: ReaderExperienceBlockExplainRequest,
    request: Request,
    current_user: User = Depends(get_current_user_for_stream),
):
    async def event_generator():
        request_payload = payload
        try:
            async with async_session_factory() as db:
                paper = await _get_owned_paper_or_404(db, current_user, paper_id)
                if str(request_payload.explain_kind or "").strip().lower() == "figure":
                    if not str(request_payload.figure_image_url or "").strip():
                        yield _sse_payload("error", {"message": "当前图块没有可用图片 asset，无法只解释这张图。"})
                        return
                    normalized_figure_image_url = await _normalize_reader_experience_block_explain_image_url(
                        db=db,
                        paper=paper,
                        raw_url=str(request_payload.figure_image_url or "").strip(),
                    )
                    if normalized_figure_image_url != str(request_payload.figure_image_url or "").strip():
                        request_payload = request_payload.model_copy(update={"figure_image_url": normalized_figure_image_url})

            config = _experience_session_v2_artifact_agent_config()
            client = AsyncOpenAI(api_key=config["api_key"], base_url=config["base_url"])
            system_prompt = _reader_experience_block_explain_system_prompt(str(request_payload.explain_kind or ""))
            messages = [{"role": "system", "content": system_prompt}] + _build_reader_experience_block_explain_messages(request_payload)

            if await request.is_disconnected():
                return

            yield _sse_payload(
                "start",
                {
                    "page": int(request_payload.page),
                    "block_id": str(request_payload.block_id),
                    "explain_kind": str(request_payload.explain_kind),
                    "model": str(config["model"]),
                },
            )

            request_kwargs = {
                "model": str(config["model"]),
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": min(int(config["max_tokens"]), 2200),
                "stream": True,
            }
            stream = await asyncio.wait_for(
                _create_reader_experience_block_explain_stream(
                    client=client,
                    request_kwargs=request_kwargs,
                ),
                timeout=float(config["timeout_seconds"]),
            )

            chunks: List[str] = []
            async for chunk in stream:
                if await request.is_disconnected():
                    return
                delta = getattr((getattr(chunk, "choices", None) or [None])[0], "delta", None)
                token = str(getattr(delta, "content", "") or "")
                if not token:
                    continue
                chunks.append(token)
                yield _sse_payload("token", {"text": token})

            answer = "".join(chunks).strip()
            if not answer:
                if str(request_payload.explain_kind or "").strip().lower() == "figure":
                    answer = "仅根据当前图块材料，可以先把它理解为这张图在用图注和标签提示读者先看重点证据，再看它支持的结论。你可以继续追问想看哪一部分。"
                else:
                    answer = "仅根据当前这段材料，可以先把它理解为作者在说明这一段的核心意思、关键指标和它为什么重要。你可以继续追问具体哪一句还不够通俗。"

            yield _sse_payload(
                "done",
                {
                    "page": int(request_payload.page),
                    "block_id": str(request_payload.block_id),
                    "explain_kind": str(request_payload.explain_kind),
                    "model": str(config["model"]),
                    "answer": answer,
                },
            )
        except Exception as exc:
            logger.exception(f"[Literature API] experience-v2 block explain failed paper={paper_id}: {exc}")
            yield _sse_payload("error", {"message": _friendly_reader_experience_block_explain_error_message(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/papers/{paper_id}/experience-v2/block-rewrite",
    response_model=ReaderExperienceBlockRewriteResponse,
    response_class=JSONResponse,
)
async def rewrite_reader_experience_v2_block_http(
    paper_id: int,
    payload: ReaderExperienceBlockRewriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    runtime_request = ReaderExperiencePlanRequest(
        page=int(payload.page),
        focus_page=int(payload.page),
        selected_kb_id=payload.selected_kb_id,
        reader_profile=str(payload.reader_profile or "").strip() or "curious_generalist",
        user_intent=str(payload.user_intent or "").strip(),
    )
    runtime_state = await _prepare_reader_experience_v2_runtime(
        paper_id=paper_id,
        payload=runtime_request,
        db=db,
        current_user=current_user,
    )
    cached_artifact = runtime_state.get("cached_artifact")
    if not isinstance(cached_artifact, Mapping):
        raise HTTPException(
            status_code=409,
            detail="completed page_artifact_v2 not available: block rewrite requires an existing ready artifact",
        )

    artifact_payload = PageArtifactV2.model_validate(_jsonable_dict(cached_artifact)).model_dump(mode="json")
    target_index = _find_experience_v2_block_index(list(artifact_payload.get("reading_blocks") or []), payload.block_id)
    if target_index < 0:
        raise HTTPException(status_code=404, detail="target block not found in current artifact")
    target_block = _jsonable_dict(list(artifact_payload.get("reading_blocks") or [])[target_index])
    if not _experience_v2_block_rewrite_is_supported(target_block):
        raise HTTPException(
            status_code=409,
            detail="current block kind does not support local rewrite yet",
        )

    narrative_brief = _find_latest_experience_session_v2_narrative_brief(
        _jsonable_dict(runtime_state.get("cached_session") or {})
    ) or {}
    prompt_payload = _build_experience_v2_block_rewrite_prompt_payload(
        paper=runtime_state["paper"],
        artifact_payload=artifact_payload,
        narrative_brief=narrative_brief,
        block_id=str(payload.block_id or "").strip(),
        rewrite_prompt=str(payload.rewrite_prompt or "").strip(),
        reader_profile=str(runtime_state.get("reader_profile") or "").strip(),
        user_intent=str(runtime_state.get("user_intent") or "").strip(),
    )
    config = _experience_session_v2_artifact_agent_config()
    parsed = await _call_experience_session_v2_artifact_draft_model(
        system_prompt=_experience_v2_block_rewrite_system_prompt(str(target_block.get("segment_kind") or "")),
        user_prompt_payload=prompt_payload,
        provider=str(config.get("provider") or "").strip(),
        api_key=str(config.get("api_key") or "").strip(),
        base_url=str(config.get("base_url") or "").strip(),
        model=str(config.get("model") or "").strip(),
        timeout_seconds=float(config.get("timeout_seconds") or 0.0),
        max_tokens=min(2200, max(512, int(config.get("max_tokens") or 0))),
    )
    rewrite_payload = _validate_experience_v2_block_rewrite_model_payload(parsed)
    updated_artifact, rewritten_block = _apply_experience_v2_block_rewrite_to_artifact(
        artifact_payload=artifact_payload,
        block_id=str(payload.block_id or "").strip(),
        rewritten_text=str(rewrite_payload.get("text") or "").strip(),
        rewrite_prompt=str(payload.rewrite_prompt or "").strip(),
    )
    artifact_validation = _validate_page_artifact_v2_contract(updated_artifact)
    if not artifact_validation.get("valid") or not artifact_validation.get("renderable"):
        raise HTTPException(
            status_code=409,
            detail="completed page_artifact_v2 not available: rewritten block failed artifact validation",
        )
    await _page_artifact_v2_cache_set(
        str(runtime_state.get("artifact_cache_key") or "").strip(),
        updated_artifact,
        user_id=int(current_user.id),
        paper_id=int(runtime_state["paper"].id),
        page=int(runtime_state["focus_page"]),
        compose_source_signature=str(runtime_state.get("compose_source_signature") or "").strip(),
    )
    response_payload = ReaderExperienceBlockRewriteResponse(
        focus_page=int(runtime_state["focus_page"]),
        artifact=PageArtifactV2.model_validate(updated_artifact),
        rewritten_block=PageArtifactV2ReadingBlock.model_validate(rewritten_block),
        message="当前块已重写并覆盖到当前 artifact；重新生成整页后该改写可能被覆盖。",
    ).model_dump(mode="json")
    return JSONResponse(content=response_payload)


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
        download_candidates = _build_pdf_download_candidates(paper)
        if not download_candidates:
            raise HTTPException(status_code=400, detail="论文缺少可用 PDF 文件与下载链接，无法入库")
        pdf_path = _build_paper_pdf_file_path(
            user_id=current_user.id,
            paper_id=int(paper.id),
            title=paper.title,
            ensure_dir=True,
        )
        success = False
        download_error = ""
        download_url = ""
        for candidate_url in download_candidates:
            success, download_error = await get_literature_service().download_pdf(candidate_url, pdf_path)
            if success:
                download_url = candidate_url
                break
        if not success:
            raise HTTPException(status_code=502, detail=download_error or "PDF 下载失败，无法加入知识库")
        paper.pdf_downloaded = True
        if not paper.pdf_url or str(paper.pdf_url).strip() != download_url:
            paper.pdf_url = download_url
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
        metadata_={
            "paper_id": paper.id,
            "title": paper.title,
            "ingest_request": {
                "mode": "local_fast",
                "extract_profile": "general",
                "extract_granularity": "medium",
                "requested_by": int(current_user.id),
                "requested_at": datetime.utcnow().isoformat(),
                "source": "literature_add_to_knowledge",
            },
        },
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
        document_changed, link_changed = await _sync_link_status_from_document(db, link)
        if document_changed or link_changed:
            need_commit = True
        if link_changed:
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
    current_user: User = Depends(get_current_user_for_stream),
):
    """文献模块状态事件流（SSE）。"""
    if paper_id is not None:
        async with async_session_factory() as db:
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

            if event not in {"paper_link_status", "reader_page_ready"} or not isinstance(data, dict):
                continue

            event_paper_id = int(data.get("paper_id") or 0)
            if paper_id is not None and event_paper_id != int(paper_id):
                continue

            yield _sse_payload(event, data)

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
    current_user: User = Depends(get_current_user_for_stream),
):
    ask_mode = str(payload.mode or "agentic").strip().lower()
    if ask_mode not in {"agentic", "classic"}:
        raise HTTPException(status_code=400, detail="mode 仅支持 agentic 或 classic")

    scope = payload.scope
    paper: Optional[Paper] = None
    paper_pdf_path: Optional[str] = None
    target_id: int
    paper_ids: List[int] = []
    document_ids: List[int] = []
    session_id: int = 0
    kb_id: int = 0
    kb_name: str = ""
    paper_id_for_agent: Optional[int] = None
    paper_title_for_agent: str = ""
    cache_key: str = ""
    cached_answer = ""
    cached_sources: List[Dict[str, Any]] = []
    cached_message_id: Optional[int] = None
    agent_messages: List[Dict[str, str]] = []
    messages: List[Dict[str, str]] = []
    public_sources: List[Dict[str, Any]] = []

    async with async_session_factory() as db:
        if scope == AskScope.PAPER.value:
            if payload.paper_id is None:
                raise HTTPException(status_code=400, detail="scope=paper 时必须提供 paper_id")
            paper = await _get_owned_paper_or_404(db, current_user, int(payload.paper_id))
            paper_pdf_path = _resolve_local_pdf_path(int(current_user.id), paper)
            target_id = paper.id
            paper_ids = [paper.id]
            paper_id_for_agent = int(paper.id)
            paper_title_for_agent = str(paper.title or "")
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
        kb_id = int(kb.id)
        kb_name = str(kb.name or f"KB#{kb.id}")
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
        session_id = int(session.id)
        user_message_id = int(user_message.id)

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
            cached_sources = list(cached_payload.get("sources") or [])
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
            cached_message_id = int(assistant.id)
        elif ask_mode == "agentic":
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

            for row in history_rows:
                if row.role not in {"user", "assistant"}:
                    continue
                agent_messages.append({"role": row.role, "content": row.content})
            agent_messages.append({"role": "user", "content": payload.question.strip()})
        else:
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
                "请仅基于以下检索片段回答。\n"
                "若证据不足，请明确说明“无法从当前资料确定”。\n\n"
                f"检索片段：\n{joined_context}"
            )
            messages.append({"role": "user", "content": enriched_user_content})
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

    if cached_answer:
        async def cached_stream():
            yield _sse_payload("start", {"session_id": session_id, "cache_hit": True, "mode": ask_mode})
            step = 40
            for idx in range(0, len(cached_answer), step):
                yield _sse_payload("token", {"text": cached_answer[idx: idx + step]})
            yield _sse_payload("sources", cached_sources)
            yield _sse_payload(
                "done",
                {
                    "session_id": session_id,
                    "message_id": cached_message_id,
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
                        "session_id": session_id,
                        "knowledge_base_id": kb_id,
                        "scope": scope,
                        "cache_hit": False,
                        "mode": "agentic",
                    },
                )

                async with async_session_factory() as db:
                    tool_registry, allowed_tool_names = await _build_literature_agent_tool_registry(
                        db=db,
                        user_id=int(current_user.id),
                        knowledge_base_id=kb_id,
                        knowledge_base_name=kb_name,
                        document_ids=document_ids,
                        paper_id=paper_id_for_agent,
                        paper_title=paper_title_for_agent,
                        paper_pdf_path=paper_pdf_path,
                    )
                if not allowed_tool_names:
                    raise RuntimeError("Agent 工具初始化失败：可用工具为空")

                llm_service = await get_llm_service()
                runtime_context = AgentRuntimeContext(
                    user_id=int(current_user.id),
                    channel="literature",
                    scope_type="literature_session",
                    scope_id=str(session_id),
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
                    async with async_session_factory() as db:
                        fallback_sources = await _retrieve_rag_sources(
                            db,
                            knowledge_base_id=kb_id,
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
                        session_id=session_id,
                        role="assistant",
                        content=answer,
                        sources=public_sources,
                    )
                    save_db.add(assistant)
                    session_row = await save_db.get(LiteratureQASession, session_id)
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
                        "session_id": session_id,
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

    system_prompt = (
        "你是论文阅读问答助手。"
        "必须基于提供的检索片段回答，不得编造事实。"
        "回答尽量简洁准确，引用时使用 [序号] 标注。"
    )

    llm_service = await get_llm_service()

    async def stream():
        chunks: List[str] = []
        saved_message_id: Optional[int] = None
        try:
            yield _sse_payload(
                "start",
                {
                    "session_id": session_id,
                    "knowledge_base_id": kb_id,
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
                    session_id=session_id,
                    role="assistant",
                    content=answer,
                    sources=public_sources,
                )
                save_db.add(assistant)
                session_row = await save_db.get(LiteratureQASession, session_id)
                if session_row:
                    session_row.updated_at = datetime.utcnow()
                await save_db.commit()
                await save_db.refresh(assistant)
                saved_message_id = assistant.id

            yield _sse_payload("sources", public_sources)
            yield _sse_payload(
                "done",
                {
                    "session_id": session_id,
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
    """初始化用户的文献管理（创建默认收藏夹）。"""
    repaired = await _repair_user_collection_mojibake(db, int(current_user.id))

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
        if repaired:
            await db.commit()
            return {"message": "已初始化，并完成乱码修复"}
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
    
    if created_count > 0 or repaired:
        try:
            await db.commit()
            if repaired and created_count <= 0:
                return {"message": "已初始化，并完成乱码修复"}
            return {"message": f"初始化成功，创建了 {created_count} 个收藏夹"}
        except Exception as e:
            await db.rollback()
            logger.warning(f"[Literature API] 初始化时发生冲突: {e}")
            return {"message": "已初始化"}
    
    return {"message": "已初始化"}
