from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Sequence

import httpx

from .contracts import (
    PdfBBox,
    PdfHybridExecutionResult,
    PdfHybridParsedBlock,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfPageAtoms,
    PdfResolvedDocument,
    PdfResolvedPage,
    PdfStructuredDocument,
)
from .docling_fast_triage_service import LocalPdfDoclingFastTriageService
from .hybrid_backend_transformer import LocalPdfHybridBackendTransformer
from .hybrid_fusion_service import LocalStructuredPdfHybridFusionService
from .hybrid_planner import LocalStructuredPdfHybridPlanner
from .ollama_page_parser import LocalOllamaQwenVlPageParser
from .pipeline import LocalStructuredPdfPipeline


class LocalStructuredPdfDoclingFastHybridPipeline:
    """Python-side docling-fast hybrid orchestrator, replacing the tmp JAR route."""

    def __init__(
        self,
        *,
        pipeline: LocalStructuredPdfPipeline | None = None,
        planner: LocalStructuredPdfHybridPlanner | None = None,
        backend_transformer: LocalPdfHybridBackendTransformer | None = None,
        fusion_service: LocalStructuredPdfHybridFusionService | None = None,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
        heuristic_profile: str = "balanced",
        backend_url: str | None = None,
        backend_timeout_seconds: float | None = None,
    ) -> None:
        self._pipeline = pipeline or LocalStructuredPdfPipeline(heuristic_profile=heuristic_profile)
        self._planner = planner or LocalStructuredPdfHybridPlanner(
            triage_service=LocalPdfDoclingFastTriageService(),
        )
        self._backend_transformer = backend_transformer or LocalPdfHybridBackendTransformer()
        self._fusion_service = fusion_service or LocalStructuredPdfHybridFusionService()
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()
        self._backend_url = self._normalize_backend_url(backend_url)
        self._backend_timeout_seconds = self._resolve_timeout_seconds(backend_timeout_seconds)

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
        local_document, triage_document, page_atoms, normalized_pages, resolved_document = self._build_local_artifacts(
            pdf_path=pdf_path,
            page_limit=page_limit,
            mode=mode,
            include_chars=include_chars,
        )
        if not list(getattr(triage_document, "pages", []) or []):
            return PdfHybridExecutionResult(
                mode=self._normalize_mode(mode),
                document=local_document,
                triage=triage_document,
                parsed_pages=[],
            )

        parsed_pages = await self._parse_backend_pages(
            pdf_path=pdf_path,
            triage_document=triage_document,
            resolved_document=resolved_document,
        )
        fused_document = self._fusion_service.fuse_document(
            resolved_document=resolved_document,
            local_document=local_document,
            triage_document=triage_document,
            parsed_pages=parsed_pages,
        )
        final_document = self._apply_pipeline_postprocessors(document=fused_document)
        return PdfHybridExecutionResult(
            mode=str(triage_document.mode or self._normalize_mode(mode)),
            document=final_document,
            triage=triage_document,
            parsed_pages=parsed_pages,
        )

    def ensure_runtime_ready(self) -> None:
        self._pipeline.ensure_runtime_ready()

    def _build_local_artifacts(
        self,
        *,
        pdf_path: str,
        page_limit: int | None,
        mode: str,
        include_chars: bool,
    ) -> tuple[
        PdfStructuredDocument,
        PdfHybridTriageDocument,
        list[PdfPageAtoms],
        list[Any],
        PdfResolvedDocument,
    ]:
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
            return (
                PdfStructuredDocument(),
                PdfHybridTriageDocument(mode=self._normalize_mode(mode), pages=[]),
                [],
                [],
                PdfResolvedDocument(),
            )

        normalized_pages = [normalizer.normalize_page(page_atoms=page) for page in page_atoms]
        resolved_document = document_resolver.resolve_document(pages=normalized_pages)
        structured_document = block_builder.build_document(document=resolved_document)
        triage_document = self._planner.plan_from_artifacts(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
            mode=mode,
        )
        structured_document = table_detector.detect_document(
            page_atoms=page_atoms,
            normalized_pages=normalized_pages,
            resolved_document=resolved_document,
            structured_document=structured_document,
        )
        return structured_document, triage_document, page_atoms, normalized_pages, resolved_document

    async def _parse_backend_pages(
        self,
        *,
        pdf_path: str,
        triage_document: PdfHybridTriageDocument,
        resolved_document: PdfResolvedDocument,
    ) -> list[PdfHybridParsedPage]:
        backend_pages = sorted(int(page) for page in list(triage_document.backend_pages or []) if int(page) > 0)
        if not backend_pages:
            return []

        resolved_by_page = {
            int(page.page): page
            for page in list(getattr(resolved_document, "pages", []) or [])
            if isinstance(page, PdfResolvedPage)
        }
        missing_pages = [page for page in backend_pages if page not in resolved_by_page]
        page_map = {
            int(item.page): item
            for item in list(getattr(triage_document, "pages", []) or [])
        }
        parsed_pages: dict[int, PdfHybridParsedPage] = {
            page: PdfHybridParsedPage(page=page, model="docling-fast", error="resolved_page_missing")
            for page in missing_pages
        }

        requested_pages = [page for page in backend_pages if page in resolved_by_page]
        if not requested_pages:
            return [parsed_pages[page] for page in backend_pages]

        response_payload, failed_pages, error = await self._convert_via_backend(
            pdf_path=pdf_path,
            page_numbers=requested_pages,
        )
        if error:
            for page in requested_pages:
                parsed_pages[page] = PdfHybridParsedPage(
                    page=page,
                    model="docling-fast",
                    attempted_models=["docling-fast"],
                    protocol="docling-fast",
                    used=False,
                    error=error,
                )
            return [parsed_pages[page] for page in backend_pages]

        for page in requested_pages:
            if page in failed_pages:
                parsed_pages[page] = PdfHybridParsedPage(
                    page=page,
                    model="docling-fast",
                    attempted_models=["docling-fast"],
                    protocol="docling-fast",
                    used=False,
                    error="backend_partial_failure",
                )
                continue
            resolved_page = resolved_by_page[page]
            triage_page = page_map.get(page)
            parsed_pages[page] = self._transform_backend_page(
                page=page,
                json_content=response_payload,
                resolved_page=resolved_page,
                triage_page=triage_page,
            )

        return [parsed_pages[page] for page in backend_pages]

    async def _convert_via_backend(
        self,
        *,
        pdf_path: str,
        page_numbers: Sequence[int],
    ) -> tuple[dict[str, Any] | None, set[int], str]:
        if not page_numbers:
            return {}, set(), ""

        try:
            pdf_bytes = await asyncio.to_thread(Path(pdf_path).read_bytes)
        except Exception as exc:
            return None, set(), f"backend_pdf_read_failed:{exc}"

        timeout = httpx.Timeout(self._backend_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._backend_url}/v1/convert/file",
                    files={"files": ("document.pdf", pdf_bytes, "application/pdf")},
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return None, set(), f"backend_request_failed:{exc}"

        if str(payload.get("status") or "").strip().lower() == "failure":
            errors = list(payload.get("errors") or [])
            error_text = "; ".join(str(item).strip() for item in errors if str(item).strip()) or "backend_failure"
            return None, set(), error_text

        document = payload.get("document")
        if not isinstance(document, dict):
            return None, set(), "backend_document_missing"
        json_content = document.get("json_content")
        if not isinstance(json_content, dict):
            return None, set(), "backend_json_content_missing"
        failed_pages = {
            int(item)
            for item in list(payload.get("failed_pages") or [])
            if isinstance(item, int) or str(item).isdigit()
        }
        return json_content, failed_pages, ""

    def _transform_backend_page(
        self,
        *,
        page: int,
        json_content: dict[str, Any],
        resolved_page: PdfResolvedPage,
        triage_page: Any,
    ) -> PdfHybridParsedPage:
        prompt_payload = self._build_backend_prompt_payload(
            page=page,
            resolved_page=resolved_page,
            triage_page=triage_page,
        )
        page_payload = dict(json_content)
        page_payload["page"] = int(page)
        transformed = self._backend_transformer.transform_payload(
            payload=page_payload,
            prompt_payload=prompt_payload,
        )
        if not isinstance(transformed, dict):
            return PdfHybridParsedPage(
                page=page,
                model="docling-fast",
                attempted_models=["docling-fast"],
                protocol="docling-fast",
                used=False,
                error="backend_page_transform_failed",
            )
        blocks = self._build_parsed_blocks_from_transformed(
            page=page,
            transformed=transformed,
        )
        return PdfHybridParsedPage(
            page=page,
            model="docling-fast",
            page_role=str(transformed.get("page_role") or "unknown").strip() or "unknown",
            blocks=blocks,
            notes=[
                str(item).strip()
                for item in list(transformed.get("notes") or [])[:24]
                if str(item).strip()
            ],
            attempted_models=["docling-fast"],
            protocol="docling-fast",
            used=True,
            error="",
        )

    @staticmethod
    def _build_backend_prompt_payload(
        *,
        page: int,
        resolved_page: PdfResolvedPage,
        triage_page: Any,
    ) -> dict[str, Any]:
        meta = getattr(resolved_page, "meta", None)
        line_rows = LocalStructuredPdfDoclingFastHybridPipeline._select_line_rows(
            resolved_page=resolved_page,
        )
        return {
            "page": int(page),
            "page_width": float(getattr(meta, "page_width", 0.0) or 0.0),
            "page_height": float(getattr(meta, "page_height", 0.0) or 0.0),
            "triage": {
                "page_type": str(getattr(triage_page, "page_type", "") or "").strip().lower(),
            },
            "line_rows": line_rows,
        }

    @staticmethod
    def _select_line_rows(*, resolved_page: PdfResolvedPage) -> list[dict[str, Any]]:
        lines = [
            line
            for line in list(getattr(resolved_page, "lines", []) or [])
            if str(getattr(line, "text", "") or "").strip()
        ]
        selected = sorted(
            lines,
            key=lambda item: (
                int(getattr(item, "reading_order", 0) or 0),
                round(float(item.bbox.top), 2),
                round(float(item.bbox.x0), 2),
                str(item.line_id),
            ),
        )
        return [
            {
                "line_id": str(line.line_id),
                "text": str(line.text or ""),
                "band": str(line.band or "body"),
                "column_id": str(line.column_id or "main"),
                "region": str(line.region or "main"),
                "reading_order": int(line.reading_order or 0),
                "bbox": {
                    "x0": round(float(line.bbox.x0), 2),
                    "top": round(float(line.bbox.top), 2),
                    "x1": round(float(line.bbox.x1), 2),
                    "bottom": round(float(line.bbox.bottom), 2),
                },
            }
            for line in selected
        ]

    @staticmethod
    def _build_parsed_blocks_from_transformed(
        *,
        page: int,
        transformed: dict[str, Any],
    ) -> list[PdfHybridParsedBlock]:
        rows: list[PdfHybridParsedBlock] = []
        for item in list(transformed.get("blocks") or []):
            if not isinstance(item, dict):
                continue
            bbox = item.get("bbox")
            if not isinstance(bbox, dict):
                continue
            try:
                parsed_bbox = PdfBBox(
                    x0=float(bbox.get("x0")),
                    top=float(bbox.get("top")),
                    x1=float(bbox.get("x1")),
                    bottom=float(bbox.get("bottom")),
                )
            except (TypeError, ValueError):
                continue
            rows.append(
                PdfHybridParsedBlock(
                    block_id=str(item.get("block_id") or ""),
                    kind=str(item.get("kind") or "unknown").strip().lower() or "unknown",
                    page=int(item.get("page") or page),
                    reading_order=max(1, int(item.get("reading_order") or len(rows) + 1)),
                    text=str(item.get("text") or "").strip(),
                    bbox=parsed_bbox,
                    source_line_ids=[
                        str(line_id).strip()
                        for line_id in list(item.get("source_line_ids") or [])
                        if str(line_id).strip()
                    ],
                    table_rows=[
                        [str(cell).strip() for cell in list(row or [])]
                        for row in list(item.get("table_rows") or [])
                        if isinstance(row, list)
                    ],
                    zone=str(item.get("zone") or "main").strip().lower() or "main",
                    merge_strategy=str(item.get("merge_strategy") or "space").strip().lower() or "space",
                    confidence=max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                    heading_level=(
                        int(item.get("heading_level"))
                        if str(item.get("heading_level") or "").strip().isdigit()
                        else None
                    ),
                )
            )
        return rows

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

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        token = str(mode or "auto").strip().lower()
        if token not in {"auto", "full"}:
            return "auto"
        return token

    @staticmethod
    def _normalize_backend_url(value: str | None) -> str:
        raw = str(value or os.getenv("DOCLING_URL") or "http://localhost:5002").strip()
        return raw.rstrip("/")

    @staticmethod
    def _resolve_timeout_seconds(value: float | None) -> float:
        if value is not None:
            return max(5.0, float(value))
        env_value = str(os.getenv("HYBRID_TIMEOUT") or "").strip()
        if env_value:
            try:
                numeric = float(env_value)
                if numeric > 1000:
                    return max(5.0, numeric / 1000.0)
                return max(5.0, numeric)
            except ValueError:
                pass
        return 600.0
