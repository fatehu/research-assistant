"""
知识库模型 - 文档、分片和向量
使用 pgvector 进行向量存储和相似度搜索
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Float, Boolean, BigInteger, Index
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
import enum

from app.core.database import Base


# 阿里云 text-embedding-v2 向量维度
EMBEDDING_DIMENSION = 1536


class DocumentStatus(str, enum.Enum):
    """文档状态"""
    PENDING = "pending"       # 等待处理
    PROCESSING = "processing" # 处理中
    COMPLETED = "completed"   # 完成
    FAILED = "failed"         # 失败


class DocumentType(str, enum.Enum):
    """文档类型"""
    PDF = "pdf"
    TXT = "txt"
    MARKDOWN = "markdown"
    HTML = "html"
    DOCX = "docx"


class KnowledgeBase(Base):
    """知识库表"""
    __tablename__ = "knowledge_bases"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 基本信息
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    
    # 配置
    embedding_model = Column(String(100), default="text-embedding-v2")
    embedding_dimension = Column(Integer, default=EMBEDDING_DIMENSION)
    chunk_size = Column(Integer, default=500)
    chunk_overlap = Column(Integer, default=50)
    
    # 统计
    document_count = Column(Integer, default=0)
    total_chunks = Column(Integer, default=0)
    total_tokens = Column(BigInteger, default=0)
    
    # 元数据
    metadata_ = Column("metadata", JSON, default=dict)
    
    # 状态
    is_public = Column(Boolean, default=False)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="knowledge_bases")
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<KnowledgeBase {self.id}: {self.name}>"


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
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Document {self.id}: {self.original_filename}>"


class DocumentChunk(Base):
    """
    文档分片表 - 使用 pgvector 存储向量
    
    pgvector 支持的距离函数：
    - <-> : L2 距离 (欧几里得距离)
    - <#> : 内积 (负内积，用于最大内积搜索)
    - <=> : 余弦距离 (1 - 余弦相似度)
    
    对于归一化向量，余弦距离和L2距离等价
    """
    __tablename__ = "document_chunks"
    
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 分片内容
    content = Column(Text, nullable=False)
    chunk_index = Column(Integer, nullable=False)  # 在文档中的顺序
    
    # 位置信息
    start_char = Column(Integer, default=0)
    end_char = Column(Integer, default=0)
    
    # 向量 - 使用 pgvector 的 Vector 类型
    # 阿里云 text-embedding-v2 输出 1536 维向量
    embedding = Column(Vector(EMBEDDING_DIMENSION), nullable=True)
    embedding_model = Column(String(100), nullable=True)
    
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
    
    # 索引
    __table_args__ = (
        Index('idx_chunk_kb_doc', 'knowledge_base_id', 'document_id'),
        # HNSW 索引将在迁移脚本中创建，因为需要特殊语法
    )
    
    def __repr__(self):
        return f"<DocumentChunk {self.id}: doc={self.document_id}, idx={self.chunk_index}>"
