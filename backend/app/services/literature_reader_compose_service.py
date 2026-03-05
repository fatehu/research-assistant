"""
Composed literature reader service.

This service builds and validates composed reader UI payloads.
It orchestrates layout planning, quality checks, and fallback strategies.
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
from openai import AsyncOpenAI
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.knowledge import Document, KnowledgeBase
from app.models.literature import Paper, PaperReaderComponentOverlay, PaperReaderPageCache
from app.services.literature_reader_service import get_literature_reader_service
from app.services.llm_service import get_llm_service
from app.services.reader_component_contract_service import get_reader_component_contract_service
from app.services.reader_compose_agent_runtime import get_reader_compose_agent_runtime
from app.services.reader_multimodal_layout_service import ReaderMultimodalLayoutService
from app.services.reader_single_agent_controller import (
    ReaderSingleAgentController,
    parse_json_dict_from_model_text,
)
from app.services.reader_panel_plan_agent_service import get_reader_panel_plan_agent_service
from app.services.reader_single_agent_validator import ReaderSingleAgentValidator
from app.services.render_pipeline_contract import (
    CanonicalAtomBundle,
    LayoutDigestBundle,
    RenderPipelineContractError,
    build_canonical_atom_bundle,
    build_deterministic_baseline_slots,
    build_docmind_layout_digest,
    enforce_minimal_gates,
    materialize_stage2_plan,
)
from app.services.status_event_bus import build_status_channel_for_user, publish_status_event

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover
    redis_async = None


COMPOSE_ENGINE_VERSION = "reader_compose_v3"
COMPOSE_COMPONENT_SCHEMA_VERSION = "reader_components_v1"
COMPOSE_AGENT_PROMPT_VERSION = "reader_compose_prompt_v2"
COMPOSE_ASSET_POLICY_VERSION = "reader_asset_policy_v1"
COMPOSE_LAYOUT_SCHEMA_VERSION = "layout_schema_v2"
SIMPLIFIED_PIPELINE_VERSION_DEFAULT = "simplified_v2"
PIPELINE_MODE_LEGACY = "legacy"
PIPELINE_MODE_SINGLE_AGENT_V2 = "single_agent_v2"

DEFAULT_QUALITY_TARGET = 0.86
MAX_LATENCY_BUDGET_MS = max(
    1200,
    int(getattr(settings, "reader_compose_latency_budget_max_ms", 600000) or 600000),
)
DEFAULT_LATENCY_BUDGET_MS = max(
    1200,
    min(int(getattr(settings, "reader_compose_latency_budget_ms", 20000) or 20000), MAX_LATENCY_BUDGET_MS),
)
DEFAULT_MAX_ITERATIONS = max(6, int(getattr(settings, "literature_agent_max_iterations", 14) or 14))
LOW_CONFIDENCE_MAX_ITERATIONS = min(24, max(DEFAULT_MAX_ITERATIONS + 4, 12))

REDIS_TTL_SECONDS = 24 * 3600
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 6.0
LOCK_POLL_INTERVAL_SECONDS = 0.22
REDIS_KEY_PREFIX = "lit:reader:compose:v1"
REDIS_LOCK_PREFIX = "lit:reader:compose:lock:v1"
CLEANUP_LOCK_KEY = "lit:reader:compose:cleanup:lock"
LEGACY_CACHE_SCAN_MATCH = "lit:reader:compose*:*"

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
    "ContextRail",
    "CitationCard",
    "EquationBlock",
    "MethodologyCard",
    "CalloutBox",
    "AbstractCard",
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

_GENERIC_HEADING_MARKERS = {
    "research article",
    "article",
    "open access",
    "author summary",
    "plos digital health",
}

SIMPLIFIED_ALLOWED_COMPONENTS: List[str] = [
    "SectionHeading",
    "ParagraphProse",
    "ListBlock",
    "ContextRail",
    "FigurePanel",
    "TablePanel",
    "KeyTakeaways",
    "AnswerCard",
    "CitationLinks",
    "InlineQuerySlot",
    "CitationCard",
    "EquationBlock",
    "MethodologyCard",
    "CalloutBox",
    "AbstractCard",
]

INLINE_QUERY_SUPPORTED_NODE_TYPES = {
    "ParagraphProse",
    "SectionHeading",
    "ListBlock",
    "FigurePanel",
    "TablePanel",
    "KeyTakeaways",
    "InlineQuerySlot",
}


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
        self._mm_layout_service = ReaderMultimodalLayoutService()
        self._component_contract_service = get_reader_component_contract_service()
        self._compose_agent_runtime = get_reader_compose_agent_runtime()
        self._single_agent_validator = ReaderSingleAgentValidator()
        self._single_agent_controller = ReaderSingleAgentController(
            validator=self._single_agent_validator,
            max_steps=max(1, int(getattr(settings, "reader_agent_max_steps", 12) or 12)),
            max_repair_rounds=max(0, int(getattr(settings, "reader_agent_max_repair_rounds", 2) or 2)),
        )
        self._panel_plan_agent = get_reader_panel_plan_agent_service()

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _pipeline_version(self) -> str:
        token = str(getattr(settings, "reader_pipeline_version", SIMPLIFIED_PIPELINE_VERSION_DEFAULT) or "").strip()
        return token or SIMPLIFIED_PIPELINE_VERSION_DEFAULT

    def _pipeline_mode(self) -> str:
        explicit = str(getattr(settings, "reader_pipeline_mode", PIPELINE_MODE_SINGLE_AGENT_V2) or "").strip().lower()
        if explicit in {PIPELINE_MODE_LEGACY, PIPELINE_MODE_SINGLE_AGENT_V2}:
            return explicit
        return PIPELINE_MODE_SINGLE_AGENT_V2

    @staticmethod
    def _parse_int_allowlist(raw: str) -> set[int]:
        values: set[int] = set()
        text = str(raw or "").strip()
        if not text:
            return values
        for part in text.split(","):
            token = part.strip()
            if not token:
                continue
            if token.isdigit():
                values.add(int(token))
        return values

    def _is_single_agent_v2_enabled(self, *, paper_id: int, page: int) -> bool:
        _ = (paper_id, page)
        return self._pipeline_mode() == PIPELINE_MODE_SINGLE_AGENT_V2

    def _should_rebuild_cached_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        status_value = str(payload.get("status") or "").strip().lower()
        if status_value != "fallback":
            return False
        degraded_reason = str(payload.get("degraded_reason") or "").strip().lower()
        build_mode = str(payload.get("build_mode") or "").strip().lower()
        gate = payload.get("minimal_gate_report")
        if not isinstance(gate, dict):
            return False
        used_atom_count = self._safe_int(gate.get("used_atom_count"), 0)
        usable_atom_count = self._safe_int(gate.get("usable_atom_count"), 0)
        # Auto-heal only when cached simplified fallback clearly dropped all usable atoms.
        if usable_atom_count <= 0 or used_atom_count > 0:
            return False
        if degraded_reason == "simplified_pipeline":
            return True
        if build_mode == "compose_agent_simplified":
            return True
        return False

    async def _delete_payload_from_redis(self, key: str) -> None:
        client = await self._get_redis_client()
        if client is None:
            return
        try:
            await client.delete(key)
        except Exception:
            return

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
        max_iterations: Optional[int] = None,
        style_intent: Optional[str] = None,
        theme_mode: Optional[str] = None,
        detail_level: Optional[str] = None,
        compare_mode: Optional[bool] = None,
        citation_tldr: Optional[bool] = None,
        publish_ready_event_enabled: bool = False,
    ) -> Tuple[Dict[str, Any], ReaderComposeBuildMeta]:
        page_num = max(1, int(page))
        force_refresh = bool(force_refresh or regenerate)
        pipeline_mode = self._pipeline_mode()
        pipeline_version = self._pipeline_version()
        latency_budget = self._normalize_latency_budget(latency_budget_ms)
        quality_goal = self._normalize_quality_target(quality_target)
        normalized_theme = self._normalize_theme_mode(theme_mode)
        normalized_detail = self._normalize_detail_level(detail_level)
        use_compare_mode = bool(compare_mode)
        use_citation_tldr = bool(citation_tldr)
        normalized_max_iterations = self._normalize_max_iterations(max_iterations)

        source_signature = await self._build_source_signature(
            db=db,
            user_id=int(user_id),
            paper=paper,
            selected_kb_id=selected_kb_id,
            style_intent=style_intent,
            theme_mode=normalized_theme,
            detail_level=normalized_detail,
            compare_mode=use_compare_mode,
            citation_tldr=use_citation_tldr,
            max_iterations=normalized_max_iterations,
        )
        sig_hash = self._signature_hash(source_signature)
        redis_key = self._cache_key(
            user_id=int(user_id),
            paper_id=int(paper.id),
            page=page_num,
            sig_hash=sig_hash,
            pipeline_mode=pipeline_mode,
            pipeline_version=pipeline_version,
        )
        lock_key = self._lock_key(
            user_id=int(user_id),
            paper_id=int(paper.id),
            page=page_num,
            pipeline_mode=pipeline_mode,
            pipeline_version=pipeline_version,
        )

        if not force_refresh:
            cached_payload = await self._read_payload_from_redis(redis_key)
            if isinstance(cached_payload, dict):
                if self._should_rebuild_cached_payload(cached_payload):
                    logger.info(
                        "[ReaderComposeService] skip stale fallback redis cache and rebuild "
                        f"paper={paper.id} page={page_num} reason=simplified_pipeline_no_atoms"
                    )
                    await self._delete_payload_from_redis(redis_key)
                else:
                    payload = self._with_cache_meta(cached_payload, cache_hit=True, cache_layer="redis")
                    payload = await self._apply_overlay_for_user(
                        db=db,
                        user_id=int(user_id),
                        paper_id=int(paper.id),
                        page=page_num,
                        source_signature=source_signature,
                        payload=payload,
                    )
                    payload = self._ensure_payload_contract(page=page_num, payload=payload)
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
                if self._should_rebuild_cached_payload(cached_row):
                    logger.info(
                        "[ReaderComposeService] skip stale fallback db cache and rebuild "
                        f"paper={paper.id} page={page_num} reason=simplified_pipeline_no_atoms"
                    )
                else:
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
                    payload = self._ensure_payload_contract(page=page_num, payload=payload)
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
            while waited < LOCK_WAIT_SECONDS and lock_token is None:
                await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)
                waited += LOCK_POLL_INTERVAL_SECONDS
                lock_token = await self._acquire_lock(lock_key)
                if lock_token is not None:
                    break
                if force_refresh:
                    continue
                cached_payload = await self._read_payload_from_redis(redis_key)
                if isinstance(cached_payload, dict):
                    if self._should_rebuild_cached_payload(cached_payload):
                        await self._delete_payload_from_redis(redis_key)
                    else:
                        payload = self._with_cache_meta(cached_payload, cache_hit=True, cache_layer="redis")
                        payload = await self._apply_overlay_for_user(
                            db=db,
                            user_id=int(user_id),
                            paper_id=int(paper.id),
                            page=page_num,
                            source_signature=source_signature,
                            payload=payload,
                        )
                        payload = self._ensure_payload_contract(page=page_num, payload=payload)
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

            if lock_token is None and force_refresh:
                fallback_payload = await self._build_force_refresh_timeout_fallback_payload(
                    db=db,
                    user_id=int(user_id),
                    paper=paper,
                    page=page_num,
                    selected_kb_id=selected_kb_id,
                    style_intent=style_intent,
                    theme_mode=normalized_theme,
                    detail_level=normalized_detail,
                    compare_mode=use_compare_mode,
                    source_signature=source_signature,
                    pipeline_version=pipeline_version,
                )
                fallback_payload = self._with_cache_meta(fallback_payload, cache_hit=False, cache_layer="none")
                fallback_payload = await self._apply_overlay_for_user(
                    db=db,
                    user_id=int(user_id),
                    paper_id=int(paper.id),
                    page=page_num,
                    source_signature=source_signature,
                    payload=fallback_payload,
                )
                fallback_payload = self._ensure_payload_contract(page=page_num, payload=fallback_payload)
                quality_report = fallback_payload.get("quality_report") or {}
                return fallback_payload, ReaderComposeBuildMeta(
                    cache_hit=False,
                    cache_layer="none",
                    build_mode=str(fallback_payload.get("build_mode") or "compose_agent_simplified"),
                    source_signature=source_signature,
                    source_sig_hash=sig_hash,
                    iterations=int(quality_report.get("iterations") or 0),
                    degraded=True,
                    stop_reason="force_refresh_lock_timeout",
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
            simplified_enabled = self._is_single_agent_v2_enabled(
                paper_id=int(paper.id),
                page=page_num,
            )
            if simplified_enabled:
                try:
                    simplified_result = await self._build_single_agent_v2_result(
                        db=db,
                        user_id=int(user_id),
                        paper=paper,
                        page=page_num,
                        base_payload=base_payload,
                        style_intent=style_intent,
                        theme_mode=normalized_theme,
                        detail_level=normalized_detail,
                        compare_mode=use_compare_mode,
                        latency_budget_ms=latency_budget,
                        selected_kb_id=selected_kb_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "[ReaderComposeService] single_agent_v2 controller failed; "
                        "fallback to deterministic simplified baseline "
                        f"paper={paper.id} page={page_num}: {exc}"
                    )
                    simplified_result = await self._build_simplified_pipeline_result_legacy(
                        db=db,
                        user_id=int(user_id),
                        paper=paper,
                        page=page_num,
                        base_payload=base_payload,
                        style_intent=style_intent,
                        theme_mode=normalized_theme,
                        detail_level=normalized_detail,
                        compare_mode=use_compare_mode,
                        latency_budget_ms=latency_budget,
                        selected_kb_id=selected_kb_id,
                    )
                base_payload = dict(simplified_result.get("base_payload") or base_payload)
                loop_result = dict(simplified_result.get("loop_result") or {})
                assets = list(simplified_result.get("assets") or [])
            else:
                base_payload = await self._apply_multimodal_layout_assist(
                    paper=paper,
                    page=page_num,
                    base_payload=base_payload,
                )
                takeaways = await self._build_takeaways_with_neighbor_context(
                    db=db,
                    user_id=int(user_id),
                    paper=paper,
                    page=page_num,
                    selected_kb_id=selected_kb_id,
                    current_payload=base_payload,
                    detail_level=normalized_detail,
                )
                if takeaways:
                    base_payload = dict(base_payload)
                    base_payload["takeaways"] = takeaways

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
                    max_iterations=normalized_max_iterations,
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
            validation_report = self._build_validation_report(
                quality_report=quality_report,
                minimal_gate_report=dict(base_payload.get("minimal_gate_report") or {}),
            )
            main_block_ids, aux_block_ids = self._partition_main_aux_block_ids(
                page=page_num,
                base_payload=base_payload,
                ui_plan=dict(loop_result.get("ui_plan") or {}),
            )
            status_value = "done" if bool(validation_report.get("passed")) else "fallback"
            degraded_reason = ""
            if status_value != "done":
                degraded_reason = str(quality_report.get("stop_reason") or "validator_non_converged").strip()
                if not degraded_reason or degraded_reason == "unknown":
                    degraded_reason = "validator_non_converged"

            payload: Dict[str, Any] = {
                "paper_id": int(paper.id),
                "page": page_num,
                "status": status_value,
                "degraded_reason": degraded_reason,
                "pipeline_version": pipeline_version,
                "engine_version": COMPOSE_ENGINE_VERSION,
                "source_signature": source_signature,
                "build_mode": str(loop_result.get("build_mode") or "compose_agent"),
                "ui_plan": dict(loop_result.get("ui_plan") or {}),
                "assets": assets,
                "quality_report": quality_report,
                "iteration_trace": list(loop_result.get("iteration_trace") or []),
                "main_block_ids": main_block_ids,
                "aux_block_ids": aux_block_ids,
                "validation_report": validation_report,
                "asset_policy": {
                    "pdf_first": True,
                    "web_fallback": bool(getattr(settings, "reader_external_image_enabled", False)),
                    "max_external_images": 2,
                    "version": COMPOSE_ASSET_POLICY_VERSION,
                },
                "layout_channels": dict(base_payload.get("layout_channels") or {}),
                "mm_assist_meta": dict(base_payload.get("mm_assist_meta") or {}),
                "parser_chain_meta": dict(base_payload.get("parser_chain_meta") or {}),
                "docmind_meta": dict(base_payload.get("docmind_meta") or {}),
                "docmind_structure": dict(base_payload.get("docmind_structure") or {}),
                "page_structure_v3": dict(base_payload.get("page_structure_v3") or {}),
                "canonical_atoms": dict(base_payload.get("canonical_atoms") or {}),
                "atom_semantics": dict(base_payload.get("atom_semantics") or {}),
                "deterministic_page_skeleton": dict(base_payload.get("deterministic_page_skeleton") or {}),
                "stage2_style_plan": dict(base_payload.get("stage2_style_plan") or {}),
                "minimal_gate_report": dict(base_payload.get("minimal_gate_report") or {}),
                "candidate_ranking": dict(base_payload.get("candidate_ranking") or {}),
                "repair_report": dict(base_payload.get("repair_report") or {}),
                "segment_id_map": dict(base_payload.get("segment_id_map") or {}),
                "stage1_structural_annotations": dict(base_payload.get("stage1_structural_annotations") or {}),
                "stage2_design_layout": dict(base_payload.get("stage2_design_layout") or {}),
                "pipeline_contract_meta": dict(base_payload.get("pipeline_contract_meta") or {}),
                "qwen_layout_plan_v2": dict(base_payload.get("qwen_layout_plan_v2") or {}),
                "qwen_plan_meta": dict(base_payload.get("qwen_plan_meta") or {}),
                "layout_advice_v2": dict(base_payload.get("layout_advice_v2") or {}),
                "layout_advice_v3": dict(base_payload.get("layout_advice_v3") or {}),
                "layout_advice_meta": dict(base_payload.get("layout_advice_meta") or {}),
                "mm_parser_meta": dict(base_payload.get("mm_parser_meta") or {}),
                "assembly_meta": {
                    "used": bool(((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_used")),
                    "model": str((((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_model") or "")),
                    "fallback_reason": str((((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_fallback_reason") or "")),
                    "ui_ops_count": int((((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_ui_ops_count") or 0)),
                    "agent_tool_call_count": int((((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_agent_tool_call_count") or 0)),
                    "agent_trace_count": int((((loop_result.get("ui_plan") or {}).get("trace_meta") or {}).get("assembly_agent_trace_count") or 0)),
                },
                "component_registry_version": "reader_components_v2",
                "segment_map": dict(base_payload.get("segment_map") or {}),
                "segment_map_meta": dict(base_payload.get("segment_map_meta") or {}),
                "node_gate_report": dict(loop_result.get("node_gate_report") or {}),
                "toc_quality": float(base_payload.get("toc_quality") or 0.0),
                "overlay_applied": False,
                "overlay_count": 0,
                "generated_at": datetime.utcnow().isoformat(),
            }

            payload = self._ensure_payload_contract(page=page_num, payload=payload)
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
            payload = self._ensure_payload_contract(page=page_num, payload=payload)
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

    async def _build_simplified_pipeline_result(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        latency_budget_ms: int,
        selected_kb_id: Optional[int],
    ) -> Dict[str, Any]:
        return await self._build_single_agent_v2_result(
            db=db,
            user_id=user_id,
            paper=paper,
            page=page,
            base_payload=base_payload,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
            latency_budget_ms=latency_budget_ms,
            selected_kb_id=selected_kb_id,
        )

    async def _build_single_agent_v2_result(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        latency_budget_ms: int,
        selected_kb_id: Optional[int],
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        payload = dict(base_payload or {})
        docmind_blocks, layout_to_block_ids = self._collect_docmind_blocks_for_single_agent(
            page=page,
            base_payload=payload,
        )
        if not docmind_blocks:
            logger.warning(
                "[ReaderComposeService] single_agent_v2 detected empty DocMind layouts on first pass; "
                f"forcing page payload refresh paper={paper.id} page={page}"
            )
            try:
                refreshed_payload, _ = await self._reader_service.build_or_get_page_payload(
                    db=db,
                    user_id=int(user_id),
                    paper=paper,
                    page=int(page),
                    selected_kb_id=selected_kb_id,
                    force_refresh=True,
                    style_hint=None,
                    prefer_agent=False,
                    publish_ready_event_enabled=False,
                )
            except Exception as exc:
                logger.warning(
                    "[ReaderComposeService] single_agent_v2 forced refresh failed; "
                    f"paper={paper.id} page={page}: {type(exc).__name__}: {exc}"
                )
            else:
                if isinstance(refreshed_payload, dict):
                    payload = dict(refreshed_payload)
                    docmind_blocks, layout_to_block_ids = self._collect_docmind_blocks_for_single_agent(
                        page=page,
                        base_payload=payload,
                    )
        if not docmind_blocks:
            # Expected on some pages where DocMind did not emit layout rows.
            # Keep this path non-exceptional to avoid expensive traceback logging and retry churn.
            logger.warning(
                "[ReaderComposeService] single_agent_v2 degraded to deterministic baseline "
                f"paper={paper.id} page={page} reason=docmind_layout_empty"
            )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            ui_plan = self._build_initial_ui_plan(
                paper=paper,
                page=int(page),
                base_payload=payload,
                style_intent=style_intent,
                theme_mode=theme_mode,
                detail_level=detail_level,
                compare_mode=compare_mode,
            )
            quality_report = {
                "overall": 0.0,
                "hard_constraints_passed": False,
                "validation_errors": ["docmind_layout_empty"],
                "quality_target": 0.0,
                "elapsed_ms": elapsed_ms,
                "iterations": 0,
                "degraded": True,
                "stop_reason": "docmind_layout_empty",
                "schema_valid": False,
                "whitelist_valid": False,
                "ownership_unchanged": False,
                "full_coverage": False,
                "non_empty_plan_for_non_empty_input": bool(list((ui_plan or {}).get("components") or [])),
                "source_text_immutable": False,
                "pipeline_latency_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            payload["repair_report"] = {
                "rounds": 0,
                "used": False,
                "reason": "docmind_layout_empty",
                "failed_gates": [
                    "id_integrity",
                    "full_coverage",
                    "whitelist_only",
                    "ownership_unchanged",
                    "source_text_immutable",
                ],
                "step_metrics": [],
            }
            payload["qwen_plan_meta"] = {
                "used": False,
                "reason": "docmind_layout_empty",
                "model": str(getattr(settings, "reader_agent_model", "qwen-3.5-plus") or "qwen-3.5-plus"),
                "steps_executed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "pipeline_version": self._pipeline_version(),
            }
            payload["minimal_gate_report"] = {
                "passed": False,
                "schema_valid": False,
                "whitelist_valid": False,
                "layout_contract": False,
                "ownership_unchanged": False,
                "full_coverage": False,
                "non_empty_plan_for_non_empty_input": bool(list((ui_plan or {}).get("components") or [])),
                "source_text_immutable": False,
                "used_atom_count": 0,
                "usable_atom_count": 0,
            }
            payload["pipeline_contract_meta"] = {
                **dict(payload.get("pipeline_contract_meta") or {}),
                "used": True,
                "pipeline": "single_agent_v2",
                "elapsed_ms": elapsed_ms,
                "degraded_reason": "docmind_layout_empty",
            }
            return {
                "base_payload": payload,
                "loop_result": {
                    "ui_plan": ui_plan,
                    "quality_report": quality_report,
                    "node_gate_report": {},
                    "iteration_trace": [],
                    "iterations": 0,
                    "degraded": True,
                    "stop_reason": "docmind_layout_empty",
                    "build_mode": "compose_agent_single_agent_v2",
                },
                "assets": [],
            }

        rendered_page_image = ""
        pdf_path = self._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
            user_id=int(paper.user_id),
            paper_id=int(paper.id),
            paper_title=paper.title,
            paper_pdf_path=paper.pdf_path,
        )
        if pdf_path and os.path.exists(pdf_path):
            try:
                mm_payload = await self._mm_layout_service.build_mm_prompt_payload(
                    pdf_path=pdf_path,
                    page=int(page),
                    base_payload=payload,
                ) or {}
                rendered_page_image = str(mm_payload.get("image_data_url") or "")
            except Exception as exc:
                logger.warning(
                    f"[ReaderComposeService] build_mm_prompt_payload failed for single_agent_v2 "
                    f"paper={paper.id} page={page}: {exc}"
                )

        agent_result = await self._panel_plan_agent.run(
            docmind_blocks=docmind_blocks,
            rendered_page_image=rendered_page_image,
            component_whitelist=list(SIMPLIFIED_ALLOWED_COMPONENTS),
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            max_rounds=max(1, int(getattr(settings, "reader_agent_max_steps", 12) or 12)),
        )
        panel_plan = dict(agent_result.get("panel_plan") or {})
        validation_report = dict(agent_result.get("validation_report") or {})
        repair_report = dict(agent_result.get("repair_report") or {})
        usage = dict(agent_result.get("usage") or {})
        status_value = str(agent_result.get("status") or "fallback").strip().lower()
        degraded_reason = str(agent_result.get("degraded_reason") or "").strip()
        use_controller_fallback = status_value == "fallback" and degraded_reason in {"propose_failed", "model_unavailable"}
        used_layout_ids: List[str] = []
        component_hints: List[Dict[str, Any]] = []

        if use_controller_fallback:
            page_meta = {
                "paper_id": int(paper.id),
                "page": int(page),
                "pipeline_version": self._pipeline_version(),
                "style_intent": str(style_intent or ""),
                "theme_mode": str(theme_mode or ""),
                "detail_level": str(detail_level or ""),
                "compare_mode": bool(compare_mode),
                "selected_kb_id": int(selected_kb_id) if selected_kb_id is not None else None,
            }

            async def _model_infer(system_prompt: str, user_prompt: Dict[str, Any], step: int, phase: str) -> Dict[str, Any]:
                return await self._invoke_single_agent_model(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    rendered_page_image=rendered_page_image,
                    step=step,
                    phase=phase,
                )

            controller_result = await self._single_agent_controller.run(
                page_meta=page_meta,
                docmind_blocks=docmind_blocks,
                rendered_page_image=rendered_page_image,
                component_whitelist=list(SIMPLIFIED_ALLOWED_COMPONENTS),
                model_infer=_model_infer,
            )
            step_result = dict(controller_result.get("step_result") or {})
            validation_report = dict(controller_result.get("validation_report") or {})
            repair_report = dict(controller_result.get("repair_report") or {})
            status_value = str(controller_result.get("status") or "fallback").strip().lower()
            degraded_reason = str(controller_result.get("degraded_reason") or "").strip()

            ui_plan = self._step_result_to_ui_plan(
                page=page,
                step_result=step_result,
                docmind_blocks=docmind_blocks,
                layout_to_block_ids=layout_to_block_ids,
                base_payload=payload,
                style_intent=style_intent,
                theme_mode=theme_mode,
                detail_level=detail_level,
                compare_mode=compare_mode,
            )
            used_layout_ids = [
                str(item.get("layout_id") or "").strip()
                for item in list((step_result.get("classification") or {}).get("items") or [])
                if isinstance(item, dict) and str(item.get("layout_id") or "").strip()
            ]
            component_hints = [
                {
                    "block_ids": [str(x).strip() for x in list((row or {}).get("source_block_ids") or []) if str(x).strip()],
                    "component": str((row or {}).get("component") or ""),
                    "reason": "single_agent_v2",
                }
                for row in list((step_result.get("ui_plan_draft") or {}).get("components") or [])
                if isinstance(row, dict)
            ]
        else:
            ui_plan = self._panel_plan_to_ui_plan(
                page=page,
                panel_plan=panel_plan,
                docmind_blocks=docmind_blocks,
                layout_to_block_ids=layout_to_block_ids,
                base_payload=payload,
                style_intent=style_intent,
                theme_mode=theme_mode,
                detail_level=detail_level,
                compare_mode=compare_mode,
            )
            used_layout_ids = self._collect_source_layout_ids_from_panel_plan(panel_plan=panel_plan)
            component_hints = self._collect_component_hints_from_panel_plan(panel_plan=panel_plan)
            validation_report = self._normalize_panel_validation_report(
                raw_report=validation_report,
                non_empty_plan=bool((ui_plan.get("components") or [])),
                used_count=len(used_layout_ids),
                usable_count=len(docmind_blocks),
            )
        used_layout_ids = list(dict.fromkeys([str(item).strip() for item in used_layout_ids if str(item).strip()]))

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        step_metrics = [
            row
            for row in list(repair_report.get("step_metrics") or [])
            if isinstance(row, dict)
        ]
        total_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        total_completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        if total_tokens <= 0:
            total_prompt_tokens = sum(int(row.get("prompt_tokens") or 0) for row in step_metrics)
            total_completion_tokens = sum(int(row.get("completion_tokens") or 0) for row in step_metrics)
            total_tokens = sum(int(row.get("total_tokens") or 0) for row in step_metrics)

        validation_errors = [str(item) for item in list(validation_report.get("errors") or []) if str(item).strip()]
        failed_gates = ["id_integrity"] if validation_errors else []

        payload["repair_report"] = {
            **repair_report,
            "failed_gates": failed_gates,
        }
        payload["qwen_plan_meta"] = {
            "used": True,
            "reason": "single_agent_v2",
            "model": str(getattr(settings, "reader_agent_model", "qwen-3.5-plus") or "qwen-3.5-plus"),
            "steps_executed": int(repair_report.get("steps_executed") or len(step_metrics)),
            "prompt_tokens": int(total_prompt_tokens),
            "completion_tokens": int(total_completion_tokens),
            "total_tokens": int(total_tokens),
            "pipeline_version": self._pipeline_version(),
        }
        payload["layout_advice_v3"] = {
            "source": "single_agent_v2",
            "ordered_block_ids": used_layout_ids,
            "suggested_components": component_hints,
            "grouping_hints": [],
            "visual_hints": [],
            "notes": [
                f"status={status_value}",
                f"degraded_reason={degraded_reason}" if degraded_reason else "",
            ],
        }
        payload["minimal_gate_report"] = {
            "passed": bool(validation_report.get("passed", False)),
            "schema_valid": bool(validation_report.get("passed", False)),
            "whitelist_valid": bool(validation_report.get("passed", False)),
            "layout_contract": True,
            "ownership_unchanged": True,
            "full_coverage": not any(str(item).startswith("uncovered_layout_ids:") for item in validation_errors),
            "non_empty_plan_for_non_empty_input": bool((ui_plan.get("components") or [])),
            "source_text_immutable": True,
            "used_atom_count": len(used_layout_ids),
            "usable_atom_count": len(docmind_blocks),
        }
        payload["pipeline_contract_meta"] = {
            **dict(payload.get("pipeline_contract_meta") or {}),
            "used": True,
            "pipeline": "single_agent_v2",
            "elapsed_ms": elapsed_ms,
            "validation_report": validation_report,
        }

        quality_report = {
            "overall": 0.94 if status_value == "done" else 0.66,
            "hard_constraints_passed": bool(validation_report.get("passed", False)),
            "validation_errors": validation_errors,
            "quality_target": 0.0,
            "elapsed_ms": elapsed_ms,
            "iterations": int(repair_report.get("steps_executed") or len(step_metrics) or 1),
            "degraded": status_value != "done",
            "stop_reason": degraded_reason or ("single_agent_v2_done" if status_value == "done" else "validator_non_converged"),
            "schema_valid": bool(validation_report.get("passed", False)),
            "whitelist_valid": bool(validation_report.get("passed", False)),
            "ownership_unchanged": True,
            "full_coverage": not any(str(item).startswith("uncovered_layout_ids:") for item in validation_errors),
            "non_empty_plan_for_non_empty_input": bool((ui_plan.get("components") or [])),
            "source_text_immutable": True,
            "pipeline_latency_ms": elapsed_ms,
            "prompt_tokens": int(total_prompt_tokens),
            "completion_tokens": int(total_completion_tokens),
            "total_tokens": int(total_tokens),
        }

        loop_result = {
            "ui_plan": ui_plan,
            "quality_report": quality_report,
            "node_gate_report": {},
            "iteration_trace": [
                {
                    "iteration": int(row.get("step_index") or idx + 1),
                    "ui_plan": ui_plan,
                    "quality_report": quality_report,
                    "phase": str(row.get("phase") or ""),
                    "latency_ms": int(row.get("latency_ms") or 0),
                    "failed_gates": list(row.get("failed_gates") or []),
                }
                for idx, row in enumerate(step_metrics)
            ],
            "iterations": int(repair_report.get("steps_executed") or len(step_metrics) or 1),
            "degraded": status_value != "done",
            "stop_reason": degraded_reason or ("single_agent_v2_done" if status_value == "done" else "validator_non_converged"),
            "build_mode": "compose_agent_single_agent_v2",
        }
        assets = [
            row
            for row in list(payload.get("assets") or [])
            if isinstance(row, dict) and str(row.get("kind") or "") in {"link", "annotation", "image_hint"}
        ]
        return {
            "base_payload": payload,
            "loop_result": loop_result,
            "assets": assets,
        }

    @staticmethod
    def _normalize_panel_validation_report(
        *,
        raw_report: Dict[str, Any],
        non_empty_plan: bool,
        used_count: int,
        usable_count: int,
    ) -> Dict[str, Any]:
        report = dict(raw_report or {})
        errors = [str(item) for item in list(report.get("errors") or []) if str(item).strip()]
        warnings = [str(item) for item in list(report.get("warnings") or []) if str(item).strip()]
        has_unknown = any(item.startswith("unknown_source_layout_id:") for item in errors)
        has_uncovered = any(item.startswith("uncovered_layout_ids:") for item in errors)
        has_component = any(item.startswith("component_not_allowed:") for item in errors)
        has_schema = any(item in {"panels_empty", "source_layout_ids_not_array", "node_id_missing"} for item in errors)

        def gate_row(passed: bool, gate_errors: List[str]) -> Dict[str, Any]:
            return {"passed": bool(passed), "errors": [str(item) for item in gate_errors]}

        gates = {
            "id_integrity": gate_row(not has_unknown and not has_schema, [item for item in errors if item.startswith("unknown_source_layout_id:") or item in {"panels_empty", "source_layout_ids_not_array", "node_id_missing"}]),
            "full_coverage": gate_row(not has_uncovered, [item for item in errors if item.startswith("uncovered_layout_ids:")]),
            "whitelist_only": gate_row(not has_component, [item for item in errors if item.startswith("component_not_allowed:")]),
            "layout_contract": gate_row(True, []),
            "no_drop_blocks": gate_row(not has_uncovered, [item for item in errors if item.startswith("uncovered_layout_ids:")]),
            "ownership_unchanged": gate_row(True, []),
            "non_empty_plan_for_non_empty_input": gate_row(bool(non_empty_plan) or usable_count <= 0, [] if (bool(non_empty_plan) or usable_count <= 0) else ["empty_plan"]),
            "source_text_immutable": gate_row(True, []),
        }

        passed = bool(report.get("passed", False)) and all(bool((row or {}).get("passed")) for row in gates.values())
        if usable_count > 0 and used_count <= 0:
            passed = False
            gates["full_coverage"] = gate_row(False, list(gates["full_coverage"]["errors"]) + ["used_count_zero"])
            if "used_count_zero" not in errors:
                errors.append("used_count_zero")

        return {
            "passed": bool(passed),
            "status": "ok" if passed else "invalid",
            "errors": errors,
            "warnings": warnings,
            "stats": dict(report.get("stats") or {}),
            "gates": gates,
        }

    @staticmethod
    def _collect_source_layout_ids_from_panel_plan(*, panel_plan: Dict[str, Any]) -> List[str]:
        output: List[str] = []
        seen: set[str] = set()
        for panel in list(panel_plan.get("panels") or []):
            if not isinstance(panel, dict):
                continue
            for node in list(panel.get("nodes") or []):
                stack = [node]
                while stack:
                    current = stack.pop(0)
                    if not isinstance(current, dict):
                        continue
                    for src in list(current.get("source_layout_ids") or []):
                        token = str(src or "").strip()
                        if token and token not in seen:
                            seen.add(token)
                            output.append(token)
                    children = current.get("children")
                    if isinstance(children, list) and children:
                        stack = list(children) + stack
        return output

    @staticmethod
    def _collect_component_hints_from_panel_plan(*, panel_plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        hints: List[Dict[str, Any]] = []
        for panel in list(panel_plan.get("panels") or []):
            if not isinstance(panel, dict):
                continue
            for node in list(panel.get("nodes") or []):
                stack = [node]
                while stack:
                    current = stack.pop(0)
                    if not isinstance(current, dict):
                        continue
                    component = str(current.get("component") or "").strip()
                    block_ids = [str(item).strip() for item in list(current.get("source_layout_ids") or []) if str(item).strip()]
                    if component:
                        hints.append({"block_ids": block_ids, "component": component, "reason": "single_agent_v2"})
                    children = current.get("children")
                    if isinstance(children, list) and children:
                        stack = list(children) + stack
        return hints

    def _panel_plan_to_ui_plan(
        self,
        *,
        page: int,
        panel_plan: Dict[str, Any],
        docmind_blocks: Sequence[Dict[str, Any]],
        layout_to_block_ids: Dict[str, List[str]],
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
    ) -> Dict[str, Any]:
        docmind_map = {
            str(row.get("layout_id") or ""): dict(row)
            for row in list(docmind_blocks or [])
            if isinstance(row, dict) and str(row.get("layout_id") or "")
        }

        block_anchor_map: Dict[str, Dict[str, Any]] = {}
        for block in list(base_payload.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            canonical = self._normalize_canonical_block_id(page=page, raw_id=str(block.get("id") or ""))
            if not canonical:
                canonical = str(((block.get("source_anchor") or {}).get("canonical_block_id") or "")).strip()
            if not canonical or canonical in block_anchor_map:
                continue
            anchor = self._normalize_anchor_ref(
                anchor=block.get("source_anchor"),
                page=page,
                quote_text=str(block.get("text") or ""),
            )
            if anchor:
                block_anchor_map[canonical] = anchor

        allowed_displays = {"default", "collapsed", "pinned", "hidden_until_expand"}
        component_map = {
            "ProseBlock": "ParagraphProse",
            "QuoteBlock": "CalloutBox",
            "BulletList": "ListBlock",
            "FigureCard": "FigurePanel",
            "Callout": "CalloutBox",
            "PublicationMetaStrip": "ContextRail",
        }
        order_key = 0.0

        def infer_zone(source_layout_ids: Sequence[str]) -> str:
            aux_types = {"head", "header_line", "side", "footer_line", "foot", "foot_pagenum", "split_line"}
            aux_count = 0
            for layout_id in source_layout_ids:
                row = docmind_map.get(str(layout_id)) or {}
                if str(row.get("type") or "").strip().lower() in aux_types:
                    aux_count += 1
            return "side_context" if aux_count > 0 and aux_count >= max(1, len(source_layout_ids)) else "main_body"

        def fallback_text(source_layout_ids: Sequence[str]) -> str:
            parts: List[str] = []
            for layout_id in source_layout_ids:
                token = str((docmind_map.get(layout_id) or {}).get("source_text") or "").strip()
                if token:
                    parts.append(token)
            return " ".join(parts).strip()

        def normalize_component(raw_component: str) -> str:
            token = str(raw_component or "").strip()
            token = component_map.get(token, token)
            if token in SIMPLIFIED_ALLOWED_COMPONENTS:
                return token
            return "ParagraphProse"

        def normalize_props(component: str, raw_props: Any, source_layout_ids: Sequence[str]) -> Dict[str, Any]:
            props = dict(raw_props) if isinstance(raw_props, dict) else {}
            fb_text = fallback_text(source_layout_ids)
            if component == "ParagraphProse":
                text = str(props.get("text") or fb_text).strip()
                return {"text": text or "[empty]"}
            if component == "SectionHeading":
                text = str(props.get("text") or fb_text).strip() or "Untitled"
                level = int(props.get("level") or 2)
                level = max(1, min(4, level))
                return {"text": text, "level": level}
            if component == "ListBlock":
                items = props.get("items")
                rows = [str(item).strip() for item in list(items or []) if str(item).strip()] if isinstance(items, list) else []
                if not rows and fb_text:
                    rows = [fb_text]
                return {"items": rows}
            if component == "FigurePanel":
                return {
                    "caption": str(props.get("caption") or fb_text).strip(),
                    "image_url": str(props.get("image_url") or props.get("image_src") or "").strip(),
                    "source_label": str(props.get("source_label") or "").strip(),
                    "ai_insight": str(props.get("ai_insight") or "").strip(),
                }
            if component == "ContextRail":
                title = str(props.get("title") or "Context").strip()
                items = props.get("items")
                if not isinstance(items, list):
                    text = fb_text or str(props.get("text") or "").strip()
                    items = [{"text": text}] if text else []
                return {"title": title, "items": items}
            if component == "CalloutBox":
                content = str(props.get("content") or props.get("text") or fb_text).strip()
                callout_type = str(props.get("type") or "info").strip().lower()
                if callout_type not in {"info", "warning", "success", "tip"}:
                    callout_type = "info"
                return {"type": callout_type, "title": str(props.get("title") or "").strip(), "content": content or "[empty]"}
            if component == "AbstractCard":
                return {"text": str(props.get("text") or fb_text).strip() or "[empty]"}
            if component == "CitationCard":
                return {
                    "title": str(props.get("title") or fb_text).strip() or "Citation",
                    "authors": [str(item).strip() for item in list(props.get("authors") or []) if str(item).strip()],
                    "year": props.get("year"),
                    "journal": str(props.get("journal") or "").strip(),
                    "doi": str(props.get("doi") or "").strip(),
                    "abstract_tldr": str(props.get("abstract_tldr") or "").strip(),
                }
            if component == "EquationBlock":
                return {
                    "latex": str(props.get("latex") or props.get("text") or fb_text).strip() or "x = y",
                    "label": str(props.get("label") or "").strip(),
                    "description": str(props.get("description") or "").strip(),
                }
            if component == "MethodologyCard":
                steps = [str(item).strip() for item in list(props.get("steps") or []) if str(item).strip()]
                if not steps and fb_text:
                    steps = [fb_text]
                return {"title": str(props.get("title") or "").strip(), "steps": steps or ["N/A"], "participants": str(props.get("participants") or "").strip(), "tools": [str(item).strip() for item in list(props.get("tools") or []) if str(item).strip()]}
            if component == "TablePanel":
                rows = props.get("rows")
                return {"title": str(props.get("title") or fb_text or "Table").strip(), "rows": rows if isinstance(rows, list) else []}
            return {"text": str(props.get("text") or fb_text).strip() or "[empty]"}

        def convert_node(node: Dict[str, Any], panel_id: str) -> Optional[Dict[str, Any]]:
            nonlocal order_key
            source_layout_ids = [str(item).strip() for item in list(node.get("source_layout_ids") or []) if str(item).strip()]
            component = normalize_component(str(node.get("component") or ""))
            zone_type = infer_zone(source_layout_ids)
            region = "sidebar" if zone_type == "side_context" else "main"
            display = str(node.get("display") or "default").strip().lower()
            if display not in allowed_displays:
                display = "default"
            source_block_ids: List[str] = []
            source_anchor_refs: List[Dict[str, Any]] = []
            for layout_id in source_layout_ids:
                mapped = list(layout_to_block_ids.get(layout_id) or [layout_id])
                for block_id in mapped:
                    block_token = str(block_id).strip()
                    if not block_token or block_token in source_block_ids:
                        continue
                    source_block_ids.append(block_token)
                    anchor = dict(block_anchor_map.get(block_token) or {})
                    if anchor and anchor not in source_anchor_refs:
                        source_anchor_refs.append(anchor)

            order_key += 1.0
            node_id = str(node.get("node_id") or f"{panel_id}_n{int(order_key)}").strip()
            children: List[Dict[str, Any]] = []
            for child in list(node.get("children") or []):
                if not isinstance(child, dict):
                    continue
                converted = convert_node(child, panel_id)
                if converted:
                    children.append(converted)
            return {
                "id": node_id,
                "type": component,
                "props": normalize_props(component, node.get("props"), source_layout_ids),
                "children": children,
                "source_anchor_refs": source_anchor_refs,
                "source_block_ids": source_block_ids,
                "source_atom_ids": source_block_ids,
                "zone_type": zone_type,
                "column_id": region,
                "region": region,
                "display": display,
                "order_key": float(node.get("order_key") or order_key),
                "compat_filled": True,
                "compat_filled_fields": ["zone_type", "column_id", "region", "display", "order_key"],
                "heading_prob": 0.85 if component == "SectionHeading" else 0.0,
                "capabilities": [],
                "actions": [],
            }

        components: List[Dict[str, Any]] = []
        for panel in list(panel_plan.get("panels") or []):
            if not isinstance(panel, dict):
                continue
            panel_id = str(panel.get("panel_id") or f"panel_{len(components) + 1}").strip()
            panel_nodes = [row for row in list(panel.get("nodes") or []) if isinstance(row, dict)]
            title = str(panel.get("title") or "").strip()
            if title:
                title_source = []
                if panel_nodes:
                    title_source = [str(item).strip() for item in list(panel_nodes[0].get("source_layout_ids") or []) if str(item).strip()]
                title_node = convert_node(
                    {
                        "node_id": f"{panel_id}_title",
                        "component": "SectionHeading",
                        "props": {"text": title, "level": 2},
                        "source_layout_ids": title_source,
                        "children": [],
                    },
                    panel_id,
                )
                if title_node:
                    components.append(title_node)
            for node in panel_nodes:
                converted = convert_node(node, panel_id)
                if converted:
                    components.append(converted)

        style_plan = dict(panel_plan.get("style_plan") or {})
        style_tokens = {
            "pageBackground": str(style_plan.get("page_background") or style_plan.get("pageBackground") or "#f4f8fc"),
            "panelBackground": str(style_plan.get("panel_background") or style_plan.get("panelBackground") or "rgba(255,255,255,0.9)"),
            "borderColor": str(style_plan.get("border_color") or style_plan.get("borderColor") or "rgba(120,145,170,0.28)"),
            "headingColor": str(style_plan.get("heading_color") or style_plan.get("headingColor") or "#0f2740"),
            "bodyColor": str(style_plan.get("body_color") or style_plan.get("bodyColor") or "#1f3348"),
        }
        return {
            "plan_id": f"single_agent_v2_p{int(page)}",
            "components": components,
            "layout": {
                "layout_mode": "single_column",
                "regions": [{"id": "main", "kind": "content"}, {"id": "sidebar", "kind": "meta"}],
                "content_max_width": 980,
            },
            "style_tokens": style_tokens,
            "trace_meta": {
                "pipeline": "single_agent_v2",
                "panel_plan": panel_plan,
                "style_intent": str(style_intent or ""),
                "theme_mode": str(theme_mode or ""),
                "detail_level": str(detail_level or ""),
                "compare_mode": bool(compare_mode),
                "assembly_used": True,
            },
            "ui_ops": [],
            "agent_trace": [],
            "agent_tool_calls": [],
        }

    async def _invoke_single_agent_model(
        self,
        *,
        system_prompt: str,
        user_prompt: Dict[str, Any],
        rendered_page_image: str,
        step: int,
        phase: str,
    ) -> Dict[str, Any]:
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        model_name = str(getattr(settings, "reader_agent_model", "qwen-3.5-plus") or "qwen-3.5-plus").strip()
        if not api_key or not base_url or not model_name:
            return {}

        request_timeout = max(2.0, float(int(getattr(settings, "reader_agent_timeout_ms", 90000) or 90000)) / 1000.0)
        max_tokens = max(512, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000))
        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": json.dumps(user_prompt, ensure_ascii=False)}
        ]
        image_token = str(rendered_page_image or "").strip()
        if step == 1 and image_token and (image_token.startswith("data:image") or image_token.startswith("http")):
            content_parts.append({"type": "image_url", "image_url": {"url": image_token}})

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": str(system_prompt or "")},
                        {"role": "user", "content": content_parts},
                    ],
                    temperature=0,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    timeout=request_timeout,
                ),
                timeout=request_timeout + 1.0,
            )
        except Exception as exc:  # pragma: no cover - network/provider failures expected at runtime
            logger.warning(
                f"[ReaderComposeService] single_agent_v2 model call failed "
                f"step={step} phase={phase} model={model_name}: {type(exc).__name__}: {exc}"
            )
            return {}

        content = ""
        try:
            content = str((response.choices[0].message.content or "")).strip()
        except Exception:
            return {}
        parsed = await parse_json_dict_from_model_text(content)
        if not parsed:
            return {}
        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
        }
        return {
            "status": str(parsed.get("status") or ""),
            "step_result": dict(parsed.get("step_result") or {}),
            "usage": usage,
            "self_check": dict(parsed.get("self_check") or {}),
            "fixes_applied": list(parsed.get("fixes_applied") or []),
        }

    def _collect_docmind_blocks_for_single_agent(
        self,
        *,
        page: int,
        base_payload: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
        docmind_structure = dict(base_payload.get("docmind_structure") or {})
        page_structure_v3 = dict(base_payload.get("page_structure_v3") or {})
        layouts = [row for row in list(docmind_structure.get("layouts") or []) if isinstance(row, dict)]
        block_groups = [row for row in list(page_structure_v3.get("block_groups") or []) if isinstance(row, dict)]

        layout_to_block_ids: Dict[str, List[str]] = {}
        for row in block_groups:
            layout_uid = str(row.get("layout_unique_id") or "").strip()
            raw_block_id = str(row.get("block_id") or "").strip()
            canonical = self._normalize_canonical_block_id(page=page, raw_id=raw_block_id)
            if not layout_uid or not canonical:
                continue
            layout_to_block_ids.setdefault(layout_uid, [])
            if canonical not in layout_to_block_ids[layout_uid]:
                layout_to_block_ids[layout_uid].append(canonical)

        page_zero = max(0, int(page) - 1)
        page_one = max(1, int(page))
        output_rows: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, row in enumerate(layouts, start=1):
            raw_page_num = row.get("pageNum")
            if isinstance(raw_page_num, list) and raw_page_num:
                page_values = []
                for item in raw_page_num:
                    token = str(item or "").strip()
                    if token.isdigit():
                        page_values.append(int(token))
                if page_values and page_zero not in page_values and page_one not in page_values:
                    continue
            elif isinstance(raw_page_num, (int, float, str)):
                token = str(raw_page_num).strip()
                if token.isdigit() and int(token) not in {page_zero, page_one}:
                    continue

            layout_id = str(row.get("uniqueId") or row.get("layoutId") or row.get("id") or f"layout_{idx:04d}").strip()
            if not layout_id:
                layout_id = f"layout_{idx:04d}"
            if layout_id in seen_ids:
                continue
            seen_ids.add(layout_id)

            text_rows = [item for item in list(row.get("blocks") or []) if isinstance(item, dict)]
            source_text = " ".join(
                self._normalize_spaces(str(item.get("text") or ""))
                for item in text_rows
                if self._normalize_spaces(str(item.get("text") or ""))
            ).strip()
            if not source_text:
                source_text = self._normalize_spaces(str(row.get("text") or ""))

            output_rows.append(
                {
                    "layout_id": layout_id,
                    "source_text": source_text,
                    "type": str(row.get("type") or "").strip().lower(),
                    "subType": str(row.get("subType") or "").strip().lower(),
                    "block_ids": list(layout_to_block_ids.get(layout_id) or []),
                    "reading_order": int(row.get("index") or idx),
                }
            )

        output_rows = sorted(
            output_rows,
            key=lambda item: (
                int(item.get("reading_order") or 10**9),
                str(item.get("layout_id") or ""),
            ),
        )
        return output_rows, layout_to_block_ids

    def _step_result_to_ui_plan(
        self,
        *,
        page: int,
        step_result: Dict[str, Any],
        docmind_blocks: Sequence[Dict[str, Any]],
        layout_to_block_ids: Dict[str, List[str]],
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
    ) -> Dict[str, Any]:
        classification_rows = [
            row
            for row in list((step_result.get("classification") or {}).get("items") or [])
            if isinstance(row, dict)
        ]
        cleaning_rows = [
            row
            for row in list((step_result.get("cleaning") or {}).get("items") or [])
            if isinstance(row, dict)
        ]
        component_rows = [
            row
            for row in list((step_result.get("ui_plan_draft") or {}).get("components") or [])
            if isinstance(row, dict)
        ]
        layout_tokens = dict((step_result.get("ui_plan_draft") or {}).get("layout_tokens") or {})
        layout_bucket_map: Dict[str, str] = {}
        for row in classification_rows:
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            bucket = str(row.get("bucket") or "").strip().lower()
            if bucket in {"main_content", "aux_content"}:
                layout_bucket_map[layout_id] = bucket

        docmind_map = {
            str(row.get("layout_id") or ""): dict(row)
            for row in list(docmind_blocks or [])
            if isinstance(row, dict) and str(row.get("layout_id") or "")
        }
        cleaned_text_map = {
            str(row.get("layout_id") or ""): str(row.get("normalized_text") or row.get("source_text") or "")
            for row in cleaning_rows
            if str(row.get("layout_id") or "")
        }

        block_anchor_map: Dict[str, Dict[str, Any]] = {}
        for block in list(base_payload.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            canonical = self._normalize_canonical_block_id(page=page, raw_id=str(block.get("id") or ""))
            if not canonical:
                canonical = str(((block.get("source_anchor") or {}).get("canonical_block_id") or "")).strip()
            if not canonical or canonical in block_anchor_map:
                continue
            anchor = self._normalize_anchor_ref(
                anchor=block.get("source_anchor"),
                page=page,
                quote_text=str(block.get("text") or ""),
            )
            if anchor:
                block_anchor_map[canonical] = anchor

        def infer_bucket(source_layout_ids: Sequence[str]) -> str:
            aux_count = 0
            main_count = 0
            for layout_id in source_layout_ids:
                bucket = layout_bucket_map.get(str(layout_id))
                if bucket == "aux_content":
                    aux_count += 1
                elif bucket == "main_content":
                    main_count += 1
            if aux_count > 0 and aux_count >= main_count:
                return "aux_content"
            return "main_content"

        def normalize_zone_type(value: Any) -> str:
            token = str(value or "").strip().lower()
            return token if token in {"main_body", "side_context", "figure_meta"} else ""

        def normalize_region(value: Any) -> str:
            return str(value or "").strip()

        def normalize_display(value: Any) -> str:
            token = str(value or "").strip().lower()
            return token if token in {"default", "collapsed", "pinned", "hidden_until_expand"} else ""

        layout_regions = [
            str((row or {}).get("id") or "").strip()
            for row in list(layout_tokens.get("regions") or [])
            if isinstance(row, dict) and str((row or {}).get("id") or "").strip()
        ]
        layout_region_set = set(layout_regions)
        lower_region_pairs = [(region_id, region_id.lower()) for region_id in layout_regions]
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

        def default_region_for_zone(zone_type: str) -> str:
            if layout_regions:
                if zone_type == "side_context" and side_region_candidates:
                    return side_region_candidates[0]
                if zone_type != "side_context" and main_region_candidates:
                    return main_region_candidates[0]
                return layout_regions[0]
            return "sidebar" if zone_type == "side_context" else "main"

        compat_filled_count = 0
        nodes: List[Dict[str, Any]] = []
        for idx, row in enumerate(component_rows, start=1):
            component_name = str(row.get("component") or "").strip()
            if not component_name:
                continue
            source_layout_ids = [
                str(item).strip()
                for item in list(row.get("source_block_ids") or [])
                if str(item).strip()
            ]
            if not source_layout_ids:
                continue
            explicit_zone_type = normalize_zone_type(row.get("zone_type"))
            component_bucket = infer_bucket(source_layout_ids)
            zone_type = explicit_zone_type or ("side_context" if component_bucket == "aux_content" else "main_body")
            default_region = default_region_for_zone(zone_type)
            default_column = (
                default_region if (layout_region_set and default_region in layout_region_set) else ("sidebar" if zone_type == "side_context" else "main")
            )
            source_block_ids: List[str] = []
            source_anchor_refs: List[Dict[str, Any]] = []
            for layout_id in source_layout_ids:
                mapped_block_ids = list(layout_to_block_ids.get(layout_id) or [layout_id])
                for block_id in mapped_block_ids:
                    if block_id and block_id not in source_block_ids:
                        source_block_ids.append(block_id)
                    anchor = dict(block_anchor_map.get(block_id) or {})
                    if anchor and anchor not in source_anchor_refs:
                        source_anchor_refs.append(anchor)
            if not source_block_ids:
                continue

            first_layout_id = source_layout_ids[0]
            fallback_text = self._normalize_spaces(
                cleaned_text_map.get(first_layout_id) or str((docmind_map.get(first_layout_id) or {}).get("source_text") or "")
            )
            props = dict(row.get("props") or {})
            if not props:
                if component_name == "SectionHeading":
                    props = {"text": fallback_text or "Untitled", "level": 2}
                elif component_name == "ListBlock":
                    props = {"items": [fallback_text] if fallback_text else []}
                elif component_name == "ContextRail":
                    props = {"title": "Context", "items": [{"text": fallback_text}] if fallback_text else []}
                else:
                    props = {"text": fallback_text}
            elif component_name == "ListBlock":
                # Frontend contract requires ListBlock.props.items to be string[].
                # Model outputs may include object rows (for example {"content": "..."}).
                raw_items = props.get("items")
                normalized_items: List[str] = []
                if isinstance(raw_items, list):
                    for item in raw_items:
                        text = ""
                        if isinstance(item, dict):
                            text = self._normalize_spaces(
                                str(
                                    item.get("text")
                                    or item.get("content")
                                    or item.get("label")
                                    or item.get("value")
                                    or item.get("title")
                                    or ""
                                )
                            )
                        else:
                            text = self._normalize_spaces(str(item or ""))
                        if text:
                            normalized_items.append(text)
                elif raw_items is not None:
                    text = self._normalize_spaces(str(raw_items))
                    if text:
                        normalized_items.append(text)
                if not normalized_items and fallback_text:
                    normalized_items = [fallback_text]
                props["items"] = normalized_items

            compat_filled_fields: List[str] = []
            if not explicit_zone_type:
                compat_filled_fields.append("zone_type")
            column_id = normalize_region(row.get("column_id"))
            if not column_id or (layout_region_set and column_id not in layout_region_set):
                column_id = default_column
                compat_filled_fields.append("column_id")
            region = normalize_region(row.get("region"))
            if not region or (layout_region_set and region not in layout_region_set):
                region = default_region
                compat_filled_fields.append("region")
            display_mode = normalize_display(row.get("display"))
            if not display_mode:
                display_mode = "collapsed" if zone_type == "side_context" else "default"
                compat_filled_fields.append("display")
            order_key = row.get("order_key")
            resolved_order_key: Optional[float] = None
            if isinstance(order_key, (int, float)) and not isinstance(order_key, bool):
                resolved_order_key = float(order_key)
            else:
                fallback_order = row.get("order")
                if isinstance(fallback_order, (int, float)) and not isinstance(fallback_order, bool):
                    resolved_order_key = float(fallback_order)
                else:
                    resolved_order_key = float(idx)
                    compat_filled_fields.append("order_key")

            node_payload: Dict[str, Any] = {
                "id": f"sgv2_{idx:03d}",
                "type": component_name,
                "props": props,
                "children": [],
                "source_anchor_refs": source_anchor_refs,
                "source_block_ids": source_block_ids,
                "zone_type": zone_type,
                "column_id": column_id,
                "region": region,
                "display": display_mode,
                "order_key": float(resolved_order_key),
                "capabilities": ["copy", "jump_anchor", "inline_query"],
                "actions": [],
                "layout_slot": {"reserved_height": 160, "lock_height": False},
            }
            if compat_filled_fields:
                node_payload["compat_filled"] = True
                node_payload["compat_filled_fields"] = sorted(set(compat_filled_fields))
                compat_filled_count += 1

            nodes.append(
                node_payload
            )

        if not nodes and classification_rows:
            first_main = next(
                (
                    row
                    for row in classification_rows
                    if str(row.get("bucket") or "").strip() == "main_content"
                    and str(row.get("layout_id") or "").strip()
                ),
                None,
            )
            if first_main:
                layout_id = str(first_main.get("layout_id") or "").strip()
                text = self._normalize_spaces(
                    cleaned_text_map.get(layout_id) or str((docmind_map.get(layout_id) or {}).get("source_text") or "")
                )
                block_ids = list(layout_to_block_ids.get(layout_id) or [layout_id])
                anchors = [
                    dict(block_anchor_map[item])
                    for item in block_ids
                    if item in block_anchor_map
                ]
                nodes.append(
                    {
                        "id": "sgv2_fallback_001",
                        "type": "ParagraphProse",
                        "props": {"text": text or layout_id},
                        "children": [],
                        "source_anchor_refs": anchors,
                        "source_block_ids": block_ids,
                        "zone_type": "main_body",
                        "column_id": "main",
                        "region": "main",
                        "display": "default",
                        "order_key": 1.0,
                        "compat_filled": True,
                        "compat_filled_fields": ["zone_type", "column_id", "region", "display", "order_key"],
                        "capabilities": ["copy", "jump_anchor", "inline_query"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 160, "lock_height": False},
                    }
                )

        requested_layout_mode = str(layout_tokens.get("layout_mode") or "").strip().lower()
        if requested_layout_mode not in {"single_column", "split", "drawer", "section_inline"}:
            requested_layout_mode = "single_column"
        layout_payload: Dict[str, Any] = {
            "content_max_width": 980,
            "column_count": 2 if requested_layout_mode == "split" else 1,
            "layout_mode": requested_layout_mode,
        }
        if isinstance(layout_tokens.get("sidebar_width"), (int, float)) and not isinstance(layout_tokens.get("sidebar_width"), bool):
            layout_payload["sidebar_width"] = float(layout_tokens.get("sidebar_width"))
        if isinstance(layout_tokens.get("regions"), list):
            region_rows = []
            for row in list(layout_tokens.get("regions") or []):
                if not isinstance(row, dict):
                    continue
                region_id = str(row.get("id") or "").strip()
                if not region_id:
                    continue
                region_rows.append(dict(row))
            if region_rows:
                layout_payload["regions"] = region_rows

        return {
            "plan_id": f"single_agent_v2_p{int(page)}",
            "components": nodes,
            "layout": layout_payload,
            "style_tokens": {
                "intent": str(style_intent or detail_level or "standard"),
                "theme_mode": str(theme_mode or "light"),
                "compare_mode": bool(compare_mode),
            },
            "trace_meta": {
                "pipeline": "single_agent_v2",
                "component_count": len(nodes),
                "layout_mode": requested_layout_mode,
                "compat_filled_count": int(compat_filled_count),
                "compat_filled_ratio": (
                    float(compat_filled_count) / float(len(nodes))
                    if len(nodes) > 0
                    else 0.0
                ),
            },
            "ui_ops": [],
            "agent_trace": [],
            "agent_tool_calls": [],
        }

    async def _build_simplified_pipeline_result_legacy(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        latency_budget_ms: int,
        selected_kb_id: Optional[int],
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        payload = dict(base_payload or {})
        page_structure_v3 = dict(payload.get("page_structure_v3") or {})
        docmind_structure = dict(payload.get("docmind_structure") or {})
        atom_bundle = build_canonical_atom_bundle(
            docmind_structure=docmind_structure,
            page=int(page),
            paper_id=int(paper.id),
        )

        path = self._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
            user_id=int(paper.user_id),
            paper_id=int(paper.id),
            paper_title=paper.title,
            paper_pdf_path=paper.pdf_path,
        )
        mm_prompt_payload: Dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                mm_prompt_payload = await self._mm_layout_service.build_mm_prompt_payload(
                    pdf_path=path,
                    page=int(page),
                    base_payload=payload,
                ) or {}
            except Exception as exc:
                logger.warning(f"[ReaderComposeService] simplified build_mm_prompt_payload failed page={page}: {exc}")
                mm_prompt_payload = {}

        image_rows = [
            row
            for row in list(mm_prompt_payload.get("images") or [])[:1]
            if isinstance(row, dict)
        ]
        atoms_digest = [
            {
                "atom_id": str(row.get("atom_id") or ""),
                "reading_order": int(row.get("reading_order") or 0),
                "type": str(row.get("type") or ""),
                "sub_type": str(row.get("sub_type") or ""),
                "default_role": str(row.get("default_role") or "unknown"),
                "default_component": str(row.get("default_component") or "ParagraphProse"),
                "bbox": list(row.get("bbox") or [0.0, 0.0, 0.0, 0.0])[:4],
                "text_preview": self._normalize_spaces(str(row.get("text") or ""))[:220],
            }
            for row in list(atom_bundle.usable_atoms or [])
            if str(row.get("atom_id") or "")
        ]
        layout_meta = {
            "paper_id": int(paper.id),
            "page": int(page),
            "pipeline_version": self._pipeline_version(),
            "visual_reference_only": True,
        }

        stage1_payload = {
            "layout_meta": layout_meta,
            "images": image_rows,
            "atoms_digest": atoms_digest,
        }
        stage1_semantic, stage1_meta = await self._mm_layout_service.build_stage1_semantic_annotations(
            prompt_payload=stage1_payload,
            atom_bundle=atom_bundle,
        )
        stage1_failed = not isinstance(stage1_semantic, dict)
        if stage1_failed:
            stage1_semantic = {
                "annotations": [
                    {
                        "atom_id": str(row.get("atom_id") or ""),
                        "role": str(row.get("default_role") or "unknown"),
                        "importance": "normal",
                        "grouping_hint": "",
                        "component_hint": str(row.get("default_component") or "ParagraphProse"),
                        "confidence": 0.7,
                    }
                    for row in list(atom_bundle.usable_atoms or [])
                    if str(row.get("atom_id") or "")
                ]
            }
            stage1_meta = dict(stage1_meta or {})
            stage1_meta["used"] = False
            stage1_meta["fallback_reason"] = "stage1_failed"
            stage1_meta["degraded"] = True
            stage2_slots = build_deterministic_baseline_slots(
                atom_bundle=atom_bundle,
                allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
            )
            stage2_meta = {
                "used": False,
                "model": "",
                "fallback_used": True,
                "fallback_reason": "stage1_failed",
                "degraded": True,
            }
        else:
            stage2_payload = {
                "layout_meta": layout_meta,
                "images": image_rows,
                "semantic_annotations": dict(stage1_semantic or {}),
                "atoms_digest": atoms_digest,
                "allowed_components": list(SIMPLIFIED_ALLOWED_COMPONENTS),
            }
            stage2_slots, stage2_meta = await self._mm_layout_service.build_stage2_design_slots(
                prompt_payload=stage2_payload,
                atom_bundle=atom_bundle,
                allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
            )
        if not isinstance(stage2_slots, dict):
            stage2_slots = build_deterministic_baseline_slots(
                atom_bundle=atom_bundle,
                allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
            )
            stage2_meta = dict(stage2_meta or {})
            stage2_meta["used"] = False
            stage2_meta["fallback_reason"] = "stage2_failed"
            stage2_meta["degraded"] = True

        block_groups = [
            row for row in list(page_structure_v3.get("block_groups") or []) if isinstance(row, dict)
        ]
        layout_block_index: Dict[Tuple[str, int], str] = {}
        for row in block_groups:
            layout_uid = str(row.get("layout_unique_id") or "").strip()
            block_id_raw = str(row.get("block_id") or "").strip()
            if not layout_uid or not block_id_raw:
                continue
            block_match = re.search(r"_b(\d+)$", block_id_raw)
            block_index = int(block_match.group(1)) if block_match else 1
            canonical = self._normalize_canonical_block_id(page=page, raw_id=block_id_raw) or f"p{int(page)}_{block_id_raw}"
            layout_block_index[(layout_uid, block_index)] = canonical

        atom_to_block: Dict[str, str] = {}
        for row in list(atom_bundle.usable_atoms or []):
            atom_id = str(row.get("atom_id") or "").strip()
            layout_uid = str(row.get("source_layout_id") or "").strip()
            block_index = int(row.get("block_index") or 1)
            canonical = layout_block_index.get((layout_uid, block_index))
            if not canonical:
                # fallback to first block in the same layout
                candidates = [
                    value
                    for (uid, _idx), value in layout_block_index.items()
                    if uid == layout_uid
                ]
                canonical = candidates[0] if candidates else ""
            if atom_id and canonical:
                atom_to_block[atom_id] = canonical

        ordered_block_ids: List[str] = []
        suggested_components: List[Dict[str, Any]] = []
        stage2_segments: List[Dict[str, Any]] = []
        for idx, slot in enumerate(list(stage2_slots.get("page_layout_slots") or []), start=1):
            if not isinstance(slot, dict):
                continue
            atom_ids = [
                str(item).strip()
                for item in list(slot.get("atom_ids") or [])
                if str(item).strip()
            ]
            block_ids: List[str] = []
            for atom_id in atom_ids:
                canonical = str(atom_to_block.get(atom_id) or "").strip()
                if canonical and canonical not in block_ids:
                    block_ids.append(canonical)
                    if canonical not in ordered_block_ids:
                        ordered_block_ids.append(canonical)
            if not block_ids:
                continue
            component = str(slot.get("component") or "ParagraphProse")
            suggested_components.append(
                {
                    "block_ids": block_ids,
                    "component": component,
                    "reason": "stage2_design_slots",
                }
            )
            stage2_segments.append(
                {
                    "segment_id": str(slot.get("slot_id") or f"slot_{idx:03d}"),
                    "kind": "paragraph",
                    "kind_hint": "paragraph",
                    "component_hint": component,
                    "block_ids": block_ids,
                    "line_ids": [],
                    "evidence_line_ids": [],
                    "word_ids": [],
                    "char_ranges": [],
                    "title": "",
                    "resolved_text": "",
                    "sort_order": idx,
                    "continuation": "none",
                    "reason": "stage2_design_slots",
                    "confidence": 0.88,
                }
            )

        segment_id_map: Dict[str, Dict[str, Any]] = {}
        for slot in list(stage2_slots.get("page_layout_slots") or []):
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("slot_id") or "").strip()
            if not slot_id:
                continue
            atom_ids = [
                str(item).strip()
                for item in list(slot.get("atom_ids") or [])
                if str(item).strip()
            ]
            mapped_block_ids: List[str] = []
            for atom_id in atom_ids:
                block_id = str(atom_to_block.get(atom_id) or "").strip()
                if block_id and block_id not in mapped_block_ids:
                    mapped_block_ids.append(block_id)
            segment_id_map[slot_id] = {
                "atom_ids": atom_ids,
                "block_ids": mapped_block_ids,
            }

        payload["stage1_structural_annotations"] = dict(stage1_semantic or {})
        payload["stage2_design_layout"] = dict(stage2_slots or {})
        payload["atom_semantics"] = dict(stage1_semantic or {})
        payload["deterministic_page_skeleton"] = {
            "source": "deterministic_atom_skeleton",
            "slots": list(stage2_slots.get("page_layout_slots") or []),
            "unused_atom_ids": list(stage2_slots.get("unused_atom_ids") or []),
        }
        payload["stage2_style_plan"] = {
            "source": "stage2_design_v2" if bool(stage2_meta.get("used")) else "deterministic_baseline",
            "slots": list(stage2_slots.get("page_layout_slots") or []),
            "unused_atom_ids": list(stage2_slots.get("unused_atom_ids") or []),
            "notes": list((stage2_slots or {}).get("notes") or []),
        }
        payload["layout_advice_v3"] = {
            "source": "stage2_design_v2" if bool(stage2_meta.get("used")) else "deterministic_baseline",
            "ordered_block_ids": ordered_block_ids,
            "suggested_components": suggested_components,
            "grouping_hints": [],
            "visual_hints": [],
            "notes": [
                f"unused_atom_count={len(list(stage2_slots.get('unused_atom_ids') or []))}",
                "visual_reference_only=true",
            ],
            "unused_atom_ids": [
                str(item).strip()
                for item in list(stage2_slots.get("unused_atom_ids") or [])
                if str(item).strip()
            ],
        }
        payload["segment_map"] = {
            "source": "stage2_design_v2",
            "segments": stage2_segments,
            "counts": {
                "segment_count": len(stage2_segments),
                "unused_atom_count": len(list(stage2_slots.get("unused_atom_ids") or [])),
            },
        }
        payload["segment_map_meta"] = {
            "used": True,
            "source": "stage2_design_v2",
            "reason": "simplified_pipeline",
        }
        payload["segment_id_map"] = segment_id_map
        payload["pipeline_contract_meta"] = {
            "used": True,
            "pipeline": "reader_simplified_v2",
            "stage1": dict(stage1_meta or {}),
            "stage2": dict(stage2_meta or {}),
            "visual_reference_only": True,
        }
        payload["canonical_atoms"] = {
            "count": len(list(atom_bundle.atoms or [])),
            "usable_count": len(list(atom_bundle.usable_atom_ids or [])),
            "excluded_count": len(list(atom_bundle.excluded_atoms or [])),
            "excluded_atoms": list(atom_bundle.excluded_atoms or [])[:200],
            "items": list(atom_bundle.atoms or [])[:1600],
            "source": "docmind",
        }
        payload["candidate_ranking"] = {
            "strategy": "single_candidate_simplified",
            "selected_candidate_id": "candidate_1",
            "candidates": [
                {
                    "candidate_id": "candidate_1",
                    "source": "stage2_design_v2",
                    "score": 1.0,
                }
            ],
        }
        payload["repair_report"] = {
            "rounds": 0,
            "used": False,
            "reason": "",
        }

        ui_plan = self._build_initial_ui_plan(
            paper=paper,
            page=page,
            base_payload=payload,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
        )
        block_to_atoms: Dict[str, List[str]] = {}
        for atom_id, block_id in atom_to_block.items():
            block_to_atoms.setdefault(block_id, []).append(atom_id)

        components = []
        used_atoms: set[str] = set()
        for node in list(ui_plan.get("components") or []):
            if not isinstance(node, dict):
                continue
            cloned = dict(node)
            source_block_ids = [
                str(item).strip()
                for item in list(cloned.get("source_block_ids") or [])
                if str(item).strip()
            ]
            if not source_block_ids:
                source_block_ids = [
                    str((row or {}).get("canonical_block_id") or "").strip()
                    for row in list(cloned.get("source_anchor_refs") or [])
                    if isinstance(row, dict) and str((row or {}).get("canonical_block_id") or "").strip()
                ]
            source_atom_ids: List[str] = []
            for block_id in source_block_ids:
                for atom_id in list(block_to_atoms.get(block_id) or []):
                    if atom_id not in source_atom_ids:
                        source_atom_ids.append(atom_id)
                        used_atoms.add(atom_id)
            if source_block_ids:
                cloned["source_block_ids"] = source_block_ids
            if source_atom_ids:
                cloned["source_atom_ids"] = source_atom_ids
            components.append(cloned)
        ui_plan["components"] = components

        missing_atoms = [
            atom
            for atom in list(atom_bundle.usable_atoms or [])
            if str(atom.get("atom_id") or "").strip() and str(atom.get("atom_id") or "").strip() not in used_atoms
        ]
        for atom in missing_atoms:
            atom_id = str(atom.get("atom_id") or "").strip()
            if not atom_id:
                continue
            component_type = str(atom.get("default_component") or "ParagraphProse")
            text = self._normalize_spaces(str(atom.get("text") or ""))
            if not text:
                continue
            source_block_id = str(atom_to_block.get(atom_id) or "").strip()
            anchor_rows: List[Dict[str, Any]] = []
            if source_block_id:
                for row in list(payload.get("blocks") or []):
                    block_id = self._normalize_canonical_block_id(page=page, raw_id=str(row.get("id") or ""))
                    if block_id and block_id == source_block_id:
                        anchor = self._normalize_anchor_ref(anchor=row.get("source_anchor"), page=page, quote_text=text)
                        if anchor:
                            anchor_rows.append(anchor)
                        break
            node_props: Dict[str, Any] = {"text": text}
            if component_type == "SectionHeading":
                node_props = {"text": text, "level": 2}
            elif component_type == "ListBlock":
                node_props = {"items": [text]}
            elif component_type == "ContextRail":
                node_props = {"title": "Context", "items": [{"text": text, "anchor": anchor_rows}]}
            elif component_type in {"FigurePanel", "TablePanel"}:
                node_props = {"caption": text, "ai_insight": ""}
            ui_plan.setdefault("components", []).append(
                {
                    "id": f"fallback_atom_{uuid.uuid4().hex[:8]}",
                    "type": component_type,
                    "props": node_props,
                    "children": [],
                    "source_anchor_refs": anchor_rows,
                    "source_block_ids": [source_block_id] if source_block_id else [],
                    "source_atom_ids": [atom_id],
                    "capabilities": ["copy"],
                    "actions": [],
                    "layout_slot": {"reserved_height": 160, "lock_height": False},
                }
            )
            used_atoms.add(atom_id)

        ui_plan = await self._apply_deepseek_assembly_decision(
            ui_plan=ui_plan,
            base_payload=payload,
            page=page,
            latency_budget_ms=latency_budget_ms,
            user_id=int(user_id),
            user_intent=str(style_intent or detail_level or "standard"),
        )
        gate_report = enforce_minimal_gates(
            ui_plan=ui_plan,
            usable_atom_ids=list(atom_bundle.usable_atom_ids or []),
            allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
            non_empty_input=bool(atom_bundle.usable_atom_ids),
        )
        if not bool(gate_report.get("passed")):
            baseline_slots = build_deterministic_baseline_slots(
                atom_bundle=atom_bundle,
                allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
            )
            payload["stage2_design_layout"] = baseline_slots
            payload["layout_advice_v3"] = {
                "source": "deterministic_baseline",
                "ordered_block_ids": [],
                "suggested_components": [],
                "grouping_hints": [],
                "visual_hints": [],
                "notes": ["baseline_due_to_gate_failure"],
                "unused_atom_ids": [],
            }
            payload["segment_map"] = {"source": "deterministic_baseline", "segments": [], "counts": {}}
            ui_plan = self._build_initial_ui_plan(
                paper=paper,
                page=page,
                base_payload=payload,
                style_intent=style_intent,
                theme_mode=theme_mode,
                detail_level=detail_level,
                compare_mode=compare_mode,
            )
            gate_report = enforce_minimal_gates(
                ui_plan=ui_plan,
                usable_atom_ids=list(atom_bundle.usable_atom_ids or []),
                allowed_components=SIMPLIFIED_ALLOWED_COMPONENTS,
                non_empty_input=bool(atom_bundle.usable_atom_ids),
            )
            payload["repair_report"] = {
                "rounds": 1,
                "used": True,
                "reason": "minimal_gate_failed_baseline_rebuild",
            }

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        quality_report = {
            "overall": 0.92 if bool(gate_report.get("passed")) else 0.68,
            "hard_constraints_passed": bool(gate_report.get("passed")),
            "validation_errors": [],
            "quality_target": 0.0,
            "elapsed_ms": elapsed_ms,
            "iterations": 1,
            "degraded": bool(not stage1_meta.get("used") or not stage2_meta.get("used")),
            "stop_reason": "simplified_pipeline",
            "schema_valid": bool(gate_report.get("schema_valid")),
            "whitelist_valid": bool(gate_report.get("whitelist_valid")),
            "ownership_unchanged": bool(gate_report.get("ownership_unchanged")),
            "full_coverage": bool(gate_report.get("full_coverage")),
            "non_empty_plan_for_non_empty_input": bool(gate_report.get("non_empty_plan_for_non_empty_input")),
            "coverage_rate": (
                float(gate_report.get("used_atom_count") or 0) / float(max(1, gate_report.get("usable_atom_count") or 1))
            ),
            "pipeline_latency_ms": elapsed_ms,
            "p50_done_latency_ms_target": 8000,
            "p95_done_latency_ms_target": 18000,
            "minimal_gate_report": gate_report,
        }
        assets = [
            row
            for row in list(payload.get("assets") or [])
            if isinstance(row, dict) and str(row.get("kind") or "") in {"link", "annotation", "image_hint"}
        ]
        payload["pipeline_contract_meta"] = dict(payload.get("pipeline_contract_meta") or {})
        payload["pipeline_contract_meta"]["minimal_gate_report"] = gate_report
        payload["pipeline_contract_meta"]["elapsed_ms"] = elapsed_ms
        payload["minimal_gate_report"] = dict(gate_report or {})
        payload["qwen_plan_meta"] = {
            "used": True,
            "reason": "simplified_4step_pipeline",
            "stage1_model": str((stage1_meta or {}).get("model") or ""),
            "stage2_model": str((stage2_meta or {}).get("model") or ""),
            "stage1_fallback_used": bool((stage1_meta or {}).get("fallback_used")),
            "stage2_fallback_used": bool((stage2_meta or {}).get("fallback_used")),
            "pipeline_version": self._pipeline_version(),
        }
        payload["mm_assist_meta"] = {
            "used": True,
            "reason": "simplified_4step_pipeline",
            "visual_reference_only": True,
            "degraded": bool(not stage1_meta.get("used") or not stage2_meta.get("used")),
        }
        loop_result = {
            "ui_plan": ui_plan,
            "quality_report": quality_report,
            "node_gate_report": {},
            "iteration_trace": [
                {
                    "iteration": 1,
                    "ui_plan": ui_plan,
                    "quality_report": quality_report,
                    "ui_ops": list((ui_plan or {}).get("ui_ops") or []),
                    "agent_trace": list((ui_plan or {}).get("agent_trace") or []),
                    "agent_tool_calls": list((ui_plan or {}).get("agent_tool_calls") or []),
                }
            ],
            "iterations": 1,
            "degraded": bool(quality_report.get("degraded")),
            "stop_reason": "simplified_pipeline",
            "build_mode": "compose_agent_simplified",
        }
        return {
            "base_payload": payload,
            "loop_result": loop_result,
            "assets": assets,
        }

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
        max_iterations: Optional[int] = None,
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        confidence = float(base_payload.get("structure_confidence") or 0.0)
        resolved_max_iterations = (
            int(max_iterations)
            if isinstance(max_iterations, int) and max_iterations > 0
            else (
                LOW_CONFIDENCE_MAX_ITERATIONS
                if confidence < 0.68
                else DEFAULT_MAX_ITERATIONS
            )
        )
        resolved_max_iterations = max(1, min(int(resolved_max_iterations), 24))

        current_plan = self._build_initial_ui_plan(
            paper=paper,
            page=page,
            base_payload=base_payload,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
        )
        current_plan = await self._apply_deepseek_assembly_decision(
            ui_plan=current_plan,
            base_payload=base_payload,
            page=page,
            latency_budget_ms=latency_budget_ms,
            user_id=int(getattr(paper, "user_id", 0) or 0),
            user_intent=str(style_intent or detail_level or "standard"),
        )
        best_plan = current_plan
        best_quality: Dict[str, Any] = {}
        best_score = -1.0
        iteration_trace: List[Dict[str, Any]] = []
        degraded = False
        stop_reason = "max_iterations_reached"
        low_gain_streak = 0
        previous_overall: Optional[float] = None

        for iteration in range(1, resolved_max_iterations + 1):
            current_plan = self._sanitize_ui_plan_anchors(
                current_plan,
                page=page,
                base_payload=base_payload,
            )
            gate_result = self._apply_node_level_anchor_gate(
                ui_plan=current_plan,
                base_payload=base_payload,
                page=page,
            )
            current_plan = dict(gate_result.get("ui_plan") or current_plan)
            node_gate_report = dict(gate_result.get("node_gate_report") or {})
            validation = self.validate_ui_plan(current_plan, page=page)
            quality = self.score_ui_plan(
                ui_plan=current_plan,
                base_payload=base_payload,
                validation_errors=validation.get("errors") or [],
                quality_target=quality_target,
            )
            quality["node_gate_report"] = node_gate_report
            quality["iteration"] = iteration
            quality["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
            quality.setdefault("tool_call_trace", [])

            iteration_trace.append(
                {
                    "iteration": iteration,
                    "ui_plan": current_plan,
                    "quality_report": quality,
                    "ui_ops": list((current_plan or {}).get("ui_ops") or []),
                    "agent_trace": list((current_plan or {}).get("agent_trace") or []),
                    "agent_tool_calls": list((current_plan or {}).get("agent_tool_calls") or []),
                }
            )

            overall = float(quality.get("overall") or 0.0)
            if overall >= best_score:
                best_score = overall
                best_plan = current_plan
                best_quality = quality

            if previous_overall is not None:
                delta = overall - previous_overall
                if delta < 0.01:
                    low_gain_streak += 1
                else:
                    low_gain_streak = 0
            previous_overall = overall

            hard_pass = bool(quality.get("hard_constraints_passed"))
            if hard_pass and overall >= quality_target:
                stop_reason = "quality_threshold_met"
                break

            if low_gain_streak >= 2:
                stop_reason = "early_stop_low_gain"
                break

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if elapsed_ms >= latency_budget_ms:
                degraded = True
                stop_reason = "latency_budget_exceeded"
                break

            if iteration >= resolved_max_iterations:
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
            "node_gate_report": dict(best_quality.get("node_gate_report") or {}),
            "iteration_trace": iteration_trace,
            "iterations": len(iteration_trace),
            "degraded": degraded,
            "stop_reason": stop_reason,
            "build_mode": "compose_agent",
        }

    async def _apply_deepseek_assembly_decision(
        self,
        *,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
        page: int,
        latency_budget_ms: int,
        user_id: int = 0,
        user_intent: str = "standard",
    ) -> Dict[str, Any]:
        """Let DeepSeek make a lightweight final assembly decision with strict local validation."""
        cloned = json.loads(json.dumps(ui_plan, ensure_ascii=False))
        trace_meta = dict(cloned.get("trace_meta") or {})
        components = list(cloned.get("components") or [])
        candidate_types = {"SectionHeading", "ParagraphProse", "ListBlock"}
        allowed_override_types = {"SectionHeading", "ParagraphProse", "ListBlock"}

        def _mark_skip(reason: str) -> Dict[str, Any]:
            trace_meta["assembly_used"] = False
            trace_meta["assembly_fallback_reason"] = str(reason)
            cloned["trace_meta"] = trace_meta
            return cloned

        if not bool(getattr(settings, "reader_compose_layout_llm_enabled", True)):
            return _mark_skip("assembly_disabled")
        if not components:
            return _mark_skip("no_components")
        if (
            bool(getattr(settings, "reader_agent_component_stream_enabled", True))
            and isinstance((base_payload.get("page_structure_v3") or {}).get("block_groups"), list)
            and len(list((base_payload.get("page_structure_v3") or {}).get("block_groups") or [])) > 0
        ):
            try:
                runtime_result = await self._compose_agent_runtime.run_component_assembly(
                    user_id=int(user_id or 0),
                    page=int(page),
                    user_intent=str(user_intent or "standard"),
                    ui_plan=cloned,
                    page_structure_v3=dict(base_payload.get("page_structure_v3") or {}),
                    layout_advice_v3=dict(base_payload.get("layout_advice_v3") or {}),
                    latency_budget_ms=int(latency_budget_ms),
                )
                if bool(runtime_result.get("used")):
                    ui_ops = [row for row in list(runtime_result.get("ui_ops") or []) if isinstance(row, dict)]
                    apply_result = self._apply_ui_ops_to_plan(ui_plan=cloned, ui_ops=ui_ops)
                    apply_errors = [str(item).strip() for item in list(apply_result.get("errors") or []) if str(item).strip()]
                    if not apply_errors:
                        cloned = dict(apply_result.get("ui_plan") or cloned)
                        trace_meta["assembly_used"] = True
                        trace_meta["assembly_model"] = str(
                            runtime_result.get("model") or getattr(settings, "deepseek_model", "deepseek-chat")
                        )
                        trace_meta["assembly_patch_protocol"] = "ui_ops_v1"
                        trace_meta["assembly_ui_ops_count"] = len(ui_ops)
                        trace_meta["assembly_agent_trace_count"] = len(list(runtime_result.get("agent_trace") or []))
                        trace_meta["assembly_agent_tool_call_count"] = len(list(runtime_result.get("agent_tool_calls") or []))
                        trace_meta["assembly_agent_summary"] = str(runtime_result.get("agent_summary") or "")
                        trace_meta.pop("assembly_fallback_reason", None)
                        cloned["trace_meta"] = trace_meta
                        cloned["ui_ops"] = ui_ops
                        cloned["agent_trace"] = list(runtime_result.get("agent_trace") or [])
                        cloned["agent_tool_calls"] = list(runtime_result.get("agent_tool_calls") or [])
                        return cloned
                    trace_meta["assembly_agent_validation_errors"] = apply_errors
                    cloned["trace_meta"] = trace_meta
                else:
                    runtime_fallback_reason = str(runtime_result.get("fallback_reason") or "").strip()
                    if runtime_fallback_reason:
                        trace_meta["assembly_agent_fallback_reason"] = runtime_fallback_reason
                        cloned["trace_meta"] = trace_meta
            except Exception as exc:
                logger.debug(f"[ReaderComposeService] compose-agent ui_ops path failed page={page}: {exc}")

        candidate_nodes = [
            node for node in components if isinstance(node, dict) and str(node.get("type") or "") in candidate_types
        ]
        if len(candidate_nodes) < 2:
            return _mark_skip("insufficient_candidates")

        segment_rows = [
            row
            for row in list((base_payload.get("segment_map") or {}).get("segments") or [])
            if isinstance(row, dict)
        ]
        if not segment_rows:
            page_structure = dict(base_payload.get("page_structure_v3") or {})
            for row in list(page_structure.get("block_groups") or []):
                if not isinstance(row, dict):
                    continue
                block_id = str(row.get("block_id") or "").strip()
                text = self._normalize_spaces(str(row.get("text") or ""))
                if not block_id or not text:
                    continue
                segment_rows.append(
                    {
                        "segment_id": block_id,
                        "kind_hint": str(row.get("kind") or ""),
                        "component_hint": str(row.get("component_hint") or ""),
                        "block_ids": [block_id],
                        "line_ids": [],
                        "word_ids": [str(item).strip() for item in list(row.get("word_ids") or []) if str(item).strip()],
                        "resolved_text": text,
                        "reason": "from_page_structure_v3",
                        "confidence": self._safe_float(row.get("confidence"), 0.86),
                    }
                )
        if not segment_rows:
            return _mark_skip("no_ai_blocks")

        max_blocks = max(12, int(getattr(settings, "reader_compose_layout_llm_max_blocks", 80) or 80))
        compact_nodes: List[Dict[str, Any]] = []
        node_id_set: set[str] = set()
        for idx, node in enumerate(components[: max_blocks * 2]):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            node_type = str(node.get("type") or "").strip()
            if not node_id or node_id in node_id_set:
                continue
            node_id_set.add(node_id)
            text = self._normalize_spaces(self._extract_node_text(node))[:220]
            compact_nodes.append(
                {
                    "id": node_id,
                    "type": node_type,
                    "zone_type": str(node.get("zone_type") or ""),
                    "text": text,
                    "anchor_count": len(list(node.get("source_anchor_refs") or [])),
                    "index": idx,
                }
            )
            if len(compact_nodes) >= max_blocks:
                break

        if len(compact_nodes) < 2:
            return _mark_skip("insufficient_candidates")

        compact_segments: List[Dict[str, Any]] = []
        for row in segment_rows[: max_blocks]:
            if not isinstance(row, dict):
                continue
            line_ids = [str(item).strip() for item in list(row.get("line_ids") or [])[:12] if str(item).strip()]
            word_ids = [str(item).strip() for item in list(row.get("word_ids") or [])[:120] if str(item).strip()]
            block_ids = [str(item).strip() for item in list(row.get("block_ids") or [])[:8] if str(item).strip()]
            compact_segments.append(
                {
                    "segment_id": str(row.get("segment_id") or ""),
                    "kind_hint": str(row.get("kind_hint") or row.get("kind") or ""),
                    "component_hint": str(row.get("component_hint") or row.get("ui_component") or ""),
                    "block_ids": block_ids,
                    "line_id_count": len(line_ids),
                    "word_id_count": len(word_ids),
                    "text": self._normalize_spaces(str(row.get("resolved_text") or row.get("text") or ""))[:200],
                    "reason": self._normalize_spaces(str(row.get("reason") or ""))[:160],
                    "confidence": float(row.get("confidence") or 0.0),
                }
            )

        compact_channels = dict(base_payload.get("layout_channels") or {})
        layout_advice_v3 = dict(base_payload.get("layout_advice_v3") or {})
        compact_advice = {
            "ordered_block_ids": [
                str(item).strip()
                for item in list(layout_advice_v3.get("ordered_block_ids") or [])[:max_blocks]
                if str(item).strip()
            ],
            "suggested_components": [
                row
                for row in list(layout_advice_v3.get("suggested_components") or [])[:max_blocks]
                if isinstance(row, dict)
            ],
            "grouping_hints": [
                row
                for row in list(layout_advice_v3.get("grouping_hints") or [])[:max_blocks]
                if isinstance(row, dict)
            ],
            "visual_hints": [
                row
                for row in list(layout_advice_v3.get("visual_hints") or [])[:max_blocks]
                if isinstance(row, dict)
            ],
        }
        prompt = (
            "You are a strict UI assembly planner for a literature reader.\n"
            "Task: adjust component order and minor component-type override for better readability.\n"
            "Hard rules:\n"
            "1) Return JSON only.\n"
            "2) Output schema: "
            '{"ordered_node_ids":[],"drop_node_ids":[],"type_override":{"node_id":"SectionHeading|ParagraphProse|ListBlock"}}\n'
            "3) ordered_node_ids and drop_node_ids must come from provided node IDs only.\n"
            "4) type_override keys must be existing node IDs only.\n"
            "5) type_override values must be one of: SectionHeading, ParagraphProse, ListBlock.\n"
            "6) Do NOT create/edit evidence anchors, coords, block IDs, or facts.\n"
            "7) Prefer one semantic paragraph per ParagraphProse; avoid merging adjacent paragraphs.\n"
            "8) Avoid false headings from inline words.\n"
            f"page: {int(page)}\n"
            f"allowed_component_types: {json.dumps(sorted(list(allowed_override_types)), ensure_ascii=False)}\n"
            f"nodes: {json.dumps(compact_nodes, ensure_ascii=False)}\n"
            f"segment_hints: {json.dumps(compact_segments, ensure_ascii=False)}\n"
            f"layout_advice_v3: {json.dumps(compact_advice, ensure_ascii=False)}\n"
            f"layout_channels: {json.dumps(compact_channels, ensure_ascii=False)}"
        )

        try:
            llm = await get_llm_service()
            configured_timeout = float(getattr(settings, "reader_compose_layout_llm_timeout_seconds", 120) or 120)
            if configured_timeout <= 0:
                result = await llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=720,
                )
            else:
                timeout_seconds = max(6.0, configured_timeout)
                result = await asyncio.wait_for(
                    llm.chat(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=720,
                    ),
                    timeout=timeout_seconds,
                )
            decision = self._extract_json_dict(str((result or {}).get("content") or ""))
            if not isinstance(decision, dict):
                return _mark_skip("assembly_invalid_json")

            existing_ids = [str(item.get("id") or "") for item in compact_nodes]
            existing_set = set(existing_ids)

            raw_ordered = list(decision.get("ordered_node_ids") or [])
            ordered_ids: List[str] = []
            seen_ids: set[str] = set()
            for item in raw_ordered:
                node_id = str(item or "").strip()
                if not node_id:
                    continue
                if node_id not in existing_set:
                    return _mark_skip("assembly_invalid_node_id_in_order")
                if node_id in seen_ids:
                    continue
                seen_ids.add(node_id)
                ordered_ids.append(node_id)

            raw_drop = list(decision.get("drop_node_ids") or [])
            drop_ids: set[str] = set()
            for item in raw_drop:
                node_id = str(item or "").strip()
                if not node_id:
                    continue
                if node_id not in existing_set:
                    return _mark_skip("assembly_invalid_node_id_in_drop")
                drop_ids.add(node_id)

            raw_override = decision.get("type_override") or {}
            type_override: Dict[str, str] = {}
            if isinstance(raw_override, dict):
                for raw_id, raw_type in raw_override.items():
                    node_id = str(raw_id or "").strip()
                    target_type = str(raw_type or "").strip()
                    if not node_id:
                        continue
                    if node_id not in existing_set:
                        return _mark_skip("assembly_invalid_node_id_in_override")
                    if target_type not in allowed_override_types:
                        continue
                    type_override[node_id] = target_type

            top_components = [node for node in components if isinstance(node, dict)]
            id_to_node = {str(node.get("id") or ""): node for node in top_components if str(node.get("id") or "")}

            reordered: List[Dict[str, Any]] = []
            consumed: set[str] = set()
            for node_id in ordered_ids:
                if node_id in consumed or node_id in drop_ids:
                    continue
                node = id_to_node.get(node_id)
                if not isinstance(node, dict):
                    continue
                reordered.append(node)
                consumed.add(node_id)
            for node in top_components:
                node_id = str(node.get("id") or "")
                if not node_id or node_id in consumed or node_id in drop_ids:
                    continue
                reordered.append(node)
                consumed.add(node_id)

            def _coerce_type(node: Dict[str, Any], target_type: str) -> Dict[str, Any]:
                source_type = str(node.get("type") or "")
                if source_type not in allowed_override_types or target_type not in allowed_override_types:
                    return node
                if source_type == target_type:
                    return node

                patched = json.loads(json.dumps(node, ensure_ascii=False))
                props = dict(patched.get("props") or {})
                text = self._normalize_spaces(str(props.get("text") or ""))
                if not text and source_type == "ListBlock":
                    items = props.get("items")
                    if isinstance(items, list):
                        text = self._normalize_spaces(" ".join(str(item) for item in items if str(item).strip()))
                if not text:
                    text = self._normalize_spaces(self._extract_node_text(patched))

                if target_type == "SectionHeading":
                    if not text:
                        return node
                    props["text"] = text[:220]
                    try:
                        props["level"] = int(props.get("level") or 2)
                    except Exception:
                        props["level"] = 2
                    patched["type"] = "SectionHeading"
                    patched["props"] = props
                    return patched

                if target_type == "ParagraphProse":
                    if not text:
                        return node
                    props["text"] = text
                    patched["type"] = "ParagraphProse"
                    patched["props"] = props
                    return patched

                # target ListBlock
                items = props.get("items")
                normalized_items: List[str] = []
                if isinstance(items, list):
                    normalized_items = [
                        self._normalize_spaces(str(item))
                        for item in items
                        if self._normalize_spaces(str(item))
                    ]
                if not normalized_items:
                    source_text = text or self._normalize_spaces(self._extract_node_text(patched))
                    source_text = self._normalize_spaces(source_text)
                    if not source_text:
                        return node
                    split_items = [
                        self._normalize_spaces(item)
                        for item in re.split(r"[；;。.!?！？]\s*", source_text)
                        if self._normalize_spaces(item)
                    ]
                    normalized_items = split_items[:8] if split_items else [source_text]
                props["items"] = normalized_items
                patched["type"] = "ListBlock"
                patched["props"] = props
                return patched

            patched_components: List[Dict[str, Any]] = []
            for node in reordered:
                node_id = str(node.get("id") or "")
                target_type = str(type_override.get(node_id) or "")
                if target_type:
                    patched_components.append(_coerce_type(node, target_type))
                else:
                    patched_components.append(node)

            cloned["components"] = patched_components
            trace_meta["assembly_used"] = True
            trace_meta["assembly_model"] = str((result or {}).get("model") or getattr(settings, "deepseek_model", "deepseek-chat"))
            trace_meta["assembly_ordered_count"] = len(ordered_ids)
            trace_meta["assembly_drop_count"] = len(drop_ids)
            trace_meta["assembly_override_count"] = len(type_override)
            trace_meta.pop("assembly_fallback_reason", None)
            cloned["trace_meta"] = trace_meta
            return cloned
        except asyncio.TimeoutError:
            return _mark_skip("assembly_timeout")
        except Exception as exc:
            logger.debug(f"[ReaderComposeService] deepseek assembly decision failed page={page}: {exc}")
            return _mark_skip("assembly_exception")

    @staticmethod
    def _apply_ui_ops_to_plan(
        *,
        ui_plan: Dict[str, Any],
        ui_ops: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cloned = json.loads(json.dumps(ui_plan, ensure_ascii=False))
        components = [row for row in list(cloned.get("components") or []) if isinstance(row, dict)]
        errors: List[str] = []

        def _index_by_id(component_id: str) -> int:
            for idx, node in enumerate(components):
                if str(node.get("id") or "").strip() == component_id:
                    return idx
            return -1

        for row in list(ui_ops or []):
            if not isinstance(row, dict):
                continue
            op = str(row.get("op") or "").strip()
            if op == "reorder_components":
                ordered_ids = [
                    str(item).strip()
                    for item in list(row.get("ordered_component_ids") or [])
                    if str(item).strip()
                ]
                id_to_node = {
                    str(node.get("id") or "").strip(): node
                    for node in components
                    if str(node.get("id") or "").strip()
                }
                ordered_set = set(ordered_ids)
                reordered: List[Dict[str, Any]] = []
                for cid in ordered_ids:
                    node = id_to_node.get(cid)
                    if isinstance(node, dict):
                        reordered.append(node)
                for node in components:
                    cid = str(node.get("id") or "").strip()
                    if not cid or cid in ordered_set:
                        continue
                    reordered.append(node)
                components = reordered
                continue

            if op == "remove_component":
                component_id = str(row.get("component_id") or "").strip()
                if not component_id:
                    errors.append("remove_component_missing_id")
                    continue
                before = len(components)
                components = [
                    node for node in components if str(node.get("id") or "").strip() != component_id
                ]
                if len(components) == before:
                    errors.append("remove_component_not_found")
                continue

            if op == "update_component_props":
                component_id = str(row.get("component_id") or "").strip()
                props_patch = dict(row.get("props_patch") or {})
                idx = _index_by_id(component_id)
                if idx < 0:
                    errors.append("update_component_not_found")
                    continue
                node = json.loads(json.dumps(components[idx], ensure_ascii=False))
                props = dict(node.get("props") or {})
                props.update(props_patch)
                node["props"] = props
                components[idx] = node
                continue

            if op == "insert_component":
                component = row.get("component")
                if not isinstance(component, dict):
                    errors.append("insert_component_missing_component")
                    continue
                new_node = json.loads(json.dumps(component, ensure_ascii=False))
                if not isinstance(new_node.get("children"), list):
                    new_node["children"] = []
                if not isinstance(new_node.get("source_anchor_refs"), list):
                    new_node["source_anchor_refs"] = []
                after_component_id = str(row.get("after_component_id") or "").strip()
                if after_component_id:
                    idx = _index_by_id(after_component_id)
                    if idx < 0:
                        errors.append("insert_after_not_found")
                        continue
                    components.insert(idx + 1, new_node)
                else:
                    components.append(new_node)
                continue

            errors.append(f"unsupported_ui_op:{op}")

        cloned["components"] = components
        return {"ui_plan": cloned, "errors": errors}

    async def _apply_multimodal_layout_assist(
        self,
        *,
        paper: Paper,
        page: int,
        base_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Strict 4-layer contract path: DocMind -> Stage1 -> Stage2 -> assembly."""
        started_at = time.perf_counter()
        payload = json.loads(json.dumps(base_payload, ensure_ascii=False))
        payload.setdefault("mm_assist_meta", {})

        page_structure_v3 = dict(payload.get("page_structure_v3") or {})
        if str(page_structure_v3.get("source") or "").strip().lower() != "document_mind":
            raise RenderPipelineContractError(
                code="DOCMIND_LAYOUT_DIGEST_EMPTY",
                stage="docmind",
                message="Strict pipeline requires page_structure_v3.source=document_mind",
                details={
                    "paper_id": int(paper.id),
                    "page": int(page),
                    "source": str(page_structure_v3.get("source") or ""),
                },
            )

        docmind_structure = dict(payload.get("docmind_structure") or {})
        digest_bundle = build_docmind_layout_digest(docmind_structure, int(page))

        # Keep Stage1 scope page-local: only annotate layouts that are already mapped to current-page block_groups.
        page_block_groups = [
            row
            for row in list(page_structure_v3.get("block_groups") or [])
            if isinstance(row, dict)
        ]
        page_layout_ids = {
            str(row.get("layout_unique_id") or "").strip()
            for row in page_block_groups
            if str(row.get("layout_unique_id") or "").strip()
        }
        if page_layout_ids:
            scoped_rows = [
                row
                for row in list(digest_bundle.layout_digest or [])
                if str(row.get("layout_id") or "").strip() in page_layout_ids
            ]
            if scoped_rows:
                known_layout_ids = [
                    str(row.get("layout_id") or "").strip()
                    for row in scoped_rows
                    if str(row.get("layout_id") or "").strip()
                ]
                digest_bundle = LayoutDigestBundle(
                    page=int(page),
                    layout_digest=scoped_rows,
                    known_layout_ids=known_layout_ids,
                    layout_index={layout_id: idx for idx, layout_id in enumerate(known_layout_ids)},
                )

        prompt_payload: Dict[str, Any] = {}
        path = self._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
            user_id=int(paper.user_id),
            paper_id=int(paper.id),
            paper_title=paper.title,
            paper_pdf_path=paper.pdf_path,
        )
        if path and os.path.exists(path):
            try:
                prompt_payload = await self._mm_layout_service.build_mm_prompt_payload(
                    pdf_path=path,
                    page=int(page),
                    base_payload=payload,
                )
            except Exception as exc:
                logger.warning(
                    f"[ReaderComposeService] build_mm_prompt_payload failed in strict path page={page}: {exc}"
                )
                prompt_payload = {}

        layout_meta = {
            "paper_id": int(paper.id),
            "page": int(page),
            "pipeline": "reader_workbench_v2_strict_4layer",
            "visual_reference_only": True,
        }
        if isinstance(prompt_payload.get("layout_meta"), dict):
            layout_meta.update(dict(prompt_payload.get("layout_meta") or {}))
            layout_meta["visual_reference_only"] = True

        image_rows = [
            row
            for row in list(prompt_payload.get("images") or [])[:1]
            if isinstance(row, dict)
        ]

        stage1_payload = {
            "layout_meta": layout_meta,
            "images": image_rows,
            "docmind_layout_digest": list(digest_bundle.layout_digest),
        }
        stage1_structural_annotations, stage1_meta = (
            await self._mm_layout_service.build_stage1_structural_annotations(
                prompt_payload=stage1_payload,
                known_layout_ids=list(digest_bundle.known_layout_ids),
            )
        )

        stage1_block_map = {
            str(row.get("layout_id") or "").strip(): dict(row)
            for row in list(stage1_structural_annotations.get("blocks") or [])
            if isinstance(row, dict) and str(row.get("layout_id") or "").strip()
        }
        stage2_layout_digest: List[Dict[str, Any]] = []
        for row in list(digest_bundle.layout_digest):
            layout_id = str(row.get("layout_id") or "").strip()
            if not layout_id:
                continue
            anno = dict(stage1_block_map.get(layout_id) or {})
            stage2_layout_digest.append(
                {
                    "layout_id": layout_id,
                    "bbox": list(row.get("bbox") or [0.0, 0.0, 0.0, 0.0])[:4],
                    "role": str(anno.get("role") or "unknown"),
                    "text_preview": self._normalize_spaces(str(row.get("text_preview") or ""))[:200],
                }
            )

        allowed_components = [
            "SectionHeading",
            "ParagraphProse",
            "ListBlock",
            "ContextRail",
            "FigurePanel",
            "TablePanel",
            "KeyTakeaways",
            "AnswerCard",
            "CitationLinks",
            "InlineQuerySlot",
        ]
        stage2_payload = {
            "layout_meta": layout_meta,
            "images": image_rows,
            "structural_annotations": dict(stage1_structural_annotations),
            "layout_digest": stage2_layout_digest,
            "allowed_components": list(allowed_components),
        }
        stage2_design_layout, stage2_meta = await self._mm_layout_service.build_stage2_design_layout(
            prompt_payload=stage2_payload,
            known_layout_ids=list(digest_bundle.known_layout_ids),
            allowed_components=list(allowed_components),
        )
        stage2_design_layout = materialize_stage2_plan(stage2_design_layout, digest_bundle)

        block_groups = [
            row
            for row in list(page_structure_v3.get("block_groups") or [])
            if isinstance(row, dict)
        ]
        layout_to_block_ids: Dict[str, List[str]] = {}
        for row in block_groups:
            layout_id = str(row.get("layout_unique_id") or "").strip()
            block_id = str(row.get("block_id") or "").strip()
            canonical_block_id = self._normalize_canonical_block_id(page=page, raw_id=block_id)
            if not layout_id or not canonical_block_id:
                continue
            bucket = layout_to_block_ids.setdefault(layout_id, [])
            if canonical_block_id not in bucket:
                bucket.append(canonical_block_id)

        component_to_kind = {
            "SectionHeading": "heading",
            "ParagraphProse": "paragraph",
            "ListBlock": "list_item",
            "ContextRail": "paragraph",
            "FigurePanel": "caption",
            "TablePanel": "caption",
            "KeyTakeaways": "paragraph",
            "AnswerCard": "paragraph",
            "CitationLinks": "paragraph",
            "InlineQuerySlot": "paragraph",
        }

        ordered_block_ids: List[str] = []
        suggested_components: List[Dict[str, Any]] = []
        stage2_segments: List[Dict[str, Any]] = []
        for seg_idx, row in enumerate(list(stage2_design_layout.get("page_layout") or []), start=1):
            if not isinstance(row, dict):
                continue
            component = str(row.get("component") or "").strip()
            source_layout_ids = [
                str(item).strip()
                for item in list(row.get("source_layout_ids") or [])
                if str(item).strip()
            ]
            source_block_ids: List[str] = []
            missing_layout_ids: List[str] = []
            for layout_id in source_layout_ids:
                mapped = list(layout_to_block_ids.get(layout_id) or [])
                if not mapped:
                    missing_layout_ids.append(layout_id)
                    continue
                for block_id in mapped:
                    if block_id not in source_block_ids:
                        source_block_ids.append(block_id)
            if missing_layout_ids:
                raise RenderPipelineContractError(
                    code="STAGE2_LAYOUT_ID_COVERAGE_MISMATCH",
                    stage="stage2",
                    message="Stage2 source_layout_ids cannot be mapped to block_groups",
                    details={
                        "missing_layout_ids": missing_layout_ids[:80],
                        "component": component,
                    },
                )
            if not source_block_ids:
                continue
            for block_id in source_block_ids:
                if block_id not in ordered_block_ids:
                    ordered_block_ids.append(block_id)
            suggested_components.append(
                {
                    "block_ids": list(source_block_ids),
                    "component": component,
                    "source_layout_ids": list(source_layout_ids),
                    "reason": "stage2_design_layout",
                    "props": dict(row.get("props") or {}) if isinstance(row.get("props"), dict) else {},
                }
            )
            kind_hint = str(component_to_kind.get(component) or "paragraph")
            stage2_segments.append(
                {
                    "segment_id": f"stage2_seg_{seg_idx:03d}",
                    "kind": kind_hint,
                    "kind_hint": kind_hint,
                    "component_hint": component,
                    "line_ids": [],
                    "evidence_line_ids": [],
                    "word_ids": [],
                    "char_ranges": [],
                    "block_ids": list(source_block_ids),
                    "title": "",
                    "resolved_text": "",
                    "sort_order": seg_idx,
                    "continuation": "none",
                    "reason": "stage2_design_layout",
                    "confidence": 0.9,
                }
            )

        pipeline_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        payload["stage1_structural_annotations"] = dict(stage1_structural_annotations)
        payload["stage2_design_layout"] = dict(stage2_design_layout)
        payload["pipeline_contract_meta"] = {
            "used": True,
            "pipeline": "reader_workbench_v2_strict_4layer",
            "docmind_truth": True,
            "visual_reference_only": True,
            "stage1": dict(stage1_meta or {}),
            "stage2": dict(stage2_meta or {}),
            "elapsed_ms": pipeline_elapsed_ms,
        }
        payload["layout_advice_v3"] = {
            "source": "stage2_design_v1",
            "ordered_block_ids": list(ordered_block_ids),
            "suggested_components": suggested_components[:240],
            "grouping_hints": [],
            "visual_hints": [],
            "notes": [
                f"unused_layout_count={len(list(stage2_design_layout.get('unused_layout_ids') or []))}",
                "visual_reference_only=true",
            ],
            "unused_layout_ids": [
                str(item).strip()
                for item in list(stage2_design_layout.get("unused_layout_ids") or [])
                if str(item).strip()
            ],
        }
        payload["segment_map"] = {
            "source": "stage2_design_v1",
            "segments": stage2_segments,
            "counts": {
                "segment_count": int(len(stage2_segments)),
                "used_layout_count": int(
                    len(
                        {
                            layout_id
                            for row in list(stage2_design_layout.get("page_layout") or [])
                            if isinstance(row, dict)
                            for layout_id in list(row.get("source_layout_ids") or [])
                            if str(layout_id).strip()
                        }
                    )
                ),
                "unused_layout_count": int(len(list(stage2_design_layout.get("unused_layout_ids") or []))),
            },
        }
        payload["segment_map_meta"] = {
            "used": True,
            "reason": "stage2_design_layout_applied",
            "source": "stage2_design_v1",
            "model": str(stage2_meta.get("model") or ""),
            "fallback_used": bool(stage2_meta.get("fallback_used")),
            "error": None,
        }
        payload["mm_parser_meta"] = dict(stage1_meta or {})
        payload["layout_advice_meta"] = {
            "used": True,
            "reason": "stage2_design_layout_applied",
            "source": "stage2_design_v1",
            "model": str(stage2_meta.get("model") or ""),
            "fallback_used": bool(stage2_meta.get("fallback_used")),
            "error": None,
        }
        payload["qwen_plan_meta"] = {
            "used": True,
            "reason": "strict_stage1_stage2_contract_path",
            "stage1_model": str(stage1_meta.get("model") or ""),
            "stage2_model": str(stage2_meta.get("model") or ""),
            "stage1_fallback_used": bool(stage1_meta.get("fallback_used")),
            "stage2_fallback_used": bool(stage2_meta.get("fallback_used")),
            "prompt_version": str(getattr(settings, "reader_mm_prompt_version", "mm_layout_v1")),
            "pipeline_contract": True,
        }
        payload["mm_assist_meta"] = {
            "used": True,
            "degraded": False,
            "reason": "strict_stage1_stage2_contract_applied",
            "model": str(stage2_meta.get("model") or stage1_meta.get("model") or ""),
            "fallback_used": bool(stage1_meta.get("fallback_used") or stage2_meta.get("fallback_used")),
            "prompt_version": str(getattr(settings, "reader_mm_prompt_version", "mm_layout_v1")),
            "visual_reference_only": True,
        }
        payload["qwen_layout_plan_v2"] = {}

        return self._ensure_layout_channels(payload)

    def _build_segment_map_from_parser_advice(
        self,
        *,
        page: int,
        parser_advice: Dict[str, Any],
        prompt_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(parser_advice, dict):
            return {}

        line_candidates = list((prompt_payload or {}).get("line_candidates") or [])
        line_order_map: Dict[str, int] = {}
        line_text_map: Dict[str, str] = {}
        for idx, row in enumerate(line_candidates):
            if not isinstance(row, dict):
                continue
            line_id = str(row.get("line_id") or "").strip()
            if not line_id:
                continue
            try:
                line_order = int(row.get("order") or idx)
            except Exception:
                line_order = idx
            line_order_map[line_id] = line_order
            line_text_map[line_id] = self._normalize_spaces(str(row.get("text") or ""))

        native_extract = dict((prompt_payload or {}).get("native_page_extract") or {})
        native_words = [row for row in list(native_extract.get("words") or []) if isinstance(row, dict)]
        word_order_map: Dict[str, int] = {}
        word_text_map: Dict[str, str] = {}
        for idx, row in enumerate(native_words):
            word_id = str(row.get("word_id") or "").strip()
            if not word_id:
                continue
            word_order_map[word_id] = idx
            word_text_map[word_id] = self._normalize_spaces(str(row.get("text") or ""))
        native_chars = [row for row in list(native_extract.get("chars") or []) if isinstance(row, dict)]
        char_order_map: Dict[str, int] = {}
        for idx, row in enumerate(native_chars):
            char_id = str(row.get("char_id") or "").strip()
            if char_id:
                char_order_map[char_id] = idx

        heading_groups = [row for row in list(parser_advice.get("heading_groups") or []) if isinstance(row, dict)]
        paragraph_groups = [row for row in list(parser_advice.get("paragraph_groups") or []) if isinstance(row, dict)]
        line_labels = [row for row in list(parser_advice.get("line_labels") or []) if isinstance(row, dict)]
        figure_groups = [row for row in list(parser_advice.get("figure_groups") or []) if isinstance(row, dict)]
        block_groups = [row for row in list(parser_advice.get("block_groups") or []) if isinstance(row, dict)]

        heading_title_by_id: Dict[str, str] = {}
        heading_line_ids: set[str] = set()
        segments: List[Dict[str, Any]] = []
        segment_seq = 0

        def _ordered_line_ids(values: Any, *, limit: int) -> List[str]:
            rows = [str(item).strip() for item in list(values or [])[:limit] if str(item).strip()]
            rows = list(dict.fromkeys(rows))
            return sorted(rows, key=lambda line_id: (line_order_map.get(line_id, 10_000_000), line_id))

        def _next_segment_id(prefix: str) -> str:
            nonlocal segment_seq
            segment_seq += 1
            return f"{prefix}_{segment_seq}"

        if block_groups:
            ordered_block_groups = sorted(
                block_groups,
                key=lambda row: (
                    self._safe_int(row.get("reading_order"), 10_000_000),
                    min(
                        [
                            line_order_map.get(str(item).strip(), 10_000_000)
                            for item in list(row.get("line_ids") or [])
                            if str(item).strip()
                        ]
                        or [10_000_000]
                    ),
                    str(row.get("block_id") or ""),
                ),
            )
            for row in ordered_block_groups:
                kind_raw = str(row.get("kind") or "").strip().lower()
                canonical_block_id = self._normalize_canonical_block_id(
                    page=page,
                    raw_id=str(row.get("block_id") or ""),
                )
                line_ids = _ordered_line_ids(row.get("line_ids"), limit=120)
                word_ids = [
                    str(item).strip()
                    for item in list(row.get("word_ids") or [])[:320]
                    if str(item).strip()
                ]
                char_ranges = [
                    {
                        "start_char_id": str(item.get("start_char_id") or "").strip(),
                        "end_char_id": str(item.get("end_char_id") or "").strip(),
                    }
                    for item in list(row.get("char_ranges") or [])[:200]
                    if isinstance(item, dict)
                    and str(item.get("start_char_id") or "").strip()
                    and str(item.get("end_char_id") or "").strip()
                ]
                if not line_ids and not word_ids and not char_ranges:
                    continue
                title = self._normalize_spaces(str(row.get("title") or ""))
                parent_node_id = self._normalize_spaces(str(row.get("parent_node_id") or "")).lower()[:40]
                try:
                    confidence = float(row.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0

                resolved_text = self._normalize_spaces(" ".join(line_text_map.get(line_id, "") for line_id in line_ids))
                if not resolved_text and word_ids:
                    resolved_text = self._normalize_spaces(" ".join(word_text_map.get(word_id, "") for word_id in word_ids))
                if not resolved_text:
                    resolved_text = title

                if kind_raw == "heading":
                    heading_id = self._normalize_spaces(str(row.get("block_id") or parent_node_id or "")).lower()[:40]
                    if heading_id:
                        heading_title_by_id[heading_id] = title
                    heading_line_ids.update(set(line_ids))
                    segments.append(
                        {
                            "segment_id": str(row.get("block_id") or _next_segment_id("h")).strip() or _next_segment_id("h"),
                            "kind": "heading",
                            "kind_hint": "heading",
                            "component_hint": "SectionHeading",
                            "line_ids": line_ids,
                            "evidence_line_ids": list(line_ids),
                            "word_ids": word_ids,
                            "char_ranges": char_ranges,
                            "block_ids": [canonical_block_id] if canonical_block_id else [],
                            "title": title,
                            "resolved_text": resolved_text,
                            "sort_order": min(
                                [line_order_map.get(line_id, 10_000_000) for line_id in list(line_ids)] or
                                [word_order_map.get(word_id, 10_000_000) for word_id in list(word_ids)] or
                                [10_000_000]
                            ),
                            "continuation": "none",
                            "reason": "parser_block_group_heading",
                            "confidence": max(0.0, min(1.0, confidence)),
                        }
                    )
                    continue

                zone_type = str(row.get("zone_type") or "main_body").strip().lower()
                if zone_type != "main_body":
                    continue
                cleaned_line_ids = [line_id for line_id in line_ids if line_id not in heading_line_ids] or list(line_ids)
                section_title = title
                if not section_title and parent_node_id:
                    section_title = heading_title_by_id.get(parent_node_id, "")
                component_hint = "ParagraphProse"
                kind = "paragraph"
                if kind_raw == "list_item":
                    component_hint = "ListBlock"
                    kind = "list_item"
                elif kind_raw in {"caption", "table_caption"}:
                    component_hint = "ParagraphProse"
                    kind = "caption"
                segments.append(
                    {
                        "segment_id": str(row.get("block_id") or _next_segment_id("p")).strip() or _next_segment_id("p"),
                        "kind": kind,
                        "kind_hint": kind,
                        "component_hint": component_hint,
                        "line_ids": cleaned_line_ids,
                        "evidence_line_ids": list(cleaned_line_ids),
                        "word_ids": word_ids,
                        "char_ranges": char_ranges,
                        "block_ids": [canonical_block_id] if canonical_block_id else [],
                        "title": section_title,
                        "resolved_text": resolved_text,
                        "sort_order": min(
                            [line_order_map.get(line_id, 10_000_000) for line_id in list(cleaned_line_ids)] or
                            [word_order_map.get(word_id, 10_000_000) for word_id in list(word_ids)] or
                            [
                                min(
                                    char_order_map.get(str(rng.get("start_char_id") or "").strip(), 10_000_000),
                                    char_order_map.get(str(rng.get("end_char_id") or "").strip(), 10_000_000),
                                )
                                for rng in list(char_ranges)
                                if isinstance(rng, dict)
                            ] or
                            [10_000_000]
                        ),
                        "continuation": "none",
                        "reason": "parser_block_group_body",
                        "confidence": max(0.0, min(1.0, confidence)),
                    }
                )

        if not segments:
            for row in heading_groups:
                line_ids = _ordered_line_ids(row.get("line_ids"), limit=8)
                if not line_ids:
                    continue
                heading_id = self._normalize_spaces(str(row.get("heading_id") or "")).lower()[:40]
                title = self._normalize_spaces(str(row.get("title") or ""))
                if not title:
                    title = self._normalize_spaces(" ".join(line_text_map.get(line_id, "") for line_id in line_ids))
                if heading_id:
                    heading_title_by_id[heading_id] = title
                heading_line_ids.update(set(line_ids))
                try:
                    confidence = float(row.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0
                segments.append(
                    {
                        "segment_id": _next_segment_id("h"),
                        "kind": "heading",
                        "kind_hint": "heading",
                        "component_hint": "SectionHeading",
                        "line_ids": line_ids,
                        "evidence_line_ids": list(line_ids),
                        "block_ids": [],
                        "title": title,
                        "continuation": "none",
                        "reason": "parser_heading_group",
                        "confidence": max(0.0, min(1.0, confidence)),
                    }
                )

            for row in paragraph_groups:
                line_ids = _ordered_line_ids(row.get("line_ids"), limit=80)
                if not line_ids:
                    continue
                zone_type = str(row.get("zone_type") or "main_body").strip().lower()
                if zone_type != "main_body":
                    continue
                cleaned_line_ids = [line_id for line_id in line_ids if line_id not in heading_line_ids] or list(line_ids)
                heading_id = self._normalize_spaces(str(row.get("heading_id") or "")).lower()[:40]
                title = heading_title_by_id.get(heading_id, "")
                try:
                    confidence = float(row.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0
                segments.append(
                    {
                        "segment_id": str(row.get("paragraph_id") or _next_segment_id("p")).strip() or _next_segment_id("p"),
                        "kind": "paragraph",
                        "kind_hint": "paragraph",
                        "component_hint": "ParagraphProse",
                        "line_ids": cleaned_line_ids,
                        "evidence_line_ids": list(cleaned_line_ids),
                        "block_ids": [],
                        "title": title,
                        "continuation": "none",
                        "reason": "parser_paragraph_group",
                        "confidence": max(0.0, min(1.0, confidence)),
                    }
                )

        # Compatibility fallback: if parser model only returned line_labels, split conservatively.
        if not segments and line_labels:
            ordered_labels = sorted(
                line_labels,
                key=lambda row: (
                    line_order_map.get(str(row.get("line_id") or "").strip(), 10_000_000),
                    str(row.get("line_id") or ""),
                ),
            )
            paragraph_buffer: List[str] = []
            for row in ordered_labels:
                line_id = str(row.get("line_id") or "").strip()
                if not line_id:
                    continue
                zone_type = str(row.get("zone_type") or "main_body").strip().lower()
                if zone_type != "main_body":
                    continue
                try:
                    heading_prob = float(row.get("heading_prob") or 0.0)
                except Exception:
                    heading_prob = 0.0
                if heading_prob >= 0.9:
                    if paragraph_buffer:
                        segments.append(
                            {
                                "segment_id": _next_segment_id("p"),
                                "kind": "paragraph",
                                "kind_hint": "paragraph",
                                "component_hint": "ParagraphProse",
                                "line_ids": list(paragraph_buffer),
                                "evidence_line_ids": list(paragraph_buffer),
                                "block_ids": [],
                                "title": "",
                                "continuation": "none",
                                "reason": "parser_line_label_buffer",
                            }
                        )
                        paragraph_buffer = []
                    title = line_text_map.get(line_id, "")
                    segments.append(
                        {
                            "segment_id": _next_segment_id("h"),
                            "kind": "heading",
                            "kind_hint": "heading",
                            "component_hint": "SectionHeading",
                            "line_ids": [line_id],
                            "evidence_line_ids": [line_id],
                            "block_ids": [],
                            "title": title,
                            "continuation": "none",
                            "reason": "parser_line_label_heading",
                            "confidence": max(0.9, heading_prob),
                        }
                    )
                    continue
                paragraph_buffer.append(line_id)
                if bool(row.get("paragraph_break_after")):
                    segments.append(
                        {
                            "segment_id": _next_segment_id("p"),
                            "kind": "paragraph",
                            "kind_hint": "paragraph",
                            "component_hint": "ParagraphProse",
                            "line_ids": list(paragraph_buffer),
                            "evidence_line_ids": list(paragraph_buffer),
                            "block_ids": [],
                            "title": "",
                            "continuation": "none",
                            "reason": "parser_line_label_break",
                        }
                    )
                    paragraph_buffer = []
            if paragraph_buffer:
                segments.append(
                    {
                        "segment_id": _next_segment_id("p"),
                        "kind": "paragraph",
                        "kind_hint": "paragraph",
                        "component_hint": "ParagraphProse",
                        "line_ids": list(paragraph_buffer),
                        "evidence_line_ids": list(paragraph_buffer),
                        "block_ids": [],
                        "title": "",
                        "continuation": "none",
                        "reason": "parser_line_label_tail",
                    }
                )

        if not segments:
            return {}

        segments = sorted(
            segments,
            key=lambda row: (
                self._safe_int(
                    row.get("sort_order"),
                    min(
                        [line_order_map.get(line_id, 10_000_000) for line_id in list(row.get("line_ids") or [])] or
                        [word_order_map.get(str(word_id).strip(), 10_000_000) for word_id in list(row.get("word_ids") or [])] or
                        [10_000_000]
                    ),
                ),
                str(row.get("segment_id") or ""),
            ),
        )

        counts = dict(parser_advice.get("counts") or {})
        return {
            "source": "vlflash_page_structure_v2",
            "page_structure_version": "v3",
            "advice_only": True,
            "segments": segments,
            "zones": [],
            "continuation": {"from_prev": [], "to_next": [], "confidence": 0.0, "reason": ""},
            "ui_suggestions": [],
            "notes": list(parser_advice.get("notes") or []),
            "doc_nav_tree": [row for row in list(parser_advice.get("doc_nav_tree") or []) if isinstance(row, dict)][:120],
            "relations": [row for row in list(parser_advice.get("relations") or []) if isinstance(row, dict)][:320],
            "parser_counts": {
                "heading_count": self._safe_int(counts.get("heading_count"), len(heading_groups)),
                "paragraph_count": self._safe_int(counts.get("paragraph_count"), len(paragraph_groups)),
                "figure_count": self._safe_int(counts.get("figure_count"), len(figure_groups)),
                "block_count": self._safe_int(counts.get("block_count"), len(block_groups)),
            },
            "figure_groups": figure_groups[:80],
            "block_groups": block_groups[:240],
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
                        page_no = self._safe_int(anchor.get("page"), 0)
                        start_char = self._safe_int(anchor.get("start_char"), -1)
                        end_char = self._safe_int(anchor.get("end_char"), -1)
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
        paragraph_texts = [
            self._normalize_spaces(str((node.get("props") or {}).get("text") or ""))
            for node in paragraph_nodes
            if self._normalize_spaces(str((node.get("props") or {}).get("text") or ""))
        ]
        paragraph_unique = len({item.lower() for item in paragraph_texts})
        duplicate_ratio = 0.0
        if paragraph_texts:
            duplicate_ratio = 1.0 - (paragraph_unique / max(1, len(paragraph_texts)))

        anchor_eval = self._evaluate_anchor_metrics(
            ui_plan=ui_plan,
            base_payload=base_payload,
        )
        anchor_quote_hit_rate = float(anchor_eval.get("hit_rate") or 0.0)
        anchor_bbox_iou = float(anchor_eval.get("bbox_iou") or 0.0)
        anchor_misjump_rate = float(anchor_eval.get("misjump_rate") or 0.0)
        anchor_gate_passed = bool(anchor_eval.get("gate_passed"))
        if not bool(getattr(settings, "reader_anchor_eval_gate_enabled", True)):
            anchor_gate_passed = True

        anchor_ref_count = 0
        anchor_node_count = 0
        evidence_image_ready = False
        for node in flat_nodes:
            refs = list((node or {}).get("source_anchor_refs") or [])
            if refs:
                anchor_node_count += 1
                anchor_ref_count += len(refs)
                if any(isinstance((row or {}).get("bbox_hint"), dict) for row in refs if isinstance(row, dict)):
                    evidence_image_ready = True
        anchor_coverage_ratio = (anchor_node_count / max(1, len(paragraph_nodes))) if paragraph_nodes else 1.0

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
            node.get("type") in {"SectionTOC", "MetadataSidebarCard", "ContextRail"} for node in flat_nodes
        )
        layout_consistency = 0.0
        if has_header:
            layout_consistency += 0.34
        if has_body:
            layout_consistency += 0.33
        if has_toc_or_meta:
            layout_consistency += 0.33
        layout_consistency = min(1.0, layout_consistency)

        cross_column_merge_ratio = float(
            base_payload.get("cross_column_merge_ratio")
            if base_payload.get("cross_column_merge_ratio") is not None
            else self._estimate_cross_column_merge_ratio(base_payload=base_payload)
        )
        expected_sidebar = max(0, len(list(base_payload.get("side_context_blocks") or [])))
        rendered_sidebar = 0
        for node in flat_nodes:
            if str(node.get("type") or "") != "ContextRail":
                continue
            items = (node.get("props") or {}).get("items")
            if isinstance(items, list):
                rendered_sidebar += len(items)
        sidebar_recall = 1.0
        if expected_sidebar > 0:
            sidebar_recall = rendered_sidebar / max(1, expected_sidebar)
            sidebar_recall = max(0.0, min(1.0, sidebar_recall))

        toc_quality = float(base_payload.get("toc_quality") or 0.0)
        toc_hidden = bool(base_payload.get("toc_hidden"))
        toc_nodes = [node for node in flat_nodes if str(node.get("type") or "") == "SectionTOC"]
        if toc_nodes:
            props = toc_nodes[0].get("props") or {}
            if isinstance(props, dict):
                try:
                    toc_quality = float(props.get("toc_quality") or toc_quality)
                except Exception:
                    toc_quality = float(base_payload.get("toc_quality") or 0.0)
                hidden_reason = self._normalize_spaces(str(props.get("hidden_reason") or ""))
                if hidden_reason:
                    toc_hidden = True

        sidebar_leak = self._detect_sidebar_leak(paragraph_nodes)
        title_integrity = self._check_title_integrity(flat_nodes, base_payload)
        anchors_valid = not any("anchor" in str(item).lower() for item in validation_errors)
        toc_passed = bool(toc_hidden or toc_quality >= 0.55)
        hard_constraints_passed = bool(
            title_integrity
            and not sidebar_leak
            and anchors_valid
            and toc_passed
            and anchor_gate_passed
        )

        overall = (
            0.42 * structure_fidelity
            + 0.23 * readability
            + 0.18 * evidence_alignment
            + 0.09 * layout_consistency
            + 0.04 * (1.0 - max(0.0, min(1.0, cross_column_merge_ratio)))
            + 0.04 * sidebar_recall
        )
        deductions: List[Dict[str, Any]] = []
        if validation_errors:
            penalty = min(0.25, 0.03 * len(validation_errors))
            deductions.append(
                {
                    "item": "schema_validation",
                    "penalty": round(penalty, 4),
                    "reason": f"Found {len(validation_errors)} schema validation issues",
                }
            )
            overall -= min(0.25, 0.03 * len(validation_errors))
        if sidebar_leak:
            deductions.append(
                {
                    "item": "sidebar_leak",
                    "penalty": 0.3,
                    "reason": "Detected sidebar text leakage into main body",
                }
            )
            overall -= 0.3
        if not title_integrity:
            deductions.append(
                {
                    "item": "title_integrity",
                    "penalty": 0.2,
                    "reason": "Title integrity check failed",
                }
            )
            overall -= 0.2
        if cross_column_merge_ratio > 0.08:
            deductions.append(
                {
                    "item": "cross_column_merge",
                    "penalty": 0.12,
                    "reason": "Detected possible cross-column body merge",
                }
            )
            overall -= 0.12
        if sidebar_recall < 0.6:
            deductions.append(
                {
                    "item": "sidebar_recall",
                    "penalty": 0.1,
                    "reason": "Sidebar recall is below threshold",
                }
            )
            overall -= 0.1
        if not toc_passed:
            deductions.append(
                {
                    "item": "toc_quality",
                    "penalty": 0.08,
                    "reason": "TOC quality is below threshold and not hidden",
                }
            )
            overall -= 0.08
        if duplicate_ratio > 0.1:
            deductions.append(
                {
                    "item": "duplicate_content",
                    "penalty": 0.1,
                    "reason": "Detected high duplicate-content ratio",
                }
            )
            overall -= 0.1
        if not anchor_gate_passed:
            deductions.append(
                {
                    "item": "anchor_gate",
                    "penalty": 0.12,
                    "reason": "Anchor quality gate did not pass",
                }
            )
            overall -= 0.12
        overall = max(0.0, min(1.0, overall))

        fix_suggestions: List[str] = []
        if sidebar_leak:
            fix_suggestions.append("Prioritize sidebar isolation to prevent OPEN ACCESS/Citation bleed.")
        if not title_integrity:
            fix_suggestions.append("Repair heading boundaries and prevent title/body merges.")
        if validation_errors:
            fix_suggestions.append("Run node-level repair and re-validate anchors/fields.")
        if readability < 0.72:
            fix_suggestions.append("Improve paragraph segmentation and line-break cleanup.")
        if evidence_alignment < 0.8:
            fix_suggestions.append("Strengthen evidence alignment for DOI/URL and source anchors.")
        if cross_column_merge_ratio > 0.08:
            fix_suggestions.append("Improve dual-column separation to reduce cross-column merges.")
        if sidebar_recall < 0.6:
            fix_suggestions.append("Preserve sidebar content into ContextRail instead of dropping it.")
        if not toc_passed:
            fix_suggestions.append("Generate TOC only from high-confidence headings; hide low-quality TOC.")
        if duplicate_ratio > 0.1:
            fix_suggestions.append("Deduplicate repeated prose blocks to keep one semantic paragraph per render.")
        if not anchor_gate_passed:
            fix_suggestions.append("Hide jump actions when anchor gate fails and trigger re-layout.")

        mm_meta = dict(base_payload.get("mm_assist_meta") or {})

        return {
            "overall": round(overall, 4),
            "structure_fidelity": round(structure_fidelity, 4),
            "readability": round(readability, 4),
            "evidence_alignment": round(evidence_alignment, 4),
            "layout_consistency": round(layout_consistency, 4),
            "cross_column_merge_ratio": round(max(0.0, min(1.0, cross_column_merge_ratio)), 4),
            "sidebar_recall": round(max(0.0, min(1.0, sidebar_recall)), 4),
            "toc_quality": round(max(0.0, min(1.0, toc_quality)), 4),
            "duplicate_ratio": round(max(0.0, min(1.0, duplicate_ratio)), 4),
            "anchor_coverage_ratio": round(max(0.0, min(1.0, anchor_coverage_ratio)), 4),
            "anchor_quote_hit_rate": round(max(0.0, min(1.0, anchor_quote_hit_rate)), 4),
            "anchor_bbox_iou": round(max(0.0, min(1.0, anchor_bbox_iou)), 4),
            "anchor_misjump_rate": round(max(0.0, min(1.0, anchor_misjump_rate)), 4),
            "anchor_gate_passed": bool(anchor_gate_passed),
            "evidence_image_ready": bool(evidence_image_ready),
            "anchor_ref_count": int(anchor_ref_count),
            "hard_constraints_passed": hard_constraints_passed,
            "sidebar_leak_detected": sidebar_leak,
            "title_integrity_ok": title_integrity,
            "anchors_valid": anchors_valid,
            "mm_assist_used": bool(mm_meta.get("used")),
            "mm_model": str(mm_meta.get("model") or ""),
            "mm_fallback_used": bool(mm_meta.get("fallback_used")),
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
        allow_external_images = bool(getattr(settings, "reader_external_image_enabled", False))

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
        if allow_external_images and not has_pdf_image:
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
        max_iterations: Optional[int] = None,
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
                    max_iterations=max_iterations,
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
        raw_blocks = self._normalize_blocks_for_render(
            blocks=list(base_payload.get("blocks") or []),
            page=page,
        )
        page_structure_v3 = dict(base_payload.get("page_structure_v3") or {})
        ai_main_blocks = self._build_main_blocks_from_page_structure(
            page=page,
            page_structure=page_structure_v3,
            base_payload=base_payload,
        )
        main_block_source = "page_structure_v3_only"

        blocks = list(raw_blocks)
        if ai_main_blocks:
            non_main = [item for item in blocks if str(item.get("zone_type") or "") != "main_body"]
            blocks = ai_main_blocks + non_main

        side_context_blocks = self._normalize_blocks_for_render(
            blocks=list(base_payload.get("side_context_blocks") or []),
            page=page,
        )
        figure_meta_blocks = self._normalize_blocks_for_render(
            blocks=list(base_payload.get("figure_meta_blocks") or []),
            page=page,
        )
        if not figure_meta_blocks:
            figure_meta_blocks = [
                item for item in blocks if str(item.get("zone_type") or "") == "figure_meta"
            ]
        main_blocks = [
            item for item in blocks if str(item.get("zone_type") or "main_body") == "main_body"
        ]
        main_blocks = self._dedupe_main_blocks(main_blocks)
        assets = list(base_payload.get("assets") or [])
        summary = str(base_payload.get("summary") or "").strip()
        style_cues = dict(base_payload.get("style_cues") or {})
        toc_quality = float(base_payload.get("toc_quality") or 0.0)
        toc_hidden = bool(base_payload.get("toc_hidden")) or toc_quality < 0.55

        components: List[Dict[str, Any]] = []
        cid = 0

        def next_id(prefix: str) -> str:
            nonlocal cid
            cid += 1
            return f"{prefix}_{cid}"

        def wrap_anchor(anchor: Any, quote_text: str = "") -> List[Dict[str, Any]]:
            normalized = self._normalize_anchor_ref(
                anchor=anchor,
                page=page,
                quote_text=quote_text,
            )
            if not normalized:
                return []
            try:
                if not isinstance(normalized.get("bbox_hint"), dict):
                    normalized["bbox_hint"] = self._build_bbox_hint(
                        style_cues=style_cues,
                        quote_text=quote_text,
                    )
            except Exception:
                if "bbox_hint" not in normalized:
                    normalized["bbox_hint"] = None
            return [normalized]

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
                    {"key": "copy", "label": "Copy", "kind": "default", "payload": {}},
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
                    {"key": "copy", "label": "Copy", "kind": "default", "payload": {}},
                ],
                "layout_slot": {"reserved_height": 220, "lock_height": True},
            }
        )


        if side_context_blocks:
            side_items = []
            for row in side_context_blocks[:22]:
                if not isinstance(row, dict):
                    continue
                text = self._normalize_spaces(str(row.get("text") or ""))
                if not text:
                    continue
                side_items.append(
                    {
                        "text": text,
                        "anchor": wrap_anchor(row.get("source_anchor"), quote_text=text),
                        "column_id": str(row.get("column_id") or "sidebar"),
                    }
                )
            if side_items:
                components.append(
                    {
                        "id": next_id("context_rail"),
                        "type": "ContextRail",
                        "props": {
                            "title": "Side Information",
                            "items": side_items,
                            "default_collapsed": True,
                        },
                        "children": [],
                        "source_anchor_refs": [],
                        "zone_type": "side_context",
                        "column_id": "sidebar",
                        "capabilities": ["jump_anchor", "copy", "drag_markdown"],
                        "actions": [],
                        "layout_slot": {"reserved_height": 180, "lock_height": True},
                    }
                )

        takeaway_items = self._normalize_takeaway_items(
            raw_items=base_payload.get("takeaways"),
            page=page,
            fallback_summary=summary,
            detail_level=detail_level,
        )
        if takeaway_items:
            components.append(
                {
                    "id": next_id("takeaways"),
                    "type": "KeyTakeaways",
                    "props": {"items": takeaway_items},
                    "children": [],
                    "source_anchor_refs": [],
                    "capabilities": ["jump_anchor", "copy", "drag_markdown"],
                    "actions": [
                        {"key": "copy", "label": "Copy", "kind": "default", "payload": {}},
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

        for block in main_blocks:
            kind = str(block.get("kind") or "")
            text = self._normalize_spaces(str(block.get("text") or ""))
            if not text:
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
                        "zone_type": "main_body",
                        "column_id": str(block.get("column_id") or "main"),
                        "heading_prob": float(block.get("heading_prob") or 0.0),
                        "capabilities": ["jump_anchor", "copy"],
                        "actions": [],
                        # Do not hard-lock heading height; long/multi-line titles would be clipped.
                        "layout_slot": {"reserved_height": 86, "lock_height": False},
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
                        "zone_type": "main_body",
                        "column_id": str(block.get("column_id") or "main"),
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
                            {"key": "copy_markdown", "label": "Copy Markdown", "kind": "default", "payload": {}},
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
                        "zone_type": "main_body",
                        "column_id": str(block.get("column_id") or "main"),
                        "capabilities": ["copy", "drag_markdown", "inline_query", "jump_anchor"],
                        "actions": [
                            {"key": "regenerate", "label": "Repair", "kind": "default", "payload": {}},
                            {"key": "degrade", "label": "Degrade", "kind": "default", "payload": {}},
                            {"key": "copy", "label": "Copy", "kind": "default", "payload": {}},
                        ],
                        "layout_slot": {"reserved_height": 210, "lock_height": False},
                    }
                )

        seen_figure_keys: set[str] = set()
        for block in figure_meta_blocks:
            if not isinstance(block, dict):
                continue
            text = self._normalize_spaces(str(block.get("text") or ""))
            if not text:
                continue
            text_key = re.sub(r"\s+", "", text.lower())[:120]
            if text_key in seen_figure_keys:
                continue
            seen_figure_keys.add(text_key)
            anchor_refs = wrap_anchor(block.get("source_anchor"), quote_text=text)
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
                    "zone_type": "figure_meta",
                    "column_id": str(block.get("column_id") or "main"),
                    "capabilities": ["copy", "drag_markdown", "jump_anchor"],
                    "actions": [
                        {"key": "copy_markdown", "label": "Copy Markdown", "kind": "default", "payload": {}},
                    ],
                    "layout_slot": {"reserved_height": 260, "lock_height": True},
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
                "toc_quality": round(max(0.0, min(1.0, toc_quality)), 4),
                "toc_hidden": bool(toc_hidden),
                "generator": COMPOSE_ENGINE_VERSION,
                "schema_version": COMPOSE_COMPONENT_SCHEMA_VERSION,
                "layout_schema_version": COMPOSE_LAYOUT_SCHEMA_VERSION,
                "main_block_source": str(main_block_source),
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

        has_heading = any(str(item.get("type") or "") == "SectionHeading" for item in patched)
        if not has_heading:
            sections = list(base_payload.get("sections") or [])
            for section in sections:
                title = self._normalize_spaces(str(section.get("title") or ""))
                if not title or title.lower() == "body":
                    continue
                recovered_anchor = self._normalize_anchor_ref(
                    anchor=section.get("source_anchor"),
                    page=int(section.get("page") or 0) or int((section.get("source_anchor") or {}).get("page") or 0) or 1,
                    quote_text=title,
                )
                patched.insert(
                    3,
                    {
                        "id": f"heading_recover_{uuid.uuid4().hex[:8]}",
                        "type": "SectionHeading",
                        "props": {"text": title, "level": int(section.get("level") or 1)},
                        "children": [],
                        "source_anchor_refs": [recovered_anchor] if recovered_anchor else [],
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
            action_message = "Node degraded to a safer fallback component."
            patch_type = "node_replace"
        else:
            node_after = self._build_regenerated_node(node_before=node_before)
            quality_delta = 0.04
            action_message = "Node regenerated successfully."
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

        components = list(((payload.get("ui_plan") or {}).get("components") or []))
        node = self._find_component_node(components, str(node_id))
        if node is None:
            raise ValueError(f"node not found: {node_id}")

        target_node = self._resolve_inline_query_target_node(
            query_node=node,
            components=components,
        )
        contract_failure = self._validate_inline_query_contract(
            page=int(page),
            query_node=node,
            target_node=target_node,
        )
        if contract_failure:
            return {
                "disabled": True,
                "disabled_reason": contract_failure,
                "message": "Inline query contract validation failed.",
            }
        anchor_refs = self._resolve_inline_query_anchors(
            query_node=node,
            target_node=target_node,
            components=components,
            page=int(page),
        )
        if not anchor_refs:
            return {
                "disabled": True,
                "disabled_reason": "inline_query_missing_source_anchor_refs",
                "message": "Inline query source anchors are required.",
            }
        source_block_ids = self._extract_inline_query_source_block_ids(
            page=int(page),
            query_node=node,
            target_node=target_node,
        )
        if not source_block_ids:
            return {
                "disabled": True,
                "disabled_reason": "inline_query_missing_source_block_ids",
                "message": "Inline query source block IDs are required.",
            }
        context_text = self._build_inline_query_context(
            query_node=node,
            target_node=target_node,
            components=components,
            page=int(page),
        )
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
            "source_block_ids": source_block_ids[:12],
            "capabilities": ["copy", "jump_anchor", "drag_markdown"],
            "actions": [
                {"key": "copy", "label": "Copy", "kind": "default", "payload": {}},
            ],
            "layout_slot": {"reserved_height": 220, "lock_height": False},
        }
        sources = []
        for anchor in anchor_refs[:3]:
            if not isinstance(anchor, dict):
                continue
            sources.append(
                {
                    "page": self._safe_int(anchor.get("page"), self._safe_int(page, 1)),
                    "start_char": self._safe_int(anchor.get("start_char"), 0),
                    "end_char": self._safe_int(anchor.get("end_char"), 0),
                    "quote": str(anchor.get("quote") or anchor.get("quote_text") or "")[:240] or None,
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
            compact_context = "Current node has limited textual evidence."
        prompt = (
            "You are a literature reading assistant.\n"
            "Answer strictly from the provided context. Do not fabricate.\n"
            "Output in exactly two Chinese sentences:\n"
            "1) 结论：...\n"
            "2) 证据：...\n"
            "If evidence is insufficient, explicitly answer: 结论：当前证据不足以回答。\n"
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
        return (
            f"结论：当前无法基于现有上下文完整回答“{compact_question}”。"
            "证据：请先定位到原文证据后再追问。"
        )

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
                    {"key": "regenerate", "label": "Repair", "kind": "default", "payload": {}},
                ],
                "layout_slot": {"reserved_height": 200, "lock_height": False},
            }
        return {
            "id": str(node_before.get("id") or f"degrade_{uuid.uuid4().hex[:8]}"),
            "type": "PdfSnippetCard",
            "props": {
                "title": "Degraded to Original Snippet",
                "description": "当前节点无法稳定解析，建议切换到 PDF 模式核对原文。",
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
                props["ai_insight"] = "该图表用于支撑本页关键结论，建议结合原文锚点核对。"
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

    def _extract_inline_query_source_block_ids(
        self,
        *,
        page: int,
        query_node: Dict[str, Any],
        target_node: Dict[str, Any],
    ) -> List[str]:
        output: List[str] = []
        for node in (target_node, query_node):
            for raw in list((node or {}).get("source_block_ids") or []):
                canonical = self._normalize_canonical_block_id(page=page, raw_id=str(raw))
                if canonical and canonical not in output:
                    output.append(canonical)
            for anchor in list((node or {}).get("source_anchor_refs") or []):
                if not isinstance(anchor, dict):
                    continue
                canonical = self._normalize_canonical_block_id(
                    page=page,
                    raw_id=str(anchor.get("canonical_block_id") or ""),
                )
                if canonical and canonical not in output:
                    output.append(canonical)
        return output

    def _validate_inline_query_contract(
        self,
        *,
        page: int,
        query_node: Dict[str, Any],
        target_node: Dict[str, Any],
    ) -> str:
        target_type = str((target_node or {}).get("type") or "").strip()
        if target_type not in INLINE_QUERY_SUPPORTED_NODE_TYPES:
            return "inline_query_unsupported_node_type"
        source_block_ids = self._extract_inline_query_source_block_ids(
            page=page,
            query_node=query_node,
            target_node=target_node,
        )
        if not source_block_ids:
            return "inline_query_missing_source_block_ids"
        source_anchors = list((target_node or {}).get("source_anchor_refs") or []) or list(
            (query_node or {}).get("source_anchor_refs") or []
        )
        if not source_anchors:
            return "inline_query_missing_source_anchor_refs"
        for anchor in source_anchors:
            if not isinstance(anchor, dict):
                continue
            page_num = self._safe_int(anchor.get("page"), 0)
            start_char = self._safe_int(anchor.get("start_char"), -1)
            end_char = self._safe_int(anchor.get("end_char"), -1)
            quote = self._normalize_spaces(str(anchor.get("quote") or anchor.get("quote_text") or ""))
            if page_num >= 1 and start_char >= 0 and end_char > start_char and quote:
                return ""
        return "inline_query_invalid_source_anchor_shape"

    def _resolve_inline_query_target_node(
        self,
        *,
        query_node: Dict[str, Any],
        components: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        node_type = str(query_node.get("type") or "")
        if node_type != "InlineQuerySlot":
            return query_node
        target_ref = str(((query_node.get("props") or {}).get("target_node_ref") or "")).strip()
        if target_ref:
            target_node = self._find_component_node(components, target_ref)
            if isinstance(target_node, dict):
                return target_node
        return query_node

    def _resolve_inline_query_anchors(
        self,
        *,
        query_node: Dict[str, Any],
        target_node: Dict[str, Any],
        components: Sequence[Dict[str, Any]],
        page: int,
    ) -> List[Dict[str, Any]]:
        target_anchors = list(target_node.get("source_anchor_refs") or [])
        query_anchors = list(query_node.get("source_anchor_refs") or [])
        selected = target_anchors or query_anchors
        if selected:
            normalized: List[Dict[str, Any]] = []
            for row in selected:
                normalized_row = self._normalize_anchor_ref(
                    anchor=row,
                    page=page,
                    quote_text=str((row or {}).get("quote") or (row or {}).get("quote_text") or "") if isinstance(row, dict) else "",
                )
                if normalized_row:
                    normalized_row["quote"] = str(normalized_row.get("quote") or normalized_row.get("quote_text") or "").strip() or None
                    normalized.append(normalized_row)
            if normalized:
                return normalized[:3]

        flat_nodes = self._flatten_components(components)
        target_id = str(target_node.get("id") or "")
        anchor_candidates: List[Dict[str, Any]] = []
        index_map = {str(item.get("id") or ""): idx for idx, item in enumerate(flat_nodes)}
        center = index_map.get(target_id, 0)
        for offset in (0, -1, 1, -2, 2, -3, 3):
            idx = center + offset
            if idx < 0 or idx >= len(flat_nodes):
                continue
            candidate = flat_nodes[idx]
            refs = list(candidate.get("source_anchor_refs") or [])
            for row in refs:
                normalized_row = self._normalize_anchor_ref(
                    anchor=row,
                    page=page,
                    quote_text=str((row or {}).get("quote") or (row or {}).get("quote_text") or "") if isinstance(row, dict) else "",
                )
                if normalized_row:
                    normalized_row["quote"] = str(normalized_row.get("quote") or normalized_row.get("quote_text") or "").strip() or None
                    anchor_candidates.append(normalized_row)
            if anchor_candidates:
                break
        return anchor_candidates[:3]

    def _build_inline_query_context(
        self,
        *,
        query_node: Dict[str, Any],
        target_node: Dict[str, Any],
        components: Sequence[Dict[str, Any]],
        page: int,
    ) -> str:
        flat_nodes = self._flatten_components(components)
        node_id = str(target_node.get("id") or query_node.get("id") or "")
        index_map = {str(item.get("id") or ""): idx for idx, item in enumerate(flat_nodes)}
        center = index_map.get(node_id, 0)

        heading_text = ""
        for idx in range(center, -1, -1):
            node = flat_nodes[idx]
            if str(node.get("type") or "") == "SectionHeading":
                heading_text = self._extract_node_text(node)
                if heading_text:
                    break

        context_parts: List[str] = []
        if heading_text:
            context_parts.append(f"当前章节：{heading_text}")

        target_text = self._extract_node_text(target_node)
        if target_text:
            context_parts.append(f"当前节点：{target_text}")

        nearby_snippets: List[str] = []
        for idx in range(max(0, center - 2), min(len(flat_nodes), center + 3)):
            if idx == center:
                continue
            node = flat_nodes[idx]
            node_type = str(node.get("type") or "")
            if node_type not in {"ParagraphProse", "ListBlock", "SectionHeading"}:
                continue
            snippet = self._extract_node_text(node)
            if snippet:
                nearby_snippets.append(snippet)
        if nearby_snippets:
            context_parts.append(f"相邻上下文：{' '.join(nearby_snippets[:3])}")

        anchors = self._resolve_inline_query_anchors(
            query_node=query_node,
            target_node=target_node,
            components=components,
            page=page,
        )
        anchor_quotes = [
            self._normalize_spaces(str(item.get("quote") or item.get("quote_text") or ""))
            for item in anchors
            if isinstance(item, dict)
        ]
        anchor_quotes = [item for item in anchor_quotes if item]
        if anchor_quotes:
            context_parts.append(f"证据片段：{' '.join(anchor_quotes[:2])}")

        return self._normalize_spaces("\n".join(context_parts))

    async def _build_source_signature(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        selected_kb_id: Optional[int],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        citation_tldr: bool,
        max_iterations: Optional[int],
    ) -> str:
        normalized_style_intent = self._normalize_style_intent(style_intent)
        path = self._reader_service._resolve_local_pdf_path(  # pylint: disable=protected-access
            user_id=int(paper.user_id),
            paper_id=int(paper.id),
            paper_title=paper.title,
            paper_pdf_path=paper.pdf_path,
        )
        pdf_sig: Dict[str, Any] = {"path_hash": "none", "mtime": 0, "size": 0}
        if path and os.path.exists(path):
            st = os.stat(path)
            pdf_sig = {
                "path_hash": hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16],
                "mtime": int(st.st_mtime),
                "size": int(st.st_size),
            }

        kb_sig: Dict[str, Any] = {"id": 0, "doc_updated": "none"}
        kb_id = int(selected_kb_id) if selected_kb_id else 0
        if kb_id > 0:
            owned_kb_id = (
                await db.execute(
                    select(KnowledgeBase.id).where(
                        and_(
                            KnowledgeBase.id == kb_id,
                            KnowledgeBase.user_id == int(user_id),
                        )
                    )
                )
            ).scalar_one_or_none()
            if owned_kb_id:
                max_doc_updated = (
                    await db.execute(
                        select(func.max(Document.updated_at))
                        .select_from(Document)
                        .where(Document.knowledge_base_id == int(owned_kb_id))
                    )
                ).scalar_one_or_none()
                kb_sig = {
                    "id": int(owned_kb_id),
                    "doc_updated": max_doc_updated.isoformat() if max_doc_updated else "none",
                }

        theme_part = self._normalize_spaces(str(theme_mode or "light"))
        detail_part = self._normalize_spaces(str(detail_level or "standard"))
        iteration_part = int(max_iterations) if isinstance(max_iterations, int) and max_iterations > 0 else DEFAULT_MAX_ITERATIONS
        mm_enabled = bool(
            getattr(settings, "reader_mm_assist_enabled", False)
            or getattr(settings, "reader_multimodal_enabled", False)
        )
        mm_primary = str(getattr(settings, "reader_mm_primary_model", "qwen3.5-flash") or "qwen3.5-flash")
        mm_fallback = str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        mm_parser = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        mm_layout = str(getattr(settings, "reader_mm_layout_model", "qwen3.5-flash") or "qwen3.5-flash")
        mm_prompt_version = str(getattr(settings, "reader_mm_prompt_version", "mm_layout_v1") or "mm_layout_v1")
        layout_schema_version = str(
            getattr(settings, "reader_mm_layout_schema_version", COMPOSE_LAYOUT_SCHEMA_VERSION)
            or COMPOSE_LAYOUT_SCHEMA_VERSION
        )
        assembly_enabled = bool(getattr(settings, "reader_compose_layout_llm_enabled", True))
        assembly_prompt_version = str(
            getattr(settings, "reader_compose_layout_llm_prompt_version", "compose_layout_llm_v1")
            or "compose_layout_llm_v1"
        )
        assembly_max_blocks = int(getattr(settings, "reader_compose_layout_llm_max_blocks", 80) or 80)
        pipeline_mode = self._pipeline_mode()
        pipeline_version = self._pipeline_version()

        signature_payload = {
            "engine": COMPOSE_ENGINE_VERSION,
            "component": COMPOSE_COMPONENT_SCHEMA_VERSION,
            "prompt": COMPOSE_AGENT_PROMPT_VERSION,
            "asset": COMPOSE_ASSET_POLICY_VERSION,
            "layout_schema": layout_schema_version,
            "pipeline_mode": pipeline_mode,
            "pipeline_version": pipeline_version,
            "paper_id": int(paper.id),
            "pdf": pdf_sig,
            "kb": kb_sig,
            "mode": {
                "style": normalized_style_intent,
                "theme": theme_part,
                "detail": detail_part,
                "compare": int(bool(compare_mode)),
                "cite_tldr": int(bool(citation_tldr)),
                "iter": int(iteration_part),
                "mm": int(mm_enabled),
                "mm_primary": mm_primary,
                "mm_fallback": mm_fallback,
                "mm_parser": mm_parser,
                "mm_layout": mm_layout,
                "mm_prompt": mm_prompt_version,
                "assembly": int(assembly_enabled),
                "assembly_prompt": assembly_prompt_version,
                "assembly_max_blocks": int(assembly_max_blocks),
                "extimg": int(bool(getattr(settings, "reader_external_image_enabled", False))),
            },
        }
        packed = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(packed.encode("utf-8")).hexdigest()
        signature = (
            f"compose_v3|p:{int(paper.id)}|kb:{int(kb_sig.get('id') or 0)}|"
            f"m:{int(pdf_sig.get('mtime') or 0)}|s:{int(pdf_sig.get('size') or 0)}|"
            f"pm:{pipeline_mode}|"
            f"pv:{pipeline_version}|"
            f"mode:{normalized_style_intent}/{theme_part}/{detail_part}/{int(bool(compare_mode))}/{int(bool(citation_tldr))}|"
            f"h:{digest[:24]}"
        )
        return signature[:255]

    @staticmethod
    def _build_style_tokens(
        *,
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
    ) -> Dict[str, Any]:
        style = LiteratureReaderComposeService._normalize_style_intent(style_intent)
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

    @staticmethod
    def _normalize_style_intent(raw: Optional[str]) -> str:
        value = str(raw or "auto").strip().lower()
        alias_map = {
            "journal_classic": "journal",
            "clinical_brief": "clinical",
            "preprint_modern": "preprint",
            "journal": "journal",
            "clinical": "clinical",
            "preprint": "preprint",
            "auto": "auto",
        }
        return alias_map.get(value, "auto")

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

    async def _build_takeaways_with_neighbor_context(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        selected_kb_id: Optional[int],
        current_payload: Dict[str, Any],
        detail_level: str,
    ) -> List[Dict[str, Any]]:
        context_rows: List[Tuple[int, Dict[str, Any]]] = [(int(page), dict(current_payload))]
        neighbor_pages = [int(page) - 1, int(page) + 1]
        for neighbor in neighbor_pages:
            if neighbor < 1:
                continue
            try:
                payload, _ = await self._reader_service.build_or_get_page_payload(
                    db=db,
                    user_id=int(user_id),
                    paper=paper,
                    page=int(neighbor),
                    selected_kb_id=selected_kb_id,
                    force_refresh=False,
                    style_hint=None,
                    prefer_agent=False,
                    publish_ready_event_enabled=False,
                )
            except Exception as exc:
                logger.debug(
                    f"[ReaderComposeService] load neighbor page for takeaways failed page={neighbor}: {exc}"
                )
                continue
            if not isinstance(payload, dict):
                continue
            raw_text = self._normalize_spaces(str(payload.get("raw_text") or ""))
            blocks = list(payload.get("blocks") or [])
            if not raw_text and not blocks:
                continue
            context_rows.append((int(neighbor), dict(payload)))

        context_rows = sorted(context_rows, key=lambda item: item[0])
        return await self._generate_takeaways_from_context_rows(
            context_rows=context_rows,
            current_page=int(page),
            detail_level=detail_level,
        )

    async def _generate_takeaways_from_context_rows(
        self,
        *,
        context_rows: Sequence[Tuple[int, Dict[str, Any]]],
        current_page: int,
        detail_level: str,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        block_anchor_map: Dict[str, Dict[str, Any]] = {}

        for row_page, payload in context_rows:
            blocks = self._normalize_blocks_for_render(
                blocks=list((payload or {}).get("blocks") or []),
                page=int(row_page),
            )
            if not blocks:
                continue
            per_page_limit = 18 if int(row_page) == int(current_page) else 8
            collected = 0
            for idx, block in enumerate(blocks):
                if collected >= per_page_limit:
                    break
                if str(block.get("zone_type") or "main_body") != "main_body":
                    continue
                kind = str(block.get("kind") or "")
                if kind not in {"heading", "paragraph", "list_item"}:
                    continue
                text = self._normalize_spaces(str(block.get("text") or ""))
                if len(text) < 16:
                    continue
                anchor = block.get("source_anchor")
                if not isinstance(anchor, dict):
                    continue
                raw_id = str(block.get("id") or f"b{idx + 1}")
                block_id = f"p{int(row_page)}_{raw_id}"
                normalized_anchor = self._normalize_anchor_ref(
                    anchor=anchor,
                    page=int(row_page),
                    quote_text=text,
                )
                if isinstance(normalized_anchor, dict):
                    block_anchor_map[block_id] = normalized_anchor
                candidates.append(
                    {
                        "block_id": block_id,
                        "page": int(row_page),
                        "scope": "current" if int(row_page) == int(current_page) else "neighbor_ref",
                        "kind": kind,
                        "text": text[:280],
                    }
                )
                collected += 1

        if len(candidates) < 3:
            return []

        level_hint = {
            "concise": "建议偏少且聚焦，常见 2-4 条。",
            "deep": "允许更细粒度，常见 4-8 条。",
        }.get(str(detail_level or "standard"), "通常 3-6 条即可。")

        prompt = (
            "你是科研论文阅读助手。\n"
            "任务：基于上一页、当前页、下一页候选块，总结当前页关键要点。\n"
            "规则：\n"
            "1) 仅输出“当前页”核心信息；相邻页只用于上下文理解。\n"
            "2) 你可以自主决定要点条数，不固定。\n"
            f"{level_hint}\n"
            "3) 每条为简洁完整中文句子，不要省略号，不要抄长段原文。\n"
            "4) 保留具体事实，避免空泛句式。\n"
            "5) 不输出定位信息，不输出证据 ID，不输出解释性废话。\n"
            "6) 仅输出 JSON。\n"
            "输出格式：\n"
            "{\"items\":[{\"text\":\"...\"}]}\n"
            f"当前页: {int(current_page)}\n"
            f"候选证据块: {json.dumps(candidates, ensure_ascii=False)}"
        )

        try:
            llm = await get_llm_service()
            result = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=960,
            )
            payload = self._extract_json_dict(str(result.get("content") or ""))
            if not payload:
                return []
            rows = payload.get("items")
            if not isinstance(rows, list):
                return []
            normalized_rows: List[Dict[str, Any]] = []
            seen_texts: set[str] = set()
            for raw in rows:
                if isinstance(raw, dict):
                    text = self._normalize_spaces(str(raw.get("text") or raw.get("title") or ""))
                    evidence_block_ids = [
                        str(item).strip()
                        for item in list(raw.get("evidence_block_ids") or raw.get("block_ids") or [])[:4]
                        if str(item).strip()
                    ]
                else:
                    text = self._normalize_spaces(str(raw or ""))
                    evidence_block_ids = []
                text = re.sub(r"(?:\.\.\.|…)+$", "", text).strip()
                if len(text) < 8:
                    continue
                text_key = text.lower()
                if text_key in seen_texts:
                    continue
                seen_texts.add(text_key)
                evidence_anchors: List[Dict[str, Any]] = []
                for block_id in evidence_block_ids:
                    anchor = dict(block_anchor_map.get(block_id) or {})
                    if not anchor:
                        continue
                    if self._safe_int(anchor.get("page"), 0) != self._safe_int(current_page, 0):
                        continue
                    evidence_anchors.append(anchor)
                    if len(evidence_anchors) >= 2:
                        break
                normalized_rows.append(
                    {
                        "text": text[:220],
                        "evidence_anchors": evidence_anchors,
                    }
                )
                if len(normalized_rows) >= 8:
                    break
            return normalized_rows
        except Exception as exc:
            logger.debug(f"[ReaderComposeService] generate takeaways with context failed: {exc}")
            return []

    def _normalize_takeaway_items(
        self,
        *,
        raw_items: Any,
        page: int,
        fallback_summary: str,
        detail_level: str,
    ) -> List[Dict[str, Any]]:
        normalized_rows: List[Dict[str, Any]] = []
        seen_texts: set[str] = set()
        if isinstance(raw_items, list):
            for raw in raw_items:
                if isinstance(raw, dict):
                    text = self._normalize_spaces(str(raw.get("text") or raw.get("title") or ""))
                else:
                    text = self._normalize_spaces(str(raw or ""))
                text = re.sub(r"(?:\.\.\.|…)+$", "", text).strip()
                if len(text) < 8:
                    continue
                text_key = text.lower()
                if text_key in seen_texts:
                    continue
                seen_texts.add(text_key)
                normalized_rows.append({"text": text[:220], "evidence_anchors": []})
                if len(normalized_rows) >= 8:
                    break

        if normalized_rows:
            return normalized_rows

        summary = self._normalize_spaces(str(fallback_summary or ""))
        summary = re.sub(r"(?:\.\.\.|…)+$", "", summary).strip()
        if not summary:
            return []
        parts = [item.strip() for item in re.split(r"[。！？!?;；.]+\s*", summary) if item.strip()]
        if detail_level == "concise":
            parts = parts[:3]
        elif detail_level == "deep":
            parts = parts[:8]
        else:
            parts = parts[:6]
        return [{"text": item[:220], "evidence_anchors": []} for item in parts if len(item) >= 8][:8]

    @staticmethod
    def _extract_json_dict(content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "").strip()
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text, flags=re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        if not text.startswith("{"):
            match = re.search(r"\{[\s\S]+\}", text)
            if not match:
                return None
            text = match.group(0)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _build_bbox_hint(
        self,
        *,
        style_cues: Dict[str, Any],
        quote_text: str,
        source_anchor: Optional[Dict[str, Any]] = None,
        line_rows: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        line_layout = list(line_rows or style_cues.get("line_layout") or [])
        target = self._normalize_spaces(str(quote_text or "")).lower()
        if not target:
            return None

        scored_rows: List[Tuple[float, Dict[str, Any]]] = []
        for row in line_layout[:260]:
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
                target_tokens = [item for item in target.split(" ") if item]
                row_tokens = [item for item in row_text.split(" ") if item]
                if target_tokens and row_tokens:
                    overlap = len(set(target_tokens) & set(row_tokens))
                    if overlap > 0:
                        score = min(0.95, overlap / max(1, min(len(target_tokens), len(row_tokens))))
            if score > 0:
                scored_rows.append((score, row))

        if not scored_rows:
            return None
        scored_rows.sort(key=lambda item: item[0], reverse=True)
        best_score = float(scored_rows[0][0])
        keep_threshold = max(0.35, best_score * 0.6)
        kept_rows = [row for score, row in scored_rows if score >= keep_threshold][:8]
        if not kept_rows:
            kept_rows = [scored_rows[0][1]]

        x0 = min(float(row.get("x0") or 0.0) for row in kept_rows)
        x1 = max(float(row.get("x1") or 0.0) for row in kept_rows)
        top = min(float(row.get("top") or 0.0) for row in kept_rows)
        bottom = max(float(row.get("bottom") or 0.0) for row in kept_rows)
        if source_anchor and isinstance(source_anchor, dict):
            # Keep bbox stable for long spans by respecting anchor page defaults.
            _ = self._safe_int(source_anchor.get("page"), 0)
        if x1 <= x0 or bottom <= top:
            return None
        return {
            "x0": x0,
            "x1": x1,
            "top": top,
            "bottom": bottom,
            "page_width": float(style_cues.get("page_width") or 0.0) or None,
            "page_height": float(style_cues.get("page_height") or 0.0) or None,
        }

    @staticmethod
    def _build_rect_polygon(*, x0: float, x1: float, top: float, bottom: float) -> List[Dict[str, float]]:
        return [
            {"x": float(x0), "y": float(top)},
            {"x": float(x1), "y": float(top)},
            {"x": float(x1), "y": float(bottom)},
            {"x": float(x0), "y": float(bottom)},
        ]

    @staticmethod
    def _dedupe_polygon_points(points: Sequence[Dict[str, float]]) -> List[Dict[str, float]]:
        output: List[Dict[str, float]] = []
        last_key = ""
        for row in points:
            x = float(row.get("x") or 0.0)
            y = float(row.get("y") or 0.0)
            key = f"{x:.3f}:{y:.3f}"
            if key == last_key:
                continue
            output.append({"x": x, "y": y})
            last_key = key
        if len(output) >= 2:
            first = output[0]
            last = output[-1]
            if abs(float(first.get("x") or 0.0) - float(last.get("x") or 0.0)) < 1e-3 and abs(
                float(first.get("y") or 0.0) - float(last.get("y") or 0.0)
            ) < 1e-3:
                output.pop()
        return output

    def _build_anchor_geometry(
        self,
        *,
        boxes: Sequence[Dict[str, Any]],
        page_width: float,
        page_height: float,
        source: str = "word_union",
    ) -> Optional[Dict[str, Any]]:
        if not bool(getattr(settings, "reader_polygon_highlight_enabled", True)):
            return None
        normalized_boxes: List[Dict[str, float]] = []
        for row in list(boxes or [])[:4000]:
            if not isinstance(row, dict):
                continue
            x0 = self._safe_float(row.get("x0"), 0.0)
            x1 = self._safe_float(row.get("x1"), 0.0)
            top = self._safe_float(row.get("top"), 0.0)
            bottom = self._safe_float(row.get("bottom"), 0.0)
            if x1 <= x0 or bottom <= top:
                continue
            normalized_boxes.append({"x0": x0, "x1": x1, "top": top, "bottom": bottom})
        if not normalized_boxes:
            return None

        # Split into connected components to avoid bridging left/right columns.
        components: List[Dict[str, Any]] = []
        x_gap = 14.0
        y_gap = 9.0
        for box in sorted(normalized_boxes, key=lambda item: (item["top"], item["x0"])):
            matched = None
            for comp in components:
                if (
                    box["x0"] <= float(comp["x1"]) + x_gap
                    and box["x1"] >= float(comp["x0"]) - x_gap
                    and box["top"] <= float(comp["bottom"]) + y_gap
                    and box["bottom"] >= float(comp["top"]) - y_gap
                ):
                    matched = comp
                    break
            if not matched:
                components.append(
                    {
                        "boxes": [box],
                        "x0": box["x0"],
                        "x1": box["x1"],
                        "top": box["top"],
                        "bottom": box["bottom"],
                    }
                )
                continue
            matched["boxes"].append(box)
            matched["x0"] = min(float(matched["x0"]), box["x0"])
            matched["x1"] = max(float(matched["x1"]), box["x1"])
            matched["top"] = min(float(matched["top"]), box["top"])
            matched["bottom"] = max(float(matched["bottom"]), box["bottom"])

        polygons: List[Dict[str, Any]] = []
        for idx, comp in enumerate(components, start=1):
            comp_boxes = list(comp.get("boxes") or [])
            if not comp_boxes:
                continue
            rows: List[Dict[str, float]] = []
            for box in sorted(comp_boxes, key=lambda item: (item["top"], item["x0"])):
                cy = (float(box["top"]) + float(box["bottom"])) / 2.0
                target_row: Optional[Dict[str, float]] = None
                for row in rows:
                    if abs(cy - float(row["cy"])) <= max(6.0, float(row["height"]) * 0.65):
                        target_row = row
                        break
                if target_row is None:
                    rows.append(
                        {
                            "x0": float(box["x0"]),
                            "x1": float(box["x1"]),
                            "top": float(box["top"]),
                            "bottom": float(box["bottom"]),
                            "cy": cy,
                            "height": max(1.0, float(box["bottom"]) - float(box["top"])),
                        }
                    )
                    continue
                target_row["x0"] = min(float(target_row["x0"]), float(box["x0"]))
                target_row["x1"] = max(float(target_row["x1"]), float(box["x1"]))
                target_row["top"] = min(float(target_row["top"]), float(box["top"]))
                target_row["bottom"] = max(float(target_row["bottom"]), float(box["bottom"]))
                target_row["cy"] = (float(target_row["top"]) + float(target_row["bottom"])) / 2.0
                target_row["height"] = max(1.0, float(target_row["bottom"]) - float(target_row["top"]))
            rows = sorted(rows, key=lambda item: (item["top"], item["x0"]))
            if not rows:
                continue

            if len(rows) == 1:
                only = rows[0]
                points = self._build_rect_polygon(
                    x0=float(only["x0"]),
                    x1=float(only["x1"]),
                    top=float(only["top"]),
                    bottom=float(only["bottom"]),
                )
            else:
                upper: List[Dict[str, float]] = []
                lower: List[Dict[str, float]] = []
                for row in rows:
                    upper.append({"x": float(row["x0"]), "y": float(row["top"])})
                    upper.append({"x": float(row["x1"]), "y": float(row["top"])})
                for row in reversed(rows):
                    lower.append({"x": float(row["x1"]), "y": float(row["bottom"])})
                    lower.append({"x": float(row["x0"]), "y": float(row["bottom"])})
                points = upper + lower

            points = self._dedupe_polygon_points(points)
            if len(points) < 3:
                fallback = self._build_rect_polygon(
                    x0=float(comp["x0"]),
                    x1=float(comp["x1"]),
                    top=float(comp["top"]),
                    bottom=float(comp["bottom"]),
                )
                points = self._dedupe_polygon_points(fallback)
            if len(points) < 3:
                continue
            polygons.append(
                {
                    "points": points,
                    "source": source,
                    "component_id": f"comp_{idx}",
                }
            )

        if not polygons:
            return None
        return {
            "polygons": polygons,
            "page_width": float(page_width) if page_width > 0 else None,
            "page_height": float(page_height) if page_height > 0 else None,
        }

    def _build_main_blocks_from_segment_map(
        self,
        *,
        page: int,
        blocks: Sequence[Dict[str, Any]],
        segment_map: Optional[Dict[str, Any]],
        base_payload: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        seg_rows = list((segment_map or {}).get("segments") or [])
        if not seg_rows:
            return []

        line_catalog = list(((base_payload or {}).get("line_catalog") or []))
        line_map: Dict[str, Dict[str, Any]] = {}
        for row in line_catalog:
            if not isinstance(row, dict):
                continue
            line_id = str(row.get("line_id") or "").strip()
            if line_id:
                line_map[line_id] = row

        native_extract = dict(((base_payload or {}).get("native_page_extract") or {}))
        native_words = [row for row in list(native_extract.get("words") or []) if isinstance(row, dict)]
        word_map: Dict[str, Dict[str, Any]] = {}
        for row in native_words:
            word_id = str(row.get("word_id") or "").strip()
            if word_id:
                word_map[word_id] = row
        native_chars = [row for row in list(native_extract.get("chars") or []) if isinstance(row, dict)]
        char_order_map: Dict[str, int] = {}
        for idx, row in enumerate(native_chars):
            char_id = str(row.get("char_id") or "").strip()
            if char_id:
                char_order_map[char_id] = idx
        word_char_span_map: Dict[str, Tuple[int, int]] = {}
        for idx, row in enumerate(native_words):
            word_id = str(row.get("word_id") or "").strip()
            if not word_id:
                continue
            start_id = str(row.get("start_char_id") or "").strip()
            end_id = str(row.get("end_char_id") or "").strip()
            start_ord = char_order_map.get(start_id, idx * 2)
            end_ord = char_order_map.get(end_id, start_ord + 1)
            if end_ord < start_ord:
                end_ord = start_ord
            word_char_span_map[word_id] = (start_ord, end_ord)
        page_meta = dict(native_extract.get("page_meta") or {})
        page_width_native = self._safe_float(page_meta.get("page_width"), self._safe_float((base_payload or {}).get("style_cues", {}).get("page_width"), 0.0))
        page_height_native = self._safe_float(page_meta.get("page_height"), self._safe_float((base_payload or {}).get("style_cues", {}).get("page_height"), 0.0))

        block_map: Dict[str, Dict[str, Any]] = {}
        fallback_block: Optional[Dict[str, Any]] = None
        for raw in list(blocks or []):
            if not isinstance(raw, dict):
                continue
            block_id = str(raw.get("id") or "").strip()
            canonical = self._normalize_canonical_block_id(
                page=page,
                raw_id=str(((raw.get("source_anchor") or {}).get("canonical_block_id") or block_id)),
            )
            if canonical:
                block_map[canonical] = raw
            if fallback_block is None and str(raw.get("kind") or "") in {"paragraph", "heading", "list_item"}:
                fallback_block = raw
        if fallback_block is None and blocks:
            fallback_block = dict(blocks[0])

        output: List[Dict[str, Any]] = []

        def _normalize_kind(raw_kind: str) -> str:
            token = str(raw_kind or "").strip().lower()
            if token in {"list", "list_item"}:
                return "list_item"
            if token in {"heading", "paragraph"}:
                return token
            return ""

        def _consensus_kind_from_source(source_block_ids: Sequence[str]) -> str:
            kinds = [
                _normalize_kind(str((block_map.get(block_id) or {}).get("kind") or ""))
                for block_id in list(source_block_ids or [])
                if str(block_id or "").strip()
            ]
            kinds = [item for item in kinds if item]
            if not kinds:
                return ""
            unique = set(kinds)
            if len(unique) == 1:
                return kinds[0]
            if "paragraph" in unique:
                return "paragraph"
            if "heading" in unique and "list_item" not in unique:
                return "heading"
            return "paragraph"

        def _looks_like_list_lines(rows: Sequence[Dict[str, Any]]) -> bool:
            bullet_like = 0
            total = 0
            for row in list(rows or [])[:8]:
                if not isinstance(row, dict):
                    continue
                total += 1
                line_text = self._normalize_spaces(str(row.get("text") or ""))
                if re.match(r"^(\d+[\.\)]|[-*•])\s+", line_text):
                    bullet_like += 1
            if total <= 0:
                return False
            return (bullet_like / float(total)) >= 0.5

        def _resolve_group_kind(
            *,
            seg: Dict[str, Any],
            source_block_ids: Sequence[str],
            row_lines: Sequence[Dict[str, Any]],
        ) -> str:
            # Parser blocks are authoritative for final render kind.
            source_kind = _consensus_kind_from_source(source_block_ids)
            if source_kind:
                return source_kind

            # Multimodal kind/component is advisory only.
            hint_kind = _normalize_kind(str(seg.get("kind_hint") or seg.get("kind") or ""))
            if hint_kind == "list_item":
                return "list_item" if _looks_like_list_lines(row_lines) else "paragraph"
            if hint_kind == "heading":
                try:
                    hint_conf = float(seg.get("confidence") or seg.get("heading_confidence") or seg.get("heading_prob") or 0.0)
                except Exception:
                    hint_conf = 0.0
                if hint_conf >= 0.9 and len(list(row_lines or [])) <= 2:
                    return "heading"
                return "paragraph"
            return "paragraph"

        def _word_ids_from_char_ranges(
            ranges: Sequence[Dict[str, str]],
            *,
            limit: int = 480,
        ) -> List[str]:
            spans: List[Tuple[int, int]] = []
            for row in list(ranges or []):
                if not isinstance(row, dict):
                    continue
                start_id = str(row.get("start_char_id") or "").strip()
                end_id = str(row.get("end_char_id") or "").strip()
                if not start_id or not end_id:
                    continue
                start_ord = char_order_map.get(start_id)
                end_ord = char_order_map.get(end_id)
                if start_ord is None and end_ord is None:
                    continue
                if start_ord is None:
                    start_ord = end_ord
                if end_ord is None:
                    end_ord = start_ord
                if start_ord is None or end_ord is None:
                    continue
                if end_ord < start_ord:
                    start_ord, end_ord = end_ord, start_ord
                spans.append((start_ord, end_ord))
            if not spans:
                return []
            rows: List[Tuple[int, str]] = []
            for word_id, (word_start, word_end) in word_char_span_map.items():
                for span_start, span_end in spans:
                    if word_end < span_start or word_start > span_end:
                        continue
                    rows.append((word_start, word_id))
                    break
            rows = sorted(rows, key=lambda item: (item[0], item[1]))
            return list(dict.fromkeys(word_id for _, word_id in rows))[:limit]

        def _char_span_from_sources(
            *,
            word_ids: Sequence[str],
            char_ranges: Sequence[Dict[str, str]],
        ) -> Tuple[int, int]:
            starts: List[int] = []
            ends: List[int] = []
            for word_id in list(word_ids or []):
                start_end = word_char_span_map.get(str(word_id).strip())
                if not start_end:
                    continue
                starts.append(int(start_end[0]))
                # Keep end exclusive for anchor range.
                ends.append(int(start_end[1]) + 1)
            if not starts or not ends:
                for row in list(char_ranges or []):
                    if not isinstance(row, dict):
                        continue
                    start_id = str(row.get("start_char_id") or "").strip()
                    end_id = str(row.get("end_char_id") or "").strip()
                    start_ord = char_order_map.get(start_id)
                    end_ord = char_order_map.get(end_id)
                    if start_ord is None and end_ord is None:
                        continue
                    if start_ord is None:
                        start_ord = end_ord
                    if end_ord is None:
                        end_ord = start_ord
                    if start_ord is None or end_ord is None:
                        continue
                    starts.append(int(min(start_ord, end_ord)))
                    ends.append(int(max(start_ord, end_ord)) + 1)
            if not starts or not ends:
                return 0, 1
            start_char = min(starts)
            end_char = max(ends)
            return start_char, max(start_char + 1, end_char)

        def _build_word_bbox_hint(word_ids: Sequence[str]) -> Optional[Dict[str, Any]]:
            boxes = []
            for word_id in list(word_ids or []):
                row = word_map.get(str(word_id).strip())
                if not isinstance(row, dict):
                    continue
                x0 = self._safe_float(row.get("x0"), 0.0)
                x1 = self._safe_float(row.get("x1"), 0.0)
                top = self._safe_float(row.get("top"), 0.0)
                bottom = self._safe_float(row.get("bottom"), 0.0)
                if x1 <= x0 or bottom <= top:
                    continue
                boxes.append({"x0": x0, "x1": x1, "top": top, "bottom": bottom})
            if not boxes:
                return None
            return {
                "x0": min(float(item["x0"]) for item in boxes),
                "x1": max(float(item["x1"]) for item in boxes),
                "top": min(float(item["top"]) for item in boxes),
                "bottom": max(float(item["bottom"]) for item in boxes),
                "page_width": page_width_native if page_width_native > 0 else None,
                "page_height": page_height_native if page_height_native > 0 else None,
            }

        def _char_ranges_from_word_ids(
            word_ids: Sequence[str],
            *,
            limit: int = 240,
        ) -> List[Dict[str, str]]:
            rows: List[Dict[str, str]] = []
            seen: set[str] = set()
            for word_id in list(word_ids or []):
                row = word_map.get(str(word_id).strip())
                if not isinstance(row, dict):
                    continue
                start_id = str(row.get("start_char_id") or "").strip()
                end_id = str(row.get("end_char_id") or "").strip()
                if not start_id or not end_id:
                    continue
                key = f"{start_id}:{end_id}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"start_char_id": start_id, "end_char_id": end_id})
                if len(rows) >= limit:
                    break
            return rows

        def _word_ids_from_line_rows(
            rows: Sequence[Dict[str, Any]],
            *,
            limit: int = 480,
        ) -> List[str]:
            ordered_word_ids: List[str] = []
            for row in list(rows or []):
                if not isinstance(row, dict):
                    continue
                words = [item for item in list(row.get("words") or []) if isinstance(item, dict)]
                if words:
                    words = sorted(
                        words,
                        key=lambda item: (
                            self._safe_float(item.get("x0"), 0.0),
                            self._safe_float(item.get("top"), 0.0),
                            str(item.get("word_id") or item.get("id") or ""),
                        ),
                    )
                    for word in words:
                        word_id = str(word.get("word_id") or word.get("id") or "").strip()
                        if not word_id:
                            continue
                        ordered_word_ids.append(word_id)
                for item in list(row.get("word_ids") or [])[:180]:
                    token = str(item).strip()
                    if token:
                        ordered_word_ids.append(token)
            return list(dict.fromkeys(ordered_word_ids))[:limit]

        def _collect_anchor_sources_from_block_ids(
            source_block_ids: Sequence[str],
            *,
            word_limit: int = 480,
            char_limit: int = 240,
        ) -> Tuple[List[str], List[Dict[str, str]]]:
            word_ids: List[str] = []
            char_ranges: List[Dict[str, str]] = []
            seen_char_pairs: set[str] = set()
            for block_id in list(source_block_ids or []):
                src_anchor = dict((block_map.get(str(block_id).strip()) or {}).get("source_anchor") or {})
                for item in list(src_anchor.get("source_word_ids") or [])[:600]:
                    token = str(item).strip()
                    if token:
                        word_ids.append(token)
                for rng in list(src_anchor.get("source_char_ranges") or [])[:400]:
                    if not isinstance(rng, dict):
                        continue
                    start_id = str(rng.get("start_char_id") or "").strip()
                    end_id = str(rng.get("end_char_id") or "").strip()
                    if not start_id or not end_id:
                        continue
                    key = f"{start_id}:{end_id}"
                    if key in seen_char_pairs:
                        continue
                    seen_char_pairs.add(key)
                    char_ranges.append({"start_char_id": start_id, "end_char_id": end_id})
            word_ids = list(dict.fromkeys(word_ids))[:word_limit]
            char_ranges = char_ranges[:char_limit]
            return word_ids, char_ranges

        def _resolve_group_anchor_sources(
            *,
            seg_word_ids: Sequence[str],
            seg_char_ranges: Sequence[Dict[str, str]],
            source_block_ids: Sequence[str],
            row_lines: Sequence[Dict[str, Any]],
        ) -> Tuple[List[str], List[Dict[str, str]]]:
            row_word_ids = _word_ids_from_line_rows(row_lines)
            block_word_ids, block_char_ranges = _collect_anchor_sources_from_block_ids(source_block_ids)

            chosen_word_ids: List[str] = []
            if row_word_ids:
                chosen_word_ids = list(row_word_ids)
            elif block_word_ids:
                chosen_word_ids = list(block_word_ids)
            elif seg_word_ids:
                chosen_word_ids = [str(item).strip() for item in list(seg_word_ids or []) if str(item).strip()]

            chosen_word_ids = list(dict.fromkeys(chosen_word_ids))[:480]

            chosen_char_ranges: List[Dict[str, str]] = []
            if chosen_word_ids:
                chosen_char_ranges = _char_ranges_from_word_ids(chosen_word_ids)
            if not chosen_char_ranges and block_char_ranges:
                chosen_char_ranges = list(block_char_ranges)
            if not chosen_char_ranges:
                chosen_char_ranges = [
                    {
                        "start_char_id": str(item.get("start_char_id") or "").strip(),
                        "end_char_id": str(item.get("end_char_id") or "").strip(),
                    }
                    for item in list(seg_char_ranges or [])[:240]
                    if isinstance(item, dict)
                    and str(item.get("start_char_id") or "").strip()
                    and str(item.get("end_char_id") or "").strip()
                ]
            if not chosen_word_ids and chosen_char_ranges:
                chosen_word_ids = _word_ids_from_char_ranges(chosen_char_ranges)

            return chosen_word_ids[:480], chosen_char_ranges[:240]

        for idx, seg in enumerate(seg_rows, start=1):
            if not isinstance(seg, dict):
                continue
            seg_id = str(seg.get("segment_id") or f"seg_{idx}").strip() or f"seg_{idx}"
            block_ids = [
                self._normalize_canonical_block_id(page=page, raw_id=str(item))
                for item in list(seg.get("block_ids") or [])[:8]
                if self._normalize_canonical_block_id(page=page, raw_id=str(item))
            ]
            line_ids = [
                str(item).strip()
                for item in list(seg.get("line_ids") or [])[:80]
                if str(item).strip()
            ]
            explicit_evidence_line_ids = [
                str(item).strip()
                for item in list(seg.get("evidence_line_ids") or [])[:40]
                if str(item).strip()
            ]
            source_word_ids = [
                str(item).strip()
                for item in list(seg.get("word_ids") or [])[:400]
                if str(item).strip()
            ]
            source_char_ranges = [
                {
                    "start_char_id": str(item.get("start_char_id") or "").strip(),
                    "end_char_id": str(item.get("end_char_id") or "").strip(),
                }
                for item in list(seg.get("char_ranges") or [])[:240]
                if isinstance(item, dict)
                and str(item.get("start_char_id") or "").strip()
                and str(item.get("end_char_id") or "").strip()
            ]
            if not source_word_ids and source_char_ranges:
                source_word_ids = _word_ids_from_char_ranges(source_char_ranges)
            segment_text_hint = self._normalize_spaces(
                str(seg.get("resolved_text") or seg.get("text") or "")
            )
            evidence_line_ids = list(explicit_evidence_line_ids)
            if not evidence_line_ids:
                evidence_line_ids = list(line_ids)

            seg_lines = [line_map[line_id] for line_id in line_ids if line_id in line_map]

            groups: List[Dict[str, Any]] = []
            if len(block_ids) > 1:
                # Respect parser block boundaries to avoid merging two source paragraphs.
                for canonical in block_ids:
                    src_block = block_map.get(canonical)
                    src_anchor = dict((src_block or {}).get("source_anchor") or {})
                    start_raw = src_anchor.get("start_char")
                    end_raw = src_anchor.get("end_char")
                    start = self._safe_int(start_raw, -1) if start_raw is not None else -1
                    end = self._safe_int(end_raw, -1) if end_raw is not None else -1
                    owned_lines: List[Dict[str, Any]] = []
                    if start >= 0 and end > start and seg_lines:
                        for line in seg_lines:
                            line_start_raw = line.get("start_char")
                            line_end_raw = line.get("end_char")
                            line_start = self._safe_int(line_start_raw, -1) if line_start_raw is not None else -1
                            line_end = self._safe_int(line_end_raw, -1) if line_end_raw is not None else -1
                            if line_start < 0 or line_end <= line_start:
                                continue
                            mid = line_start + ((line_end - line_start) / 2.0)
                            if float(start) <= mid <= float(end):
                                owned_lines.append(line)
                    if owned_lines:
                        groups.extend(
                            {
                                "canonical": canonical,
                                "source_block_ids": [canonical],
                                "lines": split_rows,
                            }
                            for split_rows in self._split_lines_by_large_vertical_gap(owned_lines)
                            if split_rows
                        )
                    elif src_block:
                        groups.append(
                            {
                                "canonical": canonical,
                                "source_block_ids": [canonical],
                                "lines": [],
                                "fallback_text": self._normalize_spaces(str(src_block.get("text") or "")),
                                "source_anchor": dict(src_anchor),
                            }
                        )
            else:
                if seg_lines:
                    canonical = ""
                    if block_ids:
                        canonical = block_ids[0]
                    elif explicit_evidence_line_ids or source_word_ids or source_char_ranges:
                        canonical = f"p{int(page)}_seg_{seg_id}"
                    else:
                        fb_id = str((fallback_block or {}).get("id") or "b1")
                        canonical = self._normalize_canonical_block_id(page=page, raw_id=fb_id) or f"p{int(page)}_b1"
                    groups.extend(
                        {
                            "canonical": canonical,
                            "source_block_ids": list(block_ids),
                            "lines": split_rows,
                        }
                        for split_rows in self._split_lines_by_large_vertical_gap(seg_lines)
                        if split_rows
                    )
                else:
                    canonical = ""
                    if block_ids:
                        canonical = block_ids[0]
                    elif explicit_evidence_line_ids or source_word_ids or source_char_ranges:
                        canonical = f"p{int(page)}_seg_{seg_id}"
                    else:
                        fb_id = str((fallback_block or {}).get("id") or "b1")
                        canonical = self._normalize_canonical_block_id(page=page, raw_id=fb_id) or f"p{int(page)}_b1"
                    fallback_text = ""
                    if block_ids and block_map.get(block_ids[0]):
                        fallback_text = self._normalize_spaces(str((block_map.get(block_ids[0]) or {}).get("text") or ""))
                    elif segment_text_hint:
                        fallback_text = segment_text_hint
                    elif fallback_block:
                        fallback_text = self._normalize_spaces(str((fallback_block or {}).get("text") or ""))
                    if fallback_text:
                        groups.append(
                            {
                                "canonical": canonical,
                                "source_block_ids": list(block_ids),
                                "lines": [],
                                "fallback_text": fallback_text,
                                "source_anchor": dict(((fallback_block or {}).get("source_anchor") or {})),
                            }
                        )

            for part_idx, group in enumerate(groups, start=1):
                canonical = str(group.get("canonical") or "").strip() or f"p{int(page)}_seg_{seg_id}_{part_idx}"
                source_block_ids = [
                    self._normalize_canonical_block_id(page=page, raw_id=str(item))
                    for item in list(group.get("source_block_ids") or [])[:8]
                    if self._normalize_canonical_block_id(page=page, raw_id=str(item))
                ]
                row_lines = list(group.get("lines") or [])
                kind = _resolve_group_kind(
                    seg=seg,
                    source_block_ids=source_block_ids,
                    row_lines=row_lines,
                )
                group_source_word_ids, group_source_char_ranges = _resolve_group_anchor_sources(
                    seg_word_ids=source_word_ids,
                    seg_char_ranges=source_char_ranges,
                    source_block_ids=source_block_ids,
                    row_lines=row_lines,
                )
                line_id_set: List[str] = []
                evidence_set: List[str] = []
                if row_lines:
                    row_lines = sorted(
                        row_lines,
                        key=lambda item: (
                            self._safe_int(item.get("order"), 0),
                            float(item.get("top") or 0.0),
                            str(item.get("line_id") or ""),
                        ),
                    )
                    text = self._normalize_spaces(" ".join(str(item.get("text") or "") for item in row_lines))
                    if not text and segment_text_hint:
                        text = segment_text_hint
                    if not text and group_source_word_ids:
                        text = self._normalize_spaces(
                            " ".join(
                                self._normalize_spaces(str((word_map.get(word_id) or {}).get("text") or ""))
                                for word_id in group_source_word_ids
                            )
                        )
                    start_char = min(self._safe_int(item.get("start_char"), 0) for item in row_lines)
                    end_char = max(self._safe_int(item.get("end_char"), 0) for item in row_lines)
                    line_id_set = [str(item.get("line_id") or "").strip() for item in row_lines if str(item.get("line_id") or "").strip()]
                    evidence_set = [line_id for line_id in evidence_line_ids if line_id in set(line_id_set)] or list(line_id_set)
                    bbox = self._build_bbox_hint(
                        style_cues=dict(base_payload.get("style_cues") or {}) if isinstance(base_payload, dict) else {},
                        quote_text=text,
                        line_rows=row_lines,
                    )
                    source_anchor = {
                        "page": int(page),
                        "start_char": int(start_char),
                        "end_char": int(max(start_char + 1, end_char)),
                        "quote_text": text[:280] if text else None,
                        "canonical_block_id": canonical,
                        "coord_version": "anchor_v2",
                        "anchor_confidence": 0.9,
                        "anchor_id": f"{canonical}:{start_char}:{end_char}",
                        "anchor_v2": {
                            "coord_version": "anchor_v2",
                            "canonical_block_id": canonical,
                            "page": int(page),
                            "start_char": int(start_char),
                            "end_char": int(max(start_char + 1, end_char)),
                        },
                    }
                    if isinstance(bbox, dict):
                        source_anchor["bbox_hint"] = bbox
                    source_anchor["source_word_ids"] = list(group_source_word_ids)
                    source_anchor["source_char_ranges"] = list(group_source_char_ranges)
                    if group_source_word_ids:
                        word_boxes = []
                        for word_id in group_source_word_ids:
                            row = word_map.get(word_id)
                            if not isinstance(row, dict):
                                continue
                            word_boxes.append(
                                {
                                    "x0": self._safe_float(row.get("x0"), 0.0),
                                    "x1": self._safe_float(row.get("x1"), 0.0),
                                    "top": self._safe_float(row.get("top"), 0.0),
                                    "bottom": self._safe_float(row.get("bottom"), 0.0),
                                }
                            )
                        geometry = self._build_anchor_geometry(
                            boxes=word_boxes,
                            page_width=page_width_native,
                            page_height=page_height_native,
                            source="word_union",
                        )
                        if isinstance(geometry, dict):
                            source_anchor["geometry_version"] = "poly_v1"
                            source_anchor["geometry"] = geometry
                    elif row_lines:
                        geometry = self._build_anchor_geometry(
                            boxes=[
                                {
                                    "x0": self._safe_float(item.get("x0"), 0.0),
                                    "x1": self._safe_float(item.get("x1"), 0.0),
                                    "top": self._safe_float(item.get("top"), 0.0),
                                    "bottom": self._safe_float(item.get("bottom"), 0.0),
                                }
                                for item in row_lines
                                if isinstance(item, dict)
                            ],
                            page_width=page_width_native,
                            page_height=page_height_native,
                            source="line_union",
                        )
                        if isinstance(geometry, dict):
                            source_anchor["geometry_version"] = "poly_v1"
                            source_anchor["geometry"] = geometry
                else:
                    text = self._normalize_spaces(str(group.get("fallback_text") or ""))
                    if not text and segment_text_hint:
                        text = segment_text_hint
                    if not text and group_source_word_ids:
                        text = self._normalize_spaces(
                            " ".join(
                                self._normalize_spaces(str((word_map.get(word_id) or {}).get("text") or ""))
                                for word_id in group_source_word_ids
                            )
                        )
                    source_anchor = dict(group.get("source_anchor") or {})
                    span_start, span_end = _char_span_from_sources(
                        word_ids=group_source_word_ids,
                        char_ranges=group_source_char_ranges,
                    )
                    source_anchor["page"] = int(page)
                    source_anchor["start_char"] = int(span_start)
                    source_anchor["end_char"] = int(span_end)
                    source_anchor["canonical_block_id"] = canonical
                    source_anchor["coord_version"] = "anchor_v2"
                    source_anchor["anchor_confidence"] = float(source_anchor.get("anchor_confidence") or 0.86)
                    source_anchor["anchor_id"] = str(
                        source_anchor.get("anchor_id")
                        or f"{canonical}:{source_anchor.get('start_char') or 0}:{source_anchor.get('end_char') or 1}"
                    )
                    source_anchor["anchor_v2"] = {
                        "coord_version": "anchor_v2",
                        "canonical_block_id": canonical,
                        "page": int(page),
                        "start_char": int(span_start),
                        "end_char": int(span_end),
                    }
                    source_anchor["source_word_ids"] = list(group_source_word_ids)
                    source_anchor["source_char_ranges"] = list(group_source_char_ranges)
                    line_id_set = list(line_ids)
                    evidence_set = [line_id for line_id in evidence_line_ids if line_id in set(line_id_set)] or list(line_id_set)
                    bbox_hint = _build_word_bbox_hint(group_source_word_ids)
                    if isinstance(bbox_hint, dict):
                        source_anchor["bbox_hint"] = bbox_hint
                    if group_source_word_ids:
                        word_boxes = []
                        for word_id in group_source_word_ids:
                            row = word_map.get(word_id)
                            if not isinstance(row, dict):
                                continue
                            word_boxes.append(
                                {
                                    "x0": self._safe_float(row.get("x0"), 0.0),
                                    "x1": self._safe_float(row.get("x1"), 0.0),
                                    "top": self._safe_float(row.get("top"), 0.0),
                                    "bottom": self._safe_float(row.get("bottom"), 0.0),
                                }
                            )
                        geometry = self._build_anchor_geometry(
                            boxes=word_boxes,
                            page_width=page_width_native,
                            page_height=page_height_native,
                            source="word_union",
                        )
                        if isinstance(geometry, dict):
                            source_anchor["geometry_version"] = "poly_v1"
                            source_anchor["geometry"] = geometry

                if not text:
                    continue
                if kind == "heading":
                    text = self._repair_heading_text(text)

                output.append(
                    {
                        "id": canonical,
                        "kind": kind,
                        "kind_hint": str(seg.get("kind_hint") or seg.get("kind") or "paragraph"),
                        "component_hint": str(seg.get("component_hint") or seg.get("ui_component") or ""),
                        "text": text,
                        "order": len(output),
                        "section_title": self._normalize_spaces(str(seg.get("title") or "")) or str((fallback_block or {}).get("section_title") or "Body"),
                        "source_anchor": source_anchor,
                        "source_block_ids": source_block_ids,
                        "source_line_ids": line_id_set,
                        "evidence_line_ids": evidence_set,
                        "source_word_ids": group_source_word_ids,
                        "source_char_ranges": group_source_char_ranges,
                        "zone_type": "main_body",
                        "column_id": str((row_lines[0] if row_lines else {}).get("column_label") or "main"),
                        "heading_prob": 0.92 if kind == "heading" else 0.0,
                        "layout_confidence": 0.9,
                    }
                )
        return output

    def _build_main_blocks_from_page_structure(
        self,
        *,
        page: int,
        page_structure: Optional[Dict[str, Any]],
        base_payload: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build renderable main blocks directly from AI-structured page blocks.
        Content source-of-truth is page_structure_v3.block_groups.
        Parser/line heuristics are not used for text composition here.
        """
        structure = dict(page_structure or {})
        block_groups = [row for row in list(structure.get("block_groups") or []) if isinstance(row, dict)]
        if not block_groups:
            return []

        native_extract = dict(((base_payload or {}).get("native_page_extract") or {}))
        native_words = [row for row in list(native_extract.get("words") or []) if isinstance(row, dict)]
        word_map: Dict[str, Dict[str, Any]] = {}
        for row in native_words:
            word_id = str(row.get("word_id") or "").strip()
            if word_id:
                word_map[word_id] = row

        native_chars = [row for row in list(native_extract.get("chars") or []) if isinstance(row, dict)]
        char_order_map: Dict[str, int] = {}
        for idx, row in enumerate(native_chars):
            char_id = str(row.get("char_id") or "").strip()
            if char_id:
                char_order_map[char_id] = idx

        word_char_span_map: Dict[str, Tuple[int, int]] = {}
        for idx, row in enumerate(native_words):
            word_id = str(row.get("word_id") or "").strip()
            if not word_id:
                continue
            start_id = str(row.get("start_char_id") or "").strip()
            end_id = str(row.get("end_char_id") or "").strip()
            start_ord = char_order_map.get(start_id, idx * 2)
            end_ord = char_order_map.get(end_id, start_ord + 1)
            if end_ord < start_ord:
                end_ord = start_ord
            word_char_span_map[word_id] = (start_ord, end_ord)

        page_meta = dict(native_extract.get("page_meta") or {})
        style_cues = dict(((base_payload or {}).get("style_cues") or {}))
        page_width_native = self._safe_float(
            page_meta.get("page_width"),
            self._safe_float(style_cues.get("page_width"), 0.0),
        )
        page_height_native = self._safe_float(
            page_meta.get("page_height"),
            self._safe_float(style_cues.get("page_height"), 0.0),
        )

        def _kind(token: str) -> str:
            raw = str(token or "").strip().lower()
            if raw in {"heading"}:
                return "heading"
            if raw in {"list_item", "list"}:
                return "list_item"
            if raw in {"caption", "figure_meta", "table_caption"}:
                return "caption"
            return "paragraph"

        def _word_ids_from_char_ranges(
            char_ranges: Sequence[Dict[str, str]],
            *,
            limit: int = 600,
        ) -> List[str]:
            spans: List[Tuple[int, int]] = []
            for row in list(char_ranges or []):
                if not isinstance(row, dict):
                    continue
                start_id = str(row.get("start_char_id") or "").strip()
                end_id = str(row.get("end_char_id") or "").strip()
                if not start_id or not end_id:
                    continue
                start_ord = char_order_map.get(start_id)
                end_ord = char_order_map.get(end_id)
                if start_ord is None and end_ord is None:
                    continue
                if start_ord is None:
                    start_ord = end_ord
                if end_ord is None:
                    end_ord = start_ord
                if start_ord is None or end_ord is None:
                    continue
                if end_ord < start_ord:
                    start_ord, end_ord = end_ord, start_ord
                spans.append((start_ord, end_ord))
            if not spans:
                return []
            rows: List[Tuple[int, str]] = []
            for word_id, (word_start, word_end) in word_char_span_map.items():
                for span_start, span_end in spans:
                    if word_end < span_start or word_start > span_end:
                        continue
                    rows.append((word_start, word_id))
                    break
            rows = sorted(rows, key=lambda item: (item[0], item[1]))
            return list(dict.fromkeys(word_id for _, word_id in rows))[:limit]

        def _char_ranges_from_word_ids(
            word_ids: Sequence[str],
            *,
            limit: int = 320,
        ) -> List[Dict[str, str]]:
            rows: List[Dict[str, str]] = []
            seen: set[str] = set()
            for word_id in list(word_ids or []):
                row = word_map.get(str(word_id).strip())
                if not isinstance(row, dict):
                    continue
                start_id = str(row.get("start_char_id") or "").strip()
                end_id = str(row.get("end_char_id") or "").strip()
                if not start_id or not end_id:
                    continue
                key = f"{start_id}:{end_id}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"start_char_id": start_id, "end_char_id": end_id})
                if len(rows) >= limit:
                    break
            return rows

        ordered_groups = sorted(
            block_groups,
            key=lambda row: (
                self._safe_int(row.get("reading_order"), 10**9),
                str(row.get("block_id") or ""),
            ),
        )

        output: List[Dict[str, Any]] = []
        for idx, group in enumerate(ordered_groups, start=1):
            zone_type = str(group.get("zone_type") or "").strip().lower()
            if zone_type in {"side_context", "figure_meta"}:
                continue

            text = self._normalize_spaces(str(group.get("text") or ""))
            if not text:
                continue

            block_id_raw = str(group.get("block_id") or "").strip() or f"v3_{idx}"
            canonical = self._normalize_canonical_block_id(page=page, raw_id=block_id_raw) or f"p{int(page)}_{block_id_raw}"

            word_ids = [
                str(item).strip()
                for item in list(group.get("word_ids") or [])[:800]
                if str(item).strip() and str(item).strip() in word_map
            ]
            word_ids = list(dict.fromkeys(word_ids))

            char_ranges = [
                {
                    "start_char_id": str(item.get("start_char_id") or "").strip(),
                    "end_char_id": str(item.get("end_char_id") or "").strip(),
                }
                for item in list(group.get("char_ranges") or [])[:500]
                if isinstance(item, dict)
                and str(item.get("start_char_id") or "").strip()
                and str(item.get("end_char_id") or "").strip()
            ]
            if not word_ids and char_ranges:
                word_ids = _word_ids_from_char_ranges(char_ranges)
            if word_ids and not char_ranges:
                char_ranges = _char_ranges_from_word_ids(word_ids)

            start_chars: List[int] = []
            end_chars: List[int] = []
            for word_id in word_ids:
                span = word_char_span_map.get(word_id)
                if not span:
                    continue
                start_chars.append(int(span[0]))
                end_chars.append(int(span[1]) + 1)
            if not start_chars or not end_chars:
                for row in char_ranges:
                    start_ord = char_order_map.get(str(row.get("start_char_id") or "").strip())
                    end_ord = char_order_map.get(str(row.get("end_char_id") or "").strip())
                    if start_ord is None and end_ord is None:
                        continue
                    if start_ord is None:
                        start_ord = end_ord
                    if end_ord is None:
                        end_ord = start_ord
                    if start_ord is None or end_ord is None:
                        continue
                    lo, hi = min(int(start_ord), int(end_ord)), max(int(start_ord), int(end_ord)) + 1
                    start_chars.append(lo)
                    end_chars.append(hi)
            start_char = min(start_chars) if start_chars else 0
            end_char = max(end_chars) if end_chars else max(1, len(text))
            if end_char <= start_char:
                end_char = start_char + max(1, len(text))

            bbox_hint = None
            word_boxes: List[Dict[str, float]] = []
            for word_id in word_ids:
                row = word_map.get(word_id)
                if not isinstance(row, dict):
                    continue
                x0 = self._safe_float(row.get("x0"), 0.0)
                x1 = self._safe_float(row.get("x1"), 0.0)
                top = self._safe_float(row.get("top"), 0.0)
                bottom = self._safe_float(row.get("bottom"), 0.0)
                if x1 <= x0 or bottom <= top:
                    continue
                word_boxes.append({"x0": x0, "x1": x1, "top": top, "bottom": bottom})
            if word_boxes:
                bbox_hint = {
                    "x0": min(float(item["x0"]) for item in word_boxes),
                    "x1": max(float(item["x1"]) for item in word_boxes),
                    "top": min(float(item["top"]) for item in word_boxes),
                    "bottom": max(float(item["bottom"]) for item in word_boxes),
                    "page_width": page_width_native if page_width_native > 0 else None,
                    "page_height": page_height_native if page_height_native > 0 else None,
                }

            layout_geo = dict(group.get("layout_bbox_or_polygon") or {})
            layout_bbox = dict(layout_geo.get("bbox") or {}) if isinstance(layout_geo.get("bbox"), dict) else {}
            layout_polygon_raw = list(layout_geo.get("polygon") or []) if isinstance(layout_geo.get("polygon"), list) else []
            layout_polygon = [
                {
                    "x": self._safe_float(item.get("x"), 0.0),
                    "y": self._safe_float(item.get("y"), 0.0),
                }
                for item in layout_polygon_raw
                if isinstance(item, dict)
            ]
            if not bbox_hint and layout_bbox:
                x0 = self._safe_float(layout_bbox.get("x0"), 0.0)
                x1 = self._safe_float(layout_bbox.get("x1"), 0.0)
                top = self._safe_float(layout_bbox.get("top"), 0.0)
                bottom = self._safe_float(layout_bbox.get("bottom"), 0.0)
                if x1 > x0 and bottom > top:
                    bbox_hint = {
                        "x0": x0,
                        "x1": x1,
                        "top": top,
                        "bottom": bottom,
                        "page_width": page_width_native if page_width_native > 0 else None,
                        "page_height": page_height_native if page_height_native > 0 else None,
                    }
            if not bbox_hint and len(layout_polygon) >= 3:
                bbox_hint = {
                    "x0": min(float(item["x"]) for item in layout_polygon),
                    "x1": max(float(item["x"]) for item in layout_polygon),
                    "top": min(float(item["y"]) for item in layout_polygon),
                    "bottom": max(float(item["y"]) for item in layout_polygon),
                    "page_width": page_width_native if page_width_native > 0 else None,
                    "page_height": page_height_native if page_height_native > 0 else None,
                }

            source_anchor: Dict[str, Any] = {
                "page": int(page),
                "start_char": int(start_char),
                "end_char": int(end_char),
                "quote_text": text[:280] if text else None,
                "canonical_block_id": canonical,
                "coord_version": "anchor_v2",
                "anchor_confidence": max(0.7, min(1.0, self._safe_float(group.get("confidence"), 0.86))),
                "anchor_id": f"{canonical}:{int(start_char)}:{int(end_char)}",
                "anchor_v2": {
                    "coord_version": "anchor_v2",
                    "canonical_block_id": canonical,
                    "page": int(page),
                    "start_char": int(start_char),
                    "end_char": int(end_char),
                },
                "source_word_ids": list(word_ids)[:600],
                "source_char_ranges": list(char_ranges)[:320],
            }
            if isinstance(bbox_hint, dict):
                source_anchor["bbox_hint"] = bbox_hint
            if word_boxes:
                geometry = self._build_anchor_geometry(
                    boxes=word_boxes,
                    page_width=page_width_native,
                    page_height=page_height_native,
                    source="word_union",
                )
                if isinstance(geometry, dict):
                    source_anchor["geometry_version"] = "poly_v1"
                    source_anchor["geometry"] = geometry
            elif len(layout_polygon) >= 3:
                source_anchor["geometry_version"] = "poly_v1"
                source_anchor["geometry"] = {
                    "polygons": [
                        {
                            "points": layout_polygon,
                            "source": "docmind_layout",
                            "component_id": canonical,
                        }
                    ],
                    "page_width": page_width_native if page_width_native > 0 else None,
                    "page_height": page_height_native if page_height_native > 0 else None,
                }

            output.append(
                {
                    "id": canonical,
                    "kind": _kind(str(group.get("kind") or "")),
                    "kind_hint": str(group.get("kind") or ""),
                    "component_hint": str(group.get("component_hint") or group.get("ui_component") or ""),
                    "text": text,
                    "order": len(output),
                    "section_title": self._normalize_spaces(str(group.get("title") or "")),
                    "source_anchor": source_anchor,
                    "source_block_ids": [canonical],
                    "source_line_ids": [],
                    "evidence_line_ids": [],
                    "source_word_ids": list(word_ids)[:600],
                    "source_char_ranges": list(char_ranges)[:320],
                    "zone_type": "main_body",
                    "column_id": str(group.get("column_id") or "main"),
                    "heading_prob": 0.92 if _kind(str(group.get("kind") or "")) == "heading" else 0.0,
                    "layout_confidence": max(0.0, min(1.0, self._safe_float(group.get("confidence"), 0.86))),
                }
            )
        return output

    @staticmethod
    def _split_lines_by_large_vertical_gap(lines: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        ordered = sorted(
            [item for item in lines if isinstance(item, dict)],
            key=lambda item: (
                LiteratureReaderComposeService._safe_int(item.get("order"), 0),
                float(item.get("top") or 0.0),
                str(item.get("line_id") or ""),
            ),
        )
        if not ordered:
            return []
        groups: List[List[Dict[str, Any]]] = [[ordered[0]]]
        for row in ordered[1:]:
            prev = groups[-1][-1]
            prev_bottom = float(prev.get("bottom") or 0.0)
            prev_top = float(prev.get("top") or prev_bottom)
            prev_height = max(1.0, float(prev.get("height") or max(0.0, prev_bottom - prev_top)))
            top = float(row.get("top") or 0.0)
            gap = max(0.0, top - prev_bottom)
            if gap > max(18.0, prev_height * 1.45):
                groups.append([row])
            else:
                groups[-1].append(row)
        return groups

    def _build_segmented_anchor_refs(
        self,
        *,
        anchor: Dict[str, Any],
        page: int,
        quote_text: str,
        style_cues: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        normalized = self._normalize_anchor_ref(
            anchor=anchor,
            page=page,
            quote_text=quote_text,
            source_block_id=str(anchor.get("canonical_block_id") or ""),
        )
        if not normalized:
            return []
        normalized["anchor_id"] = str(
            normalized.get("anchor_id")
            or f"{normalized.get('canonical_block_id') or 'anchor'}:{normalized.get('start_char') or 0}:{normalized.get('end_char') or 1}"
        )
        bbox_hint = self._build_bbox_hint(
            style_cues=dict(style_cues or {}),
            quote_text=str(normalized.get("quote_text") or quote_text or ""),
            source_anchor=normalized,
        )
        if isinstance(bbox_hint, dict):
            normalized["bbox_hint"] = bbox_hint
        geometry = anchor.get("geometry")
        if isinstance(geometry, dict):
            normalized["geometry_version"] = str(anchor.get("geometry_version") or "poly_v1")
            normalized["geometry"] = geometry
        source_word_ids = [
            str(item).strip()
            for item in list(anchor.get("source_word_ids") or [])[:600]
            if str(item).strip()
        ]
        if source_word_ids:
            normalized["source_word_ids"] = source_word_ids
        source_char_ranges = [
            {
                "start_char_id": str(item.get("start_char_id") or "").strip(),
                "end_char_id": str(item.get("end_char_id") or "").strip(),
            }
            for item in list(anchor.get("source_char_ranges") or [])[:400]
            if isinstance(item, dict)
            and str(item.get("start_char_id") or "").strip()
            and str(item.get("end_char_id") or "").strip()
        ]
        if source_char_ranges:
            normalized["source_char_ranges"] = source_char_ranges
        normalized["segment_index"] = None
        normalized["segment_total"] = None
        return [normalized]

    def _evaluate_anchor_metrics(
        self,
        *,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        components = self._flatten_components(list(ui_plan.get("components") or []))
        raw_text = self._normalize_spaces(str(base_payload.get("raw_text") or ""))
        style_cues = dict(base_payload.get("style_cues") or {})
        anchors: List[Tuple[Dict[str, Any], str]] = []
        for node in components:
            if not isinstance(node, dict):
                continue
            node_text = self._normalize_spaces(self._extract_node_text(node))
            for row in list(node.get("source_anchor_refs") or []):
                if isinstance(row, dict):
                    anchors.append((row, node_text))
        if not anchors:
            return {"hit_rate": 1.0, "bbox_iou": 1.0, "misjump_rate": 0.0, "gate_passed": True, "total": 0}

        hit_count = 0
        iou_scores: List[float] = []
        misjump = 0
        for anchor, node_text in anchors:
            quote_text = self._normalize_spaces(str(anchor.get("quote_text") or ""))
            if not quote_text:
                quote_text = node_text[:260]
            hit = False
            if node_text and quote_text:
                if quote_text.lower() in node_text.lower() or node_text.lower() in quote_text.lower():
                    hit = True
                else:
                    hit = self._token_overlap_ratio(quote_text, node_text) >= 0.45
            if not hit and raw_text and quote_text:
                hit = quote_text.lower() in raw_text.lower()
            if hit:
                hit_count += 1
            else:
                misjump += 1

            predicted_bbox = self._build_bbox_hint(
                style_cues=style_cues,
                quote_text=quote_text or node_text,
                source_anchor=anchor,
            )
            given_bbox = dict(anchor.get("bbox_hint") or {})
            if isinstance(predicted_bbox, dict) and isinstance(given_bbox, dict) and given_bbox:
                iou_scores.append(self._bbox_iou(predicted_bbox, given_bbox))

        total = len(anchors)
        hit_rate = hit_count / max(1, total)
        misjump_rate = misjump / max(1, total)
        bbox_iou = sum(iou_scores) / len(iou_scores) if iou_scores else 1.0
        gate_passed = bool(
            hit_rate >= float(getattr(settings, "reader_anchor_eval_min_hit_rate", 0.8) or 0.8)
            and bbox_iou >= float(getattr(settings, "reader_anchor_eval_min_iou", 0.25) or 0.25)
            and misjump_rate <= float(getattr(settings, "reader_anchor_eval_max_misjump", 0.2) or 0.2)
        )
        return {
            "hit_rate": round(max(0.0, min(1.0, hit_rate)), 4),
            "bbox_iou": round(max(0.0, min(1.0, bbox_iou)), 4),
            "misjump_rate": round(max(0.0, min(1.0, misjump_rate)), 4),
            "gate_passed": gate_passed,
            "total": int(total),
        }

    def _apply_node_level_anchor_gate(
        self,
        *,
        ui_plan: Dict[str, Any],
        base_payload: Dict[str, Any],
        page: int,
    ) -> Dict[str, Any]:
        cloned = json.loads(json.dumps(ui_plan, ensure_ascii=False))
        components = list(cloned.get("components") or [])
        allowed_canonical = self._build_allowed_canonical_block_ids(base_payload=base_payload, page=page)
        min_conf = float(getattr(settings, "reader_anchor_min_confidence", 0.78) or 0.78)

        total_nodes = 0
        blocked_nodes = 0

        for node in self._flatten_components(components):
            if not isinstance(node, dict):
                continue
            refs = list(node.get("source_anchor_refs") or [])
            if not refs:
                continue
            total_nodes += 1
            normalized_rows: List[Dict[str, Any]] = []
            source_block_id = str((node.get("props") or {}).get("source_block_id") or "")
            for row in refs:
                if not isinstance(row, dict):
                    continue
                if self._safe_int(row.get("end_char"), 0) <= self._safe_int(row.get("start_char"), 0):
                    continue
                normalized = self._normalize_anchor_ref(
                    anchor=row,
                    page=page,
                    quote_text=str(row.get("quote_text") or ""),
                    source_block_id=source_block_id,
                )
                if not normalized:
                    continue
                canonical = str(normalized.get("canonical_block_id") or "")
                if allowed_canonical and canonical and canonical not in allowed_canonical:
                    continue
                if float(normalized.get("anchor_confidence") or 0.0) < min_conf:
                    continue
                if normalized.get("segment_index") is not None or normalized.get("segment_total") is not None:
                    continue
                normalized_rows.append(normalized)

            props = node.get("props") if isinstance(node.get("props"), dict) else {}
            if normalized_rows:
                node["source_anchor_refs"] = normalized_rows
                props["node_gate_passed"] = True
            else:
                node["source_anchor_refs"] = []
                props["node_gate_passed"] = False
                blocked_nodes += 1
                actions = [
                    item for item in list(node.get("actions") or [])
                    if str((item or {}).get("key") or "").strip().lower() != "jump_anchor"
                ]
                node["actions"] = actions
                caps = [
                    item for item in list(node.get("capabilities") or [])
                    if str(item).strip().lower() != "jump_anchor"
                ]
                node["capabilities"] = caps
            node["props"] = props

        cloned["components"] = components
        report = {
            "total_nodes": int(total_nodes),
            "blocked_nodes": int(blocked_nodes),
            "passed_nodes": int(max(0, total_nodes - blocked_nodes)),
            "pass_rate": round((max(0, total_nodes - blocked_nodes) / max(1, total_nodes)), 4) if total_nodes else 1.0,
        }
        return {"ui_plan": cloned, "node_gate_report": report}

    @staticmethod
    def _bbox_iou(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        ax0, ax1 = float(a.get("x0") or 0.0), float(a.get("x1") or 0.0)
        ay0, ay1 = float(a.get("top") or 0.0), float(a.get("bottom") or 0.0)
        bx0, bx1 = float(b.get("x0") or 0.0), float(b.get("x1") or 0.0)
        by0, by1 = float(b.get("top") or 0.0), float(b.get("bottom") or 0.0)
        inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
        inter_area = inter_w * inter_h
        if inter_area <= 0:
            return 0.0
        area_a = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
        area_b = max(1e-6, (bx1 - bx0) * (by1 - by0))
        union = area_a + area_b - inter_area
        if union <= 1e-6:
            return 0.0
        return max(0.0, min(1.0, inter_area / union))

    @staticmethod
    def _token_overlap_ratio(a: str, b: str) -> float:
        a_tokens = [item for item in str(a or "").lower().split(" ") if item]
        b_tokens = [item for item in str(b or "").lower().split(" ") if item]
        if not a_tokens or not b_tokens:
            return 0.0
        inter = len(set(a_tokens) & set(b_tokens))
        return inter / max(1, min(len(a_tokens), len(b_tokens)))

    def _build_allowed_canonical_block_ids(self, *, base_payload: Dict[str, Any], page: int) -> set[str]:
        allowed: set[str] = set()
        for row in list(base_payload.get("blocks") or []):
            if not isinstance(row, dict):
                continue
            block_id = str(row.get("id") or "").strip()
            canonical = str(((row.get("source_anchor") or {}).get("canonical_block_id") or "")).strip()
            normalized = self._normalize_canonical_block_id(page=page, raw_id=canonical or block_id)
            if normalized:
                allowed.add(normalized)
        segment_map = dict(base_payload.get("segment_map") or {})
        for row in list(segment_map.get("segments") or []):
            if not isinstance(row, dict):
                continue
            segment_id = str(row.get("segment_id") or "").strip()
            if segment_id:
                allowed.add(f"p{int(page)}_seg_{segment_id}")
        return allowed

    @staticmethod
    def _normalize_canonical_block_id(*, page: int, raw_id: str) -> str:
        token = str(raw_id or "").strip()
        if not token:
            return ""
        if token.startswith(f"p{int(page)}_"):
            return token
        if re.match(r"^p\d+_", token):
            return token
        return f"p{int(page)}_{token}"

    @staticmethod
    @staticmethod
    def _build_caption_insight(caption: str) -> str:
        text = str(caption or "").strip()
        if not text:
            return "该图表用于补充当前页面的关键论点。"
        return f"AI 解读：该图注强调“{text[:80]}”，建议结合对应证据锚点核对结论。"

    @staticmethod
    def _build_compare_insights_stub(summary: str) -> List[Dict[str, str]]:
        seed = str(summary or "").strip()
        if not seed:
            return [
                {"title": "共识点", "content": "当前页与知识库文献在核心任务定义上具有可比性。"},
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
        paper_doi = str(paper.doi or "").strip().lower()
        # Only perform DOI substring match when DOI is non-empty.
        if "doi.org" in normalized_href or (paper_doi and paper_doi in normalized_href):
            return "TL;DR：该链接是论文正式标识入口，可用于快速核对题目、期刊与年份。"
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

    def _sanitize_ui_plan_anchors(
        self,
        ui_plan: Dict[str, Any],
        *,
        page: int,
        base_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Normalize component tree anchors and keep actionable refs only."""
        cloned = json.loads(json.dumps(ui_plan, ensure_ascii=False))
        seen_ids: set[str] = set()
        auto_seq = 0
        allowed_canonical = (
            self._build_allowed_canonical_block_ids(base_payload=base_payload or {}, page=page)
            if isinstance(base_payload, dict)
            else set()
        )
        min_conf = float(getattr(settings, "reader_anchor_min_confidence", 0.78) or 0.78)

        def _next_id(node_type: str) -> str:
            nonlocal auto_seq
            auto_seq += 1
            prefix = re.sub(r"[^a-z0-9]+", "_", str(node_type or "node").strip().lower()).strip("_") or "node"
            return f"{prefix}_{auto_seq}"

        def _walk(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue

                node_type = str(node.get("type") or "").strip()
                if node_type not in COMPONENT_WHITELIST:
                    node_type = "ParagraphProse"
                    node["type"] = node_type
                    raw_props = node.get("props") if isinstance(node.get("props"), dict) else {}
                    text = self._normalize_spaces(str(raw_props.get("text") or "")) if isinstance(raw_props, dict) else ""
                    node["props"] = {"text": text or "内容待修复"}
                    node["children"] = []

                node_id = str(node.get("id") or "").strip()
                if not node_id or node_id in seen_ids:
                    # Missing or duplicate node IDs make node-level patching unstable.
                    # Rebuild a deterministic ID in one place.
                    node_id = _next_id(node_type)
                node["id"] = node_id
                seen_ids.add(node_id)

                if not isinstance(node.get("props"), dict):
                    node["props"] = {}
                if not isinstance(node.get("children"), list):
                    node["children"] = []

                anchors = node.get("source_anchor_refs")
                if isinstance(anchors, list):
                    normalized_rows: List[Dict[str, Any]] = []
                    source_block_id = str((node.get("props") or {}).get("source_block_id") or "")
                    for row in anchors:
                        if not isinstance(row, dict):
                            continue
                        if self._safe_int(row.get("end_char"), 0) <= self._safe_int(row.get("start_char"), 0):
                            continue
                        normalized = self._normalize_anchor_ref(
                            anchor=row,
                            page=page,
                            quote_text=str(row.get("quote_text") or ""),
                            source_block_id=source_block_id,
                        )
                        if normalized:
                            canonical = str(normalized.get("canonical_block_id") or "")
                            if allowed_canonical and canonical and canonical not in allowed_canonical:
                                continue
                            if float(normalized.get("anchor_confidence") or 0.0) < min_conf:
                                continue
                            if normalized.get("segment_index") is not None or normalized.get("segment_total") is not None:
                                continue
                            bbox_hint = row.get("bbox_hint")
                            if isinstance(bbox_hint, dict):
                                normalized["bbox_hint"] = bbox_hint
                            geometry = row.get("geometry")
                            if isinstance(geometry, dict):
                                normalized["geometry_version"] = str(row.get("geometry_version") or "poly_v1")
                                normalized["geometry"] = geometry
                            source_word_ids = [
                                str(item).strip()
                                for item in list(row.get("source_word_ids") or [])[:600]
                                if str(item).strip()
                            ]
                            if source_word_ids:
                                normalized["source_word_ids"] = source_word_ids
                            source_char_ranges = [
                                {
                                    "start_char_id": str(item.get("start_char_id") or "").strip(),
                                    "end_char_id": str(item.get("end_char_id") or "").strip(),
                                }
                                for item in list(row.get("source_char_ranges") or [])[:400]
                                if isinstance(item, dict)
                                and str(item.get("start_char_id") or "").strip()
                                and str(item.get("end_char_id") or "").strip()
                            ]
                            if source_char_ranges:
                                normalized["source_char_ranges"] = source_char_ranges
                            normalized_rows.append(normalized)
                    node["source_anchor_refs"] = normalized_rows
                else:
                    node["source_anchor_refs"] = []

                _walk(node.get("children"))

        _walk(cloned.get("components"))
        polygon_anchor_count = 0
        for node in self._flatten_components(list(cloned.get("components") or [])):
            if not isinstance(node, dict):
                continue
            for row in list(node.get("source_anchor_refs") or []):
                if not isinstance(row, dict):
                    continue
                geometry = row.get("geometry")
                polygons = list((geometry or {}).get("polygons") or []) if isinstance(geometry, dict) else []
                if polygons:
                    polygon_anchor_count += 1
        trace_meta = dict(cloned.get("trace_meta") or {})
        trace_meta["evidence_geometry_mode"] = "polygon" if polygon_anchor_count > 0 else "bbox_fallback"
        trace_meta["polygon_component_count"] = int(polygon_anchor_count)
        cloned["trace_meta"] = trace_meta
        return cloned

    def _normalize_anchor_ref(
        self,
        *,
        anchor: Any,
        page: int,
        quote_text: str = "",
        source_block_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(anchor, dict):
            return None
        # Composed payload is page-scoped. Keep anchor page aligned to current page
        # to avoid cross-page anchor contamination in scoring and jumps.
        page_no = self._safe_int(page, 1)
        start_char = self._safe_int(anchor.get("start_char"), 0)
        if start_char < 0:
            start_char = 0
        end_char = self._safe_int(anchor.get("end_char"), 0)
        normalized_quote = self._normalize_spaces(str(quote_text or anchor.get("quote") or anchor.get("quote_text") or ""))
        fallback_span = max(1, min(3200, len(normalized_quote) or 120))
        if end_char <= start_char:
            end_char = start_char + fallback_span
        if end_char - start_char > 12000:
            end_char = start_char + 12000
        canonical_raw = str(
            anchor.get("canonical_block_id")
            or ((anchor.get("anchor_v2") or {}).get("canonical_block_id")
            or source_block_id)
        ).strip()
        canonical_block_id = self._normalize_canonical_block_id(page=page_no, raw_id=canonical_raw)
        coord_version = str(anchor.get("coord_version") or ((anchor.get("anchor_v2") or {}).get("coord_version") or "anchor_v2"))
        if coord_version != "anchor_v2":
            coord_version = "anchor_v2"
        try:
            confidence = float(anchor.get("anchor_confidence") or 0.0)
        except Exception:
            confidence = 0.0
        if confidence <= 0:
            confidence = 0.86 if canonical_block_id else 0.72
        anchor_id = str(anchor.get("anchor_id") or f"{canonical_block_id or 'anchor'}:{start_char}:{end_char}")
        bbox_hint = anchor.get("bbox_hint") if isinstance(anchor.get("bbox_hint"), dict) else None
        geometry = anchor.get("geometry") if isinstance(anchor.get("geometry"), dict) else None
        source_word_ids = [
            str(item).strip()
            for item in list(anchor.get("source_word_ids") or [])[:600]
            if str(item).strip()
        ]
        source_char_ranges = [
            {
                "start_char_id": str(item.get("start_char_id") or "").strip(),
                "end_char_id": str(item.get("end_char_id") or "").strip(),
            }
            for item in list(anchor.get("source_char_ranges") or [])[:400]
            if isinstance(item, dict)
            and str(item.get("start_char_id") or "").strip()
            and str(item.get("end_char_id") or "").strip()
        ]
        return {
            "page": page_no,
            "start_char": start_char,
            "end_char": end_char,
            "quote": normalized_quote[:280] if normalized_quote else None,
            "quote_text": normalized_quote[:280] if normalized_quote else None,
            "canonical_block_id": canonical_block_id or None,
            "coord_version": coord_version,
            "anchor_confidence": max(0.0, min(1.0, confidence)),
            "anchor_id": anchor_id,
            "segment_index": anchor.get("segment_index"),
            "segment_total": anchor.get("segment_total"),
            "bbox_hint": bbox_hint,
            "geometry_version": str(anchor.get("geometry_version") or "poly_v1") if isinstance(geometry, dict) else None,
            "geometry": geometry,
            "source_word_ids": source_word_ids,
            "source_char_ranges": source_char_ranges,
            "anchor_v2": {
                "coord_version": "anchor_v2",
                "canonical_block_id": canonical_block_id or None,
                "page": page_no,
                "start_char": start_char,
                "end_char": end_char,
            },
        }

    @staticmethod
    def _layout_plan_v2_enabled_for_paper(paper_id: int) -> bool:
        _ = int(paper_id)  # keep signature stable for callers and logs
        return bool(getattr(settings, "reader_layout_plan_v2_enabled", False))

    @staticmethod
    def _collect_valid_block_ids(*, page: int, blocks: Sequence[Dict[str, Any]]) -> List[str]:
        output: List[str] = []
        seen: set[str] = set()
        for row in blocks:
            if not isinstance(row, dict):
                continue
            block_id = str(row.get("id") or "").strip()
            if not block_id:
                continue
            canonical = str(((row.get("source_anchor") or {}).get("canonical_block_id") or "")).strip()
            if not canonical:
                canonical = block_id if block_id.startswith(f"p{int(page)}_") else f"p{int(page)}_{block_id}"
            if canonical in seen:
                continue
            seen.add(canonical)
            output.append(canonical)
        return output

    def _ensure_layout_channels(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure all three layout channels exist for downstream rendering."""
        cloned = dict(payload)
        blocks = list(cloned.get("blocks") or [])
        side_context_blocks = list(cloned.get("side_context_blocks") or [])
        figure_meta_blocks = list(cloned.get("figure_meta_blocks") or [])

        if not side_context_blocks:
            side_context_blocks = [
                item for item in blocks if str((item or {}).get("zone_type") or "") == "side_context"
            ]
        if not figure_meta_blocks:
            figure_meta_blocks = [
                item for item in blocks if str((item or {}).get("zone_type") or "") == "figure_meta"
            ]

        cloned["side_context_blocks"] = side_context_blocks
        cloned["figure_meta_blocks"] = figure_meta_blocks
        cloned["layout_channels"] = dict(
            cloned.get("layout_channels")
            or {
                "main_body": [
                    str(item.get("id") or "")
                    for item in blocks
                    if str(item.get("zone_type") or "main_body") == "main_body"
                ],
                "side_context": [str(item.get("id") or "") for item in side_context_blocks if item.get("id")],
                "figure_meta": [str(item.get("id") or "") for item in figure_meta_blocks if item.get("id")],
            }
        )
        if "toc_quality" not in cloned:
            heading_blocks = [item for item in blocks if str(item.get("kind") or "") == "heading"]
            high_conf = [
                item for item in heading_blocks
                if float(item.get("heading_prob") or 0.0) >= 0.72
            ]
            cloned["toc_quality"] = round(len(high_conf) / max(1, len(heading_blocks)), 4) if heading_blocks else 0.0
        if "toc_hidden" not in cloned:
            cloned["toc_hidden"] = bool(float(cloned.get("toc_quality") or 0.0) < 0.55)
        return cloned

    def _dedupe_main_blocks(self, blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        seen_paragraph_keys: set[str] = set()
        for row in blocks:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            kind = str(item.get("kind") or "")
            if kind == "paragraph":
                text = self._normalize_spaces(str(item.get("text") or ""))
                text_key = re.sub(r"\s+", " ", text.lower()).strip()
                if text_key and text_key in seen_paragraph_keys:
                    continue
                if text_key:
                    seen_paragraph_keys.add(text_key)
            output.append(item)
        return output

    def _normalize_blocks_for_render(
        self,
        *,
        blocks: Sequence[Dict[str, Any]],
        page: int,
    ) -> List[Dict[str, Any]]:
        """Preprocess extracted blocks for rendering."""
        normalized: List[Dict[str, Any]] = []
        for raw in blocks:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip()
            if not kind:
                continue
            text = self._repair_text_artifacts(self._normalize_spaces(str(raw.get("text") or "")))
            if kind == "heading":
                text = self._repair_heading_text(text)
            if not text:
                continue
            row = dict(raw)
            row["kind"] = kind
            row["text"] = text
            source_anchor = self._normalize_anchor_ref(anchor=row.get("source_anchor"), page=page, quote_text=text)
            row["source_anchor"] = source_anchor or row.get("source_anchor")
            zone_type = str(row.get("zone_type") or ("figure_meta" if kind == "caption" else "main_body"))
            if zone_type not in {"main_body", "side_context", "figure_meta"}:
                zone_type = "main_body"
            row["zone_type"] = zone_type
            row["column_id"] = str(row.get("column_id") or "main")
            row["heading_prob"] = float(row.get("heading_prob") or (0.75 if kind == "heading" else 0.0))
            row["layout_confidence"] = float(row.get("layout_confidence") or 0.8)
            normalized.append(row)

        if not normalized:
            return []

        merged: List[Dict[str, Any]] = []
        idx = 0
        while idx < len(normalized):
            current = dict(normalized[idx])
            text = str(current.get("text") or "")
            kind = str(current.get("kind") or "")
            if kind == "heading":
                while idx + 1 < len(normalized):
                    nxt = normalized[idx + 1]
                    if str(nxt.get("kind") or "") != "heading":
                        break
                    nxt_text = str(nxt.get("text") or "")
                    if not self._should_merge_heading_lines(text, nxt_text):
                        break
                    text = self._normalize_spaces(f"{text} {nxt_text}")
                    current["text"] = text
                    current_anchor = dict(current.get("source_anchor") or {})
                    next_anchor = dict(nxt.get("source_anchor") or {})
                    if current_anchor or next_anchor:
                        anchor_page = self._safe_int(
                            current_anchor.get("page")
                            or next_anchor.get("page")
                            or page,
                            self._safe_int(page, 1),
                        )
                        start_values: List[int] = []
                        end_values: List[int] = []
                        for raw in (current_anchor.get("start_char"), next_anchor.get("start_char")):
                            try:
                                start_values.append(int(raw))
                            except Exception:
                                continue
                        for raw in (current_anchor.get("end_char"), next_anchor.get("end_char")):
                            try:
                                end_values.append(int(raw))
                            except Exception:
                                continue
                        anchor_start = min(start_values) if start_values else 0
                        anchor_end = max(end_values) if end_values else max(anchor_start + 1, len(text))
                        if anchor_end <= anchor_start:
                            anchor_end = anchor_start + max(1, len(text))

                        canonical_raw = str(
                            current_anchor.get("canonical_block_id")
                            or ((current_anchor.get("anchor_v2") or {}).get("canonical_block_id") or "")
                            or next_anchor.get("canonical_block_id")
                            or ((next_anchor.get("anchor_v2") or {}).get("canonical_block_id") or "")
                        )
                        canonical_block_id = self._normalize_canonical_block_id(page=anchor_page, raw_id=canonical_raw)
                        if not canonical_block_id:
                            fallback_id = str(current.get("id") or "")
                            canonical_block_id = (
                                self._normalize_canonical_block_id(page=anchor_page, raw_id=fallback_id)
                                or f"p{int(anchor_page)}_b1"
                            )

                        try:
                            confidence = max(
                                float(current_anchor.get("anchor_confidence") or 0.0),
                                float(next_anchor.get("anchor_confidence") or 0.0),
                            )
                        except Exception:
                            confidence = 0.0
                        confidence = max(0.86, min(1.0, confidence or 0.86))

                        merged_bbox: Optional[Dict[str, Any]] = None
                        left_bbox = current_anchor.get("bbox_hint")
                        right_bbox = next_anchor.get("bbox_hint")
                        if isinstance(left_bbox, dict) and isinstance(right_bbox, dict):
                            try:
                                lx0 = float(left_bbox.get("x0") or 0.0)
                                lx1 = float(left_bbox.get("x1") or 0.0)
                                ltop = float(left_bbox.get("top") or 0.0)
                                lbottom = float(left_bbox.get("bottom") or 0.0)
                                rx0 = float(right_bbox.get("x0") or 0.0)
                                rx1 = float(right_bbox.get("x1") or 0.0)
                                rtop = float(right_bbox.get("top") or 0.0)
                                rbottom = float(right_bbox.get("bottom") or 0.0)
                                merged_bbox = {
                                    "x0": min(lx0, rx0),
                                    "x1": max(lx1, rx1),
                                    "top": min(ltop, rtop),
                                    "bottom": max(lbottom, rbottom),
                                    "page_width": left_bbox.get("page_width") or right_bbox.get("page_width"),
                                    "page_height": left_bbox.get("page_height") or right_bbox.get("page_height"),
                                }
                            except Exception:
                                merged_bbox = None
                        elif isinstance(left_bbox, dict):
                            merged_bbox = dict(left_bbox)
                        elif isinstance(right_bbox, dict):
                            merged_bbox = dict(right_bbox)

                        merged_anchor: Dict[str, Any] = dict(current_anchor or next_anchor)
                        merged_anchor["page"] = anchor_page
                        merged_anchor["start_char"] = int(anchor_start)
                        merged_anchor["end_char"] = int(anchor_end)
                        merged_anchor["quote_text"] = text[:280]
                        merged_anchor["canonical_block_id"] = canonical_block_id
                        merged_anchor["coord_version"] = "anchor_v2"
                        merged_anchor["anchor_confidence"] = confidence
                        merged_anchor["anchor_id"] = f"{canonical_block_id}:{anchor_start}:{anchor_end}"
                        merged_anchor["anchor_v2"] = {
                            "coord_version": "anchor_v2",
                            "canonical_block_id": canonical_block_id,
                            "page": int(anchor_page),
                            "start_char": int(anchor_start),
                            "end_char": int(anchor_end),
                        }

                        merged_source_word_ids = [
                            str(item).strip()
                            for item in (
                                list(current_anchor.get("source_word_ids") or [])
                                + list(next_anchor.get("source_word_ids") or [])
                            )
                            if str(item).strip()
                        ]
                        merged_source_word_ids = list(dict.fromkeys(merged_source_word_ids))[:600]
                        if merged_source_word_ids:
                            merged_anchor["source_word_ids"] = merged_source_word_ids

                        merged_source_char_ranges = [
                            {
                                "start_char_id": str(item.get("start_char_id") or "").strip(),
                                "end_char_id": str(item.get("end_char_id") or "").strip(),
                            }
                            for item in (
                                list(current_anchor.get("source_char_ranges") or [])
                                + list(next_anchor.get("source_char_ranges") or [])
                            )
                            if isinstance(item, dict)
                            and str(item.get("start_char_id") or "").strip()
                            and str(item.get("end_char_id") or "").strip()
                        ]
                        if merged_source_char_ranges:
                            dedup_ranges: List[Dict[str, str]] = []
                            seen_ranges: set[str] = set()
                            for item in merged_source_char_ranges:
                                key = f"{item['start_char_id']}:{item['end_char_id']}"
                                if key in seen_ranges:
                                    continue
                                seen_ranges.add(key)
                                dedup_ranges.append(item)
                            merged_anchor["source_char_ranges"] = dedup_ranges[:400]

                        left_geometry = current_anchor.get("geometry")
                        right_geometry = next_anchor.get("geometry")
                        left_polygons = list((left_geometry or {}).get("polygons") or []) if isinstance(left_geometry, dict) else []
                        right_polygons = list((right_geometry or {}).get("polygons") or []) if isinstance(right_geometry, dict) else []
                        merged_polygons = [item for item in left_polygons + right_polygons if isinstance(item, dict)]
                        if merged_polygons:
                            merged_anchor["geometry_version"] = "poly_v1"
                            merged_anchor["geometry"] = {
                                "polygons": merged_polygons,
                                "page_width": (
                                    ((left_geometry or {}).get("page_width") if isinstance(left_geometry, dict) else None)
                                    or ((right_geometry or {}).get("page_width") if isinstance(right_geometry, dict) else None)
                                ),
                                "page_height": (
                                    ((left_geometry or {}).get("page_height") if isinstance(left_geometry, dict) else None)
                                    or ((right_geometry or {}).get("page_height") if isinstance(right_geometry, dict) else None)
                                ),
                            }
                        if isinstance(merged_bbox, dict):
                            merged_anchor["bbox_hint"] = merged_bbox
                        current["source_anchor"] = merged_anchor
                    idx += 1
            merged.append(current)
            idx += 1
        return merged

    @staticmethod
    def _should_merge_heading_lines(current: str, nxt: str) -> bool:
        left = str(current or "").strip()
        right = str(nxt or "").strip()
        if not left or not right:
            return False
        if len(left) > 220 or len(right) > 160:
            return False
        if re.search(r"[.!?;:。！？；：]$", left):
            return False
        if right.lower() in _GENERIC_HEADING_MARKERS:
            return False
        if right[:1].islower():
            return True
        if re.match(r"^(for|and|with|using|on|in|of|to|the)\b", right, flags=re.IGNORECASE):
            return True
        return False

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
        value = re.sub(r"\b([a-z]{2,})([A-Z]{2,})\b", r"\1 \2", value)
        value = re.sub(r"\b([A-Za-z]{4,})(of|for|to|in|and|with)([A-Z][A-Za-z]{2,})\b", r"\1 \2 \3", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _repair_heading_text(self, text: str) -> str:
        value = self._normalize_spaces(text)
        value = value.replace("RESEA RCH", "RESEARCH")
        value = value.replace("AUTH OR", "AUTHOR")
        value = value.replace("INTRO DUCTION", "INTRODUCTION")
        value = value.replace("DISCUS SION", "DISCUSSION")
        value = value.replace("CON CLUSION", "CONCLUSION")
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

    def _estimate_cross_column_merge_ratio(self, *, base_payload: Dict[str, Any]) -> float:
        """Estimate cross-column merge ratio for quality scoring."""
        style_cues = dict(base_payload.get("style_cues") or {})
        layout_mode = str(style_cues.get("layout_mode") or "")
        if layout_mode != "two_column":
            return 0.0
        blocks = list(base_payload.get("blocks") or [])
        paragraph_blocks = [
            item for item in blocks if str(item.get("kind") or "") == "paragraph"
        ]
        if not paragraph_blocks:
            return 0.0
        long_count = 0
        for item in paragraph_blocks:
            text = self._normalize_spaces(str(item.get("text") or ""))
            if len(text) >= 900:
                long_count += 1
        return max(0.0, min(1.0, long_count / max(1, len(paragraph_blocks))))

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
        title_words = [w for w in re.findall(r"[A-Za-z]{3,}", title.lower()) if w]
        if len(title_words) >= 5 and all(marker not in title.lower() for marker in _GENERIC_HEADING_MARKERS):
            return True
        blocks = list(base_payload.get("blocks") or [])
        expected = [
            self._normalize_spaces(str(item.get("text") or "")).lower()
            for item in blocks
            if str(item.get("kind") or "") == "heading"
        ]
        expected = [
            item for item in expected
            if item and item not in _GENERIC_HEADING_MARKERS
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
        if hits >= 2:
            return True

        heading_nodes = [
            self._normalize_spaces(str((item.get("props") or {}).get("text") or "")).lower()
            for item in nodes
            if str(item.get("type") or "") == "SectionHeading"
        ]
        heading_nodes = [
            item for item in heading_nodes
            if item and item not in _GENERIC_HEADING_MARKERS
        ]
        if not heading_nodes:
            return False
        for candidate in heading_nodes[:4]:
            words = [w for w in re.findall(r"[A-Za-z]{4,}", candidate) if w]
            if not words:
                continue
            hit_count = sum(1 for w in words[:8] if w.lower() in first_heading)
            if hit_count >= 3:
                return True
        return False

    @staticmethod
    def _normalize_latency_budget(raw: Optional[int]) -> int:
        try:
            value = int(raw) if raw is not None else DEFAULT_LATENCY_BUDGET_MS
        except Exception:
            value = DEFAULT_LATENCY_BUDGET_MS
        return max(1200, min(value, MAX_LATENCY_BUDGET_MS))

    @staticmethod
    def _normalize_max_iterations(raw: Optional[int]) -> int:
        try:
            value = int(raw) if raw is not None else DEFAULT_MAX_ITERATIONS
        except Exception:
            value = DEFAULT_MAX_ITERATIONS
        return max(1, min(value, 24))

    @staticmethod
    def _normalize_quality_target(raw: Optional[float]) -> float:
        try:
            value = float(raw) if raw is not None else DEFAULT_QUALITY_TARGET
        except Exception:
            value = DEFAULT_QUALITY_TARGET
        return max(0.6, min(value, 0.97))

    def _build_validation_report(
        self,
        *,
        quality_report: Dict[str, Any],
        minimal_gate_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        def _gate(passed: bool, code: str) -> Dict[str, Any]:
            return {"passed": bool(passed), "errors": [] if bool(passed) else [str(code)]}

        gates = {
            "id_integrity": _gate(bool(minimal_gate_report.get("schema_valid")), "id_integrity_failed"),
            "full_coverage": _gate(bool(minimal_gate_report.get("full_coverage")), "full_coverage_failed"),
            "whitelist_only": _gate(bool(minimal_gate_report.get("whitelist_valid")), "whitelist_only_failed"),
            "layout_contract": _gate(bool(minimal_gate_report.get("layout_contract", True)), "layout_contract_failed"),
            "ownership_unchanged": _gate(bool(minimal_gate_report.get("ownership_unchanged")), "ownership_unchanged_failed"),
            "non_empty_plan_for_non_empty_input": _gate(
                bool(minimal_gate_report.get("non_empty_plan_for_non_empty_input")),
                "non_empty_plan_for_non_empty_input_failed",
            ),
            "source_text_immutable": _gate(
                bool(minimal_gate_report.get("source_text_immutable", True)),
                "source_text_immutable_failed",
            ),
        }
        passed = all(bool((row or {}).get("passed")) for row in gates.values())
        errors = [
            str(item)
            for item in list(quality_report.get("validation_errors") or [])
            if str(item).strip()
        ]
        if not passed:
            errors.extend([name for name, row in gates.items() if not bool((row or {}).get("passed"))])
        return {"passed": bool(passed), "gates": gates, "errors": list(dict.fromkeys(errors))}

    def _enforce_no_drop_blocks_fallback(
        self,
        *,
        page: int,
        payload: Dict[str, Any],
        ui_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        components = [row for row in list((ui_plan or {}).get("components") or []) if isinstance(row, dict)]
        known_block_ids = self._collect_known_block_ids(page=page, base_payload=payload)
        if not known_block_ids:
            return {
                "triggered": False,
                "error_code": "",
                "strategy": "collapsed_paragraph_fallback_v1",
                "known_block_count": 0,
                "covered_block_count_before": 0,
                "covered_block_count_after": 0,
                "missing_block_ids": [],
                "inserted_node_ids": [],
            }

        known_set = set(known_block_ids)
        covered_before: List[str] = []
        for node in self._flatten_components(components):
            for raw in list(node.get("source_block_ids") or []):
                canonical = self._normalize_canonical_block_id(page=page, raw_id=str(raw))
                if canonical and canonical in known_set and canonical not in covered_before:
                    covered_before.append(canonical)

        missing_block_ids = [item for item in known_block_ids if item not in set(covered_before)]
        if not missing_block_ids:
            return {
                "triggered": False,
                "error_code": "",
                "strategy": "collapsed_paragraph_fallback_v1",
                "known_block_count": len(known_block_ids),
                "covered_block_count_before": len(covered_before),
                "covered_block_count_after": len(covered_before),
                "missing_block_ids": [],
                "inserted_node_ids": [],
            }

        existing_node_ids = {
            str((row or {}).get("id") or "").strip()
            for row in self._flatten_components(components)
            if isinstance(row, dict) and str((row or {}).get("id") or "").strip()
        }
        inserted_node_ids: List[str] = []
        for seq, block_id in enumerate(missing_block_ids, start=1):
            node = self._build_no_drop_fallback_node(
                page=page,
                payload=payload,
                canonical_block_id=str(block_id),
                seq=seq,
                existing_node_ids=existing_node_ids,
            )
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                continue
            components.append(node)
            inserted_node_ids.append(node_id)

        ui_plan["components"] = components
        covered_after = [
            item
            for item in known_block_ids
            if item in {
                self._normalize_canonical_block_id(page=page, raw_id=str(raw))
                for node in self._flatten_components(components)
                if isinstance(node, dict)
                for raw in list(node.get("source_block_ids") or [])
            }
        ]
        return {
            "triggered": True,
            "error_code": "no_drop_blocks_failed_auto_fallback",
            "strategy": "collapsed_paragraph_fallback_v1",
            "known_block_count": len(known_block_ids),
            "covered_block_count_before": len(covered_before),
            "covered_block_count_after": len(covered_after),
            "missing_block_ids": list(missing_block_ids),
            "inserted_node_ids": list(inserted_node_ids),
        }

    def _build_no_drop_fallback_node(
        self,
        *,
        page: int,
        payload: Dict[str, Any],
        canonical_block_id: str,
        seq: int,
        existing_node_ids: set[str],
    ) -> Dict[str, Any]:
        block_index: Dict[str, Dict[str, Any]] = {}
        for row in list((payload or {}).get("blocks") or []):
            if not isinstance(row, dict):
                continue
            canonical = self._normalize_canonical_block_id(
                page=page,
                raw_id=str(((row.get("source_anchor") or {}).get("canonical_block_id") or row.get("id") or "")),
            )
            if canonical and canonical not in block_index:
                block_index[canonical] = row

        group_index: Dict[str, Dict[str, Any]] = {}
        for row in list(((payload or {}).get("page_structure_v3") or {}).get("block_groups") or []):
            if not isinstance(row, dict):
                continue
            canonical = self._normalize_canonical_block_id(page=page, raw_id=str(row.get("block_id") or ""))
            if canonical and canonical not in group_index:
                group_index[canonical] = row

        block_row = dict(block_index.get(canonical_block_id) or {})
        group_row = dict(group_index.get(canonical_block_id) or {})
        source_text = self._normalize_spaces(
            str(
                block_row.get("text")
                or group_row.get("text")
                or group_row.get("title")
                or f"Recovered content for {canonical_block_id}"
            )
        ) or f"Recovered content for {canonical_block_id}"

        zone_type = str(block_row.get("zone_type") or group_row.get("zone_type") or "").strip().lower()
        if zone_type not in {"main_body", "side_context", "figure_meta"}:
            zone_type = "main_body"
        column_id = str(block_row.get("column_id") or group_row.get("column_id") or "").strip()
        if not column_id:
            column_id = "sidebar" if zone_type == "side_context" else "main"

        fallback_title = "Recovered block"
        if zone_type == "side_context":
            fallback_title = "Recovered side context"
        elif zone_type == "figure_meta":
            fallback_title = "Recovered figure metadata"

        anchor = self._normalize_anchor_ref(
            anchor=block_row.get("source_anchor"),
            page=page,
            quote_text=source_text,
            source_block_id=canonical_block_id,
        )
        anchor_rows = [anchor] if anchor else []

        base_id = re.sub(r"[^0-9a-zA-Z_]+", "_", str(canonical_block_id)).strip("_") or f"b{int(seq)}"
        node_id = f"no_drop_fb_{base_id}"
        suffix = 1
        while node_id in existing_node_ids:
            suffix += 1
            node_id = f"no_drop_fb_{base_id}_{suffix}"
        existing_node_ids.add(node_id)

        capabilities = ["copy", "drag_markdown", "inline_query"]
        if anchor_rows:
            capabilities.append("jump_anchor")

        return {
            "id": node_id,
            "type": "ParagraphProse",
            "props": {
                "title": fallback_title,
                "text": source_text,
                "fallback_reason": "no_drop_blocks_failed_auto_fallback",
                "fallback_source_block_id": canonical_block_id,
            },
            "children": [],
            "source_anchor_refs": anchor_rows,
            "source_block_ids": [canonical_block_id],
            "zone_type": zone_type,
            "column_id": column_id,
            "display": "collapsed",
            "capabilities": capabilities,
            "actions": [],
            "layout_slot": {"reserved_height": 150, "lock_height": False},
        }

    @staticmethod
    def _merge_no_drop_validation_report(
        *,
        validation_report: Dict[str, Any],
        fallback_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        cloned = dict(validation_report or {})
        gates = dict(cloned.get("gates") or {})
        full_coverage = dict(gates.get("full_coverage") or {})
        missing_block_ids = [
            str(item).strip()
            for item in list(fallback_report.get("missing_block_ids") or [])
            if str(item).strip()
        ]
        full_errors = [
            str(item).strip()
            for item in list(full_coverage.get("errors") or [])
            if str(item).strip()
        ]
        if missing_block_ids:
            full_errors.append(f"missing:{','.join(missing_block_ids[:80])}")
        injected_count = len(list(fallback_report.get("inserted_node_ids") or []))
        full_errors.append(f"auto_fallback_injected:{int(injected_count)}")
        full_errors.append("no_drop_blocks_failed_auto_fallback")
        full_coverage["passed"] = False
        full_coverage["errors"] = list(dict.fromkeys(full_errors))
        gates["full_coverage"] = full_coverage
        cloned["gates"] = gates
        errors = [str(item).strip() for item in list(cloned.get("errors") or []) if str(item).strip()]
        errors.extend(["full_coverage", "no_drop_blocks_failed_auto_fallback"])
        cloned["errors"] = list(dict.fromkeys(errors))
        cloned["passed"] = False
        return cloned

    @staticmethod
    def _merge_no_drop_quality_report(
        *,
        quality_report: Dict[str, Any],
        fallback_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        cloned = dict(quality_report or {})
        missing_block_ids = [
            str(item).strip()
            for item in list(fallback_report.get("missing_block_ids") or [])
            if str(item).strip()
        ]
        validation_errors = [
            str(item).strip()
            for item in list(cloned.get("validation_errors") or [])
            if str(item).strip()
        ]
        validation_errors.append("no_drop_blocks_failed_auto_fallback")
        if missing_block_ids:
            validation_errors.append(f"no_drop_blocks_missing:{','.join(missing_block_ids[:80])}")
        cloned["validation_errors"] = list(dict.fromkeys(validation_errors))
        cloned["degraded"] = True
        cloned["hard_constraints_passed"] = False
        cloned["full_coverage"] = False
        cloned["stop_reason"] = "no_drop_blocks_failed_auto_fallback"
        return cloned

    def _ensure_payload_contract(self, *, page: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        cloned = dict(payload or {})
        ui_plan = dict(cloned.get("ui_plan") or {})
        ui_plan["components"] = self._ensure_source_block_ids_on_nodes(
            page=page,
            nodes=[row for row in list(ui_plan.get("components") or []) if isinstance(row, dict)],
        )
        cloned["ui_plan"] = ui_plan

        quality_report = dict(cloned.get("quality_report") or {})
        validation_report = dict(cloned.get("validation_report") or {})
        if not isinstance(validation_report.get("gates"), dict):
            validation_report = self._build_validation_report(
                quality_report=quality_report,
                minimal_gate_report=dict(cloned.get("minimal_gate_report") or {}),
            )
        no_drop_report = self._enforce_no_drop_blocks_fallback(
            page=page,
            payload=cloned,
            ui_plan=ui_plan,
        )
        if bool(no_drop_report.get("triggered")):
            validation_report = self._merge_no_drop_validation_report(
                validation_report=validation_report,
                fallback_report=no_drop_report,
            )
            quality_report = self._merge_no_drop_quality_report(
                quality_report=quality_report,
                fallback_report=no_drop_report,
            )
            cloned["quality_report"] = quality_report

            minimal_gate_report = dict(cloned.get("minimal_gate_report") or {})
            if minimal_gate_report:
                minimal_gate_report["passed"] = False
                minimal_gate_report["full_coverage"] = False
                cloned["minimal_gate_report"] = minimal_gate_report

            repair_report = dict(cloned.get("repair_report") or {})
            failed_gates = [str(item) for item in list(repair_report.get("failed_gates") or []) if str(item)]
            if "full_coverage" not in failed_gates:
                failed_gates.append("full_coverage")
            repair_report["failed_gates"] = failed_gates
            repair_report["no_drop_blocks_fallback"] = {
                "applied": True,
                "strategy": str(no_drop_report.get("strategy") or ""),
                "error_code": str(no_drop_report.get("error_code") or ""),
                "missing_block_ids": list(no_drop_report.get("missing_block_ids") or []),
                "inserted_node_ids": list(no_drop_report.get("inserted_node_ids") or []),
            }
            cloned["repair_report"] = repair_report

            pipeline_contract_meta = dict(cloned.get("pipeline_contract_meta") or {})
            pipeline_contract_meta["no_drop_blocks_fallback"] = {
                **dict(no_drop_report or {}),
                "applied_at": datetime.utcnow().isoformat(),
            }
            cloned["pipeline_contract_meta"] = pipeline_contract_meta
        cloned["validation_report"] = validation_report

        main_block_ids, aux_block_ids = self._partition_main_aux_block_ids(
            page=page,
            base_payload=cloned,
            ui_plan=ui_plan,
        )
        cloned["main_block_ids"] = list(cloned.get("main_block_ids") or main_block_ids)
        cloned["aux_block_ids"] = list(cloned.get("aux_block_ids") or aux_block_ids)

        status_value = str(cloned.get("status") or "").strip().lower()
        if bool(no_drop_report.get("triggered")):
            status_value = "fallback"
        elif status_value not in {"done", "fallback"}:
            status_value = "done" if bool(validation_report.get("passed")) else "fallback"
        cloned["status"] = status_value
        degraded_reason = str(cloned.get("degraded_reason") or "").strip()
        if bool(no_drop_report.get("triggered")):
            degraded_reason = "no_drop_blocks_failed_auto_fallback"
        elif status_value == "done":
            degraded_reason = ""
        elif not degraded_reason:
            degraded_reason = str(quality_report.get("stop_reason") or "validator_non_converged").strip() or "validator_non_converged"
        cloned["degraded_reason"] = degraded_reason
        return cloned

    def _ensure_source_block_ids_on_nodes(
        self,
        *,
        page: int,
        nodes: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for raw in list(nodes or []):
            if not isinstance(raw, dict):
                continue
            node = dict(raw)
            existing = [
                self._normalize_canonical_block_id(page=page, raw_id=str(item))
                for item in list(node.get("source_block_ids") or [])
                if self._normalize_canonical_block_id(page=page, raw_id=str(item))
            ]
            if not existing:
                from_anchor = []
                for anchor in list(node.get("source_anchor_refs") or []):
                    if not isinstance(anchor, dict):
                        continue
                    canonical = self._normalize_canonical_block_id(
                        page=page,
                        raw_id=str(anchor.get("canonical_block_id") or ""),
                    )
                    if canonical and canonical not in from_anchor:
                        from_anchor.append(canonical)
                existing = from_anchor
            node["source_block_ids"] = existing
            node["children"] = self._ensure_source_block_ids_on_nodes(
                page=page,
                nodes=[row for row in list(node.get("children") or []) if isinstance(row, dict)],
            )
            output.append(node)
        return output

    def _collect_known_block_ids(self, *, page: int, base_payload: Dict[str, Any]) -> List[str]:
        ordered: List[str] = []
        seen: set[str] = set()
        page_structure_v3 = dict((base_payload or {}).get("page_structure_v3") or {})
        for row in list(page_structure_v3.get("block_groups") or []):
            if not isinstance(row, dict):
                continue
            canonical = self._normalize_canonical_block_id(page=page, raw_id=str(row.get("block_id") or ""))
            if canonical and canonical not in seen:
                seen.add(canonical)
                ordered.append(canonical)
        for row in list((base_payload or {}).get("blocks") or []):
            if not isinstance(row, dict):
                continue
            canonical = self._normalize_canonical_block_id(page=page, raw_id=str(row.get("id") or ""))
            if canonical and canonical not in seen:
                seen.add(canonical)
                ordered.append(canonical)
        return ordered

    def _partition_main_aux_block_ids(
        self,
        *,
        page: int,
        base_payload: Dict[str, Any],
        ui_plan: Dict[str, Any],
    ) -> Tuple[List[str], List[str]]:
        known = self._collect_known_block_ids(page=page, base_payload=base_payload)
        known_set = set(known)
        main_candidates: List[str] = []
        components = [row for row in list((ui_plan or {}).get("components") or []) if isinstance(row, dict)]
        for node in self._flatten_components(components):
            for raw in list(node.get("source_block_ids") or []):
                canonical = self._normalize_canonical_block_id(page=page, raw_id=str(raw))
                if canonical and canonical not in main_candidates:
                    main_candidates.append(canonical)
            for anchor in list(node.get("source_anchor_refs") or []):
                if not isinstance(anchor, dict):
                    continue
                canonical = self._normalize_canonical_block_id(
                    page=page,
                    raw_id=str(anchor.get("canonical_block_id") or ""),
                )
                if canonical and canonical not in main_candidates:
                    main_candidates.append(canonical)

        if known:
            main_block_ids = [item for item in main_candidates if item in known_set]
            aux_block_ids = [item for item in known if item not in set(main_block_ids)]
            return main_block_ids, aux_block_ids

        deduped_main: List[str] = []
        seen_main: set[str] = set()
        for item in main_candidates:
            if item in seen_main:
                continue
            seen_main.add(item)
            deduped_main.append(item)
        return deduped_main, []

    async def _build_force_refresh_timeout_fallback_payload(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        selected_kb_id: Optional[int],
        style_intent: Optional[str],
        theme_mode: Optional[str],
        detail_level: str,
        compare_mode: bool,
        source_signature: str,
        pipeline_version: str,
    ) -> Dict[str, Any]:
        base_payload, _ = await self._reader_service.build_or_get_page_payload(
            db=db,
            user_id=int(user_id),
            paper=paper,
            page=int(page),
            selected_kb_id=selected_kb_id,
            force_refresh=False,
            style_hint=None,
            prefer_agent=False,
            publish_ready_event_enabled=False,
        )
        ui_plan = self._build_initial_ui_plan(
            paper=paper,
            page=int(page),
            base_payload=base_payload,
            style_intent=style_intent,
            theme_mode=theme_mode,
            detail_level=detail_level,
            compare_mode=compare_mode,
        )
        main_block_ids, aux_block_ids = self._partition_main_aux_block_ids(
            page=int(page),
            base_payload=base_payload,
            ui_plan=ui_plan,
        )
        validation_report = {
            "passed": False,
            "gates": {
                "id_integrity": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
                "full_coverage": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
                "whitelist_only": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
                "ownership_unchanged": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
                "non_empty_plan_for_non_empty_input": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
                "source_text_immutable": {"passed": False, "errors": ["force_refresh_lock_timeout"]},
            },
            "errors": ["force_refresh_lock_timeout"],
        }
        return {
            "paper_id": int(paper.id),
            "page": int(page),
            "status": "fallback",
            "degraded_reason": "force_refresh_lock_timeout",
            "pipeline_version": str(pipeline_version or SIMPLIFIED_PIPELINE_VERSION_DEFAULT),
            "engine_version": COMPOSE_ENGINE_VERSION,
            "source_signature": str(source_signature),
            "build_mode": "compose_agent_simplified",
            "ui_plan": ui_plan,
            "assets": [],
            "quality_report": {
                "overall": 0.0,
                "hard_constraints_passed": False,
                "validation_errors": ["force_refresh_lock_timeout"],
                "iterations": 0,
                "degraded": True,
                "stop_reason": "force_refresh_lock_timeout",
                "quality_target": DEFAULT_QUALITY_TARGET,
                "latency_budget_ms": DEFAULT_LATENCY_BUDGET_MS,
            },
            "iteration_trace": [],
            "main_block_ids": main_block_ids,
            "aux_block_ids": aux_block_ids,
            "validation_report": validation_report,
            "repair_report": {
                "rounds": 0,
                "used": False,
                "reason": "force_refresh_lock_timeout",
                "step_metrics": [],
            },
            "asset_policy": {
                "pdf_first": True,
                "web_fallback": False,
                "max_external_images": 0,
                "version": COMPOSE_ASSET_POLICY_VERSION,
            },
            "layout_channels": dict((base_payload or {}).get("layout_channels") or {}),
            "mm_assist_meta": dict((base_payload or {}).get("mm_assist_meta") or {}),
            "parser_chain_meta": dict((base_payload or {}).get("parser_chain_meta") or {}),
            "docmind_meta": dict((base_payload or {}).get("docmind_meta") or {}),
            "docmind_structure": dict((base_payload or {}).get("docmind_structure") or {}),
            "page_structure_v3": dict((base_payload or {}).get("page_structure_v3") or {}),
            "generated_at": datetime.utcnow().isoformat(),
            "overlay_applied": False,
            "overlay_count": 0,
        }

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
    def _is_legacy_compose_cache_key(key: str) -> bool:
        token = str(key or "")
        if token.startswith(f"{REDIS_KEY_PREFIX}:v2:"):
            return False
        if token.startswith(f"{REDIS_LOCK_PREFIX}:v2:"):
            return False
        return token.startswith(REDIS_KEY_PREFIX) or token.startswith(REDIS_LOCK_PREFIX)

    async def _scan_legacy_compose_cache_keys(self, *, client: Any, scan_count: int) -> List[str]:
        cursor = 0
        output: List[str] = []
        batch_size = max(10, int(scan_count))
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=LEGACY_CACHE_SCAN_MATCH, count=batch_size)
            for raw in list(keys or []):
                key = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
                if self._is_legacy_compose_cache_key(key):
                    output.append(key)
            if cursor == 0:
                break
        return output

    async def cleanup_legacy_cache_keys(
        self,
        *,
        dry_run: bool = False,
        timeout_seconds: int = 120,
        scan_count: int = 200,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        report: Dict[str, Any] = {
            "scanned_keys": 0,
            "deleted_keys": 0,
            "error_count": 0,
            "duration_ms": 0,
            "remaining_old_keys": 0,
            "dry_run": bool(dry_run),
        }
        client = await self._get_redis_client()
        if client is None:
            report["error_count"] = 1
            report["remaining_old_keys"] = -1
            report["message"] = "redis_unavailable"
            report["duration_ms"] = int((time.perf_counter() - started) * 1000)
            return report

        lock_token = f"cleanup:{uuid.uuid4().hex}"
        timeout = max(30, int(timeout_seconds))
        acquired = False
        try:
            acquired = bool(await client.set(CLEANUP_LOCK_KEY, lock_token, nx=True, ex=timeout))
            if not acquired:
                report["error_count"] = 1
                report["remaining_old_keys"] = -1
                report["message"] = "cleanup_lock_not_acquired"
                return report

            old_keys = await asyncio.wait_for(
                self._scan_legacy_compose_cache_keys(client=client, scan_count=scan_count),
                timeout=max(5, timeout),
            )
            report["scanned_keys"] = int(len(old_keys))
            if not dry_run and old_keys:
                for key in old_keys:
                    try:
                        deleted = await client.delete(key)
                        if int(deleted or 0) > 0:
                            report["deleted_keys"] = int(report["deleted_keys"]) + 1
                    except Exception:
                        report["error_count"] = int(report["error_count"]) + 1

            remaining = await self._scan_legacy_compose_cache_keys(client=client, scan_count=scan_count)
            report["remaining_old_keys"] = int(len(remaining))
            return report
        except asyncio.TimeoutError:
            report["error_count"] = int(report["error_count"]) + 1
            report["remaining_old_keys"] = -1
            report["message"] = "cleanup_timeout"
            return report
        finally:
            report["duration_ms"] = int((time.perf_counter() - started) * 1000)
            try:
                if acquired:
                    current = await client.get(CLEANUP_LOCK_KEY)
                    if current == lock_token:
                        await client.delete(CLEANUP_LOCK_KEY)
            except Exception:
                pass

    @staticmethod
    def _signature_hash(signature: str) -> str:
        return hashlib.sha256((signature or "").encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _cache_key(
        *,
        user_id: int,
        paper_id: int,
        page: int,
        sig_hash: str,
        pipeline_mode: str,
        pipeline_version: str,
    ) -> str:
        mode = str(pipeline_mode or PIPELINE_MODE_LEGACY).strip().lower() or PIPELINE_MODE_LEGACY
        version = str(pipeline_version or SIMPLIFIED_PIPELINE_VERSION_DEFAULT).strip() or SIMPLIFIED_PIPELINE_VERSION_DEFAULT
        return (
            f"{REDIS_KEY_PREFIX}:v2:{mode}:{version}:"
            f"u{int(user_id)}:p{int(paper_id)}:pg{int(page)}:{sig_hash}"
        )

    @staticmethod
    def _lock_key(
        *,
        user_id: int,
        paper_id: int,
        page: int,
        pipeline_mode: str,
        pipeline_version: str,
    ) -> str:
        mode = str(pipeline_mode or PIPELINE_MODE_LEGACY).strip().lower() or PIPELINE_MODE_LEGACY
        version = str(pipeline_version or SIMPLIFIED_PIPELINE_VERSION_DEFAULT).strip() or SIMPLIFIED_PIPELINE_VERSION_DEFAULT
        return (
            f"{REDIS_LOCK_PREFIX}:v2:{mode}:{version}:"
            f"u{int(user_id)}:p{int(paper_id)}:pg{int(page)}"
        )

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


