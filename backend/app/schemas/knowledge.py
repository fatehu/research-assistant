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
    embedding_model: str = "BAAI/bge-m3"
    chunk_size: int = Field(default=500, ge=100, le=2000)
    chunk_overlap: int = Field(default=50, ge=0, le=500)


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    is_public: Optional[bool] = None
    chunking_config: Optional[Dict[str, Any]] = None


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
    score_threshold: float = Field(default=0.05, ge=0, le=1)
    use_reranker: bool = Field(default=True, description="是否启用Reranker精排")
    use_hybrid: bool = Field(default=True, description="是否启用混合检索（向量+全文）")
    use_query_rewrite: bool = Field(default=True, description="是否启用Query Rewrite改写")
    query_rewrite_strategies: Optional[List[str]] = Field(
        default=None,
        description="可选改写策略: synonym/hyde/decompose"
    )
    # [Fix 12] 新增字段：chunk_level 过滤
    chunk_level: Optional[str] = Field(
        default="paragraph",
        description="搜索的分块层级: paragraph/section/document/all"
    )
    section_type: Optional[str] = Field(
        default=None,
        description="过滤章节类型: abstract/methodology/results 等"
    )
    include_parent_context: bool = Field(
        default=False,
        description="是否同时返回父级 chunk 作为上下文"
    )
    use_contextual_compression: bool = Field(
        default=False,
        description="是否启用检索结果的上下文压缩（会增加延迟）"
    )


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
    # [Fix 12] 新增字段：层级信息
    chunk_level: Optional[str] = None
    section_type: Optional[str] = None
    section_title: Optional[str] = None
    parent_context: Optional[str] = None  # 父级 chunk 的内容摘要


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
