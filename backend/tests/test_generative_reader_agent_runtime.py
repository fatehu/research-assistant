import os
import sys
import asyncio
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import generative_reader_agent_runtime as runtime_module
from app.services.generative_reader_agent_runtime import GenerativeReaderAgentRuntime


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


@pytest.mark.asyncio
async def test_build_plan_should_fallback_when_no_tools(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()
    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: set())

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
    )

    assert result["status"] == "fallback"
    assert result["shell_mode"] == "resource_augmented_reader"
    assert any(item["module_type"] == "FigureExplainPanel" for item in result["resource_modules"])
    assert any(item["module_type"] == "GlossaryPanel" for item in result["interaction_modules"])
    glossary = next(item for item in result["interaction_modules"] if item["module_type"] == "GlossaryPanel")
    assert isinstance(glossary["props"].get("terms"), list)
    assert glossary["props"]["terms"]
    assert result["story_substrate"]["page_id"] == "p7"
    assert result["story_substrate"]["main_claims"]
    assert result["page_brief"]["primary_focus_target_id"]
    assert result["page_brief"]["reading_path"]
    assert result["page_brief"]["page_archetype"] == "figure_explainer"
    assert result["page_brief"]["hero_angle"]
    assert result["page_brief"]["experience_hooks"]
    assert result["page_brief"]["resource_strategy"]


@pytest.mark.asyncio
async def test_build_plan_fallback_should_emit_question_list(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()
    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: set())

    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"].append(
        {
            "target_id": "p7:s-1",
            "node_id": "sec-1",
            "target_kind": "section",
            "component_type": "SectionHeading",
            "title": "Results",
            "excerpt": "Insights and concordance across exam types.",
            "section_label": "Results",
            "suggested_resource_types": ["question_starter"],
        }
    )

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=payload,
    )

    question_panel = next(item for item in result["interaction_modules"] if item["module_type"] == "QuestionStarterPanel")
    assert isinstance(question_panel["props"].get("questions"), list)
    assert question_panel["props"]["questions"]
    assert isinstance(question_panel["props"].get("qa_pairs"), list)
    assert question_panel["props"]["qa_pairs"]


@pytest.mark.asyncio
async def test_build_plan_should_surface_model_not_found_from_agent_error_event(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: {"paper_read"})
    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime_module, "build_generative_reader_tool_registry", lambda **_kwargs: object())

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            yield {
                "type": "error",
                "data": "Error code: 404 - {'error': {'message': 'The model `qwen-3.5-plus` does not exist or you do not have access to it.', 'code': 'model_not_found'}}",
            }
            yield {"type": "done", "data": {}}

    monkeypatch.setattr(runtime_module, "GenerativeReaderAgentCore", _FakeAgent)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
    )

    assert result["status"] == "fallback"
    assert result["meta"]["fallback_reason"] == "model_not_found"
    assert "agent_error_message" in result["meta"]


def test_build_agent_prompt_should_allow_autonomous_tool_choice():
    runtime = GenerativeReaderAgentRuntime()

    prompt = runtime._build_agent_prompt(  # pylint: disable=protected-access
        page=7,
        user_intent="围绕正文补充公开资源并生成交互模块",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        compose_payload=_sample_payload(),
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页承接段落",
            }
        ],
    )

    assert "Tools are optional. Choose them autonomously" in prompt
    assert "Use the smallest useful set of tools" in prompt
    assert "use web_scrape when it materially improves confidence" in prompt
    assert "strongly consider attaching 1-3 authoritative public resources" in prompt
    assert "figure-focus accordion panels without summaries" in prompt
    assert "Keep the plan compact" in prompt
    assert "Do not call the same tool repeatedly for near-duplicate queries" in prompt
    assert "reply in Simplified Chinese" in prompt
    assert "generate all user-facing copy in Simplified Chinese" in prompt
    assert "reader_grounding_hints=" in prompt
    assert "knowledge_search_query" in prompt
    assert "story_substrate" in prompt
    assert "page_brief" in prompt
    assert "adjacent_page_context=" in prompt
    assert '"relation": "previous_page"' in prompt
    assert "reference-only continuity context" in prompt


def test_finalize_plan_should_record_adjacent_page_context_meta():
    runtime = GenerativeReaderAgentRuntime()

    finalized = runtime._finalize_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="围绕正文生成体验页",
        compose_payload=_sample_payload(),
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        parsed={
            "status": "done",
            "story_substrate": {},
            "page_brief": {},
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "meta": {},
        },
        used_tools=["paper_read"],
        tool_trace=[],
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页参考",
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "下一页参考",
            },
        ],
    )

    assert finalized["meta"]["adjacent_page_context"] == [
        {
            "page": 6,
            "relation": "previous_page",
            "reference_only": True,
            "source": "vlflash_page_ocr",
        },
        {
            "page": 8,
            "relation": "next_page",
            "reference_only": True,
            "source": "vlflash_page_ocr",
        },
    ]


