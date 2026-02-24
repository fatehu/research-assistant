"""
智能分块模块

V3: 新增 Token 计量支持，解决中英文混合场景下计量单位不统一问题。
"""

from .types import (
    ChunkConfig,
    ChunkingStrategy,
    ChunkLevel,
    ChunkMetadata,
    SmartChunk,
    ChunkResult,
    ResolvedCharLimits,
    EmbeddingLimitExceeded,
    generate_chunk_id,
)

from .service import (
    SmartChunkingService,
    create_chunking_service,
    smart_chunking_service,
    chunk_document_smart,
    get_preset_config,
)
from .academic_detector import AcademicStructureDetector
from .semantic_chunker import SemanticChunker
from .hierarchical_chunker import HierarchicalChunker

from .token_utils import (
    estimate_tokens,
    tokens_to_chars,
    chars_to_tokens,
    compute_adaptive_char_limits,
)

__all__ = [
    # Types
    "ChunkConfig",
    "ChunkingStrategy",
    "ChunkLevel",
    "ChunkMetadata",
    "SmartChunk",
    "ChunkResult",
    "ResolvedCharLimits",
    "EmbeddingLimitExceeded",
    "generate_chunk_id",
    # Service
    "SmartChunkingService",
    "create_chunking_service",
    "smart_chunking_service",
    "chunk_document_smart",
    "get_preset_config",
    # Backward-compatible concrete classes
    "AcademicStructureDetector",
    "SemanticChunker",
    "HierarchicalChunker",
    # Token utils
    "estimate_tokens",
    "tokens_to_chars",
    "chars_to_tokens",
    "compute_adaptive_char_limits",
]
