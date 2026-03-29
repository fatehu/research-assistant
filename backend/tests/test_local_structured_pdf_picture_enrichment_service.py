from __future__ import annotations

import asyncio

from app.services.local_structured_pdf.contracts import (
    PdfBBox,
    PdfCurveAtom,
    PdfImageAtom,
    PdfPageAtoms,
    PdfPageMeta,
    PdfRectAtom,
    PdfStructuredDocument,
)
from app.services.local_structured_pdf.picture_enrichment_service import LocalPdfPictureEnrichmentService


def test_picture_enrichment_service_merges_image_candidates_and_describes_once() -> None:
    calls: list[tuple[int, PdfBBox, str | None]] = []

    class _FakeExtractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return [
                PdfPageAtoms(
                    meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                    images=[
                        PdfImageAtom(
                            image_id="img1",
                            bbox=PdfBBox(x0=100.0, top=200.0, x1=260.0, bottom=420.0),
                        ),
                        PdfImageAtom(
                            image_id="img2",
                            bbox=PdfBBox(x0=240.0, top=210.0, x1=430.0, bottom=430.0),
                        ),
                    ],
                )
            ]

    class _FakePageParser:
        async def describe_picture_region(self, *, pdf_path: str, page: int, bbox: PdfBBox, prompt: str | None = None):  # type: ignore[no-untyped-def]
            del pdf_path
            calls.append((page, bbox, prompt))
            return "Combined chart description", "qwen3.5:0.8b"

    service = LocalPdfPictureEnrichmentService(
        extractor=_FakeExtractor(),
        page_parser=_FakePageParser(),
    )

    result = asyncio.run(
        service.enrich_document(
            pdf_path="/tmp/demo.pdf",
            document=PdfStructuredDocument(),
            picture_description_prompt="Describe the chart",
        )
    )

    assert len(result) == 1
    assert result[0].description == "Combined chart description"
    assert result[0].model == "qwen3.5:0.8b"
    assert calls[0][0] == 1
    assert calls[0][1].x0 == 100.0
    assert calls[0][1].x1 == 430.0
    assert calls[0][2] == "Describe the chart"


def test_picture_enrichment_service_uses_graphic_regions_when_image_atoms_absent() -> None:
    class _FakeExtractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return [
                PdfPageAtoms(
                    meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                    rects=[
                        PdfRectAtom(rect_id="r1", bbox=PdfBBox(x0=120.0, top=220.0, x1=260.0, bottom=420.0)),
                    ],
                    curves=[
                        PdfCurveAtom(curve_id="c1", bbox=PdfBBox(x0=250.0, top=230.0, x1=430.0, bottom=430.0)),
                    ],
                )
            ]

    class _FakePageParser:
        async def describe_picture_region(self, *, pdf_path: str, page: int, bbox: PdfBBox, prompt: str | None = None):  # type: ignore[no-untyped-def]
            del pdf_path, prompt
            return f"page-{page}-graphic", "qwen3.5:0.8b"

    service = LocalPdfPictureEnrichmentService(
        extractor=_FakeExtractor(),
        page_parser=_FakePageParser(),
    )

    result = asyncio.run(
        service.enrich_document(
            pdf_path="/tmp/demo.pdf",
            document=PdfStructuredDocument(),
            picture_description_prompt=None,
        )
    )

    assert len(result) == 1
    assert result[0].description == "page-1-graphic"
    assert result[0].bbox.x0 == 120.0
    assert result[0].bbox.x1 == 430.0


def test_picture_enrichment_service_skips_extractor_when_page_atoms_provided() -> None:
    class _FailExtractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            raise AssertionError("extractor should not be called")

    class _FakePageParser:
        async def describe_picture_region(self, *, pdf_path: str, page: int, bbox: PdfBBox, prompt: str | None = None):  # type: ignore[no-untyped-def]
            del pdf_path, page, bbox, prompt
            return "Provided atoms description", "qwen3.5:0.8b"

    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
            images=[
                PdfImageAtom(
                    image_id="img1",
                    bbox=PdfBBox(x0=100.0, top=200.0, x1=260.0, bottom=420.0),
                ),
            ],
        )
    ]
    service = LocalPdfPictureEnrichmentService(
        extractor=_FailExtractor(),
        page_parser=_FakePageParser(),
    )

    result = asyncio.run(
        service.enrich_document(
            pdf_path="/tmp/demo.pdf",
            document=PdfStructuredDocument(),
            page_atoms=atoms,
        )
    )

    assert len(result) == 1
    assert result[0].description == "Provided atoms description"
