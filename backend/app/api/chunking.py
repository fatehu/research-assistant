"""
智能分块 API 路由

提供分块配置、预览、测试等功能
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.knowledge import KnowledgeBase
from app.schemas.chunking import (
    ChunkingConfigCreate,
    ChunkingConfigResponse,
    ChunkingPresetEnum,
    ChunkingResultResponse,
    DocumentChunkRequest,
    PresetListResponse,
    PresetDescription,
    PRESET_DESCRIPTIONS,
    SmartChunkResponse,
    ChunkMetadataResponse,
    ChunkingStatsResponse,
    ChunkLevelEnum,
)
from app.services.smart_chunking_service import (
    SmartChunkingService,
    ChunkConfig,
    ChunkingStrategy,
    ChunkLevel,
    get_preset_config,
)

router = APIRouter()


# ============== 预设配置 ==============

@router.get("/presets", response_model=PresetListResponse)
async def list_chunking_presets():
    """
    获取所有预设分块配置
    
    返回可用的预设配置列表及其说明
    """
    return PresetListResponse(presets=PRESET_DESCRIPTIONS)


@router.get("/presets/{preset_name}", response_model=ChunkingConfigResponse)
async def get_chunking_preset(
    preset_name: ChunkingPresetEnum
):
    """
    获取指定预设的详细配置
    
    参数:
        preset_name: 预设名称 (default/fast/precise/academic/deep)
    """
    config = get_preset_config(preset_name.value)
    
    return ChunkingConfigResponse(
        strategy=config.strategy.value,
        base_chunk_size=config.base_chunk_size,
        chunk_overlap=config.chunk_overlap,
        semantic_threshold=config.semantic_threshold,
        min_semantic_chunk=config.min_semantic_chunk,
        max_semantic_chunk=config.max_semantic_chunk,
        enable_hierarchical=config.enable_hierarchical,
        hierarchy_levels=[ChunkLevelEnum(l.value) for l in config.hierarchy_levels],
        detect_academic_structure=config.detect_academic_structure,
        preserve_citations=config.preserve_citations,
        name=preset_name.value,
        is_default=preset_name == ChunkingPresetEnum.DEFAULT,
    )


# ============== 分块预览/测试 ==============

@router.post("/preview", response_model=ChunkingResultResponse)
async def preview_chunking(
    request: DocumentChunkRequest,
    current_user: User = Depends(get_current_user),
):
    """
    预览分块效果
    
    在实际应用到知识库之前，预览分块结果
    
    参数:
        text: 要分块的文本
        config: 分块配置（可选）
        preset: 预设名称（可选，优先级低于 config）
        file_type: 文件类型
    """
    # 确定使用的配置
    if request.config:
        config = _convert_to_chunk_config(request.config)
    elif request.preset:
        config = get_preset_config(request.preset.value)
    else:
        config = ChunkConfig()  # 默认配置
    
    # 执行分块
    service = SmartChunkingService()
    
    try:
        result = await service.chunk_document(
            text=request.text,
            config=config,
            file_type=request.file_type
        )
    except Exception as e:
        logger.error(f"分块预览失败: {e}")
        raise HTTPException(status_code=500, detail=f"分块失败: {str(e)}")
    
    # 转换结果
    return _convert_to_response(result)


@router.post("/analyze")
async def analyze_document(
    request: DocumentChunkRequest,
    current_user: User = Depends(get_current_user),
):
    """
    分析文档结构
    
    分析文档的结构特征，推荐最佳分块策略
    
    返回:
        - is_academic: 是否为学术文档
        - detected_sections: 检测到的章节类型
        - recommended_strategy: 推荐的分块策略
        - document_stats: 文档统计信息
    """
    # [Fix 7] 通过公开方法 analyze_document 获取结果，不再直接调用私有方法
    service = SmartChunkingService()
    return service.analyze_document(request.text)


@router.post("/compare")
async def compare_strategies(
    request: DocumentChunkRequest,
    strategies: List[ChunkingPresetEnum] = Query(
        default=[ChunkingPresetEnum.FAST, ChunkingPresetEnum.PRECISE, ChunkingPresetEnum.DEEP],
        description="要比较的策略列表"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    比较不同分块策略的效果
    
    对同一文档使用不同策略分块，比较结果
    """
    results = {}
    service = SmartChunkingService()
    
    for strategy in strategies:
        config = get_preset_config(strategy.value)
        
        try:
            result = await service.chunk_document(
                text=request.text,
                config=config,
                file_type=request.file_type
            )
            
            results[strategy.value] = {
                "strategy": strategy.value,
                "stats": result["stats"],
                "sample_chunks": [
                    {
                        "content": c.content[:200] + "..." if len(c.content) > 200 else c.content,
                        "length": len(c.content),
                        "has_citations": c.metadata.has_citations,
                    }
                    for c in result["chunks"][:3]  # 只返回前3个块作为样例
                ],
                "total_chunks": len(result["chunks"]),
            }
        except Exception as e:
            results[strategy.value] = {
                "strategy": strategy.value,
                "error": str(e)
            }
    
    return {
        "document_length": len(request.text),
        "comparisons": results,
        "recommendation": _get_recommendation(results)
    }


