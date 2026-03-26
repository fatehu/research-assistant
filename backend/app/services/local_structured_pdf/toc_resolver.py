from __future__ import annotations

from dataclasses import replace
import re

from .contracts import PdfBBox, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage


_SPACE_RE = re.compile(r"\s+")
_TOC_TITLE_RE = re.compile(r"^(?:table\s+of\s+contents?|contents?)$", re.IGNORECASE)
_PAGE_NUMBER_TOKEN_RE = re.compile(r"^(?:\d+|[ivxlcdm]+)$", re.IGNORECASE)


class LocalPdfTocResolver:
    """Stage-4.7 resolver: normalize table-of-contents pages into plain entries."""

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        page_blocks_map = self._build_page_blocks_map(document=document)
        resolved_blocks: list[PdfSemanticBlock] = []

        for page in list(document.pages or []):
            blocks = list(page_blocks_map.get(int(page.page), []))
            if not blocks:
                continue
            toc_title_index = self._toc_title_index(blocks=blocks, page_height=float(page.meta.page_height or 0.0))
            if toc_title_index is None:
                resolved_blocks.extend(blocks)
                continue
            resolved_blocks.extend(self._resolve_toc_page(blocks=blocks, toc_title_index=toc_title_index))

        page_map: dict[int, list[PdfSemanticBlock]] = {}
        for block in resolved_blocks:
            for page_number in range(int(block.page_start), int(block.page_end) + 1):
                page_map.setdefault(page_number, []).append(block)

        return PdfStructuredDocument(
            pages=[
                PdfStructuredPage(
                    meta=page.meta,
                    blocks=page_map.get(int(page.page), []),
                )
                for page in list(document.pages or [])
            ],
            blocks=resolved_blocks,
            body_font_size=float(document.body_font_size or 0.0),
        )

    @staticmethod
    def _build_page_blocks_map(*, document: PdfStructuredDocument) -> dict[int, list[PdfSemanticBlock]]:
        if any(page.blocks for page in list(document.pages or [])):
            return {
                int(page.page): list(page.blocks or [])
                for page in list(document.pages or [])
            }

        page_map: dict[int, list[PdfSemanticBlock]] = {}
        for block in list(document.blocks or []):
            page_map.setdefault(int(block.page_start), []).append(block)
        return page_map

    def _toc_title_index(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        page_height: float,
    ) -> int | None:
        for index, block in enumerate(blocks):
            if str(block.block_type or "") != "heading":
                continue
            text = _SPACE_RE.sub(" ", str(block.text or "").strip())
            if not _TOC_TITLE_RE.match(text):
                continue
            if page_height > 0.0 and float(block.bbox.top) > page_height * 0.3:
                continue
            return index
        return None

    def _resolve_toc_page(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        toc_title_index: int,
    ) -> list[PdfSemanticBlock]:
        resolved: list[PdfSemanticBlock] = []
        pending_entry_indices: list[int] = []

        for index, block in enumerate(blocks):
            if index <= toc_title_index:
                resolved.append(block)
                pending_entry_indices.clear()
                continue

            tokens = self._page_number_tokens(block.text)
            if tokens:
                if self._assign_page_number_tokens(resolved=resolved, pending_entry_indices=pending_entry_indices, tokens=tokens, source_block=block):
                    pending_entry_indices.clear()
                    continue
                resolved.append(block)
                pending_entry_indices.clear()
                continue

            normalized = self._normalize_toc_entry_block(block)
            resolved.append(normalized)
            if self._is_toc_entry_candidate(normalized):
                pending_entry_indices.append(len(resolved) - 1)
            else:
                pending_entry_indices.clear()

        return resolved

    @staticmethod
    def _normalize_toc_entry_block(block: PdfSemanticBlock) -> PdfSemanticBlock:
        if str(block.block_type or "") == "heading":
            return replace(block, block_type="paragraph", heading_level=None)
        if str(block.block_type or "") == "list_item":
            return replace(block, block_type="paragraph")
        return block

    @staticmethod
    def _is_toc_entry_candidate(block: PdfSemanticBlock) -> bool:
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not text:
            return False
        if _TOC_TITLE_RE.match(text):
            return False
        return True

    @staticmethod
    def _page_number_tokens(text: str) -> list[str]:
        normalized = _SPACE_RE.sub(" ", str(text or "").strip())
        if not normalized:
            return []
        tokens = [token for token in normalized.split(" ") if token]
        if not tokens:
            return []
        if not all(_PAGE_NUMBER_TOKEN_RE.match(token) for token in tokens):
            return []
        return tokens

    def _assign_page_number_tokens(
        self,
        *,
        resolved: list[PdfSemanticBlock],
        pending_entry_indices: list[int],
        tokens: list[str],
        source_block: PdfSemanticBlock,
    ) -> bool:
        if not pending_entry_indices:
            return False
        if len(tokens) == 1:
            target_index = pending_entry_indices[-1]
            resolved[target_index] = self._append_page_number(
                block=resolved[target_index],
                page_number=tokens[0],
                source_block=source_block,
            )
            return True
        if len(tokens) != len(pending_entry_indices):
            return False
        for target_index, token in zip(pending_entry_indices, tokens):
            resolved[target_index] = self._append_page_number(
                block=resolved[target_index],
                page_number=token,
                source_block=source_block,
            )
        return True

    @staticmethod
    def _append_page_number(
        *,
        block: PdfSemanticBlock,
        page_number: str,
        source_block: PdfSemanticBlock,
    ) -> PdfSemanticBlock:
        merged_text = f"{str(block.text or '').strip()} {str(page_number or '').strip()}".strip()
        return replace(
            block,
            text=merged_text,
            bbox=PdfBBox(
                x0=min(float(block.bbox.x0), float(source_block.bbox.x0)),
                top=min(float(block.bbox.top), float(source_block.bbox.top)),
                x1=max(float(block.bbox.x1), float(source_block.bbox.x1)),
                bottom=max(float(block.bbox.bottom), float(source_block.bbox.bottom)),
            ),
            line_ids=list(block.line_ids or []) + list(source_block.line_ids or []),
            reading_order_end=max(int(block.reading_order_end or 0), int(source_block.reading_order_end or 0)),
        )
