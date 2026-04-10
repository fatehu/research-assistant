from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.core.database import async_session_factory
from app.models.conversation import Conversation
from app.services.agent_runtime_service import AgentRuntimeService, get_agent_runtime_service
from app.services.chat_context_store import (
    ConversationItemStreamStore,
    build_context_snapshot_payload,
)
from app.services.llm_service import LLMService
from app.services.react_agent import ReActAgent


@dataclass
class ConversationCompactionArtifacts:
    context_state: Dict[str, Any]
    compacted_history: Dict[str, Any]
    summary_text: str
    up_to_message_id: Optional[int]
    message_count: int
    compacted_message_count: int


class ConversationItemStreamUnavailableError(RuntimeError):
    def __init__(self, conversation_id: int):
        self.conversation_id = int(conversation_id)
        super().__init__(f"conversation item stream unavailable: {self.conversation_id}")


class ConversationContextCompactionService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_ids: set[int] = set()
        self._worker_task: Optional[asyncio.Task] = None
        self._runtime_service: AgentRuntimeService = get_agent_runtime_service()

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except Exception:
            return None

    @classmethod
    def _message_to_state_preview(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        preview: Dict[str, Any] = {
            "role": str(item.get("role", "") or "").strip().lower(),
            "content": ReActAgent._compact_debug_text(item.get("content", ""), 320),
        }
        if str(item.get("thought") or "").strip():
            preview["thought"] = ReActAgent._compact_debug_text(item.get("thought", ""), 180)
        return preview

    @classmethod
    def _tool_arguments_preview(cls, arguments: object) -> Optional[Dict[str, str]]:
        if not isinstance(arguments, dict):
            return None
        preview: Dict[str, str] = {}
        for key, value in list(arguments.items())[:6]:
            label = str(key or "").strip()
            if not label:
                continue
            if isinstance(value, str):
                rendered = value
            elif isinstance(value, (int, float, bool)):
                rendered = str(value)
            else:
                try:
                    rendered = json.dumps(value, ensure_ascii=False)
                except Exception:
                    rendered = str(value)
            compacted = ReActAgent._compact_debug_text(rendered, 120)
            if compacted:
                preview[label] = compacted
        return preview or None

    @classmethod
    def _tool_ledger_to_state_preview(cls, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = [dict(item) for item in list(entries or []) if isinstance(item, dict)]
        if not rows:
            return []

        call_arguments: Dict[str, Dict[str, str]] = {}
        fallback_calls: List[Dict[str, Any]] = []
        previews: List[Dict[str, Any]] = []

        for item in rows:
            kind = str(item.get("kind") or "").strip().lower()
            tool_name = str(item.get("tool_name") or "").strip()
            if not kind or not tool_name:
                continue
            tool_call_id = str(item.get("tool_call_id") or "").strip() or None
            if kind == "tool_call":
                args_preview = cls._tool_arguments_preview(item.get("arguments"))
                if tool_call_id and args_preview:
                    call_arguments[tool_call_id] = args_preview
                fallback_entry: Dict[str, Any] = {"kind": "tool_call", "tool_name": tool_name}
                if tool_call_id:
                    fallback_entry["tool_call_id"] = tool_call_id
                if item.get("iteration") is not None:
                    fallback_entry["iteration"] = max(int(item.get("iteration") or 0), 0)
                if args_preview:
                    fallback_entry["arguments"] = args_preview
                fallback_calls.append(fallback_entry)
                continue

            preview: Dict[str, Any] = {
                "kind": kind,
                "tool_name": tool_name,
            }
            if tool_call_id:
                preview["tool_call_id"] = tool_call_id
            if item.get("iteration") is not None:
                preview["iteration"] = max(int(item.get("iteration") or 0), 0)
            status = str(item.get("status") or "").strip()
            if status:
                preview["status"] = status
            args_preview = cls._tool_arguments_preview(item.get("arguments")) or (
                call_arguments.get(tool_call_id) if tool_call_id else None
            )
            if args_preview:
                preview["arguments"] = args_preview
            summary = ReActAgent._compact_debug_text(item.get("summary", ""), 220)
            if summary:
                preview["summary"] = summary
            error = ReActAgent._compact_debug_text(item.get("error", ""), 160)
            if error:
                preview["error"] = error
            if item.get("success") is not None:
                preview["success"] = bool(item.get("success"))
            if item.get("permission_required") is not None:
                preview["permission_required"] = bool(item.get("permission_required"))
            metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
            source_kind = str(metadata.get("source_kind") or "").strip()
            if source_kind:
                preview["source_kind"] = source_kind
            source_labels = [
                ReActAgent._compact_debug_text(label, 32)
                for label in list(metadata.get("source_labels") or [])
                if ReActAgent._compact_debug_text(label, 32)
            ]
            if source_labels:
                preview["source_labels"] = source_labels[:4]
            previews.append(preview)

        if previews:
            return previews[-16:]
        return fallback_calls[-12:]

    @classmethod
    def _metadata_provenance_hints(cls, metadata: Dict[str, Any]) -> List[str]:
        hints: List[str] = []
        for item in list(metadata.get("evidence_preview") or [])[:4]:
            if not isinstance(item, dict):
                continue
            title = ReActAgent._compact_debug_text(item.get("title") or "", 120)
            domain = ReActAgent._compact_debug_text(item.get("domain") or "", 48)
            if title:
                hints.append(f"{title} ({domain})" if domain else title)
                continue
            kb_name = ReActAgent._compact_debug_text(item.get("knowledge_base") or "", 60)
            document = ReActAgent._compact_debug_text(item.get("document") or "", 80)
            citation_label = ReActAgent._compact_debug_text(item.get("citation_label") or "", 80)
            if kb_name and document:
                hints.append(f"{kb_name} / {document}")
            elif citation_label:
                hints.append(citation_label)
        return cls._normalize_string_list(hints, max_items=4, max_chars=140)

    @classmethod
    def _tool_rows_to_evidence_candidates(cls, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in list(entries or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "tool_result":
                continue
            if item.get("success") is False:
                continue
            metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
            summary = ReActAgent._compact_debug_text(item.get("summary", ""), 180)
            if not summary:
                continue
            normalized_key = summary.lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            source_labels = cls._normalize_string_list(
                metadata.get("source_labels") or re.findall(r"(?:来源|网页)\d+", summary),
                max_items=6,
                max_chars=32,
            )
            tool_name = str(item.get("tool_name") or "").strip()
            turn_ids = [str(item.get("turn_id") or "").strip()] if str(item.get("turn_id") or "").strip() else []
            tool_call_ids = [str(item.get("tool_call_id") or "").strip()] if str(item.get("tool_call_id") or "").strip() else []
            source_kind = str(metadata.get("source_kind") or "").strip() or None
            result_count = cls._coerce_int(metadata.get("result_count"))
            retrieval_scope = dict(metadata.get("retrieval_scope") or {}) if isinstance(metadata.get("retrieval_scope"), dict) else None
            provenance_hints = cls._metadata_provenance_hints(metadata)
            candidates.append(
                {
                    "entry_id": cls._build_evidence_entry_id(
                        summary=summary,
                        tool_call_ids=tool_call_ids,
                        turn_ids=turn_ids,
                    ),
                    "origin_kind": "tool_result",
                    "summary": summary,
                    "status": "confirmed",
                    "source_kind": source_kind,
                    "source_labels": source_labels,
                    "tool_names": [tool_name] if tool_name else [],
                    "turn_ids": turn_ids,
                    "tool_call_ids": tool_call_ids,
                    "result_count": result_count,
                    "provenance_hints": provenance_hints,
                    "retrieval_scope": retrieval_scope,
                }
            )
            if len(candidates) >= 8:
                break
        return candidates

    @staticmethod
    def _build_evidence_entry_id(
        *,
        summary: str,
        tool_call_ids: Sequence[str],
        turn_ids: Sequence[str],
    ) -> str:
        normalized_summary = ReActAgent._compact_debug_text(summary, 180)
        payload = "|".join(
            [
                normalized_summary.lower(),
                ",".join(sorted(str(item or "").strip() for item in tool_call_ids if str(item or "").strip())),
                ",".join(sorted(str(item or "").strip() for item in turn_ids if str(item or "").strip())),
            ]
        )
        return f"evidence:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _require_item_stream_payload(
        conversation_id: int,
        payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if isinstance(payload, dict) and list(payload.get("entries") or []):
            return payload
        raise ConversationItemStreamUnavailableError(int(conversation_id))

    @classmethod
    def _item_stream_to_message_rows(cls, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in list(entries or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            role = str(item.get("role") or "").strip().lower()
            if kind in {"reasoning_summary", "tool_use_summary", "permission_denial"}:
                rows.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "thought": str(item.get("summary") or item.get("content") or "").strip() or None,
                    }
                )
                continue
            if kind not in {"message", "user_message", "assistant_message", "system_message"} and role not in {"user", "assistant", "system"}:
                continue
            rows.append(
                {
                    "role": role or "assistant",
                    "content": str(item.get("content") or ""),
                    "thought": str(item.get("thought") or "").strip() or None,
                }
            )
        return rows

    @classmethod
    def _item_stream_to_tool_rows(cls, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in list(entries or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in {"tool_call", "tool_result"}:
                continue
            tool_name = str(item.get("tool_name") or "").strip()
            if not tool_name:
                continue
            row: Dict[str, Any] = {
                "kind": kind,
                "tool_name": tool_name,
                "tool_call_id": str(item.get("tool_call_id") or "").strip() or None,
                "iteration": int(item.get("iteration") or 0),
                "status": str(item.get("status") or "").strip() or None,
                "arguments": dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), dict) else None,
                "summary": str(item.get("summary") or "").strip() or None,
                "success": bool(item.get("success")) if item.get("success") is not None else None,
                "error": str(item.get("error") or "").strip() or None,
                "permission_required": bool(item.get("permission_required")),
                "execution_time_ms": item.get("execution_time_ms"),
                "output_tokens_estimate": item.get("output_tokens_estimate"),
                "truncated": bool(item.get("truncated")) if item.get("truncated") is not None else None,
                "parallel_group": str(item.get("parallel_group") or "").strip() or None,
                "metadata": dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else None,
                "created_at": str(item.get("created_at") or "").strip() or None,
            }
            rows.append(row)
        return rows

    @staticmethod
    def _normalize_string_list(raw: Any, *, max_items: int, max_chars: int) -> List[str]:
        items: List[str] = []
        for item in list(raw or []):
            compacted = ReActAgent._compact_debug_text(item, max_chars)
            if compacted:
                items.append(compacted)
            if len(items) >= max_items:
                break
        return items

    @classmethod
    def _normalize_evidence_ledger(cls, raw: Any, *, max_items: int) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for item in list(raw or []):
            if isinstance(item, str):
                summary = ReActAgent._compact_debug_text(item, 180)
                if not summary:
                    continue
                normalized.append(
                    {
                        "entry_id": cls._build_evidence_entry_id(summary=summary, tool_call_ids=[], turn_ids=[]),
                        "origin_kind": "llm_inferred",
                        "summary": summary,
                        "status": "confirmed",
                        "source_kind": None,
                        "source_labels": [],
                        "tool_names": [],
                        "turn_ids": [],
                        "tool_call_ids": [],
                        "result_count": None,
                        "provenance_hints": [],
                        "retrieval_scope": None,
                    }
                )
            elif isinstance(item, dict):
                summary = ReActAgent._compact_debug_text(item.get("summary", ""), 180)
                if not summary:
                    continue
                status = str(item.get("status") or "confirmed").strip().lower()
                if status not in {"confirmed", "provisional"}:
                    status = "confirmed"
                source_labels = cls._normalize_string_list(item.get("source_labels") or [], max_items=6, max_chars=32)
                tool_names = cls._normalize_string_list(item.get("tool_names") or [], max_items=4, max_chars=48)
                turn_ids = cls._normalize_string_list(item.get("turn_ids") or [], max_items=6, max_chars=48)
                tool_call_ids = cls._normalize_string_list(item.get("tool_call_ids") or [], max_items=8, max_chars=64)
                provenance_hints = cls._normalize_string_list(item.get("provenance_hints") or [], max_items=4, max_chars=140)
                origin_kind = str(item.get("origin_kind") or "llm_inferred").strip().lower()
                if origin_kind not in {"tool_result", "assistant_summary", "llm_inferred"}:
                    origin_kind = "llm_inferred"
                source_kind = str(item.get("source_kind") or "").strip() or None
                result_count = cls._coerce_int(item.get("result_count"))
                retrieval_scope = dict(item.get("retrieval_scope") or {}) if isinstance(item.get("retrieval_scope"), dict) else None
                normalized.append(
                    {
                        "entry_id": str(item.get("entry_id") or "").strip() or cls._build_evidence_entry_id(
                            summary=summary,
                            tool_call_ids=tool_call_ids,
                            turn_ids=turn_ids,
                        ),
                        "origin_kind": origin_kind,
                        "summary": summary,
                        "status": status,
                        "source_kind": source_kind,
                        "source_labels": source_labels,
                        "tool_names": tool_names,
                        "turn_ids": turn_ids,
                        "tool_call_ids": tool_call_ids,
                        "result_count": result_count,
                        "provenance_hints": provenance_hints,
                        "retrieval_scope": retrieval_scope,
                    }
                )
            if len(normalized) >= max_items:
                break
        return normalized

    @classmethod
    def _merge_evidence_entry(cls, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        base_summary = ReActAgent._compact_debug_text(merged.get("summary", ""), 180)
        incoming_summary = ReActAgent._compact_debug_text(incoming.get("summary", ""), 180)
        merged["summary"] = base_summary or incoming_summary

        base_status = str(merged.get("status") or "confirmed").strip().lower()
        incoming_status = str(incoming.get("status") or "confirmed").strip().lower()
        if base_status not in {"confirmed", "provisional"}:
            base_status = "confirmed"
        if incoming_status not in {"confirmed", "provisional"}:
            incoming_status = "confirmed"
        merged["status"] = "confirmed" if "confirmed" in {base_status, incoming_status} else "provisional"
        base_origin = str(merged.get("origin_kind") or "llm_inferred").strip().lower()
        incoming_origin = str(incoming.get("origin_kind") or "llm_inferred").strip().lower()
        merged["origin_kind"] = (
            "tool_result"
            if "tool_result" in {base_origin, incoming_origin}
            else "assistant_summary"
            if "assistant_summary" in {base_origin, incoming_origin}
            else "llm_inferred"
        )

        merged_source_labels = cls._normalize_string_list(
            list(merged.get("source_labels") or []) + list(incoming.get("source_labels") or []),
            max_items=6,
            max_chars=32,
        )
        merged_tool_names = cls._normalize_string_list(
            list(merged.get("tool_names") or []) + list(incoming.get("tool_names") or []),
            max_items=4,
            max_chars=48,
        )
        merged_turn_ids = cls._normalize_string_list(
            list(merged.get("turn_ids") or []) + list(incoming.get("turn_ids") or []),
            max_items=6,
            max_chars=48,
        )
        merged_tool_call_ids = cls._normalize_string_list(
            list(merged.get("tool_call_ids") or []) + list(incoming.get("tool_call_ids") or []),
            max_items=8,
            max_chars=64,
        )
        merged_provenance_hints = cls._normalize_string_list(
            list(merged.get("provenance_hints") or []) + list(incoming.get("provenance_hints") or []),
            max_items=4,
            max_chars=140,
        )
        merged["source_labels"] = merged_source_labels
        merged["tool_names"] = merged_tool_names
        merged["turn_ids"] = merged_turn_ids
        merged["tool_call_ids"] = merged_tool_call_ids
        merged["provenance_hints"] = merged_provenance_hints
        merged["source_kind"] = str(merged.get("source_kind") or incoming.get("source_kind") or "").strip() or None
        merged["result_count"] = cls._coerce_int(merged.get("result_count")) or cls._coerce_int(incoming.get("result_count"))
        merged["retrieval_scope"] = (
            dict(merged.get("retrieval_scope") or {})
            if isinstance(merged.get("retrieval_scope"), dict) and merged.get("retrieval_scope")
            else dict(incoming.get("retrieval_scope") or {})
            if isinstance(incoming.get("retrieval_scope"), dict)
            else None
        )
        merged["entry_id"] = str(merged.get("entry_id") or incoming.get("entry_id") or "").strip() or cls._build_evidence_entry_id(
            summary=str(merged.get("summary") or ""),
            tool_call_ids=merged_tool_call_ids,
            turn_ids=merged_turn_ids,
        )
        return merged

    @classmethod
    def _evidence_match_key_candidates(cls, item: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        summary = str(item.get("summary") or "").strip().lower()
        if summary:
            keys.append(f"summary:{summary}")
        for tool_call_id in cls._normalize_string_list(item.get("tool_call_ids") or [], max_items=8, max_chars=64):
            keys.append(f"tool_call:{tool_call_id}")
        for turn_id in cls._normalize_string_list(item.get("turn_ids") or [], max_items=6, max_chars=48):
            keys.append(f"turn:{turn_id}")
        return keys

    @classmethod
    def _normalize_replacement_history(cls, raw: Any) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        for item in list(raw or []):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "system").strip().lower()
            if role not in {"system", "user", "assistant"}:
                role = "system"
            content = ReActAgent._compact_debug_text(item.get("content", ""), 260)
            if not content:
                continue
            entries.append({"role": role, "content": content})
            if len(entries) >= 6:
                break
        return entries

    @classmethod
    def _normalize_context_state_payload(
        cls,
        payload: Dict[str, Any],
        *,
        turn_count: int,
        evidence_candidates: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        max_open_questions = max(int(getattr(settings, "agent_context_state_open_questions_max_items", 3) or 3), 1)
        max_evidence = max(int(getattr(settings, "agent_context_state_evidence_max_items", 6) or 6), 1)

        constraints = cls._normalize_string_list(payload.get("constraints") or [], max_items=6, max_chars=180)
        open_questions = cls._normalize_string_list(
            payload.get("open_questions") or [], max_items=max_open_questions, max_chars=180
        )
        resolved_facts = cls._normalize_string_list(payload.get("resolved_facts") or [], max_items=6, max_chars=180)
        candidate_evidence = cls._normalize_evidence_ledger(evidence_candidates or [], max_items=max_evidence)
        soft_evidence = cls._normalize_evidence_ledger(payload.get("evidence_ledger") or [], max_items=max_evidence)
        evidence_ledger = [dict(item) for item in candidate_evidence]
        evidence_index: Dict[str, int] = {}
        for idx, item in enumerate(evidence_ledger):
            for key in cls._evidence_match_key_candidates(item):
                evidence_index[key] = idx
        for candidate in soft_evidence:
            match_idx = next(
                (evidence_index[key] for key in cls._evidence_match_key_candidates(candidate) if key in evidence_index),
                None,
            )
            if match_idx is not None:
                evidence_ledger[match_idx] = cls._merge_evidence_entry(evidence_ledger[match_idx], candidate)
                for key in cls._evidence_match_key_candidates(evidence_ledger[match_idx]):
                    evidence_index[key] = match_idx
                continue
            candidate = dict(candidate)
            candidate["entry_id"] = str(candidate.get("entry_id") or "").strip() or cls._build_evidence_entry_id(
                summary=str(candidate.get("summary") or ""),
                tool_call_ids=list(candidate.get("tool_call_ids") or []),
                turn_ids=list(candidate.get("turn_ids") or []),
            )
            evidence_ledger.append(candidate)
            new_idx = len(evidence_ledger) - 1
            for key in cls._evidence_match_key_candidates(candidate):
                evidence_index[key] = new_idx
            if len(evidence_ledger) >= max_evidence:
                break

        seen_resolved = {item.strip().lower() for item in resolved_facts if str(item or "").strip()}
        for evidence in evidence_ledger:
            if not isinstance(evidence, dict):
                continue
            if str(evidence.get("status") or "").strip().lower() != "confirmed":
                continue
            summary = ReActAgent._compact_debug_text(evidence.get("summary", ""), 180)
            if not summary:
                continue
            normalized_summary = summary.lower()
            if normalized_summary in seen_resolved:
                continue
            resolved_facts.append(summary)
            seen_resolved.add(normalized_summary)
            if len(resolved_facts) >= 6:
                break

        return {
            "version": "conversation_context_state.v3",
            "active_topic": ReActAgent._compact_debug_text(payload.get("active_topic", ""), 220),
            "user_goal": ReActAgent._compact_debug_text(payload.get("user_goal", ""), 220),
            "constraints": constraints[:6],
            "open_questions": open_questions[:max_open_questions],
            "resolved_facts": resolved_facts[:6],
            "evidence_ledger": evidence_ledger[:max_evidence],
            "last_reasoning_summary": ReActAgent._compact_debug_text(payload.get("last_reasoning_summary", ""), 180),
            "turn_count": int(max(0, turn_count)),
            "updated_at": datetime.utcnow().isoformat(),
        }

    @classmethod
    def _normalize_compacted_history_payload(
        cls,
        payload: Dict[str, Any],
        *,
        compacted_message_count: int,
        up_to_message_id: Optional[int],
    ) -> Dict[str, Any]:
        return {
            "version": "conversation_compacted_history.v2",
            "history_anchors": ReActAgent._compact_debug_text(payload.get("history_anchors", ""), 800),
            "history_summary": ReActAgent._compact_debug_text(payload.get("history_summary", ""), 1200),
            "compact_boundary_message_id": int(up_to_message_id) if up_to_message_id else None,
            "replacement_history": cls._normalize_replacement_history(payload.get("replacement_history") or []),
            "compacted_message_count": int(max(0, compacted_message_count)),
            "up_to_message_id": int(up_to_message_id) if up_to_message_id else None,
            "updated_at": datetime.utcnow().isoformat(),
        }

    @classmethod
    async def _extract_context_state(
        cls,
        messages: Sequence[Dict[str, Any]],
        *,
        tool_ledger_entries: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        rows = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        user_turns = sum(1 for item in rows if str(item.get("role", "")).strip().lower() == "user")
        if not rows or user_turns <= 0:
            return {}

        provider = str(getattr(settings, "agent_context_state_provider", "aliyun") or "aliyun").strip()
        model_name = str(getattr(settings, "agent_context_state_model", "qwen3.5-flash") or "qwen3.5-flash").strip()
        max_tokens = max(int(getattr(settings, "agent_context_state_max_tokens", 420) or 420), 160)

        previews = [cls._message_to_state_preview(item) for item in rows[-32:]]
        tool_previews = cls._tool_ledger_to_state_preview(tool_ledger_entries or [])
        evidence_candidates = cls._tool_rows_to_evidence_candidates(tool_ledger_entries or [])
        payload = {
            "recent_messages": previews,
            "tool_ledger_preview": tool_previews,
            "evidence_candidates": evidence_candidates,
            "message_count": len(rows),
            "user_turns": user_turns,
        }
        system_prompt = (
            "你是会话上下文状态提取器。"
            "请根据给定的多轮对话、工具账本预览和推理摘要，提取持续性的会话状态。"
            "只输出严格 JSON，不要带 markdown，不要解释。"
            "字段固定为："
            "{\"active_topic\":\"...\",\"user_goal\":\"...\",\"constraints\":[...],"
            "\"open_questions\":[...],\"resolved_facts\":[...],\"evidence_ledger\":[{\"summary\":\"...\",\"status\":\"confirmed\",\"source_labels\":[...],\"tool_names\":[...],\"turn_ids\":[...],\"tool_call_ids\":[...]}],\"last_reasoning_summary\":\"...\"}。"
            "要求："
            "1. active_topic 必须是稳定主题，不要填'继续'、'这个'、'为什么以前没发现'这类跟进句。"
            "2. user_goal 必须描述当前会话正在解决的任务。"
            "3. constraints 只保留仍然有效的用户约束。"
            "4. open_questions 只保留尚未解决的问题。"
            "5. resolved_facts 只保留已经相对稳定、后续回答可直接复用的事实。"
            "6. evidence_ledger 只保留已获得且后续可复用的证据、来源或检索结论，优先根据 tool_ledger_preview 提炼。"
            "7. 如果 evidence_candidates 已经给出了稳定证据，不要遗漏，除非它们明显与当前主题无关。"
            "8. evidence_ledger 中 source_labels 只写类似 来源1 这样的标签，不要抄整段 observation。"
            "9. 如果 evidence_candidates 已提供 turn_ids 或 tool_call_ids，优先保留这些归属信息。"
            "10. last_reasoning_summary 只保留最近一轮仍有后续价值的推理摘要，没有就输出空字符串。"
        )

        llm = LLMService(provider)
        llm.config = dict(llm.config)
        llm.config["model"] = model_name
        response = await llm.chat(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        parsed = ReActAgent._extract_json_object(response.get("content") or "")
        if not parsed:
            return {}
        return cls._normalize_context_state_payload(
            parsed,
            turn_count=user_turns,
            evidence_candidates=evidence_candidates,
        )

    @classmethod
    async def _extract_compacted_history(
        cls,
        messages: Sequence[Dict[str, Any]],
        *,
        up_to_message_id: Optional[int],
        tool_ledger_entries: Optional[Sequence[Dict[str, Any]]] = None,
        force_compact: bool = False,
    ) -> Dict[str, Any]:
        rows = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        sanitized = [ReActAgent._sanitize_message_for_context(item) for item in rows]
        recent_turns = max(int(getattr(settings, "agent_context_window_turns", 8) or 8), 1)
        recently_slid_turns = max(int(getattr(settings, "agent_context_recently_slid_turns", 2) or 2), 0)
        older, recently_slid, recent = ReActAgent._split_context_windows(
            sanitized,
            recent_turns=recent_turns,
            recently_slid_turns=recently_slid_turns,
        )
        compact_source = older + recently_slid
        if not compact_source and force_compact:
            user_turns = sum(1 for item in sanitized if str(item.get("role", "")).strip().lower() == "user")
            if user_turns > 2:
                # Manual compact should still fold older turns even if the
                # default recent window has not started sliding history yet.
                older, recently_slid, recent = ReActAgent._split_context_windows(
                    sanitized,
                    recent_turns=2,
                    recently_slid_turns=0,
                )
                compact_source = older + recently_slid
        if not compact_source:
            return {}

        provider = str(getattr(settings, "agent_context_state_provider", "aliyun") or "aliyun").strip()
        model_name = str(getattr(settings, "agent_context_state_model", "qwen3.5-flash") or "qwen3.5-flash").strip()
        max_tokens = max(int(getattr(settings, "agent_context_state_max_tokens", 420) or 420), 160)

        tool_previews = cls._tool_ledger_to_state_preview(tool_ledger_entries or [])
        payload = {
            "history_to_compact": [cls._message_to_state_preview(item) for item in compact_source],
            "recent_context": [cls._message_to_state_preview(item) for item in recent[-4:]],
            "tool_ledger_preview": tool_previews,
            "message_count": len(rows),
            "compacted_message_count": len(compact_source),
        }
        system_prompt = (
            "你是会话历史压缩器。请把给定的历史对话压缩成两层：history_anchors 和 history_summary。"
            "只输出严格 JSON，不要带 markdown，不要解释。"
            "格式固定为：{\"history_anchors\":\"...\",\"history_summary\":\"...\",\"replacement_history\":[{\"role\":\"system\",\"content\":\"...\"}]}。"
            "要求："
            "1. history_anchors 是稳定锚点，保留早期目标、主题、仍有效约束、关键已知事实。"
            "2. history_summary 是比 anchors 更完整的摘要，用于在原文被裁掉后继续对话。"
            "3. 不要写'继续'、'这个'、'那为什么'这类跟进句做主题。"
            "4. 不要复述最近窗口里的原文，只压缩被滑出的历史。"
            "5. 如果 tool_ledger_preview 提供了稳定证据或工具结论，把它们纳入 anchors/summary，但不要逐条复述工具日志。"
            "6. replacement_history 用 2-6 条简短 message 形式表达“压缩后还能继续运行的替代历史”，role 只能是 system、user、assistant。"
        )

        llm = LLMService(provider)
        llm.config = dict(llm.config)
        llm.config["model"] = model_name
        response = await llm.chat(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        parsed = ReActAgent._extract_json_object(response.get("content") or "")
        if not parsed:
            return {}
        return cls._normalize_compacted_history_payload(
            parsed,
            compacted_message_count=len(compact_source),
            up_to_message_id=up_to_message_id,
        )

    @classmethod
    async def build_artifacts(
        cls,
        messages: Sequence[Dict[str, Any]],
        *,
        tool_ledger_entries: Optional[Sequence[Dict[str, Any]]] = None,
        up_to_message_id: Optional[int] = None,
        force_compact: bool = False,
    ) -> ConversationCompactionArtifacts:
        rows = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        state = await cls._extract_context_state(rows, tool_ledger_entries=tool_ledger_entries)
        compacted_history = await cls._extract_compacted_history(
            rows,
            up_to_message_id=up_to_message_id,
            tool_ledger_entries=tool_ledger_entries,
            force_compact=force_compact,
        )
        compacted_message_count = int(compacted_history.get("compacted_message_count") or 0)
        summary_text = str(compacted_history.get("history_summary") or "").strip()

        return ConversationCompactionArtifacts(
            context_state=state,
            compacted_history=compacted_history,
            summary_text=summary_text,
            up_to_message_id=None,
            message_count=len(rows),
            compacted_message_count=compacted_message_count,
        )

    async def _compact_conversation(
        self,
        conversation_id: int,
        *,
        mode: str = "auto",
    ) -> Optional[ConversationCompactionArtifacts]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Conversation)
                .where(Conversation.id == int(conversation_id))
            )
            conversation = result.scalar_one_or_none()
            if not conversation:
                return None

            item_stream_payload = self._require_item_stream_payload(
                int(conversation_id),
                await self._runtime_service.get_conversation_item_stream(int(conversation_id)),
            )
            item_stream = ConversationItemStreamStore.from_payload(item_stream_payload)
            canonical = item_stream.canonical_history()
            latest_message_id = next(
                (
                    int(entry.message_id)
                    for entry in reversed(list(canonical.active_entries or []))
                    if entry.message_id is not None
                ),
                None,
            )
            payload_rows = item_stream.canonical_replay_rows()
            tool_rows = self._item_stream_to_tool_rows([entry.__dict__ for entry in canonical.active_entries])
        current_compacted_history_payload = await self._runtime_service.get_conversation_compacted_history(int(conversation_id))
        current_compacted_history = (
            dict(current_compacted_history_payload)
            if isinstance(current_compacted_history_payload, dict)
            else {}
        )

        existing_boundary_message_id = self._coerce_int(
            current_compacted_history.get("compact_boundary_message_id")
            or current_compacted_history.get("up_to_message_id")
        )
        if existing_boundary_message_id is not None and (
            latest_message_id is None or latest_message_id <= existing_boundary_message_id
        ):
            current_context_state = dict(await self._runtime_service.get_conversation_context_state(int(conversation_id)) or {})
            logger.info(
                "[ConversationCompaction] skip conversation_id={} mode={} latest_message_id={} boundary_message_id={}",
                conversation_id,
                mode,
                latest_message_id,
                existing_boundary_message_id,
            )
            return ConversationCompactionArtifacts(
                context_state=current_context_state,
                compacted_history=current_compacted_history,
                summary_text=str(current_compacted_history.get("history_summary") or "").strip(),
                up_to_message_id=existing_boundary_message_id,
                message_count=len(payload_rows),
                compacted_message_count=0,
            )
        artifacts = await self.build_artifacts(
            payload_rows,
            tool_ledger_entries=tool_rows,
            up_to_message_id=latest_message_id,
            force_compact=(str(mode).strip().lower() == "manual"),
        )

        if artifacts.context_state:
            state_payload = dict(artifacts.context_state)
            state_payload["updated_at"] = state_payload.get("updated_at") or ""
            await self._runtime_service.upsert_conversation_context_state(conversation_id, state_payload)
        if artifacts.compacted_history:
            await self._runtime_service.upsert_conversation_compacted_history(
                conversation_id,
                dict(artifacts.compacted_history),
            )

        await self._runtime_service.append_conversation_history_event(
            int(conversation_id),
            title=f"{mode}_compact",
            detail=(
                f"compacted_messages={artifacts.compacted_message_count}, "
                f"summary_chars={len(artifacts.summary_text or '')}, "
                f"up_to_message_id={latest_message_id or 0}"
            ),
        )
        await self._runtime_service.append_conversation_context_snapshot(
            int(conversation_id),
            build_context_snapshot_payload(
                mode=mode,
                context_state=artifacts.context_state,
                compacted_history=artifacts.compacted_history,
                summary_text=artifacts.summary_text,
                compacted_message_count=artifacts.compacted_message_count,
                up_to_message_id=latest_message_id,
            ),
        )
        if artifacts.compacted_history:
            await self._runtime_service.append_conversation_item_entries(
                int(conversation_id),
                [
                    {
                        "kind": "compact_boundary",
                        "role": "system",
                        "content": artifacts.summary_text,
                        "summary": str(artifacts.compacted_history.get("history_anchors") or "").strip() or None,
                        "status": mode,
                        "message_id": latest_message_id,
                        "metadata": {
                            "compact_boundary_message_id": artifacts.compacted_history.get("compact_boundary_message_id"),
                            "replacement_history": list(artifacts.compacted_history.get("replacement_history") or []),
                            "compacted_message_count": artifacts.compacted_message_count,
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            )

        logger.info(
            "[ConversationCompaction] conversation_id={} messages={} compacted_messages={} summary_chars={} state_keys={} compacted_keys={}",
            conversation_id,
            artifacts.message_count,
            artifacts.compacted_message_count,
            len(artifacts.summary_text or ""),
            sorted(list(artifacts.context_state.keys())),
            sorted(list(artifacts.compacted_history.keys())),
        )
        return artifacts

    async def compact_now(self, conversation_id: int) -> ConversationCompactionArtifacts:
        artifacts = await self._compact_conversation(int(conversation_id), mode="manual")
        if artifacts is None:
            raise ValueError(f"conversation not found: {conversation_id}")
        return artifacts

    async def _worker(self) -> None:
        while True:
            conversation_id = await self._queue.get()
            try:
                await self._compact_conversation(conversation_id, mode="auto")
            except asyncio.CancelledError:
                raise
            except ConversationItemStreamUnavailableError as exc:
                logger.warning(
                    "[ConversationCompaction] skipped conversation_id={} code=conversation_item_stream_missing",
                    exc.conversation_id,
                )
            except Exception as exc:
                logger.exception("[ConversationCompaction] failed for conversation_id={}: {}", conversation_id, exc)
            finally:
                self._queued_ids.discard(int(conversation_id))
                self._queue.task_done()

    def start_background_worker(self) -> Dict[str, Any]:
        enabled = bool(getattr(settings, "conversation_context_compaction_enabled", True))
        if not enabled:
            return {"enabled": False, "running": False, "queued": 0}
        if self._worker_task and not self._worker_task.done():
            return {"enabled": True, "running": True, "queued": self._queue.qsize()}
        self._worker_task = asyncio.create_task(self._worker(), name="conversation-context-compaction")
        return {"enabled": True, "running": True, "queued": self._queue.qsize()}

    def enqueue_conversation(self, conversation_id: Optional[int]) -> None:
        if not bool(getattr(settings, "conversation_context_compaction_enabled", True)):
            return
        if not conversation_id:
            return
        conv_id = int(conversation_id)
        if conv_id in self._queued_ids:
            return
        self._queued_ids.add(conv_id)
        try:
            self._queue.put_nowait(conv_id)
        except Exception:
            self._queued_ids.discard(conv_id)
            raise

    async def shutdown(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


_conversation_context_compaction_service = ConversationContextCompactionService()


def get_conversation_context_compaction_service() -> ConversationContextCompactionService:
    return _conversation_context_compaction_service
