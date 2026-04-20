from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfHeadingRefiner,
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
    region: str = "main",
    reading_order: int = 1,
    x0: float = 60.0,
    x1: float = 320.0,
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
        region=region,
        reading_order_start=reading_order,
        reading_order_end=reading_order,
    )


def test_resolve_document_promotes_colon_section_paragraphs():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Microscope Basics",
            top=40.0,
            bottom=60.0,
            avg_font_size=18.0,
            reading_order=1,
        ),
        _block(
            block_id="section",
            block_type="paragraph",
            text="Changing objectives:",
            top=180.0,
            bottom=194.0,
            avg_font_size=12.0,
            reading_order=2,
        ),
        _block(
            block_id="body",
            block_type="list_item",
            text="1. The field of view decreases",
            top=210.0,
            bottom=224.0,
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

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "list_item"]
    assert resolved.blocks[1].heading_level == 2


def test_resolve_document_promotes_parallel_short_title_band():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=600.0, rotation=0)
    blocks = [
        _block(
            block_id="hero",
            block_type="heading",
            text="Upstage aims to enrich your business",
            top=40.0,
            bottom=60.0,
            avg_font_size=22.0,
            reading_order=1,
        ),
        _block(
            block_id="left",
            block_type="paragraph",
            text="Our Purpose",
            top=230.0,
            bottom=244.0,
            avg_font_size=13.0,
            column_id="left",
            region="left_column",
            reading_order=2,
            x0=60.0,
            x1=180.0,
        ),
        _block(
            block_id="right",
            block_type="paragraph",
            text="Our Mission",
            top=230.0,
            bottom=244.0,
            avg_font_size=13.0,
            column_id="right",
            region="right_column",
            reading_order=3,
            x0=300.0,
            x1=420.0,
        ),
        _block(
            block_id="left_body",
            block_type="paragraph",
            text="Making AI beneficial for everyone",
            top=260.0,
            bottom=280.0,
            avg_font_size=18.0,
            column_id="left",
            region="left_column",
            reading_order=4,
            x0=60.0,
            x1=280.0,
        ),
        _block(
            block_id="right_body",
            block_type="paragraph",
            text="Deploying AI solutions everywhere",
            top=260.0,
            bottom=280.0,
            avg_font_size=18.0,
            column_id="right",
            region="right_column",
            reading_order=5,
            x0=300.0,
            x1=520.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=12.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "heading", "paragraph", "paragraph"]
    assert [block.heading_level for block in resolved.blocks[1:3]] == [2, 2]


def test_resolve_document_promotes_large_title_before_table():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=900.0, page_height=800.0, rotation=0)
    blocks = [
        _block(
            block_id="intro",
            block_type="heading",
            text="Introduction of product services and key features",
            top=30.0,
            bottom=50.0,
            avg_font_size=20.0,
            reading_order=1,
        ),
        _block(
            block_id="table_title",
            block_type="paragraph",
            text="Key Functions by Main Service Flow",
            top=70.0,
            bottom=88.0,
            avg_font_size=17.0,
            reading_order=2,
        ),
        _block(
            block_id="table",
            block_type="table",
            text="Service Stage Function Name",
            top=96.0,
            bottom=200.0,
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

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "table"]
    assert resolved.blocks[1].heading_level == 2


def test_resolve_document_does_not_promote_unanchored_backend_paragraph():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=1000.0, page_height=2000.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="TOP TITLE",
            top=30.0,
            bottom=80.0,
            avg_font_size=24.0,
            reading_order=1,
        ),
        PdfSemanticBlock(
            block_id="ocr_band",
            block_type="paragraph",
            page_start=1,
            page_end=1,
            text="COPYRIGHT PROTECTS CREATIVE WORK - YOURS, MINE, EVERYONE'S!",
            bbox=PdfBBox(x0=70.0, top=260.0, x1=800.0, bottom=320.0),
            line_ids=[],
            avg_font_size=0.0,
            column_id="main",
            region="main",
            reading_order_start=2,
            reading_order_end=2,
        ),
        _block(
            block_id="body",
            block_type="paragraph",
            text="We're all both consumers and creators of creative work.",
            top=360.0,
            bottom=390.0,
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

    assert [block.block_type for block in resolved.blocks] == ["heading", "paragraph", "paragraph"]


def test_resolve_document_promotes_first_page_section_band_after_author():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="chapter",
            block_type="heading",
            text="CHAPTER 7.",
            top=36.0,
            bottom=52.0,
            avg_font_size=16.0,
            reading_order=1,
            x0=120.0,
            x1=280.0,
        ),
        _block(
            block_id="state",
            block_type="heading",
            text="TEXAS",
            top=70.0,
            bottom=88.0,
            avg_font_size=18.0,
            reading_order=2,
            x0=120.0,
            x1=260.0,
        ),
        _block(
            block_id="author",
            block_type="paragraph",
            text="MICHELLE REED",
            top=108.0,
            bottom=120.0,
            avg_font_size=11.8,
            reading_order=3,
            x0=120.0,
            x1=280.0,
        ),
        _block(
            block_id="section",
            block_type="paragraph",
            text="COURSE MARKING DRIVERS",
            top=142.0,
            bottom=156.0,
            avg_font_size=11.0,
            reading_order=4,
            x0=120.0,
            x1=380.0,
        ),
        _block(
            block_id="body",
            block_type="paragraph",
            text="SB1359 was passed in September 2016 and required institutions to mark free digital course materials for students.",
            top=176.0,
            bottom=230.0,
            avg_font_size=11.0,
            reading_order=5,
            x0=80.0,
            x1=640.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=11.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["heading", "heading", "paragraph", "heading", "paragraph"]
    assert resolved.blocks[3].heading_level == 2


def test_resolve_document_promotes_uppercase_section_band_before_body():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=2, page_width=780.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="intro",
            block_type="paragraph",
            text="This book's approach is premised on a simple assumption about how students should test and learn.",
            top=60.0,
            bottom=118.0,
            avg_font_size=10.5,
            reading_order=1,
            x0=60.0,
            x1=640.0,
        ),
        _block(
            block_id="section",
            block_type="paragraph",
            text="HOMO ECONOMICUS VS. HOMO SAPIENS",
            top=154.0,
            bottom=168.0,
            avg_font_size=10.5,
            reading_order=2,
            x0=70.0,
            x1=420.0,
        ),
        _block(
            block_id="body",
            block_type="paragraph",
            text="For ease of reference and exposition, we refer to the traditional rational-choice model as Homo economicus.",
            top=190.0,
            bottom=246.0,
            avg_font_size=10.5,
            reading_order=3,
            x0=60.0,
            x1=650.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=10.5,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["paragraph", "heading", "paragraph"]
    assert resolved.blocks[1].heading_level == 2


def test_resolve_document_promotes_parallel_panel_headings():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=900.0, page_height=700.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="heading",
            text="Overview of OCR Pack",
            top=28.0,
            bottom=40.0,
            avg_font_size=11.0,
            column_id="left",
            reading_order=1,
            x0=40.0,
            x1=180.0,
        ),
        _block(
            block_id="hero",
            block_type="heading",
            text="Base Model Performance Evaluation of Upstage OCR Pack",
            top=50.0,
            bottom=68.0,
            avg_font_size=17.0,
            reading_order=2,
            x0=40.0,
            x1=520.0,
        ),
        _block(
            block_id="left_panel",
            block_type="paragraph",
            text="Upstage universal OCR model E2E performance\nevaluation 1",
            top=122.0,
            bottom=154.0,
            avg_font_size=10.0,
            column_id="left",
            reading_order=3,
            x0=40.0,
            x1=280.0,
        ),
        _block(
            block_id="right_panel",
            block_type="paragraph",
            text="Upstage universal OCR model performance details: Document\ncriteria",
            top=122.0,
            bottom=154.0,
            avg_font_size=11.0,
            column_id="right",
            reading_order=4,
            x0=360.0,
            x1=700.0,
        ),
        _block(
            block_id="left_metric",
            block_type="paragraph",
            text="100",
            top=176.0,
            bottom=182.0,
            avg_font_size=5.0,
            column_id="left",
            reading_order=5,
            x0=60.0,
            x1=80.0,
        ),
        _block(
            block_id="right_metric",
            block_type="paragraph",
            text="OCR-Recall 3",
            top=180.0,
            bottom=188.0,
            avg_font_size=5.3,
            column_id="right",
            reading_order=6,
            x0=380.0,
            x1=460.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=5.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks[:4]] == ["heading", "heading", "heading", "heading"]


