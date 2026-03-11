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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, BackgroundTasks, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete, distinct, text
from sqlalchemy.orm import selectinload
from loguru import logger
from pydantic import BaseModel, Field

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
    ReaderGenerativePlanRequest,
    ReaderGenerativePlanResponse,
    ReaderExperiencePlanRequest,
    ReaderExperiencePlanResponse,
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
    ReaderInlineQueryRequest,
    ReaderNodeActionRequest,
    ReaderNodeActionResponse,
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
from app.services.literature_reader_compose_service import get_literature_reader_compose_service
from app.services.literature_reader_service import get_literature_reader_service
from app.services.llm_service import get_llm_service
from app.services.render_pipeline_contract import RenderPipelineContractError
from app.services.react_agent import AgentCore, AgentRuntimeContext
from app.services.agent_tools_impl.registry import ToolBase, ToolRegistry, ToolResult
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
GENERATIVE_PLAN_CACHE_TTL_SECONDS = 3600
_generative_plan_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
EXPERIENCE_PLAN_CACHE_TTL_SECONDS = 3600
_experience_plan_cache_memory: Dict[str, tuple[float, Dict[str, Any]]] = {}
PAGE_COUNT_CACHE_TTL_SECONDS = 3600
_pdf_page_count_cache: Dict[str, tuple[float, int]] = {}


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
        "You are extracting reference-only continuity text from a neighboring PDF page image.\n"
        "Return JSON only.\n"
        "Focus on readable body text, headings, captions, and section carry-over cues.\n"
        "Ignore decorative labels, chart axis ticks, legends, page chrome, and obvious OCR garbage.\n"
        "Do not summarize or explain; extract short reference text only.\n"
        "Return shape: "
        '{"page": 0, "relation": "previous_page|next_page", "reference_only": true, "text": "..."}.'
    )
    user_prompt = (
        f"target_page={int(page)}\n"
        f"relation={relation}\n"
        "This content is reference-only for the CURRENT page and must not override current-page evidence.\n"
        "Extract at most 1200 Chinese/English characters of useful continuity text."
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
    text = str(parsed.get("text") or result.get("raw_text") or "").strip()
    if not text:
        return None
    return {
        "page": int(parsed.get("page") or page),
        "relation": str(parsed.get("relation") or relation).strip() or relation,
        "reference_only": True,
        "source": "vlflash_page_ocr",
        "text": text[:1200],
    }


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
    return f"lit:genplan:v14:{int(user_id)}:{int(paper_id)}:{int(page)}:{int(selected_kb_id)}:{sig_hash}:{intent_hash}:{model_hash}"


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
        f"lit:experience:v15:{int(user_id)}:{int(paper_id)}:{int(focus_page)}:{int(selected_kb_id)}:"
        f"{sig_hash}:{plan_hash}:{intent_hash}:{profile_hash}:{sections_hash}:{model_hash}"
    )


