import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools_impl import registry as agent_tools
from app.services.project_service import ProjectService


def _project_payload() -> dict:
    return {"id": 7, "paper_id": 113}


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(id=21, notebook_id="nb-1", status="ready", title="workspace")


def _init_git_repo(repo_dir):
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)


def _complete_grounding_payload() -> dict:
    return {
        "repo": {
            "status": "grounded",
            "url": "https://github.com/facebookresearch/fastText.git",
            "resolved_ref": "refs/heads/main",
            "default_branch": "main",
            "commit_sha": "abc123",
            "blockers": [],
        },
        "entrypoint": {
            "status": "grounded",
            "candidates": [{"path": "main.py"}],
            "selected_candidate": {"path": "main.py"},
            "evidence_files": ["repo/source/main.py"],
            "blockers": [],
        },
        "dataset": {
            "status": "grounded",
            "sources": ["https://example.com/ag_news.csv"],
            "access_mode": "local_or_download",
            "local_presence": {"available": True},
            "blockers": [],
        },
        "runtime": {
            "status": "grounded",
            "inspection_summary": "plain-python available",
            "candidate_runtimes": [{"runtime_type": "plain-python", "status": "ready"}],
            "tool_availability": {"python": {"available": True}},
            "blockers": [],
        },
        "external_dependencies": {
            "status": "grounded",
            "urls": ["https://example.com/ag_news.csv"],
            "probe_results": [{"url": "https://example.com/ag_news.csv", "ok": True}],
            "blockers": [],
        },
        "summary": {
            "repo_grounded": True,
            "entrypoint_grounded": True,
            "dataset_grounded": True,
            "runtime_grounded": True,
            "external_dependencies_grounded": True,
            "overall_status": "grounded",
            "next_actions": ["继续 implementation_spec。"],
        },
    }


def _blocked_grounding_payload() -> dict:
    payload = _complete_grounding_payload()
    payload["dataset"] = {
        "status": "blocked",
        "sources": ["https://example.com/imdb.bin"],
        "access_mode": "download_only",
        "local_presence": {"available": False},
        "blockers": ["IMDB source blocked (HTTP 403)"],
    }
    payload["external_dependencies"] = {
        "status": "blocked",
        "urls": ["https://example.com/imdb.bin"],
        "probe_results": [{"url": "https://example.com/imdb.bin", "ok": False, "status": 403, "diagnosis": "http_403"}],
        "blockers": ["Official external dependency blocked: https://example.com/imdb.bin (HTTP 403, http_403)"],
    }
    payload["summary"] = {
        "repo_grounded": True,
        "entrypoint_grounded": True,
        "dataset_grounded": False,
        "runtime_grounded": True,
        "external_dependencies_grounded": False,
        "overall_status": "blocked",
        "run_decision": "blocked",
        "blockers": ["IMDB official source unavailable"],
        "next_actions": ["记录 blocker，并等待用户决定是否寻找替代源。"],
    }
    return payload


