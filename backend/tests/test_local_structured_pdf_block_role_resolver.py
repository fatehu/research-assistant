from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfBlockRoleResolver,
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


def test_resolve_document_normalizes_directory_like_page():
    resolver = LocalPdfBlockRoleResolver()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Overview",
            top=30.0,
            bottom=48.0,
            avg_font_size=20.0,
            reading_order=1,
            heading_level=1,
        ),
        _block(
            block_id="entry_1",
            block_type="heading",
            text="Executive Summary",
            top=96.0,
            bottom=112.0,
            avg_font_size=14.0,
            reading_order=2,
            heading_level=2,
        ),
        _block(
            block_id="entry_2",
            block_type="heading",
            text="Legal Framework",
            top=122.0,
            bottom=138.0,
            avg_font_size=14.0,
            reading_order=3,
            heading_level=2,
        ),
        _block(
            block_id="entry_3",
            block_type="heading",
            text="Election Administration",
            top=148.0,
            bottom=164.0,
            avg_font_size=14.0,
            reading_order=4,
            heading_level=2,
        ),
        _block(
            block_id="numbers",
            block_type="paragraph",
            text="4 6 11",
            top=170.0,
            bottom=180.0,
            avg_font_size=12.0,
            reading_order=5,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph", "paragraph", "paragraph"]
    assert [block.text for block in resolved.blocks] == [
        "Overview",
        "Executive Summary 4",
        "Legal Framework 6",
        "Election Administration 11",
    ]


def test_resolve_document_normalizes_dot_leader_entry_page_without_number_only_block():
    resolver = LocalPdfBlockRoleResolver()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Contents",
            top=28.0,
            bottom=44.0,
            avg_font_size=18.0,
            reading_order=1,
            heading_level=1,
        ),
        _block(
            block_id="entry_1",
            block_type="heading",
            text="1. First Chapter.................................1",
            top=100.0,
            bottom=114.0,
            avg_font_size=13.0,
            reading_order=2,
            heading_level=2,
        ),
        _block(
            block_id="entry_2",
            block_type="heading",
            text="2. Second Chapter................................5",
            top=124.0,
            bottom=138.0,
            avg_font_size=13.0,
            reading_order=3,
            heading_level=2,
        ),
        _block(
            block_id="entry_3",
            block_type="heading",
            text="3. Third Chapter.................................9",
            top=148.0,
            bottom=162.0,
            avg_font_size=13.0,
            reading_order=4,
            heading_level=2,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph", "paragraph", "paragraph"]
    assert resolved.blocks[1].text.endswith("1")
    assert resolved.blocks[2].text.endswith("5")
    assert resolved.blocks[3].text.endswith("9")


def test_resolve_document_leaves_non_directory_page_unchanged():
    resolver = LocalPdfBlockRoleResolver()
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
        _block(
            block_id="body",
            block_type="paragraph",
            text="This section introduces the study and explains the historical context in detail.",
            top=148.0,
            bottom=180.0,
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

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "paragraph"]
    assert [block.text for block in resolved.blocks] == [
        "Introduction",
        "Background",
        "This section introduces the study and explains the historical context in detail.",
    ]
