import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.render_pipeline_contract import (  # noqa: E402
    RenderPipelineContractError,
    build_canonical_atom_bundle,
    build_deterministic_baseline_slots,
    build_docmind_layout_digest,
    enforce_minimal_gates,
    materialize_stage2_plan,
    validate_stage1_semantic_output,
    validate_stage1_output,
    validate_stage2_design_output,
    validate_stage2_output,
)


def _docmind_structure():
    return {
        "layouts": [
            {
                "index": 2,
                "uniqueId": "l2",
                "type": "text",
                "subType": "para",
                "text": "Paragraph Two",
                "pos": [{"x": 100, "y": 220}, {"x": 700, "y": 220}, {"x": 700, "y": 260}, {"x": 100, "y": 260}],
                "pageNum": [1],
            },
            {
                "index": 1,
                "uniqueId": "l1",
                "type": "title",
                "subType": "doc_title",
                "text": "Title One",
                "pos": [{"x": 100, "y": 120}, {"x": 700, "y": 120}, {"x": 700, "y": 160}, {"x": 100, "y": 160}],
                "pageNum": [1],
            },
        ]
    }


def _known_ids():
    bundle = build_docmind_layout_digest(_docmind_structure(), page=1)
    return bundle.known_layout_ids


def test_stage1_reject_missing_layout_id():
    known = _known_ids()
    payload = {
        "blocks": [
            {"layout_id": "l1", "role": "doc_title", "section_id": "s1", "column": 0, "confidence": 0.9},
        ],
        "sections": [{"section_id": "s1", "title_layout_id": "l1", "children": ["l1"]}],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage1_output(payload, known)
    assert exc.value.code == "STAGE1_LAYOUT_ID_MISSING"


def test_stage1_reject_duplicate_layout_id():
    known = _known_ids()
    payload = {
        "blocks": [
            {"layout_id": "l1", "role": "doc_title", "section_id": "s1", "column": 0, "confidence": 0.9},
            {"layout_id": "l1", "role": "paragraph", "section_id": "s1", "column": 0, "confidence": 0.8},
            {"layout_id": "l2", "role": "paragraph", "section_id": "s1", "column": 0, "confidence": 0.8},
        ],
        "sections": [{"section_id": "s1", "title_layout_id": "l1", "children": ["l2"]}],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage1_output(payload, known)
    assert exc.value.code == "STAGE1_LAYOUT_ID_DUPLICATE"


def test_stage1_reject_unknown_layout_id():
    known = _known_ids()
    payload = {
        "blocks": [
            {"layout_id": "l1", "role": "doc_title", "section_id": "s1", "column": 0, "confidence": 0.9},
            {"layout_id": "l2", "role": "paragraph", "section_id": "s1", "column": 0, "confidence": 0.8},
            {"layout_id": "l999", "role": "paragraph", "section_id": "s1", "column": 0, "confidence": 0.8},
        ],
        "sections": [{"section_id": "s1", "title_layout_id": "l1", "children": ["l2"]}],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage1_output(payload, known)
    assert exc.value.code == "STAGE1_LAYOUT_ID_UNKNOWN"


def test_stage1_reject_invalid_section_children():
    known = _known_ids()
    payload = {
        "blocks": [
            {"layout_id": "l1", "role": "doc_title", "section_id": "s1", "column": 0, "confidence": 0.9},
            {"layout_id": "l2", "role": "paragraph", "section_id": "s1", "column": 0, "confidence": 0.8},
        ],
        "sections": [{"section_id": "s1", "title_layout_id": "l1", "children": ["l2", "l999"]}],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage1_output(payload, known)
    assert exc.value.code == "STAGE1_SECTION_REF_INVALID"


def test_stage2_reject_unknown_component():
    known = _known_ids()
    payload = {
        "page_layout": [{"component": "UnknownCard", "source_layout_ids": ["l1"], "props": {}}],
        "unused_layout_ids": ["l2"],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_output(payload, known, ["ParagraphProse", "SectionHeading"])
    assert exc.value.code == "STAGE2_COMPONENT_NOT_ALLOWED"


def test_stage2_reject_duplicate_layout_usage():
    known = _known_ids()
    payload = {
        "page_layout": [
            {"component": "SectionHeading", "source_layout_ids": ["l1"], "props": {}},
            {"component": "ParagraphProse", "source_layout_ids": ["l1"], "props": {}},
        ],
        "unused_layout_ids": ["l2"],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_output(payload, known, ["ParagraphProse", "SectionHeading"])
    assert exc.value.code == "STAGE2_LAYOUT_ID_DUPLICATE_USE"


def test_stage2_reject_coverage_mismatch():
    known = _known_ids()
    payload = {
        "page_layout": [{"component": "SectionHeading", "source_layout_ids": ["l1"], "props": {}}],
        "unused_layout_ids": [],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_output(payload, known, ["ParagraphProse", "SectionHeading"])
    assert exc.value.code == "STAGE2_LAYOUT_ID_COVERAGE_MISMATCH"


def test_stage2_accept_full_partition_and_materialize():
    bundle = build_docmind_layout_digest(_docmind_structure(), page=1)
    payload = {
        "page_layout": [{"component": "SectionHeading", "source_layout_ids": ["l1"], "props": {}}],
        "unused_layout_ids": ["l2"],
    }
    validated = validate_stage2_output(payload, bundle.known_layout_ids, ["ParagraphProse", "SectionHeading"])
    materialized = materialize_stage2_plan(validated, bundle)
    assert list(materialized.get("unused_layout_ids") or []) == ["l2"]
    rows = list(materialized.get("page_layout") or [])
    assert len(rows) == 1
    assert rows[0]["component"] == "SectionHeading"
    assert rows[0]["source_layout_ids"] == ["l1"]


def _docmind_atoms_fixture():
    return {
        "layouts": [
            {
                "uniqueId": "L1",
                "index": 1,
                "type": "title",
                "subType": "doc_title",
                "text": "Title",
                "pos": [{"x": 10, "y": 10}, {"x": 500, "y": 10}, {"x": 500, "y": 50}, {"x": 10, "y": 50}],
                "pageNum": [1],
                "blocks": [
                    {
                        "text": "Title",
                        "pos": [{"x": 10, "y": 10}, {"x": 500, "y": 10}, {"x": 500, "y": 50}, {"x": 10, "y": 50}],
                    }
                ],
            },
            {
                "uniqueId": "L2",
                "index": 2,
                "type": "text",
                "subType": "para",
                "text": "Paragraph body.",
                "pos": [{"x": 10, "y": 70}, {"x": 500, "y": 70}, {"x": 500, "y": 130}, {"x": 10, "y": 130}],
                "pageNum": [1],
                "blocks": [
                    {
                        "text": "Paragraph body.",
                        "pos": [{"x": 10, "y": 70}, {"x": 500, "y": 70}, {"x": 500, "y": 130}, {"x": 10, "y": 130}],
                    }
                ],
            },
        ]
    }


def test_build_canonical_atom_bundle_should_generate_stable_atom_ids():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    assert len(bundle.atoms) == 2
    assert len(bundle.usable_atom_ids) == 2
    assert bundle.usable_atom_ids[0].startswith("p1:l")
    assert bundle.document_fingerprint


def test_validate_stage1_semantic_output_should_cover_all_atoms():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    payload = {
        "annotations": [
            {
                "atom_id": bundle.usable_atom_ids[0],
                "role": "doc_title",
                "importance": "high",
                "grouping_hint": "title",
                "component_hint": "SectionHeading",
                "confidence": 0.99,
            },
            {
                "atom_id": bundle.usable_atom_ids[1],
                "role": "paragraph",
                "importance": "normal",
                "grouping_hint": "",
                "component_hint": "ParagraphProse",
                "confidence": 0.95,
            },
        ]
    }
    normalized = validate_stage1_semantic_output(payload, known_atom_ids=bundle.usable_atom_ids)
    assert len(list(normalized.get("annotations") or [])) == 2


def test_validate_stage2_design_output_should_reject_nested_forbidden_fields():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    payload = {
        "page_layout_slots": [
            {
                "slot_id": "slot_001",
                "component": "SectionHeading",
                "atom_ids": [bundle.usable_atom_ids[0]],
                "style_tokens": {"visual_hints": {"ownership_override": True}},
                "layout_tokens": {},
            },
            {
                "slot_id": "slot_002",
                "component": "ParagraphProse",
                "atom_ids": [bundle.usable_atom_ids[1]],
                "style_tokens": {},
                "layout_tokens": {},
            },
        ],
        "unused_atom_ids": [],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_design_output(
            payload,
            known_atom_ids=bundle.usable_atom_ids,
            allowed_components=["SectionHeading", "ParagraphProse"],
        )
    assert exc.value.code == "STAGE2_FORBIDDEN_FIELD"


def test_validate_stage2_design_output_should_reject_ownership_mutation():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    atom_id = bundle.usable_atom_ids[0]
    payload = {
        "page_layout_slots": [
            {
                "slot_id": "slot_001",
                "component": "SectionHeading",
                "atom_ids": [atom_id],
                "style_tokens": {},
                "layout_tokens": {},
            },
            {
                "slot_id": "slot_002",
                "component": "ParagraphProse",
                "atom_ids": [atom_id],
                "style_tokens": {},
                "layout_tokens": {},
            },
        ],
        "unused_atom_ids": [bundle.usable_atom_ids[1]],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_design_output(
            payload,
            known_atom_ids=bundle.usable_atom_ids,
            allowed_components=["SectionHeading", "ParagraphProse"],
        )
    assert exc.value.code == "STAGE2_OWNERSHIP_MUTATION"


def test_validate_stage2_design_output_should_reject_topology_mutation():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    payload = {
        "page_layout_slots": [
            {
                "slot_id": "slot_001",
                "component": "SectionHeading",
                "atom_ids": [bundle.usable_atom_ids[0]],
                "style_tokens": {},
                "layout_tokens": {},
            }
        ],
        "unused_atom_ids": [],
    }
    with pytest.raises(RenderPipelineContractError) as exc:
        validate_stage2_design_output(
            payload,
            known_atom_ids=bundle.usable_atom_ids,
            allowed_components=["SectionHeading", "ParagraphProse"],
        )
    assert exc.value.code == "STAGE2_TOPOLOGY_MUTATION"


def test_enforce_minimal_gates_should_detect_coverage_and_ownership():
    bundle = build_canonical_atom_bundle(docmind_structure=_docmind_atoms_fixture(), page=1, paper_id=78)
    baseline = build_deterministic_baseline_slots(
        atom_bundle=bundle,
        allowed_components=["SectionHeading", "ParagraphProse"],
    )
    ui_plan = {
        "components": [
            {
                "id": "n1",
                "type": "SectionHeading",
                "source_atom_ids": [baseline["page_layout_slots"][0]["atom_ids"][0]],
            },
            {
                "id": "n2",
                "type": "ParagraphProse",
                "source_atom_ids": [baseline["page_layout_slots"][1]["atom_ids"][0]],
            },
        ]
    }
    report = enforce_minimal_gates(
        ui_plan=ui_plan,
        usable_atom_ids=bundle.usable_atom_ids,
        allowed_components=["SectionHeading", "ParagraphProse"],
        non_empty_input=True,
    )
    assert report["schema_valid"] is True
    assert report["ownership_unchanged"] is True
    assert report["full_coverage"] is True
    assert report["passed"] is True
