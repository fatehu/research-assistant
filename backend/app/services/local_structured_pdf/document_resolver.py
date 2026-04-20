from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Sequence

from .contracts import (
    PdfBBox,
    PdfNormalizedPage,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfTextBlockAtom,
    PdfTextLine,
)


_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?%\)\]\}])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([\(\[\{])\s+")
_DIGIT_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")
_NON_WORD_EDGE_RE = re.compile(r"^[^\w#]+|[^\w#]+$")
_PAGE_NUMBER_ONLY_RE = re.compile(r"^\s*\(?\s*(?:\d+|[ivxlcdm]+)\s*\)?\s*$", re.IGNORECASE)
_TRAILING_PAGE_MARKER_RE = re.compile(
    r"^\s*.+?(?:\||[•·\-–—])\s*(?:\d+|[ivxlcdm]+)\s*$",
    re.IGNORECASE,
)
_LEADING_PAGE_MARKER_RE = re.compile(
    r"^\s*(?:\d+|[ivxlcdm]+)\s*(?:\||[•·\-–—])\s*.+$",
    re.IGNORECASE,
)
_TABLEISH_NUMBER_RE = re.compile(r"^(?:[-+]?\d+(?:\.\d+)?%?|N/?A)$", re.IGNORECASE)
_TABLEISH_MARKER_RE = re.compile(r"^(?:O|X|✗|✓|✔)$", re.IGNORECASE)


class LocalPdfDocumentResolver:
    """Stage-2 resolver: repeated header/footer detection + page reading order."""

    def __init__(
        self,
        *,
        min_header_footer_repeat: int = 2,
        full_width_ratio: float = 0.6,
        column_cluster_threshold_ratio: float = 0.12,
        min_column_lines: int = 2,
        column_outer_center_ratio: float = 0.55,
        column_vertical_overlap_ratio: float = 0.28,
        xycut_cross_layout_beta: float = 0.85,
        xycut_density_threshold: float = 0.9,
        xycut_min_gap: float = 5.0,
        xycut_narrow_element_width_ratio: float = 0.1,
        xycut_gap_tie_threshold: float = 8.0,
    ) -> None:
        self._min_header_footer_repeat = max(2, int(min_header_footer_repeat))
        self._full_width_ratio = min(0.95, max(0.3, float(full_width_ratio)))
        self._column_cluster_threshold_ratio = min(0.3, max(0.05, float(column_cluster_threshold_ratio)))
        self._min_column_lines = max(2, int(min_column_lines))
        self._column_outer_center_ratio = min(0.7, max(0.5, float(column_outer_center_ratio)))
        self._column_vertical_overlap_ratio = min(0.8, max(0.1, float(column_vertical_overlap_ratio)))
        self._xycut_cross_layout_beta = min(1.0, max(0.5, float(xycut_cross_layout_beta)))
        self._xycut_density_threshold = min(1.0, max(0.2, float(xycut_density_threshold)))
        self._xycut_min_gap = max(1.0, float(xycut_min_gap))
        self._xycut_narrow_element_width_ratio = min(0.4, max(0.02, float(xycut_narrow_element_width_ratio)))
        self._xycut_gap_tie_threshold = max(0.0, float(xycut_gap_tie_threshold))

    def resolve_document(self, *, pages: Sequence[PdfNormalizedPage]) -> PdfResolvedDocument:
        normalized_pages = [page for page in list(pages or []) if isinstance(page, PdfNormalizedPage)]
        header_signatures = self._collect_repeated_signatures(pages=normalized_pages, band="top_band")
        footer_signatures = self._collect_repeated_signatures(pages=normalized_pages, band="bottom_band")

        resolved_pages: list[PdfResolvedPage] = []
        for page in normalized_pages:
            kept_lines: list[PdfTextLine] = []
            dropped_lines: list[dict[str, object]] = []
            for line in list(page.text_lines or []):
                signature = self._signature(line.text)
                if line.band == "top_band" and signature and signature in header_signatures:
                    dropped_lines.append(
                        {
                            "line_id": line.line_id,
                            "text": line.text,
                            "reason": "repeated_header",
                            "signature": signature,
                        }
                    )
                    continue
                if line.band == "bottom_band" and signature and signature in footer_signatures:
                    dropped_lines.append(
                        {
                            "line_id": line.line_id,
                            "text": line.text,
                            "reason": "repeated_footer",
                            "signature": signature,
                        }
                    )
                    continue
                if self._is_page_marker_line(
                    line=line,
                    page_width=float(page.meta.page_width or 0.0),
                ):
                    dropped_lines.append(
                        {
                            "line_id": line.line_id,
                            "text": line.text,
                            "reason": "page_marker",
                        }
                    )
                    continue
                kept_lines.append(line)

            layout = self._detect_column_layout(page=page, lines=kept_lines)
            if int(layout.get("column_count") or 1) == 2:
                split_lines = self._split_cross_boundary_lines(
                    page=page,
                    lines=kept_lines,
                    boundary=float(layout.get("boundary") or 0.0),
                    column_top=float(layout.get("column_top") or 0.0),
                    column_bottom=float(layout.get("column_bottom") or 0.0),
                )
                if len(split_lines) != len(kept_lines):
                    kept_lines = split_lines
                    layout = self._detect_column_layout(page=page, lines=kept_lines)
            ordered_lines = self._order_page_lines(
                page=page,
                lines=kept_lines,
                page_width=float(page.meta.page_width or 0.0),
                boundary=float(layout.get("boundary") or 0.0),
                column_count=int(layout.get("column_count") or 1),
                column_top=float(layout.get("column_top") or 0.0),
                column_bottom=float(layout.get("column_bottom") or 0.0),
            )
            resolved_pages.append(
                PdfResolvedPage(
                    meta=page.meta,
                    lines=ordered_lines,
                    dropped_lines=dropped_lines,
                    column_count=int(layout.get("column_count") or 1),
                )
            )

        return PdfResolvedDocument(
            pages=resolved_pages,
            header_signatures=sorted(header_signatures),
            footer_signatures=sorted(footer_signatures),
        )

    def _collect_repeated_signatures(
        self,
        *,
        pages: Sequence[PdfNormalizedPage],
        band: str,
    ) -> set[str]:
        counts: Counter[str] = Counter()
        for page in pages:
            seen: set[str] = set()
            for line in list(page.text_lines or []):
                if str(line.band or "") != band:
                    continue
                signature = self._signature(line.text)
                if not signature:
                    continue
                seen.add(signature)
            counts.update(seen)
        return {
            signature
            for signature, count in counts.items()
            if int(count) >= self._min_header_footer_repeat
        }

    @staticmethod
    def _signature(text: str) -> str:
        token = str(text or "").strip().lower()
        if not token:
            return ""
        token = _DIGIT_RE.sub("#", token)
        token = _SPACE_RE.sub(" ", token)
        token = _NON_WORD_EDGE_RE.sub("", token)
        return token.strip()

    def _is_page_marker_line(self, *, line: PdfTextLine, page_width: float) -> bool:
        if str(line.band or "") not in {"top_band", "bottom_band"}:
            return False
        text = _SPACE_RE.sub(" ", str(line.text or "").strip())
        if not text:
            return False
        if _PAGE_NUMBER_ONLY_RE.match(text):
            return True
        if line.bbox.width > max(120.0, page_width * 0.55):
            return False
        token_count = len(text.split())
        if token_count > 8:
            return False
        return bool(
            _TRAILING_PAGE_MARKER_RE.match(text)
            or _LEADING_PAGE_MARKER_RE.match(text)
        )

    def _split_cross_boundary_lines(
        self,
        *,
        page: PdfNormalizedPage,
        lines: Sequence[PdfTextLine],
        boundary: float,
        column_top: float,
        column_bottom: float,
    ) -> list[PdfTextLine]:
        page_width = float(page.meta.page_width or 0.0)
        if page_width <= 0.0 or boundary <= 0.0:
            return list(lines or [])
        if self._has_large_table_region(page=page):
            return list(lines or [])

        word_map = {
            str(word.word_id): word
            for word in list(page.kept_words or [])
            if str(word.word_id or "").strip()
        }
        if not word_map:
            return list(lines or [])

        output: list[PdfTextLine] = []
        for line in list(lines or []):
            split_parts = self._split_cross_boundary_line(
                line=line,
                word_map=word_map,
                page_width=page_width,
                boundary=boundary,
                column_top=column_top,
                column_bottom=column_bottom,
            )
            if split_parts:
                output.extend(split_parts)
                continue
            output.append(line)
        return output

    def _split_cross_boundary_line(
        self,
        *,
        line: PdfTextLine,
        word_map: dict[str, object],
        page_width: float,
        boundary: float,
        column_top: float,
        column_bottom: float,
    ) -> list[PdfTextLine]:
        if str(line.band or "") == "bottom_band":
            return []
        if float(line.bbox.width) < page_width * 0.55:
            return []
        if float(line.bbox.x0) >= boundary - 18.0 or float(line.bbox.x1) <= boundary + 18.0:
            return []

        line_height = max(1.0, float(line.bbox.height))
        if column_top > 0.0 and float(line.bbox.top) < column_top - line_height:
            return []
        if column_bottom > 0.0 and float(line.bbox.bottom) > column_bottom + line_height:
            return []

        words = [
            word_map[word_id]
            for word_id in list(line.word_ids or [])
            if str(word_id or "") in word_map
        ]
        if len(words) < 4:
            return []
        words = sorted(words, key=lambda item: (round(float(item.bbox.x0), 2), str(item.word_id)))

        if self._text_looks_tableish(self._stitch_words(words)):
            return []

        split_index = self._find_boundary_word_split_index(
            words=words,
            boundary=boundary,
            page_width=page_width,
            avg_font_size=float(line.avg_font_size or 0.0),
            line_height=line_height,
        )
        if split_index is None:
            return []

        left_words = words[: split_index + 1]
        right_words = words[split_index + 1 :]
        if len(left_words) < 2 or len(right_words) < 2:
            return []

        left_text = self._stitch_words(left_words)
        right_text = self._stitch_words(right_words)
        if not left_text or not right_text:
            return []
        if self._text_looks_tableish(left_text) or self._text_looks_tableish(right_text):
            return []

        left_bbox = self._merge_bboxes([word.bbox for word in left_words])
        right_bbox = self._merge_bboxes([word.bbox for word in right_words])
        if left_bbox.width >= page_width * self._full_width_ratio:
            return []
        if right_bbox.width >= page_width * self._full_width_ratio:
            return []

        return [
            PdfTextLine(
                line_id=f"{line.line_id}__left",
                page=int(line.page),
                text=left_text,
                bbox=left_bbox,
                word_ids=[str(word.word_id) for word in left_words],
                avg_font_size=float(line.avg_font_size or 0.0),
                dominant_font_name=str(line.dominant_font_name or ""),
                band=str(line.band or "body"),
            ),
            PdfTextLine(
                line_id=f"{line.line_id}__right",
                page=int(line.page),
                text=right_text,
                bbox=right_bbox,
                word_ids=[str(word.word_id) for word in right_words],
                avg_font_size=float(line.avg_font_size or 0.0),
                dominant_font_name=str(line.dominant_font_name or ""),
                band=str(line.band or "body"),
            ),
        ]

    def _find_boundary_word_split_index(
        self,
        *,
        words: Sequence[object],
        boundary: float,
        page_width: float,
        avg_font_size: float,
        line_height: float,
    ) -> int | None:
        best_index: int | None = None
        best_gap = 0.0
        gap_threshold = max(
            14.0,
            page_width * 0.025,
            avg_font_size * 1.1 if avg_font_size > 0.0 else 0.0,
            line_height * 1.0,
        )
        for index in range(len(list(words)) - 1):
            current = list(words)[index]
            candidate = list(words)[index + 1]
            right_edge = float(current.bbox.x1)
            left_edge = float(candidate.bbox.x0)
            gap = left_edge - right_edge
            if gap < gap_threshold:
                continue
            if right_edge > boundary or left_edge < boundary:
                continue
            midpoint = (right_edge + left_edge) / 2.0
            if abs(midpoint - boundary) > max(36.0, page_width * 0.08):
                continue
            if gap > best_gap:
                best_gap = gap
                best_index = index
        return best_index

    @staticmethod
    def _text_looks_tableish(text: str) -> bool:
        tokens = [
            token.strip(".,;:()[]{}")
            for token in str(text or "").split()
            if token.strip(".,;:()[]{}")
        ]
        if not tokens:
            return False
        numeric_like = sum(1 for token in tokens if _TABLEISH_NUMBER_RE.match(token))
        marker_like = sum(1 for token in tokens if _TABLEISH_MARKER_RE.match(token))
        if numeric_like >= 2 or marker_like >= 2:
            return True
        if marker_like >= 1 and len(tokens) <= 5:
            return True
        return False

    def _detect_column_layout(
        self,
        *,
        page: PdfNormalizedPage,
        lines: Sequence[PdfTextLine],
    ) -> dict[str, float | int]:
        page_width = float(page.meta.page_width or 0.0)
        candidate_lines = [
            line
            for line in list(lines or [])
            if str(line.band or "") != "bottom_band"
            and len(str(line.text or "").strip()) >= 6
            and float(line.bbox.width) < page_width * self._full_width_ratio
        ]
        if len(candidate_lines) < self._min_column_lines * 2 or page_width <= 0.0:
            return {"column_count": 1, "boundary": 0.0, "column_top": 0.0, "column_bottom": 0.0}

        threshold = max(36.0, page_width * self._column_cluster_threshold_ratio)
        clusters: list[dict[str, object]] = []
        for line in sorted(candidate_lines, key=lambda item: (item.bbox.x0, item.bbox.top, item.line_id)):
            placed = False
            for cluster in clusters:
                center = float(cluster["center"])
                if abs(float(line.bbox.x0) - center) <= threshold:
                    cluster_lines = list(cluster["lines"])
                    cluster_lines.append(line)
                    cluster["lines"] = cluster_lines
                    cluster["center"] = sum(float(item.bbox.x0) for item in cluster_lines) / len(cluster_lines)
                    placed = True
                    break
            if not placed:
                clusters.append({"center": float(line.bbox.x0), "lines": [line]})

        significant = [
            cluster
            for cluster in clusters
            if len(list(cluster["lines"])) >= self._min_column_lines
        ]
        if len(significant) < 2:
            return {"column_count": 1, "boundary": 0.0, "column_top": 0.0, "column_bottom": 0.0}
        if self._looks_multi_column_grid_layout(significant=significant, total_candidates=len(candidate_lines)):
            return {"column_count": 1, "boundary": 0.0, "column_top": 0.0, "column_bottom": 0.0}

        strongest = sorted(significant, key=lambda row: len(list(row["lines"])), reverse=True)[:2]
        strongest = sorted(strongest, key=lambda row: float(row["center"]))
        left_cluster = strongest[0]
        right_cluster = strongest[1]
        if not self._cluster_pair_looks_two_column(
            page_width=page_width,
            left_lines=list(left_cluster["lines"]),
            right_lines=list(right_cluster["lines"]),
        ):
            return {"column_count": 1, "boundary": 0.0, "column_top": 0.0, "column_bottom": 0.0}
        separation = float(right_cluster["center"]) - float(left_cluster["center"])
        if separation < max(100.0, page_width * 0.2):
            return {"column_count": 1, "boundary": 0.0, "column_top": 0.0, "column_bottom": 0.0}

        all_column_lines = [*list(left_cluster["lines"]), *list(right_cluster["lines"])]
        left_cluster_right_edge = float(
            median([float(line.bbox.x1) for line in list(left_cluster["lines"])])
        )
        right_cluster_left_edge = float(
            median([float(line.bbox.x0) for line in list(right_cluster["lines"])])
        )
        if right_cluster_left_edge > left_cluster_right_edge:
            boundary = round((left_cluster_right_edge + right_cluster_left_edge) / 2.0, 2)
        else:
            boundary = round((float(left_cluster["center"]) + float(right_cluster["center"])) / 2.0, 2)
        return {
            "column_count": 2,
            "boundary": boundary,
            "column_top": round(min(float(line.bbox.top) for line in all_column_lines), 2),
            "column_bottom": round(max(float(line.bbox.bottom) for line in all_column_lines), 2),
        }

    def _cluster_pair_looks_two_column(
        self,
        *,
        page_width: float,
        left_lines: Sequence[PdfTextLine],
        right_lines: Sequence[PdfTextLine],
    ) -> bool:
        if page_width <= 0.0 or not left_lines or not right_lines:
            return False

        left_centers = [
            (float(line.bbox.x0) + float(line.bbox.x1)) / 2.0
            for line in list(left_lines or [])
        ]
        right_centers = [
            (float(line.bbox.x0) + float(line.bbox.x1)) / 2.0
            for line in list(right_lines or [])
        ]
        left_center = float(median(left_centers))
        right_center = float(median(right_centers))
        outer_ratio = self._column_outer_center_ratio
        if left_center >= page_width * (1.0 - outer_ratio):
            return False
        if right_center <= page_width * outer_ratio:
            return False

        left_top = min(float(line.bbox.top) for line in list(left_lines or []))
        left_bottom = max(float(line.bbox.bottom) for line in list(left_lines or []))
        right_top = min(float(line.bbox.top) for line in list(right_lines or []))
        right_bottom = max(float(line.bbox.bottom) for line in list(right_lines or []))
        overlap = self._interval_overlap(left_top, left_bottom, right_top, right_bottom)
        left_span = max(1.0, left_bottom - left_top)
        right_span = max(1.0, right_bottom - right_top)
        min_required_overlap = max(
            18.0,
            min(left_span, right_span) * self._column_vertical_overlap_ratio,
        )
        if overlap < min_required_overlap:
            return False

        return True

    @staticmethod
    def _has_large_table_region(*, page: PdfNormalizedPage) -> bool:
        table_bboxes = [bbox for bbox in list(page.table_bboxes or []) if bbox.width > 0.0 and bbox.height > 0.0]
        if not table_bboxes:
            return False

        page_area = max(1.0, float(page.meta.page_width or 0.0) * float(page.meta.page_height or 0.0))
        max_table_coverage = max(
            (float(bbox.width) * float(bbox.height)) / page_area
            for bbox in table_bboxes
        )
        total_table_coverage = sum((float(bbox.width) * float(bbox.height)) / page_area for bbox in table_bboxes)
        return max_table_coverage >= 0.12 or total_table_coverage >= 0.18

    def _looks_multi_column_grid_layout(
        self,
        *,
        significant: Sequence[dict[str, object]],
        total_candidates: int,
    ) -> bool:
        if len(list(significant or [])) < 3:
            return False
        ranked = sorted(
            list(significant or []),
            key=lambda row: len(list(row["lines"])),
            reverse=True,
        )
        top_three = ranked[:3]
        min_cluster_size = max(3, self._min_column_lines + 1)
        if any(len(list(cluster["lines"])) < min_cluster_size for cluster in top_three):
            return False
        covered = sum(len(list(cluster["lines"])) for cluster in top_three)
        return covered >= max(9, int(total_candidates * 0.45))

    def _order_page_lines(
        self,
        *,
        page: PdfNormalizedPage,
        lines: Sequence[PdfTextLine],
        page_width: float,
        boundary: float,
        column_count: int,
        column_top: float,
        column_bottom: float,
    ) -> list[PdfResolvedLine]:
        if not lines:
            return []
        block_guided = self._order_page_lines_by_text_blocks(
            page=page,
            lines=lines,
            page_width=page_width,
            boundary=boundary,
            column_count=column_count,
            column_top=column_top,
            column_bottom=column_bottom,
        )
        if block_guided is not None:
            return block_guided
        cross_layout_ids = {
            line.line_id
            for line in self._identify_cross_layout_lines(list(lines or []))
        }
        ordered_specs: list[tuple[PdfTextLine, str, str]] = []
        for line in self._xy_cut_sort(list(lines or [])):
            region, column_id = self._classify_line_region(
                line=line,
                page_width=page_width,
                boundary=boundary,
                column_count=column_count,
                column_top=column_top,
                column_bottom=column_bottom,
                cross_layout_ids=cross_layout_ids,
            )
            ordered_specs.append((line, region, column_id))

        return [
            self._to_resolved_line(
                line=line,
                region=region,
                column_id=column_id,
                reading_order=index,
            )
            for index, (line, region, column_id) in enumerate(ordered_specs, start=1)
        ]

    def _order_page_lines_by_text_blocks(
        self,
        *,
        page: PdfNormalizedPage,
        lines: Sequence[PdfTextLine],
        page_width: float,
        boundary: float,
        column_count: int,
        column_top: float,
        column_bottom: float,
    ) -> list[PdfResolvedLine] | None:
        if int(column_count) != 2 or page_width <= 0.0 or boundary <= 0.0:
            return None

        text_blocks = [
            block
            for block in list(page.text_blocks or [])
            if isinstance(block, PdfTextBlockAtom)
            and str(block.text or "").strip()
        ]
        if len(text_blocks) < 2:
            return None

        band_top = float(column_top or 0.0)
        band_bottom = float(column_bottom or 0.0)
        if band_bottom <= band_top:
            return None

        column_blocks: list[tuple[PdfTextBlockAtom, str]] = []
        for block in text_blocks:
            if float(block.bbox.width) >= page_width * self._full_width_ratio:
                overlap_height = self._interval_overlap(float(block.bbox.top), float(block.bbox.bottom), band_top, band_bottom)
                if overlap_height >= max(18.0, float(block.bbox.height) * 0.5):
                    return None
                continue
            overlap_height = self._interval_overlap(float(block.bbox.top), float(block.bbox.bottom), band_top, band_bottom)
            if overlap_height < max(10.0, float(block.bbox.height) * 0.35):
                continue
            center_x = (float(block.bbox.x0) + float(block.bbox.x1)) / 2.0
            region = "left" if center_x <= boundary else "right"
            column_blocks.append((block, region))

        if not column_blocks:
            return None

        line_assignments: dict[str, str] = {}
        grouped_lines: dict[str, list[PdfTextLine]] = {}
        for line in list(lines or []):
            if float(line.bbox.bottom) < band_top or float(line.bbox.top) > band_bottom:
                continue
            best_block_id = ""
            best_score = 0.0
            for block, _region in column_blocks:
                score = self._line_block_match_score(line=line, block=block)
                if score > best_score:
                    best_score = score
                    best_block_id = str(block.block_id or "")
            if best_block_id and best_score >= 0.45:
                line_assignments[str(line.line_id)] = best_block_id
                grouped_lines.setdefault(best_block_id, []).append(line)

        if len(line_assignments) < max(4, len(list(lines or [])) // 3):
            return None

        left_blocks = [
            (block, self._sort_by_y_then_x(grouped_lines[str(block.block_id)]))
            for block, region in column_blocks
            if region == "left" and str(block.block_id or "") in grouped_lines
        ]
        right_blocks = [
            (block, self._sort_by_y_then_x(grouped_lines[str(block.block_id)]))
            for block, region in column_blocks
            if region == "right" and str(block.block_id or "") in grouped_lines
        ]
        if not left_blocks or not right_blocks:
            return None

        left_blocks.sort(key=lambda item: (round(float(item[0].bbox.top), 2), round(float(item[0].bbox.x0), 2), str(item[0].block_id or "")))
        right_blocks.sort(key=lambda item: (round(float(item[0].bbox.top), 2), round(float(item[0].bbox.x0), 2), str(item[0].block_id or "")))

        mapped_ids = set(line_assignments)
        pre_lines = [
            line
            for line in list(lines or [])
            if str(line.line_id or "") not in mapped_ids
            and float(line.bbox.bottom) <= band_top
        ]
        post_lines = [
            line
            for line in list(lines or [])
            if str(line.line_id or "") not in mapped_ids
            and float(line.bbox.top) >= band_bottom
        ]
        left_residual = [
            line
            for line in list(lines or [])
            if str(line.line_id or "") not in mapped_ids
            and band_top <= float(line.bbox.top) <= band_bottom
            and ((float(line.bbox.x0) + float(line.bbox.x1)) / 2.0) <= boundary
        ]
        right_residual = [
            line
            for line in list(lines or [])
            if str(line.line_id or "") not in mapped_ids
            and band_top <= float(line.bbox.top) <= band_bottom
            and ((float(line.bbox.x0) + float(line.bbox.x1)) / 2.0) > boundary
        ]

        ordered_lines: list[PdfTextLine] = []
        ordered_lines.extend(self._sort_by_y_then_x(pre_lines))
        for _block, block_lines in left_blocks:
            ordered_lines.extend(block_lines)
        ordered_lines.extend(self._sort_by_y_then_x(left_residual))
        for _block, block_lines in right_blocks:
            ordered_lines.extend(block_lines)
        ordered_lines.extend(self._sort_by_y_then_x(right_residual))
        ordered_lines.extend(self._sort_by_y_then_x(post_lines))

        deduped: list[PdfTextLine] = []
        seen_line_ids: set[str] = set()
        for line in ordered_lines:
            line_id = str(line.line_id or "")
            if not line_id or line_id in seen_line_ids:
                continue
            seen_line_ids.add(line_id)
            deduped.append(line)
        if len(deduped) != len(list(lines or [])):
            remaining = [
                line
                for line in list(lines or [])
                if str(line.line_id or "") not in seen_line_ids
            ]
            deduped.extend(self._sort_by_y_then_x(remaining))

        cross_layout_ids = {
            line.line_id
            for line in self._identify_cross_layout_lines(list(lines or []))
        }
        resolved: list[PdfResolvedLine] = []
        for index, line in enumerate(deduped, start=1):
            region, column_id = self._classify_line_region(
                line=line,
                page_width=page_width,
                boundary=boundary,
                column_count=column_count,
                column_top=column_top,
                column_bottom=column_bottom,
                cross_layout_ids=cross_layout_ids,
            )
            resolved.append(
                self._to_resolved_line(
                    line=line,
                    region=region,
                    column_id=column_id,
                    reading_order=index,
                )
            )
        return resolved

    @staticmethod
    def _line_block_match_score(*, line: PdfTextLine, block: PdfTextBlockAtom) -> float:
        horizontal_overlap = LocalPdfDocumentResolver._interval_overlap(
            float(line.bbox.x0),
            float(line.bbox.x1),
            float(block.bbox.x0),
            float(block.bbox.x1),
        )
        vertical_overlap = LocalPdfDocumentResolver._interval_overlap(
            float(line.bbox.top),
            float(line.bbox.bottom),
            float(block.bbox.top),
            float(block.bbox.bottom),
        )
        if horizontal_overlap <= 0.0 or vertical_overlap <= 0.0:
            return 0.0
        line_area = max(1.0, float(line.bbox.width) * float(line.bbox.height))
        overlap_area = horizontal_overlap * vertical_overlap
        overlap_ratio = overlap_area / line_area
        center_x = (float(line.bbox.x0) + float(line.bbox.x1)) / 2.0
        center_y = (float(line.bbox.top) + float(line.bbox.bottom)) / 2.0
        if not (float(block.bbox.x0) <= center_x <= float(block.bbox.x1)):
            return 0.0
        if not (float(block.bbox.top) <= center_y <= float(block.bbox.bottom)):
            return overlap_ratio * 0.5
        return overlap_ratio

    @staticmethod
    def _line_sort_key(line: PdfTextLine) -> tuple[float, float, str]:
        return (round(line.bbox.top, 2), round(line.bbox.x0, 2), str(line.line_id or ""))

    def _classify_line_region(
        self,
        *,
        line: PdfTextLine,
        page_width: float,
        boundary: float,
        column_count: int,
        column_top: float,
        column_bottom: float,
        cross_layout_ids: set[str] | None = None,
    ) -> tuple[str, str]:
        if page_width <= 0.0 or int(column_count) != 2:
            return "main", "main"

        line_width = float(line.bbox.width)
        center_x = float(line.bbox.x0) + (line_width / 2.0)
        is_full_width = (
            line_width >= page_width * self._full_width_ratio
            or str(line.line_id or "") in set(cross_layout_ids or set())
        )
        if is_full_width:
            return "full_width", "main"
        if center_x <= boundary:
            return "left_column", "left"
        return "right_column", "right"

    def _xy_cut_sort(self, lines: Sequence[PdfTextLine]) -> list[PdfTextLine]:
        valid_lines = [line for line in list(lines or []) if isinstance(line, PdfTextLine)]
        if len(valid_lines) <= 1:
            return valid_lines

        cross_layout_ids = {
            line.line_id
            for line in self._identify_cross_layout_lines(valid_lines)
        }
        cross_layout_lines = [line for line in valid_lines if line.line_id in cross_layout_ids]
        remaining_lines = [line for line in valid_lines if line.line_id not in cross_layout_ids]

        if not remaining_lines:
            return self._sort_by_y_then_x(valid_lines)

        prefer_horizontal_first = self._compute_density_ratio(remaining_lines) > self._xycut_density_threshold
        sorted_main = self._recursive_segment(
            remaining_lines,
            prefer_horizontal_first=prefer_horizontal_first,
        )
        return self._merge_cross_layout_lines(sorted_main, cross_layout_lines)

    def _identify_cross_layout_lines(self, lines: Sequence[PdfTextLine]) -> list[PdfTextLine]:
        if len(list(lines or [])) < 3:
            return []
        max_width = max(float(line.bbox.width) for line in lines)
        if max_width <= 0.0:
            return []
        threshold = max_width * self._xycut_cross_layout_beta
        cross_layout: list[PdfTextLine] = []
        for line in lines:
            if float(line.bbox.width) < threshold:
                continue
            if self._has_minimum_horizontal_overlaps(line, lines, min_count=2):
                cross_layout.append(line)
        return cross_layout

    def _has_minimum_horizontal_overlaps(
        self,
        line: PdfTextLine,
        lines: Sequence[PdfTextLine],
        *,
        min_count: int,
    ) -> bool:
        overlap_count = 0
        for other in list(lines or []):
            if other.line_id == line.line_id:
                continue
            if self._horizontal_overlap_ratio(line, other) >= 0.1:
                overlap_count += 1
                if overlap_count >= max(1, int(min_count)):
                    return True
        return False

    @staticmethod
    def _horizontal_overlap_ratio(first: PdfTextLine, second: PdfTextLine) -> float:
        overlap_left = max(float(first.bbox.x0), float(second.bbox.x0))
        overlap_right = min(float(first.bbox.x1), float(second.bbox.x1))
        overlap_width = max(0.0, overlap_right - overlap_left)
        if overlap_width <= 0.0:
            return 0.0
        smaller_width = min(float(first.bbox.width), float(second.bbox.width))
        if smaller_width <= 0.0:
            return 0.0
        return overlap_width / smaller_width

    @staticmethod
    def _interval_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
        return max(0.0, min(float(end_a), float(end_b)) - max(float(start_a), float(start_b)))

    def _compute_density_ratio(self, lines: Sequence[PdfTextLine]) -> float:
        region = self._calculate_bounding_region(lines)
        if region is None:
            return 1.0
        region_area = float(region.width) * float(region.height)
        if region_area <= 0.0:
            return 1.0
        content_area = sum(max(0.0, float(line.bbox.width)) * max(0.0, float(line.bbox.height)) for line in lines)
        return min(1.0, content_area / region_area)

    def _calculate_bounding_region(self, lines: Sequence[PdfTextLine]) -> PdfBBox | None:
        valid_lines = [line for line in list(lines or []) if isinstance(line, PdfTextLine)]
        if not valid_lines:
            return None
        return PdfBBox(
            x0=min(float(line.bbox.x0) for line in valid_lines),
            top=min(float(line.bbox.top) for line in valid_lines),
            x1=max(float(line.bbox.x1) for line in valid_lines),
            bottom=max(float(line.bbox.bottom) for line in valid_lines),
        )

    def _recursive_segment(
        self,
        lines: Sequence[PdfTextLine],
        *,
        prefer_horizontal_first: bool,
    ) -> list[PdfTextLine]:
        valid_lines = list(lines or [])
        if len(valid_lines) <= 1:
            return valid_lines

        horizontal_cut = self._find_best_horizontal_cut_with_projection(valid_lines)
        vertical_cut = self._find_best_vertical_cut_with_projection(valid_lines)

        has_horizontal_cut = horizontal_cut[1] >= self._xycut_min_gap
        has_vertical_cut = vertical_cut[1] >= self._xycut_min_gap

        if has_horizontal_cut and has_vertical_cut:
            if abs(horizontal_cut[1] - vertical_cut[1]) <= self._xycut_gap_tie_threshold:
                use_horizontal_cut = prefer_horizontal_first
            else:
                use_horizontal_cut = horizontal_cut[1] > vertical_cut[1]
        elif has_horizontal_cut:
            use_horizontal_cut = True
        elif has_vertical_cut:
            use_horizontal_cut = False
        else:
            return self._sort_by_y_then_x(valid_lines)

        if use_horizontal_cut:
            groups = self._split_by_horizontal_cut(valid_lines, cut_y=horizontal_cut[0])
        else:
            groups = self._split_by_vertical_cut(valid_lines, cut_x=vertical_cut[0])
        if len(groups) <= 1:
            return self._sort_by_y_then_x(valid_lines)

        flattened: list[PdfTextLine] = []
        for group in groups:
            flattened.extend(
                self._recursive_segment(
                    group,
                    prefer_horizontal_first=prefer_horizontal_first,
                )
            )
        return flattened

    def _find_best_vertical_cut_with_projection(self, lines: Sequence[PdfTextLine]) -> tuple[float, float]:
        if len(list(lines or [])) < 2:
            return (0.0, 0.0)

        edge_cut = self._find_vertical_cut_by_edges(lines)
        if edge_cut[1] >= self._xycut_min_gap:
            return edge_cut

        region = self._calculate_bounding_region(lines)
        if region is None:
            return edge_cut
        narrow_threshold = float(region.width) * self._xycut_narrow_element_width_ratio
        filtered = [
            line
            for line in list(lines or [])
            if float(line.bbox.width) >= narrow_threshold
        ]
        if 2 <= len(filtered) < len(list(lines or [])):
            filtered_cut = self._find_vertical_cut_by_edges(filtered)
            if filtered_cut[1] > edge_cut[1] and filtered_cut[1] >= self._xycut_min_gap:
                return filtered_cut
        return edge_cut

    def _find_vertical_cut_by_edges(self, lines: Sequence[PdfTextLine]) -> tuple[float, float]:
        sorted_lines = sorted(list(lines or []), key=lambda line: (float(line.bbox.x0), float(line.bbox.x1)))
        largest_gap = 0.0
        cut_position = 0.0
        prev_right: float | None = None
        for line in sorted_lines:
            left = float(line.bbox.x0)
            right = float(line.bbox.x1)
            if prev_right is not None and left > prev_right:
                gap = left - prev_right
                if gap > largest_gap:
                    largest_gap = gap
                    cut_position = (prev_right + left) / 2.0
            prev_right = right if prev_right is None else max(prev_right, right)
        return (cut_position, largest_gap)

    def _find_best_horizontal_cut_with_projection(self, lines: Sequence[PdfTextLine]) -> tuple[float, float]:
        sorted_lines = sorted(list(lines or []), key=lambda line: (float(line.bbox.top), float(line.bbox.bottom)))
        largest_gap = 0.0
        cut_position = 0.0
        prev_bottom: float | None = None
        for line in sorted_lines:
            top = float(line.bbox.top)
            bottom = float(line.bbox.bottom)
            if prev_bottom is not None and top > prev_bottom:
                gap = top - prev_bottom
                if gap > largest_gap:
                    largest_gap = gap
                    cut_position = (prev_bottom + top) / 2.0
            prev_bottom = bottom if prev_bottom is None else max(prev_bottom, bottom)
        return (cut_position, largest_gap)

    def _split_by_horizontal_cut(
        self,
        lines: Sequence[PdfTextLine],
        *,
        cut_y: float,
    ) -> list[list[PdfTextLine]]:
        above: list[PdfTextLine] = []
        below: list[PdfTextLine] = []
        for line in list(lines or []):
            center_y = float(line.bbox.top) + (float(line.bbox.height) / 2.0)
            if center_y < cut_y:
                above.append(line)
            else:
                below.append(line)
        return [group for group in (above, below) if group]

    def _split_by_vertical_cut(
        self,
        lines: Sequence[PdfTextLine],
        *,
        cut_x: float,
    ) -> list[list[PdfTextLine]]:
        left: list[PdfTextLine] = []
        right: list[PdfTextLine] = []
        for line in list(lines or []):
            center_x = float(line.bbox.x0) + (float(line.bbox.width) / 2.0)
            if center_x < cut_x:
                left.append(line)
            else:
                right.append(line)
        return [group for group in (left, right) if group]

    def _merge_cross_layout_lines(
        self,
        sorted_main: Sequence[PdfTextLine],
        cross_layout_lines: Sequence[PdfTextLine],
    ) -> list[PdfTextLine]:
        if not cross_layout_lines:
            return list(sorted_main or [])
        if not sorted_main:
            return self._sort_by_y_then_x(cross_layout_lines)

        main_lines = list(sorted_main or [])
        cross_lines = self._sort_by_y_then_x(cross_layout_lines)
        return self._merge_cross_layout_partitioned(main_lines, cross_lines)

    def _merge_cross_layout_partitioned(
        self,
        main_lines: Sequence[PdfTextLine],
        cross_lines: Sequence[PdfTextLine],
    ) -> list[PdfTextLine]:
        if not cross_lines:
            return list(main_lines or [])
        if not main_lines:
            return list(cross_lines or [])

        cross_line = list(cross_lines)[0]
        remaining_cross_lines = list(cross_lines)[1:]
        above = [
            line
            for line in list(main_lines or [])
            if float(line.bbox.top) < float(cross_line.bbox.top)
        ]
        below = [
            line
            for line in list(main_lines or [])
            if float(line.bbox.top) >= float(cross_line.bbox.top)
        ]
        result: list[PdfTextLine] = []
        result.extend(self._merge_cross_layout_partitioned(above, []))
        result.append(cross_line)
        result.extend(self._merge_cross_layout_partitioned(below, remaining_cross_lines))
        return result

    def _sort_by_y_then_x(self, lines: Sequence[PdfTextLine]) -> list[PdfTextLine]:
        return sorted(list(lines or []), key=self._line_sort_key)

    @staticmethod
    def _merge_bboxes(boxes: Sequence[PdfBBox]) -> PdfBBox:
        valid_boxes = list(boxes or [])
        if not valid_boxes:
            return PdfBBox(x0=0.0, top=0.0, x1=0.0, bottom=0.0)
        return PdfBBox(
            x0=min(float(item.x0) for item in valid_boxes),
            top=min(float(item.top) for item in valid_boxes),
            x1=max(float(item.x1) for item in valid_boxes),
            bottom=max(float(item.bottom) for item in valid_boxes),
        )

    @staticmethod
    def _stitch_words(words: Sequence[object]) -> str:
        text = " ".join(str(getattr(item, "text", "") or "").strip() for item in list(words or []) if str(getattr(item, "text", "") or "").strip())
        text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
        text = _SPACE_AFTER_OPEN_RE.sub(r"\1", text)
        return text.strip()

    @staticmethod
    def _to_resolved_line(
        *,
        line: PdfTextLine,
        region: str,
        column_id: str,
        reading_order: int,
    ) -> PdfResolvedLine:
        role = None
        if str(line.band or "") == "top_band":
            role = "header_candidate"
        elif str(line.band or "") == "bottom_band":
            role = "footer_candidate"
        return PdfResolvedLine(
            line_id=line.line_id,
            page=int(line.page),
            text=line.text,
            bbox=PdfBBox(
                x0=float(line.bbox.x0),
                top=float(line.bbox.top),
                x1=float(line.bbox.x1),
                bottom=float(line.bbox.bottom),
            ),
            word_ids=list(line.word_ids or []),
            avg_font_size=float(line.avg_font_size or 0.0),
            dominant_font_name=str(line.dominant_font_name or ""),
            band=str(line.band or "body"),
            region=str(region or "main"),
            column_id=str(column_id or "main"),
            reading_order=max(1, int(reading_order)),
            header_footer_role=role,
        )
