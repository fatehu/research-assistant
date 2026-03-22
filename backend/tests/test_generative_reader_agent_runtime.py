import json
import os
import re
import sys
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import generative_reader_agent_runtime as runtime_module
from app.services.generative_reader_agent_runtime import GenerativeReaderAgentRuntime


_READER_FACING_NOISE_MARKERS = (
    "这一拍",
    "重点目标",
    "本段已调用",
    "把刚形成的理解转成后续追问和检查点",
    "Search failed",
    "429",
    "blocked",
    "rate limit",
    "当前位置：首页",
    "威普爱生教育",
    "weproedu",
    "weproedu.com",
    "zhihu.com",
)


def _assert_no_reader_surface_noise(value):
    if isinstance(value, str):
        haystack = value
    else:
        haystack = " ".join(str(item) for item in list(value or []))
    for marker in _READER_FACING_NOISE_MARKERS:
        assert marker not in haystack


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


def _sample_page_dossier():
    return {
        "focus_page": 7,
        "current_page": {
            "page": 7,
            "build_mode": "compose_agent_simplified",
            "pipeline_version": "reader_v2",
            "status": "done",
            "targets": [
                {
                    "target_id": "p7:fig-1",
                    "kind": "figure",
                    "title": "Fig 3",
                    "summary": "Concordance and insight of ChatGPT on USMLE.",
                },
                {
                    "target_id": "p7:p-1",
                    "kind": "paragraph",
                    "title": "Results",
                    "summary": "We first examined the frequency of insight.",
                },
            ],
            "assets": [
                {"kind": "page_render_image", "label": "Page 7 render", "source": "reader_page_asset"},
            ],
            "quality": {"overall": 0.88, "layout_monotony": False},
        },
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页介绍了图的背景。",
                "continuation_hints": ["当前页延续上一页的图示阅读。"],
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
            }
        ],
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
            "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
            "reading_path": ["hero_summary", "focus_evidence", "reading_flow", "context_explainer", "supporting_resources", "explore_questions"],
            "interaction_opportunities": ["expand_figure_panels", "open_supporting_resources"],
            "resource_gaps": ["USMLE context"],
            "experience_hooks": ["Figure-first guided tour"],
            "resource_strategy": "Bring in official context before explanatory modules.",
            "storyboard": [
                {"beat_id": "beat_hero", "role": "orient", "section_type": "hero", "title": "开场", "purpose": "先建立这一页的阅读目标。", "target_ids": ["p7:fig-1"], "priority": 1},
                {"beat_id": "beat_focus", "role": "focus_evidence", "section_type": "focus_stage", "title": "拆解这张图", "purpose": "先围绕最关键的图或证据建立理解抓手，不急着把所有信息同时展开。", "target_ids": ["p7:fig-1"], "priority": 2},
                {"beat_id": "beat_read", "role": "read_support", "section_type": "reading_flow", "title": "完整阅读本页内容", "purpose": "把当前页正文与图表顺序保留下来作为主干，再在其上补充解释和外部资源。", "target_ids": ["p7:fig-1", "p7:p-1"], "priority": 3},
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
    assert result["page_brief"]["body_flow_target_ids"] == ["p7:fig-1", "p7:p-1"]


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
                "summary": "上一页总结",
                "body_text": "上一页承接段落",
                "figures": [{"label": "Figure 1", "description": "说明上一页的主图。"}],
                "tables": [{"label": "Table 1", "description": "说明上一页的表格。"}],
                "equations": [{"label": "(1)", "description": "说明上一页的公式。"}],
                "continuation_hints": ["当前页延续了上一页的方法说明。"],
            }
        ],
        page_dossier={
            "focus_page": 7,
            "current_page": {
                "page": 7,
                "targets": [{"target_id": "p7:figure_1", "kind": "figure", "title": "主图"}],
            },
            "adjacent_page_context": [
                {
                    "page": 6,
                    "relation": "previous_page",
                    "summary": "上一页总结",
                }
            ],
        },
    )

    assert "Tools are optional, but the reading experience is the goal" in prompt
    assert "Use tools intentionally rather than mechanically" in prompt
    assert "use web_scrape when it materially improves confidence" in prompt
    assert "strongly consider attaching 1-3 authoritative public resources" in prompt
    assert "figure-focus accordion panels without summaries" in prompt
    assert "author reader-facing display content that can be shown directly in `/experience`" in prompt
    assert "Use target ids for grounding/provenance, not as a limit on what kind of reader-facing copy can be written" in prompt
    assert "Do not call the same tool repeatedly for near-duplicate queries" in prompt
    assert "reply in Simplified Chinese" in prompt
    assert "generate all user-facing copy in Simplified Chinese" in prompt
    assert "reader_grounding_hints=" in prompt
    assert "knowledge_search_query" in prompt
    assert "story_substrate" in prompt
    assert "page_brief" in prompt
    assert "adjacent_page_context=" in prompt
    assert "page_dossier=" in prompt
    assert "planning_brief=" in prompt
    assert "Treat planning_brief as grounding and sequencing context, not a rigid script" in prompt
    assert '"relation": "previous_page"' in prompt
    assert '"figures": [{"label": "Figure 1", "description": "说明上一页的主图。"}]' in prompt
    assert '"continuation_hints": ["当前页延续了上一页的方法说明。"]' in prompt
    assert '"focus_page": 7' in prompt


def test_build_agent_prompt_should_treat_guided_beats_as_scaffolding_in_page_generation():
    runtime = GenerativeReaderAgentRuntime()

    prompt = runtime._build_agent_prompt(  # pylint: disable=protected-access
        page=7,
        user_intent="把这一页做成直接可展示的体验页",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        compose_payload=_sample_payload(),
        adjacent_page_context=_sample_page_dossier()["adjacent_page_context"],
        page_dossier=_sample_page_dossier(),
        planning_brief={
            "page_archetype_hint": "figure_explainer",
            "guided_beat_seed": _sample_done_plan()["page_brief"]["storyboard"],
        },
        planner_output={
            "page_objective": "Turn the page into a reader-facing explainer.",
            "guided_beats": _sample_done_plan()["page_brief"]["storyboard"],
        },
        tool_enrichment_packet={
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先抓住图中的主要比较，再回正文看作者怎样解释。",
                    "public_links": [{"label": "USMLE overview", "href": "https://www.usmle.org/"}],
                }
            ]
        },
        allow_tool_choice=False,
    )

    assert "This is the page-generation stage. No live tools are available now." in prompt
    assert "Treat planner_output, guided beats, and beat packets as scaffolding for sequencing and emphasis, not as a rigid script." in prompt
    assert "Guided beats can suggest structure, but you may consolidate, expand, or rewrite them into better reader-facing units" in prompt
    assert "Author reader-facing sections and modules directly for `/experience`" in prompt
    assert "Keep public resources few and authoritative; never turn the page into a link dump." in prompt
    assert "This is NOT body extraction. The reading flow already exists." not in prompt
    assert "Never rewrite or replace the body content." not in prompt
    assert "Only target existing enrichment targets." not in prompt
    assert "Treat planner_output.guided_beats and tool_enrichment_packet.beat_packets as the primary guide" not in prompt


def test_compose_beat_native_summary_should_preserve_authored_display_copy_over_packet_scaffold():
    runtime = GenerativeReaderAgentRuntime()
    authored_summary = "这一段直接告诉读者，真正该看的不是绝对高低，而是 Step 2CK 和 Step 3 上洞见频率如何分化。"

    result = runtime._compose_beat_native_summary(  # pylint: disable=protected-access
        beat={
            "section_type": "reading_flow",
            "reader_goal": "按当前页顺序展开结果。",
        },
        packet={
            "tool_objectives": ["why_it_matters"],
            "summary": "先补上理解当前内容需要的背景。",
            "reader_facing_notes": ["外部背景只作为辅助说明，不替代正文主线。"],
        },
        default_summary=authored_summary,
        limit=240,
        prefer_default_if_reader_ready=True,
    )

    assert result == authored_summary


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
                "summary": "上一页参考",
                "body_text": "上一页参考正文",
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "下一页参考",
                "body_text": "下一页参考正文",
            },
        ],
        page_dossier={
            "focus_page": 7,
            "current_page": {"page": 7, "build_mode": "compose_agent_layout_uid_v1"},
        },
        planning_brief={
            "page_archetype_hint": "figure_explainer",
            "tool_hints": ["paper_read", "knowledge_search"],
        },
        runtime_stage_trace=[
            {"stage_id": "dossier_input", "status": "ready"},
            {"stage_id": "planning_brief", "status": "ready"},
        ],
    )

    assert finalized["meta"]["adjacent_page_context"] == [
        {
            "page": 6,
            "relation": "previous_page",
            "reference_only": True,
            "source": "vlflash_page_ocr",
            "summary": "",
            "continuation_hints": [],
            "figure_hints": [],
            "table_hints": [],
            "equation_hints": [],
            "figure_count": 0,
            "table_count": 0,
            "equation_count": 0,
        },
        {
            "page": 8,
            "relation": "next_page",
            "reference_only": True,
            "source": "vlflash_page_ocr",
            "summary": "",
            "continuation_hints": [],
            "figure_hints": [],
            "table_hints": [],
            "equation_hints": [],
            "figure_count": 0,
            "table_count": 0,
            "equation_count": 0,
        },
    ]
    assert finalized["meta"]["page_dossier"]["focus_page"] == 7
    assert finalized["meta"]["page_dossier"]["current_page"]["page"] == 7
    assert finalized["meta"]["page_dossier"]["current_page"]["build_mode"] == "compose_agent_layout_uid_v1"
    assert finalized["meta"]["page_dossier"]["current_page"]["target_ids"] == ["p7:fig-1", "p7:p-1"]
    assert finalized["meta"]["planning_brief"] == {
        "page_archetype_hint": "figure_explainer",
        "tool_hints": ["paper_read", "knowledge_search"],
    }
    assert finalized["meta"]["runtime_stage_trace"] == [
        {"stage_id": "dossier_input", "status": "ready"},
        {"stage_id": "planning_brief", "status": "ready"},
    ]


def test_build_planning_brief_should_capture_continuity_and_tool_hints():
    runtime = GenerativeReaderAgentRuntime()

    brief = runtime._build_planning_brief(  # pylint: disable=protected-access
        page=7,
        user_intent="帮我生成一个丰富的解释页面",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        page_dossier={
            "focus_page": 7,
            "current_page": {
                "page": 7,
                "targets": [
                    {"target_id": "p7:fig-1", "kind": "figure", "title": "Fig 3"},
                    {"target_id": "p7:p-1", "kind": "paragraph", "title": "Results"},
                ],
            },
        },
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页介绍了方法背景。",
                "figures": [{"label": "Figure 1", "description": "上一页主图。"}],
                "continuation_hints": ["当前页延续了上一页的方法说明。"],
            }
        ],
    )

    assert brief["page_archetype_hint"] == "figure_explainer"
    assert brief["continuity_mode"] == "bridged_sequence"
    assert "paper_read" in brief["tool_hints"]
    assert "web_scrape" in brief["tool_hints"]
    assert "focus_stage" in brief["recommended_sections"]
    assert brief["tool_budget"]["max_tool_requests"] >= 1
    assert brief["tool_budget"]["duplicate_query_policy"] == "exact_query_text"
    assert brief["tool_budget"]["max_reader_native_requests"] >= 1


@pytest.mark.asyncio
async def test_execute_planner_tool_requests_should_enforce_tool_budget():
    runtime = GenerativeReaderAgentRuntime()

    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        async def execute(self, tool_name, **arguments):
            self.calls.append((tool_name, arguments))
            return SimpleNamespace(success=True, output=f"{tool_name}:{arguments}", error=None, data={})

    registry = _FakeRegistry()
    used_tools, tool_trace, enrichment = await runtime._execute_planner_tool_requests(  # pylint: disable=protected-access
        registry=registry,
        allowed_tools=["paper_read", "knowledge_search", "web_search", "web_scrape"],
        planner_output={
                "tool_budget": {
                "max_tool_requests": 4,
                "max_reader_native_requests": 1,
                "max_public_web_requests": 2,
                "allow_web_scrape": False,
                "duplicate_query_policy": "exact_query_text",
                "per_tool_timeout_seconds": 8,
            },
            "tool_requests": [
                {"beat_id": "beat_read", "tool": "paper_read", "arguments": {"query": "figure 3", "top_k": 4}, "reason": "grounding", "priority": "high"},
                {"beat_id": "beat_explain", "tool": "knowledge_search", "arguments": {"query": "figure 3", "top_k": 4}, "reason": "duplicate grounding", "priority": "medium"},
                {"beat_id": "beat_context", "tool": "web_search", "arguments": {"query": "USMLE context", "max_results": 5}, "reason": "context", "priority": "medium"},
                {"beat_id": "beat_context", "tool": "web_scrape", "arguments": {"url": "https://example.com", "formats": ["markdown"]}, "reason": "scrape", "priority": "low"},
            ],
            "guided_beats": [
                {"beat_id": "beat_read", "tool_objectives": ["continuation_bridge"], "title": "完整阅读本页内容", "reader_goal": "读正文"},
                {"beat_id": "beat_explain", "tool_objectives": ["term_explain"], "title": "读懂关键术语", "reader_goal": "解释术语"},
                {"beat_id": "beat_context", "tool_objectives": ["why_it_matters"], "title": "补充背景与上下文", "reader_goal": "补背景"},
            ],
        },
    )

    assert used_tools == ["paper_read", "web_search"]
    assert registry.calls == [
        ("paper_read", {"query": "figure 3", "top_k": 4}),
        ("web_search", {"query": "USMLE context", "max_results": 5}),
    ]
    assert enrichment["budget_summary"]["requested_tool_count"] == 4
    assert enrichment["budget_summary"]["executed_tool_count"] == 2
    assert enrichment["budget_summary"]["planner_requested_tool_count"] == 4
    assert enrichment["budget_summary"]["backfill_request_count"] == 0
    assert enrichment["budget_summary"]["followup_request_count"] == 0
    assert enrichment["budget_summary"]["total_requested_tool_count"] == 4
    assert enrichment["budget_summary"]["executed_requested_tool_count"] == 2
    assert enrichment["budget_summary"]["executed_planner_tool_count"] == 2
    assert enrichment["budget_summary"]["executed_backfill_tool_count"] == 0
    assert enrichment["budget_summary"]["executed_followup_tool_count"] == 0
    assert enrichment["budget_summary"]["suppressed_request_count"] == 2
    assert any(row["reason"] == "max_reader_native_requests" for row in enrichment["budget_events"])
    assert any(row["reason"] == "web_scrape_disabled" for row in enrichment["budget_events"])
    assert any(row["type"] == "action" for row in tool_trace)
    assert any(str(row.get("beat_id") or "").strip() == "beat_read" for row in enrichment["tool_findings"])
    assert any(str(row.get("beat_id") or "").strip() == "beat_context" for row in enrichment["tool_findings"])
    assert any(str(row.get("beat_id") or "").strip() == "beat_read" for row in enrichment["beat_packets"])
    assert any(str(row.get("beat_id") or "").strip() == "beat_context" for row in enrichment["beat_packets"])
    read_packet = next(row for row in enrichment["beat_packets"] if str(row.get("beat_id") or "").strip() == "beat_read")
    context_packet = next(row for row in enrichment["beat_packets"] if str(row.get("beat_id") or "").strip() == "beat_context")
    assert read_packet["summary"] == "先把当前内容放回前后文里。"
    assert "paper_read:{'query': 'figure 3', 'top_k': 4}" not in read_packet["summary"]
    assert read_packet["supporting_points"] == []
    _assert_no_reader_surface_noise(read_packet["summary"])
    assert read_packet["tool_accounting"] == {
        "requested_count": 1,
        "planner_requested_count": 1,
        "backfill_requested_count": 0,
        "followup_requested_count": 0,
        "total_requested_count": 1,
        "executed_count": 1,
        "executed_requested_count": 1,
        "executed_backfill_count": 0,
        "executed_followup_count": 0,
        "executed_planner_count": 1,
    }
    assert read_packet["reader_facing_notes"] == ["读到这里时，留意它和前后段落如何衔接。"]
    _assert_no_reader_surface_noise(read_packet["reader_facing_notes"])
    assert context_packet["summary"] == "先补上理解当前内容需要的背景。"
    assert "web_search:{'query': 'USMLE context', 'max_results': 5}" not in context_packet["summary"]
    assert context_packet["supporting_points"] == []
    _assert_no_reader_surface_noise(context_packet["summary"])
    assert context_packet["tool_accounting"] == {
        "requested_count": 1,
        "planner_requested_count": 1,
        "backfill_requested_count": 0,
        "followup_requested_count": 0,
        "total_requested_count": 1,
        "executed_count": 1,
        "executed_requested_count": 1,
        "executed_backfill_count": 0,
        "executed_followup_count": 0,
        "executed_planner_count": 1,
    }
    assert context_packet["reader_facing_notes"] == ["外部背景只作为辅助说明，不替代正文主线。"]
    _assert_no_reader_surface_noise(context_packet["reader_facing_notes"])


def test_normalize_planner_output_should_preserve_guided_beats_and_attach_beat_ids():
    runtime = GenerativeReaderAgentRuntime()

    planning_brief = runtime._build_planning_brief(  # pylint: disable=protected-access
        page=7,
        user_intent="做成更丰富的生成式阅读网页",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        page_dossier={"focus_page": 7},
        adjacent_page_context=[],
    )

    planner_output = runtime._normalize_planner_output(  # pylint: disable=protected-access
        raw={
            "guided_beats": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "拆解这张图",
                    "reader_goal": "先看最强证据",
                    "continuity_note": "再回到正文主干。",
                    "target_ids": ["p7:fig-1"],
                    "tool_objectives": ["figure_context"],
                    "priority": 2,
                }
            ],
            "tool_requests": [
                {
                    "beat_id": "beat_focus",
                    "tool": "paper_read",
                    "arguments": {"query": "Fig 3 concordance and insight", "top_k": 5},
                    "reason": "Ground the page in the paper's own evidence.",
                    "priority": "high",
                }
            ],
        },
        planning_brief=planning_brief,
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        allowed_tools=["paper_read", "knowledge_search", "web_search"],
    )

    assert planner_output["guided_beats"]
    assert planner_output["guided_beats"][0]["beat_id"] == "beat_focus"
    assert planner_output["guided_beats"][0]["tool_objectives"] == ["figure_context"]
    assert planner_output["tool_requests"][0]["beat_id"] == "beat_focus"


def test_derive_default_planner_tool_requests_should_include_public_web_context():
    runtime = GenerativeReaderAgentRuntime()

    planning_brief = runtime._build_planning_brief(  # pylint: disable=protected-access
        page=7,
        user_intent="做成更丰富的生成式阅读网页",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        page_dossier={
            "focus_page": 7,
            "paper_title": "Large language models achieve high USMLE concordance",
            "current_page": {
                "page": 7,
                "summary": "Results page about Figure 3 and USMLE insight patterns.",
                "targets": [
                    {
                        "target_id": "p7:fig-1",
                        "kind": "figure",
                        "title": "Fig 3",
                        "summary": "Concordance and insight of ChatGPT on USMLE.",
                    },
                    {
                        "target_id": "p7:p-1",
                        "kind": "paragraph",
                        "title": "Results",
                        "summary": "We first examined the frequency of insight.",
                    },
                ],
            },
        },
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页介绍了图的背景。",
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
                "continuation_hints": ["当前页延续上一页的图示阅读。"],
            }
        ],
    )

    requests = runtime._derive_default_planner_tool_requests(  # pylint: disable=protected-access
        planning_brief=planning_brief,
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        allowed_tools=["paper_read", "knowledge_search", "web_search"],
    )

    tools = [row["tool"] for row in requests]
    web_queries = [row["arguments"]["query"] for row in requests if row["tool"] == "web_search"]
    assert tools[:2] == ["paper_read", "knowledge_search"]
    assert "web_search" in tools
    assert "Fig 3" in planning_brief["page_dossier_topics"]
    assert any("Large language models achieve high USMLE concordance" in query for query in web_queries)
    assert any("拆解这张图" in query and "Fig 3" in query and "Concordance and insight of ChatGPT on USMLE" in query for query in web_queries)
    assert any("完整阅读本页内容" in query and "Results" in query for query in web_queries)
    assert any("Results page about Figure 3 and USMLE insight patterns" in query for query in web_queries)
    assert "{focus_label} official explanation" not in web_queries
    assert "{focus_label} intuitive explanation figure" not in web_queries
    assert "Fig 3 official explanation" not in web_queries
    assert "Fig 3 intuitive explanation figure" not in web_queries
    assert "Fig 3 background overview" not in web_queries


