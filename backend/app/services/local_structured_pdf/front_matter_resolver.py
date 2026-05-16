from __future__ import annotations

from dataclasses import replace
import re

from .contracts import PdfPageMeta, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage


_SPACE_RE = re.compile(r"\s+")
_ABSTRACT_OR_SECTION_RE = re.compile(
    r"^(?:abstract|keywords?|summary|contents?)\b",
    re.IGNORECASE,
)
_NUMERIC_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)[\.\)]?\s+\S")
_LETTER_NUMBERED_SECTION_RE = re.compile(r"^[A-Z](?:\.\d+)+[\.\)]?\s+\S", re.IGNORECASE)
_APPENDIX_SECTION_RE = re.compile(r"^(?:appendix|chapter|section)\s+[A-Za-z0-9IVXLCM]+[\.\)]?\s+\S", re.IGNORECASE)
_EMAILISH_RE = re.compile(r"(?:@|https?://|www\.|doi:|arxiv:)", re.IGNORECASE)
_SYMBOL_ONLY_RE = re.compile(r"^[*†‡§¶∗]+$")
_NAME_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z'`-]+(?:\.[A-Za-z]+)?$")
_INITIAL_TOKEN_RE = re.compile(r"^[A-Z](?:\.)?$")
_NAME_OR_INITIAL_RE = re.compile(r"^(?:[A-Z][A-Za-z'`-]+(?:\.[A-Za-z]+)?|[A-Z](?:\.)?)$")

_AFFILIATION_KEYWORDS = (
    "university",
    "college",
    "institute",
    "school",
    "department",
    "faculty",
    "laboratory",
    "lab",
    "center",
    "centre",
    "hospital",
    "academy",
    "research",
    "group",
    "corporation",
    "corp",
    "inc",
    "ltd",
    "gmbh",
    "llc",
    "company",
    "press",
    "ai",
)
_PROSE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "with",
}


