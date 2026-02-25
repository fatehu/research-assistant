import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.services.literature_reader_service import LiteratureReaderService, ReaderBuildMeta


class _FakeScalarRows:
    def __init__(self, rows):
        self._rows = list(rows or [])

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return _FakeScalarRows(self._rows)


class _FakeDB:
    async def execute(self, _stmt):
        return _FakeExecuteResult(rows=[])

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_parse_page_structure_detects_introduction_heading(monkeypatch):
    service = LiteratureReaderService()
    text = (
        "PLOS DIGITAL HEALTH\n\n"
        "Introduction\n"
        "Over the past decade, advances in neural networks...\n"
        "This paragraph should stay in正文。\n"
    )
    monkeypatch.setattr(service, "_read_pdf_page_text", staticmethod(lambda _path, _page: text))

    result = await service.parse_page_structure(pdf_path="dummy.pdf", page=2)
    headings = [b for b in result["blocks"] if b.get("kind") == "heading"]

    assert headings
    assert any("Introduction" in h.get("text", "") for h in headings)


@pytest.mark.asyncio
async def test_parse_page_structure_filters_noise_line(monkeypatch):
    service = LiteratureReaderService()
    text = (
        "PLOS DIGITAL HEALTH\n"
        "a1111111111\n"
        "Introduction\n"
        "Valid paragraph text.\n"
    )
    monkeypatch.setattr(service, "_read_pdf_page_text", staticmethod(lambda _path, _page: text))
    monkeypatch.setattr(
        service,
        "_extract_page_style_cues",
        staticmethod(
            lambda _path, _page: {
                "page": 1,
                "line_count": 4,
                "image_count": 1,
                "median_font_size": 10.8,
                "heading_hints": [{"text": "Introduction", "score": 0.92}],
                "noise_hints": [{"text": "a1111111111", "reason": "image_overlap"}],
            }
        ),
    )

    result = await service.parse_page_structure(pdf_path="dummy.pdf", page=1)
    merged_text = " ".join(str(item.get("text") or "") for item in result["blocks"])
    assert "a1111111111" not in merged_text
    assert any(item.get("kind") == "heading" and "Introduction" in str(item.get("text") or "") for item in result["blocks"])


@pytest.mark.asyncio
async def test_parse_page_structure_filters_sidebar_lines(monkeypatch):
    service = LiteratureReaderService()
    text = (
        "OPEN ACCESS\n"
        "Citation: demo citation block\n"
        "Introduction\n"
        "Main paragraph content remains here.\n"
    )
    monkeypatch.setattr(service, "_read_pdf_page_text", staticmethod(lambda _path, _page: text))
    monkeypatch.setattr(
        service,
        "_extract_page_style_cues",
        staticmethod(
            lambda _path, _page: {
                "page": 1,
                "line_count": 4,
                "image_count": 0,
                "median_font_size": 10.5,
                "heading_hints": [{"text": "Introduction", "score": 0.91, "column_label": "main"}],
                "noise_hints": [],
                "line_layout": [
                    {"text": "OPEN ACCESS", "text_key": "openaccess", "column_label": "sidebar_left"},
                    {"text": "Citation: demo citation block", "text_key": "citationdemocitationblock", "column_label": "sidebar_left"},
                    {"text": "Introduction", "text_key": "introduction", "column_label": "main"},
                    {"text": "Main paragraph content remains here.", "text_key": "mainparagraphcontentremainshere", "column_label": "main"},
                ],
            }
        ),
    )

    result = await service.parse_page_structure(pdf_path="dummy.pdf", page=1)
    merged_text = " ".join(str(item.get("text") or "") for item in result["blocks"])
    assert "OPEN ACCESS" not in merged_text
    assert "Citation: demo citation block" not in merged_text
    assert "Main paragraph content remains here." in merged_text


