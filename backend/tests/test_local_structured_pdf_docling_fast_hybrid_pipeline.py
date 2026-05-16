from __future__ import annotations

from app.services.local_structured_pdf.contracts import (
    PdfBBox,
    PdfCurveAtom,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfImageAtom,
    PdfLineAtom,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfResolvedLine,
    PdfResolvedDocument,
    PdfResolvedPage,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
    PdfTableAtom,
    PdfTextLine,
    PdfWordAtom,
)
from app.services.local_structured_pdf.docling_fast_hybrid_pipeline import (
    LocalStructuredPdfDoclingFastHybridPipeline,
)
from app.services.local_structured_pdf.docling_fast_triage_service import (
    LocalPdfDoclingFastTriageService,
)
from app.services.local_structured_pdf.pipeline import LocalStructuredPdfPipeline


def _meta(page: int = 1) -> PdfPageMeta:
    return PdfPageMeta(page=page, page_width=600.0, page_height=800.0)


def _paragraph(*, text: str, page: int = 1) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=f"p{page}",
        block_type="paragraph",
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(x0=50.0, top=50.0, x1=250.0, bottom=90.0),
        line_ids=[f"l{page}"],
        reading_order_start=1,
        reading_order_end=1,
    )


def _resolved_line(
    *,
    line_id: str,
    text: str,
    page: int = 1,
    reading_order: int = 1,
    top: float = 50.0,
    x0: float = 50.0,
    x1: float = 250.0,
    bottom: float = 70.0,
    band: str = "body",
) -> PdfResolvedLine:
    words = [token for token in text.split() if token]
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}_w{index}" for index, _ in enumerate(words, start=1)],
        reading_order=reading_order,
        band=band,
    )


def _normalized_line(
    *,
    line_id: str,
    text: str,
    page: int = 1,
    top: float = 50.0,
    x0: float = 50.0,
    x1: float = 250.0,
    bottom: float = 70.0,
    band: str = "body",
) -> PdfTextLine:
    words = [token for token in text.split() if token]
    return PdfTextLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}_w{index}" for index, _ in enumerate(words, start=1)],
        band=band,
    )


def _word(
    *,
    word_id: str,
    text: str,
    top: float,
    x0: float,
    x1: float,
    bottom: float,
    doctop: float | None = None,
) -> PdfWordAtom:
    return PdfWordAtom(
        word_id=word_id,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        doctop=float(top if doctop is None else doctop),
    )


def test_docling_fast_triage_does_not_route_table_presence_alone_to_backend() -> None:
    service = LocalPdfDoclingFastTriageService()
    page_atoms = PdfPageAtoms(
        meta=_meta(),
        tables=[
            PdfTableAtom(
                table_id="t1",
                bbox=PdfBBox(x0=40.0, top=120.0, x1=560.0, bottom=320.0),
                row_count=4,
                col_count=3,
            )
        ],
    )

    result = service.triage_page(
        page_atoms=page_atoms,
        normalized_page=None,
        resolved_page=None,
        structured_page=None,
        mode="auto",
    )

    assert result.decision == "local"


def test_docling_fast_triage_does_not_route_table_border_approximation_on_geometry_alone() -> None:
    service = LocalPdfDoclingFastTriageService()
    page_atoms = PdfPageAtoms(
        meta=_meta(),
        rects=[
            type("Rect", (), {"bbox": PdfBBox(x0=60.0, top=120.0, x1=540.0, bottom=122.0)})(),
            type("Rect", (), {"bbox": PdfBBox(x0=60.0, top=170.0, x1=540.0, bottom=172.0)})(),
            type("Rect", (), {"bbox": PdfBBox(x0=60.0, top=120.0, x1=62.0, bottom=222.0)})(),
            type("Rect", (), {"bbox": PdfBBox(x0=300.0, top=120.0, x1=302.0, bottom=222.0)})(),
        ],
    )
    normalized_page = PdfNormalizedPage(
        meta=_meta(),
        text_lines=[
            _normalized_line(
                line_id=f"n{index}",
                text="This normalized view is intentionally much denser than the filtered content",
                top=20.0 + (index * 18.0),
            )
            for index in range(1, 13)
        ],
    )
    resolved_page = PdfResolvedPage(
        meta=_meta(),
        lines=[
            _resolved_line(line_id="r1", text="Method Value", reading_order=1, top=128.0),
            _resolved_line(line_id="r2", text="A 0.81", reading_order=2, top=148.0),
            _resolved_line(line_id="r3", text="B 0.79", reading_order=3, top=168.0),
            _resolved_line(line_id="r4", text="C 0.77", reading_order=4, top=188.0),
        ],
    )

    result = service.triage_page(
        page_atoms=page_atoms,
        normalized_page=normalized_page,
        resolved_page=resolved_page,
        structured_page=None,
        mode="auto",
    )

    assert result.decision == "local"
    assert result.reasons == ["java_triage:default_local"]
    assert result.signals.text_line_count == 4
    assert result.signals.average_words_per_line == 2.0


