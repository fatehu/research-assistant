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
    dominant_font_name: str = "Times",
    column_id: str = "main",
    region: str = "main",
    band: str = "body",
) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=avg_font_size,
        dominant_font_name=dominant_font_name,
        band=band,
        region=region,
        column_id=column_id,
        reading_order=reading_order,
    )


def test_build_document_recovers_heading_and_paragraph_blocks():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Paper Title",
                        x0=120.0,
                        top=40.0,
                        x1=480.0,
                        bottom=64.0,
                        reading_order=1,
                        avg_font_size=18.0,
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="This is the first paragraph line",
                        x0=60.0,
                        top=120.0,
                        x1=300.0,
                        bottom=134.0,
                        reading_order=2,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p2",
                        text="that continues on the next line",
                        x0=62.0,
                        top=136.0,
                        x1=302.0,
                        bottom=150.0,
                        reading_order=3,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h2",
                        text="1 Introduction",
                        x0=60.0,
                        top=188.0,
                        x1=220.0,
                        bottom=204.0,
                        reading_order=4,
                        avg_font_size=14.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p3",
                        text="A second paragraph starts here",
                        x0=60.0,
                        top=226.0,
                        x1=300.0,
                        bottom=240.0,
                        reading_order=5,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert round(structured.body_font_size, 2) == 12.0
    assert [block.block_type for block in structured.blocks] == [
        "heading",
        "paragraph",
        "heading",
        "paragraph",
    ]
    assert [block.heading_level for block in structured.blocks if block.block_type == "heading"] == [1, 2]
    assert structured.blocks[1].text == "This is the first paragraph line\nthat continues on the next line"
    assert structured.pages[0].blocks == structured.blocks


