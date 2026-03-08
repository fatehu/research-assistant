from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

HARD_GATES: Tuple[str, ...] = (
    "id_integrity",
    "full_coverage",
    "whitelist_only",
    "layout_contract",
    "no_drop_blocks",
    "ownership_unchanged",
    "non_empty_plan_for_non_empty_input",
    "source_text_immutable",
)

SAFE_CLEAN_OPS: Set[str] = {
    "whitespace_normalize",
    "punctuation_spacing_fix",
    "known_ocr_char_fix",
    "safe_line_merge",
}

NOISE_REJECT_SOURCE_TEXT = "NOISE_MUTATES_SOURCE_TEXT"
NOISE_REJECT_OWNERSHIP = "NOISE_OWNERSHIP_MUTATION"
NOISE_REJECT_ID = "NOISE_ID_MUTATION"

AUX_DEFAULT_TYPES: Set[str] = {
    "head",
    "header_line",
    "side",
    "footer_line",
    "foot",
    "foot_pagenum",
    "split_line",
}

AUX_KEYWORDS: Tuple[str, ...] = (
    "citation",
    "doi",
    "open access",
    "editor",
    "received",
    "accepted",
    "published",
    "copyright",
    "funding",
    "competing interests",
    "data availability",
    "email",
)

_MOJIBAKE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:\u00c3.|\u00c2.|\u00e2\u20ac)"),
    re.compile(r"\uFFFD"),
)

_ALLOWED_LAYOUT_MODES: Set[str] = {
    "single_column",
    "split",
    "drawer",
    "section_inline",
    "single_with_sidebar",
    "two_column",
}

