from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings

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
            {"name": "ParagraphProse", "props": {"text": "string"}},
            {"name": "ListBlock", "props": {"items": "string[]"}},
            {"name": "FigurePanel", "props": {"caption": "string", "image_url": "string"}},
            {"name": "TablePanel", "props": {"title": "string", "rows": "object[]"}},
            {"name": "ContextRail", "props": {"title": "string", "items": "object[]"}},
            {"name": "CalloutBox", "props": {"type": "string", "title": "string", "content": "string"}},
            {"name": "KeyTakeaways", "props": {"items": "object[]"}},
            {"name": "AbstractCard", "props": {"text": "string"}},
            {"name": "CitationCard", "props": {"title": "string", "authors": "string[]"}},
            {"name": "EquationBlock", "props": {"latex": "string"}},
            {"name": "MethodologyCard", "props": {"steps": "string[]"}},
        ]

    @staticmethod
    def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
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
            row["nodes"] = [dict(item) for item in list(nodes or []) if isinstance(item, dict)] if isinstance(nodes, list) else []
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
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=float(temperature),
            max_tokens=max(512, int(max_tokens)),
            timeout=float(timeout_seconds),
        )
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

    async def run(
        self,
        *,
        docmind_blocks: Sequence[Mapping[str, Any]],
        rendered_page_image: str,
        component_whitelist: Sequence[str],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: Optional[str],
        max_rounds: int,
    ) -> Dict[str, Any]:
        known_ids = [str(row.get("layout_id") or "").strip() for row in list(docmind_blocks or []) if isinstance(row, Mapping) and str(row.get("layout_id") or "").strip()]
        known_ids = list(dict.fromkeys(known_ids))
        whitelist = [str(item).strip() for item in list(component_whitelist or []) if str(item).strip()]
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        model = str(getattr(settings, "reader_agent_model", "qwen-3.5-plus") or "qwen-3.5-plus").strip()
        timeout_seconds = max(8.0, float(int(getattr(settings, "reader_agent_timeout_ms", 90000) or 90000) / 1000.0))
        max_tokens = max(1024, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000))
        style_goal = f"{str(style_intent or '').strip() or 'editorial'}; theme={str(theme_mode or 'light').strip() or 'light'}; detail={str(detail_level or 'standard').strip() or 'standard'}"
        started = time.perf_counter()

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
            "Rules: do not invent source_layout_ids; do not silently drop content; use coverage.omitted_layout_ids for omissions; "
            "avoid placeholder titles; use only component_whitelist.\n"
            "Schema: {schema_version,creative_direction,style_plan,panels,decision_log,coverage}\n"
            "nodes[]: {node_id,component,props,source_layout_ids,uniqueid?,style_patch?,children?}\n"
            f"style_goal={style_goal}\n"
            f"component_whitelist={_compact_json(whitelist)}\n"
            f"component_catalog={_compact_json(self._component_catalog)}\n"
            f"docmind_blocks={_compact_json(list(docmind_blocks or []))}"
        )
        content: List[Dict[str, Any]] = [{"type": "text", "text": propose_prompt}]
        image_token = str(rendered_page_image or "").strip()
        if image_token and (image_token.startswith("data:image/") or image_token.startswith("http")):
            content.append({"type": "image_url", "image_url": {"url": image_token}})
        tools = [{"type": "function", "function": {"name": "propose_panel_plan", "parameters": {"type": "object", "properties": {"panel_plan": {"type": "object"}}, "required": ["panel_plan"]}}}]

        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            propose_obj, usage = await self._call_tool(model=model, base_url=base_url, api_key=api_key, messages=[{"role": "system", "content": "You are a reader UI planner."}, {"role": "user", "content": content}], tools=tools, tool_name="propose_panel_plan", temperature=0.6, max_tokens=max_tokens, timeout_seconds=timeout_seconds)
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
        rounds: List[Dict[str, Any]] = []

        # Lightweight review loop: ask model to return a full revised panel_plan until pass.
        for r in range(1, max(1, int(max_rounds)) + 1):
            if report.get("passed"):
                break
            review_prompt = (
                "Fix panel_plan to satisfy validation_report. Return revised panel_plan JSON only.\n"
                f"validation_report={_compact_json(report)}\n"
                f"panel_plan={_compact_json(panel_plan)}"
            )
            try:
                revised_obj, usage = await self._call_tool(
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    messages=[{"role": "system", "content": "You are a strict reviewer."}, {"role": "user", "content": [{"type": "text", "text": review_prompt}]}],
                    tools=[{"type": "function", "function": {"name": "propose_panel_plan", "parameters": {"type": "object", "properties": {"panel_plan": {"type": "object"}}, "required": ["panel_plan"]}}}],
                    tool_name="propose_panel_plan",
                    temperature=0.15,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
                for key in usage_total:
                    usage_total[key] += int(usage.get(key) or 0)
                revised_plan = revised_obj.get("panel_plan") if isinstance(revised_obj, dict) else None
                if isinstance(revised_plan, dict):
                    panel_plan = self._normalize_plan(revised_plan)
                    report = self._validate_plan(panel_plan=panel_plan, known_layout_ids=known_ids, component_whitelist=whitelist)
                rounds.append({"round": int(r), "validation_status": str(report.get("status") or ""), "passed": bool(report.get("passed"))})
            except Exception as exc:
                rounds.append({"round": int(r), "validation_status": "error", "passed": False, "error": str(exc)})
                break

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        status = "done" if bool(report.get("passed")) else "fallback"
        degraded_reason = "" if status == "done" else "validator_non_converged"
        return {
            "status": status,
            "degraded_reason": degraded_reason,
            "panel_plan": panel_plan,
            "validation_report": report,
            "repair_report": {"steps_executed": int(len(rounds) + 1), "elapsed_ms": elapsed_ms, "step_metrics": rounds},
            "usage": usage_total,
        }


_reader_panel_plan_agent_service: Optional[ReaderPanelPlanAgentService] = None


def get_reader_panel_plan_agent_service() -> ReaderPanelPlanAgentService:
    global _reader_panel_plan_agent_service
    if _reader_panel_plan_agent_service is None:
        _reader_panel_plan_agent_service = ReaderPanelPlanAgentService()
    return _reader_panel_plan_agent_service