# ============== 知识库分块配置 ==============

@router.get("/knowledge-base/{kb_id}/config", response_model=ChunkingConfigResponse)
async def get_kb_chunking_config(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    获取知识库的分块配置
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 从知识库元数据中获取分块配置
    chunking_config = kb.metadata_.get("chunking_config", {}) if kb.metadata_ else {}
    
    return ChunkingConfigResponse(
        strategy=chunking_config.get("strategy", "hybrid"),
        base_chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        semantic_threshold=chunking_config.get("semantic_threshold", 0.75),
        min_semantic_chunk=chunking_config.get("min_semantic_chunk", 100),
        max_semantic_chunk=chunking_config.get("max_semantic_chunk", 1500),
        enable_hierarchical=chunking_config.get("enable_hierarchical", True),
        hierarchy_levels=chunking_config.get("hierarchy_levels", ["paragraph", "section"]),
        detect_academic_structure=chunking_config.get("detect_academic_structure", True),
        preserve_citations=chunking_config.get("preserve_citations", True),
    )


@router.put("/knowledge-base/{kb_id}/config", response_model=ChunkingConfigResponse)
async def update_kb_chunking_config(
    kb_id: int,
    config: ChunkingConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    更新知识库的分块配置
    
    注意: 更改配置后，需要重新处理已上传的文档才能应用新配置
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 更新基础配置
    kb.chunk_size = config.base_chunk_size
    kb.chunk_overlap = config.chunk_overlap
    
    # 更新高级配置到元数据
    chunking_config = {
        "strategy": config.strategy.value,
        "semantic_threshold": config.semantic_threshold,
        "min_semantic_chunk": config.min_semantic_chunk,
        "max_semantic_chunk": config.max_semantic_chunk,
        "enable_hierarchical": config.enable_hierarchical,
        "hierarchy_levels": [l.value for l in config.hierarchy_levels],
        "detect_academic_structure": config.detect_academic_structure,
        "preserve_citations": config.preserve_citations,
    }
    
    # Update metadata with new dict to trigger SQLAlchemy change
    current_metadata = dict(kb.metadata_) if kb.metadata_ else {}
    current_metadata["chunking_config"] = chunking_config
    kb.metadata_ = current_metadata
    
    await db.commit()
    await db.refresh(kb)
    
    logger.info(f"用户 {current_user.id} 更新了知识库 {kb_id} 的分块配置")
    
    return ChunkingConfigResponse(
        strategy=config.strategy,
        base_chunk_size=config.base_chunk_size,
        chunk_overlap=config.chunk_overlap,
        semantic_threshold=config.semantic_threshold,
        min_semantic_chunk=config.min_semantic_chunk,
        max_semantic_chunk=config.max_semantic_chunk,
        enable_hierarchical=config.enable_hierarchical,
        hierarchy_levels=config.hierarchy_levels,
        detect_academic_structure=config.detect_academic_structure,
        preserve_citations=config.preserve_citations,
    )


@router.post("/knowledge-base/{kb_id}/apply-preset")
async def apply_preset_to_kb(
    kb_id: int,
    preset: ChunkingPresetEnum = Query(..., description="预设配置名称"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    将预设配置应用到知识库
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 获取预设配置
    preset_config = get_preset_config(preset.value)
    
    # 更新知识库
    kb.chunk_size = preset_config.base_chunk_size
    kb.chunk_overlap = preset_config.chunk_overlap
    
    chunking_config = {
        "strategy": preset_config.strategy.value,
        "semantic_threshold": preset_config.semantic_threshold,
        "min_semantic_chunk": preset_config.min_semantic_chunk,
        "max_semantic_chunk": preset_config.max_semantic_chunk,
        "enable_hierarchical": preset_config.enable_hierarchical,
        "hierarchy_levels": [l.value for l in preset_config.hierarchy_levels],
        "detect_academic_structure": preset_config.detect_academic_structure,
        "preserve_citations": preset_config.preserve_citations,
        "applied_preset": preset.value,
    }
    
    # Update metadata with new dict to trigger SQLAlchemy change
    current_metadata = dict(kb.metadata_) if kb.metadata_ else {}
    current_metadata["chunking_config"] = chunking_config
    kb.metadata_ = current_metadata
    
    await db.commit()
    
    return {
        "message": f"已将预设 '{preset.value}' 应用到知识库",
        "knowledge_base_id": kb_id,
        "preset": preset.value,
    }


# ============== 辅助函数 ==============

def _convert_to_chunk_config(schema_config: ChunkingConfigCreate) -> ChunkConfig:
    """将 Schema 配置转换为服务配置"""
    return ChunkConfig(
        strategy=ChunkingStrategy(schema_config.strategy.value),
        base_chunk_size=schema_config.base_chunk_size,
        chunk_overlap=schema_config.chunk_overlap,
        semantic_threshold=schema_config.semantic_threshold,
        min_semantic_chunk=schema_config.min_semantic_chunk,
        max_semantic_chunk=schema_config.max_semantic_chunk,
        enable_hierarchical=schema_config.enable_hierarchical,
        hierarchy_levels=[ChunkLevel(l.value) for l in schema_config.hierarchy_levels],
        detect_academic_structure=schema_config.detect_academic_structure,
        preserve_citations=schema_config.preserve_citations,
    )


def _convert_to_response(result: dict) -> ChunkingResultResponse:
    """将服务结果转换为响应 Schema"""
    chunks = []
    for chunk in result.get("chunks", []):
        metadata = ChunkMetadataResponse(
            level=ChunkLevelEnum(chunk.metadata.level.value),
            section_type=chunk.metadata.section_type,
            section_title=chunk.metadata.section_title,
            parent_id=chunk.metadata.parent_id,
            child_ids=chunk.metadata.child_ids,
            has_citations=chunk.metadata.has_citations,
            position_ratio=chunk.metadata.position_ratio,
            keywords=chunk.metadata.keywords,
        )
        
        chunks.append(SmartChunkResponse(
            id=chunk.id,
            content=chunk.content,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            metadata=metadata,
        ))
    
    stats = result.get("stats", {})
    
    return ChunkingResultResponse(
        strategy=result.get("strategy", "unknown"),
        chunks=chunks,
        hierarchy=result.get("hierarchy"),
        metadata=result.get("metadata", {}),
        stats=ChunkingStatsResponse(
            total_chunks=stats.get("total_chunks", 0),
            total_chars=stats.get("total_chars", 0),
            avg_chunk_size=stats.get("avg_chunk_size", 0),
            min_chunk_size=stats.get("min_chunk_size", 0),
            max_chunk_size=stats.get("max_chunk_size", 0),
            chunks_with_citations=stats.get("chunks_with_citations", 0),
        )
    )


def _get_recommendation(results: dict) -> dict:
    """根据比较结果给出推荐"""
    best_strategy = None
    best_score = 0
    
    for strategy, result in results.items():
        if "error" in result:
            continue
        
        stats = result.get("stats", {})
        
        # 简单评分：考虑块大小的标准差和块数量
        avg_size = stats.get("avg_chunk_size", 0)
        min_size = stats.get("min_chunk_size", 0)
        max_size = stats.get("max_chunk_size", 0)
        
        # 理想的块大小在 300-800 之间
        size_score = 100 - abs(avg_size - 500) / 10
        
        # 块大小方差不要太大
        variance = max_size - min_size if max_size > 0 else 0
        variance_score = max(0, 100 - variance / 10)
        
        score = (size_score + variance_score) / 2
        
        if score > best_score:
            best_score = score
            best_strategy = strategy
    
    return {
        "recommended": best_strategy or "hybrid",
        "confidence": min(best_score / 100, 1.0),
        "reason": f"基于块大小和分布分析，'{best_strategy}' 策略最适合此文档"
    }