def test_build_experience_plan_should_follow_reading_path_order():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    section_types = [row["section_type"] for row in experience["main_sections"]]
    assert experience["layout_variant"] == "focus_figure_split"
    assert section_types[:5] == [
        "hero",
        "focus_stage",
        "reading_flow",
        "explainer_cluster",
        "supporting_resources",
    ]
    regions = {row["section_type"]: row["section_region"] for row in experience["main_sections"]}
    assert regions["hero"] == "main"
    assert regions["focus_stage"] == "main"
    assert regions["reading_flow"] == "main"
    assert regions["supporting_resources"] == "sidebar"
    assert regions["explainer_cluster"] == "sidebar"
    assert "question_lab" not in section_types
    assert "story_map" not in section_types


def test_build_experience_plan_should_choose_explainer_first_for_concept_decoder():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "page_archetype": "concept_decoder",
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    assert experience["layout_variant"] == "explainer_first"
    regions = {row["section_type"]: row["section_region"] for row in experience["main_sections"]}
    assert regions["supporting_resources"] == "sidebar"
    assert regions["explainer_cluster"] == "main"


def test_build_experience_plan_should_bind_modules_by_section_ids():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}

    assert sections["focus_stage"]["resource_module_ids"] == []
    assert sections["focus_stage"]["interaction_module_ids"] == []
    assert sections["focus_stage"]["widget_ids"] == ["widget_1"]
    assert sections["supporting_resources"]["resource_module_ids"] == ["res_1"]
    assert sections["explainer_cluster"]["interaction_module_ids"] == ["int_1"]
    assert "question_lab" not in sections


def test_build_experience_plan_should_assign_modules_to_single_sections():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    resource_occurrences: dict[str, int] = {}
    interaction_occurrences: dict[str, int] = {}
    widget_occurrences: dict[str, int] = {}
    for section in experience["main_sections"]:
        for module_id in list(section.get("resource_module_ids") or []):
            resource_occurrences[module_id] = resource_occurrences.get(module_id, 0) + 1
        for module_id in list(section.get("interaction_module_ids") or []):
            interaction_occurrences[module_id] = interaction_occurrences.get(module_id, 0) + 1
        for widget_id in list(section.get("widget_ids") or []):
            widget_occurrences[widget_id] = widget_occurrences.get(widget_id, 0) + 1

    assert all(count == 1 for count in resource_occurrences.values())
    assert all(count == 1 for count in interaction_occurrences.values())
    assert all(count == 1 for count in widget_occurrences.values())


def test_build_experience_plan_should_emit_unified_section_blocks():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    focus_blocks = sections["focus_stage"]["blocks"]
    explainer_blocks = sections["explainer_cluster"]["blocks"]
    resource_blocks = sections["supporting_resources"]["blocks"]

    assert [row["block_type"] for row in focus_blocks] == ["widget"]
    assert [row["ref_id"] for row in focus_blocks] == ["widget_1"]
    assert [row["version"] for row in focus_blocks] == ["block_ref_v1"]
    assert focus_blocks[0]["user_actions"] == ["expand_panel", "focus_target"]
    assert focus_blocks[0]["agent_actions"] == ["sync_focus_stage", "highlight_evidence_anchor"]
    assert [row["action_type"] for row in focus_blocks[0]["ui_actions"]] == ["expand_panel", "focus_target"]
    assert [row["event_name"] for row in focus_blocks[0]["ui_actions"]] == ["block.expand_panel", "block.focus_target"]
    assert focus_blocks[0]["event_bindings"][-1]["event_source"] == "agent"
    assert focus_blocks[0]["event_bindings"][-1]["event_type"] == "agent_action"
    assert [row["block_type"] for row in explainer_blocks] == ["interaction_module"]
    assert [row["ref_id"] for row in explainer_blocks] == ["int_1"]
    assert explainer_blocks[0]["user_actions"] == ["expand_definition", "return_to_reader", "focus_target"]
    assert explainer_blocks[0]["agent_actions"] == ["ground_term_definition", "preserve_reader_context", "sync_focus_stage"]
    assert explainer_blocks[0]["ui_actions"][0]["label"] == "展开术语解释"
    assert explainer_blocks[0]["event_bindings"][0]["event_source"] == "user"
    assert [row["block_type"] for row in resource_blocks] == ["resource_module"]
    assert [row["ref_id"] for row in resource_blocks] == ["res_1"]
    assert [row["version"] for row in resource_blocks] == ["block_ref_v1"]
    assert resource_blocks[0]["user_actions"] == ["inspect_source"]
    assert resource_blocks[0]["agent_actions"] == ["retrieve_supporting_resource", "summarize_resource_relevance", "sync_focus_stage"]
    assert resource_blocks[0]["ui_actions"][0]["label"] == "查看来源"
    assert resource_blocks[0]["event_bindings"][-1]["payload"]["agent_actions"] == [
        "retrieve_supporting_resource",
        "summarize_resource_relevance",
        "sync_focus_stage",
    ]


