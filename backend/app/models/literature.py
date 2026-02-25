"""
文献管理模型 - 论文、收藏夹、阅读、批注、评论、评分与问答
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    JSON,
    Boolean,
    Table,
    UniqueConstraint,
    Index,
    Float,
)
from sqlalchemy.orm import relationship
import enum

from app.core.database import Base


class PaperSource(str, enum.Enum):
    """论文来源"""
    SEMANTIC_SCHOLAR = "semantic_scholar"
    ARXIV = "arxiv"
    DOI = "doi"
    MANUAL = "manual"


class CollectionType(str, enum.Enum):
    """收藏夹类型"""
    DEFAULT = "default"      # 默认收藏夹
    PROJECT = "project"      # 项目相关
    READING_LIST = "reading_list"  # 阅读列表
    CUSTOM = "custom"        # 自定义


class AnnotationType(str, enum.Enum):
    """批注类型"""
    HIGHLIGHT = "highlight"
    NOTE = "note"


class KnowledgeLinkStatus(str, enum.Enum):
    """论文入知识库链路状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    # 兼容历史状态名（语义统一到新契约值）
    PROCESSING = "running"
    READY = "completed"
    CANCELED = "cancelled"


class AskScope(str, enum.Enum):
    """文献问答范围"""
    PAPER = "paper"
    COLLECTION = "collection"


# 论文-收藏夹关联表（多对多）
paper_collection_association = Table(
    'paper_collection',
    Base.metadata,
    Column('paper_id', Integer, ForeignKey('papers.id', ondelete='CASCADE'), primary_key=True),
    Column('collection_id', Integer, ForeignKey('paper_collections.id', ondelete='CASCADE'), primary_key=True),
    Column('added_at', DateTime, default=datetime.utcnow)
)


# 论文引用关系表（自引用多对多）
paper_citations = Table(
    'paper_citations',
    Base.metadata,
    Column('citing_paper_id', Integer, ForeignKey('papers.id', ondelete='CASCADE'), primary_key=True),
    Column('cited_paper_id', Integer, ForeignKey('papers.id', ondelete='CASCADE'), primary_key=True)
)


class Paper(Base):
    """论文表"""
    __tablename__ = "papers"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 外部标识符
    semantic_scholar_id = Column(String(100), nullable=True, index=True)  # S2 Paper ID
    arxiv_id = Column(String(50), nullable=True, index=True)              # arXiv ID (e.g., 2301.00001)
    doi = Column(String(200), nullable=True, index=True)                  # DOI
    pubmed_id = Column(String(50), nullable=True)                         # PubMed ID
    
    # 基本信息
    title = Column(String(1000), nullable=False)
    abstract = Column(Text, nullable=True)
    
    # 作者信息 (JSON数组)
    authors = Column(JSON, default=list)  # [{name, authorId, affiliations}]
    
    # 发表信息
    year = Column(Integer, nullable=True)
    venue = Column(String(500), nullable=True)       # 期刊/会议名称
    journal = Column(String(500), nullable=True)
    volume = Column(String(50), nullable=True)
    pages = Column(String(50), nullable=True)
    publisher = Column(String(200), nullable=True)
    
    # URL 链接
    url = Column(String(1000), nullable=True)        # 论文主页
    pdf_url = Column(String(1000), nullable=True)    # PDF 下载链接
    arxiv_url = Column(String(500), nullable=True)   # arXiv 链接
    
    # 本地存储
    pdf_path = Column(String(1000), nullable=True)   # 本地 PDF 路径
    pdf_downloaded = Column(Boolean, default=False)

    # 全局论文实体（跨用户聚合）
    paper_entity_id = Column(
        Integer,
        ForeignKey("paper_entities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    
    # 知识库关联
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    
    # 统计信息
    citation_count = Column(Integer, default=0)
    reference_count = Column(Integer, default=0)
    influential_citation_count = Column(Integer, default=0)
    
    # 分类和标签
    fields_of_study = Column(JSON, default=list)     # 研究领域 ["Computer Science", "AI"]
    tags = Column(JSON, default=list)                # 用户自定义标签
    
    # 阅读状态
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime, nullable=True)
    
    # 用户笔记
    notes = Column(Text, nullable=True)
    rating = Column(Integer, nullable=True)          # 1-5 星评分
    
    # 元数据
    source = Column(String(50), default=PaperSource.SEMANTIC_SCHOLAR.value)
    raw_data = Column(JSON, default=dict)            # 原始 API 响应
    
    # 时间戳
    published_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="papers")
    collections = relationship(
        "PaperCollection",
        secondary=paper_collection_association,
        back_populates="papers"
    )
    knowledge_base = relationship("KnowledgeBase")
    document = relationship("Document")
    paper_entity = relationship("PaperEntity")
    
    # 引用关系
    citing = relationship(
        "Paper",
        secondary=paper_citations,
        primaryjoin=id == paper_citations.c.citing_paper_id,
        secondaryjoin=id == paper_citations.c.cited_paper_id,
        backref="cited_by"
    )
    
    # 唯一约束
    __table_args__ = (
        UniqueConstraint('user_id', 'semantic_scholar_id', name='uq_user_s2_id'),
        UniqueConstraint('user_id', 'arxiv_id', name='uq_user_arxiv_id'),
    )
    
    def __repr__(self):
        return f"<Paper {self.id}: {self.title[:50]}...>"
    
    @property
    def author_names(self) -> list:
        """获取作者名称列表"""
        return [a.get('name', '') for a in (self.authors or [])]
    
    @property
    def first_author(self) -> str:
        """获取第一作者"""
        if self.authors and len(self.authors) > 0:
            return self.authors[0].get('name', 'Unknown')
        return 'Unknown'


