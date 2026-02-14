"""
Persistence helpers for agent runtime traces, summaries and long-term memory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import desc, select

from app.config import settings
from app.core.database import async_session_factory
from app.models.agent import (
    AgentMemoryItem,
    AgentRun,
    AgentStepRecord,
    ConversationSummary,
)
from app.services.embedding_service import get_embedding_service
from app.services.smart_chunking.token_utils import estimate_tokens


@dataclass
class MemoryContext:
    content: str
    score: float
    created_at: str


class AgentRuntimeService:
    """Service for persisting and loading agent runtime artifacts."""

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

    async def get_latest_conversation_summary(self, conversation_id: int) -> Optional[str]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == conversation_id)
                .order_by(desc(ConversationSummary.updated_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row.summary_text if row else None

    async def upsert_conversation_summary(
        self,
        conversation_id: int,
        summary_text: str,
        *,
        up_to_message_id: Optional[int] = None,
    ) -> None:
        if not summary_text:
            return

        summary_text = summary_text.strip()
        token_count = estimate_tokens(summary_text)
        async with async_session_factory() as db:
            result = await db.execute(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == conversation_id)
                .order_by(desc(ConversationSummary.updated_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row:
                row.summary_text = summary_text
                row.token_count = token_count
                row.up_to_message_id = up_to_message_id
                row.updated_at = datetime.utcnow()
            else:
                db.add(
                    ConversationSummary(
                        conversation_id=conversation_id,
                        summary_text=summary_text,
                        token_count=token_count,
                        up_to_message_id=up_to_message_id,
                    )
                )
            await db.commit()

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
        if not bool(getattr(settings, "agent_longterm_memory_enabled", False)):
            return
        text = (content or "").strip()
        if not text:
            return

        embedding: Optional[List[float]] = None
        try:
            embedding = await get_embedding_service().embed_text(text, is_query=False)
        except Exception as exc:  # pragma: no cover - degraded path
            logger.warning(f"[AgentMemory] embed failed, store raw text only: {exc}")

        async with async_session_factory() as db:
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

    async def recall(
        self,
        *,
        user_id: int,
        channel: str,
        scope_type: str,
        scope_id: str,
        query: str,
        top_k: int = 3,
    ) -> List[MemoryContext]:
        if not bool(getattr(settings, "agent_longterm_memory_enabled", False)):
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

        async with async_session_factory() as db:
            result = await db.execute(
                select(AgentMemoryItem)
                .where(
                    AgentMemoryItem.user_id == user_id,
                    AgentMemoryItem.channel == channel,
                    AgentMemoryItem.scope_type == scope_type,
                    AgentMemoryItem.scope_id == str(scope_id),
                )
                .order_by(desc(AgentMemoryItem.created_at))
                .limit(50)
            )
            rows = list(result.scalars().all())

            if not rows:
                return []

            scored: List[tuple[AgentMemoryItem, float]] = []
            for row in rows:
                score = 0.0
                if query_embedding and isinstance(row.embedding, list):
                    try:
                        score = embedding_service.cosine_similarity(query_embedding, row.embedding)
                    except Exception:
                        score = 0.0
                if score <= 0:
                    score = 0.1
                score += float(row.importance or 0.0) * 0.05
                scored.append((row, score))

            scored.sort(key=lambda item: item[1], reverse=True)
            selected = scored[: max(1, top_k)]

            now = datetime.utcnow()
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
