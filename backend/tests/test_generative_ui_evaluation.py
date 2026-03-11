import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.generative_reader_agent_runtime import GenerativeReaderAgentRuntime


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "generative_ui"


def _sample_payload():
    return {
        "paper_id": 78,
        "page": 7,
        "scheme_choice": {"scheme_id": "figure_focus_split", "label": "Figure Focus Split"},
        "quality_report": {"overall": 0.88, "layout_monotony": False},
        "assets": [],
        "enrichment_bundle": {
            "version": "v1",
            "targets": [
                {
                    "target_id": "p7:fig-1",
                    "node_id": "fig-1",
                    "target_kind": "figure",
                    "component_type": "FigurePanel",
                    "title": "Fig 3",
                    "excerpt": "Concordance and insight of ChatGPT on USMLE.",
                    "figure_label": "Fig 3",
                    "suggested_resource_types": ["figure_explainer", "related_public_resource"],
                },
                {
                    "target_id": "p7:p-1",
                    "node_id": "p-1",
                    "target_kind": "paragraph",
                    "component_type": "ParagraphProse",
                    "title": "",
                    "excerpt": "We first examined the frequency of insight.",
                    "section_label": "Results",
                    "suggested_resource_types": ["glossary_panel", "related_public_resource"],
                },
            ],
            "resource_modules": [],
            "interaction_modules": [],
            "meta": {},
        },
    }


def _sample_done_plan():
    return {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "story_substrate": {
            "version": "v1",
            "page_id": "p7",
            "main_claims": [
                {"claim_id": "claim_1", "text": "Figure 3 carries the primary result.", "source_node_ids": ["p7:fig-1"]},
            ],
            "evidence_units": [
                {"evidence_id": "e1", "kind": "figure", "role": "primary_visual_evidence", "source_node_ids": ["p7:fig-1"]},
            ],
            "terms_to_explain": [
                {"term": "Concordance", "reason": "metric", "source_node_ids": ["p7:p-1"]},
            ],
            "background_gaps": [
                {"topic": "USMLE structure", "reason": "reader context", "suggested_resource_type": "official_context_links"},
            ],
            "narrative_turns": [
                {"turn_id": "t1", "kind": "key_finding", "label": "Result", "target_ids": ["p7:p-1"]},
            ],
        },
        "page_brief": {
            "version": "v1",
            "page_goal": "Explain the figure first, then connect supporting resources, then unpack terms.",
            "reader_type": "curious_generalist",
            "page_archetype": "figure_explainer",
            "hero_angle": "Use the figure as the anchor for the page.",
            "primary_focus_target_id": "p7:fig-1",
            "secondary_support_target_ids": ["p7:p-1"],
            "reading_path": ["hero_summary", "focus_evidence", "reading_flow", "context_explainer", "supporting_resources", "explore_questions"],
            "interaction_opportunities": ["expand_figure_panels", "open_supporting_resources"],
            "resource_gaps": ["USMLE context"],
            "experience_hooks": ["Figure-first guided tour"],
            "resource_strategy": "Bring in official context before explanatory modules.",
            "storyboard": [
                {"beat_id": "beat_hero", "role": "orient", "section_type": "hero", "title": "开场", "purpose": "先建立这一页的阅读目标。", "target_ids": ["p7:fig-1"], "priority": 1},
                {"beat_id": "beat_focus", "role": "focus_evidence", "section_type": "focus_stage", "title": "拆解这张图", "purpose": "先围绕最关键的图或证据建立理解抓手，不急着把所有信息同时展开。", "target_ids": ["p7:fig-1"], "priority": 2},
                {"beat_id": "beat_read", "role": "read_support", "section_type": "reading_flow", "title": "阅读支撑正文", "purpose": "把清洗后的正文作为主阅读流，避免让辅助卡片替代论文内容本身。", "target_ids": ["p7:fig-1", "p7:p-1"], "priority": 3},
                {"beat_id": "beat_explain", "role": "clarify_terms", "section_type": "explainer_cluster", "title": "读懂关键术语", "purpose": "只解释真正会阻碍理解的术语和指标，不重复正文已表达的结论。", "target_ids": ["p7:p-1"], "priority": 4},
                {"beat_id": "beat_context", "role": "add_context", "section_type": "supporting_resources", "title": "补充背景与上下文", "purpose": "只补充理解当前页真正缺失的外部背景，控制数量，避免资源堆砌。", "target_ids": ["p7:p-1"], "priority": 5},
                {"beat_id": "beat_questions", "role": "test_understanding", "section_type": "question_lab", "title": "继续追问", "purpose": "把当前理解转成少量值得继续追问的问题，而不是再堆一轮摘要。", "target_ids": ["p7:p-1"], "priority": 6},
            ],
            "content_budget": {"max_claim_cards": 2, "max_hooks": 2, "max_resource_modules": 2, "max_explainer_modules": 2, "max_question_modules": 1, "max_widgets": 1},
            "meta": {"include_story_map": False},
        },
        "rationale": ["Anchor the page on the figure, then move outward."],
        "resource_modules": [
            {
                "module_id": "res_1",
                "module_type": "RelatedResourceCard",
                "target_ids": ["p7:fig-1"],
                "title": "Official USMLE context",
                "summary": "Ground the figure in exam structure.",
                "links": [],
                "source": "web",
                "interaction_mode": "stacked_cards",
                "meta": {},
            }
        ],
        "interaction_modules": [
            {
                "module_id": "int_1",
                "module_type": "GlossaryPanel",
                "target_ids": ["p7:p-1"],
                "title": "Key terms",
                "props": {"terms": [{"term": "Concordance", "definition": "Agreement metric."}]},
                "source": "agent",
                "meta": {},
            }
        ],
        "js_widgets": [
            {
                "widget_id": "widget_1",
                "widget_type": "figure-focus-accordion",
                "target_ids": ["p7:fig-1"],
                "title": "Figure walk-through",
                "data_requirements": ["figure_explainer"],
                "props": {"panels": [{"label": "Panel A", "summary": "Primary view."}]},
                "meta": {},
            }
        ],
        "used_tools": ["paper_read", "web_search"],
        "tool_trace": [],
        "meta": {"notes": "test"},
    }