_ALLOWED_ZONE_TYPES: Set[str] = {"main_body", "side_context", "figure_meta"}
_ALLOWED_DISPLAY_MODES: Set[str] = {"default", "collapsed", "pinned", "hidden_until_expand"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_spaces(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _text_hygiene_issues(text: str) -> List[str]:
    raw = str(text or "")
    issues: List[str] = []
    if "\uFFFD" in raw:
        issues.append("contains_replacement_char")
    for char in raw:
        code = ord(char)
        if 0xE000 <= code <= 0xF8FF:
            issues.append("contains_private_use_area_char")
            break
    for pattern in _MOJIBAKE_PATTERNS:
        if pattern.search(raw):
            issues.append("mojibake_pattern_matched")
            break
    return sorted(set(issues))


def detect_text_hygiene_issues(text: str) -> List[str]:
    return _text_hygiene_issues(text)


class ReaderSingleAgentValidator:
    def __init__(self) -> None:
        self.hard_gates = HARD_GATES

    @staticmethod
    def _docmind_index(docmind_blocks: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        for row in list(docmind_blocks or []):
            if not isinstance(row, Mapping):
                continue
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id or layout_id in index:
                continue
            index[layout_id] = {
                "layout_id": layout_id,
                "source_text": str(row.get("source_text") or ""),
                "type": str(row.get("type") or "").strip().lower(),
                "subType": str(row.get("subType") or row.get("sub_type") or "").strip().lower(),
                "block_ids": [
                    str(item).strip()
                    for item in list(row.get("block_ids") or [])
                    if str(item).strip()
                ],
            }
        return index

    @staticmethod
    def _gate(passed: bool, errors: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "passed": bool(passed),
            "errors": [str(item) for item in list(errors or []) if str(item)],
        }

    @staticmethod
    def _structured_error(code: str, **details: Any) -> str:
        tokens = [str(code or "").strip() or "validation_error"]
        for key in sorted(details.keys()):
            value = details.get(key)
            if value is None:
                continue
            rendered = ""
            if isinstance(value, (list, tuple, set)):
                items = [str(item).strip() for item in list(value) if str(item).strip()]
                rendered = ",".join(items[:80])
            else:
                rendered = str(value).strip()
            if rendered:
                tokens.append(f"{key}={rendered}")
        return "|".join(tokens)

    @staticmethod
    def _component_props_errors(component: str, props: Any) -> List[str]:
        errs: List[str] = []
        if not isinstance(props, Mapping):
            return [f"component_props_not_object:{component}"]

        if component == "ListBlock":
            items = props.get("items")
            if not isinstance(items, list):
                errs.append("component_props_invalid:ListBlock:items:not_array")
                return errs
            for idx, item in enumerate(items):
                if not isinstance(item, str) or not str(item).strip():
                    errs.append(f"component_props_invalid:ListBlock:items.{idx}:string_required")
                    break
            return errs

        if component == "ParagraphProse":
            text = props.get("text")
            if not isinstance(text, str) or not str(text).strip():
                errs.append("component_props_invalid:ParagraphProse:text:string_required")
            return errs

        if component == "SectionHeading":
            text = props.get("text")
            if not isinstance(text, str) or not str(text).strip():
                errs.append("component_props_invalid:SectionHeading:text:string_required")
            return errs

        if component == "CitationCard":
            title = props.get("title")
            if not isinstance(title, str) or not str(title).strip():
                errs.append("component_props_invalid:CitationCard:title:string_required")
            authors = props.get("authors")
            if authors is not None:
                if not isinstance(authors, list):
                    errs.append("component_props_invalid:CitationCard:authors:not_array")
                else:
                    for idx, item in enumerate(authors):
                        if not isinstance(item, str) or not str(item).strip():
                            errs.append(f"component_props_invalid:CitationCard:authors.{idx}:string_required")
                            break
            year = props.get("year")
            if year is not None:
                if isinstance(year, bool) or not isinstance(year, (str, int, float)):
                    errs.append("component_props_invalid:CitationCard:year:string_or_number_required")
                elif isinstance(year, str) and not year.strip():
                    errs.append("component_props_invalid:CitationCard:year:string_or_number_required")
            journal = props.get("journal")
            if journal is not None and not isinstance(journal, str):
                errs.append("component_props_invalid:CitationCard:journal:string_required")
            doi = props.get("doi")
            if doi is not None and not isinstance(doi, str):
                errs.append("component_props_invalid:CitationCard:doi:string_required")
            abstract_tldr = props.get("abstract_tldr")
            if abstract_tldr is not None and not isinstance(abstract_tldr, str):
                errs.append("component_props_invalid:CitationCard:abstract_tldr:string_required")
            return errs

        if component == "CompareInsightsCard":
            items = props.get("items")
            if items is not None:
                if not isinstance(items, list):
                    errs.append("component_props_invalid:CompareInsightsCard:items:not_array")
                else:
                    for idx, item in enumerate(items):
                        if not isinstance(item, (str, Mapping)):
                            errs.append(
                                f"component_props_invalid:CompareInsightsCard:items.{idx}:object_or_string_required"
                            )
                            break
            return errs

        if component == "InsightClusterCard":
            items = props.get("items")
            if not isinstance(items, list):
                errs.append("component_props_invalid:InsightClusterCard:items:not_array")
            elif not items:
                errs.append("component_props_invalid:InsightClusterCard:items:empty_array")
            else:
                for idx, item in enumerate(items):
                    if not isinstance(item, str) or not str(item).strip():
                        errs.append(f"component_props_invalid:InsightClusterCard:items.{idx}:string_required")
                        break
            title = props.get("title")
            if title is not None and not isinstance(title, str):
                errs.append("component_props_invalid:InsightClusterCard:title:string_required")
            tone = props.get("tone")
            if tone is not None and str(tone).strip().lower() not in {"finding", "claim", "implication"}:
                errs.append("component_props_invalid:InsightClusterCard:tone:enum_required")
            return errs

        if component == "SectionBridgeCard":
            text = props.get("text")
            if not isinstance(text, str) or not str(text).strip():
                errs.append("component_props_invalid:SectionBridgeCard:text:string_required")
            title = props.get("title")
            if title is not None and not isinstance(title, str):
                errs.append("component_props_invalid:SectionBridgeCard:title:string_required")
            return errs

        if component == "EquationBlock":
            latex = props.get("latex")
            if not isinstance(latex, str) or not str(latex).strip():
                errs.append("component_props_invalid:EquationBlock:latex:string_required")
            label = props.get("label")
            if label is not None and not isinstance(label, str):
                errs.append("component_props_invalid:EquationBlock:label:string_required")
            description = props.get("description")
            if description is not None and not isinstance(description, str):
                errs.append("component_props_invalid:EquationBlock:description:string_required")
            return errs

        if component == "MethodologyCard":
            steps = props.get("steps")
            if not isinstance(steps, list):
                errs.append("component_props_invalid:MethodologyCard:steps:not_array")
            elif not steps:
                errs.append("component_props_invalid:MethodologyCard:steps:empty_array")
            else:
                for idx, item in enumerate(steps):
                    if not isinstance(item, str) or not str(item).strip():
                        errs.append(f"component_props_invalid:MethodologyCard:steps.{idx}:string_required")
                        break
            title = props.get("title")
            if title is not None and not isinstance(title, str):
                errs.append("component_props_invalid:MethodologyCard:title:string_required")
            participants = props.get("participants")
            if participants is not None and not isinstance(participants, str):
                errs.append("component_props_invalid:MethodologyCard:participants:string_required")
            tools = props.get("tools")
            if tools is not None:
                if not isinstance(tools, list):
                    errs.append("component_props_invalid:MethodologyCard:tools:not_array")
                else:
                    for idx, item in enumerate(tools):
                        if not isinstance(item, str) or not str(item).strip():
                            errs.append(f"component_props_invalid:MethodologyCard:tools.{idx}:string_required")
                            break
            return errs

        if component == "CalloutBox":
            content = props.get("content")
            if not isinstance(content, str) or not str(content).strip():
                errs.append("component_props_invalid:CalloutBox:content:string_required")
            title = props.get("title")
            if title is not None and not isinstance(title, str):
                errs.append("component_props_invalid:CalloutBox:title:string_required")
            callout_type = props.get("type")
            if callout_type is not None:
                token = str(callout_type).strip().lower()
                if token not in {"info", "warning", "success", "tip"}:
                    errs.append("component_props_invalid:CalloutBox:type:enum_required")
            return errs

        if component == "AbstractCard":
            text = props.get("text")
            if not isinstance(text, str) or not str(text).strip():
                errs.append("component_props_invalid:AbstractCard:text:string_required")
            return errs

        return errs

    @staticmethod
    def _normalize_layout_mode(value: Any) -> str:
        token = str(value or "").strip().lower()
        return token if token in _ALLOWED_LAYOUT_MODES else ""

    @staticmethod
    def _normalize_zone_type(value: Any) -> str:
        token = str(value or "").strip().lower()
        return token if token in _ALLOWED_ZONE_TYPES else ""

    @staticmethod
    def _normalize_display_mode(value: Any) -> str:
        token = str(value or "").strip().lower()
        return token if token in _ALLOWED_DISPLAY_MODES else ""

    @staticmethod
    def _normalize_region(value: Any) -> str:
        token = str(value or "").strip()
        return token

    @staticmethod
    def _normalize_order_key(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
            if not math.isfinite(parsed):
                return None
            return parsed
        except Exception:
            return None

    def _sanitize_layout_tokens(self, value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        tokens: Dict[str, Any] = {}
        layout_mode = self._normalize_layout_mode(value.get("layout_mode") or value.get("mode"))
        if layout_mode:
            tokens["layout_mode"] = layout_mode
        regions_raw = value.get("regions")
        if isinstance(regions_raw, list):
            regions: List[Dict[str, Any]] = []
            for row in regions_raw:
                if not isinstance(row, Mapping):
                    continue
                region_id = self._normalize_region(row.get("id"))
                if not region_id:
                    continue
                region_item: Dict[str, Any] = {"id": region_id}
                kind = self._normalize_region(row.get("kind"))
                if kind:
                    region_item["kind"] = kind
                if isinstance(row.get("collapsed_by_default"), bool):
                    region_item["collapsed_by_default"] = bool(row.get("collapsed_by_default"))
                if row.get("width") is not None and not isinstance(row.get("width"), bool):
                    try:
                        region_item["width"] = float(row.get("width"))
                    except Exception:
                        pass
                regions.append(region_item)
            if regions:
                tokens["regions"] = regions
        for numeric_key in ("content_max_width", "sidebar_width", "column_count"):
            if value.get(numeric_key) is None or isinstance(value.get(numeric_key), bool):
                continue
            try:
                tokens[numeric_key] = float(value.get(numeric_key))
            except Exception:
                continue
        return tokens

    def validate(
        self,
        *,
        step_result: Mapping[str, Any],
        docmind_blocks: Sequence[Mapping[str, Any]],
        component_whitelist: Sequence[str],
        previous_ownership: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        known = self._docmind_index(docmind_blocks)
        known_ids = list(known.keys())
        known_set = set(known_ids)
        whitelist = {str(item).strip() for item in list(component_whitelist or []) if str(item).strip()}

        classification_rows = [
            row
            for row in list((step_result.get("classification") or {}).get("items") or [])
            if isinstance(row, Mapping)
        ]
        cleaning_rows = [
            row
            for row in list((step_result.get("cleaning") or {}).get("items") or [])
            if isinstance(row, Mapping)
        ]
        component_rows = [
            row
            for row in list((step_result.get("ui_plan_draft") or {}).get("components") or [])
            if isinstance(row, Mapping)
        ]

        cls_counts: Dict[str, int] = {}
        cls_buckets: Dict[str, str] = {}
        cls_invented: List[str] = []
        cls_duplicates: List[str] = []
        for row in classification_rows:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            cls_counts[layout_id] = int(cls_counts.get(layout_id, 0) + 1)
            if cls_counts[layout_id] > 1:
                cls_duplicates.append(layout_id)
            if layout_id not in known_set:
                cls_invented.append(layout_id)
            bucket = str(row.get("bucket") or "").strip()
            if bucket:
                cls_buckets[layout_id] = bucket

        missing_ids = [layout_id for layout_id in known_ids if int(cls_counts.get(layout_id, 0)) == 0]
        multi_assigned_ids = [layout_id for layout_id in known_ids if int(cls_counts.get(layout_id, 0)) > 1]

        clean_counts: Dict[str, int] = {}
        clean_invented: List[str] = []
        source_mutations: List[str] = []
        for row in cleaning_rows:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            clean_counts[layout_id] = int(clean_counts.get(layout_id, 0) + 1)
            if layout_id not in known_set:
                clean_invented.append(layout_id)
                continue
            if str(row.get("source_text") or "") != str((known.get(layout_id) or {}).get("source_text") or ""):
                source_mutations.append(layout_id)
        clean_duplicates = [layout_id for layout_id, count in clean_counts.items() if int(count) > 1]

        ownership_changes: List[str] = []
        if previous_ownership:
            for layout_id, prev_bucket in dict(previous_ownership).items():
                next_bucket = str(cls_buckets.get(layout_id) or "").strip()
                if layout_id in known_set and next_bucket and next_bucket != str(prev_bucket):
                    ownership_changes.append(layout_id)

        whitelist_errors: List[str] = []
        layout_contract_errors: List[str] = []
        covered_block_ids: Set[str] = set()
        layout_tokens_raw = (step_result.get("ui_plan_draft") or {}).get("layout_tokens")
        layout_tokens = self._sanitize_layout_tokens(layout_tokens_raw)
        if layout_tokens_raw is not None and not isinstance(layout_tokens_raw, Mapping):
            layout_contract_errors.append("layout_tokens_not_object")
        layout_mode = self._normalize_layout_mode((layout_tokens_raw or {}).get("layout_mode") if isinstance(layout_tokens_raw, Mapping) else None)
        if isinstance(layout_tokens_raw, Mapping) and (layout_tokens_raw.get("layout_mode") is not None) and (not layout_mode):
            layout_contract_errors.append("layout_mode_invalid")
        region_ids: Set[str] = set()
        if isinstance(layout_tokens_raw, Mapping) and isinstance(layout_tokens_raw.get("regions"), list) and layout_tokens_raw.get("regions") and not layout_mode:
            layout_contract_errors.append("layout_mode_required_when_regions_present")
        if isinstance(layout_tokens_raw, Mapping) and isinstance(layout_tokens_raw.get("regions"), list):
            for idx, row in enumerate(layout_tokens_raw.get("regions") or []):
                if not isinstance(row, Mapping):
                    layout_contract_errors.append(f"layout_regions_invalid_item:{idx}")
                    continue
                region_id = self._normalize_region(row.get("id"))
                if not region_id:
                    layout_contract_errors.append(f"layout_regions_missing_id:{idx}")
                    continue
                if region_id in region_ids:
                    layout_contract_errors.append(
                        self._structured_error(
                            "layout_regions_duplicate_id",
                            index=idx,
                            region_id=region_id,
                        )
                    )
                    continue
                region_ids.add(region_id)
        if layout_mode == "single_column" and len(region_ids) > 1:
            layout_contract_errors.append(
                self._structured_error(
                    "layout_mode_region_mismatch",
                    layout_mode=layout_mode,
                    region_count=len(region_ids),
                )
            )
        for row in component_rows:
            component = str(row.get("component") or "").strip()
            if not component:
                whitelist_errors.append("component_missing")
                continue
            if whitelist and component not in whitelist:
                whitelist_errors.append(f"component_not_allowed:{component}")
            source_block_ids_raw = row.get("source_block_ids")
            source_block_ids: List[str] = []
            if source_block_ids_raw is None:
                layout_contract_errors.append(
                    self._structured_error("source_block_ids_missing", component=component)
                )
            elif not isinstance(source_block_ids_raw, list):
                layout_contract_errors.append(
                    self._structured_error("source_block_ids_not_array", component=component)
                )
            else:
                source_block_ids = [
                    str(item).strip()
                    for item in source_block_ids_raw
                    if str(item).strip()
                ]
            if not source_block_ids:
                whitelist_errors.append(f"missing_source_block_ids:{component}")
            if len(set(source_block_ids)) < len(source_block_ids):
                duplicate_source_ids: List[str] = []
                seen_source_ids: Set[str] = set()
                for source_id in source_block_ids:
                    if source_id in seen_source_ids and source_id not in duplicate_source_ids:
                        duplicate_source_ids.append(source_id)
                    seen_source_ids.add(source_id)
                layout_contract_errors.append(
                    self._structured_error(
                        "source_block_ids_duplicate",
                        component=component,
                        source_block_ids=duplicate_source_ids[:12],
                    )
                )
            source_block_ids = list(dict.fromkeys(source_block_ids))
            unknown_source_ids = [item for item in source_block_ids if item not in known_set]
            if unknown_source_ids:
                layout_contract_errors.append(
                    f"unknown_source_block_ids:{component}:{','.join(unknown_source_ids[:12])}"
                )
                layout_contract_errors.append(
                    self._structured_error(
                        "source_block_ids_unknown",
                        component=component,
                        source_block_ids=unknown_source_ids[:12],
                    )
                )
            for source_id in source_block_ids:
                if source_id in known_set:
                    covered_block_ids.add(source_id)
            props_errors = self._component_props_errors(component, row.get("props"))
            if props_errors:
                whitelist_errors.extend(props_errors)
            zone_type = self._normalize_zone_type(row.get("zone_type"))
            if "zone_type" not in row:
                layout_contract_errors.append(
                    self._structured_error(
                        "required_layout_field_missing",
                        component=component,
                        field="zone_type",
                    )
                )
            elif not zone_type:
                layout_contract_errors.append(f"zone_type_invalid:{component}")
                layout_contract_errors.append(
                    self._structured_error(
                        "zone_type_invalid",
                        component=component,
                        zone_type=row.get("zone_type"),
                    )
                )

            display_mode = self._normalize_display_mode(row.get("display"))
            if "display" not in row:
                layout_contract_errors.append(
                    self._structured_error(
                        "required_layout_field_missing",
                        component=component,
                        field="display",
                    )
                )
            elif not display_mode:
                layout_contract_errors.append(f"display_invalid:{component}")
                layout_contract_errors.append(
                    self._structured_error(
                        "display_invalid",
                        component=component,
                        display=row.get("display"),
                    )
                )

            column_id = self._normalize_region(row.get("column_id"))
            if "column_id" not in row:
                layout_contract_errors.append(
                    self._structured_error(
                        "required_layout_field_missing",
                        component=component,
                        field="column_id",
                    )
                )
            elif not column_id:
                layout_contract_errors.append(f"column_id_invalid:{component}")
                layout_contract_errors.append(
                    self._structured_error(
                        "column_id_invalid",
                        component=component,
                        column_id=row.get("column_id"),
                    )
                )
            elif region_ids and column_id not in region_ids:
                layout_contract_errors.append(
                    self._structured_error(
                        "column_id_not_declared",
                        component=component,
                        column_id=column_id,
                    )
                )

            region = self._normalize_region(row.get("region"))
            if "region" not in row:
                layout_contract_errors.append(
                    self._structured_error(
                        "required_layout_field_missing",
                        component=component,
                        field="region",
                    )
                )
            elif not region:
                layout_contract_errors.append(f"region_invalid:{component}")
                layout_contract_errors.append(
                    self._structured_error(
                        "region_invalid",
                        component=component,
                        region=row.get("region"),
                    )
                )
            elif region_ids and region not in region_ids:
                layout_contract_errors.append(f"region_not_declared:{component}:{region}")
                layout_contract_errors.append(
                    self._structured_error(
                        "region_not_declared",
                        component=component,
                        region=region,
                    )
                )

            order_key_value: Optional[float] = None
            if "order_key" not in row:
                layout_contract_errors.append(
                    self._structured_error(
                        "required_layout_field_missing",
                        component=component,
                        field="order_key",
                    )
                )
            else:
                order_key_value = self._normalize_order_key(row.get("order_key"))
                if order_key_value is None:
                    layout_contract_errors.append(f"order_key_invalid:{component}")
                    layout_contract_errors.append(
                        self._structured_error(
                            "order_key_invalid",
                            component=component,
                            order_key=row.get("order_key"),
                        )
                    )
            if "order" in row:
                order_value = self._normalize_order_key(row.get("order"))
                if order_value is None:
                    layout_contract_errors.append(
                        self._structured_error(
                            "order_invalid",
                            component=component,
                            order=row.get("order"),
                        )
                    )
                elif order_key_value is not None and abs(float(order_value) - float(order_key_value)) > 1e-9:
                    layout_contract_errors.append(
                        self._structured_error(
                            "order_order_key_mismatch",
                            component=component,
                            order=order_value,
                            order_key=order_key_value,
                        )
                    )

        missing_known_block_ids = [layout_id for layout_id in known_ids if layout_id not in covered_block_ids]
        no_drop_errors: List[str] = []
        if missing_known_block_ids:
            no_drop_errors.append(
                self._structured_error(
                    "no_drop_blocks_missing",
                    missing_block_ids=missing_known_block_ids[:80],
                    missing_count=len(missing_known_block_ids),
                )
            )

        id_integrity_errors = sorted(
            set(
                list(cls_invented)
                + list(cls_duplicates)
                + list(clean_invented)
                + list(clean_duplicates)
            )
        )
        id_integrity_passed = len(id_integrity_errors) == 0

        full_coverage_errors: List[str] = []
        if missing_ids:
            full_coverage_errors.append(f"missing:{','.join(missing_ids[:80])}")
        if multi_assigned_ids:
            full_coverage_errors.append(f"duplicate_assignment:{','.join(multi_assigned_ids[:80])}")
        full_coverage_passed = len(full_coverage_errors) == 0

        gates = {
            "id_integrity": self._gate(id_integrity_passed, id_integrity_errors),
            "full_coverage": self._gate(full_coverage_passed, full_coverage_errors),
            "whitelist_only": self._gate(len(whitelist_errors) == 0, whitelist_errors),
            "layout_contract": self._gate(len(layout_contract_errors) == 0, layout_contract_errors),
            "no_drop_blocks": self._gate(len(no_drop_errors) == 0, no_drop_errors),
            "ownership_unchanged": self._gate(len(ownership_changes) == 0, ownership_changes),
            "non_empty_plan_for_non_empty_input": self._gate(
                (not bool(known_ids)) or bool(component_rows),
                [] if ((not bool(known_ids)) or bool(component_rows)) else ["empty_ui_plan_for_non_empty_input"],
            ),
            "source_text_immutable": self._gate(len(source_mutations) == 0, source_mutations),
        }

        passed = bool(all(bool((gates.get(name) or {}).get("passed")) for name in HARD_GATES))
        errors: List[str] = []
        for gate_name in HARD_GATES:
            gate_errors = list((gates.get(gate_name) or {}).get("errors") or [])
            if gate_errors:
                errors.extend([f"{gate_name}:{item}" for item in gate_errors])

        return {
            "passed": passed,
            "gates": gates,
            "errors": errors,
            "missing_layout_ids": missing_ids,
            "missing_known_block_ids": missing_known_block_ids,
            "duplicate_layout_ids": sorted(set(cls_duplicates)),
            "invented_layout_ids": sorted(set(cls_invented)),
            "ownership_changes": sorted(set(ownership_changes)),
            "ownership_map": {
                layout_id: str(bucket)
                for layout_id, bucket in cls_buckets.items()
                if layout_id in known_set and bucket in {"main_content", "aux_content"}
            },
        }

    def deterministic_repair(
        self,
        *,
        step_result: Mapping[str, Any],
        docmind_blocks: Sequence[Mapping[str, Any]],
        component_whitelist: Sequence[str],
        previous_ownership: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        known = self._docmind_index(docmind_blocks)
        known_ids = list(known.keys())
        known_set = set(known_ids)
        whitelist = {str(item).strip() for item in list(component_whitelist or []) if str(item).strip()}

        repaired: Dict[str, Any] = {
            "classification": {"items": []},
            "cleaning": {"items": []},
            "ui_plan_draft": {"components": [], "layout_tokens": {}},
        }
        fixes_applied: List[Dict[str, Any]] = []

        classification_rows = [
            row
            for row in list((step_result.get("classification") or {}).get("items") or [])
            if isinstance(row, Mapping)
        ]
        cls_map: Dict[str, Dict[str, Any]] = {}
        dropped_invented: List[str] = []
        dropped_duplicate: List[str] = []
        for row in classification_rows:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            if layout_id not in known_set:
                dropped_invented.append(layout_id)
                continue
            if layout_id in cls_map:
                dropped_duplicate.append(layout_id)
                continue
            bucket = str(row.get("bucket") or "").strip().lower()
            if bucket not in {"main_content", "aux_content"}:
                bucket = self._default_bucket(known.get(layout_id) or {})
            if previous_ownership and layout_id in previous_ownership:
                prev_bucket = str(previous_ownership.get(layout_id) or "").strip().lower()
                if prev_bucket in {"main_content", "aux_content"}:
                    bucket = prev_bucket
            cls_map[layout_id] = {
                "layout_id": layout_id,
                "bucket": bucket,
                "role": self._normalize_role(str(row.get("role") or ""), known.get(layout_id) or {}),
                "confidence": self._bounded_confidence(row.get("confidence"), 0.5),
                "reason": str(row.get("reason") or "deterministic_repair").strip() or "deterministic_repair",
            }

        missing_ids: List[str] = []
        for layout_id in known_ids:
            if layout_id in cls_map:
                continue
            missing_ids.append(layout_id)
            block = known.get(layout_id) or {}
            cls_map[layout_id] = {
                "layout_id": layout_id,
                "bucket": self._default_bucket(block),
                "role": self._normalize_role("", block),
                "confidence": 0.42,
                "reason": "deterministic_missing_fill",
            }

        repaired["classification"]["items"] = [cls_map[item] for item in known_ids if item in cls_map]

        if dropped_invented:
            fixes_applied.append(
                {
                    "gate": "id_integrity",
                    "action": "drop_invented_layout_ids",
                    "affected_layout_ids": sorted(set(dropped_invented)),
                }
            )
        if dropped_duplicate:
            fixes_applied.append(
                {
                    "gate": "id_integrity",
                    "action": "dedupe_layout_ids",
                    "affected_layout_ids": sorted(set(dropped_duplicate)),
                }
            )
        if missing_ids:
            fixes_applied.append(
                {
                    "gate": "full_coverage",
                    "action": "fill_missing_layout_ids",
                    "affected_layout_ids": missing_ids,
                }
            )

        cleaning_rows = [
            row
            for row in list((step_result.get("cleaning") or {}).get("items") or [])
            if isinstance(row, Mapping)
        ]
        clean_map: Dict[str, Dict[str, Any]] = {}
        for row in cleaning_rows:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id or layout_id not in known_set or layout_id in clean_map:
                continue
            canonical_source = str((known.get(layout_id) or {}).get("source_text") or "")
            candidate_source = str(row.get("source_text") or "")
            candidate_norm = str(row.get("normalized_text") or candidate_source)
            clean_ops = [str(item).strip() for item in list(row.get("clean_ops") or []) if str(item).strip()]
            clean_conf = self._bounded_confidence(row.get("clean_confidence"), 0.0)
            repaired_item, audit = self._apply_noise_policy(
                layout_id=layout_id,
                canonical_source=canonical_source,
                candidate_source=candidate_source,
                candidate_normalized=candidate_norm,
                clean_ops=clean_ops,
                clean_confidence=clean_conf,
                suggestion_origin=str(row.get("suggestion_origin") or "model").strip() or "model",
            )
            clean_map[layout_id] = repaired_item
            if audit:
                fixes_applied.append(
                    {
                        "gate": "source_text_immutable",
                        "action": audit,
                        "affected_layout_ids": [layout_id],
                    }
                )

        for layout_id in known_ids:
            if layout_id in clean_map:
                continue
            source_text = str((known.get(layout_id) or {}).get("source_text") or "")
            clean_map[layout_id] = {
                "layout_id": layout_id,
                "source_text": source_text,
                "normalized_text": self._apply_safe_normalize(source_text),
                "clean_ops": ["whitespace_normalize"] if source_text else [],
                "clean_confidence": 1.0,
                "needs_review": False,
                "suggestion_origin": "deterministic_repair",
                "applied": True,
                "reject_reason": "",
            }

        repaired["cleaning"]["items"] = [clean_map[item] for item in known_ids if item in clean_map]

        component_rows = [
            row
            for row in list((step_result.get("ui_plan_draft") or {}).get("components") or [])
            if isinstance(row, Mapping)
        ]
        repaired_components: List[Dict[str, Any]] = []
        dropped_components: List[str] = []
        for row in component_rows:
            component_name = str(row.get("component") or "").strip()
            if not component_name:
                continue
            if whitelist and component_name not in whitelist:
                dropped_components.append(component_name)
                continue
            source_ids_raw = row.get("source_block_ids")
            if not isinstance(source_ids_raw, list):
                dropped_components.append(component_name)
                continue
            source_block_ids = [
                str(item).strip()
                for item in source_ids_raw
                if str(item).strip()
            ]
            source_block_ids = [item for item in source_block_ids if item in known_set]
            if not source_block_ids:
                dropped_components.append(component_name)
                continue
            props = copy.deepcopy(dict(row.get("props") or {}))
            if self._component_props_errors(component_name, props):
                dropped_components.append(component_name)
                continue
            repaired_row: Dict[str, Any] = {
                "component": component_name,
                "source_block_ids": list(dict.fromkeys(source_block_ids)),
                "props": props,
            }
            zone_type = self._normalize_zone_type(row.get("zone_type"))
            if zone_type:
                repaired_row["zone_type"] = zone_type
            column_id = self._normalize_region(row.get("column_id"))
            if column_id:
                repaired_row["column_id"] = column_id
            region = self._normalize_region(row.get("region"))
            if region:
                repaired_row["region"] = region
            display_mode = self._normalize_display_mode(row.get("display"))
            if display_mode:
                repaired_row["display"] = display_mode
            order_key = self._normalize_order_key(row.get("order_key"))
            if order_key is None and "order" in row:
                order_key = self._normalize_order_key(row.get("order"))
            if order_key is not None:
                repaired_row["order_key"] = order_key
            repaired_components.append(repaired_row)

        if dropped_components:
            fixes_applied.append(
                {
                    "gate": "whitelist_only",
                    "action": "drop_non_whitelist_or_missing_source_components",
                    "affected_layout_ids": sorted(set(dropped_components)),
                }
            )

        if not repaired_components and known_ids:
            repaired_components.extend(
                self._build_deterministic_components(
                    classification_items=repaired["classification"]["items"],
                    cleaning_items=repaired["cleaning"]["items"],
                    docmind_index=known,
                    component_whitelist=whitelist,
                )
            )
            fixes_applied.append(
                {
                    "gate": "non_empty_plan_for_non_empty_input",
                    "action": "inject_deterministic_baseline_components",
                    "affected_layout_ids": [
                        str(item.get("layout_id") or "")
                        for item in repaired["classification"]["items"]
                        if str(item.get("layout_id") or "")
                    ],
                }
            )

        covered_layout_ids: Set[str] = set()
        for row in repaired_components:
            source_ids_raw = row.get("source_block_ids")
            if not isinstance(source_ids_raw, list):
                continue
            for item in source_ids_raw:
                source_id = str(item).strip()
                if source_id and source_id in known_set:
                    covered_layout_ids.add(source_id)
        missing_for_coverage = [layout_id for layout_id in known_ids if layout_id not in covered_layout_ids]
        no_drop_filled_ids: List[str] = []
        if missing_for_coverage:
            next_order_key = float(len(repaired_components) + 1)
            for layout_id in missing_for_coverage:
                fallback_row = self._build_collapsed_fallback_component(
                    layout_id=layout_id,
                    clean_map=clean_map,
                    docmind_index=known,
                    component_whitelist=whitelist,
                    default_order_key=next_order_key,
                )
                if not fallback_row:
                    continue
                repaired_components.append(fallback_row)
                no_drop_filled_ids.append(layout_id)
                next_order_key += 1.0
        if no_drop_filled_ids:
            fixes_applied.append(
                {
                    "gate": "no_drop_blocks",
                    "action": "append_collapsed_components_for_missing_blocks",
                    "affected_layout_ids": no_drop_filled_ids,
                }
            )
        layout_tokens = self._sanitize_layout_tokens((step_result.get("ui_plan_draft") or {}).get("layout_tokens"))
        token_region_ids = [
            str(row.get("id") or "").strip()
            for row in list(layout_tokens.get("regions") or [])
            if isinstance(row, Mapping) and str(row.get("id") or "").strip()
        ]
        token_region_set = set(token_region_ids)

        lower_region_pairs = [(region_id, region_id.lower()) for region_id in token_region_ids]
        main_region_candidates = [
            region_id
            for region_id, token in lower_region_pairs
            if any(key in token for key in ("main", "content", "body", "primary"))
        ]
        side_region_candidates = [
            region_id
            for region_id, token in lower_region_pairs
            if any(key in token for key in ("side", "sidebar", "rail", "aux", "meta"))
        ]

        def _default_region_for_bucket(bucket: str) -> str:
            is_aux = str(bucket or "") == "aux_content"
            if token_region_ids:
                if is_aux and side_region_candidates:
                    return side_region_candidates[0]
                if (not is_aux) and main_region_candidates:
                    return main_region_candidates[0]
                return token_region_ids[0]
            return "sidebar" if is_aux else "main"

        normalized_components: List[Dict[str, Any]] = []
        compat_component_ids: List[str] = []
        used_regions: List[str] = []
        for idx, row in enumerate(repaired_components, start=1):
            if not isinstance(row, Mapping):
                continue
            source_ids = [
                str(item).strip()
                for item in list(row.get("source_block_ids") or [])
                if str(item).strip()
            ]
            source_ids = [item for item in source_ids if item in known_set]
            if not source_ids:
                continue
            source_ids = list(dict.fromkeys(source_ids))
            component_name = str(row.get("component") or "").strip()
            if not component_name:
                continue

            primary_layout_id = source_ids[0]
            bucket = str((cls_map.get(primary_layout_id) or {}).get("bucket") or "").strip().lower()
            if bucket not in {"main_content", "aux_content"}:
                bucket = self._default_bucket(known.get(primary_layout_id) or {})
            default_zone_type = "side_context" if bucket == "aux_content" else "main_body"
            default_region = _default_region_for_bucket(bucket)
            default_column_id = (
                default_region if (token_region_set and default_region in token_region_set) else ("sidebar" if bucket == "aux_content" else "main")
            )
            default_display = "collapsed" if bucket == "aux_content" else "default"
            default_order_key = float(idx)

            normalized_row: Dict[str, Any] = {
                "component": component_name,
                "source_block_ids": source_ids,
                "props": copy.deepcopy(dict(row.get("props") or {})),
            }
            compat_filled_fields: List[str] = []

            zone_type = self._normalize_zone_type(row.get("zone_type"))
            if not zone_type:
                zone_type = default_zone_type
                compat_filled_fields.append("zone_type")
            normalized_row["zone_type"] = zone_type

            column_id = self._normalize_region(row.get("column_id"))
            if not column_id or (token_region_set and column_id not in token_region_set):
                column_id = default_column_id
                compat_filled_fields.append("column_id")
            normalized_row["column_id"] = column_id

            region = self._normalize_region(row.get("region"))
            if not region or (token_region_set and region not in token_region_set):
                region = default_region
                compat_filled_fields.append("region")
            normalized_row["region"] = region
            if region and region not in used_regions:
                used_regions.append(region)

            display_mode = self._normalize_display_mode(row.get("display"))
            if not display_mode:
                display_mode = default_display
                compat_filled_fields.append("display")
            normalized_row["display"] = display_mode

            order_key = self._normalize_order_key(row.get("order_key"))
            if order_key is None and "order" in row:
                order_key = self._normalize_order_key(row.get("order"))
            if order_key is None:
                order_key = default_order_key
                compat_filled_fields.append("order_key")
            normalized_row["order_key"] = float(order_key)

            if compat_filled_fields:
                normalized_row["compat_filled"] = True
                normalized_row["compat_filled_fields"] = sorted(set(compat_filled_fields))
                compat_component_ids.append(component_name)
            normalized_components.append(normalized_row)

        if compat_component_ids:
            fixes_applied.append(
                {
                    "gate": "layout_contract",
                    "action": "compat_fill_missing_layout_fields",
                    "affected_layout_ids": sorted(set(compat_component_ids)),
                }
            )

        if not str(layout_tokens.get("layout_mode") or "").strip():
            has_side = any(str(row.get("zone_type") or "") == "side_context" for row in normalized_components)
            layout_tokens["layout_mode"] = "split" if has_side else "single_column"
        if not list(layout_tokens.get("regions") or []):
            has_side = any(str(row.get("zone_type") or "") == "side_context" for row in normalized_components)
            if has_side:
                layout_tokens["regions"] = [
                    {"id": "main", "kind": "content"},
                    {"id": "sidebar", "kind": "rail", "collapsed_by_default": True},
                ]
            else:
                layout_tokens["regions"] = [{"id": "main", "kind": "content"}]

        repaired["ui_plan_draft"] = {
            "components": normalized_components,
            "layout_tokens": layout_tokens,
        }

        return {
            "step_result": repaired,
            "fixes_applied": fixes_applied,
        }

    def build_deterministic_baseline_step_result(
        self,
        *,
        docmind_blocks: Sequence[Mapping[str, Any]],
        component_whitelist: Sequence[str],
        previous_ownership: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        empty_seed = {
            "classification": {"items": []},
            "cleaning": {"items": []},
            "ui_plan_draft": {"components": [], "layout_tokens": {}},
        }
        repaired = self.deterministic_repair(
            step_result=empty_seed,
            docmind_blocks=docmind_blocks,
            component_whitelist=component_whitelist,
            previous_ownership=previous_ownership,
        )
        return dict(repaired.get("step_result") or empty_seed)

    @staticmethod
    def _default_bucket(block: Mapping[str, Any]) -> str:
        block_type = str(block.get("type") or "").strip().lower()
        sub_type = str(block.get("subType") or block.get("sub_type") or "").strip().lower()
        source_text = str(block.get("source_text") or "").strip().lower()
        if block_type in {"figure", "figure_name", "caption"} or sub_type in {"picture", "figure", "figure_name", "caption", "table_caption"}:
            return "main_content"
        if block_type in AUX_DEFAULT_TYPES or sub_type in AUX_DEFAULT_TYPES:
            return "aux_content"
        if any(keyword in source_text for keyword in AUX_KEYWORDS):
            return "aux_content"
        return "main_content"

    @staticmethod
    def _normalize_role(role: str, block: Mapping[str, Any]) -> str:
        token = str(role or "").strip().lower()
        if token in {
            "title",
            "section_heading",
            "paragraph",
            "figure",
            "caption",
            "metadata",
            "header",
            "footer",
            "reference",
            "noise",
            "unknown",
        }:
            return token
        block_type = str(block.get("type") or "").strip().lower()
        sub_type = str(block.get("subType") or block.get("sub_type") or "").strip().lower()
        if block_type == "title" or sub_type in {"doc_title", "title"}:
            return "title"
        if sub_type in {"section_title", "heading", "head"}:
            return "section_heading"
        if block_type == "figure" or sub_type in {"picture", "figure"}:
            return "figure"
        if block_type in {"figure_name", "caption"} or sub_type in {"figure_name", "caption", "table_caption"}:
            return "caption"
        if block_type == "text" and sub_type in {"para", "paragraph", "body"}:
            return "paragraph"
        if block_type in AUX_DEFAULT_TYPES or sub_type in AUX_DEFAULT_TYPES:
            return "metadata"
        return "unknown"

    @staticmethod
    def _bounded_confidence(value: Any, fallback: float) -> float:
        score = _safe_float(value, fallback)
        return max(0.0, min(1.0, float(score)))

    def _apply_noise_policy(
        self,
        *,
        layout_id: str,
        canonical_source: str,
        candidate_source: str,
        candidate_normalized: str,
        clean_ops: List[str],
        clean_confidence: float,
        suggestion_origin: str,
    ) -> Tuple[Dict[str, Any], str]:
        source_text = str(canonical_source or "")
        candidate_source_text = str(candidate_source or "")
        normalized = str(candidate_normalized or source_text)
        normalized_ops = [str(item).strip() for item in list(clean_ops or []) if str(item).strip()]

        reject_reason = ""
        applied = True
        ops_to_use = list(normalized_ops)

        if candidate_source_text != source_text:
            reject_reason = NOISE_REJECT_SOURCE_TEXT
            applied = False
        elif any(item not in SAFE_CLEAN_OPS for item in normalized_ops):
            reject_reason = "unsafe_clean_ops"
            applied = False
        elif float(clean_confidence) < 0.9:
            reject_reason = "low_clean_confidence"
            applied = False

        if applied:
            normalized_text = self._apply_safe_normalize(normalized)
            needs_review = False
            audit = ""
        else:
            normalized_text = source_text
            needs_review = True
            ops_to_use = []
            audit = f"reject_noise_suggestion:{reject_reason}"

        hygiene_issues = _text_hygiene_issues(normalized_text)
        if hygiene_issues:
            normalized_text = source_text
            needs_review = True
            applied = False
            reject_reason = "text_hygiene_reject"
            ops_to_use = []
            audit = "reject_noise_suggestion:text_hygiene_reject"

        item = {
            "layout_id": layout_id,
            "source_text": source_text,
            "normalized_text": normalized_text,
            "clean_ops": ops_to_use,
            "clean_confidence": self._bounded_confidence(clean_confidence, 0.0),
            "needs_review": bool(needs_review),
            "suggestion_origin": suggestion_origin or "model",
            "applied": bool(applied),
            "reject_reason": reject_reason,
        }
        return item, audit

    @staticmethod
    def _apply_safe_normalize(text: str) -> str:
        cleaned = str(text or "")
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.replace(" ,", ",").replace(" .", ".")
        return cleaned.strip()

    @staticmethod
    def _fallback_component_candidates(component_whitelist: Set[str]) -> List[str]:
        preferred = [
            "ParagraphProse",
            "SectionHeading",
            "ListBlock",
            "AbstractCard",
            "CalloutBox",
            "InsightClusterCard",
            "SectionBridgeCard",
            "MethodologyCard",
            "EquationBlock",
            "CitationCard",
        ]
        if not component_whitelist:
            return list(preferred)
        candidates = [item for item in preferred if item in component_whitelist]
        for item in sorted(component_whitelist):
            if item not in candidates:
                candidates.append(item)
        return candidates

    @staticmethod
    def _fallback_props_for_component(component: str, *, text: str, layout_id: str) -> Dict[str, Any]:
        fallback_text = str(text or "").strip() or str(layout_id or "").strip() or "content"
        if component == "ParagraphProse":
            return {"text": fallback_text}
        if component == "SectionHeading":
            return {"text": fallback_text, "level": 2}
        if component == "ListBlock":
            return {"items": [fallback_text]}
        if component == "AbstractCard":
            return {"text": fallback_text}
        if component == "CalloutBox":
            return {"type": "info", "content": fallback_text}
        if component == "InsightClusterCard":
            return {"items": [fallback_text]}
        if component == "SectionBridgeCard":
            return {"text": fallback_text}
        if component == "MethodologyCard":
            return {"steps": [fallback_text]}
        if component == "EquationBlock":
            return {"latex": fallback_text}
        if component == "CitationCard":
            return {"title": fallback_text}
        return {}

    def _build_collapsed_fallback_component(
        self,
        *,
        layout_id: str,
        clean_map: Mapping[str, Mapping[str, Any]],
        docmind_index: Mapping[str, Mapping[str, Any]],
        component_whitelist: Set[str],
        default_order_key: float,
    ) -> Dict[str, Any]:
        clean_item = dict(clean_map.get(layout_id) or {})
        source_text = str(
            clean_item.get("normalized_text")
            or clean_item.get("source_text")
            or (docmind_index.get(layout_id) or {}).get("source_text")
            or layout_id
        )
        normalized_text = _normalize_spaces(source_text) or layout_id
        for component_name in self._fallback_component_candidates(component_whitelist):
            props = self._fallback_props_for_component(
                component_name,
                text=normalized_text,
                layout_id=layout_id,
            )
            if self._component_props_errors(component_name, props):
                continue
            return {
                "component": component_name,
                "source_block_ids": [layout_id],
                "props": props,
                "display": "collapsed",
                "order_key": float(default_order_key),
            }
        return {}

    def _build_deterministic_components(
        self,
        *,
        classification_items: Sequence[Mapping[str, Any]],
        cleaning_items: Sequence[Mapping[str, Any]],
        docmind_index: Mapping[str, Mapping[str, Any]],
        component_whitelist: Set[str],
    ) -> List[Dict[str, Any]]:
        text_by_id = {
            str(row.get("layout_id") or ""): str(row.get("normalized_text") or row.get("source_text") or "")
            for row in list(cleaning_items or [])
            if str(row.get("layout_id") or "")
        }
        allow_para = (not component_whitelist) or ("ParagraphProse" in component_whitelist)
        allow_heading = (not component_whitelist) or ("SectionHeading" in component_whitelist)
        allow_figure = (not component_whitelist) or ("FigurePanel" in component_whitelist)

        components: List[Dict[str, Any]] = []
        pending_figure_component: Optional[Dict[str, Any]] = None

        def append_caption(target: Dict[str, Any], caption_text: str, layout_id: str) -> None:
            if caption_text:
                existing_caption = _normalize_spaces(str(((target.get("props") or {}).get("caption") or "")))
                merged_caption = caption_text if not existing_caption else f"{existing_caption} {caption_text}"
                target.setdefault("props", {})["caption"] = merged_caption.strip()
            source_block_ids = target.setdefault("source_block_ids", [])
            if layout_id and layout_id not in source_block_ids:
                source_block_ids.append(layout_id)

        for row in classification_items:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            role = str(row.get("role") or "").strip()
            bucket = str(row.get("bucket") or "").strip()
            text = _normalize_spaces(
                text_by_id.get(layout_id)
                or str((docmind_index.get(layout_id) or {}).get("source_text") or "")
            )
            if role == "figure" and allow_figure:
                figure_component = {
                    "component": "FigurePanel",
                    "source_block_ids": [layout_id],
                    "props": {
                        "caption": text,
                        "image_url": "",
                    },
                }
                components.append(figure_component)
                pending_figure_component = figure_component
                if len(components) >= 24:
                    break
                continue
            if role == "caption" and allow_figure:
                if pending_figure_component is None:
                    figure_component = {
                        "component": "FigurePanel",
                        "source_block_ids": [layout_id],
                        "props": {
                            "caption": text,
                            "image_url": "",
                        },
                    }
                    components.append(figure_component)
                    pending_figure_component = figure_component
                else:
                    append_caption(pending_figure_component, text, layout_id)
                if len(components) >= 24:
                    break
                continue

            pending_figure_component = None
            if bucket != "main_content":
                continue
            if not text:
                continue
            if role in {"section_heading", "title"} and allow_heading:
                components.append(
                    {
                        "component": "SectionHeading",
                        "source_block_ids": [layout_id],
                        "props": {"text": text, "level": 2},
                    }
                )
            elif allow_para:
                components.append(
                    {
                        "component": "ParagraphProse",
                        "source_block_ids": [layout_id],
                        "props": {"text": text},
                    }
                )
            if len(components) >= 24:
                break

        if components:
            return components

        first_id = next(iter(docmind_index.keys()), "")
        if not first_id:
            return []
        text = _normalize_spaces(str((docmind_index.get(first_id) or {}).get("source_text") or ""))
        if allow_para:
            return [{"component": "ParagraphProse", "source_block_ids": [first_id], "props": {"text": text or first_id}}]
        if allow_heading:
            return [{"component": "SectionHeading", "source_block_ids": [first_id], "props": {"text": text or first_id, "level": 2}}]
        return []