@pytest.mark.asyncio
async def test_grounding_report_write_and_read(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    result = await write_tool._execute(project_id=7, grounding_report=_complete_grounding_payload())

    assert result.success is True
    assert (tmp_path / "specs" / "grounding_report.json").is_file()
    assert result.data["relative_path"] == "specs/grounding_report.json"
    assert result.data["grounding_ready"] is True
    assert result.data["content"]["summary"]["run_decision"] == "ready"

    async def _resolve_for_read(self, _db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(agent_tools.PaperResearchReadArtifactTool, "_resolve_project_workspace", _resolve_for_read)
    monkeypatch.setattr(agent_tools.PaperResearchReadArtifactTool, "_workspace_dir_for", lambda self, _workspace_obj: tmp_path)

    read_tool = agent_tools.PaperResearchReadGroundingReportTool(db=object(), user_id=1)
    read_result = await read_tool._execute(project_id=7)

    assert read_result.success is True
    assert read_result.data["relative_path"] == "specs/grounding_report.json"
    content = read_result.data["content"]
    if isinstance(content, str):
        content = json.loads(content)
    assert content["summary"]["overall_status"] == "grounded"


@pytest.mark.asyncio
async def test_probe_url_requires_explicit_resolve_download_gate_for_google_drive(monkeypatch):
    tool = agent_tools.PaperResearchProbeUrlTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    class _Response:
        def __init__(self, *, status_code, url, headers, text=""):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self.text = text

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
                body=b"<!DOCTYPE html><html><title>Google Drive - Virus scan warning</title><body>download</body></html>",
            )

        async def get(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
                text='<!DOCTYPE html><html><head><title>Google Drive - Virus scan warning</title></head><body><form id="download-form"></form></body></html>',
            )

    async def _fake_page_semantics(*args, **kwargs):
        return {
            "title": "Google Drive - Virus scan warning",
            "page_kind": "download_gate",
            "signals": ["google_drive", "virus_scan_warning", "download_form"],
            "text_excerpt": "Google Drive can't scan this file for viruses.",
            "classification_source": "heuristic",
            "rationale": "download-form present",
            "diagnosis": "download_gate",
            "suggested_next_action": "retry_with_resolve_download_gate",
        }

    async def _fake_confirm_download(*, client, url, read_bytes):
        del client, read_bytes
        return {
            "status_code": 206,
            "final_url": f"{url}&confirm=t",
            "content_type": "application/octet-stream",
            "content_length": 256,
            "head_bytes": b"\x1f\x8b\x08\x00",
            "confirm_url": f"{url}&confirm=t",
            "confirm_token_present": True,
        }

    monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(agent_tools, "analyze_html_page_semantics", _fake_page_semantics)
    monkeypatch.setattr(agent_tools, "probe_google_drive_confirm_download", _fake_confirm_download)

    base_kwargs = {
        "project_id": 7,
        "url": "https://drive.google.com/uc?export=download&id=0Bz8a_Dbh9QhbUkVqNEszd0pHaFE",
        "expected_kind": "file",
    }
    without_resolve = await tool._execute(**base_kwargs)
    with_resolve = await tool._execute(**{**base_kwargs, "resolve_download_gate": True})

    assert without_resolve.success is False
    assert without_resolve.data["diagnosis"] in {"download_gate", "gdrive_confirm_required"}
    assert without_resolve.data["suggested_next_action"] == "retry_with_resolve_download_gate"
    assert without_resolve.data["resolve_download_gate"] is False

    assert with_resolve.success is True
    assert with_resolve.data["status_code"] == 206
    assert with_resolve.data["content_type"] == "application/octet-stream"
    assert with_resolve.data["diagnosis"] == "valid_gzip"
    assert with_resolve.data["resolve_download_gate"] is True


@pytest.mark.asyncio
async def test_paper_research_git_tools_surface_repo_state(tmp_path, monkeypatch):
    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True)
    _init_git_repo(repo_dir)

    tracked_file = repo_dir / "train.py"
    tracked_file.write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "train.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tracked_file.write_text("print('v2')\n", encoding="utf-8")
    (repo_dir / "notes.txt").write_text("todo\n", encoding="utf-8")

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    status_tool = agent_tools.PaperResearchGitStatusTool(db=object(), user_id=1)
    diff_tool = agent_tools.PaperResearchGitDiffTool(db=object(), user_id=1)
    log_tool = agent_tools.PaperResearchGitLogTool(db=object(), user_id=1)
    show_tool = agent_tools.PaperResearchGitShowTool(db=object(), user_id=1)
    for tool in [status_tool, diff_tool, log_tool, show_tool]:
        monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
        monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    status_result = await status_tool._execute(project_id=7)
    diff_result = await diff_tool._execute(project_id=7, repo_relative_paths=["train.py"])
    log_result = await log_tool._execute(project_id=7, max_count=5)
    show_result = await show_tool._execute(project_id=7, ref="HEAD", repo_relative_path="train.py")

    assert status_result.success is True
    assert status_result.data["clean"] is False
    assert any("train.py" in item for item in list(status_result.data.get("entries") or []))
    assert any("notes.txt" in item for item in list(status_result.data.get("entries") or []))

    assert diff_result.success is True
    assert "print('v2')" in diff_result.data["diff"]
    assert diff_result.data["repo_relative_paths"] == ["train.py"]

    assert log_result.success is True
    assert log_result.data["commits"][0]["subject"] == "initial commit"

    assert show_result.success is True
    assert "print('v1')" in show_result.data["content"]
    assert show_result.data["repo_relative_path"] == "train.py"