def test_build_agent_style_context_contains_line_positions():
    service = LiteratureReaderService()
    context = service._build_agent_style_context(
        {
            "page": 1,
            "page_width": 842.0,
            "page_height": 595.0,
            "line_count": 2,
            "image_count": 1,
            "median_font_size": 10.8,
            "layout_mode": "single_with_sidebar",
            "main_column": {"x0": 248.0, "x1": 760.0, "width": 512.0},
            "heading_hints": [{"text": "Introduction", "avg_size": 16.2, "bold_ratio": 0.9, "score": 0.96}],
            "noise_hints": [],
            "line_layout": [
                {
                    "text": "OPEN ACCESS",
                    "text_key": "openaccess",
                    "column_label": "sidebar_left",
                    "x0": 50,
                    "x1": 160,
                    "top": 410,
                    "bottom": 428,
                    "width": 110,
                    "avg_size": 11.0,
                    "bold_ratio": 0.7,
                    "image_overlap_ratio": 0.0,
                },
                {
                    "text": "Introduction",
                    "text_key": "introduction",
                    "column_label": "main",
                    "x0": 248,
                    "x1": 360,
                    "top": 120,
                    "bottom": 138,
                    "width": 112,
                    "avg_size": 16.2,
                    "bold_ratio": 0.95,
                    "image_overlap_ratio": 0.0,
                },
            ],
        }
    )
    assert context["layout_mode"] == "single_with_sidebar"
    assert isinstance(context["line_layout"], list) and len(context["line_layout"]) == 2
    assert context["line_layout"][0]["column_label"] == "sidebar_left"
    assert context["line_layout"][1]["x0"] == 248.0


def test_validate_agent_payload_normalizes_style_tuning():
    service = LiteratureReaderService()
    payload = {
        "style_key": "journal_classic",
        "style_tuning": {"body_scale": 1.8, "line_height": 1.2, "heading_scale": 0.5},
        "sections": [{"title": "Introduction", "level": 1}],
        "blocks": [
            {
                "kind": "heading",
                "text": "Introduction",
                "section_title": "Introduction",
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
            }
        ],
    }
    result = service._validate_agent_repair_payload(
        payload=payload,
        page=1,
        raw_text="Introduction paragraph",
        fallback_style="journal_classic",
    )

    assert isinstance(result, dict)
    assert result["style_tuning"]["body_scale"] == 1.25
    assert result["style_tuning"]["line_height"] == 1.55
    assert result["style_tuning"]["heading_scale"] == 0.95


@pytest.mark.asyncio
async def test_low_confidence_triggers_agent_repair(monkeypatch):
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(
        id=7,
        user_id=9,
        title="Demo Paper",
        pdf_path="dummy.pdf",
        url=None,
        pdf_url=None,
        arxiv_url=None,
        arxiv_id=None,
        doi=None,
    )

    parsed = {
        "raw_text": "Intro text",
        "style_key": "journal_classic",
        "structure_confidence": 0.31,
        "summary": "old",
        "sections": [{"title": "正文", "level": 1, "block_ids": ["b1"], "source_anchor": None}],
        "blocks": [
            {
                "id": "b1",
                "kind": "paragraph",
                "text": "Intro text",
                "order": 0,
                "section_title": "正文",
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 10},
            }
        ],
    }
    repaired = {
        "raw_text": "Intro text",
        "style_key": "clinical_brief",
        "structure_confidence": 0.9,
        "summary": "new",
        "sections": [{"title": "Introduction", "level": 1, "block_ids": ["b1"], "source_anchor": None}],
        "blocks": [
            {
                "id": "b1",
                "kind": "heading",
                "text": "Introduction",
                "order": 0,
                "section_title": "Introduction",
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
            }
        ],
    }

    monkeypatch.setattr(service, "_build_source_signature", _async_return("sig-a"))
    monkeypatch.setattr(service, "_read_payload_from_redis", _async_return(None))
    monkeypatch.setattr(service, "_read_payload_from_db", _async_return(None))
    monkeypatch.setattr(service, "_acquire_lock", _async_return("token-a"))
    monkeypatch.setattr(service, "_release_lock", _async_return(None))
    monkeypatch.setattr(service, "_resolve_local_pdf_path", lambda **_: "dummy.pdf")
    monkeypatch.setattr(service, "parse_page_structure", _async_return(parsed))
    monkeypatch.setattr(service, "repair_structure_with_agent", _async_return(repaired))
    monkeypatch.setattr(service, "collect_page_assets", _async_return([]))
    monkeypatch.setattr(service, "_upsert_payload_to_db", _async_return(None))
    monkeypatch.setattr(service, "_write_payload_to_redis", _async_return(None))

    payload, meta = await service.build_or_get_page_payload(
        db=db,
        user_id=9,
        paper=paper,
        page=1,
    )

    assert meta.build_mode == "agent_repair"
    assert payload["style_key"] == "clinical_brief"
    assert payload["blocks"][0]["kind"] == "heading"