def test_docling_fast_triage_routes_text_table_pattern_from_word_chunks() -> None:
    service = LocalPdfDoclingFastTriageService()
    normalized_page = PdfNormalizedPage(
        meta=_meta(),
        kept_words=[
            _word(word_id="w1", text="Saccharometer", top=100.0, x0=74.0, x1=150.0, bottom=112.0),
            _word(word_id="w2", text="DI", top=100.0, x0=154.0, x1=168.0, bottom=112.0),
            _word(word_id="w3", text="Water", top=100.0, x0=171.0, x1=204.0, bottom=112.0),
            _word(word_id="w4", text="Glucose", top=100.0, x0=206.0, x1=247.0, bottom=112.0),
            _word(word_id="w5", text="Solution", top=100.0, x0=250.0, x1=293.0, bottom=112.0),
            _word(word_id="w6", text="Yeast", top=100.0, x0=296.0, x1=325.0, bottom=112.0),
            _word(word_id="w7", text="Suspension", top=100.0, x0=328.0, x1=385.0, bottom=112.0),
            _word(word_id="w8", text="2", top=118.0, x0=74.0, x1=80.0, bottom=130.0),
            _word(word_id="w9", text="24", top=118.0, x0=154.0, x1=167.0, bottom=130.0),
            _word(word_id="w10", text="ml", top=118.0, x0=170.0, x1=182.0, bottom=130.0),
            _word(word_id="w11", text="0", top=118.0, x0=206.0, x1=212.0, bottom=130.0),
            _word(word_id="w12", text="ml", top=118.0, x0=215.0, x1=228.0, bottom=130.0),
            _word(word_id="w13", text="4", top=118.0, x0=296.0, x1=302.0, bottom=130.0),
            _word(word_id="w14", text="ml", top=118.0, x0=305.0, x1=318.0, bottom=130.0),
            _word(word_id="w15", text="3", top=136.0, x0=74.0, x1=80.0, bottom=148.0),
            _word(word_id="w16", text="12", top=136.0, x0=154.0, x1=167.0, bottom=148.0),
            _word(word_id="w17", text="ml", top=136.0, x0=170.0, x1=182.0, bottom=148.0),
            _word(word_id="w18", text="12", top=136.0, x0=206.0, x1=219.0, bottom=148.0),
            _word(word_id="w19", text="ml", top=136.0, x0=222.0, x1=234.0, bottom=148.0),
            _word(word_id="w20", text="4", top=136.0, x0=296.0, x1=302.0, bottom=148.0),
            _word(word_id="w21", text="ml", top=136.0, x0=305.0, x1=318.0, bottom=148.0),
        ],
        text_lines=[
            _normalized_line(line_id="l1", text="Saccharometer DI Water Glucose Solution Yeast Suspension", top=100.0, x0=74.0, x1=385.0, bottom=112.0),
            _normalized_line(line_id="l2", text="2 24 ml 0 ml 4 ml", top=118.0, x0=74.0, x1=318.0, bottom=130.0),
            _normalized_line(line_id="l3", text="3 12 ml 12 ml 4 ml", top=136.0, x0=74.0, x1=318.0, bottom=148.0),
        ],
    )
    resolved_page = PdfResolvedPage(
        meta=_meta(),
        lines=[
            _resolved_line(line_id="r1", text="Saccharometer DI Water Glucose Solution Yeast Suspension", top=100.0, x0=74.0, x1=385.0, bottom=112.0),
            _resolved_line(line_id="r2", text="2 24 ml 0 ml 4 ml", top=118.0, x0=74.0, x1=318.0, bottom=130.0),
            _resolved_line(line_id="r3", text="3 12 ml 12 ml 4 ml", top=136.0, x0=74.0, x1=318.0, bottom=148.0),
        ],
    )

    result = service.triage_page(
        page_atoms=PdfPageAtoms(meta=_meta()),
        normalized_page=normalized_page,
        resolved_page=resolved_page,
        structured_page=None,
        mode="auto",
    )

    assert result.decision == "backend"
    assert result.reasons == ["java_triage:text_table_pattern"]
    assert result.signals.table_pattern_count >= 3
    assert result.signals.has_consecutive_patterns is True


