"""
智能分块 - 类型定义

包含所有枚举、数据类、异常。纯数据模块，无外部依赖。

V3 变更:
  - ChunkConfig 新增 Token 计量字段 (base_chunk_tokens, min_semantic_tokens, etc.)
  - 当 Token 字段 > 0 时，系统以 Token 为准，运行时按文本语言比例自动转换为字符限制
  - 旧的字符字段 (base_chunk_size, min_semantic_chunk, etc.) 保留以向后兼容
  - 新增 use_token_based 标志，显式控制计量模式
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
    """
    分块配置 - 用户可自定义

    计量模式:
      - use_token_based=True  → 以 Token 字段为准 (推荐，自动适配中英文)
      - use_token_based=False → 以字符字段为准 (旧行为，向后兼容)

    当 use_token_based=True 时，运行时会根据实际文本的中英文比例，
    将 Token 限制自动换算为字符限制。这意味着:
      - 英文 500 字 → ~125 Tokens → 分块约 500 字符
      - 中文 500 字 → ~333 Tokens → 分块约 500 字符 (而非之前的 500 字符 ≈ 500 Tokens)
    """
    # 基础配置 - 字符计量 (旧字段，向后兼容)
    strategy: ChunkingStrategy = ChunkingStrategy.HYBRID
    base_chunk_size: int = 500          # 基础块大小（字符）
    chunk_overlap: int = 50             # 块重叠大小（字符）

    # ===== Token 计量 (V3 新增) =====
    use_token_based: bool = True        # 是否启用 Token 计量模式
    base_chunk_tokens: int = 128        # 基础块大小（Token） — 约等于 512 英文字符 / 192 中文字符
    overlap_tokens: int = 16            # 块重叠大小（Token）
    min_semantic_tokens: int = 32       # 最小语义块（Token）
    max_semantic_tokens: int = 384      # 最大语义块（Token）

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

    def resolve_char_limits(self, text: str = "") -> "ResolvedCharLimits":
        """
        根据计量模式和文本内容，解析出运行时使用的字符限制。

        当 use_token_based=True 且有 text:
          → 按文本语言比例将 Token 转字符
        当 use_token_based=False 或无 text:
          → 直接使用字符字段
        """
        if self.use_token_based and text:
            from .token_utils import compute_adaptive_char_limits
            limits = compute_adaptive_char_limits(
                base_tokens=self.base_chunk_tokens,
                text=text,
                min_tokens=self.min_semantic_tokens,
                max_tokens=self.max_semantic_tokens,
                overlap_tokens=self.overlap_tokens,
            )
            return ResolvedCharLimits(
                base_chunk_size=limits["base_chunk_chars"],
                chunk_overlap=limits["overlap_chars"],
                min_semantic_chunk=limits["min_semantic_chars"],
                max_semantic_chunk=limits["max_semantic_chars"],
                chars_per_token=limits["chars_per_token"],
                cjk_ratio=limits["language_ratio"]["cjk"],
                is_token_based=True,
            )
        else:
            return ResolvedCharLimits(
                base_chunk_size=self.base_chunk_size,
                chunk_overlap=self.chunk_overlap,
                min_semantic_chunk=self.min_semantic_chunk,
                max_semantic_chunk=self.max_semantic_chunk,
                chars_per_token=4.0,
                cjk_ratio=0.0,
                is_token_based=False,
            )


@dataclass
class ResolvedCharLimits:
    """
    运行时解析后的字符限制 — 由 ChunkConfig.resolve_char_limits() 生成。

    分块器内部使用这个结构而非直接读取 ChunkConfig 的字符字段，
    以确保 Token 计量模式下字符限制已根据语言比例调整。
    """
    base_chunk_size: int
    chunk_overlap: int
    min_semantic_chunk: int
    max_semantic_chunk: int
    chars_per_token: float
    cjk_ratio: float
    is_token_based: bool


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
    token_count: int = 0               # Token 数（V3 新增）
    extra: Dict[str, Any] = field(default_factory=dict)  # 第三方引擎/结构扩展信息


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
    """Embedding 预算超限，触发降级"""
    pass


# ============== 工具函数 ==============

def generate_chunk_id(content: str, position: int) -> str:
    """生成块ID — 使用 SHA-256 前 16 位，加入内容长度降低碰撞"""
    hash_input = f"{content[:200]}|{position}|{len(content)}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]
