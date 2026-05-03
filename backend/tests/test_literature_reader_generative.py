import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.config import settings
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


@pytest.fixture(autouse=True)
def _set_default_parser_mode(monkeypatch):
    monkeypatch.setattr(settings, "pdf_layout_parser", "auto", raising=False)


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


@pytest.mark.asyncio
async def test_parse_page_structure_splits_paragraphs_by_visual_gap(monkeypatch):
    service = LiteratureReaderService()
    text = (
        "Introduction\n"
        "First paragraph sentence one.\n"
        "First paragraph sentence two.\n"
        "Second paragraph starts here.\n"
        "Second paragraph continues.\n"
    )
    monkeypatch.setattr(service, "_read_pdf_page_text", staticmethod(lambda _path, _page: text))
    monkeypatch.setattr(
        service,
        "_extract_page_style_cues",
        staticmethod(
            lambda _path, _page: {
                "page": 1,
                "line_count": 5,
                "image_count": 0,
                "median_font_size": 10.5,
                "heading_hints": [{"text": "Introduction", "score": 0.92, "column_label": "main"}],
                "noise_hints": [],
                "line_layout": [
                    {"text": "Introduction", "column_label": "main", "top": 80, "bottom": 96, "height": 16},
                    {"text": "First paragraph sentence one.", "column_label": "main", "top": 110, "bottom": 126, "height": 16},
                    {"text": "First paragraph sentence two.", "column_label": "main", "top": 128, "bottom": 144, "height": 16},
                    # Large vertical gap indicates next paragraph starts here.
                    {"text": "Second paragraph starts here.", "column_label": "main", "top": 172, "bottom": 188, "height": 16},
                    {"text": "Second paragraph continues.", "column_label": "main", "top": 190, "bottom": 206, "height": 16},
                ],
            }
        ),
    )

    result = await service.parse_page_structure(pdf_path="dummy.pdf", page=1)
    paragraph_blocks = [item for item in result["blocks"] if item.get("kind") == "paragraph"]

    assert len(paragraph_blocks) >= 2
    assert "First paragraph sentence one." in str(paragraph_blocks[0].get("text") or "")
    assert any("Second paragraph starts here." in str(item.get("text") or "") for item in paragraph_blocks[1:])


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


def test_split_embedded_heading_lines_should_not_split_sentence_continuation():
    service = LiteratureReaderService()
    rows = [
        "methods. Anecdotal usage indicates that ChatGPT exhibits evidence of deductive reasoning and long-term dependency skills."
    ]
    output = service._split_embedded_heading_lines(rows)
    assert len(output) == 1
    assert output[0].lower().startswith("methods. anecdotal usage")


def test_split_words_by_spacing_should_break_large_column_gap():
    words = [
        {"text": "Left", "x0": 80, "x1": 110, "top": 120, "bottom": 135},
        {"text": "column", "x0": 116, "x1": 164, "top": 120, "bottom": 135},
        {"text": "text", "x0": 170, "x1": 196, "top": 120, "bottom": 135},
        {"text": "Right", "x0": 430, "x1": 470, "top": 120, "bottom": 135},
        {"text": "column", "x0": 476, "x1": 524, "top": 120, "bottom": 135},
    ]
    segments = LiteratureReaderService._split_words_by_spacing(words, page_width=600)
    assert len(segments) == 2
    left_text = " ".join(str(item.get("text") or "") for item in segments[0])
    right_text = " ".join(str(item.get("text") or "") for item in segments[1])
    assert "Left column text" in left_text
    assert "Right column" in right_text


@pytest.mark.asyncio
async def test_parse_page_structure_should_use_document_mind_when_available(monkeypatch):
    service = LiteratureReaderService()

    class _FakeDocMind:
        async def parse_page_text(self, **_kwargs):
            return (
                "Introduction\nThis paragraph is returned by Document Mind.",
                {"used": True, "reason": "applied"},
            )

    service._document_mind_parser = _FakeDocMind()
    monkeypatch.setattr(
        service,
        "_read_pdf_page_text",
        staticmethod(lambda _path, _page: "Fallback parser text should not be used."),
    )

    result = await service.parse_page_structure(
        pdf_path="dummy.pdf",
        page=1,
        source_url="https://example.com/demo.pdf",
        paper_id=78,
    )
    assert "Document Mind" in str(result.get("raw_text") or "")
    parser_meta = dict(result.get("parser_chain_meta") or {})
    assert bool((parser_meta.get("document_mind") or {}).get("used")) is True


