from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Sequence

from .contracts import (
    PdfBBox,
    PdfHybridParsedBlock,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


class LocalStructuredPdfHybridFusionService:
    """Fuse backend-parsed pages back into the local structured document."""

    def fuse_document(
        self,
        *,
        resolved_document: PdfResolvedDocument,
        local_document: PdfStructuredDocument,
        triage_document: PdfHybridTriageDocument,
        parsed_pages: Sequence[PdfHybridParsedPage],
    ) -> PdfStructuredDocument:
        resolved_by_page = {
            int(page.page): page
            for page in list(getattr(resolved_document, "pages", []) or [])
        }
        local_pages_by_page = {
            int(page.page): page
            for page in list(getattr(local_document, "pages", []) or [])
        }
        triage_by_page = {
            int(item.page): item
            for item in list(getattr(triage_document, "pages", []) or [])
        }
        parsed_by_page = {
            int(item.page): item
            for item in list(parsed_pages or [])
            if bool(item.used)
        }
        ordered_pages = sorted(
            {
                *resolved_by_page.keys(),
                *local_pages_by_page.keys(),
                *triage_by_page.keys(),
                *parsed_by_page.keys(),
            }
        )

        fused_pages: list[PdfStructuredPage] = []
        fused_blocks: list[PdfSemanticBlock] = []
        for page_number in ordered_pages:
            local_page = local_pages_by_page.get(page_number)
            resolved_page = resolved_by_page.get(page_number)
            local_blocks = list(getattr(local_page, "blocks", []) or [])
            triage_page = triage_by_page.get(page_number)
            parsed_page = parsed_by_page.get(page_number)
            if (
                parsed_page is not None
                and triage_page is not None
                and str(triage_page.decision or "") == "backend"
            ):
                page_blocks = self._build_backend_page_blocks(
                    parsed_page=parsed_page,
                    triage_page=triage_page,
                    resolved_page=resolved_page,
                    local_blocks=local_blocks,
                    body_font_size=float(getattr(local_document, "body_font_size", 0.0) or 0.0),
                )
            else:
                page_blocks = local_blocks
            meta = self._resolve_page_meta(local_page=local_page, resolved_page=resolved_page, page_number=page_number)
            page_blocks = sorted(
                list(page_blocks or []),
                key=lambda item: (
                    int(item.reading_order_start or 0),
                    int(item.reading_order_end or 0),
                    str(item.block_id),
                ),
            )
            fused_pages.append(PdfStructuredPage(meta=meta, blocks=page_blocks))
            fused_blocks.extend(page_blocks)

        fused_blocks = sorted(
            fused_blocks,
            key=lambda item: (
                int(item.page_start or 0),
                int(item.reading_order_start or 0),
                int(item.reading_order_end or 0),
                str(item.block_id),
            ),
        )
        return PdfStructuredDocument(
            pages=fused_pages,
            blocks=fused_blocks,
            body_font_size=float(getattr(local_document, "body_font_size", 0.0) or 0.0),
        )

    def _build_backend_page_blocks(
        self,
        *,
        parsed_page: PdfHybridParsedPage,
        triage_page,
        resolved_page,
        local_blocks: Sequence[PdfSemanticBlock],
        body_font_size: float,
    ) -> list[PdfSemanticBlock]:
        line_map = {
            str(line.line_id): line
            for line in list(getattr(resolved_page, "lines", []) or [])
            if str(line.line_id or "").strip()
        } if resolved_page is not None else {}
        blocks: list[PdfSemanticBlock] = []
        heading_count = 0
        page_type = str(getattr(triage_page, "page_type", "") or "").strip().lower()
        page_height = float(getattr(getattr(resolved_page, "meta", None), "page_height", 0.0) or 0.0)
        resolved_page_number = int(getattr(resolved_page, "page", 0) or 0)
        for parsed_block in self._dedupe_backend_blocks(parsed_page.blocks or []):
            source_lines = [
                line_map[line_id]
                for line_id in list(parsed_block.source_line_ids or [])
                if line_id in line_map
            ]
            if not source_lines and not str(parsed_block.text or "").strip():
                continue
            matched_local = self._match_local_block(parsed_block=parsed_block, local_blocks=local_blocks)
            avg_font_size = (
                round(mean(float(getattr(line, "avg_font_size", 0.0) or 0.0) for line in source_lines), 2)
                if source_lines
                else float(getattr(matched_local, "avg_font_size", 0.0) or 0.0)
            )
            source_line_orders = [
                int(getattr(line, "reading_order", 0) or 0)
                for line in source_lines
            ]
            backend_reading_order = max(1, int(getattr(parsed_block, "reading_order", 0) or 0))
            block_type = self._normalized_block_type(str(parsed_block.kind or "unknown"))
            block_type = self._normalize_visual_block_type(
                block_type=block_type,
                parsed_block=parsed_block,
                heading_count=heading_count,
                page_type=page_type,
                page_height=page_height,
            )
            if block_type == "heading":
                heading_count += 1
            heading_level = self._resolve_heading_level(
                block_type=block_type,
                parsed_block=parsed_block,
                matched_local=matched_local,
                avg_font_size=avg_font_size,
                body_font_size=body_font_size,
            )
            blocks.append(
                PdfSemanticBlock(
                    block_id=str(parsed_block.block_id or ""),
                    block_type=block_type,
                    page_start=int(parsed_block.page or resolved_page_number or 0),
                    page_end=int(parsed_block.page or resolved_page_number or 0),
                    text=str(parsed_block.text or "").strip(),
                    bbox=parsed_block.bbox,
                    line_ids=list(parsed_block.source_line_ids or []),
                    column_id=self._resolve_column_id(lines=source_lines, parsed_block=parsed_block),
                    region=self._resolve_region(lines=source_lines, parsed_block=parsed_block),
                    avg_font_size=avg_font_size,
                    # Preserve backend block order when available. Upstream Java keeps
                    # backend page results as backend-authored content and only applies
                    # cross-page post-processing later.
                    reading_order_start=backend_reading_order if backend_reading_order > 0 else min(source_line_orders or [0]),
                    reading_order_end=backend_reading_order if backend_reading_order > 0 else max(source_line_orders or [0]),
                    heading_level=heading_level,
                    table_rows=self._resolve_table_rows(
                        parsed_block=parsed_block,
                        block_type=block_type,
                        matched_local=None,
                    ),
                )
            )
        return blocks

    @staticmethod
    def _dedupe_backend_blocks(blocks: Sequence[PdfHybridParsedBlock]) -> list[PdfHybridParsedBlock]:
        seen: set[tuple[str, str, float, float, float, float]] = set()
        deduped: list[PdfHybridParsedBlock] = []
        for block in list(blocks or []):
            key = (
                str(block.kind or "").strip().lower(),
                " ".join(str(block.text or "").split()).strip().lower(),
                round(float(block.bbox.x0), 1),
                round(float(block.bbox.top), 1),
                round(float(block.bbox.x1), 1),
                round(float(block.bbox.bottom), 1),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(block)
        return deduped

    @staticmethod
    def _resolve_page_meta(
        *,
        local_page,
        resolved_page,
        page_number: int,
    ) -> PdfPageMeta:
        if local_page is not None:
            return local_page.meta
        if resolved_page is not None:
            return resolved_page.meta
        return PdfPageMeta(page=page_number, page_width=0.0, page_height=0.0, rotation=0)

    def _match_local_block(
        self,
        *,
        parsed_block: PdfHybridParsedBlock,
        local_blocks: Sequence[PdfSemanticBlock],
    ) -> PdfSemanticBlock | None:
        desired_type = self._normalized_block_type(str(parsed_block.kind or "unknown"))
        parsed_line_ids = {str(item) for item in list(parsed_block.source_line_ids or []) if str(item).strip()}
        best_block: PdfSemanticBlock | None = None
        best_score = 0.0
        for block in list(local_blocks or []):
            score = 0.0
            local_line_ids = {str(item) for item in list(block.line_ids or []) if str(item).strip()}
            overlap = len(parsed_line_ids.intersection(local_line_ids))
            if overlap:
                score += overlap * 4.0
            if str(block.block_type or "") == desired_type:
                score += 2.0
            score += self._bbox_iou(parsed_block.bbox, block.bbox)
            if score > best_score:
                best_score = score
                best_block = block
        return best_block if best_score > 0.0 else None

    @staticmethod
    def _normalized_block_type(kind: str) -> str:
        token = str(kind or "unknown").strip().lower()
        if token == "figure_meta":
            return "caption"
        return token or "unknown"

    @staticmethod
    def _normalize_visual_block_type(
        *,
        block_type: str,
        parsed_block: PdfHybridParsedBlock,
        heading_count: int,
        page_type: str,
        page_height: float,
    ) -> str:
        if page_type != "visual_or_scanned" or block_type != "heading":
            return block_type
        text = str(parsed_block.text or "").strip()
        if not text:
            return "paragraph"
        word_count = len([token for token in text.split() if token])
        top_ratio = (float(parsed_block.bbox.top) / page_height) if page_height > 0.0 else 1.0
        has_sentence_punct = any(ch in text for ch in ".!?")
        if heading_count < 2 and top_ratio <= 0.12:
            return "heading"
        if top_ratio <= 0.08 and word_count <= 8 and not has_sentence_punct:
            return "heading"
        if top_ratio <= 0.16 and word_count <= 3 and not has_sentence_punct:
            return "heading"
        return "paragraph"

    @staticmethod
    def _resolve_table_rows(
        *,
        parsed_block: PdfHybridParsedBlock,
        block_type: str,
        matched_local: PdfSemanticBlock | None,
    ) -> list[list[str]]:
        parsed_rows = [list(row) for row in list(parsed_block.table_rows or [])]
        if block_type == "table" and parsed_rows:
            return parsed_rows
        return []

    @staticmethod
    def _resolve_heading_level(
        *,
        block_type: str,
        parsed_block: PdfHybridParsedBlock,
        matched_local: PdfSemanticBlock | None,
        avg_font_size: float,
        body_font_size: float,
    ) -> int | None:
        if block_type != "heading":
            return None
        if parsed_block.heading_level is not None:
            return max(1, int(parsed_block.heading_level))
        if matched_local is not None and matched_local.heading_level is not None:
            return int(matched_local.heading_level)
        delta = float(avg_font_size or 0.0) - float(body_font_size or 0.0)
        if delta >= 6.0:
            return 1
        if delta >= 4.0:
            return 2
        if delta >= 2.0:
            return 3
        return 4

    def _resolve_column_id(
        self,
        *,
        lines: Sequence[PdfResolvedLine],
        parsed_block: PdfHybridParsedBlock,
    ) -> str:
        dominant = self._dominant_attr(lines=lines, attr="column_id")
        if dominant:
            return dominant
        if str(parsed_block.zone or "") == "side":
            return "side"
        return "main"

    def _resolve_region(
        self,
        *,
        lines: Sequence[PdfResolvedLine],
        parsed_block: PdfHybridParsedBlock,
    ) -> str:
        dominant = self._dominant_attr(lines=lines, attr="region")
        zone = str(parsed_block.zone or "").strip().lower()
        if dominant:
            return dominant
        if zone in {"figure", "table", "footer", "header", "side"}:
            return zone
        return "main"

    @staticmethod
    def _dominant_attr(*, lines: Sequence[PdfResolvedLine], attr: str) -> str:
        counter: Counter[str] = Counter(
            str(getattr(line, attr, "") or "").strip()
            for line in list(lines or [])
            if str(getattr(line, attr, "") or "").strip()
        )
        if not counter:
            return ""
        return counter.most_common(1)[0][0]

    @staticmethod
    def _bbox_iou(first: PdfBBox, second: PdfBBox) -> float:
        inter_x0 = max(float(first.x0), float(second.x0))
        inter_top = max(float(first.top), float(second.top))
        inter_x1 = min(float(first.x1), float(second.x1))
        inter_bottom = min(float(first.bottom), float(second.bottom))
        inter_width = max(0.0, inter_x1 - inter_x0)
        inter_height = max(0.0, inter_bottom - inter_top)
        intersection = inter_width * inter_height
        if intersection <= 0.0:
            return 0.0
        first_area = max(0.0, (float(first.x1) - float(first.x0)) * (float(first.bottom) - float(first.top)))
        second_area = max(0.0, (float(second.x1) - float(second.x0)) * (float(second.bottom) - float(second.top)))
        union = max(0.0, first_area + second_area - intersection)
        if union <= 0.0:
            return 0.0
        return intersection / union
