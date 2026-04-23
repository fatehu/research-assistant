from types import SimpleNamespace

import pytest

from app.services.agent_tools_impl import registry as agent_tools
from app.services.project_service import ProjectService


class _AsyncCommitDB:
    async def commit(self):
        return None


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


def test_stage_ledger_treats_runnable_with_patch_as_completed_grounding(tmp_path):
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
            "repo_grounded": true,
            "entrypoint_grounded": true,
            "dataset_grounded": false,
            "runtime_grounded": true,
            "external_dependencies_grounded": false,
            "overall_status": "blocked",
            "run_decision": "runnable_with_patch"
          },
          "repo": {"status": "grounded", "blockers": []},
          "entrypoint": {"status": "grounded", "blockers": []},
          "dataset": {"status": "blocked", "blockers": ["dataset requires manual workaround"]},
          "runtime": {"status": "grounded", "blockers": []},
          "external_dependencies": {"status": "blocked", "blockers": ["official mirror unavailable"]}
        }
        """,
        encoding="utf-8",
    )

    ledger = service._build_stage_ledger(
        workspace=workspace,
        workspace_dir=tmp_path,
        recent_executions=[],
        results={},
    )
    grounding_stage = next(item for item in ledger if item["stage"] == "grounding")
    implementation_stage = next(item for item in ledger if item["stage"] == "implementation_prep")

    assert grounding_stage["status"] == "completed"
    assert implementation_stage["status"] == "missing"


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
    assert state["run_decision"] == "blocked"
    assert state["complete"] is True
    assert state["ready_for_next_stage"] is False


def test_grounding_completion_state_does_not_trust_summary_grounded_flags_over_blocked_status():
    service = ProjectService(db=None)

    state = service._grounding_completion_state(
        {
            "summary": {
                "overall_status": "absent",
                "dataset_grounded": True,
                "external_dependencies_grounded": True,
            },
            "repo": {"status": "grounded", "blockers": []},
            "entrypoint": {"status": "grounded", "blockers": []},
            "dataset": {"status": "blocked", "blockers": ["download gate unresolved"]},
            "runtime": {"status": "grounded", "blockers": []},
            "external_dependencies": {"status": "absent", "blockers": []},
        }
    )

    assert state["overall_status"] == "absent"
    assert state["statuses"]["dataset"] == "blocked"
    assert state["statuses"]["external_dependencies"] == "absent"
    assert state["run_decision"] == "blocked"
    assert "download gate unresolved" in state["blockers"]


def test_scan_workspace_outputs_excludes_paper_repo_and_categorizes_files(tmp_path):
    service = ProjectService(db=None)
    (tmp_path / "paper_summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repo_reference.json").write_text("{}", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "executions" / "baseline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "executions" / "baseline" / "execution.log").write_text("ok\n", encoding="utf-8")
    (tmp_path / "paper_repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "paper_repo" / "train.py").write_text("print('nope')\n", encoding="utf-8")

    outputs = service._scan_workspace_outputs(tmp_path)
    paths = {item["relative_path"]: item for item in outputs}

    assert "paper_summary.json" in paths
    assert paths["paper_summary.json"]["category"] == "planning"
    assert "repo_reference.json" in paths
    assert paths["repo_reference.json"]["category"] == "repo_metadata"
    assert "specs/grounding_report.json" in paths
    assert paths["specs/grounding_report.json"]["category"] == "specs"
    assert "executions/baseline/execution.log" in paths
    assert "paper_repo/train.py" not in paths


def test_workspace_output_summary_assigns_scope_labels(tmp_path):
    service = ProjectService(db=None)
    (tmp_path / "paper_summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "executions" / "baseline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "executions" / "baseline" / "execution.log").write_text("ok\n", encoding="utf-8")

    outputs = service._scan_workspace_outputs(tmp_path)
    paths = {item["relative_path"]: item for item in outputs}

    assert paths["paper_summary.json"]["scope"] == "planning"
    assert paths["paper_summary.json"]["scope_label"] == "Planning / Intake"
    assert paths["specs/grounding_report.json"]["scope"] == "grounding"
    assert paths["specs/grounding_report.json"]["scope_label"] == "Grounding"
    assert paths["executions/baseline/execution.log"]["scope"] == "executions"
    assert paths["executions/baseline/execution.log"]["scope_label"] == "Executions"


def test_sync_workspace_model_for_asset_updates_summary_and_experiment_spec():
    service = ProjectService(db=None)
    workspace = SimpleNamespace(summary_json={}, experiment_spec_json={}, compare_report_json={})

    service._sync_workspace_model_for_asset(
        workspace_model=workspace,
        relative_path="paper_summary.json",
        content='{"research_direction":"efficient text classification"}',
        deleted=False,
    )
    service._sync_workspace_model_for_asset(
        workspace_model=workspace,
        relative_path="experiment_spec.json",
        content='{"execution_spec_version":"v3_paper_intake_scaffold"}',
        deleted=False,
    )
    service._sync_workspace_model_for_asset(
        workspace_model=workspace,
        relative_path="workspace_adapter_manifest.json",
        content='{"paper_summary_file":"paper_summary.json"}',
        deleted=False,
    )

    assert workspace.summary_json["paper_summary"]["research_direction"] == "efficient text classification"
    assert workspace.experiment_spec_json["execution_spec_version"] == "v3_paper_intake_scaffold"
    assert workspace.summary_json["workspace_adapter"]["paper_summary_file"] == "paper_summary.json"
    assert workspace.experiment_spec_json["workspace_adapter"]["paper_summary_file"] == "paper_summary.json"


@pytest.mark.asyncio
async def test_list_workspace_outputs_includes_compare_report_db_record(tmp_path, monkeypatch):
    service = ProjectService(db=_AsyncCommitDB())
    workspace_model = SimpleNamespace(
        compare_report_json={"summary": {"status": "ready"}},
        summary_json={},
        experiment_spec_json={},
    )

    async def _fake_resolve_workspace_context(**kwargs):
        return (
            SimpleNamespace(id=7),
            {"updated_at": "2026-04-22T00:00:00", "latest_run_at": None},
            workspace_model,
            tmp_path,
        )

    monkeypatch.setattr(service, "_resolve_workspace_context", _fake_resolve_workspace_context)

    outputs = await service.list_workspace_outputs(project_id=7, user_id=1, workspace_id=11)
    paths = {item["relative_path"]: item for item in outputs or []}

    assert "workspace.compare_report_json" in paths
    assert paths["workspace.compare_report_json"]["storage"] == "db_record"
    assert paths["workspace.compare_report_json"]["category"] == "results"
    assert paths["workspace.compare_report_json"]["editable"] is True


@pytest.mark.asyncio
async def test_cleanup_workspace_outputs_preserves_repo_and_clears_derived_assets(tmp_path, monkeypatch):
    service = ProjectService(db=_AsyncCommitDB())
    workspace_model = SimpleNamespace(
        id=11,
        compare_report_json={"summary": {"status": "ready"}},
        summary_json={"paper_summary": {"research_direction": "efficient text classification"}},
        experiment_spec_json={"execution_spec_version": "v3_paper_intake_scaffold"},
        status="blocked",
        updated_at=None,
    )
    (tmp_path / "paper_repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "paper_repo" / "train.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "paper_summary.json").write_text("{}", encoding="utf-8")

    async def _fake_resolve_workspace_context(**kwargs):
        return (
            SimpleNamespace(id=7),
            {"updated_at": "2026-04-22T00:00:00", "latest_run_at": None},
            workspace_model,
            tmp_path,
        )

    async def _fake_delete_workspace_runs(workspace_id: int) -> int:
        assert workspace_id == 11
        return 3

    monkeypatch.setattr(service, "_resolve_workspace_context", _fake_resolve_workspace_context)
    monkeypatch.setattr(service, "_delete_workspace_runs", _fake_delete_workspace_runs)

    result = await service.cleanup_workspace_outputs(
        project_id=7,
        user_id=1,
        workspace_id=11,
        preserve_repo=True,
    )

    assert result is not None
    assert result["deleted_run_count"] == 3
    assert (tmp_path / "paper_repo" / "train.py").is_file()
    assert not (tmp_path / "specs" / "grounding_report.json").exists()
    assert not (tmp_path / "paper_summary.json").exists()
    assert workspace_model.summary_json == {}
    assert workspace_model.experiment_spec_json == {}
    assert workspace_model.compare_report_json == {}
    assert workspace_model.status == "ready"


@pytest.mark.asyncio
async def test_cleanup_workspace_outputs_scope_grounding_also_removes_repo_analysis_artifacts(tmp_path, monkeypatch):
    service = ProjectService(db=_AsyncCommitDB())
    workspace_model = SimpleNamespace(
        id=11,
        compare_report_json={"summary": {"status": "ready"}},
        summary_json={"paper_summary": {"research_direction": "efficient text classification"}},
        experiment_spec_json={"execution_spec_version": "v3_paper_intake_scaffold"},
        status="blocked",
        updated_at=None,
    )
    (tmp_path / "paper_summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repo_reference.json").write_text("{}", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text("{}", encoding="utf-8")

    async def _fake_resolve_workspace_context(**kwargs):
        return (
            SimpleNamespace(id=7),
            {"updated_at": "2026-04-22T00:00:00", "latest_run_at": None},
            workspace_model,
            tmp_path,
        )

    async def _fake_delete_workspace_runs(workspace_id: int) -> int:
        raise AssertionError("grounding scope cleanup should not delete execution runs")

    monkeypatch.setattr(service, "_resolve_workspace_context", _fake_resolve_workspace_context)
    monkeypatch.setattr(service, "_delete_workspace_runs", _fake_delete_workspace_runs)

    result = await service.cleanup_workspace_outputs_scope(
        project_id=7,
        user_id=1,
        workspace_id=11,
        scope="grounding",
    )

    assert result is not None
    assert result["scope"] == "grounding"
    assert result["effective_scopes"] == ["grounding", "repo_analysis"]
    assert "specs/grounding_report.json" in result["deleted_paths"]
    assert "repo_reference.json" in result["deleted_paths"]
    assert not (tmp_path / "specs" / "grounding_report.json").exists()
    assert not (tmp_path / "repo_reference.json").exists()
    assert (tmp_path / "paper_summary.json").is_file()
    assert workspace_model.summary_json["paper_summary"]["research_direction"] == "efficient text classification"
    assert workspace_model.compare_report_json["summary"]["status"] == "ready"


@pytest.mark.asyncio
async def test_cleanup_workspace_outputs_scope_repo_analysis_leaves_grounding_report(tmp_path, monkeypatch):
    service = ProjectService(db=_AsyncCommitDB())
    workspace_model = SimpleNamespace(
        id=11,
        compare_report_json={},
        summary_json={},
        experiment_spec_json={},
        status="ready",
        updated_at=None,
    )
    (tmp_path / "repo_reference.json").write_text("{}", encoding="utf-8")
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "grounding_report.json").write_text("{}", encoding="utf-8")

    async def _fake_resolve_workspace_context(**kwargs):
        return (
            SimpleNamespace(id=7),
            {"updated_at": "2026-04-22T00:00:00", "latest_run_at": None},
            workspace_model,
            tmp_path,
        )

    async def _fake_delete_workspace_runs(workspace_id: int) -> int:
        raise AssertionError("repo_analysis scope cleanup should not delete execution runs")

    monkeypatch.setattr(service, "_resolve_workspace_context", _fake_resolve_workspace_context)
    monkeypatch.setattr(service, "_delete_workspace_runs", _fake_delete_workspace_runs)

    result = await service.cleanup_workspace_outputs_scope(
        project_id=7,
        user_id=1,
        workspace_id=11,
        scope="repo_analysis",
    )

    assert result is not None
    assert result["scope"] == "repo_analysis"
    assert result["effective_scopes"] == ["repo_analysis"]
    assert "repo_reference.json" in result["deleted_paths"]
    assert not (tmp_path / "repo_reference.json").exists()
    assert (tmp_path / "specs" / "grounding_report.json").is_file()
