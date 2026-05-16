from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfMarkdownRenderer,
    PdfBBox,
    PdfSemanticBlock,
    PdfStructuredDocument,
)


def test_render_document_outputs_headings_and_flattened_paragraphs():
    renderer = LocalPdfMarkdownRenderer()
    document = PdfStructuredDocument(
        pages=[],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Methods",
                bbox=PdfBBox(x0=0.0, top=0.0, x1=10.0, bottom=10.0),
                line_ids=["l1"],
                heading_level=2,
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="First line\nsecond line",
                bbox=PdfBBox(x0=0.0, top=12.0, x1=10.0, bottom=20.0),
                line_ids=["l2", "l3"],
            ),
        ],
    )

    markdown = renderer.render_document(document=document)

    assert markdown == "## Methods\n\nFirst line second line"


def test_render_document_outputs_table_and_list_items():
    renderer = LocalPdfMarkdownRenderer()
    document = PdfStructuredDocument(
        pages=[],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="table",
                page_start=1,
                page_end=1,
                text="",
                bbox=PdfBBox(x0=0.0, top=0.0, x1=10.0, bottom=10.0),
                line_ids=["l1", "l2"],
                table_rows=[["Name", "Score"], ["Alice", "95"]],
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="list_item",
                page_start=1,
                page_end=1,
                text="1. First item",
                bbox=PdfBBox(x0=0.0, top=12.0, x1=10.0, bottom=20.0),
                line_ids=["l3"],
            ),
        ],
    )

    markdown = renderer.render_document(document=document)

    assert markdown == "| Name | Score |\n| --- | --- |\n| Alice | 95 |\n\n1. First item"
