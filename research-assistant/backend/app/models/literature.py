"""
文献管理模型 - 论文、收藏夹、引用关系
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Float, Boolean, Table, UniqueConstraint
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
