from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalStructuredPdfPipeline,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfStructuredDocument,
)


class _Extractor:
    def extract_document_atoms(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        include_chars: bool = True,
    ) -> list[PdfPageAtoms]:
        del pdf_path, page_limit, include_chars
        return [PdfPageAtoms(meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0))]


class _Normalizer:
    def normalize_page(self, *, page_atoms: PdfPageAtoms) -> PdfNormalizedPage:
        return PdfNormalizedPage(meta=page_atoms.meta)


class _DocumentResolver:
    def resolve_document(self, *, pages: list[PdfNormalizedPage]) -> PdfResolvedDocument:
        return PdfResolvedDocument()


class _BlockBuilder:
    def build_document(self, *, document: PdfResolvedDocument) -> PdfStructuredDocument:
        return PdfStructuredDocument(body_font_size=12.0)


class _TableDetector:
    def detect_document(
        self,
        *,
        page_atoms: list[PdfPageAtoms] | None = None,
        normalized_pages: list[PdfNormalizedPage],
        resolved_document: PdfResolvedDocument,
        structured_document: PdfStructuredDocument,
    ) -> PdfStructuredDocument:
        return structured_document


class _ReadyExtractor(_Extractor):
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def ensure_runtime_dependencies(self) -> None:
        self._calls.append("ready")


class _RecorderResolver:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        self._calls.append(self._name)
        return document


def test_parse_document_runs_balanced_profile_by_default():
    calls: list[str] = []
    pipeline = LocalStructuredPdfPipeline(
        extractor=_Extractor(),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", calls),
        auxiliary_block_resolver=_RecorderResolver("aux", calls),
        front_matter_resolver=_RecorderResolver("front", calls),
        heading_refiner=_RecorderResolver("heading", calls),
        toc_resolver=_RecorderResolver("toc", calls),
        section_resolver=_RecorderResolver("section", calls),
    )

    result = pipeline.parse_document(pdf_path="/tmp/demo.pdf")

    assert isinstance(result, PdfStructuredDocument)
    assert calls == ["role", "aux", "front", "heading", "toc", "section"]


def test_parse_document_skips_heuristic_postprocessors_in_structural_profile():
    calls: list[str] = []
    pipeline = LocalStructuredPdfPipeline(
        extractor=_Extractor(),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", calls),
        auxiliary_block_resolver=_RecorderResolver("aux", calls),
        front_matter_resolver=_RecorderResolver("front", calls),
        heading_refiner=_RecorderResolver("heading", calls),
        toc_resolver=_RecorderResolver("toc", calls),
        section_resolver=_RecorderResolver("section", calls),
        heuristic_profile="structural",
    )

    result = pipeline.parse_document(pdf_path="/tmp/demo.pdf")

    assert isinstance(result, PdfStructuredDocument)
    assert calls == ["role", "section"]


def test_parse_document_runs_heuristic_postprocessors_when_enabled():
    calls: list[str] = []
    pipeline = LocalStructuredPdfPipeline(
        extractor=_Extractor(),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", calls),
        auxiliary_block_resolver=_RecorderResolver("aux", calls),
        front_matter_resolver=_RecorderResolver("front", calls),
        heading_refiner=_RecorderResolver("heading", calls),
        toc_resolver=_RecorderResolver("toc", calls),
        section_resolver=_RecorderResolver("section", calls),
        enable_heuristic_postprocessors=True,
    )

    result = pipeline.parse_document(pdf_path="/tmp/demo.pdf")

    assert isinstance(result, PdfStructuredDocument)
    assert calls == ["role", "aux", "front", "heading", "toc", "section"]


def test_ensure_runtime_ready_delegates_to_extractor():
    calls: list[str] = []
    pipeline = LocalStructuredPdfPipeline(
        extractor=_ReadyExtractor(calls),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", calls),
        section_resolver=_RecorderResolver("section", calls),
    )

    pipeline.ensure_runtime_ready()

    assert calls == ["ready"]


def test_parse_document_disables_chars_by_default():
    seen: dict[str, bool | None] = {"include_chars": None}

    class _RecorderExtractor(_Extractor):
        def extract_document_atoms(  # type: ignore[override]
            self,
            *,
            pdf_path: str,
            page_limit: int | None = None,
            include_chars: bool = True,
        ) -> list[PdfPageAtoms]:
            del pdf_path, page_limit
            seen["include_chars"] = include_chars
            return [PdfPageAtoms(meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0))]

    pipeline = LocalStructuredPdfPipeline(
        extractor=_RecorderExtractor(),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", []),
        section_resolver=_RecorderResolver("section", []),
    )

    pipeline.parse_document(pdf_path="/tmp/demo.pdf")

    assert seen["include_chars"] is False


def test_parse_document_allows_chars_opt_in():
    seen: dict[str, bool | None] = {"include_chars": None}

    class _RecorderExtractor(_Extractor):
        def extract_document_atoms(  # type: ignore[override]
            self,
            *,
            pdf_path: str,
            page_limit: int | None = None,
            include_chars: bool = True,
        ) -> list[PdfPageAtoms]:
            del pdf_path, page_limit
            seen["include_chars"] = include_chars
            return [PdfPageAtoms(meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0))]

    pipeline = LocalStructuredPdfPipeline(
        extractor=_RecorderExtractor(),
        normalizer=_Normalizer(),
        document_resolver=_DocumentResolver(),
        block_builder=_BlockBuilder(),
        table_detector=_TableDetector(),
        block_role_resolver=_RecorderResolver("role", []),
        section_resolver=_RecorderResolver("section", []),
    )

    pipeline.parse_document(pdf_path="/tmp/demo.pdf", include_chars=True)

    assert seen["include_chars"] is True