def _load_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _build_generative_snapshot(plan: dict) -> dict:
    page_brief = dict(plan.get("page_brief") or {})
    return {
        "contract_validation": dict((plan.get("meta") or {}).get("contract_validation") or {}),
        "page_archetype": str(page_brief.get("page_archetype") or ""),
        "reading_path": [str(item) for item in list(page_brief.get("reading_path") or [])],
        "storyboard_section_types": [str(row.get("section_type") or "") for row in list(page_brief.get("storyboard") or [])],
        "content_budget": dict(page_brief.get("content_budget") or {}),
        "resource_module_types": [str(row.get("module_type") or "") for row in list(plan.get("resource_modules") or [])],
        "interaction_module_types": [str(row.get("module_type") or "") for row in list(plan.get("interaction_modules") or [])],
        "widget_types": [str(row.get("widget_type") or "") for row in list(plan.get("js_widgets") or [])],
        "used_tools": [str(item) for item in list(plan.get("used_tools") or [])],
    }


def _build_experience_snapshot(plan: dict) -> dict:
    sections = list(plan.get("main_sections") or [])
    block_protocol = {}
    for section in sections:
        section_type = str(section.get("section_type") or "")
        rows = [
            {
                "block_type": str(block.get("block_type") or ""),
                "ref_id": str(block.get("ref_id") or ""),
                "state": str(block.get("state") or ""),
                "user_actions": [str(item) for item in list(block.get("user_actions") or [])],
                "agent_actions": [str(item) for item in list(block.get("agent_actions") or [])],
            }
            for block in list(section.get("blocks") or [])
        ]
        if rows:
            block_protocol[section_type] = rows
    return {
        "contract_validation": dict((plan.get("meta") or {}).get("contract_validation") or {}),
        "layout_variant": str(plan.get("layout_variant") or ""),
        "section_order": [str(section.get("section_type") or "") for section in sections],
        "section_regions": {
            str(section.get("section_type") or ""): str(section.get("section_region") or "")
            for section in sections
        },
        "display_copy_contract": str((plan.get("meta") or {}).get("display_copy_contract") or ""),
        "hero_focus_label": str((plan.get("hero") or {}).get("focus_label") or ""),
        "block_protocol": block_protocol,
    }


def test_golden_pages_fixture_should_cover_required_categories():
    payload = _load_json("golden_pages.json")
    goldens = list(payload.get("goldens") or [])

    assert payload["version"] == "phase6_v1"
    assert goldens
    categories = {str(row.get("category") or "") for row in goldens}
    assert {"figure-heavy", "methods-heavy", "concept-heavy"} <= categories
    assert any(str(row.get("source_kind") or "") == "paper_page" for row in goldens)
    assert all(isinstance(row.get("required_labels"), dict) and row["required_labels"] for row in goldens)


def test_generative_plan_snapshot_should_match_fixture():
    runtime = GenerativeReaderAgentRuntime()

    validated = runtime._finalize_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        parsed=_sample_done_plan(),
        used_tools=_sample_done_plan()["used_tools"],
        tool_trace=_sample_done_plan()["tool_trace"],
    )

    assert _build_generative_snapshot(validated) == _load_json("generative_plan_snapshot.json")


def test_experience_plan_snapshot_should_match_fixture():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    validated = runtime._finalize_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=payload["enrichment_bundle"],
        parsed=_sample_done_plan(),
        used_tools=_sample_done_plan()["used_tools"],
        tool_trace=_sample_done_plan()["tool_trace"],
    )

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=validated,
    )

    assert _build_experience_snapshot(experience) == _load_json("experience_plan_snapshot.json")