def test_build_experience_plan_should_preserve_block_state_contract():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["interaction_modules"] = [
        {
            "module_id": "int_loading",
            "module_type": "GlossaryPanel",
            "target_ids": ["p7:p-1"],
            "title": "Loading glossary",
            "props": {"terms": []},
            "source": "agent",
            "meta": {"state": "loading"},
        },
        {
            "module_id": "int_partial",
            "module_type": "QuestionStarterPanel",
            "target_ids": ["p7:p-1"],
            "title": "Partial questions",
            "props": {"questions": ["Why does this figure matter?"]},
            "source": "agent",
            "meta": {"state": "partial"},
        },
    ]

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    explainer_blocks = sections["explainer_cluster"]["blocks"]
    question_blocks = sections["question_lab"]["blocks"]

    assert explainer_blocks[0]["state"] == "loading"
    assert question_blocks[0]["state"] == "partial"


def test_build_experience_plan_should_keep_focus_resources_in_top_level_registry():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_focus",
            "module_type": "FigureExplainPanel",
            "target_ids": ["p7:fig-1"],
            "title": "Figure explainer",
            "summary": "Walk the reader through the figure.",
            "links": [],
            "source": "agent",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_context",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "Context reference",
            "summary": "Supplementary context.",
            "links": [],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
    ]

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}

    assert sections["focus_stage"]["resource_module_ids"] == ["res_focus"]
    assert sections["supporting_resources"]["resource_module_ids"] == ["res_context"]
    assert sorted(row["module_id"] for row in experience["supporting_resources"]) == ["res_context", "res_focus"]


def test_build_experience_plan_should_remove_sidebar_for_guided_story_stack():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "page_archetype": "methods_decoder",
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    assert experience["layout_variant"] == "guided_story_stack"
    regions = {row["section_type"]: row["section_region"] for row in experience["main_sections"]}
    assert "sidebar" not in set(regions.values())


def test_build_experience_plan_should_not_surface_english_heavy_claim_in_hero_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {
                "claim_id": "claim_1",
                "text": "adjudicator, as a second-year medical student for Step 1, fourth-year medical student for Step 2CK, and post-graduate year 1 resident for Step 3.",
                "source_target_ids": ["p7:paragraph_15"],
                "strength": "primary",
            }
        ],
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    assert "adjudicator" not in experience["hero"]["subtitle"].lower()
    assert "adjudicator" not in experience["hero"]["summary"].lower()


def test_build_experience_plan_should_emit_display_copy_fields():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    assert experience["hero"]["display_title"]
    assert experience["hero"]["display_subtitle"]
    assert experience["hero"]["display_summary"]
    assert all(str(section.get("display_title") or "").strip() for section in experience["main_sections"])
    assert all(str(section.get("display_summary") or "").strip() for section in experience["main_sections"])
    assert experience["meta"]["display_copy_contract"] == "display_copy_v1"
    assert experience["meta"]["content_budget"]["max_claim_cards"] >= 1


def test_build_experience_plan_should_keep_storyboard_purpose_out_of_user_visible_summary():
    runtime = GenerativeReaderAgentRuntime()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=_sample_done_plan(),
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}

    assert sections["focus_stage"]["meta"]["planner_purpose"] == "先围绕最关键的图或证据建立理解抓手，不急着把所有信息同时展开。"
    assert sections["reading_flow"]["meta"]["planner_purpose"] == "把清洗后的正文作为主阅读流，避免让辅助卡片替代论文内容本身。"
    assert "理解抓手" not in sections["focus_stage"]["display_summary"]
    assert "清洗后的正文" not in sections["reading_flow"]["display_summary"]
    assert "辅助卡片替代论文内容本身" not in sections["reading_flow"]["display_summary"]