def test_build_reader_facing_beat_enrichment_should_summarize_findings_and_notes():
    runtime = GenerativeReaderAgentRuntime()

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["figure_context", "why_it_matters"],
            "requested_tools": [
                {"tool": "paper_read"},
                {"tool": "web_search"},
            ],
            "tool_findings": [
                {
                    "tool": "paper_read",
                    "success": True,
                    "output_excerpt": "Figure 3 shows that concordance stays high while insight frequency varies by exam step.",
                },
                {
                    "tool": "web_search",
                    "success": True,
                    "output_excerpt": "Official USMLE guidance clarifies the exam-step differences referenced by the figure.",
                },
            ],
            "public_links": [
                {"label": "USMLE overview", "href": "https://www.usmle.org/"},
            ],
        }
    )

    assert enrichment["summary"].startswith("先抓住图里最值得注意的信息。")
    assert "Official USMLE guidance clarifies the exam-step differences referenced by the figure." in enrichment["summary"]
    _assert_no_reader_surface_noise(enrichment["summary"])
    assert enrichment["supporting_points"] == [
        "先看图里最关键的一点：Figure 3 shows that concordance stays high while insight frequency varies by exam step.",
        "先把背景补齐：Official USMLE guidance clarifies the exam-step differences referenced by the figure.",
    ]
    assert enrichment["reader_facing_notes"] == [
        "先用图或关键证据建立抓手，再回到正文核对作者的解释。",
        "外部背景只作为辅助说明，不替代正文主线。",
    ]
    _assert_no_reader_surface_noise(enrichment["reader_facing_notes"])


def test_build_reader_facing_beat_enrichment_should_drop_tool_echoes_and_rewrite_generic_link_copy():
    runtime = GenerativeReaderAgentRuntime()

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["why_it_matters"],
            "requested_tools": [
                {"tool": "web_search"},
            ],
            "tool_findings": [
                {
                    "tool": "web_search",
                    "success": True,
                    "output_excerpt": "web_search:{'query': 'USMLE context', 'max_results': 5}",
                },
            ],
            "public_links": [
                {"label": "USMLE Overview", "href": "https://www.usmle.org/", "snippet": "Official overview."},
            ],
        }
    )

    assert enrichment["summary"] == "先补上理解当前内容需要的背景。"
    assert "web_search:{'query': 'USMLE context', 'max_results': 5}" not in enrichment["summary"]
    _assert_no_reader_surface_noise(enrichment["summary"])
    assert enrichment["supporting_points"] == [
        "先把背景补齐：这份公开资料 帮助补上理解当前内容所需的官方背景。"
    ]
    assert all("Official overview." not in point for point in enrichment["supporting_points"])
    _assert_no_reader_surface_noise(enrichment["supporting_points"])


def test_build_reader_facing_beat_enrichment_should_prioritize_objective_and_scrape_over_raw_search():
    runtime = GenerativeReaderAgentRuntime()

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["why_it_matters", "external_comparison"],
            "requested_tools": [
                {"tool": "web_search"},
                {"tool": "web_scrape"},
            ],
            "tool_findings": [
                {
                    "tool": "web_search",
                    "success": True,
                    "output_excerpt": "A quick overview mentions the three-step exam structure.",
                },
                {
                    "tool": "web_scrape",
                    "success": True,
                    "output_excerpt": "The official USMLE overview explains how Step 1, Step 2, and Step 3 differ and why the figure separates them.",
                },
            ],
            "public_links": [
                {"label": "USMLE Overview", "href": "https://www.usmle.org/", "snippet": "Official overview."},
            ],
        }
    )

    assert enrichment["summary"] == "先补上理解当前内容需要的背景。"
    _assert_no_reader_surface_noise(enrichment["summary"])
    assert enrichment["supporting_points"][0].startswith("先把背景补齐：")
    assert "The official USMLE overview explains how Step 1, Step 2, and Step 3 differ" in enrichment["supporting_points"][0]
    assert enrichment["supporting_points"][1].startswith("先把背景补齐：")
    assert "A quick overview mentions the three-step exam structure." in enrichment["supporting_points"][1]
    assert enrichment["supporting_points"][2] == "先把背景补齐：这份公开资料 帮助补上理解当前内容所需的官方背景。"
    _assert_no_reader_surface_noise(enrichment["supporting_points"])
    assert enrichment["tool_accounting"] == {
        "requested_count": 2,
        "planner_requested_count": 2,
        "backfill_requested_count": 0,
        "followup_requested_count": 0,
        "total_requested_count": 2,
        "executed_count": 2,
        "executed_requested_count": 2,
        "executed_backfill_count": 0,
        "executed_followup_count": 0,
        "executed_planner_count": 2,
    }


def test_build_reader_facing_beat_enrichment_should_strip_diagnostics_json_and_html():
    runtime = GenerativeReaderAgentRuntime()

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["why_it_matters"],
            "requested_tools": [{"tool": "web_scrape"}],
            "tool_findings": [
                {
                    "tool": "web_scrape",
                    "success": True,
                    "output_excerpt": '[检索诊断] domain=researchgate.net {"markdown":"<div><p>Official USMLE overview explains the three-step exam structure.</p></div>","diagnostics":"drop this"}',
                }
            ],
            "public_links": [
                {"label": "Official USMLE", "href": "https://www.usmle.org/", "snippet": "<p>Step overview.</p>"},
            ],
        }
    )

    assert enrichment["summary"] == "先补上理解当前内容需要的背景。"
    assert all("[检索诊断]" not in point for point in enrichment["supporting_points"])
    assert all('{"markdown"' not in point for point in enrichment["supporting_points"])
    assert all("<div>" not in point and "<p>" not in point for point in enrichment["supporting_points"])
    assert enrichment["supporting_points"][0] == "先把背景补齐：Official USMLE overview explains the three-step exam structure."
    assert enrichment["supporting_points"][1] == "先把背景补齐：这份公开资料 帮助补上理解当前内容所需的官方背景。"
    _assert_no_reader_surface_noise(enrichment["summary"])
    _assert_no_reader_surface_noise(enrichment["supporting_points"])


def test_extract_tool_output_excerpt_should_strip_sample_caption_noise_from_paper_read():
    runtime = GenerativeReaderAgentRuntime()

    result = SimpleNamespace(
        success=True,
        output="ignored",
        error=None,
        data={
            "quality": "ok",
            "results": [
                {
                    "score": 0.82,
                    "content": (
                        "Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
                        "AI outputs were adjudicated on concordance and density of insight (DOI) based on the ACI "
                        "scoring system provided in S2 Data. A: Overall concordance across all exam types and "
                        "question encoding formats. B: Concordance rates stratified between accurate vs inaccurate "
                        "outputs. C: Overall insight prevalence. D: DOI stratified between accurate vs inaccurate "
                        "outputs. https://doi.org/10.1371/journal.pdig.0000198.g003 PLOS Digital Health 7 / 12 "
                        "Across all exam types, we observed that mean DOI was significantly higher in question "
                        "items answered accurately versus inaccurately (0.458 versus 0.199, p <0.0001). "
                        "The high frequency and moderate density of insights indicate that it may be possible for "
                        "a target learner to gain new or nonobvious insights from these explanations."
                    ),
                }
            ],
        },
    )

    excerpt = runtime._extract_tool_output_excerpt(  # pylint: disable=protected-access
        tool_name="paper_read",
        result=result,
    )

    assert "Fig 3." not in excerpt
    assert "A:" not in excerpt
    assert "https://doi.org" not in excerpt
    assert "PLOS Digital Health" not in excerpt
    assert "target learner" in excerpt or "mean DOI was significantly higher" in excerpt


def test_build_reader_facing_beat_enrichment_should_downrank_caption_blocks_and_duplicates():
    runtime = GenerativeReaderAgentRuntime()

    noisy_caption = (
        "Fig 3 Fig 3. Concordance and insight of ChatGPT on USMLE. For USMLE Steps 1, 2CK, and 3, "
        "AI outputs were adjudicated on concordance and density of insight (DOI). A: Overall concordance "
        "across all exam types. B: Concordance rates stratified between accurate vs inaccurate outputs. "
        "C: Overall insight prevalence. D: DOI stratified between accurate vs inaccurate outputs. "
        "https://doi.org/10.1371/journal.pdig.0000198.g003 PLOS Digital Health 7 / 12 "
        "The high frequency and moderate density of insights indicate that it may be possible for a target "
        "learner to gain new or nonobvious insights from these explanations."
    )

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["figure_context", "why_it_matters"],
            "requested_tools": [
                {"tool": "paper_read"},
                {"tool": "web_search"},
                {"tool": "web_scrape"},
            ],
            "tool_findings": [
                {
                    "tool": "paper_read",
                    "success": True,
                    "output_excerpt": noisy_caption,
                },
                {
                    "tool": "web_search",
                    "success": True,
                    "output_excerpt": noisy_caption,
                },
                {
                    "tool": "web_scrape",
                    "success": True,
                    "output_excerpt": (
                        "The official USMLE overview explains why Step 1, Step 2 CK, and Step 3 are evaluated "
                        "separately, which makes the figure easier to interpret."
                    ),
                },
            ],
            "public_links": [
                {
                    "label": "USMLE Overview",
                    "href": "https://www.usmle.org/",
                    "snippet": "Official overview of the three-step exam sequence.",
                }
            ],
        }
    )

    assert enrichment["summary"].startswith("先抓住图里最值得注意的信息。")
    assert "Fig 3" not in enrichment["summary"]
    assert "A:" not in enrichment["summary"]
    assert "https://doi.org" not in enrichment["summary"]
    assert enrichment["supporting_points"][0].startswith("先看图里最关键的一点：")
    assert "target learner" in enrichment["supporting_points"][0]
    assert all("Fig 3" not in point for point in enrichment["supporting_points"])
    assert all("https://doi.org" not in point for point in enrichment["supporting_points"])
    assert len(enrichment["supporting_points"]) == 2
    assert any(
        point.startswith("先把背景补齐：") and "USMLE" in point
        for point in enrichment["supporting_points"]
    )
    _assert_no_reader_surface_noise(enrichment["summary"])
    _assert_no_reader_surface_noise(enrichment["supporting_points"])


def test_best_reader_facing_excerpt_should_drop_exam_prompt_fragment_and_keep_explanation():
    runtime = GenerativeReaderAgentRuntime()

    excerpt = runtime._best_reader_facing_excerpt(  # pylint: disable=protected-access
        "Explain your rationale for each choice before answering. Figure 3 shows that concordance stays high while insight varies by exam step.",
        tool_name="paper_read",
    )

    assert excerpt == "Figure 3 shows that concordance stays high while insight varies by exam step."
    _assert_no_reader_surface_noise(excerpt)


def test_best_reader_facing_excerpt_should_drop_heading_only_titles():
    runtime = GenerativeReaderAgentRuntime()

    excerpt = runtime._best_reader_facing_excerpt(  # pylint: disable=protected-access
        "# Performance of Chat GPT on USMLE",
        tool_name="web_scrape",
    )

    assert excerpt == ""


def test_compose_beat_native_summary_should_fallback_when_packet_summary_is_title_like_or_english_heavy():
    runtime = GenerativeReaderAgentRuntime()

    summary = runtime._compose_beat_native_summary(  # pylint: disable=protected-access
        beat={
            "section_type": "reading_flow",
            "reader_goal": "按页内顺序保留正文主干，把解释附着在原文上。",
            "continuity_note": "这是整页阅读的主骨架。",
        },
        packet={
            "summary": "Performance of Chat GPT on USMLE 帮助把当前内容和前后文串起来。",
            "supporting_points": ["# Performance of Chat GPT on USMLE"],
            "reader_facing_notes": ["读到这里时，留意它和前后段落如何衔接。"],
        },
        default_summary="default",
    )

    assert summary == "按页内顺序保留正文主干，把解释附着在原文上。"
    _assert_no_reader_surface_noise(summary)


def test_compose_beat_native_summary_should_ignore_checkpoint_like_internal_copy():
    runtime = GenerativeReaderAgentRuntime()

    summary = runtime._compose_beat_native_summary(  # pylint: disable=protected-access
        beat={
            "section_type": "question_lab",
            "reader_goal": "把刚形成的理解转成后续追问和检查点。",
            "continuity_note": "这一段应该帮助读者停下来整理理解，而不是继续堆新信息。",
        },
        packet=None,
        default_summary="把当前理解整理成几个值得继续追问的问题。",
    )

    assert summary == "把当前理解整理成几个值得继续追问的问题。"
    _assert_no_reader_surface_noise(summary)


def test_compact_tool_enrichment_packet_for_experience_should_strip_reader_facing_leakage():
    runtime = GenerativeReaderAgentRuntime()

    packet = runtime._compact_tool_enrichment_packet_for_experience(  # pylint: disable=protected-access
        {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "title": "拆解这张图",
                    "reader_goal": "先抓住这一拍里的图示重点。",
                    "tool_objectives": ["figure_context"],
                    "requested_tools": [{"tool": "paper_read"}, {"tool": "web_search"}],
                    "supporting_points": [
                        "Explain your rationale for each choice before answering.",
                        "# Performance of Chat GPT on USMLE",
                    ],
                    "reader_facing_notes": [
                        "把刚形成的理解转成后续追问和检查点。",
                        "本段已调用 paper_read, web_search 来补足解释；请求3次，运行期追加0次，实际执行3次。",
                    ],
                    "tool_findings": [
                        {
                            "tool": "paper_read",
                            "success": True,
                            "output_excerpt": "Explain your rationale for each choice before answering. Figure 3 shows that concordance stays high while insight varies by exam step.",
                        },
                        {
                            "tool": "web_search",
                            "success": True,
                            "output_excerpt": "本段已调用 paper_read, web_search 来补足解释；请求3次，运行期追加0次，实际执行3次。",
                        },
                    ],
                    "public_links": [{"label": "USMLE", "href": "https://www.usmle.org/", "snippet": "Official overview of the exam sequence."}],
                }
            ]
        }
    )

    beat_packet = packet["beat_packets"][0]
    serialized = json.dumps(beat_packet, ensure_ascii=False)
    assert "这一拍" not in serialized
    assert "Explain your rationale" not in serialized
    assert "本段已调用" not in serialized
    assert "实际执行" not in serialized
    assert "# Performance of Chat GPT on USMLE" not in serialized
    _assert_no_reader_surface_noise(serialized)


def test_build_reader_facing_beat_enrichment_should_drop_seo_and_blog_narrative_noise():
    runtime = GenerativeReaderAgentRuntime()

    enrichment = runtime._build_reader_facing_beat_enrichment(  # pylint: disable=protected-access
        beat_packet={
            "tool_objectives": ["method_background", "why_it_matters"],
            "requested_tools": [
                {"tool": "web_scrape"},
                {"tool": "web_search"},
            ],
            "tool_findings": [
                {
                    "tool": "web_scrape",
                    "success": True,
                    "output_excerpt": "新的工作环境让我们都感觉到压力，但只要积极适应就能成长。",
                },
                {
                    "tool": "web_search",
                    "success": True,
                    "output_excerpt": "当前位置：首页 > 医疗类 > USMLE > 威普爱生教育 weproedu.com zhihu.com",
                },
                {
                    "tool": "web_scrape",
                    "success": True,
                    "output_excerpt": "The official USMLE overview explains how Step 1, Step 2, and Step 3 differ, which helps explain why the paper separates them in Figure 3.",
                },
                {
                    "tool": "web_search",
                    "success": True,
                    "request_origin": "backfill",
                    "output_excerpt": "MRCP 代表mebership of royal college of medicine，英国皇家医师的意思。有了MRCP 后就可以在英联邦的一些国家行医。",
                },
            ],
            "public_links": [
                {"label": "USMLE Overview", "href": "https://www.usmle.org/", "snippet": "Official overview of the three-step exam sequence."},
                {"label": "Weproedu", "href": "https://www.weproedu.com/usmle", "snippet": "SEO article."},
                {"label": "baigemed.com", "href": "http://www.baigemed.com/blog/usmle", "snippet": "Personal exam story."},
            ],
        }
    )

    assert enrichment["summary"] == "先补上理解当前内容需要的背景。"
    assert "新的工作环境让我们都感觉到压力" not in " ".join(enrichment["supporting_points"])
    assert "MRCP" not in enrichment["summary"]
    assert "MRCP" not in " ".join(enrichment["supporting_points"])
    _assert_no_reader_surface_noise(enrichment["summary"])
    _assert_no_reader_surface_noise(enrichment["supporting_points"])


def test_recover_plan_deterministically_should_preserve_stage_meta():
    runtime = GenerativeReaderAgentRuntime()

    recovered = runtime._recover_plan_deterministically(  # pylint: disable=protected-access
        page=7,
        user_intent="",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        compose_payload=_sample_payload(),
        tool_trace=[
            {
                "type": "observation",
                "data": {
                    "tool": "paper_read",
                    "success": True,
                    "output": "Figure 3 shows insight prevalence and concordance.",
                    "stage": "tool_enricher",
                    "beat_id": "beat_focus",
                },
            }
        ],
        used_tools=["paper_read", "web_search"],
        planner_output={
            "guided_beats": [
                {"beat_id": "beat_focus", "tool_objectives": ["figure_context"], "title": "拆解这张图", "reader_goal": "先看主图"},
            ],
        },
        tool_enrichment_packet={
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "requested_tools": [{"tool": "paper_read"}],
                    "tool_findings": [{"tool": "paper_read", "success": True, "output_excerpt": "Figure 3 shows insight prevalence and concordance."}],
                    "public_links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                }
            ]
        },
        planning_brief={
            "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
            "guided_beat_seed": [{"beat_id": "beat_read", "section_type": "reading_flow", "target_ids": ["p7:fig-1", "p7:p-1"]}],
        },
        adjacent_page_context=[{"page": 6, "relation": "previous_page"}],
        page_dossier={"focus_page": 7},
        runtime_stage_trace=[{"stage_id": "planner", "status": "exception_repaired"}],
    )

    assert recovered is not None
    meta = recovered["meta"]
    assert meta["planner_output"]["guided_beats"][0]["beat_id"] == "beat_focus"
    assert meta["tool_enrichment_packet"]["beat_packets"][0]["beat_id"] == "beat_focus"
    assert meta["page_dossier"]["focus_page"] == 7
    assert meta["page_dossier"]["current_page"]["page"] == 7
    assert meta["page_dossier"]["current_page"]["target_ids"] == ["p7:fig-1", "p7:p-1"]
    assert meta["adjacent_page_context"][0]["page"] == 6
    assert meta["runtime_stage_trace"] == [{"stage_id": "planner", "status": "exception_repaired"}]


