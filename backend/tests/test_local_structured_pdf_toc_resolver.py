from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfTocResolver,
    PdfBBox,
    PdfPageMeta,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


def _block(
    *,
    block_id: str,
    block_type: str,
    text: str,
    top: float,
    bottom: float,
    page: int = 1,
    avg_font_size: float = 12.0,
    column_id: str = "main",
    reading_order: int = 1,
    x0: float = 60.0,
    x1: float = 320.0,
    heading_level: int | None = None,
) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        line_ids=[f"{block_id}:l1"],
        avg_font_size=avg_font_size,
        column_id=column_id,
        reading_order_start=reading_order,
        reading_order_end=reading_order,
        heading_level=heading_level,
    )


def test_resolve_document_demotes_toc_headings_and_merges_page_numbers():
    resolver = LocalPdfTocResolver()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="toc",
            block_type="heading",
            text="Table of Contents",
            top=30.0,
            bottom=50.0,
            avg_font_size=20.0,
            reading_order=1,
            heading_level=1,
        ),
        _block(
            block_id="entry_1",
            block_type="heading",
            text="Executive Summary",
            top=100.0,
            bottom=116.0,
            avg_font_size=14.0,
            reading_order=2,
            heading_level=2,
        ),
        _block(
            block_id="entry_2",
            block_type="heading",
            text="Legal Framework",
            top=126.0,
            bottom=142.0,
            avg_font_size=14.0,
            reading_order=3,
            heading_level=2,
        ),
        _block(
            block_id="numbers",
            block_type="paragraph",
            text="4 6",
            top=150.0,
            bottom=160.0,
            avg_font_size=12.0,
            reading_order=4,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph", "paragraph"]
    assert [block.text for block in resolved.blocks] == [
        "Table of Contents",
        "Executive Summary 4",
        "Legal Framework 6",
    ]


def test_resolve_document_merges_single_page_number_to_last_toc_entry():
    resolver = LocalPdfTocResolver()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="toc",
            block_type="heading",
            text="Contents",
            top=24.0,
            bottom=42.0,
            avg_font_size=18.0,
            reading_order=1,
            heading_level=1,
        ),
        _block(
            block_id="entry",
            block_type="heading",
            text="1. Front Matter",
            top=100.0,
            bottom=114.0,
            avg_font_size=13.0,
            reading_order=2,
            heading_level=2,
        ),
        _block(
            block_id="page_number",
            block_type="paragraph",
            text="1",
            top=118.0,
            bottom=126.0,
            avg_font_size=12.0,
            reading_order=3,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph"]
    assert resolved.blocks[1].text == "1. Front Matter 1"


def test_resolve_document_leaves_non_toc_pages_unchanged():
    resolver = LocalPdfTocResolver()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Introduction",
            top=40.0,
            bottom=58.0,
            avg_font_size=18.0,
            reading_order=1,
            heading_level=1,
        ),
        _block(
            block_id="section",
            block_type="heading",
            text="Background",
            top=120.0,
            bottom=136.0,
            avg_font_size=14.0,
            reading_order=2,
            heading_level=2,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading"]
    assert [block.text for block in resolved.blocks] == ["Introduction", "Background"]
