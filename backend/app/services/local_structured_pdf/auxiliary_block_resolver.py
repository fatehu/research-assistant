from __future__ import annotations

import re
from dataclasses import replace

from .contracts import PdfBBox, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage


_CAPTION_PREFIX_RE = re.compile(r"^(?:fig(?:ure)?|table|chart|image|photo|plate)\b", re.IGNORECASE)
_FOOTNOTE_PREFIX_RE = re.compile(r"^\s*(?:\d{1,2}|[*†‡])[\]\).]?\s+\S", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


class LocalPdfAuxiliaryBlockResolver:
    """Stage-4 resolver: classify and merge caption / footnote-like paragraph blocks."""

    def __init__(
        self,
        *,
        caption_max_gap: float = 18.0,
        footnote_max_gap: float = 18.0,
        footnote_font_ratio: float = 0.93,
    ) -> None:
        self._caption_max_gap = max(8.0, float(caption_max_gap))
        self._footnote_max_gap = max(8.0, float(footnote_max_gap))
        self._footnote_font_ratio = min(1.0, max(0.7, float(footnote_font_ratio)))

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        blocks = list(document.blocks or [])
        if not blocks:
            return document

        resolved_blocks: list[PdfSemanticBlock] = []
        index = 0
        while index < len(blocks):
            block = blocks[index]
            if str(block.block_type or "") != "paragraph":
                resolved_blocks.append(block)
                index += 1
                continue

            if self._is_caption_candidate(block=block, body_font_size=float(document.body_font_size or 0.0)):
                merged_block, next_index = self._consume_caption_sequence(blocks=blocks, start=index)
                resolved_blocks.append(merged_block)
                index = next_index
                continue

            if self._is_footnote_candidate(block=block, body_font_size=float(document.body_font_size or 0.0)):
                merged_block, next_index = self._consume_footnote_sequence(
                    blocks=blocks,
                    start=index,
                    body_font_size=float(document.body_font_size or 0.0),
                )
                resolved_blocks.append(merged_block)
                index = next_index
                continue

            resolved_blocks.append(block)
            index += 1

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

    def _consume_caption_sequence(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        start: int,
    ) -> tuple[PdfSemanticBlock, int]:
        merged_blocks = [blocks[start]]
        index = start + 1
        while index < len(blocks):
            current = merged_blocks[-1]
            candidate = blocks[index]
            if not self._should_merge_caption_followup(anchor=current, candidate=candidate):
                break
            merged_blocks.append(candidate)
            index += 1
        return self._merge_blocks(blocks=merged_blocks, block_type="caption"), index

    def _consume_footnote_sequence(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        start: int,
        body_font_size: float,
    ) -> tuple[PdfSemanticBlock, int]:
        merged_blocks = [blocks[start]]
        index = start + 1
        while index < len(blocks):
            current = merged_blocks[-1]
            candidate = blocks[index]
            if not self._should_merge_footnote_followup(
                anchor=current,
                candidate=candidate,
                body_font_size=body_font_size,
            ):
                break
            merged_blocks.append(candidate)
            index += 1
        return self._merge_blocks(blocks=merged_blocks, block_type="footnote"), index

    def _is_caption_candidate(self, *, block: PdfSemanticBlock, body_font_size: float) -> bool:
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not text or not _CAPTION_PREFIX_RE.match(text):
            return False
        if len(text.split()) > 24:
            return False
        if body_font_size > 0.0 and float(block.avg_font_size or 0.0) > body_font_size * 1.1:
            return False
        return True

    def _should_merge_caption_followup(self, *, anchor: PdfSemanticBlock, candidate: PdfSemanticBlock) -> bool:
        if str(candidate.block_type or "") != "paragraph":
            return False
        if int(anchor.page_start) != int(candidate.page_start):
            return False
        if str(anchor.column_id or "main") != str(candidate.column_id or "main"):
            return False
        if str(anchor.region or "main") != str(candidate.region or "main"):
            return False
        if self._vertical_gap(anchor=anchor, candidate=candidate) > self._caption_max_gap:
            return False
        if abs(float(anchor.avg_font_size or 0.0) - float(candidate.avg_font_size or 0.0)) > 1.2:
            return False
        candidate_text = _SPACE_RE.sub(" ", str(candidate.text or "").strip())
        if not candidate_text or _CAPTION_PREFIX_RE.match(candidate_text) or _FOOTNOTE_PREFIX_RE.match(candidate_text):
            return False
        if len(candidate_text.split()) > 16:
            return False
        return candidate_text[:1].islower() or candidate_text.endswith(".")

    def _is_footnote_candidate(self, *, block: PdfSemanticBlock, body_font_size: float) -> bool:
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not text or not _FOOTNOTE_PREFIX_RE.match(text):
            return False
        if body_font_size > 0.0 and float(block.avg_font_size or 0.0) > body_font_size * self._footnote_font_ratio:
            return False
        return True

    def _should_merge_footnote_followup(
        self,
        *,
        anchor: PdfSemanticBlock,
        candidate: PdfSemanticBlock,
        body_font_size: float,
    ) -> bool:
        if str(candidate.block_type or "") != "paragraph":
            return False
        if int(anchor.page_start) != int(candidate.page_start):
            return False
        if str(anchor.column_id or "main") != str(candidate.column_id or "main"):
            return False
        if str(anchor.region or "main") != str(candidate.region or "main"):
            return False
        if self._vertical_gap(anchor=anchor, candidate=candidate) > self._footnote_max_gap:
            return False
        if body_font_size > 0.0 and float(candidate.avg_font_size or 0.0) > body_font_size * self._footnote_font_ratio:
            return False
        candidate_text = _SPACE_RE.sub(" ", str(candidate.text or "").strip())
        if not candidate_text or _CAPTION_PREFIX_RE.match(candidate_text):
            return False
        if candidate_text[:1].isdigit() and _FOOTNOTE_PREFIX_RE.match(candidate_text):
            return False
        return True

    @staticmethod
    def _vertical_gap(*, anchor: PdfSemanticBlock, candidate: PdfSemanticBlock) -> float:
        return float(candidate.bbox.top) - float(anchor.bbox.bottom)

    def _merge_blocks(self, *, blocks: list[PdfSemanticBlock], block_type: str) -> PdfSemanticBlock:
        first = blocks[0]
        last = blocks[-1]
        text = "\n".join(str(block.text or "").strip() for block in blocks if str(block.text or "").strip()).strip()
        avg_font_size = round(
            sum(float(block.avg_font_size or 0.0) for block in blocks) / max(1, len(blocks)),
            2,
        )
        return replace(
            first,
            block_type=block_type,
            page_start=int(first.page_start),
            page_end=int(last.page_end),
            text=text,
            bbox=self._merge_bboxes([block.bbox for block in blocks]),
            line_ids=[line_id for block in blocks for line_id in list(block.line_ids or [])],
            avg_font_size=avg_font_size,
            reading_order_start=int(first.reading_order_start or 0),
            reading_order_end=int(last.reading_order_end or 0),
        )

    @staticmethod
    def _merge_bboxes(boxes: list[PdfBBox]) -> PdfBBox:
        return PdfBBox(
            x0=round(min(float(box.x0) for box in boxes), 2),
            top=round(min(float(box.top) for box in boxes), 2),
            x1=round(max(float(box.x1) for box in boxes), 2),
            bottom=round(max(float(box.bottom) for box in boxes), 2),
        )
