from pathlib import Path

import pytest

from app.services.claude_code_collaboration_graph_service import ClaudeCodeCollaborationGraphService
from app.services.project_runtime_service import ProjectRuntimeService


@pytest.mark.asyncio
async def test_launch_writes_task_brief_and_execution_spec_then_starts_execution(tmp_path, monkeypatch):
    repo = tmp_path / "paper_repo"
    repo.mkdir()
    (tmp_path / "paper_summary.json").write_text(
        '{"problem_definition":"text classification","core_method":"bag of tricks"}',
        encoding="utf-8",
    )
    (tmp_path / "repo_readme_reproduction_intake.json").write_text(
        '{"reproduction_goal":"run baseline","run_scripts":["classification-results.sh"],"environment_requirements":["make","g++"]}',
        encoding="utf-8",
    )

    launches = []

    async def _fake_start_execution(self, *, project_id: int, workspace_id: int, workspace_dir: Path, execution_id: str):
        launches.append(
            {
                "project_id": project_id,
                "workspace_id": workspace_id,
                "workspace_dir": Path(workspace_dir),
                "execution_id": execution_id,
            }
        )
        return {
            "execution_id": execution_id,
            "runtime_type": "claude_code",
            "status": "running",
            "message": "Claude Code started.",
        }

    monkeypatch.setattr(ProjectRuntimeService, "start_execution", _fake_start_execution)

    service = ClaudeCodeCollaborationGraphService(runtime_service=ProjectRuntimeService())
    result = await service.launch(
        project_id=6,
        workspace_id=9,
        workspace_dir=tmp_path,
        project_title="fastText reproduction",
        task="Read the repo, run the documented baseline, and fix concrete blockers.",
        execution_id="claude-fasttext",
        model="qwen3.6-plus",
        max_turns=18,
        add_dirs=["executions"],
        allowed_tools=["Read", "Edit"],
        disallowed_tools=["WebFetch"],
        append_system_prompt="Keep a repo-first workflow.",
    )

    brief_path = tmp_path / "executions" / "claude-fasttext" / "claude_task_brief.md"
    spec_path = tmp_path / "executions" / "claude-fasttext" / "execution_spec.json"

    assert brief_path.is_file()
    assert spec_path.is_file()
    assert launches and launches[0]["execution_id"] == "claude-fasttext"
    assert result["launch_result"]["status"] == "running"
    assert result["task_brief_relative_path"] == "executions/claude-fasttext/claude_task_brief.md"

    spec = ProjectRuntimeService().read_execution_spec(
        workspace_dir=tmp_path,
        execution_id="claude-fasttext",
    )
    assert spec["runtime_type"] == "claude_code"
    assert spec["task_brief_relative_path"] == "executions/claude-fasttext/claude_task_brief.md"
    assert spec["allowed_tools"] == ["Read", "Edit"]
    assert spec["disallowed_tools"] == ["WebFetch"]
    assert spec["add_dirs"] == ["executions"]
    assert spec["validation"]["valid"] is True
