from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalStructuredPdfHybridFusionService,
    PdfBBox,
    PdfHybridParsedBlock,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


def _meta(page: int) -> PdfPageMeta:
    return PdfPageMeta(page=page, page_width=600.0, page_height=800.0, rotation=0)


def _line(
    *,
    page: int,
    line_id: str,
    text: str,
    order: int,
    font_size: float = 12.0,
    top: float = 100.0,
    bottom: float = 114.0,
    x0: float = 80.0,
    x1: float = 320.0,
) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=font_size,
        dominant_font_name="Times-Bold" if font_size >= 16.0 else "Times-Roman",
        band="body",
        region="main",
        column_id="main",
        reading_order=order,
    )


def test_hybrid_fusion_replaces_backend_page_and_uses_backend_table_rows():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[_line(page=1, line_id="p1_l1", text="Local heading", order=1, font_size=18.0)],
                column_count=1,
            ),
            PdfResolvedPage(
                meta=_meta(2),
                lines=[
                    _line(page=2, line_id="p2_l1", text="Metric", order=1, top=120.0, bottom=134.0, x1=180.0),
                    _line(page=2, line_id="p2_l2", text="Value", order=2, top=120.0, bottom=134.0, x0=220.0, x1=320.0),
                ],
                column_count=1,
            ),
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=_meta(1),
                blocks=[
                    PdfSemanticBlock(
                        block_id="local_p1_h1",
                        block_type="heading",
                        page_start=1,
                        page_end=1,
                        text="Local heading",
                        bbox=PdfBBox(x0=80.0, top=100.0, x1=320.0, bottom=114.0),
                        line_ids=["p1_l1"],
                        avg_font_size=18.0,
                        reading_order_start=1,
                        reading_order_end=1,
                        heading_level=2,
                    )
                ],
            ),
            PdfStructuredPage(
                meta=_meta(2),
                blocks=[
                    PdfSemanticBlock(
                        block_id="local_p2_t1",
                        block_type="table",
                        page_start=2,
                        page_end=2,
                        text="Metric Value",
                        bbox=PdfBBox(x0=80.0, top=120.0, x1=320.0, bottom=134.0),
                        line_ids=["p2_l1", "p2_l2"],
                        avg_font_size=12.0,
                        reading_order_start=1,
                        reading_order_end=2,
                        table_rows=[["Metric", "Value"], ["A", "1"]],
                    )
                ],
            ),
        ],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="plain_text",
                decision="local",
                confidence=0.9,
                reasons=["page_type:plain_text"],
                signals=PdfHybridTriageSignals(text_line_count=1),
            ),
            PdfHybridTriageResult(
                page=2,
                page_type="dense_table",
                decision="backend",
                confidence=0.88,
                reasons=["page_type:dense_table"],
                signals=PdfHybridTriageSignals(text_line_count=2, table_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=2,
            model="qwen-vl-local",
            page_role="table",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0002_b0001",
                    kind="table",
                    page=2,
                    reading_order=1,
                    text="Metric Value",
                    bbox=PdfBBox(x0=80.0, top=120.0, x1=320.0, bottom=134.0),
                    source_line_ids=["p2_l1", "p2_l2"],
                    table_rows=[["Metric", "Value"], ["A", "1"]],
                    zone="table",
                    merge_strategy="space",
                    confidence=0.91,
                )
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert [page.page for page in fused.pages] == [1, 2]
    assert fused.pages[0].blocks[0].block_id == "local_p1_h1"
    assert fused.pages[1].blocks[0].block_id == "mm_p0002_b0001"
    assert fused.pages[1].blocks[0].table_rows == [["Metric", "Value"], ["A", "1"]]
    assert [block.block_id for block in fused.blocks] == ["local_p1_h1", "mm_p0002_b0001"]


def test_hybrid_fusion_prefers_backend_table_rows_when_present():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[],
                column_count=1,
            ),
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=_meta(1),
                blocks=[
                    PdfSemanticBlock(
                        block_id="local_t1",
                        block_type="table",
                        page_start=1,
                        page_end=1,
                        text="Metric Value",
                        bbox=PdfBBox(x0=80.0, top=120.0, x1=320.0, bottom=220.0),
                        line_ids=[],
                        reading_order_start=1,
                        reading_order_end=1,
                        table_rows=[["Local", "Rows"]],
                    )
                ],
            ),
        ],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="dense_table",
                decision="backend",
                confidence=0.9,
                reasons=["page_type:dense_table"],
                signals=PdfHybridTriageSignals(table_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen3.5:0.8b",
            page_role="table",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_t1",
                    kind="table",
                    page=1,
                    reading_order=1,
                    text="Metric | Value\nA | 1",
                    bbox=PdfBBox(x0=80.0, top=120.0, x1=320.0, bottom=220.0),
                    source_line_ids=[],
                    table_rows=[["Metric", "Value"], ["A", "1"]],
                    zone="table",
                )
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert fused.pages[0].blocks[0].table_rows == [["Metric", "Value"], ["A", "1"]]