class PaperCollection(Base):
    """论文收藏夹"""
    __tablename__ = "paper_collections"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 基本信息
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String(20), default="#3b82f6")    # 颜色标识
    icon = Column(String(50), default="folder")       # 图标名称
    
    # 类型
    collection_type = Column(String(50), default=CollectionType.CUSTOM.value)
    is_default = Column(Boolean, default=False)       # 是否为默认收藏夹
    
    # 统计
    paper_count = Column(Integer, default=0)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    user = relationship("User", back_populates="paper_collections")
    papers = relationship(
        "Paper",
        secondary=paper_collection_association,
        back_populates="collections"
    )
    
    def __repr__(self):
        return f"<PaperCollection {self.id}: {self.name}>"


class PaperSearchHistory(Base):
    """论文搜索历史"""
    __tablename__ = "paper_search_history"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    query = Column(String(500), nullable=False)
    source = Column(String(50), default="semantic_scholar")  # 搜索来源
    result_count = Column(Integer, default=0)
    filters = Column(JSON, default=dict)              # 搜索过滤器
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    user = relationship("User")


class PaperEntity(Base):
    """全局论文实体（用于跨用户聚合评论与评分）"""
    __tablename__ = "paper_entities"

    id = Column(Integer, primary_key=True, index=True)
    canonical_key = Column(String(300), nullable=False, unique=True, index=True)
    doi_norm = Column(String(200), nullable=True, index=True)
    arxiv_norm = Column(String(80), nullable=True, index=True)
    title_norm = Column(String(1200), nullable=True, index=True)
    year = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<PaperEntity {self.id}: {self.canonical_key}>"


class PaperReadSession(Base):
    """论文阅读会话（阅读位置、缩放等）"""
    __tablename__ = "paper_read_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)

    page = Column(Integer, default=1)
    zoom = Column(String(20), default="100%")
    scroll_y = Column(Integer, default=0)
    selected_kb_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    last_anchor = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    paper = relationship("Paper")
    selected_kb = relationship("KnowledgeBase")

    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", name="uq_read_session_user_paper"),
    )


