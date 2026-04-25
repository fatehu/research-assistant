from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.literature import (
    Paper,
    ResearchProject,
    research_project_papers_association,
    research_project_workspaces_association,
)
from app.services.project_paths import get_project_root_dir


class ProjectService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_projects(self, *, user_id: int, paper_id: Optional[int] = None) -> List[ResearchProject]:
        stmt = (
            select(ResearchProject)
            .where(ResearchProject.user_id == int(user_id))
            .options(selectinload(ResearchProject.primary_paper))
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
        result = await self.db.execute(
            select(ResearchProject)
            .where(
                ResearchProject.id == int(project_id),
                ResearchProject.user_id == int(user_id),
            )
            .options(selectinload(ResearchProject.primary_paper))
        )
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

    async def get_project_folder_tree(self, *, project_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        project = await self.get_project(project_id=project_id, user_id=user_id)
        if project is None:
            return None
        project_root = get_project_root_dir(int(project.id), ensure_exists=False)
        return {
            "project_id": int(project.id),
            "project_root": str(project_root),
            "exists": project_root.exists(),
            "tree": self._render_project_tree(project_root),
        }

    async def delete_project(self, *, project_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        project = await self.get_project(project_id=project_id, user_id=user_id)
        if project is None:
            return None

        project_root = get_project_root_dir(int(project.id), ensure_exists=False)

        await self.db.execute(
            delete(research_project_workspaces_association).where(
                research_project_workspaces_association.c.project_id == int(project.id)
            )
        )
        await self.db.execute(
            delete(research_project_papers_association).where(
                research_project_papers_association.c.project_id == int(project.id)
            )
        )
        await self.db.execute(
            delete(ResearchProject).where(
                ResearchProject.id == int(project.id),
                ResearchProject.user_id == int(user_id),
            )
        )
        await self.db.commit()

        deleted_project_root = project_root.exists()
        shutil.rmtree(project_root, ignore_errors=True)

        return {
            "project_id": int(project.id),
            "deleted": True,
            "project_root": str(project_root),
            "deleted_project_root": deleted_project_root,
        }

    async def serialize_projects(self, projects: Iterable[ResearchProject]) -> List[Dict[str, Any]]:
        ordered_projects = [item for item in list(projects or []) if item is not None]
        if not ordered_projects:
            return []

        project_ids = [int(item.id) for item in ordered_projects if getattr(item, "id", None) is not None]
        papers_map = await self._load_project_papers(project_ids)
        return [
            self._serialize_project(
                project,
                papers=list(papers_map.get(int(project.id), []) or []),
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
            primary_workspace_id=None,
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

    @staticmethod
    def _normalize_paper_ids(paper_ids: Iterable[int]) -> List[int]:
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
            select(Paper).where(
                Paper.user_id == int(user_id),
                Paper.id.in_(paper_ids),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    def _resolve_primary_paper(*, paper_ids: List[int], papers: List[Paper]) -> Optional[Paper]:
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

    def _serialize_project(
        self,
        project: ResearchProject,
        *,
        papers: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        project_root = get_project_root_dir(int(project.id), ensure_exists=False)
        primary_paper = None
        for item in papers:
            if int(item.get("id") or 0) == int(getattr(project, "primary_paper_id", 0) or 0):
                primary_paper = item
                break
        if primary_paper is None:
            primary_paper = next((item for item in papers if str(item.get("role") or "") == "primary"), None)

        summary = dict(getattr(project, "summary_json", {}) or {})
        summary["paper_count"] = len(papers)
        summary["workspace_count"] = 0

        return {
            "id": int(project.id),
            "user_id": int(project.user_id),
            "primary_paper_id": int(project.primary_paper_id) if getattr(project, "primary_paper_id", None) is not None else None,
            "primary_workspace_id": None,
            "title": str(project.title or ""),
            "goal": getattr(project, "goal", None),
            "status": str(project.status or "draft"),
            "summary": summary,
            "paper_count": len(papers),
            "workspace_count": 0,
            "primary_paper": primary_paper,
            "primary_workspace": None,
            "papers": papers,
            "workspaces": [],
            "project_root": str(project_root),
            "project_root_exists": project_root.exists(),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }

    @staticmethod
    def _render_project_tree(project_dir: Path) -> str:
        root = Path(project_dir)
        if not root.exists():
            return "."

        lines = ["."]

        def _walk(current_dir: Path, prefix: str) -> None:
            try:
                entries = sorted(
                    list(current_dir.iterdir()),
                    key=lambda item: (not item.is_dir(), str(item.name).lower(), str(item.name)),
                )
            except OSError:
                lines.append(f"{prefix}`-- [unreadable]")
                return

            for index, entry in enumerate(entries):
                is_last = index == len(entries) - 1
                branch = "`-- " if is_last else "|-- "
                label = f"{entry.name}/" if entry.is_dir() and not entry.is_symlink() else entry.name
                lines.append(f"{prefix}{branch}{label}")
                if entry.is_dir() and not entry.is_symlink():
                    _walk(entry, prefix + ("    " if is_last else "|   "))

        _walk(root, "")
        return "\n".join(lines)
