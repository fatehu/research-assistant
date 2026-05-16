import json
import asyncio
import os
import signal
import sys
import types
from types import SimpleNamespace
from io import BytesIO

import pytest
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.config import settings
from app.schemas.literature import ReaderComposePayload
from app.services import literature_reader_compose_service as compose_module
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
from app.services.literature_reader_compose_service import ReaderComposeBuildMeta
from app.services.literature_reader_compose_service import LiteratureReaderComposeService
from app.services.reader_component_contract_service import ReaderComponentContractService
from app.services.reader_compose_agent_runtime import ReaderComposeAgentRuntime
from app.services.reader_multimodal_layout_service import ReaderMultimodalLayoutService
from app.services.reader_panel_plan_agent_service import ReaderPanelPlanAgentService
from app.services.render_pipeline_contract import RenderPipelineContractError


def _score(overall: float, hard_pass: bool, quality_target: float) -> dict:
    return {
        "overall": overall,
        "structure_fidelity": 0.9,
        "readability": 0.9,
        "evidence_alignment": 0.9,
        "layout_consistency": 0.9,
        "hard_constraints_passed": hard_pass,
        "sidebar_leak_detected": False,
        "title_integrity_ok": True,
        "anchors_valid": True,
        "validation_errors": [],
        "quality_target": quality_target,
    }


def _validation_report_stub(passed: bool = False) -> dict:
    gates = {
        "id_integrity": {"passed": bool(passed), "errors": [] if passed else ["id_integrity_failed"]},
        "full_coverage": {"passed": bool(passed), "errors": [] if passed else ["full_coverage_failed"]},
        "whitelist_only": {"passed": bool(passed), "errors": [] if passed else ["whitelist_only_failed"]},
        "ownership_unchanged": {"passed": bool(passed), "errors": [] if passed else ["ownership_unchanged_failed"]},
        "non_empty_plan_for_non_empty_input": {"passed": bool(passed), "errors": [] if passed else ["non_empty_plan_failed"]},
        "source_text_immutable": {"passed": bool(passed), "errors": [] if passed else ["source_text_immutable_failed"]},
    }
    return {"passed": bool(passed), "gates": gates, "errors": [] if passed else ["fallback"]}


@pytest.mark.asyncio
async def test_force_refresh_lock_contention_should_not_read_stale_cache(monkeypatch):
    service = LiteratureReaderComposeService()
    reads = {"redis": 0}

    monkeypatch.setattr(compose_module, "LOCK_WAIT_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(compose_module, "LOCK_POLL_INTERVAL_SECONDS", 0.01, raising=False)

    async def _build_source_signature(**_kwargs):
        return "sig-demo"

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _read_payload_from_redis(_key):
        reads["redis"] += 1
        return {"status": "done"}

    async def _always_no_lock(_lock_key):
        return None

    async def _no_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _force_fallback(**kwargs):
        page = int(kwargs.get("page") or 1)
        return {
            "paper_id": 1,
            "page": page,
            "status": "fallback",
            "degraded_reason": "force_refresh_lock_timeout",
            "pipeline_version": "single_agent_v2",
            "engine_version": "reader_compose_v3",
            "source_signature": str(kwargs.get("source_signature") or "sig-demo"),
            "build_mode": "compose_agent_single_agent_v2",
            "ui_plan": {
                "plan_id": "fallback_plan",
                "components": [
                    {
                        "id": "fallback_node_1",
                        "type": "ParagraphProse",
                        "props": {"text": "fallback"},
                        "children": [],
                        "source_anchor_refs": [],
                        "source_block_ids": ["p1_b1"],
                    }
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
            "assets": [],
            "quality_report": {"overall": 0.0, "validation_errors": []},
            "iteration_trace": [],
            "main_block_ids": ["p1_b1"],
            "aux_block_ids": [],
            "validation_report": _validation_report_stub(False),
            "repair_report": {"used": True, "reason": "force_refresh_lock_timeout"},
            "generated_at": "2026-03-02T00:00:00Z",
        }

    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _always_no_lock)
    monkeypatch.setattr(service, "_build_force_refresh_timeout_fallback_payload", _force_fallback)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _no_overlay)

    payload, _ = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=1, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=True,
    )

    assert str(payload.get("status") or "") == "fallback"
    assert str(payload.get("degraded_reason") or "") == "force_refresh_lock_timeout"
    assert reads["redis"] == 0


@pytest.mark.asyncio
async def test_waiting_request_should_reuse_redis_payload_without_duplicate_build(monkeypatch):
    service = LiteratureReaderComposeService()
    state = {"redis_reads": 0}

    monkeypatch.setattr(compose_module, "LOCK_RESULT_WAIT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(compose_module, "LOCK_POLL_INTERVAL_SECONDS", 0.01, raising=False)

    async def _build_source_signature(**_kwargs):
        return "sig-demo"

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _read_compatible_payload_from_db(**_kwargs):
        return None

    async def _read_payload_from_redis(_key):
        state["redis_reads"] += 1
        if state["redis_reads"] < 3:
            return None
        return {
            "paper_id": 1,
            "page": 1,
            "status": "done",
            "pipeline_version": "layout_uid_v1",
            "engine_version": "reader_compose_v15",
            "source_signature": "sig-demo",
            "build_mode": "compose_agent_layout_uid_v1",
            "ui_plan": {
                "plan_id": "done_plan",
                "components": [
                    {
                        "id": "done_node_1",
                        "type": "ParagraphProse",
                        "props": {"text": "reused"},
                        "children": [],
                        "source_anchor_refs": [],
                        "source_block_ids": ["p1_b1"],
                    }
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
            "assets": [],
            "quality_report": {"overall": 0.9, "iterations": 1, "degraded": False, "stop_reason": "layout_uid_v1_done"},
            "iteration_trace": [],
            "main_block_ids": ["p1_b1"],
            "aux_block_ids": [],
            "validation_report": _validation_report_stub(True),
            "generated_at": "2026-03-02T00:00:00Z",
        }

    async def _always_no_lock(_lock_key):
        return None

    async def _read_lock_token(_lock_key):
        return "other-token"

    async def _no_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _should_not_build(**_kwargs):
        raise AssertionError("waiting request should not start a duplicate compose build")

    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_read_compatible_payload_from_db", _read_compatible_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _always_no_lock)
    monkeypatch.setattr(service, "_read_lock_token", _read_lock_token)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _no_overlay)
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _should_not_build)
    monkeypatch.setattr(service, "_build_layout_uid_pipeline_result", _should_not_build)

    payload, meta = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=1, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=False,
    )

    assert str(payload.get("status") or "") == "done"
    assert str(payload.get("build_mode") or "") == "compose_agent_layout_uid_v1"
    assert state["redis_reads"] >= 3
    assert meta.cache_hit is True
    assert meta.cache_layer == "redis"


def test_should_rebuild_cached_payload_for_simplified_fallback_without_atoms():
    service = LiteratureReaderComposeService()
    should_rebuild = service._should_rebuild_cached_payload(  # pylint: disable=protected-access
        {
            "status": "fallback",
            "degraded_reason": "simplified_pipeline",
            "build_mode": "compose_agent_simplified",
            "minimal_gate_report": {
                "used_atom_count": 0,
                "usable_atom_count": 42,
            },
        }
    )
    assert should_rebuild is True

    should_not_rebuild = service._should_rebuild_cached_payload(  # pylint: disable=protected-access
        {
            "status": "fallback",
            "degraded_reason": "simplified_pipeline",
            "build_mode": "compose_agent_simplified",
            "minimal_gate_report": {
                "used_atom_count": 5,
                "usable_atom_count": 42,
            },
        }
    )
    assert should_not_rebuild is False


def test_should_rebuild_cached_payload_for_stale_engine_version():
    service = LiteratureReaderComposeService()
    should_rebuild = service._should_rebuild_cached_payload(  # pylint: disable=protected-access
        {
            "status": "done",
            "build_mode": "compose_agent_layout_uid_v1",
            "engine_version": "reader_compose_v7",
        }
    )
    assert should_rebuild is True


def test_should_not_rebuild_cached_payload_for_simplified_fallback_with_meaningful_reading_flow():
    service = LiteratureReaderComposeService()
    should_rebuild = service._should_rebuild_cached_payload(  # pylint: disable=protected-access
        {
            "status": "fallback",
            "degraded_reason": "simplified_pipeline",
            "build_mode": "compose_agent_simplified",
            "minimal_gate_report": {
                "used_atom_count": 0,
                "usable_atom_count": 20,
            },
            "ui_plan": {
                "components": [
                    {
                        "id": "fig-1",
                        "type": "FigurePanel",
                        "props": {
                            "image_url": "/api/v1/literature/papers/78/assets/figure.jpg",
                            "caption": "Fig 3. Concordance and insight of ChatGPT on USMLE.",
                        },
                    },
                    {
                        "id": "p-1",
                        "type": "ParagraphProse",
                        "props": {
                            "text": "We first examined the frequency (prevalence) of insight.",
                        },
                    },
                ]
            },
        }
    )
    assert should_rebuild is False


def test_build_enrichment_bundle_should_include_reading_flow_targets_only():
    service = LiteratureReaderComposeService()
    bundle = service._build_enrichment_bundle(  # pylint: disable=protected-access
        page=7,
        payload={},
        ui_plan={
            "components": [
                {
                    "id": "sec-1",
                    "type": "SectionHeading",
                    "props": {"text": "Results"},
                    "source_block_ids": ["p7_b1"],
                    "source_anchor_refs": [],
                    "children": [],
                },
                {
                    "id": "p-1",
                    "type": "ParagraphProse",
                    "props": {
                        "paragraphs": [
                            {"text": "We first examined the frequency of insight."},
                            {"text": "Insight frequency was generally consistent between exam type and format."},
                        ]
                    },
                    "source_block_ids": ["p7_b2", "p7_b3"],
                    "source_anchor_refs": [],
                    "children": [],
                },
                {
                    "id": "fig-1",
                    "type": "FigurePanel",
                    "props": {"source_label": "Fig 3", "caption": "Concordance and insight of ChatGPT on USMLE."},
                    "source_block_ids": ["p7_fig"],
                    "source_anchor_refs": [],
                    "children": [],
                },
                {
                    "id": "ctx-1",
                    "type": "ContextRail",
                    "props": {"title": "Context", "items": [{"text": "doi info"}]},
                    "source_block_ids": ["p7_ctx"],
                    "source_anchor_refs": [],
                    "zone_type": "side_context",
                    "children": [],
                },
            ]
        },
    )

    targets = list(bundle.get("targets") or [])
    assert [str(item.get("node_id") or "") for item in targets] == ["sec-1", "p-1", "fig-1"]
    assert bundle.get("meta", {}).get("target_count") == 3
    assert targets[1]["section_label"] == "Results"
    assert targets[2]["figure_label"] == "Fig 3"
    assert "figure_explainer" in list(targets[2].get("suggested_resource_types") or [])


def test_ensure_payload_contract_should_attach_enrichment_bundle():
    service = LiteratureReaderComposeService()
    payload = service._ensure_payload_contract(  # pylint: disable=protected-access
        page=6,
        payload={
            "paper_id": 78,
            "page": 6,
            "status": "done",
            "degraded_reason": "",
            "quality_report": _score(0.9, True, 0.86),
            "validation_report": _validation_report_stub(True),
            "ui_plan": {
                "plan_id": "plan-1",
                "components": [
                    {
                        "id": "abstract-1",
                        "type": "AbstractCard",
                        "props": {"text": "This study evaluates ChatGPT on USMLE style assessments."},
                        "source_block_ids": ["p6_abs"],
                        "source_anchor_refs": [],
                        "children": [],
                    },
                    {
                        "id": "meta-1",
                        "type": "MetadataSidebarCard",
                        "props": {"items": []},
                        "source_block_ids": ["p6_meta"],
                        "source_anchor_refs": [],
                        "zone_type": "side_context",
                        "children": [],
                    },
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
        },
    )

    enrichment_bundle = dict(payload.get("enrichment_bundle") or {})
    targets = list(enrichment_bundle.get("targets") or [])
    assert len(targets) == 1
    assert targets[0]["node_id"] == "abstract-1"
    assert targets[0]["target_kind"] == "structure"
    generative_reader_plan = dict(payload.get("generative_reader_plan") or {})
    assert generative_reader_plan.get("shell_mode") == "resource_augmented_reader"
    assert generative_reader_plan.get("status") == "draft"
    assert isinstance(generative_reader_plan.get("interaction_modules"), list)


def test_compatible_source_signature_prefix_should_strip_hash_only():
    service = LiteratureReaderComposeService()
    prefix = service._compatible_source_signature_prefix(  # pylint: disable=protected-access
        "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|pv:simplified_v2|mode:auto/light/standard/0/0|h:b9aa9b19b36c5d3f66f039c0"
    )
    assert prefix == (
        "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|pv:simplified_v2|"
        "mode:auto/light/standard/0/0|h:"
    )


def test_sanitize_components_for_runtime_should_override_single_paragraph_with_pdf_geometry_split():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "n1",
            "type": "ParagraphProse",
            "props": {
                "text": "First paragraph text. Second paragraph text.",
                "paragraphs": [{"text": "First paragraph text. Second paragraph text."}],
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_block_a", "p7_block_b"],
        }
    ]
    payload = {
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_block_a",
                    "text": "First paragraph text.",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_a",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 100, "x1": 300, "top": 100, "bottom": 120}
                    },
                },
                {
                    "block_id": "p7_block_b",
                    "text": "Second paragraph text.",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_b",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 100, "x1": 300, "top": 150, "bottom": 170}
                    },
                },
            ]
        }
    }

    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        nodes=nodes,
    )

    props = dict((sanitized[0] or {}).get("props") or {})
    paragraphs = list(props.get("paragraphs") or [])
    assert len(paragraphs) == 2
    assert [str((row or {}).get("text") or "") for row in paragraphs] == [
        "First paragraph text.",
        "Second paragraph text.",
    ]
    assert str(props.get("text") or "") == "First paragraph text.\n\nSecond paragraph text."


def test_repair_text_artifacts_should_fix_pdf_line_wrap_residuals():
    service = LiteratureReaderComposeService()

    cleaned = service._repair_text_artifacts(  # pylint: disable=protected-access
        "Insight frequency was generally consis tent and items[*]in S1 Data. AI-generated explana- tions were reviewed."
    )

    assert "consistent" in cleaned
    assert "items[*] in" in cleaned
    assert "explanations" in cleaned


def test_sanitize_components_for_runtime_should_rebuild_page7_like_paragraphs_from_source_blocks():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "n_page7",
            "type": "ParagraphProse",
            "props": {
                "text": (
                    "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at "
                    "least one significant insight in 88.9% of all responses. Insight frequency was generally consis tent "
                    "between exam type and question input format (Fig 3C). Review of this subset of questions did not "
                    "reveal a discernible pattern for the paradoxical decrease (see specifically annotated items[*]in S1 Data). "
                    "Next, we quantified the density of insight (DOI) contained within AI-generated explana- tions."
                )
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": [
                "p7_dm_p7_l010_b001",
                "p7_dm_p7_l010_b002",
                "p7_dm_p7_l010_b003",
                "p7_dm_p7_l010_b004",
                "p7_dm_p7_l010_b005",
                "p7_dm_p7_l010_b006",
                "p7_dm_p7_l010_b007",
                "p7_dm_p7_l010_b008",
                "p7_dm_p7_l010_b009",
                "p7_dm_p7_l010_b010",
            ],
        }
    ]
    payload = {
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "dm_p7_l010_b001",
                    "text": "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 510, "x1": 1356, "top": 1396, "bottom": 1425}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b002",
                    "text": "least one significant insight in 88.9% of all responses. Insight frequency was generally consis",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 479, "x1": 1371, "top": 1430, "bottom": 1455}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b003",
                    "text": "tent between exam type and question input format (Fig 3C). In Step 2CK however, insight",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 481, "x1": 1353, "top": 1461, "bottom": 1488}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b004",
                    "text": "decreased by 10.3% (n=11 items) between MC-NJ and MC-J formulations, paralleling the",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 480, "x1": 1356, "top": 1491, "bottom": 1519}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b005",
                    "text": "decrement in accuracy (Fig 1B). Review of this subset of questions did not reveal a discernible",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 480, "x1": 1386, "top": 1523, "bottom": 1550}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b006",
                    "text": "pattern for the paradoxical decrease (see specifically annotated items[*]in S1 Data).",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 480, "x1": 1296, "top": 1556, "bottom": 1581}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b007",
                    "text": "Next, we quantified the density of insight (DOI) contained within AI-generated explana-",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 509, "x1": 1368, "top": 1587, "bottom": 1612}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b008",
                    "text": "tions. A density index was defined by normalizing the number of unique insights against the",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 479, "x1": 1371, "top": 1619, "bottom": 1644}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b009",
                    "text": "number of possible answer choices. This analysis was performed on MC-J entries only. High",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 480, "x1": 1371, "top": 1649, "bottom": 1676}
                    },
                },
                {
                    "block_id": "dm_p7_l010_b010",
                    "text": "quality outputs were generally characterized by DOI>0.6 (i.e. unique, novel, nonobvious, and",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_p7_para",
                    "layout_bbox_or_polygon": {
                        "bbox": {"x0": 479, "x1": 1394, "top": 1681, "bottom": 1709}
                    },
                },
            ]
        }
    }

    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        nodes=nodes,
    )

    props = dict((sanitized[0] or {}).get("props") or {})
    paragraphs = list(props.get("paragraphs") or [])

    assert len(paragraphs) == 2
    assert "consistent between exam type" in str(paragraphs[0].get("text") or "")
    assert "items[*] in S1 Data)." in str(paragraphs[0].get("text") or "")
    assert str(paragraphs[1].get("text") or "").startswith("Next, we quantified the density of insight")
    assert "AI-generated explanations." in str(paragraphs[1].get("text") or "")
    assert "\n\n" in str(props.get("text") or "")


def test_sanitize_components_for_runtime_should_rebuild_full_figure_caption_from_caption_blocks():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "n_fig",
            "type": "FigurePanel",
            "props": {
                "caption": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, AI outputs were adjudicated",
                "image_url": "asset:layout_fig",
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_fig_img", "p7_fig_cap1"],
        }
    ]
    payload = {
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_fig_img",
                    "text": "A B Answer-Explanation Concordance",
                    "layout_type": "figure",
                    "layout_sub_type": "picture",
                    "layout_unique_id": "layout_fig",
                },
                {
                    "block_id": "p7_fig_cap1",
                    "text": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3,",
                    "layout_type": "figure_name",
                    "layout_sub_type": "none",
                    "layout_unique_id": "layout_fig_caption",
                },
                {
                    "block_id": "p7_fig_cap2",
                    "text": "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams.",
                    "layout_type": "figure_name",
                    "layout_sub_type": "none",
                    "layout_unique_id": "layout_fig_caption",
                },
            ]
        }
    }

    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        nodes=nodes,
    )

    props = dict((sanitized[0] or {}).get("props") or {})
    assert str(props.get("caption") or "") == (
        "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
        "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams."
    )
    assert str(props.get("source_label") or "") == "Fig 3"


def test_sanitize_components_for_runtime_should_merge_line_level_runs_and_attach_caption_to_figure():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "cap_1",
            "type": "ParagraphProse",
            "props": {"text": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3,"},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_fig_cap1"],
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "id": "cap_2",
            "type": "ParagraphProse",
            "props": {"text": "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams."},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_fig_cap2"],
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "id": "body_1",
            "type": "ParagraphProse",
            "props": {"text": "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at"},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_body_1"],
            "zone_type": "main_body",
            "column_id": "main_right",
        },
        {
            "id": "body_2",
            "type": "ParagraphProse",
            "props": {"text": "least one significant insight in 88.9% of all responses."},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_body_2"],
            "zone_type": "main_body",
            "column_id": "main_right",
        },
        {
            "id": "n_fig",
            "type": "FigurePanel",
            "props": {
                "caption": "A B Answer-Explanation Concordance Concordance by Accuracy Subgroup",
                "image_url": "asset:layout_fig",
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p7_fig_img"],
            "zone_type": "figure_meta",
            "column_id": "main",
        },
    ]
    payload = {
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_fig_img",
                    "text": "A B Answer-Explanation Concordance",
                    "layout_type": "figure",
                    "layout_sub_type": "picture",
                    "layout_unique_id": "layout_fig",
                    "layout_bbox_or_polygon": {"bbox": {"x0": 80, "x1": 500, "top": 200, "bottom": 700}},
                },
                {
                    "block_id": "p7_fig_cap1",
                    "text": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3,",
                    "layout_type": "figure_name",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_fig_caption",
                    "layout_bbox_or_polygon": {"bbox": {"x0": 80, "x1": 500, "top": 710, "bottom": 730}},
                },
                {
                    "block_id": "p7_fig_cap2",
                    "text": "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams.",
                    "layout_type": "figure_name",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_fig_caption",
                    "layout_bbox_or_polygon": {"bbox": {"x0": 80, "x1": 500, "top": 735, "bottom": 755}},
                },
                {
                    "block_id": "p7_body_1",
                    "text": "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_body_para",
                    "layout_bbox_or_polygon": {"bbox": {"x0": 520, "x1": 1200, "top": 780, "bottom": 805}},
                },
                {
                    "block_id": "p7_body_2",
                    "text": "least one significant insight in 88.9% of all responses.",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_body_para",
                    "layout_bbox_or_polygon": {"bbox": {"x0": 520, "x1": 1200, "top": 810, "bottom": 835}},
                },
            ]
        }
    }

    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        nodes=nodes,
    )

    assert [str(item.get("type") or "") for item in sanitized] == ["FigurePanel", "ParagraphProse"]
    figure_props = dict((sanitized[0] or {}).get("props") or {})
    body_props = dict((sanitized[1] or {}).get("props") or {})

    assert str(figure_props.get("caption") or "").startswith("Fig 3. Concordance and insight of ChatGPT on USMLE.")
    assert str(figure_props.get("source_label") or "") == "Fig 3"
    assert str(body_props.get("text") or "") == (
        "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at "
        "least one significant insight in 88.9% of all responses."
    )


def test_sanitize_components_for_runtime_should_clean_noisy_figure_meta_and_merge_caption_continuations():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "n_fig",
            "type": "FigurePanel",
            "props": {
                "caption": (
                    "Open-Ended100-Accurate Indeterminate50-Inaccurate USMLE12CK3B Multiple Choice "
                    "Single Answer100-Pass Range50-Input NJ J NJ J NJ J USMLE12CK3"
                    "Fig 2. Accuracy of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
                    "AI outputs were adjudicated to be"
                ),
                "image_url": "asset:layout_fig",
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p6_fig_meta"],
            "zone_type": "figure_meta",
            "column_id": "main",
        },
        {
            "id": "cap_1",
            "type": "ParagraphProse",
            "props": {
                "text": "accurate, inaccurate, or indeterminate based on the ACI scoring system."
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p6_cap_1"],
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "id": "cap_2",
            "type": "ParagraphProse",
            "props": {
                "text": "for inputs encoded as open-ended questions or multiple choice single answer."
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p6_cap_2"],
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "id": "cap_3",
            "type": "ParagraphProse",
            "props": {
                "text": "answer without (MC-NJ) or with forced justification(MC-J)."
            },
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p6_cap_3"],
            "zone_type": "main_body",
            "column_id": "main",
        },
        {
            "id": "body_1",
            "type": "ParagraphProse",
            "props": {"text": "We first examined the frequency (prevalence) of insight."},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p6_body_1"],
            "zone_type": "main_body",
            "column_id": "main_right",
        },
    ]
    payload = {
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p6_fig_meta",
                    "text": (
                        "Open-Ended100-Accurate Indeterminate50-Inaccurate USMLE12CK3B Multiple Choice "
                        "Single Answer100-Pass Range50-Input NJ J NJ J NJ J USMLE12CK3"
                        "Fig 2. Accuracy of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
                        "AI outputs were adjudicated to be"
                    ),
                    "layout_type": "figure",
                    "layout_sub_type": "none",
                    "kind": "figure_meta",
                    "layout_unique_id": "layout_fig",
                },
                {
                    "block_id": "p6_cap_1",
                    "text": "accurate, inaccurate, or indeterminate based on the ACI scoring system.",
                    "layout_type": "figure_name",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_fig_caption",
                },
                {
                    "block_id": "p6_cap_2",
                    "text": "for inputs encoded as open-ended questions or multiple choice single answer.",
                    "layout_type": "figure_name",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_fig_caption",
                },
                {
                    "block_id": "p6_cap_3",
                    "text": "answer without (MC-NJ) or with forced justification(MC-J).",
                    "layout_type": "figure_name",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_fig_caption",
                },
                {
                    "block_id": "p6_body_1",
                    "text": "We first examined the frequency (prevalence) of insight.",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_body",
                },
            ]
        }
    }

    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=6,
        payload=payload,
        nodes=nodes,
    )

    assert [str(item.get("type") or "") for item in sanitized] == ["FigurePanel", "ParagraphProse"]
    figure = sanitized[0]
    figure_props = dict((figure or {}).get("props") or {})
    assert str(figure_props.get("caption") or "") == (
        "Fig 2. Accuracy of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
        "AI outputs were adjudicated to be accurate, inaccurate, or indeterminate based on the ACI scoring system. "
        "for inputs encoded as open-ended questions or multiple choice single answer. "
        "answer without (MC-NJ) or with forced justification(MC-J)."
    )
    assert str(figure_props.get("ai_insight") or "") == ""
    assert str(figure_props.get("source_label") or "") == "Fig 2"
    assert list(figure.get("source_block_ids") or []) == ["p6_fig_meta", "p6_cap_1", "p6_cap_2", "p6_cap_3"]
    assert str((sanitized[1] or {}).get("props", {}).get("text") or "") == (
        "We first examined the frequency (prevalence) of insight."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="signal timer unavailable on Windows")
def test_reposition_and_caption_figure_panels_should_not_loop_on_single_figure_without_caption_candidates():
    service = LiteratureReaderComposeService()
    nodes = [
        {
            "id": "fig_only",
            "type": "FigurePanel",
            "props": {"caption": "", "image_url": "asset:layout_fig"},
            "children": [],
            "source_anchor_refs": [],
            "source_block_ids": ["p14_dm_p14_l002_b001"],
            "zone_type": "main_body",
            "column_id": "main",
        }
    ]
    block_group_index = {
        "p14_dm_p14_l002_b001": {
            "block_id": "dm_p14_l002_b001",
            "text": "| The|The|The|The| | ---|---|---|---| | | |The|",
            "layout_type": "table",
            "layout_sub_type": "none",
            "kind": "paragraph",
            "layout_unique_id": "layout_fig",
        }
    }
    layout_block_id_alias_map = {"layout_fig": ["p14_dm_p14_l002_b001"]}

    def _timeout(_signum, _frame):
        raise TimeoutError("reposition figure panels timed out")

    previous_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        result = service._reposition_and_caption_figure_panels(  # pylint: disable=protected-access
            page=14,
            nodes=nodes,
            block_group_index=block_group_index,
            layout_block_id_alias_map=layout_block_id_alias_map,
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)

    assert len(result) == 1
    assert str((result[0] or {}).get("id") or "") == "fig_only"


def test_enforce_no_drop_blocks_fallback_should_ignore_intentional_omissions():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 78,
        "page": 7,
        "blocks": [
            {"id": "dm_p7_b001", "text": "Visible paragraph", "source_anchor": {"page": 7, "start_char": 0, "end_char": 16}},
            {"id": "dm_p7_b002", "text": "Optional figure note", "source_anchor": {"page": 7, "start_char": 17, "end_char": 35}},
        ],
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "dm_p7_b001",
                    "text": "Visible paragraph",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_visible",
                },
                {
                    "block_id": "dm_p7_b002",
                    "text": "Optional figure note",
                    "layout_type": "figure",
                    "layout_sub_type": "caption",
                    "layout_unique_id": "layout_omit",
                },
            ]
        },
        "omission_decisions": [
            {
                "decision_id": "omit_layout_omit",
                "decision": "hide",
                "reason": "auxiliary visual is not shown in this scheme",
                "recoverable": True,
                "target_layout_ids": ["layout_omit"],
                "target_block_ids": ["p7_dm_p7_b002"],
                "target_atom_ids": [],
            }
        ],
    }
    ui_plan = {
        "components": [
            {
                "id": "visible_node",
                "type": "ParagraphProse",
                "props": {"text": "Visible paragraph"},
                "children": [],
                "source_anchor_refs": [],
                "source_block_ids": ["p7_dm_p7_b001"],
            }
        ],
        "trace_meta": {
            "omission_decisions": list(payload["omission_decisions"]),
        },
    }

    report = service._enforce_no_drop_blocks_fallback(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        ui_plan=ui_plan,
    )

    assert report["triggered"] is False
    assert report["missing_block_ids"] == []
    assert report["omitted_block_ids"] == ["p7_dm_p7_b002"]


@pytest.mark.asyncio
async def test_apply_review_patch_should_create_next_snapshot_with_local_ui_ops(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    base_snapshot = {
        "session_id": "sess_demo",
        "snapshot_id": "snapshot_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_base",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "Original paragraph"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow Stack", "rationale": "", "source": "panel_plan", "candidate_ids": ["reading_flow_stack"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "reading_flow_stack"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_demo&snapshotId=snapshot_base",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
    }
    state = compose_module.ReaderComposeAgentState()
    state.push({"plan_id": "snapshot_base", **base_snapshot})
    service._review_sessions["sess_demo"] = {
        "session_id": "sess_demo",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_base",
        "snapshot_order": ["snapshot_base"],
        "snapshots": {"snapshot_base": base_snapshot},
        "state": state,
        "expires_at": 9999999999.0,
    }

    updated = await service.apply_review_patch(
        session_id="sess_demo",
        snapshot_id="snapshot_base",
        ui_ops=[
            {
                "op": "update_component_props",
                "component_id": "n1",
                "props_patch": {"text": "Patched paragraph"},
            }
        ],
        decision_log_append=["tightened body spacing"],
        omission_decisions=[
            {
                "decision_id": "omit_aux",
                "decision": "collapse",
                "reason": "secondary context kept recoverable",
                "recoverable": True,
                "target_layout_ids": ["layout_aux"],
                "target_block_ids": [],
                "target_atom_ids": [],
            }
        ],
        scheme_choice={
            "scheme_id": "context_rail",
            "label": "Context Rail",
            "rationale": "review patch switched scheme",
            "source": "review_patch",
            "candidate_ids": ["reading_flow_stack", "context_rail"],
        },
        note="review round 2",
    )

    assert updated["revision"] == 2
    assert updated["parent_snapshot_id"] == "snapshot_base"
    assert updated["ui_plan"]["components"][0]["props"]["text"] == "Patched paragraph"
    assert updated["decision_log"][-1] == "tightened body spacing"
    assert updated["scheme_choice"]["scheme_id"] == "context_rail"
    assert updated["omission_decisions"][0]["decision"] == "collapse"
    assert updated["ui_plan"]["trace_meta"]["review_note"] == "review round 2"
    assert service._review_sessions["sess_demo"]["latest_snapshot_id"] == updated["snapshot_id"]


@pytest.mark.asyncio
async def test_create_review_session_should_clone_exact_cache_without_recompute(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    cached_payload = {
        "paper_id": 78,
        "page": 7,
        "status": "done",
        "source_signature": "sig-exact",
        "build_mode": "compose_agent_single_agent_v2",
        "ui_plan": {
            "plan_id": "plan_cached",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "Cached paragraph"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.91, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack"},
        "decision_log": ["cached compose"],
        "omission_decisions": [],
        "page_structure_v3": {"block_groups": []},
    }

    async def _build_source_signature(**_kwargs):
        return "sig-exact"

    async def _read_payload_from_redis(_key):
        return dict(cached_payload)

    async def _read_payload_from_db(**_kwargs):
        raise AssertionError("db cache should not be used when exact redis cache exists")

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _should_not_build(**_kwargs):
        raise AssertionError("full compose should not run when cloning exact cache for review")

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "build_or_get_composed_payload", _should_not_build)

    snapshot = await service.create_review_session(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78),
        page=7,
        selected_kb_id=84,
        force_refresh=False,
        regenerate=False,
        latency_budget_ms=None,
        quality_target=None,
        max_iterations=None,
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        citation_tldr=False,
        snapshot_label="snapshot_cached",
    )

    assert snapshot["snapshot_id"] == "snapshot_cached"
    assert snapshot["source_signature"] == "sig-exact"
    assert snapshot["ui_plan"]["components"][0]["props"]["text"] == "Cached paragraph"


@pytest.mark.asyncio
async def test_create_review_session_should_fallback_to_latest_db_cache_before_recompute(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    latest_payload = {
        "paper_id": 78,
        "page": 7,
        "status": "done",
        "source_signature": "sig-latest",
        "build_mode": "compose_agent_single_agent_v2",
        "ui_plan": {
            "plan_id": "plan_latest",
            "components": [
                {
                    "id": "n_latest",
                    "type": "ParagraphProse",
                    "props": {"text": "Latest cached paragraph"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b010"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.88, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack"},
        "decision_log": ["latest compose"],
        "omission_decisions": [],
        "page_structure_v3": {"block_groups": []},
    }

    async def _build_source_signature(**_kwargs):
        return "sig-requested"

    async def _read_payload_from_redis(_key):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _read_latest_payload_from_db(**_kwargs):
        return dict(latest_payload)

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _should_not_build(**_kwargs):
        raise AssertionError("full compose should not run when latest page cache can seed review session")

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_read_latest_payload_from_db", _read_latest_payload_from_db)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "build_or_get_composed_payload", _should_not_build)

    snapshot = await service.create_review_session(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78),
        page=7,
        selected_kb_id=84,
        force_refresh=False,
        regenerate=False,
        latency_budget_ms=None,
        quality_target=None,
        max_iterations=None,
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        citation_tldr=False,
        snapshot_label="snapshot_latest",
    )

    assert snapshot["snapshot_id"] == "snapshot_latest"
    assert snapshot["source_signature"] == "sig-latest"
    assert snapshot["ui_plan"]["components"][0]["id"] == "n_latest"


@pytest.mark.asyncio
async def test_create_review_session_from_payload_should_import_without_recompute(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    imported_payload = {
        "paper_id": 78,
        "page": 7,
        "status": "done",
        "source_signature": "sig-import",
        "build_mode": "manual_import",
        "ui_plan": {
            "plan_id": "plan_import",
            "components": [
                {
                    "id": "n_import",
                    "type": "CalloutBox",
                    "props": {"type": "info", "title": "Imported", "content": "Manual payload"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.95, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack"},
        "decision_log": ["imported payload"],
        "omission_decisions": [],
        "page_structure_v3": {"block_groups": []},
    }

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)

    snapshot = await service.create_review_session_from_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78),
        payload=imported_payload,
        snapshot_label="snapshot_import",
    )

    assert snapshot["snapshot_id"] == "snapshot_import"
    assert snapshot["source_signature"] == "sig-import"
    assert snapshot["ui_plan"]["components"][0]["type"] == "CalloutBox"
    assert snapshot["decision_log"] == ["imported payload"]


@pytest.mark.asyncio
async def test_record_review_observation_should_attach_render_context(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)
    monkeypatch.setattr(compose_module, "REVIEW_OBSERVATION_DIR", str(tmp_path))

    base_snapshot = {
        "session_id": "sess_obs",
        "snapshot_id": "snapshot_obs_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_obs",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "Observation target"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow Stack", "rationale": "", "source": "panel_plan", "candidate_ids": ["reading_flow_stack"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "reading_flow_stack"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_obs&snapshotId=snapshot_obs_base",
        "render_image_url": "",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {"block_groups": [{"block_id": "p7_dm_p7_b001", "text": "Observation target"}]},
        "layout_advice_v3": {},
    }
    service._review_sessions["sess_obs"] = {
        "session_id": "sess_obs",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_obs_base",
        "snapshot_order": ["snapshot_obs_base"],
        "snapshots": {"snapshot_obs_base": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    stored = await service.store_review_observation_image(
        session_id="sess_obs",
        snapshot_id="snapshot_obs_base",
        filename="review.png",
        content_type="image/png",
        data=b"fake-render-image",
    )

    updated = await service.record_review_observation(
        session_id="sess_obs",
        snapshot_id="snapshot_obs_base",
        render_image_url=stored["render_image_url"],
        render_image_path=stored["file_path"],
        render_image_media_type=stored["media_type"],
        diagnostics=[
            {
                "code": "crowded_panel",
                "severity": "warn",
                "message": "Main panel is too crowded.",
                "component_ids": ["n1"],
                "meta": {"density": 0.92},
            }
        ],
        note="manual review on render snapshot",
        source="assistant_observer",
    )

    assert updated["render_image_url"].endswith("/api/v1/literature/papers/78/reader/composed/review-session/sess_obs/observation-image/snapshot_obs_base")
    assert os.path.exists(stored["file_path"])
    assert updated["observation_note"] == "manual review on render snapshot"
    assert updated["observation_source"] == "assistant_observer"
    assert updated["observation_diagnostics"][0]["code"] == "crowded_panel"
    assert any(row.get("code") == "crowded_panel" for row in updated["diagnostics"])
    resolved = await service.resolve_review_observation_image(
        session_id="sess_obs",
        snapshot_id="snapshot_obs_base",
    )
    assert resolved is not None
    assert resolved["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_apply_overlay_for_user_should_prefer_published_review_snapshot(monkeypatch):
    service = LiteratureReaderComposeService()

    async def _fake_read_overlays_from_db(**_kwargs):
        return [
            SimpleNamespace(
                action_type=compose_module.REVIEW_PUBLISH_OVERLAY_ACTION,
                node_id=compose_module.REVIEW_PUBLISH_OVERLAY_NODE_ID,
                overlay_json={
                    "session_id": "sess_publish",
                    "snapshot_id": "snapshot_publish",
                    "published_at": "2026-03-06T12:00:00Z",
                    "ui_plan": {
                        "plan_id": "plan_published",
                        "components": [
                            {
                                "id": "n_pub",
                                "type": "ParagraphProse",
                                "props": {"text": "Published body"},
                                "children": [],
                                "source_anchor_refs": [],
                                "source_block_ids": ["p7_dm_p7_b001"],
                            }
                        ],
                        "layout": {},
                        "style_tokens": {},
                        "trace_meta": {},
                    },
                    "scheme_choice": {"scheme_id": "context_rail"},
                    "decision_log": ["published from review"],
                    "omission_decisions": [
                        {
                            "decision_id": "omit_demo",
                            "decision": "hide",
                            "reason": "demo omit",
                            "recoverable": True,
                            "target_layout_ids": ["layout_x"],
                            "target_block_ids": [],
                            "target_atom_ids": [],
                        }
                    ],
                },
            )
        ]

    monkeypatch.setattr(service, "_read_overlays_from_db", _fake_read_overlays_from_db)

    payload = {
        "ui_plan": {
            "plan_id": "plan_base",
            "components": [
                {
                    "id": "n_base",
                    "type": "ParagraphProse",
                    "props": {"text": "Base body"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b000"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "scheme_choice": {"scheme_id": "reading_flow_stack"},
        "decision_log": ["base compose"],
        "omission_decisions": [],
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_b001",
                    "text": "Published body",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_main",
                }
            ]
        },
    }

    updated = await service._apply_overlay_for_user(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper_id=78,
        page=7,
        source_signature="sig-demo",
        payload=payload,
    )

    assert updated["overlay_applied"] is True
    assert updated["overlay_count"] == 1
    assert updated["ui_plan"]["components"][0]["props"]["text"] == "Published body"
    assert updated["scheme_choice"]["scheme_id"] == "context_rail"
    assert updated["decision_log"] == ["published from review"]
    assert updated["omission_decisions"][0]["decision"] == "hide"
    assert updated["ui_plan"]["trace_meta"]["published_review_applied"] is True


@pytest.mark.asyncio
async def test_get_review_snapshot_should_repair_legacy_truncated_figure_caption(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    base_snapshot = {
        "session_id": "sess_fig",
        "snapshot_id": "snapshot_fig_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_fig",
            "components": [
                {
                    "id": "n_fig",
                    "type": "FigurePanel",
                    "props": {
                        "caption": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, AI outputs were adjudicated",
                        "image_url": "asset:layout_fig",
                        "source_label": "",
                    },
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_fig_img", "p7_fig_cap1"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "figure_focus_split", "label": "Figure Focus Split", "rationale": "", "source": "panel_plan", "candidate_ids": ["figure_focus_split"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "figure_focus_split"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_fig&snapshotId=snapshot_fig_base",
        "render_image_url": "",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_fig_img",
                    "text": "A B Answer-Explanation Concordance",
                    "layout_type": "figure",
                    "layout_sub_type": "picture",
                    "layout_unique_id": "layout_fig",
                },
                {
                    "block_id": "p7_fig_cap1",
                    "text": "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3,",
                    "layout_type": "figure_name",
                    "layout_sub_type": "none",
                    "layout_unique_id": "layout_fig_caption",
                },
                {
                    "block_id": "p7_fig_cap2",
                    "text": "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams.",
                    "layout_type": "figure_name",
                    "layout_sub_type": "none",
                    "layout_unique_id": "layout_fig_caption",
                },
            ]
        },
        "layout_advice_v3": {"ordered_block_ids": ["p7_fig_img", "p7_fig_cap1", "p7_fig_cap2"]},
    }
    service._review_sessions["sess_fig"] = {
        "session_id": "sess_fig",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_fig_base",
        "snapshot_order": ["snapshot_fig_base"],
        "snapshots": {"snapshot_fig_base": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    resolved = await service.get_review_snapshot(
        session_id="sess_fig",
        snapshot_id="snapshot_fig_base",
    )

    props = dict(((resolved or {}).get("ui_plan") or {}).get("components", [{}])[0].get("props") or {})
    assert str(props.get("caption") or "") == (
        "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
        "AI outputs were adjudicated on concordance and density of insight (DOI) across all exams."
    )
    assert str(props.get("source_label") or "") == "Fig 3"


@pytest.mark.asyncio
async def test_publish_review_snapshot_should_save_published_review_overlay(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)
    captured: list[dict] = []

    async def _fake_upsert_overlay_to_db(**kwargs):
        captured.append(dict(kwargs))
        return None

    async def _fake_build_source_signature(**_kwargs):
        return "sig-auto"

    monkeypatch.setattr(service, "_upsert_overlay_to_db", _fake_upsert_overlay_to_db)
    monkeypatch.setattr(service, "_build_source_signature", _fake_build_source_signature)

    base_snapshot = {
        "session_id": "sess_publish",
        "snapshot_id": "snapshot_publish",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-publish",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_publish",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "Published text"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow Stack", "rationale": "", "source": "panel_plan", "candidate_ids": ["reading_flow_stack"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [],
        "phase1_compact_input": {},
        "render_route": "/literature/78/read/review?sessionId=sess_publish&snapshotId=snapshot_publish",
        "render_image_url": "",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_b001",
                    "text": "Published text",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "layout_unique_id": "layout_main",
                }
            ]
        },
        "layout_advice_v3": {},
    }
    service._review_sessions["sess_publish"] = {
        "session_id": "sess_publish",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-publish",
        "latest_snapshot_id": "snapshot_publish",
        "snapshot_order": ["snapshot_publish"],
        "snapshots": {"snapshot_publish": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    result = await service.publish_review_snapshot(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78),
        session_id="sess_publish",
        snapshot_id="snapshot_publish",
        note="publish for main read page",
    )

    assert result["published"] is True
    assert result["source_signature"] == "sig-auto"
    assert len(captured) == 2
    assert {row["source_signature"] for row in captured} == {"sig-publish", "sig-auto"}
    assert all(row["node_id"] == compose_module.REVIEW_PUBLISH_OVERLAY_NODE_ID for row in captured)
    assert all(row["action_type"] == compose_module.REVIEW_PUBLISH_OVERLAY_ACTION for row in captured)
    assert all(row["overlay_json"]["snapshot_id"] == "snapshot_publish" for row in captured)


def test_ensure_page_render_asset_should_return_asset_url_from_cached_file(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path))

    asset_dir = tmp_path / "78"
    asset_dir.mkdir(parents=True, exist_ok=True)
    cached = asset_dir / "page_7_r220_q92_v2.jpg"
    cached.write_bytes(b"cached-jpg")

    url = asyncio.run(
        service.ensure_page_render_asset(
            paper_id=78,
            page=7,
            pdf_path="D:/missing.pdf",
        )
    )

    assert url.endswith("/api/v1/literature/reader/page-assets/78/7")


def test_page_render_asset_filename_should_include_version():
    assert LiteratureReaderComposeService._page_render_asset_filename(page=7) == "page_7_r220_q92_v2.jpg"  # pylint: disable=protected-access


def test_find_existing_page_render_asset_path_should_ignore_legacy_filename_and_use_versioned_file(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path))

    asset_dir = tmp_path / "78"
    asset_dir.mkdir(parents=True, exist_ok=True)
    legacy = asset_dir / "page_7.jpg"
    legacy.write_bytes(b"legacy-jpg")
    assert service._find_existing_page_render_asset_path(paper_id=78, page=7) is None  # pylint: disable=protected-access

    versioned = asset_dir / "page_7_r220_q92_v2.jpg"
    versioned.write_bytes(b"versioned-jpg")
    found = service._find_existing_page_render_asset_path(paper_id=78, page=7)  # pylint: disable=protected-access
    assert str(found or "") == str(versioned)


def test_reader_compose_runtime_direct_review_content_should_ignore_data_urls():
    runtime = ReaderComposeAgentRuntime()

    content = runtime._build_direct_review_content(  # pylint: disable=protected-access
        prompt="review current layout",
        review_context={"render_image_url": "data:image/png;base64,ZmFrZQ=="},
        phase1_compact_input={
            "pdf_reference": {
                "page_image_url": "https://example.com/page7.jpg",
            }
        },
        include_images=True,
    )

    image_urls = [
        str(((item.get("image_url") or {}).get("url")) or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "image_url"
    ]

    assert image_urls == ["https://example.com/page7.jpg"]


def test_dashscope_multimodal_service_should_normalize_local_file_uri(tmp_path):
    image_path = tmp_path / "page7.png"
    image_path.write_bytes(b"fake-image")

    uris = DashScopeMultimodalService.collect_local_file_uris(str(image_path))

    assert len(uris) == 1
    assert uris[0].startswith("file://")
    assert "page7.png" in uris[0]


@pytest.mark.asyncio
async def test_reader_compose_runtime_should_use_reader_agent_provider_and_model(monkeypatch):
    captured: dict = {}

    class _FakeLLM:
        def __init__(self) -> None:
            self.config = {"model": "placeholder"}

    async def _fake_get_llm_service(provider=None):
        captured["provider"] = provider
        return _FakeLLM()

    monkeypatch.setattr(compose_module.settings, "reader_agent_provider", "aliyun")
    monkeypatch.setattr(compose_module.settings, "reader_agent_model", "qwen-3.5-plus")
    monkeypatch.setattr(
        __import__("app.services.reader_compose_agent_runtime", fromlist=["get_llm_service"]),
        "get_llm_service",
        _fake_get_llm_service,
    )

    runtime = ReaderComposeAgentRuntime()
    llm = await runtime._build_reader_llm()  # pylint: disable=protected-access

    assert captured["provider"] == "aliyun"
    assert llm.config["model"] == "qwen-3.5-plus"


def test_build_phase1_compact_input_should_keep_local_page_image_path():
    service = LiteratureReaderComposeService()

    payload = service._build_phase1_compact_input(  # pylint: disable=protected-access
        paper_id=78,
        page=7,
        rendered_page_image="https://example.com/page7.jpg",
        rendered_page_image_path="D:/tmp/page7.png",
        docmind_blocks=[
            {
                "layout_id": "layout_1",
                "reading_order": 1,
                "type": "paragraph",
                "source_text": "A compact paragraph",
            }
        ],
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    assert payload["pdf_reference"]["page_image_url"] == "https://example.com/page7.jpg"
    assert payload["pdf_reference"]["page_image_path"] == "D:/tmp/page7.png"


@pytest.mark.asyncio
async def test_reader_panel_plan_agent_should_prefer_dashscope_local_image(monkeypatch, tmp_path):
    service = ReaderPanelPlanAgentService()
    image_path = tmp_path / "page7.png"
    image_path.write_bytes(b"fake-image")
    captured = {"dashscope": 0, "tool": 0}

    async def _fake_dashscope(**_kwargs):
        captured["dashscope"] += 1
        return {
            "panel_plan": {
                "schema_version": "panel_plan_v2",
                "creative_direction": "dashscope_local_file",
                "style_plan": {"scheme_id": "reading_flow_stack"},
                "decision_log": [],
                "coverage": {"omitted_layout_ids": [], "omitted_reason": ""},
                "panels": [
                    {
                        "panel_id": "panel_main",
                        "title": "",
                        "layout": {"type": "stack", "gap": 12},
                        "nodes": [
                            {
                                "node_id": "n1",
                                "component": "ParagraphProse",
                                "props": {"text": "Paragraph"},
                                "source_layout_ids": ["layout_1"],
                            }
                        ],
                    }
                ],
            }
        }, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def _fake_tool(**_kwargs):
        captured["tool"] += 1
        return {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    monkeypatch.setattr(compose_module.settings, "reader_agent_provider", "aliyun")
    monkeypatch.setattr(compose_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(compose_module.settings, "aliyun_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setattr(compose_module.settings, "reader_agent_model", "qwen-3.5-plus")
    monkeypatch.setattr(service, "_call_dashscope_json", _fake_dashscope)
    monkeypatch.setattr(service, "_call_tool", _fake_tool)
    monkeypatch.setattr(
        __import__("app.services.reader_panel_plan_agent_service", fromlist=["DashScopeMultimodalService"]),
        "DashScopeMultimodalService",
        SimpleNamespace(
            collect_local_file_uris=lambda *args, **kwargs: [image_path.as_uri()],
            is_available=lambda: True,
            chat_json=None,
        ),
    )

    result = await service.run(
        docmind_blocks=[{"layout_id": "layout_1", "source_text": "Paragraph"}],
        rendered_page_image="https://example.com/page7.jpg",
        rendered_page_image_path=str(image_path),
        component_whitelist=["ParagraphProse"],
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        max_rounds=1,
        phase1_context={"pdf_reference": {"page_image_path": str(image_path)}},
    )

    assert captured["dashscope"] == 1
    assert captured["tool"] == 0
    assert result["panel_plan"]["panels"][0]["nodes"][0]["source_layout_ids"] == ["layout_1"]


@pytest.mark.asyncio
async def test_reader_compose_runtime_should_prefer_dashscope_local_review(monkeypatch, tmp_path):
    runtime = ReaderComposeAgentRuntime()
    image_path = tmp_path / "review.png"
    image_path.write_bytes(b"fake-review")

    class _FakeLLM:
        @staticmethod
        def supports_function_calling():
            return False

    captured = {"dashscope": 0}

    async def _fake_dashscope(**kwargs):
        captured["dashscope"] += 1
        assert kwargs["review_context"]["render_image_path"] == str(image_path)
        return {
            "used": True,
            "model": "qwen-3.5-plus",
            "ui_ops": [
                {
                    "op": "update_component_props",
                    "component_id": "n1",
                    "props_patch": {"text": "Patched locally"},
                }
            ],
            "agent_trace": [],
            "agent_tool_calls": [],
            "agent_summary": "patched via dashscope",
            "fallback_reason": "",
        }

    monkeypatch.setattr(compose_module.settings, "reader_agent_provider", "aliyun")
    monkeypatch.setattr(runtime, "_run_dashscope_local_review_patch", _fake_dashscope)
    monkeypatch.setattr(
        __import__("app.services.reader_compose_agent_runtime", fromlist=["DashScopeMultimodalService"]),
        "DashScopeMultimodalService",
        SimpleNamespace(
            collect_local_file_uris=lambda *args, **kwargs: [image_path.as_uri()],
            is_available=lambda: True,
        ),
    )

    result = await runtime._run_direct_review_patch(  # pylint: disable=protected-access
        llm=_FakeLLM(),
        prompt="review this page",
        existing_component_ids=["n1"],
        valid_block_ids={"p7_dm_p7_b001"},
        review_context={"render_image_path": str(image_path)},
        phase1_compact_input={},
    )

    assert captured["dashscope"] == 1
    assert result["used"] is True
    assert result["ui_ops"][0]["props_patch"]["text"] == "Patched locally"


@pytest.mark.asyncio
async def test_auto_patch_review_snapshot_should_apply_runtime_ui_ops(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    base_snapshot = {
        "session_id": "sess_auto",
        "snapshot_id": "snapshot_auto_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_auto",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "Original paragraph"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_b001"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow Stack", "rationale": "", "source": "panel_plan", "candidate_ids": ["reading_flow_stack"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [
            {
                "code": "overlong_paragraph_nodes",
                "severity": "warn",
                "message": "Paragraph should be shortened.",
                "component_ids": ["n1"],
                "meta": {},
            }
        ],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "reading_flow_stack"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_auto&snapshotId=snapshot_auto_base",
        "render_image_url": "http://localhost:8888/api/v1/literature/papers/78/reader/composed/review-session/sess_auto/observation-image/snapshot_auto_base",
        "render_image_path": "",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_b001",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "reading_order": 1,
                    "text": "Original paragraph",
                }
            ]
        },
        "layout_advice_v3": {"ordered_block_ids": ["p7_dm_p7_b001"]},
    }
    service._review_sessions["sess_auto"] = {
        "session_id": "sess_auto",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_auto_base",
        "snapshot_order": ["snapshot_auto_base"],
        "snapshots": {"snapshot_auto_base": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    async def _runtime(**_kwargs):
        return {
            "used": True,
            "ui_ops": [
                {
                    "op": "update_component_props",
                    "component_id": "n1",
                    "props_patch": {"text": "Patched from auto review"},
                }
            ],
            "agent_summary": "Shortened a crowded paragraph after render review.",
            "validation_errors": [],
            "fallback_reason": "",
        }

    monkeypatch.setattr(service._compose_agent_runtime, "run_component_assembly", _runtime)

    result = await service.auto_patch_review_snapshot(
        session_id="sess_auto",
        snapshot_id="snapshot_auto_base",
        user_id=1,
        user_intent="render review",
        note="auto pass 1",
    )

    assert result["patch_applied"] is True
    assert result["ui_ops_count"] == 1
    assert result["snapshot"]["ui_plan"]["components"][0]["props"]["text"] == "Patched from auto review"
    assert result["snapshot"]["ui_plan"]["trace_meta"]["auto_patch_agent_summary"].startswith("Shortened a crowded paragraph")
    assert service._review_sessions["sess_auto"]["latest_snapshot_id"] == result["snapshot"]["snapshot_id"]


@pytest.mark.asyncio
async def test_auto_patch_review_snapshot_should_synthesize_omission_for_removed_component(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    base_snapshot = {
        "session_id": "sess_auto_omit",
        "snapshot_id": "snapshot_auto_omit_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_auto_omit",
            "components": [
                {
                    "id": "n_doi_link",
                    "type": "ParagraphProse",
                    "props": {"text": "https://doi.org/10.1371/journal.pdig.0000198.g003"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": [],
                    "source_atom_ids": [],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {
                "panel_plan": {
                    "panels": [
                        {
                            "panel_id": "panel_figure",
                            "nodes": [
                                {
                                    "node_id": "n_doi_link",
                                    "component": "ParagraphProse",
                                    "props": {"text": "https://doi.org/10.1371/journal.pdig.0000198.g003"},
                                    "source_layout_ids": ["layout_doi"],
                                }
                            ],
                        }
                    ]
                }
            },
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "figure_focus_split", "label": "Figure Focus Split", "rationale": "", "source": "panel_plan", "candidate_ids": ["figure_focus_split"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "figure_focus_split"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_auto_omit&snapshotId=snapshot_auto_omit_base",
        "render_image_url": "http://localhost:8888/api/v1/literature/papers/78/reader/composed/review-session/sess_auto_omit/observation-image/snapshot_auto_omit_base",
        "render_image_path": "/tmp/review.png",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {
            "block_groups": [
                {
                    "layout_unique_id": "layout_doi",
                    "block_id": "p7_dm_p7_b002",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "reading_order": 2,
                    "text": "https://doi.org/10.1371/journal.pdig.0000198.g003",
                }
            ]
        },
        "layout_advice_v3": {"ordered_block_ids": ["p7_dm_p7_b002"]},
    }
    service._review_sessions["sess_auto_omit"] = {
        "session_id": "sess_auto_omit",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_auto_omit_base",
        "snapshot_order": ["snapshot_auto_omit_base"],
        "snapshots": {"snapshot_auto_omit_base": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    async def _runtime(**_kwargs):
        return {
            "used": True,
            "ui_ops": [
                {
                    "op": "remove_component",
                    "component_id": "n_doi_link",
                    "reason": "Hide DOI-only line after render review.",
                }
            ],
            "agent_summary": "Removed standalone DOI line.",
            "validation_errors": [],
            "fallback_reason": "",
        }

    monkeypatch.setattr(service._compose_agent_runtime, "run_component_assembly", _runtime)

    result = await service.auto_patch_review_snapshot(
        session_id="sess_auto_omit",
        snapshot_id="snapshot_auto_omit_base",
        user_id=1,
        user_intent="render review",
        note="auto omit",
    )

    assert result["patch_applied"] is True
    assert result["snapshot"]["omission_decisions"]
    assert result["snapshot"]["omission_decisions"][0]["target_layout_ids"] == ["layout_doi"]
    assert result["snapshot"]["omission_decisions"][0]["decision"] == "hide"


@pytest.mark.asyncio
async def test_auto_patch_review_snapshot_should_block_probable_page_continuation_removal(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    base_snapshot = {
        "session_id": "sess_auto_continue",
        "snapshot_id": "snapshot_auto_continue_base",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_single_agent_v2",
        "status": "done",
        "ui_plan": {
            "plan_id": "plan_auto_continue",
            "components": [
                {
                    "id": "n_text_1",
                    "type": "ParagraphProse",
                    "props": {"text": "adjudicator, as a second-year medical student for Step 1, fourth-year medical student for Step 2CK."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_l009_b001"],
                    "source_atom_ids": ["p7_dm_p7_l009_b001"],
                },
                {
                    "id": "n_text_2",
                    "type": "ParagraphProse",
                    "props": {"text": "We first examined the frequency of insight."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_dm_p7_l010_b001"],
                    "source_atom_ids": ["p7_dm_p7_l010_b001"],
                },
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "scheme_choice": {"scheme_id": "figure_focus_split", "label": "Figure Focus Split", "rationale": "", "source": "panel_plan", "candidate_ids": ["figure_focus_split"]},
        "decision_log": ["initial compose"],
        "omission_decisions": [],
        "diagnostics": [],
        "observation_diagnostics": [],
        "phase1_compact_input": {"scheme_catalog": [{"scheme_id": "figure_focus_split"}]},
        "render_route": "/literature/78/read/review?sessionId=sess_auto_continue&snapshotId=snapshot_auto_continue_base",
        "render_image_url": "http://localhost:8888/api/v1/literature/papers/78/reader/composed/review-session/sess_auto_continue/observation-image/snapshot_auto_continue_base",
        "render_image_path": "/tmp/review.png",
        "docmind_page_image_url": "",
        "style_intent": "journal",
        "theme_mode": "light",
        "detail_level": "standard",
        "parent_snapshot_id": None,
        "revision": 1,
        "created_at": "2026-03-06T00:00:00Z",
        "page_structure_v3": {
            "block_groups": [
                {
                    "layout_unique_id": "layout_continue",
                    "block_id": "p7_dm_p7_l009_b001",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "reading_order": 1,
                    "text": "adjudicator, as a second-year medical student for Step 1, fourth-year medical student for Step 2CK.",
                },
                {
                    "layout_unique_id": "layout_main",
                    "block_id": "p7_dm_p7_l010_b001",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "reading_order": 2,
                    "text": "We first examined the frequency of insight.",
                },
            ]
        },
        "layout_advice_v3": {"ordered_block_ids": ["p7_dm_p7_l009_b001", "p7_dm_p7_l010_b001"]},
    }
    service._review_sessions["sess_auto_continue"] = {
        "session_id": "sess_auto_continue",
        "paper_id": 78,
        "page": 7,
        "source_signature": "sig-demo",
        "latest_snapshot_id": "snapshot_auto_continue_base",
        "snapshot_order": ["snapshot_auto_continue_base"],
        "snapshots": {"snapshot_auto_continue_base": base_snapshot},
        "state": compose_module.ReaderComposeAgentState(),
        "expires_at": 9999999999.0,
    }

    async def _runtime(**_kwargs):
        return {
            "used": True,
            "ui_ops": [
                {
                    "op": "remove_component",
                    "component_id": "n_text_1",
                    "reason": "Looks like a carry-over fragment.",
                }
            ],
            "agent_summary": "Attempted to remove carry-over fragment.",
            "validation_errors": [],
            "fallback_reason": "",
        }

    monkeypatch.setattr(service._compose_agent_runtime, "run_component_assembly", _runtime)

    result = await service.auto_patch_review_snapshot(
        session_id="sess_auto_continue",
        snapshot_id="snapshot_auto_continue_base",
        user_id=1,
        user_intent="render review",
        note="block continuation removal",
    )

    assert result["patch_applied"] is False
    assert result["fallback_reason"] == "no_review_patch"
    assert "blocked_probable_page_continuation_removal" in result["validation_errors"]


@pytest.mark.asyncio
async def test_cached_simplified_fallback_should_bypass_cache_and_rebuild(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "simplified_v2")
    monkeypatch.setattr(service, "_is_single_agent_v2_enabled", lambda **_kwargs: True)

    rebuild_calls = {"count": 0}

    async def _build_source_signature(**_kwargs):
        return "sig-rebuild-fallback"

    async def _read_payload_from_redis(_key):
        return {
            "paper_id": 82,
            "page": 1,
            "status": "fallback",
            "degraded_reason": "simplified_pipeline",
            "build_mode": "compose_agent_simplified",
            "minimal_gate_report": {
                "passed": False,
                "used_atom_count": 0,
                "usable_atom_count": 42,
            },
        }

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _acquire_lock(_lock_key):
        return "lock-token"

    async def _release_lock(_lock_key, _token):
        return None

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _reader_payload(**_kwargs):
        return (
            {
                "blocks": [
                    {
                        "id": "p1_b1",
                        "text": "Recovered paragraph",
                        "source_anchor": {"page": 1, "start_char": 0, "end_char": 18},
                    }
                ],
                "assets": [],
            },
            SimpleNamespace(),
        )

    async def _fake_single_agent_v2_result(**_kwargs):
        rebuild_calls["count"] += 1
        return {
            "base_payload": {
                "minimal_gate_report": {
                    "passed": True,
                    "schema_valid": True,
                    "whitelist_valid": True,
                    "layout_contract": True,
                    "ownership_unchanged": True,
                    "full_coverage": True,
                    "non_empty_plan_for_non_empty_input": True,
                    "source_text_immutable": True,
                    "used_atom_count": 1,
                    "usable_atom_count": 1,
                },
            },
            "loop_result": {
                "build_mode": "compose_agent_single_agent_v2",
                "ui_plan": {
                    "plan_id": "plan_rebuilt",
                    "components": [
                        {
                            "id": "rebuilt_001",
                            "type": "ParagraphProse",
                            "props": {"text": "Recovered paragraph"},
                            "children": [],
                            "source_anchor_refs": [],
                            "source_block_ids": ["p1_b1"],
                        }
                    ],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {"overall": 0.92, "hard_constraints_passed": True},
                "iteration_trace": [],
                "iterations": 1,
                "degraded": False,
                "stop_reason": "single_agent_v2_done",
            },
            "assets": [],
        }

    async def _no_db_upsert(**_kwargs):
        return None

    async def _no_redis_write(_key, _payload):
        return None

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _acquire_lock)
    monkeypatch.setattr(service, "_release_lock", _release_lock)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "_upsert_payload_to_db", _no_db_upsert)
    monkeypatch.setattr(service, "_write_payload_to_redis", _no_redis_write)
    monkeypatch.setattr(service, "_partition_main_aux_block_ids", lambda **_kwargs: (["p1_b1"], []))
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _reader_payload)
    monkeypatch.setattr(service, "_build_single_agent_v2_result", _fake_single_agent_v2_result)

    payload, _ = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=82, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=False,
    )

    assert rebuild_calls["count"] == 1
    assert str(payload.get("status") or "") == "done"
    assert str(payload.get("build_mode") or "") == "compose_agent_single_agent_v2"


@pytest.mark.asyncio
async def test_build_or_get_composed_payload_should_reuse_compatible_db_cache_before_rebuild(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "simplified_v2")

    compatible_payload = {
        "paper_id": 78,
        "page": 7,
        "status": "fallback",
        "degraded_reason": "simplified_pipeline",
        "build_mode": "compose_agent_simplified",
        "source_signature": (
            "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|"
            "pv:simplified_v2|mode:auto/light/standard/0/0|h:oldhashvalue123456789012"
        ),
        "minimal_gate_report": {
            "used_atom_count": 0,
            "usable_atom_count": 20,
        },
        "ui_plan": {
            "plan_id": "plan_cached",
            "components": [
                {
                    "id": "fig-1",
                    "type": "FigurePanel",
                    "props": {"image_url": "/fig.png", "caption": "Fig 3"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["b_fig"],
                },
                {
                    "id": "p-1",
                    "type": "ParagraphProse",
                    "props": {"text": "We first examined the frequency of insight."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["b_text"],
                },
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.88, "validation_errors": []},
        "assets": [],
        "page_structure_v3": {"block_groups": []},
    }
    writes = {"redis": 0}

    async def _build_source_signature(**_kwargs):
        return (
            "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|"
            "pv:simplified_v2|mode:auto/light/standard/0/0|h:newhashvalue123456789012"
        )

    async def _read_payload_from_redis(_key):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _read_compatible_payload_from_db(**_kwargs):
        return dict(compatible_payload)

    async def _write_payload_to_redis(_key, _payload):
        writes["redis"] += 1

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _should_not_acquire_lock(_key):
        raise AssertionError("compatible db cache should avoid rebuild lock path")

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_read_compatible_payload_from_db", _read_compatible_payload_from_db)
    monkeypatch.setattr(service, "_write_payload_to_redis", _write_payload_to_redis)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "_acquire_lock", _should_not_acquire_lock)

    payload, meta = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78, user_id=1, title="demo", pdf_path=""),
        page=7,
        selected_kb_id=84,
        force_refresh=False,
        regenerate=False,
        style_intent="auto",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        citation_tldr=False,
    )

    assert writes["redis"] == 1
    assert meta.cache_hit is True
    assert meta.cache_layer == "db_compatible"
    assert payload["source_signature"].endswith("h:newhashvalue123456789012")
    assert len(payload["ui_plan"]["components"]) == 2


def test_cleanup_compose_sibling_caches_should_delete_same_prefix_redis_siblings_and_db_rows(monkeypatch):
    service = LiteratureReaderComposeService()
    current_key = "lit:reader:compose:v1:v2:single_agent_v2:layout_uid_v1:u1:p86:pg13:newhash"
    sibling_key = "lit:reader:compose:v1:v2:single_agent_v2:layout_uid_v1:u1:p86:pg13:oldhash"
    other_page_key = "lit:reader:compose:v1:v2:single_agent_v2:layout_uid_v1:u1:p86:pg14:otherhash"
    other_mode_key = "lit:reader:compose:v1:v2:semantic_atom_pipeline:layout_uid_v1:u1:p86:pg13:modehash"

    class _FakeRedis:
        def __init__(self):
            self.store = {
                current_key: "current",
                sibling_key: "sibling",
                other_page_key: "other_page",
                other_mode_key: "other_mode",
            }
            self.deleted_keys = []
            self.scans = []

        async def scan(self, cursor=0, match=None, count=None):
            self.scans.append((cursor, match, count))
            prefix = str(match or "")
            if prefix.endswith("*"):
                prefix = prefix[:-1]
            keys = [key for key in self.store if key.startswith(prefix)]
            return 0, keys

        async def delete(self, *keys):
            deleted = 0
            for key in keys:
                self.deleted_keys.append(key)
                if key in self.store:
                    deleted += 1
                    self.store.pop(key, None)
            return deleted

    class _FakeDb:
        def __init__(self):
            self.statements = []
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, stmt):
            self.statements.append(stmt)
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    fake_redis = _FakeRedis()
    fake_db = _FakeDb()

    async def _resolve_redis_client():
        return fake_redis

    monkeypatch.setattr(service, "_resolve_redis_client", _resolve_redis_client)

    report = asyncio.run(
        service._cleanup_compose_sibling_caches(  # pylint: disable=protected-access
            db=fake_db,
            redis_key=current_key,
            paper_id=86,
            page=13,
            source_signature="newhash",
            pipeline_mode="single_agent_v2",
            pipeline_version="layout_uid_v1",
        )
    )

    assert report["redis_deleted"] == 1
    assert report["db_deleted"] == 2
    assert current_key in fake_redis.store
    assert sibling_key not in fake_redis.store
    assert other_page_key in fake_redis.store
    assert other_mode_key in fake_redis.store
    assert len(fake_db.statements) == 1
    compiled_sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "paper_reader_page_caches" in compiled_sql
    assert "paper_id = 86" in compiled_sql
    assert "page = 13" in compiled_sql
    assert "source_signature" in compiled_sql
    assert "newhash" in compiled_sql
    assert "!=" in compiled_sql or "<>" in compiled_sql
    assert fake_db.commits == 1


def test_build_or_get_composed_payload_should_cleanup_sibling_caches_only_after_successful_force_refresh(monkeypatch):
    service = LiteratureReaderComposeService()
    cleanup_calls = {"count": 0}
    build_state = {"passed": True}

    async def _build_source_signature(**_kwargs):
        return "compose_v3|p:86|kb:84|m:1773304814|s:2215244|pm:single_agent_v2|pv:layout_uid_v1|mode:auto/light/standard/0/0|h:newhash"

    async def _read_payload_from_redis(**_kwargs):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _read_compatible_payload_from_db(**_kwargs):
        return None

    async def _acquire_lock(_lock_key):
        return "lock-token"

    async def _maintain_lock(**_kwargs):
        return None

    async def _release_lock(*_args, **_kwargs):
        return None

    async def _build_page_payload(**_kwargs):
        return (
            {
            "paper_id": 86,
            "page": 13,
            "grounding_mode": "grounded",
            "evidence_enabled": False,
            "runtime_build_plan_evidence": False,
            "page_grounding_policy": {"mode": "grounded", "page_mode": "grounded"},
            "page_grounding_v1": {
                "layout_atoms": [
                    {
                        "layout_id": "layout-1",
                        "node_kind": "paragraph",
                        "clean_text": "Grounded paragraph.",
                        "normalized_text": "Grounded paragraph.",
                    }
                ],
                "page_image": {"url": "/api/v1/literature/reader/page-assets/86/13", "path": ""},
            },
            "docmind_structure": {"page_image_url": "/api/v1/literature/reader/page-assets/86/13"},
            "ui_plan": {"components": [], "layout": {}, "style_tokens": {}, "trace_meta": {}},
            "minimal_gate_report": {},
            },
            SimpleNamespace(),
        )

    async def _build_layout_uid_pipeline_result(**kwargs):
        base_payload = dict(kwargs.get("base_payload") or {})
        page = int(kwargs.get("page") or 13)
        status = "done" if build_state["passed"] else "fallback"
        return {
            "base_payload": base_payload,
            "loop_result": {
                "build_mode": "compose_ai_reconstructed",
                "status": status,
                "ui_plan": {
                    "plan_id": f"ai_reconstructed_p{page}",
                    "components": [
                        {
                            "id": "component-1",
                            "type": "ParagraphProse",
                            "props": {"text": "Reconstructed paragraph."},
                            "children": [],
                            "source_anchor_refs": [],
                            "source_block_ids": ["p13_b1"],
                        }
                    ],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {
                    "overall": 0.91 if build_state["passed"] else 0.42,
                    "validation_errors": [] if build_state["passed"] else ["fallback"],
                    "iterations": 1,
                    "degraded": not build_state["passed"],
                    "stop_reason": "layout_uid_v1_done" if build_state["passed"] else "validator_non_converged",
                },
                "iterations": 1,
                "degraded": not build_state["passed"],
                "stop_reason": "layout_uid_v1_done" if build_state["passed"] else "validator_non_converged",
                "node_gate_report": {},
                "iteration_trace": [],
            },
            "assets": [],
        }

    def _build_validation_report(**_kwargs):
        return {
            "passed": build_state["passed"],
            "gates": {"non_empty_plan_for_non_empty_input": {"passed": build_state["passed"], "errors": []}},
            "errors": [] if build_state["passed"] else ["fallback"],
        }

    def _partition_main_aux_block_ids(**_kwargs):
        return ["p13_b1"], []

    async def _upsert_payload_to_db(**_kwargs):
        return None

    async def _write_payload_to_redis(*_args, **_kwargs):
        return None

    async def _apply_overlay_for_user(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _cleanup_compose_sibling_caches(**_kwargs):
        cleanup_calls["count"] += 1
        return {"redis_deleted": 1, "db_deleted": 1}

    def _build_initial_ui_plan(**_kwargs):
        return {"components": [], "layout": {}, "style_tokens": {}, "trace_meta": {}}

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_read_compatible_payload_from_db", _read_compatible_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _acquire_lock)
    monkeypatch.setattr(service, "_maintain_lock", _maintain_lock)
    monkeypatch.setattr(service, "_release_lock", _release_lock)
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _build_page_payload)
    monkeypatch.setattr(service, "_build_layout_uid_pipeline_result", _build_layout_uid_pipeline_result)
    monkeypatch.setattr(service, "_build_validation_report", _build_validation_report)
    monkeypatch.setattr(service, "_partition_main_aux_block_ids", _partition_main_aux_block_ids)
    monkeypatch.setattr(service, "_upsert_payload_to_db", _upsert_payload_to_db)
    monkeypatch.setattr(service, "_write_payload_to_redis", _write_payload_to_redis)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay_for_user)
    monkeypatch.setattr(service, "_build_initial_ui_plan", _build_initial_ui_plan)
    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(service, "_cleanup_compose_sibling_caches", _cleanup_compose_sibling_caches)

    paper = SimpleNamespace(id=86, user_id=1, title="demo", pdf_path="")

    payload, meta = asyncio.run(
        service.build_or_get_composed_payload(
            db=SimpleNamespace(),
            user_id=1,
            paper=paper,
            page=13,
            selected_kb_id=84,
            force_refresh=True,
            regenerate=False,
            style_intent=None,
            theme_mode=None,
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
        )
    )

    assert cleanup_calls["count"] == 1
    assert str(payload.get("status") or "") == "done"
    assert meta.cache_hit is False
    assert meta.degraded is False

    build_state["passed"] = False
    payload2, meta2 = asyncio.run(
        service.build_or_get_composed_payload(
            db=SimpleNamespace(),
            user_id=1,
            paper=paper,
            page=13,
            selected_kb_id=84,
            force_refresh=True,
            regenerate=False,
            style_intent=None,
            theme_mode=None,
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
        )
    )

    assert cleanup_calls["count"] == 1
    assert str(payload2.get("status") or "") == "fallback"
    assert meta2.degraded is True


@pytest.mark.asyncio
async def test_read_compatible_payload_from_db_should_ignore_other_pipeline_version(monkeypatch):
    service = LiteratureReaderComposeService()
    compatible_payload = {
        "paper_id": 78,
        "page": 7,
        "status": "done",
        "build_mode": "compose_agent_simplified",
        "engine_version": "reader_compose_v8",
        "pipeline_version": "simplified_v2",
        "source_signature": (
            "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|"
            "pv:simplified_v2|mode:auto/light/standard/0/0|h:oldhashvalue123456789012"
        ),
        "ui_plan": {"plan_id": "plan_cached", "components": []},
    }

    class _Rows:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return list(self._items)

    class _Db:
        async def execute(self, _stmt):
            return _Rows(
                [
                    SimpleNamespace(
                        source_signature=str(compatible_payload.get("source_signature") or ""),
                        payload_json=dict(compatible_payload),
                    )
                ]
            )

    payload = await service._read_compatible_payload_from_db(  # pylint: disable=protected-access
        db=_Db(),
        paper_id=78,
        page=7,
        source_signature=(
            "compose_v3|p:78|kb:84|m:1772896437|s:1065400|pm:single_agent_v2|"
            "pv:layout_uid_v1|mode:auto/light/standard/0/0|h:newhashvalue123456789012"
        ),
        pipeline_version="layout_uid_v1",
    )

    assert payload is None


@pytest.mark.asyncio
async def test_read_payload_from_db_should_persist_repaired_grounding_contract():
    service = LiteratureReaderComposeService()
    original_payload = {
        "paper_id": 85,
        "page": 8,
        "status": "done",
        "engine_version": "reader_compose_v15",
        "pipeline_version": "layout_uid_v1",
        "source_signature": "sig",
        "build_mode": "compose_agent_layout_uid_v1",
        "ui_plan": {
            "plan_id": "plan_cached",
            "components": [
                {
                    "id": "g3",
                    "type": "ListBlock",
                    "props": {"items": ["1. llama.cpp^6 for 4-bit (Q4_K_M)"]},
                    "source_atom_ids": ["layout_list_1"],
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "source_anchor_refs": [
                        {
                            "anchor_id": "layout_uid_v1:layout_list_1",
                            "coord_version": "layout_uid_v1",
                            "source_layout_id": "layout_list_1",
                            "quote_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                            "geometry": {"polygons": [], "page_width": 1232, "page_height": 1843},
                            "bbox_hint": {"x0": 319, "x1": 1232, "top": 386, "bottom": 440, "page_width": 1232, "page_height": 1843},
                        }
                    ],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "page": 8,
            "layout_atoms": [
                {
                    "layout_id": "layout_list_1",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "node_kind": "list",
                    "reading_order": 1,
                    "raw_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "normalized_text": "1. llama.cpp^6 for 4-bit (Q4_K_M)",
                    "canonical_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ],
                    "blocks": [
                        {
                            "block_index": 1,
                            "text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                            "pos": [
                                {"x": 319, "y": 386},
                                {"x": 1232, "y": 386},
                                {"x": 1232, "y": 440},
                                {"x": 319, "y": 440},
                            ],
                        }
                    ],
                }
            ],
            "evidence_map": [
                {
                    "source_layout_id": "layout_list_1",
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ],
                    "block_positions": [[
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ]],
                }
                ],
                "page_image": {
                    "url": "https://example.com/docmind/page_8.png",
                    "path": "",
                    "width": 1360,
                    "height": 1760,
                    "source": "docmind_page_image_remote",
                },
            },
        "docmind_structure": {"layouts": []},
        "page_structure_v3": {"block_groups": []},
    }

    row = SimpleNamespace(payload_json=dict(original_payload), paper_id=85, page=8, source_signature="sig")

    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return row

    class _Db:
        def __init__(self):
            self.commits = 0

        async def execute(self, _stmt):
            return _Result()

        async def commit(self):
            self.commits += 1

    db = _Db()
    payload = await service._read_payload_from_db(  # pylint: disable=protected-access
        db=db,
        paper_id=85,
        page=8,
        source_signature="sig",
    )

    assert db.commits == 1
    assert str((payload.get("ui_plan") or {}).get("components")[0]["source_anchor_refs"][0]["quote_text"]) == "1. llama.cpp^6 for 4-bit (Q4_K_M)"
    repaired_geometry = (payload.get("ui_plan") or {}).get("components")[0]["source_anchor_refs"][0].get("geometry") or {}
    assert int(repaired_geometry.get("page_width") or 0) >= 1360
    assert int(repaired_geometry.get("page_height") or 0) >= 1760
    assert str((row.payload_json.get("ui_plan") or {}).get("components")[0]["source_anchor_refs"][0]["quote_text"]) == "1. llama.cpp^6 for 4-bit (Q4_K_M)"


@pytest.mark.asyncio
async def test_reader_compose_react_stop_by_quality_threshold(monkeypatch):
    service = LiteratureReaderComposeService()

    monkeypatch.setattr(
        service,
        "_build_initial_ui_plan",
        lambda **_: {
            "plan_id": "p1",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )
    monkeypatch.setattr(service, "validate_ui_plan", lambda _plan, page: {"valid": True, "errors": []})
    monkeypatch.setattr(service, "score_ui_plan", lambda **kwargs: _score(0.91, True, kwargs["quality_target"]))
    monkeypatch.setattr(service, "_revise_ui_plan", lambda **kwargs: kwargs["ui_plan"])

    result = await service.run_react_compose_loop(
        paper=SimpleNamespace(id=1, title="Demo", venue="PLOS", year=2023),
        page=1,
        base_payload={"structure_confidence": 0.9, "blocks": [], "assets": []},
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        quality_target=0.86,
        latency_budget_ms=8500,
    )

    assert result["stop_reason"] == "quality_threshold_met"
    assert result["iterations"] == 1
    assert result["quality_report"]["overall"] >= 0.86


@pytest.mark.asyncio
async def test_reader_compose_budget_timeout_returns_best_effort(monkeypatch):
    service = LiteratureReaderComposeService()

    monkeypatch.setattr(
        service,
        "_build_initial_ui_plan",
        lambda **_: {
            "plan_id": "p1",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )
    monkeypatch.setattr(service, "validate_ui_plan", lambda _plan, page: {"valid": True, "errors": []})
    monkeypatch.setattr(service, "score_ui_plan", lambda **kwargs: _score(0.42, False, kwargs["quality_target"]))
    monkeypatch.setattr(service, "_revise_ui_plan", lambda **kwargs: kwargs["ui_plan"])

    result = await service.run_react_compose_loop(
        paper=SimpleNamespace(id=2, title="Demo", venue="PLOS", year=2023),
        page=1,
        base_payload={"structure_confidence": 0.9, "blocks": [], "assets": []},
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        quality_target=0.86,
        latency_budget_ms=0,
    )

    assert result["degraded"] is True
    assert result["stop_reason"] == "latency_budget_exceeded"
    assert result["iterations"] == 1


@pytest.mark.asyncio
async def test_reader_compose_external_image_requires_attribution(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_external_image_enabled", True)

    monkeypatch.setattr(service, "_build_external_image_query", lambda **_: "demo")
    monkeypatch.setattr(
        service,
        "_search_external_images_sync",
        lambda _query, _limit: [
            {
                "image_url": "https://img.example.com/ok.png",
                "source_url": "https://source.example.com/paper",
                "source_domain": "source.example.com",
                "license": "cc-by-4.0",
                "caption": "related figure",
                "why_relevant": "supports understanding",
            },
            {
                "image_url": "https://img.example.com/bad.png",
                "source_url": "",
                "source_domain": "source.example.com",
                "license": "",
                "caption": "invalid",
                "why_relevant": "missing attribution",
            },
        ],
    )

    assets = await service.collect_assets_with_policy(
        paper=SimpleNamespace(id=3, title="Demo", venue="PLOS", year=2023, doi=None),
        page=2,
        base_payload={"assets": [{"kind": "link", "label": "DOI", "source": "metadata", "href": "https://doi.org/x", "meta": {}}]},
        ui_plan={"components": []},
    )

    external = [item for item in assets if str(item.get("kind")) == "external_image"]
    assert len(external) == 1
    assert external[0]["meta"]["source_url"]
    assert external[0]["meta"]["license"]


@pytest.mark.asyncio
async def test_reader_compose_pdf_image_priority_skips_web_search(monkeypatch):
    service = LiteratureReaderComposeService()

    def _raise_if_called(*_args, **_kwargs):
        raise RuntimeError("web search should not be called")

    monkeypatch.setattr(service, "_search_external_images_sync", _raise_if_called)

    assets = await service.collect_assets_with_policy(
        paper=SimpleNamespace(id=4, title="Demo", venue="PLOS", year=2023, doi=None),
        page=1,
        base_payload={
            "assets": [
                {
                    "kind": "image_hint",
                    "label": "Figure 1",
                    "source": "pdf",
                    "href": None,
                    "meta": {"page": 1},
                }
            ]
        },
        ui_plan={"components": []},
    )

    assert len(assets) == 1
    assert assets[0]["kind"] == "image_hint"


def test_reader_compose_prefetch_dedup_and_bounds():
    service = LiteratureReaderComposeService()
    queued, skipped = service.queue_prefetch(pages=[0, 1, 2, 2, 7], max_page=5)

    assert queued == [1, 2]
    assert sorted(skipped) == [0, 2, 7]


def test_reader_compose_validate_rejects_invalid_anchor():
    service = LiteratureReaderComposeService()

    result = service.validate_ui_plan(
        {
            "plan_id": "x",
            "components": [
                {
                    "id": "n1",
                    "type": "SectionHeading",
                    "props": {"text": "Introduction"},
                    "children": [],
                    "source_anchor_refs": [{"page": 1, "start_char": 10, "end_char": 8}],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        page=1,
    )

    assert result["valid"] is False
    assert any("anchor" in err.lower() for err in result["errors"])


def test_reader_compose_sanitize_anchor_should_drop_invalid_range():
    service = LiteratureReaderComposeService()
    raw_plan = {
        "plan_id": "x",
        "components": [
            {
                "id": "n1",
                "type": "SectionHeading",
                "props": {"text": "Introduction"},
                "children": [],
                "source_anchor_refs": [{"page": 0, "start_char": 15, "end_char": 10, "quote_text": "Introduction"}],
            }
        ],
        "layout": {},
        "style_tokens": {},
        "trace_meta": {},
    }

    sanitized = service._sanitize_ui_plan_anchors(raw_plan, page=1)
    result = service.validate_ui_plan(sanitized, page=1)

    assert result["valid"] is True
    assert sanitized["components"][0]["source_anchor_refs"] == []


def test_reader_compose_normalize_blocks_merge_split_heading_lines():
    service = LiteratureReaderComposeService()
    rows = service._normalize_blocks_for_render(
        blocks=[
            {
                "kind": "heading",
                "text": "Performance of ChatGPT on USMLE: Potential",
                "source_anchor": {
                    "page": 1,
                    "start_char": 100,
                    "end_char": 142,
                    "canonical_block_id": "p1_b1",
                    "coord_version": "anchor_v2",
                },
            },
            {
                "kind": "heading",
                "text": "for AI-assisted medical education using large",
                "source_anchor": {
                    "page": 1,
                    "start_char": 143,
                    "end_char": 188,
                    "canonical_block_id": "p1_b1",
                    "coord_version": "anchor_v2",
                },
            },
            {
                "kind": "heading",
                "text": "language models",
                "source_anchor": {
                    "page": 1,
                    "start_char": 189,
                    "end_char": 204,
                    "canonical_block_id": "p1_b1",
                    "coord_version": "anchor_v2",
                },
            },
            {"kind": "heading", "text": "RESEA RCH ARTICLE"},
        ],
        page=1,
    )

    headings = [str(item.get("text") or "") for item in rows if str(item.get("kind") or "") == "heading"]
    assert any("Performance of ChatGPT on USMLE: Potential for AI-assisted medical education using large language models" in item for item in headings)
    assert any("RESEARCH ARTICLE" in item for item in headings)
    merged_heading = next(item for item in rows if "Performance of ChatGPT on USMLE: Potential" in str(item.get("text") or ""))
    anchor = dict(merged_heading.get("source_anchor") or {})
    assert int(anchor.get("start_char") or 0) == 100
    assert int(anchor.get("end_char") or 0) >= 204


def test_build_main_blocks_from_segment_map_should_cover_full_line_span():
    service = LiteratureReaderComposeService()
    blocks = [
        {
            "id": "b1",
            "page": 1,
            "kind": "paragraph",
            "text": "Block text",
            "order": 1,
            "section_title": "Introduction",
            "source_anchor": {"page": 1, "start_char": 0, "end_char": 420},
            "zone_type": "main_body",
            "column_id": "main",
            "heading_prob": 0.0,
            "layout_confidence": 0.9,
        }
    ]
    line_catalog = []
    cursor = 0
    line_ids = []
    for idx in range(8):
        line_id = f"p1_l{idx + 1:03d}_main"
        line_ids.append(line_id)
        line_catalog.append(
            {
                "line_id": line_id,
                "page": 1,
                "order": idx,
                "text": f"Line {idx + 1} content.",
                "start_char": cursor,
                "end_char": cursor + 52,
                "x0": 120,
                "x1": 620,
                "top": 120 + idx * 18,
                "bottom": 136 + idx * 18,
                "page_width": 840,
                "page_height": 1188,
                "column_label": "main",
            }
        )
        cursor += 53

    output = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map={
            "segments": [
                {
                    "segment_id": "seg_long",
                    "kind": "paragraph",
                    "ui_component": "ParagraphProse",
                    "block_ids": [],
                    "line_ids": line_ids,
                    "evidence_line_ids": [],
                    "title": "",
                    "continuation": "none",
                    "reason": "test",
                }
            ]
        },
        base_payload={"line_catalog": line_catalog},
    )

    assert output
    anchor_rows = [dict((row or {}).get("source_anchor") or {}) for row in output]
    max_end = max(int(item.get("end_char") or 0) for item in anchor_rows)
    assert max_end >= int(line_catalog[-1]["end_char"])
    assert all(str(item.get("canonical_block_id") or "").startswith("p1_b1") for item in anchor_rows if item)


def test_reader_compose_build_segment_map_from_parser_advice_should_generate_segments():
    service = LiteratureReaderComposeService()
    segment_map = service._build_segment_map_from_parser_advice(
        page=1,
        parser_advice={
            "heading_groups": [
                {"heading_id": "h_intro", "line_ids": ["p1_l001_main_left"], "title": "Introduction", "level": 1, "confidence": 0.95}
            ],
            "paragraph_groups": [
                {
                    "paragraph_id": "p_intro_1",
                    "line_ids": ["p1_l002_main_left", "p1_l003_main_left"],
                    "heading_id": "h_intro",
                    "zone_type": "main_body",
                    "column_id": "main_left",
                    "confidence": 0.9,
                }
            ],
            "counts": {"heading_count": 1, "paragraph_count": 1, "figure_count": 0},
            "notes": ["parser ok"],
        },
        prompt_payload={
            "line_candidates": [
                {"line_id": "p1_l001_main_left", "order": 0, "text": "Introduction"},
                {"line_id": "p1_l002_main_left", "order": 1, "text": "First sentence."},
                {"line_id": "p1_l003_main_left", "order": 2, "text": "Second sentence."},
            ]
        },
    )

    assert isinstance(segment_map, dict)
    assert str(segment_map.get("source") or "") == "vlflash_page_structure_v2"
    segments = list(segment_map.get("segments") or [])
    assert len(segments) == 2
    assert segments[0].get("kind") == "heading"
    assert segments[1].get("kind") == "paragraph"
    assert segments[1].get("line_ids") == ["p1_l002_main_left", "p1_l003_main_left"]


def test_reader_compose_build_segment_map_from_parser_advice_should_use_block_groups():
    service = LiteratureReaderComposeService()
    segment_map = service._build_segment_map_from_parser_advice(
        page=1,
        parser_advice={
            "line_labels": [],
            "toc_tree": [{"node_id": "h_intro", "type": "heading", "title": "Introduction", "line_ids": ["p1_l001_main_left"], "children": []}],
            "heading_groups": [],
            "paragraph_groups": [],
            "figure_groups": [],
            "block_groups": [
                {
                    "block_id": "blk_h1",
                    "kind": "heading",
                    "title": "Introduction",
                    "line_ids": ["p1_l001_main_left"],
                    "word_ids": ["w000001"],
                    "char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000010"}],
                    "zone_type": "main_body",
                    "column_id": "main_left",
                    "reading_order": 1,
                    "confidence": 0.95,
                },
                {
                    "block_id": "blk_p1",
                    "kind": "paragraph",
                    "parent_node_id": "blk_h1",
                    "line_ids": ["p1_l002_main_left", "p1_l003_main_left"],
                    "word_ids": ["w000002", "w000003"],
                    "char_ranges": [{"start_char_id": "c000011", "end_char_id": "c000060"}],
                    "zone_type": "main_body",
                    "column_id": "main_left",
                    "reading_order": 2,
                    "confidence": 0.9,
                },
            ],
            "counts": {"heading_count": 1, "paragraph_count": 1, "figure_count": 0, "block_count": 2},
            "notes": ["ok"],
        },
        prompt_payload={
            "line_candidates": [
                {"line_id": "p1_l001_main_left", "order": 0, "text": "Introduction"},
                {"line_id": "p1_l002_main_left", "order": 1, "text": "First sentence."},
                {"line_id": "p1_l003_main_left", "order": 2, "text": "Second sentence."},
            ]
        },
    )

    assert isinstance(segment_map, dict)
    segments = list(segment_map.get("segments") or [])
    assert len(segments) == 2
    assert segments[0].get("segment_id") == "blk_h1"
    assert segments[0].get("kind") == "heading"
    assert segments[1].get("segment_id") == "blk_p1"
    assert segments[1].get("kind") == "paragraph"
    assert len(list(segments[1].get("word_ids") or [])) == 2
    assert len(list(segments[1].get("char_ranges") or [])) == 1


def test_build_main_blocks_from_segment_map_should_not_merge_two_block_paragraphs():
    service = LiteratureReaderComposeService()
    blocks = [
        {
            "id": "b1",
            "page": 1,
            "kind": "paragraph",
            "text": "Paragraph A",
            "order": 1,
            "section_title": "Results",
            "source_anchor": {"page": 1, "start_char": 0, "end_char": 120},
            "zone_type": "main_body",
            "column_id": "main",
            "heading_prob": 0.0,
            "layout_confidence": 0.9,
        },
        {
            "id": "b2",
            "page": 1,
            "kind": "paragraph",
            "text": "Paragraph B",
            "order": 2,
            "section_title": "Results",
            "source_anchor": {"page": 1, "start_char": 121, "end_char": 260},
            "zone_type": "main_body",
            "column_id": "main",
            "heading_prob": 0.0,
            "layout_confidence": 0.9,
        },
    ]
    line_catalog = [
        {
            "line_id": "p1_l001_main",
            "page": 1,
            "order": 1,
            "text": "A line 1.",
            "start_char": 0,
            "end_char": 42,
            "x0": 120,
            "x1": 620,
            "top": 100,
            "bottom": 116,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l002_main",
            "page": 1,
            "order": 2,
            "text": "A line 2.",
            "start_char": 43,
            "end_char": 110,
            "x0": 120,
            "x1": 620,
            "top": 118,
            "bottom": 134,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l003_main",
            "page": 1,
            "order": 3,
            "text": "B line 1.",
            "start_char": 130,
            "end_char": 186,
            "x0": 120,
            "x1": 620,
            "top": 170,
            "bottom": 186,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l004_main",
            "page": 1,
            "order": 4,
            "text": "B line 2.",
            "start_char": 187,
            "end_char": 250,
            "x0": 120,
            "x1": 620,
            "top": 188,
            "bottom": 204,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
    ]
    output = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map={
            "segments": [
                {
                    "segment_id": "seg_merge_candidate",
                    "kind": "paragraph",
                    "ui_component": "ParagraphProse",
                    "block_ids": ["p1_b1", "p1_b2"],
                    "line_ids": ["p1_l001_main", "p1_l002_main", "p1_l003_main", "p1_l004_main"],
                    "evidence_line_ids": ["p1_l001_main", "p1_l002_main", "p1_l003_main", "p1_l004_main"],
                    "title": "",
                    "continuation": "none",
                    "reason": "test no merge",
                }
            ]
        },
        base_payload={"line_catalog": line_catalog},
    )

    assert len(output) == 2
    assert output[0].get("source_block_ids") == ["p1_b1"]
    assert output[1].get("source_block_ids") == ["p1_b2"]
    assert "A line 1." in str(output[0].get("text") or "")
    assert "B line 1." in str(output[1].get("text") or "")


def test_build_main_blocks_from_segment_map_should_split_by_large_vertical_gap():
    service = LiteratureReaderComposeService()
    blocks = [
        {
            "id": "b1",
            "page": 1,
            "kind": "paragraph",
            "text": "Merged source block",
            "order": 1,
            "section_title": "Discussion",
            "source_anchor": {"page": 1, "start_char": 0, "end_char": 300},
            "zone_type": "main_body",
            "column_id": "main",
            "heading_prob": 0.0,
            "layout_confidence": 0.9,
        }
    ]
    line_catalog = [
        {
            "line_id": "p1_l001_main",
            "page": 1,
            "order": 1,
            "text": "First paragraph sentence one.",
            "start_char": 0,
            "end_char": 60,
            "x0": 120,
            "x1": 620,
            "top": 100,
            "bottom": 116,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l002_main",
            "page": 1,
            "order": 2,
            "text": "First paragraph sentence two.",
            "start_char": 61,
            "end_char": 120,
            "x0": 120,
            "x1": 620,
            "top": 118,
            "bottom": 134,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l003_main",
            "page": 1,
            "order": 3,
            "text": "Second paragraph starts here.",
            "start_char": 121,
            "end_char": 180,
            "x0": 120,
            "x1": 620,
            "top": 160,
            "bottom": 176,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
        {
            "line_id": "p1_l004_main",
            "page": 1,
            "order": 4,
            "text": "Second paragraph continues.",
            "start_char": 181,
            "end_char": 240,
            "x0": 120,
            "x1": 620,
            "top": 178,
            "bottom": 194,
            "page_width": 840,
            "page_height": 1188,
            "column_label": "main",
        },
    ]
    output = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map={
            "segments": [
                {
                    "segment_id": "seg_gap_candidate",
                    "kind": "paragraph",
                    "ui_component": "ParagraphProse",
                    "block_ids": ["p1_b1"],
                    "line_ids": ["p1_l001_main", "p1_l002_main", "p1_l003_main", "p1_l004_main"],
                    "evidence_line_ids": ["p1_l001_main", "p1_l002_main", "p1_l003_main", "p1_l004_main"],
                    "title": "",
                    "continuation": "none",
                    "reason": "test gap split",
                }
            ]
        },
        base_payload={"line_catalog": line_catalog},
    )

    assert len(output) >= 2
    assert "First paragraph sentence one." in str(output[0].get("text") or "")
    assert any("Second paragraph starts here." in str(item.get("text") or "") for item in output[1:])


def test_mm_layout_plan_validator_should_accept_common_aliases():
    mm = ReaderMultimodalLayoutService()
    payload = {
        "zones": [{"zone_type": "sidebar_left", "block_ids": ["p2_b10"]}],
        "headings": [{"block_id": "p2_b10", "level": 1, "confidence": 0.92, "text": "Introduction"}],
        "continuation": {"from_prev": [], "to_next": [], "confidence": 0.3},
        "segments": [
            {
                "segment_id": "seg_1",
                "kind": "text",
                "ui_component": "paragraph",
                "line_ids": ["1", "2"],
                "evidence_line_ids": [],
                "block_ids": ["p2_b10"],
                "title": "",
            }
        ],
    }
    validated = mm.validate_layout_plan_v2_json(
        payload=payload,
        valid_block_ids={"p2_b10"},
        valid_line_ids={"p2_l001_main", "p2_l002_main"},
        component_whitelist={"SectionHeading", "ParagraphProse", "ListBlock", "ContextRail", "FigurePanel", "TablePanel"},
    )
    assert isinstance(validated, dict)
    assert (validated.get("zones") or [])[0]["zone_type"] == "side_context"
    assert (validated.get("segments") or [])[0]["ui_component"] == "ParagraphProse"


def test_inline_query_slot_should_resolve_target_node_text():
    service = LiteratureReaderComposeService()
    components = [
        {
            "id": "paragraph_1",
            "type": "ParagraphProse",
            "props": {"text": "Step 1, Step 2CK, and Step 3 are evaluated in this section."},
            "children": [],
            "source_anchor_refs": [{"page": 1, "start_char": 20, "end_char": 88, "quote_text": "Step 1, Step 2CK, and Step 3"}],
        },
        {
            "id": "slot_1",
            "type": "InlineQuerySlot",
            "props": {"target_node_ref": "paragraph_1"},
            "children": [],
            "source_anchor_refs": [],
        },
    ]
    query_node = components[1]
    target = service._resolve_inline_query_target_node(query_node=query_node, components=components)
    context = service._build_inline_query_context(
        query_node=query_node,
        target_node=target,
        components=components,
        page=1,
    )

    assert target["id"] == "paragraph_1"
    assert "Step 1" in context


def test_revise_ui_plan_should_recover_heading_when_missing():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "plan_id": "demo",
        "components": [
            {
                "id": "paragraph_1",
                "type": "ParagraphProse",
                "props": {"text": "This is paragraph text."},
                "children": [],
                "source_anchor_refs": [],
            }
        ],
        "layout": {},
        "style_tokens": {},
        "trace_meta": {},
    }
    base_payload = {
        "sections": [
            {
                "title": "Introduction",
                "level": 1,
                "page": 1,
                "source_anchor": {
                    "page": 1,
                    "start_char": 0,
                    "end_char": 12,
                    "quote_text": "Introduction",
                },
            }
        ]
    }

    revised = service._revise_ui_plan(
        ui_plan=ui_plan,
        base_payload=base_payload,
        quality_report={"stop_reason": "max_iterations_reached"},
    )
    heading_nodes = [item for item in revised.get("components") or [] if item.get("type") == "SectionHeading"]

    assert heading_nodes
    assert str(heading_nodes[0].get("props", {}).get("text") or "") == "Introduction"


def test_inline_query_anchor_fallback_should_use_neighbor_anchors():
    service = LiteratureReaderComposeService()
    components = [
        {
            "id": "p1",
            "type": "ParagraphProse",
            "props": {"text": "Neighbor paragraph"},
            "children": [],
            "source_anchor_refs": [{"page": 1, "start_char": 10, "end_char": 40, "quote_text": "Neighbor paragraph"}],
        },
        {
            "id": "slot_1",
            "type": "InlineQuerySlot",
            "props": {"target_node_ref": "target_1"},
            "children": [],
            "source_anchor_refs": [],
        },
        {
            "id": "target_1",
            "type": "ParagraphProse",
            "props": {"text": "Target paragraph"},
            "children": [],
            "source_anchor_refs": [],
        },
    ]

    anchors = service._resolve_inline_query_anchors(
        query_node=components[1],
        target_node=components[2],
        components=components,
        page=1,
    )

    assert anchors
    assert int(anchors[0].get("page") or 0) == 1
    assert int(anchors[0].get("end_char") or 0) > int(anchors[0].get("start_char") or 0)


def test_build_link_tldr_empty_doi_should_not_match_all_links():
    tldr = LiteratureReaderComposeService._build_link_tldr(
        href="https://example.com/resource",
        label="Example Resource",
        paper=SimpleNamespace(doi=None),
    )

    assert "DOI link" not in tldr

def test_extract_query_terms_should_expand_limitations_to_valid_chinese_term():
    terms = literature_api._extract_query_terms("limitations")
    assert "limitations" in terms

class _FakeDB:
    async def execute(self, _stmt):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_reader_compose_sse_event_order(monkeypatch):
    fake_payload = {
        "paper_id": 9,
        "page": 2,
        "engine_version": "reader_compose_v1",
        "source_signature": "sig-x",
        "build_mode": "compose_agent",
        "ui_plan": {
            "plan_id": "p-x",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [
            {"kind": "link", "label": "DOI", "source": "metadata", "href": "https://doi.org/x", "meta": {}}
        ],
        "quality_report": _score(0.87, True, 0.86),
        "iteration_trace": [
            {
                "iteration": 1,
                "ui_plan": {
                    "plan_id": "p-1",
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": _score(0.61, False, 0.86),
            },
            {
                "iteration": 2,
                "ui_plan": {
                    "plan_id": "p-2",
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": _score(0.87, True, 0.86),
            },
        ],
        "asset_policy": {"pdf_first": True},
        "generated_at": "2026-02-25T00:00:00Z",
    }

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **_kwargs):
            return fake_payload, ReaderComposeBuildMeta(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent",
                source_signature="sig-x",
                source_sig_hash="hash-x",
                iterations=2,
                degraded=False,
                stop_reason="quality_threshold_met",
            )

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=9, user_id=7, title="Demo")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(
        literature_api,
        "get_literature_reader_compose_service",
        lambda: _FakeComposeService(),
    )

    class _FakeSessionFactory:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(literature_api, "async_session_factory", lambda: _FakeSessionFactory())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    response = await literature_api.stream_reader_composed_page(
        paper_id=9,
        payload=SimpleNamespace(
            page=2,
            selected_kb_id=None,
            force_refresh=False,
            regenerate=False,
            latency_budget_ms=None,
            quality_target=None,
            style_intent=None,
            theme_mode="light",
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
            ),
        request=_FakeRequest(),
        current_user=SimpleNamespace(id=7),
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode("utf-8") if isinstance(item, bytes) else str(item))

    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            parsed = json.loads(payload)
            event_name = str(parsed.get("event") or "")
            if event_name:
                events.append(event_name)

    assert events[0] == "start"
    assert events[1] == "plan_draft"
    assert "plan_patch" in events
    assert "assets" in events
    assert "quality" in events
    assert events[-1] == "done"


@pytest.mark.asyncio
async def test_reader_composed_stream_plan_draft_should_sanitize_null_figure_image_url(monkeypatch):
    class _FakeDB:
        pass

    class _FakeSessionFactory:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeComposeService:
        def __init__(self) -> None:
            self._real = LiteratureReaderComposeService()

        def _sanitize_ui_plan_for_runtime(self, *, page, payload, ui_plan):
            return self._real._sanitize_ui_plan_for_runtime(page=page, payload=payload, ui_plan=ui_plan)  # pylint: disable=protected-access

        async def build_or_get_composed_payload(self, **_kwargs):
            return (
                {
                    "paper_id": 9,
                    "page": 2,
                    "status": "done",
                    "build_mode": "compose_agent_layout_uid_v1",
                    "degraded_reason": "",
                    "source_signature": "sig-demo",
                    "engine_version": "reader_compose_v15",
                    "pipeline_version": "layout_uid_v1",
                    "ui_plan": {
                        "plan_id": "demo",
                        "components": [
                            {
                                "id": "figure_1",
                                "type": "FigurePanel",
                                "props": {
                                    "caption": "Figure 1. Demo",
                                    "image_url": None,
                                },
                                "children": [],
                                "source_anchor_refs": [],
                                "source_block_ids": [],
                            }
                        ],
                        "layout": {},
                        "style_tokens": {},
                        "trace_meta": {},
                    },
                    "assets": [],
                    "quality_report": {"overall": 0.9, "validation_errors": []},
                    "iteration_trace": [],
                    "main_block_ids": [],
                    "aux_block_ids": [],
                    "validation_report": _validation_report_stub(True),
                    "repair_report": {},
                    "generated_at": "2026-03-31T00:00:00Z",
                },
                ReaderComposeBuildMeta(
                    cache_hit=False,
                    cache_layer="none",
                    build_mode="compose_agent_layout_uid_v1",
                    source_signature="sig-demo",
                    source_sig_hash="sig-hash",
                    engine_version="reader_compose_v15",
                    iterations=1,
                    degraded=False,
                    stop_reason="",
                ),
            )

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=9, user_id=7, title="Demo")

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(literature_api, "async_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    response = await literature_api.stream_reader_composed_page(
        paper_id=9,
        payload=SimpleNamespace(
            page=2,
            selected_kb_id=None,
            force_refresh=False,
            regenerate=False,
            latency_budget_ms=None,
            quality_target=None,
            style_intent=None,
            theme_mode="light",
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
        ),
        request=_FakeRequest(),
        current_user=SimpleNamespace(id=7),
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode("utf-8") if isinstance(item, bytes) else str(item))

    payloads = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[len("data: "):].strip()))

    plan_draft = next(item for item in payloads if str(item.get("event") or "") == "plan_draft")
    ui_plan = dict((plan_draft.get("data") or {}).get("ui_plan") or {})
    figure_node = next(
        node for node in list(ui_plan.get("components") or [])
        if str((node or {}).get("type") or "") == "FigurePanel"
    )
    assert ((figure_node.get("props") or {}).get("image_url")) == ""


def test_reader_composed_stream_disconnect_should_continue_background_build_and_stop_sse(monkeypatch):
    async def _run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        disconnect_ready = asyncio.Event()
        completed = asyncio.Event()
        cancelled = asyncio.Event()

        class _FakeDB:
            pass

        class _FakeSessionFactory:
            async def __aenter__(self):
                return _FakeDB()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _FakeComposeService:
            async def build_or_get_composed_payload(self, **kwargs):
                progress_callback = kwargs["progress_callback"]
                try:
                    await progress_callback(
                        "stage",
                        {
                            "stage": "compose_running",
                            "status": "started",
                            "message": "working",
                        },
                    )
                    started.set()
                    await release.wait()
                    await progress_callback(
                        "stage",
                        {
                            "stage": "compose_late",
                            "status": "started",
                            "message": "late progress should stay hidden",
                        },
                    )
                    return (
                        {
                            "status": "done",
                            "ui_plan": {
                                "plan_id": "demo",
                                "components": [],
                                "layout": {},
                                "style_tokens": {},
                                "trace_meta": {},
                            },
                            "assets": [],
                            "quality_report": {},
                            "iteration_trace": [],
                        },
                        ReaderComposeBuildMeta(
                            cache_hit=False,
                            cache_layer="none",
                            build_mode="compose_ai_reconstructed",
                            source_signature="sig-x",
                            source_sig_hash="hash-x",
                            iterations=1,
                            degraded=False,
                            stop_reason="done",
                        ),
                    )
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                finally:
                    completed.set()

        async def _fake_get_owned(_db, _user, _paper_id):
            return SimpleNamespace(id=9, user_id=7, title="Demo")

        class _FakeRequest:
            async def is_disconnected(self):
                return disconnect_ready.is_set()

        monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
        monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
        monkeypatch.setattr(literature_api, "async_session_factory", lambda: _FakeSessionFactory())

        response = await literature_api.stream_reader_composed_page(
            paper_id=9,
            payload=SimpleNamespace(
                page=2,
                selected_kb_id=None,
                force_refresh=False,
                regenerate=False,
                latency_budget_ms=None,
                quality_target=None,
                style_intent=None,
                theme_mode="light",
                detail_level="standard",
                compare_mode=False,
                citation_tldr=False,
            ),
            request=_FakeRequest(),
            current_user=SimpleNamespace(id=7),
        )

        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                parsed = json.loads(line[len("data: "):])
                event_name = str(parsed.get("event") or "")
                if not event_name:
                    continue
                events.append(event_name)
                if event_name == "stage":
                    disconnect_ready.set()

        assert events == ["start", "stage"]
        assert started.is_set() is True

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        assert cancelled.is_set() is False

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_reader_compose_signature_should_change_with_mode_even_with_long_pdf_path(monkeypatch):
    service = LiteratureReaderComposeService()
    long_path = f"/tmp/{'very_long_pdf_name_' * 12}.pdf"

    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_: long_path,
    )
    monkeypatch.setattr(os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        os,
        "stat",
        lambda _path: SimpleNamespace(st_mtime=1739000000, st_size=12345678),
    )

    class _NoopDb:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("selected_kb_id=None should not trigger DB query")


    paper = SimpleNamespace(id=11, user_id=1, pdf_path=long_path, title="Demo")
    sig_a = await service._build_source_signature(
        db=_NoopDb(),
        user_id=1,
        paper=paper,
        selected_kb_id=None,
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        citation_tldr=False,
        max_iterations=8,
    )
    sig_b = await service._build_source_signature(
        db=_NoopDb(),
        user_id=1,
        paper=paper,
        selected_kb_id=None,
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=True,
        citation_tldr=False,
        max_iterations=8,
    )

    assert len(sig_a) <= 255
    assert len(sig_b) <= 255
    assert sig_a != sig_b


def test_reader_compose_style_tokens_should_accept_frontend_style_keys():
    service = LiteratureReaderComposeService()
    journal = service._build_style_tokens(
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
    )
    clinical = service._build_style_tokens(
        style_intent="clinical_brief",
        theme_mode="light",
        detail_level="standard",
    )
    preprint = service._build_style_tokens(
        style_intent="preprint_modern",
        theme_mode="light",
        detail_level="standard",
    )

    assert journal["style_intent"] == "journal"
    assert clinical["style_intent"] == "clinical"
    assert preprint["style_intent"] == "preprint"


@pytest.mark.asyncio
async def test_reader_inline_query_should_forward_theme_and_citation_flags(monkeypatch):
    service = LiteratureReaderComposeService()
    captured: dict = {}

    async def _fake_get_latest_cached_payload_only(**kwargs):
        captured["theme_mode"] = kwargs.get("theme_mode")
        captured["citation_tldr"] = kwargs.get("citation_tldr")
        return None

    async def _fake_build_or_get(**kwargs):
        captured["theme_mode"] = kwargs.get("theme_mode")
        captured["citation_tldr"] = kwargs.get("citation_tldr")
        return (
            {
                "ui_plan": {
                    "components": [
                        {
                            "id": "n1",
                            "type": "ParagraphProse",
                            "props": {"text": "Demo paragraph for inline query."},
                            "children": [],
                            "source_block_ids": ["p1_b1"],
                            "source_anchor_refs": [
                                {"page": 1, "start_char": 0, "end_char": 20, "quote_text": "Demo paragraph"}
                            ],
                        }
                    ]
                }
            },
            ReaderComposeBuildMeta(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_cache",
                source_signature="sig",
                source_sig_hash="hash",
            ),
        )

    async def _fake_answer(**_kwargs):
        return "Conclusion: answerable with current paragraph evidence."
    monkeypatch.setattr(service, "get_latest_cached_payload_only", _fake_get_latest_cached_payload_only)
    monkeypatch.setattr(service, "build_or_get_composed_payload", _fake_build_or_get)
    monkeypatch.setattr(service, "_generate_inline_answer", _fake_answer)

    result = await service.build_inline_answer_card(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=1),
        page=1,
        node_id="n1",
        question="test",
        scope="section",
        theme_mode="dark",
        citation_tldr=True,
    )

    assert captured["theme_mode"] == "dark"
    assert captured["citation_tldr"] is True
    assert isinstance(result.get("node"), dict)


@pytest.mark.asyncio
async def test_prepare_inline_query_answer_should_prefer_latest_cached_payload(monkeypatch):
    service = LiteratureReaderComposeService()
    captured: dict = {"cached_called": 0, "build_called": 0}

    async def _fake_cached(**_kwargs):
        captured["cached_called"] += 1
        return {
            "ui_plan": {
                "components": [
                    {
                        "id": "n1",
                        "type": "ParagraphProse",
                        "props": {"text": "Cached paragraph for inline query."},
                        "children": [],
                        "source_block_ids": ["p1_b1"],
                        "source_anchor_refs": [
                            {"page": 1, "start_char": 0, "end_char": 24, "quote_text": "Cached paragraph"}
                        ],
                    }
                ]
            }
        }

    async def _fake_build(**_kwargs):
        captured["build_called"] += 1
        raise AssertionError("should not rebuild when latest cached payload exists")

    monkeypatch.setattr(service, "get_latest_cached_payload_only", _fake_cached)
    monkeypatch.setattr(service, "build_or_get_composed_payload", _fake_build)

    result = await service.prepare_inline_query_answer(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=1),
        page=1,
        node_id="n1",
        question="what is this paragraph about?",
        scope="section",
    )

    assert captured["cached_called"] == 1
    assert captured["build_called"] == 0
    assert bool(result.get("disabled")) is False
    assert "Cached paragraph" in str(result.get("context_text") or "")


def test_build_inline_answer_prompt_should_prefer_local_answer_over_hard_refusal():
    service = LiteratureReaderComposeService()
    prompt = service._build_inline_answer_prompt(
        question="这里为什么这样做？",
        context_text="当前节点：The model uses local calibration. 证据片段：local calibration reduces error.",
        scope="section",
    )

    assert "Prefer a useful local answer" in prompt
    assert "Only say 当前段落证据不足" in prompt
    assert "请使用右侧“询问”进行全文问答" not in prompt


@pytest.mark.asyncio
async def test_reader_compose_mm_gate_not_hit_should_fail_loud_without_docmind_source(monkeypatch):
    service = LiteratureReaderComposeService()

    class _StubMMService:
        def should_trigger_mm(self, **_kwargs):
            return False, {
                "reason": "quality_gate_not_hit",
                "cross_column_merge_ratio": 0.02,
            }

    service._mm_layout_service = _StubMMService()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_kwargs: "demo.pdf",
    )
    monkeypatch.setattr(os.path, "exists", lambda _path: True)

    with pytest.raises(RenderPipelineContractError) as exc_info:
        await service._apply_multimodal_layout_assist(
            paper=SimpleNamespace(id=19, user_id=1, title="Demo", pdf_path="demo.pdf"),
            page=1,
            base_payload={
                "page": 1,
                "structure_confidence": 0.9,
                "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph"}],
                "sections": [],
            },
        )
    assert exc_info.value.code == "DOCMIND_LAYOUT_DIGEST_EMPTY"


@pytest.mark.asyncio
async def test_reader_compose_mm_should_fail_loud_when_stage_contract_prereq_missing(monkeypatch):
    service = LiteratureReaderComposeService()

    class _StubMMService:
        def should_trigger_mm(self, **_kwargs):
            return True, {"trigger_reasons": ["low_structure_confidence"]}

        async def build_mm_prompt_payload(self, **_kwargs):
            return {"image_data_url": "data:image/jpeg;base64,AA==", "line_candidates": []}

        async def call_primary_then_fallback(self, **_kwargs):
            return (
                {"headings": [], "zones": [], "toc_candidates": [], "notes": []},
                {"used": True, "model": "qwen3-vl-flash", "fallback_used": True, "error": None},
            )

        def merge_mm_decision_into_blocks(self, *, base_payload, mm_decision):
            _ = mm_decision
            merged = dict(base_payload)
            merged["toc_quality"] = 0.7
            merged["toc_hidden"] = False
            merged["side_context_blocks"] = [
                {
                    "id": "sb1",
                    "kind": "paragraph",
                    "text": "OPEN ACCESS",
                    "source_anchor": {"page": 1, "start_char": 2, "end_char": 20},
                    "zone_type": "side_context",
                    "column_id": "sidebar_left",
                }
            ]
            merged["figure_meta_blocks"] = []
            merged["layout_channels"] = {
                "main_body": ["b1"],
                "side_context": ["sb1"],
                "figure_meta": [],
            }
            return merged

        def mark_mm_triggered(self, **_kwargs):
            return None

    service._mm_layout_service = _StubMMService()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_kwargs: "demo.pdf",
    )
    monkeypatch.setattr(os.path, "exists", lambda _path: True)

    with pytest.raises(RenderPipelineContractError) as exc_info:
        await service._apply_multimodal_layout_assist(
            paper=SimpleNamespace(id=20, user_id=1, title="Demo", pdf_path="demo.pdf"),
            page=1,
            base_payload={
                "page": 1,
                "structure_confidence": 0.5,
                "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph", "zone_type": "main_body"}],
                "sections": [],
            },
        )
    assert exc_info.value.code == "DOCMIND_LAYOUT_DIGEST_EMPTY"


def test_mm_should_trigger_per_build_when_budget_allows(monkeypatch):
    service = ReaderMultimodalLayoutService()
    monkeypatch.setattr(settings, "reader_mm_assist_enabled", True)
    monkeypatch.setattr(settings, "reader_mm_max_calls_per_page", 1)

    base_payload = {
        "structure_confidence": 0.92,
        "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph"}],
        "style_cues": {"layout_mode": "single_column"},
    }
    should_trigger, trigger_meta = service.should_trigger_mm(
        paper_id=88,
        page=3,
        base_payload=base_payload,
        call_count=0,
    )
    assert should_trigger is True
    assert str(trigger_meta.get("reason") or "") == "triggered_per_page_default"

    should_trigger_again, trigger_meta_again = service.should_trigger_mm(
        paper_id=88,
        page=3,
        base_payload=base_payload,
        call_count=0,
    )
    assert should_trigger_again is True
    assert str(trigger_meta_again.get("reason") or "") == "triggered_per_page_default"

    over_budget, over_budget_meta = service.should_trigger_mm(
        paper_id=88,
        page=3,
        base_payload=base_payload,
        call_count=1,
    )
    assert over_budget is False
    assert str(over_budget_meta.get("reason") or "") == "page_call_budget_exceeded"


@pytest.mark.asyncio
async def test_mm_prompt_payload_should_include_current_image_only(monkeypatch):
    service = ReaderMultimodalLayoutService()

    async def _fake_render(*, pdf_path: str, page: int):
        _ = pdf_path
        return f"data:image/jpeg;base64,page-{page}"

    monkeypatch.setattr(service, "_render_page_image_data_url", _fake_render)
    payload = await service.build_mm_prompt_payload(
        pdf_path="demo.pdf",
        page=2,
        base_payload={
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph", "source_anchor": {"page": 2, "start_char": 0, "end_char": 10}}],
            "style_cues": {
                "line_layout": [
                    {"line_id": 1, "text": "Demo paragraph", "x0": 80, "x1": 600, "top": 120, "bottom": 138},
                ]
            },
        },
    )

    assert isinstance(payload, dict)
    images = list(payload.get("images") or [])
    scopes = [str(item.get("scope") or "") for item in images]
    assert scopes == ["current"]
    assert str(payload.get("image_data_url") or "").startswith("data:image/jpeg;base64,page-2")
    assert isinstance(payload.get("native_page_extract"), dict)
    assert isinstance(payload.get("valid_word_ids"), list)
    assert isinstance(payload.get("valid_char_ids"), list)


def test_mm_validate_json_should_keep_continuation_and_ui_suggestions():
    service = ReaderMultimodalLayoutService()
    parsed = service.validate_mm_layout_json(
        {
            "headings": [{"line_id": 1, "heading_prob": 0.88, "level": 1}],
            "zones": [{"line_id": 1, "zone_type": "main_body", "column_id": "left"}],
            "toc_candidates": [1],
            "notes": ["ok"],
            "page_continuation": {"from_prev": True, "to_next": False, "continuation_confidence": 0.81, "notes": ["tail"]},
            "ui_suggestions": [
                {"kind": "continue_from_prev", "target_block_ids": ["b1"], "reason": "tail from previous page"},
                {"kind": "unknown_kind", "target_block_ids": ["b2"], "reason": "ignored"},
            ],
        }
    )

    assert isinstance(parsed, dict)
    continuation = parsed.get("page_continuation") or {}
    assert continuation.get("from_prev") is True
    assert continuation.get("to_next") is False
    suggestions = list(parsed.get("ui_suggestions") or [])
    assert len(suggestions) == 1
    assert suggestions[0].get("kind") == "continue_from_prev"


def test_mm_validate_line_parse_advice_should_keep_groups_counts_and_aliases():
    service = ReaderMultimodalLayoutService()
    parsed = service.validate_line_parse_advice_json(
        payload={
            "line_labels": [
                {"line_id": 1, "zone_type": "main_body", "column_id": "main_left", "paragraph_break_after": False, "heading_prob": 0.95},
                {"line_id": 2, "zone_type": "main_body", "column_id": "main_left", "paragraph_break_after": False, "heading_prob": 0.05},
                {"line_id": 3, "zone_type": "main_body", "column_id": "main_left", "paragraph_break_after": True, "heading_prob": 0.05},
            ],
            "toc_tree": [
                {"node_id": "h_intro", "type": "heading", "title": "Introduction", "line_ids": [1], "level": 1, "children": []}
            ],
            "heading_groups": [
                {"heading_id": "h_intro", "line_ids": [1], "title": "Introduction", "level": 1, "confidence": 0.92}
            ],
            "paragraph_groups": [
                {"paragraph_id": "p1", "line_ids": [2, 3], "heading_id": "h_intro", "zone_type": "main_body", "column_id": "main_left", "confidence": 0.88}
            ],
            "figure_groups": [
                {"figure_id": "f1", "line_ids": [], "caption_line_ids": [3], "related_heading_id": "h_intro", "confidence": 0.8}
            ],
            "block_groups": [
                {
                    "block_id": "blk_001",
                    "kind": "paragraph",
                    "parent_node_id": "h_intro",
                    "line_ids": [2, 3],
                    "word_ids": ["w000001", "w000002"],
                    "char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000012"}],
                    "zone_type": "main_body",
                    "column_id": "main_left",
                    "reading_order": 1,
                    "confidence": 0.9,
                }
            ],
            "counts": {"heading_count": 1, "paragraph_count": 1, "figure_count": 1, "block_count": 1},
            "notes": ["ok"],
        },
        valid_line_ids={"p2_l001_main_left", "p2_l002_main_left", "p2_l003_main_left"},
        valid_word_ids={"w000001", "w000002"},
        valid_char_ids=["c000001", "c000002", "c000003", "c000004", "c000005", "c000006", "c000007", "c000008", "c000009", "c000010", "c000011", "c000012"],
    )

    assert isinstance(parsed, dict)
    assert len(list(parsed.get("line_labels") or [])) == 3
    assert len(list(parsed.get("heading_groups") or [])) == 1
    assert len(list(parsed.get("paragraph_groups") or [])) == 1
    assert len(list(parsed.get("figure_groups") or [])) == 1
    assert len(list(parsed.get("block_groups") or [])) == 1
    counts = dict(parsed.get("counts") or {})
    assert counts.get("heading_count") == 1
    assert counts.get("paragraph_count") == 1
    assert counts.get("figure_count") == 1
    assert counts.get("block_count") == 1


@pytest.mark.asyncio
async def test_mm_build_line_parse_advice_should_retry_on_quality_gate(monkeypatch):
    service = ReaderMultimodalLayoutService()
    valid_line_ids = [f"p1_l{idx:03d}_main_left" for idx in range(1, 21)]
    valid_word_ids = [f"w{idx:06d}" for idx in range(1, 81)]
    valid_char_ids = [f"c{idx:06d}" for idx in range(1, 501)]
    calls = []

    first_bad = {
        "line_labels": [
            {
                "line_id": line_id,
                "zone_type": "main_body",
                "column_id": "main_left",
                "paragraph_break_after": False,
                "heading_prob": 0.0,
            }
            for line_id in valid_line_ids
        ],
        "toc_tree": [],
        "heading_groups": [],
        "paragraph_groups": [
            {
                "paragraph_id": "p1",
                "line_ids": list(valid_line_ids),
                "heading_id": "",
                "zone_type": "main_body",
                "column_id": "main_left",
                "confidence": 0.88,
            }
        ],
        "figure_groups": [],
        "block_groups": [],
        "counts": {"heading_count": 0, "paragraph_count": 1, "figure_count": 0},
        "notes": [],
    }
    second_good = {
        "line_labels": [
            {
                "line_id": line_id,
                "zone_type": "main_body",
                "column_id": "main_left",
                "paragraph_break_after": line_id in {valid_line_ids[9], valid_line_ids[-1]},
                "heading_prob": 0.0,
            }
            for line_id in valid_line_ids
        ],
        "toc_tree": [],
        "heading_groups": [],
        "paragraph_groups": [
            {
                "paragraph_id": "p1",
                "line_ids": list(valid_line_ids[:10]),
                "heading_id": "",
                "zone_type": "main_body",
                "column_id": "main_left",
                "confidence": 0.9,
            },
            {
                "paragraph_id": "p2",
                "line_ids": list(valid_line_ids[10:]),
                "heading_id": "",
                "zone_type": "main_body",
                "column_id": "main_left",
                "confidence": 0.9,
            },
        ],
        "figure_groups": [],
        "block_groups": [
            {
                "block_id": "blk_001",
                "kind": "paragraph",
                "line_ids": list(valid_line_ids[:10]),
                "word_ids": list(valid_word_ids[:40]),
                "char_ranges": [{"start_char_id": valid_char_ids[0], "end_char_id": valid_char_ids[249]}],
                "zone_type": "main_body",
                "column_id": "main_left",
                "reading_order": 1,
                "confidence": 0.9,
            },
            {
                "block_id": "blk_002",
                "kind": "paragraph",
                "line_ids": list(valid_line_ids[10:]),
                "word_ids": list(valid_word_ids[40:80]),
                "char_ranges": [{"start_char_id": valid_char_ids[250], "end_char_id": valid_char_ids[-1]}],
                "zone_type": "main_body",
                "column_id": "main_left",
                "reading_order": 2,
                "confidence": 0.9,
            },
        ],
        "counts": {"heading_count": 0, "paragraph_count": 2, "figure_count": 0, "block_count": 2},
        "notes": ["retry_fixed"],
    }

    async def _fake_call_mm_model(*, model: str, prompt_payload: dict, timeout_ms: int, prompt_kind: str):
        _ = (model, timeout_ms, prompt_kind)
        calls.append(dict(prompt_payload))
        return first_bad if len(calls) == 1 else second_good

    monkeypatch.setattr(service, "_call_mm_model", _fake_call_mm_model)
    monkeypatch.setattr(settings, "reader_mm_parser_model", "qwen3-vl-flash")
    monkeypatch.setattr(settings, "reader_mm_fallback_model", "qwen3-vl-flash")

    parsed, meta = await service.build_line_parse_advice(
        prompt_payload={"line_candidates": [], "valid_word_ids": valid_word_ids, "valid_char_ids": valid_char_ids},
        valid_line_ids=valid_line_ids,
    )

    assert isinstance(parsed, dict)
    assert int((meta or {}).get("retry_count") or 0) >= 1
    assert bool((meta or {}).get("retry_used")) is True
    assert len(calls) == 2
    assert not str(calls[0].get("retry_hint") or "")
    assert "validation_errors=" in str(calls[1].get("retry_hint") or "")


def test_mm_merge_decision_should_accept_string_line_ids():
    service = ReaderMultimodalLayoutService()
    payload = service.merge_mm_decision_into_blocks(
        base_payload={
            "page": 1,
            "raw_text": "Introduction Body text line.",
            "style_cues": {
                "line_layout": [
                    {"line_id": "p1_l001_main_left", "text": "Introduction", "column_label": "main_left"},
                    {"line_id": "p1_l002_main_left", "text": "Body text line.", "column_label": "main_left"},
                ]
            },
            "blocks": [
                {
                    "id": "p1_b1",
                    "kind": "heading",
                    "text": "Introduction",
                    "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
                },
                {
                    "id": "p1_b2",
                    "kind": "paragraph",
                    "text": "Body text line.",
                    "source_anchor": {"page": 1, "start_char": 13, "end_char": 28},
                },
            ],
        },
        mm_decision={
            "headings": [{"line_id": "p1_l001_main_left", "heading_prob": 0.93, "level": 1}],
            "zones": [
                {"line_id": "p1_l001_main_left", "zone_type": "main_body", "column_id": "main_left"},
                {"line_id": "p1_l002_main_left", "zone_type": "main_body", "column_id": "main_left"},
            ],
            "toc_candidates": ["p1_l001_main_left"],
            "notes": [],
        },
    )

    merged_blocks = list(payload.get("blocks") or [])
    assert len(merged_blocks) == 2
    heading_block = next(item for item in merged_blocks if str(item.get("kind") or "") == "heading")
    assert heading_block.get("toc_candidate") is True
    assert str(heading_block.get("column_id") or "") == "main_left"


@pytest.mark.asyncio
async def test_generate_takeaways_should_use_neighbor_context(monkeypatch):
    service = LiteratureReaderComposeService()

    class _StubLLM:
        async def chat(self, **_kwargs):
            return {
                "content": json.dumps(
                    {
                        "items": [
                            {
                                "text": "Current-page core finding with transferable conclusion.",
                                "evidence_block_ids": ["p2_b1"],
                            },
                            {
                                "text": "Previous-page context supports condition interpretation.",
                                "evidence_block_ids": ["p1_b1"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            }

    async def _fake_get_llm_service():
        return _StubLLM()

    monkeypatch.setattr(
        "app.services.literature_reader_compose_service.get_llm_service",
        _fake_get_llm_service,
    )

    rows = await service._generate_takeaways_from_context_rows(
        context_rows=[
            (
                1,
                {
                    "blocks": [
                        {
                            "id": "b1",
                            "kind": "paragraph",
                            "text": "Page one context paragraph.",
                            "source_anchor": {"page": 1, "start_char": 0, "end_char": 24},
                        }
                    ]
                },
            ),
            (
                2,
                {
                    "blocks": [
                        {
                            "id": "b1",
                            "kind": "paragraph",
                            "text": "Current page core finding paragraph.",
                            "source_anchor": {"page": 2, "start_char": 10, "end_char": 48},
                        }
                    ]
                },
            ),
            (
                3,
                {
                    "blocks": [
                        {
                            "id": "b1",
                            "kind": "paragraph",
                            "text": "Next page supporting statement.",
                            "source_anchor": {"page": 3, "start_char": 5, "end_char": 36},
                        }
                    ]
                },
            ),
        ],
        current_page=2,
        detail_level="standard",
    )

    assert len(rows) == 2
    assert "core finding" in str(rows[0].get("text") or "").lower()
    assert len(rows[0].get("evidence_anchors") or []) > 0
    assert int((rows[0].get("evidence_anchors") or [])[0].get("page") or 0) == 2
    assert len(rows[1].get("evidence_anchors") or []) == 0


def test_build_initial_ui_plan_should_prefer_takeaways_from_payload():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=31, title="Demo", venue="PLOS", year=2024, authors=[], doi=None, pdf_url=None, url=None)
    ui_plan = service._build_initial_ui_plan(
        paper=paper,
        page=1,
        base_payload={
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph", "source_anchor": {"page": 1, "start_char": 0, "end_char": 14}}],
            "assets": [],
            "summary": "This summary should not be used...",
            "style_cues": {},
            "toc_quality": 0.8,
            "toc_hidden": False,
            "takeaways": [
                {
                    "text": "This page key takeaway is summarized by AI.",
                    "evidence_anchors": [{"page": 1, "start_char": 0, "end_char": 14}],
                }
            ],
        },
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    takeaway_nodes = [node for node in ui_plan.get("components") or [] if node.get("type") == "KeyTakeaways"]
    assert takeaway_nodes
    assert len(takeaway_nodes[0].get("source_anchor_refs") or []) == 0
    items = (takeaway_nodes[0].get("props") or {}).get("items") or []
    assert items
    text = str(items[0].get("text") or "")
    assert "AI" in text
    assert len(text) >= 8


def test_build_initial_ui_plan_should_normalize_figure_panel_image_url_to_string():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=33, title="Demo", venue="PLOS", year=2024, authors=[], doi=None, pdf_url=None, url=None)
    ui_plan = service._build_initial_ui_plan(
        paper=paper,
        page=1,
        base_payload={
            "blocks": [
                {
                    "id": "fig1",
                    "kind": "caption",
                    "text": "Figure 1. Demo caption",
                    "source_anchor": {"page": 1, "start_char": 0, "end_char": 21},
                }
            ],
            "assets": [],
            "style_cues": {},
            "toc_quality": 0.8,
            "toc_hidden": False,
        },
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    figure_nodes = [node for node in ui_plan.get("components") or [] if node.get("type") == "FigurePanel"]
    assert figure_nodes
    assert ((figure_nodes[0].get("props") or {}).get("image_url")) == ""


def test_build_initial_ui_plan_should_dedupe_main_blocks_and_reduce_inline_slot_density():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=32, title="Demo", venue="PLOS", year=2024, authors=[], doi=None, pdf_url=None, url=None)
    duplicated_text = (
        "This section reports the baseline setup and evaluation protocol for model performance across cohorts."
    )
    ui_plan = service._build_initial_ui_plan(
        paper=paper,
        page=1,
        base_payload={
            "sections": [
                {
                    "title": "Introduction",
                    "level": 1,
                    "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
                }
            ],
            "blocks": [
                {
                    "id": "h1",
                    "kind": "heading",
                    "text": "Introduction",
                    "section_title": "Introduction",
                    "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
                    "zone_type": "main_body",
                },
                {
                    "id": "p1",
                    "kind": "paragraph",
                    "text": duplicated_text,
                    "section_title": "Introduction",
                    "source_anchor": {"page": 1, "start_char": 13, "end_char": 120},
                    "zone_type": "main_body",
                },
                {
                    "id": "p2",
                    "kind": "paragraph",
                    "text": duplicated_text,
                    "section_title": "Introduction",
                    "source_anchor": {"page": 1, "start_char": 121, "end_char": 228},
                    "zone_type": "main_body",
                },
                {
                    "id": "p3",
                    "kind": "paragraph",
                    "text": (
                        "We then compare error distributions across tasks, and the analysis remains grounded on "
                        "aligned evidence snippets for every claim presented in this section."
                    ),
                    "section_title": "Introduction",
                    "source_anchor": {"page": 1, "start_char": 229, "end_char": 410},
                    "zone_type": "main_body",
                },
            ],
            "assets": [],
            "summary": "",
            "style_cues": {},
            "toc_quality": 0.8,
            "toc_hidden": False,
        },
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    nodes = list(ui_plan.get("components") or [])
    paragraph_nodes = [node for node in nodes if node.get("type") == "ParagraphProse"]
    inline_nodes = [node for node in nodes if node.get("type") == "InlineQuerySlot"]
    heading_nodes = [node for node in nodes if node.get("type") == "SectionHeading"]

    assert heading_nodes
    assert len(paragraph_nodes) == 2
    assert inline_nodes == []


def test_reader_compose_should_not_render_page_toc_when_low_quality():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=21, title="Demo", venue="PLOS", year=2024, authors=[], doi=None, pdf_url=None, url=None)
    ui_plan = service._build_initial_ui_plan(
        paper=paper,
        page=1,
        base_payload={
            "sections": [{"title": "Body", "level": 1, "source_anchor": {"page": 1, "start_char": 0, "end_char": 5}}],
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph", "source_anchor": {"page": 1, "start_char": 0, "end_char": 14}}],
            "assets": [],
            "summary": "Summary",
            "style_cues": {},
            "toc_quality": 0.2,
            "toc_hidden": True,
        },
        style_intent="journal_classic",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    toc_nodes = [node for node in ui_plan.get("components") or [] if node.get("type") == "SectionTOC"]
    assert toc_nodes == []
    trace_meta = ui_plan.get("trace_meta") or {}
    assert float(trace_meta.get("toc_quality") or 0.0) == pytest.approx(0.2, rel=0.0, abs=1e-6)
    assert bool(trace_meta.get("toc_hidden")) is True


def test_reader_compose_quality_should_penalize_duplicate_nodes():
    service = LiteratureReaderComposeService()
    quality = service.score_ui_plan(
        ui_plan={
            "plan_id": "p1",
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
            "components": [
                {
                    "id": "head_1",
                    "type": "PaperHeaderCard",
                    "props": {"title": "A Reliable Study Title"},
                    "children": [],
                    "source_anchor_refs": [],
                },
                {
                    "id": "sec_1",
                    "type": "SectionHeading",
                    "props": {"text": "Introduction"},
                    "children": [],
                    "source_anchor_refs": [{"page": 1, "start_char": 0, "end_char": 12}],
                },
                {
                    "id": "p_1",
                    "type": "ParagraphProse",
                    "props": {"text": "Repeated paragraph content for duplicate ratio checks."},
                    "children": [],
                    "source_anchor_refs": [{"page": 1, "start_char": 13, "end_char": 74}],
                },
                {
                    "id": "p_2",
                    "type": "ParagraphProse",
                    "props": {"text": "Repeated paragraph content for duplicate ratio checks."},
                    "children": [],
                    "source_anchor_refs": [{"page": 1, "start_char": 75, "end_char": 136}],
                },
            ],
        },
        base_payload={
            "blocks": [
                {"id": "h1", "kind": "heading", "text": "Introduction"},
                {"id": "p1", "kind": "paragraph", "text": "Repeated paragraph content for duplicate ratio checks."},
            ],
            "assets": [],
            "style_cues": {},
            "side_context_blocks": [],
            "toc_quality": 0.7,
            "toc_hidden": False,
        },
        validation_errors=[],
        quality_target=0.86,
    )

    assert float(quality.get("duplicate_ratio") or 0.0) > 0.1
    deductions = list(quality.get("deductions") or [])
    assert any(str(item.get("item") or "") == "duplicate_content" for item in deductions)


def test_reader_compose_quality_should_include_layout_metrics():
    service = LiteratureReaderComposeService()
    quality = service.score_ui_plan(
        ui_plan={
            "plan_id": "p1",
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
            "components": [
                {"id": "h1", "type": "PaperHeaderCard", "props": {"title": "Demo title"}, "children": [], "source_anchor_refs": []},
                {"id": "t1", "type": "SectionTOC", "props": {"items": [], "hidden_reason": "TOC quality too low, hidden."}, "children": [], "source_anchor_refs": []},
                {"id": "p1", "type": "ParagraphProse", "props": {"text": "demo body"}, "children": [], "source_anchor_refs": [{"page": 1, "start_char": 0, "end_char": 8}]},
                {"id": "c1", "type": "ContextRail", "props": {"items": [{"text": "OPEN ACCESS"}]}, "children": [], "source_anchor_refs": []},
            ],
        },
        base_payload={
            "blocks": [{"id": "b1", "kind": "heading", "text": "Introduction"}],
            "assets": [],
            "style_cues": {"layout_mode": "two_column"},
            "side_context_blocks": [{"id": "sb1", "text": "OPEN ACCESS"}],
            "cross_column_merge_ratio": 0.03,
            "toc_quality": 0.2,
            "toc_hidden": True,
            "mm_assist_meta": {"used": True, "model": "qwen3-vl-flash", "fallback_used": True},
        },
        validation_errors=[],
        quality_target=0.86,
    )

    assert "cross_column_merge_ratio" in quality
    assert "sidebar_recall" in quality
    assert "toc_quality" in quality
    assert "anchor_coverage_ratio" in quality
    assert "evidence_image_ready" in quality
    assert quality.get("mm_assist_used") is True


def test_reader_compose_quality_should_fail_anchor_gate_on_misaligned_quote():
    service = LiteratureReaderComposeService()
    quality = service.score_ui_plan(
        ui_plan={
            "plan_id": "p1",
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
            "components": [
                {
                    "id": "p1",
                    "type": "ParagraphProse",
                    "props": {"text": "This is the visible paragraph text.", "source_block_id": "b1"},
                    "children": [],
                    "source_anchor_refs": [
                        {
                            "page": 1,
                            "start_char": 0,
                            "end_char": 8,
                            "quote_text": "unrelated evidence phrase",
                            "canonical_block_id": "b1",
                            "coord_version": "anchor_v2",
                            "anchor_confidence": 0.92,
                        }
                    ],
                }
            ],
        },
        base_payload={
            "raw_text": "This is the visible paragraph text.",
            "style_cues": {
                "line_layout": [
                    {"text": "This is the visible paragraph text.", "x0": 80, "x1": 620, "top": 120, "bottom": 138},
                ]
            },
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "This is the visible paragraph text."}],
            "assets": [],
            "toc_quality": 0.0,
            "toc_hidden": True,
        },
        validation_errors=[],
        quality_target=0.86,
    )

    assert quality.get("anchor_gate_passed") is False
    assert float(quality.get("anchor_quote_hit_rate") or 0) <= 0.2
    assert float(quality.get("anchor_misjump_rate") or 0) >= 0.8


def test_reader_compose_long_paragraph_should_build_single_stable_anchor():
    service = LiteratureReaderComposeService()
    quote_text = " ".join([f"Sentence {idx}." for idx in range(1, 40)])
    refs = service._build_segmented_anchor_refs(
        anchor={"page": 1, "start_char": 100, "end_char": 4200},
        page=1,
        quote_text=quote_text,
        style_cues={
            "page_width": 900,
            "page_height": 1200,
            "line_layout": [
                {"text": "Sentence 1.", "x0": 80, "x1": 620, "top": 120, "bottom": 138, "column_label": "main"},
                {"text": "Sentence 8.", "x0": 80, "x1": 620, "top": 142, "bottom": 160, "column_label": "main"},
                {"text": "Sentence 16.", "x0": 80, "x1": 620, "top": 164, "bottom": 182, "column_label": "main"},
            ],
        },
    )

    assert len(refs) == 1
    row = refs[0]
    assert int(row.get("end_char") or 0) > int(row.get("start_char") or 0)
    assert row.get("anchor_id")
    assert row.get("segment_index") is None
    assert row.get("segment_total") is None


def test_reader_compose_bbox_hint_should_union_multi_line_rows():
    service = LiteratureReaderComposeService()
    bbox = service._build_bbox_hint(
        style_cues={
            "page_width": 900,
            "page_height": 1200,
            "line_layout": [
                {"text": "Intro line A", "x0": 100, "x1": 520, "top": 200, "bottom": 218, "column_label": "main"},
                {"text": "Intro line B", "x0": 102, "x1": 518, "top": 220, "bottom": 238, "column_label": "main"},
                {"text": "Intro line C", "x0": 105, "x1": 516, "top": 240, "bottom": 258, "column_label": "main"},
            ],
        },
        quote_text="Intro line A Intro line B Intro line C",
        source_anchor={"page": 1, "start_char": 10, "end_char": 160},
    )

    assert isinstance(bbox, dict)
    assert float(bbox.get("top") or 0) <= 200
    assert float(bbox.get("bottom") or 0) >= 258


def test_reader_compose_bbox_hint_should_prefer_line_layout_coordinate_space_when_style_dims_mismatch():
    service = LiteratureReaderComposeService()
    bbox = service._build_bbox_hint(
        style_cues={
            "page_width": 612,
            "page_height": 792,
            "line_layout": [
                {"text": "We first examined the frequency", "x0": 510, "x1": 1356, "top": 1396, "bottom": 1425, "column_label": "main"},
                {"text": "of insight overall", "x0": 479, "x1": 1394, "top": 1681, "bottom": 1709, "column_label": "main"},
            ],
        },
        quote_text="We first examined the frequency of insight overall",
        source_anchor={"page": 7, "start_char": 0, "end_char": 120},
    )

    assert isinstance(bbox, dict)
    assert float(bbox.get("x1") or 0) == pytest.approx(1394.0)
    assert float(bbox.get("page_width") or 0) >= 1394.0
    assert float(bbox.get("page_height") or 0) >= 1709.0


def test_reader_compose_node_gate_should_rebuild_implausible_anchor_bbox_from_style_cues():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "components": [
            {
                "id": "paragraph_17",
                "type": "ParagraphProse",
                "props": {
                    "text": (
                        "We first examined the frequency (prevalence) of insight. "
                        "Overall, ChatGPT produced at least one significant insight."
                    )
                },
                "source_block_ids": ["p7_dm_p7_l010_b001"],
                "source_anchor_refs": [
                    {
                        "anchor_id": "bad_anchor_1",
                        "canonical_block_id": "p7_dm_p7_l010_b001",
                        "page": 7,
                        "start_char": 0,
                        "end_char": 128,
                        "quote_text": "We first examined the frequency (prevalence) of insight.",
                        "bbox_hint": {
                            "x0": 510.0,
                            "x1": 1356.0,
                            "top": 1396.0,
                            "bottom": 1425.0,
                            "page_width": 612.0,
                            "page_height": 792.0,
                        },
                        "geometry": {
                            "page_width": 612.0,
                            "page_height": 792.0,
                            "polygons": [
                                {
                                    "points": [
                                        {"x": 510.0, "y": 1396.0},
                                        {"x": 1356.0, "y": 1396.0},
                                        {"x": 1356.0, "y": 1425.0},
                                        {"x": 510.0, "y": 1425.0},
                                    ]
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }
    base_payload = {
        "blocks": [
            {
                "id": "dm_p7_l010_b001",
                "source_anchor": {"canonical_block_id": "p7_dm_p7_l010_b001"},
            }
        ],
        "style_cues": {
            "page_width": 612.0,
            "page_height": 792.0,
            "line_layout": [
                {
                    "text": "We first examined the frequency (prevalence) of insight.",
                    "x0": 211.97,
                    "x1": 559.42,
                    "top": 577.04,
                    "bottom": 587.04,
                },
                {
                    "text": "Overall, ChatGPT produced at least one significant insight.",
                    "x0": 200.01,
                    "x1": 573.88,
                    "top": 592.00,
                    "bottom": 606.00,
                },
            ],
        },
    }

    gated = service._apply_node_level_anchor_gate(ui_plan=ui_plan, base_payload=base_payload, page=7)
    refs = gated["ui_plan"]["components"][0]["source_anchor_refs"]
    assert len(refs) == 1
    bbox = refs[0]["bbox_hint"]
    assert float(bbox["x0"]) == pytest.approx(211.97)
    assert float(bbox["x1"]) == pytest.approx(559.42)
    assert float(bbox["top"]) == pytest.approx(577.04)
    assert float(bbox["bottom"]) == pytest.approx(587.04)
    geometry = refs[0]["geometry"]
    assert float(geometry["page_width"]) == pytest.approx(612.0)
    assert float(geometry["page_height"]) == pytest.approx(792.0)
    assert float(geometry["polygons"][0]["points"][0]["x"]) == pytest.approx(211.97)


def test_sanitize_ui_plan_anchors_should_rebuild_implausible_spatial_hints():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "components": [
            {
                "id": "paragraph_17",
                "type": "ParagraphProse",
                "props": {"text": "We first examined the frequency (prevalence) of insight."},
                "children": [],
                "source_block_ids": ["p7_dm_p7_l010_b001"],
                "source_anchor_refs": [
                    {
                        "anchor_id": "bad_anchor_1",
                        "canonical_block_id": "p7_dm_p7_l010_b001",
                        "page": 7,
                        "start_char": 0,
                        "end_char": 85,
                        "quote_text": "We first examined the frequency (prevalence) of insight.",
                        "bbox_hint": {
                            "x0": 510.0,
                            "x1": 1356.0,
                            "top": 1396.0,
                            "bottom": 1425.0,
                            "page_width": 612.0,
                            "page_height": 792.0,
                        },
                        "geometry": {
                            "page_width": 612.0,
                            "page_height": 792.0,
                            "polygons": [
                                {
                                    "points": [
                                        {"x": 510.0, "y": 1396.0},
                                        {"x": 1356.0, "y": 1396.0},
                                        {"x": 1356.0, "y": 1425.0},
                                        {"x": 510.0, "y": 1425.0},
                                    ]
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }
    base_payload = {
        "blocks": [{"id": "dm_p7_l010_b001", "source_anchor": {"canonical_block_id": "p7_dm_p7_l010_b001"}}],
        "style_cues": {
            "page_width": 612.0,
            "page_height": 792.0,
            "line_layout": [
                {
                    "text": "We first examined the frequency (prevalence) of insight.",
                    "x0": 211.97,
                    "x1": 559.42,
                    "top": 577.04,
                    "bottom": 587.04,
                }
            ],
        },
    }

    sanitized = service._sanitize_ui_plan_anchors(ui_plan, page=7, base_payload=base_payload)
    refs = sanitized["components"][0]["source_anchor_refs"]
    assert len(refs) == 1
    bbox = refs[0]["bbox_hint"]
    assert float(bbox["x0"]) == pytest.approx(211.97)
    assert float(bbox["x1"]) == pytest.approx(559.42)
    assert float(bbox["top"]) == pytest.approx(577.04)
    assert float(bbox["bottom"]) == pytest.approx(587.04)


def test_ensure_payload_contract_should_rebuild_implausible_runtime_spatial_hints():
    service = LiteratureReaderComposeService()
    payload = {
        "style_cues": {
            "page_width": 612.0,
            "page_height": 792.0,
            "line_layout": [
                {
                    "text": "We first examined the frequency (prevalence) of insight.",
                    "x0": 211.97,
                    "x1": 559.42,
                    "top": 577.04,
                    "bottom": 587.04,
                }
            ],
        },
        "ui_plan": {
            "components": [
                {
                    "id": "paragraph_17",
                    "type": "ParagraphProse",
                    "props": {"text": "We first examined the frequency (prevalence) of insight."},
                    "children": [],
                    "source_block_ids": ["p7_dm_p7_l010_b001"],
                    "source_anchor_refs": [
                        {
                            "anchor_id": "bad_anchor_1",
                            "canonical_block_id": "p7_dm_p7_l010_b001",
                            "page": 7,
                            "start_char": 0,
                            "end_char": 85,
                            "quote_text": "We first examined the frequency (prevalence) of insight.",
                            "bbox_hint": {
                                "x0": 510.0,
                                "x1": 1356.0,
                                "top": 1396.0,
                                "bottom": 1425.0,
                                "page_width": 612.0,
                                "page_height": 792.0,
                            },
                            "geometry": {
                                "page_width": 612.0,
                                "page_height": 792.0,
                                "polygons": [
                                    {
                                        "points": [
                                            {"x": 510.0, "y": 1396.0},
                                            {"x": 1356.0, "y": 1396.0},
                                            {"x": 1356.0, "y": 1425.0},
                                            {"x": 510.0, "y": 1425.0},
                                        ]
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        },
    }

    sanitized = service._ensure_payload_contract(page=7, payload=payload)
    refs = sanitized["ui_plan"]["components"][0]["source_anchor_refs"]
    assert len(refs) == 1
    bbox = refs[0]["bbox_hint"]
    assert float(bbox["x0"]) == pytest.approx(211.97)
    assert float(bbox["x1"]) == pytest.approx(559.42)
    assert float(bbox["top"]) == pytest.approx(577.04)
    assert float(bbox["bottom"]) == pytest.approx(587.04)


def test_sanitize_ui_plan_should_keep_only_actionable_anchor_refs():
    service = LiteratureReaderComposeService()
    raw_plan = {
        "plan_id": "p1",
        "components": [
            {
                "id": "n1",
                "type": "ParagraphProse",
                "props": {"text": "Demo paragraph", "source_block_id": "b1"},
                "children": [],
                "source_anchor_refs": [
                    {
                        "page": 1,
                        "start_char": 0,
                        "end_char": 32,
                        "quote_text": "Demo paragraph",
                        "canonical_block_id": "b1",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.91,
                    },
                    {
                        "page": 1,
                        "start_char": 0,
                        "end_char": 32,
                        "quote_text": "Demo paragraph",
                        "canonical_block_id": "b1",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.4,
                    },
                    {
                        "page": 1,
                        "start_char": 0,
                        "end_char": 32,
                        "quote_text": "Demo paragraph",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.95,
                    },
                    {
                        "page": 1,
                        "start_char": 0,
                        "end_char": 32,
                        "quote_text": "Demo paragraph",
                        "canonical_block_id": "b1",
                        "coord_version": "anchor_v2",
                        "segment_index": 1,
                        "segment_total": 2,
                        "anchor_confidence": 0.95,
                    },
                ],
            }
        ],
    }
    base_payload = {
        "blocks": [{"id": "b1", "kind": "paragraph", "page": 1}],
        "side_context_blocks": [],
        "figure_meta_blocks": [],
    }

    sanitized = service._sanitize_ui_plan_anchors(raw_plan, page=1, base_payload=base_payload)
    anchors = list((sanitized.get("components") or [{}])[0].get("source_anchor_refs") or [])
    assert len(anchors) == 2
    assert all(str(item.get("canonical_block_id") or "") == "p1_b1" for item in anchors)
    assert all(str(item.get("coord_version") or "") == "anchor_v2" for item in anchors)
    assert all(float(item.get("anchor_confidence") or 0.0) >= 0.78 for item in anchors)


def test_anchor_eval_should_pass_with_high_hit_and_iou():
    service = LiteratureReaderComposeService()
    style_cues = {
        "page_width": 900,
        "page_height": 1200,
        "line_layout": [
            {
                "text": "This is the visible paragraph text.",
                "x0": 80,
                "x1": 620,
                "top": 120,
                "bottom": 138,
                "column_label": "main",
            }
        ],
    }
    source_anchor = {
        "page": 1,
        "start_char": 0,
        "end_char": 35,
        "quote_text": "This is the visible paragraph text.",
        "canonical_block_id": "p1_b1",
        "coord_version": "anchor_v2",
        "anchor_confidence": 0.95,
    }
    bbox = service._build_bbox_hint(
        style_cues=style_cues,
        quote_text=str(source_anchor.get("quote_text") or ""),
        source_anchor=source_anchor,
    )
    assert isinstance(bbox, dict)
    source_anchor["bbox_hint"] = bbox
    eval_result = service._evaluate_anchor_metrics(
        ui_plan={
            "components": [
                {
                    "id": "p1",
                    "type": "ParagraphProse",
                    "props": {"text": "This is the visible paragraph text."},
                    "children": [],
                    "source_anchor_refs": [source_anchor],
                }
            ]
        },
        base_payload={
            "raw_text": "This is the visible paragraph text.",
            "style_cues": style_cues,
        },
    )
    assert float(eval_result.get("hit_rate") or 0.0) >= 0.8
    assert float(eval_result.get("bbox_iou") or 0.0) >= 0.25
    assert float(eval_result.get("misjump_rate", 1.0)) <= 0.2
    assert bool(eval_result.get("gate_passed")) is True


def test_anchor_eval_should_fail_when_iou_below_gate():
    service = LiteratureReaderComposeService()
    eval_result = service._evaluate_anchor_metrics(
        ui_plan={
            "components": [
                {
                    "id": "p1",
                    "type": "ParagraphProse",
                    "props": {"text": "This is the visible paragraph text."},
                    "children": [],
                    "source_anchor_refs": [
                        {
                            "page": 1,
                            "start_char": 0,
                            "end_char": 35,
                            "quote_text": "This is the visible paragraph text.",
                            "canonical_block_id": "p1_b1",
                            "coord_version": "anchor_v2",
                            "anchor_confidence": 0.95,
                            "bbox_hint": {
                                "x0": 700,
                                "x1": 860,
                                "top": 900,
                                "bottom": 980,
                                "page_width": 900,
                                "page_height": 1200,
                            },
                        }
                    ],
                }
            ]
        },
        base_payload={
            "raw_text": "This is the visible paragraph text.",
            "style_cues": {
                "page_width": 900,
                "page_height": 1200,
                "line_layout": [
                    {
                        "text": "This is the visible paragraph text.",
                        "x0": 80,
                        "x1": 620,
                        "top": 120,
                        "bottom": 138,
                        "column_label": "main",
                    }
                ],
            },
        },
    )
    assert float(eval_result.get("hit_rate") or 0.0) >= 0.8
    assert float(eval_result.get("bbox_iou", 1.0)) < 0.25
    assert bool(eval_result.get("gate_passed")) is False


def test_node_level_anchor_gate_should_not_strip_entire_page():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "plan_id": "p1",
        "components": [
            {
                "id": "n_good",
                "type": "ParagraphProse",
                "props": {"text": "Good paragraph", "source_block_id": "p1_b1"},
                "children": [],
                "capabilities": ["jump_anchor", "copy"],
                "actions": [{"key": "jump_anchor", "label": "Jump"}],
                "source_anchor_refs": [
                    {
                        "page": 1,
                        "start_char": 10,
                        "end_char": 40,
                        "quote_text": "Good paragraph",
                        "canonical_block_id": "p1_b1",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.92,
                    }
                ],
            },
            {
                "id": "n_bad",
                "type": "ParagraphProse",
                "props": {"text": "Bad paragraph", "source_block_id": "p1_b2"},
                "children": [],
                "capabilities": ["jump_anchor", "copy"],
                "actions": [{"key": "jump_anchor", "label": "Jump"}],
                "source_anchor_refs": [
                    {
                        "page": 1,
                        "start_char": 50,
                        "end_char": 70,
                        "quote_text": "Bad paragraph",
                        "canonical_block_id": "p1_b2",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.2,
                    }
                ],
            },
        ],
    }
    base_payload = {
        "blocks": [
            {"id": "b1", "page": 1, "kind": "paragraph"},
            {"id": "b2", "page": 1, "kind": "paragraph"},
        ]
    }

    gated = service._apply_node_level_anchor_gate(ui_plan=ui_plan, base_payload=base_payload, page=1)
    nodes = list((gated.get("ui_plan") or {}).get("components") or [])
    assert len(nodes) == 2
    good = next(item for item in nodes if item.get("id") == "n_good")
    bad = next(item for item in nodes if item.get("id") == "n_bad")

    assert len(list(good.get("source_anchor_refs") or [])) == 1
    assert bool((good.get("props") or {}).get("node_gate_passed")) is True

    assert list(bad.get("source_anchor_refs") or []) == []
    assert bool((bad.get("props") or {}).get("node_gate_passed")) is False
    bad_action_keys = [str(item.get("key") or "") for item in list(bad.get("actions") or [])]
    assert "jump_anchor" not in bad_action_keys

    report = dict(gated.get("node_gate_report") or {})
    assert int(report.get("total_nodes") or 0) >= 2
    assert int(report.get("blocked_nodes") or 0) >= 1


def test_build_main_blocks_from_segment_map_should_follow_segment_order():
    service = LiteratureReaderComposeService()
    blocks = service._normalize_blocks_for_render(
        blocks=[
            {
                "id": "b1",
                "kind": "heading",
                "text": "Introduction",
                "page": 1,
                "order": 1,
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
            },
            {
                "id": "b2",
                "kind": "paragraph",
                "text": "We evaluated the model on USMLE exams.",
                "page": 1,
                "order": 2,
                "source_anchor": {"page": 1, "start_char": 13, "end_char": 52},
            },
        ],
        page=1,
    )
    segment_map = {
        "segments": [
            {
                "segment_id": "seg_1",
                "kind": "heading",
                "ui_component": "SectionHeading",
                "block_ids": ["p1_b1"],
                "title": "Introduction",
            },
            {
                "segment_id": "seg_2",
                "kind": "paragraph",
                "ui_component": "ParagraphProse",
                "block_ids": ["p1_b2"],
            },
        ]
    }

    rows = service._build_main_blocks_from_segment_map(page=1, blocks=blocks, segment_map=segment_map)
    assert len(rows) == 2
    assert str(rows[0].get("kind") or "") == "heading"
    assert str(rows[0].get("text") or "") == "Introduction"
    assert str(rows[1].get("kind") or "") == "paragraph"
    assert "USMLE" in str(rows[1].get("text") or "")


def test_build_main_blocks_from_segment_map_prefers_line_ids_and_evidence_lines():
    service = LiteratureReaderComposeService()
    blocks = service._normalize_blocks_for_render(
        blocks=[
            {
                "id": "b1",
                "kind": "paragraph",
                "text": "Legacy parser block text that should not dominate.",
                "page": 1,
                "order": 1,
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 48},
            },
        ],
        page=1,
    )
    base_payload = {
        "line_catalog": [
            {
                "line_id": "p1_l001_main_left",
                "page": 1,
                "order": 0,
                "text": "This is line one from multimodal planning.",
                "column_label": "main",
                "x0": 10,
                "x1": 500,
                "top": 100,
                "bottom": 120,
                "start_char": 100,
                "end_char": 140,
                "page_width": 612,
                "page_height": 792,
            },
            {
                "line_id": "p1_l002_main_left",
                "page": 1,
                "order": 1,
                "text": "This is line two from multimodal planning.",
                "column_label": "main",
                "x0": 10,
                "x1": 500,
                "top": 122,
                "bottom": 142,
                "start_char": 141,
                "end_char": 182,
                "page_width": 612,
                "page_height": 792,
            },
        ]
    }
    segment_map = {
        "segments": [
            {
                "segment_id": "seg_intro_p1",
                "kind": "paragraph",
                "ui_component": "ParagraphProse",
                "line_ids": ["p1_l001_main_left", "p1_l002_main_left"],
                "evidence_line_ids": ["p1_l001_main_left"],
            }
        ]
    }

    rows = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map=segment_map,
        base_payload=base_payload,
    )
    assert len(rows) == 1
    assert str(rows[0].get("id") or "").startswith("p1_seg_")
    assert "line one from multimodal planning" in str(rows[0].get("text") or "").lower()
    assert list(rows[0].get("source_line_ids") or []) == ["p1_l001_main_left", "p1_l002_main_left"]
    assert list(rows[0].get("evidence_line_ids") or []) == ["p1_l001_main_left"]
    anchor = dict(rows[0].get("source_anchor") or {})
    bbox = dict(anchor.get("bbox_hint") or {})
    assert float(bbox.get("x1") or 0) > float(bbox.get("x0") or 0)
    assert float(bbox.get("bottom") or 0) > float(bbox.get("top") or 0)


def test_node_gate_should_allow_segment_generated_canonical_block_id():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "plan_id": "p1",
        "components": [
            {
                "id": "n_seg",
                "type": "ParagraphProse",
                "props": {"text": "Segment text", "source_block_id": "p1_seg_seg_intro_p1"},
                "children": [],
                "capabilities": ["jump_anchor", "copy"],
                "actions": [{"key": "jump_anchor", "label": "Locate"}],
                "source_anchor_refs": [
                    {
                        "page": 1,
                        "start_char": 100,
                        "end_char": 182,
                        "quote_text": "Segment text",
                        "canonical_block_id": "p1_seg_seg_intro_p1",
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.9,
                    }
                ],
            }
        ],
    }
    base_payload = {
        "blocks": [{"id": "b1", "page": 1, "kind": "paragraph"}],
        "segment_map": {
            "segments": [
                {
                    "segment_id": "seg_intro_p1",
                    "ui_component": "ParagraphProse",
                    "kind": "paragraph",
                    "line_ids": ["p1_l001_main_left"],
                    "evidence_line_ids": ["p1_l001_main_left"],
                }
            ]
        },
    }
    gated = service._apply_node_level_anchor_gate(ui_plan=ui_plan, base_payload=base_payload, page=1)
    nodes = list((gated.get("ui_plan") or {}).get("components") or [])
    assert len(nodes) == 1
    refs = list((nodes[0] or {}).get("source_anchor_refs") or [])
    assert len(refs) == 1
    assert str(refs[0].get("canonical_block_id") or "") == "p1_seg_seg_intro_p1"


@pytest.mark.asyncio
async def test_apply_deepseek_assembly_decision_should_apply_order_drop_and_type_override(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_compose_layout_llm_enabled", True)

    class _FakeLLM:
        async def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return {
                "content": json.dumps(
                    {
                        "ordered_node_ids": ["n2", "n1"],
                        "drop_node_ids": ["n3"],
                        "type_override": {"n2": "ListBlock"},
                    },
                    ensure_ascii=False,
                ),
                "model": "deepseek-chat",
            }

    async def _fake_get_llm_service():  # type: ignore[no-untyped-def]
        return _FakeLLM()

    monkeypatch.setattr(
        "app.services.literature_reader_compose_service.get_llm_service",
        _fake_get_llm_service,
    )

    ui_plan = {
        "plan_id": "p1",
        "components": [
            {"id": "n1", "type": "ParagraphProse", "props": {"text": "first paragraph"}, "children": [], "source_anchor_refs": []},
            {"id": "n2", "type": "ParagraphProse", "props": {"text": "second paragraph"}, "children": [], "source_anchor_refs": []},
            {"id": "n3", "type": "SectionHeading", "props": {"text": "Dropped heading", "level": 2}, "children": [], "source_anchor_refs": []},
        ],
        "trace_meta": {},
    }
    base_payload = {
        "segment_map": {
            "segments": [
                {"segment_id": "seg_1", "kind": "paragraph", "component_hint": "ParagraphProse", "line_ids": ["p1_l001_main_left"]},
            ]
        },
        "layout_channels": {"main_body": ["p1_b1"]},
    }

    decided = await service._apply_deepseek_assembly_decision(
        ui_plan=ui_plan,
        base_payload=base_payload,
        page=1,
        latency_budget_ms=8500,
    )
    nodes = list(decided.get("components") or [])
    assert [str(item.get("id") or "") for item in nodes] == ["n2", "n1"]
    assert str((nodes[0] or {}).get("type") or "") == "ListBlock"
    assert list(((nodes[0] or {}).get("props") or {}).get("items") or [])
    trace_meta = dict(decided.get("trace_meta") or {})
    assert bool(trace_meta.get("assembly_used")) is True
    assert str(trace_meta.get("assembly_model") or "") == "deepseek-chat"


@pytest.mark.asyncio
async def test_apply_deepseek_assembly_decision_should_fallback_on_invalid_node_id(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_compose_layout_llm_enabled", True)

    class _FakeLLM:
        async def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return {"content": '{"ordered_node_ids":["missing_node"]}', "model": "deepseek-chat"}

    async def _fake_get_llm_service():  # type: ignore[no-untyped-def]
        return _FakeLLM()

    monkeypatch.setattr(
        "app.services.literature_reader_compose_service.get_llm_service",
        _fake_get_llm_service,
    )

    ui_plan = {
        "plan_id": "p1",
        "components": [
            {"id": "n1", "type": "ParagraphProse", "props": {"text": "first paragraph"}, "children": [], "source_anchor_refs": []},
            {"id": "n2", "type": "ParagraphProse", "props": {"text": "second paragraph"}, "children": [], "source_anchor_refs": []},
        ],
        "trace_meta": {},
    }
    base_payload = {
        "segment_map": {"segments": [{"segment_id": "seg_1", "kind": "paragraph", "line_ids": ["p1_l001_main_left"]}]},
        "layout_channels": {"main_body": ["p1_b1"]},
    }

    decided = await service._apply_deepseek_assembly_decision(
        ui_plan=ui_plan,
        base_payload=base_payload,
        page=1,
        latency_budget_ms=8500,
    )
    nodes = list(decided.get("components") or [])
    assert [str(item.get("id") or "") for item in nodes] == ["n1", "n2"]
    trace_meta = dict(decided.get("trace_meta") or {})
    assert bool(trace_meta.get("assembly_used")) is False
    assert str(trace_meta.get("assembly_fallback_reason") or "") == "assembly_invalid_node_id_in_order"


@pytest.mark.asyncio
async def test_apply_deepseek_assembly_decision_should_ignore_invalid_type_override(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_compose_layout_llm_enabled", True)

    class _FakeLLM:
        async def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return {
                "content": '{"ordered_node_ids":["n1","n2"],"type_override":{"n1":"FigurePanel"}}',
                "model": "deepseek-chat",
            }

    async def _fake_get_llm_service():  # type: ignore[no-untyped-def]
        return _FakeLLM()

    monkeypatch.setattr(
        "app.services.literature_reader_compose_service.get_llm_service",
        _fake_get_llm_service,
    )

    ui_plan = {
        "plan_id": "p1",
        "components": [
            {"id": "n1", "type": "ParagraphProse", "props": {"text": "first paragraph"}, "children": [], "source_anchor_refs": []},
            {"id": "n2", "type": "ParagraphProse", "props": {"text": "second paragraph"}, "children": [], "source_anchor_refs": []},
        ],
        "trace_meta": {},
    }
    base_payload = {
        "segment_map": {"segments": [{"segment_id": "seg_1", "kind": "paragraph", "line_ids": ["p1_l001_main_left"]}]},
        "layout_channels": {"main_body": ["p1_b1"]},
    }

    decided = await service._apply_deepseek_assembly_decision(
        ui_plan=ui_plan,
        base_payload=base_payload,
        page=1,
        latency_budget_ms=8500,
    )
    nodes = list(decided.get("components") or [])
    assert [str(item.get("id") or "") for item in nodes] == ["n1", "n2"]
    assert str((nodes[0] or {}).get("type") or "") == "ParagraphProse"
    trace_meta = dict(decided.get("trace_meta") or {})
    assert bool(trace_meta.get("assembly_used")) is True


def test_mm_validate_line_parse_advice_should_accept_doc_nav_tree_v2():
    service = ReaderMultimodalLayoutService()
    parsed = service.validate_line_parse_advice_json(
        payload={
            "doc_nav_tree": [
                {
                    "node_id": "h_intro",
                    "type": "heading",
                    "title": "Introduction",
                    "line_ids": ["p1_l001_main_left"],
                    "children": [],
                }
            ],
            "block_groups": [
                {
                    "block_id": "blk_p1",
                    "kind": "paragraph",
                    "title": "",
                    "parent_node_id": "h_intro",
                    "line_ids": [],
                    "word_ids": ["w000001", "w000002"],
                    "char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000020"}],
                    "zone_type": "main_body",
                    "column_id": "main_left",
                    "reading_order": 1,
                    "confidence": 0.92,
                }
            ],
            "counts": {"heading_count": 1, "paragraph_count": 1, "figure_count": 0, "table_count": 0, "block_count": 1},
            "notes": [],
        },
        valid_line_ids={"p1_l001_main_left"},
        valid_word_ids={"w000001", "w000002"},
        valid_char_ids=["c000001", "c000020"],
    )
    assert isinstance(parsed, dict)
    assert isinstance(parsed.get("doc_nav_tree"), list)
    assert len(list(parsed.get("block_groups") or [])) == 1


def test_build_main_blocks_from_segment_map_should_emit_polygon_geometry_from_word_ids():
    service = LiteratureReaderComposeService()
    blocks = [
        {
            "id": "b1",
            "page": 1,
            "kind": "paragraph",
            "text": "Alpha Beta",
            "order": 1,
            "section_title": "Introduction",
            "source_anchor": {"page": 1, "start_char": 0, "end_char": 40, "canonical_block_id": "p1_b1"},
            "zone_type": "main_body",
            "column_id": "main_left",
            "heading_prob": 0.0,
            "layout_confidence": 0.9,
        }
    ]
    output = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map={
            "segments": [
                {
                    "segment_id": "blk_p1",
                    "kind": "paragraph",
                    "kind_hint": "paragraph",
                    "component_hint": "ParagraphProse",
                    "line_ids": [],
                    "evidence_line_ids": [],
                    "word_ids": ["w000001", "w000002"],
                    "char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000012"}],
                    "block_ids": ["p1_b1"],
                    "title": "Introduction",
                }
            ]
        },
        base_payload={
            "line_catalog": [],
            "native_page_extract": {
                "page_meta": {"page": 1, "page_width": 840, "page_height": 1188},
                "words": [
                    {"word_id": "w000001", "text": "Alpha", "x0": 120, "x1": 170, "top": 220, "bottom": 236},
                    {"word_id": "w000002", "text": "Beta", "x0": 174, "x1": 214, "top": 220, "bottom": 236},
                ],
            },
        },
    )
    assert output
    anchor = dict((output[0] or {}).get("source_anchor") or {})
    assert str(anchor.get("geometry_version") or "") == "poly_v1"
    geometry = dict(anchor.get("geometry") or {})
    polygons = list(geometry.get("polygons") or [])
    assert len(polygons) >= 1
    assert len(list((polygons[0] or {}).get("points") or [])) >= 3


def test_build_main_blocks_from_segment_map_should_not_reuse_segment_word_ids_across_blocks():
    service = LiteratureReaderComposeService()
    blocks = service._normalize_blocks_for_render(
        blocks=[
            {
                "id": "b1",
                "page": 1,
                "kind": "paragraph",
                "text": "Alpha Beta",
                "order": 1,
                "section_title": "Intro",
                "source_anchor": {
                    "page": 1,
                    "start_char": 0,
                    "end_char": 30,
                    "canonical_block_id": "p1_b1",
                    "source_word_ids": ["w000001", "w000002"],
                    "source_char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000010"}],
                },
            },
            {
                "id": "b2",
                "page": 1,
                "kind": "paragraph",
                "text": "Gamma Delta",
                "order": 2,
                "section_title": "Intro",
                "source_anchor": {
                    "page": 1,
                    "start_char": 31,
                    "end_char": 80,
                    "canonical_block_id": "p1_b2",
                    "source_word_ids": ["w000003", "w000004"],
                    "source_char_ranges": [{"start_char_id": "c000011", "end_char_id": "c000020"}],
                },
            },
        ],
        page=1,
    )
    segment_map = {
        "segments": [
            {
                "segment_id": "seg_mix",
                "kind": "paragraph",
                "kind_hint": "paragraph",
                "component_hint": "ParagraphProse",
                "block_ids": ["p1_b1", "p1_b2"],
                "line_ids": ["p1_l001_main_left", "p1_l002_main_left"],
                "word_ids": ["w000001", "w000002", "w000003", "w000004"],  # intentionally mixed
                "char_ranges": [{"start_char_id": "c000001", "end_char_id": "c000020"}],
            }
        ]
    }
    base_payload = {
        "line_catalog": [
            {
                "line_id": "p1_l001_main_left",
                "page": 1,
                "order": 0,
                "text": "Alpha Beta",
                "start_char": 0,
                "end_char": 24,
                "x0": 100,
                "x1": 320,
                "top": 120,
                "bottom": 140,
                "column_label": "main_left",
                "words": [
                    {"word_id": "w000001", "text": "Alpha", "x0": 100, "x1": 150, "top": 120, "bottom": 140},
                    {"word_id": "w000002", "text": "Beta", "x0": 156, "x1": 196, "top": 120, "bottom": 140},
                ],
            },
            {
                "line_id": "p1_l002_main_left",
                "page": 1,
                "order": 1,
                "text": "Gamma Delta",
                "start_char": 40,
                "end_char": 70,
                "x0": 100,
                "x1": 340,
                "top": 160,
                "bottom": 180,
                "column_label": "main_left",
                "words": [
                    {"word_id": "w000003", "text": "Gamma", "x0": 100, "x1": 154, "top": 160, "bottom": 180},
                    {"word_id": "w000004", "text": "Delta", "x0": 160, "x1": 212, "top": 160, "bottom": 180},
                ],
            },
        ],
        "native_page_extract": {
            "page_meta": {"page": 1, "page_width": 840, "page_height": 1188},
            "words": [
                {
                    "word_id": "w000001",
                    "text": "Alpha",
                    "x0": 100,
                    "x1": 150,
                    "top": 120,
                    "bottom": 140,
                    "start_char_id": "c000001",
                    "end_char_id": "c000005",
                },
                {
                    "word_id": "w000002",
                    "text": "Beta",
                    "x0": 156,
                    "x1": 196,
                    "top": 120,
                    "bottom": 140,
                    "start_char_id": "c000006",
                    "end_char_id": "c000010",
                },
                {
                    "word_id": "w000003",
                    "text": "Gamma",
                    "x0": 100,
                    "x1": 154,
                    "top": 160,
                    "bottom": 180,
                    "start_char_id": "c000011",
                    "end_char_id": "c000015",
                },
                {
                    "word_id": "w000004",
                    "text": "Delta",
                    "x0": 160,
                    "x1": 212,
                    "top": 160,
                    "bottom": 180,
                    "start_char_id": "c000016",
                    "end_char_id": "c000020",
                },
            ],
            "chars": [{"char_id": f"c{idx:06d}"} for idx in range(1, 32)],
        },
    }

    output = service._build_main_blocks_from_segment_map(
        page=1,
        blocks=blocks,
        segment_map=segment_map,
        base_payload=base_payload,
    )

    assert len(output) == 2
    row_b1 = next(item for item in output if str(item.get("id") or "") == "p1_b1")
    row_b2 = next(item for item in output if str(item.get("id") or "") == "p1_b2")
    assert list(row_b1.get("source_word_ids") or []) == ["w000001", "w000002"]
    assert list(row_b2.get("source_word_ids") or []) == ["w000003", "w000004"]

    anchor_b1 = dict(row_b1.get("source_anchor") or {})
    anchor_b2 = dict(row_b2.get("source_anchor") or {})
    bbox_b1 = dict(anchor_b1.get("bbox_hint") or {})
    bbox_b2 = dict(anchor_b2.get("bbox_hint") or {})
    assert float(bbox_b1.get("bottom") or 0.0) <= float(bbox_b2.get("top") or 9999.0)


def test_apply_ui_ops_to_plan_reorder_update_remove_insert():
    service = LiteratureReaderComposeService()
    ui_plan = {
        "plan_id": "p1",
        "components": [
            {"id": "n1", "type": "SectionHeading", "props": {"text": "Title"}, "children": [], "source_anchor_refs": []},
            {"id": "n2", "type": "ParagraphProse", "props": {"text": "Para A"}, "children": [], "source_anchor_refs": []},
            {"id": "n3", "type": "ParagraphProse", "props": {"text": "Para B"}, "children": [], "source_anchor_refs": []},
        ],
        "layout": {},
        "style_tokens": {},
        "trace_meta": {},
    }
    ui_ops = [
        {"op": "reorder_components", "ordered_component_ids": ["n3", "n1", "n2"]},
        {"op": "update_component_props", "component_id": "n2", "props_patch": {"text": "Para A+"}},
        {"op": "remove_component", "component_id": "n1"},
        {
            "op": "insert_component",
            "after_component_id": "n3",
            "component": {"id": "n4", "type": "ParagraphProse", "props": {"text": "Inserted"}},
        },
    ]

    result = service._apply_ui_ops_to_plan(ui_plan=ui_plan, ui_ops=ui_ops)
    assert not result["errors"]
    next_plan = result["ui_plan"]
    next_ids = [str(item.get("id") or "") for item in list(next_plan.get("components") or [])]
    assert next_ids == ["n3", "n4", "n2"]
    row_n2 = next(item for item in next_plan["components"] if str(item.get("id") or "") == "n2")
    assert str((row_n2.get("props") or {}).get("text") or "") == "Para A+"


def test_reader_component_contract_service_rejects_invalid_ui_ops():
    service = ReaderComponentContractService()
    ops, errors = service.validate_and_sanitize_ui_ops(
        [
            {"op": "reorder_components", "ordered_component_ids": ["n1", "n9"]},
            {"op": "insert_component", "component": {"id": "x1", "type": "NotAllowed", "props": {}}},
            {"op": "update_component_props", "component_id": "n2", "props_patch": "bad"},
        ],
        existing_component_ids=["n1", "n2"],
        valid_block_ids={"b1", "b2"},
    )
    assert ops == []
    assert len(errors) >= 3


def test_reader_component_contract_service_accepts_new_structured_cards():
    service = ReaderComponentContractService()

    ok_cluster, err_cluster = service.validate_component(
        {
            "id": "c1",
            "type": "InsightClusterCard",
            "props": {"title": "Key findings", "items": ["Finding A", "Finding B"], "tone": "finding"},
            "source_block_ids": ["b1"],
        },
        valid_block_ids={"b1", "b2"},
    )
    ok_bridge, err_bridge = service.validate_component(
        {
            "id": "c2",
            "type": "SectionBridgeCard",
            "props": {"title": "Transition", "text": "This page continues the earlier adjudicator setup."},
            "source_block_ids": ["b2"],
        },
        valid_block_ids={"b1", "b2"},
    )

    assert ok_cluster is True
    assert err_cluster is None
    assert ok_bridge is True
    assert err_bridge is None


@pytest.mark.asyncio
async def test_apply_multimodal_layout_assist_should_fail_loud_when_not_docmind_source():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=78, user_id=1, title="Demo", pdf_path="demo.pdf")

    with pytest.raises(RenderPipelineContractError) as exc:
        await service._apply_multimodal_layout_assist(  # pylint: disable=protected-access
            paper=paper,
            page=1,
            base_payload={"page_structure_v3": {"source": "local_parser", "block_groups": []}},
        )

    assert exc.value.code == "DOCMIND_LAYOUT_DIGEST_EMPTY"
    assert exc.value.stage == "docmind"


@pytest.mark.asyncio
async def test_compose_should_skip_vl_parser_when_docmind_structure_present(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=78, user_id=1, title="Demo", pdf_path="demo.pdf")
    captured: dict[str, list[str]] = {}

    async def _should_not_call_parser(**_kwargs):
        raise AssertionError("build_line_parse_advice should be skipped when docmind structure exists")

    async def _stage1(**_kwargs):
        return (
            {
                "blocks": [
                    {
                        "layout_id": "l1",
                        "role": "paragraph",
                        "section_id": "sec_body",
                        "column": 0,
                        "confidence": 0.92,
                    }
                ],
                "sections": [
                    {
                        "section_id": "sec_body",
                        "title_layout_id": "l1",
                        "children": ["l1"],
                    }
                ],
            },
            {"used": True, "model": "qwen3-vl-flash", "fallback_used": False},
        )

    async def _stage2(**kwargs):
        captured["allowed_components"] = list(kwargs.get("allowed_components") or [])
        return (
            {
                "page_layout": [
                    {
                        "component": "ParagraphProse",
                        "source_layout_ids": ["l1"],
                        "props": {},
                    }
                ],
                "unused_layout_ids": [],
            },
            {"used": True, "model": "qwen3.5-plus", "fallback_used": False},
        )

    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(service._mm_layout_service, "build_line_parse_advice", _should_not_call_parser)
    monkeypatch.setattr(service._mm_layout_service, "build_stage1_structural_annotations", _stage1)
    monkeypatch.setattr(service._mm_layout_service, "build_stage2_design_layout", _stage2)

    base_payload = {
        "page_structure_v3": {
            "source": "document_mind",
            "block_groups": [
                {
                    "block_id": "dm_p1_l001_b001",
                    "layout_unique_id": "l1",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "text": "Demo paragraph.",
                    "reading_order": 1,
                }
            ],
            "counts": {"block_count": 1},
        },
        "docmind_structure": {
            "layouts": [
                {
                    "index": 1,
                    "uniqueId": "l1",
                    "type": "text",
                    "subType": "para",
                    "text": "Demo paragraph.",
                    "pos": [{"x": 100, "y": 100}, {"x": 700, "y": 100}, {"x": 700, "y": 130}, {"x": 100, "y": 130}],
                }
            ]
        },
    }

    output = await service._apply_multimodal_layout_assist(  # pylint: disable=protected-access
        paper=paper,
        page=1,
        base_payload=base_payload,
    )

    assert str((output.get("page_structure_v3") or {}).get("source") or "") == "document_mind"
    assert str((output.get("layout_advice_v3") or {}).get("source") or "") == "stage2_design_v1"
    assert bool((output.get("pipeline_contract_meta") or {}).get("used")) is True
    assert bool((output.get("mm_assist_meta") or {}).get("used")) is True
    assert len(list((output.get("stage1_structural_annotations") or {}).get("blocks") or [])) == 1
    assert len(list((output.get("stage2_design_layout") or {}).get("page_layout") or [])) == 1
    assert "CompareInsightsCard" in captured["allowed_components"]
    assert "InsightClusterCard" in captured["allowed_components"]
    assert "SectionBridgeCard" in captured["allowed_components"]


def test_ensure_payload_contract_should_mark_layout_monotony_for_prose_heavy_structured_page():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 78,
        "page": 7,
        "ui_plan": {
            "components": [
                {
                    "id": "h1",
                    "type": "SectionHeading",
                    "props": {"text": "Results", "level": 2},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_h1"],
                    "region": "main",
                    "display": "default",
                },
                {
                    "id": "p1",
                    "type": "ParagraphProse",
                    "props": {"text": "Paragraph one."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_b1"],
                    "region": "main",
                    "display": "default",
                },
                {
                    "id": "p2",
                    "type": "ParagraphProse",
                    "props": {"text": "Paragraph two."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_b2"],
                    "region": "main",
                    "display": "default",
                },
                {
                    "id": "p3",
                    "type": "ParagraphProse",
                    "props": {"text": "Paragraph three."},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p7_b3"],
                    "region": "main",
                    "display": "default",
                },
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "page_structure_v3": {
            "block_groups": [
                {"block_id": "p7_h1", "kind": "heading", "layout_unique_id": "l_h1", "text": "Results"},
                {"block_id": "p7_b1", "kind": "paragraph", "layout_unique_id": "l_b1", "text": "Paragraph one."},
                {"block_id": "p7_b2", "kind": "paragraph", "layout_unique_id": "l_b2", "text": "Paragraph two."},
                {"block_id": "p7_b3", "kind": "paragraph", "layout_unique_id": "l_b3", "text": "Paragraph three."},
                {"block_id": "p7_fig", "kind": "figure_meta", "layout_unique_id": "l_fig", "text": "Fig 3. Comparison plot."},
            ]
        },
        "omission_decisions": [
            {
                "decision_id": "omit_fig",
                "target_block_ids": ["p7_fig"],
                "reason": "Figure metadata routed to AI context.",
            }
        ],
        "quality_report": {"overall": 0.91, "validation_errors": []},
    }

    ensured = service._ensure_payload_contract(page=7, payload=payload)  # pylint: disable=protected-access
    quality = dict(ensured.get("quality_report") or {})

    assert quality["layout_monotony"] is True
    assert quality["flowy_layout_detected"] is True
    assert int(quality["max_consecutive_prose_nodes"]) == 3
    assert "flowy_layout_detected" in list(quality.get("warnings") or [])


def test_ensure_payload_contract_should_build_page_grounding_v1_from_layout_unique_ids(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(
        service,
        "_resolve_grounding_page_image_size",
        lambda **_kwargs: (1483, 1920),
    )
    payload = {
        "paper_id": 85,
        "page": 1,
        "ui_plan": {
            "plan_id": "plan_grounding",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.91, "validation_errors": []},
        "docmind_structure": {
            "page_image_url": "https://example.com/page-1.png",
            "layouts": [
                {
                    "index": 12,
                    "uniqueId": "f2c5fea143e04be47159e880ebe9037b",
                    "type": "title",
                    "subType": "none",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE\n",
                    "alignment": "left",
                    "lineHeight": 9,
                    "pos": [
                        {"x": 484, "y": 1338},
                        {"x": 1367, "y": 1338},
                        {"x": 1367, "y": 1398},
                        {"x": 484, "y": 1398},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 481, "y": 1334},
                                {"x": 1366, "y": 1334},
                                {"x": 1366, "y": 1367},
                                {"x": 481, "y": 1367},
                            ],
                            "styleId": 16,
                            "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                        },
                        {
                            "pos": [
                                {"x": 481, "y": 1370},
                                {"x": 576, "y": 1370},
                                {"x": 576, "y": 1396},
                                {"x": 481, "y": 1396},
                            ],
                            "styleId": 17,
                            "text": " USMLE",
                        },
                    ],
                }
            ],
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p1_dm_p1_l012_b001",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                },
                {
                    "block_id": "p1_dm_p1_l012_b002",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "USMLE",
                },
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=1, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    layout_atoms = list(grounding.get("layout_atoms") or [])
    reading_nodes = list(grounding.get("reading_nodes") or [])
    evidence_map = list(grounding.get("evidence_map") or [])

    assert str(grounding.get("version") or "") == "page_grounding_v1"
    assert len(layout_atoms) == 1
    assert str(layout_atoms[0].get("layout_id") or "") == "f2c5fea143e04be47159e880ebe9037b"
    assert len(list(layout_atoms[0].get("blocks") or [])) == 2
    assert str(layout_atoms[0].get("clean_text") or "") == "ChatGPT yields moderate accuracy approaching passing performance on USMLE"
    assert str(layout_atoms[0].get("alignment") or "") == "left"
    assert float(layout_atoms[0].get("line_height") or 0.0) == 9.0
    assert list(layout_atoms[0].get("canonical_block_ids") or []) == ["p1_dm_p1_l012_b001", "p1_dm_p1_l012_b002"]
    assert str(layout_atoms[0].get("node_kind") or "") == "title"
    assert bool(layout_atoms[0].get("include_in_main_flow")) is True
    assert len(reading_nodes) == 1
    assert list(reading_nodes[0].get("source_layout_ids") or []) == ["f2c5fea143e04be47159e880ebe9037b"]
    assert len(evidence_map) == 1
    assert len(list(evidence_map[0].get("block_positions") or [])) == 2
    assert dict(grounding.get("page_image") or {}) == {
        "url": "",
        "path": "",
        "width": 1483,
        "height": 1920,
        "source": "docmind_page_image_unlocalized",
        "origin_url": "https://example.com/page-1.png",
        "local_cached": False,
    }
    assert int(((layout_atoms[0].get("blocks") or [])[0].get("style_id") or 0)) == 16


def test_ensure_payload_contract_should_preserve_grounding_text_normalization_enrichments():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 85,
        "page": 1,
        "ui_plan": {
            "plan_id": "plan_grounding_preserve_normalize",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.91, "validation_errors": []},
        "docmind_structure": {
            "page_image_url": "https://example.com/page-1.png",
            "layouts": [
                {
                    "index": 12,
                    "uniqueId": "f2c5fea143e04be47159e880ebe9037b",
                    "type": "title",
                    "subType": "none",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE\n",
                    "alignment": "left",
                    "lineHeight": 9,
                    "pos": [
                        {"x": 484, "y": 1338},
                        {"x": 1367, "y": 1338},
                        {"x": 1367, "y": 1398},
                        {"x": 484, "y": 1398},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 481, "y": 1334},
                                {"x": 1366, "y": 1334},
                                {"x": 1366, "y": 1367},
                                {"x": 481, "y": 1367},
                            ],
                            "styleId": 16,
                            "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                        },
                        {
                            "pos": [
                                {"x": 481, "y": 1370},
                                {"x": 576, "y": 1370},
                                {"x": 576, "y": 1396},
                                {"x": 481, "y": 1396},
                            ],
                            "styleId": 17,
                            "text": " USMLE",
                        },
                    ],
                }
            ],
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p1_dm_p1_l012_b001",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                },
                {
                    "block_id": "p1_dm_p1_l012_b002",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "USMLE",
                },
            ]
        },
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "layout_atoms": [
                {
                    "layout_id": "f2c5fea143e04be47159e880ebe9037b",
                    "clean_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE",
                    "normalized_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1",
                    "normalization_reason": "restore superscript marker",
                    "normalization_mode": "ocr_cleanup",
                    "normalization_confidence": 0.96,
                }
            ],
            "reading_nodes": [
                {
                    "node_id": "layout:f2c5fea143e04be47159e880ebe9037b",
                    "source_layout_ids": ["f2c5fea143e04be47159e880ebe9037b"],
                    "normalized_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1",
                    "normalization_reason": "restore superscript marker",
                    "normalization_mode": "ocr_cleanup",
                    "normalization_confidence": 0.96,
                }
            ],
            "meta": {
                "normalization_summary": {
                    "item_count": 1,
                    "items": [
                        {
                            "layout_id": "f2c5fea143e04be47159e880ebe9037b",
                            "source_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE",
                            "normalized_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1",
                            "reason": "restore superscript marker",
                            "mode": "ocr_cleanup",
                            "confidence": 0.96,
                        }
                    ],
                }
            },
        },
    }

    ensured = service._ensure_payload_contract(page=1, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    layout_atoms = list(grounding.get("layout_atoms") or [])
    reading_nodes = list(grounding.get("reading_nodes") or [])
    normalization_summary = dict((grounding.get("meta") or {}).get("normalization_summary") or {})

    assert str(layout_atoms[0].get("normalized_text") or "") == "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1"
    assert str(layout_atoms[0].get("normalization_reason") or "") == "restore superscript marker"
    assert str(reading_nodes[0].get("normalized_text") or "") == "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1"
    assert int(normalization_summary.get("item_count") or 0) == 1


def test_ensure_payload_contract_should_backfill_grounding_text_normalizations_from_layout_advice():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 85,
        "page": 1,
        "ui_plan": {
            "plan_id": "plan_grounding_backfill_normalize",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.91, "validation_errors": []},
        "layout_advice_v3": {
            "text_normalizations": {
                "normalization_plan": {
                    "items": [
                        {
                            "layout_id": "f2c5fea143e04be47159e880ebe9037b",
                            "source_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE",
                            "normalized_text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1",
                            "reason": "restore superscript marker",
                            "mode": "ocr_cleanup",
                            "confidence": 0.96,
                            "changed": True,
                        }
                    ]
                }
            }
        },
        "docmind_structure": {
            "page_image_url": "https://example.com/page-1.png",
            "layouts": [
                {
                    "index": 12,
                    "uniqueId": "f2c5fea143e04be47159e880ebe9037b",
                    "type": "title",
                    "subType": "none",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE\n",
                    "alignment": "left",
                    "lineHeight": 9,
                    "pos": [
                        {"x": 484, "y": 1338},
                        {"x": 1367, "y": 1338},
                        {"x": 1367, "y": 1398},
                        {"x": 484, "y": 1398},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 481, "y": 1334},
                                {"x": 1366, "y": 1334},
                                {"x": 1366, "y": 1367},
                                {"x": 481, "y": 1367},
                            ],
                            "styleId": 16,
                            "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                        },
                        {
                            "pos": [
                                {"x": 481, "y": 1370},
                                {"x": 576, "y": 1370},
                                {"x": 576, "y": 1396},
                                {"x": 481, "y": 1396},
                            ],
                            "styleId": 17,
                            "text": " USMLE",
                        },
                    ],
                }
            ],
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p1_dm_p1_l012_b001",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                },
                {
                    "block_id": "p1_dm_p1_l012_b002",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "USMLE",
                },
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=1, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    layout_atoms = list(grounding.get("layout_atoms") or [])
    normalization_summary = dict((grounding.get("meta") or {}).get("normalization_summary") or {})

    assert str(layout_atoms[0].get("normalized_text") or "") == "ChatGPT yields moderate accuracy approaching passing performance on USMLE^1"
    assert str(layout_atoms[0].get("normalization_reason") or "") == "restore superscript marker"
    assert int(normalization_summary.get("item_count") or 0) == 1


def test_ensure_payload_contract_should_resolve_page_grounding_image_dimensions_when_missing(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(
        service,
        "_resolve_grounding_page_image_size",
        lambda **_kwargs: (1483, 1920),
    )
    payload = {
        "paper_id": 85,
        "page": 3,
        "ui_plan": {
            "plan_id": "plan_grounding_image_dims",
            "components": [
                {
                    "id": "p3_g1",
                    "type": "ParagraphProse",
                    "props": {"text": "Knowledge distillation paragraph."},
                    "children": [],
                    "source_block_ids": ["p3_dm_p3_l001_b001"],
                    "source_anchor_refs": [],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "docmind_structure": {
            "page_image_url": "https://example.com/page-3.png",
            "layouts": [
                {
                    "index": 1,
                    "uniqueId": "layout_para_1",
                    "type": "text",
                    "subType": "para",
                    "text": "Knowledge distillation paragraph.\n",
                    "pos": [
                        {"x": 256, "y": 254},
                        {"x": 1220, "y": 254},
                        {"x": 1220, "y": 286},
                        {"x": 256, "y": 286},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 256, "y": 254},
                                {"x": 1220, "y": 254},
                                {"x": 1220, "y": 286},
                                {"x": 256, "y": 286},
                            ],
                            "text": "Knowledge distillation paragraph.",
                        }
                    ],
                }
            ],
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p3_dm_p3_l001_b001",
                    "layout_unique_id": "layout_para_1",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "text": "Knowledge distillation paragraph.",
                }
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=3, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    assert dict(grounding.get("page_image") or {}) == {
        "url": "",
        "path": "",
        "width": 1483,
        "height": 1920,
        "source": "docmind_page_image_unlocalized",
        "origin_url": "https://example.com/page-3.png",
        "local_cached": False,
    }

    anchor = service._build_layout_uid_anchor_from_grounding(  # pylint: disable=protected-access
        page=3,
        payload=ensured,
        layout_id="layout_para_1",
        quote_text="Knowledge distillation paragraph.",
        canonical_block_ids=["p3_dm_p3_l001_b001"],
    )
    assert isinstance(anchor, dict)
    assert int((((anchor or {}).get("geometry") or {}).get("page_width") or 0)) == 1483
    assert int((((anchor or {}).get("geometry") or {}).get("page_height") or 0)) == 1920
    assert int((((anchor or {}).get("bbox_hint") or {}).get("page_width") or 0)) == 1483
    assert int((((anchor or {}).get("bbox_hint") or {}).get("page_height") or 0)) == 1920


def test_ensure_payload_contract_should_keep_doi_layout_outside_main_flow_in_page_grounding():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 78,
        "page": 1,
        "ui_plan": {
            "plan_id": "plan_grounding_doi",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "docmind_structure": {
            "layouts": [
                {
                    "index": 7,
                    "uniqueId": "0f87aa876d6a32d6bbc3a990d753f5d8",
                    "type": "text",
                    "subType": "para",
                    "text": "https://doi.org/10.1371/journal.pdig.0000198.g003\n",
                    "pos": [
                        {"x": 110, "y": 1275},
                        {"x": 477, "y": 1275},
                        {"x": 477, "y": 1296},
                        {"x": 110, "y": 1296},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 108, "y": 1274},
                                {"x": 479, "y": 1274},
                                {"x": 479, "y": 1294},
                                {"x": 108, "y": 1294},
                            ],
                            "styleId": 8,
                            "text": "https://doi.org/10.1371/journal.pdig.0000198.g003",
                        }
                    ],
                }
            ]
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p1_dm_p1_l007_b001",
                    "layout_unique_id": "0f87aa876d6a32d6bbc3a990d753f5d8",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "text": "https://doi.org/10.1371/journal.pdig.0000198.g003",
                }
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=1, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    reading_nodes = list(grounding.get("reading_nodes") or [])

    assert len(reading_nodes) == 1
    assert str(reading_nodes[0].get("node_kind") or "") == "doi"
    assert bool(reading_nodes[0].get("include_in_main_flow")) is False
    assert str(reading_nodes[0].get("region_hint") or "") == "side_context"


def test_build_no_drop_fallback_node_should_preserve_layout_uid_evidence():
    service = LiteratureReaderComposeService()
    payload = {
        "blocks": [
            {
                "id": "p7_dm_p7_l004_b001",
                "text": "1. llama.cpp6 for 4-bit(Q4K_M), 3-bit(Q3_K_M)",
                "source_anchor": None,
            }
        ],
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_l004_b001",
                    "layout_unique_id": "layout_list_1",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "text": "1. llama.cpp6 for 4-bit(Q4K_M), 3-bit(Q3_K_M)",
                }
            ]
        },
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "layout_atoms": [
                {
                    "layout_id": "layout_list_1",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M), 3-bit(Q3_K_M)",
                    "canonical_block_ids": ["p7_dm_p7_l004_b001"],
                    "layout_pos": [
                        {"x": 120, "y": 200},
                        {"x": 640, "y": 200},
                        {"x": 640, "y": 248},
                        {"x": 120, "y": 248},
                    ],
                    "blocks": [
                        {
                            "block_index": 1,
                            "text": "1. llama.cpp6 for 4-bit(Q4K_M), 3-bit(Q3_K_M)",
                            "pos": [
                                {"x": 122, "y": 202},
                                {"x": 638, "y": 202},
                                {"x": 638, "y": 246},
                                {"x": 122, "y": 246},
                            ],
                        }
                    ],
                }
            ],
            "evidence_map": [
                {
                    "source_layout_id": "layout_list_1",
                    "source_block_ids": ["p7_dm_p7_l004_b001"],
                    "layout_pos": [
                        {"x": 120, "y": 200},
                        {"x": 640, "y": 200},
                        {"x": 640, "y": 248},
                        {"x": 120, "y": 248},
                    ],
                    "block_positions": [[
                        {"x": 122, "y": 202},
                        {"x": 638, "y": 202},
                        {"x": 638, "y": 246},
                        {"x": 122, "y": 246},
                    ]],
                }
            ],
            "page_image": {
                "width": 800,
                "height": 1200,
            },
        },
    }

    node = service._build_no_drop_fallback_node(  # pylint: disable=protected-access
        page=7,
        payload=payload,
        canonical_block_id="p7_dm_p7_l004_b001",
        seq=1,
        existing_node_ids=set(),
    )

    assert list(node.get("source_atom_ids") or []) == ["layout_list_1"]
    assert list(node.get("source_layout_ids") or []) == ["layout_list_1"]
    anchors = list(node.get("source_anchor_refs") or [])
    assert len(anchors) == 1
    assert str(anchors[0].get("coord_version") or "") == "layout_uid_v1"
    assert str(anchors[0].get("source_layout_id") or "") == "layout_list_1"
    assert "jump_anchor" in list(node.get("capabilities") or [])


def test_ensure_payload_contract_should_preserve_docmind_table_cells_in_page_grounding():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 85,
        "page": 7,
        "ui_plan": {
            "plan_id": "plan_grounding_table_cells",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "docmind_structure": {
            "layouts": [
                {
                    "index": 2,
                    "uniqueId": "table_layout_1",
                    "type": "table",
                    "subType": "none",
                    "text": "| Model | Score |\n| Q8_0 | 71.68 |\n| Q4KM | 71.24 |\n",
                    "pos": [
                        {"x": 248, "y": 583},
                        {"x": 1239, "y": 583},
                        {"x": 1239, "y": 1302},
                        {"x": 248, "y": 1302},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 248, "y": 583},
                                {"x": 1239, "y": 583},
                                {"x": 1239, "y": 1302},
                                {"x": 248, "y": 1302},
                            ],
                            "styleId": 0,
                            "text": "| Model | Score |\n| Q8_0 | 71.68 |\n| Q4KM | 71.24 |",
                        }
                    ],
                    "cells": [
                        {
                            "cellId": 0,
                            "xsc": 0,
                            "xec": 0,
                            "ysc": 0,
                            "yec": 0,
                            "pos": [[100, 100, 220, 100, 220, 124, 100, 124]],
                            "layouts": [
                                {
                                    "uniqueId": "table_layout_1_r0c0",
                                    "text": "Model\n",
                                    "pos": [
                                        {"x": 100, "y": 100},
                                        {"x": 220, "y": 100},
                                        {"x": 220, "y": 124},
                                        {"x": 100, "y": 124},
                                    ],
                                    "blocks": [
                                        {
                                            "pos": [
                                                {"x": 100, "y": 100},
                                                {"x": 220, "y": 100},
                                                {"x": 220, "y": 124},
                                                {"x": 100, "y": 124},
                                            ],
                                            "text": "Model",
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "cellId": 1,
                            "xsc": 1,
                            "xec": 1,
                            "ysc": 0,
                            "yec": 0,
                            "pos": [[280, 100, 390, 100, 390, 124, 280, 124]],
                            "layouts": [
                                {
                                    "uniqueId": "table_layout_1_r0c1",
                                    "text": "Score\n",
                                    "pos": [
                                        {"x": 280, "y": 100},
                                        {"x": 390, "y": 100},
                                        {"x": 390, "y": 124},
                                        {"x": 280, "y": 124},
                                    ],
                                    "blocks": [
                                        {
                                            "pos": [
                                                {"x": 280, "y": 100},
                                                {"x": 390, "y": 100},
                                                {"x": 390, "y": 124},
                                                {"x": 280, "y": 124},
                                            ],
                                            "text": "Score",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_l003_b001",
                    "layout_unique_id": "table_layout_1",
                    "kind": "table",
                    "zone_type": "main_body",
                    "text": "| Model | Score |",
                }
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=7, payload=payload)  # pylint: disable=protected-access
    grounding = dict(ensured.get("page_grounding_v1") or {})
    layout_atoms = list(grounding.get("layout_atoms") or [])
    evidence_map = list(grounding.get("evidence_map") or [])

    assert len(layout_atoms) == 1
    table_atom = dict(layout_atoms[0] or {})
    assert str(table_atom.get("node_kind") or "") == "table"
    table_cells = list(table_atom.get("table_cells") or [])
    assert len(table_cells) == 2
    assert str(table_cells[0].get("text") or "") == "Model"
    assert list(table_cells[0].get("layout_ids") or []) == ["table_layout_1_r0c0"]
    assert len(list((evidence_map[0] or {}).get("table_cells") or [])) == 2


def test_reader_compose_payload_schema_should_keep_grounding_table_cells():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 85,
        "page": 7,
        "ui_plan": {
            "plan_id": "plan_grounding_table_cells_schema",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "docmind_structure": {
            "layouts": [
                {
                    "index": 2,
                    "uniqueId": "table_layout_schema_1",
                    "type": "table",
                    "subType": "none",
                    "text": "| Model | Score |",
                    "pos": [
                        {"x": 248, "y": 583},
                        {"x": 1239, "y": 583},
                        {"x": 1239, "y": 1302},
                        {"x": 248, "y": 1302},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 248, "y": 583},
                                {"x": 1239, "y": 583},
                                {"x": 1239, "y": 1302},
                                {"x": 248, "y": 1302},
                            ],
                            "styleId": 0,
                            "text": "| Model | Score |",
                        }
                    ],
                    "cells": [
                        {
                            "cellId": 0,
                            "xsc": 0,
                            "xec": 0,
                            "ysc": 0,
                            "yec": 0,
                            "pos": [[100, 100, 220, 100, 220, 124, 100, 124]],
                            "layouts": [
                                {
                                    "uniqueId": "table_layout_schema_1_r0c0",
                                    "text": "Model\n",
                                    "pos": [
                                        {"x": 100, "y": 100},
                                        {"x": 220, "y": 100},
                                        {"x": 220, "y": 124},
                                        {"x": 100, "y": 124},
                                    ],
                                    "blocks": [
                                        {
                                            "pos": [
                                                {"x": 100, "y": 100},
                                                {"x": 220, "y": 100},
                                                {"x": 220, "y": 124},
                                                {"x": 100, "y": 124},
                                            ],
                                            "text": "Model",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p7_dm_p7_l003_b001",
                    "layout_unique_id": "table_layout_schema_1",
                    "kind": "table",
                    "zone_type": "main_body",
                    "text": "| Model | Score |",
                }
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=7, payload=payload)  # pylint: disable=protected-access
    serialized = ReaderComposePayload.model_validate(ensured).model_dump(mode="python")
    grounding = dict(serialized.get("page_grounding_v1") or {})
    table_atom = dict((grounding.get("layout_atoms") or [])[0] or {})
    evidence_row = dict((grounding.get("evidence_map") or [])[0] or {})

    assert len(list(table_atom.get("table_cells") or [])) == 1
    assert len(list(evidence_row.get("table_cells") or [])) == 1


def test_pipeline_version_should_default_to_layout_uid_v1_when_unset(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_version", "")

    assert service._pipeline_version() == "layout_uid_v1"  # pylint: disable=protected-access


def test_build_page_grounding_v1_should_localize_remote_page_image_when_path_missing(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path), raising=False)

    class _FakeHeaders:
        @staticmethod
        def get_content_type():
            return "image/png"

    class _FakeResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def info(self):
            return _FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    image_bytes = BytesIO()
    from PIL import Image
    Image.new("RGB", (32, 48), color="white").save(image_bytes, format="PNG")

    monkeypatch.setattr(compose_module, "urlopen", lambda *_args, **_kwargs: _FakeResponse(image_bytes.getvalue()))

    grounding = service._build_page_grounding_v1(  # pylint: disable=protected-access
        page=7,
        payload={
            "paper_id": 85,
            "docmind_structure": {
                "layouts": [],
                "page_image_url": "https://example.com/docmind/page7.png",
                "page_image_path": "",
            },
            "page_structure_v3": {"block_groups": []},
        },
    )

    page_image = dict(grounding.get("page_image") or {})
    localized_path = str(page_image.get("path") or "")
    assert localized_path
    assert os.path.exists(localized_path)
    assert localized_path.endswith(".png")
    assert str(page_image.get("source") or "") == "docmind_page_image_localized"
    assert str(page_image.get("origin_url") or "") == "https://example.com/docmind/page7.png"
    assert bool(page_image.get("local_cached")) is True
    assert str(page_image.get("url") or "").endswith("/api/v1/literature/reader/grounding-page-assets/85/7")


def test_build_page_grounding_v1_should_prefer_localized_docmind_image_over_render_asset(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path), raising=False)

    render_dir = tmp_path / "85"
    render_dir.mkdir(parents=True, exist_ok=True)
    render_path = render_dir / "page_7_r220_q92_v2.jpg"
    render_path.write_bytes(b"jpg-bytes")
    grounding_dir = tmp_path / "paper_85" / "grounding_pages"
    grounding_dir.mkdir(parents=True, exist_ok=True)
    grounding_path = grounding_dir / "page_7.png"
    grounding_path.write_bytes(b"png-bytes")

    def _unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("should not fetch remote page image when render asset exists")

    monkeypatch.setattr(compose_module, "urlopen", _unexpected_urlopen)

    grounding = service._build_page_grounding_v1(  # pylint: disable=protected-access
        page=7,
        payload={
            "paper_id": 85,
            "docmind_structure": {
                "layouts": [],
                "page_image_url": "https://example.com/docmind/page7.png",
                "page_image_path": "",
                "page_image_width": 1483,
                "page_image_height": 1920,
            },
            "page_structure_v3": {"block_groups": []},
        },
    )

    page_image = dict(grounding.get("page_image") or {})
    assert str(page_image.get("source") or "") == "docmind_page_image_localized"
    assert str(page_image.get("path") or "") == str(grounding_path)
    assert str(page_image.get("url") or "").endswith("/api/v1/literature/reader/grounding-page-assets/85/7")
    assert str(page_image.get("origin_url") or "") == "https://example.com/docmind/page7.png"
    assert bool(page_image.get("local_cached")) is True
    assert int(page_image.get("width") or 0) == 1483
    assert int(page_image.get("height") or 0) == 1920


def test_build_page_grounding_v1_should_not_fallback_to_render_asset_when_docmind_url_is_stale(
    monkeypatch, tmp_path
):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path), raising=False)

    render_dir = tmp_path / "85"
    render_dir.mkdir(parents=True, exist_ok=True)
    render_path = render_dir / "page_8.jpg"
    from PIL import Image

    Image.new("RGB", (1483, 1920), color="white").save(render_path, format="JPEG")

    def _unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("should not touch stale docmind page image url when render asset exists")

    monkeypatch.setattr(compose_module, "urlopen", _unexpected_urlopen)

    grounding = service._build_page_grounding_v1(  # pylint: disable=protected-access
        page=8,
        payload={
            "paper_id": 85,
            "docmind_structure": {
                "layouts": [],
                "page_image_url": "https://example.com/stale/docmind/page8.png?expires=123",
                "page_image_path": "",
                "page_image_width": 0,
                "page_image_height": 0,
            },
            "page_structure_v3": {"block_groups": []},
        },
    )

    page_image = dict(grounding.get("page_image") or {})
    assert str(page_image.get("source") or "") == "docmind_page_image_unlocalized"
    assert str(page_image.get("url") or "") == ""
    assert str(page_image.get("path") or "") == ""
    assert str(page_image.get("origin_url") or "") == "https://example.com/stale/docmind/page8.png?expires=123"
    assert bool(page_image.get("local_cached")) is False
    assert page_image.get("width") in (None, 0)
    assert page_image.get("height") in (None, 0)


def test_ensure_payload_contract_should_refresh_layout_uid_anchors_from_current_grounding():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 85,
        "page": 8,
        "engine_version": "reader_compose_v15",
        "source_signature": "sig",
        "build_mode": "compose_agent_layout_uid_v1",
        "ui_plan": {
            "plan_id": "plan_anchor_refresh",
            "components": [
                {
                    "id": "g3",
                    "type": "ListBlock",
                    "props": {"items": ["1. llama.cpp^6 for 4-bit (Q4_K_M)"]},
                    "source_atom_ids": ["layout_list_1"],
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "source_anchor_refs": [
                        {
                            "anchor_id": "layout_uid_v1:layout_list_1",
                            "coord_version": "layout_uid_v1",
                            "source_layout_id": "layout_list_1",
                            "quote_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                            "geometry": {"polygons": [], "page_width": 1232, "page_height": 1843},
                            "bbox_hint": {"x0": 319, "x1": 1232, "top": 386, "bottom": 440, "page_width": 1232, "page_height": 1843},
                        }
                    ],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "page": 8,
            "layout_atoms": [
                {
                    "layout_id": "layout_list_1",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "node_kind": "list",
                    "reading_order": 1,
                    "raw_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "normalized_text": "1. llama.cpp^6 for 4-bit (Q4_K_M)",
                    "canonical_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ],
                    "blocks": [
                        {
                            "block_index": 1,
                            "text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                            "pos": [
                                {"x": 319, "y": 386},
                                {"x": 1232, "y": 386},
                                {"x": 1232, "y": 440},
                                {"x": 319, "y": 440},
                            ],
                        }
                    ],
                }
            ],
            "evidence_map": [
                {
                    "source_layout_id": "layout_list_1",
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ],
                    "block_positions": [[
                        {"x": 319, "y": 386},
                        {"x": 1232, "y": 386},
                        {"x": 1232, "y": 440},
                        {"x": 319, "y": 440},
                    ]],
                }
            ],
            "page_image": {
                "url": "https://example.com/docmind/page_8.png",
                "path": "",
                "width": 1360,
                "height": 1760,
                "source": "docmind_page_image_remote",
            },
        },
        "docmind_structure": {"layouts": []},
        "page_structure_v3": {"block_groups": []},
    }

    ensured = service._ensure_payload_contract(page=8, payload=payload)  # pylint: disable=protected-access
    components = list((((ensured.get("ui_plan") or {}).get("components") or [])))
    refs = list((components[0] or {}).get("source_anchor_refs") or [])
    assert len(refs) == 1
    ref = dict(refs[0] or {})
    assert str(ref.get("quote_text") or "") == "1. llama.cpp^6 for 4-bit (Q4_K_M)"
    repaired_geometry = ref.get("geometry") or {}
    assert int(repaired_geometry.get("page_width") or 0) >= 1360
    assert int(repaired_geometry.get("page_height") or 0) >= 1760
    repaired_bbox = ref.get("bbox_hint") or {}
    assert int(repaired_bbox.get("page_width") or 0) >= 1360
    assert int(repaired_bbox.get("page_height") or 0) >= 1760
    page_image = dict(((ensured.get("page_grounding_v1") or {}).get("page_image") or {}))
    page_image_path = str(page_image.get("path") or "")
    if page_image_path:
        assert page_image_path.endswith("/paper_85/grounding_pages/page_8.png")
    assert int(page_image.get("width") or 0) >= 1360
    assert int(page_image.get("height") or 0) >= 1760


@pytest.mark.asyncio
async def test_invoke_single_agent_model_should_localize_remote_prompt_image_for_dashscope(monkeypatch, tmp_path):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module, "PAGE_RENDER_ASSET_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(compose_module.settings, "reader_agent_provider", "aliyun")
    monkeypatch.setattr(compose_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(compose_module.settings, "aliyun_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setattr(compose_module.settings, "reader_agent_model", "qwen-3.5-plus")

    class _FakeHeaders:
        @staticmethod
        def get_content_type():
            return "image/png"

    class _FakeResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def info(self):
            return _FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    image_bytes = BytesIO()
    from PIL import Image
    Image.new("RGB", (48, 32), color="white").save(image_bytes, format="PNG")

    monkeypatch.setattr(compose_module, "urlopen", lambda *_args, **_kwargs: _FakeResponse(image_bytes.getvalue()))
    captured = {"image_paths": []}

    async def _fake_chat_json(**kwargs):
        captured["image_paths"] = list(kwargs.get("image_paths") or [])
        return {
            "parsed": {
                "status": "done",
                "step_result": {"ok": True},
                "self_check": {},
                "fixes_applied": [],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }

    monkeypatch.setattr(DashScopeMultimodalService, "is_available", staticmethod(lambda: True))
    monkeypatch.setattr(DashScopeMultimodalService, "chat_json", _fake_chat_json)

    result = await service._invoke_single_agent_model(  # pylint: disable=protected-access
        system_prompt="Group table rows.",
        user_prompt={"rows": []},
        rendered_page_image="https://example.com/docmind/page7.png",
        rendered_page_image_path="",
        step=2,
        phase="layout_uid_table_logical_rows:table_1",
    )

    assert result["status"] == "done"
    assert captured["image_paths"]
    localized_uri = str(captured["image_paths"][0])
    assert localized_uri.startswith("file://")
    localized_path = localized_uri.removeprefix("file://")
    assert os.path.exists(localized_path)
    assert localized_path.endswith(".png")


def test_is_public_prompt_image_url_should_reject_localhost_reader_assets():
    service = LiteratureReaderComposeService()

    assert service._is_public_prompt_image_url(  # pylint: disable=protected-access
        "http://localhost:8888/api/v1/literature/reader/page-assets/85/8"
    ) is False
    assert service._is_public_prompt_image_url(  # pylint: disable=protected-access
        "https://example.com/static/page8.png"
    ) is True


@pytest.mark.asyncio
async def test_invoke_single_agent_model_should_skip_localhost_image_url_in_compatible_fallback(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(compose_module.settings, "reader_agent_provider", "aliyun")
    monkeypatch.setattr(compose_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(compose_module.settings, "aliyun_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setattr(compose_module.settings, "reader_agent_model", "qwen-3.5-plus")
    monkeypatch.setattr(DashScopeMultimodalService, "is_available", staticmethod(lambda: False))

    captured = {"messages": []}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = list(kwargs.get("messages") or [])

            class _Usage:
                prompt_tokens = 10
                completion_tokens = 4
                total_tokens = 14

            class _Message:
                content = '{"status":"done","step_result":{"items":[]},"self_check":{},"fixes_applied":[]}'

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]
                usage = _Usage()

            return _Response()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.chat = _FakeChat()

    monkeypatch.setattr(compose_module, "AsyncOpenAI", _FakeClient)

    result = await service._invoke_single_agent_model(  # pylint: disable=protected-access
        system_prompt="Normalize display text.",
        user_prompt={"layout_atoms": []},
        rendered_page_image="http://localhost:8888/api/v1/literature/reader/page-assets/85/8",
        rendered_page_image_path="",
        step=1,
        phase="layout_uid_text_normalization",
    )

    assert result["status"] == "done"
    assert len(captured["messages"]) == 2
    user_message = dict(captured["messages"][1] or {})
    user_content = list(user_message.get("content") or [])
    assert len(user_content) == 1
    assert str((user_content[0] or {}).get("type") or "") == "text"


@pytest.mark.asyncio
async def test_build_layout_uid_pipeline_result_should_not_overwrite_grounding_page_image_with_prompt_asset(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=85, user_id=1, title="demo", pdf_path="")
    base_payload = {
        "paper_id": 85,
        "page": 8,
        "docmind_structure": {
            "page_image_url": "https://example.com/docmind/page8.png",
            "page_image_path": "",
        },
        "page_structure_v3": {"block_groups": []},
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "page": 8,
            "layout_atoms": [
                {
                    "layout_id": "layout_list_1",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "node_kind": "list",
                    "reading_order": 1,
                    "raw_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "normalized_text": "",
                    "canonical_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 348, "y": 406},
                        {"x": 1343, "y": 406},
                        {"x": 1343, "y": 464},
                        {"x": 348, "y": 464},
                    ],
                    "blocks": [
                        {
                            "block_index": 1,
                            "text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                            "pos": [
                                {"x": 348, "y": 406},
                                {"x": 1343, "y": 406},
                                {"x": 1343, "y": 464},
                                {"x": 348, "y": 464},
                            ],
                        }
                    ],
                }
            ],
            "reading_nodes": [
                {
                    "node_id": "layout:layout_list_1",
                    "node_kind": "list",
                    "raw_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "normalized_text": "",
                    "source_layout_ids": ["layout_list_1"],
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "include_in_main_flow": True,
                    "region_hint": "main_body",
                    "meta": {},
                }
            ],
            "evidence_map": [
                {
                    "evidence_id": "layout:layout_list_1",
                    "source_layout_id": "layout_list_1",
                    "source_block_ids": ["p8_dm_p8_l004_b001"],
                    "layout_pos": [
                        {"x": 348, "y": 406},
                        {"x": 1343, "y": 406},
                        {"x": 1343, "y": 464},
                        {"x": 348, "y": 464},
                    ],
                    "block_positions": [[
                        {"x": 348, "y": 406},
                        {"x": 1343, "y": 406},
                        {"x": 1343, "y": 464},
                        {"x": 348, "y": 464},
                    ]],
                }
            ],
            "page_image": {
                "url": "http://localhost:8888/api/v1/literature/reader/grounding-page-assets/85/8",
                "path": "/app/uploads/reader_page_assets/paper_85/grounding_pages/page_8.png",
                "width": 1483,
                "height": 1920,
                "source": "docmind_page_image_localized",
                "origin_url": "https://example.com/docmind/page8.png",
                "local_cached": True,
            },
        },
        "assets": [],
    }

    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(
        service,
        "_resolve_reader_page_image_asset",
        lambda **_kwargs: {
            "url": "http://localhost:8888/api/v1/literature/reader/page-assets/85/8",
            "path": "/app/uploads/reader_page_assets/85/page_8.jpg",
            "source": "page_render_asset",
            "origin_url": "",
            "local_cached": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_build_layout_uid_combined_prompt_payload",
        lambda **_kwargs: {"layout_atoms": [{"layout_id": "layout_list_1"}]},
    )
    call_counter = {"count": 0}

    async def _fake_invoke_single_agent_model(**kwargs):
        call_counter["count"] += 1
        phase = str(kwargs.get("phase") or "")
        assert phase == "layout_uid_combined_plan"
        return {
            "status": "done",
            "step_result": {
                "text_items": [],
                "groups": [
                    {
                        "group_id": "group_1",
                        "group_kind": "list",
                        "layout_ids": ["layout_list_1"],
                    }
                ],
                "omissions": [],
                "notes": [],
            },
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }

    monkeypatch.setattr(service, "_invoke_single_agent_model", _fake_invoke_single_agent_model)
    monkeypatch.setattr(
        service,
        "_normalize_layout_uid_combined_plan",
        lambda **_kwargs: {
            "normalization_plan": {"items": []},
            "text_validation": {"fallback_used": False, "errors": []},
            "grouping_plan": {"groups": [{"group_id": "group_1", "group_kind": "list", "layout_ids": ["layout_list_1"]}], "omissions": [], "notes": []},
            "grouping_validation": {"fallback_used": False, "errors": []},
        },
    )
    monkeypatch.setattr(
        service,
        "_apply_layout_uid_text_normalization_to_grounding",
        lambda **kwargs: dict(kwargs.get("grounding") or {}),
    )

    async def _empty_map(**_kwargs):
        return {}

    monkeypatch.setattr(service, "_build_layout_uid_table_refinement_map", _empty_map)
    monkeypatch.setattr(service, "_build_layout_uid_equation_refinement_map", _empty_map)
    monkeypatch.setattr(
        service,
        "_layout_uid_group_plan_to_panel_plan",
        lambda **_kwargs: {
            "plan_id": "layout_uid_v1_p8",
            "panels": [
                {
                    "panel_id": "layout_uid_main",
                    "nodes": [
                        {
                            "node_id": "group_1",
                            "component": "ListBlock",
                            "source_layout_ids": ["layout_list_1"],
                            "props": {"items": ["1. llama.cpp^6 for 4-bit (Q4_K_M)"]},
                            "children": [],
                        }
                    ],
                }
            ],
            "style_plan": {},
        },
    )
    monkeypatch.setattr(service, "_collect_docmind_blocks_for_single_agent", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(
        service,
        "_panel_plan_to_ui_plan",
        lambda **_kwargs: {
            "plan_id": "layout_uid_v1_p8",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )

    result = await service._build_layout_uid_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=paper,
        page=8,
        base_payload=base_payload,
        style_intent=None,
        theme_mode=None,
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=1000,
        selected_kb_id=84,
        pipeline_version="layout_uid_v1",
    )

    page_image = dict((((result.get("base_payload") or {}).get("page_grounding_v1") or {}).get("page_image") or {}))
    assert int(call_counter["count"]) == 1
    assert str(page_image.get("source") or "") == "docmind_page_image_localized"
    assert str(page_image.get("path") or "") == "/app/uploads/reader_page_assets/paper_85/grounding_pages/page_8.png"
    assert int(page_image.get("width") or 0) == 1483
    assert int(page_image.get("height") or 0) == 1920


def test_panel_plan_to_ui_plan_should_emit_layout_geometry_anchor_and_source_atom_ids():
    service = LiteratureReaderComposeService()
    ui_plan = service._panel_plan_to_ui_plan(  # pylint: disable=protected-access
        page=7,
        panel_plan={
            "plan_id": "panel_plan_layout_anchor",
            "panels": [
                {
                    "panel_id": "main",
                    "nodes": [
                        {
                            "node_id": "title_1",
                            "component": "SectionHeading",
                            "source_layout_ids": ["layout_title_1"],
                            "props": {"text": "Quantization Performance Drop", "level": 1},
                            "children": [],
                        }
                    ],
                }
            ],
            "style_plan": {},
        },
        docmind_blocks=[
            {
                "layout_id": "layout_title_1",
                "source_text": "Quantization Performance Drop",
                "type": "title",
            }
        ],
        layout_to_block_ids={"layout_title_1": ["p7_dm_title_1"]},
        base_payload={
            "assets": [],
            "blocks": [],
            "page_grounding_v1": {
                "layout_atoms": [
                    {
                        "layout_id": "layout_title_1",
                        "clean_text": "Quantization Performance Drop",
                        "raw_text": "Quantization Performance Drop",
                        "canonical_block_ids": ["p7_dm_title_1"],
                        "layout_pos": [
                            {"x": 120, "y": 120},
                            {"x": 840, "y": 120},
                            {"x": 840, "y": 188},
                            {"x": 120, "y": 188},
                        ],
                        "blocks": [
                            {
                                "block_index": 1,
                                "text": "Quantization Performance",
                                "pos": [
                                    {"x": 120, "y": 120},
                                    {"x": 620, "y": 120},
                                    {"x": 620, "y": 154},
                                    {"x": 120, "y": 154},
                                ],
                            },
                            {
                                "block_index": 2,
                                "text": "Drop",
                                "pos": [
                                    {"x": 120, "y": 156},
                                    {"x": 240, "y": 156},
                                    {"x": 240, "y": 188},
                                    {"x": 120, "y": 188},
                                ],
                            },
                        ],
                    }
                ],
                "evidence_map": [
                    {
                        "source_layout_id": "layout_title_1",
                        "source_block_ids": ["p7_dm_title_1"],
                        "layout_pos": [
                            {"x": 120, "y": 120},
                            {"x": 840, "y": 120},
                            {"x": 840, "y": 188},
                            {"x": 120, "y": 188},
                        ],
                        "block_positions": [
                            [
                                {"x": 120, "y": 120},
                                {"x": 620, "y": 120},
                                {"x": 620, "y": 154},
                                {"x": 120, "y": 154},
                            ],
                            [
                                {"x": 120, "y": 156},
                                {"x": 240, "y": 156},
                                {"x": 240, "y": 188},
                                {"x": 120, "y": 188},
                            ],
                        ],
                    }
                ],
                "page_image": {"width": 1600, "height": 2200},
            },
        },
        style_intent=None,
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    components = list(ui_plan.get("components") or [])
    assert len(components) == 1
    node = dict(components[0] or {})
    assert list(node.get("source_atom_ids") or []) == ["layout_title_1"]
    anchors = list(node.get("source_anchor_refs") or [])
    assert len(anchors) == 1
    assert str(anchors[0].get("source_layout_id") or "") == "layout_title_1"
    assert str(anchors[0].get("coord_version") or "") == "layout_uid_v1"
    assert str(((anchors[0].get("geometry") or {}).get("polygons") or [])[0].get("source") or "") == "page_grounding_v1"
    assert len(list((anchors[0].get("geometry") or {}).get("polygons") or [])) == 2


def test_build_layout_uid_prompt_payload_should_only_emit_uniqueid_atoms():
    service = LiteratureReaderComposeService()
    payload = {
        "paper_id": 78,
        "page": 1,
        "ui_plan": {
            "plan_id": "plan_layout_uid_prompt",
            "components": [],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "quality_report": {"overall": 0.9, "validation_errors": []},
        "docmind_structure": {
            "page_image_url": "https://example.com/page-1.png",
            "layouts": [
                {
                    "index": 12,
                    "uniqueId": "f2c5fea143e04be47159e880ebe9037b",
                    "type": "title",
                    "subType": "none",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE\n",
                    "pos": [
                        {"x": 484, "y": 1338},
                        {"x": 1367, "y": 1338},
                        {"x": 1367, "y": 1398},
                        {"x": 484, "y": 1398},
                    ],
                    "pageNum": [0],
                    "blocks": [
                        {
                            "pos": [
                                {"x": 481, "y": 1334},
                                {"x": 1366, "y": 1334},
                                {"x": 1366, "y": 1367},
                                {"x": 481, "y": 1367},
                            ],
                            "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                        },
                        {
                            "pos": [
                                {"x": 481, "y": 1370},
                                {"x": 576, "y": 1370},
                                {"x": 576, "y": 1396},
                                {"x": 481, "y": 1396},
                            ],
                            "text": " USMLE",
                        },
                    ],
                }
            ],
        },
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p1_dm_p1_l012_b001",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "ChatGPT yields moderate accuracy approaching passing performance on",
                },
                {
                    "block_id": "p1_dm_p1_l012_b002",
                    "layout_unique_id": "f2c5fea143e04be47159e880ebe9037b",
                    "kind": "heading",
                    "zone_type": "main_body",
                    "text": "USMLE",
                },
            ]
        },
    }

    ensured = service._ensure_payload_contract(page=1, payload=payload)  # pylint: disable=protected-access
    prompt_payload = service._build_layout_uid_prompt_payload(  # pylint: disable=protected-access
        paper=SimpleNamespace(id=78, title="demo"),
        page=1,
        grounding=dict(ensured.get("page_grounding_v1") or {}),
    )

    compact_atoms = list(prompt_payload.get("layout_atoms") or [])
    assert len(compact_atoms) == 1
    assert compact_atoms[0] == {
        "layout_id": "f2c5fea143e04be47159e880ebe9037b",
        "reading_order": 1,
        "layout_type": "title",
        "layout_sub_type": "none",
        "node_kind": "title",
        "text": "ChatGPT yields moderate accuracy approaching passing performance on USMLE",
        "include_in_main_flow": True,
        "region_hint": "main_body",
        "layout_pos": [
            {"x": 484.0, "y": 1338.0},
            {"x": 1367.0, "y": 1338.0},
            {"x": 1367.0, "y": 1398.0},
            {"x": 484.0, "y": 1398.0},
        ],
        "block_count": 2,
    }
    assert "blocks" not in compact_atoms[0]
    assert "canonical_block_ids" not in compact_atoms[0]
    assert str(((prompt_payload.get("rules") or {}).get("indivisible_unit")) or "") == "layout_id"


def test_normalize_layout_uid_ai_reconstruction_plan_should_accept_poor_docmind_override():
    service = LiteratureReaderComposeService()
    plan, validation = service._normalize_layout_uid_ai_reconstruction_plan(  # pylint: disable=protected-access
        step_result={
            "reconstruction": {
                "mode": "ai_reconstructed",
                "docmind_quality": "poor",
                "reason": "The page is mostly a corrupted visual and the grounded OCR is unusable.",
                "confidence": 0.81,
                "components": [
                    {"kind": "heading", "text": "Figure 4", "level": 2},
                    {
                        "kind": "paragraph",
                        "text": "Figure 4 visualizes attention heads.",
                        "paragraphs": ["Figure 4 visualizes attention heads.", "Top and bottom panels compare head behaviors."],
                    },
                    {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                ],
                "notes": ["ai_reconstructed_due_to_poor_docmind"],
            }
        },
    )

    assert bool(validation.get("enabled")) is True
    assert bool(validation.get("passed")) is True
    assert str(plan.get("mode") or "") == "fully_reconstructed"
    assert str(plan.get("docmind_quality") or "") == "poor"
    assert abs(float(plan.get("confidence") or 0.0) - 0.81) < 1e-6
    assert len(list(plan.get("components") or [])) == 3
    assert str(((plan.get("components") or [])[1].get("kind") or "")) == "paragraph"
    assert len(list(((plan.get("components") or [])[1].get("paragraphs") or []))) == 2


def test_normalize_layout_uid_combined_plan_should_keep_partial_page_decision_with_reconstructed_components():
    service = LiteratureReaderComposeService()
    combined_plan = service._normalize_layout_uid_combined_plan(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {"layout_id": "layout-1", "reading_order": 1, "node_kind": "paragraph"},
                {"layout_id": "layout-2", "reading_order": 2, "node_kind": "figure"},
            ]
        },
        step_result={
            "text_items": [],
            "groups": [
                {"group_id": "g1", "group_kind": "paragraph", "source_layout_ids": ["layout-1"], "rationale": "prose"},
                {"group_id": "g2", "group_kind": "figure", "source_layout_ids": ["layout-2"], "rationale": "figure"},
            ],
            "omissions": [],
            "page_decision": {
                "mode": "partial_reconstructed",
                "reason": "figure crop needs recrop guidance",
                "confidence": 0.79,
            },
            "reconstructed_components": [
                {
                    "kind": "figure",
                    "caption": "Figure 2",
                    "source_label": "Figure 2",
                    "visual_spec": {
                        "seed_bbox_norm": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.8},
                        "must_include": ["main curve"],
                        "must_exclude": ["neighbor prose"],
                    },
                }
            ],
        },
    )

    assert str(((combined_plan.get("page_decision") or {}).get("mode") or "")) == "partial_reconstructed"
    reconstructed_components = list(combined_plan.get("reconstructed_components") or [])
    reconstructed_validation = dict(combined_plan.get("reconstructed_validation") or {})
    assert len(reconstructed_components) == 1
    assert str((reconstructed_components[0] or {}).get("kind") or "") == "figure"
    assert bool(reconstructed_validation.get("enabled")) is True
    assert bool(reconstructed_validation.get("passed")) is True


def test_analyze_layout_uid_grounding_quality_should_flag_single_collapsed_garbled_table_page():
    service = LiteratureReaderComposeService()
    warning = service._analyze_layout_uid_grounding_quality(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "layout_bad_1",
                    "layout_type": "table",
                    "node_kind": "table",
                    "include_in_main_flow": True,
                    "blocks": [
                        {
                            "text": "| <ped><ped><ped><SO3><SO3>ino!y!p iinoy!p eiou eiow sseoojd sseooud",
                        }
                    ],
                }
            ]
        },
        payload={
            "page_structure_v3": {
                "block_groups": [
                    {
                        "block_id": "p13_dm_p13_l001_b001",
                        "layout_unique_id": "layout_bad_1",
                        "kind": "paragraph",
                        "text": "| <ped><ped><ped><SO3><SO3>ino!y!p iinoy!p eiou eiow sseoojd sseooud",
                    }
                ]
            }
        },
    )

    assert bool(warning.get("single_main_layout")) is True
    assert bool(warning.get("collapsed_block_groups")) is True
    assert bool(warning.get("suspicious_ocr")) is True
    hint = dict(warning.get("reconstruction_hint") or {})
    assert bool(hint.get("recommended")) is True
    assert bool(hint.get("advisory_only")) is True
    assert str(hint.get("reason") or "") == "single_main_layout_plus_collapsed_block_groups_plus_garbled_ocr"
    assert bool(warning.get("should_force_reconstruction")) is False
    assert "table" in list(warning.get("main_layout_types") or [])
    assert list(warning.get("suspicious_text_examples") or [])


def test_layout_uid_combined_system_prompt_should_emphasize_poor_grounding_signals():
    prompt = LiteratureReaderComposeService._layout_uid_combined_system_prompt()  # pylint: disable=protected-access
    assert "collapsed block groups" in prompt
    assert "garbled OCR" in prompt
    assert "advisory evidence" in prompt
    assert "smallest bbox that still keeps the figure body readable" in prompt
    assert "region_description" in prompt
    assert "exclude caption and page number as much as possible" in prompt


def test_layout_uid_reconstruction_only_system_prompt_should_require_tight_figure_crops():
    prompt = LiteratureReaderComposeService._layout_uid_reconstruction_only_system_prompt()  # pylint: disable=protected-access
    assert "single collapsed block with garbled OCR" in prompt
    assert "tight but safe boundaries" in prompt
    assert "region_description" in prompt
    assert "Exclude the caption unless the caption is required" in prompt


def test_build_layout_uid_pipeline_result_should_not_force_ai_reconstruction_for_single_collapsed_garbled_page(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=86, user_id=1, title="demo", pdf_path="")
    garbage_text = "| <ped><ped><ped><SO3><SO3>ino!y!p iinoy!p eiou eiow sseoojd sseooud"
    base_payload = {
        "paper_id": 86,
        "page": 13,
        "build_mode": "parser_fallback",
        "structure_confidence": 0.76,
        "page_structure_v3": {
            "block_groups": [
                {
                    "block_id": "p13_dm_p13_l001_b001",
                    "layout_unique_id": "layout_bad_1",
                    "kind": "paragraph",
                    "zone_type": "main_body",
                    "text": garbage_text,
                }
            ]
        },
        "page_grounding_v1": {
            "version": "page_grounding_v1",
            "page": 13,
            "layout_atoms": [
                {
                    "layout_id": "layout_bad_1",
                    "layout_type": "table",
                    "layout_sub_type": "none",
                    "node_kind": "table",
                    "reading_order": 1,
                    "raw_text": "",
                    "clean_text": "",
                    "normalized_text": "",
                    "canonical_block_ids": ["p13_dm_p13_l001_b001"],
                    "include_in_main_flow": True,
                    "region_hint": "main_body",
                    "layout_pos": [
                        {"x": 238.0, "y": 233.0},
                        {"x": 867.0, "y": 233.0},
                        {"x": 867.0, "y": 1213.0},
                        {"x": 238.0, "y": 1213.0},
                    ],
                    "blocks": [
                        {
                            "block_index": 1,
                            "style_id": 1,
                            "text": garbage_text,
                            "pos": [
                                {"x": 238.0, "y": 233.0},
                                {"x": 867.0, "y": 233.0},
                                {"x": 867.0, "y": 1213.0},
                                {"x": 238.0, "y": 1213.0},
                            ],
                        }
                    ],
                }
            ],
            "page_image": {
                "url": "http://localhost:8888/api/v1/literature/reader/grounding-page-assets/86/13",
                "path": "/app/uploads/reader_page_assets/paper_86/grounding_pages/page_13.png",
                "width": 1483,
                "height": 1920,
                "source": "docmind_page_image_localized",
                "origin_url": "https://example.com/docmind/page13.png",
                "local_cached": True,
            },
        },
        "assets": [],
    }

    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(
        service._reader_service,  # pylint: disable=protected-access
        "_resolve_local_pdf_path",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(
        service,
        "_resolve_reader_page_image_asset",
        lambda **_kwargs: {
            "url": "http://localhost:8888/api/v1/literature/reader/page-assets/86/13",
            "path": "/app/uploads/reader_page_assets/86/page_13.jpg",
            "source": "page_render_asset",
            "origin_url": "",
            "local_cached": True,
        },
    )

    phases: List[str] = []

    async def _fake_invoke_single_agent_model(**kwargs):
        phase = str(kwargs.get("phase") or "")
        phases.append(phase)
        if phase == "layout_uid_combined_plan":
            return {
                "status": "done",
                "step_result": {
                    "text_items": [],
                    "groups": [
                        {
                            "group_id": "g1",
                            "group_kind": "figure",
                            "source_layout_ids": ["layout_bad_1"],
                            "rationale": "single collapsed visual block",
                        }
                    ],
                    "omissions": [],
                    "notes": ["single_layout_collapsed_page"],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        if phase == "layout_uid_reconstruction_retry":
            return {
                "status": "done",
                "step_result": {
                    "reconstruction": {
                        "mode": "ai_reconstructed",
                        "docmind_quality": "poor",
                        "reason": "DocMind collapsed the page into one corrupted table-like OCR block.",
                        "confidence": 0.88,
                        "components": [
                            {
                                "kind": "paragraph",
                                "text": "This page is a visual figure with heavily corrupted grounded OCR.",
                                "paragraphs": [
                                    {
                                        "text": "This page is a visual figure with heavily corrupted grounded OCR.",
                                        "bbox_norm": {"x0": 0.12, "y0": 0.18, "x1": 0.88, "y1": 0.3},
                                    }
                                ],
                                "bbox_norm": {"x0": 0.12, "y0": 0.18, "x1": 0.88, "y1": 0.3},
                            }
                        ],
                        "notes": ["forced_reconstruction_retry"],
                    }
                },
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }
        raise AssertionError(f"unexpected phase: {phase}")

    monkeypatch.setattr(service, "_invoke_single_agent_model", _fake_invoke_single_agent_model)
    monkeypatch.setattr(
        service,
        "_apply_layout_uid_text_normalization_to_grounding",
        lambda **kwargs: dict(kwargs.get("grounding") or {}),
    )

    async def _empty_map(**_kwargs):
        return {}

    monkeypatch.setattr(service, "_build_ai_reconstructed_figure_asset_map", _empty_map)

    result = asyncio.run(
        service._build_layout_uid_pipeline_result(  # pylint: disable=protected-access
            db=SimpleNamespace(),
            user_id=1,
            paper=paper,
            page=13,
            base_payload=base_payload,
            style_intent=None,
            theme_mode=None,
            detail_level="standard",
            compare_mode=False,
            latency_budget_ms=1000,
            selected_kb_id=84,
            pipeline_version="layout_uid_v1",
        )
    )

    assert phases == ["layout_uid_combined_plan"]
    loop_result = dict(result.get("loop_result") or {})
    payload = dict(result.get("base_payload") or {})
    assert str(loop_result.get("build_mode") or "") == "compose_agent_layout_uid_v1"
    assert str(loop_result.get("stop_reason") or "") == "layout_uid_v1_done"
    warning = dict((payload.get("layout_advice_v3") or {}).get("grounding_warning") or {})
    hint = dict(warning.get("reconstruction_hint") or {})
    assert bool(hint.get("recommended")) is True
    assert bool(hint.get("advisory_only")) is True
    qwen_meta = dict(payload.get("qwen_plan_meta") or {})
    assert str(qwen_meta.get("planning_mode") or "") == "combined_once"
    assert bool(qwen_meta.get("reconstruction_retry_used")) is False
    assert int(qwen_meta.get("prompt_tokens") or 0) == 10
    assert int(qwen_meta.get("completion_tokens") or 0) == 5
    assert int(qwen_meta.get("total_tokens") or 0) == 15
    assert str(qwen_meta.get("reconstruction_retry_reason") or "") == ""


def test_build_layout_uid_pipeline_result_should_backfill_grounded_page_mode(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=86, user_id=1, title="demo", pdf_path="")
    base_payload = {
        "paper_id": 86,
        "page": 8,
        "assets": [],
        "page_grounding_v1": {
            "layout_atoms": [
                {
                    "layout_id": "layout-1",
                    "node_kind": "paragraph",
                    "clean_text": "Grounded paragraph.",
                    "normalized_text": "Grounded paragraph.",
                }
            ],
            "page_image": {"url": "/api/v1/literature/reader/grounding-page-assets/86/8", "path": ""},
        },
        "docmind_structure": {"page_image_url": "/api/v1/literature/reader/grounding-page-assets/86/8"},
    }

    async def _empty_map(**_kwargs):
        return {}

    async def _fake_invoke_single_agent_model(**kwargs):
        assert str(kwargs.get("phase") or "") == "layout_uid_combined_plan"
        return {
            "status": "done",
            "step_result": {
                "text_items": [],
                "groups": [
                    {
                        "group_id": "group_1",
                        "group_kind": "paragraph",
                        "layout_ids": ["layout-1"],
                    }
                ],
                "omissions": [],
                "notes": [],
            },
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        }

    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_kwargs: "")
    monkeypatch.setattr(
        service,
        "_resolve_reader_page_image_asset",
        lambda **_kwargs: {
            "url": "/api/v1/literature/reader/page-assets/86/8",
            "path": "",
            "source": "page_render_asset",
            "origin_url": "",
            "local_cached": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_build_layout_uid_combined_prompt_payload",
        lambda **_kwargs: {"layout_atoms": [{"layout_id": "layout-1"}], "page_quality": {}},
    )
    monkeypatch.setattr(service, "_invoke_single_agent_model", _fake_invoke_single_agent_model)
    monkeypatch.setattr(
        service,
        "_normalize_layout_uid_combined_plan",
        lambda **_kwargs: {
            "normalization_plan": {"items": []},
            "text_validation": {"passed": True, "errors": [], "fallback_used": False},
            "grouping_plan": {
                "groups": [{"group_id": "group_1", "group_kind": "paragraph", "layout_ids": ["layout-1"]}],
                "omissions": [],
                "notes": [],
            },
            "grouping_validation": {"passed": True, "errors": [], "fallback_used": False},
            "reconstruction_plan": {"mode": "grounded", "components": [], "notes": []},
            "reconstruction_validation": {"enabled": False, "passed": True, "errors": []},
        },
    )
    monkeypatch.setattr(service, "_apply_layout_uid_text_normalization_to_grounding", lambda **kwargs: dict(kwargs.get("grounding") or {}))
    monkeypatch.setattr(service, "_build_layout_uid_table_refinement_map", _empty_map)
    monkeypatch.setattr(service, "_build_layout_uid_equation_refinement_map", _empty_map)
    monkeypatch.setattr(
        service,
        "_layout_uid_group_plan_to_panel_plan",
        lambda **_kwargs: {
            "plan_id": "layout_uid_v1_p8",
            "panels": [
                {
                    "panel_id": "layout_uid_main",
                    "nodes": [
                        {
                            "node_id": "group_1",
                            "component": "ParagraphProse",
                            "props": {"text": "Grounded paragraph."},
                            "children": [],
                            "source_layout_ids": ["layout-1"],
                        }
                    ],
                }
            ],
            "style_plan": {},
        },
    )
    monkeypatch.setattr(service, "_collect_docmind_blocks_for_single_agent", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(
        service,
        "_panel_plan_to_ui_plan",
        lambda **_kwargs: {
            "plan_id": "layout_uid_v1_p8",
            "components": [
                {
                    "id": "group_1",
                    "type": "ParagraphProse",
                    "props": {"text": "Grounded paragraph."},
                    "children": [],
                    "source_layout_ids": ["layout-1"],
                    "source_anchor_refs": [],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )

    result = asyncio.run(service._build_layout_uid_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=paper,
        page=8,
        base_payload=base_payload,
        style_intent=None,
        theme_mode=None,
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=1000,
        selected_kb_id=84,
        pipeline_version="layout_uid_v1",
    ))

    payload = dict(result.get("base_payload") or {})
    loop_result = dict(result.get("loop_result") or {})
    assert str(payload.get("page_mode") or "") == "grounded"
    assert str(((payload.get("page_grounding_policy") or {}).get("mode") or "")) == "grounded"
    assert str(((payload.get("qwen_plan_meta") or {}).get("page_mode") or "")) == "grounded"
    assert str(((payload.get("layout_advice_v3") or {}).get("page_mode") or "")) == "grounded"
    assert str(((payload.get("pipeline_contract_meta") or {}).get("page_mode") or "")) == "grounded"
    assert str((((loop_result.get("ui_plan") or {}).get("components") or [])[0].get("props") or {}).get("source_mode") or "") == "grounded"


def test_build_layout_uid_pipeline_result_should_use_partial_reconstructed_mode_with_grounded_compat(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=86, user_id=1, title="demo", pdf_path="")
    calls = {"grounded_panel": 0}
    base_payload = {
        "paper_id": 86,
        "page": 8,
        "assets": [],
        "page_grounding_v1": {
            "layout_atoms": [
                {
                    "layout_id": "layout-1",
                    "reading_order": 1,
                    "node_kind": "paragraph",
                    "clean_text": "Grounded paragraph.",
                    "normalized_text": "Grounded paragraph.",
                },
                {
                    "layout_id": "layout-2",
                    "reading_order": 2,
                    "node_kind": "figure",
                    "clean_text": "Figure 4: Two attention heads.",
                    "normalized_text": "Figure 4: Two attention heads.",
                },
            ],
            "page_image": {"url": "/api/v1/literature/reader/grounding-page-assets/86/8", "path": ""},
        },
        "docmind_structure": {"page_image_url": "/api/v1/literature/reader/grounding-page-assets/86/8"},
    }

    async def _empty_map(**_kwargs):
        return {}

    async def _fake_invoke_single_agent_model(**kwargs):
        assert str(kwargs.get("phase") or "") == "layout_uid_combined_plan"
        return {
            "status": "done",
            "step_result": {},
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }

    async def _fake_figure_assets(**_kwargs):
        return {
            1: {
                "image_url": "/api/v1/literature/reader/figure-assets/86/8/partial_1",
                "status": "verified_crop",
                "bbox_norm": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.8},
                "verification": {"pass": True},
            }
        }

    def _grounded_panel_plan(**_kwargs):
        calls["grounded_panel"] += 1
        return {
            "plan_id": "layout_uid_v1_p8",
            "panels": [
                {
                    "panel_id": "layout_uid_main",
                    "nodes": [
                        {
                            "node_id": "group_1",
                            "component": "ParagraphProse",
                            "props": {"text": "Grounded paragraph."},
                            "children": [],
                            "source_layout_ids": ["layout-1"],
                        },
                        {
                            "node_id": "group_2",
                            "component": "FigurePanel",
                            "props": {"caption": "Figure 4 old", "image_url": "", "source_label": "Figure 4"},
                            "children": [],
                            "source_layout_ids": ["layout-2"],
                        },
                    ],
                }
            ],
            "style_plan": {},
        }

    def _should_not_inject_ai_evidence(**_kwargs):
        raise AssertionError("partial_reconstructed should not run fully-reconstructed evidence injection")

    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_kwargs: "")
    monkeypatch.setattr(
        service,
        "_resolve_reader_page_image_asset",
        lambda **_kwargs: {
            "url": "/api/v1/literature/reader/page-assets/86/8",
            "path": "",
            "source": "page_render_asset",
            "origin_url": "",
            "local_cached": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_build_layout_uid_combined_prompt_payload",
        lambda **_kwargs: {"layout_atoms": [{"layout_id": "layout-1"}, {"layout_id": "layout-2"}], "page_quality": {}},
    )
    monkeypatch.setattr(service, "_invoke_single_agent_model", _fake_invoke_single_agent_model)
    monkeypatch.setattr(
        service,
        "_normalize_layout_uid_combined_plan",
        lambda **_kwargs: {
            "normalization_plan": {"items": []},
            "text_validation": {"passed": True, "errors": [], "fallback_used": False},
            "grouping_plan": {
                "groups": [
                    {"group_id": "group_1", "group_kind": "paragraph", "source_layout_ids": ["layout-1"]},
                    {"group_id": "group_2", "group_kind": "figure", "source_layout_ids": ["layout-2"]},
                ],
                "omissions": [],
                "notes": [],
            },
            "grouping_validation": {"passed": True, "errors": [], "fallback_used": False},
            "page_decision": {
                "mode": "partial_reconstructed",
                "reason": "replace figure with partial reconstruction",
                "confidence": 0.77,
            },
            "reconstructed_components": [
                {
                    "kind": "figure",
                    "caption": "Figure 4 new",
                    "source_label": "Figure 4",
                    "visual_spec": {"seed_bbox_norm": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.8}},
                }
            ],
            "reconstructed_validation": {"enabled": True, "passed": True, "errors": []},
            "reconstruction_plan": {"mode": "grounded", "components": [], "notes": []},
            "reconstruction_validation": {"enabled": False, "passed": True, "errors": []},
        },
    )
    monkeypatch.setattr(service, "_apply_layout_uid_text_normalization_to_grounding", lambda **kwargs: dict(kwargs.get("grounding") or {}))
    monkeypatch.setattr(service, "_build_layout_uid_table_refinement_map", _empty_map)
    monkeypatch.setattr(service, "_build_layout_uid_equation_refinement_map", _empty_map)
    monkeypatch.setattr(service, "_select_ai_reconstructed_figure_crop_source_asset", lambda **_kwargs: {"url": "/api/v1/literature/reader/page-assets/86/8", "path": "", "source": "page_render_asset", "local_cached": True})
    monkeypatch.setattr(service, "_resolve_grounding_page_image_size", lambda **_kwargs: (1200, 1600))
    monkeypatch.setattr(service, "_build_ai_reconstructed_figure_asset_map", _fake_figure_assets)
    monkeypatch.setattr(service, "_layout_uid_group_plan_to_panel_plan", _grounded_panel_plan)
    monkeypatch.setattr(service, "_collect_docmind_blocks_for_single_agent", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(
        service,
        "_panel_plan_to_ui_plan",
        lambda **_kwargs: {
            "plan_id": "layout_uid_v1_p8",
            "components": [
                {
                    "id": "group_1",
                    "type": "ParagraphProse",
                    "props": {"text": "Grounded paragraph."},
                    "children": [],
                    "source_layout_ids": ["layout-1"],
                    "source_anchor_refs": [],
                },
                {
                    "id": "group_2",
                    "type": "FigurePanel",
                    "props": {"caption": "Figure 4 old", "image_url": "", "source_label": "Figure 4"},
                    "children": [],
                    "source_layout_ids": ["layout-2"],
                    "source_anchor_refs": [],
                },
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )
    monkeypatch.setattr(service, "_inject_ai_reconstructed_evidence_source_anchor_refs", _should_not_inject_ai_evidence)

    result = asyncio.run(service._build_layout_uid_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=paper,
        page=8,
        base_payload=base_payload,
        style_intent=None,
        theme_mode=None,
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=1000,
        selected_kb_id=84,
        pipeline_version="layout_uid_v1",
    ))

    payload = dict(result.get("base_payload") or {})
    loop_result = dict(result.get("loop_result") or {})
    components = [dict(row) for row in list(((loop_result.get("ui_plan") or {}).get("components") or [])) if isinstance(row, dict)]
    assert calls["grounded_panel"] == 1
    assert str(payload.get("page_mode") or "") == "partial_reconstructed"
    assert str(payload.get("grounding_mode") or "") == "grounded"
    assert str(payload.get("reconstruction_mode") or "") == "grounded"
    assert any(str(((row.get("props") or {}).get("source_mode") or "")) == "partial_reconstructed" for row in components)


def test_build_layout_uid_pipeline_result_should_backfill_fully_reconstructed_page_metadata(monkeypatch):
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=86, user_id=1, title="demo", pdf_path="")
    base_payload = {
        "paper_id": 86,
        "page": 14,
        "assets": [],
        "page_grounding_v1": {
            "layout_atoms": [
                {
                    "layout_id": "layout-1",
                    "node_kind": "figure",
                    "clean_text": "Figure 4: Two attention heads.",
                    "normalized_text": "Figure 4: Two attention heads.",
                }
            ],
            "page_image": {"url": "/api/v1/literature/reader/grounding-page-assets/86/14", "path": ""},
        },
        "docmind_structure": {"page_image_url": "/api/v1/literature/reader/grounding-page-assets/86/14"},
    }

    async def _fake_invoke_single_agent_model(**kwargs):
        assert str(kwargs.get("phase") or "") == "layout_uid_combined_plan"
        return {
            "status": "done",
            "step_result": {
                "text_items": [],
                "groups": [],
                "omissions": [],
                "notes": [],
                "reconstruction": {
                    "mode": "ai_reconstructed",
                    "docmind_quality": "poor",
                    "reason": "DocMind grounding is unusable.",
                    "confidence": 0.84,
                    "components": [
                        {"kind": "heading", "text": "Figure 4", "level": 2},
                        {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                        {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
                    ],
                    "notes": ["ai_reconstructed_due_to_poor_docmind"],
                },
            },
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }

    async def _empty_map(**_kwargs):
        return {}

    monkeypatch.setattr(service, "_ensure_payload_contract", lambda **kwargs: dict(kwargs.get("payload") or {}))
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_kwargs: "")
    monkeypatch.setattr(
        service,
        "_resolve_reader_page_image_asset",
        lambda **_kwargs: {
            "url": "/api/v1/literature/reader/page-assets/86/14",
            "path": "",
            "source": "page_render_asset",
            "origin_url": "",
            "local_cached": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_build_layout_uid_combined_prompt_payload",
        lambda **_kwargs: {"layout_atoms": [{"layout_id": "layout-1"}], "page_quality": {}},
    )
    monkeypatch.setattr(service, "_invoke_single_agent_model", _fake_invoke_single_agent_model)
    monkeypatch.setattr(
        service,
        "_normalize_layout_uid_combined_plan",
        lambda **_kwargs: {
            "normalization_plan": {"items": []},
            "text_validation": {"passed": True, "errors": [], "fallback_used": False},
            "grouping_plan": {"groups": [], "omissions": [], "notes": []},
            "grouping_validation": {"passed": True, "errors": [], "fallback_used": False},
            "page_decision": {
                "mode": "fully_reconstructed",
                "reason": "DocMind grounding is unusable.",
                "confidence": 0.84,
            },
            "reconstructed_components": [
                {"kind": "heading", "text": "Figure 4", "level": 2},
                {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
            ],
            "reconstructed_validation": {"enabled": True, "passed": True, "errors": []},
            "reconstruction_plan": {
                "mode": "ai_reconstructed",
                "docmind_quality": "poor",
                "reason": "DocMind grounding is unusable.",
                "confidence": 0.84,
                "components": [
                    {"kind": "heading", "text": "Figure 4", "level": 2},
                    {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                    {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
                ],
                "notes": ["ai_reconstructed_due_to_poor_docmind"],
            },
            "reconstruction_validation": {"enabled": True, "passed": True, "errors": []},
        },
    )
    monkeypatch.setattr(service, "_apply_layout_uid_text_normalization_to_grounding", lambda **kwargs: dict(kwargs.get("grounding") or {}))
    monkeypatch.setattr(service, "_build_layout_uid_table_refinement_map", _empty_map)
    monkeypatch.setattr(service, "_build_layout_uid_equation_refinement_map", _empty_map)
    monkeypatch.setattr(
        service,
        "_select_ai_reconstructed_figure_crop_source_asset",
        lambda **_kwargs: {
            "url": "/api/v1/literature/reader/page-assets/86/14",
            "path": "",
            "source": "page_render_asset",
            "local_cached": True,
        },
    )
    monkeypatch.setattr(service, "_resolve_grounding_page_image_size", lambda **_kwargs: (1200, 1600))
    monkeypatch.setattr(service, "_build_ai_reconstructed_figure_asset_map", _empty_map)
    monkeypatch.setattr(
        service,
        "_inject_ai_reconstructed_evidence_source_anchor_refs",
        lambda **kwargs: (dict(kwargs.get("ui_plan") or {}), {"enabled": False, "anchor_count": 0, "page": 14, "page_image_source": "page_render_asset", "nodes": []}),
    )

    result = asyncio.run(service._build_layout_uid_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=paper,
        page=14,
        base_payload=base_payload,
        style_intent=None,
        theme_mode=None,
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=1000,
        selected_kb_id=84,
        pipeline_version="layout_uid_v1",
    ))

    payload = dict(result.get("base_payload") or {})
    assert str(payload.get("page_mode") or "") == "fully_reconstructed"
    assert str(payload.get("grounding_mode") or "") == "ai_reconstructed"
    assert str(((payload.get("page_grounding_policy") or {}).get("page_mode") or "")) == "fully_reconstructed"
    assert str(((payload.get("page_grounding_policy") or {}).get("mode") or "")) == "ai_reconstructed"


def test_select_ai_reconstructed_figure_crop_source_asset_should_prefer_page_render_asset(tmp_path):
    service = LiteratureReaderComposeService()
    small_path = tmp_path / "small.jpg"
    large_path = tmp_path / "large.jpg"
    Image.new("RGB", (800, 1000), color="white").save(small_path, format="JPEG")
    Image.new("RGB", (1200, 1400), color="white").save(large_path, format="JPEG")

    selected = service._select_ai_reconstructed_figure_crop_source_asset(  # pylint: disable=protected-access
        rendered_page_image_url="/api/v1/literature/reader/page-assets/86/13",
        rendered_page_image_path=str(small_path),
        grounding={
            "page_image": {
                "url": "/api/v1/literature/reader/grounding-page-assets/86/13",
                "path": str(large_path),
                "source": "docmind_page_image_localized",
            }
        },
    )

    assert str(selected.get("source") or "") == "page_render_asset"
    assert str(selected.get("path") or "") == str(small_path)
    assert int(selected.get("width") or 0) == 800
    assert int(selected.get("height") or 0) == 1000


def test_ai_reconstructed_figure_asset_id_should_include_version():
    service = LiteratureReaderComposeService()
    asset_id = service._ai_reconstructed_figure_asset_id(  # pylint: disable=protected-access
        page=14,
        idx=2,
        signature="abc123def456",
        round_index=3,
    )

    assert asset_id.startswith("ai_recon_p14_f2_abc123def456_r3_")
    assert asset_id.endswith("v4_direct_ai_crop_q92")


def test_normalize_layout_uid_ai_reconstruction_plan_should_preserve_visual_spec_for_figure():
    service = LiteratureReaderComposeService()
    plan, validation = service._normalize_layout_uid_ai_reconstruction_plan(  # pylint: disable=protected-access
        step_result={
            "reconstruction": {
                "mode": "ai_reconstructed",
                "docmind_quality": "poor",
                "reason": "Figure geometry is rough, so reconstruct the figure from the page image and coarse DocMind boundary.",
                "confidence": 0.92,
                "components": [
                    {
                        "kind": "paragraph",
                        "text": "The reconstructed paragraph should stay split into model paragraphs.",
                        "paragraphs": [
                            "The reconstructed paragraph should stay split into model paragraphs.",
                            "The second paragraph stays distinct for the runtime reader.",
                        ],
                    },
                    {
                        "kind": "figure",
                        "caption": "Figure 4: Two attention heads.",
                        "source_label": "Figure 4",
                        "notes": [
                            "use the page image and coarse DocMind boundary to estimate a rough figure crop",
                            "keep the figure body and trim caption noise if needed",
                        ],
                        "partial_reconstruction": "keep the figure body with a rough crop around the visual region",
                        "visual_spec": {
                            "bbox_norm": {"x0": 0.08, "y0": 0.16, "x1": 0.92, "y1": 0.74},
                            "must_include": ["attention heads", "full panels"],
                            "must_exclude": ["header", "footer", "neighbor paragraph"],
                            "region_description": "rough crop around the plotted attention map and node panels",
                            "boundary_notes": "use the visible text positions and x/y boundaries to exclude page chrome and caption noise",
                            "crop_strategy": "figure_only_with_caption_trimmed_if_possible",
                            "require_full_boundary": True,
                            "prefer_without_caption": True,
                            "allow_caption_if_needed": False,
                        },
                    }
                ],
                "notes": ["figure_needs_verified_crop"],
            }
        },
    )

    assert bool(validation.get("enabled")) is True
    assert bool(validation.get("passed")) is True
    components = list(plan.get("components") or [])
    assert len(components) == 2

    paragraph = dict(components[0] or {})
    assert str(paragraph.get("kind") or "") == "paragraph"
    assert str(((paragraph.get("text") or ""))) == "The reconstructed paragraph should stay split into model paragraphs."
    assert [str(row.get("text") or "") for row in list(paragraph.get("paragraphs") or [])] == [
        "The reconstructed paragraph should stay split into model paragraphs.",
        "The second paragraph stays distinct for the runtime reader.",
    ]

    figure = dict(components[1] or {})
    assert str(figure.get("kind") or "") == "figure"
    visual_spec = dict(figure.get("visual_spec") or {})
    assert dict(visual_spec.get("seed_bbox_norm") or {}) == {"x0": 0.08, "y0": 0.16, "x1": 0.92, "y1": 0.74}
    assert list(visual_spec.get("must_include") or []) == ["attention heads", "full panels"]
    assert list(visual_spec.get("must_exclude") or []) == ["header", "footer", "neighbor paragraph"]
    assert str(visual_spec.get("region_description") or "") == "rough crop around the plotted attention map and node panels"
    assert str(visual_spec.get("boundary_notes") or "") == "use the visible text positions and x/y boundaries to exclude page chrome and caption noise"
    assert bool(visual_spec.get("require_full_boundary")) is True
    assert bool(visual_spec.get("prefer_without_caption")) is True
    assert bool(visual_spec.get("allow_caption_if_needed")) is False
    assert str(visual_spec.get("crop_strategy") or "") == "figure_only_with_caption_trimmed_if_possible"
    assert list(figure.get("notes") or []) == [
        "use the page image and coarse DocMind boundary to estimate a rough figure crop",
        "keep the figure body and trim caption noise if needed",
    ]
    assert str(figure.get("partial_reconstruction") or "") == "keep the figure body with a rough crop around the visual region"


def test_layout_uid_combined_system_prompt_should_request_coarse_bbox_guidance_for_reconstructed_figures():
    service = LiteratureReaderComposeService()
    prompt = service._layout_uid_combined_system_prompt()  # pylint: disable=protected-access

    assert "coarse_visual_bbox_norm" in prompt
    assert "preferred coarse boundary for figure crops on collapsed pages" in prompt
    assert "mislabeled the region as a table" in prompt
    assert "partial_reconstructed" in prompt
    assert "fully_reconstructed" in prompt


def test_derive_ai_reconstructed_figure_seed_bbox_norm_should_use_single_main_layout_bbox_even_when_layout_is_mislabeled_table():
    service = LiteratureReaderComposeService()
    seed_bbox = service._derive_ai_reconstructed_figure_seed_bbox_norm(  # pylint: disable=protected-access
        visual_spec={},
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "layout_bad_1",
                    "layout_type": "table",
                    "node_kind": "table",
                    "layout_pos": [
                        {"x": 120.0, "y": 100.0},
                        {"x": 1320.0, "y": 100.0},
                        {"x": 1320.0, "y": 900.0},
                        {"x": 120.0, "y": 900.0},
                    ],
                }
            ]
        },
    )

    assert dict(seed_bbox or {}) == {"x0": 0.0909, "y0": 0.0758, "x1": 1.0, "y1": 0.6818}


def test_expand_ai_reconstructed_figure_crop_bbox_norm_should_add_rough_margin_and_clamp_edges():
    service = LiteratureReaderComposeService()
    expanded = service._expand_ai_reconstructed_figure_crop_bbox_norm(  # pylint: disable=protected-access
        {"x0": 0.005, "y0": 0.004, "x1": 0.13, "y1": 0.14}
    )

    assert dict(expanded or {}) == {
        "x0": 0.0,
        "y0": 0.0,
        "x1": 0.14,
        "y1": 0.15,
    }


def test_infer_docmind_page_size_should_prefer_anchor_geometry_over_layout_extent():
    service = LiteratureReaderComposeService()
    width, height = service._infer_docmind_page_size(  # pylint: disable=protected-access
        payload={
            "page_grounding_v1": {
                "page_image": {
                    "width": 999,
                    "height": 944,
                }
            },
            "ui_plan": {
                "components": [
                    {
                        "id": "fig-3",
                        "type": "FigurePanel",
                        "source_anchor_refs": [
                            {
                                "geometry": {"page_width": 1483, "page_height": 1920},
                                "bbox_hint": {"page_width": 1483, "page_height": 1920},
                            }
                        ],
                    }
                ]
            },
        },
        layouts=[
            {
                "pos": [
                    {"x": 100.0, "y": 100.0},
                    {"x": 999.0, "y": 100.0},
                    {"x": 999.0, "y": 944.0},
                    {"x": 100.0, "y": 944.0},
                ]
            }
        ],
    )

    assert int(width or 0) == 1483
    assert int(height or 0) == 1920


def test_build_figure_assets_sync_should_prefer_clipped_bbox_over_native_and_version_grounded_assets(
    tmp_path, monkeypatch
):
    service = LiteratureReaderComposeService()
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    pdf_path = tmp_path / "paper.pdf"
    out_dir = tmp_path / "reader_figure_assets" / "85" / "p3"
    out_dir.mkdir(parents=True, exist_ok=True)

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    with open(pdf_path, "wb") as handle:
        writer.write(handle)

    legacy_path = out_dir / "figure_3.jpg"
    Image.new("RGB", (24, 24), "red").save(legacy_path, format="JPEG")
    recorded = {}

    class _FakePage:
        width = 600
        height = 800
        images = []

        @staticmethod
        def to_image(resolution=220):  # noqa: ARG004
            return SimpleNamespace(original=Image.new("RGB", (600, 800), "white"))

    class _FakePdf:
        pages = [_FakePage(), _FakePage(), _FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ARG002
            return False

    fake_pdfplumber = types.ModuleType("pdfplumber")
    fake_pdfplumber.open = lambda *_args, **_kwargs: _FakePdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)
    monkeypatch.setattr(service, "_filter_page_images_by_bbox", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_select_native_pdf_image", lambda **_kwargs: SimpleNamespace(
        data=b"native-image-bytes",
        name="native-image.jpg",
        image=Image.new("RGB", (80, 80), "white"),
    ))

    def _recording_convert_docmind_bbox_to_pdf(**kwargs):
        recorded["convert"] = (
            float(kwargs.get("docmind_width") or 0.0),
            float(kwargs.get("docmind_height") or 0.0),
        )
        return tuple(kwargs.get("bbox") or ())

    def _recording_write_native_pdf_image(**kwargs):
        recorded["native_called"] = True
        recorded["asset_id"] = str(kwargs.get("asset_id") or "")
        target = out_dir / f"{recorded['asset_id']}.jpg"
        Image.new("RGB", (64, 64), "blue").save(target, format="JPEG")
        return str(target)

    def _recording_write_clipped_page_image(**kwargs):
        recorded["clip_called"] = True
        recorded["asset_id"] = str(kwargs.get("asset_id") or "")
        target = out_dir / f"{recorded['asset_id']}.png"
        Image.new("RGB", (96, 96), "green").save(target, format="PNG")
        return str(target)

    monkeypatch.setattr(service, "_convert_docmind_bbox_to_pdf", _recording_convert_docmind_bbox_to_pdf)
    monkeypatch.setattr(service, "_write_native_pdf_image", _recording_write_native_pdf_image)
    monkeypatch.setattr(service, "_write_clipped_page_image", _recording_write_clipped_page_image)

    assets = service._build_figure_assets_sync(  # pylint: disable=protected-access
        paper_id=85,
        page=3,
        pdf_path=str(pdf_path),
        payload={
            "ui_plan": {
                "components": [
                    {
                        "id": "fig-3",
                        "type": "FigurePanel",
                        "source_anchor_refs": [
                            {
                                "geometry": {"page_width": 1483, "page_height": 1920},
                                "bbox_hint": {"page_width": 1483, "page_height": 1920},
                            }
                        ],
                    }
                ]
            },
            "page_grounding_v1": {"page_image": {"width": 999, "height": 944}},
        },
        figure_layouts=[
            {
                "uniqueId": "figure_3",
                "type": "figure",
                "pos": [
                    {"x": 100.0, "y": 100.0},
                    {"x": 999.0, "y": 100.0},
                    {"x": 999.0, "y": 944.0},
                    {"x": 100.0, "y": 944.0},
                ],
                "text": "Figure 3",
            }
        ],
    )

    versioned_asset_id = service._grounded_figure_asset_id(layout_uid="figure_3")  # pylint: disable=protected-access
    expected_convert = (1483.0, 1920.0)

    assert str(recorded.get("asset_id") or "") == versioned_asset_id
    assert recorded.get("convert") == expected_convert
    assert bool(recorded.get("clip_called")) is True
    assert bool(recorded.get("native_called")) is False
    assert legacy_path.exists()
    assert (out_dir / f"{versioned_asset_id}.png").exists()
    assert len(assets) == 1
    asset = dict(assets[0] or {})
    meta = dict(asset.get("meta") or {})
    assert str(meta.get("asset_id") or "") == versioned_asset_id
    assert str(meta.get("layout_unique_id") or "") == "figure_3"
    assert str(meta.get("asset_version") or "") == compose_module.GROUNDED_FIGURE_ASSET_VERSION
    assert str(asset.get("href") or "").endswith(f"/figure-assets/85/3/{versioned_asset_id}")


def test_locate_reader_figure_asset_candidate_file_should_ignore_legacy_grounded_asset_file(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    base_dir = tmp_path / "reader_figure_assets" / "85" / "p3"
    base_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = base_dir / "figure_3.jpg"
    legacy_path.write_bytes(b"legacy")
    versioned_asset_id = f"figure_3_{compose_module.GROUNDED_FIGURE_ASSET_VERSION}"
    versioned_path = base_dir / f"{versioned_asset_id}.jpg"
    versioned_path.write_bytes(b"versioned")

    assert literature_api._locate_reader_figure_asset_candidate_file(85, 3, "figure_3") == (
        str(versioned_path),
        "jpg",
    )
    assert literature_api._locate_reader_figure_asset_candidate_file(85, 3, versioned_asset_id) == (
        str(versioned_path),
        "jpg",
    )


def test_build_ai_reconstructed_figure_asset_map_should_expand_ai_bbox_and_clamp_edges_before_crop(tmp_path, monkeypatch):
    service = LiteratureReaderComposeService()
    page_image_path = tmp_path / "page.jpg"
    Image.new("RGB", (1200, 1600), "white").save(page_image_path, format="JPEG")
    out_dir = tmp_path / "figure-assets"
    crop_bboxes = []
    verifier_calls = {"count": 0}

    def _select_source_asset(*_args, **_kwargs):
        return {
            "url": "/api/v1/literature/reader/page-assets/86/13",
            "path": str(page_image_path),
            "source": "page_render_asset",
        }

    def _recording_write_clipped_page_image_norm(**kwargs):
        crop_bboxes.append(dict(kwargs.get("bbox_norm") or {}))
        asset_id = str(kwargs.get("asset_id") or "crop")
        target = out_dir / f"{asset_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 1600), "white").save(target, format="JPEG")
        return str(target)

    async def _fail_if_called(*_args, **_kwargs):
        verifier_calls["count"] += 1
        raise AssertionError("direct crop flow should not invoke the VL verifier")

    raw_component = {
        "kind": "figure",
        "caption": "Figure 3: Attention visualizations.",
        "source_label": "Figure 3",
        "bbox_norm": {"x0": 0.005, "y0": 0.004, "x1": 0.13, "y1": 0.14},
        "visual_spec": {
            "must_include": ["attention visualizations"],
            "must_exclude": ["caption", "neighbor prose"],
            "region_description": "rough crop around the figure body",
            "boundary_notes": "use the AI bbox directly and add a small margin",
            "require_full_boundary": True,
            "prefer_without_caption": True,
            "allow_caption_if_needed": False,
        },
    }

    selected_bbox_norm, bbox_source, *_ = service._resolve_ai_reconstructed_figure_crop_bbox_norm(  # pylint: disable=protected-access
        raw_component=raw_component,
        grounding={"page_image": {"width": 1200, "height": 1600}, "layout_atoms": []},
        payload={"page_structure_v3": {"block_groups": []}},
    )

    assert selected_bbox_norm is not None
    assert dict(selected_bbox_norm or {}) == {"x0": 0.0, "y0": 0.0, "x1": 0.14, "y1": 0.15}
    assert str(bbox_source or "") == "component_bbox_norm"

    monkeypatch.setattr(service, "_select_ai_reconstructed_figure_crop_source_asset", _select_source_asset)
    monkeypatch.setattr(service, "_reader_figure_asset_out_dir", lambda **_kwargs: str(out_dir))
    monkeypatch.setattr(service, "_write_clipped_page_image_norm", _recording_write_clipped_page_image_norm)
    monkeypatch.setattr(service, "_verify_ai_reconstructed_figure_crop", _fail_if_called)

    payload = {"page_structure_v3": {"block_groups": []}}
    assets = asyncio.run(
        service._build_ai_reconstructed_figure_asset_map(  # pylint: disable=protected-access
            paper_id=86,
            page=13,
            reconstruction_plan={
                "mode": "partial_reconstructed",
                "components": [raw_component],
            },
            page_image_url="/api/v1/literature/reader/page-assets/86/13",
            page_image_path=str(page_image_path),
            grounding={"page_image": {"width": 1200, "height": 1600}, "layout_atoms": []},
            payload=payload,
        )
    )

    entry = dict(assets.get(1) or {})
    assert verifier_calls["count"] == 0
    assert len(crop_bboxes) == 1
    assert dict(crop_bboxes[0] or {}) == dict(selected_bbox_norm or {})
    assert dict(crop_bboxes[0] or {}) != {"x0": 0.005, "y0": 0.004, "x1": 0.13, "y1": 0.14}
    assert str(entry.get("status") or "") == "direct_ai_crop"
    assert dict(entry.get("bbox_norm") or {}) == dict(selected_bbox_norm or {})
    assert str(entry.get("crop_bbox_source") or "") == str(bbox_source or "")


def test_build_ai_reconstructed_figure_asset_map_should_use_single_direct_ai_crop_from_coarse_bbox(tmp_path, monkeypatch):
    service = LiteratureReaderComposeService()
    page_image_path = tmp_path / "page.jpg"
    Image.new("RGB", (1200, 1600), "white").save(page_image_path, format="JPEG")
    out_dir = tmp_path / "figure-assets"
    crop_bboxes = []
    verifier_calls = {"count": 0}

    def _select_source_asset(*_args, **_kwargs):
        return {
            "url": "/api/v1/literature/reader/page-assets/86/13",
            "path": str(page_image_path),
            "source": "page_render_asset",
        }

    def _recording_write_clipped_page_image_norm(**kwargs):
        crop_bboxes.append(dict(kwargs.get("bbox_norm") or {}))
        asset_id = str(kwargs.get("asset_id") or "crop")
        target = out_dir / f"{asset_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 1600), "white").save(target, format="JPEG")
        return str(target)

    async def _fail_if_called(*_args, **_kwargs):
        verifier_calls["count"] += 1
        raise AssertionError("direct crop flow should not invoke the VL verifier")

    raw_component = {
        "kind": "figure",
        "caption": "Figure 3: Attention visualizations.",
        "source_label": "Figure 3",
        "visual_spec": {
            "must_include": ["attention visualizations"],
            "must_exclude": ["caption", "neighbor prose"],
            "region_description": "tight crop around the figure body",
            "boundary_notes": "exclude caption and surrounding prose",
            "require_full_boundary": True,
            "prefer_without_caption": True,
            "allow_caption_if_needed": False,
        },
    }
    selected_bbox_norm, bbox_source, *_ = service._resolve_ai_reconstructed_figure_crop_bbox_norm(  # pylint: disable=protected-access
        raw_component=raw_component,
        grounding={
            "page_image": {"width": 1200, "height": 1600},
            "layout_atoms": [
                {
                    "layout_id": "layout_bad_1",
                    "layout_type": "table",
                    "node_kind": "table",
                    "include_in_main_flow": True,
                    "layout_pos": [
                        {"x": 120.0, "y": 100.0},
                        {"x": 1320.0, "y": 100.0},
                        {"x": 1320.0, "y": 900.0},
                        {"x": 120.0, "y": 900.0},
                    ],
                }
            ],
        },
        payload={"page_structure_v3": {"block_groups": []}},
    )
    assert selected_bbox_norm is not None
    assert str(bbox_source or "") == "docmind_coarse_bbox_norm"
    grounding = {
        "page_image": {"width": 1200, "height": 1600},
        "layout_atoms": [
            {
                "layout_id": "layout_bad_1",
                "layout_type": "table",
                "node_kind": "table",
                "include_in_main_flow": True,
                "layout_pos": [
                    {"x": 120.0, "y": 100.0},
                    {"x": 1320.0, "y": 100.0},
                    {"x": 1320.0, "y": 900.0},
                    {"x": 120.0, "y": 900.0},
                ],
            }
        ],
    }

    monkeypatch.setattr(service, "_select_ai_reconstructed_figure_crop_source_asset", _select_source_asset)
    monkeypatch.setattr(service, "_reader_figure_asset_out_dir", lambda **_kwargs: str(out_dir))
    monkeypatch.setattr(service, "_write_clipped_page_image_norm", _recording_write_clipped_page_image_norm)
    monkeypatch.setattr(service, "_verify_ai_reconstructed_figure_crop", _fail_if_called)

    payload = {"page_structure_v3": {"block_groups": []}}
    assets = asyncio.run(
        service._build_ai_reconstructed_figure_asset_map(  # pylint: disable=protected-access
            paper_id=86,
            page=13,
            reconstruction_plan={
                "mode": "partial_reconstructed",
                "components": [raw_component],
            },
            page_image_url="/api/v1/literature/reader/page-assets/86/13",
            page_image_path=str(page_image_path),
            grounding=grounding,
            payload=payload,
        )
    )

    entry = dict(assets.get(1) or {})
    assert verifier_calls["count"] == 0
    assert len(crop_bboxes) == 1
    assert dict(crop_bboxes[0] or {}) == dict(selected_bbox_norm or {})
    assert str(entry.get("status") or "") == "direct_ai_crop"
    assert dict(entry.get("bbox_norm") or {}) == dict(selected_bbox_norm or {})
    assert str(entry.get("crop_bbox_source") or "") == str(bbox_source or "")


def test_build_ai_reconstructed_figure_asset_map_should_fallback_to_page_image_when_bbox_is_missing_or_invalid(tmp_path, monkeypatch):
    service = LiteratureReaderComposeService()
    page_image_path = tmp_path / "page.jpg"
    Image.new("RGB", (1200, 1600), "white").save(page_image_path, format="JPEG")
    out_dir = tmp_path / "figure-assets"
    crop_bboxes = []

    def _select_source_asset(*_args, **_kwargs):
        return {
            "url": "/api/v1/literature/reader/page-assets/86/13",
            "path": str(page_image_path),
            "source": "page_render_asset",
        }

    def _recording_write_clipped_page_image_norm(**kwargs):
        crop_bboxes.append(dict(kwargs.get("bbox_norm") or {}))
        asset_id = str(kwargs.get("asset_id") or "crop")
        target = out_dir / f"{asset_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 1600), "white").save(target, format="JPEG")
        return str(target)

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("missing bbox should not reach the verifier")

    monkeypatch.setattr(service, "_select_ai_reconstructed_figure_crop_source_asset", _select_source_asset)
    monkeypatch.setattr(service, "_reader_figure_asset_out_dir", lambda **_kwargs: str(out_dir))
    monkeypatch.setattr(service, "_write_clipped_page_image_norm", _recording_write_clipped_page_image_norm)
    monkeypatch.setattr(
        service,
        "_resolve_ai_reconstructed_figure_crop_bbox_norm",
        lambda **_kwargs: (None, "missing_bbox_norm", None, None),
    )
    monkeypatch.setattr(service, "_verify_ai_reconstructed_figure_crop", _fail_if_called)

    payload = {"page_structure_v3": {"block_groups": []}}
    assets = asyncio.run(
        service._build_ai_reconstructed_figure_asset_map(  # pylint: disable=protected-access
            paper_id=86,
            page=13,
            reconstruction_plan={
                "mode": "partial_reconstructed",
                "components": [
                    {
                        "kind": "figure",
                        "caption": "Figure 3: Attention visualizations.",
                        "source_label": "Figure 3",
                        "visual_spec": {
                            "seed_bbox_norm": {"x0": 0.15, "y0": 0.15, "x1": 0.85, "y1": 0.85},
                            "must_include": ["attention visualizations"],
                            "must_exclude": ["caption", "neighbor prose"],
                            "region_description": "tight crop around the figure body",
                            "boundary_notes": "exclude caption and surrounding prose",
                            "require_full_boundary": True,
                            "prefer_without_caption": True,
                            "allow_caption_if_needed": False,
                        },
                    }
                ],
            },
            page_image_url="/api/v1/literature/reader/page-assets/86/13",
            page_image_path=str(page_image_path),
            grounding={"page_image": {"width": 1200, "height": 1600}},
            payload=payload,
        )
    )

    entry = dict(assets.get(1) or {})
    assert str(entry.get("status") or "") == "fallback_page"
    assert str(entry.get("image_url") or "") == "/api/v1/literature/reader/page-assets/86/13"
    assert len(crop_bboxes) == 0


def test_ensure_payload_contract_should_retain_ai_evidence_and_anchor_refs_when_bbox_hint_exists():
    service = LiteratureReaderComposeService()
    bbox_hint = {"x0": 0.1, "x1": 0.9, "top": 0.15, "bottom": 0.75, "page_width": 1000, "page_height": 1200}
    ensured = service._ensure_payload_contract(  # pylint: disable=protected-access
        page=14,
        payload={
            "paper_id": 86,
            "page": 14,
            "status": "done",
            "build_mode": "compose_ai_reconstructed",
            "grounding_mode": "ai_reconstructed",
            "evidence_enabled": True,
            "runtime_build_plan_evidence": True,
            "page_grounding_policy": {
                "mode": "ai_reconstructed",
                "evidence_enabled": True,
                "reason": "DocMind grounding unusable for this page.",
                "docmind_quality": "poor",
                "confidence": 0.91,
            },
            "quality_report": {
                "overall": 0.74,
                "stop_reason": "layout_uid_v1_ai_reconstructed",
                "validation_errors": [],
            },
            "ui_plan": {
                "plan_id": "ai_reconstructed_p14",
                "components": [
                    {
                        "id": "ai_reconstructed_1",
                        "type": "ParagraphProse",
                        "props": {"text": "Reconstructed summary of the page."},
                        "children": [],
                        "source_block_ids": [],
                        "source_anchor_refs": [
                            {
                                "page": 14,
                                "start_char": 0,
                                "end_char": 32,
                                "quote_text": "Reconstructed summary of the page.",
                                "anchor_id": "ai-bbox-1",
                                "anchor_confidence": 0.94,
                                "bbox_hint": bbox_hint,
                                "coord_version": "ai_bbox_v1",
                            }
                        ],
                    }
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
            "page_grounding_v1": {
                "layout_atoms": [
                    {
                        "layout_id": "layout-1",
                        "node_kind": "paragraph",
                        "clean_text": "Reconstructed summary of the page.",
                        "normalized_text": "Reconstructed summary of the page.",
                    }
                ]
            },
        },
    )

    assert bool(ensured.get("evidence_enabled")) is True
    assert bool(ensured.get("runtime_build_plan_evidence")) is True
    assert str(((ensured.get("page_grounding_policy") or {}).get("mode") or "")) == "ai_reconstructed"
    refs = list((((ensured.get("ui_plan") or {}).get("components") or [])[0].get("source_anchor_refs") or []))
    assert len(refs) == 1
    assert dict(refs[0].get("bbox_hint") or {}) == bbox_hint
    assert str(refs[0].get("anchor_id") or "") == "ai-bbox-1"
    assert bool(((ensured.get("quality_report") or {}).get("grounded_evidence_enabled"))) is True


def test_ensure_payload_contract_should_preserve_partial_reconstructed_page_mode_and_component_source_modes(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(
        service,
        "_enforce_no_drop_blocks_fallback",
        lambda **_kwargs: {
            "triggered": False,
            "strategy": "",
            "error_code": "",
            "missing_block_ids": [],
            "inserted_node_ids": [],
        },
    )
    ensured = service._ensure_payload_contract(  # pylint: disable=protected-access
        page=14,
        payload={
            "paper_id": 86,
            "page": 14,
            "page_mode": "partial_reconstructed",
            "grounding_mode": "partial_reconstructed",
            "reconstruction_mode": "partial_reconstructed",
            "page_grounding_policy": {
                "mode": "partial_reconstructed",
                "page_mode": "partial_reconstructed",
                "reconstruction_mode": "partial_reconstructed",
                "evidence_enabled": False,
            },
            "ui_plan": {
                "plan_id": "partial_reconstructed_p14",
                "components": [
                    {
                        "id": "paragraph_1",
                        "type": "ParagraphProse",
                        "props": {"text": "Grounded paragraph.", "source_mode": "grounded"},
                        "children": [],
                        "source_block_ids": ["p14_b1"],
                        "source_anchor_refs": [],
                    },
                    {
                        "id": "figure_1",
                        "type": "FigurePanel",
                        "props": {
                            "caption": "Figure 4: Two attention heads.",
                            "image_url": "/api/v1/literature/reader/page-assets/86/14",
                            "source_label": "Figure 4",
                            "source_mode": "partial_reconstructed",
                            "partial_reconstruction": "crop the figure body only",
                        },
                        "children": [],
                        "source_block_ids": ["p14_b2"],
                        "source_anchor_refs": [],
                    },
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
            "page_grounding_v1": {
                "page_image": {"url": "/api/v1/literature/reader/grounding-page-assets/86/14", "path": ""},
                "layout_atoms": [
                    {"layout_id": "layout-1", "node_kind": "paragraph", "clean_text": "Grounded paragraph.", "normalized_text": "Grounded paragraph."},
                    {"layout_id": "layout-2", "node_kind": "figure", "clean_text": "Figure 4: Two attention heads.", "normalized_text": "Figure 4: Two attention heads."},
                ],
            },
        },
    )

    components = list(((ensured.get("ui_plan") or {}).get("components") or []))
    assert str(ensured.get("page_mode") or "") == "partial_reconstructed"
    assert str(((ensured.get("page_grounding_policy") or {}).get("mode") or "")) == "partial_reconstructed"
    assert str(((components[0] or {}).get("props") or {}).get("source_mode") or "") == "grounded"
    assert str(((components[1] or {}).get("props") or {}).get("source_mode") or "") == "partial_reconstructed"
    assert str(((components[1] or {}).get("props") or {}).get("partial_reconstruction") or "") == "crop the figure body only"


def test_ensure_payload_contract_should_restore_ai_evidence_from_old_ai_reconstructed_payload():
    service = LiteratureReaderComposeService()
    figure_bbox = {"x0": 0.12, "y0": 0.18, "x1": 0.88, "y1": 0.76}
    ensured = service._ensure_payload_contract(  # pylint: disable=protected-access
        page=14,
        payload={
            "paper_id": 86,
            "page": 14,
            "status": "done",
            "build_mode": "compose_ai_reconstructed",
            "grounding_mode": "ai_reconstructed",
            "evidence_enabled": False,
            "runtime_build_plan_evidence": False,
            "page_grounding_policy": {
                "mode": "ai_reconstructed",
                "reconstruction_mode": "ai_reconstructed",
                "evidence_enabled": False,
                "runtime_build_plan_evidence": False,
            },
            "layout_advice_v3": {
                "reconstruction": {
                    "mode": "ai_reconstructed",
                    "docmind_quality": "poor",
                    "reason": "DocMind figure evidence is unusable for this page.",
                    "confidence": 0.91,
                    "components": [
                        {
                            "kind": "figure",
                            "caption": "Figure 4: Two attention heads.",
                            "source_label": "Figure 4",
                            "bbox_norm": figure_bbox,
                        }
                    ],
                    "notes": ["old_ai_reconstructed_payload"],
                },
                "ai_reconstructed_figure_assets": json.dumps(
                    {
                        "1": {
                            "asset_id": "ai_reconstructed_figure_1",
                            "image_url": "/api/v1/literature/reader/figure-assets/86/14/ai_reconstructed_figure_1.png",
                            "bbox_norm": figure_bbox,
                            "verified": True,
                        }
                    },
                    ensure_ascii=False,
                ),
            },
            "pipeline_contract_meta": {
                "ai_reconstructed_figure_assets": json.dumps(
                    {
                        "1": {
                            "asset_id": "ai_reconstructed_figure_1",
                            "image_url": "/api/v1/literature/reader/figure-assets/86/14/ai_reconstructed_figure_1.png",
                            "bbox_norm": figure_bbox,
                            "verified": True,
                        }
                    },
                    ensure_ascii=False,
                ),
            },
            "quality_report": {
                "overall": 0.72,
                "stop_reason": "layout_uid_v1_ai_reconstructed",
                "validation_errors": [],
            },
            "page_grounding_v1": {
                "page_image": {"width": 1360, "height": 1760, "url": "/api/v1/literature/reader/page-assets/86/14"},
                "layout_atoms": [
                    {
                        "layout_id": "layout-figure-1",
                        "node_kind": "figure",
                        "clean_text": "Figure 4: Two attention heads.",
                        "normalized_text": "Figure 4: Two attention heads.",
                    }
                ],
            },
            "ui_plan": {
                "plan_id": "ai_reconstructed_p14",
                "components": [
                    {
                        "id": "figure_1",
                        "type": "FigurePanel",
                        "props": {
                            "caption": "Figure 4: Two attention heads.",
                            "image_url": "/api/v1/literature/reader/page-assets/86/14",
                            "source_label": "Figure 4",
                            "ai_insight": "",
                        },
                        "children": [],
                        "source_block_ids": [],
                        "source_anchor_refs": [],
                    }
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
        },
    )

    assert bool(ensured.get("evidence_enabled")) is True
    assert bool(ensured.get("runtime_build_plan_evidence")) is True
    assert bool(ensured.get("ai_reconstructed_evidence_enabled")) is True
    assert str(((ensured.get("page_grounding_policy") or {}).get("mode") or "")) == "ai_reconstructed"
    refs = list((((ensured.get("ui_plan") or {}).get("components") or [])[0].get("source_anchor_refs") or []))
    assert len(refs) == 1
    assert dict(refs[0].get("bbox_hint") or {}) == {
        "x0": 163.2,
        "x1": 1196.8,
        "top": 316.8,
        "bottom": 1337.6,
        "page_width": 1360,
        "page_height": 1760,
    }
    assert str(((ensured.get("ui_plan") or {}).get("components") or [])[0].get("props", {}).get("image_url") or "") == "/api/v1/literature/reader/page-assets/86/14"
    assert dict(((ensured.get("ui_plan") or {}).get("components") or [])[0].get("props") or {}).get("bbox_norm") == figure_bbox


def test_sanitize_ui_plan_for_runtime_should_keep_injected_ai_bbox_anchor_refs_on_ai_reconstructed_components():
    service = LiteratureReaderComposeService()
    bbox_hint = {"x0": 0.12, "x1": 0.88, "top": 0.22, "bottom": 0.44, "page_width": 1000, "page_height": 1200}
    geometry = {
        "page_width": 1000,
        "page_height": 1200,
        "polygons": [
            {
                "points": [
                    {"x": 120.0, "y": 220.0},
                    {"x": 880.0, "y": 220.0},
                    {"x": 880.0, "y": 440.0},
                    {"x": 120.0, "y": 440.0},
                ],
                "source": "ai_bbox_v1",
            }
        ],
    }
    payload = {
        "grounding_mode": "ai_reconstructed",
        "evidence_enabled": True,
        "page_grounding_policy": {"mode": "ai_reconstructed", "evidence_enabled": True},
        "page_grounding_v1": {
            "layout_atoms": [
                {
                    "layout_id": "layout-1",
                    "node_kind": "paragraph",
                    "clean_text": "Reconstructed summary of the page.",
                    "normalized_text": "Reconstructed summary of the page.",
                }
            ]
        },
    }
    ui_plan = {
        "plan_id": "ai_reconstructed_p14",
        "components": [
            {
                "id": "ai_reconstructed_1",
                "type": "ParagraphProse",
                "props": {"text": "Reconstructed summary of the page."},
                "children": [],
                "source_anchor_refs": [
                    {
                        "page": 14,
                        "start_char": 0,
                        "end_char": 32,
                        "quote_text": "Reconstructed summary of the page.",
                        "anchor_id": "ai-bbox-1",
                        "anchor_confidence": 0.96,
                        "bbox_hint": bbox_hint,
                        "geometry": geometry,
                        "coord_version": "ai_bbox_v1",
                    }
                ],
            }
        ],
    }

    sanitized = service._sanitize_ui_plan_for_runtime(  # pylint: disable=protected-access
        page=14,
        payload=payload,
        ui_plan=ui_plan,
    )
    refs = list((((sanitized.get("components") or [])[0]).get("source_anchor_refs") or []))
    assert len(refs) == 1
    assert dict(refs[0].get("bbox_hint") or {}) == bbox_hint
    assert dict(refs[0].get("geometry") or {}) == geometry
    assert str(refs[0].get("coord_version") or "") == "ai_bbox_v1"


def test_refresh_layout_uid_source_anchor_refs_should_keep_ai_bbox_anchors():
    service = LiteratureReaderComposeService()
    ai_anchor = {
        "page": 14,
        "start_char": 0,
        "end_char": 32,
        "quote_text": "Reconstructed summary of the page.",
        "anchor_id": "ai-bbox-1",
        "anchor_confidence": 0.94,
        "bbox_hint": {"x0": 0.1, "x1": 0.9, "top": 0.15, "bottom": 0.75, "page_width": 1000, "page_height": 1200},
        "coord_version": "ai_bbox_v1",
    }
    ui_plan = {
        "components": [
            {
                "id": "ai_reconstructed_1",
                "type": "ParagraphProse",
                "props": {"text": "Reconstructed summary of the page."},
                "children": [],
                "source_layout_ids": ["layout-1"],
                "source_anchor_refs": [ai_anchor],
            }
        ]
    }
    payload = {
        "page_grounding_v1": {
            "layout_atoms": [
                {
                    "layout_id": "layout-1",
                    "node_kind": "paragraph",
                    "clean_text": "Reconstructed summary of the page.",
                    "normalized_text": "Reconstructed summary of the page.",
                    "layout_pos": [{"x": 10.0, "y": 20.0}, {"x": 210.0, "y": 20.0}, {"x": 210.0, "y": 120.0}, {"x": 10.0, "y": 120.0}],
                }
            ]
        }
    }

    refreshed = service._refresh_layout_uid_source_anchor_refs(  # pylint: disable=protected-access
        page=14,
        payload=payload,
        ui_plan=ui_plan,
    )

    refs = list(((refreshed.get("components") or [])[0].get("source_anchor_refs") or []))
    assert len(refs) == 2
    assert any(str(ref.get("coord_version") or "") == "ai_bbox_v1" and dict(ref.get("bbox_hint") or {}) == ai_anchor["bbox_hint"] for ref in refs)
    assert any(str(ref.get("coord_version") or "") == "layout_uid_v1" for ref in refs)


def test_build_ai_reconstructed_panel_plan_should_create_ungrounded_reader_nodes():
    service = LiteratureReaderComposeService()
    panel_plan = service._build_ai_reconstructed_panel_plan(  # pylint: disable=protected-access
        page=14,
        reconstruction_plan={
            "mode": "ai_reconstructed",
            "notes": ["poor_docmind"],
            "components": [
                {"kind": "heading", "text": "Figure 4", "level": 2},
                {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
            ],
        },
        page_image_url="/api/v1/literature/reader/page-render-assets/86/14",
    )

    nodes = list(((panel_plan.get("panels") or [])[0].get("nodes") or []))
    assert len(nodes) == 3
    assert str((nodes[0] or {}).get("component") or "") == "SectionHeading"
    assert list((nodes[1] or {}).get("source_layout_ids") or []) == []
    assert str((nodes[1] or {}).get("component") or "") == "FigurePanel"
    assert str((((nodes[1] or {}).get("props") or {}).get("image_url") or "")) == "/api/v1/literature/reader/page-render-assets/86/14"
    assert str((((nodes[2] or {}).get("props") or {}).get("text") or "")) == "The visual compares multiple attention heads."


def test_build_ai_reconstructed_panel_plan_should_keep_figure_partial_reconstruction_notes():
    service = LiteratureReaderComposeService()
    panel_plan = service._build_ai_reconstructed_panel_plan(  # pylint: disable=protected-access
        page=14,
        reconstruction_plan={
            "mode": "ai_reconstructed",
            "notes": ["poor_docmind"],
            "components": [
                {"kind": "heading", "text": "Figure 4", "level": 2},
                {
                    "kind": "figure",
                    "caption": "Figure 4: Two attention heads.",
                    "source_label": "Figure 4",
                    "notes": ["keep the figure body and trim caption noise"],
                    "partial_reconstruction": "crop just the figure body and keep a short explanation",
                },
                {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
            ],
        },
        page_image_url="/api/v1/literature/reader/page-render-assets/86/14",
    )

    nodes = list(((panel_plan.get("panels") or [])[0].get("nodes") or []))
    figure_props = dict((nodes[1] or {}).get("props") or {})
    assert list(figure_props.get("reconstruction_notes") or []) == ["keep the figure body and trim caption noise"]
    assert str(figure_props.get("partial_reconstruction") or "") == "crop just the figure body and keep a short explanation"


def test_build_ai_reconstructed_panel_plan_should_prefer_verified_figure_assets_over_page_image():
    service = LiteratureReaderComposeService()
    panel_plan = service._build_ai_reconstructed_panel_plan(  # pylint: disable=protected-access
        page=14,
        reconstruction_plan={
            "mode": "ai_reconstructed",
            "notes": ["poor_docmind"],
            "components": [
                {"kind": "paragraph", "text": "Leading summary"},
                {
                    "kind": "figure",
                    "caption": "Figure 4: Two attention heads.",
                    "source_label": "Figure 4",
                    "visual_spec": {
                        "seed_bbox_norm": {"x0": 0.08, "y0": 0.16, "x1": 0.92, "y1": 0.74},
                        "must_include": ["attention heads"],
                        "must_exclude": ["header", "footer"],
                        "require_full_boundary": True,
                        "prefer_without_caption": False,
                        "allow_caption_if_needed": True,
                    },
                },
            ],
        },
        page_image_url="/api/v1/literature/reader/page-render-assets/86/14",
        figure_assets={
            2: {
                "image_url": "/api/v1/literature/reader/figure-assets/86/14/ai_reconstructed_figure_2.png",
                "asset_id": "ai_reconstructed_figure_2",
                "verified": True,
            }
        },
    )

    nodes = list(((panel_plan.get("panels") or [])[0].get("nodes") or []))
    assert len(nodes) == 2
    figure_node = dict(nodes[1] or {})
    assert str(figure_node.get("component") or "") == "FigurePanel"
    assert str(((figure_node.get("props") or {}).get("image_url") or "")) == "/api/v1/literature/reader/figure-assets/86/14/ai_reconstructed_figure_2.png"


def test_build_ai_reconstructed_panel_plan_should_fall_back_to_page_image_when_no_verified_figure_asset():
    service = LiteratureReaderComposeService()
    panel_plan = service._build_ai_reconstructed_panel_plan(  # pylint: disable=protected-access
        page=14,
        reconstruction_plan={
            "mode": "ai_reconstructed",
            "notes": ["poor_docmind"],
            "components": [
                {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"}
            ],
        },
        page_image_url="/api/v1/literature/reader/page-render-assets/86/14",
        figure_assets={},
    )

    nodes = list(((panel_plan.get("panels") or [])[0].get("nodes") or []))
    assert len(nodes) == 1
    figure_node = dict(nodes[0] or {})
    assert str(figure_node.get("component") or "") == "FigurePanel"
    assert str(((figure_node.get("props") or {}).get("image_url") or "")) == "/api/v1/literature/reader/page-render-assets/86/14"


def test_build_ai_reconstructed_panel_plan_should_mark_all_nodes_fully_reconstructed():
    service = LiteratureReaderComposeService()
    panel_plan = service._build_ai_reconstructed_panel_plan(  # pylint: disable=protected-access
        page=14,
        reconstruction_plan={
            "mode": "ai_reconstructed",
            "notes": ["poor_docmind"],
            "components": [
                {"kind": "heading", "text": "Figure 4", "level": 2},
                {"kind": "figure", "caption": "Figure 4: Two attention heads.", "source_label": "Figure 4"},
                {"kind": "paragraph", "text": "The visual compares multiple attention heads."},
            ],
        },
        page_image_url="/api/v1/literature/reader/page-render-assets/86/14",
        figure_assets={},
    )

    nodes = list(((panel_plan.get("panels") or [])[0].get("nodes") or []))
    assert len(nodes) == 3
    assert str((((nodes[0] or {}).get("props") or {}).get("source_mode") or "")) == "fully_reconstructed"
    assert str((((nodes[1] or {}).get("props") or {}).get("source_mode") or "")) == "fully_reconstructed"
    assert str((((nodes[2] or {}).get("props") or {}).get("source_mode") or "")) == "fully_reconstructed"


def test_ensure_payload_contract_should_skip_no_drop_for_ai_reconstructed_pages():
    service = LiteratureReaderComposeService()
    ensured = service._ensure_payload_contract(  # pylint: disable=protected-access
        page=14,
        payload={
            "paper_id": 86,
            "page": 14,
            "status": "done",
            "build_mode": "compose_ai_reconstructed",
            "grounding_mode": "ai_reconstructed",
            "evidence_enabled": False,
            "quality_report": {
                "overall": 0.74,
                "stop_reason": "layout_uid_v1_ai_reconstructed",
                "validation_errors": [],
            },
            "ui_plan": {
                "plan_id": "ai_reconstructed_p14",
                "components": [
                    {
                        "id": "p1",
                        "type": "ParagraphProse",
                        "props": {"text": "Reconstructed summary of the page."},
                        "children": [],
                        "source_block_ids": [],
                        "source_anchor_refs": [],
                    }
                ],
                "layout": {},
                "style_tokens": {},
                "trace_meta": {},
            },
            "page_structure_v3": {
                "block_groups": [
                    {
                        "block_id": "p14_dm_p14_l001_b001",
                        "layout_unique_id": "L1",
                        "kind": "paragraph",
                        "zone_type": "main_body",
                        "text": "Broken DocMind text",
                    }
                ]
            },
            "docmind_structure": {"layouts": [], "page_image_url": ""},
            "assets": [],
            "blocks": [],
        },
    )

    assert str(ensured.get("status") or "") == "done"
    assert str(ensured.get("degraded_reason") or "") == ""
    assert bool(((ensured.get("quality_report") or {}).get("grounded_evidence_enabled"))) is False


def test_normalize_layout_uid_group_plan_should_fallback_on_duplicate_or_missing_layout_ids():
    service = LiteratureReaderComposeService()
    grounding = {
        "layout_atoms": [
            {
                "layout_id": "L1",
                "reading_order": 1,
                "node_kind": "title",
                "clean_text": "Paper title",
                "include_in_main_flow": True,
            },
            {
                "layout_id": "L2",
                "reading_order": 2,
                "node_kind": "paragraph",
                "clean_text": "Body paragraph",
                "include_in_main_flow": True,
            },
        ]
    }

    plan, validation = service._normalize_layout_uid_group_plan(  # pylint: disable=protected-access
        grounding=grounding,
        step_result={
            "groups": [
                {
                    "group_id": "g1",
                    "group_kind": "title",
                    "source_layout_ids": ["L1", "L1"],
                }
            ],
            "omissions": [],
        },
    )

    assert bool(validation.get("passed")) is False
    assert bool(validation.get("fallback_used")) is True
    assert any(str(item).startswith("duplicate_layout_id:") for item in list(validation.get("errors") or []))
    assert any(str(item).startswith("missing_layout_id:") for item in list(validation.get("errors") or []))
    assert len(list(plan.get("groups") or [])) == 2


def test_build_layout_uid_fallback_group_plan_should_merge_figure_with_adjacent_caption():
    service = LiteratureReaderComposeService()
    plan = service._build_layout_uid_fallback_group_plan(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "F1",
                    "reading_order": 1,
                    "node_kind": "figure",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "C1",
                    "reading_order": 2,
                    "node_kind": "figure_caption",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "P1",
                    "reading_order": 3,
                    "node_kind": "paragraph",
                    "include_in_main_flow": True,
                },
            ]
        },
    )

    groups = list(plan.get("groups") or [])
    assert groups[0]["group_kind"] == "figure"
    assert groups[0]["source_layout_ids"] == ["F1", "C1"]
    assert groups[1]["group_kind"] == "paragraph"


def test_classify_grounding_node_kind_should_detect_table_caption_and_equation():
    service = LiteratureReaderComposeService()

    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="text",
        layout_sub_type="para",
        text="Table 2. Quantization comparison across evaluation suites",
        block_rows=[{"kind": "table_caption", "zone_type": "main_body"}],
    ) == "table_caption"
    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="text",
        layout_sub_type="none",
        text="y = mx^2 + b",
        block_rows=[{"kind": "paragraph", "zone_type": "main_body"}],
    ) == "equation"


def test_classify_grounding_node_kind_should_not_treat_quantization_config_lists_as_equations():
    service = LiteratureReaderComposeService()

    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="text",
        layout_sub_type="para",
        text="1. llama.cpp6 for 4-bit(Q4K_M), 3-bit(Q3_K_M), 2-bit (Q2_K), and 8-bit (Q8_0) configurations",
        block_rows=[{"kind": "paragraph", "zone_type": "main_body"}],
    ) == "paragraph"
    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="text",
        layout_sub_type="para",
        text="·DeepSeek-R1 2-bit: Large-scale UD-Q2_K_XL (unsloth)",
        block_rows=[{"kind": "paragraph", "zone_type": "main_body"}],
    ) == "paragraph"


def test_classify_grounding_node_kind_should_treat_footnote_like_layouts_as_footer():
    service = LiteratureReaderComposeService()

    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="text",
        layout_sub_type="footnote",
        text="6 https://github.com/ggml-org/llama.cpp",
        block_rows=[{"kind": "paragraph", "zone_type": "main_body"}],
    ) == "footer"
    assert service._classify_grounding_node_kind(  # pylint: disable=protected-access
        layout_type="corner_note",
        layout_sub_type="none",
        text="9 https://cloud.tencent.com/document/product/1772/115963",
        block_rows=[{"kind": "paragraph", "zone_type": "side_context"}],
    ) == "footer"


def test_build_layout_uid_fallback_group_plan_should_merge_table_with_adjacent_caption():
    service = LiteratureReaderComposeService()
    plan = service._build_layout_uid_fallback_group_plan(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "T1",
                    "reading_order": 1,
                    "node_kind": "table",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "TC1",
                    "reading_order": 2,
                    "node_kind": "table_caption",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "P1",
                    "reading_order": 3,
                    "node_kind": "paragraph",
                    "include_in_main_flow": True,
                },
            ]
        },
    )

    groups = list(plan.get("groups") or [])
    assert groups[0]["group_kind"] == "table"
    assert groups[0]["source_layout_ids"] == ["T1", "TC1"]
    assert groups[1]["group_kind"] == "paragraph"


def test_layout_uid_group_plan_to_panel_plan_should_materialize_table_and_equation():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=7,
        grouping_plan={
            "groups": [
                {
                    "group_id": "table_group_1",
                    "group_kind": "table",
                    "source_layout_ids": ["table_body_1", "table_caption_1"],
                    "rationale": "test_table_bundle",
                },
                {
                    "group_id": "equation_group_1",
                    "group_kind": "equation",
                    "source_layout_ids": ["equation_1"],
                    "rationale": "test_equation_bundle",
                },
            ],
            "omissions": [],
            "notes": [],
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "table_body_1",
                    "node_kind": "table",
                    "clean_text": "Model Score",
                    "raw_text": "Model Score",
                    "canonical_block_ids": ["p7_dm_p7_l003_b001"],
                    "table_cells": [
                        {"cell_id": 0, "row_start": 0, "row_end": 0, "col_start": 0, "col_end": 0, "text": "Model", "layout_ids": ["table_r0c0"], "polygons": [[{"x": 100, "y": 100}, {"x": 220, "y": 100}, {"x": 220, "y": 124}, {"x": 100, "y": 124}]]},
                        {"cell_id": 1, "row_start": 0, "row_end": 0, "col_start": 1, "col_end": 1, "text": "Score", "layout_ids": ["table_r0c1"], "polygons": [[{"x": 280, "y": 100}, {"x": 390, "y": 100}, {"x": 390, "y": 124}, {"x": 280, "y": 124}]]},
                        {"cell_id": 2, "row_start": 1, "row_end": 1, "col_start": 0, "col_end": 0, "text": "Q8_0", "layout_ids": ["table_r1c0"], "polygons": [[{"x": 100, "y": 136}, {"x": 220, "y": 136}, {"x": 220, "y": 160}, {"x": 100, "y": 160}]]},
                        {"cell_id": 3, "row_start": 1, "row_end": 1, "col_start": 1, "col_end": 1, "text": "71.68", "layout_ids": ["table_r1c1"], "polygons": [[{"x": 280, "y": 136}, {"x": 390, "y": 136}, {"x": 390, "y": 160}, {"x": 280, "y": 160}]]},
                        {"cell_id": 4, "row_start": 2, "row_end": 2, "col_start": 0, "col_end": 0, "text": "Q4KM", "layout_ids": ["table_r2c0"], "polygons": [[{"x": 100, "y": 168}, {"x": 220, "y": 168}, {"x": 220, "y": 192}, {"x": 100, "y": 192}]]},
                        {"cell_id": 5, "row_start": 2, "row_end": 2, "col_start": 1, "col_end": 1, "text": "71.24", "layout_ids": ["table_r2c1"], "polygons": [[{"x": 280, "y": 168}, {"x": 390, "y": 168}, {"x": 390, "y": 192}, {"x": 280, "y": 192}]]},
                    ],
                    "blocks": [
                        {"block_index": 1, "text": "Model", "pos": [{"x": 100, "y": 100}, {"x": 220, "y": 100}, {"x": 220, "y": 124}, {"x": 100, "y": 124}]},
                        {"block_index": 2, "text": "Score", "pos": [{"x": 280, "y": 100}, {"x": 390, "y": 100}, {"x": 390, "y": 124}, {"x": 280, "y": 124}]},
                        {"block_index": 3, "text": "Q8_0", "pos": [{"x": 100, "y": 136}, {"x": 220, "y": 136}, {"x": 220, "y": 160}, {"x": 100, "y": 160}]},
                        {"block_index": 4, "text": "71.68", "pos": [{"x": 280, "y": 136}, {"x": 390, "y": 136}, {"x": 390, "y": 160}, {"x": 280, "y": 160}]},
                        {"block_index": 5, "text": "Q4KM", "pos": [{"x": 100, "y": 168}, {"x": 220, "y": 168}, {"x": 220, "y": 192}, {"x": 100, "y": 192}]},
                        {"block_index": 6, "text": "71.24", "pos": [{"x": 280, "y": 168}, {"x": 390, "y": 168}, {"x": 390, "y": 192}, {"x": 280, "y": 192}]},
                    ],
                },
                {
                    "layout_id": "table_caption_1",
                    "node_kind": "table_caption",
                    "clean_text": "Table 2. Quantization comparison across evaluation suites",
                    "raw_text": "Table 2. Quantization comparison across evaluation suites",
                    "blocks": [
                        {"block_index": 1, "text": "Table 2. Quantization comparison across evaluation suites", "pos": [{"x": 100, "y": 205}, {"x": 520, "y": 205}, {"x": 520, "y": 228}, {"x": 100, "y": 228}]}
                    ],
                },
                {
                    "layout_id": "equation_1",
                    "node_kind": "equation",
                    "clean_text": "y = mx^2 + b",
                    "raw_text": "y = mx^2 + b",
                    "blocks": [
                        {"block_index": 1, "text": "y = mx^2 + b", "pos": [{"x": 100, "y": 260}, {"x": 340, "y": 260}, {"x": 340, "y": 284}, {"x": 100, "y": 284}]}
                    ],
                },
            ]
        },
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    assert len(nodes) == 2
    table_node = dict(nodes[0] or {})
    equation_node = dict(nodes[1] or {})

    assert str(table_node.get("component") or "") == "TablePanel"
    table_props = dict(table_node.get("props") or {})
    assert str(table_props.get("title") or "") == "Table 2. Quantization comparison across evaluation suites"
    assert int(table_props.get("header_row_count") or 0) == 1
    assert list(table_props.get("headers") or []) == ["Model", "Score"]
    column_widths = list(table_props.get("column_widths") or [])
    assert len(column_widths) == 2
    assert abs(sum(float(item or 0.0) for item in column_widths) - 1.0) < 1e-6
    assert list(table_props.get("matrix") or [])[0] == ["Model", "Score"]
    assert list(table_props.get("matrix") or [])[1] == ["Q8_0", "71.68"]
    assert len(list(table_props.get("table_cells") or [])) == 6
    assert float((table_props.get("table_cells") or [])[0].get("x0") or 0.0) == 100.0
    assert float((table_props.get("table_cells") or [])[1].get("x1") or 0.0) == 390.0
    assert list(table_props.get("rows") or [])[0]["col_1"] == "Q8_0"
    assert str(table_props.get("caption") or "") == "Table 2. Quantization comparison across evaluation suites"
    row_evidence = list(table_props.get("row_evidence") or [])
    cell_evidence = list(table_props.get("cell_evidence") or [])
    assert len(row_evidence) == 3
    assert len(cell_evidence) == 6
    assert str((((row_evidence[1] or {}).get("anchor") or {}).get("anchor_id") or "")) == "layout_uid_v1:table_body_1:row:2"
    assert (((row_evidence[1] or {}).get("anchor") or {}).get("geometry") or {}).get("page_width") is None
    assert (((row_evidence[1] or {}).get("anchor") or {}).get("bbox_hint") or {}).get("page_width") is None
    assert str((((cell_evidence[1] or {}).get("anchor") or {}).get("source_layout_id") or "")) == "table_r0c1"
    assert list(table_node.get("source_layout_ids") or []) == ["table_body_1", "table_caption_1"]

    assert str(equation_node.get("component") or "") == "EquationBlock"
    equation_props = dict(equation_node.get("props") or {})
    assert str(equation_props.get("latex") or "") == "y = mx^2 + b"
    assert str(equation_props.get("render_mode") or "") == "image_first"
    assert str(equation_props.get("transcript") or "") == "y = mx^2 + b"


def test_layout_uid_group_plan_to_panel_plan_should_keep_standalone_table_caption_renderable():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=8,
        grouping_plan={
            "groups": [
                {
                    "group_id": "paragraph_1",
                    "group_kind": "paragraph",
                    "source_layout_ids": ["P1"],
                },
                {
                    "group_id": "table_caption_1",
                    "group_kind": "table_caption",
                    "source_layout_ids": ["TC1"],
                },
            ],
            "omissions": [],
            "notes": [],
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "P1",
                    "node_kind": "paragraph",
                    "clean_text": "Quantization improves throughput under fixed memory budgets.",
                    "raw_text": "Quantization improves throughput under fixed memory budgets.",
                },
                {
                    "layout_id": "TC1",
                    "node_kind": "table_caption",
                    "clean_text": "Table 2 demonstrates the impact of various quantization methods on DeepSeek-R1's performance.",
                    "raw_text": "Table 2 demonstrates the impact of various quantization methods on DeepSeek-R1's performance.",
                },
            ]
        },
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    assert len(nodes) == 2
    caption_node = dict(nodes[1] or {})
    caption_props = dict(caption_node.get("props") or {})
    assert str(caption_node.get("component") or "") == "ParagraphProse"
    assert list(caption_node.get("source_layout_ids") or []) == ["TC1"]
    assert (
        str(caption_props.get("text") or "")
        == "Table 2 demonstrates the impact of various quantization methods on DeepSeek-R1's performance."
    )


def test_layout_uid_group_plan_to_panel_plan_should_preserve_model_paragraphs():
    service = LiteratureReaderComposeService()
    grounding = {
        "layout_atoms": [
            {
                "layout_id": "P1",
                "node_kind": "paragraph",
                "clean_text": "MAA. American invitational mathematics examination - aime.",
                "raw_text": "MAA. American invitational mathematics examination - aime.",
            },
            {
                "layout_id": "P2",
                "node_kind": "paragraph",
                "clean_text": "In American Invitational Mathematics Examination - AIME 2024, February 2024.",
                "raw_text": "In American Invitational Mathematics Examination - AIME 2024, February 2024.",
            },
            {
                "layout_id": "P3",
                "node_kind": "paragraph",
                "clean_text": "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
                "raw_text": "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
            },
        ]
    }
    grouping_plan, validation = service._normalize_layout_uid_group_plan(  # pylint: disable=protected-access
        grounding=grounding,
        step_result={
            "groups": [
                {
                    "group_id": "g1",
                    "group_kind": "paragraph",
                    "source_layout_ids": ["P1", "P2", "P3"],
                    "paragraphs": [
                        {"text": "MAA. American invitational mathematics examination - aime.", "source_layout_ids": ["P1"]},
                        {"text": "In American Invitational Mathematics Examination - AIME 2024, February 2024.", "source_layout_ids": ["P2"]},
                        {
                            "text": "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
                            "source_layout_ids": ["P3"],
                        },
                    ],
                }
            ],
            "omissions": [],
            "notes": [],
        },
    )

    assert validation["passed"] is True
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=12,
        grouping_plan=grouping_plan,
        grounding=grounding,
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    prose_node = dict(nodes[0] or {})
    prose_props = dict(prose_node.get("props") or {})
    assert str(prose_node.get("component") or "") == "ParagraphProse"
    assert str(prose_props.get("paragraph_strategy") or "") == "model"
    assert [str((row or {}).get("text") or "") for row in list(prose_props.get("paragraphs") or [])] == [
        "MAA. American invitational mathematics examination - aime.",
        "In American Invitational Mathematics Examination - AIME 2024, February 2024.",
        "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
    ]


def test_sanitize_components_for_runtime_should_preserve_model_paragraphs_without_reinference():
    service = LiteratureReaderComposeService()
    sanitized = service._sanitize_components_for_runtime(  # pylint: disable=protected-access
        page=12,
        payload={
            "page_structure_v3": {
                "block_groups": [
                    {
                        "block_id": "dm_p12_l006_b001",
                        "layout_unique_id": "L1",
                        "text": "MAA.",
                        "layout_bbox_or_polygon": {"bbox": {"x0": 100, "x1": 150, "top": 100, "bottom": 120}},
                    },
                    {
                        "block_id": "dm_p12_l006_b002",
                        "layout_unique_id": "L1",
                        "text": "American invitational mathematics examination - aime.",
                        "layout_bbox_or_polygon": {"bbox": {"x0": 160, "x1": 500, "top": 100, "bottom": 120}},
                    },
                    {
                        "block_id": "dm_p12_l006_b003",
                        "layout_unique_id": "L1",
                        "text": "In American Invitational Mathematics Examination - AIME 2024, February 2024.",
                        "layout_bbox_or_polygon": {"bbox": {"x0": 100, "x1": 520, "top": 130, "bottom": 150}},
                    },
                    {
                        "block_id": "dm_p12_l006_b004",
                        "layout_unique_id": "L1",
                        "text": "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
                        "layout_bbox_or_polygon": {"bbox": {"x0": 100, "x1": 560, "top": 160, "bottom": 180}},
                    },
                ]
            }
        },
        nodes=[
            {
                "id": "n1",
                "type": "ParagraphProse",
                "props": {
                    "text": "stale",
                    "paragraph_strategy": "model",
                    "paragraphs": [
                        {"text": "MAA. American invitational mathematics examination - aime.", "source_layout_ids": ["L1"]},
                        {"text": "In American Invitational Mathematics Examination - AIME 2024, February 2024.", "source_layout_ids": ["L1"]},
                        {
                            "text": "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
                            "source_layout_ids": ["L1"],
                        },
                    ],
                },
                "children": [],
                "source_anchor_refs": [],
                "source_block_ids": [
                    "p12_dm_p12_l006_b001",
                    "p12_dm_p12_l006_b002",
                    "p12_dm_p12_l006_b003",
                    "p12_dm_p12_l006_b004",
                ],
            }
        ],
    )

    prose_node = dict(sanitized[0] or {})
    prose_props = dict(prose_node.get("props") or {})
    assert [str((row or {}).get("text") or "") for row in list(prose_props.get("paragraphs") or [])] == [
        "MAA. American invitational mathematics examination - aime.",
        "In American Invitational Mathematics Examination - AIME 2024, February 2024.",
        "4. URL https://maa.org/math-competitions/american-invitational-mathematics-examination-aime",
    ]
    assert "stale" not in str(prose_props.get("text") or "")


def test_build_layout_uid_equation_props_should_split_where_clause_into_description():
    service = LiteratureReaderComposeService()
    props = service._build_layout_uid_equation_props(  # pylint: disable=protected-access
        atoms=[
            {
                "layout_id": "eq1",
                "clean_text": r"Eq. (1) \min_x D_{\mathrm{calib}}(x) - f_{\mathrm{quant}}(x) where D_{\mathrm{calib}} denotes the calibration dataset",
            }
        ]
    )

    assert str(props.get("label") or "") == "Eq. (1)"
    assert str(props.get("latex") or "") == r"\min_x D_{\mathrm{calib}}(x) - f_{\mathrm{quant}}(x)"
    assert str(props.get("description") or "") == "where D_{\\mathrm{calib}} denotes the calibration dataset"
    assert str(props.get("render_mode") or "") == "image_first"
    assert str(props.get("transcript") or "") == r"Eq. (1) \min_x D_{\mathrm{calib}}(x) - f_{\mathrm{quant}}(x) where D_{\mathrm{calib}} denotes the calibration dataset"


def test_build_layout_uid_equation_props_should_include_ai_normalization_fields():
    service = LiteratureReaderComposeService()
    props = service._build_layout_uid_equation_props(  # pylint: disable=protected-access
        atoms=[
            {
                "layout_id": "eq1",
                "clean_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                "raw_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
            }
        ],
        equation_refinement={
            "normalized_text": "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||",
            "normalized_latex": r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert",
            "reason": "Recovered theta and calibration subscript from the page image and style hints.",
            "confidence": 0.84,
            "mode": "latex_reconstructed",
        },
    )

    assert str(props.get("render_mode") or "") == "math_first"
    assert str(props.get("normalized_text") or "") == "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||"
    assert str(props.get("normalized_latex") or "") == r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert"
    assert str(props.get("normalization_reason") or "") == "Recovered theta and calibration subscript from the page image and style hints."
    assert float(props.get("normalization_confidence") or 0.0) == 0.84
    assert str(props.get("normalization_mode") or "") == "latex_reconstructed"


def test_build_layout_uid_equation_props_should_extract_label_from_standalone_block():
    service = LiteratureReaderComposeService()
    props = service._build_layout_uid_equation_props(  # pylint: disable=protected-access
        atoms=[
            {
                "layout_id": "eq1",
                "clean_text": "QKT Attention(Q,K,V)=softmax( 一) (1) √dk",
                "raw_text": "QKT Attention(Q,K,V)=softmax( 一) (1) √dk",
                "blocks": [
                    {"text": "QKT"},
                    {"text": "Attention(Q,K,V)=softmax("},
                    {"text": "一)"},
                    {"text": "(1)"},
                    {"text": "√dk"},
                ],
            }
        ],
        equation_refinement={
            "normalized_text": "Attention(Q, K, V) = softmax(QK^T / √d_k)V",
            "normalized_latex": r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
            "reason": "Recovered the standard scaled dot-product attention equation from the page image.",
            "confidence": 0.95,
            "mode": "latex_reconstructed",
        },
    )

    assert str(props.get("label") or "") == "(1)"
    assert str(props.get("render_mode") or "") == "math_first"


def test_build_layout_uid_equation_props_should_fallback_to_image_first_without_normalized_latex():
    service = LiteratureReaderComposeService()
    props = service._build_layout_uid_equation_props(  # pylint: disable=protected-access
        atoms=[
            {
                "layout_id": "eq1",
                "clean_text": "x = y",
                "raw_text": "x = y",
            }
        ],
        equation_refinement={
            "normalized_text": "x = y",
            "reason": "transcript_cleanup",
            "confidence": 0.61,
            "mode": "display_normalized",
        },
    )

    assert str(props.get("render_mode") or "") == "image_first"
    assert str(props.get("normalized_latex") or "") == ""


def test_normalize_layout_uid_figure_refinement_should_accept_short_insight():
    service = LiteratureReaderComposeService()
    normalized, validation = service._normalize_layout_uid_figure_refinement(  # pylint: disable=protected-access
        atoms=[
            {
                "layout_id": "fig_1",
                "node_kind": "figure",
                "clean_text": "",
                "raw_text": "",
            },
            {
                "layout_id": "fig_caption_1",
                "node_kind": "figure_caption",
                "clean_text": "Figure 2: Multi-head attention consists of several attention layers running in parallel.",
                "raw_text": "Figure 2: Multi-head attention consists of several attention layers running in parallel.",
            },
        ],
        step_result={
            "insight": "左图给出缩放点积注意力流程，右图展示多头注意力把多个注意力分支并行组合。",
            "reason": "image_grounded_summary",
            "confidence": 0.91,
            "mode": "image_grounded_summary",
        },
    )

    assert validation.get("passed") is True
    assert str(normalized.get("ai_insight") or "") == "左图给出缩放点积注意力流程，右图展示多头注意力把多个注意力分支并行组合。"
    assert float(normalized.get("confidence") or 0.0) == 0.91


def test_layout_uid_group_plan_to_panel_plan_should_apply_figure_insight():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=4,
        grouping_plan={
            "groups": [
                {
                    "group_id": "figure_group_1",
                    "group_kind": "figure",
                    "source_layout_ids": ["figure_1", "figure_caption_1"],
                    "rationale": "test_figure_bundle",
                },
            ],
            "omissions": [],
            "notes": [],
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "figure_1",
                    "node_kind": "figure",
                    "clean_text": "",
                    "raw_text": "",
                    "layout_type": "figure",
                    "layout_sub_type": "diagram",
                },
                {
                    "layout_id": "figure_caption_1",
                    "node_kind": "figure_caption",
                    "clean_text": "Figure 2: Multi-head attention consists of several attention layers running in parallel.",
                    "raw_text": "Figure 2: Multi-head attention consists of several attention layers running in parallel.",
                    "layout_type": "text",
                    "layout_sub_type": "caption",
                },
            ]
        },
        figure_refinements={
            "figure_group_1": {
                "insight": {
                    "ai_insight": "右图强调多个注意力分支并行计算，再将结果拼接回统一输出。",
                    "reason": "image_grounded_summary",
                    "confidence": 0.88,
                    "mode": "image_grounded_summary",
                }
            }
        },
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    assert len(nodes) == 1
    figure_props = dict((nodes[0] or {}).get("props") or {})
    assert str(figure_props.get("source_label") or "") == "Figure 2"
    assert str(figure_props.get("ai_insight") or "") == "右图强调多个注意力分支并行计算，再将结果拼接回统一输出。"


def test_build_regenerated_node_should_not_inject_template_figure_insight():
    service = LiteratureReaderComposeService()
    regenerated = service._build_regenerated_node(  # pylint: disable=protected-access
        node_before={
            "id": "fig_1",
            "type": "FigurePanel",
            "props": {
                "caption": "Figure 2: Multi-head attention consists of several attention layers running in parallel.",
                "ai_insight": "",
            },
        }
    )

    props = dict(regenerated.get("props") or {})
    assert str(props.get("ai_insight") or "") == ""


def test_select_native_pdf_image_should_reject_shape_mismatch():
    service = LiteratureReaderComposeService()
    portrait = SimpleNamespace(image=SimpleNamespace(size=(405, 568)))
    result = service._select_native_pdf_image(  # pylint: disable=protected-access
        pypdf_images=[portrait],
        pypdf_image_map={},
        candidate_images=[],
        bbox_pdf=(390.0, 171.0, 1120.0, 580.0),
    )
    assert result is None


def test_select_native_pdf_image_should_accept_shape_match():
    service = LiteratureReaderComposeService()
    landscape = SimpleNamespace(image=SimpleNamespace(size=(730, 410)))
    result = service._select_native_pdf_image(  # pylint: disable=protected-access
        pypdf_images=[landscape],
        pypdf_image_map={},
        candidate_images=[],
        bbox_pdf=(390.0, 171.0, 1120.0, 580.0),
    )
    assert result is landscape


def test_write_composited_pdf_images_should_place_multiple_native_images(tmp_path):
    service = LiteratureReaderComposeService()
    left = SimpleNamespace(image=Image.new("RGB", (20, 40), "red"))
    right = SimpleNamespace(image=Image.new("RGB", (40, 60), "blue"))
    target = service._write_composited_pdf_images(  # pylint: disable=protected-access
        out_dir=str(tmp_path),
        asset_id="fig1",
        page_images=[
            {"x0": 10.0, "x1": 30.0, "top": 20.0, "bottom": 60.0},
            {"x0": 40.0, "x1": 80.0, "top": 10.0, "bottom": 70.0},
        ],
        pypdf_images=[left, right],
        page_render_size=(100, 100),
        pdf_width=100.0,
        pdf_height=100.0,
    )
    assert target is not None
    image = Image.open(target)
    assert image.size == (70, 60)


def test_layout_uid_group_plan_to_panel_plan_should_apply_equation_normalization():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=3,
        grouping_plan={
            "groups": [
                {
                    "group_id": "equation_group_1",
                    "group_kind": "equation",
                    "source_layout_ids": ["equation_1"],
                    "rationale": "test_equation_bundle",
                },
            ],
            "omissions": [],
            "notes": [],
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "equation_1",
                    "node_kind": "equation",
                    "clean_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                    "raw_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                    "alignment": "center",
                    "line_height": 9.0,
                    "blocks": [
                        {
                            "block_index": 1,
                            "text": "minEx~DcaliblfFp(x)-fquant(0x)||,",
                            "style_id": 31,
                            "pos": [{"x": 100, "y": 260}, {"x": 340, "y": 260}, {"x": 340, "y": 284}, {"x": 100, "y": 284}],
                        }
                    ],
                },
            ]
        },
        equation_refinements={
            "equation_group_1": {
                "normalization": {
                    "normalized_text": "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||",
                    "normalized_latex": r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert",
                    "reason": "Recovered theta and calibration subscript from the page image and style hints.",
                    "confidence": 0.84,
                    "mode": "latex_reconstructed",
                }
            }
        },
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    assert len(nodes) == 1
    equation_props = dict((nodes[0] or {}).get("props") or {})
    assert str(equation_props.get("normalized_text") or "") == "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||"
    assert str(equation_props.get("normalized_latex") or "") == r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert"
    assert str(equation_props.get("normalization_mode") or "") == "latex_reconstructed"


def test_normalize_layout_uid_text_normalization_plan_should_require_exact_once_coverage():
    service = LiteratureReaderComposeService()
    normalized, validation = service._normalize_layout_uid_text_normalization_plan(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "L1",
                    "node_kind": "paragraph",
                    "clean_text": "A p p l e",
                    "raw_text": "A p p l e",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "L2",
                    "node_kind": "section_heading",
                    "clean_text": "Resu lts",
                    "raw_text": "Resu lts",
                    "include_in_main_flow": True,
                },
            ]
        },
        step_result={
            "items": [
                {
                    "layout_id": "L1",
                    "normalized_text": "Apple",
                    "reason": "spacing_repair",
                    "confidence": 0.93,
                    "mode": "spacing_repair",
                }
            ],
            "notes": ["missing_one_layout"],
        },
    )

    assert bool(validation.get("passed")) is False
    assert bool(validation.get("fallback_used")) is True
    assert "missing_layout_id:L2" in list(validation.get("errors") or [])
    assert list(normalized.get("items") or []) == []


def test_build_layout_uid_text_normalization_prompt_payload_should_include_hidden_footer_like_kinds():
    service = LiteratureReaderComposeService()
    prompt_payload = service._build_layout_uid_text_normalization_prompt_payload(  # pylint: disable=protected-access
        page=8,
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "p8_title",
                    "reading_order": 1,
                    "node_kind": "title",
                    "clean_text": "4.2 Experimental Setting",
                    "raw_text": "4.2 Experimental Setting",
                    "include_in_main_flow": True,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 16, "text": "4.2 Experimental Setting"}],
                },
                {
                    "layout_id": "p8_footer_link",
                    "reading_order": 20,
                    "node_kind": "footer",
                    "clean_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "raw_text": "1. llama.cpp6 for 4-bit(Q4K_M)",
                    "include_in_main_flow": False,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 8, "text": "1. llama.cpp6 for 4-bit(Q4K_M)"}],
                },
                {
                    "layout_id": "p8_noise",
                    "reading_order": 21,
                    "node_kind": "noise",
                    "clean_text": "Random separator",
                    "raw_text": "Random separator",
                    "include_in_main_flow": False,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 0, "text": "Random separator"}],
                },
            ]
        },
    )

    items = list(prompt_payload.get("layout_atoms") or [])
    item_ids = [str(item.get("layout_id") or "") for item in items]
    assert "p8_title" in item_ids
    assert "p8_footer_link" in item_ids
    assert "p8_noise" not in item_ids
    footer_bundles = list(prompt_payload.get("footer_bundles") or [])
    assert len(footer_bundles) == 1
    assert str((((footer_bundles[0] or {}).get("items") or [])[0] or {}).get("layout_id") or "") == "p8_footer_link"


def test_build_layout_uid_text_normalization_prompt_payload_should_include_footer_bundle_context():
    service = LiteratureReaderComposeService()
    prompt_payload = service._build_layout_uid_text_normalization_prompt_payload(  # pylint: disable=protected-access
        page=8,
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "f8_marker",
                    "reading_order": 40,
                    "node_kind": "footer",
                    "layout_type": "corner_note",
                    "layout_sub_type": "footer_note",
                    "clean_text": "8",
                    "raw_text": "8",
                    "include_in_main_flow": False,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 15, "text": "8"}],
                },
                {
                    "layout_id": "f8_url",
                    "reading_order": 41,
                    "node_kind": "footer",
                    "layout_type": "corner_note",
                    "layout_sub_type": "footer_note",
                    "clean_text": "Shttps://api-docs.deepseek.com/",
                    "raw_text": "Shttps://api-docs.deepseek.com/",
                    "include_in_main_flow": False,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 6, "text": "Shttps://api-docs.deepseek.com/"}],
                },
                {
                    "layout_id": "f9_url",
                    "reading_order": 42,
                    "node_kind": "footer",
                    "layout_type": "corner_note",
                    "layout_sub_type": "footer_note",
                    "clean_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                    "raw_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                    "include_in_main_flow": False,
                    "alignment": "left",
                    "line_height": 0,
                    "blocks": [{"style_id": 15, "text": "Yhttps://cloud.tencent.com/document/product/1772/115963"}],
                },
            ]
        },
    )

    footer_bundles = list(prompt_payload.get("footer_bundles") or [])
    assert len(footer_bundles) == 1
    bundle = dict(footer_bundles[0] or {})
    bundle_items = list(bundle.get("items") or [])
    assert [str(item.get("layout_id") or "") for item in bundle_items] == ["f8_marker", "f8_url", "f9_url"]
    assert bool((bundle_items[0] or {}).get("is_marker_only")) is True
    assert bool((bundle_items[1] or {}).get("contains_url")) is True
    assert int((bundle_items[1] or {}).get("primary_style_id") or 0) == 6


def test_build_layout_uid_combined_prompt_payload_should_union_grouping_and_normalization_fields():
    service = LiteratureReaderComposeService()
    paper = SimpleNamespace(id=85, title="demo")
    prompt_payload = service._build_layout_uid_combined_prompt_payload(  # pylint: disable=protected-access
        paper=paper,
        page=8,
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "p8_para",
                    "reading_order": 1,
                    "node_kind": "paragraph",
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "clean_text": "A p p l e",
                    "raw_text": "A p p l e",
                    "include_in_main_flow": True,
                    "region_hint": "main_body",
                    "layout_pos": [{"x": 1, "y": 2}],
                    "alignment": "left",
                    "line_height": 12,
                    "blocks": [{"style_id": 7, "text": "A p p l e"}],
                },
                {
                    "layout_id": "p8_footer",
                    "reading_order": 2,
                    "node_kind": "footer",
                    "layout_type": "corner_note",
                    "layout_sub_type": "footer_note",
                    "clean_text": "Shttps://api-docs.deepseek.com/",
                    "raw_text": "Shttps://api-docs.deepseek.com/",
                    "include_in_main_flow": False,
                    "region_hint": "side_context",
                    "layout_pos": [{"x": 3, "y": 4}],
                    "alignment": "left",
                    "line_height": 10,
                    "blocks": [{"style_id": 9, "text": "Shttps://api-docs.deepseek.com/"}],
                },
            ]
        },
    )

    atoms = list(prompt_payload.get("layout_atoms") or [])
    assert len(atoms) == 2
    first = dict(atoms[0] or {})
    second = dict(atoms[1] or {})
    assert str(first.get("source_text") or "") == "A p p l e"
    assert str(first.get("text") or "") == "A p p l e"
    assert bool(first.get("include_in_main_flow")) is True
    assert str(first.get("region_hint") or "") == "main_body"
    assert int(first.get("block_count") or 0) == 1
    assert str(second.get("node_kind") or "") == "footer"
    assert str((second.get("footer_bundle") or {}).get("bundle_id") or "") == "footer_bundle_1"
    rules = dict(prompt_payload.get("rules") or {})
    assert str(rules.get("text_item_mode") or "") == "sparse_diff_only"
    assert bool(rules.get("exact_group_assignment")) is True


def test_normalize_layout_uid_sparse_text_normalization_plan_should_accept_changed_subset_only():
    service = LiteratureReaderComposeService()
    normalized, validation = service._normalize_layout_uid_sparse_text_normalization_plan(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "L1",
                    "node_kind": "paragraph",
                    "clean_text": "A p p l e",
                    "raw_text": "A p p l e",
                    "include_in_main_flow": True,
                },
                {
                    "layout_id": "L2",
                    "node_kind": "paragraph",
                    "clean_text": "Banana",
                    "raw_text": "Banana",
                    "include_in_main_flow": True,
                },
            ]
        },
        step_result={
            "text_items": [
                {
                    "layout_id": "L1",
                    "normalized_text": "Apple",
                    "reason": "spacing_repair",
                    "confidence": 0.93,
                    "mode": "spacing_repair",
                }
            ],
            "notes": ["changed_subset_only"],
        },
    )

    assert bool(validation.get("passed")) is True
    assert bool(validation.get("fallback_used")) is False
    items = list(normalized.get("items") or [])
    assert len(items) == 1
    assert str((items[0] or {}).get("layout_id") or "") == "L1"
    assert str((items[0] or {}).get("normalized_text") or "") == "Apple"


def test_layout_uid_text_normalization_system_prompt_should_mention_footer_links():
    service = LiteratureReaderComposeService()
    prompt = service._layout_uid_text_normalization_system_prompt()  # pylint: disable=protected-access

    assert "footer/header link footnotes" in prompt
    assert "^6, ^7, ^8, ^9" in prompt
    assert "footer_bundles" in prompt


def test_apply_layout_uid_text_normalization_to_grounding_should_update_atoms_nodes_and_meta():
    service = LiteratureReaderComposeService()
    grounding = service._apply_layout_uid_text_normalization_to_grounding(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "L1",
                    "node_kind": "paragraph",
                    "clean_text": "A p p l e",
                    "raw_text": "A p p l e",
                    "include_in_main_flow": True,
                    "source_block_ids": ["b1"],
                },
                {
                    "layout_id": "L2",
                    "node_kind": "section_heading",
                    "clean_text": "Resu lts",
                    "raw_text": "Resu lts",
                    "include_in_main_flow": True,
                    "source_block_ids": ["b2"],
                },
            ],
            "reading_nodes": [
                {
                    "node_id": "n1",
                    "node_kind": "paragraph",
                    "clean_text": "A p p l e",
                    "source_layout_ids": ["L1"],
                },
                {
                    "node_id": "n2",
                    "node_kind": "section_heading",
                    "clean_text": "Resu lts",
                    "source_layout_ids": ["L2"],
                },
            ],
            "meta": {},
        },
        normalization_plan={
            "items": [
                {
                    "layout_id": "L1",
                    "source_text": "A p p l e",
                    "normalized_text": "Apple",
                    "reason": "spacing_repair",
                    "mode": "spacing_repair",
                    "confidence": 0.93,
                    "changed": True,
                }
            ],
            "notes": ["applied_spacing_repair"],
        },
    )

    atoms = list(grounding.get("layout_atoms") or [])
    nodes = list(grounding.get("reading_nodes") or [])
    assert str((atoms[0] or {}).get("normalized_text") or "") == "Apple"
    assert str((atoms[0] or {}).get("normalization_reason") or "") == "spacing_repair"
    assert str((nodes[0] or {}).get("normalized_text") or "") == "Apple"
    assert str((nodes[0] or {}).get("normalization_mode") or "") == "spacing_repair"
    summary = dict((grounding.get("meta") or {}).get("normalization_summary") or {})
    assert int(summary.get("item_count") or 0) == 1
    assert str(((summary.get("items") or [])[0] or {}).get("layout_id") or "") == "L1"


def test_apply_layout_uid_text_normalization_to_grounding_should_backfill_footer_link_urls():
    service = LiteratureReaderComposeService()
    grounding = service._apply_layout_uid_text_normalization_to_grounding(  # pylint: disable=protected-access
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "f6",
                    "reading_order": 10,
                    "node_kind": "footer",
                    "clean_text": "ehttps://github.com/ggml-org/llama.cpp",
                    "raw_text": "ehttps://github.com/ggml-org/llama.cpp",
                },
                {
                    "layout_id": "f7",
                    "reading_order": 11,
                    "node_kind": "footer",
                    "clean_text": "/https://unsloth.ai/blog/deepseekr1-dynamic",
                    "raw_text": "/https://unsloth.ai/blog/deepseekr1-dynamic",
                },
                {
                    "layout_id": "f8",
                    "reading_order": 12,
                    "node_kind": "footer",
                    "clean_text": "Shttps://api-docs.deepseek.com/",
                    "raw_text": "Shttps://api-docs.deepseek.com/",
                },
                {
                    "layout_id": "f9",
                    "reading_order": 13,
                    "node_kind": "footer",
                    "clean_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                    "raw_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                },
            ],
            "reading_nodes": [
                {"node_id": "layout:f6", "source_layout_ids": ["f6"]},
                {"node_id": "layout:f7", "source_layout_ids": ["f7"]},
                {"node_id": "layout:f8", "source_layout_ids": ["f8"]},
                {"node_id": "layout:f9", "source_layout_ids": ["f9"]},
            ],
        },
        normalization_plan={
            "items": [
                {
                    "layout_id": "f6",
                    "source_text": "ehttps://github.com/ggml-org/llama.cpp",
                    "normalized_text": "^6 https://github.com/ggml-org/llama.cpp",
                    "reason": "footer_link_cleanup",
                    "mode": "ocr_cleanup",
                    "confidence": 0.95,
                    "changed": True,
                },
                {
                    "layout_id": "f7",
                    "source_text": "/https://unsloth.ai/blog/deepseekr1-dynamic",
                    "normalized_text": "^7 https://unsloth.ai/blog/deepseekr1-dynamic",
                    "reason": "footer_link_cleanup",
                    "mode": "ocr_cleanup",
                    "confidence": 0.95,
                    "changed": True,
                },
                {
                    "layout_id": "f8",
                    "source_text": "Shttps://api-docs.deepseek.com/",
                    "normalized_text": "Shttps://api-docs.deepseek.com/",
                    "reason": "",
                    "mode": "no_change",
                    "confidence": 0.0,
                    "changed": False,
                },
                {
                    "layout_id": "f9",
                    "source_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                    "normalized_text": "Yhttps://cloud.tencent.com/document/product/1772/115963",
                    "reason": "",
                    "mode": "no_change",
                    "confidence": 0.0,
                    "changed": False,
                },
            ]
        },
    )

    atoms = {
        str(atom.get("layout_id") or ""): dict(atom)
        for atom in list(grounding.get("layout_atoms") or [])
    }
    assert str(atoms["f8"].get("normalized_text") or "") == "^8 https://api-docs.deepseek.com/"
    assert str(atoms["f9"].get("normalized_text") or "") == "^9 https://cloud.tencent.com/document/product/1772/115963"
    assert str(atoms["f8"].get("normalization_mode") or "") == "footer_link_fallback"
    assert str(atoms["f9"].get("normalization_reason") or "") == "footer_link_cleanup"


def test_merge_existing_grounding_enrichments_should_not_keep_stale_render_asset_page_image():
    service = LiteratureReaderComposeService()
    merged = service._merge_existing_grounding_enrichments(  # pylint: disable=protected-access
        existing_grounding={
            "page_image": {
                "url": "http://localhost:3000/api/v1/literature/reader/page-assets/85/5",
                "path": "/app/uploads/reader_page_assets/85/page_5.jpg",
                "width": 1360,
                "height": 1760,
                "source": "page_render_asset",
                "origin_url": "",
                "local_cached": True,
            }
        },
        rebuilt_grounding={
            "page_image": {
                "url": "",
                "path": "",
                "width": None,
                "height": None,
                "source": "docmind_page_image_unlocalized",
                "origin_url": "https://example.com/docmind/page5.png",
                "local_cached": False,
            }
        },
    )

    page_image = dict(merged.get("page_image") or {})
    assert str(page_image.get("source") or "") == "docmind_page_image_unlocalized"
    assert str(page_image.get("url") or "") == ""
    assert str(page_image.get("path") or "") == ""
    assert bool(page_image.get("local_cached")) is False
    assert int(page_image.get("width") or 0) == 1360
    assert int(page_image.get("height") or 0) == 1760


def test_build_layout_uid_prompt_payload_should_prefer_normalized_text():
    service = LiteratureReaderComposeService()
    payload = service._build_layout_uid_prompt_payload(  # pylint: disable=protected-access
        paper=SimpleNamespace(id=85, title="demo"),
        page=4,
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "L1",
                    "reading_order": 1,
                    "layout_type": "text",
                    "layout_sub_type": "para",
                    "node_kind": "paragraph",
                    "clean_text": "A p p l e",
                    "normalized_text": "Apple",
                    "include_in_main_flow": True,
                    "region_hint": "main",
                    "layout_pos": [],
                    "blocks": [],
                }
            ]
        },
    )

    atoms = list(payload.get("layout_atoms") or [])
    assert len(atoms) == 1
    assert str((atoms[0] or {}).get("text") or "") == "Apple"


def test_layout_uid_group_plan_to_panel_plan_should_prefer_normalized_text_for_reading_nodes():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=4,
        grouping_plan={
            "groups": [
                {
                    "group_id": "title_1",
                    "group_kind": "section_heading",
                    "source_layout_ids": ["L1"],
                },
                {
                    "group_id": "paragraph_1",
                    "group_kind": "paragraph",
                    "source_layout_ids": ["L2"],
                },
                {
                    "group_id": "list_1",
                    "group_kind": "list",
                    "source_layout_ids": ["L3"],
                },
            ]
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "L1",
                    "node_kind": "section_heading",
                    "clean_text": "Experi mental Setting",
                    "normalized_text": "Experimental Setting",
                },
                {
                    "layout_id": "L2",
                    "node_kind": "paragraph",
                    "clean_text": "We eva luate model behavior.",
                    "normalized_text": "We evaluate model behavior.",
                },
                {
                    "layout_id": "L3",
                    "node_kind": "list",
                    "clean_text": "1. llama.cpp6\n2. Unsloth7",
                    "normalized_text": "1. llama.cpp\n2. Unsloth",
                },
            ]
        },
    )

    nodes = list((panel_plan.get("panels") or [])[0].get("nodes") or [])
    assert len(nodes) == 3
    assert str(((nodes[0] or {}).get("props") or {}).get("text") or "") == "Experimental Setting"
    paragraph_props = dict((nodes[1] or {}).get("props") or {})
    assert str(paragraph_props.get("text") or "") == "We evaluate model behavior."
    list_props = dict((nodes[2] or {}).get("props") or {})
    assert list(list_props.get("items") or []) == ["1. llama.cpp", "2. Unsloth"]


def test_normalize_layout_uid_table_logical_row_plan_should_require_exact_once_coverage():
    service = LiteratureReaderComposeService()
    normalized, validation = service._normalize_layout_uid_table_logical_row_plan(  # pylint: disable=protected-access
        physical_rows=[
            {"row_index": 0, "cells": [{"col_start": 0, "text": "Header"}]},
            {"row_index": 1, "cells": [{"col_start": 0, "text": "AIME 2024"}]},
            {"row_index": 2, "cells": [{"col_start": 0, "text": "72.6"}]},
        ],
        step_result={
            "logical_rows": [
                {"logical_row_id": "lr1", "row_role": "header", "source_row_indices": [0]},
                {"logical_row_id": "lr2", "row_role": "data", "source_row_indices": [1, 1]},
            ],
            "notes": ["bad_plan"],
        },
    )

    assert validation["passed"] is False
    assert validation["fallback_used"] is True
    assert "missing_physical_row:2" in list(validation.get("errors") or [])
    assert list((normalized.get("logical_rows") or [])[0].get("source_row_indices") or []) == [0]
    assert list((normalized.get("logical_rows") or [])[1].get("source_row_indices") or []) == [1]
    assert list((normalized.get("logical_rows") or [])[2].get("source_row_indices") or []) == [2]


def test_layout_uid_group_plan_to_panel_plan_should_apply_ai_table_logical_rows():
    service = LiteratureReaderComposeService()
    panel_plan = service._layout_uid_group_plan_to_panel_plan(  # pylint: disable=protected-access
        page=7,
        grouping_plan={
            "groups": [
                {
                    "group_id": "table_group_1",
                    "group_kind": "table",
                    "source_layout_ids": ["table_body_1", "table_caption_1"],
                    "rationale": "test_table_bundle",
                },
            ],
            "omissions": [],
            "notes": [],
        },
        grounding={
            "layout_atoms": [
                {
                    "layout_id": "table_body_1",
                    "node_kind": "table",
                    "clean_text": "Model Score",
                    "raw_text": "Model Score",
                    "canonical_block_ids": ["p7_dm_p7_l003_b001"],
                    "table_cells": [
                        {"cell_id": 0, "row_start": 0, "row_end": 0, "col_start": 0, "col_end": 0, "text": "DeepSeek-R1", "layout_ids": ["table_r0c0"], "polygons": [[{"x": 100, "y": 100}, {"x": 220, "y": 100}, {"x": 220, "y": 124}, {"x": 100, "y": 124}]]},
                        {"cell_id": 1, "row_start": 1, "row_end": 1, "col_start": 0, "col_end": 0, "text": "distill-Qwen-32B", "layout_ids": ["table_r1c0"], "polygons": [[{"x": 100, "y": 125}, {"x": 220, "y": 125}, {"x": 220, "y": 148}, {"x": 100, "y": 148}]]},
                        {"cell_id": 2, "row_start": 0, "row_end": 0, "col_start": 1, "col_end": 1, "text": "BF16", "layout_ids": ["table_r0c1"], "polygons": [[{"x": 260, "y": 100}, {"x": 360, "y": 100}, {"x": 360, "y": 124}, {"x": 260, "y": 124}]]},
                        {"cell_id": 3, "row_start": 1, "row_end": 1, "col_start": 1, "col_end": 1, "text": "(Reported)", "layout_ids": ["table_r1c1"], "polygons": [[{"x": 260, "y": 125}, {"x": 360, "y": 125}, {"x": 360, "y": 148}, {"x": 260, "y": 148}]]},
                        {"cell_id": 4, "row_start": 2, "row_end": 2, "col_start": 0, "col_end": 0, "text": "AIME 2024", "layout_ids": ["table_r2c0"], "polygons": [[{"x": 100, "y": 160}, {"x": 220, "y": 160}, {"x": 220, "y": 184}, {"x": 100, "y": 184}]]},
                        {"cell_id": 5, "row_start": 2, "row_end": 2, "col_start": 1, "col_end": 1, "text": "72.6", "layout_ids": ["table_r2c1"], "polygons": [[{"x": 260, "y": 160}, {"x": 360, "y": 160}, {"x": 360, "y": 184}, {"x": 260, "y": 184}]]},
                        {"cell_id": 6, "row_start": 3, "row_end": 3, "col_start": 1, "col_end": 1, "text": "(±2.75)", "layout_ids": ["table_r3c1"], "polygons": [[{"x": 260, "y": 188}, {"x": 360, "y": 188}, {"x": 360, "y": 212}, {"x": 260, "y": 212}]]},
                    ],
                    "blocks": [],
                },
                {
                    "layout_id": "table_caption_1",
                    "node_kind": "table_caption",
                    "clean_text": "Table 2. Quantization comparison across evaluation suites",
                    "raw_text": "Table 2. Quantization comparison across evaluation suites",
                    "blocks": [],
                },
            ]
        },
        table_refinements={
            "table_group_1": {
                "logical_row_plan": {
                    "logical_rows": [
                        {"logical_row_id": "lr1", "row_role": "header", "source_row_indices": [0, 1], "rationale": "multi_line_header"},
                        {"logical_row_id": "lr2", "row_role": "data", "source_row_indices": [2, 3], "rationale": "value_plus_uncertainty"},
                    ],
                    "notes": ["ai_table_logical_rows"],
                }
            }
        },
    )

    table_node = dict(((panel_plan.get("panels") or [])[0].get("nodes") or [])[0] or {})
    table_props = dict(table_node.get("props") or {})
    logical_rows = list(table_props.get("logical_rows") or [])
    assert str(table_props.get("reconstruction_mode") or "") == "ai_logical_rows"
    assert int(table_props.get("logical_header_row_count") or 0) == 1
    assert len(logical_rows) == 2
    assert list((logical_rows[0] or {}).get("source_row_indices") or []) == [0, 1]
    assert str((((logical_rows[0] or {}).get("cells") or [])[0].get("text") or "")) == "DeepSeek-R1\ndistill-Qwen-32B"
    assert list((logical_rows[1] or {}).get("source_row_indices") or []) == [2, 3]
    assert str((((logical_rows[1] or {}).get("cells") or [])[1].get("text") or "")) == "72.6\n(±2.75)"


def test_layout_uid_table_logical_row_system_prompt_should_describe_value_uncertainty_pairing():
    service = LiteratureReaderComposeService()
    prompt = service._layout_uid_table_logical_row_system_prompt()  # pylint: disable=protected-access

    assert "value row and its uncertainty row `(±...)` usually belong to the same logical data row" in prompt
    assert "blank first-column continuation row" in prompt
    assert "multi-line headers" in prompt


def test_build_layout_uid_table_logical_row_prompt_payload_should_include_pairing_hints():
    service = LiteratureReaderComposeService()
    payload = service._build_layout_uid_table_logical_row_prompt_payload(  # pylint: disable=protected-access
        page=7,
        title="Table 5",
        caption="Benchmark results",
        table_cells=[
            {"cell_id": 1, "row_start": 2, "row_end": 2, "col_start": 0, "col_end": 0, "text": "AIME 2024"},
            {"cell_id": 2, "row_start": 2, "row_end": 2, "col_start": 1, "col_end": 1, "text": "72.6"},
            {"cell_id": 3, "row_start": 3, "row_end": 3, "col_start": 1, "col_end": 1, "text": "(±2.75)"},
            {"cell_id": 4, "row_start": 3, "row_end": 3, "col_start": 2, "col_end": 2, "text": "(±4.71)"},
        ],
    )

    rows = list(payload.get("physical_rows") or [])
    assert len(rows) == 2
    assert dict(rows[0].get("hints") or {}).get("blank_first_column") is False
    assert dict(rows[1].get("hints") or {}).get("blank_first_column") is True
    assert dict(rows[1].get("hints") or {}).get("looks_like_uncertainty_row") is True
    assert "value_plus_uncertainty" in list((payload.get("rules") or {}).get("common_patterns") or [])


def test_materialize_layout_uid_logical_table_rows_should_merge_sparse_value_row_with_uncertainty_row():
    service = LiteratureReaderComposeService()
    logical_rows, logical_header_row_count = service._materialize_layout_uid_logical_table_rows(  # pylint: disable=protected-access
        normalized_cells=[
            {"cell_id": 1, "row_start": 0, "row_end": 0, "col_start": 2, "col_end": 2, "text": "38.34", "layout_ids": ["r0c2"]},
            {"cell_id": 2, "row_start": 0, "row_end": 0, "col_start": 3, "col_end": 3, "text": "41.66", "layout_ids": ["r0c3"]},
            {"cell_id": 3, "row_start": 1, "row_end": 1, "col_start": 0, "col_end": 0, "text": "AIME 2024", "layout_ids": ["r1c0"]},
            {"cell_id": 4, "row_start": 1, "row_end": 1, "col_start": 1, "col_end": 1, "text": "39.2", "layout_ids": ["r1c1"]},
            {"cell_id": 5, "row_start": 1, "row_end": 1, "col_start": 2, "col_end": 2, "text": "(±2.52)", "layout_ids": ["r1c2"]},
            {"cell_id": 6, "row_start": 1, "row_end": 1, "col_start": 3, "col_end": 3, "text": "(±4.72)", "layout_ids": ["r1c3"]},
        ],
        logical_row_plan={
            "logical_rows": [
                {"logical_row_id": "lr1", "row_role": "data", "source_row_indices": [0]},
                {"logical_row_id": "lr2", "row_role": "data", "source_row_indices": [1]},
            ]
        },
    )

    assert logical_header_row_count == 0
    assert len(logical_rows) == 1
    cells = {int(cell.get("col_start") or 0): dict(cell) for cell in list((logical_rows[0] or {}).get("cells") or [])}
    assert list((logical_rows[0] or {}).get("source_row_indices") or []) == [0, 1]
    assert str((cells[0].get("text") or "")) == "AIME 2024"
    assert str((cells[1].get("text") or "")) == "39.2"
    assert str((cells[2].get("text") or "")) == "38.34\n(±2.52)"
    assert str((cells[3].get("text") or "")) == "41.66\n(±4.72)"


def test_materialize_layout_uid_logical_table_rows_should_merge_blank_lead_uncertainty_row_into_previous():
    service = LiteratureReaderComposeService()
    logical_rows, _ = service._materialize_layout_uid_logical_table_rows(  # pylint: disable=protected-access
        normalized_cells=[
            {"cell_id": 1, "row_start": 0, "row_end": 0, "col_start": 0, "col_end": 0, "text": "MBPP+", "layout_ids": ["r0c0"]},
            {"cell_id": 2, "row_start": 0, "row_end": 0, "col_start": 2, "col_end": 2, "text": "73.35", "layout_ids": ["r0c2"]},
            {"cell_id": 3, "row_start": 0, "row_end": 0, "col_start": 3, "col_end": 3, "text": "72.90", "layout_ids": ["r0c3"]},
            {"cell_id": 4, "row_start": 1, "row_end": 1, "col_start": 2, "col_end": 2, "text": "(±1.21)", "layout_ids": ["r1c2"]},
            {"cell_id": 5, "row_start": 1, "row_end": 1, "col_start": 3, "col_end": 3, "text": "(±0.66)", "layout_ids": ["r1c3"]},
        ],
        logical_row_plan={
            "logical_rows": [
                {"logical_row_id": "lr1", "row_role": "data", "source_row_indices": [0]},
                {"logical_row_id": "lr2", "row_role": "data", "source_row_indices": [1]},
            ]
        },
    )

    assert len(logical_rows) == 1
    cells = {int(cell.get("col_start") or 0): dict(cell) for cell in list((logical_rows[0] or {}).get("cells") or [])}
    assert str((cells[2].get("text") or "")) == "73.35\n(±1.21)"
    assert str((cells[3].get("text") or "")) == "72.90\n(±0.66)"


def test_panel_plan_to_ui_plan_should_keep_ai_table_logical_row_fields():
    service = LiteratureReaderComposeService()
    ui_plan = service._panel_plan_to_ui_plan(  # pylint: disable=protected-access
        page=7,
        panel_plan={
            "plan_id": "panel_plan_keep_ai_table_rows",
            "panels": [
                {
                    "panel_id": "main",
                    "nodes": [
                        {
                            "node_id": "table_1",
                            "component": "TablePanel",
                            "source_layout_ids": ["layout_table_1"],
                            "props": {
                                "title": "Table 5",
                                "rows": [["legacy"]],
                                "logical_rows": [
                                    {
                                        "logical_row_id": "logical_row_0",
                                        "row_role": "header",
                                        "source_row_indices": [0, 1],
                                        "cells": [
                                            {
                                                "text": "DeepSeek-R1\ndistill-Qwen-32B",
                                                "col_start": 0,
                                                "col_span": 1,
                                            },
                                            {
                                                "text": "BF16\n(Reported)",
                                                "col_start": 1,
                                                "col_span": 1,
                                            },
                                        ],
                                    }
                                ],
                                "logical_header_row_count": 1,
                                "reconstruction_mode": "ai_logical_rows",
                                "reconstruction_notes": ["ai_table_logical_rows"],
                            },
                            "children": [],
                        }
                    ],
                }
            ],
            "style_plan": {},
        },
        docmind_blocks=[
            {
                "layout_id": "layout_table_1",
                "source_text": "Table 5",
                "type": "table",
            }
        ],
        layout_to_block_ids={"layout_table_1": ["p7_dm_table_1"]},
        base_payload={
            "assets": [],
            "blocks": [],
            "page_grounding_v1": {
                "layout_atoms": [
                    {
                        "layout_id": "layout_table_1",
                        "clean_text": "Table 5",
                        "raw_text": "Table 5",
                        "canonical_block_ids": ["p7_dm_table_1"],
                        "layout_pos": [
                            {"x": 120, "y": 220},
                            {"x": 920, "y": 220},
                            {"x": 920, "y": 720},
                            {"x": 120, "y": 720},
                        ],
                        "blocks": [
                            {
                                "block_index": 1,
                                "text": "Table 5",
                                "pos": [
                                    {"x": 120, "y": 220},
                                    {"x": 920, "y": 220},
                                    {"x": 920, "y": 720},
                                    {"x": 120, "y": 720},
                                ],
                            }
                        ],
                    }
                ],
                "evidence_map": [
                    {
                        "source_layout_id": "layout_table_1",
                        "source_block_ids": ["p7_dm_table_1"],
                        "layout_pos": [
                            {"x": 120, "y": 220},
                            {"x": 920, "y": 220},
                            {"x": 920, "y": 720},
                            {"x": 120, "y": 720},
                        ],
                        "block_positions": [
                            [
                                {"x": 120, "y": 220},
                                {"x": 920, "y": 220},
                                {"x": 920, "y": 720},
                                {"x": 120, "y": 720},
                            ]
                        ],
                    }
                ],
                "page_image": {"width": 1600, "height": 2200},
            },
        },
        style_intent=None,
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    components = list(ui_plan.get("components") or [])
    assert len(components) == 1
    node = dict(components[0] or {})
    props = dict(node.get("props") or {})
    assert str(props.get("reconstruction_mode") or "") == "ai_logical_rows"
    assert list(props.get("reconstruction_notes") or []) == ["ai_table_logical_rows"]
    assert int(props.get("logical_header_row_count") or 0) == 1
    logical_rows = list(props.get("logical_rows") or [])
    assert len(logical_rows) == 1
    assert list((logical_rows[0] or {}).get("source_row_indices") or []) == [0, 1]
    assert str((((logical_rows[0] or {}).get("cells") or [])[0].get("text") or "")) == "DeepSeek-R1\ndistill-Qwen-32B"


def test_panel_plan_to_ui_plan_should_keep_equation_normalization_fields():
    service = LiteratureReaderComposeService()
    ui_plan = service._panel_plan_to_ui_plan(  # pylint: disable=protected-access
        page=3,
        panel_plan={
            "plan_id": "panel_plan_keep_equation_normalization",
            "panels": [
                {
                    "panel_id": "main",
                    "nodes": [
                        {
                            "node_id": "equation_1",
                            "component": "EquationBlock",
                            "source_layout_ids": ["layout_equation_1"],
                            "props": {
                                "latex": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                                "label": "(1)",
                                "description": "",
                                "render_mode": "math_first",
                                "transcript": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                                "normalized_text": "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||",
                                "normalized_latex": r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert",
                                "normalization_reason": "Recovered theta and calibration subscript from the page image and style hints.",
                                "normalization_mode": "latex_reconstructed",
                                "normalization_confidence": 0.84,
                            },
                            "children": [],
                        }
                    ],
                }
            ],
            "style_plan": {},
        },
        docmind_blocks=[
            {
                "layout_id": "layout_equation_1",
                "source_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                "type": "formula",
            }
        ],
        layout_to_block_ids={"layout_equation_1": ["p3_dm_formula_1"]},
        base_payload={
            "assets": [],
            "blocks": [],
            "page_grounding_v1": {
                "layout_atoms": [
                    {
                        "layout_id": "layout_equation_1",
                        "clean_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                        "raw_text": "minEx~DcaliblfFp(x)-fquant(0x)||, (1) 0",
                        "canonical_block_ids": ["p3_dm_formula_1"],
                        "layout_pos": [
                            {"x": 100, "y": 260},
                            {"x": 340, "y": 260},
                            {"x": 340, "y": 284},
                            {"x": 100, "y": 284},
                        ],
                        "blocks": [],
                    }
                ],
                "page_image": {"width": 1483, "height": 1920},
            },
        },
        style_intent=None,
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    components = list(ui_plan.get("components") or [])
    assert len(components) == 1
    props = dict((components[0] or {}).get("props") or {})
    assert str(props.get("normalized_latex") or "") == r"\min_{x \sim D_{\mathrm{calib}}}\lVert f_P(x) - f_{\mathrm{quant}}(\theta, x)\rVert"
    assert str(props.get("normalized_text") or "") == "min_{x\\sim D_{calib}} ||f_P(x) - f_{quant}(\\theta, x)||"
    assert str(props.get("normalization_reason") or "") == "Recovered theta and calibration subscript from the page image and style hints."
    assert str(props.get("normalization_mode") or "") == "latex_reconstructed"
    assert float(props.get("normalization_confidence") or 0.0) == 0.84


@pytest.mark.asyncio
async def test_build_or_get_composed_payload_should_route_layout_uid_v1_pipeline(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "layout_uid_v1")
    calls = {"layout_uid": 0, "semantic": 0, "controller": 0}

    async def _build_source_signature(**_kwargs):
        return "sig-layout-uid"

    async def _read_payload_from_redis(_key):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _acquire_lock(_lock_key):
        return "lock-token"

    async def _release_lock(_lock_key, _token):
        return None

    async def _reader_payload(**_kwargs):
        return (
            {
                "docmind_structure": {"layouts": []},
                "page_structure_v3": {"block_groups": []},
                "blocks": [],
                "assets": [],
            },
            SimpleNamespace(),
        )

    async def _no_db_upsert(**_kwargs):
        return None

    async def _no_redis_write(_key, _payload):
        return None

    async def _layout_uid_result(**_kwargs):
        calls["layout_uid"] += 1
        return {
            "base_payload": {
                "pipeline_contract_meta": {"pipeline": "reader_layout_uid_v1"},
                "minimal_gate_report": {},
                "qwen_plan_meta": {"reason": "layout_uid_v1"},
            },
            "loop_result": {
                "ui_plan": {
                    "plan_id": "layout_uid_v1_p1",
                    "components": [],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {"overall": 0.91, "hard_constraints_passed": True},
                "iteration_trace": [],
                "iterations": 1,
                "degraded": False,
                "stop_reason": "layout_uid_v1_done",
                "build_mode": "compose_agent_layout_uid_v1",
            },
            "assets": [],
        }

    async def _semantic_result(**_kwargs):
        calls["semantic"] += 1
        raise AssertionError("semantic atom pipeline should not be used for layout_uid_v1")

    async def _controller_result(**_kwargs):
        calls["controller"] += 1
        raise AssertionError("single_agent controller should not be used for layout_uid_v1")

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _acquire_lock)
    monkeypatch.setattr(service, "_release_lock", _release_lock)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "_upsert_payload_to_db", _no_db_upsert)
    monkeypatch.setattr(service, "_write_payload_to_redis", _no_redis_write)
    monkeypatch.setattr(service, "_partition_main_aux_block_ids", lambda **_kwargs: ([], []))
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _reader_payload)
    monkeypatch.setattr(service, "_build_layout_uid_pipeline_result", _layout_uid_result)
    monkeypatch.setattr(service, "_build_simplified_pipeline_result", _semantic_result)
    monkeypatch.setattr(service, "_build_single_agent_v2_result", _controller_result)

    payload, _ = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=82, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=False,
    )

    assert calls["layout_uid"] == 1
    assert calls["semantic"] == 0
    assert calls["controller"] == 0
    assert str(payload.get("build_mode") or "") == "compose_agent_layout_uid_v1"
    assert str((payload.get("pipeline_contract_meta") or {}).get("pipeline") or "") == "reader_layout_uid_v1"


def test_layout_plan_prompt_should_include_all_block_ids_without_truncation():
    service = ReaderMultimodalLayoutService()
    block_groups = [
        {
            "block_id": f"p1_dm_{idx:03d}",
            "kind": "paragraph",
            "zone_type": "main_body",
            "reading_order": idx,
            "text": f"paragraph {idx}",
            "layout_bbox_or_polygon": {"bbox": {"x0": 10, "x1": 20, "top": idx, "bottom": idx + 1}, "polygon": []},
            "style_summary": {"font_size": 10.5},
        }
        for idx in range(1, 141)
    ]
    prompt = service._build_layout_plan_v2_prompt_text(  # pylint: disable=protected-access
        {
            "layout_summary": {},
            "layout_meta": {},
            "images": [],
            "valid_block_ids": [row["block_id"] for row in block_groups],
            "component_whitelist": ["ParagraphProse"],
            "page_structure_v3": {"block_groups": block_groups, "counts": {"block_count": len(block_groups)}},
        }
    )
    assert "p1_dm_001" in prompt
    assert "p1_dm_140" in prompt


def test_build_main_blocks_from_page_structure_should_use_docmind_geometry_when_no_word_ids():
    service = LiteratureReaderComposeService()
    page_structure = {
        "source": "document_mind",
        "block_groups": [
            {
                "block_id": "dm_p1_l001_b001",
                "kind": "paragraph",
                "zone_type": "main_body",
                "column_id": "main_left",
                "reading_order": 1,
                "text": "DocMind paragraph text.",
                "confidence": 0.92,
                "word_ids": [],
                "char_ranges": [],
                "layout_bbox_or_polygon": {
                    "bbox": {"x0": 80, "x1": 760, "top": 180, "bottom": 240},
                    "polygon": [{"x": 80, "y": 180}, {"x": 760, "y": 180}, {"x": 760, "y": 240}, {"x": 80, "y": 240}],
                },
            }
        ],
    }
    output = service._build_main_blocks_from_page_structure(
        page=1,
        page_structure=page_structure,
        base_payload={"native_page_extract": {"page_meta": {"page_width": 840, "page_height": 1188}, "words": [], "chars": []}},
    )
    assert len(output) == 1
    anchor = dict((output[0].get("source_anchor") or {}))
    assert str(anchor.get("geometry_version") or "") == "poly_v1"
    assert isinstance(anchor.get("geometry"), dict)
    bbox = dict(anchor.get("bbox_hint") or {})
    assert float(bbox.get("x0") or 0.0) == 80.0
    assert float(bbox.get("x1") or 0.0) == 760.0


def _simplified_docmind_payload() -> dict:
    return {
        "layouts": [
            {
                "uniqueId": "L1",
                "index": 1,
                "type": "title",
                "subType": "doc_title",
                "text": "Title",
                "pageNum": [1],
                "pos": [{"x": 10, "y": 10}, {"x": 510, "y": 10}, {"x": 510, "y": 50}, {"x": 10, "y": 50}],
                "blocks": [{"text": "Title", "pos": [{"x": 10, "y": 10}, {"x": 510, "y": 10}, {"x": 510, "y": 50}, {"x": 10, "y": 50}]}],
            },
            {
                "uniqueId": "L2",
                "index": 2,
                "type": "text",
                "subType": "para",
                "text": "Paragraph",
                "pageNum": [1],
                "pos": [{"x": 10, "y": 70}, {"x": 510, "y": 70}, {"x": 510, "y": 140}, {"x": 10, "y": 140}],
                "blocks": [{"text": "Paragraph", "pos": [{"x": 10, "y": 70}, {"x": 510, "y": 70}, {"x": 510, "y": 140}, {"x": 10, "y": 140}]}],
            },
        ]
    }


@pytest.mark.asyncio
async def test_single_agent_v2_should_force_refresh_once_when_docmind_empty(monkeypatch):
    service = LiteratureReaderComposeService()
    refresh_calls = {"count": 0}

    async def _fake_refresh_payload(**kwargs):
        refresh_calls["count"] += 1
        assert bool(kwargs.get("force_refresh")) is True
        return (
            {
                "docmind_structure": _simplified_docmind_payload(),
                "page_structure_v3": {
                    "block_groups": [
                        {"layout_unique_id": "L1", "block_id": "p1_b1"},
                        {"layout_unique_id": "L2", "block_id": "p1_b2"},
                    ]
                },
                "blocks": [
                    {"id": "p1_b1", "text": "Title", "source_anchor": {"page": 1, "start_char": 0, "end_char": 5}},
                    {"id": "p1_b2", "text": "Paragraph", "source_anchor": {"page": 1, "start_char": 6, "end_char": 16}},
                ],
                "assets": [],
            },
            SimpleNamespace(),
        )

    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _fake_refresh_payload)
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_: "")  # pylint: disable=protected-access

    async def _fake_controller_run(**_kwargs):
        return {
            "status": "done",
            "degraded_reason": "",
            "step_result": {
                "classification": {"items": [{"layout_id": "L2", "bucket": "main_content"}]},
                "cleaning": {"items": [{"layout_id": "L2", "source_text": "Paragraph", "normalized_text": "Paragraph"}]},
                "ui_plan_draft": {
                    "components": [
                        {"component": "ParagraphProse", "source_block_ids": ["L2"], "props": {"text": "Paragraph"}}
                    ]
                },
            },
            "validation_report": _validation_report_stub(True),
            "repair_report": {"steps_executed": 1, "step_metrics": []},
        }

    monkeypatch.setattr(service._single_agent_controller, "run", _fake_controller_run)

    async def _fake_panel_plan_run(**_kwargs):
        return {
            "status": "fallback",
            "degraded_reason": "validator_non_converged",
            "panel_plan": {},
            "validation_report": _validation_report_stub(False),
            "repair_report": {"steps_executed": 0, "step_metrics": []},
            "usage": {},
        }

    monkeypatch.setattr(service._panel_plan_agent, "run", _fake_panel_plan_run)

    result = await service._build_single_agent_v2_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78, user_id=1, title="demo", pdf_path=""),
        page=1,
        base_payload={
            "docmind_structure": {"layouts": []},
            "page_structure_v3": {"block_groups": []},
            "blocks": [],
            "assets": [],
        },
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=8000,
        selected_kb_id=None,
    )

    loop_result = dict(result.get("loop_result") or {})
    ui_plan = dict(loop_result.get("ui_plan") or {})
    components = list(ui_plan.get("components") or [])
    assert refresh_calls["count"] == 1
    assert str(loop_result.get("stop_reason") or "") == "single_agent_v2_done"
    assert len(components) > 0


@pytest.mark.asyncio
async def test_single_agent_v2_missing_aux_block_should_trigger_no_drop_blocks_fallback_and_keep_renderable_ui(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "single_agent_v2")
    monkeypatch.setattr(service, "_is_single_agent_v2_enabled", lambda **_kwargs: True)

    async def _build_source_signature(**_kwargs):
        return "sig-no-drop"

    async def _read_payload_from_redis(_key):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _upsert_payload_to_db(**_kwargs):
        return None

    async def _write_payload_to_redis(_key, _payload):
        return None

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _acquire_lock(_lock_key):
        return "lock-token"

    async def _release_lock(_lock_key, _token):
        return None

    async def _fake_reader_payload(**_kwargs):
        return (
            {
                "docmind_structure": {
                    "layouts": [
                        {
                            "uniqueId": "L1",
                            "index": 1,
                            "type": "text",
                            "subType": "para",
                            "text": "Main paragraph.",
                            "pageNum": [1],
                            "blocks": [{"text": "Main paragraph."}],
                        },
                        {
                            "uniqueId": "L2",
                            "index": 2,
                            "type": "header",
                            "subType": "header",
                            "text": "OPEN ACCESS",
                            "pageNum": [1],
                            "blocks": [{"text": "OPEN ACCESS"}],
                        },
                    ]
                },
                "page_structure_v3": {
                    "block_groups": [
                        {"layout_unique_id": "L1", "block_id": "p1_b1"},
                        {"layout_unique_id": "L2", "block_id": "p1_aux1"},
                    ]
                },
                "blocks": [
                    {"id": "p1_b1", "text": "Main paragraph.", "source_anchor": {"page": 1, "start_char": 0, "end_char": 15}},
                    {"id": "p1_aux1", "text": "OPEN ACCESS", "source_anchor": {"page": 1, "start_char": 16, "end_char": 27}},
                ],
                "assets": [],
            },
            SimpleNamespace(),
        )

    async def _fake_controller_run(**_kwargs):
        return {
            "status": "fallback",
            "degraded_reason": "no_drop_blocks_failed",
            "step_result": {
                "classification": {
                    "items": [
                        {
                            "layout_id": "L1",
                            "bucket": "main_content",
                            "role": "paragraph",
                            "confidence": 0.96,
                            "reason": "main_only",
                        }
                    ]
                },
                "cleaning": {
                    "items": [
                        {
                            "layout_id": "L1",
                            "source_text": "Main paragraph.",
                            "normalized_text": "Main paragraph.",
                            "clean_ops": ["whitespace_normalize"],
                            "clean_confidence": 0.99,
                            "needs_review": False,
                        },
                        {
                            "layout_id": "L2",
                            "source_text": "OPEN ACCESS",
                            "normalized_text": "OPEN ACCESS",
                            "clean_ops": [],
                            "clean_confidence": 1.0,
                            "needs_review": False,
                        },
                    ]
                },
                "ui_plan_draft": {
                    "components": [
                        {"component": "ParagraphProse", "source_block_ids": ["L1"], "props": {"text": "Main paragraph."}},
                    ],
                    "layout_tokens": {},
                },
            },
            "validation_report": {
                "passed": False,
                "gates": {
                    "id_integrity": {"passed": True, "errors": []},
                    "full_coverage": {"passed": False, "errors": ["no_drop_blocks_failed:missing:L2"]},
                    "whitelist_only": {"passed": True, "errors": []},
                    "layout_contract": {"passed": True, "errors": []},
                    "ownership_unchanged": {"passed": True, "errors": []},
                    "non_empty_plan_for_non_empty_input": {"passed": True, "errors": []},
                    "source_text_immutable": {"passed": True, "errors": []},
                },
                "errors": ["full_coverage:no_drop_blocks_failed:missing:L2"],
            },
            "repair_report": {"steps_executed": 1, "step_metrics": []},
        }

    async def _fake_panel_plan_run(**_kwargs):
        return {
            "status": "fallback",
            "degraded_reason": "validator_non_converged",
            "panel_plan": {},
            "validation_report": {
                "passed": False,
                "errors": ["validator_non_converged"],
            },
            "repair_report": {"steps_executed": 1, "step_metrics": []},
            "usage": {},
        }

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_upsert_payload_to_db", _upsert_payload_to_db)
    monkeypatch.setattr(service, "_write_payload_to_redis", _write_payload_to_redis)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "_acquire_lock", _acquire_lock)
    monkeypatch.setattr(service, "_release_lock", _release_lock)
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_: "")  # pylint: disable=protected-access
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _fake_reader_payload)
    monkeypatch.setattr(service._panel_plan_agent, "run", _fake_panel_plan_run)
    monkeypatch.setattr(service._single_agent_controller, "run", _fake_controller_run)

    payload, _ = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=False,
    )

    assert str(payload.get("status") or "") == "fallback"
    assert "no_drop_blocks_failed" in str(payload.get("degraded_reason") or "")
    assert bool(((payload.get("validation_report") or {}).get("gates") or {}).get("full_coverage", {}).get("passed")) is False
    assert "no_drop_blocks_failed" in str(
        (((payload.get("pipeline_contract_meta") or {}).get("validation_report") or {}).get("errors") or [])
    )
    assert list(payload.get("main_block_ids") or []) == ["p1_b1"]
    assert list(payload.get("aux_block_ids") or []) == ["p1_aux1"]
    components = list(((payload.get("ui_plan") or {}).get("components") or []))
    assert len(components) >= 1
    assert str((components[0] or {}).get("type") or "") == "ParagraphProse"
    assert str(((components[0] or {}).get("props") or {}).get("text") or "").strip()
    assert list((components[0] or {}).get("source_block_ids") or []) == ["p1_b1"]


@pytest.mark.asyncio
async def test_simplified_pipeline_stage1_fail_should_use_deterministic_baseline(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "simplified_v2")
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_: "")  # pylint: disable=protected-access
    async def _fake_mm_prompt(**_kwargs):
        return {}

    async def _fake_stage1(**_kwargs):
        return None, {"used": False, "fallback_used": True}

    monkeypatch.setattr(service._mm_layout_service, "build_mm_prompt_payload", _fake_mm_prompt)
    monkeypatch.setattr(service._mm_layout_service, "build_stage1_semantic_annotations", _fake_stage1)

    async def _should_not_call_stage2(**_kwargs):
        raise AssertionError("stage2 should not be called when stage1 failed in simplified pipeline")

    monkeypatch.setattr(service._mm_layout_service, "build_stage2_design_slots", _should_not_call_stage2)

    monkeypatch.setattr(
        service,
        "_build_initial_ui_plan",
        lambda **kwargs: {
            "plan_id": "p1",
            "components": [
                {
                    "id": f"node_{idx}",
                    "type": "ParagraphProse",
                    "props": {"text": "x"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": list((seg or {}).get("block_ids") or []),
                }
                for idx, seg in enumerate(list(((kwargs.get("base_payload") or {}).get("segment_map") or {}).get("segments") or []), start=1)
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )
    async def _identity_assembly(**kwargs):
        return kwargs["ui_plan"]

    monkeypatch.setattr(service, "_apply_deepseek_assembly_decision", _identity_assembly)

    result = await service._build_simplified_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78, user_id=1, title="demo", pdf_path=""),
        page=1,
        base_payload={
            "docmind_structure": _simplified_docmind_payload(),
            "page_structure_v3": {
                "block_groups": [
                    {"layout_unique_id": "L1", "block_id": "p1_b1"},
                    {"layout_unique_id": "L2", "block_id": "p1_b2"},
                ],
            },
            "blocks": [
                {"id": "p1_b1", "source_anchor": {"page": 1, "start_char": 0, "end_char": 5}},
                {"id": "p1_b2", "source_anchor": {"page": 1, "start_char": 6, "end_char": 15}},
            ],
            "assets": [],
        },
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=8000,
        selected_kb_id=None,
    )
    payload = dict(result.get("base_payload") or {})
    ui_plan = dict((result.get("loop_result") or {}).get("ui_plan") or {})
    assert str((payload.get("layout_advice_v3") or {}).get("source") or "") == "deterministic_baseline"
    assert str(((payload.get("pipeline_contract_meta") or {}).get("pipeline") or "")) == "reader_simplified_v2"
    assert str(((result.get("loop_result") or {}).get("build_mode") or "")) == "compose_agent_simplified"
    full_coverage_passed = (payload.get("minimal_gate_report") or {}).get("full_coverage")
    assert isinstance(full_coverage_passed, bool)
    assert len(list(ui_plan.get("components") or [])) > 0
    assert any(list((component or {}).get("source_atom_ids") or []) for component in list(ui_plan.get("components") or []))


@pytest.mark.asyncio
async def test_simplified_pipeline_stage2_fail_should_use_deterministic_baseline(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "simplified_v2")
    monkeypatch.setattr(service._reader_service, "_resolve_local_pdf_path", lambda **_: "")  # pylint: disable=protected-access
    async def _fake_mm_prompt(**_kwargs):
        return {}

    monkeypatch.setattr(service._mm_layout_service, "build_mm_prompt_payload", _fake_mm_prompt)

    async def _valid_stage1(**_kwargs):
        return (
            {
                "annotations": [
                    {"atom_id": "p1:lL1:b1", "role": "doc_title", "importance": "high", "grouping_hint": "", "component_hint": "SectionHeading", "confidence": 0.9},
                    {"atom_id": "p1:lL2:b1", "role": "paragraph", "importance": "normal", "grouping_hint": "", "component_hint": "ParagraphProse", "confidence": 0.9},
                ]
            },
            {"used": True},
        )

    monkeypatch.setattr(service._mm_layout_service, "build_stage1_semantic_annotations", _valid_stage1)
    async def _fake_stage2(**_kwargs):
        return None, {"used": False, "fallback_used": True, "fallback_reason": "stage2_failed"}

    monkeypatch.setattr(service._mm_layout_service, "build_stage2_design_slots", _fake_stage2)
    monkeypatch.setattr(
        service,
        "_build_initial_ui_plan",
        lambda **kwargs: {
            "plan_id": "p1",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "x"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_block_ids": ["p1_b1"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
    )
    async def _identity_assembly(**kwargs):
        return kwargs["ui_plan"]

    monkeypatch.setattr(service, "_apply_deepseek_assembly_decision", _identity_assembly)

    result = await service._build_simplified_pipeline_result(  # pylint: disable=protected-access
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=78, user_id=1, title="demo", pdf_path=""),
        page=1,
        base_payload={
            "docmind_structure": _simplified_docmind_payload(),
            "page_structure_v3": {"block_groups": [{"layout_unique_id": "L1", "block_id": "p1_b1"}]},
            "blocks": [{"id": "p1_b1", "source_anchor": {"page": 1, "start_char": 0, "end_char": 5}}],
            "assets": [],
        },
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
        latency_budget_ms=8000,
        selected_kb_id=None,
    )
    payload = dict(result.get("base_payload") or {})
    ui_plan = dict((result.get("loop_result") or {}).get("ui_plan") or {})
    assert str((payload.get("layout_advice_v3") or {}).get("source") or "") == "deterministic_baseline"
    assert str(((payload.get("pipeline_contract_meta") or {}).get("pipeline") or "")) == "reader_simplified_v2"
    assert str(((result.get("loop_result") or {}).get("build_mode") or "")) == "compose_agent_simplified"
    assert bool((((payload.get("pipeline_contract_meta") or {}).get("stage2") or {}).get("degraded"))) is True
    assert len(list(ui_plan.get("components") or [])) > 0
    assert any(list((component or {}).get("source_atom_ids") or []) for component in list(ui_plan.get("components") or []))


@pytest.mark.asyncio
async def test_build_or_get_composed_payload_should_route_simplified_v2_to_semantic_atom_pipeline(monkeypatch):
    service = LiteratureReaderComposeService()
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_version", "simplified_v2")
    monkeypatch.setattr(service, "_is_single_agent_v2_enabled", lambda **_kwargs: True)

    calls = {"semantic": 0, "controller": 0}

    async def _build_source_signature(**_kwargs):
        return "sig-semantic-route"

    async def _read_payload_from_redis(_key):
        return None

    async def _read_payload_from_db(**_kwargs):
        return None

    async def _acquire_lock(_lock_key):
        return "lock-token"

    async def _release_lock(_lock_key, _token):
        return None

    async def _apply_overlay(**kwargs):
        return dict(kwargs.get("payload") or {})

    async def _no_db_upsert(**_kwargs):
        return None

    async def _no_redis_write(_key, _payload):
        return None

    async def _reader_payload(**_kwargs):
        return (
            {
                "docmind_structure": _simplified_docmind_payload(),
                "page_structure_v3": {
                    "source": "document_mind",
                    "block_groups": [
                        {"layout_unique_id": "L1", "block_id": "dm_p1_l001_b001"},
                        {"layout_unique_id": "L2", "block_id": "dm_p1_l002_b001"},
                    ],
                },
                "blocks": [
                    {"id": "dm_p1_l001_b001", "text": "Title", "source_anchor": {"page": 1, "start_char": 0, "end_char": 5}},
                    {"id": "dm_p1_l002_b001", "text": "Paragraph", "source_anchor": {"page": 1, "start_char": 6, "end_char": 16}},
                ],
                "assets": [],
            },
            SimpleNamespace(),
        )

    async def _semantic_result(**_kwargs):
        calls["semantic"] += 1
        return {
            "base_payload": {
                "minimal_gate_report": {
                    "passed": True,
                    "schema_valid": True,
                    "whitelist_valid": True,
                    "ownership_unchanged": True,
                    "full_coverage": True,
                    "non_empty_plan_for_non_empty_input": True,
                    "used_atom_count": 2,
                    "usable_atom_count": 2,
                },
                "pipeline_contract_meta": {"used": True, "pipeline": "reader_simplified_v2"},
            },
            "loop_result": {
                "build_mode": "compose_agent_simplified",
                "ui_plan": {
                    "plan_id": "plan_semantic",
                    "components": [
                        {
                            "id": "semantic_001",
                            "type": "ParagraphProse",
                            "props": {"text": "Paragraph"},
                            "children": [],
                            "source_anchor_refs": [],
                            "source_block_ids": ["p1_dm_p1_l002_b001"],
                        }
                    ],
                    "layout": {},
                    "style_tokens": {},
                    "trace_meta": {},
                },
                "quality_report": {"overall": 0.93, "hard_constraints_passed": True},
                "iteration_trace": [],
                "iterations": 1,
                "degraded": False,
                "stop_reason": "simplified_pipeline",
            },
            "assets": [],
        }

    async def _controller_result(**_kwargs):
        calls["controller"] += 1
        raise AssertionError("single_agent controller path should not be used for simplified_v2 in phase12")

    monkeypatch.setattr(service, "_build_source_signature", _build_source_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _read_payload_from_redis)
    monkeypatch.setattr(service, "_read_payload_from_db", _read_payload_from_db)
    monkeypatch.setattr(service, "_acquire_lock", _acquire_lock)
    monkeypatch.setattr(service, "_release_lock", _release_lock)
    monkeypatch.setattr(service, "_apply_overlay_for_user", _apply_overlay)
    monkeypatch.setattr(service, "_upsert_payload_to_db", _no_db_upsert)
    monkeypatch.setattr(service, "_write_payload_to_redis", _no_redis_write)
    monkeypatch.setattr(service, "_partition_main_aux_block_ids", lambda **_kwargs: (["p1_dm_p1_l002_b001"], []))
    monkeypatch.setattr(service._reader_service, "build_or_get_page_payload", _reader_payload)
    monkeypatch.setattr(service, "_build_simplified_pipeline_result", _semantic_result)
    monkeypatch.setattr(service, "_build_single_agent_v2_result", _controller_result)

    payload, _ = await service.build_or_get_composed_payload(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=82, user_id=1, title="demo", pdf_path=""),
        page=1,
        force_refresh=False,
    )

    assert calls["semantic"] == 1
    assert calls["controller"] == 0
    assert str(payload.get("build_mode") or "") == "compose_agent_simplified"
    assert str((payload.get("pipeline_contract_meta") or {}).get("pipeline") or "") == "reader_simplified_v2"


def test_single_agent_step_result_listblock_object_items_are_sanitized():
    service = LiteratureReaderComposeService()
    ui_plan = service._step_result_to_ui_plan(  # pylint: disable=protected-access
        page=3,
        step_result={
            "classification": {"items": [{"layout_id": "L1", "bucket": "main_content"}]},
            "cleaning": {"items": [{"layout_id": "L1", "source_text": "alpha", "normalized_text": "alpha"}]},
            "ui_plan_draft": {
                "components": [
                    {
                        "component": "ListBlock",
                        "source_block_ids": ["L1"],
                        "props": {
                            "items": [
                                {"content": "first"},
                                {"text": "second"},
                                "third",
                                4,
                            ]
                        },
                    }
                ],
                "layout_tokens": {},
            },
        },
        docmind_blocks=[
            {
                "layout_id": "L1",
                "source_text": "fallback text",
                "type": "text",
                "subType": "para",
                "block_ids": ["p3_b1"],
            }
        ],
        layout_to_block_ids={"L1": ["p3_b1"]},
        base_payload={
            "blocks": [
                {
                    "id": "p3_b1",
                    "text": "fallback text",
                    "source_anchor": {"page": 3, "start_char": 0, "end_char": 12},
                }
            ]
        },
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )
    components = list((ui_plan.get("components") or []))
    assert len(components) == 1
    assert str((components[0] or {}).get("type") or "") == "ListBlock"
    assert list(((components[0] or {}).get("props") or {}).get("items") or []) == ["first", "second", "third", "4"]
    assert str((components[0] or {}).get("zone_type") or "") in {"main_body", "side_context", "figure_meta"}
    assert str((components[0] or {}).get("column_id") or "").strip()
    assert str((components[0] or {}).get("region") or "").strip()
    assert str((components[0] or {}).get("display") or "") in {"default", "collapsed", "pinned", "hidden_until_expand"}
    assert isinstance((components[0] or {}).get("order_key"), (int, float))
    assert bool((components[0] or {}).get("compat_filled")) is True
    assert len(list((components[0] or {}).get("compat_filled_fields") or [])) > 0


def test_step_result_to_ui_plan_compat_filled_for_legacy_layout_fields():
    service = LiteratureReaderComposeService()
    ui_plan = service._step_result_to_ui_plan(  # pylint: disable=protected-access
        page=5,
        step_result={
            "classification": {
                "items": [
                    {"layout_id": "L1", "bucket": "main_content"},
                    {"layout_id": "L2", "bucket": "aux_content"},
                ]
            },
            "cleaning": {
                "items": [
                    {"layout_id": "L1", "source_text": "Main paragraph", "normalized_text": "Main paragraph"},
                    {"layout_id": "L2", "source_text": "Data Availability Statement", "normalized_text": "Data Availability Statement"},
                ]
            },
            "ui_plan_draft": {
                "components": [
                    {
                        "component": "ParagraphProse",
                        "source_block_ids": ["L1"],
                        "props": {"text": "Main paragraph"},
                    },
                    {
                        "component": "ParagraphProse",
                        "source_block_ids": ["L2"],
                        "props": {"text": "Data Availability Statement"},
                        "display": "collapsed",
                    },
                ],
                "layout_tokens": {
                    "layout_mode": "split",
                    "regions": [{"id": "main", "kind": "content"}, {"id": "sidebar", "kind": "rail"}],
                },
            },
        },
        docmind_blocks=[
            {"layout_id": "L1", "source_text": "Main paragraph", "type": "text", "subType": "para", "block_ids": ["p5_b1"]},
            {"layout_id": "L2", "source_text": "Data Availability Statement", "type": "text", "subType": "para", "block_ids": ["p5_aux1"]},
        ],
        layout_to_block_ids={"L1": ["p5_b1"], "L2": ["p5_aux1"]},
        base_payload={
            "blocks": [
                {"id": "p5_b1", "text": "Main paragraph", "source_anchor": {"page": 5, "start_char": 0, "end_char": 14}},
                {"id": "p5_aux1", "text": "Data Availability Statement", "source_anchor": {"page": 5, "start_char": 20, "end_char": 46}},
            ]
        },
        style_intent="journal",
        theme_mode="light",
        detail_level="standard",
        compare_mode=False,
    )

    components = list((ui_plan.get("components") or []))
    assert len(components) == 2
    for node in components:
        assert str((node or {}).get("zone_type") or "") in {"main_body", "side_context", "figure_meta"}
        assert str((node or {}).get("column_id") or "").strip()
        assert str((node or {}).get("region") or "").strip()
        assert str((node or {}).get("display") or "") in {"default", "collapsed", "pinned", "hidden_until_expand"}
        assert isinstance((node or {}).get("order_key"), (int, float))
    assert bool((components[0] or {}).get("compat_filled")) is True
    assert "zone_type" in list((components[0] or {}).get("compat_filled_fields") or [])
    trace_meta = dict(ui_plan.get("trace_meta") or {})
    assert int(trace_meta.get("compat_filled_count") or 0) >= 1


@pytest.mark.asyncio
async def test_reader_composed_soft_disabled_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "reader_pipeline_mode", "single_agent_v2")
    monkeypatch.setattr(settings, "reader_pipeline_allowlist_papers", "")
    monkeypatch.setattr(settings, "reader_pipeline_allowlist_pages", "")

    result = await literature_api.action_reader_composed_node(
        paper_id=78,
        payload=SimpleNamespace(page=1, node_id="n1", action="degrade", reason=None, selected_kb_id=None, style_intent=None),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=1),
    )
    assert bool(result.disabled) is True
    assert str(result.disabled_reason or "") == "single_agent_v2_node_action_disabled"

    async def _fake_get_paper(_db, _user, paper_id):
        return SimpleNamespace(id=int(paper_id), user_id=1, title="demo", pdf_path="")

    class _FakeService:
        async def prepare_inline_query_answer(self, **_kwargs):
            return {
                "disabled": True,
                "disabled_reason": "inline_query_missing_source_anchor_refs",
                "message": "Inline query contract validation failed.",
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_paper)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    class _FakeSessionFactory:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(literature_api, "async_session_factory", lambda: _FakeSessionFactory())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    stream_response = await literature_api.stream_reader_composed_inline_query(
        paper_id=78,
        payload=SimpleNamespace(page=1, node_id="n1", question="q", scope="section", selected_kb_id=None, style_intent=None),
        request=_FakeRequest(),
        current_user=SimpleNamespace(id=1),
    )
    events = []
    async for chunk in stream_response.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[len("data: "):])
                events.append(str(data.get("event") or ""))
    assert events[:2] == ["disabled", "done"]