def test_build_document_splits_paragraphs_on_column_or_large_gap():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="l1",
                        text="Left paragraph line one",
                        x0=60.0,
                        top=140.0,
                        x1=220.0,
                        bottom=154.0,
                        reading_order=1,
                        column_id="left",
                        region="left_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="l2",
                        text="Left paragraph line two",
                        x0=62.0,
                        top=156.0,
                        x1=222.0,
                        bottom=170.0,
                        reading_order=2,
                        column_id="left",
                        region="left_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="r1",
                        text="Right paragraph line one",
                        x0=340.0,
                        top=140.0,
                        x1=500.0,
                        bottom=154.0,
                        reading_order=3,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body2",
                        text="Body after a large vertical gap",
                        x0=60.0,
                        top=240.0,
                        x1=260.0,
                        bottom=254.0,
                        reading_order=4,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == [
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert structured.blocks[0].line_ids == ["l1", "l2"]
    assert structured.blocks[1].line_ids == ["r1"]
    assert structured.blocks[2].line_ids == ["body2"]


def test_build_document_uses_numbering_depth_for_heading_levels():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Document Title",
                        x0=140.0,
                        top=42.0,
                        x1=460.0,
                        bottom=64.0,
                        reading_order=1,
                        avg_font_size=18.0,
                        dominant_font_name="Times-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="1 Introduction",
                        x0=60.0,
                        top=120.0,
                        x1=260.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h2",
                        text="1.1 Background",
                        x0=60.0,
                        top=154.0,
                        x1=260.0,
                        bottom=170.0,
                        reading_order=3,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="Body paragraph text starts here",
                        x0=60.0,
                        top=198.0,
                        x1=320.0,
                        bottom=212.0,
                        reading_order=4,
                        avg_font_size=12.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    heading_blocks = [block for block in structured.blocks if block.block_type == "heading"]
    assert [block.text for block in heading_blocks] == [
        "Document Title",
        "1 Introduction",
        "1.1 Background",
    ]
    assert [block.heading_level for block in heading_blocks] == [1, 2, 3]


def test_build_document_does_not_promote_table_like_rows_to_headings():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Ablation Results",
                        x0=140.0,
                        top=42.0,
                        x1=460.0,
                        bottom=64.0,
                        reading_order=1,
                        avg_font_size=18.0,
                        dominant_font_name="Times-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="row",
                        text="O 70.03 65.87 85.55 65.31 57.93 81.37 64.14",
                        x0=60.0,
                        top=120.0,
                        x1=420.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="The following paragraph explains the table.",
                        x0=60.0,
                        top=176.0,
                        x1=360.0,
                        bottom=190.0,
                        reading_order=3,
                        avg_font_size=12.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph", "paragraph"]
    assert structured.blocks[1].text == "O 70.03 65.87 85.55 65.31 57.93 81.37 64.14"


def test_build_document_promotes_short_top_title_by_context():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Print vs. Digital",
                        x0=180.0,
                        top=42.0,
                        x1=420.0,
                        bottom=58.0,
                        reading_order=1,
                        avg_font_size=11.5,
                        dominant_font_name="Montserrat-Regular",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="deck",
                        text="Why do some researchers abhor digital and favor print, or vice-versa? The classic print debate was necessary for us to understand reader preferences.",
                        x0=86.0,
                        top=72.0,
                        x1=514.0,
                        bottom=102.0,
                        reading_order=2,
                        avg_font_size=11.5,
                        dominant_font_name="Montserrat-Regular",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="The body paragraph starts here with regular text.",
                        x0=60.0,
                        top=136.0,
                        x1=320.0,
                        bottom=150.0,
                        reading_order=3,
                        avg_font_size=11.5,
                        dominant_font_name="Montserrat-Regular",
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert structured.blocks[0].block_type == "heading"
    assert structured.blocks[0].text == "Print vs. Digital"


def test_build_document_detects_appendix_style_headings_without_larger_font():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="p1",
                        text="The previous paragraph ends here with body copy.",
                        x0=60.0,
                        top=120.0,
                        x1=340.0,
                        bottom=134.0,
                        reading_order=1,
                        avg_font_size=10.9,
                        dominant_font_name="NimbusRomNo9L-Regular",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="B.3 Prompt Engineering",
                        x0=60.0,
                        top=164.0,
                        x1=230.0,
                        bottom=178.0,
                        reading_order=2,
                        avg_font_size=10.9,
                        dominant_font_name="NimbusRomNo9L-Medium",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="p2",
                        text="Prompt engineering studies how to design inputs that help models perform specific tasks.",
                        x0=60.0,
                        top=198.0,
                        x1=360.0,
                        bottom=212.0,
                        reading_order=3,
                        avg_font_size=10.9,
                        dominant_font_name="NimbusRomNo9L-Regular",
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["paragraph", "heading", "paragraph"]
    assert structured.blocks[1].heading_level == 2


def test_build_document_keeps_enumerated_reference_note_as_paragraph():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Fact-Checking 2",
                        x0=160.0,
                        top=56.0,
                        x1=420.0,
                        bottom=74.0,
                        reading_order=1,
                        avg_font_size=11.5,
                        dominant_font_name="Montserrat-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="Fact checkers verify that the names, dates, and facts in a work are correct.",
                        x0=60.0,
                        top=120.0,
                        x1=420.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=9.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="note1",
                        text="2. Content in this section is adapted from the Wikipedia",
                        x0=60.0,
                        top=510.0,
                        x1=420.0,
                        bottom=524.0,
                        reading_order=3,
                        avg_font_size=10.7,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="note2",
                        text="entry and is used under a CC BY-SA 3.0 license.",
                        x0=60.0,
                        top=526.0,
                        x1=420.0,
                        bottom=540.0,
                        reading_order=4,
                        avg_font_size=10.7,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph", "paragraph"]
    assert structured.blocks[2].text == (
        "2. Content in this section is adapted from the Wikipedia\n"
        "entry and is used under a CC BY-SA 3.0 license."
    )


def test_build_document_keeps_enumerated_source_table_reference_as_paragraph():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="False Causation",
                        x0=140.0,
                        top=56.0,
                        x1=420.0,
                        bottom=74.0,
                        reading_order=1,
                        avg_font_size=14.0,
                        dominant_font_name="Montserrat-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="Correlation does not imply causation.",
                        x0=60.0,
                        top=120.0,
                        x1=320.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=9.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="note1",
                        text="2. Statistics Canada. Table 37-10-0079-01 Registered apprenticeship",
                        x0=60.0,
                        top=510.0,
                        x1=420.0,
                        bottom=524.0,
                        reading_order=3,
                        avg_font_size=8.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="note2",
                        text="training, registrations by major trade groups and sex.",
                        x0=60.0,
                        top=526.0,
                        x1=420.0,
                        bottom=540.0,
                        reading_order=4,
                        avg_font_size=8.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph", "paragraph"]
    assert structured.blocks[2].text == (
        "2. Statistics Canada. Table 37-10-0079-01 Registered apprenticeship\n"
        "training, registrations by major trade groups and sex."
    )


def test_build_document_promotes_standalone_section_number_before_title():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="section_number",
                        text="2",
                        x0=210.0,
                        top=140.0,
                        x1=230.0,
                        bottom=170.0,
                        reading_order=1,
                        avg_font_size=30.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="The Lost Homeland",
                        x0=120.0,
                        top=184.0,
                        x1=410.0,
                        bottom=208.0,
                        reading_order=2,
                        avg_font_size=24.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="Since the death of my mother, I have been haunted by the desire to visit the homeland.",
                        x0=60.0,
                        top=250.0,
                        x1=480.0,
                        bottom=266.0,
                        reading_order=3,
                        avg_font_size=11.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body2",
                        text="I nevertheless hoped for the opportunity to do so, although I expected to find the names of the places changed.",
                        x0=60.0,
                        top=270.0,
                        x1=500.0,
                        bottom=286.0,
                        reading_order=4,
                        avg_font_size=11.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    heading_blocks = [block for block in structured.blocks if block.block_type == "heading"]
    assert [block.text for block in heading_blocks] == ["2", "The Lost Homeland"]
    assert [block.heading_level for block in heading_blocks] == [1, 2]


def test_build_document_assigns_same_level_to_same_style_headings():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Document Title",
                        x0=120.0,
                        top=40.0,
                        x1=420.0,
                        bottom=60.0,
                        reading_order=1,
                        avg_font_size=18.0,
                        dominant_font_name="Times-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="Comparison with Beauty Commerce",
                        x0=60.0,
                        top=120.0,
                        x1=320.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h2",
                        text="Education Content Platform PoC Case",
                        x0=60.0,
                        top=160.0,
                        x1=340.0,
                        bottom=176.0,
                        reading_order=3,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="Regular paragraph text.",
                        x0=60.0,
                        top=210.0,
                        x1=240.0,
                        bottom=224.0,
                        reading_order=4,
                        avg_font_size=12.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    heading_blocks = [block for block in structured.blocks if block.block_type == "heading"]
    assert [block.text for block in heading_blocks] == [
        "Document Title",
        "Comparison with Beauty Commerce",
        "Education Content Platform PoC Case",
    ]
    assert [block.heading_level for block in heading_blocks] == [1, 2, 2]


def test_build_document_keeps_directory_entries_separate_on_contents_pages():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Table of contents",
                        x0=150.0,
                        top=90.0,
                        x1=360.0,
                        bottom=110.0,
                        reading_order=1,
                        avg_font_size=19.0,
                        dominant_font_name="Times-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry1",
                        text="2. Core and Periphery of Play",
                        x0=60.0,
                        top=160.0,
                        x1=280.0,
                        bottom=174.0,
                        reading_order=2,
                        avg_font_size=10.5,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page1",
                        text="12",
                        x0=380.0,
                        top=160.0,
                        x1=400.0,
                        bottom=174.0,
                        reading_order=3,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry2",
                        text="Part I: New Children, Different Toys",
                        x0=60.0,
                        top=182.0,
                        x1=300.0,
                        bottom=196.0,
                        reading_order=4,
                        avg_font_size=12.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page2",
                        text="21",
                        x0=380.0,
                        top=182.0,
                        x1=400.0,
                        bottom=196.0,
                        reading_order=5,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry3",
                        text="3. The Child as Consumer",
                        x0=60.0,
                        top=204.0,
                        x1=250.0,
                        bottom=218.0,
                        reading_order=6,
                        avg_font_size=10.5,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page3",
                        text="26",
                        x0=380.0,
                        top=204.0,
                        x1=400.0,
                        bottom=218.0,
                        reading_order=7,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry4",
                        text="20. Making Do",
                        x0=60.0,
                        top=240.0,
                        x1=180.0,
                        bottom=254.0,
                        reading_order=8,
                        avg_font_size=10.5,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page4",
                        text="127",
                        x0=380.0,
                        top=240.0,
                        x1=406.0,
                        bottom=254.0,
                        reading_order=9,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry5",
                        text="Notes",
                        x0=60.0,
                        top=262.0,
                        x1=110.0,
                        bottom=276.0,
                        reading_order=10,
                        avg_font_size=12.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page5",
                        text="132",
                        x0=380.0,
                        top=262.0,
                        x1=406.0,
                        bottom=276.0,
                        reading_order=11,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry6",
                        text="Bibliography",
                        x0=60.0,
                        top=284.0,
                        x1=150.0,
                        bottom=298.0,
                        reading_order=12,
                        avg_font_size=12.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page6",
                        text="137",
                        x0=380.0,
                        top=284.0,
                        x1=406.0,
                        bottom=298.0,
                        reading_order=13,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="entry7",
                        text="Index",
                        x0=60.0,
                        top=306.0,
                        x1=110.0,
                        bottom=320.0,
                        reading_order=14,
                        avg_font_size=12.0,
                    ),
                    _resolved_line(
                        page=1,
                        line_id="page7",
                        text="139",
                        x0=380.0,
                        top=306.0,
                        x1=406.0,
                        bottom=320.0,
                        reading_order=15,
                        avg_font_size=10.5,
                        column_id="right",
                        region="right_column",
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)
    texts = [block.text for block in structured.blocks]

    assert "2. Core and Periphery of Play" in texts
    assert "Part I: New Children, Different Toys" in texts
    assert "3. The Child as Consumer" in texts
    assert "20. Making Do" in texts
    assert "Notes" in texts
    assert "Bibliography" in texts
    assert "Index" in texts
    assert "2. Core and Periphery of Play\nPart I: New Children, Different Toys" not in texts
    assert "20. Making Do\nNotes\nBibliography\nIndex" not in texts


