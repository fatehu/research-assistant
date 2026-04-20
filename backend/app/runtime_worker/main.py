from __future__ import annotations

import asyncio
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.project_runtime_service import ProjectRuntimeService, _json_dumps, _safe_slug, _utc_now


app = FastAPI(title="Project Runtime Worker", version="0.1.0")


class RuntimeStartRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_id: int = Field(ge=1)
    workspace_dir: str = Field(min_length=1)
    execution_id: str = Field(min_length=1, max_length=120)
    execution_spec: Dict[str, Any] = Field(default_factory=dict)


class RuntimeCancelRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_dir: str = ""


@dataclass
class WorkerExecutionRecord:
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
            "worker": "runtime-worker",
        }


class RuntimeWorkerManager:
    def __init__(self) -> None:
        self._records: Dict[str, WorkerExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        project_id: int,
        workspace_id: int,
        workspace_dir: Path,
        execution_id: str,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized = _safe_slug(execution_id)
        async with self._lock:
            existing = self._records.get(normalized)
            if existing and existing.status in {"pending", "running"}:
                return existing.snapshot()
            record = WorkerExecutionRecord(
                execution_id=normalized,
                project_id=int(project_id),
                workspace_id=int(workspace_id),
                workspace_dir=workspace_dir,
                spec={**dict(spec or {}), "execution_id": normalized},
            )
            self._records[normalized] = record
        record.task = asyncio.create_task(self._run(record))
        return record.snapshot()

    async def get(
        self,
        *,
        execution_id: str,
        project_id: Optional[int],
        workspace_dir: Optional[Path],
        include_logs: bool,
        max_log_chars: int,
    ) -> Dict[str, Any]:
        normalized = _safe_slug(execution_id)
        async with self._lock:
            record = self._records.get(normalized)
            if record and project_id is not None and int(record.project_id) != int(project_id):
                raise HTTPException(status_code=404, detail="execution not found")
            snapshot = record.snapshot() if record else None
            resolved_workspace = record.workspace_dir if record else workspace_dir
        if snapshot is None:
            snapshot = {
                "execution_id": normalized,
                "status": "unknown",
                "worker": "runtime-worker",
            }
        if resolved_workspace is not None:
            snapshot["result"] = ProjectRuntimeService.read_execution_result_file(
                workspace_dir=resolved_workspace,
                execution_id=normalized,
                max_log_chars=max_log_chars if include_logs else 0,
            )
            if snapshot["status"] == "unknown":
                result_status = snapshot["result"].get("status")
                if result_status:
                    snapshot["status"] = result_status
        return snapshot

    async def cancel(
        self,
        *,
        execution_id: str,
        project_id: int,
        workspace_dir: Optional[Path],
    ) -> Dict[str, Any]:
        normalized = _safe_slug(execution_id)
        async with self._lock:
            record = self._records.get(normalized)
            if record and int(record.project_id) != int(project_id):
                raise HTTPException(status_code=404, detail="execution not found")
            if record:
                record.status = "cancelled"
                process = record.process
                resolved_workspace = record.workspace_dir
            else:
                process = None
                resolved_workspace = workspace_dir
        if process is not None and process.returncode is None:
            _terminate_process(process)
        if resolved_workspace is not None:
            ProjectRuntimeService.write_execution_result_file(
                workspace_dir=resolved_workspace,
                execution_id=normalized,
                payload={
                    "execution_id": normalized,
                    "status": "cancelled",
                    "success": False,
                    "cancelled_at": _utc_now(),
                    "message": "Execution cancellation was requested.",
                    "worker": "runtime-worker",
                },
            )
            _restore_execution_permissions(ProjectRuntimeService.execution_dir(resolved_workspace, normalized))
        return {
            "execution_id": normalized,
            "project_id": int(project_id),
            "status": "cancelled",
            "worker": "runtime-worker",
        }

    async def _run(self, record: WorkerExecutionRecord) -> None:
        record.status = "running"
        record.started_at = _utc_now()
        try:
            result = await RuntimeWorkerExecutor().run(record)
            record.status = str(result.get("status") or ("completed" if result.get("success") else "failed"))
        except Exception as exc:  # noqa: BLE001 - persisted as execution result.
            record.status = "failed"
            result = {
                "execution_id": record.execution_id,
                "runtime_type": str(record.spec.get("runtime_type") or ""),
                "status": "failed",
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "worker": "runtime-worker",
                "completed_at": _utc_now(),
            }
            ProjectRuntimeService.write_execution_result_file(
                workspace_dir=record.workspace_dir,
                execution_id=record.execution_id,
                payload=result,
            )
        finally:
            record.completed_at = _utc_now()


class RuntimeWorkerExecutor:
    def __init__(self) -> None:
        self.service = ProjectRuntimeService()

    async def run(self, record: WorkerExecutionRecord) -> Dict[str, Any]:
        spec = self.service._normalize_execution_spec_payload(
            workspace_dir=record.workspace_dir,
            payload=dict(record.spec or {}),
        )
        record.spec = spec
        runtime_type = str(spec.get("runtime_type") or "").strip()
        execution_id = record.execution_id
        execution_root = self.service.execution_dir(record.workspace_dir, execution_id)
        execution_root.mkdir(parents=True, exist_ok=True)
        log_path = execution_root / "execution.log"

        required_tool = self._required_tool(runtime_type)
        if required_tool and shutil.which(required_tool) is None:
            payload = self._blocked_payload(
                execution_id=execution_id,
                runtime_type=runtime_type,
                error="runtime_tool_unavailable",
                message=f"Runtime tool `{required_tool}` is not installed in runtime-worker.",
            )
            ProjectRuntimeService.write_execution_result_file(
                workspace_dir=record.workspace_dir,
                execution_id=execution_id,
                payload=payload,
            )
            return payload

        start = time.time()
        with log_path.open("ab") as log_handle:
            log_handle.write((f"[runtime-worker] started_at={_utc_now()} runtime={runtime_type}\n").encode("utf-8"))
            if runtime_type == "plain-python":
                return await self._run_plain_python(record, log_handle, start)
            if runtime_type == "papermill":
                return await self._run_papermill(record, log_handle, start)
            if runtime_type == "dockerfile":
                return await self._run_dockerfile(record, log_handle, start)
            if runtime_type == "repo2docker":
                return await self._run_repo2docker(record, log_handle, start)
            if runtime_type == "devcontainer":
                return await self._run_devcontainer(record, log_handle, start)
            if runtime_type == "docker_compose":
                return await self._run_docker_compose(record, log_handle, start)
        raise ValueError(f"unsupported runtime_type: {runtime_type}")

    async def _run_plain_python(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        spec = record.spec
        cwd = self.service.resolve_workspace_path(
            record.workspace_dir,
            spec.get("cwd") or "repo/source",
            require_exists=False,
        )
        if cwd is None:
            raise ValueError("cwd is invalid or outside workspace")
        generated_files = self.service._materialize_generated_files(workspace_dir=record.workspace_dir, spec=spec)
        if generated_files:
            log_handle.write((f"[runtime-worker] generated_files={json.dumps(generated_files, ensure_ascii=False)}\n").encode("utf-8"))
            log_handle.flush()
        return await self._run_argv(record, list(spec.get("command") or []), cwd, log_handle, start)

    async def _run_papermill(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        execution_root = self.service.execution_dir(record.workspace_dir, record.execution_id)
        argv = self.service._build_papermill_argv(  # Reuse the validated argv builder.
            workspace_dir=record.workspace_dir,
            spec=record.spec,
            execution_root=execution_root,
        )
        return await self._run_argv(record, argv, record.workspace_dir, log_handle, start)

    async def _run_dockerfile(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        repo_root = self._repo_root(record)
        dockerfile = self._dockerfile_path(record, repo_root)
        image_tag = self._image_tag(record, "dockerfile")
        build = ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(repo_root)]
        build_result = await self._run_argv(record, build, repo_root, log_handle, start, persist=False)
        if not build_result.get("success"):
            return self._persist(record, build_result)
        run = self._docker_run_argv(record, image_tag, list(record.spec.get("command") or []), repo_root)
        return await self._run_argv(record, run, repo_root, log_handle, start)

    async def _run_repo2docker(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        if shutil.which("docker") is None:
            payload = self._blocked_payload(
                execution_id=record.execution_id,
                runtime_type="repo2docker",
                error="runtime_tool_unavailable",
                message="Runtime tool `docker` is required for repo2docker execution.",
            )
            return self._persist(record, payload)
        repo_root = self._repo_root(record)
        image_tag = self._image_tag(record, "repo2docker")
        build = ["repo2docker", "--no-run", "--image-name", image_tag, str(repo_root)]
        build_result = await self._run_argv(record, build, repo_root, log_handle, start, persist=False)
        if not build_result.get("success"):
            return self._persist(record, build_result)
        run = self._docker_run_argv(record, image_tag, list(record.spec.get("command") or []), repo_root)
        return await self._run_argv(record, run, repo_root, log_handle, start)

    async def _run_devcontainer(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        repo_root = self._repo_root(record)
        up = ["devcontainer", "up", "--workspace-folder", str(repo_root)]
        up_result = await self._run_argv(record, up, repo_root, log_handle, start, persist=False)
        if not up_result.get("success"):
            return self._persist(record, up_result)
        exec_argv = [
            "devcontainer",
            "exec",
            "--workspace-folder",
            str(repo_root),
            *list(record.spec.get("command") or []),
        ]
        return await self._run_argv(record, exec_argv, repo_root, log_handle, start)

    async def _run_docker_compose(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        repo_root = self._repo_root(record)
        service_name = str(record.spec.get("service") or "").strip()
        if not service_name:
            raise ValueError("docker_compose execution requires `service`")
        compose_file = self._compose_file_path(record, repo_root)
        argv = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "run",
            "--rm",
            service_name,
            *list(record.spec.get("command") or []),
        ]
        return await self._run_argv(record, argv, repo_root, log_handle, start)

    async def _run_argv(
        self,
        record: WorkerExecutionRecord,
        argv: List[str],
        cwd: Path,
        log_handle: Any,
        start: float,
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        if not argv:
            raise ValueError("command argv is empty")
        log_handle.write((f"[runtime-worker] cwd={cwd}\n").encode("utf-8"))
        log_handle.write((f"[runtime-worker] argv={json.dumps(argv, ensure_ascii=False)}\n").encode("utf-8"))
        log_handle.flush()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=log_handle,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        record.process = process
        returncode = await process.wait()
        elapsed_ms = int((time.time() - start) * 1000)
        success = returncode == 0
        payload = {
            "execution_id": record.execution_id,
            "runtime_type": str(record.spec.get("runtime_type") or ""),
            "status": "completed" if success else "failed",
            "success": success,
            "returncode": returncode,
            "elapsed_ms": elapsed_ms,
            "log_relative_path": f"executions/{record.execution_id}/execution.log",
            "artifact_matches": self._collect_artifacts(record),
            "worker": "runtime-worker",
            "completed_at": _utc_now(),
        }
        return self._persist(record, payload) if persist else payload

    def _persist(self, record: WorkerExecutionRecord, payload: Dict[str, Any]) -> Dict[str, Any]:
        ProjectRuntimeService.write_execution_result_file(
            workspace_dir=record.workspace_dir,
            execution_id=record.execution_id,
            payload=payload,
        )
        _restore_execution_permissions(ProjectRuntimeService.execution_dir(record.workspace_dir, record.execution_id))
        return payload

    def _blocked_payload(self, *, execution_id: str, runtime_type: str, error: str, message: str) -> Dict[str, Any]:
        return {
            "execution_id": execution_id,
            "runtime_type": runtime_type,
            "status": "blocked",
            "success": False,
            "error": error,
            "message": message,
            "tool_availability": ProjectRuntimeService.tool_availability(),
            "worker": "runtime-worker",
            "completed_at": _utc_now(),
        }

    def _repo_root(self, record: WorkerExecutionRecord) -> Path:
        repo_root = self.service.resolve_workspace_path(
            record.workspace_dir,
            record.spec.get("repo_root_relative_path") or "repo/source",
            require_exists=True,
        )
        if repo_root is None:
            raise ValueError("repo_root_relative_path is invalid, missing, or outside workspace")
        return repo_root

    def _dockerfile_path(self, record: WorkerExecutionRecord, repo_root: Path) -> Path:
        configured = str(record.spec.get("dockerfile") or "").strip()
        if configured:
            path = self.service.resolve_workspace_path(record.workspace_dir, configured, require_exists=True)
            if path is not None:
                return path
        path = repo_root / "Dockerfile"
        if not path.is_file():
            raise ValueError("Dockerfile was not found under repo root")
        return path

    def _compose_file_path(self, record: WorkerExecutionRecord, repo_root: Path) -> Path:
        configured = str(record.spec.get("compose_file") or "").strip()
        if configured:
            path = self.service.resolve_workspace_path(record.workspace_dir, configured, require_exists=True)
            if path is not None:
                return path
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            path = repo_root / name
            if path.is_file():
                return path
        raise ValueError("compose_file was not found under repo root")

    def _docker_run_argv(self, record: WorkerExecutionRecord, image_tag: str, command: List[str], repo_root: Path) -> List[str]:
        repo_rel = repo_root.resolve().relative_to(record.workspace_dir.resolve()).as_posix()
        return [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{record.workspace_dir.resolve()}:/workspace",
            "-w",
            f"/workspace/{repo_rel}",
            image_tag,
            *command,
        ]

    @staticmethod
    def _image_tag(record: WorkerExecutionRecord, prefix: str) -> str:
        raw = f"research-paper-{prefix}-{record.project_id}-{record.workspace_id}-{record.execution_id}"
        tag = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip("-.")
        return tag[:120] or "research-paper-runtime"

    @staticmethod
    def _required_tool(runtime_type: str) -> str:
        return {
            "plain-python": shutil.which("python") and "python" or "python3",
            "papermill": "papermill",
            "dockerfile": "docker",
            "repo2docker": "repo2docker",
            "devcontainer": "devcontainer",
            "docker_compose": "docker",
        }.get(runtime_type, "")

    @staticmethod
    def environment_report() -> Dict[str, Any]:
        packages = [
            ("numpy", "numpy"),
            ("pandas", "pandas"),
            ("scipy", "scipy"),
            ("scikit-learn", "sklearn"),
            ("h5py", "h5py"),
            ("matplotlib", "matplotlib"),
            ("seaborn", "seaborn"),
            ("torch", "torch"),
            ("schedulefree", "schedulefree"),
            ("papermill", "papermill"),
            ("jupyter-repo2docker", "repo2docker"),
        ]
        package_status: Dict[str, Any] = {}
        for distribution_name, import_name in packages:
            installed = importlib.util.find_spec(import_name) is not None
            try:
                version = importlib_metadata.version(distribution_name) if installed else None
            except importlib_metadata.PackageNotFoundError:
                version = None
            package_status[distribution_name] = {
                "installed": installed,
                "import_name": import_name,
                "version": version,
            }
        return {
            "python": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
            },
            "packages": package_status,
            "cache_env": {
                "PIP_CACHE_DIR": os.getenv("PIP_CACHE_DIR", ""),
                "HF_HOME": os.getenv("HF_HOME", ""),
                "HUGGINGFACE_HUB_CACHE": os.getenv("HUGGINGFACE_HUB_CACHE", ""),
                "XDG_CACHE_HOME": os.getenv("XDG_CACHE_HOME", ""),
            },
        }

    def _collect_artifacts(self, record: WorkerExecutionRecord) -> List[str]:
        matches: List[str] = []
        for pattern in list(record.spec.get("artifact_globs") or []):
            rel = _normalize_relative_path(pattern)
            if not rel:
                continue
            for path in record.workspace_dir.glob(rel):
                if path.is_file():
                    try:
                        matches.append(path.relative_to(record.workspace_dir).as_posix())
                    except ValueError:
                        continue
                if len(matches) >= 200:
                    return matches
        return matches


_MANAGER = RuntimeWorkerManager()


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return ""
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _workspace_roots() -> List[Path]:
    raw = os.getenv("PROJECT_RUNTIME_WORKSPACE_ROOTS", "/app/uploads,/tmp")
    roots = []
    for item in raw.split(","):
        text = item.strip()
        if text:
            roots.append(Path(text).resolve())
    return roots or [Path("/app/uploads").resolve()]


def _resolve_workspace_dir(value: str) -> Path:
    path = Path(value).resolve()
    for root in _workspace_roots():
        try:
            path.relative_to(root)
            path.mkdir(parents=True, exist_ok=True)
            return path
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="workspace_dir is outside allowed runtime roots")


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def _restore_execution_permissions(root: Path) -> None:
    uid = int(os.getenv("PROJECT_RUNTIME_WORKER_APP_UID", "10001") or 10001)
    gid = int(os.getenv("PROJECT_RUNTIME_WORKER_APP_GID", "10001") or 10001)
    paths = []
    if root.name and root.parent.name == "executions":
        paths.append(root.parent)
    paths.extend([root, *root.rglob("*")])
    for path in paths:
        try:
            os.chown(path, uid, gid)
        except Exception:
            continue


async def _authorize(request: Request) -> None:
    token = os.getenv("PROJECT_RUNTIME_WORKER_TOKEN", "")
    if not token:
        return
    supplied = request.headers.get("X-Runtime-Worker-Token", "")
    if supplied != token:
        raise HTTPException(status_code=401, detail="invalid runtime worker token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    await _authorize(request)
    return await call_next(request)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "worker": "runtime-worker"}


@app.get("/tools")
async def tools() -> Dict[str, Any]:
    return {
        "worker": "runtime-worker",
        "tool_availability": ProjectRuntimeService.tool_availability(),
        "environment": RuntimeWorkerExecutor.environment_report(),
        "workspace_roots": [str(item) for item in _workspace_roots()],
    }


@app.post("/executions/start")
async def start_execution(payload: RuntimeStartRequest) -> Dict[str, Any]:
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    spec = dict(payload.execution_spec or {})
    spec["execution_id"] = _safe_slug(payload.execution_id or spec.get("execution_id"))
    spec.setdefault("project_id", int(payload.project_id))
    spec.setdefault("workspace_id", int(payload.workspace_id))
    spec.setdefault("workspace_root", "project_workspace")
    service = ProjectRuntimeService()
    spec = service._normalize_execution_spec_payload(workspace_dir=workspace_dir, payload=spec)
    validation = service.validate_execution_spec(spec, workspace_dir=workspace_dir)
    if not validation.get("valid"):
        result = {
            "execution_id": str(spec.get("execution_id") or payload.execution_id),
            "runtime_type": str(spec.get("runtime_type") or ""),
            "status": "failed",
            "success": False,
            "validation": validation,
            "error": "execution_spec_invalid",
            "worker": "runtime-worker",
            "completed_at": _utc_now(),
        }
        ProjectRuntimeService.write_execution_result_file(
            workspace_dir=workspace_dir,
            execution_id=str(spec.get("execution_id") or payload.execution_id),
            payload=result,
        )
        _restore_execution_permissions(
            ProjectRuntimeService.execution_dir(workspace_dir, str(spec.get("execution_id") or payload.execution_id))
        )
        return result
    return await _MANAGER.start(
        project_id=int(payload.project_id),
        workspace_id=int(payload.workspace_id),
        workspace_dir=workspace_dir,
        execution_id=str(spec.get("execution_id") or payload.execution_id),
        spec=spec,
    )


@app.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    project_id: Optional[int] = Query(default=None, ge=1),
    workspace_dir: str = "",
    include_logs: bool = True,
    max_log_chars: int = Query(default=20000, ge=0, le=200000),
) -> Dict[str, Any]:
    resolved_workspace = _resolve_workspace_dir(workspace_dir) if workspace_dir else None
    return await _MANAGER.get(
        execution_id=execution_id,
        project_id=project_id,
        workspace_dir=resolved_workspace,
        include_logs=include_logs,
        max_log_chars=max_log_chars,
    )


@app.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, payload: RuntimeCancelRequest) -> Dict[str, Any]:
    resolved_workspace = _resolve_workspace_dir(payload.workspace_dir) if payload.workspace_dir else None
    return await _MANAGER.cancel(
        execution_id=execution_id,
        project_id=int(payload.project_id),
        workspace_dir=resolved_workspace,
    )
