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

FIGURE_DOCMIND_BLOCKS = [
    {
        "layout_id": "l_fig",
        "type": "figure",
        "subType": "picture",
        "source_text": "",
        "block_ids": ["p7_fig"],
    },
    {
        "layout_id": "l_caption",
        "type": "figure_name",
        "subType": "none",
        "source_text": "Fig 3. Example result. https://doi.org/10.1000/example",
        "block_ids": ["p7_caption"],
    },
    {
        "layout_id": "l_para",
        "type": "text",
        "subType": "para",
        "source_text": "Follow-up discussion paragraph.",
        "block_ids": ["p7_para"],
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
            "layout_tokens": {
                "layout_mode": "split",
                "regions": [
                    {"id": "main", "kind": "content"},
                    {"id": "sidebar", "kind": "rail"},
                ],
            },
            "components": [
                {
                    "component": "SectionHeading",
                    "source_block_ids": ["l_title"],
                    "props": {"text": "Sample Paper Title", "level": 2},
                    "zone_type": "main_body",
                    "column_id": "main",
                    "region": "main",
                    "display": "default",
                    "order_key": 1.0,
                },
                {
                    "component": "ParagraphProse",
                    "source_block_ids": ["l_para"],
                    "props": {"text": "This is body paragraph."},
                    "zone_type": "main_body",
                    "column_id": "main",
                    "region": "main",
                    "display": "default",
                    "order_key": 2.0,
                },
            ],
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
        "layout_contract",
        "no_drop_blocks",
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


def test_validator_rejects_listblock_items_object_shape():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["ui_plan_draft"]["components"] = [
        {
            "component": "ListBlock",
            "source_block_ids": ["l_para"],
            "props": {"items": [{"content": "x"}]},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        }
    ]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "ListBlock"],
    )

    assert result["passed"] is False
    assert result["gates"]["whitelist_only"]["passed"] is False
    errors = list(result["gates"]["whitelist_only"]["errors"] or [])
    assert any("component_props_invalid:ListBlock:items.0:string_required" in str(item) for item in errors)


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


def test_deterministic_baseline_preserves_figure_and_caption_when_model_unavailable():
    validator = ReaderSingleAgentValidator()
    baseline = validator.build_deterministic_baseline_step_result(
        docmind_blocks=FIGURE_DOCMIND_BLOCKS,
        component_whitelist=["ParagraphProse", "FigurePanel"],
    )

    components = list(((baseline.get("ui_plan_draft") or {}).get("components") or []))
    figure_components = [row for row in components if str(row.get("component") or "") == "FigurePanel"]
    paragraph_components = [row for row in components if str(row.get("component") or "") == "ParagraphProse"]

    assert len(figure_components) == 1
    assert any(str(item).strip() == "l_fig" for item in list(figure_components[0].get("source_block_ids") or []))
    assert any(str(item).strip() == "l_caption" for item in list(figure_components[0].get("source_block_ids") or []))
    assert "Fig 3. Example result." in str((figure_components[0].get("props") or {}).get("caption") or "")

    assert len(paragraph_components) == 1
    assert list(paragraph_components[0].get("source_block_ids") or []) == ["l_para"]


def test_deterministic_repair_drops_invalid_component_props_and_converges():
    validator = ReaderSingleAgentValidator()
    broken = _valid_step_result()
    broken["ui_plan_draft"]["components"] = [
        {
            "component": "ListBlock",
            "source_block_ids": ["l_para"],
            "props": {"items": [{"content": "x"}]},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        }
    ]

    repaired = validator.deterministic_repair(
        step_result=broken,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "ListBlock"],
    )
    repaired_step_result = repaired["step_result"]
    validation = validator.validate(
        step_result=repaired_step_result,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "ListBlock"],
    )

    assert validation["passed"] is True


def test_validator_rejects_invalid_methodology_and_callout_props():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["ui_plan_draft"]["components"] = [
        {
            "component": "MethodologyCard",
            "source_block_ids": ["l_para"],
            "props": {"steps": [{"text": "bad"}]},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        },
        {
            "component": "CalloutBox",
            "source_block_ids": ["l_title"],
            "props": {"type": "danger", "content": "check"},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 2,
        },
    ]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "MethodologyCard", "CalloutBox"],
    )

    assert result["passed"] is False
    errors = list(result["gates"]["whitelist_only"]["errors"] or [])
    assert any("component_props_invalid:MethodologyCard:steps.0:string_required" in str(item) for item in errors)
    assert any("component_props_invalid:CalloutBox:type:enum_required" in str(item) for item in errors)


def test_validator_accepts_citation_card_year_as_number():
    validator = ReaderSingleAgentValidator()
    valid = _valid_step_result()
    valid["ui_plan_draft"]["components"] = [
        {
            "component": "CitationCard",
            "source_block_ids": ["l_title"],
            "props": {
                "title": "Paper",
                "authors": ["Alice", "Bob"],
                "year": 2024,
                "journal": "Test Journal",
                "doi": "10.1000/test",
            },
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        },
        {
            "component": "ParagraphProse",
            "source_block_ids": ["l_para"],
            "props": {"text": "This is body paragraph."},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 2,
        },
    ]

    result = validator.validate(
        step_result=valid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "CitationCard"],
    )

    assert result["passed"] is True


