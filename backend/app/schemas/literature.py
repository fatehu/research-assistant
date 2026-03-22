"""
文献管理 Schema
"""
from datetime import datetime
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Literal, Optional, Union
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
    has_more: bool = False
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


class ImportPaperByLinkRequest(BaseModel):
    """通过链接导入论文"""
    link: str = Field(..., min_length=3)
    collection_ids: List[int] = Field(default_factory=list)


class ImportPaperByLinkResponse(BaseModel):
    """通过链接导入论文的响应"""
    paper: PaperResponse
    already_exists: bool = False
    resolved_source: str
    normalized_link: str


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
    pipeline_version: Optional[str] = None
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


class ReaderGenerativePlanRequest(ReaderComposeRequest):
    user_intent: Optional[str] = None


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
    source_layout_id: Optional[str] = None
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


class ReaderEnrichmentTarget(BaseModel):
    target_id: str
    node_id: str
    target_kind: Literal["section", "paragraph", "figure", "table", "equation", "structure"]
    component_type: str
    title: str = ""
    excerpt: str = ""
    source_block_ids: List[str] = Field(default_factory=list)
    source_atom_ids: List[str] = Field(default_factory=list)
    section_label: str = ""
    figure_label: str = ""
    suggested_resource_types: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderEnrichmentBundle(BaseModel):
    version: str = "v1"
    targets: List[ReaderEnrichmentTarget] = Field(default_factory=list)
    resource_modules: List[Dict[str, Any]] = Field(default_factory=list)
    interaction_modules: List[Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderStoryClaim(BaseModel):
    claim_id: str
    text: str = ""
    display_text: str = ""
    source_target_ids: List[str] = Field(default_factory=list)
    strength: Literal["primary", "supporting"] = "supporting"


class ReaderStoryEvidenceUnit(BaseModel):
    evidence_id: str
    kind: Literal["figure", "paragraph", "table", "equation", "section"] = "paragraph"
    role: str = ""
    title: str = ""
    source_target_ids: List[str] = Field(default_factory=list)


class ReaderStoryTermGap(BaseModel):
    term: str
    reason: str = ""
    source_target_ids: List[str] = Field(default_factory=list)


class ReaderStoryBackgroundGap(BaseModel):
    topic: str
    reason: str = ""
    suggested_resource_type: str = ""


class ReaderStoryNarrativeTurn(BaseModel):
    turn_id: str
    kind: str
    label: str = ""
    target_ids: List[str] = Field(default_factory=list)


class ReaderStorySubstrate(BaseModel):
    version: str = "v1"
    page_id: str = ""
    main_claims: List[ReaderStoryClaim] = Field(default_factory=list)
    evidence_units: List[ReaderStoryEvidenceUnit] = Field(default_factory=list)
    terms_to_explain: List[ReaderStoryTermGap] = Field(default_factory=list)
    background_gaps: List[ReaderStoryBackgroundGap] = Field(default_factory=list)
    narrative_turns: List[ReaderStoryNarrativeTurn] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderPageBrief(BaseModel):
    version: str = "v1"
    page_goal: str = ""
    reader_type: str = "curious_generalist"
    page_archetype: Literal[
        "figure_explainer",
        "finding_digest",
        "methods_decoder",
        "concept_decoder",
        "context_builder",
    ] = "finding_digest"
    hero_angle: str = ""
    primary_focus_target_id: str = ""
    secondary_support_target_ids: List[str] = Field(default_factory=list)
    body_flow_target_ids: List[str] = Field(default_factory=list)
    reading_path: List[str] = Field(default_factory=list)
    interaction_opportunities: List[str] = Field(default_factory=list)
    resource_gaps: List[str] = Field(default_factory=list)
    experience_hooks: List[str] = Field(default_factory=list)
    resource_strategy: str = ""
    storyboard: List["ReaderPageStoryboardBeat"] = Field(default_factory=list)
    content_budget: Dict[str, int] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderPageStoryboardBeat(BaseModel):
    beat_id: str
    role: str = ""
    section_type: str = ""
    title: str = ""
    purpose: str = ""
    reader_goal: str = ""
    continuity_note: str = ""
    target_ids: List[str] = Field(default_factory=list)
    tool_objectives: List[str] = Field(default_factory=list)
    block_stack: List[str] = Field(default_factory=list)
    drop_notes: List[str] = Field(default_factory=list)
    priority: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGenerativeResourceModule(BaseModel):
    module_id: str
    module_type: str
    target_ids: List[str] = Field(default_factory=list)
    title: str = ""
    display_title: str = ""
    summary: str = ""
    display_summary: str = ""
    links: List[Dict[str, Any]] = Field(default_factory=list)
    source: Literal["agent", "paper_read", "knowledge_search", "web", "mcp", "fallback", "paper_assets", "metadata", "tool_trace"] = "agent"
    interaction_mode: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGenerativeInteractionModule(BaseModel):
    module_id: str
    module_type: str
    target_ids: List[str] = Field(default_factory=list)
    title: str = ""
    display_title: str = ""
    display_summary: str = ""
    props: Dict[str, Any] = Field(default_factory=dict)
    source: Literal["agent", "paper_read", "knowledge_search", "web", "mcp", "fallback", "paper_assets", "metadata", "tool_trace"] = "agent"
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGenerativeJsWidgetPlan(BaseModel):
    widget_id: str
    widget_type: str
    target_ids: List[str] = Field(default_factory=list)
    title: str = ""
    display_title: str = ""
    display_summary: str = ""
    data_requirements: List[str] = Field(default_factory=list)
    props: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderAdjacentPageItem(BaseModel):
    label: str = ""
    description: str = ""


class ReaderAdjacentPageContext(BaseModel):
    page: int = Field(default=0, ge=0)
    relation: str = ""
    reference_only: bool = True
    source: str = ""
    summary: str = ""
    body_text: str = ""
    figures: List[ReaderAdjacentPageItem] = Field(default_factory=list)
    tables: List[ReaderAdjacentPageItem] = Field(default_factory=list)
    equations: List[ReaderAdjacentPageItem] = Field(default_factory=list)
    continuation_hints: List[str] = Field(default_factory=list)
    raw_text: str = ""


class ReadingDossierV2AdjacentPageImage(BaseModel):
    url: str = ""
    width: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=0)


_LEGACY_ADJACENT_JSON_STUFFING_KEYS = (
    '"page"',
    '"relation"',
    '"content_stream"',
    '"page_summary"',
    '"summary"',
    '"body_text"',
    '"continuation_hints"',
    '"reference_only"',
    '"fidelity"',
)


def _looks_like_legacy_adjacent_payload_stuffing(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 24:
        return False
    candidate = text.lstrip("`").strip()
    if not candidate.startswith(("{", "[")):
        return False
    hits = sum(1 for key in _LEGACY_ADJACENT_JSON_STUFFING_KEYS if key in candidate)
    if hits >= 2 and ("content_stream" in candidate or "body_text" in candidate or "page_summary" in candidate):
        return True
    return bool(
        re.search(
            r'^\{\s*"page"\s*:\s*\d+.*"(content_stream|body_text|page_summary|summary)"\s*:',
            candidate,
            re.DOTALL,
        )
    )


class ReadingDossierV2AdjacentContentStreamItem(BaseModel):
    seq: int = Field(..., ge=1)
    type: Literal["paragraph", "figure", "table", "equation", "caption", "header", "footer"]
    text: str = ""
    ocr_text: str = ""
    role: str = ""
    label: str = ""
    caption: str = ""
    description: str = ""
    columns: List[str] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)
    normalized_text: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_content_payload(self):
        if not any(
            [
                str(self.text or "").strip(),
                str(self.ocr_text or "").strip(),
                str(self.caption or "").strip(),
                str(self.description or "").strip(),
                str(self.normalized_text or "").strip(),
                bool(self.columns),
                bool(self.rows),
            ]
        ):
            raise ValueError("adjacent content_stream item must preserve extracted content")
        if _looks_like_legacy_adjacent_payload_stuffing(self.text):
            raise ValueError(
                "adjacent content_stream item text contains legacy JSON payload stuffing; invalid ordered_structured_context"
            )
        if _looks_like_legacy_adjacent_payload_stuffing(self.ocr_text):
            raise ValueError(
                "adjacent content_stream item ocr_text contains legacy JSON payload stuffing; invalid ordered_structured_context"
            )
        return self


