from __future__ import annotations

import asyncio
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services.project_runtime_service import ProjectRuntimeService, _json_dumps, _safe_slug, _utc_now


app = FastAPI(title="Project Runtime Worker", version="0.1.0")

CLAUDE_STREAM_PIPE_LIMIT = 8 * 1024 * 1024


def _claude_stream_heartbeat_seconds() -> float:
    raw_value = getattr(settings, "claude_code_stream_heartbeat_seconds", 15.0)
    try:
        return max(1.0, float(raw_value))
    except (TypeError, ValueError):
        return 15.0


def _claude_stream_heartbeat_payload() -> Dict[str, Any]:
    return {
        "type": "heartbeat",
        "worker": "runtime-worker",
        "timestamp": _utc_now(),
    }


class RuntimeStartRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_id: int = Field(ge=1)
    workspace_dir: str = Field(min_length=1)
    execution_id: str = Field(min_length=1, max_length=120)
    execution_spec: Dict[str, Any] = Field(default_factory=dict)


class RuntimeCancelRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_dir: str = ""


class BashRunRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_dir: str = Field(min_length=1)
    command: str = Field(min_length=1, max_length=20000)


class ClaudeRunRequest(BaseModel):
    project_id: int = Field(ge=1)
    workspace_dir: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=20000)
    continue_session: bool = False


