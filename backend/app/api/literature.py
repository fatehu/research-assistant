"""
文献管理 API 路由
"""
import os
import uuid
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete
from sqlalchemy.orm import selectinload
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.literature import Paper, PaperCollection, PaperSearchHistory, paper_collection_association
from app.models.knowledge import KnowledgeBase, Document, DocumentStatus
from app.schemas.literature import (
    PaperResponse, PaperCreate, PaperUpdate,
    PaperSearchResult, PaperSearchResponse,
    CollectionResponse, CollectionCreate, CollectionUpdate, CollectionWithPapers,
    AddToCollectionRequest, RemoveFromCollectionRequest,
    SavePaperFromSearchRequest, DownloadPdfRequest,
    SearchHistoryResponse
)
from app.services.literature_service import get_literature_service, PaperResult
from app.services.document_service import document_processor

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
    
    if paper.pdf_downloaded and paper.pdf_path:
        return {"message": "PDF 已下载", "pdf_path": paper.pdf_path}
    
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
        kb_stmt = select(KnowledgeBase).where(
            and_(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.user_id == current_user.id
            )
        )
        kb_result = await db.execute(kb_stmt)
        kb = kb_result.scalar_one_or_none()
        
        if kb:
            # 创建文档记录
            doc = Document(
                knowledge_base_id=knowledge_base_id,
                filename=filename,
                original_filename=filename,
                file_path=pdf_path,
                file_size=os.path.getsize(pdf_path),
                file_type="pdf",
                mime_type="application/pdf",
                status=DocumentStatus.PENDING.value,
                metadata_={"paper_id": paper.id, "title": paper.title}
            )
            db.add(doc)
            await db.flush()
            
            paper.knowledge_base_id = knowledge_base_id
            paper.document_id = doc.id
            
            # 后台处理文档
            if background_tasks:
                background_tasks.add_task(
                    process_document_background,
                    doc.id,
                    knowledge_base_id,
                    pdf_path
                )
    
    await db.commit()
    
    return {
        "message": "PDF 下载成功",
        "pdf_path": pdf_path,
        "knowledge_base_id": knowledge_base_id,
        "document_id": paper.document_id
    }


async def process_document_background(doc_id: int, kb_id: int, file_path: str):
    """后台处理文档"""
    # 这里需要实现文档处理逻辑
    # 由于需要数据库会话，这个函数需要在实际使用时完善
    pass


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