def test_build_document_merges_multiline_heading_blocks():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="Upstage offers 3 AI packs that process unstructured information and data,",
                        x0=90.0,
                        top=40.0,
                        x1=510.0,
                        bottom=58.0,
                        reading_order=1,
                        avg_font_size=22.0,
                        dominant_font_name="Montserrat-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h2",
                        text="making a tangible impact on your business",
                        x0=128.0,
                        top=60.0,
                        x1=472.0,
                        bottom=78.0,
                        reading_order=2,
                        avg_font_size=22.0,
                        dominant_font_name="Montserrat-Bold",
                        band="top_band",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="The body paragraph starts here.",
                        x0=60.0,
                        top=120.0,
                        x1=260.0,
                        bottom=134.0,
                        reading_order=3,
                        avg_font_size=12.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph"]
    assert structured.blocks[0].text == (
        "Upstage offers 3 AI packs that process unstructured information and data,\n"
        "making a tangible impact on your business"
    )
    assert structured.blocks[0].line_ids == ["h1", "h2"]


def test_build_document_merges_left_aligned_multiline_heading_blocks():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="h1",
                        text="Comparison with Beauty Commerce",
                        x0=60.0,
                        top=120.0,
                        x1=312.0,
                        bottom=136.0,
                        reading_order=1,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="h2",
                        text="Recommendation Models",
                        x0=60.0,
                        top=138.0,
                        x1=238.0,
                        bottom=154.0,
                        reading_order=2,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="body",
                        text="Recommendation model Hit Ratio comparison",
                        x0=60.0,
                        top=184.0,
                        x1=320.0,
                        bottom=198.0,
                        reading_order=3,
                        avg_font_size=12.0,
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph"]
    assert structured.blocks[0].text == "Comparison with Beauty Commerce\nRecommendation Models"
    assert structured.blocks[0].line_ids == ["h1", "h2"]


def test_build_document_does_not_promote_chart_metric_labels_to_headings():
    builder = LocalPdfBlockBuilder()
    document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="title",
                        text="Recommendation Pack: Track Record",
                        x0=60.0,
                        top=60.0,
                        x1=360.0,
                        bottom=78.0,
                        reading_order=1,
                        avg_font_size=16.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="metric1",
                        text="0.4048 CustomerBERT",
                        x0=88.0,
                        top=120.0,
                        x1=220.0,
                        bottom=136.0,
                        reading_order=2,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                    _resolved_line(
                        page=1,
                        line_id="metric2",
                        text="_MultiVAE 20%↑",
                        x0=88.0,
                        top=142.0,
                        x1=220.0,
                        bottom=158.0,
                        reading_order=3,
                        avg_font_size=14.0,
                        dominant_font_name="Times-Bold",
                    ),
                ],
            )
        ]
    )

    structured = builder.build_document(document=document)

    assert [block.block_type for block in structured.blocks] == ["heading", "paragraph"]
    assert structured.blocks[1].text == "0.4048 CustomerBERT\n_MultiVAE 20%↑"
