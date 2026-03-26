from __future__ import annotations

from .auxiliary_block_resolver import LocalPdfAuxiliaryBlockResolver
from .block_builder import LocalPdfBlockBuilder
from .block_role_resolver import LocalPdfBlockRoleResolver
from .contracts import PdfStructuredDocument
from .document_resolver import LocalPdfDocumentResolver
from .front_matter_resolver import LocalPdfFrontMatterResolver
from .heading_refiner import LocalPdfHeadingRefiner
from .native_extractor import LocalPdfNativeExtractor
from .page_normalizer import LocalPdfPageNormalizer
from .section_resolver import LocalPdfSectionResolver
from .table_detector import LocalPdfTableDetector
from .toc_resolver import LocalPdfTocResolver


class LocalStructuredPdfPipeline:
    """End-to-end deterministic local PDF pipeline."""

    def __init__(
        self,
        *,
        extractor: LocalPdfNativeExtractor | None = None,
        normalizer: LocalPdfPageNormalizer | None = None,
        document_resolver: LocalPdfDocumentResolver | None = None,
        block_builder: LocalPdfBlockBuilder | None = None,
        table_detector: LocalPdfTableDetector | None = None,
        block_role_resolver: LocalPdfBlockRoleResolver | None = None,
        auxiliary_block_resolver: LocalPdfAuxiliaryBlockResolver | None = None,
        front_matter_resolver: LocalPdfFrontMatterResolver | None = None,
        heading_refiner: LocalPdfHeadingRefiner | None = None,
        toc_resolver: LocalPdfTocResolver | None = None,
        section_resolver: LocalPdfSectionResolver | None = None,
        heuristic_profile: str = "balanced",
        enable_heuristic_postprocessors: bool | None = None,
    ) -> None:
        self._extractor = extractor or LocalPdfNativeExtractor()
        self._normalizer = normalizer or LocalPdfPageNormalizer()
        self._document_resolver = document_resolver or LocalPdfDocumentResolver()
        self._block_builder = block_builder or LocalPdfBlockBuilder()
        self._table_detector = table_detector or LocalPdfTableDetector()
        self._block_role_resolver = block_role_resolver or LocalPdfBlockRoleResolver()
        profile = str(heuristic_profile or "balanced").strip().lower()
        if profile not in {"balanced", "structural"}:
            raise ValueError(f"Unsupported heuristic_profile: {heuristic_profile}")
        self._heuristic_profile = profile
        self._enable_heuristic_postprocessors = (
            bool(enable_heuristic_postprocessors)
            if enable_heuristic_postprocessors is not None
            else profile == "balanced"
        )
        self._auxiliary_block_resolver = (
            auxiliary_block_resolver or LocalPdfAuxiliaryBlockResolver()
            if self._enable_heuristic_postprocessors
            else None
        )
        self._front_matter_resolver = (
            front_matter_resolver or LocalPdfFrontMatterResolver()
            if self._enable_heuristic_postprocessors
            else None
        )
        self._heading_refiner = (
            heading_refiner or LocalPdfHeadingRefiner()
            if self._enable_heuristic_postprocessors
            else None
        )
        self._toc_resolver = (
            toc_resolver or LocalPdfTocResolver()
            if self._enable_heuristic_postprocessors
            else None
        )
        self._section_resolver = section_resolver or LocalPdfSectionResolver()

    def parse_document(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
    ) -> PdfStructuredDocument:
        page_atoms = self._extractor.extract_document_atoms(pdf_path=pdf_path, page_limit=page_limit)
        if not page_atoms:
            return PdfStructuredDocument()

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
        structured_document = self._block_role_resolver.resolve_document(document=structured_document)
        if self._auxiliary_block_resolver is not None:
            structured_document = self._auxiliary_block_resolver.resolve_document(document=structured_document)
        if self._front_matter_resolver is not None:
            structured_document = self._front_matter_resolver.resolve_document(document=structured_document)
        if self._heading_refiner is not None:
            structured_document = self._heading_refiner.resolve_document(document=structured_document)
        if self._toc_resolver is not None:
            structured_document = self._toc_resolver.resolve_document(document=structured_document)
        return self._section_resolver.resolve_document(document=structured_document)

    def ensure_runtime_ready(self) -> None:
        ensure_runtime_dependencies = getattr(self._extractor, "ensure_runtime_dependencies", None)
        if callable(ensure_runtime_dependencies):
            ensure_runtime_dependencies()
