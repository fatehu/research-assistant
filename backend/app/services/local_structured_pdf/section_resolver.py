from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from .contracts import (
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


class LocalPdfSectionResolver:
    """Stage-4 resolver: propagate heading hierarchy and section context across pages."""

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        blocks = list(document.blocks or [])
        active_headings: list[PdfSemanticBlock] = []
        resolved_blocks: list[PdfSemanticBlock] = []

        for block in blocks:
            if str(block.block_type or "") == "heading":
                level = max(1, int(block.heading_level or 1))
                active_headings = active_headings[: level - 1]
                parent_heading_id = active_headings[-1].block_id if active_headings else None
                section_heading_ids = [item.block_id for item in active_headings] + [block.block_id]
                section_titles = [item.text for item in active_headings] + [block.text]
                resolved = replace(
                    block,
                    parent_heading_id=parent_heading_id,
                    section_heading_ids=section_heading_ids,
                    section_titles=section_titles,
                    section_path=self._build_section_path(section_titles),
                )
                active_headings.append(resolved)
                resolved_blocks.append(resolved)
                continue

            parent_heading_id = active_headings[-1].block_id if active_headings else None
            section_heading_ids = [item.block_id for item in active_headings]
            section_titles = [item.text for item in active_headings]
            resolved_blocks.append(
                replace(
                    block,
                    parent_heading_id=parent_heading_id,
                    section_heading_ids=section_heading_ids,
                    section_titles=section_titles,
                    section_path=self._build_section_path(section_titles),
                )
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

    @staticmethod
    def _build_section_path(section_titles: Sequence[str]) -> str:
        titles = [str(title or "").strip() for title in list(section_titles or []) if str(title or "").strip()]
        return " > ".join(titles)
