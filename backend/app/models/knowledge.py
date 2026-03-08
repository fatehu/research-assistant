"""
知识库模型 - 文档、分片和向量
使用 pgvector 进行向量存储和相似度搜索
支持层级分块和语义分块
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Float, Boolean, BigInteger, Index
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
import enum

from app.core.database import Base


# 向量维度 - 根据嵌入模型动态确定
# 从 embedding_service 获取实际维度，避免硬编码
def _get_embedding_dimension() -> int:
    """获取当前配置的嵌入模型维度"""
    from app.config import settings
    try:
        # 测试/最小环境下允许 embedding_service 缺省，回退到内置维度映射。
        from app.services.embedding_service import MODEL_DIMENSIONS
    except Exception:
        MODEL_DIMENSIONS = {}
    
    if settings.embedding_provider == "local":
        model = settings.local_embedding_model
        target_dim = settings.local_embedding_dimension
        if target_dim > 0:
            return target_dim
        return MODEL_DIMENSIONS.get(model, 1024)
    elif settings.embedding_provider == "mock":
        return max(1, int(settings.mock_embedding_dimension or 256))
    elif settings.embedding_provider == "aliyun":
        return 1536
    elif settings.embedding_provider == "openai":
        return 1536
    elif settings.embedding_provider == "ollama":
        return 768
    return 1024


EMBEDDING_DIMENSION = _get_embedding_dimension()


class DocumentStatus(str, enum.Enum):
    """文档状态"""
    PENDING = "pending"         # 等待处理
    RUNNING = "running"         # 处理中
    COMPLETED = "completed"     # 完成
    FAILED = "failed"           # 失败
    TIMEOUT = "timeout"         # 处理超时
    CANCELLED = "cancelled"     # 已取消

    # 兼容历史状态名（语义统一到新契约值）
    PROCESSING = "running"
    CANCELED = "cancelled"


class DocumentType(str, enum.Enum):
    """文档类型"""
    PDF = "pdf"
    TXT = "txt"
    MARKDOWN = "markdown"
    HTML = "html"
    DOCX = "docx"


class ChunkLevel(str, enum.Enum):
    """分块层级"""
    PARAGRAPH = "paragraph"    # 段落级（细粒度）
    SECTION = "section"        # 章节级（中粒度）
    DOCUMENT = "document"      # 文档级（粗粒度）


class KnowledgeBase(Base):
    """知识库表"""
    __tablename__ = "knowledge_bases"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 基本信息
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    
    # 配置
    embedding_model = Column(String(100), default="BAAI/bge-m3")
    embedding_dimension = Column(Integer, default=EMBEDDING_DIMENSION)
    chunk_size = Column(Integer, default=500)
    chunk_overlap = Column(Integer, default=50)
    
    # 统计
    document_count = Column(Integer, default=0)
    total_chunks = Column(Integer, default=0)
    total_tokens = Column(BigInteger, default=0)
    
    # 元数据（包含分块策略配置）
    metadata_ = Column("metadata", JSON, default=dict)
    
    # 状态
    is_public = Column(Boolean, default=False)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="knowledge_bases")
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan", passive_deletes=True)
    
    def __repr__(self):
        return f"<KnowledgeBase {self.id}: {self.name}>"
    
    @property
    def chunking_config(self) -> dict:
        """获取分块配置"""
        if self.metadata_ and "chunking_config" in self.metadata_:
            return self.metadata_["chunking_config"]
        return {
            "strategy": "hybrid",
            "breakpoint_percentile": 95.0,
            "semantic_threshold": 0.75,
            "enable_hierarchical": True,
        }


class Document(Base):
    """文档表"""
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 文件信息
    filename = Column(String(500), nullable=False)
    original_filename = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=True)
    file_size = Column(BigInteger, default=0)
    file_type = Column(String(50), nullable=False)
    mime_type = Column(String(100), nullable=True)
    
    # 内容信息
    content = Column(Text, nullable=True)  # 原始文本内容
    content_hash = Column(String(64), nullable=True, index=True)  # 内容哈希，用于去重
    
    # 处理状态
    status = Column(String(20), default=DocumentStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    
    # 统计
    chunk_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)
    char_count = Column(Integer, default=0)
    
    # 元数据
    metadata_ = Column("metadata", JSON, default=dict)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    
    # 关系
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan", passive_deletes=True)
    
    def __repr__(self):
        return f"<Document {self.id}: {self.original_filename}>"


class DocumentChunk(Base):
    """
    文档分片表 - 使用 pgvector 存储向量，支持层级分块
    
    pgvector 支持的距离函数：
    - <-> : L2 距离 (欧几里得距离)
    - <#> : 内积 (负内积，用于最大内积搜索)
    - <=> : 余弦距离 (1 - 余弦相似度)
    
    对于归一化向量，余弦距离和L2距离等价
    
    层级分块说明：
    - paragraph: 细粒度分块，用于精确检索
    - section: 章节级分块，用于上下文理解
    - document: 文档级摘要，用于概览
    """
    __tablename__ = "document_chunks"
    
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 分片内容
    content = Column(Text, nullable=False)
    content_segmented = Column(Text, nullable=True)
    context_summary = Column(Text, nullable=True)
    chunk_index = Column(Integer, nullable=False)  # 在文档中的顺序
    
    # 位置信息
    start_char = Column(Integer, default=0)
    end_char = Column(Integer, default=0)
    
    # 向量 - 使用 pgvector 的 Vector 类型
    # 维度由 EMBEDDING_DIMENSION 动态确定 (取决于所选嵌入模型)
    embedding = Column(Vector(), nullable=True)
    embedding_model = Column(String(100), nullable=True)
    embedding_dimension = Column(Integer, default=EMBEDDING_DIMENSION, nullable=False)
    
    # ===== 层级分块相关字段 =====
    # 分块层级
    chunk_level = Column(String(20), default=ChunkLevel.PARAGRAPH.value, index=True)
    
    # 学术文档结构
    section_type = Column(String(50), nullable=True, index=True)  # abstract/introduction/methodology等
    section_title = Column(String(500), nullable=True)            # 章节标题
    
    # 层级关系
    parent_chunk_id = Column(Integer, ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # 语义分析
    has_citations = Column(Boolean, default=False)    # 是否包含引用
    semantic_score = Column(Float, nullable=True)     # 语义连贯性得分
    
    # 统计
    token_count = Column(Integer, default=0)
    char_count = Column(Integer, default=0)
    
    # 元数据
    metadata_ = Column("metadata", JSON, default=dict)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    document = relationship("Document", back_populates="chunks")
    knowledge_base = relationship("KnowledgeBase")
    parent_chunk = relationship("DocumentChunk", remote_side=[id], backref="child_chunks")
    
    # 索引
    __table_args__ = (
        Index('idx_chunk_kb_doc', 'knowledge_base_id', 'document_id'),
        Index('idx_chunk_level_kb', 'chunk_level', 'knowledge_base_id'),
        # HNSW 索引将在迁移脚本中创建，因为需要特殊语法
    )
    
    def __repr__(self):
        return f"<DocumentChunk {self.id}: doc={self.document_id}, idx={self.chunk_index}, level={self.chunk_level}>"
