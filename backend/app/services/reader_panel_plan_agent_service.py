from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings
from app.services.dashscope_multimodal_service import DashScopeMultimodalService

GENERIC_TITLES = {
    "panel design preview",
    "article content",
    "publication header",
    "untitled panel plan",
}


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _iter_nodes(nodes: Any):
    if not isinstance(nodes, list):
        return
    for row in nodes:
        if not isinstance(row, dict):
            continue
        yield row
        children = row.get("children")
        if isinstance(children, list):
            for child in _iter_nodes(children):
                yield child


def _extract_json_dict(raw_text: str) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        pass
    if "```" in text:
        for chunk in text.split("```"):
            piece = chunk.strip()
            if piece.startswith("json"):
                piece = piece[4:].strip()
            try:
                parsed = json.loads(piece)
                if isinstance(parsed, dict):
                    return dict(parsed)
            except Exception:
                continue
    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right > left:
        try:
            parsed = json.loads(text[left : right + 1])
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


class ReaderPanelPlanAgentService:
    def __init__(self) -> None:
        self._component_catalog: List[Dict[str, Any]] = [
            {"name": "SectionHeading", "props": {"text": "string", "level": "number"}},
            {"name": "ParagraphProse", "props": {"text": "string", "paragraphs": "object[] (optional, [{text:string}])"}},
            {"name": "ListBlock", "props": {"items": "string[]"}},
            {"name": "FigurePanel", "props": {"caption": "string", "image_url": "string"}},
            {"name": "TablePanel", "props": {"title": "string", "rows": "object[]"}},
            {"name": "ContextRail", "props": {"title": "string", "items": "object[]"}},
            {"name": "CalloutBox", "props": {"type": "string", "title": "string", "content": "string"}},
            {"name": "KeyTakeaways", "props": {"items": "object[]"}},
            {"name": "AbstractCard", "props": {"text": "string"}},
            {"name": "CitationCard", "props": {"title": "string", "authors": "string[]"}},
            {"name": "CompareInsightsCard", "props": {"items": "object[]"}},
            {"name": "InsightClusterCard", "props": {"title": "string", "items": "string[]", "tone": "string"}},
            {"name": "SectionBridgeCard", "props": {"title": "string", "text": "string"}},
            {"name": "EquationBlock", "props": {"latex": "string"}},
            {"name": "MethodologyCard", "props": {"steps": "string[]"}},
        ]

    @staticmethod
    def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        def _normalize_paragraph_props(raw_props: Any) -> Dict[str, Any]:
            props = dict(raw_props) if isinstance(raw_props, dict) else {}
            text = str(props.get("text") or "").strip()
            paragraphs: List[Dict[str, str]] = []
            raw_paragraphs = props.get("paragraphs")
            if isinstance(raw_paragraphs, list):
                for item in raw_paragraphs:
                    if isinstance(item, dict):
                        seg = str(item.get("text") or "").strip()
                    else:
                        seg = str(item or "").strip()
                    if seg:
                        paragraphs.append({"text": seg})
            if text:
                props["text"] = text
            else:
                props["text"] = ""
            if paragraphs:
                props["paragraphs"] = paragraphs
            elif "paragraphs" in props:
                props.pop("paragraphs", None)
            return props

        def _normalize_node(node: Dict[str, Any], idx: int) -> Dict[str, Any]:
            out = dict(node)
            if not str(out.get("node_id") or "").strip():
                out["node_id"] = f"node_{idx}"
            component = str(out.get("component") or "").strip()
            out["component"] = component
            if component == "ParagraphProse":
                out["props"] = _normalize_paragraph_props(out.get("props"))
            else:
                out["props"] = dict(out.get("props") or {}) if isinstance(out.get("props"), dict) else {}
            out["source_layout_ids"] = [
                str(item).strip()
                for item in list(out.get("source_layout_ids") or [])
                if str(item).strip()
            ]
            children = out.get("children")
            if isinstance(children, list):
                out["children"] = [
                    _normalize_node(dict(child), child_idx + 1)
                    for child_idx, child in enumerate(children)
                    if isinstance(child, dict)
                ]
            else:
                out["children"] = []
            return out

        out = copy.deepcopy(plan if isinstance(plan, dict) else {})
        out.setdefault("schema_version", "panel_plan_v2")
        out.setdefault("creative_direction", "")
        out.setdefault("style_plan", {})
        out.setdefault("decision_log", [])
        out.setdefault("coverage", {"omitted_layout_ids": [], "omitted_reason": ""})
        raw_panels = out.get("panels")
        panels = raw_panels if isinstance(raw_panels, list) else []
        normalized: List[Dict[str, Any]] = []
        for idx, panel in enumerate(panels, start=1):
            if not isinstance(panel, dict):
                continue
            row = dict(panel)
            row.setdefault("panel_id", f"panel_{idx}")
            title = str(row.get("title") or "").strip().lower()
            if title in GENERIC_TITLES:
                row["title"] = ""
            layout = row.get("layout")
            row["layout"] = dict(layout) if isinstance(layout, dict) else {"type": "stack", "gap": 12}
            nodes = row.get("nodes")
            row["nodes"] = (
                [
                    _normalize_node(dict(item), item_idx + 1)
                    for item_idx, item in enumerate(nodes)
                    if isinstance(item, dict)
                ]
                if isinstance(nodes, list)
                else []
            )
            normalized.append(row)
        if not normalized:
            normalized = [{"panel_id": "panel_main", "title": "", "layout": {"type": "stack", "gap": 12}, "nodes": []}]
        out["panels"] = normalized
        return out

    @staticmethod
    def _fallback_plan(docmind_blocks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(docmind_blocks or []), start=1):
            layout_id = str(row.get("layout_id") or "").strip()
            text = str(row.get("source_text") or "").strip()
            if not layout_id:
                continue
            nodes.append(
                {
                    "node_id": f"fallback_n{idx}",
                    "component": "ParagraphProse",
                    "props": {"text": text},
                    "source_layout_ids": [layout_id],
                }
            )
        return {
            "schema_version": "panel_plan_v2",
            "creative_direction": "deterministic_fallback",
            "style_plan": {},
            "decision_log": [],
            "coverage": {"omitted_layout_ids": [], "omitted_reason": ""},
            "panels": [{"panel_id": "panel_main", "title": "", "layout": {"type": "stack", "gap": 12}, "nodes": nodes}],
        }

    @staticmethod
    def _propose_panel_plan_schema() -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "panel_plan": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "schema_version": {"type": "string"},
                        "creative_direction": {"type": "string"},
                        "style_plan": {"type": "object", "additionalProperties": True},
                        "decision_log": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "coverage": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "omitted_layout_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "omitted_reason": {"type": "string"},
                            },
                            "required": ["omitted_layout_ids", "omitted_reason"],
                        },
                        "panels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                                "properties": {
                                    "panel_id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "layout": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "properties": {
                                            "type": {"type": "string"},
                                            "gap": {"type": ["number", "integer"]},
                                            "columns": {"type": ["number", "integer"]},
                                            "variant": {"type": "string"},
                                        },
                                        "required": ["type"],
                                    },
                                    "nodes": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "properties": {
                                                "node_id": {"type": "string"},
                                                "component": {"type": "string"},
                                                "props": {"type": "object", "additionalProperties": True},
                                                "source_layout_ids": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                                "uniqueid": {"type": "string"},
                                                "style_patch": {"type": "object", "additionalProperties": True},
                                            },
                                            "required": ["node_id", "component", "props", "source_layout_ids"],
                                        },
                                    },
                                },
                                "required": ["panel_id", "title", "layout", "nodes"],
                            },
                        },
                    },
                    "required": [
                        "schema_version",
                        "creative_direction",
                        "style_plan",
                        "panels",
                        "decision_log",
                        "coverage",
                    ],
                }
            },
            "required": ["panel_plan"],
        }

    @staticmethod
    def _review_panel_plan_schema() -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {"type": "string", "enum": ["done", "revise"]},
                "rationale": {"type": "string"},
                "panel_plan_patch": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "panels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                                "properties": {
                                    "panel_id": {"type": "string"},
                                    "_delete": {"type": "boolean"},
                                    "title": {"type": "string"},
                                    "layout": {"type": "object", "additionalProperties": True},
                                    "nodes": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "properties": {
                                                "node_id": {"type": "string"},
                                                "_delete": {"type": "boolean"},
                                                "component": {"type": "string"},
                                                "props": {"type": "object", "additionalProperties": True},
                                                "source_layout_ids": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                                "uniqueid": {"type": "string"},
                                                "style_patch": {"type": "object", "additionalProperties": True},
                                            },
                                            "required": ["node_id"],
                                        },
                                    },
                                },
                                "required": ["panel_id"],
                            },
                        },
                        "panel_order": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "decision_log_append": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["decision", "rationale"],
        }

    @staticmethod
    def _validate_plan(
        *,
        panel_plan: Dict[str, Any],
        known_layout_ids: Sequence[str],
        component_whitelist: Sequence[str],
    ) -> Dict[str, Any]:
        known = {str(item).strip() for item in list(known_layout_ids or []) if str(item).strip()}
        whitelist = {str(item).strip() for item in list(component_whitelist or []) if str(item).strip()}
        errors: List[str] = []
        warnings: List[str] = []
        used: set[str] = set()
        panels = panel_plan.get("panels")
        if not isinstance(panels, list) or not panels:
            return {"passed": False, "status": "invalid", "errors": ["panels_empty"], "warnings": [], "stats": {"known_ids": len(known), "used_ids": 0}}

        for panel in panels:
            if not isinstance(panel, dict):
                continue
            nodes = panel.get("nodes")
            if not isinstance(nodes, list):
                continue
            for node in _iter_nodes(nodes):
                comp = str(node.get("component") or "").strip()
                if whitelist and comp and comp not in whitelist:
                    errors.append(f"component_not_allowed:{comp}")
                src_ids = node.get("source_layout_ids")
                if not isinstance(src_ids, list):
                    errors.append("source_layout_ids_not_array")
                    continue
                clean_src = [str(item).strip() for item in src_ids if str(item).strip()]
                if not clean_src:
                    warnings.append("node_without_source_layout_ids")
                for src in clean_src:
                    used.add(src)
                    if known and src not in known:
                        errors.append(f"unknown_source_layout_id:{src}")

        coverage = panel_plan.get("coverage") if isinstance(panel_plan.get("coverage"), dict) else {}
        omitted_raw = coverage.get("omitted_layout_ids")
        omitted = {str(item).strip() for item in list(omitted_raw or []) if str(item).strip()} if isinstance(omitted_raw, list) else set()
        missing = sorted([item for item in known if item not in used and item not in omitted])
        if missing:
            errors.append(f"uncovered_layout_ids:{','.join(missing[:120])}")
        return {
            "passed": len(errors) == 0,
            "status": "ok" if len(errors) == 0 and len(warnings) == 0 else ("warn" if len(errors) == 0 else "invalid"),
            "errors": errors,
            "warnings": warnings,
            "stats": {"known_ids": len(known), "used_ids": len(used), "missing_ids": len(missing)},
        }

    @staticmethod
    def _deep_merge_dict(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base or {})
        for key, value in dict(patch or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ReaderPanelPlanAgentService._deep_merge_dict(
                    dict(merged.get(key) or {}),
                    dict(value),
                )
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    @staticmethod
    def _merge_nodes(
        existing_nodes: Sequence[Mapping[str, Any]],
        patch_nodes: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        order: List[str] = []
        node_map: Dict[str, Dict[str, Any]] = {}
        for idx, raw in enumerate(list(existing_nodes or []), start=1):
            if not isinstance(raw, Mapping):
                continue
            node = dict(raw)
            node_id = str(node.get("node_id") or f"node_{idx}").strip() or f"node_{idx}"
            node["node_id"] = node_id
            if node_id not in node_map:
                order.append(node_id)
            node_map[node_id] = node

        for idx, raw_patch in enumerate(list(patch_nodes or []), start=1):
            if not isinstance(raw_patch, Mapping):
                continue
            patch = dict(raw_patch)
            node_id = str(patch.get("node_id") or "").strip() or f"patch_node_{idx}"
            should_delete = bool(patch.get("_delete"))
            if should_delete:
                if node_id in node_map:
                    node_map.pop(node_id, None)
                    order = [item for item in order if item != node_id]
                continue

            if node_id in node_map:
                merged_node = dict(node_map[node_id])
                for key, value in patch.items():
                    if key in {"_delete"}:
                        continue
                    if key == "children" and isinstance(value, list):
                        merged_node["children"] = ReaderPanelPlanAgentService._merge_nodes(
                            existing_nodes=list(merged_node.get("children") or []),
                            patch_nodes=[item for item in value if isinstance(item, Mapping)],
                        )
                        continue
                    if key == "props" and isinstance(value, dict):
                        merged_node["props"] = ReaderPanelPlanAgentService._deep_merge_dict(
                            dict(merged_node.get("props") or {}),
                            dict(value),
                        )
                        continue
                    if key == "source_layout_ids" and isinstance(value, list):
                        merged_node["source_layout_ids"] = [
                            str(item).strip()
                            for item in list(value)
                            if str(item).strip()
                        ]
                        continue
                    merged_node[key] = copy.deepcopy(value)
                merged_node["node_id"] = node_id
                node_map[node_id] = merged_node
                continue

            new_node = {k: copy.deepcopy(v) for k, v in patch.items() if k != "_delete"}
            new_node["node_id"] = str(new_node.get("node_id") or node_id).strip() or node_id
            if isinstance(new_node.get("children"), list):
                new_node["children"] = ReaderPanelPlanAgentService._merge_nodes(
                    existing_nodes=[],
                    patch_nodes=[item for item in list(new_node.get("children") or []) if isinstance(item, Mapping)],
                )
            if new_node["node_id"] not in order:
                order.append(new_node["node_id"])
            node_map[new_node["node_id"]] = new_node

        return [dict(node_map[node_id]) for node_id in order if node_id in node_map]

    @staticmethod
    def _merge_panels(
        existing_panels: Sequence[Mapping[str, Any]],
        patch_panels: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        order: List[str] = []
        panel_map: Dict[str, Dict[str, Any]] = {}
        for idx, raw in enumerate(list(existing_panels or []), start=1):
            if not isinstance(raw, Mapping):
                continue
            panel = dict(raw)
            panel_id = str(panel.get("panel_id") or f"panel_{idx}").strip() or f"panel_{idx}"
            panel["panel_id"] = panel_id
            if panel_id not in panel_map:
                order.append(panel_id)
            panel_map[panel_id] = panel

        for idx, raw_patch in enumerate(list(patch_panels or []), start=1):
            if not isinstance(raw_patch, Mapping):
                continue
            patch = dict(raw_patch)
            panel_id = str(patch.get("panel_id") or "").strip() or f"panel_patch_{idx}"
            should_delete = bool(patch.get("_delete"))
            if should_delete:
                if panel_id in panel_map:
                    panel_map.pop(panel_id, None)
                    order = [item for item in order if item != panel_id]
                continue

            if panel_id in panel_map:
                merged_panel = dict(panel_map[panel_id])
                for key, value in patch.items():
                    if key in {"_delete"}:
                        continue
                    if key == "nodes" and isinstance(value, list):
                        merged_panel["nodes"] = ReaderPanelPlanAgentService._merge_nodes(
                            existing_nodes=[item for item in list(merged_panel.get("nodes") or []) if isinstance(item, Mapping)],
                            patch_nodes=[item for item in value if isinstance(item, Mapping)],
                        )
                        continue
                    if key == "layout" and isinstance(value, dict):
                        merged_panel["layout"] = ReaderPanelPlanAgentService._deep_merge_dict(
                            dict(merged_panel.get("layout") or {}),
                            dict(value),
                        )
                        continue
                    merged_panel[key] = copy.deepcopy(value)
                merged_panel["panel_id"] = panel_id
                panel_map[panel_id] = merged_panel
                continue

            new_panel = {k: copy.deepcopy(v) for k, v in patch.items() if k != "_delete"}
            new_panel["panel_id"] = str(new_panel.get("panel_id") or panel_id).strip() or panel_id
            if new_panel["panel_id"] not in order:
                order.append(new_panel["panel_id"])
            panel_map[new_panel["panel_id"]] = new_panel

        return [dict(panel_map[panel_id]) for panel_id in order if panel_id in panel_map]

    @staticmethod
    def _merge_panel_plan(base_plan: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base_plan or {})
        patch_obj = dict(patch or {})
        for key, value in patch_obj.items():
            if key in {"decision_log_append", "panel_order"}:
                continue
            if key == "panels" and isinstance(value, list):
                merged["panels"] = ReaderPanelPlanAgentService._merge_panels(
                    existing_panels=[item for item in list(merged.get("panels") or []) if isinstance(item, Mapping)],
                    patch_panels=[item for item in value if isinstance(item, Mapping)],
                )
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ReaderPanelPlanAgentService._deep_merge_dict(
                    dict(merged.get(key) or {}),
                    dict(value),
                )
                continue
            merged[key] = copy.deepcopy(value)

        append_logs = [
            str(item).strip()
            for item in list(patch_obj.get("decision_log_append") or [])
            if str(item).strip()
        ]
        if append_logs:
            existing_logs = [str(item).strip() for item in list(merged.get("decision_log") or []) if str(item).strip()]
            merged["decision_log"] = existing_logs + append_logs

        panel_order = [
            str(item).strip()
            for item in list(patch_obj.get("panel_order") or [])
            if str(item).strip()
        ]
        if panel_order and isinstance(merged.get("panels"), list):
            panel_map = {
                str((panel or {}).get("panel_id") or "").strip(): dict(panel)
                for panel in list(merged.get("panels") or [])
                if isinstance(panel, Mapping) and str((panel or {}).get("panel_id") or "").strip()
            }
            reordered: List[Dict[str, Any]] = []
            used: set[str] = set()
            for panel_id in panel_order:
                if panel_id in panel_map and panel_id not in used:
                    reordered.append(dict(panel_map[panel_id]))
                    used.add(panel_id)
            for panel in list(merged.get("panels") or []):
                panel_id = str((panel or {}).get("panel_id") or "").strip()
                if panel_id and panel_id not in used:
                    reordered.append(dict(panel))
            merged["panels"] = reordered
        return merged

    @staticmethod
    def _render_panel_plan_preview(panel_plan: Dict[str, Any], max_chars: int = 6000) -> str:
        panels = [row for row in list((panel_plan or {}).get("panels") or []) if isinstance(row, dict)]
        rows: List[str] = []
        rows.append("# UI Preview")
        for pidx, panel in enumerate(panels, start=1):
            panel_id = str(panel.get("panel_id") or f"panel_{pidx}").strip() or f"panel_{pidx}"
            title = str(panel.get("title") or "").strip()
            layout = dict(panel.get("layout") or {})
            rows.append(f"## Panel {pidx}: {panel_id}")
            if title:
                rows.append(f"title: {title}")
            if layout:
                rows.append(f"layout: {_compact_json(layout)}")
            nodes = [item for item in list(panel.get("nodes") or []) if isinstance(item, dict)]
            for nidx, node in enumerate(nodes, start=1):
                node_id = str(node.get("node_id") or f"{panel_id}_n{nidx}").strip() or f"{panel_id}_n{nidx}"
                component = str(node.get("component") or "").strip()
                src_ids = [str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()]
                props = dict(node.get("props") or {})
                text_preview = ""
                if component == "ParagraphProse":
                    paras = [str((item or {}).get("text") or "").strip() for item in list(props.get("paragraphs") or []) if isinstance(item, dict)]
                    if paras:
                        text_preview = " / ".join(paras[:2])
                    else:
                        text_preview = str(props.get("text") or "").strip()
                elif component == "SectionHeading":
                    text_preview = str(props.get("text") or "").strip()
                elif component == "FigurePanel":
                    text_preview = str(props.get("caption") or "").strip()
                else:
                    for key in ("text", "title", "content", "caption"):
                        val = str(props.get(key) or "").strip()
                        if val:
                            text_preview = val
                            break
                rows.append(
                    f"- node {nidx}: id={node_id} comp={component} src={_compact_json(src_ids)}"
                    + (f" preview={text_preview[:180]}" if text_preview else "")
                )
        preview = "\n".join(rows).strip()
        if len(preview) > max_chars:
            preview = f"{preview[:max_chars]}\n...[truncated]"
        return preview

    async def _call_tool(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_name: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
    ) -> tuple[Dict[str, Any], Dict[str, int]]:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        request = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
            "temperature": float(temperature),
            "max_tokens": max(512, int(max_tokens)),
            "timeout": float(timeout_seconds),
        }
        try:
            response = await client.chat.completions.create(
                **request,
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:
            message = str(exc).lower()
            disable_thinking_unsupported = (
                "enable_thinking" in message
                or "cannot unmarshal" in message
                or "invalid_request_error" in message
            )
            if not disable_thinking_unsupported:
                raise
            response = await client.chat.completions.create(**request)
        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
        }
        msg = response.choices[0].message
        for tool_call in list(getattr(msg, "tool_calls", None) or []):
            fn = getattr(tool_call, "function", None)
            if fn and str(getattr(fn, "name", "") or "") == tool_name:
                parsed = _extract_json_dict(str(getattr(fn, "arguments", "") or ""))
                if parsed:
                    return parsed, usage
        parsed = _extract_json_dict(str(getattr(msg, "content", "") or ""))
        return parsed, usage

    async def _call_dashscope_json(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[str],
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
    ) -> tuple[Dict[str, Any], Dict[str, int]]:
        del timeout_seconds
        result = await DashScopeMultimodalService.chat_json(
            api_key=api_key,
            base_url=str(getattr(settings, "aliyun_dashscope_api_base", "") or base_url or "").strip(),
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=image_paths,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return dict(result.get("parsed") or {}), dict(result.get("usage") or {})

    async def run(
        self,
        *,
        docmind_blocks: Sequence[Mapping[str, Any]],
        rendered_page_image: str,
        rendered_page_image_path: str = "",
        component_whitelist: Sequence[str],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: Optional[str],
        max_rounds: int,
        phase1_context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        known_ids = [str(row.get("layout_id") or "").strip() for row in list(docmind_blocks or []) if isinstance(row, Mapping) and str(row.get("layout_id") or "").strip()]
        known_ids = list(dict.fromkeys(known_ids))
        whitelist = [str(item).strip() for item in list(component_whitelist or []) if str(item).strip()]
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        model = str(getattr(settings, "reader_agent_model", "qwen3.5-flash") or "qwen3.5-flash").strip()
        timeout_seconds = max(8.0, float(int(getattr(settings, "reader_agent_timeout_ms", 90000) or 90000) / 1000.0))
        max_tokens = max(1024, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000))
        style_goal = f"{str(style_intent or '').strip() or 'editorial'}; theme={str(theme_mode or 'light').strip() or 'light'}; detail={str(detail_level or 'standard').strip() or 'standard'}"
        started = time.perf_counter()
        compact_context = dict(phase1_context or {})
        prompt_docmind_blocks = [
            dict(row)
            for row in list(compact_context.get("docmind_blocks_compact") or [])
            if isinstance(row, Mapping)
        ] or [dict(row) for row in list(docmind_blocks or []) if isinstance(row, Mapping)]
        scheme_catalog = [
            dict(row)
            for row in list(compact_context.get("scheme_catalog") or [])
            if isinstance(row, Mapping)
        ]
        token_strategy = dict(compact_context.get("token_strategy") or {})
        local_image_paths = DashScopeMultimodalService.collect_local_file_uris(
            str(rendered_page_image_path or "").strip(),
            str((((compact_context.get("pdf_reference") or {}).get("page_image_path")) or "")).strip(),
            limit=1,
        )
        can_use_dashscope_local_image = (
            str(getattr(settings, "reader_agent_provider", "") or "").strip() == "aliyun"
            and bool(local_image_paths)
            and DashScopeMultimodalService.is_available()
        )

        if not api_key or not base_url or not model:
            fallback = self._normalize_plan(self._fallback_plan(docmind_blocks))
            report = self._validate_plan(panel_plan=fallback, known_layout_ids=known_ids, component_whitelist=whitelist)
            report = dict(report)
            report["passed"] = False
            report["status"] = "invalid"
            report["errors"] = ["model_unavailable"] + [str(item) for item in list(report.get("errors") or []) if str(item).strip()]
            return {"status": "fallback", "degraded_reason": "model_unavailable", "panel_plan": fallback, "validation_report": report, "repair_report": {"steps_executed": 0, "step_metrics": []}, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

        propose_prompt = (
            "Build panel_plan JSON for one PDF page.\n"
            "Return exactly one function call to propose_panel_plan.\n"
            "Round-1 objective: build one COMPLETE panel_plan using page image + docmind blocks.\n"
            "Hard rules:\n"
            "1) panel_plan.panels MUST be an array of panel objects, not an array of nodes.\n"
            "2) Each panel object MUST contain {panel_id,title,layout,nodes}.\n"
            "3) panels[] items must NEVER contain component/node_id/source_layout_ids at the top level.\n"
            "4) Every visual/content item MUST live inside panel.nodes[].\n"
            "5) Every node MUST contain {node_id,component,props,source_layout_ids}.\n"
            "6) source_layout_ids must use only existing layout_id values from docmind_blocks.\n"
            "7) Every known layout_id must appear exactly once in node.source_layout_ids or in coverage.omitted_layout_ids.\n"
            "8) Do not silently drop content. If omitted, explain in coverage.omitted_reason.\n"
            "9) Avoid placeholder titles such as Panel Design Preview, Article Content, Publication Header.\n"
            "10) Prefer flat nodes inside a panel. Do not use nested children unless absolutely necessary.\n"
            "11) Do not summarize, rename, or rewrite source text beyond minor whitespace cleanup, except for obvious OCR/layout noise or caption continuation cleanup clearly supported by the page image.\n"
            "12) Choose one scheme_id from scheme_catalog and store it in style_plan.scheme_id.\n"
            "13) Avoid long prose-only layouts when richer structure exists on the page.\n"
            "14) If 3+ prose-like blocks would appear in a row, prefer inserting one structure block such as InsightClusterCard, CalloutBox, MethodologyCard, CitationCard, CompareInsightsCard, or SectionBridgeCard.\n"
            "15) Figure + analysis usually works better as FigurePanel plus InsightClusterCard or CalloutBox.\n"
            "16) Methods/protocol/exam setup should prefer MethodologyCard; cross-page continuation or section handoff should prefer SectionBridgeCard.\n"
            "17) Keep the main reading stack focused on reading flow only: headings, body prose, figures, captions, equations, and a small number of structural cards. Metadata, DOI links, publication info, citation bundles, quality/debug panels, and other auxiliary AI assets should be routed to side context.\n"
            "Paragraph rule: for ParagraphProse, keep source_layout_ids ownership stable, and use props.paragraphs=[{text}] for paragraph breaks inside one node.\n"
            "Use the provided page image as evidence for segmentation, especially indentation, vertical gap, figure/caption split, and obvious paragraph breaks.\n"
            "You may clean obvious OCR/layout noise when it visibly conflicts with the page image, especially figure blocks polluted by chart labels or split caption continuation blocks.\n"
            "If you clean display text, do not change scientific meaning, do not invent missing facts, and keep all original source_layout_ids attached to the cleaned node.\n"
            "Never modify geometry or provenance ownership while cleaning; cleaning is display-text only.\n"
            "If image evidence is unclear, keep one paragraph instead of hallucinating.\n"
            "Bad shape example: {\"panels\":[{\"component\":\"ParagraphProse\",\"node_id\":\"n1\",\"source_layout_ids\":[\"id1\"]}]}\n"
            "Good shape example: {\"panels\":[{\"panel_id\":\"panel_main\",\"title\":\"\",\"layout\":{\"type\":\"stack\",\"gap\":12},\"nodes\":[{\"node_id\":\"n1\",\"component\":\"ParagraphProse\",\"props\":{\"text\":\"...\"},\"source_layout_ids\":[\"id1\"]}]}]}\n"
            "Schema: {schema_version,creative_direction,style_plan,panels,decision_log,coverage}\n"
            "Panel schema: {panel_id,title,layout,nodes[]}\n"
            "Node schema: {node_id,component,props,source_layout_ids,uniqueid?,style_patch?}\n"
            f"style_goal={style_goal}\n"
            f"scheme_catalog={_compact_json(scheme_catalog)}\n"
            f"token_strategy={_compact_json(token_strategy)}\n"
            f"component_whitelist={_compact_json(whitelist)}\n"
            f"component_catalog={_compact_json(self._component_catalog)}\n"
            f"docmind_blocks={_compact_json(prompt_docmind_blocks)}"
        )
        content: List[Dict[str, Any]] = [{"type": "text", "text": propose_prompt}]
        image_token = str(rendered_page_image or "").strip()
        if image_token and (image_token.startswith("http://") or image_token.startswith("https://")):
            content.append({"type": "image_url", "image_url": {"url": image_token}})
        tools = [{"type": "function", "function": {"name": "propose_panel_plan", "parameters": self._propose_panel_plan_schema()}}]

        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            if can_use_dashscope_local_image:
                propose_obj, usage = await self._call_dashscope_json(
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    system_prompt="You are a strict reader UI planner. Output JSON only. Produce a complete panel_plan, not a flat node list.",
                    user_prompt=(
                        f"{propose_prompt}\n"
                        "Return only one JSON object with this shape:\n"
                        f"{_compact_json(self._propose_panel_plan_schema())}"
                    ),
                    image_paths=local_image_paths,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            else:
                propose_obj, usage = await self._call_tool(model=model, base_url=base_url, api_key=api_key, messages=[{"role": "system", "content": "You are a strict reader UI planner. Follow the required function schema exactly and produce a complete panel_plan, not a flat node list."}, {"role": "user", "content": content}], tools=tools, tool_name="propose_panel_plan", temperature=0.3, max_tokens=max_tokens, timeout_seconds=timeout_seconds)
            for key in usage_total:
                usage_total[key] += int(usage.get(key) or 0)
            raw_plan = propose_obj.get("panel_plan") if isinstance(propose_obj, dict) else None
            panel_plan = self._normalize_plan(raw_plan if isinstance(raw_plan, dict) else propose_obj)
        except Exception as exc:
            logger.warning(f"[ReaderPanelPlanAgent] propose failed: {exc}")
            panel_plan = self._normalize_plan(self._fallback_plan(docmind_blocks))
            report = self._validate_plan(panel_plan=panel_plan, known_layout_ids=known_ids, component_whitelist=whitelist)
            report = dict(report)
            report["passed"] = False
            report["status"] = "invalid"
            report["errors"] = ["propose_failed"] + [str(item) for item in list(report.get("errors") or []) if str(item).strip()]
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return {
                "status": "fallback",
                "degraded_reason": "propose_failed",
                "panel_plan": panel_plan,
                "validation_report": report,
                "repair_report": {"steps_executed": 1, "elapsed_ms": elapsed_ms, "step_metrics": [{"round": 1, "validation_status": "error", "passed": False}]},
                "usage": usage_total,
            }
        report = self._validate_plan(panel_plan=panel_plan, known_layout_ids=known_ids, component_whitelist=whitelist)
        logger.info(
            "[ReaderPanelPlanAgent] round=1 phase=first validation_status={} passed={}",
            str(report.get("status") or ""),
            bool(report.get("passed")),
        )
        rounds: List[Dict[str, Any]] = [
            {
                "round": 1,
                "phase": "first",
                "decision": "propose",
                "validation_status": str(report.get("status") or ""),
                "passed": bool(report.get("passed")),
                "patch_applied": False,
            }
        ]

        max_iter = max(1, int(max_rounds))
        ai_done = max_iter <= 1
        for r in range(2, max_iter + 1):
            ui_preview = self._render_panel_plan_preview(panel_plan)
            review_prompt = (
                "Evaluate current panel_plan quality and decide if iteration is needed.\n"
                "Return decision=done if UI is good enough; return decision=revise with a minimal partial patch otherwise.\n"
                "Do not return full plan rewrite. Only changed fields in panel_plan_patch.\n"
                "Patch merge behavior:\n"
                "1) panel_plan_patch.panels[] merges by panel_id.\n"
                "2) nodes[] inside a panel merge by node_id.\n"
                "3) _delete=true removes target panel/node.\n"
                "4) decision_log_append[] appends notes.\n"
                "Rules: keep source_layout_ids ownership stable; do not invent IDs; do not replace panel objects with node objects; use props.paragraphs for ParagraphProse segmentation if needed; keep style_plan.scheme_id aligned with scheme_catalog.\n"
                "You may apply display-text cleanup only for obvious OCR/layout noise or split caption continuation confirmed by the page image; never change scientific meaning, geometry, or source ownership.\n"
                "Prefer revising when the current plan degenerates into a long ParagraphProse stack despite available figure/method/citation/transition structure.\n"
                "If 3+ ParagraphProse nodes appear consecutively, treat that as a quality problem unless the page is genuinely plain prose.\n"
                "Keep non-body AI assets out of the main reading stack whenever possible: DOI links, publication metadata, citation bundles, quality/debug/status panels, and support context belong in side context.\n"
                f"validation_report={_compact_json(report)}\n"
                f"scheme_catalog={_compact_json(scheme_catalog)}\n"
                f"ui_preview_markdown={ui_preview}\n"
                f"current_panel_plan={_compact_json(panel_plan)}"
            )
            review_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "review_panel_plan",
                        "parameters": self._review_panel_plan_schema(),
                    },
                }
            ]
            try:
                if can_use_dashscope_local_image:
                    review_obj, usage = await self._call_dashscope_json(
                        model=model,
                        base_url=base_url,
                        api_key=api_key,
                        system_prompt="You are a reader UI design reviewer. Output JSON only.",
                        user_prompt=(
                            f"{review_prompt}\n"
                            "Return only one JSON object with this shape:\n"
                            f"{_compact_json(self._review_panel_plan_schema())}"
                        ),
                        image_paths=local_image_paths,
                        temperature=0.25,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    review_obj, usage = await self._call_tool(
                        model=model,
                        base_url=base_url,
                        api_key=api_key,
                        messages=[
                            {"role": "system", "content": "You are a reader UI design reviewer."},
                            {"role": "user", "content": [{"type": "text", "text": review_prompt}]},
                        ],
                        tools=review_tools,
                        tool_name="review_panel_plan",
                        temperature=0.25,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    )
                for key in usage_total:
                    usage_total[key] += int(usage.get(key) or 0)
            except Exception as exc:
                logger.warning(
                    "[ReaderPanelPlanAgent] round={} phase=iterative_review call_failed error={}",
                    int(r),
                    str(exc),
                )
                rounds.append(
                    {
                        "round": int(r),
                        "phase": "iterative_review",
                        "decision": "error",
                        "validation_status": str(report.get("status") or ""),
                        "passed": bool(report.get("passed")),
                        "patch_applied": False,
                        "error": str(exc),
                    }
                )
                break

            if not isinstance(review_obj, dict) or not review_obj:
                logger.warning(
                    "[ReaderPanelPlanAgent] round={} phase=iterative_review empty_review_obj",
                    int(r),
                )
                review_obj = {}

            decision = str(review_obj.get("decision") or "").strip().lower()
            rationale = str(review_obj.get("rationale") or "").strip()
            if decision not in {"done", "revise"}:
                decision = "revise"

            patch = review_obj.get("panel_plan_patch")
            if not isinstance(patch, dict):
                patch = review_obj.get("patch")
            if not isinstance(patch, dict):
                patch = review_obj.get("partial_panel_plan")
            if not isinstance(patch, dict):
                patch = {}
            append_logs = [
                str(item).strip()
                for item in list(review_obj.get("decision_log_append") or [])
                if str(item).strip()
            ]
            if append_logs:
                patch = {
                    **dict(patch),
                    "decision_log_append": append_logs,
                }

            patch_applied = False
            if decision == "revise" and patch:
                panel_plan = self._normalize_plan(
                    self._merge_panel_plan(
                        base_plan=panel_plan,
                        patch=patch,
                    )
                )
                patch_applied = True
                report = self._validate_plan(panel_plan=panel_plan, known_layout_ids=known_ids, component_whitelist=whitelist)
            elif decision == "revise":
                logger.warning(
                    "[ReaderPanelPlanAgent] round={} phase=iterative_review revise_without_patch rationale={}",
                    int(r),
                    rationale[:300],
                )
            elif decision == "done":
                ai_done = True

            rounds.append(
                {
                    "round": int(r),
                    "phase": "iterative_review",
                    "decision": decision,
                    "rationale": rationale[:300],
                    "validation_status": str(report.get("status") or ""),
                    "passed": bool(report.get("passed")),
                    "patch_applied": bool(patch_applied),
                    "patch_keys": sorted(list(dict(patch).keys()))[:20] if isinstance(patch, dict) else [],
                }
            )
            logger.info(
                "[ReaderPanelPlanAgent] round={} phase=iterative_review decision={} patch_applied={} validation_status={} passed={}",
                int(r),
                decision,
                bool(patch_applied),
                str(report.get("status") or ""),
                bool(report.get("passed")),
            )
            if ai_done:
                break

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        status = "done" if ai_done else "fallback"
        degraded_reason = "" if status == "done" else "agent_max_rounds_exhausted"
        if status != "done":
            logger.warning(
                "[ReaderPanelPlanAgent] finalize status=fallback reason={} rounds={}",
                degraded_reason,
                _compact_json(rounds),
            )
        return {
            "status": status,
            "degraded_reason": degraded_reason,
            "panel_plan": panel_plan,
            "validation_report": report,
            "repair_report": {"steps_executed": int(len(rounds)), "elapsed_ms": elapsed_ms, "step_metrics": rounds},
            "usage": usage_total,
        }


_reader_panel_plan_agent_service: Optional[ReaderPanelPlanAgentService] = None


def get_reader_panel_plan_agent_service() -> ReaderPanelPlanAgentService:
    global _reader_panel_plan_agent_service
    if _reader_panel_plan_agent_service is None:
        _reader_panel_plan_agent_service = ReaderPanelPlanAgentService()
    return _reader_panel_plan_agent_service
