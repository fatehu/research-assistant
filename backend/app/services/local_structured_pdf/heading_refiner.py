from __future__ import annotations

from dataclasses import replace
import re

from .contracts import PdfBBox, PdfPageMeta, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage


_SPACE_RE = re.compile(r"\s+")
_CAPTION_PREFIX_RE = re.compile(r"^(?:fig(?:ure)?|table|chart|image|photo|plate)\b", re.IGNORECASE)
_FOOTNOTE_PREFIX_RE = re.compile(r"^\s*(?:\d{1,2}|[*†‡§¶])[\]\).]?\s+\S", re.IGNORECASE)
_COLON_SECTION_RE = re.compile(r"^[A-Z][A-Za-z0-9'`&/\- ]{1,80}:$")
_CHAPTER_HEADING_RE = re.compile(r"^(?:chapter|appendix)\s+[A-Za-z0-9IVXLCM]+[\.\)]?$", re.IGNORECASE)
_TITLE_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z0-9'`&/\-]*$")
_ALL_CAPS_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9'`&/\-]*$")
_METAISH_RE = re.compile(r"(?:https?://|www\.|doi:|arxiv:|@)", re.IGNORECASE)
_TABLEISH_NUMBER_RE = re.compile(r"^(?:[-+]?\d+(?:\.\d+)?%?|N/?A)$", re.IGNORECASE)
_TABLEISH_MARKER_RE = re.compile(r"^(?:O|X|✗|✓|✔)$", re.IGNORECASE)
_CANONICAL_SECTION_RE = re.compile(
    r"^(?:abstract|introduction|background|methods?|methodology|results?|discussion|conclusions?|summary|keywords?|references|bibliography|acknowledg(?:e)?ments?|contents?|table\s+of\s+contents?)$",
    re.IGNORECASE,
)

_HEADING_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


