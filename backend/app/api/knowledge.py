"""
知识库 API 路由 - 支持共享知识库访问（可选）
"""
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import List, Optional, Set
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_, and_
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
from app.services.embedding_service import get_embedding_service, get_embedding_service_for_model, MODEL_DIMENSIONS
from app.services.smart_chunking_service import (
    SmartChunkingService,
    ChunkConfig,
    ChunkingStrategy,
    ChunkLevel,
    get_preset_config,
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
            
            # [Revert] 统一使用默认嵌入模型
            embedding_svc = get_embedding_service()
            logger.info(f"文档 {doc_id} 使用嵌入模型: {embedding_svc._get_model()}")
            
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
            
            # 获取知识库以读取分块配置
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if not kb:
                logger.error(f"知识库不存在: {doc.knowledge_base_id}")
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "知识库不存在"
                await db.commit()
                return

            # 分片
            logger.info(f"开始智能分块: {doc_id}")
            
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
                    logger.warning(f"文档 {doc_id} 没有 paragraph 级分块，降级使用 section 级")
            
            if not chunks_to_save:
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = "文档分片失败：无有效分块"
                await db.commit()
                return
                
            # 按位置排序
            chunks_to_save.sort(key=lambda x: x["start_char"])
            
            # 生成嵌入向量
            logger.info(f"开始生成嵌入向量: {doc_id}, {len(chunks_to_save)} 个分片")
            chunk_texts = [c["content"] for c in chunks_to_save]
            embeddings = await processor.embed_chunks(chunk_texts, embedding_svc=embedding_svc)
            
            # 创建分片记录
            smart_id_map = {} # str_id -> DocumentChunk
            
            for i, chunk_data in enumerate(chunks_to_save):
                meta = chunk_data["metadata"]
                
                chunk = DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    content=chunk_data["content"],
                    chunk_index=i,
                    start_char=chunk_data["start_char"],
                    end_char=chunk_data["end_char"],
                    embedding=embeddings[i] if i < len(embeddings) else None,
                    embedding_model=embedding_svc._get_model(),
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
                    chunk_index=-1, # context chunk index 设为 -1 或其他标记
                    start_char=chunk_data["start_char"],
                    end_char=chunk_data["end_char"],
                    embedding=None, # 不生成 embedding
                    embedding_model=embedding_svc._get_model(),
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
            doc.chunk_count = len(chunks_to_save)
            doc.processed_at = datetime.utcnow()
            
            # 更新知识库统计
            kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
            if kb:
                kb.document_count = (kb.document_count or 0) + 1
                kb.total_chunks = (kb.total_chunks or 0) + len(chunks_to_save)
                kb.total_tokens = (kb.total_tokens or 0) + doc.token_count
            
            await db.commit()
            logger.info(f"文档处理完成: {doc_id}, {len(chunks_to_save)} 个分片")
            
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
    start_time = time.time()
    
    # [Revert] 统一使用默认嵌入模型，忽略知识库配置
    embedding_svc = get_embedding_service()
    
    # 生成查询向量
    try:
        query_embedding = await embedding_svc.embed_text(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成查询向量失败: {str(e)}")
    
    if not query_embedding:
        raise HTTPException(status_code=400, detail="无法生成查询向量")
    
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
    from sqlalchemy import text
    
    vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
    
    # [Fix 12] 动态构建 WHERE 条件，支持 chunk_level/section_type 过滤
    where_clauses = [
        "dc.knowledge_base_id = ANY(:kb_ids)",
        "dc.embedding IS NOT NULL",
        "(dc.embedding <=> :query_vector) <= :distance_threshold",
    ]
    params = {
        "query_vector": vector_str,
        "kb_ids": kb_ids,
        "distance_threshold": distance_threshold,
        "top_k": request.top_k,
    }
    
    # chunk_level 过滤（默认只搜索 paragraph 级）
    if request.chunk_level and request.chunk_level != "all":
        where_clauses.append("dc.chunk_level = :chunk_level")
        params["chunk_level"] = request.chunk_level
    
    # section_type 过滤（如 "只搜方法部分"）
    if request.section_type:
        where_clauses.append("dc.section_type = :section_type")
        params["section_type"] = request.section_type
    
    where_sql = " AND ".join(where_clauses)
    
    sql = text(f"""
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
            1 - (dc.embedding <=> :query_vector) as similarity,
            d.original_filename as document_name,
            kb.name as knowledge_base_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
        WHERE {where_sql}
        ORDER BY dc.embedding <=> :query_vector
        LIMIT :top_k
    """)
    
    result = await db.execute(sql, params)
    rows = result.fetchall()
    
    # 构建结果
    results = []
    parent_ids_to_fetch = set()
    
    for row in rows:
        item = SearchResultItem(
            chunk_id=row.id,
            document_id=row.document_id,
            knowledge_base_id=row.knowledge_base_id,
            document_name=row.document_name or "未知文档",
            knowledge_base_name=row.knowledge_base_name or "未知知识库",
            content=row.content,
            score=round(float(row.similarity), 4),
            chunk_index=row.chunk_index,
            metadata=row.metadata or {},
            chunk_level=getattr(row, 'chunk_level', None),
            section_type=getattr(row, 'section_type', None),
            section_title=getattr(row, 'section_title', None),
        )
        results.append(item)
        
        # [Fix 12] 收集需要回溯的父级 chunk
        parent_id = getattr(row, 'parent_chunk_id', None)
        if request.include_parent_context and parent_id:
            parent_ids_to_fetch.add((len(results) - 1, parent_id))
    
    # [Fix 12] 批量回溯父级上下文
    if parent_ids_to_fetch:
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
    
    search_time = (time.time() - start_time) * 1000
    
    logger.info(f"向量搜索完成: query='{request.query[:50]}...', results={len(results)}, time={search_time:.2f}ms")
    
    return SearchResponse(
        query=request.query,
        results=results,
        total=len(results),
        search_time_ms=round(search_time, 2)
    )
