from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfDocumentResolver,
    PdfBBox,
    PdfNormalizedPage,
    PdfPageMeta,
    PdfTextBlockAtom,
    PdfTextLine,
    PdfWordAtom,
)


def _line(
    *,
    page: int,
    line_id: str,
    text: str,
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    band: str = "body",
) -> PdfTextLine:
    return PdfTextLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=12.0,
        dominant_font_name="Times",
        band=band,
    )


def _word(
    *,
    word_id: str,
    text: str,
    x0: float,
    top: float,
    x1: float,
    bottom: float,
) -> PdfWordAtom:
    return PdfWordAtom(
        word_id=word_id,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        doctop=top,
        font_name="Times",
        font_size=12.0,
    )


def test_resolve_document_drops_repeated_headers_and_footers():
    resolver = LocalPdfDocumentResolver()
    pages = [
        PdfNormalizedPage(
            meta=PdfPageMeta(page=index, page_width=600.0, page_height=800.0, rotation=0),
            text_lines=[
                _line(page=index, line_id=f"p{index}_h", text="Conference 2026", x0=40.0, top=24.0, x1=180.0, bottom=36.0, band="top_band"),
                _line(page=index, line_id=f"p{index}_b1", text=f"Body line {index}", x0=60.0, top=120.0, x1=220.0, bottom=132.0),
                _line(page=index, line_id=f"p{index}_f", text=str(index), x0=295.0, top=770.0, x1=305.0, bottom=782.0, band="bottom_band"),
            ],
        )
        for index in (1, 2, 3)
    ]

    resolved = resolver.resolve_document(pages=pages)

    assert resolved.header_signatures == ["conference #"]
    assert resolved.footer_signatures == ["#"]
    assert all(len(page.lines) == 1 for page in resolved.pages)
    assert all(page.lines[0].text.startswith("Body line") for page in resolved.pages)


def test_resolve_document_drops_page_markers_even_when_not_repeated():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="top_num", text="150", x0=286.0, top=24.0, x1=314.0, bottom=38.0, band="top_band"),
            _line(page=1, line_id="body", text="Main paragraph", x0=60.0, top=140.0, x1=220.0, bottom=154.0),
            _line(
                page=1,
                line_id="footer",
                text="Online Survey | 39",
                x0=420.0,
                top=768.0,
                x1=520.0,
                bottom=782.0,
                band="bottom_band",
            ),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert [line.text for line in resolved.pages[0].lines] == ["Main paragraph"]
    assert [entry["reason"] for entry in resolved.pages[0].dropped_lines] == ["page_marker", "page_marker"]