def test_docling_fast_triage_routes_vector_table_signal_without_table_area() -> None:
    service = LocalPdfDoclingFastTriageService()
    page_atoms = PdfPageAtoms(
        meta=_meta(),
        curves=[
            type("Curve", (), {"bbox": PdfBBox(x0=60.0, top=120.0, x1=120.0, bottom=160.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=125.0, top=120.0, x1=185.0, bottom=160.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=190.0, top=120.0, x1=250.0, bottom=160.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=255.0, top=120.0, x1=315.0, bottom=160.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=60.0, top=165.0, x1=120.0, bottom=205.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=125.0, top=165.0, x1=185.0, bottom=205.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=190.0, top=165.0, x1=250.0, bottom=205.0)})(),
            type("Curve", (), {"bbox": PdfBBox(x0=255.0, top=165.0, x1=315.0, bottom=205.0)})(),
        ],
    )
    normalized_page = PdfNormalizedPage(
        meta=_meta(),
        text_lines=[
            _normalized_line(line_id="n1", text="This page has normal prose around a vector table", top=60.0, x0=80.0, x1=420.0, bottom=72.0, band="body"),
            _normalized_line(line_id="n2", text="Method Value Delta", top=130.0, x0=80.0, x1=240.0, bottom=142.0, band="body"),
            _normalized_line(line_id="n3", text="A 0.81 +0.02", top=160.0, x0=80.0, x1=210.0, bottom=172.0, band="body"),
        ],
    )
    resolved_page = PdfResolvedPage(
        meta=_meta(),
        lines=[
            _resolved_line(line_id="r1", text="This page has normal prose around a vector table", top=60.0, x0=80.0, x1=420.0, bottom=72.0),
            _resolved_line(line_id="r2", text="Method Value Delta", top=130.0, x0=80.0, x1=240.0, bottom=142.0),
            _resolved_line(line_id="r3", text="A 0.81 +0.02", top=160.0, x0=80.0, x1=210.0, bottom=172.0),
        ],
    )

    result = service.triage_page(
        page_atoms=page_atoms,
        normalized_page=normalized_page,
        resolved_page=resolved_page,
        structured_page=None,
        mode="auto",
    )

    assert result.decision == "backend"
    assert result.reasons == ["java_triage:vector_table_signal"]
    assert result.signals.line_art_count >= 8