@pytest.mark.asyncio
async def test_invalid_agent_repair_falls_back_to_parser(monkeypatch):
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(
        id=8,
        user_id=9,
        title="Demo Paper",
        pdf_path="dummy.pdf",
        url=None,
        pdf_url=None,
        arxiv_url=None,
        arxiv_id=None,
        doi=None,
    )
    parsed = {
        "raw_text": "正文内容",
        "style_key": "journal_classic",
        "structure_confidence": 0.2,
        "summary": "x",
        "sections": [{"title": "正文", "level": 1, "block_ids": ["b1"], "source_anchor": None}],
        "blocks": [
            {
                "id": "b1",
                "kind": "paragraph",
                "text": "正文内容",
                "order": 0,
                "section_title": "正文",
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 4},
            }
        ],
    }

    monkeypatch.setattr(service, "_build_source_signature", _async_return("sig-a"))
    monkeypatch.setattr(service, "_read_payload_from_redis", _async_return(None))
    monkeypatch.setattr(service, "_read_payload_from_db", _async_return(None))
    monkeypatch.setattr(service, "_acquire_lock", _async_return("token-a"))
    monkeypatch.setattr(service, "_release_lock", _async_return(None))
    monkeypatch.setattr(service, "_resolve_local_pdf_path", lambda **_: "dummy.pdf")
    monkeypatch.setattr(service, "parse_page_structure", _async_return(parsed))
    monkeypatch.setattr(service, "repair_structure_with_agent", _async_return(parsed))
    monkeypatch.setattr(service, "collect_page_assets", _async_return([]))
    monkeypatch.setattr(service, "_upsert_payload_to_db", _async_return(None))
    monkeypatch.setattr(service, "_write_payload_to_redis", _async_return(None))

    payload, meta = await service.build_or_get_page_payload(
        db=db,
        user_id=9,
        paper=paper,
        page=1,
    )

    assert meta.build_mode == "parser_fallback"
    assert payload["blocks"][0]["kind"] == "paragraph"


@pytest.mark.asyncio
async def test_redis_hit_skips_rebuild(monkeypatch):
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(id=11, user_id=9, title="P", pdf_path="x", url=None, pdf_url=None, arxiv_url=None, arxiv_id=None, doi=None)
    cached = {
        "paper_id": 11,
        "page": 2,
        "build_mode": "cache",
        "blocks": [],
        "sections": [],
        "assets": [],
        "summary": "",
        "style_key": "journal_classic",
        "structure_confidence": 0.9,
    }

    monkeypatch.setattr(service, "_build_source_signature", _async_return("sig-cache"))
    monkeypatch.setattr(service, "_read_payload_from_redis", _async_return(cached))
    monkeypatch.setattr(service, "parse_page_structure", _async_raise(RuntimeError("should not rebuild")))

    payload, meta = await service.build_or_get_page_payload(
        db=db,
        user_id=9,
        paper=paper,
        page=2,
    )

    assert meta.cache_hit is True
    assert meta.cache_layer == "redis"
    assert payload["cache_hit"] is True