def test_restore_page_brief_guided_reading_contract_should_rebuild_reading_path_from_guided_beats():
    runtime = GenerativeReaderAgentRuntime()

    restored = runtime._restore_page_brief_guided_reading_contract(  # pylint: disable=protected-access
        page_brief={
            "version": "v1",
            "reading_path": ["hero_summary", "focus_evidence"],
            "storyboard": [],
            "body_flow_target_ids": [],
        },
        meta={
            "planner_output": {
                "guided_beats": [
                    {
                        "beat_id": "beat_read",
                        "section_type": "reading_flow",
                        "title": "完整阅读本页内容",
                        "reader_goal": "保留正文主干",
                        "continuity_note": "先顺着正文读，再补解释。",
                        "target_ids": ["p7:fig-1", "p7:p-1"],
                        "priority": 1,
                    },
                    {
                        "beat_id": "beat_context",
                        "section_type": "supporting_resources",
                        "title": "补充背景与上下文",
                        "reader_goal": "补足真正缺失的背景。",
                        "continuity_note": "外部资料只服务于这一页。",
                        "target_ids": ["p7:p-1"],
                        "priority": 2,
                    },
                    {
                        "beat_id": "beat_explain",
                        "section_type": "explainer_cluster",
                        "title": "读懂关键术语",
                        "reader_goal": "解释术语",
                        "continuity_note": "术语解释紧贴正文。",
                        "target_ids": ["p7:p-1"],
                        "priority": 3,
                    },
                ]
            }
        },
    )

    assert restored["reading_path"] == ["reading_flow", "supporting_resources", "context_explainer"]


def test_finalize_plan_should_apply_beat_native_display_copy_from_guidance_meta():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    sample_plan = _sample_done_plan()
    focus_summary = "先抓住图中的主要比较，再回到正文看作者如何解释差异。"
    explain_summary = "先把 Concordance 和 Insight 讲清楚，再回到正文继续读。"
    context_summary = "补一层 USMLE 背景，帮助读懂图里为什么要分 Step 1、2CK 和 3。"

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": sample_plan["story_substrate"],
            "page_brief": sample_plan["page_brief"],
            "resource_modules": [
                {
                    "module_id": "res_focus",
                    "module_type": "FigureExplainPanel",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure explainer",
                    "summary": "",
                    "links": [],
                    "source": "fallback",
                    "interaction_mode": "expandable_sidecar",
                    "meta": {},
                },
                {
                    "module_id": "res_context",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Related resources",
                    "summary": "",
                    "links": [],
                    "source": "fallback",
                    "interaction_mode": "stacked_cards",
                    "meta": {},
                },
            ],
            "interaction_modules": [
                {
                    "module_id": "int_1",
                    "module_type": "GlossaryPanel",
                    "target_ids": ["p7:p-1"],
                    "title": "Glossary and background",
                    "props": {"terms": [{"term": "Concordance", "definition": "Agreement metric."}]},
                    "source": "fallback",
                    "meta": {},
                }
            ],
            "js_widgets": [
                {
                    "widget_id": "widget_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:fig-1"],
                    "title": "Figure exploration",
                    "data_requirements": ["figure_explainer"],
                    "props": {"panels": [{"label": "Panel A", "summary": "Primary view."}]},
                    "meta": {},
                }
            ],
            "meta": {
                "planner_output": {
                    "guided_beats": [
                        {
                            "beat_id": "beat_focus",
                            "role": "focus_evidence",
                            "section_type": "focus_stage",
                            "title": "拆解这张图",
                            "reader_goal": "先看主图，再回正文。",
                            "continuity_note": "先抓住最强证据，再看作者如何展开。",
                            "target_ids": ["p7:fig-1"],
                            "tool_objectives": ["figure_context"],
                            "priority": 2,
                        },
                        {
                            "beat_id": "beat_explain",
                            "role": "clarify_terms",
                            "section_type": "explainer_cluster",
                            "title": "读懂关键术语",
                            "reader_goal": "解释术语",
                            "continuity_note": "解释紧贴刚读过的正文。",
                            "target_ids": ["p7:p-1"],
                            "tool_objectives": ["term_explain"],
                            "priority": 4,
                        },
                        {
                            "beat_id": "beat_context",
                            "role": "add_context",
                            "section_type": "supporting_resources",
                            "title": "补充背景与上下文",
                            "reader_goal": "补背景",
                            "continuity_note": "外部资料只服务于这页理解。",
                            "target_ids": ["p7:p-1"],
                            "tool_objectives": ["why_it_matters"],
                            "priority": 5,
                        },
                    ]
                },
                "tool_enrichment_packet": {
                    "beat_packets": [
                        {
                            "beat_id": "beat_focus",
                            "tool_objectives": ["figure_context"],
                            "summary": focus_summary,
                            "supporting_points": [],
                            "reader_facing_notes": ["先看主图，再回正文。"],
                            "tool_findings": [],
                            "public_links": [],
                            "requested_tools": [{"tool": "paper_read"}],
                        },
                        {
                            "beat_id": "beat_explain",
                            "tool_objectives": ["term_explain"],
                            "summary": explain_summary,
                            "supporting_points": [],
                            "reader_facing_notes": ["术语解释贴着正文出现。"],
                            "tool_findings": [],
                            "public_links": [],
                            "requested_tools": [{"tool": "knowledge_search"}],
                        },
                        {
                            "beat_id": "beat_context",
                            "tool_objectives": ["why_it_matters"],
                            "summary": context_summary,
                            "supporting_points": [],
                            "reader_facing_notes": ["先补背景，再回正文。"],
                            "tool_findings": [],
                            "public_links": [],
                            "requested_tools": [{"tool": "web_search"}],
                        },
                    ]
                },
            },
        },
        page=7,
        user_intent="围绕正文生成新的页面阅读体验",
        enrichment_bundle=payload["enrichment_bundle"],
        used_tools=["paper_read", "knowledge_search", "web_search"],
        tool_trace=[],
    )

    assert result["resource_modules"][0]["display_title"] == "拆解这张图"
    assert result["resource_modules"][0]["display_summary"] == focus_summary
    assert result["resource_modules"][1]["display_title"] == "补充背景与上下文"
    assert result["resource_modules"][1]["display_summary"] == context_summary
    assert result["interaction_modules"][0]["display_title"] == "读懂关键术语"
    assert result["interaction_modules"][0]["display_summary"] == explain_summary
    assert result["js_widgets"][0]["display_title"] == "拆解这张图"
    assert result["js_widgets"][0]["display_summary"] == focus_summary
    focus_storyboard = next(row for row in result["page_brief"]["storyboard"] if row["section_type"] == "focus_stage")
    assert "packet_summary" not in dict(focus_storyboard.get("meta") or {})
    assert "beat_packet_summary" not in dict(result["resource_modules"][0].get("meta") or {})
    assert "beat_supporting_points" not in dict(result["resource_modules"][0].get("meta") or {})
    assert "beat_packet_summary" not in dict(result["interaction_modules"][0].get("meta") or {})
    assert "beat_reader_notes" not in dict(result["interaction_modules"][0].get("meta") or {})
    assert "beat_packet_summary" not in dict(result["js_widgets"][0].get("meta") or {})


def test_build_guided_beats_from_sections_should_not_fallback_to_planner_reader_copy():
    runtime = GenerativeReaderAgentRuntime()

    guided = runtime._build_guided_beats_from_sections(  # pylint: disable=protected-access
        hero={
            "display_title": "阅读导言",
            "display_summary": "先抓住这一页最关键的证据。",
            "target_ids": ["p7:fig-1"],
        },
        main_sections=[
            {
                "section_id": "section_focus",
                "section_type": "focus_stage",
                "display_title": "拆解这张图",
                "display_summary": "先看图中最强信号，再回正文。",
                "target_ids": ["p7:fig-1"],
                "meta": {
                    "planner_beat_id": "beat_focus",
                    "planner_reader_goal": "把刚形成的理解转成后续追问和检查点。",
                    "planner_continuity_note": "这一段应该帮助读者停下来整理理解，而不是继续堆新信息。",
                },
            }
        ],
    )

    focus_beat = next(item for item in guided if item["section_type_hint"] == "focus_stage")
    assert focus_beat["reader_goal"] == "先抓住本页最强的图示或证据，再回到正文读论证。"
    assert focus_beat["continuity_note"] == "这一段是理解全页的抓手，不要求一次读完所有细节。"
    assert "后续追问和检查点" not in json.dumps(focus_beat, ensure_ascii=False)


def test_recover_plan_deterministically_should_drive_experience_copy_from_beat_packets():
    runtime = GenerativeReaderAgentRuntime()
    focus_summary = "先抓住图中的主要比较，再回到正文看作者如何解释差异。"
    context_summary = "补一层 USMLE 背景，帮助读懂图里为什么要分 Step 1、2CK 和 3。"

    recovered = runtime._recover_plan_deterministically(  # pylint: disable=protected-access
        page=7,
        user_intent="",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
        compose_payload=_sample_payload(),
        tool_trace=[
            {
                "type": "observation",
                "data": {
                    "tool": "paper_read",
                    "success": True,
                    "output": "Figure 3 shows insight prevalence and concordance.",
                    "stage": "tool_enricher",
                    "beat_id": "beat_focus",
                },
            }
        ],
        used_tools=["paper_read", "web_search"],
        planner_output={
            "guided_beats": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "拆解这张图",
                    "reader_goal": "先看主图，再回正文。",
                    "continuity_note": "先抓住最强证据，再看作者如何展开。",
                    "target_ids": ["p7:fig-1"],
                    "tool_objectives": ["figure_context"],
                    "priority": 2,
                },
                {
                    "beat_id": "beat_read",
                    "role": "read_support",
                    "section_type": "reading_flow",
                    "title": "完整阅读本页内容",
                    "reader_goal": "保留正文主干。",
                    "continuity_note": "顺着正文理解作者如何展开。",
                    "target_ids": ["p7:fig-1", "p7:p-1"],
                    "tool_objectives": ["continuation_bridge"],
                    "priority": 3,
                },
                {
                    "beat_id": "beat_context",
                    "role": "add_context",
                    "section_type": "supporting_resources",
                    "title": "补充背景与上下文",
                    "reader_goal": "补背景",
                    "continuity_note": "外部资料只服务于这页理解。",
                    "target_ids": ["p7:p-1"],
                    "tool_objectives": ["why_it_matters"],
                    "priority": 4,
                },
            ],
        },
        tool_enrichment_packet={
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": focus_summary,
                    "supporting_points": [],
                    "reader_facing_notes": ["先看主图，再回正文。"],
                    "tool_findings": [{"tool": "paper_read", "success": True, "output_excerpt": focus_summary}],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read"}],
                },
                {
                    "beat_id": "beat_context",
                    "tool_objectives": ["why_it_matters"],
                    "summary": context_summary,
                    "supporting_points": [],
                    "reader_facing_notes": ["先补背景，再回正文。"],
                    "tool_findings": [{"tool": "web_search", "success": True, "output_excerpt": context_summary}],
                    "public_links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                    "requested_tools": [{"tool": "web_search"}],
                },
            ]
        },
        planning_brief={
            "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
            "guided_beat_seed": _sample_done_plan()["page_brief"]["storyboard"],
        },
        adjacent_page_context=[{"page": 6, "relation": "previous_page"}],
        page_dossier={"focus_page": 7},
        runtime_stage_trace=[{"stage_id": "planner", "status": "exception_repaired"}],
    )

    assert recovered is not None
    assert recovered["resource_modules"][0]["display_title"] == "拆解这张图"
    assert recovered["resource_modules"][0]["display_summary"] == focus_summary

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=recovered,
    )

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    focus_beat = next(row for row in experience["guided_beats"] if row["beat_type"] == "figure_walkthrough")
    assert sections["focus_stage"]["display_summary"] == focus_summary
    assert sections["supporting_resources"]["display_summary"] == context_summary
    assert focus_beat["display_summary"] == focus_summary


@pytest.mark.asyncio
async def test_execute_planner_tool_requests_should_backfill_public_web_context_when_native_results_are_thin():
    runtime = GenerativeReaderAgentRuntime()

    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        async def execute(self, tool_name, **arguments):
            self.calls.append((tool_name, arguments))
            if tool_name == "web_search":
                return SimpleNamespace(
                    success=True,
                    output='{"results":[{"title":"USMLE","url":"https://www.usmle.org/","snippet":"Official overview."}]}',
                    error=None,
                    data={
                        "structured_content": {
                            "results": [
                                {
                                    "title": "USMLE",
                                    "url": "https://www.usmle.org/",
                                    "snippet": "Official overview.",
                                }
                            ]
                        }
                    },
                )
            if tool_name == "knowledge_search":
                return SimpleNamespace(success=False, output="", error="no_results", data={})
            return SimpleNamespace(
                success=True,
                output="Figure 3 shows insight prevalence and concordance.",
                error=None,
                data={},
            )

    registry = _FakeRegistry()
    used_tools, _tool_trace, enrichment = await runtime._execute_planner_tool_requests(  # pylint: disable=protected-access
        registry=registry,
        allowed_tools=["paper_read", "knowledge_search", "web_search"],
        planner_output={
            "tool_budget": {
                "max_tool_requests": 4,
                "max_reader_native_requests": 2,
                "max_public_web_requests": 1,
                "allow_web_scrape": False,
                "duplicate_query_policy": "exact_query_text",
                "per_tool_timeout_seconds": 8,
            },
            "tool_requests": [
                {"beat_id": "beat_focus", "tool": "paper_read", "arguments": {"query": "Fig 3", "top_k": 4}, "reason": "grounding", "priority": "high"},
                {"beat_id": "beat_explain", "tool": "knowledge_search", "arguments": {"query": "Fig 3 USMLE 结构", "top_k": 4}, "reason": "context", "priority": "medium"},
            ],
            "resource_objectives": ["USMLE 结构"],
            "widget_focus": "Fig 3",
            "guided_beats": [
                {"beat_id": "beat_focus", "tool_objectives": ["figure_context"], "title": "拆解这张图", "reader_goal": "先看主图"},
                {"beat_id": "beat_context", "tool_objectives": ["why_it_matters"], "title": "补充背景与上下文", "reader_goal": "补背景"},
            ],
        },
    )

    assert used_tools == ["paper_read", "knowledge_search", "web_search"]
    assert registry.calls[-1] == ("web_search", {"query": "USMLE 结构 Fig 3", "max_results": 5})
    assert enrichment["budget_summary"]["requested_tool_count"] == 3
    assert enrichment["budget_summary"]["planner_requested_tool_count"] == 2
    assert enrichment["budget_summary"]["backfill_request_count"] == 1
    assert enrichment["budget_summary"]["followup_request_count"] == 0
    assert enrichment["budget_summary"]["total_requested_tool_count"] == 3
    assert enrichment["budget_summary"]["executed_requested_tool_count"] == 3
    assert enrichment["budget_summary"]["executed_planner_tool_count"] == 2
    assert enrichment["budget_summary"]["executed_backfill_tool_count"] == 1
    assert enrichment["public_links"]
    assert enrichment["public_links"][0]["href"] == "https://www.usmle.org/"
    context_packet = next(row for row in enrichment["beat_packets"] if row["beat_id"] == "beat_context")
    assert context_packet["public_links"][0]["href"] == "https://www.usmle.org/"
    assert context_packet["summary"] == "先补上理解当前内容需要的背景。"
    assert context_packet["supporting_points"] == ["先把背景补齐：USMLE 帮助补上理解当前内容所需的官方背景。"]
    assert context_packet["tool_accounting"] == {
        "requested_count": 1,
        "planner_requested_count": 0,
        "backfill_requested_count": 1,
        "followup_requested_count": 0,
        "total_requested_count": 1,
        "executed_count": 1,
        "executed_requested_count": 1,
        "executed_planner_count": 0,
        "executed_backfill_count": 1,
        "executed_followup_count": 0,
    }
    assert context_packet["reader_facing_notes"] == [
        "外部背景只作为辅助说明，不替代正文主线。",
    ]
    _assert_no_reader_surface_noise(context_packet["summary"])
    _assert_no_reader_surface_noise(context_packet["reader_facing_notes"])


@pytest.mark.asyncio
async def test_execute_planner_tool_requests_should_follow_up_with_web_scrape_for_high_value_search_results():
    runtime = GenerativeReaderAgentRuntime()

    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        async def execute(self, tool_name, **arguments):
            self.calls.append((tool_name, arguments))
            if tool_name == "web_search":
                return SimpleNamespace(
                    success=True,
                    output='{"results":[{"title":"USMLE Overview","url":"https://www.usmle.org/","snippet":"Official overview."}]}',
                    error=None,
                    data={
                        "structured_content": {
                            "results": [
                                {
                                    "title": "USMLE Overview",
                                    "url": "https://www.usmle.org/",
                                    "snippet": "Official overview.",
                                }
                            ]
                        }
                    },
                )
            if tool_name == "web_scrape":
                return SimpleNamespace(
                    success=True,
                    output="The official USMLE overview explains the three-step structure and how candidates progress through it.",
                    error=None,
                    data={"public_links": [{"label": "USMLE Overview", "href": "https://www.usmle.org/"}]},
                )
            return SimpleNamespace(success=True, output="", error=None, data={})

    registry = _FakeRegistry()
    _used_tools, _tool_trace, enrichment = await runtime._execute_planner_tool_requests(  # pylint: disable=protected-access
        registry=registry,
        allowed_tools=["web_search", "web_scrape"],
        planner_output={
            "tool_budget": {
                "max_tool_requests": 3,
                "max_reader_native_requests": 0,
                "max_public_web_requests": 3,
                "allow_web_scrape": True,
                "duplicate_query_policy": "exact_query_text",
                "per_tool_timeout_seconds": 8,
            },
            "tool_requests": [
                {
                    "beat_id": "beat_context",
                    "tool": "web_search",
                    "arguments": {"query": "USMLE structure official overview", "max_results": 4},
                    "reason": "need official context",
                    "priority": "medium",
                }
            ],
            "resource_objectives": ["USMLE 结构"],
            "widget_focus": "Fig 3",
            "guided_beats": [
                {
                    "beat_id": "beat_context",
                    "tool_objectives": ["why_it_matters", "external_comparison"],
                    "title": "补充背景与上下文",
                    "reader_goal": "补背景",
                }
            ],
        },
    )

    assert registry.calls[0][0] == "web_search"
    assert registry.calls[1][0] == "web_scrape"
    assert enrichment["budget_summary"]["requested_tool_count"] == 1
    assert enrichment["budget_summary"]["planner_requested_tool_count"] == 1
    assert enrichment["budget_summary"]["backfill_request_count"] == 0
    assert enrichment["budget_summary"]["followup_request_count"] == 1
    assert enrichment["budget_summary"]["total_requested_tool_count"] == 2
    assert enrichment["budget_summary"]["executed_requested_tool_count"] == 1
    assert enrichment["budget_summary"]["executed_planner_tool_count"] == 1
    assert enrichment["budget_summary"]["executed_backfill_tool_count"] == 0
    assert enrichment["budget_summary"]["executed_followup_tool_count"] == 1
    context_packet = next(row for row in enrichment["beat_packets"] if row["beat_id"] == "beat_context")
    assert any(str(row.get("tool") or "") == "web_scrape" for row in context_packet["requested_tools"])
    assert any(str(row.get("request_origin") or "") == "followup" for row in context_packet["requested_tools"])
    assert any(str(row.get("tool") or "") == "web_scrape" and row.get("success") for row in context_packet["tool_findings"])
    assert any(str(row.get("source_kind") or "") == "public_web_search" for row in context_packet["tool_findings"])
    assert any(str(row.get("source_kind") or "") == "public_web_page" for row in context_packet["tool_findings"])
    assert any("usmle.org" in str(row.get("source_url") or "") for row in context_packet["tool_findings"])
    assert context_packet["tool_accounting"] == {
        "requested_count": 1,
        "planner_requested_count": 1,
        "backfill_requested_count": 0,
        "followup_requested_count": 1,
        "total_requested_count": 2,
        "executed_count": 2,
        "executed_requested_count": 1,
        "executed_backfill_count": 0,
        "executed_followup_count": 1,
        "executed_planner_count": 1,
    }
    assert context_packet["reader_facing_notes"] == [
        "外部背景只作为辅助说明，不替代正文主线。",
        "外部对照只保留少量高相关来源，帮助判断差异。",
    ]
    _assert_no_reader_surface_noise(context_packet["reader_facing_notes"])
    assert context_packet["public_links"][0]["href"] == "https://www.usmle.org/"


