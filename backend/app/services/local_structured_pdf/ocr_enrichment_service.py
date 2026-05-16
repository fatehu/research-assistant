from __future__ import annotations

import asyncio
from dataclasses import replace

from .contracts import PdfBBox, PdfPageAtoms, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage
from .native_extractor import LocalPdfNativeExtractor
from .ollama_page_parser import LocalOllamaQwenVlPageParser


class LocalPdfOcrEnrichmentService:
    """Upstream-style OCR stage backed by Qwen page transcription."""

    def __init__(
        self,
        *,
        extractor: LocalPdfNativeExtractor | None = None,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
    ) -> None:
        self._extractor = extractor or LocalPdfNativeExtractor()
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()

    async def enrich_document(
        self,
        *,
        pdf_path: str,
        document: PdfStructuredDocument,
        ocr_lang: list[str] | None = None,
        page_numbers: set[int] | None = None,
        page_atoms: list[PdfPageAtoms] | None = None,
    ) -> PdfStructuredDocument:
        del ocr_lang
        available_page_atoms = page_atoms
        if available_page_atoms is None:
            available_page_atoms = await asyncio.to_thread(
                self._extractor.extract_document_atoms,
                pdf_path=pdf_path,
            )
        if not available_page_atoms:
            return document

        existing_by_page = {
            int(page.page): page
            for page in list(getattr(document, "pages", []) or [])
        }
        rebuilt_pages: list[PdfStructuredPage] = []
        rebuilt_blocks: list[PdfSemanticBlock] = []

        for atoms in available_page_atoms:
            page_number = int(atoms.page)
            if page_numbers is not None and page_number not in page_numbers:
                page = existing_by_page.get(page_number)
                rebuilt_page = page or PdfStructuredPage(meta=atoms.meta, blocks=[])
                rebuilt_pages.append(rebuilt_page)
                rebuilt_blocks.extend(list(rebuilt_page.blocks or []))
                continue
            page_width = float(atoms.meta.page_width or 0.0)
            page_height = float(atoms.meta.page_height or 0.0)
            page = existing_by_page.get(page_number)
            existing_blocks = list(getattr(page, "blocks", []) or []) if page is not None else []
            page_meta = atoms.meta if page is None else page.meta

            ocr_text, _model = await self._page_parser.transcribe_page_text(
                pdf_path=pdf_path,
                page=page_number,
                page_type="visual_or_scanned",
            )
            if not ocr_text:
                rebuilt_page = page or PdfStructuredPage(meta=page_meta, blocks=[])
                rebuilt_pages.append(rebuilt_page)
                rebuilt_blocks.extend(list(rebuilt_page.blocks or []))
                continue

            ocr_block = PdfSemanticBlock(
                block_id=f"ocr_p{page_number:04d}_b0001",
                block_type="paragraph",
                page_start=page_number,
                page_end=page_number,
                text=ocr_text,
                bbox=PdfBBox(
                    x0=0.0,
                    top=0.0,
                    x1=max(1.0, page_width),
                    bottom=max(1.0, page_height),
                ),
                line_ids=[],
                column_id="main",
                region="main",
                reading_order_start=1,
                reading_order_end=1,
            )
            preserved_blocks = [
                block
                for block in existing_blocks
                if str(getattr(block, "block_type", "") or "").strip().lower() in {"table", "equation", "figure_meta"}
            ]
            new_blocks = [ocr_block, *preserved_blocks]
            rebuilt_page = replace(page, blocks=new_blocks) if page is not None else PdfStructuredPage(meta=page_meta, blocks=new_blocks)
            rebuilt_pages.append(rebuilt_page)
            rebuilt_blocks.extend(new_blocks)

        return replace(document, pages=rebuilt_pages, blocks=rebuilt_blocks)