class ReadingDossierV2AdjacentPageRow(BaseModel):
    page: int = Field(default=0, ge=1)
    relation: Literal["previous_page", "next_page"]
    source: str = ""
    fidelity: Literal["ordered_structured_context"] = "ordered_structured_context"
    reference_only: bool = False
    page_image: ReadingDossierV2AdjacentPageImage = Field(default_factory=ReadingDossierV2AdjacentPageImage)
    page_summary: str = ""
    content_stream: List[ReadingDossierV2AdjacentContentStreamItem] = Field(default_factory=list)
    continuation_hints: List[str] = Field(default_factory=list)
    raw_text: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_structured_context(self):
        if self.reference_only:
            raise ValueError("reading_dossier_v2 adjacent rows cannot remain reference_only")
        if not list(self.content_stream or []):
            raise ValueError(
                "neighboring-page structured context not implemented: adjacent page row missing ordered content_stream"
            )
        seqs = [int(item.seq) for item in list(self.content_stream or [])]
        if seqs != sorted(seqs):
            raise ValueError("adjacent page content_stream must preserve page reading order")
        if len(seqs) != len(set(seqs)):
            raise ValueError("adjacent page content_stream seq values must be unique")
        return self


class ReaderGenerativePlan(BaseModel):
    version: str = "v1"
    status: Literal["draft", "done", "fallback"] = "draft"
    shell_mode: str = "resource_augmented_reader"
    story_substrate: ReaderStorySubstrate = Field(default_factory=ReaderStorySubstrate)
    page_brief: ReaderPageBrief = Field(default_factory=ReaderPageBrief)
    rationale: List[str] = Field(default_factory=list)
    resource_modules: List[ReaderGenerativeResourceModule] = Field(default_factory=list)
    interaction_modules: List[ReaderGenerativeInteractionModule] = Field(default_factory=list)
    js_widgets: List[ReaderGenerativeJsWidgetPlan] = Field(default_factory=list)
    used_tools: List[str] = Field(default_factory=list)
    tool_trace: List[Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceHero(BaseModel):
    title: str = ""
    display_title: str = ""
    subtitle: str = ""
    display_subtitle: str = ""
    summary: str = ""
    display_summary: str = ""
    focus_label: str = ""
    target_ids: List[str] = Field(default_factory=list)
    claim_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceUiAction(BaseModel):
    action_id: str
    action_type: str
    label: str = ""
    target_ref: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    event_name: str = ""
    agent_handoff: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceEventBinding(BaseModel):
    event_id: str
    event_name: str
    event_source: Literal["user", "agent", "system"] = "user"
    event_type: str = ""
    action_ids: List[str] = Field(default_factory=list)
    target_ref: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceBlockRef(BaseModel):
    block_id: str
    block_type: Literal["resource_module", "interaction_module", "widget"]
    version: str = "block_ref_v1"
    ref_id: str = ""
    variant: str = ""
    target_ids: List[str] = Field(default_factory=list)
    priority: int = 0
    state: Literal["ready", "empty", "loading", "partial", "error"] = "ready"
    data_requirements: List[str] = Field(default_factory=list)
    fallback_policy: str = "omit"
    user_actions: List[str] = Field(default_factory=list)
    agent_actions: List[str] = Field(default_factory=list)
    ui_actions: List[ReaderExperienceUiAction] = Field(default_factory=list)
    event_bindings: List[ReaderExperienceEventBinding] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceSection(BaseModel):
    section_id: str
    section_type: Literal[
        "hero",
        "focus_stage",
        "reading_flow",
        "explainer_cluster",
        "supporting_resources",
        "question_lab",
        "story_map",
    ]
    title: str = ""
    display_title: str = ""
    summary: str = ""
    display_summary: str = ""
    target_ids: List[str] = Field(default_factory=list)
    section_region: Literal["main", "sidebar", "footer"] = "main"
    layout_variant: str = ""
    blocks: List[ReaderExperienceBlockRef] = Field(default_factory=list)
    resource_module_ids: List[str] = Field(default_factory=list)
    interaction_module_ids: List[str] = Field(default_factory=list)
    widget_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceGuidedBeat(BaseModel):
    beat_id: str
    beat_type: str = ""
    section_type_hint: str = ""
    title: str = ""
    display_title: str = ""
    summary: str = ""
    display_summary: str = ""
    reader_goal: str = ""
    continuity_note: str = ""
    target_ids: List[str] = Field(default_factory=list)
    tool_objectives: List[str] = Field(default_factory=list)
    block_stack: List[ReaderExperienceBlockRef] = Field(default_factory=list)
    drop_notes: List[str] = Field(default_factory=list)
    importance: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderTeachingManuscriptReferenceLink(BaseModel):
    label: str = ""
    href: str = ""
    note: str = ""


class ReaderTeachingManuscriptGlossaryItem(BaseModel):
    term: str = ""
    note: str = ""
    target_ids: List[str] = Field(default_factory=list)


class ReaderTeachingManuscriptSegment(BaseModel):
    segment_id: str
    segment_type: str = ""
    title: str = ""
    teaching_text: str = ""
    anchor_excerpt: str = ""
    target_ids: List[str] = Field(default_factory=list)
    full_evidence_target_ids: List[str] = Field(default_factory=list)
    glossary: List[ReaderTeachingManuscriptGlossaryItem] = Field(default_factory=list)
    adjacent_bridge: str = ""
    reference_links: List[ReaderTeachingManuscriptReferenceLink] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderTeachingManuscript(BaseModel):
    version: str = "v1"
    status: Literal["draft", "done", "fallback"] = "draft"
    segments: List[ReaderTeachingManuscriptSegment] = Field(default_factory=list)


class ReaderExperiencePlan(BaseModel):
    version: str = "v1"
    status: Literal["draft", "done", "fallback"] = "draft"
    scope: Literal["paper", "section", "page_focus"] = "paper"
    focus_page: int = Field(default=1, ge=1)
    reader_profile: str = "curious_generalist"
    layout_variant: str = "resource_augmented_reader"
    fidelity_mode: Literal["strict", "light_repair", "guided_explainer"] = "light_repair"
    page_story_title: str = ""
    page_story_subtitle: str = ""
    narrative_goal: str = ""
    hero: ReaderExperienceHero = Field(default_factory=ReaderExperienceHero)
    main_sections: List[ReaderExperienceSection] = Field(default_factory=list)
    guided_beats: List[ReaderExperienceGuidedBeat] = Field(default_factory=list)
    teaching_manuscript: Optional[ReaderTeachingManuscript] = None
    supporting_resources: List[ReaderGenerativeResourceModule] = Field(default_factory=list)
    interactive_blocks: List[ReaderGenerativeInteractionModule] = Field(default_factory=list)
    widget_blocks: List[ReaderGenerativeJsWidgetPlan] = Field(default_factory=list)
    reading_path: List[str] = Field(default_factory=list)
    used_tools: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGroundingPoint(BaseModel):
    x: float = 0.0
    y: float = 0.0


class ReaderGroundingBlock(BaseModel):
    block_index: int = 0
    text: str = ""
    pos: List[ReaderGroundingPoint] = Field(default_factory=list)
    style_id: int = 0


class ReaderGroundingTableCell(BaseModel):
    cell_id: int = 0
    row_start: int = 0
    row_end: int = 0
    col_start: int = 0
    col_end: int = 0
    text: str = ""
    layout_ids: List[str] = Field(default_factory=list)
    polygons: List[List[ReaderGroundingPoint]] = Field(default_factory=list)


class ReaderGroundingLayoutAtom(BaseModel):
    layout_id: str = ""
    reading_order: int = 0
    layout_type: str = ""
    layout_sub_type: str = ""
    raw_text: str = ""
    clean_text: str = ""
    normalized_text: str = ""
    normalization_reason: str = ""
    normalization_mode: str = ""
    normalization_confidence: Optional[float] = None
    alignment: str = ""
    line_height: float = 0.0
    layout_pos: List[ReaderGroundingPoint] = Field(default_factory=list)
    blocks: List[ReaderGroundingBlock] = Field(default_factory=list)
    table_cells: List[ReaderGroundingTableCell] = Field(default_factory=list)
    canonical_block_ids: List[str] = Field(default_factory=list)
    node_kind: str = ""
    include_in_main_flow: bool = True
    region_hint: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGroundingReadingNode(BaseModel):
    node_id: str = ""
    node_kind: str = ""
    raw_text: str = ""
    clean_text: str = ""
    normalized_text: str = ""
    normalization_reason: str = ""
    normalization_mode: str = ""
    normalization_confidence: Optional[float] = None
    source_layout_ids: List[str] = Field(default_factory=list)
    source_block_ids: List[str] = Field(default_factory=list)
    include_in_main_flow: bool = True
    region_hint: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGroundingEvidenceEntry(BaseModel):
    evidence_id: str = ""
    source_layout_id: str = ""
    source_block_ids: List[str] = Field(default_factory=list)
    layout_pos: List[ReaderGroundingPoint] = Field(default_factory=list)
    block_positions: List[List[ReaderGroundingPoint]] = Field(default_factory=list)
    table_cells: List[ReaderGroundingTableCell] = Field(default_factory=list)
    geometry_source: str = "docmind_layout_blocks"
    highlight_strategy: str = "layout_block_union"
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGroundingPageImage(BaseModel):
    url: str = ""
    path: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    source: str = ""
    origin_url: str = ""
    local_cached: bool = False


class ReaderPageGrounding(BaseModel):
    version: str = "page_grounding_v1"
    page: int = Field(default=1, ge=1)
    layout_atoms: List[ReaderGroundingLayoutAtom] = Field(default_factory=list)
    reading_nodes: List[ReaderGroundingReadingNode] = Field(default_factory=list)
    evidence_map: List[ReaderGroundingEvidenceEntry] = Field(default_factory=list)
    page_image: ReaderGroundingPageImage = Field(default_factory=ReaderGroundingPageImage)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReadingDossierV2CurrentPageLane(BaseModel):
    owner: str = "compose/page_grounding_v1"
    fidelity: str = "grounded_evidence"
    build_meta: Dict[str, Any] = Field(default_factory=dict)
    rich_grounding: ReaderPageGrounding = Field(default_factory=ReaderPageGrounding)


class ReadingDossierV2AdjacentPagesLimits(BaseModel):
    reference_only: bool = False
    max_pages: int = Field(default=2, ge=0, le=8)
    max_page_summary_chars: int = Field(default=400, ge=32, le=4000)
    max_content_stream_items: int = Field(default=48, ge=1, le=256)
    max_continuation_hints: int = Field(default=6, ge=1, le=24)
    max_raw_text_chars: int = Field(default=1600, ge=64, le=24000)


class ReadingDossierV2AdjacentPagesLane(BaseModel):
    owner: str = "api/adjacent_page_extraction"
    fidelity: Literal["ordered_structured_context"] = "ordered_structured_context"
    reference_only: bool = False
    limits: ReadingDossierV2AdjacentPagesLimits = Field(default_factory=ReadingDossierV2AdjacentPagesLimits)
    pages: List[ReadingDossierV2AdjacentPageRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_pages(self):
        if self.reference_only:
            raise ValueError("reading_dossier_v2 adjacent_pages lane cannot be reference_only")
        return self


class ReadingDossierV2DerivedAdjacentBridgeCue(BaseModel):
    cue_id: str = ""
    from_page: int = Field(default=0, ge=0)
    to_page: int = Field(default=0, ge=0)
    text: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReadingDossierV2DerivedAdjacentBridgeCuesLane(BaseModel):
    owner: str = "runtime"
    fidelity: str = "derived_summary"
    items: List[ReadingDossierV2DerivedAdjacentBridgeCue] = Field(default_factory=list)


class ReadingDossierV2CacheMetaLane(BaseModel):
    dossier_namespace: str = "lit:reading_dossier:v2"
    compose_pipeline_version: str = ""
    source_sig_hash: str = ""
    adjacent_context_parser_version: str = ""
    adjacent_context_sig_hash: str = ""
    adjacent_context_page_scope_version: str = "ordered_structured_context.v1"


class ReadingDossierV2(BaseModel):
    version: Literal["reading_dossier_v2"] = "reading_dossier_v2"
    dossier_contract: str = "rd2.v1"
    focus_page: int = Field(default=1, ge=1)
    reader_profile: str = "curious_generalist"
    compose_source_signature: str = ""
    current_page: ReadingDossierV2CurrentPageLane = Field(default_factory=ReadingDossierV2CurrentPageLane)
    adjacent_pages: ReadingDossierV2AdjacentPagesLane = Field(default_factory=ReadingDossierV2AdjacentPagesLane)
    derived_adjacent_bridge_cues: ReadingDossierV2DerivedAdjacentBridgeCuesLane = Field(
        default_factory=ReadingDossierV2DerivedAdjacentBridgeCuesLane
    )
    cache_meta: ReadingDossierV2CacheMetaLane = Field(default_factory=ReadingDossierV2CacheMetaLane)
    meta: Dict[str, Any] = Field(default_factory=dict)


class PageArtifactV2AuthoredPlanInput(BaseModel):
    template_id: str = Field(..., min_length=1)
    layout_recipe: str = Field(..., min_length=1)
    presentation_mode: str = Field(..., min_length=1)
    widget_family: str = Field(..., min_length=1)
    motion_preset: str = Field(..., min_length=1)
    interaction_policy: str = Field(..., min_length=1)
    authored_explanations: List[str] = Field(default_factory=list)
    authored_text_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    excerpt_overrides: List[Dict[str, Any]] = Field(default_factory=list)
    figure_slots: List[Dict[str, Any]] = Field(default_factory=list)
    table_slots: List[Dict[str, Any]] = Field(default_factory=list)
    equation_slots: List[Dict[str, Any]] = Field(default_factory=list)
    media_slots: List[Dict[str, Any]] = Field(default_factory=list)
    aside_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    term_annotations: List[Dict[str, Any]] = Field(default_factory=list)
    external_resources: List[Dict[str, Any]] = Field(default_factory=list)
    requested_node_kinds: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_authored_explanations(self):
        cleaned = [str(item).strip() for item in list(self.authored_explanations or []) if str(item).strip()]
        self.authored_explanations = cleaned
        normalized_text_blocks: List[Dict[str, Any]] = []
        for raw_item in list(self.authored_text_blocks or []):
            if not isinstance(raw_item, Mapping):
                continue
            segment_kind = str(raw_item.get("segment_kind") or "").strip()
            text = str(raw_item.get("text") or "").strip()
            if segment_kind not in {"heading", "paragraph", "authored_explanation"}:
                raise ValueError("authored_text_blocks only support heading, paragraph, or authored_explanation kinds")
            if not text:
                raise ValueError("authored_text_blocks require non-empty text")
            normalized_text_blocks.append(
                {
                    "segment_kind": segment_kind,
                    "text": text,
                    "meta": dict(raw_item.get("meta") or {}),
                }
            )
        self.authored_text_blocks = normalized_text_blocks
        if not cleaned and not normalized_text_blocks:
            raise ValueError("authored plan requires at least one non-empty authored text block or explanation")
        return self


class ExperienceSessionV2ArtifactDraftResourceRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    tool_name: Literal["paper_read", "knowledge_search", "web_search", "web_scrape"]
    query: str = ""
    url: str = ""
    reason: str = ""
    max_results: int = Field(default=3, ge=1, le=6)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_request_shape(cls, value: Any):
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        tool_name = str(
            payload.get("tool_name")
            or payload.get("tool")
            or payload.get("name")
            or payload.get("requested_tool")
            or ""
        ).strip()
        if tool_name and not str(payload.get("tool_name") or "").strip():
            payload["tool_name"] = tool_name

        if not str(payload.get("query") or "").strip():
            query = str(payload.get("q") or payload.get("search_query") or payload.get("search_term") or "").strip()
            if query:
                payload["query"] = query

        if not str(payload.get("url") or "").strip():
            url = str(payload.get("href") or payload.get("target_url") or "").strip()
            if url:
                payload["url"] = url

        if not str(payload.get("reason") or "").strip():
            reason = str(payload.get("purpose") or payload.get("why") or payload.get("note") or "").strip()
            if reason:
                payload["reason"] = reason

        if not str(payload.get("request_id") or "").strip():
            seed = "|".join(
                part
                for part in [
                    str(payload.get("tool_name") or "").strip(),
                    str(payload.get("query") or "").strip(),
                    str(payload.get("url") or "").strip(),
                    str(payload.get("reason") or "").strip(),
                ]
                if part
            )
            if seed:
                payload["request_id"] = f"req-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"
        return payload

    @model_validator(mode="after")
    def _validate_request_shape(self):
        if self.tool_name == "web_scrape":
            if not str(self.url or "").strip():
                raise ValueError("artifact_draft web_scrape requests require url")
            return self
        if not str(self.query or "").strip():
            raise ValueError("artifact_draft retrieval requests require query")
        return self


def _artifact_draft_node_collect_string_list(
    payload: Mapping[str, Any],
    *,
    direct_keys: Sequence[str],
    nested_keys: Sequence[str] = (),
    object_id_keys: Sequence[str] = (),
) -> List[str]:
    values: List[str] = []

    def _append(raw: Any):
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                values.append(text)
            return
        if isinstance(raw, Mapping):
            for object_key in object_id_keys:
                candidate = str(raw.get(object_key) or "").strip()
                if candidate:
                    values.append(candidate)
                    return
            return
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            for item in raw:
                _append(item)

    for key in direct_keys:
        _append(payload.get(key))
    for key in nested_keys:
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            _append(nested)
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
            for item in nested:
                _append(item)

    deduped: List[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


_ARTIFACT_DRAFT_ALLOWED_READER_ROLES = {
    "anchor_excerpt",
    "teaching_explanation",
    "continuity_bridge",
    "support_note",
    "visual_evidence",
}


def _normalize_artifact_draft_reader_role(node_kind: str, raw_value: Any) -> str:
    role = str(raw_value or "").strip().lower()
    aliases = {
        "anchor": "anchor_excerpt",
        "excerpt": "anchor_excerpt",
        "anchor_quote": "anchor_excerpt",
        "teaching": "teaching_explanation",
        "explanation": "teaching_explanation",
        "teaching_note": "teaching_explanation",
        "bridge": "continuity_bridge",
        "continuity": "continuity_bridge",
        "support": "support_note",
        "note": "support_note",
        "visual": "visual_evidence",
        "visual_anchor": "visual_evidence",
        "media": "visual_evidence",
    }
    if role in aliases:
        role = aliases[role]
    if role:
        return role
    if node_kind == "original_excerpt":
        return "anchor_excerpt"
    if node_kind == "paragraph":
        return "teaching_explanation"
    if node_kind in {"figure_slot", "table_slot", "equation_slot"}:
        return "visual_evidence"
    if node_kind in {"aside", "term_note", "external_resource"}:
        return "support_note"
    return ""


class ExperienceSessionV2ArtifactDraftNode(BaseModel):
    node_kind: Literal[
        "heading",
        "paragraph",
        "original_excerpt",
        "figure_slot",
        "table_slot",
        "equation_slot",
        "aside",
        "term_note",
        "external_resource",
    ]
    text: str = ""
    display_text: str = ""
    translation_zh: str = ""
    label: str = ""
    caption: str = ""
    term: str = ""
    definition: str = ""
    source_layout_ids: List[str] = Field(default_factory=list)
    source_block_ids: List[str] = Field(default_factory=list)
    resource_ref_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_node_shape(cls, value: Any):
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        node_kind = str(
            payload.get("node_kind")
            or payload.get("kind")
            or payload.get("type")
            or payload.get("node_type")
            or ""
        ).strip()
        if node_kind:
            payload["node_kind"] = node_kind

        label = str(payload.get("label") or payload.get("title") or payload.get("name") or "").strip()
        if label and not str(payload.get("label") or "").strip():
            payload["label"] = label

        text = str(payload.get("text") or "").strip()
        if not text:
            if node_kind == "heading":
                text = str(payload.get("heading") or payload.get("title") or payload.get("label") or "").strip()
            elif node_kind in {"paragraph", "aside"}:
                text = str(
                    payload.get("content")
                    or payload.get("body")
                    or payload.get("paragraph_text")
                    or payload.get("aside_text")
                    or payload.get("summary")
                    or ""
                ).strip()
            elif node_kind == "term_note":
                text = str(payload.get("definition") or payload.get("content") or payload.get("body") or "").strip()
        if text and not str(payload.get("text") or "").strip():
            payload["text"] = text

        display_text = str(payload.get("display_text") or "").strip()
        if not display_text and node_kind == "original_excerpt":
            display_text = str(
                payload.get("excerpt")
                or payload.get("quote")
                or payload.get("content")
                or payload.get("text")
                or ""
            ).strip()
        if display_text and not str(payload.get("display_text") or "").strip():
            payload["display_text"] = display_text

        translation_zh = str(payload.get("translation_zh") or "").strip()
        if not translation_zh and node_kind == "original_excerpt":
            translation_zh = str(
                payload.get("translation")
                or payload.get("translation_cn")
                or payload.get("zh_translation")
                or payload.get("reader_translation_zh")
                or payload.get("chinese_translation")
                or ""
            ).strip()
        if translation_zh and not str(payload.get("translation_zh") or "").strip():
            payload["translation_zh"] = translation_zh

        definition = str(payload.get("definition") or "").strip()
        if not definition and node_kind == "term_note":
            definition = str(payload.get("text") or payload.get("content") or "").strip()
        if definition and not str(payload.get("definition") or "").strip():
            payload["definition"] = definition

        if not str(payload.get("caption") or "").strip() and node_kind in {"figure_slot", "table_slot", "equation_slot"}:
            caption = str(payload.get("description") or payload.get("summary") or payload.get("text") or "").strip()
            if caption:
                payload["caption"] = caption

        layout_ids = _artifact_draft_node_collect_string_list(
            payload,
            direct_keys=("source_layout_ids", "source_layout_id", "layout_id", "layout_ids", "anchor_layout_id"),
            nested_keys=("source_ref", "source_refs", "figure_ref", "table_ref", "equation_ref", "media_ref", "binding"),
            object_id_keys=("source_layout_id", "layout_id", "anchor_layout_id", "source_layout_ref"),
        )
        if layout_ids:
            payload["source_layout_ids"] = layout_ids

        block_ids = _artifact_draft_node_collect_string_list(
            payload,
            direct_keys=("source_block_ids", "source_block_id", "block_id", "block_ids"),
            nested_keys=("source_ref", "source_refs", "binding"),
            object_id_keys=("source_block_id", "block_id", "source_block_ref"),
        )
        if block_ids:
            payload["source_block_ids"] = block_ids

        resource_ref_ids = _artifact_draft_node_collect_string_list(
            payload,
            direct_keys=("resource_ref_ids", "resource_ref_id", "resource_ids", "resource_id"),
            nested_keys=("resource_refs", "resources", "bundle_entries", "resource_ref", "resource"),
            object_id_keys=("resource_id", "resource_ref_id"),
        )
        if resource_ref_ids:
            payload["resource_ref_ids"] = resource_ref_ids

        meta = dict(payload.get("meta") or {})
        node_id = str(payload.get("node_id") or "").strip()
        if node_id:
            meta.setdefault("node_id", node_id)
        for source_key, meta_key in (
            ("lane", "lane"),
            ("placement", "placement"),
            ("prominence", "prominence"),
            ("group_id", "group_id"),
            ("group_label", "group_label"),
            ("section_id", "section_id"),
            ("section_label", "section_label"),
        ):
            value = str(payload.get(source_key) or "").strip()
            if value:
                meta.setdefault(meta_key, value)
        reader_role = _normalize_artifact_draft_reader_role(
            node_kind,
            payload.get("reader_role")
            or meta.get("reader_role")
            or payload.get("role")
            or payload.get("readerRole"),
        )
        if reader_role:
            meta.setdefault("reader_role", reader_role)
        if meta:
            payload["meta"] = meta
        return payload

    @model_validator(mode="after")
    def _validate_node_shape(self):
        if self.node_kind in {"heading", "paragraph", "aside"} and not str(self.text or "").strip():
            raise ValueError(f"{self.node_kind} nodes require text")
        if self.node_kind == "original_excerpt":
            if not str(self.display_text or "").strip():
                raise ValueError("original_excerpt nodes require display_text")
            if not list(self.source_layout_ids or []) and not list(self.source_block_ids or []):
                raise ValueError("original_excerpt nodes require source ids")
        if self.node_kind in {"figure_slot", "table_slot", "equation_slot"}:
            if not list(self.source_layout_ids or []) and not str(self.meta.get("source_layout_id") or "").strip():
                raise ValueError(f"{self.node_kind} nodes require source_layout_ids")
        if self.node_kind == "term_note":
            if not str(self.term or self.label or "").strip():
                raise ValueError("term_note nodes require term or label")
            if not str(self.definition or self.text or "").strip():
                raise ValueError("term_note nodes require definition or text")
        if self.node_kind == "external_resource":
            if not list(self.resource_ref_ids or []):
                raise ValueError("external_resource nodes require resource_ref_ids")
            if not str(self.label or self.text or "").strip():
                raise ValueError("external_resource nodes require label or text")
        reader_role = str((self.meta or {}).get("reader_role") or "").strip()
        if reader_role and reader_role not in _ARTIFACT_DRAFT_ALLOWED_READER_ROLES:
            raise ValueError(
                "artifact_draft nodes only support reader_role values: "
                + ", ".join(sorted(_ARTIFACT_DRAFT_ALLOWED_READER_ROLES))
            )
        return self


class ExperienceSessionV2ArtifactDraft(BaseModel):
    version: Literal["experience_session_artifact_draft_v1"] = "experience_session_artifact_draft_v1"
    focus_page: int = Field(default=1, ge=1)
    template_hint: str = Field(..., min_length=1)
    layout_recipe: str = Field(..., min_length=1)
    presentation_mode: str = Field(..., min_length=1)
    widget_family: str = "reader_v2_surface"
    motion_preset: str = "calm_progressive"
    interaction_policy: str = "reader_first_guided"
    nodes: List[ExperienceSessionV2ArtifactDraftNode] = Field(default_factory=list)
    resource_requests: List[ExperienceSessionV2ArtifactDraftResourceRequest] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_draft_shape(cls, value: Any):
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        draft_meta = dict(payload.get("meta") or {})

        def _normalize_top_level_string_field(
            field_name: str,
            *,
            alias_keys: Sequence[str],
            default_value: str,
        ) -> None:
            raw_value = payload.get(field_name)
            if isinstance(raw_value, str) and raw_value.strip():
                payload[field_name] = raw_value.strip()
                return
            for alias_key in alias_keys:
                alias_value = payload.get(alias_key)
                if isinstance(alias_value, str) and alias_value.strip():
                    payload[field_name] = alias_value.strip()
                    return
            if isinstance(raw_value, Mapping):
                draft_meta.setdefault(f"{field_name}_config", dict(raw_value))
                preferred = ""
                for key in ("recipe", "mode", "name", "id", "variant", "style", "label"):
                    item = raw_value.get(key)
                    if isinstance(item, str) and item.strip():
                        preferred = item.strip()
                        break
                if preferred:
                    payload[field_name] = preferred
                    return
                compact_parts: List[str] = []
                for key, item in raw_value.items():
                    if isinstance(item, str) and item.strip():
                        compact_parts.append(f"{key}:{item.strip()}")
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        compact_parts.append(f"{key}:{item}")
                    if len(compact_parts) >= 4:
                        break
                if compact_parts:
                    payload[field_name] = "|".join(compact_parts)
                    return
                payload[field_name] = default_value
                return
            if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes, bytearray)):
                draft_meta.setdefault(f"{field_name}_config", list(raw_value))
                compact_items = [str(item).strip() for item in raw_value if str(item).strip()]
                payload[field_name] = "|".join(compact_items[:4]) if compact_items else default_value
                return
            payload[field_name] = default_value

        if not str(payload.get("template_hint") or "").strip() or not isinstance(payload.get("template_hint"), str):
            _normalize_top_level_string_field(
                "template_hint",
                alias_keys=("template_id", "template"),
                default_value="guided_mixed_media_v1",
            )
        if not str(payload.get("layout_recipe") or "").strip() or not isinstance(payload.get("layout_recipe"), str):
            _normalize_top_level_string_field(
                "layout_recipe",
                alias_keys=("layout", "layout_strategy"),
                default_value="current_page_spine_interleave_v1",
            )
        if not str(payload.get("presentation_mode") or "").strip() or not isinstance(payload.get("presentation_mode"), str):
            _normalize_top_level_string_field(
                "presentation_mode",
                alias_keys=("presentation", "presentation_strategy"),
                default_value="mixed_layout",
            )
        if "resource_requests" not in payload and isinstance(payload.get("retrieval_requests"), Sequence):
            payload["resource_requests"] = list(payload.get("retrieval_requests") or [])
        if draft_meta:
            payload["meta"] = draft_meta
        return payload

    @model_validator(mode="after")
    def _validate_draft(self):
        if not list(self.nodes or []):
            raise ValueError("artifact_draft requires nodes")
        if not any(node.node_kind in {"heading", "paragraph", "original_excerpt"} for node in list(self.nodes or [])):
            raise ValueError("artifact_draft requires guided-reading narrative nodes")
        main_nodes = [
            node
            for node in list(self.nodes or [])
            if str((node.meta or {}).get("lane") or "main").strip() != "support"
        ]
        excerpt_nodes = [node for node in main_nodes if node.node_kind == "original_excerpt"]
        paragraph_nodes = [node for node in main_nodes if node.node_kind == "paragraph"]
        if excerpt_nodes and not paragraph_nodes:
            raise ValueError("artifact_draft requires teaching paragraphs alongside selected excerpts")
        return self


class PageArtifactV2ReadingBlock(BaseModel):
    segment_id: str = Field(..., min_length=1)
    segment_kind: Literal[
        "heading",
        "paragraph",
        "original_excerpt",
        "authored_explanation",
        "figure_slot",
        "table_slot",
        "equation_slot",
        "media_slot",
        "aside_content",
        "term_annotation",
        "external_resource",
    ]
    source_lane: Literal["current_page", "authoring_plan"] = "current_page"
    page: int = Field(default=1, ge=1)
    text: str = ""
    source_layout_ids: List[str] = Field(default_factory=list)
    source_block_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_text(self):
        text = str(self.text or "").strip()
        if not text:
            raise ValueError("reading block text cannot be empty")
        self.text = text
        if self.segment_kind in {"figure_slot", "table_slot", "equation_slot", "media_slot"}:
            meta = dict(self.meta or {})
            binding = meta.get("media_binding") or meta.get("figure_binding") or {}
            binding_kind = str(binding.get("binding_kind") or meta.get("binding_kind") or "").strip()
            page_asset_ref = str(
                binding.get("page_asset_ref")
                or binding.get("page_image_url")
                or binding.get("page_image_path")
                or meta.get("page_image_url")
                or ""
            ).strip()
            if not page_asset_ref:
                raise ValueError(f"{self.segment_kind} blocks must include current-page asset bindings")
            if binding_kind.endswith("_layout_anchor"):
                if not list(self.source_layout_ids or []):
                    raise ValueError(f"{self.segment_kind} blocks must include resolved current-page layout bindings")
                if not list(self.evidence_ids or []):
                    raise ValueError(f"{self.segment_kind} blocks must include resolved current-page evidence bindings")
            elif binding_kind != "page_image_anchor":
                raise ValueError(f"{self.segment_kind} blocks must declare a supported binding kind")
        return self


class PageArtifactV2CurrentPageSpine(BaseModel):
    page: int = Field(default=1, ge=1)
    owner: str = "reading_dossier_v2.current_page"
    primary: bool = True
    reading_node_ids: List[str] = Field(default_factory=list)
    layout_ids: List[str] = Field(default_factory=list)
    block_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    main_segment_ids: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_spine_anchors(self):
        has_anchor = any(
            [
                bool(self.reading_node_ids),
                bool(self.layout_ids),
                bool(self.block_ids),
                bool(self.evidence_ids),
            ]
        )
        if not has_anchor:
            raise ValueError("current_page_spine must include at least one grounding anchor")
        if not list(self.main_segment_ids or []):
            raise ValueError("current_page_spine must include main_segment_ids")
        return self


class PageArtifactV2ProvenanceLane(BaseModel):
    continuity_mode: Literal[
        "current_page_primary_ordered_adjacent_context",
    ] = "current_page_primary_ordered_adjacent_context"
    adjacent_context_pages: List[int] = Field(default_factory=list)
    include_adjacent_as_coequal_anchor: bool = False
    source_lanes: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class PageArtifactV2(BaseModel):
    version: Literal["page_artifact_v2"] = "page_artifact_v2"
    artifact_contract_id: Literal["page_artifact_v2.contract.v1"] = "page_artifact_v2.contract.v1"
    focus_page: int = Field(default=1, ge=1)
    reader_profile: str = "curious_generalist"
    dossier_signature: str = ""
    session_id: Optional[str] = None
    template_id: str = Field(..., min_length=1)
    layout_recipe: str = Field(..., min_length=1)
    presentation_mode: str = Field(..., min_length=1)
    widget_family: str = Field(..., min_length=1)
    motion_preset: str = Field(..., min_length=1)
    interaction_policy: str = Field(..., min_length=1)
    reading_blocks: List[PageArtifactV2ReadingBlock] = Field(default_factory=list)
    current_page_spine: PageArtifactV2CurrentPageSpine
    provenance: PageArtifactV2ProvenanceLane = Field(default_factory=PageArtifactV2ProvenanceLane)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_contract(self):
        if int(self.current_page_spine.page) != int(self.focus_page):
            raise ValueError("current_page_spine.page must equal focus_page")
        if bool(self.provenance.include_adjacent_as_coequal_anchor):
            raise ValueError("adjacent pages cannot be co-equal anchors in page_artifact_v2")

        original_segments = [item for item in list(self.reading_blocks or []) if item.segment_kind == "original_excerpt"]
        authored_segments = [
            item
            for item in list(self.reading_blocks or [])
            if item.segment_kind in {"heading", "paragraph", "authored_explanation"}
        ]
        if not original_segments:
            raise ValueError("page_artifact_v2 requires original_excerpt blocks")
        if not authored_segments:
            raise ValueError("page_artifact_v2 requires authored narrative blocks")

        original_ids = {str(item.segment_id) for item in original_segments}
        spine_ids = {str(item).strip() for item in list(self.current_page_spine.main_segment_ids or []) if str(item).strip()}
        if not spine_ids or not (spine_ids & original_ids):
            raise ValueError("current_page_spine.main_segment_ids must point to original_excerpt blocks")
        if any(segment_id not in original_ids for segment_id in spine_ids):
            raise ValueError("current_page_spine.main_segment_ids cannot include non-original blocks")

        adjacent_pages_meta = dict(self.provenance.source_lanes or {}).get("adjacent_pages_meta")
        if self.provenance.adjacent_context_pages and not isinstance(adjacent_pages_meta, dict):
            raise ValueError("page_artifact_v2 provenance must preserve ordered adjacent_pages context")
        return self


class ExperienceSessionV2RuntimeBudget(BaseModel):
    max_iterations: int = Field(default=6, ge=1, le=32)
    max_tool_rounds: int = Field(default=8, ge=1, le=64)


class ExperienceSessionV2NarrativeBrief(BaseModel):
    version: Literal["experience_session_narrative_brief_v2"] = "experience_session_narrative_brief_v2"
    focus_page: int = Field(default=1, ge=1)
    current_page_main_arc: Union[str, Dict[str, Any]] = ""
    continuity_resolutions: Union[str, List[Any], Dict[str, Any]] = Field(default_factory=list)
    required_media_refs: List[Dict[str, Any]] = Field(default_factory=list)
    opening_key_points: List[str] = Field(default_factory=list)
    previous_page_bridge: Dict[str, Any] = Field(default_factory=dict)
    next_page_bridge: Dict[str, Any] = Field(default_factory=dict)
    reader_attention_order: List[str] = Field(default_factory=list)
    must_surface_nodes: List[str] = Field(default_factory=list)
    suppressed_threads: List[str] = Field(default_factory=list)
    content_strategy: Union[str, Dict[str, Any]] = ""
    presentation_strategy: Union[str, Dict[str, Any]] = ""
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_strategy_shape(cls, value: Any):
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        nested_strategy_roots = [
            payload.get("reading_strategy"),
            payload.get("strategy"),
            payload.get("narrative_strategy"),
            payload.get("brief"),
        ]

        field_aliases = {
            "current_page_main_arc": (
                "current_page_main_arc",
                "main_arc",
                "page_arc",
                "page_main_arc",
                "primary_claim",
                "page_role",
            ),
            "continuity_resolutions": (
                "continuity_resolutions",
                "continuity",
                "continuity_notes",
                "continuity_plan",
                "continuity_bridges",
                "bridges",
            ),
            "required_media_refs": (
                "required_media_refs",
                "required_media",
                "media_refs",
                "media_plan",
                "must_surface_media",
                "visual_evidence",
            ),
            "opening_key_points": (
                "opening_key_points",
                "opening_points",
                "opening_takeaways",
                "page_opening_points",
                "lead_points",
                "lead_takeaways",
                "opening_bullets",
                "reader_opening_points",
            ),
            "previous_page_bridge": (
                "previous_page_bridge",
                "from_previous_page",
                "previous_page_context",
                "previous_bridge",
                "bridge_from_previous_page",
            ),
            "next_page_bridge": (
                "next_page_bridge",
                "to_next_page",
                "next_page_context",
                "next_bridge",
                "bridge_to_next_page",
            ),
            "content_strategy": (
                "content_strategy",
                "content_plan",
                "reading_plan",
                "teaching_strategy",
                "drafting_strategy",
                "composition_strategy",
            ),
            "presentation_strategy": (
                "presentation_strategy",
                "presentation",
                "presentation_plan",
                "layout_strategy",
                "surface_strategy",
                "display_strategy",
            ),
            "reader_attention_order": (
                "reader_attention_order",
                "attention_order",
                "reading_order",
                "reader_steps",
            ),
            "must_surface_nodes": (
                "must_surface_nodes",
                "must_surface",
                "surface_nodes",
                "must_keep_nodes",
                "evidence_anchors",
            ),
            "suppressed_threads": (
                "suppressed_threads",
                "secondary_threads",
                "deprioritized_threads",
                "omit_threads",
            ),
        }

        for canonical_key, aliases in field_aliases.items():
            current_value = payload.get(canonical_key)
            if current_value not in (None, "", [], {}):
                continue
            resolved = None
            for alias in aliases:
                alias_value = payload.get(alias)
                if alias_value not in (None, "", [], {}):
                    resolved = alias_value
                    break
            if resolved in (None, "", [], {}):
                for nested in nested_strategy_roots:
                    if not isinstance(nested, Mapping):
                        continue
                    for alias in aliases:
                        alias_value = nested.get(alias)
                        if alias_value not in (None, "", [], {}):
                            resolved = alias_value
                            break
                    if resolved not in (None, "", [], {}):
                        break
            if resolved not in (None, "", [], {}):
                payload[canonical_key] = resolved

        continuity_payload = payload.get("continuity_resolutions")
        if isinstance(continuity_payload, Mapping):
            if payload.get("previous_page_bridge") in (None, "", [], {}):
                previous_payload = continuity_payload.get("from_previous_page") or continuity_payload.get("previous_page")
                if previous_payload not in (None, "", [], {}):
                    payload["previous_page_bridge"] = previous_payload
            if payload.get("next_page_bridge") in (None, "", [], {}):
                next_payload = continuity_payload.get("to_next_page") or continuity_payload.get("next_page")
                if next_payload not in (None, "", [], {}):
                    payload["next_page_bridge"] = next_payload

        raw_media_value = payload.get("required_media_refs")
        if isinstance(raw_media_value, Mapping):
            raw_media_refs = [dict(raw_media_value)]
        elif isinstance(raw_media_value, (str, bytes, bytearray)):
            raw_media_refs = [str(raw_media_value)]
        else:
            raw_media_refs = list(raw_media_value or [])
        normalized_media_refs: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_media_refs, start=1):
            if isinstance(item, Mapping):
                normalized = dict(item)
                label = str(
                    normalized.get("label")
                    or normalized.get("title")
                    or normalized.get("name")
                    or normalized.get("description")
                    or normalized.get("text")
                    or ""
                ).strip()
                media_type = str(
                    normalized.get("type")
                    or normalized.get("media_type")
                    or normalized.get("kind")
                    or ""
                ).strip()
                if not label:
                    page = str(normalized.get("page") or "").strip()
                    ref = str(
                        normalized.get("ref")
                        or normalized.get("layout_id")
                        or normalized.get("layout_ref")
                        or normalized.get("url")
                        or ""
                    ).strip()
                    synthesized = " ".join(part for part in [media_type, page] if part).strip()
                    label = synthesized or ref
                if label and not str(normalized.get("label") or "").strip():
                    normalized["label"] = label
                if media_type and not str(normalized.get("type") or "").strip():
                    normalized["type"] = media_type
                normalized_media_refs.append(normalized)
                continue
            text = str(item or "").strip()
            if not text:
                continue
            normalized_media_refs.append(
                {
                    "type": "media_ref",
                    "label": text,
                    "description": text,
                    "meta": {
                        "normalized_from": "string",
                        "position": int(index),
                    },
                }
            )
        payload["required_media_refs"] = normalized_media_refs

        opening_points = payload.get("opening_key_points")
        if isinstance(opening_points, str):
            payload["opening_key_points"] = [opening_points]
        elif not isinstance(opening_points, Sequence) or isinstance(opening_points, (bytes, bytearray)):
            payload["opening_key_points"] = []

        for bridge_key in ("previous_page_bridge", "next_page_bridge"):
            bridge_value = payload.get(bridge_key)
            if isinstance(bridge_value, str):
                payload[bridge_key] = {"bridge_text": bridge_value}
            elif not isinstance(bridge_value, Mapping):
                payload[bridge_key] = {}
        return payload

    @staticmethod
    def _has_strategy_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return any(ExperienceSessionV2NarrativeBrief._has_strategy_value(item) for item in value.values())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(ExperienceSessionV2NarrativeBrief._has_strategy_value(item) for item in value)
        return value is not None and bool(str(value).strip())

    @model_validator(mode="after")
    def _validate_brief(self):
        if not self._has_strategy_value(self.current_page_main_arc):
            raise ValueError("narrative brief requires current_page_main_arc")
        if not self._has_strategy_value(self.continuity_resolutions):
            raise ValueError("narrative brief requires continuity_resolutions")
        normalized_media_refs = []
        for item in list(self.required_media_refs or []):
            payload = dict(item)
            if not self._has_strategy_value(payload):
                continue
            label = str(payload.get("label") or payload.get("description") or payload.get("text") or "").strip()
            if not label:
                raise ValueError("narrative brief required_media_refs entries must include label-like content")
            payload.setdefault("label", label)
            payload.setdefault("type", str(payload.get("type") or "media_ref").strip() or "media_ref")
            normalized_media_refs.append(payload)
        self.required_media_refs = normalized_media_refs
        self.opening_key_points = [str(item).strip() for item in list(self.opening_key_points or []) if str(item).strip()][:6]
        if not self._has_strategy_value(self.content_strategy):
            raise ValueError("narrative brief requires content_strategy")
        if not self._has_strategy_value(self.presentation_strategy):
            raise ValueError("narrative brief requires presentation_strategy")
        return self