def test_build_experience_plan_should_prune_redundant_modules_by_budget():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = plan["resource_modules"] + [
        {
            "module_id": "res_2",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "Background reference",
            "summary": "Extra context 1",
            "links": [],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_3",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "Background reference",
            "summary": "Extra context 2",
            "links": [],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
    ]
    plan["interaction_modules"] = plan["interaction_modules"] + [
        {
            "module_id": "int_2",
            "module_type": "GlossaryPanel",
            "target_ids": ["p7:p-1"],
            "title": "More terms",
            "props": {"terms": [{"term": "USMLE", "definition": "Exam system."}]},
            "source": "agent",
            "meta": {},
        },
        {
            "module_id": "int_3",
            "module_type": "QuestionStarterPanel",
            "target_ids": ["p7:p-1"],
            "title": "More questions",
            "props": {"questions": ["What changes if you ignore the figure?"]},
            "source": "agent",
            "meta": {},
        },
    ]
    plan["js_widgets"] = plan["js_widgets"] + [
        {
            "widget_id": "widget_2",
            "widget_type": "figure-focus-accordion",
            "target_ids": ["p7:fig-1"],
            "title": "Second figure walk-through",
            "data_requirements": ["figure_explainer"],
            "props": {"panels": [{"label": "Panel B", "summary": "Secondary view."}]},
            "meta": {},
        }
    ]

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    assert len(experience["supporting_resources"]) <= 2
    assert len([row for row in experience["interactive_blocks"] if row["module_type"] == "GlossaryPanel"]) <= 2
    assert len([row for row in experience["interactive_blocks"] if row["module_type"] == "QuestionStarterPanel"]) <= 1
    assert len(experience["widget_blocks"]) <= 1


def test_build_story_substrate_should_skip_fragment_like_lead_paragraphs():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"] = [
        {
            "target_id": "p7:figure_27",
            "target_kind": "figure",
            "title": "Fig 3",
            "excerpt": "Concordance and insight of ChatGPT on USMLE.",
            "figure_label": "Fig 3",
        },
        {
            "target_id": "p7:paragraph_15",
            "target_kind": "paragraph",
            "excerpt": "adjudicator, as a second-year medical student for Step 1, fourth-year medical student for Step 2CK, and post-graduate year 1 resident for Step 3.",
        },
        {
            "target_id": "p7:paragraph_17",
            "target_kind": "paragraph",
            "excerpt": "We first examined the frequency (prevalence) of insight. Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
        },
    ]

    substrate = runtime._build_story_substrate(  # pylint: disable=protected-access
        page=7,
        user_intent="",
        enrichment_bundle=payload["enrichment_bundle"],
    )

    assert substrate["main_claims"][0]["source_target_ids"] == ["p7:paragraph_17"]
    assert substrate["main_claims"][0]["text"].startswith("We first examined")


@pytest.mark.asyncio
async def test_build_plan_should_record_web_only_grounding_when_only_web_tools_used(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: {"paper_read", "knowledge_search", "web_search"})
    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime_module, "build_generative_reader_tool_registry", lambda **_kwargs: object())

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            yield {"type": "action", "data": {"tool": "web_search"}}
            yield {
                "type": "answer",
                "data": """{
                    "version":"v1",
                    "status":"done",
                    "shell_mode":"resource_augmented_reader",
                    "rationale":["test"],
                    "resource_modules":[],
                    "interaction_modules":[],
                    "js_widgets":[],
                    "meta":{"notes":"test"}
                }""",
            }
            yield {"type": "done", "data": {}}

    monkeypatch.setattr(runtime_module, "GenerativeReaderAgentCore", _FakeAgent)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
    )

    assert result["status"] == "done"
    assert result["used_tools"] == ["web_search"]
    assert result["meta"]["grounding_strategy"] == "web_only"
    assert result["meta"]["public_resource_grounding"] == "search_only"


@pytest.mark.asyncio
async def test_build_plan_should_use_provided_tool_registry(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()
    provided_registry = object()

    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime_module, "build_generative_reader_tool_registry", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not build default registry")))

    captured = {}

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            captured["tool_registry"] = kwargs.get("tool_registry")
            captured["max_iterations"] = kwargs.get("max_iterations")

        async def run(self, *args, **kwargs):
            yield {
                "type": "answer",
                "data": """{
                    "version":"v1",
                    "status":"done",
                    "shell_mode":"resource_augmented_reader",
                    "rationale":["test"],
                    "resource_modules":[],
                    "interaction_modules":[],
                    "js_widgets":[],
                    "meta":{"notes":"test"}
                }""",
            }
            yield {"type": "done", "data": {}}

    monkeypatch.setattr(runtime_module, "GenerativeReaderAgentCore", _FakeAgent)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
        tool_registry=provided_registry,
        allowed_tool_names=["paper_read"],
    )

    assert result["status"] == "done"
    assert captured["tool_registry"] is provided_registry
    assert captured["max_iterations"] == 4


def test_finalize_plan_should_backfill_story_substrate_and_page_brief():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=[],
        tool_trace=[],
    )

    assert result["story_substrate"]["main_claims"]
    assert result["story_substrate"]["evidence_units"]
    assert result["page_brief"]["page_goal"]
    assert result["page_brief"]["reading_path"]
    assert result["page_brief"]["page_archetype"]
    assert result["page_brief"]["hero_angle"]
    assert result["resource_modules"]
    assert result["interaction_modules"]
    assert result["js_widgets"]
    assert result["page_brief"]["storyboard"]
    assert result["page_brief"]["content_budget"]["max_resource_modules"] >= 1


