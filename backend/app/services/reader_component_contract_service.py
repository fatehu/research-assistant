"""
Reader component contract service.

Provides a shared whitelist and lightweight schema validation for:
- component registration contracts (backend side)
- agent ui_ops sanitization
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class ReaderComponentContractService:
    """Single source of truth for reader component contracts."""

    COMPONENT_SCHEMAS: Dict[str, Dict[str, Any]] = {
        "PaperHeaderCard": {"required": ["title"], "properties": {"title": "string"}},
        "MetadataSidebarCard": {"required": [], "properties": {"items": "array"}},
        "SectionTOC": {"required": [], "properties": {"items": "array"}},
        "SectionHeading": {"required": ["text"], "properties": {"text": "string", "level": "number"}},
        "ParagraphProse": {"required": ["text"], "properties": {"text": "string", "paragraphs": "array"}},
        "ListBlock": {"required": ["items"], "properties": {"items": "array"}},
        "FigurePanel": {"required": [], "properties": {"caption": "string", "image_url": "string", "source_label": "string", "ai_insight": "string"}},
        "TablePanel": {"required": [], "properties": {"title": "string", "rows": "array", "ai_insight": "string"}},
        "CitationLinks": {"required": [], "properties": {"links": "array"}},
        "KeyTakeaways": {"required": [], "properties": {"items": "array"}},
        "AnnotationRail": {"required": [], "properties": {"items": "array"}},
        "QualityBadge": {"required": [], "properties": {}},
        "QualityPanel": {"required": [], "properties": {}},
        "InlineQuerySlot": {"required": [], "properties": {"placeholder": "string"}},
        "AnswerCard": {"required": ["question", "answer"], "properties": {"question": "string", "answer": "string"}},
        "CompareInsightsCard": {"required": [], "properties": {"items": "array"}},
        "PdfSnippetCard": {"required": [], "properties": {"title": "string", "description": "string"}},
        "ContextRail": {"required": [], "properties": {"title": "string", "items": "array"}},
        "CitationCard": {"required": ["title"], "properties": {"citation_key": "string", "authors": "array", "year": "string_or_number", "title": "string", "journal": "string", "doi": "string", "abstract_tldr": "string"}},
        "EquationBlock": {"required": ["latex"], "properties": {"latex": "string", "label": "string", "description": "string"}},
        "MethodologyCard": {"required": ["steps"], "properties": {"title": "string", "steps": "array", "participants": "string", "tools": "array"}},
        "CalloutBox": {"required": ["content"], "properties": {"type": "string", "title": "string", "content": "string"}},
        "AbstractCard": {"required": ["text"], "properties": {"text": "string"}},
    }

    ALLOWED_UI_OPS = {
        "insert_component",
        "update_component_props",
        "remove_component",
        "reorder_components",
    }

    @classmethod
    def component_whitelist(cls) -> List[str]:
        return sorted(list(cls.COMPONENT_SCHEMAS.keys()))

    @classmethod
    def component_schema_manifest(cls) -> Dict[str, Any]:
        return {
            "component_registry_version": "reader_components_v2",
            "components": {
                name: {
                    "required": list(schema.get("required") or []),
                    "properties": dict(schema.get("properties") or {}),
                }
                for name, schema in cls.COMPONENT_SCHEMAS.items()
            },
            "ui_ops": sorted(list(cls.ALLOWED_UI_OPS)),
        }

    @staticmethod
    def _matches_type(value: Any, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "number":
            return isinstance(value, (int, float))
        if expected == "string_or_number":
            if isinstance(value, bool):
                return False
            return isinstance(value, (str, int, float))
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, dict)
        return True

    @classmethod
    def validate_component(
        cls,
        component: Dict[str, Any],
        *,
        valid_block_ids: Optional[Set[str]] = None,
    ) -> Tuple[bool, Optional[str]]:
        if not isinstance(component, dict):
            return False, "component_not_object"
        component_type = str(component.get("type") or "").strip()
        if component_type not in cls.COMPONENT_SCHEMAS:
            return False, "component_type_not_allowed"
        props = component.get("props")
        if not isinstance(props, dict):
            return False, "component_props_not_object"
        schema = cls.COMPONENT_SCHEMAS.get(component_type) or {}
        required = [str(item).strip() for item in list(schema.get("required") or []) if str(item).strip()]
        properties = dict(schema.get("properties") or {})
        for key in required:
            if key not in props:
                return False, f"component_missing_required_prop:{key}"
        for key, expected in properties.items():
            if key not in props:
                continue
            if not cls._matches_type(props.get(key), str(expected)):
                return False, f"component_prop_type_invalid:{key}:{expected}"
        source_block_ids = [str(item).strip() for item in list(component.get("source_block_ids") or []) if str(item).strip()]
        if valid_block_ids is not None and source_block_ids:
            if any(item not in valid_block_ids for item in source_block_ids):
                return False, "component_source_block_ids_invalid"
        return True, None

    @classmethod
    def validate_and_sanitize_ui_ops(
        cls,
        ui_ops: Sequence[Dict[str, Any]],
        *,
        existing_component_ids: Sequence[str],
        valid_block_ids: Optional[Set[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        existing_ids = {str(item).strip() for item in list(existing_component_ids or []) if str(item).strip()}
        sanitized: List[Dict[str, Any]] = []
        errors: List[str] = []

        for row in list(ui_ops or []):
            if not isinstance(row, dict):
                errors.append("ui_op_not_object")
                continue
            op = str(row.get("op") or "").strip()
            if op not in cls.ALLOWED_UI_OPS:
                errors.append(f"ui_op_not_allowed:{op}")
                continue

            if op == "reorder_components":
                ordered_ids = [
                    str(item).strip()
                    for item in list(row.get("ordered_component_ids") or [])
                    if str(item).strip()
                ]
                if not ordered_ids:
                    errors.append("reorder_empty_order")
                    continue
                if len(set(ordered_ids)) != len(ordered_ids):
                    errors.append("reorder_duplicate_ids")
                    continue
                if any(item not in existing_ids for item in ordered_ids):
                    errors.append("reorder_unknown_component_id")
                    continue
                sanitized.append(
                    {
                        "op": "reorder_components",
                        "ordered_component_ids": ordered_ids,
                        "reason": str(row.get("reason") or ""),
                    }
                )
                continue

            if op == "remove_component":
                component_id = str(row.get("component_id") or "").strip()
                if not component_id or component_id not in existing_ids:
                    errors.append("remove_unknown_component_id")
                    continue
                sanitized.append(
                    {
                        "op": "remove_component",
                        "component_id": component_id,
                        "reason": str(row.get("reason") or ""),
                    }
                )
                continue

            if op == "update_component_props":
                component_id = str(row.get("component_id") or "").strip()
                props_patch = row.get("props_patch")
                if not component_id or component_id not in existing_ids:
                    errors.append("update_unknown_component_id")
                    continue
                if not isinstance(props_patch, dict):
                    errors.append("update_props_patch_not_object")
                    continue
                sanitized.append(
                    {
                        "op": "update_component_props",
                        "component_id": component_id,
                        "props_patch": json.loads(json.dumps(props_patch, ensure_ascii=False)),
                        "reason": str(row.get("reason") or ""),
                    }
                )
                continue

            # insert_component
            component = row.get("component")
            if not isinstance(component, dict):
                errors.append("insert_component_missing_component")
                continue
            ok, reason = cls.validate_component(component, valid_block_ids=valid_block_ids)
            if not ok:
                errors.append(str(reason or "insert_component_invalid"))
                continue
            after_component_id = str(row.get("after_component_id") or "").strip()
            if after_component_id and after_component_id not in existing_ids:
                errors.append("insert_after_unknown_component_id")
                continue
            sanitized.append(
                {
                    "op": "insert_component",
                    "after_component_id": after_component_id or None,
                    "component": json.loads(json.dumps(component, ensure_ascii=False)),
                    "reason": str(row.get("reason") or ""),
                }
            )

        return sanitized, errors


def get_reader_component_contract_service() -> ReaderComponentContractService:
    return ReaderComponentContractService()