def test_docling_fast_triage_routes_mixed_visual_table_signal() -> None:
    service = LocalPdfDoclingFastTriageService()
    page_atoms = PdfPageAtoms(
        meta=_meta(),
        images=[
            PdfImageAtom(
                image_id="img1",
                bbox=PdfBBox(x0=340.0, top=120.0, x1=540.0, bottom=360.0),
            )
        ],
        curves=[
            PdfCurveAtom(curve_id="c1", bbox=PdfBBox(x0=74.0, top=180.0, x1=210.0, bottom=220.0)),
            PdfCurveAtom(curve_id="c2", bbox=PdfBBox(x0=74.0, top=230.0, x1=210.0, bottom=270.0)),
            PdfCurveAtom(curve_id="c3", bbox=PdfBBox(x0=74.0, top=280.0, x1=210.0, bottom=320.0)),
        ],
    )
    normalized_page = PdfNormalizedPage(
        meta=_meta(),
        kept_words=[
            _word(word_id="w1", text="Revenue", top=180.0, x0=74.0, x1=126.0, bottom=192.0),
            _word(word_id="w2", text="by", top=180.0, x0=130.0, x1=144.0, bottom=192.0),
            _word(word_id="w3", text="segment", top=180.0, x0=148.0, x1=198.0, bottom=192.0),
            _word(word_id="w4", text="Q4", top=180.0, x0=340.0, x1=356.0, bottom=192.0),
            _word(word_id="w5", text="2025", top=180.0, x0=360.0, x1=388.0, bottom=192.0),
            _word(word_id="w6", text="summary", top=180.0, x0=392.0, x1=438.0, bottom=192.0),
            _word(word_id="w7", text="North", top=205.0, x0=74.0, x1=112.0, bottom=217.0),
            _word(word_id="w8", text="America", top=205.0, x0=116.0, x1=164.0, bottom=217.0),
            _word(word_id="w9", text="consumer", top=205.0, x0=168.0, x1=220.0, bottom=217.0),
            _word(word_id="w10", text="14.2", top=205.0, x0=340.0, x1=364.0, bottom=217.0),
            _word(word_id="w11", text="billion", top=205.0, x0=368.0, x1=404.0, bottom=217.0),
            _word(word_id="w12", text="usd", top=205.0, x0=408.0, x1=426.0, bottom=217.0),
        ],
        text_lines=[
            _normalized_line(
                line_id="n1",
                text="Revenue by segment Q4 2025 summary",
                top=180.0,
                x0=74.0,
                x1=438.0,
                bottom=192.0,
            ),
            _normalized_line(
                line_id="n2",
                text="North America consumer 14.2 billion usd",
                top=205.0,
                x0=74.0,
                x1=426.0,
                bottom=217.0,
            ),
        ],
    )
    resolved_page = PdfResolvedPage(
        meta=_meta(),
        column_count=2,
        lines=[
            _resolved_line(
                line_id="r1",
                text="Revenue by segment Q4 2025 summary",
                reading_order=1,
                top=180.0,
                x0=74.0,
                x1=438.0,
                bottom=192.0,
            ),
            _resolved_line(
                line_id="r2",
                text="North America consumer 14.2 billion usd",
                reading_order=2,
                top=205.0,
                x0=74.0,
                x1=426.0,
                bottom=217.0,
            ),
        ],
    )
    structured_page = PdfStructuredPage(
        meta=_meta(),
        blocks=[
            PdfSemanticBlock(
                block_id="h1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Regional revenue highlights",
                bbox=PdfBBox(x0=72.0, top=72.0, x1=310.0, bottom=98.0),
                line_ids=["r0"],
                reading_order_start=0,
                reading_order_end=0,
            )
        ],
    )

    result = service.triage_page(
        page_atoms=page_atoms,
        normalized_page=normalized_page,
        resolved_page=resolved_page,
        structured_page=structured_page,
        mode="auto",
    )

    assert result.decision == "backend"
    assert result.reasons == ["python_triage:mixed_visual_table_signal"]
    assert result.signals.double_column is True
    assert result.signals.image_count == 1
    assert result.signals.heading_count == 1
    assert result.signals.table_pattern_count >= 1


def test_docling_fast_triage_text_chunk_direction_matches_java_coordinates() -> None:
    service = LocalPdfDoclingFastTriageService()

    previous = _word(word_id="w1", text="Earlier line", top=100.0, x0=80.0, x1=180.0, bottom=112.0)
    current_below = _word(word_id="w2", text="Next line below", top=118.0, x0=82.0, x1=188.0, bottom=130.0)
    current_above = _word(word_id="w3", text="Backwards chunk", top=82.0, x0=84.0, x1=170.0, bottom=94.0)

    assert service._are_suspicious_word_chunks(previous=previous, current=current_below) is False
    assert service._are_suspicious_word_chunks(previous=previous, current=current_above) is True


def test_docling_fast_triage_row_separator_requires_text_line_alternation() -> None:
    service = LocalPdfDoclingFastTriageService()
    horizontal_lines = [
        PdfLineAtom(
            line_id=f"h{index}",
            bbox=PdfBBox(x0=80.0, top=110.0 + (index * 30.0), x1=320.0, bottom=112.0 + (index * 30.0)),
        )
        for index in range(6)
    ]
    page_atoms = PdfPageAtoms(meta=_meta(), lines=horizontal_lines)

    no_text_metrics = service._extract_vector_metrics(page_atoms=page_atoms, text_chunks=[])
    assert no_text_metrics["has_row_separator_pattern"] is False

    text_chunks = [
        _word(word_id="w1", text="row1", top=95.0, x0=90.0, x1=120.0, bottom=103.0),
        _word(word_id="w2", text="row2", top=125.0, x0=90.0, x1=120.0, bottom=133.0),
        _word(word_id="w3", text="row3", top=155.0, x0=90.0, x1=120.0, bottom=163.0),
        _word(word_id="w4", text="row4", top=185.0, x0=90.0, x1=120.0, bottom=193.0),
        _word(word_id="w5", text="row5", top=215.0, x0=90.0, x1=120.0, bottom=223.0),
        _word(word_id="w6", text="row6", top=245.0, x0=90.0, x1=120.0, bottom=253.0),
    ]
    alternating_metrics = service._extract_vector_metrics(page_atoms=page_atoms, text_chunks=text_chunks)
    assert alternating_metrics["has_row_separator_pattern"] is True