def test_hybrid_fusion_keeps_unanchored_backend_ocr_block():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[_line(page=1, line_id="p1_l1", text="tiny residual", order=1, top=760.0, bottom=772.0)],
                column_count=1,
            )
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=_meta(1),
                blocks=[
                    PdfSemanticBlock(
                        block_id="local_p1_b1",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="tiny residual",
                        bbox=PdfBBox(x0=80.0, top=760.0, x1=180.0, bottom=772.0),
                        line_ids=["p1_l1"],
                        avg_font_size=10.0,
                        reading_order_start=1,
                        reading_order_end=1,
                    )
                ],
            )
        ],
        blocks=[],
        body_font_size=10.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="visual_or_scanned",
                decision="backend",
                confidence=0.95,
                reasons=["page_type:visual_or_scanned"],
                signals=PdfHybridTriageSignals(text_line_count=1, image_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen-vl-local",
            page_role="poster",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0001",
                    kind="heading",
                    page=1,
                    reading_order=1,
                    text="REAL TITLE FROM OCR",
                    bbox=PdfBBox(x0=100.0, top=80.0, x1=520.0, bottom=150.0),
                    source_line_ids=[],
                    zone="main",
                    merge_strategy="space",
                    confidence=0.92,
                )
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert fused.pages[0].blocks[0].block_id == "mm_p0001_b0001"
    assert fused.pages[0].blocks[0].text == "REAL TITLE FROM OCR"
    assert fused.pages[0].blocks[0].line_ids == []


def test_hybrid_fusion_uses_backend_result_for_backend_routed_visual_page():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[
                    _line(page=1, line_id="p1_l1", text="Light visual title", order=1, top=120.0, bottom=136.0),
                    _line(page=1, line_id="p1_l2", text="Usable native text remains on page", order=2, top=144.0, bottom=158.0),
                ],
                column_count=1,
            )
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=_meta(1),
                blocks=[
                    PdfSemanticBlock(
                        block_id="local_p1_b1",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Light visual title Usable native text remains on page",
                        bbox=PdfBBox(x0=80.0, top=120.0, x1=420.0, bottom=158.0),
                        line_ids=["p1_l1", "p1_l2"],
                        avg_font_size=12.0,
                        reading_order_start=1,
                        reading_order_end=2,
                    )
                ],
            )
        ],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="visual_or_scanned",
                decision="backend",
                confidence=0.91,
                reasons=["page_type:visual_or_scanned"],
                signals=PdfHybridTriageSignals(text_line_count=2, image_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen-vl-local",
            page_role="body",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0001",
                    kind="paragraph",
                    page=1,
                    reading_order=1,
                    text="Light visual title",
                    bbox=PdfBBox(x0=80.0, top=120.0, x1=260.0, bottom=136.0),
                    source_line_ids=["p1_l1"],
                    zone="main",
                    merge_strategy="space",
                    confidence=0.9,
                ),
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0002",
                    kind="paragraph",
                    page=1,
                    reading_order=2,
                    text="Usable native text remains on page",
                    bbox=PdfBBox(x0=80.0, top=144.0, x1=420.0, bottom=158.0),
                    source_line_ids=["p1_l2"],
                    zone="main",
                    merge_strategy="space",
                    confidence=0.9,
                ),
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert [block.block_id for block in fused.pages[0].blocks] == ["mm_p0001_b0001", "mm_p0001_b0002"]
    assert fused.pages[0].blocks[0].text == "Light visual title"


def test_hybrid_fusion_prefers_backend_heading_level_from_docling_meta():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[],
                column_count=1,
            )
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=_meta(1), blocks=[])],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="mixed_layout",
                decision="backend",
                confidence=0.9,
                reasons=["page_type:mixed_layout"],
                signals=PdfHybridTriageSignals(text_line_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen-vl-local",
            page_role="body",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0001",
                    kind="heading",
                    page=1,
                    reading_order=1,
                    text="Introduction",
                    bbox=PdfBBox(x0=80.0, top=100.0, x1=220.0, bottom=116.0),
                    source_line_ids=[],
                    zone="main",
                    merge_strategy="space",
                    confidence=0.91,
                    heading_level=2,
                )
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert fused.pages[0].blocks[0].block_type == "heading"
    assert fused.pages[0].blocks[0].heading_level == 2


