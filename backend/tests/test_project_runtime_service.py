import asyncio
import sys

import pytest

from app.services import project_runtime_service as runtime_module
from app.services.project_runtime_service import ProjectRuntimeService


def _fake_which(name: str) -> str | None:
    if name in {"docker", "repo2docker", "papermill", "devcontainer", "python", "python3"}:
        return f"/usr/bin/{name}"
    return None


def _fake_which_without_docker(name: str) -> str | None:
    if name in {"repo2docker", "papermill", "devcontainer", "python", "python3"}:
        return f"/usr/bin/{name}"
    return None


def test_runtime_inspection_detects_repo_environment_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)

    repo = tmp_path / "paper_repo"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (repo / "demo.ipynb").write_text("{}", encoding="utf-8")
    (repo / "train.py").write_text("print('train')\n", encoding="utf-8")

    payload = ProjectRuntimeService().inspect(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
    )

    candidate_types = [item["runtime_type"] for item in payload["runtime_candidates"]]
    assert candidate_types[:5] == [
        "devcontainer",
        "dockerfile",
        "papermill",
        "repo2docker",
        "plain-python",
    ]
    assert payload["repo"]["available"] is True
    assert payload["repo"]["detected_root_relative_path"] == "repo/source"


def test_write_and_read_execution_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)
    (tmp_path / "paper_repo").mkdir()

    saved = ProjectRuntimeService().write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "smoke",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": ["python", "-c", "print('ok')"],
            "evidence_files": ["drafts/run_drafts.json"],
        },
    )

    assert saved["relative_path"] == "executions/smoke/execution_spec.json"
    content = ProjectRuntimeService().read_execution_spec(workspace_dir=tmp_path, execution_id="smoke")
    assert content["runtime_type"] == "plain-python"
    assert content["validation"]["valid"] is True


def test_write_execution_spec_renders_repo_script_from_execution_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    (repo / "train.py").write_text("print('ok')\n", encoding="utf-8")

    saved = ProjectRuntimeService().write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "intent-repo-script",
            "execution_intent": {
                "runtime_type": "plain-python",
                "entrypoint_type": "repo_script",
                "entrypoint_path": "train.py",
                "args": ["--epochs", "1"],
            },
        },
    )

    content = saved["content"]
    assert content["runtime_type"] == "plain-python"
    assert content["cwd"] == "repo/source"
    assert content["command"] == ["python", "train.py", "--epochs", "1"]
    assert content["validation"]["valid"] is True


def test_write_execution_spec_renders_executable_shell_repo_script_from_execution_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    script = repo / "classification-results.sh"
    script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    script.chmod(0o755)

    saved = ProjectRuntimeService().write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "intent-shell-repo-script",
            "execution_intent": {
                "runtime_type": "plain-python",
                "entrypoint_type": "repo_script",
                "entrypoint_path": "classification-results.sh",
                "args": ["--dataset", "ag_news"],
            },
        },
    )

    content = saved["content"]
    assert content["runtime_type"] == "plain-python"
    assert content["cwd"] == "repo/source"
    assert content["command"] == ["./classification-results.sh", "--dataset", "ag_news"]
    assert content["validation"]["valid"] is True


def test_write_execution_spec_renders_generated_python_from_execution_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)
    (tmp_path / "paper_repo").mkdir()

    saved = ProjectRuntimeService().write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "intent-generated-script",
            "execution_intent": {
                "runtime_type": "plain-python",
                "entrypoint_type": "generated_python",
                "generated_program_name": "train_variant.py",
            },
            "generated_files": [
                {
                    "relative_path": "executions/intent-generated-script/train_variant.py",
                    "content": "print('variant-ok')\n",
                }
            ],
        },
    )

    content = saved["content"]
    assert content["cwd"] == "repo/source"
    assert content["command"] == ["python", "../../executions/intent-generated-script/train_variant.py"]
    assert content["validation"]["valid"] is True