@pytest.mark.asyncio
async def test_parse_page_structure_should_map_docmind_structure_to_page_structure_v3(monkeypatch):
    service = LiteratureReaderService()

    class _FakeDocMind:
        async def parse_page_structure(self, **_kwargs):
            return (
                {
                    "layouts": [
                        {
                            "index": 1,
                            "uniqueId": "l1",
                            "type": "title",
                            "subType": "doc_title",
                            "alignment": "left",
                            "pos": [{"x": 80, "y": 100}, {"x": 760, "y": 100}, {"x": 760, "y": 160}, {"x": 80, "y": 160}],
                            "blocks": [{"text": "Document Title", "pos": [{"x": 80, "y": 100}, {"x": 760, "y": 100}, {"x": 760, "y": 160}, {"x": 80, "y": 160}]}],
                        },
                        {
                            "index": 2,
                            "uniqueId": "l2",
                            "type": "text",
                            "subType": "para",
                            "alignment": "left",
                            "pos": [{"x": 90, "y": 180}, {"x": 760, "y": 180}, {"x": 760, "y": 260}, {"x": 90, "y": 260}],
                            "blocks": [{"text": "Paragraph body from docmind.", "pos": [{"x": 90, "y": 180}, {"x": 760, "y": 180}, {"x": 760, "y": 260}, {"x": 90, "y": 260}]}],
                        },
                    ],
                    "styles": [],
                    "doc_tree": [],
                    "doc_info": {},
                },
                {"used": True, "reason": "applied"},
            )

    service._document_mind_parser = _FakeDocMind()
    monkeypatch.setattr(service, "_extract_page_style_cues", staticmethod(lambda _path, _page: {"page_width": 840.0, "page_height": 1188.0}))

    result = await service.parse_page_structure(
        pdf_path="dummy.pdf",
        page=1,
        source_url="https://example.com/demo.pdf",
        paper_id=78,
    )

    page_structure_v3 = dict(result.get("page_structure_v3") or {})
    assert str(page_structure_v3.get("source") or "") == "document_mind"
    block_groups = list(page_structure_v3.get("block_groups") or [])
    assert len(block_groups) >= 2
    assert any(str(row.get("kind") or "") == "heading" for row in block_groups)
    assert any(str(row.get("kind") or "") == "paragraph" for row in block_groups)


@pytest.mark.asyncio
async def test_parse_page_structure_docmind_mode_should_not_fallback_to_local_parser(monkeypatch):
    service = LiteratureReaderService()

    class _FakeDocMind:
        async def parse_page_structure(self, **_kwargs):
            return (None, {"used": False, "reason": "docmind_failed"})

        async def parse_page_text(self, **_kwargs):
            return (None, {"used": False, "reason": "docmind_failed"})

    service._document_mind_parser = _FakeDocMind()
    monkeypatch.setattr(settings, "pdf_layout_parser", "document_mind", raising=False)

    def _raise_local_parser(*_args, **_kwargs):
        raise AssertionError("local parser should not be called in document_mind mode")

    monkeypatch.setattr(service, "_read_pdf_page_text", staticmethod(_raise_local_parser))

    result = await service.parse_page_structure(
        pdf_path="dummy.pdf",
        page=1,
        source_url="https://example.com/demo.pdf",
        paper_id=78,
    )
    assert list(result.get("blocks") or []) == []
    assert str((result.get("page_structure_v3") or {}).get("source") or "") == "document_mind"
    parser_meta = dict(result.get("parser_chain_meta") or {})
    assert str((parser_meta.get("document_mind") or {}).get("reason") or "") == "docmind_failed"


