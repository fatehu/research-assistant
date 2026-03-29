from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from loguru import logger

from app.config import settings
from app.services.local_structured_pdf.contracts import (
    PdfBBox,
    PdfHybridExecutionResult,
    PdfSemanticBlock,
    PdfStructuredDocument,
)
from app.services.local_structured_pdf.docling_fast_hybrid_pipeline import (
    LocalStructuredPdfDoclingFastHybridPipeline,
)
from app.services.local_structured_pdf.markdown_renderer import LocalPdfMarkdownRenderer
from app.services.local_structured_pdf.pipeline import LocalStructuredPdfPipeline
from app.services.smart_chunking.types import ChunkLevel, generate_chunk_id


_SPACE_RE = re.compile(r"\s+")
_CITATION_RE = re.compile(r"\[[0-9,\-\s]+\]|\([12][0-9]{3}[a-z]?\)")


class PdfRagIngestService:
    def __init__(
        self,
        *,
        fast_pipeline: LocalStructuredPdfPipeline | None = None,
        hybrid_pipeline: LocalStructuredPdfDoclingFastHybridPipeline | None = None,
        renderer: LocalPdfMarkdownRenderer | None = None,
    ) -> None:
        self._fast_pipeline = fast_pipeline
        self._hybrid_pipeline = hybrid_pipeline
        self._renderer = renderer or LocalPdfMarkdownRenderer()

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
                "chunks": [],
                "extractor": extractor_name,
                "report": {
                    "pipeline": "pdf_structured_rag_v2",
                    "mode": selected_mode,
                    "document_name": document_name or "",
                },
            }

        document_text = self._renderer.render_document(document=document)
        chunks = self._build_chunks_from_document(document=document, mode=selected_mode)
        if not document_text.strip() or not chunks:
            return {
                "applied": False,
                "failure_reason": "no_structured_content",
                "document_text": document_text,
                "chunks": [],
                "extractor": extractor_name,
                "report": self._build_report(
                    document=document,
                    chunks=[],
                    mode=selected_mode,
                    document_name=document_name,
                    execution=execution,
                ),
            }

        return {
            "applied": True,
            "failure_reason": None,
            "document_text": document_text,
            "chunks": chunks,
            "extractor": extractor_name,
            "report": self._build_report(
                document=document,
                chunks=chunks,
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

    def _build_chunks_from_document(
        self,
        *,
        document: PdfStructuredDocument,
        mode: str,
    ) -> list[dict[str, Any]]:
        chunkable_blocks = [block for block in list(document.blocks or []) if self._render_block(block)]
        if not chunkable_blocks:
            return []

        chunks: list[dict[str, Any]] = []
        cursor = 0
        total_blocks = max(1, len(chunkable_blocks))
        for index, block in enumerate(chunkable_blocks):
            block_content = self._build_chunk_content(block)
            if not block_content:
                continue
            start_char = cursor
            end_char = start_char + len(block_content)
            cursor = end_char + 2

            section_titles = [str(item).strip() for item in list(block.section_titles or []) if str(item).strip()]
            section_title = self._resolve_section_title(block=block, section_titles=section_titles)
            pages = list(range(int(block.page_start), int(block.page_end) + 1))
            extra = {
                "source_kind": "pdf_structured_rag_v2",
                "structured_mode": mode,
                "block_id": block.block_id,
                "block_type": block.block_type,
                "raw_block_content": self._render_block(block),
                "pages": pages,
                "bbox": self._bbox_to_dict(block.bbox),
                "line_ids": list(block.line_ids or []),
                "page_span": [int(block.page_start), int(block.page_end)],
                "section_path_titles": section_titles,
                "section_path": str(block.section_path or ""),
                "heading_level": int(block.heading_level) if block.heading_level else None,
                "table_row_count": len(list(block.table_rows or [])),
            }
            meta = {
                "level": (
                    ChunkLevel.SECTION.value
                    if str(block.block_type or "").strip().lower() == "heading"
                    else ChunkLevel.PARAGRAPH.value
                ),
                "section_type": str(block.block_type or "paragraph"),
                "section_title": section_title,
                "has_citations": bool(_CITATION_RE.search(block_content)),
                "position_ratio": float(index + 1) / float(total_blocks),
                "keywords": [],
                "extra": extra,
            }
            chunks.append(
                {
                    "id": generate_chunk_id(block_content, start_char),
                    "content": block_content,
                    "start_char": start_char,
                    "end_char": end_char,
                    "metadata": meta,
                }
            )
        return chunks

    def _build_report(
        self,
        *,
        document: PdfStructuredDocument,
        chunks: list[dict[str, Any]],
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
            "chunk_count": len(chunks),
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

    def _build_chunk_content(self, block: PdfSemanticBlock) -> str:
        rendered = self._render_block(block)
        if not rendered:
            return ""
        if str(block.block_type or "").strip().lower() == "heading":
            return rendered

        section_titles = [str(item).strip() for item in list(block.section_titles or []) if str(item).strip()]
        if not section_titles:
            return rendered
        section_path = " > ".join(section_titles)
        return f"Section: {section_path}\n\n{rendered}".strip()

    def _render_block(self, block: PdfSemanticBlock) -> str:
        return self._renderer.render_document(
            document=PdfStructuredDocument(blocks=[block]),
        ).strip()

    @staticmethod
    def _resolve_section_title(*, block: PdfSemanticBlock, section_titles: list[str]) -> Optional[str]:
        block_type = str(block.block_type or "").strip().lower()
        if block_type == "heading":
            text = _normalize_spaces(block.text)
            return text or (section_titles[-1] if section_titles else None)
        if section_titles:
            return section_titles[-1]
        return None

    @staticmethod
    def _normalize_mode(value: str | None) -> str:
        normalized = str(value or "fast").strip().lower()
        if normalized not in {"fast", "hybrid"}:
            return "fast"
        return normalized

    @staticmethod
    def _bbox_to_dict(bbox: PdfBBox) -> dict[str, float]:
        return {
            "x0": float(bbox.x0),
            "top": float(bbox.top),
            "x1": float(bbox.x1),
            "bottom": float(bbox.bottom),
        }


def _normalize_spaces(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "")).strip()


_pdf_rag_ingest_service = PdfRagIngestService()


def get_pdf_rag_ingest_service() -> PdfRagIngestService:
    return _pdf_rag_ingest_service