def _plan_signature(payload: Mapping[str, Any]) -> str:
    try:
        normalized = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


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
) -> None:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
            return
        except Exception as exc:
            logger.warning(f"[Literature GenerativePlan] Redis写入失败，降级内存缓存: {exc}")

    _generative_plan_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


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
) -> None:
    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=max(1, int(ttl_seconds)))
            return
        except Exception as exc:
            logger.warning(f"[Literature ExperiencePlan] Redis写入失败，降级内存缓存: {exc}")

    _experience_plan_cache_memory[cache_key] = (time.time() + max(1, int(ttl_seconds)), payload)


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
        return f"{self.SYSTEM_PROMPT.format(tools_description=tools_desc)}\n\n{self.CITATION_POLICY_PROMPT}"

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
    resolved_kb_id = int(selected_kb_id or getattr(paper, "knowledge_base_id", 0) or 0)

    if resolved_kb_id > 0:
        kb = await _get_owned_kb_or_404(db, current_user, int(resolved_kb_id))
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
        return registry, {name for name in allowed if name in semantic_tool_names}

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
    return registry, {name for name in allowed_tool_names if name in semantic_tool_names}


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
    source: str = Query("semantic_scholar", description="数据源：semantic_scholar, arxiv, pubmed, openalex, crossref"),
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
    - arxiv: arXiv (预印本平台，含 cs/physics/math 等学科)
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
        # 使用 scalars().first() 安全处理可能存在的多个默认收藏夹
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

    upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
    base_dir = os.path.abspath(
        os.path.join(upload_dir, "reader_figure_assets", str(int(paper.id)), f"p{int(page)}")
    )

    def _locate_candidate_file() -> tuple[Optional[str], str]:
        if not os.path.isdir(base_dir):
            return None, ""
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = os.path.abspath(os.path.join(base_dir, f"{normalized_asset_id}.{ext}"))
            if not path.startswith(base_dir + os.sep):
                continue
            if os.path.exists(path):
                return path, ext
        return None, ""

    candidate_path, candidate_ext = _locate_candidate_file()

    # 兼容旧缓存：如果文件尚未生成，尝试基于该页已缓存 payload + 本地 PDF 现场补抽 figure 资产。
    if not candidate_path:
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
                candidate_path, candidate_ext = _locate_candidate_file()
        except Exception as exc:
            logger.warning(
                "[Literature API] figure asset lazy-build failed "
                f"paper={paper_id} page={page} asset_id={normalized_asset_id}: {exc}"
            )

    if not candidate_path:
        raise HTTPException(status_code=404, detail="图片资源不存在")

    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(candidate_ext, "application/octet-stream")

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


@router.post("/papers/{paper_id}/reader/generative/stream")
async def stream_reader_generative_page(
    paper_id: int,
    payload: ReaderGenerativeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_service()
    page_num = max(1, int(payload.page))

    async def event_generator():
        try:
            page_payload, meta = await service.build_or_get_page_payload(
                db=db,
                user_id=int(current_user.id),
                paper=paper,
                page=page_num,
                selected_kb_id=payload.selected_kb_id,
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
        selected_kb_id=int(payload.selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        user_intent=str(payload.user_intent or "").strip(),
    )
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_plan, cache_layer = await _generative_plan_cache_get(cache_key)
        if isinstance(cached_plan, dict):
            plan_cache_hit = True
            plan_cache_layer = cache_layer
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
            )
    tool_registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
        db=db,
        current_user=current_user,
        paper=paper,
        selected_kb_id=payload.selected_kb_id,
    )
    adjacent_page_context = await _build_experience_adjacent_page_context(
        compose_service=compose_service,
        paper=paper,
        focus_page=page_num,
    )
    plan = await runtime.build_plan(
        user_id=int(current_user.id),
        page=page_num,
        user_intent=str(payload.user_intent or "").strip(),
        compose_payload=composed_payload,
        tool_registry=tool_registry,
        allowed_tool_names=sorted(list(allowed_tool_names)),
        adjacent_page_context=adjacent_page_context,
    )
    plan_payload = plan if isinstance(plan, dict) else json.loads(json.dumps(plan, ensure_ascii=False, default=str))
    await _generative_plan_cache_set(cache_key, plan_payload)
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
    )


