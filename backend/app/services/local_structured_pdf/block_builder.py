from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Sequence

from .contracts import (
    PdfBBox,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


_SPACE_RE = re.compile(r"\s+")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•·▪◦]|(?:\d+|[A-Za-z]|[ivxlcdm]+)[\.\)])\s+\S", re.IGNORECASE)
_NUMBERED_HEADING_DEPTH_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_LETTER_NUMBERED_HEADING_DEPTH_RE = re.compile(r"^([A-Z](?:\.\d+)+)", re.IGNORECASE)
_TABLEISH_NUMBER_RE = re.compile(r"^(?:[-+]?\d+(?:\.\d+)?%?|N/?A)$", re.IGNORECASE)
_TABLEISH_MARKER_RE = re.compile(r"^(?:O|X|✗|✓|✔)$", re.IGNORECASE)
_CAPTION_PREFIX_RE = re.compile(r"^(?:fig(?:ure)?|table|chart|image|photo|plate)\b", re.IGNORECASE)
_PAGE_NUMBER_ONLY_RE = re.compile(r"^\s*\(?\s*(?:\d+|[ivxlcdm]+)\s*\)?\s*$", re.IGNORECASE)
_APPENDIX_ENUM_RE = re.compile(
    r"^(?:appendix|chapter|section)\s+[A-Za-z0-9IVXLCM]+[\.\)]?\s+\S",
    re.IGNORECASE,
)
_LETTER_NUMBERED_ENUM_RE = re.compile(r"^[A-Z](?:\.\d+)+[\.\)]?\s+\S", re.IGNORECASE)
_ROMAN_OR_LETTER_ENUM_RE = re.compile(r"^(?:[ivxlcdm]+|[A-Z])[\.\)]?\s+\S", re.IGNORECASE)
_NUMERIC_ENUM_RE = re.compile(r"^(\d+(?:\.\d+)*)[\.\)]?\s+\S")
_METRIC_PREFIX_RE = re.compile(r"^[+-]?\d+(?:\.\d+)*(?:[%‰])?(?:[↑↓↗↘→←]+)?$", re.IGNORECASE)


class _ModeWeightStatistics:
    def __init__(
        self,
        *,
        score_min: float,
        score_max: float,
        mode_min: float,
        mode_max: float,
        rounding: int = 1,
    ) -> None:
        self._score_min = float(score_min)
        self._score_max = float(score_max)
        self._mode_min = float(mode_min)
        self._mode_max = float(mode_max)
        self._rounding = max(0, int(rounding))
        self._count_map: Counter[float] = Counter()

    def add_score(self, score: float) -> None:
        value = round(float(score or 0.0), self._rounding)
        if value <= 0.0:
            return
        self._count_map[value] += 1

    def get_boost(self, score: float) -> float:
        higher_scores = self._higher_scores()
        if not higher_scores:
            return 0.0
        value = round(float(score or 0.0), self._rounding)
        for index, candidate in enumerate(higher_scores, start=1):
            if value == candidate:
                return index / len(higher_scores)
        return 0.0

    def _higher_scores(self) -> list[float]:
        if not self._count_map:
            return []
        mode = self._mode()
        return sorted(
            score
            for score in self._count_map
            if score > mode and self._score_min <= score <= self._score_max
        )

    def _mode(self) -> float:
        ranked = sorted(
            self._count_map.items(),
            key=lambda item: (-item[1], item[0]),
        )
        for value, _count in ranked:
            if self._mode_min <= value <= self._mode_max:
                return value
        return 0.0


class _HeadingStatistics:
    def __init__(self) -> None:
        self._font_size_stats = _ModeWeightStatistics(
            score_min=10.0,
            score_max=32.0,
            mode_min=10.0,
            mode_max=13.0,
            rounding=1,
        )
        self._font_weight_stats = _ModeWeightStatistics(
            score_min=400.0,
            score_max=900.0,
            mode_min=395.0,
            mode_max=405.0,
            rounding=0,
        )

    def add_line(self, line: PdfResolvedLine) -> None:
        self._font_size_stats.add_score(float(line.avg_font_size or 0.0))
        self._font_weight_stats.add_score(LocalPdfBlockBuilder._font_weight_score(line.dominant_font_name))

    def font_size_rarity_boost(self, line: PdfResolvedLine) -> float:
        return self._font_size_stats.get_boost(float(line.avg_font_size or 0.0)) * 0.5

    def font_weight_rarity_boost(self, line: PdfResolvedLine) -> float:
        return self._font_weight_stats.get_boost(LocalPdfBlockBuilder._font_weight_score(line.dominant_font_name)) * 0.3


