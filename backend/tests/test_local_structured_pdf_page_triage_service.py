from __future__ import annotations

import pathlib
import sys


# Import triage bits directly without importing the local_structured_pdf package __init__,
# which pulls in optional runtime dependencies not needed for unit tests.
_SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "local_structured_pdf"
sys.path.insert(0, str(_SERVICE_DIR))

from contracts import (  # type: ignore  # noqa: E402
    PdfBBox,
    PdfHybridTriageDocument,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
    PdfTableAtom,
    PdfTextLine,
    PdfWordAtom,
)
from page_triage_service import LocalPdfPageTriageService  # type: ignore  # noqa: E402


def _word(*, word_id: str, text: str, x0: float, top: float, x1: float, bottom: float) -> PdfWordAtom:
    return PdfWordAtom(
        word_id=word_id,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        doctop=top,
        font_name="Times",
        font_size=12.0,
    )


def _text_line(
    *,
    page: int,
    line_id: str,
    text: str,
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    band: str = "body",
    word_count: int = 2,
) -> PdfTextLine:
    return PdfTextLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w{i}" for i in range(1, max(1, int(word_count)) + 1)],
        avg_font_size=12.0,
        dominant_font_name="Times",
        band=band,
    )


def _resolved_line(*, page: int, line_id: str, text: str, x0: float, top: float, x1: float, bottom: float, column_id: str = "main") -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=12.0,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id=column_id,
        reading_order=1,
    )


def _block(*, block_id: str, block_type: str, page: int, text: str, x0: float, top: float, x1: float, bottom: float) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        line_ids=[f"{block_id}:line"],
        avg_font_size=12.0,
    )


def test_triage_document_routes_dense_table_page_to_backend():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            words=[_word(word_id="w1", text="Metric", x0=80, top=120, x1=120, bottom=132)],
            lines=[object() for _ in range(10)],  # type: ignore[list-item]
            tables=[PdfTableAtom(table_id="t1", bbox=PdfBBox(x0=70.0, top=110.0, x1=530.0, bottom=520.0), row_count=6, col_count=4)],
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            table_bboxes=[PdfBBox(x0=70.0, top=110.0, x1=530.0, bottom=520.0)],
            text_lines=[_text_line(page=1, line_id="l1", text="Metric Value", x0=80, top=120, x1=180, bottom=132)],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[_resolved_line(page=1, line_id="l1", text="Metric Value", x0=80, top=120, x1=180, bottom=132)],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[_block(block_id="b1", block_type="table", page=1, text="Metric | Value", x0=70, top=110, x1=530, bottom=520)],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert isinstance(triage, PdfHybridTriageDocument)
    assert triage.backend_pages == [1]
    assert triage.pages[0].page_type == "dense_table"
    assert "table_signal" in triage.pages[0].reasons


def test_triage_document_does_not_route_backend_from_structured_table_block_only():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="A simple page",
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[_text_line(page=1, line_id="l1", text="A simple page", x0=80, top=120, x1=220, bottom=132)],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[_resolved_line(page=1, line_id="l1", text="A simple page", x0=80, top=120, x1=220, bottom=132)],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="t1", block_type="table", page=1, text="Cell A | Cell B", x0=70, top=110, x1=530, bottom=220),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].decision == "local"
    assert "table_signal" not in triage.pages[0].reasons


def test_triage_document_routes_visual_page_to_backend():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Poster",
            images=[],
        )
    ]
    atoms[0].images.append(
        type(
            "Img",
            (),
            {"bbox": PdfBBox(x0=80.0, top=120.0, x1=520.0, bottom=600.0)},
        )()
    )
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[_text_line(page=1, line_id="title", text="Poster Title", x0=120, top=60, x1=420, bottom=84)],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[PdfResolvedPage(meta=atoms[0].meta, lines=[], column_count=1)]
    )
    structured_document = PdfStructuredDocument(body_font_size=14.0, pages=[PdfStructuredPage(meta=atoms[0].meta, blocks=[])])

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.backend_pages == [1]
    assert triage.pages[0].page_type == "visual_or_scanned"
    assert "large_image_signal" in triage.pages[0].reasons