@pytest.mark.asyncio
async def test_execute_planner_tool_requests_should_reserve_public_web_capacity_for_scrape_followups():
    runtime = GenerativeReaderAgentRuntime()

    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        async def execute(self, tool_name, **arguments):
            self.calls.append((tool_name, arguments))
            if tool_name == "web_search":
                return SimpleNamespace(
                    success=True,
                    output='{"results":[{"title":"USMLE Overview","url":"https://www.usmle.org/","snippet":"Official overview."}]}',
                    error=None,
                    data={
                        "structured_content": {
                            "results": [
                                {
                                    "title": "USMLE Overview",
                                    "url": "https://www.usmle.org/",
                                    "snippet": "Official overview.",
                                }
                            ]
                        }
                    },
                )
            if tool_name == "web_scrape":
                return SimpleNamespace(
                    success=True,
                    output="The official USMLE overview explains the three-step structure and how candidates progress through it.",
                    error=None,
                    data={"public_links": [{"label": "USMLE Overview", "href": "https://www.usmle.org/"}]},
                )
            return SimpleNamespace(success=True, output="", error=None, data={})

    registry = _FakeRegistry()
    _used_tools, _tool_trace, enrichment = await runtime._execute_planner_tool_requests(  # pylint: disable=protected-access
        registry=registry,
        allowed_tools=["web_search", "web_scrape"],
        planner_output={
            "tool_budget": {
                "max_tool_requests": 5,
                "max_reader_native_requests": 0,
                "max_public_web_requests": 2,
                "allow_web_scrape": True,
                "duplicate_query_policy": "exact_query_text",
                "per_tool_timeout_seconds": 8,
            },
            "tool_requests": [
                {
                    "beat_id": "beat_context",
                    "tool": "web_search",
                    "arguments": {"query": "USMLE structure official overview", "max_results": 4},
                    "reason": "need official context",
                    "priority": "medium",
                },
                {
                    "beat_id": "beat_context",
                    "tool": "web_search",
                    "arguments": {"query": "USMLE step differences overview", "max_results": 4},
                    "reason": "need comparison context",
                    "priority": "medium",
                },
            ],
            "resource_objectives": ["USMLE 结构"],
            "widget_focus": "Fig 3",
            "guided_beats": [
                {
                    "beat_id": "beat_context",
                    "tool_objectives": ["why_it_matters", "external_comparison"],
                    "title": "补充背景与上下文",
                    "reader_goal": "补背景",
                }
            ],
        },
    )

    assert registry.calls[0][0] == "web_search"
    assert registry.calls[1][0] == "web_scrape"
    assert not any(call[0] == "web_search" and call[1].get("query") == "USMLE step differences overview" for call in registry.calls)
    context_packet = next(row for row in enrichment["beat_packets"] if row["beat_id"] == "beat_context")
    assert any(str(row.get("tool") or "") == "web_scrape" for row in context_packet["requested_tools"])
    assert context_packet["summary"]


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
    guided_beats = experience["guided_beats"]
    assert guided_beats
    assert guided_beats[0]["beat_type"] == "guide_intro"
    assert any(row["beat_type"] == "body_segment" for row in guided_beats)
    assert any(row["beat_type"] == "figure_walkthrough" for row in guided_beats)


def test_build_experience_plan_should_derive_teacher_narrative_spine_before_sections():
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
    spine = experience["meta"]["teacher_narrative_spine"]

    assert spine["page_anchor"] == "Fig 3"
    assert sections["reading_flow"]["title"] == "再看正文怎么解释这些差异"
    assert sections["reading_flow"]["display_summary"] == spine["body_guidance"]
    assert sections["supporting_resources"]["display_summary"] == spine["support_guidance"]


def test_build_experience_plan_should_differentiate_default_section_jobs_in_teacher_spine():
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
    spine = experience["meta"]["teacher_narrative_spine"]

    assert spine["opening"] == experience["hero"]["display_summary"]
    assert spine["opening"] != spine["focus_guidance"]
    assert spine["opening"].startswith("这一页重要")
    assert "Fig 3" in spine["opening"]
    assert "重点是看" not in spine["opening"]
    assert "最值得注意的是" in spine["focus_guidance"]
    assert "并排摆在一起" in spine["focus_guidance"]
    assert any(token in spine["body_guidance"] for token in ("解释链", "撑起来", "串成"))
    assert "这个判断词" in spine["term_guidance"]
    assert "这类词决定了" not in spine["term_guidance"]
    assert "不是另起一条线" in spine["support_guidance"]
    assert "各自对应什么对象或场景" in spine["support_guidance"]
    assert "Concordance (一致性)" not in spine["term_guidance"]
    assert sections["focus_stage"]["display_summary"] == spine["focus_guidance"]
    assert sections["reading_flow"]["display_summary"] == spine["body_guidance"]
    assert sections["explainer_cluster"]["display_summary"] == spine["term_guidance"]
    assert sections["supporting_resources"]["display_summary"] == spine["support_guidance"]


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


def test_build_experience_plan_should_demote_raw_focus_evidence_from_primary_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    raw_caption = (
        "Figure 3. Concordance and insight of ChatGPT on USMLE across target learner groups and exam types, "
        "with panels A-D showing prevalence and concordance patterns."
    )
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": raw_caption,
                    "supporting_points": [raw_caption],
                    "reader_facing_notes": ["先用图抓住重点，再回正文。"],
                    "tool_findings": [{"tool": "paper_read", "success": True, "output_excerpt": raw_caption}],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read"}],
                }
            ]
        },
    }
    plan["js_widgets"] = [
        {
            "widget_id": "widget_1",
            "widget_type": "figure-focus-accordion",
            "target_ids": ["p7:fig-1"],
            "title": "Figure exploration",
            "props": {"panels": [{"label": "Figure overview", "summary": "Concordance and insight of ChatGPT on USMLE."}]},
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]
    assert "Concordance and insight" not in sections["focus_stage"]["display_summary"]
    assert sections["focus_stage"]["display_summary"] == spine["focus_guidance"]
    assert sections["focus_stage"]["meta"]["content_lane"] == "current_page_evidence"
    assert experience["widget_blocks"][0]["display_summary"]
    panel = experience["widget_blocks"][0]["props"]["panels"][0]
    assert panel["summary"] == panel["display_summary"]
    assert "Concordance and insight" not in panel["summary"]
    assert panel["source_evidence_summary"] == "Concordance and insight of ChatGPT on USMLE."


def test_build_experience_plan_should_rewrite_scaffold_like_primary_copy_to_natural_summaries():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {
                "claim_id": "claim_1",
                "text": "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，表明其解释具有潜在的教学价值。",
                "source_node_ids": ["p7:p-1"],
            }
        ],
        "terms_to_explain": [
            {"term": "Concordance", "reason": "metric", "source_node_ids": ["p7:p-1"]},
            {"term": "Insight", "reason": "metric", "source_node_ids": ["p7:p-1"]},
        ],
    }
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "Fig 3 里最关键的比较把 USMLE 和 Concordance 这些判断点集中到一起，正文随后解释这些差异为什么足以支撑“Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，表明其解释具有潜在的教学价值。”。",
                    "reader_facing_notes": [],
                    "public_links": [],
                },
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": "顺着当前页正文往下读时，也别忘了它是在接前文的线索；重点看作者怎样把 Fig 3 里的差异解释成“Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，表明其解释具有潜在的教学价值。”。",
                    "reader_facing_notes": [],
                    "public_links": [],
                },
            ]
        },
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "summary": "上一页先铺开了解释质量这条线索。",
                "continuation_hints": ["当前页继续解释 Concordance 和 Insight 的关系。"],
                "figure_hints": [],
            }
        ],
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    reader_copy = [
        experience["hero"]["display_subtitle"],
        experience["hero"]["display_summary"],
        sections["focus_stage"]["display_summary"],
        sections["reading_flow"]["display_summary"],
    ]
    forbidden_markers = (
        "顺着当前页正文往下读",
        "顺着正文往下读",
        "重点看作者怎样",
        "足以支撑",
        "关键结论：",
        "也别忘了它是在接前文",
        "“Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察",
    )

    assert "Fig 3" in experience["hero"]["display_subtitle"]
    assert "Fig 3" in sections["focus_stage"]["display_summary"]
    assert "正文" in sections["reading_flow"]["display_summary"]
    assert len(experience["hero"]["display_summary"]) <= 120
    for value in reader_copy:
        for marker in forbidden_markers:
            assert marker not in value


def test_collect_reader_anchor_terms_should_drop_raw_claims_and_fragments():
    runtime = GenerativeReaderAgentRuntime()

    anchor_terms = runtime._collect_reader_anchor_terms(  # pylint: disable=protected-access
        "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，且洞察频率在不同考试类型间保持一致。",
        "Chat",
        "的回答中产生了至少一",
        "个显著洞察",
        "Concordance (一致性)",
        "USMLE",
        "augment",
    )

    assert "一致性" in anchor_terms
    assert "USMLE" in anchor_terms
    assert "考试类型" in anchor_terms or "洞见" in anchor_terms
    forbidden = (
        "Chat",
        "的回答中产生了至少一",
        "个显著洞察",
        "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，且洞察频率在不同考试类型间保持一致。",
        "augment",
    )
    for marker in forbidden:
        assert marker not in anchor_terms
    assert all("88.9%" not in term for term in anchor_terms)


def test_derive_adjacent_bridge_cues_should_drop_mixed_ocr_english_fragments():
    runtime = GenerativeReaderAgentRuntime()

    cues = runtime._derive_adjacent_bridge_cues(  # pylint: disable=protected-access
        [
            {
                "page": 8,
                "relation": "next_page",
                "source": "ocr",
                "summary": "next page implications for medical education and ... ai development",
                "continuation_hints": ["next page implications for medical education and ... ai development"],
                "figure_hints": [],
                "reference_only": True,
            }
        ]
    )

    assert cues
    assert cues[0]["text"].startswith("后文")
    assert "ai development" not in cues[0]["text"].lower()
    assert "medical education and" not in cues[0]["text"].lower()
    assert "..." not in cues[0]["text"]


def test_build_experience_plan_should_preserve_natural_focus_packet_summary():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    natural_summary = "Fig 3 把一致性和洞见放在一起比较，后面的解释都会围绕这两个维度展开。"
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": natural_summary,
                    "reader_facing_notes": [],
                    "public_links": [],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    assert sections["focus_stage"]["display_summary"] == natural_summary


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
    assert experience["meta"]["display_copy_contract"] == "display_copy_v3"
    assert experience["meta"]["guided_reading_contract"] == "guided_beats_v3"
    assert experience["meta"]["content_budget"]["max_claim_cards"] >= 1


def test_build_experience_plan_should_emit_teaching_manuscript_contract():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            **plan["resource_modules"][0],
            "links": [
                {
                    "href": "https://pubmed.ncbi.nlm.nih.gov/37600000/",
                    "label": "PubMed background",
                    "snippet": "补一层和 USMLE 题型相关的权威背景。",
                }
            ],
        }
    ]
    plan["meta"] = {
        **plan.get("meta", {}),
        "page_dossier": _sample_page_dossier(),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "summary": "",
                "continuation_hints": [
                    "The analysis continues on this page with evaluation of explanation quality.",
                    "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                ],
                "figure_hints": ["Figure 2: Accuracy of Chat GPT on USMLE ..."],
            }
        ],
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先看 Fig 3，抓住它在这一页承载的关键比较。",
                    "reader_facing_notes": ["把图里的比较对象看清楚，再回正文。"],
                    "public_links": [],
                },
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": "顺着正文往下读，重点看作者怎样解释 88.9% 这个结果。",
                    "reader_facing_notes": ["别把图和正文拆开看。"],
                    "public_links": [
                        {
                            "href": "https://pubmed.ncbi.nlm.nih.gov/37600000/",
                            "label": "PubMed background",
                            "snippet": "补一层和 USMLE 题型相关的权威背景。",
                        }
                    ],
                },
            ]
        },
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

    manuscript = experience["teaching_manuscript"]
    assert manuscript["version"] == "v2"
    assert manuscript["status"] == "done"
    assert experience["meta"]["experience_primary_contract"] == "reader_content_v1"
    assert experience["meta"]["experience_secondary_contract"] == "teaching_manuscript_v2"
    assert experience["meta"]["experience_output"]["primary_artifact_path"] == "main_sections"
    assert experience["meta"]["experience_output"]["primary_content_paths"] == [
        "hero",
        "main_sections",
        "guided_beats",
        "supporting_resources",
        "interactive_blocks",
        "widget_blocks",
    ]
    assert experience["meta"]["experience_contract_boundary"]["primary_reader_fields"] == [
        "focus_page",
        "status",
        "layout_variant",
        "hero",
        "main_sections",
        "guided_beats",
        "supporting_resources",
        "interactive_blocks",
        "widget_blocks",
    ]
    assert experience["meta"]["experience_contract_boundary"]["secondary_fallback_fields"] == [
        "teaching_manuscript.segments",
    ]
    assert experience["meta"]["content_artifact"]["contract"] == "reader_content_v1"
    assert experience["meta"]["content_artifact"]["reader_constraints"]["ui_flexible_content_units"] is True
    assert "page_dossier" in experience["meta"]["experience_contract_boundary"]["inspect_only_fields"]
    assert experience["meta"]["story_substrate"]["page_id"] == "p7"
    assert experience["meta"]["page_brief"]["page_archetype"] == "figure_explainer"
    assert len(manuscript["segments"]) >= 3
    focus_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "figure")
    body_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "body")
    manuscript_artifact = experience["meta"]["manuscript_artifact"]
    assert manuscript_artifact["contract"] == "teaching_manuscript_v2"
    assert manuscript_artifact["reader_constraints"]["current_page_primary"] is True
    assert manuscript_artifact["reader_constraints"]["adjacent_pages_bridge_only"] is True
    assert manuscript_artifact["page_dossier"]["current_page"]["target_ids"] == ["p7:fig-1", "p7:p-1"]
    assert manuscript_artifact["page_dossier"]["current_page"]["asset_kinds"] == ["page_render_image"]
    assert manuscript_artifact["page_dossier"]["adjacent_pages"][0]["page"] == 6
    assert manuscript_artifact["page_dossier"]["authoritative_resource_candidates"][0]["href"] == "https://pubmed.ncbi.nlm.nih.gov/37600000/"
    assert [row["stage_id"] for row in manuscript_artifact["stages"]] == [
        "dossier_assembly",
        "draft_manuscript",
        "critic_findings",
        "final_manuscript",
    ]
    runtime_stage_ids = [row["stage_id"] for row in experience["meta"]["runtime_stage_trace"]]
    assert "manuscript_dossier_assembly" in runtime_stage_ids
    assert "manuscript_final" in runtime_stage_ids
    opening_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "opening")
    assert opening_segment["teaching_text"].startswith("先把阅读顺序定下来：先看 Fig 3")
    assert "Fig 3 把" in opening_segment["teaching_text"]
    assert "一致性和洞见" in opening_segment["teaching_text"]
    assert "Results" in opening_segment["teaching_text"]
    assert "核心问题" not in opening_segment["title"]
    assert opening_segment["display_flow"] == [
        {"kind": "prose", "text": opening_segment["teaching_text"]},
        {
            "kind": "figure_slot",
            "slot_id": "fig:ms-opening",
            "label": "Fig 3",
            "target_ids": ["p7:fig-1"],
        },
    ]
    assert opening_segment["slot_bindings"] == [
        {
            "slot_id": "fig:ms-opening",
            "kind": "figure_slot",
            "label": "Fig 3",
            "target_ids": ["p7:fig-1"],
            "full_evidence_target_ids": ["p7:fig-1"],
        }
    ]
    assert focus_segment["teaching_text"].startswith("这一步先只看图里的比较：")
    assert "一致性和洞见" in focus_segment["teaching_text"]
    assert "USMLE" in focus_segment["teaching_text"]
    assert "真正说明什么" not in focus_segment["title"]
    assert focus_segment["display_flow"] == [
        {"kind": "prose", "text": focus_segment["teaching_text"]},
        {
            "kind": "figure_slot",
            "slot_id": "fig:ms-focus",
            "label": "Fig 3",
            "target_ids": ["p7:fig-1"],
        },
    ]
    assert focus_segment["slot_bindings"] == [
        {
            "slot_id": "fig:ms-focus",
            "kind": "figure_slot",
            "label": "Fig 3",
            "target_ids": ["p7:fig-1"],
            "full_evidence_target_ids": ["p7:fig-1"],
        }
    ]
    assert focus_segment["adjacent_bridge"].startswith("读到这里时，先接上前文关于")
    assert focus_segment["reference_links"][0]["href"] == "https://pubmed.ncbi.nlm.nih.gov/37600000/"
    assert body_segment["target_ids"] == ["p7:p-1"]
    assert body_segment["full_evidence_target_ids"] == ["p7:fig-1", "p7:p-1"]
    assert body_segment["teaching_text"].startswith("接下来读 Results，重点看作者怎样解释前面的比较")
    assert "洞见" in body_segment["teaching_text"]
    assert "Fig 3" in body_segment["teaching_text"]
    assert "正文怎样把结果讲具体" not in body_segment["title"]
    assert body_segment["display_flow"] == [
        {"kind": "prose", "text": body_segment["teaching_text"]},
        {
            "kind": "body_slot",
            "slot_id": "body:ms-body",
            "label": "Results",
            "target_ids": ["p7:p-1"],
        },
    ]
    assert body_segment["slot_bindings"] == [
        {
            "slot_id": "body:ms-body",
            "kind": "body_slot",
            "label": "Results",
            "target_ids": ["p7:p-1"],
            "full_evidence_target_ids": ["p7:p-1"],
        },
    ]
    assert body_segment["adjacent_bridge"].startswith("读到这里时，先接上前文关于")
    assert body_segment["glossary"][0]["term"] == "Concordance"
    assert body_segment["reference_links"][0]["href"] == "https://pubmed.ncbi.nlm.nih.gov/37600000/"
    assert body_segment["reference_links"][0]["note"]
    assert "USMLE" in body_segment["reference_links"][0]["note"] or "题型" in body_segment["reference_links"][0]["note"]
    assert experience["meta"]["tool_enrichment_packet"]["adjacent_page_context"][0]["page"] == 6
    assert experience["meta"]["tool_enrichment_packet"]["adjacent_bridge_cues"][0]["text"]
    assert experience["meta"]["tool_enrichment_packet"]["adjacent_page_continuity"][0]["summary"]
    _assert_no_reader_surface_noise(
        [row["teaching_text"] for row in manuscript["segments"]]
        + [focus_segment["adjacent_bridge"], body_segment["adjacent_bridge"]]
    )


