"""
AI line-level denoise service for PDF ingestion.

Design:
1) ask local LLM to label noisy lines using line ids only (no rewriting),
2) run multiple parallel votes to mitigate unstable JSON from small models,
3) keep fail-open behavior for ingestion safety.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService


@dataclass
class LineUnit:
    line_id: int
    text: str


class AILineDenoiseService:
    SYSTEM_PROMPT = (
        "You are a strict OCR/PDF line denoise assistant. "
        "Return JSON only. Never rewrite text."
    )
    USER_PROMPT = """
Review extracted lines and decide which lines are obvious noise.

Rules:
1) Output line ids only.
2) Keep meaningful title/body/author/affiliation/reference lines.
3) Drop only obvious noise, e.g. repeated garbage like A1111, symbol separators, standalone page counters.
4) Be conservative. If uncertain, keep.

Return JSON:
{{
  "keep_line_ids": [1,2,3],
  "drop_line_ids": [4,5],
  "reason": "short reason"
}}

Input JSON:
{payload}
""".strip()

    _NOISE_TOKEN_RE = re.compile(
        r"([a-zA-Z])\1{4,}|([0-9])\2{4,}|[a-zA-Z]\d{3,}|(?:\d[a-zA-Z]){3,}",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self._llm_service: Optional[LLMService] = None
        self._ocr_llm_service: Optional[LLMService] = None

    def _ensure_llm_service(self) -> LLMService:
        if self._llm_service is None:
            self._llm_service = LLMService("ollama")
            self._llm_service.config["model"] = settings.ai_line_denoise_model
        return self._llm_service

    def _llm_available(self) -> bool:
        try:
            self._ensure_llm_service()
            return True
        except Exception as exc:
            logger.warning(f"[AILineDenoise] LLM init failed: {exc}")
            return False

    def _ensure_ocr_llm_service(self) -> LLMService:
        if self._ocr_llm_service is None:
            self._ocr_llm_service = LLMService("ollama")
            self._ocr_llm_service.config["model"] = settings.ai_line_denoise_drop_ocr_model
        return self._ocr_llm_service

    @staticmethod
    def _extract_json_value(content: str) -> Any:
        text = (content or "").strip()
        if not text:
            return {}

        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        return {}

    async def _chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int,
        max_tokens: int,
    ) -> Any:
        llm = self._ensure_llm_service()
        request = {
            "model": llm.config["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        response = None
        try:
            response = await asyncio.wait_for(
                llm.client.chat.completions.create(
                    **request,
                    extra_body={"reasoning": {"effort": "none"}},
                ),
                timeout=max(1, int(timeout_seconds)),
            )
        except Exception as exc:
            message = str(exc).lower()
            disable_reasoning_unsupported = (
                "reasoning" in message
                or "cannot unmarshal" in message
                or "invalid_request_error" in message
            )
            if not disable_reasoning_unsupported:
                raise
            response = await asyncio.wait_for(
                llm.client.chat.completions.create(**request),
                timeout=max(1, int(timeout_seconds)),
            )

        msg = response.choices[0].message
        raw = str(getattr(msg, "content", "") or "")
        if not raw:
            raw = str(getattr(msg, "reasoning", "") or getattr(msg, "reasoning_content", "") or "")
        return self._extract_json_value(raw)

    @staticmethod
    def _normalize_lines(text: str) -> list[LineUnit]:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines: list[LineUnit] = []
        line_id = 1
        for line in raw.split("\n"):
            cleaned = re.sub(r"\s+", " ", line).strip()
            if not cleaned:
                continue
            lines.append(LineUnit(line_id=line_id, text=cleaned))
            line_id += 1
        return lines

    @staticmethod
    def _looks_noisy_line(text: str) -> bool:
        s = str(text or "").strip()
        if not s:
            return True
        if re.fullmatch(r"(?:page\s*)?\d+(?:\s*/\s*\d+)?", s, re.IGNORECASE):
            return True
        if re.fullmatch(r"[-_=*~·•\s]+", s):
            return True
        if AILineDenoiseService._NOISE_TOKEN_RE.search(s):
            return True
        alpha = sum(1 for ch in s if ch.isalpha())
        digit = sum(1 for ch in s if ch.isdigit())
        if len(s) >= 8 and digit / len(s) > 0.45 and alpha / len(s) < 0.35:
            return True
        return False

    @staticmethod
    def _is_hard_noise_line(text: str) -> bool:
        s = str(text or "").strip()
        if not s:
            return True
        if re.fullmatch(r"[a-zA-Z]?\d{4,}", s):
            return True
        if re.fullmatch(r"(?:[a-zA-Z]\d+){1,3}", s):
            return True
        if re.fullmatch(r"[-_=*~·•\s]{4,}", s):
            return True
        compact = re.sub(r"[^a-zA-Z0-9]+", "", s).lower()
        if compact and len(compact) >= 8 and len(set(compact)) <= 2 and any(ch.isdigit() for ch in compact):
            return True
        return False

    @staticmethod
    def _coerce_ids(value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        output: list[int] = []
        for item in value:
            try:
                output.append(int(item))
            except Exception:
                continue
        return output

    @staticmethod
    def _build_batches(lines: Sequence[LineUnit], max_lines_per_call: int) -> list[list[LineUnit]]:
        max_lines = max(1, int(max_lines_per_call or 60))
        if not lines:
            return []
        batches: list[list[LineUnit]] = []
        for i in range(0, len(lines), max_lines):
            batches.append(list(lines[i:i + max_lines]))
        return batches

    async def _review_batch_once(
        self,
        *,
        document_name: str,
        batch_lines: Sequence[LineUnit],
        vote_index: int,
    ) -> Any:
        payload = {
            "document_name": document_name,
            "vote_index": int(vote_index),
            "line_count": len(batch_lines),
            "lines": [
                {
                    "line_id": int(line.line_id),
                    "text": str(line.text)[:400],
                }
                for line in batch_lines
            ],
        }
        return await self._chat_json(
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=self.USER_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False)),
            timeout_seconds=int(settings.ai_line_denoise_timeout_seconds),
            max_tokens=220,
        )

    async def _review_batch(
        self,
        *,
        document_name: str,
        batch_lines: Sequence[LineUnit],
    ) -> tuple[set[int], int, int]:
        votes = max(1, int(settings.ai_line_denoise_parallel_votes or 3))
        retry_rounds = max(1, int(settings.ai_line_denoise_retry_rounds or 2))
        responses: list[Any] = []
        for round_no in range(retry_rounds):
            tasks = [
                self._review_batch_once(
                    document_name=document_name,
                    batch_lines=batch_lines,
                    vote_index=round_no * votes + i + 1,
                )
                for i in range(votes)
            ]
            round_responses = await asyncio.gather(*tasks, return_exceptions=True)
            responses.extend(round_responses)

        allowed = {int(line.line_id) for line in batch_lines}
        line_map = {int(line.line_id): str(line.text) for line in batch_lines}
        drop_votes: dict[int, int] = {}
        keep_votes: dict[int, int] = {}
        malformed_count = 0
        valid_count = 0

        for item in responses:
            if isinstance(item, Exception):
                malformed_count += 1
                continue
            if not isinstance(item, Mapping):
                malformed_count += 1
                continue

            raw_drop = set(self._coerce_ids(item.get("drop_line_ids"))) & allowed
            raw_keep = set(self._coerce_ids(item.get("keep_line_ids"))) & allowed
            if not raw_drop and not raw_keep:
                malformed_count += 1
                continue

            valid_count += 1
            for line_id in raw_drop:
                drop_votes[line_id] = drop_votes.get(line_id, 0) + 1
            for line_id in raw_keep:
                keep_votes[line_id] = keep_votes.get(line_id, 0) + 1

        if valid_count <= 0:
            return set(), malformed_count, valid_count

        majority = valid_count // 2 + 1
        dropped: set[int] = set()
        for line_id in allowed:
            drop_count = drop_votes.get(line_id, 0)
            keep_count = keep_votes.get(line_id, 0)
            if drop_count >= majority and drop_count > keep_count:
                if self._looks_noisy_line(line_map.get(line_id, "")):
                    dropped.add(line_id)
        return dropped, malformed_count, valid_count

    @staticmethod
    def _rebuild_text(
        lines: Sequence[LineUnit],
        dropped_ids: set[int],
        join_with_space: bool,
        replacements: Optional[Mapping[int, str]] = None,
    ) -> str:
        recovered = dict(replacements or {})
        kept: list[str] = []
        for line in lines:
            line_id = int(line.line_id)
            if line_id in dropped_ids:
                replacement = re.sub(r"\s+", " ", str(recovered.get(line_id) or "")).strip()
                if replacement:
                    kept.append(replacement)
                continue
            kept.append(line.text)
        if not kept:
            return ""
        if join_with_space:
            return re.sub(r"\s+", " ", " ".join(kept)).strip()
        return "\n".join(kept).strip()

    @staticmethod
    def _collapse_newlines_to_spaces(text: str) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\s+", " ", raw).strip()

    @staticmethod
    def _sanitize_line_spans(line_spans: Optional[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
        if not line_spans:
            return []
        output: list[dict[str, Any]] = []
        for row in line_spans:
            if not isinstance(row, Mapping):
                continue
            try:
                line_id = int(row.get("line_id"))
            except Exception:
                continue
            if line_id <= 0:
                continue
            normalized: dict[str, Any] = {
                "line_id": int(line_id),
                "text": str(row.get("text") or "")[:500],
            }
            for key in ("page", "x0", "y0", "x1", "y1", "page_width", "page_height", "coord_space"):
                value = row.get(key)
                if value is not None:
                    normalized[key] = value
            output.append(normalized)
        output.sort(key=lambda item: int(item.get("line_id") or 0))
        return output

    @staticmethod
    def _build_dropped_line_spans(
        *,
        dropped_ids: set[int],
        line_spans: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not dropped_ids or not line_spans:
            return []
        dropped: list[dict[str, Any]] = []
        for row in line_spans:
            try:
                line_id = int(row.get("line_id"))  # type: ignore[arg-type]
            except Exception:
                continue
            if line_id not in dropped_ids:
                continue
            payload: dict[str, Any] = {"line_id": line_id}
            for key in ("text", "page", "x0", "y0", "x1", "y1", "page_width", "page_height", "coord_space"):
                value = row.get(key)  # type: ignore[union-attr]
                if value is not None:
                    payload[key] = value
            dropped.append(payload)
        return dropped

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    async def _ocr_drop_line_once(
        self,
        *,
        model: str,
        timeout_seconds: int,
        line_id: int,
        original_text: str,
        image_data_url: str,
    ) -> dict[str, Any]:
        ocr_llm = self._ensure_ocr_llm_service()
        user_content = [
            {
                "type": "text",
                "text": (
                    "Read OCR from image for one dropped PDF line.\n"
                    "Return JSON only: "
                    '{"line_id": <int>, "ocr_text": "...", "confidence": 0.0-1.0, "use_recovered": true/false, "reason": "..."}\n'
                    "Rules:\n"
                    "1) Prefer exact visible text from image.\n"
                    "2) Keep punctuation/case as seen.\n"
                    "3) If unreadable, set ocr_text=\"\" and use_recovered=false.\n"
                    f"line_id={int(line_id)}\n"
                    f"original_text={str(original_text)[:600]}"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": str(image_data_url)},
            },
        ]
        request = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict OCR validator for PDF lines. "
                        "Output compact JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            "temperature": 0.0,
            "max_tokens": 220,
        }
        response = None
        try:
            response = await asyncio.wait_for(
                ocr_llm.client.chat.completions.create(
                    **request,
                    extra_body={"reasoning": {"effort": "none"}},
                ),
                timeout=max(1, int(timeout_seconds)),
            )
        except Exception as exc:
            message = str(exc).lower()
            disable_reasoning_unsupported = (
                "reasoning" in message
                or "cannot unmarshal" in message
                or "invalid_request_error" in message
            )
            if not disable_reasoning_unsupported:
                raise
            response = await asyncio.wait_for(
                ocr_llm.client.chat.completions.create(**request),
                timeout=max(1, int(timeout_seconds)),
            )

        msg = response.choices[0].message
        raw = str(getattr(msg, "content", "") or "")
        if not raw:
            raw = str(getattr(msg, "reasoning", "") or getattr(msg, "reasoning_content", "") or "")
        parsed = self._extract_json_value(raw)
        if not isinstance(parsed, Mapping):
            parsed = {}
        conf = max(0.0, min(1.0, self._safe_float(parsed.get("confidence"), 0.0)))
        ocr_text = re.sub(r"\s+", " ", str(parsed.get("ocr_text") or "")).strip()
        use_recovered = bool(parsed.get("use_recovered"))
        reason = str(parsed.get("reason") or "").strip()
        return {
            "line_id": int(line_id),
            "ocr_text": ocr_text,
            "confidence": conf,
            "use_recovered": use_recovered,
            "reason": reason,
        }

    @staticmethod
    def _render_drop_ocr_crops_sync(
        *,
        pdf_path: str,
        spans: Sequence[Mapping[str, Any]],
        dpi: int,
        image_max_side: int,
        pad_ratio: float,
    ) -> list[dict[str, Any]]:
        import pdfplumber

        rows_by_page: dict[int, list[Mapping[str, Any]]] = {}
        for row in spans:
            try:
                page = int(row.get("page") or 0)
            except Exception:
                page = 0
            if page <= 0:
                continue
            rows_by_page.setdefault(page, []).append(row)

        output: list[dict[str, Any]] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page, page_rows in rows_by_page.items():
                idx = int(page) - 1
                if idx < 0 or idx >= len(pdf.pages):
                    continue
                page_obj = pdf.pages[idx]
                page_width = float(getattr(page_obj, "width", 0.0) or 0.0)
                page_height = float(getattr(page_obj, "height", 0.0) or 0.0)
                if page_width <= 0 or page_height <= 0:
                    continue

                image = page_obj.to_image(resolution=max(96, int(dpi or 180))).original
                if image is None:
                    continue
                if int(image_max_side or 0) > 0:
                    image.thumbnail((int(image_max_side), int(image_max_side)))
                img_width = float(image.size[0] or 0.0)
                img_height = float(image.size[1] or 0.0)
                if img_width <= 1 or img_height <= 1:
                    continue
                scale_x = img_width / page_width
                scale_y = img_height / page_height

                for span in page_rows:
                    x0 = AILineDenoiseService._safe_float(span.get("x0"), -1.0)
                    y0 = AILineDenoiseService._safe_float(span.get("y0"), -1.0)
                    x1 = AILineDenoiseService._safe_float(span.get("x1"), -1.0)
                    y1 = AILineDenoiseService._safe_float(span.get("y1"), -1.0)
                    if x1 <= x0 or y1 <= y0:
                        continue

                    top_pdf = page_height - y1
                    bottom_pdf = page_height - y0
                    pad_x = max(1.0, (x1 - x0) * max(0.0, float(pad_ratio)))
                    pad_y = max(1.0, (bottom_pdf - top_pdf) * max(0.0, float(pad_ratio)))
                    left = max(0, int(round((x0 - pad_x) * scale_x)))
                    top = max(0, int(round((top_pdf - pad_y) * scale_y)))
                    right = min(int(img_width), int(round((x1 + pad_x) * scale_x)))
                    bottom = min(int(img_height), int(round((bottom_pdf + pad_y) * scale_y)))
                    if right <= left + 4 or bottom <= top + 4:
                        continue

                    crop = image.crop((left, top, right, bottom))
                    if crop.size[0] <= 4 or crop.size[1] <= 4:
                        continue

                    buffer = io.BytesIO()
                    crop.save(buffer, format="JPEG", quality=86, optimize=True)
                    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    output.append(
                        {
                            "line_id": int(span.get("line_id") or 0),
                            "page": int(page),
                            "original_text": str(span.get("text") or ""),
                            "image_data_url": f"data:image/jpeg;base64,{encoded}",
                        }
                    )
        output.sort(key=lambda item: int(item.get("line_id") or 0))
        return output

    async def _recover_dropped_lines_with_ocr(
        self,
        *,
        pdf_path: str,
        dropped_spans: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not dropped_spans:
            return {
                "recovered_map": {},
                "attempted": 0,
                "recovered": 0,
                "errors": 0,
                "rows": [],
            }

        selected_spans: list[Mapping[str, Any]] = []
        for row in dropped_spans:
            if len(selected_spans) >= max(1, int(settings.ai_line_denoise_drop_ocr_max_lines or 64)):
                break
            if not isinstance(row, Mapping):
                continue
            if row.get("x0") is None or row.get("y0") is None or row.get("x1") is None or row.get("y1") is None:
                continue
            selected_spans.append(row)

        if not selected_spans:
            return {
                "recovered_map": {},
                "attempted": 0,
                "recovered": 0,
                "errors": 0,
                "rows": [],
            }

        try:
            crops = await asyncio.to_thread(
                self._render_drop_ocr_crops_sync,
                pdf_path=str(pdf_path),
                spans=selected_spans,
                dpi=max(96, int(settings.ai_line_denoise_drop_ocr_dpi or 180)),
                image_max_side=max(256, int(settings.ai_line_denoise_drop_ocr_image_max_side or 768)),
                pad_ratio=max(0.0, float(settings.ai_line_denoise_drop_ocr_pad_ratio or 0.06)),
            )
        except Exception as exc:
            logger.warning(f"[AILineDenoise] drop OCR crop render failed: {exc}")
            return {
                "recovered_map": {},
                "attempted": 0,
                "recovered": 0,
                "errors": 1,
                "rows": [],
            }

        if not crops:
            return {
                "recovered_map": {},
                "attempted": 0,
                "recovered": 0,
                "errors": 0,
                "rows": [],
            }

        ocr_model = str(settings.ai_line_denoise_drop_ocr_model or settings.ai_line_denoise_model).strip()
        threshold = max(0.0, min(1.0, self._safe_float(settings.ai_line_denoise_drop_ocr_confidence_threshold, 0.6)))
        timeout_seconds = max(2, int(settings.ai_line_denoise_drop_ocr_timeout_seconds or 24))
        semaphore = asyncio.Semaphore(max(1, int(settings.ai_line_denoise_drop_ocr_max_parallel or 3)))

        async def run_one(row: Mapping[str, Any]) -> Any:
            async with semaphore:
                return await self._ocr_drop_line_once(
                    model=ocr_model,
                    timeout_seconds=timeout_seconds,
                    line_id=int(row.get("line_id") or 0),
                    original_text=str(row.get("original_text") or ""),
                    image_data_url=str(row.get("image_data_url") or ""),
                )

        results = await asyncio.gather(*(run_one(row) for row in crops), return_exceptions=True)
        recovered_map: dict[int, str] = {}
        rows: list[dict[str, Any]] = []
        errors = 0
        recovered_count = 0
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                errors += 1
                rows.append(
                    {
                        "line_id": int((crops[idx] or {}).get("line_id") or 0) if idx < len(crops) else 0,
                        "accepted": False,
                        "confidence": 0.0,
                        "ocr_text": "",
                        "reason": f"error:{type(result).__name__}",
                    }
                )
                continue
            line_id = int(result.get("line_id") or 0)
            ocr_text = re.sub(r"\s+", " ", str(result.get("ocr_text") or "")).strip()
            confidence = max(0.0, min(1.0, self._safe_float(result.get("confidence"), 0.0)))
            use_recovered = bool(result.get("use_recovered"))
            accepted = (
                line_id > 0
                and use_recovered
                and bool(ocr_text)
                and confidence >= threshold
                and not self._looks_noisy_line(ocr_text)
            )
            if accepted:
                recovered_map[line_id] = ocr_text
                recovered_count += 1
            rows.append(
                {
                    "line_id": line_id,
                    "accepted": bool(accepted),
                    "confidence": confidence,
                    "ocr_text": ocr_text[:320],
                    "reason": str(result.get("reason") or "")[:160],
                }
            )
        rows.sort(key=lambda item: int(item.get("line_id") or 0))
        return {
            "recovered_map": recovered_map,
            "attempted": len(crops),
            "recovered": recovered_count,
            "errors": int(errors),
            "rows": rows,
        }

    async def denoise_text(
        self,
        text: str,
        *,
        document_name: str = "",
        file_type: str = "",
        line_spans: Optional[Sequence[Mapping[str, Any]]] = None,
        pdf_path: str = "",
    ) -> dict[str, Any]:
        normalized_type = (file_type or "").lower().replace(".", "")
        sanitized_spans = self._sanitize_line_spans(line_spans)
        spans_available = bool(sanitized_spans)
        if normalized_type and normalized_type != "pdf":
            return {
                "text": str(text or ""),
                "report": {
                    "enabled": False,
                    "reason": f"skip_non_pdf:{normalized_type}",
                    "line_spans_available": spans_available,
                },
            }

        lines = self._normalize_lines(text)
        if not lines:
            return {
                "text": "",
                "report": {
                    "enabled": bool(settings.ai_line_denoise_enabled),
                    "total_lines": 0,
                    "dropped_lines": 0,
                    "line_spans_available": spans_available,
                },
            }

        if not bool(settings.ai_line_denoise_enabled):
            return {
                "text": self._collapse_newlines_to_spaces(text),
                "report": {
                    "enabled": False,
                    "total_lines": len(lines),
                    "dropped_lines": 0,
                    "line_spans_available": spans_available,
                },
            }

        if not self._llm_available():
            if bool(settings.ai_line_denoise_fail_open):
                return {
                    "text": self._collapse_newlines_to_spaces(text),
                    "report": {
                        "enabled": True,
                        "total_lines": len(lines),
                        "dropped_lines": 0,
                        "fail_open": True,
                        "reason": "llm_unavailable",
                        "line_spans_available": spans_available,
                    },
                }
            return {
                "text": "",
                "report": {
                    "enabled": True,
                    "total_lines": len(lines),
                    "dropped_lines": len(lines),
                    "fail_open": False,
                    "reason": "llm_unavailable",
                    "line_spans_available": spans_available,
                },
            }

        rule_dropped_ids = {
            int(line.line_id)
            for line in lines
            if self._is_hard_noise_line(line.text)
        }
        candidate_lines = [line for line in lines if int(line.line_id) not in rule_dropped_ids]

        batches = self._build_batches(candidate_lines, int(settings.ai_line_denoise_max_lines_per_call or 60))
        semaphore = asyncio.Semaphore(max(1, int(settings.ai_line_denoise_max_parallel_batches or 3)))
        dropped_ids: set[int] = set(rule_dropped_ids)
        malformed_count = 0
        valid_vote_count = 0

        async def run_batch(batch: Sequence[LineUnit]) -> tuple[set[int], int, int]:
            async with semaphore:
                return await self._review_batch(document_name=document_name, batch_lines=batch)

        results = await asyncio.gather(*(run_batch(batch) for batch in batches), return_exceptions=True)
        batch_error_count = 0
        for result in results:
            if isinstance(result, Exception):
                batch_error_count += 1
                logger.warning(f"[AILineDenoise] batch failed: {result}")
                continue
            drop_set, malformed, valid_votes = result
            dropped_ids.update(drop_set)
            malformed_count += int(malformed)
            valid_vote_count += int(valid_votes)

        if batch_error_count > 0 and not bool(settings.ai_line_denoise_fail_open):
            dropped_spans = self._build_dropped_line_spans(
                dropped_ids={int(line.line_id) for line in lines},
                line_spans=sanitized_spans,
            )
            return {
                "text": "",
                "report": {
                    "enabled": True,
                    "total_lines": len(lines),
                    "dropped_lines": len(lines),
                    "dropped_line_ids": [int(line.line_id) for line in lines],
                    "dropped_line_spans": dropped_spans,
                    "batch_error_count": batch_error_count,
                    "fail_open": False,
                    "reason": "batch_error",
                    "line_spans_available": spans_available,
                },
            }

        dropped_spans = self._build_dropped_line_spans(
            dropped_ids=dropped_ids,
            line_spans=sanitized_spans,
        )
        ocr_recovered_map: dict[int, str] = {}
        ocr_attempted = 0
        ocr_recovered = 0
        ocr_errors = 0
        ocr_rows: list[dict[str, Any]] = []
        drop_ocr_enabled = bool(settings.ai_line_denoise_drop_ocr_enabled)
        if (
            drop_ocr_enabled
            and bool(pdf_path)
            and dropped_spans
            and spans_available
        ):
            ocr_result = await self._recover_dropped_lines_with_ocr(
                pdf_path=str(pdf_path),
                dropped_spans=dropped_spans,
            )
            ocr_recovered_map = {
                int(k): str(v)
                for k, v in dict(ocr_result.get("recovered_map") or {}).items()
                if str(v or "").strip()
            }
            ocr_attempted = int(ocr_result.get("attempted") or 0)
            ocr_recovered = int(ocr_result.get("recovered") or 0)
            ocr_errors = int(ocr_result.get("errors") or 0)
            ocr_rows = list(ocr_result.get("rows") or [])

        final_dropped_ids = {
            int(line_id)
            for line_id in dropped_ids
            if int(line_id) not in ocr_recovered_map
        }
        denoised_text = self._rebuild_text(
            lines,
            dropped_ids,
            join_with_space=bool(settings.ai_line_denoise_join_lines_with_space),
            replacements=ocr_recovered_map,
        )
        if not denoised_text and bool(settings.ai_line_denoise_fail_open):
            denoised_text = self._collapse_newlines_to_spaces(text)

        final_dropped_spans = self._build_dropped_line_spans(
            dropped_ids=final_dropped_ids,
            line_spans=sanitized_spans,
        )
        return {
            "text": denoised_text,
            "report": {
                "enabled": True,
                "model": settings.ai_line_denoise_model,
                "total_lines": len(lines),
                "batch_count": len(batches),
                "parallel_votes": int(settings.ai_line_denoise_parallel_votes or 3),
                "retry_rounds": int(settings.ai_line_denoise_retry_rounds or 2),
                "valid_vote_count": int(valid_vote_count),
                "malformed_vote_count": int(malformed_count),
                "batch_error_count": int(batch_error_count),
                "rule_dropped_lines": len(rule_dropped_ids),
                "raw_dropped_lines": len(dropped_ids),
                "raw_dropped_line_ids": sorted(dropped_ids),
                "dropped_lines": len(final_dropped_ids),
                "dropped_line_ids": sorted(final_dropped_ids),
                "dropped_line_spans": final_dropped_spans,
                "drop_ocr_enabled": drop_ocr_enabled,
                "drop_ocr_model": str(settings.ai_line_denoise_drop_ocr_model or ""),
                "drop_ocr_attempted": int(ocr_attempted),
                "drop_ocr_recovered": int(ocr_recovered),
                "drop_ocr_errors": int(ocr_errors),
                "drop_ocr_recovered_line_ids": sorted(int(k) for k in ocr_recovered_map.keys()),
                "drop_ocr_rows": ocr_rows[:80],
                "fail_open": bool(settings.ai_line_denoise_fail_open),
                "line_spans_available": spans_available,
            },
        }


_ai_line_denoise_service = AILineDenoiseService()


def get_ai_line_denoise_service() -> AILineDenoiseService:
    return _ai_line_denoise_service
