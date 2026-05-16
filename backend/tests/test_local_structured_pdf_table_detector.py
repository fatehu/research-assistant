from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfTableDetector,
    PdfBBox,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfRectAtom,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
    PdfTableAtom,
    PdfWordAtom,
)


def _word(word_id: str, text: str, x0: float, x1: float, top: float = 100.0, bottom: float = 112.0) -> PdfWordAtom:
    return PdfWordAtom(
        word_id=word_id,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        doctop=top,
        font_name="Times",
        font_size=12.0,
    )


def _line(line_id: str, word_ids: list[str], text: str, top: float) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=1,
        text=text,
        bbox=PdfBBox(x0=60.0, top=top, x1=320.0, bottom=top + 14.0),
        word_ids=word_ids,
        avg_font_size=12.0,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id="main",
        reading_order=1,
    )


def _wide_line(line_id: str, word_ids: list[str], text: str, top: float) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=1,
        text=text,
        bbox=PdfBBox(x0=60.0, top=top, x1=560.0, bottom=top + 14.0),
        word_ids=word_ids,
        avg_font_size=12.0,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id="main",
        reading_order=1,
    )


def _row_words(prefix: str, left: list[str], right: list[str], top: float) -> list[PdfWordAtom]:
    words: list[PdfWordAtom] = []
    cursor = 60.0
    for index, token in enumerate(left, start=1):
        width = max(12.0, float(len(token)) * 4.2)
        words.append(_word(f"{prefix}l{index}", token, cursor, cursor + width, top, top + 12.0))
        cursor += width + 6.0
    cursor = 420.0
    for index, token in enumerate(right, start=1):
        width = max(12.0, float(len(token)) * 4.2)
        words.append(_word(f"{prefix}r{index}", token, cursor, cursor + width, top, top + 12.0))
        cursor += width + 6.0
    return words


def test_detect_document_converts_grid_like_paragraph_into_table_block():
    detector = LocalPdfTableDetector()
    normalized_page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        kept_words=[
            _word("w1", "Name", 60.0, 100.0, 100.0, 112.0),
            _word("w2", "Score", 210.0, 250.0, 100.0, 112.0),
            _word("w3", "Alice", 60.0, 100.0, 120.0, 132.0),
            _word("w4", "95", 210.0, 225.0, 120.0, 132.0),
            _word("w5", "Bob", 60.0, 90.0, 140.0, 152.0),
            _word("w6", "88", 210.0, 225.0, 140.0, 152.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=normalized_page.meta,
                lines=[
                    _line("l1", ["w1", "w2"], "Name Score", 100.0),
                    _line("l2", ["w3", "w4"], "Alice 95", 120.0),
                    _line("l3", ["w5", "w6"], "Bob 88", 140.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=normalized_page.meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Name Score\nAlice 95\nBob 88",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=320.0, bottom=154.0),
                line_ids=["l1", "l2", "l3"],
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert enriched.blocks[0].block_type == "table"
    assert enriched.blocks[0].table_rows == [
        ["Name", "Score"],
        ["Alice", "95"],
        ["Bob", "88"],
    ]


def test_detect_document_prefers_pymupdf_table_atoms_when_available():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word("w1", "Name", 60.0, 100.0, 100.0, 112.0),
            _word("w2", "Score", 210.0, 250.0, 100.0, 112.0),
            _word("w3", "Alice", 60.0, 100.0, 120.0, 132.0),
            _word("w4", "95", 210.0, 225.0, 120.0, 132.0),
            _word("w5", "Bob", 60.0, 90.0, 140.0, 152.0),
            _word("w6", "88", 210.0, 225.0, 140.0, 152.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1", "w2"], "Name Score", 100.0),
                    _line("l2", ["w3", "w4"], "Alice 95", 120.0),
                    _line("l3", ["w5", "w6"], "Bob 88", 140.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Name Score\nAlice 95\nBob 88",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=320.0, bottom=154.0),
                line_ids=["l1", "l2", "l3"],
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=55.0, top=95.0, x1=325.0, bottom=160.0),
                        row_count=3,
                        col_count=2,
                        cells=[
                            ["Name", "Score"],
                            ["Alice", "95"],
                            ["Bob", "88"],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert enriched.blocks[0].block_type == "table"
    assert enriched.blocks[0].table_rows == [
        ["Name", "Score"],
        ["Alice", "95"],
        ["Bob", "88"],
    ]


def test_detect_document_merges_heading_and_paragraph_blocks_into_single_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word("w1", "Name", 60.0, 100.0, 100.0, 112.0),
            _word("w2", "Score", 210.0, 250.0, 100.0, 112.0),
            _word("w3", "Alice", 60.0, 100.0, 120.0, 132.0),
            _word("w4", "95", 210.0, 225.0, 120.0, 132.0),
            _word("w5", "Bob", 60.0, 90.0, 140.0, 152.0),
            _word("w6", "88", 210.0, 225.0, 140.0, 152.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1", "w2"], "Name Score", 100.0),
                    _line("l2", ["w3", "w4"], "Alice 95", 120.0),
                    _line("l3", ["w5", "w6"], "Bob 88", 140.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=page_meta,
                blocks=[
                    PdfSemanticBlock(
                        block_id="b1",
                        block_type="heading",
                        page_start=1,
                        page_end=1,
                        text="Name Score",
                        bbox=PdfBBox(x0=60.0, top=100.0, x1=320.0, bottom=114.0),
                        line_ids=["l1"],
                        reading_order_start=1,
                        reading_order_end=1,
                    ),
                    PdfSemanticBlock(
                        block_id="b2",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Alice 95\nBob 88",
                        bbox=PdfBBox(x0=60.0, top=120.0, x1=320.0, bottom=154.0),
                        line_ids=["l2", "l3"],
                        reading_order_start=2,
                        reading_order_end=3,
                    ),
                ],
            )
        ],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Name Score",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=320.0, bottom=114.0),
                line_ids=["l1"],
                reading_order_start=1,
                reading_order_end=1,
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Alice 95\nBob 88",
                bbox=PdfBBox(x0=60.0, top=120.0, x1=320.0, bottom=154.0),
                line_ids=["l2", "l3"],
                reading_order_start=2,
                reading_order_end=3,
            ),
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["table"]
    assert enriched.blocks[0].table_rows == [
        ["Name", "Score"],
        ["Alice", "95"],
        ["Bob", "88"],
    ]


def test_detect_document_splits_mixed_paragraph_and_preserves_caption():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word("w1", "We", 60.0, 75.0, 80.0, 92.0),
            _word("w2", "summarize", 80.0, 135.0, 80.0, 92.0),
            _word("w3", "the", 140.0, 158.0, 80.0, 92.0),
            _word("w4", "results", 163.0, 205.0, 80.0, 92.0),
            _word("w5", "below.", 210.0, 248.0, 80.0, 92.0),
            _word("w6", "Metric", 60.0, 100.0, 100.0, 112.0),
            _word("w7", "Value", 210.0, 245.0, 100.0, 112.0),
            _word("w8", "Alpha:1.0", 60.0, 130.0, 120.0, 132.0),
            _word("w9", "Beta:2.0", 60.0, 120.0, 140.0, 152.0),
            _word("w10", "Table", 60.0, 92.0, 160.0, 172.0),
            _word("w11", "1:", 96.0, 110.0, 160.0, 172.0),
            _word("w12", "Example", 116.0, 164.0, 160.0, 172.0),
            _word("w13", "caption", 170.0, 214.0, 160.0, 172.0),
            _word("w14", "Following", 60.0, 112.0, 180.0, 192.0),
            _word("w15", "text", 118.0, 142.0, 180.0, 192.0),
            _word("w16", "continues.", 148.0, 208.0, 180.0, 192.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1", "w2", "w3", "w4", "w5"], "We summarize the results below.", 80.0),
                    _line("l2", ["w6", "w7"], "Metric Value", 100.0),
                    _line("l3", ["w8"], "Alpha:1.0", 120.0),
                    _line("l4", ["w9"], "Beta:2.0", 140.0),
                    _line("l5", ["w10", "w11", "w12", "w13"], "Table 1: Example caption", 160.0),
                    _line("l6", ["w14", "w15", "w16"], "Following text continues.", 180.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "We summarize the results below.\n"
                    "Metric Value\n"
                    "Alpha:1.0\n"
                    "Beta:2.0\n"
                    "Table 1: Example caption\n"
                    "Following text continues."
                ),
                bbox=PdfBBox(x0=60.0, top=80.0, x1=320.0, bottom=194.0),
                line_ids=["l1", "l2", "l3", "l4", "l5", "l6"],
                reading_order_start=1,
                reading_order_end=6,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph", "table", "paragraph"]
    assert enriched.blocks[0].text == "We summarize the results below."
    assert enriched.blocks[1].table_rows == [
        ["Metric", "Value"],
        ["Alpha", "1.0"],
        ["Beta", "2.0"],
    ]
    assert "Table 1: Example caption" in enriched.blocks[2].text


def test_detect_document_does_not_materialize_source_metadata_pairs_as_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            *_row_words("r1", ["9th,", "2022.", "DOI:"], ["https://doi.org/10.25318/3210036401-eng."], 100.0),
            *_row_words("r2", ["Canada", "Open", "Licence:"], ["https://www.statcan.gc.ca/en/reference/licence"], 120.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _wide_line(
                        "l1",
                        ["r1l1", "r1l2", "r1l3", "r1r1"],
                        "9th, 2022. DOI: https://doi.org/10.25318/3210036401-eng.",
                        100.0,
                    ),
                    _wide_line(
                        "l2",
                        ["r2l1", "r2l2", "r2l3", "r2r1"],
                        "Canada Open Licence: https://www.statcan.gc.ca/en/reference/licence",
                        120.0,
                    ),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "9th, 2022. DOI: https://doi.org/10.25318/3210036401-eng.\n"
                    "Canada Open Licence: https://www.statcan.gc.ca/en/reference/licence"
                ),
                bbox=PdfBBox(x0=60.0, top=100.0, x1=560.0, bottom=134.0),
                line_ids=["l1", "l2"],
                reading_order_start=1,
                reading_order_end=2,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph"]
    assert enriched.blocks[0].table_rows == []


def test_detect_document_uses_header_anchors_for_wide_table_rows():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word("w1", "Model", 60.0, 90.0, 100.0, 112.0),
            _word("w2", "H6", 110.0, 122.0, 100.0, 112.0),
            _word("w3", "(Avg.)", 125.0, 160.0, 100.0, 112.0),
            _word("w4", "ARC", 180.0, 200.0, 100.0, 112.0),
            _word("w5", "MMLU", 220.0, 250.0, 100.0, 112.0),
            _word("w6", "Cand.", 60.0, 82.0, 120.0, 132.0),
            _word("w7", "1", 85.0, 90.0, 120.0, 132.0),
            _word("w8", "73.73", 110.0, 145.0, 120.0, 132.0),
            _word("w9", "70.48", 180.0, 215.0, 120.0, 132.0),
            _word("w10", "87.47", 220.0, 255.0, 120.0, 132.0),
            _word("w11", "Cand.", 60.0, 82.0, 140.0, 152.0),
            _word("w12", "2", 85.0, 90.0, 140.0, 152.0),
            _word("w13", "73.28", 110.0, 145.0, 140.0, 152.0),
            _word("w14", "71.59", 180.0, 215.0, 140.0, 152.0),
            _word("w15", "88.39", 220.0, 255.0, 140.0, 152.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1", "w2", "w3", "w4", "w5"], "Model H6 (Avg.) ARC MMLU", 100.0),
                    _line("l2", ["w6", "w7", "w8", "w9", "w10"], "Cand. 1 73.73 70.48 87.47", 120.0),
                    _line("l3", ["w11", "w12", "w13", "w14", "w15"], "Cand. 2 73.28 71.59 88.39", 140.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Model H6 (Avg.) ARC MMLU\nCand. 1 73.73 70.48 87.47\nCand. 2 73.28 71.59 88.39",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=255.0, bottom=154.0),
                line_ids=["l1", "l2", "l3"],
                reading_order_start=1,
                reading_order_end=3,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["table"]
    assert enriched.blocks[0].table_rows == [
        ["Model", "H6 (Avg.)", "ARC", "MMLU"],
        ["Cand. 1", "73.73", "70.48", "87.47"],
        ["Cand. 2", "73.28", "71.59", "88.39"],
    ]


def test_detect_document_uses_page_level_pymupdf_table_to_split_long_cell_paragraph():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word("w1", "We summarize the initiatives below.", 60.0, 280.0, 80.0, 92.0),
            _word(
                "w2",
                "Source Year Description Circular Economy issues addressed",
                60.0,
                320.0,
                100.0,
                112.0,
            ),
            _word(
                "w3",
                "Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions",
                60.0,
                360.0,
                120.0,
                132.0,
            ),
            _word(
                "w4",
                "Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables",
                60.0,
                360.0,
                140.0,
                152.0,
            ),
            _word(
                "w5",
                "Fondation Terre Solidaire 2016 The foundation mobilized local initiatives Support and encourage experimentation",
                60.0,
                360.0,
                160.0,
                172.0,
            ),
            _word("w6", "The narrative continues below.", 60.0, 250.0, 190.0, 202.0),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1"], "We summarize the initiatives below.", 80.0),
                    _line(
                        "l2",
                        ["w2"],
                        "Source Year Description Circular Economy issues addressed",
                        100.0,
                    ),
                    _line(
                        "l3",
                        ["w3"],
                        "Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions",
                        120.0,
                    ),
                    _line(
                        "l4",
                        ["w4"],
                        "Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables",
                        140.0,
                    ),
                    _line(
                        "l5",
                        ["w5"],
                        "Fondation Terre Solidaire 2016 The foundation mobilized local initiatives Support and encourage experimentation",
                        160.0,
                    ),
                    _line("l6", ["w6"], "The narrative continues below.", 190.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "We summarize the initiatives below.\n"
                    "Source Year Description Circular Economy issues addressed\n"
                    "Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions\n"
                    "Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables\n"
                    "Fondation Terre Solidaire 2016 The foundation mobilized local initiatives Support and encourage experimentation\n"
                    "The narrative continues below."
                ),
                bbox=PdfBBox(x0=60.0, top=80.0, x1=360.0, bottom=204.0),
                line_ids=["l1", "l2", "l3", "l4", "l5", "l6"],
                reading_order_start=1,
                reading_order_end=6,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=55.0, top=96.0, x1=365.0, bottom=176.0),
                        row_count=4,
                        col_count=4,
                        cells=[
                            [
                                "Source",
                                "Year",
                                "Description",
                                "Circular Economy issues addressed",
                            ],
                            [
                                "Eco-Ecole Program",
                                "2005",
                                "Eco-Ecole is a program in schools",
                                "Eco-Ecole offers sustainability actions",
                            ],
                            [
                                "Horsnormes",
                                "2020",
                                "Horsnormes is a project about food waste reduction",
                                "Waste reduction of fruits and vegetables",
                            ],
                            [
                                "Fondation Terre Solidaire",
                                "2016",
                                "The foundation mobilized local initiatives",
                                "Support and encourage experimentation",
                            ],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph", "table", "paragraph"]
    assert enriched.blocks[0].text == "We summarize the initiatives below."
    assert enriched.blocks[1].table_rows == [
        ["Source", "Year", "Description", "Circular Economy issues addressed"],
        [
            "Eco-Ecole Program",
            "2005",
            "Eco-Ecole is a program in schools",
            "Eco-Ecole offers sustainability actions",
        ],
        [
            "Horsnormes",
            "2020",
            "Horsnormes is a project about food waste reduction",
            "Waste reduction of fruits and vegetables",
        ],
        [
            "Fondation Terre Solidaire",
            "2016",
            "The foundation mobilized local initiatives",
            "Support and encourage experimentation",
        ],
    ]
    assert enriched.blocks[2].text == "The narrative continues below."


def test_detect_document_merges_fragmented_blocks_from_single_pymupdf_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=[
            _word(
                "w1",
                "Source Year Description Circular Economy issues addressed",
                60.0,
                320.0,
                100.0,
                112.0,
            ),
            _word(
                "w2",
                "Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions",
                60.0,
                360.0,
                130.0,
                142.0,
            ),
            _word(
                "w3",
                "Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables",
                60.0,
                360.0,
                160.0,
                172.0,
            ),
        ],
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", ["w1"], "Source Year Description Circular Economy issues addressed", 100.0),
                    _line(
                        "l2",
                        ["w2"],
                        "Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions",
                        130.0,
                    ),
                    _line(
                        "l3",
                        ["w3"],
                        "Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables",
                        160.0,
                    ),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Source Year Description Circular Economy issues addressed",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=360.0, bottom=114.0),
                line_ids=["l1"],
                reading_order_start=1,
                reading_order_end=1,
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Eco-Ecole Program 2005 Eco-Ecole is a program in schools Eco-Ecole offers sustainability actions",
                bbox=PdfBBox(x0=60.0, top=130.0, x1=360.0, bottom=144.0),
                line_ids=["l2"],
                reading_order_start=2,
                reading_order_end=2,
            ),
            PdfSemanticBlock(
                block_id="b3",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Horsnormes 2020 Horsnormes is a project about food waste reduction Waste reduction of fruits and vegetables",
                bbox=PdfBBox(x0=60.0, top=160.0, x1=360.0, bottom=174.0),
                line_ids=["l3"],
                reading_order_start=3,
                reading_order_end=3,
            ),
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=55.0, top=96.0, x1=365.0, bottom=176.0),
                        row_count=3,
                        col_count=4,
                        cells=[
                            ["Source", "Year", "Description", "Circular Economy issues addressed"],
                            [
                                "Eco-Ecole Program",
                                "2005",
                                "Eco-Ecole is a program in schools",
                                "Eco-Ecole offers sustainability actions",
                            ],
                            [
                                "Horsnormes",
                                "2020",
                                "Horsnormes is a project about food waste reduction",
                                "Waste reduction of fruits and vegetables",
                            ],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["table"]
    assert enriched.blocks[0].table_rows == [
        ["Source", "Year", "Description", "Circular Economy issues addressed"],
        [
            "Eco-Ecole Program",
            "2005",
            "Eco-Ecole is a program in schools",
            "Eco-Ecole offers sustainability actions",
        ],
        [
            "Horsnormes",
            "2020",
            "Horsnormes is a project about food waste reduction",
            "Waste reduction of fruits and vegetables",
        ],
    ]


def test_detect_document_avoids_turning_bibliography_columns_into_two_col_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    row1_words = _row_words(
        "r1",
        ["Edward", "Beeching,", "Clémentine", "Fourrier,"],
        ["Dan", "Hendrycks,", "Collin", "Burns,"],
        100.0,
    )
    row2_words = _row_words(
        "r2",
        ["Nathan", "Habib,", "Sheon", "Han,"],
        ["Saurav", "Kadavath,", "Akul", "Arora,"],
        120.0,
    )
    row3_words = _row_words(
        "r3",
        ["Thomas", "Wolf.", "2023.", "Open", "leaderboard."],
        ["Steven", "Basart,", "Eric", "Tang.", "2021."],
        140.0,
    )
    kept_words = [*row1_words, *row2_words, *row3_words]
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=kept_words, dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _wide_line(
                        "l1",
                        [word.word_id for word in row1_words],
                        "Edward Beeching, Clémentine Fourrier, Dan Hendrycks, Collin Burns,",
                        100.0,
                    ),
                    _wide_line(
                        "l2",
                        [word.word_id for word in row2_words],
                        "Nathan Habib, Sheon Han, Saurav Kadavath, Akul Arora,",
                        120.0,
                    ),
                    _wide_line(
                        "l3",
                        [word.word_id for word in row3_words],
                        "Thomas Wolf. 2023. Open leaderboard. Steven Basart, Eric Tang. 2021.",
                        140.0,
                    ),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "Edward Beeching, Clémentine Fourrier, Dan Hendrycks, Collin Burns,\n"
                    "Nathan Habib, Sheon Han, Saurav Kadavath, Akul Arora,\n"
                    "Thomas Wolf. 2023. Open leaderboard. Steven Basart, Eric Tang. 2021."
                ),
                bbox=PdfBBox(x0=60.0, top=100.0, x1=560.0, bottom=154.0),
                line_ids=["l1", "l2", "l3"],
                reading_order_start=1,
                reading_order_end=3,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph"]


def test_detect_document_avoids_turning_sidebar_and_body_into_two_col_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0)
    row1_words = _row_words(
        "s1",
        ["context,", "we", "are"],
        ["example,", "they", "may", "contact", "a", "person"],
        100.0,
    )
    row2_words = _row_words(
        "s2",
        ["talking", "about"],
        ["who", "is", "quoted", "in", "a", "proposed", "news"],
        120.0,
    )
    row3_words = _row_words(
        "s3",
        ["fact-checking"],
        ["article", "and", "ask", "the", "person", "whether"],
        140.0,
    )
    kept_words = [*row1_words, *row2_words, *row3_words]
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=kept_words, dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _wide_line(
                        "l1",
                        [word.word_id for word in row1_words],
                        "context, we are example, they may contact a person",
                        100.0,
                    ),
                    _wide_line(
                        "l2",
                        [word.word_id for word in row2_words],
                        "talking about who is quoted in a proposed news",
                        120.0,
                    ),
                    _wide_line(
                        "l3",
                        [word.word_id for word in row3_words],
                        "fact-checking article and ask the person whether",
                        140.0,
                    ),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "context, we are example, they may contact a person\n"
                    "talking about who is quoted in a proposed news\n"
                    "fact-checking article and ask the person whether"
                ),
                bbox=PdfBBox(x0=60.0, top=100.0, x1=560.0, bottom=154.0),
                line_ids=["l1", "l2", "l3"],
                reading_order_start=1,
                reading_order_end=3,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph"]


def test_detect_document_merges_page_level_pymupdf_table_from_fragmented_cell_blocks():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", [], "Service Stage", 100.0),
                    _line("l2", [], "Function Name", 100.0),
                    _line("l3", [], "Explanation", 100.0),
                    _line("l4", [], "Expected Benefit", 100.0),
                    _line("l5", [], "1. Project creation", 130.0),
                    _line("l6", [], "Project creation and management", 130.0),
                    _line("l7", [], "Select document type to automatically run project creation", 130.0),
                    _line("l8", [], "The intuitive UI environment improves work efficiency", 130.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Service Stage",
                bbox=PdfBBox(x0=40.0, top=100.0, x1=130.0, bottom=114.0),
                line_ids=["l1"],
                column_id="left",
                region="left_column",
                reading_order_start=1,
                reading_order_end=1,
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Function Name",
                bbox=PdfBBox(x0=150.0, top=100.0, x1=250.0, bottom=114.0),
                line_ids=["l2"],
                column_id="right",
                region="right_column",
                reading_order_start=2,
                reading_order_end=2,
            ),
            PdfSemanticBlock(
                block_id="b3",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Explanation",
                bbox=PdfBBox(x0=270.0, top=100.0, x1=430.0, bottom=114.0),
                line_ids=["l3"],
                column_id="right",
                region="right_column",
                reading_order_start=3,
                reading_order_end=3,
            ),
            PdfSemanticBlock(
                block_id="b4",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Expected Benefit",
                bbox=PdfBBox(x0=450.0, top=100.0, x1=700.0, bottom=114.0),
                line_ids=["l4"],
                column_id="right",
                region="right_column",
                reading_order_start=4,
                reading_order_end=4,
            ),
            PdfSemanticBlock(
                block_id="b5",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="1. Project creation",
                bbox=PdfBBox(x0=40.0, top=130.0, x1=130.0, bottom=144.0),
                line_ids=["l5"],
                column_id="left",
                region="left_column",
                reading_order_start=5,
                reading_order_end=5,
            ),
            PdfSemanticBlock(
                block_id="b6",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Project creation and management",
                bbox=PdfBBox(x0=150.0, top=130.0, x1=250.0, bottom=144.0),
                line_ids=["l6"],
                column_id="right",
                region="right_column",
                reading_order_start=6,
                reading_order_end=6,
            ),
            PdfSemanticBlock(
                block_id="b7",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Select document type to automatically run project creation",
                bbox=PdfBBox(x0=270.0, top=130.0, x1=430.0, bottom=144.0),
                line_ids=["l7"],
                column_id="main",
                region="full_width",
                reading_order_start=7,
                reading_order_end=7,
            ),
            PdfSemanticBlock(
                block_id="b8",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="The intuitive UI environment improves work efficiency",
                bbox=PdfBBox(x0=450.0, top=130.0, x1=700.0, bottom=144.0),
                line_ids=["l8"],
                column_id="right",
                region="right_column",
                reading_order_start=8,
                reading_order_end=8,
            ),
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=35.0, top=96.0, x1=705.0, bottom=148.0),
                        row_count=2,
                        col_count=4,
                        cells=[
                            ["Service Stage", "Function Name", "Explanation", "Expected Benefit"],
                            [
                                "1. Project creation",
                                "Project creation and management",
                                "Select document type to automatically run project creation",
                                "The intuitive UI environment improves work efficiency",
                            ],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["table"]
    assert enriched.blocks[0].table_rows == [
        ["Service Stage", "Function Name", "Explanation", "Expected Benefit"],
        [
            "1. Project creation",
            "Project creation and management",
            "Select document type to automatically run project creation",
            "The intuitive UI environment improves work efficiency",
        ],
    ]


def test_detect_document_falls_back_to_rect_grid_when_pymupdf_cells_are_sparse():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=800.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _line("l1", [], "Service Stage", 100.0),
                    _line("l2", [], "Function Name", 100.0),
                    _line("l3", [], "Explanation", 100.0),
                    _line("l4", [], "Expected Benefit", 100.0),
                    _line("l5", [], "1. Project creation", 130.0),
                    _line("l6", [], "Project creation and management", 130.0),
                    _line("l7", [], "Select document type", 130.0),
                    _line("l8", [], "Improves work efficiency", 130.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Service Stage",
                bbox=PdfBBox(x0=40.0, top=100.0, x1=130.0, bottom=114.0),
                line_ids=["l1"],
                column_id="left",
                region="left_column",
                reading_order_start=1,
                reading_order_end=1,
            ),
            PdfSemanticBlock(
                block_id="b2",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Function Name",
                bbox=PdfBBox(x0=150.0, top=100.0, x1=250.0, bottom=114.0),
                line_ids=["l2"],
                column_id="right",
                region="right_column",
                reading_order_start=2,
                reading_order_end=2,
            ),
            PdfSemanticBlock(
                block_id="b3",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Explanation",
                bbox=PdfBBox(x0=270.0, top=100.0, x1=430.0, bottom=114.0),
                line_ids=["l3"],
                column_id="right",
                region="right_column",
                reading_order_start=3,
                reading_order_end=3,
            ),
            PdfSemanticBlock(
                block_id="b4",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="Expected Benefit",
                bbox=PdfBBox(x0=450.0, top=100.0, x1=700.0, bottom=114.0),
                line_ids=["l4"],
                column_id="right",
                region="right_column",
                reading_order_start=4,
                reading_order_end=4,
            ),
            PdfSemanticBlock(
                block_id="b5",
                block_type="heading",
                page_start=1,
                page_end=1,
                text="1. Project creation",
                bbox=PdfBBox(x0=40.0, top=130.0, x1=130.0, bottom=144.0),
                line_ids=["l5"],
                column_id="left",
                region="left_column",
                reading_order_start=5,
                reading_order_end=5,
            ),
            PdfSemanticBlock(
                block_id="b6",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Project creation and management",
                bbox=PdfBBox(x0=150.0, top=130.0, x1=250.0, bottom=144.0),
                line_ids=["l6"],
                column_id="right",
                region="right_column",
                reading_order_start=6,
                reading_order_end=6,
            ),
            PdfSemanticBlock(
                block_id="b7",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Select document type",
                bbox=PdfBBox(x0=270.0, top=130.0, x1=430.0, bottom=144.0),
                line_ids=["l7"],
                column_id="main",
                region="full_width",
                reading_order_start=7,
                reading_order_end=7,
            ),
            PdfSemanticBlock(
                block_id="b8",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="Improves work efficiency",
                bbox=PdfBBox(x0=450.0, top=130.0, x1=700.0, bottom=144.0),
                line_ids=["l8"],
                column_id="right",
                region="right_column",
                reading_order_start=8,
                reading_order_end=8,
            ),
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                words=[
                    _word("w1", "Service", 45.0, 80.0, 100.0, 112.0),
                    _word("w2", "Stage", 82.0, 118.0, 100.0, 112.0),
                    _word("w3", "Function", 155.0, 210.0, 100.0, 112.0),
                    _word("w4", "Name", 214.0, 248.0, 100.0, 112.0),
                    _word("w5", "Explanation", 275.0, 360.0, 100.0, 112.0),
                    _word("w6", "Expected", 455.0, 520.0, 100.0, 112.0),
                    _word("w7", "Benefit", 525.0, 585.0, 100.0, 112.0),
                    _word("w8", "1.", 45.0, 55.0, 130.0, 142.0),
                    _word("w9", "Project", 58.0, 88.0, 130.0, 142.0),
                    _word("w10", "creation", 92.0, 125.0, 130.0, 142.0),
                    _word("w11", "Project", 155.0, 182.0, 130.0, 142.0),
                    _word("w12", "creation", 185.0, 215.0, 130.0, 142.0),
                    _word("w13", "and", 218.0, 228.0, 130.0, 142.0),
                    _word("w14", "management", 230.0, 248.0, 130.0, 142.0),
                    _word("w15", "Select", 275.0, 320.0, 130.0, 142.0),
                    _word("w16", "document", 323.0, 390.0, 130.0, 142.0),
                    _word("w17", "type", 393.0, 420.0, 130.0, 142.0),
                    _word("w18", "Improves", 455.0, 520.0, 130.0, 142.0),
                    _word("w19", "work", 523.0, 560.0, 130.0, 142.0),
                    _word("w20", "efficiency", 563.0, 635.0, 130.0, 142.0),
                ],
                rects=[
                    PdfRectAtom("r1", PdfBBox(x0=40.0, top=98.0, x1=130.0, bottom=114.0)),
                    PdfRectAtom("r2", PdfBBox(x0=150.0, top=98.0, x1=250.0, bottom=114.0)),
                    PdfRectAtom("r3", PdfBBox(x0=270.0, top=98.0, x1=430.0, bottom=114.0)),
                    PdfRectAtom("r4", PdfBBox(x0=450.0, top=98.0, x1=700.0, bottom=114.0)),
                    PdfRectAtom("r5", PdfBBox(x0=40.0, top=128.0, x1=130.0, bottom=144.0)),
                    PdfRectAtom("r6", PdfBBox(x0=150.0, top=128.0, x1=250.0, bottom=144.0)),
                    PdfRectAtom("r7", PdfBBox(x0=270.0, top=128.0, x1=430.0, bottom=144.0)),
                    PdfRectAtom("r8", PdfBBox(x0=450.0, top=128.0, x1=700.0, bottom=144.0)),
                ],
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=35.0, top=96.0, x1=705.0, bottom=148.0),
                        row_count=2,
                        col_count=4,
                        cells=[
                            ["Service Stage Function Name Explanation Expected Benefit", "", "", ""],
                            ["", "", "", ""],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["table"]
    assert enriched.blocks[0].table_rows == [
        ["Service Stage", "Function Name", "Explanation", "Expected Benefit"],
        [
            "1. Project creation",
            "Project creation and management",
            "Select document type",
            "Improves work efficiency",
        ],
    ]


def test_detect_document_does_not_turn_parallel_prose_columns_into_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    row1_words = _row_words(
        "w1",
        ["Thailand,", "Philippines", "and", "Indonesia", "in"],
        ["of", "the", "region", "that", "most", "experience", "violent"],
        100.0,
    )
    row2_words = _row_words(
        "w2",
        ["particular,", "identifying", "known", "experts", "at"],
        ["extremism", "and", "terrorism.", "However,"],
        120.0,
    )
    row3_words = _row_words(
        "w3",
        ["the", "national,", "subnational", "and", "community"],
        ["through", "our", "networks,", "where", "possible,"],
        140.0,
    )
    row4_words = _row_words(
        "w4",
        ["level.", "The", "survey", "and", "interviews", "with"],
        ["we", "disseminated", "the", "survey", "throughout"],
        160.0,
    )
    all_words = row1_words + row2_words + row3_words + row4_words
    normalized_page = PdfNormalizedPage(
        meta=page_meta,
        kept_words=all_words,
        dropped_words=[],
        text_lines=[],
    )
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _wide_line("l1", [word.word_id for word in row1_words], "Thailand, Philippines and Indonesia in of the region that most experience violent", 100.0),
                    _wide_line("l2", [word.word_id for word in row2_words], "particular, identifying known experts at extremism and terrorism. However,", 120.0),
                    _wide_line("l3", [word.word_id for word in row3_words], "the national, subnational and community through our networks, where possible,", 140.0),
                    _wide_line("l4", [word.word_id for word in row4_words], "level. The survey and interviews with we disseminated the survey throughout", 160.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text=(
                    "Thailand, Philippines and Indonesia in of the region that most experience violent\n"
                    "particular, identifying known experts at extremism and terrorism. However,\n"
                    "the national, subnational and community through our networks, where possible,\n"
                    "level. The survey and interviews with we disseminated the survey throughout"
                ),
                bbox=PdfBBox(x0=60.0, top=100.0, x1=560.0, bottom=174.0),
                line_ids=["l1", "l2", "l3", "l4"],
                reading_order_start=1,
                reading_order_end=4,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph"]


def test_detect_document_rejects_sparse_pymupdf_chart_false_positive():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    _wide_line("l1", [], "3,230 3,140 2,907", 100.0),
                    _wide_line("l2", [], "2,693", 120.0),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[PdfStructuredPage(meta=page_meta, blocks=[])],
        blocks=[
            PdfSemanticBlock(
                block_id="b1",
                block_type="paragraph",
                page_start=1,
                page_end=1,
                text="3,230 3,140 2,907\n2,693",
                bbox=PdfBBox(x0=60.0, top=100.0, x1=560.0, bottom=134.0),
                line_ids=["l1", "l2"],
                reading_order_start=1,
                reading_order_end=2,
            )
        ],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=55.0, top=96.0, x1=565.0, bottom=136.0),
                        row_count=2,
                        col_count=13,
                        cells=[
                            ["3,230", "3,140", "2,907", "", "", "", "", "", "", "", "", "", ""],
                            ["", "", "", "", "", "", "", "", "2,693", "", "", "", ""],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph"]


def test_detect_document_materializes_page_dominant_pymupdf_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    PdfResolvedLine(
                        line_id="title",
                        page=1,
                        text="Restrictions on Land Ownership by Foreigners in Selected Jurisdictions",
                        bbox=PdfBBox(x0=60.0, top=40.0, x1=520.0, bottom=56.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=1,
                    ),
                    PdfResolvedLine(
                        line_id="r1",
                        page=1,
                        text="Canada Y Y Prohibition on ownership of",
                        bbox=PdfBBox(x0=80.0, top=120.0, x1=640.0, bottom=136.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=2,
                    ),
                    PdfResolvedLine(
                        line_id="r2",
                        page=1,
                        text="Chile N Y Prohibition on acquisition of",
                        bbox=PdfBBox(x0=80.0, top=142.0, x1=640.0, bottom=158.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=3,
                    ),
                    PdfResolvedLine(
                        line_id="r3",
                        page=1,
                        text="China N (2001) N No individuals, domestic or",
                        bbox=PdfBBox(x0=80.0, top=164.0, x1=640.0, bottom=180.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=4,
                    ),
                ],
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=page_meta,
                blocks=[
                    PdfSemanticBlock(
                        block_id="heading",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Restrictions on Land Ownership by Foreigners in Selected Jurisdictions",
                        bbox=PdfBBox(x0=60.0, top=40.0, x1=520.0, bottom=56.0),
                        line_ids=["title"],
                        reading_order_start=1,
                        reading_order_end=1,
                    ),
                    PdfSemanticBlock(
                        block_id="tbl_a",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Canada Y Y Prohibition on ownership of",
                        bbox=PdfBBox(x0=80.0, top=120.0, x1=640.0, bottom=136.0),
                        line_ids=["r1"],
                        reading_order_start=2,
                        reading_order_end=2,
                    ),
                    PdfSemanticBlock(
                        block_id="tbl_b",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Chile N Y Prohibition on acquisition of",
                        bbox=PdfBBox(x0=80.0, top=142.0, x1=640.0, bottom=158.0),
                        line_ids=["r2"],
                        reading_order_start=3,
                        reading_order_end=3,
                    ),
                    PdfSemanticBlock(
                        block_id="tbl_c",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="China N (2001) N No individuals, domestic or",
                        bbox=PdfBBox(x0=80.0, top=164.0, x1=640.0, bottom=180.0),
                        line_ids=["r3"],
                        reading_order_start=4,
                        reading_order_end=4,
                    ),
                ],
            )
        ],
        blocks=[],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=72.0, top=112.0, x1=648.0, bottom=188.0),
                        row_count=4,
                        col_count=4,
                        cells=[
                            ["Jurisdiction", "GATS XVII Reservation (1994)", "Foreign Ownership Permitted", "Restrictions on Foreign Ownership"],
                            ["Canada", "Y", "Y", "Prohibition on ownership of residential property"],
                            ["Chile", "N", "Y", "Prohibition on acquisition of public lands"],
                            ["China", "N (2001)", "N", "No individuals, domestic or foreign, can privately own land."],
                        ],
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph", "table"]
    assert enriched.blocks[1].table_rows[0][0] == "Jurisdiction"
    assert enriched.blocks[1].table_rows[1][0] == "Canada"


def test_detect_document_preserves_existing_strong_table_block():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    PdfResolvedLine(
                        line_id="title",
                        page=1,
                        text="Restrictions on Land Ownership by Foreigners in Selected Jurisdictions",
                        bbox=PdfBBox(x0=60.0, top=40.0, x1=520.0, bottom=56.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=1,
                    ),
                    PdfResolvedLine(
                        line_id="foot",
                        page=1,
                        text="The Law Library of Congress",
                        bbox=PdfBBox(x0=80.0, top=745.0, x1=260.0, bottom=756.0),
                        word_ids=[],
                        avg_font_size=10.0,
                        dominant_font_name="Times",
                        band="footnote",
                        region="main",
                        column_id="main",
                        reading_order=2,
                    ),
                ],
            )
        ]
    )
    table_rows = [
        ["Jurisdiction", "GATS XVII Reservation (1994)", "Foreign Ownership Permitted", "Restrictions on Foreign Ownership"],
        ["Canada", "Y", "Y", "Prohibition on ownership of residential property"],
        ["Chile", "N", "Y", "Prohibition on acquisition of public lands"],
        ["China", "N (2001)", "N", "No individuals, domestic or foreign, can privately own land."],
    ]
    structured_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=page_meta,
                blocks=[
                    PdfSemanticBlock(
                        block_id="title",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="Restrictions on Land Ownership by Foreigners in Selected Jurisdictions",
                        bbox=PdfBBox(x0=60.0, top=40.0, x1=520.0, bottom=56.0),
                        line_ids=["title"],
                        reading_order_start=1,
                        reading_order_end=1,
                    ),
                    PdfSemanticBlock(
                        block_id="table",
                        block_type="table",
                        page_start=1,
                        page_end=1,
                        text="\n".join(" ".join(row) for row in table_rows),
                        bbox=PdfBBox(x0=72.0, top=74.0, x1=648.0, bottom=716.0),
                        line_ids=["r1", "r2", "r3", "r4"],
                        reading_order_start=2,
                        reading_order_end=5,
                        table_rows=table_rows,
                    ),
                    PdfSemanticBlock(
                        block_id="footer",
                        block_type="paragraph",
                        page_start=1,
                        page_end=1,
                        text="The Law Library of Congress",
                        bbox=PdfBBox(x0=80.0, top=745.0, x1=260.0, bottom=756.0),
                        line_ids=["foot"],
                        reading_order_start=6,
                        reading_order_end=6,
                    ),
                ],
            )
        ],
        blocks=[],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=72.0, top=72.0, x1=648.0, bottom=716.0),
                        row_count=4,
                        col_count=4,
                        cells=table_rows,
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["paragraph", "table", "paragraph"]
    assert enriched.blocks[1].table_rows == table_rows


