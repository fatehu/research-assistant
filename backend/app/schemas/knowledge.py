"""Pydantic schemas for knowledge base APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


DocumentUploadMode = Literal["local_fast", "local_hybrid", "online_mm", "auto"]
DocumentExtractProfile = Literal["general", "academic_formula", "table_first"]
DocumentExtractGranularity = Literal["fine", "medium", "coarse"]


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    embedding_model: str = "BAAI/bge-m3"
    chunk_size: int = Field(default=500, ge=100, le=2000)
    chunk_overlap: int = Field(default=50, ge=0, le=500)


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    is_public: Optional[bool] = None
    chunking_config: Optional[Dict[str, Any]] = None


class KnowledgeBaseResponse(BaseModel):
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
    items: List[KnowledgeBaseResponse]
    total: int


class DocumentUploadResponse(BaseModel):
    id: int
    filename: str
    original_filename: str
    file_size: int
    file_type: str
    status: str
    processing_stage: Optional[str] = None
    processing_stage_label: Optional[str] = None
    processing_progress: Optional[float] = None
    processing_detail: Optional[str] = None
    processing_mode: DocumentUploadMode = "local_fast"
    extract_profile: DocumentExtractProfile = "general"
    extract_granularity: DocumentExtractGranularity = "medium"
    message: str


class DocumentResponse(BaseModel):
    id: int
    knowledge_base_id: int
    filename: str
    original_filename: str
    file_size: int
    file_type: str
    status: str
    processing_stage: Optional[str] = None
    processing_stage_label: Optional[str] = None
    processing_progress: Optional[float] = None
    processing_detail: Optional[str] = None
    processing_mode: DocumentUploadMode = "local_fast"
    extract_profile: DocumentExtractProfile = "general"
    extract_granularity: DocumentExtractGranularity = "medium"
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
    items: List[DocumentResponse]
    total: int


class DocumentDetailResponse(DocumentResponse):
    content: Optional[str] = None
    metadata: Dict[str, Any] = {}


class ChunkResponse(BaseModel):
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
    items: List[ChunkResponse]
    total: int


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    knowledge_base_ids: Optional[List[int]] = None
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.05, ge=0, le=1)
    use_reranker: bool = Field(default=True, description="是否启用重排")
    use_hybrid: bool = Field(default=True, description="是否启用混合检索（向量+全文）")
    use_query_rewrite: bool = Field(default=True, description="是否启用 Query Rewrite")
    rewrite_mode: Literal["auto", "force", "off"] = Field(
        default="auto",
        description="改写模式，优先级高于 use_query_rewrite",
    )
    query_rewrite_profile: Optional[Literal["off", "light", "deep"]] = Field(
        default=None,
        description="改写层级：off 关闭，light 仅轻量同义扩展，deep 使用完整多策略改写。",
    )
    query_rewrite_strategies: Optional[List[str]] = Field(
        default=None,
        description="可选改写策略：synonym/hyde/decompose",
    )
    chunk_level: Optional[str] = Field(
        default="paragraph",
        description="检索分块层级：paragraph/section/document/all",
    )
    section_type: Optional[str] = Field(
        default=None,
        description="可选章节类型过滤",
    )
    include_parent_context: bool = Field(
        default=False,
        description="是否返回父级 chunk 上下文",
    )
    include_adjacent_chunks: bool = Field(
        default=False,
        description="是否返回命中 chunk 的相邻上下文",
    )
    adjacent_window: int = Field(
        default=1,
        ge=1,
        le=3,
        description="相邻窗口大小（1-3）",
    )
    use_contextual_compression: bool = Field(
        default=False,
        description="是否启用上下文压缩",
    )


class SearchResultItem(BaseModel):
    chunk_id: int
    document_id: int
    knowledge_base_id: int
    document_name: str
    knowledge_base_name: str
    content: str
    score: float
    chunk_index: int
    metadata: Dict[str, Any] = {}
    chunk_level: Optional[str] = None
    section_type: Optional[str] = None
    section_title: Optional[str] = None
    parent_context: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResultItem]
    total: int
    search_time_ms: float


class ProcessingStatus(BaseModel):
    document_id: int
    status: str
    progress: float
    message: str
    processing_stage: Optional[str] = None
    processing_stage_label: Optional[str] = None
    processing_detail: Optional[str] = None
    chunk_count: int = 0
    error: Optional[str] = None