def test_finalize_plan_should_normalize_invalid_page_brief_contract():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {
                "page_id": "p7",
                "main_claims": [{"claim_id": "", "text": "claim without id"}],
                "evidence_units": [],
                "terms_to_explain": [],
                "background_gaps": [],
                "narrative_turns": [{"turn_id": "", "kind": "", "label": "bad turn"}],
            },
            "page_brief": {
                "page_goal": "",
                "page_archetype": "figure_explainer",
                "reading_path": [],
                "storyboard": [
                    {"section_type": "not_real", "title": "bad"},
                    {"section_type": "focus_stage", "title": "重复一"},
                    {"section_type": "focus_stage", "title": "重复二"},
                ],
                "content_budget": {
                    "max_claim_cards": -3,
                    "max_hooks": "oops",
                    "max_resource_modules": 99,
                },
            },
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=[],
        tool_trace=[],
    )

    assert result["meta"]["contract_validation"]["status"] == "validated"
    assert result["story_substrate"]["main_claims"]
    assert result["page_brief"]["storyboard"]
    assert all(
        row["section_type"] in {"hero", "focus_stage", "reading_flow", "explainer_cluster", "supporting_resources", "question_lab", "story_map"}
        for row in result["page_brief"]["storyboard"]
    )
    assert result["page_brief"]["content_budget"]["max_claim_cards"] >= 0
    assert result["page_brief"]["content_budget"]["max_resource_modules"] <= 4


def test_build_experience_plan_should_use_page_archetype_to_title_sections():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    fallback = runtime._build_fallback_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="围绕正文补充公开资源并生成交互模块",
        enrichment_bundle=payload["enrichment_bundle"],
    )

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="围绕正文补充公开资源并生成交互模块",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=fallback,
    )

    assert experience["hero"]["title"]
    section_titles = {row["section_type"]: row["title"] for row in experience["main_sections"]}
    assert section_titles["focus_stage"] == "拆解这张图"
    assert section_titles["reading_flow"] == "阅读支撑正文"
    assert experience["meta"]["page_archetype"] == "figure_explainer"
    assert experience["meta"]["contract_validation"]["status"] == "validated"


def test_finalize_plan_should_polish_generic_module_copy():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [
                {
                    "module_id": "res_1",
                    "module_type": "FigureExplainPanel",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure explainer",
                    "summary": "Summarize the figure, connect it to the body analysis, and attach related public resources.",
                    "links": [],
                },
                {
                    "module_id": "res_2",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Related resources",
                    "summary": "Attach a small set of public references or background material directly relevant to this passage.",
                    "links": [{"label": "Official USMLE Website", "href": "https://www.usmle.org/"}],
                },
            ],
            "interaction_modules": [
                {
                    "module_id": "int_1",
                    "module_type": "GlossaryPanel",
                    "target_ids": ["p7:p-1"],
                    "title": "Glossary and background",
                    "props": {"terms": [{"term": "Concordance", "definition": "test"}]},
                }
            ],
            "js_widgets": [
                {
                    "widget_id": "wid_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure exploration",
                    "props": {
                        "panels": [
                            {"label": "Panel A: Overall Concordance", "focus": "concordance_metrics"},
                        ]
                    },
                }
            ],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=["web_search"],
        tool_trace=[],
    )

    assert result["resource_modules"][0]["title"] == "如何阅读 Fig 3"
    assert result["resource_modules"][1]["title"] == "USMLE 官方背景"
    assert result["interaction_modules"][0]["title"] == "读懂这一页的关键术语"
    assert result["js_widgets"][0]["title"] == "逐面板理解 Fig 3"
    assert result["js_widgets"][0]["meta"]["story_role"] == "primary_interactive_guide"


