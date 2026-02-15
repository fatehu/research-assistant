"""
文献管理 API 路由
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete, distinct, text
from sqlalchemy.orm import selectinload
from loguru import logger

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
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
    CollectionWithPapers,
    DownloadPdfRequest,
    LiteratureAskRequest,
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
from app.services.literature_service import PaperResult, get_literature_service
from app.services.llm_service import get_llm_service

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


def _sse_payload(event: str, data: Any) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


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


def _extract_page_from_metadata(metadata: Any) -> Optional[int]:
    if not isinstance(metadata, dict):
        return None
    for key in ("page", "page_number", "pdf_page", "page_index"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _ask_cache_key(user_id: int, kb_id: int, scope: str, target_id: int, question: str) -> str:
    q_hash = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()
    return f"lit:ask:v1:{user_id}:{kb_id}:{scope}:{target_id}:{q_hash}"


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
            dc.content,
            dc.chunk_index,
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
                DocumentChunk.content,
                DocumentChunk.chunk_index,
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
        content = (getattr(row, "content", "") or "").strip()
        snippet = content[:240]
        score_value = getattr(row, "score", None)
        score_float = float(score_value) if score_value is not None else 0.0
        results.append(
            {
                "idx": idx,
                "chunk_id": int(getattr(row, "chunk_id")),
                "document_id": int(getattr(row, "document_id")),
                "document_name": getattr(row, "document_name") or "未知文档",
                "page": page,
                "snippet": snippet,
                "content": content[:1600],
                "score": round(score_float, 4),
            }
        )
    return results


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
        # 创建存储目录
        upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
        pdf_dir = os.path.join(upload_dir, str(current_user.id), "papers")
        os.makedirs(pdf_dir, exist_ok=True)

        # 生成文件名
        safe_title = "".join(c for c in paper.title[:50] if c.isalnum() or c in " -_").strip()
        filename = f"{safe_title}_{paper.id}.pdf"
        pdf_path = os.path.join(pdf_dir, filename)

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
        upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
        pdf_dir = os.path.join(upload_dir, str(current_user.id), "papers")
        os.makedirs(pdf_dir, exist_ok=True)
        safe_title = "".join(c for c in paper.title[:50] if c.isalnum() or c in " -_").strip() or f"paper_{paper.id}"
        filename = f"{safe_title}_{paper.id}.pdf"
        pdf_path = os.path.join(pdf_dir, filename)
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
        elif doc.status == DocumentStatus.FAILED.value and link.status != KnowledgeLinkStatus.FAILED.value:
            link.status = KnowledgeLinkStatus.FAILED.value
            link.error_message = doc.error_message
            need_commit = True
        elif doc.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value} and link.status != KnowledgeLinkStatus.PROCESSING.value:
            link.status = KnowledgeLinkStatus.PROCESSING.value
            need_commit = True

    if need_commit:
        await db.commit()

    return links


# ============ 询问（SSE） ============

@router.post("/ask")
async def literature_ask(
    payload: LiteratureAskRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = payload.scope
    if scope == AskScope.PAPER.value:
        if payload.paper_id is None:
            raise HTTPException(status_code=400, detail="scope=paper 时必须提供 paper_id")
        paper = await _get_owned_paper_or_404(db, current_user, int(payload.paper_id))
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
    if not ready_links:
        ready_details.update(
            {
                "scope": scope,
                "knowledge_base_id": kb.id,
                "paper_ids": paper_ids,
            }
        )
        raise _knowledge_not_ready_error(ready_details)

    document_ids = sorted({int(link.document_id) for link in ready_links if link.document_id})
    if not document_ids:
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
    await db.commit()
    await db.refresh(user_message)

    cache_key = _ask_cache_key(
        user_id=current_user.id,
        kb_id=kb.id,
        scope=scope,
        target_id=target_id,
        question=payload.question,
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
        await db.commit()
        await db.refresh(assistant)

        async def cached_stream():
            yield _sse_payload("start", {"session_id": session.id, "cache_hit": True})
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
                DocumentChunk.content,
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
            sources.append(
                {
                    "idx": idx,
                    "chunk_id": int(getattr(row, "id")),
                    "document_id": int(getattr(row, "document_id")),
                    "document_name": getattr(row, "original_filename") or "未知文档",
                    "page": _extract_page_from_metadata(metadata),
                    "snippet": content[:240],
                    "content": content[:1600],
                    "score": 0.0,
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
            "chunk_id": source["chunk_id"],
            "document_id": source["document_id"],
            "document_name": source["document_name"],
            "page": source.get("page"),
            "snippet": source["snippet"],
            "score": source["score"],
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
