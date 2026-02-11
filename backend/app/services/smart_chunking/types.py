"""
智能分块 - 类型定义

包含所有枚举、数据类、异常。纯数据模块，无外部依赖。
"""
import hashlib
from enum import Enum
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field


# ============== 配置枚举 ==============

class ChunkingStrategy(str, Enum):
    """分块策略类型"""
    FIXED = "fixed"                    # 固定大小分块（原有方式）
    SEMANTIC = "semantic"              # 语义分块
    HIERARCHICAL = "hierarchical"      # 层级分块
    ACADEMIC = "academic"              # 学术论文专用
    HYBRID = "hybrid"                  # 混合策略（推荐）


class ChunkLevel(str, Enum):
    """分块层级"""
    PARAGRAPH = "paragraph"            # 段落级（细粒度）
    SECTION = "section"                # 章节级（中粒度）
    DOCUMENT = "document"              # 文档级（粗粒度）


# ============== 数据结构 ==============

@dataclass
class ChunkConfig:
    """分块配置 - 用户可自定义"""
    # 基础配置
    strategy: ChunkingStrategy = ChunkingStrategy.HYBRID
    base_chunk_size: int = 500          # 基础块大小（字符）
    chunk_overlap: int = 50             # 块重叠大小

    # 语义分块配置（V2: 相邻句子余弦距离算法）
    breakpoint_percentile: float = 95.0  # 断点百分位阈值（距离高于此百分位视为边界）
                                         # 越高 → 切分越少，块越大；越低 → 切分越多，块越小
                                         # 推荐范围: 85-95
    min_semantic_chunk: int = 100       # 最小语义块大小（字符）
    max_semantic_chunk: int = 1500      # 最大语义块大小（字符）

    # 向后兼容：旧配置仍可接受但不再影响核心算法
    semantic_threshold: float = 0.75    # [已弃用] 保留字段以兼容旧 API，不再用于边界检测
    window_size: int = 5               # [已弃用] 保留字段以兼容旧 API

    # 层级分块配置
    enable_hierarchical: bool = True    # 是否启用层级
    hierarchy_levels: List[ChunkLevel] = field(
        default_factory=lambda: [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION]
    )

    # 学术文档配置
    detect_academic_structure: bool = True   # 是否检测学术结构
    preserve_citations: bool = True          # 是否保留引用上下文（分句时避免切断引用句）

    # 高级配置
    sentence_split_threshold: int = 200      # 句子级切分阈值
    use_sliding_window: bool = True          # [已弃用] 保留兼容


@dataclass
class ChunkMetadata:
    """分块元数据"""
    level: ChunkLevel                   # 层级
    section_type: Optional[str] = None  # 学术文档章节类型
    section_title: Optional[str] = None # 章节标题
    parent_id: Optional[str] = None     # 父块ID（用于层级）
    child_ids: List[str] = field(default_factory=list)  # 子块ID列表
    semantic_score: float = 0.0         # 语义连贯性得分
    position_ratio: float = 0.0         # 在文档中的位置比例
    has_citations: bool = False         # 是否包含引用
    keywords: List[str] = field(default_factory=list)   # 关键词


@dataclass
class SmartChunk:
    """智能分块结果"""
    id: str                             # 块唯一ID
    content: str                        # 块内容
    start_char: int                     # 起始字符位置
    end_char: int                       # 结束字符位置
    metadata: ChunkMetadata             # 元数据
    embedding: Optional[List[float]] = None  # 嵌入向量

    @property
    def section_type(self) -> Optional[str]:
        """便捷属性：获取章节类型"""
        return self.metadata.section_type if self.metadata else None


@dataclass
class ChunkResult:
    """统一的分块结果容器"""
    chunks: List[SmartChunk] = field(default_factory=list)
    hierarchy: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    strategy: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkResult":
        return cls(
            chunks=data.get("chunks", []),
            hierarchy=data.get("hierarchy"),
            metadata=data.get("metadata", {}),
            stats=data.get("stats", {}),
            strategy=data.get("strategy", ""),
        )

    def get(self, key: str, default=None):
        """兼容 dict 访问"""
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        """兼容 dict 的 [] 访问"""
        return getattr(self, key)


# ============== 异常 ==============

class EmbeddingLimitExceeded(Exception):
    """Embedding 调用次数超限，触发降级"""
    pass


# ============== 工具函数 ==============

def generate_chunk_id(content: str, position: int) -> str:
    """生成块ID — 使用 SHA-256 前 16 位，加入内容长度降低碰撞"""
    hash_input = f"{content[:200]}|{position}|{len(content)}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]
