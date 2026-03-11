import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.models.knowledge import DocumentStatus
from app.models.literature import KnowledgeLinkStatus


class _FakeResult:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        if not self._results:
            return _FakeResult(rows=[])
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_stream_paper_pdf_reads_local_file(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "paper_10.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%EOF\n")

    paper = SimpleNamespace(id=10, title="Test Paper", pdf_path=str(pdf_path), pdf_url="https://example.com/a.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    response = await literature_api.stream_paper_pdf(
        paper_id=10,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=99),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == pdf_path
    assert response.headers.get("content-disposition", "").startswith("inline;")


@pytest.mark.asyncio
async def test_stream_paper_pdf_raises_404_when_missing(monkeypatch):
    paper = SimpleNamespace(id=11, title="Missing Paper", pdf_path=None, pdf_url="https://example.com/missing.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)

    with pytest.raises(HTTPException) as exc:
        await literature_api.stream_paper_pdf(
            paper_id=11,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=99),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_forwards_cache_clone_flags(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **kwargs):
            captured.update(kwargs)
            return {
                "snapshot_id": "snapshot_fast",
                "session_id": "session_fast",
                "page": 7,
                "paper_id": 78,
                "source_signature": "sig-fast",
                "ui_plan": {"components": [], "layout": {}, "style_tokens": {}, "trace_meta": {}},
                "assets": [],
            }

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        selected_kb_id=84,
        snapshot_label="snapshot_fast",
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    response = await literature_api.create_reader_composed_review_session(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response["snapshot_id"] == "snapshot_fast"
    assert captured["paper"] is paper
    assert captured["page"] == 7
    assert captured["prefer_cache_clone"] is True
    assert captured["allow_recompute_on_cache_miss"] is False


@pytest.mark.asyncio
async def test_create_reader_composed_review_session_returns_404_for_cache_miss(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeService:
        async def create_review_session(self, **_kwargs):
            raise ValueError("review_cache_not_found")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeService())

    payload = literature_api.ReaderComposeReviewSessionRequest(
        page=7,
        prefer_cache_clone=True,
        allow_recompute_on_cache_miss=False,
    )

    with pytest.raises(HTTPException) as exc:
        await literature_api.create_reader_composed_review_session(
            paper_id=78,
            payload=payload,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=5),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "review_cache_not_found"


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_build_from_compose_payload(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {
                    "version": "v1",
                    "targets": [
                        {
                            "target_id": "p7:paragraph_1",
                            "node_id": "paragraph_1",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                            "title": "Main body",
                            "excerpt": "excerpt",
                            "source_block_ids": ["p7_dm_p7_l009_b001"],
                            "source_atom_ids": [],
                            "section_label": "Results",
                            "figure_label": "",
                            "suggested_resource_types": ["related_public_resource"],
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["Use public resources around the body paragraph."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "tool_trace": [],
                "meta": {"page": 7},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search", "web_search", "web_scrape"}

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页承接段落",
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "下一页延续段落",
            },
        ]

    async def _fake_plan_cache_get(_cache_key: str):
        captured["plan_cache_get"] = True
        return None, "none"

    async def _fake_plan_cache_set(_cache_key: str, payload: dict):
        captured["plan_cache_set_payload"] = payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_plan_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_plan_cache_set)

    payload = literature_api.ReaderGenerativePlanRequest(
        page=7,
        selected_kb_id=84,
        force_refresh=False,
        regenerate=False,
        style_intent="reader_workbench",
        user_intent="Enrich this page with public resources",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 7
    assert response.plan.shell_mode == "resource_augmented_reader"
    assert response.compose_build_mode == "compose_agent_simplified"
    assert response.compose_source_signature == "compose-sig"
    assert response.source_sig_hash == "sig-hash"
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.plan_cache_hit is False
    assert response.plan_cache_layer == "none"
    assert response.enrichment_bundle.targets[0].target_id == "p7:paragraph_1"
    assert response.scheme_choice.scheme_id == "reading_flow_stack"
    assert captured["compose_kwargs"]["paper"] is paper
    assert captured["compose_kwargs"]["page"] == 7
    assert captured["compose_kwargs"]["selected_kb_id"] == 84
    assert captured["runtime_kwargs"]["user_id"] == 5
    assert captured["runtime_kwargs"]["page"] == 7
    assert captured["runtime_kwargs"]["user_intent"] == "Enrich this page with public resources"
    assert captured["runtime_kwargs"]["tool_registry"] is not None
    assert captured["runtime_kwargs"]["allowed_tool_names"] == ["knowledge_search", "paper_read", "web_scrape", "web_search"]
    assert captured["runtime_kwargs"]["adjacent_page_context"][0]["relation"] == "previous_page"
    assert captured["plan_cache_get"] is True
    assert captured["plan_cache_set_payload"]["status"] == "done"
    assert captured["adjacent_context_kwargs"]["focus_page"] == 7
    assert captured["tool_registry_kwargs"]["paper"] is paper
    assert captured["tool_registry_kwargs"]["selected_kb_id"] == 84


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_allow_missing_selected_kb(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["runtime_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["No KB required."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": [],
                "tool_trace": [],
                "meta": {"page": 1},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return None, set()

    async def _fake_cache_get(_cache_key):
        return None, "none"

    async def _fake_cache_set(_cache_key, payload, ttl_seconds=3600):
        captured["cached_plan"] = payload

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderGenerativePlanRequest(
        page=1,
        selected_kb_id=None,
        user_intent="Build a safe default experience",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 1
    assert response.plan.status == "done"
    assert captured["compose_kwargs"]["selected_kb_id"] is None
    assert captured["tool_registry_kwargs"]["selected_kb_id"] is None
    assert captured["runtime_kwargs"]["tool_registry"] is None
    assert captured["runtime_kwargs"]["allowed_tool_names"] == []
    assert captured["runtime_kwargs"]["adjacent_page_context"] == []
    assert captured["cached_plan"]["status"] == "done"


@pytest.mark.asyncio
async def test_get_reader_composed_generative_plan_should_use_plan_cache(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="db_compatible",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search"}

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页补充",
            }
        ]

    async def _fake_cache_get(_cache_key):
        return ({
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "rationale": ["cached plan"],
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "used_tools": ["paper_read"],
            "tool_trace": [],
            "meta": {"page": 7},
        }, "redis")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_cache_get)

    class _ExplodingRuntime:
        async def build_plan(self, **kwargs):
            raise AssertionError("runtime should not be called when plan cache hits")

    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _ExplodingRuntime())

    payload = literature_api.ReaderGenerativePlanRequest(
        page=7,
        selected_kb_id=84,
        user_intent="Enrich this page with public resources",
    )

    response = await literature_api.get_reader_composed_generative_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.page == 7
    assert response.plan.status == "done"
    assert response.plan.rationale == ["cached plan"]
    assert response.plan_cache_hit is True
    assert response.plan_cache_layer == "redis"
    assert response.compose_build_mode == "compose_agent_simplified"
    assert response.compose_source_signature == "compose-sig"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_build_and_cache_experience_plan(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
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
                            "source_block_ids": ["p7_dm_p7_l007_b001"],
                            "source_atom_ids": [],
                            "section_label": "Results",
                            "figure_label": "Fig 3",
                            "suggested_resource_types": ["figure_explainer", "related_public_resource"],
                        }
                    ],
                    "resource_modules": [],
                    "interaction_modules": [],
                    "meta": {},
                },
            }
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["plan_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "rationale": ["Use figure-first reading."],
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "story_substrate": {
                    "page_id": "p7",
                    "main_claims": [{"claim_id": "claim_1", "text": "Figure 3 is key", "source_target_ids": ["p7:fig-1"]}],
                },
                "page_brief": {
                    "version": "v1",
                    "page_goal": "Explain Figure 3 first.",
                    "reader_type": "curious_generalist",
                    "primary_focus_target_id": "p7:fig-1",
                    "secondary_support_target_ids": [],
                    "reading_path": ["hero_summary", "focus_evidence", "reading_flow"],
                    "interaction_opportunities": [],
                    "resource_gaps": [],
                    "meta": {"page": 7},
                },
                "meta": {"notes": "test"},
            }

        def build_experience_plan(self, **kwargs):
            captured["experience_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Experience title",
                "page_story_subtitle": "Experience subtitle",
                "narrative_goal": "Explain the page",
                "hero": {"title": "Hero", "subtitle": "", "summary": "Summary", "focus_label": "Fig 3", "target_ids": ["p7:fig-1"], "claim_ids": [], "meta": {}},
                "main_sections": [{"section_id": "hero", "section_type": "hero", "title": "Hero", "summary": "", "target_ids": ["p7:fig-1"], "layout_variant": "editorial_hero", "resource_module_ids": [], "interaction_module_ids": [], "widget_ids": [], "meta": {}}],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence", "reading_flow"],
                "used_tools": ["paper_read"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return object(), {"paper_read", "knowledge_search"}

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_gen_cache_set(_cache_key, payload, ttl_seconds=3600):
        captured["cached_generative"] = payload

    async def _fake_exp_cache_set(_cache_key, payload, ttl_seconds=3600):
        captured["cached_experience"] = payload

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return [
            {
                "page": 6,
                "relation": "previous_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "上一页补充",
            },
            {
                "page": 8,
                "relation": "next_page",
                "reference_only": True,
                "source": "vlflash_page_ocr",
                "text": "下一页补充",
            },
        ]

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.focus_page == 7
    assert response.plan.status == "done"
    assert response.plan.page_story_title == "Experience title"
    assert response.generative_plan.status == "done"
    assert response.generative_plan_cache_hit is False
    assert response.experience_cache_hit is False
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.compose_source_signature == "compose-sig"
    assert captured["cached_generative"]["status"] == "done"
    assert captured["cached_experience"]["status"] == "done"
    assert captured["plan_kwargs"]["adjacent_page_context"][0]["page"] == 6
    assert captured["adjacent_context_kwargs"]["focus_page"] == 7


@pytest.mark.asyncio
async def test_get_reader_experience_plan_cached_should_derive_experience_when_generative_plan_exists(monkeypatch):
    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78)

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {
                    "targets": [
                        {
                            "target_id": "p7:paragraph_15",
                            "node_id": "p7:paragraph_15",
                            "target_kind": "paragraph",
                            "component_type": "ParagraphProse",
                        }
                    ]
                },
            }

    async def _fake_gen_cache_get(_cache_key):
        return {
            "version": "v1",
            "status": "done",
            "story_substrate": {"page_id": "p7"},
            "page_brief": {"page_goal": "Explain the figure", "reading_path": ["hero_summary", "focus_evidence"]},
            "resource_modules": [],
            "interaction_modules": [],
            "js_widgets": [],
            "meta": {},
        }, "redis"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    class _FakeRuntime:
        def build_experience_plan(self, **kwargs):
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 7,
                "reader_profile": "curious_generalist",
                "page_story_title": "Paper 78 Experience",
                "page_story_subtitle": "A generated page.",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary", "focus_evidence"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_exp_cache_set(_cache_key, _payload):
        return None

    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.compose_status == "done"
    assert response.cache_hit is True
    assert response.cache_layer == "redis"
    assert response.generative_plan_cache_hit is True
    assert response.experience_cache_hit is True
    assert response.generative_plan.status == "done"
    assert response.plan.status == "done"
    assert response.experience_cache_layer == "derived"


@pytest.mark.asyncio
async def test_get_reader_experience_plan_should_allow_missing_selected_kb(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured: dict[str, object] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **kwargs):
            captured["compose_kwargs"] = kwargs
            payload = {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "scheme_choice": {"scheme_id": "reading_flow_stack", "label": "Reading Flow"},
                "enrichment_bundle": {"version": "v1", "targets": [], "resource_modules": [], "interaction_modules": [], "meta": {}},
            }
            meta = SimpleNamespace(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        async def build_plan(self, **kwargs):
            captured["plan_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "draft",
                "shell_mode": "resource_augmented_reader",
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "meta": {},
            }

        def build_experience_plan(self, **kwargs):
            captured["experience_kwargs"] = kwargs
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": 1,
                "reader_profile": "curious_generalist",
                "page_story_title": "Fallback experience",
                "page_story_subtitle": "No KB selected.",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": ["hero_summary"],
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**kwargs):
        captured["tool_registry_kwargs"] = kwargs
        return None, set()

    async def _fake_adjacent_context(**kwargs):
        captured["adjacent_context_kwargs"] = kwargs
        return []

    async def _fake_gen_cache_get(_cache_key):
        return None, "none"

    async def _fake_exp_cache_get(_cache_key):
        return None, "none"

    async def _fake_gen_cache_set(_cache_key, payload, ttl_seconds=3600):
        captured["cached_generative"] = payload

    async def _fake_exp_cache_set(_cache_key, payload, ttl_seconds=3600):
        captured["cached_experience"] = payload

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=1,
        focus_page=1,
        selected_kb_id=None,
        user_intent="Create a safe default experience",
        reader_profile="curious_generalist",
    )

    response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert response.focus_page == 1
    assert response.plan.status == "done"
    assert captured["compose_kwargs"]["selected_kb_id"] is None
    assert captured["tool_registry_kwargs"]["selected_kb_id"] is None
    assert captured["plan_kwargs"]["tool_registry"] is None
    assert captured["plan_kwargs"]["allowed_tool_names"] == []
    assert captured["plan_kwargs"]["adjacent_page_context"] == []
    assert captured["cached_generative"]["status"] == "draft"
    assert captured["cached_experience"]["status"] == "done"


@pytest.mark.asyncio
async def test_reader_experience_flow_should_progress_from_seed_to_full_plan_then_cache_hit(monkeypatch):
    paper = SimpleNamespace(id=78, title="Demo Paper", pdf_path="demo.pdf")
    captured = {"build_plan_calls": 0, "build_experience_calls": 0}
    generative_cache: dict[str, dict[str, object]] = {}
    experience_cache: dict[str, dict[str, object]] = {}

    async def _fake_get_owned(_db, _current_user, _paper_id):
        return paper

    class _FakeComposeService:
        async def get_latest_cached_payload_only(self, **_kwargs):
            return {
                "status": "done",
                "build_mode": "compose_agent_simplified",
                "source_signature": "compose-sig",
                "cache_hit": True,
                "cache_layer": "redis",
                "components": [{"id": "paragraph_15"}],
                "enrichment_bundle": {"targets": []},
            }

        async def build_or_get_composed_payload(self, **_kwargs):
            payload = await self.get_latest_cached_payload_only()
            meta = SimpleNamespace(
                cache_hit=True,
                cache_layer="redis",
                build_mode="compose_agent_simplified",
                source_signature="compose-sig",
                source_sig_hash="sig-hash",
            )
            return payload, meta

    class _FakeRuntime:
        def build_seed_plan(self, **kwargs):
            return {
                "version": "v1",
                "status": "draft",
                "shell_mode": "resource_augmented_reader",
                "story_substrate": {"page_id": f"p{kwargs['page']}"},
                "page_brief": {"page_goal": "Seed plan", "reading_path": ["hero_summary"]},
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "meta": {"seed_plan": True},
            }

        async def build_plan(self, **kwargs):
            captured["build_plan_calls"] += 1
            return {
                "version": "v1",
                "status": "done",
                "shell_mode": "resource_augmented_reader",
                "story_substrate": {"page_id": f"p{kwargs['page']}"},
                "page_brief": {"page_goal": "Full plan", "reading_path": ["hero_summary", "focus_evidence"]},
                "resource_modules": [],
                "interaction_modules": [],
                "js_widgets": [],
                "used_tools": ["paper_read"],
                "meta": {"full_plan": True},
            }

        def build_experience_plan(self, **kwargs):
            captured["build_experience_calls"] += 1
            generative_plan = kwargs["generative_plan"]
            plan_status = str(generative_plan.get("status") or "").strip()
            return {
                "version": "v1",
                "status": "done",
                "scope": "page_focus",
                "focus_page": kwargs["focus_page"],
                "reader_profile": kwargs["reader_profile"],
                "page_story_title": "Seed experience" if plan_status == "draft" else "Full experience",
                "page_story_subtitle": "Derived from plan",
                "hero": {"title": "Focus"},
                "main_sections": [],
                "supporting_resources": [],
                "interactive_blocks": [],
                "widget_blocks": [],
                "reading_path": list(generative_plan.get("page_brief", {}).get("reading_path") or []),
                "meta": {"derived_from": "generative_reader_plan"},
            }

    async def _fake_build_registry(**_kwargs):
        return object(), {"paper_read"}

    async def _fake_gen_cache_get(cache_key):
        return generative_cache.get(cache_key), ("redis" if cache_key in generative_cache else "none")

    async def _fake_exp_cache_get(cache_key):
        return experience_cache.get(cache_key), ("redis" if cache_key in experience_cache else "none")

    async def _fake_gen_cache_set(cache_key, payload, ttl_seconds=3600):
        generative_cache[cache_key] = payload

    async def _fake_exp_cache_set(cache_key, payload, ttl_seconds=3600):
        experience_cache[cache_key] = payload

    async def _fake_adjacent_context(**_kwargs):
        return []

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())
    monkeypatch.setattr(literature_api, "get_generative_reader_agent_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(literature_api, "_build_generative_reader_agent_tool_registry_for_paper", _fake_build_registry)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_get", _fake_gen_cache_get)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_get", _fake_exp_cache_get)
    monkeypatch.setattr(literature_api, "_generative_plan_cache_set", _fake_gen_cache_set)
    monkeypatch.setattr(literature_api, "_experience_plan_cache_set", _fake_exp_cache_set)
    monkeypatch.setattr(literature_api, "_build_experience_adjacent_page_context", _fake_adjacent_context)

    payload = literature_api.ReaderExperiencePlanRequest(
        page=7,
        focus_page=7,
        selected_kb_id=84,
        user_intent="Create a paper experience",
        reader_profile="curious_generalist",
    )

    seed_response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert seed_response.plan.status == "done"
    assert seed_response.plan.meta["seed_plan"] is True
    assert seed_response.generative_plan.status == "draft"
    assert seed_response.experience_cache_layer == "derived_seed"

    full_response = await literature_api.get_reader_experience_plan(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert full_response.generative_plan.status == "done"
    assert full_response.plan.page_story_title == "Full experience"
    assert full_response.generative_plan_cache_hit is False
    assert full_response.experience_cache_hit is False
    assert captured["build_plan_calls"] == 1

    cached_response = await literature_api.get_reader_experience_plan_cached(
        paper_id=78,
        payload=payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=5),
    )

    assert cached_response.generative_plan_cache_hit is True
    assert cached_response.experience_cache_hit is True
    assert cached_response.generative_plan.status == "done"
    assert cached_response.plan.page_story_title == "Full experience"
    assert captured["build_plan_calls"] == 1


