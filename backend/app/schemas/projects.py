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
    project_root: Optional[str] = None
    project_root_exists: bool = False
    created_at: datetime
    updated_at: datetime


class ResearchProjectFolderTreeResponse(BaseModel):
    project_id: int
    project_root: str
    exists: bool = False
    tree: str = "."
