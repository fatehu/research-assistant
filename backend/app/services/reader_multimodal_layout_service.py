"""
Reader multimodal layout assist service.

Only for layout decisions, never rewriting body content:
- heading identification
- main/side/figure channel split
- TOC candidate filtering
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings
from app.services.llm_service import (
    build_llm_source_headers,
    log_tagged_llm_request_done,
    log_tagged_llm_request_error,
    log_tagged_llm_request_start,
)
from app.services.render_pipeline_contract import (
    CanonicalAtomBundle,
    RenderPipelineContractError,
    validate_stage1_semantic_output,
    validate_stage2_design_output,
    validate_stage1_output,
    validate_stage2_output,
)


_GENERIC_HEADINGS = {
    "research article",
    "article",
    "open access",
    "author summary",
    "plos digital health",
}

_SIDEBAR_PATTERNS = (
    "open access",
    "citation:",
    "received:",
    "accepted:",
    "published:",
    "editor:",
    "copyright",
)


class ReaderMultimodalLayoutService:
    """Low-frequency multimodal layout assist service."""

    def __init__(self) -> None:
        self._doc_stats: Dict[int, Dict[str, Any]] = {}

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

    def should_trigger_mm(
        self,
        *,
        paper_id: int,
        page: int,
        base_payload: Dict[str, Any],
        call_count: int = 0,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Decide whether to trigger multimodal layout assist for this page."""
        enabled = bool(getattr(settings, "reader_mm_assist_enabled", False))
        if not enabled:
            return False, {"reason": "mm_disabled"}
        if call_count >= max(1, int(getattr(settings, "reader_mm_max_calls_per_page", 1) or 1)):
            return False, {"reason": "page_call_budget_exceeded"}

        blocks = list(base_payload.get("blocks") or [])
        style_cues = dict(base_payload.get("style_cues") or {})
        structure_confidence = float(base_payload.get("structure_confidence") or 0.0)
        title_integrity = self._estimate_title_integrity(blocks)
        sidebar_leak = self._estimate_sidebar_leak(blocks)
        cross_column_merge_ratio = self._estimate_cross_column_merge_ratio(
            blocks=blocks,
            style_cues=style_cues,
        )

        threshold = float(getattr(settings, "reader_mm_trigger_confidence", 0.62) or 0.62)
        trigger_reasons: List[str] = []
        if structure_confidence < threshold:
            trigger_reasons.append("low_structure_confidence")
        if not title_integrity:
            trigger_reasons.append("title_integrity_false")
        if sidebar_leak:
            trigger_reasons.append("sidebar_leak_true")
        if cross_column_merge_ratio > 0.08:
            trigger_reasons.append("cross_column_merge_high")

        # 即使调用方当前把 enabled 当作所有页面的 opt-in，也保留逐文档遥测；
        # 返回的 trigger_reasons 仍说明页面是否真的有风险特征。
        state = self._doc_stats.setdefault(
            int(paper_id),
            {
                "seen_pages": set(),
                "triggered_pages": set(),
                "updated_at": time.time(),
            },
        )
        seen_pages = state.get("seen_pages")
        if not isinstance(seen_pages, set):
            seen_pages = set()
            state["seen_pages"] = seen_pages
        triggered_pages = state.get("triggered_pages")
        if not isinstance(triggered_pages, set):
            triggered_pages = set()
            state["triggered_pages"] = triggered_pages

        seen_pages.add(int(page))
        state["updated_at"] = time.time()

        return True, {
            "reason": "triggered_per_page_default",
            "structure_confidence": structure_confidence,
            "title_integrity": title_integrity,
            "sidebar_leak": sidebar_leak,
            "cross_column_merge_ratio": round(cross_column_merge_ratio, 4),
            "trigger_reasons": trigger_reasons,
        }

    async def build_mm_prompt_payload(
        self,
        *,
        pdf_path: str,
        page: int,
        base_payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Build multimodal prompt payload.

        Parser pass contract (step-1):
        - model input must be page image + source plain text only (plus strict schema instructions)
        - word/char coordinates stay local for post-parse validation and anchoring
        """
        native_page_extract = self._extract_native_pdf_page_data(
            pdf_path=pdf_path,
            page=int(page),
        )
        style_cues = dict(base_payload.get("style_cues") or {})
        image_rows: List[Dict[str, Any]] = []
        image_data_url = await self._render_page_image_data_url(pdf_path=pdf_path, page=int(page))
        if image_data_url:
            image_rows.append(
                {
                    "scope": "current",
                    "page": int(page),
                    "image_data_url": image_data_url,
                }
            )
        if not image_rows:
            return None
        current_image = next(
            (str(item.get("image_data_url") or "") for item in image_rows if str(item.get("scope") or "") == "current"),
            str((image_rows[0] or {}).get("image_data_url") or ""),
        )
        compact_native_extract = self._compact_native_extract_for_mm_prompt(native_page_extract)
        compact_words = list(compact_native_extract.get("words") or [])
        compact_char_ids: List[str] = []
        for row in compact_words:
            if not isinstance(row, dict):
                continue
            start_id = str(row.get("start_char_id") or "").strip()
            end_id = str(row.get("end_char_id") or "").strip()
            if start_id:
                compact_char_ids.append(start_id)
            if end_id:
                compact_char_ids.append(end_id)
        if not compact_char_ids:
            compact_char_ids = [
                str(item.get("char_id") or "").strip()
                for item in list(native_page_extract.get("chars") or [])[:2200]
                if str(item.get("char_id") or "").strip()
            ]
        compact_char_ids = list(dict.fromkeys(compact_char_ids))[:2400]
        compact_word_ids = [
            str(item.get("word_id") or "").strip()
            for item in compact_words[:2200]
            if str(item.get("word_id") or "").strip()
        ]
        source_text_bundle = self._build_source_text_bundle(native_page_extract=native_page_extract)
        source_text_full = str(source_text_bundle.get("source_text_full") or "")
        source_word_spans = [
            item
            for item in list(source_text_bundle.get("source_word_spans") or [])[:5000]
            if isinstance(item, dict)
        ]
        source_checksum = str(source_text_bundle.get("source_checksum") or "").strip()
        image_placeholders = [
            item
            for item in list(source_text_bundle.get("image_placeholders") or [])[:80]
            if isinstance(item, dict)
        ]
        layout_summary = {
            "word_count": len(list(native_page_extract.get("words") or [])),
            "char_count": len(list(native_page_extract.get("chars") or [])),
            "image_count": len(list(native_page_extract.get("images") or [])),
            "text_chars": len(source_text_full),
            "placeholder_count": len(image_placeholders),
        }

        return {
            "image_data_url": current_image,
            "images": image_rows[:1],
            "line_candidates": [],
            "native_page_extract": native_page_extract,
            "native_page_extract_compact": compact_native_extract,
            "layout_summary": layout_summary,
            "block_candidates": [],
            "layout_meta": {
                "page": int(page),
                "layout_mode": str(style_cues.get("layout_mode") or "unknown"),
                "page_width": float(style_cues.get("page_width") or 0.0),
                "page_height": float(style_cues.get("page_height") or 0.0),
                "prompt_version": str(getattr(settings, "reader_mm_prompt_version", "mm_layout_v1")),
                "prompt_contract": "image_plus_plain_text_only",
            },
            "valid_line_ids": [],
            "valid_word_ids": compact_word_ids,
            "valid_char_ids": compact_char_ids,
            "source_text_full": source_text_full,
            "source_word_spans": source_word_spans,
            "source_checksum": source_checksum,
            "image_placeholders": image_placeholders,
        }

    async def call_primary_then_fallback(
        self,
        *,
        prompt_payload: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Call primary model first, then fallback once if needed."""
        return await self._call_primary_then_fallback_with_validator(
            prompt_payload=prompt_payload,
            prompt_kind="layout_judge_v1",
            validator=self.validate_mm_layout_json,
        )

    @staticmethod
    def _compact_native_extract_for_mm_prompt(native_page_extract: Dict[str, Any]) -> Dict[str, Any]:
        """Shrink native extractor payload for VL parsing prompt while keeping coordinate fidelity."""
        source = dict(native_page_extract or {})
        page_meta = dict(source.get("page_meta") or {})
        words_raw = [row for row in list(source.get("words") or []) if isinstance(row, dict)]
        images_raw = [row for row in list(source.get("images") or []) if isinstance(row, dict)]
        rects_raw = [row for row in list(source.get("rects") or []) if isinstance(row, dict)]

        words: List[Dict[str, Any]] = []
        for row in words_raw[:1100]:
            words.append(
                {
                    "word_id": str(row.get("word_id") or ""),
                    "text": str(row.get("text") or "")[:80],
                    "x0": round(float(row.get("x0") or 0.0), 2),
                    "x1": round(float(row.get("x1") or 0.0), 2),
                    "top": round(float(row.get("top") or 0.0), 2),
                    "bottom": round(float(row.get("bottom") or 0.0), 2),
                    "font_size": round(float(row.get("font_size") or 0.0), 2),
                    "start_char_id": str(row.get("start_char_id") or ""),
                    "end_char_id": str(row.get("end_char_id") or ""),
                }
            )

        images: List[Dict[str, Any]] = []
        for row in images_raw[:60]:
            images.append(
                {
                    "id": str(row.get("id") or ""),
                    "x0": round(float(row.get("x0") or 0.0), 2),
                    "x1": round(float(row.get("x1") or 0.0), 2),
                    "top": round(float(row.get("top") or 0.0), 2),
                    "bottom": round(float(row.get("bottom") or 0.0), 2),
                    "width": round(float(row.get("width") or 0.0), 2),
                    "height": round(float(row.get("height") or 0.0), 2),
                }
            )

        rects: List[Dict[str, Any]] = []
        for row in rects_raw[:120]:
            rects.append(
                {
                    "id": str(row.get("id") or ""),
                    "x0": round(float(row.get("x0") or 0.0), 2),
                    "x1": round(float(row.get("x1") or 0.0), 2),
                    "top": round(float(row.get("top") or 0.0), 2),
                    "bottom": round(float(row.get("bottom") or 0.0), 2),
                    "width": round(float(row.get("width") or 0.0), 2),
                    "height": round(float(row.get("height") or 0.0), 2),
                }
            )

        return {
            "page_meta": {
                "page": int(page_meta.get("page") or 0),
                "page_width": round(float(page_meta.get("page_width") or 0.0), 2),
                "page_height": round(float(page_meta.get("page_height") or 0.0), 2),
                "rotation": int(page_meta.get("rotation") or 0),
            },
            "words": words,
            "images": images,
            "rects": rects,
            "extract_text_raw": str(source.get("extract_text_raw") or "")[:2200],
        }

    def _build_source_text_bundle(self, *, native_page_extract: Dict[str, Any]) -> Dict[str, Any]:
        """Build strict source text + word span mapping for span-only parser contract."""
        source = dict(native_page_extract or {})
        words_raw = [row for row in list(source.get("words") or []) if isinstance(row, dict)]
        images_raw = [row for row in list(source.get("images") or []) if isinstance(row, dict)]
        if not words_raw:
            text = self._normalize_spaces(str(source.get("extract_text_raw") or ""))
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            return {
                "source_text_full": text,
                "source_word_spans": [],
                "source_checksum": checksum,
                "image_placeholders": [],
            }

        words = sorted(
            words_raw,
            key=lambda row: (
                float(row.get("doctop") or row.get("top") or 0.0),
                float(row.get("x0") or 0.0),
                str(row.get("word_id") or ""),
            ),
        )
        words = self._dedupe_near_duplicate_words(words)

        text_parts: List[str] = []
        spans: List[Dict[str, Any]] = []
        cursor = 0
        prev_top: Optional[float] = None
        prev_bottom: Optional[float] = None
        prev_x1: Optional[float] = None
        prev_height: float = 0.0
        for row in words:
            text = self._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            top = float(row.get("top") or row.get("doctop") or 0.0)
            bottom = float(row.get("bottom") or top)
            x0 = float(row.get("x0") or 0.0)
            x1 = float(row.get("x1") or x0)
            height = max(0.0, bottom - top)
            sep = ""
            if prev_top is not None:
                row_gap = max(0.0, top - float(prev_bottom or prev_top))
                same_row = row_gap <= max(2.8, prev_height * 0.45)
                if same_row:
                    space_gap = max(0.0, x0 - float(prev_x1 or x0))
                    sep = " " if space_gap >= max(1.6, height * 0.08) else ""
                else:
                    sep = "\n"
            if sep:
                text_parts.append(sep)
                cursor += len(sep)
            start = cursor
            text_parts.append(text)
            cursor += len(text)
            end = cursor
            spans.append(
                {
                    "word_id": str(row.get("word_id") or "").strip(),
                    "start": int(start),
                    "end": int(end),
                    "start_char_id": str(row.get("start_char_id") or "").strip(),
                    "end_char_id": str(row.get("end_char_id") or "").strip(),
                }
            )
            prev_top = top
            prev_bottom = bottom
            prev_x1 = x1
            prev_height = max(1.0, height)

        source_text_full = "".join(text_parts)
        source_checksum = hashlib.sha256(source_text_full.encode("utf-8")).hexdigest() if source_text_full else ""

        image_placeholders: List[Dict[str, Any]] = []
        for idx, row in enumerate(images_raw[:80], start=1):
            token = f"[FIG_{idx}]"
            image_placeholders.append(
                {
                    "token": token,
                    "image_id": str(row.get("id") or f"img{idx:04d}"),
                    "bbox": {
                        "x0": round(float(row.get("x0") or 0.0), 2),
                        "x1": round(float(row.get("x1") or 0.0), 2),
                        "top": round(float(row.get("top") or 0.0), 2),
                        "bottom": round(float(row.get("bottom") or 0.0), 2),
                    },
                    "description": "image region placeholder",
                }
            )

        return {
            "source_text_full": source_text_full,
            "source_word_spans": spans,
            "source_checksum": source_checksum,
            "image_placeholders": image_placeholders,
        }

    @staticmethod
    def _dedupe_near_duplicate_words(words: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop near-identical duplicate words produced by overlapping PDF text layers."""
        if not words:
            return []
        output: List[Dict[str, Any]] = []
        seen: set[Tuple[str, float, float, float, float]] = set()
        for row in words:
            if not isinstance(row, dict):
                continue
            text = ReaderMultimodalLayoutService._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            x0 = float(row.get("x0") or 0.0)
            x1 = float(row.get("x1") or x0)
            top = float(row.get("top") or row.get("doctop") or 0.0)
            bottom = float(row.get("bottom") or top)
            # 0.5pt quantization keeps real words while collapsing overlay duplicates.
            key = (
                text.lower(),
                round(x0 * 2.0) / 2.0,
                round(top * 2.0) / 2.0,
                round(x1 * 2.0) / 2.0,
                round(bottom * 2.0) / 2.0,
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(row)
        return output

    async def build_layout_plan_v2(
        self,
        *,
        prompt_payload: Dict[str, Any],
        valid_block_ids: Sequence[str],
        valid_line_ids: Sequence[str],
        component_whitelist: Sequence[str],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Build multimodal layout plan v2 (plan+segments only)."""
        payload = dict(prompt_payload)
        payload["valid_block_ids"] = [str(item).strip() for item in list(valid_block_ids or []) if str(item).strip()]
        payload["valid_line_ids"] = [str(item).strip() for item in list(valid_line_ids or []) if str(item).strip()]
        payload["component_whitelist"] = [
            str(item).strip() for item in list(component_whitelist or []) if str(item).strip()
        ]
        return await self._call_primary_then_fallback_with_validator(
            prompt_payload=payload,
            prompt_kind="layout_plan_v2",
            validator=lambda data: self.validate_layout_plan_v2_json(
                payload=data,
                valid_block_ids=set(payload.get("valid_block_ids") or []),
                valid_line_ids=set(payload.get("valid_line_ids") or []),
                component_whitelist=set(payload.get("component_whitelist") or []),
            ),
            primary_model=str(getattr(settings, "reader_mm_layout_model", "qwen3.5-flash") or "qwen3.5-flash"),
            fallback_model=str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-flash") or "qwen3-vl-flash"),
        )

    async def build_stage1_structural_annotations(
        self,
        *,
        prompt_payload: Dict[str, Any],
        known_layout_ids: Sequence[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = dict(prompt_payload)
        known_ids = [str(item).strip() for item in list(known_layout_ids or []) if str(item).strip()]
        real_to_alias: Dict[str, str] = {}
        alias_to_real: Dict[str, str] = {}
        for idx, layout_id in enumerate(known_ids, start=1):
            alias = f"L{idx:04d}"
            real_to_alias[layout_id] = alias
            alias_to_real[alias] = layout_id
        payload["known_layout_ids"] = [real_to_alias.get(item, item) for item in known_ids]
        digest_rows = [row for row in list(payload.get("docmind_layout_digest") or []) if isinstance(row, dict)]
        if digest_rows and real_to_alias:
            # 过长 layout ID 会让多模态 JSON 变脆弱；先在 prompt 中使用别名，
            # 校验后再映射回规范 DocMind ID，保证下游契约稳定。
            aliased_digest_rows: List[Dict[str, Any]] = []
            for row in digest_rows:
                cloned = dict(row)
                original_layout_id = str(cloned.get("layout_id") or "").strip()
                if original_layout_id and original_layout_id in real_to_alias:
                    cloned["layout_id"] = real_to_alias[original_layout_id]
                aliased_digest_rows.append(cloned)
            payload["docmind_layout_digest"] = aliased_digest_rows
        parser_model = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        parser_fallback_model = str(getattr(settings, "reader_mm_parser_fallback_model", "") or "").strip()
        layout_model = str(getattr(settings, "reader_mm_layout_model", "qwen3.5-plus") or "qwen3.5-plus").strip()
        mm_fallback_model = str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-plus") or "qwen3-vl-plus").strip()
        # Stage1 is strict JSON annotation; prefer stable text-JSON model over OCR-style fallback.
        if parser_fallback_model and "ocr" not in parser_fallback_model.lower():
            fallback_model = parser_fallback_model
        else:
            fallback_model = layout_model or mm_fallback_model or "qwen3.5-plus"
        timeout_ms = int(getattr(settings, "reader_mm_parser_timeout_ms", 120000) or 120000)
        attempt_count = 0
        last_error: Optional[RenderPipelineContractError] = None

        async def _attempt(model: str, retry_hint: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[RenderPipelineContractError]]:
            nonlocal attempt_count
            attempt_count += 1
            current_payload = dict(payload)
            if retry_hint:
                current_payload["retry_hint"] = retry_hint
            advice = await self._call_mm_model(
                model=model,
                prompt_payload=current_payload,
                timeout_ms=timeout_ms,
                prompt_kind="stage1_structural_v1",
            )
            if not isinstance(advice, dict):
                return None, RenderPipelineContractError(
                    code="STAGE1_INVALID_JSON",
                    stage="stage1",
                    message="Stage1 model response is not a JSON object",
                    details={"model": model},
                )
            advice = self._remap_stage1_layout_ids_from_aliases(
                payload=advice,
                alias_to_real=alias_to_real,
            )
            try:
                validated = validate_stage1_output(advice, known_ids)
            except RenderPipelineContractError as exc:
                block_preview: Dict[str, Any] = {}
                if isinstance(advice.get("blocks"), list) and advice.get("blocks"):
                    first_block = advice["blocks"][0]
                    if isinstance(first_block, dict):
                        block_preview = {
                            "keys": sorted(list(first_block.keys()))[:30],
                            "layout_id": str(first_block.get("layout_id") or first_block.get("layoutId") or ""),
                            "role": str(first_block.get("role") or first_block.get("block_role") or ""),
                        }
                logger.warning(
                    f"[ReaderMM][stage1] validation failed model={model} "
                    f"code={getattr(exc, 'code', '')} "
                    f"details={json.dumps(dict(getattr(exc, 'details', {}) or {}), ensure_ascii=False)[:1200]} "
                    f"block_preview={json.dumps(block_preview, ensure_ascii=False)[:600]}"
                )
                return None, exc
            return validated, None

        validated, err = await _attempt(parser_model, "")
        if isinstance(validated, dict):
            return validated, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": False,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err
        retry_hint = ""
        if isinstance(last_error, RenderPipelineContractError):
            # 校验反馈刻意保持简短：足够修复 enum 和必填字段错误，
            # 但不鼓励模型偏离 schema。
            retry_hint = f"Previous output failed validation: code={last_error.code}, stage={last_error.stage}. Return strict JSON only."
            if str(last_error.code) == "STAGE1_REQUIRED_FIELD_MISSING":
                retry_hint += (
                    " Roles must be exact enum tokens only: "
                    "doc_title,section_title,paragraph,list_item,caption,figure,table,"
                    "sidebar,metadata,header,footer,noise,unknown. "
                    "If uncertain, always use unknown."
                )
        validated_retry, err_retry = await _attempt(parser_model, retry_hint)
        if isinstance(validated_retry, dict):
            return validated_retry, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err_retry

        if fallback_model and fallback_model != parser_model:
            validated_fb, err_fb = await _attempt(fallback_model, retry_hint)
            if isinstance(validated_fb, dict):
                return validated_fb, {
                    "used": True,
                    "model": fallback_model,
                    "fallback_used": True,
                    "retry_used": True,
                    "retry_count": max(0, attempt_count - 1),
                    "error": None,
                }
            last_error = err_fb

        if isinstance(last_error, RenderPipelineContractError):
            raise last_error
        raise RenderPipelineContractError(
            code="STAGE1_INVALID_JSON",
            stage="stage1",
            message="Stage1 failed with unknown error",
        )

    @staticmethod
    def _remap_stage1_layout_ids_from_aliases(
        *,
        payload: Dict[str, Any],
        alias_to_real: Mapping[str, str],
    ) -> Dict[str, Any]:
        alias_map = {str(key).strip(): str(value).strip() for key, value in dict(alias_to_real or {}).items() if str(key).strip() and str(value).strip()}
        if not alias_map:
            return payload

        def _map_id(value: Any) -> str:
            token = str(value or "").strip()
            if not token:
                return ""
            return alias_map.get(token, token)

        cloned = dict(payload or {})
        blocks = []
        for row in list(cloned.get("blocks") or []):
            if not isinstance(row, dict):
                blocks.append(row)
                continue
            mapped = dict(row)
            mapped["layout_id"] = _map_id(row.get("layout_id") or row.get("layoutId") or row.get("id"))
            blocks.append(mapped)
        cloned["blocks"] = blocks

        sections = []
        for row in list(cloned.get("sections") or []):
            if not isinstance(row, dict):
                sections.append(row)
                continue
            mapped = dict(row)
            mapped["title_layout_id"] = _map_id(
                row.get("title_layout_id") or row.get("titleLayoutId") or row.get("title_id")
            )
            children = []
            for item in list(
                row.get("children")
                or row.get("child_layout_ids")
                or row.get("content_layout_ids")
                or []
            ):
                mapped_id = _map_id(item)
                if mapped_id:
                    children.append(mapped_id)
            mapped["children"] = children
            sections.append(mapped)
        cloned["sections"] = sections
        return cloned

    async def build_stage2_design_layout(
        self,
        *,
        prompt_payload: Dict[str, Any],
        known_layout_ids: Sequence[str],
        allowed_components: Sequence[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = dict(prompt_payload)
        known_ids = [str(item).strip() for item in list(known_layout_ids or []) if str(item).strip()]
        allowed = [str(item).strip() for item in list(allowed_components or []) if str(item).strip()]
        payload["known_layout_ids"] = list(known_ids)
        payload["allowed_components"] = list(allowed)
        primary_model = str(getattr(settings, "reader_mm_layout_model", "qwen3.5-plus") or "qwen3.5-plus")
        fallback_model = str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-plus") or "qwen3-vl-plus")
        timeout_ms = int(getattr(settings, "reader_mm_timeout_ms", 90000) or 90000)
        attempt_count = 0
        last_error: Optional[RenderPipelineContractError] = None

        async def _attempt(model: str, retry_hint: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[RenderPipelineContractError]]:
            nonlocal attempt_count
            attempt_count += 1
            current_payload = dict(payload)
            if retry_hint:
                current_payload["retry_hint"] = retry_hint
            advice = await self._call_mm_model(
                model=model,
                prompt_payload=current_payload,
                timeout_ms=timeout_ms,
                prompt_kind="stage2_design_v1",
            )
            if not isinstance(advice, dict):
                return None, RenderPipelineContractError(
                    code="STAGE2_INVALID_JSON",
                    stage="stage2",
                    message="Stage2 model response is not a JSON object",
                    details={"model": model},
                )
            try:
                validated = validate_stage2_output(advice, known_ids, allowed)
            except RenderPipelineContractError as exc:
                return None, exc
            return validated, None

        validated, err = await _attempt(primary_model, "")
        if isinstance(validated, dict):
            return validated, {
                "used": True,
                "model": primary_model,
                "fallback_used": False,
                "retry_used": False,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err
        retry_hint = ""
        if isinstance(last_error, RenderPipelineContractError):
            # 第二阶段只负责设计；重试时要求模型修复 JSON，同时保留已知的
            # 布局/组件允许列表。
            retry_hint = f"Previous output failed validation: code={last_error.code}, stage={last_error.stage}. Return strict JSON only."
        validated_retry, err_retry = await _attempt(primary_model, retry_hint)
        if isinstance(validated_retry, dict):
            return validated_retry, {
                "used": True,
                "model": primary_model,
                "fallback_used": False,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err_retry

        if fallback_model and fallback_model != primary_model:
            validated_fb, err_fb = await _attempt(fallback_model, retry_hint)
            if isinstance(validated_fb, dict):
                return validated_fb, {
                    "used": True,
                    "model": fallback_model,
                    "fallback_used": True,
                    "retry_used": True,
                    "retry_count": max(0, attempt_count - 1),
                    "error": None,
                }
            last_error = err_fb

        if isinstance(last_error, RenderPipelineContractError):
            raise last_error
        raise RenderPipelineContractError(
            code="STAGE2_INVALID_JSON",
            stage="stage2",
            message="Stage2 failed with unknown error",
        )

    async def build_stage1_semantic_annotations(
        self,
        *,
        prompt_payload: Dict[str, Any],
        atom_bundle: CanonicalAtomBundle,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Stage1 semantic-only annotations over deterministic atom IDs."""
        payload = dict(prompt_payload)
        payload["known_atom_ids"] = list(atom_bundle.usable_atom_ids or [])
        parser_model = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        fallback_model = str(getattr(settings, "reader_mm_layout_model", "qwen3.5-plus") or "qwen3.5-plus")
        timeout_ms = int(getattr(settings, "reader_mm_parser_timeout_ms", 120000) or 120000)
        attempt_count = 0
        last_error: Optional[RenderPipelineContractError] = None

        async def _attempt(model: str, retry_hint: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[RenderPipelineContractError]]:
            nonlocal attempt_count
            attempt_count += 1
            current_payload = dict(payload)
            if retry_hint:
                current_payload["retry_hint"] = retry_hint
            advice = await self._call_mm_model(
                model=model,
                prompt_payload=current_payload,
                timeout_ms=timeout_ms,
                prompt_kind="stage1_semantic_v2",
            )
            if not isinstance(advice, dict):
                return None, RenderPipelineContractError(
                    code="STAGE1_INVALID_JSON",
                    stage="stage1",
                    message="Stage1 semantic response is not a JSON object",
                    details={"model": model},
                )
            try:
                validated = validate_stage1_semantic_output(
                    advice,
                    known_atom_ids=list(atom_bundle.usable_atom_ids or []),
                )
                return validated, None
            except RenderPipelineContractError as exc:
                return None, exc

        validated, err = await _attempt(parser_model, "")
        if isinstance(validated, dict):
            return validated, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": False,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err
        retry_hint = ""
        if isinstance(last_error, RenderPipelineContractError):
            retry_hint = (
                f"Previous output failed validation: code={last_error.code}, stage={last_error.stage}. "
                "Return strict JSON with one annotation per atom_id."
            )
        validated_retry, err_retry = await _attempt(parser_model, retry_hint)
        if isinstance(validated_retry, dict):
            return validated_retry, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err_retry

        if fallback_model and fallback_model != parser_model:
            validated_fb, err_fb = await _attempt(fallback_model, retry_hint)
            if isinstance(validated_fb, dict):
                return validated_fb, {
                    "used": True,
                    "model": fallback_model,
                    "fallback_used": True,
                    "retry_used": True,
                    "retry_count": max(0, attempt_count - 1),
                    "error": None,
                }
            last_error = err_fb

        if isinstance(last_error, RenderPipelineContractError):
            return None, {
                "used": False,
                "model": parser_model,
                "fallback_used": bool(fallback_model and fallback_model != parser_model),
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": last_error.to_dict(),
            }
        return None, {
            "used": False,
            "model": parser_model,
            "fallback_used": bool(fallback_model and fallback_model != parser_model),
            "retry_used": True,
            "retry_count": max(0, attempt_count - 1),
            "error": {"code": "STAGE1_INVALID_JSON", "message": "unknown stage1 semantic error"},
        }

    async def build_stage2_design_slots(
        self,
        *,
        prompt_payload: Dict[str, Any],
        atom_bundle: CanonicalAtomBundle,
        allowed_components: Sequence[str],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Stage2 design-only plan over Stage1 annotations + deterministic atoms."""
        payload = dict(prompt_payload)
        payload["known_atom_ids"] = list(atom_bundle.usable_atom_ids or [])
        payload["allowed_components"] = [
            str(item).strip() for item in list(allowed_components or []) if str(item).strip()
        ]
        primary_model = str(getattr(settings, "reader_mm_layout_model", "qwen3.5-plus") or "qwen3.5-plus")
        fallback_model = str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-plus") or "qwen3-vl-plus")
        timeout_ms = int(getattr(settings, "reader_mm_timeout_ms", 90000) or 90000)
        attempt_count = 0
        last_error: Optional[RenderPipelineContractError] = None

        async def _attempt(model: str, retry_hint: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[RenderPipelineContractError]]:
            nonlocal attempt_count
            attempt_count += 1
            current_payload = dict(payload)
            if retry_hint:
                current_payload["retry_hint"] = retry_hint
            advice = await self._call_mm_model(
                model=model,
                prompt_payload=current_payload,
                timeout_ms=timeout_ms,
                prompt_kind="stage2_design_v2",
            )
            if not isinstance(advice, dict):
                return None, RenderPipelineContractError(
                    code="STAGE2_INVALID_JSON",
                    stage="stage2",
                    message="Stage2 design response is not a JSON object",
                    details={"model": model},
                )
            try:
                validated = validate_stage2_design_output(
                    advice,
                    known_atom_ids=list(atom_bundle.usable_atom_ids or []),
                    allowed_components=list(payload.get("allowed_components") or []),
                )
                return validated, None
            except RenderPipelineContractError as exc:
                return None, exc

        validated, err = await _attempt(primary_model, "")
        if isinstance(validated, dict):
            return validated, {
                "used": True,
                "model": primary_model,
                "fallback_used": False,
                "retry_used": False,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err
        retry_hint = ""
        if isinstance(last_error, RenderPipelineContractError):
            retry_hint = (
                f"Previous output failed validation: code={last_error.code}, stage={last_error.stage}. "
                "Return strict JSON and ensure full atom coverage partition."
            )
        validated_retry, err_retry = await _attempt(primary_model, retry_hint)
        if isinstance(validated_retry, dict):
            return validated_retry, {
                "used": True,
                "model": primary_model,
                "fallback_used": False,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": None,
            }
        last_error = err_retry

        if fallback_model and fallback_model != primary_model:
            validated_fb, err_fb = await _attempt(fallback_model, retry_hint)
            if isinstance(validated_fb, dict):
                return validated_fb, {
                    "used": True,
                    "model": fallback_model,
                    "fallback_used": True,
                    "retry_used": True,
                    "retry_count": max(0, attempt_count - 1),
                    "error": None,
                }
            last_error = err_fb

        if isinstance(last_error, RenderPipelineContractError):
            return None, {
                "used": False,
                "model": primary_model,
                "fallback_used": bool(fallback_model and fallback_model != primary_model),
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "error": last_error.to_dict(),
            }
        return None, {
            "used": False,
            "model": primary_model,
            "fallback_used": bool(fallback_model and fallback_model != primary_model),
            "retry_used": True,
            "retry_count": max(0, attempt_count - 1),
            "error": {"code": "STAGE2_INVALID_JSON", "message": "unknown stage2 design error"},
        }

    async def build_line_parse_advice(
        self,
        *,
        prompt_payload: Dict[str, Any],
        valid_line_ids: Sequence[str],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Strict multimodal parser pass with schema validation + retry."""
        payload = dict(prompt_payload)
        payload["valid_line_ids"] = [str(item).strip() for item in list(valid_line_ids or []) if str(item).strip()]
        valid_id_set = set(payload.get("valid_line_ids") or [])
        valid_word_ids = set(str(item).strip() for item in list(payload.get("valid_word_ids") or []) if str(item).strip())
        valid_char_ids = [str(item).strip() for item in list(payload.get("valid_char_ids") or []) if str(item).strip()]
        source_text_full = str(payload.get("source_text_full") or "")
        source_checksum = str(payload.get("source_checksum") or "").strip()
        source_word_spans = [item for item in list(payload.get("source_word_spans") or []) if isinstance(item, dict)]
        image_placeholders = [item for item in list(payload.get("image_placeholders") or []) if isinstance(item, dict)]
        parser_model = str(getattr(settings, "reader_mm_parser_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        fallback_model = str(
            getattr(
                settings,
                "reader_mm_parser_fallback_model",
                getattr(settings, "reader_mm_layout_model", "qwen3.5-flash"),
            )
            or "qwen3.5-flash"
        )
        timeout_ms = int(getattr(settings, "reader_mm_timeout_ms", 6000) or 6000)
        parser_timeout_cap = int(getattr(settings, "reader_mm_parser_timeout_ms", 18000) or 18000)
        timeout_ms = max(6000, min(timeout_ms, parser_timeout_cap))
        attempt_count = 0
        last_errors: List[str] = []

        async def _attempt(*, model: str, retry_hint: str = "") -> Tuple[Optional[Dict[str, Any]], List[str]]:
            nonlocal attempt_count
            attempt_count += 1
            current_payload = dict(payload)
            if retry_hint:
                current_payload["retry_hint"] = retry_hint
            advice = await self._call_mm_model(
                model=model,
                prompt_payload=current_payload,
                timeout_ms=timeout_ms,
                prompt_kind="line_parse_advice_v1",
            )
            if not isinstance(advice, dict):
                return None, ["response_not_json_object"]

            advice = self._coerce_page_structure_v2_payload(advice)
            if isinstance(advice.get("blocks"), list):
                required_keys = {"blocks", "counts", "notes"}
            else:
                required_keys = {"doc_nav_tree", "block_groups", "counts", "notes"}
            missing_keys = sorted(key for key in required_keys if key not in advice)

            validated = self.validate_line_parse_advice_json(
                payload=advice,
                valid_line_ids=valid_id_set,
                valid_word_ids=valid_word_ids,
                valid_char_ids=valid_char_ids,
                source_text_full=source_text_full,
                source_checksum=source_checksum,
                source_word_spans=source_word_spans,
                image_placeholders=image_placeholders,
            )
            if isinstance(validated, dict):
                quality_errors = self._validate_line_parse_advice_quality(
                    payload=validated,
                    valid_line_ids=valid_id_set,
                    valid_word_ids=valid_word_ids,
                    valid_char_ids=valid_char_ids,
                )
                if not quality_errors:
                    return validated, []
                return None, quality_errors

            errors: List[str] = ["schema_validation_failed"]
            if missing_keys:
                errors.append("missing_required_keys:" + ",".join(missing_keys))
            return None, errors

        validated, errors = await _attempt(model=parser_model, retry_hint="")
        if isinstance(validated, dict):
            return validated, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": False,
                "retry_count": max(0, attempt_count - 1),
                "validation_errors": [],
                "error": None,
            }

        last_errors = list(errors)
        retry_hint = self._build_line_parse_retry_hint(last_errors)
        validated_retry, errors_retry = await _attempt(model=parser_model, retry_hint=retry_hint)
        if isinstance(validated_retry, dict):
            return validated_retry, {
                "used": True,
                "model": parser_model,
                "fallback_used": False,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "validation_errors": list(last_errors),
                "error": None,
            }
        last_errors = list(errors_retry)

        if fallback_model and fallback_model != parser_model:
            validated_fb, errors_fb = await _attempt(model=fallback_model, retry_hint=retry_hint)
            if isinstance(validated_fb, dict):
                return validated_fb, {
                    "used": True,
                    "model": fallback_model,
                    "fallback_used": True,
                    "retry_used": True,
                    "retry_count": max(0, attempt_count - 1),
                    "validation_errors": list(last_errors),
                    "error": None,
                }
            last_errors = list(errors_fb)
            final_error = ";".join(last_errors) or "parser_advice_invalid_or_failed"
            return None, {
                "used": False,
                "model": parser_model,
                "fallback_used": True,
                "retry_used": True,
                "retry_count": max(0, attempt_count - 1),
                "validation_errors": list(last_errors),
                "error": final_error,
            }

        final_error = ";".join(last_errors) or "parser_advice_invalid_or_failed"
        return None, {
            "used": False,
            "model": parser_model,
            "fallback_used": False,
            "retry_used": True,
            "retry_count": max(0, attempt_count - 1),
            "validation_errors": list(last_errors),
            "error": final_error,
        }

    @staticmethod
    def _coerce_page_structure_v2_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce legacy parser payload into page_structure_v2-compatible shape."""
        cloned = dict(payload or {})
        # New contract key.
        if not isinstance(cloned.get("doc_nav_tree"), list):
            if isinstance(cloned.get("toc_tree"), list):
                cloned["doc_nav_tree"] = list(cloned.get("toc_tree") or [])
            else:
                cloned["doc_nav_tree"] = []
        # Keep legacy keys for backward compatibility in downstream code/tests.
        if "toc_tree" not in cloned:
            cloned["toc_tree"] = list(cloned.get("doc_nav_tree") or [])
        if not isinstance(cloned.get("line_labels"), list):
            cloned["line_labels"] = []
        if not isinstance(cloned.get("heading_groups"), list):
            cloned["heading_groups"] = []
        if not isinstance(cloned.get("paragraph_groups"), list):
            cloned["paragraph_groups"] = []
        if not isinstance(cloned.get("figure_groups"), list):
            cloned["figure_groups"] = []
        if not isinstance(cloned.get("block_groups"), list):
            cloned["block_groups"] = []
        if not isinstance(cloned.get("counts"), dict):
            cloned["counts"] = {}
        if not isinstance(cloned.get("notes"), list):
            cloned["notes"] = []
        return cloned

    @staticmethod
    def _build_line_parse_retry_hint(validation_errors: Sequence[str]) -> str:
        errors = [str(item).strip() for item in list(validation_errors or []) if str(item).strip()]
        top_errors = errors[:6]
        return (
            "Previous output failed strict validation. "
            "Return JSON only. "
            "Preferred schema: blocks + relations + doc_nav_tree + counts + notes. "
            "blocks[].text must be exact snippets from source_text_full; spans[start,end) are optional but preferred when confident. "
            "Do not add or remove source text; block texts over non-whitespace chars must be complete and non-overlapping. "
            "For text blocks, spans must map to existing word/char ids after local resolution. "
            "If uncertain, split conservatively and keep kind=unknown. "
            f"validation_errors={'; '.join(top_errors) if top_errors else 'unknown'}"
        )

    def _validate_line_parse_advice_quality(
        self,
        *,
        payload: Dict[str, Any],
        valid_line_ids: set[str],
        valid_word_ids: Optional[set[str]] = None,
        valid_char_ids: Optional[Sequence[str]] = None,
    ) -> List[str]:
        errors: List[str] = []
        valid_ids = {str(item).strip() for item in list(valid_line_ids or set()) if str(item).strip()}
        valid_word_set = {str(item).strip() for item in list(valid_word_ids or set()) if str(item).strip()}
        valid_char_list = [str(item).strip() for item in list(valid_char_ids or []) if str(item).strip()]
        valid_char_set = set(valid_char_list)

        heading_groups = [item for item in list(payload.get("heading_groups") or []) if isinstance(item, dict)]
        paragraph_groups = [item for item in list(payload.get("paragraph_groups") or []) if isinstance(item, dict)]
        figure_groups = [item for item in list(payload.get("figure_groups") or []) if isinstance(item, dict)]
        block_groups = [item for item in list(payload.get("block_groups") or []) if isinstance(item, dict)]

        has_para_or_figure_block = any(
            str((item or {}).get("kind") or "") in {"paragraph", "list_item", "caption", "figure_meta", "table_caption"}
            for item in block_groups
            if isinstance(item, dict)
        )
        if not paragraph_groups and not figure_groups and not has_para_or_figure_block:
            errors.append("missing_paragraph_or_figure_groups")
        if not block_groups:
            errors.append("missing_block_groups")

        paragraph_line_seen: set[str] = set()
        duplicate_paragraph_line_ids: set[str] = set()
        paragraph_line_ids: set[str] = set()
        for row in paragraph_groups:
            line_ids = [str(item).strip() for item in list(row.get("line_ids") or []) if str(item).strip()]
            for line_id in line_ids:
                paragraph_line_ids.add(line_id)
                if line_id in paragraph_line_seen:
                    duplicate_paragraph_line_ids.add(line_id)
                paragraph_line_seen.add(line_id)
        if duplicate_paragraph_line_ids:
            errors.append(f"duplicate_line_ids_in_paragraph_groups:{len(duplicate_paragraph_line_ids)}")

        annotated_line_ids: set[str] = set()
        for row in heading_groups:
            for line_id in list(row.get("line_ids") or []):
                token = str(line_id).strip()
                if token:
                    annotated_line_ids.add(token)
        for row in paragraph_groups:
            for line_id in list(row.get("line_ids") or []):
                token = str(line_id).strip()
                if token:
                    annotated_line_ids.add(token)
        for row in figure_groups:
            for line_id in list(row.get("line_ids") or []):
                token = str(line_id).strip()
                if token:
                    annotated_line_ids.add(token)
            for line_id in list(row.get("caption_line_ids") or []):
                token = str(line_id).strip()
                if token:
                    annotated_line_ids.add(token)

        if valid_ids and annotated_line_ids:
            coverage = float(len(annotated_line_ids.intersection(valid_ids)) / max(1, len(valid_ids)))
            min_coverage = float(getattr(settings, "reader_mm_parser_min_line_coverage", 0.18) or 0.18)
            if coverage < min_coverage:
                errors.append(f"line_coverage_too_low:{coverage:.3f}<{min_coverage:.3f}")

        if len(paragraph_groups) == 1 and len(valid_ids) >= 20:
            single_paragraph_coverage = float(len(paragraph_line_ids.intersection(valid_ids)) / max(1, len(valid_ids)))
            max_single_coverage = float(
                getattr(settings, "reader_mm_parser_single_paragraph_max_coverage", 0.78) or 0.78
            )
            if single_paragraph_coverage > max_single_coverage:
                errors.append(
                    f"single_paragraph_over_merged:{single_paragraph_coverage:.3f}>{max_single_coverage:.3f}"
                )

        counts = dict(payload.get("counts") or {})
        expected_paragraph_count_raw = counts.get("paragraph_count")
        if expected_paragraph_count_raw is not None:
            try:
                expected_paragraph_count = int(expected_paragraph_count_raw)
            except Exception:
                expected_paragraph_count = len(paragraph_groups)
            tolerance = max(1, int(round(expected_paragraph_count * 0.4)))
            if abs(expected_paragraph_count - len(paragraph_groups)) > tolerance:
                errors.append(
                    f"paragraph_count_mismatch:{len(paragraph_groups)}!={expected_paragraph_count}"
                )

        if block_groups:
            empty_location_count = 0
            invalid_word_ref_count = 0
            invalid_char_ref_count = 0
            for row in block_groups:
                kind = str(row.get("kind") or "").strip().lower()
                line_ids = [str(item).strip() for item in list(row.get("line_ids") or []) if str(item).strip()]
                word_ids = [str(item).strip() for item in list(row.get("word_ids") or []) if str(item).strip()]
                char_ranges = [item for item in list(row.get("char_ranges") or []) if isinstance(item, dict)]
                image_refs = [str(item).strip() for item in list(row.get("image_refs") or []) if str(item).strip()]
                if not line_ids and not word_ids and not char_ranges and not image_refs:
                    empty_location_count += 1
                if valid_word_set and word_ids:
                    if any(item not in valid_word_set for item in word_ids):
                        invalid_word_ref_count += 1
                for rng in char_ranges:
                    start_id = str(rng.get("start_char_id") or "").strip()
                    end_id = str(rng.get("end_char_id") or "").strip()
                    if not start_id or not end_id:
                        invalid_char_ref_count += 1
                        continue
                    if valid_char_set and (start_id not in valid_char_set or end_id not in valid_char_set):
                        invalid_char_ref_count += 1
            if empty_location_count > 0:
                errors.append(f"block_groups_missing_locations:{empty_location_count}")
            if invalid_word_ref_count > 0:
                errors.append(f"block_groups_invalid_word_ids:{invalid_word_ref_count}")
            if invalid_char_ref_count > 0:
                errors.append(f"block_groups_invalid_char_ranges:{invalid_char_ref_count}")

        return errors

    async def _call_primary_then_fallback_with_validator(
        self,
        *,
        prompt_payload: Dict[str, Any],
        prompt_kind: str,
        validator,
        primary_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Call primary then fallback using the provided validator."""
        primary_model = str(
            primary_model
            or getattr(settings, "reader_mm_primary_model", "qwen3.5-flash")
            or "qwen3.5-flash"
        )
        fallback_model = str(
            fallback_model
            or getattr(settings, "reader_mm_fallback_model", "qwen3-vl-flash")
            or "qwen3-vl-flash"
        )
        timeout_ms = int(getattr(settings, "reader_mm_timeout_ms", 6000) or 6000)

        result = await self._call_mm_model(
            model=primary_model,
            prompt_payload=prompt_payload,
            timeout_ms=timeout_ms,
            prompt_kind=prompt_kind,
        )
        if isinstance(result, dict):
            validated = validator(result)
            if validated:
                return validated, {
                    "used": True,
                    "model": primary_model,
                    "fallback_used": False,
                    "error": None,
                }

        fallback_error = "primary_invalid_or_failed"
        if fallback_model and fallback_model != primary_model:
            fallback = await self._call_mm_model(
                model=fallback_model,
                prompt_payload=prompt_payload,
                timeout_ms=timeout_ms,
                prompt_kind=prompt_kind,
            )
            if isinstance(fallback, dict):
                validated = validator(fallback)
                if validated:
                    return validated, {
                        "used": True,
                        "model": fallback_model,
                        "fallback_used": True,
                        "error": None,
                    }
            fallback_error = "fallback_invalid_or_failed"

        return None, {
            "used": False,
            "model": primary_model,
            "fallback_used": bool(fallback_model and fallback_model != primary_model),
            "error": fallback_error,
        }

    def validate_layout_plan_v2_json(
        self,
        *,
        payload: Any,
        valid_block_ids: set[str],
        valid_line_ids: set[str],
        component_whitelist: set[str],
    ) -> Optional[Dict[str, Any]]:
        """Validate plan+segments payload; block_ids must stay in current-page allowlist."""
        if not isinstance(payload, dict):
            return None

        allowed_components = set(component_whitelist or set())
        if not allowed_components:
            return None
        allowed_kinds = {"heading", "paragraph", "list", "figure", "table", "context"}
        component_alias_map = {
            "sectionheading": "SectionHeading",
            "heading": "SectionHeading",
            "paragraph": "ParagraphProse",
            "paragraphprose": "ParagraphProse",
            "list": "ListBlock",
            "listblock": "ListBlock",
            "figurepanel": "FigurePanel",
            "figure": "FigurePanel",
            "tablepanel": "TablePanel",
            "table": "TablePanel",
            "contextrail": "ContextRail",
            "context": "ContextRail",
            "calloutbox": "CalloutBox",
            "abstractcard": "AbstractCard",
            "citationcard": "CitationCard",
            "methodologycard": "MethodologyCard",
            "compareinsightscard": "CompareInsightsCard",
            "insightclustercard": "InsightClusterCard",
            "sectionbridgecard": "SectionBridgeCard",
        }
        kind_alias_map = {
            "prose": "paragraph",
            "text": "paragraph",
            "bullet": "list",
            "image": "figure",
            "chart": "figure",
            "sidebar": "context",
        }
        zone_alias_map = {
            "main": "main_body",
            "body": "main_body",
            "mainbody": "main_body",
            "content": "main_body",
            "side": "side_context",
            "sidebar": "side_context",
            "sidebar_left": "side_context",
            "sidebar_right": "side_context",
            "side_context": "side_context",
            "context_rail": "side_context",
            "figure": "figure_meta",
            "table": "figure_meta",
            "figure_meta": "figure_meta",
        }
        sorted_line_ids = sorted({str(item).strip() for item in list(valid_line_ids or set()) if str(item).strip()})
        line_alias_map: Dict[str, str] = {}
        for idx, line_id in enumerate(sorted_line_ids, start=1):
            lower_raw = str(line_id).lower()
            normalized = re.sub(r"[^a-z0-9]+", "", lower_raw)
            if normalized:
                line_alias_map.setdefault(normalized, line_id)
            line_alias_map.setdefault(lower_raw, line_id)
            number_match = re.search(r"l0*([0-9]{1,4})", lower_raw)
            if number_match:
                line_alias_map.setdefault(str(int(number_match.group(1))), line_id)
            line_alias_map.setdefault(str(idx), line_id)

        def _resolve_line_id(raw_value: Any) -> str:
            token = str(raw_value or "").strip()
            if not token:
                return ""
            if token in valid_line_ids:
                return token
            lower_token = token.lower()
            if lower_token in line_alias_map:
                return str(line_alias_map.get(lower_token) or "")
            normalized = re.sub(r"[^a-z0-9]+", "", lower_token)
            if normalized in line_alias_map:
                return str(line_alias_map.get(normalized) or "")
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(sorted_line_ids):
                    return str(sorted_line_ids[idx - 1])
            return ""

        zones: List[Dict[str, Any]] = []
        for row in list(payload.get("zones") or [])[:120]:
            if not isinstance(row, dict):
                continue
            zone_raw = str(row.get("zone_type") or "main_body").strip().lower()
            zone_type = str(zone_alias_map.get(zone_raw) or zone_raw)
            if zone_type not in {"main_body", "side_context", "figure_meta"}:
                continue
            block_ids = [
                str(item).strip()
                for item in list(row.get("block_ids") or [])[:16]
                if str(item).strip() and str(item).strip() in valid_block_ids
            ]
            if not block_ids:
                continue
            zones.append({"zone_type": zone_type, "block_ids": block_ids})

        headings: List[Dict[str, Any]] = []
        for row in list(payload.get("headings") or [])[:80]:
            if not isinstance(row, dict):
                continue
            block_id = str(row.get("block_id") or "").strip()
            if not block_id or block_id not in valid_block_ids:
                continue
            try:
                level = int(row.get("level") or 1)
            except Exception:
                level = 1
            try:
                confidence = float(row.get("confidence") or row.get("heading_prob") or 0.0)
            except Exception:
                confidence = 0.0
            headings.append(
                {
                    "block_id": block_id,
                    "level": max(1, min(4, level)),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "text": self._normalize_spaces(str(row.get("text") or ""))[:180] or None,
                }
            )

        segments: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(payload.get("segments") or [])[:220], start=1):
            if not isinstance(row, dict):
                continue
            component_hint_raw = str(
                row.get("component_hint") or row.get("ui_component") or row.get("component") or ""
            ).strip()
            component_hint = component_alias_map.get(
                component_hint_raw.replace("_", "").replace("-", "").lower(),
                component_hint_raw,
            )
            if component_hint not in allowed_components:
                component_hint = ""

            kind_hint = str(row.get("kind_hint") or row.get("kind") or "").strip().lower()
            if not kind_hint and component_hint:
                kind_hint = {
                    "SectionHeading": "heading",
                    "ParagraphProse": "paragraph",
                    "ListBlock": "list",
                    "FigurePanel": "figure",
                    "TablePanel": "table",
                    "ContextRail": "context",
                    "CalloutBox": "paragraph",
                    "AbstractCard": "paragraph",
                    "CitationCard": "paragraph",
                    "MethodologyCard": "paragraph",
                    "CompareInsightsCard": "paragraph",
                    "InsightClusterCard": "paragraph",
                    "SectionBridgeCard": "paragraph",
                }.get(component_hint, "paragraph")
            kind = kind_alias_map.get(kind_hint, kind_hint)
            if kind not in allowed_kinds:
                kind = "paragraph"
            block_ids = [
                str(item).strip()
                for item in list(row.get("block_ids") or [])[:4]
                if str(item).strip() and str(item).strip() in valid_block_ids
            ]
            line_ids = [
                _resolve_line_id(item)
                for item in list(row.get("line_ids") or row.get("source_line_ids") or [])[:24]
                if _resolve_line_id(item)
            ]
            if not line_ids and not block_ids:
                continue
            evidence_line_ids = [
                _resolve_line_id(item)
                for item in list(row.get("evidence_line_ids") or row.get("evidence_lines") or [])[:12]
                if _resolve_line_id(item)
            ]
            if not evidence_line_ids:
                evidence_line_ids = list(line_ids)
            continuation = str(row.get("continuation") or "none").strip().lower()
            if continuation not in {"none", "from_prev", "to_next"}:
                continuation = "none"

            if not component_hint:
                component_hint = {
                    "heading": "SectionHeading",
                    "paragraph": "ParagraphProse",
                    "list": "ListBlock",
                    "figure": "FigurePanel",
                    "table": "TablePanel",
                    "context": "ContextRail",
                }.get(kind, "ParagraphProse")
            if component_hint not in allowed_components:
                component_hint = "ParagraphProse" if "ParagraphProse" in allowed_components else sorted(allowed_components)[0]

            try:
                confidence = float(row.get("confidence") or row.get("segment_confidence") or 0.0)
            except Exception:
                confidence = 0.0
            segments.append(
                {
                    "segment_id": str(row.get("segment_id") or f"seg_{idx}").strip() or f"seg_{idx}",
                    "kind": kind,
                    # Backward compatible field; treated as hint only downstream.
                    "ui_component": component_hint,
                    "component_hint": component_hint,
                    "kind_hint": kind,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "block_ids": block_ids,
                    "line_ids": line_ids,
                    "evidence_line_ids": evidence_line_ids,
                    "title": self._normalize_spaces(str(row.get("title") or ""))[:180] or None,
                    "continuation": continuation,
                    "reason": self._normalize_spaces(str(row.get("reason") or ""))[:200] or "",
                }
            )

        if not segments:
            return None

        continuation_raw = payload.get("continuation")
        continuation = {
            "from_prev": [],
            "to_next": [],
            "confidence": 0.0,
            "reason": "",
        }
        if isinstance(continuation_raw, dict):
            from_prev = [
                str(item).strip()
                for item in list(continuation_raw.get("from_prev") or [])[:12]
                if str(item).strip() and str(item).strip() in valid_block_ids
            ]
            to_next = [
                str(item).strip()
                for item in list(continuation_raw.get("to_next") or [])[:12]
                if str(item).strip() and str(item).strip() in valid_block_ids
            ]
            try:
                conf = float(continuation_raw.get("confidence") or 0.0)
            except Exception:
                conf = 0.0
            continuation = {
                "from_prev": from_prev,
                "to_next": to_next,
                "confidence": max(0.0, min(1.0, conf)),
                "reason": self._normalize_spaces(str(continuation_raw.get("reason") or ""))[:180],
            }

        ui_suggestions: List[Dict[str, Any]] = []
        for row in list(payload.get("ui_suggestions") or [])[:24]:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "").strip()
            if not kind:
                continue
            target_block_ids = [
                str(item).strip()
                for item in list(row.get("target_block_ids") or [])[:8]
                if str(item).strip() and str(item).strip() in valid_block_ids
            ]
            ui_suggestions.append(
                {
                    "kind": kind,
                    "target_block_ids": target_block_ids,
                    "reason": self._normalize_spaces(str(row.get("reason") or ""))[:180],
                }
            )

        notes = [
            self._normalize_spaces(str(item or ""))[:180]
            for item in list(payload.get("notes") or [])[:18]
            if self._normalize_spaces(str(item or ""))
        ]

        return {
            "zones": zones,
            "headings": headings,
            "segments": segments,
            "continuation": continuation,
            "ui_suggestions": ui_suggestions,
            "notes": notes,
        }

    @staticmethod
    def _extract_text_from_spans(*, source_text: str, spans: Sequence[Dict[str, int]]) -> str:
        parts: List[str] = []
        for row in list(spans or []):
            if not isinstance(row, dict):
                continue
            start = int(row.get("start") or 0)
            end = int(row.get("end") or 0)
            if end <= start:
                continue
            parts.append(str(source_text[start:end]))
        return ReaderMultimodalLayoutService._normalize_spaces(" ".join(parts))

    @staticmethod
    def _tokenize_match_text(text: str) -> List[str]:
        normalized = ReaderMultimodalLayoutService._normalize_spaces(str(text or "")).lower()
        if not normalized:
            return []
        tokens = [tok for tok in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE) if tok]
        if tokens:
            return tokens
        return [normalized]

    def _match_block_text_to_source_spans(
        self,
        *,
        source_text: str,
        source_words: Sequence[Dict[str, Any]],
        block_text: str,
        occupied_non_ws: set[int],
    ) -> List[Dict[str, int]]:
        """Match block text to source words and return one conservative span.

        This matcher is local-only and used when model output does not provide spans.
        """
        block_tokens = self._tokenize_match_text(block_text)
        if not block_tokens:
            return []

        token_stream: List[str] = []
        token_to_word_idx: List[int] = []
        for word_idx, row in enumerate(list(source_words or [])):
            try:
                start = int(row.get("start") or 0)
                end = int(row.get("end") or 0)
            except Exception:
                continue
            if end <= start:
                continue
            word_text = str(source_text[start:end])
            word_tokens = self._tokenize_match_text(word_text)
            if not word_tokens:
                continue
            for tok in word_tokens:
                token_stream.append(tok)
                token_to_word_idx.append(word_idx)

        if not token_stream or len(block_tokens) > len(token_stream):
            return []

        best_span: Optional[Tuple[int, int, int]] = None  # (overlap_penalty, start, end)
        n = len(block_tokens)
        for idx in range(0, len(token_stream) - n + 1):
            if token_stream[idx] != block_tokens[0]:
                continue
            if token_stream[idx: idx + n] != block_tokens:
                continue
            start_word_idx = token_to_word_idx[idx]
            end_word_idx = token_to_word_idx[idx + n - 1]
            try:
                start = int(source_words[start_word_idx].get("start") or 0)
                end = int(source_words[end_word_idx].get("end") or 0)
            except Exception:
                continue
            if end <= start:
                continue
            overlap_penalty = 0
            for ch_idx in range(start, end):
                ch = source_text[ch_idx]
                if str(ch).isspace():
                    continue
                if ch_idx in occupied_non_ws:
                    overlap_penalty += 1
            candidate = (overlap_penalty, start, end)
            if best_span is None or candidate < best_span:
                best_span = candidate

        if not best_span:
            return []
        return [{"start": int(best_span[1]), "end": int(best_span[2])}]

    def _normalize_page_structure_v3(
        self,
        *,
        payload: Dict[str, Any],
        source_text_full: str,
        source_checksum: str,
        source_word_spans: Optional[Sequence[Dict[str, Any]]],
        image_placeholders: Optional[Sequence[Dict[str, Any]]],
        valid_word_ids: Optional[set[str]],
        valid_char_ids: Optional[Sequence[str]],
    ) -> Optional[Dict[str, Any]]:
        """Normalize span-based page_structure_v3 payload into legacy parser schema."""
        raw_blocks = payload.get("blocks")
        if not isinstance(raw_blocks, list):
            return None

        source_text = str(source_text_full or "")
        if not source_text:
            return None
        expected_checksum = str(source_checksum or "").strip().lower()
        actual_checksum = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text else ""
        payload_checksum = str(payload.get("source_checksum") or "").strip().lower()
        if expected_checksum and expected_checksum != actual_checksum:
            return None
        if payload_checksum and expected_checksum and payload_checksum != expected_checksum:
            return None

        valid_word_set = {str(item).strip() for item in list(valid_word_ids or set()) if str(item).strip()}
        valid_char_set = {str(item).strip() for item in list(valid_char_ids or []) if str(item).strip()}
        source_words = []
        for row in list(source_word_spans or []):
            if not isinstance(row, dict):
                continue
            word_id = str(row.get("word_id") or "").strip()
            if not word_id:
                continue
            try:
                start = int(row.get("start"))
                end = int(row.get("end"))
            except Exception:
                continue
            if start < 0 or end <= start or end > len(source_text):
                continue
            start_char_id = str(row.get("start_char_id") or "").strip()
            end_char_id = str(row.get("end_char_id") or "").strip()
            source_words.append(
                {
                    "word_id": word_id,
                    "start": start,
                    "end": end,
                    "start_char_id": start_char_id,
                    "end_char_id": end_char_id,
                }
            )
        source_words = sorted(source_words, key=lambda row: (int(row["start"]), int(row["end"]), str(row["word_id"])))
        image_tokens = {
            str(item.get("token") or "").strip()
            for item in list(image_placeholders or [])
            if isinstance(item, dict) and str(item.get("token") or "").strip()
        }

        allowed_zone = {"main_body", "side_context", "figure_meta", "unknown"}
        allowed_kind = {
            "heading",
            "paragraph",
            "list_item",
            "caption",
            "figure_meta",
            "table_caption",
            "unknown",
        }
        occupied_non_ws: set[int] = set()
        block_groups: List[Dict[str, Any]] = []

        for idx, raw in enumerate(list(raw_blocks)[:360], start=1):
            if not isinstance(raw, dict):
                continue
            block_id = self._normalize_spaces(str(raw.get("block_id") or "")).lower()[:64] or f"blk_{idx:03d}"
            kind = str(raw.get("kind") or "unknown").strip().lower()
            if kind not in allowed_kind:
                kind = "unknown"
            zone_type = str(raw.get("zone_type") or "unknown").strip().lower()
            if zone_type not in allowed_zone:
                zone_type = "unknown"
            parent_node_id = self._normalize_spaces(str(raw.get("parent_block_id") or "")).lower()[:64]
            try:
                reading_order = int(raw.get("reading_order") or idx)
            except Exception:
                reading_order = idx
            confidence = self._safe_float(raw.get("confidence"), 0.0)

            block_text_raw = self._normalize_spaces(str(raw.get("text") or ""))
            spans: List[Dict[str, int]] = []
            for span in list(raw.get("spans") or [])[:120]:
                if not isinstance(span, dict):
                    continue
                try:
                    start = int(span.get("start"))
                    end = int(span.get("end"))
                except Exception:
                    continue
                if start < 0 or end <= start or end > len(source_text):
                    continue
                spans.append({"start": start, "end": end})
            spans = sorted(spans, key=lambda item: (int(item["start"]), int(item["end"])))
            if not spans and block_text_raw:
                spans = self._match_block_text_to_source_spans(
                    source_text=source_text,
                    source_words=source_words,
                    block_text=block_text_raw,
                    occupied_non_ws=occupied_non_ws,
                )

            image_refs = [str(item).strip() for item in list(raw.get("image_refs") or [])[:24] if str(item).strip()]
            if not image_refs and kind == "figure_meta":
                token_from_tags = [
                    str(item).strip()
                    for item in list(raw.get("tags") or [])
                    if str(item).strip() in image_tokens
                ]
                image_refs = token_from_tags[:24]
            if not spans and not image_refs:
                return None

            prev_end = -1
            for span in spans:
                start = int(span["start"])
                end = int(span["end"])
                if start < prev_end:
                    # Keep parsing even if model span order overlaps; final coverage gate handles quality.
                    prev_end = max(prev_end, end)
                else:
                    prev_end = end
                for i in range(start, end):
                    ch = source_text[i]
                    if str(ch).isspace():
                        continue
                    occupied_non_ws.add(i)

            word_ids: List[str] = []
            char_ranges: List[Dict[str, str]] = []
            if spans:
                for word in source_words:
                    w_start = int(word["start"])
                    w_end = int(word["end"])
                    overlap = False
                    for span in spans:
                        s = int(span["start"])
                        e = int(span["end"])
                        if w_end <= s or w_start >= e:
                            continue
                        overlap = True
                        break
                    if not overlap:
                        continue
                    word_id = str(word["word_id"])
                    if valid_word_set and word_id not in valid_word_set:
                        continue
                    word_ids.append(word_id)
                    start_char_id = str(word.get("start_char_id") or "").strip()
                    end_char_id = str(word.get("end_char_id") or "").strip()
                    if not start_char_id or not end_char_id:
                        continue
                    if valid_char_set and (start_char_id not in valid_char_set or end_char_id not in valid_char_set):
                        continue
                    char_ranges.append({"start_char_id": start_char_id, "end_char_id": end_char_id})
            word_ids = list(dict.fromkeys(word_ids))
            dedup_char_ranges: Dict[str, Dict[str, str]] = {}
            for row in char_ranges:
                key = f"{row['start_char_id']}::{row['end_char_id']}"
                dedup_char_ranges[key] = row
            char_ranges = list(dedup_char_ranges.values())

            if spans and not word_ids and kind not in {"figure_meta"}:
                return None

            block_text = self._extract_text_from_spans(source_text=source_text, spans=spans)
            if not block_text:
                block_text = block_text_raw
            block_groups.append(
                {
                    "block_id": block_id,
                    "kind": kind,
                    "title": (block_text_raw or block_text)[:220] if kind == "heading" else "",
                    "text": (block_text_raw or block_text)[:1600],
                    "parent_node_id": parent_node_id,
                    "line_ids": [],
                    "word_ids": word_ids[:400],
                    "char_ranges": char_ranges[:220],
                    "zone_type": zone_type,
                    "column_id": "unknown",
                    "reading_order": max(1, min(20000, reading_order)),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "image_refs": image_refs[:24],
                    "source_spans": spans[:120],
                }
            )

        required_non_ws = {idx for idx, ch in enumerate(source_text) if not str(ch).isspace()}
        required_count = len(required_non_ws)
        covered_count = len(occupied_non_ws.intersection(required_non_ws))
        coverage_ratio = float(covered_count / max(1, required_count))
        min_coverage = float(getattr(settings, "reader_mm_parser_min_coverage", 0.9) or 0.9)
        if coverage_ratio < max(0.6, min(0.99, min_coverage)):
            return None

        block_by_id = {str(item.get("block_id") or ""): item for item in block_groups if str(item.get("block_id") or "")}
        relations_raw = [item for item in list(payload.get("relations") or []) if isinstance(item, dict)]
        allowed_relation = {
            "belongs_to_heading",
            "caption_of",
            "table_caption_of",
            "continues_from",
            "references_figure",
        }
        relations: List[Dict[str, Any]] = []
        for row in relations_raw[:400]:
            rel_type = str(row.get("type") or "").strip().lower()
            from_id = str(row.get("from") or "").strip()
            to_id = str(row.get("to") or "").strip()
            if rel_type not in allowed_relation or not from_id or not to_id:
                continue
            if from_id not in block_by_id or to_id not in block_by_id:
                continue
            relations.append(
                {
                    "type": rel_type,
                    "from": from_id,
                    "to": to_id,
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )
        for row in relations:
            if str(row.get("type") or "") != "belongs_to_heading":
                continue
            from_id = str(row.get("from") or "")
            to_id = str(row.get("to") or "")
            if from_id in block_by_id and to_id in block_by_id:
                if not str((block_by_id[from_id] or {}).get("parent_node_id") or "").strip():
                    block_by_id[from_id]["parent_node_id"] = to_id

        heading_groups: List[Dict[str, Any]] = []
        for row in block_groups:
            if str(row.get("kind") or "") != "heading":
                continue
            heading_id = str(row.get("block_id") or "")
            text = self._extract_text_from_spans(
                source_text=source_text,
                spans=[item for item in list(row.get("source_spans") or []) if isinstance(item, dict)],
            )
            heading_groups.append(
                {
                    "heading_id": heading_id[:40],
                    "line_ids": [],
                    "title": text[:200],
                    "level": 1,
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        heading_links = {
            str(item.get("from") or ""): str(item.get("to") or "")
            for item in relations
            if str(item.get("type") or "") == "belongs_to_heading"
        }
        paragraph_groups: List[Dict[str, Any]] = []
        para_seq = 0
        for row in sorted(
            block_groups,
            key=lambda item: self._safe_int((item or {}).get("reading_order"), 0),
        ):
            kind = str(row.get("kind") or "")
            if kind not in {"paragraph", "list_item", "caption", "table_caption", "unknown"}:
                continue
            para_seq += 1
            block_id = str(row.get("block_id") or f"p{para_seq}")
            heading_id = heading_links.get(block_id, "")
            paragraph_groups.append(
                {
                    "paragraph_id": block_id[:48],
                    "line_ids": [],
                    "heading_id": heading_id[:40],
                    "zone_type": str(row.get("zone_type") or "main_body"),
                    "column_id": "unknown",
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        figure_groups: List[Dict[str, Any]] = []
        fig_seq = 0
        for row in sorted(
            block_groups,
            key=lambda item: self._safe_int((item or {}).get("reading_order"), 0),
        ):
            kind = str(row.get("kind") or "")
            if kind not in {"figure_meta", "caption", "table_caption"}:
                continue
            fig_seq += 1
            block_id = str(row.get("block_id") or f"f{fig_seq}")
            figure_groups.append(
                {
                    "figure_id": block_id[:48],
                    "line_ids": [],
                    "caption_line_ids": [],
                    "related_heading_id": heading_links.get(block_id, "")[:40],
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        doc_nav_raw = [item for item in list(payload.get("doc_nav_tree") or []) if isinstance(item, dict)]

        def _normalize_doc_nav(rows: Sequence[Dict[str, Any]], depth: int = 0) -> List[Dict[str, Any]]:
            if depth > 8:
                return []
            out: List[Dict[str, Any]] = []
            for item in rows[:120]:
                node_id = self._normalize_spaces(str(item.get("node_id") or "")).lower()[:64]
                title_block_id = str(item.get("title_block_id") or "").strip()
                title_text = ""
                if title_block_id and title_block_id in block_by_id:
                    title_text = self._extract_text_from_spans(
                        source_text=source_text,
                        spans=[entry for entry in list((block_by_id.get(title_block_id) or {}).get("source_spans") or []) if isinstance(entry, dict)],
                    )[:220]
                if not title_text:
                    title_text = self._normalize_spaces(str(item.get("title") or ""))[:220]
                try:
                    level = int(item.get("level") or 1)
                except Exception:
                    level = 1
                out.append(
                    {
                        "node_id": node_id or f"node_{depth}_{len(out)+1}",
                        "type": "section",
                        "title": title_text,
                        "level": max(1, min(4, level)),
                        "line_ids": [],
                        "zone_type": "main_body",
                        "column_id": "unknown",
                        "confidence": 0.9,
                        "children": _normalize_doc_nav([row for row in list(item.get("children") or []) if isinstance(row, dict)], depth + 1),
                    }
                )
            return out

        toc_tree = _normalize_doc_nav(doc_nav_raw, depth=0)
        if not toc_tree and heading_groups:
            toc_tree = [
                {
                    "node_id": str(row.get("heading_id") or f"node_{idx+1}"),
                    "type": "section",
                    "title": str(row.get("title") or ""),
                    "level": int(row.get("level") or 1),
                    "line_ids": [],
                    "zone_type": "main_body",
                    "column_id": "unknown",
                    "confidence": float(row.get("confidence") or 0.8),
                    "children": [],
                }
                for idx, row in enumerate(heading_groups[:120])
                if isinstance(row, dict)
            ]

        counts = {
            "heading_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") == "heading")),
            "paragraph_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"paragraph", "list_item"})),
            "figure_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"figure_meta", "caption"})),
            "table_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"table_caption"})),
            "block_count": int(len(block_groups)),
            "relation_count": int(len(relations)),
        }
        notes = [
            self._normalize_spaces(str(item or ""))[:180]
            for item in list(payload.get("notes") or [])[:18]
            if self._normalize_spaces(str(item or ""))
        ]
        notes.append(f"coverage_ratio={coverage_ratio:.4f}")
        blocks_v3 = [
            {
                "block_id": str(row.get("block_id") or ""),
                "kind": str(row.get("kind") or "unknown"),
                "zone_type": str(row.get("zone_type") or "unknown"),
                "parent_block_id": str(row.get("parent_node_id") or "") or None,
                "reading_order": self._safe_int(row.get("reading_order"), 0),
                "text": self._normalize_spaces(str(row.get("text") or "")),
                "spans": [item for item in list(row.get("source_spans") or []) if isinstance(item, dict)],
                "confidence": self._safe_float(row.get("confidence"), 0.0),
                "image_refs": [str(item).strip() for item in list(row.get("image_refs") or []) if str(item).strip()],
            }
            for row in block_groups
            if isinstance(row, dict)
        ]

        return {
            "source_checksum": expected_checksum or actual_checksum,
            "blocks": blocks_v3,
            "line_labels": [],
            "line_index": [],
            "doc_nav_tree": toc_tree,
            "toc_tree": toc_tree,
            "heading_groups": heading_groups,
            "paragraph_groups": paragraph_groups,
            "figure_groups": figure_groups,
            "block_groups": block_groups,
            "relations": relations,
            "counts": counts,
            "notes": notes,
        }

    def _normalize_page_structure_v3_permissive(
        self,
        *,
        payload: Dict[str, Any],
        source_text_full: str,
        source_checksum: str,
        source_word_spans: Optional[Sequence[Dict[str, Any]]],
        image_placeholders: Optional[Sequence[Dict[str, Any]]],
        valid_word_ids: Optional[set[str]],
        valid_char_ids: Optional[Sequence[str]],
    ) -> Optional[Dict[str, Any]]:
        """Best-effort normalizer for blocks-first parser JSON.

        Used only when strict normalization fails. It still enforces:
        - known kinds/zones
        - block text must map to source text (or image refs for figure_meta)
        - word/char refs must come from valid id sets when present
        """
        raw_blocks = payload.get("blocks")
        if not isinstance(raw_blocks, list):
            return None

        source_text = str(source_text_full or "")
        if not source_text:
            return None
        expected_checksum = str(source_checksum or "").strip().lower()
        actual_checksum = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text else ""
        payload_checksum = str(payload.get("source_checksum") or "").strip().lower()
        if payload_checksum and expected_checksum and payload_checksum != expected_checksum:
            return None

        valid_word_set = {str(item).strip() for item in list(valid_word_ids or set()) if str(item).strip()}
        valid_char_set = {str(item).strip() for item in list(valid_char_ids or []) if str(item).strip()}
        source_words: List[Dict[str, Any]] = []
        for row in list(source_word_spans or []):
            if not isinstance(row, dict):
                continue
            word_id = str(row.get("word_id") or "").strip()
            if not word_id:
                continue
            try:
                start = int(row.get("start"))
                end = int(row.get("end"))
            except Exception:
                continue
            if start < 0 or end <= start or end > len(source_text):
                continue
            source_words.append(
                {
                    "word_id": word_id,
                    "start": start,
                    "end": end,
                    "start_char_id": str(row.get("start_char_id") or "").strip(),
                    "end_char_id": str(row.get("end_char_id") or "").strip(),
                }
            )
        source_words = sorted(source_words, key=lambda row: (int(row["start"]), int(row["end"]), str(row["word_id"])))
        image_tokens = {
            str(item.get("token") or "").strip()
            for item in list(image_placeholders or [])
            if isinstance(item, dict) and str(item.get("token") or "").strip()
        }

        allowed_zone = {"main_body", "side_context", "figure_meta", "unknown"}
        allowed_kind = {
            "heading",
            "paragraph",
            "list_item",
            "caption",
            "figure_meta",
            "table_caption",
            "unknown",
        }

        occupied_non_ws: set[int] = set()
        block_groups: List[Dict[str, Any]] = []
        for idx, raw in enumerate(list(raw_blocks)[:360], start=1):
            if not isinstance(raw, dict):
                continue
            block_id = self._normalize_spaces(str(raw.get("block_id") or "")).lower()[:64] or f"blk_{idx:03d}"
            kind = str(raw.get("kind") or "unknown").strip().lower()
            if kind not in allowed_kind:
                kind = "unknown"
            zone_type = str(raw.get("zone_type") or "unknown").strip().lower()
            if zone_type not in allowed_zone:
                zone_type = "unknown"
            parent_node_id = self._normalize_spaces(str(raw.get("parent_block_id") or "")).lower()[:64]
            reading_order = self._safe_int(raw.get("reading_order"), idx)
            confidence = self._safe_float(raw.get("confidence"), 0.0)
            block_text_raw = self._normalize_spaces(str(raw.get("text") or ""))

            spans: List[Dict[str, int]] = []
            for span in list(raw.get("spans") or [])[:120]:
                if not isinstance(span, dict):
                    continue
                start = self._safe_int(span.get("start"), -1)
                end = self._safe_int(span.get("end"), -1)
                if start < 0 or end <= start or end > len(source_text):
                    continue
                spans.append({"start": start, "end": end})
            spans = sorted(spans, key=lambda item: (int(item["start"]), int(item["end"])))
            if not spans and block_text_raw:
                spans = self._match_block_text_to_source_spans(
                    source_text=source_text,
                    source_words=source_words,
                    block_text=block_text_raw,
                    occupied_non_ws=occupied_non_ws,
                )

            image_refs = [str(item).strip() for item in list(raw.get("image_refs") or [])[:24] if str(item).strip()]
            if not image_refs and kind == "figure_meta":
                image_refs = [
                    str(item).strip()
                    for item in list(raw.get("tags") or [])
                    if str(item).strip() in image_tokens
                ][:24]

            if not spans and not image_refs:
                continue

            for span in spans:
                start = int(span["start"])
                end = int(span["end"])
                for i in range(start, end):
                    ch = source_text[i]
                    if str(ch).isspace():
                        continue
                    occupied_non_ws.add(i)

            word_ids: List[str] = []
            char_ranges: List[Dict[str, str]] = []
            if spans:
                for word in source_words:
                    w_start = int(word["start"])
                    w_end = int(word["end"])
                    if all(w_end <= int(span["start"]) or w_start >= int(span["end"]) for span in spans):
                        continue
                    word_id = str(word["word_id"] or "").strip()
                    if not word_id:
                        continue
                    if valid_word_set and word_id not in valid_word_set:
                        continue
                    word_ids.append(word_id)
                    start_char_id = str(word.get("start_char_id") or "").strip()
                    end_char_id = str(word.get("end_char_id") or "").strip()
                    if not start_char_id or not end_char_id:
                        continue
                    if valid_char_set and (start_char_id not in valid_char_set or end_char_id not in valid_char_set):
                        continue
                    char_ranges.append({"start_char_id": start_char_id, "end_char_id": end_char_id})
            word_ids = list(dict.fromkeys(word_ids))
            dedup_char_ranges: Dict[str, Dict[str, str]] = {}
            for row in char_ranges:
                key = f"{row['start_char_id']}::{row['end_char_id']}"
                dedup_char_ranges[key] = row
            char_ranges = list(dedup_char_ranges.values())

            block_text = self._extract_text_from_spans(source_text=source_text, spans=spans) or block_text_raw
            block_groups.append(
                {
                    "block_id": block_id,
                    "kind": kind,
                    "title": (block_text or block_text_raw)[:220] if kind == "heading" else "",
                    "text": (block_text or block_text_raw)[:1600],
                    "parent_node_id": parent_node_id,
                    "line_ids": [],
                    "word_ids": word_ids[:400],
                    "char_ranges": char_ranges[:220],
                    "zone_type": zone_type,
                    "column_id": "unknown",
                    "reading_order": max(1, min(20000, reading_order)),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "image_refs": image_refs[:24],
                    "source_spans": spans[:120],
                }
            )

        if not block_groups:
            return None

        block_by_id = {str(item.get("block_id") or ""): item for item in block_groups if str(item.get("block_id") or "")}
        allowed_relation = {
            "belongs_to_heading",
            "caption_of",
            "table_caption_of",
            "continues_from",
            "references_figure",
        }
        relations: List[Dict[str, Any]] = []
        for row in list(payload.get("relations") or [])[:400]:
            if not isinstance(row, dict):
                continue
            rel_type = str(row.get("type") or "").strip().lower()
            from_id = str(row.get("from") or "").strip()
            to_id = str(row.get("to") or "").strip()
            if rel_type not in allowed_relation or not from_id or not to_id:
                continue
            if from_id not in block_by_id or to_id not in block_by_id:
                continue
            relations.append(
                {
                    "type": rel_type,
                    "from": from_id,
                    "to": to_id,
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        heading_links = {
            str(item.get("from") or ""): str(item.get("to") or "")
            for item in relations
            if str(item.get("type") or "") == "belongs_to_heading"
        }
        for from_id, to_id in heading_links.items():
            if from_id in block_by_id and to_id in block_by_id:
                if not str((block_by_id[from_id] or {}).get("parent_node_id") or "").strip():
                    block_by_id[from_id]["parent_node_id"] = to_id

        heading_groups: List[Dict[str, Any]] = []
        for row in block_groups:
            if str(row.get("kind") or "") != "heading":
                continue
            heading_id = str(row.get("block_id") or "")
            title = self._extract_text_from_spans(
                source_text=source_text,
                spans=[item for item in list(row.get("source_spans") or []) if isinstance(item, dict)],
            ) or self._normalize_spaces(str(row.get("title") or ""))
            heading_groups.append(
                {
                    "heading_id": heading_id[:40],
                    "line_ids": [],
                    "title": title[:220],
                    "level": 1,
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        paragraph_groups: List[Dict[str, Any]] = []
        figure_groups: List[Dict[str, Any]] = []
        for row in sorted(block_groups, key=lambda item: self._safe_int((item or {}).get("reading_order"), 0)):
            kind = str(row.get("kind") or "")
            block_id = str(row.get("block_id") or "")
            if kind in {"figure_meta", "caption", "table_caption"}:
                figure_groups.append(
                    {
                        "figure_id": block_id[:48],
                        "line_ids": [],
                        "caption_line_ids": [],
                        "related_heading_id": heading_links.get(block_id, "")[:40],
                        "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                    }
                )
                continue
            if kind in {"heading"}:
                continue
            paragraph_groups.append(
                {
                    "paragraph_id": block_id[:48],
                    "line_ids": [],
                    "heading_id": heading_links.get(block_id, "")[:40],
                    "zone_type": str(row.get("zone_type") or "main_body"),
                    "column_id": "unknown",
                    "confidence": max(0.0, min(1.0, self._safe_float(row.get("confidence"), 0.0))),
                }
            )

        doc_nav_raw = [item for item in list(payload.get("doc_nav_tree") or []) if isinstance(item, dict)]
        toc_tree: List[Dict[str, Any]] = []
        if doc_nav_raw:
            for idx, item in enumerate(doc_nav_raw[:120], start=1):
                title_block_id = str(item.get("title_block_id") or "").strip()
                title = ""
                if title_block_id and title_block_id in block_by_id:
                    title = self._extract_text_from_spans(
                        source_text=source_text,
                        spans=[entry for entry in list((block_by_id.get(title_block_id) or {}).get("source_spans") or []) if isinstance(entry, dict)],
                    )[:220]
                if not title:
                    title = self._normalize_spaces(str(item.get("title") or ""))[:220]
                toc_tree.append(
                    {
                        "node_id": self._normalize_spaces(str(item.get("node_id") or f"node_{idx}")).lower()[:64],
                        "type": "section",
                        "title": title,
                        "level": max(1, min(4, self._safe_int(item.get("level"), 1))),
                        "line_ids": [],
                        "zone_type": "main_body",
                        "column_id": "unknown",
                        "confidence": 0.85,
                        "children": [],
                    }
                )
        if not toc_tree and heading_groups:
            toc_tree = [
                {
                    "node_id": str(row.get("heading_id") or f"node_{idx+1}"),
                    "type": "section",
                    "title": str(row.get("title") or ""),
                    "level": int(row.get("level") or 1),
                    "line_ids": [],
                    "zone_type": "main_body",
                    "column_id": "unknown",
                    "confidence": float(row.get("confidence") or 0.8),
                    "children": [],
                }
                for idx, row in enumerate(heading_groups[:120])
                if isinstance(row, dict)
            ]

        required_non_ws = {idx for idx, ch in enumerate(source_text) if not str(ch).isspace()}
        covered_count = len(occupied_non_ws.intersection(required_non_ws))
        coverage_ratio = float(covered_count / max(1, len(required_non_ws)))

        counts = {
            "heading_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") == "heading")),
            "paragraph_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"paragraph", "list_item"})),
            "figure_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"figure_meta", "caption"})),
            "table_count": int(sum(1 for row in block_groups if str(row.get("kind") or "") in {"table_caption"})),
            "block_count": int(len(block_groups)),
            "relation_count": int(len(relations)),
        }
        notes = [
            self._normalize_spaces(str(item or ""))[:180]
            for item in list(payload.get("notes") or [])[:18]
            if self._normalize_spaces(str(item or ""))
        ]
        notes.append("normalized_by=permissive")
        notes.append(f"coverage_ratio={coverage_ratio:.4f}")

        blocks_v3 = [
            {
                "block_id": str(row.get("block_id") or ""),
                "kind": str(row.get("kind") or "unknown"),
                "zone_type": str(row.get("zone_type") or "unknown"),
                "parent_block_id": str(row.get("parent_node_id") or "") or None,
                "reading_order": self._safe_int(row.get("reading_order"), 0),
                "text": self._normalize_spaces(str(row.get("text") or "")),
                "spans": [item for item in list(row.get("source_spans") or []) if isinstance(item, dict)],
                "confidence": self._safe_float(row.get("confidence"), 0.0),
                "image_refs": [str(item).strip() for item in list(row.get("image_refs") or []) if str(item).strip()],
            }
            for row in block_groups
            if isinstance(row, dict)
        ]

        return {
            "source_checksum": expected_checksum or actual_checksum,
            "blocks": blocks_v3,
            "line_labels": [],
            "line_index": [],
            "doc_nav_tree": toc_tree,
            "toc_tree": toc_tree,
            "heading_groups": heading_groups,
            "paragraph_groups": paragraph_groups,
            "figure_groups": figure_groups,
            "block_groups": block_groups,
            "relations": relations,
            "counts": counts,
            "notes": notes,
        }

    def validate_line_parse_advice_json(
        self,
        *,
        payload: Any,
        valid_line_ids: set[str],
        valid_word_ids: Optional[set[str]] = None,
        valid_char_ids: Optional[Sequence[str]] = None,
        source_text_full: str = "",
        source_checksum: str = "",
        source_word_spans: Optional[Sequence[Dict[str, Any]]] = None,
        image_placeholders: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Validate parser advice JSON with strict line-level/group-level contracts."""
        if not isinstance(payload, dict):
            return None
        normalized_page_structure = self._normalize_page_structure_v3(
            payload=payload,
            source_text_full=source_text_full,
            source_checksum=source_checksum,
            source_word_spans=source_word_spans,
            image_placeholders=image_placeholders,
            valid_word_ids=valid_word_ids,
            valid_char_ids=valid_char_ids,
        )
        if isinstance(normalized_page_structure, dict):
            return normalized_page_structure
        permissive_page_structure = self._normalize_page_structure_v3_permissive(
            payload=payload,
            source_text_full=source_text_full,
            source_checksum=source_checksum,
            source_word_spans=source_word_spans,
            image_placeholders=image_placeholders,
            valid_word_ids=valid_word_ids,
            valid_char_ids=valid_char_ids,
        )
        if isinstance(permissive_page_structure, dict):
            return permissive_page_structure
        payload = self._coerce_page_structure_v2_payload(payload)

        valid_ids = {str(item).strip() for item in list(valid_line_ids or set()) if str(item).strip()}
        valid_word_set = {str(item).strip() for item in list(valid_word_ids or set()) if str(item).strip()}
        valid_char_list = [str(item).strip() for item in list(valid_char_ids or []) if str(item).strip()]
        valid_char_set = set(valid_char_list)
        char_order_map = {char_id: idx for idx, char_id in enumerate(valid_char_list)}

        allowed_zone = {"main_body", "side_context", "figure_meta", "unknown"}
        allowed_column = {"main", "main_left", "main_right", "sidebar_left", "sidebar_right", "sidebar", "unknown"}
        allowed_node_types = {"section", "heading", "paragraph", "figure", "caption", "context"}
        allowed_block_kinds = {
            "heading",
            "paragraph",
            "list_item",
            "caption",
            "figure_meta",
            "side_context",
            "table_caption",
            "context",
            "unknown",
        }
        used_heading_ids: set[str] = set()
        sorted_ids = sorted(valid_ids)
        line_alias_map: Dict[str, str] = {}
        for idx, line_id in enumerate(sorted_ids, start=1):
            lower_raw = str(line_id).lower()
            line_alias_map.setdefault(lower_raw, line_id)
            number_match = re.search(r"_l(\d+)_", lower_raw)
            if number_match:
                line_alias_map.setdefault(str(int(number_match.group(1))), line_id)
            line_alias_map.setdefault(str(idx), line_id)

        def _resolve_line_id(value: Any) -> str:
            token = self._normalize_line_id(value)
            if not token:
                return ""
            if token in valid_ids:
                return token
            lower_token = token.lower()
            if lower_token in line_alias_map:
                return line_alias_map[lower_token]
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(sorted_ids):
                    return str(sorted_ids[idx - 1])
            return ""

        def _normalized_line_ids(values: Any, *, limit: int) -> List[str]:
            rows: List[str] = []
            for item in list(values or [])[:limit]:
                line_id = _resolve_line_id(item)
                if not line_id:
                    continue
                rows.append(line_id)
            # Keep order while removing duplicates.
            return list(dict.fromkeys(rows))

        def _normalized_word_ids(values: Any, *, limit: int) -> List[str]:
            rows: List[str] = []
            for item in list(values or [])[:limit]:
                token = str(item).strip()
                if not token:
                    continue
                if valid_word_set and token not in valid_word_set:
                    continue
                rows.append(token)
            return list(dict.fromkeys(rows))

        char_alias_map: Dict[str, str] = {}
        for idx, char_id in enumerate(valid_char_list, start=1):
            key = str(char_id).strip()
            if not key:
                continue
            lower_key = key.lower()
            char_alias_map.setdefault(lower_key, key)
            char_alias_map.setdefault(str(idx), key)
            number_match = re.search(r"(\d+)$", lower_key)
            if number_match:
                char_alias_map.setdefault(str(int(number_match.group(1))), key)

        def _resolve_char_id(value: Any) -> str:
            token = str(value or "").strip()
            if not token:
                return ""
            if token in valid_char_set:
                return token
            lower = token.lower()
            if lower in char_alias_map:
                return char_alias_map[lower]
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(valid_char_list):
                    return str(valid_char_list[idx - 1])
            return ""

        def _normalized_char_ranges(value: Any, *, limit: int) -> List[Dict[str, str]]:
            rows: List[Dict[str, str]] = []
            for item in list(value or [])[:limit]:
                if not isinstance(item, dict):
                    continue
                start_id = _resolve_char_id(
                    item.get("start_char_id")
                    or item.get("start")
                    or item.get("from_char_id")
                    or item.get("from")
                )
                end_id = _resolve_char_id(
                    item.get("end_char_id")
                    or item.get("end")
                    or item.get("to_char_id")
                    or item.get("to")
                )
                if not start_id or not end_id:
                    continue
                if char_order_map and char_order_map.get(start_id, -1) > char_order_map.get(end_id, -1):
                    continue
                rows.append({"start_char_id": start_id, "end_char_id": end_id})
            dedup: Dict[str, Dict[str, str]] = {}
            for row in rows:
                key = f"{row['start_char_id']}::{row['end_char_id']}"
                dedup[key] = row
            return list(dedup.values())

        def _safe_prob(value: Any, default: float = 0.0) -> float:
            try:
                raw = float(value)
            except Exception:
                raw = default
            return max(0.0, min(1.0, raw))

        def _normalize_zone(value: Any) -> str:
            token = str(value or "main_body").strip().lower()
            return token if token in allowed_zone else "main_body"

        def _normalize_column(value: Any) -> str:
            token = str(value or "unknown").strip().lower()
            return token if token in allowed_column else "unknown"

        toc_node_seq = 0

        def _normalize_toc_tree(rows: Any, *, depth: int = 0, heading_stack: Optional[List[str]] = None) -> List[Dict[str, Any]]:
            nonlocal toc_node_seq
            if depth > 8:
                return []
            output: List[Dict[str, Any]] = []
            for raw in list(rows or [])[:180]:
                if not isinstance(raw, dict):
                    continue
                node_type = str(raw.get("type") or raw.get("node_type") or "").strip().lower()
                if node_type not in allowed_node_types:
                    continue
                toc_node_seq += 1
                node_id = self._normalize_spaces(str(raw.get("node_id") or "")).lower()[:64] or f"node_{toc_node_seq}"
                title = self._normalize_spaces(str(raw.get("title") or raw.get("text") or ""))[:220]
                line_ids = _normalized_line_ids(
                    raw.get("line_ids") or (raw.get("loc") or {}).get("line_ids"),
                    limit=80,
                )
                zone_type = _normalize_zone(raw.get("zone_type") or raw.get("zone"))
                column_id = _normalize_column(raw.get("column_id") or raw.get("column"))
                level = None
                try:
                    if raw.get("level") is not None:
                        level = max(1, min(4, int(raw.get("level") or 1)))
                except Exception:
                    level = None
                confidence = _safe_prob(raw.get("confidence"), default=0.0)

                child_heading_stack = list(heading_stack or [])
                if node_type in {"section", "heading"}:
                    used_heading_ids.add(node_id)
                    child_heading_stack.append(node_id)

                children = _normalize_toc_tree(
                    raw.get("children"),
                    depth=depth + 1,
                    heading_stack=child_heading_stack,
                )
                if not line_ids and not children and node_type not in {"figure", "section"}:
                    continue

                output.append(
                    {
                        "node_id": node_id,
                        "type": node_type,
                        "title": title,
                        "level": level,
                        "line_ids": line_ids,
                        "zone_type": zone_type,
                        "column_id": column_id,
                        "confidence": confidence,
                        "children": children,
                    }
                )
            return output

        line_labels: List[Dict[str, Any]] = []
        for row in list(payload.get("line_labels") or [])[:320]:
            if not isinstance(row, dict):
                continue
            line_id = _resolve_line_id(row.get("line_id"))
            if not line_id:
                continue
            zone_type = _normalize_zone(row.get("zone_type") or "unknown")
            column_id = _normalize_column(row.get("column_id") or "unknown")
            try:
                heading_prob = float(row.get("heading_prob") or 0.0)
            except Exception:
                heading_prob = 0.0
            line_labels.append(
                {
                    "line_id": line_id,
                    "zone_type": zone_type,
                    "column_id": column_id,
                    "paragraph_break_after": bool(row.get("paragraph_break_after")),
                    "heading_prob": max(0.0, min(1.0, heading_prob)),
                }
            )

        line_index: List[Dict[str, Any]] = []
        for row in list(payload.get("line_index") or [])[:420]:
            if not isinstance(row, dict):
                continue
            line_id = _resolve_line_id(row.get("line_id"))
            if not line_id:
                continue
            assigned_node_id = self._normalize_spaces(str(row.get("assigned_node_id") or "")).lower()[:64]
            line_index.append(
                {
                    "line_id": line_id,
                    "assigned_node_id": assigned_node_id,
                    "zone_type": _normalize_zone(row.get("zone_type")),
                    "column_id": _normalize_column(row.get("column_id")),
                }
            )

        heading_groups: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(payload.get("heading_groups") or [])[:80], start=1):
            if not isinstance(row, dict):
                continue
            line_ids = _normalized_line_ids(row.get("line_ids"), limit=8)
            if not line_ids:
                continue
            heading_id = self._normalize_spaces(str(row.get("heading_id") or "")).lower()
            if not heading_id:
                heading_id = f"h{idx}"
            used_heading_ids.add(heading_id)
            try:
                level = int(row.get("level") or 1)
            except Exception:
                level = 1
            heading_groups.append(
                {
                    "heading_id": heading_id[:40],
                    "line_ids": line_ids,
                    "title": self._normalize_spaces(str(row.get("title") or ""))[:200],
                    "level": max(1, min(4, level)),
                    "confidence": _safe_prob(row.get("confidence"), default=0.0),
                }
            )

        paragraph_groups: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(payload.get("paragraph_groups") or [])[:220], start=1):
            if not isinstance(row, dict):
                continue
            line_ids = _normalized_line_ids(row.get("line_ids"), limit=64)
            if not line_ids:
                continue
            zone_type = str(row.get("zone_type") or "main_body").strip().lower()
            if zone_type not in allowed_zone:
                zone_type = "main_body"
            column_id = str(row.get("column_id") or "unknown").strip().lower()
            if column_id not in allowed_column:
                column_id = "unknown"
            paragraph_id = self._normalize_spaces(str(row.get("paragraph_id") or "")).lower()[:48] or f"p{idx}"
            heading_id = self._normalize_spaces(str(row.get("heading_id") or "")).lower()[:40]
            if heading_id and heading_id not in used_heading_ids:
                heading_id = ""
            paragraph_groups.append(
                {
                    "paragraph_id": paragraph_id,
                    "line_ids": line_ids,
                    "heading_id": heading_id,
                    "zone_type": zone_type,
                    "column_id": column_id,
                    "confidence": _safe_prob(row.get("confidence"), default=0.0),
                }
            )

        toc_tree = _normalize_toc_tree(payload.get("toc_tree"), depth=0, heading_stack=[])

        def _walk_toc(rows: Sequence[Dict[str, Any]], *, heading_id: str = "") -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                node_type = str(row.get("type") or "")
                node_id = str(row.get("node_id") or "")
                line_ids = [str(item).strip() for item in list(row.get("line_ids") or []) if str(item).strip()]
                current_heading = heading_id
                if node_type in {"section", "heading"} and node_id:
                    current_heading = node_id
                out.append(
                    {
                        "node_id": node_id,
                        "type": node_type,
                        "line_ids": line_ids,
                        "heading_id": current_heading,
                        "title": self._normalize_spaces(str(row.get("title") or "")),
                        "zone_type": _normalize_zone(row.get("zone_type")),
                        "column_id": _normalize_column(row.get("column_id")),
                        "confidence": _safe_prob(row.get("confidence"), default=0.0),
                    }
                )
                out.extend(_walk_toc(list(row.get("children") or []), heading_id=current_heading))
            return out

        toc_flat = _walk_toc(toc_tree, heading_id="")

        if toc_flat and not heading_groups:
            for item in toc_flat:
                node_type = str(item.get("type") or "")
                line_ids = list(item.get("line_ids") or [])
                if node_type not in {"section", "heading"} or not line_ids:
                    continue
                heading_groups.append(
                    {
                        "heading_id": str(item.get("node_id") or ""),
                        "line_ids": line_ids,
                        "title": str(item.get("title") or ""),
                        "level": 1,
                        "confidence": float(item.get("confidence") or 0.0),
                    }
                )

        if toc_flat and not paragraph_groups:
            para_seq = 0
            for item in toc_flat:
                node_type = str(item.get("type") or "")
                if node_type not in {"paragraph", "caption", "context"}:
                    continue
                line_ids = list(item.get("line_ids") or [])
                if not line_ids:
                    continue
                para_seq += 1
                paragraph_groups.append(
                    {
                        "paragraph_id": str(item.get("node_id") or f"p{para_seq}"),
                        "line_ids": line_ids,
                        "heading_id": str(item.get("heading_id") or "")[:40],
                        "zone_type": _normalize_zone(item.get("zone_type")),
                        "column_id": _normalize_column(item.get("column_id")),
                        "confidence": _safe_prob(item.get("confidence"), default=0.0),
                    }
                )

        figure_groups: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(payload.get("figure_groups") or [])[:80], start=1):
            if not isinstance(row, dict):
                continue
            line_ids = _normalized_line_ids(row.get("line_ids"), limit=24)
            caption_line_ids = _normalized_line_ids(row.get("caption_line_ids"), limit=24)
            if not line_ids and not caption_line_ids:
                continue
            figure_id = self._normalize_spaces(str(row.get("figure_id") or "")).lower()[:48] or f"f{idx}"
            related_heading_id = self._normalize_spaces(str(row.get("related_heading_id") or "")).lower()[:40]
            if related_heading_id and related_heading_id not in used_heading_ids:
                related_heading_id = ""
            figure_groups.append(
                {
                    "figure_id": figure_id,
                    "line_ids": line_ids,
                    "caption_line_ids": caption_line_ids,
                    "related_heading_id": related_heading_id,
                    "confidence": _safe_prob(row.get("confidence"), default=0.0),
                }
            )

        if toc_flat and not figure_groups:
            fig_seq = 0
            for item in toc_flat:
                node_type = str(item.get("type") or "")
                if node_type != "figure":
                    continue
                fig_seq += 1
                line_ids = list(item.get("line_ids") or [])
                if not line_ids:
                    continue
                figure_groups.append(
                    {
                        "figure_id": str(item.get("node_id") or f"f{fig_seq}"),
                        "line_ids": line_ids,
                        "caption_line_ids": [],
                        "related_heading_id": str(item.get("heading_id") or "")[:40],
                        "confidence": _safe_prob(item.get("confidence"), default=0.0),
                    }
                )

        block_groups: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(payload.get("block_groups") or [])[:320], start=1):
            if not isinstance(row, dict):
                continue
            block_id = self._normalize_spaces(str(row.get("block_id") or "")).lower()[:64] or f"blk_{idx:03d}"
            kind = str(row.get("kind") or row.get("block_kind") or "unknown").strip().lower()
            if kind not in allowed_block_kinds:
                kind = "unknown"
            line_ids = _normalized_line_ids(row.get("line_ids"), limit=120)
            word_ids = _normalized_word_ids(row.get("word_ids"), limit=240)
            char_ranges = _normalized_char_ranges(row.get("char_ranges"), limit=180)
            if not char_ranges and list(row.get("char_ids") or []):
                char_ids = [_resolve_char_id(item) for item in list(row.get("char_ids") or [])[:500]]
                char_ids = [item for item in char_ids if item]
                if char_ids:
                    start_id = char_ids[0]
                    end_id = char_ids[-1]
                    if (
                        (not char_order_map)
                        or char_order_map.get(start_id, -1) <= char_order_map.get(end_id, -1)
                    ):
                        char_ranges = [{"start_char_id": start_id, "end_char_id": end_id}]
            if not line_ids and not word_ids and not char_ranges:
                continue
            parent_node_id = self._normalize_spaces(str(row.get("parent_node_id") or row.get("heading_id") or "")).lower()[:64]
            if parent_node_id and parent_node_id not in used_heading_ids:
                parent_node_id = ""
            try:
                reading_order = int(row.get("reading_order") or idx)
            except Exception:
                reading_order = idx
            block_groups.append(
                {
                    "block_id": block_id,
                    "kind": kind,
                    "title": self._normalize_spaces(str(row.get("title") or ""))[:220],
                    "parent_node_id": parent_node_id,
                    "line_ids": line_ids,
                    "word_ids": word_ids,
                    "char_ranges": char_ranges,
                    "zone_type": _normalize_zone(row.get("zone_type") or "main_body"),
                    "column_id": _normalize_column(row.get("column_id") or "unknown"),
                    "reading_order": max(1, min(20000, reading_order)),
                    "confidence": _safe_prob(row.get("confidence"), default=0.0),
                }
            )

        if block_groups and not heading_groups:
            head_seq = 0
            for row in block_groups:
                if str(row.get("kind") or "") != "heading":
                    continue
                line_ids = list(row.get("line_ids") or [])
                if not line_ids:
                    continue
                head_seq += 1
                heading_id = self._normalize_spaces(str(row.get("block_id") or f"h{head_seq}")).lower()[:40]
                used_heading_ids.add(heading_id)
                heading_groups.append(
                    {
                        "heading_id": heading_id,
                        "line_ids": line_ids,
                        "title": self._normalize_spaces(str(row.get("title") or ""))[:200],
                        "level": 1,
                        "confidence": _safe_prob(row.get("confidence"), default=0.0),
                    }
                )

        if block_groups and not paragraph_groups:
            para_seq = 0
            for row in block_groups:
                if str(row.get("kind") or "") not in {"paragraph", "list_item", "caption", "context"}:
                    continue
                line_ids = list(row.get("line_ids") or [])
                if not line_ids:
                    continue
                para_seq += 1
                parent_node_id = self._normalize_spaces(str(row.get("parent_node_id") or "")).lower()[:40]
                if parent_node_id and parent_node_id not in used_heading_ids:
                    parent_node_id = ""
                paragraph_groups.append(
                    {
                        "paragraph_id": self._normalize_spaces(str(row.get("block_id") or f"p{para_seq}")).lower()[:48],
                        "line_ids": line_ids,
                        "heading_id": parent_node_id,
                        "zone_type": _normalize_zone(row.get("zone_type") or "main_body"),
                        "column_id": _normalize_column(row.get("column_id") or "unknown"),
                        "confidence": _safe_prob(row.get("confidence"), default=0.0),
                    }
                )

        counts = {
            "heading_count": int(len(heading_groups)),
            "paragraph_count": int(len(paragraph_groups)),
            "figure_count": int(len(figure_groups)),
            "block_count": int(len(block_groups)),
        }
        raw_counts = payload.get("counts")
        if isinstance(raw_counts, dict):
            for key in ("heading_count", "paragraph_count", "figure_count", "block_count"):
                try:
                    value = int(raw_counts.get(key))
                except Exception:
                    value = counts[key]
                counts[key] = max(0, min(500, value))

        if line_index and not line_labels:
            line_labels = [
                {
                    "line_id": str(item.get("line_id") or ""),
                    "zone_type": str(item.get("zone_type") or "unknown"),
                    "column_id": str(item.get("column_id") or "unknown"),
                    "paragraph_break_after": False,
                    "heading_prob": 0.0,
                }
                for item in line_index
                if str(item.get("line_id") or "")
            ]

        if not line_labels and not heading_groups and not paragraph_groups and not figure_groups and not block_groups and not toc_tree:
            return None
        return {
            "line_labels": line_labels,
            "line_index": line_index,
            "doc_nav_tree": toc_tree,
            "toc_tree": toc_tree,
            "heading_groups": heading_groups,
            "paragraph_groups": paragraph_groups,
            "figure_groups": figure_groups,
            "block_groups": block_groups,
            "counts": counts,
            "notes": [
                self._normalize_spaces(str(item or ""))[:180]
                for item in list(payload.get("notes") or [])[:18]
                if self._normalize_spaces(str(item or ""))
            ],
        }

    def validate_mm_layout_json(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Validate multimodal layout JSON with strict whitelist fields."""
        if not isinstance(payload, dict):
            return None

        headings_raw = payload.get("headings")
        zones_raw = payload.get("zones")
        toc_raw = payload.get("toc_candidates")
        notes_raw = payload.get("notes")
        if not isinstance(headings_raw, list) or not isinstance(zones_raw, list):
            return None

        headings: List[Dict[str, Any]] = []
        for row in headings_raw[:80]:
            if not isinstance(row, dict):
                continue
            line_id = self._normalize_line_id(row.get("line_id"))
            if not line_id:
                continue
            try:
                heading_prob = float(row.get("heading_prob", 0.0))
                level = int(row.get("level", 1))
            except Exception:
                continue
            headings.append(
                {
                    "line_id": line_id,
                    "heading_prob": max(0.0, min(1.0, heading_prob)),
                    "level": max(1, min(4, level)),
                }
            )

        zones: List[Dict[str, Any]] = []
        for row in zones_raw[:220]:
            if not isinstance(row, dict):
                continue
            line_id = self._normalize_line_id(row.get("line_id"))
            if not line_id:
                continue
            zone_type = str(row.get("zone_type") or "main_body")
            if zone_type not in {"main_body", "side_context", "figure_meta"}:
                continue
            column_id = str(row.get("column_id") or "main")
            zones.append(
                {
                    "line_id": line_id,
                    "zone_type": zone_type,
                    "column_id": column_id[:32],
                }
            )

        toc_candidates: List[str] = []
        if isinstance(toc_raw, list):
            for row in toc_raw[:80]:
                line_id = self._normalize_line_id(row)
                if not line_id:
                    continue
                toc_candidates.append(line_id)

        notes: List[str] = []
        if isinstance(notes_raw, list):
            for row in notes_raw[:24]:
                notes.append(self._normalize_spaces(str(row or ""))[:180])

        continuation_raw = payload.get("page_continuation")
        continuation = {
            "from_prev": False,
            "to_next": False,
            "continuation_confidence": 0.0,
            "notes": [],
        }
        if isinstance(continuation_raw, dict):
            try:
                confidence = float(continuation_raw.get("continuation_confidence") or 0.0)
            except Exception:
                confidence = 0.0
            continuation = {
                "from_prev": bool(continuation_raw.get("from_prev")),
                "to_next": bool(continuation_raw.get("to_next")),
                "continuation_confidence": max(
                    0.0,
                    min(1.0, confidence),
                ),
                "notes": [
                    self._normalize_spaces(str(item or ""))[:160]
                    for item in list(continuation_raw.get("notes") or [])[:6]
                    if self._normalize_spaces(str(item or ""))
                ],
            }

        ui_suggestions: List[Dict[str, Any]] = []
        allowed_kinds = {"split_paragraph", "continue_from_prev", "defer_to_next", "promote_figure", "suppress_noise"}
        for row in list(payload.get("ui_suggestions") or [])[:18]:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "").strip()
            if kind not in allowed_kinds:
                continue
            target_block_ids = [
                str(item).strip()
                for item in list(row.get("target_block_ids") or [])[:6]
                if str(item).strip()
            ]
            ui_suggestions.append(
                {
                    "kind": kind,
                    "target_block_ids": target_block_ids,
                    "reason": self._normalize_spaces(str(row.get("reason") or ""))[:180],
                }
            )

        return {
            "headings": headings,
            "zones": zones,
            "toc_candidates": toc_candidates,
            "notes": notes,
            "page_continuation": continuation,
            "ui_suggestions": ui_suggestions,
        }

    def merge_mm_decision_into_blocks(
        self,
        *,
        base_payload: Dict[str, Any],
        mm_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge multimodal layout decisions back into extracted blocks."""
        payload = json.loads(json.dumps(base_payload, ensure_ascii=False))
        raw_text = str(payload.get("raw_text") or "")
        style_cues = dict(payload.get("style_cues") or {})
        line_layout = list(style_cues.get("line_layout") or [])
        blocks = list(payload.get("blocks") or [])

        line_rows: List[Dict[str, Any]] = []
        for idx, row in enumerate(line_layout):
            if not isinstance(row, dict):
                continue
            line_id = self._normalize_line_id(row.get("line_id"))
            if not line_id:
                line_id = str(idx)
            item = dict(row)
            item["line_id"] = line_id
            line_rows.append(item)

        zone_map = {
            self._normalize_line_id(row.get("line_id")): {
                "zone_type": str(row.get("zone_type") or "main_body"),
                "column_id": str(row.get("column_id") or "main"),
            }
            for row in list(mm_decision.get("zones") or [])
            if isinstance(row, dict) and self._normalize_line_id(row.get("line_id"))
        }
        heading_map = {
            self._normalize_line_id(row.get("line_id")): {
                "heading_prob": self._safe_float(row.get("heading_prob"), 0.0),
                "level": self._safe_int(row.get("level"), 1),
            }
            for row in list(mm_decision.get("headings") or [])
            if isinstance(row, dict) and self._normalize_line_id(row.get("line_id"))
        }
        toc_line_ids = {
            line_id
            for line_id in (
                self._normalize_line_id(item)
                for item in list(mm_decision.get("toc_candidates") or [])
            )
            if line_id
        }

        consumed_line_ids: set[str] = set()
        merged_blocks: List[Dict[str, Any]] = []

        for raw_block in blocks:
            if not isinstance(raw_block, dict):
                continue
            block = dict(raw_block)
            text = self._normalize_spaces(str(block.get("text") or ""))
            kind = str(block.get("kind") or "paragraph")
            if not text:
                continue

            line_id, match_score, row = self._match_line_for_block(text=text, line_rows=line_rows)
            if line_id is not None:
                consumed_line_ids.add(line_id)

            default_zone = "figure_meta" if kind == "caption" else "main_body"
            default_column = str((row or {}).get("column_label") or "main")
            if default_column.startswith("sidebar"):
                default_zone = "side_context"

            zone = zone_map.get(line_id) if line_id is not None else None
            zone_type = str((zone or {}).get("zone_type") or default_zone)
            if zone_type not in {"main_body", "side_context", "figure_meta"}:
                zone_type = default_zone
            column_id = str((zone or {}).get("column_id") or default_column or "main")
            if zone_type == "side_context" and column_id == "main":
                column_id = "sidebar_auto"

            heading_prob = 0.0
            if kind == "heading":
                heading = heading_map.get(line_id) if line_id is not None else None
                heading_prob = float((heading or {}).get("heading_prob") or 0.0)
                if heading_prob <= 0.0:
                    heading_prob = 0.75 if self._looks_like_heading_text(text) else 0.35
            block["zone_type"] = zone_type
            block["column_id"] = column_id[:32]
            block["heading_prob"] = round(max(0.0, min(1.0, heading_prob)), 4)
            block["layout_confidence"] = round(max(0.0, min(1.0, match_score)), 4)
            if kind == "heading" and line_id is not None and line_id in toc_line_ids:
                block["toc_candidate"] = True
            merged_blocks.append(block)

        side_context_blocks: List[Dict[str, Any]] = [
            item for item in merged_blocks if str(item.get("zone_type") or "") == "side_context"
        ]
        figure_meta_blocks: List[Dict[str, Any]] = [
            item for item in merged_blocks if str(item.get("zone_type") or "") == "figure_meta"
        ]

        for idx, row in enumerate(line_rows):
            line_id = self._normalize_line_id(row.get("line_id")) or str(idx)
            if line_id in consumed_line_ids:
                continue
            column_label = str(row.get("column_label") or "main")
            zone = zone_map.get(line_id)
            zone_type = str((zone or {}).get("zone_type") or "")
            if not (column_label.startswith("sidebar") or zone_type == "side_context"):
                continue
            text = self._normalize_spaces(str(row.get("text") or ""))
            if not text or len(text) < 4:
                continue
            anchor = self._find_anchor_in_raw_text(
                raw_text=raw_text,
                text=text,
                page=int(payload.get("page") or 1),
            )
            side_context_blocks.append(
                {
                    "id": f"side_line_{line_id}",
                    "kind": "paragraph",
                    "text": text,
                    "order": 10000 + idx,
                    "section_title": "Side Context",
                    "source_anchor": anchor,
                    "zone_type": "side_context",
                    "column_id": str((zone or {}).get("column_id") or column_label),
                    "heading_prob": 0.0,
                    "layout_confidence": 0.78,
                }
            )

        heading_blocks = [
            item
            for item in merged_blocks
            if str(item.get("kind") or "") == "heading" and str(item.get("zone_type") or "") == "main_body"
        ]
        high_conf_headings = [
            item
            for item in heading_blocks
            if float(item.get("heading_prob") or 0.0) >= 0.72
        ]
        toc_quality = len(high_conf_headings) / max(1, len(heading_blocks))
        toc_hidden = bool(toc_quality < 0.55)

        expected_sidebar_count = sum(
            1 for row in line_rows if str(row.get("column_label") or "").startswith("sidebar")
        )
        sidebar_recall = len(side_context_blocks) / max(1, expected_sidebar_count) if expected_sidebar_count else 1.0
        cross_column_merge_ratio = self._estimate_cross_column_merge_ratio(
            blocks=merged_blocks,
            style_cues=style_cues,
        )

        payload["blocks"] = merged_blocks
        payload["side_context_blocks"] = side_context_blocks[:60]
        payload["figure_meta_blocks"] = figure_meta_blocks[:40]
        payload["layout_channels"] = {
            "main_body": [
                str(item.get("id") or "")
                for item in merged_blocks
                if str(item.get("zone_type") or "") == "main_body"
            ],
            "side_context": [str(item.get("id") or "") for item in side_context_blocks if item.get("id")],
            "figure_meta": [str(item.get("id") or "") for item in figure_meta_blocks if item.get("id")],
        }
        payload["toc_candidates"] = [
            {
                "title": self._normalize_spaces(str(item.get("text") or "")),
                "level": int(item.get("level") or 1),
                "source_anchor": item.get("source_anchor"),
            }
            for item in high_conf_headings[:24]
        ]
        payload["toc_quality"] = round(max(0.0, min(1.0, toc_quality)), 4)
        payload["toc_hidden"] = toc_hidden
        payload["cross_column_merge_ratio"] = round(max(0.0, min(1.0, cross_column_merge_ratio)), 4)
        payload["sidebar_recall"] = round(max(0.0, min(1.0, sidebar_recall)), 4)
        payload["mm_layout_notes"] = [self._normalize_spaces(str(item or ""))[:180] for item in list(mm_decision.get("notes") or []) if self._normalize_spaces(str(item or ""))][:24]
        payload["mm_page_judgement"] = dict(mm_decision.get("page_continuation") or {})
        payload["mm_ui_suggestions"] = list(mm_decision.get("ui_suggestions") or [])[:18]
        payload["mm_layout_structured"] = {
            "headings": list(mm_decision.get("headings") or [])[:80],
            "zones": list(mm_decision.get("zones") or [])[:220],
            "toc_candidates": list(mm_decision.get("toc_candidates") or [])[:80],
            "notes": list(payload.get("mm_layout_notes") or []),
            "page_continuation": dict(payload.get("mm_page_judgement") or {}),
            "ui_suggestions": list(payload.get("mm_ui_suggestions") or []),
        }
        return payload

    def mark_mm_triggered(self, *, paper_id: int, page: int) -> None:
        state = self._doc_stats.setdefault(
            int(paper_id),
            {
                "seen_pages": set(),
                "triggered_pages": set(),
                "updated_at": time.time(),
            },
        )
        seen_pages = state.get("seen_pages")
        if not isinstance(seen_pages, set):
            seen_pages = set()
            state["seen_pages"] = seen_pages
        triggered_pages = state.get("triggered_pages")
        if not isinstance(triggered_pages, set):
            triggered_pages = set()
            state["triggered_pages"] = triggered_pages
        seen_pages.add(int(page))
        triggered_pages.add(int(page))
        state["updated_at"] = time.time()

    async def _call_mm_model(
        self,
        *,
        model: str,
        prompt_payload: Dict[str, Any],
        timeout_ms: int,
        prompt_kind: str = "layout_judge_v1",
    ) -> Optional[Dict[str, Any]]:
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        if not api_key or not base_url:
            return None

        if str(prompt_kind) == "layout_plan_v2":
            user_prompt = self._build_layout_plan_v2_prompt_text(prompt_payload)
        elif str(prompt_kind) == "stage1_semantic_v2":
            user_prompt = self._build_stage1_semantic_prompt_text(prompt_payload)
        elif str(prompt_kind) == "stage2_design_v2":
            user_prompt = self._build_stage2_design_slots_prompt_text(prompt_payload)
        elif str(prompt_kind) == "stage1_structural_v1":
            user_prompt = self._build_stage1_structural_prompt_text(prompt_payload)
        elif str(prompt_kind) == "stage2_design_v1":
            user_prompt = self._build_stage2_design_prompt_text(prompt_payload)
        elif str(prompt_kind) == "line_parse_advice_v1":
            user_prompt = self._build_line_parse_advice_prompt_text(prompt_payload)
        else:
            user_prompt = self._build_prompt_text(prompt_payload)
        image_rows = list(prompt_payload.get("images") or [])
        image_data_url = str(prompt_payload.get("image_data_url") or "")
        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        model_name = str(model or "").strip().lower()
        use_images_for_prompt = True
        if str(prompt_kind) == "stage1_structural_v1":
            # Structural stage keeps image out of band; it only annotates existing layout IDs.
            use_images_for_prompt = False
        elif str(prompt_kind) == "line_parse_advice_v1" and "vl" not in model_name:
            # Fallback text model: use text-only parse prompt to improve robustness and latency.
            use_images_for_prompt = False
        if use_images_for_prompt and image_rows:
            selected_rows = list(image_rows[:3])
            if str(prompt_kind) == "line_parse_advice_v1":
                current_rows = [
                    row
                    for row in image_rows
                    if isinstance(row, dict) and str((row or {}).get("scope") or "").strip().lower() == "current"
                ]
                selected_rows = current_rows[:1] if current_rows else list(image_rows[:1])
            for row in selected_rows:
                image_url = str((row or {}).get("image_data_url") or "").strip()
                if not image_url:
                    continue
                content_parts.append({"type": "image_url", "image_url": {"url": image_url}})
        elif use_images_for_prompt and image_data_url:
            content_parts.append({"type": "image_url", "image_url": {"url": image_data_url}})
        if len(content_parts) <= 1 and str(prompt_kind) == "layout_judge_v1":
            return None

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        source = f"reader.multimodal_layout.{prompt_kind}"
        if str(prompt_kind) in {"line_parse_advice_v1", "stage1_structural_v1", "stage1_semantic_v2"}:
            max_tokens = max(1200, int(getattr(settings, "reader_mm_parser_max_tokens", 4200) or 4200))
        else:
            max_tokens = max(900, int(getattr(settings, "reader_mm_max_tokens", 2200) or 2200))
        request_timeout = max(2.0, float(timeout_ms) / 1000.0)
        extra_headers = build_llm_source_headers(source)
        log_tagged_llm_request_start(
            source=source,
            provider="aliyun",
            model=model,
            operation="chat",
        )
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a paper layout structure judge. "
                                "Output JSON only. Do not rewrite body content."
                            ),
                        },
                        {
                            "role": "user",
                            "content": content_parts,
                        },
                    ],
                    temperature=0,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    timeout=request_timeout,
                    extra_headers=extra_headers or None,
                ),
                timeout=request_timeout + 1.0,
            )
        except Exception as exc:  # pragma: no cover - network failures are acceptable
            log_tagged_llm_request_error(
                source=source,
                provider="aliyun",
                model=model,
                operation="chat",
                error=f"{type(exc).__name__}: {exc!r}",
            )
            logger.warning(
                f"[ReaderMM] model call failed model={model}, prompt_kind={prompt_kind}: {type(exc).__name__}: {exc!r}"
            )
            return None
        usage_obj = getattr(resp, "usage", None)
        log_tagged_llm_request_done(
            source=source,
            provider="aliyun",
            model=str(getattr(resp, "model", "") or model),
            operation="chat",
            finish_reason=str(getattr((getattr(resp, "choices", None) or [None])[0], "finish_reason", "") or ""),
            usage={
                "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            },
        )

        content = ""
        finish_reason = ""
        try:
            content = str((resp.choices[0].message.content or "")).strip()
            finish_reason = str(resp.choices[0].finish_reason or "").strip().lower()
        except Exception:
            return None
        if not content:
            return None
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except Exception:
            recovered = self._extract_json_dict_from_text(content)
            if isinstance(recovered, dict):
                return recovered
            if finish_reason == "length":
                logger.warning(f"[ReaderMM] output truncated model={model}, prompt_kind={prompt_kind}, max_tokens={max_tokens}")
            else:
                logger.warning(f"[ReaderMM] non-json output model={model}, prompt_kind={prompt_kind}, preview={content[:220]!r}")
            return None

    @staticmethod
    def _extract_json_dict_from_text(text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = raw[start : end + 1]
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _build_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        lines = list(prompt_payload.get("line_candidates") or [])
        summary = dict(prompt_payload.get("layout_summary") or {})
        block_candidates = list(prompt_payload.get("block_candidates") or [])
        meta = dict(prompt_payload.get("layout_meta") or {})
        image_meta = [
            {
                "scope": str(item.get("scope") or ""),
                "page": self._safe_int(item.get("page"), 0),
            }
            for item in list(prompt_payload.get("images") or [])[:3]
            if isinstance(item, dict)
        ]
        schema_text = (
            '{'
            '"headings":[{"line_id":12,"heading_prob":0.91,"level":1}],'
            '"zones":[{"line_id":12,"zone_type":"main_body","column_id":"left"}],'
            '"toc_candidates":[12,18],'
            '"notes":["optional"],'
            '"page_continuation":{"from_prev":true,"to_next":false,"continuation_confidence":0.86,"notes":["tail continuation"]},'
            '"ui_suggestions":[{"kind":"continue_from_prev","target_block_ids":["b4"],"reason":"paragraph starts mid-sentence"}]'
            '}'
        )
        return (
            "任务：结合上一页/当前页/下一页图与候选行，判断当前页标题、栏位、区域归属，并输出续接判断与UI建议。\n"
            "硬约束：\n"
            "1) 只返回 JSON；\n"
            "2) zone_type 仅允许 main_body|side_context|figure_meta；\n"
            "3) 不改写正文，不生成摘要；\n"
            "4) 优先识别侧栏并与正文分离；\n"
            "5) page_continuation 只描述当前页与前后页续接，不给事实结论；\n"
            "6) ui_suggestions 只能引用候选块中的 block_id。\n"
            f"页元信息：{json.dumps(meta, ensure_ascii=False)}\n"
            f"三页图元信息：{json.dumps(image_meta, ensure_ascii=False)}\n"
            f"视觉摘要：{json.dumps(summary, ensure_ascii=False)}\n"
            f"候选行：{json.dumps(lines, ensure_ascii=False)}\n"
            f"候选块：{json.dumps(block_candidates, ensure_ascii=False)}\n"
            f"输出示例：{schema_text}"
        )

    def _build_layout_plan_v2_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        summary = dict(prompt_payload.get("layout_summary") or {})
        meta = dict(prompt_payload.get("layout_meta") or {})
        page_structure_v3 = dict(
            prompt_payload.get("page_structure_v3")
            or prompt_payload.get("parser_advice")
            or {}
        )
        image_meta = [
            {
                "scope": str(item.get("scope") or ""),
                "page": int(item.get("page") or 0),
            }
            for item in list(prompt_payload.get("images") or [])[:1]
            if isinstance(item, dict)
        ]
        valid_block_ids = [
            str(item).strip()
            for item in list(prompt_payload.get("valid_block_ids") or [])
            if str(item).strip()
        ]
        component_whitelist = [
            str(item).strip()
            for item in list(prompt_payload.get("component_whitelist") or [])[:36]
            if str(item).strip()
        ]
        block_groups = [
            row
            for row in list(page_structure_v3.get("block_groups") or [])
            if isinstance(row, dict)
        ]
        compact_structure = {
            "counts": dict(page_structure_v3.get("counts") or {}),
            "relations": [row for row in list(page_structure_v3.get("relations") or [])[:120] if isinstance(row, dict)],
            "doc_nav_tree": [row for row in list(page_structure_v3.get("doc_nav_tree") or [])[:40] if isinstance(row, dict)],
            "block_groups": [
                {
                    "block_id": str(item.get("block_id") or ""),
                    "kind": str(item.get("kind") or "unknown"),
                    "zone_type": str(item.get("zone_type") or "unknown"),
                    "reading_order": self._safe_int(item.get("reading_order"), 0),
                    "parent_node_id": str(item.get("parent_node_id") or ""),
                    "title": self._normalize_spaces(str(item.get("title") or ""))[:200],
                    "text": self._normalize_spaces(str(item.get("text") or ""))[:220],
                    "layout_bbox_or_polygon": dict(item.get("layout_bbox_or_polygon") or {}),
                    "style_summary": dict(item.get("style_summary") or {}),
                }
                for item in block_groups
                if str(item.get("block_id") or "").strip()
            ],
        }
        schema_text = (
            '{'
            '"zones":[{"zone_type":"main_body","block_ids":["blk_001"]}],'
            '"headings":[{"block_id":"blk_001","level":1,"confidence":0.92,"text":"Introduction"}],'
            '"continuation":{"from_prev":["blk_010"],"to_next":[],"confidence":0.81,"reason":"continued sentence"},'
            '"segments":[{"segment_id":"seg_1","kind":"paragraph","component_hint":"ParagraphProse","block_ids":["blk_002"],"title":"","continuation":"none","reason":"main prose"}],'
            '"ui_suggestions":[{"kind":"continue_from_prev","target_block_ids":["blk_010"],"reason":"sentence starts mid-way"}],'
            '"notes":["layout note"]'
            '}'
        )
        return (
            "Task: You are a page UI designer. Build strict JSON layout advice for the current page.\n"
            "Hard constraints:\n"
            "1) Output JSON only.\n"
            "2) Do not rewrite body content.\n"
            "3) segments[].block_ids must come from valid_block_ids.\n"
            "4) component_hint (or ui_component) must come from component_whitelist.\n"
            "5) Exactly one semantic paragraph per ParagraphProse segment; do not merge paragraph starts.\n"
            "6) continuation only describes cross-page continuity.\n"
            "7) Do not invent IDs or coordinates.\n"
            "8) component_hint is advisory only, not a final rendering decision.\n"
            "9) Keep output compact: at most 64 segments, at most 12 ui_suggestions, notes <= 8.\n"
            "10) Do not echo source block text in notes/reason fields.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"layout_summary: {json.dumps(summary, ensure_ascii=False)}\n"
            f"page_structure_v3: {json.dumps(compact_structure, ensure_ascii=False, separators=(',', ':'))}\n"
            f"valid_block_ids: {json.dumps(valid_block_ids, ensure_ascii=False)}\n"
            f"component_whitelist: {json.dumps(component_whitelist, ensure_ascii=False)}\n"
            f"output_schema_example: {schema_text}"
        )

    def _build_stage1_semantic_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        meta = dict(prompt_payload.get("layout_meta") or {})
        retry_hint = self._normalize_spaces(str(prompt_payload.get("retry_hint") or ""))[:600]
        known_atom_ids = [
            str(item).strip()
            for item in list(prompt_payload.get("known_atom_ids") or [])[:2400]
            if str(item).strip()
        ]
        atoms_digest = [
            row for row in list(prompt_payload.get("atoms_digest") or [])[:2400]
            if isinstance(row, dict)
        ]
        image_meta = [
            {"scope": str(item.get("scope") or ""), "page": int(item.get("page") or 0)}
            for item in list(prompt_payload.get("images") or [])[:1]
            if isinstance(item, dict)
        ]
        schema_text = {
            "annotations": [
                {
                    "atom_id": "p1:lA:b1",
                    "role": "paragraph",
                    "importance": "normal",
                    "grouping_hint": "belongs_to_intro",
                    "component_hint": "ParagraphProse",
                    "confidence": 0.92,
                }
            ]
        }
        return (
            "You are Stage1 semantic annotator for deterministic document atoms.\n"
            "Return JSON only.\n"
            "Hard constraints:\n"
            "1) Annotate existing atom IDs only.\n"
            "2) Do not merge/split atoms, do not rewrite text, do not invent IDs.\n"
            "3) Every known atom_id must appear exactly once.\n"
            "4) role must be one of: doc_title,section_title,paragraph,list_item,caption,figure,table,sidebar,metadata,header,footer,noise,unknown.\n"
            "5) confidence in [0,1].\n"
            "6) visual_reference_only=true: current page image may be used only to judge structure/boundaries, never to invent text.\n"
            "6.1) Use the page image to identify obvious OCR/layout noise boundaries such as figure_meta pollution or split caption continuation, but do not change geometry or source ownership.\n"
            "7) Use grouping_hint conservatively to mark atoms that clearly belong to the same semantic paragraph/list/caption unit.\n"
            "8) If bbox/reading_order/indent suggests a new paragraph start, do not reuse the previous grouping_hint.\n"
            "9) When an atom clearly looks like methods/protocol/setup, citation/meta, figure commentary, or a cross-page continuation bridge, use component_hint to reflect that instead of defaulting everything to ParagraphProse.\n"
            "10) Prefer component hints such as MethodologyCard, CitationCard, InsightClusterCard, or SectionBridgeCard only when the atom text itself supports that role.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"visual_reference_only: true\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"known_atom_ids: {json.dumps(known_atom_ids, ensure_ascii=False)}\n"
            f"atoms_digest: {json.dumps(atoms_digest, ensure_ascii=False, separators=(',', ':'))}\n"
            f"retry_hint: {json.dumps(retry_hint, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(schema_text, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    def _build_stage2_design_slots_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        meta = dict(prompt_payload.get("layout_meta") or {})
        retry_hint = self._normalize_spaces(str(prompt_payload.get("retry_hint") or ""))[:600]
        annotations = dict(prompt_payload.get("semantic_annotations") or {})
        atoms_digest = [
            row for row in list(prompt_payload.get("atoms_digest") or [])[:2400]
            if isinstance(row, dict)
        ]
        known_atom_ids = [
            str(item).strip()
            for item in list(prompt_payload.get("known_atom_ids") or [])[:2400]
            if str(item).strip()
        ]
        allowed_components = [
            str(item).strip()
            for item in list(prompt_payload.get("allowed_components") or [])[:96]
            if str(item).strip()
        ]
        image_meta = [
            {"scope": str(item.get("scope") or ""), "page": int(item.get("page") or 0)}
            for item in list(prompt_payload.get("images") or [])[:1]
            if isinstance(item, dict)
        ]
        schema_text = {
            "page_layout_slots": [
                {
                    "slot_id": "slot_001",
                    "component": "ParagraphProse",
                    "atom_ids": ["p1:lA:b2"],
                    "style_tokens": {"tone": "clean"},
                    "layout_tokens": {"region": "main"},
                }
            ],
            "unused_atom_ids": ["p1:lA:b9"],
        }
        return (
            "You are Stage2 design planner for a generative reader page.\n"
            "Return JSON only.\n"
            "Hard constraints:\n"
            "1) Use only semantic_annotations + atoms_digest as source truth.\n"
            "2) Do not invent atom IDs.\n"
            "3) Do not use one atom_id more than once across page_layout_slots.\n"
            "4) Every known atom_id must be accounted for via page_layout_slots.atom_ids or unused_atom_ids.\n"
            "5) component must come from allowed_components.\n"
            "6) Do not output ownership/topology override fields in any nested object.\n"
            "7) visual_reference_only=true: image may guide grouping, but never add missing text.\n"
            "7.1) You may clean obvious OCR/layout noise in display text when it visibly conflicts with the page image, especially chart-label pollution inside figure_meta or split caption continuation.\n"
            "7.2) Cleaning is display-text only: do not modify geometry, bbox, polygon, atom ownership, or source ownership.\n"
            "8) Prefer one semantic paragraph per ParagraphProse slot; do not merge across paragraph starts suggested by grouping_hint, reading_order jumps, vertical gaps, or indent changes.\n"
            "9) Keep captions/figures separate from body paragraphs unless atoms clearly describe the same figure unit.\n"
            "10) Avoid prose-only流水账 layouts when richer structure exists. If 3 or more prose slots would appear in a row, prefer a structure card such as InsightClusterCard, CalloutBox, MethodologyCard, CitationCard, CompareInsightsCard, or SectionBridgeCard.\n"
            "11) Figure + analysis should usually become FigurePanel plus InsightClusterCard or CalloutBox.\n"
            "12) Methods/protocol/setup text should prefer MethodologyCard. Citation-heavy or DOI-heavy text should prefer CitationCard or remain unused for side context. Cross-page continuation should prefer SectionBridgeCard.\n"
            "13) Prefer one of these composition templates when applicable: figure-led analysis, claim plus evidence, methods aside, citation cluster, section bridge.\n"
            "14) Keep the main reading canvas focused on reading flow only. Metadata, DOI links, publication info, citation bundles, quality/debug status, and auxiliary AI assets should go to side context rather than the main canvas.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"visual_reference_only: true\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"semantic_annotations: {json.dumps(annotations, ensure_ascii=False, separators=(',', ':'))}\n"
            f"atoms_digest: {json.dumps(atoms_digest, ensure_ascii=False, separators=(',', ':'))}\n"
            f"known_atom_ids: {json.dumps(known_atom_ids, ensure_ascii=False)}\n"
            f"allowed_components: {json.dumps(allowed_components, ensure_ascii=False)}\n"
            f"retry_hint: {json.dumps(retry_hint, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(schema_text, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    def _build_stage1_structural_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        meta = dict(prompt_payload.get("layout_meta") or {})
        retry_hint = self._normalize_spaces(str(prompt_payload.get("retry_hint") or ""))[:600]
        known_layout_ids = [
            str(item).strip()
            for item in list(prompt_payload.get("known_layout_ids") or [])[:1200]
            if str(item).strip()
        ]
        digest_rows = [
            row for row in list(prompt_payload.get("docmind_layout_digest") or [])[:1200]
            if isinstance(row, dict)
        ]
        digest_compact = [
            {
                "layout_id": str(row.get("layout_id") or "").strip(),
                "reading_order": self._safe_int(row.get("reading_order"), 0),
                "bbox": list(row.get("bbox") or [0.0, 0.0, 0.0, 0.0])[:4],
                "text_preview": self._normalize_spaces(str(row.get("text_preview") or ""))[:120],
            }
            for row in digest_rows
        ]
        image_meta = [
            {"scope": str(item.get("scope") or ""), "page": int(item.get("page") or 0)}
            for item in list(prompt_payload.get("images") or [])[:1]
            if isinstance(item, dict)
        ]
        schema_text = {
            "blocks": [
                {
                    "layout_id": "layout_001",
                    "role": "paragraph",
                    "section_id": "sec_intro",
                    "column": 0,
                    "confidence": 0.92,
                }
            ],
            "sections": [
                {
                    "section_id": "sec_intro",
                    "title_layout_id": "layout_000",
                    "children": ["layout_001"],
                }
            ],
        }
        return (
            "You are Stage1 structural annotator for PDF layouts.\n"
            "Return JSON only.\n"
            "Hard constraints:\n"
            "1) Annotate existing layout IDs only.\n"
            "2) Do not merge/split blocks.\n"
            "3) Do not rewrite or generate text.\n"
            "4) Do not invent layout_id.\n"
            "5) Every provided layout_id must appear exactly once in blocks.\n"
            "6) role must be one of: doc_title,section_title,paragraph,list_item,caption,figure,table,sidebar,metadata,header,footer,noise,unknown.\n"
            "6.1) role must be lowercase snake_case exact token; never output Chinese labels or camelCase.\n"
            "7) confidence in [0,1], column must be integer >=0.\n"
            "8) sections must reference valid layout_id only.\n"
            "9) visual_reference_only=true: image is optional reference only, never authoritative for structure truth.\n"
            "9.1) Image may be used to notice obvious OCR/layout pollution, but geometry and ownership must still come from the original layout provenance.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"visual_reference_only: true\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"known_layout_ids: {json.dumps(known_layout_ids, ensure_ascii=False)}\n"
            f"docmind_layout_digest: {json.dumps(digest_compact, ensure_ascii=False, separators=(',', ':'))}\n"
            f"retry_hint: {json.dumps(retry_hint, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(schema_text, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    def _build_stage2_design_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        meta = dict(prompt_payload.get("layout_meta") or {})
        retry_hint = self._normalize_spaces(str(prompt_payload.get("retry_hint") or ""))[:600]
        annotations = dict(prompt_payload.get("structural_annotations") or {})
        layout_digest = [
            row for row in list(prompt_payload.get("layout_digest") or [])[:1200]
            if isinstance(row, dict)
        ]
        known_layout_ids = [
            str(item).strip()
            for item in list(prompt_payload.get("known_layout_ids") or [])[:1200]
            if str(item).strip()
        ]
        allowed_components = [
            str(item).strip()
            for item in list(prompt_payload.get("allowed_components") or [])[:64]
            if str(item).strip()
        ]
        image_meta = [
            {"scope": str(item.get("scope") or ""), "page": int(item.get("page") or 0)}
            for item in list(prompt_payload.get("images") or [])[:1]
            if isinstance(item, dict)
        ]
        schema_text = {
            "page_layout": [
                {
                    "component": "ParagraphProse",
                    "source_layout_ids": ["layout_001"],
                    "props": {},
                }
            ],
            "unused_layout_ids": ["layout_099"],
        }
        return (
            "You are Stage2 design layout planner.\n"
            "Return JSON only.\n"
            "Hard constraints:\n"
            "1) Use only structural_annotations + layout_digest as input truth.\n"
            "2) Do not invent layout IDs.\n"
            "3) Do not use one layout_id more than once across page_layout.\n"
            "4) Every known layout_id must be accounted for in exactly one of: used or unused.\n"
            "5) component must be in allowed_components.\n"
            "6) Do not rewrite scientific facts.\n"
            "7) visual_reference_only=true: image is optional reference only.\n"
            "7.1) You may clean obvious OCR/layout noise in display text when it visibly conflicts with the page image, especially chart-label pollution or split caption continuation.\n"
            "7.2) Cleaning is display-text only: do not modify geometry, bbox, polygon, layout ownership, or source ownership.\n"
            "8) Avoid pure prose stacks when the page contains figure/caption/method/citation/transition structure.\n"
            "9) If 3 or more prose-like segments would run consecutively, prefer CalloutBox, InsightClusterCard, MethodologyCard, CitationCard, CompareInsightsCard, or SectionBridgeCard when justified by the source layouts.\n"
            "10) Figure commentary should often pair FigurePanel with InsightClusterCard or CalloutBox; continuation fragments should prefer SectionBridgeCard over burying the text inside long prose.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"visual_reference_only: true\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"structural_annotations: {json.dumps(annotations, ensure_ascii=False, separators=(',', ':'))}\n"
            f"layout_digest: {json.dumps(layout_digest, ensure_ascii=False, separators=(',', ':'))}\n"
            f"known_layout_ids: {json.dumps(known_layout_ids, ensure_ascii=False)}\n"
            f"allowed_components: {json.dumps(allowed_components, ensure_ascii=False)}\n"
            f"retry_hint: {json.dumps(retry_hint, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(schema_text, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    def _build_line_parse_advice_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        summary = dict(prompt_payload.get("layout_summary") or {})
        meta = dict(prompt_payload.get("layout_meta") or {})
        retry_hint = self._normalize_spaces(str(prompt_payload.get("retry_hint") or ""))[:600]
        image_meta = [
            {
                "scope": str(item.get("scope") or ""),
                "page": int(item.get("page") or 0),
            }
            for item in list(prompt_payload.get("images") or [])[:3]
            if isinstance(item, dict)
        ]
        source_text_full = str(prompt_payload.get("source_text_full") or "")
        source_checksum = str(prompt_payload.get("source_checksum") or "").strip()
        image_placeholders = [
            {
                "token": str(item.get("token") or ""),
                "image_id": str(item.get("image_id") or ""),
                "description": str(item.get("description") or ""),
                "bbox": dict(item.get("bbox") or {}),
            }
            for item in list(prompt_payload.get("image_placeholders") or [])[:64]
            if isinstance(item, dict)
        ]
        schema_text = (
            '{'
            '"page":2,'
            '"source_checksum":"sha256...",'
            '"blocks":[{"block_id":"b1","kind":"heading","zone_type":"main_body","parent_block_id":null,'
            '"reading_order":1,"text":"Introduction","spans":[{"start":12,"end":88}],"tags":["section_title"],"confidence":0.95}],'
            '"relations":[{"type":"belongs_to_heading","from":"b2","to":"b1","confidence":0.9}],'
            '"doc_nav_tree":[{"node_id":"n1","title_block_id":"b1","level":1,"children":[],"content_block_ids":["b2"]}],'
            '"counts":{"heading_count":1,"paragraph_count":1,"figure_count":0,"table_count":0,"block_count":2,"relation_count":1},'
            '"notes":[]'
            '}'
        )
        return (
            "You are a PDF page structure labeler. Output JSON only.\n"
            "Task: classify and label source_text_full into ordered blocks with relationships.\n"
            "Hard constraints:\n"
            "1) DO NOT add, remove, or rewrite any source text.\n"
            "2) blocks[*].text must be exact text snippets from source_text_full (normalized whitespace allowed).\n"
            "3) You may include spans [start,end) when confident, but never fabricate offsets.\n"
            "4) For non-whitespace characters in source_text_full: block texts must provide complete coverage with no overlap in meaning.\n"
            "5) heading may merge multi-line title if it is one logical title.\n"
            "6) Do not merge two semantic paragraphs into one paragraph block.\n"
            "7) figure/caption/side context must be explicitly labeled via kind + zone_type.\n"
            "8) Valid kind: heading|paragraph|list_item|caption|figure_meta|table_caption|unknown.\n"
            "9) Valid zone_type: main_body|side_context|figure_meta|unknown.\n"
            "10) If uncertain, split conservatively and use kind=unknown.\n"
            "11) Relations should capture hierarchy and associations (belongs_to_heading/caption_of/table_caption_of/continues_from/references_figure).\n"
            "12) Do not emit unknown top-level fields.\n"
            f"layout_meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"image_meta: {json.dumps(image_meta, ensure_ascii=False)}\n"
            f"layout_summary: {json.dumps(summary, ensure_ascii=False)}\n"
            f"source_checksum: {json.dumps(source_checksum, ensure_ascii=False)}\n"
            f"image_placeholders: {json.dumps(image_placeholders, ensure_ascii=False)}\n"
            f"source_text_full: {json.dumps(source_text_full, ensure_ascii=False)}\n"
            f"retry_hint: {json.dumps(retry_hint, ensure_ascii=False)}\n"
            f"output_schema_example: {schema_text}\n"
            "Return JSON now."
        )

    @staticmethod
    def _summarize_line_candidates(candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        column_distribution: Dict[str, int] = {}
        font_sizes: List[float] = []
        bold_line_count = 0
        for row in candidates:
            if not isinstance(row, dict):
                continue
            column = str(row.get("column_label") or "main")
            column_distribution[column] = int(column_distribution.get(column, 0) + 1)
            font_size = float(row.get("font_size") or 0.0)
            if font_size > 0:
                font_sizes.append(font_size)
            if float(row.get("bold_ratio") or 0.0) >= 0.45:
                bold_line_count += 1
        avg_font = sum(font_sizes) / len(font_sizes) if font_sizes else 0.0
        return {
            "line_count": int(len(candidates)),
            "column_distribution": column_distribution,
            "avg_font_size": round(float(avg_font), 3),
            "max_font_size": round(float(max(font_sizes) if font_sizes else 0.0), 3),
            "bold_line_ratio": round(float(bold_line_count / max(1, len(candidates))), 4),
        }

    def _build_block_candidates(
        self,
        *,
        base_payload: Dict[str, Any],
        page: int,
    ) -> List[Dict[str, Any]]:
        blocks = list(base_payload.get("blocks") or [])
        block_limit = max(24, int(getattr(settings, "reader_mm_block_candidate_limit", 72) or 72))
        output: List[Dict[str, Any]] = []
        for idx, row in enumerate(blocks[:block_limit]):
            if not isinstance(row, dict):
                continue
            text = self._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            output.append(
                {
                    "block_id": str(row.get("id") or f"b{idx + 1}"),
                    "kind": str(row.get("kind") or "paragraph"),
                    "zone_type": str(row.get("zone_type") or "main_body"),
                    "column_id": str(row.get("column_id") or "main"),
                    "heading_prob": round(float(row.get("heading_prob") or 0.0), 4),
                    "page": self._safe_int(row.get("page"), self._safe_int(page, 1)),
                    "text": text[:220],
                    "has_anchor": isinstance(row.get("source_anchor"), dict),
                }
            )
        return output

    def _extract_native_pdf_page_data(self, *, pdf_path: str, page: int) -> Dict[str, Any]:
        """Extract page-native raw objects from pdfplumber without semantic grouping."""
        output: Dict[str, Any] = {
            "page_meta": {
                "page": int(page),
                "page_width": 0.0,
                "page_height": 0.0,
                "rotation": 0,
            },
            "words": [],
            "chars": [],
            "images": [],
            "lines": [],
            "rects": [],
            "curves": [],
            "annots": [],
            "hyperlinks": [],
            "extract_text_raw": "",
        }
        try:
            import pdfplumber

            with pdfplumber.open(pdf_path) as pdf:
                idx = max(0, int(page) - 1)
                if idx >= len(pdf.pages):
                    return output
                page_obj = pdf.pages[idx]
                page_width = float(getattr(page_obj, "width", 0.0) or 0.0)
                page_height = float(getattr(page_obj, "height", 0.0) or 0.0)
                rotation = int(getattr(page_obj, "rotation", 0) or 0)
                output["page_meta"] = {
                    "page": int(page),
                    "page_width": round(page_width, 2),
                    "page_height": round(page_height, 2),
                    "rotation": int(rotation),
                }
                output["extract_text_raw"] = str(
                    page_obj.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                )

                raw_words = page_obj.extract_words(
                    x_tolerance=1.5,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=False,
                    extra_attrs=["fontname", "size"],
                ) or []
                words: List[Dict[str, Any]] = []
                for word_idx, row in enumerate(raw_words, start=1):
                    if not isinstance(row, dict):
                        continue
                    text = self._normalize_spaces(str(row.get("text") or ""))
                    if not text:
                        continue
                    x0 = float(row.get("x0") or 0.0)
                    x1 = float(row.get("x1") or x0)
                    top = float(row.get("top") or row.get("doctop") or 0.0)
                    bottom = float(row.get("bottom") or top)
                    words.append(
                        {
                            "word_id": f"w{word_idx:06d}",
                            "text": text[:120],
                            "x0": round(x0, 2),
                            "x1": round(x1, 2),
                            "top": round(top, 2),
                            "bottom": round(bottom, 2),
                            "width": round(float(row.get("width") or max(0.0, x1 - x0)), 2),
                            "height": round(float(row.get("height") or max(0.0, bottom - top)), 2),
                            "doctop": round(float(row.get("doctop") or top), 2),
                            "font_name": str(row.get("fontname") or "")[:120],
                            "font_size": round(float(row.get("size") or 0.0), 2),
                            "start_char_id": "",
                            "end_char_id": "",
                        }
                    )
                output["words"] = words

                raw_chars = list(getattr(page_obj, "chars", []) or [])
                chars: List[Dict[str, Any]] = []
                for char_idx, row in enumerate(raw_chars, start=1):
                    if not isinstance(row, dict):
                        continue
                    ch = str(row.get("text") or "")
                    if not ch:
                        continue
                    x0 = float(row.get("x0") or 0.0)
                    x1 = float(row.get("x1") or x0)
                    top = float(row.get("top") or row.get("doctop") or 0.0)
                    bottom = float(row.get("bottom") or top)
                    chars.append(
                        {
                            "char_id": f"c{char_idx:06d}",
                            "text": ch[:8],
                            "x0": round(x0, 2),
                            "x1": round(x1, 2),
                            "top": round(top, 2),
                            "bottom": round(bottom, 2),
                            "width": round(float(row.get("width") or max(0.0, x1 - x0)), 2),
                            "height": round(float(row.get("height") or max(0.0, bottom - top)), 2),
                            "doctop": round(float(row.get("doctop") or top), 2),
                            "font_name": str(row.get("fontname") or "")[:120],
                            "font_size": round(float(row.get("size") or 0.0), 2),
                        }
                    )
                output["chars"] = chars
                if words and chars:
                    ordered_chars = [
                        row for row in chars
                        if isinstance(row, dict) and str(row.get("char_id") or "").strip()
                    ]
                    for word in words:
                        wx0 = float(word.get("x0") or 0.0)
                        wx1 = float(word.get("x1") or wx0)
                        wtop = float(word.get("top") or 0.0)
                        wbottom = float(word.get("bottom") or wtop)
                        matched_ids: List[str] = []
                        for ch in ordered_chars:
                            cx0 = float(ch.get("x0") or 0.0)
                            cx1 = float(ch.get("x1") or cx0)
                            ctop = float(ch.get("top") or 0.0)
                            cbottom = float(ch.get("bottom") or ctop)
                            if cbottom < (wtop - 1.2) or ctop > (wbottom + 1.2):
                                continue
                            center_x = cx0 + ((cx1 - cx0) / 2.0)
                            if center_x < (wx0 - 0.8) or center_x > (wx1 + 0.8):
                                continue
                            matched_ids.append(str(ch.get("char_id") or ""))
                        matched_ids = [item for item in matched_ids if item]
                        if matched_ids:
                            word["start_char_id"] = matched_ids[0]
                            word["end_char_id"] = matched_ids[-1]

                def _sanitize_geom_row(row: Dict[str, Any], *, row_id: str, fields: Sequence[str]) -> Dict[str, Any]:
                    sanitized: Dict[str, Any] = {"id": row_id}
                    for key in fields:
                        val = row.get(key)
                        if isinstance(val, (int, float)):
                            sanitized[key] = round(float(val), 2)
                        elif isinstance(val, str):
                            sanitized[key] = val[:180]
                        elif isinstance(val, bool):
                            sanitized[key] = bool(val)
                    return sanitized

                raw_images = list(getattr(page_obj, "images", []) or [])
                output["images"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"img{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "name", "srcsize", "bits", "colorspace"),
                    )
                    for idx, row in enumerate(raw_images[:256])
                    if isinstance(row, dict)
                ]

                raw_lines = list(getattr(page_obj, "lines", []) or [])
                output["lines"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"ln{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color"),
                    )
                    for idx, row in enumerate(raw_lines[:1200])
                    if isinstance(row, dict)
                ]

                raw_rects = list(getattr(page_obj, "rects", []) or [])
                output["rects"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"rc{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color", "non_stroking_color"),
                    )
                    for idx, row in enumerate(raw_rects[:1200])
                    if isinstance(row, dict)
                ]

                raw_curves = list(getattr(page_obj, "curves", []) or [])
                output["curves"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"cv{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color"),
                    )
                    for idx, row in enumerate(raw_curves[:1200])
                    if isinstance(row, dict)
                ]

                raw_annots = list(getattr(page_obj, "annots", []) or [])
                output["annots"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"an{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "uri", "title", "contents"),
                    )
                    for idx, row in enumerate(raw_annots[:400])
                    if isinstance(row, dict)
                ]

                raw_links = list(getattr(page_obj, "hyperlinks", []) or [])
                output["hyperlinks"] = [
                    _sanitize_geom_row(
                        row,
                        row_id=f"lk{idx + 1:04d}",
                        fields=("x0", "x1", "top", "bottom", "width", "height", "uri"),
                    )
                    for idx, row in enumerate(raw_links[:400])
                    if isinstance(row, dict)
                ]
        except Exception as exc:
            logger.debug(f"[ReaderMM] native pdfplumber extraction failed page={page}: {exc}")
        return output

    async def _render_page_image_data_url(self, *, pdf_path: str, page: int) -> Optional[str]:
        """Render page thumbnail (JPEG base64) for multimodal layout assist."""
        try:
            import pdfplumber

            with pdfplumber.open(pdf_path) as pdf:
                idx = max(0, int(page) - 1)
                if idx >= len(pdf.pages):
                    return None
                page_obj = pdf.pages[idx]
                resolution = max(72, int(getattr(settings, "reader_mm_image_resolution", 96) or 96))
                image = page_obj.to_image(resolution=resolution).original
                if image is None:
                    return None
                max_side = max(640, int(getattr(settings, "reader_mm_image_max_side", 1024) or 1024))
                image.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=82, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                return f"data:image/jpeg;base64,{b64}"
        except Exception as exc:
            logger.debug(f"[ReaderMM] render page image failed page={page}: {exc}")
            return None

    def _match_line_for_block(
        self,
        *,
        text: str,
        line_rows: Sequence[Dict[str, Any]],
    ) -> Tuple[Optional[str], float, Optional[Dict[str, Any]]]:
        target = self._text_key(text)
        if not target:
            return None, 0.0, None
        best_id: Optional[str] = None
        best_score = 0.0
        best_row: Optional[Dict[str, Any]] = None
        for row in line_rows:
            if not isinstance(row, dict):
                continue
            row_text = self._normalize_spaces(str(row.get("text") or ""))
            row_key = str(row.get("text_key") or self._text_key(row_text))
            if not row_key:
                continue
            score = 0.0
            if target in row_key or row_key in target:
                score = 1.0
            else:
                score = self._token_overlap(text, row_text)
            if score > best_score:
                best_score = score
                best_id = self._normalize_line_id(row.get("line_id"))
                best_row = row
        if best_id is None or best_score < 0.18:
            return None, 0.0, None
        return best_id, best_score, best_row

    @staticmethod
    def _normalize_line_id(value: Any) -> str:
        line_id = str(value or "").strip()
        if not line_id:
            return ""
        return line_id

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        a_tokens = [item for item in ReaderMultimodalLayoutService._normalize_spaces(a).lower().split(" ") if item]
        b_tokens = [item for item in ReaderMultimodalLayoutService._normalize_spaces(b).lower().split(" ") if item]
        if not a_tokens or not b_tokens:
            return 0.0
        inter = len(set(a_tokens) & set(b_tokens))
        return inter / max(1, min(len(a_tokens), len(b_tokens)))

    @staticmethod
    def _find_anchor_in_raw_text(*, raw_text: str, text: str, page: int) -> Dict[str, Any]:
        clean_raw = str(raw_text or "")
        clean_text = ReaderMultimodalLayoutService._normalize_spaces(str(text or ""))
        if not clean_text:
            return {"page": int(page), "start_char": 0, "end_char": 1}
        idx = clean_raw.find(clean_text)
        if idx >= 0:
            return {"page": int(page), "start_char": idx, "end_char": idx + len(clean_text)}
        end = max(1, min(8000, len(clean_text)))
        return {"page": int(page), "start_char": 0, "end_char": end}

    @staticmethod
    def _estimate_title_integrity(blocks: Sequence[Dict[str, Any]]) -> bool:
        headings = [
            ReaderMultimodalLayoutService._normalize_spaces(str(item.get("text") or "")).lower()
            for item in blocks
            if isinstance(item, dict) and str(item.get("kind") or "") == "heading"
        ]
        headings = [item for item in headings if item and item not in _GENERIC_HEADINGS]
        if not headings:
            return False
        first = headings[0]
        words = [w for w in first.split(" ") if len(w) >= 3]
        return len(words) >= 3

    @staticmethod
    def _estimate_sidebar_leak(blocks: Sequence[Dict[str, Any]]) -> bool:
        for item in blocks:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") not in {"paragraph", "list_item"}:
                continue
            text = ReaderMultimodalLayoutService._normalize_spaces(str(item.get("text") or "")).lower()
            if not text:
                continue
            if any(pattern in text for pattern in _SIDEBAR_PATTERNS):
                return True
        return False

    @staticmethod
    def _estimate_cross_column_merge_ratio(
        *,
        blocks: Sequence[Dict[str, Any]],
        style_cues: Dict[str, Any],
    ) -> float:
        layout_mode = str(style_cues.get("layout_mode") or "")
        paragraph_blocks = [
            item
            for item in blocks
            if isinstance(item, dict) and str(item.get("kind") or "") == "paragraph"
        ]
        if not paragraph_blocks:
            return 0.0
        if layout_mode != "two_column":
            return 0.0
        long_count = 0
        for item in paragraph_blocks:
            text = ReaderMultimodalLayoutService._normalize_spaces(str(item.get("text") or ""))
            if len(text) >= 900:
                long_count += 1
        return max(0.0, min(1.0, long_count / max(1, len(paragraph_blocks))))

    @staticmethod
    def _looks_like_heading_text(text: str) -> bool:
        value = ReaderMultimodalLayoutService._normalize_spaces(text)
        if not value:
            return False
        if len(value) > 120:
            return False
        if value.lower() in _GENERIC_HEADINGS:
            return False
        return True

    @staticmethod
    def _normalize_spaces(value: str) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _text_key(value: str) -> str:
        text = ReaderMultimodalLayoutService._normalize_spaces(value).lower()
        cleaned = []
        for ch in text:
            if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
                cleaned.append(ch)
        return "".join(cleaned)[:180]
