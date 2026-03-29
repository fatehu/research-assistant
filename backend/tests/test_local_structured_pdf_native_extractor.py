from __future__ import annotations

import base64

import fitz

from app.services.local_structured_pdf import LocalPdfNativeExtractor
from app.services.local_structured_pdf.contracts import PdfBBox, PdfImageAtom


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X7h8AAAAASUVORK5CYII="
)


def test_extract_page_atoms_reads_words_chars_and_page_meta(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Hello local parser", fontsize=14)
    page.insert_text((72, 128), "Second line", fontsize=11)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert page_atoms.meta.page == 1
    assert page_atoms.meta.page_width > 500
    assert page_atoms.meta.page_height > 800
    assert "Hello local parser" in page_atoms.extract_text_raw
    assert "Hello local parser" in page_atoms.extract_text_fitz
    assert len(page_atoms.words) >= 4
    assert len(page_atoms.chars) >= 10
    assert len(page_atoms.text_blocks) >= 1
    assert any(word.text == "Hello" for word in page_atoms.words)
    assert any(word.start_char_id and word.end_char_id for word in page_atoms.words)
    assert "pdfplumber" in page_atoms.source_engines
    assert "pymupdf" in page_atoms.source_engines
    assert "pypdf" in page_atoms.source_engines
    assert page_atoms.has_struct_tree is False


def test_extract_document_atoms_respects_page_limit(tmp_path):
    pdf_path = tmp_path / "multi.pdf"
    doc = fitz.open()
    first = doc.new_page(width=400, height=400)
    first.insert_text((40, 50), "Page one", fontsize=12)
    second = doc.new_page(width=400, height=400)
    second.insert_text((40, 50), "Page two", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    pages = extractor.extract_document_atoms(pdf_path=str(pdf_path), page_limit=1)

    assert len(pages) == 1
    assert pages[0].meta.page == 1
    assert "Page one" in pages[0].extract_text_raw


def test_extract_page_atoms_skips_chars_when_disabled(tmp_path):
    pdf_path = tmp_path / "no-chars.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Hello local parser", fontsize=14)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1, include_chars=False)

    assert len(page_atoms.words) >= 2
    assert page_atoms.chars == []
    assert not any(word.start_char_id or word.end_char_id for word in page_atoms.words)


