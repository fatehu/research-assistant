from __future__ import annotations

from dataclasses import replace
import re

from .contracts import PdfBBox, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage


_SPACE_RE = re.compile(r"\s+")
_PAGE_NUMBER_TOKEN_RE = re.compile(r"^(?:\d+|[ivxlcdm]+)$", re.IGNORECASE)
_DOT_LEADER_ENTRY_RE = re.compile(r"\.{2,}\s*(\d+|[ivxlcdm]+)\s*$", re.IGNORECASE)


class LocalPdfBlockRoleResolver:
    """Stage-4.3 resolver: normalize directory-like entry list pages."""

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        page_blocks_map = self._build_page_blocks_map(document=document)
        resolved_blocks: list[PdfSemanticBlock] = []

        for page in list(document.pages or []):
            blocks = list(page_blocks_map.get(int(page.page), []))
            if not blocks:
                continue
            if self._is_directory_like_page(blocks=blocks, page_height=float(page.meta.page_height or 0.0)):
                resolved_blocks.extend(
                    self._resolve_directory_page(
                        blocks=blocks,
                        page_height=float(page.meta.page_height or 0.0),
                    )
                )
                continue
            resolved_blocks.extend(blocks)

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

    def _is_directory_like_page(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        page_height: float,
    ) -> bool:
        text_blocks = [block for block in blocks if self._normalized_text(block.text)]
        if len(text_blocks) < 4:
            return False
        if any(str(block.block_type or "") == "table" for block in text_blocks):
            return False

        body_blocks = text_blocks[1:] if self._has_page_title(block=text_blocks[0], page_height=page_height, page_blocks=text_blocks) else text_blocks
        if len(body_blocks) < 3:
            return False

        page_number_only_count = sum(1 for block in body_blocks if self._is_page_number_only_block(block))
        page_number_signal_count = sum(self._page_number_signal_count(block) for block in body_blocks)
        entry_like_count = sum(1 for block in body_blocks if self._is_directory_entry_candidate(block))
        heading_like_count = sum(
            1
            for block in body_blocks
            if str(block.block_type or "") in {"heading", "list_item"}
        )
        prose_like_count = sum(1 for block in body_blocks if self._looks_prose_block(block))

        if page_number_signal_count < 3:
            return False
        if entry_like_count < max(3, page_number_only_count):
            return False
        if heading_like_count + page_number_signal_count < max(4, len(body_blocks) // 2):
            return False
        if prose_like_count > max(1, len(body_blocks) // 3):
            return False
        return True

    def _resolve_directory_page(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        page_height: float,
    ) -> list[PdfSemanticBlock]:
        resolved: list[PdfSemanticBlock] = []
        pending_entry_indices: list[int] = []
        body_start_index = 0

        if blocks and self._has_page_title(block=blocks[0], page_height=page_height, page_blocks=blocks):
            resolved.append(blocks[0])
            body_start_index = 1

        for block in blocks[body_start_index:]:
            tokens = self._page_number_tokens(block.text)
            if tokens:
                if self._assign_page_number_tokens(
                    resolved=resolved,
                    pending_entry_indices=pending_entry_indices,
                    tokens=tokens,
                    source_block=block,
                ):
                    pending_entry_indices.clear()
                    continue
                resolved.append(block)
                pending_entry_indices.clear()
                continue

            is_entry_candidate = self._is_directory_entry_candidate(block)
            normalized = self._normalize_entry_block(block)
            resolved.append(normalized)
            if is_entry_candidate:
                pending_entry_indices.append(len(resolved) - 1)
            else:
                pending_entry_indices.clear()

        return resolved

    def _has_page_title(
        self,
        *,
        block: PdfSemanticBlock,
        page_height: float,
        page_blocks: list[PdfSemanticBlock],
    ) -> bool:
        text = self._normalized_text(block.text)
        if not text:
            return False
        if page_height > 0.0 and float(block.bbox.top) > page_height * 0.25:
            return False
        if len(text.split()) > 6:
            return False
        max_font = max(float(candidate.avg_font_size or 0.0) for candidate in page_blocks)
        if max_font <= 0.0:
            return False
        return float(block.avg_font_size or 0.0) >= max_font * 0.9

    def _is_directory_entry_candidate(self, block: PdfSemanticBlock) -> bool:
        text = self._normalized_text(block.text)
        if not text:
            return False
        if self._is_page_number_only_block(block):
            return False
        if self._looks_prose_block(block):
            return False
        if self._has_entry_page_number(block):
            return True
        tokens = [token for token in text.split(" ") if token]
        if str(block.block_type or "") in {"heading", "list_item"} and len(tokens) <= 18:
            return True
        return False

    def _looks_prose_block(self, block: PdfSemanticBlock) -> bool:
        text = self._normalized_text(block.text)
        if not text:
            return False
        words = [token for token in text.split(" ") if token]
        if len(words) > 24:
            return True
        if text.count(". ") >= 1 and not _DOT_LEADER_ENTRY_RE.search(text):
            return True
        if text.endswith((".", "!", "?")) and not _DOT_LEADER_ENTRY_RE.search(text):
            return True
        return False

    def _is_page_number_only_block(self, block: PdfSemanticBlock) -> bool:
        tokens = self._page_number_tokens(block.text)
        return bool(tokens)

    def _has_entry_page_number(self, block: PdfSemanticBlock) -> bool:
        text = self._normalized_text(block.text)
        if not text:
            return False
        if _DOT_LEADER_ENTRY_RE.search(text):
            return True
        tokens = [token for token in text.split(" ") if token]
        return bool(tokens and _PAGE_NUMBER_TOKEN_RE.match(tokens[-1]))

    def _page_number_signal_count(self, block: PdfSemanticBlock) -> int:
        if self._is_page_number_only_block(block):
            return len(self._page_number_tokens(block.text))
        return 1 if self._has_entry_page_number(block) else 0

    @staticmethod
    def _normalize_entry_block(block: PdfSemanticBlock) -> PdfSemanticBlock:
        if str(block.block_type or "") == "heading":
            return replace(block, block_type="paragraph", heading_level=None)
        if str(block.block_type or "") == "list_item":
            return replace(block, block_type="paragraph")
        return block

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

    @staticmethod
    def _normalized_text(text: str) -> str:
        return _SPACE_RE.sub(" ", str(text or "").strip())
