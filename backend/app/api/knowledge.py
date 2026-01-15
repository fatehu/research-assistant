"""
知识库 API 路由
"""
import os
import shutil
import time
import uuid
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk, DocumentStatus
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
)
from app.services.document_service import get_document_processor
from app.services.embedding_service import get_embedding_service

router = APIRouter()

# 文件上传目录
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
    
    return KnowledgeBaseListResponse(
        items=[KnowledgeBaseResponse.model_validate(item) for item in items],
        total=total
    )


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建知识库"""
    kb = KnowledgeBase(
        user_id=current_user.id,
        name=data.name,
        description=data.description,
        embedding_model=data.embedding_model,
        chunk_size=data.chunk_size,
        chunk_overlap=data.chunk_overlap,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    
    logger.info(f"用户 {current_user.id} 创建知识库: {kb.name}")
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
        if doc.file_path and os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
            except:
                pass
    
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档列表"""
    # 验证知识库权限
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 查询总数
    count_query = select(func.count(Document.id)).where(Document.knowledge_base_id == kb_id)
    total = (await db.execute(count_query)).scalar() or 0
    
    # 查询列表
    query = (
        select(Document)
        .where(Document.knowledge_base_id == kb_id)
        .order_by(Document.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    items = result.scalars().all()
    
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(item) for item in items],
        total=total
    )