def test_validate_experience_plan_contract_should_strip_teaching_manuscript_noise():
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
    experience["teaching_manuscript"] = {
        "version": "v1",
        "status": "done",
        "segments": [
            {
                "segment_id": "ms-body",
                "segment_type": "body",
                "title": "这一拍",
                "teaching_text": "把刚形成的理解转成后续追问和检查点。",
                "anchor_excerpt": "We first examined the frequency of insight.",
                "target_ids": ["p7:p-1"],
                "glossary": [{"term": "Concordance", "note": "这一拍里要记住它。"}],
                "adjacent_bridge": "Search failed 429 blocked",
                "reference_links": [
                    {
                        "href": "https://www.zhihu.com/question/1",
                        "label": "弱链接",
                        "note": "当前位置：首页",
                    }
                ],
                "meta": {},
            }
        ],
    }
    experience["meta"] = {
        **experience["meta"],
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页先解释了当前图示的比较基线。",
                "continuation_hints": ["当前页延续上一页的图示说明。"],
            }
        ],
    }

    result = runtime._validate_experience_plan_contract(experience)

    manuscript = result["teaching_manuscript"]
    assert manuscript["version"] == "v2"
    segment = manuscript["segments"][0]
    assert segment["title"]
    assert segment["title"] != "这一拍"
    assert "Fig 3" in segment["teaching_text"]
    assert "判断" in segment["teaching_text"] or "洞见" in segment["teaching_text"]
    assert "Fig 3" in segment["teaching_text"]
    assert segment["anchor_excerpt"] == "We first examined the frequency of insight."
    assert segment["display_flow"][0]["kind"] == "prose"
    assert segment["display_flow"][1]["kind"] == "body_slot"
    assert segment["slot_bindings"][0]["slot_id"] == "body:ms-body"
    assert segment["adjacent_bridge"] == ""
    assert segment["reference_links"] == []
    assert result["meta"]["experience_primary_contract"] == "reader_content_v1"
    assert result["meta"]["manuscript_artifact"]["critic_findings"]["findings"]
    _assert_no_reader_surface_noise(
        [segment["title"], segment["teaching_text"], segment["adjacent_bridge"]]
        + [item["note"] for item in segment["glossary"]]
    )


def test_validate_experience_plan_contract_should_upgrade_missing_current_page_grounding_from_dossier():
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
    experience["teaching_manuscript"] = {
        "version": "v1",
        "status": "done",
        "segments": [
            {
                "segment_id": "ms-body",
                "segment_type": "body",
                "title": "顺着正文把作者的解释读完",
                "teaching_text": "先把这一页的解释主线读顺。",
                "anchor_excerpt": "",
                "target_ids": [],
                "full_evidence_target_ids": [],
                "glossary": [],
                "adjacent_bridge": "",
                "reference_links": [],
                "meta": {},
            }
        ],
    }
    experience["meta"] = {
        **experience["meta"],
        "page_dossier": _sample_page_dossier(),
    }

    result = runtime._validate_experience_plan_contract(experience)

    unresolved_gaps = result["meta"]["manuscript_artifact"]["critic_findings"]["unresolved_gaps"]
    assert not any(item["gap_id"] == "missing_current_page_grounding" for item in unresolved_gaps)
    body_segment = next(row for row in result["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert body_segment["teaching_text"].startswith("接下来读 Results，重点看作者怎样解释前面的比较")
    assert body_segment["anchor_excerpt"] == "We first examined the frequency of insight."


def test_validate_experience_plan_contract_should_rewrite_live_failure_shape_manuscript_copy():
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
    experience["teaching_manuscript"] = {
        "version": "v2",
        "status": "done",
        "segments": [
            {
                "segment_id": "ms-focus",
                "segment_type": "figure",
                "title": "拆解这张图",
                "teaching_text": "先看 Fig 3 里比较了什么、变化落在哪，再回正文核对 “The analysis of explanation quality continues on this page.” 这句提示。",
                "anchor_excerpt": "Fig 3：这一页最关键的比较图。",
                "target_ids": ["p7:fig-1"],
                "full_evidence_target_ids": ["p7:fig-1"],
                "glossary": [],
                "adjacent_bridge": "读到这里时，把前文先铺开了这条线索：The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.当成承上启下的线索，再继续看作者怎样往下展开。",
                "reference_links": [],
                "meta": {},
            },
            {
                "segment_id": "ms-body",
                "segment_type": "body",
                "title": "顺着正文把作者的解释读完",
                "teaching_text": "正文这部分负责把 Fig 3 里的结果讲完整。先把前文关于 会继续沿着 的线索接上，再看作者怎样把当前页的判断落稳。",
                "anchor_excerpt": "",
                "target_ids": ["p7:p-1"],
                "full_evidence_target_ids": ["p7:fig-1", "p7:p-1"],
                "glossary": [],
                "adjacent_bridge": "读到这里时，把前文先铺开了这条线索：The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.当成承上启下的线索，再继续看作者怎样往下展开。",
                "reference_links": [],
                "meta": {},
            },
        ],
    }
    experience["meta"] = {
        **experience["meta"],
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "summary": "",
                "continuation_hints": [
                    "The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.",
                    "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                ],
                "figure_hints": [],
            }
        ],
        "page_dossier": _sample_page_dossier(),
    }

    result = runtime._validate_experience_plan_contract(experience)

    segments = {row["segment_type"]: row for row in result["teaching_manuscript"]["segments"]}
    focus_segment = segments["figure"]
    body_segment = segments["body"]

    assert focus_segment["teaching_text"].startswith("这一步先只看图里的比较：")
    assert "一致性和洞见" in focus_segment["teaching_text"]
    assert body_segment["teaching_text"].startswith("接下来读 Results，重点看作者怎样解释前面的比较")
    assert "洞见" in body_segment["teaching_text"]
    assert "关于 会继续沿着" not in body_segment["teaching_text"]
    assert "承上启下" not in focus_segment["adjacent_bridge"]
    assert "explanation quality continues" not in focus_segment["adjacent_bridge"]
    assert "non-obviousness" not in body_segment["adjacent_bridge"]
    assert not re.search(r"[A-Za-z]{6,}", focus_segment["adjacent_bridge"])
    assert not re.search(r"[A-Za-z]{6,}", body_segment["adjacent_bridge"])
    assert "解释质量" in focus_segment["adjacent_bridge"] or "非显而易见的洞见" in focus_segment["adjacent_bridge"]
    assert "解释质量" in body_segment["adjacent_bridge"] or "非显而易见的洞见" in body_segment["adjacent_bridge"]


def test_teaching_manuscript_needs_upgrade_should_flag_live_failure_shapes():
    runtime = GenerativeReaderAgentRuntime()
    manuscript = {
        "version": "v2",
        "status": "done",
        "segments": [
            {
                "segment_id": "ms-opening",
                "segment_type": "opening",
                "title": "这一页的核心问题",
                "teaching_text": "这页真正重要的不是读完几个段落，而是先弄清作者到底在解释哪一个核心结果。",
                "anchor_excerpt": "",
                "target_ids": ["p7:fig-1"],
                "full_evidence_target_ids": ["p7:fig-1", "p7:p-1"],
                "adjacent_bridge": "",
                "reference_links": [],
                "glossary": [],
            },
            {
                "segment_id": "ms-focus",
                "segment_type": "figure",
                "title": "这张图真正说明什么",
                "teaching_text": "先看 Fig 3 里比较了什么、变化落在哪，再回正文核对 “The analysis of explanation quality continues on this page.” 这句提示。",
                "anchor_excerpt": "Fig 3：这一页最关键的比较图。",
                "target_ids": ["p7:fig-1"],
                "full_evidence_target_ids": ["p7:fig-1"],
                "adjacent_bridge": "读到这里时，把前文先铺开了这条线索：The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.当成承上启下的线索，再继续看作者怎样往下展开。",
                "reference_links": [],
                "glossary": [],
            },
            {
                "segment_id": "ms-body",
                "segment_type": "body",
                "title": "正文怎样把结果讲具体",
                "teaching_text": "正文这部分负责把 Fig 3 里的结果讲完整。先把前文关于 会继续沿着 的线索接上，再看作者怎样把当前页的判断落稳。",
                "anchor_excerpt": "",
                "target_ids": ["p7:p-1"],
                "full_evidence_target_ids": ["p7:fig-1", "p7:p-1"],
                "adjacent_bridge": "读到这里时，把前文先铺开了这条线索：The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.当成承上启下的线索，再继续看作者怎样往下展开。",
                "reference_links": [],
                "glossary": [],
            },
        ],
    }
    adjacent_page_context = [
        {
            "page": 6,
            "relation": "previous_page",
            "reference_only": True,
            "summary": "",
            "continuation_hints": [
                "The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.",
                "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
            ],
            "figure_hints": [],
        }
    ]

    assert runtime._teaching_manuscript_needs_upgrade(  # pylint: disable=protected-access
        manuscript=manuscript,
        adjacent_page_context=adjacent_page_context,
    )


def test_build_experience_plan_should_anchor_body_manuscript_to_text_targets():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "secondary_support_target_ids": [],
        "body_flow_target_ids": ["p7:fig-1"],
    }
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "narrative_turns": [
            {"turn_id": "t1", "kind": "key_finding", "label": "Result", "target_ids": ["p7:p-1"]},
        ],
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

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert body_segment["target_ids"] == ["p7:p-1"]
    assert body_segment["full_evidence_target_ids"] == ["p7:fig-1", "p7:p-1"]
    assert body_segment["anchor_excerpt"] == "We first examined the frequency of insight."
    assert "Fig 3" in body_segment["teaching_text"]
    assert "洞见" in body_segment["teaching_text"]


def test_build_experience_plan_should_backfill_manuscript_reference_notes_when_links_lack_snippets():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": "顺着正文往下读，重点看作者怎样解释这一页的结果。",
                    "reader_facing_notes": [],
                    "public_links": [
                        {
                            "href": "https://pubmed.ncbi.nlm.nih.gov/37600000/",
                            "label": "PubMed background",
                        }
                    ],
                }
            ]
        },
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

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert body_segment["reference_links"]
    assert body_segment["reference_links"][0]["note"]


def test_build_experience_plan_should_enrich_seed_like_manuscript_without_tool_packets():
    runtime = GenerativeReaderAgentRuntime()
    seed_plan = runtime.build_seed_plan(
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
    )
    seed_plan["resource_modules"] = [
        {
            **dict(seed_plan["resource_modules"][0]),
            "target_ids": ["p7:p-1"],
            "links": [
                {
                    "href": "https://pubmed.ncbi.nlm.nih.gov/37600000/",
                    "label": "PubMed background",
                    "snippet": "补一层和 USMLE 题型相关的权威背景。",
                }
            ],
        }
    ]
    seed_meta = dict(seed_plan.get("meta") or {})
    seed_meta["adjacent_page_context"] = [
        {
            "page": 6,
            "relation": "previous_page",
            "summary": "上一页先解释了当前图示的比较基线。",
            "continuation_hints": ["当前页延续上一页的图示说明。"],
        }
    ]
    seed_plan["meta"] = seed_meta

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=seed_plan,
    )

    manuscript = experience["teaching_manuscript"]
    opening_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "opening")
    body_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "body")

    assert opening_segment["teaching_text"].startswith("先把阅读顺序定下来：先看 Fig 3")
    assert "Fig 3 把" in opening_segment["teaching_text"]
    assert body_segment["teaching_text"].startswith("接下来读 Results，重点看作者怎样解释前面的比较")
    assert "Fig 3" in body_segment["teaching_text"] and "洞见" in body_segment["teaching_text"]
    assert body_segment["adjacent_bridge"] == ""
    assert body_segment["glossary"]
    assert body_segment["reference_links"]
    assert "USMLE" in body_segment["reference_links"][0]["note"] or "题型" in body_segment["reference_links"][0]["note"]
    _assert_no_reader_surface_noise(
        [opening_segment["teaching_text"], body_segment["teaching_text"], body_segment["adjacent_bridge"]]
    )


def test_build_experience_plan_should_derive_adjacent_bridge_from_english_adjacent_context():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "summary": "",
                "continuation_hints": [
                    "The analysis continues on this page with evaluation of explanation quality.",
                    "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                ],
                "figure_hints": ["Figure 2: Accuracy of Chat GPT on USMLE ..."],
            }
        ],
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

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert body_segment["adjacent_bridge"].startswith("读到这里时，先")
    continuity_rows = experience["meta"]["tool_enrichment_packet"]["adjacent_page_continuity"]
    assert continuity_rows
    assert continuity_rows[0]["summary"]
    assert "nonobvious insights" not in body_segment["adjacent_bridge"]
    assert "evaluation of explanation quality" not in body_segment["adjacent_bridge"]
    assert "解释质量" in body_segment["adjacent_bridge"] or "非显而易见的洞见" in body_segment["adjacent_bridge"] or "Figure 2" in body_segment["adjacent_bridge"]


def test_build_experience_plan_should_remove_raw_english_from_adjacent_bridge_when_only_english_hints_exist():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "summary": "",
                "continuation_hints": [
                    "The analysis of explanation quality continues on this page, focusing on novelty, non-obviousness, and validity criteria.",
                    "Next section focuses on 'nonobvious insights' in AI-generated explanations.",
                ],
                "figure_hints": [],
            }
        ],
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

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert "nonobvious insights" not in body_segment["adjacent_bridge"]
    assert "explanation quality" not in body_segment["adjacent_bridge"]
    assert not re.search(r"[A-Za-z]{6,}", body_segment["adjacent_bridge"])
    assert "解释质量" in body_segment["adjacent_bridge"] or "非显而易见的洞见" in body_segment["adjacent_bridge"]


def test_build_experience_plan_should_dedupe_repetitive_manuscript_clauses():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    repeated_clause = "先看 Fig 3，抓住 Concordance 和 Insight 这两个词怎样把这一页的比较结果串起来。"
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": repeated_clause,
                    "reader_facing_notes": [repeated_clause],
                    "supporting_points": [repeated_clause],
                    "public_links": [],
                }
            ]
        },
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

    focus_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "figure")
    assert focus_segment["teaching_text"].count("Concordance") <= 1
    assert focus_segment["teaching_text"].count("Fig 3") <= 1


def test_build_experience_plan_should_use_short_anchor_fallback_for_english_heavy_focus_excerpt():
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

    focus_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "figure")
    assert focus_segment["anchor_excerpt"] == "Concordance and insight of Chat GPT on USMLE."
    assert "Fig 3：这一页最关键的比较图。" not in focus_segment["anchor_excerpt"]


def test_build_experience_plan_should_skip_fragmentary_body_targets_when_paragraph_exists():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"] = [
        payload["enrichment_bundle"]["targets"][0],
        {
            "target_id": "p7:p-frag",
            "node_id": "p-frag",
            "target_kind": "paragraph",
            "component_type": "ParagraphProse",
            "title": "",
            "excerpt": "adjudicator, as a second-year medical student for Step 1.",
            "section_label": "Results",
            "suggested_resource_types": ["glossary_panel"],
        },
        {
            "target_id": "p7:p-main",
            "node_id": "p-main",
            "target_kind": "paragraph",
            "component_type": "ParagraphProse",
            "title": "",
            "excerpt": "We first examined the frequency of insight. Overall, ChatGPT produced at least one significant insight in 88.9% of all responses.",
            "section_label": "Results",
            "suggested_resource_types": ["glossary_panel"],
        },
    ]
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "secondary_support_target_ids": ["p7:p-frag"],
        "body_flow_target_ids": ["p7:p-frag", "p7:p-main"],
    }
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "narrative_turns": [
            {"turn_id": "t1", "kind": "key_finding", "label": "Result", "target_ids": ["p7:p-frag", "p7:p-main"]},
        ],
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=plan,
    )

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert body_segment["target_ids"] == ["p7:p-main"]
    assert "88.9%" in body_segment["teaching_text"]
    assert body_segment["anchor_excerpt"]
    assert "88.9%" in body_segment["anchor_excerpt"] or "significant insight" in body_segment["anchor_excerpt"]


def test_build_experience_plan_should_surface_numeric_body_evidence_in_manuscript_copy():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"] = [
        payload["enrichment_bundle"]["targets"][0],
        {
            "target_id": "p7:p-claim",
            "node_id": "p-claim",
            "target_kind": "paragraph",
            "component_type": "ParagraphProse",
            "title": "",
            "excerpt": (
                "We first examined the frequency of insight. Overall, ChatGPT produced at least one significant "
                "insight in 88.9% of all responses. In Step 2CK however, insight decreased by 10.3%."
            ),
            "section_label": "Results",
            "suggested_resource_types": ["glossary_panel"],
        },
    ]
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "secondary_support_target_ids": ["p7:p-claim"],
        "body_flow_target_ids": ["p7:fig-1", "p7:p-claim"],
    }
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "narrative_turns": [
            {"turn_id": "t1", "kind": "key_finding", "label": "Result", "target_ids": ["p7:p-claim"]},
        ],
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=plan,
    )

    body_segment = next(row for row in experience["teaching_manuscript"]["segments"] if row["segment_type"] == "body")
    assert "88.9%" in body_segment["title"] or "洞见" in body_segment["title"]
    assert "88.9%" in body_segment["teaching_text"]
    assert "10.3%" in body_segment["teaching_text"]
    assert body_segment["anchor_excerpt"]
    assert "88.9%" in body_segment["anchor_excerpt"] or "10.3%" in body_segment["anchor_excerpt"]


def test_build_experience_plan_should_prefer_concrete_compose_blocks_over_guide_targets():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"] = [
        {
            "target_id": "p7:g1",
            "node_id": "g1",
            "target_kind": "figure",
            "component_type": "GuideSummaryCard",
            "title": "Fig 3 overview",
            "excerpt": "This guide card summarizes what the figure is about.",
            "figure_label": "Fig 3",
        },
        {
            "target_id": "p7:g2",
            "node_id": "g2",
            "target_kind": "paragraph",
            "component_type": "GuideSummaryCard",
            "title": "Results summary",
            "excerpt": "This guide card summarizes the body text.",
            "section_label": "Results",
        },
        {
            "target_id": "p7:fig-src",
            "node_id": "no_drop_fb_p7_dm_p7_l007_b001",
            "target_kind": "figure",
            "component_type": "FigurePanel",
            "title": "Fig 3",
            "figure_label": "Fig 3",
            "excerpt": "Fig 3. Concordance and insight of ChatGPT on USMLE.",
        },
        {
            "target_id": "p7:body-src",
            "node_id": "no_drop_fb_p7_dm_p7_l010_b001",
            "target_kind": "paragraph",
            "component_type": "ParagraphProse",
            "title": "",
            "section_label": "Results",
            "excerpt": (
                "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses. "
                "In Step 2CK however, insight decreased by 10.3%."
            ),
        },
    ]
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {"claim_id": "claim_1", "text": "Guide claim", "source_target_ids": ["p7:g2"]},
        ],
        "evidence_units": [
            {"evidence_id": "e1", "kind": "figure", "role": "primary_visual_evidence", "source_target_ids": ["p7:g1"]},
        ],
        "terms_to_explain": [
            {"term": "Concordance", "reason": "metric", "source_target_ids": ["p7:g2"]},
        ],
        "narrative_turns": [
            {"turn_id": "t1", "kind": "figure_focus", "label": "Guide figure", "target_ids": ["p7:g1"]},
            {"turn_id": "t2", "kind": "key_finding", "label": "Guide body", "target_ids": ["p7:g2"]},
        ],
    }
    plan["page_brief"] = {
        **plan["page_brief"],
        "primary_focus_target_id": "p7:g1",
        "secondary_support_target_ids": ["p7:g2"],
        "body_flow_target_ids": ["p7:g1", "p7:g2"],
        "storyboard": [
            {**dict(plan["page_brief"]["storyboard"][0]), "target_ids": ["p7:g1"]},
            {**dict(plan["page_brief"]["storyboard"][1]), "target_ids": ["p7:g1"]},
            {**dict(plan["page_brief"]["storyboard"][2]), "target_ids": ["p7:g1", "p7:g2"]},
            {**dict(plan["page_brief"]["storyboard"][3]), "target_ids": ["p7:g2"]},
            {**dict(plan["page_brief"]["storyboard"][4]), "target_ids": ["p7:g2"]},
            {**dict(plan["page_brief"]["storyboard"][5]), "target_ids": ["p7:g2"]},
        ],
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=plan,
    )

    manuscript = experience["teaching_manuscript"]
    opening_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "opening")
    focus_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "figure")
    body_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "body")

    assert focus_segment["target_ids"] == ["p7:fig-src"]
    assert focus_segment["full_evidence_target_ids"] == ["p7:fig-src"]
    assert focus_segment["display_flow"][1]["target_ids"] == ["p7:fig-src"]
    assert focus_segment["anchor_excerpt"].startswith("Fig 3. Concordance and insight of Chat")
    assert "USMLE" in focus_segment["anchor_excerpt"]
    assert body_segment["target_ids"] == ["p7:body-src"]
    assert body_segment["display_flow"][1]["target_ids"] == ["p7:body-src"]
    assert body_segment["slot_bindings"][0]["target_ids"] == ["p7:body-src"]
    assert len(body_segment["slot_bindings"]) == 1
    assert all(binding["kind"] != "figure_slot" for binding in body_segment["slot_bindings"])
    assert "88.9%" in body_segment["teaching_text"]
    assert "10.3%" in body_segment["anchor_excerpt"] or "88.9%" in body_segment["anchor_excerpt"]
    assert opening_segment["target_ids"] == ["p7:fig-src", "p7:body-src"]
    assert "USMLE" in experience["hero"]["summary"]
    assert all("p7:g" not in row["title"] for row in manuscript["segments"])
    assert not any(
        target_id in {"p7:g1", "p7:g2"}
        for row in manuscript["segments"]
        for target_id in list(row.get("target_ids") or [])
    )


