from __future__ import annotations

from .auxiliary_block_resolver import LocalPdfAuxiliaryBlockResolver
from .block_builder import LocalPdfBlockBuilder
from .block_role_resolver import LocalPdfBlockRoleResolver
from .contracts import (
    PdfHybridExecutionResult,
    PdfHybridParsedPage,
    PdfStructuredDocument,
)
from .document_resolver import LocalPdfDocumentResolver
from .front_matter_resolver import LocalPdfFrontMatterResolver
from .heading_refiner import LocalPdfHeadingRefiner
from .hybrid_fusion_service import LocalStructuredPdfHybridFusionService
from .native_extractor import LocalPdfNativeExtractor
from .ollama_page_parser import LocalOllamaQwenVlPageParser
from .page_normalizer import LocalPdfPageNormalizer
from .page_triage_service import LocalPdfPageTriageService
from .section_resolver import LocalPdfSectionResolver
from .table_detector import LocalPdfTableDetector
from .toc_resolver import LocalPdfTocResolver


class LocalStructuredPdfHybridPipeline:
    """Selective hybrid pipeline: local parse first, then replace only routed pages."""

    def __init__(
        self,
        *,
        extractor: LocalPdfNativeExtractor | None = None,
        normalizer: LocalPdfPageNormalizer | None = None,
        document_resolver: LocalPdfDocumentResolver | None = None,
        block_builder: LocalPdfBlockBuilder | None = None,
        table_detector: LocalPdfTableDetector | None = None,
        triage_service: LocalPdfPageTriageService | None = None,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
        fusion_service: LocalStructuredPdfHybridFusionService | None = None,
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
        self._triage_service = triage_service or LocalPdfPageTriageService()
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()
        self._fusion_service = fusion_service or LocalStructuredPdfHybridFusionService()
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

    async def parse_document(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        mode: str = "auto",
        force_ocr: bool = False,
        task_hints: dict | None = None,
    ) -> PdfStructuredDocument:
        result = await self.parse_document_with_trace(
            pdf_path=pdf_path,
            page_limit=page_limit,
            mode=mode,
            force_ocr=force_ocr,
            task_hints=task_hints,
        )
        return result.document

    async def parse_document_with_trace(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        mode: str = "auto",
        force_ocr: bool = False,
        task_hints: dict | None = None,
    ) -> PdfHybridExecutionResult:
        page_atoms = self._extractor.extract_document_atoms(pdf_path=pdf_path, page_limit=page_limit)
        if not page_atoms:
            empty_document = PdfStructuredDocument()
            return PdfHybridExecutionResult(
                mode=self._normalize_mode(mode),
                document=empty_document,
            )

        normalized_pages = [
            self._normalizer.normalize_page(page_atoms=page)
            for page in page_atoms
        ]
        resolved_document = self._document_resolver.resolve_document(pages=normalized_pages)
        local_document = self._block_builder.build_document(document=resolved_document)
        local_document = self._table_detector.detect_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=local_document,
        )
        triage_document = self._triage_service.triage_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=local_document,
            mode=mode,
        )
        parsed_pages = await self._parse_backend_pages(
            pdf_path=pdf_path,
            resolved_document=resolved_document,
            triage_document=triage_document,
            force_ocr=force_ocr,
            task_hints=task_hints,
        )
        fused_document = self._fusion_service.fuse_document(
            resolved_document=resolved_document,
            local_document=local_document,
            triage_document=triage_document,
            parsed_pages=parsed_pages,
        )
        final_document = self._apply_postprocessors(document=fused_document)
        return PdfHybridExecutionResult(
            mode=str(triage_document.mode or self._normalize_mode(mode)),
            document=final_document,
            triage=triage_document,
            parsed_pages=parsed_pages,
        )

    async def _parse_backend_pages(
        self,
        *,
        pdf_path: str,
        resolved_document,
        triage_document,
        force_ocr: bool = False,
        task_hints: dict | None = None,
    ) -> list[PdfHybridParsedPage]:
        results: list[PdfHybridParsedPage] = []
        resolved_by_page = {
            int(page.page): page
            for page in list(getattr(resolved_document, "pages", []) or [])
        }
        backend_triage_pages = [
            triage_page
            for triage_page in list(getattr(triage_document, "pages", []) or [])
            if str(triage_page.decision or "") == "backend"
        ]
        if not bool(getattr(self._page_parser, "is_configured", lambda: False)()):
            for triage_page in backend_triage_pages:
                results.append(
                    PdfHybridParsedPage(
                        page=int(triage_page.page or 0),
                        model="",
                        error="backend_parser_not_configured",
                    )
                )
            return results

        requested_pages = []
        requested_triage_results = []
        missing_pages: set[int] = set()
        for triage_page in backend_triage_pages:
            resolved_page = resolved_by_page.get(int(triage_page.page or 0))
            if resolved_page is None:
                missing_pages.add(int(triage_page.page or 0))
                continue
            requested_pages.append(resolved_page)
            requested_triage_results.append(triage_page)

        parsed_by_page: dict[int, PdfHybridParsedPage] = {}
        if requested_pages:
            try:
                try:
                    parsed_pages = await self._page_parser.parse_pages(
                        pdf_path=pdf_path,
                        resolved_pages=requested_pages,
                        triage_results=requested_triage_results,
                        force_ocr=force_ocr,
                        task_hints=task_hints,
                    )
                except TypeError:
                    # Backward-compatible fallback for older parser signatures used by tests/mocks.
                    parsed_pages = await self._page_parser.parse_pages(
                        pdf_path=pdf_path,
                        resolved_pages=requested_pages,
                        triage_results=requested_triage_results,
                    )
            except Exception:
                # Batch-level fallback: keep the pipeline running, but record per-page failures
                # so trace semantics are explicit and stable.
                for triage_page in requested_triage_results:
                    results.append(
                        PdfHybridParsedPage(
                            page=int(triage_page.page or 0),
                            model="",
                            used=False,
                            error="backend_batch_failed",
                        )
                    )
                return results

            for parsed_page in list(parsed_pages or []):
                if parsed_page is None:
                    continue
                page_number = int(getattr(parsed_page, "page", 0) or 0)
                if page_number <= 0:
                    continue
                # Align "used" semantics with fusion: if a page has no blocks it should not be
                # treated as "used" even if the backend marked it so.
                if bool(getattr(parsed_page, "used", False)) and not list(getattr(parsed_page, "blocks", []) or []):
                    parsed_page.used = False
                    if not str(getattr(parsed_page, "error", "") or "").strip():
                        parsed_page.error = "backend_empty_result"
                parsed_by_page[page_number] = parsed_page

        for triage_page in backend_triage_pages:
            page_number = int(triage_page.page or 0)
            if page_number in missing_pages:
                results.append(
                    PdfHybridParsedPage(
                        page=page_number,
                        model="",
                        error="resolved_page_missing",
                    )
                )
                continue
            parsed_page = parsed_by_page.get(page_number)
            if parsed_page is None:
                results.append(
                    PdfHybridParsedPage(
                        page=page_number,
                        model="",
                        error="backend_parser_missing_result",
                    )
                )
                continue
            results.append(parsed_page)
        return results

    def _apply_postprocessors(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        structured_document = self._block_role_resolver.resolve_document(document=document)
        if self._auxiliary_block_resolver is not None:
            structured_document = self._auxiliary_block_resolver.resolve_document(document=structured_document)
        if self._front_matter_resolver is not None:
            structured_document = self._front_matter_resolver.resolve_document(document=structured_document)
        if self._heading_refiner is not None:
            structured_document = self._heading_refiner.resolve_document(document=structured_document)
        if self._toc_resolver is not None:
            structured_document = self._toc_resolver.resolve_document(document=structured_document)
        return self._section_resolver.resolve_document(document=structured_document)

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        token = str(mode or "auto").strip().lower()
        if token not in {"auto", "full"}:
            return "auto"
        return token

    def ensure_runtime_ready(self) -> None:
        ensure_runtime_dependencies = getattr(self._extractor, "ensure_runtime_dependencies", None)
        if callable(ensure_runtime_dependencies):
            ensure_runtime_dependencies()
