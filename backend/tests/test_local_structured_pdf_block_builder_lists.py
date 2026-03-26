from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfBlockBuilder,
    PdfBBox,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
)


def _resolved_line(
    *,
    page: int,
    line_id: str,
    text: str,
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    reading_order: int,
    avg_font_size: float = 12.0,
) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=avg_font_size,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id="main",
        reading_order=reading_order,
    )


def test_build_document_recovers_list_items_separately_from_paragraphs():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="Lead paragraph text",
                        x0=60.0,
                        top=100.0,
                        x1=220.0,
                        bottom=114.0,
                        reading_order=1,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="li1",
                        text="1. First item",
                        x0=60.0,
                        top=140.0,
                        x1=200.0,
                        bottom=154.0,
                        reading_order=2,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="li1c",
                        text="continued explanation",
                        x0=84.0,
                        top=156.0,
                        x1=250.0,
                        bottom=170.0,
                        reading_order=3,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="li2",
                        text="2. Second item",
                        x0=60.0,
                        top=180.0,
                        x1=210.0,
                        bottom=194.0,
                        reading_order=4,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == [
        "paragraph",
        "list_item",
        "list_item",
    ]
    assert structured.blocks[1].text == "1. First item\ncontinued explanation"