def test_triage_document_keeps_large_image_page_local_when_local_text_is_sufficient():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Figure heavy page with usable body text",
            images=[],
        )
    ]
    # Large image (about 65% of the page area) but the page also has plenty of readable text lines.
    atoms[0].images.append(
        type(
            "Img",
            (),
            {"bbox": PdfBBox(x0=30.0, top=120.0, x1=570.0, bottom=720.0)},
        )()
    )
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(
                    page=1,
                    line_id=f"l{i}",
                    text="This is a normal sentence with several words.",
                    x0=70,
                    top=40 + i * 18,
                    x1=520,
                    bottom=52 + i * 18,
                )
                for i in range(1, 26)
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="l1", text="This is a normal sentence with several words.", x0=70, top=58, x1=520, bottom=70),
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(body_font_size=12.0, pages=[PdfStructuredPage(meta=atoms[0].meta, blocks=[])])

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].decision == "local"
    assert triage.pages[0].page_type != "visual_or_scanned"
    assert "large_image_signal" in triage.pages[0].reasons


def test_triage_document_keeps_light_visual_page_local_for_usable_native_text():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            # Models 01030000000107-like pages: light visual layout, two medium figures, and
            # a short but readable native-text footprint that should stay on the local path.
            meta=PdfPageMeta(page=1, page_width=1000.0, page_height=1000.0, rotation=0),
            extract_text_raw="Readable figure page with native text.",
            images=[
                type("Img", (), {"bbox": PdfBBox(x0=60.0, top=180.0, x1=500.0, bottom=730.0)})(),
                type("Img", (), {"bbox": PdfBBox(x0=520.0, top=170.0, x1=940.0, bottom=740.0)})(),
            ],
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(
                    page=1,
                    line_id=f"l{i}",
                    text="This caption line has enough readable native words.",
                    x0=80,
                    top=40 + i * 22,
                    x1=620,
                    bottom=54 + i * 22,
                    word_count=7,
                )
                for i in range(1, 6)
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="l1",
                        text="This caption line has enough readable native words.",
                        x0=80,
                        top=62,
                        x1=620,
                        bottom=76,
                    )
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(
                        block_id="b1",
                        block_type="heading",
                        page=1,
                        text="Readable figure page",
                        x0=80,
                        top=40,
                        x1=280,
                        bottom=56,
                    ),
                    _block(
                        block_id="b2",
                        block_type="paragraph",
                        page=1,
                        text="This caption line has enough readable native words.",
                        x0=80,
                        top=62,
                        x1=620,
                        bottom=76,
                    ),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].decision == "local"
    assert triage.pages[0].page_type == "plain_text"
    assert "large_image_signal" in triage.pages[0].reasons


def test_triage_document_routes_vector_heavy_sparse_text_page_to_backend():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=1728.0, page_height=2592.0, rotation=0),
            extract_text_raw="and.org",
            images=[
                type(
                    "Img",
                    (),
                    {"bbox": PdfBBox(x0=1200.0, top=1200.0, x1=1368.0, bottom=1368.0)},
                )()
            ],
            rects=[object() for _ in range(60)],  # type: ignore[list-item]
            curves=[object() for _ in range(240)],  # type: ignore[list-item]
            lines=[object() for _ in range(120)],  # type: ignore[list-item]
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(
                    page=1,
                    line_id="l1",
                    text="and.org",
                    x0=800,
                    top=2478,
                    x1=1136,
                    bottom=2516,
                    band="bottom_band",
                )
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="l1",
                        text="and.org",
                        x0=800,
                        top=2478,
                        x1=1136,
                        bottom=2516,
                    )
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=38.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(
                        block_id="b1",
                        block_type="paragraph",
                        page=1,
                        text="and.org",
                        x0=800,
                        top=2478,
                        x1=1136,
                        bottom=2516,
                    )
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.backend_pages == [1]
    assert triage.pages[0].page_type == "visual_or_scanned"


def test_triage_document_keeps_plain_text_page_local_for_vector_noise_pattern():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            # Models 01030000000165-like pages: plain text with lots of vector/curve noise and
            # tiny non-dominant images that should not trigger backend routing by themselves.
            meta=PdfPageMeta(page=1, page_width=612.0, page_height=792.0, rotation=0),
            extract_text_raw="Plain text page with vector noise",
            images=[
                type("Img", (), {"bbox": PdfBBox(x0=520.0, top=60.0, x1=538.0, bottom=78.0)})(),
                type("Img", (), {"bbox": PdfBBox(x0=520.0, top=88.0, x1=538.0, bottom=106.0)})(),
            ],
            lines=[object() for _ in range(10)],  # type: ignore[list-item]
            rects=[object() for _ in range(10)],  # type: ignore[list-item]
            curves=[object() for _ in range(218)],  # type: ignore[list-item]
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(
                    page=1,
                    line_id=f"l{i}",
                    text="This body line represents readable plain text with many words in it.",
                    x0=72,
                    top=90 + i * 18,
                    x1=540,
                    bottom=102 + i * 18,
                    word_count=11,
                )
                for i in range(1, 28)
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(
                        page=1,
                        line_id="l1",
                        text="This body line represents readable plain text with many words in it.",
                        x0=72,
                        top=108,
                        x1=540,
                        bottom=120,
                    )
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(
                        block_id="h1",
                        block_type="heading",
                        page=1,
                        text="Plain text page",
                        x0=72,
                        top=72,
                        x1=220,
                        bottom=86,
                    ),
                    _block(
                        block_id="p1",
                        block_type="paragraph",
                        page=1,
                        text="This body line represents readable plain text with many words in it.",
                        x0=72,
                        top=108,
                        x1=540,
                        bottom=120,
                    ),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].page_type == "plain_text"
    assert triage.pages[0].decision == "local"
    assert "vector_grid_signal" not in triage.pages[0].reasons