@pytest.mark.asyncio
async def test_list_literature_ask_sessions_returns_user_scoped_rows():
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            id=1,
            user_id=7,
            scope="paper",
            paper_id=100,
            collection_id=None,
            knowledge_base_id=3,
            title="会话1",
            created_at=now,
            updated_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_sessions(
        scope=None,
        paper_id=None,
        collection_id=None,
        knowledge_base_id=None,
        limit=30,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].id == 1
    assert result[0].scope == "paper"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_filters_invalid_sources():
    now = datetime.now(timezone.utc)
    session = SimpleNamespace(id=8, user_id=7)
    rows = [
        SimpleNamespace(
            id=11,
            session_id=8,
            role="assistant",
            content="answer",
            sources=[
                "bad-source",
                {
                    "document_id": 1,
                    "document_name": "paper.pdf",
                    "snippet": "snippet",
                    "score": 0.91,
                },
            ],
            created_at=now,
        )
    ]
    db = _FakeDB([_FakeResult(row=session), _FakeResult(rows=rows)])

    result = await literature_api.list_literature_ask_messages(
        session_id=8,
        limit=200,
        offset=0,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].sources[0].document_name == "paper.pdf"


@pytest.mark.asyncio
async def test_list_literature_ask_messages_raises_404_for_unknown_session():
    db = _FakeDB([_FakeResult(row=None)])

    with pytest.raises(HTTPException) as exc:
        await literature_api.list_literature_ask_messages(
            session_id=999,
            limit=200,
            offset=0,
            db=db,
            current_user=SimpleNamespace(id=7),
        )

    assert exc.value.status_code == 404


