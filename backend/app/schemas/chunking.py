"""
智能分块相关的 Pydantic schemas

V3 变更:
  - ChunkingConfigCreate 新增 Token 计量字段
  - ChunkingStatsResponse 新增 token 统计
  - 前端可选择 Token 或字符模式
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

    # ===== Token 计量 (V3 新增，推荐) =====
    use_token_based: bool = Field(
        default=True,
        description="是否启用 Token 计量模式。开启后系统根据文本的实际中/英比例"
                    "自动换算分块尺寸，确保中英文文档获得一致的信息密度。"
    )
    base_chunk_tokens: int = Field(
        default=128,
        ge=16,
        le=1024,
        description="基础块大小（Token）。128 Token ≈ 512 英文字符 / 192 中文字符"
    )
    overlap_tokens: int = Field(
        default=16,
        ge=0,
        le=128,
        description="块重叠大小（Token）"
    )
    min_semantic_tokens: int = Field(
        default=32,
        ge=8,
        le=256,
        description="最小语义块（Token）"
    )
    max_semantic_tokens: int = Field(
        default=384,
        ge=64,
        le=2048,
        description="最大语义块（Token）"
    )

    # ===== 字符计量 (旧字段，向后兼容) =====
    base_chunk_size: int = Field(
        default=500,
        ge=100,
        le=3000,
        description="基础块大小（字符）— 仅在 use_token_based=False 时生效"
    )
    chunk_overlap: int = Field(
        default=50,
        ge=0,
        le=500,
        description="块重叠大小（字符）— 仅在 use_token_based=False 时生效"
    )
    min_semantic_chunk: int = Field(
        default=100,
        ge=50,
        le=500,
        description="最小语义块大小（字符）— 仅在 use_token_based=False 时生效"
    )
    max_semantic_chunk: int = Field(
        default=1500,
        ge=500,
        le=5000,
        description="最大语义块大小（字符）— 仅在 use_token_based=False 时生效"
    )

    # 语义分块配置（V2: breakpoint_percentile）
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
    token_count: int = 0


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
    total_tokens: int = 0
    avg_chunk_size: int
    min_chunk_size: int
    max_chunk_size: int
    avg_chunk_tokens: int = 0
    min_chunk_tokens: int = 0
    max_chunk_tokens: int = 0
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
        description="默认混合策略，平衡速度和质量，自动适配中英文",
        strategy="hybrid",
        recommended_for=["通用文档", "混合内容", "中英文混合"]
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