def test_triage_document_keeps_plain_text_page_local_for_weak_vector_grid_signal():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Plain text page",
            lines=[object() for _ in range(8)],  # type: ignore[list-item]
            rects=[object() for _ in range(2)],  # type: ignore[list-item]
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=1, line_id="l1", text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                _text_line(page=1, line_id="l2", text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                _text_line(page=1, line_id="l3", text="Yet another paragraph line.", x0=80, top=160, x1=330, bottom=172),
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="l1", text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                    _resolved_line(page=1, line_id="l2", text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="b1", block_type="paragraph", page=1, text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                    _block(block_id="b2", block_type="paragraph", page=1, text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].page_type == "plain_text"
    assert triage.pages[0].decision == "local"
    assert "vector_grid_signal" not in triage.pages[0].reasons


def test_triage_document_keeps_plain_text_page_local_for_strong_vector_grid_signal():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=612.0, page_height=792.0, rotation=0),
            extract_text_raw="Plain text page",
            lines=[object() for _ in range(18)],  # type: ignore[list-item]
            rects=[object() for _ in range(80)],  # type: ignore[list-item]
            curves=[object() for _ in range(160)],  # type: ignore[list-item]
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=1, line_id="l1", text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                _text_line(page=1, line_id="l2", text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                _text_line(page=1, line_id="l3", text="Yet another paragraph line.", x0=80, top=160, x1=330, bottom=172),
                _text_line(page=1, line_id="l4", text="More body text.", x0=80, top=180, x1=260, bottom=192),
                _text_line(page=1, line_id="l5", text="Still body text.", x0=80, top=200, x1=240, bottom=212),
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="l1", text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                    _resolved_line(page=1, line_id="l2", text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="b1", block_type="paragraph", page=1, text="A simple paragraph line.", x0=80, top=120, x1=320, bottom=132),
                    _block(block_id="b2", block_type="paragraph", page=1, text="Another simple paragraph line.", x0=80, top=140, x1=340, bottom=152),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].page_type == "plain_text"
    assert triage.pages[0].decision == "local"
    assert "vector_grid_signal" not in triage.pages[0].reasons


def test_triage_document_keeps_plain_text_page_local_for_single_table_atom_without_table_layout():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Plain text page with a small table atom",
            tables=[
                PdfTableAtom(
                    table_id="t1",
                    bbox=PdfBBox(x0=420.0, top=640.0, x1=480.0, bottom=690.0),
                    row_count=2,
                    col_count=2,
                )
            ],
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=1, line_id=f"l{i}", text=f"Body paragraph line {i}.", x0=80, top=100 + i * 18, x1=360, bottom=112 + i * 18)
                for i in range(1, 31)
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="l1", text="Body paragraph line 1.", x0=80, top=100, x1=360, bottom=112),
                    _resolved_line(page=1, line_id="l2", text="Body paragraph line 2.", x0=80, top=118, x1=360, bottom=130),
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="b1", block_type="paragraph", page=1, text="Body paragraph line 1.", x0=80, top=100, x1=360, bottom=112),
                    _block(block_id="b2", block_type="paragraph", page=1, text="Body paragraph line 2.", x0=80, top=118, x1=360, bottom=130),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].page_type == "plain_text"
    assert triage.pages[0].decision == "local"
    assert "table_signal" in triage.pages[0].reasons


