from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ResearchProjectPaperSummary(BaseModel):
    id: int
    title: str
    year: Optional[int] = None
    venue: Optional[str] = None
    arxiv_id: Optional[str] = None
    role: str = "related"
    notes: Optional[str] = None


class ResearchProjectWorkspaceSummary(BaseModel):
    id: int
    paper_id: Optional[int] = None
    paper_title: Optional[str] = None
    notebook_id: Optional[str] = None
    title: str
    status: str
    role: str = "related_reproduction"
    run_count: int = 0
    latest_run_status: Optional[str] = None
    latest_run_at: Optional[datetime] = None


class ResearchProjectExecutionSummary(BaseModel):
    execution_id: str
    label: Optional[str] = None
    draft_id: Optional[str] = None
    runtime_type: Optional[str] = None
    status: str
    success: Optional[bool] = None
    error: Optional[str] = None
    message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    spec_relative_path: Optional[str] = None
    result_relative_path: Optional[str] = None
    log_relative_path: Optional[str] = None
    result_exists: bool = False
    log_exists: bool = False
    log_total_chars: int = 0
    log_truncated: bool = False
    log_tail: Optional[str] = None
    last_log_line: Optional[str] = None
    latest_elapsed_sec: Optional[float] = None
    latest_loss: Optional[float] = None
    command_preview: Optional[str] = None


class ResearchProjectArtifactSummary(BaseModel):
    label: str
    relative_path: str
    kind: str = "artifact"
    present: bool = False
    updated_at: Optional[str] = None


class ResearchProjectStageSummary(BaseModel):
    stage: str
    label: str
    status: str
    summary: Optional[str] = None
    blockers: List[str] = Field(default_factory=list)
    artifacts: List[ResearchProjectArtifactSummary] = Field(default_factory=list)
    updated_at: Optional[str] = None


class ResearchProjectRuntimeToolSummary(BaseModel):
    tool_key: str
    available: bool = False
    command: Optional[str] = None


class ResearchProjectRuntimeCandidateSummary(BaseModel):
    runtime_type: str
    status: str
    priority: int = 0
    reason: Optional[str] = None
    entrypoints: List[str] = Field(default_factory=list)
    evidence_files: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    requires_runtime_worker: bool = False
    requires_explicit_user_confirm: bool = False


class ResearchProjectRuntimeContextSummary(BaseModel):
    execution_mode: Optional[str] = None
    notebook_id: Optional[str] = None
    notebook_asset_relative_path: Optional[str] = None
    repo_available: bool = False
    repo_root_relative_path: Optional[str] = None
    repo_file_count: int = 0
    repo_reference_url: Optional[str] = None
    repo_history_candidate_count: int = 0
    entrypoint_hints: List[str] = Field(default_factory=list)
    runtime_candidates: List[ResearchProjectRuntimeCandidateSummary] = Field(default_factory=list)
    tools: List[ResearchProjectRuntimeToolSummary] = Field(default_factory=list)
    runtime_worker_enabled: bool = False
    runtime_worker_available: bool = False


class ResearchProjectResultSummary(BaseModel):
    baseline_status: str = "missing"
    baseline_execution_id: Optional[str] = None
    baseline_completed_at: Optional[str] = None
    baseline_metrics: Dict[str, Any] = Field(default_factory=dict)
    tuning_status: str = "missing"
    tuning_execution_id: Optional[str] = None
    tuning_completed_at: Optional[str] = None
    tuning_metrics: Dict[str, Any] = Field(default_factory=dict)
    compare_status: str = "missing"
    compare_summary: Optional[str] = None
    highlights: List[str] = Field(default_factory=list)


class ResearchProjectWorkspaceRuntimeOverview(BaseModel):
    workspace_id: int
    paper_id: Optional[int] = None
    paper_title: Optional[str] = None
    notebook_id: Optional[str] = None
    title: str
    status: str
    role: str = "related_reproduction"
    run_count: int = 0
    latest_run_status: Optional[str] = None
    latest_run_at: Optional[datetime] = None
    current_stage: str = "planning"
    current_status: str = "draft"
    stage_ledger: List[ResearchProjectStageSummary] = Field(default_factory=list)
    runtime_context: ResearchProjectRuntimeContextSummary = Field(default_factory=ResearchProjectRuntimeContextSummary)
    results: ResearchProjectResultSummary = Field(default_factory=ResearchProjectResultSummary)
    execution_count: int = 0
    running_execution_count: int = 0
    recent_executions: List[ResearchProjectExecutionSummary] = Field(default_factory=list)


class ResearchProjectRuntimeOverviewResponse(BaseModel):
    project_id: int
    current_stage: str = "planning"
    current_status: str = "draft"
    recommended_chat_stage: Optional[str] = None
    continue_reason: Optional[str] = None
    primary_workspace_id: Optional[int] = None
    workspace_count: int = 0
    execution_count: int = 0
    running_execution_count: int = 0
    workspaces: List[ResearchProjectWorkspaceRuntimeOverview] = Field(default_factory=list)


class ResearchProjectCreateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    goal: Optional[str] = None
    status: Literal["draft", "active", "archived"] = "draft"
    paper_ids: List[int] = Field(default_factory=list)


class ResearchProjectResponse(BaseModel):
    id: int
    user_id: int
    primary_paper_id: Optional[int] = None
    primary_workspace_id: Optional[int] = None
    title: str
    goal: Optional[str] = None
    status: str
    summary: Dict[str, Any] = Field(default_factory=dict)
    paper_count: int = 0
    workspace_count: int = 0
    primary_paper: Optional[ResearchProjectPaperSummary] = None
    primary_workspace: Optional[ResearchProjectWorkspaceSummary] = None
    papers: List[ResearchProjectPaperSummary] = Field(default_factory=list)
    workspaces: List[ResearchProjectWorkspaceSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