def test_finalize_plan_should_localize_generic_english_reader_copy():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {
                "page_id": "p7",
                "background_gaps": [
                    {
                        "topic": "USMLE structure",
                        "reason": "reader may lack context",
                        "suggested_resource_type": "related_public_resource",
                    }
                ],
                "narrative_turns": [
                    {"turn_id": "t1", "kind": "figure_focus", "label": "Primary figure", "target_ids": ["p7:fig-1"]},
                ],
            },
            "page_brief": {
                "page_goal": "Help the reader understand the main claim through the figure and key metrics.",
                "reader_type": "curious_generalist",
                "page_archetype": "figure_explainer",
                "hero_angle": "Start from Fig 3 and use it to interpret the page's core claim: the model performs differently across exam stages.",
                "primary_focus_target_id": "p7:fig-1",
                "secondary_support_target_ids": ["p7:p-1"],
                "reading_path": ["hero_summary", "focus_evidence", "context_explainer", "explore_questions"],
                "interaction_opportunities": ["expand_focus_panels"],
                "resource_gaps": ["USMLE structure"],
                "experience_hooks": [
                    "Start with Fig 3 before reading the supporting passage.",
                    "Use the explainer cards only when a technical term blocks your understanding.",
                    "Open outside context only for USMLE structure, not as a substitute for the paper.",
                ],
                "resource_strategy": "Use 1-3 authoritative public resources to clarify USMLE context without repeating the paper's argument.",
            },
            "resource_modules": [
                {
                    "module_id": "res_1",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Related resources",
                    "summary": "Attach a small set of public references or background material directly relevant to this passage.",
                    "links": [],
                }
            ],
            "interaction_modules": [
                {
                    "module_id": "int_1",
                    "module_type": "QuestionStarterPanel",
                    "target_ids": ["p7:p-1"],
                    "title": "Suggested follow-up questions",
                    "props": {
                        "questions": [
                            "What Fig 3 reveals",
                        ]
                    },
                }
            ],
            "js_widgets": [
                {
                    "widget_id": "wid_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure exploration",
                    "props": {
                        "panels": [
                            {"label": "Figure overview", "summary": "Start with Fig 3 before reading the supporting passage."},
                        ]
                    },
                }
            ],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=["web_search"],
        tool_trace=[],
    )

    assert result["story_substrate"]["background_gaps"][0]["topic"] == "USMLE 结构"
    assert result["story_substrate"]["narrative_turns"][0]["label"] == "主图"
    assert result["page_brief"]["experience_hooks"][0] == "先看 Fig 3，再回到支撑正文。"
    assert result["page_brief"]["experience_hooks"][1] == "只有当技术术语真的卡住理解时，再去看解释卡片。"
    assert result["page_brief"]["experience_hooks"][2] == "只在需要补充 USMLE structure 这类背景时再打开外部资料，不要拿它替代论文。"
    assert result["interaction_modules"][0]["title"] == "接下来值得追问的问题"
    assert result["js_widgets"][0]["props"]["panels"][0]["label"] == "整图概览"
    assert result["js_widgets"][0]["props"]["panels"][0]["summary"] == "Concordance and insight of ChatGPT on USMLE."


def test_finalize_plan_should_materialize_display_copy_contract():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {
                "version": "v1",
                "page_id": "p7",
                "main_claims": [
                    {
                        "claim_id": "claim_1",
                        "text": "Figure 3 carries the primary result.",
                        "source_target_ids": ["p7:fig-1"],
                        "strength": "primary",
                    }
                ],
                "evidence_units": [],
                "terms_to_explain": [],
                "background_gaps": [],
                "narrative_turns": [],
                "meta": {},
            },
            "page_brief": {
                "version": "v1",
                "page_goal": "Help the reader understand the main claim through the figure and key metrics.",
                "reader_type": "curious_generalist",
                "page_archetype": "figure_explainer",
                "hero_angle": "Use the figure as the anchor for the page.",
                "primary_focus_target_id": "p7:fig-1",
                "secondary_support_target_ids": ["p7:p-1"],
                "reading_path": ["hero_summary", "focus_evidence", "supporting_resources", "context_explainer", "explore_questions"],
                "interaction_opportunities": ["expand_focus_panels"],
                "resource_gaps": ["USMLE context"],
                "experience_hooks": ["Figure-first guided tour"],
                "resource_strategy": "Bring in official context before explanatory modules.",
            },
            "resource_modules": [
                {
                    "module_id": "res_1",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Official USMLE context",
                    "summary": "Ground the figure in exam structure.",
                    "links": [],
                }
            ],
            "interaction_modules": [
                {
                    "module_id": "int_1",
                    "module_type": "QuestionStarterPanel",
                    "target_ids": ["p7:p-1"],
                    "title": "Suggested follow-up questions",
                    "props": {"questions": ["What Fig 3 reveals"]},
                }
            ],
            "js_widgets": [
                {
                    "widget_id": "wid_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure exploration",
                    "props": {
                        "panels": [
                            {"label": "Panel A", "focus": "concordance_metrics", "summary": "Highlights answer–explanation agreement."},
                        ]
                    },
                }
            ],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=["web_search"],
        tool_trace=[],
    )

    assert result["meta"]["display_copy_contract"] == "display_copy_v1"
    assert result["story_substrate"]["main_claims"][0]["display_text"] == "Fig 3承载了这一页最值得先看的关键结果。"
    assert result["resource_modules"][0]["display_title"] == "这一段的背景补充"
    assert result["resource_modules"][0]["display_summary"] == "补充少量高相关的外部资源，帮助理解正文，而不是替代正文。"
    assert result["interaction_modules"][0]["display_title"] == "接下来值得追问的问题"
    assert result["interaction_modules"][0]["display_summary"] == "把当前理解转成追问，检查你是否真的读懂了这一页。"
    assert result["js_widgets"][0]["display_title"] == "逐面板理解 Fig 3"
    assert result["js_widgets"][0]["props"]["panels"][0]["display_label"] == "整图概览"
    assert result["js_widgets"][0]["props"]["panels"][0]["display_summary"] == "先把整张图当成进入这一页的主要视觉入口。"


