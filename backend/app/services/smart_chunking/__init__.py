"""
智能分块包 - 公共 API 导出

所有旧的 ``from app.services.smart_chunking_service import X``
现在通过 shim 文件代理到此处。
"""

# 类型 & 数据结构
from .types import (
    ChunkingStrategy,
    ChunkLevel,
    ChunkConfig,
    ChunkMetadata,
    SmartChunk,
    ChunkResult,
    EmbeddingLimitExceeded,
    generate_chunk_id,
)

# 检测器
from .academic_detector import AcademicStructureDetector

# 分块器
from .semantic_chunker import SemanticChunker
from .hierarchical_chunker import HierarchicalChunker, enforce_limit, _enforce_limit

# 文本预处理
from .text_preprocessor import preprocess_text, split_to_sentences

# 主服务
from .service import (
    SmartChunkingService,
    create_chunking_service,
    smart_chunking_service,
    chunk_document_smart,
    get_preset_config,
)

__all__ = [
    # 枚举
    "ChunkingStrategy", "ChunkLevel",
    # 配置 & 数据
    "ChunkConfig", "ChunkMetadata", "SmartChunk", "ChunkResult",
    # 异常
    "EmbeddingLimitExceeded",
    # 检测器
    "AcademicStructureDetector",
    # 分块器
    "SemanticChunker", "HierarchicalChunker",
    # 服务
    "SmartChunkingService", "create_chunking_service",
    "smart_chunking_service", "chunk_document_smart",
    # 工具
    "get_preset_config", "generate_chunk_id",
    "enforce_limit", "_enforce_limit",
    "preprocess_text", "split_to_sentences",
]
