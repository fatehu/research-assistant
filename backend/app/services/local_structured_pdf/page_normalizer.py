from __future__ import annotations

import re
from collections import Counter
from statistics import median

from .contracts import PdfBBox, PdfNormalizedPage, PdfPageAtoms, PdfTextBlockAtom, PdfTextLine, PdfWordAtom


_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?%\)\]\}])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([\(\[\{])\s+")
_TABLEISH_NUMBER_RE = re.compile(r"^(?:[-+]?\d+(?:\.\d+)?%?|N/?A)$", re.IGNORECASE)
_TABLEISH_MARKER_RE = re.compile(r"^(?:O|X|✗|✓|✔)$", re.IGNORECASE)
_HEADING_ENUM_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+)|(?:appendix|chapter|section)\s+[A-Za-z0-9IVXLCM]+)\b",
    re.IGNORECASE,
)
_HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+)|(?:appendix|chapter|section)\s+[A-Za-z0-9IVXLCM]+)$",
    re.IGNORECASE,
)
_TITLE_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z0-9'`-]+$")
_FOOTNOTEISH_START_RE = re.compile(r"^(?:\d{1,2}|[*†‡§¶])(?:[\]\).-]?\s+|\b)")


class LocalPdfPageNormalizer:
    """Stage-1 normalizer: drop obvious noise and group words into line-like units."""

    def __init__(
        self,
        *,
        tiny_font_threshold: float = 1.0,
        out_of_page_tolerance: float = 2.0,
        same_line_vertical_tolerance: float = 3.0,
        same_line_gap_threshold: float = 36.0,
        margin_vertical_edge_ratio: float = 0.08,
        margin_vertical_width_max: float = 36.0,
        margin_vertical_aspect_ratio: float = 1.6,
        margin_vertical_text_max_len: int = 24,
    ) -> None:
        self._tiny_font_threshold = max(0.0, float(tiny_font_threshold))
        self._out_of_page_tolerance = max(0.0, float(out_of_page_tolerance))
        self._same_line_vertical_tolerance = max(0.0, float(same_line_vertical_tolerance))
        self._same_line_gap_threshold = max(8.0, float(same_line_gap_threshold))
        self._margin_vertical_edge_ratio = max(0.02, float(margin_vertical_edge_ratio))
        self._margin_vertical_width_max = max(12.0, float(margin_vertical_width_max))
        self._margin_vertical_aspect_ratio = max(1.1, float(margin_vertical_aspect_ratio))
        self._margin_vertical_text_max_len = max(4, int(margin_vertical_text_max_len))

    def normalize_page(self, *, page_atoms: PdfPageAtoms) -> PdfNormalizedPage:
        kept_words: list[PdfWordAtom] = []
        dropped_words: list[dict[str, object]] = []
        for word in list(page_atoms.words or []):
            reason = self._drop_reason(page_atoms=page_atoms, word=word)
            if reason:
                dropped_words.append({"word_id": word.word_id, "text": word.text, "reason": reason})
                continue
            kept_words.append(word)

        kept_words.sort(
            key=lambda item: (
                int(
                    round(
                        ((float(item.bbox.top) + float(item.bbox.bottom)) / 2.0)
                        / max(1.0, self._same_line_vertical_tolerance)
                    )
                ),
                round(item.bbox.x0, 2),
                round(item.bbox.top, 2),
                item.word_id,
            )
        )
        text_lines = self._group_words_into_lines(page_atoms=page_atoms, words=kept_words)
        return PdfNormalizedPage(
            meta=page_atoms.meta,
            kept_words=kept_words,
            dropped_words=dropped_words,
            table_bboxes=[item.bbox for item in list(page_atoms.tables or []) if item.bbox.width > 0.0 and item.bbox.height > 0.0],
            text_blocks=self._select_text_blocks(page_atoms=page_atoms),
            text_lines=text_lines,
        )

    @staticmethod
    def _select_text_blocks(*, page_atoms: PdfPageAtoms) -> list[PdfTextBlockAtom]:
        return [
            block
            for block in list(page_atoms.text_blocks or [])
            if isinstance(block, PdfTextBlockAtom)
            and str(block.block_kind or "text") == "text"
            and str(block.text or "").strip()
            and float(block.bbox.width) > 0.0
            and float(block.bbox.height) > 0.0
        ]

    def _drop_reason(self, *, page_atoms: PdfPageAtoms, word: PdfWordAtom) -> str:
        text = str(word.text or "").strip()
        if not text:
            return "blank_text"
        if word.bbox.width <= 0.0 or word.bbox.height <= 0.0:
            return "zero_bbox"
        if self._is_tiny(word):
            return "tiny_word"
        if self._is_out_of_page(page_atoms=page_atoms, word=word):
            return "out_of_page"
        if self._is_vertical_margin_text(page_atoms=page_atoms, word=word):
            return "vertical_margin_text"
        return ""

    def _is_tiny(self, word: PdfWordAtom) -> bool:
        if float(word.font_size or 0.0) > 0.0 and float(word.font_size or 0.0) <= self._tiny_font_threshold:
            return True
        if float(word.bbox.height or 0.0) <= self._tiny_font_threshold:
            return True
        return False

    def _is_out_of_page(self, *, page_atoms: PdfPageAtoms, word: PdfWordAtom) -> bool:
        page_width = float(page_atoms.meta.page_width or 0.0)
        page_height = float(page_atoms.meta.page_height or 0.0)
        tol = self._out_of_page_tolerance
        if page_width > 0.0 and (word.bbox.x1 < -tol or word.bbox.x0 > page_width + tol):
            return True
        if page_height > 0.0 and (word.bbox.bottom < -tol or word.bbox.top > page_height + tol):
            return True
        return False

    def _is_vertical_margin_text(self, *, page_atoms: PdfPageAtoms, word: PdfWordAtom) -> bool:
        text = re.sub(r"\s+", "", str(word.text or ""))
        if not text or len(text) > self._margin_vertical_text_max_len:
            return False

        page_width = float(page_atoms.meta.page_width or 0.0)
        if page_width <= 0.0:
            return False

        edge_threshold = max(18.0, page_width * self._margin_vertical_edge_ratio)
        near_left_edge = float(word.bbox.x0) <= edge_threshold
        near_right_edge = (page_width - float(word.bbox.x1)) <= edge_threshold
        if not (near_left_edge or near_right_edge):
            return False

        bbox_width = float(word.bbox.width or 0.0)
        bbox_height = float(word.bbox.height or 0.0)
        if bbox_width <= 0.0 or bbox_height <= 0.0:
            return False
        if bbox_width > self._margin_vertical_width_max:
            return False
        if bbox_height < max(18.0, bbox_width * self._margin_vertical_aspect_ratio):
            return False
        return True

    def _group_words_into_lines(
        self,
        *,
        page_atoms: PdfPageAtoms,
        words: list[PdfWordAtom],
    ) -> list[PdfTextLine]:
        if not words:
            return []
        word_block_map = self._build_word_block_map(page_atoms=page_atoms, words=words)
        text_block_lookup = {
            str(block.block_id): block
            for block in self._select_text_blocks(page_atoms=page_atoms)
            if str(block.block_id or "").strip()
        }
        groups: list[list[PdfWordAtom]] = []
        current: list[PdfWordAtom] = []
        for word in words:
            if not current:
                current = [word]
                continue
            if self._should_join_current_line(
                current=current,
                candidate=word,
                word_block_map=word_block_map,
                text_block_lookup=text_block_lookup,
            ):
                current.append(word)
                continue
            groups.append(sorted(current, key=lambda item: (round(item.bbox.x0, 2), item.word_id)))
            current = [word]
        if current:
            groups.append(sorted(current, key=lambda item: (round(item.bbox.x0, 2), item.word_id)))
        groups = self._merge_tableish_groups(groups=groups)
        groups = self._split_mixed_semantic_groups(groups=groups)

        lines: list[PdfTextLine] = []
        for index, group in enumerate(groups, start=1):
            bbox = self._merge_bboxes([item.bbox for item in group])
            font_sizes = [float(item.font_size or 0.0) for item in group if float(item.font_size or 0.0) > 0.0]
            font_names = [str(item.font_name or "").strip() for item in group if str(item.font_name or "").strip()]
            lines.append(
                PdfTextLine(
                    line_id=f"p{int(page_atoms.meta.page):04d}_l{index:04d}",
                    page=int(page_atoms.meta.page),
                    text=self._stitch_words(group),
                    bbox=bbox,
                    word_ids=[item.word_id for item in group],
                    avg_font_size=round(sum(font_sizes) / len(font_sizes), 2) if font_sizes else 0.0,
                    dominant_font_name=Counter(font_names).most_common(1)[0][0] if font_names else "",
                    band=self._band_for_bbox(page_atoms=page_atoms, bbox=bbox),
                )
            )
        return [
            line
            for line in lines
            if not self._is_margin_fragment_line(page_atoms=page_atoms, line=line)
        ]

    def _build_word_block_map(
        self,
        *,
        page_atoms: PdfPageAtoms,
        words: list[PdfWordAtom],
    ) -> dict[str, str]:
        text_blocks = self._select_text_blocks(page_atoms=page_atoms)
        if not text_blocks:
            return {}

        word_block_map: dict[str, str] = {}
        for word in list(words or []):
            best_block_id = ""
            best_score = 0.0
            for block in text_blocks:
                score = self._word_block_match_score(word=word, block=block)
                if score > best_score:
                    best_score = score
                    best_block_id = str(block.block_id or "")
            if best_block_id and best_score >= 0.55:
                word_block_map[str(word.word_id)] = best_block_id
        return word_block_map

    def _should_join_current_line(
        self,
        *,
        current: list[PdfWordAtom],
        candidate: PdfWordAtom,
        word_block_map: dict[str, str] | None = None,
        text_block_lookup: dict[str, PdfTextBlockAtom] | None = None,
    ) -> bool:
        block_map = dict(word_block_map or {})
        block_lookup = dict(text_block_lookup or {})
        current_block_ids = {
            block_map.get(str(item.word_id), "")
            for item in list(current or [])
            if block_map.get(str(item.word_id), "")
        }
        candidate_block_id = block_map.get(str(candidate.word_id), "")
        if current_block_ids and candidate_block_id and candidate_block_id not in current_block_ids:
            candidate_block = block_lookup.get(candidate_block_id)
            if candidate_block is not None:
                for current_block_id in current_block_ids:
                    current_block = block_lookup.get(current_block_id)
                    if current_block is None:
                        continue
                    if self._blocks_indicate_separate_columns(current_block=current_block, candidate_block=candidate_block):
                        return False

        current_bbox = self._merge_bboxes([item.bbox for item in current])
        vertical_overlap = min(current_bbox.bottom, candidate.bbox.bottom) - max(current_bbox.top, candidate.bbox.top)
        max_height = max(current_bbox.height, candidate.bbox.height, 1.0)
        same_row = vertical_overlap >= max(0.5, max_height * 0.35)
        center_delta = abs(
            ((current_bbox.top + current_bbox.bottom) / 2.0)
            - ((candidate.bbox.top + candidate.bbox.bottom) / 2.0)
        )
        if not same_row and center_delta > max(self._same_line_vertical_tolerance, max_height * 0.45):
            return False
        x_gap = float(candidate.bbox.x0) - float(current_bbox.x1)
        avg_font_size = (
            sum(float(item.font_size or 0.0) for item in current if float(item.font_size or 0.0) > 0.0)
            / max(1, len([item for item in current if float(item.font_size or 0.0) > 0.0]))
        )
        dynamic_gap = max(self._same_line_gap_threshold, avg_font_size * 6.0 if avg_font_size > 0.0 else 0.0)
        return x_gap <= dynamic_gap

    @staticmethod
    def _word_block_match_score(*, word: PdfWordAtom, block: PdfTextBlockAtom) -> float:
        overlap_left = max(float(word.bbox.x0), float(block.bbox.x0))
        overlap_right = min(float(word.bbox.x1), float(block.bbox.x1))
        overlap_top = max(float(word.bbox.top), float(block.bbox.top))
        overlap_bottom = min(float(word.bbox.bottom), float(block.bbox.bottom))
        overlap_width = max(0.0, overlap_right - overlap_left)
        overlap_height = max(0.0, overlap_bottom - overlap_top)
        if overlap_width <= 0.0 or overlap_height <= 0.0:
            return 0.0
        word_area = max(1.0, float(word.bbox.width) * float(word.bbox.height))
        overlap_area = overlap_width * overlap_height
        center_x = (float(word.bbox.x0) + float(word.bbox.x1)) / 2.0
        center_y = (float(word.bbox.top) + float(word.bbox.bottom)) / 2.0
        if not (float(block.bbox.x0) <= center_x <= float(block.bbox.x1)):
            return 0.0
        if not (float(block.bbox.top) <= center_y <= float(block.bbox.bottom)):
            return overlap_area / word_area * 0.5
        return overlap_area / word_area

    def _is_margin_fragment_line(self, *, page_atoms: PdfPageAtoms, line: PdfTextLine) -> bool:
        text = str(line.text or "").strip()
        compact_text = re.sub(r"\s+", "", text)
        if not compact_text or " " in text or len(compact_text) > self._margin_vertical_text_max_len:
            return False

        page_width = float(page_atoms.meta.page_width or 0.0)
        if page_width <= 0.0:
            return False

        edge_threshold = max(18.0, page_width * self._margin_vertical_edge_ratio)
        near_left_edge = float(line.bbox.x1) <= edge_threshold
        near_right_edge = float(line.bbox.x0) >= (page_width - edge_threshold)
        if not (near_left_edge or near_right_edge):
            return False
        if float(line.bbox.width or 0.0) > self._margin_vertical_width_max:
            return False
        return True

    @staticmethod
    def _blocks_indicate_separate_columns(
        *,
        current_block: PdfTextBlockAtom,
        candidate_block: PdfTextBlockAtom,
    ) -> bool:
        overlap_left = max(float(current_block.bbox.x0), float(candidate_block.bbox.x0))
        overlap_right = min(float(current_block.bbox.x1), float(candidate_block.bbox.x1))
        horizontal_overlap = max(0.0, overlap_right - overlap_left)
        smaller_width = min(float(current_block.bbox.width), float(candidate_block.bbox.width))
        if smaller_width > 0.0 and horizontal_overlap >= smaller_width * 0.2:
            return False

        current_center = (float(current_block.bbox.x0) + float(current_block.bbox.x1)) / 2.0
        candidate_center = (float(candidate_block.bbox.x0) + float(candidate_block.bbox.x1)) / 2.0
        center_distance = abs(current_center - candidate_center)
        block_gap = max(
            0.0,
            max(float(current_block.bbox.x0), float(candidate_block.bbox.x0))
            - min(float(current_block.bbox.x1), float(candidate_block.bbox.x1)),
        )
        return center_distance >= 120.0 or (center_distance >= 80.0 and block_gap >= 12.0)

    def _merge_tableish_groups(self, *, groups: list[list[PdfWordAtom]]) -> list[list[PdfWordAtom]]:
        if len(groups) < 2:
            return groups
        merged: list[list[PdfWordAtom]] = []
        for group in groups:
            normalized_group = sorted(group, key=lambda item: (round(item.bbox.x0, 2), item.word_id))
            if not merged:
                merged.append(normalized_group)
                continue
            previous = merged[-1]
            if self._should_merge_tableish_groups(previous=previous, current=normalized_group):
                merged[-1] = sorted(previous + normalized_group, key=lambda item: (round(item.bbox.x0, 2), item.word_id))
                continue
            merged.append(normalized_group)
        return merged

    def _should_merge_tableish_groups(
        self,
        *,
        previous: list[PdfWordAtom],
        current: list[PdfWordAtom],
    ) -> bool:
        previous_bbox = self._merge_bboxes([item.bbox for item in previous])
        current_bbox = self._merge_bboxes([item.bbox for item in current])
        vertical_overlap = min(previous_bbox.bottom, current_bbox.bottom) - max(previous_bbox.top, current_bbox.top)
        max_height = max(previous_bbox.height, current_bbox.height, 1.0)
        if vertical_overlap < max(0.5, max_height * 0.25):
            return False
        center_delta = abs(
            ((previous_bbox.top + previous_bbox.bottom) / 2.0)
            - ((current_bbox.top + current_bbox.bottom) / 2.0)
        )
        if center_delta > max(self._same_line_vertical_tolerance, max_height * 0.65):
            return False

        previous_text = self._stitch_words(previous)
        current_text = self._stitch_words(current)
        if not (self._looks_table_fragment_text(previous_text) and self._looks_table_fragment_text(current_text)):
            return False

        horizontal_overlap = min(previous_bbox.x1, current_bbox.x1) - max(previous_bbox.x0, current_bbox.x0)
        if horizontal_overlap > max(6.0, min(previous_bbox.width, current_bbox.width) * 0.2):
            return self._is_isolated_marker_fragment(previous_text) or self._is_isolated_marker_fragment(current_text)

        x_gap = float(current_bbox.x0) - float(previous_bbox.x1)
        return x_gap <= max(self._same_line_gap_threshold * 5.0, max_height * 16.0)

    def _split_mixed_semantic_groups(self, *, groups: list[list[PdfWordAtom]]) -> list[list[PdfWordAtom]]:
        if not groups:
            return []
        split_groups: list[list[PdfWordAtom]] = []
        for group in groups:
            split_groups.extend(self._split_group_on_semantic_gap(group=group))
        return split_groups

    def _split_group_on_semantic_gap(self, *, group: list[PdfWordAtom]) -> list[list[PdfWordAtom]]:
        words = sorted(group, key=lambda item: (round(item.bbox.x0, 2), item.word_id))
        if len(words) < 3:
            return [words]

        positive_gaps = [
            max(0.0, float(words[index + 1].bbox.x0) - float(words[index].bbox.x1))
            for index in range(len(words) - 1)
        ]
        median_gap = float(median([gap for gap in positive_gaps if gap > 0.0])) if any(gap > 0.0 for gap in positive_gaps) else 0.0
        avg_font_size = self._average_font_size(words)

        segments: list[list[PdfWordAtom]] = []
        start = 0
        for index in range(len(words) - 1):
            if not self._should_split_on_large_gap(
                words=words,
                split_index=index,
                median_gap=median_gap,
                avg_font_size=avg_font_size,
            ):
                continue
            segments.append(words[start : index + 1])
            start = index + 1
        segments.append(words[start:])
        return [segment for segment in segments if segment]

    def _should_split_on_large_gap(
        self,
        *,
        words: list[PdfWordAtom],
        split_index: int,
        median_gap: float,
        avg_font_size: float,
    ) -> bool:
        current = words[split_index]
        candidate = words[split_index + 1]
        x_gap = float(candidate.bbox.x0) - float(current.bbox.x1)
        gap_threshold = max(
            self._same_line_gap_threshold * 0.4,
            avg_font_size * 1.3 if avg_font_size > 0.0 else 0.0,
            median_gap * 3.0 if median_gap > 0.0 else 0.0,
        )
        if x_gap < gap_threshold:
            return False

        left_words = words[: split_index + 1]
        right_words = words[split_index + 1 :]
        left_text = self._stitch_words(left_words)
        right_text = self._stitch_words(right_words)
        if not left_text or not right_text:
            return False

        left_heading_like = self._looks_heading_like_fragment(left_text)
        right_heading_like = self._looks_heading_like_fragment(right_text)
        right_footnote_like = self._looks_footnote_like_fragment(right_text)
        left_sentence_tail = self._looks_sentence_tail_fragment(left_text)
        font_delta = abs(self._average_font_size(left_words) - self._average_font_size(right_words))

        if (
            not left_heading_like
            and not right_heading_like
            and self._looks_table_fragment_text(left_text)
            and self._looks_table_fragment_text(right_text)
        ):
            return False

        if self._looks_heading_prefix_fragment(left_text) and right_heading_like:
            return False

        if right_heading_like and left_sentence_tail:
            return True
        if left_heading_like and (
            right_footnote_like
            or (font_delta >= 1.0 and len(right_words) <= 4)
        ):
            return True
        if right_heading_like and x_gap >= max(gap_threshold, avg_font_size * 1.4 if avg_font_size > 0.0 else 0.0):
            return True
        return False

    @staticmethod
    def _average_font_size(words: list[PdfWordAtom]) -> float:
        font_sizes = [float(item.font_size or 0.0) for item in words if float(item.font_size or 0.0) > 0.0]
        if not font_sizes:
            return 0.0
        return sum(font_sizes) / len(font_sizes)

    @staticmethod
    def _looks_heading_like_fragment(text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if cleaned.endswith((".", ";", ",")):
            return False
        words = [token.strip(".,;:()[]{}") for token in cleaned.split() if token.strip(".,;:()[]{}")]
        if not words or len(words) > 8:
            return False
        if _HEADING_ENUM_RE.match(cleaned):
            return True
        title_like = sum(1 for token in words if _TITLE_TOKEN_RE.match(token))
        return title_like >= max(1, len(words) - 1)

    @staticmethod
    def _looks_heading_prefix_fragment(text: str) -> bool:
        cleaned = str(text or "").strip().strip(".,;:()[]{}")
        if not cleaned:
            return False
        return bool(_HEADING_PREFIX_RE.match(cleaned))

    @staticmethod
    def _looks_footnote_like_fragment(text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if _FOOTNOTEISH_START_RE.match(cleaned):
            return True
        return cleaned[:1].islower()

    @staticmethod
    def _looks_sentence_tail_fragment(text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if cleaned.endswith((".", "!", "?", ":", ";")):
            return True
        words = [token.strip(".,;:()[]{}") for token in cleaned.split() if token.strip(".,;:()[]{}")]
        if len(words) < 3:
            return False
        last_word = words[-1]
        if not last_word:
            return False
        return last_word[:1].islower()

    @staticmethod
    def _merge_bboxes(boxes: list[PdfBBox]) -> PdfBBox:
        if not boxes:
            return PdfBBox(x0=0.0, top=0.0, x1=0.0, bottom=0.0)
        return PdfBBox(
            x0=round(min(item.x0 for item in boxes), 2),
            top=round(min(item.top for item in boxes), 2),
            x1=round(max(item.x1 for item in boxes), 2),
            bottom=round(max(item.bottom for item in boxes), 2),
        )

    @classmethod
    def _stitch_words(cls, words: list[PdfWordAtom]) -> str:
        text = " ".join(str(item.text or "").strip() for item in words if str(item.text or "").strip())
        text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
        text = _SPACE_AFTER_OPEN_RE.sub(r"\1", text)
        return text.strip()

    @staticmethod
    def _looks_table_fragment_text(text: str) -> bool:
        tokens = [
            token.strip(".,;:()[]{}")
            for token in str(text or "").split()
            if token.strip(".,;:()[]{}")
        ]
        if not tokens:
            return False
        numeric_like = sum(1 for token in tokens if _TABLEISH_NUMBER_RE.match(token))
        marker_like = sum(1 for token in tokens if _TABLEISH_MARKER_RE.match(token))
        if numeric_like >= 2:
            return True
        if marker_like >= 1 and len(tokens) <= 5:
            return True
        if marker_like >= 2:
            return True
        if len(tokens) <= 4 and any(char.isdigit() for char in str(text or "")) and not str(text or "").strip().endswith((".", "?", "!")):
            return True
        return False

    @staticmethod
    def _is_isolated_marker_fragment(text: str) -> bool:
        tokens = [
            token.strip(".,;:()[]{}")
            for token in str(text or "").split()
            if token.strip(".,;:()[]{}")
        ]
        if not tokens or len(tokens) > 2:
            return False
        return all(_TABLEISH_MARKER_RE.match(token) for token in tokens)

    @staticmethod
    def _band_for_bbox(*, page_atoms: PdfPageAtoms, bbox: PdfBBox) -> str:
        page_height = float(page_atoms.meta.page_height or 0.0)
        if page_height <= 0.0:
            return "body"
        center_y = bbox.top + (bbox.height / 2.0)
        if center_y <= page_height * 0.15:
            return "top_band"
        if center_y >= page_height * 0.85:
            return "bottom_band"
        return "body"