@pytest.mark.asyncio
async def test_grounding_incomplete_blocks_implementation_and_execution_tools(tmp_path, monkeypatch):
    project_payload = _project_payload()
    workspace = _workspace()

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return project_payload, workspace

    implementation_tool = agent_tools.PaperResearchWriteImplementationSpecTool(db=object(), user_id=1)
    monkeypatch.setattr(implementation_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(implementation_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)
    implementation_result = await implementation_tool._execute(
        project_id=7,
        implementation_spec={"source_summary": {}, "baseline": {}, "repo_plan": {}, "runtime_snapshot": {}, "data_plan": {}, "tuning_plan": {}, "readiness": {}},
    )
    assert implementation_result.success is False
    assert implementation_result.error == "grounding_incomplete"

    execution_spec_tool = agent_tools.PaperResearchWriteExecutionSpecTool(db=object(), user_id=1)
    monkeypatch.setattr(execution_spec_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(execution_spec_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)
    execution_spec_result = await execution_spec_tool._execute(
        project_id=7,
        execution_spec={"execution_id": "baseline", "runtime_type": "plain-python", "command": [sys.executable, "main.py"], "cwd": "repo/source"},
    )
    assert execution_spec_result.success is False
    assert execution_spec_result.error == "grounding_incomplete"

    execution_script_tool = agent_tools.PaperResearchWriteExecutionScriptTool(db=object(), user_id=1)
    monkeypatch.setattr(execution_script_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(execution_script_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)
    execution_script_result = await execution_script_tool._execute(
        project_id=7,
        execution_id="baseline",
        relative_path="variant.py",
        content="print('hi')\n",
    )
    assert execution_script_result.success is False
    assert execution_script_result.error == "grounding_incomplete"

    start_tool = agent_tools.PaperResearchStartExecutionTool(db=object(), user_id=1)
    monkeypatch.setattr(start_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(start_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)
    start_result = await start_tool._execute(project_id=7, execution_id="baseline")
    assert start_result.success is False
    assert start_result.error == "grounding_incomplete"


@pytest.mark.asyncio
async def test_grounding_blocked_is_complete_but_still_blocks_execution_tools(tmp_path, monkeypatch):
    project_payload = _project_payload()
    workspace = _workspace()

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return project_payload, workspace

    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "grounding_report.json").write_text(
        json.dumps(_blocked_grounding_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    implementation_tool = agent_tools.PaperResearchWriteImplementationSpecTool(db=object(), user_id=1)
    monkeypatch.setattr(implementation_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(implementation_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)
    implementation_result = await implementation_tool._execute(
        project_id=7,
        implementation_spec={"source_summary": {}, "baseline": {}, "repo_plan": {}, "runtime_snapshot": {}, "data_plan": {}, "tuning_plan": {}, "readiness": {}},
    )
    assert implementation_result.success is False
    assert implementation_result.error == "grounding_blocked"
    assert implementation_result.data["grounding_complete"] is True
    assert implementation_result.data["grounding_ready"] is False
    assert implementation_result.data["run_decision"] == "blocked"


@pytest.mark.asyncio
async def test_assess_repo_mainpath_prefers_readme_commands_and_entrypoint_hints(tmp_path, monkeypatch):
    tool = agent_tools.PaperResearchAssessRepoMainpathTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "classification-results.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo_dir / "train.py").write_text("print('train')\n", encoding="utf-8")
    (tmp_path / "repo_readme_excerpt.md").write_text(
        "Run the main reproduction with:\n```bash\nbash classification-results.sh\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "repo_reference.json").write_text(
        json.dumps({"repo_url": "https://github.com/facebookresearch/fastText.git"}),
        encoding="utf-8",
    )
    (tmp_path / "repo_file_index.json").write_text(
        json.dumps(
            {
                "readme_excerpt_file": "repo_readme_excerpt.md",
                "files": ["classification-results.sh", "train.py", "README.md"],
                "entrypoint_candidates": [{"path": "train.py"}, {"path": "classification-results.sh"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "experiment_spec.json").write_text(
        json.dumps(
            {
                "entrypoint_hints": [
                    {"value": "classification-results.sh"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = await tool._execute(project_id=7)

    assert result.success is True
    assert result.data["status"] == "identified"
    assert result.data["selected_main_path"]["path"] == "classification-results.sh"
    assert "classification-results.sh" in result.data["selected_main_path_reason"]


@pytest.mark.asyncio
async def test_grounding_report_requires_successful_probe_results_for_grounded_urls(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["dataset"]["sources"] = [
        "https://example.com/ag_news.csv",
        "https://example.com/dbpedia.csv",
    ]
    payload["dataset"]["local_presence"] = {"available": False}
    payload["external_dependencies"]["urls"] = [
        "https://example.com/ag_news.csv",
        "https://example.com/dbpedia.csv",
    ]
    payload["external_dependencies"]["probe_results"] = [
        {"url": "https://example.com/ag_news.csv", "ok": True},
    ]

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is False
    assert result.error == "grounding_report_invalid"
    assert any("external_dependencies.status" in str(item) for item in result.data["validation_errors"])
    assert any("dataset.status" in str(item) for item in result.data["validation_errors"])


@pytest.mark.asyncio
async def test_grounding_report_rejects_failed_probe_for_grounded_external_urls(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["external_dependencies"]["urls"] = ["https://example.com/ag_news.csv"]
    payload["external_dependencies"]["probe_results"] = [
        {"url": "https://example.com/ag_news.csv", "ok": False},
    ]

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is False
    assert result.error == "grounding_report_invalid"
    assert result.data["validation_errors"]


@pytest.mark.asyncio
async def test_grounding_report_normalizes_nested_url_probe_results_and_dataset_aliases(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["dataset"] = {
        "status": "grounded",
        "datasets": [
            {"name": "AG News", "source_url": "https://example.com/ag_news.csv"},
            {"name": "DBPedia", "source_url": "https://example.com/dbpedia.csv"},
        ],
        "local_presence": {"available": False},
        "blockers": [],
    }
    payload["external_dependencies"] = {
        "status": "grounded",
        "urls": [
            {
                "url": "https://example.com/ag_news.csv",
                "probe_results": [{"ok": True, "status_code": 206}],
            },
            {
                "url": "https://example.com/dbpedia.csv",
                "probe_results": [{"ok": True, "status_code": 206}],
            },
        ],
        "blockers": [],
    }

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is True
    content = result.data["content"]
    assert content["dataset"]["sources"] == [
        {"name": "AG News", "source_url": "https://example.com/ag_news.csv"},
        {"name": "DBPedia", "source_url": "https://example.com/dbpedia.csv"},
    ]
    assert content["external_dependencies"]["urls"] == [
        "https://example.com/ag_news.csv",
        "https://example.com/dbpedia.csv",
    ]
    assert [item["url"] for item in content["external_dependencies"]["probe_results"]] == [
        "https://example.com/ag_news.csv",
        "https://example.com/dbpedia.csv",
    ]


@pytest.mark.asyncio
async def test_grounding_report_inferrs_ok_from_probe_status_fields(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["dataset"] = {
        "status": "grounded",
        "sources": [
            "https://dl.fbaipublicfiles.com/fasttext/supervised-models/ag_news.bin",
            "https://dl.fbaipublicfiles.com/fasttext/supervised-models/dbpedia.bin",
        ],
        "local_presence": {"available": False},
        "blockers": [],
    }
    payload["external_dependencies"] = {
        "status": "grounded",
        "urls": [
            {
                "url": "https://dl.fbaipublicfiles.com/fasttext/supervised-models/ag_news.bin",
                "probe_results": [
                    {
                        "status": 206,
                        "content_type": "application/octet-stream",
                        "kind": "binary",
                        "diagnosis": "valid_binary",
                    }
                ],
            },
            {
                "url": "https://dl.fbaipublicfiles.com/fasttext/supervised-models/dbpedia.bin",
                "probe_results": [
                    {
                        "status_code": 206,
                        "content_type": "application/octet-stream",
                        "detected_kind": "file",
                        "diagnosis": "valid_binary",
                    }
                ],
            },
        ],
        "blockers": [],
    }

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is True
    content = result.data["content"]
    assert [item["ok"] for item in content["external_dependencies"]["probe_results"]] == [True, True]


@pytest.mark.asyncio
async def test_grounding_report_rejects_html_landing_page_probe_for_grounded_url(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["dataset"] = {
        "status": "grounded",
        "sources": ["https://drive.google.com/file/d/demo/view"],
        "local_presence": {"available": False},
        "blockers": [],
    }
    payload["external_dependencies"] = {
        "status": "grounded",
        "urls": ["https://drive.google.com/file/d/demo/view"],
        "probe_results": [
            {
                "url": "https://drive.google.com/file/d/demo/view",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "detected_kind": "html",
                "diagnosis": "html_page",
            }
        ],
        "blockers": [],
    }

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is False
    assert result.error == "grounding_report_invalid"
    assert result.data["validation_errors"]


@pytest.mark.asyncio
async def test_grounding_report_normalizes_blocked_sources_and_verified_sections(tmp_path, monkeypatch):
    write_tool = agent_tools.PaperResearchWriteGroundingReportTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(write_tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(write_tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    payload = _complete_grounding_payload()
    payload["repo"] = {
        "status": "unknown",
        "url": "https://github.com/facebookresearch/fastText.git",
        "default_branch": "main",
        "verification_status": "verified",
        "blockers": [],
    }
    payload["entrypoint"] = {
        "status": "unknown",
        "selected_candidate": {"path": "main.py"},
        "evidence_files": ["repo/source/main.py"],
        "verification_status": "verified",
        "blockers": [],
    }
    payload["dataset"] = {
        "status": "blocked",
        "sources": [
            {"name": "IMDB", "source_url": "https://example.com/imdb.bin"},
        ],
        "local_presence": {"available": False},
        "blockers": [],
        "alternative_source_candidates": [
            {
                "url": "https://mirror.example.com/imdb.bin",
                "source_type": "mirror",
                "status": "candidate",
            }
        ],
    }
    payload["external_dependencies"] = {
        "status": "blocked",
        "urls": ["https://example.com/imdb.bin"],
        "probe_results": [
            {"url": "https://example.com/imdb.bin", "status": 403, "diagnosis": "http_403"},
        ],
        "blockers": [],
        "alternative_source_candidates": [
            {
                "url": "https://mirror.example.com/imdb.bin",
                "source_type": "mirror",
                "status": "candidate",
            }
        ],
    }
    payload["summary"] = {}

    result = await write_tool._execute(project_id=7, grounding_report=payload)

    assert result.success is True
    content = result.data["content"]
    assert content["repo"]["status"] == "grounded"
    assert content["entrypoint"]["status"] == "grounded"
    assert content["dataset"]["status"] == "blocked"
    assert "Official dataset source blocked: IMDB (HTTP 403, http_403)" in content["dataset"]["blockers"]
    assert "Official external dependency blocked: https://example.com/imdb.bin (HTTP 403, http_403)" in content["external_dependencies"]["blockers"]
    assert content["summary"]["overall_status"] == "blocked"
    assert any("Official dataset source blocked" in item for item in content["summary"]["blockers"])
    assert content["external_dependencies"]["alternative_source_candidates"][0]["url"] == "https://mirror.example.com/imdb.bin"
    assert "alternative_source_candidates" not in " ".join(content["summary"]["next_actions"])


@pytest.mark.asyncio
async def test_workspace_output_tools_support_list_delete_and_scope_cleanup(tmp_path, monkeypatch):
    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    (tmp_path / "paper_summary.json").write_text("{}", encoding="utf-8")
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "grounding_report.json").write_text("{}", encoding="utf-8")

    list_tool = agent_tools.PaperResearchListOutputsTool(db=object(), user_id=1)
    delete_tool = agent_tools.PaperResearchDeleteOutputTool(db=object(), user_id=1)
    cleanup_tool = agent_tools.PaperResearchCleanupScopeTool(db=object(), user_id=1)
    for tool in [list_tool, delete_tool, cleanup_tool]:
        monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
        monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    async def _list_workspace_outputs(self, *, project_id: int, user_id: int, workspace_id: int):
        assert project_id == 7 and user_id == 1 and workspace_id == 21
        return [
            {
                "relative_path": "paper_summary.json",
                "scope": "planning",
                "kind": "json",
                "present": True,
            },
            {
                "relative_path": "specs/grounding_report.json",
                "scope": "grounding",
                "kind": "json",
                "present": True,
            },
        ]

    async def _delete_workspace_output(self, *, project_id: int, user_id: int, workspace_id: int, relative_path: str):
        assert project_id == 7 and user_id == 1 and workspace_id == 21
        target = tmp_path / relative_path
        if not target.exists():
            return None
        target.unlink()
        return {"success": True, "relative_path": relative_path, "deleted": True}

    async def _cleanup_workspace_outputs_scope(self, *, project_id: int, user_id: int, workspace_id: int, scope: str):
        assert project_id == 7 and user_id == 1 and workspace_id == 21
        deleted = []
        if scope == "planning":
            target = tmp_path / "paper_summary.json"
            if target.exists():
                target.unlink()
                deleted.append("paper_summary.json")
        return {
            "project_id": project_id,
            "workspace_id": workspace_id,
            "scope": scope,
            "deleted_file_count": len(deleted),
            "deleted_dir_count": 0,
            "deleted_run_count": 0,
            "deleted_paths": deleted,
        }

    monkeypatch.setattr(ProjectService, "list_workspace_outputs", _list_workspace_outputs)
    monkeypatch.setattr(ProjectService, "delete_workspace_output", _delete_workspace_output)
    monkeypatch.setattr(ProjectService, "cleanup_workspace_outputs_scope", _cleanup_workspace_outputs_scope)

    list_result = await list_tool._execute(project_id=7, scope="all")
    assert list_result.success is True
    assert list_result.data["count"] == 2
    assert any(item["relative_path"] == "paper_summary.json" for item in list_result.data["outputs"])

    delete_result = await delete_tool._execute(project_id=7, relative_path="specs/grounding_report.json")
    assert delete_result.success is True
    assert not (tmp_path / "specs" / "grounding_report.json").exists()

    cleanup_result = await cleanup_tool._execute(project_id=7, scope="planning")
    assert cleanup_result.success is True
    assert cleanup_result.data["deleted_file_count"] == 1
    assert cleanup_result.data["scope"] == "planning"
    assert not (tmp_path / "paper_summary.json").exists()


@pytest.mark.asyncio
async def test_search_outputs_finds_matches_in_planning_artifacts(tmp_path, monkeypatch):
    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    markdown_path = tmp_path / "paper_intake_markdown.md"
    markdown_path.write_text(
        "# Bag of Tricks\n\nThe paper studies fastText and AG News.\nAnother line.\n",
        encoding="utf-8",
    )
    summary_path = tmp_path / "paper_summary.json"
    summary_path.write_text('{"method":"fastText baseline"}', encoding="utf-8")

    tool = agent_tools.PaperResearchSearchOutputsTool(db=object(), user_id=1)
    monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    async def _list_workspace_outputs(self, *, project_id: int, user_id: int, workspace_id: int):
        assert project_id == 7 and user_id == 1 and workspace_id == 21
        return [
            {
                "relative_path": "paper_intake_markdown.md",
                "scope": "planning",
                "kind": "markdown",
                "storage": "file",
                "present": True,
            },
            {
                "relative_path": "paper_summary.json",
                "scope": "planning",
                "kind": "json",
                "storage": "file",
                "present": True,
            },
        ]

    monkeypatch.setattr(ProjectService, "list_workspace_outputs", _list_workspace_outputs)

    result = await tool._execute(project_id=7, query="AG News", scope="planning", context_lines=1)

    assert result.success is True
    assert result.data["matched_file_count"] == 1
    assert result.data["searchable_output_count"] == 2
    assert result.data["matches"][0]["relative_path"] == "paper_intake_markdown.md"
    assert result.data["matches"][0]["line_number"] == 3
    assert "AG News" in result.data["matches"][0]["line_text"]
    assert result.data["matches"][0]["context_start_line"] == 2
    assert result.data["matches"][0]["context_end_line"] == 4
    assert "AG News" in str(result.data["matches"][0].get("context_text") or "")
