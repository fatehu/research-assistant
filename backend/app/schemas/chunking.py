"""
智能分块相关的 Pydantic schemas
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


# ============== 枚举 ==============

class ChunkingStrategyEnum(str, Enum):
    """分块策略"""
    FIXED = "fixed"                    # 固定大小分块
    SEMANTIC = "semantic"              # 语义分块
    HIERARCHICAL = "hierarchical"      # 层级分块
    ACADEMIC = "academic"              # 学术论文专用
    HYBRID = "hybrid"                  # 混合策略（推荐）


class ChunkLevelEnum(str, Enum):
    """分块层级"""
    PARAGRAPH = "paragraph"            # 段落级（细粒度）
    SECTION = "section"                # 章节级（中粒度）
    DOCUMENT = "document"              # 文档级（粗粒度）


class ChunkingPresetEnum(str, Enum):
    """预设配置"""
    DEFAULT = "default"                # 默认混合策略
    FAST = "fast"                      # 快速固定分块
    PRECISE = "precise"                # 精确语义分块
    ACADEMIC = "academic"              # 学术论文优化
    DEEP = "deep"                      # 深度层级分块


# ============== 配置 Schemas ==============

class ChunkingConfigCreate(BaseModel):
    """创建分块配置"""
    # 基础配置
    strategy: ChunkingStrategyEnum = Field(
        default=ChunkingStrategyEnum.HYBRID,
        description="分块策略"
    )
    base_chunk_size: int = Field(
        default=500,
        ge=100,
        le=3000,
        description="基础块大小（字符）"
    )
    chunk_overlap: int = Field(
        default=50,
        ge=0,
        le=500,
        description="块重叠大小"
    )
    
    # 语义分块配置（V2: 新增 breakpoint_percentile）
    breakpoint_percentile: float = Field(
        default=95.0,
        ge=50.0,
        le=99.9,
        description="断点百分位阈值。距离高于此百分位的句子间隙视为语义边界。"
                    "越高→切分越少、块越大；越低→切分越多、块越小。推荐 85-95"
    )
    semantic_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="[已弃用] 保留以兼容旧 API，不再影响核心语义检测算法"
    )
    min_semantic_chunk: int = Field(
        default=100,
        ge=50,
        le=500,
        description="最小语义块大小"
    )
    max_semantic_chunk: int = Field(
        default=1500,
        ge=500,
        le=5000,
        description="最大语义块大小"
    )
    
    # 层级分块配置
    enable_hierarchical: bool = Field(
        default=True,
        description="是否启用层级"
    )
    hierarchy_levels: List[ChunkLevelEnum] = Field(
        default=[ChunkLevelEnum.PARAGRAPH, ChunkLevelEnum.SECTION],
        description="层级列表"
    )
    
    # 学术文档配置
    detect_academic_structure: bool = Field(
        default=True,
        description="是否检测学术结构"
    )
    preserve_citations: bool = Field(
        default=True,
        description="是否保留引用上下文"
    )


class ChunkingConfigResponse(ChunkingConfigCreate):
    """分块配置响应"""
    id: Optional[int] = None
    user_id: Optional[int] = None
    name: Optional[str] = None
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ChunkingPresetRequest(BaseModel):
    """使用预设配置"""
    preset: ChunkingPresetEnum = Field(
        default=ChunkingPresetEnum.DEFAULT,
        description="预设配置名称"
    )


# ============== 分块结果 Schemas ==============

class ChunkMetadataResponse(BaseModel):
    """分块元数据"""
    level: ChunkLevelEnum
    section_type: Optional[str] = None
    section_title: Optional[str] = None
    parent_id: Optional[str] = None
    child_ids: List[str] = []
    has_citations: bool = False
    position_ratio: float = 0.0
    keywords: List[str] = []


class SmartChunkResponse(BaseModel):
    """智能分块结果"""
    id: str
    content: str
    start_char: int
    end_char: int
    metadata: ChunkMetadataResponse
    
    class Config:
        from_attributes = True


class ChunkingStatsResponse(BaseModel):
    """分块统计"""
    total_chunks: int
    total_chars: int
    avg_chunk_size: int
    min_chunk_size: int
    max_chunk_size: int
    chunks_with_citations: int = 0


class ChunkingResultResponse(BaseModel):
    """完整分块结果"""
    strategy: str
    chunks: List[SmartChunkResponse]
    hierarchy: Optional[Dict[str, List[Dict[str, Any]]]] = None
    metadata: Dict[str, Any] = {}
    stats: ChunkingStatsResponse


# ============== API 请求 Schemas ==============

class DocumentChunkRequest(BaseModel):
    """文档分块请求"""
    text: str = Field(
        ...,
        min_length=1,
        max_length=500000,
        description="要分块的文本"
    )
    config: Optional[ChunkingConfigCreate] = Field(
        default=None,
        description="分块配置（不提供则使用默认配置）"
    )
    preset: Optional[ChunkingPresetEnum] = Field(
        default=None,
        description="使用预设配置（优先级低于 config）"
    )
    file_type: str = Field(
        default="txt",
        description="文件类型"
    )


class KnowledgeBaseChunkConfigUpdate(BaseModel):
    """更新知识库的分块配置"""
    chunking_strategy: Optional[ChunkingStrategyEnum] = None
    chunking_config: Optional[ChunkingConfigCreate] = None


# ============== 预设配置说明 ==============

class PresetDescription(BaseModel):
    """预设配置说明"""
    name: str
    description: str
    strategy: str
    recommended_for: List[str]


PRESET_DESCRIPTIONS = [
    PresetDescription(
        name="default",
        description="默认混合策略，平衡速度和质量",
        strategy="hybrid",
        recommended_for=["通用文档", "混合内容"]
    ),
    PresetDescription(
        name="fast",
        description="快速固定分块，适合大量文档批量处理",
        strategy="fixed",
        recommended_for=["大批量处理", "简单文档"]
    ),
    PresetDescription(
        name="precise",
        description="精确语义分块，更好的语义边界检测",
        strategy="semantic",
        recommended_for=["重要文档", "需要精确检索"]
    ),
    PresetDescription(
        name="academic",
        description="学术论文优化，识别论文结构",
        strategy="academic",
        recommended_for=["学术论文", "研究报告", "技术文档"]
    ),
    PresetDescription(
        name="deep",
        description="深度层级分块，支持多粒度检索",
        strategy="hierarchical",
        recommended_for=["长文档", "书籍", "需要多层级索引"]
    ),
]


class PresetListResponse(BaseModel):
    """预设配置列表"""
    presets: List[PresetDescription]