def test_finalize_plan_should_prefer_trusted_resource_domains():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [
                {
                    "module_id": "res_1",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Related resources",
                    "summary": "Attach a small set of public references or background material directly relevant to this passage.",
                    "links": [
                        {"label": "Low value", "href": "https://academically.com/usmle-guide"},
                        {"label": "Official USMLE", "href": "https://www.usmle.org/step-exams/step-1"},
                        {"label": "PubMed", "href": "https://pubmed.ncbi.nlm.nih.gov/37934828/"},
                    ],
                }
            ],
            "interaction_modules": [],
            "js_widgets": [],
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=["web_search"],
        tool_trace=[],
    )

    resource = result["resource_modules"][0]
    assert resource["title"] == "USMLE 官方背景"
    assert resource["meta"]["source_quality"] == "trusted"
    domains = [row["domain"] for row in resource["links"]]
    assert "usmle.org" in domains
    assert "pubmed.ncbi.nlm.nih.gov" in domains
    assert "academically.com" not in domains


@pytest.mark.asyncio
async def test_build_plan_should_mark_search_plus_scrape_grounding(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime_module, "build_generative_reader_tool_registry", lambda **_kwargs: object())

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            yield {"type": "action", "data": {"tool": "paper_read"}}
            yield {"type": "action", "data": {"tool": "web_search"}}
            yield {"type": "action", "data": {"tool": "web_scrape"}}
            yield {
                "type": "answer",
                "data": """{
                    "version":"v1",
                    "status":"done",
                    "shell_mode":"resource_augmented_reader",
                    "rationale":["test"],
                    "resource_modules":[],
                    "interaction_modules":[],
                    "js_widgets":[],
                    "meta":{"notes":"test"}
                }""",
            }
            yield {"type": "done", "data": {}}

    monkeypatch.setattr(runtime_module, "GenerativeReaderAgentCore", _FakeAgent)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
        allowed_tool_names=["paper_read", "web_search", "web_scrape"],
    )

    assert result["status"] == "done"
    assert result["meta"]["grounding_strategy"] == "reader_native_assist"
    assert result["meta"]["public_resource_grounding"] == "search_plus_scrape"


@pytest.mark.asyncio
async def test_build_plan_should_recover_from_timeout_with_compact_formatter(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            return {
                "content": """{
                    "version":"v1",
                    "status":"done",
                    "shell_mode":"resource_augmented_reader",
                    "rationale":["recovered"],
                    "resource_modules":[{"module_id":"res_1","module_type":"RelatedResourceCard","target_ids":["p7:p-1"],"title":"Recovered","summary":"Recovered from trace","links":[],"source":"web","interaction_mode":"stacked_cards","meta":{}}],
                    "interaction_modules":[],
                    "js_widgets":[],
                    "meta":{"notes":"recovered"}
                }"""
            }

    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=_FakeLLM()))
    monkeypatch.setattr(runtime_module, "build_generative_reader_tool_registry", lambda **_kwargs: object())
    monkeypatch.setattr(runtime_module.settings, "generative_reader_agent_timeout_seconds", 1)

    class _TimeoutAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            yield {"type": "action", "data": {"tool": "paper_read", "input": {"query": "fig 3"}}}
            yield {"type": "observation", "data": {"tool": "paper_read", "success": True, "output": "paper observation"}}
            yield {"type": "action", "data": {"tool": "web_search", "input": {"query": "usmle"}}}
            yield {"type": "observation", "data": {"tool": "web_search", "success": True, "output": "search observation"}}
            await asyncio.sleep(11)

    monkeypatch.setattr(runtime_module, "GenerativeReaderAgentCore", _TimeoutAgent)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
        allowed_tool_names=["paper_read", "web_search"],
    )

    assert result["status"] == "done"
    assert result["resource_modules"][0]["title"] == "Recovered"
    assert result["meta"]["recovered_from"] == "agent_timeout"
    assert result["meta"]["public_resource_grounding"] == "search_only"