def test_validator_rejects_invalid_insight_cluster_and_section_bridge_props():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["ui_plan_draft"]["components"] = [
        {
            "component": "InsightClusterCard",
            "source_block_ids": ["l_para"],
            "props": {"items": ["good", ""], "tone": "unknown"},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        },
        {
            "component": "SectionBridgeCard",
            "source_block_ids": ["l_title"],
            "props": {"text": ""},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 2,
        },
    ]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse", "InsightClusterCard", "SectionBridgeCard"],
    )

    assert result["passed"] is False
    errors = list(result["gates"]["whitelist_only"]["errors"] or [])
    assert any("component_props_invalid:InsightClusterCard:items.1:string_required" in str(item) for item in errors)
    assert any("component_props_invalid:InsightClusterCard:tone:enum_required" in str(item) for item in errors)
    assert any("component_props_invalid:SectionBridgeCard:text:string_required" in str(item) for item in errors)


def test_validator_accepts_valid_layout_contract_fields():
    validator = ReaderSingleAgentValidator()
    valid = _valid_step_result()
    valid["ui_plan_draft"]["layout_tokens"] = {
        "layout_mode": "split",
        "regions": [
            {"id": "main", "kind": "content"},
            {"id": "sidebar", "kind": "rail"},
        ],
    }
    valid["ui_plan_draft"]["components"] = [
        {
            "component": "SectionHeading",
            "source_block_ids": ["l_title"],
            "props": {"text": "Sample Paper Title", "level": 2},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "main",
            "display": "default",
            "order_key": 1,
        },
        {
            "component": "ParagraphProse",
            "source_block_ids": ["l_para"],
            "props": {"text": "This is body paragraph."},
            "zone_type": "side_context",
            "column_id": "sidebar",
            "region": "sidebar",
            "display": "collapsed",
            "order_key": 2.5,
        },
    ]

    result = validator.validate(
        step_result=valid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert result["passed"] is True
    assert result["gates"]["layout_contract"]["passed"] is True
    layout_errors = list(result["gates"]["layout_contract"]["errors"] or [])
    assert not layout_errors


def test_validator_rejects_bad_payload_missing_source_fake_block_and_invalid_layout_fields():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["ui_plan_draft"]["layout_tokens"] = {
        "layout_mode": "split",
        "regions": [{"id": "main", "kind": "content"}],
    }
    invalid["ui_plan_draft"]["components"] = [
        {
            "component": "SectionHeading",
            "props": {"text": "Sample Paper Title", "level": 2},
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "component": "ParagraphProse",
            "source_block_ids": ["fake_layout_id"],
            "props": {"text": "bad"},
            "zone_type": "main_body",
            "column_id": "main",
            "region": "ghost_region",
            "display": "hover_only",
            "order_key": "not-a-number",
        },
    ]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert result["passed"] is False
    assert result["gates"]["whitelist_only"]["passed"] is False
    assert result["gates"]["layout_contract"]["passed"] is False
    whitelist_errors = list(result["gates"]["whitelist_only"]["errors"] or [])
    layout_errors = list(result["gates"]["layout_contract"]["errors"] or [])
    assert any("missing_source_block_ids:SectionHeading" in str(item) for item in whitelist_errors)
    assert any("unknown_source_block_ids:ParagraphProse:fake_layout_id" in str(item) for item in layout_errors)
    assert any("region_not_declared:ParagraphProse:ghost_region" in str(item) for item in layout_errors)
    assert any("display_invalid:ParagraphProse" in str(item) for item in layout_errors)
    assert any("order_key_invalid:ParagraphProse" in str(item) for item in layout_errors)


def test_validator_rejects_missing_required_layout_fields():
    validator = ReaderSingleAgentValidator()
    invalid = _valid_step_result()
    invalid["ui_plan_draft"]["components"] = [
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
    ]

    result = validator.validate(
        step_result=invalid,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )

    assert result["passed"] is False
    assert result["gates"]["layout_contract"]["passed"] is False
    layout_errors = list(result["gates"]["layout_contract"]["errors"] or [])
    assert any("required_layout_field_missing|component=SectionHeading|field=zone_type" in str(item) for item in layout_errors)
    assert any("required_layout_field_missing|component=SectionHeading|field=column_id" in str(item) for item in layout_errors)
    assert any("required_layout_field_missing|component=SectionHeading|field=region" in str(item) for item in layout_errors)
    assert any("required_layout_field_missing|component=SectionHeading|field=display" in str(item) for item in layout_errors)
    assert any("required_layout_field_missing|component=SectionHeading|field=order_key" in str(item) for item in layout_errors)


def test_deterministic_repair_fills_layout_contract_fields_with_compat_mark():
    validator = ReaderSingleAgentValidator()
    broken = _valid_step_result()
    broken["ui_plan_draft"]["layout_tokens"] = {}
    broken["ui_plan_draft"]["components"] = [
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
    ]

    repaired = validator.deterministic_repair(
        step_result=broken,
        docmind_blocks=DOCMIND_BLOCKS,
        component_whitelist=["SectionHeading", "ParagraphProse"],
    )
    repaired_step_result = repaired["step_result"]
    repaired_components = list(((repaired_step_result.get("ui_plan_draft") or {}).get("components") or []))
    assert len(repaired_components) == 2
    for row in repaired_components:
        assert str(row.get("zone_type") or "") in {"main_body", "side_context", "figure_meta"}
        assert str(row.get("column_id") or "").strip()
        assert str(row.get("region") or "").strip()
        assert str(row.get("display") or "") in {"default", "collapsed", "pinned", "hidden_until_expand"}
        assert isinstance(row.get("order_key"), (int, float))
        assert bool(row.get("compat_filled")) is True
        assert len(list(row.get("compat_filled_fields") or [])) > 0


def test_text_hygiene_detection_flags_replacement_and_pua_and_mojibake():
    issues = detect_text_hygiene_issues("bad\uFFFDe000\ue000 ??")
    assert "contains_replacement_char" in issues
    assert "contains_private_use_area_char" in issues
    assert "mojibake_pattern_matched" in issues