def test_detect_document_does_not_merge_heading_into_existing_strong_table():
    detector = LocalPdfTableDetector()
    page_meta = PdfPageMeta(page=1, page_width=720.0, page_height=900.0, rotation=0)
    normalized_page = PdfNormalizedPage(meta=page_meta, kept_words=[], dropped_words=[], text_lines=[])
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=page_meta,
                lines=[
                    PdfResolvedLine(
                        line_id="hdr",
                        page=1,
                        text="Key Functions by Main Service Flow",
                        bbox=PdfBBox(x0=60.0, top=52.0, x1=420.0, bottom=68.0),
                        word_ids=[],
                        avg_font_size=12.0,
                        dominant_font_name="Times",
                        band="body",
                        region="main",
                        column_id="main",
                        reading_order=1,
                    ),
                ],
            )
        ]
    )
    table_rows = [
        ["1. Project creation", "Project creation and management", "Select document type to automatically run project creation", "Improving work efficiency"],
        ["2. Data labeling", "Data storage management", "Provides convenient functions for uploading raw data", "Conveniently manage raw data"],
        ["3. Pipeline configuration", "Pipeline, Endpoint Creation and management", "Choose Detector, Recognizer, or Parser to create a Pipeline", "Upgrade their own OCR model"],
        ["4. Monitoring and evaluation", "Project monitoring", "Monitoring of deployed Pipelines and Endpoints", "Quickly identify and respond to issues"],
    ]
    structured_document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=page_meta,
                blocks=[
                    PdfSemanticBlock(
                        block_id="hdr",
                        block_type="heading",
                        page_start=1,
                        page_end=1,
                        text="Key Functions by Main Service Flow",
                        bbox=PdfBBox(x0=60.0, top=52.0, x1=420.0, bottom=68.0),
                        line_ids=["hdr"],
                        reading_order_start=1,
                        reading_order_end=1,
                    ),
                    PdfSemanticBlock(
                        block_id="table",
                        block_type="table",
                        page_start=1,
                        page_end=1,
                        text="\n".join(" ".join(row) for row in table_rows),
                        bbox=PdfBBox(x0=30.0, top=116.0, x1=708.0, bottom=392.0),
                        line_ids=["r1", "r2", "r3", "r4"],
                        reading_order_start=2,
                        reading_order_end=5,
                        table_rows=table_rows,
                    ),
                ],
            )
        ],
        blocks=[],
        body_font_size=12.0,
    )

    enriched = detector.detect_document(
        page_atoms=[
            PdfPageAtoms(
                meta=page_meta,
                tables=[
                    PdfTableAtom(
                        table_id="ft0001",
                        bbox=PdfBBox(x0=30.0, top=110.0, x1=708.0, bottom=392.0),
                        row_count=4,
                        col_count=4,
                        cells=table_rows,
                    )
                ],
            )
        ],
        normalized_pages=[normalized_page],
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert [block.block_type for block in enriched.blocks] == ["heading", "table"]
    assert enriched.blocks[0].text == "Key Functions by Main Service Flow"
    assert enriched.blocks[1].table_rows == table_rows


def test_normalize_pymupdf_rows_collapses_sparse_multiline_header_and_drops_empty_columns():
    rows = LocalPdfTableDetector._normalize_pymupdf_rows(
        [
            [
                "Jurisdiction",
                "GATS XVII Reservation (1994)",
                "Foreign Ownership Permitted",
                "Restrictions on Foreign Ownership",
                "",
                "Foreign",
                "",
            ],
            ["", "", "", "", "", "Ownership", ""],
            ["", "", "", "", "", "Reporting", ""],
            ["", "", "", "", "", "Requirements", ""],
            [
                "Australia",
                "N",
                "Y",
                "Approval is needed from the Treasurer",
                "Acquisitions must be reported",
                "",
                "",
            ],
        ]
    )

    assert rows == [
        [
            "Jurisdiction",
            "GATS XVII Reservation (1994)",
            "Foreign Ownership Permitted",
            "Restrictions on Foreign Ownership",
            "Foreign Ownership Reporting Requirements",
        ],
        [
            "Australia",
            "N",
            "Y",
            "Approval is needed from the Treasurer",
            "Acquisitions must be reported",
        ],
    ]


def test_normalize_pymupdf_rows_splits_centered_dual_header():
    rows = LocalPdfTableDetector._normalize_pymupdf_rows(
        [
            ["", "Mitosis Meiosis", ""],
            ["", "(begins with a single cell) (begins with a single cell)", ""],
            ["# chromosomes in parent cells", "", ""],
        ]
    )

    assert rows == [
        ["", "Mitosis (begins with a single cell)", "Meiosis (begins with a single cell)"],
        ["# chromosomes in parent cells", "", ""],
    ]
