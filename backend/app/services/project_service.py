from __future__ import annotations

import ast
from collections import defaultdict
from datetime import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.literature import (
    Paper,
    PaperExperimentRun,
    PaperExperimentWorkspace,
    ResearchProject,
    research_project_papers_association,
    research_project_workspaces_association,
)
from app.services.notebook_workspace_service import get_notebook_workspace_dir
from app.services.project_runtime_service import ProjectRuntimeService


_TRAIN_PROGRESS_RE = re.compile(r"time\s+([0-9.]+)s\s+\|\s+loss\s+([0-9.]+)", flags=re.IGNORECASE)
_METRIC_DICT_RE = re.compile(r"\{[^{}]+\}")
_RESULT_STAGE_LABELS = {
    "planning": "Planning / Intake",
    "implementation_prep": "Implementation Prep",
    "run_drafts": "Run Drafts",
    "execution": "Execution",
    "results": "Results",
}


class ProjectService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_projects(self, *, user_id: int, paper_id: Optional[int] = None) -> List[ResearchProject]:
        stmt = (
            select(ResearchProject)
            .where(ResearchProject.user_id == int(user_id))
            .options(
                selectinload(ResearchProject.primary_paper),
                selectinload(ResearchProject.primary_workspace),
            )
            .order_by(ResearchProject.updated_at.desc(), ResearchProject.id.desc())
        )
        if paper_id is not None:
            stmt = stmt.join(
                research_project_papers_association,
                research_project_papers_association.c.project_id == ResearchProject.id,
            ).where(research_project_papers_association.c.paper_id == int(paper_id))
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_project(self, *, project_id: int, user_id: int) -> Optional[ResearchProject]:
        stmt = (
            select(ResearchProject)
            .where(
                ResearchProject.id == int(project_id),
                ResearchProject.user_id == int(user_id),
            )
            .options(
                selectinload(ResearchProject.primary_paper),
                selectinload(ResearchProject.primary_workspace),
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_project_payloads(self, *, user_id: int, paper_id: Optional[int] = None) -> List[Dict[str, Any]]:
        projects = await self.list_projects(user_id=user_id, paper_id=paper_id)
        return await self.serialize_projects(projects)

    async def get_project_payload(self, *, project_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        project = await self.get_project(project_id=project_id, user_id=user_id)
        if project is None:
            return None
        payloads = await self.serialize_projects([project])
        return payloads[0] if payloads else None

    async def get_project_runtime_overview(
        self,
        *,
        project_id: int,
        user_id: int,
        recent_execution_limit: int = 5,
        max_log_chars: int = 4000,
    ) -> Optional[Dict[str, Any]]:
        project = await self.get_project(project_id=project_id, user_id=user_id)
        if project is None:
            return None

        workspaces_map = await self._load_project_workspaces([int(project.id)])
        workspace_rows = list(workspaces_map.get(int(project.id), []) or [])
        runtime_service = ProjectRuntimeService()
        overviews = []
        total_execution_count = 0
        total_running_count = 0

        for workspace in workspace_rows:
            notebook_id = str(workspace.get("notebook_id") or "").strip()
            workspace_dir = Path(get_notebook_workspace_dir(notebook_id, int(user_id))) if notebook_id else None
            execution_summaries = self._load_workspace_execution_summaries(workspace_dir=workspace_dir)
            recent_executions = await self._load_recent_workspace_executions(
                project_id=int(project.id),
                workspace_dir=workspace_dir,
                limit=recent_execution_limit,
                max_log_chars=max_log_chars,
            )
            runtime_context = await self._build_runtime_context(
                runtime_service=runtime_service,
                workspace=workspace,
                workspace_dir=workspace_dir,
                project_id=int(project.id),
            )
            results = self._build_result_summary(
                workspace=workspace,
                executions=execution_summaries,
            )
            stage_ledger = self._build_stage_ledger(
                workspace=workspace,
                workspace_dir=workspace_dir,
                recent_executions=recent_executions,
                results=results,
            )
            current_stage, current_status = self._derive_workspace_state(
                stage_ledger=stage_ledger,
                results=results,
                executions=execution_summaries,
            )
            running_count = sum(1 for item in execution_summaries if str(item.get("status") or "").strip().lower() in {"pending", "running"})
            total_execution_count += len(execution_summaries)
            total_running_count += running_count
            overviews.append(
                {
                    "workspace_id": int(workspace["id"]),
                    "paper_id": workspace.get("paper_id"),
                    "paper_title": workspace.get("paper_title"),
                    "notebook_id": workspace.get("notebook_id"),
                    "title": str(workspace.get("title") or ""),
                    "status": str(workspace.get("status") or "draft"),
                    "role": str(workspace.get("role") or "related_reproduction"),
                    "run_count": int(workspace.get("run_count") or 0),
                    "latest_run_status": workspace.get("latest_run_status"),
                    "latest_run_at": workspace.get("latest_run_at"),
                    "current_stage": current_stage,
                    "current_status": current_status,
                    "stage_ledger": stage_ledger,
                    "runtime_context": runtime_context,
                    "results": results,
                    "execution_count": len(execution_summaries),
                    "running_execution_count": running_count,
                    "recent_executions": recent_executions,
                }
            )

        current_stage, current_status, continue_reason = self._derive_project_state(
            project=project,
            workspace_overviews=overviews,
        )

        return {
            "project_id": int(project.id),
            "current_stage": current_stage,
            "current_status": current_status,
            "recommended_chat_stage": self._recommended_chat_stage(current_stage=current_stage, current_status=current_status),
            "continue_reason": continue_reason,
            "primary_workspace_id": int(project.primary_workspace_id) if getattr(project, "primary_workspace_id", None) is not None else None,
            "workspace_count": len(overviews),
            "execution_count": total_execution_count,
            "running_execution_count": total_running_count,
            "workspaces": overviews,
        }

    async def cancel_project_execution(
        self,
        *,
        project_id: int,
        user_id: int,
        workspace_id: int,
        execution_id: str,
    ) -> Optional[Dict[str, Any]]:
        project = await self.get_project(project_id=project_id, user_id=user_id)
        if project is None:
            return None
        workspaces_map = await self._load_project_workspaces([int(project.id)])
        workspace = next(
            (
                item
                for item in list(workspaces_map.get(int(project.id), []) or [])
                if int(item.get("id") or 0) == int(workspace_id)
            ),
            None,
        )
        if workspace is None:
            return None
        notebook_id = str(workspace.get("notebook_id") or "").strip()
        if not notebook_id:
            return None
        workspace_dir = Path(get_notebook_workspace_dir(notebook_id, int(user_id)))
        if not workspace_dir.is_dir():
            return None
        runtime_service = ProjectRuntimeService()
        return await runtime_service.cancel_execution(
            project_id=int(project.id),
            execution_id=str(execution_id or ""),
            workspace_dir=workspace_dir,
        )

    async def serialize_projects(self, projects: Iterable[ResearchProject]) -> List[Dict[str, Any]]:
        ordered_projects = [item for item in list(projects or []) if item is not None]
        if not ordered_projects:
            return []

        project_ids = [int(item.id) for item in ordered_projects if getattr(item, "id", None) is not None]
        papers_map = await self._load_project_papers(project_ids)
        workspaces_map = await self._load_project_workspaces(project_ids)
        return [
            self._serialize_project(
                project,
                papers=list(papers_map.get(int(project.id), []) or []),
                workspaces=list(workspaces_map.get(int(project.id), []) or []),
            )
            for project in ordered_projects
        ]

    async def create_project(
        self,
        *,
        user_id: int,
        title: Optional[str],
        goal: Optional[str],
        status: str,
        paper_ids: List[int],
    ) -> ResearchProject:
        normalized_paper_ids = self._normalize_paper_ids(paper_ids)
        papers = await self._load_owned_papers(user_id=int(user_id), paper_ids=normalized_paper_ids)
        primary_paper = self._resolve_primary_paper(paper_ids=normalized_paper_ids, papers=papers)

        resolved_title = str(title or "").strip()
        if not resolved_title:
            if primary_paper is not None:
                resolved_title = f"{str(primary_paper.title or '').strip()[:120]} - Research Project"
            else:
                resolved_title = f"Research Project {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

        project = ResearchProject(
            user_id=int(user_id),
            title=resolved_title[:300],
            goal=str(goal or "").strip() or None,
            status=str(status or "draft").strip() or "draft",
            primary_paper_id=int(primary_paper.id) if primary_paper is not None else None,
            summary_json={
                "entry_mode": "paper_seed" if primary_paper is not None else "manual",
                "paper_count": len(papers),
                "workspace_count": 0,
            },
        )
        self.db.add(project)
        await self.db.flush()

        if papers:
            await self.db.execute(
                insert(research_project_papers_association),
                [
                    {
                        "project_id": int(project.id),
                        "paper_id": int(paper.id),
                        "role": "primary" if primary_paper is not None and int(paper.id) == int(primary_paper.id) else "related",
                        "notes": None,
                    }
                    for paper in papers
                ],
            )

        await self.db.commit()
        await self.db.refresh(project)
        return await self.get_project(project_id=int(project.id), user_id=int(user_id)) or project

    async def link_workspace(
        self,
        *,
        project_id: int,
        user_id: int,
        workspace_id: int,
        paper_id: Optional[int],
        role: str = "primary_reproduction",
    ) -> Optional[ResearchProject]:
        project = await self.get_project(project_id=int(project_id), user_id=int(user_id))
        if project is None:
            return None

        workspace_result = await self.db.execute(
            select(PaperExperimentWorkspace).where(
                PaperExperimentWorkspace.id == int(workspace_id),
                PaperExperimentWorkspace.user_id == int(user_id),
            )
        )
        workspace = workspace_result.scalar_one_or_none()
        if workspace is None:
            return None

        existing = await self.db.execute(
            select(research_project_workspaces_association.c.workspace_id).where(
                research_project_workspaces_association.c.project_id == int(project_id),
                research_project_workspaces_association.c.workspace_id == int(workspace_id),
            )
        )
        if existing.scalar_one_or_none() is None:
            await self.db.execute(
                insert(research_project_workspaces_association).values(
                    project_id=int(project_id),
                    workspace_id=int(workspace_id),
                    paper_id=int(paper_id) if paper_id is not None else workspace.paper_id,
                    role=str(role or "related_reproduction"),
                )
            )

        if project.primary_workspace_id is None:
            project.primary_workspace_id = int(workspace_id)
        project.summary_json = {
            **dict(project.summary_json or {}),
            "workspace_count": max(int(dict(project.summary_json or {}).get("workspace_count") or 0), 1),
            "primary_workspace_status": str(workspace.status or "ready"),
        }
        project.updated_at = datetime.utcnow()
        await self.db.commit()
        await self.db.refresh(project)
        return await self.get_project(project_id=int(project.id), user_id=int(user_id)) or project

    def _normalize_paper_ids(self, paper_ids: Iterable[int]) -> List[int]:
        normalized: List[int] = []
        seen = set()
        for item in list(paper_ids or []):
            try:
                paper_id = int(item)
            except (TypeError, ValueError):
                continue
            if paper_id <= 0 or paper_id in seen:
                continue
            seen.add(paper_id)
            normalized.append(paper_id)
        return normalized

    async def _load_owned_papers(self, *, user_id: int, paper_ids: List[int]) -> List[Paper]:
        if not paper_ids:
            return []
        result = await self.db.execute(
            select(Paper)
            .where(
                Paper.user_id == int(user_id),
                Paper.id.in_(paper_ids),
            )
        )
        return list(result.scalars().all())

    def _resolve_primary_paper(self, *, paper_ids: List[int], papers: List[Paper]) -> Optional[Paper]:
        if not papers:
            return None
        paper_map = {int(item.id): item for item in papers if getattr(item, "id", None) is not None}
        for item in list(paper_ids or []):
            matched = paper_map.get(int(item))
            if matched is not None:
                return matched
        return papers[0]

    async def _load_project_papers(self, project_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not project_ids:
            return {}
        result = await self.db.execute(
            select(
                research_project_papers_association.c.project_id.label("project_id"),
                research_project_papers_association.c.paper_id.label("paper_id"),
                research_project_papers_association.c.role.label("role"),
                research_project_papers_association.c.notes.label("notes"),
                Paper.title.label("title"),
                Paper.year.label("year"),
                Paper.venue.label("venue"),
                Paper.arxiv_id.label("arxiv_id"),
            )
            .join(Paper, Paper.id == research_project_papers_association.c.paper_id)
            .where(research_project_papers_association.c.project_id.in_(project_ids))
        )
        grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in result.mappings().all():
            grouped[int(row["project_id"])].append(
                {
                    "id": int(row["paper_id"]),
                    "title": str(row["title"] or ""),
                    "year": row["year"],
                    "venue": row["venue"],
                    "arxiv_id": row["arxiv_id"],
                    "role": str(row["role"] or "related"),
                    "notes": row["notes"],
                }
            )

        for project_id, rows in grouped.items():
            grouped[project_id] = sorted(
                rows,
                key=lambda item: (0 if str(item.get("role") or "") == "primary" else 1, -int(item.get("id") or 0)),
            )
        return grouped

    async def _load_project_workspaces(self, project_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not project_ids:
            return {}

        workspace_rows = (
            await self.db.execute(
                select(
                    research_project_workspaces_association.c.project_id.label("project_id"),
                    research_project_workspaces_association.c.workspace_id.label("workspace_id"),
                    research_project_workspaces_association.c.paper_id.label("paper_id"),
                    research_project_workspaces_association.c.role.label("role"),
                    PaperExperimentWorkspace.notebook_id.label("notebook_id"),
                    PaperExperimentWorkspace.title.label("title"),
                    PaperExperimentWorkspace.status.label("status"),
                    PaperExperimentWorkspace.summary_json.label("summary_json"),
                    PaperExperimentWorkspace.experiment_spec_json.label("experiment_spec_json"),
                    PaperExperimentWorkspace.compare_report_json.label("compare_report_json"),
                    PaperExperimentWorkspace.updated_at.label("updated_at"),
                    Paper.title.label("paper_title"),
                )
                .join(
                    PaperExperimentWorkspace,
                    PaperExperimentWorkspace.id == research_project_workspaces_association.c.workspace_id,
                )
                .outerjoin(Paper, Paper.id == research_project_workspaces_association.c.paper_id)
                .where(research_project_workspaces_association.c.project_id.in_(project_ids))
            )
        ).mappings().all()

        workspace_ids = [
            int(row["workspace_id"])
            for row in workspace_rows
            if row.get("workspace_id") is not None
        ]
        run_counts: Dict[int, int] = {}
        latest_runs: Dict[int, Dict[str, Any]] = {}

        if workspace_ids:
            count_rows = (
                await self.db.execute(
                    select(
                        PaperExperimentRun.workspace_id.label("workspace_id"),
                        func.count(PaperExperimentRun.id).label("run_count"),
                    )
                    .where(PaperExperimentRun.workspace_id.in_(workspace_ids))
                    .group_by(PaperExperimentRun.workspace_id)
                )
            ).mappings().all()
            run_counts = {
                int(row["workspace_id"]): int(row["run_count"] or 0)
                for row in count_rows
                if row.get("workspace_id") is not None
            }

            latest_run_rows = (
                await self.db.execute(
                    select(
                        PaperExperimentRun.workspace_id.label("workspace_id"),
                        PaperExperimentRun.status.label("status"),
                        PaperExperimentRun.updated_at.label("updated_at"),
                    )
                    .where(PaperExperimentRun.workspace_id.in_(workspace_ids))
                    .order_by(
                        PaperExperimentRun.workspace_id.asc(),
                        PaperExperimentRun.updated_at.desc(),
                        PaperExperimentRun.id.desc(),
                    )
                )
            ).mappings().all()
            for row in latest_run_rows:
                workspace_id = row.get("workspace_id")
                if workspace_id is None:
                    continue
                workspace_key = int(workspace_id)
                if workspace_key not in latest_runs:
                    latest_runs[workspace_key] = {
                        "status": row.get("status"),
                        "updated_at": row.get("updated_at"),
                    }

        grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in workspace_rows:
            workspace_id = row.get("workspace_id")
            if workspace_id is None:
                continue
            workspace_key = int(workspace_id)
            grouped[int(row["project_id"])].append(
                {
                    "id": workspace_key,
                    "paper_id": int(row["paper_id"]) if row.get("paper_id") is not None else None,
                    "paper_title": str(row["paper_title"] or "") or None,
                    "notebook_id": row.get("notebook_id"),
                    "title": str(row["title"] or ""),
                    "status": str(row["status"] or "draft"),
                    "summary": dict(row.get("summary_json") or {}),
                    "experiment_spec": dict(row.get("experiment_spec_json") or {}),
                    "compare_report": dict(row.get("compare_report_json") or {}),
                    "role": str(row["role"] or "related_reproduction"),
                    "run_count": int(run_counts.get(workspace_key) or 0),
                    "latest_run_status": latest_runs.get(workspace_key, {}).get("status"),
                    "latest_run_at": latest_runs.get(workspace_key, {}).get("updated_at"),
                    "updated_at": row.get("updated_at"),
                    "_updated_at": row.get("updated_at"),
                }
            )

        for project_id, rows in grouped.items():
            grouped[project_id] = sorted(
                rows,
                key=lambda item: (
                    0 if str(item.get("role") or "") == "primary_reproduction" else 1,
                    -float(item.get("_updated_at").timestamp()) if item.get("_updated_at") is not None else 0.0,
                    -int(item.get("id") or 0),
                ),
            )
            for item in grouped[project_id]:
                item.pop("_updated_at", None)
        return grouped

    async def _build_runtime_context(
        self,
        *,
        runtime_service: ProjectRuntimeService,
        workspace: Dict[str, Any],
        workspace_dir: Optional[Path],
        project_id: int,
    ) -> Dict[str, Any]:
        experiment_spec = dict(workspace.get("experiment_spec") or {})
        repo_reference = self._safe_read_json_file(workspace_dir / "repo_reference.json" if workspace_dir else None)
        repo_index = self._safe_read_json_file(workspace_dir / "repo_file_index.json" if workspace_dir else None)
        workspace_manifest = self._safe_read_json_file(workspace_dir / "workspace_adapter_manifest.json" if workspace_dir else None)
        inspection: Dict[str, Any] = {}
        if workspace_dir and workspace_dir.is_dir():
            inspection = await runtime_service.inspect_runtime(
                workspace_dir=workspace_dir,
                project_id=project_id,
                workspace_id=int(workspace.get("id") or 0),
                notebook_id=str(workspace.get("notebook_id") or ""),
            )

        runtime_worker = dict(inspection.get("runtime_worker") or {})
        repo_payload = dict(inspection.get("repo") or {})
        entrypoint_hints = self._collect_entrypoint_hints(
            experiment_spec=experiment_spec,
            repo_reference=repo_reference,
            repo_index=repo_index,
        )
        notebook_asset_relative_path = next(
            (
                f"repo/source/{item}"
                for item in entrypoint_hints
                if str(item or "").strip().endswith(".ipynb")
            ),
            None,
        )
        return {
            "execution_mode": str(experiment_spec.get("execution_mode") or "").strip() or None,
            "notebook_id": str(workspace.get("notebook_id") or "").strip() or None,
            "notebook_asset_relative_path": notebook_asset_relative_path,
            "repo_available": bool(repo_payload.get("available") or repo_reference.get("status") == "cloned"),
            "repo_root_relative_path": str(repo_payload.get("detected_root_relative_path") or "repo/source").strip() or None,
            "repo_file_count": int(repo_payload.get("file_count") or repo_index.get("indexed_file_count") or 0),
            "repo_reference_url": str(repo_reference.get("repo_url") or workspace_manifest.get("repo", {}).get("repo_url") or "").strip() or None,
            "repo_history_candidate_count": int(
                repo_reference.get("history_candidate_count")
                or repo_index.get("history_candidate_count")
                or workspace_manifest.get("repo", {}).get("history_candidate_count")
                or 0
            ),
            "entrypoint_hints": entrypoint_hints,
            "runtime_candidates": [
                {
                    "runtime_type": str(item.get("runtime_type") or ""),
                    "status": str(item.get("status") or "unknown"),
                    "priority": int(item.get("priority") or 0),
                    "reason": str(item.get("reason") or "").strip() or None,
                    "entrypoints": [str(entry or "").strip() for entry in list(item.get("entrypoints") or []) if str(entry or "").strip()],
                    "evidence_files": [str(entry or "").strip() for entry in list(item.get("evidence_files") or []) if str(entry or "").strip()],
                    "blockers": [str(entry or "").strip() for entry in list(item.get("blockers") or []) if str(entry or "").strip()],
                    "requires_runtime_worker": bool(item.get("requires_runtime_worker")),
                    "requires_explicit_user_confirm": bool(item.get("requires_explicit_user_confirm")),
                }
                for item in list(inspection.get("runtime_candidates") or [])
                if isinstance(item, dict)
            ],
            "tools": [
                {
                    "tool_key": str(tool_key or ""),
                    "available": bool(dict(tool_payload or {}).get("available")),
                    "command": str(dict(tool_payload or {}).get("command") or "").strip() or None,
                }
                for tool_key, tool_payload in dict(inspection.get("tool_availability") or {}).items()
            ],
            "runtime_worker_enabled": bool(runtime_worker.get("enabled")),
            "runtime_worker_available": bool(runtime_worker.get("available")),
        }

    def _build_stage_ledger(
        self,
        *,
        workspace: Dict[str, Any],
        workspace_dir: Optional[Path],
        recent_executions: List[Dict[str, Any]],
        results: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        experiment_spec = dict(workspace.get("experiment_spec") or {})
        implementation_spec = self._safe_read_json_file(workspace_dir / "specs" / "implementation_spec.json" if workspace_dir else None)
        run_drafts = self._safe_read_json_file(workspace_dir / "drafts" / "run_drafts.json" if workspace_dir else None)
        compare_report = dict(workspace.get("compare_report") or {})

        planning_artifacts = self._artifact_group(
            workspace_dir,
            [
                ("论文 Markdown", "paper_intake_markdown.md", "markdown"),
                ("Intake payload", "paper_intake_payload.json", "json"),
                ("Intake result", "paper_intake_result.json", "json"),
                ("Experiment spec", "experiment_spec.json", "json"),
                ("Repo reference", "repo_reference.json", "json"),
                ("Repo index", "repo_file_index.json", "json"),
                ("历史候选", "repo_history_url_candidates.json", "json"),
            ],
        )
        planning_present = [item for item in planning_artifacts if bool(item.get("present"))]
        planning_complete = self._artifact_present(workspace_dir, "paper_intake_result.json") and self._artifact_present(workspace_dir, "experiment_spec.json")
        task = dict(experiment_spec.get("task") or {})
        planning_summary = (
            str(task.get("task_type") or "").strip()
            or str(task.get("domain") or "").strip()
            or ("已生成 intake 与 experiment spec" if planning_complete else None)
        )

        implementation_artifacts = self._artifact_group(
            workspace_dir,
            [("Implementation spec", "specs/implementation_spec.json", "json")],
        )
        implementation_blockers = self._extract_blockers(implementation_spec)
        readiness = dict(implementation_spec.get("readiness") or {})
        if not implementation_artifacts[0]["present"]:
            implementation_status = "missing"
        elif bool(readiness.get("can_execute")):
            implementation_status = "completed"
        elif implementation_blockers:
            implementation_status = "blocked"
        else:
            implementation_status = "ready"
        implementation_summary = (
            str(dict(implementation_spec.get("baseline") or {}).get("entrypoint") or "").strip()
            or str(implementation_spec.get("mode") or "").strip()
            or None
        )

        draft_items = list(run_drafts.get("drafts") or []) if isinstance(run_drafts, dict) else []
        run_drafts_artifacts = self._artifact_group(
            workspace_dir,
            [("Run drafts", "drafts/run_drafts.json", "json")],
        )
        if run_drafts_artifacts[0]["present"] and draft_items:
            run_drafts_status = "completed"
        elif implementation_artifacts[0]["present"]:
            run_drafts_status = "ready"
        else:
            run_drafts_status = "missing"
        run_drafts_summary = f"{len(draft_items)} drafts prepared" if draft_items else None

        latest_execution = recent_executions[0] if recent_executions else {}
        latest_execution_status = str(latest_execution.get("status") or "").strip().lower()
        latest_execution_artifacts: List[Dict[str, Any]] = []
        if latest_execution:
            for label, key, kind in [
                ("Execution spec", "spec_relative_path", "json"),
                ("Execution result", "result_relative_path", "json"),
                ("Execution log", "log_relative_path", "log"),
            ]:
                relative_path = str(latest_execution.get(key) or "").strip()
                if relative_path:
                    latest_execution_artifacts.append(
                        self._artifact_summary(
                            workspace_dir=workspace_dir,
                            label=label,
                            relative_path=relative_path,
                            kind=kind,
                            present=True,
                        )
                    )
        if any(str(item.get("status") or "").strip().lower() in {"running", "pending"} for item in recent_executions):
            execution_status = "running"
        elif any(self._execution_succeeded(item) for item in recent_executions):
            execution_status = "completed"
        elif any(str(item.get("status") or "").strip().lower() in {"failed", "blocked"} for item in recent_executions):
            execution_status = "blocked"
        elif draft_items:
            execution_status = "ready"
        else:
            execution_status = "missing"
        execution_blockers = []
        if latest_execution_status in {"failed", "blocked"}:
            execution_blockers = [
                str(latest_execution.get("error") or latest_execution.get("message") or latest_execution.get("last_log_line") or "").strip()
            ]
            execution_blockers = [item for item in execution_blockers if item]
        execution_summary = (
            str(latest_execution.get("label") or "").strip()
            or str(latest_execution.get("draft_id") or "").strip()
            or None
        )

        results_artifacts: List[Dict[str, Any]] = []
        baseline_execution_id = str(results.get("baseline_execution_id") or "").strip()
        if baseline_execution_id:
            results_artifacts.extend(
                [
                    self._artifact_summary(
                        workspace_dir=workspace_dir,
                        label="Baseline result",
                        relative_path=f"executions/{baseline_execution_id}/execution_result.json",
                        kind="json",
                        present=True,
                    ),
                    self._artifact_summary(
                        workspace_dir=workspace_dir,
                        label="Baseline log",
                        relative_path=f"executions/{baseline_execution_id}/execution.log",
                        kind="log",
                        present=True,
                    ),
                ]
            )
        if compare_report:
            results_artifacts.append(
                {
                    "label": "Compare report",
                    "relative_path": "workspace.compare_report_json",
                    "kind": "db_record",
                    "present": True,
                    "updated_at": str(workspace.get("latest_run_at") or workspace.get("updated_at") or "") or None,
                }
            )
        if str(results.get("baseline_status") or "") == "completed" or str(results.get("compare_status") or "") == "completed":
            results_status = "completed"
        elif execution_status == "running":
            results_status = "running"
        elif execution_status == "completed":
            results_status = "ready"
        else:
            results_status = "missing"
        results_summary = self._format_metric_summary(dict(results.get("baseline_metrics") or {})) or str(results.get("compare_summary") or "").strip() or None

        return [
            {
                "stage": "planning",
                "label": _RESULT_STAGE_LABELS["planning"],
                "status": "completed" if planning_complete else ("ready" if planning_present else "missing"),
                "summary": planning_summary,
                "blockers": [],
                "artifacts": planning_artifacts,
                "updated_at": self._latest_artifact_timestamp(planning_artifacts),
            },
            {
                "stage": "implementation_prep",
                "label": _RESULT_STAGE_LABELS["implementation_prep"],
                "status": implementation_status,
                "summary": implementation_summary,
                "blockers": implementation_blockers,
                "artifacts": implementation_artifacts,
                "updated_at": self._latest_artifact_timestamp(implementation_artifacts),
            },
            {
                "stage": "run_drafts",
                "label": _RESULT_STAGE_LABELS["run_drafts"],
                "status": run_drafts_status,
                "summary": run_drafts_summary,
                "blockers": [],
                "artifacts": run_drafts_artifacts,
                "updated_at": self._latest_artifact_timestamp(run_drafts_artifacts),
            },
            {
                "stage": "execution",
                "label": _RESULT_STAGE_LABELS["execution"],
                "status": execution_status,
                "summary": execution_summary,
                "blockers": execution_blockers,
                "artifacts": latest_execution_artifacts,
                "updated_at": self._execution_timestamp(latest_execution),
            },
            {
                "stage": "results",
                "label": _RESULT_STAGE_LABELS["results"],
                "status": results_status,
                "summary": results_summary,
                "blockers": [],
                "artifacts": results_artifacts,
                "updated_at": str(results.get("baseline_completed_at") or results.get("tuning_completed_at") or "") or None,
            },
        ]

    def _build_result_summary(
        self,
        *,
        workspace: Dict[str, Any],
        executions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        baseline_execution = self._pick_execution(executions, family="baseline")
        tuning_execution = self._pick_execution(executions, family="tuning")
        baseline_metrics = self._extract_execution_metrics(baseline_execution)
        tuning_metrics = self._extract_execution_metrics(tuning_execution)
        compare_report = dict(workspace.get("compare_report") or {})
        has_real_compare = self._has_meaningful_compare(compare_report, tuning_execution)
        compare_summary = self._extract_compare_summary(compare_report) if has_real_compare else None
        highlights = [item for item in [self._format_metric_summary(baseline_metrics), compare_summary] if item]
        return {
            "baseline_status": self._normalize_result_status(baseline_execution),
            "baseline_execution_id": str(baseline_execution.get("execution_id") or "").strip() or None,
            "baseline_completed_at": str(baseline_execution.get("completed_at") or "").strip() or None,
            "baseline_metrics": baseline_metrics,
            "tuning_status": self._normalize_result_status(tuning_execution),
            "tuning_execution_id": str(tuning_execution.get("execution_id") or "").strip() or None,
            "tuning_completed_at": str(tuning_execution.get("completed_at") or "").strip() or None,
            "tuning_metrics": tuning_metrics,
            "compare_status": "completed" if has_real_compare else "ready" if self._execution_succeeded(baseline_execution) else "missing",
            "compare_summary": compare_summary,
            "highlights": highlights,
        }

    def _derive_workspace_state(
        self,
        *,
        stage_ledger: List[Dict[str, Any]],
        results: Dict[str, Any],
        executions: List[Dict[str, Any]],
    ) -> Tuple[str, str]:
        if any(str(item.get("status") or "").strip().lower() in {"running", "pending"} for item in executions):
            return "execution", "running"
        if str(results.get("baseline_status") or "") == "completed":
            if str(results.get("tuning_status") or "") == "completed" or str(results.get("compare_status") or "") == "completed":
                return "results", "completed"
            return "tuning", "active"
        stage_map = {str(item.get("stage") or ""): item for item in stage_ledger}
        if str(stage_map.get("planning", {}).get("status") or "") == "missing":
            return "planning", "draft"
        if str(stage_map.get("implementation_prep", {}).get("status") or "") in {"missing", "blocked"}:
            return "implementation_prep", "blocked" if stage_map.get("implementation_prep", {}).get("status") == "blocked" else "active"
        if str(stage_map.get("run_drafts", {}).get("status") or "") == "missing":
            return "run_drafts", "active"
        if str(stage_map.get("execution", {}).get("status") or "") == "blocked":
            return "execution", "blocked"
        return "execution", "active"

    def _load_workspace_execution_summaries(self, *, workspace_dir: Optional[Path]) -> List[Dict[str, Any]]:
        if workspace_dir is None or not workspace_dir.is_dir():
            return []
        executions_root = workspace_dir / "executions"
        if not executions_root.is_dir():
            return []

        execution_dirs = [item for item in executions_root.iterdir() if item.is_dir()]
        execution_dirs.sort(key=self._execution_dir_sort_key, reverse=True)
        items: List[Dict[str, Any]] = []
        for execution_dir in execution_dirs:
            execution_id = str(execution_dir.name or "").strip()
            if not execution_id:
                continue
            spec = self._read_execution_spec_safe(workspace_dir=workspace_dir, execution_id=execution_id)
            result = self._read_execution_result_safe(workspace_dir=workspace_dir, execution_id=execution_id)
            items.append(
                {
                    "execution_id": execution_id,
                    "label": str(spec.get("label") or spec.get("name") or spec.get("draft_id") or execution_id),
                    "draft_id": str(spec.get("draft_id") or "").strip() or None,
                    "stage": str(spec.get("stage") or "").strip() or None,
                    "runtime_type": str(spec.get("runtime_type") or result.get("runtime_type") or ""),
                    "status": str(result.get("status") or "unknown"),
                    "success": result.get("success") if isinstance(result.get("success"), bool) else None,
                    "error": str(result.get("error") or "").strip() or None,
                    "message": str(result.get("message") or "").strip() or None,
                    "created_at": result.get("created_at") or spec.get("created_at"),
                    "started_at": result.get("started_at") or spec.get("started_at"),
                    "completed_at": result.get("completed_at"),
                    "command_preview": self._command_preview(spec.get("command")),
                }
            )
        return items

    def _derive_project_state(
        self,
        *,
        project: ResearchProject,
        workspace_overviews: List[Dict[str, Any]],
    ) -> Tuple[str, str, Optional[str]]:
        if not workspace_overviews:
            return "planning", "draft", "项目还没有 workspace，下一步应从 Chat 启动规划阶段。"
        primary_workspace_id = int(project.primary_workspace_id) if getattr(project, "primary_workspace_id", None) is not None else None
        selected = next((item for item in workspace_overviews if int(item.get("workspace_id") or 0) == primary_workspace_id), None)
        if selected is None:
            selected = workspace_overviews[0]
        if any(str(item.get("current_status") or "").strip().lower() == "running" for item in workspace_overviews):
            return "execution", "running", "存在后台 execution 正在运行，优先在本页观察日志和结果。"
        current_stage = str(selected.get("current_stage") or "planning")
        current_status = str(selected.get("current_status") or "draft")
        summary = next(
            (
                str(item.get("summary") or "").strip()
                for item in list(selected.get("stage_ledger") or [])
                if str(item.get("stage") or "") == current_stage and str(item.get("summary") or "").strip()
            ),
            None,
        )
        return current_stage, current_status, summary

    @staticmethod
    def _recommended_chat_stage(*, current_stage: str, current_status: str) -> str:
        stage = str(current_stage or "planning").strip().lower() or "planning"
        if stage == "results":
            return "tuning" if str(current_status or "").strip().lower() == "active" else "execution"
        if stage not in {"planning", "implementation_prep", "run_drafts", "execution", "tuning"}:
            stage = "execution"
        if str(current_status or "").strip().lower() == "running":
            return "execution"
        return stage

    @staticmethod
    def _safe_read_json_file(path: Optional[Path]) -> Dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")) or {})
        except Exception:
            return {}

    def _artifact_group(
        self,
        workspace_dir: Optional[Path],
        definitions: List[Tuple[str, str, str]],
    ) -> List[Dict[str, Any]]:
        return [
            self._artifact_summary(
                workspace_dir=workspace_dir,
                label=label,
                relative_path=relative_path,
                kind=kind,
                present=self._artifact_present(workspace_dir, relative_path),
            )
            for label, relative_path, kind in definitions
        ]

    @staticmethod
    def _artifact_present(workspace_dir: Optional[Path], relative_path: str) -> bool:
        if workspace_dir is None:
            return False
        return (Path(workspace_dir) / str(relative_path)).is_file()

    def _artifact_summary(
        self,
        *,
        workspace_dir: Optional[Path],
        label: str,
        relative_path: str,
        kind: str,
        present: bool,
    ) -> Dict[str, Any]:
        updated_at = None
        if workspace_dir is not None:
            path = Path(workspace_dir) / str(relative_path)
            if path.exists():
                try:
                    updated_at = datetime.utcfromtimestamp(path.stat().st_mtime).isoformat()
                except OSError:
                    updated_at = None
        return {
            "label": str(label or relative_path),
            "relative_path": str(relative_path),
            "kind": str(kind or "artifact"),
            "present": bool(present),
            "updated_at": updated_at,
        }

    @staticmethod
    def _latest_artifact_timestamp(artifacts: List[Dict[str, Any]]) -> Optional[str]:
        timestamps = [str(item.get("updated_at") or "").strip() for item in list(artifacts or []) if str(item.get("updated_at") or "").strip()]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _execution_timestamp(execution: Dict[str, Any]) -> Optional[str]:
        for key in ("completed_at", "started_at", "created_at"):
            value = str(execution.get(key) or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _collect_entrypoint_hints(
        *,
        experiment_spec: Dict[str, Any],
        repo_reference: Dict[str, Any],
        repo_index: Dict[str, Any],
    ) -> List[str]:
        hints: List[str] = []
        for item in list(dict(experiment_spec.get("execution_assets") or {}).get("entrypoint_hints") or []):
            if isinstance(item, dict):
                value = str(item.get("value") or "").strip()
                if value:
                    hints.append(value)
        for item in list(repo_reference.get("entrypoint_candidates") or []):
            if isinstance(item, dict):
                value = str(item.get("path") or "").strip()
                if value:
                    hints.append(value)
        for item in list(repo_index.get("entrypoint_candidates") or []):
            if isinstance(item, dict):
                value = str(item.get("path") or "").strip()
                if value:
                    hints.append(value)
        deduped: List[str] = []
        seen = set()
        for item in hints:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped[:8]

    @staticmethod
    def _extract_blockers(implementation_spec: Dict[str, Any]) -> List[str]:
        blockers: List[str] = []
        for item in list(implementation_spec.get("blockers") or []):
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("description") or item.get("resolution_action") or "").strip()
            else:
                text = ""
            if text:
                blockers.append(text)
        return blockers[:4]

    @staticmethod
    def _pick_execution(executions: List[Dict[str, Any]], *, family: str) -> Dict[str, Any]:
        if family == "baseline":
            matcher = ProjectService._is_baseline_execution
        else:
            matcher = ProjectService._is_tuning_execution
        matched = [item for item in list(executions or []) if matcher(item)]
        if not matched:
            return {}
        succeeded = [item for item in matched if ProjectService._execution_succeeded(item)]
        return succeeded[0] if succeeded else matched[0]

    @staticmethod
    def _normalize_result_status(execution: Dict[str, Any]) -> str:
        if not execution:
            return "missing"
        return str(execution.get("status") or "unknown").strip().lower() or "unknown"

    @staticmethod
    def _execution_text(execution: Dict[str, Any]) -> str:
        return " ".join(
            str(execution.get(key) or "").strip().lower()
            for key in ("stage", "draft_id", "label", "execution_id", "command_preview")
        )

    @classmethod
    def _is_prerequisite_execution(cls, execution: Dict[str, Any]) -> bool:
        text = cls._execution_text(execution)
        return any(
            token in text
            for token in (
                "env_setup",
                "env check",
                "check_env",
                "install_deps",
                "install deps",
                "install_torch",
                "dependency",
                "check_deps",
                "data_prep",
                "data preparation",
                "data_preparation",
                "prepare dataset",
                "dataset preparation",
                "smoke_test",
                "smoke test",
                "simple-test",
                "simple test",
                "test_seq2seq",
                "python --version",
            )
        )

    @staticmethod
    def _is_baseline_execution(execution: Dict[str, Any]) -> bool:
        stage = str(execution.get("stage") or "").strip().lower()
        if stage == "baseline_repro":
            return True
        text = ProjectService._execution_text(execution)
        if ProjectService._is_prerequisite_execution(execution):
            return False
        return "baseline" in text

    @staticmethod
    def _is_tuning_execution(execution: Dict[str, Any]) -> bool:
        stage = str(execution.get("stage") or "").strip().lower()
        if stage in {"tuning", "first_tuning", "compare"}:
            return True
        text = ProjectService._execution_text(execution)
        if ProjectService._is_prerequisite_execution(execution):
            return False
        return any(token in text for token in ("first_tuning", "tuning", "variant", "sweep", "ablation", "embedding", " lr", "lr="))

    @staticmethod
    def _execution_succeeded(execution: Dict[str, Any]) -> bool:
        status = str(execution.get("status") or "").strip().lower()
        return status == "completed" and execution.get("success") is not False

    def _extract_execution_metrics(self, execution: Dict[str, Any]) -> Dict[str, Any]:
        if not execution:
            return {}
        log_text = str(execution.get("log_tail") or "")
        last_signal = str(execution.get("last_log_line") or "")
        metric_text = log_text if log_text else last_signal
        matches = _METRIC_DICT_RE.findall(metric_text)
        for raw in reversed(matches):
            try:
                payload = ast.literal_eval(raw)
            except Exception:
                continue
            if isinstance(payload, dict) and any(key in payload for key in ("roc_auc", "acc", "balanced_acc", "loss", "mae", "rmse")):
                return {
                    str(key): value
                    for key, value in payload.items()
                    if isinstance(key, str) and isinstance(value, (int, float, str, bool))
                }
        return {}

    @staticmethod
    def _extract_compare_summary(compare_report: Dict[str, Any]) -> Optional[str]:
        if not compare_report:
            return None
        for key in ("summary", "recommendation", "winner", "headline"):
            value = compare_report.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        conclusion = compare_report.get("conclusion")
        if isinstance(conclusion, dict):
            for key in ("summary", "recommendation"):
                value = conclusion.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _has_meaningful_compare(compare_report: Dict[str, Any], tuning_execution: Dict[str, Any]) -> bool:
        if tuning_execution:
            return True
        if not compare_report:
            return False

        try:
            if int(compare_report.get("completed_runs") or 0) >= 2:
                return True
        except (TypeError, ValueError):
            pass

        runs = compare_report.get("runs")
        if isinstance(runs, list):
            completed = [
                item
                for item in runs
                if isinstance(item, dict)
                and str(item.get("status") or "").strip().lower() == "completed"
                and item.get("success") is not False
            ]
            if len(completed) >= 2:
                return True

        for key in ("baseline_delta", "deltas", "metric_deltas"):
            value = compare_report.get(key)
            if isinstance(value, dict) and value:
                return True
            if isinstance(value, list) and value:
                return True

        best_run_id = str(compare_report.get("best_run_id") or "").strip()
        baseline_run_id = str(compare_report.get("baseline_run_id") or compare_report.get("baseline_execution_id") or "").strip()
        ranking_metric = str(compare_report.get("ranking_metric") or "").strip()
        return bool(best_run_id and baseline_run_id and best_run_id != baseline_run_id and ranking_metric)

    @staticmethod
    def _format_metric_summary(metrics: Dict[str, Any]) -> Optional[str]:
        if not metrics:
            return None
        preferred = []
        for key in ("roc_auc", "acc", "balanced_acc", "loss", "rmse", "mae"):
            if key in metrics:
                value = metrics[key]
                if isinstance(value, float):
                    preferred.append(f"{key}={value:.4f}")
                else:
                    preferred.append(f"{key}={value}")
        if preferred:
            return " · ".join(preferred)
        parts = []
        for key, value in list(metrics.items())[:4]:
            parts.append(f"{key}={value}")
        return " · ".join(parts) if parts else None

    async def _load_recent_workspace_executions(
        self,
        *,
        project_id: int,
        workspace_dir: Optional[Path],
        limit: int,
        max_log_chars: int,
    ) -> List[Dict[str, Any]]:
        if workspace_dir is None or not workspace_dir.is_dir():
            return []
        executions_root = workspace_dir / "executions"
        if not executions_root.is_dir():
            return []

        execution_dirs = [item for item in executions_root.iterdir() if item.is_dir()]
        execution_dirs.sort(key=self._execution_dir_sort_key, reverse=True)
        selected = execution_dirs[: max(1, int(limit or 0))]
        runtime_service = ProjectRuntimeService()
        items: List[Dict[str, Any]] = []
        for execution_dir in selected:
            execution_id = str(execution_dir.name or "").strip()
            if not execution_id:
                continue
            spec = self._read_execution_spec_safe(workspace_dir=workspace_dir, execution_id=execution_id)
            payload = await runtime_service.get_execution(
                workspace_dir=workspace_dir,
                project_id=int(project_id),
                execution_id=execution_id,
                include_logs=True,
                max_log_chars=max_log_chars,
            )
            result = dict(payload.get("result") or {})
            log_text = str(result.get("log") or "")
            last_log_line = self._extract_last_log_line(log_text)
            progress = self._extract_progress_from_log_line(last_log_line)
            command_preview = self._command_preview(spec.get("command"))
            items.append(
                {
                    "execution_id": execution_id,
                    "label": str(spec.get("label") or spec.get("name") or spec.get("draft_id") or execution_id),
                    "draft_id": str(spec.get("draft_id") or "").strip() or None,
                    "runtime_type": str(spec.get("runtime_type") or payload.get("runtime_type") or ""),
                    "status": str(payload.get("status") or result.get("status") or "unknown"),
                    "success": result.get("success") if isinstance(result.get("success"), bool) else None,
                    "error": str(result.get("error") or "").strip() or None,
                    "message": str(result.get("message") or "").strip() or None,
                    "created_at": payload.get("created_at") or spec.get("created_at"),
                    "started_at": payload.get("started_at") or result.get("started_at"),
                    "completed_at": payload.get("completed_at") or result.get("completed_at"),
                    "spec_relative_path": f"executions/{execution_id}/execution_spec.json",
                    "result_relative_path": f"executions/{execution_id}/execution_result.json",
                    "log_relative_path": f"executions/{execution_id}/execution.log",
                    "result_exists": bool(result.get("result_exists")),
                    "log_exists": bool(result.get("log_exists")),
                    "log_total_chars": int(result.get("log_total_chars") or 0),
                    "log_truncated": bool(result.get("log_truncated")),
                    "log_tail": log_text or None,
                    "last_log_line": last_log_line,
                    "latest_elapsed_sec": progress.get("elapsed_sec"),
                    "latest_loss": progress.get("loss"),
                    "command_preview": command_preview,
                }
            )
        return items

    @staticmethod
    def _execution_dir_sort_key(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    @staticmethod
    def _read_execution_spec_safe(*, workspace_dir: Path, execution_id: str) -> Dict[str, Any]:
        path = Path(workspace_dir) / "executions" / str(execution_id) / "execution_spec.json"
        if not path.is_file():
            return {}
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")) or {})
        except Exception:
            return {}

    @staticmethod
    def _read_execution_result_safe(*, workspace_dir: Path, execution_id: str) -> Dict[str, Any]:
        path = Path(workspace_dir) / "executions" / str(execution_id) / "execution_result.json"
        if not path.is_file():
            return {}
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")) or {})
        except Exception:
            return {}

    @staticmethod
    def _extract_last_log_line(log_text: str) -> Optional[str]:
        lines = [line.strip() for line in str(log_text or "").splitlines() if line.strip()]
        return lines[-1] if lines else None

    @staticmethod
    def _extract_progress_from_log_line(line: Optional[str]) -> Dict[str, Optional[float]]:
        text = str(line or "").strip()
        matched = _TRAIN_PROGRESS_RE.search(text)
        if not matched:
            return {"elapsed_sec": None, "loss": None}
        try:
            elapsed_sec = float(matched.group(1))
        except (TypeError, ValueError):
            elapsed_sec = None
        try:
            loss = float(matched.group(2))
        except (TypeError, ValueError):
            loss = None
        return {"elapsed_sec": elapsed_sec, "loss": loss}

    @staticmethod
    def _command_preview(command: Any) -> Optional[str]:
        if isinstance(command, list):
            items = [str(item or "").strip() for item in command if str(item or "").strip()]
            return " ".join(items[:12])[:220] or None
        if isinstance(command, str):
            text = command.strip()
            return text[:220] or None
        return None

    def _serialize_project(
        self,
        project: ResearchProject,
        *,
        papers: List[Dict[str, Any]],
        workspaces: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        primary_paper = None
        primary_workspace = None
        for item in papers:
            if int(item.get("id") or 0) == int(getattr(project, "primary_paper_id", 0) or 0):
                primary_paper = item
                break
        if primary_paper is None:
            primary_paper = next((item for item in papers if str(item.get("role") or "") == "primary"), None)

        for item in workspaces:
            if int(item.get("id") or 0) == int(getattr(project, "primary_workspace_id", 0) or 0):
                primary_workspace = item
                break
        if primary_workspace is None:
            primary_workspace = next((item for item in workspaces if str(item.get("role") or "") == "primary_reproduction"), None)

        summary = dict(getattr(project, "summary_json", {}) or {})
        summary["paper_count"] = len(papers)
        summary["workspace_count"] = len(workspaces)

        return {
            "id": int(project.id),
            "user_id": int(project.user_id),
            "primary_paper_id": int(project.primary_paper_id) if getattr(project, "primary_paper_id", None) is not None else None,
            "primary_workspace_id": int(project.primary_workspace_id) if getattr(project, "primary_workspace_id", None) is not None else None,
            "title": str(project.title or ""),
            "goal": getattr(project, "goal", None),
            "status": str(project.status or "draft"),
            "summary": summary,
            "paper_count": len(papers),
            "workspace_count": len(workspaces),
            "primary_paper": primary_paper,
            "primary_workspace": primary_workspace,
            "papers": papers,
            "workspaces": workspaces,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }
