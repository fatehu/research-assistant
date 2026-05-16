from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalStructuredPdfHybridPlanner,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfStructuredDocument,
)


class _Extractor:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def extract_document_atoms(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        include_chars: bool = True,
    ) -> list[PdfPageAtoms]:
        self._calls.append(f"extract:{pdf_path}:{page_limit}:{include_chars}")
        return [PdfPageAtoms(meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0))]

    def ensure_runtime_dependencies(self) -> None:
        self._calls.append("ready")


class _Normalizer:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def normalize_page(self, *, page_atoms: PdfPageAtoms) -> PdfNormalizedPage:
        self._calls.append(f"normalize:{page_atoms.page}")
        return PdfNormalizedPage(meta=page_atoms.meta)


class _DocumentResolver:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def resolve_document(self, *, pages: list[PdfNormalizedPage]) -> PdfResolvedDocument:
        self._calls.append(f"resolve:{len(pages)}")
        return PdfResolvedDocument()


class _BlockBuilder:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def build_document(self, *, document: PdfResolvedDocument) -> PdfStructuredDocument:
        self._calls.append("build")
        return PdfStructuredDocument(body_font_size=12.0)


class _TableDetector:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def detect_document(
        self,
        *,
        page_atoms: list[PdfPageAtoms] | None = None,
        normalized_pages: list[PdfNormalizedPage],
        resolved_document: PdfResolvedDocument,
        structured_document: PdfStructuredDocument,
    ) -> PdfStructuredDocument:
        self._calls.append(f"table:{len(page_atoms or [])}:{len(normalized_pages)}")
        return structured_document


class _TriageService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def triage_document(
        self,
        *,
        page_atoms,
        normalized_pages,
        resolved_document,
        structured_document,
        mode: str = "auto",
    ) -> PdfHybridTriageDocument:
        self._calls.append(f"triage:{mode}:{len(page_atoms)}")
        return PdfHybridTriageDocument(
            mode=mode,
            pages=[
                PdfHybridTriageResult(
                    page=1,
                    page_type="plain_text",
                    decision="local",
                    confidence=0.9,
                    reasons=["page_type:plain_text"],
                    signals=PdfHybridTriageSignals(text_line_count=4),
                )
            ],
        )


def test_hybrid_planner_runs_pre_triage_pipeline_in_order():
    calls: list[str] = []
    planner = LocalStructuredPdfHybridPlanner(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
    )

    result = planner.plan_document(pdf_path="/tmp/demo.pdf", page_limit=3, mode="full")

    assert isinstance(result, PdfHybridTriageDocument)
    assert calls == [
        "extract:/tmp/demo.pdf:3:False",
        "normalize:1",
        "resolve:1",
        "build",
        "table:1:1",
        "triage:full:1",
    ]


def test_hybrid_planner_delegates_runtime_ready_to_extractor():
    calls: list[str] = []
    planner = LocalStructuredPdfHybridPlanner(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
    )

    planner.ensure_runtime_ready()

    assert calls == ["ready"]


def test_hybrid_planner_allows_chars_opt_in():
    calls: list[str] = []
    planner = LocalStructuredPdfHybridPlanner(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
    )

    planner.plan_document(pdf_path="/tmp/demo.pdf", include_chars=True)

    assert calls[0] == "extract:/tmp/demo.pdf:None:True"
