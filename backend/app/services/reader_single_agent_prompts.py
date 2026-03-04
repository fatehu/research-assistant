from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, MutableMapping

PIPELINE_VERSION = "single_agent_v2"
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_REPAIR_ROUNDS = 2

SYSTEM_PROMPT_A = (
    "You are ReaderAgent-Orchestrator.\n"
    "You must produce structured JSON only.\n"
    "Validator is authoritative for pass/fail.\n"
    "Never invent IDs. Never drop IDs.\n"
    "Use specialized components for research papers: AbstractCard for abstract, MethodologyCard for methods/experiment setup, EquationBlock for LaTeX math, CitationCard for detailed source metadata, and CalloutBox for important highlights."
)

SYSTEM_PROMPT_B = (
    "You are in repair mode.\n"
    "Fix only validator-reported failures.\n"
    "Preserve IDs and ownership unless validator explicitly requires change.\n"
    "JSON only."
)


def _safe_json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {}


def _safe_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return copy.deepcopy(value)
    return []


def build_first_turn_prompt(
    *,
    page_meta: Mapping[str, Any],
    docmind_blocks: List[Mapping[str, Any]],
    rendered_page_image: str,
    component_whitelist: List[str],
    max_steps: int = DEFAULT_MAX_STEPS,
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_config": {
            "pipeline_version": PIPELINE_VERSION,
            "step": 1,
            "max_steps": int(max_steps),
            "max_repair_rounds": int(max_repair_rounds),
        },
        "task": (
            "Classify all blocks into main_content/aux_content, clean text safely, "
            "and draft UI plan with explicit layout contract fields."
        ),
        "inputs": {
            "page_meta": _safe_json_obj(page_meta),
            "docmind_blocks": _safe_json_list(docmind_blocks),
            "rendered_page_image": str(rendered_page_image or ""),
            "component_whitelist": [str(item).strip() for item in list(component_whitelist or []) if str(item).strip()],
        },
        "rules": [
            "100% coverage across all input layout_id",
            "No invented/missing/duplicate layout_id",
            "Atomic paragraph: type=text & subType=para cannot be split",
            "source_text must remain unchanged",
            "Only whitelist components allowed",
            "Attach source_block_ids for every proposed component",
            "Every component must include zone_type, column_id, region, display, order_key (no omission)",
            "For contiguous prose statements, group consecutive source_block_ids into one semantic component when possible",
            "Do not split statement prose into one-line fragments unless it is structurally list/table/caption-like",
            "If you provide layout fields, they must be valid and consistent (layout_mode/regions/region/display/order_key/zone_type/column_id)",
        ],
        "classification_policy": {
            "aux_defaults_by_type": [
                "head",
                "header_line",
                "side",
                "footer_line",
                "foot",
                "foot_pagenum",
                "split_line",
            ],
            "aux_keywords": [
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
            ],
            "uncertain_policy": "prefer aux_content with lower confidence",
        },
        "required_output_schema": {
            "status": "continue|done|fallback",
            "step_result": {
                "classification": {
                    "items": [
                        {
                            "layout_id": "string",
                            "bucket": "main_content|aux_content",
                            "role": "title|section_heading|paragraph|metadata|header|footer|reference|noise|unknown",
                            "confidence": 0.0,
                            "reason": "string",
                        }
                    ]
                },
                "cleaning": {
                    "items": [
                        {
                            "layout_id": "string",
                            "source_text": "string",
                            "normalized_text": "string",
                            "clean_ops": ["string"],
                            "clean_confidence": 0.0,
                            "needs_review": False,
                        }
                    ]
                },
                "ui_plan_draft": {
                    "components": [
                        {
                            "component": "string",
                            "source_block_ids": ["layout_id"],
                            "props": {},
                            "zone_type": "main_body|side_context|figure_meta",
                            "column_id": "string",
                            "region": "string",
                            "display": "default|collapsed|pinned|hidden_until_expand",
                            "order_key": 0.0,
                        }
                    ],
                    "layout_tokens": {
                        "layout_mode": "single_column|split|drawer|section_inline",
                        "regions": [
                            {
                                "id": "string",
                                "kind": "string",
                                "collapsed_by_default": False,
                            }
                        ],
                    },
                },
            },
            "self_check": {
                "duplicate_layout_ids": ["string"],
                "invented_layout_ids": ["string"],
            },
        },
    }

    return {
        "system_prompt": SYSTEM_PROMPT_A,
        "user_prompt": payload,
    }


