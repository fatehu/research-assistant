from app.config import settings
from app.services.document_service import DocumentProcessor


def test_layout_parser_auto_prefers_markitdown(monkeypatch):
    processor = DocumentProcessor()
    calls = []

    def fake_markitdown(_file_path: str) -> str:
        calls.append("markitdown")
        return "A" * 300

    def fake_docling(_file_path: str) -> str:
        calls.append("docling")
        return "B" * 300

    monkeypatch.setattr(settings, "pdf_layout_parser", "auto")
    monkeypatch.setattr(settings, "pdf_layout_min_chars", 100)
    monkeypatch.setattr(processor, "_extract_pdf_with_markitdown", fake_markitdown)
    monkeypatch.setattr(processor, "_extract_pdf_with_docling", fake_docling)

    text = processor._extract_pdf_with_layout_parser("dummy.pdf")

    assert text == "A" * 300
    assert calls == ["markitdown"]
    assert processor.last_pdf_extractor == "markitdown"


def test_layout_parser_falls_back_to_docling_when_markitdown_too_short(monkeypatch):
    processor = DocumentProcessor()
    calls = []

    def fake_markitdown(_file_path: str) -> str:
        calls.append("markitdown")
        return "short"

    def fake_docling(_file_path: str) -> str:
        calls.append("docling")
        return "D" * 300

    monkeypatch.setattr(settings, "pdf_layout_parser", "auto")
    monkeypatch.setattr(settings, "pdf_layout_min_chars", 100)
    monkeypatch.setattr(processor, "_extract_pdf_with_markitdown", fake_markitdown)
    monkeypatch.setattr(processor, "_extract_pdf_with_docling", fake_docling)

    text = processor._extract_pdf_with_layout_parser("dummy.pdf")

    assert text == "D" * 300
    assert calls == ["markitdown", "docling"]
    assert processor.last_pdf_extractor == "docling"


def test_layout_parser_can_be_disabled(monkeypatch):
    processor = DocumentProcessor()

    monkeypatch.setattr(settings, "pdf_layout_parser", "none")
    monkeypatch.setattr(settings, "pdf_layout_min_chars", 100)

    text = processor._extract_pdf_with_layout_parser("dummy.pdf")

    assert text is None
    assert processor.last_pdf_extractor is None


def test_layout_parser_falls_back_when_markitdown_is_fragmented(monkeypatch):
    processor = DocumentProcessor()
    calls = []

    fragmented = "\n".join(["A"] * 300)

    def fake_markitdown(_file_path: str) -> str:
        calls.append("markitdown")
        return fragmented

    def fake_docling(_file_path: str) -> str:
        calls.append("docling")
        return "G" * 300

    monkeypatch.setattr(settings, "pdf_layout_parser", "auto")
    monkeypatch.setattr(settings, "pdf_layout_min_chars", 100)
    monkeypatch.setattr(processor, "_extract_pdf_with_markitdown", fake_markitdown)
    monkeypatch.setattr(processor, "_extract_pdf_with_docling", fake_docling)

    text = processor._extract_pdf_with_layout_parser("dummy.pdf")

    assert text == "G" * 300
    assert calls == ["markitdown", "docling"]
    assert processor.last_pdf_extractor == "docling"