class ExperienceSessionV2ContextCarry(BaseModel):
    mode: Literal["full_dossier_bootstrap", "delta_state_handle"] = "full_dossier_bootstrap"
    full_dossier: Optional[ReadingDossierV2] = None
    delta_packet: Dict[str, Any] = Field(default_factory=dict)
    state_handle: str = ""

    @model_validator(mode="after")
    def _validate_context_carry(self):
        if self.mode == "full_dossier_bootstrap" and self.full_dossier is None:
            self.full_dossier = ReadingDossierV2()
        if self.mode == "delta_state_handle" and not str(self.state_handle or "").strip():
            raise ValueError("delta_state_handle mode requires state_handle")
        return self


class ExperienceSessionV2ToolTraceEntry(BaseModel):
    round_index: int = Field(default=1, ge=1)
    tool_name: str = ""
    success: bool = True
    latency_ms: Optional[int] = Field(default=None, ge=0)
    note: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ExperienceSessionV2Iteration(BaseModel):
    iteration_index: int = Field(default=1, ge=1)
    phase: Literal["bootstrap", "revise", "resume"] = "bootstrap"
    context_carry: ExperienceSessionV2ContextCarry = Field(default_factory=ExperienceSessionV2ContextCarry)
    narrative_brief: Optional[ExperienceSessionV2NarrativeBrief] = None
    tool_trace: List[ExperienceSessionV2ToolTraceEntry] = Field(default_factory=list)
    stop_reason: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_phase_context_contract(self):
        if self.phase == "bootstrap":
            if self.context_carry.mode != "full_dossier_bootstrap" or self.context_carry.full_dossier is None:
                raise ValueError("bootstrap iteration requires full_dossier_bootstrap context")
            if self.narrative_brief is None:
                raise ValueError("bootstrap iteration requires narrative_brief")
            return self
        if self.context_carry.mode != "delta_state_handle":
            raise ValueError("revise/resume iteration requires delta_state_handle context")
        if not str(self.context_carry.state_handle or "").strip():
            raise ValueError("revise/resume iteration requires non-empty state_handle")
        return self