@pytest.mark.asyncio
async def test_source_signature_change_triggers_rebuild(monkeypatch):
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(
        id=21,
        user_id=9,
        title="Sig Test",
        pdf_path="dummy.pdf",
        url=None,
        pdf_url=None,
        arxiv_url=None,
        arxiv_id=None,
        doi=None,
    )
    calls = {"parse": 0}
    signatures = iter(["sig-a", "sig-b"])

    async def _next_signature(**_kwargs):
        return next(signatures)

    async def _parse(**_kwargs):
        calls["parse"] += 1
        return {
            "raw_text": "abc",
            "style_key": "journal_classic",
            "structure_confidence": 0.8,
            "summary": "",
            "sections": [],
            "blocks": [],
        }

    monkeypatch.setattr(service, "_build_source_signature", _next_signature)
    monkeypatch.setattr(service, "_read_payload_from_redis", _async_return(None))
    monkeypatch.setattr(service, "_read_payload_from_db", _async_return(None))
    monkeypatch.setattr(service, "_acquire_lock", _async_return("token"))
    monkeypatch.setattr(service, "_release_lock", _async_return(None))
    monkeypatch.setattr(service, "_resolve_local_pdf_path", lambda **_: "dummy.pdf")
    monkeypatch.setattr(service, "parse_page_structure", _parse)
    monkeypatch.setattr(service, "collect_page_assets", _async_return([]))
    monkeypatch.setattr(service, "_upsert_payload_to_db", _async_return(None))
    monkeypatch.setattr(service, "_write_payload_to_redis", _async_return(None))

    first_payload, _ = await service.build_or_get_page_payload(db=db, user_id=9, paper=paper, page=1)
    second_payload, _ = await service.build_or_get_page_payload(db=db, user_id=9, paper=paper, page=1)

    assert calls["parse"] == 2
    assert first_payload["source_signature"] != second_payload["source_signature"]


@pytest.mark.asyncio
async def test_stream_event_order(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    fake_payload = {
        "paper_id": 5,
        "page": 1,
        "style_key": "journal_classic",
        "summary": "x",
        "sections": [{"title": "Introduction", "level": 1, "block_ids": ["b1"], "source_anchor": None}],
        "blocks": [
            {
                "id": "b1",
                "kind": "heading",
                "text": "Introduction",
                "order": 0,
                "section_title": "Introduction",
                "source_anchor": {"page": 1, "start_char": 0, "end_char": 12},
            }
        ],
        "assets": [{"kind": "image_hint", "label": "Figure 1", "source": "pdf", "href": None, "meta": {}}],
        "build_mode": "parser",
        "structure_confidence": 0.9,
        "generated_at": now,
    }

    class _FakeReaderService:
        async def build_or_get_page_payload(self, **_kwargs):
            return fake_payload, ReaderBuildMeta(
                cache_hit=False,
                cache_layer="none",
                build_mode="parser",
                source_signature="sig-a",
                source_sig_hash="hash-a",
            )

    async def _fake_get_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=5, user_id=7, title="Demo")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_get_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_service", lambda: _FakeReaderService())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    response = await literature_api.stream_reader_generative_page(
        paper_id=5,
        payload=SimpleNamespace(page=1, selected_kb_id=None, force_refresh=False, style_hint=None),
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
    assert events[1] == "skeleton"
    assert "chunk" in events
    assert events[-2] == "assets"
    assert events[-1] == "done"


def test_queue_prefetch_dedupe_and_boundaries():
    service = LiteratureReaderService()
    queued, skipped = service.queue_prefetch(pages=[0, 1, 1, 2, 5], max_page=3)
    assert queued == [1, 2]
    assert 0 in skipped
    assert 1 in skipped
    assert 5 in skipped


@pytest.mark.asyncio
async def test_image_hints_never_return_external_image_url():
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(
        id=31,
        url="https://example.com/paper",
        pdf_url=None,
        arxiv_url=None,
        arxiv_id=None,
        doi=None,
    )
    assets = await service.collect_page_assets(
        db=db,
        paper=paper,
        page=1,
        raw_text="Figure 1: model overview\nhttps://example.com/image.png",
        pdf_path="/tmp/a.pdf",
    )
    image_hints = [item for item in assets if item.get("kind") == "image_hint"]
    assert image_hints
    assert all(item.get("href") in (None, "") for item in image_hints)


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _async_raise(exc: Exception):
    async def _inner(*_args, **_kwargs):
        raise exc

    return _inner
