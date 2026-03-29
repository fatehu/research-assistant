from __future__ import annotations

from typing import Any


_VALID_BLOCK_KINDS = {
    "heading",
    "paragraph",
    "list_item",
    "caption",
    "table",
    "equation",
    "figure_meta",
    "footnote",
    "unknown",
}
_VALID_ZONES = {"main", "side", "figure", "table", "footer", "header", "unknown"}
_VALID_MERGE_STRATEGIES = {"space", "newline"}
_ELEMENT_LABEL_TO_KIND = {
    "caption": "caption",
    "equation": "equation",
    "figure": "figure_meta",
    "figure_meta": "figure_meta",
    "formula": "equation",
    "footnote": "footnote",
    "heading": "heading",
    "image": "figure_meta",
    "list": "list_item",
    "list_item": "list_item",
    "page_footer": "unknown",
    "page_header": "unknown",
    "paragraph": "paragraph",
    "picture": "figure_meta",
    "section_header": "heading",
    "table": "table",
    "text": "paragraph",
    "title": "heading",
    "unknown": "unknown",
}
_KIND_DEFAULT_ZONE = {
    "caption": "figure",
    "equation": "main",
    "figure_meta": "figure",
    "footnote": "footer",
    "heading": "main",
    "list_item": "main",
    "paragraph": "main",
    "table": "table",
    "unknown": "unknown",
}
_KIND_DEFAULT_MERGE = {
    "caption": "space",
    "equation": "space",
    "figure_meta": "newline",
    "footnote": "space",
    "heading": "space",
    "list_item": "space",
    "paragraph": "space",
    "table": "newline",
    "unknown": "space",
}
_FURNITURE_LABELS = {"page_header", "page_footer"}


