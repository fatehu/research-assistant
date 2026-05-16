from __future__ import annotations

import asyncio
import re
from dataclasses import replace

from .contracts import PdfBBox, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage
from .ollama_page_parser import LocalOllamaQwenVlPageParser


class LocalPdfFormulaEnrichmentService:
    """Upstream-style formula enrich stage backed by Qwen region extraction."""

    def __init__(
        self,
        *,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
    ) -> None:
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()

    async def enrich_document(
        self,
        *,
        pdf_path: str,
        document: PdfStructuredDocument,
        page_numbers: set[int] | None = None,
    ) -> PdfStructuredDocument:
        equation_blocks: list[PdfSemanticBlock] = [
            block
            for block in list(getattr(document, "blocks", []) or [])
            if page_numbers is None or int(getattr(block, "page_start", 0) or 0) in page_numbers
            if str(getattr(block, "block_type", "") or "").strip().lower() == "equation"
        ]
        if not equation_blocks:
            # Minimal adapter fallback: when upstream-style equation objects are
            # absent, enrich text blocks that look formula-like.
            equation_blocks = [
                block
                for block in list(getattr(document, "blocks", []) or [])
                if page_numbers is None or int(getattr(block, "page_start", 0) or 0) in page_numbers
                if self._looks_formula_like(str(getattr(block, "text", "") or ""))
            ]
        if not equation_blocks:
            equation_blocks = [
                PdfSemanticBlock(
                    block_id=f"formula_p{int(page.page):04d}_b0001",
                    block_type="equation",
                    page_start=int(page.page),
                    page_end=int(page.page),
                    text="",
                    bbox=PdfBBox(
                        x0=0.0,
                        top=0.0,
                        x1=float(page.meta.page_width or 0.0),
                        bottom=float(page.meta.page_height or 0.0),
                    ),
                    line_ids=[],
                )
                for page in list(getattr(document, "pages", []) or [])
                if page_numbers is None or int(page.page) in page_numbers
            ]

        page_updates: dict[int, dict[str, str]] = {}
        for block in equation_blocks:
            text, _model = await self._page_parser.describe_formula_region(
                pdf_path=pdf_path,
                page=int(block.page_start),
                bbox=block.bbox,
            )
            text = str(text or "").strip()
            if not text:
                continue
            page_updates.setdefault(int(block.page_start), {})[str(block.block_id)] = text

        if not page_updates:
            return document

        rebuilt_pages: list[PdfStructuredPage] = []
        rebuilt_blocks: list[PdfSemanticBlock] = []
        for page in list(getattr(document, "pages", []) or []):
            if page_numbers is not None and int(page.page) not in page_numbers:
                rebuilt_pages.append(page)
                rebuilt_blocks.extend(list(getattr(page, "blocks", []) or []))
                continue
            updates = page_updates.get(int(page.page), {})
            rebuilt_page_blocks: list[PdfSemanticBlock] = []
            consumed_ids: set[str] = set()
            for block in list(getattr(page, "blocks", []) or []):
                new_text = updates.get(str(block.block_id))
                if new_text:
                    consumed_ids.add(str(block.block_id))
                    rebuilt_type = str(getattr(block, "block_type", "") or "").strip().lower()
                    if rebuilt_type != "equation":
                        rebuilt_block = replace(block, block_type="equation", text=new_text)
                    else:
                        rebuilt_block = replace(block, text=new_text)
                else:
                    rebuilt_block = block
                rebuilt_page_blocks.append(rebuilt_block)
                rebuilt_blocks.append(rebuilt_block)
            for block_id, new_text in updates.items():
                if block_id in consumed_ids:
                    continue
                appended_block = PdfSemanticBlock(
                    block_id=str(block_id),
                    block_type="equation",
                    page_start=int(page.page),
                    page_end=int(page.page),
                    text=str(new_text),
                    bbox=PdfBBox(
                        x0=0.0,
                        top=0.0,
                        x1=float(page.meta.page_width or 0.0),
                        bottom=float(page.meta.page_height or 0.0),
                    ),
                    line_ids=[],
                )
                rebuilt_page_blocks.append(appended_block)
                rebuilt_blocks.append(appended_block)
            rebuilt_pages.append(replace(page, blocks=rebuilt_page_blocks))
        return replace(document, pages=rebuilt_pages, blocks=rebuilt_blocks)

    @staticmethod
    def _looks_formula_like(text: str) -> bool:
        payload = str(text or "").strip()
        if not payload:
            return False
        if len(payload) > 220:
            return False
        pattern = r"(=|\\frac|\\sum|\\int|\\sqrt|[A-Za-z]\([^)]*\)|\d+\s*[\+\-\*/]\s*\d+)"
        return bool(re.search(pattern, payload))