def test_build_experience_plan_should_extract_concrete_targets_from_compose_payload_when_bundle_is_abstract_only():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()
    payload["enrichment_bundle"]["targets"] = [
        {
            "target_id": "p7:g1",
            "node_id": "g1",
            "target_kind": "figure",
            "component_type": "GuideSummaryCard",
            "title": "Figure guide",
            "excerpt": "A guide summary for the figure.",
            "figure_label": "Fig 3",
        },
        {
            "target_id": "p7:g2",
            "node_id": "g2",
            "target_kind": "paragraph",
            "component_type": "GuideSummaryCard",
            "title": "Results guide",
            "excerpt": "A guide summary for the results paragraph.",
            "section_label": "Results",
        },
    ]
    payload["ui_plan"] = {
        "components": [
            {
                "id": "no_drop_fb_p7_dm_p7_l007_b001",
                "type": "FigurePanel",
                "props": {
                    "source_label": "Fig 3",
                    "title": "Fig 3",
                    "caption": "Fig 3. Concordance and insight of ChatGPT on USMLE.",
                },
            },
            {
                "id": "no_drop_fb_p7_dm_p7_l010_b001",
                "type": "ParagraphProse",
                "props": {
                    "section_label": "Results",
                    "paragraphs": [
                        {
                            "text": (
                                "Overall, ChatGPT produced at least one significant insight in 88.9% of all responses. "
                                "In Step 2CK however, insight decreased by 10.3%."
                            )
                        }
                    ],
                },
            },
        ]
    }
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {"claim_id": "claim_1", "text": "Guide claim", "source_target_ids": ["p7:g2"]},
        ],
        "evidence_units": [
            {"evidence_id": "e1", "kind": "figure", "role": "primary_visual_evidence", "source_target_ids": ["p7:g1"]},
        ],
        "narrative_turns": [
            {"turn_id": "t1", "kind": "figure_focus", "label": "Guide figure", "target_ids": ["p7:g1"]},
            {"turn_id": "t2", "kind": "key_finding", "label": "Guide body", "target_ids": ["p7:g2"]},
        ],
    }
    plan["page_brief"] = {
        **plan["page_brief"],
        "primary_focus_target_id": "p7:g1",
        "secondary_support_target_ids": ["p7:g2"],
        "body_flow_target_ids": ["p7:g1", "p7:g2"],
    }

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=payload,
        generative_plan=plan,
    )

    manuscript = experience["teaching_manuscript"]
    focus_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "figure")
    body_segment = next(row for row in manuscript["segments"] if row["segment_type"] == "body")

    assert focus_segment["target_ids"] == ["p7:no_drop_fb_p7_dm_p7_l007_b001"]
    assert body_segment["target_ids"] == ["p7:no_drop_fb_p7_dm_p7_l010_b001"]
    assert focus_segment["slot_bindings"][0]["target_ids"] == ["p7:no_drop_fb_p7_dm_p7_l007_b001"]
    assert body_segment["slot_bindings"][0]["target_ids"] == ["p7:no_drop_fb_p7_dm_p7_l010_b001"]
    assert focus_segment["anchor_excerpt"].startswith("Fig 3. Concordance and insight")
    assert "88.9%" in body_segment["teaching_text"]
    assert "10.3%" in body_segment["anchor_excerpt"] or "88.9%" in body_segment["anchor_excerpt"]
    assert "USMLE" in experience["hero"]["summary"]


def test_validate_experience_plan_contract_should_upgrade_prose_only_abstract_manuscript_into_slot_flow():
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
    experience["teaching_manuscript"] = {
        "version": "v2",
        "status": "done",
        "segments": [
            {
                "segment_id": "ms-focus",
                "segment_type": "figure",
                "title": "这张图真正说明什么",
                "teaching_text": "先看这张图，再把后面的判断串起来。",
                "anchor_excerpt": "",
                "target_ids": ["p7:g1"],
                "full_evidence_target_ids": ["p7:g1"],
                "display_flow": [{"kind": "prose", "text": "先看这张图，再把后面的判断串起来。"}],
                "slot_bindings": [],
                "glossary": [],
                "adjacent_bridge": "",
                "reference_links": [],
                "meta": {},
            },
            {
                "segment_id": "ms-body",
                "segment_type": "body",
                "title": "顺着正文读",
                "teaching_text": "正文把结果展开。",
                "anchor_excerpt": "",
                "target_ids": ["p7:g2"],
                "full_evidence_target_ids": ["p7:g1", "p7:g2"],
                "display_flow": [{"kind": "prose", "text": "正文把结果展开。"}],
                "slot_bindings": [],
                "glossary": [],
                "adjacent_bridge": "",
                "reference_links": [],
                "meta": {},
            },
        ],
    }
    experience["meta"] = {
        **experience["meta"],
        "page_dossier": _sample_page_dossier(),
    }

    result = runtime._validate_experience_plan_contract(experience)

    focus_segment = next(row for row in result["teaching_manuscript"]["segments"] if row["segment_type"] == "figure")
    body_segment = next(row for row in result["teaching_manuscript"]["segments"] if row["segment_type"] == "body")

    assert focus_segment["target_ids"] == ["p7:fig-1"]
    assert focus_segment["display_flow"][1]["kind"] == "figure_slot"
    assert focus_segment["display_flow"][1]["target_ids"] == ["p7:fig-1"]
    assert focus_segment["slot_bindings"][0]["target_ids"] == ["p7:fig-1"]
    assert body_segment["target_ids"] == ["p7:p-1"]
    assert any(block["kind"] == "body_slot" and block["target_ids"] == ["p7:p-1"] for block in body_segment["display_flow"])
    assert all(binding["kind"] == "body_slot" for binding in body_segment["slot_bindings"])


def test_build_experience_plan_should_polish_repeated_manuscript_leads_across_segments():
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

    manuscript = experience["teaching_manuscript"]
    texts = [
        str(row.get("teaching_text") or "").strip()
        for row in manuscript["segments"]
        if str(row.get("segment_type") or "").strip() in {"opening", "figure", "body"}
    ]
    leads = [
        runtime._leading_clause_for_manuscript_text(text)  # pylint: disable=protected-access
        for text in texts
    ]

    assert len(texts) == 3
    assert all(leads)
    assert len(set(leads)) == len(leads)
    assert sum("再回到正文" in text for text in texts) <= 1


def test_build_experience_plan_should_suppress_empty_question_panels():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["interaction_modules"] = plan["interaction_modules"] + [
        {
            "module_id": "int_q_1",
            "module_type": "QuestionStarterPanel",
            "target_ids": ["p7:p-1"],
            "title": "Suggested follow-up questions",
            "display_summary": "Suggested follow-up questions",
            "props": {
                "questions": [
                    "What Fig 3 reveals",
                    "Suggested follow-up questions",
                ]
            },
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

    assert all(row["module_type"] != "QuestionStarterPanel" for row in experience["interactive_blocks"])
    assert "question_lab" not in {row["section_type"] for row in experience["main_sections"]}
    assert all(row["beat_type"] != "checkpoint" for row in experience["guided_beats"])


def test_build_experience_plan_should_keep_adjacent_continuity_out_of_external_resource_lane():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页先解释了当前图示的比较基线。",
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
                "continuation_hints": ["当前页延续上一页的图示说明。"],
            }
        ],
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    assert sections["reading_flow"]["meta"]["content_lane"] == "main_narrative"
    assert sections["reading_flow"]["meta"]["adjacent_bridge_cues"][0]["text"].startswith("这里延续了前文")
    assert sections["reading_flow"]["meta"]["adjacent_bridge_cues"][0]["provenance"] == {
        "page": 6,
        "relation": "previous_page",
        "source": "",
        "reference_only": False,
    }
    assert sections["supporting_resources"]["meta"]["content_lane"] == "curated_external_resources"
    assert "adjacent_bridge_cues" not in sections["supporting_resources"]["meta"]


def test_build_experience_plan_should_demote_english_heavy_body_reading_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    raw_body_copy = (
        "We first examined the frequency of insight and found that ChatGPT generated at least one significant insight "
        "in 88.9% of all questions, with Figure 3 breaking concordance and insight out across target learner groups."
    )
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": raw_body_copy,
                    "supporting_points": [raw_body_copy],
                    "reader_facing_notes": ["读正文时留意和前后段落的衔接。"],
                    "tool_findings": [{"tool": "paper_read", "success": True, "output_excerpt": raw_body_copy}],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read"}],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    body_beat = next(row for row in experience["guided_beats"] if row["beat_type"] == "body_segment")
    assert "We first examined" not in sections["reading_flow"]["display_summary"]
    assert "Fig 3" in sections["reading_flow"]["display_summary"]
    assert any(token in sections["reading_flow"]["display_summary"] for token in ("解释链", "解释这些差异", "讲成"))
    assert any(token in sections["reading_flow"]["display_summary"] for token in ("Concordance", "洞见", "判断"))
    assert "We first examined" not in body_beat["display_summary"]
    assert "Fig 3" in body_beat["display_summary"]
    assert any(token in body_beat["display_summary"] for token in ("解释链", "解释这些差异", "讲成"))
    assert any(token in body_beat["display_summary"] for token in ("Concordance", "洞见", "判断"))


def test_build_experience_plan_should_promote_adjacent_bridge_cue_into_body_beat():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页先解释了当前图示的比较基线。",
                "continuation_hints": ["当前页延续上一页的图示说明。"],
                "figure_hints": ["Figure 2：承接到当前页的图示。"],
            }
        ],
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": "先把当前内容放回前后文里。",
                    "supporting_points": [],
                    "reader_facing_notes": ["读正文时留意和前后段落的衔接。"],
                    "tool_findings": [],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read"}],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    body_beat = next(row for row in experience["guided_beats"] if row["beat_type"] == "body_segment")
    assert sections["reading_flow"]["display_summary"].startswith("顺着当前页正文往下读时，也带着前文关于图示说明的铺垫")
    assert body_beat["continuity_note"] == "读到这里时，先接上前文关于图示说明的铺垫，再看作者怎样把当前页的结果讲清楚。"
    assert body_beat["meta"]["adjacent_bridge_cue"]["provenance"]["page"] == 6
    assert "previous_page" not in body_beat["continuity_note"]


def test_build_experience_plan_should_drop_scaffold_continuity_and_use_reader_facing_support_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()

    experience = runtime.build_experience_plan(
        paper_id=78,
        focus_page=7,
        user_intent="help me understand this page",
        reader_profile="curious_generalist",
        focus_section_ids=[],
        compose_payload=_sample_payload(),
        generative_plan=plan,
    )

    beats = {row["beat_type"]: row for row in experience["guided_beats"]}

    assert beats["figure_walkthrough"]["continuity_note"] == ""
    assert beats["body_segment"]["continuity_note"] == ""
    assert beats["concept_bridge"]["continuity_note"] == ""
    assert beats["why_it_matters"]["continuity_note"] == ""
    assert beats["why_it_matters"]["title"] == "读到这里再补背景"
    assert "只在正文需要时补一层" not in beats["why_it_matters"]["summary"]
    assert "而不是把外部资料变成主线" not in beats["why_it_matters"]["summary"]
    assert "把结果放回背景里" not in beats["why_it_matters"]["title"]
    assert "把刚读过的正文段落变成更容易吸收的解释。" not in str(beats["concept_bridge"])


def test_build_experience_plan_should_strip_low_value_packet_links_and_helper_notes_from_experience_meta():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先看 Fig 3，抓住它在这一页承载的关键比较。",
                    "supporting_points": [],
                    "reader_facing_notes": [
                        "外部资源保留少量高相关来源，方便按需展开。",
                        "先用图或关键证据建立抓手，再回到正文核对作者的解释。",
                    ],
                    "tool_findings": [],
                    "public_links": [
                        {
                            "label": "Medvily",
                            "href": "https://medvily.com/usmle-step-1-format-change-2026-step-2-format-change/",
                            "snippet": "Medical exam content farm.",
                        },
                        {
                            "label": "USMLE official",
                            "href": "https://www.usmle.org/sites/default/files/2022-01/USMLE_Content_Outline_0.pdf",
                            "snippet": "Official USMLE outline.",
                        },
                    ],
                    "requested_tools": [{"tool": "web_search", "request_origin": "planner"}],
                },
                {
                    "beat_id": "beat_context",
                    "tool_objectives": ["why_it_matters", "external_comparison"],
                    "summary": "只在正文需要时补一层 USMLE 结构, 评估指标 背景，帮助解释 Fig 3 为什么这样比较，而不是把外部资料变成主线。",
                    "supporting_points": [],
                    "reader_facing_notes": [
                        "外部背景只作为辅助说明，不替代正文主线。",
                        "外部资源保留少量高相关来源，方便按需展开。",
                    ],
                    "tool_findings": [],
                    "public_links": [
                        {
                            "label": "USMLE official",
                            "href": "https://www.usmle.org/sites/default/files/2022-01/USMLE_Content_Outline_0.pdf",
                            "snippet": "Official USMLE outline.",
                        },
                    ],
                    "requested_tools": [{"tool": "web_search", "request_origin": "planner"}],
                },
            ]
        },
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

    packet_lookup = {
        row["beat_id"]: row
        for row in experience["meta"]["tool_enrichment_packet"]["beat_packets"]
    }

    assert packet_lookup["beat_focus"]["public_links"] == [
        {
            "label": "USMLE 官方说明",
            "href": "https://www.usmle.org/sites/default/files/2022-01/USMLE_Content_Outline_0.pdf",
            "domain": "usmle.org",
            "snippet": "Official USMLE outline.",
        }
    ]
    assert "reader_facing_notes" not in packet_lookup["beat_focus"]
    assert "只在正文需要时补一层" not in packet_lookup["beat_context"]["summary"]
    assert "而不是把外部资料变成主线" not in packet_lookup["beat_context"]["summary"]
    assert "Fig 3" in packet_lookup["beat_context"]["summary"]
    assert "reader_facing_notes" not in packet_lookup["beat_context"]


def test_build_experience_plan_should_prefer_teacher_spine_over_generic_helper_preface_in_focus_summary():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    helper_preface = "先看图里最关键的一点：Figure 3 shows that concordance stays high while insight varies by exam step."
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": helper_preface,
                    "supporting_points": [helper_preface],
                    "reader_facing_notes": ["先用图或关键证据建立抓手，再回到正文核对作者的解释。"],
                    "tool_findings": [{"tool": "paper_read", "success": True, "output_excerpt": helper_preface}],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read", "request_origin": "planner"}],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    focus_beat = next(row for row in experience["guided_beats"] if row["beat_type"] == "figure_walkthrough")
    spine = experience["meta"]["teacher_narrative_spine"]

    assert sections["focus_stage"]["display_summary"] == spine["focus_guidance"]
    assert focus_beat["display_summary"] == spine["focus_guidance"]
    assert "先看图里最关键的一点" not in sections["focus_stage"]["display_summary"]
    assert "Figure 3 shows" not in sections["focus_stage"]["display_summary"]


def test_build_experience_plan_should_interleave_guided_beats_with_block_stacks():
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

    guided_beats = experience["guided_beats"]
    body_beats = [row for row in guided_beats if row["beat_type"] == "body_segment"]
    bridge_beats = [row for row in guided_beats if row["beat_type"] in {"concept_bridge", "why_it_matters", "context_bridge"}]

    assert body_beats
    assert body_beats[0]["target_ids"]
    assert body_beats[0]["tool_objectives"]
    assert bridge_beats
    assert any(row["block_stack"] for row in bridge_beats)
    assert all("reader_goal" in row for row in guided_beats)
    assert all("tool_objectives" in row for row in guided_beats)
    assert all("drop_notes" in row for row in guided_beats)


def test_validate_generative_plan_contract_fallback_should_restore_guided_reading_metadata():
    runtime = GenerativeReaderAgentRuntime()

    result = runtime._validate_generative_plan_contract(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {},
            "page_brief": {"storyboard": [], "body_flow_target_ids": []},
            "resource_modules": [{"module_id": 123}],
            "interaction_modules": [],
            "js_widgets": [],
            "used_tools": ["paper_read"],
            "tool_trace": [{"type": "action", "data": {"tool": "paper_read"}}],
            "meta": {
                "planning_brief": {
                    "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
                    "guided_beat_seed": [
                        {
                            "beat_id": "beat_focus",
                            "role": "focus_evidence",
                            "section_type": "focus_stage",
                            "title": "拆解这张图",
                            "reader_goal": "先看图再回正文",
                            "continuity_note": "看完主图后顺着正文往下读。",
                            "target_ids": ["p7:fig-1"],
                            "tool_objectives": ["figure_context"],
                            "priority": 2,
                        }
                    ],
                },
                "planner_output": {
                    "guided_beats": [
                        {
                            "beat_id": "beat_focus",
                            "role": "focus_evidence",
                            "section_type": "focus_stage",
                            "title": "拆解这张图",
                            "reader_goal": "先看图再回正文",
                            "continuity_note": "看完主图后顺着正文往下读。",
                            "target_ids": ["p7:fig-1"],
                            "tool_objectives": ["figure_context"],
                            "drop_notes": ["保留图作为阅读锚点。"],
                            "priority": 2,
                        }
                    ]
                },
                "tool_enrichment_packet": {
                    "beat_packets": [
                        {
                            "beat_id": "beat_focus",
                            "tool_objectives": ["figure_context"],
                            "tool_findings": [
                                {
                                    "tool": "paper_read",
                                    "success": True,
                                    "output_excerpt": "Figure 3 shows insight prevalence and concordance.",
                                }
                            ],
                        }
                    ]
                },
            },
        },
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
    )

    assert result["meta"]["contract_validation"]["status"] == "fallback"
    assert result["meta"]["contract_validation"]["errors_preview"]
    assert result["meta"]["guided_beats_preview"][0]["beat_id"] == "beat_focus"
    assert result["meta"]["guided_beat_count"] == 1
    assert result["meta"]["beat_packet_count"] == 1
    assert result["page_brief"]["body_flow_target_ids"] == ["p7:fig-1", "p7:p-1"]
    focus_storyboard = next(row for row in result["page_brief"]["storyboard"] if row["section_type"] == "focus_stage")
    assert focus_storyboard["beat_id"] == "beat_focus"
    assert focus_storyboard["reader_goal"] == "先看图再回正文"
    assert focus_storyboard["tool_objectives"] == ["figure_context"]
    assert focus_storyboard["drop_notes"] == ["保留图作为阅读锚点。"]