class LocalPdfHybridBackendTransformer:
    """Normalize loose backend/model output into internal hybrid blocks.

    This intentionally mirrors the Java hybrid pattern:
    backend returns a looser structure, then Python transforms it into
    the repository's internal block contract.
    """

    def transform_payload(
        self,
        *,
        payload: dict[str, Any],
        prompt_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        blocks = self._transform_docling_payload_to_blocks(payload=payload, prompt_payload=prompt_payload)
        if not isinstance(blocks, list):
            blocks = self._transform_loose_elements_to_blocks(payload=payload, prompt_payload=prompt_payload)
        if not isinstance(blocks, list):
            blocks = self._tag_raw_blocks(blocks=payload.get("blocks"))
        if not isinstance(blocks, list):
            return None

        line_rows_by_id = self._line_rows_by_id(prompt_payload=prompt_payload)
        valid_line_ids = {
            str(item.get("line_id") or "").strip()
            for item in list(prompt_payload.get("line_rows") or [])
            if isinstance(item, dict) and str(item.get("line_id") or "").strip()
        }
        allow_unanchored_text = self._allow_unanchored_text(prompt_payload=prompt_payload)
        allow_bbox_line_inference = self._allow_bbox_line_inference(prompt_payload=prompt_payload)
        normalized_blocks: list[dict[str, Any]] = []
        seen_line_ids: set[str] = set()
        for index, block in enumerate(blocks, start=1):
            if not isinstance(block, dict):
                continue
            uses_docling_payload = self._is_docling_block(block=block)
            kind = str(block.get("kind") or "unknown").strip().lower()
            if kind not in _VALID_BLOCK_KINDS:
                kind = "unknown"
            zone = str(block.get("zone") or "main").strip().lower()
            if zone not in _VALID_ZONES:
                zone = "unknown"
            merge_strategy = str(block.get("merge_strategy") or "space").strip().lower()
            if merge_strategy not in _VALID_MERGE_STRATEGIES:
                merge_strategy = "space"

            source_line_ids = [
                str(item).strip()
                for item in list(block.get("source_line_ids") or [])
                if str(item).strip()
            ]
            raw_text = self._extract_text(block)
            raw_bbox = self._extract_bbox(block=block, prompt_payload=prompt_payload)
            source_line_ids = [item for item in source_line_ids if item in valid_line_ids]

            # Docling backend payloads already provide their own block text/bbox semantics.
            # Keep those blocks direct and avoid re-anchoring them back onto local line_rows.
            if (
                not uses_docling_payload
                and allow_bbox_line_inference
                and not source_line_ids
                and isinstance(raw_bbox, dict)
            ):
                inferred = self._infer_source_line_ids_from_bbox(bbox=raw_bbox, prompt_payload=prompt_payload)
                if inferred:
                    source_line_ids = [item for item in inferred if item in valid_line_ids]

            # Loose/raw model outputs may still need local line_rows to complete text/bbox. For
            # Docling payloads, preserve the backend-provided block directly.
            if not uses_docling_payload and source_line_ids:
                if not raw_text:
                    raw_text = self._materialize_text_from_line_rows(
                        source_line_ids=source_line_ids,
                        line_rows_by_id=line_rows_by_id,
                        merge_strategy=merge_strategy,
                    )
                if raw_bbox is None:
                    raw_bbox = self._merge_bboxes_from_line_rows(
                        source_line_ids=source_line_ids,
                        line_rows_by_id=line_rows_by_id,
                    )

            deduped_line_ids: list[str] = []
            for item in source_line_ids:
                if item in seen_line_ids or item in deduped_line_ids:
                    continue
                deduped_line_ids.append(item)
            source_line_ids = deduped_line_ids
            if not source_line_ids:
                if not (allow_unanchored_text and (raw_text or kind == "figure_meta")):
                    continue
                if raw_bbox is None and not uses_docling_payload:
                    raw_bbox = self._page_bbox(prompt_payload=prompt_payload)
            seen_line_ids.update(source_line_ids)

            normalized_blocks.append(
                {
                    "block_id": str(block.get("block_id") or f"mm_p{int(prompt_payload.get('page') or 0):04d}_b{index:04d}"),
                    "kind": kind,
                    "reading_order": max(1, int(block.get("reading_order") or index)),
                    "source_line_ids": source_line_ids,
                    "table_rows": [list(row) for row in list(block.get("table_rows") or [])]
                    if kind == "table"
                    else [],
                    "zone": zone,
                    "merge_strategy": merge_strategy,
                    "confidence": max(0.0, min(1.0, float(block.get("confidence") or 0.0))),
                    "text": raw_text,
                    "bbox": raw_bbox,
                    "_backend_origin": block.get("_backend_origin"),
                    "_docling_label": block.get("_docling_label"),
                    "heading_level": (
                        self._coerce_positive_int(block.get("heading_level")) if kind == "heading" else None
                    ),
                }
            )

        if not normalized_blocks:
            return None
        if all(self._is_docling_block(block=item) for item in normalized_blocks) and self._should_preserve_docling_order(
            blocks=normalized_blocks,
            prompt_payload=prompt_payload,
        ):
            normalized_blocks = self._preserve_block_order(normalized_blocks)
        else:
            normalized_blocks = self._sort_blocks_by_reading_order(normalized_blocks)
        return {
            "page": int(payload.get("page") or prompt_payload.get("page") or 0),
            "page_role": str(payload.get("page_role") or payload.get("document_role") or "unknown").strip().lower() or "unknown",
            "blocks": normalized_blocks,
            "notes": [
                str(item).strip()
                for item in list(payload.get("notes") or [])[:24]
                if str(item).strip()
            ],
        }

    def _transform_docling_payload_to_blocks(
        self,
        *,
        payload: dict[str, Any],
        prompt_payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        elements = self._collect_docling_like_elements(payload=payload, prompt_payload=prompt_payload)
        if not isinstance(elements, list):
            return None
        return self._normalize_elements_to_blocks(elements=elements, payload=payload, origin="docling")

    def _transform_loose_elements_to_blocks(
        self,
        *,
        payload: dict[str, Any],
        prompt_payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        elements = payload.get("elements")
        if not isinstance(elements, list):
            return None
        filtered_elements: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            filtered = self._filter_element_by_page(element=element, prompt_payload=prompt_payload)
            if filtered is not None:
                filtered_elements.append(filtered)
        if not filtered_elements:
            return None
        return self._normalize_elements_to_blocks(elements=filtered_elements, payload=payload, origin="loose")

    def _normalize_elements_to_blocks(
        self,
        *,
        elements: list[dict[str, Any]],
        payload: dict[str, Any],
        origin: str,
    ) -> list[dict[str, Any]] | None:
        normalized: list[dict[str, Any]] = []
        for index, element in enumerate(elements, start=1):
            if not isinstance(element, dict):
                continue
            label = str(
                element.get("type")
                or element.get("label")
                or element.get("kind")
                or "unknown"
            ).strip().lower()
            if label in _FURNITURE_LABELS:
                continue
            kind = _ELEMENT_LABEL_TO_KIND.get(label, "unknown")
            line_ids = [
                str(item).strip()
                for item in list(
                    element.get("line_ids")
                    or element.get("source_line_ids")
                    or element.get("lines")
                    or []
                )
                if str(item).strip()
            ]
            zone = str(element.get("zone") or _KIND_DEFAULT_ZONE.get(kind, "unknown")).strip().lower()
            if zone not in _VALID_ZONES:
                zone = _KIND_DEFAULT_ZONE.get(kind, "unknown")
            merge_strategy = str(
                element.get("merge_strategy") or _KIND_DEFAULT_MERGE.get(kind, "space")
            ).strip().lower()
            if merge_strategy not in _VALID_MERGE_STRATEGIES:
                merge_strategy = _KIND_DEFAULT_MERGE.get(kind, "space")
            normalized.append(
                {
                    "block_id": str(
                        element.get("block_id")
                        or f"mm_p{int(payload.get('page') or 0):04d}_b{index:04d}"
                    ),
                    "kind": kind,
                    "reading_order": max(1, int(element.get("reading_order") or index)),
                    "source_line_ids": line_ids,
                    "table_rows": self._extract_table_rows(element) if kind == "table" else [],
                    "zone": zone,
                    "merge_strategy": merge_strategy,
                    "confidence": max(0.0, min(1.0, float(element.get("confidence") or 0.0))),
                    "text": self._extract_text(element),
                    "bbox": element.get("bbox"),
                    "prov": element.get("prov"),
                    "heading_level": self._extract_heading_level(element) if kind == "heading" else None,
                    "_backend_origin": origin,
                    "_docling_label": label if origin == "docling" else None,
                }
            )
        return normalized or None

    @staticmethod
    def _tag_raw_blocks(*, blocks: Any) -> list[dict[str, Any]] | None:
        if not isinstance(blocks, list):
            return None
        tagged: list[dict[str, Any]] = []
        for item in blocks:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault("_backend_origin", "raw")
            tagged.append(copied)
        return tagged or None

    @staticmethod
    def _is_docling_block(*, block: dict[str, Any]) -> bool:
        return str(block.get("_backend_origin") or "").strip().lower() == "docling"

    def _collect_docling_like_elements(
        self,
        *,
        payload: dict[str, Any],
        prompt_payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        collected: list[dict[str, Any]] = []
        for field_name, default_label in (
            ("texts", "text"),
            ("tables", "table"),
            ("pictures", "picture"),
        ):
            rows = payload.get(field_name)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                element = dict(row)
                if not str(element.get("type") or element.get("label") or "").strip():
                    element["label"] = default_label
                filtered = self._filter_element_by_page(element=element, prompt_payload=prompt_payload)
                if filtered is not None:
                    collected.append(filtered)
        return collected or None

    @staticmethod
    def _filter_element_by_page(*, element: dict[str, Any], prompt_payload: dict[str, Any]) -> dict[str, Any] | None:
        target_page = int(prompt_payload.get("page") or 0)
        prov = element.get("prov")
        if not isinstance(prov, list) or not prov:
            return element
        filtered_prov = [
            dict(item)
            for item in prov
            if isinstance(item, dict) and int(item.get("page_no") or 0) == target_page
        ]
        if not filtered_prov:
            return None
        copied = dict(element)
        copied["prov"] = filtered_prov
        return copied

    @staticmethod
    def _sort_blocks_by_reading_order(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(item: dict[str, Any]) -> tuple[float, float, int, str]:
            bbox = item.get("bbox")
            if isinstance(bbox, dict):
                try:
                    top = round(float(bbox.get("top")), 1)
                    x0 = round(float(bbox.get("x0")), 1)
                    band_top = round(top / 5.0) * 5.0
                    return (band_top, x0, int(item.get("reading_order") or 0), str(item.get("block_id") or ""))
                except (TypeError, ValueError):
                    pass
            return (float("inf"), float("inf"), int(item.get("reading_order") or 0), str(item.get("block_id") or ""))

        ordered = sorted(blocks, key=sort_key)
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(ordered, start=1):
            copied = dict(item)
            copied["reading_order"] = index
            normalized.append(copied)
        return normalized

    @staticmethod
    def _preserve_block_order(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(list(blocks or []), start=1):
            copied = dict(item)
            copied["reading_order"] = index
            normalized.append(copied)
        return normalized

    @staticmethod
    def _should_preserve_docling_order(
        *,
        blocks: list[dict[str, Any]],
        prompt_payload: dict[str, Any],
    ) -> bool:
        kinds = {str(item.get("kind") or "").strip().lower() for item in list(blocks or [])}
        if "table" in kinds:
            return False
        labels = [str(item.get("_docling_label") or "").strip().lower() for item in list(blocks or [])]
        if "footnote" in labels:
            return False

        figure_markers = [
            index
            for index, item in enumerate(list(blocks or []))
            if str(item.get("kind") or "").strip().lower() == "figure_meta"
            or str(item.get("_docling_label") or "").strip().lower() in {"caption", "figure", "figure_meta", "image", "picture"}
        ]
        if not figure_markers:
            return False

        if len(figure_markers) == 1:
            prose_before_first_figure = sum(
                1
                for item in list(blocks or [])[: figure_markers[0]]
                if LocalPdfHybridBackendTransformer._is_docling_prose_anchor(item)
            )
            return prose_before_first_figure >= 2

        anchor_before_last_figure = sum(
            1
            for item in list(blocks or [])[: figure_markers[-1]]
            if LocalPdfHybridBackendTransformer._is_docling_non_caption_anchor(item)
        )
        return anchor_before_last_figure >= 2

    @staticmethod
    def _is_docling_prose_anchor(block: dict[str, Any]) -> bool:
        label = str(block.get("_docling_label") or "").strip().lower()
        text = " ".join(str(block.get("text") or "").strip().split())
        if label in {"section_header", "title"}:
            return True
        if label not in {"paragraph", "text"}:
            return False
        return len(text) >= 80 and len(text.split()) >= 10

    @staticmethod
    def _is_docling_non_caption_anchor(block: dict[str, Any]) -> bool:
        label = str(block.get("_docling_label") or "").strip().lower()
        if label in {"caption", "figure", "figure_meta", "image", "picture"}:
            return False
        return LocalPdfHybridBackendTransformer._is_docling_prose_anchor(block)

    @staticmethod
    def _allow_unanchored_text(*, prompt_payload: dict[str, Any]) -> bool:
        return True

    @staticmethod
    def _allow_bbox_line_inference(*, prompt_payload: dict[str, Any]) -> bool:
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()
        # For visual/scanned pages we prefer OCR-style unanchored regions. Inferring anchors from
        # sparse residual line rows tends to collapse back to the degraded extraction.
        return page_type not in {"visual_or_scanned"}

    @staticmethod
    def _line_rows_by_id(*, prompt_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = [item for item in list(prompt_payload.get("line_rows") or []) if isinstance(item, dict)]
        mapped: dict[str, dict[str, Any]] = {}
        for row in rows:
            line_id = str(row.get("line_id") or "").strip()
            if not line_id:
                continue
            mapped[line_id] = row
        return mapped

    @staticmethod
    def _materialize_text_from_line_rows(
        *,
        source_line_ids: list[str],
        line_rows_by_id: dict[str, dict[str, Any]],
        merge_strategy: str,
    ) -> str:
        rows: list[str] = []
        for line_id in source_line_ids:
            row = line_rows_by_id.get(str(line_id))
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if text:
                rows.append(text)
        if not rows:
            return ""
        if merge_strategy == "newline":
            return "\n".join(rows).strip()
        return " ".join(rows).strip()

    def _merge_bboxes_from_line_rows(
        self,
        *,
        source_line_ids: list[str],
        line_rows_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, float] | None:
        bboxes: list[dict[str, float]] = []
        for line_id in source_line_ids:
            row = line_rows_by_id.get(str(line_id))
            if not isinstance(row, dict):
                continue
            bbox = self._normalize_bbox(row.get("bbox"))
            if bbox is not None:
                bboxes.append(bbox)
        if not bboxes:
            return None
        return {
            "x0": min(float(item["x0"]) for item in bboxes),
            "top": min(float(item["top"]) for item in bboxes),
            "x1": max(float(item["x1"]) for item in bboxes),
            "bottom": max(float(item["bottom"]) for item in bboxes),
        }

    def _infer_source_line_ids_from_bbox(self, *, bbox: dict[str, Any], prompt_payload: dict[str, Any]) -> list[str]:
        normalized = self._normalize_bbox(bbox)
        if normalized is None:
            return []
        page_width = float(prompt_payload.get("page_width") or 0.0)
        page_height = float(prompt_payload.get("page_height") or 0.0)
        page_area = max(1.0, page_width * page_height)
        bbox_area = max(0.0, (float(normalized["x1"]) - float(normalized["x0"])) * (float(normalized["bottom"]) - float(normalized["top"])))
        if bbox_area / page_area >= 0.85:
            return []

        matched: list[tuple[int, str]] = []
        for row in list(prompt_payload.get("line_rows") or []):
            if not isinstance(row, dict):
                continue
            line_id = str(row.get("line_id") or "").strip()
            if not line_id:
                continue
            line_bbox = self._normalize_bbox(row.get("bbox"))
            if line_bbox is None:
                continue
            if self._line_bbox_matches_bbox(line_bbox=line_bbox, bbox=normalized):
                matched.append((int(row.get("reading_order") or 0), line_id))
        matched.sort(key=lambda item: (item[0], item[1]))
        return [line_id for _, line_id in matched]

    @staticmethod
    def _line_bbox_matches_bbox(*, line_bbox: dict[str, float], bbox: dict[str, float]) -> bool:
        inter_left = max(float(line_bbox["x0"]), float(bbox["x0"]))
        inter_top = max(float(line_bbox["top"]), float(bbox["top"]))
        inter_right = min(float(line_bbox["x1"]), float(bbox["x1"]))
        inter_bottom = min(float(line_bbox["bottom"]), float(bbox["bottom"]))
        inter_w = max(0.0, inter_right - inter_left)
        inter_h = max(0.0, inter_bottom - inter_top)
        if inter_w <= 0.0 or inter_h <= 0.0:
            return False

        line_width = max(1.0, float(line_bbox["x1"]) - float(line_bbox["x0"]))
        line_height = max(1.0, float(line_bbox["bottom"]) - float(line_bbox["top"]))
        width_overlap = inter_w / line_width
        height_overlap = inter_h / line_height
        center_x = (float(line_bbox["x0"]) + float(line_bbox["x1"])) / 2.0
        center_y = (float(line_bbox["top"]) + float(line_bbox["bottom"])) / 2.0
        center_inside = float(bbox["x0"]) <= center_x <= float(bbox["x1"]) and float(bbox["top"]) <= center_y <= float(bbox["bottom"])
        return center_inside or (width_overlap >= 0.35 and height_overlap >= 0.55)

    @staticmethod
    def _normalize_bbox(value: Any) -> dict[str, float] | None:
        if not isinstance(value, dict):
            return None
        try:
            x0 = float(value.get("x0"))
            top = float(value.get("top"))
            x1 = float(value.get("x1"))
            bottom = float(value.get("bottom"))
        except (TypeError, ValueError):
            return None
        if x1 <= x0 or bottom <= top:
            return None
        return {
            "x0": x0,
            "top": top,
            "x1": x1,
            "bottom": bottom,
        }

    def _extract_bbox(self, *, block: dict[str, Any], prompt_payload: dict[str, Any]) -> dict[str, float] | None:
        bbox = self._normalize_bbox(block.get("bbox"))
        if bbox is not None:
            return bbox

        prov = block.get("prov")
        if isinstance(prov, list) and prov:
            first = prov[0] if isinstance(prov[0], dict) else None
            if isinstance(first, dict):
                bbox = self._normalize_docling_bbox(first.get("bbox"), prompt_payload=prompt_payload)
                if bbox is not None:
                    return bbox
        return None

    @staticmethod
    def _extract_text(block: dict[str, Any]) -> str:
        for key in ("text", "orig", "content"):
            value = str(block.get(key) or "").strip()
            if value:
                return value
        data = block.get("data")
        if isinstance(data, dict):
            grid = data.get("grid")
            if isinstance(grid, list):
                rows: list[str] = []
                for row in grid[:32]:
                    if not isinstance(row, list):
                        continue
                    cells = [
                        LocalPdfHybridBackendTransformer._coerce_table_cell_text(item)
                        for item in row
                    ]
                    cells = [cell for cell in cells if cell]
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    return "\n".join(rows).strip()
        annotations = block.get("annotations")
        if isinstance(annotations, list):
            for item in annotations:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip().lower()
                text = str(item.get("text") or "").strip()
                if kind == "description" and text:
                    return text
            for item in annotations:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _extract_heading_level(block: dict[str, Any]) -> int | None:
        meta = block.get("meta")
        if not isinstance(meta, dict):
            return None
        return LocalPdfHybridBackendTransformer._coerce_positive_int(meta.get("level"))

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            level = int(value)
        except (TypeError, ValueError):
            return None
        return level if level > 0 else None

    @staticmethod
    def _extract_table_rows(block: dict[str, Any]) -> list[list[str]]:
        data = block.get("data")
        if not isinstance(data, dict):
            return []

        grid = data.get("grid")
        if isinstance(grid, list):
            rows: list[list[str]] = []
            for row in grid[:128]:
                if not isinstance(row, list):
                    continue
                rows.append(
                    [LocalPdfHybridBackendTransformer._coerce_table_cell_text(item) for item in row]
                )
            if any(any(cell for cell in row) for row in rows):
                return rows

        table_cells = data.get("table_cells")
        if isinstance(table_cells, list):
            max_row = -1
            max_col = -1
            cells: list[tuple[int, int, str, int, int]] = []
            for cell in table_cells:
                if not isinstance(cell, dict):
                    continue
                try:
                    row = int(cell.get("start_row_offset_idx", 0))
                    col = int(cell.get("start_col_offset_idx", 0))
                    row_span = max(1, int(cell.get("row_span", 1) or 1))
                    col_span = max(1, int(cell.get("col_span", 1) or 1))
                except (TypeError, ValueError):
                    continue
                text = str(cell.get("text") or "").strip()
                cells.append((row, col, text, row_span, col_span))
                max_row = max(max_row, row + row_span - 1)
                max_col = max(max_col, col + col_span - 1)
            if max_row >= 0 and max_col >= 0:
                rows = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
                for row, col, text, row_span, col_span in cells:
                    rows[row][col] = text
                    for extra_row in range(row, row + row_span):
                        for extra_col in range(col, col + col_span):
                            if extra_row == row and extra_col == col:
                                continue
                            rows[extra_row][extra_col] = rows[extra_row][extra_col] or ""
                return rows

        return []

    @staticmethod
    def _coerce_table_cell_text(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("text", "orig", "content"):
                text = str(value.get(key) or "").strip()
                if text:
                    return text
            return ""
        return str(value or "").strip()

    def _normalize_docling_bbox(self, value: Any, *, prompt_payload: dict[str, Any]) -> dict[str, float] | None:
        if not isinstance(value, dict):
            return None
        try:
            l = float(value.get("l"))
            t = float(value.get("t"))
            r = float(value.get("r"))
            b = float(value.get("b"))
        except (TypeError, ValueError):
            return None
        if r <= l:
            return None
        coord_origin = str(value.get("coord_origin") or "BOTTOMLEFT").strip().upper()
        page_height = float(prompt_payload.get("page_height") or 0.0)
        if coord_origin == "TOPLEFT":
            top = t
            bottom = b
        else:
            if page_height <= 0:
                return None
            top = page_height - t
            bottom = page_height - b
        if bottom <= top:
            return None
        return {
            "x0": l,
            "top": top,
            "x1": r,
            "bottom": bottom,
        }

    @staticmethod
    def _page_bbox(*, prompt_payload: dict[str, Any]) -> dict[str, float] | None:
        rows = [item for item in list(prompt_payload.get("line_rows") or []) if isinstance(item, dict)]
        if not rows:
            return None
        xs0 = [float(item["bbox"]["x0"]) for item in rows if isinstance(item.get("bbox"), dict)]
        tops = [float(item["bbox"]["top"]) for item in rows if isinstance(item.get("bbox"), dict)]
        xs1 = [float(item["bbox"]["x1"]) for item in rows if isinstance(item.get("bbox"), dict)]
        bottoms = [float(item["bbox"]["bottom"]) for item in rows if isinstance(item.get("bbox"), dict)]
        if not xs0 or not tops or not xs1 or not bottoms:
            return None
        return {
            "x0": min(xs0),
            "top": min(tops),
            "x1": max(xs1),
            "bottom": max(bottoms),
        }