@pytest.mark.asyncio
async def test_parse_page_structure_docmind_mode_should_build_text_only_payload_when_only_text_exists(monkeypatch):
    service = LiteratureReaderService()

    class _FakeDocMind:
        async def parse_page_structure(self, **_kwargs):
            return ({}, {"used": False, "reason": "no_layouts"})

        async def parse_page_text(self, **_kwargs):
            return ("Paragraph A.\n\nParagraph B.", {"used": True, "reason": "applied"})

    service._document_mind_parser = _FakeDocMind()
    monkeypatch.setattr(settings, "pdf_layout_parser", "document_mind", raising=False)
    monkeypatch.setattr(
        service,
        "_read_pdf_page_text",
        staticmethod(lambda *_args, **_kwargs: "LOCAL_PARSER_TEXT_SHOULD_NOT_BE_USED"),
    )

    result = await service.parse_page_structure(
        pdf_path="dummy.pdf",
        page=1,
        source_url="https://example.com/demo.pdf",
        paper_id=78,
    )
    blocks = list(result.get("blocks") or [])
    assert len(blocks) >= 2
    assert str((result.get("page_structure_v3") or {}).get("source") or "") == "document_mind"
    assert "LOCAL_PARSER_TEXT_SHOULD_NOT_BE_USED" not in str(result.get("raw_text") or "")


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
async def test_redis_hit_with_stale_docmind_cache_should_rebuild(monkeypatch):
    service = LiteratureReaderService()
    db = _FakeDB()
    paper = SimpleNamespace(id=12, user_id=9, title="P", pdf_path="x", url=None, pdf_url=None, arxiv_url=None, arxiv_id=None, doi=None)
    stale_cached = {
        "paper_id": 12,
        "page": 2,
        "build_mode": "cache",
        "blocks": [],
        "sections": [],
        "assets": [],
        "summary": "",
        "style_key": "journal_classic",
        "structure_confidence": 0.2,
        "docmind_structure": {"layouts": []},
        "parser_chain_meta": {"document_mind": {"used": False, "reason": "client_unavailable"}},
        "page_structure_v3": {"source": "document_mind", "block_groups": []},
    }
    rebuilt_payload = {
        "raw_text": "DocMind text",
        "style_key": "journal_classic",
        "structure_confidence": 0.92,
        "summary": "",
        "sections": [],
        "blocks": [
            {
                "id": "b1",
                "kind": "paragraph",
                "text": "DocMind text",
                "order": 0,
                "section_title": "Body",
                "source_anchor": {"page": 2, "start_char": 0, "end_char": 11},
            }
        ],
        "docmind_structure": {
            "layouts": [
                {
                    "index": 1,
                    "uniqueId": "l1",
                    "type": "text",
                    "subType": "para",
                    "blocks": [{"text": "DocMind text"}],
                    "pageNum": [2],
                }
            ]
        },
        "parser_chain_meta": {"document_mind": {"used": True, "reason": "applied"}},
        "page_structure_v3": {"source": "document_mind", "block_groups": [{"layout_unique_id": "l1", "block_id": "p2_b1"}]},
    }
    parse_calls = {"count": 0}

    async def _parse(**_kwargs):
        parse_calls["count"] += 1
        return dict(rebuilt_payload)

    monkeypatch.setattr(settings, "pdf_layout_parser", "document_mind", raising=False)
    monkeypatch.setattr(settings, "reader_document_mind_enabled", True, raising=False)
    monkeypatch.setattr(service, "_build_source_signature", _async_return("sig-cache-stale"))
    monkeypatch.setattr(service, "_read_payload_from_redis", _async_return(stale_cached))
    monkeypatch.setattr(service, "_read_payload_from_db", _async_return(None))
    monkeypatch.setattr(service, "_acquire_lock", _async_return("token-cache-stale"))
    monkeypatch.setattr(service, "_release_lock", _async_return(None))
    monkeypatch.setattr(service, "_resolve_local_pdf_path", lambda **_: "dummy.pdf")
    monkeypatch.setattr(service, "parse_page_structure", _parse)
    monkeypatch.setattr(service, "collect_page_assets", _async_return([]))
    monkeypatch.setattr(service, "_upsert_payload_to_db", _async_return(None))
    monkeypatch.setattr(service, "_write_payload_to_redis", _async_return(None))

    payload, meta = await service.build_or_get_page_payload(
        db=db,
        user_id=9,
        paper=paper,
        page=2,
    )

    assert parse_calls["count"] == 1
    assert meta.cache_hit is False
    assert str(((payload.get("parser_chain_meta") or {}).get("document_mind") or {}).get("reason") or "") == "applied"
    assert len(list((payload.get("docmind_structure") or {}).get("layouts") or [])) > 0


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

    class _FakeSessionFactory:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(literature_api, "async_session_factory", lambda: _FakeSessionFactory())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    response = await literature_api.stream_reader_generative_page(
        paper_id=5,
        payload=SimpleNamespace(page=1, selected_kb_id=None, force_refresh=False, style_hint=None),
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


def test_split_embedded_heading_lines_should_not_split_mid_sentence_method():
    line = (
        "ChatGPT is powered by GPT3.5 and a large corpus of text data from the Internet "
        "via reinforcement and supervised learning method Anecdotal usage indicates "
        "that ChatGPT exhibits evidence of deductive reasoning."
    )
    rows = LiteratureReaderService._split_embedded_heading_lines([line])
    assert rows == [line]


def test_split_embedded_heading_lines_should_split_heading_near_line_start():
    line = "Methods In this study, we evaluate the performance of ChatGPT."
    rows = LiteratureReaderService._split_embedded_heading_lines([line])
    assert rows == ["Methods", "In this study, we evaluate the performance of ChatGPT."]


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


@pytest.mark.asyncio
async def test_reader_signature_should_scope_selected_kb_to_current_user(monkeypatch):
    service = LiteratureReaderService()

    class _ScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _ScopedDb:
        def __init__(self):
            self.calls = 0

        async def execute(self, stmt):
            self.calls += 1
            sql = str(stmt)
            if "knowledge_bases" in sql:
                # 非当前用户知识库应直接忽略，不允许继续探测 documents 更新时间。
                return _ScalarResult(None)
            if "documents" in sql:
                raise AssertionError("unowned selected_kb_id should not query documents")
            return _ScalarResult(None)

    db = _ScopedDb()
    monkeypatch.setattr(service, "_resolve_local_pdf_path", lambda **_: None)
    signature = await service._build_source_signature(
        db=db,
        user_id=7,
        paper=SimpleNamespace(id=10, user_id=7, title="Demo", pdf_path=None),
        selected_kb_id=999,
        style_hint="journal_classic",
    )

    assert "kb:none" in signature
    assert db.calls == 1


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _async_raise(exc: Exception):
    async def _inner(*_args, **_kwargs):
        raise exc

    return _inner