def test_docling_fast_backend_prompt_payload_includes_filtered_line_rows() -> None:
    resolved_page = PdfResolvedPage(
        meta=_meta(),
        lines=[
            _resolved_line(line_id="l2", text="Second row", reading_order=2, top=90.0),
            _resolved_line(line_id="l1", text="First row", reading_order=1, top=50.0),
        ],
    )
    triage_page = PdfHybridTriageResult(
        page=1,
        page_type="dense_table",
        decision="backend",
        confidence=0.95,
        reasons=["test"],
    )

    payload = LocalStructuredPdfDoclingFastHybridPipeline._build_backend_prompt_payload(
        page=1,
        resolved_page=resolved_page,
        triage_page=triage_page,
    )

    assert [row["line_id"] for row in payload["line_rows"]] == ["l1", "l2"]
    assert payload["line_rows"][0]["text"] == "First row"
    assert payload["line_rows"][0]["bbox"] == {
        "x0": 50.0,
        "top": 50.0,
        "x1": 250.0,
        "bottom": 70.0,
    }
    assert payload["triage"]["page_type"] == "dense_table"


class _FakeDoclingFastHybridPipeline(LocalStructuredPdfDoclingFastHybridPipeline):
    def __init__(self) -> None:
        super().__init__(pipeline=LocalStructuredPdfPipeline())

    def _build_local_artifacts(self, *, pdf_path: str, page_limit, mode: str, include_chars: bool):
        del pdf_path, page_limit, include_chars
        local_document = PdfStructuredDocument(
            pages=[
                PdfStructuredPage(
                    meta=_meta(),
                    blocks=[_paragraph(text="Local text")],
                )
            ],
            blocks=[_paragraph(text="Local text")],
        )
        triage_document = PdfHybridTriageDocument(
            mode=mode,
            pages=[
                PdfHybridTriageResult(
                    page=1,
                    page_type="mixed_layout",
                    decision="backend",
                    confidence=0.95,
                    reasons=["test"],
                )
            ],
        )
        resolved_document = PdfResolvedDocument(
            pages=[
                PdfResolvedPage(
                    meta=_meta(),
                    lines=[
                        PdfResolvedLine(
                            line_id="l1",
                            page=1,
                            text="Local text",
                            bbox=PdfBBox(x0=50.0, top=50.0, x1=250.0, bottom=90.0),
                            word_ids=["w1"],
                            reading_order=1,
                        )
                    ],
                )
            ]
        )
        return local_document, triage_document, [], [], resolved_document

    async def _convert_via_backend(self, *, pdf_path: str, page_numbers):
        del pdf_path, page_numbers
        return (
            {
                "pages": {"1": {"page_no": 1, "size": {"width": 600.0, "height": 800.0}}},
                "texts": [
                    {
                        "label": "text",
                        "text": "Backend text",
                        "orig": "Backend text",
                        "prov": [
                            {
                                "page_no": 1,
                                "bbox": {
                                    "l": 50.0,
                                    "t": 50.0,
                                    "r": 250.0,
                                    "b": 90.0,
                                    "coord_origin": "TOPLEFT",
                                },
                            }
                        ],
                    }
                ],
                "tables": [],
                "pictures": [],
            },
            set(),
            "",
        )


def test_docling_fast_hybrid_pipeline_fuses_backend_output() -> None:
    import asyncio

    pipeline = _FakeDoclingFastHybridPipeline()
    trace = asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/sample.pdf",
            mode="auto",
        )
    )

    assert trace.triage.backend_pages == [1]
    assert trace.parsed_pages[0].model == "docling-fast"
    assert trace.parsed_pages[0].used is True
    assert trace.document.blocks[0].text == "Backend text"
