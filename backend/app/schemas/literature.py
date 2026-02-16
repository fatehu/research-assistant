"""
文献管理 Schema
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
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


class CollectionKnowledgeReadinessItem(BaseModel):
    paper_id: int
    title: str
    status: Literal["ready", "processing", "pending", "failed", "missing"]
    document_id: Optional[int] = None
    error_message: Optional[str] = None
    pdf_available: bool = False


class CollectionKnowledgeReadinessResponse(BaseModel):
    collection_id: int
    knowledge_base_id: int
    total_papers: int
    ready_papers: int
    processing_papers: int
    pending_papers: int
    failed_papers: int
    missing_papers: int
    can_cross_paper_answer: bool
    papers: List[CollectionKnowledgeReadinessItem] = Field(default_factory=list)


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


# ============ Reader Session ============

class ReaderSessionBase(BaseModel):
    page: int = Field(default=1, ge=1)
    zoom: str = Field(default="100%")
    scroll_y: int = Field(default=0, ge=0)
    selected_kb_id: Optional[int] = None
    last_anchor: Dict[str, Any] = Field(default_factory=dict)


class ReaderSessionUpdate(ReaderSessionBase):
    pass


class ReaderSessionResponse(ReaderSessionBase):
    updated_at: datetime


# ============ Annotation ============

class PaperAnnotationBase(BaseModel):
    annotation_type: Literal["highlight", "note"] = "highlight"
    page: int = Field(..., ge=1)
    quote_text: Optional[str] = None
    anchor: Dict[str, Any] = Field(default_factory=dict)
    content: Optional[str] = None
    color: str = "#f59e0b"


class PaperAnnotationCreate(PaperAnnotationBase):
    pass


class PaperAnnotationUpdate(BaseModel):
    annotation_type: Optional[Literal["highlight", "note"]] = None
    page: Optional[int] = Field(default=None, ge=1)
    quote_text: Optional[str] = None
    anchor: Optional[Dict[str, Any]] = None
    content: Optional[str] = None
    color: Optional[str] = None


class PaperAnnotationResponse(PaperAnnotationBase):
    id: int
    user_id: int
    paper_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============ Comment ============

class PaperCommentAuthor(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    avatar: Optional[str] = None


class PaperCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    parent_id: Optional[int] = None


class PaperCommentUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)


class PaperCommentResponse(BaseModel):
    id: int
    paper_entity_id: int
    user_id: int
    parent_id: Optional[int] = None
    content: str
    created_at: datetime
    updated_at: datetime
    author: PaperCommentAuthor


# ============ Rating ============

class PaperRatingUpdate(BaseModel):
    rating: int = Field(..., ge=1, le=5)


class PaperRatingSummary(BaseModel):
    my_rating: Optional[int] = None
    global_avg: Optional[float] = None
    global_count: int = 0
    same_group_avg: Optional[float] = None
    same_group_count: int = 0


# ============ Knowledge Link ============

class AddPaperToKnowledgeRequest(BaseModel):
    knowledge_base_id: int


class PaperKnowledgeLinkResponse(BaseModel):
    id: int
    user_id: int
    paper_id: int
    knowledge_base_id: int
    document_id: Optional[int] = None
    status: Literal["pending", "processing", "ready", "failed"]
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============ Literature Ask ============

class LiteratureAskRequest(BaseModel):
    scope: Literal["paper", "collection"]
    paper_id: Optional[int] = None
    collection_id: Optional[int] = None
    knowledge_base_id: int
    mode: Literal["agentic", "classic"] = "agentic"
    question: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[int] = None


class LiteratureAskSource(BaseModel):
    idx: Optional[int] = None
    chunk_id: Optional[int] = None
    document_id: int
    document_name: str
    page: Optional[int] = None
    page_source: Optional[Literal["metadata", "estimated", "unknown"]] = None
    section_title: Optional[str] = None
    section_type: Optional[str] = None
    snippet: str
    score: Optional[float] = None
    score_source: Optional[Literal["fts", "fallback", "paper_read"]] = None


class LiteratureAskSession(BaseModel):
    id: int
    user_id: int
    scope: Literal["paper", "collection"]
    paper_id: Optional[int] = None
    collection_id: Optional[int] = None
    knowledge_base_id: int
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LiteratureAskMessage(BaseModel):
    id: int
    session_id: int
    role: Literal["user", "assistant"]
    content: str
    sources: List[LiteratureAskSource] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True
