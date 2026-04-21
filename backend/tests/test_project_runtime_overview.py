from types import SimpleNamespace

import pytest

from app.services.agent_tools_impl import registry as agent_tools
from app.services.project_service import ProjectService


@pytest.mark.asyncio
async def test_project_runtime_overview_uses_full_execution_summaries_for_baseline(monkeypatch):
    service = ProjectService(db=None)

    async def _fake_get_project(*, project_id: int, user_id: int):
        return SimpleNamespace(id=project_id, primary_workspace_id=21)

    async def _fake_load_workspaces(project_ids):
        return {
            2: [
                {
                    "id": 21,
                    "paper_id": 111,
                    "paper_title": "nanoTabPFN",
                    "notebook_id": "nb-1",
                    "title": "nanoTabPFN workspace",
                    "status": "ready",
                    "role": "primary_reproduction",
                    "run_count": 0,
                    "latest_run_status": None,
                    "latest_run_at": None,
                    "compare_report": {},
                }
            ]
        }

    async def _fake_recent_executions(**kwargs):
        return [
            {"execution_id": "install_dependencies", "status": "completed", "label": "install_dependencies"},
            {"execution_id": "repo_sync", "status": "completed", "label": "repo_sync"},
            {"execution_id": "dataset_probe", "status": "completed", "label": "dataset_probe"},
            {"execution_id": "smoke_run", "status": "completed", "label": "smoke_run"},
            {"execution_id": "lint_check", "status": "completed", "label": "lint_check"},
        ]

    async def _fake_runtime_context(**kwargs):
        return {}

    monkeypatch.setattr(service, "get_project", _fake_get_project)
    monkeypatch.setattr(service, "_load_project_workspaces", _fake_load_workspaces)
    monkeypatch.setattr(service, "_load_recent_workspace_executions", _fake_recent_executions)
    monkeypatch.setattr(
        service,
        "_load_workspace_execution_summaries",
        lambda **kwargs: [
            {"execution_id": "install_dependencies", "status": "completed", "label": "install_dependencies"},
            {"execution_id": "repo_sync", "status": "completed", "label": "repo_sync"},
            {"execution_id": "dataset_probe", "status": "completed", "label": "dataset_probe"},
            {"execution_id": "smoke_run", "status": "completed", "label": "smoke_run"},
            {"execution_id": "lint_check", "status": "completed", "label": "lint_check"},
            {
                "execution_id": "baseline_repro_fresh",
                "status": "completed",
                "success": True,
                "label": "baseline_repro_fresh",
                "draft_id": "baseline_repro_fresh",
                "completed_at": "2026-04-20T00:00:00",
            },
        ],
    )
    monkeypatch.setattr(service, "_build_runtime_context", _fake_runtime_context)
    monkeypatch.setattr(service, "_build_stage_ledger", lambda **kwargs: [])
    monkeypatch.setattr(service, "_derive_workspace_state", lambda **kwargs: ("tuning", "active"))
    monkeypatch.setattr(service, "_derive_project_state", lambda **kwargs: ("tuning", "active", "ready"))

    overview = await service.get_project_runtime_overview(project_id=2, user_id=1)

    assert overview is not None
    assert overview["execution_count"] == 6
    assert overview["workspaces"][0]["results"]["baseline_status"] == "completed"
    assert overview["workspaces"][0]["results"]["baseline_execution_id"] == "baseline_repro_fresh"
    assert overview["workspaces"][0]["results"]["compare_status"] == "ready"


def test_paper_research_status_result_output_surfaces_runtime_state():
    tool = agent_tools.PaperResearchStatusTool(db=None, user_id=1)
    paper = SimpleNamespace(id=111, title="nanoTabPFN", year=2025, venue=None, arxiv_id="2501.00001")
    workspace = SimpleNamespace(
        id=21,
        status="ready",
        title="nanoTabPFN workspace",
        notebook_id="nb-1",
        experiment_spec_json={},
        summary_json={},
        runs=[],
    )
    project = {"id": 2, "title": "nanoTabPFN Project", "status": "active", "goal": None, "paper_count": 1, "workspace_count": 1}

    result = tool._result(
        action="status",
        paper=paper,
        project=project,
        workspace=workspace,
        extra={
            "status_summary": {
                "current_stage": "tuning",
                "current_status": "active",
                "baseline_status": "completed",
                "baseline_execution_id": "baseline_repro_fresh",
                "tuning_status": "missing",
                "tuning_execution_id": None,
                "compare_status": "ready",
                "recommended_next_action": "先调用 paper_research_read_execution(project_id=2, execution_id=\"baseline_repro_fresh\")。",
            }
        },
    )

    assert "当前阶段: tuning / active" in result.output
    assert "baseline_status: completed (execution_id=baseline_repro_fresh)" in result.output
    assert "tuning_status: missing" in result.output
    assert "compare_status: ready" in result.output
    assert "recommended_next_action:" in result.output
    assert "该流程只准备计划和草案，不会自动执行训练" not in result.output


def test_paper_research_status_summary_handles_missing_runtime_overview():
    summary = agent_tools.PaperResearchStatusTool._status_summary_from_runtime(
        None,
        workspace_id=None,
        project_id=None,
    )

    assert summary["current_stage"] == "planning"
    assert summary["current_status"] == "draft"
    assert summary["baseline_status"] == "missing"
    assert summary["tuning_status"] == "missing"
    assert "paper_research_prepare" in summary["recommended_next_action"]
