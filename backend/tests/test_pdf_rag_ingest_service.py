from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.local_structured_pdf.contracts import (
    PdfBBox,
    PdfHybridExecutionResult,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfPageMeta,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)
from app.services.pdf_rag_ingest_service import PdfRagIngestService


def _block(
    *,
    block_id: str,
    block_type: str,
    text: str,
    page: int = 1,
    heading_level: int | None = None,
    section_titles: list[str] | None = None,
    table_rows: list[list[str]] | None = None,
) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(10.0, 20.0, 200.0, 80.0),
        line_ids=[f"{block_id}-l1"],
        heading_level=heading_level,
        section_titles=list(section_titles or []),
        section_path=" > ".join(section_titles or []),
        table_rows=list(table_rows or []),
    )


def _document(blocks: list[PdfSemanticBlock]) -> PdfStructuredDocument:
    return PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                blocks=list(blocks),
            )
        ],
        blocks=list(blocks),
    )


class _FakeFastPipeline:
    def __init__(self, document: PdfStructuredDocument) -> None:
        self.document = document
        self.calls: list[tuple[str, dict]] = []

    def parse_document(self, **kwargs) -> PdfStructuredDocument:
        self.calls.append(("parse_document", kwargs))
        return self.document


class _FakeHybridPipeline:
    def __init__(self, execution: PdfHybridExecutionResult) -> None:
        self.execution = execution
        self.calls: list[tuple[str, dict]] = []

    async def parse_document_with_trace(self, **kwargs) -> PdfHybridExecutionResult:
        self.calls.append(("parse_document_with_trace", kwargs))
        return self.execution


@pytest.mark.asyncio
async def test_ingest_pdf_fast_mode_returns_ingest_markdown_and_report(monkeypatch):
    document = _document(
        [
            _block(
                block_id="h1",
                block_type="heading",
                text="1 Introduction",
                heading_level=1,
                section_titles=["1 Introduction"],
            ),
            _block(
                block_id="p1",
                block_type="paragraph",
                text="This is the first paragraph with [1] style citation.",
                section_titles=["1 Introduction"],
            ),
            _block(
                block_id="t1",
                block_type="table",
                text="",
                section_titles=["1 Introduction"],
                table_rows=[["A", "B"], ["1", "2"]],
            ),
        ]
    )
    fast_pipeline = _FakeFastPipeline(document)
    service = PdfRagIngestService(fast_pipeline=fast_pipeline)
    monkeypatch.setattr("app.services.pdf_rag_ingest_service.settings.pdf_rag_structured_mode", "fast")

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    assert result["extractor"] == "local_structured_pdf_fast"
    assert result["report"]["mode"] == "fast"
    assert result["report"]["page_count"] == 1
    assert result["report"]["block_count"] == 3
    assert fast_pipeline.calls[0][0] == "parse_document"
    assert result["document_text"].startswith("# 1 Introduction")
    assert "| A | B |" in result["document_text"]
    spans = list(result["document_source_spans"] or [])
    assert [span["block_id"] for span in spans] == ["h1", "p1", "t1"]
    assert [span["block_type"] for span in spans] == ["heading", "paragraph", "table"]
    assert all(span["page_start"] == 1 and span["page_end"] == 1 for span in spans)
    assert all(span["section_path"] == "1 Introduction" for span in spans)
    assert [result["document_text"][span["start_char"]:span["end_char"]] for span in spans] == [
        "# 1 Introduction",
        "This is the first paragraph with [1] style citation.",
        "| A | B |\n| --- | --- |\n| 1 | 2 |",
    ]


@pytest.mark.asyncio
async def test_ingest_pdf_uses_ingest_markdown_renderer_for_document_text(monkeypatch):
    document = _document(
        [
            _block(
                block_id="h1",
                block_type="heading",
                text="1 Introduction",
                heading_level=1,
                section_titles=["1 Introduction"],
            ),
            _block(
                block_id="eq1",
                block_type="equation",
                text=r"E = mc^2",
                section_titles=["1 Introduction"],
            ),
            _block(
                block_id="c1",
                block_type="caption",
                text="Figure 1. Model overview.",
                section_titles=["1 Introduction"],
            ),
        ]
    )
    fast_pipeline = _FakeFastPipeline(document)
    service = PdfRagIngestService(fast_pipeline=fast_pipeline)
    monkeypatch.setattr("app.services.pdf_rag_ingest_service.settings.pdf_rag_structured_mode", "fast")

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["document_text"] == (
        "# 1 Introduction\n\n"
        "$$\nE = mc^2\n$$\n\n"
        "Figure 1. Model overview."
    )
    spans = list(result["document_source_spans"] or [])
    assert [span["block_id"] for span in spans] == ["h1", "eq1", "c1"]
    assert [span["block_type"] for span in spans] == ["heading", "equation", "caption"]
    assert all(span["section_path"] == "1 Introduction" for span in spans)
    assert [result["document_text"][span["start_char"]:span["end_char"]] for span in spans] == [
        "# 1 Introduction",
        "$$\nE = mc^2\n$$",
        "Figure 1. Model overview.",
    ]


@pytest.mark.asyncio
async def test_ingest_pdf_hybrid_mode_uses_trace_and_backend_report(monkeypatch):
    document = _document(
        [
            _block(
                block_id="p1",
                block_type="paragraph",
                text="Hybrid block content",
                section_titles=["Results"],
            )
        ]
    )
    execution = PdfHybridExecutionResult(
        mode="auto",
        document=document,
        triage=PdfHybridTriageDocument(
            mode="auto",
            pages=[
                PdfHybridTriageResult(page=1, page_type="visual", decision="backend", confidence=0.9),
                PdfHybridTriageResult(page=2, page_type="plain_text", decision="local", confidence=0.8),
            ],
        ),
        parsed_pages=[
            PdfHybridParsedPage(page=1, model="docling-fast", used=True),
        ],
    )
    hybrid_pipeline = _FakeHybridPipeline(execution)
    service = PdfRagIngestService(hybrid_pipeline=hybrid_pipeline)
    monkeypatch.setattr("app.services.pdf_rag_ingest_service.settings.pdf_rag_structured_mode", "hybrid")

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    assert result["extractor"] == "local_structured_pdf_hybrid"
    assert hybrid_pipeline.calls[0][0] == "parse_document_with_trace"
    assert result["report"]["mode"] == "hybrid"
    assert result["report"]["triage_mode"] == "auto"
    assert result["report"]["backend_attempted_pages"] == [1]
    assert result["report"]["backend_used_pages"] == [1]
    assert result["report"]["backend_fallback_pages"] == []
    assert result["report"]["triage_backend_page_count"] == 1
    assert result["report"]["triage_local_page_count"] == 1


@pytest.mark.asyncio
async def test_ingest_pdf_returns_unapplied_when_structured_document_has_no_chunkable_content(monkeypatch):
    document = _document([])
    fast_pipeline = _FakeFastPipeline(document)
    service = PdfRagIngestService(fast_pipeline=fast_pipeline)
    monkeypatch.setattr("app.services.pdf_rag_ingest_service.settings.pdf_rag_structured_mode", "fast")

    result = await service.ingest_pdf(file_path="dummy.pdf")

    assert result["applied"] is False
    assert result["failure_reason"] == "no_structured_content"
    assert result["extractor"] == "local_structured_pdf_fast"
    assert result["report"]["block_count"] == 0
