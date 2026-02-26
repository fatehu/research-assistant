"""
Reader multimodal layout assist service.

仅用于结构裁决，不改写正文：
- 标题判定
- 正文/侧栏/图注三通道分流
- TOC 候选过滤
"""

from __future__ import annotations

import base64
import io
import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings


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
    """多模态布局辅助服务（低频触发）。"""

    def __init__(self) -> None:
        self._doc_stats: Dict[int, Dict[str, Any]] = {}

    def should_trigger_mm(
        self,
        *,
        paper_id: int,
        page: int,
        base_payload: Dict[str, Any],
        call_count: int = 0,
    ) -> Tuple[bool, Dict[str, Any]]:
        """根据结构质量门控与节流策略判断是否触发多模态辅助。"""
        # 中文注释：全局开关关闭时直接走纯文本链路，避免额外模型耗时和费用。
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

        # 中文注释：只有命中“结构低置信”类风险才触发多模态辅助，平稳页不触发。
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
        if not trigger_reasons:
            return False, {
                "reason": "quality_gate_not_hit",
                "structure_confidence": structure_confidence,
                "title_integrity": title_integrity,
                "sidebar_leak": sidebar_leak,
                "cross_column_merge_ratio": round(cross_column_merge_ratio, 4),
            }

        # 中文注释：按论文统计触发占比，严格控制触发率，防止整篇高频调用视觉模型。
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

        doc_trigger_ratio = len(triggered_pages) / max(1, len(seen_pages))
        max_ratio = float(getattr(settings, "reader_mm_max_doc_trigger_ratio", 0.08) or 0.08)
        if int(page) not in triggered_pages and doc_trigger_ratio >= max_ratio:
            return False, {
                "reason": "doc_trigger_ratio_exceeded",
                "structure_confidence": structure_confidence,
                "title_integrity": title_integrity,
                "sidebar_leak": sidebar_leak,
                "cross_column_merge_ratio": round(cross_column_merge_ratio, 4),
                "doc_trigger_ratio": round(doc_trigger_ratio, 4),
                "trigger_reasons": trigger_reasons,
            }

        return True, {
            "reason": "triggered",
            "structure_confidence": structure_confidence,
            "title_integrity": title_integrity,
            "sidebar_leak": sidebar_leak,
            "cross_column_merge_ratio": round(cross_column_merge_ratio, 4),
            "doc_trigger_ratio": round(doc_trigger_ratio, 4),
            "trigger_reasons": trigger_reasons,
        }

    async def build_mm_prompt_payload(
        self,
        *,
        pdf_path: str,
        page: int,
        base_payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """构建视觉辅助输入：页图 + 候选文本块（含 bbox/font/line_id）。"""
        style_cues = dict(base_payload.get("style_cues") or {})
        line_layout = list(style_cues.get("line_layout") or [])
        if not line_layout:
            return None

        image_data_url = await self._render_page_image_data_url(pdf_path=pdf_path, page=page)
        if not image_data_url:
            return None

        candidates: List[Dict[str, Any]] = []
        for idx, row in enumerate(line_layout[:140]):
            if not isinstance(row, dict):
                continue
            text = self._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            candidates.append(
                {
                    "line_id": int(row.get("line_id") or idx),
                    "text": text[:220],
                    "bbox": {
                        "x0": float(row.get("x0") or 0.0),
                        "x1": float(row.get("x1") or 0.0),
                        "top": float(row.get("top") or 0.0),
                        "bottom": float(row.get("bottom") or 0.0),
                    },
                    "font_size": float(row.get("avg_size") or 0.0),
                    "bold_ratio": float(row.get("bold_ratio") or 0.0),
                    "column_label": str(row.get("column_label") or "main"),
                }
            )
        if not candidates:
            return None

        # 中文注释：视觉输入只包含“缩略页图 + 候选行特征”，不喂整页原文重写任务。
        return {
            "image_data_url": image_data_url,
            "line_candidates": candidates,
            "layout_meta": {
                "page": int(page),
                "layout_mode": str(style_cues.get("layout_mode") or "unknown"),
                "page_width": float(style_cues.get("page_width") or 0.0),
                "page_height": float(style_cues.get("page_height") or 0.0),
                "prompt_version": str(getattr(settings, "reader_mm_prompt_version", "mm_layout_v1")),
            },
        }

    async def call_primary_then_fallback(
        self,
        *,
        prompt_payload: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """主模型失败时自动回退一次。"""
        primary_model = str(getattr(settings, "reader_mm_primary_model", "qwen3.5-flash") or "qwen3.5-flash")
        fallback_model = str(getattr(settings, "reader_mm_fallback_model", "qwen3-vl-flash") or "qwen3-vl-flash")
        timeout_ms = int(getattr(settings, "reader_mm_timeout_ms", 6000) or 6000)

        # 中文注释：主模型优先，失败后最多再尝试一次回退模型，避免长链路重试抖动。
        result = await self._call_mm_model(
            model=primary_model,
            prompt_payload=prompt_payload,
            timeout_ms=timeout_ms,
        )
        if isinstance(result, dict):
            validated = self.validate_mm_layout_json(result)
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
            )
            if isinstance(fallback, dict):
                validated = self.validate_mm_layout_json(fallback)
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

    def validate_mm_layout_json(self, payload: Any) -> Optional[Dict[str, Any]]:
        """严格校验多模态输出结构，仅接受白名单字段。"""
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
            try:
                line_id = int(row.get("line_id"))
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
            try:
                line_id = int(row.get("line_id"))
            except Exception:
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

        toc_candidates: List[int] = []
        if isinstance(toc_raw, list):
            for row in toc_raw[:80]:
                try:
                    toc_candidates.append(int(row))
                except Exception:
                    continue

        notes: List[str] = []
        if isinstance(notes_raw, list):
            for row in notes_raw[:24]:
                notes.append(self._normalize_spaces(str(row or ""))[:180])

        return {
            "headings": headings,
            "zones": zones,
            "toc_candidates": toc_candidates,
            "notes": notes,
        }

    def merge_mm_decision_into_blocks(
        self,
        *,
        base_payload: Dict[str, Any],
        mm_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """将多模态裁决融合到块结构，输出三通道与 TOC 候选。"""
        payload = json.loads(json.dumps(base_payload, ensure_ascii=False))
        raw_text = str(payload.get("raw_text") or "")
        style_cues = dict(payload.get("style_cues") or {})
        line_layout = list(style_cues.get("line_layout") or [])
        blocks = list(payload.get("blocks") or [])

        line_rows: List[Dict[str, Any]] = []
        for idx, row in enumerate(line_layout):
            if not isinstance(row, dict):
                continue
            line_id = int(row.get("line_id") or idx)
            item = dict(row)
            item["line_id"] = line_id
            line_rows.append(item)

        # 中文注释：将模型“行级判定”映射到抽取块，统一落到三通道字段。
        zone_map = {
            int(row.get("line_id")): {
                "zone_type": str(row.get("zone_type") or "main_body"),
                "column_id": str(row.get("column_id") or "main"),
            }
            for row in list(mm_decision.get("zones") or [])
            if isinstance(row, dict)
        }
        heading_map = {
            int(row.get("line_id")): {
                "heading_prob": float(row.get("heading_prob") or 0.0),
                "level": int(row.get("level") or 1),
            }
            for row in list(mm_decision.get("headings") or [])
            if isinstance(row, dict)
        }
        toc_line_ids = {int(item) for item in list(mm_decision.get("toc_candidates") or [])}

        consumed_line_ids: set[int] = set()
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
                consumed_line_ids.add(int(line_id))

            default_zone = "figure_meta" if kind == "caption" else "main_body"
            default_column = str((row or {}).get("column_label") or "main")
            if default_column.startswith("sidebar"):
                default_zone = "side_context"

            zone = zone_map.get(int(line_id)) if line_id is not None else None
            zone_type = str((zone or {}).get("zone_type") or default_zone)
            if zone_type not in {"main_body", "side_context", "figure_meta"}:
                zone_type = default_zone
            column_id = str((zone or {}).get("column_id") or default_column or "main")
            if zone_type == "side_context" and column_id == "main":
                column_id = "sidebar_auto"

            heading_prob = 0.0
            if kind == "heading":
                heading = heading_map.get(int(line_id)) if line_id is not None else None
                heading_prob = float((heading or {}).get("heading_prob") or 0.0)
                if heading_prob <= 0.0:
                    heading_prob = 0.75 if self._looks_like_heading_text(text) else 0.35
            block["zone_type"] = zone_type
            block["column_id"] = column_id[:32]
            block["heading_prob"] = round(max(0.0, min(1.0, heading_prob)), 4)
            block["layout_confidence"] = round(max(0.0, min(1.0, match_score)), 4)
            if kind == "heading" and line_id is not None and int(line_id) in toc_line_ids:
                block["toc_candidate"] = True
            merged_blocks.append(block)

        side_context_blocks: List[Dict[str, Any]] = [
            item for item in merged_blocks if str(item.get("zone_type") or "") == "side_context"
        ]
        figure_meta_blocks: List[Dict[str, Any]] = [
            item for item in merged_blocks if str(item.get("zone_type") or "") == "figure_meta"
        ]

        # 补全未被正文消费的侧栏行，避免信息丢失。
        for row in line_rows:
            line_id = int(row.get("line_id") or 0)
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
                    "order": 10000 + line_id,
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
        # 中文注释：目录质量不足时直接隐藏 TOC，避免将碎片标题暴露给用户。
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
    ) -> Optional[Dict[str, Any]]:
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_base_url", "") or "").strip()
        if not api_key or not base_url:
            return None

        user_prompt = self._build_prompt_text(prompt_payload)
        image_data_url = str(prompt_payload.get("image_data_url") or "")
        if not image_data_url:
            return None

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是论文版面结构裁决器。"
                            "只输出 JSON，不输出解释，不改写正文。"
                            "JSON 必须包含 headings/zones/toc_candidates/notes。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    },
                ],
                temperature=0,
                max_tokens=1200,
                response_format={"type": "json_object"},
                timeout=max(2.0, float(timeout_ms) / 1000.0),
            )
        except Exception as exc:  # pragma: no cover - 网络错误可接受
            logger.debug(f"[ReaderMM] model call failed model={model}: {exc}")
            return None

        content = ""
        try:
            content = str((resp.choices[0].message.content or "")).strip()
        except Exception:
            return None
        if not content:
            return None
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _build_prompt_text(self, prompt_payload: Dict[str, Any]) -> str:
        lines = list(prompt_payload.get("line_candidates") or [])
        meta = dict(prompt_payload.get("layout_meta") or {})
        schema_text = (
            '{'
            '"headings":[{"line_id":12,"heading_prob":0.91,"level":1}],'
            '"zones":[{"line_id":12,"zone_type":"main_body","column_id":"left"}],'
            '"toc_candidates":[12,18],'
            '"notes":["optional"]'
            '}'
        )
        return (
            "任务：结合页图与候选行，判断标题、栏位与区域归属。\n"
            "硬约束：\n"
            "1) 只返回 JSON；\n"
            "2) zone_type 仅允许 main_body|side_context|figure_meta；\n"
            "3) 不改写正文，不生成摘要；\n"
            "4) 优先识别侧栏并与正文分离。\n"
            f"页元信息：{json.dumps(meta, ensure_ascii=False)}\n"
            f"候选行：{json.dumps(lines, ensure_ascii=False)}\n"
            f"输出示例：{schema_text}"
        )

    async def _render_page_image_data_url(self, *, pdf_path: str, page: int) -> Optional[str]:
        """渲染页缩略图（JPEG base64），用于视觉结构辅助。"""
        try:
            import pdfplumber

            with pdfplumber.open(pdf_path) as pdf:
                idx = max(0, int(page) - 1)
                if idx >= len(pdf.pages):
                    return None
                page_obj = pdf.pages[idx]
                image = page_obj.to_image(resolution=120).original
                if image is None:
                    return None
                image.thumbnail((1280, 1280))
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
    ) -> Tuple[Optional[int], float, Optional[Dict[str, Any]]]:
        target = self._text_key(text)
        if not target:
            return None, 0.0, None
        best_id: Optional[int] = None
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
                best_id = int(row.get("line_id") or 0)
                best_row = row
        if best_id is None or best_score < 0.18:
            return None, 0.0, None
        return best_id, best_score, best_row

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
        # 兜底：保障锚点合法，避免前端校验失败。
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
