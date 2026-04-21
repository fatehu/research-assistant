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


def test_stage_ledger_planning_requires_paper_summary(tmp_path):
    service = ProjectService(db=None)
    workspace = {
        "experiment_spec": {"task": {"task_type": "classification"}},
        "summary": {},
        "compare_report": {},
    }

    (tmp_path / "paper_intake_result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "experiment_spec.json").write_text("{}", encoding="utf-8")

    ledger_without_summary = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )

    planning_stage = next(item for item in ledger_without_summary if item["stage"] == "planning")
    assert planning_stage["status"] == "ready"

    (tmp_path / "paper_summary.json").write_text('{"problem_definition":"classify tabular data"}', encoding="utf-8")

    ledger_with_summary = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )

    planning_stage_with_summary = next(item for item in ledger_with_summary if item["stage"] == "planning")
    assert planning_stage_with_summary["status"] == "completed"
    assert planning_stage_with_summary["summary"] == "classify tabular data"


def test_stage_ledger_inserts_grounding_between_planning_and_implementation(tmp_path):
    service = ProjectService(db=None)
    workspace = {
        "experiment_spec": {"task": {"task_type": "classification"}},
        "summary": {},
        "compare_report": {},
    }

    (tmp_path / "paper_intake_result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "paper_summary.json").write_text('{"problem_definition":"classify news"}', encoding="utf-8")
    (tmp_path / "experiment_spec.json").write_text("{}", encoding="utf-8")

    ledger = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )

    assert [item["stage"] for item in ledger[:3]] == ["planning", "grounding", "implementation_prep"]
    grounding_stage = next(item for item in ledger if item["stage"] == "grounding")
    implementation_stage = next(item for item in ledger if item["stage"] == "implementation_prep")
    assert grounding_stage["status"] == "ready"
    assert implementation_stage["status"] == "missing"


def test_stage_ledger_grounding_requires_grounding_report_completion(tmp_path):
    service = ProjectService(db=None)
    workspace = {
        "experiment_spec": {"task": {"task_type": "classification"}},
        "summary": {},
        "compare_report": {},
    }

    (tmp_path / "paper_intake_result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "paper_summary.json").write_text('{"problem_definition":"classify news"}', encoding="utf-8")
    (tmp_path / "experiment_spec.json").write_text("{}", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text(
        """
        {
          "summary": {
            "repo_grounded": false,
            "entrypoint_grounded": false,
            "dataset_grounded": true,
            "runtime_grounded": true,
            "external_dependencies_grounded": false,
            "overall_status": "blocked"
          },
          "repo": {"status": "blocked", "blockers": ["repo missing"]},
          "entrypoint": {"status": "absent", "blockers": []},
          "dataset": {"status": "grounded", "blockers": []},
          "runtime": {"status": "grounded", "blockers": []},
          "external_dependencies": {"status": "blocked", "blockers": ["url dead"]}
        }
        """,
        encoding="utf-8",
    )

    blocked_ledger = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )
    blocked_grounding = next(item for item in blocked_ledger if item["stage"] == "grounding")
    assert blocked_grounding["status"] == "blocked"

    (tmp_path / "specs" / "grounding_report.json").write_text(
        """
        {
          "summary": {
            "repo_grounded": true,
            "entrypoint_grounded": true,
            "dataset_grounded": true,
            "runtime_grounded": true,
            "external_dependencies_grounded": true,
            "overall_status": "grounded"
          },
          "repo": {"status": "grounded", "blockers": []},
          "entrypoint": {"status": "grounded", "blockers": []},
          "dataset": {"status": "grounded", "blockers": []},
          "runtime": {"status": "grounded", "blockers": []},
          "external_dependencies": {"status": "grounded", "blockers": []}
        }
        """,
        encoding="utf-8",
    )

    completed_ledger = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )
    completed_grounding = next(item for item in completed_ledger if item["stage"] == "grounding")
    assert completed_grounding["status"] == "completed"


def test_workspace_state_stays_in_grounding_until_grounding_complete(tmp_path):
    service = ProjectService(db=None)
    stage_ledger = [
        {"stage": "planning", "status": "completed"},
        {"stage": "grounding", "status": "ready"},
        {"stage": "implementation_prep", "status": "missing"},
        {"stage": "run_drafts", "status": "missing"},
        {"stage": "execution", "status": "missing"},
        {"stage": "results", "status": "missing"},
    ]

    current_stage, current_status = service._derive_workspace_state(
        stage_ledger=stage_ledger,
        results={},
        executions=[],
    )

    assert current_stage == "grounding"
    assert current_status == "active"


def test_grounding_completion_state_reads_summary_and_blocker_details():
    service = ProjectService(db=None)

    state = service._grounding_completion_state(
        {
            "summary": {
                "overall_status": "blocked",
                "blockers": ["Official source unavailable"],
            },
            "repo": {"status": "grounded", "blockers": []},
            "entrypoint": {"status": "grounded", "blockers": []},
            "dataset": {
                "status": "blocked",
                "blockers": [],
                "blocker_details": [{"reason": "IMDB source blocked (HTTP 403)"}],
            },
            "runtime": {"status": "unknown", "blockers": []},
            "external_dependencies": {"status": "blocked", "blockers": []},
        }
    )

    assert state["overall_status"] == "blocked"
    assert "Official source unavailable" in state["blockers"]
    assert "IMDB source blocked (HTTP 403)" in state["blockers"]