def test_derive_link_status_from_document_completed():
    doc = SimpleNamespace(id=123, status=DocumentStatus.COMPLETED.value, error_message=None)
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.COMPLETED.value
    assert error_message is None
    assert doc_id == 123


def test_derive_link_status_from_document_processing_clears_error():
    doc = SimpleNamespace(id=9, status=DocumentStatus.RUNNING.value, error_message="old error")
    status, error_message, doc_id = literature_api._derive_link_status_from_document(doc)
    assert status == KnowledgeLinkStatus.RUNNING.value
    assert error_message is None
    assert doc_id == 9


def test_mark_stale_document_timeout_marks_processing_doc_as_timeout(monkeypatch):
    monkeypatch.setattr(literature_api.settings, "document_processing_stale_timeout_seconds", 60)
    doc = SimpleNamespace(
        id=130,
        status=DocumentStatus.RUNNING.value,
        error_message=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    changed = literature_api._mark_stale_document_timeout(doc)

    assert changed is True
    assert doc.status == DocumentStatus.TIMEOUT.value
    assert "文档处理超时" in doc.error_message


def test_normalize_collection_name_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有论文"):
        assert literature_api._normalize_collection_name(token) == "所有论文"
    for token in literature_api._build_mojibake_variants("待读"):
        assert literature_api._normalize_collection_name(token) == "待读"
    for token in literature_api._build_mojibake_variants("已读"):
        assert literature_api._normalize_collection_name(token) == "已读"
    for token in literature_api._build_mojibake_variants("收藏"):
        assert literature_api._normalize_collection_name(token) == "收藏"
    assert literature_api._normalize_collection_name("我的收藏") == "我的收藏"


def test_normalize_collection_description_repairs_known_mojibake_tokens():
    for token in literature_api._build_mojibake_variants("所有保存的论文"):
        assert literature_api._normalize_collection_description(token) == "所有保存的论文"
    for token in literature_api._build_mojibake_variants("待阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "待阅读的论文"
    for token in literature_api._build_mojibake_variants("已阅读的论文"):
        assert literature_api._normalize_collection_description(token) == "已阅读的论文"
    for token in literature_api._build_mojibake_variants("重要论文"):
        assert literature_api._normalize_collection_description(token) == "重要论文"
    assert literature_api._normalize_collection_description("用户自定义描述") == "用户自定义描述"
