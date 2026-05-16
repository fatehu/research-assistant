"""
Strict render pipeline contracts for Reader Workbench v2.

This module enforces stage boundaries:
1) DocMind layout digest (truth source)
2) Stage 1 structural annotations
3) Stage 2 design layout plan
4) deterministic stage2 materialization for assembly
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple


STAGE1_ROLE_ENUM = {
    "doc_title",
    "section_title",
    "paragraph",
    "list_item",
    "caption",
    "figure",
    "table",
    "sidebar",
    "metadata",
    "header",
    "footer",
    "noise",
    "unknown",
}


class RenderPipelineContractError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        stage: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.stage = str(stage)
        self.message = str(message)
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class LayoutDigestBundle:
    page: int
    layout_digest: List[Dict[str, Any]]
    known_layout_ids: List[str]
    layout_index: Dict[str, int]


@dataclass(frozen=True)
class CanonicalAtomBundle:
    page: int
    paper_id: int
    document_fingerprint: str
    atoms: List[Dict[str, Any]]
    usable_atoms: List[Dict[str, Any]]
    excluded_atoms: List[Dict[str, Any]]
    known_atom_ids: List[str]
    usable_atom_ids: List[str]
    atom_index: Dict[str, int]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_spaces(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _pick_first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in list(keys or []):
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def _points_from_pos(value: Any) -> List[Dict[str, float]]:
    points: List[Dict[str, float]] = []
    for row in list(value or []):
        if not isinstance(row, dict):
            continue
        x = _safe_float(row.get("x"), 0.0)
        y = _safe_float(row.get("y"), 0.0)
        points.append({"x": round(float(x), 2), "y": round(float(y), 2)})
    return points


def _bbox_from_points(points: Sequence[Dict[str, float]]) -> List[float]:
    if not points:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [float(item.get("x") or 0.0) for item in points]
    ys = [float(item.get("y") or 0.0) for item in points]
    return [
        round(min(xs), 2),
        round(min(ys), 2),
        round(max(xs), 2),
        round(max(ys), 2),
    ]


def _layout_text(layout: Mapping[str, Any]) -> str:
    blocks = [row for row in list(layout.get("blocks") or []) if isinstance(row, dict)]
    if blocks:
        merged = " ".join(_normalize_spaces(str(row.get("text") or "")) for row in blocks)
        merged = _normalize_spaces(merged)
        if merged:
            return merged
    return _normalize_spaces(str(layout.get("text") or ""))


def build_docmind_layout_digest(docmind_structure: Dict[str, Any], page: int) -> LayoutDigestBundle:
    layouts = [row for row in list((docmind_structure or {}).get("layouts") or []) if isinstance(row, dict)]
    if not layouts:
        raise RenderPipelineContractError(
            code="DOCMIND_LAYOUT_DIGEST_EMPTY",
            stage="docmind",
            message="Document Mind layouts are empty",
            details={"page": int(page)},
        )

    page_zero = max(0, int(page) - 1)
    page_one = max(1, int(page))
    digest_rows: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, row in enumerate(layouts, start=1):
        page_num = row.get("pageNum")
        if isinstance(page_num, list) and page_num:
            page_values = []
            for item in page_num:
                token = str(item or "").strip()
                if token.isdigit():
                    page_values.append(int(token))
            if page_values and page_zero not in page_values and page_one not in page_values:
                continue
        elif isinstance(page_num, (int, float, str)):
            token = str(page_num).strip()
            if token.isdigit():
                value = int(token)
                if value not in {page_zero, page_one}:
                    continue

        layout_id = str(
            row.get("uniqueId")
            or row.get("layoutId")
            or row.get("id")
            or f"layout_{idx:04d}"
        ).strip()
        if not layout_id:
            layout_id = f"layout_{idx:04d}"
        if layout_id in seen_ids:
            layout_id = f"{layout_id}__{idx:04d}"
        seen_ids.add(layout_id)

        points = _points_from_pos(row.get("pos"))
        if not points:
            block_points: List[Dict[str, float]] = []
            for block in [item for item in list(row.get("blocks") or []) if isinstance(item, dict)]:
                block_points.extend(_points_from_pos(block.get("pos")))
            points = block_points
        bbox = _bbox_from_points(points)
        text_preview = _layout_text(row)[:200]
        digest_rows.append(
            {
                "layout_id": layout_id,
                "reading_order": _safe_int(row.get("index"), idx),
                "bbox": bbox,
                "text_preview": text_preview,
                "layout_type": str(row.get("type") or ""),
                "layout_sub_type": str(row.get("subType") or ""),
            }
        )

    digest_rows = sorted(
        digest_rows,
        key=lambda item: (
            _safe_int(item.get("reading_order"), 10**9),
            _safe_float((item.get("bbox") or [0, 0, 0, 0])[1], 0.0),
            _safe_float((item.get("bbox") or [0, 0, 0, 0])[0], 0.0),
            str(item.get("layout_id") or ""),
        ),
    )
    if not digest_rows:
        raise RenderPipelineContractError(
            code="DOCMIND_LAYOUT_DIGEST_EMPTY",
            stage="docmind",
            message="No layouts matched requested page",
            details={"page": int(page)},
        )

    known_layout_ids = [str(item.get("layout_id") or "") for item in digest_rows if str(item.get("layout_id") or "")]
    layout_index = {layout_id: idx for idx, layout_id in enumerate(known_layout_ids)}
    return LayoutDigestBundle(
        page=int(page),
        layout_digest=digest_rows,
        known_layout_ids=known_layout_ids,
        layout_index=layout_index,
    )


def validate_stage1_output(stage1_json: Any, known_layout_ids: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(stage1_json, dict):
        raise RenderPipelineContractError(
            code="STAGE1_INVALID_JSON",
            stage="stage1",
            message="Stage1 output must be a JSON object",
        )
    blocks = stage1_json.get("blocks")
    sections = stage1_json.get("sections")
    if not isinstance(blocks, list) or not isinstance(sections, list):
        raise RenderPipelineContractError(
            code="STAGE1_REQUIRED_FIELD_MISSING",
            stage="stage1",
            message="Stage1 output missing required fields: blocks/sections",
        )

    known = [str(item).strip() for item in list(known_layout_ids or []) if str(item).strip()]
    known_set = set(known)
    seen: Dict[str, Dict[str, Any]] = {}
    unknown_ids: List[str] = []
    duplicate_ids: List[str] = []
    required_missing = False
    missing_samples: List[Dict[str, Any]] = []
    normalized_blocks: List[Dict[str, Any]] = []

    for idx, row in enumerate(blocks, start=1):
        if not isinstance(row, dict):
            required_missing = True
            if len(missing_samples) < 10:
                missing_samples.append(
                    {
                        "index": idx,
                        "reason": "block_item_not_object",
                        "type": str(type(row).__name__),
                    }
                )
            continue
        layout_id = str(_pick_first(row, ["layout_id", "layoutId", "id"]) or "").strip()
        role_raw = str(_pick_first(row, ["role", "block_role", "kind"]) or "").strip()
        role = role_raw.lower().replace("-", "_").replace(" ", "_")
        section_id = str(_pick_first(row, ["section_id", "sectionId", "section"]) or "").strip()
        column_raw = _pick_first(row, ["column", "column_index", "col"])
        confidence_raw = _pick_first(row, ["confidence", "score", "conf"])
        missing_fields: List[str] = []
        if not layout_id:
            missing_fields.append("layout_id")
        if not role:
            missing_fields.append("role")
        if not section_id:
            missing_fields.append("section_id")
        if column_raw is None:
            missing_fields.append("column")
        if confidence_raw is None:
            missing_fields.append("confidence")
        if missing_fields:
            required_missing = True
            if len(missing_samples) < 10:
                missing_samples.append(
                    {
                        "index": idx,
                        "layout_id": layout_id,
                        "missing_fields": missing_fields,
                        "present_keys": sorted(list(row.keys()))[:60],
                    }
                )
            continue
        if layout_id not in known_set:
            unknown_ids.append(layout_id)
            continue
        if layout_id in seen:
            duplicate_ids.append(layout_id)
            continue
        if role not in STAGE1_ROLE_ENUM:
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 role is outside strict enum",
                details={"layout_id": layout_id, "role": role_raw, "normalized_role": role},
            )
        column = _safe_int(column_raw, -1)
        confidence = _safe_float(confidence_raw, -1.0)
        if column < 0 or confidence < 0.0 or confidence > 1.0:
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 column/confidence out of range",
                details={"layout_id": layout_id, "column": column, "confidence": confidence},
            )
        normalized = {
            "layout_id": layout_id,
            "role": role,
            "section_id": section_id,
            "column": int(column),
            "confidence": round(float(confidence), 4),
        }
        seen[layout_id] = normalized
        normalized_blocks.append(normalized)

    if required_missing:
        raise RenderPipelineContractError(
            code="STAGE1_REQUIRED_FIELD_MISSING",
            stage="stage1",
            message="Stage1 blocks missing required fields",
            details={"samples": missing_samples[:10]},
        )
    if duplicate_ids:
        raise RenderPipelineContractError(
            code="STAGE1_LAYOUT_ID_DUPLICATE",
            stage="stage1",
            message="Stage1 contains duplicate layout_id",
            details={"duplicates": sorted(set(duplicate_ids))[:50]},
        )
    if unknown_ids:
        raise RenderPipelineContractError(
            code="STAGE1_LAYOUT_ID_UNKNOWN",
            stage="stage1",
            message="Stage1 contains unknown layout_id",
            details={"unknown_layout_ids": sorted(set(unknown_ids))[:50]},
        )

    missing_ids = [item for item in known if item not in seen]
    if missing_ids:
        raise RenderPipelineContractError(
            code="STAGE1_LAYOUT_ID_MISSING",
            stage="stage1",
            message="Stage1 does not cover all known layout IDs",
            details={"missing_layout_ids": missing_ids[:120]},
        )

    section_id_set = {str(item.get("section_id") or "").strip() for item in normalized_blocks}
    normalized_sections: List[Dict[str, Any]] = []
    for row in sections:
        if not isinstance(row, dict):
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 section item must be object",
            )
        section_id = str(_pick_first(row, ["section_id", "sectionId", "id"]) or "").strip()
        title_layout_id = str(_pick_first(row, ["title_layout_id", "titleLayoutId", "title_id"]) or "").strip()
        children_raw = _pick_first(row, ["children", "child_layout_ids", "content_layout_ids"])
        children = [str(item).strip() for item in list(children_raw or []) if str(item).strip()]
        if not section_id or not title_layout_id:
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 section missing section_id/title_layout_id",
            )
        if title_layout_id not in known_set:
            raise RenderPipelineContractError(
                code="STAGE1_SECTION_REF_INVALID",
                stage="stage1",
                message="Stage1 section title_layout_id is invalid",
                details={"section_id": section_id, "title_layout_id": title_layout_id},
            )
        invalid_children = [item for item in children if item not in known_set]
        if invalid_children:
            raise RenderPipelineContractError(
                code="STAGE1_SECTION_REF_INVALID",
                stage="stage1",
                message="Stage1 section children contain invalid layout_id",
                details={"section_id": section_id, "invalid_children": invalid_children[:50]},
            )
        normalized_sections.append(
            {
                "section_id": section_id,
                "title_layout_id": title_layout_id,
                "children": children,
            }
        )

    if any(item not in section_id_set for item in [row.get("section_id") for row in normalized_sections]):
        raise RenderPipelineContractError(
            code="STAGE1_SECTION_REF_INVALID",
            stage="stage1",
            message="Stage1 sections reference unknown section_id",
        )

    known_order = {layout_id: idx for idx, layout_id in enumerate(known)}
    normalized_blocks = sorted(
        normalized_blocks,
        key=lambda item: known_order.get(str(item.get("layout_id") or ""), 10**9),
    )
    normalized_sections = sorted(
        normalized_sections,
        key=lambda item: (
            known_order.get(str(item.get("title_layout_id") or ""), 10**9),
            str(item.get("section_id") or ""),
        ),
    )
    return {
        "blocks": normalized_blocks,
        "sections": normalized_sections,
    }


def validate_stage2_output(
    stage2_json: Any,
    known_layout_ids: Sequence[str],
    allowed_components: Sequence[str],
) -> Dict[str, Any]:
    if not isinstance(stage2_json, dict):
        raise RenderPipelineContractError(
            code="STAGE2_INVALID_JSON",
            stage="stage2",
            message="Stage2 output must be a JSON object",
        )
    page_layout = stage2_json.get("page_layout")
    unused_layout_ids = stage2_json.get("unused_layout_ids")
    if not isinstance(page_layout, list) or not isinstance(unused_layout_ids, list):
        raise RenderPipelineContractError(
            code="STAGE2_INVALID_JSON",
            stage="stage2",
            message="Stage2 output missing page_layout/unused_layout_ids",
        )

    known = [str(item).strip() for item in list(known_layout_ids or []) if str(item).strip()]
    known_set = set(known)
    allowed = {str(item).strip() for item in list(allowed_components or []) if str(item).strip()}
    if not allowed:
        raise RenderPipelineContractError(
            code="STAGE2_COMPONENT_NOT_ALLOWED",
            stage="stage2",
            message="Allowed component list is empty",
        )

    used_by_layout: Dict[str, int] = {}
    normalized_layout_rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(page_layout, start=1):
        if not isinstance(row, dict):
            raise RenderPipelineContractError(
                code="STAGE2_INVALID_JSON",
                stage="stage2",
                message="Stage2 page_layout item must be object",
                details={"index": idx},
            )
        component = str(row.get("component") or "").strip()
        if not component or component not in allowed:
            raise RenderPipelineContractError(
                code="STAGE2_COMPONENT_NOT_ALLOWED",
                stage="stage2",
                message="Stage2 component is not in allowlist",
                details={"index": idx, "component": component},
            )
        source_layout_ids = [str(item).strip() for item in list(row.get("source_layout_ids") or []) if str(item).strip()]
        if not source_layout_ids:
            raise RenderPipelineContractError(
                code="STAGE2_INVALID_JSON",
                stage="stage2",
                message="Stage2 source_layout_ids cannot be empty",
                details={"index": idx},
            )
        for layout_id in source_layout_ids:
            if layout_id not in known_set:
                raise RenderPipelineContractError(
                    code="STAGE2_LAYOUT_ID_UNKNOWN",
                    stage="stage2",
                    message="Stage2 references unknown layout_id",
                    details={"layout_id": layout_id, "component": component},
                )
            if layout_id in used_by_layout:
                raise RenderPipelineContractError(
                    code="STAGE2_LAYOUT_ID_DUPLICATE_USE",
                    stage="stage2",
                    message="Stage2 reuses layout_id across page_layout items",
                    details={"layout_id": layout_id},
                )
            used_by_layout[layout_id] = idx
        props = row.get("props")
        normalized_layout_rows.append(
            {
                "component": component,
                "source_layout_ids": source_layout_ids,
                "props": dict(props or {}) if isinstance(props, dict) else {},
            }
        )

    unused = [str(item).strip() for item in list(unused_layout_ids or []) if str(item).strip()]
    for layout_id in unused:
        if layout_id not in known_set:
            raise RenderPipelineContractError(
                code="STAGE2_LAYOUT_ID_UNKNOWN",
                stage="stage2",
                message="Stage2 unused_layout_ids contains unknown layout_id",
                details={"layout_id": layout_id},
            )
    used_set = set(used_by_layout.keys())
    unused_set = set(unused)
    overlap = used_set.intersection(unused_set)
    if overlap:
        raise RenderPipelineContractError(
            code="STAGE2_UNUSED_OVERLAP",
            stage="stage2",
            message="Stage2 has overlap between used and unused layout IDs",
            details={"overlap": sorted(overlap)[:80]},
        )
    coverage_set = used_set.union(unused_set)
    if coverage_set != known_set:
        missing = [item for item in known if item not in coverage_set]
        extra = [item for item in coverage_set if item not in known_set]
        raise RenderPipelineContractError(
            code="STAGE2_LAYOUT_ID_COVERAGE_MISMATCH",
            stage="stage2",
            message="Stage2 does not fully partition known layout IDs",
            details={"missing": missing[:120], "extra": extra[:120]},
        )

    return {
        "page_layout": normalized_layout_rows,
        "unused_layout_ids": unused,
    }


def materialize_stage2_plan(
    normalized_stage2: Dict[str, Any],
    digest_bundle: LayoutDigestBundle,
) -> Dict[str, Any]:
    page_layout = [row for row in list((normalized_stage2 or {}).get("page_layout") or []) if isinstance(row, dict)]
    unused_layout_ids = [str(item).strip() for item in list((normalized_stage2 or {}).get("unused_layout_ids") or []) if str(item).strip()]
    index_map = dict(digest_bundle.layout_index or {})

    def _layout_rank(layout_ids: Sequence[str]) -> Tuple[int, str]:
        ranks = [index_map.get(str(item).strip(), 10**9) for item in list(layout_ids or []) if str(item).strip()]
        return (min(ranks) if ranks else 10**9, ",".join(sorted(set(str(item).strip() for item in layout_ids if str(item).strip()))))

    normalized_rows: List[Dict[str, Any]] = []
    for row in page_layout:
        source_layout_ids = [
            str(item).strip()
            for item in list(row.get("source_layout_ids") or [])
            if str(item).strip()
        ]
        source_layout_ids = sorted(set(source_layout_ids), key=lambda item: index_map.get(item, 10**9))
        normalized_rows.append(
            {
                "component": str(row.get("component") or ""),
                "source_layout_ids": source_layout_ids,
                "props": dict(row.get("props") or {}) if isinstance(row.get("props"), dict) else {},
            }
        )
    normalized_rows = sorted(
        normalized_rows,
        key=lambda row: (_layout_rank(row.get("source_layout_ids") or []), str(row.get("component") or "")),
    )
    unused_sorted = sorted(set(unused_layout_ids), key=lambda item: index_map.get(item, 10**9))
    return {
        "page_layout": normalized_rows,
        "unused_layout_ids": unused_sorted,
    }


SEMANTIC_ROLE_ENUM = {
    "doc_title",
    "section_title",
    "paragraph",
    "list_item",
    "caption",
    "figure",
    "table",
    "sidebar",
    "metadata",
    "header",
    "footer",
    "noise",
    "unknown",
}

_EXCLUDED_SUBTYPES = {
    "split_line",
    "decoration",
    "background_line",
    "watermark",
}
_EXCLUDED_TYPES = {
    "split_line",
    "decoration",
    "background_line",
    "watermark",
}
_EXCLUDED_ROLE_HINTS = {"header", "footer", "noise"}


def _stable_document_fingerprint(docmind_structure: Mapping[str, Any]) -> str:
    payload = {
        "doc_info": dict((docmind_structure or {}).get("doc_info") or {}),
        "layout_count": len(list((docmind_structure or {}).get("layouts") or [])),
    }
    packed = str(payload).encode("utf-8", errors="ignore")
    return hashlib.sha256(packed).hexdigest()[:16]


def _layout_uid(layout: Mapping[str, Any], fallback_idx: int) -> str:
    token = str(
        layout.get("uniqueId")
        or layout.get("layoutId")
        or layout.get("id")
        or f"layout_{fallback_idx:04d}"
    ).strip()
    return token or f"layout_{fallback_idx:04d}"


def _resolve_page_filter(page: int) -> Tuple[int, int]:
    page_zero = max(0, int(page) - 1)
    page_one = max(1, int(page))
    return page_zero, page_one


def _layout_matches_page(layout: Mapping[str, Any], page: int) -> bool:
    page_zero, page_one = _resolve_page_filter(page)
    page_num = layout.get("pageNum")
    if isinstance(page_num, list) and page_num:
        values: List[int] = []
        for item in page_num:
            token = str(item or "").strip()
            if token.isdigit():
                values.append(int(token))
        if values and page_zero not in values and page_one not in values:
            return False
    elif isinstance(page_num, (int, float, str)):
        token = str(page_num).strip()
        if token.isdigit():
            value = int(token)
            if value not in {page_zero, page_one}:
                return False
    return True


def _polygon_area(points: Sequence[Mapping[str, Any]]) -> float:
    rows = [
        (
            _safe_float(row.get("x"), 0.0),
            _safe_float(row.get("y"), 0.0),
        )
        for row in list(points or [])
        if isinstance(row, Mapping)
    ]
    if len(rows) < 3:
        return 0.0
    total = 0.0
    for idx in range(len(rows)):
        x1, y1 = rows[idx]
        x2, y2 = rows[(idx + 1) % len(rows)]
        total += x1 * y2 - x2 * y1
    return abs(total) * 0.5


def _infer_default_role(*, layout_type: str, layout_sub_type: str) -> str:
    token_type = str(layout_type or "").strip().lower()
    token_sub = str(layout_sub_type or "").strip().lower()
    if "title" in token_sub or token_type == "title":
        return "section_title"
    if "caption" in token_sub:
        return "caption"
    if token_type == "figure":
        return "figure"
    if token_type in {"head", "header_line", "header"}:
        return "header"
    if token_type in {"foot", "footer_line", "foot_pagenum", "footer"}:
        return "footer"
    if token_type in {"side"}:
        return "sidebar"
    if token_type in {"table"}:
        return "table"
    if token_type in {"text", "para"}:
        return "paragraph"
    return "unknown"


def _infer_default_component(role: str) -> str:
    token = str(role or "").strip().lower()
    if token in {"doc_title", "section_title"}:
        return "SectionHeading"
    if token == "list_item":
        return "ListBlock"
    if token in {"figure", "caption"}:
        return "FigurePanel"
    if token == "table":
        return "TablePanel"
    if token in {"sidebar", "metadata", "header", "footer"}:
        return "ContextRail"
    return "ParagraphProse"


def build_canonical_atom_bundle(
    *,
    docmind_structure: Dict[str, Any],
    page: int,
    paper_id: int,
    min_atom_area: float = 9.0,
    document_fingerprint: str = "",
) -> CanonicalAtomBundle:
    layouts = [
        row
        for row in list((docmind_structure or {}).get("layouts") or [])
        if isinstance(row, dict) and _layout_matches_page(row, int(page))
    ]
    if not layouts:
        raise RenderPipelineContractError(
            code="DOCMIND_LAYOUT_DIGEST_EMPTY",
            stage="docmind",
            message="Document Mind layouts are empty for current page",
            details={"page": int(page)},
        )

    doc_fingerprint = str(document_fingerprint or "").strip() or _stable_document_fingerprint(docmind_structure)
    atoms: List[Dict[str, Any]] = []
    excluded_atoms: List[Dict[str, Any]] = []
    layout_rows = sorted(
        layouts,
        key=lambda row: (
            _safe_int(row.get("index"), 10**9),
            _safe_float(_bbox_from_points(_points_from_pos(row.get("pos")))[1], 0.0),
            _safe_float(_bbox_from_points(_points_from_pos(row.get("pos")))[0], 0.0),
            _layout_uid(row, 0),
        ),
    )

    global_order = 0
    seen_atom_ids: Set[str] = set()
    for layout_idx, layout in enumerate(layout_rows, start=1):
        l_uid = _layout_uid(layout, layout_idx)
        l_type = str(layout.get("type") or "").strip().lower()
        l_sub_type = str(layout.get("subType") or "").strip().lower()
        style_id = layout.get("styleId")
        blocks = [row for row in list(layout.get("blocks") or []) if isinstance(row, dict)]
        if not blocks:
            layout_text = _normalize_spaces(str(layout.get("text") or ""))
            if layout_text:
                blocks = [{"text": layout_text, "pos": list(layout.get("pos") or []), "styleId": style_id}]

        for block_idx, block in enumerate(blocks, start=1):
            text = _normalize_spaces(str(block.get("text") or ""))
            points = _points_from_pos(block.get("pos")) or _points_from_pos(layout.get("pos"))
            bbox = _bbox_from_points(points)
            area = _polygon_area(points)
            atom_id = f"p{int(page)}:l{l_uid}:b{int(block_idx)}"
            if atom_id in seen_atom_ids:
                atom_id = f"{atom_id}:{global_order + 1}"
            seen_atom_ids.add(atom_id)
            role = _infer_default_role(layout_type=l_type, layout_sub_type=l_sub_type)
            excluded_reason = ""
            if not text:
                excluded_reason = "empty_text"
            elif area < float(min_atom_area):
                excluded_reason = "area_below_min"
            elif l_sub_type in _EXCLUDED_SUBTYPES or l_type in _EXCLUDED_TYPES:
                excluded_reason = "decorative_layout"
            elif role in _EXCLUDED_ROLE_HINTS:
                excluded_reason = "excluded_role"

            # 被排除的 atoms 保留在 bundle 中用于诊断，但只有可用 atoms
            # 可以成为组件 ownership 目标。
            atom = {
                "atom_id": atom_id,
                "page": int(page),
                "paper_id": int(paper_id),
                "document_fingerprint": doc_fingerprint,
                "source_layout_id": l_uid,
                "block_index": int(block_idx),
                "text": text,
                "bbox": bbox,
                "polygon": points,
                "reading_order": int(global_order + 1),
                "type": l_type,
                "sub_type": l_sub_type,
                "style_id": _safe_int(block.get("styleId"), _safe_int(style_id, 0)),
                "excluded_reason": excluded_reason,
                "default_role": role,
                "default_component": _infer_default_component(role),
            }
            atoms.append(atom)
            if excluded_reason:
                excluded_atoms.append(
                    {
                        "atom_id": atom_id,
                        "reason": excluded_reason,
                        "type": l_type,
                        "sub_type": l_sub_type,
                    }
                )
            global_order += 1

    if not atoms:
        raise RenderPipelineContractError(
            code="DOCMIND_LAYOUT_DIGEST_EMPTY",
            stage="docmind",
            message="Canonical atom bundle is empty",
            details={"page": int(page)},
        )

    atoms = sorted(
        atoms,
        key=lambda row: (
            _safe_int(row.get("reading_order"), 10**9),
            _safe_float((row.get("bbox") or [0.0, 0.0, 0.0, 0.0])[1], 0.0),
            _safe_float((row.get("bbox") or [0.0, 0.0, 0.0, 0.0])[0], 0.0),
            str(row.get("atom_id") or ""),
        ),
    )
    usable_atoms = [row for row in atoms if not str(row.get("excluded_reason") or "").strip()]
    known_atom_ids = [str(row.get("atom_id") or "") for row in atoms if str(row.get("atom_id") or "")]
    usable_atom_ids = [str(row.get("atom_id") or "") for row in usable_atoms if str(row.get("atom_id") or "")]
    if not usable_atom_ids:
        raise RenderPipelineContractError(
            code="DOCMIND_LAYOUT_DIGEST_EMPTY",
            stage="docmind",
            message="No usable atoms after deterministic filtering",
            details={"page": int(page), "atom_count": len(atoms)},
        )
    atom_index = {atom_id: idx for idx, atom_id in enumerate(known_atom_ids)}
    return CanonicalAtomBundle(
        page=int(page),
        paper_id=int(paper_id),
        document_fingerprint=doc_fingerprint,
        atoms=atoms,
        usable_atoms=usable_atoms,
        excluded_atoms=excluded_atoms,
        known_atom_ids=known_atom_ids,
        usable_atom_ids=usable_atom_ids,
        atom_index=atom_index,
    )


def validate_stage1_semantic_output(
    stage1_json: Any,
    *,
    known_atom_ids: Sequence[str],
) -> Dict[str, Any]:
    if not isinstance(stage1_json, dict):
        raise RenderPipelineContractError(
            code="STAGE1_INVALID_JSON",
            stage="stage1",
            message="Stage1 semantic output must be JSON object",
        )
    rows = stage1_json.get("annotations")
    if not isinstance(rows, list):
        raise RenderPipelineContractError(
            code="STAGE1_REQUIRED_FIELD_MISSING",
            stage="stage1",
            message="Stage1 semantic output missing annotations list",
        )
    known = [str(item).strip() for item in list(known_atom_ids or []) if str(item).strip()]
    known_set = set(known)
    seen: Dict[str, Dict[str, Any]] = {}
    missing_fields = False
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            missing_fields = True
            continue
        atom_id = str(row.get("atom_id") or "").strip()
        role = str(row.get("role") or "").strip().lower().replace("-", "_")
        importance = str(row.get("importance") or "normal").strip().lower()
        grouping_hint = _normalize_spaces(str(row.get("grouping_hint") or ""))[:120]
        component_hint = str(row.get("component_hint") or "").strip()
        confidence = _safe_float(row.get("confidence"), -1.0)
        if not atom_id or not role:
            missing_fields = True
            continue
        if atom_id not in known_set:
            raise RenderPipelineContractError(
                code="STAGE1_LAYOUT_ID_UNKNOWN",
                stage="stage1",
                message="Stage1 semantic output has unknown atom_id",
                details={"atom_id": atom_id, "index": idx},
            )
        if atom_id in seen:
            raise RenderPipelineContractError(
                code="STAGE1_LAYOUT_ID_DUPLICATE",
                stage="stage1",
                message="Stage1 semantic output has duplicate atom_id",
                details={"atom_id": atom_id, "index": idx},
            )
        if role not in SEMANTIC_ROLE_ENUM:
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 semantic role outside strict enum",
                details={"atom_id": atom_id, "role": role},
            )
        if confidence < 0.0 or confidence > 1.0:
            raise RenderPipelineContractError(
                code="STAGE1_REQUIRED_FIELD_MISSING",
                stage="stage1",
                message="Stage1 semantic confidence out of range",
                details={"atom_id": atom_id, "confidence": confidence},
            )
        seen[atom_id] = {
            "atom_id": atom_id,
            "role": role,
            "importance": importance or "normal",
            "grouping_hint": grouping_hint,
            "component_hint": component_hint,
            "confidence": round(float(confidence), 4),
        }
    if missing_fields:
        raise RenderPipelineContractError(
            code="STAGE1_REQUIRED_FIELD_MISSING",
            stage="stage1",
            message="Stage1 semantic annotations contain invalid items",
        )
    missing = [atom_id for atom_id in known if atom_id not in seen]
    if missing:
        raise RenderPipelineContractError(
            code="STAGE1_LAYOUT_ID_MISSING",
            stage="stage1",
            message="Stage1 semantic output does not cover all atom ids",
            details={"missing_atom_ids": missing[:160]},
        )
    normalized = [seen[atom_id] for atom_id in known if atom_id in seen]
    return {"annotations": normalized}


def _check_forbidden_nested_keys(
    payload: Any,
    *,
    forbidden_tokens: Sequence[str],
    path: str = "",
    hits: Optional[List[str]] = None,
) -> List[str]:
    bucket = hits if isinstance(hits, list) else []
    lowered = [str(item or "").strip().lower() for item in list(forbidden_tokens or []) if str(item or "").strip()]
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_token = str(key or "").strip()
            key_lower = key_token.lower()
            current_path = f"{path}.{key_token}" if path else key_token
            if any(token in key_lower for token in lowered):
                bucket.append(current_path)
            _check_forbidden_nested_keys(value, forbidden_tokens=lowered, path=current_path, hits=bucket)
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            current_path = f"{path}[{idx}]"
            _check_forbidden_nested_keys(item, forbidden_tokens=lowered, path=current_path, hits=bucket)
    return bucket


def validate_stage2_design_output(
    stage2_json: Any,
    *,
    known_atom_ids: Sequence[str],
    allowed_components: Sequence[str],
) -> Dict[str, Any]:
    if not isinstance(stage2_json, dict):
        raise RenderPipelineContractError(
            code="STAGE2_INVALID_JSON",
            stage="stage2",
            message="Stage2 design output must be JSON object",
        )
    slots = stage2_json.get("page_layout_slots")
    unused_atom_ids = stage2_json.get("unused_atom_ids")
    if not isinstance(slots, list) or not isinstance(unused_atom_ids, list):
        raise RenderPipelineContractError(
            code="STAGE2_INVALID_JSON",
            stage="stage2",
            message="Stage2 design output missing page_layout_slots/unused_atom_ids",
        )
    known = [str(item).strip() for item in list(known_atom_ids or []) if str(item).strip()]
    known_set = set(known)
    allowed_set = {str(item).strip() for item in list(allowed_components or []) if str(item).strip()}
    if not allowed_set:
        raise RenderPipelineContractError(
            code="STAGE2_COMPONENT_NOT_ALLOWED",
            stage="stage2",
            message="Stage2 design allowed component list is empty",
        )

    used: Dict[str, str] = {}
    normalized_slots: List[Dict[str, Any]] = []
    forbidden = [
        "ownership",
        "topology",
        "source_layout_ids",
        "source_block_ids",
        "reassign",
        "atom_owner",
    ]
    forbidden_hits = _check_forbidden_nested_keys(stage2_json, forbidden_tokens=forbidden)
    if forbidden_hits:
        raise RenderPipelineContractError(
            code="STAGE2_FORBIDDEN_FIELD",
            stage="stage2",
            message="Stage2 design output contains forbidden nested ownership/topology fields",
            details={"forbidden_paths": forbidden_hits[:80]},
        )

    for idx, row in enumerate(slots, start=1):
        if not isinstance(row, dict):
            raise RenderPipelineContractError(
                code="STAGE2_INVALID_JSON",
                stage="stage2",
                message="Stage2 slot item must be object",
                details={"index": idx},
            )
        slot_id = str(row.get("slot_id") or f"slot_{idx:03d}").strip()
        component = str(row.get("component") or "").strip()
        atom_ids = [str(item).strip() for item in list(row.get("atom_ids") or []) if str(item).strip()]
        style_tokens = dict(row.get("style_tokens") or {}) if isinstance(row.get("style_tokens"), dict) else {}
        layout_tokens = dict(row.get("layout_tokens") or {}) if isinstance(row.get("layout_tokens"), dict) else {}
        if not component or component not in allowed_set:
            raise RenderPipelineContractError(
                code="STAGE2_COMPONENT_NOT_ALLOWED",
                stage="stage2",
                message="Stage2 slot component not allowed",
                details={"slot_id": slot_id, "component": component},
            )
        if not atom_ids:
            raise RenderPipelineContractError(
                code="STAGE2_INVALID_JSON",
                stage="stage2",
                message="Stage2 slot atom_ids cannot be empty",
                details={"slot_id": slot_id},
            )
        for atom_id in atom_ids:
            if atom_id not in known_set:
                raise RenderPipelineContractError(
                    code="STAGE2_LAYOUT_ID_UNKNOWN",
                    stage="stage2",
                    message="Stage2 slot has unknown atom_id",
                    details={"slot_id": slot_id, "atom_id": atom_id},
                )
            if atom_id in used:
                raise RenderPipelineContractError(
                    code="STAGE2_OWNERSHIP_MUTATION",
                    stage="stage2",
                    message="Stage2 atom_id used by multiple slots",
                    details={"atom_id": atom_id, "slot_id": slot_id, "existing_slot_id": used[atom_id]},
                )
            used[atom_id] = slot_id
        normalized_slots.append(
            {
                "slot_id": slot_id,
                "component": component,
                "atom_ids": atom_ids,
                "style_tokens": style_tokens,
                "layout_tokens": layout_tokens,
            }
        )

    unused = [str(item).strip() for item in list(unused_atom_ids or []) if str(item).strip()]
    for atom_id in unused:
        if atom_id not in known_set:
            raise RenderPipelineContractError(
                code="STAGE2_LAYOUT_ID_UNKNOWN",
                stage="stage2",
                message="Stage2 unused_atom_ids has unknown atom_id",
                details={"atom_id": atom_id},
            )
    used_set = set(used.keys())
    unused_set = set(unused)
    overlap = used_set.intersection(unused_set)
    if overlap:
        raise RenderPipelineContractError(
            code="STAGE2_OWNERSHIP_MUTATION",
            stage="stage2",
            message="Stage2 used and unused atom sets overlap",
            details={"overlap": sorted(overlap)[:120]},
        )
    coverage = used_set.union(unused_set)
    if coverage != known_set:
        missing = [atom_id for atom_id in known if atom_id not in coverage]
        extra = [atom_id for atom_id in coverage if atom_id not in known_set]
        raise RenderPipelineContractError(
            code="STAGE2_TOPOLOGY_MUTATION",
            stage="stage2",
            message="Stage2 design output mutates topology by not fully partitioning atom ids",
            details={"missing": missing[:160], "extra": extra[:120]},
        )

    return {
        "page_layout_slots": normalized_slots,
        "unused_atom_ids": unused,
    }


def build_deterministic_baseline_slots(
    *,
    atom_bundle: CanonicalAtomBundle,
    allowed_components: Sequence[str],
) -> Dict[str, Any]:
    allowed_set = {str(item).strip() for item in list(allowed_components or []) if str(item).strip()}
    slots: List[Dict[str, Any]] = []
    for idx, atom in enumerate(list(atom_bundle.usable_atoms or []), start=1):
        atom_id = str(atom.get("atom_id") or "").strip()
        if not atom_id:
            continue
        default_component = str(atom.get("default_component") or "ParagraphProse")
        component = default_component if default_component in allowed_set else "ParagraphProse"
        if component not in allowed_set and allowed_set:
            component = sorted(list(allowed_set))[0]
        # 基线 slots 是确定性 fallback 输出：一个组件拥有一个 atom，
        # 这样后续 gates 不依赖模型也能验证完整覆盖。
        slots.append(
            {
                "slot_id": f"slot_{idx:03d}",
                "component": component,
                "atom_ids": [atom_id],
                "style_tokens": {},
                "layout_tokens": {},
            }
        )
    return {
        "page_layout_slots": slots,
        "unused_atom_ids": [],
    }


def enforce_minimal_gates(
    *,
    ui_plan: Dict[str, Any],
    usable_atom_ids: Sequence[str],
    allowed_components: Sequence[str],
    non_empty_input: bool,
) -> Dict[str, Any]:
    components = [
        row for row in list((ui_plan or {}).get("components") or []) if isinstance(row, dict)
    ]
    allowed_set = {str(item).strip() for item in list(allowed_components or []) if str(item).strip()}
    schema_valid = isinstance(ui_plan, dict) and isinstance(ui_plan.get("components"), list)
    whitelist_valid = True
    ownership_unchanged = True
    used_atoms: List[str] = []
    seen_atoms: Set[str] = set()
    for node in components:
        node_type = str(node.get("type") or "").strip()
        if allowed_set and node_type and node_type not in allowed_set:
            whitelist_valid = False
        refs = [str(item).strip() for item in list(node.get("source_atom_ids") or []) if str(item).strip()]
        for atom_id in refs:
            used_atoms.append(atom_id)
            if atom_id in seen_atoms:
                ownership_unchanged = False
            seen_atoms.add(atom_id)
    usable_set = {str(item).strip() for item in list(usable_atom_ids or []) if str(item).strip()}
    full_coverage = usable_set.issubset(set(used_atoms))
    non_empty_plan = (len(components) > 0) if bool(non_empty_input) else True
    # 最小 gates 在主观质量评分前保护 renderer contract：schema、白名单、
    # 单一 ownership 和完整 source coverage。
    passed = bool(schema_valid and whitelist_valid and ownership_unchanged and full_coverage and non_empty_plan)
    return {
        "schema_valid": bool(schema_valid),
        "whitelist_valid": bool(whitelist_valid),
        "ownership_unchanged": bool(ownership_unchanged),
        "full_coverage": bool(full_coverage),
        "non_empty_plan_for_non_empty_input": bool(non_empty_plan),
        "passed": passed,
        "used_atom_count": len(set(used_atoms)),
        "usable_atom_count": len(usable_set),
    }