@router.post("/knowledge-bases/{kb_id}/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    kb_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
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
    
    # 保存文件
    file_id = str(uuid.uuid4())
    file_name = f"{file_id}.{file_type}"
    file_path = os.path.join(UPLOAD_DIR, str(current_user.id), str(kb_id))
    os.makedirs(file_path, exist_ok=True)
    
    full_path = os.path.join(file_path, file_name)
    
    try:
        content = await file.read()
        with open(full_path, 'wb') as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {str(e)}")
    
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
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    
    # 后台处理文档
    background_tasks.add_task(
        process_document_task,
        doc.id,
        kb.chunk_size,
        kb.chunk_overlap,
    )
    
    logger.info(f"用户 {current_user.id} 上传文档: {file.filename} -> {doc.id}")
    
    return DocumentUploadResponse(
        id=doc.id,
        filename=doc.filename,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        file_type=doc.file_type,
        status=doc.status,
        message="文件上传成功，正在处理中..."
    )


async def process_document_task(doc_id: int, chunk_size: int, chunk_overlap: int):
    """后台处理文档任务"""
    from app.core.database import async_session_factory
    
    async with async_session_factory() as db:
        try:
            # 获取文档
            doc = await db.get(Document, doc_id)
            if not doc:
                return
            
            # 更新状态为处理中
            doc.status = DocumentStatus.PROCESSING.value
            await db.commit()
            
            # 创建处理器
            processor = get_document_processor(chunk_size, chunk_overlap)
            embedding_svc = get_embedding_service()
            
            # 提取文本
            logger.info(f"开始提取文档文本: {doc_id}")
            text = await processor.extract_text(doc.file_path, doc.file_type)
            
            if not text.strip():
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档内容为空"
                await db.commit()
                return
            
            doc.content = text
            doc.content_hash = processor.compute_hash(text)
            doc.char_count = len(text)
            doc.token_count = processor.estimate_tokens(text)
            
            # 分片
            logger.info(f"开始分片: {doc_id}")
            chunks = processor.chunk_text(text)
            
            if not chunks:
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档分片失败"
                await db.commit()
                return
            
            # 生成嵌入向量
            logger.info(f"开始生成嵌入向量: {doc_id}, {len(chunks)} 个分片")
            chunk_texts = [c[0] for c in chunks]
            embeddings = await processor.embed_chunks(chunk_texts)
            
            # 创建分片记录
            for i, (chunk_text, start_char, end_char) in enumerate(chunks):
                chunk = DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    content=chunk_text,
                    chunk_index=i,
                    start_char=start_char,
                    end_char=end_char,
                    embedding=embeddings[i] if i < len(embeddings) else None,
                    embedding_model=embedding_svc._get_model(),  # 正确存储模型名称
                    char_count=len(chunk_text),
                    token_count=processor.estimate_tokens(chunk_text),
                )
                db.add(chunk)
            
            # 更新文档状态
            doc.status = DocumentStatus.COMPLETED.value
            doc.chunk_count = len(chunks)
            doc.processed_at = datetime.utcnow()
            
            # 更新知识库统计
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if kb:
                kb.document_count = (kb.document_count or 0) + 1
                kb.total_chunks = (kb.total_chunks or 0) + len(chunks)
                kb.total_tokens = (kb.total_tokens or 0) + doc.token_count
            
            await db.commit()
            logger.info(f"文档处理完成: {doc_id}, {len(chunks)} 个分片")
            
        except Exception as e:
            logger.error(f"处理文档失败 {doc_id}: {e}")
            try:
                doc = await db.get(Document, doc_id)
                if doc:
                    doc.status = DocumentStatus.FAILED.value
                    doc.error_message = str(e)
                    await db.commit()
            except:
                pass


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
    
    return DocumentDetailResponse(
        id=doc.id,
        knowledge_base_id=doc.knowledge_base_id,
        filename=doc.filename,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        file_type=doc.file_type,
        status=doc.status,
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
    
    # 删除文件
    if doc.file_path and os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except:
            pass
    
    # 更新知识库统计
    kb.document_count = max(0, (kb.document_count or 0) - 1)
    kb.total_chunks = max(0, (kb.total_chunks or 0) - (doc.chunk_count or 0))
    kb.total_tokens = max(0, (kb.total_tokens or 0) - (doc.token_count or 0))
    
    await db.delete(doc)
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
    progress = 0
    message = "等待处理"
    
    if doc.status == DocumentStatus.PENDING.value:
        progress = 0
        message = "等待处理"
    elif doc.status == DocumentStatus.PROCESSING.value:
        progress = 50
        message = "正在处理..."
    elif doc.status == DocumentStatus.COMPLETED.value:
        progress = 100
        message = "处理完成"
    elif doc.status == DocumentStatus.FAILED.value:
        progress = 0
        message = "处理失败"
    
    return ProcessingStatus(
        document_id=doc.id,
        status=doc.status,
        progress=progress,
        message=message,
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    向量搜索 - 使用 pgvector 进行高效相似度检索
    
    pgvector 使用余弦距离 (1 - 余弦相似度) 进行搜索
    <=> 操作符返回余弦距离，越小越相似
    """
    start_time = time.time()
    
    embedding_svc = get_embedding_service()
    
    # 生成查询向量
    try:
        query_embedding = await embedding_svc.embed_text(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成查询向量失败: {str(e)}")
    
    if not query_embedding:
        raise HTTPException(status_code=400, detail="无法生成查询向量")
    
    # 确定要搜索的知识库
    if request.knowledge_base_ids:
        # 验证知识库权限
        for kb_id in request.knowledge_base_ids:
            kb = await db.get(KnowledgeBase, kb_id)
            if not kb or (kb.user_id != current_user.id and not kb.is_public):
                raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在或无权限")
        kb_ids = request.knowledge_base_ids
    else:
        # 搜索用户所有知识库
        kb_query = select(KnowledgeBase.id).where(KnowledgeBase.user_id == current_user.id)
        kb_result = await db.execute(kb_query)
        kb_ids = [row[0] for row in kb_result.fetchall()]
        
        if not kb_ids:
            return SearchResponse(
                query=request.query,
                results=[],
                total=0,
                search_time_ms=(time.time() - start_time) * 1000
            )
    
    # 使用 pgvector 进行向量相似度搜索
    # <=> 是余弦距离运算符 (cosine distance = 1 - cosine similarity)
    # 距离越小，相似度越高
    # 我们需要将距离阈值转换为：score_threshold 对应 distance_threshold = 1 - score_threshold
    distance_threshold = 1 - request.score_threshold
    
    # 构建 pgvector 原生查询
    # 使用 ORDER BY embedding <=> query_embedding 进行排序
    # HNSW 索引会加速这个查询
    from sqlalchemy import text
    
    vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
    
    sql = text("""
        SELECT 
            dc.id,
            dc.document_id,
            dc.knowledge_base_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            1 - (dc.embedding <=> :query_vector) as similarity,
            d.original_filename as document_name,
            kb.name as knowledge_base_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
        WHERE dc.knowledge_base_id = ANY(:kb_ids)
            AND dc.embedding IS NOT NULL
            AND (dc.embedding <=> :query_vector) <= :distance_threshold
        ORDER BY dc.embedding <=> :query_vector
        LIMIT :top_k
    """)
    
    result = await db.execute(sql, {
        "query_vector": vector_str,
        "kb_ids": kb_ids,
        "distance_threshold": distance_threshold,
        "top_k": request.top_k
    })
    rows = result.fetchall()
    
    # 构建结果
    results = []
    for row in rows:
        results.append(SearchResultItem(
            chunk_id=row.id,
            document_id=row.document_id,
            knowledge_base_id=row.knowledge_base_id,
            document_name=row.document_name or "未知文档",
            knowledge_base_name=row.knowledge_base_name or "未知知识库",
            content=row.content,
            score=round(float(row.similarity), 4),
            chunk_index=row.chunk_index,
            metadata=row.metadata or {},
        ))
    
    search_time = (time.time() - start_time) * 1000
    
    logger.info(f"向量搜索完成: query='{request.query[:50]}...', results={len(results)}, time={search_time:.2f}ms")
    
    return SearchResponse(
        query=request.query,
        results=results,
        total=len(results),
        search_time_ms=round(search_time, 2)
    )
