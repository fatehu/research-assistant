import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
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