def test_resolve_document_orders_two_column_page_left_then_right():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="title", text="Paper Title", x0=120.0, top=40.0, x1=480.0, bottom=64.0, band="top_band"),
            _line(page=1, line_id="l1", text="Left one", x0=60.0, top=140.0, x1=190.0, bottom=154.0),
            _line(page=1, line_id="l2", text="Left two", x0=62.0, top=160.0, x1=192.0, bottom=174.0),
            _line(page=1, line_id="r1", text="Right one", x0=340.0, top=138.0, x1=490.0, bottom=152.0),
            _line(page=1, line_id="r2", text="Right two", x0=342.0, top=158.0, x1=492.0, bottom=172.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert resolved.pages[0].column_count == 2
    assert [line.text for line in resolved.pages[0].lines] == [
        "Paper Title",
        "Left one",
        "Left two",
        "Right one",
        "Right two",
    ]
    assert [line.column_id for line in resolved.pages[0].lines] == [
        "main",
        "left",
        "left",
        "right",
        "right",
    ]


def test_resolve_document_keeps_single_column_for_centered_front_matter_page():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="title1", text="Py-Calabi quasi-morphisms and quasi-states", x0=122.0, top=170.0, x1=418.0, bottom=188.0),
            _line(page=1, line_id="title2", text="on orientable surfaces of higher genus", x0=150.0, top=194.0, x1=450.0, bottom=212.0),
            _line(page=1, line_id="author", text="Maor Rosenberg", x0=252.0, top=236.0, x1=350.0, bottom=250.0),
            _line(page=1, line_id="date", text="October 30, 2018", x0=250.0, top=264.0, x1=360.0, bottom=278.0),
            _line(page=1, line_id="abstract_h", text="Abstract", x0=281.0, top=300.0, x1=335.0, bottom=314.0),
            _line(page=1, line_id="abstract_1", text="We show that Py-Calabi quasi-morphism on the group of Hamilto-", x0=156.0, top=330.0, x1=430.0, bottom=344.0),
            _line(page=1, line_id="abstract_2", text="nian diffeomorphisms of surfaces of higher genus gives rise to a quasi-", x0=140.0, top=344.0, x1=460.0, bottom=358.0),
            _line(page=1, line_id="abstract_3", text="state.", x0=140.0, top=358.0, x1=180.0, bottom=372.0),
            _line(page=1, line_id="intro_h", text="1 Introduction", x0=111.0, top=416.0, x1=220.0, bottom=430.0),
            _line(page=1, line_id="intro_1", text="In this paper we show a connection between quasi-morphisms and quasi-states.", x0=111.0, top=464.0, x1=502.0, bottom=478.0),
            _line(page=1, line_id="intro_2", text="The proof relies on hyperbolic geometry and combinatorial tools.", x0=111.0, top=482.0, x1=482.0, bottom=496.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert resolved.pages[0].column_count == 1
    assert [line.text for line in resolved.pages[0].lines[:6]] == [
        "Py-Calabi quasi-morphisms and quasi-states",
        "on orientable surfaces of higher genus",
        "Maor Rosenberg",
        "October 30, 2018",
        "Abstract",
        "We show that Py-Calabi quasi-morphism on the group of Hamilto-",
    ]


def test_resolve_document_keeps_single_column_for_centered_display_blocks():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="sec", text="1.2 Algebraic Results on Ham (M, ω)", x0=110.0, top=120.0, x1=320.0, bottom=134.0),
            _line(page=1, line_id="lead", text="The following algebraic results are due to Banyaga.", x0=110.0, top=148.0, x1=372.0, bottom=162.0),
            _line(page=1, line_id="thm", text="Theorem 1.3. Let M be an open manifold with an exact symplectic struc-", x0=110.0, top=176.0, x1=430.0, bottom=190.0),
            _line(page=1, line_id="eq_h", text="Cal M: Ham (M, ω) → R", x0=244.0, top=220.0, x1=360.0, bottom=234.0),
            _line(page=1, line_id="eq_i", text="defined as", x0=262.0, top=254.0, x1=320.0, bottom=268.0),
            _line(page=1, line_id="eq_1", text="Cal M (φ F) = F (x, t) ω m dt,", x0=220.0, top=276.0, x1=380.0, bottom=290.0),
            _line(page=1, line_id="eq_2", text="Z 0 Z M", x0=286.0, top=296.0, x1=330.0, bottom=310.0),
            _line(page=1, line_id="tail", text="whose kernel is equal to the commutator subgroup of Ham (M, ω).", x0=110.0, top=332.0, x1=456.0, bottom=346.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert resolved.pages[0].column_count == 1
    assert [line.text for line in resolved.pages[0].lines] == [
        "1.2 Algebraic Results on Ham (M, ω)",
        "The following algebraic results are due to Banyaga.",
        "Theorem 1.3. Let M be an open manifold with an exact symplectic struc-",
        "Cal M: Ham (M, ω) → R",
        "defined as",
        "Cal M (φ F) = F (x, t) ω m dt,",
        "Z 0 Z M",
        "whose kernel is equal to the commutator subgroup of Ham (M, ω).",
    ]


def test_resolve_document_xycut_keeps_full_width_block_between_column_groups():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="title", text="Paper Title", x0=120.0, top=40.0, x1=480.0, bottom=64.0, band="top_band"),
            _line(page=1, line_id="l1", text="Left intro", x0=60.0, top=140.0, x1=190.0, bottom=154.0),
            _line(page=1, line_id="r1", text="Right intro", x0=340.0, top=142.0, x1=490.0, bottom=156.0),
            _line(page=1, line_id="mid", text="2 Methods", x0=140.0, top=220.0, x1=470.0, bottom=238.0),
            _line(page=1, line_id="l2", text="Left methods", x0=60.0, top=280.0, x1=210.0, bottom=294.0),
            _line(page=1, line_id="r2", text="Right methods", x0=340.0, top=282.0, x1=500.0, bottom=296.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert [line.text for line in resolved.pages[0].lines] == [
        "Paper Title",
        "Left intro",
        "Right intro",
        "2 Methods",
        "Left methods",
        "Right methods",
    ]
    assert [line.column_id for line in resolved.pages[0].lines] == [
        "main",
        "left",
        "right",
        "main",
        "left",
        "right",
    ]


def test_resolve_document_splits_obvious_cross_boundary_rowwise_merge():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        kept_words=[
            _word(word_id="l1w1", text="Left", x0=60.0, top=140.0, x1=88.0, bottom=154.0),
            _word(word_id="l1w2", text="intro", x0=92.0, top=140.0, x1=128.0, bottom=154.0),
            _word(word_id="r1w1", text="Right", x0=340.0, top=142.0, x1=376.0, bottom=156.0),
            _word(word_id="r1w2", text="intro", x0=380.0, top=142.0, x1=416.0, bottom=156.0),
            _word(word_id="mw1", text="Left", x0=62.0, top=162.0, x1=88.0, bottom=176.0),
            _word(word_id="mw2", text="merged", x0=92.0, top=162.0, x1=138.0, bottom=176.0),
            _word(word_id="mw3", text="Right", x0=342.0, top=162.0, x1=378.0, bottom=176.0),
            _word(word_id="mw4", text="merged", x0=382.0, top=162.0, x1=428.0, bottom=176.0),
        ],
        text_lines=[
            _line(page=1, line_id="title", text="Paper Title", x0=120.0, top=40.0, x1=480.0, bottom=64.0, band="top_band"),
            PdfTextLine(
                line_id="l1",
                page=1,
                text="Left intro",
                bbox=PdfBBox(x0=60.0, top=140.0, x1=128.0, bottom=154.0),
                word_ids=["l1w1", "l1w2"],
                avg_font_size=12.0,
                dominant_font_name="Times",
                band="body",
            ),
            PdfTextLine(
                line_id="r1",
                page=1,
                text="Right intro",
                bbox=PdfBBox(x0=340.0, top=142.0, x1=416.0, bottom=156.0),
                word_ids=["r1w1", "r1w2"],
                avg_font_size=12.0,
                dominant_font_name="Times",
                band="body",
            ),
            PdfTextLine(
                line_id="merged",
                page=1,
                text="Left merged Right merged",
                bbox=PdfBBox(x0=62.0, top=162.0, x1=428.0, bottom=176.0),
                word_ids=["mw1", "mw2", "mw3", "mw4"],
                avg_font_size=12.0,
                dominant_font_name="Times",
                band="body",
            ),
            _line(page=1, line_id="l2", text="Left tail", x0=64.0, top=184.0, x1=136.0, bottom=198.0),
            _line(page=1, line_id="r2", text="Right tail", x0=344.0, top=186.0, x1=424.0, bottom=200.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert [line.text for line in resolved.pages[0].lines] == [
        "Paper Title",
        "Left intro",
        "Left merged",
        "Left tail",
        "Right intro",
        "Right merged",
        "Right tail",
    ]


def test_resolve_document_prefers_text_block_column_flow_for_complex_two_column_page():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        text_blocks=[
            PdfTextBlockAtom(
                block_id="tb_left_ack",
                bbox=PdfBBox(x0=70.0, top=72.0, x1=290.0, bottom=280.0),
                text="Acknowledgements\nWe would like to extend our gratitude",
                line_count=10,
            ),
            PdfTextBlockAtom(
                block_id="tb_left_lim",
                bbox=PdfBBox(x0=70.0, top=294.0, x1=290.0, bottom=760.0),
                text="Limitations\nOur study has important limitations",
                line_count=18,
            ),
            PdfTextBlockAtom(
                block_id="tb_right_tail",
                bbox=PdfBBox(x0=306.0, top=72.0, x1=474.0, bottom=88.0),
                text="and development in the field of LLMs.",
                line_count=1,
            ),
            PdfTextBlockAtom(
                block_id="tb_right_ethics",
                bbox=PdfBBox(x0=306.0, top=96.0, x1=526.0, bottom=512.0),
                text="Ethics Statement\nWe conscientiously address and emphasize",
                line_count=16,
            ),
        ],
        text_lines=[
            _line(page=1, line_id="ack_h", text="Acknowledgements", x0=70.0, top=72.0, x1=170.0, bottom=84.0),
            _line(page=1, line_id="ack_p", text="We would like to extend our gratitude", x0=70.0, top=96.0, x1=286.0, bottom=108.0),
            _line(page=1, line_id="lim_h", text="Limitations", x0=70.0, top=294.0, x1=130.0, bottom=306.0),
            _line(page=1, line_id="lim_p", text="Our study has important limitations", x0=70.0, top=318.0, x1=286.0, bottom=330.0),
            _line(page=1, line_id="tail", text="and development in the field of LLMs.", x0=306.0, top=73.0, x1=474.0, bottom=85.0),
            _line(page=1, line_id="eth_h", text="Ethics Statement", x0=306.0, top=97.0, x1=393.0, bottom=109.0),
            _line(page=1, line_id="eth_p", text="We conscientiously address and emphasize", x0=306.0, top=120.0, x1=522.0, bottom=132.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert resolved.pages[0].column_count == 2
    assert [line.text for line in resolved.pages[0].lines] == [
        "Acknowledgements",
        "We would like to extend our gratitude",
        "Limitations",
        "Our study has important limitations",
        "and development in the field of LLMs.",
        "Ethics Statement",
        "We conscientiously address and emphasize",
    ]


def test_resolve_document_avoids_two_column_layout_for_three_cluster_grid_page():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0),
        text_lines=[
            _line(page=1, line_id="h1", text="Service Stage", x0=40.0, top=120.0, x1=120.0, bottom=134.0),
            _line(page=1, line_id="h2", text="Function Name", x0=220.0, top=120.0, x1=320.0, bottom=134.0),
            _line(page=1, line_id="h3", text="Explanation", x0=430.0, top=120.0, x1=520.0, bottom=134.0),
            _line(page=1, line_id="r1c1", text="1. Create project", x0=40.0, top=150.0, x1=150.0, bottom=164.0),
            _line(page=1, line_id="r1c2", text="Project creation", x0=220.0, top=150.0, x1=330.0, bottom=164.0),
            _line(page=1, line_id="r1c3", text="Create project with template", x0=430.0, top=150.0, x1=620.0, bottom=164.0),
            _line(page=1, line_id="r2c1", text="2. Label data", x0=40.0, top=180.0, x1=130.0, bottom=194.0),
            _line(page=1, line_id="r2c2", text="Data labeling", x0=220.0, top=180.0, x1=305.0, bottom=194.0),
            _line(page=1, line_id="r2c3", text="Upload, tag, and annotate files", x0=430.0, top=180.0, x1=620.0, bottom=194.0),
            _line(page=1, line_id="r3c1", text="3. Deploy", x0=40.0, top=210.0, x1=100.0, bottom=224.0),
            _line(page=1, line_id="r3c2", text="Endpoint deploy", x0=220.0, top=210.0, x1=315.0, bottom=224.0),
            _line(page=1, line_id="r3c3", text="Deploy OCR endpoint", x0=430.0, top=210.0, x1=560.0, bottom=224.0),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert resolved.pages[0].column_count == 1
    assert [line.column_id for line in resolved.pages[0].lines[:6]] == ["main"] * 6


def test_resolve_document_skips_cross_boundary_split_for_large_table_page():
    resolver = LocalPdfDocumentResolver()
    page = PdfNormalizedPage(
        meta=PdfPageMeta(page=1, page_width=700.0, page_height=900.0, rotation=0),
        table_bboxes=[PdfBBox(x0=80.0, top=140.0, x1=620.0, bottom=520.0)],
        kept_words=[
            _word(word_id="w1", text="State", x0=100.0, top=150.0, x1=150.0, bottom=164.0),
            _word(word_id="w2", text="Number", x0=290.0, top=150.0, x1=340.0, bottom=164.0),
            _word(word_id="w3", text="of", x0=344.0, top=150.0, x1=356.0, bottom=164.0),
            _word(word_id="w4", text="clauses", x0=360.0, top=150.0, x1=410.0, bottom=164.0),
            _word(word_id="w5", text="Gujarat", x0=100.0, top=180.0, x1=150.0, bottom=194.0),
            _word(word_id="w6", text="1469", x0=300.0, top=180.0, x1=330.0, bottom=194.0),
        ],
        text_lines=[
            _line(page=1, line_id="h1", text="State", x0=100.0, top=150.0, x1=150.0, bottom=164.0),
            _line(page=1, line_id="h2", text="Number of clauses", x0=290.0, top=150.0, x1=390.0, bottom=164.0),
            _line(page=1, line_id="h3", text="Percentage", x0=450.0, top=150.0, x1=520.0, bottom=164.0),
            _line(page=1, line_id="r1c1", text="Gujarat", x0=100.0, top=180.0, x1=150.0, bottom=194.0),
            _line(page=1, line_id="r1c2", text="1469", x0=300.0, top=180.0, x1=330.0, bottom=194.0),
            _line(page=1, line_id="r1c3", text="15.6%", x0=450.0, top=180.0, x1=485.0, bottom=194.0),
            _line(page=1, line_id="r2c1", text="Punjab", x0=100.0, top=210.0, x1=150.0, bottom=224.0),
            _line(page=1, line_id="r2c2", text="1273", x0=300.0, top=210.0, x1=330.0, bottom=224.0),
            _line(page=1, line_id="r2c3", text="5.3%", x0=450.0, top=210.0, x1=480.0, bottom=224.0),
            _line(page=1, line_id="r3c1", text="Tamil Nadu", x0=100.0, top=240.0, x1=180.0, bottom=254.0),
            _line(page=1, line_id="r3c2", text="1043", x0=300.0, top=240.0, x1=330.0, bottom=254.0),
            _line(page=1, line_id="r3c3", text="16.3%", x0=450.0, top=240.0, x1=485.0, bottom=254.0),
            PdfTextLine(
                line_id="merged_row",
                page=1,
                text="State Number of clauses",
                bbox=PdfBBox(x0=100.0, top=150.0, x1=390.0, bottom=164.0),
                word_ids=["w1", "w2", "w3", "w4"],
                avg_font_size=12.0,
                dominant_font_name="Times",
                band="body",
            ),
        ],
    )

    resolved = resolver.resolve_document(pages=[page])

    assert any(line.text == "State Number of clauses" for line in resolved.pages[0].lines)
