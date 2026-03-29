from __future__ import annotations

import asyncio

from app.services.local_structured_pdf.contracts import PdfPageAtoms, PdfPageMeta, PdfStructuredDocument
from app.services.local_structured_pdf.ocr_enrichment_service import LocalPdfOcrEnrichmentService


def test_ocr_enrichment_service_creates_page_block_from_qwen_text() -> None:
    class _FakeExtractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return [
                PdfPageAtoms(
                    meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                )
            ]

    class _FakePageParser:
        async def transcribe_page_text(  # type: ignore[no-untyped-def]
            self,
            *,
            pdf_path: str,
            page: int,
            page_type: str | None = None,
            prompt: str | None = None,
        ):
            del pdf_path, page, prompt
            assert page_type == "visual_or_scanned"
            return "Recovered OCR text", "qwen3.5:0.8b"

    service = LocalPdfOcrEnrichmentService(
        extractor=_FakeExtractor(),
        page_parser=_FakePageParser(),
    )

    enriched = asyncio.run(
        service.enrich_document(
            pdf_path="/tmp/demo.pdf",
            document=PdfStructuredDocument(),
            ocr_lang=["en"],
        )
    )

    assert len(enriched.pages) == 1
    assert enriched.pages[0].blocks[0].text == "Recovered OCR text"
    assert enriched.pages[0].blocks[0].block_type == "paragraph"


def test_ocr_enrichment_service_skips_extractor_when_page_atoms_provided() -> None:
    class _FailExtractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            raise AssertionError("extractor should not be called")

    class _FakePageParser:
        async def transcribe_page_text(  # type: ignore[no-untyped-def]
            self,
            *,
            pdf_path: str,
            page: int,
            page_type: str | None = None,
            prompt: str | None = None,
        ):
            del pdf_path, page, prompt
            assert page_type == "visual_or_scanned"
            return "Recovered OCR text", "qwen3.5:0.8b"

    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
        )
    ]
    service = LocalPdfOcrEnrichmentService(
        extractor=_FailExtractor(),
        page_parser=_FakePageParser(),
    )

    enriched = asyncio.run(
        service.enrich_document(
            pdf_path="/tmp/demo.pdf",
            document=PdfStructuredDocument(),
            ocr_lang=["en"],
            page_atoms=atoms,
        )
    )

    assert len(enriched.pages) == 1
    assert enriched.pages[0].blocks[0].text == "Recovered OCR text"
