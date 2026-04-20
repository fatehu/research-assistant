from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
from loguru import logger

from app.models.literature import Paper
from app.services.notebook_workspace_service import ensure_notebook_workspace, list_notebook_workspace_files


_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>`]+", flags=re.IGNORECASE)
_HISTORY_DELIM = "__COMMIT__"


class PaperExperimentAdapterService:
    """Materialize paper-intake artifacts into a CodeLab workspace.

    This service intentionally fails open. The notebook workspace should still
    be usable even when repo acquisition fails.
    """

    async def prepare_workspace(
        self,
        *,
        paper: Paper,
        notebook_id: str,
        user_id: int,
        summary: Dict[str, Any],
        experiment_spec: Dict[str, Any],
        materials: Dict[str, Any],
    ) -> Dict[str, Any]:
        workspace_dir = Path(ensure_notebook_workspace(notebook_id, user_id))
        intake_payload = dict(materials.get("intake_payload") or {})
        intake_json = dict(materials.get("paper_intake") or {})
        paper_markdown = str(materials.get("paper_markdown") or "")

        self._write_json(workspace_dir / "paper_metadata.json", self._paper_metadata_payload(paper))
        self._write_json(workspace_dir / "paper_intake_payload.json", self._safe_intake_payload(intake_payload))
        self._write_json(workspace_dir / "paper_intake_result.json", intake_json)
        self._write_json(workspace_dir / "experiment_spec.json", dict(experiment_spec or {}))
        self._write_text(workspace_dir / "paper_intake_markdown.md", paper_markdown)
        self._write_text(
            workspace_dir / "WORKSPACE_README.md",
            self._build_workspace_readme(paper=paper, summary=summary, experiment_spec=experiment_spec),
        )

        template_files = self._materialize_run_templates(workspace_dir=workspace_dir, experiment_spec=experiment_spec)
        repo_manifest = await self._prepare_repo_reference(
            workspace_dir=workspace_dir,
            repo_urls=list(summary.get("repo_urls") or []),
            experiment_spec=experiment_spec,
        )
        repo_index = self._build_repo_index(
            workspace_dir=workspace_dir,
            repo_manifest=repo_manifest,
            experiment_spec=experiment_spec,
        )
        repo_manifest.update(
            {
                "repo_file_index_file": str(repo_index.get("repo_file_index_file") or "repo_file_index.json"),
                "readme_excerpt_file": repo_index.get("readme_excerpt_file"),
                "repo_history_candidates_file": repo_index.get("repo_history_candidates_file"),
                "history_candidate_count": int(repo_index.get("history_candidate_count") or 0),
                "indexed_file_count": int(repo_index.get("indexed_file_count") or 0),
                "entrypoint_candidates": list(repo_index.get("entrypoint_candidates") or []),
                "dependency_files": list(repo_index.get("dependency_files") or []),
                "readme_candidates": list(repo_index.get("readme_candidates") or []),
            }
        )
        self._write_json(workspace_dir / "repo_reference.json", repo_manifest)

        manifest = {
            "status": "ready",
            "workspace_dir": str(workspace_dir),
            "paper_markdown_file": "paper_intake_markdown.md",
            "intake_payload_file": "paper_intake_payload.json",
            "intake_json_file": "paper_intake_result.json",
            "experiment_spec_file": "experiment_spec.json",
            "readme_file": "WORKSPACE_README.md",
            "template_files": template_files,
            "repo_index_file": str(repo_index.get("repo_file_index_file") or "repo_file_index.json"),
            "repo": repo_manifest,
        }
        self._write_json(workspace_dir / "workspace_adapter_manifest.json", manifest)
        files = list_notebook_workspace_files(notebook_id, user_id)
        manifest["workspace_files"] = files
        manifest["workspace_file_count"] = len(files)
        self._write_json(workspace_dir / "workspace_adapter_manifest.json", manifest)
        self._write_json(
            workspace_dir / "experiment_spec.json",
            {**dict(experiment_spec or {}), "workspace_adapter": manifest},
        )
        return manifest

    async def materialize_repo(
        self,
        *,
        workspace_dir: Path,
        repo_url: Optional[str] = None,
        experiment_spec: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        """Clone/reuse the paper repo and refresh repo index artifacts.

        This is the small, repeatable primitive exposed to the agent. It avoids
        making the full PDF/intake preparation step the only way to acquire or
        refresh repository evidence.
        """

        workspace_dir = Path(workspace_dir)
        spec = dict(experiment_spec or {})
        repo_urls = [str(repo_url or "").strip()] if str(repo_url or "").strip() else self._repo_urls_from_spec(spec)
        repo_dir = workspace_dir / "paper_repo"
        existing_reference = self._read_json(workspace_dir / "repo_reference.json")
        existing_url = str(existing_reference.get("repo_url") or "").strip()
        requested_url = next((item for item in repo_urls if item), "")

        if repo_dir.exists() and requested_url and existing_url and requested_url != existing_url and not refresh:
            manifest = {
                "status": "blocked_existing_repo_url_mismatch",
                "repo_url": requested_url,
                "existing_repo_url": existing_url,
                "message": "A different repo is already materialized. Set refresh=true to replace it.",
                "repo_dir": str(repo_dir),
            }
            return {
                "status": manifest["status"],
                "repo": manifest,
                "repo_index": self._build_repo_index(
                    workspace_dir=workspace_dir,
                    repo_manifest=existing_reference or manifest,
                    experiment_spec=spec,
                ),
            }

        if refresh and repo_dir.exists():
            shutil.rmtree(repo_dir)

        repo_manifest = await self._prepare_repo_reference(
            workspace_dir=workspace_dir,
            repo_urls=repo_urls,
            experiment_spec=spec,
        )
        repo_index = self._build_repo_index(
            workspace_dir=workspace_dir,
            repo_manifest=repo_manifest,
            experiment_spec=spec,
        )
        repo_manifest.update(
            {
                "repo_file_index_file": str(repo_index.get("repo_file_index_file") or "repo_file_index.json"),
                "readme_excerpt_file": repo_index.get("readme_excerpt_file"),
                "repo_history_candidates_file": repo_index.get("repo_history_candidates_file"),
                "history_candidate_count": int(repo_index.get("history_candidate_count") or 0),
                "indexed_file_count": int(repo_index.get("indexed_file_count") or 0),
                "entrypoint_candidates": list(repo_index.get("entrypoint_candidates") or []),
                "dependency_files": list(repo_index.get("dependency_files") or []),
                "readme_candidates": list(repo_index.get("readme_candidates") or []),
            }
        )
        self._write_json(workspace_dir / "repo_reference.json", repo_manifest)
        self._merge_workspace_manifest_repo(
            workspace_dir=workspace_dir,
            repo_manifest=repo_manifest,
            repo_index=repo_index,
        )
        return {
            "status": str(repo_manifest.get("status") or "missing"),
            "repo": repo_manifest,
            "repo_index": repo_index,
        }

    def ensure_workspace_archive_from_existing_state(
        self,
        *,
        paper: Paper,
        workspace_dir: Path,
        summary: Dict[str, Any],
        experiment_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Backfill canonical workspace files from persisted DB state.

        Older workspaces may already have structured intake/spec state in DB but
        miss the adapter-side archive files that newer skills rely on. This path
        rewrites the canonical files without rerunning PDF parsing or the intake
        LLM.
        """

        workspace_dir = Path(workspace_dir)
        intake_payload = dict(summary.get("paper_llm_input") or {})
        intake_json = dict(summary.get("paper_intake") or {})

        self._write_json(workspace_dir / "paper_metadata.json", self._paper_metadata_payload(paper))
        if intake_payload:
            self._write_json(workspace_dir / "paper_intake_payload.json", self._safe_intake_payload(intake_payload))
        if intake_json:
            self._write_json(workspace_dir / "paper_intake_result.json", intake_json)
        self._write_json(workspace_dir / "experiment_spec.json", dict(experiment_spec or {}))
        self._write_text(
            workspace_dir / "WORKSPACE_README.md",
            self._build_workspace_readme(
                paper=paper,
                summary=dict(summary or {}),
                experiment_spec=dict(experiment_spec or {}),
            ),
        )

        repo_manifest = self._read_json(workspace_dir / "repo_reference.json")
        if not repo_manifest:
            repo_manifest = dict(dict(summary.get("workspace_adapter") or {}).get("repo") or {})
            if repo_manifest:
                self._write_json(workspace_dir / "repo_reference.json", repo_manifest)
        repo_index = self._build_repo_index(
            workspace_dir=workspace_dir,
            repo_manifest=repo_manifest,
            experiment_spec=dict(experiment_spec or {}),
        )
        repo_manifest = self._read_json(workspace_dir / "repo_reference.json") or repo_manifest
        repo_manifest.update(
            {
                "repo_file_index_file": str(repo_index.get("repo_file_index_file") or "repo_file_index.json"),
                "readme_excerpt_file": repo_index.get("readme_excerpt_file"),
                "repo_history_candidates_file": repo_index.get("repo_history_candidates_file"),
                "history_candidate_count": int(repo_index.get("history_candidate_count") or 0),
                "indexed_file_count": int(repo_index.get("indexed_file_count") or 0),
                "entrypoint_candidates": list(repo_index.get("entrypoint_candidates") or []),
                "dependency_files": list(repo_index.get("dependency_files") or []),
                "readme_candidates": list(repo_index.get("readme_candidates") or []),
            }
        )
        if repo_manifest:
            self._write_json(workspace_dir / "repo_reference.json", repo_manifest)

        existing_manifest = self._read_json(workspace_dir / "workspace_adapter_manifest.json")
        manifest = {
            **existing_manifest,
            "status": str(existing_manifest.get("status") or "rehydrated"),
            "workspace_dir": str(workspace_dir),
            "paper_markdown_file": existing_manifest.get("paper_markdown_file"),
            "intake_payload_file": "paper_intake_payload.json" if intake_payload else existing_manifest.get("intake_payload_file"),
            "intake_json_file": "paper_intake_result.json" if intake_json else existing_manifest.get("intake_json_file"),
            "experiment_spec_file": "experiment_spec.json",
            "readme_file": "WORKSPACE_README.md",
            "template_files": list(existing_manifest.get("template_files") or []),
            "repo_index_file": str(repo_index.get("repo_file_index_file") or "repo_file_index.json"),
            "repo": repo_manifest,
        }
        self._write_json(workspace_dir / "workspace_adapter_manifest.json", manifest)
        return manifest

    def _repo_urls_from_spec(self, experiment_spec: Dict[str, Any]) -> List[str]:
        urls: List[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in urls:
                urls.append(text)

        sources = dict(experiment_spec.get("sources") or {})
        for item in list(sources.get("repo_urls") or []):
            add(item)

        assets = dict(experiment_spec.get("execution_assets") or {})
        repositories = list(assets.get("code_repositories") or [])
        repositories.sort(
            key=lambda item: (
                0
                if str(dict(item or {}).get("role") or "") == "primary_official"
                or str(dict(item or {}).get("priority") or "") == "primary"
                else 1
            )
        )
        for item in repositories:
            if isinstance(item, dict):
                add(item.get("url"))
        return urls

    def _materialize_run_templates(self, *, workspace_dir: Path, experiment_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        template_rows: List[Dict[str, Any]] = []
        for idx, item in enumerate(list(experiment_spec.get("codelab_run_templates") or []), start=1):
            payload = dict(item or {})
            code = str(payload.get("python_code") or "").strip()
            if not code:
                continue
            target = str(payload.get("target") or f"template_{idx}").strip().lower() or f"template_{idx}"
            file_name = f"run_template_{target}_{idx}.py"
            self._write_text(workspace_dir / file_name, code.rstrip() + "\n")
            template_rows.append(
                {
                    "target": target,
                    "title": str(payload.get("title") or f"Run Template {idx}").strip() or f"Run Template {idx}",
                    "description": str(payload.get("description") or "").strip(),
                    "file_name": file_name,
                }
            )
        return template_rows

    async def _prepare_repo_reference(
        self,
        *,
        workspace_dir: Path,
        repo_urls: Iterable[str],
        experiment_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        repo_url = next((str(item).strip() for item in list(repo_urls or []) if str(item or "").strip()), "")
        if not repo_url:
            manifest = {
                "status": "missing",
                "repo_url": "",
                "message": "No repository URL was resolved from the paper intake.",
            }
            self._write_json(workspace_dir / "repo_reference.json", manifest)
            return manifest

        repo_match = _GITHUB_REPO_RE.match(repo_url)
        if repo_match is None:
            manifest = {
                "status": "unsupported_host",
                "repo_url": repo_url,
                "message": "Only GitHub URLs are supported for automatic repo acquisition in this adapter.",
            }
            self._write_json(workspace_dir / "repo_reference.json", manifest)
            return manifest

        owner = str(repo_match.group("owner") or "").strip()
        repo = str(repo_match.group("repo") or "").strip()
        repo_dir = workspace_dir / "paper_repo"
        entrypoint_hints = list(experiment_spec.get("entrypoint_hints") or [])
        manifest: Dict[str, Any] = {
            "status": "planned",
            "repo_url": repo_url,
            "host": "github",
            "owner": owner,
            "repo": repo,
            "repo_dir": str(repo_dir),
            "entrypoint_hints": entrypoint_hints,
        }

        if repo_dir.exists() and any(repo_dir.iterdir()):
            git_path = shutil.which("git")
            history_refresh = await self._ensure_repo_history_available(
                git_path=git_path,
                repo_dir=repo_dir,
            )
            manifest["status"] = "reused"
            manifest.update(history_refresh)
            self._write_json(workspace_dir / "repo_reference.json", manifest)
            return manifest

        repo_dir.mkdir(parents=True, exist_ok=True)
        git_path = shutil.which("git")
        if git_path:
            clone_result = await self._clone_repo_via_git(git_path=git_path, repo_url=repo_url, repo_dir=repo_dir)
            manifest.update(clone_result)
            manifest.update(
                await self._ensure_repo_history_available(
                    git_path=git_path,
                    repo_dir=repo_dir,
                )
            )
            self._write_json(workspace_dir / "repo_reference.json", manifest)
            return manifest

        archive_result = await self._download_github_archive(owner=owner, repo=repo, repo_dir=repo_dir)
        manifest.update(archive_result)
        self._write_json(workspace_dir / "repo_reference.json", manifest)
        return manifest

    async def _clone_repo_via_git(self, *, git_path: str, repo_url: str, repo_dir: Path) -> Dict[str, Any]:
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [git_path, "clone", repo_url, str(repo_dir)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PaperExperimentAdapter] git clone failed: {}", exc)
            return {
                "status": "clone_failed",
                "method": "git",
                "error": f"{type(exc).__name__}: {exc}",
            }

        if completed.returncode != 0:
            stderr = str(completed.stderr or "").strip()
            return {
                "status": "clone_failed",
                "method": "git",
                "error": stderr[:1200],
            }
        return {
            "status": "cloned",
            "method": "git",
        }

    async def _ensure_repo_history_available(self, *, git_path: Optional[str], repo_dir: Path) -> Dict[str, Any]:
        if not git_path:
            return {"history_status": "git_unavailable"}
        if not (repo_dir / ".git").exists():
            return {"history_status": "no_git_metadata"}

        try:
            probe = await asyncio.to_thread(
                subprocess.run,
                [git_path, "-C", str(repo_dir), "rev-parse", "--is-shallow-repository"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            return {"history_status": "probe_failed", "history_error": f"{type(exc).__name__}: {exc}"}

        is_shallow = str(probe.stdout or "").strip().lower() == "true"
        if not is_shallow:
            return {"history_status": "available", "history_refreshed": False}

        try:
            fetch = await asyncio.to_thread(
                subprocess.run,
                [git_path, "-C", str(repo_dir), "fetch", "--deepen", "64", "origin"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            return {"history_status": "refresh_failed", "history_error": f"{type(exc).__name__}: {exc}"}

        if fetch.returncode != 0:
            return {
                "history_status": "refresh_failed",
                "history_error": str(fetch.stderr or "").strip()[:1200],
            }

        return {"history_status": "available", "history_refreshed": True}

    async def _download_github_archive(self, *, owner: str, repo: str, repo_dir: Path) -> Dict[str, Any]:
        default_branch = await self._resolve_default_branch(owner=owner, repo=repo)
        if not default_branch:
            default_branch = "main"

        archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{default_branch}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True) as client:
                response = await client.get(archive_url)
                response.raise_for_status()
                archive_bytes = response.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PaperExperimentAdapter] archive download failed: {}", exc)
            return {
                "status": "archive_failed",
                "method": "github_archive",
                "default_branch": default_branch,
                "archive_url": archive_url,
                "error": f"{type(exc).__name__}: {exc}",
            }

        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
                handle.write(archive_bytes)
                temp_path = Path(handle.name)
            with zipfile.ZipFile(temp_path, "r") as archive:
                archive.extractall(repo_dir)
            extracted_roots = [item.name for item in repo_dir.iterdir()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PaperExperimentAdapter] archive extract failed: {}", exc)
            return {
                "status": "archive_extract_failed",
                "method": "github_archive",
                "default_branch": default_branch,
                "archive_url": archive_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            try:
                temp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
            except Exception:
                pass

        return {
            "status": "archived",
            "method": "github_archive",
            "default_branch": default_branch,
            "archive_url": archive_url,
            "extracted_roots": extracted_roots,
        }

    async def _resolve_default_branch(self, *, owner: str, repo: str) -> str:
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True) as client:
                response = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return ""
        branch = str(dict(payload or {}).get("default_branch") or "").strip()
        return branch

    def _build_repo_index(
        self,
        *,
        workspace_dir: Path,
        repo_manifest: Dict[str, Any],
        experiment_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        repo_dir_text = str(repo_manifest.get("repo_dir") or "").strip()
        repo_status = str(repo_manifest.get("status") or "missing").strip() or "missing"
        repo_dir = Path(repo_dir_text) if repo_dir_text else None
        hints = [
            str(item.get("value") or item.get("evidence_text") or "").strip()
            for item in list(experiment_spec.get("entrypoint_hints") or [])
            if isinstance(item, dict)
        ]
        payload: Dict[str, Any] = {
            "status": "missing",
            "repo_status": repo_status,
            "repo_dir": repo_dir_text,
            "repo_file_index_file": "repo_file_index.json",
            "entrypoint_hints": [item for item in hints if item],
            "indexed_file_count": 0,
            "file_count_truncated": False,
            "files": [],
            "entrypoint_candidates": [],
            "dependency_files": [],
            "readme_candidates": [],
            "readme_excerpt_file": None,
            "repo_history_candidates_file": None,
            "history_candidate_count": 0,
        }
        if repo_status not in {"reused", "cloned", "archived"} or repo_dir is None or not repo_dir.exists():
            self._write_json(workspace_dir / "repo_file_index.json", payload)
            return payload

        files: List[str] = []
        skipped_dirs = {
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "node_modules",
            ".venv",
            "venv",
            "env",
            "build",
            "dist",
            ".idea",
            ".vscode",
        }
        truncated = False
        for path in sorted(repo_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repo_dir).as_posix()
            if any(part in skipped_dirs for part in relative.split("/")):
                continue
            files.append(relative)
            if len(files) >= 300:
                truncated = True
                break

        readme_candidates = [item for item in files if item.lower().split("/")[-1].startswith("readme")]
        dependency_files = [
            item
            for item in files
            if item.lower().split("/")[-1]
            in {"requirements.txt", "environment.yml", "environment.yaml", "pyproject.toml", "setup.py", "pdm.lock", "poetry.lock"}
        ][:24]
        entrypoint_candidates = self._rank_repo_entrypoints(files=files, hints=hints)
        readme_excerpt_file = self._write_repo_readme_excerpt(
            workspace_dir=workspace_dir,
            repo_dir=repo_dir,
            readme_candidates=readme_candidates,
        )
        repo_history_candidates_file, history_candidate_count = self._write_repo_history_candidates(
            workspace_dir=workspace_dir,
            repo_dir=repo_dir,
            readme_candidates=readme_candidates,
            files=files,
        )
        payload.update(
            {
                "status": "indexed",
                "indexed_file_count": len(files),
                "file_count_truncated": truncated,
                "files": files,
                "entrypoint_candidates": entrypoint_candidates,
                "dependency_files": dependency_files,
                "readme_candidates": readme_candidates[:12],
                "readme_excerpt_file": readme_excerpt_file,
                "repo_history_candidates_file": repo_history_candidates_file,
                "history_candidate_count": history_candidate_count,
            }
        )
        self._write_json(workspace_dir / "repo_file_index.json", payload)
        return payload

    def _rank_repo_entrypoints(self, *, files: List[str], hints: List[str]) -> List[Dict[str, Any]]:
        normalized_hints = [self._normalize_hint_token(item) for item in hints if self._normalize_hint_token(item)]
        candidates: List[Dict[str, Any]] = []
        for relative in files:
            lowered = relative.lower()
            file_name = lowered.split("/")[-1]
            if not (
                lowered.endswith(".py")
                or lowered.endswith(".ipynb")
                or file_name in {"makefile", "readme.md"}
            ):
                continue
            score = 0
            if lowered.endswith(".py"):
                score += 2
            if lowered.endswith(".ipynb"):
                score += 1
            for token in ("train", "eval", "test", "infer", "predict", "main", "run", "demo", "finetune"):
                if token in lowered:
                    score += 2
            for hint in normalized_hints:
                if hint and hint in lowered:
                    score += 3
            if score <= 0:
                continue
            candidates.append({"path": relative, "score": score})
        candidates.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
        return candidates[:24]

    @staticmethod
    def _normalize_hint_token(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9._/-]+", " ", text)
        token = max((item.strip() for item in text.split()), key=len, default="")
        return token[:80]

    def _write_repo_readme_excerpt(self, *, workspace_dir: Path, repo_dir: Path, readme_candidates: List[str]) -> Optional[str]:
        if not readme_candidates:
            return None
        for relative in readme_candidates[:3]:
            try:
                content = (repo_dir / relative).read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not content:
                continue
            excerpt = content[:16000].strip()
            file_name = "repo_readme_excerpt.md"
            self._write_text(workspace_dir / file_name, excerpt + ("\n" if not excerpt.endswith("\n") else ""))
            return file_name
        return None

    def _write_repo_history_candidates(
        self,
        *,
        workspace_dir: Path,
        repo_dir: Path,
        readme_candidates: List[str],
        files: List[str],
    ) -> tuple[Optional[str], int]:
        file_name = "repo_history_url_candidates.json"
        payload: Dict[str, Any] = {
            "status": "unavailable",
            "repo_dir": str(repo_dir),
            "source_files_scanned": [],
            "candidates": [],
        }
        git_path = shutil.which("git")
        if not git_path:
            payload["status"] = "git_unavailable"
            self._write_json(workspace_dir / file_name, payload)
            return file_name, 0
        if not (repo_dir / ".git").exists():
            payload["status"] = "no_git_metadata"
            self._write_json(workspace_dir / file_name, payload)
            return file_name, 0

        source_files: List[str] = []
        for relative in readme_candidates[:6]:
            if relative not in source_files:
                source_files.append(relative)
        for relative in files:
            lowered = relative.lower()
            if lowered.endswith(".ipynb") or lowered.endswith(".sh"):
                if relative not in source_files:
                    source_files.append(relative)
            if len(source_files) >= 12:
                break
        payload["source_files_scanned"] = list(source_files)
        if not source_files:
            payload["status"] = "no_candidate_files"
            self._write_json(workspace_dir / file_name, payload)
            return file_name, 0

        history_rows: List[Dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str, str]] = set()
        for relative in source_files:
            rows = self._extract_history_url_rows(git_path=git_path, repo_dir=repo_dir, relative_path=relative)
            for row in rows:
                key = (
                    str(row.get("relative_path") or ""),
                    str(row.get("commit") or ""),
                    str(row.get("change_kind") or ""),
                    str(row.get("url") or ""),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                history_rows.append(row)

        payload["status"] = "indexed" if history_rows else "empty"
        payload["candidates"] = history_rows[:80]
        self._write_json(workspace_dir / file_name, payload)
        return file_name, len(payload["candidates"])

    def _extract_history_url_rows(self, *, git_path: str, repo_dir: Path, relative_path: str) -> List[Dict[str, Any]]:
        try:
            completed = subprocess.run(
                [
                    git_path,
                    "-C",
                    str(repo_dir),
                    "log",
                    "--follow",
                    "--date=short",
                    f"--format={_HISTORY_DELIM}%H%x1f%ad%x1f%s",
                    "-p",
                    "--",
                    relative_path,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception:
            return []
        if completed.returncode != 0:
            return []

        rows: List[Dict[str, Any]] = []
        commit = ""
        commit_date = ""
        commit_subject = ""
        for raw_line in str(completed.stdout or "").splitlines():
            line = raw_line.rstrip("\n")
            if line.startswith(_HISTORY_DELIM):
                meta = line[len(_HISTORY_DELIM) :].split("\x1f", 2)
                commit = meta[0] if len(meta) > 0 else ""
                commit_date = meta[1] if len(meta) > 1 else ""
                commit_subject = meta[2] if len(meta) > 2 else ""
                continue
            if line.startswith(("+++", "---")):
                continue
            change_kind = ""
            if line.startswith("+"):
                change_kind = "added"
            elif line.startswith("-"):
                change_kind = "removed"
            if not change_kind:
                continue
            text = line[1:].strip()
            for match in _URL_RE.findall(text):
                url = match.rstrip(").,;")
                if not url:
                    continue
                rows.append(
                    {
                        "relative_path": relative_path,
                        "commit": commit[:12],
                        "commit_date": commit_date,
                        "commit_subject": commit_subject,
                        "change_kind": change_kind,
                        "url": url,
                        "host": self._host_from_url(url),
                        "artifact_name": self._artifact_name_from_url(url),
                        "line_excerpt": text[:400],
                    }
                )
        return rows

    @staticmethod
    def _host_from_url(value: str) -> str:
        match = re.match(r"^https?://([^/]+)", str(value or "").strip(), flags=re.IGNORECASE)
        return str(match.group(1) or "").lower() if match else ""

    @staticmethod
    def _artifact_name_from_url(value: str) -> str:
        text = str(value or "").strip().rstrip("/")
        if not text:
            return ""
        return text.split("/")[-1].split("?", 1)[0]

    def _merge_workspace_manifest_repo(
        self,
        *,
        workspace_dir: Path,
        repo_manifest: Dict[str, Any],
        repo_index: Dict[str, Any],
    ) -> None:
        manifest_path = workspace_dir / "workspace_adapter_manifest.json"
        manifest = self._read_json(manifest_path)
        if not manifest:
            return
        manifest["repo"] = repo_manifest
        manifest["repo_index_file"] = str(repo_index.get("repo_file_index_file") or "repo_file_index.json")
        self._write_json(manifest_path, manifest)

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        if not Path(path).is_file():
            return {}
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload or {}) if isinstance(payload, dict) else {}

    @staticmethod
    def _paper_metadata_payload(paper: Paper) -> Dict[str, Any]:
        return {
            "id": int(paper.id),
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": list(paper.authors or []),
            "year": paper.year,
            "venue": paper.venue,
            "journal": paper.journal,
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "url": paper.url,
            "pdf_url": paper.pdf_url,
            "arxiv_url": paper.arxiv_url,
            "fields_of_study": list(paper.fields_of_study or []),
        }

    @staticmethod
    def _safe_intake_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        next_payload = dict(payload or {})
        if "paper_markdown" in next_payload:
            next_payload["paper_markdown"] = None
        if "paper_markdown_spans" in next_payload:
            next_payload["paper_markdown_spans"] = None
        return next_payload

    @staticmethod
    def _build_workspace_readme(
        *,
        paper: Paper,
        summary: Dict[str, Any],
        experiment_spec: Dict[str, Any],
    ) -> str:
        task = dict(experiment_spec.get("task") or {})
        sources = dict(experiment_spec.get("sources") or {})
        lines = [
            f"# {paper.title}",
            "",
            "## Workspace Assets",
            "",
            "- `paper_intake_markdown.md`: local PDF -> markdown output used for the intake LLM.",
            "- `paper_intake_payload.json`: metadata and context summary sent into the intake pipeline.",
            "- `paper_intake_result.json`: structured JSON returned by the intake LLM.",
            "- `experiment_spec.json`: execution-oriented spec consumed by CodeLab runs.",
            "- `workspace_adapter_manifest.json`: repo/materialization status for this workspace.",
            "- `repo_reference.json`: resolved repo acquisition result.",
            "- `repo_file_index.json`: indexed repo files, dependency files, and entrypoint candidates.",
            "- `repo_history_url_candidates.json`: official repo history candidate URLs extracted from commit diffs when git history is available.",
            "",
            "## Resolved Signals",
            "",
            f"- Execution mode: {summary.get('execution_mode') or 'unknown'}",
            f"- Task: {task.get('task_type') or task.get('problem_statement') or 'unknown'}",
            f"- Repo URLs: {', '.join(list(sources.get('repo_urls') or [])[:3]) or 'none'}",
            f"- Dataset URLs: {', '.join(list(sources.get('dataset_urls') or [])[:3]) or 'none'}",
            "",
            "## Notes",
            "",
            "- Notebook sandbox blocks `os`, `pathlib`, `subprocess`, and `open` in user code.",
            "- Run templates are therefore generated as sandbox-safe Python and should set `run_metrics` and `run_artifacts`.",
            "- Repo acquisition is best-effort and is treated as reference material, not as a hard dependency for the baseline run.",
        ]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ""), encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        PaperExperimentAdapterService._write_text(
            path,
            json.dumps(payload or {}, ensure_ascii=False, indent=2, default=str),
        )