class PaperReaderPageCache(Base):
    """论文阅读生成式页缓存（论文共享）"""
    __tablename__ = "paper_reader_page_caches"

    id = Column(Integer, primary_key=True, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    page = Column(Integer, nullable=False, default=1, index=True)
    source_signature = Column(String(255), nullable=False)
    parser_version = Column(String(64), nullable=False)
    build_mode = Column(String(32), nullable=False, default="parser")
    structure_confidence = Column(Float, nullable=False, default=0.0)
    payload_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    paper = relationship("Paper")

    __table_args__ = (
        UniqueConstraint("paper_id", "page", "source_signature", name="uq_reader_page_cache_sig"),
        Index("idx_reader_page_cache_paper_page", "paper_id", "page"),
        Index("idx_reader_page_cache_updated_at", "updated_at"),
    )


class PaperReaderComponentOverlay(Base):
    """论文阅读组件覆盖缓存（用户个性化，不污染共享缓存）"""
    __tablename__ = "paper_reader_component_overlays"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    page = Column(Integer, nullable=False, default=1, index=True)
    source_signature = Column(String(255), nullable=False)
    node_id = Column(String(96), nullable=False)
    action_type = Column(String(32), nullable=False, default="patch")
    overlay_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    paper = relationship("Paper")

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "paper_id",
            "page",
            "source_signature",
            "node_id",
            name="uq_reader_overlay_user_paper_page_sig_node",
        ),
        Index(
            "idx_reader_overlay_user_paper_page",
            "user_id",
            "paper_id",
            "page",
        ),
        Index("idx_reader_overlay_updated_at", "updated_at"),
    )


class PaperAnnotation(Base):
    """论文批注"""
    __tablename__ = "paper_annotations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)

    annotation_type = Column(String(20), default=AnnotationType.HIGHLIGHT.value, nullable=False)
    page = Column(Integer, nullable=False, default=1, index=True)
    quote_text = Column(Text, nullable=True)
    anchor_json = Column(JSON, default=dict)
    content = Column(Text, nullable=True)
    color = Column(String(20), default="#f59e0b")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    paper = relationship("Paper")

    __table_args__ = (
        Index("idx_paper_annotations_user_paper_page", "user_id", "paper_id", "page"),
    )


class PaperComment(Base):
    """论文评论（全站登录用户可见，支持一级回复）"""
    __tablename__ = "paper_comments"

    id = Column(Integer, primary_key=True, index=True)
    paper_entity_id = Column(Integer, ForeignKey("paper_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("paper_comments.id", ondelete="CASCADE"), nullable=True, index=True)

    content = Column(Text, nullable=False)
    deleted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    paper_entity = relationship("PaperEntity")
    user = relationship("User")
    parent = relationship("PaperComment", remote_side=[id], backref="replies")


class PaperRating(Base):
    """按用户对全局论文实体评分"""
    __tablename__ = "paper_ratings"

    id = Column(Integer, primary_key=True, index=True)
    paper_entity_id = Column(Integer, ForeignKey("paper_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    rating = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    paper_entity = relationship("PaperEntity")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("paper_entity_id", "user_id", name="uq_paper_rating_entity_user"),
    )


class PaperKnowledgeLink(Base):
    """论文加入知识库链路状态"""
    __tablename__ = "paper_knowledge_links"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(20), default=KnowledgeLinkStatus.PENDING.value, nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    paper = relationship("Paper")
    knowledge_base = relationship("KnowledgeBase")
    document = relationship("Document")

    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", "knowledge_base_id", name="uq_paper_kb_link_user_paper_kb"),
    )


class LiteratureQASession(Base):
    """文献问答会话（仅会话拥有者可见）"""
    __tablename__ = "literature_qa_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = Column(String(20), default=AskScope.PAPER.value, nullable=False)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=True, index=True)
    collection_id = Column(Integer, ForeignKey("paper_collections.id", ondelete="CASCADE"), nullable=True, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    paper = relationship("Paper")
    collection = relationship("PaperCollection")
    knowledge_base = relationship("KnowledgeBase")
    messages = relationship(
        "LiteratureQAMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="LiteratureQAMessage.created_at",
    )


class LiteratureQAMessage(Base):
    """文献问答消息"""
    __tablename__ = "literature_qa_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("literature_qa_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user/assistant
    content = Column(Text, nullable=False)
    sources = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    session = relationship("LiteratureQASession", back_populates="messages")