def test_write_execution_generated_file_defaults_to_execution_workspace(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    saved = ProjectRuntimeService().write_execution_generated_file(
        workspace_dir=tmp_path,
        execution_id="variant-script",
        content="print('variant')\n",
    )

    target = tmp_path / "executions" / "variant-script" / "train_variant.py"
    assert saved["relative_path"] == "executions/variant-script/train_variant.py"
    assert saved["entrypoint_hint"]["entrypoint_type"] == "generated_python"
    assert target.is_file()
    assert "project-runtime: repo-import-shim" in target.read_text(encoding="utf-8")


def test_write_execution_generated_file_rejects_paths_outside_execution_scope(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    with pytest.raises(ValueError) as exc_info:
        ProjectRuntimeService().write_execution_generated_file(
            workspace_dir=tmp_path,
            execution_id="variant-script",
            relative_path="executions/other-run/train_variant.py",
            content="print('variant')\n",
        )

    assert "relative_path must stay under `executions/variant-script/`" in str(exc_info.value)


def test_derive_external_dependencies_from_download_command():
    deps = ProjectRuntimeService._derive_external_dependencies_from_spec(
        {
            "runtime_type": "plain-python",
            "command": [
                "curl",
                "-L",
                "-o",
                "300k_150x5_2.h5",
                "https://figshare.example/ndownloader/files/123",
            ],
        }
    )

    assert len(deps) == 1
    assert deps[0]["kind"] == "url"
    assert deps[0]["target"] == "https://figshare.example/ndownloader/files/123"
    assert deps[0]["expected_kind"] == "hdf5"
    assert deps[0]["required"] is True


def test_invalid_execution_spec_is_not_saved(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    with pytest.raises(ValueError):
        ProjectRuntimeService().write_execution_spec(
            workspace_dir=tmp_path,
            project_id=1,
            workspace_id=2,
            notebook_id="nb",
            execution_spec={
                "execution_id": "bad",
                "runtime_type": "plain-python",
                "cwd": "repo/source",
            },
        )

    assert not (tmp_path / "executions" / "bad" / "execution_spec.json").exists()


def test_execution_intent_rejects_raw_command_and_cwd(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    with pytest.raises(ValueError) as exc_info:
        ProjectRuntimeService().write_execution_spec(
            workspace_dir=tmp_path,
            project_id=1,
            workspace_id=2,
            notebook_id="nb",
            execution_spec={
                "execution_id": "mixed-intent",
                "cwd": "repo/source",
                "command": ["python", "train.py"],
                "execution_intent": {
                    "runtime_type": "plain-python",
                    "entrypoint_type": "repo_script",
                    "entrypoint_path": "train.py",
                },
            },
        )

    assert "execution_intent cannot be combined with raw command" in str(exc_info.value)


def test_execution_spec_rejects_shell_wrapper_command(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    with pytest.raises(ValueError) as exc_info:
        ProjectRuntimeService().write_execution_spec(
            workspace_dir=tmp_path,
            project_id=1,
            workspace_id=2,
            notebook_id="nb",
            execution_spec={
                "execution_id": "wrapped-command",
                "runtime_type": "plain-python",
                "cwd": "repo/source",
                "command": ["bash", "-lc", "python train.py"],
            },
        )

    assert "shell wrapper commands are not allowed" in str(exc_info.value)


def test_execution_spec_rejects_failed_required_preflight(tmp_path):
    (tmp_path / "paper_repo").mkdir()

    with pytest.raises(ValueError) as exc_info:
        ProjectRuntimeService().write_execution_spec(
            workspace_dir=tmp_path,
            project_id=1,
            workspace_id=2,
            notebook_id="nb",
            execution_spec={
                "execution_id": "blocked-by-probe",
                "runtime_type": "plain-python",
                "cwd": "repo/source",
                "command": [sys.executable, "-c", "print('ok')"],
                "preflight_checks": [
                    {
                        "name": "official-data-url",
                        "required": True,
                        "ok": False,
                        "diagnosis": "accepted_but_empty",
                    }
                ],
            },
        )

    assert "required preflight check failed" in str(exc_info.value)


def test_generated_files_are_limited_to_execution_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which)
    (tmp_path / "paper_repo").mkdir()

    saved = ProjectRuntimeService().write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "variant",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": [sys.executable, "../executions/variant/train_variant.py"],
            "generated_files": [
                {
                    "relative_path": "executions/variant/train_variant.py",
                    "content": "print('variant-ok')\n",
                }
            ],
        },
    )

    assert saved["content"]["validation"]["valid"] is True

    with pytest.raises(ValueError) as exc_info:
        ProjectRuntimeService().write_execution_spec(
            workspace_dir=tmp_path,
            project_id=1,
            workspace_id=2,
            notebook_id="nb",
            execution_spec={
                "execution_id": "bad-variant",
                "runtime_type": "plain-python",
                "cwd": "repo/source",
                "command": [sys.executable, "train.py"],
                "generated_files": [
                    {
                        "relative_path": "repo/source/train.py",
                        "content": "print('do not overwrite repo')\n",
                    }
                ],
            },
        )

    assert "generated_files[0].relative_path" in str(exc_info.value)


@pytest.mark.asyncio
async def test_start_execution_blocks_on_failed_external_dependency_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", False)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    (repo / "script.py").write_text("print('runtime-ok')\n", encoding="utf-8")
    service = ProjectRuntimeService()
    service.write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "blocked-download",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": [
                "curl",
                "-L",
                "-o",
                "300k_150x5_2.h5",
                "https://figshare.example/ndownloader/files/123",
            ],
            "external_dependencies": [
                {
                    "name": "prior-dump",
                    "kind": "url",
                    "target": "https://figshare.example/ndownloader/files/123",
                    "expected_kind": "hdf5",
                    "required": True,
                    "source": "official",
                }
            ],
        },
    )

    async def _fake_preflight(self, spec):  # type: ignore[no-untyped-def]
        return [
            {
                "name": "prior-dump",
                "kind": "url",
                "target": "https://figshare.example/ndownloader/files/123",
                "required": True,
                "ok": False,
                "status": "failed",
                "diagnosis": "accepted_but_empty",
            }
        ]

    monkeypatch.setattr(ProjectRuntimeService, "_run_external_dependency_preflight", _fake_preflight)

    payload = await service.start_execution(
        project_id=1,
        workspace_id=2,
        workspace_dir=tmp_path,
        execution_id="blocked-download",
    )

    assert payload["status"] == "blocked"
    assert payload["error"] == "external_dependency_preflight_failed"
    assert payload["external_dependency_preflight"][0]["diagnosis"] == "accepted_but_empty"


