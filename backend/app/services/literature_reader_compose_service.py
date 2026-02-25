"""
Composed literature reader service.

目标：
- 在现有结构化抽取基础上，生成受控组件树（UI-DSL）
- 引入质量评分与迭代修订（ReAct 风格）
- 复用共享缓存（Redis + DB）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.knowledge import Document
from app.models.literature import Paper, PaperReaderComponentOverlay, PaperReaderPageCache
from app.services.literature_reader_service import get_literature_reader_service
from app.services.llm_service import get_llm_service
from app.services.status_event_bus import build_status_channel_for_user, publish_status_event

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover
    redis_async = None


COMPOSE_ENGINE_VERSION = "reader_compose_v1"
COMPOSE_COMPONENT_SCHEMA_VERSION = "reader_components_v1"
COMPOSE_AGENT_PROMPT_VERSION = "reader_compose_prompt_v1"
COMPOSE_ASSET_POLICY_VERSION = "reader_asset_policy_v1"

DEFAULT_QUALITY_TARGET = 0.86
DEFAULT_LATENCY_BUDGET_MS = 8500
DEFAULT_MAX_ITERATIONS = 5
LOW_CONFIDENCE_MAX_ITERATIONS = 7

REDIS_TTL_SECONDS = 24 * 3600
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 6.0
LOCK_POLL_INTERVAL_SECONDS = 0.22
REDIS_KEY_PREFIX = "lit:reader:compose:v1"
REDIS_LOCK_PREFIX = "lit:reader:compose:lock:v1"

COMPONENT_WHITELIST = {
    "PaperHeaderCard",
    "MetadataSidebarCard",
    "SectionTOC",
    "SectionHeading",
    "ParagraphProse",
    "ListBlock",
    "FigurePanel",
    "TablePanel",
    "CitationLinks",
    "KeyTakeaways",
    "AnnotationRail",
    "QualityBadge",
    "QualityPanel",
    "InlineQuerySlot",
    "AnswerCard",
    "CompareInsightsCard",
    "PdfSnippetCard",
}

_SIDEBAR_TEXT_PATTERNS = (
    "open access",
    "citation:",
    "received:",
    "accepted:",
    "published:",
    "editor:",
    "copyright",
)


@dataclass
class ReaderComposeBuildMeta:
    cache_hit: bool
    cache_layer: str
    build_mode: str
    source_signature: str
    source_sig_hash: str
    engine_version: str = COMPOSE_ENGINE_VERSION
    iterations: int = 0
    degraded: bool = False
    stop_reason: str = "unknown"


class LiteratureReaderComposeService:
    def __init__(self) -> None:
        self._redis_client: Any = None
        self._reader_service = get_literature_reader_service()

    async def build_or_get_composed_payload(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        selected_kb_id: Optional[int] = None,
        force_refresh: bool = False,
        regenerate: bool = False,
        latency_budget_ms: Optional[int] = None,
        quality_target: Optional[float] = None,
        style_intent: Optional[str] = None,
        theme_mode: Optional[str] = None,
        detail_level: Optional[str] = None,
        compare_mode: Optional[bool] = None,
        citation_tldr: Optional[bool] = None,
        publish_ready_event_enabled: bool = False,
    ) -> Tuple[Dict[str, Any], ReaderComposeBuildMeta]:
        page_num = max(1, int(page))
        force_refresh = bool(force_refresh or regenerate)
        latency_budget = self._normalize_latency_budget(latency_budget_ms)
        quality_goal = self._normalize_quality_target(quality_target)
        normalized_theme = self._normalize_theme_mode(theme_mode)
        normalized_detail = self._normalize_detail_level(detail_level)
        use_compare_mode = bool(compare_mode)
        use_citation_tldr = bool(citation_tldr)

        source_signature = await self._build_source_signature(
            db=db,
            paper=paper,
            selected_kb_id=selected_kb_id,
            style_intent=style_intent,
            theme_mode=normalized_theme,
            detail_level=normalized_detail,
            compare_mode=use_compare_mode,
            citation_tldr=use_citation_tldr,
        )
        sig_hash = self._signature_hash(source_signature)
        redis_key = self._cache_key(paper_id=int(paper.id), page=page_num, sig_hash=sig_hash)
        lock_key = self._lock_key(paper_id=int(paper.id), page=page_num)

        if not force_refresh:
            cached_payload = await self._read_payload_from_redis(redis_key)
            if isinstance(cached_payload, dict):
                payload = self._with_cache_meta(cached_payload, cache_hit=True, cache_layer="redis")
                payload = await self._apply_overlay_for_user(
                    db=db,
                    user_id=int(user_id),
                    paper_id=int(paper.id),
                    page=page_num,
                    source_signature=source_signature,
                    payload=payload,
                )
                quality_report = payload.get("quality_report") or {}
                return payload, ReaderComposeBuildMeta(
                    cache_hit=True,
                    cache_layer="redis",
                    build_mode=str(payload.get("build_mode") or "compose_cache"),
                    source_signature=source_signature,
                    source_sig_hash=sig_hash,
                    iterations=int(quality_report.get("iterations") or 0),
                    degraded=bool(quality_report.get("degraded")),
                    stop_reason=str(quality_report.get("stop_reason") or "cache_hit"),
                )

            cached_row = await self._read_payload_from_db(
                db=db,
                paper_id=int(paper.id),
                page=page_num,
                source_signature=source_signature,
            )
            if isinstance(cached_row, dict):
                await self._write_payload_to_redis(redis_key, cached_row)
                payload = self._with_cache_meta(cached_row, cache_hit=True, cache_layer="db")
                payload = await self._apply_overlay_for_user(
                    db=db,
                    user_id=int(user_id),
                    paper_id=int(paper.id),
                    page=page_num,
                    source_signature=source_signature,
                    payload=payload,
                )
                quality_report = payload.get("quality_report") or {}
                return payload, ReaderComposeBuildMeta(
                    cache_hit=True,
                    cache_layer="db",
                    build_mode=str(payload.get("build_mode") or "compose_cache"),
                    source_signature=source_signature,
                    source_sig_hash=sig_hash,
                    iterations=int(quality_report.get("iterations") or 0),
                    degraded=bool(quality_report.get("degraded")),
                    stop_reason=str(quality_report.get("stop_reason") or "cache_hit"),
                )

        lock_token = await self._acquire_lock(lock_key)
        if lock_token is None:
            waited = 0.0
            while waited < LOCK_WAIT_SECONDS:
                await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)
                waited += LOCK_POLL_INTERVAL_SECONDS
                cached_payload = await self._read_payload_from_redis(redis_key)
                if isinstance(cached_payload, dict):
                    payload = self._with_cache_meta(cached_payload, cache_hit=True, cache_layer="redis")
                    payload = await self._apply_overlay_for_user(
                        db=db,
                        user_id=int(user_id),
                        paper_id=int(paper.id),
                        page=page_num,
                        source_signature=source_signature,
                        payload=payload,
                    )
                    quality_report = payload.get("quality_report") or {}
                    return payload, ReaderComposeBuildMeta(
                        cache_hit=True,
                        cache_layer="redis",
                        build_mode=str(payload.get("build_mode") or "compose_cache"),
                        source_signature=source_signature,
                        source_sig_hash=sig_hash,
                        iterations=int(quality_report.get("iterations") or 0),
                        degraded=bool(quality_report.get("degraded")),
                        stop_reason=str(quality_report.get("stop_reason") or "cache_hit"),
                    )

        try:
            base_payload, _ = await self._reader_service.build_or_get_page_payload(
                db=db,
                user_id=int(user_id),
                paper=paper,
                page=page_num,
                selected_kb_id=selected_kb_id,
                force_refresh=bool(force_refresh),
                style_hint=None,
                prefer_agent=bool(regenerate),
                publish_ready_event_enabled=False,
            )

            loop_result = await self.run_react_compose_loop(
                paper=paper,
                page=page_num,
                base_payload=base_payload,
                style_intent=style_intent,
                theme_mode=normalized_theme,
                detail_level=normalized_detail,
                compare_mode=use_compare_mode,
                quality_target=quality_goal,
                latency_budget_ms=latency_budget,
            )
            assets = await self.collect_assets_with_policy(
                paper=paper,
                page=page_num,
                base_payload=base_payload,
                ui_plan=loop_result.get("ui_plan") or {},
                citation_tldr=use_citation_tldr,
            )

            quality_report = dict(loop_result.get("quality_report") or {})
            quality_report["iterations"] = int(loop_result.get("iterations") or 0)
            quality_report["degraded"] = bool(loop_result.get("degraded"))
            quality_report["stop_reason"] = str(loop_result.get("stop_reason") or "unknown")
            quality_report["quality_target"] = quality_goal
            quality_report["latency_budget_ms"] = latency_budget

            payload: Dict[str, Any] = {
                "paper_id": int(paper.id),
                "page": page_num,
                "engine_version": COMPOSE_ENGINE_VERSION,
                "source_signature": source_signature,
                "build_mode": str(loop_result.get("build_mode") or "compose_agent"),
                "ui_plan": dict(loop_result.get("ui_plan") or {}),
                "assets": assets,
                "quality_report": quality_report,
                "iteration_trace": list(loop_result.get("iteration_trace") or []),
                "asset_policy": {
                    "pdf_first": True,
                    "web_fallback": True,
                    "max_external_images": 2,
                    "version": COMPOSE_ASSET_POLICY_VERSION,
                },
                "overlay_applied": False,
                "overlay_count": 0,
                "generated_at": datetime.utcnow().isoformat(),
            }

            await self._upsert_payload_to_db(
                db=db,
                paper_id=int(paper.id),
                page=page_num,
                source_signature=source_signature,
                parser_version=COMPOSE_ENGINE_VERSION,
                build_mode=str(payload["build_mode"]),
                structure_confidence=max(
                    0.0, min(1.0, float(quality_report.get("overall") or 0.0))
                ),
                payload=payload,
            )
            await self._write_payload_to_redis(redis_key, payload)

            if publish_ready_event_enabled:
                await self._publish_reader_ready_event(
                    user_id=int(user_id),
                    paper_id=int(paper.id),
                    page=page_num,
                    source_signature=source_signature,
                )

            payload = self._with_cache_meta(payload, cache_hit=False, cache_layer="none")
            payload = await self._apply_overlay_for_user(
                db=db,
                user_id=int(user_id),
                paper_id=int(paper.id),
                page=page_num,
                source_signature=source_signature,
                payload=payload,
            )
            return payload, ReaderComposeBuildMeta(
                cache_hit=False,
                cache_layer="none",
                build_mode=str(payload.get("build_mode") or "compose_agent"),
                source_signature=source_signature,
                source_sig_hash=sig_hash,
                iterations=int(quality_report.get("iterations") or 0),
                degraded=bool(quality_report.get("degraded")),
                stop_reason=str(quality_report.get("stop_reason") or "unknown"),
            )
        finally:
            if lock_token is not None:
                await self._release_lock(lock_key, lock_token)

    async def run_react_compose_loop(
        self,
        *,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        quality_target: float,
        latency_budget_ms: int,
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        confidence = float(base_payload.get("structure_confidence") or 0.0)
        max_iterations = (
            LOW_CONFIDENCE_MAX_ITERATIONS
            if confidence < 0.68
            else DEFAULT_MAX_ITERATIONS
        )

        current_plan = self._build_initial_ui_plan(
            paper=paper,
            page=page,
            base_payload=base_payload,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
        )
        best_plan = current_plan
        best_quality: Dict[str, Any] = {}
        best_score = -1.0
        iteration_trace: List[Dict[str, Any]] = []
        degraded = False
        stop_reason = "max_iterations_reached"

        for iteration in range(1, max_iterations + 1):
            validation = self.validate_ui_plan(current_plan, page=page)
            quality = self.score_ui_plan(
                ui_plan=current_plan,
                base_payload=base_payload,
                validation_errors=validation.get("errors") or [],
                quality_target=quality_target,
            )
            quality["iteration"] = iteration
            quality["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
            quality.setdefault("tool_call_trace", [])

            iteration_trace.append(
                {
                    "iteration": iteration,
                    "ui_plan": current_plan,
                    "quality_report": quality,
                }
            )

            overall = float(quality.get("overall") or 0.0)
            if overall >= best_score:
                best_score = overall
                best_plan = current_plan
                best_quality = quality

            hard_pass = bool(quality.get("hard_constraints_passed"))
            if hard_pass and overall >= quality_target:
                stop_reason = "quality_threshold_met"
                break

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if elapsed_ms >= latency_budget_ms:
                degraded = True
                stop_reason = "latency_budget_exceeded"
                break

            if iteration >= max_iterations:
                stop_reason = "max_iterations_reached"
                break

            current_plan = self._revise_ui_plan(
                ui_plan=current_plan,
                base_payload=base_payload,
                quality_report=quality,
            )

        if not best_quality:
            best_quality = {
                "overall": 0.0,
                "hard_constraints_passed": False,
                "quality_target": quality_target,
                "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            }

        best_quality["degraded"] = degraded
        best_quality["stop_reason"] = stop_reason
        best_quality["iterations"] = len(iteration_trace)
        best_quality["quality_target"] = quality_target
        best_quality["latency_budget_ms"] = latency_budget_ms
        best_quality["iteration_trace_summary"] = [
            {
                "iteration": int(item.get("iteration") or 0),
                "overall": float((item.get("quality_report") or {}).get("overall") or 0.0),
                "hard_constraints_passed": bool((item.get("quality_report") or {}).get("hard_constraints_passed")),
            }
            for item in iteration_trace
        ]

        return {
            "ui_plan": best_plan,
            "quality_report": best_quality,
            "iteration_trace": iteration_trace,
            "iterations": len(iteration_trace),
            "degraded": degraded,
            "stop_reason": stop_reason,
            "build_mode": "compose_agent",
        }

    def validate_ui_plan(self, ui_plan: Dict[str, Any], *, page: int) -> Dict[str, Any]:
        errors: List[str] = []
        if not isinstance(ui_plan, dict):
            return {"valid": False, "errors": ["ui_plan must be an object"]}

        components = ui_plan.get("components")
        if not isinstance(components, list):
            return {"valid": False, "errors": ["components must be a list"]}

        seen_ids: set[str] = set()

        def _walk(nodes: Sequence[Dict[str, Any]], depth: int = 0) -> None:
            if depth > 8:
                errors.append("component tree depth exceeds 8")
                return
            for node in nodes:
                if not isinstance(node, dict):
                    errors.append("component node must be an object")
                    continue
                node_id = str(node.get("id") or "").strip()
                node_type = str(node.get("type") or "").strip()
                props = node.get("props")
                children = node.get("children") or []
                anchor_refs = node.get("source_anchor_refs") or []

                if not node_id:
                    errors.append("component id is required")
                elif node_id in seen_ids:
                    errors.append(f"duplicated component id: {node_id}")
                else:
                    seen_ids.add(node_id)

                if node_type not in COMPONENT_WHITELIST:
                    errors.append(f"component type not allowed: {node_type}")
                if not isinstance(props, dict):
                    errors.append(f"props must be object for component {node_id or node_type}")

                if not isinstance(anchor_refs, list):
                    errors.append(f"source_anchor_refs must be list for {node_id or node_type}")
                else:
                    for anchor in anchor_refs:
                        if not isinstance(anchor, dict):
                            errors.append(f"invalid anchor in {node_id or node_type}")
                            continue
                        page_no = int(anchor.get("page") or 0)
                        start_char = int(anchor.get("start_char") or -1)
                        end_char = int(anchor.get("end_char") or -1)
                        if page_no != int(page):
                            errors.append(f"anchor page mismatch in {node_id or node_type}")
                        if start_char < 0 or end_char <= start_char:
                            errors.append(f"invalid anchor range in {node_id or node_type}")

                if children and not isinstance(children, list):
                    errors.append(f"children must be list in {node_id or node_type}")
                elif isinstance(children, list):
                    _walk(children, depth + 1)

        _walk(components)
        return {"valid": len(errors) == 0, "errors": errors}

    def score_ui_plan(
        self,
        *,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
        validation_errors: Sequence[str],
        quality_target: float,
    ) -> Dict[str, Any]:
        components = list(ui_plan.get("components") or [])
        base_blocks = list(base_payload.get("blocks") or [])
        base_assets = list(base_payload.get("assets") or [])
        base_headings = [
            str(item.get("text") or "")
            for item in base_blocks
            if str(item.get("kind") or "") == "heading"
        ]
        base_paragraphs = [
            str(item.get("text") or "")
            for item in base_blocks
            if str(item.get("kind") or "") == "paragraph"
        ]

        flat_nodes = self._flatten_components(components)
        heading_nodes = [node for node in flat_nodes if node.get("type") == "SectionHeading"]
        paragraph_nodes = [node for node in flat_nodes if node.get("type") == "ParagraphProse"]
        link_nodes = [node for node in flat_nodes if node.get("type") == "CitationLinks"]

        expected_heading_count = max(1, len(base_headings))
        rendered_heading_count = len(heading_nodes)
        structure_fidelity = min(
            1.0,
            rendered_heading_count / max(1, expected_heading_count),
        )
        if expected_heading_count > 0 and rendered_heading_count == 0:
            structure_fidelity = 0.0

        paragraph_lengths = [
            len(str((node.get("props") or {}).get("text") or ""))
            for node in paragraph_nodes
        ]
        if paragraph_lengths:
            median_len = sorted(paragraph_lengths)[len(paragraph_lengths) // 2]
            readability = 1.0
            if median_len > 720:
                readability -= 0.35
            if any(length > 1600 for length in paragraph_lengths):
                readability -= 0.25
            if any(self._has_broken_words(str((node.get("props") or {}).get("text") or "")) for node in paragraph_nodes):
                readability -= 0.25
            readability = max(0.0, min(1.0, readability))
        else:
            readability = 0.35 if base_paragraphs else 1.0

        link_assets_count = sum(1 for item in base_assets if str(item.get("kind") or "") == "link")
        evidence_alignment = 1.0 if link_assets_count == 0 else (1.0 if link_nodes else 0.45)

        has_header = any(node.get("type") == "PaperHeaderCard" for node in flat_nodes)
        has_body = bool(paragraph_nodes)
        has_toc_or_meta = any(
            node.get("type") in {"SectionTOC", "MetadataSidebarCard"} for node in flat_nodes
        )
        layout_consistency = 0.0
        if has_header:
            layout_consistency += 0.34
        if has_body:
            layout_consistency += 0.33
        if has_toc_or_meta:
            layout_consistency += 0.33
        layout_consistency = min(1.0, layout_consistency)

        sidebar_leak = self._detect_sidebar_leak(paragraph_nodes)
        title_integrity = self._check_title_integrity(flat_nodes, base_payload)
        anchors_valid = not any("anchor" in str(item).lower() for item in validation_errors)
        hard_constraints_passed = bool(title_integrity and not sidebar_leak and anchors_valid)

        overall = (
            0.45 * structure_fidelity
            + 0.25 * readability
            + 0.20 * evidence_alignment
            + 0.10 * layout_consistency
        )
        deductions: List[Dict[str, Any]] = []
        if validation_errors:
            penalty = min(0.25, 0.03 * len(validation_errors))
            deductions.append(
                {
                    "item": "schema_validation",
                    "penalty": round(penalty, 4),
                    "reason": f"发现 {len(validation_errors)} 个结构校验错误",
                }
            )
            overall -= min(0.25, 0.03 * len(validation_errors))
        if sidebar_leak:
            deductions.append(
                {
                    "item": "sidebar_leak",
                    "penalty": 0.3,
                    "reason": "检测到侧栏文本泄漏到正文",
                }
            )
            overall -= 0.3
        if not title_integrity:
            deductions.append(
                {
                    "item": "title_integrity",
                    "penalty": 0.2,
                    "reason": "标题完整性不足",
                }
            )
            overall -= 0.2
        overall = max(0.0, min(1.0, overall))

        fix_suggestions: List[str] = []
        if sidebar_leak:
            fix_suggestions.append("优先执行侧栏隔离修复，避免 OPEN ACCESS/Citation 混入正文。")
        if not title_integrity:
            fix_suggestions.append("补齐并校验首个章节标题，确保标题不被并入正文段落。")
        if validation_errors:
            fix_suggestions.append("执行节点级修复，重新校验 anchor 与组件字段。")
        if readability < 0.72:
            fix_suggestions.append("执行断词修复与段落分句优化，降低长段落密度。")
        if evidence_alignment < 0.8:
            fix_suggestions.append("补充 DOI/URL 对应证据锚点，提升证据对齐度。")

        return {
            "overall": round(overall, 4),
            "structure_fidelity": round(structure_fidelity, 4),
            "readability": round(readability, 4),
            "evidence_alignment": round(evidence_alignment, 4),
            "layout_consistency": round(layout_consistency, 4),
            "hard_constraints_passed": hard_constraints_passed,
            "sidebar_leak_detected": sidebar_leak,
            "title_integrity_ok": title_integrity,
            "anchors_valid": anchors_valid,
            "validation_errors": list(validation_errors),
            "quality_target": quality_target,
            "deductions": deductions,
            "fix_suggestions": fix_suggestions[:6],
        }

    async def collect_assets_with_policy(
        self,
        *,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        ui_plan: Dict[str, Any],
        citation_tldr: bool = False,
    ) -> List[Dict[str, Any]]:
        base_assets = list(base_payload.get("assets") or [])
        dedup: Dict[str, Dict[str, Any]] = {}

        def _asset_key(item: Dict[str, Any]) -> str:
            kind = str(item.get("kind") or "")
            href = str(item.get("href") or "")
            label = str(item.get("label") or "")
            return f"{kind}|{href.lower()}|{label.lower()}"

        for item in base_assets:
            if not isinstance(item, dict):
                continue
            dedup[_asset_key(item)] = dict(item)

        has_pdf_image = any(str(item.get("kind") or "") == "image_hint" for item in dedup.values())
        if not has_pdf_image:
            query = self._build_external_image_query(
                paper=paper,
                ui_plan=ui_plan,
                base_payload=base_payload,
            )
            external_images = await asyncio.to_thread(
                self._search_external_images_sync,
                query,
                2,
            )
            for idx, item in enumerate(external_images, start=1):
                asset = {
                    "kind": "external_image",
                    "label": str(item.get("caption") or f"External image {idx}"),
                    "source": "web",
                    "href": str(item.get("image_url") or ""),
                    "meta": {
                        "source_url": str(item.get("source_url") or ""),
                        "source_domain": str(item.get("source_domain") or ""),
                        "license": str(item.get("license") or "unknown"),
                        "caption": str(item.get("caption") or ""),
                        "why_relevant": str(item.get("why_relevant") or ""),
                        "page": int(page),
                    },
                }
                # 中文注释：外网图片必须包含可追溯来源信息。
                meta = asset.get("meta") or {}
                if not (meta.get("source_url") and meta.get("source_domain") and meta.get("license")):
                    continue
                dedup[_asset_key(asset)] = asset

        merged_assets = list(dedup.values())[:18]
        if citation_tldr:
            for item in merged_assets:
                if str(item.get("kind") or "") != "link":
                    continue
                href = str(item.get("href") or "")
                label = str(item.get("label") or "链接")
                item["tldr"] = self._build_link_tldr(
                    href=href,
                    label=label,
                    paper=paper,
                )
        return merged_assets

    def queue_prefetch(
        self,
        *,
        pages: Sequence[int],
        max_page: Optional[int],
    ) -> Tuple[List[int], List[int]]:
        queued: List[int] = []
        skipped: List[int] = []
        seen: set[int] = set()
        upper_bound = int(max_page) if isinstance(max_page, int) and max_page > 0 else None
        for raw_page in pages:
            try:
                page = int(raw_page)
            except Exception:
                continue
            if page <= 0:
                skipped.append(page)
                continue
            if upper_bound is not None and page > upper_bound:
                skipped.append(page)
                continue
            if page in seen:
                skipped.append(page)
                continue
            seen.add(page)
            queued.append(page)
        return queued, skipped

    async def prefetch_pages(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        pages: Sequence[int],
        selected_kb_id: Optional[int] = None,
        style_intent: Optional[str] = None,
        latency_budget_ms: Optional[int] = None,
        quality_target: Optional[float] = None,
        theme_mode: Optional[str] = None,
        detail_level: Optional[str] = None,
        compare_mode: Optional[bool] = None,
        citation_tldr: Optional[bool] = None,
    ) -> None:
        for page in pages:
            try:
                await self.build_or_get_composed_payload(
                    db=db,
                    user_id=user_id,
                    paper=paper,
                    page=int(page),
                    selected_kb_id=selected_kb_id,
                    force_refresh=False,
                    style_intent=style_intent,
                    latency_budget_ms=latency_budget_ms,
                    quality_target=quality_target,
                    theme_mode=theme_mode,
                    detail_level=detail_level,
                    compare_mode=compare_mode,
                    citation_tldr=citation_tldr,
                    publish_ready_event_enabled=True,
                )
            except Exception as exc:
                logger.warning(
                    f"[ReaderComposeService] prefetch failed paper={paper.id} page={page}: {exc}"
                )

    async def _publish_reader_ready_event(
        self,
        *,
        user_id: int,
        paper_id: int,
        page: int,
        source_signature: str,
    ) -> None:
        payload = {
            "event": "reader_page_ready",
            "data": {
                "paper_id": int(paper_id),
                "page": int(page),
                "source_signature": source_signature,
                "updated_at": datetime.utcnow().isoformat(),
            },
        }
        try:
            await publish_status_event(build_status_channel_for_user(int(user_id)), payload)
        except Exception as exc:
            logger.warning(
                f"[ReaderComposeService] publish reader_page_ready failed paper={paper_id}, page={page}: {exc}"
            )

    def _build_initial_ui_plan(
        self,
        *,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
    ) -> Dict[str, Any]:
        sections = list(base_payload.get("sections") or [])
        blocks = list(base_payload.get("blocks") or [])
        assets = list(base_payload.get("assets") or [])
        summary = str(base_payload.get("summary") or "").strip()
        style_cues = dict(base_payload.get("style_cues") or {})

        components: List[Dict[str, Any]] = []
        cid = 0

        def next_id(prefix: str) -> str:
            nonlocal cid
            cid += 1
            return f"{prefix}_{cid}"

        def wrap_anchor(anchor: Any, quote_text: str = "") -> List[Dict[str, Any]]:
            if not isinstance(anchor, dict):
                return []
            try:
                bbox_hint = self._build_bbox_hint(
                    style_cues=style_cues,
                    quote_text=quote_text,
                )
                return [
                    {
                        "page": int(anchor.get("page") or page),
                        "start_char": int(anchor.get("start_char") or 0),
                        "end_char": int(anchor.get("end_char") or 1),
                        "quote_text": quote_text[:280] if quote_text else None,
                        "bbox_hint": bbox_hint,
                    }
                ]
            except Exception:
                return []

        components.append(
            {
                "id": next_id("header"),
                "type": "PaperHeaderCard",
                "props": {
                    "title": str(paper.title or "Untitled Paper"),
                    "venue": str(paper.venue or ""),
                    "year": int(paper.year) if paper.year else None,
                    "authors": [str(item.get("name") or "") for item in list(paper.authors or [])[:10]],
                },
                "children": [],
                "source_anchor_refs": [],
                "capabilities": ["copy"],
                "actions": [
                    {"key": "copy", "label": "复制", "kind": "default", "payload": {}},
                ],
                "layout_slot": {"reserved_height": 200, "lock_height": True},
            }
        )

        metadata_items: List[Dict[str, Any]] = []
        if paper.doi:
            metadata_items.append({"label": "DOI", "value": str(paper.doi)})
        if paper.venue:
            metadata_items.append({"label": "Venue", "value": str(paper.venue)})
        if paper.year:
            metadata_items.append({"label": "Year", "value": str(paper.year)})
        if paper.pdf_url:
            metadata_items.append({"label": "PDF", "value": str(paper.pdf_url)})
        if paper.url:
            metadata_items.append({"label": "Paper", "value": str(paper.url)})

        components.append(
            {
                "id": next_id("meta"),
                "type": "MetadataSidebarCard",
                "props": {"items": metadata_items[:10]},
                "children": [],
                "source_anchor_refs": [],
                "capabilities": ["copy"],
                "actions": [
                    {"key": "copy", "label": "复制", "kind": "default", "payload": {}},
                ],
                "layout_slot": {"reserved_height": 220, "lock_height": True},
            }
        )

        toc_items = []
        for item in sections:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            toc_items.append(
                {
                    "title": title,
                    "anchor": wrap_anchor(item.get("source_anchor"), quote_text=title),
                }
            )
        components.append(
            {
                "id": next_id("toc"),
                "type": "SectionTOC",
                "props": {"items": toc_items[:24]},
                "children": [],
                "source_anchor_refs": [],
                "capabilities": ["jump_anchor"],
                "actions": [],
                "layout_slot": {"reserved_height": 220, "lock_height": True},
            }
        )

        if summary:
            summary_parts = [item.strip() for item in re.split(r"[。.!?；;]\s*", summary) if item.strip()]
            if detail_level == "concise":
                summary_parts = summary_parts[:3]
            elif detail_level == "deep":
                summary_parts = summary_parts[:8]
            components.append(
                {
                    "id": next_id("takeaways"),
                    "type": "KeyTakeaways",
                    "props": {
                        "items": [
                            {
                                "text": text,
                                "evidence_anchors": [],
                            }
                            for text in summary_parts[:8]
                        ],
                    },
                    "children": [],
                    "source_anchor_refs": [],
                    "capabilities": ["jump_anchor", "copy", "drag_markdown"],
                    "actions": [
                        {"key": "copy", "label": "复制", "kind": "default", "payload": {}},
                    ],
                    "layout_slot": {"reserved_height": 180, "lock_height": True},
                }
            )

        link_assets = [item for item in assets if str(item.get("kind") or "") == "link"]
        if link_assets:
            links = []
            for item in link_assets[:12]:
                href = str(item.get("href") or "")
                if not href:
                    continue
                links.append(
                    {
                        "label": str(item.get("label") or "Link"),
                        "href": href,
                        "source": str(item.get("source") or "metadata"),
                    }
                )
            if links:
                components.append(
                    {
                        "id": next_id("citations"),
                        "type": "CitationLinks",
                        "props": {"links": links},
                        "children": [],
                        "source_anchor_refs": [],
                        "capabilities": ["copy"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 170, "lock_height": True},
                    }
                )

        annotation_assets = [
            item for item in assets if str(item.get("kind") or "") == "annotation"
        ]
        if annotation_assets:
            components.append(
                {
                    "id": next_id("annotations"),
                    "type": "AnnotationRail",
                    "props": {
                        "items": [str(item.get("label") or "") for item in annotation_assets[:8]],
                    },
                    "children": [],
                    "source_anchor_refs": [],
                    "capabilities": ["copy"],
                    "actions": [],
                    "layout_slot": {"reserved_height": 170, "lock_height": True},
                }
            )

        for block in blocks:
            kind = str(block.get("kind") or "")
            text = self._normalize_spaces(str(block.get("text") or ""))
            if not text:
                continue
            if self._looks_like_sidebar_text(text):
                continue
            anchor_refs = wrap_anchor(block.get("source_anchor"), quote_text=text)
            if kind == "heading":
                components.append(
                    {
                        "id": next_id("heading"),
                        "type": "SectionHeading",
                        "props": {
                            "text": text,
                            "level": self._infer_heading_level(text),
                        },
                        "children": [],
                        "source_anchor_refs": anchor_refs,
                        "capabilities": ["jump_anchor", "copy"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 86, "lock_height": True},
                    }
                )
            elif kind == "list_item":
                components.append(
                    {
                        "id": next_id("list"),
                        "type": "ListBlock",
                        "props": {"items": [text]},
                        "children": [],
                        "source_anchor_refs": anchor_refs,
                        "capabilities": ["copy", "drag_markdown"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 130, "lock_height": False},
                    }
                )
            elif kind == "caption":
                components.append(
                    {
                        "id": next_id("figure"),
                        "type": "FigurePanel",
                        "props": {
                            "caption": text,
                            "image_url": None,
                            "ai_insight": self._build_caption_insight(text),
                        },
                        "children": [],
                        "source_anchor_refs": anchor_refs,
                        "capabilities": ["copy", "drag_markdown", "jump_anchor"],
                        "actions": [
                            {"key": "copy_markdown", "label": "复制Markdown", "kind": "default", "payload": {}},
                        ],
                        "layout_slot": {"reserved_height": 260, "lock_height": True},
                    }
                )
            else:
                components.append(
                    {
                        "id": next_id("paragraph"),
                        "type": "ParagraphProse",
                        "props": {"text": text},
                        "children": [],
                        "source_anchor_refs": anchor_refs,
                        "capabilities": ["copy", "drag_markdown", "inline_query", "jump_anchor"],
                        "actions": [
                            {"key": "regenerate", "label": "修复", "kind": "default", "payload": {}},
                            {"key": "degrade", "label": "降级", "kind": "default", "payload": {}},
                            {"key": "copy", "label": "复制", "kind": "default", "payload": {}},
                        ],
                        "layout_slot": {"reserved_height": 210, "lock_height": False},
                    }
                )
                components.append(
                    {
                        "id": next_id("inline_slot"),
                        "type": "InlineQuerySlot",
                        "props": {
                            "placeholder": "在此提问（仅针对当前段落/章节）",
                            "target_node_ref": str(components[-1]["id"]),
                        },
                        "children": [],
                        "source_anchor_refs": anchor_refs,
                        "capabilities": ["inline_query"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 68, "lock_height": True},
                    }
                )

        if compare_mode:
            components.append(
                {
                    "id": next_id("compare"),
                    "type": "CompareInsightsCard",
                    "props": {
                        "mode": "knowledge_base",
                        "items": self._build_compare_insights_stub(summary),
                    },
                    "children": [],
                    "source_anchor_refs": [],
                    "capabilities": ["copy", "drag_markdown"],
                    "actions": [],
                    "layout_slot": {"reserved_height": 210, "lock_height": True},
                }
            )

        components.append(
            {
                "id": next_id("quality"),
                "type": "QualityPanel",
                "props": {"show": True},
                "children": [],
                "source_anchor_refs": [],
                "capabilities": ["copy"],
                "actions": [],
                "layout_slot": {"reserved_height": 180, "lock_height": True},
            }
        )

        return {
            "plan_id": uuid.uuid4().hex,
            "layout": {
                "mode": "split",
                "sidebar_width": 320,
                "content_max_width": 920,
            },
            "style_tokens": self._build_style_tokens(
                style_intent=style_intent,
                theme_mode=theme_mode,
                detail_level=detail_level,
            ),
            "components": components,
            "trace_meta": {
                "style_intent": str(style_intent or "auto"),
                "theme_mode": str(theme_mode or "light"),
                "detail_level": str(detail_level),
                "compare_mode": bool(compare_mode),
                "generator": COMPOSE_ENGINE_VERSION,
                "schema_version": COMPOSE_COMPONENT_SCHEMA_VERSION,
                "tool_call_trace": [],
            },
        }

    def _revise_ui_plan(
        self,
        *,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
        quality_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        revised = json.loads(json.dumps(ui_plan, ensure_ascii=False))
        components = list(revised.get("components") or [])
        patched: List[Dict[str, Any]] = []

        for node in components:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "")
            props = node.get("props") if isinstance(node.get("props"), dict) else {}
            if node_type == "ParagraphProse":
                text = self._normalize_spaces(str(props.get("text") or ""))
                if not text or self._looks_like_sidebar_text(text):
                    continue
                text = self._repair_text_artifacts(text)
                props["text"] = text
                node["props"] = props
            elif node_type == "SectionHeading":
                text = self._normalize_spaces(str(props.get("text") or ""))
                if text:
                    props["text"] = self._repair_heading_text(text)
                    node["props"] = props
            patched.append(node)

        # 中文注释：当标题组件缺失时，从 sections 中补齐至少一个。
        has_heading = any(str(item.get("type") or "") == "SectionHeading" for item in patched)
        if not has_heading:
            sections = list(base_payload.get("sections") or [])
            for section in sections:
                title = self._normalize_spaces(str(section.get("title") or ""))
                if not title or title.lower() == "body":
                    continue
                patched.insert(
                    3,
                    {
                        "id": f"heading_recover_{uuid.uuid4().hex[:8]}",
                        "type": "SectionHeading",
                        "props": {"text": title, "level": int(section.get("level") or 1)},
                        "children": [],
                        "source_anchor_refs": [section.get("source_anchor")] if isinstance(section.get("source_anchor"), dict) else [],
                    },
                )
                break

        revised["components"] = patched
        revised["plan_id"] = uuid.uuid4().hex
        revised.setdefault("trace_meta", {})
        revised["trace_meta"]["revision_reason"] = quality_report.get("stop_reason") or "iterative_refine"
        return revised

    async def perform_node_action(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        node_id: str,
        action: str,
        reason: Optional[str] = None,
        selected_kb_id: Optional[int] = None,
        style_intent: Optional[str] = None,
        theme_mode: Optional[str] = None,
        detail_level: Optional[str] = None,
        compare_mode: Optional[bool] = None,
        citation_tldr: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload, _ = await self.build_or_get_composed_payload(
            db=db,
            user_id=int(user_id),
            paper=paper,
            page=int(page),
            selected_kb_id=selected_kb_id,
            force_refresh=False,
            regenerate=False,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
            citation_tldr=citation_tldr,
            publish_ready_event_enabled=False,
        )

        ui_plan = dict(payload.get("ui_plan") or {})
        components = json.loads(json.dumps(list(ui_plan.get("components") or []), ensure_ascii=False))
        found = self._find_component_holder(components, str(node_id))
        if found is None:
            raise ValueError(f"node not found: {node_id}")
        holder, idx = found
        node_before = dict(holder[idx])
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"regenerate", "degrade"}:
            raise ValueError(f"unsupported action: {action}")

        if normalized_action == "degrade":
            node_after = self._build_degraded_node(node_before=node_before, page=int(page))
            quality_delta = -0.02
            action_message = "已降级为更稳定的展示组件。"
            patch_type = "node_replace"
        else:
            node_after = self._build_regenerated_node(node_before=node_before)
            quality_delta = 0.04
            action_message = "已完成节点修复。"
            patch_type = "node_replace"

        holder[idx] = node_after
        ui_plan["components"] = components

        overlay_json = {
            "patch_type": patch_type,
            "node_before": node_before,
            "node_after": node_after,
            "reason": str(reason or ""),
            "action": normalized_action,
        }
        await self._upsert_overlay_to_db(
            db=db,
            user_id=int(user_id),
            paper_id=int(paper.id),
            page=int(page),
            source_signature=str(payload.get("source_signature") or ""),
            node_id=str(node_id),
            action_type=normalized_action,
            overlay_json=overlay_json,
        )

        return {
            "patch_type": patch_type,
            "node_before": node_before,
            "node_after": node_after,
            "quality_delta": quality_delta,
            "overlay_saved": True,
            "message": action_message,
        }

    async def build_inline_answer_card(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        node_id: str,
        question: str,
        scope: str = "section",
        selected_kb_id: Optional[int] = None,
        style_intent: Optional[str] = None,
        detail_level: Optional[str] = None,
        compare_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload, _ = await self.build_or_get_composed_payload(
            db=db,
            user_id=int(user_id),
            paper=paper,
            page=int(page),
            selected_kb_id=selected_kb_id,
            force_refresh=False,
            regenerate=False,
            style_intent=style_intent,
            detail_level=detail_level,
            compare_mode=compare_mode,
            publish_ready_event_enabled=False,
        )

        components = list(((payload.get("ui_plan") or {}).get("components") or []))
        node = self._find_component_node(components, str(node_id))
        if node is None:
            raise ValueError(f"node not found: {node_id}")

        anchor_refs = list(node.get("source_anchor_refs") or [])
        context_text = self._extract_node_text(node)
        answer = await self._generate_inline_answer(
            question=question,
            context_text=context_text,
            scope=scope,
        )
        answer_node = {
            "id": f"answer_{uuid.uuid4().hex[:12]}",
            "type": "AnswerCard",
            "props": {
                "question": str(question).strip(),
                "answer": answer,
                "foldable": True,
            },
            "children": [],
            "source_anchor_refs": anchor_refs[:3],
            "capabilities": ["copy", "jump_anchor", "drag_markdown"],
            "actions": [
                {"key": "copy", "label": "复制", "kind": "default", "payload": {}},
            ],
            "layout_slot": {"reserved_height": 220, "lock_height": False},
        }
        sources = []
        for anchor in anchor_refs[:3]:
            if not isinstance(anchor, dict):
                continue
            sources.append(
                {
                    "page": int(anchor.get("page") or int(page)),
                    "start_char": int(anchor.get("start_char") or 0),
                    "end_char": int(anchor.get("end_char") or 0),
                    "quote_text": str(anchor.get("quote_text") or "")[:240] or None,
                }
            )
        return {"node": answer_node, "sources": sources}

    async def _generate_inline_answer(
        self,
        *,
        question: str,
        context_text: str,
        scope: str,
    ) -> str:
        compact_question = self._normalize_spaces(question)
        compact_context = self._normalize_spaces(context_text)
        if not compact_context:
            compact_context = "当前节点缺少可提取正文，请切换 PDF 模式查看原文。"
        prompt = (
            "你是论文阅读助手。请只基于给定上下文回答，不要编造。\n"
            "输出中文，先给结论，再给一句证据说明。\n"
            f"问题：{compact_question}\n"
            f"范围：{scope}\n"
            f"上下文：{compact_context[:2200]}"
        )
        try:
            llm = await get_llm_service()
            result = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=420,
            )
            content = self._normalize_spaces(str(result.get("content") or ""))
            if content:
                return content
        except Exception as exc:
            logger.debug(f"[ReaderComposeService] inline answer generation failed: {exc}")
        return f"结论：基于当前段落信息，{compact_question} 的关键依据在该段原文中。证据说明：请点击“定位到证据”查看对应锚点。"

    async def _apply_overlay_for_user(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper_id: int,
        page: int,
        source_signature: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        overlays = await self._read_overlays_from_db(
            db=db,
            user_id=int(user_id),
            paper_id=int(paper_id),
            page=int(page),
            source_signature=str(source_signature),
        )
        if not overlays:
            cloned = dict(payload)
            cloned["overlay_applied"] = False
            cloned["overlay_count"] = 0
            return cloned

        cloned = json.loads(json.dumps(payload, ensure_ascii=False))
        ui_plan = dict(cloned.get("ui_plan") or {})
        components = list(ui_plan.get("components") or [])
        applied = 0
        for row in overlays:
            patch = dict(row.overlay_json or {})
            node_after = patch.get("node_after")
            if not isinstance(node_after, dict):
                continue
            holder = self._find_component_holder(components, str(row.node_id))
            if holder is None:
                continue
            ref_nodes, ref_idx = holder
            ref_nodes[ref_idx] = node_after
            applied += 1

        ui_plan["components"] = components
        cloned["ui_plan"] = ui_plan
        cloned["overlay_applied"] = applied > 0
        cloned["overlay_count"] = applied
        return cloned

    async def _read_overlays_from_db(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper_id: int,
        page: int,
        source_signature: str,
    ) -> List[PaperReaderComponentOverlay]:
        stmt = (
            select(PaperReaderComponentOverlay)
            .where(
                and_(
                    PaperReaderComponentOverlay.user_id == int(user_id),
                    PaperReaderComponentOverlay.paper_id == int(paper_id),
                    PaperReaderComponentOverlay.page == int(page),
                    PaperReaderComponentOverlay.source_signature == str(source_signature),
                )
            )
            .order_by(PaperReaderComponentOverlay.updated_at.asc(), PaperReaderComponentOverlay.id.asc())
        )
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows or [])

    async def _upsert_overlay_to_db(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper_id: int,
        page: int,
        source_signature: str,
        node_id: str,
        action_type: str,
        overlay_json: Dict[str, Any],
    ) -> None:
        stmt = select(PaperReaderComponentOverlay).where(
            and_(
                PaperReaderComponentOverlay.user_id == int(user_id),
                PaperReaderComponentOverlay.paper_id == int(paper_id),
                PaperReaderComponentOverlay.page == int(page),
                PaperReaderComponentOverlay.source_signature == str(source_signature),
                PaperReaderComponentOverlay.node_id == str(node_id),
            )
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = PaperReaderComponentOverlay(
                user_id=int(user_id),
                paper_id=int(paper_id),
                page=int(page),
                source_signature=str(source_signature),
                node_id=str(node_id),
            )
            db.add(row)
        row.action_type = str(action_type)
        row.overlay_json = dict(overlay_json or {})
        await db.commit()

    @staticmethod
    def _find_component_holder(
        nodes: List[Dict[str, Any]],
        node_id: str,
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        for idx, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            if str(node.get("id") or "") == str(node_id):
                return nodes, idx
            children = node.get("children")
            if isinstance(children, list):
                found = LiteratureReaderComposeService._find_component_holder(children, node_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _find_component_node(
        nodes: Sequence[Dict[str, Any]],
        node_id: str,
    ) -> Optional[Dict[str, Any]]:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("id") or "") == str(node_id):
                return node
            children = node.get("children")
            if isinstance(children, list):
                found = LiteratureReaderComposeService._find_component_node(children, node_id)
                if found is not None:
                    return found
        return None

    def _build_degraded_node(self, *, node_before: Dict[str, Any], page: int) -> Dict[str, Any]:
        anchor_refs = list(node_before.get("source_anchor_refs") or [])
        fallback_text = self._extract_node_text(node_before)
        if fallback_text:
            return {
                "id": str(node_before.get("id") or f"degrade_{uuid.uuid4().hex[:8]}"),
                "type": "ParagraphProse",
                "props": {"text": fallback_text},
                "children": [],
                "source_anchor_refs": anchor_refs,
                "capabilities": ["copy", "jump_anchor", "drag_markdown"],
                "actions": [
                    {"key": "regenerate", "label": "修复", "kind": "default", "payload": {}},
                ],
                "layout_slot": {"reserved_height": 200, "lock_height": False},
            }
        return {
            "id": str(node_before.get("id") or f"degrade_{uuid.uuid4().hex[:8]}"),
            "type": "PdfSnippetCard",
            "props": {
                "title": "已降级为原文片段",
                "description": "当前节点无法稳定解析，建议切换 PDF 模式核对原文。",
                "page": int(page),
            },
            "children": [],
            "source_anchor_refs": anchor_refs,
            "capabilities": ["jump_anchor"],
            "actions": [],
            "layout_slot": {"reserved_height": 150, "lock_height": True},
        }

    def _build_regenerated_node(self, *, node_before: Dict[str, Any]) -> Dict[str, Any]:
        node_type = str(node_before.get("type") or "")
        regenerated = json.loads(json.dumps(node_before, ensure_ascii=False))
        props = regenerated.get("props") if isinstance(regenerated.get("props"), dict) else {}
        if node_type == "ParagraphProse":
            props["text"] = self._repair_text_artifacts(self._normalize_spaces(str(props.get("text") or "")))
        elif node_type == "SectionHeading":
            props["text"] = self._repair_heading_text(self._normalize_spaces(str(props.get("text") or "")))
        elif node_type in {"FigurePanel", "TablePanel"}:
            insight = self._normalize_spaces(str(props.get("ai_insight") or ""))
            if not insight:
                props["ai_insight"] = "该图表用于支撑本页关键结论，建议结合原文锚点进行核对。"
        regenerated["props"] = props
        return regenerated

    @staticmethod
    def _extract_node_text(node: Dict[str, Any]) -> str:
        props = node.get("props") if isinstance(node.get("props"), dict) else {}
        text_parts: List[str] = []
        for key in ("text", "caption", "answer", "question", "title", "description"):
            value = props.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value.strip())
        items = props.get("items")
        if isinstance(items, list):
            for item in items[:8]:
                if isinstance(item, str) and item.strip():
                    text_parts.append(item.strip())
                elif isinstance(item, dict):
                    value = str(item.get("text") or item.get("title") or "").strip()
                    if value:
                        text_parts.append(value)
        return " ".join(text_parts).strip()

    async def _build_source_signature(
        self,
        *,
        db: AsyncSession,
        paper: Paper,
        selected_kb_id: Optional[int],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        citation_tldr: bool,
    ) -> str:
        path = self._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
            user_id=int(paper.user_id),
            paper_id=int(paper.id),
            paper_title=paper.title,
            paper_pdf_path=paper.pdf_path,
        )
        stat_part = "pdf:none"
        if path and os.path.exists(path):
            st = os.stat(path)
            stat_part = f"pdf:{path}|mtime:{int(st.st_mtime)}|size:{int(st.st_size)}"

        kb_part = "kb:none"
        kb_id = int(selected_kb_id) if selected_kb_id else 0
        if kb_id > 0:
            max_doc_updated = (
                await db.execute(
                    select(func.max(Document.updated_at)).where(
                        Document.knowledge_base_id == kb_id
                    )
                )
            ).scalar_one_or_none()
            kb_part = f"kb:{kb_id}|doc_updated:{max_doc_updated.isoformat() if max_doc_updated else 'none'}"

        style_part = self._normalize_spaces(str(style_intent or "auto"))
        theme_part = self._normalize_spaces(str(theme_mode or "light"))
        detail_part = self._normalize_spaces(str(detail_level or "standard"))
        signature = (
            f"{stat_part}|engine:{COMPOSE_ENGINE_VERSION}|component:{COMPOSE_COMPONENT_SCHEMA_VERSION}|"
            f"prompt:{COMPOSE_AGENT_PROMPT_VERSION}|asset:{COMPOSE_ASSET_POLICY_VERSION}|"
            f"{kb_part}|style:{style_part}|theme:{theme_part}|detail:{detail_part}|"
            f"compare:{int(bool(compare_mode))}|cite_tldr:{int(bool(citation_tldr))}"
        )
        return signature[:240]

    @staticmethod
    def _build_style_tokens(
        *,
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
    ) -> Dict[str, Any]:
        style = str(style_intent or "journal").strip().lower()
        if style not in {"journal", "clinical", "preprint", "auto"}:
            style = "auto"
        theme = str(theme_mode or "light").strip().lower()
        if theme not in {"light", "dark"}:
            theme = "light"
        return {
            "style_intent": style,
            "theme_mode": theme,
            "detail_level": detail_level,
            "heading_weight": 700,
            "body_line_height": 1.9 if style in {"journal", "auto"} else (1.82 if style == "clinical" else 1.86),
            "body_font_size": 18 if style in {"journal", "auto"} else 17,
            "panel_contrast": 0.9 if theme == "light" else 1.05,
        }

    def _build_external_image_query(
        self,
        *,
        paper: Paper,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
    ) -> str:
        heading_texts = []
        for node in self._flatten_components(ui_plan.get("components") or []):
            if str(node.get("type") or "") != "SectionHeading":
                continue
            heading_texts.append(self._normalize_spaces(str((node.get("props") or {}).get("text") or "")))
        first_heading = heading_texts[0] if heading_texts else ""
        summary = self._normalize_spaces(str(base_payload.get("summary") or ""))
        title = self._normalize_spaces(str(paper.title or "medical research"))
        query = " ".join(item for item in [title[:120], first_heading[:60], summary[:80], "diagram"] if item).strip()
        return query or "medical education diagram"

    def _build_bbox_hint(self, *, style_cues: Dict[str, Any], quote_text: str) -> Optional[Dict[str, Any]]:
        line_layout = list(style_cues.get("line_layout") or [])
        target = self._normalize_spaces(str(quote_text or "")).lower()
        if not target:
            return None

        best_row: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for row in line_layout[:180]:
            if not isinstance(row, dict):
                continue
            row_text = self._normalize_spaces(str(row.get("text") or "")).lower()
            if not row_text:
                continue
            if str(row.get("column_label") or "").startswith("sidebar"):
                continue
            score = 0.0
            if target in row_text or row_text in target:
                score = 1.0
            else:
                overlap = len(set(target.split()) & set(row_text.split()))
                if overlap > 0:
                    score = min(0.95, 0.18 * overlap)
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is None or best_score < 0.2:
            return None
        return {
            "x0": float(best_row.get("x0") or 0.0),
            "x1": float(best_row.get("x1") or 0.0),
            "top": float(best_row.get("top") or 0.0),
            "bottom": float(best_row.get("bottom") or 0.0),
            "page_width": float(style_cues.get("page_width") or 0.0) or None,
            "page_height": float(style_cues.get("page_height") or 0.0) or None,
        }

    @staticmethod
    def _build_caption_insight(caption: str) -> str:
        text = str(caption or "").strip()
        if not text:
            return "该图表用于补充论文当前页面的关键论点。"
        return f"AI解读：该图注强调“{text[:80]}”，建议结合对应证据锚点核对结论。"

    @staticmethod
    def _build_compare_insights_stub(summary: str) -> List[Dict[str, str]]:
        seed = str(summary or "").strip()
        if not seed:
            return [
                {"title": "共识点", "content": "当前页与知识库文献在核心任务定义上具备可比性。"},
                {"title": "差异点", "content": "实验设置和样本边界可能不同，建议查看方法章节细节。"},
            ]
        return [
            {"title": "共识点", "content": f"当前页结论与相关文献在“{seed[:40]}”上存在重合趋势。"},
            {"title": "差异点", "content": "建议对比样本规模、评价指标与数据来源，确认结论迁移边界。"},
        ]

    @staticmethod
    def _build_link_tldr(*, href: str, label: str, paper: Paper) -> str:
        normalized_href = str(href or "").strip().lower()
        normalized_label = str(label or "").strip()
        if "doi.org" in normalized_href or str(paper.doi or "").lower() in normalized_href:
            return "TL;DR：该链接是论文正式标识入口，可用于快速核对题目、期刊与年份信息。"
        if "arxiv.org" in normalized_href:
            return "TL;DR：该链接可查看预印本版本，适合核查方法细节和补充材料。"
        return f"TL;DR：{normalized_label or '该资源'}用于补充当前页上下文，建议结合证据锚点阅读。"

    @staticmethod
    def _normalize_theme_mode(raw: Optional[str]) -> str:
        value = str(raw or "light").strip().lower()
        return value if value in {"light", "dark"} else "light"

    @staticmethod
    def _normalize_detail_level(raw: Optional[str]) -> str:
        value = str(raw or "standard").strip().lower()
        return value if value in {"concise", "standard", "deep"} else "standard"

    def _search_external_images_sync(self, query: str, limit: int) -> List[Dict[str, Any]]:
        max_results = max(1, min(int(limit), 2))
        try:
            from duckduckgo_search import DDGS
        except Exception:
            return []

        allowed_domains = {
            "upload.wikimedia.org",
            "images.unsplash.com",
            "images.pexels.com",
            "cdn.pixabay.com",
        }
        candidates: List[Dict[str, Any]] = []
        try:
            with DDGS() as ddgs:
                for item in ddgs.images(query, max_results=12):
                    if not isinstance(item, dict):
                        continue
                    image_url = str(item.get("image") or item.get("thumbnail") or "").strip()
                    source_url = str(item.get("url") or "").strip()
                    if not self._is_safe_http_url(image_url):
                        continue
                    parsed = urlparse(image_url)
                    domain = parsed.netloc.lower()
                    if domain not in allowed_domains:
                        continue
                    if not source_url:
                        source_url = image_url
                    license_text = str(item.get("license") or "unknown").strip() or "unknown"
                    caption = self._normalize_spaces(str(item.get("title") or query))
                    candidates.append(
                        {
                            "image_url": image_url,
                            "source_url": source_url,
                            "source_domain": domain,
                            "license": license_text,
                            "caption": caption[:180],
                            "why_relevant": "supplemental visual aid for page understanding",
                        }
                    )
                    if len(candidates) >= max_results:
                        break
        except Exception as exc:
            logger.debug(f"[ReaderComposeService] external image search failed: {exc}")
            return []
        return candidates

    @staticmethod
    def _is_safe_http_url(raw: str) -> bool:
        value = str(raw or "").strip()
        if not value:
            return False
        if not re.match(r"^https?://", value, flags=re.IGNORECASE):
            return False
        lowered = value.lower()
        if lowered.startswith("javascript:") or lowered.startswith("data:"):
            return False
        return True

    @staticmethod
    def _flatten_components(components: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        stack = list(components or [])
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            output.append(node)
            children = node.get("children")
            if isinstance(children, list) and children:
                stack[0:0] = children
        return output

    @staticmethod
    def _normalize_spaces(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _has_broken_words(text: str) -> bool:
        value = str(text or "")
        if re.search(r"\b[a-z]+-\s+[a-z]+\b", value):
            return True
        if re.search(r"\b(?:of|for|to|in|and|with)[A-Z][a-z]{3,}\b", value):
            return True
        return False

    def _repair_text_artifacts(self, text: str) -> str:
        value = str(text or "")
        value = re.sub(r"([A-Za-z]{2,})-\s+([a-z]{2,})", r"\1\2", value)
        value = re.sub(r"\b(of|for|to|in|and|with)([A-Z][a-z]{3,})", r"\1 \2", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _repair_heading_text(self, text: str) -> str:
        value = self._normalize_spaces(text)
        value = value.replace("RESEA RCH", "RESEARCH")
        value = value.replace("AUTH OR", "AUTHOR")
        return value

    def _infer_heading_level(self, text: str) -> int:
        match = re.match(r"^(\d+(?:\.\d+)*)\s+", str(text or ""))
        if not match:
            return 1
        return max(1, min(4, len(match.group(1).split("."))))

    def _looks_like_sidebar_text(self, text: str) -> bool:
        normalized = self._normalize_spaces(text).lower()
        if not normalized:
            return False
        if any(pattern in normalized for pattern in _SIDEBAR_TEXT_PATTERNS):
            return True
        if normalized.startswith("plos digital health"):
            return True
        return False

    def _detect_sidebar_leak(self, paragraph_nodes: Sequence[Dict[str, Any]]) -> bool:
        for node in paragraph_nodes:
            text = str((node.get("props") or {}).get("text") or "")
            if self._looks_like_sidebar_text(text):
                return True
        return False

    def _check_title_integrity(self, nodes: Sequence[Dict[str, Any]], base_payload: Dict[str, Any]) -> bool:
        header_nodes = [item for item in nodes if str(item.get("type") or "") == "PaperHeaderCard"]
        if not header_nodes:
            return False
        title = self._normalize_spaces(str((header_nodes[0].get("props") or {}).get("title") or ""))
        if len(title) < 12:
            return False
        blocks = list(base_payload.get("blocks") or [])
        expected = [
            self._normalize_spaces(str(item.get("text") or "")).lower()
            for item in blocks
            if str(item.get("kind") or "") == "heading"
        ]
        if not expected:
            return True
        first_heading = expected[0]
        if first_heading and first_heading in title.lower():
            return True
        words = [w for w in re.findall(r"[A-Za-z]{4,}", first_heading) if w]
        if not words:
            return True
        hits = sum(1 for w in words[:6] if w.lower() in title.lower())
        return hits >= 2

    @staticmethod
    def _normalize_latency_budget(raw: Optional[int]) -> int:
        try:
            value = int(raw) if raw is not None else DEFAULT_LATENCY_BUDGET_MS
        except Exception:
            value = DEFAULT_LATENCY_BUDGET_MS
        return max(1200, min(value, 25000))

    @staticmethod
    def _normalize_quality_target(raw: Optional[float]) -> float:
        try:
            value = float(raw) if raw is not None else DEFAULT_QUALITY_TARGET
        except Exception:
            value = DEFAULT_QUALITY_TARGET
        return max(0.6, min(value, 0.97))

    async def _read_payload_from_db(
        self,
        *,
        db: AsyncSession,
        paper_id: int,
        page: int,
        source_signature: str,
    ) -> Optional[Dict[str, Any]]:
        stmt = select(PaperReaderPageCache).where(
            and_(
                PaperReaderPageCache.paper_id == int(paper_id),
                PaperReaderPageCache.page == int(page),
                PaperReaderPageCache.source_signature == str(source_signature),
            )
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if not row:
            return None
        if not isinstance(row.payload_json, dict):
            return None
        return dict(row.payload_json)

    async def _upsert_payload_to_db(
        self,
        *,
        db: AsyncSession,
        paper_id: int,
        page: int,
        source_signature: str,
        parser_version: str,
        build_mode: str,
        structure_confidence: float,
        payload: Dict[str, Any],
    ) -> None:
        stmt = select(PaperReaderPageCache).where(
            and_(
                PaperReaderPageCache.paper_id == int(paper_id),
                PaperReaderPageCache.page == int(page),
                PaperReaderPageCache.source_signature == str(source_signature),
            )
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = PaperReaderPageCache(
                paper_id=int(paper_id),
                page=int(page),
                source_signature=str(source_signature),
            )
            db.add(row)
        row.parser_version = str(parser_version)
        row.build_mode = str(build_mode)
        row.structure_confidence = max(0.0, min(1.0, float(structure_confidence)))
        row.payload_json = dict(payload)
        await db.commit()

    async def _get_redis_client(self):
        if redis_async is None:
            return None
        if self._redis_client is not None:
            return self._redis_client
        redis_url = (getattr(settings, "redis_url", "") or "").strip()
        if not redis_url:
            return None
        try:
            self._redis_client = redis_async.from_url(redis_url, decode_responses=True)
            return self._redis_client
        except Exception as exc:
            logger.warning(f"[ReaderComposeService] redis init failed: {exc}")
            self._redis_client = None
            return None

    async def _read_payload_from_redis(self, key: str) -> Optional[Dict[str, Any]]:
        client = await self._get_redis_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
            if not raw:
                return None
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    async def _write_payload_to_redis(self, key: str, payload: Dict[str, Any]) -> None:
        client = await self._get_redis_client()
        if client is None:
            return
        try:
            await client.set(
                key,
                json.dumps(payload, ensure_ascii=False),
                ex=max(1, int(REDIS_TTL_SECONDS)),
            )
        except Exception as exc:
            logger.warning(f"[ReaderComposeService] redis set failed: {exc}")

    async def _acquire_lock(self, lock_key: str) -> Optional[str]:
        client = await self._get_redis_client()
        if client is None:
            return None
        token = uuid.uuid4().hex
        try:
            acquired = await client.set(lock_key, token, ex=max(1, LOCK_TTL_SECONDS), nx=True)
            if acquired:
                return token
            return None
        except Exception:
            return None

    async def _release_lock(self, lock_key: str, token: str) -> None:
        client = await self._get_redis_client()
        if client is None:
            return
        try:
            current = await client.get(lock_key)
            if current == token:
                await client.delete(lock_key)
        except Exception:
            pass

    @staticmethod
    def _signature_hash(signature: str) -> str:
        return hashlib.sha256((signature or "").encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _cache_key(*, paper_id: int, page: int, sig_hash: str) -> str:
        return f"{REDIS_KEY_PREFIX}:{int(paper_id)}:{int(page)}:{sig_hash}"

    @staticmethod
    def _lock_key(*, paper_id: int, page: int) -> str:
        return f"{REDIS_LOCK_PREFIX}:{int(paper_id)}:{int(page)}"

    @staticmethod
    def _with_cache_meta(
        payload: Dict[str, Any],
        *,
        cache_hit: bool,
        cache_layer: str,
    ) -> Dict[str, Any]:
        cloned = dict(payload)
        cloned["cache_hit"] = bool(cache_hit)
        cloned["cache_layer"] = str(cache_layer)
        return cloned


_literature_reader_compose_service = LiteratureReaderComposeService()


def get_literature_reader_compose_service() -> LiteratureReaderComposeService:
    return _literature_reader_compose_service
