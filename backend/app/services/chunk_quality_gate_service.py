"""
Chunk quality gate for RAG ingestion.

Design goals:
1) score chunk quality with local Ollama model,
2) mark bad chunks and attempt local repair with neighbor context,
3) strictly forbid rewritten text in repair output (only exact source substrings),
4) expose a report for document-level fail-open / fail-close decisions.
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
class ChunkDecision:
    score: float
    label: str
    issues: list[str]
    composition: list[str]
    reason: str


@dataclass
class ChunkRepair:
    content: str
    used_fragments: list[dict[str, Any]]
    rounds_used: int
    decision: ChunkDecision
    reason: str


class ChunkQualityGateService:
    SCORE_SYSTEM_PROMPT = (
        "You are a strict chunk quality gate for RAG ingestion. "
        "Return JSON only. Do not add any extra text."
    )
    SCORE_USER_PROMPT = """
Evaluate this chunk and return JSON with fields:
{
  "score": 0.0 to 1.0,
  "issues": ["..."],
  "composition": ["main_text|header|footer|caption|table_fragment|reference|noise|garbled"],
  "reason": "short explanation"
}

Scoring rubric:
- 0.85~1.00: coherent, mostly main text, little noise.
- 0.65~0.84: usable but suspicious (fragmented, mixed with captions/headers).
- 0.00~0.64: bad chunk (garbled, mostly noise, severe fragmentation, too little useful text).

Constraints:
- Judge only by given text.
- Keep output compact.

Input JSON:
{payload}
""".strip()
    REPAIR_SYSTEM_PROMPT = (
        "You repair bad RAG chunks using only exact substrings from provided sources. "
        "Never rewrite or invent text. Return JSON only."
    )
    REPAIR_USER_PROMPT = """
Repair this bad chunk with neighbor context.
Rules:
1) You MUST only copy exact substrings from source texts.
2) Never paraphrase or invent characters.
3) You may drop obvious noise lines (page numbers, repeated headers/footers).
4) Keep repaired output concise and coherent.
5) Max fragments: {max_fragments}. Max chars total: {max_chars}.

Return JSON:
{
  "fragments": [
    {"source": "self|prev_1|next_1|prev_2|next_2", "text": "exact substring"}
  ],
  "reason": "short explanation"
}