@pytest.mark.asyncio
async def test_probe_url_dependency_uses_google_drive_confirm_helper(monkeypatch):
    service = ProjectRuntimeService()

    class _Response:
        def __init__(self, *, status_code, url, headers):
            self.status_code = status_code
            self.url = url
            self.headers = headers

    class _StreamResponse:
        def __init__(self, *, status_code, url, headers, body: bytes):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            yield self._body

    class _Client:
        def __init__(self, *args, **kwargs):
            self.cookies = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def head(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
            )

        def stream(self, method, url, headers=None):
            del method, headers
            return _StreamResponse(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
                body=b"<!DOCTYPE html><html><body>Google Drive</body></html>",
            )

    async def _fake_confirm_download(*, client, url, read_bytes):
        del client, read_bytes
        return {
            "status_code": 206,
            "final_url": f"{url}&confirm=token",
            "content_type": "application/octet-stream",
            "content_length": 1024,
            "head_bytes": b"\x00\x01\x02\x03",
            "confirm_url": f"{url}&confirm=token",
            "confirm_token_present": True,
        }

    monkeypatch.setattr(runtime_module.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(runtime_module, "probe_google_drive_confirm_download", _fake_confirm_download)

    result = await service._probe_url_dependency(
        {
            "name": "sogou-news",
            "kind": "url",
            "target": "https://drive.google.com/file/d/demo/view",
            "expected_kind": "auto",
            "required": True,
            "source": "official",
        }
    )

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["status_code"] == 206
    assert result["detected_kind"] == "binary"
    assert result["content_type"] == "application/octet-stream"
    assert result["diagnosis"] == "response_ok"


@pytest.mark.asyncio
async def test_plain_python_execution_writes_result_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", False)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    (repo / "script.py").write_text("print('runtime-ok')\n", encoding="utf-8")
    service = ProjectRuntimeService()
    service.write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "run-script",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": [sys.executable, "script.py"],
        },
    )

    started = await service.start_execution(
        project_id=1,
        workspace_id=2,
        workspace_dir=tmp_path,
        execution_id="run-script",
    )
    assert started["status"] in {"pending", "running"}

    for _ in range(20):
        result = await service.get_execution(
            workspace_dir=tmp_path,
            project_id=1,
            execution_id="run-script",
            include_logs=True,
        )
        if result["result"].get("result_exists"):
            break
        await asyncio.sleep(0.1)

    assert result["result"]["success"] is True
    assert "runtime-ok" in result["result"]["log"]


@pytest.mark.asyncio
async def test_plain_python_execution_materializes_generated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", False)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    service = ProjectRuntimeService()
    service.write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "variant-run",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": [sys.executable, "../executions/variant-run/train_variant.py"],
            "generated_files": [
                {
                    "relative_path": "executions/variant-run/train_variant.py",
                    "content": "print('variant-ok')\n",
                }
            ],
        },
    )

    started = await service.start_execution(
        project_id=1,
        workspace_id=2,
        workspace_dir=tmp_path,
        execution_id="variant-run",
    )
    assert started["status"] in {"pending", "running"}

    for _ in range(20):
        result = await service.get_execution(
            workspace_dir=tmp_path,
            project_id=1,
            execution_id="variant-run",
            include_logs=True,
        )
        if result["result"].get("result_exists"):
            break
        await asyncio.sleep(0.1)

    assert result["result"]["success"] is True
    assert "variant-ok" in result["result"]["log"]
    assert (tmp_path / "executions" / "variant-run" / "train_variant.py").is_file()


