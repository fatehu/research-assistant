import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.reader_single_agent_controller import ReaderSingleAgentController
from app.services.reader_single_agent_validator import ReaderSingleAgentValidator


DOCMIND_BLOCKS = [
    {
        "layout_id": "l1",
        "type": "text",
        "subType": "para",
        "source_text": "Paragraph one.",
        "block_ids": ["p1_b1"],
    }
]


@pytest.mark.asyncio
async def test_controller_done_when_hard_gates_pass():
    controller = ReaderSingleAgentController(
        validator=ReaderSingleAgentValidator(),
        max_steps=12,
        max_repair_rounds=2,
    )

    async def _model(_system_prompt, _user_prompt, _step, _phase):
        return {
            "status": "done",
            "step_result": {
                "classification": {
                    "items": [
                        {
                            "layout_id": "l1",
                            "bucket": "main_content",
                            "role": "paragraph",
                            "confidence": 0.95,
                            "reason": "body",
                        }
                    ]
                },
                "cleaning": {
                    "items": [
                        {
                            "layout_id": "l1",
                            "source_text": "Paragraph one.",
                            "normalized_text": "Paragraph one.",
                            "clean_ops": ["whitespace_normalize"],
                            "clean_confidence": 0.99,
                            "needs_review": False,
                        }
                    ]
                },
                "ui_plan_draft": {
                    "components": [
                        {
                            "component": "ParagraphProse",
                            "source_block_ids": ["l1"],
                            "props": {"text": "Paragraph one."},
                            "zone_type": "main_body",
                            "column_id": "main",
                            "region": "main",
                            "display": "default",
                            "order_key": 1.0,
                        }
                    ],
                    "layout_tokens": {
                        "layout_mode": "single_column",
                        "regions": [{"id": "main", "kind": "content"}],
                    },
                },
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    result = await controller.run(
        page_meta={"paper_id": 11, "page": 1},
        docmind_blocks=DOCMIND_BLOCKS,
        rendered_page_image="",
        component_whitelist=["ParagraphProse"],
        model_infer=_model,
    )

    assert result["status"] == "done"
    assert result["degraded_reason"] == ""
    assert result["validation_report"]["passed"] is True
    assert int(result["repair_report"]["steps_executed"]) == 1


@pytest.mark.asyncio
async def test_controller_fallback_when_model_unavailable():
    controller = ReaderSingleAgentController(
        validator=ReaderSingleAgentValidator(),
        max_steps=12,
        max_repair_rounds=2,
    )
    result = await controller.run(
        page_meta={"paper_id": 12, "page": 1},
        docmind_blocks=DOCMIND_BLOCKS,
        rendered_page_image="",
        component_whitelist=["ParagraphProse"],
        model_infer=None,
    )

    assert result["status"] == "fallback"
    assert result["degraded_reason"] == "model_unavailable"
    assert result["validation_report"]["passed"] is True
    assert len(result["step_result"]["ui_plan_draft"]["components"]) >= 1


class _AlwaysFailValidator(ReaderSingleAgentValidator):
    def validate(self, **kwargs):  # type: ignore[override]
        return {
            "passed": False,
            "gates": {
                "id_integrity": {"passed": False, "errors": ["forced"]},
                "full_coverage": {"passed": False, "errors": ["forced"]},
                "whitelist_only": {"passed": False, "errors": ["forced"]},
                "layout_contract": {"passed": False, "errors": ["forced"]},
                "no_drop_blocks": {"passed": False, "errors": ["forced"]},
                "ownership_unchanged": {"passed": False, "errors": ["forced"]},
                "non_empty_plan_for_non_empty_input": {"passed": False, "errors": ["forced"]},
                "source_text_immutable": {"passed": False, "errors": ["forced"]},
            },
            "errors": ["forced"],
            "ownership_map": {},
        }

    def deterministic_repair(self, **kwargs):  # type: ignore[override]
        return {"step_result": kwargs.get("step_result") or {}, "fixes_applied": []}

    def build_deterministic_baseline_step_result(self, **kwargs):  # type: ignore[override]
        return kwargs.get("step_result") or {
            "classification": {"items": []},
            "cleaning": {"items": []},
            "ui_plan_draft": {"components": [], "layout_tokens": {}},
        }


@pytest.mark.asyncio
async def test_controller_fallback_when_repair_rounds_exhausted():
    controller = ReaderSingleAgentController(
        validator=_AlwaysFailValidator(),
        max_steps=4,
        max_repair_rounds=0,
    )

    async def _bad_model(_system_prompt, _user_prompt, _step, _phase):
        return {"status": "continue", "step_result": {"classification": {"items": []}, "cleaning": {"items": []}, "ui_plan_draft": {"components": [], "layout_tokens": {}}}}

    result = await controller.run(
        page_meta={"paper_id": 13, "page": 1},
        docmind_blocks=DOCMIND_BLOCKS,
        rendered_page_image="",
        component_whitelist=["ParagraphProse"],
        model_infer=_bad_model,
    )

    assert result["status"] == "fallback"
    assert result["degraded_reason"] == "repair_rounds_exhausted"

