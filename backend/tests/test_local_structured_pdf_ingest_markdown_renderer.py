from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfIngestMarkdownRenderer,
    PdfBBox,
    PdfSemanticBlock,
    PdfStructuredDocument,
)


def test_render_document_outputs_natural_markdown_for_structured_blocks():
    renderer = LocalPdfIngestMarkdownRenderer()
    document = PdfStructuredDocument(
        pages=[],
        blocks=[
            PdfSemanticBlock(
                block_id="h1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Methods",
                bbox=PdfBBox(x0=0.0, top=0.0, x1=10.0, bottom=10.0),
                line_ids=["l1"],
                heading_level=2,
            ),
            PdfSemanticBlock(
                block_id="p1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="First line\nsecond line",
                bbox=PdfBBox(x0=0.0, top=12.0, x1=10.0, bottom=20.0),
                line_ids=["l2", "l3"],
            ),
            PdfSemanticBlock(
                block_id="eq1",
                block_type="equation",
                page_start=1,
                page_end=1,
                text=r"E = mc^2",
                bbox=PdfBBox(x0=0.0, top=22.0, x1=10.0, bottom=30.0),
                line_ids=["l4"],
            ),
            PdfSemanticBlock(
                block_id="c1",
                block_type="caption",
                page_start=1,
                page_end=1,
                text="Figure 1. Model overview.",
                bbox=PdfBBox(x0=0.0, top=32.0, x1=10.0, bottom=40.0),
                line_ids=["l5"],
            ),
            PdfSemanticBlock(
                block_id="f1",
                block_type="footnote",
                page_start=1,
                page_end=1,
                text="1 Source: Author calculation.",
                bbox=PdfBBox(x0=0.0, top=42.0, x1=10.0, bottom=50.0),
                line_ids=["l6"],
            ),
        ],
    )

    rendered = renderer.render_document(document=document)

    assert rendered.markdown == (
        "## Methods\n\n"
        "First line second line\n\n"
        "$$\nE = mc^2\n$$\n\n"
        "Figure 1. Model overview.\n\n"
        "1 Source: Author calculation."
    )
    spans = [span.to_dict() for span in rendered.spans]
    assert [span["block_id"] for span in spans] == ["h1", "p1", "eq1", "c1", "f1"]
    assert [span["block_type"] for span in spans] == ["heading", "paragraph", "equation", "caption", "footnote"]
    assert all(span["page_start"] == 1 and span["page_end"] == 1 for span in spans)
    assert [rendered.markdown[span["start_char"]:span["end_char"]] for span in spans] == [
        "## Methods",
        "First line second line",
        "$$\nE = mc^2\n$$",
        "Figure 1. Model overview.",
        "1 Source: Author calculation.",
    ]


def test_render_document_keeps_table_and_list_items_stable():
    renderer = LocalPdfIngestMarkdownRenderer()
    document = PdfStructuredDocument(
        pages=[],
        blocks=[
            PdfSemanticBlock(
                block_id="t1",
                block_type="table",
                page_start=1,
                page_end=1,
                text="",
                bbox=PdfBBox(x0=0.0, top=0.0, x1=10.0, bottom=10.0),
                line_ids=["l1", "l2"],
                table_rows=[["Name", "Score"], ["Alice", "95"]],
            ),
            PdfSemanticBlock(
                block_id="l1",
                block_type="list_item",
                page_start=1,
                page_end=1,
                text="First item",
                bbox=PdfBBox(x0=0.0, top=12.0, x1=10.0, bottom=20.0),
                line_ids=["l3"],
            ),
        ],
    )

    rendered = renderer.render_document(document=document)

    assert rendered.markdown == "| Name | Score |\n| --- | --- |\n| Alice | 95 |\n\n- First item"
    spans = [span.to_dict() for span in rendered.spans]
    assert [span["block_id"] for span in spans] == ["t1", "l1"]
    assert [span["block_type"] for span in spans] == ["table", "list_item"]
    assert [rendered.markdown[span["start_char"]:span["end_char"]] for span in spans] == [
        "| Name | Score |\n| --- | --- |\n| Alice | 95 |",
        "- First item",
    ]
