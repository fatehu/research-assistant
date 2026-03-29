from __future__ import annotations

import asyncio

from app.services.local_structured_pdf.compat_hybrid_pipeline import LocalStructuredPdfCompatHybridPipeline
from app.services.local_structured_pdf.contracts import (
    PdfBBox,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfPageMeta,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


def _document() -> PdfStructuredDocument:
    block = PdfSemanticBlock(
        block_id="b1",
        block_type="paragraph",
        page_start=1,
        page_end=1,
        text="local",
        bbox=PdfBBox(x0=0.0, top=0.0, x1=100.0, bottom=40.0),
        line_ids=[],
    )
    page = PdfStructuredPage(meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0), blocks=[block])
    return PdfStructuredDocument(pages=[page], blocks=[block])


class _IdentityResolver:
    def resolve_document(self, *, document):  # type: ignore[no-untyped-def]
        return document


def test_compat_hybrid_pipeline_uses_selected_stages() -> None:
    class _Pipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return _document()

        def ensure_runtime_ready(self) -> None:
            return None

    class _Planner:
        def plan_from_artifacts(self, *, page_atoms, normalized_pages, resolved_document, structured_document, mode: str = "auto"):  # type: ignore[no-untyped-def]
            del page_atoms, normalized_pages, resolved_document, structured_document, mode
            return PdfHybridTriageDocument(
                mode="auto",
                pages=[
                    PdfHybridTriageResult(
                        page=1,
                        page_type="visual_or_scanned",
                        decision="backend",
                        confidence=0.9,
                        reasons=["page_type:visual_or_scanned"],
                        signals=PdfHybridTriageSignals(),
                    )
                ],
            )

    class _Extractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None, include_chars=True):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit, include_chars
            return [object()]

    class _Parser:
        def is_configured(self) -> bool:
            return True

    calls: list[tuple[str, set[int] | None]] = []

    class _Ocr:
        async def enrich_document(self, *, pdf_path: str, document, ocr_lang=None, page_numbers=None, page_atoms=None):  # type: ignore[no-untyped-def]
            del pdf_path, ocr_lang
            calls.append(("ocr", set(page_numbers or [])))
            return document

    class _Formula:
        async def enrich_document(self, *, pdf_path: str, document, page_numbers=None):  # type: ignore[no-untyped-def]
            del pdf_path
            calls.append(("formula", set(page_numbers or [])))
            return document

    class _Picture:
        async def enrich_document(self, *, pdf_path: str, document, picture_description_prompt=None, page_numbers=None, page_atoms=None):  # type: ignore[no-untyped-def]
            del pdf_path, document, picture_description_prompt
            calls.append(("picture", set(page_numbers or [])))
            return []

    pipeline = LocalStructuredPdfCompatHybridPipeline(
        pipeline=_Pipeline(),
        planner=_Planner(),
        # inject stubs so front half is effectively no-op
        page_parser=_Parser(),
        ocr_enrichment_service=_Ocr(),
        formula_enrichment_service=_Formula(),
        picture_enrichment_service=_Picture(),
    )
    pipeline._pipeline._extractor = _Extractor()  # type: ignore[attr-defined]
    pipeline._pipeline._normalizer = type("_N", (), {"normalize_page": staticmethod(lambda page_atoms: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._document_resolver = type("_D", (), {"resolve_document": staticmethod(lambda pages: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_builder = type("_B", (), {"build_document": staticmethod(lambda document: _document())})()  # type: ignore[attr-defined]
    pipeline._pipeline._table_detector = type("_T", (), {"detect_document": staticmethod(lambda **kwargs: kwargs["structured_document"])})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_role_resolver = _IdentityResolver()  # type: ignore[attr-defined]
    pipeline._pipeline._auxiliary_block_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._front_matter_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._heading_refiner = None  # type: ignore[attr-defined]
    pipeline._pipeline._toc_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._section_resolver = _IdentityResolver()  # type: ignore[attr-defined]

    result = asyncio.run(pipeline.parse_document_with_trace(pdf_path="/tmp/demo.pdf", mode="auto"))

    assert calls == [("ocr", {1})]
    assert result.parsed_pages[0].notes == ["stage:ocr"]


def test_compat_hybrid_pipeline_passes_local_page_atoms_to_ocr_and_picture() -> None:
    class _Pipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return _document()

        def ensure_runtime_ready(self) -> None:
            return None

    class _Planner:
        def plan_from_artifacts(self, *, page_atoms, normalized_pages, resolved_document, structured_document, mode: str = "auto"):  # type: ignore[no-untyped-def]
            del page_atoms, normalized_pages, resolved_document, structured_document, mode
            return PdfHybridTriageDocument(
                mode="full",
                pages=[
                    PdfHybridTriageResult(
                        page=1,
                        page_type="visual_or_scanned",
                        decision="backend",
                        confidence=0.9,
                        reasons=["page_type:visual_or_scanned"],
                        signals=PdfHybridTriageSignals(),
                    )
                ],
            )

    shared_atoms = [object()]

    class _Extractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None, include_chars=True):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit, include_chars
            return shared_atoms

    class _Parser:
        def is_configured(self) -> bool:
            return True

    seen: dict[str, object | None] = {"ocr": None, "picture": None}

    class _Ocr:
        async def enrich_document(self, *, pdf_path: str, document, ocr_lang=None, page_numbers=None, page_atoms=None):  # type: ignore[no-untyped-def]
            del pdf_path, ocr_lang, page_numbers
            seen["ocr"] = page_atoms
            return document

    class _Formula:
        async def enrich_document(self, *, pdf_path: str, document, page_numbers=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_numbers
            return document

    class _Picture:
        async def enrich_document(self, *, pdf_path: str, document, picture_description_prompt=None, page_numbers=None, page_atoms=None):  # type: ignore[no-untyped-def]
            del pdf_path, document, picture_description_prompt, page_numbers
            seen["picture"] = page_atoms
            return []

    pipeline = LocalStructuredPdfCompatHybridPipeline(
        pipeline=_Pipeline(),
        planner=_Planner(),
        page_parser=_Parser(),
        ocr_enrichment_service=_Ocr(),
        formula_enrichment_service=_Formula(),
        picture_enrichment_service=_Picture(),
    )
    pipeline._pipeline._extractor = _Extractor()  # type: ignore[attr-defined]
    pipeline._pipeline._normalizer = type("_N", (), {"normalize_page": staticmethod(lambda page_atoms: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._document_resolver = type("_D", (), {"resolve_document": staticmethod(lambda pages: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_builder = type("_B", (), {"build_document": staticmethod(lambda document: _document())})()  # type: ignore[attr-defined]
    pipeline._pipeline._table_detector = type("_T", (), {"detect_document": staticmethod(lambda **kwargs: kwargs["structured_document"])})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_role_resolver = _IdentityResolver()  # type: ignore[attr-defined]
    pipeline._pipeline._auxiliary_block_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._front_matter_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._heading_refiner = None  # type: ignore[attr-defined]
    pipeline._pipeline._toc_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._section_resolver = _IdentityResolver()  # type: ignore[attr-defined]

    asyncio.run(pipeline.parse_document_with_trace(pdf_path="/tmp/demo.pdf", mode="full"))

    assert seen["ocr"] is shared_atoms
    assert seen["picture"] is shared_atoms


def test_compat_hybrid_pipeline_returns_local_document_when_parser_not_configured() -> None:
    class _Pipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return _document()

        def ensure_runtime_ready(self) -> None:
            return None

    class _Planner:
        def plan_from_artifacts(self, *, page_atoms, normalized_pages, resolved_document, structured_document, mode: str = "auto"):  # type: ignore[no-untyped-def]
            del page_atoms, normalized_pages, resolved_document, structured_document, mode
            return PdfHybridTriageDocument(
                mode="auto",
                pages=[
                    PdfHybridTriageResult(
                        page=1,
                        page_type="formula_or_display_heavy",
                        decision="backend",
                        confidence=0.9,
                        reasons=["page_type:formula_or_display_heavy"],
                        signals=PdfHybridTriageSignals(),
                    )
                ],
            )

    class _Parser:
        def is_configured(self) -> bool:
            return False

    pipeline = LocalStructuredPdfCompatHybridPipeline(
        pipeline=_Pipeline(),
        planner=_Planner(),
        page_parser=_Parser(),
    )
    pipeline._pipeline._extractor = type("_E", (), {"extract_document_atoms": staticmethod(lambda **kwargs: [object()])})()  # type: ignore[attr-defined]
    pipeline._pipeline._normalizer = type("_N", (), {"normalize_page": staticmethod(lambda page_atoms: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._document_resolver = type("_D", (), {"resolve_document": staticmethod(lambda pages: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_builder = type("_B", (), {"build_document": staticmethod(lambda document: _document())})()  # type: ignore[attr-defined]
    pipeline._pipeline._table_detector = type("_T", (), {"detect_document": staticmethod(lambda **kwargs: kwargs["structured_document"])})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_role_resolver = _IdentityResolver()  # type: ignore[attr-defined]
    pipeline._pipeline._auxiliary_block_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._front_matter_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._heading_refiner = None  # type: ignore[attr-defined]
    pipeline._pipeline._toc_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._section_resolver = _IdentityResolver()  # type: ignore[attr-defined]

    result = asyncio.run(pipeline.parse_document_with_trace(pdf_path="/tmp/demo.pdf", mode="auto"))

    assert result.document.blocks[0].text == "local"
    assert result.parsed_pages[0].error == "backend_parser_not_configured"


def test_compat_hybrid_pipeline_disables_chars_by_default() -> None:
    seen: dict[str, bool | None] = {"include_chars": None}

    class _Pipeline:
        def ensure_runtime_ready(self) -> None:
            return None

    class _Planner:
        def plan_from_artifacts(self, *, page_atoms, normalized_pages, resolved_document, structured_document, mode: str = "auto"):  # type: ignore[no-untyped-def]
            del page_atoms, normalized_pages, resolved_document, structured_document, mode
            return PdfHybridTriageDocument(
                mode="auto",
                pages=[
                    PdfHybridTriageResult(
                        page=1,
                        page_type="formula_or_display_heavy",
                        decision="backend",
                        confidence=0.9,
                        reasons=["page_type:formula_or_display_heavy"],
                        signals=PdfHybridTriageSignals(),
                    )
                ],
            )

    class _Extractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None, include_chars=True):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            seen["include_chars"] = include_chars
            return [object()]

    class _Parser:
        def is_configured(self) -> bool:
            return False

    pipeline = LocalStructuredPdfCompatHybridPipeline(
        pipeline=_Pipeline(),
        planner=_Planner(),
        page_parser=_Parser(),
    )
    pipeline._pipeline._extractor = _Extractor()  # type: ignore[attr-defined]
    pipeline._pipeline._normalizer = type("_N", (), {"normalize_page": staticmethod(lambda page_atoms: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._document_resolver = type("_D", (), {"resolve_document": staticmethod(lambda pages: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_builder = type("_B", (), {"build_document": staticmethod(lambda document: _document())})()  # type: ignore[attr-defined]
    pipeline._pipeline._table_detector = type("_T", (), {"detect_document": staticmethod(lambda **kwargs: kwargs["structured_document"])})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_role_resolver = _IdentityResolver()  # type: ignore[attr-defined]
    pipeline._pipeline._auxiliary_block_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._front_matter_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._heading_refiner = None  # type: ignore[attr-defined]
    pipeline._pipeline._toc_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._section_resolver = _IdentityResolver()  # type: ignore[attr-defined]

    asyncio.run(pipeline.parse_document_with_trace(pdf_path="/tmp/demo.pdf", mode="auto"))

    assert seen["include_chars"] is False


def test_compat_hybrid_pipeline_allows_chars_opt_in() -> None:
    seen: dict[str, bool | None] = {"include_chars": None}

    class _Pipeline:
        def ensure_runtime_ready(self) -> None:
            return None

    class _Planner:
        def plan_from_artifacts(self, *, page_atoms, normalized_pages, resolved_document, structured_document, mode: str = "auto"):  # type: ignore[no-untyped-def]
            del page_atoms, normalized_pages, resolved_document, structured_document, mode
            return PdfHybridTriageDocument(
                mode="auto",
                pages=[
                    PdfHybridTriageResult(
                        page=1,
                        page_type="formula_or_display_heavy",
                        decision="backend",
                        confidence=0.9,
                        reasons=["page_type:formula_or_display_heavy"],
                        signals=PdfHybridTriageSignals(),
                    )
                ],
            )

    class _Extractor:
        def extract_document_atoms(self, *, pdf_path: str, page_limit=None, include_chars=True):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            seen["include_chars"] = include_chars
            return [object()]

    class _Parser:
        def is_configured(self) -> bool:
            return False

    pipeline = LocalStructuredPdfCompatHybridPipeline(
        pipeline=_Pipeline(),
        planner=_Planner(),
        page_parser=_Parser(),
    )
    pipeline._pipeline._extractor = _Extractor()  # type: ignore[attr-defined]
    pipeline._pipeline._normalizer = type("_N", (), {"normalize_page": staticmethod(lambda page_atoms: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._document_resolver = type("_D", (), {"resolve_document": staticmethod(lambda pages: object())})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_builder = type("_B", (), {"build_document": staticmethod(lambda document: _document())})()  # type: ignore[attr-defined]
    pipeline._pipeline._table_detector = type("_T", (), {"detect_document": staticmethod(lambda **kwargs: kwargs["structured_document"])})()  # type: ignore[attr-defined]
    pipeline._pipeline._block_role_resolver = _IdentityResolver()  # type: ignore[attr-defined]
    pipeline._pipeline._auxiliary_block_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._front_matter_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._heading_refiner = None  # type: ignore[attr-defined]
    pipeline._pipeline._toc_resolver = None  # type: ignore[attr-defined]
    pipeline._pipeline._section_resolver = _IdentityResolver()  # type: ignore[attr-defined]

    asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/demo.pdf",
            mode="auto",
            include_chars=True,
        )
    )

    assert seen["include_chars"] is True