def test_hybrid_fusion_preserves_backend_reading_order_over_local_line_order():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=_meta(1),
                lines=[
                    _line(page=1, line_id="p1_l1", text="Body first locally", order=20, top=160.0, bottom=176.0),
                    _line(page=1, line_id="p1_l2", text="Heading second locally", order=10, top=100.0, bottom=116.0),
                ],
                column_count=1,
            )
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=_meta(1), blocks=[])],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="mixed_layout",
                decision="backend",
                confidence=0.9,
                reasons=["page_type:mixed_layout"],
                signals=PdfHybridTriageSignals(text_line_count=2),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="docling-fast",
            page_role="body",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0001",
                    kind="heading",
                    page=1,
                    reading_order=1,
                    text="Heading second locally",
                    bbox=PdfBBox(x0=80.0, top=100.0, x1=280.0, bottom=116.0),
                    source_line_ids=["p1_l2"],
                    zone="main",
                ),
                PdfHybridParsedBlock(
                    block_id="mm_p0001_b0002",
                    kind="paragraph",
                    page=1,
                    reading_order=2,
                    text="Body first locally",
                    bbox=PdfBBox(x0=80.0, top=160.0, x1=320.0, bottom=176.0),
                    source_line_ids=["p1_l1"],
                    zone="main",
                ),
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert [block.block_id for block in fused.pages[0].blocks] == ["mm_p0001_b0001", "mm_p0001_b0002"]
    assert [block.reading_order_start for block in fused.pages[0].blocks] == [1, 2]


def test_hybrid_fusion_demotes_extra_visual_page_headings_to_paragraphs():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=1000.0, page_height=2000.0, rotation=0),
                lines=[],
                column_count=1,
            )
        ]
    )
    local_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=PdfPageMeta(page=1, page_width=1000.0, page_height=2000.0, rotation=0), blocks=[])],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="visual_or_scanned",
                decision="backend",
                confidence=0.95,
                reasons=["page_type:visual_or_scanned"],
                signals=PdfHybridTriageSignals(text_line_count=0, image_count=1),
            ),
        ],
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen3.5:0.8b",
            page_role="poster",
            used=True,
            blocks=[
                PdfHybridParsedBlock(
                    block_id="b1",
                    kind="heading",
                    page=1,
                    reading_order=1,
                    text="TOP TITLE",
                    bbox=PdfBBox(x0=50.0, top=30.0, x1=600.0, bottom=80.0),
                    source_line_ids=[],
                    zone="main",
                ),
                PdfHybridParsedBlock(
                    block_id="b2",
                    kind="heading",
                    page=1,
                    reading_order=2,
                    text="COPYRIGHT",
                    bbox=PdfBBox(x0=60.0, top=90.0, x1=500.0, bottom=140.0),
                    source_line_ids=[],
                    zone="main",
                ),
                PdfHybridParsedBlock(
                    block_id="b3",
                    kind="heading",
                    page=1,
                    reading_order=3,
                    text="COPYRIGHT PROTECTS CREATIVE WORK - YOURS, MINE, EVERYONE'S!",
                    bbox=PdfBBox(x0=70.0, top=260.0, x1=800.0, bottom=320.0),
                    source_line_ids=[],
                    zone="main",
                ),
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert [block.block_type for block in fused.pages[0].blocks] == ["heading", "heading", "paragraph"]


def test_hybrid_fusion_dedupes_identical_backend_blocks():
    service = LocalStructuredPdfHybridFusionService()
    resolved_document = PdfResolvedDocument(
        pages=[PdfResolvedPage(meta=_meta(1), lines=[], column_count=1)]
    )
    local_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=_meta(1), blocks=[])],
        blocks=[],
        body_font_size=12.0,
    )
    triage_document = PdfHybridTriageDocument(
        mode="auto",
        pages=[
            PdfHybridTriageResult(
                page=1,
                page_type="visual_or_scanned",
                decision="backend",
                confidence=0.95,
                reasons=["page_type:visual_or_scanned"],
                signals=PdfHybridTriageSignals(text_line_count=0, image_count=1),
            ),
        ],
    )
    duplicate = PdfHybridParsedBlock(
        block_id="dup1",
        kind="paragraph",
        page=1,
        reading_order=1,
        text="Repeated OCR text",
        bbox=PdfBBox(x0=80.0, top=260.0, x1=480.0, bottom=320.0),
        source_line_ids=[],
        zone="main",
    )
    parsed_pages = [
        PdfHybridParsedPage(
            page=1,
            model="qwen3.5:0.8b",
            page_role="poster",
            used=True,
            blocks=[
                duplicate,
                PdfHybridParsedBlock(
                    block_id="dup2",
                    kind="paragraph",
                    page=1,
                    reading_order=2,
                    text="Repeated OCR text",
                    bbox=PdfBBox(x0=80.0, top=260.0, x1=480.0, bottom=320.0),
                    source_line_ids=[],
                    zone="main",
                ),
            ],
        )
    ]

    fused = service.fuse_document(
        resolved_document=resolved_document,
        local_document=local_document,
        triage_document=triage_document,
        parsed_pages=parsed_pages,
    )

    assert len(fused.pages[0].blocks) == 1
    assert fused.pages[0].blocks[0].text == "Repeated OCR text"
