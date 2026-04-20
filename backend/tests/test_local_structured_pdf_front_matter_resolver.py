from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfFrontMatterResolver,
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
    heading_level: int | None = None,
    reading_order_start: int = 1,
    reading_order_end: int | None = None,
) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=1,
        page_end=1,
        text=text,
        bbox=PdfBBox(x0=60.0, top=top, x1=520.0, bottom=bottom),
        line_ids=[f"{block_id}:l1"],
        avg_font_size=14.0 if block_type == "heading" else 11.5,
        reading_order_start=reading_order_start,
        reading_order_end=reading_order_end or reading_order_start,
        heading_level=heading_level,
    )


def test_resolve_document_demotes_front_matter_heading_blocks_before_abstract():
    resolver = LocalPdfFrontMatterResolver()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="SOLAR 10.7B: Scaling Large Language Models",
            top=40.0,
            bottom=64.0,
            heading_level=1,
            reading_order_start=1,
        ),
        _block(
            block_id="authors",
            block_type="heading",
            text="Dahyun Kim, Chanjun Park, Sanghoon Kim, Wonsung Lee",
            top=86.0,
            bottom=102.0,
            heading_level=2,
            reading_order_start=2,
        ),
        _block(
            block_id="affiliation",
            block_type="heading",
            text="Upstage AI, South Korea",
            top=108.0,
            bottom=124.0,
            heading_level=2,
            reading_order_start=3,
        ),
        _block(
            block_id="email",
            block_type="heading",
            text="{authors}@upstage.ai",
            top=128.0,
            bottom=144.0,
            heading_level=2,
            reading_order_start=4,
        ),
        _block(
            block_id="abstract",
            block_type="heading",
            text="Abstract",
            top=168.0,
            bottom=184.0,
            heading_level=2,
            reading_order_start=5,
        ),
        _block(
            block_id="intro",
            block_type="heading",
            text="1 Introduction",
            top=240.0,
            bottom=256.0,
            heading_level=2,
            reading_order_start=6,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=11.5,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "paragraph",
        "heading",
        "heading",
    ]
    assert resolved.blocks[0].heading_level == 1
    assert resolved.blocks[1].heading_level is None
    assert resolved.blocks[4].text == "Abstract"
    assert resolved.blocks[5].text == "1 Introduction"


def test_resolve_document_keeps_non_front_matter_headings_after_section_start():
    resolver = LocalPdfFrontMatterResolver()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Document Title",
            top=40.0,
            bottom=60.0,
            heading_level=1,
            reading_order_start=1,
        ),
        _block(
            block_id="abstract",
            block_type="heading",
            text="Abstract",
            top=100.0,
            bottom=116.0,
            heading_level=2,
            reading_order_start=2,
        ),
        _block(
            block_id="method",
            block_type="heading",
            text="Method Overview",
            top=180.0,
            bottom=196.0,
            heading_level=2,
            reading_order_start=3,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=11.5,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "heading"]


