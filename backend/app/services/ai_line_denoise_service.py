"""
AI line-level denoise service for PDF ingestion.

Design:
1) ask local LLM to label noisy lines using line ids only (no rewriting),
2) run multiple parallel votes to mitigate unstable JSON from small models,
3) keep fail-open behavior for ingestion safety.
"""
from __future__ import annotations

import asyncio
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
    def _rebuild_text(lines: Sequence[LineUnit], dropped_ids: set[int], join_with_space: bool) -> str:
        kept = [line.text for line in lines if int(line.line_id) not in dropped_ids]
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

    async def denoise_text(
        self,
        text: str,
        *,
        document_name: str = "",
        file_type: str = "",
        line_spans: Optional[Sequence[Mapping[str, Any]]] = None,
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
        if not candidate_lines:
            dropped_spans = self._build_dropped_line_spans(
                dropped_ids=rule_dropped_ids,
                line_spans=sanitized_spans,
            )
            return {
                "text": "",
                "report": {
                    "enabled": True,
                    "model": settings.ai_line_denoise_model,
                    "total_lines": len(lines),
                    "batch_count": 0,
                    "parallel_votes": int(settings.ai_line_denoise_parallel_votes or 3),
                    "retry_rounds": int(settings.ai_line_denoise_retry_rounds or 2),
                    "valid_vote_count": 0,
                    "malformed_vote_count": 0,
                    "batch_error_count": 0,
                    "rule_dropped_lines": len(rule_dropped_ids),
                    "dropped_lines": len(rule_dropped_ids),
                    "dropped_line_ids": sorted(rule_dropped_ids),
                    "dropped_line_spans": dropped_spans,
                    "fail_open": bool(settings.ai_line_denoise_fail_open),
                    "line_spans_available": spans_available,
                },
            }

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

        denoised_text = self._rebuild_text(
            lines,
            dropped_ids,
            join_with_space=bool(settings.ai_line_denoise_join_lines_with_space),
        )
        if not denoised_text and bool(settings.ai_line_denoise_fail_open):
            denoised_text = self._collapse_newlines_to_spaces(text)

        dropped_spans = self._build_dropped_line_spans(
            dropped_ids=dropped_ids,
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
                "dropped_lines": len(dropped_ids),
                "dropped_line_ids": sorted(dropped_ids),
                "dropped_line_spans": dropped_spans,
                "fail_open": bool(settings.ai_line_denoise_fail_open),
                "line_spans_available": spans_available,
            },
        }


_ai_line_denoise_service = AILineDenoiseService()


def get_ai_line_denoise_service() -> AILineDenoiseService:
    return _ai_line_denoise_service
