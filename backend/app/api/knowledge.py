"""
知识库 API 路由 - 支持共享知识库访问（可选）
"""
import os
import json
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import Any, List, Optional, Set
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_, and_, tuple_
from loguru import logger

from app.config import settings
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
from app.services.smart_chunking_service import (
    SmartChunkingService,
    ChunkConfig,
    ChunkingStrategy,
    ChunkLevel,
    get_preset_config,
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


def _sse_payload(event: str, data: Any) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


async def _publish_document_status_event(
    *,
    user_id: int,
    kb_id: int,
    doc: Document,
) -> None:
    payload = {
        "event": "document_status",
        "data": {
            "kb_id": int(kb_id),
            "document_id": int(doc.id),
            "status": str(doc.status),
            "chunk_count": int(doc.chunk_count or 0),
            "error_message": (doc.error_message or None),
            "updated_at": (doc.updated_at or datetime.utcnow()).isoformat(),
        },
    }
    try:
        await publish_status_event(build_status_channel_for_user(int(user_id)), payload)
    except Exception as exc:  # pragma: no cover - push failures should not break main path
        logger.warning(f"[Knowledge API] 发布文档状态事件失败 doc={doc.id}: {exc}")


# ========== 可用嵌入模型注册表 ==========

# 面向用户的模型描述信息
EMBEDDING_MODEL_CATALOG = [
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
        settings.local_embedding_model
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


@router.get("/events/stream")
async def stream_knowledge_status_events(
    request: Request,
    kb_id: Optional[int] = Query(default=None, ge=1, description="可选：仅订阅指定知识库"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """知识库状态事件流（SSE）。"""
    if kb_id is not None:
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
    await _publish_document_status_event(
        user_id=int(current_user.id),
        kb_id=int(kb_id),
        doc=doc,
    )
    
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
            
            # 更新状态为处理中
            doc.status = DocumentStatus.PROCESSING.value
            await db.commit()
            await _emit_status()
            
            # 创建处理器
            processor = get_document_processor(chunk_size, chunk_overlap)
            
            # 嵌入模型与维度在分块完成后按策略动态决策
            embedding_svc = None
            logger.info(
                f"[doc:{task_trace_id}] 文档开始处理，嵌入维度将按规模自适应, "
                f"elapsed={_task_elapsed_ms():.2f}ms"
            )
            
            # 提取文本
            extract_started_at = time.perf_counter()
            logger.info(f"[doc:{task_trace_id}] 开始提取文档文本: {doc_id}")
            text = await processor.extract_text(doc.file_path, doc.file_type)
            logger.info(
                f"[doc:{task_trace_id}] 文本提取完成: chars={len(text)}, "
                f"stage_ms={(time.perf_counter() - extract_started_at) * 1000:.2f}, "
                f"elapsed={_task_elapsed_ms():.2f}ms"
            )
            
            if not text.strip():
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档内容为空"
                await db.commit()
                await _emit_status()
                return
            
            doc.content = text
            doc.content_hash = processor.compute_hash(text)
            doc.char_count = len(text)
            doc.token_count = processor.estimate_tokens(text)
            if doc.file_type.lower() == "pdf" and processor.last_pdf_extractor:
                current_metadata = dict(doc.metadata_) if doc.metadata_ else {}
                current_metadata["pdf_extractor"] = processor.last_pdf_extractor
                doc.metadata_ = current_metadata
            
            # 获取知识库以读取分块配置
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if not kb:
                logger.error(f"知识库不存在: {doc.knowledge_base_id}")
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "知识库不存在"
                await db.commit()
                await _emit_status()
                return

            # 分片
            chunk_started_at = time.perf_counter()
            logger.info(f"[doc:{task_trace_id}] 开始智能分块: {doc_id}")
            
            # 准备配置
            kb_config = kb.chunking_config
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

            # 执行分块
            smart_service = SmartChunkingService()
            result = await smart_service.chunk_document(text, chunk_config, doc.file_type)
            logger.info(
                f"[doc:{task_trace_id}] 智能分块完成: 层级分块={'是' if result.get('hierarchy') else '否'}, "
                f"stage_ms={(time.perf_counter() - chunk_started_at) * 1000:.2f}, "
                f"elapsed={_task_elapsed_ms():.2f}ms"
            )
            
            # ===== [Fix 1] 收集分块 =====
            # 核心原则：paragraph 级作为检索单元（生成 embedding）
            #          section/document 级作为上下文参考（不生成 embedding，存入文档 metadata）
            
            primary_chunks = []   # paragraph 级，用于检索
            context_chunks = []   # section/document 级，仅存元数据
            
            if result.get("hierarchy"):
                hierarchy = result["hierarchy"]
                for level, level_chunks in hierarchy.items():
                    for chunk_data in level_chunks:
                        # 确保 chunk_data 是 dict 格式
                        if not isinstance(chunk_data, dict):
                            chunk_data = smart_service._chunk_to_dict(chunk_data) if hasattr(chunk_data, 'metadata') else chunk_data
                        
                        chunk_level = chunk_data.get("metadata", {}).get("level", "paragraph")
                        if chunk_level == "paragraph":
                            primary_chunks.append(chunk_data)
                        else:
                            context_chunks.append(chunk_data)
            else:
                # result["chunks"] 是 SmartChunk 对象，转为 dict
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
                            "has_citations": val.metadata.has_citations,
                            "position_ratio": val.metadata.position_ratio,
                            "keywords": val.metadata.keywords,
                        }
                    })
            
            chunks_to_save = primary_chunks
            
            if not chunks_to_save:
                # 降级：如果 primary_chunks 为空但 context_chunks 有内容，使用 context
                if context_chunks:
                    chunks_to_save = context_chunks
                    logger.warning(f"文档 {doc_id} 没有段落级分块，降级使用章节级")
            
            if not chunks_to_save:
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档分片失败：无有效分块"
                await db.commit()
                await _emit_status()
                return
                
            # 按位置排序
            chunks_to_save.sort(key=lambda x: x["start_char"])

            # 生成嵌入向量
            logger.info(f"[doc:{task_trace_id}] 开始生成嵌入向量: {doc_id}, {len(chunks_to_save)} 个分片")
            embedding_model = (kb.embedding_model or "").strip() or settings.local_embedding_model
            policy_service = get_embedding_dimension_policy_service()
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
            smart_id_map = {} # str_id -> DocumentChunk
            
            for i, chunk_data in enumerate(chunks_to_save):
                meta = chunk_data["metadata"]
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
                    section_type=meta.get("section_type"),
                    section_title=meta.get("section_title"),
                    has_citations=meta.get("has_citations", False),
                    metadata_={
                        "position_ratio": meta.get("position_ratio"),
                        "keywords": meta.get("keywords"),
                        "original_id": chunk_data["id"] 
                    }
                )
                db.add(chunk)
                smart_id_map[chunk_data["id"]] = chunk
            
            # [Fix 12 Correction] 同时也保存 context_chunks (section/document)，但不生成 embedding
            # 这样可以在 search 时通过 parent_id 回溯到父级 chunk
            for chunk_data in context_chunks:
                meta = chunk_data["metadata"]
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
                    section_type=meta.get("section_type"),
                    section_title=meta.get("section_title"),
                    has_citations=meta.get("has_citations", False),
                    metadata_={
                        "position_ratio": meta.get("position_ratio"),
                        "keywords": meta.get("keywords"),
                        "original_id": chunk_data["id"] 
                    }
                )
                db.add(chunk)
                smart_id_map[chunk_data["id"]] = chunk
            
            await db.flush() # Generate IDs
            
            # 更新父子关系
            await db.flush() # Generate IDs
            
            # 更新父子关系 (现在 context chunks 也在 smart_id_map 中了，可以链接)
            all_chunks = primary_chunks + context_chunks
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
                        "section_type": ctx.get("metadata", {}).get("section_type"),
                        "section_title": ctx.get("metadata", {}).get("section_title"),
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
            
            # 更新知识库统计
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            should_schedule_rebuild = False
            if kb:
                kb.document_count = (kb.document_count or 0) + 1
                kb.total_chunks = (kb.total_chunks or 0) + len(chunks_to_save)
                kb.total_tokens = (kb.total_tokens or 0) + doc.token_count
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
    
    stale_timeout_seconds = max(
        int(getattr(settings, "document_processing_stale_timeout_seconds", 7200)),
        60,
    )
    last_updated_at = doc.updated_at or doc.created_at
    if is_stale_processing_status(
        status=doc.status,
        last_updated_at=last_updated_at,
        timeout_seconds=stale_timeout_seconds,
    ):
        previous_error = (doc.error_message or "").strip()
        timeout_error = build_timeout_error_message(stale_timeout_seconds)
        doc.status = DocumentStatus.FAILED.value
        doc.error_message = f"{previous_error} | {timeout_error}" if previous_error else timeout_error
        await db.commit()
        await db.refresh(doc)
        await _publish_document_status_event(
            user_id=int(current_user.id),
            kb_id=int(kb_id),
            doc=doc,
        )
        logger.warning(
            "文档状态因处理超时自动失败: "
            f"doc_id={doc.id}, kb_id={kb_id}, last_updated_at={last_updated_at}, "
            f"timeout_seconds={stale_timeout_seconds}"
        )

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
            reranked = await reranker.rerank(
                query=request.query,
                documents=[candidate.row.content for candidate in fused_candidates],
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
