from __future__ import annotations

import asyncio

from app.services.local_structured_pdf.contracts import PdfBBox, PdfPageMeta, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage
from app.services.local_structured_pdf.formula_enrichment_service import LocalPdfFormulaEnrichmentService


def _equation_block(text: str = "raw") -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id="eq1",
        block_type="equation",
        page_start=1,
        page_end=1,
        text=text,
        bbox=PdfBBox(x0=120.0, top=220.0, x1=360.0, bottom=290.0),
        line_ids=[],
    )


def test_formula_enrichment_service_rewrites_equation_text() -> None:
    class _FakePageParser:
        async def describe_formula_region(self, *, pdf_path: str, page: int, bbox: PdfBBox):  # type: ignore[no-untyped-def]
            del pdf_path, page, bbox
            return r"\frac{d}{dt}\\left(\\frac{\\partial L}{\\partial \\dot{q}}\\right)-\\frac{\\partial L}{\\partial q}=0", "qwen3.5:0.8b"

    service = LocalPdfFormulaEnrichmentService(page_parser=_FakePageParser())
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                blocks=[_equation_block()],
            )
        ],
        blocks=[_equation_block()],
    )

    enriched = asyncio.run(service.enrich_document(pdf_path="/tmp/demo.pdf", document=document))

    assert enriched.blocks[0].text.startswith(r"\frac{d}{dt}")
    assert enriched.pages[0].blocks[0].text.startswith(r"\frac{d}{dt}")


def test_formula_enrichment_service_falls_back_to_page_level_formula_when_no_equation_blocks() -> None:
    class _FakePageParser:
        async def describe_formula_region(self, *, pdf_path: str, page: int, bbox: PdfBBox):  # type: ignore[no-untyped-def]
            del pdf_path, page, bbox
            return r"E=mc^2", "qwen3.5:0.8b"

    service = LocalPdfFormulaEnrichmentService(page_parser=_FakePageParser())
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                blocks=[],
            )
        ],
        blocks=[],
    )

    enriched = asyncio.run(service.enrich_document(pdf_path="/tmp/demo.pdf", document=document))

    assert enriched.blocks[0].block_type == "equation"
    assert enriched.blocks[0].text == r"E=mc^2"
