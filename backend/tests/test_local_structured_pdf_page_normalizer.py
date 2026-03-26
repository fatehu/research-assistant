from __future__ import annotations

import fitz

from app.services.local_structured_pdf import (
    LocalPdfNativeExtractor,
    LocalPdfPageNormalizer,
    PdfBBox,
    PdfPageAtoms,
    PdfPageMeta,
    PdfTextBlockAtom,
    PdfWordAtom,
)


def test_normalize_page_groups_words_into_line(tmp_path):
    pdf_path = tmp_path / "line.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Alpha beta gamma", fontsize=14)
    page.insert_text((72, 128), "Delta epsilon", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    normalizer = LocalPdfPageNormalizer()
    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert len(normalized.text_lines) >= 2
    assert normalized.text_lines[0].text == "Alpha beta gamma"
    assert normalized.text_lines[1].text == "Delta epsilon"
    assert normalized.text_lines[0].band == "top_band"


def test_normalize_page_drops_tiny_words():
    normalizer = LocalPdfPageNormalizer(tiny_font_threshold=1.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=400.0, page_height=600.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="Visible",
                bbox=PdfBBox(x0=40.0, top=80.0, x1=90.0, bottom=92.0),
                doctop=80.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="Noise",
                bbox=PdfBBox(x0=40.0, top=100.0, x1=60.0, bottom=100.8),
                doctop=100.0,
                font_name="Times",
                font_size=0.8,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.kept_words] == ["Visible"]
    assert normalized.dropped_words[0]["reason"] == "tiny_word"


def test_normalize_page_splits_far_apart_words_on_same_row():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=24.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="Left",
                bbox=PdfBBox(x0=40.0, top=200.0, x1=70.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="column",
                bbox=PdfBBox(x0=74.0, top=200.3, x1=124.0, bottom=212.3),
                doctop=200.3,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="Right",
                bbox=PdfBBox(x0=340.0, top=200.4, x1=375.0, bottom=212.4),
                doctop=200.4,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="column",
                bbox=PdfBBox(x0=379.0, top=200.2, x1=429.0, bottom=212.2),
                doctop=200.2,
                font_name="Times",
                font_size=12.0,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == ["Left column", "Right column"]


def test_normalize_page_does_not_join_words_across_different_text_blocks():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=80.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="Left",
                bbox=PdfBBox(x0=70.0, top=200.0, x1=95.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="column",
                bbox=PdfBBox(x0=100.0, top=200.0, x1=145.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="Right",
                bbox=PdfBBox(x0=320.0, top=200.0, x1=355.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="column",
                bbox=PdfBBox(x0=360.0, top=200.0, x1=405.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
        ],
        text_blocks=[
            PdfTextBlockAtom(
                block_id="tb_left",
                bbox=PdfBBox(x0=60.0, top=190.0, x1=180.0, bottom=220.0),
                text="Left column",
                line_count=1,
            ),
            PdfTextBlockAtom(
                block_id="tb_right",
                bbox=PdfBBox(x0=300.0, top=190.0, x1=430.0, bottom=220.0),
                text="Right column",
                line_count=1,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == ["Left column", "Right column"]


def test_normalize_page_merges_table_like_fragments_on_same_baseline():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=24.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="SFT",
                bbox=PdfBBox(x0=40.0, top=200.0, x1=62.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="v2",
                bbox=PdfBBox(x0=66.0, top=200.2, x1=80.0, bottom=212.2),
                doctop=200.2,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="O",
                bbox=PdfBBox(x0=116.0, top=200.1, x1=122.0, bottom=212.1),
                doctop=200.1,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="O",
                bbox=PdfBBox(x0=150.0, top=200.0, x1=156.0, bottom=212.0),
                doctop=200.0,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000005",
                text="✗",
                bbox=PdfBBox(x0=280.0, top=200.3, x1=286.0, bottom=212.3),
                doctop=200.3,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000006",
                text="69.21",
                bbox=PdfBBox(x0=318.0, top=200.2, x1=350.0, bottom=212.2),
                doctop=200.2,
                font_name="Times",
                font_size=12.0,
            ),
            PdfWordAtom(
                word_id="w000007",
                text="65.36",
                bbox=PdfBBox(x0=380.0, top=200.1, x1=412.0, bottom=212.1),
                doctop=200.1,
                font_name="Times",
                font_size=12.0,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == ["SFT v2 O O ✗ 69.21 65.36"]


def test_normalize_page_splits_large_gap_before_enumerated_heading_fragment():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=36.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="results",
                bbox=PdfBBox(x0=196.62, top=398.65, x1=224.54, bottom=409.56),
                doctop=398.65,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="in",
                bbox=PdfBBox(x0=226.73, top=398.65, x1=235.05, bottom=409.56),
                doctop=398.65,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="a",
                bbox=PdfBBox(x0=237.24, top=398.65, x1=241.98, bottom=409.56),
                doctop=398.65,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="model",
                bbox=PdfBBox(x0=244.18, top=398.65, x1=270.90, bottom=409.56),
                doctop=398.65,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000005",
                text="that",
                bbox=PdfBBox(x0=273.10, top=398.65, x1=289.13, bottom=409.56),
                doctop=398.65,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000006",
                text="4.3.2",
                bbox=PdfBBox(x0=306.14, top=398.58, x1=327.96, bottom=409.49),
                doctop=398.58,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000007",
                text="Alignment",
                bbox=PdfBBox(x0=338.87, top=398.58, x1=387.96, bottom=409.49),
                doctop=398.58,
                font_name="Times",
                font_size=10.91,
            ),
            PdfWordAtom(
                word_id="w000008",
                text="Tuning",
                bbox=PdfBBox(x0=390.69, top=398.58, x1=423.64, bottom=409.49),
                doctop=398.58,
                font_name="Times",
                font_size=10.91,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == [
        "results in a model that",
        "4.3.2 Alignment Tuning",
    ]


def test_normalize_page_splits_heading_from_smaller_font_followup_fragment():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=36.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="4",
                bbox=PdfBBox(x0=56.69, top=521.05, x1=61.80, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="Al-Sadu",
                bbox=PdfBBox(x0=79.37, top=521.04, x1=113.72, bottom=532.04),
                doctop=521.04,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="Symbols",
                bbox=PdfBBox(x0=115.79, top=521.05, x1=152.22, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="and",
                bbox=PdfBBox(x0=154.29, top=521.05, x1=170.78, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000005",
                text="Social",
                bbox=PdfBBox(x0=172.85, top=521.05, x1=198.93, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000006",
                text="Significance",
                bbox=PdfBBox(x0=201.00, top=521.05, x1=254.06, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000007",
                text="weavings,",
                bbox=PdfBBox(x0=303.31, top=525.13, x1=336.44, bottom=534.13),
                doctop=525.13,
                font_name="Times",
                font_size=9.0,
            ),
            PdfWordAtom(
                word_id="w000008",
                text="see",
                bbox=PdfBBox(x0=339.17, top=525.13, x1=350.21, bottom=534.13),
                doctop=525.13,
                font_name="Times",
                font_size=9.0,
            ),
            PdfWordAtom(
                word_id="w000009",
                text="also",
                bbox=PdfBBox(x0=352.94, top=525.13, x1=366.61, bottom=534.13),
                doctop=525.13,
                font_name="Times",
                font_size=9.0,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == [
        "4 Al-Sadu Symbols and Social Significance",
        "weavings, see also",
    ]


def test_normalize_page_keeps_numeric_heading_prefix_with_title_fragment():
    normalizer = LocalPdfPageNormalizer(same_line_gap_threshold=36.0)
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="4",
                bbox=PdfBBox(x0=56.69, top=521.05, x1=61.80, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="Al-Sadu",
                bbox=PdfBBox(x0=79.37, top=521.04, x1=113.72, bottom=532.04),
                doctop=521.04,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="Symbols",
                bbox=PdfBBox(x0=115.79, top=521.05, x1=152.22, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="and",
                bbox=PdfBBox(x0=154.29, top=521.05, x1=170.78, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000005",
                text="Social",
                bbox=PdfBBox(x0=172.85, top=521.05, x1=198.93, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
            PdfWordAtom(
                word_id="w000006",
                text="Significance",
                bbox=PdfBBox(x0=201.00, top=521.05, x1=254.06, bottom=532.05),
                doctop=521.05,
                font_name="Times-Bold",
                font_size=11.0,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == [
        "4 Al-Sadu Symbols and Social Significance",
    ]


def test_normalize_page_drops_vertical_margin_metadata_words():
    normalizer = LocalPdfPageNormalizer()
    page_atoms = PdfPageAtoms(
        meta=PdfPageMeta(page=1, page_width=612.0, page_height=792.0, rotation=0),
        words=[
            PdfWordAtom(
                word_id="w000001",
                text="0705.4297v2",
                bbox=PdfBBox(x0=16.3, top=388.9, x1=36.3, bottom=448.9),
                doctop=388.9,
                font_name="Times",
                font_size=10.0,
            ),
            PdfWordAtom(
                word_id="w000002",
                text="r",
                bbox=PdfBBox(x0=16.3, top=225.6, x1=36.3, bottom=232.3),
                doctop=225.6,
                font_name="Times",
                font_size=6.7,
            ),
            PdfWordAtom(
                word_id="w000003",
                text="M",
                bbox=PdfBBox(x0=16.3, top=241.1, x1=36.3, bottom=258.9),
                doctop=241.1,
                font_name="Times",
                font_size=17.8,
            ),
            PdfWordAtom(
                word_id="w000004",
                text="1.",
                bbox=PdfBBox(x0=165.8, top=326.2, x1=172.0, bottom=336.7),
                doctop=326.2,
                font_name="Times-Bold",
                font_size=10.0,
            ),
            PdfWordAtom(
                word_id="w000005",
                text="Introduction",
                bbox=PdfBBox(x0=176.0, top=326.2, x1=246.3, bottom=336.7),
                doctop=326.2,
                font_name="Times-Bold",
                font_size=10.0,
            ),
        ],
    )

    normalized = normalizer.normalize_page(page_atoms=page_atoms)

    assert [item.text for item in normalized.text_lines] == ["1. Introduction"]
    assert [item["reason"] for item in normalized.dropped_words] == ["vertical_margin_text"]
