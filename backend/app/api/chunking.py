"""
智能分块 API 路由

提供分块配置、预览、测试等功能

V3 变更:
  - _convert_to_chunk_config 传递 Token 计量字段
  - stats 响应新增 token 统计
  - 配置响应新增 Token 字段
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
    create_chunking_service,
)

router = APIRouter()


# ============== 预设配置 ==============

@router.get("/presets", response_model=PresetListResponse)
async def list_chunking_presets():
    """获取所有预设分块配置"""
    return PresetListResponse(presets=PRESET_DESCRIPTIONS)


@router.get("/presets/{preset_name}", response_model=ChunkingConfigResponse)
async def get_chunking_preset(preset_name: ChunkingPresetEnum):
    """获取指定预设的详细配置"""
    config = get_preset_config(preset_name.value)
    return ChunkingConfigResponse(
        strategy=config.strategy.value,
        use_token_based=config.use_token_based,
        base_chunk_tokens=config.base_chunk_tokens,
        overlap_tokens=config.overlap_tokens,
        min_semantic_tokens=config.min_semantic_tokens,
        max_semantic_tokens=config.max_semantic_tokens,
        base_chunk_size=config.base_chunk_size,
        chunk_overlap=config.chunk_overlap,
        breakpoint_percentile=config.breakpoint_percentile,
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
    """预览分块效果"""
    if request.config:
        config = _convert_to_chunk_config(request.config)
    elif request.preset:
        config = get_preset_config(request.preset.value)
    else:
        config = ChunkConfig()

    service = create_chunking_service()
    try:
        result = await service.chunk_document(
            text=request.text, config=config, file_type=request.file_type
        )
    except Exception as e:
        logger.error(f"分块预览失败: {e}")
        raise HTTPException(status_code=500, detail=f"分块失败: {str(e)}")

    return _convert_to_response(result)


@router.post("/analyze")
async def analyze_document(
    request: DocumentChunkRequest,
    current_user: User = Depends(get_current_user),
):
    """分析文档结构，推荐最佳分块策略"""
    service = create_chunking_service()
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
    """比较不同分块策略的效果"""
    results = {}
    service = create_chunking_service()

    for strategy in strategies:
        config = get_preset_config(strategy.value)
        try:
            result = await service.chunk_document(
                text=request.text, config=config, file_type=request.file_type
            )
            results[strategy.value] = {
                "strategy": strategy.value,
                "stats": result["stats"],
                "sample_chunks": [
                    {
                        "content": c.content[:200] + "..." if len(c.content) > 200 else c.content,
                        "length": len(c.content),
                        "tokens": c.metadata.token_count,
                        "has_citations": c.metadata.has_citations,
                    }
                    for c in result["chunks"][:3]
                ],
                "total_chunks": len(result["chunks"]),
            }
        except Exception as e:
            results[strategy.value] = {"strategy": strategy.value, "error": str(e)}

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
    """获取知识库的分块配置"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    chunking_config = kb.metadata_.get("chunking_config", {}) if kb.metadata_ else {}

    return ChunkingConfigResponse(
        strategy=chunking_config.get("strategy", "hybrid"),
        use_token_based=chunking_config.get("use_token_based", True),
        base_chunk_tokens=chunking_config.get("base_chunk_tokens", 128),
        overlap_tokens=chunking_config.get("overlap_tokens", 16),
        min_semantic_tokens=chunking_config.get("min_semantic_tokens", 32),
        max_semantic_tokens=chunking_config.get("max_semantic_tokens", 384),
        base_chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        breakpoint_percentile=chunking_config.get("breakpoint_percentile", 95.0),
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
    """更新知识库的分块配置"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    kb.chunk_size = config.base_chunk_size
    kb.chunk_overlap = config.chunk_overlap

    chunking_config = {
        "strategy": config.strategy.value,
        "use_token_based": config.use_token_based,
        "base_chunk_tokens": config.base_chunk_tokens,
        "overlap_tokens": config.overlap_tokens,
        "min_semantic_tokens": config.min_semantic_tokens,
        "max_semantic_tokens": config.max_semantic_tokens,
        "breakpoint_percentile": config.breakpoint_percentile,
        "semantic_threshold": config.semantic_threshold,
        "min_semantic_chunk": config.min_semantic_chunk,
        "max_semantic_chunk": config.max_semantic_chunk,
        "enable_hierarchical": config.enable_hierarchical,
        "hierarchy_levels": [l.value for l in config.hierarchy_levels],
        "detect_academic_structure": config.detect_academic_structure,
        "preserve_citations": config.preserve_citations,
    }

    current_metadata = dict(kb.metadata_) if kb.metadata_ else {}
    current_metadata["chunking_config"] = chunking_config
    kb.metadata_ = current_metadata

    await db.commit()
    await db.refresh(kb)

    logger.info(f"用户 {current_user.id} 更新了知识库 {kb_id} 的分块配置")

    return ChunkingConfigResponse(
        strategy=config.strategy,
        use_token_based=config.use_token_based,
        base_chunk_tokens=config.base_chunk_tokens,
        overlap_tokens=config.overlap_tokens,
        min_semantic_tokens=config.min_semantic_tokens,
        max_semantic_tokens=config.max_semantic_tokens,
        base_chunk_size=config.base_chunk_size,
        chunk_overlap=config.chunk_overlap,
        breakpoint_percentile=config.breakpoint_percentile,
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
    """将预设配置应用到知识库"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    preset_config = get_preset_config(preset.value)

    kb.chunk_size = preset_config.base_chunk_size
    kb.chunk_overlap = preset_config.chunk_overlap

    chunking_config = {
        "strategy": preset_config.strategy.value,
        "use_token_based": preset_config.use_token_based,
        "base_chunk_tokens": preset_config.base_chunk_tokens,
        "overlap_tokens": preset_config.overlap_tokens,
        "min_semantic_tokens": preset_config.min_semantic_tokens,
        "max_semantic_tokens": preset_config.max_semantic_tokens,
        "breakpoint_percentile": preset_config.breakpoint_percentile,
        "semantic_threshold": preset_config.semantic_threshold,
        "min_semantic_chunk": preset_config.min_semantic_chunk,
        "max_semantic_chunk": preset_config.max_semantic_chunk,
        "enable_hierarchical": preset_config.enable_hierarchical,
        "hierarchy_levels": [l.value for l in preset_config.hierarchy_levels],
        "detect_academic_structure": preset_config.detect_academic_structure,
        "preserve_citations": preset_config.preserve_citations,
        "applied_preset": preset.value,
    }

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
        use_token_based=schema_config.use_token_based,
        base_chunk_tokens=schema_config.base_chunk_tokens,
        overlap_tokens=schema_config.overlap_tokens,
        min_semantic_tokens=schema_config.min_semantic_tokens,
        max_semantic_tokens=schema_config.max_semantic_tokens,
        base_chunk_size=schema_config.base_chunk_size,
        chunk_overlap=schema_config.chunk_overlap,
        breakpoint_percentile=schema_config.breakpoint_percentile,
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
            token_count=chunk.metadata.token_count,
            extra=chunk.metadata.extra,
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
            total_tokens=stats.get("total_tokens", 0),
            avg_chunk_size=stats.get("avg_chunk_size", 0),
            min_chunk_size=stats.get("min_chunk_size", 0),
            max_chunk_size=stats.get("max_chunk_size", 0),
            avg_chunk_tokens=stats.get("avg_chunk_tokens", 0),
            min_chunk_tokens=stats.get("min_chunk_tokens", 0),
            max_chunk_tokens=stats.get("max_chunk_tokens", 0),
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
        avg_size = stats.get("avg_chunk_size", 0)
        min_size = stats.get("min_chunk_size", 0)
        max_size = stats.get("max_chunk_size", 0)

        size_score = 100 - abs(avg_size - 500) / 10
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
