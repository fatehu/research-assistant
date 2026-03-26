from __future__ import annotations

import fitz

from app.services.local_structured_pdf import LocalPdfNativeExtractor


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

    def _patched(self, *, path, page_number, plumber_pdf, fitz_doc, document_flags):  # type: ignore[no-untyped-def]
        if int(page_number) == 2:
            raise RuntimeError("page boom")
        return original(
            self,
            path=path,
            page_number=page_number,
            plumber_pdf=plumber_pdf,
            fitz_doc=fitz_doc,
            document_flags=document_flags,
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