class LocalPdfBlockBuilder:
    """Stage-3 builder: recover heading/paragraph blocks from ordered lines."""

    def __init__(
        self,
        *,
        heading_font_ratio: float = 1.15,
        heading_max_words: int = 18,
        paragraph_gap_ratio: float = 1.7,
        paragraph_indent_tolerance: float = 24.0,
    ) -> None:
        self._heading_font_ratio = max(1.05, float(heading_font_ratio))
        self._heading_max_words = max(3, int(heading_max_words))
        self._paragraph_gap_ratio = max(1.0, float(paragraph_gap_ratio))
        self._paragraph_indent_tolerance = max(8.0, float(paragraph_indent_tolerance))

    def build_document(self, *, document: PdfResolvedDocument) -> PdfStructuredDocument:
        pages = list(document.pages or [])
        body_font_size = self._estimate_body_font_size(pages=pages)
        heading_stats = self._build_heading_statistics(pages=pages)
        line_map = {
            str(line.line_id): line
            for page in pages
            for line in list(page.lines or [])
        }

        blocks: list[PdfSemanticBlock] = []
        current_paragraph: list[PdfResolvedLine] = []

        def flush_paragraph() -> None:
            nonlocal current_paragraph
            if not current_paragraph:
                return
            block = self._make_block(
                block_index=len(blocks) + 1,
                block_type="paragraph",
                lines=current_paragraph,
            )
            blocks.append(block)
            current_paragraph = []

        for page in pages:
            lines = list(page.lines or [])
            index = 0
            while index < len(lines):
                line = lines[index]
                previous_line = lines[index - 1] if index > 0 else None
                next_line = lines[index + 1] if index + 1 < len(lines) else None
                if self._is_heading_candidate(
                    line=line,
                    body_font_size=body_font_size,
                    heading_stats=heading_stats,
                    previous_line=previous_line,
                    next_line=next_line,
                ):
                    flush_paragraph()
                    heading_lines = [line]
                    index += 1
                    while index < len(lines):
                        candidate_line = lines[index]
                        candidate_next = lines[index + 1] if index + 1 < len(lines) else None
                        if not self._is_heading_candidate(
                            line=candidate_line,
                            body_font_size=body_font_size,
                            heading_stats=heading_stats,
                            previous_line=heading_lines[-1],
                            next_line=candidate_next,
                        ):
                            break
                        if not self._should_continue_heading(
                            previous=heading_lines[-1],
                            current=candidate_line,
                            accumulated=heading_lines,
                        ):
                            break
                        heading_lines.append(candidate_line)
                        index += 1
                    blocks.append(
                        self._make_block(
                            block_index=len(blocks) + 1,
                            block_type="heading",
                            lines=heading_lines,
                        )
                    )
                    continue

                if self._is_list_item_start(line):
                    flush_paragraph()
                    list_lines = [line]
                    index += 1
                    while index < len(lines):
                        next_line = lines[index]
                        if self._is_heading_candidate(
                            line=next_line,
                            body_font_size=body_font_size,
                            heading_stats=heading_stats,
                            previous_line=list_lines[-1] if list_lines else None,
                            next_line=lines[index + 1] if index + 1 < len(lines) else None,
                        ):
                            break
                        if self._is_list_item_start(next_line):
                            break
                        if not self._should_continue_list_item(
                            anchor=list_lines[0],
                            previous=list_lines[-1],
                            current=next_line,
                        ):
                            break
                        list_lines.append(next_line)
                        index += 1
                    blocks.append(
                        self._make_block(
                            block_index=len(blocks) + 1,
                            block_type="list_item",
                            lines=list_lines,
                        )
                    )
                    continue

                if not current_paragraph:
                    current_paragraph = [line]
                    index += 1
                    continue

                if self._should_continue_paragraph(previous=current_paragraph[-1], current=line):
                    current_paragraph.append(line)
                    index += 1
                    continue

                flush_paragraph()
                current_paragraph = [line]
                index += 1

            flush_paragraph()

        leveled_blocks = self._assign_heading_levels(
            blocks=blocks,
            line_map=line_map,
            body_font_size=body_font_size,
        )
        page_map: dict[int, list[PdfSemanticBlock]] = {}
        for block in leveled_blocks:
            page_map.setdefault(int(block.page_start), []).append(block)

        structured_pages = [
            PdfStructuredPage(
                meta=page.meta,
                blocks=page_map.get(int(page.page), []),
            )
            for page in pages
        ]
        return PdfStructuredDocument(
            pages=structured_pages,
            blocks=leveled_blocks,
            body_font_size=body_font_size,
        )

    def _build_heading_statistics(self, *, pages: Sequence) -> _HeadingStatistics:
        stats = _HeadingStatistics()
        for page in pages:
            for line in list(page.lines or []):
                text = _SPACE_RE.sub(" ", str(line.text or "").strip())
                if not text:
                    continue
                stats.add_line(line)
        return stats

    @staticmethod
    def _estimate_body_font_size(*, pages: Sequence) -> float:
        font_sizes: list[float] = [
            float(line.avg_font_size)
            for page in pages
            for line in list(page.lines or [])
            if float(line.avg_font_size or 0.0) > 0.0
        ]
        if not font_sizes:
            return 12.0
        return round(float(median(font_sizes)), 2)

    def _is_heading_candidate(
        self,
        *,
        line: PdfResolvedLine,
        body_font_size: float,
        heading_stats: _HeadingStatistics,
        previous_line: PdfResolvedLine | None = None,
        next_line: PdfResolvedLine | None = None,
    ) -> bool:
        text = _SPACE_RE.sub(" ", str(line.text or "").strip())
        if not text:
            return False
        enumerated_heading = self._is_enumerated_heading_text(text)
        if self._is_list_item_start(line) and not enumerated_heading:
            return False
        if self._looks_table_like_line(text):
            return False
        if self._looks_chart_label_line(text) and not enumerated_heading:
            return False
        word_count = len(text.split())
        if word_count > self._heading_max_words:
            return False
        if len(text) > 140:
            return False

        avg_font_size = float(line.avg_font_size or 0.0)
        font_weight = self._font_weight_score(line.dominant_font_name)
        font_large_enough = avg_font_size >= float(body_font_size) * self._heading_font_ratio
        title_like = self._looks_title_like(text)
        no_terminal_punct = not text.endswith((".", ";", ",")) and not text.endswith(":")
        size_rarity_boost = heading_stats.font_size_rarity_boost(line)
        weight_rarity_boost = heading_stats.font_weight_rarity_boost(line)
        contextual_title = self._looks_contextual_title(
            line=line,
            previous_line=previous_line,
            next_line=next_line,
        )

        score = 0.0
        if font_large_enough:
            score += 0.42
        elif avg_font_size >= float(body_font_size) * 1.05:
            score += 0.20
        if title_like:
            score += 0.20
        if enumerated_heading:
            score += 0.24
        if no_terminal_punct:
            score += 0.10
        if word_count <= 10:
            score += 0.08
        if line.band == "top_band":
            score += 0.14
        if font_weight >= 600:
            score += 0.12
        elif font_weight >= 500:
            score += 0.06
        if contextual_title:
            score += 0.24
        score += size_rarity_boost + weight_rarity_boost

        if text.endswith((".", ";", ",")):
            score -= 0.18
        if len(text) > 100:
            score -= 0.12

        if line.band == "top_band" and avg_font_size >= float(body_font_size) * 1.05 and title_like:
            return True
        if contextual_title and no_terminal_punct:
            return True
        if enumerated_heading and title_like and no_terminal_punct and avg_font_size >= float(body_font_size) * 0.95:
            return True
        return score >= 0.72

    def _looks_contextual_title(
        self,
        *,
        line: PdfResolvedLine,
        previous_line: PdfResolvedLine | None,
        next_line: PdfResolvedLine | None,
    ) -> bool:
        text = _SPACE_RE.sub(" ", str(line.text or "").strip())
        if str(line.band or "") != "top_band":
            return False
        if int(line.reading_order or 0) > 2:
            return False
        if previous_line is not None and int(previous_line.page) == int(line.page):
            return False
        if not text or _PAGE_NUMBER_ONLY_RE.match(text):
            return False
        if _CAPTION_PREFIX_RE.match(text):
            return False
        if len(text.split()) > 8 or text.endswith((".", ";", ",", ":")):
            return False
        if next_line is None or int(next_line.page) != int(line.page):
            return False
        next_text = _SPACE_RE.sub(" ", str(next_line.text or "").strip())
        if not next_text or _CAPTION_PREFIX_RE.match(next_text):
            return False
        if len(next_text.split()) < max(8, len(text.split()) * 2):
            return False
        if len(next_text) <= len(text) * 1.5:
            return False
        return True

    @staticmethod
    def _looks_title_like(text: str) -> bool:
        letters = [char for char in text if char.isalpha()]
        if not letters:
            return False
        upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
        tokens = [token for token in text.split() if any(char.isalpha() for char in token)]
        title_case_hits = sum(1 for token in tokens if token[:1].isupper())
        title_ratio = title_case_hits / max(1, len(tokens))
        return upper_ratio >= 0.5 or title_ratio >= 0.6

    @staticmethod
    def _looks_table_like_line(text: str) -> bool:
        tokens = [
            token.strip(".,;:()[]{}")
            for token in str(text or "").split()
            if token.strip(".,;:()[]{}")
        ]
        if len(tokens) < 4:
            return False
        numeric_like = sum(1 for token in tokens if _TABLEISH_NUMBER_RE.match(token))
        marker_like = sum(1 for token in tokens if _TABLEISH_MARKER_RE.match(token))
        if len(tokens) >= 5 and numeric_like >= 2:
            return True
        if len(tokens) >= 5 and marker_like >= 2 and numeric_like >= 1:
            return True
        if len(tokens) >= 7 and marker_like >= 1 and numeric_like >= 2:
            return True
        return False

    @staticmethod
    def _is_enumerated_heading_text(text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not token:
            return False
        if _APPENDIX_ENUM_RE.match(token):
            return True
        if _LETTER_NUMBERED_ENUM_RE.match(token):
            return True
        numeric_match = _NUMERIC_ENUM_RE.match(token)
        if numeric_match:
            prefix = str(numeric_match.group(1) or "")
            segments = [part for part in prefix.split(".") if part]
            if not segments:
                return False
            if segments[0] == "0":
                return False
            if any(len(part) > 3 for part in segments):
                return False
            if len(segments) > 1 and any(len(part) > 2 for part in segments[1:]):
                return False
            return True
        return bool(_ROMAN_OR_LETTER_ENUM_RE.match(token))

    @staticmethod
    def _looks_chart_label_line(text: str) -> bool:
        tokens = [
            token.strip()
            for token in str(text or "").split()
            if str(token or "").strip()
        ]
        if not tokens:
            return False
        alpha_tokens = sum(1 for token in tokens if any(char.isalpha() for char in token))
        first_token = tokens[0].strip("()[]{}")
        if _METRIC_PREFIX_RE.match(first_token):
            return len(tokens) <= 4 and alpha_tokens <= 2
        if any("_" in token for token in tokens) and alpha_tokens <= 2:
            if any(_METRIC_PREFIX_RE.match(token.strip("()[]{}")) for token in tokens):
                return True
        return False

    def _should_continue_heading(
        self,
        *,
        previous: PdfResolvedLine,
        current: PdfResolvedLine,
        accumulated: Sequence[PdfResolvedLine],
    ) -> bool:
        if int(previous.page) != int(current.page):
            return False
        if str(previous.column_id or "main") != str(current.column_id or "main"):
            return False
        if str(previous.region or "main") != str(current.region or "main"):
            return False

        previous_text = _SPACE_RE.sub(" ", str(previous.text or "").strip())
        current_text = _SPACE_RE.sub(" ", str(current.text or "").strip())
        if not previous_text or not current_text:
            return False
        if self._is_list_item_start(current):
            return False
        if self._looks_table_like_line(current_text):
            return False
        if self._is_enumerated_heading_text(current_text) and accumulated:
            return False

        max_font = max(float(previous.avg_font_size or 0.0), float(current.avg_font_size or 0.0), 8.0)
        vertical_gap = float(current.bbox.top) - float(previous.bbox.bottom)
        if vertical_gap > max(14.0, max_font * 1.35):
            return False

        font_delta = abs(float(previous.avg_font_size or 0.0) - float(current.avg_font_size or 0.0))
        if font_delta > 2.2:
            return False

        prev_center = (float(previous.bbox.x0) + float(previous.bbox.x1)) / 2.0
        curr_center = (float(current.bbox.x0) + float(current.bbox.x1)) / 2.0
        center_delta = abs(prev_center - curr_center)
        left_delta = abs(float(previous.bbox.x0) - float(current.bbox.x0))
        if (
            center_delta > max(54.0, self._paragraph_indent_tolerance * 3.0)
            and left_delta > max(12.0, self._paragraph_indent_tolerance)
        ):
            return False

        total_words = sum(len(str(line.text or "").split()) for line in list(accumulated or [])) + len(current_text.split())
        if total_words > max(self._heading_max_words + 8, 24):
            return False

        return True

    def _should_continue_paragraph(self, *, previous: PdfResolvedLine, current: PdfResolvedLine) -> bool:
        if int(previous.page) != int(current.page):
            return False
        if str(previous.column_id or "main") != str(current.column_id or "main"):
            return False
        if str(previous.region or "main") != str(current.region or "main"):
            return False

        max_font = max(float(previous.avg_font_size or 0.0), float(current.avg_font_size or 0.0), 8.0)
        max_gap = max(18.0, max_font * self._paragraph_gap_ratio)
        vertical_gap = float(current.bbox.top) - float(previous.bbox.bottom)
        if vertical_gap > max_gap:
            return False

        indent_delta = abs(float(current.bbox.x0) - float(previous.bbox.x0))
        if indent_delta > self._paragraph_indent_tolerance:
            return False

        return True

    @staticmethod
    def _is_list_item_start(line: PdfResolvedLine) -> bool:
        text = _SPACE_RE.sub(" ", str(line.text or "").strip())
        if not text:
            return False
        return bool(_LIST_ITEM_RE.match(text))

    def _should_continue_list_item(
        self,
        *,
        anchor: PdfResolvedLine,
        previous: PdfResolvedLine,
        current: PdfResolvedLine,
    ) -> bool:
        if int(previous.page) != int(current.page):
            return False
        if str(previous.column_id or "main") != str(current.column_id or "main"):
            return False
        if str(previous.region or "main") != str(current.region or "main"):
            return False

        max_font = max(float(previous.avg_font_size or 0.0), float(current.avg_font_size or 0.0), 8.0)
        max_gap = max(18.0, max_font * self._paragraph_gap_ratio)
        vertical_gap = float(current.bbox.top) - float(previous.bbox.bottom)
        if vertical_gap > max_gap:
            return False

        anchor_x0 = float(anchor.bbox.x0)
        current_x0 = float(current.bbox.x0)
        if current_x0 < anchor_x0 - 4.0:
            return False
        if current_x0 > anchor_x0 + (self._paragraph_indent_tolerance * 2.0):
            return False
        return True

    def _make_block(
        self,
        *,
        block_index: int,
        block_type: str,
        lines: Sequence[PdfResolvedLine],
    ) -> PdfSemanticBlock:
        block_lines = list(lines or [])
        bbox = self._merge_bboxes([line.bbox for line in block_lines])
        text = "\n".join(str(line.text or "").strip() for line in block_lines if str(line.text or "").strip()).strip()
        avg_font_size = 0.0
        if block_lines:
            avg_font_size = round(
                sum(float(line.avg_font_size or 0.0) for line in block_lines) / len(block_lines),
                2,
            )
        first = block_lines[0]
        last = block_lines[-1]
        return PdfSemanticBlock(
            block_id=f"b{block_index:05d}",
            block_type=block_type,
            page_start=int(first.page),
            page_end=int(last.page),
            text=text,
            bbox=bbox,
            line_ids=[str(line.line_id) for line in block_lines],
            column_id=str(first.column_id or "main"),
            region=str(first.region or "main"),
            avg_font_size=avg_font_size,
            reading_order_start=int(first.reading_order or 0),
            reading_order_end=int(last.reading_order or 0),
        )

    def _assign_heading_levels(
        self,
        *,
        blocks: Sequence[PdfSemanticBlock],
        line_map: dict[str, PdfResolvedLine],
        body_font_size: float,
    ) -> list[PdfSemanticBlock]:
        heading_blocks = [block for block in blocks if str(block.block_type) == "heading"]
        if not heading_blocks:
            return list(blocks)

        style_keys = sorted(
            {
                self._heading_style_key(block=block, line_map=line_map)
                for block in heading_blocks
            },
            key=lambda item: (-item[0], -item[1], -item[2]),
        )
        style_to_level = {
            style_key: min(index + 1, 6)
            for index, style_key in enumerate(style_keys)
        }
        has_document_title = any(
            self._is_document_title_block(
                block=block,
                line_map=line_map,
                body_font_size=body_font_size,
            )
            for block in heading_blocks
        )
        heading_offset = 1 if has_document_title else 0

        leveled: list[PdfSemanticBlock] = []
        for block in blocks:
            if str(block.block_type) != "heading":
                leveled.append(block)
                continue
            style_level = style_to_level.get(
                self._heading_style_key(block=block, line_map=line_map),
                1,
            )
            numbered_depth = self._heading_number_depth(block.text)
            if numbered_depth > 0:
                style_level = max(style_level, min(6, numbered_depth + heading_offset))
            leveled.append(
                PdfSemanticBlock(
                    block_id=block.block_id,
                    block_type=block.block_type,
                    page_start=block.page_start,
                    page_end=block.page_end,
                    text=block.text,
                    bbox=block.bbox,
                    line_ids=list(block.line_ids),
                    column_id=block.column_id,
                    region=block.region,
                    avg_font_size=block.avg_font_size,
                    reading_order_start=block.reading_order_start,
                    reading_order_end=block.reading_order_end,
                    heading_level=style_level,
                    parent_heading_id=block.parent_heading_id,
                    section_heading_ids=list(block.section_heading_ids),
                    section_titles=list(block.section_titles),
                    section_path=block.section_path,
                    table_rows=[list(row) for row in block.table_rows],
                )
            )
        return leveled

    def _heading_style_key(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
    ) -> tuple[float, float, int]:
        first_line = self._first_block_line(block=block, line_map=line_map)
        font_size = round(float(block.avg_font_size or 0.0), 1)
        font_weight = self._font_weight_score(first_line.dominant_font_name if first_line else "")
        title_signal = 1 if self._looks_title_like(str(block.text or "")) else 0
        return (font_size, font_weight, title_signal)

    def _is_document_title_block(
        self,
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
        body_font_size: float,
    ) -> bool:
        line = self._first_block_line(block=block, line_map=line_map)
        if line is None:
            return False
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not text or self._is_enumerated_heading_text(text):
            return False
        if int(block.page_start) != 1:
            return False
        if str(line.band or "") == "top_band" and float(block.avg_font_size or 0.0) >= float(body_font_size) * 1.15:
            return True
        return int(block.reading_order_start or 0) == 1 and float(block.avg_font_size or 0.0) >= float(body_font_size) * 1.3

    @staticmethod
    def _first_block_line(
        *,
        block: PdfSemanticBlock,
        line_map: dict[str, PdfResolvedLine],
    ) -> PdfResolvedLine | None:
        for line_id in list(block.line_ids or []):
            line = line_map.get(str(line_id))
            if line is not None:
                return line
        return None

    @staticmethod
    def _heading_number_depth(text: str) -> int:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not LocalPdfBlockBuilder._is_enumerated_heading_text(token):
            return 0
        match = _NUMBERED_HEADING_DEPTH_RE.match(token)
        if match:
            value = str(match.group(1) or "").strip(".")
            if value:
                return max(1, len([part for part in value.split(".") if part]))
        letter_match = _LETTER_NUMBERED_HEADING_DEPTH_RE.match(token)
        if not letter_match:
            return 0
        value = str(letter_match.group(1) or "").strip(".")
        if not value:
            return 0
        return max(1, len([part for part in value.split(".") if part]))

    @staticmethod
    def _font_weight_score(font_name: str) -> float:
        token = str(font_name or "").lower()
        if any(flag in token for flag in ("black", "heavy", "extrabold", "ultrabold")):
            return 800.0
        if any(flag in token for flag in ("bold", "demi", "semibold", "semi-bold")):
            return 700.0
        if "medium" in token:
            return 500.0
        if any(flag in token for flag in ("light", "thin", "book")):
            return 300.0
        return 400.0

    @staticmethod
    def _merge_bboxes(boxes: Sequence[PdfBBox]) -> PdfBBox:
        valid_boxes = [box for box in list(boxes or []) if isinstance(box, PdfBBox)]
        if not valid_boxes:
            return PdfBBox(x0=0.0, top=0.0, x1=0.0, bottom=0.0)
        return PdfBBox(
            x0=round(min(float(box.x0) for box in valid_boxes), 2),
            top=round(min(float(box.top) for box in valid_boxes), 2),
            x1=round(max(float(box.x1) for box in valid_boxes), 2),
            bottom=round(max(float(box.bottom) for box in valid_boxes), 2),
        )
