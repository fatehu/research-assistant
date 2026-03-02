import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.reader_single_agent_validator import (
    ReaderSingleAgentValidator,
    detect_text_hygiene_issues,
)


DOCMIND_BLOCKS = [
    {
        "layout_id": "l_title",
        "type": "title",
        "subType": "doc_title",
        "source_text": "Sample Paper Title",
        "block_ids": ["p1_b1"],
    },
    {
        "layout_id": "l_para",
        "type": "text",
        "subType": "para",
        "source_text": "This is body paragraph.",
        "block_ids": ["p1_b2"],
    },
]


def _valid_step_result():
    return {
        "classification": {
            "items": [
                {
                    "layout_id": "l_title",
                    "bucket": "main_content",
                    "role": "title",
                    "confidence": 0.98,
                    "reason": "title",
                },
                {
                    "layout_id": "l_para",
                    "bucket": "main_content",
                    "role": "paragraph",
                    "confidence": 0.96,
                    "reason": "paragraph",
                },
            ]
        },
        "cleaning": {
            "items": [
                {
                    "layout_id": "l_title",
                    "source_text": "Sample Paper Title",
                    "normalized_text": "Sample Paper Title",
                    "clean_ops": [],
                    "clean_confidence": 1.0,
                    "needs_review": False,
                },
                {
                    "layout_id": "l_para",
                    "source_text": "This is body paragraph.",
                    "normalized_text": "This is body paragraph.",
                    "clean_ops": ["whitespace_normalize"],
                    "clean_confidence": 0.95,
                    "needs_review": False,
                },
            ]
        },
        "ui_plan_draft": {
            "components": [
                {
                    "component": "SectionHeading",
                    "source_block_ids": ["l_title"],
                    "props": {"text": "Sample Paper Title", "level": 2},
                },
                {
                    "component": "ParagraphProse",
                    "source_block_ids": ["l_para"],
                    "props": {"text": "This is body paragraph."},
                },
            ],
            "layout_tokens": {},
        },
    }


def test_validator_hard_gates_pass_for_valid_step_result():
    validator = ReaderSingleAgentValidator()
    result = validator.validate(
        step_result=_valid_step_result(),
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert result["passed"] is True
    for gate in (
        "id_integrity",
        "full_coverage",
        "whitelist_only",
        "ownership_unchanged",
        "non_empty_plan_for_non_empty_input",
        "source_text_immutable",
    ):
        assert result["gates"][gate]["passed"] is True


def test_validator_hard_gates_fail_for_invalid_step_result():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["classification"]["items"].append(
        {
            "layout_id": "fake_layout",
            "bucket": "main_content",
            "role": "paragraph",
            "confidence": 0.2,
            "reason": "invented",
        }
    )
    invalid["classification"]["items"] = invalid["classification"]["items"][:1]
    invalid["cleaning"]["items"][0]["source_text"] = "MUTATED"
    invalid["ui_plan_draft"]["components"] = [{"component": "NotAllowed", "source_block_ids": ["l_title"], "props": {}}]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert result["passed"] is False
    assert result["gates"]["full_coverage"]["passed"] is False
    assert result["gates"]["whitelist_only"]["passed"] is False
    assert result["gates"]["source_text_immutable"]["passed"] is False


def test_deterministic_repair_converges_to_valid_result():
    validator = ReaderSingleAgentValidator()
    broken = {
        "classification": {"items": [{"layout_id": "fake", "bucket": "main_content"}]},
        "cleaning": {"items": []},
        "ui_plan_draft": {"components": [], "layout_tokens": {}},
    }

    repaired = validator.deterministic_repair(
        step_result=broken,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )
    repaired_step_result = repaired["step_result"]
    validation = validator.validate(
        step_result=repaired_step_result,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert validation["passed"] is True
    assert len(repaired_step_result["classification"]["items"]) == 2
    assert len(repaired_step_result["ui_plan_draft"]["components"]) >= 1


def test_text_hygiene_detection_flags_replacement_and_pua_and_mojibake():
    issues = detect_text_hygiene_issues("bad\uFFFDe000\ue000 ??")
    assert "contains_replacement_char" in issues
    assert "contains_private_use_area_char" in issues
    assert "mojibake_pattern_matched" in issues

