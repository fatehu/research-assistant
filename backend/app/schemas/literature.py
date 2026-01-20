"""
文献管理 Schema
"""
from datetime import datetime
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


# ============ Paper Schemas ============

class PaperAuthor(BaseModel):
    """论文作者"""
    name: str
    authorId: Optional[str] = None
    affiliations: List[str] = []


class PaperBase(BaseModel):
    """论文基础信息"""
    title: str
    abstract: Optional[str] = None
    authors: List[PaperAuthor] = []
    year: Optional[int] = None
    venue: Optional[str] = None
    citation_count: int = 0
    reference_count: int = 0


class PaperCreate(PaperBase):
    """创建论文"""
    semantic_scholar_id: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    fields_of_study: List[str] = []
    source: str = "manual"
    raw_data: Dict[str, Any] = {}


class PaperUpdate(BaseModel):
    """更新论文"""
    title: Optional[str] = None
    abstract: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    rating: Optional[int] = Field(None, ge=1, le=5)
    is_read: Optional[bool] = None


class PaperResponse(PaperBase):
    """论文响应"""
    id: int
    user_id: int
    semantic_scholar_id: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    arxiv_url: Optional[str] = None
    pdf_path: Optional[str] = None
    pdf_downloaded: bool = False
    knowledge_base_id: Optional[int] = None
    document_id: Optional[int] = None
    influential_citation_count: int = 0
    fields_of_study: List[str] = []
    tags: List[str] = []
    is_read: bool = False
    read_at: Optional[datetime] = None
    notes: Optional[str] = None
    rating: Optional[int] = None
    source: str
    published_date: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    
    # 收藏夹信息
    collection_ids: List[int] = []
    
    class Config:
        from_attributes = True


class PaperSearchResult(BaseModel):
    """搜索结果"""
    source: str
    external_id: str
    title: str
    abstract: Optional[str] = None
    authors: List[PaperAuthor] = []
    year: Optional[int] = None
    venue: Optional[str] = None
    citation_count: int = 0
    reference_count: int = 0
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    fields_of_study: List[str] = []
    
    # 是否已收藏
    is_saved: bool = False
    saved_paper_id: Optional[int] = None


class PaperSearchResponse(BaseModel):
    """搜索响应"""
    total: int
    offset: int = 0
    papers: List[PaperSearchResult]
    query: str
    source: str


# ============ Collection Schemas ============

class CollectionBase(BaseModel):
    """收藏夹基础"""
    name: str
    description: Optional[str] = None
    color: str = "#3b82f6"
    icon: str = "folder"


class CollectionCreate(CollectionBase):
    """创建收藏夹"""
    collection_type: str = "custom"


class CollectionUpdate(BaseModel):
    """更新收藏夹"""
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class CollectionResponse(CollectionBase):
    """收藏夹响应"""
    id: int
    user_id: int
    collection_type: str
    is_default: bool = False
    paper_count: int = 0
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class CollectionWithPapers(CollectionResponse):
    """带论文列表的收藏夹"""
    papers: List[PaperResponse] = []


# ============ Citation Graph Schemas ============

class GraphNode(BaseModel):
    """图谱节点"""
    id: str
    title: str
    year: Optional[int] = None
    citations: int = 0
    authors: List[str] = []
    level: int = 0
    type: str = "paper"  # center, citing, referenced


class GraphEdge(BaseModel):
    """图谱边"""
    source: str = Field(..., alias="from")
    target: str = Field(..., alias="to")
    type: str = "cites"
    
    class Config:
        populate_by_name = True


class CitationGraphResponse(BaseModel):
    """引用图谱响应"""
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    center_id: str


# ============ Action Schemas ============

class AddToCollectionRequest(BaseModel):
    """添加到收藏夹请求"""
    paper_id: int
    collection_ids: List[int]


class RemoveFromCollectionRequest(BaseModel):
    """从收藏夹移除请求"""
    paper_id: int
    collection_id: int


class SavePaperFromSearchRequest(BaseModel):
    """从搜索结果保存论文"""
    source: str
    external_id: str
    title: str
    abstract: Optional[str] = None
    authors: List[Dict[str, Any]] = []
    year: Optional[int] = None
    venue: Optional[str] = None
    citation_count: int = 0
    reference_count: int = 0
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    fields_of_study: List[str] = []
    raw_data: Dict[str, Any] = {}
    collection_ids: List[int] = []  # 可选：直接添加到收藏夹


class DownloadPdfRequest(BaseModel):
    """下载 PDF 请求"""
    paper_id: int
    knowledge_base_id: Optional[int] = None  # 可选：下载后添加到知识库


# ============ Search History ============

class SearchHistoryResponse(BaseModel):
    """搜索历史响应"""
    id: int
    query: str
    source: str
    result_count: int
    filters: Dict[str, Any] = {}
    created_at: datetime
    
    class Config:
        from_attributes = True