def build_iterative_turn_prompt(
    *,
    current_step: int,
    remaining_repair_rounds: int,
    previous_step_result_digest: Mapping[str, Any],
    validator_result: Mapping[str, Any],
    must_fix: List[str],
    do_not_change: List[str],
    component_whitelist: List[str],
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_config": {
            "pipeline_version": PIPELINE_VERSION,
            "step": int(current_step),
            "max_steps": int(max_steps),
            "remaining_repair_rounds": int(remaining_repair_rounds),
        },
        "task": "Apply minimal fixes to pass all hard gates.",
        "inputs": {
            "previous_step_result_digest": _safe_json_obj(previous_step_result_digest),
            "validator_result": _safe_json_obj(validator_result),
            "must_fix": [str(item).strip() for item in list(must_fix or []) if str(item).strip()],
            "do_not_change": [str(item).strip() for item in list(do_not_change or []) if str(item).strip()],
            "component_whitelist": [str(item).strip() for item in list(component_whitelist or []) if str(item).strip()],
        },
        "rules": [
            "Do not rework passed sections",
            "No ID invention/deletion",
            "Preserve atomic paragraph ownership",
            "Keep source_text immutable",
            "Do not invent layout tokens or region references",
            "Output components with mandatory layout fields: zone_type, column_id, region, display, order_key",
            "Keep contiguous prose as semantic groups instead of one-line fragments when structure allows",
            "Output full updated step_result JSON",
        ],
        "required_output_schema": {
            "status": "continue|done|fallback",
            "fixes_applied": [
                {
                    "gate": "string",
                    "action": "string",
                    "affected_layout_ids": ["string"],
                }
            ],
            "step_result": {
                "classification": {"items": []},
                "cleaning": {"items": []},
                "ui_plan_draft": {
                    "components": [
                        {
                            "component": "string",
                            "source_block_ids": ["layout_id"],
                            "props": {},
                            "zone_type": "main_body|side_context|figure_meta",
                            "column_id": "string",
                            "region": "string",
                            "display": "default|collapsed|pinned|hidden_until_expand",
                            "order_key": 0.0,
                        }
                    ],
                    "layout_tokens": {
                        "layout_mode": "single_column|split|drawer|section_inline",
                        "regions": [
                            {
                                "id": "string",
                                "kind": "string",
                                "collapsed_by_default": False,
                            }
                        ],
                    },
                },
            },
            "self_check": {
                "coverage_ratio": 0.0,
                "missing_layout_ids": ["string"],
                "duplicate_layout_ids": ["string"],
                "invented_layout_ids": ["string"],
            },
        },
    }

    return {
        "system_prompt": SYSTEM_PROMPT_B,
        "user_prompt": payload,
    }


def build_step_result_digest(step_result: Mapping[str, Any]) -> Dict[str, Any]:
    result = _safe_json_obj(step_result)
    classification = [
        row
        for row in list((result.get("classification") or {}).get("items") or [])
        if isinstance(row, dict)
    ]
    cleaning = [
        row
        for row in list((result.get("cleaning") or {}).get("items") or [])
        if isinstance(row, dict)
    ]
    components = [
        row
        for row in list((result.get("ui_plan_draft") or {}).get("components") or [])
        if isinstance(row, dict)
    ]

    digest: Dict[str, Any] = {
        "classification": [
            {
                "layout_id": str(row.get("layout_id") or ""),
                "bucket": str(row.get("bucket") or ""),
                "role": str(row.get("role") or ""),
                "confidence": float(row.get("confidence") or 0.0),
            }
            for row in classification
            if str(row.get("layout_id") or "")
        ],
        "cleaning": [
            {
                "layout_id": str(row.get("layout_id") or ""),
                "clean_ops": [str(item).strip() for item in list(row.get("clean_ops") or []) if str(item).strip()],
                "clean_confidence": float(row.get("clean_confidence") or 0.0),
                "needs_review": bool(row.get("needs_review")),
            }
            for row in cleaning
            if str(row.get("layout_id") or "")
        ],
        "ui_plan_draft": {
            "component_count": len(components),
            "components": [
                {
                    "component": str(row.get("component") or ""),
                    "source_block_ids": [
                        str(item).strip()
                        for item in list(row.get("source_block_ids") or [])
                        if str(item).strip()
                    ],
                    "zone_type": str(row.get("zone_type") or ""),
                    "column_id": str(row.get("column_id") or ""),
                    "region": str(row.get("region") or ""),
                    "display": str(row.get("display") or ""),
                    "order_key": row.get("order_key"),
                }
                for row in components
                if str(row.get("component") or "")
            ],
            "layout_tokens": _safe_json_obj((result.get("ui_plan_draft") or {}).get("layout_tokens")),
        },
    }
    return digest