def test_finalize_plan_should_fill_missing_accordion_panel_summary():
    runtime = GenerativeReaderAgentRuntime()

    parsed = {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "rationale": [],
        "resource_modules": [],
        "interaction_modules": [],
        "js_widgets": [
            {
                "widget_id": "widget_001",
                "widget_type": "figure-focus-accordion",
                "target_ids": ["p7:fig-1"],
                "title": "Explore Figure 3 Panels",
                "data_requirements": ["figure_explainer"],
                "props": {
                    "panels": [
                        {"label": "Panel A: Overall Concordance", "focus": "concordance_metrics"}
                    ]
                },
                "meta": {},
            }
        ],
        "meta": {},
    }

    finalized = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed=parsed,
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        used_tools=[],
        tool_trace=[],
    )

    panel = finalized["js_widgets"][0]["props"]["panels"][0]
    assert panel["summary"]
    assert panel["label"] == "整图概览"
    assert "concordance and insight of chatgpt on usmle" in panel["summary"].lower()


def test_build_fallback_plan_should_not_invent_panel_abc_sequence():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._build_fallback_plan(  # pylint: disable=protected-access
        page=7,
        user_intent="围绕正文补充公开资源并生成交互模块",
        enrichment_bundle=payload["enrichment_bundle"],
    )

    widget = result["js_widgets"][0]
    panels = list(widget["props"]["panels"])
    assert len(panels) == 1
    assert panels[0]["label"] == "整图概览"
    assert "原始图注有充分依据" in panels[0]["summary"]


def test_finalize_plan_should_replace_generic_panels_with_real_caption_panels_even_when_fewer():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"][0]["full_text"] = (
        "Fig 2. Accuracy of ChatGPT on USMLE. "
        "A: Overall concordance across all exam types and encodings. "
        "B: Overall insight prevalence across exam types and encodings."
    )

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [
                {
                    "widget_id": "wid_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure exploration",
                    "props": {
                        "panels": [
                            {"label": "Panel A: Overall Concordance", "focus": "concordance_metrics"},
                            {"label": "Panel B: Insight by Exam Type", "focus": "insight_breakdown"},
                            {"label": "Panel C: Insight Frequency", "focus": "prevalence_data"},
                        ]
                    },
                }
            ],
        },
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=[],
        tool_trace=[],
    )

    labels = [row["label"] for row in result["js_widgets"][0]["props"]["panels"]]
    assert labels == ["Panel A", "Panel B"]


def test_build_experience_plan_should_promote_page_brief_to_page_sections():
    runtime = GenerativeReaderAgentRuntime()
    compose_payload = _sample_payload()
    generative_plan = {
        "version": "v1",
        "status": "done",
        "shell_mode": "resource_augmented_reader",
        "rationale": ["Center the figure first, then enrich the body passage."],
        "resource_modules": [
            {
                "module_id": "res_1",
                "module_type": "RelatedResourceCard",
                "target_ids": ["p7:fig-1"],
                "title": "Context link",
                "summary": "Helpful public context",
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
                "props": {"terms": [{"term": "Insight", "definition": "Meaningful explanation quality."}]},
                "source": "agent",
                "meta": {},
            }
        ],
        "js_widgets": [
            {
                "widget_id": "widget_1",
                "widget_type": "figure-focus-accordion",
                "target_ids": ["p7:fig-1"],
                "title": "Explore Figure 3",
                "data_requirements": ["figure_explainer"],
                "props": {"panels": [{"label": "Panel A", "summary": "Overall concordance."}]},
                "meta": {},
            }
        ],
        "used_tools": ["paper_read"],
        "story_substrate": {
            "page_id": "p7",
            "main_claims": [{"claim_id": "claim_1", "text": "Figure 3 is the key evidence.", "source_target_ids": ["p7:fig-1"]}],
            "narrative_turns": [{"turn_id": "turn_1", "kind": "key_finding", "label": "Key finding", "target_ids": ["p7:fig-1", "p7:p-1"]}],
            "background_gaps": [{"topic": "USMLE structure", "reason": "reader may lack context"}],
        },
        "page_brief": {
            "version": "v1",
            "page_goal": "Explain the figure before the prose details.",
            "reader_type": "curious_generalist",
            "primary_focus_target_id": "p7:fig-1",
            "secondary_support_target_ids": ["p7:p-1"],
            "reading_path": ["hero_summary", "focus_evidence", "reading_flow", "supporting_resources"],
            "interaction_opportunities": ["expand_focus_panels"],
            "resource_gaps": ["USMLE structure"],
            "meta": {"page": 7},
        },
        "meta": {},
    }

    result = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="Build an explorable page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=compose_payload,
        generative_plan=generative_plan,
    )

    assert result["status"] == "done"
    assert result["hero"]["target_ids"] == ["p7:fig-1"]
    assert result["main_sections"][0]["section_type"] == "hero"
    assert any(section["section_type"] == "focus_stage" for section in result["main_sections"])
    assert any(section["section_type"] == "reading_flow" for section in result["main_sections"])
    assert result["supporting_resources"][0]["module_id"] == "res_1"
    assert result["interactive_blocks"][0]["module_id"] == "int_1"
    assert result["widget_blocks"][0]["widget_id"] == "widget_1"
