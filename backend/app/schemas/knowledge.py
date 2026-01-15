"""
知识库相关的 Pydantic schemas
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ========== 知识库 Schemas ==========

class KnowledgeBaseCreate(BaseModel):
    """创建知识库"""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    embedding_model: str = "text-embedding-v2"
    chunk_size: int = Field(default=500, ge=100, le=2000)
    chunk_overlap: int = Field(default=50, ge=0, le=500)


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    is_public: Optional[bool] = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""
    id: int
    user_id: int
    name: str
    description: Optional[str]
    embedding_model: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    document_count: int
    total_chunks: int
    total_tokens: int
    is_public: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应"""
    items: List[KnowledgeBaseResponse]
    total: int


# ========== 文档 Schemas ==========

class DocumentUploadResponse(BaseModel):
    """文档上传响应"""
    id: int
    filename: str
    original_filename: str
    file_size: int
    file_type: str
    status: str
    message: str


class DocumentResponse(BaseModel):
    """文档响应"""
    id: int
    knowledge_base_id: int
    filename: str
    original_filename: str
    file_size: int
    file_type: str
    status: str
    error_message: Optional[str]
    chunk_count: int
    token_count: int
    char_count: int
    created_at: datetime
    updated_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True


class DocumentListResponse(BaseModel):
    """文档列表响应"""
    items: List[DocumentResponse]
    total: int


class DocumentDetailResponse(DocumentResponse):
    """文档详情响应"""
    content: Optional[str] = None
    metadata: Dict[str, Any] = {}


# ========== 分片 Schemas ==========

class ChunkResponse(BaseModel):
    """分片响应"""
    id: int
    document_id: int
    chunk_index: int
    content: str
    start_char: int
    end_char: int
    token_count: int
    char_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class ChunkListResponse(BaseModel):
    """分片列表响应"""
    items: List[ChunkResponse]
    total: int


# ========== 搜索 Schemas ==========

class SearchRequest(BaseModel):
    """向量搜索请求"""
    query: str = Field(..., min_length=1, max_length=2000)
    knowledge_base_ids: Optional[List[int]] = None  # 不指定则搜索所有知识库
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.5, ge=0, le=1)


class SearchResultItem(BaseModel):
    """搜索结果项"""
    chunk_id: int
    document_id: int
    knowledge_base_id: int
    document_name: str
    knowledge_base_name: str
    content: str
    score: float
    chunk_index: int
    metadata: Dict[str, Any] = {}


class SearchResponse(BaseModel):
    """搜索响应"""
    query: str
    results: List[SearchResultItem]
    total: int
    search_time_ms: float


# ========== 处理状态 Schemas ==========

class ProcessingStatus(BaseModel):
    """处理状态"""
    document_id: int
    status: str
    progress: float  # 0-100
    message: str
    chunk_count: int = 0
    error: Optional[str] = None
