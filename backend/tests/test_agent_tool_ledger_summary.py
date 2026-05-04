import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.services.react_agent as react_agent_module
from app.services.react_agent import ExecutedToolCall, ReActAgent


def _executed_tool_call(
    *,
    tool_name: str,
    observation_output: str,
    result_data: dict,
    arguments: dict | None = None,
    success: bool = True,
    error: str | None = None,
    permission_required: bool = False,
    metadata: dict | None = None,
) -> ExecutedToolCall:
    return ExecutedToolCall(
        action_event={},
        observation_event={},
        tool_message={},
        tool_name=tool_name,
        observation_output=observation_output,
        result_data=result_data,
        tool_call_id=f"call_{tool_name}",
        arguments=dict(arguments or {}),
        success=success,
        error=error,
        permission_required=permission_required,
        execution_time_ms=12.5,
        output_tokens_estimate=256,
        truncated=False,
        metadata=dict(metadata or {}),
    )


@pytest.mark.asyncio
async def test_tool_result_ledger_summary_is_deterministic_and_preserves_paper_anchors(monkeypatch):
    class _FailingLLMService:
        def __init__(self, provider):
            raise AssertionError("tool result ledger summary must not call LLMService")

    monkeypatch.setattr(react_agent_module, "LLMService", _FailingLLMService)

    status_call = _executed_tool_call(
        tool_name="paper_research_status",
        observation_output="Project reference bundle ready. " * 80,
        result_data={
            "paper": {"id": 113, "title": "Test Paper"},
            "project": {"id": 6},
            "status_summary": {"current_stage": "ready"},
            "reference_builder": {"reference_ready": True},
        },
        arguments={"paper_id": 113},
    )
    summary = await ReActAgent._tool_result_ledger_summary_text(status_call)
    first_line = summary.splitlines()[0]

    assert len(summary) <= 900
    assert len(first_line) <= 220
    assert "tool=paper_research_status" in first_line
    assert "paper_id=113" in first_line
    assert "project_id=6" in first_line
    assert "current_stage=ready" in first_line
    assert "reference_ready=true" in summary

    execution_call = _executed_tool_call(
        tool_name="paper_research_start_execution",
        observation_output="已启动后台 execution。",
        result_data={
            "project_id": 6,
            "execution_id": "baseline-001",
            "background_execution": {
                "execution_id": "baseline-001",
                "stage": "baseline_repro",
                "status": "running",
            },
        },
        arguments={"project_id": 6, "execution_id": "baseline-001"},
    )
    execution_summary = await ReActAgent._tool_result_ledger_summary_text(execution_call)

    assert "project_id=6" in execution_summary
    assert "background_execution_id=baseline-001" not in execution_summary
    assert "background_stage=baseline_repro" not in execution_summary
    assert "background_status=running" not in execution_summary


@pytest.mark.asyncio
async def test_tool_result_ledger_summary_preserves_literature_paths():
    item = _executed_tool_call(
        tool_name="literature_review_pdf_to_markdown",
        observation_output="PDF 已完整转换为 Markdown。",
        result_data={
            "literature_review_id": "review-20260504010101-demo",
            "paper_key": "paper-a",
            "pdf_path": "/app/uploads/literature_reviews/review-20260504010101-demo/pdf/paper-a.pdf",
            "md_path": "/app/uploads/literature_reviews/review-20260504010101-demo/md/paper-a.md",
            "report_path": "/app/uploads/literature_reviews/review-20260504010101-demo/md/paper-a.json",
            "page_count": 12,
            "markdown_chars": 45678,
        },
    )

    summary = await ReActAgent._tool_result_ledger_summary_text(item)

    assert "literature_review_id=review-20260504010101-demo" in summary
    assert "paper_key=paper-a" in summary
    assert "md_path=" in summary
    assert "paper-a.md" in summary
    assert "report_path=" in summary
    assert "paper-a.json" in summary
    assert "page_count=12" in summary
    assert "character_count=45678" in summary


@pytest.mark.asyncio
async def test_tool_result_ledger_summary_preserves_literature_review_list_paths():
    item = _executed_tool_call(
        tool_name="literature_review_read",
        observation_output="已列出文献综述 review Markdown 文件。",
        result_data={
            "literature_review_id": "review-20260504010101-demo",
            "review_dir": "/app/uploads/literature_reviews/review-20260504010101-demo/review",
            "review_files": [
                {"relative_path": "review/final.md", "paper_key": "final"},
                {"relative_path": "review/paper-a.md", "paper_key": "paper-a"},
            ],
        },
    )

    summary = await ReActAgent._tool_result_ledger_summary_text(item)

    assert "literature_review_id=review-20260504010101-demo" in summary
    assert "review_paths=review/final.md,review/paper-a.md" in summary
    assert "review_dir=" in summary


@pytest.mark.asyncio
async def test_tool_result_ledger_summary_preserves_artifact_failure_block_ids():
    item = _executed_tool_call(
        tool_name="document_artifact_update_blocks",
        observation_output="block_id 不存在或 updates 校验失败。",
        result_data={"artifact_id": "artifact-abc"},
        arguments={
            "updates": [
                {"block_id": "block_intro", "markdown": "# Intro"},
                {"block_id": "block_method", "markdown": "# Method"},
            ]
        },
        success=False,
        error="document_artifact_update_failed",
    )

    summary = await ReActAgent._tool_result_ledger_summary_text(item)

    assert "tool=document_artifact_update_blocks" in summary
    assert "status=失败" in summary
    assert "artifact_id=artifact-abc" in summary
    assert "block_ids=block_intro,block_method" in summary
    assert "error=document_artifact_update_failed" in summary
    assert "Preview:" in summary
    assert "block_id 不存在" in summary
