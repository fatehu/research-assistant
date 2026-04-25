from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.literature import Paper
from app.services.paper_intake_service import PaperIntakeService
from app.services.project_paths import get_project_root_dir
from app.services.repo_readme_reproduction_intake_service import RepoReadmeReproductionIntakeService
from app.services.zoekt_cli_service import ZoektCliService


_REPO_SKIPPED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
    ".idea",
    ".vscode",
}


class ProjectReferenceBuilderService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.paper_intake_service = PaperIntakeService()

    @staticmethod
    def required_reference_relative_paths() -> List[str]:
        return [
            "reference/paper/paper_pdf2md.md",
            "reference/paper/paper_interpretation.md",
            "reference/paper/paper_interpretation.json",
            "reference/repo/readme_intake.json",
        ]

    @classmethod
    def required_reference_paths(cls, project_dir: Path) -> List[Path]:
        return [Path(project_dir) / relative for relative in cls.required_reference_relative_paths()]

    @classmethod
    def reference_bundle_ready(cls, project_dir: Path) -> bool:
        return all(path.is_file() for path in cls.required_reference_paths(project_dir))

    async def build(
        self,
        *,
        paper: Paper,
        project_id: int,
        user_id: int,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        project_dir = get_project_root_dir(project_id)
        if not refresh and self.reference_bundle_ready(project_dir):
            summary = self._existing_summary(project_dir=project_dir, project_id=project_id)
            summary["zoekt_index"] = await self._ensure_project_zoekt_index(
                project_dir=project_dir,
                force_reindex=False,
            )
            return summary

        paper_reference_dir = project_dir / "reference" / "paper"
        repo_reference_dir = project_dir / "reference" / "repo"
        paper_reference_dir.mkdir(parents=True, exist_ok=True)
        repo_reference_dir.mkdir(parents=True, exist_ok=True)

        paper_bundle = await self._build_paper_intake(paper=paper, user_id=user_id)
        paper_markdown = str(paper_bundle.get("paper_markdown") or "")
        paper_intake = dict(paper_bundle.get("paper_intake") or {})

        paper_interpretation_json = self._build_paper_interpretation_json(
            paper=paper,
            paper_intake=paper_intake,
            intake_metadata=dict(paper_bundle.get("intake_metadata") or {}),
        )
        paper_interpretation_markdown = self._render_paper_interpretation_markdown(paper_interpretation_json)

        self._write_text(paper_reference_dir / "paper_pdf2md.md", paper_markdown.rstrip() + ("\n" if paper_markdown else ""))
        self._write_text(
            paper_reference_dir / "paper_interpretation.md",
            paper_interpretation_markdown.rstrip() + ("\n" if paper_interpretation_markdown else ""),
        )
        self._write_json(paper_reference_dir / "paper_interpretation.json", paper_interpretation_json)

        repo_url = self._resolve_primary_repo_url(paper_intake)
        repo_materialization = await self._materialize_repo(
            project_dir=project_dir,
            repo_url=repo_url,
            refresh=refresh,
        )
        repo_source_dir_text = str(repo_materialization.get("repo_source_dir") or "").strip()
        repo_source_dir = Path(repo_source_dir_text) if repo_source_dir_text else None
        repo_scan = self._scan_repo(repo_source_dir) if repo_source_dir and repo_source_dir.is_dir() else self._empty_repo_scan()
        readme_intake = await self._build_repo_readme_intake(
            repo_url=repo_url,
            repo_source_dir=repo_source_dir,
            repo_status=str(repo_materialization.get("status") or "missing_repo").strip() or "missing_repo",
            repo_scan=repo_scan,
        )
        self._write_json(repo_reference_dir / "readme_intake.json", readme_intake)
        zoekt_index = await self._ensure_project_zoekt_index(
            project_dir=project_dir,
            force_reindex=bool(refresh),
        )

        return {
            "project_id": int(project_id),
            "project_root": str(project_dir),
            "reference_root": str(project_dir / "reference"),
            "reference_ready": self.reference_bundle_ready(project_dir),
            "reference_files": self.required_reference_relative_paths(),
            "paper_reference": {
                "paper_markdown_relative_path": "reference/paper/paper_pdf2md.md",
                "paper_interpretation_markdown_relative_path": "reference/paper/paper_interpretation.md",
                "paper_interpretation_json_relative_path": "reference/paper/paper_interpretation.json",
            },
            "repo_reference": {
                "repo_relative_root": "repo/source",
                "readme_intake_relative_path": "reference/repo/readme_intake.json",
                "repo_materialization": repo_materialization,
            },
            "zoekt_index": zoekt_index,
        }

    async def _ensure_project_zoekt_index(
        self,
        *,
        project_dir: Path,
        force_reindex: bool,
    ) -> Dict[str, Any]:
        try:
            payload = await ZoektCliService.build_project_index(
                project_dir=project_dir,
                workspace_dir=project_dir,
                force_reindex=force_reindex,
            )
        except Exception as exc:
            logger.warning(f"project zoekt index build failed unexpectedly: {exc}")
            return {
                "success": False,
                "error": "zoekt_index_exception",
                "detail": str(exc),
            }
        if not bool(payload.get("success")):
            logger.warning(
                "project zoekt index build returned failure: "
                f"project_dir={project_dir} error={payload.get('error')}"
            )
        return dict(payload or {})

    async def _build_paper_intake(self, *, paper: Paper, user_id: int) -> Dict[str, Any]:
        return await self.paper_intake_service.build_intake(paper=paper, user_id=user_id)

    async def _materialize_repo(
        self,
        *,
        project_dir: Path,
        repo_url: Optional[str],
        refresh: bool,
    ) -> Dict[str, Any]:
        repo_root = Path(project_dir) / "repo"
        repo_source_dir = repo_root / "source"
        resolved_repo_url = str(repo_url or "").strip()
        if not resolved_repo_url:
            return {
                "status": "missing_repo_url",
                "repo_url": None,
                "repo_source_dir": None,
            }

        if refresh and repo_root.exists():
            shutil.rmtree(repo_root, ignore_errors=True)

        if repo_source_dir.is_dir() and any(repo_source_dir.iterdir()):
            return {
                "status": "reused",
                "repo_url": resolved_repo_url,
                "repo_source_dir": str(repo_source_dir),
            }

        git_path = shutil.which("git")
        if not git_path:
            return {
                "status": "git_unavailable",
                "repo_url": resolved_repo_url,
                "repo_source_dir": None,
            }

        repo_source_dir.parent.mkdir(parents=True, exist_ok=True)
        completed = await asyncio.to_thread(
            subprocess.run,
            [git_path, "clone", resolved_repo_url, str(repo_source_dir)],
            capture_output=True,
            text=True,
            check=False,
            timeout=360,
        )
        if completed.returncode != 0:
            logger.warning(
                "[ProjectReferenceBuilder] git clone failed project_id_dir={} repo_url={} error={}",
                project_dir,
                resolved_repo_url,
                str(completed.stderr or "").strip(),
            )
            shutil.rmtree(repo_root, ignore_errors=True)
            return {
                "status": "clone_failed",
                "repo_url": resolved_repo_url,
                "repo_source_dir": None,
                "error": str(completed.stderr or "").strip()[:1200],
            }

        return {
            "status": "cloned",
            "repo_url": resolved_repo_url,
            "repo_source_dir": str(repo_source_dir),
        }

    async def _build_repo_readme_intake(
        self,
        *,
        repo_url: Optional[str],
        repo_source_dir: Optional[Path],
        repo_status: str,
        repo_scan: Dict[str, Any],
    ) -> Dict[str, Any]:
        resolved_repo_url = str(repo_url or "").strip() or None
        if repo_source_dir is None or not repo_source_dir.is_dir():
            return {
                "schema_version": "project_repo_readme_intake_v1",
                "status": repo_status or "missing_repo",
                "repo_url": resolved_repo_url,
                "repo_root_relative_path": "repo/source",
                "readme_relative_path": None,
                **repo_scan,
                "blocking_questions": self._append_unique(
                    list(repo_scan.get("blocking_questions") or []),
                    ["Repository is not available under repo/source yet."],
                ),
            }

        readme_candidates = [str(item).strip() for item in list(repo_scan.get("readme_candidates") or []) if str(item or "").strip()]
        for relative_path in readme_candidates[:3]:
            target = repo_source_dir / relative_path
            try:
                readme_text = target.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not readme_text:
                continue

            payload = await RepoReadmeReproductionIntakeService().generate(
                repo_url=resolved_repo_url,
                readme_relative_path=relative_path,
                readme_text=readme_text,
            )
            return self._merge_repo_scan_into_readme_intake(
                payload=dict(payload or {}),
                repo_status=repo_status,
                repo_scan=repo_scan,
                readme_relative_path=relative_path,
            )

        return {
            "schema_version": "project_repo_readme_intake_v1",
            "status": "missing_readme",
            "repo_url": resolved_repo_url,
            "repo_root_relative_path": "repo/source",
            "readme_relative_path": None,
            **repo_scan,
            "blocking_questions": self._append_unique(
                list(repo_scan.get("blocking_questions") or []),
                ["No README file was found under repo/source."],
            ),
        }

    def _merge_repo_scan_into_readme_intake(
        self,
        *,
        payload: Dict[str, Any],
        repo_status: str,
        repo_scan: Dict[str, Any],
        readme_relative_path: str,
    ) -> Dict[str, Any]:
        merged = dict(payload or {})
        merged["status"] = "ready"
        merged["repo_status"] = repo_status or "ready"
        merged["repo_root_relative_path"] = "repo/source"
        merged["readme_relative_path"] = str(readme_relative_path or "").strip() or None
        merged["dependency_files"] = list(repo_scan.get("dependency_files") or [])
        merged["repo_structure"] = dict(repo_scan.get("repo_structure") or {})
        merged["indexed_file_count"] = int(repo_scan.get("indexed_file_count") or 0)
        merged["file_count_truncated"] = bool(repo_scan.get("file_count_truncated"))
        merged["readme_candidates"] = list(repo_scan.get("readme_candidates") or [])

        entrypoints = list(merged.get("entrypoints") or [])
        if not entrypoints:
            entrypoints = list(repo_scan.get("entrypoints") or [])
        merged["entrypoints"] = entrypoints

        merged["focus_files"] = self._append_unique(
            list(merged.get("focus_files") or []),
            list(repo_scan.get("focus_files") or []),
        )
        merged["focus_directories"] = self._append_unique(
            list(merged.get("focus_directories") or []),
            list(repo_scan.get("focus_directories") or []),
        )
        merged["blocking_questions"] = self._append_unique(
            list(merged.get("blocking_questions") or []),
            list(repo_scan.get("blocking_questions") or []),
        )
        return merged

    def _build_paper_interpretation_json(
        self,
        *,
        paper: Paper,
        paper_intake: Dict[str, Any],
        intake_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        profile = dict(paper_intake.get("paper_profile") or {})
        paper_pipeline = dict(paper_intake.get("paper_pipeline") or {})
        code_repositories = [
            {
                "url": str(item.get("url") or "").strip(),
                "role": str(item.get("role") or "").strip() or None,
                "priority": str(item.get("priority") or "").strip() or None,
                "evidence_text": str(item.get("evidence_text") or "").strip() or None,
            }
            for item in list(paper_intake.get("code_repositories") or [])
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        ]
        project_pages = [
            {
                "url": str(item.get("url") or "").strip(),
                "evidence_text": str(item.get("evidence_text") or "").strip() or None,
            }
            for item in list(paper_intake.get("project_page_candidates") or [])
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        ]
        datasets = [
            {
                "name": str(item.get("name") or "").strip() or None,
                "url": str(item.get("url") or "").strip() or None,
                "purpose": str(item.get("purpose") or "").strip() or None,
                "source_type": str(item.get("source_type") or "").strip() or None,
                "split_or_config": str(item.get("split_or_config") or "").strip() or None,
                "artifact_hint": str(item.get("artifact_hint") or "").strip() or None,
                "evidence_text": str(item.get("evidence_text") or "").strip() or None,
            }
            for item in list(paper_intake.get("dataset_candidates") or [])
            if isinstance(item, dict)
        ]
        models = [
            {
                "name": str(item.get("name") or "").strip(),
                "role": str(item.get("role") or "").strip() or None,
                "evidence_text": str(item.get("evidence_text") or "").strip() or None,
            }
            for item in list(paper_intake.get("models") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        metrics = [
            {
                "name": str(item.get("name") or "").strip(),
                "direction": str(item.get("direction") or "").strip() or None,
                "evidence_text": str(item.get("evidence_text") or "").strip() or None,
            }
            for item in list(paper_intake.get("metrics") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        verification_questions = [
            {
                "id": str(item.get("id") or "").strip() or None,
                "question": str(item.get("question") or "").strip(),
                "why_it_matters": str(item.get("why_it_matters") or "").strip() or None,
                "target": str(item.get("target") or "").strip() or None,
            }
            for item in list(paper_intake.get("verification_questions") or [])
            if isinstance(item, dict) and str(item.get("question") or "").strip()
        ]
        tuning_directions: List[Dict[str, Any]] = []
        for item in list(paper_intake.get("optimization_candidates") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            if not name:
                continue
            tuning_directions.append(
                {
                    "name": name,
                    "category": str(item.get("category") or "").strip() or None,
                    "rationale": str(item.get("rationale") or "").strip() or None,
                    "expected_effect": str(item.get("expected_effect") or "").strip() or None,
                    "risk": str(item.get("risk") or "").strip() or None,
                    "paper_values": [str(value).strip() for value in list(item.get("paper_values") or []) if str(value).strip()],
                }
            )
        for item in list(paper_intake.get("model_swap_candidates") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            tuning_directions.append(
                {
                    "name": name,
                    "category": str(item.get("swap_type") or "").strip() or "model_swap",
                    "rationale": str(item.get("reason") or "").strip() or None,
                    "expected_effect": str(item.get("expected_effect") or "").strip() or None,
                    "risk": str(item.get("risk") or "").strip() or None,
                    "paper_values": [],
                }
            )

        reproduction_risks = [
            question.get("why_it_matters")
            for question in verification_questions
            if str(question.get("why_it_matters") or "").strip()
        ]
        reproduction_risks = self._append_unique(
            reproduction_risks,
            [str(item).strip() for item in list(paper_intake.get("limitations") or []) if str(item or "").strip()],
        )

        return {
            "schema_version": "paper_interpretation_v1",
            "paper_id": int(paper.id),
            "title": str(paper.title or ""),
            "authors": list(getattr(paper, "authors", []) or []),
            "year": getattr(paper, "year", None),
            "venue": getattr(paper, "venue", None) or getattr(paper, "journal", None),
            "task_type": profile.get("task_type"),
            "domain": profile.get("domain"),
            "problem_definition": profile.get("problem_statement"),
            "author_intent": profile.get("author_intent"),
            "research_direction": profile.get("research_direction"),
            "core_innovation": profile.get("core_innovation"),
            "contribution_summary": profile.get("contribution_summary"),
            "research_method": profile.get("research_method"),
            "research_content": profile.get("research_content"),
            "experiment_goal": profile.get("experiment_goal"),
            "paper_pipeline": {
                "data_flow": paper_pipeline.get("data_flow"),
                "model_flow": paper_pipeline.get("model_flow"),
                "train_eval_flow": paper_pipeline.get("train_eval_flow"),
                "evidence_text": paper_pipeline.get("evidence_text"),
            },
            "models": models,
            "datasets": datasets,
            "metrics": metrics,
            "training_setup": dict(paper_intake.get("training_setup") or {}),
            "evaluation_setup": dict(paper_intake.get("evaluation_setup") or {}),
            "paper_resources": {
                "official_repositories": code_repositories,
                "project_pages": project_pages,
                "reference_links": [
                    {
                        "url": str(item.get("url") or "").strip(),
                        "category": str(item.get("category") or "").strip() or None,
                        "label": str(item.get("label") or "").strip() or None,
                        "evidence_text": str(item.get("evidence_text") or "").strip() or None,
                    }
                    for item in list(paper_intake.get("reference_links") or [])
                    if isinstance(item, dict) and str(item.get("url") or "").strip()
                ],
            },
            "verification_questions": verification_questions,
            "reproduction_risks": reproduction_risks,
            "tuning_directions": tuning_directions,
            "limitations": [str(item).strip() for item in list(paper_intake.get("limitations") or []) if str(item or "").strip()],
            "intake_metadata": {
                "source_mode": intake_metadata.get("source_mode"),
                "extractor": intake_metadata.get("extractor"),
                "page_count": int(intake_metadata.get("page_count") or 0),
                "total_chars": int(intake_metadata.get("total_chars") or 0),
                "sent_chars": int(intake_metadata.get("sent_chars") or 0),
                "truncated": bool(intake_metadata.get("truncated")),
            },
        }

    def _render_paper_interpretation_markdown(self, interpretation: Dict[str, Any]) -> str:
        lines = [
            f"# {str(interpretation.get('title') or '').strip()}",
            "",
            "## 论文解读",
            "",
        ]
        self._append_markdown_field(lines, "任务类型", interpretation.get("task_type"))
        self._append_markdown_field(lines, "领域", interpretation.get("domain"))
        self._append_markdown_field(lines, "问题定义", interpretation.get("problem_definition"))
        self._append_markdown_field(lines, "作者意图", interpretation.get("author_intent"))
        self._append_markdown_field(lines, "研究方向", interpretation.get("research_direction"))
        self._append_markdown_field(lines, "核心创新", interpretation.get("core_innovation"))
        self._append_markdown_field(lines, "贡献总结", interpretation.get("contribution_summary"))
        self._append_markdown_field(lines, "研究方法", interpretation.get("research_method"))
        self._append_markdown_field(lines, "研究内容", interpretation.get("research_content"))
        self._append_markdown_field(lines, "实验目标", interpretation.get("experiment_goal"))

        pipeline = dict(interpretation.get("paper_pipeline") or {})
        lines.extend(["", "## 论文流程", ""])
        self._append_markdown_field(lines, "数据流", pipeline.get("data_flow"))
        self._append_markdown_field(lines, "模型流", pipeline.get("model_flow"))
        self._append_markdown_field(lines, "训练与评测流程", pipeline.get("train_eval_flow"))

        lines.extend(["", "## 使用的模型", ""])
        for item in list(interpretation.get("models") or []):
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            role = str(item.get("role") or "").strip()
            suffix = f" ({role})" if role else ""
            lines.append(f"- {item['name']}{suffix}")
        if lines[-1] == "":
            lines.append("- 未提取到明确模型")

        lines.extend(["", "## 数据集与指标", ""])
        datasets = list(interpretation.get("datasets") or [])
        if datasets:
            lines.append("### 数据集")
            for item in datasets:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("url") or "unknown").strip()
                details = [str(item.get("purpose") or "").strip(), str(item.get("split_or_config") or "").strip()]
                details = [item for item in details if item]
                lines.append(f"- {name}" + (f" | {' | '.join(details)}" if details else ""))
        metrics = list(interpretation.get("metrics") or [])
        if metrics:
            lines.append("")
            lines.append("### 指标")
            for item in metrics:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                direction = str(item.get("direction") or "").strip()
                if name:
                    lines.append(f"- {name}" + (f" ({direction})" if direction else ""))

        lines.extend(["", "## 论文资源", ""])
        resources = dict(interpretation.get("paper_resources") or {})
        official_repos = list(resources.get("official_repositories") or [])
        if official_repos:
            lines.append("### 官方仓库")
            for item in official_repos:
                if isinstance(item, dict) and str(item.get("url") or "").strip():
                    lines.append(f"- {str(item.get('url') or '').strip()}")
        project_pages = list(resources.get("project_pages") or [])
        if project_pages:
            lines.append("")
            lines.append("### 项目页面")
            for item in project_pages:
                if isinstance(item, dict) and str(item.get("url") or "").strip():
                    lines.append(f"- {str(item.get('url') or '').strip()}")

        lines.extend(["", "## 复现风险", ""])
        risks = [str(item).strip() for item in list(interpretation.get("reproduction_risks") or []) if str(item or "").strip()]
        if risks:
            for item in risks:
                lines.append(f"- {item}")
        else:
            lines.append("- 未提取到明确复现风险")

        lines.extend(["", "## 调优方向", ""])
        tuning_directions = list(interpretation.get("tuning_directions") or [])
        if tuning_directions:
            for item in tuning_directions:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                rationale = str(item.get("rationale") or "").strip()
                if not name:
                    continue
                lines.append(f"- {name}" + (f": {rationale}" if rationale else ""))
        else:
            lines.append("- 未提取到明确调优方向")

        return "\n".join(lines).strip()

    @staticmethod
    def _append_markdown_field(lines: List[str], label: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            lines.append(f"- {label}: {text}")

    def _scan_repo(self, repo_source_dir: Path) -> Dict[str, Any]:
        files: List[str] = []
        truncated = False
        for path in sorted(Path(repo_source_dir).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repo_source_dir).as_posix()
            if any(part in _REPO_SKIPPED_DIRS for part in relative.split("/")):
                continue
            files.append(relative)
            if len(files) >= 500:
                truncated = True
                break

        readme_candidates = [item for item in files if item.lower().split("/")[-1].startswith("readme")]
        dependency_files = [
            item
            for item in files
            if item.lower().split("/")[-1]
            in {"requirements.txt", "environment.yml", "environment.yaml", "pyproject.toml", "setup.py", "pdm.lock", "poetry.lock"}
        ][:24]
        entrypoint_candidates = self._rank_repo_entrypoints(files)
        entrypoints = [
            {
                "path_or_hint": str(item.get("path") or "").strip(),
                "kind": "notebook" if str(item.get("path") or "").strip().endswith(".ipynb") else "script",
                "purpose": None,
                "evidence_text": None,
            }
            for item in entrypoint_candidates
            if str(item.get("path") or "").strip()
        ]
        focus_files = self._append_unique(
            readme_candidates[:1],
            dependency_files[:8] + [str(item.get("path") or "").strip() for item in entrypoint_candidates[:8]],
        )
        focus_directories = self._append_unique(
            [],
            [
                str(Path(item).parent).replace("\\", "/")
                for item in focus_files
                if str(Path(item).parent).replace("\\", "/") not in {"", "."}
            ],
        )
        top_level_entries = []
        for item in sorted(Path(repo_source_dir).iterdir(), key=lambda value: (not value.is_dir(), value.name.lower(), value.name)):
            if item.name in _REPO_SKIPPED_DIRS:
                continue
            top_level_entries.append(item.name + ("/" if item.is_dir() else ""))

        return {
            "indexed_file_count": len(files),
            "file_count_truncated": truncated,
            "readme_candidates": readme_candidates[:12],
            "dependency_files": dependency_files,
            "entrypoints": entrypoints,
            "focus_files": focus_files,
            "focus_directories": focus_directories,
            "repo_structure": {
                "top_level_entries": top_level_entries[:80],
                "sample_files": files[:120],
            },
            "blocking_questions": [],
        }

    @staticmethod
    def _rank_repo_entrypoints(files: Iterable[str]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for relative in list(files or []):
            lowered = str(relative or "").lower()
            file_name = lowered.split("/")[-1]
            if not (lowered.endswith(".py") or lowered.endswith(".ipynb")):
                continue
            score = 0
            if lowered.endswith(".py"):
                score += 2
            if lowered.endswith(".ipynb"):
                score += 1
            for token in ("train", "eval", "test", "infer", "predict", "main", "run", "demo", "finetune"):
                if token in lowered:
                    score += 2
            if file_name in {"main.py", "train.py", "run.py"}:
                score += 3
            if score <= 0:
                continue
            candidates.append({"path": relative, "score": score})
        candidates.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
        return candidates[:24]

    @staticmethod
    def _resolve_primary_repo_url(paper_intake: Dict[str, Any]) -> Optional[str]:
        repositories = [item for item in list(paper_intake.get("code_repositories") or []) if isinstance(item, dict)]
        ranked = sorted(
            repositories,
            key=lambda item: (
                0 if str(item.get("role") or "").strip() == "primary_official" else 1,
                0 if str(item.get("priority") or "").strip() == "primary" else 1,
                str(item.get("url") or ""),
            ),
        )
        for item in ranked:
            url = str(item.get("url") or "").strip()
            if url:
                return url
        for item in list(paper_intake.get("reference_links") or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("category") or "").strip() != "official_repo":
                continue
            url = str(item.get("url") or "").strip()
            if url:
                return url
        return None

    @staticmethod
    def _append_unique(base: Iterable[str], extra: Iterable[str]) -> List[str]:
        items: List[str] = []
        seen: set[str] = set()
        for candidate in [*list(base or []), *list(extra or [])]:
            text = str(candidate or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            items.append(text)
        return items

    @staticmethod
    def _empty_repo_scan() -> Dict[str, Any]:
        return {
            "indexed_file_count": 0,
            "file_count_truncated": False,
            "readme_candidates": [],
            "dependency_files": [],
            "entrypoints": [],
            "focus_files": [],
            "focus_directories": [],
            "repo_structure": {"top_level_entries": [], "sample_files": []},
            "blocking_questions": [],
        }

    @staticmethod
    def _existing_summary(*, project_dir: Path, project_id: int) -> Dict[str, Any]:
        return {
            "project_id": int(project_id),
            "project_root": str(project_dir),
            "reference_root": str(project_dir / "reference"),
            "reference_ready": True,
            "reference_files": ProjectReferenceBuilderService.required_reference_relative_paths(),
            "paper_reference": {
                "paper_markdown_relative_path": "reference/paper/paper_pdf2md.md",
                "paper_interpretation_markdown_relative_path": "reference/paper/paper_interpretation.md",
                "paper_interpretation_json_relative_path": "reference/paper/paper_interpretation.json",
            },
            "repo_reference": {
                "repo_relative_root": "repo/source",
                "readme_intake_relative_path": "reference/repo/readme_intake.json",
                "repo_materialization": {
                    "status": "reused_existing_reference",
                    "repo_source_dir": str(project_dir / "repo" / "source"),
                },
            },
        }

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload or {}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
