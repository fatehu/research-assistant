"""
Persistence helpers for agent runtime traces, summaries and long-term memory.
"""

from __future__ import annotations

import uuid
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import delete, desc, func, select

from app.config import settings
from app.core.database import async_session_factory
from app.models.agent import (
    AgentMemoryItem,
    AgentRun,
    AgentStepRecord,
)
from app.models.conversation import Conversation
from app.models.user import User
from app.services.chat_context_store import (
    ConversationItemEntry,
    ConversationItemStreamStore,
    ConversationTurnEntry,
    ConversationTurnStore,
    HistoryLog,
    ToolLedgerEntry,
    ToolLedgerStore,
)
from app.services.embedding_service import get_embedding_service


@dataclass
class MemoryContext:
    content: str
    score: float
    created_at: str


@dataclass
class PreparedSendPlanRecord:
    plan_id: str
    user_id: int
    conversation_id: Optional[int]
    llm_provider: str
    draft_message: str
    preview_mode: str
    conversation_revision: Optional[str]
    draft_hash: str
    system_prompt: str
    llm_messages: List[Dict[str, Any]]
    routing_decision: Optional[Dict[str, Any]]
    tool_selection: Dict[str, Any]
    chat_preferences: Dict[str, Any]
    rag_overrides: Dict[str, Any]
    conversation_state: Dict[str, Any]
    compacted_history: Dict[str, Any]
    prefetched_rag_messages: List[Dict[str, Any]]
    prefetched_rag_metadata: Dict[str, Any]
    created_at: str
    expires_at: str