class LocalPdfFrontMatterResolver:
    """Stage-4.5 resolver: demote author/contact/affiliation headings on the first page."""

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        blocks = list(document.blocks or [])
        if not blocks:
            return document

        page_meta_map = {
            int(page.page): page.meta
            for page in list(document.pages or [])
        }
        blocks = self._reorder_first_page_front_matter(
            blocks=blocks,
            page_meta=page_meta_map.get(1),
        )
        resolved_blocks: list[PdfSemanticBlock] = []
        seen_title_heading = False
        section_started = False
        title_block: PdfSemanticBlock | None = None
        for block in blocks:
            if int(block.page_start) != 1 or int(block.page_end) != 1:
                resolved_blocks.append(block)
                continue

            block_type = str(block.block_type or "")
            text = _SPACE_RE.sub(" ", str(block.text or "").strip())
            if not text:
                resolved_blocks.append(block)
                continue

            if block_type == "heading" and not seen_title_heading:
                seen_title_heading = True
                title_block = block
                resolved_blocks.append(block)
                continue

            if block_type == "heading" and self._is_primary_section_heading(text):
                section_started = True
                resolved_blocks.append(block)
                continue

            if (
                not section_started
                and block_type == "heading"
                and self._should_demote_front_matter_heading(
                    block=block,
                    text=text,
                    title_block=title_block,
                    page_meta=page_meta_map.get(1),
                )
            ):
                resolved_blocks.append(
                    replace(
                        block,
                        block_type="paragraph",
                        heading_level=None,
                    )
                )
                continue

            resolved_blocks.append(block)

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

    def _reorder_first_page_front_matter(
        self,
        *,
        blocks: list[PdfSemanticBlock],
        page_meta: PdfPageMeta | None,
    ) -> list[PdfSemanticBlock]:
        if page_meta is None:
            return blocks

        page_one_indices = [
            index
            for index, block in enumerate(blocks)
            if int(block.page_start) == 1 and int(block.page_end) == 1
        ]
        if not page_one_indices:
            return blocks

        page_one_blocks = [blocks[index] for index in page_one_indices]
        first_section_position = next(
            (
                index
                for index, block in enumerate(page_one_blocks)
                if str(block.block_type or "") == "heading"
                and self._is_primary_section_heading(str(block.text or ""))
            ),
            None,
        )
        if first_section_position is None:
            return blocks

        first_section_block = page_one_blocks[first_section_position]
        first_section_top = float(first_section_block.bbox.top)
        page_height = float(page_meta.page_height or 0.0)
        page_width = float(page_meta.page_width or 0.0)
        upper_page_limit = page_height * 0.62 if page_height > 0.0 else None

        title_candidates = [
            block
            for block in page_one_blocks
            if str(block.block_type or "") == "heading"
            and float(block.bbox.top) < first_section_top
            and (upper_page_limit is None or float(block.bbox.bottom) <= upper_page_limit)
            and (
                float(block.avg_font_size or 0.0) >= float(first_section_block.avg_font_size or 0.0) * 1.08
                or (page_width > 0.0 and float(block.bbox.width) >= page_width * 0.45)
            )
        ]
        if not title_candidates:
            return blocks
        if not self._looks_reorderable_front_matter_band(
            page_one_blocks=page_one_blocks,
            title_candidates=title_candidates,
            first_section_top=first_section_top,
            upper_page_limit=upper_page_limit,
        ):
            return blocks
        title_anchor_bottom = max(float(block.bbox.bottom) for block in title_candidates)
        front_cluster = [
            block
            for block in page_one_blocks
            if float(block.bbox.top) < first_section_top
            and (upper_page_limit is None or float(block.bbox.bottom) <= upper_page_limit)
            and (
                block in title_candidates
                or float(block.bbox.top) <= title_anchor_bottom + 160.0
            )
        ]
        if len(front_cluster) < 2:
            return blocks

        original_positions = {block.block_id: index for index, block in enumerate(page_one_blocks)}
        if all(original_positions.get(block.block_id, 0) < first_section_position for block in front_cluster):
            return blocks

        front_ids = {block.block_id for block in front_cluster}
        front_sorted = sorted(
            front_cluster,
            key=lambda block: (
                round(float(block.bbox.top), 2),
                round(float(block.bbox.x0), 2),
                int(block.reading_order_start or 0),
                str(block.block_id or ""),
            ),
        )
        before_section = [
            block
            for block in page_one_blocks
            if block.block_id not in front_ids
            and original_positions.get(block.block_id, 0) < first_section_position
        ]
        from_section_onward = [
            block
            for block in page_one_blocks
            if block.block_id not in front_ids
            and original_positions.get(block.block_id, 0) >= first_section_position
        ]
        reordered_page_one = front_sorted + before_section + from_section_onward
        reordered_blocks = list(blocks)
        for index, block in zip(page_one_indices, reordered_page_one):
            reordered_blocks[index] = block
        return reordered_blocks

    def _looks_reorderable_front_matter_band(
        self,
        *,
        page_one_blocks: list[PdfSemanticBlock],
        title_candidates: list[PdfSemanticBlock],
        first_section_top: float,
        upper_page_limit: float | None,
    ) -> bool:
        if len(title_candidates) >= 2:
            return True

        pre_section_blocks = [
            block
            for block in page_one_blocks
            if float(block.bbox.top) < first_section_top
            and (upper_page_limit is None or float(block.bbox.bottom) <= upper_page_limit)
        ]
        for block in pre_section_blocks:
            text = _SPACE_RE.sub(" ", str(block.text or "").strip())
            if not text:
                continue
            if _ABSTRACT_OR_SECTION_RE.match(text):
                return True
            if self._looks_symbol_only_meta(text):
                return True
            if self._looks_email_or_contact_line(text):
                return True
            if self._looks_author_line(text):
                return True
            if self._looks_affiliation_or_contact_line(text):
                return True
        return False

    def _should_demote_front_matter_heading(
        self,
        *,
        block: PdfSemanticBlock,
        text: str,
        title_block: PdfSemanticBlock | None,
        page_meta: PdfPageMeta | None,
    ) -> bool:
        if not text:
            return False
        if self._looks_symbol_only_meta(text):
            return True
        if self._looks_email_or_contact_line(text):
            return True
        if page_meta is not None and not self._is_upper_page_block(block=block, page_meta=page_meta):
            return False
        if self._looks_author_line(text):
            return True
        if self._looks_title_adjacent_meta_band(block=block, title_block=title_block, page_meta=page_meta):
            return self._looks_name_like_band(text) or self._looks_affiliation_or_contact_line(text)
        return self._looks_affiliation_or_contact_line(text)

    @staticmethod
    def _is_primary_section_heading(text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not token:
            return False
        if _ABSTRACT_OR_SECTION_RE.match(token):
            return True
        if _APPENDIX_SECTION_RE.match(token):
            return True
        if _LETTER_NUMBERED_SECTION_RE.match(token):
            return True
        numeric_match = _NUMERIC_SECTION_RE.match(token)
        if numeric_match:
            prefix = str(numeric_match.group(1) or "")
            segments = [part for part in prefix.split(".") if part]
            if segments and segments[0] != "0" and all(len(part) <= 3 for part in segments):
                return True
        return False

    def _looks_author_line(self, text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not token or _EMAILISH_RE.search(token):
            return False
        if token.endswith((".", ":", ";")):
            return False

        words = [self._strip_name_token(part) for part in token.split() if self._strip_name_token(part)]
        if len(words) < 4 or len(words) > 24:
            return False
        prose_hits = sum(1 for part in words if part.lower() in _PROSE_STOPWORDS)
        if prose_hits > 1:
            return False
        name_like = sum(
            1
            for part in words
            if _NAME_TOKEN_RE.match(part) or _INITIAL_TOKEN_RE.match(part)
        )
        if token.count(",") >= 1 and name_like >= max(3, int(len(words) * 0.65)):
            return True
        return name_like >= max(4, int(len(words) * 0.8))

    def _looks_affiliation_or_contact_line(self, text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not token:
            return False
        lowered = token.lower()
        if any(keyword in lowered for keyword in _AFFILIATION_KEYWORDS):
            return len(token.split()) <= 16
        return False

    @staticmethod
    def _looks_email_or_contact_line(text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        return bool(token and _EMAILISH_RE.search(token))

    @staticmethod
    def _looks_symbol_only_meta(text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        return bool(token and _SYMBOL_ONLY_RE.match(token))

    def _looks_name_like_band(self, text: str) -> bool:
        token = _SPACE_RE.sub(" ", str(text or "").strip())
        if not token or token.endswith((".", ";", ":")):
            return False
        words = [self._strip_name_token(part) for part in token.split() if self._strip_name_token(part)]
        if len(words) < 2 or len(words) > 16:
            return False
        name_like = sum(1 for part in words if _NAME_OR_INITIAL_RE.match(part))
        prose_hits = sum(1 for part in words if part.lower() in _PROSE_STOPWORDS)
        return prose_hits <= 1 and name_like >= max(2, int(len(words) * 0.7))

    @staticmethod
    def _is_upper_page_block(*, block: PdfSemanticBlock, page_meta: PdfPageMeta) -> bool:
        page_height = float(page_meta.page_height or 0.0)
        if page_height <= 0.0:
            return True
        return float(block.bbox.bottom) <= page_height * 0.55

    def _looks_title_adjacent_meta_band(
        self,
        *,
        block: PdfSemanticBlock,
        title_block: PdfSemanticBlock | None,
        page_meta: PdfPageMeta | None,
    ) -> bool:
        if title_block is None:
            return False
        if float(block.bbox.top) <= float(title_block.bbox.bottom):
            return False
        if float(block.bbox.top) - float(title_block.bbox.bottom) > 144.0:
            return False
        if float(block.avg_font_size or 0.0) > float(title_block.avg_font_size or 0.0) * 0.92:
            return False
        if page_meta is None:
            return True
        page_width = float(page_meta.page_width or 0.0)
        if page_width <= 0.0:
            return True
        title_center = (float(title_block.bbox.x0) + float(title_block.bbox.x1)) / 2.0
        block_center = (float(block.bbox.x0) + float(block.bbox.x1)) / 2.0
        center_delta = abs(title_center - block_center)
        title_width = max(1.0, float(title_block.bbox.x1) - float(title_block.bbox.x0))
        block_width = max(1.0, float(block.bbox.x1) - float(block.bbox.x0))
        return center_delta <= page_width * 0.12 and block_width <= max(page_width * 0.82, title_width * 1.05)

    @staticmethod
    def _strip_name_token(text: str) -> str:
        return str(text or "").strip(" ,;:()[]{}*†‡§¶∗")
