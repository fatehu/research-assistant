from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

from .contracts import PdfBBox, PdfImageAtom, PdfPageAtoms, PdfSemanticBlock, PdfStructuredDocument
from .native_extractor import LocalPdfNativeExtractor
from .ollama_page_parser import LocalOllamaQwenVlPageParser


@dataclass(frozen=True)
class PdfPictureDescription:
    page: int
    bbox: PdfBBox
    description: str
    model: str = ""
    source: str = "qwen_picture_enrich"


class LocalPdfPictureEnrichmentService:
    """Generate upstream-style picture descriptions as a separate enrich stage.

    Upstream `opendataloader-pdf` enables picture description after picture objects
    are materialized. This service keeps the adapter minimal:
    1. collect picture candidates from extracted image atoms
    2. fallback to figure blocks already present in structured output
    3. ask local Qwen VL for description text
    4. write descriptions into `pictures[].annotations[]`
    """

    def __init__(
        self,
        *,
        extractor: LocalPdfNativeExtractor | None = None,
        page_parser: LocalOllamaQwenVlPageParser | None = None,
    ) -> None:
        self._extractor = extractor or LocalPdfNativeExtractor()
        self._page_parser = page_parser or LocalOllamaQwenVlPageParser()

    async def enrich_document(
        self,
        *,
        pdf_path: str,
        document: PdfStructuredDocument,
        picture_description_prompt: str | None = None,
        page_numbers: set[int] | None = None,
        page_atoms: list[PdfPageAtoms] | None = None,
    ) -> list[PdfPictureDescription]:
        available_page_atoms = page_atoms
        if available_page_atoms is None:
            available_page_atoms = await asyncio.to_thread(
                self._extractor.extract_document_atoms,
                pdf_path=pdf_path,
            )
        if not available_page_atoms:
            return []

        descriptions: list[PdfPictureDescription] = []
        for page_atoms_item in available_page_atoms:
            if page_numbers is not None and int(page_atoms_item.page) not in page_numbers:
                continue
            for candidate in self._collect_picture_candidates(
                page_atoms=page_atoms_item,
                document=document,
            ):
                description, model = await self._page_parser.describe_picture_region(
                    pdf_path=pdf_path,
                    page=int(page_atoms_item.page),
                    bbox=candidate,
                    prompt=picture_description_prompt,
                )
                description = str(description or "").strip()
                if not description:
                    continue
                descriptions.append(
                    PdfPictureDescription(
                        page=int(page_atoms_item.page),
                        bbox=candidate,
                        description=description,
                        model=str(model or "").strip(),
                    )
                )
        return descriptions

    def _collect_picture_candidates(
        self,
        *,
        page_atoms: PdfPageAtoms,
        document: PdfStructuredDocument,
    ) -> list[PdfBBox]:
        page_width = max(1.0, float(page_atoms.meta.page_width or 0.0))
        page_height = max(1.0, float(page_atoms.meta.page_height or 0.0))
        page_area = max(1.0, page_width * page_height)

        candidates: list[PdfBBox] = []
        for image in list(getattr(page_atoms, "images", []) or []):
            if not isinstance(image, PdfImageAtom):
                continue
            bbox = image.bbox
            if bbox.width < 48.0 or bbox.height < 48.0:
                continue
            if (bbox.width * bbox.height) / page_area < 0.015:
                continue
            candidates.append(bbox)

        if not candidates:
            for block in self._iter_figure_blocks(document=document, page=int(page_atoms.page)):
                bbox = block.bbox
                if bbox.width < 48.0 or bbox.height < 48.0:
                    continue
                candidates.append(bbox)

        if not candidates:
            candidates.extend(self._collect_graphic_region_candidates(page_atoms=page_atoms))

        if not candidates:
            # Keep a minimal upstream-compatible fallback: if enrich is enabled but
            # no picture object was detected, provide one coarse page visual region.
            margin_x = max(24.0, page_width * 0.08)
            margin_y = max(24.0, page_height * 0.08)
            candidates.append(
                PdfBBox(
                    x0=margin_x,
                    top=margin_y,
                    x1=max(margin_x + 48.0, page_width - margin_x),
                    bottom=max(margin_y + 48.0, page_height - margin_y),
                )
            )

        return self._merge_candidate_bboxes(candidates)

    @staticmethod
    def _iter_figure_blocks(*, document: PdfStructuredDocument, page: int) -> Iterable[PdfSemanticBlock]:
        for block in list(getattr(document, "blocks", []) or []):
            if int(getattr(block, "page_start", 0) or 0) != int(page):
                continue
            if str(getattr(block, "block_type", "") or "").strip().lower() != "figure_meta":
                continue
            yield block

    def _merge_candidate_bboxes(self, candidates: list[PdfBBox]) -> list[PdfBBox]:
        merged: list[PdfBBox] = []
        for bbox in sorted(candidates, key=lambda item: (round(float(item.top), 2), round(float(item.x0), 2))):
            attached = False
            for index, current in enumerate(list(merged)):
                if self._should_merge(left=current, right=bbox):
                    merged[index] = PdfBBox(
                        x0=min(float(current.x0), float(bbox.x0)),
                        top=min(float(current.top), float(bbox.top)),
                        x1=max(float(current.x1), float(bbox.x1)),
                        bottom=max(float(current.bottom), float(bbox.bottom)),
                    )
                    attached = True
                    break
            if not attached:
                merged.append(bbox)
        return merged

    @staticmethod
    def _collect_graphic_region_candidates(*, page_atoms: PdfPageAtoms) -> list[PdfBBox]:
        page_width = max(1.0, float(page_atoms.meta.page_width or 0.0))
        page_height = max(1.0, float(page_atoms.meta.page_height or 0.0))
        page_area = max(1.0, page_width * page_height)
        candidates: list[PdfBBox] = []
        for atom in [*list(getattr(page_atoms, "rects", []) or []), *list(getattr(page_atoms, "curves", []) or [])]:
            bbox = getattr(atom, "bbox", None)
            if not isinstance(bbox, PdfBBox):
                continue
            if bbox.width < 24.0 or bbox.height < 24.0:
                continue
            if (bbox.width * bbox.height) / page_area < 0.004:
                continue
            candidates.append(bbox)
        return candidates

    @staticmethod
    def _should_merge(*, left: PdfBBox, right: PdfBBox) -> bool:
        horizontal_gap = max(0.0, max(float(left.x0), float(right.x0)) - min(float(left.x1), float(right.x1)))
        vertical_gap = max(0.0, max(float(left.top), float(right.top)) - min(float(left.bottom), float(right.bottom)))
        overlap_width = max(0.0, min(float(left.x1), float(right.x1)) - max(float(left.x0), float(right.x0)))
        overlap_height = max(0.0, min(float(left.bottom), float(right.bottom)) - max(float(left.top), float(right.top)))
        min_width = max(1.0, min(float(left.width), float(right.width)))
        min_height = max(1.0, min(float(left.height), float(right.height)))
        overlap_ratio = (overlap_width / min_width >= 0.3) and (overlap_height / min_height >= 0.3)
        close_stack = overlap_width / min_width >= 0.6 and vertical_gap <= 32.0
        close_row = overlap_height / min_height >= 0.6 and horizontal_gap <= 32.0
        return overlap_ratio or close_stack or close_row