class ExperienceSessionV2ResumeMeta(BaseModel):
    preferred_strategy: Literal["resume", "restart"] = "resume"
    resumable: bool = True
    resume_state_handle: str = ""
    resume_token: str = ""
    last_failed_iteration: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ExperienceSessionV2ArtifactPromotionMeta(BaseModel):
    promotion_ready: bool = False
    completed_artifact_exists: bool = False
    no_second_full_generation_pass: bool = True
    artifact_ref: str = ""
    promoted_fields: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ExperienceSessionV2(BaseModel):
    version: Literal["experience_session_v2"] = "experience_session_v2"
    status: Literal["running", "failed", "completed"] = "running"
    stop_reason: str = ""
    session_id: str
    cache_namespace: str = "lit:experience_session:v2"
    plan_kind: str = "experience_session_v2"
    cache_key: str = ""
    focus_page: int = Field(default=1, ge=1)
    reader_profile: str = "curious_generalist"
    dossier_signature: str = ""
    runtime_budget: ExperienceSessionV2RuntimeBudget = Field(default_factory=ExperienceSessionV2RuntimeBudget)
    iterations: List[ExperienceSessionV2Iteration] = Field(default_factory=list)
    resume: ExperienceSessionV2ResumeMeta = Field(default_factory=ExperienceSessionV2ResumeMeta)
    artifact_promotion: ExperienceSessionV2ArtifactPromotionMeta = Field(default_factory=ExperienceSessionV2ArtifactPromotionMeta)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_failed_stop_reason(self):
        if self.status == "failed" and not str(self.stop_reason or "").strip():
            raise ValueError("failed status requires stop_reason")
        return self


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
    page_grounding_v1: ReaderPageGrounding = Field(default_factory=ReaderPageGrounding)
    enrichment_bundle: ReaderEnrichmentBundle = Field(default_factory=ReaderEnrichmentBundle)
    generative_reader_plan: ReaderGenerativePlan = Field(default_factory=ReaderGenerativePlan)
    generated_at: datetime
    cache_hit: bool = False
    cache_layer: Optional[Literal["redis", "db", "none"]] = None
    overlay_applied: bool = False
    overlay_count: int = 0