def test_resolve_document_demotes_title_adjacent_name_band_without_keyword_trigger():
    resolver = LocalPdfFrontMatterResolver()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    blocks = [
        PdfSemanticBlock(
            block_id="title",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="A Practical Layout Parser",
            bbox=PdfBBox(x0=80.0, top=40.0, x1=520.0, bottom=66.0),
            line_ids=["title:l1"],
            avg_font_size=20.0,
            reading_order_start=1,
            reading_order_end=1,
            heading_level=1,
        ),
        PdfSemanticBlock(
            block_id="authors",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="John Smith Jane Doe Alan Turing",
            bbox=PdfBBox(x0=150.0, top=84.0, x1=450.0, bottom=100.0),
            line_ids=["authors:l1"],
            avg_font_size=14.0,
            reading_order_start=2,
            reading_order_end=2,
            heading_level=2,
        ),
        PdfSemanticBlock(
            block_id="contact",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="contact@example.org",
            bbox=PdfBBox(x0=170.0, top=108.0, x1=430.0, bottom=122.0),
            line_ids=["contact:l1"],
            avg_font_size=12.0,
            reading_order_start=3,
            reading_order_end=3,
            heading_level=2,
        ),
        PdfSemanticBlock(
            block_id="abstract",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="Abstract",
            bbox=PdfBBox(x0=60.0, top=160.0, x1=180.0, bottom=176.0),
            line_ids=["abstract:l1"],
            avg_font_size=13.0,
            reading_order_start=4,
            reading_order_end=4,
            heading_level=2,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=11.5,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph", "paragraph", "heading"]


def test_resolve_document_reorders_first_page_front_matter_before_early_section_heading():
    resolver = LocalPdfFrontMatterResolver()
    page_meta = PdfPageMeta(page=1, page_width=612.0, page_height=792.0, rotation=0)
    blocks = [
        PdfSemanticBlock(
            block_id="intro",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="1 Introduction",
            bbox=PdfBBox(x0=110.9, top=415.1, x1=246.8, bottom=432.3),
            line_ids=["intro:l1"],
            avg_font_size=17.2,
            reading_order_start=1,
            reading_order_end=1,
            heading_level=2,
        ),
        PdfSemanticBlock(
            block_id="trailing_abs",
            block_type="paragraph",
            page_start=1,
            page_end=1,
            text="state.",
            bbox=PdfBBox(x0=140.2, top=356.3, x1=166.1, bottom=367.2),
            line_ids=["abs:l3"],
            avg_font_size=10.9,
            reading_order_start=2,
            reading_order_end=2,
        ),
        PdfSemanticBlock(
            block_id="title_1",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="Py-Calabi quasi-morphisms and quasi-states",
            bbox=PdfBBox(x0=121.6, top=169.7, x1=488.6, bottom=190.3),
            line_ids=["title:l1"],
            avg_font_size=20.7,
            reading_order_start=3,
            reading_order_end=3,
            heading_level=1,
        ),
        PdfSemanticBlock(
            block_id="title_2",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="on orientable surfaces of higher genus",
            bbox=PdfBBox(x0=150.2, top=194.5, x1=460.1, bottom=215.2),
            line_ids=["title:l2"],
            avg_font_size=20.7,
            reading_order_start=4,
            reading_order_end=4,
            heading_level=1,
        ),
        PdfSemanticBlock(
            block_id="author",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="Maor Rosenberg October 30, 2018",
            bbox=PdfBBox(x0=252.6, top=235.7, x1=357.8, bottom=278.2),
            line_ids=["author:l1"],
            avg_font_size=14.3,
            reading_order_start=5,
            reading_order_end=5,
            heading_level=3,
        ),
        PdfSemanticBlock(
            block_id="abstract",
            block_type="paragraph",
            page_start=1,
            page_end=1,
            text="Abstract",
            bbox=PdfBBox(x0=281.2, top=300.4, x1=329.1, bottom=311.4),
            line_ids=["abstract:l1"],
            avg_font_size=10.9,
            reading_order_start=6,
            reading_order_end=6,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=10.9,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.text for block in resolved.blocks[:6]] == [
        "Py-Calabi quasi-morphisms and quasi-states",
        "on orientable surfaces of higher genus",
        "Maor Rosenberg October 30, 2018",
        "Abstract",
        "state.",
        "1 Introduction",
    ]
    assert resolved.blocks[0].block_type == "heading"
    assert resolved.blocks[2].block_type == "paragraph"


def test_resolve_document_does_not_reorder_appendix_style_content_as_front_matter():
    resolver = LocalPdfFrontMatterResolver()
    page_meta = PdfPageMeta(page=1, page_width=612.0, page_height=792.0, rotation=0)
    blocks = [
        PdfSemanticBlock(
            block_id="appendix_title",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="A Contributions",
            bbox=PdfBBox(x0=70.9, top=72.4, x1=220.0, bottom=90.0),
            line_ids=["appendix_title:l1"],
            avg_font_size=17.0,
            reading_order_start=1,
            reading_order_end=1,
            heading_level=2,
        ),
        PdfSemanticBlock(
            block_id="lead",
            block_type="paragraph",
            page_start=1,
            page_end=1,
            text="The contributions of this study are as follows:",
            bbox=PdfBBox(x0=70.5, top=94.3, x1=320.0, bottom=108.0),
            line_ids=["lead:l1"],
            avg_font_size=11.5,
            reading_order_start=2,
            reading_order_end=2,
        ),
        PdfSemanticBlock(
            block_id="list_item",
            block_type="list_item",
            page_start=1,
            page_end=1,
            text="• Introduction of the SOLAR 10.7 Billion-Parameter Model",
            bbox=PdfBBox(x0=83.9, top=116.9, x1=360.0, bottom=150.0),
            line_ids=["list:l1"],
            avg_font_size=11.5,
            reading_order_start=3,
            reading_order_end=3,
        ),
        PdfSemanticBlock(
            block_id="right_column_tail",
            block_type="paragraph",
            page_start=1,
            page_end=1,
            text="ability for In-context learning, including Zero-shot learning.",
            bbox=PdfBBox(x0=335.0, top=73.3, x1=560.0, bottom=110.0),
            line_ids=["right:l1"],
            avg_font_size=11.5,
            reading_order_start=44,
            reading_order_end=44,
        ),
        PdfSemanticBlock(
            block_id="late_section",
            block_type="heading",
            page_start=1,
            page_end=1,
            text="B.2 Mixture of Experts",
            bbox=PdfBBox(x0=70.9, top=182.0, x1=270.0, bottom=198.0),
            line_ids=["late_section:l1"],
            avg_font_size=15.0,
            reading_order_start=51,
            reading_order_end=51,
            heading_level=2,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=11.5,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_id for block in resolved.blocks] == [
        "appendix_title",
        "lead",
        "list_item",
        "right_column_tail",
        "late_section",
    ]