@pytest.mark.asyncio
async def test_generated_python_variant_can_import_repo_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", False)
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    (repo / "model.py").write_text("VALUE = 128\n", encoding="utf-8")
    service = ProjectRuntimeService()
    service.write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "variant-import-run",
            "runtime_type": "plain-python",
            "cwd": "repo/source",
            "command": [sys.executable, "../executions/variant-import-run/train_variant.py"],
            "generated_files": [
                {
                    "relative_path": "executions/variant-import-run/train_variant.py",
                    "content": "from model import VALUE\nprint(f'value={VALUE}')\n",
                }
            ],
        },
    )

    started = await service.start_execution(
        project_id=1,
        workspace_id=2,
        workspace_dir=tmp_path,
        execution_id="variant-import-run",
    )
    assert started["status"] in {"pending", "running"}

    for _ in range(20):
        result = await service.get_execution(
            workspace_dir=tmp_path,
            project_id=1,
            execution_id="variant-import-run",
            include_logs=True,
        )
        if result["result"].get("result_exists"):
            break
        await asyncio.sleep(0.1)

    generated_script = (tmp_path / "executions" / "variant-import-run" / "train_variant.py").read_text(encoding="utf-8")
    assert "# project-runtime: repo-import-shim" in generated_script
    assert result["result"]["success"] is True
    assert "value=128" in result["result"]["log"]


@pytest.mark.asyncio
async def test_inspect_runtime_uses_worker_tool_availability(tmp_path, monkeypatch):
    repo = tmp_path / "paper_repo"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", True)

    async def _fake_tools(self):  # type: ignore[no-untyped-def]
        return {
            "environment": {
                "packages": {
                    "torch": {"installed": True, "version": "test"},
                }
            },
            "tool_availability": {
                "docker": {"available": True, "command": "/usr/bin/docker"},
                "docker_compose": {"available": True, "command": "docker compose"},
                "repo2docker": {"available": True, "command": "/usr/bin/repo2docker"},
                "papermill": {"available": True, "command": "/usr/bin/papermill"},
                "devcontainer": {"available": True, "command": "/usr/bin/devcontainer"},
                "python": {"available": True, "command": sys.executable},
            }
        }

    monkeypatch.setattr(runtime_module.ProjectRuntimeWorkerClient, "tools", _fake_tools)

    payload = await ProjectRuntimeService().inspect_runtime(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
    )

    candidates = {item["runtime_type"]: item for item in payload["runtime_candidates"]}
    assert payload["runtime_worker"]["available"] is True
    assert payload["runtime_worker"]["environment"]["packages"]["torch"]["installed"] is True
    assert candidates["devcontainer"]["status"] == "ready"
    assert "runtime_worker_required" not in candidates["devcontainer"]["blockers"]
    assert candidates["dockerfile"]["status"] == "ready"


def test_runtime_inspection_blocks_containerized_candidates_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.shutil, "which", _fake_which_without_docker)

    repo = tmp_path / "paper_repo"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")

    payload = ProjectRuntimeService().inspect(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        runtime_worker_available=True,
    )

    candidates = {item["runtime_type"]: item for item in payload["runtime_candidates"]}
    assert candidates["devcontainer"]["status"] == "blocked"
    assert "tool_missing:docker" in candidates["devcontainer"]["blockers"]
    assert candidates["repo2docker"]["status"] == "blocked"
    assert "tool_missing:docker" in candidates["repo2docker"]["blockers"]


@pytest.mark.asyncio
async def test_start_execution_uses_worker_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "project_runtime_worker_enabled", True)
    (tmp_path / "paper_repo").mkdir()

    service = ProjectRuntimeService()
    service.write_execution_spec(
        workspace_dir=tmp_path,
        project_id=1,
        workspace_id=2,
        notebook_id="nb",
        execution_spec={
            "execution_id": "docker-smoke",
            "runtime_type": "dockerfile",
            "cwd": "repo/source",
            "command": ["python", "-c", "print('ok')"],
            "evidence_files": ["drafts/run_drafts.json"],
        },
    )

    async def _fake_start(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["execution_id"] == "docker-smoke"
        return {
            "execution_id": "docker-smoke",
            "runtime_type": "dockerfile",
            "status": "running",
            "worker": "runtime-worker",
        }

    monkeypatch.setattr(runtime_module.ProjectRuntimeWorkerClient, "start", _fake_start)

    payload = await service.start_execution(
        project_id=1,
        workspace_id=2,
        workspace_dir=tmp_path,
        execution_id="docker-smoke",
    )

    assert payload["status"] == "running"
    assert payload["worker"] == "runtime-worker"