class ReaderComposeFetchResponse(BaseModel):
    payload: ReaderComposePayload
    cache_meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderGenerativePlanResponse(BaseModel):
    page: int = Field(..., ge=1)
    plan: ReaderGenerativePlan = Field(default_factory=ReaderGenerativePlan)
    enrichment_bundle: ReaderEnrichmentBundle = Field(default_factory=ReaderEnrichmentBundle)
    scheme_choice: ReaderComposeSchemeChoice = Field(default_factory=ReaderComposeSchemeChoice)
    compose_status: Literal["done", "fallback"] = "done"
    compose_build_mode: str = ""
    compose_source_signature: str = ""
    source_sig_hash: str = ""
    cache_hit: bool = False
    cache_layer: str = "none"
    plan_cache_hit: bool = False
    plan_cache_layer: str = "none"
    adjacent_page_context: List[ReaderAdjacentPageContext] = Field(default_factory=list)
    page_dossier: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperiencePlanRequest(ReaderGenerativePlanRequest):
    focus_page: Optional[int] = Field(default=None, ge=1)
    focus_section_ids: List[str] = Field(default_factory=list, max_length=12)
    reader_profile: str = "curious_generalist"


class ReaderExperiencePlanResponse(BaseModel):
    focus_page: int = Field(..., ge=1)
    plan: ReaderExperiencePlan = Field(default_factory=ReaderExperiencePlan)
    generative_plan: ReaderGenerativePlan = Field(default_factory=ReaderGenerativePlan)
    compose_payload: Dict[str, Any] = Field(default_factory=dict)
    enrichment_bundle: ReaderEnrichmentBundle = Field(default_factory=ReaderEnrichmentBundle)
    compose_status: Literal["done", "fallback"] = "done"
    compose_build_mode: str = ""
    compose_source_signature: str = ""
    source_sig_hash: str = ""
    cache_hit: bool = False
    cache_layer: str = "none"
    generative_plan_cache_hit: bool = False
    generative_plan_cache_layer: str = "none"
    experience_cache_hit: bool = False
    experience_cache_layer: str = "none"
    adjacent_page_context: List[ReaderAdjacentPageContext] = Field(default_factory=list)
    page_dossier: Dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceV2Response(BaseModel):
    focus_page: int = Field(..., ge=1)
    status: Literal["ready", "generating", "failed"] = "generating"
    artifact: Optional[PageArtifactV2] = None
    compose_payload: Dict[str, Any] = Field(default_factory=dict)
    compose_status: Literal["done", "fallback"] = "done"
    compose_build_mode: str = ""
    compose_source_signature: str = ""
    source_sig_hash: str = ""
    artifact_cache_hit: bool = False
    artifact_cache_layer: str = "none"
    session_cache_hit: bool = False
    session_cache_layer: str = "none"
    session_id: str = ""
    session_status: str = ""
    failure_detail: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderWorkbenchV2Response(BaseModel):
    focus_page: int = Field(..., ge=1)
    status: Literal["ready", "running", "failed", "empty"] = "empty"
    compose_payload: Dict[str, Any] = Field(default_factory=dict)
    compose_status: Literal["done", "fallback"] = "done"
    compose_build_mode: str = ""
    compose_source_signature: str = ""
    source_sig_hash: str = ""
    reading_dossier: Optional[ReadingDossierV2] = None
    session: Optional[ExperienceSessionV2] = None
    artifact: Optional[PageArtifactV2] = None
    artifact_validation: Dict[str, Any] = Field(default_factory=dict)
    artifact_cache_hit: bool = False
    artifact_cache_layer: str = "none"
    session_cache_hit: bool = False
    session_cache_layer: str = "none"
    failure_detail: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class ReaderComposePrefetchRequest(BaseModel):
    pages: List[int] = Field(default_factory=list, max_length=16)
    selected_kb_id: Optional[int] = None
    pipeline_version: Optional[str] = None
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
    enrichment_bundle: ReaderEnrichmentBundle = Field(default_factory=ReaderEnrichmentBundle)
    generative_reader_plan: ReaderGenerativePlan = Field(default_factory=ReaderGenerativePlan)
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


class ReaderExperienceBlockExplainTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class ReaderExperienceBlockExplainRequest(BaseModel):
    page: int = Field(..., ge=1)
    block_id: str = Field(..., min_length=1, max_length=128)
    explain_kind: Literal["simplify", "figure"]
    question: str = Field(..., min_length=1, max_length=2000)
    source_excerpt: Optional[str] = Field(None, max_length=6000)
    source_translation_zh: Optional[str] = Field(None, max_length=6000)
    explanation_text: Optional[str] = Field(None, max_length=6000)
    figure_label: Optional[str] = Field(None, max_length=512)
    figure_caption: Optional[str] = Field(None, max_length=6000)
    figure_text: Optional[str] = Field(None, max_length=6000)
    figure_image_url: Optional[str] = Field(None, max_length=2000000)
    history: List[ReaderExperienceBlockExplainTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_local_context(self):
        if self.explain_kind == "simplify":
            has_excerpt = bool(str(self.source_excerpt or "").strip())
            has_explanation = bool(str(self.explanation_text or "").strip())
            if not (has_excerpt or has_explanation):
                raise ValueError("block_explain simplify requests require source_excerpt or explanation_text")
        if self.explain_kind == "figure":
            has_figure_context = any(
                bool(str(value or "").strip())
                for value in (self.figure_label, self.figure_caption, self.figure_text, self.figure_image_url)
            )
            if not has_figure_context:
                raise ValueError("block_explain figure requests require figure context")
        if len(self.history) > 12:
            self.history = list(self.history[-12:])
        return self


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
