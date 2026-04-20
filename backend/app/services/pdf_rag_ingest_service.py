from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.config import settings
from app.services.local_structured_pdf.contracts import (
    PdfHybridExecutionResult,
    PdfStructuredDocument,
)
from app.services.local_structured_pdf.docling_fast_hybrid_pipeline import (
    LocalStructuredPdfDoclingFastHybridPipeline,
)
from app.services.local_structured_pdf.ingest_markdown_renderer import (
    LocalPdfIngestMarkdownRenderer,
)
from app.services.local_structured_pdf.pipeline import LocalStructuredPdfPipeline


class PdfRagIngestService:
    def __init__(
        self,
        *,
        fast_pipeline: LocalStructuredPdfPipeline | None = None,
        hybrid_pipeline: LocalStructuredPdfDoclingFastHybridPipeline | None = None,
        ingest_renderer: LocalPdfIngestMarkdownRenderer | None = None,
    ) -> None:
        self._fast_pipeline = fast_pipeline
        self._hybrid_pipeline = hybrid_pipeline
        self._ingest_renderer = ingest_renderer or LocalPdfIngestMarkdownRenderer()

    async def ingest_pdf(
        self,
        *,
        file_path: str,
        document_name: str = "",
        mode: str | None = None,
    ) -> dict[str, Any]:
        selected_mode = self._normalize_mode(mode or settings.pdf_rag_structured_mode)
        extractor_name = f"local_structured_pdf_{selected_mode}"
        try:
            document, execution = await self._parse_structured_document(
                file_path=file_path,
                mode=selected_mode,
            )
        except Exception as exc:
            logger.exception(f"[PdfRag] structured ingest failed mode={selected_mode}: {exc}")
            return {
                "applied": False,
                "failure_reason": f"structured_ingest_failed:{exc}",
                "document_text": "",
                "extractor": extractor_name,
                "report": {
                    "pipeline": "pdf_structured_rag_v2",
                    "mode": selected_mode,
                    "document_name": document_name or "",
                },
            }

        rendered = self._ingest_renderer.render_document(document=document)
        document_text = str(rendered.markdown or "")
        document_source_spans = [span.to_dict() for span in list(rendered.spans or [])]
        if not document_text.strip():
            return {
                "applied": False,
                "failure_reason": "no_structured_content",
                "document_text": document_text,
                "document_source_spans": document_source_spans,
                "extractor": extractor_name,
                "report": self._build_report(
                    document=document,
                    mode=selected_mode,
                    document_name=document_name,
                    execution=execution,
                ),
            }

        return {
            "applied": True,
            "failure_reason": None,
            "document_text": document_text,
            "document_source_spans": document_source_spans,
            "extractor": extractor_name,
            "report": self._build_report(
                document=document,
                mode=selected_mode,
                document_name=document_name,
                execution=execution,
            ),
        }

    async def _parse_structured_document(
        self,
        *,
        file_path: str,
        mode: str,
    ) -> tuple[PdfStructuredDocument, PdfHybridExecutionResult | None]:
        if mode == "hybrid":
            pipeline = self._get_hybrid_pipeline()
            execution = await pipeline.parse_document_with_trace(
                pdf_path=file_path,
                mode="auto",
            )
            return execution.document, execution

        pipeline = self._get_fast_pipeline()
        document = await asyncio.to_thread(
            pipeline.parse_document,
            pdf_path=file_path,
        )
        return document, None

    def _get_fast_pipeline(self) -> LocalStructuredPdfPipeline:
        if self._fast_pipeline is None:
            self._fast_pipeline = LocalStructuredPdfPipeline(heuristic_profile="balanced")
        return self._fast_pipeline

    def _get_hybrid_pipeline(self) -> LocalStructuredPdfDoclingFastHybridPipeline:
        if self._hybrid_pipeline is None:
            self._hybrid_pipeline = LocalStructuredPdfDoclingFastHybridPipeline(
                heuristic_profile="balanced",
            )
        return self._hybrid_pipeline

    def _build_report(
        self,
        *,
        document: PdfStructuredDocument,
        mode: str,
        document_name: str,
        execution: PdfHybridExecutionResult | None,
    ) -> dict[str, Any]:
        block_counts: dict[str, int] = {}
        for block in list(document.blocks or []):
            key = str(block.block_type or "unknown").strip().lower() or "unknown"
            block_counts[key] = int(block_counts.get(key, 0)) + 1

        report: dict[str, Any] = {
            "pipeline": "pdf_structured_rag_v2",
            "mode": mode,
            "document_name": document_name or "",
            "page_count": len(list(document.pages or [])),
            "block_count": len(list(document.blocks or [])),
            "block_type_counts": block_counts,
        }
        if execution is not None:
            report.update(
                {
                    "triage_mode": str(execution.mode or "auto"),
                    "backend_attempted_pages": list(execution.backend_attempted_pages or []),
                    "backend_used_pages": list(execution.backend_used_pages or []),
                    "backend_fallback_pages": list(execution.backend_fallback_pages or []),
                    "triage_backend_page_count": len(list(execution.triage.backend_pages or [])),
                    "triage_local_page_count": len(list(execution.triage.local_pages or [])),
                }
            )
        return report

    @staticmethod
    def _normalize_mode(value: str | None) -> str:
        normalized = str(value or "fast").strip().lower()
        if normalized not in {"fast", "hybrid"}:
            return "fast"
        return normalized


_pdf_rag_ingest_service = PdfRagIngestService()


def get_pdf_rag_ingest_service() -> PdfRagIngestService:
    return _pdf_rag_ingest_service