def test_extract_page_atoms_reads_pymupdf_tables(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    shape = page.new_shape()
    for x in (50, 150, 250, 350):
        shape.draw_line((x, 50), (x, 150))
    for y in (50, 100, 150):
        shape.draw_line((50, y), (350, y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()
    page.insert_text((70, 80), "A1", fontsize=11)
    page.insert_text((170, 80), "B1", fontsize=11)
    page.insert_text((270, 80), "C1", fontsize=11)
    page.insert_text((70, 130), "A2", fontsize=11)
    page.insert_text((170, 130), "B2", fontsize=11)
    page.insert_text((270, 130), "C2", fontsize=11)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert len(page_atoms.tables) >= 1
    table = page_atoms.tables[0]
    assert table.row_count >= 2
    assert table.col_count >= 2
    flattened = " ".join(cell for row in table.cells for cell in row)
    assert "A1" in flattened
    assert "C2" in flattened


def test_extract_page_atoms_tolerates_single_stage_failure(tmp_path, monkeypatch):
    pdf_path = tmp_path / "stage-failure.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Hello stage failure", fontsize=14)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()

    def _raise_annots(*, page_obj):  # type: ignore[no-untyped-def]
        raise UnicodeDecodeError("utf-16-le", b"\t", 0, 1, "broken annot")

    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_annots", classmethod(lambda cls, *, page_obj: _raise_annots(page_obj=page_obj)))

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert "Hello stage failure" in page_atoms.extract_text_raw
    assert len(page_atoms.words) >= 3
    assert len(page_atoms.text_blocks) >= 1
    assert page_atoms.annots == []


def test_extract_page_atoms_skips_plumber_text_when_fitz_text_present(tmp_path, monkeypatch):
    pdf_path = tmp_path / "fitz-priority.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Fitz text wins", fontsize=14)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()

    def _raise_if_called(cls, *, page_obj):  # type: ignore[no-untyped-def]
        raise RuntimeError("plumber text should be skipped when fitz text exists")

    monkeypatch.setattr(
        LocalPdfNativeExtractor,
        "_extract_plumber_text",
        classmethod(_raise_if_called),
    )

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert "Fitz text wins" in page_atoms.extract_text_fitz
    assert "Fitz text wins" in page_atoms.extract_text_raw
    assert page_atoms.meta.page == 1


def test_extract_page_atoms_uses_plumber_text_when_fitz_text_empty(tmp_path, monkeypatch):
    pdf_path = tmp_path / "plumber-fallback.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "Text for fallback path", fontsize=14)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    calls = {"count": 0}

    monkeypatch.setattr(
        LocalPdfNativeExtractor,
        "_extract_fitz_text",
        staticmethod(lambda *, fitz_page: ""),
    )

    def _plumber_text(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return "plumber fallback text"

    monkeypatch.setattr(
        LocalPdfNativeExtractor,
        "_extract_plumber_text",
        classmethod(_plumber_text),
    )

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert calls["count"] == 1
    assert page_atoms.extract_text_fitz == ""
    assert page_atoms.extract_text_raw == "plumber fallback text"


def test_extract_page_atoms_skips_words_and_tables_for_obvious_visual_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "visual-fast-path.pdf"
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((30, 30), "A", fontsize=10)
    page.insert_image(fitz.Rect(20, 60, 580, 780), stream=_ONE_BY_ONE_PNG)
    shape = page.new_shape()
    for y in (100, 130, 160, 190, 220, 250):
        shape.draw_line((40, y), (560, y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    calls = {"words": 0, "tables": 0, "images": 0, "lines": 0}

    def _count_words(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["words"] += 1
        return []

    def _count_tables(cls, *, fitz_page):  # type: ignore[no-untyped-def]
        calls["tables"] += 1
        return []

    def _count_images(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["images"] += 1
        return []

    def _count_lines(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["lines"] += 1
        return []

    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_words", classmethod(_count_words))
    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_tables", classmethod(_count_tables))
    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_images", classmethod(_count_images))
    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_lines", classmethod(_count_lines))

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert calls["words"] == 0
    assert calls["tables"] == 0
    assert calls["images"] == 0
    assert calls["lines"] == 0
    assert page_atoms.words == []
    assert page_atoms.tables == []


def test_extract_page_atoms_keeps_word_extraction_for_normal_text_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "normal-text-fast-path-guard.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), "This is a normal local text page with enough words.", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    calls = {"words": 0}
    original_words = LocalPdfNativeExtractor._extract_words.__func__

    def _count_words(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["words"] += 1
        return original_words(cls, page_obj=page_obj)

    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_words", classmethod(_count_words))

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert calls["words"] == 1
    assert len(page_atoms.words) >= 6


def test_extract_page_atoms_extracts_images_on_non_fast_path(tmp_path, monkeypatch):
    pdf_path = tmp_path / "normal-page-image-extraction.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (72, 96),
        "This page has plenty of readable text so fast path should stay disabled.",
        fontsize=12,
    )
    page.insert_image(fitz.Rect(72, 140, 240, 300), stream=_ONE_BY_ONE_PNG)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    calls = {"images": 0, "words": 0}
    original_words = LocalPdfNativeExtractor._extract_words.__func__

    def _count_words(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["words"] += 1
        return original_words(cls, page_obj=page_obj)

    def _stub_images(cls, *, page_obj):  # type: ignore[no-untyped-def]
        calls["images"] += 1
        return [
            PdfImageAtom(
                image_id="img0001",
                bbox=PdfBBox(x0=72.0, top=140.0, x1=240.0, bottom=300.0),
                name="stubbed",
            )
        ]

    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_words", classmethod(_count_words))
    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_images", classmethod(_stub_images))

    page_atoms = extractor.extract_page_atoms(pdf_path=str(pdf_path), page=1)

    assert calls["words"] == 1
    assert calls["images"] == 1
    assert len(page_atoms.images) == 1
    assert page_atoms.images[0].name == "stubbed"


def test_extract_document_atoms_preserves_pages_when_one_page_fails(tmp_path, monkeypatch):
    pdf_path = tmp_path / "page-failure.pdf"
    doc = fitz.open()
    first = doc.new_page(width=400, height=400)
    first.insert_text((40, 50), "Page one", fontsize=12)
    second = doc.new_page(width=400, height=400)
    second.insert_text((40, 50), "Page two", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    extractor = LocalPdfNativeExtractor()
    original = LocalPdfNativeExtractor._extract_page_atoms_from_handles

    def _patched(self, *, path, page_number, plumber_pdf, fitz_doc, document_flags, include_chars=True):  # type: ignore[no-untyped-def]
        if int(page_number) == 2:
            raise RuntimeError("page boom")
        return original(
            self,
            path=path,
            page_number=page_number,
            plumber_pdf=plumber_pdf,
            fitz_doc=fitz_doc,
            document_flags=document_flags,
            include_chars=include_chars,
        )

    monkeypatch.setattr(LocalPdfNativeExtractor, "_extract_page_atoms_from_handles", _patched)

    pages = extractor.extract_document_atoms(pdf_path=str(pdf_path))

    assert len(pages) == 2
    assert pages[0].meta.page == 1
    assert "Page one" in pages[0].extract_text_raw
    assert pages[1].meta.page == 2
    assert pages[1].extract_text_raw == ""


def test_ensure_runtime_dependencies_raises_when_no_text_backend(monkeypatch):
    monkeypatch.setattr(
        LocalPdfNativeExtractor,
        "_module_available",
        staticmethod(lambda module_name: module_name == "pypdf"),
    )

    try:
        LocalPdfNativeExtractor.ensure_runtime_dependencies()
    except RuntimeError as exc:
        assert "pdfplumber" in str(exc)
        assert "PyMuPDF" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("Expected RuntimeError when no text backend is installed")