class AgentRuntimeService:
    """Service for persisting and loading agent runtime artifacts."""

    def __init__(self) -> None:
        self._prepared_send_plans: Dict[str, PreparedSendPlanRecord] = {}

    @staticmethod
    def _normalize_optional_text(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        if text.lower() in {"none", "null"}:
            return None
        return text

    @staticmethod
    def _normalize_chat_preferences(raw: Any) -> Dict[str, Any]:
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        language = str(payload.get("response_language") or "auto").strip()
        if language not in {"auto", "zh-CN", "en-US"}:
            language = "auto"
        verbosity = str(payload.get("response_verbosity") or "balanced").strip()
        if verbosity not in {"concise", "balanced", "detailed"}:
            verbosity = "balanced"
        web_search = str(payload.get("web_search") or "ask").strip()
        if web_search not in {"ask", "avoid", "allow_when_needed"}:
            web_search = "ask"
        updated_at = str(payload.get("updated_at") or "").strip() or None
        return {
            "version": "chat_preferences.v1",
            "response_language": language,
            "response_verbosity": verbosity,
            "web_search": web_search,
            "updated_at": updated_at or datetime.utcnow().isoformat(),
        }

    @staticmethod
    def normalize_chat_preference_overrides(raw: Any) -> Dict[str, Any]:
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        normalized: Dict[str, Any] = {}
        if "response_language" in payload:
            language = str(payload.get("response_language") or "auto").strip()
            if language in {"auto", "zh-CN", "en-US"}:
                normalized["response_language"] = language
        if "response_verbosity" in payload:
            verbosity = str(payload.get("response_verbosity") or "balanced").strip()
            if verbosity in {"concise", "balanced", "detailed"}:
                normalized["response_verbosity"] = verbosity
        if "web_search" in payload:
            web_search = str(payload.get("web_search") or "ask").strip()
            if web_search in {"ask", "avoid", "allow_when_needed"}:
                normalized["web_search"] = web_search
        return normalized

    @staticmethod
    def normalize_chat_rag_overrides(raw: Any) -> Dict[str, Any]:
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        if not payload or not bool(payload.get("enabled", False)):
            return {}

        def _normalize_id_list(values: Any) -> List[int]:
            normalized: List[int] = []
            for item in list(values or []):
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0 and value not in normalized:
                    normalized.append(value)
            return normalized

        scope_mode = str(payload.get("scope_mode") or "all").strip()
        if scope_mode not in {"all", "knowledge_base", "document"}:
            scope_mode = "all"

        knowledge_base_ids = _normalize_id_list(payload.get("knowledge_base_ids"))
        document_ids = _normalize_id_list(payload.get("document_ids"))

        if scope_mode == "knowledge_base" and not knowledge_base_ids:
            scope_mode = "all"
        elif scope_mode == "document":
            if not document_ids and knowledge_base_ids:
                scope_mode = "knowledge_base"
            elif not document_ids:
                scope_mode = "all"

        normalized: Dict[str, Any] = {
            "version": "chat_rag_overrides.v1",
            "enabled": True,
            "scope_mode": scope_mode,
            "knowledge_base_ids": knowledge_base_ids,
            "document_ids": document_ids,
        }
        query_rewrite_profile = str(payload.get("query_rewrite_profile") or "").strip().lower()
        if query_rewrite_profile not in {"off", "light", "deep"}:
            if payload.get("use_query_rewrite") is not None:
                query_rewrite_profile = "light" if bool(payload.get("use_query_rewrite")) else "off"
            else:
                query_rewrite_profile = ""
        if query_rewrite_profile:
            normalized["query_rewrite_profile"] = query_rewrite_profile
            normalized["use_query_rewrite"] = query_rewrite_profile != "off"
        for key in (
            "use_reranker",
            "use_hybrid",
            "use_contextual_compression",
        ):
            if key in payload and payload.get(key) is not None:
                normalized[key] = bool(payload.get(key))
        return normalized

    @classmethod
    def merge_chat_preferences(cls, base: Any, overrides: Any) -> Dict[str, Any]:
        normalized_base = cls._normalize_chat_preferences(base)
        normalized_overrides = cls.normalize_chat_preference_overrides(overrides)
        if not normalized_overrides:
            return normalized_base
        merged = dict(normalized_base)
        merged.update(normalized_overrides)
        merged["updated_at"] = datetime.utcnow().isoformat()
        return merged

    @classmethod
    def extract_chat_preference_candidates(
        cls,
        *,
        draft_message: str,
        confirmed_preferences: Any,
    ) -> List[Dict[str, Any]]:
        text = str(draft_message or "").strip()
        if not text:
            return []

        confirmed = cls._normalize_chat_preferences(confirmed_preferences)
        lowered = text.lower()
        compact_text = " ".join(text.split())
        candidates: List[Dict[str, Any]] = []

        def _append_candidate(
            *,
            key: str,
            suggested_value: str,
            reason: str,
            source_excerpt: str,
        ) -> None:
            if confirmed.get(key) == suggested_value:
                return
            candidate_id = hashlib.sha256(
                f"{key}|{suggested_value}|{source_excerpt}".encode("utf-8")
            ).hexdigest()[:16]
            if any(item.get("candidate_id") == candidate_id for item in candidates):
                return
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "key": key,
                    "suggested_value": suggested_value,
                    "reason": reason,
                    "source_excerpt": source_excerpt[:120],
                    "source_kind": "draft",
                }
            )

        if re.search(r"(用中文|中文回答|中文输出|中文讲|请用中文)", compact_text, re.IGNORECASE):
            _append_candidate(
                key="response_language",
                suggested_value="zh-CN",
                reason="草稿里明确要求中文回答。",
                source_excerpt=compact_text,
            )
        elif re.search(r"(用英文|英文回答|英文输出|english|in english)", lowered, re.IGNORECASE):
            _append_candidate(
                key="response_language",
                suggested_value="en-US",
                reason="草稿里明确要求英文回答。",
                source_excerpt=compact_text,
            )

        if re.search(r"(简洁|简短|一句话|简要|简单说)", compact_text, re.IGNORECASE):
            _append_candidate(
                key="response_verbosity",
                suggested_value="concise",
                reason="草稿里要求更短、更快的表达。",
                source_excerpt=compact_text,
            )
        elif re.search(r"(详细|展开|具体|深入|系统地|全面)", compact_text, re.IGNORECASE):
            _append_candidate(
                key="response_verbosity",
                suggested_value="detailed",
                reason="草稿里要求展开说明。",
                source_excerpt=compact_text,
            )

        if re.search(r"(不要联网|别联网|不要上网|别上网|不要搜索|不要查网)", compact_text, re.IGNORECASE):
            _append_candidate(
                key="web_search",
                suggested_value="avoid",
                reason="草稿里明确要求避免联网或搜索。",
                source_excerpt=compact_text,
            )
        elif re.search(r"(联网|上网查|查一下|搜一下|搜索一下|检索一下)", compact_text, re.IGNORECASE):
            _append_candidate(
                key="web_search",
                suggested_value="allow_when_needed",
                reason="草稿里表达了搜索/联网诉求。",
                source_excerpt=compact_text,
            )

        return candidates[:6]

    def _cleanup_expired_send_plans(self) -> None:
        now = datetime.utcnow()
        expired = [
            plan_id
            for plan_id, record in self._prepared_send_plans.items()
            if datetime.fromisoformat(record.expires_at) <= now
        ]
        for plan_id in expired:
            self._prepared_send_plans.pop(plan_id, None)

    @staticmethod
    def _hash_draft_message(value: str) -> str:
        normalized = str(value or "").strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def get_conversation_revision(self, conversation_id: Optional[int]) -> Optional[str]:
        if conversation_id is None:
            return None
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = dict(row.metadata_ or {})
            item_stream_payload = metadata.get("item_stream")
            turn_store_payload = metadata.get("turn_store")
            compacted_history_payload = metadata.get("compacted_history")
            context_state_payload = metadata.get("context_state")
            parts = [
                str(conversation_id),
                str((row.updated_at.isoformat() if getattr(row, "updated_at", None) else "")),
                str(((item_stream_payload or {}).get("updated_at") if isinstance(item_stream_payload, dict) else "")),
                str(((turn_store_payload or {}).get("updated_at") if isinstance(turn_store_payload, dict) else "")),
                str(((compacted_history_payload or {}).get("updated_at") if isinstance(compacted_history_payload, dict) else "")),
                str(((context_state_payload or {}).get("updated_at") if isinstance(context_state_payload, dict) else "")),
            ]
            return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _memory_default_channels() -> List[str]:
        raw = str(getattr(settings, "agent_memory_default_channels", "") or "").strip()
        if not raw:
            return ["chat", "codelab_agent", "notebook_agent", "literature_agent"]
        channels: List[str] = []
        for item in raw.split(","):
            value = str(item).strip()
            if value and value not in channels:
                channels.append(value)
        return channels or ["chat", "codelab_agent", "notebook_agent", "literature_agent"]

    async def get_user_memory_control(self, *, user_id: int, channel: Optional[str] = None) -> Dict[str, Any]:
        user_preferences: Dict[str, Any] = {}
        async with async_session_factory() as db:
            row = await db.get(User, int(user_id))
            if row and isinstance(row.preferences, dict):
                user_preferences = dict(row.preferences)

        memory_cfg = user_preferences.get("agent_memory") if isinstance(user_preferences, dict) else {}
        if not isinstance(memory_cfg, dict):
            memory_cfg = {}

        enabled_channels = memory_cfg.get("enabled_channels")
        if isinstance(enabled_channels, list):
            normalized_channels: List[str] = []
            for item in enabled_channels:
                value = str(item or "").strip()
                if value and value not in normalized_channels:
                    normalized_channels.append(value)
            enabled_channels = normalized_channels
        else:
            enabled_channels = self._memory_default_channels()

        system_enabled = bool(getattr(settings, "agent_longterm_memory_enabled", False))
        user_enabled = bool(memory_cfg.get("enabled", False))
        channel_enabled = True if not channel else str(channel).strip() in enabled_channels
        effective_enabled = bool(system_enabled and user_enabled and channel_enabled)
        return {
            "system_enabled": system_enabled,
            "user_enabled": user_enabled,
            "effective_enabled": effective_enabled,
            "enabled_channels": enabled_channels,
            "updated_at": memory_cfg.get("updated_at"),
        }

    async def get_user_chat_preferences(self, *, user_id: int) -> Dict[str, Any]:
        async with async_session_factory() as db:
            row = await db.get(User, int(user_id))
            preferences = dict(row.preferences or {}) if row and isinstance(row.preferences, dict) else {}
        raw = preferences.get("chat_preferences") if isinstance(preferences, dict) else {}
        return self._normalize_chat_preferences(raw)

    async def clear_memories(
        self,
        *,
        user_id: int,
        channel: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> int:
        async with async_session_factory() as db:
            stmt = delete(AgentMemoryItem).where(AgentMemoryItem.user_id == int(user_id))
            if channel:
                stmt = stmt.where(AgentMemoryItem.channel == str(channel))
            if scope_type:
                stmt = stmt.where(AgentMemoryItem.scope_type == str(scope_type))
            if scope_id is not None:
                stmt = stmt.where(AgentMemoryItem.scope_id == str(scope_id))
            result = await db.execute(stmt)
            await db.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def create_run(
        self,
        *,
        user_id: int,
        channel: str,
        conversation_id: Optional[int] = None,
        notebook_id: Optional[str] = None,
        intent: Optional[str] = None,
        selected_tools: Optional[List[str]] = None,
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        async with async_session_factory() as db:
            record = AgentRun(
                id=run_id,
                user_id=user_id,
                channel=channel,
                conversation_id=conversation_id,
                notebook_id=notebook_id,
                intent=intent,
                selected_tools=selected_tools or [],
                model_provider=model_provider,
                model_name=model_name,
                status="running",
                metadata_=metadata or {},
                started_at=datetime.utcnow(),
            )
            db.add(record)
            await db.commit()
        return run_id

    async def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        iteration_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with async_session_factory() as db:
            result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
            record = result.scalar_one_or_none()
            if not record:
                return
            record.status = status
            record.prompt_tokens = int(prompt_tokens or 0)
            record.completion_tokens = int(completion_tokens or 0)
            record.total_tokens = int(total_tokens or 0)
            record.iteration_count = int(iteration_count or 0)
            record.finished_at = datetime.utcnow()
            if metadata:
                merged = dict(record.metadata_ or {})
                merged.update(metadata)
                record.metadata_ = merged
            await db.commit()

    async def append_steps(self, run_id: str, steps: Iterable[Dict[str, Any]]) -> None:
        payload = list(steps)
        if not payload:
            return

        async with async_session_factory() as db:
            for idx, step in enumerate(payload, start=1):
                data = step.get("data", {}) if isinstance(step, dict) else {}
                tool_data = data.get("data", {}) if isinstance(data, dict) else {}
                metadata = {}
                if isinstance(data, dict):
                    for key in ("iteration", "tool_call_id", "parallel_group"):
                        if key in data:
                            metadata[key] = data.get(key)

                step_type = str(step.get("type", "unknown"))
                if step_type in {"thought", "content", "answer", "error"}:
                    text_content = data if not isinstance(data, dict) else step.get("content", "")
                    content = str(text_content or "")
                else:
                    content = str(data.get("output", "")) if isinstance(data, dict) else ""

                db.add(
                    AgentStepRecord(
                        run_id=run_id,
                        step_index=idx,
                        step_type=step_type,
                        content=content,
                        tool_name=str(data.get("tool", "")) if isinstance(data, dict) else None,
                        tool_input=data.get("input") if isinstance(data, dict) else None,
                        tool_output=str(data.get("output", "")) if isinstance(data, dict) else None,
                        tool_success=bool(data.get("success")) if isinstance(data, dict) and data.get("success") is not None else None,
                        execution_time_ms=float(data.get("execution_time_ms", 0.0)) if isinstance(data, dict) else 0.0,
                        output_tokens_estimate=int(data.get("output_tokens_estimate", 0)) if isinstance(data, dict) else 0,
                        truncated=bool(data.get("truncated", False)) if isinstance(data, dict) else False,
                        retry_attempt=int(tool_data.get("retry_attempt", 0) or 0) if isinstance(tool_data, dict) else None,
                        metadata_=metadata,
                    )
                )
            await db.commit()

    @staticmethod
    def _json_safe_payload(payload: Any) -> Any:
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            return str(payload)

    async def append_chat_run_event(
        self,
        run_id: str,
        *,
        event: str,
        data: Any,
        created_at: Optional[str] = None,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()
        event_name = str(event or "").strip()
        if not normalized_run_id or not event_name:
            return

        safe_data = self._json_safe_payload(data)
        if isinstance(safe_data, str):
            content = safe_data
        elif isinstance(safe_data, dict):
            content = str(safe_data.get("answer") or safe_data.get("content") or safe_data.get("message") or "")
        else:
            content = ""
        metadata = {
            "chat_background_event": True,
            "event": event_name,
            "payload": safe_data,
            "created_at": str(created_at or datetime.utcnow().isoformat()),
        }

        async with async_session_factory() as db:
            max_index_result = await db.execute(
                select(func.max(AgentStepRecord.step_index)).where(AgentStepRecord.run_id == normalized_run_id)
            )
            next_index = int(max_index_result.scalar() or 0) + 1
            db.add(
                AgentStepRecord(
                    run_id=normalized_run_id,
                    step_index=next_index,
                    step_type="chat_event",
                    content=content,
                    metadata_=metadata,
                )
            )
            await db.commit()

    async def list_chat_run_events(self, run_id: str, *, limit: int = 1000) -> List[Dict[str, Any]]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return []
        bounded_limit = min(max(int(limit or 1000), 1), 5000)

        async with async_session_factory() as db:
            result = await db.execute(
                select(AgentStepRecord)
                .where(
                    AgentStepRecord.run_id == normalized_run_id,
                    AgentStepRecord.step_type == "chat_event",
                )
                .order_by(AgentStepRecord.step_index.asc(), AgentStepRecord.id.asc())
                .limit(bounded_limit)
            )
            rows = list(result.scalars().all())

        events: List[Dict[str, Any]] = []
        for row in rows:
            metadata = dict(row.metadata_ or {}) if isinstance(row.metadata_, dict) else {}
            event_name = str(metadata.get("event") or "").strip()
            if not event_name:
                continue
            events.append(
                {
                    "event": event_name,
                    "data": metadata.get("payload"),
                    "created_at": str(metadata.get("created_at") or (row.created_at.isoformat() if row.created_at else "")),
                }
            )
        return events

    async def get_conversation_context_state(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("context_state")
            return dict(payload) if isinstance(payload, dict) else None

    async def get_conversation_compacted_history(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("compacted_history")
            return dict(payload) if isinstance(payload, dict) else None

    async def get_conversation_history_log(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("history_log")
            return dict(payload) if isinstance(payload, dict) else None

    async def get_conversation_tool_ledger(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("tool_ledger")
            return dict(payload) if isinstance(payload, dict) else None

    async def get_conversation_context_snapshots(self, conversation_id: int) -> List[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return []
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("context_snapshots")
            if not isinstance(payload, list):
                return []
            return [dict(item) for item in payload if isinstance(item, dict)]

    async def get_conversation_item_stream(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("item_stream")
            return dict(payload) if isinstance(payload, dict) else None

    async def get_conversation_turn_store(self, conversation_id: int) -> Optional[Dict[str, Any]]:
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return None
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            payload = metadata.get("turn_store")
            if not isinstance(payload, dict):
                return None
            return ConversationTurnStore.from_payload(dict(payload)).to_payload()

    async def upsert_conversation_context_state(self, conversation_id: int, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict) or not state:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["context_state"] = state
            row.metadata_ = metadata
            await db.commit()

    async def upsert_conversation_compacted_history(self, conversation_id: int, compacted_history: Dict[str, Any]) -> None:
        if not isinstance(compacted_history, dict) or not compacted_history:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["compacted_history"] = compacted_history
            row.metadata_ = metadata
            await db.commit()

    async def upsert_conversation_tool_ledger(self, conversation_id: int, tool_ledger: Dict[str, Any]) -> None:
        if not isinstance(tool_ledger, dict) or not tool_ledger:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["tool_ledger"] = tool_ledger
            row.metadata_ = metadata
            await db.commit()

    async def upsert_conversation_item_stream(self, conversation_id: int, item_stream: Dict[str, Any]) -> None:
        if not isinstance(item_stream, dict) or not item_stream:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["item_stream"] = item_stream
            row.metadata_ = metadata
            await db.commit()

    async def upsert_conversation_turn_store(self, conversation_id: int, turn_store: Dict[str, Any]) -> None:
        if not isinstance(turn_store, dict) or not turn_store:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["turn_store"] = turn_store
            row.metadata_ = metadata
            await db.commit()

    async def upsert_conversation_turn_entry(
        self,
        conversation_id: int,
        entry: Dict[str, Any],
    ) -> None:
        if not isinstance(entry, dict):
            return
        turn_id = str(entry.get("turn_id") or "").strip()
        if not turn_id:
            return
        current_payload = await self.get_conversation_turn_store(conversation_id) or {}
        turn_store = ConversationTurnStore.from_payload(current_payload)
        existing = next((item for item in turn_store.entries if item.turn_id == turn_id), None)
        try:
            iteration_count = max(int(entry.get("iteration_count") if entry.get("iteration_count") is not None else (existing.iteration_count if existing else 0)), 0)
        except Exception:
            iteration_count = existing.iteration_count if existing else 0
        try:
            tool_call_count = max(int(entry.get("tool_call_count") if entry.get("tool_call_count") is not None else (existing.tool_call_count if existing else 0)), 0)
        except Exception:
            tool_call_count = existing.tool_call_count if existing else 0
        try:
            tool_result_count = max(int(entry.get("tool_result_count") if entry.get("tool_result_count") is not None else (existing.tool_result_count if existing else 0)), 0)
        except Exception:
            tool_result_count = existing.tool_result_count if existing else 0
        try:
            user_message_id = int(entry["user_message_id"]) if entry.get("user_message_id") is not None else (existing.user_message_id if existing else None)
        except Exception:
            user_message_id = existing.user_message_id if existing else None
        try:
            assistant_message_id = int(entry["assistant_message_id"]) if entry.get("assistant_message_id") is not None else (existing.assistant_message_id if existing else None)
        except Exception:
            assistant_message_id = existing.assistant_message_id if existing else None
        merged = ConversationTurnEntry(
            turn_id=turn_id,
            status=str(entry.get("status") or (existing.status if existing else "running")).strip().lower() or "running",
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            run_id=self._normalize_optional_text(entry.get("run_id") if entry.get("run_id") is not None else (existing.run_id if existing else None)),
            user_content=self._normalize_optional_text(entry.get("user_content") if entry.get("user_content") is not None else (existing.user_content if existing else None)),
            assistant_summary=self._normalize_optional_text(entry.get("assistant_summary") if entry.get("assistant_summary") is not None else (existing.assistant_summary if existing else None)),
            iteration_count=iteration_count,
            tool_call_count=tool_call_count,
            tool_result_count=tool_result_count,
            error_message=self._normalize_optional_text(entry.get("error_message") if entry.get("error_message") is not None else (existing.error_message if existing else None)),
            started_at=self._normalize_optional_text(entry.get("started_at") if entry.get("started_at") is not None else (existing.started_at if existing else None)) or datetime.utcnow().isoformat(),
            completed_at=self._normalize_optional_text(entry.get("completed_at") if entry.get("completed_at") is not None else (existing.completed_at if existing else None)),
        )
        turn_store.upsert(merged)
        turn_store.compact(max(int(getattr(settings, "agent_context_turn_store_keep_entries", 120) or 120), 20))
        await self.upsert_conversation_turn_store(conversation_id, turn_store.to_payload())

    async def append_conversation_item_entries(
        self,
        conversation_id: int,
        entries: List[Dict[str, Any]],
    ) -> None:
        normalized_entries = [item for item in list(entries or []) if isinstance(item, dict)]
        if not normalized_entries:
            return
        current_payload = await self.get_conversation_item_stream(conversation_id) or {}
        item_stream = ConversationItemStreamStore.from_payload(current_payload)
        prepared: List[ConversationItemEntry] = []
        for item in normalized_entries:
            kind = str(item.get("kind") or "").strip()
            if not kind:
                continue
            role = str(item.get("role") or "").strip() or None
            if role and role not in {"user", "assistant", "system", "tool"}:
                role = None
            try:
                iteration = int(item.get("iteration") or 0)
            except Exception:
                iteration = 0
            try:
                execution_time_ms = (
                    float(item.get("execution_time_ms"))
                    if item.get("execution_time_ms") is not None
                    else None
                )
            except Exception:
                execution_time_ms = None
            try:
                output_tokens_estimate = (
                    int(item.get("output_tokens_estimate"))
                    if item.get("output_tokens_estimate") is not None
                    else None
                )
            except Exception:
                output_tokens_estimate = None
            prepared.append(
                ConversationItemEntry(
                    item_id=str(item.get("item_id") or uuid.uuid4().hex),
                    kind=kind,
                    turn_id=str(item.get("turn_id") or "").strip() or None,
                    role=role,
                    content=str(item.get("content") or "").strip() or None,
                    message_id=int(item["message_id"]) if item.get("message_id") is not None else None,
                    run_id=str(item.get("run_id") or "").strip() or None,
                    iteration=max(0, iteration),
                    tool_name=str(item.get("tool_name") or "").strip() or None,
                    tool_call_id=str(item.get("tool_call_id") or "").strip() or None,
                    status=str(item.get("status") or "").strip() or None,
                    arguments=dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), dict) else None,
                    thought=str(item.get("thought") or "").strip() or None,
                    summary=str(item.get("summary") or "").strip() or None,
                    success=bool(item.get("success")) if item.get("success") is not None else None,
                    error=str(item.get("error") or "").strip() or None,
                    permission_required=bool(item.get("permission_required")),
                    execution_time_ms=execution_time_ms,
                    output_tokens_estimate=output_tokens_estimate,
                    truncated=bool(item.get("truncated")) if item.get("truncated") is not None else None,
                    parallel_group=str(item.get("parallel_group") or "").strip() or None,
                    metadata=dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else None,
                    created_at=str(item.get("created_at") or "").strip() or datetime.utcnow().isoformat(),
                )
            )
        if not prepared:
            return
        item_stream.extend(prepared)
        item_stream.compact(max(int(getattr(settings, "agent_context_item_stream_keep_entries", 320) or 320), 60))
        await self.upsert_conversation_item_stream(conversation_id, item_stream.to_payload())

    async def append_conversation_history_event(self, conversation_id: int, *, title: str, detail: str) -> None:
        current_payload = await self.get_conversation_history_log(conversation_id) or {}
        history_log = HistoryLog.from_payload(current_payload)
        history_log.add(str(title or "").strip() or "event", str(detail or "").strip() or "updated")
        history_log.compact(max(int(getattr(settings, "agent_context_history_log_keep_events", 48) or 48), 10))
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            metadata["history_log"] = history_log.to_payload()
            row.metadata_ = metadata
            await db.commit()
        await self.append_conversation_item_entries(
            conversation_id,
            [
                {
                    "kind": "history_event",
                    "role": "system",
                    "summary": str(title or "").strip() or "event",
                    "content": str(detail or "").strip() or "updated",
                    "created_at": history_log.updated_at or datetime.utcnow().isoformat(),
                }
            ],
        )

    async def _tool_counts_for_turn(self, conversation_id: int, turn_id: str) -> Dict[str, int]:
        payload = await self.get_conversation_tool_ledger(conversation_id) or {}
        store = ToolLedgerStore.from_payload(payload)
        call_count = 0
        result_count = 0
        for entry in list(store.entries or []):
            if str(entry.turn_id or "").strip() != str(turn_id or "").strip():
                continue
            if entry.kind == "tool_call":
                call_count += 1
            elif entry.kind == "tool_result":
                result_count += 1
        return {"tool_call_count": call_count, "tool_result_count": result_count}

    async def append_conversation_tool_ledger_entries(
        self,
        conversation_id: int,
        entries: List[Dict[str, Any]],
    ) -> None:
        normalized_entries = [item for item in list(entries or []) if isinstance(item, dict)]
        if not normalized_entries:
            return
        current_payload = await self.get_conversation_tool_ledger(conversation_id) or {}
        tool_ledger = ToolLedgerStore.from_payload(current_payload)
        prepared: List[ToolLedgerEntry] = []
        for item in normalized_entries:
            tool_name = str(item.get("tool_name") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if not tool_name or not kind:
                continue
            try:
                iteration = int(item.get("iteration") or 0)
            except Exception:
                iteration = 0
            try:
                execution_time_ms = (
                    float(item.get("execution_time_ms"))
                    if item.get("execution_time_ms") is not None
                    else None
                )
            except Exception:
                execution_time_ms = None
            try:
                output_tokens_estimate = (
                    int(item.get("output_tokens_estimate"))
                    if item.get("output_tokens_estimate") is not None
                    else None
                )
            except Exception:
                output_tokens_estimate = None
            prepared.append(
                ToolLedgerEntry(
                    entry_id=str(item.get("entry_id") or uuid.uuid4().hex),
                    kind=kind,
                    tool_name=tool_name,
                    turn_id=str(item.get("turn_id") or "").strip() or None,
                    tool_call_id=str(item.get("tool_call_id") or "").strip() or None,
                    run_id=str(item.get("run_id") or "").strip() or None,
                    iteration=max(0, iteration),
                    status=str(item.get("status") or "").strip() or None,
                    arguments=dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), dict) else None,
                    summary=str(item.get("summary") or "").strip() or None,
                    success=bool(item.get("success")) if item.get("success") is not None else None,
                    error=str(item.get("error") or "").strip() or None,
                    permission_required=bool(item.get("permission_required")),
                    execution_time_ms=execution_time_ms,
                    output_tokens_estimate=output_tokens_estimate,
                    truncated=bool(item.get("truncated")) if item.get("truncated") is not None else None,
                    parallel_group=str(item.get("parallel_group") or "").strip() or None,
                    metadata=dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else None,
                    created_at=str(item.get("created_at") or "").strip() or datetime.utcnow().isoformat(),
                )
            )
        if not prepared:
            return
        tool_ledger.extend(prepared)
        tool_ledger.compact(max(int(getattr(settings, "agent_context_tool_ledger_keep_entries", 240) or 240), 40))
        await self.upsert_conversation_tool_ledger(conversation_id, tool_ledger.to_payload())
        await self.append_conversation_item_entries(
            conversation_id,
            [
                {
                    "kind": entry.kind,
                    "turn_id": entry.turn_id,
                    "role": "tool",
                    "run_id": entry.run_id,
                    "iteration": entry.iteration,
                    "tool_name": entry.tool_name,
                    "tool_call_id": entry.tool_call_id,
                    "status": entry.status,
                    "arguments": dict(entry.arguments or {}) if isinstance(entry.arguments, dict) else None,
                    "summary": entry.summary,
                    "success": entry.success,
                    "error": entry.error,
                    "permission_required": entry.permission_required,
                    "execution_time_ms": entry.execution_time_ms,
                    "output_tokens_estimate": entry.output_tokens_estimate,
                    "truncated": entry.truncated,
                    "parallel_group": entry.parallel_group,
                    "metadata": dict(entry.metadata or {}) if isinstance(entry.metadata, dict) else None,
                    "created_at": entry.created_at or datetime.utcnow().isoformat(),
                }
                for entry in prepared
            ],
        )
        touched_turn_ids = {
            str(entry.turn_id or "").strip()
            for entry in prepared
            if str(entry.turn_id or "").strip()
        }
        for turn_id in touched_turn_ids:
            counts = await self._tool_counts_for_turn(conversation_id, turn_id)
            await self.upsert_conversation_turn_entry(
                conversation_id,
                {
                    "turn_id": turn_id,
                    "tool_call_count": counts["tool_call_count"],
                    "tool_result_count": counts["tool_result_count"],
                },
            )

    async def append_conversation_context_snapshot(self, conversation_id: int, snapshot: Dict[str, Any]) -> None:
        if not isinstance(snapshot, dict) or not snapshot:
            return
        async with async_session_factory() as db:
            row = await db.get(Conversation, int(conversation_id))
            if not row:
                return
            metadata = dict(row.metadata_ or {})
            current = [dict(item) for item in list(metadata.get("context_snapshots") or []) if isinstance(item, dict)]
            current.append(dict(snapshot))
            keep_last = max(int(getattr(settings, "agent_context_snapshot_keep_items", 12) or 12), 1)
            metadata["context_snapshots"] = current[-keep_last:]
            row.metadata_ = metadata
            await db.commit()

    def store_prepared_send_plan(
        self,
        *,
        user_id: int,
        conversation_id: Optional[int],
        llm_provider: str,
        draft_message: str,
        preview_mode: str,
        conversation_revision: Optional[str],
        system_prompt: str,
        llm_messages: List[Dict[str, Any]],
        routing_decision: Optional[Dict[str, Any]] = None,
        tool_selection: Optional[Dict[str, Any]] = None,
        chat_preferences: Optional[Dict[str, Any]] = None,
        rag_overrides: Optional[Dict[str, Any]] = None,
        conversation_state: Optional[Dict[str, Any]] = None,
        compacted_history: Optional[Dict[str, Any]] = None,
        prefetched_rag_messages: Optional[List[Dict[str, Any]]] = None,
        prefetched_rag_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._cleanup_expired_send_plans()
        plan_id = uuid.uuid4().hex
        created_at = datetime.utcnow()
        ttl_seconds = max(int(getattr(settings, "agent_send_plan_ttl_seconds", 900) or 900), 60)
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        record = PreparedSendPlanRecord(
            plan_id=plan_id,
            user_id=int(user_id),
            conversation_id=int(conversation_id) if conversation_id is not None else None,
            llm_provider=str(llm_provider or "").strip() or "unknown",
            draft_message=str(draft_message or ""),
            preview_mode=str(preview_mode or "agent"),
            conversation_revision=str(conversation_revision or "").strip() or None,
            draft_hash=self._hash_draft_message(draft_message),
            system_prompt=str(system_prompt or ""),
            llm_messages=[dict(item) for item in list(llm_messages or []) if isinstance(item, dict)],
            routing_decision=dict(routing_decision or {}) if isinstance(routing_decision, dict) else None,
            tool_selection=dict(tool_selection or {}) if isinstance(tool_selection, dict) else {},
            chat_preferences=self._normalize_chat_preferences(chat_preferences or {}),
            rag_overrides=self.normalize_chat_rag_overrides(rag_overrides),
            conversation_state=dict(conversation_state or {}) if isinstance(conversation_state, dict) else {},
            compacted_history=dict(compacted_history or {}) if isinstance(compacted_history, dict) else {},
            prefetched_rag_messages=[
                dict(item)
                for item in list(prefetched_rag_messages or [])
                if isinstance(item, dict)
            ],
            prefetched_rag_metadata=(
                dict(prefetched_rag_metadata or {})
                if isinstance(prefetched_rag_metadata, dict)
                else {}
            ),
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        self._prepared_send_plans[plan_id] = record
        return {
            "plan_id": plan_id,
            "preview_mode": record.preview_mode,
            "reusable": True,
            "draft_message": record.draft_message,
            "draft_hash": record.draft_hash,
            "conversation_revision": record.conversation_revision,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "message_count_sent": len(record.llm_messages),
        }

    def take_prepared_send_plan(
        self,
        *,
        plan_id: Optional[str],
        user_id: int,
        conversation_id: Optional[int],
        draft_message: str,
        llm_provider: str,
        conversation_revision: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        self._cleanup_expired_send_plans()
        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_plan_id:
            return None
        record = self._prepared_send_plans.get(normalized_plan_id)
        if not record:
            return None
        if record.user_id != int(user_id):
            return None
        if record.conversation_id != (int(conversation_id) if conversation_id is not None else None):
            return None
        if record.draft_hash != self._hash_draft_message(draft_message):
            return None
        if str(record.llm_provider or "").strip() != str(llm_provider or "").strip():
            return None
        if str(record.conversation_revision or "").strip() != str(conversation_revision or "").strip():
            return None
        self._prepared_send_plans.pop(normalized_plan_id, None)
        return {
            "plan_id": record.plan_id,
            "preview_mode": record.preview_mode,
            "conversation_revision": record.conversation_revision,
            "draft_hash": record.draft_hash,
            "system_prompt": record.system_prompt,
            "llm_messages": [dict(item) for item in record.llm_messages],
            "routing_decision": dict(record.routing_decision or {}) if record.routing_decision else None,
            "tool_selection": dict(record.tool_selection or {}),
            "chat_preferences": dict(record.chat_preferences or {}),
            "rag_overrides": dict(record.rag_overrides or {}),
            "conversation_state": dict(record.conversation_state or {}),
            "compacted_history": dict(record.compacted_history or {}),
            "prefetched_rag_messages": [dict(item) for item in record.prefetched_rag_messages],
            "prefetched_rag_metadata": dict(record.prefetched_rag_metadata or {}),
            "created_at": record.created_at,
            "expires_at": record.expires_at,
        }

    async def remember(
        self,
        *,
        user_id: int,
        channel: str,
        scope_type: str,
        scope_id: str,
        content: str,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        memory_control = await self.get_user_memory_control(user_id=user_id, channel=channel)
        if not bool(memory_control.get("effective_enabled", False)):
            return
        text = (content or "").strip()
        if not text:
            return

        embedding: Optional[List[float]] = None
        try:
            embedding = await get_embedding_service().embed_text(text, is_query=False)
        except Exception as exc:  # pragma: no cover - degraded path
            logger.warning(f"[AgentMemory] embed failed, store raw text only: {exc}")

        retention_days = max(int(getattr(settings, "agent_memory_retention_days", 180)), 1)
        retention_cutoff = datetime.utcnow() - timedelta(days=retention_days)
        cap_per_user_channel = max(int(getattr(settings, "agent_memory_max_items_per_user_channel", 2000)), 100)

        async with async_session_factory() as db:
            await db.execute(
                delete(AgentMemoryItem).where(
                    AgentMemoryItem.user_id == int(user_id),
                    AgentMemoryItem.channel == str(channel),
                    AgentMemoryItem.created_at < retention_cutoff,
                )
            )
            db.add(
                AgentMemoryItem(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    channel=channel,
                    scope_type=scope_type,
                    scope_id=str(scope_id),
                    content=text,
                    embedding=embedding,
                    importance=float(importance),
                    metadata_=metadata or {},
                )
            )
            await db.commit()

            result = await db.execute(
                select(AgentMemoryItem.id)
                .where(
                    AgentMemoryItem.user_id == int(user_id),
                    AgentMemoryItem.channel == str(channel),
                )
                .order_by(desc(AgentMemoryItem.created_at))
                .offset(cap_per_user_channel)
            )
            stale_ids = [str(row[0]) for row in result.all() if row and row[0]]
            if stale_ids:
                await db.execute(delete(AgentMemoryItem).where(AgentMemoryItem.id.in_(stale_ids)))
                await db.commit()

    async def recall(
        self,
        *,
        user_id: int,
        channel: str,
        scope_type: Optional[str],
        scope_id: Optional[str],
        query: str,
        top_k: int = 3,
    ) -> List[MemoryContext]:
        memory_control = await self.get_user_memory_control(user_id=user_id, channel=channel)
        if not bool(memory_control.get("effective_enabled", False)):
            return []

        text = (query or "").strip()
        if not text:
            return []

        query_embedding: Optional[List[float]] = None
        embedding_service = get_embedding_service()
        try:
            query_embedding = await embedding_service.embed_text(text, is_query=True)
        except Exception as exc:  # pragma: no cover - degraded path
            logger.warning(f"[AgentMemory] query embed failed, fallback to recency: {exc}")

        retention_days = max(int(getattr(settings, "agent_memory_retention_days", 180)), 1)
        retention_cutoff = datetime.utcnow() - timedelta(days=retention_days)
        scan_limit = max(int(getattr(settings, "agent_memory_scan_limit", 200)), 20)
        scope_boost = float(getattr(settings, "agent_memory_scope_match_boost", 0.18))
        user_scope_boost = min(max(scope_boost * 0.4, 0.02), 0.08)

        async with async_session_factory() as db:
            result = await db.execute(
                select(AgentMemoryItem)
                .where(
                    AgentMemoryItem.user_id == user_id,
                    AgentMemoryItem.channel == channel,
                    AgentMemoryItem.created_at >= retention_cutoff,
                )
                .order_by(desc(AgentMemoryItem.created_at))
                .limit(scan_limit)
            )
            rows = list(result.scalars().all())

            if not rows:
                return []

            scored: List[tuple[AgentMemoryItem, float]] = []
            now = datetime.utcnow()
            for row in rows:
                score = 0.0
                if query_embedding and isinstance(row.embedding, list):
                    try:
                        score = embedding_service.cosine_similarity(query_embedding, row.embedding)
                    except Exception:
                        score = 0.0
                if score <= 0:
                    score = 0.1
                if scope_type and scope_id and row.scope_type == str(scope_type) and row.scope_id == str(scope_id):
                    score += scope_boost
                elif row.scope_type == "user":
                    score += user_scope_boost
                if row.created_at:
                    age_seconds = max((now - row.created_at).total_seconds(), 0.0)
                    recency_ratio = max(0.0, 1.0 - min(age_seconds / (86400.0 * 30.0), 1.0))
                    score += recency_ratio * 0.06
                score += float(row.importance or 0.0) * 0.05
                scored.append((row, score))

            scored.sort(key=lambda item: item[1], reverse=True)
            selected = scored[: max(1, int(top_k or 1))]

            contexts: List[MemoryContext] = []
            for row, score in selected:
                row.last_accessed_at = now
                contexts.append(
                    MemoryContext(
                        content=row.content,
                        score=round(float(score), 4),
                        created_at=row.created_at.isoformat() if row.created_at else "",
                    )
                )
            await db.commit()
            return contexts


_runtime_service = AgentRuntimeService()


def get_agent_runtime_service() -> AgentRuntimeService:
    return _runtime_service
