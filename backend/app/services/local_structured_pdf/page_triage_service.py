from __future__ import annotations

import re
from typing import Sequence

try:
    from .contracts import (
        PdfBBox,
        PdfHybridTriageDocument,
        PdfHybridTriageResult,
        PdfHybridTriageSignals,
        PdfNormalizedPage,
        PdfPageAtoms,
        PdfResolvedDocument,
        PdfResolvedPage,
        PdfSemanticBlock,
        PdfStructuredDocument,
        PdfStructuredPage,
    )
except ImportError:  # pragma: no cover
    # Allows importing this module directly in unit tests without importing the package __init__.
    from contracts import (  # type: ignore
        PdfBBox,
        PdfHybridTriageDocument,
        PdfHybridTriageResult,
        PdfHybridTriageSignals,
        PdfNormalizedPage,
        PdfPageAtoms,
        PdfResolvedDocument,
        PdfResolvedPage,
        PdfSemanticBlock,
        PdfStructuredDocument,
        PdfStructuredPage,
    )


_CID_RE = re.compile(r"\(cid:\d+\)", re.IGNORECASE)


class LocalPdfPageTriageService:
    """Route pages to the local or hybrid backend path using structural signals."""

    def __init__(
        self,
        *,
        replacement_char_ratio_threshold: float = 0.18,
        large_image_ratio_threshold: float = 0.11,
        total_image_ratio_threshold: float = 0.22,
        dense_table_area_threshold: float = 0.08,
        table_vector_line_threshold: int = 8,
        formula_block_threshold: int = 2,
        front_matter_band_threshold: int = 4,
    ) -> None:
        self._replacement_char_ratio_threshold = max(0.05, float(replacement_char_ratio_threshold))
        self._large_image_ratio_threshold = max(0.05, float(large_image_ratio_threshold))
        self._total_image_ratio_threshold = max(
            self._large_image_ratio_threshold,
            float(total_image_ratio_threshold),
        )
        self._dense_table_area_threshold = max(0.02, float(dense_table_area_threshold))
        self._table_vector_line_threshold = max(4, int(table_vector_line_threshold))
        self._formula_block_threshold = max(1, int(formula_block_threshold))
        self._front_matter_band_threshold = max(3, int(front_matter_band_threshold))

    def triage_document(
        self,
        *,
        page_atoms: Sequence[PdfPageAtoms],
        normalized_pages: Sequence[PdfNormalizedPage],
        resolved_document: PdfResolvedDocument,
        structured_document: PdfStructuredDocument,
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
        reasons = self._build_reasons(page_type=page_type, signals=signals)
        if normalized_mode == "full":
            return PdfHybridTriageResult(
                page=page_number,
                page_type=page_type,
                decision="backend",
                confidence=max(0.88, self._backend_confidence(page_type=page_type, signals=signals)),
                reasons=["hybrid_full_mode", *reasons],
                signals=signals,
            )

        decision = "backend" if self._has_backend_signal(page_type=page_type, signals=signals) else "local"
        confidence = (
            self._local_confidence(page_type=page_type, signals=signals)
            if decision == "local"
            else self._backend_confidence(page_type=page_type, signals=signals)
        )
        return PdfHybridTriageResult(
            page=page_number,
            page_type=page_type,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            signals=signals,
        )

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        token = str(mode or "auto").strip().lower()
        if token not in {"auto", "full"}:
            return "auto"
        return token

    @staticmethod
    def _page_number(
        *,
        page_atoms: PdfPageAtoms | None,
        normalized_page: PdfNormalizedPage | None,
        resolved_page: PdfResolvedPage | None,
        structured_page: PdfStructuredPage | None,
    ) -> int:
        for item in (page_atoms, normalized_page, resolved_page, structured_page):
            if item is not None:
                return int(getattr(item, "page", 0) or 0)
        return 0

    def _extract_signals(
        self,
        *,
        page_atoms: PdfPageAtoms | None,
        normalized_page: PdfNormalizedPage | None,
        resolved_page: PdfResolvedPage | None,
        structured_page: PdfStructuredPage | None,
    ) -> PdfHybridTriageSignals:
        page_area = max(1.0, self._page_area(page_atoms=page_atoms, normalized_page=normalized_page))
        normalized_lines = list(getattr(normalized_page, "text_lines", []) or [])
        structured_blocks = list(getattr(structured_page, "blocks", []) or [])
        line_count = len(list(getattr(page_atoms, "lines", []) or [])) or int(getattr(page_atoms, "coarse_line_count", 0) or 0)
        rect_count = len(list(getattr(page_atoms, "rects", []) or [])) or int(getattr(page_atoms, "coarse_rect_count", 0) or 0)
        curve_count = len(list(getattr(page_atoms, "curves", []) or [])) or int(getattr(page_atoms, "coarse_curve_count", 0) or 0)
        image_ratios = [
            self._bbox_area(item.bbox) / page_area
            for item in list(getattr(page_atoms, "images", []) or [])
            if self._bbox_area(item.bbox) > 0.0
        ]
        table_area_ratio = max(
            self._bbox_collection_area(list(getattr(page_atoms, "tables", []) or []), page_area=page_area),
            self._bbox_collection_area(list(getattr(normalized_page, "table_bboxes", []) or []), page_area=page_area),
        )
        average_words_per_line = (
            sum(len(list(line.word_ids or [])) for line in normalized_lines) / len(normalized_lines)
            if normalized_lines
            else 0.0
        )
        return PdfHybridTriageSignals(
            text_line_count=len(normalized_lines),
            text_block_count=len(list(getattr(normalized_page, "text_blocks", []) or [])),
            structured_block_count=len(structured_blocks),
            heading_count=sum(1 for block in structured_blocks if str(block.block_type or "") == "heading"),
            table_count=max(
                len(list(getattr(page_atoms, "tables", []) or [])),
                len(list(getattr(normalized_page, "table_bboxes", []) or [])),
            ),
            equation_count=sum(1 for block in structured_blocks if str(block.block_type or "") == "equation"),
            image_count=len(list(getattr(page_atoms, "images", []) or [])),
            vector_line_count=(line_count + rect_count + curve_count),
            rect_count=rect_count,
            curve_count=curve_count,
            top_band_count=sum(1 for line in normalized_lines if str(line.band or "") == "top_band"),
            bottom_band_count=sum(1 for line in normalized_lines if str(line.band or "") == "bottom_band"),
            average_words_per_line=round(float(average_words_per_line), 2),
            image_area_ratio=round(sum(image_ratios), 4),
            largest_image_ratio=round(max(image_ratios) if image_ratios else 0.0, 4),
            table_area_ratio=round(float(table_area_ratio), 4),
            replacement_char_ratio=round(
                self._replacement_char_ratio(getattr(page_atoms, "extract_text_raw", "") or ""),
                4,
            ),
            double_column=bool(int(getattr(resolved_page, "column_count", 1) or 1) >= 2),
            has_struct_tree=bool(getattr(page_atoms, "has_struct_tree", False)),
        )

    def _classify_page_type(self, *, signals: PdfHybridTriageSignals) -> str:
        if signals.replacement_char_ratio >= self._replacement_char_ratio_threshold:
            return "visual_or_scanned"
        if self._looks_visual_or_scanned(signals=signals):
            return "visual_or_scanned"
        if self._looks_dense_table(signals=signals):
            return "dense_table"
        if self._looks_sparse_form(signals=signals):
            return "sparse_form"
        if self._looks_formula_or_display_heavy(signals=signals):
            return "formula_or_display_heavy"
        if self._looks_mixed_layout(signals=signals):
            return "mixed_layout"
        if self._looks_front_matter_heavy(signals=signals):
            return "front_matter_heavy"
        return "plain_text"

    def _build_reasons(
        self,
        *,
        page_type: str,
        signals: PdfHybridTriageSignals,
    ) -> list[str]:
        reasons: list[str] = [f"page_type:{page_type}"]
        if signals.has_struct_tree:
            reasons.append("struct_tree_present")
        if signals.replacement_char_ratio >= self._replacement_char_ratio_threshold:
            reasons.append("text_corruption_signal")
        if signals.table_count > 0:
            reasons.append("table_signal")
        if self._has_strong_vector_grid_signal(page_type=page_type, signals=signals):
            reasons.append("vector_grid_signal")
        if signals.largest_image_ratio >= self._large_image_ratio_threshold:
            reasons.append("large_image_signal")
        if signals.double_column:
            reasons.append("double_column_layout")
        if signals.top_band_count >= self._front_matter_band_threshold:
            reasons.append("front_matter_band")
        if signals.equation_count >= self._formula_block_threshold:
            reasons.append("equation_density")
        return reasons

    def _looks_visual_or_scanned(self, *, signals: PdfHybridTriageSignals) -> bool:
        # Conservative "strong signal" strategy: images alone should not push a page to backend
        # when local extraction already has enough usable text.
        if (
            signals.largest_image_ratio >= self._large_image_ratio_threshold
            and self._local_text_is_insufficient(signals=signals)
            and signals.text_line_count <= 4
        ):
            return True
        if (
            signals.image_area_ratio >= self._total_image_ratio_threshold
            and self._local_text_is_insufficient(signals=signals)
            and signals.text_line_count <= 6
        ):
            return True
        if (
            signals.text_line_count <= 2
            and signals.average_words_per_line <= 4.0
            and signals.vector_line_count >= 120
            and (signals.rect_count >= 20 or signals.curve_count >= 50)
        ):
            return True
        return False

    def _looks_dense_table(self, *, signals: PdfHybridTriageSignals) -> bool:
        if signals.table_count <= 0:
            return False
        if signals.table_area_ratio >= self._dense_table_area_threshold:
            return True
        return signals.vector_line_count >= self._table_vector_line_threshold

    @staticmethod
    def _looks_sparse_form(*, signals: PdfHybridTriageSignals) -> bool:
        if signals.table_count <= 0:
            return False
        if signals.table_area_ratio >= 0.08:
            return False
        if signals.average_words_per_line <= 5.0 and signals.text_line_count <= 24:
            return True
        return signals.vector_line_count >= 4 and signals.text_line_count <= 18

    def _looks_formula_or_display_heavy(self, *, signals: PdfHybridTriageSignals) -> bool:
        if signals.equation_count >= self._formula_block_threshold:
            return True
        return signals.equation_count >= 1 and signals.double_column and signals.text_line_count <= 30

    @staticmethod
    def _looks_mixed_layout(*, signals: PdfHybridTriageSignals) -> bool:
        if not signals.double_column:
            return False
        return signals.image_count > 0 or signals.equation_count > 0 or signals.top_band_count >= 4

    def _looks_front_matter_heavy(self, *, signals: PdfHybridTriageSignals) -> bool:
        if signals.double_column:
            return False
        if signals.top_band_count < self._front_matter_band_threshold:
            return False
        if signals.heading_count > 1:
            return False
        return signals.text_line_count <= 18

    @staticmethod
    def _has_strong_vector_grid_signal(*, page_type: str, signals: PdfHybridTriageSignals) -> bool:
        if page_type == "plain_text":
            return False
        if signals.vector_line_count < 20:
            return False
        if signals.text_line_count <= 12:
            return True
        if signals.text_line_count > 0 and (signals.vector_line_count / max(1, signals.text_line_count)) >= 4.0:
            return True
        return signals.vector_line_count >= 8

    def _has_backend_signal(self, *, page_type: str, signals: PdfHybridTriageSignals) -> bool:
        if signals.replacement_char_ratio >= self._replacement_char_ratio_threshold:
            return True
        if page_type in {"dense_table", "sparse_form"}:
            return True
        # Large images are only a backend signal when local text is likely missing/unusable.
        if self._has_strong_large_image_backend_signal(signals=signals):
            return True
        if signals.table_area_ratio >= self._dense_table_area_threshold:
            return True
        if self._has_strong_vector_grid_signal(page_type=page_type, signals=signals):
            return True
        return False

    @staticmethod
    def _local_text_is_insufficient(*, signals: PdfHybridTriageSignals) -> bool:
        """Heuristic for 'do we have usable local text already?'.

        The goal is to avoid routing to backend for "big image pages" where local extraction still
        yields enough readable text (common for papers with large figures + captions + body).
        """

        # Strong positive local signal: lots of lines, or moderately many lines with decent word density.
        if signals.text_line_count >= 18:
            return False
        if signals.text_line_count >= 12 and signals.average_words_per_line >= 5.5:
            return False
        # If the text is already corrupted, treat it as insufficient even if there are lines.
        if signals.replacement_char_ratio >= 0.12:
            return True
        # Keep Java-style default-local behavior for light visual pages that still expose a
        # short but clearly readable native-text footprint.
        if signals.text_line_count >= 5 and signals.average_words_per_line >= 6.0:
            return False
        return signals.text_line_count <= 6 or signals.average_words_per_line <= 4.5

    def _has_strong_large_image_backend_signal(self, *, signals: PdfHybridTriageSignals) -> bool:
        if signals.largest_image_ratio < self._large_image_ratio_threshold:
            return False
        if not self._local_text_is_insufficient(signals=signals):
            return False
        # "Large image + very little text" is a strong signal for scanned/visual pages.
        if signals.text_line_count <= 4:
            return True
        # If the page is extremely image-dominant, allow a bit more text while still treating it as visual.
        return signals.image_area_ratio >= self._total_image_ratio_threshold and signals.text_line_count <= 6

    @staticmethod
    def _local_confidence(*, page_type: str, signals: PdfHybridTriageSignals) -> float:
        confidence = 0.92
        if signals.double_column:
            confidence -= 0.07
        if signals.image_area_ratio >= 0.08:
            confidence -= 0.07
        if signals.equation_count > 0:
            confidence -= 0.05
        if signals.replacement_char_ratio > 0.08:
            confidence -= 0.15
        return round(max(0.55, min(0.97, confidence)), 4)

    def _backend_confidence(self, *, page_type: str, signals: PdfHybridTriageSignals) -> float:
        if signals.replacement_char_ratio >= self._replacement_char_ratio_threshold:
            return 0.98
        if signals.table_count > 0 and signals.table_area_ratio >= self._dense_table_area_threshold:
            return 0.95
        if signals.table_count > 0:
            return 0.88
        if signals.largest_image_ratio >= self._large_image_ratio_threshold:
            return 0.94
        if signals.vector_line_count >= self._table_vector_line_threshold:
            return 0.9
        if signals.text_line_count <= 12 and signals.vector_line_count >= 20:
            return 0.84
        return 0.8

    @staticmethod
    def _page_area(
        *,
        page_atoms: PdfPageAtoms | None,
        normalized_page: PdfNormalizedPage | None,
    ) -> float:
        for item in (page_atoms, normalized_page):
            meta = getattr(item, "meta", None)
            page_width = float(getattr(meta, "page_width", 0.0) or 0.0)
            page_height = float(getattr(meta, "page_height", 0.0) or 0.0)
            if page_width > 0.0 and page_height > 0.0:
                return page_width * page_height
        return 1.0

    @staticmethod
    def _replacement_char_ratio(text: str) -> float:
        token = str(text or "")
        if not token:
            return 0.0
        bad_count = token.count("\ufffd") + len(_CID_RE.findall(token))
        meaningful_chars = len([char for char in token if not char.isspace()])
        if meaningful_chars <= 0:
            return 0.0
        return bad_count / meaningful_chars

    @staticmethod
    def _bbox_area(bbox: PdfBBox) -> float:
        return max(0.0, float(bbox.width)) * max(0.0, float(bbox.height))

    def _bbox_collection_area(self, entries: Sequence[object], *, page_area: float) -> float:
        total = 0.0
        for item in list(entries or []):
            if isinstance(item, PdfBBox):
                total += self._bbox_area(item)
                continue
            bbox = getattr(item, "bbox", None)
            if isinstance(bbox, PdfBBox):
                total += self._bbox_area(bbox)
        return total / max(1.0, float(page_area))