Input JSON:
{payload}
""".strip()

    def __init__(self):
        self._llm_service: Optional[LLMService] = None

    def _ensure_llm_service(self) -> LLMService:
        if self._llm_service is None:
            self._llm_service = LLMService("ollama")
            # Use dedicated gate model instead of global OLLAMA_MODEL.
            self._llm_service.config["model"] = settings.chunk_quality_gate_model
        return self._llm_service

    def _llm_available(self) -> bool:
        try:
            self._ensure_llm_service()
            return True
        except Exception as exc:
            logger.warning(f"[ChunkGate] LLM init failed: {exc}")
            return False

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _bound_score(value: Any) -> float:
        return max(0.0, min(1.0, ChunkQualityGateService._safe_float(value, 0.0)))

    @staticmethod
    def _normalize_label(score: float, *, bad_threshold: float, suspect_threshold: float) -> str:
        if score < bad_threshold:
            return "bad"
        if score < suspect_threshold:
            return "suspect"
        return "good"

    @staticmethod
    def _extract_json_value(content: str) -> Any:
        text = (content or "").strip()
        if not text:
            return {}

        fenced = re.search(
            r"```(?:json)?\s*([\s\S]*?)\s*```",
            text,
            re.IGNORECASE,
        )
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
        response = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=max_tokens,
            ),
            timeout=max(1, int(timeout_seconds)),
        )
        raw = str(response.get("content") or "")
        return self._extract_json_value(raw)

    @staticmethod
    def _clean_obvious_noise(text: str) -> str:
        if not text:
            return ""
        raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
        lines: list[str] = []
        page_no = re.compile(r"^(?:page\s*)?\d+(?:\s*/\s*\d+)?$", re.IGNORECASE)
        for line in raw.split("\n"):
            s = line.strip()
            if not s:
                continue
            if page_no.match(s):
                continue
            if re.fullmatch(r"[-_=*~·•\s]+", s):
                continue
            lines.append(s)
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    async def _evaluate_chunk(
        self,
        *,
        chunk_text: str,
        chunk_index: int,
        document_name: str,
        chunk_id: str,
    ) -> ChunkDecision:
        payload = {
            "document_name": document_name,
            "chunk_index": int(chunk_index),
            "chunk_id": chunk_id,
            "char_count": len(chunk_text or ""),
            "text": (chunk_text or "")[: max(200, int(settings.chunk_repair_max_chars_per_chunk or 1800))],
        }
        data = await self._chat_json(
            system_prompt=self.SCORE_SYSTEM_PROMPT,
            user_prompt=self.SCORE_USER_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False)),
            timeout_seconds=int(settings.chunk_quality_gate_timeout_seconds),
            max_tokens=280,
        )
        if not isinstance(data, Mapping):
            raise ValueError("score payload is not an object")

        score = self._bound_score(data.get("score"))
        issues = [
            str(item).strip()
            for item in list(data.get("issues") or [])
            if str(item).strip()
        ][:8]
        composition = [
            str(item).strip()
            for item in list(data.get("composition") or [])
            if str(item).strip()
        ][:8]
        reason = str(data.get("reason") or "").strip()
        return ChunkDecision(
            score=score,
            label=self._normalize_label(
                score,
                bad_threshold=self._safe_float(settings.chunk_quality_gate_bad_threshold, 0.5),
                suspect_threshold=self._safe_float(settings.chunk_quality_gate_suspect_threshold, 0.65),
            ),
            issues=issues,
            composition=composition,
            reason=reason,
        )

    @staticmethod
    def _build_repair_sources(
        chunks: Sequence[Mapping[str, Any]],
        *,
        chunk_index: int,
        window: int,
        max_chars: int,
    ) -> dict[str, str]:
        picked: dict[str, str] = {}

        def _text_at(idx: int) -> str:
            if idx < 0 or idx >= len(chunks):
                return ""
            return str((chunks[idx] or {}).get("content") or "")[:max_chars]

        picked["self"] = _text_at(chunk_index)
        for step in range(1, max(0, window) + 1):
            prev_text = _text_at(chunk_index - step)
            next_text = _text_at(chunk_index + step)
            if prev_text:
                picked[f"prev_{step}"] = prev_text
            if next_text:
                picked[f"next_{step}"] = next_text
        return picked

    @staticmethod
    def _build_repaired_text(
        *,
        payload: Mapping[str, Any],
        source_map: Mapping[str, str],
        max_fragments: int,
        max_chars: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        fragments_raw = payload.get("fragments")
        if not isinstance(fragments_raw, list):
            return "", []

        used: list[dict[str, Any]] = []
        buffer: list[str] = []
        total = 0
        for row in fragments_raw:
            if len(used) >= max_fragments:
                break
            if not isinstance(row, Mapping):
                continue
            source = str(row.get("source") or "").strip()
            text = str(row.get("text") or "")
            if not source or not text.strip():
                continue
            source_text = str(source_map.get(source) or "")
            if not source_text:
                continue
            # Hard constraint: exact substring only.
            if text not in source_text:
                continue
            if buffer and buffer[-1] == text:
                continue
            next_total = total + len(text)
            if next_total > max_chars:
                break
            total = next_total
            buffer.append(text)
            used.append(
                {
                    "source": source,
                    "length": len(text),
                }
            )
        if not buffer:
            return "", []
        return "\n".join(buffer).strip(), used

    async def _repair_bad_chunk(
        self,
        *,
        chunks: Sequence[Mapping[str, Any]],
        chunk_index: int,
        document_name: str,
        chunk_id: str,
    ) -> Optional[ChunkRepair]:
        if not bool(settings.chunk_repair_enabled):
            return None

        max_rounds = max(1, int(settings.chunk_repair_max_rounds or 1))
        max_fragments = max(1, int(settings.chunk_repair_max_fragments or 120))
        max_chars = max(256, int(settings.chunk_repair_max_chars_per_chunk or 1800))
        window = max(0, int(settings.chunk_quality_gate_neighbor_window or 1))

        for round_no in range(1, max_rounds + 1):
            source_map = self._build_repair_sources(
                chunks,
                chunk_index=chunk_index,
                window=window,
                max_chars=max_chars,
            )
            if not source_map.get("self"):
                return None

            payload = {
                "document_name": document_name,
                "chunk_index": int(chunk_index),
                "chunk_id": chunk_id,
                "sources": source_map,
            }
            data = await self._chat_json(
                system_prompt=self.REPAIR_SYSTEM_PROMPT,
                user_prompt=self.REPAIR_USER_PROMPT.format(
                    max_fragments=max_fragments,
                    max_chars=max_chars,
                    payload=json.dumps(payload, ensure_ascii=False),
                ),
                timeout_seconds=int(settings.chunk_quality_gate_timeout_seconds),
                max_tokens=400,
            )
            if not isinstance(data, Mapping):
                continue

            repaired_text, used_fragments = self._build_repaired_text(
                payload=data,
                source_map=source_map,
                max_fragments=max_fragments,
                max_chars=max_chars,
            )
            repaired_text = self._clean_obvious_noise(repaired_text)
            if not repaired_text:
                continue

            decision = await self._evaluate_chunk(
                chunk_text=repaired_text,
                chunk_index=chunk_index,
                document_name=document_name,
                chunk_id=chunk_id,
            )
            if decision.score >= self._safe_float(settings.chunk_quality_gate_bad_threshold, 0.5):
                return ChunkRepair(
                    content=repaired_text,
                    used_fragments=used_fragments,
                    rounds_used=round_no,
                    decision=decision,
                    reason=str(data.get("reason") or "").strip(),
                )
        return None

    @staticmethod
    def _copy_chunk(chunk: Mapping[str, Any]) -> dict[str, Any]:
        cloned = dict(chunk)
        cloned["metadata"] = dict(chunk.get("metadata") or {})
        return cloned

    async def gate_chunks(
        self,
        chunks: Sequence[Mapping[str, Any]],
        *,
        document_name: str = "",
    ) -> dict[str, Any]:
        copied_chunks = [self._copy_chunk(item) for item in list(chunks or [])]
        if not bool(settings.chunk_quality_gate_enabled):
            return {
                "chunks": copied_chunks,
                "report": {
                    "enabled": False,
                    "total_input": len(copied_chunks),
                    "total_output": len(copied_chunks),
                },
                "should_fail_document": False,
                "failure_reason": None,
            }

        if not copied_chunks:
            return {
                "chunks": [],
                "report": {
                    "enabled": True,
                    "total_input": 0,
                    "total_output": 0,
                    "checked_chunks": 0,
                },
                "should_fail_document": False,
                "failure_reason": None,
            }

        max_chunks = max(1, int(settings.chunk_quality_gate_max_chunks or 300))
        checked = copied_chunks[:max_chunks]
        tail = copied_chunks[max_chunks:]

        bad_threshold = self._safe_float(settings.chunk_quality_gate_bad_threshold, 0.5)
        suspect_threshold = self._safe_float(settings.chunk_quality_gate_suspect_threshold, 0.65)
        fail_open = bool(settings.chunk_quality_gate_fail_open)
        fail_on_unrepaired_bad = bool(settings.chunk_quality_gate_fail_on_unrepaired_bad)

        kept_chunks: list[dict[str, Any]] = []
        dropped_bad_ids: list[str] = []
        bad_count = 0
        suspect_count = 0
        repaired_count = 0
        unrepaired_bad_count = 0
        gate_error_count = 0

        llm_ready = self._llm_available()
        for idx, chunk in enumerate(checked):
            chunk_id = str(chunk.get("id") or f"chunk_{idx}")
            content = self._clean_obvious_noise(str(chunk.get("content") or ""))
            chunk["content"] = content
            meta = dict(chunk.get("metadata") or {})
            gate_meta = {
                "status": "unchecked",
                "score": 1.0,
                "label": "good",
                "issues": [],
                "composition": [],
                "repaired": False,
                "dropped": False,
            }

            if not content:
                decision = ChunkDecision(
                    score=0.0,
                    label="bad",
                    issues=["empty_after_noise_clean"],
                    composition=["noise"],
                    reason="chunk is empty after cleaning",
                )
            elif not llm_ready:
                if fail_open:
                    decision = ChunkDecision(
                        score=1.0,
                        label="good",
                        issues=["gate_llm_unavailable_fail_open"],
                        composition=[],
                        reason="gate llm unavailable",
                    )
                else:
                    decision = ChunkDecision(
                        score=0.0,
                        label="bad",
                        issues=["gate_llm_unavailable"],
                        composition=[],
                        reason="gate llm unavailable",
                    )
            else:
                try:
                    decision = await self._evaluate_chunk(
                        chunk_text=content,
                        chunk_index=idx,
                        document_name=document_name,
                        chunk_id=chunk_id,
                    )
                except Exception as exc:
                    gate_error_count += 1
                    logger.warning(f"[ChunkGate] score failed for chunk={chunk_id}: {exc}")
                    if fail_open:
                        decision = ChunkDecision(
                            score=1.0,
                            label="good",
                            issues=["gate_score_error_fail_open"],
                            composition=[],
                            reason="score call failed",
                        )
                    else:
                        decision = ChunkDecision(
                            score=0.0,
                            label="bad",
                            issues=["gate_score_error"],
                            composition=[],
                            reason="score call failed",
                        )

            label = self._normalize_label(
                decision.score,
                bad_threshold=bad_threshold,
                suspect_threshold=suspect_threshold,
            )
            gate_meta.update(
                {
                    "score": float(decision.score),
                    "label": label,
                    "issues": list(decision.issues),
                    "composition": list(decision.composition),
                    "reason": decision.reason,
                }
            )

            if label == "bad":
                bad_count += 1
                repaired: Optional[ChunkRepair] = None
                if llm_ready and bool(settings.chunk_repair_enabled):
                    try:
                        repaired = await self._repair_bad_chunk(
                            chunks=checked,
                            chunk_index=idx,
                            document_name=document_name,
                            chunk_id=chunk_id,
                        )
                    except Exception as exc:
                        gate_error_count += 1
                        logger.warning(f"[ChunkGate] repair failed for chunk={chunk_id}: {exc}")

                if repaired:
                    repaired_count += 1
                    chunk["content"] = repaired.content
                    gate_meta.update(
                        {
                            "status": "repaired",
                            "repaired": True,
                            "score": float(repaired.decision.score),
                            "label": self._normalize_label(
                                repaired.decision.score,
                                bad_threshold=bad_threshold,
                                suspect_threshold=suspect_threshold,
                            ),
                            "issues": list(repaired.decision.issues),
                            "composition": list(repaired.decision.composition),
                            "reason": repaired.decision.reason or repaired.reason,
                            "repair_rounds_used": int(repaired.rounds_used),
                            "repair_fragments": list(repaired.used_fragments),
                        }
                    )
                    kept_chunks.append(chunk)
                else:
                    unrepaired_bad_count += 1
                    gate_meta.update(
                        {
                            "status": "failed_bad",
                            "dropped": True,
                        }
                    )
                    dropped_bad_ids.append(chunk_id)
                    # Drop unrepaired bad chunks from embedding path.
            else:
                if label == "suspect":
                    suspect_count += 1
                    gate_meta["status"] = "suspect"
                else:
                    gate_meta["status"] = "good"
                kept_chunks.append(chunk)

            meta["quality_gate"] = gate_meta
            chunk["metadata"] = meta

        for offset, chunk in enumerate(tail, start=max_chunks):
            meta = dict(chunk.get("metadata") or {})
            meta["quality_gate"] = {
                "status": "skipped_limit",
                "score": 1.0,
                "label": "good",
                "issues": [],
                "composition": [],
                "repaired": False,
                "dropped": False,
                "skipped_index": int(offset),
            }
            chunk["metadata"] = meta
            kept_chunks.append(chunk)

        checked_count = len(checked)
        effective_bad_ratio = (float(unrepaired_bad_count) / float(checked_count)) if checked_count else 0.0
        fail_reason: Optional[str] = None
        should_fail = False

        if checked_count > 0 and effective_bad_ratio >= self._safe_float(settings.chunk_quality_gate_doc_fail_ratio, 0.55):
            should_fail = True
            fail_reason = f"bad_ratio_exceeded:{effective_bad_ratio:.3f}"
        if fail_on_unrepaired_bad and unrepaired_bad_count > 0:
            should_fail = True
            fail_reason = fail_reason or "unrepaired_bad_exists"

        if fail_open and gate_error_count > 0:
            # Fail-open downgrades technical gate errors to soft warnings.
            if fail_reason and fail_reason.startswith("bad_ratio_exceeded") and unrepaired_bad_count == 0:
                should_fail = False
                fail_reason = None

        report = {
            "enabled": True,
            "model": settings.chunk_quality_gate_model,
            "total_input": len(copied_chunks),
            "total_output": len(kept_chunks),
            "checked_chunks": checked_count,
            "max_chunks": max_chunks,
            "bad_threshold": bad_threshold,
            "suspect_threshold": suspect_threshold,
            "bad_count": bad_count,
            "suspect_count": suspect_count,
            "repaired_count": repaired_count,
            "unrepaired_bad_count": unrepaired_bad_count,
            "effective_bad_ratio": effective_bad_ratio,
            "dropped_bad_chunk_ids": dropped_bad_ids,
            "gate_error_count": gate_error_count,
            "fail_open": fail_open,
            "fail_on_unrepaired_bad": fail_on_unrepaired_bad,
            "doc_fail_ratio": self._safe_float(settings.chunk_quality_gate_doc_fail_ratio, 0.55),
        }
        return {
            "chunks": kept_chunks,
            "report": report,
            "should_fail_document": bool(should_fail),
            "failure_reason": fail_reason,
        }


_chunk_quality_gate_service = ChunkQualityGateService()


def get_chunk_quality_gate_service() -> ChunkQualityGateService:
    return _chunk_quality_gate_service