@router.post(
    "/papers/{paper_id}/experience/plan/cached",
    response_model=ReaderExperiencePlanResponse,
)
async def get_reader_experience_plan_cached(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    runtime = get_generative_reader_agent_runtime()
    focus_page = max(1, int(payload.focus_page or payload.page or 1))

    composed_payload = await compose_service.get_latest_cached_payload_only(
        db=db,
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
    )
    if not isinstance(composed_payload, dict):
        raise HTTPException(status_code=404, detail="No cached reader payload available for this page")

    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or "compose_cache")
    compose_source_signature = str(composed_payload.get("source_signature") or "")
    source_sig_hash = ""

    generative_cache_hit = False
    generative_cache_layer = "none"
    plan_cache_key = _generative_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
        selected_kb_id=int(payload.selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        user_intent=str(payload.user_intent or "").strip(),
    )
    cached_plan, cached_layer = await _generative_plan_cache_get(plan_cache_key)
    if isinstance(cached_plan, dict):
        generative_plan_payload = cached_plan
        generative_cache_hit = True
        generative_cache_layer = cached_layer
    else:
        generative_plan_payload = {
            "version": "v1",
            "status": "draft",
            "shell_mode": "resource_augmented_reader",
            "meta": {"cache_miss": True},
        }
    generative_plan_signature = _plan_signature(generative_plan_payload)

    experience_cache_hit = False
    experience_cache_layer = "none"
    experience_cache_key = _experience_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=int(payload.selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        generative_plan_signature=generative_plan_signature,
        user_intent=str(payload.user_intent or "").strip(),
        reader_profile=str(payload.reader_profile or "").strip(),
        focus_section_ids=list(payload.focus_section_ids or []),
    )
    cached_experience, cached_exp_layer = await _experience_plan_cache_get(experience_cache_key)
    if isinstance(cached_experience, dict):
        experience_plan_payload = cached_experience
        experience_cache_hit = True
        experience_cache_layer = cached_exp_layer
    else:
        can_derive_experience = isinstance(generative_plan_payload, dict) and str(generative_plan_payload.get("status") or "").strip() not in {"", "draft"}
        if can_derive_experience:
            built_experience = runtime.build_experience_plan(
                paper_id=int(paper.id),
                focus_page=focus_page,
                user_intent=str(payload.user_intent or "").strip(),
                reader_profile=str(payload.reader_profile or "").strip() or "curious_generalist",
                focus_section_ids=list(payload.focus_section_ids or []),
                compose_payload=composed_payload,
                generative_plan=generative_plan_payload,
            )
            experience_plan_payload = (
                built_experience
                if isinstance(built_experience, dict)
                else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
            )
            await _experience_plan_cache_set(experience_cache_key, experience_plan_payload)
            experience_cache_hit = True
            experience_cache_layer = "derived"
        else:
            seed_plan = runtime.build_seed_plan(
                page=focus_page,
                user_intent=str(payload.user_intent or "").strip(),
                compose_payload=composed_payload,
            )
            built_experience = runtime.build_experience_plan(
                paper_id=int(paper.id),
                focus_page=focus_page,
                user_intent=str(payload.user_intent or "").strip(),
                reader_profile=str(payload.reader_profile or "").strip() or "curious_generalist",
                focus_section_ids=list(payload.focus_section_ids or []),
                compose_payload=composed_payload,
                generative_plan=seed_plan,
            )
            experience_plan_payload = (
                built_experience
                if isinstance(built_experience, dict)
                else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
            )
            experience_meta = dict(experience_plan_payload.get("meta") or {})
            experience_meta["seed_plan"] = True
            experience_meta["cache_miss"] = True
            experience_plan_payload["meta"] = experience_meta
            await _experience_plan_cache_set(experience_cache_key, experience_plan_payload)
            experience_cache_hit = True
            experience_cache_layer = "derived_seed"

    return ReaderExperiencePlanResponse(
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
    )


