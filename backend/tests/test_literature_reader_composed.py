import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.config import settings
from app.services.literature_reader_compose_service import ReaderComposeBuildMeta
from app.services.literature_reader_compose_service import LiteratureReaderComposeService


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


def test_reader_compose_sanitize_anchor_can_recover_invalid_range():
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
    anchor = sanitized["components"][0]["source_anchor_refs"][0]
    assert anchor["page"] == 1
    assert anchor["end_char"] > anchor["start_char"]


def test_reader_compose_normalize_blocks_merge_split_heading_lines():
    service = LiteratureReaderComposeService()
    rows = service._normalize_blocks_for_render(
        blocks=[
            {"kind": "heading", "text": "Performance of ChatGPT on USMLE: Potential"},
            {"kind": "heading", "text": "for AI-assisted medical education using large"},
            {"kind": "heading", "text": "language models"},
            {"kind": "heading", "text": "RESEA RCH ARTICLE"},
        ],
        page=1,
    )

    headings = [str(item.get("text") or "") for item in rows if str(item.get("kind") or "") == "heading"]
    assert any("Performance of ChatGPT on USMLE: Potential for AI-assisted medical education using large language models" in item for item in headings)
    assert any("RESEARCH ARTICLE" in item for item in headings)


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

    assert "正式标识入口" not in tldr


def test_extract_query_terms_should_expand_limitations_to_valid_chinese_term():
    terms = literature_api._extract_query_terms("limitations")
    assert "局限性" in terms


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
        db=_FakeDB(),
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
            raise AssertionError("selected_kb_id=None 时不应触发 DB 查询")

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
        return "结论：可回答。证据：来自当前段落。"

    monkeypatch.setattr(service, "build_or_get_composed_payload", _fake_build_or_get)
    monkeypatch.setattr(service, "_generate_inline_answer", _fake_answer)

    result = await service.build_inline_answer_card(
        db=SimpleNamespace(),
        user_id=1,
        paper=SimpleNamespace(id=1),
        page=1,
        node_id="n1",
        question="测试",
        scope="section",
        theme_mode="dark",
        citation_tldr=True,
    )

    assert captured["theme_mode"] == "dark"
    assert captured["citation_tldr"] is True
    assert isinstance(result.get("node"), dict)


@pytest.mark.asyncio
async def test_reader_compose_mm_gate_not_hit_should_skip_mm_call(monkeypatch):
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

    payload = await service._apply_multimodal_layout_assist(
        paper=SimpleNamespace(id=19, user_id=1, title="Demo", pdf_path="demo.pdf"),
        page=1,
        base_payload={
            "page": 1,
            "structure_confidence": 0.9,
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph"}],
            "sections": [],
        },
    )

    meta = payload.get("mm_assist_meta") or {}
    assert meta.get("used") is False
    assert meta.get("reason") == "quality_gate_not_hit"
    assert "layout_channels" in payload


@pytest.mark.asyncio
async def test_reader_compose_mm_should_use_fallback_and_merge_channels(monkeypatch):
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

    payload = await service._apply_multimodal_layout_assist(
        paper=SimpleNamespace(id=20, user_id=1, title="Demo", pdf_path="demo.pdf"),
        page=1,
        base_payload={
            "page": 1,
            "structure_confidence": 0.5,
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "Demo paragraph", "zone_type": "main_body"}],
            "sections": [],
        },
    )

    mm_meta = payload.get("mm_assist_meta") or {}
    assert mm_meta.get("used") is True
    assert mm_meta.get("fallback_used") is True
    channels = payload.get("layout_channels") or {}
    assert "main_body" in channels
    assert "side_context" in channels


@pytest.mark.asyncio
async def test_generate_takeaways_should_use_neighbor_context(monkeypatch):
    service = LiteratureReaderComposeService()

    class _StubLLM:
        async def chat(self, **_kwargs):
            return {
                "content": json.dumps(
                    {
                        "items": [
                            {"text": "模型在当前页给出了可迁移的核心实验结论。"},
                            {"text": "上一页背景用于解释当前页结论成立条件。"},
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
    assert "核心实验结论" in str(rows[0].get("text") or "")
    assert all((row.get("evidence_anchors") or []) == [] for row in rows)


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
                    "text": "该页的关键结论已经由 AI 概括完成",
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
    items = (takeaway_nodes[0].get("props") or {}).get("items") or []
    assert items
    assert "由 AI 概括完成" in str(items[0].get("text") or "")


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
                {"id": "t1", "type": "SectionTOC", "props": {"items": [], "hidden_reason": "本页目录质量不足，已隐藏。"}, "children": [], "source_anchor_refs": []},
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
    assert quality.get("mm_assist_used") is True
