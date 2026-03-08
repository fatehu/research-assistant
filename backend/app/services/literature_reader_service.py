"""
Generative literature reader service.

Strategy:
- parser-first page structure extraction
- low-confidence agent repair with strict anchor validation
- shared cache (Redis + DB)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.knowledge import Document, KnowledgeBase
from app.models.literature import Paper, PaperAnnotation, PaperReaderPageCache
from app.services.document_mind_parser_service import get_document_mind_parser_service
from app.services.llm_service import get_llm_service
from app.services.status_event_bus import build_status_channel_for_user, publish_status_event

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover - runtime optional
    redis_async = None


STYLE_WHITELIST = ("journal_classic", "clinical_brief", "preprint_modern")
PARSER_VERSION = "reader_parser_v7"
CONFIDENCE_THRESHOLD = 0.68
REDIS_TTL_SECONDS = 24 * 3600
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 6.0
LOCK_POLL_INTERVAL_SECONDS = 0.22
REDIS_KEY_PREFIX = "lit:reader:gpage:v1"
REDIS_LOCK_PREFIX = "lit:reader:gpage:lock:v1"


SECTION_KEYWORDS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "methods",
    "method",
    "materials and methods",
    "results",
    "discussion",
    "conclusion",
    "limitations",
    "references",
    "appendix",
}

DEFAULT_STYLE_TUNING = {
    "body_scale": 1.0,
    "line_height": 1.9,
    "heading_scale": 1.0,
}


@dataclass
class ReaderBuildMeta:
    cache_hit: bool
    cache_layer: str
    build_mode: str
    source_signature: str
    source_sig_hash: str
    parser_version: str = PARSER_VERSION


class LiteratureReaderService:
    def __init__(self) -> None:
        self._redis_client: Any = None
        self._document_mind_parser = get_document_mind_parser_service()

    async def build_or_get_page_payload(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        page: int,
        selected_kb_id: Optional[int] = None,
        force_refresh: bool = False,
        style_hint: Optional[str] = None,
        prefer_agent: bool = False,
        publish_ready_event_enabled: bool = False,
    ) -> Tuple[Dict[str, Any], ReaderBuildMeta]:
        page_num = max(1, int(page))
        # Force one rebuild when user explicitly prefers agent-first parsing.
        prefer_agent = bool(prefer_agent)
        force_refresh = bool(force_refresh or prefer_agent)
        source_signature = await self._build_source_signature(
            db=db,
            user_id=int(user_id),
            paper=paper,
            selected_kb_id=selected_kb_id,
            style_hint=style_hint,
        )
        sig_hash = self._signature_hash(source_signature)
        redis_key = self._cache_key(paper_id=int(paper.id), page=page_num, sig_hash=sig_hash)
        lock_key = self._lock_key(paper_id=int(paper.id), page=page_num)

        if not force_refresh:
            cached_payload = await self._read_payload_from_redis(redis_key)
            if isinstance(cached_payload, dict):
                should_bypass, bypass_reason = self._should_bypass_cached_docmind_payload(cached_payload)
                if should_bypass:
                    logger.info(
                        "[ReaderService] bypass stale redis cache due to missing DocMind layouts "
                        f"paper={paper.id} page={page_num} reason={bypass_reason or 'missing_docmind_layouts'}"
                    )
                else:
                    payload = self._with_cache_meta(cached_payload, cache_hit=True, cache_layer="redis")
                    return payload, ReaderBuildMeta(
                        cache_hit=True,
                        cache_layer="redis",
                        build_mode=str(payload.get("build_mode") or "cache"),
                        source_signature=source_signature,
                        source_sig_hash=sig_hash,
                    )

            cached_row = await self._read_payload_from_db(
                db=db,
                paper_id=int(paper.id),
                page=page_num,
                source_signature=source_signature,
            )
            if isinstance(cached_row, dict):
                should_bypass, bypass_reason = self._should_bypass_cached_docmind_payload(cached_row)
                if should_bypass:
                    logger.info(
                        "[ReaderService] bypass stale db cache due to missing DocMind layouts "
                        f"paper={paper.id} page={page_num} reason={bypass_reason or 'missing_docmind_layouts'}"
                    )
                else:
                    await self._write_payload_to_redis(redis_key, cached_row)
                    payload = self._with_cache_meta(cached_row, cache_hit=True, cache_layer="db")
                    return payload, ReaderBuildMeta(
                        cache_hit=True,
                        cache_layer="db",
                        build_mode=str(payload.get("build_mode") or "cache"),
                        source_signature=source_signature,
                        source_sig_hash=sig_hash,
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
                    return payload, ReaderBuildMeta(
                        cache_hit=True,
                        cache_layer="redis",
                        build_mode=str(payload.get("build_mode") or "cache"),
                        source_signature=source_signature,
                        source_sig_hash=sig_hash,
                    )

        try:
            local_pdf_path = self._resolve_local_pdf_path(
                user_id=int(user_id),
                paper_id=int(paper.id),
                paper_title=paper.title,
                paper_pdf_path=paper.pdf_path,
            )
            if not local_pdf_path:
                raise FileNotFoundError("Paper PDF not found. Please download it first.")

            parsed = await self.parse_page_structure(
                pdf_path=local_pdf_path,
                page=page_num,
                style_hint=style_hint,
                source_url=str(paper.pdf_url or paper.url or ""),
                paper_id=int(paper.id),
            )
            raw_text = str(parsed.get("raw_text") or "")
            structure_confidence = float(parsed.get("structure_confidence") or 0.0)
            build_mode = "parser"

            repaired = parsed
            if prefer_agent or structure_confidence < CONFIDENCE_THRESHOLD:
                repaired = await self.repair_structure_with_agent(
                    page=page_num,
                    raw_text=raw_text,
                    parsed_payload=parsed,
                    style_cues=parsed.get("style_cues") if isinstance(parsed, dict) else None,
                    style_hint=style_hint,
                    temperature=0.25 if prefer_agent else 0.0,
                )
                if repaired is parsed:
                    build_mode = "parser_refresh" if prefer_agent else "parser_fallback"
                else:
                    build_mode = "agent_regenerated" if prefer_agent else "agent_repair"
            structure_confidence = float(repaired.get("structure_confidence") or structure_confidence or 0.0)

            assets = await self.collect_page_assets(
                db=db,
                paper=paper,
                page=page_num,
                raw_text=str(repaired.get("raw_text") or raw_text),
                pdf_path=local_pdf_path,
            )

            payload: Dict[str, Any] = {
                "paper_id": int(paper.id),
                "page": page_num,
                "parser_version": PARSER_VERSION,
                "source_signature": source_signature,
                "style_key": self._normalize_style_key(
                    repaired.get("style_key") if isinstance(repaired, dict) else None,
                    fallback=style_hint,
                ),
                "build_mode": build_mode,
                "structure_confidence": max(0.0, min(1.0, structure_confidence)),
                "summary": str(repaired.get("summary") or ""),
                "style_tuning": self._normalize_style_tuning(
                    repaired.get("style_tuning") if isinstance(repaired, dict) else None,
                    fallback=parsed.get("style_tuning") if isinstance(parsed, dict) else None,
                ),
                "raw_text": str(repaired.get("raw_text") or raw_text),
                "style_cues": dict(repaired.get("style_cues") or parsed.get("style_cues") or {}),
                "line_catalog": list(repaired.get("line_catalog") or parsed.get("line_catalog") or []),
                "sections": list(repaired.get("sections") or []),
                "blocks": list(repaired.get("blocks") or []),
                "side_context_blocks": list(repaired.get("side_context_blocks") or parsed.get("side_context_blocks") or []),
                "figure_meta_blocks": list(repaired.get("figure_meta_blocks") or parsed.get("figure_meta_blocks") or []),
                "toc_quality": float(repaired.get("toc_quality") or parsed.get("toc_quality") or 0.0),
                "parser_chain_meta": dict(repaired.get("parser_chain_meta") or parsed.get("parser_chain_meta") or {}),
                "docmind_meta": dict(
                    repaired.get("docmind_meta")
                    or parsed.get("docmind_meta")
                    or ((repaired.get("parser_chain_meta") or parsed.get("parser_chain_meta") or {}).get("document_mind") or {})
                ),
                "docmind_structure": dict(repaired.get("docmind_structure") or parsed.get("docmind_structure") or {}),
                "page_structure_v3": dict(repaired.get("page_structure_v3") or parsed.get("page_structure_v3") or {}),
                "assets": assets,
                "generated_at": datetime.utcnow().isoformat(),
            }

            await self._upsert_payload_to_db(
                db=db,
                paper_id=int(paper.id),
                page=page_num,
                source_signature=source_signature,
                parser_version=PARSER_VERSION,
                build_mode=build_mode,
                structure_confidence=float(payload["structure_confidence"]),
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
            return payload, ReaderBuildMeta(
                cache_hit=False,
                cache_layer="none",
                build_mode=build_mode,
                source_signature=source_signature,
                source_sig_hash=sig_hash,
            )
        finally:
            if lock_token is not None:
                await self._release_lock(lock_key, lock_token)

    async def parse_page_structure(
        self,
        *,
        pdf_path: str,
        page: int,
        style_hint: Optional[str] = None,
        source_url: Optional[str] = None,
        paper_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        parser_chain_meta: Dict[str, Any] = {
            "document_mind": {"used": False, "reason": "not_attempted"},
        }
        docmind_structure: Dict[str, Any] = {}
        raw_text = ""
        parser_mode = str(getattr(settings, "pdf_layout_parser", "auto") or "auto").strip().lower()
        docmind_only_mode = parser_mode == "document_mind"
        if parser_mode in {"auto", "document_mind"}:
            if hasattr(self._document_mind_parser, "parse_page_structure"):
                docmind_rows, docmind_meta = await self._document_mind_parser.parse_page_structure(
                    paper_id=int(paper_id) if isinstance(paper_id, int) else None,
                    page=int(page),
                    file_url=str(source_url or "").strip(),
                    file_name=os.path.basename(str(pdf_path or "").strip()) or None,
                    local_pdf_path=str(pdf_path or "").strip() or None,
                )
                parser_chain_meta["document_mind"] = dict(docmind_meta or {})
                if isinstance(docmind_rows, dict) and list(docmind_rows.get("layouts") or []):
                    docmind_structure = dict(docmind_rows)
                else:
                    docmind_text, docmind_meta_text = await self._document_mind_parser.parse_page_text(
                        paper_id=int(paper_id) if isinstance(paper_id, int) else None,
                        page=int(page),
                        file_url=str(source_url or "").strip(),
                        file_name=os.path.basename(str(pdf_path or "").strip()) or None,
                        local_pdf_path=str(pdf_path or "").strip() or None,
                    )
                    parser_chain_meta["document_mind"] = dict(docmind_meta_text or parser_chain_meta.get("document_mind") or {})
                    if isinstance(docmind_text, str) and docmind_text.strip():
                        raw_text = docmind_text
            else:
                docmind_text, docmind_meta = await self._document_mind_parser.parse_page_text(
                    paper_id=int(paper_id) if isinstance(paper_id, int) else None,
                    page=int(page),
                    file_url=str(source_url or "").strip(),
                    file_name=os.path.basename(str(pdf_path or "").strip()) or None,
                    local_pdf_path=str(pdf_path or "").strip() or None,
                )
                parser_chain_meta["document_mind"] = dict(docmind_meta or {})
                if isinstance(docmind_text, str) and docmind_text.strip():
                    raw_text = docmind_text

        if docmind_structure:
            style_cues = await asyncio.to_thread(self._extract_page_style_cues, pdf_path, page)
            parsed_docmind = self._build_page_structure_v3_from_docmind(
                page=int(page),
                docmind_structure=docmind_structure,
                parser_chain_meta=parser_chain_meta,
                style_hint=style_hint,
                style_cues=style_cues,
            )
            if isinstance(parsed_docmind, dict) and list(parsed_docmind.get("blocks") or []):
                return parsed_docmind

        if docmind_only_mode:
            if raw_text.strip():
                return self._build_docmind_text_only_payload(
                    page=int(page),
                    raw_text=raw_text,
                    parser_chain_meta=parser_chain_meta,
                    style_hint=style_hint,
                    docmind_structure=docmind_structure,
                )
            return self._build_docmind_empty_payload(
                page=int(page),
                parser_chain_meta=parser_chain_meta,
                style_hint=style_hint,
                docmind_structure=docmind_structure,
            )

        if not raw_text.strip():
            raw_text = await asyncio.to_thread(self._read_pdf_page_text, pdf_path, page)
            if not parser_chain_meta["document_mind"].get("used"):
                parser_chain_meta["document_mind"]["reason"] = str(
                    parser_chain_meta["document_mind"].get("reason") or "fallback_to_local_parser"
                )

        normalized_raw_text = self._normalize_pdf_text(raw_text)
        lines = [line.strip() for line in normalized_raw_text.splitlines()]
        lines = self._split_embedded_heading_lines(lines)
        style_cues = await asyncio.to_thread(self._extract_page_style_cues, pdf_path, page)
        line_catalog = self._build_line_catalog(
            page=int(page),
            raw_text=normalized_raw_text,
            style_cues=style_cues,
        )
        style_heading_hints = {
            self._normalize_spaces(str(item.get("text") or "")).lower(): float(item.get("score") or 0.0)
            for item in list(style_cues.get("heading_hints") or [])
            if isinstance(item, dict)
        }
        noise_line_hints = {
            self._normalize_spaces(str(item.get("text") or "")).lower()
            for item in list(style_cues.get("noise_hints") or [])
            if isinstance(item, dict)
        }
        sidebar_line_hints = self._build_sidebar_line_hints(style_cues)
        paragraph_break_markers = self._build_paragraph_break_markers(
            style_cues=style_cues,
            noise_hints=noise_line_hints,
        )
        side_context_blocks = self._build_side_context_blocks_from_style_cues(
            style_cues=style_cues,
            page=page,
            raw_text=normalized_raw_text,
            noise_hints=noise_line_hints,
        )

        blocks: List[Dict[str, Any]] = []
        sections: List[Dict[str, Any]] = []
        cursor = 0
        order = 0
        section_to_block_ids: Dict[str, List[str]] = {}
        current_section = "Body"
        paragraph_lines: List[str] = []
        paragraph_line_seen: Dict[str, int] = {}

        def _ensure_section(title: str, anchor: Optional[Dict[str, Any]] = None, level: int = 1) -> None:
            nonlocal sections, section_to_block_ids
            if not title:
                title = "Body"
            if title in section_to_block_ids:
                return
            section_to_block_ids[title] = []
            sections.append(
                {
                    "title": title,
                    "level": max(1, min(4, int(level))),
                    "block_ids": section_to_block_ids[title],
                    "source_anchor": anchor,
                }
            )

        def _append_block(
            kind: str,
            text: str,
            section_title: str,
            *,
            zone_type: str = "main_body",
            column_id: str = "main",
            heading_prob: float = 0.0,
            layout_confidence: float = 0.78,
        ) -> None:
            nonlocal cursor, order, blocks
            content = self._normalize_spaces(text)
            if not content:
                return
            start_char, end_char = self._locate_anchor(normalized_raw_text, content, cursor)
            cursor = max(cursor, end_char)
            block_id = f"b{order + 1}"
            canonical_block_id = f"p{int(page)}_{block_id}"
            block = {
                "id": block_id,
                "kind": kind,
                "text": content,
                "order": order,
                "section_title": section_title,
                "source_anchor": {
                    "page": int(page),
                    "start_char": int(start_char),
                    "end_char": int(end_char),
                    "canonical_block_id": canonical_block_id,
                    "coord_version": "anchor_v2",
                    "anchor_v2": {
                        "coord_version": "anchor_v2",
                        "canonical_block_id": canonical_block_id,
                        "page": int(page),
                        "start_char": int(start_char),
                        "end_char": int(end_char),
                    },
                },
                "zone_type": str(zone_type or "main_body"),
                "column_id": str(column_id or "main"),
                "heading_prob": float(max(0.0, min(1.0, heading_prob))),
                "layout_confidence": float(max(0.0, min(1.0, layout_confidence))),
            }
            blocks.append(block)
            section_to_block_ids.setdefault(section_title or "Body", []).append(block_id)
            order += 1

        def _flush_paragraph() -> None:
            nonlocal paragraph_lines
            if not paragraph_lines:
                return
            merged = self._normalize_spaces(" ".join(paragraph_lines))
            paragraph_lines = []
            if merged:
                _ensure_section(current_section)
                _append_block("paragraph", merged, current_section)

        def _append_paragraph_line(line_text: str) -> None:
            paragraph_lines.append(line_text)
            text_key = self._to_text_key(line_text)
            if not text_key:
                return
            occ = int(paragraph_line_seen.get(text_key, 0) + 1)
            paragraph_line_seen[text_key] = occ
            marker_occ = paragraph_break_markers.get(text_key) or set()
            if occ in marker_occ:
                _flush_paragraph()

        _ensure_section("Body")
        for idx, raw_line in enumerate(lines):
            line = self._normalize_spaces(raw_line)
            if not line:
                _flush_paragraph()
                continue
            # Exclude sidebar/callout rows before body structure decisions.
            if self._is_sidebar_line(line, sidebar_hints=sidebar_line_hints):
                _flush_paragraph()
                continue

            # Filter image-overlapped noise rows while keeping potential headings.
            if self._is_noise_line(line, noise_hints=noise_line_hints) and not self._is_heading_line(line):
                _flush_paragraph()
                continue

            next_line = ""
            if idx + 1 < len(lines):
                next_line = self._normalize_spaces(lines[idx + 1])
            is_heading_candidate = self._is_heading_line(line) or self._is_style_heading_hint(
                line,
                heading_hints=style_heading_hints,
            )
            if is_heading_candidate and self._should_demote_heading_line(
                heading_text=line,
                next_line=next_line,
            ):
                _append_paragraph_line(line)
                continue

            if is_heading_candidate:
                _flush_paragraph()
                heading_level = self._heading_level(line)
                current_section = line
                start_char, end_char = self._locate_anchor(normalized_raw_text, line, cursor)
                anchor = {"page": int(page), "start_char": int(start_char), "end_char": int(end_char)}
                _ensure_section(current_section, anchor=anchor, level=heading_level)
                heading_prob = float(style_heading_hints.get(line.lower(), 0.78))
                _append_block(
                    "heading",
                    line,
                    current_section,
                    zone_type="main_body",
                    column_id="main",
                    heading_prob=heading_prob,
                    layout_confidence=max(0.7, heading_prob),
                )
                continue

            if self._is_caption_line(line):
                _flush_paragraph()
                _ensure_section(current_section)
                _append_block(
                    "caption",
                    line,
                    current_section,
                    zone_type="figure_meta",
                    column_id="main",
                    layout_confidence=0.84,
                )
                continue

            if self._is_list_item_line(line):
                _flush_paragraph()
                _ensure_section(current_section)
                _append_block(
                    "list_item",
                    line,
                    current_section,
                    zone_type="main_body",
                    column_id="main",
                    layout_confidence=0.8,
                )
                continue

            _append_paragraph_line(line)
        _flush_paragraph()

        paragraphs = [item["text"] for item in blocks if item.get("kind") == "paragraph"]
        heading_count = sum(1 for item in blocks if item.get("kind") == "heading")
        structure_confidence = self._estimate_structure_confidence(
            raw_text=normalized_raw_text,
            heading_count=heading_count,
            paragraph_count=len(paragraphs),
        )
        summary = self._build_summary(paragraphs)

        if not blocks and normalized_raw_text:
            fallback_text = self._normalize_spaces(normalized_raw_text)
            fallback_block_id = f"p{int(page)}_b1"
            blocks = [
                {
                    "id": "b1",
                    "kind": "paragraph",
                    "text": fallback_text,
                    "order": 0,
                    "section_title": "Body",
                    "source_anchor": {
                        "page": int(page),
                        "start_char": 0,
                        "end_char": max(1, len(fallback_text)),
                        "canonical_block_id": fallback_block_id,
                        "coord_version": "anchor_v2",
                        "anchor_v2": {
                            "coord_version": "anchor_v2",
                            "canonical_block_id": fallback_block_id,
                            "page": int(page),
                            "start_char": 0,
                            "end_char": max(1, len(fallback_text)),
                        },
                    },
                    "zone_type": "main_body",
                    "column_id": "main",
                    "heading_prob": 0.0,
                    "layout_confidence": 0.55,
                }
            ]
            sections = [
                {
                    "title": "Body",
                    "level": 1,
                    "block_ids": ["b1"],
                    "source_anchor": None,
                }
            ]

        figure_meta_blocks = [item for item in blocks if str(item.get("zone_type") or "") == "figure_meta"]
        heading_blocks = [item for item in blocks if str(item.get("kind") or "") == "heading"]
        high_conf_headings = [
            item
            for item in heading_blocks
            if float(item.get("heading_prob") or 0.0) >= 0.72
            and str(item.get("text") or "").strip().lower() not in {"body"}
        ]
        toc_quality = len(high_conf_headings) / max(1, len(heading_blocks)) if heading_blocks else 0.0

        style_key = self._pick_style_key(
            raw_text=normalized_raw_text,
            sections=sections,
            style_hint=style_hint,
        )
        style_tuning = self._derive_style_tuning(style_key=style_key, style_cues=style_cues)
        return {
            "raw_text": normalized_raw_text,
            "style_key": style_key,
            "style_tuning": style_tuning,
            "style_cues": style_cues,
            "line_catalog": line_catalog,
            "structure_confidence": structure_confidence,
            "summary": summary,
            "sections": sections,
            "blocks": blocks,
            "side_context_blocks": side_context_blocks,
            "figure_meta_blocks": figure_meta_blocks,
            "toc_quality": round(max(0.0, min(1.0, toc_quality)), 4),
            "parser_chain_meta": parser_chain_meta,
            "docmind_meta": dict(parser_chain_meta.get("document_mind") or {}),
        }

    def _build_docmind_text_only_payload(
        self,
        *,
        page: int,
        raw_text: str,
        parser_chain_meta: Dict[str, Any],
        style_hint: Optional[str],
        docmind_structure: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_raw_text = self._normalize_pdf_text(raw_text)
        chunks = [
            self._normalize_spaces(item)
            for item in re.split(r"\n\s*\n+", normalized_raw_text)
        ]
        paragraphs = [item for item in chunks if item]
        if not paragraphs and normalized_raw_text:
            fallback = self._normalize_spaces(normalized_raw_text)
            if fallback:
                paragraphs = [fallback]

        blocks: List[Dict[str, Any]] = []
        block_groups: List[Dict[str, Any]] = []
        cursor = 0
        for idx, text in enumerate(paragraphs, start=1):
            if not text:
                continue
            start_char, end_char = self._locate_anchor(normalized_raw_text, text, cursor)
            cursor = max(cursor, end_char)
            block_id = f"b{idx}"
            canonical_block_id = f"p{int(page)}_{block_id}"
            block = {
                "id": block_id,
                "kind": "paragraph",
                "text": text,
                "order": idx - 1,
                "section_title": "Body",
                "source_anchor": {
                    "page": int(page),
                    "start_char": int(start_char),
                    "end_char": int(end_char),
                    "canonical_block_id": canonical_block_id,
                    "coord_version": "anchor_v2",
                    "anchor_v2": {
                        "coord_version": "anchor_v2",
                        "canonical_block_id": canonical_block_id,
                        "page": int(page),
                        "start_char": int(start_char),
                        "end_char": int(end_char),
                    },
                },
                "zone_type": "main_body",
                "column_id": "main",
                "heading_prob": 0.0,
                "layout_confidence": 0.78,
            }
            blocks.append(block)
            block_groups.append(
                {
                    "block_id": canonical_block_id,
                    "kind": "paragraph",
                    "title": "",
                    "text": text,
                    "parent_node_id": "",
                    "line_ids": [],
                    "word_ids": [],
                    "char_ranges": [],
                    "zone_type": "main_body",
                    "column_id": "main",
                    "reading_order": int(idx),
                    "confidence": 0.78,
                    "image_refs": [],
                    "source_spans": [{"start": int(start_char), "end": int(max(start_char + 1, end_char))}],
                    "layout_bbox_or_polygon": {},
                    "style_summary": {},
                }
            )

        sections = [{"title": "Body", "level": 1, "block_ids": [str(row.get("id") or "") for row in blocks], "source_anchor": None}]
        style_key = self._pick_style_key(
            raw_text=normalized_raw_text,
            sections=sections,
            style_hint=style_hint,
        )
        style_tuning = self._derive_style_tuning(style_key=style_key, style_cues={})
        summary = self._build_summary([str(item.get("text") or "") for item in blocks[:8]])
        page_structure_v3 = {
            "source": "document_mind",
            "doc_nav_tree": [],
            "heading_groups": [],
            "paragraph_groups": [],
            "figure_groups": [],
            "block_groups": block_groups,
            "relations": [],
            "counts": {
                "heading_count": 0,
                "paragraph_count": int(len(block_groups)),
                "figure_count": 0,
                "table_count": 0,
                "block_count": int(len(block_groups)),
                "relation_count": 0,
            },
            "notes": [
                "normalized_by=document_mind_text_only",
                f"paragraph_count={len(block_groups)}",
            ],
        }
        return {
            "raw_text": normalized_raw_text,
            "style_key": style_key,
            "style_tuning": style_tuning,
            "style_cues": {},
            "line_catalog": [],
            "structure_confidence": 0.72 if block_groups else 0.0,
            "summary": summary,
            "sections": sections,
            "blocks": blocks,
            "side_context_blocks": [],
            "figure_meta_blocks": [],
            "toc_quality": 0.0,
            "parser_chain_meta": parser_chain_meta,
            "docmind_meta": dict(parser_chain_meta.get("document_mind") or {}),
            "docmind_structure": dict(docmind_structure or {}),
            "page_structure_v3": page_structure_v3,
        }

    def _build_docmind_empty_payload(
        self,
        *,
        page: int,
        parser_chain_meta: Dict[str, Any],
        style_hint: Optional[str],
        docmind_structure: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        style_key = self._pick_style_key(
            raw_text="",
            sections=[],
            style_hint=style_hint,
        )
        style_tuning = self._derive_style_tuning(style_key=style_key, style_cues={})
        reason = str((parser_chain_meta.get("document_mind") or {}).get("reason") or "docmind_empty")
        return {
            "raw_text": "",
            "style_key": style_key,
            "style_tuning": style_tuning,
            "style_cues": {},
            "line_catalog": [],
            "structure_confidence": 0.0,
            "summary": "",
            "sections": [],
            "blocks": [],
            "side_context_blocks": [],
            "figure_meta_blocks": [],
            "toc_quality": 0.0,
            "parser_chain_meta": parser_chain_meta,
            "docmind_meta": dict(parser_chain_meta.get("document_mind") or {}),
            "docmind_structure": dict(docmind_structure or {}),
            "page_structure_v3": {
                "source": "document_mind",
                "doc_nav_tree": [],
                "heading_groups": [],
                "paragraph_groups": [],
                "figure_groups": [],
                "block_groups": [],
                "relations": [],
                "counts": {
                    "heading_count": 0,
                    "paragraph_count": 0,
                    "figure_count": 0,
                    "table_count": 0,
                    "block_count": 0,
                    "relation_count": 0,
                },
                "notes": [
                    "normalized_by=document_mind_only",
                    f"reason={reason}",
                    f"page={int(page)}",
                ],
            },
        }

    def _build_page_structure_v3_from_docmind(
        self,
        *,
        page: int,
        docmind_structure: Dict[str, Any],
        parser_chain_meta: Dict[str, Any],
        style_hint: Optional[str],
        style_cues: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        layouts = [row for row in list((docmind_structure or {}).get("layouts") or []) if isinstance(row, dict)]
        styles = [row for row in list((docmind_structure or {}).get("styles") or []) if isinstance(row, dict)]
        doc_tree = [row for row in list((docmind_structure or {}).get("doc_tree") or []) if isinstance(row, dict)]
        doc_info = dict((docmind_structure or {}).get("doc_info") or {})

        style_map: Dict[int, Dict[str, Any]] = {}
        for row in styles:
            try:
                style_id = int(row.get("styleId"))
            except Exception:
                continue
            style_map[style_id] = row

        page_width = 0.0
        page_height = 0.0
        page_rows = [row for row in list(doc_info.get("pages") or []) if isinstance(row, dict)]
        if page_rows:
            page_idx = max(0, int(page) - 1)
            page_row = page_rows[page_idx] if page_idx < len(page_rows) else page_rows[0]
            page_width = float(page_row.get("imageWidth") or page_row.get("pageWidth") or 0.0)
            page_height = float(page_row.get("imageHeight") or page_row.get("pageHeight") or 0.0)
        if page_width <= 0:
            page_width = float((style_cues or {}).get("page_width") or 0.0)
        if page_height <= 0:
            page_height = float((style_cues or {}).get("page_height") or 0.0)

        def _safe_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except Exception:
                return float(default)

        def _points_from_pos(value: Any) -> List[Dict[str, float]]:
            points: List[Dict[str, float]] = []
            for row in list(value or []):
                if not isinstance(row, dict):
                    continue
                x = _safe_float(row.get("x"), 0.0)
                y = _safe_float(row.get("y"), 0.0)
                points.append({"x": round(float(x), 2), "y": round(float(y), 2)})
            return points

        def _bbox_from_points(points: Sequence[Dict[str, float]]) -> Optional[Dict[str, float]]:
            if not points:
                return None
            xs = [float(item.get("x") or 0.0) for item in points]
            ys = [float(item.get("y") or 0.0) for item in points]
            if not xs or not ys:
                return None
            return {
                "x0": round(min(xs), 2),
                "x1": round(max(xs), 2),
                "top": round(min(ys), 2),
                "bottom": round(max(ys), 2),
            }

        def _kind_for_layout(*, layout_type: str, sub_type: str) -> str:
            token_type = str(layout_type or "").strip().lower()
            token_sub = str(sub_type or "").strip().lower()
            if token_type == "figure":
                return "figure_meta"
            if "title" in token_sub or token_type == "title":
                return "heading"
            if "caption" in token_sub:
                return "caption"
            return "paragraph"

        def _zone_for_layout(*, layout_type: str, kind: str) -> str:
            token_type = str(layout_type or "").strip().lower()
            if token_type in {"figure"} or kind in {"figure_meta", "caption", "table_caption"}:
                return "figure_meta"
            if token_type in {"side", "foot", "header_line", "footer_line", "foot_pagenum"}:
                return "side_context"
            if token_type in {"head", "title", "text"}:
                return "main_body"
            return "main_body"

        def _column_id_for(*, zone_type: str, alignment: str, bbox: Optional[Dict[str, float]]) -> str:
            align = str(alignment or "").strip().lower()
            if zone_type == "side_context":
                if align == "right":
                    return "sidebar_right"
                return "sidebar_left"
            if zone_type == "figure_meta":
                return "main"
            if not isinstance(bbox, dict) or page_width <= 0:
                return "main_right" if align == "right" else "main_left"
            center = (float(bbox.get("x0") or 0.0) + float(bbox.get("x1") or 0.0)) / 2.0
            if center <= page_width * 0.46:
                return "main_left"
            if center >= page_width * 0.54:
                return "main_right"
            return "main"

        ordered_layouts = sorted(
            layouts,
            key=lambda row: (
                int(row.get("index") or 0),
                str(row.get("uniqueId") or ""),
            ),
        )

        raw_parts: List[str] = []
        blocks: List[Dict[str, Any]] = []
        block_groups: List[Dict[str, Any]] = []
        relation_rows: List[Dict[str, Any]] = []
        cursor = 0
        order = 0
        layout_block_ids: Dict[str, List[str]] = {}

        for l_idx, layout in enumerate(ordered_layouts, start=1):
            layout_type = str(layout.get("type") or "").strip()
            sub_type = str(layout.get("subType") or "").strip()
            alignment = str(layout.get("alignment") or "").strip()
            layout_uid = str(layout.get("uniqueId") or f"layout_{l_idx}").strip()
            layout_points = _points_from_pos(layout.get("pos"))
            layout_bbox = _bbox_from_points(layout_points)
            layout_blocks = [row for row in list(layout.get("blocks") or []) if isinstance(row, dict)]
            if not layout_blocks:
                text = self._normalize_spaces(str(layout.get("text") or ""))
                if text:
                    layout_blocks = [{"text": text, "pos": list(layout.get("pos") or []), "styleId": None}]
            if not layout_blocks:
                continue

            created_ids: List[str] = []
            for b_idx, row in enumerate(layout_blocks, start=1):
                text = self._normalize_spaces(str(row.get("text") or ""))
                if not text:
                    continue
                kind = _kind_for_layout(layout_type=layout_type, sub_type=sub_type)
                zone_type = _zone_for_layout(layout_type=layout_type, kind=kind)

                points = _points_from_pos(row.get("pos")) or list(layout_points)
                bbox = _bbox_from_points(points) or layout_bbox
                column_id = _column_id_for(zone_type=zone_type, alignment=alignment, bbox=bbox)

                block_id = f"dm_p{int(page)}_l{int(l_idx):03d}_b{int(b_idx):03d}"
                canonical_block_id = f"p{int(page)}_{block_id}"
                start_char = int(cursor)
                raw_parts.append(text)
                cursor += len(text)
                end_char = int(cursor)
                raw_parts.append("\n")
                cursor += 1

                style_id_raw = row.get("styleId")
                style_summary: Dict[str, Any] = {}
                try:
                    style_id = int(style_id_raw)
                except Exception:
                    style_id = None
                if isinstance(style_id, int) and style_id in style_map:
                    style_row = dict(style_map.get(style_id) or {})
                    style_summary = {
                        "style_id": style_id,
                        "font_name": str(style_row.get("fontName") or "")[:120],
                        "font_size": _safe_float(style_row.get("fontSize"), 0.0),
                        "bold": bool(style_row.get("bold")),
                        "italic": bool(style_row.get("italic")),
                        "color": str(style_row.get("color") or "")[:40],
                    }

                anchor: Dict[str, Any] = {
                    "page": int(page),
                    "start_char": int(start_char),
                    "end_char": int(max(start_char + 1, end_char)),
                    "canonical_block_id": canonical_block_id,
                    "coord_version": "anchor_v2",
                    "anchor_v2": {
                        "coord_version": "anchor_v2",
                        "canonical_block_id": canonical_block_id,
                        "page": int(page),
                        "start_char": int(start_char),
                        "end_char": int(max(start_char + 1, end_char)),
                    },
                }
                if isinstance(bbox, dict):
                    anchor["bbox_hint"] = {
                        "x0": float(bbox.get("x0") or 0.0),
                        "x1": float(bbox.get("x1") or 0.0),
                        "top": float(bbox.get("top") or 0.0),
                        "bottom": float(bbox.get("bottom") or 0.0),
                        "page_width": float(page_width) if page_width > 0 else None,
                        "page_height": float(page_height) if page_height > 0 else None,
                    }
                if points:
                    anchor["geometry_version"] = "poly_v1"
                    anchor["geometry"] = {
                        "polygons": [
                            {
                                "points": points,
                                "source": "docmind_layout",
                                "component_id": block_id,
                            }
                        ],
                        "page_width": float(page_width) if page_width > 0 else None,
                        "page_height": float(page_height) if page_height > 0 else None,
                    }
                anchor["anchor_confidence"] = 0.92

                block_row = {
                    "id": block_id,
                    "kind": kind,
                    "text": text,
                    "order": int(order),
                    "section_title": "Body",
                    "source_anchor": anchor,
                    "zone_type": zone_type,
                    "column_id": column_id,
                    "heading_prob": 0.9 if kind == "heading" else 0.0,
                    "layout_confidence": 0.9,
                }
                blocks.append(block_row)

                group_row = {
                    "block_id": block_id,
                    "kind": kind,
                    "title": text[:220] if kind == "heading" else "",
                    "text": text[:2000],
                    "parent_node_id": "",
                    "line_ids": [],
                    "word_ids": [],
                    "char_ranges": [],
                    "zone_type": zone_type,
                    "column_id": column_id,
                    "reading_order": int(order + 1),
                    "confidence": 0.92,
                    "image_refs": [],
                    "source_spans": [{"start": int(start_char), "end": int(max(start_char + 1, end_char))}],
                    "layout_bbox_or_polygon": {
                        "bbox": dict(bbox or {}),
                        "polygon": points,
                    },
                    "style_summary": style_summary,
                    "layout_unique_id": layout_uid,
                    "layout_type": layout_type,
                    "layout_sub_type": sub_type,
                }
                block_groups.append(group_row)
                created_ids.append(block_id)
                order += 1

            if created_ids:
                layout_block_ids[layout_uid] = list(created_ids)

        normalized_raw_text = self._normalize_pdf_text("".join(raw_parts))
        if not blocks:
            return {}

        heading_blocks = [row for row in blocks if str(row.get("kind") or "") == "heading" and str(row.get("zone_type") or "") == "main_body"]
        paragraph_blocks = [row for row in blocks if str(row.get("kind") or "") == "paragraph" and str(row.get("zone_type") or "") == "main_body"]
        sections: List[Dict[str, Any]] = []
        section_to_block_ids: Dict[str, List[str]] = {}
        current_section = "Body"
        if heading_blocks:
            for row in heading_blocks:
                title = self._normalize_spaces(str(row.get("text") or "")) or "Body"
                section_to_block_ids.setdefault(title, [])
                sections.append(
                    {
                        "title": title,
                        "level": 1,
                        "block_ids": section_to_block_ids[title],
                        "source_anchor": dict(row.get("source_anchor") or {}),
                    }
                )
            current_section = sections[0]["title"] if sections else "Body"
            heading_idx = 0
            ordered_main = [row for row in blocks if str(row.get("zone_type") or "") == "main_body"]
            for row in ordered_main:
                if str(row.get("kind") or "") == "heading":
                    current_section = self._normalize_spaces(str(row.get("text") or "")) or current_section
                    heading_idx += 1
                    continue
                section_to_block_ids.setdefault(current_section, []).append(str(row.get("id") or ""))
                row["section_title"] = current_section
        else:
            sections = [{"title": "Body", "level": 1, "block_ids": [], "source_anchor": None}]
            for row in blocks:
                if str(row.get("zone_type") or "") != "main_body":
                    continue
                sections[0]["block_ids"].append(str(row.get("id") or ""))
                row["section_title"] = "Body"

        main_heading_ids = [str(row.get("id") or "") for row in heading_blocks]
        if main_heading_ids:
            heading_cursor = 0
            current_heading_id = main_heading_ids[0]
            for row in blocks:
                row_id = str(row.get("id") or "")
                if row_id in main_heading_ids:
                    current_heading_id = row_id
                    continue
                if str(row.get("zone_type") or "") != "main_body":
                    continue
                relation_rows.append({"type": "belongs_to_heading", "from": row_id, "to": current_heading_id, "confidence": 0.86})

        doc_nav_tree: List[Dict[str, Any]] = []
        for idx, row in enumerate(heading_blocks, start=1):
            doc_nav_tree.append(
                {
                    "node_id": f"dm_node_{idx:03d}",
                    "type": "section",
                    "title": self._normalize_spaces(str(row.get("text") or ""))[:220],
                    "level": 1,
                    "line_ids": [],
                    "zone_type": "main_body",
                    "column_id": str(row.get("column_id") or "main"),
                    "confidence": float(max(0.8, float(row.get("heading_prob") or 0.8))),
                    "children": [],
                }
            )

        if doc_tree:
            # Keep raw tree availability in notes/meta for debugging and future hierarchy upgrades.
            relation_rows.append(
                {
                    "type": "references_figure",
                    "from": str(blocks[0].get("id") or ""),
                    "to": str(blocks[0].get("id") or ""),
                    "confidence": 0.0,
                    "meta": {"doc_tree_nodes": len(doc_tree)},
                }
            )
            relation_rows = [row for row in relation_rows if float(row.get("confidence") or 0.0) > 0.0]

        figure_meta_blocks = [row for row in blocks if str(row.get("zone_type") or "") == "figure_meta"]
        side_context_blocks = [row for row in blocks if str(row.get("zone_type") or "") == "side_context"]
        toc_quality = len(heading_blocks) / max(1, len([row for row in blocks if str(row.get("zone_type") or "") == "main_body"]))
        structure_confidence = self._estimate_structure_confidence(
            raw_text=normalized_raw_text,
            heading_count=len(heading_blocks),
            paragraph_count=len(paragraph_blocks),
        )
        summary = self._build_summary([str(item.get("text") or "") for item in paragraph_blocks])

        counts = {
            "heading_count": int(len([row for row in block_groups if str(row.get("kind") or "") == "heading"])),
            "paragraph_count": int(len([row for row in block_groups if str(row.get("kind") or "") in {"paragraph", "list_item"}])),
            "figure_count": int(len([row for row in block_groups if str(row.get("zone_type") or "") == "figure_meta"])),
            "table_count": int(len([row for row in block_groups if str(row.get("kind") or "") == "table_caption"])),
            "block_count": int(len(block_groups)),
            "relation_count": int(len(relation_rows)),
        }
        page_structure_v3 = {
            "source": "document_mind",
            "doc_nav_tree": doc_nav_tree,
            "heading_groups": [],
            "paragraph_groups": [],
            "figure_groups": [],
            "block_groups": block_groups,
            "relations": relation_rows,
            "counts": counts,
            "notes": [
                "normalized_by=document_mind",
                f"layout_count={len(layouts)}",
                f"doc_tree_count={len(doc_tree)}",
            ],
        }

        style_key = self._pick_style_key(
            raw_text=normalized_raw_text,
            sections=sections,
            style_hint=style_hint,
        )
        style_tuning = self._derive_style_tuning(style_key=style_key, style_cues=style_cues or {})
        return {
            "raw_text": normalized_raw_text,
            "style_key": style_key,
            "style_tuning": style_tuning,
            "style_cues": dict(style_cues or {}),
            "line_catalog": [],
            "structure_confidence": float(max(0.0, min(1.0, structure_confidence))),
            "summary": summary,
            "sections": sections,
            "blocks": blocks,
            "side_context_blocks": side_context_blocks,
            "figure_meta_blocks": figure_meta_blocks,
            "toc_quality": round(max(0.0, min(1.0, toc_quality)), 4),
            "parser_chain_meta": parser_chain_meta,
            "docmind_structure": docmind_structure,
            "docmind_meta": dict(parser_chain_meta.get("document_mind") or {}),
            "page_structure_v3": page_structure_v3,
        }

    async def repair_structure_with_agent(
        self,
        *,
        page: int,
        raw_text: str,
        parsed_payload: Dict[str, Any],
        style_cues: Optional[Dict[str, Any]] = None,
        style_hint: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        if not raw_text.strip():
            return parsed_payload

        style_context = self._build_agent_style_context(style_cues)
        base_tuning = self._normalize_style_tuning(parsed_payload.get("style_tuning"), fallback=None)

        prompt = (
            "You are a structure-repair agent for a scientific paper page.\n"
            "Rebuild the page into structured JSON without rewriting meaning.\n"
            "Rules:\n"
            "1) Output JSON only.\n"
            "2) Every block must include source_anchor(page,start_char,end_char).\n"
            "3) source_anchor must point to valid character ranges in original text.\n"
            "4) style_key must be one of: journal_classic, clinical_brief, preprint_modern.\n"
            "5) Preserve section boundaries such as Abstract/Introduction/Methods/Results.\n"
            "6) Fix broken word boundaries (e.g. 'RESEA RCH' -> 'RESEARCH') when obvious.\n"
            "7) Prefer style_context typography cues (font size, bold ratio, position) for heading detection.\n"
            "8) Ignore decorative or image-overlap noise text.\n"
            "9) style_tuning must be numeric and bounded: body_scale[0.9,1.25], line_height[1.55,2.2], heading_scale[0.95,1.35].\n"
            "10) If style_context.line_layout marks a row as sidebar_left/sidebar_right, do not merge it into main body sections.\n"
            "\nJSON schema example:\n"
            "{"
            "\"style_key\":\"journal_classic\","
            "\"style_tuning\":{\"body_scale\":1.0,\"line_height\":1.9,\"heading_scale\":1.0},"
            "\"sections\":[{\"title\":\"Introduction\",\"level\":1,"
            "\"source_anchor\":{\"page\":2,\"start_char\":120,\"end_char\":132}}],"
            "\"blocks\":[{\"kind\":\"heading\",\"text\":\"Introduction\",\"section_title\":\"Introduction\","
            "\"source_anchor\":{\"page\":2,\"start_char\":120,\"end_char\":132}}]"
            "}"
            f"\n\nTarget page: {int(page)}\n"
            f"Style context:\n{json.dumps(style_context, ensure_ascii=False)}\n"
            f"Base style_tuning:\n{json.dumps(base_tuning, ensure_ascii=False)}\n"
            f"Original text:\n{raw_text[:16000]}"
        )

        try:
            llm = await get_llm_service()
            result = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=max(0.0, min(0.8, float(temperature))),
                max_tokens=2000,
            )
            content = str(result.get("content") or "").strip()
            parsed_json = self._extract_json_object(content)
            if not isinstance(parsed_json, dict):
                return parsed_payload
            validated = self._validate_agent_repair_payload(
                payload=parsed_json,
                page=page,
                raw_text=raw_text,
                fallback_style=style_hint,
            )
            if validated is None:
                return parsed_payload
            validated["summary"] = self._build_summary(
                [item.get("text", "") for item in validated.get("blocks", []) if item.get("kind") == "paragraph"]
            )
            validated["structure_confidence"] = max(
                float(parsed_payload.get("structure_confidence") or 0.0),
                0.82,
            )
            validated["raw_text"] = raw_text
            validated["style_cues"] = style_cues or parsed_payload.get("style_cues") or {}
            validated["style_tuning"] = self._normalize_style_tuning(
                validated.get("style_tuning"),
                fallback=parsed_payload.get("style_tuning"),
            )
            return validated
        except Exception as exc:
            logger.warning(f"[ReaderService] agent repair failed: {exc}")
            return parsed_payload

    async def collect_page_assets(
        self,
        *,
        db: AsyncSession,
        paper: Paper,
        page: int,
        raw_text: str,
        pdf_path: str,
    ) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        link_map: Dict[str, Dict[str, Any]] = {}

        def _push_link(label: str, href: Optional[str], source: str) -> None:
            normalized = self._normalize_absolute_link(href)
            if not normalized:
                return
            key = normalized.lower()
            if key in link_map:
                return
            link_map[key] = {
                "kind": "link",
                "label": label,
                "source": source,
                "href": normalized,
                "meta": {},
            }

        _push_link("Paper Home", paper.url, "metadata")
        _push_link("PDF Source", paper.pdf_url, "metadata")
        if paper.arxiv_url:
            _push_link("arXiv", paper.arxiv_url, "metadata")
        elif paper.arxiv_id:
            _push_link("arXiv", f"https://arxiv.org/abs/{paper.arxiv_id}", "metadata")
        if paper.doi:
            _push_link(f"DOI: {paper.doi}", paper.doi, "metadata")

        doi_matches = re.findall(r"\b10\.\d{4,9}/[^\s\"'<>]+", raw_text or "", flags=re.IGNORECASE)
        for doi in doi_matches[:8]:
            _push_link(f"DOI: {doi}", doi, "text")

        url_matches = re.findall(r"\bhttps?://[^\s\"'<>]+", raw_text or "", flags=re.IGNORECASE)
        for url in url_matches[:12]:
            _push_link("Page URL", url, "text")

        assets.extend(list(link_map.values())[:12])

        annotation_stmt = (
            select(PaperAnnotation)
            .where(
                and_(
                    PaperAnnotation.paper_id == int(paper.id),
                    PaperAnnotation.page == int(page),
                )
            )
            .order_by(PaperAnnotation.updated_at.desc())
            .limit(8)
        )
        annotation_rows = (await db.execute(annotation_stmt)).scalars().all()
        for row in annotation_rows:
            assets.append(
                {
                    "kind": "annotation",
                    "label": str(row.content or row.quote_text or "Page annotation"),
                    "source": "annotation",
                    "href": None,
                    "meta": {
                        "annotation_id": int(row.id),
                        "annotation_type": str(row.annotation_type or ""),
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    },
                }
            )
        caption_lines = [
            self._normalize_spaces(item)
            for item in (raw_text or "").splitlines()
            if self._is_caption_line(item)
        ]
        for caption in caption_lines[:6]:
            assets.append(
                {
                    "kind": "image_hint",
                    "label": caption[:180],
                    "source": "pdf",
                    "href": None,
                    "meta": {"page": int(page), "caption": caption},
                }
            )

        return assets

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
        style_hint: Optional[str] = None,
    ) -> None:
        for page in pages:
            try:
                await self.build_or_get_page_payload(
                    db=db,
                    user_id=user_id,
                    paper=paper,
                    page=int(page),
                    selected_kb_id=selected_kb_id,
                    force_refresh=False,
                    style_hint=style_hint,
                    publish_ready_event_enabled=True,
                )
            except Exception as exc:
                logger.warning(
                    f"[ReaderService] prefetch failed paper={paper.id} page={page}: {exc}"
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
                f"[ReaderService] publish reader_page_ready failed paper={paper_id}, page={page}: {exc}"
            )

    async def _build_source_signature(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        paper: Paper,
        selected_kb_id: Optional[int],
        style_hint: Optional[str],
    ) -> str:
        path = self._resolve_local_pdf_path(
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
            # Only include knowledge bases owned by the current user.
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
                        select(func.max(Document.updated_at)).where(
                            Document.knowledge_base_id == int(owned_kb_id)
                        )
                    )
                ).scalar_one_or_none()
                kb_part = f"kb:{int(owned_kb_id)}|doc_updated:{max_doc_updated.isoformat() if max_doc_updated else 'none'}"

        style_part = self._normalize_style_key(style_hint, fallback=None)
        parser_mode = str(getattr(settings, "pdf_layout_parser", "auto") or "auto").strip().lower() or "auto"
        docmind_enabled = bool(getattr(settings, "reader_document_mind_enabled", False))
        docmind_allowlist = str(getattr(settings, "reader_document_mind_allowlist", "") or "").strip()
        source_url = str(getattr(paper, "pdf_url", None) or getattr(paper, "url", None) or "").strip()
        signature = (
            f"{stat_part}|parser:{PARSER_VERSION}|parser_mode:{parser_mode}|"
            f"{kb_part}|style:{style_part or 'auto'}|"
            f"docmind:{int(docmind_enabled)}|docmind_allow:{docmind_allowlist}|"
            f"source_url:{source_url[:180]}"
        )
        return signature[:240]

    @staticmethod
    def _should_bypass_cached_docmind_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
        parser_mode = str(getattr(settings, "pdf_layout_parser", "auto") or "auto").strip().lower()
        if parser_mode != "document_mind":
            return False, ""
        if not bool(getattr(settings, "reader_document_mind_enabled", False)):
            return False, ""
        if not isinstance(payload, dict):
            return False, ""
        layouts = [
            row
            for row in list(((payload.get("docmind_structure") or {}).get("layouts") or []))
            if isinstance(row, dict)
        ]
        if layouts:
            return False, ""
        dm_reason = str(
            (((payload.get("parser_chain_meta") or {}).get("document_mind") or {}).get("reason") or "")
        ).strip().lower()
        if dm_reason in {"disabled_or_not_allowlisted", "client_unavailable"}:
            return True, dm_reason
        return False, ""

    @staticmethod
    def _signature_hash(signature: str) -> str:
        return hashlib.sha256((signature or "").encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _cache_key(*, paper_id: int, page: int, sig_hash: str) -> str:
        return f"{REDIS_KEY_PREFIX}:{int(paper_id)}:{int(page)}:{sig_hash}"

    @staticmethod
    def _lock_key(*, paper_id: int, page: int) -> str:
        return f"{REDIS_LOCK_PREFIX}:{int(paper_id)}:{int(page)}"

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
            logger.warning(f"[ReaderService] redis init failed: {exc}")
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
            logger.warning(f"[ReaderService] redis set failed: {exc}")

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

    @staticmethod
    def _normalize_spaces(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _normalize_pdf_text(value: str) -> str:
        text = str(value or "").replace("\u00a0", " ")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        repaired_lines: List[str] = []
        for line in text.splitlines():
            # Repair word boundaries first, then repair fragmented heading tokens.
            normalized_line = LiteratureReaderService._repair_word_boundaries(line.rstrip())
            normalized_line = LiteratureReaderService._repair_fragmented_heading_words(normalized_line)
            repaired_lines.append(normalized_line)
        text = "\n".join(repaired_lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _repair_word_boundaries(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"([,;:])(?=[A-Za-z0-9])", r"\1 ", text)
        text = re.sub(r"\b(of|on|for|in|to|and|with|from|by|at|via|vs)(?=[A-Z])", r"\1 ", text)
        text = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", text)
        text = re.sub(r"(?<=[a-z])(?=[A-Z]{2,}\b)", " ", text)
        text = re.sub(r"(?<=\))(?=[A-Za-z])", " ", text)
        text = re.sub(r"(?<=[A-Za-z])(?=\()", " ", text)
        text = re.sub(r"(?<=\d)(?=[A-Za-z]{3,})", " ", text)
        text = re.sub(r"([A-Za-z]{3,})(\d)", r"\1 \2", text)
        text = re.sub(r"\s+", " ", text).strip()
        return LiteratureReaderService._repair_fragmented_uppercase_title(text)

    @staticmethod
    def _repair_fragmented_uppercase_title(value: str) -> str:
        tokens = str(value or "").split()
        if len(tokens) < 4:
            return value
        if not all(re.fullmatch(r"[A-Z]{1,8}", token or "") for token in tokens):
            return value
        short_token_count = sum(1 for token in tokens if len(token) <= 5)
        if short_token_count < max(4, len(tokens) - 1):
            return value
        joined = "".join(tokens)
        merged_map = {
            "RESEARCHARTICLE": "RESEARCH ARTICLE",
            "AUTHORSUMMARY": "AUTHOR SUMMARY",
            "MATERIALSANDMETHODS": "MATERIALS AND METHODS",
            "RESULTSANDDISCUSSION": "RESULTS AND DISCUSSION",
        }
        return merged_map.get(joined, value)

    @staticmethod
    def _repair_fragmented_heading_words(value: str) -> str:
        text = LiteratureReaderService._normalize_spaces(value)
        if not text:
            return ""
        words = text.split(" ")
        if len(words) < 2:
            return text

        # Only merge a safe, explicit list of fragmented heading words.
        merged_map = {
            "PLOSDIGITALHEALTH": "PLOS DIGITAL HEALTH",
            "DIGITALHEALTH": "DIGITAL HEALTH",
            "RESEARCHARTICLE": "RESEARCH ARTICLE",
            "AUTHORSUMMARY": "AUTHOR SUMMARY",
            "INTRODUCTION": "INTRODUCTION",
            "ABSTRACT": "ABSTRACT",
            "METHODS": "METHODS",
            "RESULTS": "RESULTS",
            "DISCUSSION": "DISCUSSION",
            "CONCLUSION": "CONCLUSION",
            "REFERENCES": "REFERENCES",
            "LIMITATIONS": "LIMITATIONS",
            "MATERIALSANDMETHODS": "MATERIALS AND METHODS",
        }

        repaired: List[str] = []
        idx = 0
        while idx < len(words):
            replaced = False
            token = words[idx]
            if re.fullmatch(r"[A-Z]{1,8}", token or ""):
                for window in (6, 5, 4, 3, 2):
                    if idx + window > len(words):
                        continue
                    segment = words[idx: idx + window]
                    if not all(re.fullmatch(r"[A-Z]{1,8}", part or "") for part in segment):
                        continue
                    merged = "".join(segment)
                    mapped = merged_map.get(merged)
                    if mapped:
                        repaired.append(mapped)
                        idx += window
                        replaced = True
                        break
            if replaced:
                continue
            repaired.append(token)
            idx += 1
        return " ".join(repaired)

    @staticmethod
    def _split_embedded_heading_lines(lines: Sequence[str]) -> List[str]:
        heading_candidates = sorted(
            list(SECTION_KEYWORDS | {"author summary", "materials and methods"}),
            key=len,
            reverse=True,
        )
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(item) for item in heading_candidates) + r")\b",
            flags=re.IGNORECASE,
        )

        output: List[str] = []
        for raw_line in lines:
            line = LiteratureReaderService._normalize_spaces(raw_line)
            if not line:
                continue

            matched = False
            for match in pattern.finditer(line):
                prefix = line[:match.start()].strip()
                heading = line[match.start():match.end()].strip()
                suffix = line[match.end():].strip()
                # KISS safety rule:
                # Only split when a section keyword appears near line start.
                # This prevents false positives like "learning method Anecdotal ..."
                # from being promoted to a heading.
                if match.start() > 8 or len(suffix) < 24:
                    continue
                if suffix and re.match(r"^[\.,;!?]", suffix):
                    continue
                if suffix and suffix[:1].islower():
                    continue
                if prefix and re.search(r"[A-Za-z\u4e00-\u9fff]", prefix):
                    continue
                if prefix:
                    output.append(prefix)
                output.append(heading)
                if suffix:
                    output.append(suffix)
                matched = True
                break

            if not matched:
                output.append(line)
        return output

    @staticmethod
    def _is_style_heading_hint(text: str, *, heading_hints: Dict[str, float]) -> bool:
        normalized = LiteratureReaderService._normalize_spaces(text).lower()
        if not normalized or not heading_hints:
            return False
        score = float(heading_hints.get(normalized) or 0.0)
        if score >= 0.56:
            return True
        if len(normalized) < 8:
            return False
        for candidate, candidate_score in heading_hints.items():
            if float(candidate_score) < 0.72:
                continue
            if normalized.startswith(candidate) or candidate.startswith(normalized):
                return True
        return False

    @staticmethod
    def _is_noise_line(text: str, *, noise_hints: Optional[set[str]] = None) -> bool:
        normalized = LiteratureReaderService._normalize_spaces(text)
        if not normalized:
            return False
        lowered = normalized.lower()
        if noise_hints and lowered in noise_hints:
            return True
        return LiteratureReaderService._is_probable_noise_line(normalized)

    @staticmethod
    def _is_probable_noise_line(text: str) -> bool:
        value = LiteratureReaderService._normalize_spaces(text)
        if not value:
            return False
        if re.search(r"(.)\1{5,}", value):
            return True
        compact = value.replace(" ", "")
        if re.fullmatch(r"[A-Za-z]\d{6,}", compact):
            return True
        if re.fullmatch(r"[A-Za-z]{0,2}\d{8,}[A-Za-z]{0,2}", compact):
            return True
        alnum = re.sub(r"[^A-Za-z0-9]", "", value)
        if len(alnum) >= 10:
            digit_ratio = sum(1 for ch in alnum if ch.isdigit()) / max(1, len(alnum))
            if digit_ratio >= 0.72:
                return True
        return False

    @staticmethod
    def _build_sidebar_line_hints(raw_style_cues: Optional[Dict[str, Any]]) -> Dict[str, set[str]]:
        cues = raw_style_cues if isinstance(raw_style_cues, dict) else {}
        text_hints: set[str] = set()
        key_hints: set[str] = set()
        for item in list(cues.get("line_layout") or []):
            if not isinstance(item, dict):
                continue
            column_label = str(item.get("column_label") or "")
            if not column_label.startswith("sidebar"):
                continue
            text = LiteratureReaderService._normalize_spaces(str(item.get("text") or ""))
            if not text:
                continue
            text_hints.add(text.lower())
            text_key = str(item.get("text_key") or LiteratureReaderService._to_text_key(text))
            if text_key:
                key_hints.add(text_key)
        return {"texts": text_hints, "keys": key_hints}

    @staticmethod
    def _build_side_context_blocks_from_style_cues(
        *,
        style_cues: Optional[Dict[str, Any]],
        page: int,
        raw_text: str,
        noise_hints: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Build side-context blocks from visual line rows to preserve sidebar content."""
        cues = style_cues if isinstance(style_cues, dict) else {}
        line_layout = list(cues.get("line_layout") or [])
        noise_set = noise_hints or set()
        output: List[Dict[str, Any]] = []
        order = 0
        cursor = 0
        for idx, row in enumerate(line_layout[:220]):
            if not isinstance(row, dict):
                continue
            column_label = str(row.get("column_label") or "main")
            if not column_label.startswith("sidebar"):
                continue
            text = LiteratureReaderService._normalize_spaces(str(row.get("text") or ""))
            if not text or len(text) < 4:
                continue
            if text.lower() in noise_set:
                continue
            if LiteratureReaderService._is_probable_noise_line(text):
                continue
            start_char, end_char = LiteratureReaderService._locate_anchor(raw_text, text, cursor)
            cursor = max(cursor, end_char)
            block_id = f"sb{idx + 1}"
            canonical_block_id = f"p{int(page)}_{block_id}"
            output.append(
                {
                    "id": block_id,
                    "kind": "paragraph",
                    "text": text,
                    "order": order,
                    "section_title": "Side Context",
                    "source_anchor": {
                        "page": int(page),
                        "start_char": int(start_char),
                        "end_char": int(end_char),
                        "canonical_block_id": canonical_block_id,
                        "coord_version": "anchor_v2",
                        "anchor_v2": {
                            "coord_version": "anchor_v2",
                            "canonical_block_id": canonical_block_id,
                            "page": int(page),
                            "start_char": int(start_char),
                            "end_char": int(end_char),
                        },
                    },
                    "zone_type": "side_context",
                    "column_id": column_label,
                    "heading_prob": 0.0,
                    "layout_confidence": 0.78,
                }
            )
            order += 1
        return output

    @staticmethod
    def _line_column_slot(
        *,
        column_label: str,
        x0: float,
        x1: float,
        page_width: float,
    ) -> str:
        label = str(column_label or "main").strip().lower()
        if label.startswith("sidebar_left"):
            return "sidebar_left"
        if label.startswith("sidebar_right"):
            return "sidebar_right"
        if label.startswith("sidebar"):
            return "sidebar"
        if label.startswith("main"):
            if page_width > 1.0:
                center = (float(x0) + float(x1)) / 2.0
                if center <= page_width * 0.46:
                    return "main_left"
                if center >= page_width * 0.54:
                    return "main_right"
            return "main"
        return "main"

    def _build_line_catalog(
        self,
        *,
        page: int,
        raw_text: str,
        style_cues: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        cues = style_cues if isinstance(style_cues, dict) else {}
        line_layout = list(cues.get("line_layout") or [])
        if not line_layout:
            return []

        page_width = float(cues.get("page_width") or 0.0)
        page_height = float(cues.get("page_height") or 0.0)
        cursor = 0
        output: List[Dict[str, Any]] = []
        for idx, row in enumerate(line_layout[:220], start=1):
            if not isinstance(row, dict):
                continue
            text = self._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            x0 = float(row.get("x0") or 0.0)
            x1 = float(row.get("x1") or x0)
            top = float(row.get("top") or 0.0)
            bottom = float(row.get("bottom") or top)
            start_char, end_char = self._locate_anchor(raw_text, text, cursor)
            cursor = max(cursor, end_char)
            column_label = str(row.get("column_label") or "main")
            column_slot = self._line_column_slot(
                column_label=column_label,
                x0=x0,
                x1=x1,
                page_width=page_width,
            )
            existing_line_id = str(row.get("line_id") or "").strip()
            if not re.match(r"^p\d+_l\d+_[a-z0-9_]+$", existing_line_id, flags=re.IGNORECASE):
                existing_line_id = ""
            line_id = existing_line_id or f"p{int(page)}_l{idx:03d}_{column_slot}"
            words = []
            for word in list(row.get("words") or [])[:120]:
                if not isinstance(word, dict):
                    continue
                word_text = self._normalize_spaces(str(word.get("text") or ""))
                if not word_text:
                    continue
                word_x0 = float(word.get("x0") or 0.0)
                word_x1 = float(word.get("x1") or word_x0)
                word_top = float(word.get("top") or 0.0)
                word_bottom = float(word.get("bottom") or word_top)
                words.append(
                    {
                        "text": word_text[:80],
                        "x0": round(word_x0, 2),
                        "x1": round(word_x1, 2),
                        "top": round(word_top, 2),
                        "bottom": round(word_bottom, 2),
                        "width": round(float(word.get("width") or max(0.0, word_x1 - word_x0)), 2),
                        "height": round(float(word.get("height") or max(0.0, word_bottom - word_top)), 2),
                        "font_name": str(word.get("font_name") or "")[:120],
                        "font_size": round(float(word.get("font_size") or 0.0), 2),
                    }
                )
            output.append(
                {
                    "line_id": line_id,
                    "page": int(page),
                    "order": int(idx - 1),
                    "text": text[:220],
                    "text_key": str(row.get("text_key") or ""),
                    "column_label": column_label,
                    "column_slot": column_slot,
                    "x0": round(x0, 2),
                    "x1": round(x1, 2),
                    "top": round(top, 2),
                    "bottom": round(bottom, 2),
                    "width": round(float(row.get("width") or max(0.0, x1 - x0)), 2),
                    "height": round(float(row.get("height") or max(0.0, bottom - top)), 2),
                    "avg_size": round(float(row.get("avg_size") or 0.0), 2),
                    "bold_ratio": round(float(row.get("bold_ratio") or 0.0), 3),
                    "image_overlap_ratio": round(float(row.get("image_overlap_ratio") or 0.0), 3),
                    "start_char": int(start_char),
                    "end_char": int(end_char),
                    "page_width": round(page_width, 2),
                    "page_height": round(page_height, 2),
                    "words": words,
                }
            )
        return output

    def _build_paragraph_break_markers(
        self,
        *,
        style_cues: Optional[Dict[str, Any]],
        noise_hints: Optional[set[str]] = None,
    ) -> Dict[str, set[int]]:
        """Build deterministic paragraph break markers from visual line layout."""
        cues = style_cues if isinstance(style_cues, dict) else {}
        rows: List[Dict[str, Any]] = []
        for raw in list(cues.get("line_layout") or [])[:220]:
            if not isinstance(raw, dict):
                continue
            text = self._normalize_spaces(str(raw.get("text") or ""))
            if not text:
                continue
            if self._is_noise_line(text, noise_hints=noise_hints):
                continue
            column_label = str(raw.get("column_label") or "main")
            if column_label.startswith("sidebar"):
                continue
            text_key = self._to_text_key(text)
            if not text_key:
                continue
            top = float(raw.get("top") or 0.0)
            bottom = float(raw.get("bottom") or top)
            height = max(1.0, float(raw.get("height") or max(0.0, bottom - top)))
            rows.append(
                {
                    "text": text,
                    "text_key": text_key,
                    "column_label": column_label,
                    "top": top,
                    "bottom": bottom,
                    "height": height,
                }
            )
        if len(rows) < 2:
            return {}

        seen_counts: Dict[str, int] = {}
        for row in rows:
            key = str(row.get("text_key") or "")
            occ = int(seen_counts.get(key, 0) + 1)
            seen_counts[key] = occ
            row["occ"] = occ

        markers: Dict[str, set[int]] = {}
        for idx in range(len(rows) - 1):
            current = rows[idx]
            nxt = rows[idx + 1]
            should_break = False

            current_col = str(current.get("column_label") or "main")
            next_col = str(nxt.get("column_label") or "main")
            if current_col != next_col:
                should_break = True

            gap = float(nxt.get("top") or 0.0) - float(current.get("bottom") or 0.0)
            prev_height = max(1.0, float(current.get("height") or 1.0))
            if gap > max(12.0, prev_height * 1.45):
                should_break = True

            sentence_end = bool(re.search(r"[.!?;:。！？；]$", str(current.get("text") or "")))
            if sentence_end and gap > max(8.0, prev_height * 1.15):
                should_break = True
            if sentence_end and str((nxt.get("text") or ""))[:1].isupper() and gap > max(4.0, prev_height * 0.45):
                should_break = True

            if should_break:
                key = str(current.get("text_key") or "")
                occ = int(current.get("occ") or 0)
                if key and occ > 0:
                    markers.setdefault(key, set()).add(occ)
        return markers

    @staticmethod
    def _is_sidebar_line(text: str, *, sidebar_hints: Optional[Dict[str, set[str]]]) -> bool:
        if not sidebar_hints:
            return False
        normalized = LiteratureReaderService._normalize_spaces(text).lower()
        if not normalized:
            return False
        text_hints = sidebar_hints.get("texts") or set()
        key_hints = sidebar_hints.get("keys") or set()
        if normalized in text_hints:
            return True
        text_key = LiteratureReaderService._to_text_key(normalized)
        if text_key and text_key in key_hints:
            return True
        if not text_key or len(text_key) < 18:
            return False
        for hint in key_hints:
            if len(hint) < 18:
                continue
            if text_key in hint or hint in text_key:
                return True
        return False

    @staticmethod
    def _extract_page_style_cues(path: str, page: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "page": int(page),
            "page_width": 0.0,
            "page_height": 0.0,
            "line_count": 0,
            "image_count": 0,
            "median_font_size": 0.0,
            "layout_mode": "unknown",
            "main_column": None,
            "heading_hints": [],
            "noise_hints": [],
            "line_layout": [],
        }
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                page_index = max(0, int(page) - 1)
                if page_index >= len(pdf.pages):
                    return result

                page_obj = pdf.pages[page_index]
                page_width = float(getattr(page_obj, "width", 0.0) or 0.0)
                page_height = float(getattr(page_obj, "height", 0.0) or 0.0)
                words = page_obj.extract_words(
                    x_tolerance=1.5,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=True,
                    extra_attrs=["fontname", "size"],
                ) or []
                images = list(page_obj.images or [])

                image_boxes: List[Tuple[float, float, float, float]] = []
                for img in images[:24]:
                    x0 = float(img.get("x0") or 0.0)
                    x1 = float(img.get("x1") or x0)
                    top = float(img.get("top") or 0.0)
                    bottom = float(img.get("bottom") or top)
                    image_boxes.append((min(x0, x1), max(x0, x1), min(top, bottom), max(top, bottom)))

                line_rows: List[Dict[str, Any]] = []
                current: List[Dict[str, Any]] = []
                current_top: Optional[float] = None
                def _flush_current_words() -> None:
                    nonlocal current
                    if not current:
                        return
                    segments = LiteratureReaderService._split_words_by_spacing(
                        current,
                        page_width=page_width,
                    )
                    for seg in segments:
                        row = LiteratureReaderService._build_style_line_row(seg, image_boxes)
                        if row.get("text"):
                            line_rows.append(row)
                    current = []

                for item in words:
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    top_val = float(item.get("top") or item.get("doctop") or 0.0)
                    if current_top is None:
                        current = [item]
                        current_top = top_val
                        continue
                    if abs(top_val - current_top) <= 2.8:
                        current.append(item)
                        current_top = (current_top + top_val) / 2.0
                    else:
                        _flush_current_words()
                        current = [item]
                        current_top = top_val
                _flush_current_words()

                line_rows = [row for row in line_rows if row.get("text")]
                line_rows, layout_meta = LiteratureReaderService._classify_line_layout(
                    line_rows=line_rows,
                    page_width=page_width,
                )
                font_sizes = [float(row.get("avg_size") or 0.0) for row in line_rows if float(row.get("avg_size") or 0.0) > 0]
                median_size = float(statistics.median(font_sizes)) if font_sizes else 0.0

                heading_hints: List[Dict[str, Any]] = []
                noise_hints: List[Dict[str, Any]] = []
                for row in line_rows:
                    text = LiteratureReaderService._normalize_spaces(str(row.get("text") or ""))
                    if not text:
                        continue
                    column_label = str(row.get("column_label") or "main")
                    overlap_ratio = float(row.get("image_overlap_ratio") or 0.0)
                    likely_noise = LiteratureReaderService._is_probable_noise_line(text) or overlap_ratio >= 0.55
                    if likely_noise:
                        noise_hints.append(
                            {
                                "text": text[:120],
                                "column_label": column_label,
                                "reason": "image_overlap" if overlap_ratio >= 0.55 else "glyph_noise",
                            }
                        )
                        continue

                    avg_size = float(row.get("avg_size") or 0.0)
                    max_size = float(row.get("max_size") or avg_size)
                    bold_ratio = float(row.get("bold_ratio") or 0.0)
                    size_score = 0.0
                    if median_size > 0:
                        size_score = max(0.0, min(1.0, (max_size / max(0.01, median_size)) - 1.0))
                    score = max(0.0, min(1.0, size_score * 0.7 + bold_ratio * 0.3))
                    if len(text) <= 120 and (
                        avg_size >= max(8.0, median_size * 1.18)
                        or max_size >= max(9.5, median_size * 1.28)
                        or (bold_ratio >= 0.62 and len(text.split()) <= 14)
                    ):
                        if column_label.startswith("sidebar"):
                            continue
                        heading_hints.append(
                            {
                                "text": text[:140],
                                "avg_size": round(avg_size, 2),
                                "bold_ratio": round(bold_ratio, 3),
                                "score": round(score, 3),
                                "column_label": column_label,
                            }
                        )

                line_layout = []
                char_cursor = 0
                for row in line_rows[:160]:
                    row_text = str(row.get("text") or "")[:180]
                    row_span = max(1, len(row_text))
                    start_char = int(char_cursor)
                    end_char = int(start_char + row_span)
                    char_cursor = end_char + 1
                    column_label = str(row.get("column_label") or "main")
                    x0 = float(row.get("x0") or 0.0)
                    x1 = float(row.get("x1") or x0)
                    column_slot = LiteratureReaderService._line_column_slot(
                        column_label=column_label,
                        x0=x0,
                        x1=x1,
                        page_width=page_width,
                    )
                    line_id = f"p{int(page)}_l{len(line_layout) + 1:03d}_{column_slot}"
                    line_layout.append(
                        {
                            "line_id": line_id,
                            "text": row_text,
                            "text_key": str(row.get("text_key") or ""),
                            "column_label": column_label,
                            "column_slot": column_slot,
                            "x0": round(x0, 2),
                            "x1": round(x1, 2),
                            "top": round(float(row.get("top") or 0.0), 2),
                            "bottom": round(float(row.get("bottom") or 0.0), 2),
                            "width": round(float(row.get("width") or 0.0), 2),
                            "height": round(float(row.get("height") or 0.0), 2),
                            "avg_size": round(float(row.get("avg_size") or 0.0), 2),
                            "bold_ratio": round(float(row.get("bold_ratio") or 0.0), 3),
                            "image_overlap_ratio": round(float(row.get("image_overlap_ratio") or 0.0), 3),
                            "start_char": start_char,
                            "end_char": end_char,
                            "words": list(row.get("words") or [])[:120],
                        }
                    )

                result.update(
                    {
                        "page_width": round(page_width, 2),
                        "page_height": round(page_height, 2),
                        "line_count": len(line_rows),
                        "image_count": len(images),
                        "median_font_size": round(median_size, 3),
                        "layout_mode": str(layout_meta.get("layout_mode") or "unknown"),
                        "main_column": layout_meta.get("main_column"),
                        "heading_hints": heading_hints[:22],
                        "noise_hints": noise_hints[:18],
                        "line_layout": line_layout,
                    }
                )
                return result
        except Exception as exc:
            logger.debug(f"[ReaderService] style cue extract failed page={page}: {exc}")
            return result

    @staticmethod
    def _build_style_line_row(words: Sequence[Dict[str, Any]], image_boxes: Sequence[Tuple[float, float, float, float]]) -> Dict[str, Any]:
        ordered = sorted(words, key=lambda item: (float(item.get("x0") or 0.0), str(item.get("text") or "")))
        texts = [str(item.get("text") or "").strip() for item in ordered if str(item.get("text") or "").strip()]
        if not texts:
            return {}
        x0 = min(float(item.get("x0") or 0.0) for item in ordered)
        x1 = max(float(item.get("x1") or x0) for item in ordered)
        top = min(float(item.get("top") or item.get("doctop") or 0.0) for item in ordered)
        bottom = max(float(item.get("bottom") or top) for item in ordered)

        sizes = [float(item.get("size") or 0.0) for item in ordered if float(item.get("size") or 0.0) > 0.0]
        avg_size = (sum(sizes) / len(sizes)) if sizes else 0.0
        max_size = max(sizes) if sizes else avg_size

        bold_count = 0
        for item in ordered:
            font_name = str(item.get("fontname") or "").lower()
            if any(flag in font_name for flag in ("bold", "black", "heavy", "semibold", "demi")):
                bold_count += 1
        bold_ratio = bold_count / max(1, len(ordered))

        line_box = (x0, x1, top, bottom)
        overlap_ratio = 0.0
        if image_boxes:
            overlap_ratio = max(
                LiteratureReaderService._rect_overlap_ratio(line_box, image_box)
                for image_box in image_boxes
            )

        text = LiteratureReaderService._normalize_spaces(" ".join(texts))
        width = max(0.0, x1 - x0)
        height = max(0.0, bottom - top)
        word_rows = []
        for item in ordered:
            word_text = LiteratureReaderService._normalize_spaces(str(item.get("text") or ""))
            if not word_text:
                continue
            word_x0 = float(item.get("x0") or 0.0)
            word_x1 = float(item.get("x1") or word_x0)
            word_top = float(item.get("top") or item.get("doctop") or 0.0)
            word_bottom = float(item.get("bottom") or word_top)
            word_rows.append(
                {
                    "text": word_text[:80],
                    "x0": round(word_x0, 2),
                    "x1": round(word_x1, 2),
                    "top": round(word_top, 2),
                    "bottom": round(word_bottom, 2),
                    "width": round(max(0.0, word_x1 - word_x0), 2),
                    "height": round(max(0.0, word_bottom - word_top), 2),
                    "font_name": str(item.get("fontname") or "")[:120],
                    "font_size": round(float(item.get("size") or 0.0), 2),
                }
            )
        return {
            "text": text,
            "text_key": LiteratureReaderService._to_text_key(text),
            "avg_size": avg_size,
            "max_size": max_size,
            "bold_ratio": bold_ratio,
            "image_overlap_ratio": overlap_ratio,
            "x0": x0,
            "x1": x1,
            "top": top,
            "bottom": bottom,
            "width": width,
            "height": height,
            "x_center": x0 + (width / 2.0),
            "char_count": len(text),
            "words": word_rows,
        }

    @staticmethod
    def _split_words_by_spacing(
        words: Sequence[Dict[str, Any]],
        *,
        page_width: float,
    ) -> List[List[Dict[str, Any]]]:
        ordered = sorted(
            [item for item in words if isinstance(item, dict)],
            key=lambda item: (float(item.get("x0") or 0.0), str(item.get("text") or "")),
        )
        if len(ordered) <= 1:
            return [ordered] if ordered else []

        widths: List[float] = []
        gaps: List[float] = []
        for item in ordered:
            x0 = float(item.get("x0") or 0.0)
            x1 = float(item.get("x1") or x0)
            widths.append(max(0.0, x1 - x0))
        for idx in range(1, len(ordered)):
            prev = ordered[idx - 1]
            cur = ordered[idx]
            prev_x1 = float(prev.get("x1") or prev.get("x0") or 0.0)
            cur_x0 = float(cur.get("x0") or 0.0)
            gaps.append(max(0.0, cur_x0 - prev_x1))

        width_base = float(statistics.median(widths)) if widths else 0.0
        positive_gaps = [gap for gap in gaps if gap > 0.0]
        gap_base = float(statistics.median(positive_gaps)) if positive_gaps else 0.0

        dynamic_gap_threshold = max(10.0, gap_base * 3.2, width_base * 1.6)
        hard_gap_threshold = max(56.0, float(page_width) * 0.09) if page_width > 0 else 56.0

        segments: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = [ordered[0]]
        for idx in range(1, len(ordered)):
            prev = ordered[idx - 1]
            cur = ordered[idx]
            prev_x1 = float(prev.get("x1") or prev.get("x0") or 0.0)
            cur_x0 = float(cur.get("x0") or 0.0)
            gap = max(0.0, cur_x0 - prev_x1)

            should_split = False
            if gap >= hard_gap_threshold:
                should_split = True
            elif gap_base > 0.0 and gap >= dynamic_gap_threshold:
                should_split = True

            if should_split and current:
                segments.append(current)
                current = [cur]
            else:
                current.append(cur)
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def _rect_overlap_ratio(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
        ax0, ax1, ay0, ay1 = a
        bx0, bx1, by0, by1 = b
        inter_x = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        inter_y = max(0.0, min(ay1, by1) - max(ay0, by0))
        if inter_x <= 0.0 or inter_y <= 0.0:
            return 0.0
        inter_area = inter_x * inter_y
        area_a = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
        return max(0.0, min(1.0, inter_area / area_a))

    @staticmethod
    def _line_x_overlap_ratio(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        ax0, ax1 = a
        bx0, bx1 = b
        overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        width = max(1e-6, ax1 - ax0)
        return max(0.0, min(1.0, overlap / width))

    @staticmethod
    def _classify_line_layout(
        *,
        line_rows: Sequence[Dict[str, Any]],
        page_width: float,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not line_rows:
            return [], {"layout_mode": "unknown", "main_column": None}

        rows = [dict(item) for item in line_rows]
        if page_width <= 1.0:
            for row in rows:
                row["column_label"] = "main"
            return rows, {"layout_mode": "single", "main_column": None}

        valid_rows = [
            row
            for row in rows
            if not LiteratureReaderService._is_probable_noise_line(str(row.get("text") or ""))
            and float(row.get("width") or 0.0) >= max(40.0, page_width * 0.12)
            and int(row.get("char_count") or 0) >= 6
        ]

        # When both left/right columns are dense and similarly wide, treat as two-column body.
        left_candidates = [
            row
            for row in valid_rows
            if float(row.get("x_center") or 0.0) <= page_width * 0.5
            and page_width * 0.18 <= float(row.get("width") or 0.0) <= page_width * 0.56
        ]
        right_candidates = [
            row
            for row in valid_rows
            if float(row.get("x_center") or 0.0) > page_width * 0.5
            and page_width * 0.18 <= float(row.get("width") or 0.0) <= page_width * 0.56
        ]
        left_width_med = statistics.median([float(row.get("width") or 0.0) for row in left_candidates]) if left_candidates else 0.0
        right_width_med = statistics.median([float(row.get("width") or 0.0) for row in right_candidates]) if right_candidates else 0.0
        two_column = (
            len(left_candidates) >= 6
            and len(right_candidates) >= 6
            and left_width_med > 0
            and right_width_med > 0
            and abs(left_width_med - right_width_med) <= page_width * 0.1
        )
        if two_column:
            for row in rows:
                row["column_label"] = "main"
            return rows, {"layout_mode": "two_column", "main_column": None}

        main_candidates = [
            row
            for row in valid_rows
            if int(row.get("char_count") or 0) >= 22 and float(row.get("width") or 0.0) >= page_width * 0.28
        ]
        if not main_candidates and valid_rows:
            main_candidates = sorted(
                valid_rows,
                key=lambda row: (float(row.get("width") or 0.0), int(row.get("char_count") or 0)),
                reverse=True,
            )[:8]
        if not main_candidates:
            for row in rows:
                row["column_label"] = "main"
            return rows, {"layout_mode": "single", "main_column": None}

        main_x0 = float(statistics.median([float(row.get("x0") or 0.0) for row in main_candidates]))
        main_x1 = float(statistics.median([float(row.get("x1") or 0.0) for row in main_candidates]))
        main_width = max(1.0, main_x1 - main_x0)

        for row in rows:
            x0 = float(row.get("x0") or 0.0)
            x1 = float(row.get("x1") or x0)
            width = float(row.get("width") or max(0.0, x1 - x0))
            overlap_ratio = LiteratureReaderService._line_x_overlap_ratio((x0, x1), (main_x0, main_x1))
            if width >= page_width * 0.72 or overlap_ratio >= 0.42:
                row["column_label"] = "main"
                continue
            if x1 <= main_x0 + page_width * 0.06 and width <= max(page_width * 0.48, main_width * 0.82):
                row["column_label"] = "sidebar_left"
                continue
            if x0 >= main_x1 - page_width * 0.06 and width <= max(page_width * 0.48, main_width * 0.82):
                row["column_label"] = "sidebar_right"
                continue
            row["column_label"] = "main"

        return rows, {
            "layout_mode": "single_with_sidebar",
            "main_column": {
                "x0": round(main_x0, 2),
                "x1": round(main_x1, 2),
                "width": round(main_width, 2),
            },
        }

    @staticmethod
    def _to_text_key(text: str) -> str:
        normalized = LiteratureReaderService._normalize_spaces(text).lower()
        if not normalized:
            return ""
        compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)
        if not compact:
            compact = re.sub(r"\s+", "", normalized)
        return compact[:120]

    @staticmethod
    def _build_agent_style_context(raw_style_cues: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        cues = raw_style_cues if isinstance(raw_style_cues, dict) else {}
        heading_hints = [
            {
                "text": str(item.get("text") or "")[:120],
                "avg_size": float(item.get("avg_size") or 0.0),
                "bold_ratio": float(item.get("bold_ratio") or 0.0),
                "score": float(item.get("score") or 0.0),
            }
            for item in list(cues.get("heading_hints") or [])[:16]
            if isinstance(item, dict)
        ]
        noise_hints = [
            {
                "text": str(item.get("text") or "")[:80],
                "reason": str(item.get("reason") or ""),
            }
            for item in list(cues.get("noise_hints") or [])[:12]
            if isinstance(item, dict)
        ]
        line_layout = [
            {
                "text": str(item.get("text") or "")[:130],
                "text_key": str(item.get("text_key") or ""),
                "column_label": str(item.get("column_label") or "main"),
                "x0": round(float(item.get("x0") or 0.0), 2),
                "x1": round(float(item.get("x1") or 0.0), 2),
                "top": round(float(item.get("top") or 0.0), 2),
                "bottom": round(float(item.get("bottom") or 0.0), 2),
                "width": round(float(item.get("width") or 0.0), 2),
                "avg_size": round(float(item.get("avg_size") or 0.0), 2),
                "bold_ratio": round(float(item.get("bold_ratio") or 0.0), 3),
                "image_overlap_ratio": round(float(item.get("image_overlap_ratio") or 0.0), 3),
            }
            for item in list(cues.get("line_layout") or [])[:120]
            if isinstance(item, dict)
        ]
        return {
            "page": int(cues.get("page") or 0),
            "page_width": float(cues.get("page_width") or 0.0),
            "page_height": float(cues.get("page_height") or 0.0),
            "line_count": int(cues.get("line_count") or 0),
            "image_count": int(cues.get("image_count") or 0),
            "median_font_size": float(cues.get("median_font_size") or 0.0),
            "layout_mode": str(cues.get("layout_mode") or "unknown"),
            "main_column": cues.get("main_column"),
            "heading_hints": heading_hints,
            "noise_hints": noise_hints,
            "line_layout": line_layout,
        }

    @staticmethod
    def _build_summary(paragraphs: Sequence[str]) -> str:
        merged = " ".join(str(item).strip() for item in paragraphs[:2] if str(item).strip())
        merged = re.sub(r"\s+", " ", merged).strip()
        if len(merged) > 360:
            return f"{merged[:360]}..."
        return merged

    @staticmethod
    def _locate_anchor(raw_text: str, snippet: str, cursor: int) -> Tuple[int, int]:
        source = str(raw_text or "")
        needle = str(snippet or "").strip()
        if not source or not needle:
            start = max(0, min(len(source), int(cursor)))
            return start, start
        start_pos = max(0, min(len(source), int(cursor)))
        idx = source.find(needle, start_pos)
        if idx < 0:
            idx = source.lower().find(needle.lower(), start_pos)
        if idx < 0:
            idx = source.lower().find(needle.lower())
        if idx < 0:
            idx = start_pos
        end = min(len(source), idx + max(1, len(needle)))
        return idx, max(idx + 1, end)

    @staticmethod
    def _is_heading_line(text: str) -> bool:
        value = str(text or "").strip()
        if not value or len(value) > 120:
            return False
        normalized = re.sub(
            r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]{1,5})[\s\)\.\-:]+",
            "",
            value,
        ).strip().lower()
        if normalized in SECTION_KEYWORDS:
            return True
        if re.match(r"^(?:\d+(?:\.\d+){0,2}|[IVXLC]{1,5})\s+[A-Z][A-Za-z0-9 ,:;()\-]{2,90}$", value):
            return True
        if value.upper() == value and re.search(r"[A-Z]{4,}", value) and len(value.split()) <= 10:
            return True
        return False

    @staticmethod
    def _should_demote_heading_line(*, heading_text: str, next_line: str) -> bool:
        heading = LiteratureReaderService._normalize_spaces(heading_text).lower()
        if heading not in SECTION_KEYWORDS:
            return False
        nxt = LiteratureReaderService._normalize_spaces(next_line)
        if not nxt:
            return False
        if nxt[:1].islower() and len(nxt) >= 20:
            return True
        if re.match(r"^[\.,;!?]", nxt):
            return True
        return False

    @staticmethod
    def _heading_level(text: str) -> int:
        value = str(text or "").strip()
        match = re.match(r"^(\d+(?:\.\d+)*)\s+", value)
        if not match:
            return 1
        depth = len(match.group(1).split("."))
        return max(1, min(4, depth))

    @staticmethod
    def _is_caption_line(text: str) -> bool:
        return bool(re.match(r"^\s*(figure|fig\.?|table)\s*\d+[\s:.\-]", str(text or ""), flags=re.IGNORECASE))

    @staticmethod
    def _is_list_item_line(text: str) -> bool:
        return bool(re.match(r"^\s*(?:[-*]|\(?\d+\)|\d+\.)\s+", str(text or "")))

    @staticmethod
    def _estimate_structure_confidence(*, raw_text: str, heading_count: int, paragraph_count: int) -> float:
        score = 0.42
        length = len(str(raw_text or ""))
        if length >= 400:
            score += 0.08
        if length >= 1200:
            score += 0.06
        if heading_count >= 1:
            score += 0.18
        if heading_count >= 2:
            score += 0.08
        if paragraph_count >= 2:
            score += 0.12
        if heading_count == 0 and paragraph_count >= 1:
            score -= 0.08
        return max(0.0, min(0.98, score))

    def _pick_style_key(
        self,
        *,
        raw_text: str,
        sections: Sequence[Dict[str, Any]],
        style_hint: Optional[str],
    ) -> str:
        hinted = self._normalize_style_key(style_hint, fallback=None)
        if hinted:
            return hinted
        section_titles = " ".join(str(item.get("title") or "") for item in sections).lower()
        corpus = f"{section_titles}\n{str(raw_text or '').lower()[:1500]}"
        medical_terms = ("patient", "clinical", "cohort", "hospital", "trial", "disease")
        if any(term in corpus for term in medical_terms):
            return "clinical_brief"
        if "preprint" in corpus or "arxiv" in corpus:
            return "preprint_modern"
        return "journal_classic"

    @staticmethod
    def _normalize_style_key(raw: Any, fallback: Optional[str]) -> str:
        value = str(raw or "").strip().lower()
        if value in STYLE_WHITELIST:
            return value
        if fallback:
            fallback_value = str(fallback).strip().lower()
            if fallback_value in STYLE_WHITELIST:
                return fallback_value
        return "journal_classic"

    @staticmethod
    def _normalize_style_tuning(
        raw: Any,
        *,
        fallback: Optional[Any],
    ) -> Dict[str, float]:
        base = dict(DEFAULT_STYLE_TUNING)

        def _read_from(source: Any) -> None:
            if not isinstance(source, dict):
                return
            try:
                body_scale = float(source.get("body_scale"))
                base["body_scale"] = max(0.9, min(1.25, body_scale))
            except Exception:
                pass
            try:
                line_height = float(source.get("line_height"))
                base["line_height"] = max(1.55, min(2.2, line_height))
            except Exception:
                pass
            try:
                heading_scale = float(source.get("heading_scale"))
                base["heading_scale"] = max(0.95, min(1.35, heading_scale))
            except Exception:
                pass

        _read_from(fallback)
        _read_from(raw)
        return {
            "body_scale": round(float(base["body_scale"]), 3),
            "line_height": round(float(base["line_height"]), 3),
            "heading_scale": round(float(base["heading_scale"]), 3),
        }

    def _derive_style_tuning(
        self,
        *,
        style_key: str,
        style_cues: Optional[Dict[str, Any]],
    ) -> Dict[str, float]:
        cues = style_cues if isinstance(style_cues, dict) else {}
        median_size = float(cues.get("median_font_size") or 0.0)
        heading_hint_count = len(list(cues.get("heading_hints") or []))
        image_count = int(cues.get("image_count") or 0)

        body_scale = 1.0
        if median_size > 0:
            if median_size <= 9.0:
                body_scale = 0.97
            elif median_size >= 12.8:
                body_scale = 1.07

        line_height = 1.9
        if style_key == "clinical_brief":
            line_height = 1.78
        elif style_key == "preprint_modern":
            line_height = 1.84
        if heading_hint_count >= 3:
            line_height -= 0.04
        if image_count >= 2:
            line_height += 0.03

        heading_scale = 1.0
        if heading_hint_count >= 4:
            heading_scale = 1.09
        elif heading_hint_count == 0:
            heading_scale = 0.98

        return self._normalize_style_tuning(
            {
                "body_scale": body_scale,
                "line_height": line_height,
                "heading_scale": heading_scale,
            },
            fallback=None,
        )

    @staticmethod
    def _extract_json_object(content: str) -> Optional[Dict[str, Any]]:
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
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _validate_agent_repair_payload(
        self,
        *,
        payload: Dict[str, Any],
        page: int,
        raw_text: str,
        fallback_style: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        raw_len = len(str(raw_text or ""))
        blocks = payload.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return None

        normalized_blocks: List[Dict[str, Any]] = []
        for idx, item in enumerate(blocks):
            if not isinstance(item, dict):
                return None
            kind = str(item.get("kind") or "").strip()
            if kind not in {"heading", "paragraph", "list_item", "caption"}:
                return None
            text = self._normalize_spaces(str(item.get("text") or ""))
            if not text:
                return None
            anchor = item.get("source_anchor")
            if not isinstance(anchor, dict):
                return None
            page_raw = anchor.get("page")
            start_raw = anchor.get("start_char")
            end_raw = anchor.get("end_char")
            page_no = int(page_raw) if page_raw is not None else 0
            start_char = int(start_raw) if start_raw is not None else -1
            end_char = int(end_raw) if end_raw is not None else -1
            if page_no != int(page):
                return None
            if start_char < 0 or end_char <= start_char:
                return None
            if raw_len > 0 and (start_char > raw_len or end_char > raw_len):
                return None

            block_id = f"b{idx + 1}"
            canonical_block_id = f"p{int(page)}_{block_id}"
            normalized_blocks.append(
                {
                    "id": block_id,
                    "kind": kind,
                    "text": text,
                    "order": idx,
                    "section_title": str(item.get("section_title") or "").strip() or "Body",
                    "source_anchor": {
                        "page": int(page),
                        "start_char": start_char,
                        "end_char": end_char,
                        "canonical_block_id": canonical_block_id,
                        "coord_version": "anchor_v2",
                        "anchor_v2": {
                            "coord_version": "anchor_v2",
                            "canonical_block_id": canonical_block_id,
                            "page": int(page),
                            "start_char": start_char,
                            "end_char": end_char,
                        },
                    },
                    "zone_type": (
                        str(item.get("zone_type") or ("figure_meta" if kind == "caption" else "main_body"))
                        if str(item.get("zone_type") or ("figure_meta" if kind == "caption" else "main_body")) in {"main_body", "side_context", "figure_meta"}
                        else ("figure_meta" if kind == "caption" else "main_body")
                    ),
                    "column_id": str(item.get("column_id") or "main"),
                    "heading_prob": float(item.get("heading_prob") or (0.78 if kind == "heading" else 0.0)),
                    "layout_confidence": float(item.get("layout_confidence") or 0.8),
                }
            )

        section_map: Dict[str, Dict[str, Any]] = {}
        for block in normalized_blocks:
            title = str(block.get("section_title") or "Body").strip() or "Body"
            if title not in section_map:
                section_map[title] = {
                    "title": title,
                    "level": 1,
                    "block_ids": [],
                    "source_anchor": block["source_anchor"] if block["kind"] == "heading" else None,
                }
            section_map[title]["block_ids"].append(block["id"])

        sections_input = payload.get("sections")
        if isinstance(sections_input, list):
            for item in sections_input:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title or title not in section_map:
                    continue
                level = int(item.get("level") or 1)
                section_map[title]["level"] = max(1, min(4, level))

        sections = list(section_map.values())
        side_context_blocks = [
            item for item in normalized_blocks if str(item.get("zone_type") or "") == "side_context"
        ]
        figure_meta_blocks = [
            item for item in normalized_blocks if str(item.get("zone_type") or "") == "figure_meta"
        ]
        heading_blocks = [item for item in normalized_blocks if str(item.get("kind") or "") == "heading"]
        high_conf_headings = [
            item for item in heading_blocks if float(item.get("heading_prob") or 0.0) >= 0.72
        ]

        return {
            "style_key": self._normalize_style_key(payload.get("style_key"), fallback=fallback_style),
            "style_tuning": self._normalize_style_tuning(payload.get("style_tuning"), fallback=None),
            "sections": sections,
            "blocks": normalized_blocks,
            "side_context_blocks": side_context_blocks,
            "figure_meta_blocks": figure_meta_blocks,
            "toc_quality": round(len(high_conf_headings) / max(1, len(heading_blocks)), 4) if heading_blocks else 0.0,
        }

    @staticmethod
    def _normalize_absolute_link(raw: Optional[str]) -> Optional[str]:
        value = str(raw or "").strip().rstrip(").,;:")
        if not value:
            return None
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            return value
        if re.match(r"^(?:dx\.)?doi\.org/", value, flags=re.IGNORECASE):
            return f"https://{value}"
        if re.match(r"^10\.\d{4,9}/\S+", value, flags=re.IGNORECASE):
            return f"https://doi.org/{value}"
        if value.lower().startswith("www."):
            return f"https://{value}"
        return None

    @staticmethod
    def _resolve_local_pdf_path(
        *,
        user_id: int,
        paper_id: int,
        paper_title: Optional[str],
        paper_pdf_path: Optional[str],
    ) -> Optional[str]:
        candidates: List[str] = []
        if isinstance(paper_pdf_path, str) and paper_pdf_path.strip():
            candidates.append(paper_pdf_path.strip())

        upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
        safe_title = "".join(
            c for c in (paper_title or "")[:50] if c.isalnum() or c in " -_"
        ).strip()
        default_name = f"{safe_title or f'paper_{paper_id}'}_{paper_id}.pdf"
        default_path = os.path.join(upload_dir, str(user_id), "papers", default_name)
        if default_path not in candidates:
            candidates.append(default_path)

        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def _read_pdf_page_text(path: str, page: int) -> str:
        return LiteratureReaderService._read_pdf_page_text_with_pdfplumber_words(path, page)

    @staticmethod
    def _read_pdf_page_text_with_pypdf(path: str, page: int) -> str:
        try:
            import pypdf

            with open(path, "rb") as fp:
                reader = pypdf.PdfReader(fp)
                page_index = max(0, int(page) - 1)
                if page_index >= len(reader.pages):
                    return ""
                return reader.pages[page_index].extract_text() or ""
        except Exception as exc:
            logger.debug(f"[ReaderService] pypdf page extract failed page={page}: {exc}")
            return ""

    @staticmethod
    def _read_pdf_page_text_with_pdfplumber(path: str, page: int) -> str:
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                page_index = max(0, int(page) - 1)
                if page_index >= len(pdf.pages):
                    return ""
                text = pdf.pages[page_index].extract_text(x_tolerance=1.6, y_tolerance=3)
                return text or ""
        except Exception as exc:
            logger.debug(f"[ReaderService] pdfplumber page extract failed page={page}: {exc}")
            return ""

    @staticmethod
    def _read_pdf_page_text_with_pdfplumber_words(path: str, page: int) -> str:
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                page_index = max(0, int(page) - 1)
                if page_index >= len(pdf.pages):
                    return ""
                page_obj = pdf.pages[page_index]
                page_width = float(getattr(page_obj, "width", 0.0) or 0.0)
                words = page_obj.extract_words(
                    x_tolerance=1.5,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=True,
                )
                if not words:
                    return ""

                lines: List[List[str]] = []
                current_words: List[Dict[str, Any]] = []
                current_top: Optional[float] = None
                def _flush_current_words() -> None:
                    nonlocal current_words
                    if not current_words:
                        return
                    segments = LiteratureReaderService._split_words_by_spacing(
                        current_words,
                        page_width=page_width,
                    )
                    for seg in segments:
                        parts = [
                            str(item.get("text") or "").strip()
                            for item in seg
                            if str(item.get("text") or "").strip()
                        ]
                        if parts:
                            lines.append(parts)
                    current_words = []

                for item in words:
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    top_value = float(item.get("top") or item.get("doctop") or 0.0)
                    if current_top is None:
                        current_top = top_value
                        current_words = [item]
                        continue
                    if abs(top_value - current_top) <= 2.8:
                        current_words.append(item)
                        current_top = (current_top + top_value) / 2.0
                    else:
                        _flush_current_words()
                        current_words = [item]
                        current_top = top_value

                _flush_current_words()

                return "\n".join(" ".join(parts) for parts in lines if parts)
        except Exception as exc:
            logger.debug(f"[ReaderService] pdfplumber words extract failed page={page}: {exc}")
            return ""

    @staticmethod
    def _score_extraction_quality(text: str) -> float:
        value = str(text or "")
        if not value.strip():
            return -1e9
        tokens = re.findall(r"\S+", value)
        if not tokens:
            return -1e9
        lines = [line for line in value.splitlines() if line.strip()]
        camel_merges = len(re.findall(r"[a-z][A-Z]", value))
        punct_stick = len(re.findall(r"[,;:][A-Za-z]", value))
        common_word_stick = len(
            re.findall(r"\b(?:of|on|for|in|to|the|and|with|from|by)[A-Za-z]{4,}", value)
        )
        stuck_alpha_numeric = len(re.findall(r"\b[A-Za-z]{3,}\d+[A-Za-z]{2,}\b", value))
        short_alpha_tokens = sum(1 for token in tokens if token.isalpha() and len(token) <= 2)
        fragmented_upper_lines = 0
        for line in lines[:40]:
            parts = line.split()
            if len(parts) < 4:
                continue
            short_upper = sum(1 for item in parts if item.isupper() and len(item) <= 5)
            if short_upper >= 4:
                fragmented_upper_lines += 1
        # Penalize glued words and fragmented uppercase lines to improve extraction quality.
        score = (
            len(tokens) * 0.02
            + len(lines) * 0.05
            - camel_merges * 0.8
            - punct_stick * 0.35
            - common_word_stick * 0.9
            - stuck_alpha_numeric * 0.6
            - (short_alpha_tokens / max(1, len(tokens))) * 18.0
            - fragmented_upper_lines * 1.2
        )
        return score


_literature_reader_service = LiteratureReaderService()


def get_literature_reader_service() -> LiteratureReaderService:
    return _literature_reader_service



