import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api


def _sample_artifact_payload():
    return {
        "version": "page_artifact_v2",
        "artifact_contract_id": "page_artifact_v2.contract.v1",
        "focus_page": 1,
        "reader_profile": "curious_generalist",
        "dossier_signature": "sig-demo",
        "session_id": "sess-demo",
        "template_id": "guided_reading_v1",
        "layout_recipe": "editorial_flow_v1",
        "presentation_mode": "guided_reading",
        "widget_family": "reader_prose",
        "motion_preset": "calm",
        "interaction_policy": "guided_focus",
        "reading_blocks": [
            {
                "segment_id": "seg-excerpt",
                "segment_kind": "original_excerpt",
                "source_lane": "current_page",
                "page": 1,
                "text": "Agentic search is a ReAct-style retrieval loop.",
                "source_layout_ids": ["layout-1"],
                "source_block_ids": ["block-1"],
                "evidence_ids": ["evidence-1"],
                "meta": {
                    "group_id": "group-a",
                    "group_label": "问题定义",
                    "translation_zh": "Agentic search 可以理解为一种 ReAct 风格的检索循环。",
                },
            },
            {
                "segment_id": "seg-paragraph",
                "segment_kind": "paragraph",
                "source_lane": "authoring_plan",
                "page": 1,
                "text": "这段在说明 agentic search 会主动决定下一步怎么搜。",
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {
                    "group_id": "group-a",
                    "group_label": "问题定义",
                    "reader_role": "teaching_explanation",
                },
            },
            {
                "segment_id": "seg-aside",
                "segment_kind": "aside_content",
                "source_lane": "authoring_plan",
                "page": 1,
                "text": "这里的重点不是多搜一次，而是系统自己决定是否继续搜。",
                "source_layout_ids": [],
                "source_block_ids": [],
                "evidence_ids": [],
                "meta": {
                    "group_id": "group-a",
                    "group_label": "问题定义",
                    "lane": "support",
                    "reader_title": "页边提示",
                },
            },
        ],
        "current_page_spine": {
            "page": 1,
            "owner": "reading_dossier_v2.current_page",
            "primary": True,
            "reading_node_ids": ["node-1"],
            "layout_ids": ["layout-1"],
            "block_ids": ["block-1"],
            "evidence_ids": ["evidence-1"],
            "main_segment_ids": ["seg-excerpt"],
            "meta": {},
        },
        "provenance": {
            "continuity_mode": "current_page_primary_ordered_adjacent_context",
            "adjacent_context_pages": [2],
            "include_adjacent_as_coequal_anchor": False,
            "source_lanes": {"adjacent_pages_meta": {"2": {"summary": "next"}}},
            "meta": {},
        },
        "meta": {
            "reader_opening": {
                "summary": "先抓住作者如何定义 agentic search，再看它和传统 RAG 的差别。",
                "key_points": ["主动决定是否继续搜", "不是静态一次性命中"],
            }
        },
    }


def test_apply_experience_v2_block_rewrite_updates_supported_block_and_preserves_contract():
    artifact_payload = _sample_artifact_payload()

    updated_artifact, rewritten_block = literature_api._apply_experience_v2_block_rewrite_to_artifact(  # pylint: disable=protected-access
        artifact_payload=artifact_payload,
        block_id="seg-paragraph",
        rewritten_text="这段真正想说的是：agentic search 不会只执行固定检索，而是会边看结果边决定下一步怎么搜。",
        rewrite_prompt="把这一段讲得更像老师在带着读。",
    )

    assert rewritten_block["segment_id"] == "seg-paragraph"
    assert rewritten_block["segment_kind"] == "paragraph"
    assert "老师在带着读" not in rewritten_block["text"]
    assert rewritten_block["text"].startswith("这段真正想说的是")
    assert rewritten_block["meta"]["manual_rewrite"]["source"] == "user_prompt"
    assert updated_artifact["meta"]["manual_block_rewrites"][-1]["segment_id"] == "seg-paragraph"

    validation = literature_api._validate_page_artifact_v2_contract(updated_artifact)  # pylint: disable=protected-access
    assert validation["valid"] is True
    assert validation["renderable"] is True


def test_apply_experience_v2_block_rewrite_rejects_unsupported_original_excerpt():
    artifact_payload = _sample_artifact_payload()

    with pytest.raises(ValueError, match="does not support local rewrite"):
        literature_api._apply_experience_v2_block_rewrite_to_artifact(  # pylint: disable=protected-access
            artifact_payload=artifact_payload,
            block_id="seg-excerpt",
            rewritten_text="不应该允许改写原文摘录。",
            rewrite_prompt="随便改一下。",
        )


def test_build_experience_v2_block_rewrite_prompt_payload_keeps_context_compact():
    artifact_payload = _sample_artifact_payload()
    paper = SimpleNamespace(
        title="Agentic Search Overview",
        abstract="This paper compares agentic search and traditional RAG across retrieval control and iterative reasoning.",
    )

    prompt_payload = literature_api._build_experience_v2_block_rewrite_prompt_payload(  # pylint: disable=protected-access
        paper=paper,
        artifact_payload=artifact_payload,
        narrative_brief={
            "focus_page": 1,
            "current_page_main_arc": "先定义 agentic search，再点出它和传统 RAG 的核心差别。",
            "content_strategy": "definition first",
            "presentation_strategy": "guided",
        },
        block_id="seg-paragraph",
        rewrite_prompt="把这一段说得更通俗一点。",
        reader_profile="curious_generalist",
        user_intent="理解概念定义",
    )

    assert prompt_payload["target_block"]["segment_id"] == "seg-paragraph"
    assert prompt_payload["paper"]["title"] == "Agentic Search Overview"
    assert prompt_payload["page_context"]["reader_opening_summary"]
    assert len(prompt_payload["local_context"]["previous_blocks"]) <= 2
    assert len(prompt_payload["local_context"]["next_blocks"]) <= 2
    assert prompt_payload["local_context"]["nearest_original_excerpt"]["segment_id"] == "seg-excerpt"
