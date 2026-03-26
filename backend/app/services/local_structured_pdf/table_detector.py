from __future__ import annotations

from collections import Counter
from dataclasses import replace
import re
from statistics import median
from typing import Sequence

from .contracts import (
    PdfBBox,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
    PdfTableAtom,
    PdfWordAtom,
)

_CAPTION_RE = re.compile(r"^\s*(?:table|tab\.?|figure|fig\.)\s*\d+[\s:\.-]", re.IGNORECASE)
_COMMON_PROSE_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "this",
    "to",
    "we",
    "with",
}


class LocalPdfTableDetector:
    """Stage-3.5 detector: recover simple text tables from line/word geometry."""

    def __init__(
        self,
        *,
        min_rows: int = 2,
        min_cols: int = 2,
        min_gap: float = 18.0,
        alignment_tolerance: float = 18.0,
    ) -> None:
        self._min_rows = max(2, int(min_rows))
        self._min_cols = max(2, int(min_cols))
        self._min_gap = max(8.0, float(min_gap))
        self._alignment_tolerance = max(6.0, float(alignment_tolerance))

    def detect_document(
        self,
        *,
        page_atoms: Sequence[PdfPageAtoms] | None = None,
        normalized_pages: Sequence[PdfNormalizedPage],
        resolved_document: PdfResolvedDocument,
        structured_document: PdfStructuredDocument,
    ) -> PdfStructuredDocument:
        word_map = {
            str(word.word_id): word
            for page in list(normalized_pages or [])
            for word in list(page.kept_words or [])
        }
        line_map = {
            str(line.line_id): line
            for page in list(resolved_document.pages or [])
            for line in list(page.lines or [])
        }
        page_table_map = {
            int(page.meta.page): list(page.tables or [])
            for page in list(page_atoms or [])
        }
        page_atom_map = {
            int(page.meta.page): page
            for page in list(page_atoms or [])
        }

        source_page_blocks: dict[int, list[PdfSemanticBlock]] = {}
        for block in list(structured_document.blocks or []):
            source_page_blocks.setdefault(int(block.page_start), []).append(block)

        updated_blocks: list[PdfSemanticBlock] = []
        for page in list(structured_document.pages or []):
            raw_page_blocks = list(page.blocks or []) or source_page_blocks.get(int(page.page), [])
            page_blocks = sorted(
                raw_page_blocks,
                key=lambda block: (
                    int(block.reading_order_start or 0),
                    int(block.reading_order_end or 0),
                    str(block.block_id or ""),
                ),
            )
            updated_blocks.extend(
                self._detect_page_blocks(
                    page_blocks=page_blocks,
                    line_map=line_map,
                    word_map=word_map,
                    page_table_map=page_table_map,
                    page_atom_map=page_atom_map,
                )
            )

        page_map: dict[int, list[PdfSemanticBlock]] = {}
        for block in updated_blocks:
            for page_number in range(int(block.page_start), int(block.page_end) + 1):
                page_map.setdefault(page_number, []).append(block)

        return PdfStructuredDocument(
            pages=[
                PdfStructuredPage(
                    meta=page.meta,
                    blocks=page_map.get(int(page.page), []),
                )
                for page in list(structured_document.pages or [])
            ],
            blocks=updated_blocks,
            body_font_size=float(structured_document.body_font_size or 0.0),
        )

    def _detect_page_blocks(
        self,
        *,
        page_blocks: Sequence[PdfSemanticBlock],
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
        page_table_map: dict[int, list[PdfTableAtom]],
        page_atom_map: dict[int, PdfPageAtoms],
    ) -> list[PdfSemanticBlock]:
        preprocessed_blocks: list[PdfSemanticBlock] = []
        for block in list(page_blocks or []):
            if str(block.block_type or "") != "paragraph":
                preprocessed_blocks.append(block)
                continue
            preprocessed_blocks.extend(
                self._split_paragraph_block(
                    block=block,
                    line_map=line_map,
                    word_map=word_map,
                    page_table_map=page_table_map,
                )
            )

        updated: list[PdfSemanticBlock] = []
        blocks = self._merge_page_pymupdf_tables(
            blocks=list(preprocessed_blocks or []),
            page_table_map=page_table_map,
            page_atom_map=page_atom_map,
        )
        index = 0
        while index < len(blocks):
            pymupdf_merged = self._try_merge_pymupdf_table_sequence(
                blocks=blocks,
                start_index=index,
                page_table_map=page_table_map,
            )
            if pymupdf_merged is not None:
                merged_block, next_index = pymupdf_merged
                updated.append(merged_block)
                index = next_index
                continue

            merged = self._try_merge_table_sequence(
                blocks=blocks,
                start_index=index,
                line_map=line_map,
                word_map=word_map,
                page_table_map=page_table_map,
            )
            if merged is not None:
                merged_block, next_index = merged
                updated.append(merged_block)
                index = next_index
                continue

            block = blocks[index]
            updated.append(block)
            index += 1
        return updated

    def _merge_page_pymupdf_tables(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        page_table_map: dict[int, list[PdfTableAtom]],
        page_atom_map: dict[int, PdfPageAtoms],
    ) -> list[PdfSemanticBlock]:
        items = list(blocks or [])
        if len(items) < 2:
            return items

        page_number = int(items[0].page_start)
        page_atoms = page_atom_map.get(page_number)
        candidate_tables = sorted(
            list(page_table_map.get(page_number, []) or []),
            key=lambda table: (
                -float(table.bbox.width * table.bbox.height),
                -(int(table.row_count or 0) * int(table.col_count or 0)),
            ),
        )
        if not candidate_tables:
            return items

        replacements: list[tuple[int, int, PdfSemanticBlock]] = []
        consumed_indexes: set[int] = set()
        for table in candidate_tables:
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if self._rows_look_like_sparse_chart(rows):
                continue
            if not self._rows_are_usable(rows):
                rows = self._extract_rect_based_table_rows(
                    page_atoms=page_atoms,
                    table=table,
                )
            if not self._rows_are_usable(rows):
                continue

            overlapping_indexes = [
                index
                for index, block in enumerate(items)
                if index not in consumed_indexes
                and self._block_is_page_table_member(block=block, table=table)
            ]
            if len(overlapping_indexes) < 2:
                continue

            spans = self._group_contiguous_indexes(overlapping_indexes)
            best_span: tuple[int, int, float] | None = None
            for start_index, end_index in spans:
                subset = items[start_index : end_index + 1]
                merged_bbox = self._merge_bboxes([block.bbox for block in subset])
                table_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=merged_bbox)
                bbox_overlap = self._bbox_overlap_ratio(merged_bbox, table.bbox)
                token_overlap = self._token_overlap_ratio(
                    "\n".join(str(block.text or "").strip() for block in subset if str(block.text or "").strip()),
                    rows,
                )
                if table_coverage < 0.72 or bbox_overlap < 0.72 or token_overlap < 0.42:
                    continue
                score = (table_coverage * 0.4) + (bbox_overlap * 0.25) + (token_overlap * 0.35)
                if best_span is None or score > best_span[2]:
                    best_span = (start_index, end_index, score)

            if best_span is None:
                continue

            start_index, end_index, _score = best_span
            merged_block = self._merge_blocks_as_table(
                blocks=items[start_index : end_index + 1],
                table_rows=rows,
            )
            replacements.append((start_index, end_index, merged_block))
            consumed_indexes.update(range(start_index, end_index + 1))

        if not replacements:
            fragmented = self._materialize_fragmented_pymupdf_table(
                blocks=items,
                candidate_tables=candidate_tables,
            )
            if fragmented is not None:
                return fragmented
            page_dominant = self._materialize_page_dominant_pymupdf_table(
                blocks=items,
                candidate_tables=candidate_tables,
                page_atoms=page_atoms,
            )
            if page_dominant is not None:
                return page_dominant
            return items

        replacement_map = {start: (end, block) for start, end, block in replacements}
        updated: list[PdfSemanticBlock] = []
        index = 0
        while index < len(items):
            replacement = replacement_map.get(index)
            if replacement is not None:
                end_index, block = replacement
                updated.append(block)
                index = end_index + 1
                continue
            if index in consumed_indexes:
                index += 1
                continue
            updated.append(items[index])
            index += 1
        return updated

    def _materialize_fragmented_pymupdf_table(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        candidate_tables: Sequence[PdfTableAtom],
    ) -> list[PdfSemanticBlock] | None:
        items = list(blocks or [])
        if len(items) < 3:
            return None

        best_result: list[PdfSemanticBlock] | None = None
        best_score = 0.0
        for table in list(candidate_tables or []):
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if self._rows_look_like_sparse_chart(rows) or not self._rows_are_usable(rows):
                continue

            overlapping_indexes = [
                index
                for index, block in enumerate(items)
                if self._block_is_page_table_member(block=block, table=table)
            ]
            if len(overlapping_indexes) < 3:
                continue

            overlapping_blocks = [items[index] for index in overlapping_indexes]
            distinct_columns = {
                str(block.column_id or "main")
                for block in overlapping_blocks
                if str(block.text or "").strip()
            }
            if len(distinct_columns) < 2 and len(overlapping_blocks) < 4:
                continue

            merged_bbox = self._merge_bboxes([block.bbox for block in overlapping_blocks])
            bbox_overlap = self._bbox_overlap_ratio(merged_bbox, table.bbox)
            table_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=merged_bbox)
            token_overlap = self._token_overlap_ratio(
                "\n".join(str(block.text or "").strip() for block in overlapping_blocks if str(block.text or "").strip()),
                rows,
            )
            if bbox_overlap < 0.48 or table_coverage < 0.48 or token_overlap < 0.26:
                continue

            first_index = min(overlapping_indexes)
            overlap_set = set(overlapping_indexes)
            merged_block = self._merge_blocks_as_table(
                blocks=overlapping_blocks,
                table_rows=rows,
            )

            updated: list[PdfSemanticBlock] = []
            inserted = False
            for index, block in enumerate(items):
                if index in overlap_set:
                    if not inserted and index == first_index:
                        updated.append(merged_block)
                        inserted = True
                    continue
                updated.append(block)

            score = (bbox_overlap * 0.35) + (table_coverage * 0.25) + (token_overlap * 0.40)
            if score > best_score:
                best_score = score
                best_result = updated

        return best_result

    def _materialize_page_dominant_pymupdf_table(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        candidate_tables: Sequence[PdfTableAtom],
        page_atoms: PdfPageAtoms | None,
    ) -> list[PdfSemanticBlock] | None:
        items = list(blocks or [])
        if len(items) < 2 or page_atoms is None:
            return None

        page_width = float(page_atoms.meta.page_width or 0.0)
        page_height = float(page_atoms.meta.page_height or 0.0)
        if page_width <= 0.0 or page_height <= 0.0:
            return None

        best_result: list[PdfSemanticBlock] | None = None
        best_score = 0.0
        for table in list(candidate_tables or []):
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if not self._rows_are_usable(rows):
                rows = self._extract_rect_based_table_rows(
                    page_atoms=page_atoms,
                    table=table,
                )
            if not self._rows_are_usable(rows):
                continue

            table_width_ratio = float(table.bbox.width) / max(1.0, page_width)
            table_height_ratio = float(table.bbox.height) / max(1.0, page_height)
            if table_width_ratio < 0.72 or table_height_ratio < 0.16:
                continue

            overlapping_indexes = [
                index
                for index, block in enumerate(items)
                if self._block_is_page_table_member(block=block, table=table)
            ]
            if len(overlapping_indexes) < 2:
                continue

            overlapping_blocks = [items[index] for index in overlapping_indexes]
            merged_bbox = self._merge_bboxes([block.bbox for block in overlapping_blocks])
            bbox_overlap = self._bbox_overlap_ratio(merged_bbox, table.bbox)
            table_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=merged_bbox)
            if bbox_overlap < 0.55 or table_coverage < 0.55:
                continue

            first_index = min(overlapping_indexes)
            overlap_set = set(overlapping_indexes)
            merged_block = self._merge_blocks_as_table(
                blocks=overlapping_blocks,
                table_rows=rows,
            )

            updated: list[PdfSemanticBlock] = []
            inserted = False
            for index, block in enumerate(items):
                if index in overlap_set:
                    if not inserted and index == first_index:
                        updated.append(merged_block)
                        inserted = True
                    continue
                updated.append(block)

            score = (table_width_ratio * 0.25) + (table_height_ratio * 0.15) + (bbox_overlap * 0.3) + (table_coverage * 0.3)
            if score > best_score:
                best_score = score
                best_result = updated

        return best_result

    def _extract_rect_based_table_rows(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        table: PdfTableAtom,
    ) -> list[list[str]]:
        if page_atoms is None:
            return []
        target_cols = max(self._min_cols, int(table.col_count or 0))
        if target_cols <= 0:
            return []

        table_area = max(1.0, float(table.bbox.width) * float(table.bbox.height))
        candidate_boxes: list[PdfBBox] = []
        for rect in list(page_atoms.rects or []):
            bbox = rect.bbox
            rect_area = max(0.0, float(bbox.width) * float(bbox.height))
            if rect_area <= 0.0 or rect_area > table_area * 0.35:
                continue
            if float(bbox.x0) < float(table.bbox.x0) - 5.0 or float(bbox.x1) > float(table.bbox.x1) + 5.0:
                continue
            if float(bbox.top) < float(table.bbox.top) - 5.0 or float(bbox.bottom) > float(table.bbox.bottom) + 5.0:
                continue
            if float(bbox.width) < 20.0 or float(bbox.height) < 6.0:
                continue
            if float(bbox.width) > float(table.bbox.width) * 0.75 and float(bbox.height) < max(24.0, float(table.bbox.height) * 0.12):
                continue
            candidate_boxes.append(bbox)

        if not candidate_boxes:
            return []

        row_groups: list[list[PdfBBox]] = []
        for bbox in sorted(candidate_boxes, key=lambda item: (float(item.top), float(item.x0))):
            placed = False
            for row in row_groups:
                row_top = min(float(item.top) for item in row)
                if abs(row_top - float(bbox.top)) <= 6.0:
                    row.append(bbox)
                    placed = True
                    break
            if not placed:
                row_groups.append([bbox])

        rows: list[list[str]] = []
        for row in row_groups:
            ordered_boxes = sorted(row, key=lambda item: float(item.x0))
            cells = [
                self._extract_words_in_bbox(words=page_atoms.words, bbox=bbox)
                for bbox in ordered_boxes
            ]
            normalized = self._fit_row_to_target_cols(cells=cells, target_cols=target_cols)
            if self._row_non_empty_cells(normalized) == 0:
                continue
            rows.append(normalized)
        return rows

    def _block_is_page_table_member(
        self,
        *,
        block: PdfSemanticBlock,
        table: PdfTableAtom,
    ) -> bool:
        if int(block.page_start) != int(block.page_end):
            return False
        vertical_overlap = self._interval_overlap(
            float(block.bbox.top),
            float(block.bbox.bottom),
            float(table.bbox.top),
            float(table.bbox.bottom),
        )
        horizontal_overlap = self._interval_overlap(
            float(block.bbox.x0),
            float(block.bbox.x1),
            float(table.bbox.x0),
            float(table.bbox.x1),
        )
        if vertical_overlap <= 0.0:
            return False
        if horizontal_overlap <= 0.0:
            return False

        block_vertical_ratio = vertical_overlap / max(1.0, float(block.bbox.height))
        block_horizontal_ratio = horizontal_overlap / max(1.0, float(block.bbox.width))
        center_x = (float(block.bbox.x0) + float(block.bbox.x1)) / 2.0
        center_y = (float(block.bbox.top) + float(block.bbox.bottom)) / 2.0
        center_inside = (
            float(table.bbox.x0) <= center_x <= float(table.bbox.x1)
            and float(table.bbox.top) <= center_y <= float(table.bbox.bottom)
        )
        return center_inside or (
            block_vertical_ratio >= 0.55
            and block_horizontal_ratio >= 0.18
        )

    @staticmethod
    def _interval_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
        return max(0.0, min(float(end_a), float(end_b)) - max(float(start_a), float(start_b)))

    @staticmethod
    def _group_contiguous_indexes(indexes: Sequence[int]) -> list[tuple[int, int]]:
        values = sorted(set(int(index) for index in list(indexes or [])))
        if not values:
            return []
        spans: list[tuple[int, int]] = []
        start = values[0]
        end = values[0]
        for value in values[1:]:
            if value == end + 1:
                end = value
                continue
            spans.append((start, end))
            start = value
            end = value
        spans.append((start, end))
        return spans

    def _try_merge_pymupdf_table_sequence(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        start_index: int,
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> tuple[PdfSemanticBlock, int] | None:
        items = list(blocks or [])
        if start_index >= len(items):
            return None
        anchor = items[start_index]
        if int(anchor.page_start) != int(anchor.page_end):
            return None
        if str(anchor.block_type or "") not in {"paragraph", "heading", "table", "list_item"}:
            return None

        candidate_tables = list(page_table_map.get(int(anchor.page_start), []) or [])
        if not candidate_tables:
            return None

        best_result: tuple[PdfSemanticBlock, int, float] | None = None
        for table in candidate_tables:
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if len(rows) < self._min_rows:
                continue
            col_count = max((len(row) for row in rows), default=0)
            if col_count < self._min_cols:
                continue
            if float(anchor.bbox.bottom) < float(table.bbox.top) + 4.0:
                continue
            if float(anchor.bbox.top) > float(table.bbox.top) + 80.0:
                continue
            if str(anchor.column_id or "main") != "main":
                continue

            subset: list[PdfSemanticBlock] = []
            next_index = start_index
            for index in range(start_index, len(items)):
                block = items[index]
                if int(block.page_start) != int(anchor.page_start) or int(block.page_end) != int(anchor.page_end):
                    break
                if str(block.column_id or "main") != str(anchor.column_id or "main"):
                    break
                if str(block.region or "main") != str(anchor.region or "main"):
                    break
                if float(block.bbox.top) > float(table.bbox.bottom) + 10.0:
                    break
                if float(block.bbox.bottom) < float(table.bbox.top) - 18.0:
                    if index == start_index:
                        break
                    continue
                subset.append(block)
                next_index = index + 1

            if len(subset) < 2:
                continue

            merged_bbox = self._merge_bboxes([block.bbox for block in subset])
            table_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=merged_bbox)
            bbox_overlap = self._bbox_overlap_ratio(merged_bbox, table.bbox)
            token_overlap = self._token_overlap_ratio(
                "\n".join(str(block.text or "").strip() for block in subset if str(block.text or "").strip()),
                rows,
            )
            if table_coverage < 0.82 or bbox_overlap < 0.82 or token_overlap < 0.56:
                continue

            merged_block = self._merge_blocks_as_table(blocks=subset, table_rows=rows)
            score = (table_coverage * 0.4) + (bbox_overlap * 0.25) + (token_overlap * 0.35)
            if best_result is None or score > best_result[2]:
                best_result = (merged_block, next_index, score)

        if best_result is None:
            return None
        return best_result[0], best_result[1]

    def _split_paragraph_block(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> list[PdfSemanticBlock]:
        lines = [
            line_map[str(line_id)]
            for line_id in list(block.line_ids or [])
            if str(line_id) in line_map
        ]
        if len(lines) < self._min_rows:
            return [block]
        pymupdf_split = self._split_paragraph_block_using_pymupdf_tables(
            block=block,
            lines=lines,
            page_table_map=page_table_map,
        )
        if pymupdf_split is not None:
            return pymupdf_split

        output: list[PdfSemanticBlock] = []
        cursor = 0
        index = 0
        while index < len(lines):
            row_indexes = self._collect_row_like_indexes(lines=lines[index:], word_map=word_map)
            if not row_indexes:
                break
            first_row_index = index + row_indexes[0]
            start, end = self._find_table_cluster_bounds(
                lines=lines,
                start_index=first_row_index,
                word_map=word_map,
            )
            segment_lines = list(lines[start : end + 1])
            segment_line_ids = [
                str(line.line_id)
                for line in segment_lines
                if not self._is_caption_like(str(line.text or ""))
            ]
            if len(segment_line_ids) < self._min_rows:
                index = first_row_index + 1
                continue
            segment_block = self._build_block_from_lines(
                base_block=block,
                lines=segment_lines,
                block_type="paragraph",
                suffix=f"seg{start:03d}",
            )
            geometry_rows = self._extract_table_rows_from_line_ids(
                line_ids=segment_line_ids,
                line_map=line_map,
                word_map=word_map,
            )
            table_rows = geometry_rows
            if geometry_rows:
                pymupdf_rows = self._extract_pymupdf_table_rows(
                    block=segment_block,
                    page_table_map=page_table_map,
                )
                if pymupdf_rows:
                    table_rows = pymupdf_rows
            if not table_rows:
                index = first_row_index + 1
                continue
            if cursor < start:
                output.append(
                    self._build_block_from_lines(
                        base_block=block,
                        lines=lines[cursor:start],
                        block_type="paragraph",
                        suffix=f"lead{cursor:03d}",
                    )
                )
            output.append(
                self._build_block_from_lines(
                    base_block=block,
                    lines=segment_lines,
                    block_type="table",
                    suffix=f"tbl{start:03d}",
                    table_rows=table_rows,
                )
            )
            cursor = end + 1
            index = cursor

        if not output:
            return [block]
        if cursor < len(lines):
            output.append(
                self._build_block_from_lines(
                    base_block=block,
                    lines=lines[cursor:],
                    block_type="paragraph",
                    suffix=f"tail{cursor:03d}",
                )
            )
        return [candidate for candidate in output if candidate.line_ids]

    def _try_merge_table_sequence(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        start_index: int,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> tuple[PdfSemanticBlock, int] | None:
        anchor = list(blocks or [])[start_index]
        if str(anchor.block_type or "") not in {"heading", "table"}:
            return None
        if self._block_has_strong_table_rows(anchor):
            return None

        best_result: tuple[PdfSemanticBlock, int] | None = None
        max_window_end = min(len(blocks), start_index + 4)
        for end_index in range(start_index + 2, max_window_end + 1):
            subset = list(blocks[start_index:end_index])
            if str(anchor.block_type or "") == "heading" and any(
                self._block_has_strong_table_rows(block)
                for block in subset[1:]
            ):
                continue
            if not self._sequence_is_table_candidate(
                blocks=subset,
                line_map=line_map,
                word_map=word_map,
            ):
                continue
            line_ids = [line_id for block in subset for line_id in list(block.line_ids or [])]
            geometry_rows = self._extract_table_rows_from_line_ids(
                line_ids=line_ids,
                line_map=line_map,
                word_map=word_map,
            )
            if not geometry_rows:
                continue
            merged_block = self._merge_blocks_as_table(blocks=subset, table_rows=geometry_rows)
            pymupdf_rows = self._extract_pymupdf_table_rows(
                block=merged_block,
                page_table_map=page_table_map,
            )
            if pymupdf_rows:
                merged_block = replace(merged_block, table_rows=pymupdf_rows)
            best_result = (merged_block, end_index)
        return best_result

    def _sequence_is_table_candidate(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> bool:
        items = list(blocks or [])
        if len(items) < 2:
            return False
        if not any(str(block.block_type or "") in {"paragraph", "table"} for block in items):
            return False
        first = items[0]
        if any(
            int(block.page_start) != int(first.page_start)
            or int(block.page_end) != int(first.page_end)
            or str(block.column_id or "main") != str(first.column_id or "main")
            or str(block.region or "main") != str(first.region or "main")
            for block in items
        ):
            return False
        total_line_count = sum(len(list(block.line_ids or [])) for block in items)
        if total_line_count < self._min_rows:
            return False
        dense_paragraph_blocks = 0
        for prev, current in zip(items, items[1:]):
            gap = float(current.bbox.top) - float(prev.bbox.bottom)
            if gap > 42.0:
                return False
        for block in items:
            if self._block_has_caption_line(block=block, line_map=line_map):
                return False
            if str(block.block_type or "") == "table":
                dense_paragraph_blocks += 1
                continue
            if str(block.block_type or "") != "heading":
                density = self._block_table_line_density(
                    block=block,
                    line_map=line_map,
                    word_map=word_map,
                )
                if density >= 0.5:
                    dense_paragraph_blocks += 1
                continue
            if not (
                self._block_has_multicell_row(block=block, line_map=line_map, word_map=word_map)
                or self._block_has_compact_header_row(block=block, line_map=line_map, word_map=word_map)
            ):
                return False
        return dense_paragraph_blocks >= 1

    def _block_has_multicell_row(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> bool:
        for line_id in list(block.line_ids or []):
            line = line_map.get(str(line_id))
            if line is None:
                continue
            cells, _starts = self._split_line(line=line, word_map=word_map)
            if len(cells) >= self._min_cols:
                return True
            label_value_cells = self._split_label_value(line=line)
            if len(label_value_cells) >= self._min_cols:
                return True
        return False

    def _block_has_compact_header_row(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> bool:
        for line_id in list(block.line_ids or []):
            line = line_map.get(str(line_id))
            if line is None:
                continue
            cells, _starts = self._split_compact_header_line(
                line=line,
                word_map=word_map,
                target_cols=None,
            )
            if len(cells) >= self._min_cols:
                return True
        return False

    def _block_has_caption_line(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
    ) -> bool:
        for line_id in list(block.line_ids or []):
            line = line_map.get(str(line_id))
            if line is None:
                continue
            if self._is_caption_like(str(line.text or "")):
                return True
        return False

    def _block_table_line_density(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> float:
        line_ids = list(block.line_ids or [])
        if not line_ids:
            return 0.0
        row_like = 0
        for line_id in line_ids:
            line = line_map.get(str(line_id))
            if line is None or self._is_caption_like(str(line.text or "")):
                continue
            cells, _starts = self._split_line(line=line, word_map=word_map)
            if len(cells) >= self._min_cols:
                row_like += 1
                continue
            label_value_cells = self._split_label_value(line=line)
            if len(label_value_cells) >= self._min_cols:
                row_like += 1
        return row_like / max(1, len(line_ids))

    def _merge_blocks_as_table(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        table_rows: Sequence[Sequence[str]],
    ) -> PdfSemanticBlock:
        items = list(blocks or [])
        first = items[0]
        last = items[-1]
        avg_font_size = sum(float(block.avg_font_size or 0.0) for block in items) / max(1, len(items))
        merged_bbox = self._merge_bboxes([block.bbox for block in items])
        merged_text = "\n".join(str(block.text or "").strip() for block in items if str(block.text or "").strip()).strip()
        merged_line_ids = [line_id for block in items for line_id in list(block.line_ids or [])]
        return PdfSemanticBlock(
            block_id=str(first.block_id),
            block_type="table",
            page_start=int(first.page_start),
            page_end=int(last.page_end),
            text=merged_text,
            bbox=merged_bbox,
            line_ids=merged_line_ids,
            column_id=str(first.column_id or "main"),
            region=str(first.region or "main"),
            avg_font_size=round(avg_font_size, 2),
            reading_order_start=int(first.reading_order_start or 0),
            reading_order_end=int(last.reading_order_end or 0),
            table_rows=[list(row) for row in table_rows],
        )

    def _extract_pymupdf_table_rows(
        self,
        *,
        block: PdfSemanticBlock,
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> list[list[str]]:
        if int(block.page_start) != int(block.page_end):
            return []
        candidate_tables = list(page_table_map.get(int(block.page_start), []) or [])
        if not candidate_tables:
            return []

        best_rows: list[list[str]] = []
        best_score = 0.0
        block_line_count = len([line for line in str(block.text or "").splitlines() if line.strip()])
        for table in candidate_tables:
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if len(rows) < self._min_rows:
                continue
            col_count = max((len(row) for row in rows), default=0)
            if col_count < self._min_cols:
                continue
            bbox_score = self._bbox_overlap_ratio(block.bbox, table.bbox)
            token_score = self._token_overlap_ratio(block.text, rows)
            row_count_delta = abs(block_line_count - len(rows))
            if bbox_score < 0.78 or token_score < 0.78 or row_count_delta > 2:
                continue
            score = (bbox_score * 0.7) + (token_score * 0.3)
            if score > best_score:
                best_score = score
                best_rows = rows
        if best_score < 0.82:
            return []
        return best_rows

    def _split_paragraph_block_using_pymupdf_tables(
        self,
        *,
        block: PdfSemanticBlock,
        lines: Sequence[PdfResolvedLine],
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> list[PdfSemanticBlock] | None:
        if int(block.page_start) != int(block.page_end):
            return None
        matches = self._match_pymupdf_tables_to_lines(
            block=block,
            lines=lines,
            page_table_map=page_table_map,
        )
        if not matches:
            return None

        output: list[PdfSemanticBlock] = []
        cursor = 0
        for match_index, match in enumerate(matches):
            start_index = int(match["start_index"])
            end_index = int(match["end_index"])
            if start_index < cursor:
                continue
            if cursor < start_index:
                output.append(
                    self._build_block_from_lines(
                        base_block=block,
                        lines=lines[cursor:start_index],
                        block_type="paragraph",
                        suffix=f"lead{cursor:03d}",
                    )
                )
            table_lines = list(lines[start_index : end_index + 1])
            output.append(
                self._build_block_from_lines(
                    base_block=block,
                    lines=table_lines,
                    block_type="table",
                    suffix=f"pymutbl{match_index:03d}",
                    table_rows=match["rows"],
                )
            )
            cursor = end_index + 1

        if not output:
            return None
        if cursor < len(lines):
            output.append(
                self._build_block_from_lines(
                    base_block=block,
                    lines=lines[cursor:],
                    block_type="paragraph",
                    suffix=f"tail{cursor:03d}",
                )
            )
        return [candidate for candidate in output if candidate.line_ids]

    def _match_pymupdf_tables_to_lines(
        self,
        *,
        block: PdfSemanticBlock,
        lines: Sequence[PdfResolvedLine],
        page_table_map: dict[int, list[PdfTableAtom]],
    ) -> list[dict[str, object]]:
        candidate_tables = list(page_table_map.get(int(block.page_start), []) or [])
        if not candidate_tables:
            return []

        candidates: list[dict[str, object]] = []
        for table in candidate_tables:
            if self._raw_pymupdf_rows_look_like_sparse_chart(table.cells):
                continue
            rows = self._normalize_pymupdf_rows(table.cells)
            if len(rows) < self._min_rows:
                continue
            col_count = max((len(row) for row in rows), default=0)
            if col_count < self._min_cols:
                continue

            block_overlap = self._bbox_overlap_ratio(block.bbox, table.bbox)
            table_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=block.bbox)
            if block_overlap < 0.92 or table_coverage < 0.74:
                continue

            inside_indexes = [
                index
                for index, line in enumerate(list(lines or []))
                if self._line_overlaps_bbox(line=line, bbox=table.bbox)
            ]
            if not inside_indexes:
                continue

            start_index = min(inside_indexes)
            end_index = max(inside_indexes)
            segment_lines = list(lines[start_index : end_index + 1])
            segment_bbox = self._merge_bboxes([line.bbox for line in segment_lines])
            segment_overlap = self._bbox_overlap_ratio(segment_bbox, table.bbox)
            segment_coverage = self._bbox_coverage(target_bbox=table.bbox, observed_bbox=segment_bbox)
            segment_text = "\n".join(
                str(line.text or "").strip()
                for line in segment_lines
                if str(line.text or "").strip()
            ).strip()
            token_overlap = self._token_overlap_ratio(segment_text, rows)
            if segment_overlap < 0.68 or segment_coverage < 0.68 or token_overlap < 0.54:
                continue

            score = (
                (segment_overlap * 0.35)
                + (segment_coverage * 0.20)
                + (token_overlap * 0.35)
                + (table_coverage * 0.10)
            )
            candidates.append(
                {
                    "start_index": start_index,
                    "end_index": end_index,
                    "rows": rows,
                    "score": score,
                }
            )

        if not candidates:
            return []

        candidates.sort(
            key=lambda item: (
                int(item["start_index"]),
                -float(item["score"]),
                int(item["end_index"]),
            )
        )

        selected: list[dict[str, object]] = []
        consumed_indexes: set[int] = set()
        for candidate in candidates:
            start_index = int(candidate["start_index"])
            end_index = int(candidate["end_index"])
            if any(index in consumed_indexes for index in range(start_index, end_index + 1)):
                continue
            selected.append(candidate)
            consumed_indexes.update(range(start_index, end_index + 1))
        return selected

    def _extract_table_rows(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> list[list[str]]:
        return self._extract_table_rows_from_line_ids(
            line_ids=block.line_ids,
            line_map=line_map,
            word_map=word_map,
        )

    def _extract_table_rows_from_line_ids(
        self,
        *,
        line_ids: Sequence[str],
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> list[list[str]]:
        anchor_rows = self._extract_anchor_aligned_rows_from_line_ids(
            line_ids=line_ids,
            line_map=line_map,
            word_map=word_map,
        )
        default_rows = self._extract_consistent_rows_from_line_ids(
            line_ids=line_ids,
            line_map=line_map,
            word_map=word_map,
        )
        if self._should_prefer_anchor_rows(anchor_rows=anchor_rows, default_rows=default_rows):
            return anchor_rows
        return default_rows

    def _extract_consistent_rows_from_line_ids(
        self,
        *,
        line_ids: Sequence[str],
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> list[list[str]]:
        ordered_line_ids = [str(line_id) for line_id in list(line_ids or [])]
        row_candidates: list[tuple[str, list[str], list[float], str]] = []
        unresolved_lines: list[PdfResolvedLine] = []
        for line_id in list(line_ids or []):
            line = line_map.get(str(line_id))
            if line is None:
                continue
            cells, starts = self._split_line(line=line, word_map=word_map)
            if len(cells) >= self._min_cols:
                row_candidates.append((str(line.line_id), cells, starts, "grid"))
                continue
            label_value_cells = self._split_label_value(line=line)
            if label_value_cells:
                second_col_start = float(line.bbox.x0) + max(120.0, float(line.bbox.width) * 0.58)
                row_candidates.append((str(line.line_id), label_value_cells, [float(line.bbox.x0), second_col_start], "label"))
                continue
            unresolved_lines.append(line)

        if len(row_candidates) < self._min_rows:
            cell_count = Counter(len(cells) for _, cells, _, _ in row_candidates if len(cells) >= self._min_cols)
            target_cols = cell_count.most_common(1)[0][0] if cell_count else 0
            if target_cols <= 0:
                return []
        else:
            cell_count = Counter(len(cells) for _, cells, _, _ in row_candidates if len(cells) >= self._min_cols)
            if not cell_count:
                return []
            target_cols = cell_count.most_common(1)[0][0]

        ordered_candidates: dict[str, tuple[list[str], list[float], str]] = {
            line_id: (cells, starts, kind)
            for line_id, cells, starts, kind in row_candidates
            if len(cells) == target_cols
        }
        if unresolved_lines:
            for line in unresolved_lines:
                compact_cells, compact_starts = self._split_compact_header_line(
                    line=line,
                    word_map=word_map,
                    target_cols=target_cols,
                )
                if not compact_cells:
                    continue
                if not self._has_adjacent_row_candidate(
                    line_id=str(line.line_id),
                    ordered_line_ids=ordered_line_ids,
                    row_candidate_ids=set(ordered_candidates.keys()),
                ):
                    continue
                ordered_candidates[str(line.line_id)] = (compact_cells, compact_starts, "compact")

        ordered_rows = [
            ordered_candidates[line_id]
            for line_id in ordered_line_ids
            if line_id in ordered_candidates
        ]
        if len(ordered_rows) < self._min_rows:
            return []

        aligned_rows = [
            (cells, starts)
            for cells, starts, kind in ordered_rows
            if kind != "compact"
        ]
        compact_rows = [
            (cells, starts)
            for cells, starts, kind in ordered_rows
            if kind == "compact"
        ]
        if not aligned_rows:
            return []
        reference_starts = next(
            (starts for cells, starts in aligned_rows if len(cells) == target_cols and starts),
            [],
        )
        if not reference_starts:
            return []
        alignment_threshold = max(self._alignment_tolerance, self._min_gap * 0.75)
        if len(aligned_rows) >= 2 and not self._has_consistent_alignment(
            aligned_rows=aligned_rows,
            reference_starts=reference_starts,
            threshold=alignment_threshold,
        ):
            return []

        rows = [self._normalize_row(cells, target_cols) for cells, _starts, _kind in ordered_rows]
        if len(rows) < self._min_rows:
            return []
        if self._rows_look_like_parallel_prose(rows):
            return []
        return rows

    def _extract_anchor_aligned_rows_from_line_ids(
        self,
        *,
        line_ids: Sequence[str],
        line_map: dict[str, PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> list[list[str]]:
        lines = [
            line_map[str(line_id)]
            for line_id in list(line_ids or [])
            if str(line_id) in line_map and not self._is_caption_like(str(line_map[str(line_id)].text or ""))
        ]
        if len(lines) < self._min_rows:
            return []

        header = self._select_anchor_header_line(lines=lines, word_map=word_map)
        if header is None:
            return []
        header_cells = self._split_anchor_header_cells(line=header, word_map=word_map)
        if len(header_cells) < max(self._min_cols, 4):
            return []

        intervals = self._build_anchor_intervals(header_cells=header_cells)
        rows: list[list[str]] = []
        numeric_like_rows = 0
        for line in lines:
            if line.line_id == header.line_id:
                header_row = [cell_text for cell_text, _bbox in header_cells]
                if self._row_non_empty_cells(header_row) >= self._min_cols:
                    rows.append(header_row)
                continue
            row = self._assign_words_to_anchor_intervals(
                line=line,
                intervals=intervals,
                word_map=word_map,
            )
            if self._row_non_empty_cells(row) < self._min_cols:
                continue
            if self._row_numeric_marker_cell_count(row) >= max(2, len(row) // 2):
                numeric_like_rows += 1
            rows.append(row)

        if len(rows) < self._min_rows or numeric_like_rows < 1:
            return []
        return rows

    def _split_line(
        self,
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
    ) -> tuple[list[str], list[float]]:
        words = [
            word_map[word_id]
            for word_id in list(line.word_ids or [])
            if word_id in word_map
        ]
        if len(words) < self._min_cols:
            return [], []
        words = sorted(words, key=lambda item: (float(item.bbox.x0), float(item.bbox.top), item.word_id))
        cells: list[list[str]] = []
        starts: list[float] = []
        current_cell: list[str] = []
        current_start = float(words[0].bbox.x0)
        max_font = max(float(line.avg_font_size or 0.0), 8.0)
        gap_threshold = max(self._min_gap, max_font * 2.2)
        for index, word in enumerate(words):
            if not current_cell:
                current_cell = [str(word.text or "").strip()]
                current_start = float(word.bbox.x0)
            else:
                previous_word = words[index - 1]
                gap = float(word.bbox.x0) - float(previous_word.bbox.x1)
                if gap > gap_threshold:
                    cells.append(self._join_cell(current_cell))
                    starts.append(current_start)
                    current_cell = [str(word.text or "").strip()]
                    current_start = float(word.bbox.x0)
                else:
                    current_cell.append(str(word.text or "").strip())
        if current_cell:
            cells.append(self._join_cell(current_cell))
            starts.append(current_start)
        cleaned_cells = [cell for cell in cells if cell]
        if len(cleaned_cells) < self._min_cols:
            return [], []
        return cleaned_cells, starts[: len(cleaned_cells)]

    def _split_compact_header_line(
        self,
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
        target_cols: int | None,
    ) -> tuple[list[str], list[float]]:
        words = [
            word_map[word_id]
            for word_id in list(line.word_ids or [])
            if word_id in word_map and str(word_map[word_id].text or "").strip()
        ]
        tokens = [str(word.text or "").strip() for word in words if str(word.text or "").strip()]
        if not tokens:
            return [], []
        if target_cols is not None and len(tokens) != target_cols:
            return [], []
        if target_cols is None and len(tokens) < self._min_cols:
            return [], []
        if len(tokens) > 12:
            return [], []
        if self._is_caption_like(str(line.text or "")) or self._looks_sentence_like(str(line.text or "")):
            return [], []
        if any(token.endswith((".", ";", ",")) for token in tokens):
            return [], []
        if any(len(token) > 24 for token in tokens):
            return [], []
        prose_tokens = sum(1 for token in tokens if token.lower() in _COMMON_PROSE_WORDS)
        if prose_tokens > max(1, len(tokens) // 4):
            return [], []
        headerish_tokens = sum(
            1
            for token in tokens
            if token[:1].isupper() or token.isupper() or any(char.isdigit() for char in token)
        )
        if headerish_tokens < max(2, len(tokens) // 2):
            return [], []
        return tokens, [float(word.bbox.x0) for word in words[: len(tokens)]]

    def _select_anchor_header_line(
        self,
        *,
        lines: Sequence[PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> PdfResolvedLine | None:
        best_line: PdfResolvedLine | None = None
        best_score = -1.0
        for line in list(lines[:4]):
            if self._looks_sentence_like(str(line.text or "")):
                continue
            header_cells = self._split_anchor_header_cells(line=line, word_map=word_map)
            if len(header_cells) < max(self._min_cols, 4):
                continue
            numeric_tokens = self._count_numeric_marker_words(line=line, word_map=word_map)
            score = (len(header_cells) * 10) - numeric_tokens
            if score > best_score:
                best_score = score
                best_line = line
        return best_line

    def _split_anchor_header_cells(
        self,
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
    ) -> list[tuple[str, PdfBBox]]:
        words = self._line_words(line=line, word_map=word_map)
        if len(words) < max(self._min_cols, 4):
            return []
        gaps = [
            max(0.0, float(words[index].bbox.x0) - float(words[index - 1].bbox.x1))
            for index in range(1, len(words))
        ]
        median_gap = float(median(gaps)) if gaps else 0.0
        merge_threshold = max(3.5, min(6.0, median_gap * 0.55))

        cells: list[list[PdfWordAtom]] = []
        current: list[PdfWordAtom] = []
        for index, word in enumerate(words):
            if not current:
                current = [word]
                continue
            previous_word = words[index - 1]
            gap = float(word.bbox.x0) - float(previous_word.bbox.x1)
            if self._should_merge_anchor_words(
                previous_word=previous_word,
                current_word=word,
                gap=gap,
                merge_threshold=merge_threshold,
            ):
                current.append(word)
                continue
            cells.append(current)
            current = [word]
        if current:
            cells.append(current)

        return [
            (
                self._join_cell([str(word.text or "").strip() for word in cell]),
                self._merge_bboxes([word.bbox for word in cell]),
            )
            for cell in cells
            if any(str(word.text or "").strip() for word in cell)
        ]

    @staticmethod
    def _should_merge_anchor_words(
        *,
        previous_word: PdfWordAtom,
        current_word: PdfWordAtom,
        gap: float,
        merge_threshold: float,
    ) -> bool:
        previous_text = str(previous_word.text or "").strip()
        current_text = str(current_word.text or "").strip()
        if not previous_text or not current_text:
            return False
        if current_text.startswith("(") or previous_text.endswith("("):
            return True
        return gap <= merge_threshold

    def _build_anchor_intervals(
        self,
        *,
        header_cells: Sequence[tuple[str, PdfBBox]],
    ) -> list[tuple[float, float]]:
        intervals: list[tuple[float, float]] = []
        boxes = [bbox for _text, bbox in list(header_cells or [])]
        for index, bbox in enumerate(boxes):
            left_boundary = float("-inf")
            right_boundary = float("inf")
            if index > 0:
                previous_bbox = boxes[index - 1]
                left_boundary = (float(previous_bbox.x1) + float(bbox.x0)) / 2.0
            if index + 1 < len(boxes):
                next_bbox = boxes[index + 1]
                right_boundary = (float(bbox.x1) + float(next_bbox.x0)) / 2.0
            intervals.append((left_boundary, right_boundary))
        return intervals

    def _assign_words_to_anchor_intervals(
        self,
        *,
        line: PdfResolvedLine,
        intervals: Sequence[tuple[float, float]],
        word_map: dict[str, PdfWordAtom],
    ) -> list[str]:
        words = self._line_words(line=line, word_map=word_map)
        if not words or not intervals:
            return []
        cells: list[list[str]] = [[] for _ in list(intervals)]
        for word in words:
            center_x = (float(word.bbox.x0) + float(word.bbox.x1)) / 2.0
            assigned_index = None
            for index, (left, right) in enumerate(intervals):
                if center_x >= left and center_x < right:
                    assigned_index = index
                    break
            if assigned_index is None:
                assigned_index = 0 if center_x < intervals[0][0] else len(intervals) - 1
            cells[assigned_index].append(str(word.text or "").strip())
        return [self._join_cell(parts) for parts in cells]

    @staticmethod
    def _line_words(
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
    ) -> list[PdfWordAtom]:
        return sorted(
            [
                word_map[word_id]
                for word_id in list(line.word_ids or [])
                if word_id in word_map and str(word_map[word_id].text or "").strip()
            ],
            key=lambda item: (float(item.bbox.x0), float(item.bbox.top), item.word_id),
        )

    @staticmethod
    def _row_non_empty_cells(row: Sequence[str]) -> int:
        return sum(1 for cell in list(row or []) if str(cell or "").strip())

    @staticmethod
    def _row_numeric_marker_cell_count(row: Sequence[str]) -> int:
        count = 0
        for cell in list(row or []):
            text = str(cell or "").strip()
            if not text:
                continue
            normalized = text.replace(" ", "")
            if re.fullmatch(r"(?:[-+]?\d+(?:\.\d+)?%?|N/?A|O|X|✗|✓|✔)+", normalized, flags=re.IGNORECASE):
                count += 1
                continue
            tokens = [token for token in text.split() if token]
            if tokens and all(
                re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?|N/?A|O|X|✗|✓|✔", token, flags=re.IGNORECASE)
                for token in tokens
            ):
                count += 1
        return count

    def _count_numeric_marker_words(
        self,
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
    ) -> int:
        count = 0
        for word in self._line_words(line=line, word_map=word_map):
            token = str(word.text or "").strip()
            if re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?|N/?A|O|X|✗|✓|✔", token, flags=re.IGNORECASE):
                count += 1
        return count

    def _should_prefer_anchor_rows(
        self,
        *,
        anchor_rows: Sequence[Sequence[str]],
        default_rows: Sequence[Sequence[str]],
    ) -> bool:
        if not anchor_rows:
            return False
        if not default_rows:
            return True

        anchor_cols = max((self._row_non_empty_cells(row) for row in list(anchor_rows or [])), default=0)
        default_cols = max((self._row_non_empty_cells(row) for row in list(default_rows or [])), default=0)
        if anchor_cols >= default_cols + 2 and len(anchor_rows) >= max(2, len(default_rows) - 1):
            return True

        anchor_score = len(anchor_rows) * anchor_cols
        default_score = len(default_rows) * default_cols
        return anchor_score > (default_score * 1.2)

    @staticmethod
    def _split_label_value(*, line: PdfResolvedLine) -> list[str]:
        text = str(line.text or "").strip()
        if not text or ":" not in text:
            return []
        left, right = text.split(":", 1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            return []
        if len(left.split()) > 4:
            return []
        return [left, right]

    @staticmethod
    def _join_cell(parts: Sequence[str]) -> str:
        return " ".join(part for part in list(parts or []) if str(part or "").strip()).strip()

    @staticmethod
    def _extract_words_in_bbox(
        *,
        words: Sequence[PdfWordAtom],
        bbox: PdfBBox,
    ) -> str:
        cell_words: list[tuple[float, float, str]] = []
        for word in list(words or []):
            center_x = (float(word.bbox.x0) + float(word.bbox.x1)) / 2.0
            center_y = (float(word.bbox.top) + float(word.bbox.bottom)) / 2.0
            if center_x < float(bbox.x0) or center_x > float(bbox.x1):
                continue
            if center_y < float(bbox.top) or center_y > float(bbox.bottom):
                continue
            text = str(word.text or "").strip()
            if not text:
                continue
            cell_words.append((float(word.bbox.top), float(word.bbox.x0), text))
        return " ".join(text for _top, _left, text in sorted(cell_words)).strip()

    def _fit_row_to_target_cols(
        self,
        *,
        cells: Sequence[str],
        target_cols: int,
    ) -> list[str]:
        normalized = [str(cell or "").strip() for cell in list(cells or [])]
        while len(normalized) > target_cols and normalized and not normalized[0]:
            normalized.pop(0)
        while len(normalized) > target_cols and normalized and not normalized[-1]:
            normalized.pop()
        if len(normalized) > target_cols:
            normalized = normalized[:target_cols]
        return self._normalize_row(normalized, target_cols)

    def _rows_are_usable(self, rows: Sequence[Sequence[str]]) -> bool:
        materialized = [list(row) for row in list(rows or [])]
        if len(materialized) < self._min_rows:
            return False
        max_cols = max((self._row_non_empty_cells(row) for row in materialized), default=0)
        return max_cols >= self._min_cols

    def _block_has_strong_table_rows(self, block: PdfSemanticBlock) -> bool:
        rows = [list(row) for row in list(block.table_rows or [])]
        if not self._rows_are_usable(rows):
            return False
        row_count = sum(1 for row in rows if self._row_non_empty_cells(row) > 0)
        col_count = max((self._row_non_empty_cells(row) for row in rows), default=0)
        return row_count >= max(3, self._min_rows + 1) and col_count >= max(3, self._min_cols + 1)

    @classmethod
    def _rows_look_like_parallel_prose(cls, rows: Sequence[Sequence[str]]) -> bool:
        materialized = [
            [str(cell or "").strip() for cell in list(row or []) if str(cell or "").strip()]
            for row in list(rows or [])
        ]
        materialized = [row for row in materialized if row]
        if len(materialized) < 2:
            return False
        if max((len(row) for row in materialized), default=0) != 2:
            return False
        headerish_first = cls._row_looks_like_compact_header(materialized[0])
        numeric_rows = sum(1 for row in materialized if cls._row_numeric_marker_cell_count(row) > 0)
        if numeric_rows > 0:
            return False
        prose_rows = sum(
            1
            for row in materialized
            if len(row) == 2 and all(cls._cell_looks_like_parallel_prose_fragment(cell) for cell in row)
        )
        if headerish_first:
            if prose_rows >= len(materialized):
                return True
            return len(materialized) >= 3 and prose_rows >= max(2, len(materialized) - 1)
        return prose_rows >= len(materialized)

    @classmethod
    def _rows_look_like_sparse_chart(cls, rows: Sequence[Sequence[str]]) -> bool:
        materialized = [
            [str(cell or "").strip() for cell in list(row or [])]
            for row in list(rows or [])
        ]
        if len(materialized) < 2:
            return False
        col_count = max((len(row) for row in materialized), default=0)
        if col_count < 6:
            return False
        non_empty_cells = [
            cell
            for row in materialized
            for cell in row
            if str(cell or "").strip()
        ]
        if len(non_empty_cells) < 3:
            return False
        density = len(non_empty_cells) / max(1, len(materialized) * col_count)
        if density > 0.3:
            return False
        max_row_fill = max((cls._row_non_empty_cells(row) for row in materialized), default=0)
        if max_row_fill > max(3, col_count // 3):
            return False
        chart_like_cells = sum(1 for cell in non_empty_cells if cls._cell_looks_like_chart_axis_label(cell))
        return chart_like_cells >= max(3, int(len(non_empty_cells) * 0.75))

    @classmethod
    def _row_looks_like_compact_header(cls, row: Sequence[str]) -> bool:
        cells = [str(cell or "").strip() for cell in list(row or []) if str(cell or "").strip()]
        if len(cells) < 2:
            return False
        if cls._row_numeric_marker_cell_count(cells) >= max(1, len(cells) // 2):
            return False
        headerish_cells = 0
        for cell in cells:
            tokens = [token.strip(".,;:()[]{}") for token in cell.split() if token.strip()]
            if not tokens or len(tokens) > 5:
                continue
            if cls._looks_sentence_like(cell):
                continue
            prose_hits = sum(1 for token in tokens if token.lower() in _COMMON_PROSE_WORDS)
            if prose_hits > max(1, len(tokens) // 3):
                continue
            if cell[:1].isupper() or any(char.isdigit() for char in cell):
                headerish_cells += 1
        return headerish_cells >= max(2, len(cells) - 1)

    @classmethod
    def _cell_looks_like_prose_span(cls, text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        tokens = [token.strip(".,;:()[]{}").lower() for token in cleaned.split() if token.strip()]
        if len(tokens) < 4:
            return False
        if any(re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?|N/?A|O|X|✗|✓|✔", token, flags=re.IGNORECASE) for token in tokens):
            return False
        prose_hits = sum(1 for token in tokens if token in _COMMON_PROSE_WORDS)
        lowercase_heavy = sum(1 for token in tokens if token and token[:1].islower())
        titlecase_heavy = sum(1 for token in tokens if token and token[:1].isupper())
        if prose_hits >= max(2, len(tokens) // 5):
            return True
        if len(tokens) >= 6 and titlecase_heavy <= max(1, len(tokens) // 3):
            return True
        return len(tokens) >= 4 and lowercase_heavy >= max(3, len(tokens) - 1)

    @classmethod
    def _cell_looks_like_parallel_prose_fragment(cls, text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if cls._cell_looks_like_prose_span(cleaned):
            return True
        tokens = [token.strip(".,;:()[]{}") for token in cleaned.split() if token.strip(".,;:()[]{}")]
        if not tokens:
            return False
        if cls._row_numeric_marker_cell_count([cleaned]) > 0:
            return False
        if len(tokens) == 1:
            token = tokens[0]
            return len(token) >= 6 and (token[:1].islower() or "-" in token)
        if len(tokens) == 2:
            lowercase_initials = sum(1 for token in tokens if token and token[:1].islower())
            if lowercase_initials == 2:
                return True
        if len(tokens) < 3:
            return False
        if cleaned.endswith((",", ";", "-", "–", "—")):
            return True
        if cleaned.count(",") >= 1 and len(tokens) >= 3:
            return True
        uppercase_initials = sum(1 for token in tokens if token and token[:1].isupper())
        lowercase_initials = sum(1 for token in tokens if token and token[:1].islower())
        if len(tokens) >= 4 and any(char.isdigit() for char in cleaned) and uppercase_initials >= 2:
            return True
        if len(tokens) >= 4 and uppercase_initials >= max(3, len(tokens) - 1):
            return True
        return len(tokens) >= 4 and lowercase_initials >= 2

    @classmethod
    def _cell_looks_like_chart_axis_label(cls, text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if cls._row_numeric_marker_cell_count([cleaned]) > 0:
            return True
        tokens = [token.strip() for token in cleaned.split() if token.strip()]
        if not tokens or len(tokens) > 3:
            return False
        if any(char.isdigit() for char in cleaned):
            return True
        return all(len(token) <= 6 for token in tokens) and not cls._looks_sentence_like(cleaned)

    @staticmethod
    def _normalize_row(cells: Sequence[str], target_cols: int) -> list[str]:
        row = [str(cell or "").strip() for cell in list(cells or [])[:target_cols]]
        if len(row) < target_cols:
            row.extend([""] * (target_cols - len(row)))
        return row

    @staticmethod
    def _normalize_pymupdf_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
        normalized: list[list[str]] = []
        target_cols = max((len(list(row or [])) for row in list(rows or [])), default=0)
        if target_cols <= 0:
            return []
        for row in list(rows or []):
            cleaned = [" ".join(str(cell or "").split()).strip() for cell in list(row or [])]
            if not any(cleaned):
                continue
            if len(cleaned) < target_cols:
                cleaned.extend([""] * (target_cols - len(cleaned)))
            normalized.append(cleaned[:target_cols])
        normalized = LocalPdfTableDetector._expand_centered_dual_header_rows(normalized)
        normalized = LocalPdfTableDetector._collapse_sparse_header_rows(normalized)
        normalized = LocalPdfTableDetector._shift_orphan_header_cells(normalized)
        normalized = LocalPdfTableDetector._drop_globally_empty_columns(normalized)
        return normalized

    @classmethod
    def _raw_pymupdf_rows_look_like_sparse_chart(
        cls,
        rows: Sequence[Sequence[str]],
    ) -> bool:
        materialized = [
            [" ".join(str(cell or "").split()).strip() for cell in list(row or [])]
            for row in list(rows or [])
        ]
        return cls._rows_look_like_sparse_chart(materialized)

    @classmethod
    def _expand_centered_dual_header_rows(
        cls,
        rows: Sequence[Sequence[str]],
    ) -> list[list[str]]:
        expanded: list[list[str]] = []
        for row in list(rows or []):
            materialized = [str(cell or "").strip() for cell in list(row or [])]
            if len(materialized) == 3 and not materialized[0] and not materialized[2] and materialized[1]:
                split_cells = cls._split_centered_dual_header_text(materialized[1])
                if split_cells is not None:
                    expanded.append(["", split_cells[0], split_cells[1]])
                    continue
            expanded.append(materialized)
        return expanded

    @classmethod
    def _split_centered_dual_header_text(cls, text: str) -> tuple[str, str] | None:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return None

        tokens = [token for token in cleaned.split() if token]
        if (
            len(tokens) == 2
            and all(token[:1].isupper() for token in tokens)
            and not any(any(char.isdigit() for char in token) for token in tokens)
        ):
            return tokens[0], tokens[1]

        if len(tokens) % 2 == 0 and len(tokens) >= 4:
            midpoint = len(tokens) // 2
            left = " ".join(tokens[:midpoint]).strip()
            right = " ".join(tokens[midpoint:]).strip()
            if left and right and left == right:
                return left, right
        return None

    @classmethod
    def _collapse_sparse_header_rows(
        cls,
        rows: Sequence[Sequence[str]],
    ) -> list[list[str]]:
        materialized = [
            [str(cell or "").strip() for cell in list(row or [])]
            for row in list(rows or [])
        ]
        if len(materialized) < 2:
            return materialized

        header = list(materialized[0])
        consumed = 1
        merged_any = False
        for row in materialized[1:4]:
            non_empty_indexes = [index for index, cell in enumerate(row) if str(cell or "").strip()]
            if not non_empty_indexes:
                consumed += 1
                merged_any = True
                continue
            if len(non_empty_indexes) > 2:
                break
            if cls._row_numeric_marker_cell_count(row) > 0:
                break
            if len(non_empty_indexes) == 1 and not str(header[non_empty_indexes[0]] or "").strip():
                break
            for index in non_empty_indexes:
                text = str(row[index] or "").strip()
                if not text:
                    continue
                header[index] = f"{header[index]} {text}".strip() if header[index] else text
            consumed += 1
            merged_any = True

        if not merged_any:
            return materialized
        return [header] + materialized[consumed:]

    @classmethod
    def _shift_orphan_header_cells(
        cls,
        rows: Sequence[Sequence[str]],
    ) -> list[list[str]]:
        materialized = [
            [str(cell or "").strip() for cell in list(row or [])]
            for row in list(rows or [])
        ]
        if len(materialized) < 2:
            return materialized

        header = list(materialized[0])
        col_count = len(header)
        if col_count < 4:
            return materialized
        data_support = [
            sum(1 for row in materialized[1:] if index < len(row) and str(row[index] or "").strip())
            for index in range(col_count)
        ]
        for index, cell in enumerate(list(header)):
            text = str(cell or "").strip()
            if not text or data_support[index] > 0:
                continue
            candidates: list[tuple[int, int, int]] = []
            for offset in (1, 2):
                for target_index in (index - offset, index + offset):
                    if target_index < 0 or target_index >= col_count:
                        continue
                    if data_support[target_index] <= 0:
                        continue
                    if str(header[target_index] or "").strip():
                        continue
                    candidates.append((offset, -data_support[target_index], target_index))
                if candidates:
                    break
            if not candidates:
                continue
            _offset, _support, target_index = sorted(candidates)[0]
            header[target_index] = f"{header[target_index]} {text}".strip() if header[target_index] else text
            header[index] = ""
        return [header] + materialized[1:]

    @classmethod
    def _drop_globally_empty_columns(
        cls,
        rows: Sequence[Sequence[str]],
    ) -> list[list[str]]:
        materialized = [
            [str(cell or "").strip() for cell in list(row or [])]
            for row in list(rows or [])
        ]
        if not materialized:
            return materialized
        keep_indexes = [
            index
            for index in range(max(len(row) for row in materialized))
            if any(index < len(row) and str(row[index] or "").strip() for row in materialized)
        ]
        if not keep_indexes:
            return materialized
        return [
            [row[index] if index < len(row) else "" for index in keep_indexes]
            for row in materialized
        ]

    @staticmethod
    def _line_overlaps_bbox(
        *,
        line: PdfResolvedLine,
        bbox: PdfBBox,
        horizontal_ratio: float = 0.2,
    ) -> bool:
        overlap_top = max(float(line.bbox.top), float(bbox.top))
        overlap_bottom = min(float(line.bbox.bottom), float(bbox.bottom))
        vertical_overlap = max(0.0, overlap_bottom - overlap_top)
        if vertical_overlap <= 0.0:
            center_y = (float(line.bbox.top) + float(line.bbox.bottom)) / 2.0
            if center_y < float(bbox.top) or center_y > float(bbox.bottom):
                return False

        overlap_left = max(float(line.bbox.x0), float(bbox.x0))
        overlap_right = min(float(line.bbox.x1), float(bbox.x1))
        overlap_width = max(0.0, overlap_right - overlap_left)
        min_width = max(1.0, min(float(line.bbox.width), float(bbox.width)))
        if overlap_width >= min_width * horizontal_ratio:
            return True

        center_x = (float(line.bbox.x0) + float(line.bbox.x1)) / 2.0
        return float(bbox.x0) <= center_x <= float(bbox.x1)

    @staticmethod
    def _bbox_overlap_ratio(first_bbox, second_bbox) -> float:
        overlap_left = max(float(first_bbox.x0), float(second_bbox.x0))
        overlap_top = max(float(first_bbox.top), float(second_bbox.top))
        overlap_right = min(float(first_bbox.x1), float(second_bbox.x1))
        overlap_bottom = min(float(first_bbox.bottom), float(second_bbox.bottom))
        overlap_width = max(0.0, overlap_right - overlap_left)
        overlap_height = max(0.0, overlap_bottom - overlap_top)
        overlap_area = overlap_width * overlap_height
        if overlap_area <= 0.0:
            return 0.0
        first_area = max(1.0, float(first_bbox.width) * float(first_bbox.height))
        second_area = max(1.0, float(second_bbox.width) * float(second_bbox.height))
        return overlap_area / min(first_area, second_area)

    @staticmethod
    def _bbox_coverage(*, target_bbox: PdfBBox, observed_bbox: PdfBBox) -> float:
        overlap_left = max(float(target_bbox.x0), float(observed_bbox.x0))
        overlap_top = max(float(target_bbox.top), float(observed_bbox.top))
        overlap_right = min(float(target_bbox.x1), float(observed_bbox.x1))
        overlap_bottom = min(float(target_bbox.bottom), float(observed_bbox.bottom))
        overlap_width = max(0.0, overlap_right - overlap_left)
        overlap_height = max(0.0, overlap_bottom - overlap_top)
        overlap_area = overlap_width * overlap_height
        if overlap_area <= 0.0:
            return 0.0
        target_area = max(1.0, float(target_bbox.width) * float(target_bbox.height))
        return overlap_area / target_area

    @staticmethod
    def _token_overlap_ratio(block_text: str, rows: Sequence[Sequence[str]]) -> float:
        block_tokens = {
            token.strip().lower()
            for token in str(block_text or "").replace("\n", " ").split()
            if token.strip()
        }
        if not block_tokens:
            return 0.0
        table_tokens = {
            token.strip().lower()
            for row in list(rows or [])
            for cell in list(row or [])
            for token in str(cell or "").split()
            if token.strip()
        }
        if not table_tokens:
            return 0.0
        overlap = block_tokens & table_tokens
        return len(overlap) / max(1, min(len(block_tokens), len(table_tokens)))

    @staticmethod
    def _merge_bboxes(boxes: Sequence[PdfBBox]) -> PdfBBox:
        valid_boxes = [box for box in list(boxes or []) if isinstance(box, PdfBBox)]
        if not valid_boxes:
            return PdfBBox(x0=0.0, top=0.0, x1=0.0, bottom=0.0)
        return PdfBBox(
            x0=min(float(box.x0) for box in valid_boxes),
            top=min(float(box.top) for box in valid_boxes),
            x1=max(float(box.x1) for box in valid_boxes),
            bottom=max(float(box.bottom) for box in valid_boxes),
        )

    @staticmethod
    def _has_consistent_alignment(
        *,
        aligned_rows: Sequence[tuple[list[str], list[float]]],
        reference_starts: Sequence[float],
        threshold: float,
    ) -> bool:
        if not aligned_rows or not reference_starts:
            return False
        matched_rows = 0
        for _, starts in aligned_rows:
            if len(starts) != len(reference_starts):
                continue
            if all(abs(float(starts[index]) - float(reference_starts[index])) <= threshold for index in range(len(reference_starts))):
                matched_rows += 1
        return matched_rows >= 2

    def _collect_row_like_indexes(
        self,
        *,
        lines: Sequence[PdfResolvedLine],
        word_map: dict[str, PdfWordAtom],
    ) -> list[int]:
        indexes: list[int] = []
        for index, line in enumerate(list(lines or [])):
            if self._is_caption_like(str(line.text or "")):
                continue
            cells, _starts = self._split_line(line=line, word_map=word_map)
            if len(cells) >= self._min_cols:
                indexes.append(index)
                continue
            label_value_cells = self._split_label_value(line=line)
            if len(label_value_cells) >= self._min_cols:
                indexes.append(index)
        return indexes

    def _find_table_cluster_bounds(
        self,
        *,
        lines: Sequence[PdfResolvedLine],
        start_index: int,
        word_map: dict[str, PdfWordAtom],
    ) -> tuple[int, int]:
        items = list(lines or [])
        start = int(start_index)
        end = int(start_index)
        row_count = 0
        last_row_index = start
        for index in range(start, len(items)):
            line = items[index]
            if self._is_caption_like(str(line.text or "")):
                break
            cells, _starts = self._split_line(line=line, word_map=word_map)
            label_value_cells = self._split_label_value(line=line)
            is_row_like = len(cells) >= self._min_cols or len(label_value_cells) >= self._min_cols
            if is_row_like:
                row_count += 1
                last_row_index = index
                end = index
                continue
            if index == start - 1:
                continue
            if index <= last_row_index + 1 and self._is_potential_table_context_line(line=line, word_map=word_map):
                end = index
                continue
            break

        while start > 0:
            previous = items[start - 1]
            if self._is_caption_like(str(previous.text or "")):
                break
            if not self._is_potential_table_context_line(line=previous, word_map=word_map):
                break
            if self._looks_sentence_like(str(previous.text or "")):
                break
            start -= 1
            if start_index - start >= 2:
                break
        return start, end

    def _is_potential_table_context_line(
        self,
        *,
        line: PdfResolvedLine,
        word_map: dict[str, PdfWordAtom],
    ) -> bool:
        if self._is_caption_like(str(line.text or "")):
            return False
        compact_cells, _starts = self._split_compact_header_line(
            line=line,
            word_map=word_map,
            target_cols=None,
        )
        if compact_cells:
            return True
        text = str(line.text or "").strip()
        if not text or self._looks_sentence_like(text):
            return False
        word_count = len(text.split())
        return word_count <= 6 and len(text) <= 60

    def _build_block_from_lines(
        self,
        *,
        base_block: PdfSemanticBlock,
        lines: Sequence[PdfResolvedLine],
        block_type: str,
        suffix: str,
        table_rows: Sequence[Sequence[str]] | None = None,
    ) -> PdfSemanticBlock:
        kept_lines = [line for line in list(lines or []) if isinstance(line, PdfResolvedLine)]
        if not kept_lines:
            return replace(base_block, block_id=f"{base_block.block_id}_{suffix}", line_ids=[])
        avg_font_size = sum(float(line.avg_font_size or 0.0) for line in kept_lines) / max(1, len(kept_lines))
        return PdfSemanticBlock(
            block_id=f"{base_block.block_id}_{suffix}",
            block_type=block_type,
            page_start=int(kept_lines[0].page),
            page_end=int(kept_lines[-1].page),
            text="\n".join(str(line.text or "").strip() for line in kept_lines if str(line.text or "").strip()).strip(),
            bbox=self._merge_bboxes([line.bbox for line in kept_lines]),
            line_ids=[str(line.line_id) for line in kept_lines],
            column_id=str(base_block.column_id or "main"),
            region=str(base_block.region or "main"),
            avg_font_size=round(avg_font_size, 2),
            reading_order_start=int(kept_lines[0].reading_order or 0),
            reading_order_end=int(kept_lines[-1].reading_order or 0),
            table_rows=[list(row) for row in list(table_rows or [])],
        )

    @staticmethod
    def _has_adjacent_row_candidate(
        *,
        line_id: str,
        ordered_line_ids: Sequence[str],
        row_candidate_ids: set[str],
    ) -> bool:
        try:
            index = list(ordered_line_ids).index(str(line_id))
        except ValueError:
            return False
        previous_id = ordered_line_ids[index - 1] if index > 0 else None
        next_id = ordered_line_ids[index + 1] if index + 1 < len(ordered_line_ids) else None
        return str(previous_id or "") in row_candidate_ids or str(next_id or "") in row_candidate_ids

    @staticmethod
    def _is_caption_like(text: str) -> bool:
        return bool(_CAPTION_RE.match(str(text or "").strip()))

    @staticmethod
    def _looks_sentence_like(text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if cleaned.endswith((".", "?", "!")) and len(cleaned.split()) >= 4:
            return True
        tokens = [token.strip(".,;:()[]{}").lower() for token in cleaned.split()]
        prose_hits = sum(1 for token in tokens if token in _COMMON_PROSE_WORDS)
        return len(tokens) >= 8 and prose_hits >= max(2, len(tokens) // 4)
