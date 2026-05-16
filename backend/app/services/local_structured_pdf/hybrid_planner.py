from __future__ import annotations

from .block_builder import LocalPdfBlockBuilder
from .contracts import PdfHybridTriageDocument
from .document_resolver import LocalPdfDocumentResolver
from .native_extractor import LocalPdfNativeExtractor
from .page_normalizer import LocalPdfPageNormalizer
from .page_triage_service import LocalPdfPageTriageService
from .table_detector import LocalPdfTableDetector


class LocalStructuredPdfHybridPlanner:
    """Prepare page-level routing decisions for selective hybrid parsing."""

    def __init__(
        self,
        *,
        extractor: LocalPdfNativeExtractor | None = None,
        normalizer: LocalPdfPageNormalizer | None = None,
        document_resolver: LocalPdfDocumentResolver | None = None,
        block_builder: LocalPdfBlockBuilder | None = None,
        table_detector: LocalPdfTableDetector | None = None,
        triage_service: LocalPdfPageTriageService | None = None,
    ) -> None:
        self._extractor = extractor or LocalPdfNativeExtractor()
        self._normalizer = normalizer or LocalPdfPageNormalizer()
        self._document_resolver = document_resolver or LocalPdfDocumentResolver()
        self._block_builder = block_builder or LocalPdfBlockBuilder()
        self._table_detector = table_detector or LocalPdfTableDetector()
        self._triage_service = triage_service or LocalPdfPageTriageService()

    def plan_document(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        mode: str = "auto",
        include_chars: bool = False,
    ) -> PdfHybridTriageDocument:
        page_atoms = self._extractor.extract_document_atoms(
            pdf_path=pdf_path,
            page_limit=page_limit,
            include_chars=include_chars,
        )
        normalized_pages = [
            self._normalizer.normalize_page(page_atoms=page)
            for page in page_atoms
        ]
        resolved_document = self._document_resolver.resolve_document(pages=normalized_pages)
        structured_document = self._block_builder.build_document(document=resolved_document)
        structured_document = self._table_detector.detect_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
        )
        return self.plan_from_artifacts(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
            mode=mode,
        )

    def plan_from_artifacts(
        self,
        *,
        page_atoms,
        normalized_pages,
        resolved_document,
        structured_document,
        mode: str = "auto",
    ) -> PdfHybridTriageDocument:
        return self._triage_service.triage_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
            mode=mode,
        )

    def ensure_runtime_ready(self) -> None:
        ensure_runtime_dependencies = getattr(self._extractor, "ensure_runtime_dependencies", None)
        if callable(ensure_runtime_dependencies):
            ensure_runtime_dependencies()
