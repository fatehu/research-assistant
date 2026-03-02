from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

HARD_GATES: Tuple[str, ...] = (
    "id_integrity",
    "full_coverage",
    "whitelist_only",
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

        return errs

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
        for row in component_rows:
            component = str(row.get("component") or "").strip()
            if not component:
                whitelist_errors.append("component_missing")
                continue
            if whitelist and component not in whitelist:
                whitelist_errors.append(f"component_not_allowed:{component}")
            source_block_ids = [
                str(item).strip()
                for item in list(row.get("source_block_ids") or [])
                if str(item).strip()
            ]
            if not source_block_ids:
                whitelist_errors.append(f"missing_source_block_ids:{component}")
            props_errors = self._component_props_errors(component, row.get("props"))
            if props_errors:
                whitelist_errors.extend(props_errors)

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
            source_block_ids = [
                str(item).strip()
                for item in list(row.get("source_block_ids") or [])
                if str(item).strip()
            ]
            if not source_block_ids:
                dropped_components.append(component_name)
                continue
            props = copy.deepcopy(dict(row.get("props") or {}))
            if self._component_props_errors(component_name, props):
                dropped_components.append(component_name)
                continue
            repaired_components.append(
                {
                    "component": component_name,
                    "source_block_ids": list(dict.fromkeys(source_block_ids)),
                    "props": props,
                }
            )

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

        repaired["ui_plan_draft"] = {
            "components": repaired_components,
            "layout_tokens": copy.deepcopy(dict((step_result.get("ui_plan_draft") or {}).get("layout_tokens") or {})),
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

        components: List[Dict[str, Any]] = []
        for row in classification_items:
            layout_id = str(row.get("layout_id") or "").strip()
            if str(row.get("bucket") or "") != "main_content" or not layout_id:
                continue
            role = str(row.get("role") or "").strip()
            text = _normalize_spaces(
                text_by_id.get(layout_id)
                or str((docmind_index.get(layout_id) or {}).get("source_text") or "")
            )
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