class DocxClaudeRunRequest(BaseModel):
    docx_id: str = Field(min_length=1)
    workspace_dir: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    continue_session: bool = False


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
            if runtime_type == "claude_code":
                return await self._run_claude_code(record, log_handle, start)
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

    async def _run_claude_code(self, record: WorkerExecutionRecord, log_handle: Any, start: float) -> Dict[str, Any]:
        spec = record.spec
        repo_root = self._repo_root(record)
        execution_root = self.service.execution_dir(record.workspace_dir, record.execution_id)
        progress_path = execution_root / "progress.jsonl"
        prompt_text = self._claude_prompt_text(record)
        argv = self._claude_code_argv(record=record, repo_root=repo_root, prompt_text=prompt_text)
        env = self._claude_code_env(record)
        log_handle.write((f"[runtime-worker] cwd={repo_root}\n").encode("utf-8"))
        log_handle.write((f"[runtime-worker] argv={json.dumps(argv, ensure_ascii=False)}\n").encode("utf-8"))
        log_handle.flush()

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        record.process = process
        started_at = _utc_now()
        latest_message = "Claude Code started."
        progress_count = 0
        self._persist(
            record,
            {
                "execution_id": record.execution_id,
                "runtime_type": "claude_code",
                "status": "running",
                "started_at": started_at,
                "message": latest_message,
                "log_relative_path": f"executions/{record.execution_id}/execution.log",
                "progress_relative_path": f"executions/{record.execution_id}/progress.jsonl",
                "worker": "runtime-worker",
            },
        )

        assert process.stdout is not None
        while True:
            chunk = await process.stdout.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="ignore")
            log_handle.write(text.encode("utf-8", errors="ignore"))
            log_handle.flush()
            stripped = text.strip()
            if not stripped:
                continue
            progress_count += 1
            progress_event = _claude_progress_event(stripped)
            latest_message = str(progress_event.get("message") or latest_message).strip() or latest_message
            with progress_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(progress_event, ensure_ascii=False))
                handle.write("\n")
            self._persist(
                record,
                {
                    "execution_id": record.execution_id,
                    "runtime_type": "claude_code",
                    "status": "running",
                    "started_at": started_at,
                    "message": latest_message,
                    "progress_event_count": progress_count,
                    "progress_relative_path": f"executions/{record.execution_id}/progress.jsonl",
                    "log_relative_path": f"executions/{record.execution_id}/execution.log",
                    "worker": "runtime-worker",
                },
            )

        returncode = await process.wait()
        elapsed_ms = int((time.time() - start) * 1000)
        success = returncode == 0
        payload = {
            "execution_id": record.execution_id,
            "runtime_type": "claude_code",
            "status": "completed" if success else "failed",
            "success": success,
            "returncode": returncode,
            "elapsed_ms": elapsed_ms,
            "message": latest_message,
            "progress_event_count": progress_count,
            "progress_relative_path": f"executions/{record.execution_id}/progress.jsonl",
            "log_relative_path": f"executions/{record.execution_id}/execution.log",
            "artifact_matches": self._collect_artifacts(record),
            "worker": "runtime-worker",
            "completed_at": _utc_now(),
        }
        return self._persist(record, payload)

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

    def _claude_prompt_text(self, record: WorkerExecutionRecord) -> str:
        spec = dict(record.spec or {})
        parts: List[str] = []
        brief_relative_path = _normalize_relative_path(spec.get("task_brief_relative_path") or "")
        if brief_relative_path:
            brief_path = self.service.resolve_workspace_path(
                record.workspace_dir,
                brief_relative_path,
                require_exists=True,
            )
            if brief_path is not None and brief_path.is_file():
                parts.append(brief_path.read_text(encoding="utf-8"))
        task_prompt = str(spec.get("task_prompt") or "").strip()
        if task_prompt:
            parts.append(task_prompt)
        return "\n\n".join(part for part in parts if str(part).strip()).strip()

    def _claude_code_env(self, record: WorkerExecutionRecord) -> Dict[str, str]:
        spec = dict(record.spec or {})
        env = dict(os.environ)
        model = str(spec.get("model") or os.getenv("ANTHROPIC_MODEL") or "").strip()
        if model:
            env["ANTHROPIC_MODEL"] = model
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        return env

    def _claude_code_argv(self, *, record: WorkerExecutionRecord, repo_root: Path, prompt_text: str) -> List[str]:
        spec = dict(record.spec or {})
        argv = [
            str(getattr(settings, "claude_code_binary", "claude") or "claude"),
            "-p",
            prompt_text,
            "--output-format",
            str(getattr(settings, "claude_code_output_format", "stream-json") or "stream-json"),
            "--verbose",
            "--cwd",
            str(repo_root),
        ]
        model = str(spec.get("model") or "").strip()
        if model:
            argv.extend(["--model", model])
        max_turns = int(spec.get("max_turns") or getattr(settings, "claude_code_default_max_turns", 24) or 24)
        if max_turns > 0:
            argv.extend(["--max-turns", str(max_turns)])
        append_system_prompt = str(spec.get("append_system_prompt") or "").strip()
        if append_system_prompt:
            argv.extend(["--append-system-prompt", append_system_prompt])
        if bool(spec.get("dangerously_skip_permissions", getattr(settings, "claude_code_dangerously_skip_permissions", True))):
            argv.append("--dangerously-skip-permissions")
        else:
            permission_mode = str(spec.get("permission_mode") or "").strip()
            if permission_mode:
                argv.extend(["--permission-mode", permission_mode])
        for tool_name in list(spec.get("allowed_tools") or []):
            text = str(tool_name or "").strip()
            if text:
                argv.extend(["--allowedTools", text])
        for tool_name in list(spec.get("disallowed_tools") or []):
            text = str(tool_name or "").strip()
            if text:
                argv.extend(["--disallowedTools", text])
        for add_dir in list(spec.get("add_dirs") or []):
            normalized = _normalize_relative_path(add_dir)
            if not normalized:
                continue
            resolved = self.service.resolve_workspace_path(
                record.workspace_dir,
                normalized,
                require_exists=True,
            )
            if resolved is not None and resolved.is_dir():
                argv.extend(["--add-dir", str(resolved)])
        return argv

    @staticmethod
    def _image_tag(record: WorkerExecutionRecord, prefix: str) -> str:
        raw = f"research-paper-{prefix}-{record.project_id}-{record.workspace_id}-{record.execution_id}"
        tag = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip("-.")
        return tag[:120] or "research-paper-runtime"

    @staticmethod
    def _required_tool(runtime_type: str) -> str:
        return {
            "claude_code": str(getattr(settings, "claude_code_binary", "claude") or "claude"),
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
            "commands": {
                "claude": shutil.which(str(getattr(settings, "claude_code_binary", "claude") or "claude")),
                "devcontainer": shutil.which("devcontainer"),
                "docker": shutil.which("docker"),
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


def _claude_progress_event(line: str) -> Dict[str, Any]:
    stripped = str(line or "").strip()
    if not stripped:
        return {"event_type": "empty", "message": ""}
    try:
        payload = json.loads(stripped)
    except Exception:
        return {
            "event_type": "text",
            "message": stripped[-500:],
            "raw": stripped[-2000:],
        }
    event_type = str(
        payload.get("type")
        or payload.get("event")
        or payload.get("subtype")
        or ""
    ).strip() or "json"
    message = _extract_claude_message(payload) or f"Claude Code event: {event_type}"
    return {
        "event_type": event_type,
        "message": message[:1000],
        "payload": payload,
    }


def _extract_claude_message(value: Any) -> str:
    snippets: List[str] = []

    def walk(node: Any) -> None:
        if len(snippets) >= 4:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                snippets.append(text)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
                if len(snippets) >= 4:
                    return
            return
        if isinstance(node, dict):
            text_value = node.get("text")
            if isinstance(text_value, str) and text_value.strip():
                snippets.append(text_value.strip())
                return
            for key in ("message", "content", "result", "delta", "summary", "error"):
                if key in node:
                    walk(node.get(key))
                    if len(snippets) >= 4:
                        return

    walk(value)
    merged = " | ".join(item for item in snippets if item)
    return merged[:1000]


def _parse_claude_stream_text(stream_text: str) -> Dict[str, Any]:
    session_id = ""
    assistant_messages: List[str] = []
    result_text = ""
    is_error = False
    event_count = 0
    for raw_line in str(stream_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        event_count += 1
        if payload.get("session_id"):
            session_id = str(payload.get("session_id") or "").strip() or session_id
        if str(payload.get("type") or "").strip() == "assistant":
            message = payload.get("message")
            extracted = _extract_claude_message(message)
            if extracted:
                assistant_messages.append(extracted)
        if str(payload.get("type") or "").strip() == "result":
            result_text = str(payload.get("result") or "").strip() or result_text
            is_error = bool(payload.get("is_error"))
    return {
        "session_id": session_id,
        "assistant_text": "\n\n".join(item for item in assistant_messages if item).strip(),
        "result_text": result_text,
        "is_error": is_error,
        "event_count": event_count,
    }


def _prepare_claude_home() -> Path:
    claude_home = Path("/tmp/claude-home-app")
    settings_dir = claude_home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY") or ""
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")
    model = os.getenv("ANTHROPIC_MODEL", "qwen3.5-flash")
    settings_payload = {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": os.getenv("ANTHROPIC_SMALL_FAST_MODEL", model),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", model),
            "ANTHROPIC_DEFAULT_SONNET_MODEL": os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", model),
            "ANTHROPIC_DEFAULT_OPUS_MODEL": os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL", model),
            "CLAUDE_CODE_SUBAGENT_MODEL": os.getenv("CLAUDE_CODE_SUBAGENT_MODEL", model),
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": os.getenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", "/opt/claude-plugin-seed"),
            "CLAUDE_CODE_PLUGIN_SEED_DIR": os.getenv("CLAUDE_CODE_PLUGIN_SEED_DIR", "/opt/claude-plugin-seed"),
        },
        "enabledPlugins": {
            "document-skills@anthropic-agent-skills": True,
        },
        "extraKnownMarketplaces": {
            "anthropic-agent-skills": {
                "source": {
                    "source": "github",
                    "repo": "anthropics/skills",
                }
            }
        }
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(settings_payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    (claude_home / ".claude.json").write_text(
        json.dumps({"hasCompletedOnboarding": True}, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    app_user = pwd.getpwnam("app")
    os.chown(claude_home, app_user.pw_uid, app_user.pw_gid)
    os.chown(settings_dir, app_user.pw_uid, app_user.pw_gid)
    os.chown(settings_dir / "settings.json", app_user.pw_uid, app_user.pw_gid)
    os.chown(claude_home / ".claude.json", app_user.pw_uid, app_user.pw_gid)
    return claude_home


def _claude_project_session_dir(*, claude_home: Path, workspace_dir: Path) -> Path:
    raw_path = workspace_dir.as_posix()
    project_slug = raw_path.replace("/", "-") if raw_path.startswith("/") else raw_path.replace("/", "-")
    return claude_home / ".claude" / "projects" / project_slug


def _claude_project_has_session(*, claude_home: Path, workspace_dir: Path) -> bool:
    session_dir = _claude_project_session_dir(claude_home=claude_home, workspace_dir=workspace_dir)
    if not session_dir.exists():
        return False
    return any(session_dir.glob("*.jsonl"))


def _build_claude_shell_command(*, workspace_dir: Path, argv: List[str]) -> tuple[str, bool]:
    claude_home = _prepare_claude_home()
    continue_session = _claude_project_has_session(
        claude_home=claude_home,
        workspace_dir=workspace_dir,
    )
    if continue_session and "--continue" not in argv:
        argv = [*argv, "--continue"]
    shell_command = (
        f"export HOME={shlex.quote(str(claude_home))} USER=app LOGNAME=app; "
        f"cd {shlex.quote(str(workspace_dir))} && {shlex.join(argv)}"
    )
    return shell_command, continue_session


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


@app.post("/bash/run")
async def run_bash(payload: BashRunRequest) -> Dict[str, Any]:
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    command = str(payload.command or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required")
    if shutil.which("bash") is None:
        raise HTTPException(status_code=503, detail="bash is not installed in runtime-worker")

    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=str(workspace_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=CLAUDE_STREAM_PIPE_LIMIT,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=120.0)
    except asyncio.TimeoutError:
        _terminate_process(process)
        stdout_bytes, stderr_bytes = await process.communicate()
        return {
            "project_id": int(payload.project_id),
            "workspace_dir": str(workspace_dir),
            "command": command,
            "exit_code": None,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "success": False,
            "error": "project_bash_timeout",
            "worker": "runtime-worker",
        }

    return {
        "project_id": int(payload.project_id),
        "workspace_dir": str(workspace_dir),
        "command": command,
        "exit_code": int(process.returncode or 0),
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "success": int(process.returncode or 0) == 0,
        "error": None if int(process.returncode or 0) == 0 else "project_bash_failed",
        "worker": "runtime-worker",
    }


@app.post("/claude/run")
async def run_claude(payload: ClaudeRunRequest) -> Dict[str, Any]:
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    prompt = str(payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    claude_binary = str(getattr(settings, "claude_code_binary", "claude") or "claude")
    if shutil.which(claude_binary) is None:
        raise HTTPException(status_code=503, detail="claude is not installed in runtime-worker")
    if shutil.which("su") is None:
        raise HTTPException(status_code=503, detail="su is not installed in runtime-worker")

    argv = [
        claude_binary,
        "-p",
        prompt,
        "--output-format",
        str(getattr(settings, "claude_code_output_format", "stream-json") or "stream-json"),
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    shell_command, auto_continue_session = _build_claude_shell_command(
        workspace_dir=workspace_dir,
        argv=argv if not bool(payload.continue_session) else [*argv, "--continue"],
    )
    continue_session = bool(payload.continue_session) or auto_continue_session

    process = await asyncio.create_subprocess_exec(
        "su",
        "-m",
        "app",
        "-s",
        "/bin/sh",
        "-c",
        shell_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=CLAUDE_STREAM_PIPE_LIMIT,
    )
    stdout_bytes, stderr_bytes = await process.communicate()

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    parsed = _parse_claude_stream_text(stdout_text)
    exit_code = int(process.returncode or 0)
    return {
        "project_id": int(payload.project_id),
        "workspace_dir": str(workspace_dir),
        "prompt": prompt,
        "continue_session": continue_session,
        "session_id": parsed.get("session_id") or "",
        "assistant_text": parsed.get("assistant_text") or "",
        "result_text": parsed.get("result_text") or "",
        "is_error": bool(parsed.get("is_error")) or exit_code != 0,
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "error": None if exit_code == 0 else "project_claude_failed",
        "worker": "runtime-worker",
    }


@app.post("/claude/run_stream")
async def run_claude_stream(payload: ClaudeRunRequest):
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    prompt = str(payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    claude_binary = str(getattr(settings, "claude_code_binary", "claude") or "claude")
    if shutil.which(claude_binary) is None:
        raise HTTPException(status_code=503, detail="claude is not installed in runtime-worker")
    if shutil.which("su") is None:
        raise HTTPException(status_code=503, detail="su is not installed in runtime-worker")

    argv = [
        claude_binary,
        "-p",
        prompt,
        "--output-format",
        str(getattr(settings, "claude_code_output_format", "stream-json") or "stream-json"),
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    shell_command, auto_continue_session = _build_claude_shell_command(
        workspace_dir=workspace_dir,
        argv=argv if not bool(payload.continue_session) else [*argv, "--continue"],
    )
    continue_session = bool(payload.continue_session) or auto_continue_session

    process = await asyncio.create_subprocess_exec(
        "su",
        "-m",
        "app",
        "-s",
        "/bin/sh",
        "-c",
        shell_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=CLAUDE_STREAM_PIPE_LIMIT,
    )

    async def _generate():
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def _pump(reader: asyncio.StreamReader, stream_name: str, sink: List[str]) -> None:
            while True:
                try:
                    chunk = await reader.readline()
                except ValueError as exc:
                    await queue.put(
                        {
                            "type": "stream_error",
                            "stream": stream_name,
                            "text": f"Claude stream line exceeded buffer limit: {exc}",
                        }
                    )
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                sink.append(text)
                await queue.put({"type": "chunk", "stream": stream_name, "text": text})

        stdout_task = asyncio.create_task(_pump(process.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(_pump(process.stderr, "stderr", stderr_parts))
        wait_task = asyncio.create_task(process.wait())
        heartbeat_seconds = _claude_stream_heartbeat_seconds()
        last_stream_activity = time.monotonic()
        try:
            while True:
                if wait_task.done() and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if not wait_task.done() and now - last_stream_activity >= heartbeat_seconds:
                        last_stream_activity = now
                        yield json.dumps(_claude_stream_heartbeat_payload(), ensure_ascii=False, default=str) + "\n"
                    continue
                last_stream_activity = time.monotonic()
                yield json.dumps(item, ensure_ascii=False, default=str) + "\n"
        finally:
            await stdout_task
            await stderr_task
        returncode = await wait_task
        stdout_text = "".join(stdout_parts)
        stderr_text = "".join(stderr_parts)
        parsed = _parse_claude_stream_text(stdout_text)
        final_payload = {
            "project_id": int(payload.project_id),
            "workspace_dir": str(workspace_dir),
            "prompt": prompt,
            "continue_session": continue_session,
            "session_id": parsed.get("session_id") or "",
            "assistant_text": parsed.get("assistant_text") or "",
            "result_text": parsed.get("result_text") or "",
            "is_error": bool(parsed.get("is_error")),
            "exit_code": int(returncode or 0),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "error": None if int(returncode or 0) == 0 else "project_claude_failed",
            "worker": "runtime-worker",
        }
        yield json.dumps({"type": "result", "payload": final_payload}, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(
        _generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/docx/claude/run")
async def run_docx_claude(payload: DocxClaudeRunRequest) -> Dict[str, Any]:
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    prompt = str(payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    claude_binary = str(getattr(settings, "claude_code_binary", "claude") or "claude")
    if shutil.which(claude_binary) is None:
        raise HTTPException(status_code=503, detail="claude is not installed in runtime-worker")
    if shutil.which("su") is None:
        raise HTTPException(status_code=503, detail="su is not installed in runtime-worker")

    argv = [
        claude_binary,
        "-p",
        prompt,
        "--output-format",
        str(getattr(settings, "claude_code_output_format", "stream-json") or "stream-json"),
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    shell_command, auto_continue_session = _build_claude_shell_command(
        workspace_dir=workspace_dir,
        argv=argv if not bool(payload.continue_session) else [*argv, "--continue"],
    )
    continue_session = bool(payload.continue_session) or auto_continue_session

    process = await asyncio.create_subprocess_exec(
        "su",
        "-m",
        "app",
        "-s",
        "/bin/sh",
        "-c",
        shell_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=CLAUDE_STREAM_PIPE_LIMIT,
    )
    stdout_bytes, stderr_bytes = await process.communicate()

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    parsed = _parse_claude_stream_text(stdout_text)
    exit_code = int(process.returncode or 0)
    return {
        "docx_id": str(payload.docx_id or "").strip(),
        "workspace_dir": str(workspace_dir),
        "prompt": prompt,
        "continue_session": continue_session,
        "session_id": parsed.get("session_id") or "",
        "assistant_text": parsed.get("assistant_text") or "",
        "result_text": parsed.get("result_text") or "",
        "is_error": bool(parsed.get("is_error")) or exit_code != 0,
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "error": None if exit_code == 0 else "docx_claude_failed",
        "worker": "runtime-worker",
    }


@app.post("/docx/claude/run_stream")
async def run_docx_claude_stream(payload: DocxClaudeRunRequest):
    workspace_dir = _resolve_workspace_dir(payload.workspace_dir)
    prompt = str(payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    claude_binary = str(getattr(settings, "claude_code_binary", "claude") or "claude")
    if shutil.which(claude_binary) is None:
        raise HTTPException(status_code=503, detail="claude is not installed in runtime-worker")
    if shutil.which("su") is None:
        raise HTTPException(status_code=503, detail="su is not installed in runtime-worker")

    argv = [
        claude_binary,
        "-p",
        prompt,
        "--output-format",
        str(getattr(settings, "claude_code_output_format", "stream-json") or "stream-json"),
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    shell_command, auto_continue_session = _build_claude_shell_command(
        workspace_dir=workspace_dir,
        argv=argv if not bool(payload.continue_session) else [*argv, "--continue"],
    )
    continue_session = bool(payload.continue_session) or auto_continue_session

    process = await asyncio.create_subprocess_exec(
        "su",
        "-m",
        "app",
        "-s",
        "/bin/sh",
        "-c",
        shell_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=CLAUDE_STREAM_PIPE_LIMIT,
    )

    async def _generate():
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def _pump(reader: asyncio.StreamReader, stream_name: str, sink: List[str]) -> None:
            while True:
                try:
                    chunk = await reader.readline()
                except ValueError as exc:
                    await queue.put(
                        {
                            "type": "stream_error",
                            "stream": stream_name,
                            "text": f"Claude stream line exceeded buffer limit: {exc}",
                        }
                    )
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                sink.append(text)
                await queue.put({"type": "chunk", "stream": stream_name, "text": text})

        stdout_task = asyncio.create_task(_pump(process.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(_pump(process.stderr, "stderr", stderr_parts))
        wait_task = asyncio.create_task(process.wait())
        heartbeat_seconds = _claude_stream_heartbeat_seconds()
        last_stream_activity = time.monotonic()
        try:
            while True:
                if wait_task.done() and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if not wait_task.done() and now - last_stream_activity >= heartbeat_seconds:
                        last_stream_activity = now
                        yield json.dumps(_claude_stream_heartbeat_payload(), ensure_ascii=False, default=str) + "\n"
                    continue
                last_stream_activity = time.monotonic()
                yield json.dumps(item, ensure_ascii=False, default=str) + "\n"
        finally:
            await stdout_task
            await stderr_task
        returncode = await wait_task
        stdout_text = "".join(stdout_parts)
        stderr_text = "".join(stderr_parts)
        parsed = _parse_claude_stream_text(stdout_text)
        final_payload = {
            "docx_id": str(payload.docx_id or "").strip(),
            "workspace_dir": str(workspace_dir),
            "prompt": prompt,
            "continue_session": continue_session,
            "session_id": parsed.get("session_id") or "",
            "assistant_text": parsed.get("assistant_text") or "",
            "result_text": parsed.get("result_text") or "",
            "is_error": bool(parsed.get("is_error")),
            "exit_code": int(returncode or 0),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "error": None if int(returncode or 0) == 0 else "docx_claude_failed",
            "worker": "runtime-worker",
        }
        yield json.dumps({"type": "result", "payload": final_payload}, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(
        _generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