class LocalPdfHeadingRefiner:
    """Stage-4.6 resolver: promote conservative paragraph heading patterns."""

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        blocks = list(document.blocks or [])
        if not blocks:
            return document

        page_meta_map = {
            int(page.page): page.meta
            for page in list(document.pages or [])
        }
        resolved_blocks: list[PdfSemanticBlock] = []
        promoted_heading_ids: set[str] = set()

        for index, block in enumerate(blocks):
            if str(block.block_type or "") != "paragraph":
                resolved_blocks.append(block)
                continue

            if not self._should_promote(
                blocks=blocks,
                resolved_blocks=resolved_blocks,
                index=index,
                block=block,
                body_font_size=float(document.body_font_size or 0.0),
                page_meta=page_meta_map.get(int(block.page_start)),
            ):
                resolved_blocks.append(block)
                continue

            previous_heading = self._previous_heading(blocks=resolved_blocks)
            resolved_heading = replace(
                block,
                block_type="heading",
                heading_level=self._infer_heading_level(
                    block=block,
                    previous_heading=previous_heading,
                ),
            )
            resolved_blocks.append(resolved_heading)
            promoted_heading_ids.add(str(block.block_id))

        resolved_blocks = self._merge_adjacent_headings(
            blocks=resolved_blocks,
            promoted_heading_ids=promoted_heading_ids,
            body_font_size=float(document.body_font_size or 0.0),
        )

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

    def _should_promote(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        resolved_blocks: list[PdfSemanticBlock],
        index: int,
        block: PdfSemanticBlock,
        body_font_size: float,
        page_meta: PdfPageMeta | None,
    ) -> bool:
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not text or int(block.page_start) != int(block.page_end):
            return False
        if _CAPTION_PREFIX_RE.match(text) or _FOOTNOTE_PREFIX_RE.match(text):
            return False
        if self._looks_table_like_text(text):
            return False

        words = [token for token in text.split() if token]
        if not words or len(words) > 10:
            return False

        next_block = blocks[index + 1] if index + 1 < len(blocks) else None
        previous_block = resolved_blocks[-1] if resolved_blocks else None
        if self._looks_chapter_heading(text):
            return True
        if self._looks_colon_heading(text) and self._can_anchor_following_content(block=block, candidate=next_block):
            return True
        if self._looks_table_lead_heading(text=text, block=block, next_block=next_block, body_font_size=body_font_size):
            return True
        if self._looks_standalone_section_heading(
            text=text,
            block=block,
            previous_block=previous_block,
            next_block=next_block,
            body_font_size=body_font_size,
            page_meta=page_meta,
        ):
            return True
        if self._looks_heading_continuation(
            block=block,
            previous_block=previous_block,
            body_font_size=body_font_size,
        ):
            return True

        if body_font_size > 0.0 and float(block.avg_font_size or 0.0) < body_font_size * 0.95:
            if not self._looks_parallel_short_title_band(blocks=blocks, index=index):
                return False

        if self._looks_parallel_short_title_band(blocks=blocks, index=index):
            return True
        if self._looks_parallel_panel_heading(blocks=blocks, index=index, body_font_size=body_font_size):
            return True
        if self._is_first_page_top_matter(block=block, page_meta=page_meta):
            return False
        return False

    def _looks_standalone_section_heading(
        self,
        *,
        text: str,
        block: PdfSemanticBlock,
        previous_block: PdfSemanticBlock | None,
        next_block: PdfSemanticBlock | None,
        body_font_size: float,
        page_meta: PdfPageMeta | None,
    ) -> bool:
        if not self._can_anchor_following_prose(block=block, candidate=next_block):
            return False

        normalized = _SPACE_RE.sub(" ", str(text or "").strip())
        if not normalized:
            return False

        looks_sectionish = (
            bool(_CANONICAL_SECTION_RE.match(normalized))
            or self._looks_short_title(normalized)
            or self._looks_uppercase_heading_band(normalized)
        )
        if not looks_sectionish:
            return False

        if previous_block is not None:
            if int(previous_block.page_start) != int(block.page_start):
                return False
            vertical_gap = float(block.bbox.top) - float(previous_block.bbox.bottom)
            if vertical_gap < 10.0:
                return False

        if body_font_size > 0.0:
            if bool(_CANONICAL_SECTION_RE.match(normalized)) or self._looks_uppercase_heading_band(normalized):
                min_ratio = 0.98
            else:
                min_ratio = 1.08
            if float(block.avg_font_size or 0.0) < body_font_size * min_ratio:
                return False

        if self._is_first_page_top_matter(block=block, page_meta=page_meta):
            if previous_block is None:
                return False
            prev_text = _SPACE_RE.sub(" ", str(previous_block.text or "").strip())
            if len(prev_text.split()) > 8 and str(previous_block.block_type or "") != "heading":
                return False

        return True

    @staticmethod
    def _previous_heading(*, blocks: list[PdfSemanticBlock]) -> PdfSemanticBlock | None:
        for block in reversed(blocks):
            if str(block.block_type or "") == "heading":
                return block
        return None

    @staticmethod
    def _infer_heading_level(
        *,
        block: PdfSemanticBlock,
        previous_heading: PdfSemanticBlock | None,
    ) -> int:
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if _CHAPTER_HEADING_RE.match(text):
            return 1 if previous_heading is None else max(1, min(2, int(previous_heading.heading_level or 1)))
        if previous_heading is None:
            return 2
        if (
            abs(float(previous_heading.avg_font_size or 0.0) - float(block.avg_font_size or 0.0)) <= 0.6
            and abs(float(previous_heading.bbox.top) - float(block.bbox.top)) <= 18.0
        ):
            return max(1, int(previous_heading.heading_level or 1))
        previous_level = max(1, int(previous_heading.heading_level or 1))
        return min(6, previous_level + 1)

    @staticmethod
    def _looks_colon_heading(text: str) -> bool:
        if not _COLON_SECTION_RE.match(text):
            return False
        return len(text.split()) <= 8

    @staticmethod
    def _looks_chapter_heading(text: str) -> bool:
        return bool(_CHAPTER_HEADING_RE.match(text))

    def _looks_table_lead_heading(
        self,
        *,
        text: str,
        block: PdfSemanticBlock,
        next_block: PdfSemanticBlock | None,
        body_font_size: float,
    ) -> bool:
        if next_block is None or str(next_block.block_type or "") != "table":
            return False
        if not self._looks_short_title(text):
            return False
        if body_font_size <= 0.0:
            return True
        return float(block.avg_font_size or 0.0) >= body_font_size * 1.2

    def _looks_parallel_short_title_band(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        index: int,
    ) -> bool:
        block = blocks[index]
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not self._looks_short_title(text):
            return False

        peer_count = self._parallel_band_peer_count(
            blocks=blocks,
            index=index,
            text_predicate=self._looks_short_title,
        )
        if peer_count < 1:
            return False
        return self._has_following_panel_content(
            blocks=blocks,
            index=index,
            max_gap=50.0,
            min_font_ratio=1.15,
        )

    def _looks_parallel_panel_heading(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        index: int,
        body_font_size: float,
    ) -> bool:
        block = blocks[index]
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        words = [token for token in text.split() if token]
        if self._looks_short_title(text):
            return False
        if len(words) < 4 or len(words) > 12:
            return False
        if text.endswith((".", ";", ",")):
            return False
        if text.count("\n") > 1:
            return False
        if body_font_size > 0.0 and float(block.avg_font_size or 0.0) < body_font_size * 1.6:
            return False
        if self._parallel_band_peer_count(blocks=blocks, index=index, font_tolerance=1.2) < 1:
            return False
        return self._has_following_panel_content(
            blocks=blocks,
            index=index,
            require_smaller_font=True,
        )

    def _looks_heading_continuation(
        self,
        *,
        block: PdfSemanticBlock,
        previous_block: PdfSemanticBlock | None,
        body_font_size: float,
    ) -> bool:
        if previous_block is None or str(previous_block.block_type or "") != "heading":
            return False
        if int(previous_block.page_start) != int(block.page_start):
            return False
        if float(block.bbox.top) - float(previous_block.bbox.bottom) > 24.0:
            return False
        if abs(float(previous_block.avg_font_size or 0.0) - float(block.avg_font_size or 0.0)) > 1.2:
            return False
        if body_font_size > 0.0 and float(block.avg_font_size or 0.0) < body_font_size * 1.35:
            return False
        text = _SPACE_RE.sub(" ", str(block.text or "").strip())
        if not self._looks_short_title(text):
            return False
        if not self._has_horizontal_overlap(previous_block, block, min_ratio=0.35):
            return False
        return True

    def _parallel_band_peer_count(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        index: int,
        text_predicate=None,
        font_tolerance: float = 0.6,
    ) -> int:
        block = blocks[index]
        predicate = text_predicate or (lambda _text: True)
        peers = 0
        for peer_index, peer in enumerate(blocks):
            if peer_index == index:
                continue
            if str(peer.block_type or "") != "paragraph":
                continue
            if int(peer.page_start) != int(block.page_start):
                continue
            if abs(float(peer.bbox.top) - float(block.bbox.top)) > 18.0:
                continue
            if abs(float(peer.avg_font_size or 0.0) - float(block.avg_font_size or 0.0)) > float(font_tolerance):
                continue
            peer_text = _SPACE_RE.sub(" ", str(peer.text or "").strip())
            if not predicate(peer_text):
                continue
            peers += 1
        return peers

    def _has_following_panel_content(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        index: int,
        require_smaller_font: bool = False,
        max_gap: float = 96.0,
        min_font_ratio: float | None = None,
    ) -> bool:
        block = blocks[index]
        for peer in blocks[index + 1:]:
            if int(peer.page_start) != int(block.page_start):
                break
            if str(peer.column_id or "main") != str(block.column_id or "main"):
                continue
            if float(peer.bbox.top) <= float(block.bbox.bottom):
                continue
            if float(peer.bbox.top) - float(block.bbox.bottom) > float(max_gap):
                break
            peer_text = _SPACE_RE.sub(" ", str(peer.text or "").strip())
            if not peer_text:
                continue
            if self._looks_short_title(peer_text):
                if not (
                    require_smaller_font
                    and float(peer.avg_font_size or 0.0) < float(block.avg_font_size or 0.0) * 0.75
                    and len(peer_text.split()) <= 4
                ):
                    continue
            if str(peer.block_type or "") in {"caption", "footnote"}:
                continue
            if require_smaller_font and float(peer.avg_font_size or 0.0) >= float(block.avg_font_size or 0.0) * 0.85:
                continue
            if min_font_ratio is not None and float(peer.avg_font_size or 0.0) < float(block.avg_font_size or 0.0) * float(min_font_ratio):
                continue
            return True
        return False

    def _merge_adjacent_headings(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        promoted_heading_ids: set[str],
        body_font_size: float,
    ) -> list[PdfSemanticBlock]:
        if not blocks:
            return []
        merged: list[PdfSemanticBlock] = []
        index = 0
        while index < len(blocks):
            current = blocks[index]
            if str(current.block_type or "") != "heading":
                merged.append(current)
                index += 1
                continue

            while index + 1 < len(blocks):
                candidate = blocks[index + 1]
                if not self._should_merge_heading_pair(
                    current=current,
                    candidate=candidate,
                    promoted_heading_ids=promoted_heading_ids,
                    body_font_size=body_font_size,
                ):
                    break
                current = self._merge_heading_pair(current=current, candidate=candidate)
                index += 1
            merged.append(current)
            index += 1
        return merged

    def _should_merge_heading_pair(
        self,
        *,
        current: PdfSemanticBlock,
        candidate: PdfSemanticBlock,
        promoted_heading_ids: set[str],
        body_font_size: float,
    ) -> bool:
        if str(candidate.block_type or "") != "heading":
            return False
        if str(candidate.block_id or "") not in promoted_heading_ids:
            return False
        if int(current.page_start) != int(candidate.page_start):
            return False
        if float(candidate.bbox.top) - float(current.bbox.bottom) > 24.0:
            return False
        if abs(float(current.avg_font_size or 0.0) - float(candidate.avg_font_size or 0.0)) > 1.2:
            return False
        if body_font_size > 0.0 and float(candidate.avg_font_size or 0.0) < body_font_size * 1.2:
            return False
        if not self._has_horizontal_overlap(current, candidate, min_ratio=0.3):
            return False
        combined_words = len(str(current.text or "").split()) + len(str(candidate.text or "").split())
        return combined_words <= 18

    @staticmethod
    def _merge_heading_pair(
        *,
        current: PdfSemanticBlock,
        candidate: PdfSemanticBlock,
    ) -> PdfSemanticBlock:
        return replace(
            current,
            text="\n".join(
                [
                    str(current.text or "").strip(),
                    str(candidate.text or "").strip(),
                ]
            ).strip(),
            bbox=PdfBBox(
                x0=min(float(current.bbox.x0), float(candidate.bbox.x0)),
                top=min(float(current.bbox.top), float(candidate.bbox.top)),
                x1=max(float(current.bbox.x1), float(candidate.bbox.x1)),
                bottom=max(float(current.bbox.bottom), float(candidate.bbox.bottom)),
            ),
            line_ids=list(current.line_ids or []) + list(candidate.line_ids or []),
            page_end=max(int(current.page_end), int(candidate.page_end)),
            column_id=str(current.column_id or "main"),
            region=str(current.region or "main"),
            avg_font_size=max(float(current.avg_font_size or 0.0), float(candidate.avg_font_size or 0.0)),
            reading_order_end=max(int(current.reading_order_end or 0), int(candidate.reading_order_end or 0)),
            heading_level=min(
                max(1, int(current.heading_level or 1)),
                max(1, int(candidate.heading_level or current.heading_level or 1)),
            ),
        )

    @staticmethod
    def _looks_short_title(text: str) -> bool:
        if not text or text.endswith((".", ";", ",")):
            return False
        if _METAISH_RE.search(text):
            return False
        words = [token.strip(".,;:()[]{}") for token in text.split() if token.strip(".,;:()[]{}")]
        if not words or len(words) > 6:
            return False
        title_hits = sum(1 for token in words if _TITLE_TOKEN_RE.match(token))
        return title_hits >= max(1, len(words) - 1)

    @staticmethod
    def _looks_uppercase_heading_band(text: str) -> bool:
        if not text or text.endswith((".", ";", ",")):
            return False
        if _METAISH_RE.search(text):
            return False
        words = [token.strip(".,;:()[]{}") for token in text.split() if token.strip(".,;:()[]{}")]
        if len(words) < 2 or len(words) > 8:
            return False
        upper_hits = sum(1 for token in words if _ALL_CAPS_TOKEN_RE.match(token))
        stopword_hits = sum(1 for token in words if token.lower() in _HEADING_STOPWORDS)
        return upper_hits >= max(2, int(len(words) * 0.75)) and stopword_hits <= 2

    @staticmethod
    def _can_anchor_following_content(
        *,
        block: PdfSemanticBlock,
        candidate: PdfSemanticBlock | None,
    ) -> bool:
        if candidate is None:
            return False
        if str(candidate.block_type or "") in {"caption", "footnote"}:
            return False
        if int(candidate.page_start) != int(block.page_start):
            return False
        if str(candidate.column_id or "main") != str(block.column_id or "main"):
            return False
        if float(candidate.bbox.top) - float(block.bbox.bottom) > 42.0:
            return False
        return True

    def _can_anchor_following_prose(
        self,
        *,
        block: PdfSemanticBlock,
        candidate: PdfSemanticBlock | None,
    ) -> bool:
        if not self._can_anchor_following_content(block=block, candidate=candidate):
            return False
        if candidate is None:
            return False
        if str(candidate.block_type or "") != "paragraph":
            return False
        candidate_text = _SPACE_RE.sub(" ", str(candidate.text or "").strip())
        if not candidate_text or self._looks_short_title(candidate_text):
            return False
        if self._looks_uppercase_heading_band(candidate_text):
            return False
        if _METAISH_RE.search(candidate_text):
            return False
        words = [token for token in candidate_text.split() if token]
        if len(words) < 8:
            return False
        return True

    @staticmethod
    def _is_first_page_top_matter(*, block: PdfSemanticBlock, page_meta: PdfPageMeta | None) -> bool:
        if int(block.page_start) != 1 or page_meta is None:
            return False
        page_height = float(page_meta.page_height or 0.0)
        if page_height <= 0.0:
            return False
        return float(block.bbox.top) <= page_height * 0.18 and int(block.reading_order_start or 0) <= 4

    @staticmethod
    def _looks_table_like_text(text: str) -> bool:
        tokens = [
            token.strip(".,;:()[]{}")
            for token in str(text or "").split()
            if token.strip(".,;:()[]{}")
        ]
        if len(tokens) < 4:
            return False
        numeric_like = sum(1 for token in tokens if _TABLEISH_NUMBER_RE.match(token))
        marker_like = sum(1 for token in tokens if _TABLEISH_MARKER_RE.match(token))
        return numeric_like >= 2 or marker_like >= 2

    @staticmethod
    def _has_horizontal_overlap(
        left: PdfSemanticBlock,
        right: PdfSemanticBlock,
        *,
        min_ratio: float,
    ) -> bool:
        overlap = min(float(left.bbox.x1), float(right.bbox.x1)) - max(float(left.bbox.x0), float(right.bbox.x0))
        if overlap <= 0.0:
            return False
        base_width = min(float(left.bbox.width), float(right.bbox.width))
        if base_width <= 0.0:
            return False
        return overlap / base_width >= float(min_ratio)
