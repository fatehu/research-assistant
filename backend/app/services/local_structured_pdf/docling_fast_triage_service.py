from __future__ import annotations

from collections.abc import Sequence

from .contracts import (
    PdfBBox,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfStructuredDocument,
    PdfStructuredPage,
    PdfWordAtom,
)
from .page_triage_service import LocalPdfPageTriageService


class LocalPdfDoclingFastTriageService(LocalPdfPageTriageService):
    """Conservative, Java-style triage for docling-fast hybrid routing."""

    _BASELINE_EPSILON = 0.1
    _MIN_LINE_COUNT_FOR_TABLE = 8
    _MIN_GRID_LINES = 3
    _MIN_ROW_SEPARATOR_PATTERN = 5
    _MIN_LINE_ART_FOR_TABLE = 8
    _LINE_LENGTH_TOLERANCE = 0.05
    _MIN_ALIGNED_SHORT_LINES = 2
    _MIN_CONSECUTIVE_PATTERNS = 2
    _HIGH_PATTERN_COUNT_THRESHOLD = 30
    _MIN_TABLE_PATTERNS = 3
    _MIN_PATTERN_DENSITY = 0.10
    _MIN_PATTERNS_FOR_DENSITY = 2
    _MULTI_COLUMN_X_SHIFT_RATIO = 2.0
    _X_DIFFERENCE_EPSILON = 1.5

    def __init__(self) -> None:
        super().__init__()
        self._replacement_char_ratio_threshold = 0.30
        self._line_ratio_threshold = 0.30
        self._large_image_ratio_threshold = 0.11
        self._large_image_aspect_ratio_threshold = 1.75
        self._vector_line_threshold = 8
        self._table_area_ratio_threshold = 0.04
        self._average_words_per_line_threshold = 4.0
        self._minimum_text_lines_for_pattern = 4
        self._table_border_axis_threshold = 2
        self._table_border_total_threshold = 6
        self._table_border_distinct_axis_threshold = 3
        self._table_border_max_thickness = 6.0
        self._table_border_min_span = 18.0
        self._table_border_text_line_threshold = 24
        self._table_border_average_words_threshold = 6.0

    def triage_document(
        self,
        *,
        page_atoms,
        normalized_pages,
        resolved_document,
        structured_document,
        mode: str = "auto",
    ) -> PdfHybridTriageDocument:
        normalized_mode = self._normalize_mode(mode)
        atoms_by_page = {int(item.page): item for item in list(page_atoms or [])}
        normalized_by_page = {int(item.page): item for item in list(normalized_pages or [])}
        resolved_by_page = {
            int(item.page): item
            for item in list(getattr(resolved_document, "pages", []) or [])
            if isinstance(item, PdfResolvedPage)
        }
        structured_by_page = {
            int(item.page): item
            for item in list(getattr(structured_document, "pages", []) or [])
            if isinstance(item, PdfStructuredPage)
        }
        ordered_pages = sorted(
            {
                *atoms_by_page.keys(),
                *normalized_by_page.keys(),
                *resolved_by_page.keys(),
                *structured_by_page.keys(),
            }
        )
        return PdfHybridTriageDocument(
            mode=normalized_mode,
            pages=[
                self.triage_page(
                    page_atoms=atoms_by_page.get(page_number),
                    normalized_page=normalized_by_page.get(page_number),
                    resolved_page=resolved_by_page.get(page_number),
                    structured_page=structured_by_page.get(page_number),
                    mode=normalized_mode,
                )
                for page_number in ordered_pages
            ],
        )

    def triage_page(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        normalized_page: PdfNormalizedPage | None,
        resolved_page: PdfResolvedPage | None,
        structured_page: PdfStructuredPage | None,
        mode: str = "auto",
    ) -> PdfHybridTriageResult:
        normalized_mode = self._normalize_mode(mode)
        page_number = self._page_number(
            page_atoms=page_atoms,
            normalized_page=normalized_page,
            resolved_page=resolved_page,
            structured_page=structured_page,
        )
        signals = self._extract_signals(
            page_atoms=page_atoms,
            normalized_page=normalized_page,
            resolved_page=resolved_page,
            structured_page=structured_page,
        )
        page_type = self._classify_page_type(signals=signals)
        if normalized_mode == "full":
            return PdfHybridTriageResult(
                page=page_number,
                page_type=page_type,
                decision="backend",
                confidence=1.0,
                reasons=["hybrid_full_mode"],
                signals=signals,
            )

        decision, confidence, reasons = self._classify_java_style(
            page_atoms=page_atoms,
            signals=signals,
        )
        return PdfHybridTriageResult(
            page=page_number,
            page_type=page_type,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            signals=signals,
        )

    def _classify_java_style(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        signals: PdfHybridTriageSignals,
    ) -> tuple[str, float, list[str]]:
        if signals.replacement_char_ratio >= self._replacement_char_ratio_threshold:
            return "backend", 1.0, ["java_triage:replacement_char_ratio"]

        if self._has_vector_table_signal(signals=signals):
            return "backend", 0.95, ["java_triage:vector_table_signal"]

        if self._has_text_table_pattern(signals=signals):
            return "backend", 0.90, ["java_triage:text_table_pattern"]

        if self._has_mixed_visual_table_signal(signals=signals):
            return "backend", 0.88, ["python_triage:mixed_visual_table_signal"]

        if self._has_large_image(page_atoms=page_atoms, signals=signals):
            return "backend", 0.85, ["java_triage:large_image"]

        if self._line_to_text_ratio(signals=signals) > self._line_ratio_threshold:
            return "backend", 0.80, ["java_triage:line_ratio"]

        return "local", 0.90, ["java_triage:default_local"]

    def _has_vector_table_signal(self, *, signals: PdfHybridTriageSignals) -> bool:
        if bool(signals.has_grid_lines):
            return True
        if bool(signals.has_table_border_lines):
            return True
        if int(signals.line_art_count or 0) >= self._MIN_LINE_ART_FOR_TABLE:
            return True
        if bool(signals.has_row_separator_pattern):
            return True
        if bool(signals.has_aligned_short_lines):
            return True
        return False

    def _extract_signals(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        normalized_page: PdfNormalizedPage | None,
        resolved_page: PdfResolvedPage | None,
        structured_page: PdfStructuredPage | None,
    ) -> PdfHybridTriageSignals:
        base = super()._extract_signals(
            page_atoms=page_atoms,
            normalized_page=normalized_page,
            resolved_page=resolved_page,
            structured_page=structured_page,
        )
        word_chunks = self._select_filtered_text_chunks(
            resolved_page=resolved_page,
            normalized_page=normalized_page,
        )
        vector_metrics = self._extract_vector_metrics(
            page_atoms=page_atoms,
            text_chunks=word_chunks,
        )
        text_pattern_metrics = self._extract_text_pattern_metrics(word_chunks=word_chunks)
        filtered_lines = self._select_filtered_text_lines(
            resolved_page=resolved_page,
            normalized_page=normalized_page,
        )

        text_line_count = len(filtered_lines)
        average_words_per_line = round(
            sum(self._line_word_count(item) for item in filtered_lines) / max(1, text_line_count),
            2,
        )
        top_band_count = sum(1 for item in filtered_lines if str(getattr(item, "band", "") or "") == "top_band")
        bottom_band_count = sum(1 for item in filtered_lines if str(getattr(item, "band", "") or "") == "bottom_band")
        return PdfHybridTriageSignals(
            text_line_count=text_line_count,
            text_chunk_count=len(word_chunks),
            text_block_count=int(base.text_block_count or 0),
            structured_block_count=int(base.structured_block_count or 0),
            heading_count=int(base.heading_count or 0),
            table_count=int(base.table_count or 0),
            equation_count=int(base.equation_count or 0),
            image_count=int(base.image_count or 0),
            vector_line_count=int(vector_metrics["line_chunk_count"]),
            horizontal_line_count=int(vector_metrics["horizontal_line_count"]),
            vertical_line_count=int(vector_metrics["vertical_line_count"]),
            line_art_count=int(vector_metrics["line_art_count"]),
            rect_count=int(base.rect_count or 0),
            curve_count=int(base.curve_count or 0),
            top_band_count=top_band_count,
            bottom_band_count=bottom_band_count,
            average_words_per_line=average_words_per_line,
            image_area_ratio=float(base.image_area_ratio or 0.0),
            largest_image_ratio=float(base.largest_image_ratio or 0.0),
            table_area_ratio=float(base.table_area_ratio or 0.0),
            replacement_char_ratio=float(base.replacement_char_ratio or 0.0),
            table_pattern_count=int(text_pattern_metrics["table_pattern_count"]),
            max_consecutive_streak=int(text_pattern_metrics["max_consecutive_streak"]),
            pattern_density=float(text_pattern_metrics["pattern_density"]),
            has_consecutive_patterns=bool(text_pattern_metrics["has_consecutive_patterns"]),
            has_grid_lines=bool(vector_metrics["has_grid_lines"]),
            has_table_border_lines=bool(vector_metrics["has_table_border_lines"]),
            has_row_separator_pattern=bool(vector_metrics["has_row_separator_pattern"]),
            has_aligned_short_lines=bool(vector_metrics["has_aligned_short_lines"]),
            double_column=bool(base.double_column),
            has_struct_tree=bool(base.has_struct_tree),
        )

    def _select_filtered_text_lines(
        self,
        *,
        resolved_page: PdfResolvedPage | None,
        normalized_page: PdfNormalizedPage | None,
    ) -> list[PdfResolvedLine | object]:
        resolved_lines = [
            line
            for line in list(getattr(resolved_page, "lines", []) or [])
            if str(getattr(line, "text", "") or "").strip()
            and not str(getattr(line, "header_footer_role", "") or "").strip()
        ]
        if resolved_lines:
            return sorted(
                resolved_lines,
                key=lambda item: (
                    int(getattr(item, "reading_order", 0) or 0),
                    round(float(item.bbox.top), 2),
                    round(float(item.bbox.x0), 2),
                    str(getattr(item, "line_id", "")),
                ),
            )
        normalized_lines = [
            line
            for line in list(getattr(normalized_page, "text_lines", []) or [])
            if str(getattr(line, "text", "") or "").strip()
        ]
        return sorted(
            normalized_lines,
            key=lambda item: (
                round(float(item.bbox.top), 2),
                round(float(item.bbox.x0), 2),
                str(getattr(item, "line_id", "")),
            ),
        )

    def _select_filtered_text_chunks(
        self,
        *,
        resolved_page: PdfResolvedPage | None,
        normalized_page: PdfNormalizedPage | None,
    ) -> list[PdfWordAtom]:
        words = [
            word
            for word in list(getattr(normalized_page, "kept_words", []) or [])
            if str(getattr(word, "text", "") or "").strip()
        ]
        if not words:
            return []
        resolved_lines = [
            line
            for line in list(getattr(resolved_page, "lines", []) or [])
            if str(getattr(line, "text", "") or "").strip()
            and not str(getattr(line, "header_footer_role", "") or "").strip()
        ]
        if resolved_lines:
            filtered_words = [
                word
                for word in words
                if self._word_matches_any_line(word=word, lines=resolved_lines)
            ]
            if filtered_words:
                words = filtered_words
        words = sorted(
            words,
            key=lambda item: (
                round(float(item.bbox.top), 2),
                round(float(item.bbox.x0), 2),
                round(float(item.bbox.bottom), 2),
                str(item.word_id),
            ),
        )
        return self._merge_words_into_text_chunks(words)

    @staticmethod
    def _line_word_count(line: object) -> int:
        word_ids = list(getattr(line, "word_ids", []) or [])
        if word_ids:
            return len(word_ids)
        text = str(getattr(line, "text", "") or "").strip()
        if not text:
            return 0
        return len([token for token in text.split() if token])

    def _has_text_table_pattern(self, *, signals: PdfHybridTriageSignals) -> bool:
        table_pattern_count = int(signals.table_pattern_count or 0)
        pattern_density = float(signals.pattern_density or 0.0)
        has_high_pattern_count = table_pattern_count >= self._HIGH_PATTERN_COUNT_THRESHOLD
        meets_pattern_threshold = (
            table_pattern_count >= self._MIN_TABLE_PATTERNS
            or (
                pattern_density >= self._MIN_PATTERN_DENSITY
                and table_pattern_count >= self._MIN_PATTERNS_FOR_DENSITY
            )
        )
        return (bool(signals.has_consecutive_patterns) or has_high_pattern_count) and meets_pattern_threshold

    def _has_large_image(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        signals: PdfHybridTriageSignals,
    ) -> bool:
        if float(signals.largest_image_ratio or 0.0) < self._large_image_ratio_threshold:
            return False
        images = list(getattr(page_atoms, "images", []) or [])
        if not images:
            return False
        largest = max(images, key=lambda item: float(item.bbox.width) * float(item.bbox.height))
        height = max(1e-6, float(largest.bbox.height))
        aspect_ratio = float(largest.bbox.width) / height
        return aspect_ratio >= self._large_image_aspect_ratio_threshold

    @staticmethod
    def _has_mixed_visual_table_signal(*, signals: PdfHybridTriageSignals) -> bool:
        if not bool(signals.double_column):
            return False
        if int(signals.image_count or 0) < 1:
            return False
        if int(signals.heading_count or 0) < 1:
            return False
        if int(signals.table_pattern_count or 0) < 1:
            return False
        if not (1 <= int(signals.line_art_count or 0) < 8):
            return False
        largest_image_ratio = float(signals.largest_image_ratio or 0.0)
        if not (0.08 <= largest_image_ratio <= 0.20):
            return False
        if float(signals.average_words_per_line or 0.0) < 6.0:
            return False
        return True

    def _extract_vector_metrics(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        text_chunks: Sequence[PdfWordAtom] | None = None,
    ) -> dict[str, int | bool]:
        if page_atoms is None:
            return {
                "line_chunk_count": 0,
                "horizontal_line_count": 0,
                "vertical_line_count": 0,
                "line_art_count": 0,
                "has_grid_lines": False,
                "has_table_border_lines": False,
                "has_row_separator_pattern": False,
                "has_aligned_short_lines": False,
            }
        horizontal_lines: list[PdfBBox] = []
        vertical_lines: list[PdfBBox] = []
        line_bboxes = [
            item.bbox
            for item in list(getattr(page_atoms, "lines", []) or [])
            if getattr(item, "bbox", None) is not None
        ]
        for bbox in line_bboxes:
            width = max(0.0, float(bbox.width))
            height = max(0.0, float(bbox.height))
            if width <= 0.0 and height <= 0.0:
                continue
            if width > (height * 3.0):
                horizontal_lines.append(bbox)
            elif height > (width * 3.0):
                vertical_lines.append(bbox)
        horizontal_count = len(horizontal_lines)
        vertical_count = len(vertical_lines)
        line_art_count = len(list(getattr(page_atoms, "rects", []) or [])) + len(list(getattr(page_atoms, "curves", []) or []))
        row_separator_pattern_count = self._count_row_separator_patterns(
            text_chunks=text_chunks,
            horizontal_lines=horizontal_lines,
        )
        return {
            "line_chunk_count": len(line_bboxes),
            "horizontal_line_count": horizontal_count,
            "vertical_line_count": vertical_count,
            "line_art_count": line_art_count,
            "has_grid_lines": horizontal_count >= self._MIN_GRID_LINES and vertical_count >= self._MIN_GRID_LINES,
            "has_table_border_lines": (horizontal_count + vertical_count) >= self._MIN_LINE_COUNT_FOR_TABLE,
            "has_row_separator_pattern": row_separator_pattern_count >= self._MIN_ROW_SEPARATOR_PATTERN,
            "has_aligned_short_lines": self._has_aligned_short_horizontal_lines(horizontal_lines),
        }

    def _extract_text_pattern_metrics(self, *, word_chunks: Sequence[PdfWordAtom]) -> dict[str, int | float | bool]:
        previous: PdfWordAtom | None = None
        table_pattern_count = 0
        current_consecutive_streak = 0
        max_consecutive_streak = 0
        non_whitespace_text_count = 0
        for current in list(word_chunks or []):
            if not str(current.text or "").strip():
                continue
            non_whitespace_text_count += 1
            if previous is not None:
                if self._are_suspicious_word_chunks(previous=previous, current=current):
                    table_pattern_count += 1
                    current_consecutive_streak += 1
                    max_consecutive_streak = max(max_consecutive_streak, current_consecutive_streak)
                else:
                    current_consecutive_streak = 0
            previous = current
        pattern_density = (
            float(table_pattern_count) / float(non_whitespace_text_count)
            if non_whitespace_text_count > 0
            else 0.0
        )
        return {
            "table_pattern_count": table_pattern_count,
            "max_consecutive_streak": max_consecutive_streak,
            "pattern_density": pattern_density,
            "has_consecutive_patterns": max_consecutive_streak >= self._MIN_CONSECUTIVE_PATTERNS,
        }

    def _merge_words_into_text_chunks(self, words: Sequence[PdfWordAtom]) -> list[PdfWordAtom]:
        chunks: list[PdfWordAtom] = []
        current_words: list[PdfWordAtom] = []
        for word in list(words or []):
            if not current_words:
                current_words = [word]
                continue
            previous = current_words[-1]
            previous_height = max(1e-6, float(previous.bbox.height))
            current_height = max(1e-6, float(word.bbox.height))
            avg_height = (previous_height + current_height) / 2.0
            baseline_diff = abs(float(previous.bbox.bottom) - float(word.bbox.bottom))
            horizontal_gap = float(word.bbox.x0) - float(previous.bbox.x1)
            same_chunk = (
                baseline_diff <= avg_height * self._BASELINE_EPSILON
                and horizontal_gap <= avg_height * self._X_DIFFERENCE_EPSILON
            )
            if same_chunk:
                current_words.append(word)
                continue
            chunks.append(self._coalesce_chunk(current_words))
            current_words = [word]
        if current_words:
            chunks.append(self._coalesce_chunk(current_words))
        return chunks

    @staticmethod
    def _coalesce_chunk(words: Sequence[PdfWordAtom]) -> PdfWordAtom:
        first = words[0]
        return PdfWordAtom(
            word_id="|".join(str(item.word_id) for item in words),
            text=" ".join(str(item.text or "").strip() for item in words if str(item.text or "").strip()),
            bbox=PdfBBox(
                x0=min(float(item.bbox.x0) for item in words),
                top=min(float(item.bbox.top) for item in words),
                x1=max(float(item.bbox.x1) for item in words),
                bottom=max(float(item.bbox.bottom) for item in words),
            ),
            doctop=float(getattr(first, "doctop", first.bbox.top) or first.bbox.top),
            font_name=str(getattr(first, "font_name", "") or ""),
            font_size=float(getattr(first, "font_size", 0.0) or 0.0),
            start_char_id=str(getattr(first, "start_char_id", "") or ""),
            end_char_id=str(getattr(words[-1], "end_char_id", "") or ""),
        )

    @staticmethod
    def _word_matches_any_line(*, word: PdfWordAtom, lines: Sequence[PdfResolvedLine]) -> bool:
        center_x = (float(word.bbox.x0) + float(word.bbox.x1)) / 2.0
        center_y = (float(word.bbox.top) + float(word.bbox.bottom)) / 2.0
        for line in list(lines or []):
            if (
                float(line.bbox.x0) <= center_x <= float(line.bbox.x1)
                and float(line.bbox.top) <= center_y <= float(line.bbox.bottom)
            ):
                return True
        return False

    def _are_suspicious_word_chunks(self, *, previous: PdfWordAtom, current: PdfWordAtom) -> bool:
        previous_height = max(1e-6, float(previous.bbox.height))
        current_height = max(1e-6, float(current.bbox.height))
        avg_height = (previous_height + current_height) / 2.0
        baseline_diff = abs(float(previous.bbox.bottom) - float(current.bbox.bottom))
        # Upstream Java uses a coordinate system where larger Y is visually higher.
        # In our top-down coordinates, "text going backwards" is current.bottom < previous.top.
        if float(current.bbox.bottom) < float(previous.bbox.top):
            x_shift = float(previous.bbox.x0) - float(current.bbox.x0)
            text_width = max(1e-6, float(previous.bbox.width))
            if x_shift > text_width * self._MULTI_COLUMN_X_SHIFT_RATIO:
                return False
            return True
        if baseline_diff <= avg_height * self._BASELINE_EPSILON:
            horizontal_gap = float(current.bbox.x0) - float(previous.bbox.x1)
            return horizontal_gap > current_height * self._X_DIFFERENCE_EPSILON
        return False

    def _has_aligned_short_horizontal_lines(self, horizontal_lines: Sequence[PdfBBox]) -> bool:
        if len(list(horizontal_lines or [])) < self._MIN_ALIGNED_SHORT_LINES:
            return False
        line_specs = [
            (float(item.x0), float(item.width))
            for item in list(horizontal_lines or [])
            if float(item.width) > 0.0
        ]
        for index, (ref_left_x, ref_length) in enumerate(line_specs):
            match_count = 1
            for left_x, length in line_specs[index + 1:]:
                max_length = max(ref_length, length, 1e-6)
                x_matches = abs(ref_left_x - left_x) / max_length <= self._LINE_LENGTH_TOLERANCE
                len_matches = abs(ref_length - length) / max_length <= self._LINE_LENGTH_TOLERANCE
                if x_matches and len_matches:
                    match_count += 1
                    if match_count >= self._MIN_ALIGNED_SHORT_LINES:
                        return True
        return False

    @staticmethod
    def _count_row_separator_patterns(
        *,
        text_chunks: Sequence[PdfWordAtom] | None,
        horizontal_lines: Sequence[PdfBBox] | None,
    ) -> int:
        items: list[tuple[float, float, str]] = []
        for chunk in list(text_chunks or []):
            text = str(getattr(chunk, "text", "") or "").strip()
            if not text:
                continue
            items.append((float(chunk.bbox.top), float(chunk.bbox.x0), "text"))
        for bbox in list(horizontal_lines or []):
            items.append((float(bbox.top), float(bbox.x0), "line"))
        items.sort(key=lambda item: (round(item[0], 2), round(item[1], 2), 0 if item[2] == "text" else 1))

        count = 0
        last_was_horizontal = False
        for _, _, kind in items:
            if kind == "line":
                if not last_was_horizontal:
                    count += 1
                last_was_horizontal = True
                continue
            last_was_horizontal = False
        return count

    @staticmethod
    def _line_to_text_ratio(*, signals: PdfHybridTriageSignals) -> float:
        vector_lines = float(signals.vector_line_count or 0)
        text_lines = float(signals.text_chunk_count or signals.text_line_count or 0)
        total = vector_lines + text_lines
        if total <= 0.0:
            return 0.0
        return vector_lines / total