def test_validate_generative_plan_contract_should_materialize_missing_block_ids():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    del plan["resource_modules"][0]["module_id"]
    plan["interaction_modules"][0]["module_id"] = ""
    del plan["js_widgets"][0]["widget_id"]

    result = runtime._validate_generative_plan_contract(  # pylint: disable=protected-access
        parsed=plan,
        page=7,
        user_intent="help me understand this page",
        enrichment_bundle=_sample_payload()["enrichment_bundle"],
    )

    assert result["status"] == "done"
    assert result["meta"]["contract_validation"]["status"] == "validated"
    assert result["meta"]["id_materialization"] == {
        "resource_modules": 1,
        "interaction_modules": 1,
        "js_widgets": 1,
        "status": "repaired_missing_ids",
    }
    assert result["resource_modules"][0]["module_id"].startswith("res_7_")
    assert result["interaction_modules"][0]["module_id"].startswith("int_7_")
    assert result["js_widgets"][0]["widget_id"].startswith("widget_7_")


def test_build_experience_plan_should_surface_guided_runtime_meta():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["page_brief"] = {
        **plan["page_brief"],
        "storyboard": [],
    }
    plan["tool_trace"] = [
        {"type": "action", "data": {"tool": "paper_read", "beat_id": "beat_focus"}},
        {"type": "observation", "data": {"tool": "paper_read", "success": True, "beat_id": "beat_focus"}},
    ]
    plan["meta"] = {
        **plan["meta"],
        "planning_brief": {
            "body_flow_target_ids": ["p7:fig-1", "p7:p-1"],
            "guided_beat_seed": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "拆解这张图",
                    "reader_goal": "先看图再回正文",
                    "continuity_note": "先用图抓住重点，再返回正文。",
                    "target_ids": ["p7:fig-1"],
                    "tool_objectives": ["figure_context"],
                    "priority": 2,
                }
            ],
        },
        "planner_output": {
            "page_objective": "Turn the page into a figure-led explainer.",
            "guided_beats": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "拆解这张图",
                    "reader_goal": "先看图再回正文",
                    "continuity_note": "先用图抓住重点，再返回正文。",
                    "target_ids": ["p7:fig-1"],
                    "tool_objectives": ["figure_context"],
                    "drop_notes": ["优先保留图作为入口。"],
                    "priority": 2,
                }
            ],
        },
        "tool_enrichment_packet": {
            "executed_tools": ["paper_read"],
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先用论文自身证据解释主图。",
                    "supporting_points": ["Figure 3 shows concordance and insight prevalence."],
                    "reader_facing_notes": ["先看图，再回正文。"],
                    "tool_findings": [],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read", "reason": "ground the figure", "priority": "high"}],
                }
            ],
        },
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

    focus_beat = next(row for row in experience["guided_beats"] if row["beat_type"] == "figure_walkthrough")
    assert focus_beat["reader_goal"] == "先抓住本页最强的图示或证据，再回到正文读论证。"
    assert focus_beat["continuity_note"] == "先用图抓住重点，再返回正文。"
    assert focus_beat["tool_objectives"] == ["figure_context"]
    assert focus_beat["drop_notes"] == ["优先保留图作为入口。"]
    assert experience["meta"]["planner_output"]["guided_beats"][0]["beat_id"] == "beat_focus"
    assert experience["meta"]["tool_enrichment_packet"]["beat_packets"][0]["beat_id"] == "beat_focus"
    assert experience["meta"]["tool_trace"] == [
        {"type": "action", "tool": "paper_read", "beat_id": "beat_focus"},
        {"type": "observation", "tool": "paper_read", "beat_id": "beat_focus", "success": True},
    ]
    assert experience["meta"]["tool_trace_summary"]


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
    assert sections["reading_flow"]["meta"]["planner_purpose"] == "把当前页正文与图表顺序保留下来作为主干，再在其上补充解释和外部资源。"
    assert "理解抓手" not in sections["focus_stage"]["display_summary"]
    assert "当前页正文与图表顺序" not in sections["reading_flow"]["display_summary"]
    assert "补充解释和外部资源" not in sections["reading_flow"]["display_summary"]


def test_build_experience_plan_should_strip_heading_and_blog_noise_from_experience_meta():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["tool_trace"] = [
        {
            "type": "observation",
            "data": {
                "tool": "web_scrape",
                "beat_id": "beat_explain",
                "success": True,
                "request_origin": "planner",
                "output": "# Performance of Chat GPT on USMLE",
            },
        },
        {
            "type": "observation",
            "data": {
                "tool": "web_search",
                "beat_id": "beat_explain",
                "success": True,
                "request_origin": "planner",
                "output": "新的工作环境让我们都感觉到压力，但只要积极适应就能成长。",
            },
        },
    ]
    plan["meta"]["tool_enrichment_packet"] = {
        "beat_packets": [
            {
                "beat_id": "beat_explain",
                "requested_tools": [{"tool": "web_scrape", "priority": "medium"}],
                "tool_findings": [
                    {
                        "tool": "web_scrape",
                        "success": True,
                        "output_excerpt": "# Performance of Chat GPT on USMLE",
                    },
                    {
                        "tool": "knowledge_search",
                        "success": True,
                        "output_excerpt": "新的工作环境让我们都感觉到压力，但只要积极适应就能成长。",
                    },
                ],
                "public_links": [],
            }
        ],
        "tool_findings": [
            {
                "beat_id": "beat_explain",
                "tool": "web_scrape",
                "success": True,
                "output_excerpt": "# Performance of Chat GPT on USMLE",
            },
            {
                "beat_id": "beat_explain",
                "tool": "knowledge_search",
                "success": True,
                "output_excerpt": "新的工作环境让我们都感觉到压力，但只要积极适应就能成长。",
            },
        ],
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

    meta_json = json.dumps(experience["meta"], ensure_ascii=False)
    assert "# Performance of Chat GPT on USMLE" not in meta_json
    assert "新的工作环境让我们都感觉到压力" not in meta_json
    _assert_no_reader_surface_noise(meta_json)


def test_build_experience_plan_should_let_teacher_spine_override_generic_focus_filler():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先抓住图里最值得注意的信息。",
                    "supporting_points": ["先抓住图里最值得注意的信息。"],
                    "reader_facing_notes": ["先看图，再回正文。"],
                    "tool_findings": [],
                    "public_links": [],
                    "requested_tools": [{"tool": "paper_read", "request_origin": "planner"}],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]

    assert sections["focus_stage"]["display_summary"] == spine["focus_guidance"]
    assert "先抓住图里最值得注意的信息" not in sections["focus_stage"]["display_summary"]


def test_build_experience_plan_should_reject_marketing_public_links_from_explainer_primary_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    marketing_snippet = (
        "本文深入解读通过USMLE（美国医师执照考试）的临床级大模型Open Evidence如何为医疗领域带来革命性变革。"
        "文章详细拆解其架构创新、数据飞轮、模型精调与安全"
    )
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_explain",
                    "tool_objectives": ["term_explain", "method_background"],
                    "summary": f"先补一层必要的方法背景：{marketing_snippet}",
                    "supporting_points": [
                        f"先补一层方法背景：{marketing_snippet}",
                        "先补一层方法背景：USMLE Step 2 CK Content Outline & Specifications.",
                    ],
                    "reader_facing_notes": ["先掌握方法背景，再看作者在这一页怎么用它。"],
                    "tool_findings": [
                        {
                            "tool": "web_search",
                            "success": True,
                            "output_excerpt": "USMLE Step 2 CK Content Outline & Specifications.",
                            "source_url": "https://www.usmle.org/step-2-ck",
                            "source_kind": "public_web_search",
                            "domain_score": 100,
                            "request_origin": "backfill",
                        }
                    ],
                    "public_links": [
                        {
                            "label": "200亿估值AI医生上岗！美国40%医生都在用，连USMLE都考过了！",
                            "href": "https://modelengine.csdn.net/690c4ef45511483559e2a39d.html",
                            "snippet": marketing_snippet,
                        },
                        {
                            "label": "相关视频",
                            "href": "https://www.youtube.com/shorts/gAGr6V7zTZA",
                        },
                        {
                            "label": "USMLE Step 2 CK Content Outline & Specifications",
                            "href": "https://www.usmle.org/step-2-ck",
                            "snippet": "USMLE Step 2 CK Content Outline & Specifications.",
                        },
                    ],
                    "requested_tools": [
                        {"tool": "knowledge_search", "request_origin": "planner"},
                        {"tool": "web_search", "request_origin": "backfill"},
                    ],
                }
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]
    beat_explain_packet = next(
        row
        for row in experience["meta"]["tool_enrichment_packet"]["beat_packets"]
        if row["beat_id"] == "beat_explain"
    )

    assert sections["explainer_cluster"]["display_summary"] == spine["term_guidance"]
    assert "Open Evidence" not in sections["explainer_cluster"]["display_summary"]
    assert all("csdn.net" not in str(link.get("domain") or "") for link in beat_explain_packet["public_links"])
    assert all("/shorts/" not in str(link.get("href") or "") for link in beat_explain_packet["public_links"])
    assert "Open Evidence" not in json.dumps(beat_explain_packet, ensure_ascii=False)


def test_build_experience_plan_should_override_generic_beat_packet_summaries_and_drop_youtube_focus_links():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": "先抓住图里最值得注意的信息。",
                    "supporting_points": ["先抓住图里最值得注意的信息。"],
                    "public_links": [
                        {
                            "label": "相关视频",
                            "href": "https://www.youtube.com/watch?v=V-tvy4DOZ_M",
                            "snippet": "A quick walkthrough video.",
                        }
                    ],
                    "requested_tools": [{"tool": "paper_read", "request_origin": "planner"}],
                },
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": "先把当前内容放回前后文里。",
                    "supporting_points": ["先把当前内容放回前后文里。"],
                    "requested_tools": [{"tool": "paper_read", "request_origin": "planner"}],
                },
                {
                    "beat_id": "beat_explain",
                    "tool_objectives": ["term_explain", "method_background"],
                    "summary": "先补一层必要的方法背景。",
                    "supporting_points": ["先补一层必要的方法背景。"],
                    "requested_tools": [{"tool": "knowledge_search", "request_origin": "planner"}],
                },
            ]
        },
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

    spine = experience["meta"]["teacher_narrative_spine"]
    packets = {
        str(row.get("beat_id") or "").strip(): row
        for row in experience["meta"]["tool_enrichment_packet"]["beat_packets"]
    }
    sections = {row["section_type"]: row for row in experience["main_sections"]}

    assert packets["beat_focus"]["summary"] == spine["focus_guidance"]
    assert packets["beat_read"]["summary"] == spine["body_guidance"]
    assert packets["beat_explain"]["summary"] == spine["term_guidance"]
    assert packets["beat_focus"]["public_links"] == []
    assert sections["hero"]["display_summary"] == spine["opening"]


def test_build_experience_plan_should_clean_polluted_anchor_terms_and_adjacent_bridge_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {
                "claim_id": "claim_1",
                "text": "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，且洞察频率在不同考试类型间保持一致。",
                "source_node_ids": ["p7:p-1"],
            }
        ],
        "terms_to_explain": [
            {"term": "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，且洞察频率在不同考试类型间保持一致。", "source_node_ids": ["p7:p-1"]},
            {"term": "Chat", "source_node_ids": ["p7:p-1"]},
            {"term": "的回答中产生了至少一", "source_node_ids": ["p7:p-1"]},
            {"term": "个显著洞察", "source_node_ids": ["p7:p-1"]},
            {"term": "augment", "source_node_ids": ["p7:p-1"]},
            {"term": "USMLE", "source_node_ids": ["p7:p-1"]},
            {"term": "Concordance (一致性)", "source_node_ids": ["p7:p-1"]},
        ],
    }
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 8,
                "relation": "next_page",
                "source": "ocr",
                "reference_only": True,
                "summary": "next page implications for medical education and ... ai development",
                "continuation_hints": ["next page implications for medical education and ... ai development"],
                "figure_hints": [],
            }
        ],
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]
    anchor_blob = " ".join(spine["anchor_terms"])
    reader_blob = " ".join(
        [
            experience["hero"]["display_summary"],
            sections["focus_stage"]["display_summary"],
            sections["reading_flow"]["display_summary"],
            json.dumps(sections["reading_flow"]["meta"]["adjacent_page_continuity"], ensure_ascii=False),
        ]
    )

    assert "USMLE" in spine["anchor_terms"]
    assert "context" not in spine["anchor_terms"]
    for marker in (
        "Chat",
        "的回答中产生了至少一",
        "个显著洞察",
        "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察",
    ):
        assert marker not in anchor_blob
        assert marker not in reader_blob
    assert "Concordance (一致性)" not in sections["focus_stage"]["display_summary"]
    assert "augment" not in anchor_blob.lower()
    assert "augment" not in reader_blob.lower()
    assert "ai development" not in reader_blob.lower()
    assert "..." not in reader_blob
    assert "Fig 3" in sections["focus_stage"]["display_summary"]


def test_build_experience_plan_should_drop_augment_style_scaffold_terms_from_narrative_copy():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {
                "claim_id": "claim_1",
                "text": "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察。",
                "source_node_ids": ["p7:p-1"],
            }
        ],
        "terms_to_explain": [
            {"term": "augment", "source_node_ids": ["p7:p-1"]},
            {"term": "Concordance (一致性)", "source_node_ids": ["p7:p-1"]},
            {"term": "USMLE", "source_node_ids": ["p7:p-1"]},
        ],
    }
    plan["page_brief"] = {
        **plan["page_brief"],
        "resource_gaps": ["USMLE 结构"],
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]
    reader_blob = " ".join(
        [
            experience["hero"]["display_summary"],
            sections["focus_stage"]["display_summary"],
            sections["reading_flow"]["display_summary"],
            sections["explainer_cluster"]["display_summary"],
        ]
    ).lower()
    anchor_blob = " ".join(spine["anchor_terms"]).lower()

    assert "augment" not in anchor_blob
    assert "augment" not in reader_blob
    assert "一致性" in reader_blob
    assert "usmle" in reader_blob


