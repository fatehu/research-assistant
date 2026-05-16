from __future__ import annotations

import asyncio

from .contracts import (
    PdfPageAtoms,
    PdfHybridExecutionResult,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfStructuredDocument,
)
from .formula_enrichment_service import LocalPdfFormulaEnrichmentService
from .hybrid_planner import LocalStructuredPdfHybridPlanner
from .ocr_enrichment_service import LocalPdfOcrEnrichmentService
from .ollama_page_parser import LocalOllamaQwenVlPageParser
from .picture_enrichment_service import LocalPdfPictureEnrichmentService
from .pipeline import LocalStructuredPdfPipeline


class LocalStructuredPdfCompatHybridPipeline:
    """Java-style hybrid route using deterministic parsing plus Qwen slot enrichments."""

    def __init__(
        self,
        *,
        pipeline: LocalStructuredPdfPipeline | None = None,
        planner: LocalStructuredPdfHybridPlanner | None = None,
        ocr_enrichment_service: LocalPdfOcrEnrichmentService | None = None,
        formula_enrichment_service: LocalPdfFormulaEnrichmentService | None = None,
        picture_enrichment_service: LocalPdfPictureEnrichmentService | None = None,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
        heuristic_profile: str = "balanced",
    ) -> None:
        self._pipeline = pipeline or LocalStructuredPdfPipeline(heuristic_profile=heuristic_profile)
        self._planner = planner or LocalStructuredPdfHybridPlanner()
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()
        self._ocr_enrichment_service = ocr_enrichment_service or LocalPdfOcrEnrichmentService(page_parser=self._page_parser)
        self._formula_enrichment_service = formula_enrichment_service or LocalPdfFormulaEnrichmentService(page_parser=self._page_parser)
        self._picture_enrichment_service = picture_enrichment_service or LocalPdfPictureEnrichmentService(page_parser=self._page_parser)

    async def parse_document(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        mode: str = "auto",
        include_chars: bool = False,
    ) -> PdfStructuredDocument:
        result = await self.parse_document_with_trace(
            pdf_path=pdf_path,
            page_limit=page_limit,
            mode=mode,
            include_chars=include_chars,
        )
        return result.document

    async def parse_document_with_trace(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        mode: str = "auto",
        include_chars: bool = False,
    ) -> PdfHybridExecutionResult:
        base_document, triage_document, page_atoms = self._build_local_document_and_triage(
            pdf_path=pdf_path,
            page_limit=page_limit,
            mode=mode,
            include_chars=include_chars,
        )
        ocr_pages, formula_pages, picture_pages = self._route_stage_pages(triage=triage_document)

        if not self._page_parser.is_configured():
            return PdfHybridExecutionResult(
                mode=str(triage_document.mode or self._normalize_mode(mode)),
                document=base_document,
                triage=triage_document,
                parsed_pages=[
                    PdfHybridParsedPage(
                        page=int(item.page),
                        model="",
                        used=False,
                        notes=["compat_backend_slot_skipped"],
                        error="backend_parser_not_configured",
                    )
                    for item in list(triage_document.pages or [])
                    if self._page_requires_slot(item.page, ocr_pages, formula_pages, picture_pages)
                ],
            )

        document = base_document
        if ocr_pages:
            document = await self._ocr_enrichment_service.enrich_document(
                pdf_path=pdf_path,
                document=document,
                page_numbers=ocr_pages,
                page_atoms=page_atoms,
            )
        if formula_pages:
            document = await self._formula_enrichment_service.enrich_document(
                pdf_path=pdf_path,
                document=document,
                page_numbers=formula_pages,
            )
        if picture_pages:
            await self._picture_enrichment_service.enrich_document(
                pdf_path=pdf_path,
                document=document,
                page_numbers=picture_pages,
                page_atoms=page_atoms,
            )

        parsed_pages: list[PdfHybridParsedPage] = []
        for triage_page in list(triage_document.pages or []):
            stages: list[str] = []
            page_number = int(triage_page.page or 0)
            if page_number in ocr_pages:
                stages.append("ocr")
            if page_number in formula_pages:
                stages.append("formula")
            if page_number in picture_pages:
                stages.append("picture_description")
            if not stages:
                continue
            parsed_pages.append(
                PdfHybridParsedPage(
                    page=page_number,
                    model="qwen_slot_adapter",
                    page_role=str(triage_page.page_type or "unknown"),
                    notes=[f"stage:{stage}" for stage in stages],
                    attempted_models=["qwen_slot_adapter"],
                    used=True,
                )
            )

        return PdfHybridExecutionResult(
            mode=str(triage_document.mode or self._normalize_mode(mode)),
            document=document,
            triage=triage_document,
            parsed_pages=parsed_pages,
        )

    def ensure_runtime_ready(self) -> None:
        self._pipeline.ensure_runtime_ready()

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        token = str(mode or "auto").strip().lower()
        if token not in {"auto", "full"}:
            return "auto"
        return token

    def _route_stage_pages(self, *, triage: PdfHybridTriageDocument) -> tuple[set[int], set[int], set[int]]:
        mode = self._normalize_mode(getattr(triage, "mode", "auto"))
        all_pages = {int(item.page) for item in list(triage.pages or []) if int(item.page or 0) > 0}
        if mode == "full":
            return set(all_pages), set(all_pages), set(all_pages)

        ocr_pages: set[int] = set()
        formula_pages: set[int] = set()
        picture_pages: set[int] = set()
        for item in list(triage.pages or []):
            if str(item.decision or "") != "backend":
                continue
            page_number = int(item.page or 0)
            page_type = str(item.page_type or "").strip().lower()
            if page_type == "visual_or_scanned":
                ocr_pages.add(page_number)
            elif page_type == "formula_or_display_heavy":
                formula_pages.add(page_number)
        return ocr_pages, formula_pages, picture_pages

    @staticmethod
    def _page_requires_slot(page: int, ocr_pages: set[int], formula_pages: set[int], picture_pages: set[int]) -> bool:
        page_number = int(page or 0)
        return page_number in ocr_pages or page_number in formula_pages or page_number in picture_pages

    def _build_local_document_and_triage(
        self,
        *,
        pdf_path: str,
        page_limit: int | None,
        mode: str,
        include_chars: bool = False,
    ) -> tuple[PdfStructuredDocument, PdfHybridTriageDocument, list[PdfPageAtoms]]:
        extractor = self._pipeline._extractor
        normalizer = self._pipeline._normalizer
        document_resolver = self._pipeline._document_resolver
        block_builder = self._pipeline._block_builder
        table_detector = self._pipeline._table_detector

        page_atoms = extractor.extract_document_atoms(
            pdf_path=pdf_path,
            page_limit=page_limit,
            include_chars=include_chars,
        )
        if not page_atoms:
            return PdfStructuredDocument(), PdfHybridTriageDocument(mode=self._normalize_mode(mode), pages=[]), []

        normalized_pages = [normalizer.normalize_page(page_atoms=page) for page in page_atoms]
        resolved_document = document_resolver.resolve_document(pages=normalized_pages)
        structured_document = block_builder.build_document(document=resolved_document)
        structured_document = table_detector.detect_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
        )
        triage_document = self._planner.plan_from_artifacts(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
            mode=mode,
        )
        return self._apply_pipeline_postprocessors(document=structured_document), triage_document, page_atoms

    def _apply_pipeline_postprocessors(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        structured_document = self._pipeline._block_role_resolver.resolve_document(document=document)
        if self._pipeline._auxiliary_block_resolver is not None:
            structured_document = self._pipeline._auxiliary_block_resolver.resolve_document(document=structured_document)
        if self._pipeline._front_matter_resolver is not None:
            structured_document = self._pipeline._front_matter_resolver.resolve_document(document=structured_document)
        if self._pipeline._heading_refiner is not None:
            structured_document = self._pipeline._heading_refiner.resolve_document(document=structured_document)
        if self._pipeline._toc_resolver is not None:
            structured_document = self._pipeline._toc_resolver.resolve_document(document=structured_document)
        return self._pipeline._section_resolver.resolve_document(document=structured_document)