def test_resolve_document_promotes_and_merges_heading_continuation():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=600.0, rotation=0)
    blocks = [
        _block(
            block_id="hero",
            block_type="heading",
            text="Upstage aims to enrich your business by providing",
            top=50.0,
            bottom=76.0,
            avg_font_size=22.0,
            reading_order=1,
            x0=70.0,
            x1=520.0,
        ),
        _block(
            block_id="hero_tail",
            block_type="paragraph",
            text="Easy-to-Apply AI solutions",
            top=84.0,
            bottom=106.0,
            avg_font_size=22.0,
            column_id="left",
            region="left_column",
            reading_order=2,
            x0=70.0,
            x1=300.0,
        ),
        _block(
            block_id="body",
            block_type="paragraph",
            text="Practical AI services for enterprise teams",
            top=180.0,
            bottom=198.0,
            avg_font_size=13.0,
            reading_order=3,
            x0=70.0,
            x1=420.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=13.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert len(resolved.blocks) == 2
    assert resolved.blocks[0].block_type == "heading"
    assert (
        resolved.blocks[0].text
        == "Upstage aims to enrich your business by providing\nEasy-to-Apply AI solutions"
    )


def test_resolve_document_does_not_promote_chart_legend_labels():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=700.0, rotation=0)
    blocks = [
        _block(
            block_id="figure_caption",
            block_type="caption",
            text="Figure 9.4.1: Challenges in importing",
            top=60.0,
            bottom=74.0,
            avg_font_size=9.5,
            reading_order=1,
            x0=40.0,
            x1=420.0,
        ),
        _block(
            block_id="legend_a",
            block_type="paragraph",
            text="Big Challenge",
            top=260.0,
            bottom=268.0,
            avg_font_size=6.8,
            column_id="right",
            reading_order=2,
            x0=420.0,
            x1=520.0,
        ),
        _block(
            block_id="legend_b",
            block_type="paragraph",
            text="Small Challenge",
            top=260.0,
            bottom=268.0,
            avg_font_size=6.8,
            column_id="right",
            reading_order=3,
            x0=540.0,
            x1=660.0,
        ),
        _block(
            block_id="legend_c",
            block_type="paragraph",
            text="No Challenge",
            top=260.0,
            bottom=268.0,
            avg_font_size=6.8,
            column_id="right",
            reading_order=4,
            x0=680.0,
            x1=780.0,
        ),
        _block(
            block_id="section",
            block_type="paragraph",
            text="9.5. Adapting to the New Normal: Changing Business Models",
            top=352.0,
            bottom=374.0,
            avg_font_size=9.5,
            reading_order=5,
            x0=60.0,
            x1=560.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=6.8,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["caption", "paragraph", "paragraph", "paragraph", "paragraph"]


def test_resolve_document_does_not_promote_table_header_band():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=900.0, page_height=700.0, rotation=0)
    blocks = [
        _block(
            block_id="hdr1",
            block_type="paragraph",
            text="Properties",
            top=82.0,
            bottom=90.0,
            avg_font_size=7.2,
            column_id="left",
            reading_order=1,
            x0=40.0,
            x1=140.0,
        ),
        _block(
            block_id="hdr2",
            block_type="paragraph",
            text="Instruction",
            top=82.0,
            bottom=90.0,
            avg_font_size=7.2,
            column_id="main",
            reading_order=2,
            x0=220.0,
            x1=340.0,
        ),
        _block(
            block_id="hdr3",
            block_type="paragraph",
            text="Alignment",
            top=82.0,
            bottom=90.0,
            avg_font_size=7.2,
            column_id="right",
            reading_order=3,
            x0=540.0,
            x1=660.0,
        ),
        _block(
            block_id="row",
            block_type="paragraph",
            text="Alpaca-GPT4 OpenOrca Synth. Math-Instruct Orca DPO Pairs",
            top=95.0,
            bottom=103.0,
            avg_font_size=7.2,
            column_id="main",
            reading_order=4,
            x0=220.0,
            x1=760.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=7.2,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["paragraph", "paragraph", "paragraph", "paragraph"]


def test_resolve_document_does_not_promote_top_title_with_doi_meta_peer():
    resolver = LocalPdfHeadingRefiner()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=900.0, rotation=0)
    blocks = [
        _block(
            block_id="title",
            block_type="paragraph",
            text="Combinatorial Cosmology",
            top=20.0,
            bottom=30.0,
            avg_font_size=9.0,
            column_id="left",
            reading_order=1,
            x0=60.0,
            x1=240.0,
        ),
        _block(
            block_id="doi",
            block_type="paragraph",
            text="DOI: http://dx.doi.org/10.5772/intechopen.90696",
            top=32.0,
            bottom=42.0,
            avg_font_size=9.0,
            column_id="right",
            reading_order=2,
            x0=360.0,
            x1=760.0,
        ),
        _block(
            block_id="section",
            block_type="heading",
            text="5. The dynamics",
            top=56.0,
            bottom=68.0,
            avg_font_size=11.0,
            column_id="left",
            reading_order=3,
            x0=60.0,
            x1=220.0,
        ),
    ]
    document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=blocks)],
        blocks=blocks,
        body_font_size=10.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["paragraph", "paragraph", "heading"]