def test_build_experience_plan_should_prefer_clean_packet_copy_over_fragment_terms():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    focus_summary = "Fig 3 先把这一页最关键的比较摆在一起，方便看清作者到底在对照什么。"
    body_summary = "正文会顺着 Fig 3 里的比较往下讲，把这些差异解释成作者真正关心的判断。"
    context_summary = "补一层 USMLE 背景，帮助读懂图里为什么要分 Step 1、2CK 和 3。"
    plan["story_substrate"] = {
        **plan["story_substrate"],
        "main_claims": [
            {
                "claim_id": "claim_1",
                "text": "Chat GPT 在 88.9% 的回答中产生了至少一个显著洞察，表明其具有辅助医学学习的潜力。",
                "source_node_ids": ["p7:p-1"],
            }
        ],
        "terms_to_explain": [
            {"term": "表明其具有辅助医学学", "source_node_ids": ["p7:p-1"]},
            {"term": "习的潜力", "source_node_ids": ["p7:p-1"]},
            {"term": "但在不同考试步骤中表", "source_node_ids": ["p7:p-1"]},
            {"term": "现存在差异", "source_node_ids": ["p7:p-1"]},
            {"term": "Concordance (一致性)", "source_node_ids": ["p7:p-1"]},
        ],
    }
    plan["page_brief"] = {
        **plan["page_brief"],
        "resource_gaps": ["USMLE 结构", "评估指标"],
    }
    plan["meta"] = {
        **plan.get("meta", {}),
        "tool_enrichment_packet": {
            "beat_packets": [
                {
                    "beat_id": "beat_focus",
                    "tool_objectives": ["figure_context"],
                    "summary": focus_summary,
                    "supporting_points": [],
                    "reader_facing_notes": [],
                    "public_links": [],
                },
                {
                    "beat_id": "beat_read",
                    "tool_objectives": ["continuation_bridge"],
                    "summary": body_summary,
                    "supporting_points": [],
                    "reader_facing_notes": [],
                    "public_links": [],
                },
                {
                    "beat_id": "beat_context",
                    "tool_objectives": ["why_it_matters"],
                    "summary": context_summary,
                    "supporting_points": [],
                    "reader_facing_notes": [],
                    "public_links": [{"label": "USMLE", "href": "https://www.usmle.org/"}],
                },
            ]
        },
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

    sections = {row["section_type"]: row for row in experience["main_sections"]}
    spine = experience["meta"]["teacher_narrative_spine"]
    anchor_blob = " ".join(spine["anchor_terms"])

    assert spine["focus_guidance"] == focus_summary
    assert spine["body_guidance"] == body_summary
    assert spine["support_guidance"] == context_summary
    assert sections["focus_stage"]["display_summary"] == focus_summary
    assert sections["reading_flow"]["display_summary"] == body_summary
    assert sections["supporting_resources"]["display_summary"] == context_summary
    for marker in ("表明其具有辅助医学学", "习的潜力", "但在不同考试步骤中表", "现存在差异", "结构", "评估指标"):
        assert marker not in anchor_blob


def test_build_experience_plan_should_not_surface_raw_english_adjacent_continuity_in_reader_visible_meta():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["meta"] = {
        **plan.get("meta", {}),
        "adjacent_page_context": [
            {
                "page": 6,
                "relation": "previous_page",
                "source": "ocr",
                "summary": "Continuation of analysis on ChatGPT's performance across exam sections.",
                "continuation_hints": [
                    "Continuation of analysis on ChatGPT's performance across exam sections."
                ],
            },
            {
                "page": 8,
                "relation": "next_page",
                "source": "ocr",
                "summary": "Next section evaluates nonobvious insights in greater detail.",
                "continuation_hints": [
                    "Next section evaluates nonobvious insights in greater detail."
                ],
            },
        ],
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

    reading_flow = next(row for row in experience["main_sections"] if row["section_type"] == "reading_flow")
    continuity_rows = list(dict(reading_flow.get("meta") or {}).get("adjacent_page_continuity") or [])

    assert len(continuity_rows) == 2
    assert all("Continuation of analysis" not in str(row.get("summary") or "") for row in continuity_rows)
    assert all("Next section evaluates" not in str(row.get("summary") or "") for row in continuity_rows)
    assert "Continuation of analysis" not in reading_flow["display_summary"]
    assert "Next section evaluates" not in reading_flow["display_summary"]


def test_build_experience_plan_should_dedupe_semantically_redundant_support_modules():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_1",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "把结果放回背景里",
            "summary": "USMLE 评分框架帮助理解 Fig 3 为什么同时比较 concordance 与 insight。",
            "links": [{"href": "https://www.usmle.org/step-2-ck"}],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_2",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "把结果放回背景里",
            "summary": "USMLE 评分框架帮助理解 Fig 3 为什么同时比较 concordance 与 insight。",
            "links": [{"href": "https://doi.org/10.1001/example"}],
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

    support_section = next(row for row in experience["main_sections"] if row["section_type"] == "supporting_resources")
    assert support_section["resource_module_ids"] == ["res_1"]


def test_build_experience_plan_should_keep_only_reader_worthy_support_resources():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_fig_source",
            "module_type": "FigureSourceCard",
            "target_ids": ["p7:fig-1"],
            "title": "Figure source",
            "summary": "Original figure asset.",
            "links": [{"href": "https://doi.org/10.1371/journal.pdig.0000198.g003"}],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_weak",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "Background reference",
            "summary": "Weak external background.",
            "links": [{"href": "https://www.celap.org.cn/article/usmle-overview"}],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_strong",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "USMLE background",
            "summary": "Official exam structure context.",
            "links": [{"href": "https://www.usmle.org/step-exams"}],
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

    assert [row["module_id"] for row in experience["supporting_resources"]] == ["res_strong"]
    assert all(
        "doi.org/10.1371/journal.pdig.0000198.g003" not in str(link.get("href") or "")
        for row in experience["supporting_resources"]
        for link in list(row.get("links") or [])
    )
    assert all(
        "celap.org.cn" not in str(link.get("domain") or "")
        for row in experience["supporting_resources"]
        for link in list(row.get("links") or [])
    )


def test_build_experience_plan_should_allow_zero_supporting_resources_when_only_weak_links_exist():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_fig_source",
            "module_type": "FigureSourceCard",
            "target_ids": ["p7:fig-1"],
            "title": "Figure source",
            "summary": "Original figure asset.",
            "links": [{"href": "https://doi.org/10.1371/journal.pdig.0000198.g003"}],
            "source": "web",
            "interaction_mode": "stacked_cards",
            "meta": {},
        },
        {
            "module_id": "res_weak",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "Library mirror",
            "summary": "Mirror copy.",
            "links": [
                {"href": "https://lib.smu.edu.cn/resource/usmle"},
                {"href": "https://www.celap.org.cn/article/usmle-overview"},
            ],
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

    assert experience["supporting_resources"] == []
    assert all(row["section_type"] != "supporting_resources" for row in experience["main_sections"])


def test_build_experience_plan_should_drop_generic_fallback_support_card_without_links():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_generic_fallback",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "延伸资源",
            "display_title": "补充背景与上下文",
            "summary": "这组背景资料只负责补一层解释，帮助你把刚读过的正文放回上下文。",
            "display_summary": "补充少量真正需要的外部背景，帮助理解正文。",
            "links": [],
            "source": "fallback",
            "interaction_mode": "stacked_cards",
            "meta": {"source_quality": "none"},
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

    assert experience["supporting_resources"] == []
    assert all(row["section_type"] != "supporting_resources" for row in experience["main_sections"])


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


@pytest.mark.asyncio
async def test_build_plan_should_run_staged_runtime_with_planner_and_tool_enricher(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    planner_response = {
        "content": """{
            "version":"v1",
            "page_objective":"Turn the page into a figure-led explainer.",
            "narrative_strategy":"Start from the figure, then bridge into the key paragraph.",
            "section_strategy":["hero","focus_stage","reading_flow","supporting_resources"],
            "tool_requests":[
                {
                    "tool":"paper_read",
                    "arguments":{"query":"Figure 3 concordance insight","top_k":4},
                    "reason":"Ground the strongest visual claim in the paper.",
                    "priority":"high"
                }
            ],
            "resource_objectives":["USMLE context"],
            "widget_focus":"Fig 3",
            "page_generation_notes":["Keep the page compact and evidence-led."]
        }""",
    }
    generation_response = {
        "content": """{
            "version":"v1",
            "status":"done",
            "shell_mode":"resource_augmented_reader",
            "story_substrate":{
                "page_id":"p7",
                "main_claims":[{"claim_id":"claim_1","text":"Figure 3 is the main evidence.","source_target_ids":["p7:fig-1"]}],
                "evidence_units":[{"evidence_id":"e1","kind":"figure","role":"primary_visual_evidence","source_target_ids":["p7:fig-1"]}],
                "terms_to_explain":[],
                "background_gaps":[{"topic":"USMLE context","reason":"reader may not know the exam structure"}],
                "narrative_turns":[{"turn_id":"turn_1","kind":"figure_focus","label":"Read Fig 3 first","target_ids":["p7:fig-1"]}],
                "meta":{}
            },
            "page_brief":{
                "version":"v1",
                "page_goal":"Explain the figure before the prose details.",
                "reader_type":"curious_generalist",
                "page_archetype":"figure_explainer",
                "hero_angle":"Anchor on Fig 3, then unpack the supporting paragraph.",
                "primary_focus_target_id":"p7:fig-1",
                "secondary_support_target_ids":["p7:p-1"],
                "reading_path":["hero_summary","focus_evidence","reading_flow","supporting_resources"],
                "interaction_opportunities":["expand_focus_panels"],
                "resource_gaps":["USMLE context"],
                "experience_hooks":["Figure-first guided tour"],
                "resource_strategy":"Use the paper evidence first, then add one compact context card.",
                "meta":{}
            },
            "resource_modules":[
                {
                    "module_id":"res_1",
                    "module_type":"RelatedResourceCard",
                    "target_ids":["p7:p-1"],
                    "title":"USMLE background",
                    "summary":"A small amount of official context helps interpret the page.",
                    "links":[{"label":"USMLE overview","href":"https://www.usmle.org/"}],
                    "source":"web",
                    "interaction_mode":"stacked_cards",
                    "meta":{}
                }
            ],
            "interaction_modules":[],
            "js_widgets":[],
            "rationale":["Use the figure as the anchor and keep the page compact."]
        }""",
    }

    class _FakeLLM:
        def __init__(self):
            self.calls = []

        async def chat(self, *, messages, system_prompt, temperature, max_tokens):
            self.calls.append(
                {
                    "prompt": str(messages[0]["content"]),
                    "system_prompt": system_prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
            )
            if "planner stage" in system_prompt:
                return planner_response
            if "page-generation stage" in system_prompt:
                return generation_response
            raise AssertionError(f"Unexpected system prompt: {system_prompt}")

    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        async def execute(self, tool_name, **arguments):
            self.calls.append((tool_name, arguments))
            return SimpleNamespace(
                success=True,
                output="Figure 3 highlights answer-explanation agreement and insight prevalence.",
                error=None,
                data={
                    "public_links": [
                        {"label": "USMLE overview", "href": "https://www.usmle.org/"}
                    ]
                },
            )

    llm = _FakeLLM()
    registry = _FakeRegistry()
    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: {"paper_read", "knowledge_search", "web_search"})
    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=llm))

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="做成更丰富的生成式阅读网页",
        compose_payload=_sample_payload(),
        tool_registry=registry,
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "summary": "上一页介绍了图的背景。",
                "body_text": "上一页正文补充了指标上下文。",
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
                "tables": [],
                "equations": [],
                "continuation_hints": ["当前页延续上一页的图示阅读。"],
            }
        ],
        page_dossier={
            "focus_page": 7,
            "current_page": {"page": 7, "targets": [{"target_id": "p7:fig-1", "kind": "figure"}]},
        },
    )

    assert result["status"] == "done"
    assert result["used_tools"] == ["paper_read", "web_search"]
    assert registry.calls == [
        ("paper_read", {"query": "Figure 3 concordance insight", "top_k": 4}),
        ("web_search", {"query": "USMLE context Fig 3", "max_results": 5}),
    ]
    assert len(llm.calls) == 2
    assert "tool_budget=" in llm.calls[0]["prompt"]
    assert "planner_output=" in llm.calls[1]["prompt"]
    assert "tool_enrichment_packet=" in llm.calls[1]["prompt"]
    assert result["meta"]["planning_brief"]["tool_budget"]["max_tool_requests"] >= 1
    assert result["meta"]["tool_budget"]["max_tool_requests"] >= 1
    assert result["meta"]["planner_output"]["page_objective"] == "Turn the page into a figure-led explainer."
    assert result["meta"]["planner_output"]["guided_beats"]
    assert result["meta"]["planner_output"]["tool_requests"][0]["beat_id"] == "beat_focus"
    assert result["meta"]["tool_enrichment_packet"]["executed_tools"] == ["paper_read", "web_search"]
    assert result["meta"]["tool_enrichment_packet"]["beat_packets"]
    stage_ids = [row["stage_id"] for row in result["meta"]["runtime_stage_trace"]]
    assert stage_ids[:2] == ["dossier_input", "planning_brief"]
    assert "planner" in stage_ids
    assert "tool_enricher" in stage_ids
    assert "page_generation" in stage_ids


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
    sections = {row["section_type"]: row for row in experience["main_sections"]}
    section_titles = {row["section_type"]: row["title"] for row in experience["main_sections"]}
    assert section_titles["focus_stage"] == "拆解这张图"
    assert section_titles["reading_flow"] == "正文如何展开这些结果"
    assert sections["reading_flow"]["target_ids"] == ["p7:p-1"]
    assert sections["reading_flow"]["meta"]["body_flow_mode"] == "full_page"
    assert sections["reading_flow"]["meta"]["deferred_visual_target_ids"] == ["p7:fig-1"]
    assert experience["meta"]["page_archetype"] == "figure_explainer"
    assert experience["meta"]["contract_validation"]["status"] == "validated"


def test_build_experience_plan_should_keep_fallback_hero_copy_reader_facing():
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

    assert "复用清洗后的正文阅读流作为主画布" not in " ".join(fallback["rationale"])
    assert "基于清洗后阅读流构建的引导式页面体验" not in str(experience["hero"]["display_subtitle"])
    assert "复用清洗后的正文阅读流作为主画布" not in str(experience["hero"]["display_summary"])


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
        tool_trace=[
            {
                "type": "observation",
                "data": {
                    "tool": "web_search",
                    "success": True,
                    "input": {"query": "USMLE Step 1 official overview"},
                    "output": "Found the official Step 1 overview page.",
                },
            }
        ],
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页介绍了研究设计。",
                "figures": [{"label": "Figure 2", "description": "上一页图示介绍研究流程。"}],
                "tables": [],
                "equations": [],
                "continuation_hints": ["当前页延续上一页的图示说明。"],
            }
        ],
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
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页解释了 Figure 3 的背景。",
                "body_text": "上一页正文提供了 figure 的延续解释。",
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
            }
        ],
        runtime_stage_trace=[
            {"stage_id": "planner", "status": "done"},
            {"stage_id": "tool_enricher", "status": "done"},
            {"stage_id": "experience_materialization", "status": "done"},
        ],
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
        adjacent_page_context=[
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "summary": "上一页解释了 Figure 3 的背景。",
                "body_text": "上一页正文提供了 figure 的延续解释。",
                "figures": [{"label": "Figure 2", "description": "承接到当前页的图示。"}],
            }
        ],
        runtime_stage_trace=[
            {"stage_id": "planner", "status": "done"},
            {"stage_id": "tool_enricher", "status": "done"},
            {"stage_id": "experience_materialization", "status": "done"},
        ],
    )

    assert result["meta"]["display_copy_contract"] == "display_copy_v3"
    assert "洞见" in result["story_substrate"]["main_claims"][0]["display_text"]
    assert "Fig 3" in result["story_substrate"]["main_claims"][0]["display_text"] or "Results" in result["story_substrate"]["main_claims"][0]["display_text"]
    assert result["resource_modules"][0]["display_title"] == "读到这里再补的背景"
    assert result["resource_modules"][0]["display_summary"] == "这组背景资料只负责帮你读懂图里的比较对象和现实含义，不替代正文。"
    assert result["interaction_modules"][0]["display_title"] == "接下来值得追问的问题"
    assert result["interaction_modules"][0]["display_summary"] == "把刚读懂的内容变成几个追问，帮助你继续核对而不是再看一遍摘要。"
    assert result["js_widgets"][0]["display_title"] == "逐面板理解 Fig 3"
    assert result["js_widgets"][0]["props"]["panels"][0]["display_label"] == "整图概览"
    assert result["js_widgets"][0]["props"]["panels"][0]["display_summary"] == "先把整张图当成进入这一页的主要视觉入口。"
    assert result["meta"]["resource_strategy"] == "Bring in official context before explanatory modules."
    assert result["meta"]["used_tools"] == ["web_search"]
    assert result["meta"]["tool_trace_summary"][0]["tool"] == "web_search"
    assert result["meta"]["adjacent_page_context"][0]["page"] == 6
    assert result["meta"]["adjacent_page_context"][0]["figure_count"] == 1
    assert result["meta"]["runtime_stage_trace"][-1]["stage_id"] == "experience_materialization"


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


def test_finalize_plan_should_drop_weak_or_raw_supporting_resources_when_no_reader_worthy_links():
    runtime = GenerativeReaderAgentRuntime()
    payload = _sample_payload()

    result = runtime._finalize_plan(  # pylint: disable=protected-access
        parsed={
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "resource_modules": [
                {
                    "module_id": "res_fig",
                    "module_type": "FigureSourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Fig 3 数据来源",
                    "summary": "Raw figure asset.",
                    "links": [
                        {
                            "label": "doi.org",
                            "href": "https://doi.org/10.1371/journal.pdig.0000198.g003",
                            "snippet": "Raw figure asset.",
                        }
                    ],
                },
                {
                    "module_id": "res_weak",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "USMLE 考试结构背景",
                    "summary": "Weak support only.",
                    "links": [
                        {
                            "label": "CELAP",
                            "href": "https://www.celap.org.cn/attach/0/1212041032371255438.pdf",
                            "snippet": "Weak mirror.",
                        },
                        {
                            "label": "Library mirror",
                            "href": "http://lib.smu.edu.cn/article/detail/190",
                            "snippet": "Library mirror.",
                        },
                    ],
                },
                {
                    "module_id": "res_good",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:p-1"],
                    "title": "Related resources",
                    "summary": "Attach a small set of public references or background material directly relevant to this passage.",
                    "links": [
                        {
                            "label": "Official USMLE",
                            "href": "https://www.usmle.org/step-exams/step-1",
                            "snippet": "Official overview.",
                        }
                    ],
                },
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

    module_ids = [row["module_id"] for row in result["resource_modules"]]
    assert "res_fig" not in module_ids
    assert "res_weak" not in module_ids
    assert module_ids == ["res_good"]


def test_validate_experience_plan_contract_should_strip_stale_weak_supporting_resources():
    runtime = GenerativeReaderAgentRuntime()
    plan = _sample_done_plan()
    plan["resource_modules"] = [
        {
            "module_id": "res_strong",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "USMLE background",
            "summary": "Official exam structure context.",
            "links": [{"href": "https://www.usmle.org/step-exams"}],
            "source": "web",
            "interaction_mode": "stacked_cards",
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
    experience["supporting_resources"] = [
        *list(experience.get("supporting_resources") or []),
        {
            "module_id": "res_fig_raw",
            "module_type": "FigureSourceCard",
            "target_ids": ["p7:fig-1"],
            "title": "Fig 3 数据来源",
            "summary": "Raw figure asset",
            "links": [{"href": "https://doi.org/10.1371/journal.pdig.0000198.g003"}],
            "source": "agent",
            "interaction_mode": "",
            "meta": {},
        },
        {
            "module_id": "res_generic",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "延伸资源",
            "display_title": "补充背景与上下文",
            "summary": "这组背景资料只负责补一层解释，帮助你把刚读过的正文放回上下文。",
            "display_summary": "补充少量真正需要的外部背景，帮助理解正文。",
            "links": [],
            "source": "fallback",
            "interaction_mode": "stacked_cards",
            "meta": {"source_quality": "none"},
        },
        {
            "module_id": "res_weak_links",
            "module_type": "RelatedResourceCard",
            "target_ids": ["p7:p-1"],
            "title": "USMLE 考试结构背景",
            "summary": "Weak support only.",
            "links": [
                {"href": "https://www.celap.org.cn/attach/0/1212041032371255438.pdf"},
                {"href": "http://lib.smu.edu.cn/article/detail/190"},
            ],
            "source": "agent",
            "interaction_mode": "",
            "meta": {},
        },
    ]
    for section in experience["main_sections"]:
        if section.get("section_type") == "supporting_resources":
            section["resource_module_ids"] = [
                "res_strong",
                "res_fig_raw",
                "res_generic",
                "res_weak_links",
            ]

    validated = runtime._validate_experience_plan_contract(experience)  # pylint: disable=protected-access

    assert [row["module_id"] for row in validated["supporting_resources"]] == ["res_strong"]
    support_section = next((row for row in validated["main_sections"] if row["section_type"] == "supporting_resources"), None)
    assert support_section is not None
    assert support_section["resource_module_ids"] == ["res_strong"]


def test_normalize_public_links_should_drop_low_value_domains_without_justification():
    runtime = GenerativeReaderAgentRuntime()

    links = runtime._normalize_public_links(  # pylint: disable=protected-access
        [
            {"label": "Official USMLE", "href": "https://www.usmle.org/", "snippet": "Official overview."},
            {"label": "ResearchGate", "href": "https://www.researchgate.net/publication/123", "snippet": "Mirror copy."},
            {"label": "Author video walkthrough", "href": "https://www.youtube.com/watch?v=123", "snippet": "Video walkthrough from the paper discussion."},
            {"label": "OreateAI", "href": "https://oreateai.com/usmle", "snippet": "AI summary."},
            {"label": "Zhihu", "href": "https://www.zhihu.com/question/1", "snippet": "Forum thread."},
            {"label": "LinkedIn", "href": "https://www.linkedin.com/pulse/usmle", "snippet": "Social post."},
            {"label": "Weproedu", "href": "https://www.weproedu.com/usmle", "snippet": "SEO article."},
            {"label": "Medvily", "href": "https://medvily.com/usmle-step-1-format-change-2026-step-2-format-change/", "snippet": "Medical exam content farm."},
            {"label": "medtigo.com", "href": "https://medtigo.com/usmle", "snippet": "Medical exam blog."},
            {"label": "system.com", "href": "https://www.system.com/usmle", "snippet": "Vendor marketing page."},
            {"label": "facebook.com", "href": "https://www.facebook.com/usmle/posts/1", "snippet": "Social post."},
            {"label": "baigemed.com", "href": "http://www.baigemed.com/blog/usmle", "snippet": "Personal exam story."},
        ],
        limit=4,
    )

    domains = [row["domain"] for row in links]
    assert domains == ["usmle.org"]


def test_normalize_public_links_should_allow_low_value_domain_with_explicit_justification():
    runtime = GenerativeReaderAgentRuntime()

    links = runtime._normalize_public_links(  # pylint: disable=protected-access
        [
            {
                "label": "Author webinar recording",
                "href": "https://www.youtube.com/watch?v=123",
                "snippet": "<p>Recorded walkthrough.</p>",
                "justification": "Only public recording from the paper authors.",
            }
        ],
        limit=2,
    )

    assert links == [
        {
            "label": "相关视频",
            "href": "https://www.youtube.com/watch?v=123",
            "domain": "youtube.com",
            "snippet": "Recorded walkthrough.",
            "justification": "Only public recording from the paper authors.",
        }
    ]


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


@pytest.mark.asyncio
async def test_build_plan_should_continue_with_deterministic_planner_output_when_planner_times_out(monkeypatch):
    runtime = GenerativeReaderAgentRuntime()

    class _FakeLLM:
        async def chat(self, *args, **kwargs):
            return {"content": "{}"}

    class _FakeRegistry:
        async def execute(self, *args, **kwargs):
            return {"success": True, "tool_name": "paper_read", "output": "paper observation"}

    stage_calls = {"count": 0}

    async def _timeout_stage(**kwargs):
        stage_calls["count"] += 1
        if stage_calls["count"] == 1:
            raise TimeoutError()
        return _sample_done_plan(), None

    async def _deterministic_tool_stage(**kwargs):
        return [], [], {}

    monkeypatch.setattr(runtime_module, "resolve_generative_reader_agent_tool_whitelist", lambda: {"paper_read"})
    monkeypatch.setattr(runtime, "_build_llm", AsyncMock(return_value=_FakeLLM()))
    monkeypatch.setattr(runtime, "_run_json_stage", _timeout_stage)
    monkeypatch.setattr(runtime, "_execute_planner_tool_requests", _deterministic_tool_stage)

    result = await runtime.build_plan(
        user_id=1,
        page=7,
        user_intent="help me understand this page",
        compose_payload=_sample_payload(),
        tool_registry=_FakeRegistry(),
        allowed_tool_names=["paper_read"],
    )

    assert result["status"] == "done"
    assert "planner_output" in result["meta"]
    planner_stage = next(row for row in result["meta"]["runtime_stage_trace"] if row["stage_id"] == "planner")
    assert planner_stage["status"] == "timeout_fallback"
    assert result["story_substrate"]["main_claims"]
    assert result["page_brief"]["storyboard"]


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
