"""
文献管理 Schema
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


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
    status: Literal["completed", "running", "pending", "failed", "timeout", "cancelled", "missing"]
    document_id: Optional[int] = None
    error_message: Optional[str] = None
    pdf_available: bool = False


class CollectionKnowledgeReadinessResponse(BaseModel):
    collection_id: int
    knowledge_base_id: int
    total_papers: int
    completed_papers: int
    running_papers: int
    pending_papers: int
    failed_papers: int
    timeout_papers: int
    cancelled_papers: int
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


# ============ Reader Generative ============ #

class ReaderGenerativeSourceAnchor(BaseModel):
    page: int = Field(..., ge=1)
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=0)


class ReaderGenerativeRequest(BaseModel):
    page: int = Field(..., ge=1)
    selected_kb_id: Optional[int] = None
    force_refresh: bool = False
    prefer_agent: bool = False
    style_hint: Optional[str] = None


class ReaderGenerativeBlock(BaseModel):
    id: str
    kind: Literal["heading", "paragraph", "list_item", "caption"]
    text: str
    order: int = Field(..., ge=0)
    section_title: Optional[str] = None
    source_anchor: ReaderGenerativeSourceAnchor


class ReaderGenerativeSection(BaseModel):
    title: str
    level: int = Field(default=1, ge=1, le=4)
    block_ids: List[str] = Field(default_factory=list)
    source_anchor: Optional[ReaderGenerativeSourceAnchor] = None


class ReaderGenerativeAsset(BaseModel):
    kind: Literal["link", "annotation", "image_hint"]
    label: str
    source: Literal["metadata", "text", "annotation", "pdf"]
    href: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGenerativeStyleTuning(BaseModel):
    body_scale: float = Field(default=1.0, ge=0.9, le=1.25)
    line_height: float = Field(default=1.9, ge=1.55, le=2.2)
    heading_scale: float = Field(default=1.0, ge=0.95, le=1.35)


class ReaderGenerativePageResponse(BaseModel):
    paper_id: int
    page: int
    parser_version: str
    source_signature: str
    style_key: str
    build_mode: str
    structure_confidence: float = Field(..., ge=0.0, le=1.0)
    summary: str = ""
    style_tuning: ReaderGenerativeStyleTuning = Field(default_factory=ReaderGenerativeStyleTuning)
    sections: List[ReaderGenerativeSection] = Field(default_factory=list)
    blocks: List[ReaderGenerativeBlock] = Field(default_factory=list)
    assets: List[ReaderGenerativeAsset] = Field(default_factory=list)
    cache_hit: bool = False
    cache_layer: Optional[Literal["redis", "db", "none"]] = None
    generated_at: datetime


class ReaderGenerativePrefetchRequest(BaseModel):
    pages: List[int] = Field(default_factory=list, max_length=16)
    selected_kb_id: Optional[int] = None
    style_hint: Optional[str] = None


class ReaderGenerativePrefetchResponse(BaseModel):
    queued: List[int] = Field(default_factory=list)
    skipped: List[int] = Field(default_factory=list)


# ============ Reader Composed ============ #

class ReaderComposeRequest(BaseModel):
    page: int = Field(..., ge=1)
    selected_kb_id: Optional[int] = None
    force_refresh: bool = False
    regenerate: bool = False
    latency_budget_ms: Optional[int] = Field(default=None, ge=1200, le=600000)
    quality_target: Optional[float] = Field(default=None, ge=0.6, le=0.97)
    max_iterations: Optional[int] = Field(default=None, ge=1, le=16)
    style_intent: Optional[str] = None
    theme_mode: Optional[Literal["light", "dark"]] = None
    detail_level: Optional[Literal["concise", "standard", "deep"]] = None
    compare_mode: Optional[bool] = None
    citation_tldr: Optional[bool] = None


class ReaderComposeSchemeChoice(BaseModel):
    scheme_id: str = ""
    label: str = ""
    rationale: str = ""
    source: str = ""
    candidate_ids: List[str] = Field(default_factory=list)


class ReaderComposeOmissionDecision(BaseModel):
    decision_id: str
    decision: Literal["hide", "collapse", "defer"] = "hide"
    reason: str = ""
    recoverable: bool = True
    target_layout_ids: List[str] = Field(default_factory=list)
    target_block_ids: List[str] = Field(default_factory=list)
    target_atom_ids: List[str] = Field(default_factory=list)


class ReaderComposeReviewDiagnostic(BaseModel):
    code: str
    severity: Literal["info", "warn", "error"] = "info"
    message: str
    component_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderComponentBBoxHint(BaseModel):
    x0: float = 0.0
    x1: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    page_width: Optional[float] = None
    page_height: Optional[float] = None


class ReaderAnchorPolygonPoint(BaseModel):
    x: float = 0.0
    y: float = 0.0


class ReaderAnchorPolygon(BaseModel):
    points: List[ReaderAnchorPolygonPoint] = Field(default_factory=list)
    source: Optional[str] = None
    component_id: Optional[str] = None


class ReaderAnchorGeometry(BaseModel):
    polygons: List[ReaderAnchorPolygon] = Field(default_factory=list)
    page_width: Optional[float] = None
    page_height: Optional[float] = None


class ReaderComponentAnchorV2(BaseModel):
    coord_version: str = "anchor_v2"
    canonical_block_id: str
    page: int = Field(..., ge=1)
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=0)


class ReaderComponentSourceAnchor(BaseModel):
    page: int = Field(..., ge=1)
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=0)
    quote: Optional[str] = None
    quote_text: Optional[str] = None
    anchor_id: Optional[str] = None
    segment_index: Optional[int] = Field(default=None, ge=1)
    segment_total: Optional[int] = Field(default=None, ge=1)
    bbox_hint: Optional[ReaderComponentBBoxHint] = None
    canonical_block_id: Optional[str] = None
    coord_version: Optional[str] = None
    anchor_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    anchor_v2: Optional[ReaderComponentAnchorV2] = None
    geometry_version: Optional[str] = None
    geometry: Optional[ReaderAnchorGeometry] = None
    source_word_ids: List[str] = Field(default_factory=list)
    source_char_ranges: List[Dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_quote(self):
        merged = str(self.quote or self.quote_text or "").strip()
        self.quote = merged or None
        self.quote_text = merged or None
        if int(self.end_char) <= int(self.start_char):
            self.end_char = int(self.start_char) + max(1, len(merged) or 1)
        return self


class ReaderComponentAction(BaseModel):
    key: str
    label: str
    kind: Literal["primary", "default", "danger", "link"] = "default"
    payload: Dict[str, Any] = Field(default_factory=dict)


class ReaderComponentLayoutSlot(BaseModel):
    reserved_height: Optional[int] = Field(default=None, ge=64, le=1600)
    lock_height: bool = False


class ReaderComponentNode(BaseModel):
    id: str
    type: str
    props: Dict[str, Any] = Field(default_factory=dict)
    children: List["ReaderComponentNode"] = Field(default_factory=list)
    source_anchor_refs: List[ReaderComponentSourceAnchor] = Field(default_factory=list)
    source_block_ids: List[str] = Field(default_factory=list)
    source_atom_ids: List[str] = Field(default_factory=list)
    zone_type: Optional[Literal["main_body", "side_context", "figure_meta"]] = None
    column_id: Optional[str] = None
    region: Optional[str] = None
    display: Optional[Literal["default", "collapsed", "pinned", "hidden_until_expand"]] = None
    order_key: Optional[float] = None
    compat_filled: bool = False
    compat_filled_fields: List[str] = Field(default_factory=list)
    heading_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    capabilities: List[str] = Field(default_factory=list)
    actions: List[ReaderComponentAction] = Field(default_factory=list)
    layout_slot: Optional[ReaderComponentLayoutSlot] = None


class ReaderComposeQualityReport(BaseModel):
    overall: float = Field(default=0.0, ge=0.0, le=1.0)
    structure_fidelity: float = Field(default=0.0, ge=0.0, le=1.0)
    readability: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_alignment: float = Field(default=0.0, ge=0.0, le=1.0)
    layout_consistency: float = Field(default=0.0, ge=0.0, le=1.0)
    cross_column_merge_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    sidebar_recall: float = Field(default=1.0, ge=0.0, le=1.0)
    toc_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    hard_constraints_passed: bool = False
    sidebar_leak_detected: bool = False
    title_integrity_ok: bool = False
    anchors_valid: bool = False
    mm_assist_used: bool = False
    mm_model: str = ""
    mm_fallback_used: bool = False
    anchor_coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_image_ready: float = Field(default=0.0, ge=0.0, le=1.0)
    anchor_quote_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    anchor_bbox_iou: float = Field(default=0.0, ge=0.0, le=1.0)
    anchor_misjump_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    anchor_gate_passed: bool = True
    validation_errors: List[str] = Field(default_factory=list)
    quality_target: float = Field(default=0.86, ge=0.6, le=0.97)
    iterations: int = Field(default=0, ge=0)
    degraded: bool = False
    stop_reason: str = ""
    latency_budget_ms: int = Field(default=8500, ge=1200, le=600000)
    deductions: List[Dict[str, Any]] = Field(default_factory=list)
    fix_suggestions: List[str] = Field(default_factory=list)
    iteration_trace_summary: List[Dict[str, Any]] = Field(default_factory=list)


class ReaderUIPlan(BaseModel):
    plan_id: str
    components: List[ReaderComponentNode] = Field(default_factory=list)
    layout: Dict[str, Any] = Field(default_factory=dict)
    style_tokens: Dict[str, Any] = Field(default_factory=dict)
    trace_meta: Dict[str, Any] = Field(default_factory=dict)
    ui_ops: List[Dict[str, Any]] = Field(default_factory=list)
    agent_trace: List[Dict[str, Any]] = Field(default_factory=list)
    agent_tool_calls: List[Dict[str, Any]] = Field(default_factory=list)


class SegmentPlan(BaseModel):
    segment_id: str
    kind: str
    ui_component: str
    component_hint: Optional[str] = None
    kind_hint: Optional[str] = None
    confidence: Optional[float] = None
    block_ids: List[str] = Field(default_factory=list)
    line_ids: List[str] = Field(default_factory=list)
    evidence_line_ids: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    continuation: Optional[str] = None
    reason: Optional[str] = None


class LayoutPlanV2(BaseModel):
    zones: List[Dict[str, Any]] = Field(default_factory=list)
    headings: List[Dict[str, Any]] = Field(default_factory=list)
    continuation: Dict[str, Any] = Field(default_factory=dict)
    segments: List[SegmentPlan] = Field(default_factory=list)
    ui_suggestions: List[Dict[str, Any]] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class NodeGateReport(BaseModel):
    total_nodes: int = 0
    blocked_nodes: int = 0
    passed_nodes: int = 0
    rows: List[Dict[str, Any]] = Field(default_factory=list)


class ReaderValidationGateResult(BaseModel):
    passed: bool = False
    errors: List[str] = Field(default_factory=list)


class ReaderValidationGates(BaseModel):
    id_integrity: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    full_coverage: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    whitelist_only: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    layout_contract: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    no_drop_blocks: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    ownership_unchanged: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    non_empty_plan_for_non_empty_input: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)
    source_text_immutable: ReaderValidationGateResult = Field(default_factory=ReaderValidationGateResult)


class ReaderValidationReport(BaseModel):
    passed: bool = False
    gates: ReaderValidationGates = Field(default_factory=ReaderValidationGates)
    errors: List[str] = Field(default_factory=list)


class ReaderComposeAsset(BaseModel):
    kind: Literal["link", "annotation", "image_hint", "external_image"]
    label: str
    source: Literal["metadata", "text", "annotation", "pdf", "web"]
    href: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    tldr: Optional[str] = None


class ReaderComposePayload(BaseModel):
    paper_id: int
    page: int
    status: Literal["done", "fallback"] = "done"
    degraded_reason: str = ""
    pipeline_version: str = ""
    engine_version: str
    source_signature: str
    build_mode: str
    ui_plan: ReaderUIPlan
    assets: List[ReaderComposeAsset] = Field(default_factory=list)
    scheme_choice: ReaderComposeSchemeChoice = Field(default_factory=ReaderComposeSchemeChoice)
    decision_log: List[str] = Field(default_factory=list)
    omission_decisions: List[ReaderComposeOmissionDecision] = Field(default_factory=list)
    quality_report: ReaderComposeQualityReport = Field(default_factory=ReaderComposeQualityReport)
    iteration_trace: List[Dict[str, Any]] = Field(default_factory=list)
    main_block_ids: List[str] = Field(default_factory=list)
    aux_block_ids: List[str] = Field(default_factory=list)
    validation_report: ReaderValidationReport = Field(default_factory=ReaderValidationReport)
    asset_policy: Dict[str, Any] = Field(default_factory=dict)
    layout_channels: Dict[str, List[str]] = Field(default_factory=dict)
    mm_assist_meta: Dict[str, Any] = Field(default_factory=dict)
    parser_chain_meta: Dict[str, Any] = Field(default_factory=dict)
    page_structure_v3: Dict[str, Any] = Field(default_factory=dict)
    canonical_atoms: Dict[str, Any] = Field(default_factory=dict)
    atom_semantics: Dict[str, Any] = Field(default_factory=dict)
    deterministic_page_skeleton: Dict[str, Any] = Field(default_factory=dict)
    stage2_style_plan: Dict[str, Any] = Field(default_factory=dict)
    minimal_gate_report: Dict[str, Any] = Field(default_factory=dict)
    candidate_ranking: Dict[str, Any] = Field(default_factory=dict)
    repair_report: Dict[str, Any] = Field(default_factory=dict)
    segment_id_map: Dict[str, Any] = Field(default_factory=dict)
    stage1_structural_annotations: Dict[str, Any] = Field(default_factory=dict)
    stage2_design_layout: Dict[str, Any] = Field(default_factory=dict)
    pipeline_contract_meta: Dict[str, Any] = Field(default_factory=dict)
    qwen_layout_plan_v2: Optional[LayoutPlanV2] = None
    layout_advice_v3: Dict[str, Any] = Field(default_factory=dict)
    qwen_plan_meta: Dict[str, Any] = Field(default_factory=dict)
    assembly_meta: Dict[str, Any] = Field(default_factory=dict)
    component_registry_version: Optional[str] = None
    segment_map: Dict[str, Any] = Field(default_factory=dict)
    segment_map_meta: Dict[str, Any] = Field(default_factory=dict)
    node_gate_report: Optional[NodeGateReport] = None
    toc_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    phase1_compact_input: Dict[str, Any] = Field(default_factory=dict)
    review_route_meta: Dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime
    cache_hit: bool = False
    cache_layer: Optional[Literal["redis", "db", "none"]] = None
    overlay_applied: bool = False
    overlay_count: int = 0


class ReaderComposePrefetchRequest(BaseModel):
    pages: List[int] = Field(default_factory=list, max_length=16)
    selected_kb_id: Optional[int] = None
    style_intent: Optional[str] = None
    latency_budget_ms: Optional[int] = Field(default=None, ge=1200, le=600000)
    quality_target: Optional[float] = Field(default=None, ge=0.6, le=0.97)
    max_iterations: Optional[int] = Field(default=None, ge=1, le=16)
    theme_mode: Optional[Literal["light", "dark"]] = None
    detail_level: Optional[Literal["concise", "standard", "deep"]] = None
    compare_mode: Optional[bool] = None
    citation_tldr: Optional[bool] = None


class ReaderComposePrefetchResponse(BaseModel):
    queued: List[int] = Field(default_factory=list)
    skipped: List[int] = Field(default_factory=list)


class ReaderComposeReviewSessionRequest(ReaderComposeRequest):
    snapshot_label: Optional[str] = None
    prefer_cache_clone: bool = True
    allow_recompute_on_cache_miss: bool = True


class ReaderComposeReviewImportRequest(BaseModel):
    snapshot_label: Optional[str] = None
    payload: ReaderComposePayload


class ReaderComposeReviewPatchRequest(BaseModel):
    snapshot_id: Optional[str] = None
    ui_ops: List[Dict[str, Any]] = Field(default_factory=list)
    decision_log_append: List[str] = Field(default_factory=list)
    omission_decisions: Optional[List[ReaderComposeOmissionDecision]] = None
    scheme_choice: Optional[ReaderComposeSchemeChoice] = None
    note: Optional[str] = None


class ReaderComposeReviewObservationRequest(BaseModel):
    snapshot_id: Optional[str] = None
    render_image_url: Optional[str] = None
    diagnostics: List[ReaderComposeReviewDiagnostic] = Field(default_factory=list)
    note: Optional[str] = None
    source: Optional[str] = None


class ReaderComposeReviewAutoPatchRequest(BaseModel):
    snapshot_id: Optional[str] = None
    user_intent: Optional[str] = None
    note: Optional[str] = None


class ReaderComposeReviewPublishRequest(BaseModel):
    snapshot_id: Optional[str] = None
    note: Optional[str] = None


class ReaderComposeReviewSnapshot(BaseModel):
    session_id: str
    snapshot_id: str
    paper_id: int
    page: int
    source_signature: str
    build_mode: str
    status: Literal["done", "fallback"] = "done"
    ui_plan: ReaderUIPlan
    assets: List[ReaderComposeAsset] = Field(default_factory=list)
    quality_report: ReaderComposeQualityReport = Field(default_factory=ReaderComposeQualityReport)
    scheme_choice: ReaderComposeSchemeChoice = Field(default_factory=ReaderComposeSchemeChoice)
    decision_log: List[str] = Field(default_factory=list)
    omission_decisions: List[ReaderComposeOmissionDecision] = Field(default_factory=list)
    diagnostics: List[ReaderComposeReviewDiagnostic] = Field(default_factory=list)
    phase1_compact_input: Dict[str, Any] = Field(default_factory=dict)
    render_route: str = ""
    render_image_url: str = ""
    observation_note: str = ""
    observation_source: str = ""
    observation_diagnostics: List[ReaderComposeReviewDiagnostic] = Field(default_factory=list)
    observation_updated_at: Optional[datetime] = None
    docmind_page_image_url: str = ""
    style_intent: str = ""
    theme_mode: str = ""
    detail_level: str = ""
    parent_snapshot_id: Optional[str] = None
    revision: int = 1
    created_at: datetime


class ReaderComposeReviewAutoPatchResponse(BaseModel):
    snapshot: ReaderComposeReviewSnapshot
    patch_applied: bool = False
    ui_ops: List[Dict[str, Any]] = Field(default_factory=list)
    ui_ops_count: int = 0
    fallback_reason: Optional[str] = None
    validation_errors: List[str] = Field(default_factory=list)
    agent_summary: str = ""


class ReaderComposeReviewPublishResponse(BaseModel):
    published: bool = False
    session_id: str
    snapshot_id: str
    paper_id: int
    page: int
    source_signature: str
    read_route: str = ""
    overlay_saved: bool = False



class ReaderNodeActionRequest(BaseModel):
    page: int = Field(..., ge=1)
    node_id: str = Field(..., min_length=1, max_length=96)
    action: Literal["regenerate", "degrade"]
    reason: Optional[str] = None
    selected_kb_id: Optional[int] = None
    style_intent: Optional[str] = None
    theme_mode: Optional[Literal["light", "dark"]] = None
    detail_level: Optional[Literal["concise", "standard", "deep"]] = None
    compare_mode: Optional[bool] = None
    citation_tldr: Optional[bool] = None


class ReaderNodeActionResponse(BaseModel):
    patch_type: Literal["node_replace", "node_insert", "node_update"] = "node_replace"
    node_before: Optional[ReaderComponentNode] = None
    node_after: Optional[ReaderComponentNode] = None
    quality_delta: float = 0.0
    overlay_saved: bool = False
    message: str = ""
    disabled: bool = False
    disabled_reason: Optional[str] = None


class ReaderInlineQueryRequest(BaseModel):
    page: int = Field(..., ge=1)
    node_id: str = Field(..., min_length=1, max_length=96)
    question: str = Field(..., min_length=1, max_length=2000)
    scope: Literal["page", "section"] = "section"
    selected_kb_id: Optional[int] = None
    style_intent: Optional[str] = None
    theme_mode: Optional[Literal["light", "dark"]] = None
    detail_level: Optional[Literal["concise", "standard", "deep"]] = None
    compare_mode: Optional[bool] = None
    citation_tldr: Optional[bool] = None


class ReaderInlineQuerySource(BaseModel):
    page: int = Field(..., ge=1)
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=0)
    quote: Optional[str] = None
    quote_text: Optional[str] = None

    @model_validator(mode="after")
    def _normalize_quote(self):
        merged = str(self.quote or self.quote_text or "").strip()
        self.quote = merged or None
        self.quote_text = merged or None
        if int(self.end_char) <= int(self.start_char):
            raise ValueError("inline_query_source.end_char must be greater than start_char")
        return self


class ReaderInlineQueryDonePayload(BaseModel):
    node: ReaderComponentNode
    sources: List[ReaderInlineQuerySource] = Field(default_factory=list)


ReaderComponentNode.model_rebuild()


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
    status: Literal["pending", "running", "completed", "failed", "timeout", "cancelled"]
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
