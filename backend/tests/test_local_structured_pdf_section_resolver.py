from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfBlockBuilder,
    LocalPdfSectionResolver,
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
    band: str = "body",
) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=avg_font_size,
        dominant_font_name="Times",
        band=band,
        region="main",
        column_id="main",
        reading_order=reading_order,
    )


def test_resolve_document_inherits_section_context_on_next_page_without_heading():
    builder = LocalPdfBlockBuilder()
    resolver = LocalPdfSectionResolver()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="Introduction",
                        x0=60.0,
                        top=40.0,
                        x1=220.0,
                        bottom=58.0,
                        reading_order=1,
                        avg_font_size=14.0,
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="First page body line",
                        x0=60.0,
                        top=120.0,
                        x1=240.0,
                        bottom=134.0,
                        reading_order=2,
                    ),
                ],
            ),
            PdfResolvedPage(
                meta=PdfPageMeta(page=2, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=2,
                        line_id="p2",
                        text="Second page continues the section",
                        x0=60.0,
                        top=80.0,
                        x1=300.0,
                        bottom=94.0,
                        reading_order=1,
                    ),
                ],
            ),
        ]
    )

    structured = resolver.resolve_document(document=builder.build_document(document=document))

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph", "paragraph"]
    assert structured.blocks[0].section_titles == ["Introduction"]
    assert structured.blocks[1].section_titles == ["Introduction"]
    assert structured.blocks[2].section_titles == ["Introduction"]
    assert structured.blocks[2].section_path == "Introduction"
    assert structured.blocks[2].parent_heading_id == structured.blocks[0].block_id


def test_resolve_document_builds_nested_section_stack_from_heading_levels():
    builder = LocalPdfBlockBuilder()
    resolver = LocalPdfSectionResolver()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="Methods",
                        x0=60.0,
                        top=40.0,
                        x1=200.0,
                        bottom=58.0,
                        reading_order=1,
                        avg_font_size=16.0,
                        band="top_band",
                    ),
                ],
            ),
            PdfResolvedPage(
                meta=PdfPageMeta(page=2, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=2,
                        line_id="h2",
                        text="2.1 Dataset",
                        x0=60.0,
                        top=40.0,
                        x1=220.0,
                        bottom=56.0,
                        reading_order=1,
                        avg_font_size=14.0,
                        band="top_band",
                    ),
                    _resolved_line(
                        page=2,
                        line_id="p2",
                        text="Dataset paragraph under subsection",
                        x0=60.0,
                        top=100.0,
                        x1=320.0,
                        bottom=114.0,
                        reading_order=2,
                    ),
                ],
            ),
        ]
    )

    structured = resolver.resolve_document(document=builder.build_document(document=document))

    assert [block.heading_level for block in structured.blocks if block.block_type == "heading"] == [1, 2]
    assert structured.blocks[1].parent_heading_id == structured.blocks[0].block_id
    assert structured.blocks[1].section_titles == ["Methods", "2.1 Dataset"]
    assert structured.blocks[2].section_titles == ["Methods", "2.1 Dataset"]
    assert structured.blocks[2].section_path == "Methods > 2.1 Dataset"
