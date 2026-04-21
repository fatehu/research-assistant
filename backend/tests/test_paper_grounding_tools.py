import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools_impl import registry as agent_tools


def _project_payload() -> dict:
    return {"id": 7, "paper_id": 113}


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(id=21, notebook_id="nb-1", status="ready", title="workspace")


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
    assert any("failed probe results" in str(item) for item in result.data["validation_errors"])


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