def test_triage_document_keeps_mixed_layout_page_local_without_strong_signals():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Mixed layout page",
            images=[
                type(
                    "Img",
                    (),
                    {"bbox": PdfBBox(x0=440.0, top=80.0, x1=500.0, bottom=140.0)},
                )()
            ],
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=1, line_id="l1", text="Left body", x0=70, top=120, x1=180, bottom=132),
                _text_line(page=1, line_id="r1", text="Right body", x0=340, top=122, x1=460, bottom=134),
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="l1", text="Left body", x0=70, top=120, x1=180, bottom=132, column_id="left"),
                    _resolved_line(page=1, line_id="r1", text="Right body", x0=340, top=122, x1=460, bottom=134, column_id="right"),
                ],
                column_count=2,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="b1", block_type="paragraph", page=1, text="Left body", x0=70, top=120, x1=180, bottom=132),
                    _block(block_id="b2", block_type="paragraph", page=1, text="Right body", x0=340, top=122, x1=460, bottom=134),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].page_type == "mixed_layout"
    assert triage.pages[0].decision == "local"


def test_triage_document_prefers_local_for_tagged_plain_text_page():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Title\nBody paragraph",
            has_struct_tree=True,
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=1, line_id="h1", text="Introduction", x0=80, top=100, x1=180, bottom=114),
                _text_line(page=1, line_id="p1", text="A regular paragraph line.", x0=80, top=130, x1=320, bottom=144),
                _text_line(page=1, line_id="p2", text="Another paragraph line.", x0=80, top=148, x1=300, bottom=162),
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=1, line_id="h1", text="Introduction", x0=80, top=100, x1=180, bottom=114),
                    _resolved_line(page=1, line_id="p1", text="A regular paragraph line.", x0=80, top=130, x1=320, bottom=144),
                ],
                column_count=1,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="h1", block_type="heading", page=1, text="Introduction", x0=80, top=100, x1=180, bottom=114),
                    _block(block_id="p1", block_type="paragraph", page=1, text="A regular paragraph line.", x0=80, top=130, x1=320, bottom=144),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [1]
    assert triage.pages[0].decision == "local"
    assert triage.pages[0].confidence < 0.95


def test_triage_document_keeps_formula_heavy_two_column_page_local_without_strong_signals():
    service = LocalPdfPageTriageService()
    atoms = [
        PdfPageAtoms(
            meta=PdfPageMeta(page=2, page_width=600.0, page_height=800.0, rotation=0),
            extract_text_raw="Equation heavy page",
        )
    ]
    normalized_pages = [
        PdfNormalizedPage(
            meta=atoms[0].meta,
            text_lines=[
                _text_line(page=2, line_id="l1", text="Left body", x0=70, top=120, x1=180, bottom=132),
                _text_line(page=2, line_id="r1", text="Right body", x0=340, top=122, x1=460, bottom=134),
            ],
        )
    ]
    resolved_document = PdfResolvedDocument(
        pages=[
            PdfResolvedPage(
                meta=atoms[0].meta,
                lines=[
                    _resolved_line(page=2, line_id="l1", text="Left body", x0=70, top=120, x1=180, bottom=132, column_id="left"),
                    _resolved_line(page=2, line_id="r1", text="Right body", x0=340, top=122, x1=460, bottom=134, column_id="right"),
                ],
                column_count=2,
            )
        ]
    )
    structured_document = PdfStructuredDocument(
        body_font_size=12.0,
        pages=[
            PdfStructuredPage(
                meta=atoms[0].meta,
                blocks=[
                    _block(block_id="e1", block_type="equation", page=2, text="x = y", x0=220, top=200, x1=360, bottom=224),
                    _block(block_id="e2", block_type="equation", page=2, text="a = b", x0=220, top=250, x1=360, bottom=274),
                ],
            )
        ],
    )

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
    )

    assert triage.local_pages == [2]
    assert triage.pages[0].page_type == "formula_or_display_heavy"
    assert triage.pages[0].decision == "local"
    assert "equation_density" in triage.pages[0].reasons


def test_triage_document_forces_backend_in_full_mode():
    service = LocalPdfPageTriageService()
    atoms = [PdfPageAtoms(meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0))]
    normalized_pages = [PdfNormalizedPage(meta=atoms[0].meta)]
    resolved_document = PdfResolvedDocument(pages=[PdfResolvedPage(meta=atoms[0].meta, lines=[], column_count=1)])
    structured_document = PdfStructuredDocument(body_font_size=12.0, pages=[PdfStructuredPage(meta=atoms[0].meta, blocks=[])])

    triage = service.triage_document(
        page_atoms=atoms,
        normalized_pages=normalized_pages,
        resolved_document=resolved_document,
        structured_document=structured_document,
        mode="full",
    )

    assert triage.backend_pages == [1]
    assert triage.pages[0].decision == "backend"
    assert "hybrid_full_mode" in triage.pages[0].reasons