@router.post(
    "/papers/{paper_id}/experience/plan",
    response_model=ReaderExperiencePlanResponse,
)
async def get_reader_experience_plan(
    paper_id: int,
    payload: ReaderExperiencePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    compose_service = get_literature_reader_compose_service()
    runtime = get_generative_reader_agent_runtime()
    focus_page = max(1, int(payload.focus_page or payload.page or 1))

    composed_payload, meta = await compose_service.build_or_get_composed_payload(
        db=db,
        user_id=int(current_user.id),
        paper=paper,
        page=focus_page,
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
    compose_status = "fallback" if str(composed_payload.get("status") or "").strip() == "fallback" else "done"
    compose_build_mode = str(composed_payload.get("build_mode") or meta.build_mode or "")
    compose_source_signature = str(composed_payload.get("source_signature") or meta.source_signature or "")
    source_sig_hash = str(meta.source_sig_hash or "")
    adjacent_page_context = await _build_experience_adjacent_page_context(
        compose_service=compose_service,
        paper=paper,
        focus_page=focus_page,
    )

    generative_cache_hit = False
    generative_cache_layer = "none"
    plan_cache_key = _generative_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        page=focus_page,
        selected_kb_id=int(payload.selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        user_intent=str(payload.user_intent or "").strip(),
    )
    generative_plan_payload: Dict[str, Any]
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_plan, cached_layer = await _generative_plan_cache_get(plan_cache_key)
        if isinstance(cached_plan, dict):
            generative_plan_payload = cached_plan
            generative_cache_hit = True
            generative_cache_layer = cached_layer
        else:
            tool_registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
                db=db,
                current_user=current_user,
                paper=paper,
                selected_kb_id=payload.selected_kb_id,
            )
            generated_plan = await runtime.build_plan(
                user_id=int(current_user.id),
                page=focus_page,
                user_intent=str(payload.user_intent or "").strip(),
                compose_payload=composed_payload,
                tool_registry=tool_registry,
                allowed_tool_names=sorted(list(allowed_tool_names)),
                adjacent_page_context=adjacent_page_context,
            )
            generative_plan_payload = (
                generated_plan
                if isinstance(generated_plan, dict)
                else json.loads(json.dumps(generated_plan, ensure_ascii=False, default=str))
            )
            await _generative_plan_cache_set(plan_cache_key, generative_plan_payload)
    else:
        tool_registry, allowed_tool_names = await _build_generative_reader_agent_tool_registry_for_paper(
            db=db,
            current_user=current_user,
            paper=paper,
            selected_kb_id=payload.selected_kb_id,
        )
        generated_plan = await runtime.build_plan(
            user_id=int(current_user.id),
            page=focus_page,
            user_intent=str(payload.user_intent or "").strip(),
            compose_payload=composed_payload,
            tool_registry=tool_registry,
            allowed_tool_names=sorted(list(allowed_tool_names)),
            adjacent_page_context=adjacent_page_context,
        )
        generative_plan_payload = (
            generated_plan
            if isinstance(generated_plan, dict)
            else json.loads(json.dumps(generated_plan, ensure_ascii=False, default=str))
        )
        await _generative_plan_cache_set(plan_cache_key, generative_plan_payload)
    generative_plan_signature = _plan_signature(generative_plan_payload)

    experience_cache_hit = False
    experience_cache_layer = "none"
    experience_cache_key = _experience_plan_cache_key(
        user_id=int(current_user.id),
        paper_id=int(paper.id),
        focus_page=focus_page,
        selected_kb_id=int(payload.selected_kb_id or 0),
        compose_source_signature=compose_source_signature,
        generative_plan_signature=generative_plan_signature,
        user_intent=str(payload.user_intent or "").strip(),
        reader_profile=str(payload.reader_profile or "").strip(),
        focus_section_ids=list(payload.focus_section_ids or []),
    )
    experience_plan_payload: Dict[str, Any]
    if not bool(payload.force_refresh) and not bool(payload.regenerate):
        cached_experience, cached_layer = await _experience_plan_cache_get(experience_cache_key)
        if isinstance(cached_experience, dict):
            experience_plan_payload = cached_experience
            experience_cache_hit = True
            experience_cache_layer = cached_layer
        else:
            built_experience = runtime.build_experience_plan(
                paper_id=int(paper.id),
                focus_page=focus_page,
                user_intent=str(payload.user_intent or "").strip(),
                reader_profile=str(payload.reader_profile or "").strip() or "curious_generalist",
                focus_section_ids=list(payload.focus_section_ids or []),
                compose_payload=composed_payload,
                generative_plan=generative_plan_payload,
            )
            experience_plan_payload = (
                built_experience
                if isinstance(built_experience, dict)
                else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
            )
            await _experience_plan_cache_set(experience_cache_key, experience_plan_payload)
    else:
        built_experience = runtime.build_experience_plan(
            paper_id=int(paper.id),
            focus_page=focus_page,
            user_intent=str(payload.user_intent or "").strip(),
            reader_profile=str(payload.reader_profile or "").strip() or "curious_generalist",
            focus_section_ids=list(payload.focus_section_ids or []),
            compose_payload=composed_payload,
            generative_plan=generative_plan_payload,
        )
        experience_plan_payload = (
            built_experience
            if isinstance(built_experience, dict)
            else json.loads(json.dumps(built_experience, ensure_ascii=False, default=str))
        )
        await _experience_plan_cache_set(experience_cache_key, experience_plan_payload)

    return ReaderExperiencePlanResponse(
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
    )


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
    )
    if not isinstance(composed_payload, dict):
        raise HTTPException(status_code=404, detail="No cached reader payload available for this page")
    payload_for_response = dict(composed_payload)
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()
    page_num = max(1, int(payload.page))

    async def event_generator():
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
            composed_payload, meta = await service.build_or_get_composed_payload(
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
            logger.info(
                "[Literature API] composed stream built "
                f"paper={paper_id} page={page_num} cache_hit={bool(meta.cache_hit)} cache_layer={meta.cache_layer} "
                f"build_mode={meta.build_mode} iterations={int(meta.iterations or 0)} degraded={bool(meta.degraded)} "
                f"stop_reason={str(meta.stop_reason or '')} status={str(composed_payload.get('status') or '')} "
                f"degraded_reason={str(composed_payload.get('degraded_reason') or '')}"
            )
            if await request.is_disconnected():
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
                        "ui_plan": first.get("ui_plan") or composed_payload.get("ui_plan") or {},
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
                            "ui_plan": row.get("ui_plan") or {},
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
                        "ui_plan": composed_payload.get("ui_plan") or {},
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
        except RenderPipelineContractError as exc:
            logger.warning(
                f"[Literature API] composed stream contract failed paper={paper_id}, page={page_num}: "
                f"stage={exc.stage} code={exc.code} message={exc.message}"
            )
            error_payload = exc.to_dict()
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paper = await _get_owned_paper_or_404(db, current_user, paper_id)
    service = get_literature_reader_compose_service()

    async def event_generator():
        try:
            result = await service.build_inline_answer_card(
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
            if bool(result.get("disabled")):
                yield _sse_payload(
                    "disabled",
                    {
                        "disabled": True,
                        "disabled_reason": str(result.get("disabled_reason") or "inline_query_contract_failed"),
                        "message": str(result.get("message") or "当前段落追问不可用，请改用右侧“询问”进行全文问答。"),
                    },
                )
                yield _sse_payload(
                    "done",
                    {
                        "disabled": True,
                        "disabled_reason": str(result.get("disabled_reason") or "inline_query_contract_failed"),
                        "sources": [],
                    },
                )
                return
            node = dict(result.get("node") or {})
            sources = list(result.get("sources") or [])
            answer_text = str((node.get("props") or {}).get("answer") or "")

            yield _sse_payload("start", {"page": int(payload.page), "node_id": str(payload.node_id)})
            for idx in range(0, len(answer_text), 42):
                if await request.is_disconnected():
                    return
                token = answer_text[idx : idx + 42]
                if token:
                    yield _sse_payload("token", {"text": token})
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
        if _mark_stale_document_timeout(doc):
            need_commit = True
        next_status, next_error, _ = _derive_link_status_from_document(doc)
        if link.status != next_status or (link.error_message or None) != (next_error or None):
            link.status = next_status
            link.error_message = next_error
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
        "请仅基于以下检索片段回答。\n"
        "若证据不足，请明确说明“无法从当前资料确定”。\n\n"
        f"检索片段：\n{joined_context}"
    )
    messages.append({"role": "user", "content": enriched_user_content})

    system_prompt = (
        "你是论文阅读问答助手。"
        "必须基于提供的检索片段回答，不得编造事实。"
        "回答尽量简洁准确，引用时使用 [序号] 标注。"
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
