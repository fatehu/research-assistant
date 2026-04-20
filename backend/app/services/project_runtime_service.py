from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

from app.config import settings


_MAX_GENERATED_FILE_BYTES = 256_000
_GENERATED_FILE_BASE_DIRS = {"executions", "generated", "tmp"}
_GENERATED_REPO_IMPORT_SHIM_MARKER = "# project-runtime: repo-import-shim"


RUNTIME_TYPES = {
    "devcontainer",
    "docker_compose",
    "dockerfile",
    "repo2docker",
    "papermill",
    "plain-python",
}


_SKIPPED_DIRS = {
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
}


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return ""
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _safe_slug(value: Any, fallback: str = "execution") -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
    return (text or fallback)[:80]


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, indent=2, default=str)


def _is_safe_generated_file_path(value: Any) -> bool:
    normalized = _normalize_relative_path(value)
    if not normalized:
        return False
    first = normalized.split("/", 1)[0]
    return first in _GENERATED_FILE_BASE_DIRS


def _guess_expected_kind_from_path(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.endswith((".h5", ".hdf5")):
        return "hdf5"
    if text.endswith(".zip"):
        return "zip"
    if text.endswith(".json"):
        return "json"
    if text.endswith((".txt", ".md", ".csv", ".tsv")):
        return "text"
    return "auto"


def _classify_magic_bytes(head_bytes: bytes, content_type: str) -> str:
    normalized_type = str(content_type or "").lower()
    if head_bytes.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5"
    if head_bytes.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head_bytes.startswith(b"\x1f\x8b"):
        return "gzip"
    if "json" in normalized_type:
        return "json"
    if "html" in normalized_type:
        return "html"
    if "text/" in normalized_type:
        return "text"
    if head_bytes.startswith((b"{", b"[")):
        return "json"
    if head_bytes.startswith((b"<!DOCTYPE html", b"<html", b"<HTML")):
        return "html"
    try:
        decoded = head_bytes.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    if decoded and all((ord(ch) >= 32 or ch in "\r\n\t") for ch in decoded):
        return "text"
    return "binary" if head_bytes else "unknown"


@dataclass
class ProjectRuntimeExecutionRecord:
    execution_id: str
    project_id: int
    workspace_id: int
    workspace_dir: Path
    spec: Dict[str, Any]
    status: str = "pending"
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = field(default=None, repr=False, compare=False)
    task: Optional[asyncio.Task[None]] = field(default=None, repr=False, compare=False)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "runtime_type": str(self.spec.get("runtime_type") or ""),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class ProjectRuntimeExecutionManager:
    """Small in-process executor for project runtime jobs.

    It intentionally persists state files under the project workspace, so the
    UI/skill can recover the last known state even if the API process reloads.
    """

    def __init__(self) -> None:
        self._records: Dict[str, ProjectRuntimeExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        project_id: int,
        workspace_id: int,
        workspace_dir: Path,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        execution_id = str(spec.get("execution_id") or "").strip()
        if not execution_id:
            raise ValueError("execution_id is required")

        async with self._lock:
            existing = self._records.get(execution_id)
            if existing and existing.status in {"pending", "running"}:
                return existing.snapshot()

            record = ProjectRuntimeExecutionRecord(
                execution_id=execution_id,
                project_id=int(project_id),
                workspace_id=int(workspace_id),
                workspace_dir=workspace_dir,
                spec=dict(spec),
            )
            self._records[execution_id] = record

        record.task = asyncio.create_task(self._run(record))
        return record.snapshot()

    async def get(
        self,
        *,
        execution_id: str,
        project_id: Optional[int] = None,
        include_result: bool = False,
    ) -> Optional[Dict[str, Any]]:
        normalized = str(execution_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized)
            if record and project_id is not None and int(record.project_id) != int(project_id):
                return None
            snapshot = record.snapshot() if record else None
            workspace_dir = record.workspace_dir if record else None

        if snapshot is None:
            return None
        if include_result and workspace_dir is not None:
            result = ProjectRuntimeService.read_execution_result_file(
                workspace_dir=workspace_dir,
                execution_id=normalized,
                max_log_chars=20000,
            )
            snapshot["result"] = result
        return snapshot

    async def cancel(self, *, execution_id: str, project_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        normalized = str(execution_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None
            if project_id is not None and int(record.project_id) != int(project_id):
                return None
            process = record.process
            record.status = "cancelled"

        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        ProjectRuntimeService.write_execution_result_file(
            workspace_dir=record.workspace_dir,
            execution_id=normalized,
            payload={
                "execution_id": normalized,
                "status": "cancelled",
                "success": False,
                "cancelled_at": _utc_now(),
                "message": "Execution cancellation was requested.",
            },
        )
        return record.snapshot()

    async def _run(self, record: ProjectRuntimeExecutionRecord) -> None:
        record.status = "running"
        record.started_at = _utc_now()
        service = ProjectRuntimeService()
        try:
            result = await service._run_execution_spec(
                workspace_dir=record.workspace_dir,
                spec=record.spec,
                record=record,
            )
            record.status = str(result.get("status") or ("completed" if result.get("success") else "failed"))
        except Exception as exc:  # noqa: BLE001 - execution failures must be persisted for the skill.
            record.status = "failed"
            result = {
                "execution_id": record.execution_id,
                "status": "failed",
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            ProjectRuntimeService.write_execution_result_file(
                workspace_dir=record.workspace_dir,
                execution_id=record.execution_id,
                payload=result,
            )
        finally:
            record.completed_at = _utc_now()


_EXECUTION_MANAGER = ProjectRuntimeExecutionManager()


class ProjectRuntimeWorkerClient:
    """HTTP client for the dedicated runtime worker.

    The backend owns project state and specs; the worker owns environment-heavy
    execution. If the worker is enabled but unavailable, execution should be
    reported as blocked instead of silently falling back to a weaker path.
    """

    def __init__(self) -> None:
        self.base_url = str(getattr(settings, "project_runtime_worker_url", "") or "").rstrip("/")
        self.token = str(getattr(settings, "project_runtime_worker_token", "") or "")
        self.timeout_seconds = float(getattr(settings, "project_runtime_worker_timeout_seconds", 30) or 30)

    @staticmethod
    def enabled() -> bool:
        return bool(getattr(settings, "project_runtime_worker_enabled", False))

    def _headers(self) -> Dict[str, str]:
        return {"X-Runtime-Worker-Token": self.token} if self.token else {}

    async def tools(self) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("project runtime worker url is empty")
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=3.0)) as client:
            response = await client.get(f"{self.base_url}/tools", headers=self._headers())
            response.raise_for_status()
            return dict(response.json() or {})

    async def start(
        self,
        *,
        project_id: int,
        workspace_id: int,
        workspace_dir: Path,
        execution_id: str,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("project runtime worker url is empty")
        payload = {
            "project_id": int(project_id),
            "workspace_id": int(workspace_id),
            "workspace_dir": str(Path(workspace_dir)),
            "execution_id": str(execution_id),
            "execution_spec": dict(spec or {}),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=3.0)) as client:
            response = await client.post(f"{self.base_url}/executions/start", json=payload, headers=self._headers())
            response.raise_for_status()
            return dict(response.json() or {})

    async def get(
        self,
        *,
        project_id: int,
        workspace_dir: Path,
        execution_id: str,
        include_logs: bool,
        max_log_chars: int,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("project runtime worker url is empty")
        params = {
            "project_id": int(project_id),
            "workspace_dir": str(Path(workspace_dir)),
            "include_logs": bool(include_logs),
            "max_log_chars": int(max_log_chars),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=3.0)) as client:
            response = await client.get(
                f"{self.base_url}/executions/{_safe_slug(execution_id)}",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return dict(response.json() or {})

    async def cancel(
        self,
        *,
        project_id: int,
        workspace_dir: Optional[Path],
        execution_id: str,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("project runtime worker url is empty")
        payload = {
            "project_id": int(project_id),
            "workspace_dir": str(Path(workspace_dir)) if workspace_dir is not None else "",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=3.0)) as client:
            response = await client.post(
                f"{self.base_url}/executions/{_safe_slug(execution_id)}/cancel",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            return dict(response.json() or {})


class ProjectRuntimeService:
    """Runtime adapter primitives for paper reproduction projects.

    The service does not decide the research workflow. It only exposes stable
    capabilities for skills/tools: inspect runtime candidates, archive an
    execution spec, and execute explicit specs when the provider is available.
    """

    def inspect(
        self,
        *,
        workspace_dir: Path,
        project_id: int,
        workspace_id: int,
        notebook_id: str = "",
        availability: Optional[Dict[str, Any]] = None,
        runtime_worker_available: bool = False,
        runtime_worker_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        workspace_dir = Path(workspace_dir)
        repo_dir = workspace_dir / "paper_repo"
        repo_root = self.resolve_repo_root(workspace_dir)
        files = self._collect_repo_files(repo_root)
        availability = dict(availability or self.tool_availability())
        candidates = self._build_candidates(
            workspace_dir=workspace_dir,
            repo_root=repo_root,
            files=files,
            availability=availability,
            runtime_worker_available=runtime_worker_available,
        )
        return {
            "schema_version": "project_runtime_inspection_v1",
            "project_id": int(project_id),
            "workspace_id": int(workspace_id),
            "notebook_id": str(notebook_id or ""),
            "workspace_root": "project_workspace",
            "repo": {
                "available": bool(repo_dir.is_dir()),
                "source_relative_path": "repo/source",
                "detected_root_relative_path": self._to_workspace_relative(workspace_dir, repo_root),
                "file_count": len(files),
                "file_count_truncated": len(files) >= 1000,
            },
            "tool_availability": availability,
            "runtime_worker": runtime_worker_status
            or {
                "enabled": ProjectRuntimeWorkerClient.enabled(),
                "available": bool(runtime_worker_available),
            },
            "runtime_candidates": candidates,
            "execution_artifacts": {
                "root_relative_path": "executions",
                "spec_pattern": "executions/{execution_id}/execution_spec.json",
                "result_pattern": "executions/{execution_id}/execution_result.json",
                "log_pattern": "executions/{execution_id}/execution.log",
            },
        }

    async def inspect_runtime(
        self,
        *,
        workspace_dir: Path,
        project_id: int,
        workspace_id: int,
        notebook_id: str = "",
    ) -> Dict[str, Any]:
        availability = self.tool_availability()
        worker_available = False
        worker_status: Dict[str, Any] = {
            "enabled": ProjectRuntimeWorkerClient.enabled(),
            "available": False,
        }
        if ProjectRuntimeWorkerClient.enabled():
            try:
                worker_payload = await ProjectRuntimeWorkerClient().tools()
                worker_availability = dict(worker_payload.get("tool_availability") or {})
                if worker_availability:
                    availability = worker_availability
                worker_available = True
                worker_status = {
                    "enabled": True,
                    "available": True,
                    "base_url": str(getattr(settings, "project_runtime_worker_url", "") or ""),
                    "tool_source": "runtime-worker",
                    "environment": dict(worker_payload.get("environment") or {}),
                }
            except Exception as exc:  # noqa: BLE001 - surfaced as runtime blocker, not hidden fallback.
                availability = {
                    key: {
                        **dict(value or {}),
                        "available": False,
                        "command": None,
                    }
                    for key, value in availability.items()
                }
                worker_status = {
                    "enabled": True,
                    "available": False,
                    "base_url": str(getattr(settings, "project_runtime_worker_url", "") or ""),
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return self.inspect(
            workspace_dir=workspace_dir,
            project_id=project_id,
            workspace_id=workspace_id,
            notebook_id=notebook_id,
            availability=availability,
            runtime_worker_available=worker_available,
            runtime_worker_status=worker_status,
        )

    @staticmethod
    def tool_availability() -> Dict[str, Any]:
        docker_path = shutil.which("docker")
        return {
            "docker": {"available": bool(docker_path), "command": docker_path},
            "docker_compose": {
                "available": bool(docker_path),
                "command": "docker compose" if docker_path else None,
            },
            "repo2docker": {
                "available": bool(shutil.which("repo2docker")),
                "command": shutil.which("repo2docker"),
            },
            "papermill": {
                "available": bool(shutil.which("papermill")),
                "command": shutil.which("papermill"),
            },
            "devcontainer": {
                "available": bool(shutil.which("devcontainer")),
                "command": shutil.which("devcontainer"),
            },
            "python": {
                "available": bool(shutil.which("python") or shutil.which("python3")),
                "command": shutil.which("python") or shutil.which("python3"),
            },
        }

    @staticmethod
    def resolve_repo_root(workspace_dir: Path) -> Path:
        repo_dir = Path(workspace_dir) / "paper_repo"
        if not repo_dir.is_dir():
            return repo_dir
        direct_signals = [
            ".devcontainer/devcontainer.json",
            "Dockerfile",
            "docker-compose.yml",
            "compose.yml",
            "requirements.txt",
            "pyproject.toml",
            "setup.py",
            "environment.yml",
            "environment.yaml",
        ]
        if any((repo_dir / item).exists() for item in direct_signals):
            return repo_dir
        root_files = [
            item
            for item in repo_dir.iterdir()
            if item.is_file() and item.name not in _SKIPPED_DIRS
        ]
        if root_files:
            return repo_dir
        children = [item for item in repo_dir.iterdir() if item.is_dir() and item.name not in _SKIPPED_DIRS]
        if len(children) == 1:
            return children[0]
        return repo_dir

    @staticmethod
    def execution_dir(workspace_dir: Path, execution_id: str) -> Path:
        normalized = _safe_slug(execution_id)
        return Path(workspace_dir) / "executions" / normalized

    def write_execution_spec(
        self,
        *,
        workspace_dir: Path,
        project_id: int,
        workspace_id: int,
        notebook_id: str,
        execution_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = dict(execution_spec or {})
        runtime_type = str(payload.get("runtime_type") or "").strip()
        if runtime_type not in RUNTIME_TYPES:
            raise ValueError(f"runtime_type must be one of {sorted(RUNTIME_TYPES)}, got `{runtime_type}`")
        payload = self._normalize_execution_spec_payload(workspace_dir=workspace_dir, payload=payload)

        execution_id = _safe_slug(
            payload.get("execution_id")
            or payload.get("draft_id")
            or payload.get("label")
            or f"{runtime_type}-{uuid.uuid4().hex[:8]}"
        )
        if not execution_id:
            execution_id = f"{runtime_type}-{uuid.uuid4().hex[:8]}"

        payload.update(
            {
                "schema_version": str(payload.get("schema_version") or "project_execution_spec_v1"),
                "execution_id": execution_id,
                "project_id": int(project_id),
                "workspace_id": int(workspace_id),
                "notebook_id": str(notebook_id or ""),
                "workspace_root": "project_workspace",
                "repo_root_relative_path": str(payload.get("repo_root_relative_path") or "repo/source"),
                "created_at": str(payload.get("created_at") or _utc_now()),
            }
        )
        validation = self.validate_execution_spec(payload, workspace_dir=workspace_dir)
        if not validation.get("valid"):
            raise ValueError("; ".join(str(item) for item in list(validation.get("errors") or [])) or "invalid execution spec")
        payload["validation"] = validation

        target_dir = self.execution_dir(workspace_dir, execution_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        spec_path = target_dir / "execution_spec.json"
        spec_path.write_text(_json_dumps(payload), encoding="utf-8")
        return {
            "execution_id": execution_id,
            "relative_path": f"executions/{execution_id}/execution_spec.json",
            "saved": True,
            "content": payload,
        }

    def _normalize_execution_spec_payload(self, *, workspace_dir: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload or {})
        detected_repo_root = self._to_workspace_relative(workspace_dir, self.resolve_repo_root(workspace_dir)) or "repo/source"
        raw_repo_root = _normalize_relative_path(normalized.get("repo_root_relative_path") or "")
        raw_cwd = _normalize_relative_path(normalized.get("cwd") or "")

        if not raw_repo_root or raw_repo_root == "repo/source":
            normalized["repo_root_relative_path"] = detected_repo_root
        if not raw_cwd or raw_cwd == "repo/source":
            normalized["cwd"] = detected_repo_root

        raw_preflight_checks = normalized.get("preflight_checks")
        if isinstance(raw_preflight_checks, dict):
            normalized_checks: List[Dict[str, Any]] = []
            for key, value in raw_preflight_checks.items():
                name = str(key or "").strip()
                if not name:
                    continue
                if isinstance(value, dict):
                    item = dict(value)
                    item["name"] = str(item.get("name") or name).strip() or name
                else:
                    item = {"name": name}
                    if isinstance(value, bool):
                        item["ok"] = bool(value)
                        item["status"] = "passed" if bool(value) else "failed"
                    elif value is not None:
                        item["status"] = str(value).strip() or "pending"
                if "required" not in item:
                    item["required"] = True
                normalized_checks.append(item)
            normalized["preflight_checks"] = normalized_checks

        if self._looks_like_env_check_spec(normalized):
            normalized["repo_root_relative_path"] = detected_repo_root
            normalized["cwd"] = detected_repo_root
            normalized["command"] = self._runtime_env_check_command()
        return normalized

    @staticmethod
    def _looks_like_env_check_spec(payload: Dict[str, Any]) -> bool:
        identifiers = " ".join(
            str(payload.get(key) or "").strip().lower()
            for key in ("execution_id", "draft_id")
        )
        if "env_check" in identifiers:
            return True
        command = list(payload.get("command") or [])
        if len(command) >= 3 and str(command[0]).startswith("python") and str(command[1]) == "-c":
            command_text = str(command[2] or "")
            if "pkg_resources" in command_text or "packages =" in command_text:
                return True
        return False

    @staticmethod
    def _runtime_env_check_command() -> List[str]:
        return [
            "python",
            "/app/.agents/skills/paper-reproduction/scripts/check_runtime_environment.py",
            "--json",
        ]

    def read_execution_spec(self, *, workspace_dir: Path, execution_id: str) -> Dict[str, Any]:
        normalized = _safe_slug(execution_id)
        path = self.execution_dir(workspace_dir, normalized) / "execution_spec.json"
        if not path.is_file():
            raise FileNotFoundError(f"execution spec not found: executions/{normalized}/execution_spec.json")
        return json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def read_execution_result_file(
        cls,
        *,
        workspace_dir: Path,
        execution_id: str,
        max_log_chars: int = 20000,
    ) -> Dict[str, Any]:
        normalized = _safe_slug(execution_id)
        root = cls.execution_dir(workspace_dir, normalized)
        result_path = root / "execution_result.json"
        log_path = root / "execution.log"
        payload: Dict[str, Any] = {
            "execution_id": normalized,
            "result_exists": result_path.is_file(),
            "log_exists": log_path.is_file(),
        }
        if result_path.is_file():
            try:
                payload.update(json.loads(result_path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                payload["result_parse_error"] = f"{type(exc).__name__}: {exc}"
        if log_path.is_file():
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            max_chars = max(int(max_log_chars or 0), 0)
            payload["log"] = content[-max_chars:] if max_chars else ""
            payload["log_truncated"] = bool(max_chars and len(content) > max_chars)
            payload["log_total_chars"] = len(content)
        return payload

    @classmethod
    def write_execution_result_file(
        cls,
        *,
        workspace_dir: Path,
        execution_id: str,
        payload: Dict[str, Any],
    ) -> None:
        normalized = _safe_slug(execution_id)
        root = cls.execution_dir(workspace_dir, normalized)
        root.mkdir(parents=True, exist_ok=True)
        (root / "execution_result.json").write_text(_json_dumps(payload), encoding="utf-8")

    @staticmethod
    def _normalize_external_dependency(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = dict(entry or {})
        kind = str(payload.get("kind") or "url").strip().lower() or "url"
        if kind not in {"url", "repo"}:
            return None
        target = str(payload.get("target") or payload.get("url") or payload.get("repo_url") or "").strip()
        if not target:
            return None
        name = str(payload.get("name") or "").strip() or target
        expected_kind = str(payload.get("expected_kind") or _guess_expected_kind_from_path(target)).strip().lower() or "auto"
        return {
            "name": name[:200],
            "kind": kind,
            "target": target,
            "expected_kind": expected_kind,
            "required": bool(payload.get("required", True)),
            "source": str(payload.get("source") or "official").strip().lower() or "official",
        }

    @classmethod
    def _derive_external_dependencies_from_spec(cls, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        derived: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        explicit = spec.get("external_dependencies")
        if isinstance(explicit, list):
            for item in explicit:
                if not isinstance(item, dict):
                    continue
                normalized = cls._normalize_external_dependency(item)
                if normalized is None:
                    continue
                key = (str(normalized["kind"]), str(normalized["target"]))
                if key not in seen:
                    seen.add(key)
                    derived.append(normalized)

        command = spec.get("command")
        if isinstance(command, list) and command:
            command_items = [str(item or "").strip() for item in command if str(item or "").strip()]
            output_hint = ""
            for index, token in enumerate(command_items[:-1]):
                if token in {"-o", "--output"}:
                    output_hint = command_items[index + 1]
                    break
            is_git_command = bool(command_items and command_items[0] == "git" and any(item in {"clone", "ls-remote", "fetch"} for item in command_items[1:3]))
            is_download_command = bool(command_items and command_items[0] in {"curl", "wget"})
            for index, token in enumerate(command_items):
                if not token.startswith(("http://", "https://")):
                    continue
                kind = "repo" if is_git_command else "url"
                expected_kind = "auto"
                if kind == "url":
                    expected_kind = _guess_expected_kind_from_path(output_hint or token)
                normalized = cls._normalize_external_dependency(
                    {
                        "name": Path(output_hint).name if output_hint else f"command-dependency-{index + 1}",
                        "kind": kind,
                        "target": token,
                        "expected_kind": expected_kind,
                        "required": True if is_download_command else False,
                        "source": "official",
                    }
                )
                if normalized is None:
                    continue
                key = (str(normalized["kind"]), str(normalized["target"]))
                if key not in seen:
                    seen.add(key)
                    derived.append(normalized)
        return derived

    async def _probe_url_dependency(self, dependency: Dict[str, Any]) -> Dict[str, Any]:
        target = str(dependency.get("target") or "").strip()
        expected_kind = str(dependency.get("expected_kind") or "auto").strip().lower() or "auto"
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                **dependency,
                "status": "failed",
                "ok": False,
                "diagnosis": "invalid_url",
            }

        headers = {
            "User-Agent": "Mozilla/5.0 (project-runtime-preflight)",
            "Accept": "*/*",
        }
        head_status = None
        get_status = None
        final_url = target
        content_type = ""
        content_length = None
        head_bytes = b""
        request_error = None
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            try:
                head_response = await client.head(target)
                head_status = int(head_response.status_code)
                final_url = str(head_response.url)
                content_type = str(head_response.headers.get("content-type") or "")
                raw_length = str(head_response.headers.get("content-length") or "").strip()
                content_length = int(raw_length) if raw_length.isdigit() else None
            except Exception as exc:  # noqa: BLE001
                request_error = f"HEAD {type(exc).__name__}: {exc}"

            try:
                async with client.stream("GET", target, headers={"Range": "bytes=0-63"}) as response:
                    get_status = int(response.status_code)
                    final_url = str(response.url)
                    if not content_type:
                        content_type = str(response.headers.get("content-type") or "")
                    raw_length = str(response.headers.get("content-length") or "").strip()
                    if content_length is None and raw_length.isdigit():
                        content_length = int(raw_length)
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            head_bytes += chunk[: max(0, 64 - len(head_bytes))]
                        if len(head_bytes) >= 64:
                            break
            except Exception as exc:  # noqa: BLE001
                request_error = f"{request_error}; GET {type(exc).__name__}: {exc}" if request_error else f"GET {type(exc).__name__}: {exc}"

        status_code = get_status or head_status
        detected_kind = _classify_magic_bytes(head_bytes, content_type)
        downloadable = bool(head_bytes or int(content_length or 0) > 0)
        ok = bool(status_code and 200 <= status_code < 300 and downloadable)
        diagnosis = "response_ok"
        if status_code is None:
            diagnosis = "request_failed"
            ok = False
        elif status_code == 202 and not head_bytes and int(content_length or 0) == 0:
            diagnosis = "accepted_but_empty"
            ok = False
        elif status_code >= 400:
            diagnosis = f"http_{status_code}"
            ok = False
        elif int(content_length or 0) == 0 and not head_bytes:
            diagnosis = "empty_response"
            ok = False
        elif expected_kind != "auto" and expected_kind == "hdf5" and detected_kind != "hdf5":
            diagnosis = f"unexpected_content:{detected_kind}"
            ok = False
        elif expected_kind != "auto" and expected_kind == "zip" and detected_kind != "zip":
            diagnosis = f"unexpected_content:{detected_kind}"
            ok = False
        elif expected_kind != "auto" and expected_kind == "json" and detected_kind != "json":
            diagnosis = f"unexpected_content:{detected_kind}"
            ok = False
        elif expected_kind != "auto" and expected_kind == "text" and detected_kind not in {"text", "html"}:
            diagnosis = f"unexpected_content:{detected_kind}"
            ok = False

        return {
            **dependency,
            "status": "passed" if ok else "failed",
            "ok": bool(ok),
            "status_code": status_code,
            "final_url": final_url,
            "content_type": content_type or None,
            "content_length": content_length,
            "detected_kind": detected_kind,
            "diagnosis": diagnosis,
            "request_error": request_error,
        }

    async def _probe_repo_dependency(self, dependency: Dict[str, Any]) -> Dict[str, Any]:
        target = str(dependency.get("target") or "").strip()
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                **dependency,
                "status": "failed",
                "ok": False,
                "diagnosis": "invalid_repo_url",
            }

        page_ok = False
        page_error = None
        status_code = None
        final_url = target
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (project-runtime-preflight)"}) as client:
            try:
                response = await client.get(target)
                status_code = int(response.status_code)
                final_url = str(response.url)
                page_ok = 200 <= response.status_code < 400
            except Exception as exc:  # noqa: BLE001
                page_error = f"{type(exc).__name__}: {exc}"

        cloneable = False
        git_error = None
        default_branch = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "ls-remote",
                "--symref",
                target,
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                git_error = "git_ls_remote_timeout"
            else:
                if process.returncode == 0:
                    cloneable = True
                    for raw_line in stdout.decode("utf-8", errors="ignore").splitlines():
                        line = raw_line.strip()
                        if line.startswith("ref: ") and line.endswith("\tHEAD"):
                            ref = line.removeprefix("ref: ").split("\t", 1)[0].strip()
                            default_branch = ref.removeprefix("refs/heads/")
                            break
                else:
                    git_error = stderr.decode("utf-8", errors="ignore").strip() or f"git_exit_{process.returncode}"
        except FileNotFoundError:
            git_error = "git_not_available"
        except Exception as exc:  # noqa: BLE001
            git_error = f"{type(exc).__name__}: {exc}"

        ok = bool(cloneable or page_ok)
        return {
            **dependency,
            "status": "passed" if ok else "failed",
            "ok": ok,
            "status_code": status_code,
            "final_url": final_url,
            "default_branch": default_branch,
            "diagnosis": "ready" if cloneable else "repo_page_reachable_but_not_cloneable" if page_ok else "repo_unreachable",
            "page_error": page_error,
            "git_error": git_error,
        }

    async def _run_external_dependency_preflight(self, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for dependency in self._derive_external_dependencies_from_spec(spec):
            if str(dependency.get("kind") or "") == "repo":
                results.append(await self._probe_repo_dependency(dependency))
            else:
                results.append(await self._probe_url_dependency(dependency))
        return results

    def validate_execution_spec(self, spec: Dict[str, Any], *, workspace_dir: Path) -> Dict[str, Any]:
        runtime_type = str(spec.get("runtime_type") or "").strip()
        errors: List[str] = []
        warnings: List[str] = []
        if runtime_type not in RUNTIME_TYPES:
            errors.append(f"runtime_type must be one of {sorted(RUNTIME_TYPES)}")

        if runtime_type in {"plain-python", "dockerfile", "docker_compose", "repo2docker", "devcontainer"}:
            command = spec.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                errors.append("command must be a non-empty string array for this runtime_type")

        if runtime_type == "papermill":
            input_notebook = _normalize_relative_path(spec.get("input_notebook"))
            if not input_notebook:
                errors.append("input_notebook is required for papermill runtime")
            elif self.resolve_workspace_path(workspace_dir, input_notebook) is None:
                errors.append(f"input_notebook is outside workspace or invalid: {input_notebook}")

        cwd = _normalize_relative_path(spec.get("cwd") or "repo/source")
        if cwd and self.resolve_workspace_path(workspace_dir, cwd, require_exists=False) is None:
            errors.append(f"cwd is outside workspace or invalid: {cwd}")

        external_dependencies = spec.get("external_dependencies")
        if external_dependencies is not None:
            if not isinstance(external_dependencies, list):
                errors.append("external_dependencies must be a list when provided")
            else:
                for index, item in enumerate(external_dependencies):
                    if not isinstance(item, dict):
                        errors.append(f"external_dependencies[{index}] must be an object")
                        continue
                    if self._normalize_external_dependency(item) is None:
                        errors.append(f"external_dependencies[{index}] is invalid")

        generated_files = spec.get("generated_files")
        if generated_files is not None:
            if not isinstance(generated_files, list):
                errors.append("generated_files must be a list when provided")
            else:
                for index, item in enumerate(generated_files):
                    prefix = f"generated_files[{index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{prefix} must be an object")
                        continue
                    relative_path = _normalize_relative_path(item.get("relative_path") or item.get("path"))
                    content = item.get("content")
                    if not relative_path:
                        errors.append(f"{prefix}.relative_path is required")
                    elif not _is_safe_generated_file_path(relative_path):
                        errors.append(f"{prefix}.relative_path must be under one of {sorted(_GENERATED_FILE_BASE_DIRS)}")
                    elif self.resolve_workspace_path(workspace_dir, relative_path, require_exists=False) is None:
                        errors.append(f"{prefix}.relative_path is outside workspace or invalid: {relative_path}")
                    if not isinstance(content, str):
                        errors.append(f"{prefix}.content must be a string")
                    elif len(content.encode("utf-8")) > _MAX_GENERATED_FILE_BYTES:
                        errors.append(f"{prefix}.content is too large")

        preflight_checks = spec.get("preflight_checks")
        if preflight_checks is not None:
            if not isinstance(preflight_checks, list):
                errors.append("preflight_checks must be a list when provided")
            else:
                for index, item in enumerate(preflight_checks):
                    prefix = f"preflight_checks[{index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{prefix} must be an object")
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        errors.append(f"{prefix}.name is required")
                        continue
                    required = bool(item.get("required", True))
                    status = str(item.get("status") or "").strip().lower()
                    ok_value = item.get("ok")
                    installed_value = item.get("installed")
                    exists_value = item.get("exists")
                    explicit_outcome = False
                    if isinstance(ok_value, bool):
                        explicit_outcome = True
                        ok = bool(ok_value)
                    elif status in {"passed", "ok", "success", "succeeded"}:
                        explicit_outcome = True
                        ok = True
                    elif status in {"failed", "fail", "blocked", "missing", "error"}:
                        explicit_outcome = True
                        ok = False
                    elif isinstance(installed_value, bool):
                        explicit_outcome = True
                        ok = bool(installed_value)
                    elif isinstance(exists_value, bool):
                        explicit_outcome = True
                        ok = bool(exists_value)
                    else:
                        ok = True
                    if required and explicit_outcome and not ok:
                        diagnosis = str(
                            item.get("diagnosis")
                            or status
                            or ("not_installed" if isinstance(installed_value, bool) and not installed_value else "")
                            or ("missing" if isinstance(exists_value, bool) and not exists_value else "")
                            or "failed"
                        ).strip()
                        warnings.append(f"required preflight check flagged failed: {name} ({diagnosis})")

        availability = self.tool_availability()
        tool_key = {
            "devcontainer": "devcontainer",
            "docker_compose": "docker_compose",
            "dockerfile": "docker",
            "repo2docker": "repo2docker",
            "papermill": "papermill",
            "plain-python": "python",
        }.get(runtime_type)
        if tool_key and not bool(dict(availability.get(tool_key) or {}).get("available")):
            warnings.append(f"runtime tool `{tool_key}` is not available in the current backend container")

        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "runtime_type": runtime_type,
        }

    async def start_execution(
        self,
        *,
        project_id: int,
        workspace_id: int,
        workspace_dir: Path,
        execution_id: str,
    ) -> Dict[str, Any]:
        spec = self.read_execution_spec(workspace_dir=workspace_dir, execution_id=execution_id)
        validation = self.validate_execution_spec(spec, workspace_dir=workspace_dir)
        if not validation.get("valid"):
            payload = {
                "execution_id": str(spec.get("execution_id") or execution_id),
                "status": "failed",
                "success": False,
                "validation": validation,
                "error": "execution_spec_invalid",
                "completed_at": _utc_now(),
            }
            self.write_execution_result_file(
                workspace_dir=workspace_dir,
                execution_id=str(spec.get("execution_id") or execution_id),
                payload=payload,
            )
            return payload

        external_dependency_preflight = await self._run_external_dependency_preflight(spec)
        required_preflight_failures = [
            item
            for item in list(external_dependency_preflight or [])
            if bool(item.get("required", True)) and not bool(item.get("ok"))
        ]
        if required_preflight_failures:
            payload = {
                "execution_id": str(spec.get("execution_id") or execution_id),
                "runtime_type": str(spec.get("runtime_type") or ""),
                "status": "blocked",
                "success": False,
                "error": "external_dependency_preflight_failed",
                "message": "One or more required official external dependencies failed preflight.",
                "external_dependency_preflight": external_dependency_preflight,
                "completed_at": _utc_now(),
            }
            self.write_execution_result_file(
                workspace_dir=workspace_dir,
                execution_id=str(spec.get("execution_id") or execution_id),
                payload=payload,
            )
            return payload

        if ProjectRuntimeWorkerClient.enabled():
            try:
                return await ProjectRuntimeWorkerClient().start(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    workspace_dir=workspace_dir,
                    execution_id=execution_id,
                    spec=spec,
                )
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "execution_id": str(spec.get("execution_id") or execution_id),
                    "runtime_type": str(spec.get("runtime_type") or ""),
                    "status": "blocked",
                    "success": False,
                    "error": "runtime_worker_unavailable",
                    "message": f"Runtime worker is enabled but unavailable: {type(exc).__name__}: {exc}",
                    "completed_at": _utc_now(),
                }
                self.write_execution_result_file(
                    workspace_dir=workspace_dir,
                    execution_id=str(spec.get("execution_id") or execution_id),
                    payload=payload,
                )
                return payload

        runtime_type = str(spec.get("runtime_type") or "").strip()
        availability = self.tool_availability()
        tool_key = {
            "devcontainer": "devcontainer",
            "docker_compose": "docker_compose",
            "dockerfile": "docker",
            "repo2docker": "repo2docker",
            "papermill": "papermill",
            "plain-python": "python",
        }.get(runtime_type)
        if tool_key and not bool(dict(availability.get(tool_key) or {}).get("available")):
            payload = {
                "execution_id": str(spec.get("execution_id") or execution_id),
                "runtime_type": runtime_type,
                "status": "blocked",
                "success": False,
                "error": "runtime_tool_unavailable",
                "message": f"Runtime tool `{tool_key}` is not available in the current backend container.",
                "tool_availability": availability,
                "completed_at": _utc_now(),
            }
            self.write_execution_result_file(
                workspace_dir=workspace_dir,
                execution_id=str(spec.get("execution_id") or execution_id),
                payload=payload,
            )
            return payload

        if runtime_type not in {"plain-python", "papermill"}:
            payload = {
                "execution_id": str(spec.get("execution_id") or execution_id),
                "runtime_type": runtime_type,
                "status": "blocked",
                "success": False,
                "error": "runtime_provider_not_started",
                "message": (
                    "This provider is detected but not started by the in-process backend. "
                    "Use a dedicated runtime worker with Docker/devcontainer/repo2docker access."
                ),
                "completed_at": _utc_now(),
            }
            self.write_execution_result_file(
                workspace_dir=workspace_dir,
                execution_id=str(spec.get("execution_id") or execution_id),
                payload=payload,
            )
            return payload

        return await _EXECUTION_MANAGER.start(
            project_id=int(project_id),
            workspace_id=int(workspace_id),
            workspace_dir=Path(workspace_dir),
            spec=spec,
        )

    async def get_execution(
        self,
        *,
        workspace_dir: Path,
        project_id: int,
        execution_id: str,
        include_logs: bool = True,
        max_log_chars: int = 20000,
    ) -> Dict[str, Any]:
        worker_payload: Optional[Dict[str, Any]] = None
        worker_error: Optional[str] = None
        if ProjectRuntimeWorkerClient.enabled():
            try:
                worker_payload = await ProjectRuntimeWorkerClient().get(
                    project_id=project_id,
                    workspace_dir=workspace_dir,
                    execution_id=execution_id,
                    include_logs=include_logs,
                    max_log_chars=max_log_chars,
                )
            except Exception as exc:  # noqa: BLE001
                worker_error = f"{type(exc).__name__}: {exc}"

        snapshot = await _EXECUTION_MANAGER.get(
            execution_id=execution_id,
            project_id=int(project_id),
            include_result=include_logs,
        )
        result = self.read_execution_result_file(
            workspace_dir=workspace_dir,
            execution_id=execution_id,
            max_log_chars=max_log_chars,
        )
        if worker_payload is not None:
            worker_payload["result"] = result
            return worker_payload
        if snapshot is None:
            payload = {
                "execution_id": _safe_slug(execution_id),
                "status": result.get("status") or ("completed_or_unknown" if result.get("result_exists") else "unknown"),
                "result": result,
            }
            if worker_error:
                payload["worker_error"] = worker_error
            return payload
        snapshot["result"] = result
        if worker_error:
            snapshot["worker_error"] = worker_error
        return snapshot

    async def cancel_execution(
        self,
        *,
        project_id: int,
        execution_id: str,
        workspace_dir: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        if ProjectRuntimeWorkerClient.enabled():
            try:
                return await ProjectRuntimeWorkerClient().cancel(
                    project_id=project_id,
                    workspace_dir=workspace_dir,
                    execution_id=execution_id,
                )
            except Exception:
                return None
        return await _EXECUTION_MANAGER.cancel(execution_id=execution_id, project_id=int(project_id))

    async def _run_execution_spec(
        self,
        *,
        workspace_dir: Path,
        spec: Dict[str, Any],
        record: ProjectRuntimeExecutionRecord,
    ) -> Dict[str, Any]:
        runtime_type = str(spec.get("runtime_type") or "").strip()
        execution_id = str(spec.get("execution_id") or record.execution_id)
        execution_root = self.execution_dir(workspace_dir, execution_id)
        execution_root.mkdir(parents=True, exist_ok=True)
        log_path = execution_root / "execution.log"
        generated_file_paths = self._materialize_generated_files(workspace_dir=workspace_dir, spec=spec)

        if runtime_type == "papermill":
            argv = self._build_papermill_argv(workspace_dir=workspace_dir, spec=spec, execution_root=execution_root)
            cwd_path = workspace_dir
        elif runtime_type == "plain-python":
            argv = list(spec.get("command") or [])
            cwd_path = self.resolve_workspace_path(workspace_dir, spec.get("cwd") or "repo/source", require_exists=False)
            if cwd_path is None:
                raise ValueError("cwd is invalid or outside workspace")
        else:
            raise ValueError(f"runtime_type is not executable in-process: {runtime_type}")

        start = time.time()
        with log_path.open("ab") as log_handle:
            log_handle.write((f"[project-runtime] started_at={_utc_now()} runtime={runtime_type}\n").encode("utf-8"))
            log_handle.write((f"[project-runtime] cwd={cwd_path}\n").encode("utf-8"))
            log_handle.write((f"[project-runtime] argv={json.dumps(argv, ensure_ascii=False)}\n").encode("utf-8"))
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd_path),
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            record.process = process
            returncode = await process.wait()
            elapsed_ms = int((time.time() - start) * 1000)
            success = returncode == 0
            payload = {
                "execution_id": execution_id,
                "runtime_type": runtime_type,
                "status": "completed" if success else "failed",
                "success": success,
                "returncode": returncode,
                "elapsed_ms": elapsed_ms,
                "log_relative_path": f"executions/{execution_id}/execution.log",
                "generated_files": generated_file_paths,
                "completed_at": _utc_now(),
            }
            self.write_execution_result_file(workspace_dir=workspace_dir, execution_id=execution_id, payload=payload)
            return payload

    def _materialize_generated_files(self, *, workspace_dir: Path, spec: Dict[str, Any]) -> List[str]:
        generated_files = spec.get("generated_files")
        if not isinstance(generated_files, list):
            return []
        written: List[str] = []
        repo_root = self.resolve_workspace_path(
            workspace_dir,
            spec.get("repo_root_relative_path") or spec.get("cwd") or "repo/source",
            require_exists=False,
        )
        for item in generated_files:
            if not isinstance(item, dict):
                continue
            relative_path = _normalize_relative_path(item.get("relative_path") or item.get("path"))
            content = item.get("content")
            if not relative_path or not isinstance(content, str):
                continue
            if not _is_safe_generated_file_path(relative_path):
                continue
            target = self.resolve_workspace_path(workspace_dir, relative_path, require_exists=False)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            materialized_content = self._prepare_generated_file_content(
                workspace_dir=workspace_dir,
                repo_root=repo_root,
                relative_path=relative_path,
                content=content,
            )
            target.write_text(materialized_content, encoding="utf-8")
            written.append(relative_path)
        return written

    @staticmethod
    def _prepare_generated_file_content(
        *,
        workspace_dir: Path,
        repo_root: Optional[Path],
        relative_path: str,
        content: str,
    ) -> str:
        if repo_root is None:
            return content
        normalized_path = _normalize_relative_path(relative_path)
        if not normalized_path.endswith(".py"):
            return content
        if not normalized_path.startswith("executions/"):
            return content
        if _GENERATED_REPO_IMPORT_SHIM_MARKER in content:
            return content

        try:
            repo_matches_default = repo_root.resolve() == ProjectRuntimeService.resolve_repo_root(workspace_dir).resolve()
        except OSError:
            repo_matches_default = False
        if not repo_matches_default:
            return content

        shim = (
            f"{_GENERATED_REPO_IMPORT_SHIM_MARKER}\n"
            "import sys\n"
            "from pathlib import Path\n"
            "_PROJECT_RUNTIME_REPO_ROOT = Path(__file__).resolve().parents[2] / \"paper_repo\"\n"
            "if str(_PROJECT_RUNTIME_REPO_ROOT) not in sys.path:\n"
            "    sys.path.insert(0, str(_PROJECT_RUNTIME_REPO_ROOT))\n\n"
        )
        return shim + content

    def _build_papermill_argv(self, *, workspace_dir: Path, spec: Dict[str, Any], execution_root: Path) -> List[str]:
        input_rel = _normalize_relative_path(spec.get("input_notebook"))
        input_path = self.resolve_workspace_path(workspace_dir, input_rel)
        if input_path is None:
            raise ValueError(f"input_notebook is invalid: {input_rel}")
        output_path = execution_root / "output.ipynb"
        params = dict(spec.get("parameters") or {})
        params_path = execution_root / "parameters.json"
        params_path.write_text(_json_dumps(params), encoding="utf-8")
        return [
            str(shutil.which("papermill") or "papermill"),
            str(input_path),
            str(output_path),
            "-f",
            str(params_path),
        ]

    @staticmethod
    def resolve_workspace_path(workspace_dir: Path, relative_path: Any, *, require_exists: bool = True) -> Optional[Path]:
        normalized = _normalize_relative_path(relative_path)
        if not normalized:
            return None
        if normalized == "repo/source":
            candidate = ProjectRuntimeService.resolve_repo_root(workspace_dir)
        elif normalized.startswith("repo/source/"):
            candidate = ProjectRuntimeService.resolve_repo_root(workspace_dir) / normalized.removeprefix("repo/source/")
        else:
            candidate = Path(workspace_dir) / normalized
        resolved = candidate.resolve()
        root = Path(workspace_dir).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        if require_exists and not resolved.exists():
            return None
        return resolved

    def _build_candidates(
        self,
        *,
        workspace_dir: Path,
        repo_root: Path,
        files: List[str],
        availability: Dict[str, Any],
        runtime_worker_available: bool = False,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []

        def add(
            runtime_type: str,
            *,
            priority: int,
            evidence_files: Sequence[str],
            tool_key: str,
            reason: str,
            entrypoints: Optional[Sequence[str]] = None,
            extra_tool_keys: Optional[Sequence[str]] = None,
            requires_runtime_worker: bool = False,
            requires_explicit_user_confirm: bool = False,
        ) -> None:
            available = bool(dict(availability.get(tool_key) or {}).get("available"))
            blockers = []
            if not available:
                blockers.append(f"tool_missing:{tool_key}")
            required_tools = [tool_key]
            for extra_tool_key in list(extra_tool_keys or []):
                required_tools.append(extra_tool_key)
                if not bool(dict(availability.get(extra_tool_key) or {}).get("available")):
                    blockers.append(f"tool_missing:{extra_tool_key}")
            if requires_runtime_worker and not runtime_worker_available:
                blockers.append("runtime_worker_required")
            candidates.append(
                {
                    "runtime_type": runtime_type,
                    "priority": int(priority),
                    "status": "ready" if available and not blockers else "blocked",
                    "tool_key": tool_key,
                    "required_tools": required_tools,
                    "tool_available": available,
                    "requires_runtime_worker": bool(requires_runtime_worker),
                    "requires_explicit_user_confirm": bool(requires_explicit_user_confirm),
                    "evidence_files": list(evidence_files),
                    "entrypoints": list(entrypoints or []),
                    "reason": reason,
                    "blockers": blockers,
                }
            )

        has = set(files)
        devcontainer_files = [item for item in files if item == ".devcontainer/devcontainer.json"]
        if devcontainer_files:
            add(
                "devcontainer",
                priority=10,
                evidence_files=devcontainer_files,
                tool_key="devcontainer",
                reason="Repository declares a Dev Container environment.",
                extra_tool_keys=["docker"],
                requires_runtime_worker=True,
            )

        compose_files = [item for item in files if item in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}]
        if compose_files:
            add(
                "docker_compose",
                priority=20,
                evidence_files=compose_files,
                tool_key="docker_compose",
                reason="Repository declares a docker compose environment.",
                requires_runtime_worker=True,
            )

        if "Dockerfile" in has:
            add(
                "dockerfile",
                priority=30,
                evidence_files=["Dockerfile"],
                tool_key="docker",
                reason="Repository declares a Dockerfile environment.",
                requires_runtime_worker=True,
            )

        notebooks = [item for item in files if item.lower().endswith(".ipynb")][:24]
        if notebooks:
            add(
                "papermill",
                priority=40,
                evidence_files=notebooks[:8],
                tool_key="papermill",
                reason="Repository includes notebooks that can be parameterized/executed with papermill.",
                entrypoints=notebooks[:12],
            )

        repo2docker_files = [
            item
            for item in [
                "requirements.txt",
                "environment.yml",
                "environment.yaml",
                "pyproject.toml",
                "setup.py",
                "Pipfile",
                "install.R",
            ]
            if item in has
        ]
        if repo2docker_files:
            add(
                "repo2docker",
                priority=50,
                evidence_files=repo2docker_files,
                tool_key="repo2docker",
                reason="Repository has standard dependency files that repo2docker can build from.",
                extra_tool_keys=["docker"],
                requires_runtime_worker=True,
            )

        helper_stems = {"analysis", "file_io", "utils", "helper", "helpers", "conftest", "setup", "__init__"}
        python_entrypoints: List[str] = []
        for item in files:
            lowered = item.lower()
            if not lowered.endswith(".py"):
                continue
            parts = lowered.split("/")
            basename = parts[-1]
            stem = basename[:-3]
            is_named_entrypoint = any(token in stem for token in ("train", "eval", "main", "run", "demo", "test"))
            is_top_level_script = len(parts) == 1 and not basename.startswith("_") and stem not in helper_stems
            if not (is_named_entrypoint or is_top_level_script):
                continue
            python_entrypoints.append(item)
            if len(python_entrypoints) >= 12:
                break
        if python_entrypoints:
            add(
                "plain-python",
                priority=90,
                evidence_files=python_entrypoints[:6],
                tool_key="python",
                reason="Repository has Python entrypoint candidates. Use for verified repo scripts when data and dependencies are ready; smoke tests are only the lowest-risk subset.",
                entrypoints=python_entrypoints,
                requires_explicit_user_confirm=True,
            )

        candidates.sort(key=lambda item: (int(item.get("priority") or 999), str(item.get("runtime_type") or "")))
        return candidates

    @staticmethod
    def _collect_repo_files(repo_root: Path) -> List[str]:
        if not Path(repo_root).is_dir():
            return []
        files: List[str] = []
        for path in sorted(Path(repo_root).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repo_root).as_posix()
            if any(part in _SKIPPED_DIRS for part in relative.split("/")):
                continue
            files.append(relative)
            if len(files) >= 1000:
                break
        return files

    @staticmethod
    def _to_workspace_relative(workspace_dir: Path, path: Path) -> str:
        try:
            relative = Path(path).resolve().relative_to(Path(workspace_dir).resolve()).as_posix()
        except Exception:
            return ""
        if relative == "paper_repo":
            return "repo/source"
        if relative.startswith("paper_repo/"):
            return "repo/source/" + relative.removeprefix("paper_repo/")
        return relative
